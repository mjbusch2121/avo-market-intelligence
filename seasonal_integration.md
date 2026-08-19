# Seasonal Awareness — Integration Guide

Two new files make the dashboard season-aware:

| File | Role |
|---|---|
| `seasons.json` | The calendar. Single source of truth for every region's active window. Seasonal updates = edit this one file. |
| `seasonality.py` | The logic. Classifies each region as `active` / `out_of_season` / `unexpected_gap`. Run `python seasonality.py` any time to self-test (9 built-in checks). |

Nothing runs until you wire them in. Total integration is ~20 lines across two
existing files.

Line numbers below refer to `build_summary.py` as of the 2026-07-21 commit.
They'll drift as you edit — use the surrounding code as the real anchor.

---

## Why per-region freshness matters (read this first)

`build_supply()` initializes every region to `0` each week (line 101), and
`week_end` is simply the newest week with data from *any* region. So when
California goes dormant in October, CA still appears on the current week with
`lbs: 0`.

That means **you cannot use `supply["week_end"]` as the last-reported date** —
every region would look permanently fresh and `classify()` would report
`active` forever, which defeats the entire purpose.

Instead, each region needs its own last-reported date: the most recent week
where *that* region had volume above zero. Step 2 below builds that.

---

## Step 0 — Place the files

Drop `seasons.json` and `seasonality.py` into the repo root, alongside
`build_summary.py`. Verify the logic works standalone:

```bash
python seasonality.py
```

Expect `9/9 checks passed`. A "file not found" error means you're in the
wrong folder.

---

## Step 1 — Import

Top of `build_summary.py`, after `from pathlib import Path` (~line 15):

```python
from seasonality import classify, any_unexpected_gaps
```

---

## Step 2 — Per-region freshness helper

Inside `build_supply()`, right after the `trailing_median` function ends
(~line 139) and before `partial_keys = set()`:

```python
    def last_reported_week(key):
        """Most recent week where THIS region actually had volume.
        Returns None if it has never reported."""
        for w in reversed(weeks):
            if weekly[w][key] > 0:
                return w
        return None
```

This works because `weeks` is already sorted ascending (line 106), so walking
it backwards finds the newest non-zero week first.

---

## Step 3 — Classify each region

In the existing `regions.append({...})` block (~line 156), add one key:

```python
        regions.append({
            "key": key, "name": name, "lbs": cur,
            "partial": partial,
            "wow_pct": None if partial else pct(cur, pri),
            "vs_3yr_pct": None if partial or not avg else pct(cur, avg),
            "season": classify(key, last_reported_week(key)),
        })
```

Every region in `data.json` now carries a `season` block with `status`,
`message`, and `last_reported`.

---

## Step 4 — Fail loudly on unexpected gaps

At the end of `main()`, **after** the `data.json` write (~line 509) so the
site still publishes with the warning visible rather than going stale:

```python
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
    if gaps:
        raise SystemExit(1)   # turns the GitHub Action red -> you get the email
```

The `print` loop before the exit matters — when the Action goes red, that
message tells you *which* region broke without digging through the full log.

This is the difference between the dashboard silently rotting and it telling
you it needs attention.

---

## Step 5 — Test the Python side

```bash
python build_summary.py
```

Should run clean with no `SEASON` lines (all three regions are active in
mid-summer). Verify the stamps landed:

```bash
python -c "import json; d=json.load(open('data.json')); [print(r['key'], r['season']) for r in d['supply']['regions']]"
```

Expect three regions, each `'status': 'active'`.

**Then deliberately test the failure paths** — Important: `classify()` checks
freshness *first*, and only consults the calendar when data is missing. A
region that's actually reporting is `active` regardless of its window — the
calendar's only job is to interpret absence. So changing a season window
alone will NOT produce an out-of-season card while data is flowing.

To see the non-active states, make both edits at once:

1. `seasons.json` — set CA's window `end` to a past date (e.g. `"06-30"`)
2. `seasonality.py` — set `STALE_AFTER_DAYS = 1`

Run `python build_summary.py`. Expect all three states in one run:

    SEASON [unexpected_gap] mx: Mexico crossings should be reporting but isn't...
    SEASON [out_of_season]  ca: California (South District) season concluded — resumes ~spring
    SEASON [unexpected_gap] ports: Seaport/other imports should be reporting but isn't...

