"""Helpers shared by the portal scrapers: scrolling and HTML snapshots."""
import datetime as dt
import logging
import re
import time
from pathlib import Path

logger = logging.getLogger("logging_config")


def scroll_alot(page):
    page.mouse.wheel(10000, 10000)
    time.sleep(0.2)
    page.mouse.wheel(10000, 10000)


def save_html_to_file(html_content, index, url, export_folder="exports/html"):
    """
    Improved HTML-saving function with better error handling and folder structure.

    Args:
        html_content: HTML content to save
        index: Offer index
        url: Source URL
        export_folder: Main export folder

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
