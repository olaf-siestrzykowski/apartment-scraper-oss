#!/usr/bin/env python3
"""
scheduler.py - Reads 'Schedule Days' and 'Schedule Time' from each profile's
               config sheet and runs apartment_scraper.py on the configured schedule.

               On startup it also checks for missed runs (e.g. laptop was off)
               and triggers an immediate catch-up run if needed.

Usage:
    python scheduler.py                    # watches all profiles
    python scheduler.py --profile vika     # watches a single profile
    python scheduler.py --run-now vika     # run immediately then exit

Dependencies:
    pip install schedule

Schedule format in the config sheet:
    Schedule Days (B6):  monday             run every Monday
                         tuesday,friday     run every Tuesday and Friday
                         weekdays           run Mon–Fri
                         weekend            run Sat–Sun
                         (blank)            disabled
    Schedule Time (B7):  08:00              time to run (single value)
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import schedule
except ImportError:
    print("Missing dependency: pip install schedule")
    sys.exit(1)

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "scheduler_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [scheduler] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scheduler")


# ── State persistence (last-run timestamps) ──────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        with STATE_FILE.open(encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_last_run(profile_name: str) -> None:
    state = _load_state()
    state[profile_name] = datetime.now().isoformat()
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _get_last_run(profile_name: str) -> datetime | None:
    val = _load_state().get(profile_name)
    return datetime.fromisoformat(val) if val else None


# ── Profile loading ───────────────────────────────────────────────────────────

def load_profiles() -> dict:
    profiles_path = BASE_DIR / "profiles.json"
    if not profiles_path.exists():
        raise FileNotFoundError("profiles.json not found. Run: python setup.py")
    with profiles_path.open(encoding="utf-8") as f:
        data = json.load(f)
    return {p["name"]: p for p in data["profiles"]}


# ── Google Sheets config reading ──────────────────────────────────────────────

def _open_spreadsheet(sheet_id: str):
    from oauth2client.service_account import ServiceAccountCredentials
    import gspread

    creds_path = os.environ.get("GOOGLE_CREDS_PATH", str(BASE_DIR / "google-credentials.json"))
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(str(creds_path), scope)
    client = gspread.authorize(creds)
    return client.open_by_key(sheet_id)


def read_schedule_from_config(sheet_id: str) -> tuple[str, str]:
    """Return (days_str, time_str) from the profile's config sheet, or ('', '')."""
    days_str = ""
    time_str = ""
    try:
        sp = _open_spreadsheet(sheet_id)
        ws = sp.worksheet("config")
        for row in ws.get_all_values()[1:]:
            if len(row) >= 2:
                if row[0] == "Schedule Days":
                    days_str = row[1].strip()
                elif row[0] == "Schedule Time":
                    time_str = row[1].strip()
    except Exception as e:
        logger.warning(f"Could not read schedule for sheet {sheet_id}: {e}")
    return days_str, time_str


# ── Scraper runner ────────────────────────────────────────────────────────────

def run_scraper(profile_name: str, sheet_id: str | None = None) -> None:
    logger.info(f"▶ Running scraper for profile '{profile_name}'")
    script = BASE_DIR / "apartment_scraper.py"
    result = subprocess.run([sys.executable, str(script), "--search", profile_name])
    if result.returncode == 0:
        logger.info(f"✅ Scraper finished for '{profile_name}'")
    else:
        logger.error(f"❌ Scraper exited with code {result.returncode} for '{profile_name}'")
    _save_last_run(profile_name)

    if sheet_id:
        logger.info(f"🔄 Re-reading schedule from config sheet for '{profile_name}'")
        days_str, time_str = read_schedule_from_config(sheet_id)
        if days_str:
            register_schedule(profile_name, days_str, time_str, sheet_id)
        else:
            schedule.clear(profile_name)
            logger.info(f"⏭ '{profile_name}': schedule disabled in config sheet")


# ── Schedule parsing ──────────────────────────────────────────────────────────

WEEKDAYS = {
    "monday":    lambda: schedule.every().monday,
    "tuesday":   lambda: schedule.every().tuesday,
    "wednesday": lambda: schedule.every().wednesday,
    "thursday":  lambda: schedule.every().thursday,
    "friday":    lambda: schedule.every().friday,
    "saturday":  lambda: schedule.every().saturday,
    "sunday":    lambda: schedule.every().sunday,
}

