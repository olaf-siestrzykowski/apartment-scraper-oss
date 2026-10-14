import argparse
import pandas as pd
import numpy as np
from bs4 import BeautifulSoup
import datetime as dt
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import sys
from playwright.sync_api import Playwright, sync_playwright
import time
import random
from datetime import timedelta
from pathlib import Path
import json
from typing import Optional, Dict, Any, List

import logging

from geo import API_KEY, calculate_distances_for_offers
from parsing.description import add_dishwasher_column, apply_custom_fields_extraction
from parsing.offers import validate_offer_data
from parsing.prices import add_total_costs
from scrapers.base import PortalScraper
from scrapers.olx import OlxScraper
from scrapers.otodom import OtodomScraper
from sinks.sheets import (
    GOOGLE_CREDS_PATH,
    _open_spreadsheet,
    get_image_formula,
    load_config_sheet,
    read_existing_llm_scores_map,
    read_existing_status_map,
    reorder_dataframe_columns,
    restore_status_column,
    translate_header_row,
    update_config_last_scraped,
)
from sinks.telegram import send_telegram_notification
from logging_config import setup_comprehensive_logging

# setup_comprehensive_logging() (called from main) configures this same logger and
# creates the log/debug folders - importing the module has no side effects.
logger = logging.getLogger("logging_config")
debug_handler = None


class ProgressTracker:
    """Track progress with ETA calculation for data extraction"""

    def __init__(self, total: int, description: str = "Processing"):
        self.total = total
        self.description = description
        self.start_time = None
        self.completed = 0
        self.times = []  # Store individual processing times

    def start(self):
        """Start the progress tracker"""
        self.start_time = time.time()
        self.completed = 0
        self.times = []
        logger.info(f"📊 {self.description}: Starting {self.total} items")

    def update(self, item_start_time: float = None):
        """Update progress after completing an item"""
        self.completed += 1

        if item_start_time:
            item_duration = time.time() - item_start_time
            self.times.append(item_duration)

        elapsed = time.time() - self.start_time
        percent = (self.completed / self.total) * 100

        # Calculate ETA based on average time per item
        if self.times:
            avg_time = sum(self.times) / len(self.times)
            remaining_items = self.total - self.completed
            eta_seconds = avg_time * remaining_items
            eta_str = str(timedelta(seconds=int(eta_seconds)))
        else:
            # Fallback: estimate based on elapsed time
            if self.completed > 0:
                avg_time = elapsed / self.completed
                remaining_items = self.total - self.completed
                eta_seconds = avg_time * remaining_items
                eta_str = str(timedelta(seconds=int(eta_seconds)))
            else:
                eta_str = "calculating..."

        elapsed_str = str(timedelta(seconds=int(elapsed)))

        # Create progress bar
        bar_length = 30
        filled = int(bar_length * self.completed / self.total)
        bar = '█' * filled + '░' * (bar_length - filled)

        logger.info(
            f"📈 [{bar}] {percent:5.1f}% | {self.completed}/{self.total} | "
            f"Elapsed: {elapsed_str} | ETA: {eta_str}"
        )

    def finish(self):
        """Mark progress as complete and log summary"""
        elapsed = time.time() - self.start_time
        elapsed_str = str(timedelta(seconds=int(elapsed)))

        if self.times:
            avg_time = sum(self.times) / len(self.times)
            logger.info(
                f"✅ {self.description} complete: {self.completed}/{self.total} items in {elapsed_str} "
                f"(avg: {avg_time:.1f}s/item)"
            )
        else:
            logger.info(f"✅ {self.description} complete: {self.completed}/{self.total} items in {elapsed_str}")


def load_profiles() -> dict:
    """Load search profiles from profiles.json next to this script.

    Set APARTMENT_PROFILES_PATH to load a different file (the test suite points it
    at profiles.example.json so it runs on a fresh clone).
    """
    profiles_path = Path(os.environ.get("APARTMENT_PROFILES_PATH")
                         or Path(__file__).parent / "profiles.json")
    if not profiles_path.exists():
        raise FileNotFoundError(
            f"profiles.json not found at {profiles_path}. "
            "Run: python setup.py"
        )
    with profiles_path.open(encoding="utf-8") as f:
        data = json.load(f)
    return {p["name"]: p for p in data["profiles"]}


