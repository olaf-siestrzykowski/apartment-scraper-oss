"""llm_scorer.py - Score apartment listings against user preferences using an LLM.

One LLM call per listing returns:
  - a 1-10 match score and a one-sentence reason
  - a short neutral description for the email digest
  - the monthly fees stated in the description (administrative fee on top of the
    rent, utilities estimate, optional extras such as parking) - listing forms often
    leave the fee field empty while the description spells it out

Backend selection (in priority order):
  1. Anthropic API  - set ANTHROPIC_API_KEY (paid, most accurate)
  2. Groq API       - set GROQ_API_KEY (free tier, fast)
                      Free at console.groq.com - no credit card required

Profile config keys (optional):
  groq_model    - Groq model name (default: openai/gpt-oss-20b)
"""
import json
import logging
import os
import time
from typing import Any, Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

ANTHROPIC_MODEL = "claude-haiku-4-5"
GROQ_DEFAULT_MODEL = "openai/gpt-oss-20b"
GROQ_API_BASE = "https://api.groq.com/openai/v1"
# Fee details often sit near the end of the description. In 6,260 scraped listings
# 4% of fee mentions started after character 2000 and none after 3000.
_MAX_DESC_CHARS = 3000
# Room for the JSON answer (score, summary, 2-3 sentence description, fees)
_MAX_OUTPUT_TOKENS = 600

FEES_INCLUDED = "included"
FEES_EXTRA = "extra"
FEES_UNKNOWN = "unknown"

EMPTY_RESULT = {
    "score": "", "summary": "", "description": "",
    "fees_status": "", "admin_fee": None, "utilities_estimate": None, "optional_extras": [],
}


def _build_listing_text(row: dict) -> str:
    fields = [
        ("title", row.get("Name", "")),
        ("address", row.get("Address", "")),
        ("area_m2", row.get("Area", "")),
        ("advertised_rent_pln", row.get("Base_value", "")),
        ("total_price_pln_per_month", row.get("Full_value", "")),
        ("distance_km", row.get("Distance_km", "")),
        ("commute_min", row.get("Duration_min", "")),
        ("dishwasher", row.get("Dishwasher", "")),
        ("equipment", row.get("Wyposażenie", "")),
        ("description", str(row.get("Description", ""))[:_MAX_DESC_CHARS]),
    ]
    return "\n".join(f"  {k}: {v}" for k, v in fields if v and str(v).strip())


def _build_prompt(listing_text: str, preferences: str) -> str:
    return (
        "Score this apartment listing 1-10 against the user's preferences, write a short "
        "description of the listing, and extract the monthly fees stated in the listing.\n\n"
        f"USER PREFERENCES:\n{preferences}\n\n"
        f"LISTING:\n{listing_text}\n\n"
        "FEES - read the description carefully (it is usually in Polish):\n"
        '- fees_status: "extra" if an administrative/building fee (czynsz administracyjny, '
        'opłaty eksploatacyjne, czynsz do wspólnoty/spółdzielni) is paid ON TOP of the advertised '
        'rent; "included" if the listing says the advertised rent already covers it (w cenie, '
        'wliczone, z opłatami, all inclusive); "unknown" if the listing does not say.\n'
        "- admin_fee_pln: that monthly administrative fee in PLN when fees_status is \"extra\", else null. "
        "Never put the rent here - \"czynsz najmu\", \"odstępne\", \"najem\" and \"cena\" are the rent, "
        "not the administrative fee. A price for parking, garage or storage is never the administrative "
        "fee. If a range is given, use its midpoint. \"W cenie\" about parking, internet or furniture "
        "does not mean the administrative fee is included.\n"
        "- utilities_estimate_pln: estimated monthly utilities (media, prąd, gaz, ogrzewanie) only if "
        "the listing gives a number, else null. \"Według zużycia\" without a number is null.\n"
        "- optional_extras: things the tenant may choose to pay for (parking, garage, storage, "
        "internet/TV), each as {\"item\": str, \"pln\": number}. Not deposits (kaucja).\n\n"
        "Reply with valid JSON only, no markdown, no explanation:\n"
        '{"score": <integer 1-10>, "summary": "<one sentence why this score>", '
        '"description": "<2-3 sentence neutral summary of the apartment: size, price, location, key amenities>", '
        '"fees_status": "extra" | "included" | "unknown", "admin_fee_pln": <number or null>, '
        '"utilities_estimate_pln": <number or null>, '
        '"optional_extras": [{"item": "<name>", "pln": <number>}]}'
    )


