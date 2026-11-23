#!/usr/bin/env python3
"""Do the two tiers agree, and where exactly do they not?

`tier_criterion.py check` runs both tiers now and compares them, but it costs
a Tier A run — ten minutes on the reference scenario. This compares Tier B
against the Tier A measurement ALREADY RECORDED in `tier_criterion.json`, so
it costs nothing and can run after every change to the fast tier.

That is a weaker test and the difference matters: the recorded Tier A means
come from one scenario at one length, and a metric inside the band here is
inside the band FOR THAT SCENARIO. It is a regression gate for Tier B, not a
claim about the tiers in general. `tier_criterion.py check` remains the
authority.

    python3 tools/tier_agreement.py
    python3 tools/tier_agreement.py --explain      # where the gap comes from
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "nsg_ai"))
sys.path.insert(0, HERE)

import tier_criterion as tc

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def sweep() -> int:
    """Tier B against every Tier A run on disk — where is the model RIGHT?

    One scenario cannot tell a wrong model from a model outside its range. The
    reference is six cells and thirty-six UEs; the stored runs go down to two
    cells and eight. Comparing across them says whether the disagreement is a
    constant error (the model is wrong) or a regime boundary (the model is
    right until something else starts to bind).

    Only FULL windows count. The engine measures each UE from its own last
    counter reset, so a frame taken seconds after attachment covers a second of
    airtime and one taken at attachment covers a fortieth of it; averaging them
    together is what made the reference read 691 blocks instead of 990.
    """
    import statistics
    import tier_b
    import window_model as wm
    from tier_b_calibrate import connect
    from nsoran_live import _rate_of_imsi_map
    from schema import NSGSimulationConfig
    sys.path.insert(0, os.path.join(HERE, "..", "nsg_ai"))
    from main import nsoran_params

    conn = connect()
    cur = conn.cursor()
    cur.execute("select id, config from runs where n_frames >= 10 "
                "order by started_at desc limit 20")
    rows = cur.fetchall()

    import collections
    from partition_live import CELL_BASE
    print("Tier B against every stored Tier A run, on FULL report windows only\n")
    print(f"{'run':<17}{'UE/cell':>8}{'blocks':>8}{'given':>7}"
          f"{'bytes/blk':>11}{'Mbps':>7}   engine attachment")
    for rid, cfg_json in rows:
        try:
            cfg = NSGSimulationConfig.model_validate(cfg_json)
            params = nsoran_params(cfg)
        except Exception as exc:
            print(f"{rid:<17}{DIM}  config will not load: {type(exc).__name__}{RESET}")
            continue
        cur.execute("select payload from run_frames where run_id=%s order by tick", (rid,))
        frames = [r[0] for r in cur.fetchall()]
        slots = round(float(params.get("ind_period_s") or 0.5) / wm.slot_period_s(2))
        du = [f for f in frames
              if ((f.get("measurements") or {}).get("TB.TotNbrDl.1") or {}).get("max")
              and f["measurements"]["TB.TotNbrDl.1"]["max"] > 0.8 * slots]
        if not du:
            print(f"{rid:<17}{DIM}  no full window{RESET}")
            continue

        def wmean(name):
            """Same estimator split the criterion uses — see EXTENSIVE there."""
            if name in tc.EXTENSIVE:
                peaks = [(f["measurements"].get(name) or {}).get("max") for f in du]
                peaks = [p for p in peaks if p is not None]
                return statistics.median(peaks) if peaks else 0.0
            num = den = 0.0
            for f in du:
                mm = (f["measurements"].get(name) or {})
                if mm.get("mean") is not None:
                    w = float(mm.get("count") or 1)
                    num += mm["mean"] * w
                    den += w
            return num / den if den else 0.0

        a_tb, a_vol = wmean("TB.TotNbrDl.1"), wmean("QosFlow.PdcpPduVolumeDL")
        demand = {i: r * 1e6 for i, r in _rate_of_imsi_map(cfg).items()}
        bf = tier_b.frame(params, 0, 1.0, demand_bps=demand)
        b_own = bf["measurements"]["TB.TotNbrDl.1"]["mean"]
        b_vol = bf["measurements"]["QosFlow.PdcpPduVolumeDL"]["mean"]

        # Second column: Tier B's physics on the ENGINE'S OWN attachment. The
        # two differ only in which cell each UE is counted against, so the gap
        # between the columns is exactly what attachment is worth and the rest
        # is not attachment.
        src = [f for f in du if f.get("ues")]
        given = {int(u["imsi"]): int(u["cell"]) - CELL_BASE
                 for u in (src[-1]["ues"] if src else [])
                 if u.get("imsi") is not None and u.get("cell") is not None}
        links = tier_b.link_state(params)
        per = collections.defaultdict(dict)
        for imsi, l in links.items():
            per[given.get(imsi, l["cell"])][imsi] = l["sinr_db"]
        bw_mhz = float(params.get("bw_hz", 50e6)) / 1e6
        blocks = []
        for cell, sinrs in per.items():
            cw = wm.cell_window(cell, sinrs,
                                {i: demand.get(i, float("inf")) for i in sinrs},
                                bw_mhz, 2, window_s=1.0)
            blocks += [u.tb_total for u in cw.ues]
        b_given = statistics.fmean(blocks) if blocks else 0.0
        shape = sorted(collections.Counter(given.values()).values(), reverse=True)

        n_cells = len(bf["cells"]) or 1
        bpb_a = a_vol / max(a_tb, 1e-9)
        bpb_b = b_vol / max(b_own, 1e-9)
        def col(x):
            return (GREEN if 0.8 <= x <= 1.25 else
                    YELLOW if 0.6 <= x <= 1.7 else RED) + f"{x:>7.2f}" + RESET
        print(f"{rid:<17}{params['n_ue']/n_cells:>8.1f}"
              f"{col(b_own/max(a_tb,1e-9))}{col(b_given/max(a_tb,1e-9))}"
              f"{col(bpb_b/max(bpb_a,1e-9))}{col(b_vol/max(a_vol,1e-9))}"
              f"   {shape}")
    print(f"\n{DIM}Every column is Tier B / Tier A; 1.0 is agreement. `given` repeats "
          f"the block count with the engine's OWN UE-to-cell map, so the step between "
          f"the first two columns is what attachment is worth and the residue is not "
          f"attachment. Read `bytes/blk` against `blocks`: a transport block of the "
          f"right SIZE with the wrong COUNT is a scheduling error, not a physical "
          f"one.{RESET}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--explain", action="store_true",
                    help="decompose the disagreement instead of only scoring it")
    ap.add_argument("--sweep", action="store_true",
                    help="compare against every stored Tier A run, not just the reference")
    args = ap.parse_args()

    if args.sweep:
        return sweep()

    path = os.path.join(HERE, "tier_criterion.json")
    if not os.path.exists(path):
        print(f"{RED}no recorded criterion{RESET} — run "
              f"`python3 tools/tier_criterion.py measure` first.")
        return 1
    crit = json.load(open(path))
    name, cfg, params = tc.load_scenario(crit["scenario"][:20])
    # `sim_span_s` is what the criterion recorded Tier A actually reaching.
    # Without it Tier B runs `frames` WINDOWS where Tier A ran `frames`
    # REPORTS, which at the default window is 40 seconds against 2.
    hb = tc.tier_b_harvest(cfg, params, crit["frames"], crit.get("sim_span_s"))

    print(f"Tier B against the Tier A run recorded on {crit['measured_at'][:10]} "
          f"({crit['engine_image']})")
    print(f"scenario: {name}  ·  {params['n_gnb']} cells, {params['n_ue']} UEs, "
          f"{params['bw_hz']/1e6:.0f} MHz, report window {params.get('ind_period_s')} s\n")

    print(f"{'metric':<26}{'Tier A':>13}{'Tier B':>13}{'diff':>9}{'band':>8}  verdict")
    inside = outside = missing = 0
    for k, v in crit["metrics"].items():
        # `tolerance_pct`, not `band_pct`. The raw band is 2 SEM of Tier A's
        # mean across seeds, which measures how precisely that mean is known —
        # and it keeps shrinking as the measurement improves. The corrected
        # estimator took the 64QAM band to 0.1%, so a model 4.7% away failed
        # against a threshold that says nothing about whether 4.7% matters.
        # The criterion floors it at 5% for exactly that reason and prints the
        # floored value in its own table; this tool was reading the other
        # field and holding Tier B to a precision standard instead of a
        # usefulness one.
        a, b = v["across_seeds"]["mean"], hb.get(k)
        band = v.get("tolerance_pct", v["band_pct"])
        if a is None:
            # Tier A never produced this one. DRB.PdcpSduBitRateDl appears in
            # 2 frames of 40 and none of them survive the settle cut, so it
            # is not a metric either tier can be judged on.
            print(f"{v['label']:<26}{'—':>13}{'—':>13}{'':>9}{band:>7.1f}%  "
                  f"{YELLOW}Tier A produced none{RESET}  {tag}")
            missing += 1
            continue
        tag = "contract" if v["contract"] else DIM + "reported" + RESET
        if b is None:
            print(f"{v['label']:<26}{a:>13.2f}{'—':>13}{'':>9}{band:>7.1f}%  "
                  f"{YELLOW}not produced{RESET}  {tag}")
            missing += 1
            continue
        d = (b - a) / a * 100 if a else float("inf")
        ok = abs(d) <= band
        # Only metrics Tier B is answerable for count. A metric the engine
        # reports inconsistently, or one no deterministic model can match,
        # is printed with its reason instead of being scored — see
        # tier_criterion.RESPONSIBILITY for the evidence each one rests on.
        # From the module, not the file. A label is a judgement about the
        # metric and lives in the code; baking it into the snapshot meant
        # every relabel needed a re-derive, and a re-derive is what silently
        # swapped a 2 s criterion for an 8.9 s one.
        resp = tc.RESPONSIBILITY.get(k, ("tier_b", ""))[0]
        if v["contract"] and resp == "tier_b":
            inside += ok
            outside += (not ok)
        note = "" if resp == "tier_b" else f"  {DIM}[{resp}]{RESET}"
        col = GREEN if ok else (DIM if resp != "tier_b" else RED)
        verdict = ("inside" if ok else
                   ("not scored" if resp != "tier_b" else "OUTSIDE"))
        print(f"{v['label']:<26}{a:>13.2f}{b:>13.2f}{col}{d:>8.1f}%{RESET}{band:>7.1f}%  "
              f"{verdict}  {tag}{note}")

    excused = sum(1 for k, v in crit["metrics"].items()
                  if v["contract"]
                  and tc.RESPONSIBILITY.get(k, ("tier_b", ""))[0] != "tier_b")
    total_contract = sum(1 for v in crit["metrics"].values() if v["contract"])
    print(f"\ncontract metrics Tier B is answerable for, inside the band: "
          f"{inside}/{inside + outside}"
          f"{f', {missing} not produced' if missing else ''}")
    # Self-policing. Every label has evidence behind it, but a score computed
    # over a shrinking set flatters itself, and the way that happens is one
    # defensible exclusion at a time. Say the fraction out loud.
    if excused:
        frac = excused / total_contract
        colour = RED if frac > 0.5 else YELLOW if frac >= 0.34 else DIM
        print(f"  {colour}{excused} of {total_contract} contract metrics are not "
              f"scored{RESET} — each with evidence below, but a verdict over "
              f"{total_contract - excused} of {total_contract} is a narrower claim "
              f"than it looks.")
    for k, v in crit["metrics"].items():
        if v.get("name_collision"):
            c = v["name_collision"]
            print(f"  {YELLOW}{v['label']}: the engine reports this NAME from two "
                  f"places{RESET} — {c['low_n']} values up to {c['low_max']:,.0f} and "
                  f"{c['high_n']} from {c['high_min']:,.0f}, a {c['ratio']}x jump. The "
                  f"settle cut removes the smaller cluster, so the figure above is the "
                  f"DU's; a mean over everything would not be.")
    for k, v in crit["metrics"].items():
        why = tc.RESPONSIBILITY.get(k, ("tier_b", ""))[1]
        if why:
            print(f"  {DIM}{v['label']}: not scored — {why}{RESET}")

    if not args.explain:
        print(f"{DIM}--explain decomposes what is left.{RESET}")
        return 0 if outside == 0 else 1

    # ── Where the gap is, in the terms the model is built from ──────────────
    import tier_b
    import window_model as wm
    f = tier_b.frame(params, 0, 1.0,
                     demand_bps={i: m * 1e6 for i, m in
                                 __import__("nsoran_live")._rate_of_imsi_map(cfg).items()})
    m = crit["metrics"]
    slots = round(tier_b.window_s(params) / wm.slot_period_s(2))
    a_tb = m["TB.TotNbrDl.1"]["across_seeds"]["mean"]
    a_prb = m["RRU.PrbUsedDl"]["across_seeds"]["mean"]
    a_64 = m["TB.TotNbrDlInitial.64Qam"]["across_seeds"]["mean"]
    a_q = m["TB.TotNbrDlInitial.Qpsk"]["across_seeds"]["mean"]
    a_16 = 0.0
    b = {k: v["mean"] for k, v in f["measurements"].items()}

    print(f"\n{'— decomposition ':—<74}\n")
    print(f"MODULATION MIX — is the link budget right?")
    print(f"  Tier A: {a_64/(a_64+a_q+a_16)*100:5.1f}% of blocks on 64QAM")
    print(f"  Tier B: {b['TB.TotNbrDlInitial.64Qam']/max(1e-9,b['TB.TotNbrDlInitial'])*100:5.1f}%")
    print(f"  {DIM}The array gain decides this. Without it Tier B puts everything on "
          f"QPSK.{RESET}\n")

    print(f"SLOT OCCUPANCY — is the scheduler right?")
    print(f"  Tier A: {a_tb:7.0f} blocks per UE of {slots} slots")
    print(f"  Tier B: {b['TB.TotNbrDl.1']:7.0f}")
    print(f"  Tier A symbols per slot: {a_prb/wm.PRB_METRIC_SCALE*wm.SYMBOLS_PER_SLOT:.2f} of 14")
    print(f"  Tier B symbols per slot: "
          f"{b['RRU.PrbUsedDl']/wm.PRB_METRIC_SCALE*wm.SYMBOLS_PER_SLOT:.2f} of 14")
    print(f"  {DIM}Tier A's mean is a MIXTURE, not a duty cycle: within a single frame "
          f"the per-UE block count runs from 0 to nearly the full 4000, so about half "
          f"the UEs get nothing at a given instant while the served ones run near "
          f"saturation. Early windows are short as well — the engine measures from each "
          f"UE's own last counter reset, so a freshly attached UE reports ~140 blocks "
          f"and the criterion weights that frame like any other.{RESET}")
    print()

    print(f"ATTACHMENT — do the UEs even land on the same cells?")
    import collections
    import tier_b as _tb
    tb_cells = sorted(collections.Counter(
        v["cell"] for v in _tb.link_state(params).values()).values(), reverse=True)
    print(f"  Tier B UEs per cell: {tb_cells}  "
          f"{DIM}(strongest received power, array gain included){RESET}")
    print(f"  Tier A, measured on the reference run: [13, 9, 6, 5, 2, 1]")
    print(f"  {YELLOW}The engine CLUMPS where Tier B spreads.{RESET} A cell holding 13 UEs "
          f"is past the six-UE concurrency ceiling and rotates; a cell holding 1 is not. "
          f"Same positions, different attachment — one LTE anchor's RRC decides every "
          f"secondary-cell addition, so it is not per-cell best-RSRP.")
    print()

    served = m["ue_count"]["across_seeds"]["mean"]
    a_vol = m["DRB.PdcpSduBitRateDl"]["across_seeds"]["mean"]
    print(f"WHAT IS STILL UNEXPLAINED")
    print(f"  offered {served * 50:.0f} Mbps   delivered {a_vol * served / 1e3:.0f} Mbps   "
          f"queued {m['DRB.BufferSize.Qos']['across_seeds']['mean']/1e6:.1f} MB")
    print(f"  {DIM}Even given the engine's own attachment skew the scheduler model "
          f"predicts roughly three times the blocks it reports. Not modelled yet: HARQ "
          f"retransmissions take symbols before new data does, the uplink flows compete "
          f"for the same twelve symbols, and a flow with an empty buffer is not an "
          f"active flow. A factor chosen to close the gap would hide all three.{RESET}")

    return 0 if outside == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
