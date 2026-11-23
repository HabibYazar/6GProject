#!/usr/bin/env python3
"""What the fast tier's physics gets right, and the one thing it does not.

`window_model` derives every constant from the engine's source, so it has
nothing fitted in it. That is worth checking rather than believing, and the
check splits cleanly in two:

  STAGE 1 — the transport-block chain. Given the symbol share and modulation
  THE ENGINE ITSELF REPORTED, does `tb_bytes` predict the bytes the engine
  delivered? This uses no geometry, no scheduler and no SINR of ours, so a
  disagreement here is the physics and nothing else.

  STAGE 2 — geometry and scheduling. Does our own SINR and symbol sharing
  reproduce the engine's share and modulation in the first place? That is the
  harder half and it is not what this tool measures.

Stage 1 is what you can run with no engine at all: it reads Tier A runs
already in Postgres. The number it produces is the DELIVERY EFFICIENCY — bytes
out over bytes the allocation could have carried — which absorbs HARQ
retransmissions, RLC and MAC headers, and slots the scheduler left idle. If
one value holds across loaded and unloaded cells then it is a constant; if it
splits by regime, it is not, and the tool says so instead of averaging over
the distinction.
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "nsg_ai"))

from dotenv import load_dotenv

import window_model as wm
from mcs import ECR_FOR_MCS, QM_FOR_MCS

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"

# CARR.PDSCHMCSDist bins, from mmwave-enb-net-device.cc: GetMacMcs04/59/1014/
# 1519/2024/2529UeSpecific. The representative MCS is the bin's midpoint; a bin
# is 5 wide, so this alone is worth a few per cent and the tool says so.
MCS_BINS = [("CARR.PDSCHMCSDist.Bin1", 2), ("CARR.PDSCHMCSDist.Bin2", 7),
            ("CARR.PDSCHMCSDist.Bin3", 12), ("CARR.PDSCHMCSDist.Bin4", 17),
            ("CARR.PDSCHMCSDist.Bin5", 22), ("CARR.PDSCHMCSDist.Bin6", 27)]


def connect():
    here = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(here, "..", ".env"))
    import psycopg
    url = os.environ["NSG_DATABASE_URL"].replace("postgresql+psycopg", "postgresql")
    return psycopg.connect(url)


def band_of(config) -> tuple[float, int]:
    bss = config.get("base_stations") or []
    band = ((bss[0] or {}).get("radio") or {}).get("band") or {}
    return float(band.get("bandwidth_mhz") or 50.0), int(band.get("numerology") or 2)


def mean_mcs(m: dict) -> float | None:
    """Weighted mean MCS from the engine's own six-bin histogram."""
    num = den = 0.0
    for name, mid in MCS_BINS:
        v = (m.get(name) or {}).get("mean")
        if v:
            num += v * mid
            den += v
    return num / den if den else None


