"""
Unit tests for email_report.py - the daily digest filtering/ranking/rendering logic.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

import email_report as er


def make_offer(**overrides):
    """A baseline offer that passes every quality_offers check, with overrides applied."""
    base = {
        "Name": "Ładne mieszkanie 2-pokojowe do wynajęcia",
        "Image_URL": "https://example.com/photo.jpg",
        "Description": "Przestronne mieszkanie w spokojnej okolicy, blisko metra i sklepów. " * 2,
        "Area": 45,
        "Full_value": 3000,
        "Address": "Warszawa, Mokotów",
        "Location": "Warszawa, Mokotów",
        "Link": "https://www.olx.pl/oferta/test.html",
        "Source": "OLX",
    }
    base.update(overrides)
    return base


class TestHasPhoto:
    def test_present(self):
        assert er._has_photo(make_offer()) is True

    def test_missing(self):
        assert er._has_photo(make_offer(Image_URL="")) is False


class TestHasDescription:
    def test_long_enough(self):
        assert er._has_description(make_offer()) is True

    def test_too_short(self):
        assert er._has_description(make_offer(Description="Krótki opis.")) is False


class TestHasAreaPrice:
    def test_valid_area_and_price(self):
        offer = make_offer()
        assert er._has_area(offer) is True
        assert er._has_price(offer) is True

    def test_zero_area_fails(self):
        assert er._has_area(make_offer(Area=0)) is False

    def test_zero_price_fails(self):
        assert er._has_price(make_offer(Full_value=0)) is False

    def test_non_numeric_area_fails_safely(self):
        assert er._has_area(make_offer(Area="unknown")) is False


class TestIsWholeApartment:
    def test_whole_apartment_passes(self):
        assert er._is_whole_apartment(make_offer(Name="Mieszkanie 2-pokojowe")) is True

    def test_single_room_fails(self):
        assert er._is_whole_apartment(make_offer(Name="Pokój do wynajęcia")) is False

    def test_room_with_apartment_keyword_passes(self):
        """'Mieszkanie z 2 pokojami' has both 'pokoj' and an apartment keyword - it's a flat."""
        assert er._is_whole_apartment(make_offer(Name="Mieszkanie z 2 pokojami")) is True

    def test_room_share_offer_fails(self):
        """'Wynajem pokoju' is a room-share ad even though it mentions no bare 'pokój' issue."""
        assert er._is_whole_apartment(make_offer(Name="Wynajem pokoju w centrum")) is False


class TestBuildDistrictRegex:
    def test_none_input_returns_none(self):
        assert er._build_district_regex(None) is None

    def test_empty_string_returns_none(self):
        assert er._build_district_regex("") is None

    def test_matches_plain_district_name(self):
        regex = er._build_district_regex("Mokotów")
        assert regex.search("Warszawa, Mokotów")

    def test_comma_separated_list(self):
        regex = er._build_district_regex("Wola, Mokotów, Ursynów")
        assert regex.search("mieszkanie na Ursynowie")

    def test_diacritic_folding_matches_ascii_variant(self):
        """Real scraped text sometimes drops Polish diacritics entirely."""
        regex = er._build_district_regex("Śródmieście")
        assert regex.search("Srodmiescie, Warszawa")

    def test_list_input(self):
        regex = er._build_district_regex(["Wola", "Mokotów"])
        assert regex.search("Wola")


class TestIsInDistricts:
    def test_none_regex_always_passes(self):
        assert er._is_in_districts(make_offer(), None) is True

    def test_matching_district_passes(self):
        regex = er._build_district_regex("Mokotów")
        assert er._is_in_districts(make_offer(Address="Warszawa, Mokotów"), regex) is True

    def test_non_matching_district_fails(self):
        regex = er._build_district_regex("Wola")
        assert er._is_in_districts(make_offer(Address="Warszawa, Mokotów", Location=""), regex) is False

    def test_matches_via_name_when_address_lacks_district(self):
        """OLX often only names the district in the title, not the address field."""
        regex = er._build_district_regex("Ursynów")
        offer = make_offer(Address="Warszawa", Location="", Name="Mieszkanie na Ursynowie")
        assert er._is_in_districts(offer, regex) is True


