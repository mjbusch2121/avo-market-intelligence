// dashboard.js — renders data.json into the Avocado Market Intelligence page.
// No build step: plain DOM + Chart.js (CDN).

const C = {
  green: "#7fa650",
  flesh: "#c6d89b",
  seed: "#b07a4f",
  clay: "#cc6b49",
  sky: "#8fb4c4",
  muted: "#98a289",
  grid: "rgba(42, 51, 33, 0.6)",
};

const CLAUSE_COLORS = [C.green, C.flesh, C.seed, C.muted, C.sky];

const fmtM = (lbs) => (lbs / 1e6).toFixed(1) + "M";
const fmtMoney = (v) => "$" + Number(v).toFixed(2);

function deltaHtml(pct, suffix = "%") {
  if (pct === null || pct === undefined) return "";
  const cls = pct > 1 ? "up" : pct < -1 ? "down" : "flat";
  const arrow = pct > 1 ? "▲" : pct < -1 ? "▼" : "◆";
  return `<span class="delta ${cls}">${arrow} ${Math.abs(pct).toFixed(1)}${suffix}</span>`;
}

function el(tag, cls, html) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (html !== undefined) node.innerHTML = html;
  return node;
}

function monthTick(iso) {
  const d = new Date(iso + "T00:00:00");
  return d
    .toLocaleDateString("en-US", { month: "short", year: "2-digit" })
    .replace(" ", " '");
}

Chart?.register &&
  (() => {
    Chart.defaults.color = C.muted;
    Chart.defaults.font.family = '"Spline Sans Mono", monospace';
    Chart.defaults.font.size = 10.5;
    Chart.defaults.borderColor = C.grid;
    Chart.defaults.animation.duration = matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches
      ? 0
      : 600;
  })();

// ---------------------------------------------------------------
// Sections
// ---------------------------------------------------------------

function renderHeadline(data) {
  document.getElementById("week-label").textContent = data.week.label;
  const updated = new Date(data.generated_at);
  document.getElementById("updated").textContent =
    "updated " +
    updated.toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
    });

  const h = document.getElementById("headline");
  h.innerHTML = "";
  const clauses = data.headline.replace(/\.$/, "").split("; ");
  clauses.forEach((text, i) => {
    const span = el("span", "clause", text);
    span.style.setProperty("--clause", CLAUSE_COLORS[i % CLAUSE_COLORS.length]);
    span.style.animationDelay = `${i * 0.14}s`;
    h.appendChild(span);
    h.appendChild(
      el("span", "clause-sep", i < clauses.length - 1 ? "; " : "."),
    );
  });
}

function renderKpis(data) {
  const wrap = document.getElementById("kpis");
  data.kpis.forEach((k) => {
    wrap.appendChild(
      el(
        "div",
        "kpi",
        `
      <div class="kpi-label">${k.label}</div>
      <div class="kpi-value">${k.value}</div>
      <div class="kpi-sub">${deltaHtml(k.delta_pct)} ${k.sub || ""}</div>`,
      ),
    );
  });
}

function renderSignals(data) {
  if (!data.signals?.length) return;
  document.getElementById("signals-panel").hidden = false;
  const ul = document.getElementById("signals");
  data.signals.forEach((s) => ul.appendChild(el("li", null, s)));
}

