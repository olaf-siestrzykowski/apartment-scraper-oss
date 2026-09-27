"""Resilient BeautifulSoup lookups: try several selectors until one matches."""
import re


def find_element_by_selectors(soup, selectors, attribute=None):
    """Try multiple selectors until one returns a result"""
    for selector in selectors:
        try:
            if ':contains(' in selector:
                # Handle CSS :contains() pseudo-selector (not supported by BeautifulSoup)
                import re
                match = re.search(r':contains\("([^"]+)"\)', selector)
                if match:
                    search_text = match.group(1)
                    base_selector = selector.split(':contains(')[0]
                    elements = soup.select(base_selector) if base_selector else soup.find_all()
                    for element in elements:
                        if search_text in element.get_text():
                            if attribute:
                                return element.get(attribute)
                            return element
            else:
                element = soup.select_one(selector)
                if element:
                    if attribute:
                        return element.get(attribute)
                    return element
        except Exception:
            continue
    return None


def find_elements_by_selectors(soup, selectors):
    """Try multiple selectors until one returns results (plural version)"""
    for selector in selectors:
        try:
            elements = soup.select(selector)
            if elements:
                return elements
        except Exception:
            continue
    return []


def _select_one_flexible(container, selector: str):
    """
    BeautifulSoup doesn't support :contains in CSS; handle a couple of common pseudo cases:
      - tag:contains("needle")
      - dd:has(svg) span   (limited support)
    Otherwise, fall back to container.select_one(selector).
    Returns element or None.
    """
    if not selector:
        return None

    # handle :contains("...") optionally with a tag
    if ":contains(" in selector:
        # patterns: 'p:contains("m²")', 'span:contains("zł")'
        m = re.match(r'^\s*([a-zA-Z0-9\-\_\.\*]+)?\s*:\s*contains\(\s*["\'](.+?)["\']\s*\)\s*(.*)$', selector)
        if m:
            tag, needle, tail = m.group(1), m.group(2), (m.group(3) or "").strip()
            needle = needle.strip()
            # search all matching tags (or any tag) containing the needle
            candidates = container.find_all(tag if tag and tag != '*' else True,
                                            string=lambda x: x and needle in x)
            if not candidates:
                # also try by element text (not only exact string node)
                candidates = [el for el in container.find_all(tag if tag and tag != '*' else True)
                              if needle in el.get_text(strip=True)]
            if not candidates:
                return None
            elem = candidates[0]
            if tail:
                # there is a trailing simple descendant like 'span'
                # try to resolve one level 'tail' (only simple tag name)
                tail_tag = tail.split()[0]
                found = elem.select_one(tail_tag) if elem else None
                return found or elem
            return elem

    # limited :has(svg) support for patterns like 'dd:has(svg) span'
    if ":has(" in selector:
        if selector.startswith("dd:has(svg)"):
            dd = next((d for d in container.find_all("dd") if d.find("svg")), None)
            if not dd:
                return None
            if "span" in selector:
                return dd.find("span")
            return dd

    # default
    try:
        return container.select_one(selector)
    except Exception:
        return None


def extract_with_fallback_selectors(container, selectors, *, return_element=False):
    """
    Try multiple selectors (with flexible support) and return first non-empty text (or element).
    This is now used consistently across scrapers.
    """
    if not selectors:
        return None

    for selector in selectors:
        elem = _select_one_flexible(container, selector)
        if elem:
            if return_element:
                return elem
            text = elem.get_text(strip=True)
            if text:
                return text
    return None
