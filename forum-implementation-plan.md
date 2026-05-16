# Forum — Implementation Plan

> Task-driven build guide. Each task specifies what we want, what we explicitly don't want, and how we know it's done.

**Companion document:** `forum-prd-v1.3.md`
**Stack:** Python 3.12+, Anthropic-only (Haiku 4.5 / Sonnet 4.6 / Opus 4.7), CLI-first.

---

## 1. Context: where we're going

**End goal:** A working CLI tool, `forum`, that takes a public Python GitHub repo and produces a markdown architectural briefing — citing named software engineering principles, surfacing 5 prioritized decision points, each judged by a 10-cell jury of AI experts, with a written verdict synthesized by a wide-context judge. Total runtime ~3 minutes. Total cost ~$0.50 per audit.

The demo on stage is `forum audit fastapi` running in a terminal, ending with a rendered markdown report visible behind the speaker. Three pitches, same demo:

- **Tensormesh:** "Senior engineers take two weeks. Forum does it in three minutes via KV-cache reuse across 50 parallel reasoning cells. 8× cost reduction."
- **Wafer:** "50-agent reasoning system. Speed, efficiency, scale, novelty. Zero-inference re-personalization via cached transcript re-projection."
- **Most Uncommon:** "A council of AI experts puts your codebase on trial against fifty years of software engineering literature."

**Three-layer pipeline:**

```
Layer 1: Deterministic evidence extraction         (~30s, $0)
   ↓
Layer 1.5: Values-aware prioritization              (~2s, $0)
   ↓
Layer 2: Jury — 10 parallel red/blue cells + judge  (~60–90s, $0.40)
   ↓
Layer 3: Single Opus call writes the briefing       (~30s, $0.10)
```

**Three engineers, three lanes:**

| Owner | Lane |
|---|---|
| **FS** (full-stack) | CLI shell · Layer 1 extraction · Layer 3 report writer |
| **AG** (agents) | Layer 2 single-cell debate · judge · Layer 5 whatif |
| **ML** (inference) | Layer 2 fanout · prompt caching · speculative stopping |

**What success looks like:** `forum audit fastapi --values demo-values.yaml` runs end-to-end and writes a `report.md` that reads like a staff engineer wrote it. The same command with a different value vector produces a visibly different report but identical jury verdicts on overlapping decision points. One clean 90-second video.

---

## 2. The contract (locked before any layer code)

Every layer is built against these types. Place in `src/forum/types.py` first.

```python
from typing import Literal, Optional
from pydantic import BaseModel

# Layer 1 output
class CodeLocation(BaseModel):
    file: str
    line_start: int
    line_end: int
    module: str

class DecisionPoint(BaseModel):
    id: str
    principle: Literal["P1","P2","P3","P4","P5","P6","P7"]
    locations: list[CodeLocation]
    subject: str
    evidence: dict
    alternatives: list[str]
    measured_impact: dict
    code_snippets: list[str]

class EvidenceBundle(BaseModel):
    repo: str
    commit_sha: str
    decision_points: list[DecisionPoint]
    graph_summary: dict
    git_summary: dict

# Layer 2 output
Verdict = Literal["HEALTHY","JUSTIFIED VIOLATION","STRUCTURAL DEBT",
                  "CRITICAL","DRIFTED","CONTESTED"]

class CellVote(BaseModel):
    cell_id: int
    red_persona: str
    blue_persona: str
    position: Literal["debt","justified"]
    confidence: float
    key_argument: str
    value_lens: dict[str, float]
    transcript: list[dict]

class TribunalResult(BaseModel):
    decision_point_id: str
    cells: list[CellVote]
    aggregate_vote: dict
    judge: dict

# Layer 3 output
class ReportArtifact(BaseModel):
    markdown: str
    headline: str
    stats: dict
```

Once these are committed, every layer can be built in parallel against stubs.

---

## 3. Task dependency graph