function renderSupply(data) {
  const s = data.supply;
  if (!s?.trend) return;

  const regionsBox = document.getElementById("supply-regions");
  s.regions.forEach((r) => {
    const dotColor = { mx: C.green, ca: C.flesh, ports: C.seed }[r.key];
    const st = r.season?.status || "active";

    // Out-of-season / gap: show the message instead of numbers
    if (st !== "active") {
      const cls =
        st === "out_of_season" ? "stat-row season-off" : "stat-row season-warn";
      const icon = st === "out_of_season" ? "" : "⚠ ";
      regionsBox.appendChild(
        el(
          "div",
          cls,
          `
        <span class="stat-name"><span class="dot" style="background:${dotColor}"></span>${r.name}</span>
        <span class="season-note">${icon}${r.season.message}</span>`,
        ),
      );
      return;
    }

    const comps = r.partial
      ? '<span class="badge partial">partial data</span>'
      : `${deltaHtml(r.wow_pct)} wow${r.vs_3yr_pct !== null ? " · " + deltaHtml(r.vs_3yr_pct) + " 3yr" : ""}`;
    regionsBox.appendChild(
      el(
        "div",
        "stat-row",
        `
      <span class="stat-name"><span class="dot" style="background:${dotColor}"></span>${r.name}</span>
      <span><span class="stat-val">${fmtM(r.lbs)}</span><br><span class="stat-comps">${comps}</span></span>`,
      ),
    );
  });

  const crossBox = document.getElementById("crossings");
  s.crossings.forEach((c) => {
    // suppress noisy % swings on tiny bases
    const wow = c.lbs > 1e6 ? deltaHtml(c.wow_pct) : "";
    crossBox.appendChild(
      el(
        "div",
        "stat-row",
        `
      <span class="stat-name">${c.short}</span>
      <span class="stat-val">${fmtM(c.lbs)} <span class="stat-comps">${wow}</span></span>`,
      ),
    );
  });

  new Chart(document.getElementById("supplyChart"), {
    data: {
      labels: s.trend.map((t) => t.week),
      datasets: [
        {
          type: "bar",
          label: "Mexico crossings",
          data: s.trend.map((t) => t.mx),
          backgroundColor: C.green,
          stack: "vol",
        },
        {
          type: "bar",
          label: "California",
          data: s.trend.map((t) => t.ca),
          backgroundColor: C.flesh,
          stack: "vol",
        },
        {
          type: "bar",
          label: "Seaport/other",
          data: s.trend.map((t) => t.ports),
          backgroundColor: C.seed,
          stack: "vol",
        },
        {
          type: "line",
          label: `${s.baseline_years || 3}-yr seasonal avg (total)`,
          data: s.trend.map((t) => t.avg3yr),
          borderColor: C.muted,
          borderDash: [5, 4],
          borderWidth: 1.5,
          pointRadius: 0,
          stack: "avg",
          spanGaps: true,
        },
      ],
    },
    options: {
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: {
          stacked: true,
          grid: { display: false },
          ticks: {
            maxTicksLimit: 8,
            callback(v) {
              return monthTick(this.getLabelForValue(v));
            },
          },
        },
        y: {
          stacked: true,
          ticks: { callback: (v) => v / 1e6 + "M" },
          title: { display: true, text: "lbs / week" },
        },
      },
      plugins: {
        legend: { labels: { boxWidth: 10, boxHeight: 10 } },
        tooltip: {
          callbacks: {
            label: (ctx) => ` ${ctx.dataset.label}: ${fmtM(ctx.parsed.y)} lbs`,
          },
        },
      },
    },
  });
}

