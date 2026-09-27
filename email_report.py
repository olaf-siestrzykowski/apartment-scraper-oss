"""
email_report.py - Sends a daily digest of top apartment offers, ranked by price/m².

Filters applied (quality gate):
  - Has at least one photo (Image_URL non-empty)
  - Has a real description (Description ≥ 80 characters)
  - Area is known (Area > 0)
  - Full_value is known and > 0

Ranking: price per m² ascending (best value first).

Configuration (read from config sheet or passed as a dict):
  email_recipient   - comma-separated list of addresses to send to
  email_sender      - Gmail address used to send (or any SMTP sender)
  email_app_password - Gmail App Password (Settings → Security → App Passwords)
  email_smtp_host   - SMTP host (default: smtp.gmail.com)
  email_smtp_port   - SMTP port (default: 587)
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pandas as pd

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

import re

TOP_N = 10
MIN_DESCRIPTION_LEN = 80
# Floor on price per m² - anything below this is a per-room/placeholder price, not a
# real whole-apartment rent (e.g. a "2 pokoje" listed at 400 zł → 7 zł/m²).
MIN_PRICE_PER_M2 = 35

# Any inflected form of "pokój": pokój, pokoju, pokoje, pokojów, pokojach, pokojem…
_ROOM_WORD_RE = re.compile(r'\bpok[oó]j', re.IGNORECASE)
# Whole-apartment markers - if these appear, it's not a room listing
_APARTMENT_WORD_RE = re.compile(r'kawalerka|mieszkani|apartament', re.IGNORECASE)
# Room-share offers ("mieszkanie na pokoje", "wynajem pokoju") - a whole flat split into
# rented rooms. These carry an apartment keyword so they slip past _APARTMENT_WORD_RE.
_ROOM_SHARE_RE = re.compile(r'na pokoje|na pok[oó]j\b|wynajem pokoj', re.IGNORECASE)

# Cyrillic block covers Russian, Ukrainian, Belarusian
_CYRILLIC_RE = re.compile(r'[\u0400-\u04FF]')

# ── Filtering ─────────────────────────────────────────────────────────────────

def _has_photo(row) -> bool:
    url = str(row.get("Image_URL", "") or "")
    return bool(url.strip())


def _has_description(row) -> bool:
    desc = str(row.get("Description", "") or "")
    return len(desc.strip()) >= MIN_DESCRIPTION_LEN


def _has_area(row) -> bool:
    try:
        return float(row.get("Area", 0) or 0) > 0
    except (ValueError, TypeError):
        return False


def _has_price(row) -> bool:
    try:
        return float(row.get("Full_value", 0) or 0) > 0
    except (ValueError, TypeError):
        return False


def _is_whole_apartment(row) -> bool:
    """Return False if the listing is a single room, not a whole apartment.

    Logic: if the title contains any form of 'pokój' (pokoju, pokoje, etc.)
    but NO apartment keyword (kawalerka, mieszkanie, apartament) → it's a room ad.
    """
    name = str(row.get("Name", "") or "")
    if _ROOM_SHARE_RE.search(name):
        return False
    if _ROOM_WORD_RE.search(name) and not _APARTMENT_WORD_RE.search(name):
        return False
    return True


def _build_city_regex(city: str):
    """Build a regex matching the configured city name, or None to skip city filtering.

    Strips country suffix (e.g. "Warszawa, Poland" → "Warszawa") and applies the same
    diacritic folding as _build_district_regex so "Krakow" matches "Kraków".
    Pass an empty string or None to disable city filtering entirely.
    """
    if not city or not city.strip():
        return None
    city_name = city.strip().split(",")[0].strip()
    if not city_name:
        return None
    _fold = {"ą": "[aą]", "ó": "[oó]", "ś": "[sś]", "ż": "[zżź]", "ź": "[zżź]",
             "ł": "[lł]", "ć": "[cć]", "ń": "[nń]", "ę": "[eę]"}
    pattern = "".join(_fold.get(ch, re.escape(ch)) for ch in city_name.lower())
    return re.compile(pattern, re.IGNORECASE)


def _is_in_city(row, city_re) -> bool:
    """Return False if the listing address does not mention the configured city.

    Matches against Address and Location fields. Returns True when city_re is None
    (city filtering disabled).
    """
    if city_re is None:
        return True
    address = str(row.get("Address", "") or "")
    location = str(row.get("Location", "") or "")
    return bool(city_re.search(address) or city_re.search(location))


def _build_district_regex(districts):
    """Build a case-insensitive regex matching any of the given district names.

    `districts` may be a comma-separated string or a list. Returns None if empty
    (district filtering disabled). Polish diacritics are matched flexibly so that
    e.g. "Śródmieście" also matches scraped text written as "Srodmiescie".
    """
    if not districts:
        return None
    if isinstance(districts, str):
        names = [d.strip() for d in districts.split(",") if d.strip()]
    else:
        names = [str(d).strip() for d in districts if str(d).strip()]
    if not names:
        return None

    _fold = {"ą": "[aą]", "ó": "[oó]", "ś": "[sś]", "ż": "[zżź]", "ź": "[zżź]",
             "ł": "[lł]", "ć": "[cć]", "ń": "[nń]", "ę": "[eę]"}
    alts = []
    for name in names:
        alts.append("".join(_fold.get(ch, re.escape(ch)) for ch in name.lower()))
    return re.compile("|".join(alts), re.IGNORECASE)


def _is_in_districts(row, district_re) -> bool:
    """Return True if the listing is in one of the wanted districts.

    `district_re` is a compiled regex (or None to disable district filtering).
    Matches against Address, Location and the listing title (OLX often only
    names the district in the title).
    """
    if district_re is None:
        return True
    haystack = " ".join(str(row.get(c, "") or "") for c in ("Address", "Location", "Name"))
    return bool(district_re.search(haystack))


def _is_polish_language(row) -> bool:
    """Return False if the listing description is in Cyrillic (RU/UA/BY)."""
    name = str(row.get("Name", "") or "")
    opis = str(row.get("Description", "") or "")
    return not _CYRILLIC_RE.search(name + opis)


def filter_quality_offers(df: pd.DataFrame, district_re=None, city_re=None) -> pd.DataFrame:
    """Return rows that pass all quality checks.

    city_re     -- compiled regex from _build_city_regex(); drops listings whose
                   Address/Location don't mention the configured city. Pass None to
                   skip city filtering (e.g. when city is unset in the profile).
    district_re -- compiled regex from _build_district_regex(); further narrows to
                   specific districts within the city. Pass None to include all.
    """
    if df.empty:
        return df

    results = []
    counts = {"room_only": 0, "cyrillic": 0, "outside_city": 0, "no_photo": 0,
              "no_desc": 0, "no_area_price": 0, "wrong_district": 0}

    for _, row in df.iterrows():
        if not _has_photo(row):
            counts["no_photo"] += 1; continue
        if not _has_description(row):
            counts["no_desc"] += 1; continue
        if not (_has_area(row) and _has_price(row)):
            counts["no_area_price"] += 1; continue
        if not _is_whole_apartment(row):
            counts["room_only"] += 1; continue
        if not _is_polish_language(row):
            counts["cyrillic"] += 1; continue
        if not _is_in_city(row, city_re):
            counts["outside_city"] += 1; continue
        if not _is_in_districts(row, district_re):
            counts["wrong_district"] += 1; continue
        results.append(row)

    logger.info(
        f"📧 Filtered out: {counts['room_only']} rooms, {counts['cyrillic']} Cyrillic, "
        f"{counts['outside_city']} outside city, {counts['wrong_district']} wrong district, "
        f"{counts['no_photo']} no photo, {counts['no_desc']} no desc, "
        f"{counts['no_area_price']} no area/price"
    )
    return pd.DataFrame(results) if results else pd.DataFrame(columns=df.columns)


def add_price_per_m2(df: pd.DataFrame) -> pd.DataFrame:
    """Add Price_per_m2 column and sort ascending."""
    df = df.copy()
    df["_full"] = pd.to_numeric(df["Full_value"], errors="coerce")
    df["_area"] = pd.to_numeric(df["Area"], errors="coerce")
    df["Price_per_m2"] = (df["_full"] / df["_area"]).round(0).astype("Int64", errors="ignore")
    df = df.drop(columns=["_full", "_area"], errors="ignore")
    return df.sort_values("Price_per_m2", ascending=True)


# ── HTML generation ───────────────────────────────────────────────────────────

# User-facing text for each supported language. Internal column keys and code
# always stay in English - this only controls what the recipient reads.
EMAIL_TEXT = {
    "en": {
        "lang_attr": "en",
        "heading": "Best offers: {profile} 🏠",
        "subheading": "Top {count} by price per m² · {date}",
        "subheading_llm": "Top {count} by AI match score · {date}",
        "per_month": "/mo.",
        "open_sheet": "Open the full list in Google Sheets ↗",
        "generated_by": "Automatically generated by apartment_scraper",
        "offer_default_name": "Offer",
        "llm_match": "AI match",
    },
    "pl": {
        "lang_attr": "pl",
        "heading": "Najlepsze oferty: {profile} 🏠",
        "subheading": "Top {count} według ceny za m² · {date}",
        "subheading_llm": "Top {count} według dopasowania AI · {date}",
        "per_month": "/mies.",
        "open_sheet": "Otwórz pełną listę w Google Sheets ↗",
        "generated_by": "Wygenerowano automatycznie przez apartment_scraper",
        "offer_default_name": "Oferta",
        "llm_match": "Dopasowanie AI",
    },
}


def _email_text(language: str) -> dict:
    return EMAIL_TEXT.get(language, EMAIL_TEXT["en"])


_OFFER_ROW_TMPL = """\
<tr style="border-bottom:1px solid #e5e7eb;">
  <td style="padding:12px 8px;width:90px;vertical-align:top;">
    {img_html}
  </td>
  <td style="padding:12px 8px;vertical-align:top;">
    <a href="{link}" style="font-size:15px;font-weight:600;color:#1a56db;text-decoration:none;">{name}</a>
    <span style="margin-left:8px;background:#f3f4f6;border-radius:3px;padding:1px 6px;font-size:11px;color:#6b7280;vertical-align:middle;">{source}</span>
    <div style="margin-top:4px;font-size:13px;color:#6b7280;">{address}{distance}</div>
    <div style="margin-top:6px;">
      <span style="font-size:18px;font-weight:700;color:#111827;">{price} zł</span>
      &nbsp;<span style="font-size:13px;color:#6b7280;">{per_month}</span>
      &nbsp;&nbsp;
      <span style="background:#f3f4f6;border-radius:4px;padding:2px 7px;font-size:13px;color:#374151;">
        {area} m²
      </span>
      &nbsp;&nbsp;
      <span style="background:#eff6ff;border-radius:4px;padding:2px 7px;font-size:13px;color:#1d4ed8;font-weight:600;">
        {price_m2} zł/m²
      </span>
    </div>
    <div style="margin-top:6px;font-size:12px;color:#6b7280;line-height:1.5;">{desc}</div>
    {llm_badge}
  </td>
