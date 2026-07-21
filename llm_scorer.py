"""llm_scorer.py - Score apartment listings against user preferences using an LLM.

Backend selection (in priority order):
  1. Anthropic API  - set ANTHROPIC_API_KEY (paid, most accurate)
  2. Groq API       - set GROQ_API_KEY (free tier, fast)
                      Free at console.groq.com — no credit card required

Profile config keys (optional):
  groq_model    - Groq model name (default: llama-3.1-8b-instant)
"""
import json
import logging
import os
import time
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

ANTHROPIC_MODEL = "claude-haiku-4-5"
GROQ_DEFAULT_MODEL = "llama-3.1-8b-instant"
GROQ_API_BASE = "https://api.groq.com/openai/v1"
_MAX_DESC_CHARS = 500


def _build_listing_text(row: dict) -> str:
    fields = [
        ("title", row.get("Name", "")),
        ("address", row.get("Address", "")),
        ("area_m2", row.get("Area", "")),
        ("price_pln_per_month", row.get("Full_value", "")),
        ("distance_km", row.get("Distance_km", "")),
        ("commute_min", row.get("Duration_min", "")),
        ("dishwasher", row.get("Dishwasher", "")),
        ("equipment", row.get("Wyposażenie", "")),
        ("description", str(row.get("Description", ""))[:_MAX_DESC_CHARS]),
    ]
    return "\n".join(f"  {k}: {v}" for k, v in fields if v and str(v).strip())


def _build_prompt(listing_text: str, preferences: str) -> str:
    return (
        "Score this apartment listing 1-10 against the user's preferences.\n\n"
        f"USER PREFERENCES:\n{preferences}\n\n"
        f"LISTING:\n{listing_text}\n\n"
        "Reply with valid JSON only, no markdown, no explanation:\n"
        '{"score": <integer 1-10>, "summary": "<one sentence reason>"}'
    )


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
    return {"score": score, "summary": summary}


def _score_with_anthropic(prompt: str, client) -> dict:
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=120,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_llm_response(response.content[0].text)


def _score_with_groq(prompt: str, model: str, api_key: str, max_retries: int = 5) -> dict:
    delay = 2.0
    for attempt in range(max_retries):
        resp = requests.post(
            f"{GROQ_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 120,
                "temperature": 0.1,
            },
            timeout=30,
        )
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("retry-after", delay))
            wait = max(retry_after, delay)
            logger.debug(f"Groq 429 - waiting {wait:.1f}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(wait)
            delay = min(delay * 2, 60)
            continue
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        return _parse_llm_response(text)
    raise RuntimeError(f"Groq rate limit not resolved after {max_retries} retries")


def score_listing(listing: dict, preferences: str, backend: dict) -> dict:
    """Score one listing using the configured backend. Returns {"score": int, "summary": str}."""
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
    return {"score": "", "summary": ""}


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
) -> pd.DataFrame:
    """
    Add LLM_Score and LLM_Summary columns to df.

    Backend priority: Anthropic API → Groq API (free).
    Set GROQ_API_KEY for free scoring (get one at console.groq.com).
    cached_scores: Link → {"score", "summary"} from a previous Sheets read.
    Rows already scored (non-empty LLM_Score in df) are skipped without an API call.
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
    if "LLM_Score" not in df.columns:
        df["LLM_Score"] = ""
        df["LLM_Summary"] = ""

    scored = 0
    from_cache = 0
    for idx, row in df.iterrows():
        link = str(row.get("Link", "") or "")

        # Already scored in df (e.g. loaded from pickle)
        existing = str(row.get("LLM_Score", "") or "").strip()
        if existing and existing not in ("nan", ""):
            from_cache += 1
            continue

        # Cached from previous Sheets read
        if link in cached:
            df.at[idx, "LLM_Score"] = cached[link]["score"]
            df.at[idx, "LLM_Summary"] = cached[link]["summary"]
            from_cache += 1
            continue

        result = score_listing(dict(row), preferences, backend)
        df.at[idx, "LLM_Score"] = result["score"]
        df.at[idx, "LLM_Summary"] = result["summary"]
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
