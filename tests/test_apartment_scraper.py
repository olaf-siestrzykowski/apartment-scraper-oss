"""
Comprehensive unit tests for apartment_scraper.py

Tests cover:
- Price parsing functions
- Address sanitization
- Polish date parsing
- Duplicate detection
- Cost extraction from descriptions
- Offer validation
- Link validation
- Selector helper functions
"""

import pandas as pd
import pytest
import sys
import os
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import apartment_scraper as ms
from parsing import addresses as parsing_addresses  # noqa: E402
from parsing import dates as parsing_dates  # noqa: E402
from parsing import description as parsing_description  # noqa: E402
from parsing import offers as parsing_offers  # noqa: E402
from parsing import prices as parsing_prices  # noqa: E402
from parsing import selectors as parsing_selectors  # noqa: E402


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def reset_seen_offers():
    """Reset the global seen_offers set before each test"""
    ms.seen_offers = set()
    yield
    ms.seen_offers = set()


@pytest.fixture
def sample_html():
    """Sample HTML for BeautifulSoup testing"""
    return """
    <html>
        <body>
            <div class="offer-container">
                <h2 class="offer-title">Test Offer</h2>
                <span class="price">2,500 zł</span>
                <p class="description">Piękne mieszkanie w centrum</p>
                <div class="details">
                    <span data-testid="area">50 m²</span>
                </div>
            </div>
        </body>
    </html>
    """


# =============================================================================
# TEST extract_price_from_text
# =============================================================================

class TestExtractPriceFromText:
    """Test suite for extract_price_from_text (used to parse listing prices)"""

    def test_parse_price_standard_polish_format(self):
        """Test parsing standard Polish price format with comma as decimal"""
        assert parsing_prices.extract_price_from_text("2 700,50 zł") == 2700.50
        assert parsing_prices.extract_price_from_text("1 999,99 zł") == 1999.99

    def test_parse_price_dot_thousands_comma_decimal(self):
        """Test parsing format with dot as thousands and comma as decimal"""
        assert parsing_prices.extract_price_from_text("2.700,50 zł") == 2700.50
        assert parsing_prices.extract_price_from_text("12.999,99 zł") == 12999.99

    def test_parse_price_space_thousands_comma_decimal(self):
        """Test parsing format with space as thousands separator"""
        assert parsing_prices.extract_price_from_text("2 700,50 zł") == 2700.50
        assert parsing_prices.extract_price_from_text("15 000,00 zł") == 15000.00

    def test_parse_price_only_comma_as_decimal(self):
        """Test parsing with only comma (treated as decimal separator)"""
        assert parsing_prices.extract_price_from_text("1999,50 zł") == 1999.50
        assert parsing_prices.extract_price_from_text("500,25 zł") == 500.25

    def test_parse_price_only_dot_as_decimal(self):
        """Test parsing with only dot (ambiguous - depends on digit count)"""
        assert parsing_prices.extract_price_from_text("1999.99 zł") == 1999.99  # decimal
        assert parsing_prices.extract_price_from_text("2.500 zł") == 2500.0  # thousands (3 digits after dot)

    def test_parse_price_no_decimals(self):
        """Test parsing integer prices"""
        assert parsing_prices.extract_price_from_text("2700 zł") == 2700.0
        assert parsing_prices.extract_price_from_text("1999 zł") == 1999.0

    def test_parse_price_with_nbsp(self):
        """Test parsing with non-breaking space (common in web scraping)"""
        price_with_nbsp = "2\u00A0700,50 zł"
        assert parsing_prices.extract_price_from_text(price_with_nbsp) == 2700.50

    def test_parse_price_with_extra_text(self):
        """Test parsing when price is embedded in other text"""
        assert parsing_prices.extract_price_from_text("Cena: 2 700 zł/miesiąc") == 2700.0
        assert parsing_prices.extract_price_from_text("Od 1 500,50 zł") == 1500.50

    def test_parse_price_empty_string_returns_zero(self):
        """Test that empty string returns 0.0"""
        assert parsing_prices.extract_price_from_text("") == 0.0

    def test_parse_price_none_returns_zero(self):
        """Test that None returns 0.0"""
        assert parsing_prices.extract_price_from_text(None) == 0.0

    def test_parse_price_no_digits_returns_zero(self):
        """Test that text without digits returns 0.0"""
        assert parsing_prices.extract_price_from_text("zł") == 0.0
        assert parsing_prices.extract_price_from_text("Cena do uzgodnienia") == 0.0

    def test_parse_price_both_separators_dot_last(self):
        """Test when dot appears after comma (dot is decimal)"""
        # This is an edge case: "1,234.56" format
        assert parsing_prices.extract_price_from_text("1,234.56 zł") == 1234.56

    @pytest.mark.parametrize("price_text,expected", [
        ("3 200 zł", 3200.0),
        ("2.700,50 zł", 2700.50),
        ("1 999.99 zł", 1999.99),
        ("999", 999.0),
        ("5,5 zł", 5.5),
        ("10.000", 10000.0),
    ])
    def test_parse_price_parametrized(self, price_text, expected):
        """Parametrized test for various price formats"""
        assert parsing_prices.extract_price_from_text(price_text) == expected


# =============================================================================
# TEST _number_from_text
# =============================================================================

