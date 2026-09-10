# build_summary.py
# ---------------------------------------------------------------
# Merges the four raw feeds + committed histories into the single
# data.json the front end consumes, and writes the auto-generated
# weekly headline sentence.
#
# Reads:  history/movement_weekly.json, history/pricing_weekly.json,
#         data/raw/{usda_current,freight,diesel,weather}.json
# Writes: data.json  (repo root — served by GitHub Pages)
# ---------------------------------------------------------------

import json
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from seasonality import (classify, any_unexpected_gaps,
                         load_crop_calendar, current_stage, in_window)

ROOT = Path(__file__).parent
RAW = ROOT / "data" / "raw"
HIST = ROOT / "history"

TREND_WEEKS = 52

SEASONAL_WINDOW_WEEKS = 2      # +/- N ISO weeks around the target week
BASELINE_MIN_SAMPLES = 8       # below this, suppress the percentile signal
RECENCY_CUTOFF_DAYS = 180      # unchanged; was hardcoded as timedelta(days=180)

# Week-over-week magnitude signals. A move fires only when it is BOTH
# seasonally unusual (>= this percentile of same-week history) AND materially
# large (>= the per-series floor below).
#
# PRICE ONLY. Volume series (mx, imports) were removed: USDA weekly movement
# attributes the same shipments to different weeks than AVIS does — a big weekly
# volume "move" measures when reporting landed, not what shipped, and cannot
# survive the underlying data quality. Rolling-4 volume (the honest unit) lives
# on the headline and region rows instead. Price comes from daily shipping-point
# quotes, which have no such attribution problem, so its WoW stays meaningful.
WOW_SEASONAL_PCTILE = 90        # move must rank this high among same-week history
WOW_FLOORS = {                  # ...AND exceed this absolute magnitude (%)
    "price_mx": 12.0,   # ~90th pct of abs(wow); pinned <=21.8 by 2026-08-22
}
MAX_WOW_SIGNALS = 2             # report at most the N largest; do not flood the box

FREIGHT_STALE_AFTER_DAYS = 10   # report is weekly; 10 days = missed a cycle
FETCH_STALE_AFTER_DAYS = 8      # weekly Action; 8 days = missed a cycle
ENSO_STALE_AFTER_DAYS = 45      # ENSO is monthly — do not judge it on a weekly clock
ENSO_WEEKLY_STALE_AFTER_DAYS = 14  # Niño 1+2 is WEEKLY — 45 would mask a month of misses

# ENSO signal gating. The band alone is not enough: RONI can read "weak" during
# a fast ramp that CPC is already issuing an advisory for, so we also fire on an
# established event or a steep recent move.
ENSO_MODERATE_ANOM = 1.0        # |RONI| at/above this is moderate+ on its own
ENSO_STEEP_DELTA = 0.75         # 3-season change past this = fast move / lagging mean

# Lambayeque coastal flood signal, driven by Niño 1+2 (the eastern box off Peru),
# NOT Niño 3.4/RONI. Threshold +1.8 °C is the midpoint of the empirical gap between
# ordinary years (peak <=+1.6) and the four documented flood years (1982-83,
# 1997-98, 2017, 2023; peak >=+2.0), calibrated on 1981-2026 weekly SST. Gated to
# Oct-Mar: Niño 1+2 warming through the fall/winter is the lead indicator for the
# Jan-Mar coastal rain window; a warm anomaly in June has no seasonal consequence.
NINO12_FLOOD_THRESHOLD = 1.8
NINO12_GATE_MONTHS = {10, 11, 12, 1, 2, 3}
# Coastal-pattern condition, INDEPENDENT of the absolute threshold: the coast
# running this far above the central Pacific fires on its own (this is how 2017
# and 2023 flooded with the central Pacific near neutral). Raised 1.0 -> 1.5 so
# it isolates the three genuine coastal years (1997-98 +1.8, 2016-17 +2.1,
# 2022-23 +2.0) and excludes ordinary strong events (2023-24 +1.1, 2006-07 +1.4).
NINO12_DIVERGENCE_MIN = 1.5
NINO12_NEAR_RECORD = 3.7        # >=99th pctile of 1981-2026 → "near the warmest"


def freight_stale_days(report_date):
    """How many days old the freight report is. None if unknown."""
    if not report_date:
        return None
    return (date.today() - date.fromisoformat(report_date)).days


def fetch_age_days(fetched_at):
    """Days since the feed was last fetched. None if unknown."""
    if not fetched_at:
        return None
    try:
        ts = datetime.fromisoformat(fetched_at)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).days

# Freight lanes to feature: destination -> fallback destination.
FREIGHT_DESTS = [("Los Angeles", None), ("Dallas", None),
                 ("Miami", None), ("Philadelphia", "Baltimore")]
# Origin districts in priority order (most avocado-relevant first).
FREIGHT_ORIGINS = [
    ("MEXICO CROSSINGS THROUGH SOUTH TEXAS", "S. Texas crossings (McAllen/Pharr)"),
    ("SOUTH AND CENTRAL DISTRICT CALIFORNIA", "South & Central CA"),
    ("OXNARD DISTRICT CALIFORNIA", "Oxnard district CA"),
    ("MEXICO CROSSINGS THROUGH NOGALES ARIZONA", "Nogales AZ crossings"),
]

DISTRICT_SHORT = {
    "MEXICO CROSSINGS THROUGH PHARR TEXAS": "Pharr, TX",
    "MEXICO CROSSINGS THROUGH LAREDO TEXAS": "Laredo, TX",
    "MEXICO CROSSINGS THROUGH NOGALES ARIZONA": "Nogales, AZ",
    "MEXICO CROSSINGS THROUGH OTAY MESA CALIFORNIA": "Otay Mesa, CA",
    "MEXICO CROSSINGS THROUGH TEXAS": "Texas crossings",
    "SOUTH DISTRICT CALIFORNIA": "South District CA",
}

PRICE_DISPLAY = {
    "MEXICO CROSSINGS THROUGH TEXAS": "Mexico Crossings — Texas",
    "SOUTH DISTRICT CALIFORNIA": "South District California",
}


def load(path: Path, default=None):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def iso_week(d: str) -> int:
    return date.fromisoformat(d).isocalendar().week


# Supply is grouped by ORIGIN (the third field of each movement key), not by a
# substring of the district name. The old district-substring routing misrouted
# "IMPORTS THROUGH LOS ANGELES-LONG BEACH CALIFORNIA | Peru" into California and
# "SOUTH FLORIDA | Florida" into seaport imports. Origin is unambiguous.
ORIGIN_REGION = {
    "Mexico": "mx",
    "California-South": "ca",
    "Peru": "peru",
    "Colombia": "colombia",
    "Chile": "chile",
}
REGION_ORDER = ["mx", "ca", "peru", "colombia", "chile", "other"]
# Import origins (everything shipped in that isn't Mexican or Californian). Used
# for the aggregate "seaport imports" swing signal, which watches the total, not
# a single origin line.
IMPORT_KEYS = ["peru", "colombia", "chile", "other"]
REGION_NAMES = {
    "mx": "Mexico",
    "ca": "California",
    "peru": "Peru",
    "colombia": "Colombia",
    "chile": "Chile",
    "other": "Other imports (mainly Dominican Republic)",
}


def region_of(origin: str) -> str:
    """Bucket a movement row by its ORIGIN string. Anything not separately
    tracked (Dominican Republic, Florida, Jamaica, Grenada, NZ...) → 'other'."""
    return ORIGIN_REGION.get(origin, "other")


def pct(new, old):
    if not old:
        return None
    return round((new - old) / old * 100, 1)


def mid(row):
    """Volume-concentrated midpoint. USDA 'mostly' is where the bulk of
    sales cleared; range low/high includes thin quotes at both ends and
    overstates whenever the high is thin (most weeks). Fall back to the full
    range only when USDA omits 'mostly' (no dominant range that day).

    Returns (value, basis) where basis is 'mostly' | 'range' | None so a
    mixed-basis series is visible rather than silently blended.
    """
    ml, mh = row.get("mostly_low"), row.get("mostly_high")
    if ml is not None and mh is not None:
        return (float(ml) + float(mh)) / 2, "mostly"
    lo, hi = row.get("low"), row.get("high")
    if lo is not None and hi is not None:
        return (float(lo) + float(hi)) / 2, "range"
    return None, None


def is_conventional(v) -> bool:
    return v in (None, "", "N", "No", "n")


def is_hass_benchmark(row) -> bool:
    return ((row.get("variety") or "").upper().find("HASS") >= 0
            and row.get("size") == "48s"
            and "2 layer" in (row.get("package") or "")
            and is_conventional(row.get("organic")))


# ---------------------------------------------------------------
# Supply
# ---------------------------------------------------------------