def stage1(run_id: str, frames: list, config) -> list[dict]:
    """Per frame: predicted bytes from the engine's own allocation vs measured."""
    bw_mhz, cfg_mu = band_of(config)
    mu = wm.engine_numerology(cfg_mu)
    n_rb = wm.rb_count(bw_mhz, mu)
    slots = round(1.0 / wm.slot_period_s(mu))
    rows = []
    for f in frames:
        m = f.get("measurements") or {}
        prb = (m.get("RRU.PrbUsedDl") or {}).get("mean")
        tb = (m.get("TB.TotNbrDl.1") or {}).get("mean")
        vol = (m.get("QosFlow.PdcpPduVolumeDL") or {}).get("mean")
        if not prb or not tb or not vol:
            continue
        mcs = mean_mcs(m)
        if mcs is None:
            continue
        # Invert the engine's own PRB metric back to symbols:
        #   prb = symbols / (slots * 14) * 139
        symbols = prb / wm.PRB_METRIC_SCALE * slots * wm.SYMBOLS_PER_SLOT
        lo, hi = int(mcs), min(28, int(mcs) + 1)
        frac = mcs - lo
        per_sym = ((1 - frac) * wm._payload_bytes(lo, 1.0, n_rb)
                   + frac * wm._payload_bytes(hi, 1.0, n_rb))
        predicted = per_sym * symbols
        rows.append({
            "sim_t": f.get("sim_t"), "prb": prb, "tb": tb, "mcs": round(mcs, 1),
            "symbols": symbols, "predicted": predicted, "measured": vol,
            "eff": vol / predicted if predicted > 0 else None,
            "saturated": tb >= 0.95 * slots,
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", help="comma-separated run ids (default: every run that has them)")
    ap.add_argument("--limit", type=int, default=12)
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor()
    if args.runs:
        ids = [r.strip() for r in args.runs.split(",") if r.strip()]
    else:
        cur.execute("select id from runs where n_frames >= 10 "
                    "order by started_at desc limit %s", (args.limit,))
        ids = [r[0] for r in cur.fetchall()]

    print(f"stage 1 — the transport-block chain, on {len(ids)} stored Tier A run(s)\n")
    print(f"{'run':<16}{'cells':>6}{'frames':>8}{'mean MCS':>10}{'PRB':>7}"
          f"{'predicted MB':>14}{'measured MB':>13}{'efficiency':>12}")
    all_eff, sat_eff, unsat_eff = [], [], []
    for rid in ids:
        cur.execute("select config from runs where id=%s", (rid,))
        row = cur.fetchone()
        if not row:
            continue
        config = row[0]
        cur.execute("select payload from run_frames where run_id=%s order by tick", (rid,))
        frames = [r[0] for r in cur.fetchall()]
        rows = stage1(rid, frames, config)
        if not rows:
            print(f"{rid:<16}{DIM}  no frame carries the DU counters{RESET}")
            continue
        effs = [r["eff"] for r in rows if r["eff"]]
        all_eff += effs
        sat_eff += [r["eff"] for r in rows if r["saturated"] and r["eff"]]
        unsat_eff += [r["eff"] for r in rows if not r["saturated"] and r["eff"]]
        print(f"{rid:<16}{len(config.get('base_stations') or []):>6}{len(rows):>8}"
              f"{statistics.fmean(r['mcs'] for r in rows):>10.1f}"
              f"{statistics.fmean(r['prb'] for r in rows):>7.1f}"
              f"{statistics.fmean(r['predicted'] for r in rows)/1e6:>14.2f}"
              f"{statistics.fmean(r['measured'] for r in rows)/1e6:>13.2f}"
              f"{statistics.fmean(effs):>12.3f}")

    if not all_eff:
        print(f"\n{RED}nothing to calibrate on{RESET} — no stored run carries the DU counters.")
        return 1

    print(f"\ndelivery efficiency over {len(all_eff)} frame(s): "
          f"mean {statistics.fmean(all_eff):.3f}, "
          f"median {statistics.median(all_eff):.3f}, "
          f"range {min(all_eff):.3f}-{max(all_eff):.3f}")
    if sat_eff and unsat_eff:
        s, u = statistics.fmean(sat_eff), statistics.fmean(unsat_eff)
        print(f"  cells scheduled in every slot  n={len(sat_eff):<5} {s:.3f}")
        print(f"  cells with slots to spare      n={len(unsat_eff):<5} {u:.3f}")
        if max(s, u) / max(min(s, u), 1e-9) > 1.25:
            print(f"\n{YELLOW}This is not one constant.{RESET} The two regimes differ by "
                  f"{max(s,u)/min(s,u):.2f}x, so a single efficiency would be wrong in both. "
                  f"The model needs the distinction, not an average of it.")
        else:
            print(f"\n{GREEN}One constant covers both regimes{RESET} "
                  f"({max(s,u)/min(s,u):.2f}x apart).")
    print(f"\n{DIM}Not measured here: whether our own SINR and scheduler reproduce the "
          f"symbol share and modulation this used as INPUT. That is stage 2.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
