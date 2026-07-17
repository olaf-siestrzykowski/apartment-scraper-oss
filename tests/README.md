# Tests

Unit tests for the parsing/extraction helpers in `apartment_scraper.py`: price parsing,
address cleaning, Polish date parsing, duplicate detection, cost extraction, and
offer/link validation.

## Running

```bash
pip install -r requirements.txt
pytest tests/ -v
```

Run a single class or test:

```bash
pytest tests/test_apartment_scraper.py::TestParsePriceToFloat -v
pytest tests/test_apartment_scraper.py::TestParsePriceToFloat::test_parse_price_standard_polish_format -v
```

## Notes

- Test inputs are real Polish listing text (prices in zł, addresses, descriptions) —
  that's what the functions under test actually parse, so it stays untranslated.
- `markers` in `pytest.ini` define `unit` / `integration` / `slow`, but no tests are
  currently tagged with them.