def build_supply(movement: dict, notes: list) -> dict:
    weekly = {}      # week_end -> {mx, ca, peru, colombia, chile, other}
    by_district = {} # week_end -> {district: lbs}
    for key, lbs in movement.items():
        week, district, origin = key.split("|", 2)
        weekly.setdefault(week, {k: 0 for k in REGION_ORDER})
        weekly[week][region_of(origin)] += lbs
        by_district.setdefault(week, {})
        by_district[week][district] = by_district[week].get(district, 0) + lbs

    weeks = sorted(weekly)
    if not weeks:
        notes.append("No movement data available.")
        return {}
    cur_w, prior_w = weeks[-1], (weeks[-2] if len(weeks) > 1 else None)

    def total(w):
        return sum(weekly[w].values()) if w else 0

    def seasonal_sample(week_str, series_fn):
        """Prior-season values from the same point in the season (+/- SEASONAL_WINDOW_WEEKS).

        Uses mean (not median) in seasonal_avg — supply volumes are driven by
        predictable crop cycles without single-season price extremes.
        """
        wn = iso_week(week_str)
        cutoff = date.fromisoformat(week_str) - timedelta(days=RECENCY_CUTOFF_DAYS)
        return [series_fn(w) for w in weeks
                if _iso_week_distance(iso_week(w), wn) <= SEASONAL_WINDOW_WEEKS
                and date.fromisoformat(w) < cutoff]

    def seasonal_avg(week_str, series_fn):
        vals = seasonal_sample(week_str, series_fn)
        return statistics.mean(vals) if vals else None

    # NOTE on seasonal comparisons for VOLUME. These vs-prior-season figures
    # (vs_3yr_pct, the rolling-4 baseline) are honest as DESCRIPTIVE LEVELS but are
    # NOT anomaly detectors, because volume TRENDS: arrivals grow ~15%/yr, so a
    # recent week sits structurally above a multi-year mean and any threshold on
    # the deviation fires one-sided (backtested 67% positive, 100% in 2026). This
    # is the opposite of price, which mean-reverts and so supports a symmetric
    # seasonal-percentile signal — see the band note in build_pricing. So: use
    # these for context/KPI levels, never to gate a "what to watch" line.

    # USDA posts some districts late (CA domestic movement especially), so
    # the newest week can be a fraction of the true total. Flag a region as
    # partial when it prints far below its own trailing median, and keep it
    # out of the comparisons instead of headlining a phantom collapse.
    def trailing_median(key):
        prior_vals = [weekly[w][key] for w in weeks[-5:-1]]
        return statistics.median(prior_vals) if prior_vals else 0
    def last_reported_week(key):
        """Most recent week where THIS region actually had volume.
        Returns None if it has never reported."""
        for w in reversed(weeks):
            if weekly[w][key] > 0:
                return w
        return None
    
    partial_keys = set()
    regions = []
    for key in REGION_ORDER:
        name = REGION_NAMES[key]
        cur = weekly[cur_w][key]
        pri = weekly[prior_w][key] if prior_w else 0
        avg = seasonal_avg(cur_w, lambda w, k=key: weekly[w][k])
        med = trailing_median(key)
        season = classify(key, last_reported_week(key))

        # Rolling 4-week volume — the honest unit for this data. USDA and AVIS
        # agree on Mexico's 4-week total but disagree by up to 20M on which week
        # fruit landed in, so weekly changes measure reporting timing, not
        # shipments. Rolling-4 converges across sources; weekly bars stay as
        # detail but must not drive a headline or signal. Needs 8 weeks.
        roll4_lbs = (sum(weekly[w][key] for w in weeks[-4:])
                     if len(weeks) >= 4 else None)
        roll4_prior_lbs = (sum(weekly[w][key] for w in weeks[-8:-4])
                           if len(weeks) >= 8 else None)
        roll4_pct = (pct(roll4_lbs, roll4_prior_lbs) if roll4_prior_lbs else None)

        # An out-of-season region reporting near zero is a KNOWN zero, not a
        # missing report. Only an in-season region can be "partially reported".
        looks_low = med > 1e6 and cur < 0.3 * med
        partial = looks_low and season["status"] != "out_of_season"

        if partial:
            partial_keys.add(key)
            notes.append(f"{name} movement for the latest week appears "
                         "partially reported by USDA; week-over-week and "
                         "seasonal comparisons suppressed until revised.")
            # A partial current week understates roll4_lbs, so suppress the %
            # just like wow_pct / vs_3yr_pct.
            roll4_pct = None
        regions.append({
            "key": key, "name": name, "lbs": cur,
            "partial": partial,
            "wow_pct": None if partial else pct(cur, pri),
            "roll4_lbs": roll4_lbs,
            "roll4_prior_lbs": roll4_prior_lbs,
            "roll4_pct": roll4_pct,
            "vs_3yr_pct": None if partial or not avg else pct(cur, avg),
            "season": season,
        })

    crossings = []
    for district, lbs in sorted(by_district.get(cur_w, {}).items(),
                                key=lambda kv: -kv[1]):
        if not district.startswith("MEXICO CROSSINGS"):
            continue
        prior = by_district.get(prior_w, {}).get(district) if prior_w else None
        crossings.append({
            "district": district,
            "short": DISTRICT_SHORT.get(district, district.title()),
            "lbs": lbs,
            "wow_pct": pct(lbs, prior) if prior else None,
        })

    # totals & comparisons over reliably-reported regions only
    def total_reliable(w):
        return sum(weekly[w][k] for k in REGION_ORDER
                   if k not in partial_keys)

    # Trend chart's seasonal-average line uses total_reliable — the SAME basis as
    # the KPI's total_vs_3yr_pct — so dividing the displayed total by the chart
    # line reproduces the displayed percentage. partial_keys is derived from the
    # current week; every displayed baseline excludes the same regions.
    trend = []
    for w in weeks[-TREND_WEEKS:]:
        _avg = seasonal_avg(w, total_reliable)
        row = {"week": w, "avg3yr": round(_avg) if _avg else None}
        for k in REGION_ORDER:
            row[k] = weekly[w][k]
        trend.append(row)

    rel_sample = seasonal_sample(cur_w, total_reliable)
    avg_rel = statistics.mean(rel_sample) if rel_sample else None
    baseline_n = len(rel_sample)
    wn_cur = iso_week(cur_w)
    cutoff_cur = date.fromisoformat(cur_w) - timedelta(days=RECENCY_CUTOFF_DAYS)
    baseline_years = len({date.fromisoformat(w).year for w in weeks
                          if _iso_week_distance(iso_week(w), wn_cur) <= SEASONAL_WINDOW_WEEKS
                          and date.fromisoformat(w) < cutoff_cur})
    excluded = sorted(partial_keys)

    # "Seaport imports" is now an AGGREGATE of the four import origins, not a
    # single region line. The swing signal watches the total (Peru + Colombia +
    # Chile + other), so build a per-week series for it and a current-week
    # summary. Suppress its wow if any import origin is partial this week.
    def imports_total(w):
        return sum(weekly[w][k] for k in IMPORT_KEYS)
    imports_partial = bool(partial_keys & set(IMPORT_KEYS))
    imports_agg = {
        "lbs": imports_total(cur_w),
        "wow_pct": (None if imports_partial or not prior_w
                    else pct(imports_total(cur_w), imports_total(prior_w))),
    }

    # Rolling 4-week total over the SAME reliable regions as total_reliable, for
    # the headline and total KPI — the stable read that both data sources agree on.
    def roll_sum_reliable(wk_slice):
        return sum(weekly[w][k] for w in wk_slice
                   for k in REGION_ORDER if k not in partial_keys)
    total_roll4_lbs = roll_sum_reliable(weeks[-4:]) if len(weeks) >= 4 else None
    total_roll4_prior = roll_sum_reliable(weeks[-8:-4]) if len(weeks) >= 8 else None
    total_roll4_pct = (pct(total_roll4_lbs, total_roll4_prior)
                       if total_roll4_prior else None)

    # Rolling 4-week seasonal baseline: the sum of the same-week seasonal averages
    # over the last 4 weeks (the SAME per-week baseline the trend line draws). The
    # rolling total is compared against THIS, so the KPI tile and signal report a
    # like-for-like rolling change instead of a single noisy week vs a single-week
    # baseline. Dividing total_roll4_lbs by total_roll4_baseline_lbs reproduces
    # total_roll4_vs_baseline_pct exactly.
    _roll4_weeks = weeks[-4:] if len(weeks) >= 4 else []
    _base_vals = [seasonal_avg(w, total_reliable) for w in _roll4_weeks]
    total_roll4_baseline_lbs = (round(sum(_base_vals))
                                if _base_vals and all(v is not None for v in _base_vals)
                                else None)
    total_roll4_vs_baseline_pct = (pct(total_roll4_lbs, total_roll4_baseline_lbs)
                                    if total_roll4_baseline_lbs else None)

    return {
        "week_end": cur_w,
        "imports_agg": imports_agg,
        "total_lbs": total_reliable(cur_w),
        "total_lbs_all_regions": total(cur_w),
        "total_excludes": excluded,
        "total_wow_pct": pct(total_reliable(cur_w), total_reliable(prior_w))
                         if prior_w else None,
        "total_vs_3yr_pct": pct(total_reliable(cur_w), avg_rel) if avg_rel else None,
        "total_roll4_lbs": total_roll4_lbs,
        "total_roll4_pct": total_roll4_pct,
        "total_roll4_baseline_lbs": total_roll4_baseline_lbs,
        "total_roll4_vs_baseline_pct": total_roll4_vs_baseline_pct,
        "baseline_n": baseline_n,
        "baseline_years": baseline_years,
        "partial_regions": sorted(partial_keys),
        "regions": regions,
        "crossings": crossings,
        "trend": trend,
    }


