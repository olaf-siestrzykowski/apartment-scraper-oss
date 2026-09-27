"""Polish listing dates ("Odświeżono dzisiaj o 14:30", "15 listopada 2025")."""
import datetime as dt
import re
from datetime import timedelta


def parse_polish_date(date_text: str) -> str:
    """
    Improved Polish date-parsing function.
    Handles all the date formats seen on OLX/Otodom.
    """
    if not date_text:
        return None

    months = {
        "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, 
        "maja": 5, "czerwca": 6, "lipca": 7, "sierpnia": 8, 
        "września": 9, "października": 10, "listopada": 11, "grudnia": 12
    }

    text = date_text.lower().strip()
    
    # 1. "Dzisiaj o 15:04" / "Dzisiaj"
    if "dzisiaj" in text:
        return dt.datetime.today().strftime("%Y-%m-%d")
    
    # 2. "Wczoraj o 14:30" / "Wczoraj"
    if "wczoraj" in text:
        return (dt.datetime.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    
    # 3. "Odświeżono Dzisiaj o 16:05"
    if "odświeżono" in text and "dzisiaj" in text:
        return dt.datetime.today().strftime("%Y-%m-%d")
    
    # 4. "X dni temu" / "X dzień temu"
    match = re.search(r'(\d+)\s+(?:dni|dzień|dnia)\s+temu', text)
    if match:
        days = int(match.group(1))
        return (dt.datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    
    # 5. "16 listopada 2025" / "Odświeżono dnia 17 listopada 2025"
    match = re.search(r'(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})', text)
    if match:
        day = int(match.group(1))
        month_name = match.group(2)
        year = int(match.group(3))
        
        month = months.get(month_name)
        if month:
            try:
                return dt.datetime(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                pass
    
    # 6. "17 listopada 2025" (without "dnia")
    match = re.search(r'(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})', text)
    if match:
        day = int(match.group(1))
        month_name = match.group(2)
        year = int(match.group(3))
        
        month = months.get(month_name)
        if month:
            try:
                return dt.datetime(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                pass
    
    return None