def build_arg_parser(profile_names: List[str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape flats from OLX/Otodom")
    parser.add_argument("--search", choices=profile_names, default=profile_names[0],
                        help="Search profile to run (defined in profiles.json)")
    parser.add_argument("--reextract", action="store_true",
                        help="Re-extract data from saved HTML files instead of scraping")
    parser.add_argument("--html-folder", type=str, default=None,
                        help="Folder containing saved HTML files for re-extraction (default: today's folder)")
    parser.add_argument("--pickle-file", type=str, default=None,
                        help="Pickle file to load/update for re-extraction or distance calculation")
    parser.add_argument("--force-all", action="store_true",
                        help="Force re-extraction even for offers that already have data")
    parser.add_argument("--calc-distance", action="store_true",
                        help="Calculate distance from apartments to office")
    parser.add_argument("--office-address", type=str, default=None,
                        help="Override office address for distance calculation")
    parser.add_argument("--transport-mode",
                        choices=["driving-car", "cycling-regular", "foot-walking", "public-transport"],
                        default=None, help="Transport mode (overrides config sheet; default: foot-walking)")
    return parser


# ============================================================================
# CONFIGURATION - CREDENTIALS AND API KEYS (Use environment variables!)
# ============================================================================

# Active run configuration - filled in by configure() before run() / re-extraction.
args = argparse.Namespace(reextract=False, html_folder=None, pickle_file=None, force_all=False,
                          calc_distance=False, office_address=None, transport_mode=None)
search: Optional[str] = None
olx_url: Optional[str] = None
otodom_url: Optional[str] = None
sheet_id: Optional[str] = None
origin: Optional[str] = None
search_city: Optional[str] = None
_profile: Dict[str, Any] = {}

offers_df = pd.DataFrame()
# Holds config loaded from the "config" sheet at runtime (populated by run())
_run_config: dict = {}


def configure(cli_args: argparse.Namespace, profile: Dict[str, Any]) -> None:
    """Set the active search profile and CLI options for this process."""
    global args, _profile, search, olx_url, otodom_url, sheet_id, origin, search_city
    args = cli_args
    _profile = profile
    search = profile["name"]
    olx_url = profile["olx_url"]
    otodom_url = profile["otodom_url"]
    sheet_id = profile["sheet_id"]
    origin = profile["origin_address"]
    search_city = profile["city"]
    if not API_KEY:
        logger.warning("⚠️  OPENROUTESERVICE_API_KEY not set in environment. Distance calculation will fail.")
    logger.info(f'Search configuration: {search} (city: {search_city}, office: {origin})')


LISTINGS_WORKSHEET_DEFAULT = "apartment list"


def listings_worksheet_name() -> str:
    """Tab the listings are written to - profile key "worksheet", default "apartment list"."""
    return (_profile.get("worksheet") or "").strip() or LISTINGS_WORKSHEET_DEFAULT


def open_listings_worksheet(spreadsheet):
    """Open the listings tab, creating it if the spreadsheet does not have it yet."""
    name = listings_worksheet_name()
    try:
        return spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound:
        logger.info(f"📄 Creating worksheet '{name}'")
        return spreadsheet.add_worksheet(title=name, rows=1000, cols=80)


def merge_portal_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Merge columns that have different names in OLX vs Otodom but contain the same data.
    This consolidates portal-specific columns into unified columns.
    """
    # Define merge mappings: (unified_name, [source_columns in priority order])
    merge_mappings = [
        # Image URL: OLX uses 'Photo_URL', Otodom uses 'Main_Image_URL'
        ('Image_URL', ['Main_Image_URL', 'Photo_URL']),
        # Price detail: OLX uses 'Price_Detail', Otodom uses 'Price_Text'
        ('Price_Detail', ['Price_Detail', 'Price_Text']),
        # Seller info: OLX uses 'Seller', Otodom uses 'Seller_Info'
        ('Seller_Info', ['Seller_Info', 'Seller']),
    ]

    for unified_name, source_cols in merge_mappings:
        # Check which source columns exist
        existing_sources = [col for col in source_cols if col in df.columns]

        if not existing_sources:
            continue

        # Create unified column by coalescing values (first non-empty wins)
        if unified_name not in df.columns:
            df[unified_name] = None

        for idx in df.index:
            # Skip if unified column already has a value
            current_val = df.at[idx, unified_name]
            if pd.notna(current_val) and str(current_val).strip() not in ['', 'None', 'Not found']:
                continue

            # Try each source column in priority order
            for source_col in existing_sources:
                if source_col == unified_name:
                    continue
                val = df.at[idx, source_col]
                if pd.notna(val) and str(val).strip() not in ['', 'None', 'Not found']:
                    df.at[idx, unified_name] = val
                    break

        # Drop source columns that are different from unified name
        cols_to_drop = [col for col in existing_sources if col != unified_name and col in df.columns]
        df = df.drop(columns=cols_to_drop, errors='ignore')

    return df


# ============================================================================
# CUSTOM FIELDS EXTRACTION FROM OPIS (Description)
# ============================================================================


def log_scraping_stats():
    """Log comprehensive scraping statistics with safe column checks"""
    global offers_df

    if offers_df.empty:
        logger.warning("📊 No data to analyze - offers_df is empty")
        return

    try:
        # Total offers
        total_offers = len(offers_df)

        # Safe OLX / Otodom counts
        olx_offers = offers_df["Link"].str.contains("olx.pl", na=False).sum() if "Link" in offers_df.columns else 0
        otodom_offers = offers_df["Link"].str.contains("otodom.pl", na=False).sum() if "Link" in offers_df.columns else 0

        # --- PRICE STATISTICS ---
        if "Base_value" in offers_df.columns:
            # Ensure numeric conversion
            valid_prices = pd.to_numeric(offers_df["Base_value"], errors="coerce").dropna()
            valid_prices = valid_prices[valid_prices > 0]

            if not valid_prices.empty:
                price_stats = {
                    "min": round(valid_prices.min(), 2),
                    "max": round(valid_prices.max(), 2),
                    "mean": round(valid_prices.mean(), 2),
                    "median": round(valid_prices.median(), 2)
                }
            else:
                price_stats = {"message": "No valid prices found"}
        else:
            price_stats = {"message": "Base_value column missing"}

        # --- MISSING DATA ANALYSIS ---
        missing_data = {}

        # Check missing addresses
        if "Address" in offers_df.columns:
            missing_data["no_address"] = offers_df["Address"].isin(["", "Unknown", "Unknown location"]).sum()
        else:
            missing_data["no_address"] = "Address column missing"

        # Check missing area
        if "Area" in offers_df.columns:
            missing_data["no_area"] = offers_df["Area"].isin(["", 0]).sum()
        else:
            missing_data["no_area"] = "Area column missing"

        # Check scraping errors safely
        missing_data["scraping_errors"] = (
            offers_df["Scraping_Error"].ne("").sum()
            if "Scraping_Error" in offers_df.columns
            else 0
        )

        # --- LOGGING ---
        logger.info("📊 SCRAPING STATISTICS:")
        logger.info(f"   📁 Total offers: {total_offers}")
        logger.info(f"   📱 OLX offers: {olx_offers} ({(olx_offers/total_offers*100):.1f}%)")
        logger.info(f"   🏠 Otodom offers: {otodom_offers} ({(otodom_offers/total_offers*100):.1f}%)")
        logger.info(f"   💰 Price stats: {price_stats}")
        logger.info(f"   ⚠️ Data quality: {missing_data}")

    except Exception as e:
        logger.error(f"❌ Failed to generate scraping statistics: {e}")


def save_basic_data_to_sheets():
    """Save basic scraped data to sheets before extracting details"""
    global offers_df
    
    if offers_df.empty:
        logger.warning("⚠️ No data to save - offers_df is empty")
        return
    
    try:
        logger.info(f"💾 Saving basic data ({len(offers_df)} offers) to sheets before detail extraction...")
        
        # Validate data before saving
        valid_offers = 0
        invalid_offers = 0
        
        for idx, row in offers_df.iterrows():
            is_valid, issues = validate_offer_data(row.to_dict(), "Basic")
            if not is_valid:
                invalid_offers += 1
                logger.warning(f"⚠️ Invalid offer at index {idx}: {'; '.join(issues[:2])}...")  # Log first 2 issues
            else:
                valid_offers += 1
        
        logger.info(f"📊 Data validation: {valid_offers} valid, {invalid_offers} invalid offers")
        
        # Clean and prepare data for sheets
        basic_df = offers_df.copy()
        basic_df = basic_df.replace({np.nan: ""})
        
        # Remove any existing detailed columns to keep only basic data
        basic_columns = ['Name', 'Base_value', 'Additional_value', 'Full_value', 'Area', 'Address', 'Link']
        available_columns = [col for col in basic_columns if col in basic_df.columns]
        basic_df = basic_df[available_columns]
        
        # Convert to list format for sheets
        header_language = _run_config.get("language", "en")
        basic_data_list = [translate_header_row(basic_df.columns.to_list(), header_language)] + basic_df.values.tolist()
        
        # Connect to Google Sheets
        json_creds = GOOGLE_CREDS_PATH
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_name(json_creds, scope)
        client = gspread.authorize(creds)
        
        spreadsheet = client.open_by_key(sheet_id)
        ws = open_listings_worksheet(spreadsheet)
        
        # Save basic data
        logger.info("🧹 Clearing existing data...")
        ws.batch_clear(["B:BJ"])  # Clear basic range only
        last_row = len(offers_df)
        formulas = [f"""=ARRAYFORMULA(IF($AZ2:$AZ{last_row}="", IMAGE($X2:$X{last_row}), IMAGE($AZ2:$AZ{last_row})))"""]
        ws.update(values=[formulas], range_name='A2:A2', raw=False)
        logger.info("📝 Uploading basic offer data...")
        ws.update(values=basic_data_list, range_name='B1')
        
        logger.info(f"✅ Successfully saved {len(offers_df)} basic offers to sheets")
        
    except Exception as e:
        logger.error(f"❌ Failed to save basic data to sheets: {e}")
        # Don't stop execution, just log the error


def save_full_data_to_sheets():
    """Save all available OLX scraped data (basic + details) to Google Sheets"""
    global offers_df

    if offers_df.empty:
        logger.warning("⚠️ No data to save - offers_df is empty")
        return

    try:
        logger.info(f"💾 Saving FULL scraped data ({len(offers_df)} offers) to sheets...")

        # Validate data before saving
        valid_offers = 0
        invalid_offers = 0

        for idx, row in offers_df.iterrows():
            is_valid, issues = validate_offer_data(row.to_dict(), "Full")
            if not is_valid:
                invalid_offers += 1
                logger.warning(f"⚠️ Invalid offer at index {idx}: {'; '.join(issues[:2])}...")
            else:
                valid_offers += 1

        logger.info(f"📊 Data validation: {valid_offers} valid, {invalid_offers} invalid offers")

        # Clean dataframe before saving
        full_df = offers_df.copy()
        full_df = full_df.replace({np.nan: ""})

        # Merge portal-specific columns and reorder
        full_df = merge_portal_columns(full_df)
        full_df = reorder_dataframe_columns(full_df)

        # Ensure Status column exists (new offers default to "New")
        if "Status" not in full_df.columns:
            full_df.insert(0, "Status", "New")
        else:
            full_df["Status"] = full_df["Status"].replace("", "New").fillna("New")

        # Prepare final data list for Sheets
        header_language = _run_config.get("language", "en")
        full_data_list = [translate_header_row(full_df.columns.to_list(), header_language)] + full_df.values.tolist()

        # Connect to Google Sheets
        json_creds = GOOGLE_CREDS_PATH
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_name(json_creds, scope)
        client = gspread.authorize(creds)

        spreadsheet = client.open_by_key(sheet_id)
        ws = open_listings_worksheet(spreadsheet)

        # Read existing statuses before clearing
        existing_status = read_existing_status_map(ws)

        logger.info("🧹 Clearing all existing data in the target range...")
        ws.clear()

        # Add image formulas in column A
        last_row = len(full_df)
        formula = get_image_formula(full_df, last_row)
        if formula:
            ws.update(values=[[formula]], range_name='A2:A2', raw=False)

        # Upload full offer data
        logger.info("📝 Uploading FULL offer data...")
        ws.update(values=full_data_list, range_name='B1')

        # Restore statuses for offers that existed before
        restore_status_column(ws, existing_status)

        logger.info(f"✅ Successfully saved {len(full_df)} full offers to sheets")

    except Exception as e:
        logger.error(f"❌ Failed to save FULL data to sheets: {e}")


# ----------------------- OTODOM -----------------------


def reextract_from_saved_html(html_folder: str = None, offers_df_input: pd.DataFrame = None,
                              force_all: bool = False) -> pd.DataFrame:
    """
    Re-extract data from saved HTML files for offers that failed extraction.
    This is useful when 404 detection was too aggressive or extraction failed.

    Args:
        html_folder: Path to folder containing saved HTML files. If None, uses today's folder.
        offers_df_input: DataFrame to update. If None, uses global offers_df.
        force_all: If True, re-extract all offers even if they already have data.

    Returns:
        Updated DataFrame with re-extracted data
    """
    global offers_df

    if offers_df_input is not None:
        df = offers_df_input.copy()
    else:
        df = offers_df.copy() if offers_df is not None else pd.DataFrame()

    if df.empty:
        logger.error("❌ No DataFrame to update. Load a pickle file first with --pickle-file")
        return df

    if html_folder is None:
        today = dt.datetime.now().strftime("%Y-%m-%d")
        html_folder = f"exports/html/{today}"

    html_path = Path(html_folder)
    if not html_path.exists():
        logger.error(f"❌ HTML folder not found: {html_folder}")
        return df

    # Find all HTML files (excluding listing pages)
    html_files = [f for f in html_path.glob("*.html") if not f.name.startswith("listing")]
    logger.info(f"📁 Found {len(html_files)} HTML files in {html_folder}")
    logger.info(f"📊 DataFrame has {len(df)} rows")
    logger.info(f"🔄 Force all: {force_all}")

    scrapers_by_name = {scraper.name: scraper for scraper in make_scrapers()}

    # Track progress
    progress = ProgressTracker(len(html_files), "Re-extraction from HTML")
    progress.start()

    reextracted_count = 0
    skipped_count = 0
    failed_count = 0
    fields_updated = {}

    for html_file in html_files:
        item_start = time.time()
        try:
            # Parse filename to get index and source
            # Format: source_index_timestamp_name.html
            filename = html_file.name
            parts = filename.split('_')

            if len(parts) < 3:
                logger.debug(f"Skipping file with unexpected format: {filename}")
                skipped_count += 1
                progress.update(item_start)
                continue

            source = parts[0]  # olx or otodom
            try:
                index = int(parts[1])
            except ValueError:
                logger.debug(f"Could not parse index from: {filename}")
                skipped_count += 1
                progress.update(item_start)
                continue

            # Check if this index exists in DataFrame
            if index not in df.index:
                logger.debug(f"Index {index} not found in DataFrame, skipping")
                skipped_count += 1
                progress.update(item_start)
                continue

            # Check if we already have good data for this offer (unless force_all)
            if not force_all:
                current_row = df.loc[index]
                has_description = pd.notna(current_row.get("Description")) and len(str(current_row.get("Description", ""))) > 20
                has_title = pd.notna(current_row.get("Title")) and len(str(current_row.get("Title", ""))) > 5

                if has_description and has_title:
                    logger.debug(f"Index {index} already has good data, skipping")
                    skipped_count += 1
                    progress.update(item_start)
                    continue

            # Read and parse HTML
            with open(html_file, 'r', encoding='utf-8', errors='replace') as f:
                html_content = f.read()

            soup = BeautifulSoup(html_content, "html.parser")

            # Extract data with the scraper whose name prefixes the file
            scraper = scrapers_by_name.get(source)
            if scraper is None:
                logger.debug(f"Unknown source: {source}")
                skipped_count += 1
                progress.update(item_start)
                continue
            extracted = scraper.extract_from_soup(soup, index)

            if extracted:
                # Update DataFrame with extracted data
                updated_fields = []
                for key, value in extracted.items():
                    # Update if: force_all, or column doesn't exist, or value is empty/NaN
                    should_update = force_all or key not in df.columns or pd.isna(df.at[index, key])
                    if not should_update and key in df.columns:
                        current_val = df.at[index, key]
                        should_update = current_val == "" or current_val is None

                    if value and should_update:
                        if key not in df.columns:
                            df[key] = None
                        df.at[index, key] = value
                        updated_fields.append(key)
                        fields_updated[key] = fields_updated.get(key, 0) + 1

                if updated_fields:
                    reextracted_count += 1
                    logger.debug(f"✅ {source} offer {index}: updated {len(updated_fields)} fields")
                else:
                    skipped_count += 1
            else:
                failed_count += 1
                logger.debug(f"❌ No data extracted from {filename}")

        except Exception as e:
            logger.error(f"❌ Error re-extracting from {html_file.name}: {e}")
            failed_count += 1

        progress.update(item_start)

    progress.finish()

    # Summary statistics
    logger.info(f"\n{'='*60}")
    logger.info("📊 RE-EXTRACTION SUMMARY")
    logger.info(f"{'='*60}")
    logger.info(f"✅ Successfully updated: {reextracted_count} offers")
    logger.info(f"⏭️ Skipped (already had data): {skipped_count} offers")
    logger.info(f"❌ Failed to extract: {failed_count} offers")
    logger.info("\n📋 Fields updated (top 20):")
    for field, count in sorted(fields_updated.items(), key=lambda x: -x[1])[:20]:
        logger.info(f"   {field}: {count} times")
    logger.info(f"{'='*60}\n")

    # Update global if we used it
    if offers_df_input is None:
        offers_df = df

    return df


def _add_offers(rows: List[Dict[str, Any]]) -> int:
    """Append offers parsed from one search results page to the run's offers_df."""
    global offers_df
    if rows:
        offers_df = pd.concat([offers_df, pd.DataFrame(rows)], ignore_index=True)
    return len(rows)


def make_scrapers() -> List[PortalScraper]:
    """The portals this run scrapes, in listing-collection order."""
    return [OlxScraper(debug_handler=debug_handler), OtodomScraper()]


def _load_run_config() -> None:
    """Load the profile's config tab (falls back to profiles.json) into _run_config."""
    global _run_config

    # Load per-profile config from Google Sheets (creates sheet if missing)
    _spreadsheet = None
    try:
        _spreadsheet = _open_spreadsheet(sheet_id)
        _run_config = load_config_sheet(
            _spreadsheet, search,
            default_olx_url=olx_url,
            default_otodom_url=otodom_url,
            default_origin=origin,
        )
    except Exception as e:
        logger.warning(f"⚠️ Could not load config sheet, using built-in defaults: {e}")
        _run_config = {"reference_address": origin, "olx_url": olx_url, "otodom_url": otodom_url}

    _run_config["_spreadsheet"] = _spreadsheet  # stash for later use

    # Merge email + LLM fields from profiles.json (used when config sheet fields are blank)
    for _email_key in ("email_sender", "email_recipient", "email_app_password", "email_districts", "email_top_n",
                       "llm_preferences", "groq_model"):
        if not _run_config.get(_email_key):
            _run_config[_email_key] = _profile.get(_email_key, "")

    # Language for Sheet headers / email digest / Telegram messages (config sheet wins)
    if not _run_config.get("language"):
        _run_config["language"] = _profile.get("language", "en")


def _launch_browser(playwright: Playwright):
    """Headless Chromium with a Polish locale and the webdriver flag hidden."""
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            '--disable-blink-features=AutomationControlled',
            '--disable-dev-shm-usage',
            '--no-sandbox',
        ]
    )
    context = browser.new_context(
        viewport={'width': 1920, 'height': 1080},
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        locale='pl-PL',
        timezone_id='Europe/Warsaw',
    )

    # Stealth mode: hide webdriver detection
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['pl-PL', 'pl', 'en-US', 'en']});
        window.chrome = {runtime: {}};
    """)
    return browser, context



def _finish_listing_phase() -> None:
    """Dedup, log stats, compute total costs and save the basic rows before detail extraction."""
    global offers_df

    logger.info(f"Scraping complete. Total offers: {len(offers_df)}")
    offers_df.to_csv(f'all_offers_df_{search}.csv')
    offers_df = offers_df.drop_duplicates(subset=['Link']).reset_index(drop=True)
    logger.info(f"After dedup: {len(offers_df)} offers")

    # Generate and log comprehensive statistics
    log_scraping_stats()

    # SAVE BASIC DATA TO SHEETS FIRST (before detailed extraction)
    logger.info("💾 Saving basic offer data to sheets before extracting details...")

    try:
        add_total_costs(offers_df)

        logger.info("✅ Data processing completed successfully")

    except Exception as e:
        logger.error(f"❌ Failed to process data: {e}")

    offers_df = offers_df.sort_values(by='Full_value', ascending=True).reset_index(drop=True)
    save_basic_data_to_sheets()


def _extract_all_details(context, scrapers: List[PortalScraper]) -> None:
    """Open every offer page (portals interleaved, in batches) and fill in details."""
    # PART 3 - Scrape all offers (DETAILED EXTRACTION PHASE)
    logger.info("🔍 Starting mixed detailed extraction phase...")

    # Randomize order within each portal to distribute server load and avoid patterns
    indices_by_scraper = {}
    for scraper in scrapers:
        portal_offers = offers_df[offers_df["Link"].str.contains(scraper.domain, na=False, regex=False)]
        indices_by_scraper[scraper.name] = portal_offers.sample(frac=1).index.tolist()

    # Interleave the portals for balanced extraction
    mixed_extraction_plan = []
    for i in range(max((len(ix) for ix in indices_by_scraper.values()), default=0)):
        for scraper in scrapers:
            if i < len(indices_by_scraper[scraper.name]):
                mixed_extraction_plan.append((scraper, indices_by_scraper[scraper.name][i]))

    # Apply max extractions limit if set
    max_extractions = int(os.environ.get('DEBUG_MAX_EXTRACTIONS', '')) if os.environ.get('DEBUG_MAX_EXTRACTIONS') else None
    if max_extractions and len(mixed_extraction_plan) > max_extractions:
        logger.info(f"🛑 Limiting extractions from {len(mixed_extraction_plan)} to {max_extractions}")
        mixed_extraction_plan = mixed_extraction_plan[:max_extractions]

    counts = " + ".join(f"{len(ix)} {name}" for name, ix in indices_by_scraper.items())
    logger.info(f"🔀 Mixed extraction plan: {counts} = {len(mixed_extraction_plan)} total")

    # One browser tab per portal, recreated if the site closes it
    pages = {scraper.name: context.new_page() for scraper in scrapers}

    def page_for(scraper: PortalScraper):
        """The portal's tab, recreated if it was closed."""
        try:
            pages[scraper.name].url
        except Exception:
            logger.warning(f"⚠️ {scraper.name} page was closed, recreating...")
            pages[scraper.name] = context.new_page()
        return pages[scraper.name]

    # Batch processing configuration
    BATCH_SIZE = 25
    BATCH_BREAK = 20  # Break between batches (seconds)
    MAX_RETRIES = 5
    SHEETS_UPDATE_INTERVAL = 5  # Update sheets after every N batches (1 = every batch)

    processed_count = 0
    failed_extractions = []
    batch_count = 0

    # Initialize progress tracker
    progress = ProgressTracker(len(mixed_extraction_plan), "Detailed Extraction")
    progress.start()

    for batch_start in range(0, len(mixed_extraction_plan), BATCH_SIZE):
        batch_end = min(batch_start + BATCH_SIZE, len(mixed_extraction_plan))
        current_batch = mixed_extraction_plan[batch_start:batch_end]
        batch_count += 1

        logger.info(
            f"🔄 Processing batch {batch_count}: items {batch_start + 1}-{batch_end} of {len(mixed_extraction_plan)}")

        batch_success_count = 0
        batch_failed_count = 0

        for scraper, index in current_batch:
            item_start_time = time.time()

            success = scraper.extract_details(page_for(scraper), offers_df, index, processed_count + 1,
                                              len(mixed_extraction_plan))

            if success:
                batch_success_count += 1
            else:
                batch_failed_count += 1
                failed_extractions.append((scraper, index))
                logger.warning(f"⚠️ Failed to extract {scraper.name} offer {index}, will retry later")

            processed_count += 1

            # Update progress with timing info
            progress.update(item_start_time)

            delay = random.uniform(0.2, 3.0)
            logger.info(f"⏳ Waiting {delay:.1f}s before next extraction...")
            time.sleep(delay)

        logger.info(f"📊 Batch {batch_count} completed: {batch_success_count} successful, {batch_failed_count} failed")

        # UPDATE SHEETS AFTER EACH BATCH
        if batch_count % SHEETS_UPDATE_INTERVAL == 0:
            try:
                logger.info(f"📊 Updating Google Sheets after batch {batch_count}...")

                # Process data before saving to sheets
                try:
                    add_total_costs(offers_df)

                    logger.info(f"✅ Batch {batch_count} data processing completed successfully")

                except Exception as e:
                    logger.error(f"❌ Failed to process batch {batch_count} data: {e}")

                save_full_data_to_sheets()
                logger.info(f"✅ Google Sheets updated successfully after batch {batch_count}")

                # Optional: Add a small delay after sheets update to avoid rate limiting
                time.sleep(2)

            except Exception as e:
                logger.error(f"❌ Failed to update Google Sheets after batch {batch_count}: {str(e)}")
                # Continue processing even if sheets update fails

        # Longer break between batches
        if batch_end < len(mixed_extraction_plan):
            logger.info(f"🛌 Batch {batch_count} complete. Taking {BATCH_BREAK}s break to avoid rate limiting...")
            time.sleep(BATCH_BREAK)

    # Final sheets update after all batches
    logger.info("📊 Performing final Google Sheets update...")
    try:
        # Process data before final save
        try:
            add_total_costs(offers_df)

            logger.info(f"✅ Batch {batch_count} data processing completed successfully")

        except Exception as e:
            logger.error(f"❌ Failed to process batch {batch_count} data: {e}")

        save_full_data_to_sheets()
        logger.info("✅ Final Google Sheets update completed successfully")
    except Exception as e:
        logger.error(f"❌ Failed final Google Sheets update: {str(e)}")


    retry_success_count = 0
    # Retry failed extractions
    if failed_extractions:
        logger.info(f"🔄 Retrying {len(failed_extractions)} failed extractions...")
        retry_count = 0

        for scraper, index in failed_extractions[:MAX_RETRIES * 5]:  # Limit retries
            retry_count += 1
            logger.info(f"🔄 Retry {retry_count}: {scraper.name} offer {index}")

            success = scraper.extract_details(page_for(scraper), offers_df, index, retry_count,
                                              len(failed_extractions), is_retry=True)

            if success:
                retry_success_count += 1
                logger.info(f"✅ Retry successful for {scraper.name} offer {index}")

            time.sleep(random.uniform(5.0, 10.0))

        # Update sheets after retries if any were successful
        if retry_success_count > 0:
            logger.info(f"📊 Updating Google Sheets after retries ({retry_success_count} successful)...")
            try:
                # Process data before post-retry save
                try:
                    add_total_costs(offers_df)

                    logger.info(f"✅ Batch {batch_count} data processing completed successfully")

                except Exception as e:
                    logger.error(f"❌ Failed to process batch {batch_count} data: {e}")

                save_full_data_to_sheets()
                logger.info("✅ Post-retry Google Sheets update completed successfully")
            except Exception as e:
                logger.error(f"❌ Failed post-retry Google Sheets update: {str(e)}")

        logger.info(f"🔄 Retry phase completed: {retry_success_count}/{retry_count} successful")

    # Finish progress tracking
    progress.finish()

    # Log final statistics
    total_successful = processed_count - len(failed_extractions) + retry_success_count
    logger.info(f"🏁 Extraction phase completed: {total_successful}/{len(mixed_extraction_plan)} offers successfully processed")


