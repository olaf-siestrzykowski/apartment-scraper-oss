"""Structured fields extracted from the free-text listing description."""
import logging
import re
from typing import Any, Dict

import pandas as pd

# Same named logger the scraper configures in main()
logger = logging.getLogger("logging_config")


# Metro station names for extraction
METRO_STATIONS = [
    'Młociny', 'Wawrzyszew', 'Stare Bielany', 'Słodowiec', 'Marymont',
    'Plac Wilsona', 'Dworzec Gdański', 'Ratusz Arsenał', 'Świętokrzyska',
    'Centrum', 'Politechnika', 'Pole Mokotowskie', 'Racławicka', 'Wierzbno',
    'Wilanowska', 'Służew', 'Ursynów', 'Stokłosy', 'Imielin', 'Kabaty',
    'Rondo Daszyńskiego', 'Rondo ONZ', 'Nowy Świat', 'Stadion Narodowy',
    'Dworzec Wileński', 'Szwedzka', 'Targówek Mieszkaniowy', 'Trocka', 'Zacisze',
    'Kondratowicza', 'Bródno', 'Bemowo', 'Ulrychów', 'Księcia Janusza', 'Płocka',
    'Młynów', 'Chrzanów'
]


def extract_custom_fields_from_opis(opis: str) -> Dict[str, Any]:
    """
    Extract custom fields from Polish apartment description (Description).
    Returns dictionary with extracted values.
    """
    if not opis or pd.isna(opis):
        return {}

    opis_str = str(opis)
    opis_lower = opis_str.lower()
    result = {}

    # 1. DEPOSIT - extract numeric value
    kaucja_patterns = [
        r'[Kk]aucj[aęy]\s*(?:zwrotna)?:?\s*[\s:]*(\d[\d\s,\.]*)\s*(?:zł|PLN|pln)?',
        r'[Kk]aucj[aęy]\s*(?:w\s+wysokości)?:?\s*(\d[\d\s,\.]*)\s*(?:zł|PLN|pln)?',
    ]
    for pattern in kaucja_patterns:
        match = re.search(pattern, opis_str)
        if match:
            try:
                value = match.group(1).replace(' ', '').replace(',', '.').replace('\u202f', '')
                # Handle values like "3 500" -> "3500"
                value = re.sub(r'\s+', '', value)
                result['Deposit_PLN'] = float(value)
                break
            except (ValueError, AttributeError):
                pass

    # 2. METRO STATION - find mentioned metro stations
    found_stations = []
    for station in METRO_STATIONS:
        # Look for "metro Station" or "Metro Station" pattern
        pattern = rf'\b[Mm]etr[oa]?\s+{re.escape(station)}\b'
        if re.search(pattern, opis_str, re.IGNORECASE):
            found_stations.append(station)
        # Also check for just the station name in context of metro
        elif station.lower() in opis_lower and 'metro' in opis_lower:
            # Check if station is mentioned near "metro"
            pattern2 = rf'\b{re.escape(station)}\b'
            if re.search(pattern2, opis_str, re.IGNORECASE):
                found_stations.append(station)

    if found_stations:
        result['Metro_station'] = ', '.join(list(dict.fromkeys(found_stations)))  # Remove duplicates

    # 3. METRO DISTANCE (minutes to metro)
    metro_dist_patterns = [
        r'(?:do\s+)?metr[ao]?\s*(?:\w+\s+)?(\d+)\s*min',
        r'(\d+)\s*min(?:ut)?\s*(?:do\s+)?metr[ao]',
        r'(\d+)\s*min(?:ut)?\s*(?:pieszo|spacerem)?\s*(?:do\s+)?metr[ao]',
        r'metr[ao]\s*-?\s*(\d+)\s*min',
    ]
    for pattern in metro_dist_patterns:
        match = re.search(pattern, opis_lower)
        if match:
            try:
                result['Metro_distance_min'] = int(match.group(1))
                break
            except (ValueError, AttributeError):
                pass

    # 4. AVAILABLE FROM
    if re.search(r'(?:dostępn[eay]|woln[eay])\s+od\s+zaraz|od\s+zaraz', opis_lower):
        result['Available_From'] = 'immediately'
    else:
        date_patterns = [
            r'(?:dostępn[eay]|woln[eay])\s+od\s+(\d{1,2}[\.\/]\d{1,2}[\.\/]?\d{0,4})',
            r'(?:dostępn[eay]|woln[eay])\s+od\s+(\d{1,2}\s+(?:stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|września|października|listopada|grudnia)(?:\s+\d{4})?)',
            r'od\s+(\d{1,2}\s+(?:stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|września|października|listopada|grudnia))',
        ]
        for pattern in date_patterns:
            match = re.search(pattern, opis_lower)
            if match:
                result['Available_From'] = match.group(1).strip()
                break

    # 5. OCCASIONAL LEASE (Polish "najem okazjonalny" rental agreement)
    if re.search(r'najem\s+okazjonalny|umow[ay]\s+(?:najmu\s+)?okazjonalne[goj]', opis_lower):
        result['Occasional_Lease'] = 'Yes'

    # 6. DIRECT FROM OWNER
    if re.search(r'bezpośredni[eo]|bez\s+pośredni[kó]w|bez\s+agencj[iy]|bez\s+prowizj[iy]', opis_lower):
        result['Direct_From_Owner'] = 'Yes'

    # 7. MINIMUM PERIOD (minimum rental period in months)
    min_okres_patterns = [
        r'(?:minimum|min\.?)\s*(\d+)\s*(?:rok|lat)',
        r'(?:minimum|min\.?)\s*(\d+)\s*miesi[aąę]c',
        r'(?:umowa\s+)?na\s+(?:min(?:imum)?\.?\s*)?(\d+)\s*rok',
        r'(?:umowa\s+)?na\s+(?:min(?:imum)?\.?\s*)?(\d+)\s*miesi[aąę]c',
        r'na\s+minimum\s+(\d+)\s*(?:rok|lat|miesi)',
        # "minimalny okres (najmu)" - adjective form, not covered by "minimum"/"min" above
        r'minimaln[aey]?\s+okres(?:\s+najmu)?:?\s*(?:na\s*)?(\d+)\s*(?:rok|lat)',
        r'minimaln[aey]?\s+okres(?:\s+najmu)?:?\s*(?:na\s*)?(\d+)\s*miesi[aąę]c',
    ]
    for pattern in min_okres_patterns:
        match = re.search(pattern, opis_lower)
        if match:
            try:
                value = int(match.group(1))
                # If it's in years, convert to months
                if 'rok' in pattern or 'lat' in pattern:
                    value = value * 12
                result['Min_Period_Months'] = value
                break
            except (ValueError, AttributeError):
                pass

    # 8. BALCONY
    if re.search(r'\b(?:balkon|balkonu|balkonem|loggi[aąę])\b', opis_lower):
        result['Balcony'] = 'Yes'

    # 9. AIR CONDITIONING
    if re.search(r'klimatyzacj[aąę]|klimatyzowany|klimatyzowane', opis_lower):
        result['Air_Conditioning'] = 'Yes'

    # 10. INTERNET (internet included)
    if re.search(r'\binternet\b|wi-?fi|\bwifi\b|światłowód', opis_lower):
        # Check if internet is included in price
        if re.search(r'internet\s+(?:w\s+cenie|wliczony|gratis|bezpłatny)', opis_lower):
            result['Internet'] = 'Included in price'
        else:
            result['Internet'] = 'Available'

    # 11. BUILD YEAR
    rok_patterns = [
        r'(?:z|budyn(?:ek|ku)\s+z|blok\s+z)\s+(\d{4})\s*r?\.?',
        r'z\s+roku\s+(\d{4})',
        r'wybudowan[yae]\s+w\s+(\d{4})',
    ]
    for pattern in rok_patterns:
        match = re.search(pattern, opis_str)
        if match:
            try:
                year = int(match.group(1))
                if 1900 <= year <= 2030:
                    result['Build_Year'] = year
                    break
            except (ValueError, AttributeError):
                pass

    # 12. RENOVATED
    if re.search(r'po\s+(?:generalnym\s+)?remoncie|świeżo\s+(?:od|wy)?nowion|wyremontowa[ny]|odnowion', opis_lower):
        result['Renovated'] = 'Yes'
    elif re.search(r'do\s+(?:dużego\s+)?remontu', opis_lower):
        result['Renovated'] = 'Needs renovation'

    # 13. NON-SMOKING
    if re.search(r'niepalą[cyceąi]+|dla\s+niepalących|osoby?\s+niepalą[cyceąi]+|palą[cyceąi]+\s+nie', opis_lower):
        result['Non_Smoking'] = 'Yes'

    # 14. NO PETS (from description - more reliable than the form field)
    if re.search(r'bez\s+zwierząt|nie\s+akceptuj[ęe]\s+zwierząt|zwierząt[aom]+\s+nie|zwierzęt[aom]+\s+dziękuj', opis_lower):
        result['No_Pets_Desc'] = 'Yes'
    elif re.search(r'(?:mile\s+widziane?\s+)?zwierzęt[aomy]+\s+(?:ok|akceptowane|mile)|przyjazne?\s+(?:dla\s+)?zwierząt|akceptuj[ęe]\s+zwierząt|ze\s+zwierzęt', opis_lower):
        result['No_Pets_Desc'] = 'No (pets accepted)'

    # 15. WASHING MACHINE
    if re.search(r'\bpralk[aąę]\b', opis_lower):
        result['Washing_Machine'] = 'Yes'

    # 16. TARGET TENANT
    target = []
    if re.search(r'dla\s+(?:1-?2|dwóch?)\s+osób?|idealne?\s+dla\s+pary|dla\s+pary', opis_lower):
        target.append('couple')
    if re.search(r'dla\s+singla|dla\s+jednej?\s+osoby', opis_lower):
        target.append('single')
    if re.search(r'dla\s+student[aówki]+|idealne?\s+dla\s+student', opis_lower):
        target.append('student')
    if re.search(r'dla\s+kobiet[y]?|dla\s+dziewczyn[y]?', opis_lower):
        target.append('woman')
    if re.search(r'dla\s+rodzin[y]?|z\s+dziećmi', opis_lower):
        target.append('family')
    if target:
        result['Target_Tenant'] = ', '.join(target)

    return result


