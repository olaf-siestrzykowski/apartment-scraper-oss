"""Google Sheets output: config tab, listing status columns, header translation."""
import datetime as dt
import logging
import os
from pathlib import Path

import gspread
import pandas as pd
from oauth2client.service_account import ServiceAccountCredentials

logger = logging.getLogger("logging_config")

# Google Sheets credentials path - resolved relative to the project root
GOOGLE_CREDS_PATH = os.environ.get(
    'GOOGLE_CREDS_PATH',
    str(Path(__file__).resolve().parent.parent / 'google-credentials.json')
)


def _open_spreadsheet(sheet_id: str):
    """Open a gspread spreadsheet using the service account credentials."""
    json_creds = GOOGLE_CREDS_PATH
    if not Path(json_creds).exists():
        raise RuntimeError(
            f"Google credentials not found: {json_creds}\n"
            f"Set GOOGLE_CREDS_PATH or run 'python setup.py' to get set up."
        )
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_name(str(json_creds), scope)
    client = gspread.authorize(creds)
    return client.open_by_key(sheet_id)


def _apply_config_dropdowns(spreadsheet, ws) -> None:
    """Add dropdown data-validation to Transport Mode, Schedule Days, and Schedule Time."""
    # Row indices (0-based):
    #   0: header, 1: Reference Address, 2: OLX URL, 3: Otodom URL,
    #   4: Transport Mode, 5: Schedule Days, 6: Schedule Time,
    #   7: Telegram Bot Token, 8: Telegram Chat ID, ...
    TRANSPORT_ROW    = 4
    SCHEDULE_DAYS_ROW = 5
    SCHEDULE_TIME_ROW = 6
    LANGUAGE_ROW = 14
    VALUE_COL = 1  # column B (0-based)

    transport_options = [
        "foot-walking",
        "cycling-regular",
        "driving-car",
    ]
    days_options = [
        "",
        "daily",
        "weekdays",
        "weekend",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "monday,thursday",
        "tuesday,friday",
        "monday,wednesday,friday",
    ]
    time_options = [
        "07:00",
        "08:00",
        "09:00",
        "10:00",
        "12:00",
        "14:00",
        "16:00",
        "18:00",
        "20:00",
    ]
    language_options = [
        "en",
        "pl",
    ]

    def _validation_request(row_idx: int, options: list) -> dict:
        return {
            "setDataValidation": {
                "range": {
                    "sheetId": ws.id,
                    "startRowIndex": row_idx,
                    "endRowIndex": row_idx + 1,
                    "startColumnIndex": VALUE_COL,
                    "endColumnIndex": VALUE_COL + 1,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": o} for o in options],
                    },
                    "showCustomUi": True,
                    "strict": False,
                },
            }
        }

    try:
        spreadsheet.batch_update({"requests": [
            _validation_request(TRANSPORT_ROW,     transport_options),
            _validation_request(SCHEDULE_DAYS_ROW, days_options),
            _validation_request(SCHEDULE_TIME_ROW, time_options),
            _validation_request(LANGUAGE_ROW,      language_options),
        ]})
        logger.info("📋 Config dropdowns applied")
    except Exception as e:
        logger.warning(f"Could not apply config dropdowns: {e}")


