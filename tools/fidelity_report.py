#!/usr/bin/env python3
"""
NSG fidelity report — does the platform actually measure what it claims?

Two questions, one command:

  1. CONFIG LEVERS. For each thing the user can design, perturb ONLY that thing
     and check the REAL engine's measured KPM moves. This exists because the
     failure mode we keep hitting is not a wrong number, it is a lever that is
     silently ignored: the ns-O-RAN validate spent an unknown length of time
     returning nothing while every layer reported success, and nobody could tell.
     A lever that does not move the measurement is a lever that is not wired up.

  2. CROSS-TIER. Run the SAME config through the analytical core (the fast tier
     the built-in AI trains against, and half of the Compare leaderboard) and the
     real ns-O-RAN engine, and print the gap. If those two disagree wildly, then
     ranking sandbox runs next to real runs is misleading.

Only quantities that are the same physical thing are compared. The analytical
core reports RSRP (dBm) and a PRB *proxy* (%); the engine reports serving SINR
(dB) and a PRB *count*. Those are not the same and are reported side by side,
not differenced.

Usage:  python tools/fidelity_report.py [--quick]
        --quick runs the cross-tier check only (no lever sweep, ~1 min).

Needs the platform backend on :8000 and the engine container on :8090, and takes
the engine lock for ~1 min per run, so stop any live session first.
"""
from __future__ import annotations

import argparse
import copy
import os
import sys

import pathlib as _pl, sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
from _backend_auth import auth_headers  # noqa: E402
import time

import requests

sys.path.insert(0, "/home/chancelor/nsg_ai_platform/nsg_ai")
from ns3_bridge import estimate_live_throughput          # noqa: E402
from schema import NSGSimulationConfig                    # noqa: E402

API = "http://localhost:8000"
SIM_TIME = 20


# ── the scenario under test ──────────────────────────────────────────────────
def _cell(i: str, x: float, y: float, power: float = 44.0) -> dict:
    return {
        "id": i, "name": i, "position": {"x": x, "y": y, "z": 25},
        "radio": {
            "band": {"label": "NR", "frequency_mhz": 3500, "bandwidth_mhz": 50,
                     "numerology": 1},
            "tx_power_dbm": power,
            "antenna": {"type": "cosine", "azimuth_deg": 0, "tilt_deg": 3,
                        "gain_dbi": 17, "horizontal_beamwidth_deg": 65,
                        "vertical_beamwidth_deg": 7.5},
        },
    }


def _grp(i: str, x: float, y: float, n: int) -> dict:
    return {
        "id": i, "name": i, "count": n, "spawn_position": {"x": x, "y": y},
        "mobility": {"pattern": "pedestrian", "speed_mps": 1.4},
        "services": [{"slice_type": "eMBB", "qos_id": 9,
                      "target_throughput_gbps": 0.010}],
    }


def baseline_config() -> dict:
    """5 cells + 10 UEs, inside the engine's validated envelope.

    The UEs are placed as four small groups on g2/g3/g4/g5, DELIBERATELY leaving
    g1 (0,0) with no UEs near it. Two reasons: (a) g1 is then a genuinely IDLE
    cell, which the L3 energy lever needs (sleeping it must cut power with nobody
    paying); (b) this spread stays under the backend's per-cell cap, so
    nsoran_params passes the geometry through UNCHANGED. A single 10-UE group on
    the centre — the older baseline — now trips the anti-crash rebalancer
    (main._rebalance_ue_cells), which scatters the crowd evenly across ALL five
    cells, leaving no idle cell and making the geometry non-deterministic to
    reason about. Verified layout: nearest-cell counts [g1=0, g2=1, g3=2, g4=3,
    g5=4], no rebalance."""
    return {
        "name": "fidelity", "description": "fidelity probe",
        "base_stations": [_cell("g1", 0, 0), _cell("g2", 500, 0), _cell("g3", 0, 500),
                          _cell("g4", 500, 500), _cell("g5", 250, 250)],
        "ue_groups": [_grp("u2", 500, 0, 2), _grp("u3", 0, 500, 2),
                      _grp("u4", 500, 500, 2), _grp("u5", 250, 250, 4)],
        "buildings": [],
        "area": {"x_min": -400, "x_max": 900, "y_min": -400, "y_max": 900,
                 "resolution_m": 40},
        "settings": {"duration_s": 20, "report_interval_ms": 1000, "scenario": "UMa",
                     "shadowing_enabled": True, "fast_fading_enabled": False,
                     "random_seed": 42},
    }