def apply_custom_fields_extraction(df: pd.DataFrame) -> pd.DataFrame:
    """
    Apply custom field extraction from Description column to entire DataFrame.
    Returns DataFrame with new columns added.
    """
    if 'Description' not in df.columns:
        logger.warning("⚠️ No 'Description' column found - skipping custom field extraction")
        return df

    logger.info("🔍 Extracting custom fields from descriptions...")

    # Define all possible custom columns with default empty values
    custom_columns = [
        'Deposit_PLN', 'Metro_station', 'Metro_distance_min', 'Available_From',
        'Occasional_Lease', 'Direct_From_Owner', 'Min_Period_Months', 'Balcony',
        'Air_Conditioning', 'Internet', 'Build_Year', 'Renovated',
        'Non_Smoking', 'No_Pets_Desc', 'Washing_Machine', 'Target_Tenant'
    ]

    # Initialize columns if they don't exist
    for col in custom_columns:
        if col not in df.columns:
            df[col] = ''

    # Extract custom fields for each row
    extracted_count = 0
    for idx, row in df.iterrows():
        opis = row.get('Description', '')
        if pd.isna(opis) or not opis:
            continue

        extracted = extract_custom_fields_from_opis(opis)
        if extracted:
            extracted_count += 1
            for key, value in extracted.items():
                df.at[idx, key] = value

    logger.info(f"✅ Extracted custom fields from {extracted_count}/{len(df)} descriptions")

    # Log statistics for each field
    for col in custom_columns:
        non_empty = df[col].notna() & (df[col] != '')
        count = non_empty.sum()
        if count > 0:
            logger.debug(f"  {col}: {count} values extracted")

    return df
