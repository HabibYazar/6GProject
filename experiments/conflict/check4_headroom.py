"""
Pre-check 4 — does the conflict survive dropping the base power to 36 dBm?

Lowering the baseline from 43 dBm buys both controllers room to act without
meeting a rail, which the study needs. But it is not free: it is a real change to
the radio, and the conflict this experiment measures is manufactured by a
specific geometry — the edge cluster whose serving cell is simultaneously lightly
loaded (eSave's trigger) and badly served (maxTP's trigger). Cutting 7 dB could
destroy that in either direction: the cell could lose its UEs entirely, or the
edge UEs could drop below usable service and stop reporting at all.

So this observes a run at 36 dBm with NO control at all and asks three questions:
  1. are the UEs still served (non-zero throughput, full roster attaches)?
  2. does eSave still have a cell to act on (PRB below its floor)?
  3. does maxTP still have a cell to act on (worst UE SINR below its floor)?
  4. is there at least one cell where BOTH are true — the contested cell?
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

from harness import run
from scenario import build, BASE_TX_DBM
from xapps import PRB_LOW_PCT, SINR_LOW_DB

TOTAL = 45
EXPECTED_UES = 9
OUT = Path(__file__).parent / "out" / "headroom.json"


def main() -> int:
    cfg = build(seed=1)
    raw: list[dict] = []

    def observe(kpm: dict):
        raw.append(kpm)
        return []

    print(f"observing {TOTAL} indications at {BASE_TX_DBM:.0f} dBm, no control…")
    log = run(cfg, observe, max_ticks=TOTAL,
              on_tick=lambda t: print(f"  tick {t.tick:>3} {t.wall_s:>7.1f}s "
                                      f"ues={len(t.ues)} cells={len(t.cells)} "
                                      f"W={t.energy_w}", flush=True))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"frames": raw, "error": log.error}, indent=1))
    if not log.ok:
        print(f"\nFAILED: {log.error}")
        return 1

    # Only the settled part of the run says anything about steady state.
    ramp_end = next((i for i, f in enumerate(raw)
                     if len(f.get("ues") or []) >= EXPECTED_UES), None)
    if ramp_end is None:
        best = max(len(f.get("ues") or []) for f in raw)
        print(f"\nFAILED: roster never reached {EXPECTED_UES} UEs (best {best}) — "
              f"at {BASE_TX_DBM:.0f} dBm some UEs cannot attach")
        return 1
    settled = raw[ramp_end:]
    print(f"\nramp ends at indication {ramp_end}/{len(raw)}; "
          f"{len(settled)} settled indications")

    thp = [sum(u.get("thp_kbps") or 0 for u in (f.get("ues") or [])) / 1000
           for f in settled]
    print(f"served DL throughput: median {statistics.median(thp):.1f} Mbps "
          f"(min {min(thp):.1f}, max {max(thp):.1f})")

    esave_cells, maxtp_cells = set(), set()
    print(f"\nper-cell over the settled window "
          f"(eSave fires PRB<{PRB_LOW_PCT}, maxTP fires minSINR<{SINR_LOW_DB}):")
    cells = sorted({c["cell"] for f in settled for c in (f.get("cells") or [])})
    for cid in cells:
        prb = [c.get("prb") for f in settled for c in (f.get("cells") or [])
               if c["cell"] == cid and c.get("prb") is not None]
        worst = []
        for f in settled:
            s = [u.get("sinr") for u in (f.get("ues") or [])
                 if u.get("cell") == cid and u.get("sinr") is not None]
            if s:
                worst.append(min(s))
        if not prb or not worst:
            continue
        mprb, mworst = statistics.median(prb), statistics.median(worst)
        e_hit = mprb < PRB_LOW_PCT
        m_hit = mworst < SINR_LOW_DB
        if e_hit:
            esave_cells.add(cid)
        if m_hit:
            maxtp_cells.add(cid)
        print(f"  cell {cid}: PRB med {mprb:>5.1f}%{'  <-eSave' if e_hit else '        '}"
              f"   worst-UE SINR med {mworst:>6.1f} dB{'  <-maxTP' if m_hit else ''}")

    both = esave_cells & maxtp_cells
    print(f"\neSave has {sorted(esave_cells) or '—'}, maxTP has {sorted(maxtp_cells) or '—'}")
    ok = bool(both) and min(thp) > 0
    print("\nVERDICT: " + (
        f"HEADROOM FIX HOLDS — cell(s) {sorted(both)} still contested at "
        f"{BASE_TX_DBM:.0f} dBm, UEs still served"
        if ok else
        f"CONFLICT LOST at {BASE_TX_DBM:.0f} dBm — no cell triggers both xApps; "
        f"the geometry needs retuning before the matrix runs"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
