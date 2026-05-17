# The `evals/` Directory Is Carrying All Five Findings

Every prioritized decision point in this audit lives under one directory: `evals/`. Three modules — `run_evals.py`, `generate_review.py`, `aggregate_benchmark.py` — account for all five findings. The pattern is consistent: concrete CLI entry-point scripts that grew organically, with one of them (`run_evals.py`) having absorbed a 43-branch `main()` and the other (`generate_review.py`) carrying a dead `format` parameter. With value weights flat at 1.00 across all six axes, no single lens dominates, which means the audit collapses to a simple question: where does fixing one thing produce the most downstream simplification, and where is the correctness risk concrete rather than speculative? Two findings carry **STRUCTURAL DEBT** verdicts; three carry **JUSTIFIED VIOLATION**. The real work lives in the two that the judge flagged.

## The two real defects are both inside `run_evals.py` and `generate_review.py`

Findings #1 and #5 are the load-bearing items. #1 is a verified-unreachable `format: str` parameter at `evals/generate_review.py:382` with vulture confidence=100 — a clean P5 violation, **STRUCTURAL DEBT**, ~1 LOC to fix. #5 is `main()` in `evals/run_evals.py:887-1068` at cyclomatic complexity 43 against a threshold of 15, also **STRUCTURAL DEBT** (override=True, judge overrode a near-tied panel margin of 0.0644). These are the only two verdicts where the judge said "yes, fix this." Everything else is contextually justified.

The shared cause across both: `run_evals.py` and `generate_review.py` are concrete orchestration scripts where the original author stayed inside one function/file rather than introducing seams. That's reasonable for a CLI script — until `main()` reaches 43 branches and parameters go stale without anyone noticing. The dissent on #1 raises a legitimate caveat — `log_message(self, format, *args)` matches Python's `BaseHTTPRequestHandler` signature, so a 15-minute grep against the parent class is the prerequisite before deletion. If `BaseHTTPRequestHandler.log_message` defines that signature, the symbol is not dead, it's a protocol obligation, and a vulture whitelist entry is the correct move instead.

The cost lens here is **cognitive load**, not blast radius. Both files have low afferent coupling (#1 blast_radius=0.2, #5 blast_radius=0.086) — nothing else in the repo depends on them. The damage is local: any contributor changing eval behavior has to load 181 lines of `main()` into their head, and any contributor touching `generate_review.log_message` has to puzzle out why `format` exists.

## Three "OFF-SEQUENCE" findings that the panel correctly defused

Findings #2, #3, and #4 are all P8 (main-sequence distance) violations on the same three modules: `run_evals.py`, `generate_review.py`, `aggregate_benchmark.py`. Each scored A=0.00, I=0.50, distance=0.50. All three landed at **JUSTIFIED VIOLATION**, and the reasoning is the same in each case: Martin's main-sequence heuristic was designed for reusable library components, and these are leaf CLI scripts with zero internal afferent coupling in a 3-module codebase. There is no architectural compromise being made — there's nothing to compromise against.

This is the kind of pattern an audit will surface mechanically but a team should not act on mechanically. The recommended action across all three is the same: add inline grouping comments, monitor for afferent coupling growth, and revisit only if a second module starts importing from any of them. That's a "do almost nothing" response, and it's the right one. The dissent worth preserving: #4's panel noted that `aggregate_benchmark.py` parses two different directory layouts with no defensive validation — a small, cheap correctness improvement (~5-10 LOC) that's worth folding into the work on #5 since both touch the same package.

## Why the sequencing matters

Notice what happens after #5 lands. Extracting `_resolve_iteration_id()` and `_validate_iteration_path()` from `main()` in `run_evals.py` introduces seams *into* the module. That partially addresses the dissent on #2 — "five orthogonal execution modes in one concrete module create implicit dispatch correctness risk" — without doing a structural refactor. The 38 methods get reorganized as a side effect. The next audit will probably not re-surface #2 in the same form, because the worst-offender function inside it will be gone.

Similarly, the prerequisite grep for #1 (verifying `BaseHTTPRequestHandler.log_message`) is the kind of check that, once done, gives a contributor a much clearer mental model of `generate_review.py`'s 13 methods. That partially defuses #3's already-justified verdict.

## What to do, in order

1. **Verify and resolve the dead `format` parameter** in `evals/generate_review.py:382`. Grep for `BaseHTTPRequestHandler.log_message` in the Python stdlib — if it defines `def log_message(self, format, *args)`, add a one-line vulture whitelist entry explaining the protocol obligation. If it doesn't, delete the parameter. ~1 LOC, ~15 minutes of investigation, one PR. *Resolves #1. Reduces the case for treating #3 as anything beyond a monitoring item.*

2. **Decompose `main()` in `evals/run_evals.py:887-1068`** by extracting `_resolve_iteration_id()` (~20 LOC) and `_validate_iteration_path()` (~15 LOC), then continuing extractions until every resulting function sits at CC ≤ 15. One focused PR. *Resolves #5. The 38-method count in #2 stops being a single undifferentiated wall, and the dissent on #2 about "implicit dispatch correctness risk" largely dissolves — the dispatch becomes explicit.*

3. **Fold a small hardening pass into `evals/aggregate_benchmark.py`** while the eval/ directory is already open: add the directory-layout validation the #4 dissent flagged (~5-10 LOC) and document both layout contracts in the module docstring (~5 LOC). One PR, opportunistic. *Closes the only substantive correctness concern across the three JUSTIFIED VIOLATION findings.*

4. **Add inline section-grouping comments** to `run_evals.py` and `generate_review.py` (~5 LOC each) separating concerns (cache/executor/selector/grader/CLI for the first; HTTP/embedding/feedback for the second). One PR. *Cheapest possible response to #2 and #3, appropriate given their JUSTIFIED VIOLATION verdicts.*

5. **Stop here and re-run the audit.** With #1 and #5 resolved and the cosmetic comment passes done, the next Layer 1 scan on `evals/` will likely surface entirely different concerns — probably in whatever new helpers step 2 introduced, or in coupling that emerges if a fourth module joins the package. The current top-5 will no longer be the top-5, which is the point.

The total cost across steps 1–4 is on the order of 4 PRs and well under 100 LOC of net change. None of it is a refactor; all of it is targeted. The team's flat value vector permits this kind of minimal-intervention plan precisely because no single axis is screaming for more.