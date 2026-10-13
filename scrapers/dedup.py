"""Cross-portal duplicate detection for listings found during one run."""
import logging
import re

from parsing.offers import create_offer_key

logger = logging.getLogger("logging_config")


# Global set to track seen offers and prevent duplicates
seen_offers = set()


def is_duplicate_offer(title: str, price: int, link: str = "") -> bool:
    """Check if offer is duplicate - ENHANCED WITH BETTER DETECTION"""
    
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