def run(playwright: Playwright) -> None:
    """Scrape one search profile: listings from every portal, then every offer's details."""
    _load_run_config()
    browser, context = _launch_browser(playwright)
    max_pages = int(os.environ.get('DEBUG_MAX_PAGES', '')) if os.environ.get('DEBUG_MAX_PAGES') else None

    scrapers = make_scrapers()
    for scraper in scrapers:
        _add_offers(scraper.collect_listings(context, _run_config[f"{scraper.name}_url"], max_pages))

    _finish_listing_phase()

    logger.info("🕰️ Waiting 3 seconds before starting detailed extraction...")
    time.sleep(3)

    _extract_all_details(context, scrapers)


def _load_offers_pickle() -> pd.DataFrame:
    """--pickle-file, or the newest apartments*.pkl in the working directory."""
    if args.pickle_file:
        pickle_path = Path(args.pickle_file)
        if pickle_path.exists():
            logger.info(f"📂 Loading pickle file: {args.pickle_file}")
            df = pd.read_pickle(args.pickle_file)
            logger.info(f"✅ Loaded {len(df)} offers from pickle")
        else:
            logger.error(f"❌ Pickle file not found: {args.pickle_file}")
            sys.exit(1)
    else:
        # Try to find most recent pickle file
        pickle_files = sorted(Path(".").glob("apartments*.pkl"), key=lambda x: x.stat().st_mtime, reverse=True)
        if pickle_files:
            logger.info(f"📂 Auto-loading most recent pickle: {pickle_files[0]}")
            df = pd.read_pickle(pickle_files[0])
            logger.info(f"✅ Loaded {len(df)} offers")
        else:
            logger.error("❌ No pickle file found. Specify one with --pickle-file")
            sys.exit(1)
    return df


