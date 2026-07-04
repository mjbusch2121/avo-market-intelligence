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
from pathlib import Path

import requests

ROOT = Path(__file__).parent
RAW_DIR = ROOT / "data" / "raw"

REGIONS = [
    {"key": "michoacan", "name": "Michoacán (Uruapan)", "country": "MX",
     "lat": 19.42, "lon": -102.06, "role": "Primary Mexican Hass source (~80% of exports)"},
    {"key": "jalisco", "name": "Jalisco (Cd. Guzmán)", "country": "MX",
     "lat": 19.70, "lon": -103.46, "role": "Second Mexican export state"},
    {"key": "ventura", "name": "Ventura Co. (Oxnard)", "country": "US",
     "lat": 34.23, "lon": -119.08, "role": "California coastal belt"},
    {"key": "san_diego", "name": "San Diego Co. (Fallbrook)", "country": "US",
     "lat": 33.38, "lon": -117.25, "role": "California southern belt"},
]

NWS_HEADERS = {"User-Agent": "avo-market-intelligence (github.com dashboard)"}
MET_NO_HEADERS = {"User-Agent":
                  "avo-market-intelligence github.com/mjbusch2121/avo-market-intelligence"}


def open_meteo(lat: float, lon: float, retries: int = 2) -> dict | None:
    for attempt in range(retries):
        try:
            r = requests.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": lat, "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                "past_days": 7, "forecast_days": 14, "timezone": "auto",
            }, timeout=30)
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


def flag_region(s: dict) -> tuple[str, str]:
    """Turn aggregates into a (flag, note) leading-indicator read.

    Thresholds are industry rules of thumb: sustained heavy rain slows
    Michoacan harvest/packing and hurts dry-matter quality; >35C heat
    stresses CA fruit set and sizing; frost near 0C is a crop risk.
    """
    p7, n14 = s.get("past7"), s["next14"]
    horizon = n14.get("days", 14)
    if n14["tmin_low_c"] <= 1.0:
        return "alert", "Frost risk in the forecast — potential fruit/tree damage."
    if n14["rain_mm"] >= 9.5 * horizon:  # ~sustained heavy rain for the horizon
        return "watch", (f"Heavy rain ahead ({n14['rain_mm']:.0f} mm/{horizon}d) — expect "
                         "harvest and packing slowdowns hitting arrivals in 2-4 weeks.")
    if p7 and p7["rain_mm"] >= 80:
        return "watch", (f"Wet week just ended ({p7['rain_mm']:.0f} mm) — near-term crossing "
                         "volumes may dip while orchards dry out.")
    if n14["tmax_peak_c"] >= 37:
        return "watch", (f"Heat spike forecast ({n14['tmax_peak_c']:.0f}°C peak) — watch for "
                         "fruit stress and accelerated maturity.")
    return "normal", "No weather-driven supply disruption signals in the 2-4 week window."


def main():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DIR / "weather.json"
    regions_out = []
    failures = 0

    for region in REGIONS:
        print(f"Weather: {region['name']}...")
        daily = open_meteo(region["lat"], region["lon"])
        entry = {k: region[k] for k in ("key", "name", "country", "role")}
        if daily:
            entry.update(summarize(daily))
            entry["source"] = "open-meteo"
            entry["flag"], entry["note"] = flag_region(entry)
            entry["available"] = True
        else:
            fallback = met_no(region["lat"], region["lon"])
            if fallback:
                entry["past7"] = None
                entry["next14"] = fallback
                entry["source"] = "met.no"
                entry["flag"], entry["note"] = flag_region(entry)
                entry["available"] = True
            else:
                entry["available"] = False
                entry["flag"], entry["note"] = "unknown", "Weather data unavailable this run."
                failures += 1
        if region["country"] == "US":
            entry["nws_narrative"] = nws_narrative(region["lat"], region["lon"])
        regions_out.append(entry)

    if failures == len(REGIONS) and out_path.exists():
        print("All regions failed — keeping previous weather.json")
        return

    out_path.write_text(json.dumps({"regions": regions_out}, indent=1), encoding="utf-8")
    print(f"Weather: {len(REGIONS) - failures}/{len(REGIONS)} regions ok")


if __name__ == "__main__":
    main()
