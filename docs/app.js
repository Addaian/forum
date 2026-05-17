/* Forum frontend — 4-view SPA.
 *
 * Loads docs/data/manifest.json → user picks an audit → loads that audit's
 * artifacts → renders Evidence, Prioritization, AI Jury, Briefing views.
 * Sliders re-project rankings + dissent salience in the browser via a JS
 * port of `src/forum/whatif/probe.py` math. Zero LLM calls; the page is
 * static-hostable on GitHub Pages.
 *
 * Dependency graph uses the same in-browser stack as GitNexus's web client
 * (sigma v3 + graphology + forceatlas2 + edge-curve), loaded as ESM from
 * esm.sh — see imports below.
 */

import Sigma                  from "https://esm.sh/sigma@3.0.2";
import Graph                  from "https://esm.sh/graphology@0.26.0";
import forceAtlas2            from "https://esm.sh/graphology-layout-forceatlas2@0.10.1";
import { EdgeCurvedArrowProgram } from "https://esm.sh/@sigma/edge-curve@3.1.0";

const VALUES = ["scalability","maintainability","velocity","correctness","simplicity","flexibility"];
const SALIENCE_BUMP = 1.10;
const INFINITY_SENTINEL = 999;
const STRUCTURAL_FEATURES = ["blast_radius","recency","principle_severity","pattern_violation","advocate_absence"];

// Mirrors src/forum/values/affinities.yaml — keep in sync.
const AFFINITIES = {
  P1: {scalability:0.6, maintainability:0.8, velocity:-0.6, correctness:0.3, simplicity:0.5, flexibility:0.4},
  P2: {scalability:0.4, maintainability:0.7, velocity:-0.5, correctness:0.1, simplicity:0.3, flexibility:0.7},
  P3: {scalability:0.1, maintainability:0.9, velocity: 0.6, correctness:0.8, simplicity:0.9, flexibility:0.3},
  P4: {scalability:0.2, maintainability:0.7, velocity: 0.1, correctness:0.4, simplicity:0.6, flexibility:0.5},
  P5: {scalability:0.0, maintainability:0.5, velocity: 0.4, correctness:0.3, simplicity:0.8, flexibility:0.2},
  P6: {scalability:0.7, maintainability:0.7, velocity:-0.5, correctness:0.3, simplicity:0.4, flexibility:0.7},
  P7: {scalability:0.5, maintainability:0.6, velocity: 0.5, correctness:0.2, simplicity:0.4, flexibility:0.4},
};

// User-friendly names + a one-line "what this means" subtitle for each check.
const PRINCIPLE_LABELS = {
  P1: "Cycles in imports",
  P2: "Stable depending on unstable",
  P3: "Complex functions",
  P4: "Classes doing too many jobs",
  P5: "Code nothing calls",
  P6: "Helper imports orchestrator",
  P7: "Things that change together",
};
const PRINCIPLE_SUBTITLES = {
  P1: "Two modules importing each other (directly or via a chain). Hard to ship one without the other.",
  P2: "A stable module (lots depend on it) depending on an unstable one. Inherits volatility.",
  P3: "Cyclomatic complexity above 15 — too many branches to reason about confidently.",
  P4: "Methods in one class barely share state — sign the class is two classes glued together.",
  P5: "Functions/symbols no execution path reaches. Dead code that lies about the system surface.",
  P6: "A module deep in the package imports back up to the entry point — direction violation.",
  P7: "Files in different packages keep changing together — boundary is mis-cut.",
};

const PRESETS = {
  baseline:        { label: "baseline",       weights: null /* filled per-audit */ },
  velocity:        { label: "velocity-first", weights: { scalability:0.8, maintainability:0.6, velocity:2.5, correctness:0.6, simplicity:1.2, flexibility:0.6 } },
  correctness:     { label: "correctness-first", weights: { scalability:1.0, maintainability:1.2, velocity:0.5, correctness:2.8, simplicity:1.0, flexibility:0.8 } },
  maintainability: { label: "maintainability-first", weights: { scalability:1.0, maintainability:2.5, velocity:0.6, correctness:1.0, simplicity:1.4, flexibility:1.0 } },
};

// --- shared state ---
const state = {
  manifest: null,
  activeSlug: null,
  evidence: null,
  prioritized: null,
  verdicts: [],
  reportMd: "",
  graphJson: null,
  sigma: null,
  baselineWeights: null,
  currentWeights: null,
  dpById: {},
  activeView: "evidence",
  activePreset: "baseline",
};

// =====================================================================
// Math (port of probe.py + prioritize/score.py)
// =====================================================================

function salience(lens, weights) {
  let wNorm = 0;
  for (const k of Object.keys(weights)) wNorm += Math.abs(weights[k] || 0);
  if (wNorm === 0) wNorm = 1;
  let num = 0;
  for (const k of Object.keys(weights)) num += (weights[k] || 0) * (lens?.[k] || 0);
  return num / wNorm;
}