# ---------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------

def build_pricing(price_hist: dict, current: dict, notes: list) -> dict:
    # weekly benchmark mids per district
    series = {}  # district -> {week: mid}
    basis_counts = {"mostly": 0, "range": 0}
    for row in price_hist.values():
        if not is_hass_benchmark(row):
            continue
        m, basis = mid(row)
        if m is None:
            continue
        basis_counts[basis] += 1
        series.setdefault(row["district"], {})[row["week_end"]] = m

    # Surface a mixed-basis series: the midpoint prefers USDA 'mostly', but a
    # row with no dominant range falls back to full range low/high. A heavily
    # mixed series would need different treatment, so make the split auditable.
    n_basis = basis_counts["mostly"] + basis_counts["range"]
    if basis_counts["range"] and n_basis:
        notes.append(
            f"Price benchmark: {basis_counts['range']} of {n_basis} rows "
            f"({basis_counts['range'] / n_basis * 100:.0f}%) used range-basis "
            "midpoint (USDA 'mostly' absent that day).")

    mx = series.get("MEXICO CROSSINGS THROUGH TEXAS", {})
    ca = series.get("SOUTH DISTRICT CALIFORNIA", {})
    all_weeks = sorted(set(mx) | set(ca))
    if not all_weeks:
        notes.append("No benchmark pricing history found.")
        return {}

    def seasonal_sample(week_str):
        """Prior-year (week, value) pairs from the same point in the season."""
        wn = iso_week(week_str)
        cutoff = date.fromisoformat(week_str) - timedelta(days=RECENCY_CUTOFF_DAYS)
        return [(w, v) for w, v in mx.items()
                if _iso_week_distance(iso_week(w), wn) <= SEASONAL_WINDOW_WEEKS
                and date.fromisoformat(w) < cutoff]

    def band(week_str):
        """(low, high) quartile band for the trend chart."""
        return _quartile_band([v for _, v in seasonal_sample(week_str)])

    trend = []
    for w in all_weeks[-TREND_WEEKS:]:
        lo, hi = band(w)
        trend.append({"week": w,
                      "mx_mid": mx.get(w), "ca_mid": ca.get(w),
                      "band_low": lo, "band_high": hi})

    mx_weeks = sorted(mx)
    wow_mx = pct(mx[mx_weeks[-1]], mx[mx_weeks[-2]]) if len(mx_weeks) > 1 else None
    ca_weeks = sorted(ca)
    wow_ca = pct(ca[ca_weeks[-1]], ca[ca_weeks[-2]]) if len(ca_weeks) > 1 else None

    # current-day size/grade table
    table = []
    for district in PRICE_DISPLAY:
        rows = [r for r in current.get("rows", []) if r["district"] == district
                and "HASS" in (r.get("variety") or "").upper()
                and is_conventional(r.get("organic"))]
        if not rows:
            continue
        sizes = sorted(
            ({"size": r["size"], "low": r["low"], "high": r["high"],
              "mostly_low": r["mostly_low"], "mostly_high": r["mostly_high"]}
             for r in rows if r.get("size")),
            key=lambda s: int("".join(filter(str.isdigit, s["size"])) or 999))
        tone = rows[0]
        table.append({
            "district": district,
            "display": PRICE_DISPLAY[district],
            "package": rows[0].get("package"),
            "sizes": sizes,
            "tone": {"market": tone.get("market_tone"),
                     "supply": tone.get("supply_tone"),
                     "demand": tone.get("demand_tone")},
        })

    latest_mx = mx[mx_weeks[-1]] if mx_weeks else None

    # Seasonal percentile band — a VALID anomaly detector for price, because price
    # MEAN-REVERTS. Unlike volume (which trends ~15%/yr and so sits structurally
    # above a multi-year mean — see build_supply / build_signals section 1), the
    # FOB benchmark has no secular drift, so its percentile rank within the same-
    # week prior-season sample is roughly uniform: backtested 2023-2026 it sat
    # below the 25th 38% of weeks and above the 75th 38% of weeks — symmetric.
    # That symmetry is what makes "outside the 25-75 band" a real signal here and
    # NOT for volume. Keep this distinction in mind before reusing the seasonal-
    # percentile approach on any new series: ask first whether it trends.
    raw_sample = seasonal_sample(mx_weeks[-1]) if mx_weeks else []
    sample = [v for _, v in raw_sample]
    baseline_n = len(sample)
    baseline_years = len({date.fromisoformat(w).year for w, _ in raw_sample})
    band_position = None
    if latest_mx is not None and baseline_n >= BASELINE_MIN_SAMPLES:
        band_position = _percentile_rank(sample, latest_mx)

    return {
        "mx_series": mx,   # {week: benchmark mid} for the week-over-week signal
        "benchmark": {
            "label": "Hass 48s, 2-layer cartons (conventional), FOB/shipping point",
            "mx_latest": latest_mx, "ca_latest": ca[ca_weeks[-1]] if ca_weeks else None,
            "wow_mx_pct": wow_mx, "wow_ca_pct": wow_ca,
            "band_position_pct": band_position,
            "baseline_n": baseline_n,
            "baseline_years": baseline_years,
            # Week-ending date of the weekly benchmark series. Distinct from
            # report_date (the daily size/grade table below): the two answer
            # different questions and legitimately differ by a few days.
            "week": mx_weeks[-1] if mx_weeks else None,
        },
        "report_date": current.get("report_date"),
        "table": table,
        "trend": trend,
    }


# ---------------------------------------------------------------
# Freight
# ---------------------------------------------------------------

def build_freight(freight: dict, notes: list) -> dict:
    if not freight or not freight.get("sections"):
        notes.append("Freight report unavailable this week.")
        return {}
    sections = {s["district"]: s for s in freight["sections"]}

    lanes = []
    for dest, fallback in FREIGHT_DESTS:
        found = None
        for origin, origin_short in FREIGHT_ORIGINS:
            rows = sections.get(origin, {}).get("rows", [])
            # prefer avocado-specific subsections when the district has them
            candidates = ([r for r in rows if r["dest"] == dest and
                           "AVOCADO" in (r.get("commodities") or "").upper()]
                          or [r for r in rows if r["dest"] == dest])
            if not candidates and fallback:
                candidates = ([r for r in rows if r["dest"] == fallback and
                               "AVOCADO" in (r.get("commodities") or "").upper()]
                              or [r for r in rows if r["dest"] == fallback])
            if candidates:
                r = candidates[0]
                found = {"dest": r["dest"], "origin": origin,
                         "origin_short": origin_short,
                         "origin_is_preferred": origin == FREIGHT_ORIGINS[0][0],
                         **{k: r[k] for k in
                         ("availability", "low", "high", "mostly_low",
                          "mostly_high", "wow_pct", "wow_reported")}}
                break
        if found:
            lanes.append(found)
        else:
            notes.append(f"No freight lane quoted into {dest} this week.")

    availability = []
    for origin, origin_short in FREIGHT_ORIGINS:
        rows = sections.get(origin, {}).get("rows", [])
        if rows:
            statuses = [r["availability"] for r in rows]
            availability.append({"district": origin_short,
                                 "status": max(set(statuses), key=statuses.count)})

    return {"report_date": freight.get("report_date"),
            "lanes": lanes, "availability": availability}


# ---------------------------------------------------------------
# Diesel / weather passthroughs
# ---------------------------------------------------------------

def build_diesel(diesel: dict) -> dict:
    if not diesel or not diesel.get("available"):
        return {"available": False,
                "reason": (diesel or {}).get("reason", "no data")}
    out = {"available": True, "latest": {}, "series": diesel["series"]}
    for name, pts in diesel["series"].items():
        if len(pts) >= 2:
            out["latest"][name] = {
                "period": pts[-1]["period"], "value": pts[-1]["value"],
                "wow": round(pts[-1]["value"] - pts[-2]["value"], 3)}
    return out


FLAG_RANK = {"alert": 3, "watch": 2, "normal": 1, "unknown": 0}

# Origin-country labels + why a dark weather group matters. Keyed by the
# `country` code on each weather region. The framing differs deliberately: a
# dark group is not a count, it is a specific blind spot — Mexico blinds the
# near-term supply read, Peru blinds the forward flowering signal.
COUNTRY_LABELS = {"MX": "Mexico", "US": "California", "CO": "Colombia", "PE": "Peru"}
WEATHER_DARK_FRAMING = {
    "MX": "Mexico drives ~80% of near-term supply, so the 2-4 week supply read "
          "is running blind this cycle.",
    "US": "the California belt has no live weather this cycle.",
    "CO": "the Colombian main-crop drought read is unsupported this cycle.",
    "PE": "the Peru forward signal — flowering that sets the 2027 crop — is "
          "unsupported this cycle.",
}


