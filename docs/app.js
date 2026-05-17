/* Forum frontend — 4-view SPA.
 *
 * Loads docs/data/manifest.json → user picks an audit → loads that audit's
 * artifacts → renders Evidence, Prioritization, AI Jury, Briefing views.
 * Sliders re-project rankings + dissent salience in the browser via a JS
 * port of `src/forum/whatif/probe.py` math. Zero LLM calls; the page is
 * static-hostable on GitHub Pages.
 *
 * Dependency graph uses Cytoscape.js with dagre layout for clear
 * hierarchical visualization of module relationships.
 */

// Cytoscape.js + dagre layout loaded via <script> tags in index.html

const VALUES = [
  "scalability",
  "maintainability",
  "velocity",
  "correctness",
  "simplicity",
  "flexibility",
];
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
  // P8 (stable abstractions): mis-placement on the I/A plane hurts
  // flexibility and maintainability most; concrete-stable code is hard to
  // extend, abstract-unstable code is design overhead.
  P8: {
    scalability: 0.3,
    maintainability: 0.7,
    velocity: -0.3,
    correctness: 0.2,
    simplicity: 0.4,
    flexibility: 0.9,
  },
  // P9 (god class/function): pure size/complexity concern. Hits
  // maintainability and simplicity hard, correctness via inability to
  // exhaustively reason about the unit.
  P9: {
    scalability: 0.3,
    maintainability: 0.9,
    velocity: 0.2,
    correctness: 0.7,
    simplicity: 0.9,
    flexibility: 0.4,
  },
  // P10 (duplication): two sites drift silently. Maintainability + velocity
  // (every fix has to be applied twice); correctness suffers when one drifts.
  P10: {
    scalability: 0.2,
    maintainability: 0.8,
    velocity: 0.6,
    correctness: 0.5,
    simplicity: 0.7,
    flexibility: 0.3,
  },
};

// Plain-English subtitles for each verdict label. Used wherever a verdict
// is shown in the UI; the labels themselves are jargon the judge writes
// verbatim and can't be changed at the source.
const VERDICT_PLAIN = {
  HEALTHY: "No problem found — don't touch it.",
  "JUSTIFIED VIOLATION":
    "Yes there's a textbook issue, but it's defensible. Leave it alone.",
  "STRUCTURAL DEBT": "Real problem, real cost. Worth refactoring.",
  CRITICAL: "Real problem actively hurting you. Refactor urgently.",
  DRIFTED: "Original design was sound; code wandered away. Restore the design.",
  CONTESTED: "Panel split too badly — a human architect should weigh in.",
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
  P8: "Mis-placed abstraction",
  P9: "God class / god function",
  P10: "Copy-pasted code",
};
const PRINCIPLE_SUBTITLES = {
  P1: "Modules import each other in a cycle. Can't ship one without the other.",
  P2: "Stable module depends on an unstable one. Inherits volatility.",
  P3: "Cyclomatic complexity above 15. Too many branches to reason about.",
  P4: "Class methods barely share state. Probably two classes glued together.",
  P5: "Dead code — nothing reaches it.",
  P6: "Deep module imports back to the entry point. Layering violation.",
  P7: "Files in different packages change together. Boundary mis-cut.",
  P8: "Stable concrete code (no seams to extend) or abstract code nothing uses.",
  P9: "One class/function exceeds multiple size thresholds at once.",
  P10: "A 30+ line block appears in more than one file. Drifts silently.",
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

// --- live-audit tracker (set when the new-audit modal kicks off a job) ---
// Used by handleEvent so server-streamed `tribunal_complete` events can
// re-fetch and re-render the audit-in-progress without the user clicking.
let currentRunningSlug = null;
let _liveRefreshPending = null; // simple debouncer

async function liveRefreshAudit(slug) {
  // Debounce: many tribunal_complete events arriving back-to-back collapse
  // into one refresh. ~250ms is short enough to feel live, long enough to
  // avoid hammering loadAudit while the server's still copying files.
  if (_liveRefreshPending) return;
  _liveRefreshPending = setTimeout(async () => {
    _liveRefreshPending = null;
    try {
      // Make sure the manifest has this slug — if the audit hasn't been
      // published yet, inject a temporary entry so loadAudit can find it.
      if (!state.manifest.audits.find((a) => a.slug === slug)) {
        state.manifest.audits.push({
          slug,
          label: slug,
          version: "live",
          language: "python",
          source: "(in-progress audit)",
          commit: "live",
          note: "Audit running — verdicts appear as each finding's debate concludes.",
        });
        renderAuditSwitcher();
      }
      // Only re-render if the user is already on this slug (don't yank them
      // mid-browse). If they're elsewhere, the pill will switch to this on
      // SSE 'done' anyway.
      if (state.activeSlug === slug) {
        await loadAudit(slug);
      }
    } catch (e) {
      console.error("liveRefreshAudit failed:", e);
    }
  }, 250);
}

// --- shared state ---
const state = {
  manifest: null,
  activeSlug: null,
  evidence: null,
  prioritized: null,
  verdicts: [],
  reportMd: "",
  graphJson: null,
  cyGraph: null,
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

  // Empty manifest → show the landing page instead of trying to load a
  // (non-existent) default audit. Otherwise load and render normally.
  if (!state.manifest.audits || state.manifest.audits.length === 0) {
    showLanding();
  } else {
    await loadAudit(state.manifest.default || state.manifest.audits[0].slug);
  }
}

// Switch the main area to the landing view, hide the pipeline + tools
// navigation since there's nothing to navigate to, and disable the
// nav-item highlight that switchView would otherwise apply.
function showLanding() {
  document.querySelectorAll("section.view").forEach((s) => {
    s.classList.toggle("hidden", s.dataset.view !== "landing");
  });
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.remove("active"));
  state.activeView = "landing";
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
  // Capture the slug we were asked for so a rapid sequence of pill clicks
  // doesn't have a stale fetch overwrite the freshly-selected audit's data.
  const requestedSlug = slug;
  state.activeSlug = slug;
  // Reset cross-audit UI state so the next render doesn't show "moved
  // up by N" arrows based on the previous audit's rankings.
  lastRanking = null;
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
  // If the user switched audits while these were in flight, drop this
  // response — the newer call owns state now.
  if (state.activeSlug !== requestedSlug) return;
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
  // Each step is wrapped so one bad render doesn't blank the others — the
  // console error names which view crashed and on what data.
  const safe = (label, fn) => {
    try {
      fn();
    } catch (e) {
      console.error(`render(${label}) failed for "${slug}":`, e, {
        evidence,
        prioritized,
        verdicts,
      });
    }
  };
  safe("topBar", () => renderTopBar(entry));
  safe("evidence", () => renderEvidence());
  safe("prioritization", () => renderPrioritization());
  safe("jury", () => renderJury());
  safe("briefing", () => renderBriefing());
  switchView(state.activeView);
}

// =====================================================================
// Top bar + footer + sidebar audit switcher
// =====================================================================

function renderTopBar(entry) {
  // CLI command on Evidence view
  const langFlag =
    entry.language === "auto" ? "" : ` --language ${entry.language}`;
  document.getElementById("cli-display").value =
    `forum audit ${entry.source || "<repo>"}${langFlag} --top-n ${state.verdicts.length || 5} --cell-backend wafer`;
}

function renderAuditSwitcher() {
  const root = document.getElementById("audit-switcher");
  root.innerHTML = "";
  for (const entry of state.manifest.audits) {
    const btn = document.createElement("div");
    btn.className = "audit-pill group";
    btn.dataset.slug = entry.slug;
    // Whitelist the language class so a crafted manifest can't break out of
    // the class attribute; render the language text itself escaped.
    const langClass = /^[a-z0-9_-]{1,32}$/i.test(String(entry.language || ""))
      ? `lang-${entry.language}`
      : "";
    btn.innerHTML = `
      <span class="flex-1 min-w-0 truncate">${escapeHtml(entry.label)}</span>
      <span class="lang ${langClass}">${escapeHtml(entry.language)}</span>
      <button class="audit-pill-delete opacity-0 group-hover:opacity-100 hover:text-error transition-opacity"
              title="Delete this audit"
              data-slug="${escapeHtml(entry.slug)}">
        <span class="material-symbols-outlined text-[14px] align-middle">close</span>
      </button>`;
    // .title is set via property (text-only), so no escaping needed here.
    btn.title = `${entry.source ?? ""} @ ${entry.commit ?? ""}\n\n${entry.note ?? ""}`;
    btn.addEventListener("click", (e) => {
      // Don't switch audits when the ✕ inside the pill was clicked.
      if (e.target.closest(".audit-pill-delete")) return;
      if (state.activeSlug !== entry.slug) loadAudit(entry.slug);
    });
    btn.querySelector(".audit-pill-delete").addEventListener("click", (e) => {
      e.stopPropagation();
      deleteAudit(entry);
    });
    root.appendChild(btn);
  }
}

