"""
Who is on the barred cell during the refill?

check6 counts UEs per cell, which is enough to say "the cell is still serving"
and not enough to say why. Two very different failures produce the same count:
the evicted UEs come back (the bar is not holding), or different UEs arrive (the
bar is holding but something else attaches to a dead cell).

This records the whole IMSI -> cell mapping every tick, so the two are
distinguishable by reading, and marks the UEs that were evicted.
"""
from __future__ import annotations

import sys

from harness import run
from scenario import build

SETTLE = 6
TOTAL = 60
EXPECTED_UES = 9


def main() -> int:
    cfg = build(seed=1)
    state: dict = {"seen": 0, "ticks": 0, "target": None, "off_at": None,
                   "evicted": set()}

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
        # exactly the UEs the engine will be asked to evict
        state["evicted"] = {u["imsi"] for u in (kpm.get("ues") or [])
                            if str(u.get("cell")) == str(target["cell"])}
        print(f"  [obs {state['seen']}] OFF cell {target['cell']}, "
              f"holding IMSIs {sorted(state['evicted'])}")
        return [{"cmd": "cell_off", "cell": target["cell"]}]

    log = run(cfg, controller, max_ticks=TOTAL)
    if not log.ok:
        print(f"FAILED: {log.error}")
        return 1
    cell, idx = state["target"], state["off_at"]
    if cell is None:
        print("FAILED: nothing to switch off")
        return 1

    imsis = sorted({u["imsi"] for t in log.ticks for u in t.ues})
    print(f"\nIMSI -> serving cell, per tick. '*' = was on cell {cell} at the "
          f"command (tick {idx}); '!' marks the barred cell {cell}.")
    print("        " + "".join(f"{'|' if i + 1 == idx else ' '}{i + 1:>3}"
                               for i in range(len(log.ticks))))
    for imsi in imsis:
        mark = "*" if imsi in state["evicted"] else " "
        row = ""
        for i, t in enumerate(log.ticks):
            on = next((u for u in t.ues if u["imsi"] == imsi), None)
            c = on.get("cell") if on else None
            cellstr = "-" if c is None else str(c)
            row += f"{'|' if i + 1 == idx else ' '}{cellstr:>2}"
            row += "!" if str(c) == str(cell) else " "
        print(f"  {mark}imsi {imsi:>2} {row}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
