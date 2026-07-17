#!/usr/bin/env python3
"""One-shot migration: rebuilds config sheets for all profiles with new Schedule Days/Time rows."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from apartment_scraper import _ALL_PROFILES, _open_spreadsheet, load_config_sheet

for name, profile in _ALL_PROFILES.items():
    print(f"Updating config sheet for '{name}'...")
    try:
        sp = _open_spreadsheet(profile["sheet_id"])
        load_config_sheet(
            sp, name,
            default_olx_url=profile["olx_url"],
            default_otodom_url=profile["otodom_url"],
            default_origin=profile["origin_address"],
        )
        print(f"  ✅ Done")
    except Exception as e:
        print(f"  ❌ Error: {e}")
