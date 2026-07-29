# seasonality.py
# ---------------------------------------------------------------
# Season-awareness for the dashboard. Reads seasons.json (the single
# source of truth for regional season windows) and classifies each
# region into one of three states:
#
#   "active"          - data is current, render normally
#   "out_of_season"   - no fresh data, AND the calendar says that's
#                       expected -> render a quiet "season concluded"
#                       card, suppress charts/comparisons
#   "unexpected_gap"  - no fresh data, but the calendar says there
#                       SHOULD be -> render a warning; likely a USDA
#                       slug change or a broken parser. Investigate.
#
# Used by build_summary.py (which stamps a `season` block onto each
# region in data.json) and, indirectly, by dashboard.js (which reads
# those stamps to pick a card style).
#
# Design rule: this module never fetches anything. It only interprets
# dates against the calendar, so it's trivially testable:
#     python seasonality.py        <- runs self-checks
# ---------------------------------------------------------------

import json
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
SEASONS_PATH = ROOT / "seasons.json"

# How stale (in days) current data may be before we treat the region
# as "not reporting". USDA movement is weekly, so 14 days tolerates
# one late posting without crying wolf.
STALE_AFTER_DAYS = 14


# ----------------------------------------------------------------
# Config loading
# ----------------------------------------------------------------

def load_seasons(path: Path = SEASONS_PATH) -> dict:
    """Load seasons.json and index regions by key."""
    cfg = json.loads(path.read_text(encoding="utf-8"))
    return {r["key"]: r for r in cfg["regions"]}


def _md_to_date(md: str, year: int) -> date:
    """'03-15' + 2026 -> date(2026, 3, 15)."""
    month, day = (int(x) for x in md.split("-"))
    return date(year, month, day)


# ----------------------------------------------------------------
# Core calendar logic
# ----------------------------------------------------------------

def in_window(region: dict, on: date) -> bool:
    """Is `on` inside the region's season window? Handles windows
    that wrap the new year (e.g. start 11-01, end 02-28)."""
    start = _md_to_date(region["window"]["start"], on.year)
    end = _md_to_date(region["window"]["end"], on.year)
    if start <= end:
        return start <= on <= end
    # Wrapping window (e.g. Nov -> Feb): inside if after start OR before end
    return on >= start or on <= end


def in_window_with_grace(region: dict, on: date) -> bool:
    """Same as in_window, but pads both edges by grace_weeks. Used to
    decide whether a data gap is 'expected' - USDA reporting often
    trails the field at season edges."""
    grace = timedelta(weeks=region.get("grace_weeks", 0))
    start = _md_to_date(region["window"]["start"], on.year) - grace
    end = _md_to_date(region["window"]["end"], on.year) + grace
    if start <= end:
        return start <= on <= end
    return on >= start or on <= end


def classify(region_key: str, last_reported: str | None,
             today: date | None = None,
             seasons: dict | None = None) -> dict:
    """The three-state classifier.

    Args:
        region_key:    key matching seasons.json ("mx", "ca", ...)
        last_reported: ISO date of the region's freshest data row,
                       or None if the feed returned nothing at all.
        today:         override for testing; defaults to date.today().
        seasons:       pre-loaded config; defaults to loading from disk.

    Returns a dict ready to embed in data.json, e.g.:
        {"status": "out_of_season",
         "message": "California (South District) season concluded — resumes ~spring",
         "last_reported": "2026-09-28"}
    """
    today = today or date.today()
    seasons = seasons or load_seasons()
    region = seasons.get(region_key)

    if region is None:
        # Unknown region: never silently pass. Treat as a config bug.
        return {"status": "unexpected_gap",
                "message": f"Region '{region_key}' missing from seasons.json — add it.",
                "last_reported": last_reported}

    fresh = (last_reported is not None and
             (today - date.fromisoformat(last_reported)).days <= STALE_AFTER_DAYS)

    if fresh:
        return {"status": "active", "message": None,
                "last_reported": last_reported}

    # No fresh data. Expected, or a problem?
    if in_window_with_grace(region, today):
        return {"status": "unexpected_gap",
                "message": (f"{region['name']} should be reporting but isn't "
                            f"(last data: {last_reported or 'never'}). "
                            "Check the USDA report slug and the fetcher."),
                "last_reported": last_reported}

    hint = region.get("resume_hint")
    msg = f"{region['name']} season concluded"
    if hint:
        msg += f" — {hint}"
    return {"status": "out_of_season", "message": msg,
            "last_reported": last_reported}


