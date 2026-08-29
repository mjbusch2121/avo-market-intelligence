# fetch_diesel.py
# ---------------------------------------------------------------
# Pulls weekly retail on-highway No. 2 diesel prices from the
# EIA v2 API — national plus the three PADD regions that matter
# for avocado freight lanes:
#
#   West Coast (R50)  -> LA / Oxnard lanes
#   Gulf Coast (R30)  -> Texas crossings / Dallas lanes
#   East Coast (R10)  -> Philadelphia / Baltimore / Miami lanes
#
# Output: data/raw/diesel.json
# ---------------------------------------------------------------

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_KEY = os.getenv("EIA_API_KEY")
ROOT = Path(__file__).parent
RAW_DIR = ROOT / "data" / "raw"

SERIES = {
    "national": "EMD_EPD2D_PTE_NUS_DPG",
    "west_coast": "EMD_EPD2D_PTE_R50_DPG",
    "gulf_coast": "EMD_EPD2D_PTE_R30_DPG",
    "east_coast": "EMD_EPD2D_PTE_R10_DPG",
}
WEEKS = 26  # ~6 months of trend for the supporting chart


def fetch_series(series_id: str) -> list:
    r = requests.get("https://api.eia.gov/v2/petroleum/pri/gnd/data/", params={
        "api_key": API_KEY,
        "frequency": "weekly",
        "data[0]": "value",
        "facets[series][]": series_id,
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": WEEKS,
    }, timeout=60)
    r.raise_for_status()
    rows = r.json()["response"]["data"]
    # oldest -> newest for charting
    return sorted(
        ({"period": row["period"], "value": float(row["value"])} for row in rows),
        key=lambda x: x["period"])


def write_failure(out_path: Path, reason: str):
    """Record a failed fetch without faking freshness.

    `fetched_at` means "we got data". On failure we must NOT stamp a fresh one,
    or the staleness detector never trips. Instead we preserve whatever
    `fetched_at` the prior file carried (so staleness is measured from the last
    real success) and add `fetch_attempted` + `fetch_error`. With no prior file
    there is no `fetched_at` at all, so build_summary treats the feed as stale.
    Mirrors fetch_freight_pdf.py.
    """
    now = _now_iso()
    if out_path.exists():
        prev = json.loads(out_path.read_text(encoding="utf-8"))
        prev["fetch_attempted"] = now
        prev["fetch_error"] = reason
        out_path.write_text(json.dumps(prev, indent=1), encoding="utf-8")
        print(f"Diesel fetch failed ({reason}) — kept previous data, "
              "preserved its fetched_at for staleness")
    else:
        out_path.write_text(json.dumps({
            "available": False, "reason": reason,
            "fetch_attempted": now, "fetch_error": reason}, indent=1),
            encoding="utf-8")
        print(f"Diesel fetch failed ({reason}) — no prior data to preserve")


def main():
    out_path = RAW_DIR / "diesel.json"
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    if not API_KEY:
        print("WARNING: EIA_API_KEY not set — diesel panel will show as unavailable")
        write_failure(out_path, "EIA_API_KEY not configured")
        return

    out = {"available": True, "series": {}}
    try:
        for name, sid in SERIES.items():
            pts = fetch_series(sid)
            out["series"][name] = pts
            print(f"Diesel {name}: {len(pts)} weeks, latest "
                  f"{pts[-1]['period']} = ${pts[-1]['value']:.3f}/gal")
    except Exception as e:
        print(f"ERROR fetching EIA data: {e}", file=sys.stderr)
        write_failure(out_path, str(e))
        return

    out["fetched_at"] = _now_iso()
    out_path.write_text(json.dumps(out, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