async function deleteAudit(entry) {
  if (!state.liveMode) {
    alert(
      `Delete requires the live backend (server.py) — this page is currently ` +
        `in static mode and can only read files. Configure a backend URL in Settings to enable delete.`,
    );
    return;
  }
  if (
    !confirm(
      `Delete audit "${entry.label}"?\n\nThis removes docs/data/${entry.slug}/ from disk and the entry from manifest.json. Cannot be undone.`,
    )
  ) {
    return;
  }
  let res;
  try {
    res = await apiFetch(`/api/audits/${encodeURIComponent(entry.slug)}`, {
      method: "DELETE",
    });
  } catch (err) {
    alert(`Couldn't reach the backend: ${err.message}`);
    return;
  }
  if (!res.ok) {
    const msg = (await res.json().catch(() => ({}))).detail || res.statusText;
    alert(`Delete refused: ${msg}`);
    return;
  }

  // Refresh manifest + sidebar. If the deleted slug was the active one,
  // switch to whatever's left (or show a hint if the manifest is now empty).
  try {
    const mres = await fetch("data/manifest.json", { cache: "no-store" });
    state.manifest = await mres.json();
  } catch (err) {
    alert(`Deleted, but couldn't reload manifest: ${err.message}`);
    return;
  }
  renderAuditSwitcher();
  if (state.activeSlug === entry.slug) {
    const fallback = state.manifest.default || state.manifest.audits[0]?.slug;
    if (fallback) {
      await loadAudit(fallback);
    } else {
      // Nothing left to display — fall back to the marketing landing page
      // with the "+ NEW AUDIT" CTA front and center.
      state.activeSlug = null;
      showLanding();
    }
  }
}

function markAuditActive(slug) {
  document
    .querySelectorAll(".audit-pill")
    .forEach((b) => b.classList.toggle("active", b.dataset.slug === slug));
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

  // Re-build the 3D dependency graph when switching INTO Evidence. The
  // original build in loadAudit() runs while Evidence may still be hidden,
  // so the canvas initializes at 0×0. Rebuilding here guarantees the
  // container has real dimensions before Three.js sizes itself.
  if (view === "evidence" && state.graphJson) {
    // requestAnimationFrame waits for the browser to apply the un-hide
    // and recompute layout, so clientWidth/Height are non-zero.
    requestAnimationFrame(() => {
      try {
        renderDependencyGraph();
      } catch (e) {
        console.error("graph rebuild failed:", e);
      }
    });
  }
}

function wireButtons() {
  // Sidebar RE-RUN → arm-then-fire (two-click confirmation). First click
  // shows "CONFIRM?" in red for 4s; second click within the window
  // overwrites the current audit's slug with a fresh run on the same repo.
  document
    .getElementById("btn-rerun")
    ?.addEventListener("click", handleRerunClick);
  // Landing CTA → trigger the same modal as the sidebar's "+ NEW AUDIT".
  document
    .getElementById("btn-landing-new-audit")
    ?.addEventListener("click", () => {
      const sidebarBtn = document.getElementById("btn-new-audit");
      if (sidebarBtn && !sidebarBtn.classList.contains("hidden")) {
        sidebarBtn.click();
      } else {
        alert(
          "New audits require the live backend. Configure it via the " +
          "Live backend dialog in the sidebar, or run `uvicorn server:app` locally."
        );
      }
    });
  // Clicking outside the button disarms it (keeps the UI honest if the
  // user moves on without confirming).
  document.addEventListener("click", (e) => {
    const btn = document.getElementById("btn-rerun");
    if (!btn || btn.dataset.armed !== "yes") return;
    if (e.target !== btn && !btn.contains(e.target)) disarmRerunButton(btn);
  });
  // Sidebar EXPORT REPORT → downloads report.md
  document
    .getElementById("btn-export")
    ?.addEventListener("click", downloadReport);
  // Jury jump-to-action
  document
    .getElementById("btn-scroll-action")
    ?.addEventListener("click", () => {
      switchView("briefing");
      // Briefing now ends with a "What to do, in order" section (per the new
      // strategic-synthesis prompt). Find the matching H2 and scroll to it;
      // fall back to the top of the briefing body if not present.
      setTimeout(() => {
        const headings = document.querySelectorAll("#brief-markdown h2");
        const actionHeading = [...headings].find((h) =>
          /what to do|action plan|sequenced/i.test(h.textContent),
        );
        const target = actionHeading || document.querySelector("#brief-markdown");
        target?.scrollIntoView({ behavior: "smooth", block: "start" });
      }, 80);
    });
  // Reset sliders to baseline
  document
    .getElementById("btn-reset")
    ?.addEventListener("click", () => applyPreset("baseline"));

  // Evidence view: collapse "WHAT THE CHECKS FOUND" panel + expand graph.
  // Just toggles the left column's visibility and swaps the graph col's
  // span. After the layout change settles, rebuild the 3D graph so its
  // canvas matches the new container width.
  const toggleBtn  = document.getElementById("btn-ev-toggle-panel");
  const leftPanel  = document.getElementById("ev-left-panel");
  const graphCol   = document.getElementById("ev-graph-col");
  const toggleIcon = document.getElementById("btn-ev-toggle-icon");
  const toggleLbl  = document.getElementById("btn-ev-toggle-label");
  if (toggleBtn && leftPanel && graphCol) {
    toggleBtn.addEventListener("click", () => {
      const isCollapsed = toggleBtn.dataset.collapsed === "true";
      if (isCollapsed) {
        // Restoring the panel.
        leftPanel.classList.remove("hidden");
        graphCol.classList.remove("lg:col-span-12");
        graphCol.classList.add("lg:col-span-8");
        toggleBtn.dataset.collapsed = "false";
        toggleIcon.textContent = "fullscreen";
        toggleLbl.textContent  = "EXPAND GRAPH";
      } else {
        // Collapsing — hide left, give the graph the full row.
        leftPanel.classList.add("hidden");
        graphCol.classList.remove("lg:col-span-8");
        graphCol.classList.add("lg:col-span-12");
        toggleBtn.dataset.collapsed = "true";
        toggleIcon.textContent = "fullscreen_exit";
        toggleLbl.textContent  = "SHOW FINDINGS";
      }
      // 3D-force-graph needs a rebuild against the new container width.
      if (state.graphJson) {
        requestAnimationFrame(() => {
          try { renderDependencyGraph(); }
          catch (e) { console.error("graph rebuild after panel toggle failed:", e); }
        });
      }
    });
  }
  // Copy report markdown to clipboard for pasting into an agent
  document.getElementById("btn-copy-agent")?.addEventListener("click", () => {
    const md = state.reportMd;
    if (!md) return;
    navigator.clipboard.writeText(md).then(() => {
      const btn = document.getElementById("btn-copy-agent");
      const orig = btn.innerHTML;
      btn.innerHTML = `<span class="material-symbols-outlined text-[16px]">check</span> COPIED`;
      setTimeout(() => {
        btn.innerHTML = orig;
      }, 2000);
    });
  });
}

// Two-click arm/fire pattern on RE-RUN:
//   Click 1 → button transforms into "CONFIRM?" for 4 seconds
//   Click 2 within the window → fires the re-run on the active audit
//   Outside the window or click elsewhere → reverts silently
// The actual re-run overwrites the same slug, reusing the audit-modal's
// existing form-submit flow (POST + SSE stream + live progress UI).
let rerunArmedTimer = null;
const RERUN_ARM_MS = 4000;

function disarmRerunButton(btn) {
  if (!btn) return;
  if (rerunArmedTimer) {
    clearTimeout(rerunArmedTimer);
    rerunArmedTimer = null;
  }
  btn.dataset.armed = "";
  btn.textContent = "RE-RUN";
  btn.style.background = "";
  btn.style.color = "";
}

function handleRerunClick() {
  const btn = document.getElementById("btn-rerun");
  if (!btn) return;
  if (!state.liveMode) {
    alert(
      "Re-run requires the live backend. Static demo mode is read-only — " +
      "configure a backend URL in Settings or run `uvicorn server:app` locally."
    );
    return;
  }
  if (btn.dataset.armed === "yes") {
    // Second click within the window → fire.
    disarmRerunButton(btn);
    fireCurrentAuditRerun();
    return;
  }
  // First click → arm. The button visually transforms so the user knows
  // the next click commits.
  btn.dataset.armed = "yes";
  btn.textContent = "CONFIRM?";
  btn.style.background = "#ef4444";
  btn.style.color = "#0a0a0b";
  rerunArmedTimer = setTimeout(() => disarmRerunButton(btn), RERUN_ARM_MS);
}

