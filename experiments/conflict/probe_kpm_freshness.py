"""
How much of each KPM frame is actually NEW?

The platform emits a frame on every "KPM ind_msg" line, but each gNB sends three
messages per indication round (CU-CP / CU-UP / DU) and only CU-CP carries the
serving-cell line. Measured once: 60 platform frames against 18 engine
indications. If throughput and PRB only refresh once per round while frames
arrive ~9x per round, then consecutive frames are not independent observations —
and every experiment that counts frames as samples is overcounting.

That was an inference. This measures it: for each field, how often does the
value actually CHANGE between consecutive frames? A field that changes on 1 of 9
frames is being repeated 8 times.

Reports per-field change rates, so the fix can be aimed at the fields that are
genuinely stale rather than at the frame rate as a whole. Positions and energy
are tailed from the ns-3 log at 10 Hz and are expected to look fresh; the E2 KPM
fields are the ones under suspicion.
"""
from __future__ import annotations

import sys
from collections import defaultdict

from harness import run
from scenario import build

TOTAL = 90
EXPECTED_UES = 9

# (label, extractor) — per-UE fields, keyed by imsi
UE_FIELDS = [
    ("cell         (POS 10Hz)", lambda u: u.get("cell")),
    ("x            (POS 10Hz)", lambda u: u.get("x")),
    ("sinr           (E2 KPM)", lambda u: u.get("sinr")),
    ("thp_kbps       (E2 KPM)", lambda u: u.get("thp_kbps")),
    ("prb            (E2 KPM)", lambda u: u.get("prb")),
    ("ul_kbps        (E2 KPM)", lambda u: u.get("ul_kbps")),
    ("delay_ms       (E2 KPM)", lambda u: u.get("delay_ms")),
]


def main() -> int:
    cfg = build(seed=1)
    seen = {"n": 0}

    def controller(kpm: dict):
        seen["n"] += 1
        return []

    print(f"collecting {TOTAL} frames…")
    log = run(cfg, controller, max_ticks=TOTAL)
    if not log.ok:
        print(f"FAILED: {log.error}")
        return 1

    frames = log.ticks
    print(f"\n{len(frames)} frames collected\n")

    # per-field: how many consecutive frame pairs changed the value, per UE
    changes: dict[str, int] = defaultdict(int)
    pairs: dict[str, int] = defaultdict(int)
    prev: dict[tuple[str, int], object] = {}
    for t in frames:
        for u in t.ues:
            imsi = u.get("imsi")
            if imsi is None:
                continue
            for label, get in UE_FIELDS:
                v = get(u)
                k = (label, imsi)
                if k in prev:
                    pairs[label] += 1
                    if prev[k] != v:
                        changes[label] += 1
                prev[k] = v

    print("per-UE field freshness (share of consecutive frames where it changed)")
    print(f"  {'field':<26} {'changed':>8} {'pairs':>8} {'rate':>7}  repeat factor")
    for label, _ in UE_FIELDS:
        p, c = pairs[label], changes[label]
        if not p:
            continue
        rate = c / p
        rep = (1 / rate) if rate else float("inf")
        bar = "#" * min(30, int(rate * 30))
        print(f"  {label:<26} {c:>8} {p:>8} {rate:>6.1%}  "
              f"{'inf' if rep == float('inf') else f'{rep:.1f}x'} {bar}")

    # per-cell energy, the other 10 Hz source
    e_ch = e_pa = 0
    prev_e: dict[str, float] = {}
    for t in frames:
        for cell, w in (t.per_cell_w or {}).items():
            if cell in prev_e:
                e_pa += 1
                if prev_e[cell] != w:
                    e_ch += 1
            prev_e[cell] = w
    if e_pa:
        print(f"\n  {'per-cell watts (tail 10Hz)':<26} {e_ch:>8} {e_pa:>8} "
              f"{e_ch/e_pa:>6.1%}")

    print("\nReading it: a field changing on ~1 frame in N is being repeated N-1\n"
          "times. Experiments counting frames as independent samples inflate\n"
          "their n by that factor for those fields.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
