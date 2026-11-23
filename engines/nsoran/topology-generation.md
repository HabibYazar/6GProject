# Procedural topology generation

`POST /scenario/generate` — "7 cells, hexagonal, 350 m apart, 2 UEs each" instead
of placing every cell by hand. This is VIAVI's `Scenario_Generation` block, and
it is the first item on the post-parameter roadmap because every scenario we run
from here on starts from one of these.

Code: `nsg_ai/topology.py`. Test: `test_topology_gen.py`
(`--engine` adds three real runs, ~6 min).

## The promise

Not "emits a valid config" — **emits a config the engine runs**. Validation is
cheap and the schema was never the thing that broke; the engine's unwritten
envelope was. Every constraint the generator enforces is one we hit as a real
failure, and they are the reason this is generated rather than typed:

| Trap | What the engine does | Enforced by |
|---|---|---|
| More than 7 cells | zeroes PRB and throughput | `cells` clamped to `GNB_MIN..GNB_MAX` |
| Too many UEs | run buried / heap corruption | `ues_per_cell` shrunk to fit `UE_MAX` |
| UE on a base station | `log10(0)` link | placement guards, verified >= 25 m |
| A cell with no UE | **no DU metrics — reads as a dead engine** | `_starved_cells` re-roll |
| Cell on the LTE anchor | run dies | `_HUB_CLEAR_M` = 160 m |
| Overlapping buildings | `NS_FATAL` before any KPM | blocks are disjoint by construction |
| Building swallowing a UE spawn | outdoor mobility model asserts | blocks inset from the streets |

## Why the generator does not place individual UEs

It used to. It computed a ring per cell — bearings spread apart, radii jittered,
snapped to street axes so nobody started inside a block — and **all of it was
discarded**. `main._ue_scenario_positions` treats a group's `spawn_position` as a
*centre* and re-scatters that group over a disc, then applies its own guards
(building eviction, base-station separation, balanced re-scatter). Those guards
run *after* the scene has been translated into the engine's fixed area, so they
see the geometry ns-3 will actually receive; the generator does not.

So the generator's only real levers are the **spawn point** and the **count**.
One group per cell, spawned on that cell. Fighting the platform measurably
produced a *worse* result: the hand-built rings left one cell of seven with no
UEs at all.

## The starved cell, and why it needs a re-roll rather than better arithmetic

The platform's disc has radius `0.6 x cell spacing`. The halfway line to a
neighbour is at `0.5 x spacing`. The disc therefore always reaches past it, and
a UE can land closer to a neighbour than to its own cell — that ratio is fixed
in `_ue_spread_m` and does not depend on the layout, so **no choice of site
spacing removes it**. Symmetric layouts cancel the leakage out (hex is a flat
`[2,2,2,2,2,2,2]` on every seed); an irregular one does not, and a cell with
several close neighbours can lose both UEs.

`scatter` therefore re-rolls (up to 12 deterministic attempts from the seed)
until `_starved_cells` returns 0, checked **through `nsoran_params` itself**
rather than against the generator's own coordinates — anything less is a guess
about someone else's code. If every roll starves a cell, the config comes back
with the warning in its description instead of a silent promise.

## Measured — the layout reaches ns-3

5 cells, 350 m, 2 UEs each, 25 Mbps offered, seed 42, `sim_time=20`:

| layout | E2 agents | UEs | 64QAM | BLER | PRB (mean) | UE thp (mean) | HOs |
|---|---|---|---|---|---|---|---|
| hex | 6 | 10 | 94.4% | 15.8% | 9.3 | 13.4 Mbps | 0 |
| manhattan (4 blocks) | 6 | 10 | **77.4%** | **20.8%** | **12.1** | 12.5 Mbps | 0 |
| scatter | 6 | 10 | 97.4% | 14.0% | 9.9 | 12.1 Mbps | 3 |

6 agents = 5 gNB + the LTE anchor. The Manhattan row is the one that matters:
concrete costs modulation and BLER, so the same offered traffic burns ~30% more
PRB. The buildings are geometry, not scenery. Scatter's irregular spacing is the
only layout that triggers handovers at rest.

The engine has run-to-run variance even at a fixed seed (a repeat measured 78.3%
/ 94.9% for the same two cells), so read the table as a gap, not as constants.
The gap is what `test_topology_gen.py` asserts — `manhattan 64QAM < hex 64QAM`,
not the numbers themselves. A generator whose layouts all produce identical
radio is not generating anything, and only checking `status == ok` would never
have noticed.

## Also covered

`server_import_layout` runs the generator from inside `nsg_ai/`, the way uvicorn
does. It exists because the first version of `_starved_cells` imported
`nsg_ai.main` outright: green under this harness, `ModuleNotFoundError` on the
live server, and only on `scatter` — the one layout that calls back into the
platform. Reaching for a module by name is the thing to distrust here.
