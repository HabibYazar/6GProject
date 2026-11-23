"""
Pre-check 5 — is there an energy-capacity trade-off at all?

S2 measured, in 3 of 3 seeds, that switching a cell OFF lowered network power
AND raised aggregate throughput. That is not a trade-off; it is an
interference-limited deployment where less transmission is better on every axis.
Every S2 policy number therefore describes how aggressively a policy suppresses
an over-provisioned network, which is not the question the study asks.

The suspected cause is geometry: three cells ~700 m apart at 36 dBm overlap so
heavily that no single cell is load-bearing. This probe pushes the sites apart
and asks the only question that matters before rerunning anything:

    when a cell goes OFF, does aggregate throughput now FALL?

If it does, the conflict is real and S2 is worth rerunning. If it does not, the
scenario needs more than spacing and the rerun would waste two hours.

Usage:  python3 check5_geometry.py [spread]      (default 1.8 -> ~1260 m spacing)
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

from harness import run
from scenario import build
from xapps import PRB_LOW_PCT

EXPECTED_UES = 9
SETTLE_OBS = 8       # settled observations to characterise the "on" baseline
AFTER_OBS = 12       # settled observations after the cell goes off
OUT = Path(__file__).parent / "out" / "geometry.json"


def main() -> int:
    spread = float(sys.argv[1]) if len(sys.argv) > 1 else 1.8
    cfg = build(seed=1, spread=spread)
    xs = [b["position"]["x"] for b in cfg["base_stations"]]
    print(f"spread={spread}  cell x-positions {[round(x) for x in xs]}")

    state = {"settled": False, "obs": 0, "last": None, "off_cell": None, "off_at": None}
    raw: list[dict] = []

    def controller(kpm: dict):
        if not state["settled"]:
            if len(kpm.get("ues") or []) < EXPECTED_UES:
                return []
            state["settled"] = True
        key = ((kpm.get("energy") or {}).get("total_w"),
               tuple(sorted((c.get("cell"), c.get("prb")) for c in (kpm.get("cells") or []))))
        if key == state["last"]:
            return []
        state["last"] = key
        state["obs"] += 1
        raw.append({"obs": state["obs"], "kpm": kpm})

        if state["obs"] == SETTLE_OBS:
            # Switch off the cell eSave would pick: the least loaded one.
            cells = [c for c in (kpm.get("cells") or []) if c.get("prb") is not None]
            if not cells:
                return []
            target = min(cells, key=lambda c: c["prb"])
            state["off_cell"] = target["cell"]
            state["off_at"] = state["obs"]
            print(f"  obs {state['obs']}: switching OFF cell {target['cell']} "
                  f"(PRB {target['prb']:.0f}%, eSave floor {PRB_LOW_PCT})")
            return [{"cmd": "cell_off", "cell": target["cell"]}]
        return []

    log = run(cfg, controller, max_ticks=200,
              stop=lambda: state["obs"] >= SETTLE_OBS + AFTER_OBS)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"spread": spread, "error": log.error,
                               "off_cell": state["off_cell"], "obs": raw}, indent=1))
    if not log.ok or state["off_cell"] is None:
        print(f"\nFAILED: {log.error or 'never reached a full roster'}")
        return 1

    def dl(o):
        return sum(u.get("thp_kbps") or 0 for u in (o["kpm"].get("ues") or [])) / 1000

    def n_ues(o):
        return len(o["kpm"].get("ues") or [])

    before = [o for o in raw if o["obs"] < state["off_at"]]
    after = [o for o in raw if o["obs"] > state["off_at"]]
    if not before or not after:
        print("\nFAILED: not enough observations either side of the switch-off")
        return 1

    b_dl, a_dl = statistics.median(map(dl, before)), statistics.median(map(dl, after))
    b_ue, a_ue = statistics.median(map(n_ues, before)), statistics.median(map(n_ues, after))
    b_w = statistics.median([(o["kpm"].get("energy") or {}).get("total_w") or 0
                             for o in before])
    a_w = statistics.median([(o["kpm"].get("energy") or {}).get("total_w") or 0
                             for o in after])

    print(f"\ncell {state['off_cell']} switched off after {len(before)} observations")
    print(f"  DL throughput  {b_dl:>7.1f} -> {a_dl:>7.1f} Mbps  "
          f"({(a_dl - b_dl) / b_dl * 100:+.1f}%)")
    print(f"  network power  {b_w:>7.1f} -> {a_w:>7.1f} W     "
          f"({(a_w - b_w) / b_w * 100:+.1f}%)")
    print(f"  UEs reporting  {b_ue:>7.0f} -> {a_ue:>7.0f}")

    # The trade-off exists only if the throughput loss clears the measured noise
    # floor (~6.9% on this axis).
    lost = (b_dl - a_dl) / b_dl * 100
    print("\nVERDICT: " + (
        f"TRADE-OFF IS REAL at spread={spread} — switching a cell off costs "
        f"{lost:.1f}% throughput; S2 is worth rerunning"
        if lost > 6.9 else
        f"STILL NO TRADE-OFF at spread={spread} — throughput change {-lost:+.1f}% "
        f"is inside the noise floor; try a wider spread or more load"))
    return 0 if lost > 6.9 else 1


if __name__ == "__main__":
    sys.exit(main())
