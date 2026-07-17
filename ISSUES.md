# Known issues

Consolidated from a 4-agent review (data pipeline / API integration / pandas / tests) plus a
CodeRabbit pass. Findings that both reviews hit independently are marked ⚠️⚠️ — treat those
as high-confidence.

## Critical

- [x] `apartment_scraper.py:5604-5605` - the final Google Sheets write (`batch_clear` + `update`)
      has no try/except, and `run()` itself is called with no wrapper. A single Sheets API
      hiccup crashes the whole process and skips the Telegram/email notifications that come
      right after in the same function. **Fixed**: whole block wrapped in try/except,
      `existing_status` defaults safely so Telegram/email still run after a Sheets failure.
- [x] `apartment_scraper.py:1373` (`extract_custom_fields_from_opis`) - `"Minimalny okres najmu: 2
      lata"`, a very common real listing phrase, extracts nothing. All `Min_Period_Months`
      patterns require `minimum`/`min` immediately before the number, never the adjective form
      `minimalny`. **Fixed**: added `minimalny/minimalna/minimalne okres (najmu)` patterns; also
      added `tests/test_apartment_scraper.py::TestExtractCustomFieldsFromOpis` (this function had
      zero coverage before).
- [x] `email_report.py` has zero test coverage. This is the module that decides what actually
      gets emailed (`filter_quality_offers`, district matching, room-vs-apartment heuristic) - a
      regression here fails silently. **Fixed**: added `tests/test_email_report.py` (45 tests
      covering every filter predicate, `filter_quality_offers`, `add_price_per_m2`,
      `generate_email_html` in both languages, and `send_email_report`'s early-exit + SMTP paths
      with mocked `smtplib`).

## Important

- [ ] `email_report.py:296-303` - scraped `name`/`address`/`description` are interpolated
      unescaped into the HTML email template (HTML injection via listing content).
- [ ] `apartment_scraper.py:3122`, `logging_config.py:309-315` - phone numbers logged in
      plaintext.
- [ ] `apartment_scraper.py:2478-2483` ⚠️⚠️ - Otodom additional-fee fallback regex re-matches the
      same "zł" amount already used for the base price.
- [ ] `apartment_scraper.py:~5025-5031` (duplicated at `5195, 5259, 5339`) - `Full_value =
      Base_value` heuristic is wrong whenever a genuine additional fee equals the base rent, and
      the block is copy-pasted 4x.
- [ ] `apartment_scraper.py:1264-1353` - cost-extraction dedup doesn't catch the same fee
      genuinely restated twice in different parts of one description.
- [ ] `apartment_scraper.py:1948-1959` - `is_duplicate_offer`'s price+location fallback drops
      legitimate 3rd+ listings sharing a price and district keyword.
- [ ] `apartment_scraper.py:2486-2520, 2945-2965` - `Area` silently defaults to `0.0` on total
      extraction failure, no flag, corrupts downstream price/m² math.
- [ ] `apartment_scraper.py:3673-3691` - placeholder/failure text (e.g. "No Description") can be
      recorded as if it were a successful extraction.
- [ ] `apartment_scraper.py:4680-4693` - Otodom pagination loop checks the "next" button exists,
      not whether it's disabled.
- [ ] `apartment_scraper.py:481-491` - `Distance_km`/`Duration_min`/`Geocoded_Address` get reset
      before office geocoding is confirmed to succeed, wiping prior values on transient failure.
- [ ] Credential loading duplicated across 5 call sites (`570, 1606, 2139, 2207, 5582`) - only
      `_open_spreadsheet` checks the file exists first.
- [ ] Gmail App Password sits in plaintext in the `config` sheet tab, shared as "writer" with
      every digest recipient (`create_sheet.py:107-113`).
- [ ] No retry/backoff anywhere for the Google Sheets API, which has real per-minute quotas.
- [ ] `migrate_config_sheets.py` runs its Sheets-writing loop at import time, no
      `if __name__ == "__main__":` guard.