def build_weather(weather: dict) -> dict:
    if not weather:
        return {"overall_flag": "unknown", "regions": [],
                "coverage": {}, "dark_groups": []}
    regions = weather.get("regions", [])

    # Label each region's phenological stage (harvest/flowering/sizing) from the
    # crop calendar. Per Amendment 2 we do NOT mute out-of-season regions — a
    # region outside harvest is often exactly where the leading indicator lives
    # (Peru's Oct-Feb flowering under an El Niño peak). Regions without a
    # calendar entry (Michoacán, California) get stage=None and render as before.
    cal = load_crop_calendar()
    today = date.today()
    for r in regions:
        stages = cal.get(r.get("key"), {})
        r["stage"] = current_stage(stages, today) if stages else None

    overall = max(regions, key=lambda r: FLAG_RANK.get(r.get("flag"), 0),
                  default=None)
    return {"overall_flag": overall["flag"] if overall else "unknown",
            "coverage": weather.get("coverage", {}),
            "dark_groups": weather.get("dark_groups", []),
            "regions": regions}


# ---------------------------------------------------------------
# ENSO (seasonal-to-annual layer)
# ---------------------------------------------------------------

def _confidence_rank(conf: str) -> float:
    """Sortable rank from the prose confidence strings in enso_response.json.
    Reads the leading qualifier ('high on drought, ...' -> high)."""
    c = (conf or "").lower().strip()
    if c.startswith("high"):
        return 4.0
    if c.startswith("moderate-high"):
        return 3.5
    if c.startswith("moderate"):
        return 3.0
    if c.startswith("low-moderate"):
        return 2.0
    if c.startswith("low"):
        return 1.0
    return 0.0


TIER_RANK = {"primary": 3, "secondary": 2, "tertiary": 1}


def _pathways_of(o: dict) -> list:
    """Each (type, pathway) an origin exposes. Origins with a 'pathways' array
    carry them explicitly; single-pathway origins synthesise one from their
    top-level lag_months (near_term if lag starts <6 months, else forward)."""
    if o.get("pathways"):
        out = []
        for p in o["pathways"]:
            lm = p.get("lag_months") or [0, 0]
            ptype = p.get("type") or ("forward" if lm[0] >= 6 else "near_term")
            out.append((ptype, p))
        return out
    lm = o.get("lag_months") or [0, 0]
    ptype = "forward" if lm[0] >= 6 else "near_term"
    return [(ptype, {"type": ptype, "lag_months": lm,
                     "mechanism": o.get("mechanism"), "impact": o.get("impact"),
                     "signal_effect": o.get("signal_effect")})]


def _enso_slots(origins: list) -> dict:
    """Amendment 5 (revised): one near-term slot and one forward slot, each the
    top origin exposing that pathway. Within a slot rank by
    (stage intersects watch window, supply tier, confidence), dedupe by country.
    Slots are independent — an empty slot is left empty rather than backfilled,
    and one origin (Michoacán) may legitimately hold both via its two pathways."""
    def sort_key(o):
        return (1 if (o.get("stage") and o.get("in_watch")) else 0,
                TIER_RANK.get(o.get("supply_tier"), 0),
                _confidence_rank(o.get("confidence")))

    def lag0(p):
        lm = p.get("lag_months") or [999]
        return lm[0]

    def pick(cands):
        # One pathway per country: rank first by the origin (stage/tier/confidence),
        # then, when the same origin exposes several pathways in this slot, keep the
        # nearer-lag one (Colombia's Traviesa 7-12 surfaces before Principal 13-18).
        best = {}
        for o, p in cands:
            k = o["country"]
            cand_key = (sort_key(o), -lag0(p))
            if k not in best or cand_key > best[k][1]:
                best[k] = ((o, p), cand_key)
        ranked = sorted(best.values(), key=lambda t: t[1], reverse=True)
        return ranked[0][0] if ranked else None

    near, fwd = [], []
    for o in origins:
        for ptype, p in _pathways_of(o):
            (near if ptype == "near_term" else fwd).append((o, p))
    return {"near_term": pick(near), "forward": pick(fwd)}


def _enso_slot_dict(pick: tuple) -> dict | None:
    """Flatten a (origin, pathway) pick into the display/signal fields."""
    if not pick:
        return None
    o, p = pick
    return {
        "key": o["key"], "name": o["name"], "country": o["country"],
        "weather_dark": o.get("weather_dark", False),
        "supply_tier": o.get("supply_tier"),
        "signal_effect": p.get("signal_effect") or o.get("signal_effect") or "",
        "lag_months": p.get("lag_months"),
    }


def build_enso(enso_raw: dict, response: dict, weather: dict) -> dict:
    """Assemble the ENSO block: index state (RONI led, ONI alongside), display
    helpers that lead with trend and always carry the season, and per-origin
    teleconnection rows annotated with current stage / watch intersection /
    whether that origin's live weather is currently dark."""
    if not enso_raw or not enso_raw.get("roni"):
        return {"available": False}

    roni, oni = enso_raw["roni"], enso_raw.get("oni") or {}
    anom = roni["latest"]["anom"]
    season = f'{roni["latest"]["season"]} {roni["latest"]["year"]}'
    trend = roni.get("trend", "steady")
    delta3 = roni.get("delta3")
    ref3 = roni.get("three_seasons_ago")
    steep = delta3 is not None and abs(delta3) > ENSO_STEEP_DELTA
    established = bool(roni.get("established_event"))

    side = "El Niño" if anom > 0 else ("La Niña" if anom < 0 else "ENSO-neutral")
    # #2 lead with trend + direction, not the band; #1 season always with value.
    # Plain language for a produce reader — no index acronym in body copy.
    lead = f"{side} {trend}" if side != "ENSO-neutral" else f"ENSO {trend}"
    up = f", up from {ref3['anom']:+.2f} three seasons ago" if ref3 else ""
    headline = f"{lead} — {anom:+.2f} for {season}{up}"
    # #3 lag caveat only when the recent move is steep.
    lag_caveat = ("the three-month average trails a fast-developing event, so "
                  "conditions are likely stronger than that label suggests") if steep else None

    # #4 gate: band alone would stay silent at RONI +0.98 during an advisory.
    fire = (abs(anom) >= ENSO_MODERATE_ANOM) or established or (trend == "strengthening" and steep)

    dark = set((weather or {}).get("dark_groups") or [])
    cal = load_crop_calendar()
    today = date.today()
    origins = []
    for o in (response or {}).get("origins", []):
        country = o["key"].split("_")[0].upper()   # mx_michoacan -> MX
        stage = current_stage(cal.get(o["key"], {}), today)
        wm = o.get("watch_months")
        in_watch = bool(wm and in_window({"window": wm}, today))
        origins.append({**o, "country": country, "stage": stage,
                        "in_watch": in_watch, "weather_dark": country in dark})

    slots = _enso_slots(origins)
    near_term = _enso_slot_dict(slots["near_term"])
    forward = _enso_slot_dict(slots["forward"])

    # The story this layer now tells: more than one of the largest origins is
    # setting NEXT season's crop during the event, right now. State it plainly
    # rather than leaving it implicit in a lag number.
    fwd_exposed, seen_countries = [], set()
    for o in origins:
        if o.get("stage") and o.get("in_watch") and \
                o["country"] not in seen_countries and \
                any(t == "forward" for t, _ in _pathways_of(o)):
            seen_countries.add(o["country"])   # one name per country, not per region
            # Trim sibling regions (" / "), parentheticals, and the ", Antioquia"
            # style qualifier so a comma inside one name can't read as a list item.
            short = o["name"].split(" / ")[0].split(" (")[0].split(",")[0].strip()
            fwd_exposed.append(short)
    forward_note = None
    if side == "El Niño" and len(fwd_exposed) >= 2:
        n = len(fwd_exposed)
        lead = "Both" if n == 2 else ("All three" if n == 3 else f"All {n}")
        # Qualify with the LIVE index state, not a frozen forecast: read the
        # current phase and trend off the fetched RONI so the intensity language
        # always matches what the panel is actually showing.
        phase = roni["latest"]["phase"]                      # e.g. "weak El Niño"
        trend_verb = {"strengthening": "strengthens",
                      "weakening": "weakens"}.get(trend, "holds")
        forward_note = (f"{lead} of the largest origins are setting next season's "
                        f"crop as the current {phase} {trend_verb}: "
                        f"{', '.join(fwd_exposed)}.")

    # Niño 1+2 coastal flood layer (Lambayeque only). Weekly SST, its own cadence.
    # Scoped here — NOT attached to any origin row — so it can never leak into the
    # central-coast (Cañete, Áncash) or Amazon-facing (Monobamba) Peru origins,
    # which respond to different forcing. Gated to Oct-Mar.
    nino12_raw = enso_raw.get("nino12")
    nino12 = None
    if nino12_raw and nino12_raw.get("latest"):
        lt = nino12_raw["latest"]
        a12 = lt.get("nino12_anom")
        div = lt.get("divergence")
        in_season = today.month in NINO12_GATE_MONTHS
        # Two INDEPENDENT firing conditions (either suffices): the coast reaching
        # the absolute flood level, or the coast running well above the central
        # Pacific (the coastal pattern). The signal names which one triggered.
        fire_abs = a12 is not None and a12 >= NINO12_FLOOD_THRESHOLD
        fire_div = div is not None and div >= NINO12_DIVERGENCE_MIN
        nino12 = {
            "available": True,
            "latest": lt,
            "series": nino12_raw.get("series", []),
            "threshold": NINO12_FLOOD_THRESHOLD,
            "in_season": in_season,
            "fire": bool(in_season and (fire_abs or fire_div)),
            "fire_absolute": bool(fire_abs),
            "fire_divergence": bool(fire_div),
            "near_record": bool(a12 is not None and a12 >= NINO12_NEAR_RECORD),
            "stale": bool(nino12_raw.get("nino12_stale")),
        }

    # Current WEEKLY central-Pacific reading (Niño 3.4), pulled from the same weekly
    # SST file. The panel and signal 1 lead with this — the seasonal RONI value
    # averages three months and lags a fast-developing event. None if weekly SST
    # is unavailable, in which case the signal falls back to the seasonal headline.
    cp_weekly = None
    if nino12_raw and nino12_raw.get("latest"):
        _cp = nino12_raw["latest"]
        if _cp.get("nino34_anom") is not None:
            cp_weekly = {"anom": _cp["nino34_anom"], "week": _cp.get("week")}
    return {
        "available": True,
        "primary": "roni",
        "latest": roni["latest"],   # convenience: the led index's latest season
        "roni": roni,
        "oni": oni,
        "anom": anom,
        "season": season,
        "phase_side": side,
        "trend": trend,
        "delta3": delta3,
        "three_seasons_ago": ref3,
        "steep": steep,
        "established_event": established,
        "consecutive_seasons": roni.get("consecutive_seasons"),
        # Carry the RONI value beside the band label so a reading one-hundredth
        # from the next band (+0.98, "weak", vs 1.0 "moderate") shows its proximity
        # instead of hiding behind the coarse label.
        "phase_with_season": f'{roni["latest"]["phase"]} {anom:+.2f} ({season})',   # #1
        "headline": headline,                                           # #2
        "lag_caveat": lag_caveat,                                       # #3
        "signal_fire": fire,
        "near_term": near_term,   # ranked near-term slot (or None)
        "forward": forward,       # ranked forward slot (or None)
        "forward_note": forward_note,
        "nino12": nino12,         # Niño 1+2 coastal flood layer (Lambayeque)
        "central_pacific_weekly": cp_weekly,  # weekly Niño 3.4 — panel/signal lead
        "origins": origins,
        "fetched_at": enso_raw.get("fetched_at"),
    }


