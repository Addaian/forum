/* Forum — static demo page. All data is pre-baked; the page makes ZERO
 * network calls after the initial JSON / SVG fetches. Slider interactions
 * run the whatif math in-browser via a direct port of src/forum/whatif/probe.py.
 */

const VALUES = ["scalability","maintainability","velocity","correctness","simplicity","flexibility"];

const PRESETS = {
  baseline:       null, // filled at load time from prioritized.json
  velocity:       { scalability:0.8, maintainability:0.6, velocity:2.5, correctness:0.6, simplicity:1.2, flexibility:0.6 },
  correctness:    { scalability:1.0, maintainability:1.2, velocity:0.5, correctness:2.8, simplicity:1.0, flexibility:0.8 },
  maintainability:{ scalability:1.0, maintainability:2.5, velocity:0.6, correctness:1.0, simplicity:1.4, flexibility:1.0 },
};

const SALIENCE_BUMP = 1.10;
const STRUCTURAL_FEATURES = ["blast_radius","recency","principle_severity","pattern_violation","advocate_absence"];

// ---- Whatif math (direct port of src/forum/whatif/probe.py) ----

function salience(lens, weights) {
  const wNorm = VALUES.reduce((s,k) => s + Math.abs(weights[k] || 0), 0) || 1;
  return VALUES.reduce((s,k) => s + (weights[k]||0) * (lens?.[k] || 0), 0) / wNorm;
}

function reweightedAggregate(cells, weights) {
  let debt = 0, just = 0;
  for (const c of cells) {
    const s = salience(c.value_lens, weights);
    if (c.position === "debt")           debt += c.confidence * s;
    else if (c.position === "justified") just += c.confidence * s;
  }
  const total = debt + just;
  if (total === 0) return { winner: null, debt: 0, just: 0, margin: 0 };
  return {
    winner: debt > just ? "debt" : "justified",
    debt, just,
    margin: Math.abs(debt - just) / total,
  };
}

// ---- Scoring (port of src/forum/prioritize/score.py composite formula) ----

function structuralScore(impact) {
  const vals = STRUCTURAL_FEATURES.map(f => +(impact?.[f] ?? 0));
  return vals.reduce((a,b)=>a+b, 0) / vals.length;
}

function valueAffinityScore(principle, weights, affinities) {
  const row = affinities[principle] || {};
  const num = VALUES.reduce((s,k) => s + (weights[k]||0) * (row[k]||0), 0);
  const den = VALUES.reduce((s,k) => s + Math.abs(weights[k]||0), 0) || 1;
  return num / den;
}

function composite(dp, weights, affinities) {
  const s  = structuralScore(dp.measured_impact);
  const va = valueAffinityScore(dp.principle, weights, affinities);
  return { structural: s, value_affinity: va, composite: s * (1 + 0.5 * va) };
}

// ---- Principle → value affinity table (hand-curated; matches
//     src/forum/values/affinities.yaml exactly so the JS rankings match the CLI) ----

const AFFINITIES = {
  P1: {scalability:0.6, maintainability:0.8, velocity:-0.6, correctness:0.3, simplicity:0.5, flexibility:0.4},
  P2: {scalability:0.4, maintainability:0.7, velocity:-0.5, correctness:0.1, simplicity:0.3, flexibility:0.7},
  P3: {scalability:0.1, maintainability:0.9, velocity: 0.6, correctness:0.8, simplicity:0.9, flexibility:0.3},
  P4: {scalability:0.2, maintainability:0.7, velocity: 0.1, correctness:0.4, simplicity:0.6, flexibility:0.5},
  P5: {scalability:0.0, maintainability:0.5, velocity: 0.4, correctness:0.3, simplicity:0.8, flexibility:0.2},
  P6: {scalability:0.7, maintainability:0.7, velocity:-0.5, correctness:0.3, simplicity:0.4, flexibility:0.7},
  P7: {scalability:0.5, maintainability:0.6, velocity: 0.5, correctness:0.2, simplicity:0.4, flexibility:0.4},
};