function renderPricing(data) {
  const p = data.pricing;
  if (!p?.trend) return;
  const bYrs = p.benchmark.baseline_years || 3;
  const bandLowLabel = `${bYrs}-yr seasonal 25th percentile`;
  const bandHighLabel = `${bYrs}-yr seasonal 25th–75th percentile`;
  document.getElementById("pricing-sub").textContent =
    p.benchmark.label + ` — vs its ${bYrs}-yr seasonal 25th–75th percentile band`;

  new Chart(document.getElementById("priceChart"), {
    type: "line",
    data: {
      labels: p.trend.map((t) => t.week),
      datasets: [
        {
          label: bandLowLabel,
          data: p.trend.map((t) => t.band_low),
          borderWidth: 0,
          pointRadius: 0,
          fill: false,
          spanGaps: true,
        },
        {
          label: bandHighLabel,
          data: p.trend.map((t) => t.band_high),
          borderWidth: 0,
          pointRadius: 0,
          fill: "-1",
          backgroundColor: "rgba(198, 216, 155, 0.13)",
          spanGaps: true,
        },
        {
          label: "Mexico via TX — Hass 48s",
          data: p.trend.map((t) => t.mx_mid),
          borderColor: C.green,
          borderWidth: 2.2,
          pointRadius: 0,
          spanGaps: true,
        },
        {
          label: "South District CA — Hass 48s",
          data: p.trend.map((t) => t.ca_mid),
          borderColor: C.flesh,
          borderWidth: 1.6,
          borderDash: [2, 3],
          pointRadius: 0,
          spanGaps: true,
        },
      ],
    },
    options: {
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: {
          grid: { display: false },
          ticks: {
            maxTicksLimit: 8,
            callback(v) {
              return monthTick(this.getLabelForValue(v));
            },
          },
        },
        y: {
          ticks: { callback: (v) => "$" + v },
          title: { display: true, text: "FOB $ / carton" },
        },
      },
      plugins: {
        legend: {
          labels: {
            boxWidth: 10,
            boxHeight: 10,
            filter: (item) => item.text !== bandLowLabel,
          },
        },
        tooltip: {
          callbacks: {
            label: (ctx) =>
              ctx.parsed.y === null
                ? null
                : ` ${ctx.dataset.label}: ${fmtMoney(ctx.parsed.y)}`,
          },
        },
      },
    },
  });

  const tables = document.getElementById("price-tables");
  (p.table || []).forEach((t) => {
    const rows = t.sizes
      .map((s) => {
        const mostly =
          s.mostly_low !== null && s.mostly_high !== null
            ? `${fmtMoney(s.mostly_low)}–${Number(s.mostly_high).toFixed(2)}`
            : "—";
        const range =
          s.low !== null && s.high !== null
            ? `${fmtMoney(s.low)}–${Number(s.high).toFixed(2)}`
            : "—";
        return `<tr><td>${s.size}</td><td>${range}</td><td class="mostly">${mostly}</td></tr>`;
      })
      .join("");
    const tone = ["market", "supply", "demand"]
      .filter((k) => t.tone[k])
      .map((k) => `<span class="chip">${k} <b>${t.tone[k]}</b></span>`)
      .join("");
    tables.appendChild(
      el(
        "div",
        "price-table",
        `
      <h3>${t.display}</h3>
      <div class="tone-chips">${tone}</div>
      <table class="fob">
        <thead><tr><th>size</th><th>range</th><th>mostly</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`,
      ),
    );
  });
  if (p.report_date) {
    tables.appendChild(
      el(
        "p",
        "panel-sub",
        `Daily shipping-point report, ${p.report_date} · Hass, 2-layer cartons, conventional`,
      ),
    );
  }
}

function renderFreight(data) {
  const f = data.freight;
  if (!f?.lanes?.length) {
    document.getElementById("freight-sub").textContent =
      "Freight report unavailable this week.";
    return;
  }
  const fsub = document.getElementById("freight-sub");
  const staleAfter = f.stale_after_days ?? 10;
  const fStale = f.stale_days !== null && f.stale_days !== undefined && f.stale_days > staleAfter;
  if (fStale || f.fetch_error) {
    fsub.innerHTML =
      `<span class="stale-warn">⚠ FVWTRK report for ${f.report_date} — ` +
      `${f.stale_days} days old; USDA feed may be unavailable</span>`;
  } else {
    fsub.textContent =
      `FVWTRK report for ${f.report_date} — per-load refrigerated spot rates`;
  }

  const lanesBox = document.getElementById("lanes");
  f.lanes.forEach((l) => {
    const wow = l.wow_reported
      ? l.wow_pct === 0
        ? '<span class="delta flat">◆ flat</span>'
        : deltaHtml(l.wow_pct)
      : "";
    lanesBox.appendChild(
      el(
        "div",
        "lane",
        `
      <div class="lane-dest">${l.dest}</div>
      <div class="lane-rate">$${l.low.toLocaleString()}–${l.high.toLocaleString()}</div>
      <div class="lane-meta">${wow} vs prior week</div>
      <div class="lane-meta">from ${l.origin_short}</div>
      <div class="lane-meta">trucks: ${l.availability}</div>`,
      ),
    );
  });

  const availBox = document.getElementById("availability");
  availBox.appendChild(el("span", null, "Truck availability:"));
  f.availability.forEach((a) => {
    const cls = /Shortage/.test(a.status)
      ? "status-shortage"
      : /Surplus/.test(a.status)
        ? "status-surplus"
        : "status-adequate";
    availBox.appendChild(
      el(
        "span",
        "avail-chip",
        `${a.district} <span class="${cls}">${a.status}</span>`,
      ),
    );
  });
}

