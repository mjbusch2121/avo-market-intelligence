# fetch_usda_pricing.py
# ---------------------------------------------------------------
# Pulls avocado pricing and volume (movement) data from the
# USDA MARS API (MyMarketNews v3.1).
#
# Two datasets, each with a committed history cache so the weekly
# GitHub Actions run only fetches the recent window:
#
#   history/movement_weekly.json  - weekly shipment volume (lbs)
#                                   by district/origin, ~3.5 years
#   history/pricing_weekly.json   - weekly Hass FOB price summary
#                                   by district/size, ~3.5 years
#
# Current-week detail (full size/grade breakdown + market tone)
# goes to data/raw/usda_current.json for build_summary.py.
# ---------------------------------------------------------------

import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # GitHub Actions injects env vars directly

BASE = "https://marsapi.ams.usda.gov/services/v3.1/marketTypes"
API_KEY = os.getenv("MARS_API_KEY")

ROOT = Path(__file__).parent
HISTORY_DIR = ROOT / "history"
RAW_DIR = ROOT / "data" / "raw"

# How far back the one-time historical seed reaches (3 full prior
# seasons + current, for the seasonal band).
HISTORY_START = "07/01/2022"
# Incremental window: wide enough to absorb USDA revisions of the
# last few published weeks.
INCREMENTAL_DAYS = 45

# Pricing districts we track (MARS district values, case sensitive).
PRICE_DISTRICTS = {
    "MEXICO CROSSINGS THROUGH TEXAS",
    "SOUTH DISTRICT CALIFORNIA",
}


def mars_get(path: str, q: str, retries: int = 3) -> list:
    """GET a MARS v3.1 endpoint, returning the results list."""
    for attempt in range(retries):
        try:
            r = requests.get(f"{BASE}/{path}", params={"q": q},
                             auth=(API_KEY, ""), timeout=180)
            r.raise_for_status()
            return r.json().get("results", [])
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"  retry {attempt + 1} after {type(e).__name__}: {e}")
            time.sleep(5 * (attempt + 1))
    return []


def mmdd(d: date) -> str:
    return d.strftime("%m/%d/%Y")


def to_iso(us_date: str) -> str:
    """'06/27/2026' -> '2026-06-27' (sortable, JS-friendly)."""
    m, d, y = us_date.split("/")
    return f"{y}-{m}-{d}"


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1), encoding="utf-8")


# ---------------------------------------------------------------
# Movement (volume)
# ---------------------------------------------------------------

def aggregate_movement(rows: list) -> dict:
    """Collapse raw movement rows to lbs keyed by week/district/origin.

    Key: 'week_end|district|origin' -> summed 1_lb_units across
    organic/conventional and package variants.
    """
    agg = {}
    for row in rows:
        lbs = row.get("1_lb_units")
        if not lbs:
            continue
        key = f"{to_iso(row['end_date'])}|{row['district']}|{row.get('origin') or 'Unknown'}"
        agg[key] = agg.get(key, 0) + int(lbs)
    return agg


def fetch_movement():
    hist_path = HISTORY_DIR / "movement_weekly.json"
    history = load_json(hist_path, {})

    if not history:
        print("Movement: seeding full history (one-time pull)...")
        rows = mars_get("sc-cr/sc/movement/weekly",
                        f"commodity=Avocados;shipment_date={HISTORY_START}:{mmdd(date.today())}")
    else:
        start = mmdd(date.today() - timedelta(days=INCREMENTAL_DAYS))
        print(f"Movement: incremental pull since {start}...")
        rows = mars_get("sc-cr/sc/movement/weekly",
                        f"commodity=Avocados;shipment_date={start}:{mmdd(date.today())}")

    print(f"  {len(rows)} raw rows")
    fresh = aggregate_movement(rows)

    # Fresh data wins: USDA revises recent weeks after first publication.
    history.update(fresh)
    save_json(hist_path, history)
    print(f"  history now {len(history)} week/district/origin entries")


