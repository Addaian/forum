# Forum — Product Requirements Document v1.3

> An AI architectural audit system that walks a codebase, extracts structural decisions deterministically, submits each one to a jury of AI experts that deliberate in parallel, and produces a written architectural briefing — calibrated to your team's stated engineering values.

**Version:** 1.3 (Hackathon, CLI-first)
**Tracks targeted (in priority order):** Tensormesh (Best Acceleration) · Most Uncommon · Wafer (Best Inference)
**Build window:** 48 hours
**Team:** 3 engineers (full-stack, agents, ML/inference)

**What's new in v1.3:** CLI-first architecture locked in. The product is a command-line tool (`Forum audit <repo>`) that produces a markdown briefing. The optional web wrapper is demo theater on top, not core product. Values are set via CLI flag (`--value velocity=1.8`) or config file. What-if probe is a CLI command (`Forum whatif`), not a slider.

---

## 1. Problem

Every codebase contains thousands of structural decisions — where module boundaries lie, what depends on what, how layers compose, how data flows. These decisions are made over years by many engineers, and the original rationale degrades silently as context drifts around them.

Existing AI dev tools review **changes**. CodeRabbit, Greptile, Cursor BugBot, Anthropic's Code Review (March 2026) — all wait for you to write code, then comment on the diff. The implicit decisions in code that *already exists* — the choices nobody is currently debating because nobody opened a PR about them — are invisible to every tool on the market.

But those are the decisions that matter most. Codebases rot not because PRs are bad, but because **once-good decisions become bad decisions silently** as scale, team, and surrounding code change. A staff engineer can identify these issues in two weeks of digging. Most teams don't have those two weeks.

**And one more wrinkle:** different teams have legitimately different values. A 4-engineer seed-stage startup should not be audited like a 200-engineer bank. The same dependency cycle is "fine, ship it" at one and "critical refactor" at the other. A one-size-fits-all architectural audit is wrong in a deep way — it assumes a universal trade-off curve when teams sit at different points on it.

## 2. Solution

Forum is a **CLI tool** built as a three-layer pipeline with a **values lens** applied at the entrances and exits, but never inside the jury:

1. **Evidence Gathering (deterministic).** Walks the codebase, extracts the dependency graph, computes structural metrics, runs principle-checking algorithms to identify decision points worth examining. No LLMs in this layer.
2. **Prioritization (deterministic, values-aware).** Scores extracted decision points using structural signals modulated by user-stated engineering values from a CLI flag or YAML config.
3. **Jury Deliberation (agentic, values-neutral).** For each prioritized decision point, spawns 10 parallel debate cells (Red prosecutor, Blue defender) that argue whether the decision is structural debt or justified. Confidence-weighted majority. A wide-context judge synthesizes the panel.
4. **Report Generation (single LLM, values-aware).** A long-context model writes a coherent markdown briefing document. User values shape framing, never verdicts.
5. **What-If Probe (post-audit, CLI command).** `Forum whatif <cache>` re-reads jury transcripts under alternate value vectors. No re-run; honest re-projection. Zero new inference cost.

The primary deliverable is a markdown file. Everything else is theater on top.

## 3. The CLI is the product

This is a hard commitment. The product is a CLI tool. The web UI, if built, is a demo wrapper that calls the CLI under the hood and renders its outputs with animation. The split is total:

- **Hours 0–40** are spent making the CLI work end-to-end with no UI dependency. Every layer is testable, debuggable, and demoable from the terminal.
- **Hours 40–46** (optional) add a thin Next.js wrapper that calls the CLI and renders the outputs nicely for the 90-second video.
- **If hours run short, the web wrapper is the first thing to cut.** The CLI still ships a working product. The terminal demo (Rich-powered TUI) is a legitimate fallback.

The CLI-first commitment changes how risk concentrates. The single point of failure is no longer "frontend renders correctly during demo" — it's "CLI produces correct artifact." Every team member can dogfood from their own terminal at every stage, which dramatically improves iteration speed.

## 4. CLI surface (target)

```bash
# Primary audit command
Forum audit <repo-url-or-path> [options]

# Options:
  --value <key>=<value>     # e.g., --value velocity=1.8 (repeatable)
  --values <yaml-file>      # alternative: load all weights from YAML
  --cache <path>            # persist audit artifacts (default: ./audits/<hash>.json)
  --output <path>           # markdown report destination (default: stdout + ./report.md)
  --top-n <int>             # number of decision points to surface (default: 5)
  --model-tier <fast|balanced|max>  # cost vs. quality knob (default: balanced)
  --verbose                 # stream Layer 1/2/3 progress to stderr

# Post-audit re-projection
Forum whatif <cache-path> [options]

# Options:
  --value <key>=<value>     # alternate value weights to project under
  --output <path>           # destination for re-framed report

# Utility
Forum graph <repo-url-or-path> --output graph.svg   # Layer 1 only
Forum benchmark <test-set-path>                      # F1 against ground-truth decisions
```

