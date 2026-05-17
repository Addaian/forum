/* Forum frontend — 4-view SPA.
 *
 * Loads docs/data/manifest.json → user picks an audit → loads that audit's
 * artifacts → renders Evidence, Prioritization, AI Jury, Briefing views.
 * Sliders re-project rankings + dissent salience in the browser via a JS
 * port of `src/forum/whatif/probe.py` math. Zero LLM calls; the page is
 * static-hostable on GitHub Pages.
 *
 * Dependency graph uses 3d-force-graph (Three.js + WebGL) with bloom
 * post-processing for a neon glow effect.
 */

// 3d-force-graph loaded via <script> tag in index.html (UMD global: ForceGraph3D)
// Import Three.js for custom node labels (sprites).
import * as THREE_REF from "https://esm.sh/three@0.175.0";

const VALUES = [
  "scalability",
  "maintainability",
  "velocity",
  "correctness",
  "simplicity",
  "flexibility",
];
const SALIENCE_BUMP = 1.1;
const INFINITY_SENTINEL = 999;
const STRUCTURAL_FEATURES = [
  "blast_radius",
  "recency",
  "principle_severity",
  "pattern_violation",
  "advocate_absence",
];

// Mirrors src/forum/values/affinities.yaml — keep in sync.
const AFFINITIES = {
  P1: {
    scalability: 0.6,
    maintainability: 0.8,
    velocity: -0.6,
    correctness: 0.3,
    simplicity: 0.5,
    flexibility: 0.4,
  },
  P2: {
    scalability: 0.4,
    maintainability: 0.7,
    velocity: -0.5,
    correctness: 0.1,
    simplicity: 0.3,
    flexibility: 0.7,
  },
  P3: {
    scalability: 0.1,
    maintainability: 0.9,
    velocity: 0.6,
    correctness: 0.8,
    simplicity: 0.9,
    flexibility: 0.3,
  },
  P4: {
    scalability: 0.2,
    maintainability: 0.7,
    velocity: 0.1,
    correctness: 0.4,
    simplicity: 0.6,
    flexibility: 0.5,
  },
  P5: {
    scalability: 0.0,
    maintainability: 0.5,
    velocity: 0.4,
    correctness: 0.3,
    simplicity: 0.8,
    flexibility: 0.2,
  },
  P6: {
    scalability: 0.7,
    maintainability: 0.7,
    velocity: -0.5,
    correctness: 0.3,
    simplicity: 0.4,
    flexibility: 0.7,
  },
  P7: {
    scalability: 0.5,
    maintainability: 0.6,
    velocity: 0.5,
    correctness: 0.2,
    simplicity: 0.4,
    flexibility: 0.4,
  },
};

// Plain-English subtitles for each verdict label. Used wherever a verdict
// is shown in the UI; the labels themselves are jargon the judge writes
// verbatim and can't be changed at the source.
const VERDICT_PLAIN = {
  "HEALTHY":              "No problem found — don't touch it.",
  "JUSTIFIED VIOLATION":  "Yes there's a textbook issue, but it's defensible. Leave it alone.",
  "STRUCTURAL DEBT":      "Real problem, real cost. Worth refactoring.",
  "CRITICAL":             "Real problem actively hurting you. Refactor urgently.",
  "DRIFTED":              "Original design was sound; code wandered away. Restore the design.",
  "CONTESTED":            "Panel split too badly — a human architect should weigh in.",
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
  baseline: { label: "baseline", weights: null /* filled per-audit */ },
  velocity: {
    label: "velocity-first",
    weights: {
      scalability: 0.8,
      maintainability: 0.6,
      velocity: 2.5,
      correctness: 0.6,
      simplicity: 1.2,
      flexibility: 0.6,
    },
  },
  correctness: {
    label: "correctness-first",
    weights: {
      scalability: 1.0,
      maintainability: 1.2,
      velocity: 0.5,
      correctness: 2.8,
      simplicity: 1.0,
      flexibility: 0.8,
    },
  },
  maintainability: {
    label: "maintainability-first",
    weights: {
      scalability: 1.0,
      maintainability: 2.5,
      velocity: 0.6,
      correctness: 1.0,
      simplicity: 1.4,
      flexibility: 1.0,
    },
  },
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
  forceGraph: null,
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
  for (const k of Object.keys(weights))
    num += (weights[k] || 0) * (lens?.[k] || 0);
  return num / wNorm;
}

function reweightedAggregate(cells, weights) {
  let debt = 0,
    just = 0;
  for (const c of cells) {
    const s = salience(c.value_lens, weights);
    if (c.position === "debt") debt += c.confidence * s;
    else if (c.position === "justified") just += c.confidence * s;
  }
  const total = debt + just;
  if (total === 0) return { winner: null, debt: 0, just: 0, margin: 0 };
  return {
    winner: debt > just ? "debt" : "justified",
    debt,
    just,
    margin: Math.abs(debt - just) / total,
  };
}

function structuralScore(impact) {
  const vals = STRUCTURAL_FEATURES.map((f) => +(impact?.[f] ?? 0));
  return vals.reduce((a, b) => a + b, 0) / vals.length;
}

function valueAffinityScore(principle, weights) {
  const row = AFFINITIES[principle] || {};
  const num = VALUES.reduce((s, k) => s + (weights[k] || 0) * (row[k] || 0), 0);
  const den = VALUES.reduce((s, k) => s + Math.abs(weights[k] || 0), 0) || 1;
  return num / den;
}

function compositeScore(dp, weights) {
  const s = structuralScore(dp.measured_impact);
  const va = valueAffinityScore(dp.principle, weights);
  return { structural: s, value_affinity: va, composite: s * (1 + 0.5 * va) };
}

// =====================================================================
// Boot
// =====================================================================

async function init() {
  try {
    // cache:"no-store" — when the live-audit backend appends a new entry,
    // the browser's default HTTP cache otherwise serves the stale version
    // on the next page load and the new audit silently disappears.
    const res = await fetch("data/manifest.json", { cache: "no-store" });
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
  wireSettingsModal();
  await detectLiveMode();
  wireAuditModal();

  // Honor #hash for deep links to a specific view.
  const fromHash = (location.hash || "#evidence").replace(/^#/, "");
  state.activeView = [
    "evidence",
    "prioritization",
    "jury",
    "briefing",
  ].includes(fromHash)
    ? fromHash
    : "evidence";

  await loadAudit(state.manifest.default);
}

async function fetchOptional(url, asText = false) {
  try {
    const res = await fetch(url);
    if (!res.ok) return null;
    return asText ? await res.text() : await res.json();
  } catch {
    return null;
  }
}

async function loadAudit(slug) {
  const entry = state.manifest.audits.find((a) => a.slug === slug);
  if (!entry) return;
  state.activeSlug = slug;
  markAuditActive(slug);

  const base = `data/${slug}`;
  const [evidence, prioritized, verdicts, reportMd, graphJson] =
    await Promise.all([
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
  state.dpById = Object.fromEntries(
    evidence.decision_points.map((d) => [d.id, d]),
  );
  PRESETS.baseline.weights = { ...prioritized.values };
  state.activePreset = "baseline";

  // Refresh everything (each view reads from state, switching just shows it).
  renderTopBar(entry);
  renderEvidence();
  renderPrioritization(); // also rebuilds sliders + presets
  renderJury();
  renderBriefing();
  renderFooterStatus(entry);
  switchView(state.activeView);
}

// =====================================================================
// Top bar + footer + sidebar audit switcher
// =====================================================================

function renderTopBar(entry) {
  const commit =
    entry.commit || (state.evidence.commit_sha || "").slice(0, 8) || "no-git";
  document.getElementById("topbar-commit").textContent = "commit " + commit;
  document.getElementById("topbar-branch").textContent =
    "branch " + (state.evidence.git_summary?.branch || "main");
  document.getElementById("topbar-backend").textContent = backendBlurb();
  document.getElementById("topbar-cost").textContent = costBlurb();

  // CLI command on Evidence view
  const langFlag =
    entry.language === "auto" ? "" : ` --language ${entry.language}`;
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
  return `$${(0.32 * n + 0.3).toFixed(2)}`;
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
  document
    .querySelectorAll(".audit-pill")
    .forEach((b) => b.classList.toggle("active", b.dataset.slug === slug));
}

function renderFooterStatus(entry) {
  // Footer was removed; null-guard so we don't blow up if the element ever
  // returns. Layer-status summary now derivable from view headers instead.
  const el = document.getElementById("footer-layers");
  if (!el) return;
  const numDps = state.evidence.decision_points.length;
  const numTrib = state.verdicts.length;
  const reportWords = state.reportMd
    ? state.reportMd.trim().split(/\s+/).length
    : 0;
  document.getElementById("footer-layers").textContent =
    `${numDps} findings · top ${state.prioritized.items.length} ranked · ${numTrib} debate${numTrib === 1 ? "" : "s"} · ${reportWords.toLocaleString()}-word report`;
}

function wireClock() {
  const el = document.getElementById("footer-clock");
  if (!el) return;
  const tick = () => {
    el.textContent = new Date().toISOString().slice(11, 19) + " UTC";
  };
  tick();
  setInterval(tick, 1000);
}

function wireNav() {
  document.querySelectorAll(".nav-item").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      switchView(a.dataset.view);
    });
  });
}

