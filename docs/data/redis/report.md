# The 27-Module Cycle: A Big-Bang Refactor You Can Skip With One Header File

This audit produced a single prioritized decision point, and the good news for a team optimizing for **velocity** and **simplicity** is that the headline finding is not what it appears to be at first glance. Yes, the dependency graph contains a 27-module strongly connected component — the kind of structural fact that usually translates into months of refactoring proposals and recurring "we should really clean that up" backlog items. But the Layer 1 evidence and the panel's deliberation converge on a much smaller surface: a single bidirectional edge between `src.slowlog` and `src.server` is the only confirmed cycle edge, and severing it is a sub-100-line change. The framing here matters: the verdict is real, but the ship cost to address it is bounded, scoped, and disproportionately small relative to the architectural payoff. That is the rare combination this team should act on.

## The `slowlog ↔ server` bidirectional dependency — the cycle you can actually cut

**Verdict: STRUCTURAL DEBT**

The audit identified a strongly connected component spanning 27 modules — `adlist`, `anet`, `atomicvar`, `bio`, `chk`, `cluster`, `cluster_asm`, `cluster_slot_stats`, `config`, `connection`, `dict`, `ebuckets`, `estore`, `fast_float_strtod`, `functions`, `fwtree`, `intset`, `latency`, `mstr`, `sds`, `sdsalloc`, `server`, `slowlog`, `syscheck`, `threads_mngr`, `util`, and `zmalloc` — with 68 internal edges binding them together. Every structural signal pegs at maximum: `blast_radius: 1.0`, `principle_severity: 1.0`, `pattern_violation: 1.0`. The panel voted 6-to-1 for STRUCTURAL DEBT, with two cells at 0.90 confidence independently concluding that nothing inside this cycle can be extracted, unit-tested in isolation, or re-architected without a refactor touching every internal edge.

Reading that on its face, a velocity-oriented team would reasonably groan. A 27-module big-bang refactor is exactly the kind of multi-quarter project that destroys iteration drag budgets and produces nothing shippable for months. But the actually-useful piece of Layer 1 evidence is buried in `cycle_edges`: the metric identifies **exactly one** confirmed bidirectional dependency — `src.slowlog ↔ src.server` (`src/slowlog.c:1-190`, `src/server.c:1-200`). That is the structural seam that anchors the cycle. A diagnostic/observability module and the core server module are mutually coupled, which is the kind of missing abstraction boundary that is genuinely cheap to install: a slowlog entry is a value type, not a behavior, and value types belong in headers, not in mutually-recursive translation units.

The judge's reasoning is worth quoting precisely on this point: the SCC makes independent evolution impossible, and `slowlog ↔ server` is the concrete seam violation that pins the rest of the cycle in place. The 0.90-confidence cells were not arguing for a 27-module rewrite; they were identifying the architectural constraint. Once you cut the load-bearing edge, the SCC decomposes, and the other modules in the component become legible to incremental refactoring on a normal cadence.

**The dissent matters here, and it matters in a way that aligns with this team's values.** Cell 0 — the lone justified-violation vote — pointed at two facts: `recency: 0.0` means the cycle is stable and not accreting new edges, and the `cycle_edges` metric shows only the single bidirectional pair as the actual confirmed coupling point. Cell 0's conclusion was that a full 27-module refactor would be disproportionate to the observable risk. The judge addressed this directly and correctly: stability does not make `blast_radius: 1.0` acceptable, and the single justified vote does not overturn three maximum-severity structural signals. The verdict stands as **STRUCTURAL DEBT**.

But for a team that weights velocity at 1.50 and simplicity at 1.20, Cell 0's reasoning is *also* the reason the recommended action is attractive rather than dreadful. The cycle is stable, so there is no urgency to take a heroic swing at all 27 modules. The single confirmed edge is the load-bearing complexity. The right move is to do the smallest thing that resolves the architectural objection — and then stop. The dissent is not a reason to defer the work; it is a reason to **scope the work tightly**.

### What the change looks like

The recommended action is concrete and contained:

