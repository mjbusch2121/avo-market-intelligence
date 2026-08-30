# fetch_weather.py
# ---------------------------------------------------------------
# Growing-region weather, framed as a 2-4 week leading indicator
# for supply.
#
#   Michoacan (Uruapan)      - ~80% of Mexican Hass exports
#   Jalisco (Cd. Guzman)     - #2 Mexican export state
#   Ventura County (Oxnard)  - CA coastal belt
#   San Diego Co (Fallbrook) - CA southern belt
#
# Numbers (past 7 days + next 14 days) come from Open-Meteo, which
# covers Mexico; NOAA/NWS only covers the US, so it contributes the
# forecast narrative text for the two California regions.
#
# Output: data/raw/weather.json
# ---------------------------------------------------------------

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

ROOT = Path(__file__).parent
RAW_DIR = ROOT / "data" / "raw"

# Per-region weather thresholds live in the region config, not as module-level
# constants, so different climates can carry different trigger levels and the
# later climatology project can swap percentile-based values into the same
# slots without touching flag_region().
#
# These numbers are PROVISIONAL — informed guesses calibrated for Michoacán and
# California, pending the rainfall-climatology work that will replace absolute
# mm with seasonal percentiles. The config *structure* is the permanent part.
#
# A threshold set to None disables that rule for the region (e.g. a coastal
# desert site should have no "wet week just ended" rule at all).
#   rain_14d_watch_mm     : 14-day rain that flags a watch (scaled to horizon)
#   rain_7d_past_watch_mm : rain over the past 7 days that flags a watch
#   tmax_peak_watch_c     : forecast peak high that flags a heat watch
#   frost_alert_c         : forecast low at/below which frost alerts (0 disables
#                           nothing; use None to disable). Kept at 1.0 for the
#                           original four regions to preserve prior behaviour.
_MX_CA_THRESHOLDS = {"rain_14d_watch_mm": 133, "rain_7d_past_watch_mm": 80,
                     "tmax_peak_watch_c": 37, "frost_alert_c": 1.0}

# Threshold sets by climate regime for the Westfalia CO/PE origins. Also
# PROVISIONAL, and calibrated to each regime's normal, not to Michoacán's:
#   Coastal desert : any meaningful rain is anomalous -> low 25mm flag; no
#       "wet week" rule (there is no normal wet week to compare to); frost
#       impossible at sea level -> disabled.
#   High Andean    : at altitude, fruit is unadapted to heat so 30C already
#       stresses; frost raised to 2C because a 2C station reading typically
#       means orchard-level frost in valley bottoms (load-bearing at Sonsón).
#   Cloud forest   : high baseline rainfall, so the rain bars sit high; still
#       frost-aware on the Amazon-facing eastern slope.
_COASTAL_DESERT = {"rain_14d_watch_mm": 25, "rain_7d_past_watch_mm": None,
                   "tmax_peak_watch_c": 34, "frost_alert_c": None}
_HIGH_ANDEAN = {"rain_14d_watch_mm": 160, "rain_7d_past_watch_mm": 100,
                "tmax_peak_watch_c": 30, "frost_alert_c": 2}
_CLOUD_FOREST = {"rain_14d_watch_mm": 250, "rain_7d_past_watch_mm": 150,
                 "tmax_peak_watch_c": 32, "frost_alert_c": 2}

