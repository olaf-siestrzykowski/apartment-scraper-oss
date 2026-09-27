"""Offer identity and validation."""
import re


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
