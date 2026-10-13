"""Otodom: search result pages and offer detail pages."""
import logging
import random
import re
import time

from bs4 import BeautifulSoup

from parsing.addresses import _sanitize_address
from parsing.dates import parse_polish_date
from parsing.prices import _number_from_text
from parsing.selectors import (
    _select_one_flexible,
    extract_with_fallback_selectors,
    find_element_by_selectors,
    find_elements_by_selectors,
)
from scrapers.common import save_html_to_file
from scrapers.dedup import is_duplicate_offer

logger = logging.getLogger("logging_config")


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


def scrape_otodom_working(html_content):
    """
    Robust Otodom scraper using updated selectors + date/area/address extraction.
    Measures per-offer and per-stage timings and logs the slowest stages.
    """
    rows = []

    if not html_content or len(html_content) < 1000:
        logger.warning("HTML content too short")
        return []

    soup = BeautifulSoup(html_content, "html.parser")

    # containers
    offers_found = []
    for selector in OTODOM_LISTING_SELECTORS.get("container", []):
        offers_found = soup.select(selector)
        if offers_found:
            break

    logger.info(f"Found {len(offers_found)} potential Otodom offers")
    if not offers_found:
        return []

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
            rows.append(offer_dict)
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

    return rows


def extract_otodom_details(page, offers_df, index, current_num, total_num, is_retry=False):
    """Extract detailed information from a single Otodom offer page into offers_df (in place)"""

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