function renderDiesel(data) {
  const d = data.diesel;
  const latestBox = document.getElementById("diesel-latest");
  if (!d?.available) {
    latestBox.innerHTML = `<span class="region">Diesel data unavailable: ${d?.reason || "pending"}</span>`;
    return;
  }
  const names = {
    national: "US",
    west_coast: "West Coast",
    gulf_coast: "Gulf Coast",
    east_coast: "East Coast",
  };
  const colors = {
    national: "#edefe6",
    west_coast: C.green,
    gulf_coast: C.seed,
    east_coast: C.sky,
  };

  new Chart(document.getElementById("dieselChart"), {
    type: "line",
    data: {
      labels: d.series.national.map((p) => p.period),
      datasets: Object.entries(d.series).map(([key, pts]) => ({
        label: names[key],
        data: pts.map((p) => p.value),
        borderColor: colors[key],
        borderWidth: key === "national" ? 2.2 : 1.3,
        pointRadius: 0,
      })),
    },
    options: {
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: {
          grid: { display: false },
          ticks: {
            maxTicksLimit: 6,
            callback(v) {
              return monthTick(this.getLabelForValue(v));
            },
          },
        },
        y: { ticks: { callback: (v) => "$" + v.toFixed(2) } },
      },
      plugins: { legend: { labels: { boxWidth: 10, boxHeight: 10 } } },
    },
  });

  Object.entries(d.latest).forEach(([key, v]) => {
    const dir = v.wow > 0.005 ? "up" : v.wow < -0.005 ? "down" : "flat";
    const arrow = dir === "up" ? "▲" : dir === "down" ? "▼" : "◆";
    latestBox.appendChild(
      el(
        "span",
        null,
        `<span class="region">${names[key]}</span> <span class="price">${fmtMoney(v.value)}</span>
       <span class="delta ${dir}">${arrow} ${Math.abs(v.wow).toFixed(2)}</span>`,
      ),
    );
  });
}

// Country order + labels for the weather panel. Near-term supply first
// (Mexico, then California), then the forward-signal origins (Colombia, Peru).
const WX_COUNTRY_ORDER = ["MX", "US", "CO", "PE"];
const WX_COUNTRY_LABELS = { MX: "Mexico", US: "California", CO: "Colombia", PE: "Peru" };

function weatherCard(r) {
  const card = el("div", "wx-card");
  const flag = r.flag === "unknown" ? "pending" : r.flag;
  card.appendChild(
    el(
      "div",
      "wx-head",
      `<span class="wx-name">${r.name}</span><span class="badge ${r.flag}">${flag}</span>`,
    ),
  );
  // Phenological stage (harvest/flowering/sizing) — the label that turns a
  // rainfall number into a reason to care. Absent for Michoacán/California.
  if (r.stage) card.appendChild(el("div", "wx-stage", r.stage));
  card.appendChild(el("div", "wx-role", r.role));
  if (r.available) {
    const days = r.next14.days || 14;
    const p7 = (k, unit) => (r.past7 ? r.past7[k] + unit : "—");
    card.appendChild(
      el(
        "div",
        "wx-stats",
        `
        <span class="k">past 7d rain</span><span class="k">next ${days}d rain</span>
        <span>${p7("rain_mm", " mm")}</span><span>${r.next14.rain_mm} mm</span>
        <span class="k">past 7d high</span><span class="k">next ${days}d peak</span>
        <span>${p7("tmax_avg_c", "°C")}</span><span>${r.next14.tmax_peak_c}°C</span>`,
      ),
    );
  }
  card.appendChild(
    el(
      "div",
      "wx-note",
      r.nws_narrative
        ? `${r.note} <br><span style="color:var(--muted)">NWS: ${r.nws_narrative}</span>`
        : r.note,
    ),
  );
  return card;
}

