#!/usr/bin/env python3
"""Find a config that hits a target metric, by running the real engine.

"Three cells, ten users, I want 50 Mbps" is not a language problem — it is
INVERSE DESIGN over a simulator we already own, and the simulator is the only
thing that knows the answer. So this searches rather than predicts: propose,
run, measure, keep what helped.

Three measurements decide the whole design, and ignoring any of them produces a
tool that looks like it works:

  * AN EVALUATION COSTS 40-95 SECONDS. The budget is ten to thirty runs, not
    thousands, which rules out anything data-hungry. Greedy coordinate search is
    the right shape at this scale; Bayesian optimisation needs more evaluations
    than the whole budget before it starts paying for itself.

  * THROUGHPUT MOVES 8-11% BETWEEN IDENTICAL RUNS (measured 2026-09-14, six runs
    of one config at one seed). A single run therefore cannot rank two nearby
    configs, and a search that believes it will spend its budget chasing noise
    uphill. So the baseline is measured TWICE to size the noise, and a step is
    only kept when it beats that spread.

  * THE ENVELOPE IS NOT ADVISORY. Bandwidth caps UEs per cell (10 MHz -> 2,
    20 -> 10, 50 -> 25) and crossing it aborts the run, so the search clamps
    every proposal through the same code the product uses.

What it does NOT do: claim the result is optimal. It reports the trajectory, so
a flat one reads as "these levers did not move it" rather than as success.

    python3 tools/target_search.py --target dl_mbps_mean=50 --gnb 3 --ue 10
    python3 tools/target_search.py --target dl_mbps_mean=120 --budget 20
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "nsg_ai"))

from nsg_ai.schema import NSGSimulationConfig          # noqa: E402
from nsg_ai.main import nsoran_params                  # noqa: E402
from nsg_ai.engine_envelope import max_ue_per_cell_for_bw  # noqa: E402

ENGINE = os.environ.get("NSG_TARGET_ENGINE", "http://127.0.0.1:8090/run")

# Levers that are MEASURED to move throughput on this engine. Deliberately short:
# a coordinate search spends one evaluation per lever per pass, so every lever
# that does nothing costs a run and buys nothing. Anything not on this list was
# either measured inert or is a fidelity choice rather than a performance one.
LEVERS = {
    # (path, candidate steps). Applied to EVERY cell — per-cell search would
    # multiply the space by the cell count for a gain nothing has measured.
    "tx_power_dbm":  [30.0, 34.0, 38.0, 40.0, 43.0, 46.0],
    "tilt_deg":      [0.0, 3.0, 6.0, 9.0, 12.0],
    "bandwidth_mhz": [10, 20, 50],
    # 8x8 -> 4x4 was measured at 1.43x more simulated time for ~35% of peak
    # throughput, so it is a real lever in BOTH directions.
    "mimo_rc":       [4, 8, 16],
}


def apply_lever(cfg: dict, name: str, value) -> dict:
    out = copy.deepcopy(cfg)
    for b in out.get("base_stations", []):
        radio = b.setdefault("radio", {})
        if name == "tx_power_dbm":
            radio["tx_power_dbm"] = value
        elif name == "tilt_deg":
            radio.setdefault("antenna", {})["tilt_deg"] = value
        elif name == "bandwidth_mhz":
            radio.setdefault("band", {})["bandwidth_mhz"] = value
        elif name == "mimo_rc":
            mm = radio.setdefault("antenna", {}).setdefault("massive_mimo", {}) or {}
            mm["rows"], mm["cols"] = value, value
            radio["antenna"]["massive_mimo"] = mm
    return out


def final_levers(cfg: dict) -> dict:
    """The searched levers as they stand in this config — the answer, not the path."""
    b = (cfg.get("base_stations") or [{}])[0]
    radio = b.get("radio", {})
    ant = radio.get("antenna", {}) or {}
    mm = ant.get("massive_mimo") or {}
    return {
        "tx_power_dbm": radio.get("tx_power_dbm"),
        "tilt_deg": ant.get("tilt_deg"),
        "bandwidth_mhz": (radio.get("band") or {}).get("bandwidth_mhz"),
        "mimo_rc": mm.get("rows"),
    }


def within_envelope(cfg: dict) -> bool:
    """The product's own rule, not a copy of it: below ~2 MHz per UE the
    scheduler's transport-block split rounds to zero and the run aborts."""
    cells = max(1, len(cfg.get("base_stations") or []))
    ues = sum(g.get("count", 0) for g in (cfg.get("ue_groups") or []))
    bw = ((cfg.get("base_stations") or [{}])[0].get("radio", {})
          .get("band", {}).get("bandwidth_mhz", 50))
    num = ((cfg.get("base_stations") or [{}])[0].get("radio", {})
           .get("band", {}).get("numerology", 1))
    return ues <= cells * max_ue_per_cell_for_bw(bw, num)