# ---------------------------------------------------------------
# Narrative + signals
# ---------------------------------------------------------------

def direction_word(p, up="up", down="down", flat="flat", decimals=0):
    """Coarse by default (decimals=0) for the signals block; the headline
    passes decimals=1 so its percentages match the KPI cards, which render
    delta_pct with one decimal (dashboard.js deltaHtml -> toFixed(1))."""
    if p is None:
        return flat
    if p > 1:
        return f"{up} {abs(p):.{decimals}f}%"
    if p < -1:
        return f"{down} {abs(p):.{decimals}f}%"
    return flat


def build_headline(supply, pricing, freight, diesel, weather) -> str:
    parts = []
    if supply:
        # Lead with the rolling 4-week total, the unit both data sources agree on.
        # A single week (or one week vs a 4-week average) still carries the
        # attribution noise; the 4-week TOTAL vs the prior four weeks does not.
        mx = next((r for r in supply["regions"] if r["key"] == "mx"), None)
        if mx and mx.get("roll4_lbs") is not None:
            move = direction_word(mx.get("roll4_pct"), decimals=0)
            parts.append(f"Mexico 4-week volume {mx['roll4_lbs'] / 1e6:.1f}M lbs, "
                         f"{move} vs the prior four weeks")
    bm = (pricing or {}).get("benchmark") or {}
    if bm.get("mx_latest") is not None:
        w = bm.get("wow_mx_pct")
        verb = "steady" if w is None or abs(w) <= 1 else ("firmed" if w > 0 else "softened")
        move = "" if verb == "steady" else f" {abs(w):.1f}%"
        parts.append(f"Texas-crossing Hass 48s FOB {verb}{move} at ${bm['mx_latest']:.2f}")
    lanes_list = (freight or {}).get("lanes", [])
    lanes = {l["dest"]: l for l in lanes_list}
    la, dal = lanes.get("Los Angeles"), lanes.get("Dallas")
    if la and dal:
        def word(l):
            w = l.get("wow_pct")
            if w is None:
                return "n/a"
            return "firm" if w > 1 else ("soft" if w < -1 else "flat")
        clause = f"LA/Dallas freight {word(la)}/{word(dal)}"
        # Don't let the two fixed lanes hide a bigger mover: if another lane moved
        # materially more (>= 2 pts beyond the larger of LA/Dallas), name it in the
        # same clause so the headline's freight read isn't blind to it.
        movers = [l for l in lanes_list if l.get("wow_pct") is not None]
        la_dal_max = max(abs(la.get("wow_pct") or 0), abs(dal.get("wow_pct") or 0))
        if movers:
            biggest = max(movers, key=lambda l: abs(l["wow_pct"]))
            if (biggest["dest"] not in ("Los Angeles", "Dallas")
                    and abs(biggest["wow_pct"]) >= la_dal_max + 2):
                bw = "up" if biggest["wow_pct"] > 0 else "down"
                clause += f", {biggest['dest']} {bw} {abs(biggest['wow_pct']):.0f}%"
        parts.append(clause)
    nat = ((diesel or {}).get("latest") or {}).get("national")
    if nat:
        d = nat["wow"]
        verb = "steady" if abs(d) < 0.02 else ("up" if d > 0 else "down")
        move = "" if verb == "steady" else f" {abs(d):.2f}"
        parts.append(f"diesel {verb}{move} at ${nat['value']:.2f}/gal")
    flagged = [r for r in (weather or {}).get("regions", [])
               if r.get("flag") in ("watch", "alert")]
    if flagged:
        parts.append(f"{flagged[0]['name'].split(' (')[0]} weather bears watching")
    return "; ".join(parts) + "." if parts else "Data pending first full refresh."


def build_headline_asof(supply, pricing, freight, diesel) -> str:
    """One line disclosing the as-of date behind each headline clause.

    THE WEEK ON ONE LINE blends panels with genuinely different as-of dates
    (supply/pricing weekly, freight report, diesel period). We don't reconcile
    them — they honestly differ — we disclose them. Dates are sourced from
    data.json fields, never hardcoded, so this stays correct as feeds refresh.
    """
    parts = []
    week_end = (supply or {}).get("week_end")
    if week_end:
        parts.append(f"Supply & pricing wk ending {week_end}")
    fr = (freight or {}).get("report_date")
    if fr:
        parts.append(f"Freight {fr}")
    nat = ((diesel or {}).get("latest") or {}).get("national")
    if nat and nat.get("period"):
        parts.append(f"Diesel {nat['period']}")
    return " · ".join(parts)


def active_import_origins() -> str:
    """Which import origins are plausibly shipping right now, per
    seasons.json. Replaces the hardcoded 'Peru/Colombia/DR season'
    string, which would have been wrong half the year.

    Returns e.g. 'Peru/Colombia' or '' if none are in window.
    """
    from seasonality import load_seasons, in_window
    from datetime import date

    today = date.today()
    seasons = load_seasons()
    # Keys now match the live origin regions. Dominican Republic ships inside the
    # year-round 'other' aggregate, so it surfaces via that key.
    names = {"peru": "Peru", "colombia": "Colombia", "chile": "Chile",
             "other": "DR/other"}
    active = [label for key, label in names.items()
              if key in seasons and in_window(seasons[key], today)]
    return "/".join(dict.fromkeys(active))

def _iso_week_distance(a, b):
    """Circular distance between two ISO week numbers.

    Handles the year boundary: week 52 and week 1 are 2 apart, not 51.
    """
    d = abs(a - b)
    return min(d, 53 - d)


def _percentile_rank(vals, x):
    """True percentile rank of x within vals, 0-100.

    Uses the midpoint convention for ties so that a value equal to every
    observation scores 50 rather than 0 or 100.
    """
    if not vals:
        return None
    below = sum(1 for v in vals if v < x)
    ties = sum(1 for v in vals if v == x)
    return round((below + 0.5 * ties) / len(vals) * 100)


def _quartile_band(vals):
    """25th/75th percentiles, for the trend chart band.

    Falls back to min/max when there are too few points for quantiles.
    """
    if not vals:
        return None, None
    if len(vals) < 4:
        return min(vals), max(vals)
    q = statistics.quantiles(vals, n=4, method="inclusive")
    return q[0], q[2]


def _wow_history(series_by_week, week_str):
    """abs(week-over-week %) for the same ISO week +/- window, prior seasons only.

    series_by_week: {week_str: value}, sorted keys assumed contiguous weekly.
    Reuses RECENCY_CUTOFF_DAYS / SEASONAL_WINDOW_WEEKS / _iso_week_distance so
    the "unusual for this week" comparison matches the pricing-band machinery.
    """
    weeks = sorted(series_by_week)
    idx = {w: i for i, w in enumerate(weeks)}
    wn = iso_week(week_str)
    cutoff = date.fromisoformat(week_str) - timedelta(days=RECENCY_CUTOFF_DAYS)
    out = []
    for w in weeks:
        if _iso_week_distance(iso_week(w), wn) > SEASONAL_WINDOW_WEEKS:
            continue
        if date.fromisoformat(w) >= cutoff:
            continue
        i = idx[w]
        if i == 0:
            continue
        prev = series_by_week[weeks[i - 1]]
        cur = series_by_week[w]
        if not prev:
            continue
        out.append(abs((cur - prev) / prev * 100))
    return out