1. Create a new low-level header — e.g., `src/slowlog_types.h` — containing the shared `SlowlogEntry` struct (or equivalent event-record type) that both `server.c` and `slowlog.c` currently reach for through the bidirectional include relationship.
2. `server.c` depends only on `src/slowlog_types.h` for the type definition. It no longer needs to include `slowlog.h` directly to manipulate or pass around the entry type.
3. `slowlog.c` depends on `src/slowlog_types.h` for the type and continues to expose its behavior through `slowlog.h`, but `slowlog.h` no longer needs to pull anything back from `server.h` to describe its own data.

The estimated scope: approximately 2–4 files and under 100 lines of diff. That is a single afternoon for an engineer familiar with the codebase, or a one-day spike with review for someone newer. In velocity terms: this is a single PR with a small, mechanical change, not a project. In simplicity terms: it removes one piece of load-bearing complexity (the mutual include) and replaces it with the simplest possible alternative (a header containing a struct). There is no new abstraction layer, no interface, no dependency injection — just a value type extracted to where value types belong.

### Why the small action is the right action for this team

The three plausible alternatives the audit surfaced are worth naming, because two of them are traps for a velocity-and-simplicity team:

- **Extract a shared interface into a lower-level module both can depend on.** This is the recommended action, and it is the cheapest. It is also the simplest: no behavioral abstraction, just a shared type.
- **Move the coupling concern into one of the modules and have the other depend one-way.** Plausible, but requires deciding which module "owns" the concern and likely involves moving non-trivial logic. Higher ship cost, higher review burden.
- **Use dependency inversion: introduce an abstraction and inject it at composition time.** This is the maximalist option. For a long-lived C codebase with no existing DI pattern, it introduces indirection that the team will have to maintain forever. High simplicity cost, high ramp cost for future contributors. Avoid.

Pick the first option. It costs the least in shipping velocity and adds the least complexity to the codebase. It also leaves the door open: once the `slowlog ↔ server` edge is severed and the SCC decomposes, any *future* cycle that emerges can be addressed with the same playbook — extract the shared type, depend on the type from a lower layer. The pattern becomes a normal part of the team's vocabulary instead of a rare, expensive event.

### What to watch for in code review

The change is mechanical, but two things deserve attention in the PR:

- **No behavior moves.** The point of this change is to relocate a type declaration, not to refactor slowlog logic or server logic. If the diff starts growing past 100 lines or touches functions rather than declarations, the scope has drifted and the PR should be tightened.
- **Include hygiene downstream.** Other modules in the 27-member SCC may currently transitively pick up the slowlog/server coupling through their existing includes. After the cut, run the build cleanly and confirm no other translation unit was depending on the cyclic relationship to resolve a symbol. If something breaks, the fix is almost always adding `#include "slowlog_types.h"` to the affected file — not restoring the cycle.

### The cycle that remains, and why that is fine

After this change, the audit's `cycle_edges` metric should drop to zero confirmed bidirectional edges, and the SCC should decompose into a DAG (or a substantially smaller SCC) on the next graph extraction. The other 26 modules still have a thicket of 68 internal edges, but with the load-bearing cycle gone, those edges become ordinary dependencies — legible, walkable, and amenable to incremental cleanup if and when a specific module needs to be touched for other reasons. `recency: 0.0` tells us the cycle has not been growing, so there is no reason to chase the remaining edges proactively. Let them sit until product work brings the team into one of those modules anyway, then opportunistically straighten the local dependencies as part of normal feature work.

## What to do first

Cut the `src.slowlog ↔ src.server` edge by extracting a `SlowlogEntry` type into `src/slowlog_types.h`. Scope the PR to type declarations only — under 100 lines, 2–4 files. This single change resolves the **STRUCTURAL DEBT** verdict's load-bearing seam at minimal ship cost, removes one piece of load-bearing complexity from the codebase, and converts the rest of the 27-module SCC from "untouchable big-bang refactor" into "ordinary dependency graph we can clean up opportunistically." Do not pursue dependency inversion or behavioral relocation; the simplest fix is also the correct one here. Defer all other work in the SCC until product pressure brings the team into those modules naturally.