function fireCurrentAuditRerun() {
  const entry = state.manifest.audits.find(a => a.slug === state.activeSlug);
  if (!entry) {
    alert("No active audit to re-run. Pick one from the sidebar first.");
    return;
  }
  // Map "github.com/foo/bar" → "https://github.com/foo/bar" (git clone-able).
  let repoUrl = String(entry.source || "");
  if (repoUrl && !/^https?:\/\//.test(repoUrl) && !/^git@/.test(repoUrl)) {
    repoUrl = "https://" + repoUrl.replace(/^\/+/, "");
  }
  const modal = document.getElementById("audit-modal");
  const form  = document.getElementById("audit-form");
  if (!modal || !form) {
    alert("New-Audit modal isn't on the page — can't re-run.");
    return;
  }
  // Pre-fill the form with the active audit's params using the SAME slug
  // (server overwrites in place). The audit-modal's submit handler does
  // the actual POST + SSE wiring; we just open the modal so the user
  // can watch progress.
  if (form.elements.repo_url) form.elements.repo_url.value = repoUrl;
  if (form.elements.slug)     form.elements.slug.value     = entry.slug;
  if (form.elements.language && entry.language && entry.language !== "auto") {
    form.elements.language.value = entry.language;
  }
  // Signal the submit handler that this is an explicit re-run, so it
  // passes overwrite:true and the server wipes the existing slug dir
  // instead of 409-ing on the collision.
  form.dataset.rerun = "yes";
  modal.classList.remove("hidden");
  // Submit the form via the native API so its existing event listener fires.
  if (typeof form.requestSubmit === "function") form.requestSubmit();
  else form.dispatchEvent(new Event("submit", { cancelable: true }));
}