def _wow_history_years(series_by_week, week_str):
    """Distinct prior seasons contributing to _wow_history's same-week window.

    Mirrors _wow_history's week selection exactly, but counts the calendar years
    the samples fall in rather than the moves themselves — so the signal can say
    "in N seasons of history" instead of an overstated percentile at this n.
    """
    weeks = sorted(series_by_week)
    wn = iso_week(week_str)
    cutoff = date.fromisoformat(week_str) - timedelta(days=RECENCY_CUTOFF_DAYS)
    years = set()
    for i, w in enumerate(weeks):
        if i == 0:
            continue
        if _iso_week_distance(iso_week(w), wn) > SEASONAL_WINDOW_WEEKS:
            continue
        if date.fromisoformat(w) >= cutoff:
            continue
        if not series_by_week[weeks[i - 1]]:
            continue
        years.add(date.fromisoformat(w).year)
    return len(years)


def _wow_candidates(supply, pricing):
    """(key, label, wow_pct, series_dict, week) tuples for the tracked series.

    PRICE ONLY (the Texas-crossing FOB benchmark). Volume series were dropped:
    weekly USDA movement attributes the same shipments to different weeks than
    AVIS does, so a weekly volume "move" measures reporting timing, not what
    shipped. Price is a daily shipping-point quote with no such attribution
    problem, so its WoW remains meaningful. `supply` is kept in the signature
    for the caller and in case a volume series ever regains weekly integrity.

    wow_pct is already None when build_pricing nulls a thin benchmark, so the
    caller's `wow is None` guard skips it. `week` is the series' own latest week,
    used to centre the same-week history window.
    """
    out = []
    bm = (pricing or {}).get("benchmark") or {}
    mx_series = (pricing or {}).get("mx_series") or {}
    if mx_series:
        price_week = max(mx_series)
        out.append(("price_mx", "Texas-crossing Hass 48s FOB",
                    bm.get("wow_mx_pct"), mx_series, price_week))
    return out