class TestNumberFromText:
    """Test suite for _number_from_text helper function"""

    def test_extract_integer_from_text(self):
        """Test extracting integer from text"""
        assert parsing_prices._number_from_text("50 m²", allow_float=False) == 50
        assert parsing_prices._number_from_text("Area: 75", allow_float=False) == 75

    def test_extract_float_from_text(self):
        """Test extracting float from text"""
        assert parsing_prices._number_from_text("50.5 m²") == 50.5
        assert parsing_prices._number_from_text("75,25 m²") == 75.25

    def test_extract_with_comma_decimal(self):
        """Test comma is treated as decimal separator"""
        assert parsing_prices._number_from_text("15,5") == 15.5
        assert parsing_prices._number_from_text("100,99") == 100.99

    def test_extract_first_number_when_multiple(self):
        """Test that first number is extracted when multiple numbers present"""
        assert parsing_prices._number_from_text("50 m² + 10 m² balkon") == 50.0

    def test_empty_text_returns_default(self):
        """Test that empty text returns default value"""
        assert parsing_prices._number_from_text("", default=0) == 0
        assert parsing_prices._number_from_text("", default=100) == 100

    def test_none_returns_default(self):
        """Test that None returns default value"""
        assert parsing_prices._number_from_text(None, default=0) == 0
        assert parsing_prices._number_from_text(None, default=-1) == -1

    def test_no_number_returns_default(self):
        """Test that text without numbers returns default"""
        assert parsing_prices._number_from_text("brak danych", default=0) == 0
        assert parsing_prices._number_from_text("N/A", default=999) == 999

    def test_allow_float_false_converts_to_int(self):
        """Test that allow_float=False converts to integer"""
        assert parsing_prices._number_from_text("50.9", allow_float=False) == 50
        assert parsing_prices._number_from_text("99.999", allow_float=False) == 99

    @pytest.mark.parametrize("text,expected,allow_float", [
        ("123", 123.0, True),
        ("123", 123, False),
        ("45.67", 45.67, True),
        ("45.67", 45, False),
        ("Area: 85,5 m²", 85.5, True),
    ])
    def test_number_from_text_parametrized(self, text, expected, allow_float):
        """Parametrized test for number extraction"""
        assert parsing_prices._number_from_text(text, allow_float=allow_float) == expected


# =============================================================================
# TEST _sanitize_address
# =============================================================================

class TestSanitizeAddress:
    """Test suite for _sanitize_address function"""

    def test_sanitize_removes_time(self):
        """Test that time patterns are removed from address"""
        assert parsing_addresses._sanitize_address("Warszawa, Mokotów o 14:16") == "Warszawa, Mokotów"
        assert parsing_addresses._sanitize_address("Wola o 9:30") == "Wola"

    def test_sanitize_removes_refresh_info(self):
        """Test that 'Odświeżono' information is removed"""
        assert parsing_addresses._sanitize_address("Warszawa, Wola - Odświeżono dzisiaj") == "Warszawa, Wola"
        assert parsing_addresses._sanitize_address("Mokotów - Odświeżono wczoraj") == "Mokotów"

    def test_sanitize_normalizes_whitespace(self):
        """Test that multiple spaces are normalized to single space"""
        assert parsing_addresses._sanitize_address("Warszawa,    Wola") == "Warszawa, Wola"
        assert parsing_addresses._sanitize_address("  Mokotów  ") == "Mokotów"

    def test_sanitize_strips_dashes(self):
        """Test that leading/trailing dashes are removed"""
        assert parsing_addresses._sanitize_address("- Warszawa -") == "Warszawa"
        assert parsing_addresses._sanitize_address("Wola -") == "Wola"

    def test_sanitize_strips_en_dash(self):
        """Test that en-dash (–) is also stripped"""
        assert parsing_addresses._sanitize_address("Warszawa – Wola") == "Warszawa – Wola"
        assert parsing_addresses._sanitize_address("– Mokotów –") == "Mokotów"

    def test_sanitize_empty_string_returns_none(self):
        """Test that empty string returns None"""
        assert parsing_addresses._sanitize_address("") is None
        assert parsing_addresses._sanitize_address("   ") is None

    def test_sanitize_none_returns_none(self):
        """Test that None input returns None"""
        assert parsing_addresses._sanitize_address(None) is None

    def test_sanitize_only_whitespace_and_dashes_returns_none(self):
        """Test that string with only whitespace/dashes returns None"""
        assert parsing_addresses._sanitize_address("  -  ") is None
        assert parsing_addresses._sanitize_address("---") is None

    def test_sanitize_complex_address(self):
        """Test sanitization of complex real-world address"""
        input_addr = "Warszawa, Mokotów - Odświeżono dzisiaj o 15:30"
        expected = "Warszawa, Mokotów"
        assert parsing_addresses._sanitize_address(input_addr) == expected

    @pytest.mark.parametrize("input_addr,expected", [
        ("Warszawa, Wola", "Warszawa, Wola"),
        ("Mokotów o 14:00", "Mokotów"),
        ("  Śródmieście  -  ", "Śródmieście"),
        ("Praga - Odświeżono", "Praga"),
        ("", None),
    ])
    def test_sanitize_address_parametrized(self, input_addr, expected):
        """Parametrized test for address sanitization"""
        assert parsing_addresses._sanitize_address(input_addr) == expected


# =============================================================================
# TEST parse_polish_date
# =============================================================================