function renderWeather(data) {
  const w = data.weather;
  const grid = document.getElementById("weather");
  if (!w?.regions?.length) {
    grid.appendChild(
      el(
        "p",
        "panel-sub",
        "Weather data arrives with the next automated refresh.",
      ),
    );
    return;
  }

  // Group cards by origin country. Every region stays visible year-round (no
  // muting on harvest window — the flowering months are where the leading
  // indicator lives). A whole country going dark shows a header badge.
  const dark = new Set(w.dark_groups || []);
  const cov = w.coverage || {};
  const groups = {};
  w.regions.forEach((r) => {
    (groups[r.country] ||= []).push(r);
  });
  const ordered = [
    ...WX_COUNTRY_ORDER.filter((c) => groups[c]),
    ...Object.keys(groups).filter((c) => !WX_COUNTRY_ORDER.includes(c)),
  ];

  // Each group is its own block: a heading + its own card grid. Per-group grids
  // are what let auto-fit collapse empty tracks so two-region groups (Mexico,
  // California, Colombia) get wide cards instead of blank slots — a single
  // shared grid can't, because Peru's four cards keep every track occupied.
  ordered.forEach((c) => {
    const covTxt = cov[c] ? `<span class="wx-group-cov">${cov[c]}</span>` : "";
    const darkBadge = dark.has(c)
      ? `<span class="badge dark">no data this run</span>`
      : "";
    const group = el("div", "wx-group");
    group.appendChild(
      el(
        "div",
        "wx-group-head",
        `<span class="wx-group-name">${WX_COUNTRY_LABELS[c] || c}</span>${covTxt}${darkBadge}`,
      ),
    );
    const cards = el("div", "wx-group-cards");
    groups[c].forEach((r) => cards.appendChild(weatherCard(r)));
    group.appendChild(cards);
    grid.appendChild(group);
  });
}

const ENSO_COUNTRY_LABELS = { MX: "Mexico", CO: "Colombia", PE: "Peru" };