```
T0 ──┬──> T1 ──> T2 ────────────────────────────────────┐
     │                                                    │
     ├──> T3 ──> T4 ──────────────────────────────┐      │
     │           │                                  │      │
     │           ▼                                  ▼      ▼
     ├──> T5 ──> T6 ──> T7 ─────────────────────> T8 ──> T10
     │                   │                                 ▲
     │                   ▼                                 │
     │                   T9 ────────────────────────────────
     │
     └──────────────────────────────────────────────────────
```

T1, T3, T5 can start immediately after T0. T8 needs T2, T4, T7. T9 needs T7. T10 is last.

---

## 4. Tasks

Each task specifies three things: **what we want** (positive scope), **what we don't want** (negative scope, including scope creep traps), and **achievement** (concrete, observable done-when conditions).

---

### T0 — Kickoff and shared scaffolding

**Depends on:** nothing
**Owner:** all three

**What we want:**
- Repo skeleton with the layout below committed and pushed.
- `src/forum/types.py` populated and committed exactly as in section 2 — no edits.
- Demo repo (**FastAPI**) cloned to `/tmp/forum-demo-repo` on every laptop.
- Working `.env` with Anthropic API key. Every engineer runs a one-line script that successfully hits Haiku, Sonnet, and Opus.
- `demo-values.yaml` (neutral-ish: velocity 1.5, simplicity 1.2, others 1.0) and `startup-values.yaml` (velocity 1.8, simplicity 1.5, flexibility 0.6) committed.
- Cache schema documented in a `CACHE_SCHEMA.md`: `./audits/<repo-hash>/{evidence,prioritized,verdicts}.json` + `report.md`. Format frozen.

Repo layout:
```
forum/
├── pyproject.toml          # uv-managed
├── src/forum/
│   ├── cli.py
│   ├── types.py
│   ├── evidence/
│   ├── prioritize/
│   ├── jury/
│   ├── report/
│   ├── whatif/
│   ├── personas/
│   └── values/
├── prompts/
└── audits/                 # gitignored
```

**What we don't want:**
- Debating the repo choice. FastAPI is locked.
- Debating the stack. Anthropic-only is locked. No OpenAI, no local models, no LangChain, no LlamaIndex.
- Adding fields to `types.py` mid-build. Frozen at T0.
- A `requirements.txt`. Use `uv` and `pyproject.toml`.
- A web frontend folder. We're CLI-first; the wrapper is the cut-first scope.
- Custom logger. Use stdlib `logging` + Rich for the human-readable layer.
- Auth, multi-tenant, persistence layer beyond local JSON files.

**Achievement:**
1. Every engineer runs `python -c "from forum.types import DecisionPoint, EvidenceBundle, CellVote, TribunalResult, ReportArtifact"` with zero errors.
2. Every engineer runs a Haiku, Sonnet, and Opus one-liner against the shared API key and prints the response.
3. `ls /tmp/forum-demo-repo` shows the FastAPI source tree.
4. `git log --oneline` shows the initial scaffold commit signed by all three engineers (co-author lines).

---

### T1 — Layer 1: deterministic evidence checkers

**Depends on:** T0
**Owner:** FS

**What we want:**
- A CLI invocation `forum audit /tmp/forum-demo-repo --skip-jury --skip-report` that runs end-to-end and writes a real `evidence.json` to `./audits/<hash>/evidence.json`.
- Seven principle checkers in this order of priority (build P1 first, P7 last):

| ID | Principle | Implementation | Threshold |
|---|---|---|---|
| P1 | Acyclic Dependencies | `pydeps` → `networkx.DiGraph` → Tarjan's SCC | SCC size > 1 |
| P2 | Stable Dependencies | Compute Ca, Ce, I per module; flag stable→unstable edges | I > 0.7 depending on I < 0.3 |
| P3 | McCabe Complexity | `radon cc -j` | cc > 15 |
| P4 | Cohesion (LCOM) | Per class: method pairs sharing zero attrs vs. sharing some | LCOM > 0.7 |
| P5 | Reachability | `vulture` | confidence > 80% |
| P6 | Layering | BFS layer assignment from entry points, flag upward edges | any upward edge |
| P7 | Common Closure | `pydriller` 12-month co-change | co-occur ≥ 5 times across packages |

