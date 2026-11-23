#!/usr/bin/env python3
"""Does the fast tier hold OUTSIDE the scenario it was built against?

`tier_agreement --sweep` compares the tiers on twelve runs that happen to be
on disk, and they are all one family: 3.6 GHz, 50 MHz, UMi, flat ground, no
buildings, nothing moving fast, no slice policy, no cell switched off. Every
agreement number this project has is from inside that box.

This walks out of it. Each case takes the reference scenario and changes ONE
thing the engine supports and Tier B has never been checked against, runs BOTH
tiers, and prints the per-metric ratio. A case that disagrees names a feature
Tier B does not have rather than a number that is off.

    python3 tools/tier_breadth.py                # every case
    python3 tools/tier_breadth.py --case buildings
    python3 tools/tier_breadth.py --list

It saves after every case and skips what is already saved, because each case
costs a real engine run and a crash four cases in should not cost the first
three. Delete `tier_breadth.json` to start over.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "nsg_ai"))
sys.path.insert(0, HERE)

import tier_criterion as tc

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"
OUT = os.path.join(HERE, "tier_breadth.json")

# The metrics worth judging a feature on. Extensive ones are harvested through
# the full-window estimator — see tier_criterion.EXTENSIVE, and the commit that
# put it there.
JUDGED = [
    ("TB.TotNbrDl.1", "blocks"),
    ("RRU.PrbUsedDl", "spectrum"),
    ("QosFlow.PdcpPduVolumeDL", "volume"),
    ("DRB.BufferSize.Qos", "queued"),
    ("block error rate", "errors"),
    ("64QAM share", "64QAM%"),
    ("handovers", "handovers"),
]


def case_baseline(cfg):
    """The family everything so far was measured in — the control."""
    return cfg


def case_buildings(cfg):
    """Buildings make the engine swap its channel-condition model for a
    GEOMETRIC one, so LOS stops being a coin and becomes a ray test. Tier B
    follows it now; nothing has ever checked that it follows it correctly."""
    from schema import BoundingBox3D, Building
    cx = statistics.fmean(b.position.x for b in cfg.base_stations)
    cy = statistics.fmean(b.position.y for b in cfg.base_stations)
    cfg.buildings = [
        Building(id=f"b{i}",
                 bounds=BoundingBox3D(x_min=cx + dx - 40, x_max=cx + dx + 40,
                                      y_min=cy + dy - 40, y_max=cy + dy + 40,
                                      z_min=0.0, z_max=25.0))
        for i, (dx, dy) in enumerate(((-150, -150), (150, -150),
                                      (-150, 150), (150, 150)))
    ]
    return cfg


def case_mobility(cfg):
    """UEs fast enough to cross boundaries inside the run. Handovers stop
    being structurally zero and start being a number that can be wrong."""
    for g in cfg.ue_groups:
        g.mobility.speed_mps = 25.0
    return cfg


def case_slice_quota(cfg):
    """A slice policy the engine enforces in the MAC. Tier B applies the cap
    as a post-scale on delivered throughput, which is NOT the same thing as
    reserving symbols, and this is the first time anything has compared the
    two."""
    from schema import NetworkSlice, SlicePolicy
    half = len(cfg.ue_groups) // 2
    for i, g in enumerate(cfg.ue_groups):
        for svc in g.services:
            svc.slice_type = NetworkSlice.URLLC if i < half else NetworkSlice.EMBB
            svc.qos_id = 3 if i < half else 9
    # QCI is what the engine partitions on, not the slice label.
    cfg.slices = [SlicePolicy(qos_id=3, max_pct=70.0, min_pct=40.0),
                  SlicePolicy(qos_id=9, max_pct=30.0)]
    return cfg


def case_narrow_band(cfg):
    """10 MHz instead of 50. The RB count, the noise floor and the per-UE
    bandwidth all move together, and MIN_MHZ_PER_UE starts to bite."""
    for b in cfg.base_stations:
        b.radio.band.bandwidth_mhz = 10.0
    for g in cfg.ue_groups:
        g.count = 2
    return cfg


def case_cell_off(cfg):
    """One cell asleep. Its UEs have to land somewhere, and both tiers have to
    agree on where."""
    cfg.base_stations[0].radio.tx_power_dbm = 0.0
    return cfg


CASES = {
    "baseline": case_baseline,
    "buildings": case_buildings,
    "mobility": case_mobility,
    "slice_quota": case_slice_quota,
    "narrow_band": case_narrow_band,
    "cell_off": case_cell_off,
}


def _windows_for(params: dict, span_s: float | None) -> int:
    """How many Tier B windows cover the simulated time Tier A reached.

    THE TWO TIERS DO NOT GET THE SAME CLOCK FOR FREE. Tier A's 40 frames
    span about 2 simulated seconds — that is the whole horizon a batch run
    buys. Tier B's 40 windows are 40 SECONDS, because its window is one
    second by default. For a per-window counter that does not matter; for
    anything that ACCUMULATES it is the whole comparison, and the queue
    column was measuring it: Tier B pinned at its 10 MB RLC buffer in
    every case while the engine sat at 5.5-6.6 MB, which read as a
    systematic 1.7-1.9x model error and was 20x of extra simulated time.
    Matched, it is 1.07-1.43.
    """
    if not span_s or span_s <= 0:
        return 40
    try:
        import tier_b
        w = tier_b.window_s(params)
    except Exception:                                    # noqa: BLE001
        return 40
    return max(1, round(float(span_s) / max(w, 1e-9)))


def _tier_b_frames_now(name: str, span_s: float | None = None) -> list | None:
    """The CURRENT model's frames for a case, for anything the harvest
    flattens away — a distribution, for instance."""
    try:
        import copy
        import tier_b
        from main import nsoran_params
        from nsoran_live import _rate_of_imsi_map
        crit = json.load(open(os.path.join(HERE, "tier_criterion.json")))["scenario"]
        _n, base, _p = tc.load_scenario(crit[:20])
        cfg = CASES[name](copy.deepcopy(base))
        params = nsoran_params(cfg)
        demand = {i: m * 1e6 for i, m in _rate_of_imsi_map(cfg).items()}
        return tier_b.frames(params, _windows_for(params, span_s),
                             demand_bps=demand)
    except Exception:                                    # noqa: BLE001
        return None


def _tier_b_now(name: str, span_s: float | None = None) -> dict | None:
    """Tier B on the CURRENT model, for the case's own scenario."""
    try:
        import copy
        import tier_b
        from main import nsoran_params
        from nsoran_live import _rate_of_imsi_map
        crit = json.load(open(os.path.join(HERE, "tier_criterion.json")))["scenario"]
        _n, base, _p = tc.load_scenario(crit[:20])
        cfg = CASES[name](copy.deepcopy(base))
        params = nsoran_params(cfg)
        demand = {i: m * 1e6 for i, m in _rate_of_imsi_map(cfg).items()}
        import statistics as _st
        n_win = _windows_for(params, span_s)
        runs = [tc.harvest(tier_b.frames(params, n_win, demand_bps=demand,
                                         seed=s))
                for s in range(tc.TIER_B_REALISATIONS)]
        out = {}
        for k in set().union(*(r.keys() for r in runs)):
            vals = [r[k] for r in runs if isinstance(r.get(k), (int, float))]
            out[k] = _st.fmean(vals) if vals else runs[0].get(k)
        return out
    except Exception as exc:
        print(f"{YELLOW}  (could not recompute Tier B for {name}: "
              f"{type(exc).__name__}){RESET}")
        return None