def load_config_sheet(spreadsheet, search_profile: str,
                      default_olx_url: str, default_otodom_url: str,
                      default_origin: str) -> dict:
    """Load or create the 'config' worksheet with per-profile settings.

    Sheet layout (all Value cells are user-editable):
        Setting             | Value                  | Notes
        Reference Address   | <address>              | destination for distance calc
        OLX URL             | <url>                  | edit to change OLX search
        Otodom URL          | <url>                  | edit to change Otodom search
        Transport Mode      | foot-walking           | driving-car / cycling-regular / foot-walking
        Schedule Days       |                        | monday, tuesday,friday, weekdays, weekend, etc.
        Schedule Time       | 08:00                  | time to run scraper
        Telegram Bot Token  |                        | BotFather token
        Telegram Chat ID    |                        | numeric chat ID
        Profile             | <auto>                 | set automatically
        Last scraped        | <auto>                 | set automatically

    Returns dict with keys: reference_address, olx_url, otodom_url,
                            transport_mode, schedule_days, schedule_time,
                            telegram_bot_token, telegram_chat_id
    """
    import gspread as _gspread

    SETTING_KEYS = {
        "Reference Address":  "reference_address",
        "OLX URL":            "olx_url",
        "Otodom URL":         "otodom_url",
        "Transport Mode":     "transport_mode",
        "Schedule Days":      "schedule_days",
        "Schedule Time":      "schedule_time",
        "Telegram Bot Token": "telegram_bot_token",
        "Telegram Chat ID":   "telegram_chat_id",
        "Email Recipient":    "email_recipient",
        "Email Sender":       "email_sender",
        "Email App Password": "email_app_password",
        "Email Districts":    "email_districts",
        "Email Top N":        "email_top_n",
        "Language":           "language",
    }
    defaults = {
        "reference_address":  default_origin,
        "olx_url":            default_olx_url,
        "otodom_url":         default_otodom_url,
        "transport_mode":     "foot-walking",
        "schedule_days":      "",
        "schedule_time":      "08:00",
        "telegram_bot_token": "",
        "telegram_chat_id":   "",
        "email_recipient":    "",
        "email_sender":       "",
        "email_app_password": "",
        "email_districts":    "",
        "email_top_n":        "",
        # Blank = not set in the sheet, so the profile's "language" (or "en") applies
        "language":           "",
    }

    default_rows = [
        ["Setting", "Value", "Notes"],
        ["Reference Address",  default_origin,    "Edit to change the destination for distance calculation"],
        ["OLX URL",            default_olx_url,   "Edit to change the OLX search query"],
        ["Otodom URL",         default_otodom_url, "Edit to change the Otodom search query"],
        ["Transport Mode",     "foot-walking",     "Options: foot-walking, cycling-regular, driving-car"],
        ["Schedule Days",      "",                 "Days to run: monday, tuesday,friday, weekdays, weekend, etc. (or: daily)"],
        ["Schedule Time",      "08:00",            "Time to run scraper, e.g. 08:00, 18:00"],
        ["Telegram Bot Token", "",                 "Token from @BotFather - leave blank to disable notifications"],
        ["Telegram Chat ID",   "",                 "Your Telegram chat ID (get via @userinfobot)"],
        ["Email Recipient",    "",                 "Email address(es) to send the daily digest (comma-separated)"],
        ["Email Sender",       "",                 "Gmail address used to send the email"],
        ["Email App Password", "",                 "Gmail App Password (Google Account → Security → App Passwords)"],
        ["Email Districts",    "",                 "Comma-separated districts to limit the digest to, e.g. Żoliborz, Bielany, Śródmieście, Wola, Mokotów (blank = all Warsaw)"],
        ["Email Top N",        "",                 "How many top offers (by price/m²) to include in the digest (blank = 10)"],
        ["Language",           "en",               "Language for Sheet headers, email digest and Telegram messages: en or pl"],
        ["Profile",            search_profile,     "Set automatically - do not edit"],
        ["Last scraped",       "",                 "Set automatically - do not edit"],
    ]

    try:
        ws = spreadsheet.worksheet("config")
        data = ws.get_all_values()

        config = dict(defaults)
        old_schedule_val = None   # None = row absent; "" = row present but blank
        has_new_schedule_rows = False
        for row in data[1:]:
            if len(row) >= 1:
                key = row[0]
                val = row[1].strip() if len(row) >= 2 else ""
                if key == "Schedule":           # old single-row format
                    old_schedule_val = val
                elif key == "Schedule Days":
                    has_new_schedule_rows = True
                if key in SETTING_KEYS and val:
                    config[SETTING_KEYS[key]] = val

        # Migrate old single "Schedule" row to two-row format
        if old_schedule_val is not None and not has_new_schedule_rows:
            logger.info("📋 Migrating config sheet: splitting 'Schedule' into 'Schedule Days' + 'Schedule Time'")
            # Try to parse "monday 08:00" → days="monday", time="08:00"
            parts = old_schedule_val.strip().lower().split()
            migrated_days = parts[0] if len(parts) >= 1 else ""
            migrated_time = parts[1] if len(parts) >= 2 else "08:00"
            # Rebuild the sheet with the new layout
            new_rows = []
            schedule_inserted = False
            for row in data:
                if len(row) >= 1 and row[0] == "Schedule":
                    new_rows.append(["Schedule Days", migrated_days, "Days to run: monday, tuesday,friday, weekdays, weekend, etc."])
                    new_rows.append(["Schedule Time", migrated_time, "Time to run scraper, e.g. 08:00, 18:00"])
                    schedule_inserted = True
                else:
                    padded = (row + ["", "", ""])[:3]
                    new_rows.append(padded)
            if not schedule_inserted:
                # Insert before Telegram rows if Schedule row was absent
                insert_idx = next(
                    (i for i, r in enumerate(new_rows) if r[0] == "Telegram Bot Token"),
                    len(new_rows)
                )
                new_rows.insert(insert_idx, ["Schedule Days", migrated_days, "Days to run: monday, tuesday,friday, weekdays, weekend, etc."])
                new_rows.insert(insert_idx + 1, ["Schedule Time", migrated_time, "Time to run scraper, e.g. 08:00, 18:00"])
            ws.clear()
            ws.update(values=new_rows, range_name="A1")
            try:
                ws.format("A1:C1", {"textFormat": {"bold": True}})
                ws.format("A2:A12", {"textFormat": {"bold": True}})
            except Exception:
                pass
            _apply_config_dropdowns(spreadsheet, ws)
            config["schedule_days"] = migrated_days
            config["schedule_time"] = migrated_time

        logger.info(f"📋 Config - address: {config['reference_address'][:50]}")
        logger.info(f"📋 Config - transport: {config['transport_mode']}")
        logger.info(f"📋 Config - schedule: {config['schedule_days'] or '(disabled)'} {config['schedule_time']}")
        return config

    except _gspread.exceptions.WorksheetNotFound:
        logger.info(f"📋 Creating 'config' sheet for profile '{search_profile}'")
        ws = spreadsheet.add_worksheet(title="config", rows=15, cols=3)
        ws.update(values=default_rows, range_name="A1")
        try:
            ws.format("A1:C1", {"textFormat": {"bold": True}})
            ws.format("A2:A10", {"textFormat": {"bold": True}})
        except Exception:
            pass
        _apply_config_dropdowns(spreadsheet, ws)
        logger.info("📋 Config sheet created")
        return dict(defaults)


