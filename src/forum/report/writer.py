"""Layer 3 — single Opus 4.7 call writes the markdown briefing.

Inputs: the EvidenceBundle from Layer 1, the prioritized top-N from
Layer 1.5, the verdicts from Layer 2 (TribunalResult-shaped dicts with
populated `judge`), and the user value vector. Output: one markdown
file (~1500–2000 words) and a ReportArtifact.

This module composes the Opus call. The values-aware framing happens
entirely in `prompts/report.md` and in the user message we build here.
Verdicts pass through literally — Rule 2 of the values-lens discipline.

Run standalone from a fully-populated audit cache:

    uv run python -m forum.report.writer \\
        --audit-dir ./audits/<hash> \\
        --values demo-values.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from ..cache.prompt_cache import OPUS, PromptCache
from ..types import EvidenceBundle, ReportArtifact
from ..values.loader import load_values

log = logging.getLogger("forum.report.writer")


# --- Prompt assembly ---

def _load_system_prompt() -> str:
    # parents[3] only works in the source checkout (forum/report/writer.py ->
    # forum/ -> src/ -> repo root). Try the source layout first and fall back
    # to a sibling 'prompts' dir for packaged installs.
    candidates = [
        Path(__file__).resolve().parents[3] / "prompts" / "report.md",
        Path(__file__).resolve().parents[2] / "prompts" / "report.md",
        Path(__file__).resolve().parent / "prompts" / "report.md",
    ]
    for p in candidates:
        if p.exists():
            return p.read_text(encoding="utf-8")
    raise FileNotFoundError(
        "Could not find prompts/report.md in any expected location: "
        + ", ".join(str(c) for c in candidates)
    )


def _format_value_vector(values: dict[str, float]) -> str:
    ordered = sorted(values.items(), key=lambda kv: -kv[1])
    lines = [f"- **{k}**: {v:.2f}" for k, v in ordered]
    top = ", ".join(f"{k} ({v:.2f})" for k, v in ordered[:2])
    return "Team value weights (descending):\n" + "\n".join(lines) + (
        f"\n\nTop-weighted values: **{top}**."
    )


def _f(value: Any, fmt: str = ".3f", default: float = 0.0) -> str:
    """Format a number tolerantly — coerce None/missing/non-numeric to default."""
    try:
        return format(float(value if value is not None else default), fmt)
    except (TypeError, ValueError):
        return format(default, fmt)


def _escape_md(text: str) -> str:
    """Escape markdown-significant characters in untrusted/LLM-generated text.

    Prevents accidental bold/italic/header/fence injection from judge fields
    that contain `*`, `_`, backticks, or leading `#`.
    """
    if not text:
        return text
    # Escape backticks first so we don't double-escape backslashes we add.
    out = text.replace("\\", "\\\\")
    for ch in ("`", "*", "_"):
        out = out.replace(ch, "\\" + ch)
    return out


def _safe_fence(snippet: str, limit: int = 1500) -> str:
    """Wrap a snippet in a markdown fence whose backtick count exceeds the
    longest run of backticks inside the snippet, so embedded ``` cannot break
    the rendering."""
    body = snippet[:limit]
    longest = 0
    run = 0
    for ch in body:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    fence = "`" * max(3, longest + 1)
    return f"{fence}python\n{body}\n{fence}"


def _format_decision_section(rank: int, prio_row: dict, dp_dict: dict,
                             tribunal: dict) -> str:
    """One decision point's chunk of the briefing input."""
    judge = tribunal.get("judge") or {}
    agg = tribunal.get("aggregate_vote") or {}

    locs_str = "\n".join(
        f"  - `{l['file']}`:{l['line_start']}-{l['line_end']} "
        f"(module `{l['module']}`)"
        for l in dp_dict.get("locations", [])
    )
    alts_str = "\n".join(f"  - {a}" for a in dp_dict.get("alternatives", []))
    snippets = dp_dict.get("code_snippets") or []
    snippets_str = ""
    if snippets:
        snippets_str = "\n\n**Code snippets (truncated):**\n\n" + "\n\n".join(
            _safe_fence(s) for s in snippets[:2]
        )

    return f"""## #{rank} — {dp_dict.get("subject", "(no subject)")}

**Decision point id:** `{dp_dict.get("id")}`
**Principle:** {dp_dict.get("principle")}
**Prioritization:** rank={rank}, structural={_f(prio_row.get('structural_score'))}, value_affinity={_f(prio_row.get('value_affinity_score'), '+.3f')}, composite={_f(prio_row.get('composite_score'))}

**Locations:**
{locs_str or '  (none)'}

**Measured Layer-1 evidence:**
```json
{json.dumps(dp_dict.get("evidence", {}), indent=2)}
```

**Measured structural impact (signals in [0, 1]):**
```json
{json.dumps(dp_dict.get("measured_impact", {}), indent=2)}
```

**Plausible alternatives the team could pursue:**
{alts_str or '  (none provided)'}
{snippets_str}

**Aggregate panel vote:** {json.dumps(agg, indent=2)}

**Judge verdict (preserve literally — do not modify):** **{_escape_md(str(judge.get("verdict", "(missing)")))}**
**Override flag:** {judge.get("override", False)}

**Judge reasoning (you may rephrase for flow, must not change the substance):**
{_escape_md(str(judge.get("reasoning", "(missing)")))}

**Strongest dissent (surface as a caveat in the section):**
{_escape_md(str(judge.get("dissent_summary", "(missing)")))}

**Recommended action (rephrase for flow; specificity must survive):**
{_escape_md(str(judge.get("recommended_action", "(missing)")))}
"""