def _to_amount(value: Any) -> Optional[float]:
    """Positive PLN amount from an LLM value (number or string like "716 zł"), else None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        cleaned = value.lower().replace("zł", "").replace("pln", "").replace("\u00a0", "").replace(" ", "")
        value = cleaned.replace(",", ".")
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return round(amount, 2) if amount > 0 else None


def _parse_fees(parsed: dict) -> dict:
    status = str(parsed.get("fees_status", "")).strip().lower()
    if status not in (FEES_EXTRA, FEES_INCLUDED, FEES_UNKNOWN):
        status = FEES_UNKNOWN
    admin_fee = _to_amount(parsed.get("admin_fee_pln"))
    if status != FEES_EXTRA:
        # An amount only makes sense for a fee paid on top of the rent
        admin_fee = None
    extras = []
    for extra in parsed.get("optional_extras") or []:
        if isinstance(extra, dict):
            item = str(extra.get("item", "")).strip()[:40]
            amount = _to_amount(extra.get("pln"))
            if item and amount:
                extras.append({"item": item, "pln": amount})
    return {
        "fees_status": status,
        "admin_fee": admin_fee,
        "utilities_estimate": _to_amount(parsed.get("utilities_estimate_pln")),
        "optional_extras": extras[:5],
    }


_FEE_LABELS = {
    "pl": {"included": "w cenie", "unknown": "brak informacji", "extra": "dodatkowo",
           "admin": "adm.", "utilities": "media ok.", "optional": "opcjonalnie"},
    "en": {"included": "included", "unknown": "not stated", "extra": "extra",
           "admin": "admin fee", "utilities": "utilities ~", "optional": "optional"},
}


def _fmt_pln(amount: float) -> str:
    return f"{amount:,.0f}".replace(",", "\u00a0") + "\u00a0zł"


def format_fees(result: dict, language: str = "en") -> str:
    """Human-readable fee summary for the sheet and email, e.g. "+716 zł adm.; media ok. 300 zł"."""
    labels = _FEE_LABELS.get(language, _FEE_LABELS["en"])
    status = result.get("fees_status") or ""
    if not status:
        return ""
    parts = []
    if status == FEES_INCLUDED:
        parts.append(labels["included"])
    elif status == FEES_EXTRA:
        admin_fee = result.get("admin_fee")
        parts.append(f"+{_fmt_pln(admin_fee)} {labels['admin']}" if admin_fee else labels["extra"])
    else:
        parts.append(labels["unknown"])
    if result.get("utilities_estimate"):
        parts.append(f"{labels['utilities']} {_fmt_pln(result['utilities_estimate'])}")
    extras = result.get("optional_extras") or []
    if extras:
        items = ", ".join(f"{e['item']} {_fmt_pln(e['pln'])}" for e in extras)
        parts.append(f"{labels['optional']}: {items}")
    return "; ".join(parts)


def _parse_llm_response(text: str) -> dict:
    """Parse JSON from LLM response, stripping markdown fences if present."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    parsed = json.loads(text)
    score = max(1, min(10, int(parsed["score"])))
    summary = str(parsed.get("summary", ""))[:200]
    description = str(parsed.get("description", ""))[:400]
    return {"score": score, "summary": summary, "description": description, **_parse_fees(parsed)}


def _score_with_anthropic(prompt: str, client) -> dict:
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=_MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_llm_response(response.content[0].text)


def _groq_payload(prompt: str, model: str) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": _MAX_OUTPUT_TOKENS,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    if model.startswith("openai/gpt-oss"):
        # Reasoning models otherwise spend the whole token budget thinking and
        # return an empty answer
        payload["reasoning_effort"] = "low"
    return payload