// ---- State ----

const state = {
  manifest: null,
  activeSlug: null,
  evidence: null,
  prioritized: null,
  verdicts: null,
  baselineWeights: null,
  currentWeights: null,
  dpById: {},
};

// ---- Init: load manifest, then default audit ----

async function init() {
  state.manifest = await fetch("data/manifest.json").then(r => r.json());
  renderSwitcher();
  await loadAudit(state.manifest.default);
}

async function loadAudit(slug) {
  const entry = state.manifest.audits.find(a => a.slug === slug);
  if (!entry) return;
  state.activeSlug = slug;
  document.getElementById("brand-target").textContent = entry.label;
  markSwitcherActive(slug);

  const base = `data/${slug}`;
  const [evidence, prioritized, verdicts, reportMd, graphSvg] = await Promise.all([
    fetch(`${base}/evidence.json`).then(r => r.json()),
    fetch(`${base}/prioritized.json`).then(r => r.json()),
    fetch(`${base}/verdicts.json`).then(r => r.json()),
    fetch(`${base}/report.md`).then(r => r.text()),
    fetch(`${base}/graph.svg`).then(r => r.text()),
  ]);
  state.evidence = evidence;
  state.prioritized = prioritized;
  state.verdicts = verdicts;
  state.baselineWeights = { ...prioritized.values };
  state.currentWeights = { ...prioritized.values };
  state.dpById = Object.fromEntries(evidence.decision_points.map(d => [d.id, d]));
  PRESETS.baseline = { ...prioritized.values };

  lastRanking = null;          // reset rank-diff baseline when switching audits
  renderStats();
  renderSliders();             // re-renders with fresh baseline values
  renderReport(reportMd);
  renderGraph(graphSvg);
  refresh();
  wirePresets();
  markPresetActive(document.querySelector('.preset-btn[data-preset="baseline"]'));
}

function renderSwitcher() {
  const root = document.getElementById("audit-switcher");
  root.innerHTML = "";
  for (const entry of state.manifest.audits) {
    const btn = document.createElement("button");
    btn.className = "swatch";
    btn.dataset.slug = entry.slug;
    const langTag = entry.language
      ? `<span class="lang lang-${entry.language}">${entry.language}</span>`
      : "";
    btn.innerHTML = `${langTag}${entry.label}<span class="ver">${entry.version}</span>`;
    btn.title = `${entry.source} @ ${entry.commit} — ${entry.note}`;
    btn.addEventListener("click", () => {
      if (state.activeSlug === entry.slug) return;
      loadAudit(entry.slug);
    });
    root.appendChild(btn);
  }
}

function markSwitcherActive(slug) {
  document.querySelectorAll("#audit-switcher .swatch").forEach(b =>
    b.classList.toggle("active", b.dataset.slug === slug)
  );
}

function renderStats() {
  const e = state.evidence;
  document.getElementById("stat-repo").textContent = e.repo.split("/").pop() || e.repo;
  document.getElementById("stat-commit").textContent = (e.commit_sha || "?").slice(0, 8);
  document.getElementById("stat-modules").textContent =
    `${e.graph_summary?.num_modules ?? "?"} modules, ${e.graph_summary?.num_edges ?? "?"} edges`;
  document.getElementById("stat-dps").textContent = e.decision_points.length;
  const principles = new Set(e.decision_points.map(d => d.principle));
  document.getElementById("stat-principles").textContent = [...principles].sort().join(", ");
}

