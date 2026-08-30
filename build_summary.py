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
# large (>= the per-series floor below). Floors were calibrated against the
# committed histories, not guessed: each sits near the series' own 90-95th
# percentile of abs(week-over-week %). The mx floor is deliberately just below
# the 90th percentile so genuinely large late-season moves still clear it.
WOW_SEASONAL_PCTILE = 90        # move must rank this high among same-week history
WOW_FLOORS = {                  # ...AND exceed this absolute magnitude (%)
    "price_mx": 12.0,   # ~90th pct of abs(wow); pinned <=21.8 by 2026-08-22
    "mx": 35.0,         # pinned <=36.6 so the current week's drop clears it
    "ports": 90.0,      # high on purpose: the seaport swing signal already
                        #   covers moves >=20%, so only flag extreme moves
    # California is intentionally excluded: its week-over-week % explodes off a
    # near-zero base during season transitions (+400% to +1700%), which is a
    # low-base artifact, not tradable intelligence. See _wow_candidates.
}
MAX_WOW_SIGNALS = 2             # report at most the N largest; do not flood the box

FREIGHT_STALE_AFTER_DAYS = 10   # report is weekly; 10 days = missed a cycle
FETCH_STALE_AFTER_DAYS = 8      # weekly Action; 8 days = missed a cycle
ENSO_STALE_AFTER_DAYS = 45      # ENSO is monthly — do not judge it on a weekly clock

# ENSO signal gating. The band alone is not enough: RONI can read "weak" during
# a fast ramp that CPC is already issuing an advisory for, so we also fire on an
# established event or a steep recent move.
ENSO_MODERATE_ANOM = 1.0        # |RONI| at/above this is moderate+ on its own
ENSO_STEEP_DELTA = 0.75         # 3-season change past this = fast move / lagging mean


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


def region_of(district: str) -> str:
    if district.startswith("MEXICO CROSSINGS"):
        return "mx"
    if "CALIFORNIA" in district:
        return "ca"
    return "ports"


def pct(new, old):
    if not old:
        return None
    return round((new - old) / old * 100, 1)


