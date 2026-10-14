"""Pipeline stages for one scraper run: collect listings, extract details, enrich, deliver.

Each stage takes the RunContext built by apartment_scraper.main() - the profile, CLI
options, config-sheet values and the offers DataFrame travel in it instead of in
module-level globals.
"""
import argparse
import datetime as dt
import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List

import gspread
import numpy as np
import pandas as pd
from bs4 import BeautifulSoup
from oauth2client.service_account import ServiceAccountCredentials
from playwright.sync_api import Playwright

from geo import calculate_distances_for_offers
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

logger = logging.getLogger("logging_config")


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



LISTINGS_WORKSHEET_DEFAULT = "apartment list"


@dataclass
class RunContext:
    """Everything one run of a search profile needs, passed explicitly from stage to stage."""

    args: argparse.Namespace
    profile: Dict[str, Any]
    debug_handler: Any = None
    # Config tab values (profiles.json fills the blanks) - filled by load_run_config()
    run_config: Dict[str, Any] = field(default_factory=dict)
    offers_df: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def search(self) -> str:
        return self.profile["name"]

    @property
    def sheet_id(self) -> str:
        return self.profile["sheet_id"]

    @property
    def origin(self) -> str:
        return self.profile["origin_address"]

    @property
    def city(self) -> str:
        return self.profile["city"]

    @property
    def worksheet_name(self) -> str:
        """Tab the listings are written to - profile key "worksheet", default "apartment list"."""
        return (self.profile.get("worksheet") or "").strip() or LISTINGS_WORKSHEET_DEFAULT

    def add_offers(self, rows: List[Dict[str, Any]]) -> int:
        """Append offers parsed from search result pages to offers_df."""
        if rows:
            self.offers_df = pd.concat([self.offers_df, pd.DataFrame(rows)], ignore_index=True)
        return len(rows)


def open_listings_worksheet(spreadsheet, name: str):
    """Open the listings tab, creating it if the spreadsheet does not have it yet."""
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