def _score_with_groq(prompt: str, model: str, api_key: str, max_retries: int = 5) -> dict:
    delay = 2.0
    for attempt in range(max_retries):
        resp = requests.post(
            f"{GROQ_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=_groq_payload(prompt, model),
            timeout=60,
        )
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("retry-after", delay))
            wait = max(retry_after, delay)
            logger.debug(f"Groq 429 - waiting {wait:.1f}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(wait)
            delay = min(delay * 2, 60)
            continue
        if resp.status_code >= 500 and attempt < max_retries - 1:
            # Groq returns short-lived 503s under load
            logger.debug(f"Groq {resp.status_code} - retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        return _parse_llm_response(text)
    raise RuntimeError(f"Groq request not resolved after {max_retries} retries")


def score_listing(listing: dict, preferences: str, backend: dict) -> dict:
    """Score one listing using the configured backend. Returns a dict shaped like EMPTY_RESULT."""
    listing_text = _build_listing_text(listing)
    prompt = _build_prompt(listing_text, preferences)
    name = listing.get("Name", "")
    try:
        if backend["type"] == "anthropic":
            return _score_with_anthropic(prompt, backend["client"])
        elif backend["type"] == "groq":
            return _score_with_groq(prompt, backend["model"], backend["api_key"])
    except Exception as e:
        logger.warning(f"LLM scoring failed for '{name}': {e}")
    return dict(EMPTY_RESULT)


def _resolve_backend(groq_model: Optional[str] = None) -> Optional[dict]:
    """Return a backend config dict, or None if no backend is available."""
    # 1. Anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        try:
            import anthropic
            logger.info(f"🤖 Using Anthropic backend ({ANTHROPIC_MODEL})")
            return {"type": "anthropic", "client": anthropic.Anthropic(api_key=api_key)}
        except ImportError:
            logger.warning("🤖 'anthropic' package not installed - falling back to Groq")

    # 2. Groq (free tier)
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if groq_key:
        model = groq_model or os.environ.get("GROQ_MODEL", GROQ_DEFAULT_MODEL)
        logger.info(f"🤖 Using Groq backend ({model})")
        return {"type": "groq", "model": model, "api_key": groq_key}

    return None


def score_listings_df(
    df: pd.DataFrame,
    preferences: str,
    cached_scores: Optional[dict] = None,
    groq_model: Optional[str] = None,
    language: str = "en",
) -> pd.DataFrame:
    """
    Add LLM_Score, LLM_Summary, LLM_Description, LLM_Fees and LLM_Admin_Fee columns to df.

    Backend priority: Anthropic API → Groq API (free).
    Set GROQ_API_KEY for free scoring (get one at console.groq.com).
    cached_scores: Link → {"score", "summary", "description", "fees", "admin_fee"} from a
    previous Sheets read. Cached rows without fees (scored by an older version) are
    scored again once so the fee columns get filled.
    Rows already scored (non-empty LLM_Score and LLM_Fees in df) are skipped without an API call.
    """
    if not preferences or not preferences.strip():
        logger.info("🤖 llm_preferences not configured - skipping LLM scoring")
        return df

    backend = _resolve_backend(groq_model)
    if backend is None:
        logger.warning(
            "🤖 No LLM backend available - set ANTHROPIC_API_KEY or GROQ_API_KEY. "
            "Get a free Groq key at console.groq.com"
        )
        return df

    cached = cached_scores or {}
    df = df.copy()
    for col in ("LLM_Score", "LLM_Summary", "LLM_Description", "LLM_Fees", "LLM_Admin_Fee"):
        # object dtype: these columns mix numbers with "" (pandas 3 makes "" a strict str column)
        df[col] = df[col].astype(object) if col in df.columns else pd.Series("", index=df.index, dtype=object)

    scored = 0
    from_cache = 0
    for idx, row in df.iterrows():
        link = str(row.get("Link", "") or "")

        # Already scored in df (e.g. loaded from pickle)
        existing = str(row.get("LLM_Score", "") or "").strip()
        existing_fees = str(row.get("LLM_Fees", "") or "").strip()
        if existing and existing != "nan" and existing_fees and existing_fees != "nan":
            from_cache += 1
            continue

        # Cached from previous Sheets read
        if link in cached and cached[link].get("fees"):
            df.at[idx, "LLM_Score"] = cached[link]["score"]
            df.at[idx, "LLM_Summary"] = cached[link]["summary"]
            df.at[idx, "LLM_Description"] = cached[link].get("description", "")
            df.at[idx, "LLM_Fees"] = cached[link]["fees"]
            df.at[idx, "LLM_Admin_Fee"] = cached[link].get("admin_fee", "")
            from_cache += 1
            continue

        result = score_listing(dict(row), preferences, backend)
        df.at[idx, "LLM_Score"] = result["score"]
        df.at[idx, "LLM_Summary"] = result["summary"]
        df.at[idx, "LLM_Description"] = result["description"]
        df.at[idx, "LLM_Fees"] = format_fees(result, language)
        df.at[idx, "LLM_Admin_Fee"] = result["admin_fee"] if result["admin_fee"] else ""
        scored += 1
        if backend["type"] == "anthropic" and scored % 10 == 0:
            time.sleep(0.5)
        elif backend["type"] == "groq":
            time.sleep(2.0)

    logger.info(
        f"🤖 LLM scoring done [{backend['type']}]: "
        f"{scored} new, {from_cache} from cache"
    )
    return df