- [ ] `setup.py`'s `check_playwright()` trusts `playwright install --dry-run`'s exit code, which
      doesn't reliably confirm chromium is installed.
- [ ] `scheduler.py` has zero tests despite real, deterministic date/schedule logic
      (`_expand_days`, catch-up math).
- [ ] `translate_header_row`/`COLUMN_HEADERS_PL`, `merge_portal_columns`,
      `reorder_dataframe_columns` are untested, and `COLUMN_HEADERS_PL` has already drifted from
      `priority_columns` (e.g. `Price_Text`, `Room_count`, `Floor` are in one list but not the
      other).
- [ ] ⚠️⚠️ Two tests pass for the wrong reason: `test_create_key_removes_common_words` (the
      stoplist only filters literal `"wynajem"`, not the inflected `"wynajmu"` in its own test
      input) and `test_create_key_limits_words` (asserts on `key.split("_")`, but words are
      joined with spaces).
- [ ] `_sanitize_address` drops real location data on dash-separated addresses without an
      "Odświeżono" suffix, e.g. `"Warszawa - Wola - Odświeżono dzisiaj"` → `"Warszawa"`.
- [ ] ⚠️⚠️ `seen_offers` global-state leak - `test_reset_clears_duplicates` doesn't reset the
      global back to a clean state on teardown.

## Nice-to-have

- [ ] `pd.concat` in a loop in both main scrape-collection loops (`2565, 3004`) - real O(n²),
      harmless at current row counts, cheap fix if touched again.
- [ ] `merge_portal_columns` manual per-cell coalesce - idiom nit, negligible perf impact.
- [ ] Dead code: `batch_update_sheets`/`process_batch_with_updates`, never called.
- [ ] Dead code: `improved_mixed_extraction` (`apartment_scraper.py:1738`) calls itself
      recursively and always returns an empty list - but has zero callers anywhere, so it's
      inert. Should just be deleted.
- [ ] `geocode_photon` sends no `User-Agent` (the Nominatim path does).
- [ ] OAuth token file (`create_sheet.py`) written without restricted file permissions.
- [ ] Status restore matches by exact `Link` string, no URL normalization - a tracking-param
      change silently resets "seen" status to "New".
- [ ] Pickle auto-discovery glob (`apartments*.pkl`) is ambiguous across 4 file families, no
      schema validation on load.
- [ ] `validate_offer_data` only logs suspicious offers, never filters them.
- [ ] `update_config_last_scraped` uses 2 `update_cell` calls instead of one batched write.
- [ ] Telegram notification doesn't handle 429 `retry_after`.
- [ ] Nominatim User-Agent lacks contact info.
- [ ] No retry on transient geocoding/ORS failures.
- [ ] Overly broad Drive scope (full `drive` vs `drive.file`) in both `create_sheet.py` and
      `_open_spreadsheet`.
- [ ] `apartment_scraper.py:5642-5643` - "new offers" count can go negative.
- [ ] `apartment_scraper.py:913-925` - image formula range off-by-one for a single-offer sheet.
- [ ] `apartment_scraper.py:5549-5552` - the dishwasher description-only branch computes a mask
      but never assigns it to the column.
- [ ] `email_report.py:390-391` - `smtp_port` parsing isn't wrapped in try/except like `top_n` is.
- [ ] Relative-date tests (`"today"`/`"yesterday"`) aren't frozen to a fixed clock, could flake
      right at midnight.
- [ ] A few `validate_offer_data` tests check the warning message but never assert `is_valid is
      True`.
- [ ] `extract_full_cost_from_description`'s "total ≤ base price" branch is untested.
- [ ] Several regex branches never exercised (Heating, Hot water, `plus:`, `depozyt`/
      `zabezpieczenie` deposit synonyms).
- [ ] `create_offer_key`/`is_duplicate_offer` have no defensive `None` handling (currently safe
      because both callers guard against it).