def log_scraping_stats(ctx: RunContext) -> None:
    """Log comprehensive scraping statistics with safe column checks"""

    if ctx.offers_df.empty:
        logger.warning("📊 No data to analyze - ctx.offers_df is empty")
        return

    try:
        # Total offers
        total_offers = len(ctx.offers_df)

        # Safe OLX / Otodom counts
        olx_offers = ctx.offers_df["Link"].str.contains("olx.pl", na=False).sum() if "Link" in ctx.offers_df.columns else 0
        otodom_offers = ctx.offers_df["Link"].str.contains("otodom.pl", na=False).sum() if "Link" in ctx.offers_df.columns else 0

        # --- PRICE STATISTICS ---
        if "Base_value" in ctx.offers_df.columns:
            # Ensure numeric conversion
            valid_prices = pd.to_numeric(ctx.offers_df["Base_value"], errors="coerce").dropna()
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
        if "Address" in ctx.offers_df.columns:
            missing_data["no_address"] = ctx.offers_df["Address"].isin(["", "Unknown", "Unknown location"]).sum()
        else:
            missing_data["no_address"] = "Address column missing"

        # Check missing area
        if "Area" in ctx.offers_df.columns:
            missing_data["no_area"] = ctx.offers_df["Area"].isin(["", 0]).sum()
        else:
            missing_data["no_area"] = "Area column missing"

        # Check scraping errors safely
        missing_data["scraping_errors"] = (
            ctx.offers_df["Scraping_Error"].ne("").sum()
            if "Scraping_Error" in ctx.offers_df.columns
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


def save_basic_data_to_sheets(ctx: RunContext) -> None:
    """Save basic scraped data to sheets before extracting details"""
    
    if ctx.offers_df.empty:
        logger.warning("⚠️ No data to save - ctx.offers_df is empty")
        return
    
    try:
        logger.info(f"💾 Saving basic data ({len(ctx.offers_df)} offers) to sheets before detail extraction...")
        
        # Validate data before saving
        valid_offers = 0
        invalid_offers = 0
        
        for idx, row in ctx.offers_df.iterrows():
            is_valid, issues = validate_offer_data(row.to_dict(), "Basic")
            if not is_valid:
                invalid_offers += 1
                logger.warning(f"⚠️ Invalid offer at index {idx}: {'; '.join(issues[:2])}...")  # Log first 2 issues
            else:
                valid_offers += 1
        
        logger.info(f"📊 Data validation: {valid_offers} valid, {invalid_offers} invalid offers")
        
        # Clean and prepare data for sheets
        basic_df = ctx.offers_df.copy()
        basic_df = basic_df.replace({np.nan: ""})
        
        # Remove any existing detailed columns to keep only basic data
        basic_columns = ['Name', 'Base_value', 'Additional_value', 'Full_value', 'Area', 'Address', 'Link']
        available_columns = [col for col in basic_columns if col in basic_df.columns]
        basic_df = basic_df[available_columns]
        
        # Convert to list format for sheets
        header_language = ctx.run_config.get("language", "en")
        basic_data_list = [translate_header_row(basic_df.columns.to_list(), header_language)] + basic_df.values.tolist()
        
        # Connect to Google Sheets
        json_creds = GOOGLE_CREDS_PATH
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_name(json_creds, scope)
        client = gspread.authorize(creds)
        
        spreadsheet = client.open_by_key(ctx.sheet_id)
        ws = open_listings_worksheet(spreadsheet, ctx.worksheet_name)
        
        # Save basic data
        logger.info("🧹 Clearing existing data...")
        ws.batch_clear(["B:BJ"])  # Clear basic range only
        last_row = len(ctx.offers_df)
        formulas = [f"""=ARRAYFORMULA(IF($AZ2:$AZ{last_row}="", IMAGE($X2:$X{last_row}), IMAGE($AZ2:$AZ{last_row})))"""]
        ws.update(values=[formulas], range_name='A2:A2', raw=False)
        logger.info("📝 Uploading basic offer data...")
        ws.update(values=basic_data_list, range_name='B1')
        
        logger.info(f"✅ Successfully saved {len(ctx.offers_df)} basic offers to sheets")
        
    except Exception as e:
        logger.error(f"❌ Failed to save basic data to sheets: {e}")
        # Don't stop execution, just log the error


def save_full_data_to_sheets(ctx: RunContext) -> None:
    """Save all available OLX scraped data (basic + details) to Google Sheets"""

    if ctx.offers_df.empty:
        logger.warning("⚠️ No data to save - ctx.offers_df is empty")
        return

    try:
        logger.info(f"💾 Saving FULL scraped data ({len(ctx.offers_df)} offers) to sheets...")

        # Validate data before saving
        valid_offers = 0
        invalid_offers = 0

        for idx, row in ctx.offers_df.iterrows():
            is_valid, issues = validate_offer_data(row.to_dict(), "Full")
            if not is_valid:
                invalid_offers += 1
                logger.warning(f"⚠️ Invalid offer at index {idx}: {'; '.join(issues[:2])}...")
            else:
                valid_offers += 1

        logger.info(f"📊 Data validation: {valid_offers} valid, {invalid_offers} invalid offers")

        # Clean dataframe before saving
        full_df = ctx.offers_df.copy()
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
        header_language = ctx.run_config.get("language", "en")
        full_data_list = [translate_header_row(full_df.columns.to_list(), header_language)] + full_df.values.tolist()

        # Connect to Google Sheets
        json_creds = GOOGLE_CREDS_PATH
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_name(json_creds, scope)
        client = gspread.authorize(creds)

        spreadsheet = client.open_by_key(ctx.sheet_id)
        ws = open_listings_worksheet(spreadsheet, ctx.worksheet_name)

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


def reextract_from_saved_html(offers_df: pd.DataFrame, html_folder: str = None,
                              force_all: bool = False) -> pd.DataFrame:
    """
    Re-extract data from saved HTML files for offers that failed extraction.
    This is useful when 404 detection was too aggressive or extraction failed.

    Args:
        offers_df: DataFrame to update (not modified - a copy is returned).
        html_folder: Path to folder containing saved HTML files. If None, uses today's folder.
        force_all: If True, re-extract all offers even if they already have data.

    Returns:
        Updated DataFrame with re-extracted data
    """
    df = offers_df.copy()

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
    for col, count in sorted(fields_updated.items(), key=lambda x: -x[1])[:20]:
        logger.info(f"   {col}: {count} times")
    logger.info(f"{'='*60}\n")

    return df


def make_scrapers(debug_handler=None) -> List[PortalScraper]:
    """The portals this run scrapes, in listing-collection order."""
    return [OlxScraper(debug_handler=debug_handler), OtodomScraper()]


def load_run_config(ctx: RunContext) -> None:
    """Load the profile's config tab (falls back to profiles.json) into ctx.run_config."""

    # Load per-profile config from Google Sheets (creates sheet if missing)
    _spreadsheet = None
    try:
        _spreadsheet = _open_spreadsheet(ctx.sheet_id)
        ctx.run_config = load_config_sheet(
            _spreadsheet, ctx.search,
            default_olx_url=ctx.profile["olx_url"],
            default_otodom_url=ctx.profile["otodom_url"],
            default_origin=ctx.origin,
        )
    except Exception as e:
        logger.warning(f"⚠️ Could not load config sheet, using built-in defaults: {e}")
        ctx.run_config = {"reference_address": ctx.origin,
                          "olx_url": ctx.profile["olx_url"], "otodom_url": ctx.profile["otodom_url"]}

    ctx.run_config["_spreadsheet"] = _spreadsheet  # stash for later use

    # Merge email + LLM fields from profiles.json (used when config sheet fields are blank)
    for _email_key in ("email_sender", "email_recipient", "email_app_password", "email_districts", "email_top_n",
                       "llm_preferences", "groq_model"):
        if not ctx.run_config.get(_email_key):
            ctx.run_config[_email_key] = ctx.profile.get(_email_key, "")

    # Language for Sheet headers / email digest / Telegram messages (config sheet wins)
    if not ctx.run_config.get("language"):
        ctx.run_config["language"] = ctx.profile.get("language", "en")


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



def _finish_listing_phase(ctx: RunContext) -> None:
    """Dedup, log stats, compute total costs and save the basic rows before detail extraction."""

    logger.info(f"Scraping complete. Total offers: {len(ctx.offers_df)}")
    ctx.offers_df.to_csv(f'all_offers_df_{ctx.search}.csv')
    ctx.offers_df = ctx.offers_df.drop_duplicates(subset=['Link']).reset_index(drop=True)
    logger.info(f"After dedup: {len(ctx.offers_df)} offers")

    # Generate and log comprehensive statistics
    log_scraping_stats(ctx)

    # SAVE BASIC DATA TO SHEETS FIRST (before detailed extraction)
    logger.info("💾 Saving basic offer data to sheets before extracting details...")

    try:
        add_total_costs(ctx.offers_df)

        logger.info("✅ Data processing completed successfully")

    except Exception as e:
        logger.error(f"❌ Failed to process data: {e}")

    ctx.offers_df = ctx.offers_df.sort_values(by='Full_value', ascending=True).reset_index(drop=True)
    save_basic_data_to_sheets(ctx)


def _extract_all_details(ctx: RunContext, context, scrapers: List[PortalScraper]) -> None:
    """Open every offer page (portals interleaved, in batches) and fill in details."""
    # PART 3 - Scrape all offers (DETAILED EXTRACTION PHASE)
    logger.info("🔍 Starting mixed detailed extraction phase...")

    # Randomize order within each portal to distribute server load and avoid patterns
    indices_by_scraper = {}
    for scraper in scrapers:
        portal_offers = ctx.offers_df[ctx.offers_df["Link"].str.contains(scraper.domain, na=False, regex=False)]
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

            success = scraper.extract_details(page_for(scraper), ctx.offers_df, index, processed_count + 1,
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
                    add_total_costs(ctx.offers_df)

                    logger.info(f"✅ Batch {batch_count} data processing completed successfully")

                except Exception as e:
                    logger.error(f"❌ Failed to process batch {batch_count} data: {e}")

                save_full_data_to_sheets(ctx)
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
            add_total_costs(ctx.offers_df)

            logger.info(f"✅ Batch {batch_count} data processing completed successfully")

        except Exception as e:
            logger.error(f"❌ Failed to process batch {batch_count} data: {e}")

        save_full_data_to_sheets(ctx)
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

            success = scraper.extract_details(page_for(scraper), ctx.offers_df, index, retry_count,
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
                    add_total_costs(ctx.offers_df)

                    logger.info(f"✅ Batch {batch_count} data processing completed successfully")

                except Exception as e:
                    logger.error(f"❌ Failed to process batch {batch_count} data: {e}")

                save_full_data_to_sheets(ctx)
                logger.info("✅ Post-retry Google Sheets update completed successfully")
            except Exception as e:
                logger.error(f"❌ Failed post-retry Google Sheets update: {str(e)}")

        logger.info(f"🔄 Retry phase completed: {retry_success_count}/{retry_count} successful")

    # Finish progress tracking
    progress.finish()

    # Log final statistics
    total_successful = processed_count - len(failed_extractions) + retry_success_count
    logger.info(f"🏁 Extraction phase completed: {total_successful}/{len(mixed_extraction_plan)} offers successfully processed")


def scrape(ctx: RunContext, playwright: Playwright) -> None:
    """Scrape one search profile: listings from every portal, then every offer's details."""
    load_run_config(ctx)
    browser, context = _launch_browser(playwright)
    max_pages = int(os.environ.get('DEBUG_MAX_PAGES', '')) if os.environ.get('DEBUG_MAX_PAGES') else None

    scrapers = make_scrapers(ctx.debug_handler)
    for scraper in scrapers:
        ctx.add_offers(scraper.collect_listings(context, ctx.run_config[f"{scraper.name}_url"], max_pages))

    _finish_listing_phase(ctx)

    logger.info("🕰️ Waiting 3 seconds before starting detailed extraction...")
    time.sleep(3)

    _extract_all_details(ctx, context, scrapers)


def _load_offers_pickle(ctx: RunContext) -> pd.DataFrame:
    """--pickle-file, or the newest apartments*.pkl in the working directory."""
    if ctx.args.pickle_file:
        pickle_path = Path(ctx.args.pickle_file)
        if pickle_path.exists():
            logger.info(f"📂 Loading pickle file: {ctx.args.pickle_file}")
            df = pd.read_pickle(ctx.args.pickle_file)
            logger.info(f"✅ Loaded {len(df)} offers from pickle")
        else:
            logger.error(f"❌ Pickle file not found: {ctx.args.pickle_file}")
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


def run_reextract_mode(ctx: RunContext) -> None:
    """--reextract: rebuild offer details from saved HTML instead of scraping again."""
    logger.info("="*60)
    logger.info("🔄 RUNNING IN RE-EXTRACTION MODE")
    logger.info("="*60)
    ctx.offers_df = _load_offers_pickle(ctx)

    # Determine HTML folder
    html_folder = ctx.args.html_folder
    if html_folder is None:
        today = dt.datetime.now().strftime("%Y-%m-%d")
        html_folder = f"exports/html/{today}"

    # Run re-extraction
    ctx.offers_df = reextract_from_saved_html(
        ctx.offers_df,
        html_folder=html_folder,
        force_all=ctx.args.force_all
    )

    # Calculate distances for re-extracted offers
    logger.info("="*60)
    logger.info("📏 CALCULATING DISTANCES TO OFFICE")
    logger.info("="*60)

    office_addr = ctx.args.office_address if ctx.args.office_address else ctx.origin
    transport = ctx.args.transport_mode or "foot-walking"

    ctx.offers_df = calculate_distances_for_offers(
        ctx.offers_df,
        office_address=office_addr,
        transport_mode=transport,
        city=ctx.city
    )

    logger.info("✅ Distance calculation complete!")

    # Merge portal-specific columns and reorder before saving
    ctx.offers_df = merge_portal_columns(ctx.offers_df)
    ctx.offers_df = reorder_dataframe_columns(ctx.offers_df)

    # Save updated pickle
    output_pickle = f'apartments_reextracted_{dt.datetime.now().strftime("%d.%m.%Y_%H%M%S")}.pkl'
    ctx.offers_df.to_pickle(output_pickle)
    logger.info(f"💾 Saved re-extracted data to: {output_pickle}")

    # Also save to CSV for easy viewing
    output_csv = output_pickle.replace('.pkl', '.csv')
    ctx.offers_df.to_csv(output_csv, index=False)
    logger.info(f"💾 Saved CSV to: {output_csv}")

    logger.info("✅ Re-extraction complete!")


def run_distance_mode(ctx: RunContext) -> None:
    """--calc-distance: add commute distances to a saved pickle."""
    logger.info("="*60)
    logger.info("📏 STANDALONE DISTANCE CALCULATION MODE")
    logger.info("="*60)
    ctx.offers_df = _load_offers_pickle(ctx)

    # Calculate distances
    office_addr = ctx.args.office_address if ctx.args.office_address else ctx.origin
    transport = ctx.args.transport_mode or "foot-walking"

    ctx.offers_df = calculate_distances_for_offers(
        ctx.offers_df,
        office_address=office_addr,
        transport_mode=transport,
        city=ctx.city
    )

    logger.info("✅ Distance calculation complete!")

    # Extract custom fields from Description (descriptions)
    logger.info("🔍 Extracting custom fields from descriptions...")
    ctx.offers_df = apply_custom_fields_extraction(ctx.offers_df)

    # Merge portal-specific columns and reorder before saving
    ctx.offers_df = merge_portal_columns(ctx.offers_df)
    ctx.offers_df = reorder_dataframe_columns(ctx.offers_df)

    # Save updated pickle
    output_pickle = f'apartments_distances_{dt.datetime.now().strftime("%d.%m.%Y_%H%M%S")}.pkl'
    ctx.offers_df.to_pickle(output_pickle)
    logger.info(f"💾 Saved data with distances to: {output_pickle}")

    # Also save to CSV for easy viewing
    output_csv = output_pickle.replace('.pkl', '.csv')
    ctx.offers_df.to_csv(output_csv, index=False)
    logger.info(f"💾 Saved CSV to: {output_csv}")



def enrich_offers(ctx: RunContext) -> None:
    """Commute distances, cleanup, fields from the description, column order, LLM scores."""

    # Calculate distances from apartments to office
    logger.info("="*60)
    logger.info("📏 CALCULATING DISTANCES TO OFFICE")
    logger.info("="*60)

    office_addr = ctx.args.office_address if ctx.args.office_address else ctx.run_config.get("reference_address", ctx.origin)
    transport = ctx.args.transport_mode or ctx.run_config.get("transport_mode", "foot-walking")

    ctx.offers_df = calculate_distances_for_offers(
        ctx.offers_df,
        office_address=office_addr,
        transport_mode=transport,
        city=ctx.city
    )

    logger.info("✅ Distance calculation complete!")
    logger.info(f"📍 Reference address used: {office_addr}")

    ctx.offers_df = ctx.offers_df.replace({np.nan: ""})
    logger.debug(f"Columns: {list(ctx.offers_df.columns)}")
    for col in ctx.offers_df.columns:
        ctx.offers_df[col] = ctx.offers_df[col].apply(lambda x: ", ".join(x) if isinstance(x, list) else x)

    ctx.offers_df = ctx.offers_df.fillna("")
    ctx.offers_df.to_pickle(f'apartments_backup_{dt.datetime.now().strftime("%d.%m.%Y")}.pkl')

    # Extract custom fields from Description (descriptions)
    logger.info("="*60)
    logger.info("🔍 EXTRACTING CUSTOM FIELDS FROM DESCRIPTIONS")
    logger.info("="*60)
    ctx.offers_df = apply_custom_fields_extraction(ctx.offers_df)

    ctx.offers_df = add_dishwasher_column(ctx.offers_df)

    # Merge portal-specific columns and reorder before saving
    ctx.offers_df = merge_portal_columns(ctx.offers_df)
    ctx.offers_df = reorder_dataframe_columns(ctx.offers_df)

    # LLM scoring - score listings against user preferences
    llm_prefs = ctx.run_config.get("llm_preferences", "")
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
            _ws_cache = _client.open_by_key(ctx.sheet_id).worksheet(ctx.worksheet_name)
            cached_llm = read_existing_llm_scores_map(_ws_cache)
            logger.info(f"🤖 Loaded {len(cached_llm)} cached LLM scores from Sheets")
        except Exception as _llm_cache_err:
            logger.debug(f"Could not load LLM score cache from Sheets: {_llm_cache_err}")
        ctx.offers_df = score_listings_df(
            ctx.offers_df, llm_prefs, cached_llm,
            groq_model=ctx.run_config.get("groq_model"),
            language=ctx.run_config.get("language", "en"),
        )

    # Ensure Status column exists
    if "Status" not in ctx.offers_df.columns:
        ctx.offers_df.insert(0, "Status", "New")
    else:
        ctx.offers_df["Status"] = ctx.offers_df["Status"].replace("", "New").fillna("New")

    ctx.offers_df.to_pickle(f'apartments_{dt.datetime.now().strftime("%d.%m.%Y")}.pkl')
    logger.info(f"Saved {len(ctx.offers_df)} offers to pickle")


def deliver_offers(ctx: RunContext) -> None:
    """Write the listings tab, then Telegram and email digest (skipped with DEBUG_MODE)."""

    if 'Unnamed: 0' in ctx.offers_df.columns:
        ctx.offers_df = ctx.offers_df.drop(['Unnamed: 0'], axis=1)

    ctx.offers_df = ctx.offers_df.fillna("")
    header_language = ctx.run_config.get("language", "en")
    offers_list = [translate_header_row(ctx.offers_df.columns.to_list(), header_language)] + ctx.offers_df.values.tolist()

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

            spreadsheet = client.open_by_key(ctx.sheet_id)
            ws = open_listings_worksheet(spreadsheet, ctx.worksheet_name)

            # Read existing statuses before clearing
            existing_status = read_existing_status_map(ws)

            logger.info('Clearing sheet...')
            ws.batch_clear(["A:BJ"])
            ws.update(values=offers_list, range_name='B1')
            logger.info('Updating photo preview formulas...')
            last_row = len(ctx.offers_df)
            formula = get_image_formula(ctx.offers_df, last_row)
            if formula:
                ws.update(values=[[formula]], range_name='A2:A2', raw=False)

            # Restore statuses for known offers
            restore_status_column(ws, existing_status)

            logger.info('Sheet updated successfully!')
        except Exception as e:
            logger.error(f"❌ Failed to update Google Sheet: {e}")

        # Update "Last scraped" + "Profile" in config sheet
        _sp = ctx.run_config.get("_spreadsheet")
        if _sp is None:
            try:
                _sp = _open_spreadsheet(ctx.sheet_id)
            except Exception:
                pass
        if _sp:
            update_config_last_scraped(_sp, ctx.search)

        # Telegram notification
        bot_token = ctx.run_config.get("telegram_bot_token", "")
        chat_id   = ctx.run_config.get("telegram_chat_id", "")
        if bot_token and chat_id:
            new_count = len(ctx.offers_df)
            cheapest_price, cheapest_link = "", ""
            try:
                tmp = ctx.offers_df[ctx.offers_df["Full_value"].astype(str).str.strip() != ""]
                if not tmp.empty:
                    idx_min = pd.to_numeric(tmp["Full_value"], errors="coerce").idxmin()
                    cheapest_price = tmp.at[idx_min, "Full_value"]
                    cheapest_link  = tmp.at[idx_min, "Link"]
            except Exception:
                pass
            sheet_url = f"https://docs.google.com/spreadsheets/d/{ctx.sheet_id}"
            new_listings = len([s for s in existing_status.values()]) if existing_status else 0
            brand_new = new_count - new_listings
            if ctx.run_config.get("language", "en") == "pl":
                msg = (
                    f"<b>Scraper: {ctx.search}</b>\n"
                    f"Nowe oferty: {brand_new} | Łącznie: {new_count}\n"
                    f"Najtańsza: {cheapest_price} zł - {cheapest_link}\n"
                    f'<a href="{sheet_url}">Otwórz arkusz</a>'
                )
            else:
                msg = (
                    f"<b>Scraper: {ctx.search}</b>\n"
                    f"New offers: {brand_new} | Total: {new_count}\n"
                    f"Cheapest: {cheapest_price} zł - {cheapest_link}\n"
                    f'<a href="{sheet_url}">Open spreadsheet</a>'
                )
            send_telegram_notification(bot_token, chat_id, msg)

        # Email digest
        try:
            from email_report import send_email_report
            today_str = dt.datetime.now().strftime("%d.%m.%Y")
            send_email_report(ctx.offers_df, ctx.run_config, ctx.search, ctx.sheet_id, today_str)
        except Exception as _email_err:
            logger.warning(f"📧 Email report failed: {_email_err}")
