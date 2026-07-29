# 🥑 Avocado Market Intelligence

A self-updating market intelligence dashboard for the avocado trade — built for
growers, distributors, importers, and the sales & sourcing teams that make
weekly buy/sell decisions.

**Live dashboard:** https://mjbusch2121.github.io/avo-market-intelligence/

Every Tuesday a GitHub Actions pipeline pulls fresh government data, rebuilds
`data.json`, and GitHub Pages redeploys — no server, no database, no manual
steps.

## What it shows

| Panel | Source | What it answers |
|---|---|---|
| **The wire** | generated | The whole week in one auto-written sentence |
| **Supply** | USDA AMS movement data | Volume by crossing (Pharr, Laredo, Nogales, Otay Mesa), California, and seaports — this week vs last vs the 3-year seasonal pace |
| **Pricing** | USDA AMS shipping-point reports | Hass 48s FOB benchmark plotted against its 3-year seasonal band, plus the full size/grade sheet and market tone |
| **Freight** | USDA FVWTRK truck rate PDF | Spot rates into LA, Dallas, Miami, Philadelphia with truck availability and week-over-week change |
| **Diesel** | EIA weekly retail | Fuel context for reading freight moves |
| **Weather** | Open-Meteo + NOAA/NWS | Michoacán, Jalisco, Ventura, San Diego — a 2–4 week leading indicator for arrivals |

## Architecture

```
fetch_usda_pricing.py   MARS API — prices + movement, incremental history cache
fetch_freight_pdf.py    FVWTRK PDF — parsed with pdfplumber
fetch_diesel.py         EIA v2 API — national + 3 PADD regions
fetch_weather.py        Open-Meteo + NWS — 4 growing regions
build_summary.py        merges everything → data.json + the headline sentence
index.html / style.css / dashboard.js   static front end (Chart.js)
.github/workflows/update.yml            Tuesday 20:00 UTC cron
```

Two design details worth noting:

- **History is cached in the repo** (`history/*.json`). The first run seeded
  ~4 years of weekly data; weekly runs only fetch a 45-day window and merge,
  so USDA's late revisions self-correct.
- **Partial-data guard**: USDA posts some districts late (California domestic
  movement especially). When a region prints far below its trailing median,
  its comparisons are suppressed and labeled rather than headlining a phantom
  collapse.

## Running locally

```bash
pip install -r requirements.txt
# .env: MARS_API_KEY=...   (free: https://mymarketnews.ams.usda.gov/mars-api/getting-started)
#       EIA_API_KEY=...    (free: https://www.eia.gov/opendata/register.php)
python fetch_usda_pricing.py
python fetch_freight_pdf.py
python fetch_diesel.py
python fetch_weather.py
python build_summary.py
python -m http.server   # open http://localhost:8000
```

The same two keys live as GitHub Actions secrets (`MARS_API_KEY`,
`EIA_API_KEY`) for the scheduled runs.

## Data notes

- Volume is in pounds (`1_lb_units` from USDA movement data), aggregated per
  week ending Saturday.
- The pricing benchmark is **Hass 48s, 2-layer cartons, conventional** — the
  most consistently quoted spec across both districts.
- FVWTRK publishes Tuesday-survey rates on Wednesday, so the freight panel
  always shows the most recent completed survey.
- All data is public U.S. government data (USDA AMS, EIA, NOAA).

## Seasonal maintenance checklist

The dashboard is season-aware (`seasons.json` + `seasonality.py` — see
`SEASONAL_INTEGRATION.md`), but twice a year it needs 15 minutes of human
attention. **Mid-March and mid-September**, at the season shoulders:

- [ ] Confirm USDA report slugs/IDs haven't changed (run each fetcher locally, check for empty output)
- [ ] Verify the FVWTRK PDF still parses — layout changes without notice and this is the most fragile piece (`python fetch_freight_pdf.py`)
- [ ] Update `seasons.json` if actual season timing shifted vs. the configured windows
- [ ] Refresh the 3-year historical window (confirm `history/*.json` rolled forward past the oldest year)
- [ ] Spring only: ~4 weeks before Peru's season (mid-May), check USDA import report availability if activating Peru/Colombia tracking (see SEASONAL_INTEGRATION.md, Item 6)
- [ ] Eyeball the live site: any region showing an ⚠ unexpected-gap warning needs investigation now, not later

If the Tuesday Action goes red between checkpoints, the most likely causes in
order: transient GitHub/Pages hiccup (re-run the job), USDA posted late
(re-run tomorrow), report slug changed (check fetcher output), PDF layout
changed (fix the parser).