def _run_reextract_mode() -> None:
    """--reextract: rebuild offer details from saved HTML instead of scraping again."""
    global offers_df
    logger.info("="*60)
    logger.info("🔄 RUNNING IN RE-EXTRACTION MODE")
    logger.info("="*60)
    offers_df = _load_offers_pickle()

    # Determine HTML folder
    html_folder = args.html_folder
    if html_folder is None:
        today = dt.datetime.now().strftime("%Y-%m-%d")
        html_folder = f"exports/html/{today}"

    # Run re-extraction
    offers_df = reextract_from_saved_html(
        html_folder=html_folder,
        offers_df_input=offers_df,
        force_all=args.force_all
    )

    # Calculate distances for re-extracted offers
    logger.info("="*60)
    logger.info("📏 CALCULATING DISTANCES TO OFFICE")
    logger.info("="*60)

    office_addr = args.office_address if args.office_address else origin
    transport = args.transport_mode or "foot-walking"

    offers_df = calculate_distances_for_offers(
        offers_df,
        office_address=office_addr,
        transport_mode=transport,
        city=search_city
    )

    logger.info("✅ Distance calculation complete!")

    # Merge portal-specific columns and reorder before saving
    offers_df = merge_portal_columns(offers_df)
    offers_df = reorder_dataframe_columns(offers_df)

    # Save updated pickle
    output_pickle = f'apartments_reextracted_{dt.datetime.now().strftime("%d.%m.%Y_%H%M%S")}.pkl'
    offers_df.to_pickle(output_pickle)
    logger.info(f"💾 Saved re-extracted data to: {output_pickle}")

    # Also save to CSV for easy viewing
    output_csv = output_pickle.replace('.pkl', '.csv')
    offers_df.to_csv(output_csv, index=False)
    logger.info(f"💾 Saved CSV to: {output_csv}")

    logger.info("✅ Re-extraction complete!")


