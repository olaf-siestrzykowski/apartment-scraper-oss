"""Address clean-up and street extraction from free text."""
import re
from typing import Optional


def _sanitize_address(addr_text: str):
    if not addr_text:
        return None
    # drop time like 'o 14:16'
    addr = re.sub(r'(?:\bo\s+)?\b\d{1,2}:\d{2}\b', '', addr_text)
    # OLX often shows text like "City, District - Refreshed ..."
    addr = addr.split(" - ")[0]
    addr = re.sub(r'\s+', ' ', addr).strip(" -\u2013")
    return addr or None


# Common Polish words that are NOT street names (false positives)
STREET_BLACKLIST = {
    # Verbs and common phrases
    'znajduje', 'składa', 'posiada', 'oferuje', 'wynajmę', 'wynajmuje',
    'położone', 'położony', 'położona', 'usytuowany', 'usytuowane',
    'mieści', 'lokalizacja', 'lokalizacji', 'mieszkanie', 'mieszkania',
    'pokoje', 'pokój', 'pokoi', 'kuchnia', 'kuchni', 'łazienka', 'łazienki',
    'balkon', 'balkonu', 'piwnica', 'piwnicy', 'garaż', 'garażu',
    'budynek', 'budynku', 'blok', 'bloku', 'kamienica', 'kamienicy',
    'osiedle', 'osiedla', 'osiedlu', 'dzielnica', 'dzielnicy',
    # Adjectives
    'bardzo', 'cicha', 'cichy', 'spokojna', 'spokojny', 'nowe', 'nowy', 'nowa',
    'duże', 'duży', 'duża', 'małe', 'mały', 'mała', 'ładne', 'ładny', 'ładna',
    'jasne', 'jasny', 'jasna', 'widne', 'widny', 'widna', 'przestronne',
    # Other common words
    'views', 'from', 'architecture', 'and', 'the', 'with', 'near',
    'nieruchomości', 'nieruchomość', 'oferta', 'oferty', 'opis',
    'cena', 'ceny', 'koszt', 'koszty', 'opłaty', 'opłata',
    'wynajem', 'wynajmu', 'najem', 'najmu', 'czynsz', 'czynszu',
    'kontakt', 'telefon', 'email', 'więcej', 'informacji',
    'przy', 'obok', 'blisko', 'koło', 'niedaleko', 'pobliżu',
}


def extract_street_from_text(text: str) -> Optional[str]:
    """Extract street name from text (description, title)"""
    if not text or not isinstance(text, str):
        return None

    # Patterns to match Polish street addresses
    patterns = [
        r'ul\.?\s+([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ]?[a-ząćęłńóśźż]+)?)\s*(\d+[a-zA-Z]?)?',
        r'ulica\s+([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ]?[a-ząćęłńóśźż]+)?)\s*(\d+[a-zA-Z]?)?',
        r'al\.?\s+([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ]?[a-ząćęłńóśźż]+)?)\s*(\d+[a-zA-Z]?)?',
        r'alej[aey]?\s+([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻ]?[a-ząćęłńóśźż]+)?)\s*(\d+[a-zA-Z]?)?',
    ]

    for pattern in patterns:
        matches = re.finditer(pattern, text, re.IGNORECASE)
        for match in matches:
            street_name = match.group(1).strip()

            if len(street_name) < 4:
                continue

            # Check if street name is actually a common word (false positive)
            if street_name.lower() in STREET_BLACKLIST:
                continue

            # Check if any word in the street name is blacklisted
            words = street_name.lower().split()
            if any(word in STREET_BLACKLIST for word in words):
                continue

            # Clean the street prefix for geocoding
            full_match = match.group(0).strip()
            cleaned = re.sub(r'^(ul\.?|ulica|al\.?|alej[aey]?)\s+', '', full_match, flags=re.IGNORECASE)

            if len(cleaned) >= 4:
                return cleaned

    return None
