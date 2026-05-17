# Forum

**AI architectural audit, made tractable by ~10× cache-driven cost reduction —
multi-agent debate at single-agent prices.**

A 15-cell jury of monomaniacal value-personas debates each architectural
finding in a real codebase. A judge synthesizes the panel; an Opus report
writer assembles the briefing. Anthropic prompt caching collapses the
per-cell input cost so a full panel costs the same as one un-cached agent.

## The cache story

Every cell in a tribunal reads the same ~4–6 KB cached prefix: codebase
summary, principle definitions, debate rules, decision-point evidence.
The first cell writes the cache (paying 1.25× input price); cells 2–15
read it (paying 0.10× input price).

Per audit you'll typically see:

```
Cache savings: actual $0.420 · without cache $4.180 · 9.9× reduction
Per-cell cache hit rate (cells 0–5):
  cell  0:  0.0% hit · read=    0t  created= 4200t  uncached= 510t  $0.0079
  cell  1: 89.2% hit · read= 4200t  created=    0t  uncached= 510t  $0.0029
  cell  2: 89.2% hit · read= 4200t  created=    0t  uncached= 510t  $0.0029
  ...
Warm-cache cells (≥1): 89% hit rate across 14 cells
```

This is what makes a 15-agent panel economically viable. Without
caching, every audit would cost ~10× more — and the panel size would
have to shrink, which means losing the value diversity that drives
the verdict quality.

## Architecture

- **Layer 1** (deterministic): walks the repo, extracts decision points
  for ten structural principles — Martin's classic seven (cycles,
  stability, complexity, cohesion, reachability, layering,
  common-closure) plus stable-abstractions (P8 — I/A plane mis-placement),
  god classes/functions (P9 — size thresholds), and cross-file code
  duplication (P10 — jscpd). Off-the-shelf tools: vulture, radon,
  lizard, cppcheck, pydriller, jscpd.
- **Layer 1.5** (cheap math): re-projects findings under the user's
  value weights.
- **Layer 2** (LLM panel): 15 cells per finding. Each cell pairs two
  monomaniacal personas (Simplicity vs Velocity, Maintainer vs Shipper,
  etc.) who argue their value's reading of the evidence. A Sonnet judge
  renders one verdict per finding.
- **Layer 3** (Opus briefing): one markdown report synthesizing every
  verdict into an audience-framed memo.

## Run

```bash
uv run forum audit ./your-repo
```

Or with the live UI:

```bash
uvicorn server:app  # http://localhost:8000
```