function renderEnso(data) {
  const e = data.enso;
  const head = document.getElementById("enso-head");
  const origBox = document.getElementById("enso-origins");
  if (!e || !e.available) {
    if (head)
      head.innerHTML = `<span class="panel-sub">ENSO data arrives with the next refresh.</span>`;
    return;
  }
  const roni = e.roni, oni = e.oni;

  // Header LEADS with trend + direction; the band label is secondary and always
  // carries its season. Consecutive-season count distinguishes "warm anomaly"
  // from an established event.
  const established = e.established_event
    ? `established event · ${e.consecutive_seasons} consecutive seasons`
    : `${e.consecutive_seasons} consecutive season${e.consecutive_seasons === 1 ? "" : "s"} past ±0.5 — not yet an established event`;
  const lag = e.lag_caveat
    ? `<p class="enso-lag">⚠ ${e.lag_caveat}.</p>`
    : "";
  const oniTxt = oni?.latest
    ? `ONI ${oni.latest.anom >= 0 ? "+" : ""}${oni.latest.anom.toFixed(2)} (${oni.latest.season} ${oni.latest.year}, ${oni.latest.phase})`
    : "";
  const fwdNote = e.forward_note
    ? `<p class="enso-forward-note">◆ ${e.forward_note}</p>`
    : "";
  head.innerHTML = `
    <p class="enso-headline">${e.headline}</p>
    <p class="enso-sub"><span class="enso-band">${e.phase_with_season}</span> · ${established}</p>
    <p class="enso-oni">RONI led (CPC's official basis); ${oniTxt} shown alongside.</p>
    ${lag}${fwdNote}`;

  // Trend chart — RONI led, ONI alongside, reusing the diesel/supply chart look.
  const labels = roni.series.map((p) => `${p.season} ${p.year}`);
  new Chart(document.getElementById("ensoChart"), {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "RONI", data: roni.series.map((p) => p.anom),
          borderColor: C.flesh, borderWidth: 2.2, pointRadius: 0 },
        { label: "ONI", data: (oni.series || []).map((p) => p.anom),
          borderColor: C.muted, borderWidth: 1.3, pointRadius: 0, borderDash: [4, 3] },
      ],
    },
    options: {
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { grid: { display: false }, ticks: { maxTicksLimit: 6 } },
        y: {
          ticks: { callback: (v) => (v > 0 ? "+" : "") + v },
          grid: {
            // emphasise the ±0.5 event thresholds and zero line
            color: (ctx) =>
              [0, 0.5, -0.5].includes(ctx.tick.value) ? C.line : "transparent",
          },
        },
      },
      plugins: { legend: { labels: { boxWidth: 10, boxHeight: 10 } } },
    },
  });

  // Per-origin teleconnection rows. Do NOT badge the region weather cards —
  // this is the only place ENSO appears at origin level.
  (e.origins || []).forEach((o) => {
    const sig = o.commercially_significant ? " enso-key" : "";
    const stage = o.stage ? `<span class="enso-stage">${o.stage}</span>` : "";
    const dark = o.weather_dark
      ? `<span class="enso-dark">no live ${ENSO_COUNTRY_LABELS[o.country] || o.country} weather this cycle</span>`
      : "";
    // Dual-pathway origins (Michoacán) show each pathway; single-pathway origins
    // show one effect line. Lag lives on the pathway row when pathways exist.
    const effectHtml = Array.isArray(o.pathways)
      ? o.pathways
          .map(
            (p) =>
              `<div class="enso-pathway"><span class="enso-ptype">${p.type === "forward" ? "forward" : "near-term"} · ${p.lag_months[0]}–${p.lag_months[1]} mo</span> ${p.impact}</div>`,
          )
          .join("")
      : `<div class="enso-origin-effect"><b>${o.el_nino_effect}</b> — ${o.impact}</div>`;
    const tier = o.supply_tier ? `${o.supply_tier} origin · ` : "";
    const lag =
      !Array.isArray(o.pathways) && Array.isArray(o.lag_months)
        ? ` · ${o.lag_months[0]}–${o.lag_months[1]} mo lag`
        : "";
    origBox.appendChild(
      el(
        "div",
        "enso-origin" + sig,
        `<div class="enso-origin-head">
           <span class="enso-origin-name">${o.name}</span>${stage}${dark}
         </div>
         ${effectHtml}
         <div class="enso-origin-meta">${tier}confidence: ${o.confidence}${lag} · watch: ${o.watch_window}</div>`,
      ),
    );
  });
}

function renderFooter(data) {
  const notes = document.getElementById("foot-notes");
  if (data.feeds) {
    const staleFeedNames = Object.entries(data.feeds)
      .filter(([k, v]) => !k.startsWith("_") && v.stale && k !== "freight")
      .map(([k]) => (k === "supply" || k === "pricing") ? "USDA" : k);
    const unique = [...new Set(staleFeedNames)].sort();
    if (unique.length) {
      notes.appendChild(el("p", "stale-warn",
        `⚠ Data feeds not refreshed this cycle: ${unique.join(", ")} — figures may not reflect the current week.`));
    }
  }
  (data.meta.notes || []).forEach((n) =>
    notes.appendChild(el("p", null, "⚠ " + n)),
  );
  const sources = document.getElementById("foot-sources");
  data.meta.sources.forEach(
    (s) => (sources.appendChild(el("a", null, s.name)).href = s.url),
  );
}

// ---------------------------------------------------------------

async function init() {
  let data;
  try {
    const res = await fetch(`data.json?t=${Date.now()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch (e) {
    document.getElementById("headline").textContent =
      "Could not load this week's data — try refreshing in a minute.";
    return;
  }
  renderHeadline(data);
  renderKpis(data);
  renderSignals(data);
  renderSupply(data);
  renderPricing(data);
  renderFreight(data);
  renderDiesel(data);
  renderWeather(data);
  renderEnso(data);
  renderFooter(data);
}

init();
