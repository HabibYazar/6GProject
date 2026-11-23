"""
Read the matrix and say what it supports — including what it does NOT support.

Three seeds is enough to see a large effect and not enough to see a small one,
and the difference between those two cases is the whole question. So every
pairwise arm comparison is reported against the seed spread that produced it: a
gap smaller than the noise is printed as noise-limited, not as a result. That
list is the shopping list for extra seeds — the point of starting at three is to
spend the fourth and fifth only where they change a conclusion.

Runs on partial output, so it can be called while the matrix is still going.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

SCENARIO = "s2" if "--s2" in sys.argv else "s1"
OUT = Path(__file__).parent / "out" / ("matrix" if SCENARIO == "s1"
                                       else f"matrix_{SCENARIO}")

# The metrics the paper reports. Direction is what "better" means, and is used
# only to phrase the comparison, never to filter it.
HEADLINE = [
    ("mean_network_w", "mean network power (W)", "lower"),
    ("dl_mbps_mean", "DL throughput (Mbps)", "higher"),
    ("ue_thp_p5_mbps", "5th-pct UE throughput (Mbps)", "higher"),
    ("contested_intervals", "contested intervals", "—"),
    ("writes_total", "control writes", "—"),
    ("rail_hits", "rail contacts", "lower"),
]

ARM_ORDER = ["A_none", "B_esave", "C_maxtp",
             "D_both_P0", "E_both_P1", "F_both_P2", "G_both_P3"]


def contested_cell(ticks: list[dict]) -> str | None:
    """The cell both xApps actually fought over, in THIS run.

    Selected by cause, not by effect. An earlier version picked whichever cell
    had the highest power variance and then reported that variance, which is
    circular — it selects on the outcome and inflates it, and it reordered the
    headline result. Contention is recorded independently: an outcome with more
    than one request is a cell two xApps both tried to write.
    """
    counts: dict[str, int] = {}
    for t in ticks:
        for o in (t.get("note", {}).get("outcomes") or []):
            if o.get("n_requests", 0) > 1:
                counts[str(o["cell"])] = counts.get(str(o["cell"]), 0) + 1
    return max(counts, key=counts.get) if counts else None


def load() -> dict[str, list[dict]]:
    by_arm: dict[str, list[dict]] = {}
    for p in sorted(OUT.glob("*-s*.json")):
        if p.name.endswith(".FAILED.json"):
            continue
        d = json.loads(p.read_text())
        by_arm.setdefault(d["arm"], []).append({
            "seed": d["seed"], "contested_cell": contested_cell(d.get("ticks") or []),
            **d["metrics"]})
    return by_arm


def osc(runs: list[dict]) -> tuple[float | None, float | None, str]:
    """Oscillation on the contested cell of each run, averaged over seeds.

    The contested cell is found per run, not fixed: in S1 it was reliably cell 2,
    but in S2 the cells eSave switched off varied with the seed (2 and 3 in one,
    4 and 3 in another). Reading a hard-coded cell id would have reported the
    oscillation of a cell nobody was fighting over.

    "Contested" here means the cell that received the most writes — the actuator
    the policies actually had to arbitrate.
    """
    var, flip, cells = [], [], []
    for r in runs:
        cell = r.get("contested_cell")
        if cell is None:                      # single-xApp arms never contend
            continue
        cells.append(cell)
        var.append((r.get("power_var_by_cell") or {}).get(cell, 0.0))
        flip.append((r.get("power_flips_by_cell") or {}).get(cell, 0))
    return (round(statistics.mean(var), 2) if var else None,
            round(statistics.mean(flip), 2) if flip else None,
            ",".join(sorted(set(cells))) or "—")


def main() -> int:
    by_arm = load()
    if not by_arm:
        print(f"no runs in {OUT} yet")
        return 1
    done = sum(len(v) for v in by_arm.values())
    print(f"{done}/21 runs present\n")

    arms = [a for a in ARM_ORDER if a in by_arm]
    w = max(len(a) for a in arms)

    for key, label, better in HEADLINE:
        print(f"{label}" + (f"  ({better} is better)" if better != "—" else ""))
        for a in arms:
            vals = [r[key] for r in by_arm[a] if r.get(key) is not None]
            if not vals:
                continue
            spread = max(vals) - min(vals) if len(vals) > 1 else 0.0
            print(f"  {a:<{w}}  n={len(vals)}  mean {statistics.mean(vals):>9.2f}"
                  f"   spread {spread:>8.2f}   {[round(v, 1) for v in vals]}")
        print()

    print("oscillation on each run's contested cell (the busiest actuator)")
    for a in arms:
        v, f, cells = osc(by_arm[a])
        print(f"  {a:<{w}}  variance {v}   direction-flips {f}   cell(s) {cells}")

    print("\npolicy bookkeeping")
    for a in arms:
        rs = by_arm[a]
        print(f"  {a:<{w}}  suppressed {statistics.mean(r['suppressed'] for r in rs):>5.1f}"
              f"   rolled-back {statistics.mean(r['rolled_back'] for r in rs):>5.1f}"
              f"   synthesised {statistics.mean(r['synthesised'] for r in rs):>5.1f}")

    # The noise gate. A difference is only reportable if it clears the spread
    # the seeds themselves produced.
    print("\nwhich comparisons the data can actually carry")
    policies = [a for a in arms if a.startswith(("D_", "E_", "F_", "G_"))]
    noisy: list[str] = []
    for key, label, _ in HEADLINE[:3]:
        print(f"\n  {label}")
        for i, a in enumerate(policies):
            for b in policies[i + 1:]:
                va = [r[key] for r in by_arm[a] if r.get(key) is not None]
                vb = [r[key] for r in by_arm[b] if r.get(key) is not None]
                if len(va) < 2 or len(vb) < 2:
                    continue
                gap = abs(statistics.mean(va) - statistics.mean(vb))
                noise = max(max(va) - min(va), max(vb) - min(vb))
                ok = gap > noise
                print(f"    {a} vs {b}: gap {gap:>8.2f}  seed-noise {noise:>8.2f}  "
                      f"{'SEPARABLE' if ok else 'noise-limited'}")
                if not ok:
                    noisy.append(f"{a} vs {b} on {key}")

    if noisy:
        print(f"\n{len(noisy)} comparison(s) need more seeds before they can be "
              f"claimed:")
        for n in noisy:
            print(f"  · {n}")
    else:
        print("\nevery policy comparison clears its seed noise at n=3")
    return 0


if __name__ == "__main__":
    sys.exit(main())
