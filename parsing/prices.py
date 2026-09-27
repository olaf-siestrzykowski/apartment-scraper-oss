"""Price parsing and the monthly total-cost calculation."""
import re

import numpy as np
import pandas as pd


def _number_from_text(text, default=0, allow_float=True):
    if not text:
        return default
    m = re.search(r'(\d+(?:[.,]\d+)?)', text)
    if not m:
        return default
    val = m.group(1).replace(",", ".")
    try:
        return float(val) if allow_float else int(float(val))
    except ValueError:
        return default


def extract_price_from_text(price_text: str) -> float:
    """
    Extract a price from text, handling various Polish number formats.
    """
    if not price_text:
        return 0.0
    
    cleaned = re.sub(r'[^\d\s\.,]', '', price_text)
    cleaned = cleaned.replace(' ', '').replace('\u00A0', '')
    
    # Handle formats using both a dot and a comma
    if '.' in cleaned and ',' in cleaned:
        if cleaned.rfind(',') > cleaned.rfind('.'):
            cleaned = cleaned.replace('.', '').replace(',', '.')
        else:
            cleaned = cleaned.replace(',', '')
    elif ',' in cleaned:
        cleaned = cleaned.replace(',', '.')
    elif '.' in cleaned:
        parts = cleaned.split('.')
        if len(parts) == 2 and len(parts[1]) == 3:
            cleaned = cleaned.replace('.', '')
    
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def add_total_costs(df: pd.DataFrame) -> pd.DataFrame:
    """Derive Additional_value and Full_value (monthly total) in place.

    - "Czynsz (dodatkowo)" (Otodom's extra rent field, free text) is parsed to a number
      and, when present, overrides Additional_value
    - Full_value = Base_value + Additional_value, except when both are equal: some
      listings repeat the base rent in the extra-fee field, so the sum would double it
      (known limitation - a genuine fee equal to the rent is also collapsed, see ISSUES.md)
    """
    if "Czynsz (dodatkowo)" in df.columns:
        df["Czynsz (dodatkowo) value"] = (
            df["Czynsz (dodatkowo)"].astype(str)
            .str.extract(r"(\d+(?:[\.,]\d+)?)")[0]
            .fillna("0")
            .str.replace(",", ".", regex=False)
            .astype(float)
        )
    else:
        df["Czynsz (dodatkowo) value"] = 0.0

    if "Additional_value" not in df.columns:
        df["Additional_value"] = 0.0
    df["Additional_value"] = np.where(
        df["Czynsz (dodatkowo) value"] == 0.0,
        df["Additional_value"],
        df["Czynsz (dodatkowo) value"],
    )

    if "Base_value" not in df.columns:
        df["Base_value"] = 0.0
    base = df["Base_value"].fillna(0)
    additional = df["Additional_value"].fillna(0)
    df["Full_value"] = np.where(additional == base, base, base + additional)
    return df
