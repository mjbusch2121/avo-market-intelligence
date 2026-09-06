# fetch_enso.py
# ---------------------------------------------------------------
# El Niño–Southern Oscillation (ENSO) state from NOAA CPC, framed
# as a seasonal-to-annual signal — a different horizon from the
# 2-4 week weather panel, so it lives in its own dashboard section.
#
# Two plain-text indices, no API key:
#   RONI  SEAS YR ANOM          (anomaly = column index 2)  <- LEAD
#   ONI   SEAS YR TOTAL ANOM    (anomaly = column index 3)  <- alongside
#
# We lead with RONI (Relative Oceanic Niño Index): CPC now quotes its
# official probabilities against the relative index because tropical-wide
# warming inflates the traditional ONI. ONI is shown for continuity.
#
# Niño 1+2 (the (0-10S)(90W-80W) eastern box off Ecuador/Peru) drives the
# Lambayeque coastal flood signal. It is NOT in the RONI/ONI files — it lives in
# CPC's weekly SST file (wksst9120.for, 1991-2020 base). That file is WEEKLY,
# not seasonal, so it carries its own cadence and staleness clock. The two coastal
# El Niños of 2017 and 2023 warmed Niño 1+2 while Niño 3.4 stayed near neutral, so
# RONI/ONI alone are blind to exactly the events that flood the northern coast.
#
# Output: data/raw/enso.json
# ---------------------------------------------------------------

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
RAW_DIR = ROOT / "data" / "raw"

RONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/RONI.ascii.txt"
ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"
# Weekly SST, 1991-2020 base period. Confirmed live 2026-09-06; the base-period
# suffix changes on rebaseline (was wksst8110), so if this 404s, re-list
# https://www.cpc.ncep.noaa.gov/data/indices/ for the current wksstNNNN.for.
WKSST_URL = "https://www.cpc.ncep.noaa.gov/data/indices/wksst9120.for"
HEADERS = {"User-Agent": "avo-market-intelligence/1.0 (github.com dashboard)"}

TREND_SEASONS = 36     # ~3 years of overlapping 3-month seasons for the chart
WEEKLY_TREND_WEEKS = 104   # ~2 years of weekly SST for the Niño 1+2 chart

# CPC operational bands, evaluated as "first threshold the anomaly meets".
# Anything below -2.0 falls through to "very strong La Niña".
ENSO_BANDS = [(2.0, "very strong El Niño"), (1.5, "strong El Niño"),
              (1.0, "moderate El Niño"), (0.5, "weak El Niño"),
              (-0.5, "neutral"), (-1.0, "weak La Niña"),
              (-1.5, "moderate La Niña"), (-2.0, "strong La Niña")]

# CPC requires five consecutive overlapping seasons past ±0.5 for an official
# event; below that it is only a "warm/cool anomaly".
EVENT_THRESHOLD = 0.5
EVENT_MIN_SEASONS = 5


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify(anom: float) -> str:
    for thresh, label in ENSO_BANDS:
        if anom >= thresh:
            return label
    return "very strong La Niña"


def trend(anoms: list) -> str:
    """Direction over the last three seasons, by magnitude so it reads the same
    for either phase: a deepening La Niña is 'strengthening', not 'falling'."""
    if len(anoms) < 4:
        return "steady"
    delta = abs(anoms[-1]) - abs(anoms[-4])
    if delta > 0.1:
        return "strengthening"
    if delta < -0.1:
        return "weakening"
    return "steady"


def consecutive_seasons(anoms: list) -> int:
    """How many consecutive most-recent seasons sit past ±0.5 in the current
    phase's direction. 0 if the latest season is neutral."""
    if not anoms:
        return 0
    latest = anoms[-1]
    if latest >= EVENT_THRESHOLD:
        keep = lambda v: v >= EVENT_THRESHOLD
    elif latest <= -EVENT_THRESHOLD:
        keep = lambda v: v <= -EVENT_THRESHOLD
    else:
        return 0
    count = 0
    for v in reversed(anoms):
        if keep(v):
            count += 1
        else:
            break
    return count


def parse_ascii(text: str, anom_idx: int) -> list:
    """Whitespace-delimited SEAS YR ... ANOM rows -> [{season, year, anom}].

    The header row and any stray lines are skipped by the int/float parse
    failing (SEAS/YR/ANOM are not numeric)."""
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) <= anom_idx:
            continue
        try:
            year = int(parts[1])
            anom = float(parts[anom_idx])
        except ValueError:
            continue  # header or blank/comment line
        rows.append({"season": parts[0], "year": year, "anom": anom})
    return rows


def build_index(text: str, anom_idx: int) -> dict:
    rows = parse_ascii(text, anom_idx)
    if not rows:
        raise ValueError("parsed zero rows — column layout may have changed")
    anoms = [r["anom"] for r in rows]
    latest = rows[-1]
    n_consec = consecutive_seasons(anoms)
    # Three seasons ago: powers "up from X" in the card and the steep-move
    # (lag) caveat. delta3 is signed so the dashboard can render direction.
    ref3 = rows[-4] if len(rows) >= 4 else None
    delta3 = round(latest["anom"] - ref3["anom"], 2) if ref3 else None
    return {
        "latest": {**latest, "phase": classify(latest["anom"])},
        "trend": trend(anoms),
        "three_seasons_ago": ref3,
        "delta3": delta3,
        "consecutive_seasons": n_consec,
        "established_event": n_consec >= EVENT_MIN_SEASONS,
        "series": rows[-TREND_SEASONS:],
    }


