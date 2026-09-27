"""Tests for llm_scorer.py - response parsing, fee extraction and caching (no API calls)."""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_scorer as ls  # noqa: E402


def llm_reply(**overrides):
    reply = {
        "score": 8,
        "summary": "Close to metro, within budget.",
        "description": "Bright 2-room flat, 45 m², Mokotów.",
        "fees_status": "extra",
        "admin_fee_pln": 716,
        "utilities_estimate_pln": None,
        "optional_extras": [],
    }
    reply.update(overrides)
    return json.dumps(reply)


class TestParseLlmResponse:
    def test_full_reply(self):
        result = ls._parse_llm_response(llm_reply(utilities_estimate_pln=300))
        assert result["score"] == 8
        assert result["fees_status"] == "extra"
        assert result["admin_fee"] == 716
        assert result["utilities_estimate"] == 300

    def test_markdown_fences_are_stripped(self):
        result = ls._parse_llm_response("```json\n" + llm_reply() + "\n```")
        assert result["admin_fee"] == 716

    def test_score_is_clamped(self):
        assert ls._parse_llm_response(llm_reply(score=14))["score"] == 10
        assert ls._parse_llm_response(llm_reply(score=-3))["score"] == 1

    def test_reply_without_fee_fields_still_parses(self):
        # Older prompt / model ignoring the fee instructions
        text = json.dumps({"score": 6, "summary": "ok"})
        result = ls._parse_llm_response(text)
        assert result["score"] == 6
        assert result["fees_status"] == "unknown"
        assert result["admin_fee"] is None

    def test_admin_fee_dropped_unless_fee_is_extra(self):
        # A number next to "included" is usually the rent itself - never report it as a fee
        result = ls._parse_llm_response(llm_reply(fees_status="included", admin_fee_pln=3000))
        assert result["fees_status"] == "included"
        assert result["admin_fee"] is None

    def test_unexpected_status_becomes_unknown(self):
        assert ls._parse_llm_response(llm_reply(fees_status="maybe"))["fees_status"] == "unknown"

    def test_optional_extras_are_kept_separately(self):
        extras = [{"item": "parking", "pln": "150 zł"}, {"item": "internet", "pln": 60}, {"item": "", "pln": 5}]
        result = ls._parse_llm_response(llm_reply(optional_extras=extras))
        assert result["optional_extras"] == [{"item": "parking", "pln": 150.0}, {"item": "internet", "pln": 60.0}]
        assert result["admin_fee"] == 716  # extras never added to the admin fee


class TestToAmount:
    @pytest.mark.parametrize("value,expected", [
        (716, 716.0),
        ("716 zł", 716.0),
        ("1 050,50 PLN", 1050.5),
        ("656,30", 656.3),
        (None, None),
        ("brak", None),
        (0, None),
        (-50, None),
        (True, None),
    ])
    def test_values(self, value, expected):
        assert ls._to_amount(value) == expected


class TestFormatFees:
    def test_extra_fee_with_utilities_and_extras_pl(self):
        result = {
            "fees_status": "extra", "admin_fee": 716.0, "utilities_estimate": 300.0,
            "optional_extras": [{"item": "parking", "pln": 150.0}],
        }
        assert ls.format_fees(result, "pl") == (
            "+716 zł adm.; media ok. 300 zł; opcjonalnie: parking 150 zł"
        )

    def test_included_en(self):
        assert ls.format_fees({"fees_status": "included"}, "en") == "included"

    def test_extra_without_amount(self):
        assert ls.format_fees({"fees_status": "extra", "admin_fee": None}, "pl") == "dodatkowo (bez kwoty)"

    def test_unknown(self):
        assert ls.format_fees({"fees_status": "unknown"}, "pl") == "brak informacji"

    def test_failed_scoring_gives_empty_text(self):
        assert ls.format_fees(dict(ls.EMPTY_RESULT), "pl") == ""

    def test_thousands_separator(self):
        assert ls.format_fees({"fees_status": "extra", "admin_fee": 1200.0}, "en") == "+1 200 zł admin fee"


