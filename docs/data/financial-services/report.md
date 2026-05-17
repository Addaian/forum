# Five `from typing import Optional` Lines, One Missing Lint Rule

Every prioritized finding in this audit is the same finding. Five files across `plugins/`, all scripts, all with an unused `from typing import Optional` at line 10 or 20, all flagged by vulture at 90% confidence, all ruled **STRUCTURAL DEBT**. The cross-finding pattern isn't five independent decisions — it's one missing CI guardrail in the `plugins/` tree allowing the same trivial violation to replicate across `agent-plugins/` and `vertical-plugins/`. With the team weighting flexibility and maintainability equally with everything else, the real cost isn't the five dead lines; it's that the absence of `ruff --select F401` (or equivalent) means finding #6, #7, and #8 are already being written as new skills get scaffolded. The ramp cost compounds linearly with plugin count.

## One pattern, copied five times

Look at the file paths side by side:

- `plugins/agent-plugins/model-builder/skills/dcf-model/scripts/validate_dcf.py:10` (#1)
- `plugins/agent-plugins/pitch-agent/skills/dcf-model/scripts/validate_dcf.py:10` (#2)
- `plugins/agent-plugins/pitch-agent/skills/ib-check-deck/scripts/extract_numbers.py:20` (#3)
- `plugins/vertical-plugins/financial-analysis/skills/dcf-model/scripts/validate_dcf.py:10` (#4)
- `plugins/vertical-plugins/financial-analysis/skills/ib-check-deck/scripts/extract_numbers.py:20` (#5)

Three files are named `validate_dcf.py` with the dead import on line 10. Two files are named `extract_numbers.py` with the dead import on line 20. This is not five mistakes — this is **two source templates** that were copied across three plugin hosts (`model-builder`, `pitch-agent`, `financial-analysis`). The dead `Optional` was almost certainly in the original scaffold, then propagated by copy-paste as the `dcf-model` and `ib-check-deck` skills were instantiated for each plugin.

That matters for sequencing. Fixing the five files one by one treats the symptoms. Fixing the source of the duplication treats the cause. And for a team weighting **flexibility** at 1.00, the structural question is louder than the lint question: *why are three plugins carrying near-identical skill scripts in the first place?* Each duplicated script is a future swap cost — every change to DCF validation logic now requires three synchronized edits, with drift basically guaranteed.

## The verdicts are unanimous but the dissents are coherent

All five findings landed on **STRUCTURAL DEBT**, none with override. But the dissent across panels is consistent enough to take seriously: cells repeatedly argue that `blast_radius` 0.2, `recency` 0.0, and full-PR-cycle overhead make a dedicated cleanup costlier than the symptom for any single file. That dissent is correct *per file* and wrong *in aggregate*. One PR to delete one line is overhead theater. One PR that lands a repo-wide `ruff` rule and deletes all five lines mechanically is the actual unit of work the dissents are missing.

The judge's reasoning on #2 and #5 adds a second dimension worth surfacing: both `validate_dcf.py` and `extract_numbers.py` sit at validation/boundary positions in the financial-analysis pipeline. A dead `Optional` import at a boundary is a weak but real signal that nullable-contract logic was removed or never completed. This is the only place the cost story shifts from pure maintainability into correctness — and it justifies the small surrounding-code audit the judge recommended on #3 and #4.

## What the team's vocabulary makes obvious

With flexibility and maintainability tied at the top, the framing the per-finding cards can't deliver is this:

- **Ramp cost.** A new contributor opening any of these three plugins hits inconsistent, partially-typed validation scripts. Five-minute confusion per file, multiplied by the duplication factor, multiplied by every new hire.
- **Swap cost.** Three copies of `validate_dcf.py` means changing DCF validation requires touching three plugins. The dead import is the cheap tell of a much more expensive structural condition.
- **Blast radius is per-file, not per-pattern.** The dissents anchor on 0.2 blast radius for each isolated script. But the pattern's blast radius is the entire `plugins/` tree, and it's growing with each new skill scaffold.

The five findings are not five places to spend attention. They are one signal that the `plugins/` tree has no enforced hygiene floor and an unconsolidated skill-scaffold story.

## What to do, in order

1. **Land one PR that adds `ruff --select F401` (or `flake8 F401`) to CI scoped to `plugins/**/scripts/`, and delete all five `Optional` imports in the same PR.** ~5 LOC of deletions, ~10 LOC of CI config, one review cycle. This is the single highest-leverage action in the audit. **After this lands, findings #1 through #5 all dissolve simultaneously**, and the next audit run will surface genuinely different top concerns instead of five rows of the same row.

2. **In the same PR or an immediate follow-up, do the boundary audit the judge flagged on #3 and #4.** Read the ~20–50 LOC around each deleted `Optional` in `validate_dcf.py` and `extract_numbers.py` and confirm no function signature is silently missing a nullable return or parameter. This is the only path that converts the five-finding cleanup from cosmetic into correctness-relevant. Budget: ~30 minutes per file, five files, half a day total.

3. **Open a separate investigation ticket on the duplication itself.** Three copies of `validate_dcf.py` across `model-builder`, `pitch-agent`, and `financial-analysis` is a flexibility liability the import-level audit can't see but the file paths make undeniable. The question is whether `dcf-model` and `ib-check-deck` should be shared skill libraries consumed by all three plugins, or whether the divergence between copies is already meaningful. This is not an audit finding — it's the structural question the audit findings are pointing at. Scope it before the next audit run so the dedup work, if warranted, lands on a clean base.

4. **Extend the lint scope on the next pass.** Once `plugins/**/scripts/` is clean, widen `ruff` to the full `plugins/` tree and then to the repo root. Each widening is a single PR. Doing this incrementally avoids a noisy bulk-fix commit and keeps blast radius low per step.

The takeaway: the audit prioritized these correctly by structural impact, but read as a strategic input, it's telling you about a missing CI rule and a duplicated scaffold — not five independent decisions. Step 1 closes the whole class. Steps 2 and 3 are where the real work is.