_WK_DATE = re.compile(r"^\s*(\d{2}[A-Z]{3}\d{4})")
_WK_NUM = re.compile(r"-?\d+\.\d")


def parse_weekly_sst(text: str) -> list:
    """Parse CPC's fixed-width weekly SST file into [{week, nino12_anom, nino34_anom}].

    Layout (four regions, each SST + anomaly):
        Week          SST SSTA     SST SSTA     SST SSTA     SST SSTA
        02SEP1981     20.6-0.1     24.8-0.1     26.5-0.2     28.3-0.3
        26AUG2026     25.0 4.2     28.3 3.4     29.4 2.6     29.6 1.0

    A negative anomaly is GLUED to its SST ("20.6-0.1"), so split() would merge
    the two into one token. Instead, findall on the number pattern splits
    "20.6-0.1" into ["20.6", "-0.1"], yielding exactly 8 numbers per data row:
    [SST12, ANOM12, SST3, ANOM3, SST34, ANOM34, SST4, ANOM4]. The header rows
    carry no such numbers and drop out (findall returns != 8). Columns confirmed
    2026-09-06: index 1 = Niño 1+2 anomaly, index 5 = Niño 3.4 anomaly.
    """
    rows = []
    for line in text.splitlines():
        dm = _WK_DATE.match(line)
        if not dm:
            continue
        nums = _WK_NUM.findall(line)
        if len(nums) != 8:
            continue
        try:
            week = datetime.strptime(dm.group(1), "%d%b%Y").date().isoformat()
        except ValueError:
            continue
        rows.append({"week": week,
                     "nino12_anom": float(nums[1]),
                     "nino34_anom": float(nums[5])})
    return rows


def build_weekly(text: str) -> dict:
    rows = parse_weekly_sst(text)
    if not rows:
        raise ValueError("parsed zero weekly SST rows — column layout may have changed")
    latest = rows[-1]
    # Divergence: Niño 1+2 minus Niño 3.4. A large positive value with 3.4 near
    # neutral is the coastal-El-Niño signature that RONI/ONI (both 3.4-based) miss.
    diverg = round(latest["nino12_anom"] - latest["nino34_anom"], 1)
    return {
        "latest": {**latest, "divergence": diverg},
        "series": rows[-WEEKLY_TREND_WEEKS:],
    }


def write_failure(out_path: Path, reason: str):
    """Record a failed fetch without faking freshness (see fetch_diesel.py)."""
    now = _now_iso()
    print(f"ENSO fetch failed: {reason}", file=sys.stderr)
    if out_path.exists():
        prev = json.loads(out_path.read_text(encoding="utf-8"))
        prev["fetch_attempted"] = now
        prev["fetch_error"] = reason
        out_path.write_text(json.dumps(prev, indent=1), encoding="utf-8")
        print("  kept previous enso.json, preserved fetched_at")
    else:
        out_path.write_text(json.dumps({
            "available": False, "fetch_attempted": now, "fetch_error": reason},
            indent=1), encoding="utf-8")
        print("  no prior enso.json to preserve")


def fetch(url: str) -> str:
    r = requests.get(url, timeout=60, headers=HEADERS)
    r.raise_for_status()
    return r.text


def main():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DIR / "enso.json"
    try:
        roni = build_index(fetch(RONI_URL), 2)
        oni = build_index(fetch(ONI_URL), 3)
    except Exception as e:
        # Non-fatal: exit 0 so the rest of the weekly pipeline still runs.
        # build_summary raises the alarm via the (45-day) staleness check.
        write_failure(out_path, f"could not fetch/parse ENSO indices: {e}")
        return

    out = {
        "fetched_at": _now_iso(),
        "primary": "roni",   # lead the panel with RONI, ONI alongside
        "roni": roni,
        "oni": oni,
        "sources": {"roni": RONI_URL, "oni": ONI_URL},
    }

    # Weekly Niño 1+2 SST — SEPARATE failure domain: a weekly-SST hiccup must not
    # discard the seasonal RONI/ONI we just fetched. On failure, carry forward the
    # prior weekly block (if any) so the panel keeps its last good reading.
    try:
        out["nino12"] = build_weekly(fetch(WKSST_URL))
        out["sources"]["nino12"] = WKSST_URL
    except Exception as e:
        reason = f"could not fetch/parse weekly SST (Niño 1+2): {e}"
        print(f"  weekly SST: {reason}", file=sys.stderr)
        out["nino12_error"] = reason
        if out_path.exists():
            prev = json.loads(out_path.read_text(encoding="utf-8"))
            if "nino12" in prev:
                out["nino12"] = prev["nino12"]
                out["nino12_stale"] = True
                print("  weekly SST: kept previous Niño 1+2 block")

    out_path.write_text(json.dumps(out, indent=1), encoding="utf-8")
    r = roni["latest"]
    print(f"ENSO: RONI {r['season']} {r['year']} {r['anom']:+.2f} "
          f"({r['phase']}), trend {roni['trend']}, "
          f"{roni['consecutive_seasons']} consecutive seasons"
          f"{' — established event' if roni['established_event'] else ''}")
    o = oni["latest"]
    print(f"      ONI  {o['season']} {o['year']} {o['anom']:+.2f} ({o['phase']})")
    n = out.get("nino12", {}).get("latest")
    if n:
        print(f"      Niño 1+2 {n['week']} {n['nino12_anom']:+.1f}°C "
              f"(3.4 {n['nino34_anom']:+.1f}, diverg {n['divergence']:+.1f})")


if __name__ == "__main__":
    main()