def update_config_last_scraped(spreadsheet, search_profile: str) -> None:
    """Update the 'Last scraped' and 'Profile' rows in the config sheet."""
    import gspread as _gspread
    try:
        ws = spreadsheet.worksheet("config")
        data = ws.get_all_values()
        for i, row in enumerate(data):
            if row and row[0] == "Last scraped":
                ws.update_cell(i + 1, 2, dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
            elif row and row[0] == "Profile":
                ws.update_cell(i + 1, 2, search_profile)
    except _gspread.exceptions.WorksheetNotFound:
        pass
    except Exception as e:
        logger.warning(f"Could not update config sheet: {e}")


def read_existing_status_map(worksheet) -> dict:
    """Read Link → Status mapping from the current sheet before overwriting it."""
    try:
        data = worksheet.get_all_values()
        if not data or len(data) < 2:
            return {}
        header = data[0]
        try:
            link_col = header.index("Link")
            status_col = header.index("Status")
        except ValueError:
            return {}
        result = {}
        for row in data[1:]:
            if len(row) > max(link_col, status_col):
                link = row[link_col].strip()
                status = row[status_col].strip()
                if link and status and status != "New":
                    result[link] = status
        return result
    except Exception as e:
        logger.warning(f"Could not read existing Status map: {e}")
        return {}


def read_existing_llm_scores_map(worksheet) -> dict:
    """Read Link → {"score", "summary", "description", "fees", "admin_fee"} from the sheet for LLM caching."""
    try:
        data = worksheet.get_all_values()
        if not data or len(data) < 2:
            return {}
        # "pl" profiles write translated headers ("Ocena AI") - map them back
        header = untranslate_header_row(data[0])
        try:
            link_col = header.index("Link")
            score_col = header.index("LLM_Score")
            summary_col = header.index("LLM_Summary")
        except ValueError:
            return {}
        desc_col = header.index("LLM_Description") if "LLM_Description" in header else None
        fees_col = header.index("LLM_Fees") if "LLM_Fees" in header else None
        admin_fee_col = header.index("LLM_Admin_Fee") if "LLM_Admin_Fee" in header else None

        def _cell(row, col):
            return row[col].strip() if col is not None and len(row) > col else ""
        result = {}
        for row in data[1:]:
            max_col = max(link_col, score_col, summary_col)
            if len(row) > max_col:
                link = row[link_col].strip()
                score = row[score_col].strip()
                summary = row[summary_col].strip() if len(row) > summary_col else ""
                if link and score and score not in ("", "nan"):
                    result[link] = {
                        "score": score,
                        "summary": summary,
                        "description": _cell(row, desc_col),
                        "fees": _cell(row, fees_col),
                        "admin_fee": _cell(row, admin_fee_col),
                    }
        return result
    except Exception as e:
        logger.debug(f"Could not read existing LLM scores: {e}")
        return {}


def restore_status_column(worksheet, status_map: dict) -> None:
    """After uploading new data restore non-'New' statuses matched by Link."""
    if not status_map:
        return
    try:
        import gspread.utils as _gu
        data = worksheet.get_all_values()
        if not data or len(data) < 2:
            return
        header = data[0]
        try:
            link_col = header.index("Link")
            status_col = header.index("Status")
        except ValueError:
            return
        updates = []
        for row_idx, row in enumerate(data[1:], start=2):
            if len(row) > link_col:
                old = status_map.get(row[link_col].strip())
                if old:
                    cell = _gu.rowcol_to_a1(row_idx, status_col + 1)
                    updates.append({"range": cell, "values": [[old]]})
        if updates:
            worksheet.batch_update(updates)
            logger.info(f"Restored {len(updates)} status values from previous run")
    except Exception as e:
        logger.warning(f"Could not restore Status column: {e}")




def col_index_to_letter(n: int) -> str:
    """Convert 1-based column index to spreadsheet column letter(s). 1→A, 26→Z, 27→AA."""
    result = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result


def get_image_formula(df: pd.DataFrame, last_row: int) -> str:
    """Build IMAGE formula using actual column position of Image_URL in the DataFrame.
    Data starts at sheet column B (offset +1 from 1-based index)."""
    if 'Image_URL' in df.columns:
        col_idx = df.columns.get_loc('Image_URL') + 2  # +1 for 1-based, +1 for col A offset
        col_letter = col_index_to_letter(col_idx)
        return f"=ARRAYFORMULA(IMAGE(${col_letter}2:${col_letter}{last_row}))"
    # Fallback: try to locate Photo_URL or Main_Image_URL
    for fallback_col in ('Photo_URL', 'Main_Image_URL'):
        if fallback_col in df.columns:
            col_idx = df.columns.get_loc(fallback_col) + 2
            col_letter = col_index_to_letter(col_idx)
            return f"=ARRAYFORMULA(IMAGE(${col_letter}2:${col_letter}{last_row}))"
    return ""


def reorder_dataframe_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Reorder DataFrame columns with priority columns first, then remaining columns alphabetically.
    Returns a new DataFrame with reordered columns.
    """
    priority_columns = [
        'Status', 'LLM_Score', 'LLM_Summary', 'LLM_Description',
        'Name', 'Base_value', 'Additional_value', 'Full_value', 'LLM_Fees', 'LLM_Admin_Fee',
        'Area', 'Address', 'Distance_km', 'Duration_min', 'Link',
        'Image_URL', 'Price_Detail', 'Area_Detail', 'Location', 'Description',
        'Seller_Info', 'Dishwasher', 'Geocoded_Address',
        # Custom extracted fields from Description
        'Deposit_PLN', 'Metro_station', 'Metro_distance_min', 'Available_From',
        'Occasional_Lease', 'Direct_From_Owner', 'Min_Period_Months', 'Balcony',
        'Air_Conditioning', 'Internet', 'Build_Year', 'Renovated',
        'Non_Smoking', 'No_Pets_Desc', 'Washing_Machine', 'Target_Tenant'
    ]
    existing_priority_columns = [col for col in priority_columns if col in df.columns]
    remaining_columns = sorted([col for col in df.columns if col not in existing_priority_columns])
    ordered_columns = existing_priority_columns + remaining_columns
    return df[ordered_columns]


# Display-only translation of our own column names for the Sheet header row.
# Internal code always keeps using the English keys below - this only changes
# what a "pl" profile sees as column headers in Google Sheets.
COLUMN_HEADERS_PL = {
    "Status": "Status", "LLM_Score": "Ocena AI", "LLM_Summary": "Podsumowanie AI", "LLM_Description": "Opis AI",
    "LLM_Fees": "Opłaty (AI)", "LLM_Admin_Fee": "Czynsz adm. (AI)",
    "Name": "Nazwa", "Base_value": "Cena bazowa",
    "Additional_value": "Opłaty dodatkowe", "Full_value": "Cena całkowita",
    "Area": "Powierzchnia", "Address": "Adres", "Distance_km": "Odległość (km)",
    "Duration_min": "Czas dojazdu (min)", "Link": "Link", "Image_URL": "Zdjęcie",
    "Price_Detail": "Cena (szczegóły)", "Price_Text": "Cena (tekst)",
    "Area_Detail": "Powierzchnia (szczegóły)", "Location": "Lokalizacja",
    "Description": "Opis", "Seller_Info": "Sprzedający", "Seller": "Sprzedający",
    "Seller_Name": "Nazwa sprzedającego", "Dishwasher": "Zmywarka",
    "Geocoded_Address": "Adres (geokodowany)", "Deposit_PLN": "Kaucja (zł)",
    "Metro_station": "Stacja metra", "Metro_distance_min": "Odległość do metra (min)",
    "Available_From": "Dostępne od", "Occasional_Lease": "Najem okazjonalny",
    "Direct_From_Owner": "Bezpośrednio", "Min_Period_Months": "Minimalny okres (miesiące)",
    "Balcony": "Balkon", "Air_Conditioning": "Klimatyzacja", "Internet": "Internet",
    "Build_Year": "Rok budowy", "Renovated": "Po remoncie", "Non_Smoking": "Niepalący",
    "No_Pets_Desc": "Bez zwierząt (opis)", "Washing_Machine": "Pralka",
    "Target_Tenant": "Dla kogo", "Room_count": "Liczba pokoi", "Floor": "Piętro",
    "City": "Miasto", "Region": "Region", "Scraping_Error": "Błąd scrapowania",
    "HTML_File_Path": "Ścieżka pliku HTML", "Main_Image_URL": "Główne zdjęcie (URL)",
    "Negotiable": "Do negocjacji", "Source": "Źródło", "Title": "Tytuł",
    "Cost_Details_Extracted": "Szczegóły kosztów", "Price_per_m2": "Cena za m²",
    "Phone_Number": "Numer telefonu", "Agent_Name": "Imię agenta",
    "Agency_Name": "Nazwa agencji", "Agency_URL": "Link do agencji",
    "Detail_Extraction_Status": "Status ekstrakcji szczegółów", "Photo_URL": "Zdjęcie",
    "Additional_Price_Text": "Opłaty dodatkowe (tekst)", "Category": "Kategoria",
    "Image_Count": "Liczba zdjęć", "Location_Breadcrumb": "Lokalizacja (okruszki)",
    "OLX_ID": "ID OLX", "Otodom_ID": "ID Otodom", "Posted_Date": "Data dodania",
    "Seller_LastSeen": "Sprzedający - ostatnio widziany",
    "Seller_Profile_URL": "Link do profilu sprzedającego", "Seller_Since": "Sprzedający od",
    "Views": "Wyświetlenia",
}


def translate_header_row(columns: list, language: str = "en") -> list:
    """Translate a list of our own English column names for display, e.g. in a
    Sheet header row. Columns we don't have a translation for (including ones
    mirroring real, already-Polish site labels) are passed through unchanged."""
    if language != "pl":
        return list(columns)
    return [COLUMN_HEADERS_PL.get(col, col) for col in columns]


_COLUMN_HEADERS_FROM_PL = {pl: en for en, pl in COLUMN_HEADERS_PL.items()}


def untranslate_header_row(columns: list) -> list:
    """Inverse of translate_header_row: map displayed (possibly Polish) headers back
    to our internal English column names. Two display labels are shared by two
    columns each ("Zdjęcie", "Sprzedający"); callers only look up unambiguous ones."""
    return [_COLUMN_HEADERS_FROM_PL.get(col, col) for col in columns]
