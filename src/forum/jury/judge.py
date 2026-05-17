"""Per-decision-point judge synthesis.

One Sonnet 4.6 call per DecisionPoint takes the panel's 10 cell transcripts
+ votes + the original Layer 1 evidence and renders a single verdict via
tool-use. The judge has override authority: a unanimous "debt" vote can be
overruled if Layer 1 evidence clearly supports a JUSTIFIED VIOLATION.

Run standalone for the T4 self-check:

    uv run python -m forum.jury.judge --stub
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from ..cache.prompt_cache import SONNET, PromptCache
from ..types import CellVote, DecisionPoint
from .pairings import cell_temperature, pair_for
from .single_cell import PRINCIPLE_DEFS, _format_dp, _load_prompt

log = logging.getLogger("forum.jury.judge")


# --- Tool-use schema for the verdict ---

VERDICT_VALUES = [
    "HEALTHY", "JUSTIFIED VIOLATION", "STRUCTURAL DEBT",
    "CRITICAL", "DRIFTED", "CONTESTED",
]

JUDGE_TOOL = {
    "name": "submit_verdict",
    "description": "Submit the synthesized verdict for this decision point.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": VERDICT_VALUES,
                "description": "Exactly one of the six allowed verdicts.",
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "2-3 bullet points. Format EXACTLY: '• <bullet text>\\n• "
                    "<next bullet>'. Each bullet ≤15 words, starts with a "
                    "verb or noun, no preamble. Collectively cite ≥2 cells "
                    "by numeric ID (e.g., 'cell 3', 'cell 7') and ≥1 piece "
                    "of Layer 1 evidence (file path, line, or metric). "
                    "Example: '• Cell 3 and cell 7 cite a 15-edge cycle in "
                    "auth/.\\n• Layer 1 metric: SCC size 12.\\n• Cycle "
                    "blocks isolated extraction of auth.session.'"
                ),
            },
            "dissent_summary": {
                "type": "string",
                "description": (
                    "1-2 bullet points naming the strongest losing argument(s). "
                    "Format: '• <bullet>\\n• <next>'. Each ≤15 words. "
                    "Required even if you agree with the majority. State the "
                    "claim, don't contrast it."
                ),
            },
            "recommended_action": {
                "type": "string",
                "description": (
                    "1-3 bullet points. Format: '• <bullet>\\n• <next>'. "
                    "Each bullet starts with a verb. Name WHAT, WHERE "
                    "(file/module), and HOW LARGE (e.g., '~50 LOC', "
                    "'one PR'). Example: '• Extract fastapi/protocols.py "
                    "(~80 LOC).\\n• Make routing and dependencies depend on "
                    "it one-way.\\n• Delete the back-edges from "
                    "params.py.' 'Refactor this' is unacceptable."
                ),
            },
            "override": {
                "type": "boolean",
                "description": (
                    "True if the verdict disagrees with the panel's "
                    "majority position."
                ),
            },
        },
        "required": [
            "verdict", "reasoning", "dissent_summary",
            "recommended_action", "override",
        ],
    },
}


# --- Briefing assembly ---

def _format_cell_summary(c: CellVote) -> str:
    pair_red, pair_blue = pair_for(c.cell_id)
    # If the cell was constructed outside the deterministic pairing path,
    # respect the stored personas rather than the pairing default.
    red = c.red_persona or pair_red
    blue = c.blue_persona or pair_blue
    temp = cell_temperature(c.cell_id)
    lens = ", ".join(f"{k}={v:.2f}" for k, v in sorted(c.value_lens.items()))
    return (
        f"### Cell {c.cell_id} — Red: {red} vs Blue: {blue} (T≈{temp})\n"
        f"- Vote: **{c.position.upper()}** (confidence {c.confidence:.2f})\n"
        f"- Key argument: {c.key_argument}\n"
        f"- Value lens: {lens}"
    )


def _format_cell_transcript(c: CellVote) -> str:
    """Render one cell's debate transcript. Skips moderator scaffolding."""
    lines = [f"### Cell {c.cell_id} transcript"]
    for turn in c.transcript:
        speaker = turn.get("speaker", turn.get("role", "?"))
        if speaker in ("moderator", "vote"):
            continue
        text = turn.get("text", "")
        lines.append(f"**{speaker}:** {text}")
    return "\n\n".join(lines)