class TestIsPolishLanguage:
    def test_polish_text_passes(self):
        assert er._is_polish_language(make_offer()) is True

    def test_cyrillic_text_fails(self):
        offer = make_offer(Description="Сдам квартиру в Варшаве, отличное расположение")
        assert er._is_polish_language(offer) is False


class TestIsInCity:
    WARSAW = er._build_city_regex("Warszawa, Poland")

    def test_city_address_passes(self):
        assert er._is_in_city(make_offer(Address="Warszawa, Mokotów"), self.WARSAW)

    def test_outside_city_fails(self):
        assert not er._is_in_city(make_offer(Address="Marki", Location="Marki"), self.WARSAW)

    def test_matches_via_location_when_address_missing(self):
        assert er._is_in_city(make_offer(Address="", Location="Warszawa"), self.WARSAW)

    def test_diacritics_are_folded(self):
        krakow = er._build_city_regex("Kraków")
        assert er._is_in_city(make_offer(Address="Krakow, Podgórze"), krakow)

    def test_no_city_disables_filter(self):
        assert er._build_city_regex("") is None
        assert er._is_in_city(make_offer(Address="Marki", Location="Marki"), None)


class TestFilterQualityOffers:
    def test_empty_dataframe_returns_empty(self):
        result = er.filter_quality_offers(pd.DataFrame())
        assert result.empty

    def test_good_offer_passes_all_filters(self):
        df = pd.DataFrame([make_offer()])
        result = er.filter_quality_offers(df)
        assert len(result) == 1

    def test_offer_without_photo_is_dropped(self):
        df = pd.DataFrame([make_offer(Image_URL="")])
        result = er.filter_quality_offers(df)
        assert result.empty

    def test_offer_outside_city_is_dropped(self):
        df = pd.DataFrame([make_offer(Address="Marki", Location="Marki")])
        result = er.filter_quality_offers(df, city_re=er._build_city_regex("Warszawa"))
        assert result.empty

    def test_district_filter_drops_wrong_district(self):
        district_re = er._build_district_regex("Wola")
        df = pd.DataFrame([make_offer(Address="Warszawa, Mokotów")])
        result = er.filter_quality_offers(df, district_re)
        assert result.empty

    def test_district_filter_keeps_matching_district(self):
        district_re = er._build_district_regex("Mokotów")
        df = pd.DataFrame([make_offer(Address="Warszawa, Mokotów")])
        result = er.filter_quality_offers(df, district_re)
        assert len(result) == 1

    def test_mixed_batch_keeps_only_passing_rows(self):
        df = pd.DataFrame([
            make_offer(Link="https://a"),           # passes
            make_offer(Link="https://b", Image_URL=""),  # no photo
            make_offer(Link="https://c", Name="Pokój do wynajęcia"),  # room only
        ])
        result = er.filter_quality_offers(df)
        assert len(result) == 1
        assert result.iloc[0]["Link"] == "https://a"


class TestAddPricePerM2:
    def test_computes_and_sorts_ascending(self):
        df = pd.DataFrame([
            make_offer(Link="expensive", Full_value=6000, Area=50),  # 120/m2
            make_offer(Link="cheap", Full_value=2000, Area=50),      # 40/m2
        ])
        result = er.add_price_per_m2(df)
        assert list(result["Link"]) == ["cheap", "expensive"]
        assert result.iloc[0]["Price_per_m2"] == 40
        assert result.iloc[1]["Price_per_m2"] == 120


