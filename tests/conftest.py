import os
from pathlib import Path

# apartment_scraper loads search profiles at import time. Point it at the bundled
# example so the suite runs on a fresh clone without a private profiles.json.
os.environ.setdefault(
    "APARTMENT_PROFILES_PATH",
    str(Path(__file__).resolve().parent.parent / "profiles.example.json"),
)
