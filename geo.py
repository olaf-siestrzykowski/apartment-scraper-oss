"""Geocoding (Nominatim, Photon) and commute distances (OpenRouteService Matrix API)."""
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from parsing.addresses import extract_street_from_text

logger = logging.getLogger("logging_config")

# OpenRouteService API key for distance calculations
API_KEY = os.environ.get('OPENROUTESERVICE_API_KEY')

# Rate limiting delays
NOMINATIM_DELAY = 1.1  # Nominatim requires 1 req/sec
ORS_DELAY = 0.3

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


_warned_unknown_cities: set = set()


def is_within_city_bounds(coords: Dict[str, Any], city: str = "Warszawa") -> bool:
    """Check if coordinates are within the given city's bounding box"""
    if not coords:
        return False
    bounds = CITY_BOUNDS.get(city)
    if bounds is None:
        # Unknown city: don't reject every address against another city's box
        if city not in _warned_unknown_cities:
            logger.warning(f"⚠️ No bounding box for city '{city}' - skipping the in-city check")
            _warned_unknown_cities.add(city)
        return True
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


def calculate_distances_for_offers(df: pd.DataFrame, office_address: str,
                                    transport_mode: str = "foot-walking",
                                    city: str = "Warszawa") -> pd.DataFrame:
    """
    Calculate walking/cycling distances from each apartment to the office.
    Adds Distance_km and Duration_min columns to the dataframe.
    """
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
        logger.info("📈 Distance statistics:")
        logger.info(f"   Range: {valid_distances.min():.2f} - {valid_distances.max():.2f} km")
        logger.info(f"   Average: {valid_distances.mean():.2f} km")
        logger.info(f"   Calculated for: {len(valid_distances)}/{len(df)} offers")

    return df