def build_signals(supply, pricing, freight, diesel, weather, feeds=None, enso=None) -> list:
    """Surface only what's UNUSUAL this week.

    Every signal is gated behind a threshold, so a quiet week produces a
    short list (or none) rather than four sentences restating normal
    conditions. A short 'What to watch' box is itself information: it
    means nothing is out of line.
    """
    sig = []
    # Coverage/data-integrity caveats that must survive the 5-signal cap: a
    # whole origin going dark is more important to surface than any single
    # market move, so these are floated above `sig` at the return.
    coverage_sig = []
    # The ENSO forward signal ranks just below coverage caveats and above the
    # market signals, so a firing advisory is not pushed out of the cap.
    enso_sig = []

    # --- Thresholds (tune these if the box feels too noisy/quiet) ---
    PORTS_WOW = 20          # % week-over-week swing in seaport arrivals
    # FOB: flagged only outside the 25-75 percentile band (see below)

    # 1. NO total-supply "vs seasonal norm" signal.
    # A threshold on the deviation from a multi-year seasonal average cannot work
    # as an anomaly detector for VOLUME, because volume trends: arrivals have
    # grown ~15%/yr, so recent weeks sit structurally above a baseline dragged
    # down by earlier years. Backtested across 2023-2026 the rolling-4 deviation
    # fired one-sided — 67% positive overall, 100% positive in 2026 — at every
    # threshold tried (5/7/10), and neither a prior-year-only nor a trend-fitted
    # baseline removed the lean (both still ~65% positive; a genuine surge year
    # beats any cross-year reference). It degrades to an always-on "above average"
    # light, not a signal. The rolling-4 level still appears on the KPI tile as
    # descriptive context; it just does not gate a "what to watch" line. Price is
    # different (it mean-reverts) — see the seasonal-band note in build_pricing —
    # so the FOB percentile-band signal below IS a valid anomaly detector.
    #
    # A momentum variant WAS prototyped: rolling-4 week-over-week, gated by the
    # same-week-seasonal percentile (reusing _wow_history, like the price signal).
    # It killed the level bias (firing ~3.6/yr, two-sided) but is NOT shippable
    # yet, for two empirical reasons found in backtest:
    #   1. n~=2. The same-week gate has only ~2 prior seasons of rolling history,
    #      so every "percentile" is a choice between two numbers, not discriminating.
    #   2. Firings cluster on wk4-5, wk30-31, wk43-44, wk50 -- the Mexico January
    #      restart, July ramp, October and December transitions. Those are exactly
    #      the predictable seasonal inflections the gate exists to SUPPRESS, and at
    #      n~=2 it cannot. (A residual 72% up-skew is real supply dynamics -- sharp
    #      ramps, gradual declines -- not a defect, but it reads as a ticker.)
    # REVISIT when there are >=4 prior seasons of rolling-4 history (~2027): only
    # then does the same-week gate have enough samples to tell a genuine anomaly
    # from the annual restart ramp. Backtest was scratchpad-only, not committed.

    # 1b. Week-over-week magnitude — an unusually large move for THIS week of
    # the season. Gated on both a per-series floor and the 90th percentile of
    # same-week history, so predictable seasonal ramps (Jan restart, etc.) do
    # not fire. Report at most the two largest to avoid flooding the box.
    moves = []
    for key, label, wow, series, week in _wow_candidates(supply, pricing):
        if wow is None or week is None:
            continue                      # partial regions already null their wow
        floor = WOW_FLOORS.get(key)
        if floor is None or abs(wow) < floor:
            continue
        hist = _wow_history(series, week)
        if len(hist) < BASELINE_MIN_SAMPLES:
            continue                      # too thin to judge — say nothing
        rank = _percentile_rank(hist, abs(wow))
        if rank >= WOW_SEASONAL_PCTILE:
            yrs = _wow_history_years(series, week)
            moves.append((abs(wow), key, label, wow, rank, yrs))

    regions_by_key = {r["key"]: r for r in (supply or {}).get("regions", [])}
    supply_years = (supply or {}).get("baseline_years", 3)
    bm = (pricing or {}).get("benchmark") or {}
    for _, key, label, wow, rank, yrs in sorted(moves, reverse=True)[:MAX_WOW_SIGNALS]:
        direction = "jumped" if wow > 0 else "dropped"
        # State the sample plainly instead of a percentile: with ~20 autocorrelated
        # points the effective n is ~4-5, so "100th percentile" overstates rarity.
        rank_phrase = (f"the largest same-week move in {yrs} seasons of history"
                       if rank >= 95
                       else f"among the largest same-week moves in {yrs} seasons")

        # Current level: volume for supply regions and the imports aggregate,
        # price for the FOB benchmark.
        r = regions_by_key.get(key)
        imp = (supply or {}).get("imports_agg") if key == "imports" else None
        if r is not None:
            level = f" to {r['lbs'] / 1e6:.1f}M lbs"
        elif imp is not None:
            level = f" to {imp['lbs'] / 1e6:.1f}M lbs"
        elif key == "price_mx" and bm.get("mx_latest") is not None:
            level = f" to ${bm['mx_latest']:.2f}"
        else:
            level = ""

        # Pair the WoW % with the seasonal comparison so the two can't be read in
        # isolation: a big move that lands above norm is a surge; one that lands
        # at norm is a rebound. Only supply regions carry vs_3yr_pct (the FOB
        # benchmark and partial regions don't) — omit the clause when it's null.
        vs = r.get("vs_3yr_pct") if r is not None else None
        if vs is not None:
            seasonal = (f"{abs(vs):.0f}% {'above' if vs > 0 else 'below'} the "
                        f"{supply_years}-year seasonal norm")
            sig.append(f"{label} {direction} {abs(wow):.0f}% week-over-week{level} "
                       f"— {seasonal}, and {rank_phrase}.")
        else:
            sig.append(f"{label} {direction} {abs(wow):.0f}% week-over-week{level} "
                       f"— an unusually large move for this point in the season "
                       f"({rank_phrase}).")

    # 2. Seaport imports — only on a real swing, with season-aware origins. Keyed
    # on the imports AGGREGATE (Peru + Colombia + Chile + other), not a single line.
    imp = (supply or {}).get("imports_agg")
    if imp and imp["lbs"] > 0 and imp.get("wow_pct") is not None:
        w = imp["wow_pct"]
        if abs(w) >= PORTS_WOW:
            origins = active_import_origins()
            who = f" ({origins})" if origins else ""
            sig.append(f"Seaport imports{who} moved "
                       f"{direction_word(w)} to {imp['lbs'] / 1e6:.1f}M lbs — "
                       "watch East Coast spot pressure.")

    # 3. Benchmark FOB — only when genuinely cheap or expensive for the week
    bm = (pricing or {}).get("benchmark") or {}
    if bm.get("band_position_pct") is not None:
        p = bm["band_position_pct"]
        yrs_p = bm.get("baseline_years", 3)
        if p <= 5:
            sig.append(f"Benchmark Hass 48s FOB is below the 5th percentile of its "
                       f"{yrs_p}-year seasonal history — historically cheap for this week.")
        elif p >= 95:
            sig.append(f"Benchmark Hass 48s FOB is above the 95th percentile of its "
                       f"{yrs_p}-year seasonal history — historically expensive for this week.")
        elif p >= 75:
            sig.append(f"Benchmark Hass 48s FOB is at the upper edge of its {yrs_p}-year "
                       f"seasonal 25th–75th band — firm for this week.")
        elif p < 25:
            # Strictly below the 25th percentile: the price is UNDER the band, not
            # sitting on its lower edge — the chart line dips beneath the shaded band.
            sig.append(f"Benchmark Hass 48s FOB is below its {yrs_p}-year seasonal "
                       f"25th–75th band — softer than three-quarters of same-week readings.")
        elif p <= 25:
            # Exactly at the 25th: sitting on the lower edge, inside the band.
            sig.append(f"Benchmark Hass 48s FOB is at the lower edge of its {yrs_p}-year "
                       f"seasonal 25th–75th band — soft for this week.")
        # 26-74 = unremarkable, say nothing

    # 4. Truck shortages — scoped to origin; check whether lane rates agree
    shortages = [a for a in (freight or {}).get("availability", [])
                 if "Shortage" in a["status"]]
    if shortages:
        districts = ", ".join(a["district"] for a in shortages)
        lanes = [l for l in (freight or {}).get("lanes", [])
                 if l.get("wow_pct") is not None]
        softening = [l for l in lanes if l["wow_pct"] < -1]
        if softening and len(softening) >= len(lanes) / 2:
            sig.append(f"Truck availability is tight out of {districts}, but quoted lane "
                       "rates softened this week — capacity pressure has not yet reached "
                       "spot rates. Watch for a lag.")
        else:
            sig.append(f"Truck availability tight out of {districts} — "
                       "expect upward rate pressure on lanes from this origin.")

    # 4b. Fallback lane — when S. Texas has no quote, the card flips origin silently
    fallback_lanes = [l for l in (freight or {}).get("lanes", [])
                      if not l.get("origin_is_preferred")]
    if fallback_lanes:
        names = ", ".join(f"{l['dest']} (from {l['origin_short']})" for l in fallback_lanes)
        sig.append(f"No S. Texas lane quoted into {names} this week — "
                   "rates shown are from an alternate origin and are not "
                   "comparable to prior weeks.")

    # 5. Freight data staleness — if the USDA feed is behind, say so here too
    sd = (freight or {}).get("stale_days")
    fe = (freight or {}).get("fetch_error")
    if fe:
        sig.append("Freight rates could not be refreshed from USDA this week — "
                   "lane costs shown are carried over from the last successful "
                   "fetch; treat as indicative.")
    elif sd is not None and sd > FREIGHT_STALE_AFTER_DAYS:
        sig.append(f"Freight rates shown are {sd} days old — the USDA truck "
                   "rate report has not refreshed; treat lane costs as indicative.")

    # 6. Feed freshness — warn when individual feeds missed a cycle
    if feeds:
        named = {k: v for k, v in feeds.items() if not k.startswith("_")}
        # freight has its own staleness line above; enso/enso_weekly are surfaced
        # in the ENSO panel with their own clocks, so none belong in this
        # weekly-cadence warning.
        lagging = [n for n, f in named.items()
                   if f["stale"] and n not in ("freight", "enso", "enso_weekly")]
        if lagging:
            display = ["USDA" if n in ("supply", "pricing") else n for n in lagging]
            sig.append(f"Data feeds not refreshed this cycle: {', '.join(sorted(set(display)))} "
                       "— figures from these sources may not reflect the current week.")

    # 6b. Weather origin-group outage — a whole origin country went dark. Which
    # country matters, not the count, so the message is country-specific.
    # Non-fatal (external outage isn't a code bug), but stated plainly so a
    # fresh timestamp isn't mistaken for full coverage. This is also what the
    # ENSO panel keys on to caveat origins it can no longer see.
    cov = (weather or {}).get("coverage") or {}
    for c in (weather or {}).get("dark_groups") or []:
        label = COUNTRY_LABELS.get(c, c)
        framing = WEATHER_DARK_FRAMING.get(c, "weather for this origin is unavailable this cycle.")
        coverage_sig.append(f"No live weather for {label} this cycle "
                            f"({cov.get(c, '0/0')} regions returned data) — {framing}")

    # 7. Weather — already event-driven, left as-is
    for r in (weather or {}).get("regions", []):
        if r.get("flag") in ("watch", "alert"):
            sig.append(f"{r['name']}: {r['note']}")

    # 8. ENSO — seasonal-to-annual forward signal. Leads with trend/direction
    # (not the band, which understates a fast ramp), then a near-term slot and a
    # forward slot that answer different questions (fruit on the tree now vs the
    # crop being set for 2027-28). Caveats any origin whose live weather is dark.
    if enso and enso.get("signal_fire"):
        def _seg(label, slot):
            if not slot:
                return None
            s = f"{label}: {slot['name'].split(' (')[0]} — {slot.get('signal_effect', '')}"
            if slot.get("weather_dark"):
                s += (f" (no live {COUNTRY_LABELS.get(slot['country'], slot['country'])} "
                      "weather this cycle to corroborate)")
            return s
        # Lead with the current WEEKLY central-Pacific reading, then the seasonal
        # classification as context — the same ordering the panel uses. The
        # seasonal value averages three months and understates a fast ramp, so it
        # must not open the signal. Falls back to the seasonal headline if the
        # weekly SST reading is unavailable.
        cp = enso.get("central_pacific_weekly")
        if cp and cp.get("anom") is not None:
            wk = cp.get("week")
            wtxt = (f" (week of {datetime.fromisoformat(wk).strftime('%b %d')})"
                    if wk else "")
            phase = enso["latest"]["phase"]
            ref3 = enso.get("three_seasons_ago")
            up = f", up from {ref3['anom']:+.2f} three seasons ago" if ref3 else ""
            lead = (f"Central Pacific ocean temperatures are {cp['anom']:+.1f}°C "
                    f"above normal{wtxt}. The three-month average reads {phase} at "
                    f"{enso['anom']:+.2f} for {enso['season']} and is "
                    f"{enso.get('trend', 'steady')}{up}")
        else:
            lead = enso["headline"]
        if enso.get("lag_caveat"):
            lead += f"; {enso['lag_caveat']}"
        segs = [s for s in (_seg("Near-term", enso.get("near_term")),
                            _seg("Forward", enso.get("forward"))) if s]
        enso_sig.append(f"{lead}. " + " ".join(f"{s}." for s in segs)
                        if segs else f"{lead}.")

    # 8b. Peru coastal flood signal — Lambayeque ONLY, gated to Oct-Mar. Plain
    # language for a produce reader (no ocean-index jargon). Two independent
    # triggers: the coast reaching the absolute flood level, or warming
    # concentrated on the coast (the 2017/2023 pattern that flooded with the
    # central Pacific near neutral). The text names which trigger fired, states
    # conditions-present not a forecast, and never implies a repeat of 1998.
    n12 = (enso or {}).get("nino12")
    if n12 and n12.get("fire"):
        coast = n12["latest"]["nino12_anom"]
        if n12.get("fire_absolute"):
            near = " — near the warmest in 45 years of records" if n12.get("near_record") else ""
            txt = (f"Ocean temperatures off northern Peru are {coast:.1f}°C above "
                   f"normal{near}. Warm coastal water at this level preceded the "
                   f"severe flooding in Lambayeque and Piura in 1998, 2017 and 2023.")
            if n12.get("fire_divergence"):
                txt += (" The warming is concentrated on the Peru coast rather than "
                        "the central Pacific, the pattern behind those events.")
        else:  # divergence-only trigger
            txt = ("Ocean warming is concentrated off the Peru coast rather than the "
                   "central Pacific — the pattern that produced coastal flooding in "
                   "2017 and 2023 without a strong El Niño elsewhere.")
        txt += (" Watch the January–March rain window for field access and "
                "packhouse logistics.")
        if n12.get("stale"):
            txt += " (Coastal reading carried over from a prior update.)"
        enso_sig.append(txt)

    # Priority order for the 5-signal cap: coverage caveats first, then the ENSO
    # forward + coastal signals, then the market signals.
    ordered = coverage_sig + enso_sig + sig

    # Quiet week: say so explicitly rather than showing an empty box
    if not ordered:
        ordered.append("No notable deviations this week — supply, pricing, and "
                       "freight are all tracking near seasonal norms.")

    return ordered[:5]


