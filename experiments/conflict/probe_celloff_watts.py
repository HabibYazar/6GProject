"""
Does cell_off actually reduce REPORTED POWER?

check6 deliberately measures service, not watts, because a cell reporting zero
watts while carrying traffic was the original bug. That question is now settled:
service stops. This asks the other half — once the cell is genuinely out of
service, does the energy model notice?

The chain is already wired (Phase 2a): SetBSTX drops TX power, and on the next
MmWaveSpectrumPhy::ChangeState the PHY charges the energy model state code 4
(STATE_OFF, 0 A). The suspected weakness is that `m_intState` only updates when
ChangeState is CALLED — a silent cell may stop generating PHY events and leave
the state stale. So this measures watts per cell across the switch-off instead
of assuming either way.
"""
from __future__ import annotations

import statistics
import sys

from harness import run
from scenario import build

SETTLE = 6
TOTAL = 60
EXPECTED_UES = 9
DRAIN = 6          # indications to let the switch-off settle before judging


def main() -> int:
    cfg = build(seed=1)
    state: dict = {"seen": 0, "ticks": 0, "target": None, "off_at": None}

    def controller(kpm: dict):
        state["ticks"] += 1
        if state["target"] is None and len(kpm.get("ues") or []) < EXPECTED_UES:
            return []
        state["seen"] += 1
        if state["seen"] != SETTLE:
            return []
        cells = [c for c in (kpm.get("cells") or []) if c.get("ues")]
        if not cells:
            return []
        target = max(cells, key=lambda c: c.get("ues") or 0)
        state["target"] = target["cell"]
        state["off_at"] = state["ticks"]
        print(f"  [obs {state['seen']}] OFF cell {target['cell']} "
              f"({target['ues']} UEs, {target.get('thp_kbps', 0)/1000:.1f} Mbps, "
              f"{target.get('power_w')} W)")
        return [{"cmd": "cell_off", "cell": target["cell"]}]

    log = run(cfg, controller, max_ticks=TOTAL)
    if not log.ok:
        print(f"FAILED: {log.error}")
        return 1
    cell, idx = state["target"], state["off_at"]
    if cell is None:
        print("FAILED: no cell was serving anyone")
        return 1

    key = str(cell)

    def watts(ticks):
        return [t.per_cell_w.get(key) for t in ticks if t.per_cell_w.get(key) is not None]

    def total_w(ticks):
        return [t.energy_w for t in ticks if t.energy_w is not None]

    before, after = log.ticks[:idx], log.ticks[idx - 1 + DRAIN:]
    b_w, a_w = watts(before), watts(after)
    b_tot, a_tot = total_w(before), total_w(after)

    # Service and power on the SAME run. Comparing them across runs is not good
    # enough: the question is whether a window exists where the cell serves
    # nobody and still reports power, and only one run can answer that.
    served = [len([u for u in t.ues if str(u.get("cell")) == key]) for t in log.ticks]
    mbps = [sum((u.get("thp_kbps") or 0) for u in t.ues
                if str(u.get("cell")) == key) / 1000 for t in log.ticks]
    series = [t.per_cell_w.get(key) for t in log.ticks]

    print(f"\nper-tick on cell {cell} (command at tick {idx}, '|' marks it)")
    for label, vals, fmt in (("UEs  ", served, "d"), ("Mbps ", mbps, ".0f"),
                             ("Watts", series, ".0f")):
        row = "".join(f"{'|' if i + 1 == idx else ' '}"
                      f"{('-' if v is None else format(v, fmt)):>4}"
                      for i, v in enumerate(vals))
        print(f"  {label} {row}")

    # The gap that matters: serving nobody, still charged for power.
    ghost = [i + 1 for i, (u, w) in enumerate(zip(served, series))
             if i + 1 > idx and u == 0 and (w or 0) > 0.5]
    if ghost:
        print(f"\n  ghost draw: {len(ghost)} tick(s) with 0 UEs but >0.5 W "
              f"(ticks {ghost[0]}-{ghost[-1]}, up to "
              f"{max(series[t-1] or 0 for t in ghost):.0f} W)")
    else:
        print("\n  ghost draw: none — power reaches zero as soon as service does")

    if not b_w or not a_w:
        print("\nINCONCLUSIVE: the engine reported no per-cell power for this cell")
        return 1

    print(f"\ncell {cell} power, before -> after")
    print(f"  its own watts     {statistics.median(b_w):.1f} -> {statistics.median(a_w):.1f} W"
          f"   (max after: {max(a_w):.1f})")
    if b_tot and a_tot:
        print(f"  NETWORK watts     {statistics.median(b_tot):.1f} -> "
              f"{statistics.median(a_tot):.1f} W")

    # Two separate questions, reported separately so a slow transition is never
    # mistaken for a broken one.
    settles = statistics.median(a_w) < 0.5 and (series[-1] or 0) < 0.5
    print("\nVERDICT: " + (
        "ENERGY MODEL IS TOLD — the switched-off cell settles at zero power"
        if settles else
        f"ENERGY MODEL NOT TOLD — the cell still draws "
        f"{statistics.median(a_w):.1f} W after the switch-off"))
    if settles and ghost:
        print(f"  BUT: {len(ghost)} tick(s) of ghost draw before it settles — the "
              f"cell is charged for power it cannot be using.")
    return 0 if settles else 1


if __name__ == "__main__":
    sys.exit(main())
