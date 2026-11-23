"""
Pre-check 1 — does the control loop actually CLOSE?

Everything downstream assumes an external xApp's command changes what the engine
reports next. That assumption has been wrong here before: runtime SetTxPower once
never reached the radio, so cell-off and sleep looked free and byte-identical
runs were the only tell. So this is measured, not assumed, and it is measured on
the exact path the experiment will use.

Method: observe for SETTLE indications, then cut one cell's TX power hard and
keep watching. If the loop is closed, that cell's measured radio power drops and
its UEs' SINR degrades. If nothing moves, the experiment is dead and the rest of
the build is pointless — which is precisely why this runs first.
"""
from __future__ import annotations

import statistics
import sys

from harness import run
from scenario import build

SETTLE = 10          # indications observed before intervening
TOTAL = 30           # total indications to collect
CUT_TO_DBM = 20      # from the 43 dBm baseline — far outside any noise band


def main() -> int:
    cfg = build(seed=1)
    target: dict = {}

    def controller(kpm: dict):
        tick = len(state["seen"])
        state["seen"].append(tick)
        if tick != SETTLE:
            return []
        cells = kpm.get("cells") or []
        if not cells:
            return []
        # Whichever cell the engine is actually reporting first; the point is
        # that SOME cell responds, not which one.
        target["cell"] = cells[0].get("cell")
        print(f"  [tick {tick}] cutting cell {target['cell']} to {CUT_TO_DBM} dBm")
        return [{"cmd": "cell_power", "cell": target["cell"], "dbm": CUT_TO_DBM}]

    state = {"seen": []}
    print(f"running {TOTAL} indications, cutting power at indication {SETTLE}…")
    log = run(cfg, controller, max_ticks=TOTAL,
              on_tick=lambda t: print(f"  tick {t.tick:>3}  {t.wall_s:>6.1f}s  "
                                      f"total={t.energy_w}  cells={len(t.cells)}  "
                                      f"ues={len(t.ues)}", flush=True))

    print(f"\nboot {log.boot_s}s, wall {log.wall_s}s, {len(log.ticks)} indications")
    if log.status_lines:
        print("engine said:", " | ".join(log.status_lines[-3:]))
    if not log.ok:
        print(f"FAILED: {log.error}")
        return 1

    cell = target.get("cell")
    if cell is None:
        print("FAILED: no cell rows arrived, nothing to control")
        return 1

    key = str(cell)
    before = [t.per_cell_w.get(key) for t in log.ticks[:SETTLE]]
    after = [t.per_cell_w.get(key) for t in log.ticks[SETTLE + 1:]]
    before = [v for v in before if v is not None]
    after = [v for v in after if v is not None]

    def sinr_of(t, c) -> float | None:
        vals = [u.get("sinr") for u in t.ues
                if str(u.get("cell")) == str(c) and u.get("sinr") is not None]
        return statistics.mean(vals) if vals else None

    s_before = [v for v in (sinr_of(t, cell) for t in log.ticks[:SETTLE]) if v is not None]
    s_after = [v for v in (sinr_of(t, cell) for t in log.ticks[SETTLE + 1:]) if v is not None]

    print(f"\ncell {cell}: 43 dBm -> {CUT_TO_DBM} dBm at indication {SETTLE}")
    print(f"  radio power  before {statistics.mean(before):.4f} W  "
          f"after {statistics.mean(after):.4f} W" if before and after
          else f"  radio power  before={before[:3]} after={after[:3]}")
    if s_before and s_after:
        print(f"  served SINR  before {statistics.mean(s_before):.2f} dB  "
              f"after {statistics.mean(s_after):.2f} dB")
    else:
        print(f"  served SINR  before={len(s_before)} samples after={len(s_after)} samples")

    moved_power = bool(before and after
                       and abs(statistics.mean(after) - statistics.mean(before)) > 1e-4)
    moved_sinr = bool(s_before and s_after
                      and statistics.mean(s_before) - statistics.mean(s_after) > 0.5)

    print("\nVERDICT: " + (
        "LOOP CLOSES — external xApp control reaches the running RAN"
        if (moved_power or moved_sinr) else
        "LOOP DOES NOT CLOSE — control had no observable effect"))
    return 0 if (moved_power or moved_sinr) else 1


if __name__ == "__main__":
    sys.exit(main())