def _run_distance_mode() -> None:
    """--calc-distance: add commute distances to a saved pickle."""
    global offers_df
    logger.info("="*60)
    logger.info("📏 STANDALONE DISTANCE CALCULATION MODE")
    logger.info("="*60)
    offers_df = _load_offers_pickle()

    # Calculate distances
    office_addr = args.office_address if args.office_address else origin
    transport = args.transport_mode or "foot-walking"

    offers_df = calculate_distances_for_offers(
        offers_df,
        office_address=office_addr,
        transport_mode=transport,
        city=search_city
    )

    logger.info("✅ Distance calculation complete!")

    # Extract custom fields from Description (descriptions)
    logger.info("🔍 Extracting custom fields from descriptions...")
    offers_df = apply_custom_fields_extraction(offers_df)

    # Merge portal-specific columns and reorder before saving
    offers_df = merge_portal_columns(offers_df)
    offers_df = reorder_dataframe_columns(offers_df)

    # Save updated pickle
    output_pickle = f'apartments_distances_{dt.datetime.now().strftime("%d.%m.%Y_%H%M%S")}.pkl'
    offers_df.to_pickle(output_pickle)
    logger.info(f"💾 Saved data with distances to: {output_pickle}")

    # Also save to CSV for easy viewing
    output_csv = output_pickle.replace('.pkl', '.csv')
    offers_df.to_csv(output_csv, index=False)
    logger.info(f"💾 Saved CSV to: {output_csv}")