# elevation_m is passed to Open-Meteo so the model returns orchard-altitude
# temperatures, not grid-cell defaults. At 2,200m (Sonsón) and 1,500m
# (Monobamba) the default would be several degrees off and the frost/heat
# thresholds meaningless. The original four omit it and keep grid defaults, so
# their readings — and Task 1's behaviour-neutral flags — are unchanged.
# Latitude signs matter: Colombia is NORTH (+), Peru is SOUTH (-). A sign flip
# would silently relocate Sonsón into Peru and produce plausible-but-wrong data.
REGIONS = [
    {"key": "michoacan", "name": "Michoacán (Uruapan)", "country": "MX",
     "lat": 19.42, "lon": -102.06, "role": "Primary Mexican Hass source (~80% of exports)",
     "thresholds": dict(_MX_CA_THRESHOLDS)},
    {"key": "jalisco", "name": "Jalisco (Cd. Guzmán)", "country": "MX",
     "lat": 19.70, "lon": -103.46, "role": "Second Mexican export state",
     "thresholds": dict(_MX_CA_THRESHOLDS)},
    {"key": "ventura", "name": "Ventura Co. (Oxnard)", "country": "US",
     "lat": 34.23, "lon": -119.08, "role": "California coastal belt",
     "thresholds": dict(_MX_CA_THRESHOLDS)},
    {"key": "san_diego", "name": "San Diego Co. (Fallbrook)", "country": "US",
     "lat": 33.38, "lon": -117.25, "role": "California southern belt",
     "thresholds": dict(_MX_CA_THRESHOLDS)},
    # --- Colombia (Westfalia Antioquia/Caldas belt) — north of the equator ---
    {"key": "co_sonson", "name": "Sonsón, Antioquia", "country": "CO",
     "lat": 5.71, "lon": -75.31, "elevation_m": 2200,
     "role": "Own 600ha + La Loma + main packhouse — highest weight",
     "thresholds": dict(_HIGH_ANDEAN)},
    {"key": "co_eje_cafetero", "name": "Eje Cafetero (Manizales area)", "country": "CO",
     "lat": 5.07, "lon": -75.52, "elevation_m": 2000,
     "role": "220 contract growers across Caldas/Quindío/Risaralda",
     "thresholds": dict(_HIGH_ANDEAN)},
    # --- Peru — south of the equator (note negative latitudes) ---
    {"key": "pe_canete", "name": "Cañete Valley, Lima", "country": "PE",
     "lat": -13.08, "lon": -76.39, "elevation_m": 150,
     "role": "Operational center, 370ac + packhouse",
     "thresholds": dict(_COASTAL_DESERT)},
    {"key": "pe_lambayeque", "name": "Lambayeque (Motupe/Olmos)", "country": "PE",
     "lat": -6.15, "lon": -79.72, "elevation_m": 150,
     "role": "Northern coast — the El Niño flood corridor",
     "thresholds": dict(_COASTAL_DESERT)},
    {"key": "pe_ancash", "name": "Áncash (Casma valley)", "country": "PE",
     "lat": -9.47, "lon": -78.30, "elevation_m": 100,
     "role": "Mid-to-large partner producers",
     "thresholds": dict(_COASTAL_DESERT)},
    {"key": "pe_junin", "name": "Monobamba, Junín", "country": "PE",
     "lat": -11.28, "lon": -75.42, "elevation_m": 1500,
     "role": "Cloud forest, Amazon-facing — different regime",
     "thresholds": dict(_CLOUD_FOREST)},
]

NWS_HEADERS = {"User-Agent": "avo-market-intelligence (github.com dashboard)"}
MET_NO_HEADERS = {"User-Agent":
                  "avo-market-intelligence github.com/mjbusch2121/avo-market-intelligence"}


def open_meteo(lat: float, lon: float, elevation: float | None = None,
               retries: int = 2) -> dict | None:
    for attempt in range(retries):
        try:
            params = {
                "latitude": lat, "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                "past_days": 7, "forecast_days": 14, "timezone": "auto",
            }
            # Pin elevation for high-altitude sites so temperatures are for the
            # orchard, not the grid-cell default. Omitted -> Open-Meteo picks it.
            if elevation is not None:
                params["elevation"] = elevation
            r = requests.get("https://api.open-meteo.com/v1/forecast",
                             params=params, timeout=30)
            r.raise_for_status()
            return r.json()["daily"]
        except Exception as e:
            print(f"  open-meteo attempt {attempt + 1} failed: {type(e).__name__}")
            time.sleep(3 * (attempt + 1))
    return None


