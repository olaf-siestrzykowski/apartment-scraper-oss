import argparse
import pandas as pd
import numpy as np
from bs4 import BeautifulSoup
import datetime as dt
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import re
import os
import sys
from playwright.sync_api import Playwright, sync_playwright, expect
import time
import random
import logging
from datetime import timedelta
from pathlib import Path
import json
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urljoin
import requests

# Import enhanced logging configuration
from logging_config import (
    setup_comprehensive_logging,
    log_extraction_attempt,
    log_selector_attempt,
    log_extraction_result,
    log_batch_summary,
    log_session_summary,
    DebugHTMLHandler
)

# Configure enhanced logging with HTML debugging
logger, debug_handler = setup_comprehensive_logging(include_html_debugging=True)
logger.info(f"🚀 Starting Web Scraper Session")


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


# Global progress tracker instance
extraction_progress: Optional[ProgressTracker] = None

def load_profiles() -> dict:
    """Load search profiles from profiles.json next to this script."""
    profiles_path = Path(__file__).parent / "profiles.json"
    if not profiles_path.exists():
        raise FileNotFoundError(
            f"profiles.json not found at {profiles_path}. "
            "Run: python setup.py"
        )
    with profiles_path.open(encoding="utf-8") as f:
        data = json.load(f)
    return {p["name"]: p for p in data["profiles"]}


_ALL_PROFILES = load_profiles()

parser = argparse.ArgumentParser(description="Scrape flats from OLX/Otodom")
parser.add_argument("--search", choices=list(_ALL_PROFILES.keys()),
                    default=list(_ALL_PROFILES.keys())[0],
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
parser.add_argument("--transport-mode", choices=["driving-car", "cycling-regular", "foot-walking", "public-transport"],
                    default=None, help="Transport mode (overrides config sheet; default: foot-walking)")

# Only parse arguments if running as main script, not when imported as a module
if __name__ == "__main__":
    args = parser.parse_args()
    search = args.search
else:
    # Default values when imported as a module
    search = list(_ALL_PROFILES.keys())[0]
    args = argparse.Namespace(reextract=False, html_folder=None, pickle_file=None, force_all=False,
                              calc_distance=False, office_address=None, transport_mode=None)

# ============================================================================
# CONFIGURATION - CREDENTIALS AND API KEYS (Use environment variables!)
# ============================================================================
# OpenRouteService API key for distance calculations
API_KEY = os.environ.get('OPENROUTESERVICE_API_KEY')
if not API_KEY:
    logger.warning("⚠️  OPENROUTESERVICE_API_KEY not set in environment. Distance calculation will fail.")

# Google Sheets credentials path - resolved relative to this script file
GOOGLE_CREDS_PATH = os.environ.get(
    'GOOGLE_CREDS_PATH',
    str(Path(__file__).parent / 'google-credentials.json')
)

# Load active profile from profiles.json
_profile   = _ALL_PROFILES[search]
olx_url    = _profile["olx_url"]
otodom_url = _profile["otodom_url"]
sheet_id   = _profile["sheet_id"]
origin     = _profile["origin_address"]
search_city = _profile["city"]

offers_df = pd.DataFrame()
# Global set to track seen offers and prevent duplicates
seen_offers = set()
# Holds config loaded from the "config" sheet at runtime (populated by run())
_run_config: dict = {}
logger.info(f'Search configuration: {search} (city: {search_city}, office: {origin})')


# ============================================================================
# DISTANCE CALCULATION MODULE
# ============================================================================
# Rate limiting delays
NOMINATIM_DELAY = 1.1  # Nominatim requires 1 req/sec
ORS_DELAY = 0.3

# Known Warsaw street names for validation
KNOWN_WARSAW_STREETS = {
    'puławska', 'marszałkowska', 'jerozolimskie', 'aleje jerozolimskie',
    'świętokrzyska', 'nowy świat', 'krakowskie przedmieście', 'grójecka',
    'modlińska', 'grochowska', 'targowa', 'jagiellońska', 'solidarności',
    'jana pawła', 'wolska', 'górczewska', 'powstańców śląskich',
    'kasprowicza', 'broniewskiego', 'słowackiego', 'mickiewicza',
    'wilsona', 'popiełuszki', 'żeromskiego', 'conrada', 'reymonta',
    'sikorskiego', 'powsińska', 'sobieskiego', 'belwederska',
}


def geocode_nominatim(address: str) -> Optional[Dict[str, Any]]:
    """Geocode address using Nominatim (OSM) - free, no API key needed"""
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": address,
        "format": "json",
        "limit": 1,
        "countrycodes": "pl"
    }
    headers = {"User-Agent": "ApartmentScraper/1.0"}

    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data:
                result = data[0]
                return {
                    "lat": float(result["lat"]),
                    "lon": float(result["lon"]),
                    "label": result.get("display_name", "")[:100]
                }
    except Exception as e:
        logger.debug(f"Nominatim geocode error for '{address}': {e}")
    return None


def geocode_photon(address: str) -> Optional[Dict[str, Any]]:
    """Geocode using Photon (fallback) - free, based on OSM"""
    url = "https://photon.komoot.io/api/"
    params = {"q": address, "limit": 1, "lang": "en"}

    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get("features"):
                feature = data["features"][0]
                coords = feature["geometry"]["coordinates"]
                props = feature["properties"]
                return {
                    "lat": coords[1],
                    "lon": coords[0],
                    "label": f"{props.get('name', '')} {props.get('city', '')}"
                }
    except Exception as e:
        logger.debug(f"Photon geocode error for '{address}': {e}")
    return None


def is_within_city_bounds(coords: Dict[str, Any], city: str = "Warszawa") -> bool:
    """Check if coordinates are within the given city's bounding box"""
    if not coords:
        return False
    bounds = CITY_BOUNDS.get(city, WARSAW_BOUNDS)
    lat = coords.get("lat", 0)
    lon = coords.get("lon", 0)
    return (bounds["lat_min"] <= lat <= bounds["lat_max"] and
            bounds["lon_min"] <= lon <= bounds["lon_max"])


def geocode_address(address: str, city: str = "Warszawa") -> Optional[Dict[str, Any]]:
    """Geocode with fallback: Nominatim -> Photon, with city bounds validation"""
    result = geocode_nominatim(address)
    if result and is_within_city_bounds(result, city):
        return result

    time.sleep(0.5)
    result = geocode_photon(address)
    if result and is_within_city_bounds(result, city):
        return result

    return None


# City bounding boxes (approximate, includes suburbs)
WARSAW_BOUNDS = {
    "lat_min": 52.05,
    "lat_max": 52.45,
    "lon_min": 20.75,
    "lon_max": 21.35
}

LUBLIN_BOUNDS    = {"lat_min": 51.15, "lat_max": 51.35, "lon_min": 22.40, "lon_max": 22.70}
KRAKOW_BOUNDS    = {"lat_min": 49.97, "lat_max": 50.15, "lon_min": 19.79, "lon_max": 20.25}
GDANSK_BOUNDS    = {"lat_min": 54.27, "lat_max": 54.46, "lon_min": 18.46, "lon_max": 18.78}
WROCLAW_BOUNDS   = {"lat_min": 51.05, "lat_max": 51.22, "lon_min": 16.85, "lon_max": 17.18}
POZNAN_BOUNDS    = {"lat_min": 52.30, "lat_max": 52.52, "lon_min": 16.79, "lon_max": 17.08}
LODZ_BOUNDS      = {"lat_min": 51.68, "lat_max": 51.87, "lon_min": 19.33, "lon_max": 19.63}
KATOWICE_BOUNDS  = {"lat_min": 50.19, "lat_max": 50.32, "lon_min": 18.92, "lon_max": 19.12}
SZCZECIN_BOUNDS  = {"lat_min": 53.35, "lat_max": 53.55, "lon_min": 14.45, "lon_max": 14.70}
BYDGOSZCZ_BOUNDS = {"lat_min": 53.08, "lat_max": 53.18, "lon_min": 17.95, "lon_max": 18.10}
GDYNIA_BOUNDS    = {"lat_min": 54.44, "lat_max": 54.58, "lon_min": 18.45, "lon_max": 18.60}

CITY_BOUNDS = {
    "Warszawa":   WARSAW_BOUNDS,
    "Lublin":     LUBLIN_BOUNDS,
    "Kraków":     KRAKOW_BOUNDS,
    "Krakow":     KRAKOW_BOUNDS,
    "Gdańsk":     GDANSK_BOUNDS,
    "Gdansk":     GDANSK_BOUNDS,
    "Wrocław":    WROCLAW_BOUNDS,
    "Wroclaw":    WROCLAW_BOUNDS,
    "Poznań":     POZNAN_BOUNDS,
    "Poznan":     POZNAN_BOUNDS,
    "Łódź":       LODZ_BOUNDS,
    "Lodz":       LODZ_BOUNDS,
    "Katowice":   KATOWICE_BOUNDS,
    "Szczecin":   SZCZECIN_BOUNDS,
    "Bydgoszcz":  BYDGOSZCZ_BOUNDS,
    "Gdynia":     GDYNIA_BOUNDS,
}


def is_within_warsaw_bounds(coords: Dict[str, Any]) -> bool:
    """Check if coordinates are within Warsaw metropolitan area"""
    if not coords:
        return False
    lat = coords.get("lat", 0)
    lon = coords.get("lon", 0)
    return (WARSAW_BOUNDS["lat_min"] <= lat <= WARSAW_BOUNDS["lat_max"] and
            WARSAW_BOUNDS["lon_min"] <= lon <= WARSAW_BOUNDS["lon_max"])


# Common Polish words that are NOT street names (false positives)
STREET_BLACKLIST = {
    # Verbs and common phrases
    'znajduje', 'składa', 'posiada', 'oferuje', 'wynajmę', 'wynajmuje',
    'położone', 'położony', 'położona', 'usytuowany', 'usytuowane',
    'mieści', 'lokalizacja', 'lokalizacji', 'mieszkanie', 'mieszkania',
    'pokoje', 'pokój', 'pokoi', 'kuchnia', 'kuchni', 'łazienka', 'łazienki',
    'balkon', 'balkonu', 'piwnica', 'piwnicy', 'garaż', 'garażu',
    'budynek', 'budynku', 'blok', 'bloku', 'kamienica', 'kamienicy',
    'osiedle', 'osiedla', 'osiedlu', 'dzielnica', 'dzielnicy',
    # Adjectives
    'bardzo', 'cicha', 'cichy', 'spokojna', 'spokojny', 'nowe', 'nowy', 'nowa',
    'duże', 'duży', 'duża', 'małe', 'mały', 'mała', 'ładne', 'ładny', 'ładna',
    'jasne', 'jasny', 'jasna', 'widne', 'widny', 'widna', 'przestronne',
    # Other common words
    'views', 'from', 'architecture', 'and', 'the', 'with', 'near',
    'nieruchomości', 'nieruchomość', 'oferta', 'oferty', 'opis',
    'cena', 'ceny', 'koszt', 'koszty', 'opłaty', 'opłata',
    'wynajem', 'wynajmu', 'najem', 'najmu', 'czynsz', 'czynszu',
    'kontakt', 'telefon', 'email', 'więcej', 'informacji',
    'przy', 'obok', 'blisko', 'koło', 'niedaleko', 'pobliżu',
}


def extract_street_from_text(text: str) -> Optional[str]:
    """Extract street name from text (description, title)"""
    if not text or not isinstance(text, str):
        return None

    # Patterns to match Polish street addresses
    patterns = [
        r'ul\.?\s+([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ]?[a-ząćęłńóśźż]+)?)\s*(\d+[a-zA-Z]?)?',
        r'ulica\s+([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ]?[a-ząćęłńóśźż]+)?)\s*(\d+[a-zA-Z]?)?',
        r'al\.?\s+([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ]?[a-ząćęłńóśźż]+)?)\s*(\d+[a-zA-Z]?)?',
        r'alej[aey]?\s+([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ]?[a-ząćęłńóśźż]+)?)\s*(\d+[a-zA-Z]?)?',
    ]

    for pattern in patterns:
        matches = re.finditer(pattern, text, re.IGNORECASE)
        for match in matches:
            street_name = match.group(1).strip()
            number = match.group(2) if match.lastindex >= 2 and match.group(2) else ""

            if len(street_name) < 4:
                continue

            # Check if street name is actually a common word (false positive)
            if street_name.lower() in STREET_BLACKLIST:
                continue

            # Check if any word in the street name is blacklisted
            words = street_name.lower().split()
            if any(word in STREET_BLACKLIST for word in words):
                continue

            # Clean the street prefix for geocoding
            full_match = match.group(0).strip()
            cleaned = re.sub(r'^(ul\.?|ulica|al\.?|alej[aey]?)\s+', '', full_match, flags=re.IGNORECASE)

            if len(cleaned) >= 4:
                return cleaned

    return None


def enhance_address_for_geocoding(row: pd.Series, city: str = "Warszawa") -> str:
    """Try to extract more precise address from offer data"""
    base_address = str(row.get('Address', ''))

    # Skip invalid base addresses
    invalid_addresses = {'nieruchomości', 'unknown', 'unknown location', '-', 'n/a', 'nan', ''}
    if base_address.lower().strip() in invalid_addresses:
        base_address = ''

    # Try to find street in various fields (Title and Name are most reliable)
    sources = [
        row.get('Title', ''),
        row.get('Name', ''),
    ]

    for source in sources:
        street = extract_street_from_text(str(source) if source else '')
        if street:
            return f"{street}, {city}"

    # Try description only if no street found in title/name
    # (descriptions have more false positives)
    opis = row.get('Description', '')
    if opis:
        street = extract_street_from_text(str(opis))
        if street:
            # Additional validation for description-extracted streets
            # Require a number to be more confident it's a real address
            if re.search(r'\d', street):
                return f"{street}, {city}"

    # Fall back to base address, appending city if not already present
    if base_address and base_address.lower().strip() not in invalid_addresses:
        if city.lower() not in base_address.lower():
            return f"{base_address}, {city}"
        return base_address

    return ''


def get_ors_matrix_distances(origin: Dict, destinations: List[Dict], mode: str = "foot-walking") -> List[Dict]:
    """
    Calculate distances from origin to multiple destinations using ORS Matrix API.
    Returns list of {distance_km, duration_min} for each destination.
    """
    if not destinations:
        return []

    url = f"https://api.openrouteservice.org/v2/matrix/{mode}"
    headers = {
        "Authorization": API_KEY,
        "Content-Type": "application/json"
    }

    # Build locations array: origin first, then all destinations
    locations = [[origin["lon"], origin["lat"]]]
    for dest in destinations:
        locations.append([dest["lon"], dest["lat"]])

    body = {
        "locations": locations,
        "sources": [0],
        "destinations": list(range(1, len(locations))),
        "metrics": ["distance", "duration"]
    }

    try:
        response = requests.post(url, headers=headers, json=body, timeout=30)
        if response.status_code == 200:
            data = response.json()
            distances = data.get("distances", [[]])[0]
            durations = data.get("durations", [[]])[0]

            results = []
            for dist, dur in zip(distances, durations):
                if dist is not None and dur is not None:
                    results.append({
                        "distance_km": round(dist / 1000, 2),
                        "duration_min": round(dur / 60, 1)
                    })
                else:
                    results.append({"distance_km": None, "duration_min": None})
            return results
        else:
            logger.warning(f"ORS Matrix API error: {response.status_code}")
    except Exception as e:
        logger.error(f"ORS Matrix API exception: {e}")

    return [{"distance_km": None, "duration_min": None}] * len(destinations)


def calculate_distances_for_offers(df: pd.DataFrame, office_address: str = None,
                                    transport_mode: str = "foot-walking",
                                    city: str = "Warszawa") -> pd.DataFrame:
    """
    Calculate walking/cycling distances from each apartment to the office.
    Adds Distance_km and Duration_min columns to the dataframe.
    """
    if office_address is None:
        office_address = origin  # Use global default

    logger.info(f"🚶 Starting distance calculation for {len(df)} offers")
    logger.info(f"   Office: {office_address}")
    logger.info(f"   Mode: {transport_mode}")

    # Initialize new columns
    df["Distance_km"] = None
    df["Duration_min"] = None
    df["Geocoded_Address"] = None

    # Step 1: Geocode office address
    logger.info("📍 Geocoding office address...")
    office_coords = geocode_nominatim(office_address)
    if not office_coords:
        logger.error("❌ Failed to geocode office address - skipping distance calculation")
        return df

    logger.info(f"   Office coords: ({office_coords['lat']:.6f}, {office_coords['lon']:.6f})")
    time.sleep(NOMINATIM_DELAY)

    # Step 2: Geocode all apartments
    logger.info("📍 Geocoding apartment addresses...")
    geocoded_offers = []
    failed_count = 0

    for idx, row in df.iterrows():
        # Try enhanced address first
        address = enhance_address_for_geocoding(row, city=city)

        # Skip invalid addresses
        if not address or address.lower() in ["unknown", "unknown location", "-", "n/a", "nan", ""]:
            failed_count += 1
            continue

        coords = geocode_address(address, city=city)
        if coords:
            geocoded_offers.append({
                "idx": idx,
                "coords": coords,
                "address": address
            })
            df.at[idx, "Geocoded_Address"] = address
        else:
            failed_count += 1

        time.sleep(NOMINATIM_DELAY)

        # Progress every 10 offers
        if len(geocoded_offers) % 10 == 0:
            logger.info(f"   Geocoded: {len(geocoded_offers)}/{len(df)} offers...")

    logger.info(f"📊 Geocoding complete: {len(geocoded_offers)} successful, {failed_count} failed")

    if not geocoded_offers:
        logger.warning("⚠️ No addresses could be geocoded - skipping distance calculation")
        return df

    # Step 3: Calculate distances in batches (ORS Matrix API limit is 50)
    BATCH_SIZE = 50
    all_distances = []

    for i in range(0, len(geocoded_offers), BATCH_SIZE):
        batch = geocoded_offers[i:i + BATCH_SIZE]
        destinations = [g["coords"] for g in batch]

        logger.info(f"🚶 Calculating distances for batch {i // BATCH_SIZE + 1}...")
        distances = get_ors_matrix_distances(office_coords, destinations, transport_mode)
        all_distances.extend(zip(batch, distances))

        if i + BATCH_SIZE < len(geocoded_offers):
            time.sleep(ORS_DELAY)

    # Step 4: Update dataframe with distances
    for geo, dist in all_distances:
        idx = geo["idx"]
        df.at[idx, "Distance_km"] = dist["distance_km"]
        df.at[idx, "Duration_min"] = dist["duration_min"]

    # Statistics
    valid_distances = df["Distance_km"].dropna()
    if not valid_distances.empty:
        logger.info(f"📈 Distance statistics:")
        logger.info(f"   Range: {valid_distances.min():.2f} - {valid_distances.max():.2f} km")
        logger.info(f"   Average: {valid_distances.mean():.2f} km")
        logger.info(f"   Calculated for: {len(valid_distances)}/{len(df)} offers")

    return df


# ============================================================================
# END DISTANCE CALCULATION MODULE
# ============================================================================


def _open_spreadsheet(sheet_id: str):
    """Open a gspread spreadsheet using the service account credentials."""
    json_creds = GOOGLE_CREDS_PATH
    if not Path(json_creds).exists():
        raise RuntimeError(
            f"Google credentials not found: {json_creds}\n"
            f"Set GOOGLE_CREDS_PATH or run 'python setup.py' to get set up."
        )
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_name(str(json_creds), scope)
    client = gspread.authorize(creds)
    return client.open_by_key(sheet_id)


def _apply_config_dropdowns(spreadsheet, ws) -> None:
    """Add dropdown data-validation to Transport Mode, Schedule Days, and Schedule Time."""
    # Row indices (0-based):
    #   0: header, 1: Reference Address, 2: OLX URL, 3: Otodom URL,
    #   4: Transport Mode, 5: Schedule Days, 6: Schedule Time,
    #   7: Telegram Bot Token, 8: Telegram Chat ID, ...
    TRANSPORT_ROW    = 4
    SCHEDULE_DAYS_ROW = 5
    SCHEDULE_TIME_ROW = 6
    LANGUAGE_ROW = 14
    VALUE_COL = 1  # column B (0-based)

    transport_options = [
        "foot-walking",
        "cycling-regular",
        "driving-car",
    ]
    days_options = [
        "",
        "daily",
        "weekdays",
        "weekend",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "monday,thursday",
        "tuesday,friday",
        "monday,wednesday,friday",
    ]
    time_options = [
        "07:00",
        "08:00",
        "09:00",
        "10:00",
        "12:00",
        "14:00",
        "16:00",
        "18:00",
        "20:00",
    ]
    language_options = [
        "en",
        "pl",
    ]

    def _validation_request(row_idx: int, options: list) -> dict:
        return {
            "setDataValidation": {
                "range": {
                    "sheetId": ws.id,
                    "startRowIndex": row_idx,
                    "endRowIndex": row_idx + 1,
                    "startColumnIndex": VALUE_COL,
                    "endColumnIndex": VALUE_COL + 1,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": o} for o in options],
                    },
                    "showCustomUi": True,
                    "strict": False,
                },
            }
        }

    try:
        spreadsheet.batch_update({"requests": [
            _validation_request(TRANSPORT_ROW,     transport_options),
            _validation_request(SCHEDULE_DAYS_ROW, days_options),
            _validation_request(SCHEDULE_TIME_ROW, time_options),
            _validation_request(LANGUAGE_ROW,      language_options),
        ]})
        logger.info("📋 Config dropdowns applied")
    except Exception as e:
        logger.warning(f"Could not apply config dropdowns: {e}")