class TestParsePolishDate:
    """Test suite for parse_polish_date function"""

    def test_parse_dzisiaj_returns_today(self):
        """Test that 'Dzisiaj' returns today's date"""
        today = datetime.today().strftime("%Y-%m-%d")
        assert parsing_dates.parse_polish_date("Dzisiaj") == today
        assert parsing_dates.parse_polish_date("Dzisiaj o 15:04") == today

    def test_parse_wczoraj_returns_yesterday(self):
        """Test that 'Wczoraj' returns yesterday's date"""
        yesterday = (datetime.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        assert parsing_dates.parse_polish_date("Wczoraj") == yesterday
        assert parsing_dates.parse_polish_date("Wczoraj o 14:30") == yesterday

    def test_parse_odswiezono_dzisiaj(self):
        """Test 'Odświeżono Dzisiaj' format"""
        today = datetime.today().strftime("%Y-%m-%d")
        assert parsing_dates.parse_polish_date("Odświeżono Dzisiaj o 16:05") == today

    def test_parse_days_ago_singular(self):
        """Test 'X dzień temu' format (singular)"""
        expected = (datetime.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        assert parsing_dates.parse_polish_date("1 dzień temu") == expected

    def test_parse_days_ago_plural(self):
        """Test 'X dni temu' format (plural)"""
        expected_3 = (datetime.today() - timedelta(days=3)).strftime("%Y-%m-%d")
        expected_7 = (datetime.today() - timedelta(days=7)).strftime("%Y-%m-%d")
        assert parsing_dates.parse_polish_date("3 dni temu") == expected_3
        assert parsing_dates.parse_polish_date("7 dni temu") == expected_7

    def test_parse_days_ago_dnia_form(self):
        """Test 'X dnia temu' format (genitive case)"""
        expected = (datetime.today() - timedelta(days=2)).strftime("%Y-%m-%d")
        assert parsing_dates.parse_polish_date("2 dnia temu") == expected

    def test_parse_full_polish_date(self):
        """Test full date format '16 listopada 2025'"""
        assert parsing_dates.parse_polish_date("16 listopada 2025") == "2025-11-16"
        assert parsing_dates.parse_polish_date("1 stycznia 2025") == "2025-01-01"
        assert parsing_dates.parse_polish_date("31 grudnia 2024") == "2024-12-31"

    def test_parse_date_with_odswiezono_prefix(self):
        """Test 'Odświeżono dnia 17 listopada 2025' format"""
        assert parsing_dates.parse_polish_date("Odświeżono dnia 17 listopada 2025") == "2025-11-17"

    def test_parse_all_polish_months(self):
        """Test all Polish month names"""
        months = [
            ("15 stycznia 2025", "2025-01-15"),
            ("20 lutego 2025", "2025-02-20"),
            ("10 marca 2025", "2025-03-10"),
            ("5 kwietnia 2025", "2025-04-05"),
            ("1 maja 2025", "2025-05-01"),
            ("15 czerwca 2025", "2025-06-15"),
            ("20 lipca 2025", "2025-07-20"),
            ("10 sierpnia 2025", "2025-08-10"),
            ("1 września 2025", "2025-09-01"),
            ("15 października 2025", "2025-10-15"),
            ("20 listopada 2025", "2025-11-20"),
            ("25 grudnia 2025", "2025-12-25"),
        ]
        for date_text, expected in months:
            assert parsing_dates.parse_polish_date(date_text) == expected

    def test_parse_empty_string_returns_none(self):
        """Test that empty string returns None"""
        assert parsing_dates.parse_polish_date("") is None

    def test_parse_none_returns_none(self):
        """Test that None returns None"""
        assert parsing_dates.parse_polish_date(None) is None

    def test_parse_invalid_date_returns_none(self):
        """Test that invalid date format returns None"""
        assert parsing_dates.parse_polish_date("invalid date") is None
        assert parsing_dates.parse_polish_date("32 stycznia 2025") is None  # Invalid day

    def test_parse_case_insensitive(self):
        """Test that parsing is case insensitive"""
        today = datetime.today().strftime("%Y-%m-%d")
        assert parsing_dates.parse_polish_date("DZISIAJ") == today
        assert parsing_dates.parse_polish_date("DzIsIaJ") == today

    @pytest.mark.parametrize("date_text", [
        "Dzisiaj",
        "dzisiaj o 10:00",
        "DZISIAJ O 15:30",
        "Odświeżono Dzisiaj",
    ])
    def test_parse_today_variations(self, date_text):
        """Test various 'today' format variations"""
        today = datetime.today().strftime("%Y-%m-%d")
        assert parsing_dates.parse_polish_date(date_text) == today


# =============================================================================
# TEST create_offer_key
# =============================================================================

class TestCreateOfferKey:
    """Test suite for create_offer_key function"""

    def test_create_key_removes_special_characters(self):
        """Test that special characters are removed from title"""
        key = parsing_offers.create_offer_key("Mieszkanie 2-pokojowe!", 2500)
        assert "!" not in key
        assert "-" not in key

    def test_create_key_converts_to_lowercase(self):
        """Test that title is converted to lowercase"""
        key = parsing_offers.create_offer_key("MIESZKANIE WARSZAWA", 2500)
        assert key == key.lower()

    def test_create_key_removes_common_words(self):
        """Test that common Polish words are filtered out"""
        key = parsing_offers.create_offer_key("Mieszkanie do wynajmu w Warszawie", 2500)
        assert "mieszkanie" not in key
        assert "wynajem" not in key

    def test_create_key_includes_price(self):
        """Test that price is included in the key"""
        key = parsing_offers.create_offer_key("Test Offer", 2500)
        assert "2500" in key

    def test_create_key_with_olx_link_uses_link_id(self):
        """Test that OLX link ID is extracted and used"""
        link = "https://www.olx.pl/oferta/mieszkanie-2-pokojowe-123456.html"
        key = parsing_offers.create_offer_key("Test", 2500, link)
        assert "link_123456" in key
        assert "2500" in key

    def test_create_key_with_otodom_link_uses_link_id(self):
        """Test that Otodom link ID is extracted and used"""
        link = "https://www.otodom.pl/oferta/mieszkanie-warszawa-wola-ID12345"
        key = parsing_offers.create_offer_key("Test", 2500, link)
        assert "link_mieszkanie-warszawa-wola-ID12345" in key

    def test_create_key_without_link_uses_title(self):
        """Test that without link, title-based key is created"""
        key = parsing_offers.create_offer_key("Piękne Nowoczesne Mieszkanie Centrum", 3000)
        assert "title_" in key
        assert "3000" in key

    def test_create_key_limits_words(self):
        """Test that only first 4 meaningful words are used"""
        long_title = "Słowo1 Słowo2 Słowo3 Słowo4 Słowo5 Słowo6 Słowo7"
        key = parsing_offers.create_offer_key(long_title, 2000)
        # Should have max 4 words (after filtering common words)
        word_count = len([w for w in key.split("_") if w.isalpha()])
        assert word_count <= 5  # "title" + up to 4 words

    def test_create_key_handles_short_title(self):
        """Test handling of very short titles"""
        key = parsing_offers.create_offer_key("M2", 1500)
        assert "1500" in key
        assert key.startswith("title_")

    @pytest.mark.parametrize("title,price,expected_contains", [
        ("Test Mieszkanie", 2000, "2000"),
        ("Pokój studencki", 1000, "1000"),
        ("Apartament lux", 5000, "5000"),
    ])
    def test_create_offer_key_parametrized(self, title, price, expected_contains):
        """Parametrized test for offer key creation"""
        key = parsing_offers.create_offer_key(title, price)
        assert expected_contains in key


# =============================================================================
# TEST is_duplicate_offer
# =============================================================================

class TestIsDuplicateOffer:
    """Test suite for is_duplicate_offer function"""

    def test_exact_link_duplicate_detected(self, reset_seen_offers):
        """Test that exact same link is detected as duplicate"""
        link = "https://www.olx.pl/oferta/test-123456.html"
        assert ms.is_duplicate_offer("Test", 2000, link) is False
        assert ms.is_duplicate_offer("Test", 2000, link) is True

    def test_same_title_and_price_is_duplicate(self, reset_seen_offers):
        """Test that same title+price combination is duplicate"""
        assert ms.is_duplicate_offer("Mieszkanie 2-pokojowe", 2500) is False
        assert ms.is_duplicate_offer("Mieszkanie 2-pokojowe", 2500) is True

    def test_different_price_not_duplicate(self, reset_seen_offers):
        """Test that same title but different price is not duplicate"""
        assert ms.is_duplicate_offer("Mieszkanie Wola", 2000) is False
        assert ms.is_duplicate_offer("Mieszkanie Wola", 2500) is False

    def test_different_title_not_duplicate(self, reset_seen_offers):
        """Test that different title but same price is not duplicate"""
        assert ms.is_duplicate_offer("Mieszkanie Wola", 2000) is False
        assert ms.is_duplicate_offer("Mieszkanie Mokotów", 2000) is False

    def test_link_takes_precedence_over_title(self, reset_seen_offers):
        """Test that link-based deduplication takes precedence"""
        link1 = "https://www.olx.pl/oferta/test-111111.html"
        link2 = "https://www.olx.pl/oferta/test-222222.html"

        # Same title and price but different links - should not be duplicate
        assert ms.is_duplicate_offer("Test", 2000, link1) is False
        assert ms.is_duplicate_offer("Test", 2000, link2) is False

        # Same link - should be duplicate
        assert ms.is_duplicate_offer("Different Title", 3000, link1) is True

    def test_location_similarity_limit(self, reset_seen_offers):
        """Test that price+location similarity is limited to 2 offers"""
        # Add first two similar offers
        title1 = "Mieszkanie 2-pokojowe w Warszawie, Wola, ul. Długa"
        title2 = "Mieszkanie 3-pokojowe w Warszawie, Wola, ul. Krótka"
        title3 = "Mieszkanie 1-pokojowe w Warszawie, Wola, ul. Średnia"

        assert ms.is_duplicate_offer(title1, 2000) is False
        assert ms.is_duplicate_offer(title2, 2000) is False
        # Third one with same price and location should be detected as duplicate
        assert ms.is_duplicate_offer(title3, 2000) is True

    def test_no_link_uses_content_key(self, reset_seen_offers):
        """Test that without link, content-based key is used"""
        title = "Piękne Mieszkanie Centrum"
        assert ms.is_duplicate_offer(title, 2500) is False
        assert ms.is_duplicate_offer(title, 2500) is True

    def test_reset_clears_duplicates(self):
        """Test that resetting seen_offers allows re-adding same offer"""
        ms.seen_offers = set()
        assert ms.is_duplicate_offer("Test", 2000) is False
        assert ms.is_duplicate_offer("Test", 2000) is True

        # Reset
        ms.seen_offers = set()
        assert ms.is_duplicate_offer("Test", 2000) is False


# =============================================================================
# TEST extract_custom_fields_from_opis
# =============================================================================

class TestExtractCustomFieldsFromOpis:
    """Test suite for extract_custom_fields_from_opis function"""

    def test_empty_description_returns_empty_dict(self):
        assert parsing_description.extract_custom_fields_from_opis("") == {}
        assert parsing_description.extract_custom_fields_from_opis(None) == {}

    def test_deposit_extraction(self):
        result = parsing_description.extract_custom_fields_from_opis("Kaucja zwrotna: 3500 zł")
        assert result["Deposit_PLN"] == 3500.0

    def test_min_period_minimum_form(self):
        result = parsing_description.extract_custom_fields_from_opis("Umowa na minimum 12 miesięcy")
        assert result["Min_Period_Months"] == 12

    def test_min_period_years_converted_to_months(self):
        result = parsing_description.extract_custom_fields_from_opis("Wynajmę na min 1 rok")
        assert result["Min_Period_Months"] == 12

    def test_min_period_minimalny_adjective_form(self):
        """Regression test: 'minimalny' (adjective) wasn't matched by the
        minimum/min-only patterns - a common real listing phrasing."""
        result = parsing_description.extract_custom_fields_from_opis("Minimalny okres najmu: 2 lata.")
        assert result["Min_Period_Months"] == 24

    def test_balcony_detected(self):
        result = parsing_description.extract_custom_fields_from_opis("Mieszkanie z dużym balkonem")
        assert result["Balcony"] == "Yes"

    def test_air_conditioning_detected(self):
        result = parsing_description.extract_custom_fields_from_opis("W mieszkaniu jest klimatyzacja")
        assert result["Air_Conditioning"] == "Yes"

    def test_non_smoking_detected(self):
        result = parsing_description.extract_custom_fields_from_opis("Mieszkanie tylko dla niepalących")
        assert result["Non_Smoking"] == "Yes"

    def test_no_pets_excluded(self):
        result = parsing_description.extract_custom_fields_from_opis("Niestety bez zwierząt")
        assert result["No_Pets_Desc"] == "Yes"

    def test_pets_accepted(self):
        result = parsing_description.extract_custom_fields_from_opis("Zwierzęta mile widziane")
        assert result["No_Pets_Desc"] == "No (pets accepted)"

    def test_washing_machine_detected(self):
        result = parsing_description.extract_custom_fields_from_opis("W łazience znajduje się pralka")
        assert result["Washing_Machine"] == "Yes"

    def test_direct_from_owner_detected(self):
        result = parsing_description.extract_custom_fields_from_opis("Wynajmę bezpośrednio, bez pośredników")
        assert result["Direct_From_Owner"] == "Yes"

    def test_target_tenant_couple(self):
        result = parsing_description.extract_custom_fields_from_opis("Idealne dla pary")
        assert "couple" in result["Target_Tenant"]

    def test_no_matches_returns_empty_dict(self):
        result = parsing_description.extract_custom_fields_from_opis("Ładne mieszkanie w centrum miasta.")
        assert result == {}


# =============================================================================
# TEST validate_offer_data
# =============================================================================

class TestValidateOfferData:
    """Test suite for validate_offer_data function"""

    def test_validate_valid_offer_returns_true(self):
        """Test that valid offer passes validation"""
        offer = {
            "Name": "Mieszkanie 2-pokojowe",
            "Base_value": 2500,
            "Link": "https://www.olx.pl/oferta/test-123.html",
            "Address": "Warszawa, Wola"
        }
        is_valid, issues = parsing_offers.validate_offer_data(offer)
        assert is_valid is True
        assert len(issues) == 0

    def test_validate_missing_name_fails(self):
        """Test that missing name fails validation"""
        offer = {
            "Base_value": 2500,
            "Link": "https://www.olx.pl/oferta/test.html"
        }
        is_valid, issues = parsing_offers.validate_offer_data(offer)
        assert is_valid is False
        assert any("Invalid Name" in issue for issue in issues)

    def test_validate_short_name_fails(self):
        """Test that too short name (<=5 chars) fails validation"""
        offer = {
            "Name": "Test",  # Only 4 chars
            "Base_value": 2500,
            "Link": "https://www.olx.pl/oferta/test.html"
        }
        is_valid, issues = parsing_offers.validate_offer_data(offer)
        assert is_valid is False

    def test_validate_missing_price_fails(self):
        """Test that missing price fails validation"""
        offer = {
            "Name": "Valid Name Here",
            "Link": "https://www.olx.pl/oferta/test.html"
        }
        is_valid, issues = parsing_offers.validate_offer_data(offer)
        assert is_valid is False
        assert any("Invalid Base_value" in issue for issue in issues)

    def test_validate_zero_price_fails(self):
        """Test that zero price fails validation"""
        offer = {
            "Name": "Valid Name",
            "Base_value": 0,
            "Link": "https://www.olx.pl/oferta/test.html"
        }
        is_valid, issues = parsing_offers.validate_offer_data(offer)
        assert is_valid is False

    def test_validate_negative_price_fails(self):
        """Test that negative price fails validation"""
        offer = {
            "Name": "Valid Name",
            "Base_value": -100,
            "Link": "https://www.olx.pl/oferta/test.html"
        }
        is_valid, issues = parsing_offers.validate_offer_data(offer)
        assert is_valid is False

    def test_validate_missing_link_fails(self):
        """Test that missing link fails validation"""
        offer = {
            "Name": "Valid Name",
            "Base_value": 2500
        }
        is_valid, issues = parsing_offers.validate_offer_data(offer)
        assert is_valid is False
        assert any("Invalid Link" in issue for issue in issues)

    def test_validate_link_without_http_fails(self):
        """Test that link without http protocol fails validation"""
        offer = {
            "Name": "Valid Name",
            "Base_value": 2500,
            "Link": "www.olx.pl/oferta/test.html"
        }
        is_valid, issues = parsing_offers.validate_offer_data(offer)
        assert is_valid is False

    def test_validate_suspicious_low_price_warning(self):
        """Test that suspiciously low price generates warning"""
        offer = {
            "Name": "Valid Name",
            "Base_value": 100,  # Too low
            "Link": "https://www.olx.pl/oferta/test.html"
        }
        is_valid, issues = parsing_offers.validate_offer_data(offer)
        assert any("Suspicious price" in issue for issue in issues)

    def test_validate_suspicious_high_price_warning(self):
        """Test that suspiciously high price generates warning"""
        offer = {
            "Name": "Valid Name",
            "Base_value": 100000,  # Too high
            "Link": "https://www.olx.pl/oferta/test.html"
        }
        is_valid, issues = parsing_offers.validate_offer_data(offer)
        assert any("Suspicious price" in issue for issue in issues)

    def test_validate_missing_address_warning(self):
        """Test that missing address generates warning (but doesn't fail)"""
        offer = {
            "Name": "Valid Name",
            "Base_value": 2500,
            "Link": "https://www.olx.pl/oferta/test.html",
            "Address": "Unknown"
        }
        is_valid, issues = parsing_offers.validate_offer_data(offer)
        assert any("Missing meaningful address" in issue for issue in issues)

    def test_validate_olx_source_requires_olx_domain(self):
        """Test that OLX source requires olx.pl in link"""
        offer = {
            "Name": "Valid Name",
            "Base_value": 2500,
            "Link": "https://www.otodom.pl/oferta/test.html"
        }
        is_valid, issues = parsing_offers.validate_offer_data(offer, source="OLX")
        assert is_valid is False
        assert any("olx.pl" in issue for issue in issues)

    def test_validate_otodom_source_requires_otodom_domain(self):
        """Test that Otodom source requires otodom.pl in link"""
        offer = {
            "Name": "Valid Name",
            "Base_value": 2500,
            "Link": "https://www.olx.pl/oferta/test.html"
        }
        is_valid, issues = parsing_offers.validate_offer_data(offer, source="Otodom")
        assert is_valid is False
        assert any("otodom.pl" in issue for issue in issues)

    def test_validate_float_price_accepted(self):
        """Test that float price is accepted"""
        offer = {
            "Name": "Valid Name",
            "Base_value": 2500.50,
            "Link": "https://www.olx.pl/oferta/test.html"
        }
        is_valid, issues = parsing_offers.validate_offer_data(offer)
        assert is_valid is True


# =============================================================================
# TEST find_element_by_selectors
# =============================================================================

class TestFindElementBySelectors:
    """Test suite for find_element_by_selectors function"""

    def test_find_element_with_single_selector(self, sample_html):
        """Test finding element with single valid selector"""
        soup = BeautifulSoup(sample_html, "html.parser")
        selectors = [".offer-title"]
        element = parsing_selectors.find_element_by_selectors(soup, selectors)
        assert element is not None
        assert element.get_text(strip=True) == "Test Offer"

    def test_find_element_returns_first_match(self, sample_html):
        """Test that first matching selector is used"""
        soup = BeautifulSoup(sample_html, "html.parser")
        selectors = [".nonexistent", ".offer-title", "h2"]
        element = parsing_selectors.find_element_by_selectors(soup, selectors)
        assert element is not None
        assert "offer-title" in element.get("class", [])

    def test_find_element_returns_none_when_not_found(self, sample_html):
        """Test that None is returned when no selector matches"""
        soup = BeautifulSoup(sample_html, "html.parser")
        selectors = [".nonexistent", "#missing", "notanelement"]
        element = parsing_selectors.find_element_by_selectors(soup, selectors)
        assert element is None

    def test_find_element_with_attribute(self, sample_html):
        """Test extracting specific attribute from element"""
        soup = BeautifulSoup(sample_html, "html.parser")
        selectors = ["[data-testid='area']"]
        result = parsing_selectors.find_element_by_selectors(soup, selectors, attribute="data-testid")
        assert result == "area"

    def test_find_element_with_contains_selector(self, sample_html):
        """Test finding element with :contains() pseudo-selector"""
        soup = BeautifulSoup(sample_html, "html.parser")
        selectors = ['span:contains("zł")']
        element = parsing_selectors.find_element_by_selectors(soup, selectors)
        assert element is not None
        assert "zł" in element.get_text()

    def test_find_element_handles_invalid_selector(self, sample_html):
        """Test that invalid selector is skipped without error"""
        soup = BeautifulSoup(sample_html, "html.parser")
        selectors = [":::invalid:::", ".offer-title"]
        element = parsing_selectors.find_element_by_selectors(soup, selectors)
        assert element is not None  # Should skip invalid and use valid one

    def test_find_element_empty_selectors_returns_none(self, sample_html):
        """Test that empty selectors list returns None"""
        soup = BeautifulSoup(sample_html, "html.parser")
        selectors = []
        element = parsing_selectors.find_element_by_selectors(soup, selectors)
        assert element is None


# =============================================================================
# TEST extract_with_fallback_selectors
# =============================================================================

class TestExtractWithFallbackSelectors:
    """Test suite for extract_with_fallback_selectors function"""

    def test_extract_text_with_valid_selector(self, sample_html):
        """Test extracting text with valid selector"""
        soup = BeautifulSoup(sample_html, "html.parser")
        selectors = [".offer-title"]
        text = parsing_selectors.extract_with_fallback_selectors(soup, selectors)
        assert text == "Test Offer"

    def test_extract_tries_multiple_selectors(self, sample_html):
        """Test that multiple selectors are tried in order"""
        soup = BeautifulSoup(sample_html, "html.parser")
        selectors = [".nonexistent", ".offer-title"]
        text = parsing_selectors.extract_with_fallback_selectors(soup, selectors)
        assert text == "Test Offer"

    def test_extract_returns_none_when_not_found(self, sample_html):
        """Test that None is returned when no selector matches"""
        soup = BeautifulSoup(sample_html, "html.parser")
        selectors = [".nonexistent", "#missing"]
        text = parsing_selectors.extract_with_fallback_selectors(soup, selectors)
        assert text is None

    def test_extract_empty_selectors_returns_none(self, sample_html):
        """Test that empty selectors list returns None"""
        soup = BeautifulSoup(sample_html, "html.parser")
        text = parsing_selectors.extract_with_fallback_selectors(soup, [])
        assert text is None

    def test_extract_none_selectors_returns_none(self, sample_html):
        """Test that None selectors returns None"""
        soup = BeautifulSoup(sample_html, "html.parser")
        text = parsing_selectors.extract_with_fallback_selectors(soup, None)
        assert text is None

    def test_extract_return_element_flag(self, sample_html):
        """Test returning element instead of text with return_element=True"""
        soup = BeautifulSoup(sample_html, "html.parser")
        selectors = [".offer-title"]
        element = parsing_selectors.extract_with_fallback_selectors(soup, selectors, return_element=True)
        assert element is not None
        assert element.name == "h2"
        assert "offer-title" in element.get("class", [])

    def test_extract_strips_whitespace(self):
        """Test that extracted text is stripped of whitespace"""
        html = "<div class='test'>  Text with spaces  </div>"
        soup = BeautifulSoup(html, "html.parser")
        text = parsing_selectors.extract_with_fallback_selectors(soup, [".test"])
        assert text == "Text with spaces"

    def test_extract_skips_empty_text_elements(self):
        """Test that elements with empty text are skipped"""
        html = """
        <div>
            <span class='empty'></span>
            <span class='filled'>Content</span>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        # If first selector matches empty element, should try next
        text = parsing_selectors.extract_with_fallback_selectors(soup, [".empty", ".filled"])
        assert text == "Content"


# =============================================================================
# INTEGRATION TESTS
# =============================================================================

class TestIntegration:
    """Integration tests combining multiple functions"""

    def test_complete_offer_processing_workflow(self, reset_seen_offers):
        """Test complete workflow: parse, validate, check duplicates"""
        # Create offer
        offer = {
            "Name": "Mieszkanie 2-pokojowe Warszawa Wola",
            "Base_value": parsing_prices.extract_price_from_text("2 500 zł"),
            "Link": "https://www.olx.pl/d/oferta/test-123456.html",
            "Address": parsing_addresses._sanitize_address("Warszawa, Wola - Odświeżono dzisiaj o 15:00"),
        }

        # Validate offer
        is_valid, issues = parsing_offers.validate_offer_data(offer, source="OLX")
        assert is_valid is True

        # Check not duplicate
        assert ms.is_duplicate_offer(offer["Name"], offer["Base_value"], offer["Link"]) is False

        # Same offer is duplicate
        assert ms.is_duplicate_offer(offer["Name"], offer["Base_value"], offer["Link"]) is True

    def test_date_and_address_parsing_workflow(self):
        """Test workflow: parse date and sanitize address"""
        date_text = "Odświeżono 15 listopada 2025 o 14:30"
        address_text = "Warszawa, Mokotów - Odświeżono dzisiaj o 10:00"

        parsed_date = parsing_dates.parse_polish_date(date_text)
        sanitized_address = parsing_addresses._sanitize_address(address_text)

        assert parsed_date == "2025-11-15"
        assert sanitized_address == "Warszawa, Mokotów"
        assert "o 10:00" not in sanitized_address


# =============================================================================
# EDGE CASES AND ERROR HANDLING
# =============================================================================

class TestEdgeCases:
    """Test edge cases and error handling"""

    def test_unicode_characters_in_address(self):
        """Test handling of Polish unicode characters"""
        address = "Łódź, Śródmieście - Żoliborz"
        result = parsing_addresses._sanitize_address(address)
        assert "Łódź" in result
        assert "Śródmieście" in result

    def test_very_large_price(self):
        """Test handling of very large price numbers"""
        large_price = parsing_prices.extract_price_from_text("999 999,99 zł")
        assert large_price == 999999.99

    def test_offer_key_with_unicode_title(self):
        """Test offer key creation with Polish characters"""
        key = parsing_offers.create_offer_key("Piękne Mieszkanie Łódź Śródmieście", 2000)
        assert isinstance(key, str)
        assert "2000" in key

    def test_validate_offer_with_string_price(self):
        """Test validation handles string price (should fail)"""
        offer = {
            "Name": "Valid Name",
            "Base_value": "2500",  # String instead of number
            "Link": "https://www.olx.pl/oferta/test.html"
        }
        is_valid, issues = parsing_offers.validate_offer_data(offer)
        # String "2500" should fail isinstance(x, (int, float)) check
        assert is_valid is False

if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# =============================================================================
# TEST add_total_costs
# =============================================================================

class TestAddTotalCosts:
    """Full_value / Additional_value derivation shared by every Sheets save"""

    def test_sums_base_and_additional(self):
        df = pd.DataFrame({"Base_value": [3000.0], "Additional_value": [500.0]})
        parsing_prices.add_total_costs(df)
        assert df.loc[0, "Full_value"] == 3500.0

    def test_otodom_czynsz_text_overrides_additional_value(self):
        df = pd.DataFrame({
            "Base_value": [3000.0],
            "Additional_value": [100.0],
            "Czynsz (dodatkowo)": ["650,50 zł"],
        })
        parsing_prices.add_total_costs(df)
        assert df.loc[0, "Additional_value"] == 650.5
        assert df.loc[0, "Full_value"] == 3650.5

    def test_missing_czynsz_keeps_existing_additional_value(self):
        df = pd.DataFrame({
            "Base_value": [3000.0],
            "Additional_value": [400.0],
            "Czynsz (dodatkowo)": ["brak informacji"],
        })
        parsing_prices.add_total_costs(df)
        assert df.loc[0, "Full_value"] == 3400.0

    def test_equal_values_are_not_double_counted(self):
        # Some listings repeat the base rent in the extra-fee field
        df = pd.DataFrame({"Base_value": [2800.0], "Additional_value": [2800.0]})
        parsing_prices.add_total_costs(df)
        assert df.loc[0, "Full_value"] == 2800.0

    def test_missing_columns_and_nans_default_to_zero(self):
        df = pd.DataFrame({"Name": ["a", "b"], "Base_value": [2500.0, None]})
        parsing_prices.add_total_costs(df)
        assert list(df["Additional_value"]) == [0.0, 0.0]
        assert list(df["Full_value"]) == [2500.0, 0.0]


# =============================================================================
# TEST read_existing_llm_scores_map
# =============================================================================

class TestReadExistingLlmScoresMap:
    class FakeWorksheet:
        def __init__(self, rows):
            self.rows = rows

        def get_all_values(self):
            return self.rows

    def test_reads_polish_headers(self):
        # "pl" profiles write translated headers - the cache used to come back empty for them
        header = ms.translate_header_row(["Link", "LLM_Score", "LLM_Summary", "LLM_Description", "LLM_Fees"], "pl")
        ws = self.FakeWorksheet([header, ["https://x", "8", "fajne", "opis", "w cenie"]])
        cache = ms.read_existing_llm_scores_map(ws)
        assert cache["https://x"]["score"] == "8"
        assert cache["https://x"]["fees"] == "w cenie"
        assert cache["https://x"]["admin_fee"] == ""

    def test_sheet_without_llm_columns(self):
        ws = self.FakeWorksheet([["Link", "Name"], ["https://x", "flat"]])
        assert ms.read_existing_llm_scores_map(ws) == {}


# =============================================================================
# TEST listings worksheet + config language
# =============================================================================

class FakeWorksheet:
    def __init__(self, title, rows=None):
        self.title = title
        self.rows = rows or []

    def get_all_values(self):
        return self.rows


class FakeSpreadsheet:
    def __init__(self, worksheets):
        self.sheets = {ws.title: ws for ws in worksheets}
        self.created = []

    def worksheet(self, title):
        if title not in self.sheets:
            raise ms.gspread.WorksheetNotFound(title)
        return self.sheets[title]

    def add_worksheet(self, title, rows, cols):
        self.created.append(title)
        self.sheets[title] = FakeWorksheet(title)
        return self.sheets[title]


class TestListingsWorksheet:
    def test_default_name(self, monkeypatch):
        monkeypatch.setattr(ms, "_profile", {})
        assert ms.listings_worksheet_name() == "apartment list"

    def test_profile_can_choose_the_tab(self, monkeypatch):
        monkeypatch.setattr(ms, "_profile", {"worksheet": "lista mieszkan (AI)"})
        assert ms.listings_worksheet_name() == "lista mieszkan (AI)"

    def test_existing_tab_is_reused(self, monkeypatch):
        monkeypatch.setattr(ms, "_profile", {})
        sheet = FakeSpreadsheet([FakeWorksheet("apartment list")])
        assert ms.open_listings_worksheet(sheet).title == "apartment list"
        assert sheet.created == []

    def test_missing_tab_is_created(self, monkeypatch):
        # A spreadsheet made by hand (or by another tool) may not have the tab yet
        monkeypatch.setattr(ms, "_profile", {"worksheet": "lista mieszkan (AI)"})
        sheet = FakeSpreadsheet([FakeWorksheet("config"), FakeWorksheet("lista mieszkan")])
        assert ms.open_listings_worksheet(sheet).title == "lista mieszkan (AI)"
        assert sheet.created == ["lista mieszkan (AI)"]


class TestConfigLanguage:
    def _config(self, rows):
        sheet = FakeSpreadsheet([FakeWorksheet("config", [["Setting", "Value"]] + rows)])
        return ms.load_config_sheet(sheet, "gdansk", default_olx_url="", default_otodom_url="", default_origin="")

    def test_missing_language_row_leaves_it_to_the_profile(self):
        assert self._config([["Transport Mode", "foot-walking"]])["language"] == ""

    def test_language_row_wins(self):
        assert self._config([["Language", "pl"]])["language"] == "pl"