def _build_briefing(dp: DecisionPoint, cells: list[CellVote]) -> str:
    n_debt = sum(1 for c in cells if c.position == "debt")
    n_just = len(cells) - n_debt
    head = (
        f"# Layer 1 evidence\n\n{_format_dp(dp)}\n\n"
        f"# Panel composition\n\n"
        f"{len(cells)} cells deliberated. Tally: **{n_debt} debt** / "
        f"**{n_just} justified**.\n\n"
        f"# Per-cell summaries\n\n"
    )
    summaries = "\n\n".join(_format_cell_summary(c) for c in cells)
    transcripts = "\n\n".join(_format_cell_transcript(c) for c in cells)
    return (
        head
        + summaries
        + "\n\n# Full transcripts\n\n"
        + transcripts
        + "\n\n# Now render the verdict\n\n"
        "Synthesize the deliberation into one verdict using the "
        "`submit_verdict` tool. Follow every hard rule in the system "
        "prompt. Cite at least two cells by ID and at least one piece "
        "of Layer 1 evidence in your reasoning."
    )


# --- The judge call ---

async def run_judge(
    *,
    decision_point: DecisionPoint,
    cells: list[CellVote],
    pc: PromptCache | None = None,
    model: str = SONNET,
    max_tokens: int = 600,
    temperature: float = 0.3,
) -> dict:
    """Synthesize one panel into one verdict. Returns a dict ready to drop
    into TribunalResult.judge."""
    pc = pc or PromptCache(model=model)
    if not cells:
        raise ValueError("judge needs at least one cell vote")

    system = _load_prompt("judge.md")
    briefing = _build_briefing(decision_point, cells)

    msg = await pc.call_raw(
        system=system,
        messages=[{"role": "user", "content": briefing}],
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        tools=[JUDGE_TOOL],
        tool_choice={"type": "tool", "name": "submit_verdict"},
    )

    verdict = _extract_tool_input(msg, "submit_verdict")
    # Sonnet sometimes emits "STRUCTURAL_DEBT" or "Structural-Debt" instead of
    # the canonical "STRUCTURAL DEBT" — Anthropic's tool-use enum is advisory,
    # not enforced server-side. Normalize underscores, hyphens, repeated whitespace,
    # and case before validating.
    import re as _re
    raw = verdict["verdict"]
    normalized = _re.sub(r"[\s_\-]+", " ", str(raw)).strip().upper()
    if normalized not in VERDICT_VALUES:
        raise ValueError(
            f"judge returned out-of-enum verdict: {raw!r} "
            f"(normalized: {normalized!r}). Allowed: {VERDICT_VALUES}"
        )
    verdict["verdict"] = normalized
    verdict["model"] = model
    verdict["panel_size"] = len(cells)
    return verdict


def _extract_tool_input(msg: Any, tool_name: str) -> dict:
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            return dict(block.input)
    raise RuntimeError(
        f"judge did not call {tool_name!r}; "
        f"stop_reason={getattr(msg, 'stop_reason', '?')}"
    )


# --- Stub cells for standalone testing ---

