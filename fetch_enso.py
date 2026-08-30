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
# Niño 1+2 (needed for the Lambayeque flood signal) is NOT in either file
# and its weekly-SST filename changes when CPC rebaselines, so it is
# deferred to v2 per the spec — see _nino12_hook() below.
#
# Output: data/raw/enso.json
# ---------------------------------------------------------------

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
RAW_DIR = ROOT / "data" / "raw"

RONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/RONI.ascii.txt"
ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"
HEADERS = {"User-Agent": "avo-market-intelligence/1.0 (github.com dashboard)"}

TREND_SEASONS = 36   # ~3 years of overlapping 3-month seasons for the chart

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


def _nino12_hook():
    """Deferred: Niño 1+2 weekly SST for the Lambayeque flood signal. The file
    lives under https://www.cpc.ncep.noaa.gov/data/indices/ but the base-period
    suffix (e.g. wksst8110.for) changes on rebaseline, so locate it from the
    directory listing rather than hardcoding. RONI + ONI are sufficient for v1;
    this can be added without restructuring enso.json."""
    return None


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
    out_path.write_text(json.dumps(out, indent=1), encoding="utf-8")
    r = roni["latest"]
    print(f"ENSO: RONI {r['season']} {r['year']} {r['anom']:+.2f} "
          f"({r['phase']}), trend {roni['trend']}, "
          f"{roni['consecutive_seasons']} consecutive seasons"
          f"{' — established event' if roni['established_event'] else ''}")
    o = oni["latest"]
    print(f"      ONI  {o['season']} {o['year']} {o['anom']:+.2f} ({o['phase']})")


if __name__ == "__main__":
    main()