def build_kpis(supply, pricing, freight, diesel) -> list:
    kpis = []
    mx = next((r for r in (supply or {}).get("regions", []) if r["key"] == "mx"), None)
    if mx:
        kpis.append({"label": "Mexico volume", "value": f"{mx['lbs'] / 1e6:.1f}M lbs",
                     "delta_pct": mx["wow_pct"], "sub": "vs prior week"})
    if supply and supply.get("total_roll4_lbs") is not None:
        # The VALUE is the rolling-4 LEVEL (a volume), not a percentage — so the
        # tile can't be misread as a weekly event. The "+X% vs N-yr avg" lives in
        # the sub as plain descriptive context: volume trends (arrivals grow ~15%/
        # yr), so a positive reading here reflects category growth against a lagging
        # multi-year mean, NOT an anomaly. That is why this is a level, not a
        # signal (see build_signals section 1). % is derived from the shown 0.1M
        # figures so it reconciles with a division of the displayed numbers.
        excl = supply.get("total_excludes") or []
        excl_note = f" (excl. {', '.join(e.upper() for e in excl)} — pending)" if excl else ""
        rt = supply["total_roll4_lbs"] / 1e6
        vs = supply.get("total_roll4_vs_baseline_pct")
        rb = supply.get("total_roll4_baseline_lbs")
        if vs is not None and rb:
            disp = round((round(rt, 1) / round(rb / 1e6, 1) - 1) * 100)
            yrs = supply.get("baseline_years", 3)
            sub = f"{disp:+.0f}% vs {yrs}-yr seasonal avg{excl_note}"
        else:
            sub = excl_note.strip() or "vs prior seasons"
        kpis.append({
            "label": "Arrivals · trailing 4 wk",
            "value": f"{rt:.1f}M lbs",
            "delta_pct": None,
            "sub": sub,
        })
    bm = (pricing or {}).get("benchmark") or {}
    if bm.get("mx_latest") is not None:
        # Weekly benchmark — stamp the week-ending date so it's not conflated with
        # the daily size/grade table (a few days later, different figure).
        wk = bm.get("week")
        wk_note = (datetime.fromisoformat(wk).strftime("wk ending %b %d") + " · "
                   if wk else "")
        kpis.append({"label": "Hass 48s FOB (TX)", "value": f"${bm['mx_latest']:.2f}",
                     "delta_pct": bm.get("wow_mx_pct"), "sub": f"{wk_note}vs prior week"})
    la = next((l for l in (freight or {}).get("lanes", []) if l["dest"] == "Los Angeles"), None)
    if la:
        origin_label = la["origin_short"].split(" (")[0]
        # The delta is the LANE RATE change, not a crossings-volume move. Bind it
        # to "vs prior wk" and prefix the origin with "from" so the origin text
        # can't be misread as "3% crossings".
        kpis.append({"label": f"Freight: {origin_label} → LA",
                     "value": f"${la['low']:,}–{la['high']:,}",
                     "delta_pct": la["wow_pct"] or None,
                     "sub": f"vs prior wk · from {la['origin_short']}"})
    nat = ((diesel or {}).get("latest") or {}).get("national")
    if nat:
        kpis.append({"label": "US diesel", "value": f"${nat['value']:.2f}/gal",
                     "delta_pct": round(nat["wow"] / (nat["value"] - nat["wow"]) * 100, 1)
                                  if nat["value"] != nat["wow"] else None,
                     "sub": "weekly retail"})
    return kpis


# ---------------------------------------------------------------

def main():
    notes = []
    movement = load(HIST / "movement_weekly.json", {})
    price_hist = load(HIST / "pricing_weekly.json", {})
    current = load(RAW / "usda_current.json", {}) or {}
    freight_raw = load(RAW / "freight.json", {})
    diesel_raw = load(RAW / "diesel.json", {})
    weather_raw = load(RAW / "weather.json", {})
    enso_raw = load(RAW / "enso.json", {})
    enso_response = load(ROOT / "enso_response.json", {})

    supply = build_supply(movement, notes)
    pricing = build_pricing(price_hist, current, notes)
    freight = build_freight(freight_raw, notes)
    freight["stale_days"] = freight_stale_days(freight.get("report_date"))
    freight["fetch_error"] = freight_raw.get("fetch_error")
    freight["stale_after_days"] = FREIGHT_STALE_AFTER_DAYS
    diesel = build_diesel(diesel_raw)
    weather = build_weather(weather_raw)
    enso = build_enso(enso_raw, enso_response, weather)

    # Build feeds freshness block before signals (signals reads it)
    feeds = {}
    for name, raw in (("supply", current), ("pricing", current),
                      ("freight", freight_raw), ("diesel", diesel_raw),
                      ("weather", weather_raw)):
        age = fetch_age_days(raw.get("fetched_at"))
        feeds[name] = {
            "fetched_at": raw.get("fetched_at"),
            "fetch_age_days": age,
            "stale": age is None or age > FETCH_STALE_AFTER_DAYS,
            "fetch_error": raw.get("fetch_error"),
        }
    feeds["_stale_after_days"] = FETCH_STALE_AFTER_DAYS

    # ENSO is monthly: judge it on its own 45-day clock, and keep it OUT of the
    # all-stale build-failure check (a fresh ENSO feed must not mask dead weekly
    # feeds, and a stale monthly feed must not by itself fail the run).
    enso_age = fetch_age_days(enso_raw.get("fetched_at"))
    feeds["enso"] = {
        "fetched_at": enso_raw.get("fetched_at"),
        "fetch_age_days": enso_age,
        "stale": enso_age is None or enso_age > ENSO_STALE_AFTER_DAYS,
        "fetch_error": enso_raw.get("fetch_error"),
        "stale_after_days": ENSO_STALE_AFTER_DAYS,
    }

    # Niño 1+2 is WEEKLY (same fetch as RONI/ONI, but its data cadence is weekly),
    # so it gets a tighter 14-day clock than seasonal ENSO. Like enso, it stays OUT
    # of the all-stale failure check and the weekly-feed-freshness signal (it has
    # its own panel line). fetch_error keys on the weekly-SST-specific error.
    feeds["enso_weekly"] = {
        "fetched_at": enso_raw.get("fetched_at"),
        "fetch_age_days": enso_age,
        "stale": enso_age is None or enso_age > ENSO_WEEKLY_STALE_AFTER_DAYS,
        "fetch_error": enso_raw.get("nino12_error"),
        "stale_after_days": ENSO_WEEKLY_STALE_AFTER_DAYS,
    }

    week_end = supply.get("week_end")
    label = (datetime.fromisoformat(week_end).strftime("Week ending %b %d, %Y")
             if week_end else "—")

    # The price WoW signal needs the full weekly benchmark series exposed on
    # pricing; the front end does not. Build signals first, then strip it so it
    # doesn't balloon data.json with years of history it never reads.
    signals = build_signals(supply, pricing, freight, diesel, weather, feeds, enso)
    pricing.pop("mx_series", None)

    data = {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "week": {"end": week_end, "label": label},
        "headline": build_headline(supply, pricing, freight, diesel, weather),
        "headline_asof": build_headline_asof(supply, pricing, freight, diesel),
        "signals": signals,
        "kpis": build_kpis(supply, pricing, freight, diesel),
        "feeds": feeds,
        "supply": supply,
        "pricing": pricing,
        "freight": freight,
        "diesel": diesel,
        "weather": weather,
        "enso": enso,
        "meta": {
            "notes": notes,
            "sources": [
                {"name": "USDA AMS Market News (MARS API)", "url": "https://mymarketnews.ams.usda.gov/"},
                {"name": "USDA AMS FVWTRK Truck Rate Report", "url": "https://www.ams.usda.gov/mnreports/fvwtrk.pdf"},
                {"name": "EIA Weekly Retail Diesel", "url": "https://www.eia.gov/petroleum/gasdiesel/"},
                {"name": "NOAA/NWS + Open-Meteo", "url": "https://www.weather.gov/"},
                {"name": "NOAA CPC ENSO indices (RONI/ONI)", "url": "https://www.cpc.ncep.noaa.gov/data/indices/"},
            ],
        },
    }

    (ROOT / "data.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    print("build_summary: wrote data.json")
    print("HEADLINE:", data["headline"])
    for n in notes:
        print("NOTE:", n)

    # Season check — fail the Action if a region that should be
    # reporting has gone dark (USDA slug change, broken parser, etc.)
    season_blocks = {r["key"]: r["season"] for r in supply.get("regions", [])}
    gaps = any_unexpected_gaps(season_blocks)
    for key, block in season_blocks.items():
        if block["status"] != "active":
            print(f"SEASON [{block['status']}] {key}: {block['message']}")
    # Freight freshness
    fe = freight.get("fetch_error")
    sd = freight.get("stale_days")
    freight_stale = sd is not None and sd > FREIGHT_STALE_AFTER_DAYS
    if fe:
        print(f"FREIGHT [error] {fe}")
    if freight_stale:
        print(f"FREIGHT [stale] report is {sd} days old ({freight.get('report_date')})")

    # All-feeds freshness — fail only when every weekly feed has gone dark.
    # ENSO (monthly) and its weekly Niño 1+2 companion are excluded: both have
    # their own clocks and their own panel, so neither must mask dead weekly
    # feeds nor fail the run on its own.
    named_feeds = {k: v for k, v in feeds.items()
                   if not k.startswith("_") and k not in ("enso", "enso_weekly")}
    all_stale = all(f["stale"] for f in named_feeds.values())
    if all_stale:
        print("FEEDS [error] every feed is stale — automation appears to have stopped")

    if gaps or fe or freight_stale or all_stale:
        raise SystemExit(1)

if __name__ == "__main__":
    main()