# ---------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------

def slim_price_row(row: dict) -> dict:
    return {
        "week_end": to_iso(row.get("end_date") or row["report_date"]),
        "district": row["district"],
        "variety": row.get("variety"),
        "package": row.get("package"),
        "size": row.get("item_size"),
        "organic": row.get("organic"),
        "low": row.get("low_price"),
        "high": row.get("high_price"),
        "mostly_low": row.get("mostly_low_price"),
        "mostly_high": row.get("mostly_high_price"),
    }


def price_key(s: dict) -> str:
    return "|".join(str(s[k]) for k in
                    ("week_end", "district", "variety", "package", "size", "organic"))


def fetch_pricing_history():
    hist_path = HISTORY_DIR / "pricing_weekly.json"
    history = load_json(hist_path, {})

    if not history:
        print("Pricing: seeding full weekly history (one-time pull)...")
        start = HISTORY_START
    else:
        start = mmdd(date.today() - timedelta(days=INCREMENTAL_DAYS))
        print(f"Pricing: incremental weekly pull since {start}...")

    rows = mars_get("sc-cr/sc/shippingpt/weekly",
                    f"commodity=Avocados;report_date={start}:{mmdd(date.today())}")
    print(f"  {len(rows)} raw weekly rows")

    kept = 0
    for row in rows:
        if row["district"] not in PRICE_DISTRICTS:
            continue
        slim = slim_price_row(row)
        if slim["mostly_low"] is None and slim["low"] is None:
            continue
        history[price_key(slim)] = slim
        kept += 1
    save_json(hist_path, history)
    print(f"  kept {kept}; history now {len(history)} entries")


def fetch_pricing_current():
    """Latest daily shipping-point report: full size breakdown + tone."""
    start = mmdd(date.today() - timedelta(days=12))
    rows = mars_get("sc-cr/sc/shippingpt/daily",
                    f"group=Fruits;commodity=Avocados;report_date={start}:{mmdd(date.today())}")
    if not rows:
        print("Pricing current: no daily rows in window")
        save_json(RAW_DIR / "usda_current.json", {"report_date": None, "rows": []})
        return

    latest = max(rows, key=lambda r: to_iso(r["report_date"]))["report_date"]
    current = [r for r in rows if r["report_date"] == latest
               and r["district"] in PRICE_DISTRICTS]
    prior_dates = sorted({to_iso(r["report_date"]) for r in rows
                          if r["report_date"] != latest}, reverse=True)

    save_json(RAW_DIR / "usda_current.json", {
        "report_date": to_iso(latest),
        "prior_report_dates": prior_dates[:5],
        "rows": [{
            "district": r["district"],
            "variety": r.get("variety"),
            "package": r.get("package"),
            "size": r.get("item_size"),
            "organic": r.get("organic"),
            "low": r.get("low_price"),
            "high": r.get("high_price"),
            "mostly_low": r.get("mostly_low_price"),
            "mostly_high": r.get("mostly_high_price"),
            "market_tone": r.get("market_tone_comments"),
            "supply_tone": r.get("supply_tone_comments"),
            "demand_tone": r.get("demand_tone_comments"),
        } for r in current],
        # keep prior-day rows too so build_summary can compute day/week deltas
        "recent_rows": [{
            "report_date": to_iso(r["report_date"]),
            "district": r["district"],
            "variety": r.get("variety"),
            "package": r.get("package"),
            "size": r.get("item_size"),
            "organic": r.get("organic"),
            "mostly_low": r.get("mostly_low_price"),
            "mostly_high": r.get("mostly_high_price"),
        } for r in rows if r["district"] in PRICE_DISTRICTS],
    })
    print(f"Pricing current: {len(current)} rows for {latest}")


def main():
    if not API_KEY:
        print("FATAL: MARS_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    fetch_movement()
    fetch_pricing_history()
    fetch_pricing_current()
    print("fetch_usda_pricing: done")


if __name__ == "__main__":
    main()
