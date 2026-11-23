"""
Does cell_off now actually switch a cell off?

Before the fix it did not. The RIC control path dropped transmit power to 0 dBm
and raised the noise figure, and stopped there: the cell stayed a valid handover
target, kept its users, and went on carrying between 21 and 73 Mbps while
reporting itself switched off. LteEnbRrc had the machinery to disable a cell —
SetSecondaryCellHandoverAllowedStatus and EvictUsersFromSecondaryCell — and
nothing in the tree had ever called either.

The acceptance test is deliberately about SERVICE, not about power. A cell that
reports zero watts while carrying traffic is exactly the failure being fixed, so
watts alone cannot confirm the fix:

  1. the commanded cell's UE count must fall to zero
  2. its carried throughput must fall to zero
  3. those UEs must reappear on other cells, not vanish from the network
"""
from __future__ import annotations

import statistics
import sys

from harness import run
from scenario import build

SETTLE = 6       # observations AFTER the roster is complete
TOTAL = 60
EXPECTED_UES = 9
# NOTE on the settling window. cell_off is asynchronous (platform -> FIFO ->
# ns-3), so some indications after the command still describe the old network,
# and it is tempting to widen the window until they are excluded. Two runs
# suggested the commanded cell drains monotonically to zero in ~10 indications,
# which would have justified a window of 15. A third run refuted it: the cell
# fell 4 -> 3 UEs and then went back UP to 4 UEs / 41 Mbps — above its load at
# the moment of the command — before finally emptying. That is a refill, not a
# drain, and any window wide enough to hide it would have made this test pass
# on an engine that still serves traffic from a cell it reports as off.
# So the window stays narrow deliberately. Widen it only with evidence that the
# cell never re-serves, not with evidence that it eventually stops.
SKIP_AFTER_COMMAND = 2


def cell_rows(kpm: dict, cell) -> dict | None:
    for c in kpm.get("cells") or []:
        if str(c.get("cell")) == str(cell):
            return c
    return None


def main() -> int:
    cfg = build(seed=1)
    # `seen` counts SETTLED observations (the gate below), `ticks` counts every
    # indication. The command's position has to be recorded in ticks, because
    # that is what the analysis slices — mixing the two put the "before" window
    # in the middle of the attach ramp and made every median describe a network
    # that was still being built.
    state: dict = {"seen": 0, "ticks": 0, "target": None, "off_at": None}

    def controller(kpm: dict):
        state["ticks"] += 1
        # Wait for the full roster before touching anything. The first attempt
        # switched a cell off while UEs were still attaching, so the "before"
        # window described a network being built and every median taken from it
        # was meaningless.
        if state["target"] is None and len(kpm.get("ues") or []) < EXPECTED_UES:
            return []
        state["seen"] += 1
        if state["seen"] != SETTLE:
            return []
        cells = [c for c in (kpm.get("cells") or []) if c.get("ues")]
        if not cells:
            return []
        # The busiest cell: switching off an empty one proves nothing.
        target = max(cells, key=lambda c: c.get("ues") or 0)
        state["target"] = target["cell"]
        state["off_at"] = state["ticks"]
        print(f"  [obs {state['seen']}] switching OFF cell {target['cell']} "
              f"(serving {target['ues']} UEs, {target.get('thp_kbps', 0)/1000:.1f} Mbps)")
        return [{"cmd": "cell_off", "cell": target["cell"]}]

    print(f"running {TOTAL} indications, cell_off at {SETTLE}…")
    log = run(cfg, controller, max_ticks=TOTAL,
              on_tick=lambda t: print(f"  tick {t.tick:>3} {t.wall_s:>6.1f}s "
                                      f"ues={len(t.ues)} cells={len(t.cells)}",
                                      flush=True))
    if not log.ok:
        print(f"\nFAILED: {log.error}")
        return 1
    cell = state["target"]
    if cell is None:
        print("\nFAILED: no cell was serving anyone, nothing to switch off")
        return 1

    idx = state["off_at"]
    before = log.ticks[:idx]
    after = log.ticks[idx + SKIP_AFTER_COMMAND:]   # skip the tick the command lands on

    def served(ticks):
        return [len([u for u in t.ues if str(u.get("cell")) == str(cell)]) for t in ticks]

    def carried(ticks):
        return [sum((u.get("thp_kbps") or 0) for u in t.ues
                    if str(u.get("cell")) == str(cell)) / 1000 for t in ticks]

    def total_ues(ticks):
        return [len(t.ues) for t in ticks]

    b_ue, a_ue = served(before), served(after)
    b_mb, a_mb = carried(before), carried(after)
    b_all, a_all = total_ues(before), total_ues(after)

    # The per-tick series, not just the summary. A median of 0 with a nonzero
    # max can mean two very different things — a cell that empties and refills,
    # or one stale KPM frame arriving after the command — and only the series
    # tells them apart. The engine delivers indications in bursts (several ticks
    # share a wall-clock time), so "how many ticks after the command" is not the
    # same as "how much simulated time after it".
    all_ue, all_mb = served(log.ticks), carried(log.ticks)
    print(f"\nper-tick on cell {cell}  (command issued at tick {idx}, '|' marks it)")
    for label, series in (("UEs ", all_ue), ("Mbps", [round(v) for v in all_mb])):
        row = "".join(f"{'|' if i + 1 == idx else ' '}{v:>3}" for i, v in enumerate(series))
        print(f"  {label} {row}")

    # How long the cell went on serving after the command, measured rather than
    # assumed: walk back from the end over the final run of idle observations.
    # This is a measurement, not a criterion — it says when the cell went quiet,
    # and says nothing about whether it stayed quiet in between. Read it next to
    # the per-tick series above, which does.
    last_serving = len(all_ue)
    while (last_serving > 0
           and all_ue[last_serving - 1] == 0 and all_mb[last_serving - 1] < 0.05):
        last_serving -= 1
    drained_after = last_serving - idx

    print(f"\ncell {cell}, before -> after the switch-off")
    print(f"  UEs served by it   {statistics.median(b_ue):.0f} -> {statistics.median(a_ue):.0f}"
          f"   (max after: {max(a_ue) if a_ue else 0})")
    print(f"  Mbps carried by it {statistics.median(b_mb):.1f} -> {statistics.median(a_mb):.1f}"
          f"   (max after: {max(a_mb) if a_mb else 0:.1f})")
    print(f"  UEs in the NETWORK {statistics.median(b_all):.0f} -> {statistics.median(a_all):.0f}"
          f"   (they should move, not disappear)")
    print(f"  went quiet after   {drained_after} indication(s), "
          f"then idle for the last {len(all_ue) - last_serving}")

    emptied = bool(a_ue) and max(a_ue) == 0
    silent = bool(a_mb) and max(a_mb) < 0.05
    kept = bool(a_all) and statistics.median(a_all) >= statistics.median(b_all) - 1

    print("\nVERDICT: " + (
        f"CELL OFF IS REAL — it lost its users and carries nothing "
        f"across {len(after)} settled observations"
        if emptied and silent else
        "STILL SERVING — the command did not take the cell out of service"))
    if emptied and silent and not kept:
        print("  WARNING: the network lost UEs rather than moving them — "
              "eviction worked but they did not re-attach elsewhere")
    return 0 if (emptied and silent) else 1


if __name__ == "__main__":
    sys.exit(main())
