#!/usr/bin/env python3
"""
setup.py - First-run setup assistant for apartment_scraper.

Run once before first use:
    python setup.py

What it does:
    1. Checks Python version and required dependencies
    2. Checks Google credentials file exists
    3. Creates profiles.json interactively (if missing)
    4. Creates the 'config' sheet in each profile's Google Sheet
       (with default address, URLs, transport mode, schedule, Telegram placeholders)

After setup, run the scraper:
    python apartment_scraper.py --search <profile>
"""

import json
import os
import sys
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).parent
PROFILES_PATH = BASE_DIR / "profiles.json"
MIN_PYTHON = (3, 10)

# pip install name -> import name, where they differ
REQUIRED_PACKAGES = {
    "playwright": "playwright",
    "gspread": "gspread",
    "oauth2client": "oauth2client",
    "pandas": "pandas",
    "numpy": "numpy",
    "requests": "requests",
    "beautifulsoup4": "bs4",
    "schedule": "schedule",
}


def check_python():
    if sys.version_info < MIN_PYTHON:
        print(f"[FAIL] Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required (found {sys.version})")
        sys.exit(1)
    print(f"[OK]   Python {sys.version.split()[0]}")


def check_dependencies():
    missing = []
    for pkg, import_name in REQUIRED_PACKAGES.items():
        import importlib
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"[WARN] Missing packages: {', '.join(missing)}")
        answer = input("       Install them now? [Y/n] ").strip().lower()
        if answer in ("", "y"):
            subprocess.run([sys.executable, "-m", "pip", "install"] + missing, check=True)
            print("[OK]   Packages installed")
        else:
            print("       Install manually: pip install " + " ".join(missing))
            sys.exit(1)
    else:
        print("[OK]   All required packages present")


def check_playwright():
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
            capture_output=True, text=True
        )
        if "chromium" in result.stdout.lower() or result.returncode == 0:
            print("[OK]   Playwright chromium available")
            return
    except Exception:
        pass
    print("[WARN] Playwright chromium browser not installed")
    answer = input("       Install it now? [Y/n] ").strip().lower()
    if answer in ("", "y"):
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
        print("[OK]   Chromium installed")


def check_credentials():
    creds_path = Path(os.environ.get("GOOGLE_CREDS_PATH", BASE_DIR / "google-credentials.json"))
    if creds_path.exists():
        print(f"[OK]   Google credentials: {creds_path.name}")
        return
    print(f"[FAIL] Google credentials not found: {creds_path}")
    print("       Place your service account JSON file in:")
    print(f"       {BASE_DIR}/google-credentials.json")
    print("       (or point GOOGLE_CREDS_PATH env var at it)")
    sys.exit(1)


def check_ors_key():
    if os.environ.get("OPENROUTESERVICE_API_KEY"):
        print("[OK]   OPENROUTESERVICE_API_KEY set")
        return
    print("[WARN] OPENROUTESERVICE_API_KEY not set - commute distance calculation will be skipped")
    print("       Free key: https://openrouteservice.org/dev/#/signup")


def create_profiles_json():
    if PROFILES_PATH.exists():
        with PROFILES_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        names = [p["name"] for p in data["profiles"]]
        print(f"[OK]   profiles.json found with profiles: {', '.join(names)}")
        answer = input("       Add another profile? [y/N] ").strip().lower()
        if answer != "y":
            return
        profiles = data["profiles"]
    else:
        print("\n[SETUP] profiles.json not found - let's create it.")
        print("        You'll need: OLX search URL, Otodom search URL,")
        print("        Google Sheet ID, office address, and city name.")
        profiles = []

    while True:
        print()
        name = input("  Profile name (e.g. 'krakow') or blank to finish: ").strip()
        if not name:
            break
        olx_url     = input(f"  OLX URL for '{name}': ").strip()
        otodom_url  = input(f"  Otodom URL for '{name}': ").strip()
        sheet_id    = input(f"  Google Sheet ID for '{name}': ").strip()
        origin_addr = input(f"  Reference address for '{name}' (for distance calc): ").strip()
        city        = input(f"  City name for '{name}' (e.g. Kraków): ").strip()
        profiles.append({
            "name": name,
            "olx_url": olx_url,
            "otodom_url": otodom_url,
            "sheet_id": sheet_id,
            "origin_address": origin_addr,
            "city": city,
        })
        print(f"  [OK] Profile '{name}' added")

    if not profiles:
        print("[FAIL] No profiles defined. Exiting.")
        sys.exit(1)

    with PROFILES_PATH.open("w", encoding="utf-8") as f:
        json.dump({"profiles": profiles}, f, ensure_ascii=False, indent=2)
    print(f"\n[OK]   profiles.json saved with {len(profiles)} profile(s)")


def create_config_sheets():
    print("\n[SETUP] Initialising config sheets in Google Sheets...")
    # Import after profiles.json is guaranteed to exist
    try:
        from apartment_scraper import _open_spreadsheet, load_config_sheet, load_profiles
    except Exception as e:
        print(f"[FAIL] Could not import apartment_scraper: {e}")
        sys.exit(1)

    for name, profile in load_profiles().items():
        try:
            sp = _open_spreadsheet(profile["sheet_id"])
            load_config_sheet(
                sp, name,
                default_olx_url=profile["olx_url"],
                default_otodom_url=profile["otodom_url"],
                default_origin=profile["origin_address"],
            )
            print(f"  [OK] Config sheet ready for '{name}'")
        except Exception as e:
            print(f"  [WARN] Could not set up config sheet for '{name}': {e}")


def print_next_steps(profiles):
    names = [p["name"] for p in profiles]
    print("\n" + "="*60)
    print("Setup complete! Next steps:")
    print()
    print("1. Run the scraper:")
    for name in names:
        print(f"     python apartment_scraper.py --search {name!r}")
    print()
    print("2. Optional - configure per-profile settings in the")
    print("   'config' tab of each Google Sheet:")
    print("     • Reference Address (distance destination)")
    print("     • Transport Mode (foot-walking / cycling-regular / driving-car)")
    print("     • Schedule (e.g. 'daily 08:00' for automatic runs)")
    print("     • Telegram Bot Token + Chat ID for notifications")
    print("     • Language (en / pl) for Sheet headers, email and Telegram")
    print()
    print("   Email digest fields (sender, recipient, app password, districts)")
    print("   aren't asked here - add them to profiles.json, see profiles.example.json.")
    print()
    print("3. Optional - start the scheduler for automatic runs:")
    print("     python scheduler.py")
    print("="*60)


if __name__ == "__main__":
    print("=" * 60)
    print("apartment_scraper - Setup Assistant")
    print("=" * 60 + "\n")

    check_python()
    check_dependencies()
    check_playwright()
    check_credentials()
    check_ors_key()
    create_profiles_json()
    create_config_sheets()

    with PROFILES_PATH.open(encoding="utf-8") as f:
        profiles = json.load(f)["profiles"]
    print_next_steps(profiles)
