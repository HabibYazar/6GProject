"""
Pre-checks 2 and 5 — how many control opportunities does a run really offer,
and how much simulated time does it actually cover?

Check 1 showed indications arriving in BURSTS: nine of them stamped within
0.2 s of each other, then a ~50 s wall gap, then nine more. An xApp that "acts
every indication" would in that case be acting three times on three distinct
engine states and twenty-seven times on stale ones. The oscillation the study
wants to measure needs distinct control moments, so the honest count is the
number of bursts, not the number of frames.

This run takes no control action at all — it only watches, so the cadence
measured here is the engine's own, not a response to anything we did.

Simulated duration is recovered rather than read: the live frame carries no
timestamp, but [NSG-ENERGY] reports every 0.1 simulated seconds with cumulative
joules `j` and window power `w`, so elapsed simulated time = Δj / mean(w).
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

from harness import run
from scenario import build

TOTAL = 40
OUT = Path(__file__).parent / "out" / "cadence.json"
# Two indications closer together than this came off the same engine burst: the
# xApp cannot have influenced anything between them.
BURST_GAP_S = 1.0


def main() -> int:
    # Settings overrides as k=v on the command line, so the cost knobs can be
    # probed without editing the scenario: `check2_cadence.py channel_update_ms=500`
    over: dict = {}
    for arg in sys.argv[1:]:
        k, _, v = arg.partition("=")
        over[k] = float(v) if "." in v else int(v)
    if over:
        print(f"settings override: {over}")
    cfg = build(seed=1, settings=over)
    raw: list[dict] = []

    def observer(kpm: dict):
        raw.append(kpm)
        return []

    print(f"observing {TOTAL} indications, no control…")
    log = run(cfg, observer, max_ticks=TOTAL,
              on_tick=lambda t: print(f"  tick {t.tick:>3} {t.wall_s:>7.2f}s "
                                      f"cells={len(t.cells)} ues={len(t.ues)} "
                                      f"W={t.energy_w}", flush=True))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(
        {"frames": raw, "wall_s": [t.wall_s for t in log.ticks],
         "error": log.error, "boot_s": log.boot_s}, indent=1))
    print(f"\nraw frames -> {OUT}")

    if not log.ok:
        print(f"FAILED: {log.error}")
        return 1

    walls = [t.wall_s for t in log.ticks]
    gaps = [round(b - a, 2) for a, b in zip(walls, walls[1:])]
    bursts = 1 + sum(1 for g in gaps if g > BURST_GAP_S)
    big = [g for g in gaps if g > BURST_GAP_S]

    print(f"\n[check 2] control opportunities")
    print(f"  indications          {len(log.ticks)}")
    print(f"  distinct bursts      {bursts}   <- real control moments")
    print(f"  gaps > {BURST_GAP_S}s          {len(big)}  "
          f"{'median ' + format(statistics.median(big), '.1f') + 's' if big else ''}")
    print(f"  wall clock           {log.wall_s}s")
    print(f"  verdict              " + (
        "PASS — >=20 distinct control moments" if bursts >= 20 else
        f"FAIL — only {bursts} distinct control moments, design wants >=20"))

    # Attach ramp: early indications describe a network still coming up, and any
    # metric averaged over them describes the ramp, not the policy.
    ues = [len(f.get("ues") or []) for f in raw]
    full = max(ues) if ues else 0
    settled = next((i for i, n in enumerate(ues) if n >= full), None)
    print(f"\n[ramp] UEs attached per indication: {ues}")
    print(f"  full roster ({full} UEs) first seen at indication {settled}")

    # Simulated duration, recovered from the energy stream.
    def jsum(f) -> float | None:
        e = (f.get("energy") or {}).get("per_cell") or {}
        return sum(v["j"] for v in e.values() if isinstance(v, dict) and "j" in v) or None

    js = [jsum(f) for f in raw]
    ws = [(f.get("energy") or {}).get("total_w") for f in raw]
    js = [v for v in js if v]
    ws = [v for v in ws if v]
    print(f"\n[check 5] achieved simulated duration")
    if len(js) >= 2 and ws:
        dj, mw = js[-1] - js[0], statistics.mean(ws)
        print(f"  cumulative J   {js[0]:.1f} -> {js[-1]:.1f}  (delta {dj:.1f} J)")
        print(f"  mean network W {mw:.1f}")
        print(f"  simulated span ~{dj / mw:.3f} s over {log.wall_s}s wall")
        print(f"  note           the headline metric is W (a rate), so it is "
              f"already duration-normalised")
    else:
        print("  energy stream too sparse to recover a span")
    return 0


if __name__ == "__main__":
    sys.exit(main())
