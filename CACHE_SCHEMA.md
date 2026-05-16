# Forum cache schema

All audit artifacts live under `./audits/<repo-hash>/`. `<repo-hash>` is the
first 12 hex chars of `sha256(absolute_repo_path + ":" + commit_sha)`.

```
audits/<repo-hash>/
├── evidence.json       # Layer 1 output: EvidenceBundle
├── graph.svg           # Layer 1 dependency graph (from pydeps)
├── prioritized.json    # Layer 1.5 output: top-N DecisionPoints + scores
├── verdicts.json       # Layer 2 output: TribunalResult[] (one per decision point)
└── report.md           # Layer 3 output: human-readable markdown briefing
```

## File contracts

### evidence.json
JSON-serialized `EvidenceBundle` (see `src/forum/types.py`). Frozen format —
downstream layers depend on the exact shape.

### prioritized.json
```json
{
  "values": { "<name>": <weight>, ... },
  "items": [
    {
      "decision_point_id": "<id>",
      "structural_score": <float>,
      "value_affinity_score": <float>,
      "composite_score": <float>,
      "rank": <int>
    }
  ]
}
```

### verdicts.json
JSON array of `TribunalResult`. Order matches `prioritized.json.items`.

### report.md
Plain markdown, 1200–2500 words. No HTML, no inline base64.

## Invariants

- Layer 1 re-runs every audit (no cache). Layers 1.5, 2, 3 are cacheable.
- The same `(repo_path, commit_sha)` always produces the same `<repo-hash>`.
- `verdicts.json` is replay-safe: the `whatif` command consumes it without LLM calls.
- A complete cache directory is sufficient to reproduce the full report offline.
