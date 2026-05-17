"""One debate cell: two personas read the evidence and argue from their
respective values; a neutral observer extracts a vote at the end.

Each cell is a self-contained set of Haiku/Qwen calls:

    1. Persona A opens                    ← persona A reads the evidence
    2. Persona B responds                 ← persona B reads the evidence
    3. Persona A reacts to B              ← engages B's specific claims
    4. Persona B closes                   ← engages A's reaction
    5. Vote extraction (tool-use only)    ← neutral observer

Neither persona is locked to "debt" or "justified" — each argues for
whichever side their value reads in the evidence. When both personas
converge, that's a strong cell vote. When they diverge, the observer
picks the side whose argument was more specific and better-grounded.

The two persona pools (red_pool / blue_pool) carry their original file
names for compatibility; semantically they're "pool A" and "pool B"
characterised by their typical predisposition (critical vs defending)
but not bound to a side.

Multi-turn calls share the cached prefix (codebase summary in system,
DP evidence in user-block-1) so turns 2–5 should see cache reads near
100% on the prefix.

Run standalone for the T3 self-check:

    uv run python -m forum.jury.single_cell --stub \\
        --red modularity_hawk --blue pragmatic_defender --temperature 0.7
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from importlib import resources
from pathlib import Path
from typing import Any

from typing import TYPE_CHECKING

from .. import events as fevents
from ..cache.prompt_cache import HAIKU, PromptCache
from ..personas.loader import Persona, get
from ..types import CellVote, CodeLocation, DecisionPoint

if TYPE_CHECKING:
    from ..cache.wafer_cache import WaferCache

# Backend-agnostic cache type. Anything that exposes call_multiturn,
# extract_text, extract_tool_input, and a `metrics` attribute works.
CacheBackend = "PromptCache | WaferCache"

log = logging.getLogger("forum.jury.single_cell")


# --- Tool-use schema for the vote extraction call ---

VOTE_TOOL = {
    "name": "submit_vote",
    "description": (
        "Submit a structured vote on whether the architectural decision "
        "under debate is structural debt or justified. Use ONLY this tool; "
        "do not produce free-form text."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "position": {
                "type": "string",
                "enum": ["debt", "justified"],
                "description": (
                    "'debt' if Red made the more compelling case, "
                    "'justified' if Blue did."
                ),
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": (
                    "Decisiveness of the verdict. 0.5 is a toss-up; "
                    "0.9 is decisive."
                ),
            },
            "key_argument": {
                "type": "string",
                "description": (
                    "One sentence naming the single most decisive argument "
                    "from either side."
                ),
            },
            "value_lens": {
                "type": "object",
                "description": (
                    "For each of the six engineering values, how strongly "
                    "your conclusion is rooted in that value. 0 = not "
                    "relevant; 1 = primary driver. This is an honest "
                    "self-report — not all values must be nonzero."
                ),
                "properties": {
                    "scalability": {"type": "number", "minimum": 0, "maximum": 1},
                    "maintainability": {"type": "number", "minimum": 0, "maximum": 1},
                    "velocity": {"type": "number", "minimum": 0, "maximum": 1},
                    "correctness": {"type": "number", "minimum": 0, "maximum": 1},
                    "simplicity": {"type": "number", "minimum": 0, "maximum": 1},
                    "flexibility": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": [
                    "scalability", "maintainability", "velocity",
                    "correctness", "simplicity", "flexibility",
                ],
            },
        },
        "required": ["position", "confidence", "key_argument", "value_lens"],
    },
}


# --- Prompt rendering ---

PRINCIPLE_DEFS = {
    "P1": "Acyclic Dependencies (Robert C. Martin). The dependency graph of packages or components must be a DAG. Cycles indicate that two components cannot evolve independently and signal a missing seam.",
    "P2": "Stable Dependencies (Martin). A component should depend in the direction of stability. Instability I = Ce / (Ca + Ce). A stable component (low I) that depends on an unstable one (high I) inherits its volatility.",
    "P3": "McCabe Cyclomatic Complexity. The number of linearly independent paths through a function. CC > 15 is the conventional ceiling above which a function becomes hard to reason about and hard to test exhaustively.",
    "P4": "Cohesion (LCOM family). A class with low cohesion has methods that share few or no instance attributes — a structural sign that the class is doing more than one job. LCOM > 0.7 is the practical threshold.",
    "P5": "Reachability. Symbols that no execution path can reach are dead code. Beyond the trivial cleanliness argument, dead branches lie about the system's surface area and complicate audits.",
    "P6": "Layering. Higher-level orchestration should depend on lower-level utilities, never the other way around. Edges that travel from a deeper module back up toward the entry point break the directed nature of the architecture.",
    "P7": "Common Closure (Martin). Things that change together belong together. Files that consistently appear in the same commits across packages signal that the package boundary is mis-cut relative to the actual reason-to-change.",
}


def _load_prompt(name: str) -> str:
    pkg_root = Path(__file__).resolve().parents[3]  # repo root
    return (pkg_root / "prompts" / name).read_text(encoding="utf-8")


def _render_persona(template: str, p: Persona) -> str:
    return (
        template
        .replace("{persona_name}", p.name)
        .replace("{persona_champions}", p.champions)
        .replace("{persona_angered_by}", p.angered_by)
        .replace("{persona_pattern_match_for}",
                 "\n".join(f"- {x}" for x in p.pattern_match_for))
    )


def _format_dp(dp: DecisionPoint) -> str:
    """Render a DecisionPoint as a compact evidence block for the cached prefix."""
    locs = "\n".join(
        f"- {l.file}:{l.line_start}-{l.line_end} (module {l.module})"
        for l in dp.locations
    )
    snippets = "\n\n".join(
        f"```python\n{s}\n```" for s in dp.code_snippets[:3]
    )
    return (
        f"## Decision under review\n\n"
        f"**Principle:** {dp.principle} — {PRINCIPLE_DEFS.get(dp.principle, '')}\n\n"
        f"**Subject:** {dp.subject}\n\n"
        f"**Locations:**\n{locs}\n\n"
        f"**Measured evidence:**\n```json\n{json.dumps(dp.evidence, indent=2)}\n```\n\n"
        f"**Measured impact (structural signals, all in [0, 1]):**\n"
        f"```json\n{json.dumps(dp.measured_impact, indent=2)}\n```\n\n"
        f"**Plausible alternatives the team could pursue:**\n"
        + "\n".join(f"- {a}" for a in dp.alternatives)
        + (f"\n\n**Code snippets:**\n\n{snippets}" if snippets else "")
    )


# Static, audit-wide context bundled into the cached system prefix. The bigger
# this is, the more we benefit from cache reuse — Haiku 4.5's cache threshold
# is ~4096 tokens of CACHED prefix, so we deliberately pack the system block
# with every constant we can: principle definitions, debate rules, audit
# philosophy. None of this changes per cell or per DP, so all 50 calls in a
# tribunal hit cache on this content.
AUDIT_PREAMBLE = """\
# What this audit is

