"""test_llm_scoring.py - Test LLM scoring against mock apartment listings.

Usage:
  export GROQ_API_KEY=your-key    # free at console.groq.com
  python test_llm_scoring.py
"""
import json
import logging
import os

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

PREFERENCES = (
    "Bright apartment, modern kitchen with dishwasher, close to metro or tram, "
    "max 30 min commute, balcony a plus, not ground floor, quiet street, good condition"
)

MOCK_LISTINGS = [
    {
        "Name": "Nowoczesne 2-pokojowe mieszkanie, Mokotów, balkon, metro Wilanowska",
        "Link": "https://www.otodom.pl/test/1",
        "Address": "Warszawa, Mokotów",
        "Area": "48",
        "Full_value": "3200",
        "Distance_km": "4.2",
        "Duration_min": "22",
        "Dishwasher": "Yes",
        "Description": (
            "Jasne mieszkanie na 4. piętrze w nowym budownictwie. "
            "Balkon z widokiem na park. W pełni wyposażona kuchnia ze zmywarką. "
            "Cisza i spokój, blisko metro Wilanowska."
        ),
    },
    {
        "Name": "Tanie mieszkanie centrum, parter, do remontu",
        "Link": "https://www.olx.pl/test/2",
        "Address": "Warszawa, Śródmieście",
        "Area": "38",
        "Full_value": "1900",
        "Distance_km": "1.1",
        "Duration_min": "8",
        "Dishwasher": "No",
        "Description": (
            "Mieszkanie na parterze, wymaga odświeżenia. "
            "Stara kuchnia bez zmywarki. Ruchliwa ulica, hałas od ulicy. "
            "Blisko centrum, ale budynek z lat 70."
        ),
    },
    {
        "Name": "Przestronne 3-pokoje, Żoliborz, pełne wyposażenie, ogródek",
        "Link": "https://www.otodom.pl/test/3",
        "Address": "Warszawa, Żoliborz",
        "Area": "72",
        "Full_value": "4500",
        "Distance_km": "6.8",
        "Duration_min": "35",
        "Dishwasher": "Yes",
        "Description": (
            "Duże mieszkanie na 2. piętrze z ogródkiem prywatnym. "
            "Pełne wyposażenie AGD, zmywarka, piekarnik. Spokojna dzielnica, "
            "ale dalej od centrum. Dojazd tramwajem 35 minut."
        ),
    },
    {
        "Name": "Kawalerka, wysoki parter, Wola, blisko metra",
        "Link": "https://www.olx.pl/test/4",
        "Address": "Warszawa, Wola",
        "Area": "28",
        "Full_value": "2400",
        "Distance_km": "2.5",
        "Duration_min": "15",
        "Dishwasher": "No",
        "Description": (
            "Mała kawalerka na wysokim parterze. Brak zmywarki i balkonu. "
            "Nowe wykończenie, dobry stan. 5 minut pieszo do stacji metra Płocka."
        ),
    },
    {
        "Name": "Luksusowy apartament, 10. piętro, panorama, Wilanów",
        "Link": "https://www.otodom.pl/test/5",
        "Address": "Warszawa, Wilanów",
        "Area": "90",
        "Full_value": "7500",
        "Distance_km": "18.0",
        "Duration_min": "55",
        "Dishwasher": "Yes",
        "Description": (
            "Apartament premium na 10. piętrze z panoramą Warszawy. "
            "W pełni wyposażony, zmywarka, taras 20m². Prestiżowa lokalizacja, "
            "ale daleko od centrum — dojazd samochodem ok. 55 minut."
        ),
    },
]


def main():
    groq_key = os.environ.get("GROQ_API_KEY", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if not groq_key and not anthropic_key:
        print("ERROR: Set GROQ_API_KEY (free at console.groq.com) or ANTHROPIC_API_KEY")
        return

    from llm_scorer import score_listings_df

    df = pd.DataFrame(MOCK_LISTINGS)
    print(f"\nScoring {len(df)} mock listings...\n")
    print(f"Preferences: {PREFERENCES}\n")
    print("-" * 70)

    result = score_listings_df(df, PREFERENCES)

    result_sorted = result.copy()
    result_sorted["_score_num"] = pd.to_numeric(result_sorted["LLM_Score"], errors="coerce")
    result_sorted = result_sorted.sort_values("_score_num", ascending=False)

    print(f"\n{'RANK':<5} {'SCORE':<7} {'TITLE':<45} SUMMARY")
    print("-" * 110)
    for rank, (_, row) in enumerate(result_sorted.iterrows(), 1):
        score = row.get("LLM_Score", "?")
        name = str(row.get("Name", ""))[:44]
        summary = str(row.get("LLM_Summary", ""))[:60]
        print(f"#{rank:<4} {score:<7} {name:<45} {summary}")

    print("\n--- Raw JSON output ---")
    for _, row in result_sorted.iterrows():
        print(json.dumps({
            "name": row.get("Name"),
            "score": row.get("LLM_Score"),
            "summary": row.get("LLM_Summary"),
            "description": row.get("LLM_Description"),
        }, ensure_ascii=False))

    # Quick email HTML preview
    from email_report import generate_email_html, add_price_per_m2
    result["Full_value"] = pd.to_numeric(result["Full_value"], errors="coerce")
    result["Area"] = pd.to_numeric(result["Area"], errors="coerce")
    ranked = add_price_per_m2(result)
    html = generate_email_html(ranked, "llm-test", "", "18.07.2026", "en")
    with open("llm_test_email_preview.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("\n✅ Email preview saved to: llm_test_email_preview.html")


if __name__ == "__main__":
    main()