WEEKDAY_NUMS = {
    "monday": 0, "tuesday": 1, "wednesday": 2,
    "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
}

ALIASES = {
    "weekdays": ["monday", "tuesday", "wednesday", "thursday", "friday"],
    "weekend":  ["saturday", "sunday"],
    "daily":    ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"],
}


def _normalize_time(time_str: str) -> str:
    """Zero-pad H:MM / H:MM:SS into HH:MM(:SS) - the `schedule` lib rejects '9:00'."""
    parts = time_str.split(":")
    try:
        parts = [f"{int(p):02d}" for p in parts]
    except ValueError:
        return time_str
    return ":".join(parts)


def _expand_days(days_str: str) -> list[str]:
    days = []
    for token in days_str.lower().replace(" ", "").split(","):
        if token in ALIASES:
            days.extend(ALIASES[token])
        elif token in WEEKDAYS:
            days.append(token)
        elif token:
            logger.warning(f"Unknown day token: {token!r}")
    return days


def register_schedule(profile_name: str, days_str: str, time_str: str, sheet_id: str) -> bool:
    """Parse days/time and register jobs. Returns True if any were registered."""
    if not days_str:
        return False

    time_str = _normalize_time(time_str.strip() or "08:00")
    days = _expand_days(days_str)

    if not days:
        logger.warning(f"No valid days for '{profile_name}': {days_str!r}")
        return False

    schedule.clear(profile_name)
    registered = 0
    for day in days:
        try:
            WEEKDAYS[day]().at(time_str).do(run_scraper, profile_name, sheet_id).tag(profile_name)
            logger.info(f"📅 '{profile_name}' scheduled: every {day.capitalize()} at {time_str}")
            registered += 1
        except Exception as e:
            logger.warning(f"Failed to schedule {day} at {time_str} for '{profile_name}': {e}")

    return registered > 0


# ── Catch-up logic ────────────────────────────────────────────────────────────

def _most_recent_scheduled_time(days: list[str], time_str: str) -> datetime | None:
    """Return the most recent past datetime matching the schedule, or None."""
    now = datetime.now()
    try:
        h, m = map(int, time_str.split(":"))
    except ValueError:
        return None

    best = None
    for day in days:
        target_wd = WEEKDAY_NUMS[day]
        days_ago = (now.weekday() - target_wd) % 7
        candidate = (now - timedelta(days=days_ago)).replace(
            hour=h, minute=m, second=0, microsecond=0
        )
        if candidate > now:
            candidate -= timedelta(weeks=1)
        if best is None or candidate > best:
            best = candidate
    return best


def check_catchup(profile_name: str, days: list[str], time_str: str) -> bool:
    """Return True if a scheduled run was missed and a catch-up run is needed."""
    if not days:
        return False
    scheduled = _most_recent_scheduled_time(days, time_str)
    if scheduled is None:
        return False
    last_run = _get_last_run(profile_name)
    if last_run is None or last_run < scheduled:
        logger.info(
            f"⏰ '{profile_name}': missed run at {scheduled.strftime('%A %Y-%m-%d %H:%M')} "
            f"(last run: {last_run.strftime('%Y-%m-%d %H:%M') if last_run else 'never'}) - catching up"
        )
        return True
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Apartment scraper scheduler")
    parser.add_argument("--profile", help="Watch only this profile (default: all)")
    parser.add_argument("--run-now", metavar="PROFILE", help="Run profile immediately and exit")
    args = parser.parse_args()

    if args.run_now:
        run_scraper(args.run_now)
        return

    profiles = load_profiles()
    target_profiles = {args.profile: profiles[args.profile]} if args.profile else profiles

    registered = 0
    for name, profile in target_profiles.items():
        days_str, time_str = read_schedule_from_config(profile["sheet_id"])
        if not days_str:
            logger.info(f"⏭ '{name}': no schedule set (edit the config sheet to enable)")
            continue

        time_str = time_str or "08:00"
        days = _expand_days(days_str)

        # Catch-up: run immediately if a scheduled slot was missed
        if check_catchup(name, days, time_str):
            run_scraper(name, profile["sheet_id"])

        if register_schedule(name, days_str, time_str, profile["sheet_id"]):
            registered += 1

    if registered == 0:
        logger.info("No profiles have a schedule configured. Set 'Schedule Days' in each profile's config sheet.")
        return

    logger.info(f"Watching {registered} profile(s). Press Ctrl+C to stop.")
    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