function renderSliders() {
  const root = document.getElementById("sliders");
  root.innerHTML = "";
  for (const v of VALUES) {
    const row = document.createElement("div");
    row.className = "slider-row";
    const baseline = state.baselineWeights[v] ?? 1.0;
    row.innerHTML = `
      <div class="label">
        <span class="name">${v}</span>
        <span class="value" data-val="${v}">${baseline.toFixed(2)}</span>
      </div>
      <input type="range" min="0" max="3" step="0.05" value="${baseline}" data-name="${v}">
    `;
    root.appendChild(row);
    const input = row.querySelector("input");
    input.addEventListener("input", e => {
      const name = e.target.dataset.name;
      state.currentWeights[name] = +e.target.value;
      updateSliderLabel(name);
      markPresetActive(null);
      refresh();
    });
  }
}

function updateSliderLabel(name) {
  const el = document.querySelector(`.value[data-val="${name}"]`);
  if (!el) return;
  const v = state.currentWeights[name];
  const b = state.baselineWeights[name];
  el.textContent = v.toFixed(2);
  el.classList.remove("up", "down");
  if (v > b + 0.05) el.classList.add("up");
  else if (v < b - 0.05) el.classList.add("down");
}

function wirePresets() {
  document.querySelectorAll(".preset-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const preset = PRESETS[btn.dataset.preset];
      if (!preset) return;
      state.currentWeights = { ...preset };
      for (const v of VALUES) {
        const input = document.querySelector(`input[data-name="${v}"]`);
        if (input) input.value = state.currentWeights[v];
        updateSliderLabel(v);
      }
      markPresetActive(btn);
      refresh();
    });
  });
}

function markPresetActive(activeBtn) {
  document.querySelectorAll(".preset-btn").forEach(b =>
    b.classList.toggle("active", b === activeBtn)
  );
}

// ---- Live re-projection ----

let lastRanking = null;

function refresh() {
  refreshRanking();
  refreshTribunal();
}

function refreshRanking() {
  const root = document.getElementById("ranking");
  // Re-rank all DPs in the evidence bundle under current weights.
  const scored = state.evidence.decision_points.map(dp => {
    const c = composite(dp, state.currentWeights, AFFINITIES);
    return { dp, ...c };
  });
  scored.sort((a, b) => b.composite - a.composite);
  // Keep only the original top-5 prioritization set so the demo stays focused.
  const originalIds = new Set(state.prioritized.items.map(i => i.decision_point_id));
  const top = scored.filter(s => originalIds.has(s.dp.id));

  root.innerHTML = "";
  top.forEach((row, idx) => {
    const oldRank = lastRanking ? lastRanking.indexOf(row.dp.id) : idx;
    const delta = lastRanking ? oldRank - idx : 0;
    const li = document.createElement("li");
    if (delta !== 0) li.classList.add("shifted");
    li.innerHTML = `
      <div class="subject">
        <span class="principle">${row.dp.principle}</span>${escapeHtml(row.dp.subject)}
      </div>
      <div class="score">
        ${row.composite.toFixed(3)}
        ${delta > 0 ? `<span class="delta up">↑${delta}</span>` :
          delta < 0 ? `<span class="delta down">↓${Math.abs(delta)}</span>` : ""}
      </div>
    `;
    root.appendChild(li);
  });
  lastRanking = top.map(t => t.dp.id);
}

