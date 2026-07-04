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
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
RAW = ROOT / "data" / "raw"
HIST = ROOT / "history"

TREND_WEEKS = 52

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

    def seasonal_avg(week_str, series_fn):
        """3-yr average of same ISO week from prior seasons."""
        wn = iso_week(week_str)
        cutoff = date.fromisoformat(week_str) - timedelta(days=180)
        vals = [series_fn(w) for w in weeks
                if iso_week(w) == wn and date.fromisoformat(w) < cutoff]
        return statistics.mean(vals) if vals else None

    trend = []
    for w in weeks[-TREND_WEEKS:]:
        trend.append({
            "week": w,
            "mx": weekly[w]["mx"],
            "ca": weekly[w]["ca"],
            "ports": weekly[w]["ports"],
            "avg3yr": round(seasonal_avg(w, total)) if seasonal_avg(w, total) else None,
        })

    # USDA posts some districts late (CA domestic movement especially), so
    # the newest week can be a fraction of the true total. Flag a region as
    # partial when it prints far below its own trailing median, and keep it
    # out of the comparisons instead of headlining a phantom collapse.
    def trailing_median(key):
        prior_vals = [weekly[w][key] for w in weeks[-5:-1]]
        return statistics.median(prior_vals) if prior_vals else 0

    partial_keys = set()
    regions = []
    for key, name in (("mx", "Mexico crossings"),
                      ("ca", "California"),
                      ("ports", "Seaport/other imports")):
        cur = weekly[cur_w][key]
        pri = weekly[prior_w][key] if prior_w else 0
        avg = seasonal_avg(cur_w, lambda w, k=key: weekly[w][k])
        med = trailing_median(key)
        partial = med > 1e6 and cur < 0.3 * med
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

    avg_rel = seasonal_avg(cur_w, total_reliable)
    return {
        "week_end": cur_w,
        "total_lbs": total(cur_w),
        "total_wow_pct": pct(total_reliable(cur_w), total_reliable(prior_w))
                         if prior_w else None,
        "total_vs_3yr_pct": pct(total_reliable(cur_w), avg_rel) if avg_rel else None,
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

    def band(week_str):
        wn = iso_week(week_str)
        cutoff = date.fromisoformat(week_str) - timedelta(days=180)
        vals = [v for w, v in mx.items()
                if iso_week(w) == wn and date.fromisoformat(w) < cutoff]
        if not vals:
            return None, None
        return min(vals), max(vals)

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
    b_lo, b_hi = band(mx_weeks[-1]) if mx_weeks else (None, None)
    band_position = None
    if latest_mx is not None and b_lo is not None and b_hi and b_hi > b_lo:
        band_position = round((latest_mx - b_lo) / (b_hi - b_lo) * 100)

    return {
        "benchmark": {
            "label": "Hass 48s, 2-layer cartons (conventional), FOB/shipping point",
            "mx_latest": latest_mx, "ca_latest": ca[ca_weeks[-1]] if ca_weeks else None,
            "wow_mx_pct": wow_mx, "wow_ca_pct": wow_ca,
            "band_position_pct": band_position,
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
                         "origin_short": origin_short, **{k: r[k] for k in
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


def build_weather(weather: dict) -> dict:
    if not weather:
        return {"overall_flag": "unknown", "regions": []}
    regions = weather.get("regions", [])
    overall = max(regions, key=lambda r: FLAG_RANK.get(r.get("flag"), 0),
                  default=None)
    return {"overall_flag": overall["flag"] if overall else "unknown",
            "regions": regions}


# ---------------------------------------------------------------
# Narrative + signals
# ---------------------------------------------------------------

def direction_word(p, up="up", down="down", flat="flat"):
    if p is None:
        return flat
    if p > 1:
        return f"{up} {abs(p):.0f}%"
    if p < -1:
        return f"{down} {abs(p):.0f}%"
    return flat


def build_headline(supply, pricing, freight, diesel, weather) -> str:
    parts = []
    if supply:
        parts.append(f"Mexico crossing volume "
                     f"{direction_word(next((r['wow_pct'] for r in supply['regions'] if r['key'] == 'mx'), None))} week-over-week")
    bm = (pricing or {}).get("benchmark") or {}
    if bm.get("mx_latest") is not None:
        w = bm.get("wow_mx_pct")
        verb = "steady" if w is None or abs(w) <= 1 else ("firmed" if w > 0 else "softened")
        move = "" if verb == "steady" else f" {abs(w):.0f}%"
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


def build_signals(supply, pricing, freight, diesel, weather) -> list:
    sig = []
    if supply and supply.get("total_vs_3yr_pct") is not None:
        v = supply["total_vs_3yr_pct"]
        sig.append(f"Total arrivals are {direction_word(v, 'running', 'running')} "
                   f"{'above' if v > 0 else 'below'} the 3-year seasonal average "
                   f"({supply['total_lbs'] / 1e6:.1f}M lbs this week).")
    ports = next((r for r in (supply or {}).get("regions", []) if r["key"] == "ports"), None)
    if ports and ports["lbs"] > 0 and ports.get("wow_pct") is not None:
        sig.append(f"Seaport imports (Peru/Colombia/DR season) moved "
                   f"{direction_word(ports['wow_pct'])} to {ports['lbs'] / 1e6:.1f}M lbs — "
                   "watch East Coast spot pressure.")
    bm = (pricing or {}).get("benchmark") or {}
    if bm.get("band_position_pct") is not None:
        p = bm["band_position_pct"]
        if p < 0:
            sig.append("Benchmark Hass 48s FOB is trading BELOW its 3-year "
                       "seasonal range — historically cheap for this week.")
        elif p > 100:
            sig.append("Benchmark Hass 48s FOB is trading ABOVE its 3-year "
                       "seasonal range — historically expensive for this week.")
        else:
            where = ("near the top of" if p >= 75
                     else "near the bottom of" if p <= 25 else "inside")
            sig.append(f"Benchmark Hass 48s FOB sits {where} its 3-year seasonal "
                       f"range ({p}th percentile of the band).")
    shortages = [a for a in (freight or {}).get("availability", [])
                 if "Shortage" in a["status"]]
    if shortages:
        sig.append("Truck availability tight out of " +
                   ", ".join(a["district"] for a in shortages) +
                   " — expect upward rate pressure.")
    for r in (weather or {}).get("regions", []):
        if r.get("flag") in ("watch", "alert"):
            sig.append(f"{r['name']}: {r['note']}")
    return sig[:5]


def build_kpis(supply, pricing, freight, diesel) -> list:
    kpis = []
    mx = next((r for r in (supply or {}).get("regions", []) if r["key"] == "mx"), None)
    if mx:
        kpis.append({"label": "MX crossing volume", "value": f"{mx['lbs'] / 1e6:.1f}M lbs",
                     "delta_pct": mx["wow_pct"], "sub": "vs prior week"})
    if supply and supply.get("total_vs_3yr_pct") is not None:
        kpis.append({"label": "Total vs 3-yr avg", "value": f"{supply['total_vs_3yr_pct']:+.0f}%",
                     "delta_pct": None, "sub": "seasonal pace"})
    bm = (pricing or {}).get("benchmark") or {}
    if bm.get("mx_latest") is not None:
        kpis.append({"label": "Hass 48s FOB (TX)", "value": f"${bm['mx_latest']:.2f}",
                     "delta_pct": bm.get("wow_mx_pct"), "sub": "vs prior week"})
    la = next((l for l in (freight or {}).get("lanes", []) if l["dest"] == "Los Angeles"), None)
    if la:
        kpis.append({"label": "Freight → LA", "value": f"${la['low']:,}–{la['high']:,}",
                     "delta_pct": la["wow_pct"] or None, "sub": la["origin_short"]})
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

    supply = build_supply(movement, notes)
    pricing = build_pricing(price_hist, current, notes)
    freight = build_freight(freight_raw, notes)
    diesel = build_diesel(diesel_raw)
    weather = build_weather(weather_raw)

    week_end = supply.get("week_end")
    label = (datetime.fromisoformat(week_end).strftime("Week ending %b %d, %Y")
             if week_end else "—")

    data = {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "week": {"end": week_end, "label": label},
        "headline": build_headline(supply, pricing, freight, diesel, weather),
        "signals": build_signals(supply, pricing, freight, diesel, weather),
        "kpis": build_kpis(supply, pricing, freight, diesel),
        "supply": supply,
        "pricing": pricing,
        "freight": freight,
        "diesel": diesel,
        "weather": weather,
        "meta": {
            "notes": notes,
            "sources": [
                {"name": "USDA AMS Market News (MARS API)", "url": "https://mymarketnews.ams.usda.gov/"},
                {"name": "USDA AMS FVWTRK Truck Rate Report", "url": "https://www.ams.usda.gov/mnreports/fvwtrk.pdf"},
                {"name": "EIA Weekly Retail Diesel", "url": "https://www.eia.gov/petroleum/gasdiesel/"},
                {"name": "NOAA/NWS + Open-Meteo", "url": "https://www.weather.gov/"},
            ],
        },
    }

    (ROOT / "data.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    print("build_summary: wrote data.json")
    print("HEADLINE:", data["headline"])
    for n in notes:
        print("NOTE:", n)


if __name__ == "__main__":
    main()