def met_no(lat: float, lon: float) -> dict | None:
    """Fallback forecast from MET Norway (~9 days, no past data).

    Returns a next-N-days aggregate in the same shape as summarize()'s
    'next14', or None. Precipitation comes from the 6-hourly buckets at
    synoptic hours to avoid double-counting overlapping windows.
    """
    try:
        r = requests.get("https://api.met.no/weatherapi/locationforecast/2.0/compact",
                         params={"lat": lat, "lon": lon},
                         headers=MET_NO_HEADERS, timeout=45)
        r.raise_for_status()
        ts = r.json()["properties"]["timeseries"]
    except Exception as e:
        print(f"  met.no fallback failed: {type(e).__name__}")
        return None

    days = {}
    for entry in ts:
        d = entry["time"][:10]
        rec = days.setdefault(d, {"tmax": None, "tmin": None, "rain": 0.0})
        t = entry["data"]["instant"]["details"].get("air_temperature")
        if t is not None:
            rec["tmax"] = t if rec["tmax"] is None else max(rec["tmax"], t)
            rec["tmin"] = t if rec["tmin"] is None else min(rec["tmin"], t)
        if int(entry["time"][11:13]) % 6 == 0 and "next_6_hours" in entry["data"]:
            rec["rain"] += entry["data"]["next_6_hours"]["details"].get(
                "precipitation_amount", 0)

    vals = [v for v in days.values() if v["tmax"] is not None]
    if not vals:
        return None
    return {
        "days": len(vals),
        "rain_mm": round(sum(v["rain"] for v in days.values()), 1),
        "tmax_avg_c": round(sum(v["tmax"] for v in vals) / len(vals), 1),
        "tmin_avg_c": round(sum(v["tmin"] for v in vals) / len(vals), 1),
        "tmax_peak_c": round(max(v["tmax"] for v in vals), 1),
        "tmin_low_c": round(min(v["tmin"] for v in vals), 1),
    }


def nws_narrative(lat: float, lon: float) -> str | None:
    """Short NWS forecast text for US regions."""
    try:
        pt = requests.get(f"https://api.weather.gov/points/{lat},{lon}",
                          headers=NWS_HEADERS, timeout=30).json()
        url = pt["properties"]["forecast"]
        periods = requests.get(url, headers=NWS_HEADERS, timeout=30
                               ).json()["properties"]["periods"]
        if periods:
            p = periods[0]
            return f"{p['name']}: {p['detailedForecast']}"
    except Exception as e:
        print(f"  NWS narrative failed: {type(e).__name__}")
    return None


def summarize(daily: dict) -> dict:
    """Split the 21-day daily arrays into past-7 and next-14 aggregates."""
    def agg(times, tmax, tmin, rain):
        return {
            "days": len(times),
            "rain_mm": round(sum(v or 0 for v in rain), 1),
            "tmax_avg_c": round(sum(v for v in tmax if v is not None) /
                                max(1, sum(1 for v in tmax if v is not None)), 1),
            "tmin_avg_c": round(sum(v for v in tmin if v is not None) /
                                max(1, sum(1 for v in tmin if v is not None)), 1),
            "tmax_peak_c": round(max((v for v in tmax if v is not None), default=0), 1),
            "tmin_low_c": round(min((v for v in tmin if v is not None), default=0), 1),
        }

    t, hi, lo, pr = (daily["time"], daily["temperature_2m_max"],
                     daily["temperature_2m_min"], daily["precipitation_sum"])
    return {"past7": agg(t[:7], hi[:7], lo[:7], pr[:7]),
            "next14": agg(t[7:], hi[7:], lo[7:], pr[7:])}


def flag_region(s: dict, region: dict) -> tuple[str, str]:
    """Turn aggregates into a (flag, note) leading-indicator read, using the
    region's own PROVISIONAL thresholds (see REGIONS) rather than global
    constants. A threshold of None disables that rule for the region.

    Thresholds are industry rules of thumb: sustained heavy rain slows
    harvest/packing and hurts dry-matter quality; heat stresses fruit set and
    sizing; frost is a crop risk. What counts as "heavy" or "hot" differs by
    climate, which is why the numbers live per-region.

    rain_14d_watch_mm is a 14-day reference, scaled to the actual forecast
    horizon (which is <14 days on the met.no fallback) so the mm/day trigger
    rate is preserved regardless of how many forecast days exist. This
    reproduces the old `>= 9.5 * horizon` behaviour exactly at 133mm.
    """
    th = region.get("thresholds", {})
    p7, n14 = s.get("past7"), s["next14"]
    horizon = n14.get("days", 14)

    frost = th.get("frost_alert_c")
    if frost is not None and n14["tmin_low_c"] <= frost:
        return "alert", "Frost risk in the forecast — potential fruit/tree damage."

    rain14 = th.get("rain_14d_watch_mm")
    if rain14 is not None and n14["rain_mm"] >= rain14 * horizon / 14:
        return "watch", (f"Heavy rain ahead ({n14['rain_mm']:.0f} mm/{horizon}d) — expect "
                         "harvest and packing slowdowns hitting arrivals in 2-4 weeks.")

    rain7 = th.get("rain_7d_past_watch_mm")
    if p7 and rain7 is not None and p7["rain_mm"] >= rain7:
        return "watch", (f"Wet week just ended ({p7['rain_mm']:.0f} mm) — near-term crossing "
                         "volumes may dip while orchards dry out.")

    tmax = th.get("tmax_peak_watch_c")
    if tmax is not None and n14["tmax_peak_c"] >= tmax:
        return "watch", (f"Heat spike forecast ({n14['tmax_peak_c']:.0f}°C peak) — watch for "
                         "fruit stress and accelerated maturity.")

    return "normal", "No weather-driven supply disruption signals in the 2-4 week window."