def ratio_row(a: dict, b: dict) -> list:
    out = []
    for key, label in JUDGED:
        va, vb = a.get(key), b.get(key)
        if va is None or vb is None:
            out.append((label, va, vb, None))
        elif va == 0:
            out.append((label, va, vb, None if vb == 0 else float("inf")))
        else:
            out.append((label, va, vb, vb / va))
    return out


def colour(r) -> str:
    if r is None:
        return f"{DIM}{'n/a':>8}{RESET}"
    if r == float("inf"):
        return f"{RED}{'inf':>8}{RESET}"
    c = GREEN if 0.8 <= r <= 1.25 else YELLOW if 0.6 <= r <= 1.7 else RED
    return f"{c}{r:>8.2f}{RESET}"


async def run_case(name: str, frames: int, wall_cap: int) -> dict:
    import tier_b
    from main import nsoran_params
    from nsoran_live import _rate_of_imsi_map

    crit_name = json.load(open(os.path.join(HERE, "tier_criterion.json")))["scenario"]
    _n, base_cfg, _p = tc.load_scenario(crit_name[:20])
    cfg = CASES[name](copy.deepcopy(base_cfg))
    params = nsoran_params(cfg)

    # The engine takes one run at a time and does not queue: a case started
    # before the last one let go comes straight back as "busy" with no data,
    # and a harness that recorded that as a result would report two of six
    # cases unmeasured and call it a finding. Wait and retry instead.
    t = time.time()
    for attempt in range(6):
        r, _w = await tc.run_tier_a(cfg, params, frames, wall_cap)
        if r.get("status") != "busy":
            break
        print(f"  engine busy, waiting 60 s (attempt {attempt + 1}/6)", flush=True)
        await asyncio.sleep(60)
    wall = time.time() - t
    ha = tc.harvest(r["frames"])

    demand = {i: m * 1e6 for i, m in _rate_of_imsi_map(cfg).items()}
    t2 = time.time()
    # MATCHED TO WHAT TIER A ACTUALLY REACHED, not to the frame count. See
    # `_windows_for`: 40 frames of Tier A is 2 simulated seconds and 40
    # windows of Tier B is 40.
    n_win = _windows_for(params, ha.get("_sim_span_s"))
    frames_b = tier_b.frames(params, n_win, demand_bps=demand)
    import statistics as _st
    runs = [tc.harvest(tier_b.frames(params, n_win, demand_bps=demand, seed=s))
            for s in range(tc.TIER_B_REALISATIONS)]
    hb = {}
    for k in set().union(*(r.keys() for r in runs)):
        vals = [r[k] for r in runs if isinstance(r.get(k), (int, float))]
        hb[k] = _st.fmean(vals) if vals else runs[0].get(k)
    wall_b = time.time() - t2

    return {"case": name, "status": r.get("status"), "wall_a": round(wall, 1),
            "wall_b": round(wall_b, 3), "span": ha.get("_sim_span_s"),
            "n_ue": params["n_ue"], "n_gnb": params["n_gnb"],
            "tier_a": {k: v for k, v in ha.items()},
            "tier_b": {k: v for k, v in hb.items()},
            # The FRAMES, not just the harvest. The criterion learned this the
            # expensive way: its estimator changed twice and each change cost
            # six engine runs, because only the summary had been kept. A case
            # here costs eight minutes; re-deriving it should cost nothing.
            "frames_a": r["frames"], "frames_b": frames_b}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", action="append", help="run only these (repeatable)")
    ap.add_argument("--frames", type=int, default=40)
    ap.add_argument("--wall-cap", type=int, default=900)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--report", action="store_true", help="print what is saved, run nothing")
    args = ap.parse_args()

    if args.list:
        for k, fn in CASES.items():
            print(f"  {k:<14}{(fn.__doc__ or '').strip().splitlines()[0]}")
        return 0

    saved = json.load(open(OUT)) if os.path.exists(OUT) else {}
    wanted = args.case or list(CASES)

    if not args.report:
        for name in wanted:
            if name in saved:
                print(f"{DIM}{name}: already measured, skipping{RESET}")
                continue
            if name not in CASES:
                print(f"{RED}unknown case {name!r}{RESET}")
                return 2
            print(f"running {name} on the real engine …", flush=True)
            saved[name] = asyncio.run(run_case(name, args.frames, args.wall_cap))
            # Only a completed run is a result. A "busy" or "wall-capped"
            # case recorded as one would be skipped on the next pass and its
            # n/a row read as a finding — and the control row going n/a makes
            # every other row unreadable, which is exactly what happened on
            # the first pass.
            if saved[name]["status"] not in ("done", "completed"):
                bad = saved.pop(name)["status"]
                print(f"  {RED}{bad} — not recorded, rerun this case{RESET}\n")
                json.dump(saved, open(OUT, "w"), indent=1)
                continue
            json.dump(saved, open(OUT, "w"), indent=1)
            time.sleep(20)   # let the engine release its run lock
            print(f"  {saved[name]['status']}  "
                  f"Tier A {saved[name]['wall_a']}s  Tier B {saved[name]['wall_b']}s\n",
                  flush=True)

    print(f"\n{'case':<14}{'status':>8}" + "".join(f"{l:>9}" for _, l in JUDGED))
    for name in wanted:
        row = saved.get(name)
        if not row:
            print(f"{name:<14}{DIM}  not measured{RESET}")
            continue
        # Tier A comes off the disk because it costs eight minutes of engine
        # time. Tier B is RECOMPUTED, every time, because it costs 0.2 s and
        # because reading it from disk is how a model change silently fails to
        # show up in its own report — which it did once, with a constant set
        # to zero and the table still printing the old value.
        ha = tc.harvest(row["frames_a"]) if row.get("frames_a") else row["tier_a"]
        hb = _tier_b_now(row["case"], row.get("span")) or (
            tc.harvest(row["frames_b"]) if row.get("frames_b") else row["tier_b"])
        cells = "".join(colour(r) + " " for _, _, _, r in ratio_row(ha, hb))
        print(f"{name:<14}{row['status']:>8} {cells}")
    # A consistency check that needs no second tier. Bytes per transport block
    # divided by the symbols the PRB metric implies is bytes per SYMBOL, and
    # that has a hard ceiling from the MCS table: 11 useful subcarriers times
    # the RB count times Qm times the code rate, over 8. A tier that exceeds
    # its own ceiling is not reporting the symbols it used.
    # THE SCORED COLUMN READS ONE UE. `blocks` compares the FULL-WINDOW UE
    # — deliberately, because an extensive counter averaged over UEs is a
    # different quantity — but that makes it blind to the shape behind it.
    # The engine's slice pass fills a capped slice GREEDILY from a rotating
    # start, so one UE takes the ceiling and the rest get nothing that
    # subframe; a model that shares the ceiling out evenly has the same
    # max and a completely different distribution.
    print(f"\n{'case':<14}{'blocks/UE  A: min':>20}{'mean':>8}{'max':>8}"
          f"{'   B: min':>12}{'mean':>8}{'max':>8}")
    for name in wanted:
        row = saved.get(name)
        if not row or not row.get("frames_a"):
            continue
        fa = [f for f in row["frames_a"]
              if (f.get("measurements") or {}).get("TB.TotNbrDl.1")]
        fb = _tier_b_frames_now(name, row.get("span"))
        if not fa or not fb:
            continue
        a = fa[-1]["measurements"]["TB.TotNbrDl.1"]
        b = fb[-1]["measurements"].get("TB.TotNbrDl.1") or {}
        print(f"{name:<14}{a.get('min', 0):20.0f}{a.get('mean', 0):8.0f}"
              f"{a.get('max', 0):8.0f}{b.get('min', 0):12.0f}"
              f"{b.get('mean', 0):8.0f}{b.get('max', 0):8.0f}")

    import window_model as _wm
    print(f"\n{'case':<14}{'A bytes/sym':>13}{'B bytes/sym':>13}{'ceiling':>10}")
    for name in wanted:
        row = saved.get(name)
        if not row or not row.get("frames_a"):
            continue
        ha = tc.harvest(row["frames_a"])
        hb = _tier_b_now(name, row.get("span"))
        if not hb:
            continue
        slots = 4000
        def per_sym(h):
            prb = h.get("RRU.PrbUsedDl") or 0
            vol = h.get("QosFlow.PdcpPduVolumeDL") or 0
            sym = prb / _wm.PRB_METRIC_SCALE * slots * _wm.SYMBOLS_PER_SLOT
            return (vol / sym) if sym else 0.0
        bw_mhz = 10.0 if name == "narrow_band" else 50.0
        n_rb = _wm.rb_count(bw_mhz, 2)
        ceil_b = _wm._payload_bytes(28, 1.0, n_rb)
        a, b = per_sym(ha), per_sym(hb)
        flag = RED + " over its ceiling" + RESET if a > ceil_b * 1.02 else ""
        print(f"{name:<14}{a:>13.0f}{b:>13.0f}{ceil_b:>10.0f}{flag}")

    print(f"\n{DIM}Every number is Tier B / Tier A. The baseline row is the control: "
          f"if it is not green the run differed from the recorded family for some "
          f"other reason and the other rows cannot be read.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