def evaluate(cfg: dict, metric: str, seed: int = 1,
             collect_s: int = 60) -> tuple[float | None, dict]:
    """One engine run. Returns (metric value, raw result).

    `collect_s` is not a performance knob here, it is what makes the reading
    MEAN anything. At the automatic window this search measured a baseline of
    12.88 Mbps with a noise floor of ±10.34 — eighty percent — because the run
    ends during attach with two or three UEs reporting. Every candidate then sits
    inside the noise and the search learns nothing while spending its whole
    budget. A longer window costs about a minute per evaluation and is the
    difference between a search and a random walk.
    """
    p = nsoran_params(NSGSimulationConfig(**cfg))
    p["sim_time"], p["rng_run"], p["kpm_collect_s"] = 60, seed, collect_s

    # BUSY IS NOT A VERDICT ON THE CONFIG. The engine holds its lock until the
    # previous run's processes exit, seconds after that run reported done, so
    # back-to-back evaluations hit it routinely. Without this the search read
    # "busy" as "this candidate produced nothing", discarded a good config and
    # spent the budget anyway — observed twice in one five-run pass, and the same
    # config returned 15.88 Mbps on a retry.
    d: dict = {}
    for _ in range(8):
        req = urllib.request.Request(ENGINE, data=json.dumps(p).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=1800) as r:
                d = json.loads(r.read().decode())
        except Exception as exc:                        # noqa: BLE001
            return None, {"status": f"ERR {type(exc).__name__}: {exc}"}
        if d.get("status") != "busy":
            break
        time.sleep(20)
    m = d.get("measurements") or {}
    if metric == "dl_mbps_mean":
        thp = m.get("DRB.UEThpDl") or {}
        val = (thp.get("mean") or 0) / 1000.0 if thp.get("mean") is not None else None
    else:
        val = (m.get(metric) or {}).get("mean")
    return val, d


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", required=True, help="metric=value, e.g. dl_mbps_mean=50")
    ap.add_argument("--base", default=os.path.join(ROOT, "tools", "horizon_base.json"))
    ap.add_argument("--gnb", type=int, help="fix the cell count (a constraint, not a lever)")
    ap.add_argument("--ue", type=int, help="fix the total UE count")
    ap.add_argument("--budget", type=int, default=14, help="max engine runs")
    ap.add_argument("--collect", type=int, default=60,
                    help="KPM collection window per evaluation, seconds. Lower is "
                         "faster and noisier; below ~30 the noise swallows every "
                         "candidate (measured: 80%% noise at the automatic window)")
    ap.add_argument("--out", help="write the trajectory as JSON")
    args = ap.parse_args()

    metric, _, tgt = args.target.partition("=")
    target = float(tgt)

    sys.path.insert(0, os.path.join(ROOT, "tools"))
    from horizon_sweep import stretch                   # noqa: PLC0415
    base_raw = json.load(open(args.base, encoding="utf-8"))
    cfg = stretch(base_raw, args.gnb or 3, args.ue or 6)

    spent = 0
    trail: list[dict] = []

    # TWO baseline runs, because one cannot size the noise and every later
    # decision is a comparison against it.
    print(f"target: {metric} = {target}", flush=True)
    print("sizing the noise floor (2 runs of the starting config)...", flush=True)
    b1, _ = evaluate(cfg, metric, seed=1, collect_s=args.collect); spent += 1
    b2, _ = evaluate(cfg, metric, seed=2, collect_s=args.collect); spent += 1
    if b1 is None or b2 is None:
        print("the starting config does not produce a reading — fix it first")
        return 1
    best = (b1 + b2) / 2.0
    noise = abs(b1 - b2)
    print(f"  baseline {b1:.2f} / {b2:.2f}  ->  {best:.2f}  (noise ±{noise:.2f})", flush=True)
    trail.append({"step": "baseline", "value": best, "noise": noise, "runs": spent})

    print(f"\n{'lever':>14} {'value':>8} {'result':>9} {'vs best':>9}  kept", flush=True)
    print("-" * 56, flush=True)
    improved = True
    while improved and spent < args.budget:
        improved = False
        for name, options in LEVERS.items():
            for value in options:
                if spent >= args.budget:
                    break
                cand = apply_lever(cfg, name, value)
                if cand == cfg or not within_envelope(cand):
                    continue
                val, res = evaluate(cand, metric, seed=1, collect_s=args.collect)
                spent += 1
                if val is None:
                    # Say WHICH failure. "partial" is a verdict on the config —
                    # the RAN never scheduled — while "busy" or an error is a
                    # verdict on the bench, and confusing them makes a search
                    # blame the scenario for the harness.
                    why = str(res.get("status") or "no reading")
                    print(f"{name:>14} {str(value):>8} {'—':>9} {why:>9}", flush=True)
                    trail.append({"step": name, "value": value, "result": None,
                                  "status": why, "runs": spent})
                    continue
                # Closer to the target AND outside the noise. Without the second
                # condition the search walks uphill on run-to-run variation and
                # reports a config that is no better than where it started.
                gain = abs(best - target) - abs(val - target)
                keep = gain > noise

                # A CANDIDATE INSIDE THE NOISE IS UNDECIDED, NOT REJECTED. The
                # first full pass threw away mimo_rc=16 at 18.46 against a kept
                # 15.84, because its +2.61 did not clear a ±3.29 floor — one
                # sample cannot tell a real gain that size from variation. Rather
                # than discard it, spend ONE more run on the promising ones and
                # judge the average. That is a better use of the budget than
                # trying the next arbitrary lever on a single sample.
                confirmed = None
                if not keep and gain > noise * 0.5 and spent < args.budget:
                    v2, _ = evaluate(cand, metric, seed=2, collect_s=args.collect)
                    spent += 1
                    if v2 is not None:
                        confirmed = (val + v2) / 2.0
                        gain = abs(best - target) - abs(confirmed - target)
                        keep = gain > noise
                        print(f"{'':>14} {'confirm':>8} {v2:>9.2f} {gain:>+9.2f}  "
                              f"{'KEEP' if keep else 'still noise'}", flush=True)
                if confirmed is None:
                    print(f"{name:>14} {str(value):>8} {val:>9.2f} {gain:>+9.2f}  "
                          f"{'KEEP' if keep else 'noise' if gain > 0 else ''}", flush=True)
                if val == 0.0:
                    # Not a failure — the RAN genuinely scheduled nothing — but
                    # it is almost never what the operator meant, and averaging
                    # it into a comparison silently would hide that.
                    print(f"{'':>14} {'':>8} {'':>9} {'':>9}  (zero throughput — "
                          f"the RAN scheduled nothing at this setting)", flush=True)
                trail.append({"step": name, "value": value, "result": val,
                              "confirmed": confirmed, "gain": gain, "kept": keep,
                              "runs": spent})
                if keep:
                    cfg, best, improved = cand, confirmed or val, True
                    break

    # WHAT WAS NEVER TRIED. First-improvement search moves on as soon as a step
    # helps, so better values of the SAME lever go unevaluated — the first full
    # pass kept tx_power 38 and therefore never tried 43 or 46. Printing the gap
    # is more honest than implying the space was covered.
    tried = {(t.get("step"), t.get("value")) for t in trail}
    unexplored = [(n, v) for n, opts in LEVERS.items() for v in opts
                  if (n, v) not in tried]
    print(f"\nbudget spent: {spent} runs", flush=True)
    if unexplored:
        print(f"never evaluated ({len(unexplored)}): "
              + ", ".join(f"{n}={v}" for n, v in unexplored[:10])
              + ("  …" if len(unexplored) > 10 else ""), flush=True)
    print(f"reached {best:.2f} against a target of {target:.2f} "
          f"({abs(best - target) / target * 100:.1f}% away)", flush=True)
    kept = [t for t in trail if t.get("kept")]
    if not kept:
        print("NOTHING BEAT THE NOISE — these levers do not move this metric for this\n"
              "scenario, or the budget was too small to tell. Not a config problem.")
    else:
        # THE CONFIG IT ENDED ON, read back from the config itself. Listing the
        # accepted STEPS instead was wrong in a way that matters: a lever kept
        # early and superseded later still appeared, so a trail of
        # "tx_power -> 30, tx_power -> 38" read as though 30 were in the answer.
        # Only the last value of each lever survives, and that is what someone
        # would copy.
        print("the config it ended on:")
        for name, value in final_levers(cfg).items():
            print(f"  {name:<14} {value}")
        print("how it got there:")
        for t in kept:
            shown = t.get("confirmed") or t.get("result")
            print(f"  {t['step']} -> {t['value']}   {shown:.2f}"
                  + ("  (confirmed over 2 runs)" if t.get("confirmed") else ""))
    if args.out:
        json.dump({"target": {metric: target}, "trail": trail,
                   "final_config": cfg, "reached": best, "noise": noise},
                  open(args.out, "w", encoding="utf-8"), indent=1)
        print("wrote", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
