#!/usr/bin/env python3
"""What are the FAST TIER's own limits? Nobody ever asked.

`/envelope` returns GNB_MAX 16, UE_MAX 150, 30 UEs per cell and a
bandwidth-dependent per-cell ceiling, and the canvas warns from them while a
scenario is being drawn. Every one of those was measured on ns-3: they are
where the mmWave stack aborts, where its heap corrupts, where its scheduler
splits a transport block to zero. The fast tier has none of that code. It
computes a KPM window in closed form, so its limits are arithmetic — how
long a window takes and how much memory the scene needs — and arithmetic
does not abort, it just gets slow.

This measures them, so the canvas can say which tier a warning is about
instead of implying both.

    python3 tools/fast_tier_envelope.py
    python3 tools/fast_tier_envelope.py --max-cells 48
"""
from __future__ import annotations

import argparse
import math
import os
import resource
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "nsg_ai"))
sys.path.insert(0, HERE)

GREEN, YELLOW, RED, DIM, RESET = (
    "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m")

# A window is 1 simulated second. Past this a single window costs more wall
# clock than the second it represents, i.e. the tier stops being fast — which
# is the only limit it has that a user can feel.
REAL_TIME_MS = 1000.0


def scene(n_cells: int, ue_per_cell: int, bw_mhz: float = 50.0):
    from schema import (AntennaConfig, BaseStation, FrequencyBand,
                        MassiveMIMOConfig, NetworkSlice, NSGSimulationConfig,
                        Position2D, Position3D, RadioConfig,
                        ServiceRequirement, SimulationArea, UEGroup)
    # A hex-ish grid at a 400 m pitch, so cells keep a realistic geometry
    # rather than lining up and all interfering identically.
    side = max(1, math.ceil(math.sqrt(n_cells)))
    pts = [(400.0 * (i % side), 400.0 * (i // side)) for i in range(n_cells)]
    bss = [BaseStation(
        id=f"c{i}", name=f"c{i}", position=Position3D(x=x, y=y, z=25.0),
        radio=RadioConfig(
            band=FrequencyBand(label="NR", frequency_mhz=3600.0,
                               bandwidth_mhz=bw_mhz, numerology=2),
            tx_power_dbm=43.0,
            antenna=AntennaConfig(
                massive_mimo=MassiveMIMOConfig(rows=8, cols=8))))
        for i, (x, y) in enumerate(pts)]
    ues = [UEGroup(id=f"g{i}", name=f"g{i}", count=ue_per_cell,
                   spawn_position=Position2D(x=x + 60, y=y + 60),
                   services=[ServiceRequirement(
                       slice_type=NetworkSlice.EMBB, qos_id=9,
                       target_throughput_gbps=0.05)])
           for i, (x, y) in enumerate(pts)]
    span = 400.0 * side + 600.0
    return NSGSimulationConfig(
        name=f"{n_cells}c/{n_cells * ue_per_cell}ue", base_stations=bss,
        ue_groups=ues,
        area=SimulationArea(x_min=-600.0, x_max=span, y_min=-600.0, y_max=span))


def measure(n_cells: int, ue_per_cell: int, windows: int = 4) -> dict:
    import tier_b
    from main import nsoran_params
    cfg = scene(n_cells, ue_per_cell)
    # engine_clamped=False: GNB_MAX and UE_MAX are ns-3's, and asking the fast
    # tier about its own limits through the slow tier's clamp would measure
    # the clamp.
    params = nsoran_params(cfg, engine_clamped=False)
    demand = {i: 50e6 for i in range(1, params["n_ue"] + 1)}
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.perf_counter()
    frames = tier_b.frames(params, windows, demand_bps=demand)
    wall = time.perf_counter() - t0
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    served = len({u["imsi"] for f in frames for u in f["ues"]})
    return {
        "cells": params["n_gnb"], "ues": params["n_ue"],
        "ms": wall / windows * 1000.0,
        "served": served,
        "rss_mb": max(0.0, (rss - rss0) / 1024.0),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-cells", type=int, default=48)
    ap.add_argument("--max-per-cell", type=int, default=120)
    a = ap.parse_args()

    print(f"one window = 1 simulated second. Past {REAL_TIME_MS:.0f} ms a "
          f"window costs more wall clock than it represents.\n")
    print(f"{'cells':>6}{'UEs/cell':>10}{'UEs':>7}{'ms/window':>11}"
          f"{'x real time':>13}{'served':>8}{'RSS MB':>9}")
    worst_ok = None
    for n_cells in [c for c in (3, 6, 12, 24, 48) if c <= a.max_cells]:
        for per in [p for p in (2, 6, 15, 30, 60, 120) if p <= a.max_per_cell]:
            if n_cells * per > 6000:
                continue
            try:
                r = measure(n_cells, per)
            except Exception as exc:                # noqa: BLE001
                print(f"{n_cells:>6}{per:>10}{'':>7}{RED}{type(exc).__name__:>11}"
                      f"{RESET}  {exc}")
                continue
            x = 1000.0 / r["ms"]
            col = GREEN if r["ms"] < REAL_TIME_MS else YELLOW
            miss = "" if r["served"] == r["ues"] else f"  {YELLOW}{r['ues'] - r['served']} unserved{RESET}"
            print(f"{r['cells']:>6}{per:>10}{r['ues']:>7}{col}{r['ms']:>11.1f}{RESET}"
                  f"{x:>13.0f}{r['served']:>8}{r['rss_mb']:>9.0f}{miss}")
            if r["ms"] < REAL_TIME_MS:
                worst_ok = r

    print()
    if worst_ok:
        print(f"  {GREEN}the largest scene measured that still beats real "
              f"time{RESET}: {worst_ok['cells']} cells / {worst_ok['ues']} UEs "
              f"at {worst_ok['ms']:.0f} ms a window")
    print(f"  {DIM}Nothing here aborts. The fast tier has no scheduler to "
          f"corrupt and no heap to overrun — its limit is that a window "
          f"eventually costs more than the second it stands for, and even "
          f"then it still produces the window. That is a different KIND of "
          f"limit from the engine's and the canvas should not report them "
          f"under one label.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