def _stub_cells(n: int = 10) -> list[CellVote]:
    """Build a plausible panel: 6 debt votes (varying confidence), 4 justified.
    Each cell has a different key_argument so the judge has real material to
    cite by ID."""
    args_debt = [
        "The 18-module SCC rooted at fastapi.routing blocks independent module extraction.",
        "Cycle members co-change in 5+ commits over the last 12 months, signalling tight coupling.",
        "The cycle prevents shipping `fastapi.dependencies` as a standalone package.",
        "Layer 1 shows 110 internal edges in a 48-node graph — the seam is missing.",
        "fastapi.routing depends on fastapi.params which depends back on fastapi.routing.",
        "Even partial extraction would require breaking the encoder→routing back-edge.",
    ]
    args_just = [
        "FastAPI's public API is one import; the cycle exists to preserve that ergonomic.",
        "The cycle is stable for 2+ years and no incident has been filed against it.",
        "Migration cost to break the cycle (rewrite of dependency injection) outweighs benefit.",
        "Cycle members all live within one deployable unit — the principle is theoretical here.",
    ]
    cells = []
    for i in range(n):
        if i < 6:
            cells.append(CellVote(
                cell_id=i,
                red_persona=pair_for(i)[0],
                blue_persona=pair_for(i)[1],
                position="debt",
                confidence=0.75 + 0.04 * (i % 3),
                key_argument=args_debt[i % len(args_debt)],
                value_lens={
                    "scalability": 0.6, "maintainability": 0.8,
                    "velocity": 0.2, "correctness": 0.3,
                    "simplicity": 0.5, "flexibility": 0.4,
                },
                transcript=[
                    {"role": "assistant", "speaker": f"red:{pair_for(i)[0]}",
                     "text": f"[stub red opening for cell {i}] {args_debt[i % len(args_debt)]}"},
                    {"role": "assistant", "speaker": f"blue:{pair_for(i)[1]}",
                     "text": f"[stub blue response for cell {i}] But the cycle has not caused a documented incident."},
                ],
            ))
        else:
            cells.append(CellVote(
                cell_id=i,
                red_persona=pair_for(i)[0],
                blue_persona=pair_for(i)[1],
                position="justified",
                confidence=0.7 + 0.03 * (i % 4),
                key_argument=args_just[(i - 6) % len(args_just)],
                value_lens={
                    "scalability": 0.2, "maintainability": 0.3,
                    "velocity": 0.7, "correctness": 0.3,
                    "simplicity": 0.4, "flexibility": 0.3,
                },
                transcript=[
                    {"role": "assistant", "speaker": f"red:{pair_for(i)[0]}",
                     "text": f"[stub red opening for cell {i}] This cycle is debt."},
                    {"role": "assistant", "speaker": f"blue:{pair_for(i)[1]}",
                     "text": f"[stub blue response for cell {i}] {args_just[(i - 6) % len(args_just)]}"},
                ],
            ))
    return cells


# --- CLI: `python -m forum.jury.judge --stub` ---

def _cli() -> None:
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="Run the per-DP judge.")
    parser.add_argument("--stub", action="store_true",
                        help="Stub a 6/4 debt/justified panel against the FastAPI cycle.")
    parser.add_argument("--verdicts", type=Path,
                        help="Path to a verdicts.json fragment (one TribunalResult-shaped dict).")
    parser.add_argument("--model", default=SONNET)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--verbose", action="store_true")
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
        from .single_cell import _stub_decision_point
        dp = _stub_decision_point()
        cells = _stub_cells(10)
    elif args.verdicts:
        data = json.loads(args.verdicts.read_text())
        dp = DecisionPoint.model_validate(data["decision_point"])
        cells = [CellVote.model_validate(c) for c in data["cells"]]
    else:
        print("must pass --stub or --verdicts <path>", file=sys.stderr)
        sys.exit(2)

    pc = PromptCache(model=args.model)
    verdict = asyncio.run(run_judge(
        decision_point=dp, cells=cells, pc=pc, model=args.model,
    ))

    print(json.dumps(verdict, indent=2))

    # Achievement checks the user can eyeball
    reasoning = verdict.get("reasoning", "")
    # Use \b boundaries — otherwise "cell 1" matches inside "cell 10/11/…".
    import re as _re
    n_cell_refs = sum(
        1 for i in range(len(cells))
        if _re.search(rf"\bcell {i}\b", reasoning.lower())
    )
    rec = verdict.get("recommended_action", "")
    print(f"\n--- achievement checks ---")
    print(f"  reasoning cites {n_cell_refs} cell ids (target ≥ 2)")
    print(f"  recommended_action length: {len(rec)} chars "
          f"({'pass' if len(rec) > 40 and rec.strip().lower() != 'refactor this' else 'check manually'})")
    print(f"  verdict in enum: {verdict['verdict'] in VERDICT_VALUES}")
    print(f"  override flag set: {verdict.get('override')}")

    s = pc.metrics.summary()
    print(f"\ncost: ${s['total_cost_usd']:.4f}  latency: {s['avg_latency_s']:.2f}s")

    if args.out:
        args.out.write_text(json.dumps(verdict, indent=2))


if __name__ == "__main__":  # pragma: no cover
    _cli()
