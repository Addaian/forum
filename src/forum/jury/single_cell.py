"""One debate cell: Red vs Blue across 4 turns + tool-use vote extraction.

Each cell is a self-contained call into Haiku 4.5:

    1. Red opens (prosecution)              ← uses prompts/red.md  + red persona
    2. Blue responds (defence)              ← uses prompts/blue.md + blue persona
    3. Red rebuts                            ← red persona, short follow-up
    4. Blue closes                           ← blue persona, short follow-up
    5. Vote extraction                       ← neutral observer, tool-use only

The full transcript and a structured CellVote are returned. Multi-turn
calls share the cached prefix (codebase summary in system, DP evidence in
user-block-1) so turns 2–5 should see cache reads near 100% on the prefix.

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

from ..cache.prompt_cache import HAIKU, PromptCache
from ..personas.loader import Persona, get
from ..types import CellVote, CodeLocation, DecisionPoint

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


def _build_system_cached(codebase_summary: str, git_summary: str) -> str:
    return (
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
    pc: PromptCache | None = None,
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
    msg = await pc.call_multiturn(
        system_cached=system_cached,
        user_cached_prefix=user_cached,
        turns=[{"role": "user", "text": red_intro}],
        temperature=temperature,
        max_tokens=max_turn_tokens,
    )
    red_open = _extract_text(msg)
    transcript.append({"role": "user", "speaker": "moderator", "text": "[Red persona opening prompt]"})
    transcript.append({"role": "assistant", "speaker": f"red:{red.id}", "text": red_open})

    # --- Turn 2: Blue responds ---
    turns_so_far = [
        {"role": "user", "text": red_intro},
        {"role": "assistant", "text": red_open},
        {"role": "user", "text": blue_intro},
    ]
    msg = await pc.call_multiturn(
        system_cached=system_cached,
        user_cached_prefix=user_cached,
        turns=turns_so_far,
        temperature=temperature,
        max_tokens=max_turn_tokens,
    )
    blue_open = _extract_text(msg)
    transcript.append({"role": "user", "speaker": "moderator", "text": "[Blue persona response prompt]"})
    transcript.append({"role": "assistant", "speaker": f"blue:{blue.id}", "text": blue_open})

    # --- Turn 3: Red rebuts ---
    red_rebut_prompt = (
        f"You are still **{red.name}**. Rebut the defence's strongest claim. "
        f"Stay under 400 tokens. Cite at least one specific file path or "
        f"metric. Do not concede ground."
    )
    turns_so_far += [
        {"role": "assistant", "text": blue_open},
        {"role": "user", "text": red_rebut_prompt},
    ]
    msg = await pc.call_multiturn(
        system_cached=system_cached,
        user_cached_prefix=user_cached,
        turns=turns_so_far,
        temperature=temperature,
        max_tokens=max_turn_tokens,
    )
    red_rebut = _extract_text(msg)
    transcript.append({"role": "user", "speaker": "moderator", "text": "[Red rebuttal prompt]"})
    transcript.append({"role": "assistant", "speaker": f"red:{red.id}", "text": red_rebut})

    # --- Turn 4: Blue closes ---
    blue_close_prompt = (
        f"You are still **{blue.name}**. Close the debate. Address Red's "
        f"rebuttal directly and give your strongest final position. Stay "
        f"under 400 tokens. Cite at least one specific file path or metric."
    )
    turns_so_far += [
        {"role": "assistant", "text": red_rebut},
        {"role": "user", "text": blue_close_prompt},
    ]
    msg = await pc.call_multiturn(
        system_cached=system_cached,
        user_cached_prefix=user_cached,
        turns=turns_so_far,
        temperature=temperature,
        max_tokens=max_turn_tokens,
    )
    blue_close = _extract_text(msg)
    transcript.append({"role": "user", "speaker": "moderator", "text": "[Blue closing prompt]"})
    transcript.append({"role": "assistant", "speaker": f"blue:{blue.id}", "text": blue_close})

    # --- Turn 5: Vote extraction (neutral observer, tool-use only) ---
    vote_prompt = (
        "The debate is closed. Step out of any persona and act as a "
        "neutral observer. Use the `submit_vote` tool to record your "
        "verdict — do not produce any free-form text.\n\n"
        "Render the verdict on the strength of the arguments and the "
        "evidence, not on persuasive style. For `value_lens`, report "
        "honestly which of the six engineering values your conclusion "
        "actually rests on; some may be near zero."
    )
    turns_so_far += [
        {"role": "assistant", "text": blue_close},
        {"role": "user", "text": vote_prompt},
    ]
    vote_msg = await pc.call_multiturn(
        system_cached=system_cached,
        user_cached_prefix=user_cached,
        turns=turns_so_far,
        temperature=0.2,  # vote should be steady; lower temperature
        max_tokens=400,
        tools=[VOTE_TOOL],
        tool_choice={"type": "tool", "name": "submit_vote"},
    )
    vote_data = _extract_tool_input(vote_msg, "submit_vote")
    transcript.append({"role": "user", "speaker": "moderator", "text": "[Vote extraction prompt]"})
    transcript.append({"role": "assistant", "speaker": "vote", "text": json.dumps(vote_data)})

    return CellVote(
        cell_id=cell_id,
        red_persona=red.id,
        blue_persona=blue.id,
        position=vote_data["position"],
        confidence=float(vote_data["confidence"]),
        key_argument=vote_data["key_argument"],
        value_lens={k: float(v) for k, v in vote_data["value_lens"].items()},
        transcript=transcript,
    )


# --- Anthropic Message helpers ---

def _extract_text(msg: Any) -> str:
    parts = []
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def _extract_tool_input(msg: Any, tool_name: str) -> dict:
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            return dict(block.input)
    raise RuntimeError(f"model did not call tool {tool_name!r}; "
                       f"stop_reason={getattr(msg, 'stop_reason', '?')}")


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