def load_config_sheet(spreadsheet, search_profile: str,
                      default_olx_url: str, default_otodom_url: str,
                      default_origin: str) -> dict:
    """Load or create the 'config' worksheet with per-profile settings.

    Sheet layout (all Value cells are user-editable):
        Setting             | Value                  | Notes
        Reference Address   | <address>              | destination for distance calc
        OLX URL             | <url>                  | edit to change OLX search
        Otodom URL          | <url>                  | edit to change Otodom search
        Transport Mode      | foot-walking           | driving-car / cycling-regular / foot-walking
        Schedule Days       |                        | monday, tuesday,friday, weekdays, weekend, etc.
        Schedule Time       | 08:00                  | time to run scraper
        Telegram Bot Token  |                        | BotFather token
        Telegram Chat ID    |                        | numeric chat ID
        Profile             | <auto>                 | set automatically
        Last scraped        | <auto>                 | set automatically

    Returns dict with keys: reference_address, olx_url, otodom_url,
                            transport_mode, schedule_days, schedule_time,
                            telegram_bot_token, telegram_chat_id
    """
    import gspread as _gspread

    SETTING_KEYS = {
        "Reference Address":  "reference_address",
        "OLX URL":            "olx_url",
        "Otodom URL":         "otodom_url",
        "Transport Mode":     "transport_mode",
        "Schedule Days":      "schedule_days",
        "Schedule Time":      "schedule_time",
        "Telegram Bot Token": "telegram_bot_token",
        "Telegram Chat ID":   "telegram_chat_id",
        "Email Recipient":    "email_recipient",
        "Email Sender":       "email_sender",
        "Email App Password": "email_app_password",
        "Email Districts":    "email_districts",
        "Email Top N":        "email_top_n",
        "Language":           "language",
    }
    defaults = {
        "reference_address":  default_origin,
        "olx_url":            default_olx_url,
        "otodom_url":         default_otodom_url,
        "transport_mode":     "foot-walking",
        "schedule_days":      "",
        "schedule_time":      "08:00",
        "telegram_bot_token": "",
        "telegram_chat_id":   "",
        "email_recipient":    "",
        "email_sender":       "",
        "email_app_password": "",
        "email_districts":    "",
        "email_top_n":        "",
        "language":           "en",
    }

    default_rows = [
        ["Setting", "Value", "Notes"],
        ["Reference Address",  default_origin,    "Edit to change the destination for distance calculation"],
        ["OLX URL",            default_olx_url,   "Edit to change the OLX search query"],
        ["Otodom URL",         default_otodom_url, "Edit to change the Otodom search query"],
        ["Transport Mode",     "foot-walking",     "Options: foot-walking, cycling-regular, driving-car"],
        ["Schedule Days",      "",                 "Days to run: monday, tuesday,friday, weekdays, weekend, etc. (or: daily)"],
        ["Schedule Time",      "08:00",            "Time to run scraper, e.g. 08:00, 18:00"],
        ["Telegram Bot Token", "",                 "Token from @BotFather - leave blank to disable notifications"],
        ["Telegram Chat ID",   "",                 "Your Telegram chat ID (get via @userinfobot)"],
        ["Email Recipient",    "",                 "Email address(es) to send the daily digest (comma-separated)"],
        ["Email Sender",       "",                 "Gmail address used to send the email"],
        ["Email App Password", "",                 "Gmail App Password (Google Account → Security → App Passwords)"],
        ["Email Districts",    "",                 "Comma-separated districts to limit the digest to, e.g. Żoliborz, Bielany, Śródmieście, Wola, Mokotów (blank = all Warsaw)"],
        ["Email Top N",        "",                 "How many top offers (by price/m²) to include in the digest (blank = 10)"],
        ["Language",           "en",               "Language for Sheet headers, email digest and Telegram messages: en or pl"],
        ["Profile",            search_profile,     "Set automatically - do not edit"],
        ["Last scraped",       "",                 "Set automatically - do not edit"],
    ]

    try:
        ws = spreadsheet.worksheet("config")
        data = ws.get_all_values()

        config = dict(defaults)
        old_schedule_val = None   # None = row absent; "" = row present but blank
        has_new_schedule_rows = False
        for row in data[1:]:
            if len(row) >= 1:
                key = row[0]
                val = row[1].strip() if len(row) >= 2 else ""
                if key == "Schedule":           # old single-row format
                    old_schedule_val = val
                elif key == "Schedule Days":
                    has_new_schedule_rows = True
                if key in SETTING_KEYS and val:
                    config[SETTING_KEYS[key]] = val

        # Migrate old single "Schedule" row to two-row format
        if old_schedule_val is not None and not has_new_schedule_rows:
            logger.info("📋 Migrating config sheet: splitting 'Schedule' into 'Schedule Days' + 'Schedule Time'")
            # Try to parse "monday 08:00" → days="monday", time="08:00"
            parts = old_schedule_val.strip().lower().split()
            migrated_days = parts[0] if len(parts) >= 1 else ""
            migrated_time = parts[1] if len(parts) >= 2 else "08:00"
            # Rebuild the sheet with the new layout
            new_rows = []
            schedule_inserted = False
            for row in data:
                if len(row) >= 1 and row[0] == "Schedule":
                    new_rows.append(["Schedule Days", migrated_days, "Days to run: monday, tuesday,friday, weekdays, weekend, etc."])
                    new_rows.append(["Schedule Time", migrated_time, "Time to run scraper, e.g. 08:00, 18:00"])
                    schedule_inserted = True
                else:
                    padded = (row + ["", "", ""])[:3]
                    new_rows.append(padded)
            if not schedule_inserted:
                # Insert before Telegram rows if Schedule row was absent
                insert_idx = next(
                    (i for i, r in enumerate(new_rows) if r[0] == "Telegram Bot Token"),
                    len(new_rows)
                )
                new_rows.insert(insert_idx, ["Schedule Days", migrated_days, "Days to run: monday, tuesday,friday, weekdays, weekend, etc."])
                new_rows.insert(insert_idx + 1, ["Schedule Time", migrated_time, "Time to run scraper, e.g. 08:00, 18:00"])
            ws.clear()
            ws.update(values=new_rows, range_name="A1")
            try:
                ws.format("A1:C1", {"textFormat": {"bold": True}})
                ws.format("A2:A12", {"textFormat": {"bold": True}})
            except Exception:
                pass
            _apply_config_dropdowns(spreadsheet, ws)
            config["schedule_days"] = migrated_days
            config["schedule_time"] = migrated_time

        logger.info(f"📋 Config - address: {config['reference_address'][:50]}")
        logger.info(f"📋 Config - transport: {config['transport_mode']}")
        logger.info(f"📋 Config - schedule: {config['schedule_days'] or '(disabled)'} {config['schedule_time']}")
        return config

    except _gspread.exceptions.WorksheetNotFound:
        logger.info(f"📋 Creating 'config' sheet for profile '{search_profile}'")
        ws = spreadsheet.add_worksheet(title="config", rows=15, cols=3)
        ws.update(values=default_rows, range_name="A1")
        try:
            ws.format("A1:C1", {"textFormat": {"bold": True}})
            ws.format("A2:A10", {"textFormat": {"bold": True}})
        except Exception:
            pass
        _apply_config_dropdowns(spreadsheet, ws)
        logger.info("📋 Config sheet created")
        return dict(defaults)