def _build_user_message(values: dict[str, float],
                        prioritized: list[dict],
                        dp_by_id: dict[str, dict],
                        verdicts_by_id: dict[str, dict]) -> str:
    sections: list[str] = []
    sections.append("# Team value vector\n\n" + _format_value_vector(values))
    sections.append(
        f"\n\n# Decision points to brief on ({len(prioritized)} total)\n\n"
        "The decision points appear here in **structural priority order** "
        "from Layer 1.5. **You** are free to reorder the sections you "
        "write according to value-aligned recommended actions — that "
        "reordering is required by Rule 3."
    )
    for rank, row in enumerate(prioritized, start=1):
        dp = dp_by_id.get(row["decision_point_id"])
        tr = verdicts_by_id.get(row["decision_point_id"]) or {}
        if dp is None:
            continue
        sections.append(_format_decision_section(rank, row, dp, tr))
    sections.append(
        "\n\n# Now write the briefing\n\n"
        "Produce the markdown briefing per the system prompt. Word count "
        "1500–2000. Verdicts preserved literally. Open with the decision "
        "whose verdict and recommended_action most align with the team's "
        "top-weighted values."
    )
    return "\n\n".join(sections)


# --- The writer call ---

def _extract_text(msg: Any) -> str:
    parts = []
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def _extract_headline(markdown: str) -> str:
    for line in markdown.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
        if s and not s.startswith("```"):
            return s.lstrip("#").strip()
    return "(no headline)"


def _word_count(text: str) -> int:
    return len(re.findall(r"\w+", text))


async def write_report(
    *,
    bundle: EvidenceBundle,
    prioritized: list[dict],
    verdicts: list[dict],
    user_values: dict[str, float],
    pc: PromptCache | None = None,
    model: str = OPUS,
    max_tokens: int = 4000,
    temperature: float = 0.6,
) -> ReportArtifact:
    """Single Opus call. Returns ReportArtifact; caller persists `markdown` to disk."""
    pc = pc or PromptCache(model=model)

    dp_by_id = {dp.id: dp.model_dump() for dp in bundle.decision_points}
    verdicts_by_id = {v["decision_point_id"]: v for v in verdicts}

    system = _load_system_prompt()
    user = _build_user_message(user_values, prioritized, dp_by_id, verdicts_by_id)

    log.info("Calling Opus for report (system=%d chars, user=%d chars)",
             len(system), len(user))

    msg = await pc.call_raw(
        system=system,
        messages=[{"role": "user", "content": user}],
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    markdown = _extract_text(msg)
    if not markdown:
        raise RuntimeError(
            f"Opus returned no text content; stop_reason="
            f"{getattr(msg, 'stop_reason', '?')}"
        )

    rec = pc.metrics.calls[-1]
    wc = _word_count(markdown)
    verdict_counts: dict[str, int] = {}
    for v in verdicts:
        j = (v.get("judge") or {}).get("verdict")
        if j:
            verdict_counts[j] = verdict_counts.get(j, 0) + 1

    return ReportArtifact(
        markdown=markdown,
        headline=_extract_headline(markdown),
        stats={
            "word_count": wc,
            "input_tokens": rec.input_tokens,
            "output_tokens": rec.output_tokens,
            "cache_creation_tokens": rec.cache_creation_input_tokens,
            "cache_read_tokens": rec.cache_read_input_tokens,
            "cost_usd": round(rec.cost_usd, 5),
            "latency_s": round(rec.latency_s, 3),
            "model": rec.model,
            "verdict_distribution": verdict_counts,
            "num_decision_points": len(prioritized),
        },
    )


# --- CLI: `python -m forum.report.writer` ---

def _cli() -> None:
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="Write the Layer-3 markdown briefing.")
    parser.add_argument("--audit-dir", type=Path, required=True,
                        help="Cache dir containing evidence.json, prioritized.json, verdicts.json.")
    parser.add_argument("--values", type=Path, default=None,
                        help="YAML file of value weights (default: all 1.0).")
    parser.add_argument("--value", action="append", default=[],
                        help="Single value override, e.g. --value velocity=1.8 (repeatable).")
    parser.add_argument("--model", default=OPUS)
    parser.add_argument("--out", type=Path, default=None,
                        help="Override the default <audit-dir>/report.md path.")
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

    bundle_path = args.audit_dir / "evidence.json"
    prio_path = args.audit_dir / "prioritized.json"
    verdicts_path = args.audit_dir / "verdicts.json"
    for p in (bundle_path, prio_path, verdicts_path):
        if not p.exists():
            print(f"missing required artifact: {p}", file=sys.stderr)
            sys.exit(2)

    bundle = EvidenceBundle.model_validate_json(bundle_path.read_text(encoding="utf-8"))
    prio_data = json.loads(prio_path.read_text(encoding="utf-8"))
    prioritized = prio_data["items"]
    verdicts = json.loads(verdicts_path.read_text(encoding="utf-8"))
    weights = load_values(args.values, tuple(args.value))

    pc = PromptCache(model=args.model)
    artifact = asyncio.run(write_report(
        bundle=bundle,
        prioritized=prioritized,
        verdicts=verdicts,
        user_values=weights,
        pc=pc,
        model=args.model,
    ))

    out_path = args.out or (args.audit_dir / "report.md")
    out_path.write_text(artifact.markdown, encoding="utf-8")

    print(artifact.markdown)
    print("\n---", file=sys.stderr)
    print(f"headline: {artifact.headline}", file=sys.stderr)
    print(f"stats: {json.dumps(artifact.stats, indent=2)}", file=sys.stderr)
    print(f"written: {out_path}", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    _cli()