- Each checker produces 0+ `DecisionPoint` instances with `principle`, `locations`, `subject`, `evidence`, `alternatives`, `measured_impact`, `code_snippets` all populated.
- `graph.svg` written next to `evidence.json` via pydeps SVG output.

**What we don't want:**
- LLM calls in Layer 1. Zero. None. The whole point of Layer 1 is determinism.
- New principle checkers beyond the seven. If P5 vulture is flaky, skip P5 — don't replace it.
- Building our own AST parser. Use tree-sitter or Python `ast` stdlib only.
- Rewriting pydeps' output format. Consume it as-is.
- Generic "code quality" or "find bugs" logic. We surface *structural decisions*, not bugs.
- Caching Layer 1 output across runs. Re-run on every audit — it's already fast.
- Trying to handle non-Python repos. Single language, this hackathon.

**Achievement:**
1. `forum audit /tmp/forum-demo-repo --skip-jury --skip-report` exits 0.
2. `cat ./audits/<hash>/evidence.json | jq '.decision_points | length'` returns ≥ 10.
3. `cat ./audits/<hash>/evidence.json | jq '[.decision_points[].principle] | unique | length'` returns ≥ 5.
4. The output contains the known routing↔dependencies↔params cycle in FastAPI (verifiable: at least one `DecisionPoint` with `principle: "P1"` whose `locations` include files matching `routing.py`, `dependencies.py`, `params.py`).
5. `ls ./audits/<hash>/graph.svg` exists and renders in a browser.

**This task is the foundation.** If T1 isn't passing achievement criteria, do not unblock T2, T6, or T8 to run on T1's output. Stub data only.

---

### T2 — Layer 1.5: values-aware prioritization

**Depends on:** T1
**Owner:** FS

**What we want:**
- `values/affinities.yaml`: hand-curated principle→value affinity table. Six values (scalability, maintainability, velocity, correctness, simplicity, flexibility), seven principles, ~42 numeric entries in [-1.0, 1.0]. ~30 minutes of effort. Don't overthink it.
- `prioritize/score.py` implementing:
  ```python
  composite_score = structural_score * (1 + 0.5 * value_affinity_score)
  ```
  where `structural_score` is a weighted sum of `blast_radius`, `recency`, `principle_severity`, `pattern_violation`, `advocate_absence` (all from `measured_impact`).
- `--top-n` flag, default 5.
- Output written to `./audits/<hash>/prioritized.json`.

**What we don't want:**
- LLM calls in Layer 1.5. Pure scoring math.
- Learning the affinity table from data. Hand-curated, v1.
- Tuning structural-score weights to be clever. Equal weights is fine.
- A complex multi-objective optimizer. It's a linear combination, that's it.
- Letting `value_affinity_score` overpower `structural_score` so much that two value vectors produce *disjoint* top-5 sets. Cap the multiplier at 1.5×.

**Achievement:**
1. Running `forum audit --values demo-values.yaml --skip-jury --skip-report` and `forum audit --values startup-values.yaml --skip-jury --skip-report` on the same repo produces two `prioritized.json` files.
2. The two top-5 lists overlap by at least 2 decision points (not disjoint — the codebase is the same).
3. The two top-5 lists are not identical (the value vector should reorder).
4. The top decision point under `startup-values.yaml` is plausibly velocity- or simplicity-flavored when inspected by hand.

---

### T3 — Single-cell debate scaffold

**Depends on:** T0
**Owner:** AG

