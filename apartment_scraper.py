"""CLI entry point: pick a search profile from profiles.json and run the pipeline for it.

    python apartment_scraper.py --search <profile>

The stages themselves live in pipeline.py.
"""
import argparse
import json
import logging
import os
from pathlib import Path
from typing import List

from playwright.sync_api import sync_playwright

import pipeline
from geo import API_KEY
from logging_config import setup_comprehensive_logging

# setup_comprehensive_logging() (called from main) configures this same logger and
# creates the log/debug folders - importing the module has no side effects.
logger = logging.getLogger("logging_config")


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


def main() -> None:
    global logger
    profiles = load_profiles()
    cli_args = build_arg_parser(list(profiles)).parse_args()
    logger, debug_handler = setup_comprehensive_logging(include_html_debugging=True)
    logger.info("🚀 Starting Web Scraper Session")

    ctx = pipeline.RunContext(args=cli_args, profile=profiles[cli_args.search], debug_handler=debug_handler)
    if not API_KEY:
        logger.warning("⚠️  OPENROUTESERVICE_API_KEY not set in environment. Distance calculation will fail.")
    logger.info(f'Search configuration: {ctx.search} (city: {ctx.city}, office: {ctx.origin})')

    if cli_args.reextract:
        pipeline.run_reextract_mode(ctx)
        return
    if cli_args.calc_distance:
        pipeline.run_distance_mode(ctx)
        return

    with sync_playwright() as playwright:
        pipeline.scrape(ctx, playwright)
    pipeline.enrich_offers(ctx)
    pipeline.deliver_offers(ctx)


if __name__ == "__main__":
    main()