class TestGenerateEmailHtml:
    def test_default_language_is_english(self):
        df = pd.DataFrame([make_offer()])
        html = er.generate_email_html(df, "studio", "sheet123", "2026-07-16")
        assert "Best offers: studio" in html
        assert 'lang="en"' in html

    def test_polish_language_switches_all_labels(self):
        df = pd.DataFrame([make_offer()])
        html = er.generate_email_html(df, "studio", "sheet123", "2026-07-16", language="pl")
        assert "Najlepsze oferty: studio" in html
        assert 'lang="pl"' in html
        assert "/mies." in html

    def test_unknown_language_falls_back_to_english(self):
        df = pd.DataFrame([make_offer()])
        html = er.generate_email_html(df, "studio", "sheet123", "2026-07-16", language="de")
        assert "Best offers" in html

    def test_missing_name_uses_localized_default(self):
        df = pd.DataFrame([make_offer(Name="")])
        html_en = er.generate_email_html(df, "studio", "", "2026-07-16", language="en")
        html_pl = er.generate_email_html(df, "studio", "", "2026-07-16", language="pl")
        assert ">Offer<" in html_en
        assert ">Oferta<" in html_pl

    def test_no_sheet_id_uses_placeholder_link(self):
        df = pd.DataFrame([make_offer()])
        html = er.generate_email_html(df, "studio", "", "2026-07-16")
        assert 'href="#"' in html


class TestSendEmailReport:
    """send_email_report's early-exit paths don't touch the network - test those directly.
    The full send path is exercised once with smtplib mocked out."""

    def _config(self, **overrides):
        cfg = {
            "email_recipient": "you@example.com",
            "email_sender": "sender@example.com",
            "email_app_password": "app-password",
        }
        cfg.update(overrides)
        return cfg

    def test_missing_credentials_skips_without_sending(self):
        df = pd.DataFrame([make_offer()])
        result = er.send_email_report(df, {}, "studio", "sheet123", "2026-07-16")
        assert result is False

    def test_no_quality_offers_skips_without_sending(self):
        df = pd.DataFrame([make_offer(Image_URL="")])  # fails the photo check
        result = er.send_email_report(df, self._config(), "studio", "sheet123", "2026-07-16")
        assert result is False

    def test_all_offers_below_price_floor_skips_without_sending(self):
        """MIN_PRICE_PER_M2 guards against per-room/placeholder prices slipping through."""
        df = pd.DataFrame([make_offer(Full_value=100, Area=50)])  # 2 zl/m2, well under the floor
        result = er.send_email_report(df, self._config(), "studio", "sheet123", "2026-07-16")
        assert result is False

    @patch("smtplib.SMTP")
    def test_successful_send_calls_smtp_with_credentials(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_server

        df = pd.DataFrame([make_offer()])
        result = er.send_email_report(df, self._config(), "studio", "sheet123", "2026-07-16")

        assert result is True
        mock_server.login.assert_called_once_with("sender@example.com", "app-password")
        mock_server.sendmail.assert_called_once()

    @patch("smtplib.SMTP")
    def test_smtp_failure_returns_false(self, mock_smtp_cls):
        mock_smtp_cls.side_effect = OSError("connection refused")
        df = pd.DataFrame([make_offer()])
        result = er.send_email_report(df, self._config(), "studio", "sheet123", "2026-07-16")
        assert result is False


class TestFeesLine:
    def test_fees_shown_and_escaped(self):
        df = pd.DataFrame([make_offer(LLM_Fees="+716 zł adm.; <b>parking</b>")])
        html_pl = er.generate_email_html(df, "studio", "", "2026-07-16", language="pl")
        assert "Opłaty (AI):" in html_pl
        assert "+716 zł adm." in html_pl
        assert "<b>parking</b>" not in html_pl  # listing-derived text is escaped

    def test_no_fees_no_line(self):
        df = pd.DataFrame([make_offer()])
        assert "Fees (AI)" not in er.generate_email_html(df, "studio", "", "2026-07-16")
