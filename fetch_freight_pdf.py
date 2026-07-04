# fetch_freight_pdf.py
# ---------------------------------------------------------------
# Downloads and parses the USDA AMS FVWTRK "Fruit and Vegetable
# Truck Rate Report" PDF.
#
# The report is a per-origin-district matrix of spot truck rates
# to ten destination cities, with truck availability and the
# week-over-week % change already printed per lane.
#
# Row shapes seen in the wild (city, availability, then numbers):
#   "Atlanta Slight Surplus 6500-6800 (+8)"            range only
#   "Atlanta Slight Shortage 3400-4400 4000-4400 (+2)" range + mostly
#   "Atlanta Slight Shortage 7900-9400 8600-9200 ()"   no WoW figure
#
# Output: data/raw/freight.json
# ---------------------------------------------------------------

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pdfplumber
import requests

PDF_URL = "https://www.ams.usda.gov/mnreports/fvwtrk.pdf"
ROOT = Path(__file__).parent
RAW_DIR = ROOT / "data" / "raw"

DEST_CITIES = ["Atlanta", "Baltimore", "Boston", "Chicago", "Dallas",
               "Los Angeles", "Miami", "New York", "Philadelphia", "Seattle"]

AVAILABILITY = ["Slight Surplus", "Slight Shortage", "Surplus", "Shortage", "Adequate"]

# Boilerplate lines to skip while scanning pages.
NOISE = ("SPECIALTY CROPS NATIONAL TRUCK RATE REPORT", "Agricultural Marketing Service",
         "Specialty Crops Market News", "Email us with accessibility",
         "USDA, AMS,", "Washington, DC", "Phone (202)", "https://mymarketnews",
         "RANGE MOSTLY", "Page ")

RATE_ROW = re.compile(
    r"^(?P<city>" + "|".join(DEST_CITIES) + r")\s+"
    r"(?P<avail>" + "|".join(AVAILABILITY) + r")\s+"
    r"(?P<low>\d+)-(?P<high>\d+)"
    r"(?:\s+(?P<mlow>\d+)-(?P<mhigh>\d+))?"
    r"\s+\((?P<wow>[+-]?\d+)?\)\s*$"
)

REPORT_DATE = re.compile(
    r"TRUCK RATE REPORT FOR \w+ (\w+ \d{1,2}, \d{4})")


def is_district_header(line: str) -> bool:
    """District headers are ALL-CAPS lines that aren't commodity lists,
    rate rows, availability-table rows, or boilerplate."""
    if not line or line.startswith("--"):
        return False
    if any(line.startswith(n) for n in NOISE):
        return False
    if RATE_ROW.match(line):
        return False
    # availability-legend words on page 1 rows ("... Green", "... Orange")
    stripped = re.sub(r"\s+(Light Green|Green|Orange|Red|Blue|Yellow)\s*$", "", line)
    if not stripped.isupper():
        return False
    # must contain letters, and not be a stray continuation like "WASHINGTON"
    return bool(re.search(r"[A-Z]{3,}", stripped))


def parse_pdf(path: Path) -> dict:
    report_date = None
    sections = []
    current = None
    commodity_buffer = []
    in_commodity_list = False

    with pdfplumber.open(path) as pdf:
        lines = []
        for page in pdf.pages:
            lines.extend((page.extract_text() or "").split("\n"))

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        if report_date is None:
            m = REPORT_DATE.search(line)
            if m:
                report_date = datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
                continue

        # A commodity list runs from its "--" line until the "RANGE MOSTLY"
        # column header — everything between is wrapped list text, even when
        # a page break (with footer/header noise) lands mid-list.
        if in_commodity_list:
            if line.startswith("RANGE"):
                in_commodity_list = False
            elif not any(line.startswith(n) for n in NOISE):
                commodity_buffer.append(line)
            continue

        if any(line.startswith(n) for n in NOISE):
            continue

        m = RATE_ROW.match(line)
        if m and current is not None:
            current["rows"].append({
                "dest": m.group("city"),
                "availability": m.group("avail"),
                "low": int(m.group("low")),
                "high": int(m.group("high")),
                "mostly_low": int(m.group("mlow")) if m.group("mlow") else None,
                "mostly_high": int(m.group("mhigh")) if m.group("mhigh") else None,
                "wow_pct": int(m.group("wow")) if m.group("wow") else 0,
                "wow_reported": m.group("wow") is not None,
                "commodities": " ".join(commodity_buffer) if commodity_buffer else None,
            })
            continue

        if line.startswith("--"):
            commodity_buffer = [line.lstrip("-").strip()]
            in_commodity_list = True
            continue

        if is_district_header(line):
            # skip page-1 availability legend rows (they carry a color word)
            if re.search(r"(Light Green|Green|Orange|Red|Blue|Yellow)\s*$", line):
                continue
            current = {"district": line, "rows": []}
            sections.append(current)
            commodity_buffer = []

    # prune headers that captured no rate rows (page-1 table fragments etc.)
    sections = [s for s in sections if s["rows"]]
    return {"report_date": report_date, "source": PDF_URL, "sections": sections}


def main():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = RAW_DIR / "fvwtrk.pdf"
    try:
        r = requests.get(PDF_URL, timeout=120,
                         headers={"User-Agent": "avo-market-intelligence/1.0"})
        r.raise_for_status()
        pdf_path.write_bytes(r.content)
    except Exception as e:
        print(f"FATAL: could not download FVWTRK PDF: {e}", file=sys.stderr)
        # leave any previously parsed freight.json in place
        sys.exit(0 if (RAW_DIR / "freight.json").exists() else 1)

    data = parse_pdf(pdf_path)
    n_rows = sum(len(s["rows"]) for s in data["sections"])
    print(f"Freight: report {data['report_date']}, "
          f"{len(data['sections'])} districts, {n_rows} lanes")
    if n_rows == 0:
        print("WARNING: parsed zero lanes — PDF layout may have changed", file=sys.stderr)

    (RAW_DIR / "freight.json").write_text(json.dumps(data, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