**What we want:**
- `prompts/red.md` and `prompts/blue.md`: system prompts that accept a `{persona}` placeholder describing the speaker's character (values, what makes them angry, what they look for).
- `personas/red_pool.yaml` and `personas/blue_pool.yaml`: 6 Red + 6 Blue personas, ~200 words each, each with fields: `name`, `champions`, `angered_by`, `pattern_match_for`, `value_affinities` (dict).
- `jury/single_cell.py`: takes a `DecisionPoint` (stub OK initially) + a `(red_persona, blue_persona)` pair + a temperature, runs the debate:
  1. Red turn 1 (Haiku 4.5, ~600 tokens, temperature from cell)
  2. Blue turn 1 (Haiku 4.5, ~600 tokens, same temperature)
  3. Red turn 2 (rebut)
  4. Blue turn 2 (close)
  5. Vote extraction via Anthropic tool-use, returning `CellVote` with `position`, `confidence`, `key_argument`, `value_lens`, full `transcript`.
- Tool-use schema for vote enforced at the API level.

**What we don't want:**
- More than 4 debate turns. Hard cap.
- More than ~600 tokens per turn. Hard cap. Use the `max_tokens` parameter.
- Free-form text vote parsing. Tool-use only.
- A "moderator" agent. Red and Blue speak directly.
- Personas that all sound the same. If two personas produce indistinguishable arguments on the same `DecisionPoint`, rewrite one of them.
- Letting persona prompts include the user's value vector. Personas are values-neutral; they argue from their own values, not the user's. (Rule 2 of the values-lens design discipline.)

**Achievement:**
1. `python -m forum.jury.single_cell --stub --red modularity_hawk --blue pragmatic_defender --temperature 0.7` produces a debate transcript and a structured `CellVote` JSON.
2. Two consecutive runs with different temperatures produce visibly different arguments.
3. Two consecutive runs with different persona pairs produce *meaningfully* different arguments (different concerns raised, different evidence cited).
4. The `value_lens` field is populated with at least one nonzero entry per cell.

---

### T4 — Judge synthesis

**Depends on:** T3
**Owner:** AG

**What we want:**
- `prompts/judge.md`: system prompt for the per-decision-point judge model. Takes the cell panel transcripts + votes + the original `DecisionPoint` evidence. Emits a structured `JudgeOutput` via tool-use.
- One Sonnet 4.6 call per decision point (not per cell).
- Judge prompt explicitly instructs the model to:
  - Cite at least two specific cells' arguments in `reasoning`.
  - Cite specific Layer 1 evidence (file paths, line numbers, measured values) in `reasoning`.
  - Surface the strongest dissent in `dissent_summary`.
  - Recommend a concrete next step in `recommended_action`.
- Judge has override authority: if 7/10 cells vote "debt" but evidence shows clear domain-fit justification, judge can return `JUSTIFIED VIOLATION`. Document this explicitly in the prompt.

**What we don't want:**
- A judge that just regurgitates the majority vote. The synthesis must add reasoning, not just count.
- The judge prompt seeing the user's value vector. (Same Rule 2: jury is values-neutral.)
- Multiple judge candidates that vote among themselves. One judge per decision point.
- Long-context Opus for the judge. Sonnet 4.6 is the right tier for this synthesis cost.
- A judge that picks verdicts outside the six in the enum (HEALTHY, JUSTIFIED VIOLATION, STRUCTURAL DEBT, CRITICAL, DRIFTED, CONTESTED). Enforce via tool-use schema.

**Achievement:**
1. Feeding the judge a stubbed panel of 10 votes (mix of debt/justified) returns a `JudgeOutput` with all required fields.
2. The judge's `reasoning` paragraph mentions at least two cell IDs by reference.
3. The judge's `recommended_action` is at least one specific sentence (not "refactor this").
4. On a real FastAPI decision point, the verdict reads coherently to all three engineers when reviewed.

---

### T5 — Prompt caching wrapper

**Depends on:** T0
**Owner:** ML

**What we want:**
- `forum/cache/prompt_cache.py`: wrapper around the Anthropic SDK that enforces the cache prefix structure:
  ```
  [SYSTEM: cache_control=ephemeral]
    <codebase_summary>...</codebase_summary>
    <git_summary>...</git_summary>

  [USER turn 1: cache_control=ephemeral]
    <decision_point_evidence>...</decision_point_evidence>
    <principle_definition>...</principle_definition>

  [USER turn 2: NO cache_control]
    You are the {RED_PERSONA}. Argue...
  ```
