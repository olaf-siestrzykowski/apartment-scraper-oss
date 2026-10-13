"""OLX: search result pages and offer detail pages."""
import logging
import random
import re
import time

from bs4 import BeautifulSoup

from logging_config import log_extraction_attempt, log_extraction_result
from parsing.dates import parse_polish_date
from parsing.prices import extract_price_from_text
from parsing.selectors import find_element_by_selectors
from scrapers.common import save_html_to_file
from scrapers.dedup import is_duplicate_offer

logger = logging.getLogger("logging_config")


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


def scrape_olx(html_content):
    """
    Improved OLX scraping function with more accurate extraction of:
    - Additional_value (additional fees)
    - Area (floor area)
    - Date (listing date)
    """
    rows = []

    if not html_content or len(html_content) < 1000:
        logger.warning("HTML content too short")
        return []

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
        return []

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
            
            rows.append(offer_dict)
            
            added_count += 1
            
            logger.debug(f"✅ OLX offer {i+1}: {title[:50]}... | {base_price} zł | {area} m² | {address}")
        
        except Exception as e:
            logger.error(f"❌ Error processing OLX offer {i+1}: {e}")
            skipped_count += 1
            continue
    
    logger.info(f"📊 OLX scraping complete: Added={added_count}, Skipped={skipped_count}")
    return rows


def extract_olx_details(page, offers_df, index, current_num, total_num, is_retry=False, debug_handler=None):
    """Extract detailed information from a single OLX offer page into offers_df (in place)"""

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
            logger.debug("❌ Title not found")

        # Extract price using selectors
        price_element = find_element_by_selectors(soup, OLX_SELECTORS['price'])
        if price_element:
            price_text = price_element.get_text().strip()
            offers_df.at[index, "Price_Detail"] = price_text
            extracted_values["Price_Detail"] = price_text
            logger.debug(f"✅ Price extracted: {price_text}")
        else:
            missing_fields.append("Price")
            logger.debug("❌ Price not found")

        # Extract area using selectors
        area_element = find_element_by_selectors(soup, OLX_SELECTORS['area'])
        if area_element:
            area_text = area_element.get_text().strip()
            offers_df.at[index, "Area_Detail"] = area_text
            extracted_values["Area_Detail"] = area_text
            logger.debug(f"✅ Area extracted: {area_text}")
        else:
            missing_fields.append("Area")
            logger.debug("❌ Area not found")

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
            logger.debug("❌ Description not found")

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
                logger.debug("❌ Location not found")

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
                logger.debug("⚠️ No parameters container found")

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
