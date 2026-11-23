"""
The write-up table — every arm normalised to its OWN seed's baseline.

Pooled means cannot be used here. The baseline itself moves a long way between
seeds (throughput 134.2 -> 162.5 Mbps, power 4315 -> 4660 W across three seeds of
the same config), so an average over seeds mixes the seed effect into the arm
effect and can invert a comparison. Every number below is therefore computed
inside one seed and only then compared across seeds.

Two derived quantities carry the argument:

  retained %   how much of the saving the energy xApp achieves UNOPPOSED (A - B)
               a given arm actually delivers. 100% means it matched eSave alone;
               0% means the arm saved nothing.
  throughput % change against the same seed's own baseline.

Both are reported as three per-seed values, never as a single mean: the spread
between them is the finding, not an error bar to be averaged away.

Noise floors measured from 13 byte-identical config pairs (arms issuing zero
control commands are the same run twice): power median 2.2% with one 15.6%
outlier, throughput median 2.4% / max 4.0%.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

SCEN = sys.argv[1] if len(sys.argv) > 1 else "matrix_s2_spread18"
OUT = Path(__file__).parent / "out" / SCEN
SEEDS = [1, 2, 3]
BASE, REF = "A_none", "B_esave"
THP_NOISE, PW_NOISE = 4.0, 5.8   # max observed, outlier excluded


def load() -> dict[tuple[str, int], dict]:
    d = {}
    for p in OUT.glob("*-s*.json"):
        if p.name.endswith(".FAILED.json"):
            continue
        j = json.loads(p.read_text())
        d[(j["arm"], j["seed"])] = j["metrics"]
    return d


def main() -> int:
    m = load()
    if not m:
        print(f"no runs in {OUT}")
        return 1
    arms = sorted({a for a, _ in m})
    seeds = [s for s in SEEDS if (BASE, s) in m and (REF, s) in m]
    print(f"{SCEN}  —  {len(m)} runs, seeds {seeds}\n")

    print("achievable saving per seed (baseline minus the unopposed energy xApp):")
    ach = {}
    for s in seeds:
        ach[s] = m[(BASE, s)]["mean_network_w"] - m[(REF, s)]["mean_network_w"]
        print(f"  seed {s}: {m[(BASE, s)]['mean_network_w']:.0f} W -> "
              f"{m[(REF, s)]['mean_network_w']:.0f} W  = {ach[s]:.0f} W "
              f"({ach[s] / m[(BASE, s)]['mean_network_w'] * 100:.1f}%)")

    print(f"\n{'arm':<12}{'retained % of the saving':>28}{'throughput % vs baseline':>28}"
          f"{'writes':>9}")
    for a in arms:
        ret, thp, wr = [], [], []
        for s in seeds:
            if (a, s) not in m:
                continue
            r = m[(a, s)]
            ret.append((m[(BASE, s)]["mean_network_w"] - r["mean_network_w"])
                       / ach[s] * 100)
            thp.append((r["dl_mbps_mean"] - m[(BASE, s)]["dl_mbps_mean"])
                       / m[(BASE, s)]["dl_mbps_mean"] * 100)
            wr.append(r["writes_total"])
        if not ret:
            continue
        rs = " ".join(f"{v:>7.1f}" for v in ret)
        ts = " ".join(f"{v:>+7.1f}" for v in thp)
        print(f"{a:<12}{rs:>28}{ts:>28}{statistics.mean(wr):>9.1f}")

    print("\nwhat survives the noise floor")
    for a in arms:
        if a == BASE:
            continue
        thp = [(m[(a, s)]["dl_mbps_mean"] - m[(BASE, s)]["dl_mbps_mean"])
               / m[(BASE, s)]["dl_mbps_mean"] * 100 for s in seeds if (a, s) in m]
        if not thp:
            continue
        clears = all(abs(v) > THP_NOISE for v in thp)
        same_sign = all(v > 0 for v in thp) or all(v < 0 for v in thp)
        wr = statistics.mean(m[(a, s)]["writes_total"] for s in seeds if (a, s) in m)
        note = ("no actuation at all" if wr == 0 else
                "throughput effect real in every seed" if clears and same_sign else
                "consistent direction, some seeds inside noise" if same_sign else
                "sign flips between seeds — not a stable effect")
        print(f"  {a:<12} {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
