#!/usr/bin/env python3
"""Does the beam-pointing MIXTURE reproduce the spread the engine measures?

The SINR spread defeated five closed forms, every one of them a fading
sigma, because the only instrument was a histogram whose top bin is ">24
dB" and holds nearly the whole population. The per-transmission PHY trace
(`tools/rx_trace.py`) says what is actually moving:

    consecutive samples, 0.25 ms apart     0.020 dB   (no fast fading)
    a neighbour transmitting or not          16.8 dB
    WHICH UE the neighbour is serving        50.6 dB

So this is not a sigma to predict. It is a mixture over which UE each
neighbour is serving, and Tier B knows every term of it. This tool scores `tier_b.link_state`'s mixture against the trace on the
two quantities it produces: the distribution of PER-UE spread and the
level it hangs from.

There is no RNTI -> IMSI mapping (nothing reproduces the engine's
attachment order), so the comparison is distribution against
distribution — mean, median, quartiles and range over the UE population,
which is what "0.1 to 26.5 dB across 36 UEs" describes.

    python3 tools/beam_mixture.py              # score it
    python3 tools/beam_mixture.py --floor      # sweep the null depth
"""
import argparse
import collections
import math
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "nsg_ai"))

CASE: str | None = None

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m")


def measured() -> tuple[list[float], list[float]]:
    """Per-UE spread and mean from the engine's own trace, in dB."""
    import rx_trace
    path = rx_trace.local_path(CASE)
    if not os.path.exists(path):
        return [], []
    per = collections.defaultdict(list)
    for t, cell, rnti, sinr, *_ in rx_trace.rows(path):
        per[(cell, rnti)].append(sinr)
    ok = [v for v in per.values() if len(v) > 4]
    return (sorted(statistics.pstdev(v) for v in ok),
            sorted(statistics.fmean(v) for v in ok))


def predicted() -> tuple[list[float], list[float]]:
    """Per-UE spread AND level from the mixture, on the same scenario."""
    import tier_b
    import tier_criterion as tc
    from main import nsoran_params
    import copy
    _, cfg, _ = tc.load_scenario("Partition test")
    if CASE:
        import tier_breadth as tbr
        cfg = tbr.CASES[CASE](copy.deepcopy(cfg))
    params = nsoran_params(cfg, engine_clamped=False)
    pos = tier_b._parse_points(params["ue_positions"])
    links = tier_b.link_state(params, pos)
    return (sorted(v.get("sinr_sd_db", 0.0) for v in links.values()),
            sorted(v["sinr_db"] for v in links.values()))


def line(tag: str, v: list[float]) -> str:
    if not v:
        return f"  {tag:26} —"
    q = statistics.quantiles(v, n=4) if len(v) > 3 else [v[0]] * 3
    return (f"  {tag:26} mean {statistics.fmean(v):6.2f}  median "
            f"{statistics.median(v):6.2f}  q1 {q[0]:6.2f}  q3 {q[2]:6.2f}  "
            f"range {min(v):5.2f}-{max(v):5.2f}  n={len(v)}")


TERMS = {
    "as shipped": {},
    "without the appear/disappear term": {"no_appear": True},
    "without the beam term": {"no_beam": True},
    "without the condition re-draw": {"no_cond": True},
}


def decompose(case: str | None) -> list[tuple[str, float, float]]:
    """Turn each mechanism off in turn and re-measure, on one scenario.

    The mixture has three terms and they do not contribute equally on every
    scenario — with buildings the condition is GEOMETRIC and never
    re-draws, so two of the three are dead there and the third carries
    everything. A single scenario cannot tell you that.
    """
    import copy
    import types
    import tier_b
    import tier_breadth as tbr
    import tier_criterion as tc
    from main import nsoran_params
    src = open(os.path.join(os.path.dirname(HERE), "nsg_ai", "tier_b.py")).read()
    _, base, _ = tc.load_scenario("Partition test")
    cfg = copy.deepcopy(base) if not case else tbr.CASES[case](copy.deepcopy(base))
    params = nsoran_params(cfg, engine_clamped=False)
    pos = tier_b._parse_points(params["ue_positions"])
    out = []
    for label, kw in TERMS.items():
        code = src
        if kw.get("no_appear"):
            code = code.replace(
                "                if not (cut_los[j] if los_of[j] else cut_nlos[j]):\n"
                "                    continue          # out of range in this epoch",
                "                pass")
        if kw.get("no_beam"):
            code = code.replace(
                "g_beam = _af(j, mem[int(r * len(mem)) % len(mem)], i)",
                "g_beam = 0.0")
        if kw.get("no_cond"):
            code = code.replace(
                "los_of = [cond_u[j][t] < p_los[j] for j in range(n_cell)]",
                "los_of = [p_los[j] > 0.5 for j in range(n_cell)]")
        mod = types.ModuleType("tier_b_variant")
        sys.modules["tier_b_variant"] = mod
        exec(compile(code, "tier_b_variant", "exec"), mod.__dict__)
        links = mod.link_state(params, pos)
        out.append((label,
                    statistics.fmean(v.get("sinr_sd_db", 0.0)
                                     for v in links.values()),
                    statistics.fmean(v["sinr_db"] for v in links.values())))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default=None,
                    help="a tier_breadth case; its trace must exist "
                         "(tools/rx_trace.py --run --case X)")
    ap.add_argument("--terms", action="store_true",
                    help="turn each mechanism off in turn and re-measure")
    a = ap.parse_args()
    global CASE
    CASE = a.case

    eng_sd, eng_lv = measured()
    if not eng_sd:
        print(f"{RED}no rx_trace.tsv{RESET} — run tools/rx_trace.py --run")
        return 1
    got_sd, got_lv = predicted()
    if a.terms:
        print(f"WHICH MECHANISM CARRIES IT{' — ' + CASE if CASE else ''}\n")
        print(f"  {'variant':36} {'spread':>8} {'level':>8}")
        for lbl, sd, lv in decompose(CASE):
            print(f"  {lbl:36} {sd:8.2f} {lv:8.2f}")
        print(f"  {'the engine, measured':36} "
              f"{statistics.fmean(eng_sd):8.2f} {statistics.fmean(eng_lv):8.2f}")
        return 0

    import window_model as wm
    print("PER-UE SINR SPREAD — the quantity five closed forms missed\n")
    print(line("engine (rx trace)", eng_sd))
    print(line("Tier B mixture", got_sd))
    print(f"  {DIM}flat Rayleigh would be {wm.FADING_SIGMA_DB:.2f} dB for "
          f"every UE: mean absolute error "
          f"{statistics.fmean(abs(wm.FADING_SIGMA_DB - x) for x in eng_sd):.2f}"
          f" dB, against "
          f"{statistics.fmean(abs(a - b) for a, b in zip(got_sd, eng_sd)):.2f}"
          f" dB quantile-matched for the mixture{RESET}")

    print("\nPER-UE MEAN SINR — the level it hangs from\n")
    print(line("engine (rx trace)", eng_lv))
    print(line("Tier B mixture", got_lv))
    print(f"\n  {DIM}It read 28.25 dB before the link pruning and the UE's "
          f"own 2x2 array went in.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