function refreshTribunal() {
  const tribunal = state.verdicts[0];
  if (!tribunal) {
    document.getElementById("tribunal").innerHTML =
      `<p class="hint">No tribunal data on disk. Run <code>forum audit /tmp/forum-demo-repo --top-n 5</code> and re-deploy.</p>`;
    return;
  }
  const dp = state.dpById[tribunal.decision_point_id];
  const cells = tribunal.cells || [];
  const judge = tribunal.judge || {};
  const aggOrig = tribunal.aggregate_vote || {};
  const aggNew = reweightedAggregate(cells, state.currentWeights);
  const wouldFlip = aggNew.winner && aggOrig.winner && aggNew.winner !== aggOrig.winner;

  // Per-cell salience ratio
  const cellsWithRatio = cells.map(c => {
    const b = salience(c.value_lens, state.baselineWeights);
    const n = salience(c.value_lens, state.currentWeights);
    const ratio = b > 0 ? n / b : (n > 0 ? Infinity : 1);
    return { ...c, ratio };
  });
  cellsWithRatio.sort((a, b) => b.ratio - a.ratio);

  const verdictKey = (judge.verdict || "").replace(/ /g, "-");

  document.getElementById("tribunal").innerHTML = `
    <div class="tribunal-head">
      <div>
        <div><b>${escapeHtml(dp?.subject || tribunal.decision_point_id)}</b></div>
        <div class="aggregate">
          original panel: <b>${aggOrig.n_debt ?? 0}d</b> /
          <b>${aggOrig.n_justified ?? 0}j</b>
          (margin ${(aggOrig.margin ?? 0).toFixed(2)},
          method ${aggOrig.method || "unweighted"})
          ${aggOrig.cells_cancelled ? ` · ${aggOrig.cells_cancelled} cancelled by speculative stop` : ""}
        </div>
      </div>
      <span class="verdict-tag verdict-${verdictKey}">${escapeHtml(judge.verdict || "—")}</span>
    </div>

    <div class="aggregate">
      <b>Re-projected aggregate</b> under your weights:
      winner=<b>${aggNew.winner ?? "—"}</b>
      (debt-score ${aggNew.debt.toFixed(2)}, justified-score ${aggNew.just.toFixed(2)},
      margin ${aggNew.margin.toFixed(2)})
      ${wouldFlip
        ? `<span class="delta down">— <b>would have flipped</b> under your weights</span>`
        : `<span class="delta">— verdict text is preserved literally; only emphasis shifts</span>`}
    </div>

    <div class="tribunal-cells">
      ${cellsWithRatio.map(renderCell).join("")}
    </div>

    <div class="judge-block">
      <h3>Judge reasoning (Anthropic Sonnet 4.6)</h3>
      <div class="body">${escapeHtml(judge.reasoning || "(none)")}</div>
    </div>
    <div class="judge-block">
      <h3>Strongest dissent</h3>
      <div class="body">${escapeHtml(judge.dissent_summary || "(none)")}</div>
    </div>
    <div class="judge-block">
      <h3>Recommended action</h3>
      <div class="body">${escapeHtml(judge.recommended_action || "(none)")}</div>
    </div>
  `;
}

function renderCell(c) {
  const salient = c.ratio >= SALIENCE_BUMP;
  const ratioStr = c.ratio === Infinity ? "∞" : `${c.ratio.toFixed(2)}×`;
  const persona = c.position === "debt"
    ? `red · ${c.red_persona}`
    : `blue · ${c.blue_persona}`;
  return `
    <div class="cell${salient ? " salient" : ""}">
      <div class="cell-head">
        <span class="cell-id">cell ${c.cell_id} · ${escapeHtml(persona)}</span>
        <span class="pos-${c.position}">${c.position.toUpperCase()}</span>
      </div>
      <div class="key-arg">"${escapeHtml(c.key_argument || "(no argument)")}"</div>
      <div class="salience">
        confidence ${c.confidence.toFixed(2)} ·
        salience under your weights: <span class="ratio">${ratioStr}</span>
        ${salient ? " — more salient" : ""}
      </div>
    </div>
  `;
}

// ---- Report markdown + graph ----

function renderReport(md) {
  marked.setOptions({ breaks: false, gfm: true });
  const html = marked.parse(md);
  // Post-process: wrap "**Verdict: X**" patterns in colored tags.
  const tagged = html.replace(
    /<strong>\s*Verdict:\s*([A-Z][A-Z ]+[A-Z])\s*<\/strong>/g,
    (_, v) => `<span class="verdict-tag verdict-${v.replace(/ /g, "-")}">${v}</span>`
  );
  document.getElementById("report-md").innerHTML = tagged;
}

function renderGraph(svg) {
  document.getElementById("graph-wrap").innerHTML = svg;
}

// ---- Utilities ----

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

document.addEventListener("DOMContentLoaded", init);