def any_unexpected_gaps(season_blocks: dict) -> list[str]:
    """Given {region_key: classify(...) result}, return keys with
    unexpected gaps. build_summary.py uses this to fail the pipeline
    loudly (exit code 1) so the GitHub Action goes red instead of
    committing a silently-hollow data.json.
    """
    return [k for k, v in season_blocks.items()
            if v["status"] == "unexpected_gap"]


# ----------------------------------------------------------------
# FUTURE ELEMENTS - skeleton stubs (see SEASONAL_INTEGRATION.md)
# Each is deliberately tiny: fill in when the feature is built.
# ----------------------------------------------------------------

def narrative_regions(season_blocks: dict) -> list[str]:
    """Item 4 (season-aware narrative): which regions may appear in
    the auto-written headline sentence. Rule: only 'active' regions.
    Already usable today - build_summary.py can filter on this."""
    return [k for k, v in season_blocks.items() if v["status"] == "active"]


def suppress_historical_band(season_block: dict) -> bool:
    """Item 5 (baseline matching): if a region is out of season,
    its 3-year band should not be plotted against a missing line.
    dashboard.js reads the same status; this exists so the Python
    side can also zero out band data before it ships."""
    return season_block["status"] != "active"


def peru_colombia_hook():
    """Item 6 placeholder. When a fetch_imports.py exists, its output
    should register regions 'peru' / 'colombia' and pass their
    last_reported dates through classify() like everyone else.
    Nothing to do here yet - the seasons.json entries already exist."""
    raise NotImplementedError("Add fetch_imports.py first - see SEASONAL_INTEGRATION.md")


# ----------------------------------------------------------------
# Self-checks: python seasonality.py
# ----------------------------------------------------------------

if __name__ == "__main__":
    seasons = load_seasons()
    checks = [
        # (desc, region, last_reported, today, expected_status)
        ("MX fresh data mid-year", "mx", "2026-07-10", date(2026, 7, 14), "active"),
        ("MX gone dark in July", "mx", "2026-06-01", date(2026, 7, 14), "unexpected_gap"),
        ("CA reporting in June", "ca", "2026-06-28", date(2026, 7, 1), "active"),
        ("CA dark in December", "ca", "2026-09-28", date(2026, 12, 15), "out_of_season"),
        ("CA dark in June (bad)", "ca", "2026-04-01", date(2026, 6, 15), "unexpected_gap"),
        ("CA dark in early Oct (grace)", "ca", "2026-09-25", date(2026, 10, 15), "unexpected_gap"),
        ("CA dark in Nov (past grace)", "ca", "2026-09-25", date(2026, 11, 15), "out_of_season"),
        ("Never-reported region in window", "ca", None, date(2026, 6, 15), "unexpected_gap"),
        ("Unknown region key", "chile", "2026-07-01", date(2026, 7, 14), "unexpected_gap"),
    ]
    failures = 0
    for desc, key, last, today, want in checks:
        got = classify(key, last, today=today, seasons=seasons)["status"]
        ok = got == want
        failures += (not ok)
        print(f"{'PASS' if ok else 'FAIL'}  {desc}: {got}" + ("" if ok else f" (wanted {want})"))
    print(f"\n{len(checks) - failures}/{len(checks)} checks passed")
    raise SystemExit(1 if failures else 0)