def update_config_last_scraped(spreadsheet, search_profile: str) -> None:
    """Update the 'Last scraped' and 'Profile' rows in the config sheet."""
    import gspread as _gspread
    try:
        ws = spreadsheet.worksheet("config")
        data = ws.get_all_values()
        for i, row in enumerate(data):
            if row and row[0] == "Last scraped":
                ws.update_cell(i + 1, 2, dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
            elif row and row[0] == "Profile":
                ws.update_cell(i + 1, 2, search_profile)
    except _gspread.exceptions.WorksheetNotFound:
        pass
    except Exception as e:
        logger.warning(f"Could not update config sheet: {e}")


def read_existing_status_map(worksheet) -> dict:
    """Read Link → Status mapping from the current sheet before overwriting it."""
    try:
        data = worksheet.get_all_values()
        if not data or len(data) < 2:
            return {}
        header = data[0]
        try:
            link_col = header.index("Link")
            status_col = header.index("Status")
        except ValueError:
            return {}
        result = {}
        for row in data[1:]:
            if len(row) > max(link_col, status_col):
                link = row[link_col].strip()
                status = row[status_col].strip()
                if link and status and status != "New":
                    result[link] = status
        return result
    except Exception as e:
        logger.warning(f"Could not read existing Status map: {e}")
        return {}


def read_existing_llm_scores_map(worksheet) -> dict:
    """Read Link → {"score", "summary"} from the current sheet for LLM score caching."""
    try:
        data = worksheet.get_all_values()
        if not data or len(data) < 2:
            return {}
        header = data[0]
        try:
            link_col = header.index("Link")
            score_col = header.index("LLM_Score")
            summary_col = header.index("LLM_Summary")
        except ValueError:
            return {}
        result = {}
        for row in data[1:]:
            max_col = max(link_col, score_col, summary_col)
            if len(row) > max_col:
                link = row[link_col].strip()
                score = row[score_col].strip()
                summary = row[summary_col].strip() if len(row) > summary_col else ""
                if link and score and score not in ("", "nan"):
                    result[link] = {"score": score, "summary": summary}
        return result
    except Exception as e:
        logger.debug(f"Could not read existing LLM scores: {e}")
        return {}


def restore_status_column(worksheet, status_map: dict) -> None:
    """After uploading new data restore non-'New' statuses matched by Link."""
    if not status_map:
        return
    try:
        import gspread.utils as _gu
        data = worksheet.get_all_values()
        if not data or len(data) < 2:
            return
        header = data[0]
        try:
            link_col = header.index("Link")
            status_col = header.index("Status")
        except ValueError:
            return
        updates = []
        for row_idx, row in enumerate(data[1:], start=2):
            if len(row) > link_col:
                old = status_map.get(row[link_col].strip())
                if old:
                    cell = _gu.rowcol_to_a1(row_idx, status_col + 1)
                    updates.append({"range": cell, "values": [[old]]})
        if updates:
            worksheet.batch_update(updates)
            logger.info(f"Restored {len(updates)} status values from previous run")
    except Exception as e:
        logger.warning(f"Could not restore Status column: {e}")


def send_telegram_notification(bot_token: str, chat_id: str, message: str) -> bool:
    """Send a message via Telegram Bot API. Returns True on success."""
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}, timeout=10)
        if resp.status_code == 200:
            logger.info("📱 Telegram notification sent")
            return True
        logger.warning(f"Telegram API error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"Telegram notification failed: {e}")
    return False


def col_index_to_letter(n: int) -> str:
    """Convert 1-based column index to spreadsheet column letter(s). 1→A, 26→Z, 27→AA."""
    result = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result


def get_image_formula(df: pd.DataFrame, last_row: int) -> str:
    """Build IMAGE formula using actual column position of Image_URL in the DataFrame.
    Data starts at sheet column B (offset +1 from 1-based index)."""
    if 'Image_URL' in df.columns:
        col_idx = df.columns.get_loc('Image_URL') + 2  # +1 for 1-based, +1 for col A offset
        col_letter = col_index_to_letter(col_idx)
        return f"=ARRAYFORMULA(IMAGE(${col_letter}2:${col_letter}{last_row}))"
    # Fallback: try to locate Photo_URL or Main_Image_URL
    for fallback_col in ('Photo_URL', 'Main_Image_URL'):
        if fallback_col in df.columns:
            col_idx = df.columns.get_loc(fallback_col) + 2
            col_letter = col_index_to_letter(col_idx)
            return f"=ARRAYFORMULA(IMAGE(${col_letter}2:${col_letter}{last_row}))"
    return ""


def extract_otodom_image_url(soup) -> str | None:
    """Extract main image URL from Otodom page using stable selectors + __NEXT_DATA__ fallback."""
    import json as _json

    # 1. Try __NEXT_DATA__ JSON (most reliable - server-rendered data)
    next_data_tag = soup.find('script', id='__NEXT_DATA__')
    if next_data_tag:
        try:
            data = _json.loads(next_data_tag.string)
            ad = data.get('props', {}).get('pageProps', {}).get('ad', {})
            images = ad.get('images', [])
            if images:
                img = images[0]
                return img.get('large') or img.get('medium') or img.get('small') or img.get('url')
        except Exception:
            pass

    # 2. Stable data-testid selectors
    for selector in [
        'img[data-testid="gallery-main-image"]',
        'img[data-testid="swiper-image"]',
        'img[data-cy="galleryMainImage"]',
    ]:
        el = soup.select_one(selector)
        if el and el.get('src'):
            return el['src']

    # 3. Sentry component selectors (semi-stable)
    gallery = soup.select_one('[data-sentry-element="GalleryMainContainer"] img')
    if gallery and gallery.get('src'):
        return gallery['src']

    # 4. CDN-based src filter (Otodom images come from specific CDNs)
    for img in soup.find_all('img'):
        src = img.get('src', '')
        if ('ireland.apollo.olxcdn.com' in src or 'images.otodom.pl' in src) and src.startswith('http'):
            return src

    # 5. eager-loaded img fallback (class name is fragile, but worth a last try)
    eager_img = soup.select_one('img[loading="eager"]')
    if eager_img and eager_img.get('src', '').startswith('http'):
        return eager_img['src']

    return None


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


def reorder_dataframe_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Reorder DataFrame columns with priority columns first, then remaining columns alphabetically.
    Returns a new DataFrame with reordered columns.
    """
    priority_columns = [
        'Status', 'LLM_Score', 'LLM_Summary',
        'Name', 'Base_value', 'Additional_value', 'Full_value',
        'Area', 'Address', 'Distance_km', 'Duration_min', 'Link',
        'Image_URL', 'Price_Detail', 'Area_Detail', 'Location', 'Description',
        'Seller_Info', 'Dishwasher', 'Geocoded_Address',
        # Custom extracted fields from Description
        'Deposit_PLN', 'Metro_station', 'Metro_distance_min', 'Available_From',
        'Occasional_Lease', 'Direct_From_Owner', 'Min_Period_Months', 'Balcony',
        'Air_Conditioning', 'Internet', 'Build_Year', 'Renovated',
        'Non_Smoking', 'No_Pets_Desc', 'Washing_Machine', 'Target_Tenant'
    ]
    existing_priority_columns = [col for col in priority_columns if col in df.columns]
    remaining_columns = sorted([col for col in df.columns if col not in existing_priority_columns])
    ordered_columns = existing_priority_columns + remaining_columns
    return df[ordered_columns]


# Display-only translation of our own column names for the Sheet header row.
# Internal code always keeps using the English keys below - this only changes
# what a "pl" profile sees as column headers in Google Sheets.
COLUMN_HEADERS_PL = {
    "Status": "Status", "LLM_Score": "Ocena AI", "LLM_Summary": "Podsumowanie AI",
    "Name": "Nazwa", "Base_value": "Cena bazowa",
    "Additional_value": "Opłaty dodatkowe", "Full_value": "Cena całkowita",
    "Area": "Powierzchnia", "Address": "Adres", "Distance_km": "Odległość (km)",
    "Duration_min": "Czas dojazdu (min)", "Link": "Link", "Image_URL": "Zdjęcie",
    "Price_Detail": "Cena (szczegóły)", "Price_Text": "Cena (tekst)",
    "Area_Detail": "Powierzchnia (szczegóły)", "Location": "Lokalizacja",
    "Description": "Opis", "Seller_Info": "Sprzedający", "Seller": "Sprzedający",
    "Seller_Name": "Nazwa sprzedającego", "Dishwasher": "Zmywarka",
    "Geocoded_Address": "Adres (geokodowany)", "Deposit_PLN": "Kaucja (zł)",
    "Metro_station": "Stacja metra", "Metro_distance_min": "Odległość do metra (min)",
    "Available_From": "Dostępne od", "Occasional_Lease": "Najem okazjonalny",
    "Direct_From_Owner": "Bezpośrednio", "Min_Period_Months": "Minimalny okres (miesiące)",
    "Balcony": "Balkon", "Air_Conditioning": "Klimatyzacja", "Internet": "Internet",
    "Build_Year": "Rok budowy", "Renovated": "Po remoncie", "Non_Smoking": "Niepalący",
    "No_Pets_Desc": "Bez zwierząt (opis)", "Washing_Machine": "Pralka",
    "Target_Tenant": "Dla kogo", "Room_count": "Liczba pokoi", "Floor": "Piętro",
    "City": "Miasto", "Region": "Region", "Scraping_Error": "Błąd scrapowania",
    "HTML_File_Path": "Ścieżka pliku HTML", "Main_Image_URL": "Główne zdjęcie (URL)",
    "Negotiable": "Do negocjacji", "Source": "Źródło", "Title": "Tytuł",
    "Cost_Details_Extracted": "Szczegóły kosztów", "Price_per_m2": "Cena za m²",
    "Phone_Number": "Numer telefonu", "Agent_Name": "Imię agenta",
    "Agency_Name": "Nazwa agencji", "Agency_URL": "Link do agencji",
    "Detail_Extraction_Status": "Status ekstrakcji szczegółów", "Photo_URL": "Zdjęcie",
    "Additional_Price_Text": "Opłaty dodatkowe (tekst)", "Category": "Kategoria",
    "Image_Count": "Liczba zdjęć", "Location_Breadcrumb": "Lokalizacja (okruszki)",
    "OLX_ID": "ID OLX", "Otodom_ID": "ID Otodom", "Posted_Date": "Data dodania",
    "Seller_LastSeen": "Sprzedający - ostatnio widziany",
    "Seller_Profile_URL": "Link do profilu sprzedającego", "Seller_Since": "Sprzedający od",
    "Views": "Wyświetlenia",
}


def translate_header_row(columns: list, language: str = "en") -> list:
    """Translate a list of our own English column names for display, e.g. in a
    Sheet header row. Columns we don't have a translation for (including ones
    mirroring real, already-Polish site labels) are passed through unchanged."""
    if language != "pl":
        return list(columns)
    return [COLUMN_HEADERS_PL.get(col, col) for col in columns]


OTODOM_LISTING_SELECTORS = {
    "container": [
        'article[data-sentry-component="AdvertCard"]',
        'article[data-cy="listing-item"]',
        'li[data-cy="listing-item"]',
        'div[data-cy="listing-item"]',
        'div[data-cy="search.listing.promoted"]',
        'article',
        'h1.css-1h1j0y3',
        'h1',
    ],
    "title": [
        'p[data-cy="listing-item-title"]',
        'a[data-cy="listing-item-link"] p',
        'span[data-cy="search.listing.promoted.title"]',
        'h3[data-cy="listing-item-title"]',
        'a[data-cy="listing-item-link"] span',
        'h3 span', 'h3 a', 'h2 span', 'h2 a',
        'p[class*="title"]',
    ],
    "price": [
        'span[data-sentry-element="MainPrice"]',
        'span[data-testid="ad-price"]',
        'span[data-cy="listing-item-price"]',
        'span[data-cy="adPageHeaderPrice"]',
        'p:contains("zł")',
        'span:contains("zł")',
        'strong:contains("zł")',
    ],
    "additional_price": [
        'span.css-u0t81v',
        'span.eanmlll2',
        'span.css-u0t81v:contains("czynsz")',
        'span:contains("+ czynsz")', 
        'div:contains("czynsz") span',
    ],
    "link": [
        'a[data-cy="listing-item-link"]',
        'a[href*="/oferta/"]',
        'a[href]',
    ],
    "address": [
        'p[data-sentry-component="Address"]',
        'p[data-testid="address"]',
        '[data-cy*="location"]',
        'p.css-oxb2ca',
        'header address',
    ],
    "area": [
        # Otodom dropped the "Powierzchnia" label on list cards; identify the
        # area <dd> by its value (a bare m² measurement), not by class/label.
        'dd:contains("m²")',
        'span:contains("m²")',
        'li:contains("m²")',
        'p:contains("m²")',
    ],
    "details": [
        '[data-sentry-element="DescriptionText"]',
        '[data-sentry-source-file="Description.tsx"]',
        'div[class*="css-kciq38"]',
    ]
}

OLX_SELECTORS = {
    'title': [
        'div[data-testid="offer_title"] h4',      # Primary: detail page title
        'div[data-cy="offer_title"] h4',          # Alternate
        '[data-testid="offer_title"] h4',
        'h4.css-1au435n',                          # Specific OLX title class
        'h4',                                      # Generic h4 fallback
        'h3',
        'a[data-cy="ad-link"] h4',
        'a[data-cy="listing-ad-title"]',
    ],
    'price': [
        'div[data-testid="prices-wrapper"] h3',   # Primary: price wrapper
        'div[data-testid="ad-price-container"] h3', # Price container
        'h3.css-yauxmy',                           # Specific OLX price class
        '[data-testid="ad-price"]',
        'span[data-testid="ad-price"]',
        '[data-cy="ad-price"]',
    ],
    'link': [
        'a[data-cy="ad-link"]',
        'a[href*="/oferta/"]',
        'a[data-testid="listing-ad-title"]',
        'a[data-cy="listing-ad-title"]',
        'h4 a',
        'h3 a',
        'a[href*="/d/"]',
        'a[href^="/"]',
    ],
    'area': [
        'div[data-testid="ad-parameters-container"] p',  # Parameters contain area
        'p.css-13x8d99',                           # OLX parameter text class
    ],
    'address': [
        'p[data-testid="location-date"]',          # Primary location selector
        'ol[data-testid="breadcrumbs"] li:last-child', # Breadcrumbs location
        'nav[role="navigation"] ol li:last-child', # Alternative breadcrumbs
    ],
    'description': [
        'div[data-cy="ad_description"]',           # Primary description
        'div[data-testid="ad_description"]',       # Alternate
        'div.css-19duwlz',                         # CSS class fallback
    ],
    'seller': [
        'div[data-testid="seller_card"]',          # Seller card container
        'p[data-testid="trader-title"]',           # Trader type
        'h4[data-testid="user-profile-user-name"]', # Seller name
    ],
    'phone': [
        'button[data-testid="show-phone"]',        # Phone reveal button
        'button[data-testid="ad-contact-phone"]',  # Contact phone button
    ],
    'parameters': [
        'div[data-testid="ad-parameters-container"]', # All parameters
    ],
    'image': [
        'img[data-testid="swiper-image"]',         # Main image
        'img[data-testid="swiper-image-lazy"]',    # Lazy loaded images
    ]
}

OTODOM_DETAIL_SELECTORS = {
    "title": [
        'h1[data-cy="adPageAdTitle"]',
        'h1[data-testid="ad-title"]',
        'h1',
    ],
    "price": [
        'span[data-testid="ad-price"]',
        'strong[data-cy="adPageHeaderPrice"]',
        'div[data-testid="ad-price"] span',
        'span:contains("zł")'
    ],
    "address": [
        'p[data-testid="address"]',
        'a[data-cy="adPageAdLocation"]',
        'div[data-testid="ad-location"]',
        'p',
    ],
    "details_container": [
        'div[data-sentry-component="AdDetailsBase"]',
        'div[class*="css-1wo7cpa"]',
    ],
    "item_grid": [
        'div[data-sentry-source-file="AdDetailItem.tsx"][class*="css-1xw0jqp"]',
        'div[class*="css-1xw0jqp e1gd421g1"]',
    ],
    "item_label": [
        'div[data-sentry-element="Item"][class*="css-1okys8k e1gd421g2"]',
        'div[class*="css-1okys8k e1gd421g2"]',
    ],
    "accordion_buttons": [
        'button[class*="css-1u6lqhc"]',
        'button[aria-expanded="true"]',
    ],
    "accordion_content": [
        'div[class*="n-accordionitem-content css-htlv3a"]',
        'div[data-isopen="true"][aria-hidden="false"]',
    ],
    "features_with_icons": [
        'span[class*="css-axw7ok e1gd421g4"]',
        'span[class*="css-axw7ok"]',
    ],
    "seller": [
        'div[data-sentry-component="SellerInfo"] span',
        'span:contains("nieruchomości")',
    ]
}


def _iter_cost_matches(patterns, text):
    """Yield (label, match) for each pattern match, skipping matches whose span
    overlaps text already claimed by an earlier pattern - overlapping patterns
    (e.g. 'media ...' and 'opłaty za media ...') would otherwise count the same
    amount twice."""
    claimed = []
    for pattern, label in patterns:
        for m in re.finditer(pattern, text):
            start, end = m.span()
            if any(start < c_end and c_start < end for c_start, c_end in claimed):
                continue
            claimed.append((start, end))
            yield label, m


def extract_full_cost_from_description(description: str, base_price: float) -> Dict[str, Any]:
    """
    Extract full cost information from Polish apartment descriptions using regex patterns.
    Returns dictionary with additional costs found.
    """
    if not description:
        return {"additional_costs": 0, "full_cost": base_price, "cost_details": []}

    desc_lower = description.lower()
    additional_costs = 0
    cost_details = []

    # Common Polish rental cost patterns (regex matches real Polish listing text; the
    # second tuple element is our own output label and is safe to keep in English).
    # Bare "czynsz <n>" is the base rent, so the admin pattern requires the qualifier.
    cost_patterns = [
        # Czynsz administracyjny / administrative rent
        (r'czynsz\s+admin(?:istracyjny)?:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)', 'Administrative rent'),
        (r'administracyjn\w*:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)', 'Administrative rent'),

        # Media / utilities
        (r'media:?\s*(?:około|ok\.?)?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)', 'Utilities'),
        (r'opłaty za media:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)', 'Utilities'),
        (r'koszty mediów:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)', 'Utilities'),

        # Heating / ogrzewanie
        (r'ogrzewanie:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)', 'Heating'),
        (r'c\.o\.?:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)', 'Central heating'),

        # Hot water / ciepła woda
        (r'(?:ciepła woda|c\.w\.):?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)', 'Hot water'),

        # Parking
        (r'(?:miejsce parkingowe|parking|garaż):?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)', 'Parking'),

        # Internet
        (r'internet:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)', 'Internet'),

        # General additional costs
        (r'dodatkowo:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)', 'Additional fees'),
        (r'plus:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)', 'Additional fees'),

        # Total cost patterns
        (r'razem(?:\s+z\s+opłatami)?:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)', 'Total cost'),
        (r'łącznie:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)', 'Total cost'),
    ]

    total_cost_found = None

    for cost_type, m in _iter_cost_matches(cost_patterns, desc_lower):
        try:
            cost = float(m.group(1).replace(',', '.'))
        except ValueError:
            continue

        if cost_type == 'Total cost':
            total_cost_found = cost
        else:
            additional_costs += cost
            cost_details.append(f"{cost_type}: {cost} zł")

    # If total cost is explicitly mentioned and higher than base + additional, use it
    if total_cost_found and total_cost_found > base_price:
        full_cost = total_cost_found
        if not cost_details:  # If no breakdown found, calculate additional
            additional_costs = total_cost_found - base_price
            cost_details.append(f"Additional costs: {additional_costs} zł")
    else:
        full_cost = base_price + additional_costs

    return {
        "additional_costs": additional_costs,
        "full_cost": full_cost,
        "cost_details": cost_details
    }


# ============================================================================
# CUSTOM FIELDS EXTRACTION FROM OPIS (Description)
# ============================================================================

# Metro station names for extraction
METRO_STATIONS = [
    'Młociny', 'Wawrzyszew', 'Stare Bielany', 'Słodowiec', 'Marymont',
    'Plac Wilsona', 'Dworzec Gdański', 'Ratusz Arsenał', 'Świętokrzyska',
    'Centrum', 'Politechnika', 'Pole Mokotowskie', 'Racławicka', 'Wierzbno',
    'Wilanowska', 'Służew', 'Ursynów', 'Stokłosy', 'Imielin', 'Kabaty',
    'Rondo Daszyńskiego', 'Rondo ONZ', 'Nowy Świat', 'Stadion Narodowy',
    'Dworzec Wileński', 'Szwedzka', 'Targówek Mieszkaniowy', 'Trocka', 'Zacisze',
    'Kondratowicza', 'Bródno', 'Bemowo', 'Ulrychów', 'Księcia Janusza', 'Płocka',
    'Młynów', 'Chrzanów'
]


def extract_custom_fields_from_opis(opis: str) -> Dict[str, Any]:
    """
    Extract custom fields from Polish apartment description (Description).
    Returns dictionary with extracted values.
    """
    if not opis or pd.isna(opis):
        return {}

    opis_str = str(opis)
    opis_lower = opis_str.lower()
    result = {}

    # 1. DEPOSIT - extract numeric value
    kaucja_patterns = [
        r'[Kk]aucj[aęy]\s*(?:zwrotna)?:?\s*[\s:]*(\d[\d\s,\.]*)\s*(?:zł|PLN|pln)?',
        r'[Kk]aucj[aęy]\s*(?:w\s+wysokości)?:?\s*(\d[\d\s,\.]*)\s*(?:zł|PLN|pln)?',
    ]
    for pattern in kaucja_patterns:
        match = re.search(pattern, opis_str)
        if match:
            try:
                value = match.group(1).replace(' ', '').replace(',', '.').replace('\u202f', '')
                # Handle values like "3 500" -> "3500"
                value = re.sub(r'\s+', '', value)
                result['Deposit_PLN'] = float(value)
                break
            except (ValueError, AttributeError):
                pass

    # 2. METRO STATION - find mentioned metro stations
    found_stations = []
    for station in METRO_STATIONS:
        # Look for "metro Station" or "Metro Station" pattern
        pattern = rf'\b[Mm]etr[oa]?\s+{re.escape(station)}\b'
        if re.search(pattern, opis_str, re.IGNORECASE):
            found_stations.append(station)
        # Also check for just the station name in context of metro
        elif station.lower() in opis_lower and 'metro' in opis_lower:
            # Check if station is mentioned near "metro"
            pattern2 = rf'\b{re.escape(station)}\b'
            if re.search(pattern2, opis_str, re.IGNORECASE):
                found_stations.append(station)

    if found_stations:
        result['Metro_station'] = ', '.join(list(dict.fromkeys(found_stations)))  # Remove duplicates

    # 3. METRO DISTANCE (minutes to metro)
    metro_dist_patterns = [
        r'(?:do\s+)?metr[ao]?\s*(?:\w+\s+)?(\d+)\s*min',
        r'(\d+)\s*min(?:ut)?\s*(?:do\s+)?metr[ao]',
        r'(\d+)\s*min(?:ut)?\s*(?:pieszo|spacerem)?\s*(?:do\s+)?metr[ao]',
        r'metr[ao]\s*-?\s*(\d+)\s*min',
    ]
    for pattern in metro_dist_patterns:
        match = re.search(pattern, opis_lower)
        if match:
            try:
                result['Metro_distance_min'] = int(match.group(1))
                break
            except (ValueError, AttributeError):
                pass

    # 4. AVAILABLE FROM
    if re.search(r'(?:dostępn[eay]|woln[eay])\s+od\s+zaraz|od\s+zaraz', opis_lower):
        result['Available_From'] = 'immediately'
    else:
        date_patterns = [
            r'(?:dostępn[eay]|woln[eay])\s+od\s+(\d{1,2}[\.\/]\d{1,2}[\.\/]?\d{0,4})',
            r'(?:dostępn[eay]|woln[eay])\s+od\s+(\d{1,2}\s+(?:stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|września|października|listopada|grudnia)(?:\s+\d{4})?)',
            r'od\s+(\d{1,2}\s+(?:stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|września|października|listopada|grudnia))',
        ]
        for pattern in date_patterns:
            match = re.search(pattern, opis_lower)
            if match:
                result['Available_From'] = match.group(1).strip()
                break

    # 5. OCCASIONAL LEASE (Polish "najem okazjonalny" rental agreement)
    if re.search(r'najem\s+okazjonalny|umow[ay]\s+(?:najmu\s+)?okazjonalne[goj]', opis_lower):
        result['Occasional_Lease'] = 'Yes'

    # 6. DIRECT FROM OWNER
    if re.search(r'bezpośredni[eo]|bez\s+pośredni[kó]w|bez\s+agencj[iy]|bez\s+prowizj[iy]', opis_lower):
        result['Direct_From_Owner'] = 'Yes'

    # 7. MINIMUM PERIOD (minimum rental period in months)
    min_okres_patterns = [
        r'(?:minimum|min\.?)\s*(\d+)\s*(?:rok|lat)',
        r'(?:minimum|min\.?)\s*(\d+)\s*miesi[aąę]c',
        r'(?:umowa\s+)?na\s+(?:min(?:imum)?\.?\s*)?(\d+)\s*rok',
        r'(?:umowa\s+)?na\s+(?:min(?:imum)?\.?\s*)?(\d+)\s*miesi[aąę]c',
        r'na\s+minimum\s+(\d+)\s*(?:rok|lat|miesi)',
        # "minimalny okres (najmu)" - adjective form, not covered by "minimum"/"min" above
        r'minimaln[aey]?\s+okres(?:\s+najmu)?:?\s*(?:na\s*)?(\d+)\s*(?:rok|lat)',
        r'minimaln[aey]?\s+okres(?:\s+najmu)?:?\s*(?:na\s*)?(\d+)\s*miesi[aąę]c',
    ]
    for pattern in min_okres_patterns:
        match = re.search(pattern, opis_lower)
        if match:
            try:
                value = int(match.group(1))
                # If it's in years, convert to months
                if 'rok' in pattern or 'lat' in pattern:
                    value = value * 12
                result['Min_Period_Months'] = value
                break
            except (ValueError, AttributeError):
                pass

    # 8. BALCONY
    if re.search(r'\b(?:balkon|balkonu|balkonem|loggi[aąę])\b', opis_lower):
        result['Balcony'] = 'Yes'

    # 9. AIR CONDITIONING
    if re.search(r'klimatyzacj[aąę]|klimatyzowany|klimatyzowane', opis_lower):
        result['Air_Conditioning'] = 'Yes'

    # 10. INTERNET (internet included)
    if re.search(r'\binternet\b|wi-?fi|\bwifi\b|światłowód', opis_lower):
        # Check if internet is included in price
        if re.search(r'internet\s+(?:w\s+cenie|wliczony|gratis|bezpłatny)', opis_lower):
            result['Internet'] = 'Included in price'
        else:
            result['Internet'] = 'Available'

    # 11. BUILD YEAR
    rok_patterns = [
        r'(?:z|budyn(?:ek|ku)\s+z|blok\s+z)\s+(\d{4})\s*r?\.?',
        r'z\s+roku\s+(\d{4})',
        r'wybudowan[yae]\s+w\s+(\d{4})',
    ]
    for pattern in rok_patterns:
        match = re.search(pattern, opis_str)
        if match:
            try:
                year = int(match.group(1))
                if 1900 <= year <= 2030:
                    result['Build_Year'] = year
                    break
            except (ValueError, AttributeError):
                pass

    # 12. RENOVATED
    if re.search(r'po\s+(?:generalnym\s+)?remoncie|świeżo\s+(?:od|wy)?nowion|wyremontowa[ny]|odnowion', opis_lower):
        result['Renovated'] = 'Yes'
    elif re.search(r'do\s+(?:dużego\s+)?remontu', opis_lower):
        result['Renovated'] = 'Needs renovation'

    # 13. NON-SMOKING
    if re.search(r'niepalą[cyceąi]+|dla\s+niepalących|osoby?\s+niepalą[cyceąi]+|palą[cyceąi]+\s+nie', opis_lower):
        result['Non_Smoking'] = 'Yes'

    # 14. NO PETS (from description - more reliable than the form field)
    if re.search(r'bez\s+zwierząt|nie\s+akceptuj[ęe]\s+zwierząt|zwierząt[aom]+\s+nie|zwierzęt[aom]+\s+dziękuj', opis_lower):
        result['No_Pets_Desc'] = 'Yes'
    elif re.search(r'(?:mile\s+widziane?\s+)?zwierzęt[aomy]+\s+(?:ok|akceptowane|mile)|przyjazne?\s+(?:dla\s+)?zwierząt|akceptuj[ęe]\s+zwierząt|ze\s+zwierzęt', opis_lower):
        result['No_Pets_Desc'] = 'No (pets accepted)'

    # 15. WASHING MACHINE
    if re.search(r'\bpralk[aąę]\b', opis_lower):
        result['Washing_Machine'] = 'Yes'

    # 16. TARGET TENANT
    target = []
    if re.search(r'dla\s+(?:1-?2|dwóch?)\s+osób?|idealne?\s+dla\s+pary|dla\s+pary', opis_lower):
        target.append('couple')
    if re.search(r'dla\s+singla|dla\s+jednej?\s+osoby', opis_lower):
        target.append('single')
    if re.search(r'dla\s+student[aówki]+|idealne?\s+dla\s+student', opis_lower):
        target.append('student')
    if re.search(r'dla\s+kobiet[y]?|dla\s+dziewczyn[y]?', opis_lower):
        target.append('woman')
    if re.search(r'dla\s+rodzin[y]?|z\s+dziećmi', opis_lower):
        target.append('family')
    if target:
        result['Target_Tenant'] = ', '.join(target)

    return result


def apply_custom_fields_extraction(df: pd.DataFrame) -> pd.DataFrame:
    """
    Apply custom field extraction from Description column to entire DataFrame.
    Returns DataFrame with new columns added.
    """
    if 'Description' not in df.columns:
        logger.warning("⚠️ No 'Description' column found - skipping custom field extraction")
        return df

    logger.info("🔍 Extracting custom fields from descriptions...")

    # Define all possible custom columns with default empty values
    custom_columns = [
        'Deposit_PLN', 'Metro_station', 'Metro_distance_min', 'Available_From',
        'Occasional_Lease', 'Direct_From_Owner', 'Min_Period_Months', 'Balcony',
        'Air_Conditioning', 'Internet', 'Build_Year', 'Renovated',
        'Non_Smoking', 'No_Pets_Desc', 'Washing_Machine', 'Target_Tenant'
    ]

    # Initialize columns if they don't exist
    for col in custom_columns:
        if col not in df.columns:
            df[col] = ''

    # Extract custom fields for each row
    extracted_count = 0
    for idx, row in df.iterrows():
        opis = row.get('Description', '')
        if pd.isna(opis) or not opis:
            continue

        extracted = extract_custom_fields_from_opis(opis)
        if extracted:
            extracted_count += 1
            for key, value in extracted.items():
                df.at[idx, key] = value

    logger.info(f"✅ Extracted custom fields from {extracted_count}/{len(df)} descriptions")

    # Log statistics for each field
    for col in custom_columns:
        non_empty = df[col].notna() & (df[col] != '')
        count = non_empty.sum()
        if count > 0:
            logger.debug(f"  {col}: {count} values extracted")

    return df


def batch_update_sheets(batch_data: list, sheet_id: str, start_row: int = 2):
    """
    Update Google Sheets with batch data to avoid repeated API calls.
    """
    try:
        json_creds = GOOGLE_CREDS_PATH
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_name(json_creds, scope)
        client = gspread.authorize(creds)

        spreadsheet = client.open_by_key(sheet_id)
        ws = spreadsheet.worksheet("apartment list")

        # Calculate range based on data
        end_row = start_row + len(batch_data) - 1
        range_name = f'B{start_row}:BJ{end_row}'  # Adjust column range as needed

        ws.update(values=batch_data, range_name=range_name)
        logger.info(f"✅ Updated {len(batch_data)} rows to sheets (rows {start_row}-{end_row})")

    except Exception as e:
        logger.error(f"❌ Failed to update batch to sheets: {e}")


def prioritize_offers_by_value(offers_df: pd.DataFrame) -> pd.DataFrame:
    """
    Sort offers by value to prioritize cheaper ones for faster processing.
    """
    if offers_df.empty:
        return offers_df

    # Calculate value per square meter for better comparison
    offers_df['price_per_sqm'] = offers_df.apply(
        lambda row: row['Base_value'] / row['Area'] if row['Area'] > 0 else row['Base_value'],
        axis=1
    )

    # Sort by: 1) Price per sqm, 2) Total price, 3) Area (descending for same price)
    sorted_df = offers_df.sort_values([
        'price_per_sqm',
        'Base_value',
        'Area'
    ], ascending=[True, True, False]).reset_index(drop=True)

    logger.info(f"📊 Sorted {len(sorted_df)} offers by value (cheapest first)")
    return sorted_df


def enhanced_cost_extraction(offers_df: pd.DataFrame) -> pd.DataFrame:
    """
    Apply smart cost extraction to all offers with descriptions.
    """
    logger.info("💰 Starting enhanced cost extraction from descriptions...")

    updated_count = 0

    for index, row in offers_df.iterrows():
        if pd.isna(row.get('Description')) or not row.get('Description'):
            continue

        cost_info = extract_full_cost_from_description(row['Description'], row['Base_value'])

        # Update additional costs if found and current additional_value is 0
        if cost_info['additional_costs'] > 0 and row.get('Additional_value', 0) == 0:
            offers_df.at[index, 'Additional_value'] = cost_info['additional_costs']
            offers_df.at[index, 'Full_value'] = cost_info['full_cost']
            offers_df.at[index, 'Cost_Details_Extracted'] = '; '.join(cost_info['cost_details'])
            updated_count += 1

            logger.debug(f"Updated offer {index}: {cost_info['cost_details']}")

    logger.info(f"✅ Enhanced cost extraction completed: {updated_count} offers updated")
    return offers_df


# MODIFIED BATCH PROCESSING FOR DETAILED EXTRACTION
def process_batch_with_updates(mixed_extraction_plan, batch_start, batch_size,
                               page_otodom, page_olx, sheet_id, progress_tracker=None):
    """
    Process a batch and immediately update sheets with results.
    """
    batch_end = min(batch_start + batch_size, len(mixed_extraction_plan))
    current_batch = mixed_extraction_plan[batch_start:batch_end]

    logger.info(f"🔄 Processing batch {batch_start // batch_size + 1}: items {batch_start + 1}-{batch_end}")

    batch_results = []
    processed_indices = []

    for i, (source, index) in enumerate(current_batch):
        item_start_time = time.time()
        try:
            if source == 'otodom':
                success = extract_otodom_details(page_otodom, index, batch_start + i + 1,
                                                 len(mixed_extraction_plan))
            else:
                success = extract_olx_details(page_olx, index, batch_start + i + 1,
                                              len(mixed_extraction_plan))

            if success:
                row_data = offers_df.loc[index].tolist()
                batch_results.append(row_data)
                processed_indices.append(index)

                logger.info(f"✅ Successfully processed {source} offer {index}")
            else:
                logger.warning(f"⚠️ Failed to process {source} offer {index}")

            # Update progress tracker
            if progress_tracker:
                progress_tracker.update(item_start_time)

            # Variable delay between requests
            delay = random.uniform(2.0, 6.0)
            time.sleep(delay)

        except Exception as e:
            logger.error(f"❌ Error processing {source} offer {index}: {e}")
            # Still update progress on error
            if progress_tracker:
                progress_tracker.update(item_start_time)
            continue

    # Update sheets with batch results
    if batch_results:
        try:
            # Calculate the starting row for this batch in sheets
            sheet_start_row = batch_start + 2  # +2 because sheet starts at row 2
            batch_update_sheets(batch_results, sheet_id, sheet_start_row)

            logger.info(f"📊 Batch {batch_start // batch_size + 1} completed: "
                        f"{len(batch_results)}/{len(current_batch)} offers processed and updated")
        except Exception as e:
            logger.error(f"❌ Failed to update batch results to sheets: {e}")

    return processed_indices


# ADD THIS TO YOUR MAIN SCRAPING LOOP (replace the existing batch processing)
def improved_mixed_extraction(offers_df, sheet_id):
    """
    Improved mixed extraction with immediate batch updates and cost analysis.
    """

    # Step 1: Enhance cost extraction from existing descriptions
    offers_df = enhanced_cost_extraction(offers_df)

    # Step 2: Sort offers by value (cheapest first)
    offers_df = prioritize_offers_by_value(offers_df)

    # Step 3: Prepare extraction plan
    offers_otodom = offers_df[offers_df["Link"].str.contains("otodom.pl", na=False, regex=False)].copy()
    offers_olx = offers_df[offers_df["Link"].str.contains("olx.pl", na=False, regex=False)].copy()

    # Create mixed plan prioritizing cheapest offers
    mixed_extraction_plan = []
    otodom_indices = offers_otodom.index.tolist()
    olx_indices = offers_olx.index.tolist()

    for i in range(max(len(otodom_indices), len(olx_indices))):
        if i < len(otodom_indices):
            mixed_extraction_plan.append(('otodom', otodom_indices[i]))
        if i < len(olx_indices):
            mixed_extraction_plan.append(('olx', olx_indices[i]))

    logger.info(
        f"🔀 Mixed extraction plan: {len(offers_otodom)} Otodom + {len(offers_olx)} OLX = {len(mixed_extraction_plan)} total")

    # Step 4: Process in batches with immediate updates
    BATCH_SIZE = 10  # Reduced for faster updates
    BATCH_BREAK = 20  # Reduced break time

    # Create pages for both sources
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
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
        page_otodom = context.new_page()
        page_olx = context.new_page()

        all_processed = []
        processed_offers = improved_mixed_extraction(offers_df, sheet_id)

        browser.close()

    logger.info(f"✅ Mixed extraction completed: {len(all_processed)} offers processed")
    return all_processed


# ADDITIONAL REGEX PATTERNS FOR BETTER COST EXTRACTION
POLISH_COST_PATTERNS = {
    'utilities': [
        r'media:?\s*(?:około|ok\.?)?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)',
        r'opłaty\s*(?:za\s*)?media:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)',
        r'rachunki:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)',
    ],
    'admin_fee': [
        r'czynsz\s*administracyjny:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)',
        r'opłata\s*administracyjna:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)',
        r'administracja:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)',
    ],
    'parking': [
        r'(?:miejsce\s*)?parking(?:owe)?:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)',
        r'garaż:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)',
        r'miejsce\s*garażowe:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)',
    ],
    'deposit': [
        r'kaucja\s*(?:zwrotna)?:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)',
        r'depozyt:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)',
        r'zabezpieczenie:?\s*(\d+(?:[.,]\d+)?)\s*(?:zł|pln)',
    ]
}


def extract_detailed_costs(description: str) -> Dict[str, float]:
    """
    Extract detailed cost breakdown using comprehensive regex patterns.
    """
    if not description:
        return {}

    desc_lower = description.lower()
    costs = {}

    for cost_category, patterns in POLISH_COST_PATTERNS.items():
        total_cost = 0
        pattern_pairs = [(p, cost_category) for p in patterns]
        for _, m in _iter_cost_matches(pattern_pairs, desc_lower):
            try:
                total_cost += float(m.group(1).replace(',', '.'))
            except ValueError:
                continue

        if total_cost > 0:
            costs[cost_category] = total_cost

    return costs



def find_element_by_selectors(soup, selectors, attribute=None):
    """Try multiple selectors until one returns a result"""
    for selector in selectors:
        try:
            if ':contains(' in selector:
                # Handle CSS :contains() pseudo-selector (not supported by BeautifulSoup)
                import re
                match = re.search(r':contains\("([^"]+)"\)', selector)
                if match:
                    search_text = match.group(1)
                    base_selector = selector.split(':contains(')[0]
                    elements = soup.select(base_selector) if base_selector else soup.find_all()
                    for element in elements:
                        if search_text in element.get_text():
                            if attribute:
                                return element.get(attribute)
                            return element
            else:
                element = soup.select_one(selector)
                if element:
                    if attribute:
                        return element.get(attribute)
                    return element
        except Exception as e:
            continue
    return None

def find_elements_by_selectors(soup, selectors):
    """Try multiple selectors until one returns results (plural version)"""
    for selector in selectors:
        try:
            elements = soup.select(selector)
            if elements:
                return elements
        except Exception as e:
            continue
    return []

def create_offer_key(title: str, price: int, link: str = "") -> str:
    """Create unique key for offer to detect duplicates - ENHANCED FOR ROBUSTNESS"""
    # Clean title and create normalized key
    clean_title = re.sub(r'[^\w\s]', '', title.lower()).strip()
    # Remove common words that don't distinguish offers
    common_words = ['mieszkanie', 'wynajem', 'sprzedaz', 'pokoje', 'pokojowe', 'do', 'w', 'na', 'z', 'i', 'a']
    title_words = [word for word in clean_title.split() if word not in common_words and len(word) > 2]
    
    # Create key from first 3-4 meaningful words + price
    meaningful_title = ' '.join(title_words[:4]) if title_words else clean_title[:30]
    
    # For link-based deduplication (more reliable)
    if link:
        # Extract unique identifier from link
        link_id = ""
        if "olx.pl" in link:
            # OLX links typically have format: /oferta/title-ID.html
            match = re.search(r'-(\d+)(?:\.html)?$', link)
            if match:
                link_id = match.group(1)
        elif "otodom.pl" in link:
            # Otodom links have unique path segments
            match = re.search(r'/oferta/([^/]+)$', link)
            if match:
                link_id = match.group(1)
        
        if link_id:
            return f"link_{link_id}_{price}"
    
    # Fallback to title + price based key
    key = f"title_{meaningful_title}_{price}"
    return key

def is_duplicate_offer(title: str, price: int, link: str = "") -> bool:
    """Check if offer is duplicate - ENHANCED WITH BETTER DETECTION"""
    global seen_offers
    
    # First check: exact link duplication (most reliable)
    if link:
        link_key = f"exact_link:{link}"
        if link_key in seen_offers:
            logger.debug(f"🔄 Duplicate detected by exact link: {link}")
            return True
        seen_offers.add(link_key)
    
    # Second check: similar content (title + price combination)
    content_key = create_offer_key(title, price, link)
    if content_key in seen_offers:
        logger.debug(f"🔄 Duplicate detected by content similarity: {content_key}")
        return True
    seen_offers.add(content_key)
    
    # Third check: price + location similarity for very similar offers
    if len(title) > 20:  # Only for detailed titles
        # Extract location hints from title
        location_hints = re.findall(r'\b(?:warszawa|wola|bemowo|bielany|mokotów|praga|śródmieście|żoliborz)\b', title.lower())
        if location_hints:
            location_key = f"location_{price}_{location_hints[0]}"
            # Allow up to 2 similar offers per price+location (less strict)
            similar_count = sum(1 for key in seen_offers if location_key in key)
            if similar_count >= 2:
                logger.debug(f"🔄 Duplicate detected by price+location similarity: {location_key}")
                return True
            seen_offers.add(f"{location_key}_{similar_count}")
    
    return False

def validate_olx_link(link: str) -> str:
    """Validate and fix OLX link format"""
    if not link:
        return ""
    
    # Clean up the link
    link = link.strip()
    
    # Handle relative URLs
    if link.startswith('/'):
        link = f"https://www.olx.pl{link}"
    
    # Ensure proper protocol
    if not link.startswith('http'):
        link = f"https://{link}"
    
    # Validate it's actually an OLX link and contains required parts
    if 'olx.pl' in link and '/oferta/' in link:
        return link
    
    return ""

def validate_offer_data(offer_dict: dict, source: str = "Unknown") -> tuple[bool, list]:
    """Validate scraped offer data and return validation status with issues list"""
    issues = []
    is_valid = True
    
    # Required fields validation
    required_fields = {
        'Name': lambda x: x and len(str(x).strip()) > 5,
        'Base_value': lambda x: isinstance(x, (int, float)) and x > 0,
        'Link': lambda x: x and str(x).startswith('http')
    }
    
    for field, validator in required_fields.items():
        if field not in offer_dict or not validator(offer_dict[field]):
            issues.append(f"Invalid {field}: {offer_dict.get(field, 'MISSING')}")
            is_valid = False
    
    # Data quality checks
    if 'Address' in offer_dict:
        address = str(offer_dict['Address']).strip().lower()
        if not address or address in ['unknown', 'unknown location', '-', 'n/a']:
            issues.append("Missing meaningful address")
    
    if 'Base_value' in offer_dict and offer_dict['Base_value']:
        try:
            price = float(offer_dict['Base_value'])
            if price < 500 or price > 50000:  # Reasonable price range for Polish market
                issues.append(f"Suspicious price: {price} zł")
        except (TypeError, ValueError):
            issues.append(f"Invalid price format: {offer_dict['Base_value']}")
            is_valid = False
    
    # Source-specific validation
    if source == "OLX" and 'Link' in offer_dict:
        if "olx.pl" not in str(offer_dict['Link']):
            issues.append(f"OLX link doesn't contain olx.pl domain: {offer_dict['Link']}")
            is_valid = False
    elif source == "Otodom" and 'Link' in offer_dict:
        if "otodom.pl" not in str(offer_dict['Link']):
            issues.append(f"Otodom link doesn't contain otodom.pl domain: {offer_dict['Link']}")
            is_valid = False
    
    return is_valid, issues

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
        logger.info(f"📊 SCRAPING STATISTICS:")
        logger.info(f"   📁 Total offers: {total_offers}")
        logger.info(f"   📱 OLX offers: {olx_offers} ({(olx_offers/total_offers*100):.1f}%)")
        logger.info(f"   🏠 Otodom offers: {otodom_offers} ({(otodom_offers/total_offers*100):.1f}%)")
        logger.info(f"   💰 Price stats: {price_stats}")
        logger.info(f"   ⚠️ Data quality: {missing_data}")

    except Exception as e:
        logger.error(f"❌ Failed to generate scraping statistics: {e}")


# Add a global function to save intermediate data to sheets
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
        ws = spreadsheet.worksheet("apartment list")
        
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
        ws = spreadsheet.worksheet("apartment list")

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


def scroll_alot(page):
    page.mouse.wheel(10000, 10000)
    time.sleep(0.2)
    page.mouse.wheel(10000, 10000)

# WORKING REPLACEMENT - Replace your scrape_otodom function with this

def parse_polish_date(date_text: str) -> str:
    """
    Improved Polish date-parsing function.
    Handles all the date formats seen on OLX/Otodom.
    """
    if not date_text:
        return None

    months = {
        "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, 
        "maja": 5, "czerwca": 6, "lipca": 7, "sierpnia": 8, 
        "września": 9, "października": 10, "listopada": 11, "grudnia": 12
    }

    text = date_text.lower().strip()
    
    # 1. "Dzisiaj o 15:04" / "Dzisiaj"
    if "dzisiaj" in text:
        return dt.datetime.today().strftime("%Y-%m-%d")
    
    # 2. "Wczoraj o 14:30" / "Wczoraj"
    if "wczoraj" in text:
        return (dt.datetime.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    
    # 3. "Odświeżono Dzisiaj o 16:05"
    if "odświeżono" in text and "dzisiaj" in text:
        return dt.datetime.today().strftime("%Y-%m-%d")
    
    # 4. "X dni temu" / "X dzień temu"
    match = re.search(r'(\d+)\s+(?:dni|dzień|dnia)\s+temu', text)
    if match:
        days = int(match.group(1))
        return (dt.datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    
    # 5. "16 listopada 2025" / "Odświeżono dnia 17 listopada 2025"
    match = re.search(r'(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})', text)
    if match:
        day = int(match.group(1))
        month_name = match.group(2)
        year = int(match.group(3))
        
        month = months.get(month_name)
        if month:
            try:
                return dt.datetime(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                pass
    
    # 6. "17 listopada 2025" (without "dnia")
    match = re.search(r'(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})', text)
    if match:
        day = int(match.group(1))
        month_name = match.group(2)
        year = int(match.group(3))
        
        month = months.get(month_name)
        if month:
            try:
                return dt.datetime(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                pass
    
    return None


def _select_one_flexible(container, selector: str):
    """
    BeautifulSoup doesn't support :contains in CSS; handle a couple of common pseudo cases:
      - tag:contains("needle")
      - dd:has(svg) span   (limited support)
    Otherwise, fall back to container.select_one(selector).
    Returns element or None.
    """
    if not selector:
        return None

    # handle :contains("...") optionally with a tag
    if ":contains(" in selector:
        # patterns: 'p:contains("m²")', 'span:contains("zł")'
        m = re.match(r'^\s*([a-zA-Z0-9\-\_\.\*]+)?\s*:\s*contains\(\s*["\'](.+?)["\']\s*\)\s*(.*)$', selector)
        if m:
            tag, needle, tail = m.group(1), m.group(2), (m.group(3) or "").strip()
            needle = needle.strip()
            # search all matching tags (or any tag) containing the needle
            candidates = container.find_all(tag if tag and tag != '*' else True,
                                            string=lambda x: x and needle in x)
            if not candidates:
                # also try by element text (not only exact string node)
                candidates = [el for el in container.find_all(tag if tag and tag != '*' else True)
                              if needle in el.get_text(strip=True)]
            if not candidates:
                return None
            elem = candidates[0]
            if tail:
                # there is a trailing simple descendant like 'span'
                # try to resolve one level 'tail' (only simple tag name)
                tail_tag = tail.split()[0]
                found = elem.select_one(tail_tag) if elem else None
                return found or elem
            return elem

    # limited :has(svg) support for patterns like 'dd:has(svg) span'
    if ":has(" in selector:
        if selector.startswith("dd:has(svg)"):
            dd = next((d for d in container.find_all("dd") if d.find("svg")), None)
            if not dd:
                return None
            if "span" in selector:
                return dd.find("span")
            return dd

    # default
    try:
        return container.select_one(selector)
    except Exception:
        return None

def extract_with_fallback_selectors(container, selectors, *, return_element=False):
    """
    Try multiple selectors (with flexible support) and return first non-empty text (or element).
    This is now used consistently across scrapers.
    """
    if not selectors:
        return None

    for selector in selectors:
        elem = _select_one_flexible(container, selector)
        if elem:
            if return_element:
                return elem
            text = elem.get_text(strip=True)
            if text:
                return text
    return None

def _number_from_text(text, default=0, allow_float=True):
    if not text:
        return default
    m = re.search(r'(\d+(?:[.,]\d+)?)', text)
    if not m:
        return default
    val = m.group(1).replace(",", ".")
    try:
        return float(val) if allow_float else int(float(val))
    except ValueError:
        return default

def _sanitize_address(addr_text: str):
    if not addr_text:
        return None
    # drop time like 'o 14:16'
    addr = re.sub(r'(?:\bo\s+)?\b\d{1,2}:\d{2}\b', '', addr_text)
    # OLX often shows text like "City, District - Refreshed ..."
    addr = addr.split(" - ")[0]
    addr = re.sub(r'\s+', ' ', addr).strip(" -\u2013")
    return addr or None


# ----------------------- OTODOM -----------------------

def scrape_otodom_working(html_content):
    """
    Robust Otodom scraper using updated selectors + date/area/address extraction.
    Measures per-offer and per-stage timings and logs the slowest stages.
    """
    global offers_df
    if offers_df is None:
        offers_df = pd.DataFrame()

    if not html_content or len(html_content) < 1000:
        logger.warning("HTML content too short")
        return 0

    soup = BeautifulSoup(html_content, "html.parser")

    # containers
    offers_found = []
    for selector in OTODOM_LISTING_SELECTORS.get("container", []):
        offers_found = soup.select(selector)
        if offers_found:
            break

    logger.info(f"Found {len(offers_found)} potential Otodom offers")
    if not offers_found:
        return 0

    added_count = 0
    skipped_count = 0

    stage_totals = {
        "title": 0.0, "link": 0.0, "price": 0.0,
        "area": 0.0, "address": 0.0, "date": 0.0,
        "append": 0.0
    }
    offer_durations = []

    for i, offer in enumerate(offers_found):
        t0 = time.perf_counter()
        try:
            # ---- title
            t = time.perf_counter()
            title = extract_with_fallback_selectors(offer, OTODOM_LISTING_SELECTORS.get("title", []))
            stage_totals["title"] += (time.perf_counter() - t)
            if not title:
                skipped_count += 1
                continue

            # ---- link
            t = time.perf_counter()
            link = None
            for selector in OTODOM_LISTING_SELECTORS.get("link", []):
                el = _select_one_flexible(offer, selector)
                if el and el.get("href"):
                    href = el.get("href")
                    link = href if href.startswith("http") else f"https://www.otodom.pl{href}"
                    break
            stage_totals["link"] += (time.perf_counter() - t)
            if not link:
                skipped_count += 1
                continue

            # ---- price
            t = time.perf_counter()
            price_text = extract_with_fallback_selectors(offer, OTODOM_LISTING_SELECTORS.get("price", []))
            if not price_text:
                # regex fallback
                price_match = re.search(r'([\d\s]+)\s*zł', offer.get_text(" ", strip=True))
                price_text = price_match.group(1) + " zł" if price_match else None
            price_value = int(re.sub(r"\D", "", price_text)) if price_text else 0
            if price_value < 100:
                skipped_count += 1
                continue

            additional_price_text = extract_with_fallback_selectors(offer, OTODOM_LISTING_SELECTORS.get("additional_price", []))
            if not additional_price_text:
                # regex fallback
                price_match = re.search(r'([\d\s]+)\s*zł', offer.get_text(" ", strip=True))
                additional_price_text = price_match.group(1) + " zł" if price_match else None
            additional_price_value = int(re.sub(r"\D", "", additional_price_text)) if additional_price_text else 0
            stage_totals["price"] += (time.perf_counter() - t)

            # ---- area
            t = time.perf_counter()
            area = 0.0
            area_text = None

            # 1) prefer <dl> DescriptionList. Otodom no longer labels area
            #    "Powierzchnia" on list cards (the m² value now sits under a
            #    "Cena za metr kwadratowy" label) and every <dd> shares the same
            #    class, so identify the area by its value: the <dd> reading
            #    "<number> m²". Rooms read "… pokój/pokoje", floor "… piętro";
            #    skip any "… zł/m²" price-per-metre value.
            dl_block = (offer.find("dl", {"data-sentry-component": "DescriptionList"})
                        or offer.find("dl"))
            if dl_block:
                for dd_el in dl_block.find_all("dd"):
                    dd_text = dd_el.get_text(strip=True)
                    if re.search(r'm[²2]', dd_text) and "zł" not in dd_text.lower():
                        area_text = dd_text
                        break

            # 2) fallback to selector list
            if not area_text:
                area_text = extract_with_fallback_selectors(offer, OTODOM_LISTING_SELECTORS.get("area", []))

            # 3) final regex sweep (skip price-per-metre "… zł/m²")
            if not area_text:
                full_text = offer.get_text(" ", strip=True)
                for m in re.finditer(r'(\d+(?:[.,]\d+)?)\s*m[²2]', full_text):
                    if "zł" not in full_text[max(0, m.start() - 8):m.end()].lower():
                        area_text = m.group(0)
                        break

            if area_text:
                area = _number_from_text(area_text, default=0.0, allow_float=True)
            stage_totals["area"] += (time.perf_counter() - t)

            # ---- address
            t = time.perf_counter()
            address = extract_with_fallback_selectors(offer, OTODOM_LISTING_SELECTORS.get("address", []))
            address = _sanitize_address(address) or address or "Unknown location"
            stage_totals["address"] += (time.perf_counter() - t)

            # ---- added date
            t = time.perf_counter()
            added_date = None
            # current badge on list cards often shows "Dodane ..."
            # try multiple resilient hooks:
            date_elem = (
                offer.select_one('[data-sentry-element="NexusBadge"]') or
                offer.select_one('[data-sentry-component="CustomizedTag"]') or
                next((el for el in offer.find_all(["div", "span", "p"])
                      if el.get_text(strip=True).lower().startswith("dodane")), None)
            )
            if date_elem:
                added_date = parse_polish_date(date_elem.get_text(strip=True))
            stage_totals["date"] += (time.perf_counter() - t)

            # ---- build row
            # validate (optional)
            offer_dict = {
                "Name": title,
                "Base_value": price_value,
                "Additional_value": additional_price_value,
                "Full_value": price_value,
                "Area": area,
                "Address": address,
                "Link": link,
                "Added_Date": added_date,
                "Source": "Otodom"
            }

            # dedupe
            if is_duplicate_offer(title, price_value, link):
                skipped_count += 1
                continue

            # ---- append
            t = time.perf_counter()
            offer_row_df = pd.DataFrame([offer_dict])
            offers_df = pd.concat([offers_df, offer_row_df], ignore_index=True)
            stage_totals["append"] += (time.perf_counter() - t)

            added_count += 1

        except Exception as e:
            logger.error(f"Otodom error on offer #{i+1}: {e}")
            skipped_count += 1
        finally:
            offer_durations.append(time.perf_counter() - t0)

    # summary timings
    if offer_durations:
        slowest = sorted(offer_durations, reverse=True)[:5]
        logger.info(f"Otodom slowest offers (s): {', '.join(f'{d:.3f}' for d in slowest)}")
    slow_stages = sorted(stage_totals.items(), key=lambda kv: kv[1], reverse=True)
    logger.info("Otodom stage totals (s): " + ", ".join(f"{k}={v:.3f}" for k, v in slow_stages))
    logger.info(f"Otodom summary: Added={added_count}, Skipped={skipped_count}")

    return added_count

def parse_price_to_float(price_text):
    """
    Parse price text into a float handling formats like:
      - '2 700,50 zł'
      - '2.700,50 zł'
      - '1 999.99 zł'
      - '3.200 zł'
      - '1999 zł'
    Returns float (0.0 if parsing fails).
    """
    if not price_text:
        return 0.0

    # Extract the contiguous block with digits, spaces, dots and commas (and NBSP)
    m = re.search(r'[\d\.\,\s\u00A0]+', price_text)
    if not m:
        return 0.0

    raw = m.group(0).strip()
    # normalize non-breaking space
    raw = raw.replace("\u00A0", " ")

    # remove surrounding spaces
    raw = raw.strip()

    # Remove inner spaces (thousand separator as space)
    s = raw.replace(" ", "")

    # CASE 1: both '.' and ',' present
    # Most common: '.' as thousands separator and ',' as decimal (e.g. "2.700,50")
    if '.' in s and ',' in s:
        # decide which is the decimal separator by position (the last separator is usually the decimal)
        if s.rfind(',') > s.rfind('.'):
            # comma as decimal, dots as thousands -> remove dots, replace comma with dot
            normalized = s.replace('.', '').replace(',', '.')
        else:
            # dot appears after last comma -> dot likely decimal, comma thousands - remove commas
            normalized = s.replace(',', '')
    # CASE 2: only comma present -> treat comma as decimal separator (Polish style)
    elif ',' in s:
        normalized = s.replace(',', '.')
    # CASE 3: only dot present -> ambiguous: could be thousands or decimal
    elif '.' in s:
        # If there's exactly one dot and exactly 3 digits after it, treat it as thousand separator
        parts = s.split('.')
        if len(parts) == 2 and len(parts[1]) == 3:
            normalized = ''.join(parts)  # remove dot
        else:
            # otherwise treat dot as decimal separator (e.g., '1999.99')
            normalized = s
    else:
        normalized = s

    # Final cleanup: remove any non digit / dot characters (shouldn't be any left)
    normalized = re.sub(r'[^\d\.]', '', normalized)

    # Convert to float
    try:
        return float(normalized) if normalized else 0.0
    except ValueError:
        return 0.0

def extract_detailed_property_info(soup, selectors):
    """
    Extract detailed property information using OTODOM_DETAIL_SELECTORS
    """
    details = {}

    try:
        # Extract title
        title = extract_with_fallback_selectors(soup, selectors.get('title', []))
        if title:
            details['title'] = title

        # Extract price
        price_text = extract_with_fallback_selectors(soup, selectors.get('price', []))
        if price_text:
            price_match = re.search(r'([\d\s]+)\s*zł', price_text)
            if price_match:
                details['price'] = int("".join(re.findall(r"\d+", price_match.group(1))))

        # Extract address
        address = extract_with_fallback_selectors(soup, selectors.get('address', []))
        if address:
            details['address'] = address

        # Extract property details from item grid
        details_container = None
        for selector in selectors.get('details_container', []):
            details_container = soup.select_one(selector)
            if details_container:
                break

        if details_container:
            # Get all property detail items
            item_grids = details_container.select('div[class*="css-1xw0jqp"]')

            for item in item_grids:
                # Extract label and value pairs
                label_elem = item.select_one('div[class*="css-1okys8k"]')
                if label_elem:
                    label = label_elem.get_text(strip=True)

                    # Get the value (usually the next sibling or in a specific container)
                    value_elem = item.find('div', class_=lambda x: x and 'css-axw7ok' in x)
                    if not value_elem:
                        # Try alternative value extraction
                        all_divs = item.find_all('div')
                        for div in all_divs:
                            if div != label_elem and div.get_text(strip=True):
                                value_elem = div
                                break

                    if value_elem:
                        value = value_elem.get_text(strip=True)
                        details[f'detail_{label.lower().replace(" ", "_")}'] = value

        # Extract features from accordion sections
        accordion_buttons = soup.select('button[class*="css-1u6lqhc"]')
        for button in accordion_buttons:
            section_name = button.get_text(strip=True)
            # Find corresponding content
            content_div = button.find_next_sibling('div')
            if content_div:
                features = []
                feature_spans = content_div.select('span[class*="css-axw7ok"]')
                for span in feature_spans:
                    feature_text = span.get_text(strip=True)
                    if feature_text:
                        features.append(feature_text)
                if features:
                    details[f'features_{section_name.lower().replace(" ", "_")}'] = features

    except Exception as e:
        logger.error(f"Error extracting detailed property info: {e}")

    return details

# DEBUG VERSION - Add this function to identify exactly what's failing

def extract_offer_details_debug(offer, offer_index=0):
    logger.info(f"🔍 DEBUGGING OFFER {offer_index + 1}")
    logger.info(f"Offer HTML preview: {str(offer)[:200]}...")

    logger.info("🏷️ Attempting title extraction...")
    # Step 2 logic starts here:

    # ​​ Try the reliable anchor first
    title_tag = offer.select_one('a[data-cy="listing-item-link"]')
    if title_tag:
        title = title_tag.get_text(strip=True)
        link = title_tag.get("href", None)
        if link and link.startswith("/"):
            link = f"https://www.otodom.pl{link}"
    else:
        # ​​ Fallback for promoted listings
        promo_tag = offer.select_one('[data-cy="search.listing.promoted.title"]')
        title = promo_tag.get_text(strip=True) if promo_tag else None
        link = None

    if not title:
        logger.warning(f"   ❌ TITLE NOT FOUND for offer {offer_index + 1}")
        return None

    logger.info(f"   ✅ Title found: {title}")

    logger.info("🔗 Attempting link extraction...")
    if not link:
        link_elem = offer.select_one('a[data-cy="listing-item-link"]')
        link = link_elem.get("href", None) if link_elem else None
        if link and link.startswith("/"):
            link = f"https://www.otodom.pl{link}"

    if not link:
        logger.warning(f"   ❌ LINK NOT FOUND for offer {offer_index + 1}")
        return None

    logger.info(f"   Link accepted: {link}")

    # Continue with price extraction, etc.
    ...

# UPDATED SCRAPE_OTODOM FUNCTION WITH DEBUG
def scrape_otodom_debug(html_content):
    global offers_df
    if offers_df is None:
        offers_df = pd.DataFrame()

    if not html_content or len(html_content) < 1000:
        logger.warning("HTML content too short, likely failed page load")
        return 0

    soup = BeautifulSoup(html_content, "html.parser")
    offers = soup.select(
        'article[data-cy="listing-item"], '
        'li[data-cy="listing-item"], '
        'div[data-cy="listing-item"], '
        'div[data-cy="search.listing.promoted"]'
    )

    if not offers:
        logger.warning("No offers found on page")
        return 0

    logger.info(f"🚀 Starting to process {len(offers)} offers with DEBUG extraction...")

    added_count = skipped_count = 0
    debug_limit = min(5, len(offers))

    for i, offer in enumerate(offers[:debug_limit]):
        try:
            logger.info("\n" + "=" * 60)
            logger.debug(f"DEBUGGING OFFER {i+1} - {offer.name} with class {offer.get('class')}")
            offer_data = extract_offer_details_debug(offer, i)

            if not offer_data:
                skipped_count += 1
                logger.warning(f"❌ Offer {i+1} skipped - extraction returned None")
                continue

            offer_df = pd.DataFrame([offer_data])
            offers_df = pd.concat([offers_df, offer_df], ignore_index=True)
            added_count += 1

            logger.info(f"✅ Successfully added offer {i+1}")

        except Exception as e:
            logger.error(f"❌ Error processing offer {i+1}: {e}")
            skipped_count += 1

    logger.info(f"\n🎯 DEBUG SUMMARY: Added={added_count}, Skipped={skipped_count} out of {debug_limit} tested")
    return added_count

def extract_price_from_text(price_text: str) -> float:
    """
    Extract a price from text, handling various Polish number formats.
    """
    if not price_text:
        return 0.0
    
    cleaned = re.sub(r'[^\d\s\.,]', '', price_text)
    cleaned = cleaned.replace(' ', '').replace('\u00A0', '')
    
    # Handle formats using both a dot and a comma
    if '.' in cleaned and ',' in cleaned:
        if cleaned.rfind(',') > cleaned.rfind('.'):
            cleaned = cleaned.replace('.', '').replace(',', '.')
        else:
            cleaned = cleaned.replace(',', '')
    elif ',' in cleaned:
        cleaned = cleaned.replace(',', '.')
    elif '.' in cleaned:
        parts = cleaned.split('.')
        if len(parts) == 2 and len(parts[1]) == 3:
            cleaned = cleaned.replace('.', '')
    
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def scrape_olx(html_content):
    """
    Improved OLX scraping function with more accurate extraction of:
    - Additional_value (additional fees)
    - Area (floor area)
    - Date (listing date)
    """
    global offers_df

    if not html_content or len(html_content) < 1000:
        logger.warning("HTML content too short")
        return 0

    soup = BeautifulSoup(html_content, "html.parser")

    # Try multiple container selectors (OLX updates their structure)
    containers = []

    # Primary: data-testid="l-card" (current OLX structure)
    containers = soup.find_all("div", {"data-testid": "l-card"})

    if not containers:
        # Fallback 1: CSS class based container
        containers = soup.find_all("div", class_="css-1apmciz")

    if not containers:
        # Fallback 2: Any div with ad card title
        ad_cards = soup.find_all("h4", {"data-testid": "ad-card-title"})
        containers = [card.find_parent("div", recursive=True) for card in ad_cards if card.find_parent("div")]
        containers = [c for c in containers if c]  # Remove None values

    if not containers:
        logger.warning("No OLX offer containers found")
        return 0

    logger.info(f"Found {len(containers)} potential OLX offers")
    
    added_count = 0
    skipped_count = 0
    
    for i, container in enumerate(containers):
        try:
            # === TITLE ===
            # Try multiple selectors for title
            title_elem = (
                container.select_one('h4[data-testid="ad-card-title"]') or
                container.select_one('h4.css-hzlye5') or
                container.select_one('h4') or
                container.select_one('h6')
            )
            if not title_elem:
                skipped_count += 1
                continue
            title = title_elem.get_text(strip=True)

            if "powiadomieni" in title.lower() or not title:
                skipped_count += 1
                continue

            # === LINK ===
            # Try multiple selectors for link
            link_elem = (
                container.select_one('a[href*="/d/oferta/"]') or
                container.select_one('a.css-1tqlkj0') or
                container.select_one('a[href*="/oferta/"]') or
                container.find('a', href=True)
            )
            if not link_elem or not link_elem.get('href'):
                skipped_count += 1
                continue

            link = link_elem.get('href')
            if not link.startswith('http'):
                if link.startswith('/'):
                    link = f"https://www.olx.pl{link}"
                else:
                    skipped_count += 1
                    continue

            # Skip Otodom cross-posts
            if "otodom.pl" in link:
                skipped_count += 1
                continue

            # === PRICE (BASE) ===
            price_elem = container.select_one('p[data-testid="ad-price"]')
            base_price = 0.0
            additional_price = 0.0

            if price_elem:
                price_text = price_elem.get_text(strip=True)
                base_price = extract_price_from_text(price_text)

            if base_price < 100:
                skipped_count += 1
                continue

            # === AREA (POWIERZCHNIA) ===
            area = 0.0
            # Try multiple selectors for area - it's often in a span with specific class
            area_elem = (
                container.select_one('span[data-testid="blueprint-card-param-icon"] + span') or
                container.select_one('span.css-h59g4b') or
                container.select_one('span[class*="css-"]')
            )

            if area_elem:
                area_text = area_elem.get_text(strip=True)
                match = re.search(r'(\d+(?:[.,]\d+)?)\s*m[²2]', area_text)
                if match:
                    area = float(match.group(1).replace(',', '.'))

            # If area not found in dedicated element, search in container text
            if area == 0.0:
                container_text = container.get_text()
                match = re.search(r'(\d+(?:[.,]\d+)?)\s*m[²2]', container_text)
                if match:
                    area = float(match.group(1).replace(',', '.'))
            
            # === ADDRESS + DATE ===
            # On OLX, the address and date live in the same element: p[data-testid="location-date"]
            location_date_elem = container.select_one('p[data-testid="location-date"]')
            address = "Unknown location"
            added_date = None
            
            if location_date_elem:
                location_date_text = location_date_elem.get_text(strip=True)
                
                added_date = parse_polish_date(location_date_text)
                
                if " - " in location_date_text:
                    address = location_date_text.split(" - ")[0].strip()
                else:
                    address = re.split(r'\s+\d+\s+', location_date_text)[0].strip()
                
                # Strip the time from the address (e.g. "at 15:04")
                address = re.sub(r'\s+o\s+\d{1,2}:\d{2}', '', address)
            
            # === BUILD OFFER DICT ===
            offer_dict = {
                "Name": title,
                "Base_value": base_price,
                "Additional_value": additional_price,
                "Full_value": base_price,
                "Area": area,
                "Address": address,
                "Link": link,
                "Added_Date": added_date,
                "Source": "OLX"
            }
            
            if is_duplicate_offer(title, base_price, link):
                skipped_count += 1
                continue
            
            offer_row_df = pd.DataFrame([offer_dict])
            offers_df = pd.concat([offers_df, offer_row_df], ignore_index=True)
            
            added_count += 1
            
            logger.debug(f"✅ OLX offer {i+1}: {title[:50]}... | {base_price} zł | {area} m² | {address}")
        
        except Exception as e:
            logger.error(f"❌ Error processing OLX offer {i+1}: {e}")
            skipped_count += 1
            continue
    
    logger.info(f"📊 OLX scraping complete: Added={added_count}, Skipped={skipped_count}")
    return added_count

def extract_otodom_details(page, index, current_num, total_num, is_retry=False):
    """Extract detailed information from a single Otodom offer page"""
    global offers_df

    try:
        offer = offers_df.loc[index]
        url = offer["Link"]

        if not url.startswith("http"):
            url = "https://" + url

        retry_text = "[RETRY] " if is_retry else ""
        logger.info(f"🏠 {retry_text}Processing Otodom offer {current_num}/{total_num} (index {index}): {url}")

        # Add random delay with longer delays for retries
        delay_range = (2.0, 5.0) if is_retry else (0.1, 2.0)
        delay = random.uniform(*delay_range)
        logger.info(f"🕰️ Waiting {delay:.1f}s before loading page...")
        time.sleep(delay)

        page.goto(url, timeout=60000, wait_until="load")
        try:
            html = page.inner_html("body")
        except Exception as e:
            logger.error(f"Page.inner_html failed: {e}")
            return None

        html_filepath = save_html_to_file(html, index, url)
        if html_filepath:
            offers_df.at[index, "HTML_File_Path"] = html_filepath

        soup = BeautifulSoup(html, "html.parser")

        # Check if page loaded properly - specific 404 detection
        html_lower = html.lower()

        # Check page title for 404 errors (must be exact match patterns, not just containing "404")
        title_tag = soup.find("title")
        title_text = title_tag.get_text().lower() if title_tag else ""
        title_indicates_404 = any([
            title_text.startswith("404"),
            title_text.startswith("błąd 404"),
            title_text.startswith("error 404"),
            "- 404" in title_text,
            "| 404" in title_text,
        ])

        # Check for specific 404 error page patterns in content
        content_indicates_404 = any([
            "błąd 404" in html_lower,
            "error 404" in html_lower,
            "strona nie istnieje" in html_lower,
            "ogłoszenie nie istnieje" in html_lower,
            "oferta nie istnieje" in html_lower,
            "nie znaleziono strony" in html_lower,
            "ta oferta już nie istnieje" in html_lower,
        ])

        # Only mark as 404 if we have error indicators AND can't find key offer elements
        has_offer_title = soup.select_one('[data-sentry-element="StyledAnchor"]') or soup.find("h1")
        has_price = soup.find("strong", class_=re.compile(r"e1")) or soup.find(string=re.compile(r"\d+\s*zł"))

        is_404_page = (title_indicates_404 or content_indicates_404) and not (has_offer_title and has_price)

        if is_404_page:
            logger.warning(f"⚠️ Otodom page {index} returned 404 or not found")
            offers_df.at[index, "Scraping_Error"] = "Page not found (404)"
            return False
        
        if len(html) < 2000:
            logger.warning(f"⚠️ Otodom page {index} has insufficient content ({len(html)} chars)")
            offers_df.at[index, "Scraping_Error"] = "Insufficient page content"
            return False

        # Dictionary to store all extracted values for printing
        extracted_values = {}

        # Extract title using selectors
        title_element = find_element_by_selectors(soup, OTODOM_DETAIL_SELECTORS["title"])
        if title_element:
            title_text = title_element.get_text().strip()
            offers_df.at[index, "Title"] = title_text
            extracted_values["Title"] = title_text

        # Extract price using selectors
        price_element = find_element_by_selectors(soup, OTODOM_DETAIL_SELECTORS["price"])
        if price_element:
            price_text = price_element.get_text().strip()
            offers_df.at[index, "Price_Text"] = price_text
            extracted_values["Price_Text"] = price_text

        # Extract address using selectors
        address_element = find_element_by_selectors(soup, OTODOM_DETAIL_SELECTORS["address"])
        if address_element:
            address_text = address_element.get_text().strip()
            offers_df.at[index, "Address"] = address_text
            extracted_values["Address"] = address_text

        # Extract phone number with click interaction
        logger.info(f"🔍 Attempting to extract phone number for offer {index}")
        phone_number = extract_phone_number_with_click(page)
        if phone_number and phone_number not in ["Phone button not found", "Phone extraction failed",
                                                 "Phone button clicked but number not found"]:
            offers_df.at[index, "Phone_Number"] = phone_number
            extracted_values["Phone_Number"] = phone_number
            logger.info(f"📞 Successfully extracted phone number for offer {index}: {phone_number}")
        else:
            offers_df.at[index, "Phone_Number"] = phone_number
            extracted_values["Phone_Number"] = phone_number
            logger.warning(f"📞 Could not extract phone number for offer {index}: {phone_number}")


        # Extract description using existing logic (keeping the working selector)
        opis_tag = soup.select_one('[data-sentry-element="DescriptionWrapper"]')
        if opis_tag:
            all_details = opis_tag.get_text().strip()
            offers_df.at[index, "Description"] = all_details
            extracted_values["Description"] = all_details[:100] + "..." if len(all_details) > 100 else all_details
            logger.info(f"Description added {len(all_details)} characters")

        # Extract main property details from ItemGridContainer elements
        item_grids = find_elements_by_selectors(soup, OTODOM_DETAIL_SELECTORS["item_grid"])

        for grid in item_grids:
            # Each grid should have exactly 2 divs: label and value
            items = grid.find_all('div', class_=lambda x: x and 'css-1okys8k e1gd421g2' in x)

            if len(items) >= 2:
                label = items[0].get_text().strip().rstrip(':')
                value_div = items[1]

                # Check if value contains spans with features (like equipment lists)
                feature_spans = value_div.find_all('span', class_=lambda x: x and 'css-axw7ok' in x)

                if feature_spans:
                    # Extract individual features from spans
                    features = []
                    for span in feature_spans:
                        feature_text = span.get_text().strip()
                        if feature_text:
                            features.append(feature_text)
                    value = ', '.join(features)
                else:
                    # Regular text value
                    value = value_div.get_text().strip()

                if label and value:
                    # Clean up label for column name
                    column_name = label.replace(':', '').strip()
                    offers_df.at[index, column_name] = value
                    extracted_values[column_name] = value

        logger.info("Main property details extracted from item grids")

        # Extract accordion sections (Building materials, Equipment, etc.)
        accordion_contents = find_elements_by_selectors(soup, OTODOM_DETAIL_SELECTORS["accordion_content"])

        for content in accordion_contents:
            # Find the header to get section name
            accordion_container = content.find_parent()
            if accordion_container:
                header = accordion_container.find('p', class_='css-10a5kuz')
                section_name = header.get_text().strip() if header else "Unknown_Section"
            else:
                section_name = "Unknown_Section"

            # Extract all item grids within this accordion section
            section_grids = content.find_all('div', class_=lambda x: x and 'css-1xw0jqp e1gd421g1' in x)

            for grid in section_grids:
                items = grid.find_all('div', class_=lambda x: x and 'css-1okys8k e1gd421g2' in x)

                if len(items) >= 2:
                    label = items[0].get_text().strip().rstrip(':')
                    value_div = items[1]

                    # Check if value contains spans with features
                    feature_spans = value_div.find_all('span', class_=lambda x: x and 'css-axw7ok' in x)

                    if feature_spans:
                        # Extract individual features from spans
                        features = []
                        for span in feature_spans:
                            feature_text = span.get_text().strip()
                            if feature_text:
                                features.append(feature_text)
                        value = ', '.join(features)
                    else:
                        # Regular text value
                        value = value_div.get_text().strip()

                    if label and value:
                        # Prefix with section name for clarity
                        column_name = f"{section_name}_{label}".replace(':', '').strip()
                        offers_df.at[index, column_name] = value
                        extracted_values[column_name] = value

        logger.info("Accordion section details extracted")

        # Extract seller information using selectors where possible
        seller_element = find_element_by_selectors(soup, OTODOM_DETAIL_SELECTORS["seller"])
        if seller_element:
            seller_text = seller_element.get_text().strip()
            offers_df.at[index, "Seller_Info"] = seller_text
            extracted_values["Seller_Info"] = seller_text

        # Keep existing agent extraction as fallback
        try:
            # More reliable approach using data attributes
            seller_container = soup.find('div', {'data-sentry-element': 'CompanyInfoContainer'})

            if seller_container:
                # Agent name
                agent_name_element = seller_container.find('p', {'data-sentry-element': 'SellerName'})
                agent_name = agent_name_element.get_text() if agent_name_element else "Not found"

                # Agency link
                agency_link_element = seller_container.find('a')  # First link in container
                agency_name = agency_link_element.get_text() if agency_link_element else "Not found"
                agency_url = agency_link_element[
                    'href'] if agency_link_element and 'href' in agency_link_element.attrs else "Not found"
            else:
                agent_name = "Not found"
                agency_name = "Not found"
                agency_url = "Not found"

            offers_df.at[index, "Agent_Name"] = agent_name
            offers_df.at[index, "Agency_Name"] = agency_name
            offers_df.at[index, "Agency_URL"] = agency_url

            extracted_values["Agent_Name"] = agent_name
            extracted_values["Agency_Name"] = agency_name
            extracted_values["Agency_URL"] = agency_url

        except Exception as e:
            logger.warning(f"Error extracting agent information: {e}")

        logger.info("Agent information extracted")

        # Extract property metrics (keeping existing logic as fallback)
        try:
            boxes = soup.find("div", class_="css-8mnxk5 esen0m90")
            if boxes:
                metrics = boxes.find_all("div", class_="esen0m91")
                for i, metric in enumerate(metrics):
                    metric_name_tag = metric.find('p', class_="css-1airkmu")
                    metric_value_tag = metric.find('div', class_="css-1airkmu")

                    if metric_value_tag:
                        metric_name = metric_name_tag.get_text().strip()
                        metric_value = metric_value_tag.get_text().strip()
                        column_name = f"Metric_{metric_name}"
                        offers_df.at[index, column_name] = metric_value
                        extracted_values[column_name] = metric_value
                        logger.debug(f'{metric_name}: {metric_value}')
            logger.info("Property metrics extracted")
        except Exception as e:
            logger.warning(f"Error extracting property metrics: {e}")

        # Extract main image
        try:
            image_url = extract_otodom_image_url(soup)
            if image_url:
                offers_df.at[index, "Main_Image_URL"] = image_url
                extracted_values["Main_Image_URL"] = image_url
                logger.info(f"Main image URL extracted: {image_url[:60]}...")
        except Exception as e:
            logger.warning(f"Error extracting main image: {e}")

        # Log all extracted values
        logger.debug(f"🏠 OTODOM OFFER {index} - ALL EXTRACTED VALUES (URL: {url})")
        if extracted_values:
            for key, value in extracted_values.items():
                logger.debug(f"  {key:35} | {str(value)[:60]}")
        logger.debug(f"Total fields extracted: {len(extracted_values)}")

        # Validate detailed data extraction
        core_fields = ['Title', 'Price_Text', 'Address', 'Description']
        extracted_core_fields = sum(1 for field in core_fields if
                                    field in extracted_values and extracted_values[field] not in ["", "Not found",
                                                                                                  "No Description"])

        if extracted_core_fields == 0:
            logger.warning(f"⚠️ Otodom offer {index}: No core data was successfully extracted")
            offers_df.at[index, "Detail_Extraction_Status"] = "Failed - No core data extracted"
            return False
        else:
            offers_df.at[
                index, "Detail_Extraction_Status"] = f"Success - {len(extracted_values)} total fields extracted"
            logger.info(f"✅ Successfully extracted {len(extracted_values)} fields from Otodom offer {index}")
            return True

    except Exception as e:
        logger.error(f"❌ Failed to extract Otodom offer page {index}: {e}")
        offers_df.at[index, "Scraping_Error"] = str(e)
        logger.error(f"  URL: {url if 'url' in locals() else 'Unknown'}, Error: {str(e)}")
        return False

def extract_phone_number_with_click(page, contact_container_selector=None):
    """
    Helper function to click phone-related buttons and extract phone number
    This uses Playwright for web automation - SYNC VERSION
    """
    # Enhanced regex patterns for Polish phone numbers
    phone_patterns = [
        r'(\+48\s?\d{3}\s?\d{3}\s?\d{3})',   # +48 123 456 789
        r'(\d{3}\s?\d{3}\s?\d{3})',          # 123 456 789
        r'(\d{9})',                          # 123456789
        r'(\d{2}\s?\d{3}\s?\d{2}\s?\d{2})',  # 12 345 67 89
        r'(\d{3}-\d{3}-\d{3})',              # 123-456-789
        r'(\+48\s?\d{9})',                   # +48 123456789
    ]

    # Multiple selectors to try for phone buttons
    phone_button_selectors = [
        # Primary OLX selectors
        'button[data-cy="phone-number.show-full-number-button"]',

        # Alternative phone contact buttons
        'button[data-cy="ad-contact-phone"]',
        'button[data-testid="ad-contact-phone"]',

        # Generic phone buttons
        'button:has-text("Pokaż numer")',
        'button:has-text("Show number")',
        'button:has-text("Zadzwoń")',
        'button:has-text("Telefon")',

        # CSS class based selectors
        'button.css-1k93fmv:has-text("Zadzwoń")',
        'button[data-button-variant="secondary"]:has-text("Zadzwoń")',

        # Contact section buttons
        'div[data-cy="contact-seller"] button',
        'div[data-testid="contact-seller"] button',

        # Any button with phone-related attributes
        'button[data-cy*="phone"]',
        'button[data-testid*="phone"]',
        'button[class*="phone"]',
    ]

    # Selectors for phone number display after clicking
    phone_display_selectors = [
        'a[data-cy="phone-number.number-button"]',
        'span[data-cy="phone-number.number"]',
        'a[href^="tel:"]',
        'span[class*="phone"]',
        'div[data-cy="phone-number"] span',
        'div[data-cy="phone-number"] a',
        # After clicking contact button, phone might appear in different places
        'span:has-text("+")',
        'a:has-text("+")',
    ]

    try:
        logger.info("🔍 Starting comprehensive phone number extraction...")

        # Dismiss any cookie consent overlay that might block clicks
        try:
            page.evaluate("document.getElementById('onetrust-consent-sdk')?.remove()")
        except Exception:
            pass

        # Step 1: Try to find and click any phone-related button
        button_clicked = False
        button_found = None

        for selector in phone_button_selectors:
            try:
                button = page.locator(selector).first
                if button.count() > 0:
                    logger.info(f"📞 Found phone button with selector: {selector}")
                    button_found = selector

                    # Try to click the button
                    try:
                        button.wait_for(state="visible", timeout=3000)
                        button.click(timeout=5000)
                        button_clicked = True
                        logger.info(f"✅ Successfully clicked phone button: {selector}")

                        # Wait for phone number to potentially load
                        page.wait_for_timeout(2000)
                        break

                    except Exception as click_error:
                        logger.warning(f"⚠️ Could not click button {selector}: {click_error}")
                        continue

            except Exception as find_error:
                logger.debug(f"Button selector failed {selector}: {find_error}")
                continue

        if not button_clicked and not button_found:
            logger.info("ℹ️ No phone buttons found, checking if number is already visible...")

        # Step 2: Try to find phone number in various locations
        phone_found = None

        for selector in phone_display_selectors:
            try:
                phone_element = page.locator(selector)
                if phone_element.count() > 0:
                    phone_text = phone_element.first.text_content()

                    if phone_text and len(phone_text.strip()) > 0:
                        logger.info(f"📱 Found potential phone text with selector {selector}: '{phone_text}'")

                        # Try to extract phone number using patterns
                        for pattern in phone_patterns:
                            phone_match = re.search(pattern, phone_text)
                            if phone_match:
                                extracted_phone = phone_match.group(1).strip()
                                logger.info(f"✅ Successfully extracted phone number: {extracted_phone}")
                                return extracted_phone

                        # Store potential phone text even if pattern didn't match
                        if not phone_found:
                            phone_found = phone_text.strip()

            except Exception as display_error:
                logger.debug(f"Display selector failed {selector}: {display_error}")
                continue

        # Step 3: Search entire page content for phone patterns as last resort
        try:
            page_content = page.content()
            logger.info("🔍 Searching entire page content for phone patterns...")

            for pattern in phone_patterns:
                phone_matches = re.findall(pattern, page_content)
                if phone_matches:
                    # Filter out common false positives
                    valid_phones = []
                    for match in phone_matches:
                        # Skip common false positives
                        if not any(
                                false_positive in match for false_positive in ['000000000', '111111111', '123456789']):
                            valid_phones.append(match)

                    if valid_phones:
                        extracted_phone = valid_phones[0].strip()
                        logger.info(f"✅ Found phone in page content: {extracted_phone}")
                        return extracted_phone

        except Exception as content_error:
            logger.error(f"Error searching page content: {content_error}")

        # Return appropriate message based on what was found
        if button_clicked and phone_found:
            return f"Button clicked, found text but invalid format: {phone_found[:50]}"
        elif button_clicked:
            return "Phone button clicked but number not found"
        elif button_found:
            return f"Phone button found ({button_found}) but could not click"
        elif phone_found:
            return f"Phone text found but invalid format: {phone_found[:50]}"
        else:
            return "No phone button or number found"

    except Exception as e:
        logger.error(f"❌ Error during phone extraction process: {e}")
        return f"Phone extraction error: {str(e)}"

def save_html_to_file(html_content, index, url, export_folder="exports/html"):
    """
    Save HTML content to a file in the export folder AND debug handler
    """
    try:
        # Create export folder if it doesn't exist
        if not os.path.exists(export_folder):
            os.makedirs(export_folder)

        # Create a safe filename from the URL
        url_part = url.split('/')[-1] if url.split('/')[-1] else f"offer_{index}"
        safe_filename = re.sub(r'[^\w\-_\.]', '_', url_part)
        if not safe_filename.endswith('.html'):
            safe_filename += '.html'

        # Add index prefix for uniqueness and limit filename length
        filename = f"olx_{index}_{safe_filename}"
        if len(filename) > 200:
            filename = f"olx_{index}_offer.html"

        filepath = os.path.join(export_folder, filename)

        # Save HTML content to exports folder
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html_content)

        logger.debug(f"💾 Saved HTML for offer {index} to {filepath} ({len(html_content)} bytes)")
        
        # Also save to debug handler if available
        if debug_handler:
            debug_path = debug_handler.save_html_for_issue(
                html_content=html_content,
                issue_type="offer_snapshot",
                offer_index=index,
                url=url,
                metadata={"filename": filename, "size_bytes": len(html_content)}
            )
            if debug_path:
                logger.debug(f"🔍 Debug copy saved to: {debug_path}")
        
        return filepath

    except Exception as e:
        logger.error(f"Failed to save HTML for offer {index}: {e}")
        logger.error(f"URL: {url}")
        logger.error(f"Export folder: {export_folder}")
        return None


def extract_olx_details(page, index, current_num, total_num, is_retry=False):
    """Extract detailed information from a single OLX offer page"""

    global offers_df

    try:
        offer = offers_df.loc[index]
        url = offer["Link"]

        if not url.startswith("http"):
            url = "https://" + url

        retry_text = "[RETRY] " if is_retry else ""
        logger.info(f"📱 {retry_text}Processing OLX offer {current_num}/{total_num} (index {index}): {url}")

        # Log extraction attempt with selectors being tested
        log_extraction_attempt(
            logger,
            offer_index=index,
            source="OLX",
            url=url,
            selectors_to_test={
                "title": OLX_SELECTORS.get('title', [])[:3],
                "price": OLX_SELECTORS.get('price', [])[:3],
                "area": OLX_SELECTORS.get('area', [])[:3],
                "location": OLX_SELECTORS.get('address', [])[:3],
            }
        )

        # Add random delay with longer delays for retries
        delay_range = (2.0, 5.0) if is_retry else (0.1, 1.9)
        delay = random.uniform(*delay_range)
        logger.debug(f"🕰️ Waiting {delay:.1f}s before loading page...")
        time.sleep(delay)

        page.goto(url, timeout=60000, wait_until="load")
        html = page.inner_html("body")
        
        page_state = f"HTML size: {len(html)} bytes, Contains 404: {'404' in html}, Line count: {len(html.split(chr(10)))}"
        logger.debug(f"📄 Page loaded - {page_state}")

        # Save full HTML to export folder
        html_filepath = save_html_to_file(html, index, url)
        if html_filepath:
            offers_df.at[index, "HTML_File_Path"] = html_filepath

        soup = BeautifulSoup(html, "html.parser")

        # Check if page loaded properly with better diagnostics
        # More specific 404 detection - look for actual error page indicators
        is_404_page = False
        html_lower = html.lower()

        # Check page title for 404 errors (must be exact match patterns, not just containing "404")
        title_tag = soup.find("title")
        title_text = title_tag.get_text().lower() if title_tag else ""
        title_indicates_404 = any([
            title_text.startswith("404"),
            title_text.startswith("błąd 404"),
            title_text.startswith("error 404"),
            "- 404" in title_text,
            "| 404" in title_text,
        ])

        # Check for specific 404 error page patterns in content
        content_indicates_404 = any([
            "błąd 404" in html_lower,
            "error 404" in html_lower,
            "strona nie istnieje" in html_lower,
            "ogłoszenie nie istnieje" in html_lower,
            "ogłoszenie zostało usunięte" in html_lower,
            "to ogłoszenie nie jest już dostępne" in html_lower,
            "nie znaleziono strony" in html_lower,
            "ta oferta już nie istnieje" in html_lower,
        ])

        # Only mark as 404 if title indicates error OR content has error messages
        # AND we can't find key offer elements (to avoid false positives)
        has_offer_title = soup.find("h4", {"data-cy": "ad_title"}) or soup.find("h4", class_=re.compile(r"css-"))
        has_price = soup.find("h3", {"data-testid": "ad-price-container"}) or soup.find(string=re.compile(r"\d+\s*zł"))

        if (title_indicates_404 or content_indicates_404) and not (has_offer_title and has_price):
            is_404_page = True

        if is_404_page:
            # Check if this is a retry - if so, it's a persistent 404 (listing expired/removed)
            if is_retry:
                logger.warning(f"⚠️ OLX page {index} returned 404 on RETRY - listing likely expired/removed")
                debug_handler.save_html_for_issue(html, "olx_404_persistent", index, url, page_state=page_state)
                offers_df.at[index, "Scraping_Error"] = "Page returned 404 (persistent - listing removed)"
            else:
                logger.warning(f"⚠️ OLX page {index} returned 404 on first attempt - will retry")
                debug_handler.save_html_for_issue(html, "olx_404_error", index, url, page_state=page_state)
                offers_df.at[index, "Scraping_Error"] = "Page returned 404 (temporary)"
            return False
            
        if len(html) < 1000:
            logger.warning(f"⚠️ OLX page {index} has insufficient content ({len(html)} bytes)")
            debug_handler.save_html_for_issue(html, "olx_insufficient_content", index, url, page_state=page_state)
            offers_df.at[index, "Scraping_Error"] = "Page content too small"
            return False

        # Dictionary to store all extracted values for printing
        extracted_values = {}
        missing_fields = []

        # Extract title using selectors
        title_element = find_element_by_selectors(soup, OLX_SELECTORS['title'])
        if title_element:
            title_text = title_element.get_text().strip()
            offers_df.at[index, "Title"] = title_text
            extracted_values["Title"] = title_text
            logger.debug(f"✅ Title extracted: {title_text[:60]}")
        else:
            missing_fields.append("Title")
            logger.debug(f"❌ Title not found")

        # Extract price using selectors
        price_element = find_element_by_selectors(soup, OLX_SELECTORS['price'])
        if price_element:
            price_text = price_element.get_text().strip()
            offers_df.at[index, "Price_Detail"] = price_text
            extracted_values["Price_Detail"] = price_text
            logger.debug(f"✅ Price extracted: {price_text}")
        else:
            missing_fields.append("Price")
            logger.debug(f"❌ Price not found")

        # Extract area using selectors
        area_element = find_element_by_selectors(soup, OLX_SELECTORS['area'])
        if area_element:
            area_text = area_element.get_text().strip()
            offers_df.at[index, "Area_Detail"] = area_text
            extracted_values["Area_Detail"] = area_text
            logger.debug(f"✅ Area extracted: {area_text}")
        else:
            missing_fields.append("Area")
            logger.debug(f"❌ Area not found")

        # Extract detailed data using existing OLX extraction logic
        # Try multiple selectors for description - CSS classes may change
        opis_tag = None
        for desc_selector in [
            ("div", {"data-cy": "ad_description"}),
            ("div", {"data-testid": "ad_description"}),
            ("div", {"class": "css-19duwlz"}),
            ("div", {"class": re.compile(r"css-\w+")}),  # Fallback to any css class div with description-like content
        ]:
            opis_tag = soup.find(desc_selector[0], desc_selector[1])
            if opis_tag and len(opis_tag.get_text(strip=True)) > 20:
                break

        opis = opis_tag.get_text(strip=True) if opis_tag else "No Description"
        if opis != "No Description" and len(opis) > 10:
            extracted_values["Description"] = opis[:100]
            offers_df.at[index, "Description"] = opis
            logger.debug(f"✅ Description extracted ({len(opis)} chars)")
        else:
            missing_fields.append("Description")
            logger.debug(f"❌ Description not found")

        # Extract location using selectors first, then fallback
        location_element = find_element_by_selectors(soup, OLX_SELECTORS['address'])
        if location_element:
            location = location_element.get_text().strip()
            logger.debug(f"✅ Location extracted (via selector): {location}")
        else:
            # Fallback to multiple possible location containers
            location = "No Location"
            for loc_selector in [
                ("div", {"class": "css-1dp6pbg"}),
                ("p", {"data-testid": "location-date"}),
                ("nav", {"role": "navigation"}),  # breadcrumbs contain location
            ]:
                location_container = soup.find(loc_selector[0], loc_selector[1])
                if location_container:
                    # Try to find location text
                    loc_text = location_container.get_text(strip=True)
                    if "Warszawa" in loc_text or "mazowieckie" in loc_text.lower():
                        # Extract just the location part
                        if " - " in loc_text:
                            location = loc_text.split(" - ")[0].strip()
                        else:
                            location = loc_text[:100]
                        logger.debug(f"✅ Location extracted (via fallback): {location}")
                        break

            if location == "No Location":
                # Try breadcrumbs as last resort
                breadcrumbs = soup.find("ol", {"data-testid": "breadcrumbs"})
                if breadcrumbs:
                    items = breadcrumbs.find_all("li")
                    if len(items) >= 6:
                        location = items[-1].get_text(strip=True).replace("Wynajem - ", "")
                        logger.debug(f"✅ Location extracted (via breadcrumbs): {location}")

            if location == "No Location":
                missing_fields.append("Location")
                logger.debug(f"❌ Location not found")

        trader_tag = soup.find("div", {"data-testid": "seller_card"})
        if not trader_tag:
            trader_tag = soup.select_one("p[data-testid='trader-title']")
            if not trader_tag:
                seller_card = soup.find("div", {"data-cy": "seller_card"})
                if seller_card:
                    trader_tag = seller_card.find("p", {"data-testid": "trader-title"})

        trader_info = trader_tag.text.strip() if trader_tag else "No Seller Info"

        offers_df.at[index, "Description"] = opis
        offers_df.at[index, "Seller"] = trader_info
        offers_df.at[index, "Location"] = location

        # Store main extracted values
        extracted_values["Description"] = opis[:100] + "..." if len(opis) > 100 else opis
        extracted_values["Seller"] = trader_info
        extracted_values["Location"] = location

        # Extract data from the ad-parameters-container (primary method for OLX)
        data_dict = {}
        ad_params_container = soup.find("div", {"data-testid": "ad-parameters-container"})

        if ad_params_container:
            # Extract all <p> elements in the parameters container
            for p in ad_params_container.find_all("p"):
                text = p.get_text(strip=True)
                if not text:
                    continue
                if ":" in text:
                    key, value = text.split(":", 1)
                    data_dict[key.strip()] = value.strip()
                else:
                    # Fields like "Prywatne" (no colon, means it's the "Typ oferty")
                    if text not in ["Prywatne", "Firmowe"]:
                        data_dict["Info"] = text
                    else:
                        data_dict["Typ oferty"] = text
            logger.debug(f"✅ Extracted {len(data_dict)} parameters from ad-parameters-container")
        else:
            # Fallback to old container (css-41yf00)
            container = soup.find("div", class_="css-41yf00")
            if container:
                for p in container.find_all("p"):
                    text = p.get_text(strip=True)
                    if ":" in text:
                        key, value = text.split(":", 1)
                        data_dict[key.strip()] = value.strip()
                    else:
                        data_dict["Type"] = text
                logger.debug(f"✅ Extracted {len(data_dict)} parameters from fallback container")
            else:
                logger.debug(f"⚠️ No parameters container found")

        for key, value in data_dict.items():
            offers_df.at[index, key] = value
            extracted_values[key] = value

        img_tag = soup.find("img", {"data-testid": "swiper-image"})
        image_url = img_tag["src"] if img_tag else "No Image Found"
        offers_df.at[index, "Photo_URL"] = image_url
        extracted_values["Photo_URL"] = image_url

        # Log all extracted values
        logger.debug(f"📱 OLX OFFER {index} - ALL EXTRACTED VALUES (URL: {url})")
        if extracted_values:
            for key, value in extracted_values.items():
                logger.debug(f"  {key:30} | {str(value)[:80]}")
        logger.debug(f"Total fields extracted: {len(extracted_values)}")

        # Validate that we actually extracted some meaningful data
        total_fields_extracted = len(extracted_values)
        if total_fields_extracted == 0:
            logger.warning(f"⚠️ OLX offer {index} - No meaningful data extracted from page")
            debug_handler.save_html_for_issue(html, "olx_no_data_extracted", index, url, page_state=page_state)
            offers_df.at[index, "Scraping_Error"] = "No data extracted from page"
            offers_df.at[index, "Detail_Extraction_Status"] = "Failed - No data extracted"
            log_extraction_result(logger, index, "OLX", {}, list(missing_fields), False, "No data extracted")
            return False
        else:
            logger.info(f"✅ Successfully extracted {total_fields_extracted} fields from OLX offer {index}")
            offers_df.at[index, "Detail_Extraction_Status"] = f"Success - {total_fields_extracted} fields extracted"
            log_extraction_result(logger, index, "OLX", extracted_values, missing_fields, True)
            return True

    except Exception as e:
        logger.error(f"❌ Failed to extract OLX offer page {index}: {e}")
        offers_df.at[index, "Scraping_Error"] = str(e)
        if 'html' in locals():
            debug_handler.save_html_for_issue(html, "olx_extraction_exception", index, url if 'url' in locals() else "unknown", metadata={"exception": str(e)})
        log_extraction_result(logger, index, "OLX", {}, [], False, f"Exception: {str(e)}")
        return False


def save_html_to_file(html_content, index, url, export_folder="exports/html", file_prefix="offer"):
    """
    Improved HTML-saving function with better error handling and folder structure.

    Args:
        html_content: HTML content to save
        index: Offer index
        url: Source URL
        export_folder: Main export folder
        file_prefix: Filename prefix (e.g. "olx", "otodom", "listing")

    Returns:
        str: Path to the saved file, or None on error
    """
    if not html_content or len(html_content) < 100:
        logger.warning(f"HTML content too short for offer {index}: {len(html_content) if html_content else 0} bytes")
        return None

    try:
        # Build the dated folder structure
        today = dt.datetime.now().strftime("%Y-%m-%d")
        full_export_path = Path(export_folder) / today
        full_export_path.mkdir(parents=True, exist_ok=True)

        # Determine the source from the URL
        if "otodom.pl" in url:
            source = "otodom"
        elif "olx.pl" in url:
            source = "olx"
        else:
            source = "unknown"

        # Build a safe filename from the URL
        url_part = url.split('/')[-1] if url.split('/')[-1] else f"offer_{index}"
        # Strip all unsafe characters
        safe_filename = re.sub(r'[^\w\-_\.]', '_', url_part)

        # Cap the filename length
        if len(safe_filename) > 150:
            # Try to pull an ID out of the URL
            id_match = re.search(r'(?:ID)?(\d{6,})', url)
            if id_match:
                safe_filename = f"id_{id_match.group(1)}"
            else:
                safe_filename = f"offer_{index}"

        # Add the .html extension if missing
        if not safe_filename.endswith('.html'):
            safe_filename += '.html'

        # Full filename with source prefix and index
        timestamp = dt.datetime.now().strftime("%H%M%S")
        filename = f"{source}_{index:04d}_{timestamp}_{safe_filename}"

        # Full path
        filepath = full_export_path / filename

        # Save the HTML with explicit encoding handling
        with open(filepath, 'w', encoding='utf-8', errors='replace') as f:
            f.write(html_content)

        file_size_kb = len(html_content) / 1024
        logger.info(f"💾 Saved HTML for {source} offer {index}: {filepath.name} ({file_size_kb:.1f} KB)")

        # Also save a copy to the debug handler if available
        try:
            from logging_config import debug_handler
            if debug_handler:
                debug_metadata = {
                    "filename": filename,
                    "size_bytes": len(html_content),
                    "source": source,
                    "timestamp": timestamp
                }
                debug_path = debug_handler.save_html_for_issue(
                    html_content=html_content,
                    issue_type="offer_snapshot",
                    offer_index=index,
                    url=url,
                    metadata=debug_metadata
                )
                if debug_path:
                    logger.debug(f"🔍 Debug copy saved to: {debug_path}")
        except ImportError:
            pass  # Debug handler unavailable
        
        return str(filepath)
        
    except PermissionError as e:
        logger.error(f"❌ Permission denied saving HTML for offer {index}: {e}")
        logger.error(f"   Export folder: {export_folder}")
        return None
    except OSError as e:
        logger.error(f"❌ OS error saving HTML for offer {index}: {e}")
        logger.error(f"   Filepath: {filepath if 'filepath' in locals() else 'unknown'}")
        return None
    except Exception as e:
        logger.error(f"❌ Unexpected error saving HTML for offer {index}: {e}")
        logger.error(f"   URL: {url}")
        logger.error(f"   Export folder: {export_folder}")
        import traceback
        logger.error(traceback.format_exc())
        return None


def save_listing_page_html(html_content, page_num, source="unknown", export_folder="exports/html"):
    """
    Saves the HTML of a listing (search results) page, not a single offer.

    Args:
        html_content: HTML content of the listing page
        page_num: Page number
        source: Source ("olx" or "otodom")
        export_folder: Export folder

    Returns:
        str: Path to the saved file, or None
    """
    if not html_content or len(html_content) < 1000:
        logger.warning(f"Listing HTML too short for {source} page {page_num}: {len(html_content) if html_content else 0} bytes")
        return None

    try:
        # Folder structure: exports/html/YYYY-MM-DD/listings/
        today = dt.datetime.now().strftime("%Y-%m-%d")
        listings_path = Path(export_folder) / today / "listings"
        listings_path.mkdir(parents=True, exist_ok=True)

        timestamp = dt.datetime.now().strftime("%H%M%S")
        filename = f"{source}_listing_page{page_num:03d}_{timestamp}.html"
        filepath = listings_path / filename
        
        with open(filepath, 'w', encoding='utf-8', errors='replace') as f:
            f.write(html_content)
        
        file_size_kb = len(html_content) / 1024
        logger.info(f"📄 Saved {source} listing page {page_num}: {filepath.name} ({file_size_kb:.1f} KB)")
        
        return str(filepath)
        
    except Exception as e:
        logger.error(f"❌ Failed to save listing HTML for {source} page {page_num}: {e}")
        return None


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

            # Extract data based on source
            if source == "olx":
                extracted = _extract_olx_from_soup(soup, index)
            elif source == "otodom":
                extracted = _extract_otodom_from_soup(soup, index)
            else:
                logger.debug(f"Unknown source: {source}")
                skipped_count += 1
                progress.update(item_start)
                continue

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
    logger.info(f"📊 RE-EXTRACTION SUMMARY")
    logger.info(f"{'='*60}")
    logger.info(f"✅ Successfully updated: {reextracted_count} offers")
    logger.info(f"⏭️ Skipped (already had data): {skipped_count} offers")
    logger.info(f"❌ Failed to extract: {failed_count} offers")
    logger.info(f"\n📋 Fields updated (top 20):")
    for field, count in sorted(fields_updated.items(), key=lambda x: -x[1])[:20]:
        logger.info(f"   {field}: {count} times")
    logger.info(f"{'='*60}\n")

    # Update global if we used it
    if offers_df_input is None:
        offers_df = df

    return df


def _extract_olx_from_soup(soup, index: int) -> dict:
    """Extract ALL available OLX offer data from parsed HTML soup."""
    extracted = {}

    try:
        # Title - multiple selectors
        title_elem = (
            soup.find("h4", {"data-cy": "ad_title"}) or
            soup.find("h4", {"data-cy": "offer_title"}) or
            soup.select_one('[data-cy="offer_title"] h4') or
            soup.find("h4", class_=re.compile(r"css-1[a-z0-9]+"))
        )
        if title_elem:
            extracted["Title"] = title_elem.get_text().strip()

        # Price from price container
        price_container = soup.find("div", {"data-testid": "ad-price-container"})
        if price_container:
            price_h3 = price_container.find("h3")
            if price_h3:
                extracted["Price_Text"] = price_h3.get_text().strip()
                # Extract numeric price
                price_match = re.search(r'[\d\s]+', price_h3.get_text())
                if price_match:
                    price_num = price_match.group().replace(' ', '').replace('\xa0', '')
                    try:
                        extracted["Base_value"] = int(price_num)
                    except ValueError:
                        pass
            # Check for negotiable
            nego_elem = price_container.find("p")
            if nego_elem and "negocjacji" in nego_elem.get_text().lower():
                extracted["Negotiable"] = "Yes"

        # Description - try multiple containers
        desc_container = (
            soup.find("div", {"data-cy": "ad_description"}) or
            soup.find("div", {"data-testid": "ad_description"})
        )
        if desc_container:
            # Get the actual text div inside
            desc_text_div = desc_container.find("div", class_=re.compile(r"css-"))
            if desc_text_div:
                extracted["Description"] = desc_text_div.get_text(separator="\n").strip()
            else:
                # Fallback to container text minus header
                full_text = desc_container.get_text(separator="\n").strip()
                # Remove "Description" header if present (Otodom shows this Polish heading verbatim)
                if full_text.startswith("Description"):
                    full_text = full_text[4:].strip()
                extracted["Description"] = full_text

        # Ad parameters - this is the main data source for OLX!
        params_container = soup.find("div", {"data-testid": "ad-parameters-container"})
        if params_container:
            params = params_container.find_all("p", class_=re.compile(r"css-"))
            for param in params:
                text = param.get_text().strip()
                if ":" in text:
                    key, value = text.split(":", 1)
                    key = key.strip()
                    value = value.strip()
                    extracted[key] = value

                    # Parse specific fields
                    if "Powierzchnia" in key:
                        area_match = re.search(r'(\d+)', value)
                        if area_match:
                            extracted["Area"] = int(area_match.group(1))
                    elif "Liczba pokoi" in key:
                        extracted["Room_count"] = value
                    elif "Czynsz" in key:
                        czynsz_match = re.search(r'(\d+)', value)
                        if czynsz_match:
                            extracted["Additional_value"] = int(czynsz_match.group(1))
                    elif "Poziom" in key or "Piętro" in key:
                        extracted["Floor"] = value
                elif text == "Prywatne":
                    extracted["Seller"] = "Private individual"
                elif text == "Firmowe":
                    extracted["Seller"] = "Company"

        # Location from multiple possible places
        loc_elem = soup.find("p", class_="css-9pna1a")  # City
        if loc_elem:
            extracted["City"] = loc_elem.get_text().strip()

        loc_detail = soup.find("p", class_="css-3cz5o2")  # Region
        if loc_detail:
            extracted["Region"] = loc_detail.get_text().strip()

        # Combined location
        loc_container = soup.find("div", class_=re.compile(r"css-17dk4rn"))
        if loc_container:
            loc_parts = loc_container.find_all("p")
            if loc_parts:
                extracted["Location"] = ", ".join(p.get_text().strip() for p in loc_parts)

        # Seller card info
        seller_card = soup.find("div", {"data-testid": "seller_card"})
        if seller_card:
            trader_title = seller_card.find("p", {"data-testid": "trader-title"})
            if trader_title:
                extracted["Seller"] = trader_title.get_text().strip()

            user_name = seller_card.find("h4", {"data-testid": "user-profile-user-name"})
            if user_name:
                extracted["Seller_Name"] = user_name.get_text().strip()

            member_since = seller_card.find("p", {"data-testid": "member-since"})
            if member_since:
                extracted["Seller_Since"] = member_since.get_text().strip()

            last_seen = seller_card.find("p", {"data-testid": "lastSeenBox"})
            if last_seen:
                extracted["Seller_LastSeen"] = last_seen.get_text().strip()

            user_link = seller_card.find("a", {"data-testid": "user-profile-link"})
            if user_link and user_link.get("href"):
                extracted["Seller_Profile_URL"] = "https://www.olx.pl" + user_link["href"]

        # Posted date
        posted_elem = soup.find("span", {"data-testid": "ad-posted-at"})
        if posted_elem:
            extracted["Posted_Date"] = posted_elem.get_text().strip()

        # Ad ID from footer
        footer = soup.find("div", {"data-testid": "ad-footer-bar-section"})
        if footer:
            id_span = footer.find("span", class_=re.compile(r"css-"))
            if id_span and "ID:" in id_span.get_text():
                id_match = re.search(r'ID:\s*(\d+)', id_span.get_text())
                if id_match:
                    extracted["OLX_ID"] = id_match.group(1)

        # Images - get main image and count
        main_img = soup.find("img", {"data-testid": "swiper-image"})
        if main_img and main_img.get("src"):
            extracted["Photo_URL"] = main_img["src"]

        # Count all images
        all_images = soup.find_all("div", {"data-cy": "adPhotos-swiperSlide"})
        if all_images:
            extracted["Image_Count"] = len(all_images)

        # Breadcrumbs for category
        breadcrumbs = soup.find("nav", {"data-testid": "breadcrumbs"})
        if breadcrumbs:
            crumb_items = breadcrumbs.find_all("li")
            if crumb_items:
                extracted["Category"] = " > ".join(
                    li.get_text().strip() for li in crumb_items if li.get_text().strip()
                )

        return extracted if extracted else None

    except Exception as e:
        logger.error(f"Error extracting OLX data from soup for index {index}: {e}")
        import traceback
        logger.debug(traceback.format_exc())
        return None


def _extract_otodom_from_soup(soup, index: int) -> dict:
    """Extract ALL available Otodom offer data from parsed HTML soup."""
    extracted = {}

    try:
        # Title - multiple selectors
        title_elem = (
            soup.find("h1", {"data-cy": "adPageAdTitle"}) or
            soup.select_one('[data-cy="adPageAdTitle"]') or
            soup.find("h1") or
            soup.select_one('[data-sentry-element="Title"]')
        )
        if title_elem:
            extracted["Title"] = title_elem.get_text().strip()

        # Price - primary selector
        price_elem = (
            soup.find("strong", {"data-cy": "adPageHeaderPrice"}) or
            soup.select_one('[data-cy="adPageHeaderPrice"]') or
            soup.select_one('[data-sentry-element="Price"]')
        )
        if price_elem:
            price_text = price_elem.get_text().strip()
            extracted["Price_Text"] = price_text
            # Extract numeric price
            price_match = re.search(r'[\d\s]+', price_text)
            if price_match:
                price_num = price_match.group().replace(' ', '').replace('\xa0', '')
                try:
                    extracted["Base_value"] = int(price_num)
                except ValueError:
                    pass

        # Additional price (czynsz)
        additional_price = soup.select_one('[data-sentry-element="AdditionalPriceWrapper"]')
        if additional_price:
            add_price_text = additional_price.get_text().strip()
            extracted["Additional_Price_Text"] = add_price_text
            czynsz_match = re.search(r'(\d[\d\s]*)', add_price_text)
            if czynsz_match:
                try:
                    extracted["Additional_value"] = int(czynsz_match.group(1).replace(' ', ''))
                except ValueError:
                    pass

        # Description
        desc_elem = (
            soup.select_one('[data-cy="adPageAdDescription"]') or
            soup.select_one('[data-sentry-element="DescriptionWrapper"]') or
            soup.select_one('[data-sentry-element="Description"]')
        )
        if desc_elem:
            extracted["Description"] = desc_elem.get_text(separator="\n").strip()

        # Address
        addr_elem = (
            soup.select_one('[data-sentry-element="AdvertAddress"]') or
            soup.find("a", {"aria-label": re.compile(r"Adres", re.IGNORECASE)})
        )
        if addr_elem:
            extracted["Address"] = addr_elem.get_text().strip()

        # Otodom ID
        id_elem = soup.select_one('[data-sentry-element="DetailsProperty"]')
        if id_elem:
            id_text = id_elem.get_text()
            id_match = re.search(r'ID\s*:\s*(\d+)', id_text)
            if id_match:
                extracted["Otodom_ID"] = id_match.group(1)

        # Property details from ItemsContainer (main details section)
        items_container = soup.select_one('[data-sentry-element="ItemsContainer"]')
        if items_container:
            items = items_container.select('[data-sentry-element="ItemGridContainer"]')
            for item in items:
                label_elem = item.select_one('[data-sentry-element="P3"]')
                value_elem = item.select_one('[data-sentry-element="P2"]')
                if label_elem and value_elem:
                    label = label_elem.get_text().strip()
                    value = value_elem.get_text().strip()
                    extracted[f"Property_{label}"] = value

                    # Parse specific fields
                    if "Powierzchnia" in label:
                        area_match = re.search(r'(\d+)', value)
                        if area_match:
                            extracted["Area"] = int(area_match.group(1))
                    elif "Liczba pokoi" in label or "pokoi" in label.lower():
                        extracted["Room_count"] = value
                    elif "Piętro" in label:
                        extracted["Floor"] = value

        # Alternative property details from divs with ezix4ds class pattern
        detail_divs = soup.find_all("div", class_=re.compile(r"e[a-z]+\d+s\d+"))
        for div in detail_divs:
            paragraphs = div.find_all("p")
            if len(paragraphs) >= 2:
                label = paragraphs[0].get_text().strip().rstrip(':')
                value = paragraphs[1].get_text().strip()
                if label and value and f"Property_{label}" not in extracted:
                    extracted[f"Property_{label}"] = value

        # Agent/Seller info from CompanyInfoContainer
        company_info = soup.select_one('[data-sentry-element="CompanyInfoContainer"]')
        if company_info:
            # Agent name
            agent_name = company_info.find("p", class_=re.compile(r"e1fuu9gk"))
            if agent_name:
                extracted["Agent_Name"] = agent_name.get_text().strip()

            # Agency link and name
            agency_link = company_info.find("a", class_=re.compile(r"css-"))
            if agency_link:
                extracted["Agency_Name"] = agency_link.get_text().strip()
                if agency_link.get("href"):
                    href = agency_link["href"]
                    if href.startswith("/"):
                        href = "https://www.otodom.pl" + href
                    extracted["Agency_URL"] = href

        # Fallback agent info
        if "Agent_Name" not in extracted:
            agent_elem = soup.find("p", class_=re.compile(r"e1fuu9gk"))
            if agent_elem:
                extracted["Agent_Name"] = agent_elem.get_text().strip()

        # Images - use centralized extractor
        image_url = extract_otodom_image_url(soup)
        if image_url:
            extracted["Main_Image_URL"] = image_url

        # Count images
        photo_blocks = soup.select('[data-sentry-element="PhotoBlock"]')
        if photo_blocks:
            extracted["Image_Count"] = len(photo_blocks)

        # Multimedia list for all images
        multimedia = soup.select_one('[data-sentry-element="MultimediaList"]')
        if multimedia:
            all_imgs = multimedia.find_all("img")
            if all_imgs:
                extracted["Image_Count"] = len(all_imgs)

        # Breadcrumbs for location/category info
        breadcrumbs = soup.select_one('[data-sentry-element="BreadcrumbsWrapper"]')
        if breadcrumbs:
            crumb_links = breadcrumbs.find_all("a")
            if crumb_links:
                # Last few crumbs are usually location
                crumb_texts = [a.get_text().strip() for a in crumb_links if a.get_text().strip()]
                if len(crumb_texts) >= 2:
                    extracted["Category"] = " > ".join(crumb_texts[:3])
                    extracted["Location_Breadcrumb"] = " > ".join(crumb_texts[-3:])

        # Contact form phone if visible
        phone_wrapper = soup.select_one('[data-sentry-element="PhoneNumberWrapper"]')
        if phone_wrapper:
            phone_text = phone_wrapper.get_text().strip()
            if phone_text and not phone_text.startswith("xxx"):
                extracted["Phone_Number"] = phone_text

        # Views count if available
        views_elem = soup.select_one('[data-sentry-element="EyeIcon"]')
        if views_elem:
            parent = views_elem.parent
            if parent:
                views_text = parent.get_text().strip()
                views_match = re.search(r'(\d+)', views_text)
                if views_match:
                    extracted["Views"] = int(views_match.group(1))

        return extracted if extracted else None

    except Exception as e:
        logger.error(f"Error extracting Otodom data from soup for index {index}: {e}")
        import traceback
        logger.debug(traceback.format_exc())
        return None


def validate_html_content(html_content, min_size=1000, check_404=True):
    """
    Validate HTML content before saving it.

    Args:
        html_content: HTML to validate
        min_size: Minimum required size in bytes
        check_404: Whether to check for 404 errors

    Returns:
        tuple: (is_valid, error_message)
    """
    if not html_content:
        return False, "HTML content is None or empty"
    
    if len(html_content) < min_size:
        return False, f"HTML too short: {len(html_content)} bytes (min: {min_size})"
    
    if check_404:
        lower_html = html_content.lower()
        # More specific 404 detection patterns
        is_404 = any([
            "błąd 404" in lower_html,
            "error 404" in lower_html,
            "strona nie istnieje" in lower_html,
            "ogłoszenie nie istnieje" in lower_html,
            "nie znaleziono strony" in lower_html,
        ])
        if is_404:
            return False, "Page returned 404 error"
    
    # Check for basic HTML tags
    if "<html" not in html_content.lower() and "<body" not in html_content.lower():
        return False, "Missing basic HTML structure"

    return True, "Valid HTML"


# Example usage in the main script:
def example_integration():
    """
    Example of how to integrate these functions into the main script.
    """

    # Inside scrape_olx / scrape_otodom - save the listing HTML:
    def scrape_olx_with_html_save(html_content, page_num=1):
        # Validate the HTML
        is_valid, error_msg = validate_html_content(html_content, min_size=5000)
        if not is_valid:
            logger.warning(f"⚠️ Invalid listing HTML: {error_msg}")
            return 0

        # Save the listing page HTML
        listing_html_path = save_listing_page_html(html_content, page_num, source="olx")
        if listing_html_path:
            logger.info(f"✅ Listing HTML saved: {listing_html_path}")

        # Continue parsing as usual...
        soup = BeautifulSoup(html_content, "html.parser")
        # ... rest of the code


    # Inside extract_olx_details - save the offer HTML:
    def extract_olx_details_with_html_save(page, index, current_num, total_num):
        try:
            offer = offers_df.loc[index]
            url = offer["Link"]

            # Load the page
            page.goto(url, timeout=60000, wait_until="load")
            html = page.inner_html("body")

            # VALIDATE BEFORE SAVING
            is_valid, error_msg = validate_html_content(html)
            if not is_valid:
                logger.warning(f"⚠️ Invalid offer HTML for {index}: {error_msg}")
                offers_df.at[index, "Scraping_Error"] = error_msg
                return False
            
            # SAVE HTML
            html_filepath = save_html_to_file(
                html_content=html,
                index=index,
                url=url,
                file_prefix="olx_detail"
            )

            if html_filepath:
                offers_df.at[index, "HTML_File_Path"] = html_filepath
                logger.debug(f"✅ HTML saved for offer {index}")
            else:
                logger.warning(f"⚠️ Failed to save HTML for offer {index}")

            # Continue parsing...
            soup = BeautifulSoup(html, "html.parser")
            # ... rest of the code
            
        except Exception as e:
            logger.error(f"❌ Error in extract_olx_details: {e}")
            return False
        
        


def run(playwright: Playwright) -> None:
    global offers_df, _run_config

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
    run_olx_url = _run_config["olx_url"]
    run_otodom_url = _run_config["otodom_url"]
    run_origin = _run_config["reference_address"]

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

    ## Get max pages limit from environment if set
    max_pages = int(os.environ.get('DEBUG_MAX_PAGES', '')) if os.environ.get('DEBUG_MAX_PAGES') else None
    
    ## PART 1 - OLX
    page2 = context.new_page()
    page2.goto(run_olx_url, timeout=60000)
    try:
        accept_button = page2.get_by_role("button", name="Akceptuję")
        if accept_button.count() > 0:
            accept_button.click(timeout=10000)
            logger.info("✅ Clicked OLX accept cookies button")
        else:
            logger.info("ℹ️ No OLX 'Akceptuję' button found, trying OneTrust accept-all...")
            try:
                onetrust_btn = page2.locator("#onetrust-accept-btn-handler, .ot-btn-accept-all, button#accept-all-button")
                if onetrust_btn.count() > 0:
                    onetrust_btn.first.click(timeout=5000)
                    logger.info("✅ Clicked OneTrust accept-all button")
                    page2.wait_for_selector("#onetrust-consent-sdk", state="hidden", timeout=5000)
                else:
                    logger.info("ℹ️ No cookie consent button found")
            except Exception as e2:
                logger.warning(f"⚠️ Could not dismiss OneTrust: {e2}")
                # Force-hide the overlay via JS as last resort
                try:
                    page2.evaluate("document.getElementById('onetrust-consent-sdk')?.remove()")
                    logger.info("✅ Removed OneTrust overlay via JS")
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"⚠️ Could not click OLX accept button: {e}")
    scroll_alot(page2)
    next_btn = page2.get_by_test_id("pagination-forward")
    current_page_number = page2.locator("li.pagination-item__active")
    last_page_number = page2.locator('ul[data-testid="pagination-list"] li').last
    olx_html = page2.inner_html("body")
    # Save listing page HTML
    save_listing_page_html(olx_html, 1, source="olx")
    olx_added = scrape_olx(olx_html)
    logger.info(f"OLX page 1 added {olx_added} offers")

    try:
        logger.debug(f'OLX page: {current_page_number.inner_text(timeout=3000)} / {last_page_number.inner_text(timeout=3000)}')
    except Exception:
        pass
    
    page_count = 1  # Track page count for debug mode
    while next_btn.count() > 0:
        # Check if we've reached max pages limit
        if max_pages and page_count >= max_pages:
            logger.info(f"🛑 Reached max_pages limit of {max_pages}")
            break
            
        next_btn = page2.get_by_test_id("pagination-forward")
        # Dismiss any cookie overlay before clicking pagination
        try:
            page2.evaluate("document.getElementById('onetrust-consent-sdk')?.remove()")
        except Exception:
            pass
        next_btn.click()
        time.sleep(0.2)
        scroll_alot(page2)
        time.sleep(0.4)
        next_btn = page2.get_by_test_id("pagination-forward")
        current_page_number = page2.locator("li.pagination-item__active")
        last_page_number = page2.locator('ul[data-testid="pagination-list"] li').last
        
        page_count += 1  # Increment page counter
        
        try:
            current_page_text = current_page_number.inner_text(timeout=5000)
            last_page_text = last_page_number.inner_text(timeout=5000)
            logger.debug(f'OLX page: {current_page_text} / {last_page_text}')
        except Exception as e:
            logger.warning(f"⚠️ Could not get page numbers: {e}")
            current_page_text = "unknown"
        
        olx_html = page2.inner_html("body")
        # Save listing page HTML
        save_listing_page_html(olx_html, page_count, source="olx")
        olx_added = scrape_olx(olx_html)

        try:
            if current_page_text != "unknown":
                logger.info(f"OLX page {current_page_text} added {olx_added} offers")
            else:
                logger.info(f"OLX page added {olx_added} offers")
        except:
            logger.info(f"OLX page added {olx_added} offers")

    logger.debug(f'OLX offers before dedup: {len(offers_df)}')
    offers_df = offers_df[offers_df['Link'] != "olx.pl/"]
    offers_df = offers_df.drop_duplicates(subset=['Link']).reset_index(drop=True)
    logger.info(f'OLX offers after dedup: {len(offers_df)}')

    # PART 2 - Scrape Otodom
    page = context.new_page()

    # Add retry logic for page loading
    max_retries = 3
    page_loaded_successfully = False

    for attempt in range(max_retries):
        try:
            logger.info(f"Loading Otodom page (attempt {attempt + 1}/{max_retries})")
            page.goto(run_otodom_url, timeout=50000, wait_until="domcontentloaded")
            
            # Wait for page content with multiple verification strategies
            try:
                # Wait for network idle to ensure basic content is loaded
                page.wait_for_load_state('networkidle', timeout=30000)
                time.sleep(1)  # Extra grace period for JavaScript rendering
                
                # Strategy 1: Check for listing container elements (most reliable)
                page_html = page.inner_html("body")
                has_listings = False
                has_content = False
                listing_count = 0
                
                # Try to find listings using multiple selectors
                try:
                    # Primary selector for Otodom listings
                    listings = page.locator('div[data-testid="listing-item"]')
                    listing_count = listings.count()
                    if listing_count > 0:
                        has_listings = True
                        logger.info(f"✅ Found {listing_count} listings via primary selector")
                    else:
                        logger.debug(f"ℹ️ No listings found via primary selector (data-testid=listing-item)")
                except Exception as e:
                    logger.debug(f"ℹ️ Primary selector check failed: {e}")
                
                # Fallback: Check for any listing-like containers
                if not has_listings:
                    try:
                        articles = page.locator('article')
                        article_count = articles.count()
                        if article_count > 0:
                            has_listings = True
                            listing_count = article_count
                            logger.info(f"✅ Found {article_count} listings via fallback selector (article)")
                        else:
                            logger.debug(f"ℹ️ No articles found on page")
                    except Exception as e:
                        logger.debug(f"ℹ️ Article selector check failed: {e}")
                
                # Fallback: Check for divs with specific classes
                if not has_listings:
                    try:
                        listing_divs = page.locator('div[class*="listing"]')
                        div_count = listing_divs.count()
                        if div_count > 0:
                            has_listings = True
                            listing_count = div_count
                            logger.info(f"✅ Found {div_count} potential listings via div selector")
                        else:
                            logger.debug(f"ℹ️ No divs with 'listing' in class found")
                    except Exception as e:
                        logger.debug(f"ℹ️ Div selector check failed: {e}")
                
                # Fallback: Check HTML content for indicators
                if not has_listings:
                    has_content = len(page_html) > 5000
                    has_text_indicators = (
                        'oferta' in page_html.lower() or 
                        'listing' in page_html.lower() or 
                        'zł' in page_html or
                        'wynajm' in page_html.lower()
                    )
                    
                    logger.debug(f"ℹ️ HTML check: size={len(page_html)} bytes, has_indicators={has_text_indicators}")
                    
                    if has_content and has_text_indicators:
                        logger.info(f"✅ Page has content (HTML size: {len(page_html)} bytes)")
                        has_listings = True
                
                if has_listings:
                    logger.info("✅ Page loaded successfully with content")
                    page_loaded_successfully = True
                    break
                else:
                    logger.warning(f"⚠️ Page appears empty on attempt {attempt + 1} (no listings found)")
                    if attempt < max_retries - 1:
                        time.sleep(5)
                        continue
                        
            except Exception as e:
                logger.warning(f"⚠️ Page load timeout on attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    time.sleep(5)
                    continue
                    
        except Exception as e:
            logger.error(f"❌ Failed to load page on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                time.sleep(5)
                continue

    # NOW this is at the correct indentation level (outside the for loop)
    # Skip Otodom but continue to detailed extraction
    # After the for loop, before the if statement
    if 'page_loaded_successfully' not in locals():
        page_loaded_successfully = False
        logger.error("⚠️ page_loaded_successfully was not set - defaulting to False")

    if not page_loaded_successfully:
        logger.error("❌ Otodom listing scraping skipped due to page load failure")
        logger.info(f"⚠️ Continuing with {len(offers_df)} offers from OLX only - will extract details")
        try:
            page.close()
        except:
            pass
        
    else:
        try:
            accept_button = page.get_by_role("button", name="Akceptuję")
            if accept_button.count() > 0:
                accept_button.click()
                logger.info("✅ Clicked accept cookies button")
            else:
                logger.info("ℹ️ No accept button found")
        except Exception as e:
            logger.warning(f"⚠️ Could not click accept button: {e}")
        
        scroll_alot(page)
        time.sleep(1)
        page_html = page.inner_html("body")
        # Save listing page HTML
        save_listing_page_html(page_html, 1, source="otodom")

        # Initial scrape after loading first page
        otodom_added = scrape_otodom_working(page_html)
        logger.info(f"Otodom initial page added {otodom_added} offers")

        # Updated selectors for the pagination elements
        try:
            # Try new selector first, fall back to old one
            ul_nav = page.locator("ul[data-cy='nexus-pagination-component']")
            if ul_nav.count() == 0:
                ul_nav = page.locator("[data-cy='search-list-pagination'] nav ul, [data-sentry-component='BasePagination'] ul")
            ul_nav.wait_for(timeout=10000)

            all_items = ul_nav.locator("li")
            logger.info(f"Pagination items found: {all_items.count()}")
            
            # Your pagination logic continues here...
            def go_to_next_page(page) -> bool:
                """Clicks the 'Next' pagination item if enabled. Returns False on last page."""
                logger.debug("Checking pagination for NEXT button...")

                scroll_alot(page)
                time.sleep(1)

                pagination = ul_nav
                pagination.wait_for(state="visible", timeout=30000)

                # The aria-label is on the BUTTON, not on the LI
                next_btn = pagination.locator("button[aria-label='Go to next Page']").first

                if next_btn.count() == 0:
                    # Try alternative selector
                    next_btn = pagination.locator("button[title='Go to next Page']").first

                if next_btn.count() == 0:
                    logger.debug("No next button found – assuming last page.")
                    return False

                # Check if button is disabled
                is_disabled = next_btn.is_disabled()
                logger.debug(f"Next button disabled: {is_disabled}")

                if is_disabled:
                    logger.debug("Next button is disabled – reached last page.")
                    return False

                logger.debug("Clicking NEXT button...")
                next_btn.click()
                page.wait_for_timeout(1500)
                return True

            page_index = 1

            while True:
                logger.info(f"Scraping Otodom page {page_index}")
                scroll_alot(page)
                time.sleep(1)
                scroll_alot(page)
                html_now = page.content()
                # Save listing page HTML
                save_listing_page_html(html_now, page_index, source="otodom")
                page_added = scrape_otodom_working(html_now)
                logger.info(f"Otodom page {page_index} added {page_added} offers")

                # Get current selected page number before clicking next
                try:
                    # aria-current is on the BUTTON, not the LI
                    selected_locator = ul_nav.locator("button[aria-current='true']").first
                    selected_before = selected_locator.inner_text().strip()
                    logger.debug(f"Currently selected page: {selected_before}")
                except Exception:
                    selected_before = "?"
                    logger.debug("Could not read currently selected page.")

                # Try to move to next page
                if not go_to_next_page(page):
                    logger.info(f"No more pages after page {page_index}. Stopping.")
                    break

                logger.debug(f"Waiting for page to change from {selected_before}...")

                try:
                    page.wait_for_function(
                        """
                        ([sel, before]) => {
                            const el = document.querySelector(sel);
                            if (!el) return false;
                            const cur = el.textContent.trim();
                            return cur && cur !== before;
                        }
                        """,
                        arg=["ul[data-cy='nexus-pagination-component'] button[aria-current='true']", selected_before],
                        timeout=8000
                    )
                    logger.debug("Page changed successfully.")
                except Exception:
                    logger.debug("Page change timed out – checking if page actually changed...")
                    # Check if page changed despite timeout
                    try:
                        selected_after = ul_nav.locator("button[aria-current='true']").first.inner_text().strip()
                        if selected_after == selected_before:
                            logger.debug(f"Page still on {selected_before} – stopping pagination.")
                            break
                        else:
                            logger.debug(f"Page did change to {selected_after} – continuing.")
                    except Exception:
                        logger.debug("Could not verify page change – stopping.")
                        break

                page_index += 1
                
        except Exception as e:
            logger.warning(f"⚠️ Otodom pagination not available (single page of results): {e}")
            logger.info("Continuing with offers collected from first page only")

    logger.info("Otodom scraping complete")

    # Close pages before detailed extraction
    try:
        page.close()
        page2.close()
    except:
        pass


    logger.info(f"Scraping complete. Total offers: {len(offers_df)}")
    offers_df.to_csv(f'all_offers_df_{search}.csv')
    offers_df = offers_df.drop_duplicates(subset=['Link']).reset_index(drop=True)
    logger.info(f"After dedup: {len(offers_df)} offers")

    # Generate and log comprehensive statistics
    log_scraping_stats()

    # SAVE BASIC DATA TO SHEETS FIRST (before detailed extraction)
    logger.info("💾 Saving basic offer data to sheets before extracting details...")

    try:
        if "Czynsz (dodatkowo)" in offers_df.columns:
            # Force Series selection and convert to string to avoid .str errors
            czynsz_series = offers_df["Czynsz (dodatkowo)"].astype(str)

            offers_df["Czynsz (dodatkowo) value"] = (
                czynsz_series
                .str.extract(r"(\d+(?:[\.,]\d+)?)")[0]  # [0] ensures we select the first match
                .fillna("0")
                .str.replace(",", ".", regex=False)
                .astype(float)
            )
        else:
            offers_df["Czynsz (dodatkowo) value"] = 0.0

        if "Additional_value" not in offers_df.columns:
            offers_df["Additional_value"] = 0.0

        offers_df["Additional_value"] = np.where(
            offers_df["Czynsz (dodatkowo) value"] == 0.0,
            offers_df["Additional_value"],
            offers_df["Czynsz (dodatkowo) value"]
        )

        if "Base_value" not in offers_df.columns:
            offers_df["Base_value"] = 0.0

        # If Additional_value == Base_value → Full_value = Base_value
        # Otherwise, Full_value = Base_value + Additional_value
        offers_df["Full_value"] = np.where(
            offers_df["Additional_value"].fillna(0) == offers_df["Base_value"].fillna(0),
            offers_df["Base_value"].fillna(0),
            offers_df["Base_value"].fillna(0) + offers_df["Additional_value"].fillna(0)
        )

        logger.info(f"✅ Data processing completed successfully")

    except Exception as e:
        logger.error(f"❌ Failed to process data: {e}")

    offers_df = offers_df.sort_values(by='Full_value', ascending=True).reset_index(drop=True)
    save_basic_data_to_sheets()
    
    # Close listing pages before starting detailed extraction
    try:
        page.close()
        page2.close()
    except:
        pass
    
    logger.info("🕰️ Waiting 3 seconds before starting detailed extraction...")
    time.sleep(3)

    # PART 3 - Scrape all offers (DETAILED EXTRACTION PHASE)
    logger.info("🔍 Starting mixed detailed extraction phase...")

    offers_otodom = offers_df[offers_df["Link"].str.contains("otodom.pl", na=False, regex=False)].copy()
    offers_olx = offers_df[offers_df["Link"].str.contains("olx.pl", na=False, regex=False)].copy()

    # Randomize order to distribute server load and avoid patterns
    otodom_indices = offers_otodom.sample(frac=1).index.tolist()
    olx_indices = offers_olx.sample(frac=1).index.tolist()

    # Mix both sources for balanced extraction
    mixed_extraction_plan = []
    for i in range(max(len(otodom_indices), len(olx_indices))):
        if i < len(otodom_indices):
            mixed_extraction_plan.append(('otodom', otodom_indices[i]))
        if i < len(olx_indices):
            mixed_extraction_plan.append(('olx', olx_indices[i]))

    # Apply max extractions limit if set
    max_extractions = int(os.environ.get('DEBUG_MAX_EXTRACTIONS', '')) if os.environ.get('DEBUG_MAX_EXTRACTIONS') else None
    if max_extractions and len(mixed_extraction_plan) > max_extractions:
        logger.info(f"🛑 Limiting extractions from {len(mixed_extraction_plan)} to {max_extractions}")
        mixed_extraction_plan = mixed_extraction_plan[:max_extractions]

    logger.info(
        f"🔀 Mixed extraction plan: {len(offers_otodom)} Otodom + {len(offers_olx)} OLX = {len(mixed_extraction_plan)} total")

    page_otodom = context.new_page()
    page_olx = context.new_page()

    def is_page_valid(page):
        """Check if page is still valid and usable"""
        try:
            # Try to access the page - if closed, this will fail
            page.url
            return True
        except Exception:
            return False

    def ensure_valid_page(page, page_name):
        """Ensure page is valid, recreate if needed"""
        nonlocal page_otodom, page_olx
        if not is_page_valid(page):
            logger.warning(f"⚠️ {page_name} page was closed, recreating...")
            new_page = context.new_page()
            if page_name == "otodom":
                page_otodom = new_page
            else:
                page_olx = new_page
            return new_page
        return page

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

        for source, index in current_batch:
            item_start_time = time.time()

            if source == 'otodom':
                page_otodom = ensure_valid_page(page_otodom, "otodom")
                success = extract_otodom_details(page_otodom, index, processed_count + 1, len(mixed_extraction_plan))
            else:  # olx
                page_olx = ensure_valid_page(page_olx, "olx")
                success = extract_olx_details(page_olx, index, processed_count + 1, len(mixed_extraction_plan))

            if success:
                batch_success_count += 1
            else:
                batch_failed_count += 1
                failed_extractions.append((source, index))
                logger.warning(f"⚠️ Failed to extract {source} offer {index}, will retry later")

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
                    # Ensure the column exists
                    if "Czynsz (dodatkowo)" in offers_df.columns:
                        # Force Series selection and convert to string to avoid .str errors
                        czynsz_series = offers_df["Czynsz (dodatkowo)"].astype(str)

                        offers_df["Czynsz (dodatkowo) value"] = (
                            czynsz_series
                            .str.extract(r"(\d+(?:[\.,]\d+)?)")[0]  # [0] ensures we select the first match
                            .fillna("0")
                            .str.replace(",", ".", regex=False)
                            .astype(float)
                        )
                    else:
                        # If the column is missing, default to zeros
                        offers_df["Czynsz (dodatkowo) value"] = 0.0

                    # Safely update Additional_value
                    if "Additional_value" not in offers_df.columns:
                        offers_df["Additional_value"] = 0.0

                    offers_df["Additional_value"] = np.where(
                        offers_df["Czynsz (dodatkowo) value"] == 0.0,
                        offers_df["Additional_value"],
                        offers_df["Czynsz (dodatkowo) value"]
                    )

                    # Ensure Base_value column exists before summing
                    if "Base_value" not in offers_df.columns:
                        offers_df["Base_value"] = 0.0

                    # Calculate Full_value:
                    # If Additional_value == Base_value → Full_value = Base_value
                    # Otherwise, Full_value = Base_value + Additional_value
                    offers_df["Full_value"] = np.where(
                        offers_df["Additional_value"].fillna(0) == offers_df["Base_value"].fillna(0),
                        offers_df["Base_value"].fillna(0),
                        offers_df["Base_value"].fillna(0) + offers_df["Additional_value"].fillna(0)
                    )

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
            # Ensure the column exists
            if "Czynsz (dodatkowo)" in offers_df.columns:
                # Force Series selection and convert to string to avoid .str errors
                czynsz_series = offers_df["Czynsz (dodatkowo)"].astype(str)

                offers_df["Czynsz (dodatkowo) value"] = (
                    czynsz_series
                    .str.extract(r"(\d+(?:[\.,]\d+)?)")[0]  # [0] ensures we select the first match
                    .fillna("0")
                    .str.replace(",", ".", regex=False)
                    .astype(float)
                )
            else:
                # If the column is missing, default to zeros
                offers_df["Czynsz (dodatkowo) value"] = 0.0

            # Safely update Additional_value
            if "Additional_value" not in offers_df.columns:
                offers_df["Additional_value"] = 0.0

            offers_df["Additional_value"] = np.where(
                offers_df["Czynsz (dodatkowo) value"] == 0.0,
                offers_df["Additional_value"],
                offers_df["Czynsz (dodatkowo) value"]
            )

            # Ensure Base_value column exists before summing
            if "Base_value" not in offers_df.columns:
                offers_df["Base_value"] = 0.0

            # Calculate Full_value:
            # If Additional_value == Base_value → Full_value = Base_value
            # Otherwise, Full_value = Base_value + Additional_value
            offers_df["Full_value"] = np.where(
                offers_df["Additional_value"].fillna(0) == offers_df["Base_value"].fillna(0),
                offers_df["Base_value"].fillna(0),
                offers_df["Base_value"].fillna(0) + offers_df["Additional_value"].fillna(0)
            )

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

        for source, index in failed_extractions[:MAX_RETRIES * 5]:  # Limit retries
            retry_count += 1
            logger.info(f"🔄 Retry {retry_count}: {source} offer {index}")

            if source == 'otodom':
                page_otodom = ensure_valid_page(page_otodom, "otodom")
                success = extract_otodom_details(page_otodom, index, retry_count, len(failed_extractions),
                                                 is_retry=True)
            else:
                page_olx = ensure_valid_page(page_olx, "olx")
                success = extract_olx_details(page_olx, index, retry_count, len(failed_extractions), is_retry=True)

            if success:
                retry_success_count += 1
                logger.info(f"✅ Retry successful for {source} offer {index}")

            time.sleep(random.uniform(5.0, 10.0))

        # Update sheets after retries if any were successful
        if retry_success_count > 0:
            logger.info(f"📊 Updating Google Sheets after retries ({retry_success_count} successful)...")
            try:
                # Process data before post-retry save
                try:
                    # Ensure the column exists
                    if "Czynsz (dodatkowo)" in offers_df.columns:
                        # Force Series selection and convert to string to avoid .str errors
                        czynsz_series = offers_df["Czynsz (dodatkowo)"].astype(str)

                        offers_df["Czynsz (dodatkowo) value"] = (
                            czynsz_series
                            .str.extract(r"(\d+(?:[\.,]\d+)?)")[0]  # [0] ensures we select the first match
                            .fillna("0")
                            .str.replace(",", ".", regex=False)
                            .astype(float)
                        )
                    else:
                        # If the column is missing, default to zeros
                        offers_df["Czynsz (dodatkowo) value"] = 0.0

                    # Safely update Additional_value
                    if "Additional_value" not in offers_df.columns:
                        offers_df["Additional_value"] = 0.0

                    offers_df["Additional_value"] = np.where(
                        offers_df["Czynsz (dodatkowo) value"] == 0.0,
                        offers_df["Additional_value"],
                        offers_df["Czynsz (dodatkowo) value"]
                    )

                    # Ensure Base_value column exists before summing
                    if "Base_value" not in offers_df.columns:
                        offers_df["Base_value"] = 0.0

                    # Calculate Full_value:
                    # If Additional_value == Base_value → Full_value = Base_value
                    # Otherwise, Full_value = Base_value + Additional_value
                    offers_df["Full_value"] = np.where(
                        offers_df["Additional_value"].fillna(0) == offers_df["Base_value"].fillna(0),
                        offers_df["Base_value"].fillna(0),
                        offers_df["Base_value"].fillna(0) + offers_df["Additional_value"].fillna(0)
                    )

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


if __name__ == "__main__":
    # Check if running in re-extraction mode
    if args.reextract:
        logger.info("="*60)
        logger.info("🔄 RUNNING IN RE-EXTRACTION MODE")
        logger.info("="*60)

        # Load pickle file if specified
        if args.pickle_file:
            pickle_path = Path(args.pickle_file)
            if pickle_path.exists():
                logger.info(f"📂 Loading pickle file: {args.pickle_file}")
                offers_df = pd.read_pickle(args.pickle_file)
                logger.info(f"✅ Loaded {len(offers_df)} offers from pickle")
            else:
                logger.error(f"❌ Pickle file not found: {args.pickle_file}")
                sys.exit(1)
        else:
            # Try to find most recent pickle file
            pickle_files = sorted(Path(".").glob("apartments*.pkl"), key=lambda x: x.stat().st_mtime, reverse=True)
            if pickle_files:
                logger.info(f"📂 Auto-loading most recent pickle: {pickle_files[0]}")
                offers_df = pd.read_pickle(pickle_files[0])
                logger.info(f"✅ Loaded {len(offers_df)} offers")
            else:
                logger.error("❌ No pickle file found. Specify one with --pickle-file")
                sys.exit(1)

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
        sys.exit(0)

    # Standalone distance calculation mode
    if args.calc_distance:
        logger.info("="*60)
        logger.info("📏 STANDALONE DISTANCE CALCULATION MODE")
        logger.info("="*60)

        # Load pickle file
        if args.pickle_file:
            pickle_path = Path(args.pickle_file)
            if pickle_path.exists():
                logger.info(f"📂 Loading pickle file: {args.pickle_file}")
                offers_df = pd.read_pickle(args.pickle_file)
                logger.info(f"✅ Loaded {len(offers_df)} offers from pickle")
            else:
                logger.error(f"❌ Pickle file not found: {args.pickle_file}")
                sys.exit(1)
        else:
            # Try to find most recent pickle file
            pickle_files = sorted(Path(".").glob("apartments*.pkl"), key=lambda x: x.stat().st_mtime, reverse=True)
            if pickle_files:
                logger.info(f"📂 Auto-loading most recent pickle: {pickle_files[0]}")
                offers_df = pd.read_pickle(pickle_files[0])
                logger.info(f"✅ Loaded {len(offers_df)} offers")
            else:
                logger.error("❌ No pickle file found. Specify one with --pickle-file")
                sys.exit(1)

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

        sys.exit(0)

    # Normal scraping mode
    with sync_playwright() as playwright:
        run(playwright)

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

    if 'Budynek i materiały_Wyposażenie' in offers_df.columns and 'Wyposażenie' in offers_df.columns:
        offers_df['Dishwasher'] = np.where(
            (offers_df['Wyposażenie'].str.contains('zmywarka', na=False)) |
            (offers_df['Description'].str.contains('zmywarka', na=False)) |
            (offers_df['Budynek i materiały_Wyposażenie'].str.contains('zmywarka', na=False)),
            "Yes", "No")
    elif 'Wyposażenie' in offers_df.columns:
        offers_df['Dishwasher'] = np.where(
            (offers_df['Wyposażenie'].str.contains('zmywarka', na=False)) |
            (offers_df['Description'].str.contains('zmywarka', na=False)),
            "Yes", "No")
    elif 'Description' in offers_df.columns:
        mask = offers_df['Description'].str.contains('zmywarka', na=False)
    else:
        offers_df['Dishwasher'] = "No"

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
            _ws_cache = _client.open_by_key(sheet_id).worksheet('apartment list')
            cached_llm = read_existing_llm_scores_map(_ws_cache)
            logger.info(f"🤖 Loaded {len(cached_llm)} cached LLM scores from Sheets")
        except Exception as _llm_cache_err:
            logger.debug(f"Could not load LLM score cache from Sheets: {_llm_cache_err}")
        offers_df = score_listings_df(
            offers_df, llm_prefs, cached_llm,
            groq_model=_run_config.get("groq_model"),
        )

    # Ensure Status column exists
    if "Status" not in offers_df.columns:
        offers_df.insert(0, "Status", "New")
    else:
        offers_df["Status"] = offers_df["Status"].replace("", "New").fillna("New")

    offers_df.to_pickle(f'apartments_{dt.datetime.now().strftime("%d.%m.%Y")}.pkl')
    logger.info(f"Saved {len(offers_df)} offers to pickle")

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

            sheet_name = 'apartment list'
            spreadsheet = client.open_by_key(sheet_id)
            ws2 = spreadsheet.worksheet("cheapest")

            try:
                new_deals = ws2.get("A:BJ")
                if not new_deals:
                    new_deals = []
            except Exception as e:
                logger.error(f"Error fetching records: {e}")
                new_deals = []

            new_deals_df = pd.DataFrame(new_deals)
            ws = spreadsheet.worksheet(sheet_name)

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