function reweightedAggregate(cells, weights) {
  let debt = 0, just = 0;
  for (const c of cells) {
    const s = salience(c.value_lens, weights);
    if (c.position === "debt") debt += c.confidence * s;
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

function structuralScore(impact) {
  const vals = STRUCTURAL_FEATURES.map(f => +(impact?.[f] ?? 0));
  return vals.reduce((a,b)=>a+b, 0) / vals.length;
}

function valueAffinityScore(principle, weights) {
  const row = AFFINITIES[principle] || {};
  const num = VALUES.reduce((s,k) => s + (weights[k]||0) * (row[k]||0), 0);
  const den = VALUES.reduce((s,k) => s + Math.abs(weights[k]||0), 0) || 1;
  return num / den;
}

function compositeScore(dp, weights) {
  const s  = structuralScore(dp.measured_impact);
  const va = valueAffinityScore(dp.principle, weights);
  return { structural: s, value_affinity: va, composite: s * (1 + 0.5 * va) };
}

// =====================================================================
// Boot
// =====================================================================

async function init() {
  try {
    const res = await fetch("data/manifest.json");
    if (!res.ok) throw new Error(`manifest.json ${res.status}`);
    state.manifest = await res.json();
  } catch (e) {
    document.body.innerHTML = `<div style="padding:40px;color:#ef4444;font-family:sans-serif">
      <h2>UI failed to load</h2><p>Could not fetch <code>data/manifest.json</code>: ${e.message}</p></div>`;
    return;
  }

  renderAuditSwitcher();
  wireNav();
  wireButtons();
  wireClock();

  // Honor #hash for deep links to a specific view.
  const fromHash = (location.hash || "#evidence").replace(/^#/, "");
  state.activeView = ["evidence","prioritization","jury","briefing"].includes(fromHash) ? fromHash : "evidence";

  await loadAudit(state.manifest.default);
}

async function fetchOptional(url, asText = false) {
  try {
    const res = await fetch(url);
    if (!res.ok) return null;
    return asText ? await res.text() : await res.json();
  } catch { return null; }
}

async function loadAudit(slug) {
  const entry = state.manifest.audits.find(a => a.slug === slug);
  if (!entry) return;
  state.activeSlug = slug;
  markAuditActive(slug);

  const base = `data/${slug}`;
  const [evidence, prioritized, verdicts, reportMd, graphJson] = await Promise.all([
    fetchOptional(`${base}/evidence.json`),
    fetchOptional(`${base}/prioritized.json`),
    fetchOptional(`${base}/verdicts.json`),
    fetchOptional(`${base}/report.md`, true),
    fetchOptional(`${base}/graph.json`),
  ]);
  if (!evidence || !prioritized) {
    alert(`Audit "${slug}" is missing data files. Check docs/data/${slug}/.`);
    return;
  }
  state.evidence = evidence;
  state.prioritized = prioritized;
  state.verdicts = verdicts || [];
  state.reportMd = reportMd || "";
  state.graphJson = graphJson || null;
  state.baselineWeights = { ...prioritized.values };
  state.currentWeights = { ...prioritized.values };
  state.dpById = Object.fromEntries(evidence.decision_points.map(d => [d.id, d]));
  PRESETS.baseline.weights = { ...prioritized.values };
  state.activePreset = "baseline";

  // Refresh everything (each view reads from state, switching just shows it).
  renderTopBar(entry);
  renderEvidence();
  renderPrioritization();   // also rebuilds sliders + presets
  renderJury();
  renderBriefing();
  renderFooterStatus(entry);
  switchView(state.activeView);
}

// =====================================================================
// Top bar + footer + sidebar audit switcher
// =====================================================================

function renderTopBar(entry) {
  const commit = entry.commit || (state.evidence.commit_sha || "").slice(0, 8) || "no-git";
  document.getElementById("topbar-commit").textContent  = "commit " + commit;
  document.getElementById("topbar-branch").textContent  = "branch " + (state.evidence.git_summary?.branch || "main");
  document.getElementById("topbar-backend").textContent = backendBlurb();
  document.getElementById("topbar-cost").textContent    = costBlurb();

  // CLI command on Evidence view
  const langFlag = entry.language === "auto" ? "" : ` --language ${entry.language}`;
  document.getElementById("cli-display").value =
    `forum audit ${entry.source || "<repo>"}${langFlag} --top-n ${state.verdicts.length || 5} --cell-backend wafer`;
}

function backendBlurb() {
  // We don't carry per-audit backend metadata yet — infer from cell count
  // (Wafer = 10/10 typical; Anthropic-throttled = 6/10).
  if (!state.verdicts.length) return "no jury";
  const cells = state.verdicts[0].cells || [];
  return cells.length === 10 ? "wafer · qwen3.5" : "anthropic · haiku 4.5";
}

function costBlurb() {
  // Estimate from typical per-tribunal cost; refine if verdicts carry a stats key later.
  const n = state.verdicts.length;
  if (n === 0) return "$0.00";
  // ~$0.32 per Wafer tribunal + $0.30 Opus, approx
  return `$${(0.32 * n + 0.30).toFixed(2)}`;
}

function renderAuditSwitcher() {
  const root = document.getElementById("audit-switcher");
  root.innerHTML = "";
  for (const entry of state.manifest.audits) {
    const btn = document.createElement("div");
    btn.className = "audit-pill";
    btn.dataset.slug = entry.slug;
    btn.innerHTML = `
      <span>${escapeHtml(entry.label)}</span>
      <span class="lang lang-${entry.language}">${entry.language}</span>`;
    btn.title = `${entry.source} @ ${entry.commit}\n\n${entry.note}`;
    btn.addEventListener("click", () => {
      if (state.activeSlug !== entry.slug) loadAudit(entry.slug);
    });
    root.appendChild(btn);
  }
}

function markAuditActive(slug) {
  document.querySelectorAll(".audit-pill").forEach(b =>
    b.classList.toggle("active", b.dataset.slug === slug)
  );
}

function renderFooterStatus(entry) {
  const numDps = state.evidence.decision_points.length;
  const numTrib = state.verdicts.length;
  const reportWords = state.reportMd ? state.reportMd.trim().split(/\s+/).length : 0;
  document.getElementById("footer-layers").textContent =
    `${numDps} findings · top ${state.prioritized.items.length} ranked · ${numTrib} debate${numTrib === 1 ? "" : "s"} · ${reportWords.toLocaleString()}-word report`;
}

function wireClock() {
  const tick = () => {
    const t = new Date().toISOString().slice(11, 19) + " UTC";
    document.getElementById("footer-clock").textContent = t;
  };
  tick();
  setInterval(tick, 1000);
}

function wireNav() {
  document.querySelectorAll(".nav-item").forEach(a => {
    a.addEventListener("click", e => {
      e.preventDefault();
      switchView(a.dataset.view);
    });
  });
}

function switchView(view) {
  state.activeView = view;
  location.hash = view;
  document.querySelectorAll(".view").forEach(s => s.classList.toggle("hidden", s.dataset.view !== view));
  document.querySelectorAll(".nav-item").forEach(a => a.classList.toggle("active", a.dataset.view === view));
}

function wireButtons() {
  // Top-bar WHAT-IF → jump to Prioritization
  document.getElementById("btn-whatif").addEventListener("click", () => switchView("prioritization"));
  // Top-bar download icon → download report.md
  document.getElementById("btn-download").addEventListener("click", downloadReport);
  // Sidebar EXPORT REPORT
  document.getElementById("btn-export").addEventListener("click", downloadReport);
  // Footer DOWNLOAD_BUNDLE
  document.getElementById("btn-bundle").addEventListener("click", downloadBundle);
  // Jury jump-to-action
  document.getElementById("btn-scroll-action").addEventListener("click", () => {
    switchView("briefing");
    setTimeout(() => {
      const el = document.querySelector("#brief-verbatims");
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 80);
  });
  // Reset sliders to baseline
  document.getElementById("btn-reset").addEventListener("click", () => applyPreset("baseline"));
}

function downloadReport() {
  if (!state.reportMd) { alert("No report.md for this audit."); return; }
  const blob = new Blob([state.reportMd], { type: "text/markdown" });
  triggerDownload(blob, `${state.activeSlug}-report.md`);
}

async function downloadBundle() {
  if (!window.JSZip) { alert("JSZip didn't load."); return; }
  const zip = new JSZip();
  if (state.evidence)    zip.file("evidence.json",    JSON.stringify(state.evidence, null, 2));
  if (state.prioritized) zip.file("prioritized.json", JSON.stringify(state.prioritized, null, 2));
  if (state.verdicts)    zip.file("verdicts.json",    JSON.stringify(state.verdicts, null, 2));
  if (state.reportMd)    zip.file("report.md",        state.reportMd);
  if (state.graphJson)   zip.file("graph.json",       JSON.stringify(state.graphJson, null, 2));
  const blob = await zip.generateAsync({ type: "blob" });
  triggerDownload(blob, `${state.activeSlug}-bundle.zip`);
}

function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// =====================================================================
// Evidence view
// =====================================================================

function renderEvidence() {
  const e = state.evidence;
  document.getElementById("ev-language").textContent =
    `${e.decision_points.length} findings · ${(e.git_summary?.commit_sha || "").slice(0,8) || "no-git"}`;

  // Metric cards from real Layer 1 data
  const principlesFound = new Set(e.decision_points.map(d => d.principle));
  const metricsHtml = [];

  // P1 — cycles
  const cycleDp = e.decision_points.find(d => d.principle === "P1");
  const sccSize = cycleDp?.evidence?.scc_size || 0;
  metricsHtml.push(metricBlock({
    label: PRINCIPLE_LABELS.P1,
    sublabel: "P1 · Acyclic Dependencies",
    explainer: PRINCIPLE_SUBTITLES.P1,
    value: sccSize > 0 ? `${sccSize} modules tangled` : "clean",
    pct: Math.min(100, sccSize * 5),
    tone: sccSize > 10 ? "error" : sccSize > 0 ? "tertiary" : "primary",
    note: sccSize > 10 ? "Large cycle — many files locked together" :
          sccSize > 0  ? "Small cycle present" :
                         "No cycles found",
  }));

  // P3 — complexity (peak CC)
  const cxDps = e.decision_points.filter(d => d.principle === "P3");
  const peakCC = Math.max(0, ...cxDps.map(d => d.evidence?.complexity || 0));
  metricsHtml.push(metricBlock({
    label: PRINCIPLE_LABELS.P3,
    sublabel: "P3 · McCabe cyclomatic complexity",
    explainer: PRINCIPLE_SUBTITLES.P3,
    value: peakCC > 0 ? `peak ${peakCC}` : "all under 15",
    pct: Math.min(100, peakCC),
    tone: peakCC > 100 ? "error" : peakCC > 30 ? "tertiary" : "primary",
    note: cxDps.length
      ? `${cxDps.length} function${cxDps.length === 1 ? "" : "s"} above the 15-branch ceiling`
      : "All functions stay under the limit",
  }));

  // P4 — LCOM (peak)
  const lcDps = e.decision_points.filter(d => d.principle === "P4");
  const peakLcom = Math.max(0, ...lcDps.map(d => d.evidence?.lcom || 0));
  metricsHtml.push(metricBlock({
    label: PRINCIPLE_LABELS.P4,
    sublabel: "P4 · LCOM cohesion (Python only)",
    explainer: PRINCIPLE_SUBTITLES.P4,
    value: lcDps.length ? `peak ${peakLcom.toFixed(2)}` : "n/a (or C)",
    pct: peakLcom * 100,
    tone: peakLcom > 0.9 ? "error" : peakLcom > 0.7 ? "tertiary" : "primary",
    note: lcDps.length
      ? `${lcDps.length} class${lcDps.length === 1 ? "" : "es"} above 0.7 (1.0 = methods share zero state)`
      : "Skipped for non-Python repos (C has no classes)",
  }));

  // P5 — dead code
  const dcDps = e.decision_points.filter(d => d.principle === "P5");
  metricsHtml.push(metricBlock({
    label: PRINCIPLE_LABELS.P5,
    sublabel: "P5 · Reachability (vulture / cppcheck)",
    explainer: PRINCIPLE_SUBTITLES.P5,
    value: `${dcDps.length} dead`,
    pct: Math.min(100, dcDps.length * 20),
    tone: dcDps.length > 3 ? "tertiary" : "primary",
    note: dcDps.length
      ? `Detected by ${dcDps[0].evidence?.analyzer || "static checker"}`
      : "No dead code surfaced",
  }));

  document.getElementById("ev-metrics").innerHTML = metricsHtml.join("");

  // Telemetry pane (plain-English labels)
  const gs = e.graph_summary || {};
  document.getElementById("ev-telemetry").innerHTML = `
    <div class="flex justify-between"><span>Files analyzed</span><span class="text-primary">${gs.num_modules ?? "?"}</span></div>
    <div class="flex justify-between"><span>Imports between files</span><span class="text-primary">${gs.num_edges ?? "?"}</span></div>
    <div class="flex justify-between"><span>Top-level packages</span><span class="text-primary">${gs.num_packages ?? "?"}</span></div>
    <div class="flex justify-between"><span>Checks that flagged</span><span class="text-primary">${[...principlesFound].sort().join(", ") || "none"}</span></div>
    <div class="flex justify-between"><span>Commits last 12 months</span><span class="text-primary">${e.git_summary?.recent_commits ?? "?"}</span></div>`;

  document.getElementById("ev-graph-stats").textContent =
    `${gs.num_modules ?? "?"} files · ${gs.num_edges ?? "?"} imports`;
  renderDependencyGraph();
}

// =====================================================================
// Dependency graph (sigma v3 + graphology + forceatlas2)
// =====================================================================

// Tailwind-aligned palette for per-package coloring; cycles for repos with
// many top-level packages.
const PKG_COLORS = [
  "#67e8f9", "#facc15", "#fb923c", "#c084fc",
  "#4ade80", "#f87171", "#60a5fa", "#fbbf24",
];

function renderDependencyGraph() {
  const container = document.getElementById("ev-graph-wrap");
  if (!container) return;

  // Tear down any previous sigma instance — loadAudit may be called repeatedly.
  if (state.sigma) {
    state.sigma.kill();
    state.sigma = null;
  }
  container.innerHTML = "";

  const data = state.graphJson;
  if (!data || !data.nodes?.length) {
    container.innerHTML =
      `<div style="padding:24px;color:#919094;text-align:center">No dependency graph on disk for this audit.</div>`;
    return;
  }

  const graph = new Graph({ multi: false, type: "directed" });

  // Assign a stable color per top-level package.
  const pkgOrder = [...new Set(data.nodes.map(n => n.pkg))];
  const colorFor = pkg => PKG_COLORS[pkgOrder.indexOf(pkg) % PKG_COLORS.length];

  // Node size scales with in-degree (fan-in): widely-depended-on modules pop.
  const fanIn = new Map();
  for (const e of data.edges) fanIn.set(e.target, (fanIn.get(e.target) || 0) + 1);

  for (const n of data.nodes) {
    graph.addNode(n.id, {
      label: n.label || n.id,
      x: n.x ?? Math.random(),
      y: n.y ?? Math.random(),
      size: 3 + Math.min(12, (fanIn.get(n.id) || 0) * 0.8),
      color: colorFor(n.pkg),
      pkg: n.pkg,
      fullId: n.id,
    });
  }
  for (const e of data.edges) {
    if (graph.hasNode(e.source) && graph.hasNode(e.target) &&
        !graph.hasEdge(e.source, e.target)) {
      graph.addEdge(e.source, e.target, {
        size: 0.6,
        color: "rgba(199,198,202,0.25)",
        type: "curved",
      });
    }
  }

  // ForceAtlas2 settles the warm-start positions from Graphviz into a
  // physically-meaningful layout — same algorithm gitnexus-web uses.
  const settings = forceAtlas2.inferSettings(graph);
  forceAtlas2.assign(graph, {
    iterations: 200,
    settings: { ...settings, gravity: 1, scalingRatio: 8, slowDown: 4 },
  });

  state.sigma = new Sigma(graph, container, {
    renderEdgeLabels: false,
    labelColor: { color: "#c7c6ca" },
    labelSize: 11,
    labelFont: "JetBrains Mono",
    labelWeight: "400",
    defaultEdgeType: "curved",
    edgeProgramClasses: { curved: EdgeCurvedArrowProgram },
    minCameraRatio: 0.1,
    maxCameraRatio: 6,
    labelRenderedSizeThreshold: 6,
  });

  // Hover highlights the node + its 1-hop neighborhood.
  let hovered = null;
  state.sigma.on("enterNode", ({ node }) => { hovered = node; state.sigma.refresh(); });
  state.sigma.on("leaveNode", () => { hovered = null; state.sigma.refresh(); });

  state.sigma.setSetting("nodeReducer", (node, attrs) => {
    if (!hovered) return attrs;
    const neighbors = new Set(graph.neighbors(hovered));
    neighbors.add(hovered);
    if (neighbors.has(node)) return attrs;
    return { ...attrs, color: "#2b2a2a", label: "", zIndex: 0 };
  });
  state.sigma.setSetting("edgeReducer", (edge, attrs) => {
    if (!hovered) return attrs;
    const [s, t] = graph.extremities(edge);
    if (s === hovered || t === hovered) {
      return { ...attrs, color: "rgba(200,198,199,0.9)", size: 1.4, zIndex: 1 };
    }
    return { ...attrs, color: "rgba(70,70,74,0.2)" };
  });
}

function metricBlock({ label, sublabel, explainer, value, pct, tone, note }) {
  const barColor = ({ error: "#ef4444", tertiary: "#fb923c", primary: "#c8c6c7" })[tone] || "#c8c6c7";
  return `
    <div class="space-y-1.5">
      <div class="flex justify-between items-end">
        <div>
          <div class="font-body-md text-on-surface">${escapeHtml(label)}</div>
          <div class="font-code-sm text-[10px] text-on-surface-variant opacity-70">${escapeHtml(sublabel)}</div>
        </div>
        <span class="font-code-md text-primary font-bold whitespace-nowrap ml-3">${escapeHtml(value)}</span>
      </div>
      <p class="text-[11px] text-on-surface-variant opacity-70 leading-snug">${escapeHtml(explainer)}</p>
      <div class="h-1 bg-surface-container-highest w-full overflow-hidden">
        <div class="h-full" style="width:${Math.min(100, pct).toFixed(0)}%;background:${barColor}"></div>
      </div>
      <p class="text-[10px] font-code-sm" style="color:${barColor}">${escapeHtml(note)}</p>
    </div>`;
}

// =====================================================================
// Prioritization view (rankings + sliders)
// =====================================================================

function renderPrioritization() {
  renderPresets();
  renderSliders();
  renderRanking();
  document.getElementById("prio-stat-dps").textContent = state.prioritized.items.length;
}

function renderPresets() {
  const root = document.getElementById("prio-presets");
  root.innerHTML = "";
  for (const [key, p] of Object.entries(PRESETS)) {
    const btn = document.createElement("button");
    btn.className = "tone-btn" + (key === state.activePreset ? " active" : "");
    btn.textContent = p.label;
    btn.dataset.preset = key;
    btn.addEventListener("click", () => applyPreset(key));
    root.appendChild(btn);
  }
}

function applyPreset(key) {
  state.activePreset = key;
  const target = key === "baseline" ? state.baselineWeights : PRESETS[key].weights;
  state.currentWeights = { ...target };
  // Refresh sliders to new values
  for (const v of VALUES) {
    const input = document.querySelector(`#prio-sliders input[data-name="${v}"]`);
    if (input) input.value = state.currentWeights[v];
    updateSliderLabel(v);
  }
  // Update preset highlights everywhere
  document.querySelectorAll(".tone-btn").forEach(b => b.classList.toggle("active", b.dataset.preset === key));
  refreshAfterSlider();
}

function renderSliders() {
  const root = document.getElementById("prio-sliders");
  root.innerHTML = "";
  for (const v of VALUES) {
    const baseline = state.baselineWeights[v] ?? 1.0;
    const current  = state.currentWeights[v] ?? baseline;
    const row = document.createElement("div");
    row.className = "space-y-1";
    row.innerHTML = `
      <div class="flex justify-between text-[11px] font-code-sm">
        <span class="text-on-surface-variant uppercase">${v}</span>
        <span class="text-primary" data-val="${v}">${current.toFixed(2)}</span>
      </div>
      <input type="range" min="0" max="3" step="0.05" value="${current}" data-name="${v}"
             class="w-full h-1 bg-surface-container-highest appearance-none cursor-pointer accent-primary border-none p-0 focus:ring-0">
      <div class="text-[9px] text-on-surface-variant opacity-60">baseline ${baseline.toFixed(2)}</div>
    `;
    root.appendChild(row);
    row.querySelector("input").addEventListener("input", e => {
      const name = e.target.dataset.name;
      state.currentWeights[name] = +e.target.value;
      state.activePreset = "custom";
      document.querySelectorAll(".tone-btn").forEach(b => b.classList.remove("active"));
      updateSliderLabel(name);
      refreshAfterSlider();
    });
  }
}

function updateSliderLabel(name) {
  const el = document.querySelector(`#prio-sliders [data-val="${name}"]`);
  if (!el) return;
  const v = state.currentWeights[name];
  const b = state.baselineWeights[name];
  el.textContent = v.toFixed(2);
  el.style.color = v > b + 0.05 ? "#4ade80" : v < b - 0.05 ? "#fb923c" : "#c8c6c7";
}

let lastRanking = null;

function renderRanking() {
  const root = document.getElementById("prio-ranking");
  // Re-score only the originally-prioritized set; live composite under current weights.
  const originalIds = new Set(state.prioritized.items.map(i => i.decision_point_id));
  const scored = state.evidence.decision_points
    .filter(dp => originalIds.has(dp.id))
    .map(dp => {
      const c = compositeScore(dp, state.currentWeights);
      return { dp, ...c };
    });
  scored.sort((a, b) => b.composite - a.composite);

  root.innerHTML = "";
  let nShifted = 0;
  scored.forEach((row, idx) => {
    const oldRank = lastRanking ? lastRanking.indexOf(row.dp.id) : idx;
    const delta = lastRanking ? oldRank - idx : 0;
    if (delta !== 0) nShifted += 1;
    const principleName = PRINCIPLE_LABELS[row.dp.principle] || row.dp.principle;
    const deltaTag = delta > 0
      ? `<span class="text-[#4ade80] text-[10px] ml-2" title="Moved up ${delta} place${delta === 1 ? "" : "s"}">↑${delta}</span>`
      : delta < 0
        ? `<span class="text-[#fb923c] text-[10px] ml-2" title="Moved down ${Math.abs(delta)} place${Math.abs(delta) === 1 ? "" : "s"}">↓${Math.abs(delta)}</span>`
        : "";
    const div = document.createElement("div");
    div.className = "bg-surface-container border border-outline-variant p-4 transition-all" + (delta !== 0 ? " ring-1 ring-primary/30" : "");
    div.innerHTML = `
      <div class="flex items-start justify-between gap-4 mb-3">
        <div class="flex items-baseline gap-3">
          <span class="font-code-md text-primary font-bold">#${idx + 1}</span>
          <div>
            <div class="font-body-md text-on-surface">${escapeHtml(row.dp.subject)}</div>
            <div class="font-code-sm text-on-surface-variant text-[11px] mt-1">
              <span title="${escapeHtml(PRINCIPLE_SUBTITLES[row.dp.principle] || "")}">${escapeHtml(principleName)} (${row.dp.principle})</span>
            </div>
          </div>
        </div>
        <div class="text-right">
          <div class="font-code-md text-primary font-bold" title="Composite score = structural × (1 + 0.5 × value-affinity)">${row.composite.toFixed(3)}${deltaTag}</div>
          <div class="font-code-sm text-on-surface-variant text-[10px]" title="Raw severity from the static checks">raw severity ${row.structural.toFixed(3)}</div>
          <div class="font-code-sm text-on-surface-variant text-[10px]" title="How much your priorities care about this kind of issue (-1 to +1)">priority match ${row.value_affinity >= 0 ? "+" : ""}${row.value_affinity.toFixed(3)}</div>
        </div>
      </div>
      <div class="flex gap-1 h-1" title="Visual width = composite score">
        <div class="bg-primary" style="flex:${row.composite}"></div>
        <div class="bg-surface-container-highest" style="flex:${Math.max(0, 1.5 - row.composite)}"></div>
      </div>`;
    root.appendChild(div);
  });
  lastRanking = scored.map(s => s.dp.id);
  document.getElementById("prio-stat-shift").textContent = nShifted;
}

function refreshAfterSlider() {
  renderRanking();
  renderJuryAggregates();  // re-projected verdict line per tribunal updates too
  renderBriefingSummary(); // verdict-distribution card might shift
}

// =====================================================================
// AI Jury view
// =====================================================================

function renderJury() {
  const root = document.getElementById("jury-cells");
  const judgeRoot = document.getElementById("jury-judges");
  if (!state.verdicts.length) {
    root.innerHTML = `<div class="text-center text-on-surface-variant p-12">No Layer-2 verdicts on disk for this audit.</div>`;
    judgeRoot.innerHTML = "";
    document.getElementById("jury-stat-tribunals").textContent = "0";
    document.getElementById("jury-stat-cells").textContent = "0";
    document.getElementById("jury-stat-overrides").textContent = "0";
    return;
  }

  root.innerHTML = "";
  judgeRoot.innerHTML = "";
  let totalCells = 0, overrides = 0;

  state.verdicts.forEach((trib, tribIdx) => {
    const dp = state.dpById[trib.decision_point_id];
    const cells = trib.cells || [];
    const judge = trib.judge || {};
    totalCells += cells.length;
    if (judge.override) overrides += 1;

    // ---- Tribunal header ----
    const header = document.createElement("div");
    header.className = "mb-3 mt-6 first:mt-0";
    header.innerHTML = `
      <div class="flex items-baseline justify-between flex-wrap gap-2 pb-3 border-b border-outline-variant">
        <div>
          <span class="font-label-caps text-label-caps text-primary">TRIBUNAL #${tribIdx + 1}</span>
          <span class="font-code-sm text-on-surface-variant ml-2">${dp?.principle ?? "?"}</span>
        </div>
        <div class="text-right">
          <div class="font-code-md text-on-surface">${escapeHtml(dp?.subject || trib.decision_point_id)}</div>
        </div>
      </div>`;
    root.appendChild(header);

    // ---- Cells grid ----
    const grid = document.createElement("div");
    grid.className = "grid grid-cols-1 md:grid-cols-2 gap-3 mb-2";
    grid.dataset.tribunalId = trib.decision_point_id;
    cells.forEach(c => grid.appendChild(renderCellCard(c)));
    root.appendChild(grid);

    // ---- Aggregate line (re-projected under current weights) ----
    const aggLine = document.createElement("div");
    aggLine.className = "mb-6 text-[11px] font-code-sm text-on-surface-variant";
    aggLine.dataset.aggregateFor = trib.decision_point_id;
    root.appendChild(aggLine);

    // ---- Judge card in right panel ----
    judgeRoot.appendChild(renderJudgeCard(trib, tribIdx, dp));
  });

  document.getElementById("jury-stat-tribunals").textContent = state.verdicts.length;
  document.getElementById("jury-stat-cells").textContent = totalCells;
  document.getElementById("jury-stat-overrides").textContent = overrides;

  renderJuryAggregates();
}

// Maps persona id → display name + the single value it cares about.
// Used to label cell cards now that personas are monomaniacal and
// neither persona is locked to a side.
const PERSONA_INFO = {
  simplifier:  { name: "The Simplifier", value: "simplicity",       color: "#a78bfa" },
  shipper:     { name: "The Shipper",    value: "velocity",         color: "#fb923c" },
  maintainer:  { name: "The Maintainer", value: "maintainability",  color: "#5dd6ff" },
  verifier:    { name: "The Verifier",   value: "correctness",      color: "#4ade80" },
  scaler:      { name: "The Scaler",     value: "scalability",      color: "#f472b6" },
  adapter:     { name: "The Adapter",    value: "flexibility",      color: "#facc15" },
};

function personaPill(personaId) {
  const info = PERSONA_INFO[personaId] || { name: personaId, value: "?", color: "#919094" };
  return `<span class="inline-flex items-baseline gap-1.5 px-2 py-0.5 border rounded-sm"
                style="color:${info.color};border-color:${info.color};background:${info.color}14;"
                title="Cares only about ${info.value}.">
            <span class="font-code-sm text-[11px] font-bold">${escapeHtml(info.name)}</span>
            <span class="text-[9px] opacity-70">${info.value}</span>
          </span>`;
}

function renderCellCard(c) {
  // New model: two personas debate, the CELL votes. Vote color reflects the
  // cell's conclusion (debt=orange, justified=green) — NOT the persona's
  // "side" since neither persona has a side anymore.
  const voteIsDebt = c.position === "debt";
  const voteColor  = voteIsDebt ? "#fb923c" : "#4ade80";

  // red_persona/blue_persona field names are kept for schema compat; they're
  // now just "persona A" and "persona B" labels — neither is prosecution/defense.
  const personaAId = c.red_persona;
  const personaBId = c.blue_persona;
  const confidencePct = Math.round((c.confidence || 0) * 100);

  // Salience under current weights
  const b = salience(c.value_lens, state.baselineWeights);
  const n = salience(c.value_lens, state.currentWeights);
  const ratio = b > 0 ? n / b : (n > 0 ? INFINITY_SENTINEL : 1);
  const salient = ratio >= SALIENCE_BUMP;
  const ratioStr = ratio >= INFINITY_SENTINEL ? "∞" : `${ratio.toFixed(2)}×`;

  const voteExplainer = voteIsDebt
    ? "Cell concluded: this is real debt — the personas' reading converged on harm."
    : "Cell concluded: this is justified — the personas' reading converged on serving / defensible.";
  const salienceTitle = salient
    ? `This cell's reasoning rests on values your sliders weight highly. Your priorities care ${ratioStr} more about this argument than the baseline.`
    : "How much your current priorities overlap with the values this cell's argument rests on. 1.00× = baseline.";

  const card = document.createElement("div");
  card.className = "bg-surface-container border border-outline-variant" + (salient ? " ring-1 ring-yellow-400/40" : "");
  card.innerHTML = `
    <div class="h-1 w-full" style="background:${voteColor}"></div>
    <div class="p-4">
      <div class="flex justify-between items-start mb-3 gap-3">
        <div>
          <h3 class="font-code-md text-on-surface font-bold mb-1">Cell ${c.cell_id}</h3>
          <div class="flex items-center gap-1.5 flex-wrap text-[10px]">
            ${personaPill(personaAId)}
            <span class="text-on-surface-variant opacity-60">vs</span>
            ${personaPill(personaBId)}
          </div>
        </div>
        <span class="font-label-caps text-[9px] px-2 py-0.5 border whitespace-nowrap"
              style="color:${voteColor};border-color:${voteColor};background:${voteColor}1a"
              title="${escapeHtml(voteExplainer)}">
          VOTE: ${voteIsDebt ? "DEBT" : "JUSTIFIED"}
        </span>
      </div>
      <!-- Confidence bar -->
      <div class="flex items-center gap-2 mb-3 bg-surface-container-high p-2 border border-outline-variant/30" title="How sure this cell was of its vote (0% = toss-up, 100% = decisive)">
        <div class="flex-1 h-1.5 bg-surface-variant relative overflow-hidden">
          <div class="absolute left-0 top-0 h-full" style="width:${confidencePct}%;background:${voteColor}"></div>
        </div>
        <span class="font-code-sm text-[10px]" style="color:${voteColor}">${confidencePct}% sure</span>
      </div>
      <div class="font-code-sm text-[11px] text-on-surface leading-relaxed italic">
        "${escapeHtml(c.key_argument || "(no argument)")}"
      </div>
      <div class="mt-3 pt-2 border-t border-outline-variant/30 text-[10px] font-code-sm text-on-surface-variant flex items-center justify-between"
           title="${escapeHtml(salienceTitle)}">
        <span>matches your priorities</span>
        <span class="${salient ? "text-yellow-400 font-bold" : ""}">${ratioStr}${salient ? " — emphasized" : ""}</span>
      </div>
    </div>`;
  return card;
}

function renderJudgeCard(trib, tribIdx, dp) {
  const judge = trib.judge || {};
  const v = String(judge.verdict || "—").toUpperCase();
  const vKey = v.replace(/ /g, "-");
  const verdictExplainer = ({
    "HEALTHY":              "Decision is sound — no action needed.",
    "JUSTIFIED VIOLATION":  "Yes it violates the principle, but the violation is defensible.",
    "STRUCTURAL DEBT":      "Real debt with observable cost. Refactor warranted.",
    "CRITICAL":             "Actively causing or about to cause production impact. Urgent.",
    "DRIFTED":              "Design was sound; code has drifted away. Restore it.",
    "CONTESTED":            "Panel split too hard to call — a human architect should weigh in.",
  })[v] || "";

  const card = document.createElement("div");
  card.className = `bg-surface-container-low border border-outline-variant p-4`;
  card.innerHTML = `
    <div class="flex justify-between items-center mb-2 flex-wrap gap-2">
      <span class="font-label-caps text-[10px] text-on-tertiary-container">FINDING #${tribIdx + 1}${judge.override ? " · JUDGE OVERRODE PANEL" : ""}</span>
      <span class="font-code-sm font-bold verdict-${vKey} verdict-bg-${vKey} px-2 py-0.5"
            title="${escapeHtml(verdictExplainer)}">${escapeHtml(v)}</span>
    </div>
    <div class="font-code-sm text-on-surface-variant text-[10px] mb-3 truncate" title="${escapeHtml(dp?.subject || "")}">${escapeHtml(dp?.subject || trib.decision_point_id)}</div>
    ${verdictExplainer ? `<div class="text-[11px] text-on-surface-variant opacity-70 mb-3 leading-snug">${escapeHtml(verdictExplainer)}</div>` : ""}
    <div class="font-body-md text-on-surface leading-relaxed text-[13px]">
      <span class="font-label-caps text-[9px] text-on-tertiary-container">REASONING</span><br>
      ${escapeHtml(judge.reasoning || "(no reasoning)")}
    </div>
    ${judge.dissent_summary ? `
      <div class="mt-3 pt-3 border-t border-outline-variant/30">
        <div class="font-label-caps text-[9px] text-on-tertiary-container mb-1">STRONGEST DISSENT (the losing side's best point)</div>
        <div class="font-code-sm text-on-surface-variant text-[11px] italic">${escapeHtml(judge.dissent_summary)}</div>
      </div>` : ""}`;
  return card;
}

function renderJuryAggregates() {
  // Update each tribunal's "Re-projected aggregate" line under current weights.
  document.querySelectorAll("[data-aggregate-for]").forEach(line => {
    const tribunalId = line.dataset.aggregateFor;
    const trib = state.verdicts.find(t => t.decision_point_id === tribunalId);
    if (!trib) return;
    const cells = trib.cells || [];
    const orig = trib.aggregate_vote || {};
    const proj = reweightedAggregate(cells, state.currentWeights);
    const wouldFlip = proj.winner && orig.winner && proj.winner !== orig.winner;
    line.innerHTML = `
      <span class="text-on-surface-variant">Actual panel:</span>
      <b class="text-on-surface">${orig.n_debt ?? 0} voted DEBT · ${orig.n_justified ?? 0} voted JUSTIFIED</b>
      &nbsp;·&nbsp;
      <span class="text-on-surface-variant">If we'd weighted by your priorities:</span>
      <b style="color:${proj.winner === "debt" ? "#fb923c" : "#4ade80"}">${proj.winner === "debt" ? "would lean DEBT" : proj.winner === "justified" ? "would lean JUSTIFIED" : "—"}</b>
      ${wouldFlip ? `<span class="ml-2 text-yellow-400 font-bold">— PANEL VOTE WOULD HAVE FLIPPED</span>` : `<span class="ml-2 opacity-60">— matches the actual panel direction (the verdict label never changes regardless)</span>`}
    `;
  });
}

// =====================================================================
// Briefing view
// =====================================================================

function renderBriefing() {
  // Tone switcher
  const toneRoot = document.getElementById("brief-tones");
  toneRoot.innerHTML = "";
  for (const [key, p] of Object.entries(PRESETS)) {
    const b = document.createElement("button");
    b.className = "tone-btn" + (key === state.activePreset ? " active" : "");
    b.dataset.preset = key;
    b.textContent = p.label;
    b.addEventListener("click", () => applyPreset(key));
    toneRoot.appendChild(b);
  }

  renderBriefingSummary();
  renderBriefingBody();
  renderBriefingVerbatims();

  const stamp = new Date().toISOString().replace("T", " ").slice(0, 19);
  document.getElementById("brief-report-id").textContent = `REPORT_ID: ${state.activeSlug.toUpperCase()}-${state.evidence.commit_sha?.slice(0,6) || "—"}`;
  document.getElementById("brief-words").textContent = `${state.reportMd ? state.reportMd.trim().split(/\s+/).length : 0} WORDS`;
  document.getElementById("brief-stamp").textContent = stamp;
  document.getElementById("brief-watermark-stamp").textContent = `GEN_STAMP: ${stamp}`;
  document.getElementById("brief-watermark-sig").textContent = `SIGNATURE: ${(state.evidence.commit_sha || "00000000").slice(0,8)}…`;
}

function renderBriefingSummary() {
  // Counts of verdicts in current cache (rule: verdict text never changes
  // with sliders; only the re-projected aggregate winner can hypothetically
  // shift — counts here always reflect the literal judge output).
  const verdictCounts = {};
  for (const t of state.verdicts) {
    const v = (t.judge?.verdict || "—");
    verdictCounts[v] = (verdictCounts[v] || 0) + 1;
  }
  const critical = verdictCounts["CRITICAL"] || 0;
  const debt     = verdictCounts["STRUCTURAL DEBT"] || 0;
  const just     = (verdictCounts["JUSTIFIED VIOLATION"] || 0);

  // Confidence avg (mean of all cell confidences across all tribunals)
  let confSum = 0, confN = 0;
  for (const t of state.verdicts) {
    for (const c of (t.cells || [])) { confSum += (c.confidence || 0); confN += 1; }
  }
  const meanConf = confN ? confSum / confN : 0;

  document.getElementById("brief-summary").innerHTML = `
    <div class="bg-surface-container border border-outline-variant p-6" style="border-top:2px solid #ef4444"
         title="Verdicts marked CRITICAL by the judge — actively causing or about to cause production impact.">
      <div class="flex items-center justify-between mb-2">
        <span class="font-label-caps text-label-caps text-[#ef4444]">CRITICAL</span>
        <span class="material-symbols-outlined text-[#ef4444]">dangerous</span>
      </div>
      <div class="text-4xl font-headline-lg text-on-surface">${String(critical).padStart(2,"0")}</div>
      <p class="text-[11px] text-on-surface-variant opacity-70 mt-2 leading-snug">
        Urgent — refactor right away
      </p>
      <div class="mt-3 h-1 bg-surface-container-highest">
        <div class="h-full bg-[#ef4444]" style="width:${Math.min(100, critical * 50)}%"></div>
      </div>
    </div>
    <div class="bg-surface-container border border-outline-variant p-6" style="border-top:2px solid #fb923c"
         title="Verdicts marked STRUCTURAL DEBT — real cost, refactor warranted but not urgent.">
      <div class="flex items-center justify-between mb-2">
        <span class="font-label-caps text-label-caps text-[#fb923c]">STRUCTURAL DEBT</span>
        <span class="material-symbols-outlined text-[#fb923c]">warning</span>
      </div>
      <div class="text-4xl font-headline-lg text-on-surface">${String(debt).padStart(2,"0")}</div>
      <p class="text-[11px] text-on-surface-variant opacity-70 mt-2 leading-snug">
        Real debt — refactor on the roadmap
      </p>
      <div class="mt-3 h-1 bg-surface-container-highest">
        <div class="h-full bg-[#fb923c]" style="width:${Math.min(100, debt * 25)}%"></div>
      </div>
      ${just ? `<p class="text-[10px] text-on-surface-variant mt-2 opacity-70">+${just} JUSTIFIED VIOLATION (judge says leave it alone)</p>` : ""}
    </div>
    <div class="bg-surface-container border border-outline-variant p-6" style="border-top:2px solid #c8c6c7"
         title="Average confidence across all ${confN} cell votes. High = the panel was decisive.">
      <div class="flex items-center justify-between mb-2">
        <span class="font-label-caps text-label-caps text-primary">PANEL CONFIDENCE</span>
        <span class="material-symbols-outlined text-primary">bolt</span>
      </div>
      <div class="text-4xl font-headline-lg text-on-surface">${(meanConf * 100).toFixed(0)}%</div>
      <p class="text-[11px] text-on-surface-variant opacity-70 mt-2 leading-snug">
        Average over ${confN} cell votes across ${state.verdicts.length} finding${state.verdicts.length === 1 ? "" : "s"}
      </p>
      <div class="mt-3 h-1 bg-surface-container-highest">
        <div class="h-full bg-primary" style="width:${meanConf * 100}%"></div>
      </div>
    </div>`;
}

function renderBriefingBody() {
  if (!window.marked) return;
  marked.setOptions({ breaks: false, gfm: true });
  let html = marked.parse(state.reportMd || "_(no Layer-3 briefing on disk)_");
  // Wrap literal verdict labels in colored chips.
  html = html.replace(
    /<strong>\s*Verdict:\s*([A-Z][A-Z ]+[A-Z])\s*<\/strong>/g,
    (_, v) => `<span class="verdict-tag verdict-${v.replace(/ /g, "-")} verdict-bg-${v.replace(/ /g, "-")}">${v}</span>`
  );
  document.getElementById("brief-markdown").innerHTML = html;
}

function renderBriefingVerbatims() {
  const root = document.getElementById("brief-verbatims");
  if (!state.verdicts.length) { root.innerHTML = ""; return; }
  root.innerHTML = `
    <h3 class="font-headline-sm text-headline-sm text-primary uppercase tracking-widest flex items-center gap-2 mt-4">
      <span class="w-4 h-[2px] bg-primary"></span> What the judge actually recommended
    </h3>
    <p class="text-[12px] text-on-surface-variant opacity-70 mt-1 mb-4 leading-snug">
      The judge model wrote one specific next step per finding — quoted exactly as it appeared.
    </p>`;
  state.verdicts.forEach((trib, idx) => {
    const judge = trib.judge || {};
    const v = String(judge.verdict || "—").toUpperCase();
    const vKey = v.replace(/ /g, "-");
    const dp = state.dpById[trib.decision_point_id];
    const block = document.createElement("div");
    block.className = "bg-surface-container-low p-6 my-4";
    block.style.borderLeft = "4px solid";
    block.style.borderLeftColor = ({
      "HEALTHY": "#4ade80",
      "JUSTIFIED VIOLATION": "#facc15",
      "STRUCTURAL DEBT": "#fb923c",
      "CRITICAL": "#ef4444",
      "DRIFTED": "#c084fc",
      "CONTESTED": "#67e8f9",
    })[v] || "#c8c6c7";
    block.innerHTML = `
      <div class="flex items-center gap-2 mb-4 flex-wrap">
        <span class="font-label-caps text-label-caps verdict-bg-${vKey} verdict-${vKey} px-2 py-1">${escapeHtml(v)}</span>
        ${judge.override ? `<span class="font-label-caps text-[10px] text-yellow-400 bg-yellow-900/20 px-2 py-1">OVERRIDE</span>` : ""}
        <span class="text-on-surface-variant font-code-sm">DP ${idx + 1}/${state.verdicts.length} · ${escapeHtml(trib.decision_point_id)}</span>
      </div>
      <div class="font-code-sm text-on-surface-variant text-[11px] mb-3">${escapeHtml(dp?.subject || "(no subject)")}</div>
      <blockquote class="m-0 border-none p-0 text-on-surface italic font-code-md leading-relaxed">
        "${escapeHtml(judge.recommended_action || "(no recommended action)")}"
      </blockquote>`;
    root.appendChild(block);
  });
}

// =====================================================================
// Util
// =====================================================================

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

document.addEventListener("DOMContentLoaded", init);