</tr>
"""

_EMAIL_TMPL = """\
<!DOCTYPE html>
<html lang="{lang_attr}">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:'Helvetica Neue',Arial,sans-serif;">
<div style="max-width:680px;margin:32px auto;background:#fff;border-radius:10px;
            box-shadow:0 1px 6px rgba(0,0,0,.08);overflow:hidden;">

  <!-- Header -->
  <div style="background:#1a56db;padding:24px 32px;">
    <h1 style="margin:0;color:#fff;font-size:22px;font-weight:700;">
      {heading}
    </h1>
    <p style="margin:4px 0 0;color:#bfdbfe;font-size:14px;">
      {subheading}
    </p>
  </div>

  <!-- Body -->
  <div style="padding:8px 24px 24px;">
    <table style="width:100%;border-collapse:collapse;">
{rows}
    </table>
  </div>

  <!-- Footer -->
  <div style="background:#f3f4f6;padding:16px 32px;text-align:center;font-size:12px;color:#9ca3af;">
    <a href="{sheet_url}" style="color:#1a56db;text-decoration:none;font-weight:600;">
      {open_sheet}
    </a>
    &nbsp;·&nbsp; {generated_by}
  </div>

</div>
</body>
</html>
"""


def _llm_badge(row, text: dict) -> str:
    """Return an HTML snippet with the LLM match score badge, or empty string."""
    score_raw = str(row.get("LLM_Score", "") or "").strip()
    if not score_raw or score_raw == "nan":
        return ""
    try:
        s = int(float(score_raw))
    except (ValueError, TypeError):
        return ""
    summary = str(row.get("LLM_Summary", "") or "").strip()
    if s >= 8:
        bg, fg = "#dcfce7", "#15803d"
    elif s >= 5:
        bg, fg = "#fef9c3", "#854d0e"
    else:
        bg, fg = "#fee2e2", "#b91c1c"
    label = text.get("llm_match", "AI match")
    summary_html = (
        f'<span style="margin-left:8px;font-size:11px;color:#6b7280;">{summary}</span>'
        if summary else ""
    )
    return (
        f'<div style="margin-top:5px;">'
        f'<span style="background:{bg};color:{fg};border-radius:4px;padding:2px 8px;'
        f'font-size:12px;font-weight:700;">{label}: {s}/10</span>'
        f'{summary_html}</div>'
    )


def _format_distance(row) -> str:
    try:
        km = float(row.get("Distance_km", "") or "")
        mins = float(row.get("Duration_min", "") or "")
        return f" · {km:.1f} km / {int(mins)} min"
    except (ValueError, TypeError):
        return ""


def _img_tag(url: str) -> str:
    url = str(url or "").strip()
    if not url:
        return "<div style='width:80px;height:60px;background:#e5e7eb;border-radius:4px;'></div>"
    return (
        f'<img src="{url}" width="80" height="60" '
        f'style="object-fit:cover;border-radius:4px;display:block;" alt="">'
    )


def generate_email_html(top_df: pd.DataFrame, profile: str, sheet_id: str, today: str, language: str = "en") -> str:
    text = _email_text(language)
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}" if sheet_id else "#"
    rows_html = []

    for _, row in top_df.iterrows():
        price = row.get("Full_value", "")
        try:
            price = f"{int(float(price)):,}".replace(",", "\u00a0")
        except (ValueError, TypeError):
            price = str(price)

        area = row.get("Area", "")
        try:
            area = int(float(area))
        except (ValueError, TypeError):
            pass

        price_m2 = row.get("Price_per_m2", "")
        try:
            price_m2 = f"{int(float(price_m2)):,}".replace(",", "\u00a0")
        except (ValueError, TypeError):
            price_m2 = str(price_m2)

        llm_desc = str(row.get("LLM_Description", "") or "").strip()
        if llm_desc and llm_desc != "nan":
            desc_excerpt = llm_desc
        else:
            opis = str(row.get("Description", "") or "")
            opis = re.sub(r'^Opis\s*', '', opis, flags=re.IGNORECASE)
            desc_excerpt = opis[:350].replace("\n", " ").strip()
            if len(opis) > 350:
                desc_excerpt += "…"

        rows_html.append(_OFFER_ROW_TMPL.format(
            img_html=_img_tag(row.get("Image_URL", "")),
            link=str(row.get("Link", "#") or "#"),
            name=str(row.get("Name", text["offer_default_name"]) or text["offer_default_name"])[:90],
            source=str(row.get("Source", "") or ""),
            address=str(row.get("Address", "") or ""),
            distance=_format_distance(row),
            price=price,
            area=area,
            price_m2=price_m2,
            desc=desc_excerpt,
            per_month=text["per_month"],
            llm_badge=_llm_badge(row, text),
        ))

    has_llm_scores = (
        "LLM_Score" in top_df.columns
        and pd.to_numeric(top_df["LLM_Score"], errors="coerce").notna().any()
    )
    subheading_key = "subheading_llm" if has_llm_scores else "subheading"
    return _EMAIL_TMPL.format(
        lang_attr=text["lang_attr"],
        heading=text["heading"].format(profile=profile),
        subheading=text[subheading_key].format(count=len(top_df), date=today),
        open_sheet=text["open_sheet"],
        generated_by=text["generated_by"],
        sheet_url=sheet_url,
        rows="\n".join(rows_html),
    )


# ── SMTP sending ──────────────────────────────────────────────────────────────

def send_email_report(
    offers_df: pd.DataFrame,
    config: dict,
    profile: str,
    sheet_id: str,
    today: str,
) -> bool:
    """
    Filter offers, pick top N by price/m², and send an HTML email digest.
    Returns True on success.

    Required config keys:
      email_recipient, email_sender, email_app_password
    Optional:
      email_smtp_host (default smtp.gmail.com), email_smtp_port (default 587)
      email_districts - comma-separated district names to restrict the digest to
                        (e.g. "Żoliborz, Bielany, Śródmieście, Wola, Mokotów").
                        Empty/absent → no district filter.
      email_top_n - how many top offers to include (default TOP_N). e.g. kawalerka → 15.
    """
    recipient = config.get("email_recipient", "").strip()
    sender    = config.get("email_sender", "").strip()
    password  = config.get("email_app_password", "").strip()

    if not (recipient and sender and password):
        logger.debug("Email report skipped - email_recipient/sender/app_password not configured")
        return False

    smtp_host = config.get("email_smtp_host", "smtp.gmail.com").strip() or "smtp.gmail.com"
    smtp_port = int(config.get("email_smtp_port", 587) or 587)

    city_re     = _build_city_regex(config.get("city", ""))
    district_re = _build_district_regex(config.get("email_districts", ""))

    try:
        top_n = int(config.get("email_top_n", TOP_N) or TOP_N)
    except (ValueError, TypeError):
        top_n = TOP_N

    logger.info("📧 Preparing email report...")

    quality = filter_quality_offers(offers_df, district_re, city_re)
    logger.info(f"📧 Quality filter: {len(quality)}/{len(offers_df)} offers passed")

    if quality.empty:
        logger.warning("📧 No quality offers to send - skipping email")
        return False

    ranked = add_price_per_m2(quality)
    # Sanity floor: drop per-room/placeholder prices below MIN_PRICE_PER_M2 zł/m².
    ppm = pd.to_numeric(ranked["Price_per_m2"], errors="coerce")
    ranked = ranked[ppm >= MIN_PRICE_PER_M2]
    # When LLM scores are available, rank by score descending (best match first).
    if "LLM_Score" in ranked.columns:
        llm_numeric = pd.to_numeric(ranked["LLM_Score"], errors="coerce")
        if llm_numeric.notna().any():
            ranked = ranked.assign(_llm_num=llm_numeric).sort_values(
                "_llm_num", ascending=False, na_position="last"
            ).drop(columns=["_llm_num"])
    top_df = ranked.head(top_n)

    if top_df.empty:
        logger.warning("📧 No offers left after price/m² floor - skipping email")
        return False

    language = config.get("language", "en").strip().lower() or "en"
    html = generate_email_html(top_df, profile, sheet_id, today, language)

    recipients = [r.strip() for r in recipient.split(",") if r.strip()]
    subject = f"🏠 Top {len(top_df)} {profile} · {today}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, recipients, msg.as_string())
        logger.info(f"📧 Email sent to {', '.join(recipients)} ({len(top_df)} offers)")
        return True
    except Exception as e:
        logger.error(f"📧 Failed to send email: {e}")
        return False