You are participating in an architectural audit of a real Python codebase.
The audit is run by Forum, a CLI tool that walks a repository, extracts
structural decisions deterministically (Layer 1), prioritizes them by a
user-supplied value vector (Layer 1.5), submits each prioritized decision
to a 10-cell debate panel (Layer 2 — that's you), synthesizes each panel
with a per-DP judge (Layer 2 — Sonnet 4.6, not you), and writes a single
markdown briefing (Layer 3 — Opus 4.7, not you).

Your role is one debater inside one cell of the 10-cell panel. Each cell
pairs two personas with different architectural values; each persona
reads the Layer 1 evidence through the value it champions and forms an
honest reading. You are not assigned a side. You don't argue "for debt"
or "for justified" by mandate — you argue what your value reads in the
specific evidence. If your value is harmed by the decision, you'll
argue it's structural debt. If your value is served by it, or is
indifferent to it, you'll argue it's justified.

Two cells with the same persona pair will reach similar substance; the
variance across the 10 cells comes from persona diversity (six personas
in pool A, six in pool B, paired on a deterministic diagonal) and a
linear temperature ramp from 0.5 at cell 0 to 0.9 at cell 9. Higher-
temperature cells take riskier positions and surface edge-case arguments;
lower-temperature cells anchor the panel on the most-defensible reading.

The cell's vote — "debt" or "justified" — emerges from the debate,
extracted at the end by a neutral observer. When both personas read the
evidence the same way, that's a stronger signal than when they disagree.

# Hard ground rules (apply to every turn)

1. Stay under the token budget specified per turn (typically 400–500
   tokens). Going over wastes panel budget and dilutes your argument;
   shorter is almost always better.
2. Cite at least one specific file path, line range, or measured metric
   in every turn. Generic claims about "this kind of pattern" are
   discounted heavily; specific citations are the unit of argument.
3. Argue strictly from the Layer 1 evidence provided in the user block.
   Do not invent facts about the codebase that the evidence does not
   establish. If the evidence is silent on something material to your
   argument, name the silence rather than fabricating around it.
4. Don't soften your reading. If your value reads this as debt, say so
   crisply. If your value reads this as justified, say so crisply. The
   cell vote is rendered by a neutral observer at the end; you give
   your honest take, the observer aggregates. A persona that hedges
   to seem balanced wastes the slot.
5. Never reference user values, team priorities, business framing,
   audience weights, product direction, or shipping goals. The panel is
   values-neutral by design. Argue the architecture. The report writer
   will frame the verdict for the audience downstream; you must not
   pre-frame it.
6. Speak in your persona's voice the whole time. Do not summarize the
   other persona's position or pretend to be neutral. Your persona
   definition (in the user-message tail of turn 1) describes what you
   champion and what angers you — read the evidence through that lens
   consistently.
7. Do not propose verdicts (HEALTHY, JUSTIFIED VIOLATION, STRUCTURAL
   DEBT, CRITICAL, DRIFTED, CONTESTED) in your debate turns. Those are
   the judge's decision after synthesizing the panel; you simply argue
   for "debt" or "justified" and let the judge weigh it.

# Principle definitions (the seven Forum currently checks)

**P1 — Acyclic Dependencies Principle (Robert C. Martin, "Clean
Architecture").** The dependency graph of packages or components must
be a directed acyclic graph. Cycles indicate that two components
cannot evolve, ship, build, test, or be extracted independently. Forum
detects cycles as strongly-connected components of size > 1 in the
module-level import graph (`networkx.strongly_connected_components`
over the result of static import analysis). Common defences: the
cycle exists for a public-API ergonomic reason (one import gets the
user everything); the cycle is purely a circular type reference that
TYPE_CHECKING guards have rendered runtime-safe; the cycle has been
bounded and audited and nobody is paying its cost.

**P2 — Stable Dependencies Principle (Martin).** A component should
depend in the direction of stability. Instability is computed as
I = Ce / (Ca + Ce), where Ca = afferent couplings (number of components
that depend on this one) and Ce = efferent couplings (number this
component depends on). I ∈ [0, 1]: low I is stable (everyone depends
on you, you depend on few); high I is unstable (you depend on many, few
depend on you). A stable component that depends on an unstable one
inherits its volatility. Forum flags edges where source I < 0.3 and
target I > 0.7. Common defences: the unstable target is a stable
boundary in practice (low actual change rate despite high theoretical
instability); the import is type-only; the alternative — inverting the
dependency — would push more complexity to more callers.

**P3 — McCabe Cyclomatic Complexity.** The number of linearly
independent paths through a function. Forum uses radon's `cc_visit`
to compute this per function. CC > 15 is the conventional ceiling
above which a function becomes hard to reason about exhaustively
and a likely source of subtle bugs in branches that lack test
coverage. Common defences: the function is a parser, dispatcher,
or pattern-matcher where the complexity is inherent to the problem
domain (compilers, type checkers, protocol parsers); the branches
are exhaustively covered by tests; decomposition would push complex
state across helpers and obscure the dispatch.

**P4 — Cohesion (LCOM family).** A class with low cohesion has methods
that share few or no instance attributes — a structural sign the
class is doing more than one job. Forum computes an LCOM1-style score
as the fraction of method pairs that share zero `self.x` attributes:
LCOM > 0.7 means most pairs are attribute-disjoint. Classes with
fewer than 4 methods are excluded as noise. Common defences: the
class is intentionally a value-object plus operators (methods touch
different fields but cohere around a single concept); the class is
a public façade composed of orthogonal capabilities that the user
benefits from accessing under one name; splitting would force the
user to learn two import paths to do one thing.

**P5 — Reachability.** Symbols that no execution path can reach are
dead code. Beyond the trivial cleanliness argument, dead branches lie
about the system's surface area, complicate audits, can mask real bugs
in tests, and slow down ramp time for new contributors. Forum uses
vulture with a confidence threshold of 80%. Common defences: the
symbol is part of a public API consumed outside the analyzed package
(a library exposing its surface); the symbol is reachable via a
dynamic dispatch vulture cannot trace (registry, plugin system, entry
point); the symbol is reachable in tests vulture did not see.

**P6 — Layering.** Higher-level orchestration should depend on
lower-level utilities, never the other way. Edges that travel from
a deeper module back up toward the entry point violate the directed
nature of the architecture. Forum assigns BFS-depth from package
entry points (top-level package modules) and flags edges where
source_depth > target_depth, excluding cyclic edges (those are
P1's territory). Common defences: the "upward" edge is to a
genuinely cross-cutting utility that the layer model doesn't capture
(logging, config, telemetry); the BFS depth metric is misaligned
with the team's mental model of layers (e.g., the entry point is
not at depth 0 in the team's diagram).

**P7 — Common Closure (Martin).** Things that change together belong
together. Files that consistently appear in the same commit across
packages signal that the package boundary is mis-cut relative to the
actual reason-to-change. Forum uses pydriller to compute co-change
frequency over a 12-month window and flags pairs that co-occur ≥ 5
times. Common defences: the co-change is driven by a shared schema
or interface migration that is now complete; the co-change reflects
two teams making coordinated changes to a known seam that was
deliberately designed for shared evolution; the time window included
a one-off refactor that is unrepresentative of steady-state.

# How to read the per-DP evidence

You will receive, in the next user message, a `<decision_point_evidence>`
block containing the principle, the subject sentence, the locations
(file:line ranges and module names), the measured evidence dict (the
raw data from Layer 1), the measured impact (structural signals
normalized to [0, 1]), the plausible alternatives the team could
pursue, and up to three code snippets. Treat every value in this
block as authoritative. Do not contradict measured metrics, do not
invent additional locations, and do not extrapolate beyond what the
evidence supports. The measured_impact dict reports five normalized
features: blast_radius (how many things are affected), recency (how
recently the violation has accreted), principle_severity (how far
past threshold), pattern_violation (how cleanly this matches the
named anti-pattern), and advocate_absence (whether there is anyone
on the current team defending the structure). Higher values lean
toward "debt"; lower values toward "justified". Use these as
priors, not as votes — your debate exists precisely because the
final reading is not deterministic from the metrics alone.

# Debate technique notes

A strong Red opening cites three things: the specific file path
where the violation lives, the measured metric value, and a concrete
downstream cost that the violation has imposed or imminently will
impose. "FastAPI's 18-module cycle in fastapi.routing makes the
routing layer impossible to extract for testing in isolation" is
specific; "this is bad" is not.

A strong opening that reads the evidence as "justified" accepts the
measured fact and contests its interpretation through your value.
"Yes, the cycle exists; from my perspective the ergonomic gain it
produces in the public API surface outweighs the principle violation,
and breaking it would impose a migration cost my value cares about"
is engagement. "The cycle does not exist" is incoherent and will lose
the cell.

Strong rebuttals address the other persona's strongest specific claim,
not their weakest. If the other persona cited a specific cost, engage
that cost (refute it through your lens, contextualize it, weigh it
against what your value sees). If they cited an ergonomic or
historical defence, engage that defence (show why your value still
reads the evidence the way it does). Rebuttals that change the
subject signal a weak case.

Closings should compress the debate into the single most decisive
fact from your side. A good closing reads like a one-paragraph
brief a senior engineer could hand to a colleague: this is what
the evidence shows, this is what it means, this is what to do.

# What a strong cell vote looks like

After the four debate turns, a neutral observer (still you, but
stepping out of the persona) renders the cell's structured vote via
the `submit_vote` tool. The vote has four required fields.
`position` is "debt" when the debate's stronger arguments (from
either persona) read this evidence as harmful or worth refactoring,
and "justified" when they read it as defensible or worth keeping.
When both personas converge on the same reading, that's a strong
signal; when they diverge, pick the side whose argument was more
specific and better-grounded in the evidence. `confidence` is in
[0.0, 1.0]: 0.5 is a toss-up, 0.7 is "I am fairly sure", 0.9 is
"the evidence is decisive". Inflated confidence is a common cell
failure mode — calibrate honestly. `key_argument` is one sentence
naming the single most decisive argument from either persona;
pick the argument a senior engineer would cite if asked to
summarize the debate in one breath. `value_lens` is the cell's
honest self-report of which of the six values (scalability,
maintainability, velocity, correctness, simplicity, flexibility)
its conclusion rests on; not every value needs to be nonzero, and
the lens should reflect what actually drove the reading, not what
you imagine the team cares about.

The value_lens is the substrate of the what-if probe: Forum can
re-project the panel under alternate value weights without
re-running the debate, scaling each cell's confidence by how much
its lens overlaps with the new weights. A cell whose key_argument
was "this complexity is a velocity tax" with value_lens.velocity =
0.9 will become much more salient if the team later weights
velocity higher. Misreporting your value_lens to make your vote
"land" is a category error — the probe relies on honest reporting,
not strategic reporting.

# What good "judge" output looks like (for context, you are not the judge)

A judge sees all 10 cells plus the original Layer-1 evidence and
renders one verdict using the six-element enum. The judge has
override authority: a panel that splits 7-debt-to-3-justified can
still receive a JUSTIFIED VIOLATION verdict if the judge concludes
the evidence supports the dissenters' reading. Judges are required
to cite at least two specific cells by ID in their reasoning, and
at least one specific piece of Layer-1 evidence. This means: cells
whose arguments are crisp, specific, and well-cited get cited;
cells whose arguments are mushy do not. Argue to be citable.

# Common cell-level failure modes

1. **Restating the principle.** "P1 says cycles are bad, therefore
   this cycle is bad" is not an argument. The principle is given;
   the panel exists to interpret the specific instance.

2. **Vague costs.** "This will be painful to maintain" without
   naming what kind of pain, where, or who pays it. Specific costs
   sound like "new contributors take 2× longer to land their first
   PR on routing.py" — verifiable in principle, even when not
   measured.

3. **Pre-framing for an audience.** "From a velocity perspective…"
   or "for a fast-moving team…" — the panel is values-neutral. The
   report writer downstream handles audience framing.

4. **Concession theater.** Red opening with "Blue has a point that…"
   gives up the slot. The cell's job is to advocate; the cell's
   job description does not include making the case for the other
   side.

5. **Generic refactoring advice.** "Just extract a base class" or
   "introduce an interface" without naming the seam, the migration
   shape, or the cost. The judge's recommended_action will be more
   concrete than your debate; your role is to surface the relevant
   facts, not to design the refactor.

# Persona archetypes you may be assigned

Six monomaniacal personas. Each one champions exactly one architectural
value and is indifferent to the other five. They are pitted against
each other in 10 hand-picked pairs where the two personas' values
naturally pull in different directions.

The six personas:

- **The Simplifier**: cares only about simplicity. Less indirection,
  fewer abstractions. Dead code, one-implementation wrappers, and
  unused configuration knobs anger them. Indifferent to whether
  removing the cleverness slows shipping or limits flexibility.

- **The Shipper**: cares only about velocity. PR cycle time and how
  fast a contributor can land a change. Refactors with high cost and
  unclear payoff anger them. Indifferent to long-term maintainability
  or scalability the system has never needed.

- **The Maintainer**: cares only about maintainability. Ramp cost for
  the next contributor, blast radius of any single change. Cleverness
  that requires tribal knowledge angers them. Indifferent to raw
  shipping speed and to minimalism for its own sake.

- **The Verifier**: cares only about correctness. Exhaustive coverage,
  defensive validation at boundaries, no implicit fall-through.
  Cyclomatic complexity in boundary code angers them. Indifferent
  to shipping speed and to whether the code is the simplest possible.

- **The Scaler**: cares only about scalability. Surviving 10× growth
  in load, team, and surface area. Coupling that prevents independent
  deployment angers them. Indifferent to minimalism and to short-term
  shipping cost.

- **The Adapter**: cares only about flexibility. Modules that can be
  lifted out cleanly when requirements change. High efferent coupling
  and stable-on-unstable dependencies anger them. Indifferent to
  raw simplicity and to current shipping speed.

Each cell pits two of these against each other (Simplifier vs Shipper,
Maintainer vs Adapter, Scaler vs Verifier, etc.). On a finding that
clearly harms BOTH personas' values, they converge on "debt." On a
finding that clearly serves BOTH personas' values, they converge on
"justified." In the contested middle — where the finding helps one
value and hurts another — they disagree, and the neutral vote-
extractor picks the side whose argument was more specific and better-
grounded in the evidence.

Your assigned persona is named in the user-message tail of turn 1
along with that persona's "champions", "angered_by", and
"pattern_match_for" fields. Speak from that perspective for the
full four turns.

# Why Forum exists (philosophy you can assume)

Forum exists because every codebase contains thousands of
structural decisions that are made over years by many engineers,
and the original rationale degrades silently as context drifts
around them. Existing AI dev tools review changes; they wait for a
PR to be opened. The implicit decisions in code that already
exists — the choices nobody is currently debating because nobody
opened a PR about them — are invisible to every other tool. But
those are the decisions that matter most. Codebases rot not
because PRs are bad, but because once-good decisions become bad
decisions silently as scale, team, and surrounding code change.
Different teams legitimately have different values: a 4-engineer
seed-stage startup should not be audited like a 200-engineer bank.
The same dependency cycle is "fine, ship it" at one and "critical
refactor" at the other. The values-neutral panel exists precisely
so that the architectural reading does not collapse into the
team's local optimum — the team's value vector enters only at
prioritization (which decisions get surfaced) and report framing
(what vocabulary the briefing uses), never at the verdict layer
where you live. Argue accordingly. Your job is to surface the
architectural truth, not to predict what the team wants to hear.
The team will read the briefing with their values lens applied
downstream; your contribution is the honest reading the lens then
projects through.
"""


def _build_system_cached(codebase_summary: str, git_summary: str) -> str:
    """Compose the cached system prefix.

    Layout (all of this becomes a single cache_control=ephemeral block):
        AUDIT_PREAMBLE (~2.5K tokens of static context — defs, rules, theory)
        <codebase_summary> … </codebase_summary>  (per-audit, ~200–500 tokens)
        <git_summary> … </git_summary>            (per-audit, ~50 tokens)

    The preamble alone pushes us over Haiku 4.5's ~4K cache minimum. Without
    it, real audits would never engage caching because per-DP context is
    too small.
    """
    return (
        f"{AUDIT_PREAMBLE}\n\n"
        f"# Codebase under audit\n\n"
        f"<codebase_summary>\n{codebase_summary}\n</codebase_summary>\n\n"
        f"<git_summary>\n{git_summary}\n</git_summary>"
    )


def _build_user_cached(dp: DecisionPoint) -> str:
    return (
        f"<decision_point_evidence>\n{_format_dp(dp)}\n</decision_point_evidence>"
    )


# --- The cell itself ---

async def run_cell(
    *,
    cell_id: int,
    decision_point: DecisionPoint,
    red_persona_id: str,
    blue_persona_id: str,
    temperature: float = 0.7,
    codebase_summary: str = "",
    git_summary: str = "",
    pc: "PromptCache | WaferCache | None" = None,
    max_turn_tokens: int = 600,
) -> CellVote:
    """Run one full debate cell (4 turns + vote) and return the structured vote."""
    pc = pc or PromptCache(model=HAIKU)
    red = get("red", red_persona_id)
    blue = get("blue", blue_persona_id)
    red_intro = _render_persona(_load_prompt("red.md"), red)
    blue_intro = _render_persona(_load_prompt("blue.md"), blue)

    system_cached = _build_system_cached(codebase_summary, git_summary)
    user_cached = _build_user_cached(decision_point)

    transcript: list[dict] = []

    # --- Turn 1: Red opens ---
    fevents.TURN_CTX.set({"turn": 0, "speaker": f"red:{red.id}", "label": "open"})
    fevents.emit("turn_start")
    msg = await pc.call_multiturn(
        system_cached=system_cached,
        user_cached_prefix=user_cached,
        turns=[{"role": "user", "text": red_intro}],
        temperature=temperature,
        max_tokens=max_turn_tokens,
    )
    red_open = pc.extract_text(msg)
    fevents.emit("turn_end", text=red_open)
    transcript.append({"role": "user", "speaker": "moderator", "text": "[Red persona opening prompt]"})
    transcript.append({"role": "assistant", "speaker": f"red:{red.id}", "text": red_open})

    # --- Turn 2: Blue responds ---
    turns_so_far = [
        {"role": "user", "text": red_intro},
        {"role": "assistant", "text": red_open},
        {"role": "user", "text": blue_intro},
    ]
    fevents.TURN_CTX.set({"turn": 1, "speaker": f"blue:{blue.id}", "label": "open"})
    fevents.emit("turn_start")
    msg = await pc.call_multiturn(
        system_cached=system_cached,
        user_cached_prefix=user_cached,
        turns=turns_so_far,
        temperature=temperature,
        max_tokens=max_turn_tokens,
    )
    blue_open = pc.extract_text(msg)
    fevents.emit("turn_end", text=blue_open)
    transcript.append({"role": "user", "speaker": "moderator", "text": "[Blue persona response prompt]"})
    transcript.append({"role": "assistant", "speaker": f"blue:{blue.id}", "text": blue_open})

    # --- Turn 3: Persona A reacts to B's reading ---
    red_rebut_prompt = (
        f"You are still **{red.name}**. The other persona has given their reading. "
        f"React to their strongest specific claim through your value's lens — "
        f"either push back on it, or acknowledge where they have a point. "
        f"Stay under 400 tokens. Cite at least one specific file path or metric."
    )
    turns_so_far += [
        {"role": "assistant", "text": blue_open},
        {"role": "user", "text": red_rebut_prompt},
    ]
    fevents.TURN_CTX.set({"turn": 2, "speaker": f"red:{red.id}", "label": "rebut"})
    fevents.emit("turn_start")
    msg = await pc.call_multiturn(
        system_cached=system_cached,
        user_cached_prefix=user_cached,
        turns=turns_so_far,
        temperature=temperature,
        max_tokens=max_turn_tokens,
    )
    red_rebut = pc.extract_text(msg)
    fevents.emit("turn_end", text=red_rebut)
    transcript.append({"role": "user", "speaker": "moderator", "text": "[Red rebuttal prompt]"})
    transcript.append({"role": "assistant", "speaker": f"red:{red.id}", "text": red_rebut})

    # --- Turn 4: Persona B closes ---
    blue_close_prompt = (
        f"You are still **{blue.name}**. Close the debate. Address the other "
        f"persona's reaction directly and give your final reading through your "
        f"value's lens. Stay under 400 tokens. Cite at least one specific file "
        f"path or metric."
    )
    turns_so_far += [
        {"role": "assistant", "text": red_rebut},
        {"role": "user", "text": blue_close_prompt},
    ]
    fevents.TURN_CTX.set({"turn": 3, "speaker": f"blue:{blue.id}", "label": "close"})
    fevents.emit("turn_start")
    msg = await pc.call_multiturn(
        system_cached=system_cached,
        user_cached_prefix=user_cached,
        turns=turns_so_far,
        temperature=temperature,
        max_tokens=max_turn_tokens,
    )
    blue_close = pc.extract_text(msg)
    fevents.emit("turn_end", text=blue_close)
    transcript.append({"role": "user", "speaker": "moderator", "text": "[Blue closing prompt]"})
    transcript.append({"role": "assistant", "speaker": f"blue:{blue.id}", "text": blue_close})

    # --- Turn 5: Vote extraction (neutral observer, tool-use only) ---
    vote_prompt = (
        "The debate is closed. Step out of any persona and act as a neutral "
        "observer. Use the `submit_vote` tool to record what the CELL "
        "concluded — do not produce any free-form text.\n\n"
        "If both personas converged on the same reading, that's the vote — "
        "with high confidence proportional to how strongly they converged. "
        "If they diverged, pick the side whose argument was more specific, "
        "better-grounded in the evidence, and more honest about its lens. "
        "For `value_lens`, report which of the six engineering values "
        "actually drove the cell's conclusion; not every value needs to "
        "be nonzero."
    )
    turns_so_far += [
        {"role": "assistant", "text": blue_close},
        {"role": "user", "text": vote_prompt},
    ]
    fevents.TURN_CTX.set({"turn": 4, "speaker": "vote", "label": "vote"})
    fevents.emit("turn_start")
    vote_msg = await pc.call_multiturn(
        system_cached=system_cached,
        user_cached_prefix=user_cached,
        turns=turns_so_far,
        temperature=0.2,  # vote should be steady; lower temperature
        # 1500 not 400: Qwen / OpenAI-style models often emit reasoning
        # text before the tool call; the small JSON of submit_vote itself
        # is ~200 tokens, so we need budget for both.
        max_tokens=1500,
        tools=[VOTE_TOOL],
        tool_choice={"type": "tool", "name": "submit_vote"},
    )
    vote_data = pc.extract_tool_input(vote_msg, "submit_vote")
    fevents.emit("turn_end", text="")
    transcript.append({"role": "user", "speaker": "moderator", "text": "[Vote extraction prompt]"})
    transcript.append({"role": "assistant", "speaker": "vote", "text": json.dumps(vote_data)})

    # Defensive normalization — Anthropic tool-use enum is advisory, not
    # enforced server-side, and Wafer/Qwen sometimes returns variants like
    # "Debt", "STRUCTURAL_DEBT", "debt.", etc. Map them to the strict literal
    # before Pydantic validates.
    raw_pos = str(vote_data.get("position", "")).strip().lower()
    raw_pos = raw_pos.replace("_", " ").rstrip(".")
    if raw_pos.startswith("just"):              # "justified", "justified violation"
        position = "justified"
    elif raw_pos.startswith("debt") or "debt" in raw_pos:
        position = "debt"
    else:
        raise RuntimeError(
            f"cell {cell_id}: unrecognized vote position "
            f"{vote_data.get('position')!r}; expected 'debt' or 'justified'."
        )

    # Fill any missing value_lens keys with 0.0 so probe.py and app.js see
    # identical 6-key dicts and salience() never silently drops a dimension.
    raw_lens = vote_data.get("value_lens", {}) or {}
    # OpenAI-compatible providers (Wafer/Qwen) sometimes JSON-stringify nested
    # objects inside tool arguments — outer parse returns a dict whose
    # value_lens field is a string instead of a dict. Decode if needed.
    if isinstance(raw_lens, str):
        try:
            raw_lens = json.loads(raw_lens)
        except json.JSONDecodeError:
            raw_lens = {}
    if not isinstance(raw_lens, dict):
        raw_lens = {}
    value_lens = {v: 0.0 for v in (
        "scalability", "maintainability", "velocity",
        "correctness", "simplicity", "flexibility",
    )}
    for k, v in raw_lens.items():
        if k in value_lens:
            try:
                value_lens[k] = float(v)
            except (TypeError, ValueError):
                pass  # keep 0.0; invalid entries shouldn't poison the cell

    # Defensive coercion for confidence — Wafer/Qwen sometimes returns it
    # as a string with whitespace or tag residue ("0.85", "0.85\n", etc.)
    import re as _re
    conf_raw = vote_data.get("confidence", 0.5)
    if isinstance(conf_raw, str):
        m = _re.search(r"[-+]?\d*\.?\d+", conf_raw)
        conf_val = float(m.group()) if m else 0.5
    else:
        try:
            conf_val = float(conf_raw)
        except (TypeError, ValueError):
            conf_val = 0.5

    return CellVote(
        cell_id=cell_id,
        red_persona=red.id,
        blue_persona=blue.id,
        position=position,
        confidence=max(0.0, min(1.0, conf_val)),
        key_argument=str(vote_data.get("key_argument", "(missing)")),
        value_lens=value_lens,
        transcript=transcript,
    )


# --- Stub DecisionPoint for standalone runs ---

def _stub_decision_point() -> DecisionPoint:
    """A realistic-feeling DP modeled on the FastAPI routing cycle."""
    return DecisionPoint(
        id="stub-p1-fastapi-cycle",
        principle="P1",
        locations=[
            CodeLocation(file="fastapi/routing.py", line_start=1, line_end=200,
                         module="fastapi.routing"),
            CodeLocation(file="fastapi/dependencies/utils.py", line_start=1, line_end=200,
                         module="fastapi.dependencies.utils"),
            CodeLocation(file="fastapi/params.py", line_start=1, line_end=120,
                         module="fastapi.params"),
            CodeLocation(file="fastapi/encoders.py", line_start=1, line_end=180,
                         module="fastapi.encoders"),
        ],
        subject="Dependency cycle across 18 modules rooted at fastapi.routing",
        evidence={
            "scc_members": [
                "fastapi.routing", "fastapi.dependencies.utils",
                "fastapi.params", "fastapi.encoders", "fastapi.applications",
                "fastapi.openapi.utils",
            ],
            "scc_size": 18,
            "cycle_edges": [
                ["fastapi.routing", "fastapi.dependencies.utils"],
                ["fastapi.dependencies.utils", "fastapi.params"],
                ["fastapi.params", "fastapi.routing"],
            ],
        },
        alternatives=[
            "Extract a shared `protocols.py` that both routing and dependencies depend on.",
            "Invert dependency: pass param-extraction callables in from routing rather than importing them.",
            "Move the cycle's shared concern into one canonical module and have everyone else depend one-way.",
        ],
        measured_impact={
            "blast_radius": 1.0,
            "principle_severity": 1.0,
            "pattern_violation": 1.0,
            "advocate_absence": 0.5,
            "recency": 0.0,
        },
        code_snippets=[
            "# fastapi/routing.py\nfrom fastapi.dependencies.utils import solve_dependencies\n# … APIRouter, APIRoute, etc.",
            "# fastapi/dependencies/utils.py\nfrom fastapi import params\nfrom fastapi.routing import APIRoute  # imported back!",
        ],
    )


# --- CLI entry: `python -m forum.jury.single_cell` ---

def _cli() -> None:
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="Run one Red/Blue debate cell.")
    parser.add_argument("--stub", action="store_true",
                        help="Use a built-in FastAPI-style DecisionPoint.")
    parser.add_argument("--evidence", type=Path,
                        help="Path to evidence.json (and --dp-id to pick one).")
    parser.add_argument("--dp-id", type=str, default=None)
    parser.add_argument("--red", required=True, help="Red persona id.")
    parser.add_argument("--blue", required=True, help="Blue persona id.")
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--cell-id", type=int, default=0)
    parser.add_argument("--codebase-summary", type=str,
                        default="Python web framework with ~50 modules: routing core, "
                                "dependency-injection, parameter parsing, OpenAPI generation, "
                                "security primitives. Several years of accumulated structural "
                                "decisions; many cross-cutting imports across the core surface.")
    parser.add_argument("--git-summary", type=str,
                        default="Active repo: hundreds of commits in the last 12 months across "
                                "dozens of contributors. Main branch is the default deploy target.")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--out", type=Path, default=None,
                        help="Write the resulting CellVote JSON here.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set. Drop it into .env and retry.",
              file=sys.stderr)
        sys.exit(2)

    if args.stub:
        dp = _stub_decision_point()
    elif args.evidence:
        data = json.loads(args.evidence.read_text())
        dps = data["decision_points"]
        if args.dp_id:
            dps = [d for d in dps if d["id"] == args.dp_id]
            if not dps:
                print(f"no decision point with id {args.dp_id!r} in {args.evidence}",
                      file=sys.stderr)
                sys.exit(2)
        dp = DecisionPoint.model_validate(dps[0])
    else:
        print("must pass --stub or --evidence <path>", file=sys.stderr)
        sys.exit(2)

    vote = asyncio.run(run_cell(
        cell_id=args.cell_id,
        decision_point=dp,
        red_persona_id=args.red,
        blue_persona_id=args.blue,
        temperature=args.temperature,
        codebase_summary=args.codebase_summary,
        git_summary=args.git_summary,
    ))

    payload = vote.model_dump()
    print(json.dumps(payload, indent=2))
    if args.out:
        args.out.write_text(json.dumps(payload, indent=2))


if __name__ == "__main__":  # pragma: no cover
    _cli()