def mid(row) -> float | None:
    for lo_k, hi_k in (("mostly_low", "mostly_high"), ("low", "high")):
        lo, hi = row.get(lo_k), row.get(hi_k)
        if lo is not None and hi is not None:
            return (float(lo) + float(hi)) / 2
    return None


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
    weekly = {}      # week_end -> {mx, ca, ports}
    by_district = {} # week_end -> {district: lbs}
    for key, lbs in movement.items():
        week, district, _origin = key.split("|", 2)
        weekly.setdefault(week, {"mx": 0, "ca": 0, "ports": 0})
        weekly[week][region_of(district)] += lbs
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

    trend = []
    for w in weeks[-TREND_WEEKS:]:
        _avg = seasonal_avg(w, total)
        trend.append({
            "week": w,
            "mx": weekly[w]["mx"],
            "ca": weekly[w]["ca"],
            "ports": weekly[w]["ports"],
            "avg3yr": round(_avg) if _avg else None,
        })

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
    for key, name in (("mx", "Mexico crossings"),
                      ("ca", "California"),
                      ("ports", "Seaport/other imports")):
        cur = weekly[cur_w][key]
        pri = weekly[prior_w][key] if prior_w else 0
        avg = seasonal_avg(cur_w, lambda w, k=key: weekly[w][k])
        med = trailing_median(key)
        season = classify(key, last_reported_week(key))

        # An out-of-season region reporting near zero is a KNOWN zero, not a
        # missing report. Only an in-season region can be "partially reported".
        looks_low = med > 1e6 and cur < 0.3 * med
        partial = looks_low and season["status"] != "out_of_season"

        if partial:
            partial_keys.add(key)
            notes.append(f"{name} movement for the latest week appears "
                         "partially reported by USDA; week-over-week and "
                         "seasonal comparisons suppressed until revised.")
        regions.append({
            "key": key, "name": name, "lbs": cur,
            "partial": partial,
            "wow_pct": None if partial else pct(cur, pri),
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
        return sum(weekly[w][k] for k in ("mx", "ca", "ports")
                   if k not in partial_keys)

    rel_sample = seasonal_sample(cur_w, total_reliable)
    avg_rel = statistics.mean(rel_sample) if rel_sample else None
    baseline_n = len(rel_sample)
    wn_cur = iso_week(cur_w)
    cutoff_cur = date.fromisoformat(cur_w) - timedelta(days=RECENCY_CUTOFF_DAYS)
    baseline_years = len({date.fromisoformat(w).year for w in weeks
                          if _iso_week_distance(iso_week(w), wn_cur) <= SEASONAL_WINDOW_WEEKS
                          and date.fromisoformat(w) < cutoff_cur})
    excluded = sorted(partial_keys)
    # Per-region weekly series {week: lbs}, exposed for the week-over-week
    # magnitude signal so it reuses these totals instead of re-parsing history.
    region_wow_series = {
        k: {w: weekly[w][k] for w in weeks}
        for k in ("mx", "ports")
    }
    return {
        "week_end": cur_w,
        "series": region_wow_series,
        "total_lbs": total_reliable(cur_w),
        "total_lbs_all_regions": total(cur_w),
        "total_excludes": excluded,
        "total_wow_pct": pct(total_reliable(cur_w), total_reliable(prior_w))
                         if prior_w else None,
        "total_vs_3yr_pct": pct(total_reliable(cur_w), avg_rel) if avg_rel else None,
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
    for row in price_hist.values():
        if not is_hass_benchmark(row):
            continue
        m = mid(row)
        if m is None:
            continue
        series.setdefault(row["district"], {})[row["week_end"]] = m

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


def _enso_rank(origins: list) -> list:
    """Amendment 5: prefer origins whose current stage intersects their ENSO
    watch window; dedupe by country (so two Colombia entries don't crowd out
    Peru), sort by confidence, cap at two. If none intersect — e.g. because the
    only in-window origin (Michoacán) is unlabelled and has no stage — fall back
    to the single highest-confidence origin so the signal never goes silent."""
    candidates = [o for o in origins if o.get("stage") and o.get("in_watch")]
    best = {}
    for o in candidates:
        r = _confidence_rank(o["confidence"])
        c = o["country"]
        if c not in best or r > best[c][0]:
            best[c] = (r, o)
    ranked = [o for _, o in sorted(best.values(), key=lambda t: -t[0])]
    if ranked:
        return ranked[:2]
    return [max(origins, key=lambda o: _confidence_rank(o["confidence"]))] if origins else []


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
    lead = f"{side} {trend}" if side != "ENSO-neutral" else f"ENSO {trend}"
    up = f", up from {ref3['anom']:+.2f} three seasons ago" if ref3 else ""
    headline = f"{lead} — RONI {anom:+.2f} ({season}){up}"
    # #3 lag caveat only when the recent move is steep.
    lag_caveat = ("the 3-month seasonal mean trails a fast-developing event, so the "
                  "current state is likely stronger than the season label") if steep else None

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

    ranked = _enso_rank(origins)
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
        "phase_with_season": f'{roni["latest"]["phase"]} ({season})',   # #1
        "headline": headline,                                           # #2
        "lag_caveat": lag_caveat,                                       # #3
        "signal_fire": fire,
        "signal_keys": [o["key"] for o in ranked],
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
        parts.append(f"Mexico crossing volume "
                     f"{direction_word(next((r['wow_pct'] for r in supply['regions'] if r['key'] == 'mx'), None), decimals=1)} week-over-week")
    bm = (pricing or {}).get("benchmark") or {}
    if bm.get("mx_latest") is not None:
        w = bm.get("wow_mx_pct")
        verb = "steady" if w is None or abs(w) <= 1 else ("firmed" if w > 0 else "softened")
        move = "" if verb == "steady" else f" {abs(w):.1f}%"
        parts.append(f"Texas-crossing Hass 48s FOB {verb}{move} at ${bm['mx_latest']:.2f}")
    lanes = {l["dest"]: l for l in (freight or {}).get("lanes", [])}
    la, dal = lanes.get("Los Angeles"), lanes.get("Dallas")
    if la and dal:
        def word(l):
            return "firm" if l["wow_pct"] > 1 else ("soft" if l["wow_pct"] < -1 else "flat")
        parts.append(f"LA/Dallas freight {word(la)}/{word(dal)}")
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
    names = {"peru": "Peru", "colombia": "Colombia", "chile": "Chile",
             "dr": "DR", "dominican": "DR"}
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


def _ordinal(n):
    """Turn 23 into '23rd', 11 into '11th', 1 into '1st'."""
    n = int(n)
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"

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


def _wow_candidates(supply, pricing):
    """(key, label, wow_pct, series_dict, week) tuples for the tracked series.

    Covers Mexico crossings, seaport imports, and the Texas-crossing FOB
    benchmark. California is deliberately omitted — its wow% is a low-base
    artifact during season transitions (see WOW_FLOORS).

    wow_pct is already None for anything flagged partial (regions null it,
    build_pricing nulls a thin benchmark), so the caller's `wow is None` guard
    skips partial regions with no special-casing. `week` is each series' own
    latest week, used to centre the same-week history window.
    """
    out = []
    regions = {r["key"]: r for r in (supply or {}).get("regions", [])}
    series = (supply or {}).get("series", {})
    supply_week = (supply or {}).get("week_end")
    for key, label in (("mx", "Mexico crossings"),
                       ("ports", "Seaport/other imports")):
        r = regions.get(key)
        if r is not None and key in series:
            out.append((key, label, r.get("wow_pct"), series[key], supply_week))

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
    SUPPLY_VS_3YR = 10      # % from seasonal average worth mentioning
    PORTS_WOW = 20          # % week-over-week swing in seaport arrivals
    # FOB: flagged only outside the 25-75 percentile band (see below)

    # 1. Supply vs seasonal norm — only when meaningfully off-pace
    if supply and supply.get("total_vs_3yr_pct") is not None:
        v = supply["total_vs_3yr_pct"]
        if abs(v) >= SUPPLY_VS_3YR:
            yrs = supply.get("baseline_years", 3)
            sig.append(f"Total arrivals are running {abs(v):.0f}% "
                       f"{'above' if v > 0 else 'below'} the {yrs}-year seasonal "
                       f"average ({supply['total_lbs'] / 1e6:.1f}M lbs this week).")

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
            moves.append((abs(wow), key, label, wow, rank))

    for _, key, label, wow, rank in sorted(moves, reverse=True)[:MAX_WOW_SIGNALS]:
        direction = "jumped" if wow > 0 else "dropped"
        sig.append(f"{label} {direction} {abs(wow):.0f}% week-over-week — "
                   f"an unusually large move for this point in the season "
                   f"({_ordinal(rank)} percentile of same-week history).")

    # 2. Seaport imports — only on a real swing, with season-aware origins
    ports = next((r for r in (supply or {}).get("regions", [])
                  if r["key"] == "ports"), None)
    if ports and ports["lbs"] > 0 and ports.get("wow_pct") is not None:
        w = ports["wow_pct"]
        if abs(w) >= PORTS_WOW:
            origins = active_import_origins()
            who = f" ({origins})" if origins else ""
            sig.append(f"Seaport imports{who} moved "
                       f"{direction_word(w)} to {ports['lbs'] / 1e6:.1f}M lbs — "
                       "watch East Coast spot pressure.")

    # 3. Benchmark FOB — only when genuinely cheap or expensive for the week
    bm = (pricing or {}).get("benchmark") or {}
    if bm.get("band_position_pct") is not None:
        p = bm["band_position_pct"]
        yrs_p = bm.get("baseline_years", 3)
        if p <= 5:
            sig.append(f"Benchmark Hass 48s FOB is at the very bottom of its {yrs_p}-year "
                       f"seasonal range ({_ordinal(p)} percentile) — historically "
                       f"cheap for this week.")
        elif p >= 95:
            sig.append(f"Benchmark Hass 48s FOB is at the very top of its {yrs_p}-year "
                       f"seasonal range ({_ordinal(p)} percentile) — historically "
                       f"expensive for this week.")
        elif p >= 75:
            sig.append(f"Benchmark Hass 48s FOB is near the top of its {yrs_p}-year "
                       f"seasonal range ({_ordinal(p)} percentile) — firm for this week.")
        elif p <= 25:
            sig.append(f"Benchmark Hass 48s FOB is near the bottom of its {yrs_p}-year "
                       f"seasonal range ({_ordinal(p)} percentile) — soft for this week.")
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
        # freight has its own staleness line above; enso is monthly and surfaced
        # in its own panel, so neither belongs in this weekly-cadence warning.
        lagging = [n for n, f in named.items() if f["stale"] and n not in ("freight", "enso")]
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
    # (not the band, which understates a fast ramp), names the ranked affected
    # origins with their specific effect, and caveats any origin whose live
    # weather is currently dark. One bullet so it doesn't flood the box.
    if enso and enso.get("signal_fire"):
        by_key = {o["key"]: o for o in enso.get("origins", [])}
        parts = []
        for k in enso.get("signal_keys", []):
            o = by_key.get(k)
            if not o:
                continue
            seg = f"{o['name'].split(' (')[0]} — {o.get('signal_effect', '')}"
            if o.get("weather_dark"):
                seg += (f" (no live {COUNTRY_LABELS.get(o['country'], o['country'])} "
                        "weather this cycle to corroborate)")
            parts.append(seg)
        lead = enso["headline"]
        if enso.get("lag_caveat"):
            lead += f"; {enso['lag_caveat']}"
        watch = "; ".join(parts)
        enso_sig.append(f"{lead}. Watch {watch}." if watch else f"{lead}.")

    # Priority order for the 5-signal cap: coverage caveats first, then the ENSO
    # forward signal, then the market signals.
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
        kpis.append({"label": "MX crossing volume", "value": f"{mx['lbs'] / 1e6:.1f}M lbs",
                     "delta_pct": mx["wow_pct"], "sub": "vs prior week"})
    if supply and supply.get("total_vs_3yr_pct") is not None:
        excl = supply.get("total_excludes") or []
        excl_note = f" (excl. {', '.join(e.upper() for e in excl)} — pending)" if excl else ""
        kpis.append({
            "label": f"Total arrivals vs {supply.get('baseline_years', 3)}-yr avg",
            "value": f"{supply['total_vs_3yr_pct']:+.0f}%",
            "delta_pct": None,
            "sub": f"{supply['total_lbs']/1e6:.1f}M lbs this week{excl_note}",
        })
    bm = (pricing or {}).get("benchmark") or {}
    if bm.get("mx_latest") is not None:
        kpis.append({"label": "Hass 48s FOB (TX)", "value": f"${bm['mx_latest']:.2f}",
                     "delta_pct": bm.get("wow_mx_pct"), "sub": "vs prior week"})
    la = next((l for l in (freight or {}).get("lanes", []) if l["dest"] == "Los Angeles"), None)
    if la:
        origin_label = la["origin_short"].split(" (")[0]
        kpis.append({"label": f"Freight: {origin_label} → LA",
                     "value": f"${la['low']:,}–{la['high']:,}",
                     "delta_pct": la["wow_pct"] or None,
                     "sub": la["origin_short"]})
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

    week_end = supply.get("week_end")
    label = (datetime.fromisoformat(week_end).strftime("Week ending %b %d, %Y")
             if week_end else "—")

    # Signals need the full weekly series exposed on supply/pricing; the front
    # end does not. Build signals first, then strip those internal series so
    # they don't balloon data.json with years of history it never reads.
    signals = build_signals(supply, pricing, freight, diesel, weather, feeds, enso)
    supply.pop("series", None)
    pricing.pop("mx_series", None)

    data = {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "week": {"end": week_end, "label": label},
        "headline": build_headline(supply, pricing, freight, diesel, weather),
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
    # ENSO is excluded: it is monthly (its own 45-day clock), so a fresh ENSO
    # feed must not mask dead weekly feeds and a stale one must not fail the run.
    named_feeds = {k: v for k, v in feeds.items()
                   if not k.startswith("_") and k != "enso"}
    all_stale = all(f["stale"] for f in named_feeds.values())
    if all_stale:
        print("FEEDS [error] every feed is stale — automation appears to have stopped")

    if gaps or fe or freight_stale or all_stale:
        raise SystemExit(1)

if __name__ == "__main__":
    main()