A YAML config for `--values`:

```yaml
# team.yaml
values:
  scalability:     1.5
  maintainability: 1.0
  velocity:        1.8
  correctness:     1.0
  simplicity:      1.2
  flexibility:     0.6
```

The CLI streams Layer 1/2/3 progress to stderr (so it's visible during the demo) and writes the markdown report to stdout and to disk. This is Unix-philosophy: composable, scriptable, no UI required.

## 5. The values-lens design principle

Three rules govern where user values may and may not influence the system:

1. **Values shape what we look at.** (Prioritization is values-aware.)
2. **Values do not shape what we conclude.** (The jury aggregates honestly.)
3. **Values shape how we communicate findings.** (Report framing is values-aware.)

This is the central design discipline. Violations of rule 2 turn Forum from "an architectural auditor that respects your priorities" into "a confirmation-bias generator." Code-review rule: `user_values` must never appear in Layer 2 prompts.

## 6. Primary user

A senior or staff engineer inheriting, auditing, or reviewing a codebase they don't fully know. Reaches for Forum the way they'd reach for `git log` — a CLI tool that produces a focused, useful artifact. Secondary users: engineering managers planning refactor work, open-source maintainers deciding what to deprecate, hackathon judges evaluating the project itself.

## 7. What Forum is NOT

- **Not a linter.** Linters check syntactic rules; Forum judges structure against principles requiring interpretation.
- **Not a PR review tool.** The unit of analysis is the whole codebase at rest, not a diff.
- **Not a refactoring tool.** It identifies and argues; it doesn't apply changes.
- **Not a code quality score.** It produces *reasoned verdicts*, not a single number.
- **Not a chatbot.** The output is a persistent markdown artifact, not a conversation.
- **Not a SaaS dashboard.** It's a CLI tool. The web demo is a wrapper, not the product.
- **Not a verdict dial.** User values shape prioritization and presentation. Verdicts are jury-honest.

## 8. Success criteria (hackathon)

1. `Forum audit https://github.com/...` on a public Python repo of >5K LOC produces a coherent markdown briefing in under 5 minutes wall-clock.
2. Every verdict cites a named principle from cited literature. An architecture-literate judge nods.
3. Demonstrably runs 50 parallel jury cells per audit with measured cache hit rate ≥ 50%.
4. Same repo + two different `--values` invocations produce visibly different reports but identical jury verdicts on overlapping decision points.
5. `Forum whatif` re-projects in <2 seconds with zero inference cost.
6. The demo runs reproducibly from cached artifacts even if venue WiFi dies.

---

## 9. The Stack (LOCKED)

**The bet:** Python end-to-end for the analysis pipeline. Anthropic-only for all LLM calls. CLI is the primary interface; web is optional demo theater.

### Language and runtimes

- **Python 3.12+** — all pipeline layers
- **TypeScript** — optional demo wrapper only

### Layer 1 — Evidence Gathering (deterministic)

| Concern | Tool | Why |
|---|---|---|
| Module dependency graph | **pydeps** | Best Python dep extractor; DOT/JSON output |
| AST walking | **tree-sitter** + Python `ast` stdlib | Tree-sitter is robust to syntax errors |
| Cyclomatic complexity | **radon** | Standard, fast |
| Dead code detection | **vulture** | Reachability from entry points |
| Git history mining | **pydriller** | Co-change matrix, author activity |
| Graph algorithms | **networkx** | Tarjan's SCC, centrality, dominators |
| Repo ingestion | `git clone --depth=1` subprocess | Shallow clones for speed |

### Layer 1.5 — Prioritization (deterministic, values-aware)

| Concern | Tool | Why |
|---|---|---|
| Scoring function | Custom Python (~50 LOC) | Composite of structural signals + value-affinity bonus |
| Value affinity tables | Hand-curated YAML config | Map principles/personas to value affinities |

### Layer 2 — Jury Deliberation (agentic, values-NEUTRAL)

| Concern | Tool | Why |
|---|---|---|
| Cell LLM (Red, Blue, vote) | **Anthropic Claude Haiku 4.5** | Cheapest tier with full prompt caching support |
| Judge LLM (per decision point) | **Anthropic Claude Sonnet 4.6** | Sweet-spot intelligence and cost for synthesis |
| Async orchestration | **`anthropic` Python SDK + `asyncio`** | Native async support; `asyncio.gather()` for fanout |
| Prompt caching | **`cache_control: ephemeral`** | 5-minute ephemeral cache; reads at 10% of input cost |
| Structured outputs | **Anthropic tool-use / JSON mode** | For vote schemas and judge verdicts |
| Speculative early-stopping | Custom `asyncio.CancelledError` logic | Cancel cells once 6/10 vote high-confidence |
| Confidence aggregation | Custom Python (weighted majority) | Per-cell `confidence ∈ [0,1]` × vote — **no user-value modulation** |

### Layer 3 — Report Generation (single LLM, values-aware)

| Concern | Tool | Why |
|---|---|---|
| Report LLM | **Anthropic Claude Opus 4.7** | Best long-context synthesis; one call per audit |
| Output format | Markdown with embedded SVG | Renders in any terminal viewer, GitHub, IDE preview |
| Values injection | System-prompt template with user-value vector | Shapes prose framing, headline, action ordering |

### Layer 5 — What-If Probe (post-audit, CLI command, zero inference)

| Concern | Tool | Why |
|---|---|---|
| Dissent re-projection | Pure Python over cached transcripts | No LLM calls; re-score cells under alternate weights |
| CLI command | `Forum whatif <cache>` | Same UX as `Forum audit`, instant turnaround |

### Backend

| Concern | Tool | Why |
|---|---|---|
| Process orchestration | In-process `asyncio` | No queue infrastructure, no daemon |
| Caching artifacts | Local JSON files in `./audits/` | Persist Layer 1/2/3 outputs for replay and `whatif` |
| Secrets | `.env` + `python-dotenv` | Standard |
| Terminal UI (progress) | **Rich** | Beautiful CLI progress, tables, syntax highlight |

### Demo wrapper (OPTIONAL — cut first if time runs short)

| Concern | Tool | Why |
|---|---|---|
| Web framework | **Next.js 14 (App Router)** | Fast scaffolding for the demo |
| Graph visualization | **react-flow** + **dagre** auto-layout | Best React-native graph library |
| Live updates | **Server-Sent Events** | Stream CLI stderr to browser |
| State management | **Zustand** | Lightweight |
| Styling | **Tailwind CSS** + **shadcn/ui** | Speed |
| Animation | **Framer Motion** | For jury reveal animations |
| Backend bridge | **FastAPI** wrapping the CLI | Web → FastAPI → subprocess → CLI |

The web wrapper does **not** drive analysis. It calls the CLI and renders its outputs. There is no slider in the wrapper — values are baked into the audit at CLI invocation time. The wrapper's job is animation, not interaction.

### Build tooling

| Concern | Tool | Why |
|---|---|---|
| Python package mgmt | **uv** | Fastest, modern |
| Node package mgmt | **pnpm** | Faster than npm |
| Type checking | **mypy** strict (Python), **TypeScript** strict | Catch bugs early when sleep-deprived |
| Linting | **ruff** (Python), **biome** (JS/TS) | One tool each, fast |

### Deployment

| Concern | Tool | Why |
|---|---|---|
| CLI distribution | `uv tool install` from a GitHub repo | One command for judges to install |
| Demo wrapper hosting | **Modal** or **Render** | Modal preferred if hackathon sponsor |
| Public demo URL | Vercel-deployed Next.js calling Modal-hosted FastAPI | If web wrapper ships |

### Anthropic-only commitment

Single-vendor across all three LLM layers. Reasons:

1. **Prompt caching is per-account.** Haiku, Sonnet, Opus all share the cache infrastructure. Cross-tier cache reuse compounds the Tensormesh-track story.
2. **One SDK, one error model.** Sleep-deprived debugging at hour 38 is not the time to context-switch between APIs.
3. **One billing surface.** Easier to track the "$0.50 per audit" claim.
4. **Model cascade is clean.** Haiku → Sonnet → Opus is a clean cost/intelligence ladder within one provider.

---

## 10. The Pipeline (end-to-end)

```
CLI INVOCATION:
  $ Forum audit <repo> --value velocity=1.8 --value simplicity=1.5
        │
        ▼
┌─────────────────────────────────────────────────────────────────────┐
│ LAYER 1: EVIDENCE GATHERING            (~30s, $0, values-neutral)   │
│                                                                      │
│  1.1  git clone --depth=1 → /tmp/repo                                │
│  1.2  AST walk all .py files (tree-sitter)                           │
│  1.3  Build module dep graph (pydeps → networkx)                     │
│  1.4  Compute metrics (Ca, Ce, instability, McCabe, LCOM, reach.)    │
│  1.5  Mine git history (pydriller co-change, author activity)        │
│  1.6  Layer assignment (entry-point distance heuristic)              │
│  1.7  Run 7 principle checkers:                                      │
│         P1 ADP, P2 SDP, P3 McCabe, P4 LCOM,                          │
│         P5 Reachability, P6 Layering, P7 CCP                         │
│                                                                      │
│  OUTPUT: ./audits/<hash>/evidence.json                               │
│          + graph.json (for optional visualization)                   │
└─────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────────┐
│ LAYER 1.5: PRIORITIZATION              (~2s, $0, VALUES-AWARE)      │
│                                                                      │
│  Read CLI --value flags or --values YAML.                            │
│                                                                      │
│  For each decision point dp:                                         │
│    structural_score = (                                              │
│        w1 * blast_radius(dp) +                                       │
│        w2 * recency(dp) +                                            │
│        w3 * principle_severity(dp) +                                 │
│        w4 * pattern_violation(dp) +                                  │
│        w5 * advocate_absence(dp)                                     │
│    )                                                                 │
│                                                                      │
│    value_affinity_score = Σ over values v:                           │
│        user_values[v] * principle_affinity(dp.principle, v)          │
│                                                                      │
│    composite_score = structural_score * (1 + 0.5 * value_affinity)   │
│                                                                      │
│  Take top --top-n by composite_score for jury (default: 5).          │
│                                                                      │
│  OUTPUT: ./audits/<hash>/prioritized.json                            │
└─────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────────┐
│ LAYER 2: JURY DELIBERATION             (~60-90s, $0.40, NEUTRAL)    │
│                                                                      │
│  Build shared global prefix (cached once per audit):                 │
│    - Codebase summary                                                │
│    - Dependency graph statistics                                     │
│    - Team conventions (CONTRIBUTING.md, README.md)                   │
│    - cache_control: ephemeral                                        │
│                                                                      │
│  *** USER VALUES ARE NOT IN THIS PROMPT. ***                         │
│                                                                      │
│  For each decision point (5 in parallel via asyncio.gather):         │
│    Spawn 10 cells in parallel:                                       │
│      Each cell:                                                      │
│        - Pick (Red, Blue) persona pairing from heterogeneous library │
│        - Set temperature ∈ [0.5, 0.9]                                │
│        - Run 4-turn debate (Haiku 4.5, ~600 tok per turn)            │
│        - Emit structured vote (JSON):                                │
│            { position: "debt" | "justified",                         │
│              confidence: 0.0-1.0,                                    │
│              key_argument: string,                                   │
│              evidence_cited: [string],                               │
│              value_lens: {scalability: 0.0-1.0, ...} }               │
│                                                                      │
│    Speculative early-stop:                                           │
│      Once 6/10 cells vote one direction with confidence ≥ 0.7,       │
│      cancel remaining cells (asyncio.Task.cancel()).                 │
│                                                                      │
│    Aggregate: confidence-weighted majority (values-neutral)          │
│                                                                      │
│    Judge call (Sonnet 4.6):                                          │
│      Output: { verdict, confidence, reasoning,                       │
│                recommended_action, dissent_summary,                  │
│                dissent_value_lenses }                                │
│                                                                      │
│  Progress streamed to stderr via Rich progress bars.                 │
│                                                                      │
│  OUTPUT: ./audits/<hash>/verdicts.json                               │
│          + full cell transcripts for whatif probe                    │
└─────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────────┐
│ LAYER 3: REPORT GENERATION             (~30s, $0.10, VALUES-AWARE)  │
│                                                                      │
│  Single Opus 4.7 call (long-context, max ~80k input):                │
│    Inputs: all 5 judge verdicts + full evidence + USER VALUE VECTOR  │
│                                                                      │
│    Prompt instructs Opus to:                                         │
│      - Open with verdict most aligned to user's top value            │
│      - Order action recommendations by value alignment               │
│      - Frame prose in vocabulary matching user's values              │
│      - PRESERVE every jury verdict unchanged                         │
│      - Surface dissents that match user values prominently           │
│                                                                      │
│  OUTPUT: ./report.md (THE ARTIFACT)                                  │
│          + write same to stdout                                      │
└─────────────────────────────────────────────────────────────────────┘
        │
        ▼
   CLI exits cleanly. User has report.md and ./audits/<hash>/ cache.

────────────────────────────────────────────────────────────────────

POST-AUDIT (separate invocation):
  $ Forum whatif ./audits/<hash> --value scalability=2.0

┌─────────────────────────────────────────────────────────────────────┐
│ LAYER 5: WHAT-IF PROBE                  (~1s, $0, ZERO INFERENCE)   │
│                                                                      │
│  Pure Python over cached transcripts:                                │
│    1. Re-read verdicts.json (no new inference)                       │
│    2. For each decision point, recompute under new weights:          │
│         - Which cells argued from scale-related grounds?             │
│         - Under user's new weights, which dissents are now salient?  │
│    3. Re-call Layer 3 with new value vector and existing verdicts    │
│       (this is the one place we DO call Opus again — to re-frame.    │
│       But: the JURY VERDICTS are not re-derived.)                    │
│                                                                      │
│  Cost: one Opus call (~$0.10). Latency: ~30s.                        │
│                                                                      │
│  Note: a stricter version skips Opus and just emits a text diff      │
│  showing which dissents become salient. That version is $0 / 1s.     │
│  We'll implement the cheap version first; promote to Opus re-framing │
│  if time allows.                                                     │
│                                                                      │
│  OUTPUT: ./report-whatif.md                                          │
└─────────────────────────────────────────────────────────────────────┘
```

### Cost & latency budget (target, 10K-LOC Python repo)

| Stage | Latency | Tokens | Cost |
|---|---|---|---|
| Layer 1 | 30s | 0 | $0.00 |
| Layer 1.5 | 2s | 0 | $0.00 |
| Layer 2 cells (50 Haiku calls, warm-cache) | 60s | ~200K (mostly cached at 10%) | ~$0.30 |
| Layer 2 judges (5 Sonnet calls) | 25s | ~50K | ~$0.10 |
| Layer 3 report (1 Opus call) | 30s | ~30K | ~$0.10 |
| Layer 5 whatif (cheap version) | 1s | 0 | $0.00 |
| Layer 5 whatif (Opus re-framing) | 30s | ~30K | ~$0.10 |
| **TOTAL per `Forum audit`** | **~3 min** | **~280K** | **~$0.50** |
| **`Forum whatif` (cheap)** | 1s | 0 | $0.00 |
| **`Forum whatif` (Opus)** | 30s | ~30K | $0.10 |

---

## 11. Pros & Cons — Honest Assessment (v1.3)

### Pros (CLI-first additions)

**The product is debuggable end-to-end from day one.** Every team member runs `Forum audit` on their own laptop. No "the frontend isn't connecting to the backend" debugging. The artifact is a markdown file. You can `cat report.md`. You can `diff` two reports. You can pipe it to anything.

**The demo has a credible fallback.** If the web wrapper breaks, you `Forum audit fastapi` on stage with Rich progress bars and it's still a great demo. Terminal-native is its own aesthetic.

**The CLI matches how this tool would actually be used.** Senior engineers don't open a SaaS dashboard to audit a codebase. They run a CLI command in CI or locally. Forum as CLI is closer to a real product shape, even at hackathon scale.

**The values lens is more honest as flags than as sliders.** A slider implies "drag this and watch the verdict change." A flag implies "set your team's parameters once, run the audit." The CLI framing reinforces the design principle that values shape *what* and *how*, not *what we conclude*.

**The whatif probe is more impressive as a separate CLI command than as a slider.** `$ Forum whatif <cache> --value scalability=2.0` taking 1 second produces a re-framed report from cached transcripts. The Wafer pitch becomes: "re-personalize the audit without re-running any inference. Zero new tokens."

**No frontend means more hours for the analysis layers.** The 6+ hours that would have gone to slider UI go to making Layer 2's jury verdicts actually substantive. That's where credibility is won or lost.

### Pros (carried from v1.2)

**Strong on multiple judging dimensions.** Tensormesh sees KV-cache reuse. Wafer sees a 50-agent reasoning system. Most Uncommon sees a courtroom-format AI jury.

**The deterministic layer is irreplaceable.** Tarjan's algorithm cannot be replicated by an LLM. McCabe complexity cannot be replicated by an LLM. Git co-change matrices cannot be computed from training data.

**The artifact is screenshot-worthy.** A markdown briefing written like a staff engineer wrote it, citing principles by name, with annotated graph and dissent summaries.

**Genuine acceleration story.** Senior engineers take two weeks. Forum takes 3 minutes. That's a defensible 1000× speedup.

**Principle citation = instant credibility.** Martin 2002, McCabe 1976, Chidamber & Kemerer 1994 make the project read as serious rather than performative.

**Personalization that doesn't lie.** The values lens adapts framing without pretending the codebase is different.

**The Tensormesh KV-cache story is essentially perfect.** 50 cells sharing a codebase prefix is one of the cleanest KV-reuse workloads.

**Stack is conservative and proven.** No exotic dependencies.

### Cons

**The web demo is less cinematic than a slider would have been.** A slider on stage that flips the verdict color in real-time is dramatic. Two pre-rendered reports side-by-side ("startup mode" vs "bank mode") is convincing but quieter. Mitigation: lean into the CLI as the demo's aesthetic — terminal-native demos hit different.

**Without sliders, the values lens has to be explained more carefully.** A slider self-explains. A flag requires the audience to grasp the concept in 5 seconds. Mitigation: show two side-by-side reports with the diff highlighted. Visual comparison does the work.

**Implementation surface is still large.** Three distinct layers, 7 principle checkers, persona library, caching prefix structure, speculative stopping, confidence aggregation, judge synthesis, long-context report prose. The CLI-first commitment doesn't reduce this — it just removes the additional frontend work.

**Wafer fit is moderate, not strong.** The Wafer pitch requires careful framing as "application-layer analog of inference engineering." Less natural than the Tensormesh pitch.

**Greptile is closer than earlier turns let on.** Greptile indexes the whole codebase and surfaces architectural concerns. Real differentiation is in (a) deterministic-first methodology, (b) named-principle citations, (c) standalone briefing as artifact, (d) values lens. Defensible but narrower than initially framed.

**Anthropic Code Review (March 2026) is a direct reference.** Anthropic's own multi-agent code reviewer shipped three months ago. The differentiator (whole-codebase audit vs. diff review) has to be explained in 15 seconds.

**Multi-Agent Debate doesn't reliably beat self-consistency.** Per Smit et al., ICML 2024. The defense — heterogeneous personas inject genuine variance — has to be argued.

**Layer 1 prioritization is the silent risk.** If the prioritizer surfaces boring decision points, the demo is boring no matter how good Layers 2 and 3 are.

**Persona correlation problem.** If 10 cells produce highly correlated votes, the jury reduces to "10 votes of the same thing."

**Demo runs require pre-caching.** The cached-replay mitigation works but means showing a recording, not a live system.

**Python-only narrows the audience.** Half the hackathon teams use TypeScript/Go/Rust.

### Net assessment of v1.3

CLI-first is the right call. It reduces risk concentration (no frontend bottleneck), produces a more honest product (the values lens is structural, not theatrical), and gives the team a working artifact at every hour of the build. The web wrapper is now a *bonus* — if it ships, great; if not, the demo still works.

The single biggest predictor of success remains: **does the team get Layer 1 + a credible Layer 2 single-cell working by hour 18?** If yes, you have a backstop demo at hour 20 (the deterministic-only version) and can iterate Layers 2 and 3 with confidence. If no, you'll be debugging plumbing during the polish window.

---

## 12. Decision points / principle library

Seven principles for v1, each with a deterministic checker:

| ID | Principle | Source | Checker | Affinity hint (top 2 values) |
|---|---|---|---|---|
| P1 | Acyclic Dependencies | Martin 2002 | Tarjan's SCC on module graph | maintainability, scalability |
| P2 | Stable Dependencies | Martin 2002 | Compute Ca, Ce, I; flag stable→unstable | maintainability, flexibility |
| P3 | Bounded Complexity | McCabe 1976 | Cyclomatic complexity per function | maintainability, correctness |
| P4 | Cohesion | Chidamber & Kemerer 1994 | LCOM-style metric per module | maintainability, simplicity |
| P5 | Reachability | Standard | Dead code from entry-point reachability | simplicity, correctness |
| P6 | Layering | Hexagonal/Clean architecture | Detect layers; flag upward deps | maintainability, flexibility |
| P7 | Common Closure | Martin 2002 | Git co-change across packages | maintainability, velocity |

A **decision point** is a structural choice satisfying all four: locatable, has plausible alternatives, consequential to the dep/call/reachability/co-change graph, not externally forced.

## 13. Verdict vocabulary

- **HEALTHY** — checked, no concern.
- **JUSTIFIED VIOLATION** — principle technically violated but defensible.
- **STRUCTURAL DEBT** — principle violated, refactor recommended.
- **CRITICAL** — severe; immediate action needed.
- **DRIFTED** — was correct; no longer fits current scale.
- **CONTESTED** — vote within 60/40; judge documents the tension.

Verdicts are jury-assigned and values-neutral. The report's *framing* is values-aware.

## 14. Expert persona library

**Red (prosecution):** Modularity Hawk · Scale Skeptic · Cohesion Auditor · Layering Enforcer · Refactor Pragmatist · Pattern Conservative

**Blue (defense):** Chesterton Preservationist · Pragmatic Defender · Refactor Cost Analyst · Domain Fit Specialist · Stability Champion · Migration Realist

6 × 6 = 36 possible pairings; 10 cells per decision point span the pairing space. Affinities used in Layer 1.5 prioritization only, never as vote weights in Layer 2.

## 15. Build plan (48 hours, v1.3)

| Hours | Phase | Owner | Acceptance criterion |
|---|---|---|---|
| 0–4 | CLI scaffolding + Layer 1 wiring | Full-stack dev | `Forum audit <repo>` runs deterministic checkers, writes evidence.json |
| 4–10 | Layer 1 principle checkers + graph output | Full-stack dev | All 7 checkers produce candidate decision points; `Forum graph` emits SVG |
| 10–14 | Layer 1.5 values-aware scoring | Full-stack dev | `--value` flags change top-N surfaced points; structural signals dominate |
| 10–18 | Single-cell debate loop | Agents dev | Red/Blue/Judge produces substantive verdict on one real decision point |
| 18–28 | Fanout to 10 cells + prefix caching | ML/inference dev | 10 cells run in parallel; measured cache hit rate ≥ 50% |
| 28–32 | Speculative stopping + confidence-weighted voting | Agents dev | Wall-clock per audit < 90s under realistic load |
| 32–38 | Layer 3 values-aware report writer | Full-stack dev | Two `--values` invocations produce visibly different reports; verdicts preserved |
| 38–42 | Layer 5 `Forum whatif` (cheap version first) | Agents dev | `whatif` re-projects in <2s without LLM calls |
| 42–46 | Demo polish — choose path | All | Either: web wrapper with animated reveal, OR: terminal-native demo with Rich |
| 46–48 | Rehearsal + video recording + Devpost submission | All | Best-of-three takes uploaded |

**Cut order if time runs short:**
1. First cut: Opus re-framing in `whatif` (use cheap diff version)
2. Second cut: Web wrapper (terminal demo is the fallback)
3. Third cut: 10 cells per decision point → 3 cells per decision point (collapse to single-tribunal-with-redundancy)
4. Fourth cut: Layer 5 entirely (Layers 1–3 alone make a complete demo)
5. Last resort: Drop to single Red/Blue/Judge per decision point (still ships, less dramatic)

**If Layer 1 isn't working by hour 10, stop and refactor. Do not proceed to Layer 2 on a broken foundation.**

## 16. Track strategy

| Track | Probability | Pitch lead |
|---|---|---|
| Tensormesh (Best Acceleration) | ~40–50% | "Senior engineers take 2 weeks; we do it in 3 minutes for fifty cents. 8× cost reduction from KV cache reuse across 50 parallel reasoning cells. And `Forum whatif` re-personalizes the audit with zero new inference." |
| Most Uncommon | strong | "A jury of AI experts puts your codebase on trial — calibrated to what your team values. Modularity Hawks and Scale Skeptics arguing whether your architecture violates Martin's Stable Dependencies Principle." |
| Wafer (Best Inference) | ~25–35% | "50-agent reasoning system. Speed: 90s via async fanout + speculative early-stopping. Efficiency: $0.50/audit via cascade + caching. Scale: 50 cells per query. Novelty: re-personalize the audit in <1s without re-running inference." |

Submit to all three if rules permit. The CLI is universal — the same demo command (`Forum audit fastapi`) works for all three pitches with different opening framings.

## 17. Risks summary (v1.3)

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Layer 1 misreads codebase | Medium | Catastrophic | Use mature tools (pydeps, radon, vulture). Don't reinvent. |
| 10 cells produce correlated votes | Medium | High | Heterogeneous personas, temperature variance, prompt orthogonality |
| Wall-clock exceeds 90s | High | High | Speculative stopping. Pre-cache demo run. Replay on stage. |
| Network flakes on stage | Medium | Catastrophic | CLI runs from cache. Real network is bonus. |
| Judge pushes back on specific verdict | Medium | Medium | Pre-run on demo repo. Pick verdicts you're confident in. |
| "Isn't this just Claude?" | High | Medium | Rehearsed answer about Layer 1 grounding + named-principle citations. |
| "Isn't this just Greptile?" | High | Medium | Rehearsed answer about enumeration-first methodology + standalone artifact. |
| Layer 2 fanout broken by hour 28 | Medium | High | Fallback: 3 cells per decision point. Demo still works. |
| Web wrapper breaks during demo | Low | Low | CLI fallback is rehearsed. Terminal-native demo is its own aesthetic. |
| Values discipline violated during dev | Medium | High | Code review rule: `user_values` must never appear in Layer 2 prompts. |
| Affinity table contested in Q&A | Low | Low | "v1 design judgment. Future versions learn from user feedback." |

## 18. Open questions for kickoff

- **Which public Python repo for the demo?** FastAPI, Flask, Django sub-app, requests, httpx. Decision: hour 0.
- **Cherry-pick demo violations?** Probably yes — pre-run on the demo repo, pick the 5 most defensible findings.
- **Web wrapper or terminal-native demo?** Default: build web wrapper if hours 42–46 are clean, otherwise terminal-native with Rich. Decision: hour 42.
- **Default value vector for first demo audit?** Suggestion: all 1.0 (neutral). Second run: "startup mode" (velocity=1.8, simplicity=1.5) for the values-lens contrast.
- **`whatif` cheap version vs. Opus re-framing?** Default: ship cheap version first ($0, 1s). Upgrade to Opus re-framing if time allows.
- **Demo recording: live or cached?** Default: cached. Live as bonus if pre-show network is solid.

---

## 19. The pitch sentences (memorize)

**One-sentence pitch (v1.3):**
> Forum is a CLI tool that audits your codebase against fifty years of software engineering literature — deterministically extracting every cycle, every coupling inversion, every cohesion violation — and submits each finding to a jury of ten AI experts who deliberate in parallel under heterogeneous adversarial personas, with a wide-context judge model synthesizing the panel into a written architectural briefing calibrated to your team's stated engineering values, in under three minutes for fifty cents.

**Tensormesh slide-1 line:**
> Senior engineers take two weeks to audit a codebase. Forum does it in three minutes for fifty cents — 8× cost reduction via KV cache reuse across 50 parallel reasoning cells, plus zero-cost re-personalization via cached transcript re-projection. Run `Forum whatif` and re-frame the entire audit in one second without spending a single new token.

**Wafer slide-1 line:**
> A 50-agent reasoning system that hits speed (90 seconds), efficiency ($0.50 per audit), scale (50 cells per query), and novelty (heterogeneous red/blue persona reasoning with deterministic evidence grounding plus zero-inference re-personalization) at the same time — by treating inference engineering principles as orchestration patterns.

**Most Uncommon slide-1 line:**
> A jury of AI experts puts your codebase on trial — calibrated to what your team actually values. Modularity Hawks and Scale Skeptics argue whether your architecture violates Martin's Stable Dependencies Principle. The verdict is what the jury found. The framing is what your team needs to hear.

**The "isn't this just Claude?" answer:**
> Claude can't enumerate every cycle in your dependency graph — Tarjan's algorithm does that, deterministically, in milliseconds. Claude can't compute the change-coupling matrix from a year of git history — pydriller does. Claude is the judge in our courtroom, not the bailiff and not the evidence. Three different tools, three different jobs.

**The "isn't this just Greptile?" answer:**
> Greptile is the closest comparable, and we respect their work. Differences: we're enumeration-first, not LLM-first — Tarjan's algorithm finds every cycle, not just the ones the LLM notices. We tie every verdict to named software-engineering principles with literature citations. We produce a standalone CLI-emitted briefing document, not PR comments scattered across history. And we calibrate to your team's stated engineering values without contaminating the analysis. Could Greptile ship this? In three months, yes. We think the audit-at-rest deliverable is the underserved category.

**The "isn't this just confirmation bias?" answer:**
> The `--value` flag doesn't change verdicts. It changes which decision points get surfaced (prioritization) and how findings are framed (presentation). The jury's deliberation is invariant. We separated those layers on purpose — personalization belongs at the edges, deliberation belongs at the core. `Forum whatif` proves it: re-project the same cached jury transcripts under any value vector, in one second, without new inference. Same jury. Different lens.

---

**End of PRD v1.3.**