class TestPrompt:
    def test_listing_text_includes_advertised_rent_and_long_description(self):
        text = ls._build_listing_text({"Base_value": 3000, "Description": "x" * 1500 + " czynsz adm. 716 zł"})
        assert "advertised_rent_pln: 3000" in text
        assert "czynsz adm. 716 zł" in text  # fee details near the end must not be cut off

    def test_prompt_asks_for_profile_language(self):
        assert "in Polish" in ls._build_prompt("listing", "prefs", "pl")
        assert "in English" in ls._build_prompt("listing", "prefs")
        assert "in English" in ls._build_prompt("listing", "prefs", "de")

    def test_groq_payload_limits_reasoning_for_gpt_oss(self):
        payload = ls._groq_payload("prompt", "openai/gpt-oss-20b")
        assert payload["reasoning_effort"] == "low"
        assert payload["response_format"] == {"type": "json_object"}
        assert "reasoning_effort" not in ls._groq_payload("prompt", "qwen/qwen3-32b")


class TestScoreListing:
    def test_backend_error_returns_all_keys(self):
        backend = {"type": "groq", "model": "m", "api_key": "k"}
        with patch.object(ls, "_score_with_groq", side_effect=RuntimeError("down")):
            result = ls.score_listing({"Name": "x"}, "prefs", backend)
        assert set(result) == set(ls.EMPTY_RESULT)
        assert result["score"] == ""


class TestScoreListingsDf:
    BACKEND = {"type": "groq", "model": "m", "api_key": "k"}

    def _run(self, df, cached=None, language="pl"):
        parsed = ls._parse_llm_response(llm_reply())
        with patch.object(ls, "_resolve_backend", return_value=self.BACKEND), \
             patch.object(ls, "score_listing", return_value=parsed) as scorer, \
             patch.object(ls.time, "sleep"):
            out = ls.score_listings_df(df, "near metro", cached, language=language)
        return out, scorer

    def test_new_listing_gets_fee_columns(self):
        out, scorer = self._run(pd.DataFrame([{"Link": "a", "Name": "flat"}]))
        assert scorer.call_count == 1
        assert out.loc[0, "LLM_Fees"] == "+716 zł adm."
        assert out.loc[0, "LLM_Admin_Fee"] == 716

    def test_cached_listing_with_fees_is_not_rescored(self):
        cached = {"a": {"score": "7", "summary": "s", "description": "d", "fees": "w cenie", "admin_fee": ""}}
        out, scorer = self._run(pd.DataFrame([{"Link": "a"}]), cached)
        scorer.assert_not_called()
        assert out.loc[0, "LLM_Fees"] == "w cenie"

    def test_cached_listing_without_fees_is_rescored_once(self):
        # Scored by a version that did not extract fees yet
        cached = {"a": {"score": "7", "summary": "s", "description": "d", "fees": "", "admin_fee": ""}}
        out, scorer = self._run(pd.DataFrame([{"Link": "a"}]), cached)
        assert scorer.call_count == 1
        assert out.loc[0, "LLM_Fees"] == "+716 zł adm."

    def test_no_preferences_skips_scoring(self):
        df = pd.DataFrame([{"Link": "a"}])
        assert ls.score_listings_df(df, "  ") is df


class TestGroqRetries:
    @staticmethod
    def _resp(status, content=None, headers=None):
        resp = MagicMock()
        resp.status_code = status
        resp.headers = headers or {}
        resp.json.return_value = {"choices": [{"message": {"content": content}}]}
        return resp

    def test_retries_transient_503(self):
        replies = [self._resp(503), self._resp(200, llm_reply())]
        with patch.object(ls.requests, "post", side_effect=replies) as post, patch.object(ls.time, "sleep"):
            result = ls._score_with_groq("prompt", "openai/gpt-oss-20b", "key")
        assert post.call_count == 2
        assert result["admin_fee"] == 716

    def test_gives_up_after_max_retries(self):
        failing = self._resp(503)
        failing.raise_for_status.side_effect = RuntimeError("503")
        with patch.object(ls.requests, "post", return_value=failing), patch.object(ls.time, "sleep"):
            with pytest.raises(RuntimeError):
                ls._score_with_groq("prompt", "m", "key", max_retries=3)