def _enrich_offers() -> None:
    """Commute distances, cleanup, fields from the description, column order, LLM scores."""
    global offers_df

    # Calculate distances from apartments to office
    logger.info("="*60)
    logger.info("📏 CALCULATING DISTANCES TO OFFICE")
    logger.info("="*60)

    office_addr = args.office_address if args.office_address else _run_config.get("reference_address", origin)
    transport = args.transport_mode or _run_config.get("transport_mode", "foot-walking")

    offers_df = calculate_distances_for_offers(
        offers_df,
        office_address=office_addr,
        transport_mode=transport,
        city=search_city
    )

    logger.info("✅ Distance calculation complete!")
    logger.info(f"📍 Reference address used: {office_addr}")

    offers_df = offers_df.replace({np.nan: ""})
    logger.debug(f"Columns: {list(offers_df.columns)}")
    for col in offers_df.columns:
        offers_df[col] = offers_df[col].apply(lambda x: ", ".join(x) if isinstance(x, list) else x)

    offers_df = offers_df.fillna("")
    offers_df.to_pickle(f'apartments_backup_{dt.datetime.now().strftime("%d.%m.%Y")}.pkl')

    # Extract custom fields from Description (descriptions)
    logger.info("="*60)
    logger.info("🔍 EXTRACTING CUSTOM FIELDS FROM DESCRIPTIONS")
    logger.info("="*60)
    offers_df = apply_custom_fields_extraction(offers_df)

    offers_df = add_dishwasher_column(offers_df)

    # Merge portal-specific columns and reorder before saving
    offers_df = merge_portal_columns(offers_df)
    offers_df = reorder_dataframe_columns(offers_df)

    # LLM scoring - score listings against user preferences
    llm_prefs = _run_config.get("llm_preferences", "")
    _has_llm_backend = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("GROQ_API_KEY")
    if llm_prefs and _has_llm_backend:
        logger.info("="*60)
        logger.info("🤖 LLM SCORING LISTINGS")
        logger.info("="*60)
        from llm_scorer import score_listings_df
        cached_llm = {}
        try:
            json_creds = GOOGLE_CREDS_PATH
            scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
            _creds = ServiceAccountCredentials.from_json_keyfile_name(str(json_creds), scope)
            _client = gspread.authorize(_creds)
            _ws_cache = _client.open_by_key(sheet_id).worksheet(listings_worksheet_name())
            cached_llm = read_existing_llm_scores_map(_ws_cache)
            logger.info(f"🤖 Loaded {len(cached_llm)} cached LLM scores from Sheets")
        except Exception as _llm_cache_err:
            logger.debug(f"Could not load LLM score cache from Sheets: {_llm_cache_err}")
        offers_df = score_listings_df(
            offers_df, llm_prefs, cached_llm,
            groq_model=_run_config.get("groq_model"),
            language=_run_config.get("language", "en"),
        )

    # Ensure Status column exists
    if "Status" not in offers_df.columns:
        offers_df.insert(0, "Status", "New")
    else:
        offers_df["Status"] = offers_df["Status"].replace("", "New").fillna("New")

    offers_df.to_pickle(f'apartments_{dt.datetime.now().strftime("%d.%m.%Y")}.pkl')
    logger.info(f"Saved {len(offers_df)} offers to pickle")


