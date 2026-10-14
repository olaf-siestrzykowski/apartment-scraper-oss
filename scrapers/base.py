"""The interface every portal scraper implements, so the pipeline can treat them alike."""
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

import pandas as pd


@runtime_checkable
class PortalScraper(Protocol):
    """One listing portal (OLX, Otodom, ...).

    name   - short id; prefix of saved offer HTML files and of the "<name>_url" config key
    domain - matched against an offer's Link to pick the scraper for its detail page
    """

    name: str
    domain: str

    def collect_listings(self, context, url: str, max_pages: Optional[int] = None) -> List[Dict[str, Any]]:
        """Walk the search result pages in a Playwright context and return one row per offer."""
        ...

    def extract_details(self, page, offers_df: pd.DataFrame, index, current_num: int, total_num: int,
                        is_retry: bool = False) -> bool:
        """Open one offer page and fill its details into offers_df (in place). True on success."""
        ...

    def extract_from_soup(self, soup, index) -> Dict[str, Any]:
        """Parse details from a saved offer page (used by --reextract)."""
        ...