# ── the two tiers ────────────────────────────────────────────────────────────
def run_real(cfg: dict) -> dict:
    """One real ns-O-RAN run. Returns the KPM the platform itself consumes.

    The engine takes a lock for the whole run and only drops it as the process
    exits, so a back-to-back sweep can race its own previous run: wait that out
    rather than reporting a lever broken because the engine was still busy.
    """
    t0 = time.time()
    for attempt in range(6):
        r = requests.post(f"{API}/ric/validate-nsoran?sim_time={SIM_TIME}", json=cfg,
                          headers=auth_headers(API),
                          timeout=600)
        r.raise_for_status()
        k = r.json().get("kpm", {})
        if k.get("status") != "busy":
            break
        time.sleep(10)
    m = k.get("measurements", {})

    def mean(name):
        v = m.get(name, {}).get("mean")
        return float(v) if v is not None else None

    thp_kbps = mean("DRB.UEThpDl")
    return {
        "status": k.get("status"), "error": k.get("error"),
        "retried": k.get("retried", False), "secs": time.time() - t0,
        "sinr_db": mean("L3servingSINR"),
        "prb": mean("RRU.PrbUsedDl"),
        "thp_mbps_per_ue": thp_kbps / 1000 if thp_kbps is not None else None,
        "bler_pct": k.get("derived", {}).get("bler_pct"),
        "energy": k.get("energy"),
        # flattened for delta_line. Energy is compared as MEAN POWER (W), not
        # total Joules: the total accumulates over however much sim time the
        # run happened to reach before the wall-clock kill, so two runs'
        # totals aren't comparable — a slept-cell run that simulated longer
        # once measured MORE total J than the baseline and failed the lever.
        "energy_w": (k.get("energy") or {}).get("mean_power_w"),
        "ul_kbps": mean("DRB.PdcpSduBitRateUl"),
        "ue_count": k.get("ue_count"),
    }


def run_engine_direct(slice_quotas: str) -> dict:
    """One engine run through its own API (:8090) with fixed explicit geometry.

    Exists for the slice-quota lever only: quotas are a runtime/E2SM-RC control
    (deliberately not a config field), so the backend validate path cannot set
    them — the engine API accepts them as an initial condition. Everything but
    `slice_quotas` is pinned, so the pair isolates the quota's effect.
    """
    url = os.environ.get("NSG_NSORAN_URL", "http://localhost:8090").rstrip("/") + "/run"
    payload = {
        "sim_time": 15, "n_gnb": 5, "n_ue": 10, "bw_hz": "50000000.0",
        "gnb_positions": ("1550.0,1750.0;2050.0,1750.0;1550.0,2250.0;"
                          "2050.0,2250.0;1800.0,2000.0"),
        "ue_positions": ("2050,2000;1950,2170;1750,2240;1580,2130;1550,1930;"
                         "1650,1780;1850,1760;2020,1850;1700,2100;1900,2050"),
        "gnb_powers": "44;44;44;44;44",
        "ue_slices": ";".join(["9,30"] * 10),   # saturating, so the cap binds
        "slice_quotas": slice_quotas,
    }
    for _ in range(8):   # wait out the engine lock between back-to-back runs
        k = requests.post(url, json=payload, headers=auth_headers(API), timeout=400).json()
        if k.get("status") != "busy":
            break
        time.sleep(10)
    m = k.get("measurements", {})
    thp = m.get("DRB.UEThpDl", {}).get("mean")
    return {"status": k.get("status"),
            "thp_mbps_per_ue": thp / 1000 if thp is not None else None}