function downloadReport() {
  if (!state.reportMd) {
    alert("No report.md for this audit.");
    return;
  }
  const blob = new Blob([state.reportMd], { type: "text/markdown" });
  triggerDownload(blob, `${state.activeSlug}-report.md`);
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

  // Empty-audit callout: when Layer 1 found nothing, the user sees a blank UI
  // unless we explain why. Most common causes are non-Python/C repos or all
  // source under a directory Forum skips (tests/, docs/, scripts/, …).
  const gs = e.graph_summary || {};
  if (e.decision_points.length === 0 || (gs.num_modules || 0) === 0) {
    document.getElementById("ev-metrics").innerHTML = `
      <div class="bg-error/10 border border-error/40 p-4 rounded">
        <div class="font-label-caps text-label-caps text-error mb-2">NO FINDINGS</div>
        <p class="text-[12px] text-on-surface leading-relaxed">
          Forum walked this repo but found nothing to audit. Three usual causes:
        </p>
        <ul class="text-[12px] text-on-surface-variant mt-2 space-y-1 list-disc list-inside leading-snug">
          <li>The repo has no <code>.py</code> or <code>.c</code> files (Forum is Python + C only — TypeScript / JS / notebooks are skipped).</li>
          <li>All source lives under a skipped directory: <code>tests</code>, <code>docs</code>, <code>scripts</code>, <code>examples</code>, <code>build</code>, <code>vendor</code>, <code>node_modules</code>, etc.</li>
          <li>No top-level Python package (no folder with an <code>__init__.py</code>) and no <code>src/</code> dir for C.</li>
        </ul>
        <p class="text-[11px] text-on-surface-variant opacity-70 mt-3">
          Try another repo, or relax Forum's <code>SKIP_DIRS</code> in <code>src/forum/evidence/utils.py</code>.
        </p>
      </div>`;
    document.getElementById("ev-telemetry").innerHTML = `
      <div class="flex justify-between"><span>Files analyzed</span><span class="text-error">0</span></div>
      <div class="flex justify-between"><span>Imports between files</span><span class="text-error">0</span></div>
      <div class="flex justify-between"><span>Top-level packages</span><span class="text-error">0</span></div>`;
    document.getElementById("ev-graph-stats").textContent = "no graph";
    const wrap = document.getElementById("ev-graph-wrap");
    if (wrap)
      wrap.innerHTML = `<div style="padding:48px;color:#919094;text-align:center">No dependency graph — Layer 1 found no source files to analyze.</div>`;
    return;
  }

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

  // Telemetry pane (plain-English labels) — `gs` already declared at top of fn.
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
// Dependency graph (Cytoscape.js + dagre layout)
// =====================================================================

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
  if (state.cyGraph) {
    state.cyGraph.destroy();
    state.cyGraph = null;
  }
  container.innerHTML = "";

  const data = state.graphJson;
  if (!data || !data.nodes?.length) {
    container.innerHTML = `<div style="padding:24px;color:#919094;text-align:center">No dependency graph on disk for this audit.</div>`;
    return;
  }

  if (!window.cytoscape) {
    container.innerHTML = `<div style="padding:24px;color:#919094;text-align:center">Cytoscape.js library not loaded.</div>`;
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

  // Identify modules with errors (decision points from evidence). Match on
  // whole tokens, not substrings — a finding on `foo.bar` was previously
  // also flagging `bar`, `foo`, and any module sharing those name fragments.
  const errorModules = new Set();
  if (state.evidence?.decision_points) {
    for (const dp of state.evidence.decision_points) {
      if (dp.subject) errorModules.add(dp.subject);
      if (dp.evidence?.module) errorModules.add(dp.evidence.module);
      if (dp.subject) {
        const tokens = new Set(dp.subject.split(/[\s.,;:()\[\]{}"'`]+/));
        for (const n of data.nodes) {
          if (tokens.has(n.id)) errorModules.add(n.id);
        }
      }
    }
  }

  // Determine which nodes are packages (have children) vs leaf files.
  const allIds = new Set(data.nodes.map((n) => n.id));
  const isPackage = (id) =>
    data.nodes.some(
      (other) => other.id !== id && other.id.startsWith(id + "."),
    );

  // Build cytoscape elements.
  const elements = [];
  for (const n of data.nodes) {
    const fi = fanIn.get(n.id) || 0;
    const isPkg = isPackage(n.id);
    elements.push({
      data: {
        id: n.id,
        label: (isPkg ? "📦 " : "") + (n.label || n.id),
        pkg: n.pkg,
        color: errorModules.has(n.id) ? "#ef4444" : colorFor(n.pkg),
        hasError: errorModules.has(n.id),
        isPackage: isPkg,
        size: isPkg ? 140 + (fi / maxFanIn) * 100 : 90 + (fi / maxFanIn) * 80,
        fanIn: fi,
      },
    });
  }
  for (const e of data.edges) {
    elements.push({
      data: {
        id: e.source + "->" + e.target,
        source: e.source,
        target: e.target,
        color: colorFor(data.nodes.find((n) => n.id === e.source)?.pkg || ""),
      },
    });
  }

  const cy = cytoscape({
    container,
    elements,
    style: [
      {
        selector: "node",
        style: {
          "background-color": "data(color)",
          label: "data(label)",
          color: "data(color)",
          "font-size": "28px",
          "font-family": "JetBrains Mono, monospace",
          "font-weight": "bold",
          "text-valign": "bottom",
          "text-margin-y": 10,
          width: "data(size)",
          height: "data(size)",
          "border-width": 3,
          "border-color": "data(color)",
          "border-opacity": 0.8,
          "background-opacity": 0.3,
          "text-outline-width": 3,
          "text-outline-color": "#0e0e0e",
          "text-outline-opacity": 1,
        },
      },
      {
        selector: "node[?isPackage]",
        style: {
          shape: "round-rectangle",
          "border-width": 4,
          "border-style": "double",
          "background-opacity": 0.35,
          "font-size": "32px",
        },
      },
      {
        selector: "node[?hasError]",
        style: {
          "border-width": 3,
          "background-opacity": 0.3,
          "border-style": "solid",
        },
      },
      {
        selector: "edge",
        style: {
          width: 1,
          "line-color": "data(color)",
          "line-opacity": 0.3,
          "target-arrow-color": "data(color)",
          "target-arrow-shape": "triangle",
          "arrow-scale": 0.8,
          "curve-style": "bezier",
        },
      },
      {
        selector: "node.hover",
        style: {
          "background-opacity": 0.5,
          "border-width": 4,
          "font-size": "32px",
        },
      },
      {
        selector: "node.neighbor",
        style: {
          "background-opacity": 0.3,
          "border-width": 2,
        },
      },
      {
        selector: "node.dimmed",
        style: {
          opacity: 0.15,
          "text-opacity": 0.1,
        },
      },
      {
        selector: "edge.highlighted",
        style: {
          width: 2.5,
          "line-opacity": 0.8,
          "target-arrow-color": "data(color)",
        },
      },
      {
        selector: "edge.dimmed",
        style: {
          opacity: 0.05,
        },
      },
    ],
    layout: {
      name: "dagre",
      rankDir: "TB",
      nodeSep: 50,
      rankSep: 80,
      edgeSep: 20,
      animate: false,
    },
    minZoom: 0.2,
    maxZoom: 3,
    wheelSensitivity: 0.3,
  });

  // Hover interactions.
  cy.on("mouseover", "node", (e) => {
    const node = e.target;
    const neighborhood = node.neighborhood();
    cy.elements().addClass("dimmed");
    node.removeClass("dimmed").addClass("hover");
    neighborhood.nodes().removeClass("dimmed").addClass("neighbor");
    neighborhood.edges().removeClass("dimmed").addClass("highlighted");
    node.connectedEdges().removeClass("dimmed").addClass("highlighted");
    container.style.cursor = "pointer";
  });

  cy.on("mouseout", "node", () => {
    cy.elements().removeClass("dimmed hover neighbor highlighted");
    container.style.cursor = "grab";
  });

  // Click to show findings.
  cy.on("tap", "node", (e) => {
    showNodeFindings(e.target.id());
  });

  // Fit with padding after layout.
  cy.fit(undefined, 40);

  state.cyGraph = cy;

  // ---- Legend ----
  // Floating panel bottom-left explaining what each color and node-shape means.
  // Without this users have to guess what the colors mean (e.g., why is most
  // of shadowbroker yellow? — because `services` happens to land in the 2nd
  // palette slot, not because anything is wrong with services).
  const oldLegend = container.querySelector(".graph-legend");
  if (oldLegend) oldLegend.remove();
  const legend = document.createElement("div");
  legend.className = "graph-legend";
  legend.style.cssText = `
    position: absolute; left: 12px; bottom: 12px; z-index: 5;
    background: rgba(20, 19, 19, 0.92); border: 1px solid #46464a;
    padding: 8px 10px; min-width: 140px; max-width: 240px;
    font-family: "JetBrains Mono", monospace; font-size: 10px;
    color: #c7c6ca; line-height: 1.4;
    backdrop-filter: blur(4px);
  `;
  const counts = new Map();
  for (const n of data.nodes) counts.set(n.pkg, (counts.get(n.pkg) || 0) + 1);
  const pkgRows = pkgOrder
    .map((pkg) => {
      const c = colorFor(pkg);
      // Package names come from the indexed source — escape so arbitrary
      // module identifiers can't inject HTML/script into the legend.
      return (
        `<div style="display:flex;align-items:center;gap:6px;margin:2px 0">` +
        `<span style="width:10px;height:10px;background:${c};display:inline-block;border:1px solid ${c}"></span>` +
        `<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(pkg || "?")}</span>` +
        `<span style="opacity:0.6">${escapeHtml(String(counts.get(pkg) ?? ""))}</span>` +
        `</div>`
      );
    })
    .join("");
  const errorRow = errorModules.size
    ? `<div style="display:flex;align-items:center;gap:6px;margin:4px 0 2px;padding-top:4px;border-top:1px solid #46464a">
         <span style="width:10px;height:10px;background:#ef4444;display:inline-block;border:1px solid #ef4444"></span>
         <span style="flex:1">has finding</span>
         <span style="opacity:0.6">${errorModules.size}</span>
       </div>`
    : "";
  legend.innerHTML = `
    <div style="font-weight:700;letter-spacing:0.08em;text-transform:uppercase;font-size:9px;color:#919094;margin-bottom:6px">
      LEGEND · COLOR = PACKAGE
    </div>
    ${pkgRows}
    ${errorRow}
    <div style="margin-top:6px;padding-top:4px;border-top:1px solid #46464a;font-size:9px;color:#919094;line-height:1.3">
      <div>□ rounded box = package dir</div>
      <div>○ circle = module file</div>
      <div>size ∝ fan-in (importers)</div>
    </div>
  `;
  container.appendChild(legend);

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
  // Plain-English placeholders in the header explainer.
  const topN = state.prioritized.items.length;
  const totalFindings = state.evidence?.decision_points?.length ?? topN;
  const elN    = document.getElementById("prio-explain-n");
  const elTopN = document.getElementById("prio-explain-topn");
  if (elN)    elN.textContent    = `${totalFindings}`;
  if (elTopN) elTopN.textContent = `${topN}`;
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

// Severity buckets for the per-card pill. `structural` is the average of 5
// normalized impact features in [0,1]; thresholds split into three readable
// tiers. Colors match the site's existing red/orange/grey accents.
const SEVERITY_BUCKETS = [
  { min: 0.66, label: "HIGH", color: "#ef4444" },
  { min: 0.33, label: "MED",  color: "#fb923c" },
  { min: 0.0,  label: "LOW",  color: "#919094" },
];
function severityFor(structural) {
  return SEVERITY_BUCKETS.find((b) => structural >= b.min) ||
    SEVERITY_BUCKETS[SEVERITY_BUCKETS.length - 1];
}

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
  scored.forEach((row, idx) => {
    const oldRank = lastRanking ? lastRanking.indexOf(row.dp.id) : idx;
    const delta = lastRanking ? oldRank - idx : 0;
    const principleName =
      PRINCIPLE_LABELS[row.dp.principle] || row.dp.principle;
    const sev = severityFor(row.structural);

    // Delta indicator: icon + count when rank shifted, em-dash placeholder
    // otherwise (keeps the right column width stable across rows).
    const deltaMarkup =
      delta > 0
        ? `<span class="flex items-center gap-0.5 text-[#4ade80]"
                  title="Moved up ${delta} place${delta === 1 ? "" : "s"}">
             <span class="material-symbols-outlined" style="font-size:16px">arrow_upward</span>
             <span class="font-code-md">${delta}</span>
           </span>`
        : delta < 0
          ? `<span class="flex items-center gap-0.5 text-[#fb923c]"
                    title="Moved down ${Math.abs(delta)} place${Math.abs(delta) === 1 ? "" : "s"}">
               <span class="material-symbols-outlined" style="font-size:16px">arrow_downward</span>
               <span class="font-code-md">${Math.abs(delta)}</span>
             </span>`
          : `<span class="font-code-md text-on-surface-variant opacity-40">—</span>`;

    const div = document.createElement("div");
    div.className =
      "bg-surface-container border border-outline-variant p-4 " +
      "flex items-center justify-between gap-4 " +
      "relative overflow-hidden hover:border-primary/40 transition-colors" +
      (delta !== 0 ? " ring-1 ring-primary/30" : "");
    div.innerHTML = `
      <div class="absolute top-0 left-0 w-1 h-full" style="background:${sev.color}"></div>
      <div class="flex items-start gap-4 min-w-0 flex-1">
        <span class="font-code-md text-primary font-bold pt-0.5 shrink-0">#${idx + 1}</span>
        <div class="min-w-0 flex-1">
          <div class="font-body-md text-on-surface leading-tight">${escapeHtml(row.dp.subject)}</div>
          <div class="font-code-sm text-on-surface-variant text-[11px] mt-1 opacity-70"
               title="${escapeHtml(PRINCIPLE_SUBTITLES[row.dp.principle] || "")}">
            ${escapeHtml(principleName)} (${row.dp.principle})
          </div>
        </div>
      </div>
      <div class="flex items-center gap-6 pl-6 border-l border-outline-variant/30 shrink-0">
        <div class="flex flex-col items-end"
             title="Composite score = structural × (1 + 0.5 × value-affinity)">
          <span class="font-code-md text-primary font-bold">${row.composite.toFixed(2)}</span>
          <span class="font-label-caps text-[9px] text-on-surface-variant opacity-60">COMPOSITE</span>
        </div>
        <span class="font-label-caps text-[10px] tracking-widest px-2 py-1 border"
              style="color:${sev.color};border-color:${sev.color};background:${sev.color}1A"
              title="Static-analysis severity: ${row.structural.toFixed(2)} (0–1)">
          ${sev.label}
        </span>
        <div class="w-10 flex justify-end">${deltaMarkup}</div>
      </div>`;
    root.appendChild(div);
  });
  lastRanking = scored.map((s) => s.dp.id);
}

function refreshAfterSlider() {
  renderRanking();
  renderJuryAggregates(); // re-projected verdict line per tribunal updates too
}

// =====================================================================
// AI Jury view
// =====================================================================

function renderJury() {
  renderJuryDebaters();
  const root = document.getElementById("jury-cells");
  if (!state.verdicts.length) {
    root.innerHTML = `<div class="text-center text-on-surface-variant p-12">No Layer-2 verdicts on disk for this audit.</div>`;
    document.getElementById("jury-stat-tribunals").textContent = "0";
    document.getElementById("jury-stat-cells").textContent = "0";
    document.getElementById("jury-stat-overrides").textContent = "0";
    return;
  }

  root.innerHTML = "";
  let totalCells = 0,
    overrides = 0;

  state.verdicts.forEach((trib, tribIdx) => {
    const dp = state.dpById[trib.decision_point_id];
    const cells = trib.cells || [];
    const judge = trib.judge || {};
    totalCells += cells.length;
    if (judge.override) overrides += 1;

    const principleName =
      PRINCIPLE_LABELS[dp?.principle] || dp?.principle || "?";
    // Summary view — compact card per finding
    const nDebt = cells.filter((c) => c.position === "debt").length;
    const nJust = cells.filter((c) => c.position === "justified").length;
    const majority = nDebt >= nJust ? "debt" : "justified";
    const majorityLabel = majority === "debt" ? "PROBLEM" : "FINE";
    const majorityColor = majority === "debt" ? "#fb923c" : "#4ade80";
    const dissentColor = majority === "debt" ? "#4ade80" : "#fb923c";
    const avgConf = cells.length
      ? Math.round(
          (cells.reduce((s, c) => s + (c.confidence || 0), 0) / cells.length) *
            100,
        )
      : 0;
    const v = String(judge.verdict || "—").toUpperCase();
    const vKey = v.replace(/ /g, "-");

    // Verdict color drives the left-edge stripe and the FINDING-N chip,
    // so the severity is the first thing the eye picks up.
    const VERDICT_COLOR = {
      CRITICAL:               "#ef4444",
      "STRUCTURAL DEBT":      "#fb923c",
      "JUSTIFIED VIOLATION":  "#facc15",
      DRIFTED:                "#c084fc",
      CONTESTED:              "#67e8f9",
      HEALTHY:                "#4ade80",
    };
    const vColor = VERDICT_COLOR[v] || "#919094";

    // ---- Compact summary card ----
    const section = document.createElement("div");
    section.className =
      "mb-4 mt-6 first:mt-0 bg-surface-container border border-outline-variant " +
      "relative overflow-hidden";

    // Vote dots: one dot per cell, colored by position
    const dots = cells
      .map((c) => {
        const color = c.position === "debt" ? "#fb923c" : "#4ade80";
        const pA = PERSONA_INFO[c.red_persona]?.name || c.red_persona;
        const pB = PERSONA_INFO[c.blue_persona]?.name || c.blue_persona;
        return `<span class="inline-block w-3 h-3 rounded-full" style="background:${color}" title="Cell ${c.cell_id + 1}: ${pA} vs ${pB} → ${c.position} (${Math.round((c.confidence || 0) * 100)}%)"></span>`;
      })
      .join("");

    // Best argument from each side
    const majCells = cells.filter((c) => c.position === majority);
    const disCells = cells.filter((c) => c.position !== majority);
    majCells.sort((a, b) => (b.confidence || 0) - (a.confidence || 0));
    disCells.sort((a, b) => (b.confidence || 0) - (a.confidence || 0));
    const bestMaj = majCells[0];
    const bestDis = disCells[0];

    section.innerHTML = `
      <!-- Verdict-colored stripe on the left edge -->
      <div class="absolute top-0 left-0 w-1 h-full" style="background:${vColor}"></div>

      <!-- Prominent header band: big FINDING-N chip + verdict pill -->
      <div class="flex items-center justify-between gap-4 px-5 pt-4 pb-3 border-b border-outline-variant/40">
        <div class="flex items-center gap-3 min-w-0">
          <span class="font-label-caps text-[13px] tracking-widest px-2.5 py-1 border-2 shrink-0"
                style="color:${vColor};border-color:${vColor};background:${vColor}1A">
            FINDING ${tribIdx + 1}
          </span>
          <span class="font-code-sm text-[11px] text-on-surface-variant opacity-70 truncate"
                title="${escapeHtml(PRINCIPLE_SUBTITLES[dp?.principle] || "")}">
            ${escapeHtml(principleName)} (${dp?.principle ?? "?"})
          </span>
        </div>
        <span class="font-code-md font-bold verdict-${vKey} verdict-bg-${vKey} px-3 py-1 border shrink-0">${escapeHtml(v)}</span>
      </div>

      <div class="px-5 py-4">
        <!-- Subject as the headline of the card -->
        <div class="font-headline-sm text-headline-sm text-on-surface leading-tight mb-4">
          ${escapeHtml(dp?.subject || trib.decision_point_id)}
        </div>

        ${
          judge.panel_skipped || cells.length === 0
            ? `<!-- Fast-tracked: panel skipped because Layer 1 was extreme -->
        <div class="flex items-center gap-2 mb-3">
          <span class="material-symbols-outlined text-[14px] text-primary">bolt</span>
          <span class="font-label-caps text-[10px] text-primary tracking-widest">FAST-TRACKED</span>
          <span class="text-[11px] text-on-surface-variant opacity-70">no panel needed — Layer 1 evidence was clear-cut</span>
          ${judge.override ? '<span class="text-[10px] text-yellow-400 font-bold ml-2">· JUDGE OVERRODE</span>' : ""}
        </div>`
            : `<!-- Vote split strip -->
        <div class="flex items-center gap-3 mb-3">
          <div class="flex gap-1">${dots}</div>
          <span class="text-[11px]"><span style="color:${majorityColor}" class="font-bold">${majority === "debt" ? nDebt : nJust} ${majorityLabel}</span> <span class="text-on-surface-variant opacity-60">·</span> <span style="color:${dissentColor}">${majority === "debt" ? nJust : nDebt} dissent</span></span>
          <span class="text-[10px] text-on-surface-variant opacity-60">· avg ${avgConf}% confident</span>
          ${judge.override ? '<span class="text-[10px] text-yellow-400 font-bold">· JUDGE OVERRODE</span>' : ""}
        </div>`
        }

        <!-- Best arguments from each side (only when there are cells) -->
        ${
          bestMaj
            ? `<div class="text-[12px] text-on-surface leading-relaxed mb-2">
          <span class="font-label-caps text-[9px]" style="color:${majorityColor}">STRONGEST ${majorityLabel}</span>
          <span class="italic text-on-surface-variant ml-1">"${escapeHtml(bestMaj.key_argument || "")}"</span>
        </div>`
            : ""
        }
        ${
          bestDis
            ? `<div class="text-[12px] text-on-surface leading-relaxed mb-2">
          <span class="font-label-caps text-[9px]" style="color:${dissentColor}">STRONGEST DISSENT</span>
          <span class="italic text-on-surface-variant ml-1">"${escapeHtml(bestDis.key_argument || "")}"</span>
        </div>`
            : ""
        }

        <!-- Judge reasoning -->
        <div class="mt-3 pt-3 border-t border-outline-variant/30 text-[12px] text-on-surface leading-relaxed">
          <div class="font-label-caps text-[9px] text-primary mb-1">JUDGE</div>
          <div class="whitespace-pre-line">${escapeHtml(judge.reasoning || "(no reasoning)")}</div>
        </div>
      </div>
    `;
    root.appendChild(section);

    // ---- Aggregate line (re-projected under current weights) ----
    const aggLine = document.createElement("div");
    aggLine.className =
      "mb-2 text-[11px] font-code-sm text-on-surface-variant px-4 pb-2";
    aggLine.dataset.aggregateFor = trib.decision_point_id;
    section.appendChild(aggLine);

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
  simplifier: { name: "Simplicity", value: "simplicity", color: "#a78bfa" },
  shipper: { name: "Velocity", value: "velocity", color: "#fb923c" },
  maintainer: {
    name: "Maintainability",
    value: "maintainability",
    color: "#5dd6ff",
  },
  verifier: { name: "Correctness", value: "correctness", color: "#4ade80" },
  scaler: { name: "Scalability", value: "scalability", color: "#f472b6" },
  adapter: { name: "Flexibility", value: "flexibility", color: "#facc15" },
};

// Per-persona hover blurbs for the AI Jury header avatars. Kept in sync
// with personas.yaml on the backend — each persona cares about exactly one
// value and is indifferent to the other five.
const JURY_DEBATERS = [
  { id: "simplifier", code: "Si", name: "Simplicity", color: "#a78bfa",
    blurb: "Cares about less indirection and fewer abstractions. " +
           "Dead code, one-implementation wrappers, and unused config knobs " +
           "anger them. Indifferent to shipping speed or flexibility." },
  { id: "shipper", code: "Ve", name: "Velocity", color: "#fb923c",
    blurb: "Cares about PR cycle time and how fast a contributor can land " +
           "a change. Refactors with high cost and unclear payoff anger them. " +
           "Indifferent to long-term maintainability." },
  { id: "maintainer", code: "Ma", name: "Maintainability", color: "#5dd6ff",
    blurb: "Cares about ramp cost for the next contributor and the blast " +
           "radius of any single change. Cleverness that requires tribal " +
           "knowledge angers them. Indifferent to raw shipping speed." },
  { id: "verifier", code: "Co", name: "Correctness", color: "#4ade80",
    blurb: "Cares about exhaustive coverage and defensive validation at " +
           "boundaries. Cyclomatic complexity in boundary code angers them. " +
           "Indifferent to whether the code is the simplest possible." },
  { id: "scaler", code: "Sc", name: "Scalability", color: "#f472b6",
    blurb: "Cares about surviving 10× growth in load, team, and surface " +
           "area. Coupling that prevents independent deployment angers them. " +
           "Indifferent to minimalism or short-term shipping cost." },
  { id: "adapter", code: "Fl", name: "Flexibility", color: "#facc15",
    blurb: "Cares about modules that can be lifted out cleanly when " +
           "requirements change. High efferent coupling and stable-on-" +
           "unstable dependencies anger them. Indifferent to raw simplicity." },
];

function renderJuryDebaters() {
  const root = document.getElementById("jury-debaters");
  if (!root) return;
  // Fixed-width grid → constant column width regardless of label length,
  // and constant inter-column gap. All 6 personas are statically lit here;
  // the dynamic "highlight the 2 currently debating" behavior happens in
  // the live audit modal's renderPersonasRow, where it tracks cell_start.
  root.innerHTML = "";
  root.className = "mt-5 grid grid-cols-6 gap-3 max-w-2xl";
  const n = JURY_DEBATERS.length;
  for (let i = 0; i < n; i++) {
    const d = JURY_DEBATERS[i];
    // Anchor the popover so it stays inside the section's overflow-hidden
    // bounds: first cell → anchor left (extends right), last cell → anchor
    // right (extends left), middle cells → centered.
    const anchor =
      i === 0 ? "left-0" :
      i === n - 1 ? "right-0" :
      "left-1/2 -translate-x-1/2";
    const cell = document.createElement("div");
    cell.className =
      "group relative flex flex-col items-center gap-1.5 cursor-help";
    cell.innerHTML = `
      <div class="flex items-center justify-center
                  font-code-md font-bold text-[13px] border-2 transition-transform
                  group-hover:scale-110"
           style="width:44px;height:44px;border-radius:9999px;
                  background:${d.color};border-color:${d.color};
                  color:#0a0a0b;box-shadow:0 0 10px ${d.color}55">
        ${d.code}
      </div>
      <div class="font-label-caps text-[9px] tracking-wider text-center leading-tight"
           style="color:${d.color}">
        ${d.name}
      </div>

      <!-- Hover popover. left-/right- anchor adapts to position so the
           leftmost and rightmost cards don't get clipped by section overflow. -->
      <div class="absolute top-full mt-2 ${anchor} w-64 z-30
                  bg-surface-container-high border border-outline-variant
                  p-3 shadow-2xl
                  opacity-0 group-hover:opacity-100
                  pointer-events-none transition-opacity duration-150">
        <div class="flex items-center gap-2 mb-2">
          <div class="flex items-center justify-center
                      font-code-md font-bold text-[10px]"
               style="width:24px;height:24px;border-radius:9999px;
                      background:${d.color};color:#0a0a0b">${d.code}</div>
          <div class="font-label-caps text-[10px] tracking-widest"
               style="color:${d.color}">
            ${d.name.toUpperCase()}
          </div>
        </div>
        <div class="text-[11px] text-on-surface-variant leading-snug">
          ${d.blurb}
        </div>
      </div>
    `;
    root.appendChild(cell);
  }
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
      proj.winner === "debt"
        ? "would lean PROBLEM"
        : proj.winner === "justified"
          ? "would lean FINE"
          : "—";
    line.innerHTML = `
      <div class="bg-surface-container-low border border-outline-variant/40 p-3 rounded">
        <div class="mb-1">
          <span class="text-on-surface-variant">What the ${cells.length} pairs decided:</span>
          <b class="text-[#fb923c]">${dCount} said PROBLEM</b>
          ·
          <b class="text-[#4ade80]">${jCount} said FINE</b>
        </div>
        <div>
          <span class="text-on-surface-variant">If we re-counted using your priority sliders:</span>
          <b style="color:${proj.winner === "debt" ? "#fb923c" : "#4ade80"}">${projShort}</b>
          ${
            wouldFlip
              ? `<span class="ml-2 text-yellow-400 font-bold">— the majority side would flip</span>`
              : `<span class="ml-2 opacity-60">— same majority as actual (the judge's verdict label never changes either way)</span>`
          }
        </div>
      </div>
    `;
  });
}

// =====================================================================
// Briefing view
// =====================================================================

function renderBriefing() {
  renderBriefingBody();

  const stamp = new Date().toISOString().replace("T", " ").slice(0, 19);
  document.getElementById("brief-report-id").textContent =
    `REPORT_ID: ${(state.activeSlug || "").toUpperCase()}-${state.evidence.commit_sha?.slice(0, 6) || "—"}`;
  document.getElementById("brief-words").textContent =
    `${state.reportMd ? state.reportMd.trim().split(/\s+/).length : 0} WORDS`;
  document.getElementById("brief-stamp").textContent = stamp;
  // Total audit wall-clock from metrics.json (written by cli.py once the
  // full pipeline finishes). Format as "Nm SSs" or "SSs" if <1 min.
  const durEl = document.getElementById("brief-duration");
  if (durEl) {
    const dur = state.metrics?.audit_duration_s;
    if (typeof dur === "number" && dur > 0) {
      const m = Math.floor(dur / 60);
      const s = Math.round(dur % 60);
      durEl.textContent = `RAN IN ${m > 0 ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`}`;
    } else {
      durEl.textContent = "";
    }
  }
  document.getElementById("brief-watermark-stamp").textContent =
    `GEN_STAMP: ${stamp}`;
  document.getElementById("brief-watermark-sig").textContent =
    `SIGNATURE: ${(state.evidence.commit_sha || "00000000").slice(0, 8)}…`;
}

// Lightweight DOM-level enhancer: walks every paragraph in the rendered
// briefing and wraps a few scanability signals — file paths with line
// ranges, finding/cell references, metric assertions, and standalone
// numbers — so the eye lands on the load-bearing parts of each
// paragraph without reading every word.
//
// We operate on actual TEXT NODES so we never touch text inside <code>,
// <a>, or <strong> children. That avoids accidentally re-tagging spans
// the markdown renderer already styled.
function enhanceBriefingHTML(root) {
  const PATTERNS = [
    // Path with optional line ref: fastapi/_compat/__init__.py:1-40
    {
      re: /\b([\w-]+(?:\/[\w.-]+)+\.(?:py|c|h|hpp|cpp|js|ts|tsx|jsx|go|rs|json|yaml|md))(?::(\d+(?:-\d+)?))?\b/g,
      wrap: (m, path, lines) =>
        `<span class="brief-path">${path}${lines ? `:${lines}` : ""}</span>`,
    },
    // Finding / cell references — "Finding #3", "#3", "cell 7"
    {
      re: /\b(Findings?\s+#?\d+|#\d+|cells?\s+\d+)\b/g,
      wrap: (m) => `<span class="brief-ref">${m}</span>`,
    },
    // Metric assertion — "blast_radius=1.0", "LCOM > 0.7", "CC > 15"
    {
      re: /\b([a-z][a-z_]{2,})\s*([=≥≤<>])\s*([\d.]+)\b/g,
      wrap: (m, k, op, v) =>
        `<span class="brief-metric">${k}${op}${v}</span>`,
    },
    // Plain hyphenated counts like "18-module", "50-LOC", "2-turn"
    {
      re: /\b(\d+(?:[.,]\d+)?)-([a-z]+)\b/g,
      wrap: (m, num, word) =>
        `<span class="brief-num">${num}-${word}</span>`,
    },
  ];

  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      // Skip text inside elements where re-styling would corrupt formatting
      // or duplicate styling (code blocks, links, existing chips).
      const skip = new Set([
        "CODE", "PRE", "A", "STRONG", "SPAN",
      ]);
      for (let p = node.parentElement; p; p = p.parentElement) {
        if (skip.has(p.tagName)) return NodeFilter.FILTER_REJECT;
      }
      return node.nodeValue.trim().length
        ? NodeFilter.FILTER_ACCEPT
        : NodeFilter.FILTER_REJECT;
    },
  });
  const targets = [];
  while (walker.nextNode()) targets.push(walker.currentNode);

  for (const node of targets) {
    let html = node.nodeValue
      // Escape HTML entities first since we're emitting raw HTML strings.
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
    for (const { re, wrap } of PATTERNS) {
      html = html.replace(re, wrap);
    }
    if (html !== node.nodeValue) {
      const span = document.createElement("span");
      span.innerHTML = html;
      node.parentNode.replaceChild(span, node);
    }
  }

  // Mark the very first paragraph as the "lead" so CSS can give it
  // typographic prominence — this is the "if you only read one paragraph"
  // line that the new Opus prompt is told to write.
  const firstP = root.querySelector(":scope > p");
  if (firstP) firstP.classList.add("brief-lead");
}

function renderBriefingBody() {
  if (!window.marked) return;
  marked.setOptions({ breaks: false, gfm: true });
  // Strip the values-tone framing the Opus report writer appends to the H1
  // title ("— A Velocity and Simplicity Briefing", "— A Correctness
  // Briefing", etc.). These were generated when the UI exposed a tone
  // selector; that selector is gone, so the title shouldn't claim a tone.
  let md = (state.reportMd || "_(no Layer-3 briefing on disk)_");
  md = md.replace(
    /^(#\s+.+?)\s*[—–-]\s*A\s+[A-Z][A-Za-z]+(?:\s+and\s+[A-Z][A-Za-z]+)?\s+Briefing\s*$/m,
    "$1",
  );
  let html = marked.parse(md);
  // Wrap literal verdict labels in colored chips. The verdict label is
  // a captured regex group restricted to /[A-Z][A-Z ]+[A-Z]/, so no
  // injection surface here.
  html = html.replace(
    /<strong>\s*Verdict:\s*([A-Z][A-Z ]+[A-Z])\s*<\/strong>/g,
    (_, v) => {
      const plain = VERDICT_PLAIN[v] || "";
      const cls = v.replace(/ /g, "-");
      return `<span class="verdict-tag verdict-${cls} verdict-bg-${cls}" title="${escapeHtml(plain)}">${escapeHtml(v)}</span>`;
    },
  );
  // Sanitize before injecting: report.md is LLM-generated and could
  // theoretically contain raw <script> or <img onerror>. Prefer DOMPurify
  // if it's loaded; otherwise fall back to a conservative tag stripper.
  if (window.DOMPurify) {
    html = window.DOMPurify.sanitize(html, { USE_PROFILES: { html: true } });
  } else {
    // Last-resort: strip <script>/<iframe>/<object>/<embed> and inline
    // event handlers. Not as thorough as DOMPurify but blocks the obvious.
    html = html
      .replace(/<\/?(script|iframe|object|embed|link|meta)\b[^>]*>/gi, "")
      .replace(/\son\w+\s*=\s*"[^"]*"/gi, "")
      .replace(/\son\w+\s*=\s*'[^']*'/gi, "")
      .replace(/\son\w+\s*=\s*[^\s>]+/gi, "");
  }
  const root = document.getElementById("brief-markdown");
  root.innerHTML = html;
  enhanceBriefingHTML(root);
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
    // Top-bar RE-RUN button only makes sense when a backend can re-run.
    const rerun = document.getElementById("btn-rerun");
    if (rerun) {
      rerun.disabled = false;
      rerun.classList.remove("opacity-50", "cursor-not-allowed");
      rerun.title =
        "Re-run the active audit (click twice to confirm — overwrites the same slug).";
    }
    // Landing CTA hint switches from "configure backend" to "submit any repo".
    const hint = document.getElementById("btn-landing-new-audit-hint");
    if (hint) hint.textContent = "Submit any public git repo.";
  } catch {
    // No backend reachable. Tell landing visitors how to enable live mode.
    const hint = document.getElementById("btn-landing-new-audit-hint");
    if (hint) {
      hint.innerHTML =
        'Static demo mode — <span class="text-primary">configure a live backend</span> to submit audits.';
    }
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
  const liveDp = document.getElementById("audit-live-dp");
  const liveSidePill = document.getElementById("audit-live-side-pill");
  const cellsStrip = document.getElementById("audit-cells-strip");
  const personasRow = document.getElementById("audit-personas-row");
  const tribunalsWrap = document.getElementById("audit-tribunals-wrap");
  const tribunalsList = document.getElementById("audit-tribunals-list");
  const tribunalsCount = document.getElementById("audit-tribunals-count");
  const statusDot = document.getElementById("audit-status-dot");
  const statusLabel = document.getElementById("audit-status-label");
  const elapsedEl = document.getElementById("audit-elapsed");
  if (!openBtn || !modal) return;

  // Elapsed-time ticker. setInterval ID lives here so reset()/done can clear it.
  let elapsedTimer = null;
  let elapsedStart = 0;
  const fmtElapsed = (s) => {
    const m = Math.floor(s / 60);
    const r = Math.floor(s % 60);
    return m > 0 ? `${m}m ${String(r).padStart(2, "0")}s` : `${r}s`;
  };
  const startElapsed = () => {
    if (elapsedTimer) clearInterval(elapsedTimer);
    elapsedStart = performance.now();
    if (elapsedEl) elapsedEl.textContent = "0s";
    elapsedTimer = setInterval(() => {
      if (!elapsedEl) return;
      const secs = (performance.now() - elapsedStart) / 1000;
      elapsedEl.textContent = fmtElapsed(secs);
    }, 1000);
  };
  const stopElapsed = () => {
    if (elapsedTimer) clearInterval(elapsedTimer);
    elapsedTimer = null;
    if (!elapsedEl || !elapsedStart) return;
    const finalSecs = (performance.now() - elapsedStart) / 1000;
    elapsedEl.textContent = `total ${fmtElapsed(finalSecs)}`;
  };

  // Per-audit live state — reset on each open.
  //   tribunals: dpId → { subject, principle, status, cellsVoted, cellsFailed }
  //   activeCell: { red, blue } for the currently-streaming cell (drives the
  //     6-persona row highlight)
  //   userPinnedDp: if the user clicks a tribunal chip, pin focus there
  const live = {
    activeDp: null,
    cells: new Map(),
    tribunals: new Map(),
    activeCell: { red: null, blue: null },
    userPinnedDp: null,
  };

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
    liveDp.textContent = "";
    cellsStrip.innerHTML = "";
    if (elapsedEl) elapsedEl.textContent = "";
    if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; }
    if (personasRow) personasRow.innerHTML = "";
    if (tribunalsList) tribunalsList.innerHTML = "";
    if (tribunalsWrap) tribunalsWrap.classList.add("hidden");
    if (tribunalsCount) tribunalsCount.textContent = "";
    statusDot.className = "w-2 h-2 rounded-full bg-emerald-500 animate-pulse";
    statusLabel.textContent = "RUNNING";
    live.activeDp = null;
    live.cells.clear();
    live.tribunals.clear();
    live.activeCell = { red: null, blue: null };
    live.userPinnedDp = null;
  };

  openBtn.addEventListener("click", () => {
    // If an audit is already running, just re-open the modal — preserve the
    // live progress view instead of wiping it back to the empty form.
    // currentRunningSlug clears in the SSE `done` handler below, so this
    // condition only holds while the backend is actively streaming.
    if (currentRunningSlug) {
      modal.classList.remove("hidden");
      return;
    }
    // Open the modal first, then reset. If reset() throws on a stale/
    // missing element, the modal still appears so the user sees *something*
    // happen and we get a console error pinpointing the bad reference.
    modal.classList.remove("hidden");
    try {
      reset();
    } catch (e) {
      console.error("audit-modal reset failed:", e);
    }
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
      // Paint the 6 personas immediately (all dimmed, no active pair yet) so
      // users understand the 6-agent system from the moment the panel opens.
      renderPersonasRow();
    }
  };

  // The dpId we render details for: the user's pinned choice if they clicked
  // a tribunal chip, otherwise the most-recently-active one.
  const focusedDp = () => live.userPinnedDp || live.activeDp;

  // Render the row of in-flight tribunals at the top of the live panel.
  // Each chip = one finding being audited concurrently; clicking switches
  // the cells strip + persona row + streaming text to that tribunal.
  const renderTribunalsList = () => {
    if (!tribunalsList) return;
    const tribs = [...live.tribunals.values()];
    if (tribs.length === 0) {
      tribunalsWrap?.classList.add("hidden");
      return;
    }
    tribunalsWrap?.classList.remove("hidden");

    const inFlight = tribs.filter((t) => t.status !== "complete").length;
    if (tribunalsCount) {
      tribunalsCount.textContent =
        `${inFlight} running · ${tribs.length - inFlight} done`;
    }

    tribunalsList.innerHTML = "";
    const focus = focusedDp();
    tribs
      .sort((a, b) => (a.idx ?? 0) - (b.idx ?? 0))
      .forEach((t) => {
        const isFocus = t.dpId === focus;
        const dotColor =
          t.status === "complete"
            ? "#4ade80"
            : t.status === "active"
              ? "#facc15"
              : "#919094";
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = [
          "flex items-center gap-2 px-2 py-1 border font-code-sm text-[10px]",
          "max-w-[200px] min-w-0 transition-colors",
          isFocus
            ? "bg-primary/20 border-primary text-on-surface"
            : "bg-surface-container border-outline-variant text-on-surface-variant hover:bg-surface-container-high",
        ].join(" ");
        chip.title =
          `${t.principle || ""} · ${t.subject || t.dpId}\n` +
          `${t.cellsVoted}/${t.cellsTotal} cells voted` +
          (t.cellsFailed ? ` · ${t.cellsFailed} failed` : "");
        const dot = document.createElement("span");
        dot.className = "w-1.5 h-1.5 rounded-full" + (
          t.status === "active" ? " animate-pulse" : ""
        );
        dot.style.background = dotColor;
        const label = document.createElement("span");
        label.className = "truncate min-w-0 flex-1";
        label.textContent =
          `${t.principle || "?"} · ${(t.subject || t.dpId).slice(0, 28)}`;
        const prog = document.createElement("span");
        prog.className = "opacity-70 ml-1";
        prog.textContent = `${t.cellsVoted}/${t.cellsTotal}`;
        chip.append(dot, label, prog);
        chip.addEventListener("click", () => {
          live.userPinnedDp = t.dpId;
          renderTribunalsList();
          renderPersonasRow();
          renderCellsStrip(t.dpId);
        });
        tribunalsList.appendChild(chip);
      });
  };

  const PERSONA_ORDER = [
    "simplifier", "shipper", "maintainer",
    "verifier", "scaler", "adapter",
  ];
  // Subset of PERSONA_INFO duplicated here so the live modal works even if
  // app.js's main render path hasn't run yet (the modal opens before any
  // verdicts.json fetch). Keep in sync with the global PERSONA_INFO.
  const PERSONA_PALETTE = {
    simplifier: { name: "Simplicity",    color: "#a78bfa" },
    shipper:    { name: "Velocity",      color: "#fb923c" },
    maintainer: { name: "Maintainability", color: "#5dd6ff" },
    verifier:   { name: "Correctness",   color: "#4ade80" },
    scaler:     { name: "Scalability",   color: "#f472b6" },
    adapter:    { name: "Flexibility",   color: "#facc15" },
  };

  // Render the 6-persona row. The 2 personas paired in the currently-streaming
  // cell glow with a colored ring + animate-pulse; the others are dimmed.
  const renderPersonasRow = () => {
    if (!personasRow) return;
    personasRow.innerHTML = "";
    const { red, blue } = live.activeCell;
    for (const id of PERSONA_ORDER) {
      const info = PERSONA_PALETTE[id];
      const isActive = id === red || id === blue;
      const role =
        id === red ? "RED" : id === blue ? "BLUE" : null;

      const wrap = document.createElement("div");
      wrap.className = "flex flex-col items-center gap-1";
      wrap.title = `${info.name}${role ? ` · ${role}` : ""}`;

      const circle = document.createElement("div");
      circle.className =
        "flex items-center justify-center " +
        "font-code-sm text-[11px] font-bold border-2 transition-all";
      // Inline border-radius because the site's Tailwind config aliases
      // `rounded-full` to 12px (not 50%) — using a literal pixel value
      // keeps these as actual circles.
      circle.style.width = "36px";
      circle.style.height = "36px";
      circle.style.borderRadius = "9999px";
      if (isActive) {
        circle.style.background = info.color;
        circle.style.borderColor = info.color;
        circle.style.boxShadow = `0 0 12px ${info.color}80`;
        circle.style.color = "#0a0a0b";
        circle.classList.add("animate-pulse");
      } else {
        circle.style.background = "transparent";
        circle.style.borderColor = info.color + "40";
        circle.style.color = info.color + "60";
      }
      circle.textContent = info.name.charAt(0);

      const label = document.createElement("div");
      label.className = "font-label-caps text-[8px] tracking-wider";
      label.style.color = isActive ? info.color : info.color + "60";
      label.textContent = info.name;

      wrap.append(circle, label);
      personasRow.appendChild(wrap);
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

  // Bump a tribunal's tally and re-render the chips row. No-op if the
  // tribunal isn't yet registered (e.g., cell events arrive before
  // tribunal_start because they emit from a different async path).
  const bumpTribunal = (dpId, field) => {
    let t = live.tribunals.get(dpId);
    if (!t) {
      t = {
        dpId, idx: null, subject: null, principle: null,
        status: "active", cellsVoted: 0, cellsFailed: 0, cellsTotal: 0,
      };
      live.tribunals.set(dpId, t);
    }
    t[field] = (t[field] || 0) + 1;
    renderTribunalsList();
  };

  const handleEvent = (ev) => {
    // ---- Tribunal-level events: track concurrent in-flight findings ----
    if (ev.t === "tribunal_start") {
      const dpId = ev.dp_id;
      if (dpId) {
        const existing = live.tribunals.get(dpId) || {
          dpId,
          idx: ev.trib_idx,
          status: "active",
          cellsVoted: 0,
          cellsFailed: 0,
          cellsTotal: 0,
        };
        existing.idx = ev.trib_idx ?? existing.idx;
        existing.subject = ev.subject || existing.subject;
        existing.principle = ev.principle || existing.principle;
        existing.status = "active";
        live.tribunals.set(dpId, existing);
        ensureLivePanel();
        renderTribunalsList();
      }
    }

    // ---- Live-publish refresh (preserved) + mark tribunal complete ----
    if (
      ev.t === "tribunal_complete" ||
      ev.t === "layer1_done" ||
      ev.t === "layer3_done"
    ) {
      if (ev.t === "tribunal_complete" && ev.dp_id) {
        const t = live.tribunals.get(ev.dp_id);
        if (t) {
          t.status = "complete";
          renderTribunalsList();
        }
      }
      const slug = currentRunningSlug;
      if (slug) {
        liveRefreshAudit(slug);
      }
      return;
    }

    const cell = ev.cell || {};
    const turn = ev.turn || {};
    const dpId = cell.dp_id;
    const cellId = cell.cell_id;

    if (!dpId || cellId == null) {
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
        live.activeCell = { red: cell.red, blue: cell.blue };
        liveDp.textContent = `${cell.principle || ""} · ${dpId}`;
        ensureLivePanel();
        bumpTribunal(dpId, "cellsTotal");
        renderPersonasRow();
        // Only refocus the cells strip onto this dp if the user hasn't
        // pinned a different one via the tribunal chips.
        if (!live.userPinnedDp || live.userPinnedDp === dpId) {
          renderCellsStrip(dpId);
        }
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
        // Re-anchor the personas row to whichever cell is producing the
        // tokens we're showing right now. Without this, the row sticks on
        // whichever cell most recently fired cell_start — which, with cells
        // running concurrently, is rarely the cell whose text is in front
        // of the user. cell.red/cell.blue come from the per-cell context
        // the backend stamps on every emitted event.
        if (cell.red && cell.blue) {
          live.activeCell = { red: cell.red, blue: cell.blue };
          renderPersonasRow();
        }
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
        bumpTribunal(dpId, "cellsVoted");
        if (!live.userPinnedDp || live.userPinnedDp === dpId) {
          renderCellsStrip(dpId);
        }
        break;

      case "cell_failed":
        entry.status = "failed";
        bumpTribunal(dpId, "cellsFailed");
        if (!live.userPinnedDp || live.userPinnedDp === dpId) {
          renderCellsStrip(dpId);
        }
        break;
    }
  };

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const isRerun = form.dataset.rerun === "yes";
    // Clear the marker immediately so any subsequent manual submit defaults
    // back to "new audit, don't overwrite".
    form.dataset.rerun = "";
    const body = {
      repo_url: fd.get("repo_url"),
      slug: fd.get("slug")?.trim() || undefined,
      language: fd.get("language") || "auto",
      overwrite: isRerun,
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

    // Track the slug the live-audit is writing to so handleEvent() knows
    // whose docs/data/<slug>/ to re-fetch when partial events fire.
    currentRunningSlug = slug;

    // Pre-add the slug to the switcher (as a placeholder) and switch the
    // main views to it RIGHT NOW. The user can watch Evidence, Jury, and
    // Briefing populate live as each phase completes — much more dramatic
    // than staring at the modal log.
    if (!state.manifest.audits.find((a) => a.slug === slug)) {
      state.manifest.audits.push({
        slug,
        label: slug,
        version: "live",
        language: "python",
        source: "(in-progress audit)",
        commit: "live",
        note: "Audit running — phases appear as they complete.",
      });
      renderAuditSwitcher();
    }
    state.activeSlug = slug;
    markAuditActive(slug);

    form.classList.add("hidden");
    logWrap.classList.remove("hidden");
    startElapsed();

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
      stopElapsed();
      const ok = info.status === "completed";
      statusDot.className = `w-2 h-2 rounded-full ${ok ? "bg-emerald-500" : "bg-red-500"}`;
      statusLabel.textContent = ok
        ? `COMPLETED — switching to "${slug}"`
        : `FAILED${info.error ? ": " + info.error : ""}`;
      // Clear the running-slug so subsequent clicks of "New Audit" open a
      // fresh form instead of landing back on this finished run's progress.
      currentRunningSlug = null;
      // Clear the active-cell highlight so the personas row stops glowing
      // on the last-fired cell's pair (which would otherwise leave Si+Co
      // — cell 14 — perpetually lit after every audit).
      live.activeCell = { red: null, blue: null };
      renderPersonasRow();
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
      stopElapsed();
      // Treat a dropped SSE the same as a finished run — let the user
      // start a fresh audit instead of trapping them in the dead modal.
      currentRunningSlug = null;
      live.activeCell = { red: null, blue: null };
      renderPersonasRow();
    };
  });
}

document.addEventListener("DOMContentLoaded", init);
