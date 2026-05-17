"""Forum CLI entry point."""
from __future__ import annotations

import hashlib
import logging
import subprocess
import sys
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler

console = Console(stderr=True)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True,
                              show_path=False, markup=True)],
    )


def _audit_hash(repo_path: Path, commit_sha: str) -> str:
    """Stable 12-char hash for the audit dir."""
    h = hashlib.sha256(f"{repo_path.resolve()}:{commit_sha}".encode()).hexdigest()
    return h[:12]


def _commit_sha(repo_path: Path) -> str:
    if not (repo_path / ".git").exists():
        return "no-git"
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_path, text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


@click.group()
@click.version_option()
def main() -> None:
    """Forum — an AI architectural audit for Python codebases."""
    load_dotenv()
    # Activates per-token streaming via stdout-prefixed JSON when the FastAPI
    # server (or any caller) sets FORUM_EVENTS=1. No-op otherwise.
    from . import events as fevents
    fevents.install_stdout_emitter_if_requested()


@main.command()
@click.argument("repo", type=click.Path(file_okay=False, path_type=Path), required=False)
@click.option("--values", "values_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="YAML file of value weights.")
@click.option("--value", "value_overrides", multiple=True,
              help="Single value override, e.g. --value velocity=1.8")
@click.option("--top-n", "top_n", type=int, default=0,
              help="Number of decision points to surface in Layer 1.5. "
                   "0 (default) audits every finding; pass a positive int to cap.")
@click.option("--cache", "cache_dir", type=click.Path(path_type=Path),
              default=Path("./audits"), help="Where to write audit artifacts.")
@click.option("--skip-jury", is_flag=True, default=False,
              help="Skip Layer 2 (jury deliberation).")
@click.option("--skip-report", is_flag=True, default=False,
              help="Skip Layer 3 (markdown report).")
@click.option("--only", "only_checkers", default=None,
              help="Comma-separated principle IDs to run (e.g. P1,P3).")
@click.option("--language", "language", default=None,
              type=click.Choice(["python", "c", "auto"]),
              help="Source language. 'auto' (default) picks by file extension. "
              "P4 (LCOM) is skipped on C; P5 (dead code) requires cppcheck on C.")
@click.option("--cell-backend", "cell_backend",
              type=click.Choice(["anthropic", "wafer"]), default="anthropic",
              help="Inference backend for Layer-2 cells. 'anthropic' uses "
              "Haiku 4.5 with KV-cache reuse; 'wafer' routes the 50 cells "
              "through Wafer's Qwen3.5-397B-A17B (no prompt caching). "
              "Judge and report always stay on Anthropic.")
@click.option("--replay", "replay_dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Replay a cached audit (no API calls). "
              "Mutually exclusive with REPO.")
@click.option("--verbose", is_flag=True, default=False)
def audit(repo: Path | None, values_path: Path | None,
          value_overrides: tuple[str, ...], top_n: int, cache_dir: Path,
          skip_jury: bool, skip_report: bool, only_checkers: str | None,
          language: str | None, cell_backend: str,
          replay_dir: Path | None, verbose: bool) -> None:
    """Audit a Python repository and produce a markdown briefing.

    With --replay, re-emits a previously-cached audit (no API calls); used
    as the demo backstop when WiFi is iffy or budget is tight.
    """
    _setup_logging(verbose)

    # Replay path — completely separate; never touches the SDK.
    if replay_dir is not None:
        if repo is not None:
            console.print("[red]--replay is mutually exclusive with REPO.[/]")
            sys.exit(2)
        _replay_audit(replay_dir.resolve())
        return

    if repo is None:
        console.print("[red]Must supply REPO (or use --replay <audit-dir>).[/]")
        sys.exit(2)
    if not repo.exists() or not repo.is_dir():
        console.print(f"[red]REPO does not exist or is not a directory: {repo}[/]")
        sys.exit(2)

    log = logging.getLogger("forum")

    import time as _time
    audit_t0 = _time.perf_counter()

    repo = repo.resolve()
    sha = _commit_sha(repo)
    audit_dir = (cache_dir / _audit_hash(repo, sha)).resolve()
    audit_dir.mkdir(parents=True, exist_ok=True)

    console.rule(f"[bold]forum audit[/] {repo.name} @ {sha[:8]}")
    console.print(f"Cache: [cyan]{audit_dir}[/]")

    # --- Layer 1 ---
    from ._polish import phase
    from .evidence.runner import run as run_evidence
    want = None
    if only_checkers:
        want = {p.strip().upper() for p in only_checkers.split(",")}
    lang_arg = None if (language in (None, "auto")) else language
    with phase(console, "Layer 1: deterministic evidence extraction"):
        bundle = run_evidence(repo, audit_dir, run_checkers=want, language=lang_arg)
    console.print(f"[green]Layer 1[/]: {len(bundle.decision_points)} decision points "
                  f"across {len({d.principle for d in bundle.decision_points})} principles")

    # Pre-flight: if Layer 1 found nothing, abort BEFORE spending on jury +
    # report. Spares the user a useless ~$0.40 on a repo Forum can't see.
    num_modules = (bundle.graph_summary or {}).get("num_modules", 0)
    if len(bundle.decision_points) == 0 or num_modules == 0:
        console.print(
            f"[red]Layer 1 found nothing to audit[/] — "
            f"{num_modules} modules indexed, {len(bundle.decision_points)} findings."
        )
        console.print(
            "[dim]Common causes: non-Python/non-C repo, or all source under a "
            "skipped directory (tests/, docs/, scripts/, examples/, build/, vendor/, …). "
            "Aborting without spending on jury or report.[/]"
        )
        sys.exit(3)  # distinct exit code so the server can recognize empty-audit

    # --- Layer 1.5 ---
    from .prioritize.score import rank, write_prioritized
    from .values.loader import load_affinities, load_values
    weights = load_values(values_path, value_overrides)
    affinities = load_affinities()
    ranked = rank(bundle, weights, affinities, top_n=top_n)
    prioritized_path = write_prioritized(audit_dir, ranked, weights)
    console.print(f"[green]Layer 1.5[/]: top {len(ranked)} → [cyan]{prioritized_path}[/]")
    for r in ranked:
        console.print(
            f"  [bold]#{r['rank']}[/] {r['principle']} "
            f"(comp={r['composite_score']:.3f}, struct={r['structural_score']:.3f}, "
            f"val={r['value_affinity_score']:+.2f}) — {r['subject']}"
        )

    if skip_jury and skip_report:
        return

    # --- Layer 2: jury + judge ---
    if not skip_jury:
        import asyncio
        import json
        import os

        from ._polish import verdict_markup
        from .cache.prompt_cache import HAIKU, PromptCache
        from .jury.judge import run_judge
        from .jury.speculative import run_tribunal_speculative

        # Pre-flight: catch missing API keys before we instantiate clients,
        # so the user gets a friendly Click error instead of a stack trace.
        if cell_backend == "wafer" and not os.environ.get("WAFER_API_KEY"):
            console.print(
                "[red]WAFER_API_KEY is not set.[/] Drop it into .env or "
                "drop --cell-backend wafer."
            )
            sys.exit(2)
        if not os.environ.get("ANTHROPIC_API_KEY"):
            console.print(
                "[red]ANTHROPIC_API_KEY is not set.[/] The judge (Sonnet) "
                "needs it even when cells go to Wafer."
            )
            sys.exit(2)

        # Cell backend: Anthropic Haiku (cached) or Wafer Qwen3.5 (no cache).
        # The judge always stays on Anthropic Sonnet — we reuse `judge_pc`
        # below — because verdict synthesis is the hot quality bar.
        if cell_backend == "wafer":
            from .cache.wafer_cache import QWEN3, WaferCache
            cell_pc = WaferCache(model=QWEN3)
            console.print(f"[magenta]Cells →[/] Wafer ({QWEN3})  "
                          f"[dim](judge + report stay on Anthropic)[/]")
        else:
            cell_pc = PromptCache(model=HAIKU)
            console.print(f"[magenta]Cells →[/] Anthropic ({HAIKU})")
        # Judge runs on Sonnet. Anthropic Tier-1 caps Sonnet at 30K input
        # tokens/min and each judge call sends ~5-10K input tokens (cells
        # transcripts + evidence). Throttle hard so a 19-tribunal fanout
        # doesn't 429 mid-flight: 1 in-flight call, 15s spacing → ~4 calls/min,
        # comfortably under the cap.
        # Judge runs on Sonnet. Anthropic Tier-1 caps Sonnet at 30K input
        # tokens/min and each judge call sends ~5-10K input tokens. The
        # 2-concurrent × 8s spacing gives us ~15 calls/min in the best case
        # (~75-150K TPM) which would actually exceed the cap on long-cell
        # cases, but the actual call latency (~3-5s) means in-flight count
        # stays at 1-2 most of the time. Throughput vs safety tradeoff
        # tuned to ~3 min for 19-finding judge stage instead of 5 min.
        judge_pc = PromptCache(max_concurrent=2, min_interval_s=8.0)

        # Build a one-paragraph codebase narrative for the cached system prefix.
        pkgs = sorted({d.locations[0].module.split(".")[0]
                       for d in bundle.decision_points if d.locations})
        principles = sorted({d.principle for d in bundle.decision_points})
        codebase_summary = (
            f"Repository: {repo.name}. "
            f"Top-level package(s) analyzed: {', '.join(pkgs) or 'unknown'}. "
            f"Modules: {bundle.graph_summary.get('num_modules', 0)}, "
            f"internal edges: {bundle.graph_summary.get('num_edges', 0)}. "
            f"{len(bundle.decision_points)} structural decision points were "
            f"flagged across {len(principles)} principles "
            f"({', '.join(principles)}) by deterministic Layer 1 analysis."
        )
        git_summary = (
            f"Commit {bundle.commit_sha[:8]} on branch "
            f"{bundle.git_summary.get('branch', 'unknown')}; "
            f"{bundle.git_summary.get('recent_commits', 0)} commits in the last 12 months."
        )

        # Look up the full DecisionPoints for the top-N (prioritized has only summaries).
        dp_by_id = {d.id: d for d in bundle.decision_points}
        top_dps = [dp_by_id[r["decision_point_id"]] for r in ranked
                   if r["decision_point_id"] in dp_by_id]

        # Each finding's tribunal+judge is independent. Run them concurrently.
        # On Wafer (no concurrency cap) this collapses top-N wall-clock to
        # ~the time of one tribunal. On Anthropic Tier-1 the per-tribunal
        # cell semaphore still protects us from the 50K TPM ceiling.
        verdicts: list[dict] = [None] * len(top_dps)

        # Import the live-event emitter so the FastAPI server (when wrapping
        # us) can show per-tribunal progress as each one completes — not
        # one giant batch at the end.
        from . import events as fevents

        # Fast-track threshold: if Layer 1 says the finding is unambiguous
        # in either direction, skip the 10-cell debate and let the judge
        # call directly from the evidence. We use only the 3 features that
        # actually measure severity (blast_radius, principle_severity,
        # pattern_violation); `recency` and `advocate_absence` are
        # context/history signals that often pin near 0 and would otherwise
        # drag every average below the threshold. Empirically ~50% of
        # findings on real-world audits fall into one of these tails.
        SKIP_CELLS_HIGH = 0.85
        SKIP_CELLS_LOW = 0.15

        def _structural_score(dp) -> float:
            feats = dp.measured_impact or {}
            keys = ("blast_radius", "principle_severity", "pattern_violation")
            vals = [feats.get(k, 0) or 0 for k in keys]
            return sum(vals) / len(vals) if vals else 0.5

        from .types import TribunalResult

        async def _one_tribunal(i: int, dp) -> dict:
            structural = _structural_score(dp)
            skip_cells = (
                structural >= SKIP_CELLS_HIGH or structural <= SKIP_CELLS_LOW
            )
            console.print(
                f"[blue]Tribunal {i + 1}/{len(top_dps)} ▸ started[/]: "
                f"{dp.id} ({dp.principle}) — {dp.subject[:80]}"
                + (f" [yellow](fast-tracked · structural={structural:.2f})[/]"
                   if skip_cells else "")
            )
            fevents.emit("tribunal_start", trib_idx=i, dp_id=dp.id,
                         subject=dp.subject, principle=dp.principle,
                         skip_cells=skip_cells)

            if skip_cells:
                # No panel — synthesize an empty TribunalResult and let the
                # judge work from Layer 1 evidence alone.
                tribunal = TribunalResult(
                    decision_point_id=dp.id,
                    cells=[],
                    aggregate_vote={
                        "winner": "n/a",
                        "cells_run": 0,
                        "cells_cancelled": 0,
                        "cells_failed": 0,
                        "panel_skipped": True,
                        "skip_reason": f"structural={structural:.2f}",
                    },
                    judge={},
                )
            else:
                # 10 cells per finding (was 15) covers the top 10 of 15 pair
                # combinations — the highest-tension matchups (Simp×Ship
                # through Scaler×Simp). Speculative stopping may exit
                # even earlier when ≥6 cells agree with avg conf ≥0.7.
                tribunal = await run_tribunal_speculative(
                    decision_point=dp, num_cells=10,
                    codebase_summary=codebase_summary, git_summary=git_summary,
                    pc=cell_pc,
                )

            judge_out = await run_judge(
                decision_point=dp, cells=tribunal.cells, pc=judge_pc,
                panel_skipped=skip_cells,
            )
            console.print(
                f"  [blue]◂ {i + 1}[/] {tribunal.aggregate_vote['cells_run']}/10 cells · "
                f"winner={tribunal.aggregate_vote['winner']} · "
                f"⚖  {verdict_markup(judge_out['verdict'])}"
                f"{' [yellow](override)[/]' if judge_out.get('override') else ''}"
            )
            tr = tribunal.model_dump()
            tr["judge"] = judge_out

            # Save THIS finding's verdict slot immediately + flush partial
            # verdicts.json so the live-audit UI can re-fetch and render
            # what's done without waiting for the whole batch.
            verdicts[i] = tr
            (audit_dir / "verdicts.json").write_text(
                json.dumps([v for v in verdicts if v is not None], indent=2),
                encoding="utf-8",
            )
            fevents.emit("tribunal_complete", trib_idx=i, dp_id=dp.id,
                         verdict=judge_out.get("verdict"),
                         override=bool(judge_out.get("override")))
            return tr

        async def _layer2() -> list:
            return await asyncio.gather(*[
                _one_tribunal(i, dp) for i, dp in enumerate(top_dps)
            ], return_exceptions=True)

        results = asyncio.run(_layer2())
        # Surface any tribunal failures without losing the rest of the run.
        failed = [(i, r) for i, r in enumerate(results) if isinstance(r, BaseException)]
        for i, exc in failed:
            console.print(f"[red]Tribunal {i + 1} failed:[/] {exc!r}")

        # Final canonical write (in case the concurrent partial-flushes left
        # a slot-ordering quirk; this is a no-op if all slots filled cleanly).
        (audit_dir / "verdicts.json").write_text(
            json.dumps([v for v in verdicts if v is not None], indent=2),
            encoding="utf-8",
        )
        cell_s = cell_pc.metrics.summary()
        judge_s = judge_pc.metrics.summary()
        total_cost = cell_s["total_cost_usd"] + judge_s["total_cost_usd"]
        total_nocache = (
            cell_s.get("no_cache_cost_usd", cell_s["total_cost_usd"])
            + judge_s.get("no_cache_cost_usd", judge_s["total_cost_usd"])
        )
        savings_x = (total_nocache / total_cost) if total_cost > 0 else 1.0
        console.print(
            f"[green]Layer 2[/]: {len(verdicts)} tribunals · "
            f"cells({cell_pc.backend_name})="
            f"cache_ratio={cell_s['cache_read_ratio']:.1%} "
            f"${cell_s['total_cost_usd']:.3f} · "
            f"judge(anthropic)=${judge_s['total_cost_usd']:.3f} · "
            f"total ${total_cost:.3f}"
        )

        # --- Cache savings story ---
        # Print the counterfactual cost (what we'd have paid without prompt
        # caching) and the per-cell hit-rate breakdown. Cell 0 always misses
        # — it writes the cache. Cells 1..N read it. The gap shows the
        # cache delivering value, in real numbers.
        console.print(
            f"[bold cyan]Cache savings[/]: actual ${total_cost:.3f} · "
            f"without cache ${total_nocache:.3f} · "
            f"[bold]{savings_x:.1f}× reduction[/] "
            f"(saved ${total_nocache - total_cost:.3f})"
        )

        # Persist a metrics sidecar the UI can read for the sidebar
        # hero number. Keep the schema small and stable — it's the only
        # surface the frontend consumes.
        (audit_dir / "metrics.json").write_text(
            json.dumps({
                "actual_cost_usd": round(total_cost, 4),
                "no_cache_cost_usd": round(total_nocache, 4),
                "savings_usd": round(total_nocache - total_cost, 4),
                "savings_multiplier": round(savings_x, 2),
                "cache_read_ratio": cell_s.get("cache_read_ratio", 0),
                "cell_backend": cell_pc.backend_name,
                "tribunals": len(verdicts),
            }, indent=2),
            encoding="utf-8",
        )

        per_cell = cell_pc.metrics.per_cell()
        if per_cell:
            # Show first few cells so the cold-cache → warm-cache curve is
            # legible without flooding the terminal.
            ids = sorted(per_cell.keys())[:6]
            console.print("[cyan]Per-cell cache hit rate (cells 0–{}):[/]".format(ids[-1]))
            for cid in ids:
                s = per_cell[cid]
                console.print(
                    f"  cell {cid:>2}: {s['cache_read_ratio']:>5.1%} hit · "
                    f"read={s['cache_read']:>5}t  "
                    f"created={s['cache_creation']:>5}t  "
                    f"uncached={s['uncached_input']:>4}t  "
                    f"${s['cost_usd']:.4f}"
                )
            # Aggregate over cells ≥ 1 (the cold cell 0 is the only writer).
            warm_ids = [i for i in per_cell if i >= 1]
            if warm_ids:
                cr_warm = sum(per_cell[i]["cache_read"] for i in warm_ids)
                un_warm = sum(per_cell[i]["uncached_input"] for i in warm_ids)
                ratio_warm = cr_warm / (cr_warm + un_warm) if (cr_warm + un_warm) else 0
                console.print(
                    f"[cyan]Warm-cache cells (≥1):[/] "
                    f"{ratio_warm:.1%} hit rate across {len(warm_ids)} cells"
                )

    # --- Layer 3: Opus report writer ---
    if not skip_report:
        import asyncio
        import json

        from ._polish import phase, render_report
        from .cache.prompt_cache import OPUS, PromptCache
        from .report.writer import write_report

        verdicts_path = audit_dir / "verdicts.json"
        if not verdicts_path.exists():
            console.print(
                f"[red]Cannot write report:[/] {verdicts_path} is missing. "
                f"Layer 2 was skipped — either drop --skip-jury, or also pass "
                f"--skip-report."
            )
            sys.exit(2)
        verdicts_data = json.loads(verdicts_path.read_text(encoding="utf-8"))

        report_pc = PromptCache(model=OPUS)
        with phase(console, "Layer 3: Opus is writing the briefing"):
            artifact = asyncio.run(write_report(
                bundle=bundle,
                prioritized=ranked,
                verdicts=verdicts_data,
                user_values=weights,
                pc=report_pc,
            ))

        report_path = audit_dir / "report.md"
        report_path.write_text(artifact.markdown, encoding="utf-8")

        # Update metrics.json with the total wall-clock now that the full
        # pipeline has finished. The earlier write (after Layer 2) didn't
        # include Layer 3 latency. The UI shows this in the Briefing header.
        metrics_path = audit_dir / "metrics.json"
        try:
            existing = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        existing["audit_duration_s"] = round(_time.perf_counter() - audit_t0, 1)
        metrics_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

        console.rule("[bold]Report[/]")
        console.print(f"[green]Layer 3[/]: [cyan]{report_path}[/]")
        console.print(f"  headline: {artifact.headline}")
        console.print(f"  words: {artifact.stats['word_count']} "
                      f"(target 1500–2000)")
        console.print(f"  cost: ${artifact.stats['cost_usd']:.4f}  "
                      f"latency: {artifact.stats['latency_s']:.1f}s")
        console.print(f"[bold cyan]Total audit wall-clock:[/] "
                      f"{existing['audit_duration_s']:.1f}s")

        render_report(console, artifact.markdown)


def _replay_audit(audit_dir: Path) -> None:
    """Re-emit a cached audit at demo pace, with zero LLM calls.

    Reads evidence.json, prioritized.json, verdicts.json, report.md from
    `audit_dir`; loops them back through the same animations and headers
    as the live path. Designed to finish in well under 10s so it can
    stand in for a live audit on stage when WiFi is iffy or budget is
    tight (T10 demo backstop).
    """
    import json
    import time

    from ._polish import phase, render_report, verdict_markup
    from .types import EvidenceBundle

    evidence_path = audit_dir / "evidence.json"
    prio_path = audit_dir / "prioritized.json"
    verdicts_path = audit_dir / "verdicts.json"
    report_path = audit_dir / "report.md"
    for p in (evidence_path, prio_path, verdicts_path, report_path):
        if not p.exists():
            console.print(f"[red]Replay cache missing artifact:[/] {p}")
            sys.exit(2)

    bundle = EvidenceBundle.model_validate_json(evidence_path.read_text(encoding="utf-8"))
    prio = json.loads(prio_path.read_text(encoding="utf-8"))
    verdicts = json.loads(verdicts_path.read_text(encoding="utf-8"))
    report_md = report_path.read_text(encoding="utf-8")

    repo_name = Path(bundle.repo).name
    console.rule(f"[bold]forum audit[/] {repo_name} @ {bundle.commit_sha[:8]} "
                 f"[dim](replay)[/]")
    console.print(f"Cache: [cyan]{audit_dir}[/]")

    # --- Layer 1 (animated) ---
    with phase(console, "Layer 1: deterministic evidence extraction"):
        time.sleep(0.6)
    n_dps = len(bundle.decision_points)
    n_pri = len({d.principle for d in bundle.decision_points})
    console.print(f"[green]Layer 1[/]: {n_dps} decision points across {n_pri} principles")
    time.sleep(0.2)

    # --- Layer 1.5 ---
    weights = prio.get("values", {})
    ranked = prio.get("items", [])
    console.print(f"[green]Layer 1.5[/]: top {len(ranked)} → [cyan]{prio_path}[/]")
    for r in ranked:
        console.print(
            f"  [bold]#{r['rank']}[/] {r['principle']} "
            f"(comp={r['composite_score']:.3f}, struct={r['structural_score']:.3f}, "
            f"val={r['value_affinity_score']:+.2f}) — {r['subject']}"
        )
    time.sleep(0.3)

    # --- Layer 2 (per-tribunal) ---
    dp_by_id = {d.id: d for d in bundle.decision_points}
    for i, tribunal in enumerate(verdicts, start=1):
        dp_id = tribunal["decision_point_id"]
        dp = dp_by_id.get(dp_id)
        subject = (dp.subject if dp else dp_id)[:80]
        principle = dp.principle if dp else "?"
        console.print(f"[blue]Tribunal {i}/{len(verdicts)}[/]: "
                      f"{dp_id} ({principle}) — {subject}")
        agg = tribunal.get("aggregate_vote", {})
        with phase(console, f"  10 cells deliberating, KV-cache reuse on the prefix"):
            time.sleep(0.6)
        console.print(
            f"  → {agg.get('cells_run', '?')}/10 cells, "
            f"winner={agg.get('winner', '?')} "
            f"margin={agg.get('margin', 0):.2f}"
        )
        judge = tribunal.get("judge") or {}
        v_text = judge.get("verdict", "(no verdict)")
        console.print(f"  ⚖  verdict: {verdict_markup(v_text)}"
                      f"{' [yellow](override)[/]' if judge.get('override') else ''}")
        time.sleep(0.15)
    console.print(f"[green]Layer 2[/]: {len(verdicts)} tribunals "
                  f"[dim](cached — zero new tokens)[/]")

    # --- Layer 3 ---
    with phase(console, "Layer 3: Opus is writing the briefing"):
        time.sleep(0.9)
    headline = next((l.lstrip("# ").strip() for l in report_md.splitlines()
                     if l.strip().startswith("# ")), "(no headline)")
    word_count = len(report_md.split())
    console.rule("[bold]Report[/]")
    console.print(f"[green]Layer 3[/]: [cyan]{report_path}[/]")
    console.print(f"  headline: {headline}")
    console.print(f"  words: {word_count} (target 1500–2000)")
    console.print(f"  [dim](cached — zero new tokens)[/]")

    render_report(console, report_md)


@main.command()
@click.argument("audit_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--values", "values_path", type=click.Path(exists=True, path_type=Path),
              default=None, help="YAML file of NEW value weights to re-project under.")
@click.option("--value", "value_overrides", multiple=True,
              help="Single NEW value override, e.g. --value velocity=2.5 (repeatable).")
@click.option("--output", "out_path", type=click.Path(path_type=Path), default=None,
              help="Write the markdown probe to a file (default: stdout + <audit_dir>/whatif.md).")
@click.option("--verbose", is_flag=True, default=False)
def whatif(audit_dir: Path, values_path: Path | None,
           value_overrides: tuple[str, ...], out_path: Path | None,
           verbose: bool) -> None:
    """Re-project a cached audit under alternate value weights. Zero LLM cost."""
    import time

    import json

    from .values.loader import VALID_VALUES, load_values
    from .whatif.probe import probe

    _setup_logging(verbose)
    audit_dir = audit_dir.resolve()

    # Baseline weights come from prioritized.json (the audit's Layer 1.5 input).
    # New weights start from the baseline and apply the user's --values /
    # --value flags on top — so `--value velocity=2.5` changes velocity and
    # leaves every other dimension as the original audit had it. Without
    # this, undeclared dims would silently revert to load_values's all-1.0
    # default, surprising the user.
    prio_path = audit_dir / "prioritized.json"
    if prio_path.exists():
        baseline = json.loads(prio_path.read_text(encoding="utf-8")).get("values")
        if not baseline:
            baseline = {v: 1.0 for v in VALID_VALUES}
    else:
        baseline = {v: 1.0 for v in VALID_VALUES}
    new_weights = dict(baseline)
    if values_path is not None:
        new_weights.update(load_values(values_path))
    for ov in value_overrides:
        if "=" not in ov:
            continue
        k, v = ov.split("=", 1)
        k = k.strip()
        if k in VALID_VALUES:
            new_weights[k] = float(v)

    t0 = time.perf_counter()
    result = probe(audit_dir, new_weights=new_weights, baseline_weights=None)
    dt = time.perf_counter() - t0

    out = out_path or (audit_dir / "whatif.md")
    out.write_text(result["markdown"], encoding="utf-8")

    print(result["markdown"])
    console.rule()
    console.print(
        f"[green]whatif[/]: {result['n_decision_points']} DPs, "
        f"{result['n_with_shifted_dissent']} with shifted dissents — "
        f"changed dims: {result['changed_dimensions'] or '(none)'} — "
        f"{dt:.3f}s — [cyan]{out}[/]"
    )


@main.command("cache-test")
@click.option("--model", default=None, help="Model id (default: Haiku).")
@click.option("--verbose", is_flag=True, default=False)
def cache_test(model: str | None, verbose: bool) -> None:
    """Send two identical-prefix calls to Anthropic and report cache stats.

    Verifies T5 achievement criteria 1, 2, 3, 4: warm call shows cache_read > 0,
    cache_read_ratio ≥ 0.8 on the warm call, metrics aggregator returns a
    populated dict, and the cost calculation is non-zero (sanity check
    against Anthropic's posted prices).
    """
    import asyncio
    from .cache.prompt_cache import HAIKU, PromptCache

    _setup_logging(verbose)
    model = model or HAIKU

    # The cached prefix needs to be large enough to clear the model's cache
    # minimum (Haiku ~2048 tokens). Pad with realistic codebase narrative.
    paragraph = (
        "This repository implements a high-performance Python web framework. "
        "It exposes a routing layer, a dependency-injection system, OpenAPI "
        "generation, parameter parsing, security primitives, and middleware. "
        "Modules are organized into a `routing` core, a `dependencies` "
        "package containing models and resolution utilities, a `params` "
        "module describing query/path/body parameter shapes, an `encoders` "
        "module that handles JSON serialization of Pydantic models, and an "
        "`openapi` package responsible for schema rendering. The codebase "
        "has been around several years and has accumulated structural "
        "decisions worth examining under principled scrutiny. "
    )
    codebase_summary = paragraph * 40  # ~7K tokens — well over Haiku 4.5's ~4K cache floor
    git_summary = (
        "Recent activity: 312 commits over the last 12 months across "
        "21 contributors. Largest co-change pairs include routing.py with "
        "dependencies/utils.py and openapi/utils.py with applications.py. "
        "The default branch is `main`. "
    ) * 5
    decision_evidence = (
        "Decision under review: a strongly-connected component of 18 modules "
        "rooted at `fastapi/routing.py`. Cycle members include "
        "fastapi.dependencies.utils, fastapi.params, fastapi.encoders, and "
        "fastapi.openapi.utils. The cycle has been stable for >2 years; "
        "co-change frequency between cycle members is high. "
    ) * 5

    system_cached = f"<codebase_summary>\n{codebase_summary}\n</codebase_summary>\n\n<git_summary>\n{git_summary}\n</git_summary>"
    user_cached = f"<decision_point_evidence>\n{decision_evidence}\n</decision_point_evidence>\n\n<principle_definition>\nP1 — Acyclic Dependencies (Robert C. Martin). Modules form a DAG.\n</principle_definition>"

    async def _run() -> None:
        pc = PromptCache(model=model)
        for label, tail in [
            ("COLD", "In one sentence, name the principle this cycle violates."),
            ("WARM", "In one sentence, name the most concrete next step a senior engineer would take."),
        ]:
            await pc.call(
                system_cached=system_cached,
                user_cached=user_cached,
                user_tail=tail,
                max_tokens=80,
                temperature=0.3,
            )
            r = pc.metrics.calls[-1]
            ratio = r.cache_read_input_tokens / max(1, r.cache_read_input_tokens + r.input_tokens)
            console.print(
                f"[bold]{label}[/]: in={r.input_tokens} cw={r.cache_creation_input_tokens} "
                f"cr={r.cache_read_input_tokens} out={r.output_tokens} "
                f"ratio={ratio:.1%} {r.latency_s:.2f}s ${r.cost_usd:.5f}"
            )

        s = pc.metrics.summary()
        console.rule("CacheMetrics summary")
        for k, v in s.items():
            console.print(f"  {k}: {v}")

    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    main()