- Wrapper logs per-call: `cache_creation_input_tokens`, `cache_read_input_tokens`, `input_tokens`, `output_tokens`, model, latency.
- A `CacheMetrics` aggregator that produces summary stats per audit: total tokens, % from cache reads, total cost in USD, avg latency.

**What we don't want:**
- Custom in-memory caching. Anthropic's `cache_control` is the cache.
- Persistent (non-ephemeral) cache_control. Demo cache lifetime is plenty.
- Building our own retry / rate-limit handling. Use the SDK's retries.
- Sneaking the user value vector into the cached system prompt. (Same Rule 2.)
- Logging full prompt contents in production paths. Log only metadata.

**Achievement:**
1. Running the same Haiku call twice in a row (same prefix) shows `cache_read_input_tokens` > 0 on the second call.
2. The ratio `cache_read_input_tokens / (cache_read_input_tokens + input_tokens)` on the warm call is ≥ 80%.
3. `CacheMetrics` aggregator returns a populated dict at end of run.
4. Cost calculation matches Anthropic's posted prices (verify with one call).

---

### T6 — 10-cell parallel fanout

**Depends on:** T3, T5
**Owner:** ML

**What we want:**
- `jury/parallel.py`: for one `DecisionPoint`, fan out 10 cells via `asyncio.gather`.
- Persona pairing: deterministic round-robin across 36 possible (red, blue) pairs. Cell 0 always pairs (Modularity Hawk, Chesterton Preservationist); cell 1 always (Scale Skeptic, Pragmatic Defender); etc. Reproducible across runs.
- Temperature: cell index 0 → 0.5, cell 9 → 0.9, linear.
- All 10 cells share the cached codebase + decision-point prefix (via T5's wrapper).
- Each cell writes its `CellVote` to a list as it completes.

**What we don't want:**
- Random persona pairing. Demos must be reproducible.
- More than 10 cells per decision point. Hard cap.
- Spawning all 50 cells across 5 decision points simultaneously. Run 5 tribunals in parallel, but within each tribunal the 10 cells run together — limits concurrent API calls to a manageable number.
- A separate orchestration framework (Temporal, Prefect, etc.). `asyncio.gather` is the orchestration.
- Different cell models. All 10 cells run on Haiku 4.5.

**Achievement:**
1. Running one tribunal (10 cells on one stubbed decision point) completes in < 30s wall-clock.
2. Measured cache hit rate across the 10 cells is ≥ 50% (target ≥ 80%).
3. All 10 cells return valid `CellVote` JSON.
4. Two consecutive runs on the same input produce identical persona pairings (determinism check).
5. The 10 cells produce at least 4 distinct `key_argument` strings (variance check — if all 10 say roughly the same thing, the personas aren't differentiating).

---

### T7 — Speculative stopping + confidence-weighted aggregation

**Depends on:** T6
**Owner:** AG

**What we want:**
- `jury/speculative.py`: monitors cell results as they stream in via `asyncio.as_completed`. Once 6 of 10 cells have voted in the same direction with average confidence ≥ 0.7, cancel the remaining cells via `asyncio.Task.cancel()`.
- `jury/aggregate.py`: confidence-weighted majority:
  ```python
  score_debt = sum(c.confidence for c in cells if c.position == "debt")
  score_justified = sum(c.confidence for c in cells if c.position == "justified")
  winner = "debt" if score_debt > score_justified else "justified"
  margin = abs(score_debt - score_justified) / (score_debt + score_justified)
  ```
- Aggregate emits the dict that goes into `TribunalResult.aggregate_vote`.
- Cancellation must be safe: no hanging HTTP connections, no resource leaks. Wrap calls in proper context managers; use `asyncio.shield` where appropriate.

**What we don't want:**
- Speculative stopping that cancels too aggressively. 6/10 with high confidence is the floor; don't drop to 4/10.
- Reusing cancelled cells' partial transcripts. If a cell is cancelled mid-debate, its vote is discarded.
- Plain majority voting (ignoring confidence). The weights are the whole point.
- Aggregation logic in Layer 3. Layer 2 emits a finalized `aggregate_vote` and `JudgeOutput`.

**Achievement:**
1. End-to-end audit on FastAPI with 5 tribunals and up to 50 cells completes in < 90s wall-clock.
2. Average number of cells actually run per tribunal (logged): in the range [5, 8].
3. No `asyncio` warnings about pending tasks or unclosed sessions in logs.
4. `aggregate_vote` is correctly computed and matches a manual calculation on a sample tribunal.

---

### T8 — Layer 3 report writer

**Depends on:** T2, T4, T7
**Owner:** FS

**What we want:**
- `prompts/report.md`: the most important single prompt in the system. Instructs Opus 4.7 to:
  1. Take all 5 judge verdicts + `EvidenceBundle` + user value vector as input.
  2. Open with the verdict most aligned to the user's top-weighted value.
  3. Order recommended actions by value alignment.
  4. Frame prose in vocabulary matching user values (velocity-first → "ship cost," "iteration drag"; correctness-first → "risk surface," "failure modes"; maintainability-first → "ramp cost," "blast radius").
  5. **Preserve every jury verdict literally — do not modify or soften them.**
  6. Surface dissents that match user values prominently.
- `report/writer.py`: takes the cached artifacts + value vector, makes a single Opus 4.7 call, writes `./audits/<hash>/report.md` and prints to stdout.
- Output ~1500–2000 words of markdown.

**What we don't want:**
- The report prompt seeing only the verdicts and not the original evidence. It needs full context to write prose with measured numbers.
- Multiple report writer calls. One pass. One artifact.
- The report changing verdicts based on value vector. (Rule 2 again; this is the discipline.)
- Inline HTML, fancy formatting, embedded base64 images. Plain markdown only.
- A "TL;DR" generated by a separate call. The report itself is the deliverable.
- Custom retry logic for Opus. If it fails, fail loudly.

**Achievement:**
1. Two audits on FastAPI with `demo-values.yaml` and `startup-values.yaml` produce two `report.md` files.
2. The two reports differ in: opening headline, section ordering, prose vocabulary, recommendation order.
3. For decision points that appear in both reports' top-5 lists, the jury verdict text is identical.
4. Both reports read coherently to all three engineers when reviewed — i.e., would a staff engineer think a peer wrote this? If yes, T8 passes. If no, iterate on the prompt.
5. Word count is in [1200, 2500] for both reports.

---

### T9 — Layer 5: whatif probe

**Depends on:** T7
**Owner:** AG

**What we want:**
- `forum whatif <cache-path> --value <key>=<value>` CLI command.
- Cheap version: pure Python over cached `verdicts.json`. For each decision point, read the `value_lens` fields of each cell. Compute which dissents become salient under the new weights. Emit a markdown diff showing:
  ```
  ### Under your new weights:
  Decision point #1 (cycle): original verdict STRUCTURAL DEBT (7-3).
    - Cell 7 (Scale Skeptic) argued the cycle hurts horizontal scale.
      Under your weights, this argument is 1.8× more salient.
    - Verdict would have flipped at velocity weight ≥ 2.3.
  ```
- Zero LLM calls. Zero cost. < 1 second.

**What we don't want:**
- Re-running the jury under new weights. The whole point is honest re-projection from cached deliberation.
- The Opus re-framing stretch goal blocking T10. Cut it if not done by the time T10 starts.
- The whatif probe claiming the verdict has changed. The verdict is what the jury found. The probe shows what *would* have changed if the user had weighted differently — that's a different statement.
- A web UI for this. CLI command.

**Achievement:**
1. `forum whatif ./audits/<hash> --value scalability=2.0` completes in < 2 seconds.
2. Zero new LLM calls (verifiable by token-usage delta = 0).
3. Output identifies at least one decision point where the dissent would have shifted.
4. Output never claims the verdict itself has changed — only that dissent has become more or less salient.

---

### T10 — Demo polish + recording

**Depends on:** T8, T9
**Owner:** all three

**What we want:**
- CLI output beautiful: Rich progress bars during Layer 1/2/3, syntax-highlighted code snippets in the report, in-terminal markdown rendering of `report.md` via `rich.markdown.Markdown`.
- Cached-replay mode: `forum audit --replay ./audits/fastapi-demo-cache` runs through the same animations and writes the same `report.md` but doesn't hit the API. Use this as the demo backstop.
- One 90-second video recording (best of three takes).
- Devpost writeup using the pitch sentences from PRD section 19 verbatim.
- Submit to all three tracks (Tensormesh, Most Uncommon, Wafer) if rules permit.

**What we don't want:**
- A web wrapper. Cut.
- Live audit on stage if WiFi is iffy. Use cached replay; it looks identical.
- A 3-minute video. Edit ruthlessly to 90 seconds.
- Different writeups per track. Same writeup, different lead sentence (per PRD section 19).
- Pre-recorded voiceover. Live commentary over screen recording.
- A landing page or marketing site. The Devpost page is the marketing.

**Achievement:**
1. `forum audit --replay ./audits/fastapi-demo-cache` runs in < 10 seconds (replay mode) and produces the same `report.md` as a live run.
2. One MP4 file in the repo named `DEMO_VIDEO.mp4`, ≤ 90 seconds, audio audible, screen content legible at 1080p.
3. Devpost submission accepted (visible on the Devpost project page).
4. All three engineers have reviewed and signed off on the final pitch text.

---

## 5. The critical path

Five tasks gate everything else. If one of these is broken, stop new work and fix it before moving on.

| Gate | Task | Why |
|---|---|---|
| 1 | T1 (Layer 1 produces real findings) | Everything downstream needs real data |
| 2 | T3 (single-cell produces coherent verdict) | Proves deliberation works |
| 3 | T6 (10-cell fanout with cache hits) | The Tensormesh/Wafer pitch lives here |
| 4 | T8 (report reads like staff engineer wrote it) | The artifact is the demo |
| 5 | T10 (clean recording exists) | Devpost requires it |

---

## 6. Cut order

In order, from first to cut to last:

1. **Opus re-framing in whatif** — ship cheap text-diff version only
2. **Web wrapper** — terminal demo is the default
3. **10 cells → 3 cells per tribunal**
4. **Layer 5 entirely** — T1–T8 alone make a complete demo
5. **Top-N: 5 → 3** — fewer tribunals
6. **Principles: 7 → 4** — keep P1, P2, P3, P7
7. **Last resort:** single Red/Blue/Judge, no fanout

**Never cut:** Layer 1, principle citations in verdicts, cache prefix structure, the markdown report.

---

## 7. Risk huddles

10-minute team check-ins after T3, T6, T8. Honest answers:

- **After T3:** Does Layer 1 surface real architecture findings? Does the single-cell debate read coherently? Do the personas actually argue differently?
- **After T6:** Wall-clock per tribunal < 30s? Cache hit > 50%? Are the 10 cells producing meaningfully different arguments?
- **After T8:** Does the report read like a staff engineer wrote it? Do value vectors visibly shape framing? Are jury verdicts preserved across value vectors?

If any answer is "no" — stop new features, fix what's broken.

---

## 8. Demo fallback plan

1. **Primary:** Live `forum audit fastapi` on stage.
2. **Plan B:** Cached replay — `forum audit --replay ./audits/fastapi-demo-cache`. Set up before T10 finalization.
3. **Plan C:** Static screenshots of `report.md` rendered in a code editor.

---

## 9. Day-of-demo commit

- `README.md` with install: `uv tool install forum`.
- `demo-values.yaml` and `startup-values.yaml` for the two-pass demo.
- Pre-cached `./audits/fastapi-demo/` for replay.
- `prompts/` directory committed — judges should be able to read Red/Blue/Judge/Report prompts.

---

**End of plan.**