function switchView(view) {
  state.activeView = view;
  location.hash = view;
  document
    .querySelectorAll(".view")
    .forEach((s) => s.classList.toggle("hidden", s.dataset.view !== view));
  document
    .querySelectorAll(".nav-item")
    .forEach((a) => a.classList.toggle("active", a.dataset.view === view));
}

function wireButtons() {
  // Top-bar WHAT-IF → jump to Prioritization
  document
    .getElementById("btn-whatif")
    .addEventListener("click", () => switchView("prioritization"));
  // Top-bar download icon → download report.md
  document
    .getElementById("btn-download")
    .addEventListener("click", downloadReport);
  // Sidebar EXPORT REPORT
  document
    .getElementById("btn-export")
    .addEventListener("click", downloadReport);
  // Footer DOWNLOAD_BUNDLE
  document
    .getElementById("btn-bundle")
    .addEventListener("click", downloadBundle);
  // Jury jump-to-action
  document.getElementById("btn-scroll-action").addEventListener("click", () => {
    switchView("briefing");
    setTimeout(() => {
      const el = document.querySelector("#brief-verbatims");
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 80);
  });
  // Reset sliders to baseline
  document
    .getElementById("btn-reset")
    .addEventListener("click", () => applyPreset("baseline"));
}

function downloadReport() {
  if (!state.reportMd) {
    alert("No report.md for this audit.");
    return;
  }
  const blob = new Blob([state.reportMd], { type: "text/markdown" });
  triggerDownload(blob, `${state.activeSlug}-report.md`);
}

async function downloadBundle() {
  if (!window.JSZip) {
    alert("JSZip didn't load.");
    return;
  }
  const zip = new JSZip();
  if (state.evidence)
    zip.file("evidence.json", JSON.stringify(state.evidence, null, 2));
  if (state.prioritized)
    zip.file("prioritized.json", JSON.stringify(state.prioritized, null, 2));
  if (state.verdicts)
    zip.file("verdicts.json", JSON.stringify(state.verdicts, null, 2));
  if (state.reportMd) zip.file("report.md", state.reportMd);
  if (state.graphJson)
    zip.file("graph.json", JSON.stringify(state.graphJson, null, 2));
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
    `${e.decision_points.length} findings · ${(e.git_summary?.commit_sha || "").slice(0, 8) || "no-git"}`;

  // Metric cards from real Layer 1 data
  const principlesFound = new Set(e.decision_points.map((d) => d.principle));
  const metricsHtml = [];

  // P1 — cycles
  const cycleDp = e.decision_points.find((d) => d.principle === "P1");
  const sccSize = cycleDp?.evidence?.scc_size || 0;
  metricsHtml.push(
    metricBlock({
      label: PRINCIPLE_LABELS.P1,
      sublabel: "P1 · Acyclic Dependencies",
      explainer: PRINCIPLE_SUBTITLES.P1,
      value: sccSize > 0 ? `${sccSize} modules tangled` : "clean",
      pct: Math.min(100, sccSize * 5),
      tone: sccSize > 10 ? "error" : sccSize > 0 ? "tertiary" : "primary",
      note:
        sccSize > 10
          ? "Large cycle — many files locked together"
          : sccSize > 0
            ? "Small cycle present"
            : "No cycles found",
    }),
  );

  // P3 — complexity (peak CC)
  const cxDps = e.decision_points.filter((d) => d.principle === "P3");
  const peakCC = Math.max(0, ...cxDps.map((d) => d.evidence?.complexity || 0));
  metricsHtml.push(
    metricBlock({
      label: PRINCIPLE_LABELS.P3,
      sublabel: "P3 · McCabe cyclomatic complexity",
      explainer: PRINCIPLE_SUBTITLES.P3,
      value: peakCC > 0 ? `peak ${peakCC}` : "all under 15",
      pct: Math.min(100, peakCC),
      tone: peakCC > 100 ? "error" : peakCC > 30 ? "tertiary" : "primary",
      note: cxDps.length
        ? `${cxDps.length} function${cxDps.length === 1 ? "" : "s"} above the 15-branch ceiling`
        : "All functions stay under the limit",
    }),
  );

  // P4 — LCOM (peak)
  const lcDps = e.decision_points.filter((d) => d.principle === "P4");
  const peakLcom = Math.max(0, ...lcDps.map((d) => d.evidence?.lcom || 0));
  metricsHtml.push(
    metricBlock({
      label: PRINCIPLE_LABELS.P4,
      sublabel: "P4 · LCOM cohesion (Python only)",
      explainer: PRINCIPLE_SUBTITLES.P4,
      value: lcDps.length ? `peak ${peakLcom.toFixed(2)}` : "n/a (or C)",
      pct: peakLcom * 100,
      tone: peakLcom > 0.9 ? "error" : peakLcom > 0.7 ? "tertiary" : "primary",
      note: lcDps.length
        ? `${lcDps.length} class${lcDps.length === 1 ? "" : "es"} above 0.7 (1.0 = methods share zero state)`
        : "Skipped for non-Python repos (C has no classes)",
    }),
  );

  // P5 — dead code
  const dcDps = e.decision_points.filter((d) => d.principle === "P5");
  metricsHtml.push(
    metricBlock({
      label: PRINCIPLE_LABELS.P5,
      sublabel: "P5 · Reachability (vulture / cppcheck)",
      explainer: PRINCIPLE_SUBTITLES.P5,
      value: `${dcDps.length} dead`,
      pct: Math.min(100, dcDps.length * 20),
      tone: dcDps.length > 3 ? "tertiary" : "primary",
      note: dcDps.length
        ? `Detected by ${dcDps[0].evidence?.analyzer || "static checker"}`
        : "No dead code surfaced",
    }),
  );

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
// Dependency graph (3d-force-graph + Three.js bloom)
// =====================================================================

// Softer pastel palette — glows better on dark backgrounds than full saturation.
const PKG_COLORS = [
  "#7dd3fc", // sky-300
  "#fde047", // yellow-300
  "#fdba74", // orange-300
  "#d8b4fe", // purple-300
  "#86efac", // green-300
  "#fca5a5", // red-300
  "#93c5fd", // blue-300
  "#fcd34d", // amber-300
];

function renderDependencyGraph() {
  const container = document.getElementById("ev-graph-wrap");
  if (!container) return;

  // Tear down any previous instance.
  if (state.forceGraph) {
    state.forceGraph._destructor && state.forceGraph._destructor();
    state.forceGraph = null;
  }
  container.innerHTML = "";

  const data = state.graphJson;
  if (!data || !data.nodes?.length) {
    container.innerHTML = `<div style="padding:24px;color:#919094;text-align:center">No dependency graph on disk for this audit.</div>`;
    return;
  }

  const Graph3D = window.ForceGraph3D;
  if (!Graph3D) {
    container.innerHTML = `<div style="padding:24px;color:#919094;text-align:center">3d-force-graph library not loaded.</div>`;
    return;
  }

  // Assign a stable color per top-level package.
  const pkgOrder = [...new Set(data.nodes.map((n) => n.pkg))];
  const colorFor = (pkg) =>
    PKG_COLORS[pkgOrder.indexOf(pkg) % PKG_COLORS.length];

  // Node size scales with in-degree (fan-in).
  const fanIn = new Map();
  for (const e of data.edges)
    fanIn.set(e.target, (fanIn.get(e.target) || 0) + 1);
  const maxFanIn = Math.max(1, ...fanIn.values());

  // Build adjacency for hover highlighting.
  const neighbors = new Map();
  for (const e of data.edges) {
    if (!neighbors.has(e.source)) neighbors.set(e.source, new Set());
    if (!neighbors.has(e.target)) neighbors.set(e.target, new Set());
    neighbors.get(e.source).add(e.target);
    neighbors.get(e.target).add(e.source);
  }

  // Identify modules with errors (decision points from evidence).
  const errorModules = new Set();
  if (state.evidence?.decision_points) {
    for (const dp of state.evidence.decision_points) {
      if (dp.subject) errorModules.add(dp.subject);
      if (dp.evidence?.module) errorModules.add(dp.evidence.module);
      // Also match by partial qualname (e.g. "fastapi.routing" matches DP subject containing it)
      for (const n of data.nodes) {
        if (dp.subject && dp.subject.includes(n.id)) errorModules.add(n.id);
      }
    }
  }

  const nodes = data.nodes.map((n) => ({
    id: n.id,
    label: n.label || n.id,
    pkg: n.pkg,
    color: errorModules.has(n.id) ? "#ef4444" : colorFor(n.pkg),
    hasError: errorModules.has(n.id),
    val: 1.5 + ((fanIn.get(n.id) || 0) / maxFanIn) * 6,
  }));
  const links = data.edges.map((e) => ({
    source: e.source,
    target: e.target,
  }));

  // Track hover state for highlighting.
  let hoveredNode = null;

  const graph = Graph3D({ controlType: "orbit" })(container)
    .graphData({ nodes, links })
    .backgroundColor("#000000")
    .showNavInfo(false)

    // --- Nodes ---
    .nodeLabel(
      (n) =>
        `<div style="color:${n.color};font-family:JetBrains Mono,monospace;font-size:11px;line-height:1.4;padding:4px 8px;background:#1a1a22;border:1px solid ${n.color}44;border-radius:3px">
        <div style="font-weight:700">${n.label}</div>
        <div style="font-size:9px;opacity:0.6">${n.id}</div>
        <div style="font-size:9px;opacity:0.5;margin-top:2px">${fanIn.get(n.id) || 0} dependents · pkg: ${n.pkg}</div>
      </div>`,
    )
    .nodeColor((n) => {
      if (!hoveredNode) return n.color;
      if (n.id === hoveredNode) return n.color;
      const nh = neighbors.get(hoveredNode);
      if (nh && nh.has(n.id)) return n.color;
      return "#1a1a22";
    })
    .nodeVal((n) => n.val)
    .nodeOpacity(0.9)
    .nodeResolution(16)
    .nodeThreeObject((n) => {
      const canvas = document.createElement("canvas");
      const ctx = canvas.getContext("2d");
      const fontSize = 40;
      const fontStr = `bold ${fontSize}px JetBrains Mono, monospace`;
      ctx.font = fontStr;
      const textWidth = ctx.measureText(n.label).width;
      canvas.width = Math.ceil(textWidth + 24);
      canvas.height = fontSize + 12;
      ctx.font = fontStr;
      ctx.fillStyle = n.color;
      ctx.textBaseline = "middle";
      ctx.textAlign = "center";
      ctx.fillText(n.label, canvas.width / 2, canvas.height / 2);

      const texture = new THREE_REF.CanvasTexture(canvas);
      texture.minFilter = THREE_REF.LinearFilter;
      const spriteMat = new THREE_REF.SpriteMaterial({
        map: texture,
        transparent: true,
        opacity: 0.8,
        depthWrite: false,
      });
      const sprite = new THREE_REF.Sprite(spriteMat);
      const scale = canvas.width / canvas.height;
      sprite.scale.set(scale * 5, 5, 1);
      const radius = Math.cbrt(n.val) * 2;
      sprite.position.set(0, radius + 3, 0);
      return sprite;
    })
    .nodeThreeObjectExtend(true)

    // --- Links ---
    .linkColor((link) => {
      const src =
        typeof link.source === "object" ? link.source : { id: link.source };
      const srcColor = colorFor(nodes.find((n) => n.id === src.id)?.pkg || "");
      if (!hoveredNode) return srcColor + "44";
      if (
        src.id === hoveredNode ||
        (typeof link.target === "object" ? link.target.id : link.target) ===
          hoveredNode
      ) {
        return srcColor + "dd";
      }
      return "#1a1a2208";
    })
    .linkWidth((link) => {
      if (!hoveredNode) return 0.6;
      const srcId =
        typeof link.source === "object" ? link.source.id : link.source;
      const tgtId =
        typeof link.target === "object" ? link.target.id : link.target;
      return srcId === hoveredNode || tgtId === hoveredNode ? 1.8 : 0.1;
    })
    .linkOpacity(0.8)
    .linkCurvature(0.15)
    .linkCurveRotation(0.5)
    .linkDirectionalParticles((link) => {
      if (!hoveredNode) return 0;
      const srcId =
        typeof link.source === "object" ? link.source.id : link.source;
      const tgtId =
        typeof link.target === "object" ? link.target.id : link.target;
      return srcId === hoveredNode || tgtId === hoveredNode ? 4 : 0;
    })
    .linkDirectionalParticleWidth(1.0)
    .linkDirectionalParticleSpeed(0.008)
    .linkDirectionalParticleColor((link) => {
      const src =
        typeof link.source === "object" ? link.source : { id: link.source };
      return colorFor(nodes.find((n) => n.id === src.id)?.pkg || "");
    })
    .linkDirectionalArrowLength(3.5)
    .linkDirectionalArrowRelPos(1)
    .linkDirectionalArrowColor((link) => {
      const src =
        typeof link.source === "object" ? link.source : { id: link.source };
      return colorFor(nodes.find((n) => n.id === src.id)?.pkg || "") + "60";
    })

    // --- Physics (spread out the cluster) ---
    .d3AlphaDecay(0.01)
    .d3VelocityDecay(0.2)
    .warmupTicks(150)
    .cooldownTicks(400)

    // --- Hover interactions ---
    .onNodeHover((node) => {
      hoveredNode = node ? node.id : null;
      container.style.cursor = node ? "pointer" : "grab";
    })
    .onNodeClick((node) => {
      showNodeFindings(node.id);
    });

  // Increase repulsion so nodes spread out more.
  graph.d3Force("charge").strength(-120).distanceMax(300);
  graph.d3Force("link").distance(40);

  state.forceGraph = graph;

  // Wire close button for findings panel.
  const closeBtn = document.getElementById("node-findings-close");
  if (closeBtn)
    closeBtn.onclick = () => {
      document.getElementById("node-findings-panel").classList.add("hidden");
    };
}

function showNodeFindings(moduleId) {
  const panel = document.getElementById("node-findings-panel");
  const title = document.getElementById("node-findings-title");
  const body = document.getElementById("node-findings-body");
  if (!panel || !state.evidence) return;

  title.textContent = moduleId;

  // Find all decision points that reference this module.
  const dps = state.evidence.decision_points.filter((dp) => {
    // Check locations
    if (dp.locations?.some((loc) => loc.module === moduleId)) return true;
    // Check subject
    if (dp.subject && dp.subject.includes(moduleId)) return true;
    // Check evidence.module
    if (dp.evidence?.module === moduleId) return true;
    // Check scc_members
    if (dp.evidence?.scc_members?.includes(moduleId)) return true;
    return false;
  });

  if (!dps.length) {
    body.innerHTML = `<div class="text-on-surface-variant text-[12px] opacity-70 py-8 text-center">No findings for this module.</div>`;
    panel.classList.remove("hidden");
    return;
  }

  body.innerHTML = dps
    .map((dp, i) => {
      const principleName = PRINCIPLE_LABELS[dp.principle] || dp.principle;
      const snippet =
        dp.code_snippets?.find((s) => s.includes(moduleId.split(".").pop())) ||
        dp.code_snippets?.[0] ||
        "";
      const loc =
        dp.locations?.find((l) => l.module === moduleId) || dp.locations?.[0];
      const fileInfo = loc
        ? `${loc.file}:${loc.line_start}-${loc.line_end}`
        : "";

      return `
      <div class="bg-surface-container-low border border-outline-variant">
        <div class="h-1 w-full bg-[#ef4444]"></div>
        <div class="p-4">
          <div class="flex items-center gap-2 mb-2">
            <span class="font-label-caps text-[9px] px-2 py-0.5 bg-[#ef4444]/10 text-[#ef4444] border border-[#ef4444]/30">${escapeHtml(dp.principle)}</span>
            <span class="font-label-caps text-[9px] text-on-surface-variant">${escapeHtml(principleName)}</span>
          </div>
          <div class="font-body-md text-on-surface text-[13px] mb-2">${escapeHtml(dp.subject)}</div>
          ${fileInfo ? `<div class="font-code-sm text-[10px] text-on-surface-variant opacity-60 mb-3">${escapeHtml(fileInfo)}</div>` : ""}
          ${snippet ? `<pre class="bg-surface-container-lowest border border-outline-variant p-3 text-[10px] font-code-sm text-on-surface-variant overflow-x-auto max-h-[200px] overflow-y-auto whitespace-pre-wrap leading-relaxed">${escapeHtml(snippet)}</pre>` : ""}
          ${
            dp.alternatives?.length
              ? `
            <div class="mt-3 pt-3 border-t border-outline-variant/30">
              <div class="font-label-caps text-[9px] text-on-tertiary-container mb-2">SUGGESTED FIXES</div>
              <ul class="text-[11px] text-on-surface-variant space-y-1 list-disc list-inside">
                ${dp.alternatives.map((a) => `<li>${escapeHtml(a)}</li>`).join("")}
              </ul>
            </div>
          `
              : ""
          }
        </div>
      </div>`;
    })
    .join("");

  panel.classList.remove("hidden");
}

function metricBlock({ label, sublabel, explainer, value, pct, tone, note }) {
  const barColor =
    { error: "#ef4444", tertiary: "#fb923c", primary: "#c8c6c7" }[tone] ||
    "#c8c6c7";
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
  document.getElementById("prio-stat-dps").textContent =
    state.prioritized.items.length;
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
  const target =
    key === "baseline" ? state.baselineWeights : PRESETS[key].weights;
  state.currentWeights = { ...target };
  // Refresh sliders to new values
  for (const v of VALUES) {
    const input = document.querySelector(
      `#prio-sliders input[data-name="${v}"]`,
    );
    if (input) input.value = state.currentWeights[v];
    updateSliderLabel(v);
  }
  // Update preset highlights everywhere
  document
    .querySelectorAll(".tone-btn")
    .forEach((b) => b.classList.toggle("active", b.dataset.preset === key));
  refreshAfterSlider();
}

function renderSliders() {
  const root = document.getElementById("prio-sliders");
  root.innerHTML = "";
  for (const v of VALUES) {
    const baseline = state.baselineWeights[v] ?? 1.0;
    const current = state.currentWeights[v] ?? baseline;
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
    row.querySelector("input").addEventListener("input", (e) => {
      const name = e.target.dataset.name;
      state.currentWeights[name] = +e.target.value;
      state.activePreset = "custom";
      document
        .querySelectorAll(".tone-btn")
        .forEach((b) => b.classList.remove("active"));
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
  el.style.color =
    v > b + 0.05 ? "#4ade80" : v < b - 0.05 ? "#fb923c" : "#c8c6c7";
}

let lastRanking = null;

function renderRanking() {
  const root = document.getElementById("prio-ranking");
  // Re-score only the originally-prioritized set; live composite under current weights.
  const originalIds = new Set(
    state.prioritized.items.map((i) => i.decision_point_id),
  );
  const scored = state.evidence.decision_points
    .filter((dp) => originalIds.has(dp.id))
    .map((dp) => {
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
    const principleName =
      PRINCIPLE_LABELS[row.dp.principle] || row.dp.principle;
    const deltaTag =
      delta > 0
        ? `<span class="text-[#4ade80] text-[10px] ml-2" title="Moved up ${delta} place${delta === 1 ? "" : "s"}">↑${delta}</span>`
        : delta < 0
          ? `<span class="text-[#fb923c] text-[10px] ml-2" title="Moved down ${Math.abs(delta)} place${Math.abs(delta) === 1 ? "" : "s"}">↓${Math.abs(delta)}</span>`
          : "";
    const div = document.createElement("div");
    div.className =
      "bg-surface-container border border-outline-variant p-4 transition-all" +
      (delta !== 0 ? " ring-1 ring-primary/30" : "");
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
  lastRanking = scored.map((s) => s.dp.id);
  document.getElementById("prio-stat-shift").textContent = nShifted;
}

function refreshAfterSlider() {
  renderRanking();
  renderJuryAggregates(); // re-projected verdict line per tribunal updates too
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
  let totalCells = 0,
    overrides = 0;

  state.verdicts.forEach((trib, tribIdx) => {
    const dp = state.dpById[trib.decision_point_id];
    const cells = trib.cells || [];
    const judge = trib.judge || {};
    totalCells += cells.length;
    if (judge.override) overrides += 1;

    // ---- Finding header ----
    const principleName = PRINCIPLE_LABELS[dp?.principle] || dp?.principle || "?";
    const header = document.createElement("div");
    header.className = "mb-3 mt-8 first:mt-0";
    header.innerHTML = `
      <div class="pb-3 border-b border-outline-variant">
        <div class="flex items-center gap-3 mb-1">
          <span class="font-label-caps text-label-caps text-primary">FINDING ${tribIdx + 1} of ${state.verdicts.length}</span>
          <span class="text-[11px] text-on-surface-variant opacity-70">
            ${escapeHtml(principleName)} <span class="opacity-60">(${dp?.principle ?? "?"})</span>
          </span>
        </div>
        <div class="font-body-lg text-on-surface leading-tight">${escapeHtml(dp?.subject || trib.decision_point_id)}</div>
        <div class="text-[11px] text-on-surface-variant opacity-70 mt-2">
          Below: 10 debate pairs argued from their own value perspectives. Each card is one pairing.
        </div>
      </div>`;
    root.appendChild(header);

    // ---- Cells grid: majority first, then a divider, then dissenters ----
    // Group by vote so the user can see at a glance who pushed back.
    const nDebt = cells.filter(c => c.position === "debt").length;
    const nJust = cells.filter(c => c.position === "justified").length;
    const majority = nDebt >= nJust ? "debt" : "justified";
    const majCells = cells.filter(c => c.position === majority);
    const disCells = cells.filter(c => c.position !== majority);
    // Within each group, sort by confidence descending — strongest argument first.
    majCells.sort((a, b) => (b.confidence || 0) - (a.confidence || 0));
    disCells.sort((a, b) => (b.confidence || 0) - (a.confidence || 0));

    const majorityLabel = majority === "debt" ? "PROBLEM" : "FINE";
    const dissentLabel  = majority === "debt" ? "FINE"    : "PROBLEM";
    const majorityColor = majority === "debt" ? "#fb923c" : "#4ade80";
    const dissentColor  = majority === "debt" ? "#4ade80" : "#fb923c";

    // Section: majority group
    const majHeader = document.createElement("div");
    majHeader.className = "mb-2 mt-1 flex items-center gap-2 text-[11px]";
    majHeader.innerHTML = `
      <span class="inline-block w-2 h-2 rounded-full" style="background:${majorityColor}"></span>
      <span class="font-label-caps text-[10px] tracking-widest" style="color:${majorityColor}">${majCells.length} pair${majCells.length === 1 ? "" : "s"} said ${majorityLabel}</span>
      <span class="text-on-surface-variant opacity-60">— the majority reading</span>
    `;
    root.appendChild(majHeader);

    const majGrid = document.createElement("div");
    majGrid.className = "grid grid-cols-1 md:grid-cols-2 gap-3 mb-2";
    majGrid.dataset.tribunalId = trib.decision_point_id;
    majCells.forEach((c) => majGrid.appendChild(renderCellCard(c, { isDissent: false })));
    root.appendChild(majGrid);

    // Section: dissenters
    if (disCells.length > 0) {
      const disHeader = document.createElement("div");
      disHeader.className = "mt-4 mb-2 flex items-center gap-2 text-[11px]";
      disHeader.innerHTML = `
        <span class="inline-block w-2 h-2 rounded-full" style="background:${dissentColor}"></span>
        <span class="font-label-caps text-[10px] tracking-widest" style="color:${dissentColor}">↯ ${disCells.length} pair${disCells.length === 1 ? "" : "s"} pushed back — said ${dissentLabel}</span>
        <span class="text-on-surface-variant opacity-60">— their values read the evidence differently. The judge weighed these too.</span>
      `;
      root.appendChild(disHeader);

      const disGrid = document.createElement("div");
      disGrid.className = "grid grid-cols-1 md:grid-cols-2 gap-3 mb-2";
      disGrid.dataset.tribunalId = trib.decision_point_id;
      disCells.forEach((c) => disGrid.appendChild(renderCellCard(c, { isDissent: true })));
      root.appendChild(disGrid);
    }

    // ---- Aggregate line (re-projected under current weights) ----
    const aggLine = document.createElement("div");
    aggLine.className = "mb-6 text-[11px] font-code-sm text-on-surface-variant";
    aggLine.dataset.aggregateFor = trib.decision_point_id;
    root.appendChild(aggLine);

    // ---- Judge card in right panel ----
    judgeRoot.appendChild(renderJudgeCard(trib, tribIdx, dp));
  });

  document.getElementById("jury-stat-tribunals").textContent =
    state.verdicts.length;
  document.getElementById("jury-stat-cells").textContent = totalCells;
  document.getElementById("jury-stat-overrides").textContent = overrides;

  renderJuryAggregates();
}

// Maps persona id → display name + the single value it cares about.
// Used to label cell cards now that personas are monomaniacal and
// neither persona is locked to a side.
// Persona display = the value it cares about, full stop. The persona ID
// (simplifier / shipper / etc.) stays for code/data compatibility.
const PERSONA_INFO = {
  simplifier: { name: "Simplicity",      value: "simplicity",      color: "#a78bfa" },
  shipper:    { name: "Velocity",        value: "velocity",        color: "#fb923c" },
  maintainer: { name: "Maintainability", value: "maintainability", color: "#5dd6ff" },
  verifier:   { name: "Correctness",     value: "correctness",     color: "#4ade80" },
  scaler:     { name: "Scalability",     value: "scalability",     color: "#f472b6" },
  adapter:    { name: "Flexibility",     value: "flexibility",     color: "#facc15" },
};

function personaPill(personaId) {
  const info = PERSONA_INFO[personaId] || {
    name: personaId,
    value: "?",
    color: "#919094",
  };
  // Name is the value (e.g., "Velocity"); a colored dot reinforces it.
  return `<span class="inline-flex items-center gap-1.5 px-2 py-0.5 border rounded-sm"
                style="color:${info.color};border-color:${info.color};background:${info.color}14;"
                title="${escapeHtml(info.name)} — only cares about ${escapeHtml(info.value)}, indifferent to the other 5 values.">
            <span class="inline-block w-1.5 h-1.5 rounded-full" style="background:${info.color}"></span>
            <span class="font-code-sm text-[11px] font-bold">${escapeHtml(info.name)}</span>
          </span>`;
}

function renderCellCard(c, opts = {}) {
  const { isDissent = false } = opts;
  // New model: two personas debate, the CELL votes. Vote color reflects the
  // cell's conclusion (debt=orange, justified=green) — NOT the persona's
  // "side" since neither persona has a side anymore.
  const voteIsDebt = c.position === "debt";
  const voteColor = voteIsDebt ? "#fb923c" : "#4ade80";

  // red_persona/blue_persona field names are kept for schema compat; they're
  // now just "persona A" and "persona B" labels — neither is prosecution/defense.
  const personaAId = c.red_persona;
  const personaBId = c.blue_persona;
  const confidencePct = Math.round((c.confidence || 0) * 100);

  // Salience under current weights
  const b = salience(c.value_lens, state.baselineWeights);
  const n = salience(c.value_lens, state.currentWeights);
  const ratio = b > 0 ? n / b : n > 0 ? INFINITY_SENTINEL : 1;
  const salient = ratio >= SALIENCE_BUMP;
  const ratioStr = ratio >= INFINITY_SENTINEL ? "∞" : `${ratio.toFixed(2)}×`;

  const voteShort = voteIsDebt ? "real problem, worth fixing" : "fine as-is, defensible";
  const voteExplainer = voteIsDebt
    ? "Cell concluded: this is real debt — the personas' reading converged on harm."
    : "Cell concluded: this is justified — the personas' reading converged on serving / defensible.";
  const salienceTitle = salient
    ? `This cell's reasoning rests on values your sliders weight highly. Your priorities care ${ratioStr} more about this argument than the baseline.`
    : "How much your current priorities overlap with the values this cell's argument rests on. 1.00× = baseline.";

  const card = document.createElement("div");
  // Dissenters get a stronger border in their vote-color to make the
  // "this pair pushed back" signal pop against the surrounding majority.
  const dissentRing = isDissent ? ` ring-2 ring-offset-0` : "";
  card.className =
    "bg-surface-container border border-outline-variant relative" +
    (salient ? " ring-1 ring-yellow-400/40" : "") + dissentRing;
  if (isDissent) {
    card.style.boxShadow = `0 0 0 1px ${voteColor}55`;
  }
  // Use the persona pair as the headline. Each persona's display name IS
  // the value it cares about (e.g. "Simplicity vs Velocity") — paired with
  // a colored dot for instant visual recognition.
  const aInfo = PERSONA_INFO[personaAId] || { name: personaAId, value: "?", color: "#919094" };
  const bInfo = PERSONA_INFO[personaBId] || { name: personaBId, value: "?", color: "#919094" };
  const dot   = (color) => `<span class="inline-block w-2.5 h-2.5 rounded-full align-middle" style="background:${color}"></span>`;

  const dissentRibbon = isDissent
    ? `<div class="absolute top-0 right-0 font-label-caps text-[8px] px-1.5 py-0.5 tracking-widest"
            style="color:${voteColor};background:${voteColor}22;border-bottom-left-radius:3px;"
            title="This pair disagreed with the majority — pushed back from their value's perspective.">↯ PUSHED BACK</div>`
    : "";

  card.innerHTML = `
    ${dissentRibbon}
    <div class="h-1 w-full" style="background:${voteColor}"></div>
    <div class="p-4">
      <div class="flex justify-between items-start mb-3 gap-3">
        <div class="min-w-0">
          <div class="font-headline-sm text-on-surface leading-tight"
               title="Debate ${c.cell_id + 1} of 10. Each persona cares about exactly one engineering value and argues from that perspective.">
            <span title="${escapeHtml(aInfo.name)} — cares only about ${escapeHtml(aInfo.value)}">
              ${dot(aInfo.color)} <span style="color:${aInfo.color}">${escapeHtml(aInfo.name)}</span>
            </span>
            <span class="text-on-surface-variant opacity-60 font-normal text-[14px] mx-1">vs</span>
            <span title="${escapeHtml(bInfo.name)} — cares only about ${escapeHtml(bInfo.value)}">
              ${dot(bInfo.color)} <span style="color:${bInfo.color}">${escapeHtml(bInfo.name)}</span>
            </span>
          </div>
        </div>
        <div class="flex flex-col items-end gap-0.5 flex-shrink-0">
          <span class="font-label-caps text-[9px] px-2 py-0.5 border whitespace-nowrap"
                style="color:${voteColor};border-color:${voteColor};background:${voteColor}1a"
                title="${escapeHtml(voteExplainer)}">
            ${voteIsDebt ? "PROBLEM" : "FINE"}
          </span>
          <span class="text-[9px] text-on-surface-variant opacity-70 italic">${voteShort}</span>
        </div>
      </div>
      <!-- Confidence bar -->
      <div class="flex items-center gap-2 mb-3 bg-surface-container-high p-2 border border-outline-variant/30" title="How sure this pair was of their conclusion (0% = toss-up, 100% = decisive)">
        <div class="flex-1 h-1.5 bg-surface-variant relative overflow-hidden">
          <div class="absolute left-0 top-0 h-full" style="width:${confidencePct}%;background:${voteColor}"></div>
        </div>
        <span class="font-code-sm text-[10px]" style="color:${voteColor}">${confidencePct}% sure</span>
      </div>
      <div class="text-[12px] text-on-surface leading-relaxed italic">
        "${escapeHtml(c.key_argument || "(no argument)")}"
      </div>
      <div class="mt-3 pt-2 border-t border-outline-variant/30 text-[10px] text-on-surface-variant flex items-center justify-between"
           title="${escapeHtml(salienceTitle)}">
        <span>your priorities care about this</span>
        <span class="${salient ? "text-yellow-400 font-bold" : ""}">${ratioStr}${salient ? " — emphasized" : ""}</span>
      </div>
    </div>`;
  return card;
}

function renderJudgeCard(trib, tribIdx, dp) {
  const judge = trib.judge || {};
  const v = String(judge.verdict || "—").toUpperCase();
  const vKey = v.replace(/ /g, "-");
  const verdictExplainer = VERDICT_PLAIN[v] || "";

  const card = document.createElement("div");
  card.className = `bg-surface-container-low border border-outline-variant p-4`;
  card.innerHTML = `
    <div class="flex justify-between items-center mb-2 flex-wrap gap-2">
      <span class="font-label-caps text-[10px] text-on-tertiary-container">FINDING ${tribIdx + 1}${judge.override ? " · JUDGE OVERRODE THE PAIRS" : ""}</span>
      <span class="font-code-sm font-bold verdict-${vKey} verdict-bg-${vKey} px-2 py-0.5"
            title="${escapeHtml(verdictExplainer)}">${escapeHtml(v)}</span>
    </div>
    <div class="font-code-sm text-on-surface-variant text-[10px] mb-3 truncate" title="${escapeHtml(dp?.subject || "")}">${escapeHtml(dp?.subject || trib.decision_point_id)}</div>
    ${verdictExplainer ? `<div class="text-[11px] text-on-surface-variant opacity-70 mb-3 leading-snug">${escapeHtml(verdictExplainer)}</div>` : ""}
    <div class="font-body-md text-on-surface leading-relaxed text-[13px]">
      <span class="font-label-caps text-[9px] text-on-tertiary-container">REASONING</span><br>
      ${escapeHtml(judge.reasoning || "(no reasoning)")}
    </div>
    ${
      judge.dissent_summary
        ? `
      <div class="mt-3 pt-3 border-t border-outline-variant/30">
        <div class="font-label-caps text-[9px] text-on-tertiary-container mb-1">STRONGEST DISSENT (the losing side's best point)</div>
        <div class="font-code-sm text-on-surface-variant text-[11px] italic">${escapeHtml(judge.dissent_summary)}</div>
      </div>`
        : ""
    }`;
  return card;
}

function renderJuryAggregates() {
  // Update each tribunal's "Re-projected aggregate" line under current weights.
  document.querySelectorAll("[data-aggregate-for]").forEach((line) => {
    const tribunalId = line.dataset.aggregateFor;
    const trib = state.verdicts.find((t) => t.decision_point_id === tribunalId);
    if (!trib) return;
    const cells = trib.cells || [];
    const orig = trib.aggregate_vote || {};
    const proj = reweightedAggregate(cells, state.currentWeights);
    const wouldFlip = proj.winner && orig.winner && proj.winner !== orig.winner;
    const dCount = orig.n_debt ?? 0;
    const jCount = orig.n_justified ?? 0;
    const projShort =
      proj.winner === "debt"      ? "would lean PROBLEM" :
      proj.winner === "justified" ? "would lean FINE"    : "—";
    line.innerHTML = `
      <div class="bg-surface-container-low border border-outline-variant/40 p-3 rounded">
        <div class="mb-1">
          <span class="text-on-surface-variant">What the 10 pairs decided:</span>
          <b class="text-[#fb923c]">${dCount} said PROBLEM</b>
          ·
          <b class="text-[#4ade80]">${jCount} said FINE</b>
        </div>
        <div>
          <span class="text-on-surface-variant">If we re-counted using your priority sliders:</span>
          <b style="color:${proj.winner === "debt" ? "#fb923c" : "#4ade80"}">${projShort}</b>
          ${wouldFlip
            ? `<span class="ml-2 text-yellow-400 font-bold">— the majority side would flip</span>`
            : `<span class="ml-2 opacity-60">— same majority as actual (the judge's verdict label never changes either way)</span>`}
        </div>
      </div>
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
  document.getElementById("brief-report-id").textContent =
    `REPORT_ID: ${state.activeSlug.toUpperCase()}-${state.evidence.commit_sha?.slice(0, 6) || "—"}`;
  document.getElementById("brief-words").textContent =
    `${state.reportMd ? state.reportMd.trim().split(/\s+/).length : 0} WORDS`;
  document.getElementById("brief-stamp").textContent = stamp;
  document.getElementById("brief-watermark-stamp").textContent =
    `GEN_STAMP: ${stamp}`;
  document.getElementById("brief-watermark-sig").textContent =
    `SIGNATURE: ${(state.evidence.commit_sha || "00000000").slice(0, 8)}…`;
}

function renderBriefingSummary() {
  // Counts of verdicts in current cache (rule: verdict text never changes
  // with sliders; only the re-projected aggregate winner can hypothetically
  // shift — counts here always reflect the literal judge output).
  const verdictCounts = {};
  for (const t of state.verdicts) {
    const v = t.judge?.verdict || "—";
    verdictCounts[v] = (verdictCounts[v] || 0) + 1;
  }
  const critical = verdictCounts["CRITICAL"] || 0;
  const debt = verdictCounts["STRUCTURAL DEBT"] || 0;
  const just = verdictCounts["JUSTIFIED VIOLATION"] || 0;

  // Confidence avg (mean of all cell confidences across all tribunals)
  let confSum = 0,
    confN = 0;
  for (const t of state.verdicts) {
    for (const c of t.cells || []) {
      confSum += c.confidence || 0;
      confN += 1;
    }
  }
  const meanConf = confN ? confSum / confN : 0;

  document.getElementById("brief-summary").innerHTML = `
    <div class="bg-surface-container border border-outline-variant p-6" style="border-top:2px solid #ef4444"
         title="Verdicts marked CRITICAL by the judge — actively causing or about to cause production impact.">
      <div class="flex items-center justify-between mb-2">
        <span class="font-label-caps text-label-caps text-[#ef4444]">CRITICAL</span>
        <span class="material-symbols-outlined text-[#ef4444]">dangerous</span>
      </div>
      <div class="text-4xl font-headline-lg text-on-surface">${String(critical).padStart(2, "0")}</div>
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
      <div class="text-4xl font-headline-lg text-on-surface">${String(debt).padStart(2, "0")}</div>
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
    (_, v) => {
      const plain = VERDICT_PLAIN[v] || "";
      return `<span class="verdict-tag verdict-${v.replace(/ /g, "-")} verdict-bg-${v.replace(/ /g, "-")}" title="${plain.replace(/"/g, "&quot;")}">${v}</span>`;
    },
  );
  document.getElementById("brief-markdown").innerHTML = html;
}

function renderBriefingVerbatims() {
  const root = document.getElementById("brief-verbatims");
  if (!state.verdicts.length) {
    root.innerHTML = "";
    return;
  }
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
    block.style.borderLeftColor =
      ({
        HEALTHY: "#4ade80",
        "JUSTIFIED VIOLATION": "#facc15",
        "STRUCTURAL DEBT": "#fb923c",
        CRITICAL: "#ef4444",
        DRIFTED: "#c084fc",
        CONTESTED: "#67e8f9",
      })[v] || "#c8c6c7";
    const plain = VERDICT_PLAIN[v] || "";
    block.innerHTML = `
      <div class="flex items-center gap-2 mb-2 flex-wrap">
        <span class="font-label-caps text-label-caps verdict-bg-${vKey} verdict-${vKey} px-2 py-1"
              title="${escapeHtml(plain)}">${escapeHtml(v)}</span>
        ${judge.override ? `<span class="font-label-caps text-[10px] text-yellow-400 bg-yellow-900/20 px-2 py-1" title="Judge overrode the panel majority on this finding.">OVERRIDE</span>` : ""}
        <span class="text-on-surface-variant font-code-sm">DP ${idx + 1}/${state.verdicts.length} · ${escapeHtml(trib.decision_point_id)}</span>
      </div>
      ${plain ? `<div class="text-[11px] text-on-surface-variant opacity-70 mb-3 leading-snug">${escapeHtml(plain)}</div>` : ""}
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
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Strip ANSI color codes — even with NO_COLOR=1 set, Rich still emits a few.
const ANSI_RE = /\x1B\[[0-9;?]*[ -/]*[@-~]/g;
function stripAnsi(s) {
  return s.replace(ANSI_RE, "");
}

// =====================================================================
// Live mode (FastAPI backend at /api/*)
// =====================================================================

// ----- API base + token (settings persisted in localStorage) -----
// Same-origin uses "" (relative paths) so localhost dev keeps working
// untouched. Cross-origin (Pages → laptop tunnel) needs the absolute URL.
function getApiBase() {
  return (localStorage.getItem("forumApiBase") || "").replace(/\/+$/, "");
}
function getApiToken() {
  return localStorage.getItem("forumApiToken") || "";
}
function setApiConfig(base, token) {
  if (base) localStorage.setItem("forumApiBase", base.replace(/\/+$/, ""));
  else localStorage.removeItem("forumApiBase");
  if (token) localStorage.setItem("forumApiToken", token);
  else localStorage.removeItem("forumApiToken");
}

function apiUrl(path) {
  // path always starts with /api/...
  return getApiBase() + path;
}

async function apiFetch(path, opts = {}) {
  const headers = new Headers(opts.headers || {});
  const tok = getApiToken();
  if (tok) headers.set("Authorization", `Bearer ${tok}`);
  return fetch(apiUrl(path), { ...opts, headers });
}

function apiEventSource(path) {
  // EventSource can't set headers, so fall back to a ?token= query param.
  // The backend accepts both ways (see require_token in server.py).
  const tok = getApiToken();
  const sep = path.includes("?") ? "&" : "?";
  const url =
    apiUrl(path) + (tok ? `${sep}token=${encodeURIComponent(tok)}` : "");
  return new EventSource(url);
}

async function detectLiveMode() {
  // Probe the configured backend. On vanilla GitHub Pages with no settings
  // saved, getApiBase() is "" so we hit /api/manifest on the Pages origin —
  // gets a 404 and gracefully returns to static mode.
  try {
    const res = await apiFetch("/api/manifest", { method: "GET" });
    if (!res.ok) return;
    const ct = res.headers.get("content-type") || "";
    if (!ct.includes("json")) return;
    await res.json();
    state.liveMode = true;
    document.getElementById("btn-new-audit").classList.remove("hidden");
  } catch {
    /* unreachable backend — leave button hidden */
  }
}

function wireSettingsModal() {
  const modal = document.getElementById("settings-modal");
  const openBtn = document.getElementById("btn-settings");
  const closeBtn = document.getElementById("settings-modal-close");
  const form = document.getElementById("settings-form");
  const testBtn = document.getElementById("settings-test");
  const result = document.getElementById("settings-result");
  const statusDot = document.getElementById("settings-status-dot");
  if (!modal || !openBtn) return;

  const refreshStatusDot = (ok) => {
    statusDot.className =
      "ml-auto w-2 h-2 rounded-full " +
      (ok
        ? "bg-emerald-500"
        : getApiBase() || getApiToken()
          ? "bg-red-500"
          : "bg-outline-variant");
    statusDot.title = ok
      ? "Connected"
      : getApiBase() || getApiToken()
        ? "Configured but unreachable"
        : "Disconnected";
  };

  const probe = async () => {
    try {
      const res = await apiFetch("/api/manifest");
      const ok =
        res.ok && (res.headers.get("content-type") || "").includes("json");
      refreshStatusDot(ok);
      return { ok, status: res.status };
    } catch (err) {
      refreshStatusDot(false);
      return { ok: false, status: 0, err: err.message };
    }
  };

  openBtn.addEventListener("click", () => {
    form.elements.api_base.value = getApiBase();
    form.elements.api_token.value = getApiToken();
    result.textContent = "";
    modal.classList.remove("hidden");
  });
  closeBtn.addEventListener("click", () => modal.classList.add("hidden"));
  modal.addEventListener("click", (e) => {
    if (e.target === modal) modal.classList.add("hidden");
  });

  testBtn.addEventListener("click", async () => {
    const prevBase = getApiBase(),
      prevTok = getApiToken();
    setApiConfig(
      form.elements.api_base.value.trim(),
      form.elements.api_token.value,
    );
    const { ok, status, err } = await probe();
    setApiConfig(prevBase, prevTok); // probe only — don't persist on test
    result.textContent = ok
      ? "✓ reachable"
      : err
        ? `✗ ${err}`
        : `✗ HTTP ${status}${status === 401 ? " — bad token" : ""}`;
    result.style.color = ok ? "#4ade80" : "#ef4444";
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    setApiConfig(
      form.elements.api_base.value.trim(),
      form.elements.api_token.value,
    );
    const { ok, status, err } = await probe();
    result.textContent = ok
      ? "✓ saved + reachable — closing…"
      : err
        ? `saved but unreachable: ${err}`
        : `saved but HTTP ${status}`;
    result.style.color = ok ? "#4ade80" : "#facc15";
    // Reflect into the rest of the UI immediately.
    const newAuditBtn = document.getElementById("btn-new-audit");
    if (ok) {
      state.liveMode = true;
      newAuditBtn?.classList.remove("hidden");
      setTimeout(() => modal.classList.add("hidden"), 800);
    } else {
      state.liveMode = false;
      newAuditBtn?.classList.add("hidden");
    }
  });

  // Initial dot color reflects whatever's in localStorage.
  probe();
}

function wireAuditModal() {
  const modal = document.getElementById("audit-modal");
  const modalInner = document.getElementById("audit-modal-inner");
  const openBtn = document.getElementById("btn-new-audit");
  const closeBtn = document.getElementById("audit-modal-close");
  const form = document.getElementById("audit-form");
  const logWrap = document.getElementById("audit-log-wrap");
  const logEl = document.getElementById("audit-log");
  const liveWrap = document.getElementById("audit-live-wrap");
  const liveText = document.getElementById("audit-live-text");
  const liveSpeaker = document.getElementById("audit-live-speaker");
  const liveTurnLabel = document.getElementById("audit-live-turn-label");
  const liveCellTag = document.getElementById("audit-live-cell-tag");
  const liveDp = document.getElementById("audit-live-dp");
  const liveSidePill = document.getElementById("audit-live-side-pill");
  const cellsStrip = document.getElementById("audit-cells-strip");
  const statusDot = document.getElementById("audit-status-dot");
  const statusLabel = document.getElementById("audit-status-label");
  const activeMeta = document.getElementById("audit-active-meta");
  if (!openBtn || !modal) return;

  // Per-audit live state — reset on each open.
  const live = { activeDp: null, cells: new Map() };

  const reset = () => {
    form.classList.remove("hidden");
    logWrap.classList.add("hidden");
    liveWrap.classList.add("hidden");
    modalInner.classList.remove("max-w-4xl");
    modalInner.classList.add("max-w-2xl");
    logEl.textContent = "";
    liveText.textContent = "";
    liveSpeaker.textContent = "—";
    liveTurnLabel.textContent = "OPEN";
    liveCellTag.textContent = "—";
    liveDp.textContent = "";
    cellsStrip.innerHTML = "";
    activeMeta.textContent = "";
    statusDot.className = "w-2 h-2 rounded-full bg-emerald-500 animate-pulse";
    statusLabel.textContent = "RUNNING";
    live.activeDp = null;
    live.cells.clear();
  };

  openBtn.addEventListener("click", () => {
    reset();
    modal.classList.remove("hidden");
  });
  closeBtn.addEventListener("click", () => modal.classList.add("hidden"));
  modal.addEventListener("click", (e) => {
    if (e.target === modal) modal.classList.add("hidden");
  });

  const ensureLivePanel = () => {
    if (liveWrap.classList.contains("hidden")) {
      liveWrap.classList.remove("hidden");
      // Modal widens so the streaming text has room to breathe.
      modalInner.classList.remove("max-w-2xl");
      modalInner.classList.add("max-w-4xl");
    }
  };

  const renderCellsStrip = (dpId) => {
    cellsStrip.innerHTML = "";
    const cells = [...live.cells.values()]
      .filter((c) => c.dpId === dpId)
      .sort((a, b) => a.cellId - b.cellId);
    for (const c of cells) {
      const tone =
        c.status === "voted"
          ? c.position === "debt"
            ? "bg-[#ef4444]/30 border-[#ef4444]"
            : "bg-[#4ade80]/30 border-[#4ade80]"
          : c.status === "active"
            ? "bg-primary/40 border-primary animate-pulse"
            : c.status === "failed"
              ? "bg-error/30 border-error"
              : "bg-surface-container border-outline-variant";
      const dot = document.createElement("span");
      dot.className = `w-5 h-5 border ${tone} font-code-sm text-[9px] flex items-center justify-center text-on-surface`;
      dot.textContent = String(c.cellId);
      dot.title = `Cell ${c.cellId} · ${c.red || ""} vs ${c.blue || ""} · ${c.status}${c.position ? ` (${c.position})` : ""}`;
      cellsStrip.appendChild(dot);
    }
  };

  const handleEvent = (ev) => {
    const cell = ev.cell || {};
    const turn = ev.turn || {};
    const dpId = cell.dp_id;
    const cellId = cell.cell_id;

    if (!dpId || cellId == null) {
      // Some events (judge, report) won't carry cell context — just log.
      return;
    }

    const key = `${dpId}#${cellId}`;
    let entry = live.cells.get(key);
    if (!entry) {
      entry = {
        dpId,
        cellId,
        red: cell.red,
        blue: cell.blue,
        status: "queued",
        position: null,
      };
      live.cells.set(key, entry);
    }

    switch (ev.t) {
      case "cell_start":
        entry.status = "active";
        live.activeDp = dpId;
        liveDp.textContent = `${cell.principle || ""} · ${dpId}`;
        liveCellTag.textContent = `cell ${cellId} · ${cell.red} vs ${cell.blue}`;
        activeMeta.textContent = `${cell.principle || ""} · cell ${cellId}/10`;
        ensureLivePanel();
        renderCellsStrip(dpId);
        break;

      case "turn_start":
        // Switch the visible text area to the new speaker. Tokens for the
        // previous turn stay in entry.transcript[label] for posterity.
        liveText.textContent = "";
        const speaker = turn.speaker || "";
        liveSpeaker.textContent = speaker;
        liveTurnLabel.textContent = (turn.label || "").toUpperCase();
        const isRed = speaker.startsWith("red");
        const isVote = speaker === "vote";
        liveSidePill.textContent = isVote ? "VOTE" : isRed ? "RED" : "BLUE";
        liveSidePill.style.color = isVote
          ? "#c8c6c7"
          : isRed
            ? "#ef4444"
            : "#4ade80";
        liveSidePill.style.borderColor = liveSidePill.style.color;
        break;

      case "token":
        if (ev.text) {
          liveText.textContent += ev.text;
          liveText.scrollTop = liveText.scrollHeight;
        }
        break;

      case "turn_end":
        // Leave the text on screen so the user can read it before the next
        // turn clears the panel.
        break;

      case "cell_voted":
        entry.status = "voted";
        entry.position = ev.position;
        renderCellsStrip(dpId);
        break;

      case "cell_failed":
        entry.status = "failed";
        renderCellsStrip(dpId);
        break;
    }
  };

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const body = {
      repo_url: fd.get("repo_url"),
      slug: fd.get("slug")?.trim() || undefined,
      language: fd.get("language") || "auto",
    };

    let res;
    try {
      res = await apiFetch("/api/audits", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    } catch (err) {
      alert(`Couldn't reach the backend: ${err.message}`);
      return;
    }
    if (!res.ok) {
      const msg = (await res.json().catch(() => ({}))).detail || res.statusText;
      alert(`Audit refused: ${msg}`);
      return;
    }
    const { job_id, slug } = await res.json();

    form.classList.add("hidden");
    logWrap.classList.remove("hidden");

    const sse = apiEventSource(`/api/audits/${job_id}/stream`);
    sse.addEventListener("log", (e) => {
      logEl.textContent += stripAnsi(e.data) + "\n";
      logEl.scrollTop = logEl.scrollHeight;
    });
    sse.addEventListener("event", (e) => {
      try {
        handleEvent(JSON.parse(e.data));
      } catch {
        /* malformed — skip */
      }
    });
    sse.addEventListener("done", async (e) => {
      const info = JSON.parse(e.data);
      sse.close();
      const ok = info.status === "completed";
      statusDot.className = `w-2 h-2 rounded-full ${ok ? "bg-emerald-500" : "bg-red-500"}`;
      statusLabel.textContent = ok
        ? `COMPLETED — switching to "${slug}"`
        : `FAILED${info.error ? ": " + info.error : ""}`;
      if (ok) {
        const mres = await fetch("data/manifest.json", { cache: "no-store" });
        state.manifest = await mres.json();
        renderAuditSwitcher();
        await loadAudit(slug);
        setTimeout(() => modal.classList.add("hidden"), 2000);
      }
    });
    sse.onerror = () => {
      statusDot.className = "w-2 h-2 rounded-full bg-red-500";
      statusLabel.textContent = "DISCONNECTED";
    };
  });
}

document.addEventListener("DOMContentLoaded", init);