def country_coverage(regions_out: list) -> tuple[dict, list]:
    """Availability grouped by origin country, not by raw count.

    Four of ten failing is a meaningless number; *which* country is dark
    determines whether a downstream panel is still trustworthy. A country is
    "dark" only when it returned zero regions — that is the case the ENSO panel
    must guard against, since it would otherwise narrate an origin with no
    live weather behind it.

    Returns ({country: "ok/total"}, [dark country codes]).
    """
    tally = {}
    for e in regions_out:
        c = e.get("country", "??")
        t = tally.setdefault(c, {"ok": 0, "total": 0})
        t["total"] += 1
        t["ok"] += 1 if e.get("available") else 0
    coverage = {c: f'{t["ok"]}/{t["total"]}' for c, t in sorted(tally.items())}
    dark = [c for c, t in sorted(tally.items()) if t["ok"] == 0]
    return coverage, dark


def main():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DIR / "weather.json"
    regions_out = []
    failures = 0

    for region in REGIONS:
        print(f"Weather: {region['name']}...")
        daily = open_meteo(region["lat"], region["lon"], region.get("elevation_m"))
        entry = {k: region[k] for k in ("key", "name", "country", "role")}
        if daily:
            entry.update(summarize(daily))
            entry["source"] = "open-meteo"
            entry["flag"], entry["note"] = flag_region(entry, region)
            entry["available"] = True
        else:
            fallback = met_no(region["lat"], region["lon"])
            if fallback:
                entry["past7"] = None
                entry["next14"] = fallback
                entry["source"] = "met.no"
                entry["flag"], entry["note"] = flag_region(entry, region)
                entry["available"] = True
            else:
                entry["available"] = False
                entry["flag"], entry["note"] = "unknown", "Weather data unavailable this run."
                failures += 1
        if region["country"] == "US":
            entry["nws_narrative"] = nws_narrative(region["lat"], region["lon"])
        regions_out.append(entry)

    coverage, dark_groups = country_coverage(regions_out)

    if failures == len(REGIONS):
        # Total failure: never stamp a fresh fetched_at (that would mask the
        # outage from the staleness detector). Preserve the prior file's
        # timestamp and flag the attempt; with no prior file, omit fetched_at
        # so the feed reads stale. Mirrors fetch_freight_pdf.py.
        reason = "all regions failed to fetch"
        if out_path.exists():
            prev = json.loads(out_path.read_text(encoding="utf-8"))
            prev["fetch_attempted"] = _now_iso()
            prev["fetch_error"] = reason
            prev["coverage"], prev["dark_groups"] = coverage, dark_groups
            out_path.write_text(json.dumps(prev, indent=1), encoding="utf-8")
            print("All regions failed — kept previous weather.json, preserved fetched_at")
        else:
            out_path.write_text(json.dumps({
                "fetch_attempted": _now_iso(), "fetch_error": reason,
                "coverage": coverage, "dark_groups": dark_groups,
                "regions": regions_out}, indent=1), encoding="utf-8")
            print("All regions failed — no prior weather.json to preserve")
        return

    # Partial success is still success for the regions that returned: keep a
    # fresh fetched_at, but publish per-country coverage so a whole origin group
    # going dark (e.g. all of Peru) is legible rather than hidden behind a green
    # timestamp. build_summary turns dark_groups into a WHAT-TO-WATCH note and
    # the ENSO panel uses it to caveat origins it can no longer see.
    out_path.write_text(json.dumps(
        {"fetched_at": _now_iso(), "coverage": coverage,
         "dark_groups": dark_groups, "regions": regions_out},
        indent=1), encoding="utf-8")
    print(f"Weather: {len(REGIONS) - failures}/{len(REGIONS)} regions ok; "
          f"coverage {coverage}" + (f"; DARK: {dark_groups}" if dark_groups else ""))


if __name__ == "__main__":
    main()