def run_analytic(cfg: dict) -> dict:
    """The fast tier, averaged over a few ticks so a single tick's noise doesn't
    decide the comparison."""
    model = NSGSimulationConfig(**cfg)
    ticks = [estimate_live_throughput(model, t) for t in range(5)]

    def avg(key):
        vals = [t[key] for t in ticks if t.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    return {"thp_mbps": avg("tput"), "prb_pct": avg("prb"), "rsrp_dbm": avg("rsrp"),
            "conn_ues": avg("conn_ues"), "qos": avg("qos"), "power_w": avg("power")}


# ── report helpers ───────────────────────────────────────────────────────────
def fmt(v, spec=".2f", dash="n/a"):
    return dash if v is None else format(v, spec)


def delta_line(label, before, run, key, unit, expect, why):
    """expect: 'up' | 'down' | 'move' — 'move' only asserts the lever does
    SOMETHING, for levers whose direction is genuinely ambiguous."""
    after = run.get(key)
    if before is None or after is None:
        # Say WHY there is no number: a lever that looks "not wired" because the
        # run died is a different bug from one the engine ignores.
        reason = (f"engine status={run.get('status')}: {run.get('error')}"
                  if run.get("status") != "ok" else "engine ran but reported no value")
        print(f"  [FAIL] {label:24s} {'NO DATA':>22s}   {reason}")
        return False
    d = after - before
    rel = abs(d) / abs(before) if before else float("inf")
    if expect == "up":
        ok = d > 0 and rel > 0.05
    elif expect == "down":
        ok = d < 0 and rel > 0.05
    else:
        ok = rel > 0.05
    arrow = "+" if d >= 0 else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {label:24s} "
          f"{fmt(before)} -> {fmt(after)} {unit:<4s} ({arrow}{fmt(d)})   {why}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="cross-tier only, skip the lever sweep")
    args = ap.parse_args()

    base = baseline_config()
    fails = 0

    print("=" * 78)
    print("NSG FIDELITY REPORT   5 cells / 10 UEs / 3.5 GHz / 50 MHz")
    print("=" * 78)

    print("\n[1] CROSS-TIER — same config through both tiers")
    real = run_real(base)
    if real["status"] != "ok":
        print(f"  engine unusable: status={real['status']} — {real['error']}")
        return 1
    ana = run_analytic(base)

    ues = real["ue_count"] or 0
    real_total = (real["thp_mbps_per_ue"] or 0) * ues
    print(f"    (real run: {real['secs']:.0f}s, status={real['status']}, "
          f"retried={real['retried']})\n")
    print(f"    {'quantity':<22s}{'analytical':>14s}{'real engine':>16s}{'ratio':>10s}")
    print(f"    {'-' * 62}")
    ratio = (ana["thp_mbps"] / real_total) if real_total else None
    print(f"    {'total DL (Mbps)':<22s}{fmt(ana['thp_mbps']):>14s}"
          f"{fmt(real_total):>16s}{fmt(ratio, '.1f', '—') + 'x':>10s}")
    print(f"    {'active UEs':<22s}{fmt(ana['conn_ues'], '.1f'):>14s}{ues:>16d}"
          f"{'':>10s}")
    print(f"\n    not comparable (different quantities, shown for reference):")
    print(f"      analytical RSRP {fmt(ana['rsrp_dbm'], '.1f')} dBm   vs   "
          f"engine serving SINR {fmt(real['sinr_db'], '.2f')} dB")
    print(f"      analytical PRB  {fmt(ana['prb_pct'], '.1f')} % (demand proxy)   vs   "
          f"engine PRB {fmt(real['prb'])} (count of PRBs used)")
    e = real.get("energy") or {}
    print(f"      analytical power {fmt(ana['power_w'], '.0f')} W is CONFIGURED; the engine "
          f"MEASURES energy: {fmt(e.get('total_j'), '.0f')} J over {fmt(e.get('span_s'))} s of "
          f"sim time ({fmt(e.get('mean_power_w'), '.0f')} W mean, radio energy model) — "
          f"different quantities, shown side by side")

    if ratio is not None and (ratio > 3 or ratio < 1 / 3):
        print(f"\n    !! the two tiers disagree by {ratio:.1f}x on throughput.")
        print(f"       The built-in AI trains against the analytical tier and the")
        print(f"       Compare leaderboard ranks sandbox runs next to real ones, so")
        print(f"       this gap propagates into both.")

    if args.quick:
        # a parsable verdict line even in quick mode (the nightly watchdog
        # greps for RESULT:)
        print("\nRESULT: QUICK OK (cross-tier only — no lever sweep)")
        return 0

    print("\n[2] CONFIG LEVERS — does each design lever move the REAL engine?")
    print(f"    baseline: SINR {fmt(real['sinr_db'])} dB, PRB {fmt(real['prb'])}, "
          f"DL {fmt(real['thp_mbps_per_ue'])} Mbps/UE\n")

    # Level-3: sleep the cell the UEs actually sit on. Sleeping a far corner cell
    # proves nothing — it contributes no interference to this cluster, so the
    # measurement is entitled not to move. The direction here depends on the
    # layout (UEs fall back to cells further away), so only require an effect.
    # This is the AI's main lever and what the Compare leaderboard replays: if it
    # does not move the measurement, that ranking is measuring nothing.
    off = copy.deepcopy(base)
    off["base_stations"][4]["radio"]["tx_power_dbm"] = 0.0
    r_off = run_real(off)
    fails += not delta_line("L3 serving cell asleep", real["sinr_db"], r_off, "sinr_db",
                            "dB", "move", "UEs must fall back to other cells")
    # Power needs its OWN experiment: sleeping the SERVING cell above can
    # legitimately RAISE network power (the UEs fall to 0.7 dB links on far
    # cells and burn far more TX time on retransmissions — measured, and good
    # physics). The clean energy claim is about an idle cell: the corner cell
    # carries no UEs in this layout, so sleeping it must cut its draw without
    # anyone paying elsewhere.
    idle = copy.deepcopy(base)
    idle["base_stations"][0]["radio"]["tx_power_dbm"] = 0.0
    r_idle = run_real(idle)
    fails += not delta_line("L3 idle cell power", real["energy_w"], r_idle, "energy_w",
                            "W", "down", "a slept idle cell draws ~0, nobody pays")

    # Phase 2a: RU sleep states. Same idle corner cell, but NOT switched off —
    # it stays configured at full power and is left to descend into deep sleep
    # on its own once the shutdown timer expires. So this isolates the sleep
    # state machine from the (already proven) power lever: the only difference
    # from the baseline is that one cell is ALLOWED to sleep.
    slp = copy.deepcopy(base)
    slp["base_stations"][0]["radio"]["sleep"] = {
        "mode": "deep", "idle_timer_ms": 200, "wake_delay_ms": 10}
    r_slp = run_real(slp)
    fails += not delta_line("2a RU deep sleep", real["energy_w"], r_slp, "energy_w",
                            "W", "down", "an idle cell must descend and draw less")

    # NO antenna lever here, deliberately. Panel orientation IS real — measured
    # -59.5% rotated 180 deg and -70.9% tilted at the sky in
    # engines/nsoran/test_antenna_2c.py, on a geometry where every UE sits on
    # one side of the cells. On THIS baseline the UEs ring five cells at mixed
    # distances and elevations, which dilutes it: the same perturbation read
    # -17.5% before shadowing_enabled was wired to the engine and -2.7% after.
    # A fixed random_seed does not rescue it — the seed reproduces the SAME
    # config exactly, but two DIFFERENT configs diverge in their RNG draws, so
    # config-to-config variance survives seeding. A lever that flip-flops around
    # the threshold is worse than no lever; the magnitude proof is the dedicated
    # test, the same call made for mobility in mobility-2b.patch.

    # Level-4: drop buildings across the scene. Direction is genuinely ambiguous
    # (blocking an interferer helps, blocking your server hurts), so this only
    # asserts the lever has an effect at all.
    bld = copy.deepcopy(base)
    bld["buildings"] = [
        {"id": f"b{i}", "bounds": {"x_min": x, "x_max": x + 60, "y_min": y,
                                   "y_max": y + 60, "z_min": 0, "z_max": 30},
         "building_type": "office", "wall_material": "concrete",
         "floors": 8, "rooms_x": 4, "rooms_y": 4}
        for i, (x, y) in enumerate([(120, 120), (330, 120), (120, 330), (330, 330)])
    ]
    bld["settings"]["scenario"] = "UMa-Buildings"
    r_bld = run_real(bld)
    fails += not delta_line("L4 buildings added", real["sinr_db"], r_bld, "sinr_db",
                            "dB", "move", "real LOS/NLOS geometry must change SINR")
    # Consistency guard for the same run: carrying throughput on zero PRBs is
    # impossible. This is the regression net for the long-truncation bug that
    # zeroed RRU.PrbUsedDl whenever the per-UE PRB average fell below 1 (the
    # high-MCS regime buildings create).
    prb_ok = not ((r_bld.get("thp_mbps_per_ue") or 0) > 0) or (r_bld.get("prb") or 0) > 0
    fails += not prb_ok
    print(f"  [{'PASS' if prb_ok else 'FAIL'}] {'L4 PRB consistency':24s} "
          f"thp={fmt(r_bld.get('thp_mbps_per_ue'))} Mbps/UE with "
          f"PRB={fmt(r_bld.get('prb'))}   (thp > 0 requires PRB > 0)")

    # Level-5: triple every UE's demand. More offered load must occupy more PRBs.
    # Apply to ALL groups (the baseline splits the 10 UEs across four), so the
    # lever moves the whole population, not a quarter of it.
    load = copy.deepcopy(base)
    for g in load["ue_groups"]:
        g["services"][0]["target_throughput_gbps"] = 0.030
    r_load = run_real(load)
    # Direction is "move", not "up": since the scenario carries real uplink
    # (25% of the DL rate), tripled demand also triples UL, and in this TDD MAC
    # the UL flows COMPETE for the same symbol pool as DL (the scheduler splits
    # symbols across nFlowsDl + nFlowsUl together) — so under mixed saturation
    # the DL PRB share can legitimately fall while UL rises. The demand lever
    # itself is proven by the UL assertion below.
    fails += not delta_line("L5 slice demand x3", real["prb"], r_load, "prb",
                            "PRB", "move", "demand re-shapes the DL/UL symbol split")
    # Same run, second lever: uplink is offered at 25% of the DL slice rate, so
    # tripled demand must raise the MEASURED UL bitrate too.
    fails += not delta_line("L5 uplink follows demand", real["ul_kbps"], r_load,
                            "ul_kbps", "kbps", "up", "UL offered = 25% of DL slice rate")

    # Call dynamics: same offered rate, but bursty (2 s calls / 20 s idle ->
    # time-compressed to a ~9% duty cycle by the backend). Real traffic breathes;
    # a bursty service must carry measurably less than a continuous one, and the
    # engine must show it (this rides the full config -> backend -> engine path,
    # so it also guards the schema fields and the spec-string extension).
    calls = copy.deepcopy(base)
    for g in calls["ue_groups"]:
        g["services"][0]["avg_call_duration_s"] = 2.0
        g["services"][0]["avg_idle_duration_s"] = 20.0
    r_calls = run_real(calls)
    fails += not delta_line("L5 call dynamics", real["thp_mbps_per_ue"], r_calls,
                            "thp_mbps_per_ue", "Mbps", "down",
                            "9% duty cycle must carry less than continuous")

    # Level-6: slice quota. The quota is a runtime/E2 lever (not a config field),
    # so this pair drives the ENGINE API directly with identical explicit
    # parameters — only the quota differs. Capping the only slice at 10% of the
    # DL symbols must visibly cut its measured throughput.
    r_unc = run_engine_direct("")
    r_cap = run_engine_direct("9:10")
    if r_unc.get("thp_mbps_per_ue") and r_cap.get("thp_mbps_per_ue") is not None:
        drop = 1.0 - r_cap["thp_mbps_per_ue"] / r_unc["thp_mbps_per_ue"]
        ok = drop > 0.3
        fails += not ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {'L6 slice quota 10% cap':24s} "
              f"{fmt(r_unc['thp_mbps_per_ue'])} -> {fmt(r_cap['thp_mbps_per_ue'])} Mbps/UE "
              f"(-{drop * 100:.0f}%)   capping the slice must cut its throughput")
    else:
        fails += 1
        print(f"  [FAIL] {'L6 slice quota 10% cap':24s} {'NO DATA':>22s}   "
              f"uncapped={r_unc.get('status')} capped={r_cap.get('status')}")

    # Geometry guard: a "stadium" — one dense UE group sitting on a centre cell —
    # used to attach a dozen UEs to that one cell and corrupt the mmWave heap
    # (malloc abort, no KPM). The backend now scatters an over-cap crowd across
    # the cells (main._rebalance_ue_cells). This runs that exact crash pattern
    # through the real path and asserts it comes back ALIVE with real throughput —
    # so a regression that removes the guard shows up here, not as a user crash.
    stadium = copy.deepcopy(base)
    stadium["ue_groups"] = [{
        "id": "crowd", "name": "crowd", "count": 14,
        "spawn_position": {"x": 250, "y": 250},
        "mobility": {"pattern": "pedestrian", "speed_mps": 1.0},
        "services": [{"slice_type": "eMBB", "qos_id": 9,
                      "target_throughput_gbps": 0.020}],
    }]
    r_stad = run_real(stadium)
    stad_ok = r_stad.get("status") == "ok" and (r_stad.get("thp_mbps_per_ue") or 0) > 0
    fails += not stad_ok
    print(f"  [{'PASS' if stad_ok else 'FAIL'}] {'Geometry guard (stadium)':24s} "
          f"status={r_stad.get('status')} DL={fmt(r_stad.get('thp_mbps_per_ue'))} Mbps/UE"
          f"   (14 UEs on a centre cell must NOT crash the engine)")

    print("\n" + "=" * 78)
    print("RESULT:", "ALL LEVERS WIRED" if not fails else f"{fails} LEVER(S) NOT WIRED")
    print("=" * 78)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