def _deliver_offers() -> None:
    """Write the listings tab, then Telegram and email digest (skipped with DEBUG_MODE)."""
    global offers_df

    if 'Unnamed: 0' in offers_df.columns:
        offers_df = offers_df.drop(['Unnamed: 0'], axis=1)

    offers_df = offers_df.fillna("")
    header_language = _run_config.get("language", "en")
    offers_list = [translate_header_row(offers_df.columns.to_list(), header_language)] + offers_df.values.tolist()

    # Check if running in debug mode - skip sheets update if so
    debug_mode = os.environ.get('DEBUG_MODE', '').lower() in ('true', '1', 'yes')
    if debug_mode:
        logger.warning("🛑 DEBUG MODE: Skipping Google Sheets update (data preserved)")
    else:
        existing_status = {}
        try:
            # Use absolute path for credentials file
            json_creds = GOOGLE_CREDS_PATH
            scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
            creds = ServiceAccountCredentials.from_json_keyfile_name(str(json_creds), scope)
            client = gspread.authorize(creds)

            spreadsheet = client.open_by_key(sheet_id)
            ws = open_listings_worksheet(spreadsheet)

            # Read existing statuses before clearing
            existing_status = read_existing_status_map(ws)

            logger.info('Clearing sheet...')
            ws.batch_clear(["A:BJ"])
            ws.update(values=offers_list, range_name='B1')
            logger.info('Updating photo preview formulas...')
            last_row = len(offers_df)
            formula = get_image_formula(offers_df, last_row)
            if formula:
                ws.update(values=[[formula]], range_name='A2:A2', raw=False)

            # Restore statuses for known offers
            restore_status_column(ws, existing_status)

            logger.info('Sheet updated successfully!')
        except Exception as e:
            logger.error(f"❌ Failed to update Google Sheet: {e}")

        # Update "Last scraped" + "Profile" in config sheet
        _sp = _run_config.get("_spreadsheet")
        if _sp is None:
            try:
                _sp = _open_spreadsheet(sheet_id)
            except Exception:
                pass
        if _sp:
            update_config_last_scraped(_sp, search)

        # Telegram notification
        bot_token = _run_config.get("telegram_bot_token", "")
        chat_id   = _run_config.get("telegram_chat_id", "")
        if bot_token and chat_id:
            new_count = len(offers_df)
            cheapest_price, cheapest_link = "", ""
            try:
                tmp = offers_df[offers_df["Full_value"].astype(str).str.strip() != ""]
                if not tmp.empty:
                    idx_min = pd.to_numeric(tmp["Full_value"], errors="coerce").idxmin()
                    cheapest_price = tmp.at[idx_min, "Full_value"]
                    cheapest_link  = tmp.at[idx_min, "Link"]
            except Exception:
                pass
            sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
            new_listings = len([s for s in existing_status.values()]) if existing_status else 0
            brand_new = new_count - new_listings
            if _run_config.get("language", "en") == "pl":
                msg = (
                    f"<b>Scraper: {search}</b>\n"
                    f"Nowe oferty: {brand_new} | Łącznie: {new_count}\n"
                    f"Najtańsza: {cheapest_price} zł - {cheapest_link}\n"
                    f'<a href="{sheet_url}">Otwórz arkusz</a>'
                )
            else:
                msg = (
                    f"<b>Scraper: {search}</b>\n"
                    f"New offers: {brand_new} | Total: {new_count}\n"
                    f"Cheapest: {cheapest_price} zł - {cheapest_link}\n"
                    f'<a href="{sheet_url}">Open spreadsheet</a>'
                )
            send_telegram_notification(bot_token, chat_id, msg)

        # Email digest
        try:
            from email_report import send_email_report
            today_str = dt.datetime.now().strftime("%d.%m.%Y")
            send_email_report(offers_df, _run_config, search, sheet_id, today_str)
        except Exception as _email_err:
            logger.warning(f"📧 Email report failed: {_email_err}")


def main() -> None:
    global logger, debug_handler
    _profiles = load_profiles()
    _cli_args = build_arg_parser(list(_profiles)).parse_args()
    logger, debug_handler = setup_comprehensive_logging(include_html_debugging=True)
    logger.info("🚀 Starting Web Scraper Session")
    configure(_cli_args, _profiles[_cli_args.search])

    if args.reextract:
        _run_reextract_mode()
        return
    if args.calc_distance:
        _run_distance_mode()
        return

    with sync_playwright() as playwright:
        run(playwright)
    _enrich_offers()
    _deliver_offers()


if __name__ == "__main__":
    main()