Same stale data, three regions, different verdicts — produced entirely by the
calendar. That's the design working.

Exit code will be `1` because of the mx/ports gaps. To confirm out-of-season
alone exits `0`, you'd have to widen those windows too; the message appearing
is sufficient proof.

**Revert BOTH files afterward** — CA `end` to `"09-30"`, `STALE_AFTER_DAYS`
to `14` — then rerun `build_summary.py` and confirm no `SEASON` lines print.
Forgetting the first revert would take California dark on the live site in
midseason.

---

## Step 6 — Front end: the three-state cards (`dashboard.js`)

In `renderSupply()`, branch on `season.status` before rendering a region's
normal numbers:

```javascript
function seasonCard(region) {
  const s = region.season || { status: "active" };
  if (s.status === "out_of_season") {
    return `<div class="region-card muted">
              <div class="region-name">${region.name}</div>
              <div class="season-note">${s.message}</div>
            </div>`;
  }
  if (s.status === "unexpected_gap") {
    return `<div class="region-card warn">
              <div class="region-name">${region.name}</div>
              <div class="season-note">⚠ ${s.message}</div>
            </div>`;
  }
  return null;  // active -> caller renders the normal card
}
```

In the region loop:

```javascript
const special = seasonCard(r);
if (special) { html += special; continue; }
```

Matching CSS for `style.css`:

```css
.region-card.muted { opacity: 0.55; }
.region-card.muted .season-note { font-style: italic; font-size: 0.85rem; }
.region-card.warn  { border: 1px solid #e0a030; }
.region-card.warn .season-note { color: #e0a030; font-size: 0.85rem; }
```

Preview locally with `python -m http.server`, then open
`http://localhost:8000`.

---

## Skeleton — Future Elements (items 3–7)

Stubs for these already exist in `seasonality.py`; each item says what to
build and where it plugs in.

### Item 3 — Loud failure signal ✅ done in Step 4
*Optional upgrade:* in `.github/workflows/update.yml`, auto-open a GitHub
issue on failure:

```yaml
- name: Open issue on failure
  if: failure()
  uses: actions/github-script@v7
  with:
    script: |
      github.rest.issues.create({
        owner: context.repo.owner, repo: context.repo.repo,
        title: `Pipeline failure ${new Date().toISOString().slice(0,10)}`,
        body: "A data source expected to be active returned nothing. Check the Actions log."
      })
```

### Item 4 — Season-aware narrative
`narrative_regions(season_blocks)` returns only-active region keys. Filter
region mentions in `build_headline()` (~line 372) and `build_signals()`
(~line 402) through that list, so the sentence never references a region that
isn't shipping. Out-of-season transitions can *become* the story:
"California's season has concluded; Mexico now carries full domestic supply."

### Item 5 — Historical band suppression
`suppress_historical_band(season_block)` returns True when a region isn't
active. Use it in `build_supply()`'s `trend` loop (~line 123) to null that
region's `avg3yr` contribution — prevents plotting a "normal range" against a
missing line, which reads as a crash rather than an off-season.

### Item 6 — Peru / Colombia activation
`seasons.json` already has `peru` and `colombia` entries marked FUTURE, so the
calendar side is done. To activate:
1. ~4 weeks before Peru's window (mid-May), verify which USDA AMS import
   reports carry origin-level detail (terminal market vs. shipping point differ)
2. Build `fetch_imports.py` on the same pattern as `fetch_usda_pricing.py`
3. Add `peru` / `colombia` to the region tuple list (~line 143) and to
   `region_of()` (~line 59) — classification then works automatically, with
   no changes to `seasonality.py`

Note: today's `ports` region already includes Peru and Colombia volume in
aggregate. Splitting them out means deciding whether `ports` becomes
"other seaports" or stays a total — worth settling before you build.

### Item 7 — Recurring checklist
Lives in `README.md`. Calendar reminders for mid-March and mid-September are
the low-tech way to actually do it.

---

## Rollback

Every change above is additive. If something misbehaves, remove the import
(Step 1), the `"season":` line (Step 3), and the Step 4 block — the dashboard
returns to its prior behavior. `seasons.json` and `seasonality.py` can sit
unused in the repo harmlessly.