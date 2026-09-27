"""Evaluate LLM fee extraction against the fee field scraped from the listing form.

Runs the real scoring prompt on a stratified sample of previously scraped listings
(pickles written by apartment_scraper.py) and reports:

- agreement: listings whose form has an extra-fee value - does the LLM find the same
  administrative fee in the description?
- recovery: listings with an empty fee field - how often does the LLM find the fee
  (or "included") in the description?
- optional extras: listings mentioning a priced parking spot - is it kept out of the
  administrative fee?

Usage:
    export GROQ_API_KEY=...            # or ANTHROPIC_API_KEY
    python scripts/eval_fee_extraction.py apartments_*.pkl --per-group 20
"""
import argparse
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm_scorer  # noqa: E402

PREFERENCES = "2+ rooms, max 30 min commute, quiet area"
# "W cenie" alone is usually about parking or internet - require it next to a fee word
INCLUDED_RE = (r"(?:czynsz\w*|opłat\w*|media|rachunki)[^.]{0,30}(?:w cenie|wliczon)"
               r"|all inclusive|ze wszystkimi opłatami")
PARKING_RE = r"(?:parking|garaż|postojow)\w*[^.]{0,40}?\d+\s*(?:zł|pln)"


def load_listings(paths):
    frames = []
    for path in paths:
        df = pd.read_pickle(path)
        if "Description" not in df.columns and "Opis" in df.columns:
            df["Description"] = df["Opis"]
        frames.append(df)
    df = pd.concat(frames, ignore_index=True).drop_duplicates("Link")
    df = df[df["Description"].astype(str).str.len() > 50].copy()
    df["base"] = pd.to_numeric(df["Base_value"], errors="coerce").fillna(0)
    df["form_fee"] = pd.to_numeric(df["Additional_value"], errors="coerce").fillna(0)
    # Form fee equal to the rent is the "rent repeated in the fee field" case - not a fee
    df.loc[df["form_fee"] == df["base"], "form_fee"] = 0
    return df


def sample_groups(df, per_group, seed):
    desc = df["Description"].astype(str).str.lower()
    groups = {
        "form_fee_present": df[(df["form_fee"] > 0) & ~desc.str.contains(PARKING_RE)],
        "form_fee_empty": df[(df["form_fee"] == 0) & ~desc.str.contains(INCLUDED_RE)],
        "says_included": df[(df["form_fee"] == 0) & desc.str.contains(INCLUDED_RE)],
        "priced_parking": df[desc.str.contains(PARKING_RE)],
    }
    return {name: g.sample(min(per_group, len(g)), random_state=seed) for name, g in groups.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pickles", nargs="+")
    parser.add_argument("--per-group", type=int, default=20)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--groq-model", default=None)
    parser.add_argument("--out", default="fee_eval_results.csv", help="per-listing results (keep private)")
    args = parser.parse_args()

    backend = llm_scorer._resolve_backend(args.groq_model)
    if backend is None:
        sys.exit("Set GROQ_API_KEY or ANTHROPIC_API_KEY")

    df = load_listings(args.pickles)
    print(f"{len(df)} unique listings with a description")
    rows = []
    for group, sample in sample_groups(df, args.per_group, args.seed).items():
        for _, listing in sample.iterrows():
            result = llm_scorer.score_listing(listing.to_dict(), PREFERENCES, backend)
            rows.append({
                "group": group,
                "link": listing["Link"],
                "base": listing["base"],
                "form_fee": listing["form_fee"],
                "failed": result["score"] == "",
                "fees_status": result["fees_status"],
                "admin_fee": result["admin_fee"],
                "utilities": result["utilities_estimate"],
                "extras": "; ".join(f"{e['item']} {e['pln']:.0f}" for e in result["optional_extras"]),
                # Admin fee equal to a parking price that is not the form's fee value
                "parking_in_admin_fee": bool(
                    result["admin_fee"]
                    and abs(result["admin_fee"] - listing["form_fee"]) > 1
                    and any(abs(result["admin_fee"] - float(m)) < 1 for m in re.findall(
                        r"(?:parking|garaż|postojow)\w*[^.]{0,40}?(\d+)\s*(?:zł|pln)",
                        str(listing["Description"]).lower()))
                ),
            })
            print(f"  {group:17} status={result['fees_status'] or 'FAILED':8} admin={result['admin_fee']} "
                  f"form={listing['form_fee']:.0f}", flush=True)

    res = pd.DataFrame(rows)
    res.to_csv(args.out, index=False)
    ok = res[~res["failed"]]
    print(f"\nModel: {backend.get('model', backend['type'])} | listings: {len(res)} | failed calls: {res['failed'].sum()}")

    present = ok[ok["group"] == "form_fee_present"]
    same = (present["admin_fee"].notna() & (abs(present["admin_fee"] - present["form_fee"]) <= 1)).sum()
    print(f"form fee present ({len(present)}): LLM found the same admin fee in {same} "
          f"({same / max(len(present), 1):.0%}); status=extra in {(present['fees_status'] == 'extra').sum()}")

    empty = ok[ok["group"] == "form_fee_empty"]
    found = empty["admin_fee"].notna().sum()
    print(f"form fee empty ({len(empty)}): admin fee found in {found}, "
          f"'included' in {(empty['fees_status'] == 'included').sum()}, "
          f"'unknown' in {(empty['fees_status'] == 'unknown').sum()}")

    included = ok[ok["group"] == "says_included"]
    print(f"description says fees included ({len(included)}): status=included in "
          f"{(included['fees_status'] == 'included').sum()}")

    parking = ok[ok["group"] == "priced_parking"]
    print(f"priced parking in description ({len(parking)}): parking kept out of admin fee in "
          f"{(~parking['parking_in_admin_fee']).sum()}, listed as optional extra in "
          f"{parking['extras'].str.contains('park|garaż|garage|postoj', case=False).sum()}")
    print(f"\nPer-listing results: {args.out}")


if __name__ == "__main__":
    main()
