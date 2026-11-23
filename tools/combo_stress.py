#!/usr/bin/env python3
"""
Combinatorial stress of the config surface — the parameters we just added, in
combination, through the SAME path the UI uses.

Why combinations: every parameter here has been tested alone. Bugs that survive
that live in the interaction — a fault scheduled on a cell that is also asleep, a
waypoint path with a handover threshold that never triggers, a slice policy on a
5QI no UE carries. And the day's three real bugs were all of this shape: a value
that was individually fine and wrong in context.

Two tiers, cheap first:

  TIER 1 (seconds, hundreds of cases) — build the config, push it through
    POST /config/normalize (the editor's actual Sync path), then through
    nsoran_params (the engine translator). Catches: schema rejections, dropped
    fields, exceptions in the translator, and mutations that silently do not
    reach the engine payload.

  TIER 2 (--engine, minutes each) — run a selected subset on the real engine and
    require status ok with non-degenerate metrics. Catches: what only the engine
    knows, e.g. a combination that aborts ns-3.

  python3 tools/combo_stress.py                 # tier 1
  python3 tools/combo_stress.py --engine        # + tier 2 on the risky subset
  python3 tools/combo_stress.py --engine --all  # + tier 2 on everything (slow)
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "nsg_ai"))

API = "http://localhost:8000"
ENGINE_RUN = "http://localhost:8090/run"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


# ── the known-good starting point ─────────────────────────────────────────────

def base() -> dict:
    """4 cells in a square + a UE group on three of them, inside the envelope.

    Cell c1 is deliberately left WITHOUT a nearby UE group: several mutations
    below (sleep, outage) only mean anything on a cell that is idle or whose loss
    is survivable, and a base where every cell is loaded cannot express them.
    """
    cells = [("c1", 0.0, 0.0), ("c2", 500.0, 0.0), ("c3", 0.0, 500.0), ("c4", 500.0, 500.0)]
    return {
        "name": "combo-stress",
        "description": "combinatorial stress base",
        "base_stations": [
            {"id": cid, "name": cid.upper(),
             "position": {"x": x, "y": y, "z": 25.0},
             "radio": {"band": {"label": "N78", "frequency_mhz": 3500.0,
                                "bandwidth_mhz": 50.0, "numerology": 2},
                       "tx_power_dbm": 44.0}}
            for cid, x, y in cells],
        "ue_groups": [
            {"id": f"u{i}", "name": f"U{i}", "count": 3,
             "spawn_position": {"x": x, "y": y},
             "services": [{"slice_type": "eMBB", "qos_id": 9,
                           "target_throughput_gbps": 0.02}]}
            for i, (_, x, y) in enumerate(cells[1:], start=2)],
        "buildings": [],
        "area": {"x_min": -400.0, "x_max": 900.0, "y_min": -400.0, "y_max": 900.0,
                 "resolution_m": 40.0},
        "settings": {"duration_s": 20, "report_interval_ms": 500,
                     "scenario": "UMa", "shadowing_enabled": True,
                     "random_seed": 42},
    }


# ── mutations ─────────────────────────────────────────────────────────────────
# Each is (name, apply, fingerprint). `fingerprint` names an engine-payload key
# whose value must DIFFER from the unmutated payload — a mutation that changes
# nothing in the payload is a mutation the engine will never see, which is the
# inert-field bug class this project already closed once.

def _s(cfg: dict, **kw) -> dict:
    cfg["settings"].update(kw)
    return cfg


MUTATIONS: list[tuple[str, callable, str | None]] = [
    # ── anomalies, including the two added yesterday ──────────────────────────
    # start_s is in SIMULATED seconds, and a batch run reaches ~0.4 s of them.
    # MEASURED: the same degradation at start_s=5.0 produced a run byte-identical
    # to one with no fault (10505.89 kbps) and at start_s=0.05 cost 9.2%. A
    # harness that schedules faults at 5 s tests nothing and reports PASS.
    ("anomaly:outage",
     lambda c: c | {"anomalies": [{"type": "cell_outage", "cell_id": "c1",
                                   "start_s": 0.05, "duration_s": 0.1}]}, "gnb_faults"),
    ("anomaly:degrade",
     lambda c: c | {"anomalies": [{"type": "radio_degradation", "cell_id": "c2",
                                   "start_s": 0.05, "duration_s": 0.1,
                                   "tx_power_dbm": 20.0}]}, "gnb_faults"),
    ("anomaly:noise",
     lambda c: c | {"anomalies": [{"type": "noise_rise", "cell_id": "c2",
                                   "start_s": 0.05, "noise_rise_db": 30.0}]}, "gnb_faults"),
    ("anomaly:ho_block",
     lambda c: c | {"anomalies": [{"type": "handover_block", "cell_id": "c1",
                                   "start_s": 0.05, "duration_s": 0.1}]}, "gnb_faults"),
    ("anomaly:three_at_once",
     lambda c: c | {"anomalies": [
         {"type": "cell_outage", "cell_id": "c1", "start_s": 0.03, "duration_s": 0.08},
         {"type": "noise_rise", "cell_id": "c2", "start_s": 0.06, "noise_rise_db": 25.0},
         {"type": "handover_block", "cell_id": "c3", "start_s": 0.08, "duration_s": 0.1},
     ]}, "gnb_faults"),
    ("anomaly:overlapping_same_cell",
     lambda c: c | {"anomalies": [
         {"type": "radio_degradation", "cell_id": "c2", "start_s": 0.04,
          "duration_s": 0.2, "tx_power_dbm": 25.0},
         {"type": "noise_rise", "cell_id": "c2", "start_s": 0.06,
          "duration_s": 0.1, "noise_rise_db": 20.0},
     ]}, "gnb_faults"),
    ("anomaly:zero_duration_forever",
     lambda c: c | {"anomalies": [{"type": "cell_outage", "cell_id": "c1",
                                   "start_s": 0.02, "duration_s": 0.0}]}, "gnb_faults"),
    ("anomaly:after_the_run_ends",
     lambda c: c | {"anomalies": [{"type": "cell_outage", "cell_id": "c1",
                                   "start_s": 9999.0, "duration_s": 5.0}]}, "gnb_faults"),

    # ── sleep ────────────────────────────────────────────────────────────────
    ("sleep:deep_on_idle_cell",
     lambda c: _sleep(c, "c1", "deep", 100.0), "gnb_sleep"),
    ("sleep:micro_everywhere",
     lambda c: _sleep_all(c, "micro", 50.0), "gnb_sleep"),
    ("sleep:deep_on_loaded_cell",
     lambda c: _sleep(c, "c2", "deep", 100.0), "gnb_sleep"),

    # ── handover event layer ─────────────────────────────────────────────────
    ("ho:a3_eager",
     lambda c: _s(c, ho_check_period_ms=100, ho_a3_entry=True,
                  ho_sinr_margin_db=1.0, ho_hysteresis_db=0.0), "ho_a3"),
    ("ho:a3_sticky",
     lambda c: _s(c, ho_check_period_ms=100, ho_a3_entry=True,
                  ho_sinr_margin_db=15.0, ho_hysteresis_db=5.0), "ho_hyst"),
    ("ho:a2_only",
     lambda c: _s(c, ho_check_period_ms=100, ho_a3_entry=False,
                  ho_a2_threshold_db=-4.0), "ho_a2"),
    ("ho:evaluation_off",
     lambda c: _s(c, ho_check_period_ms=0), "ho_check_ms"),
    ("ho:mode_noauto",
     lambda c: _s(c, handover_mode="NoAuto"), "ho_mode"),
    ("ho:mode_threshold",
     lambda c: _s(c, handover_mode="Threshold"), "ho_mode"),
    ("ho:mode_fixedttt",
     lambda c: _s(c, handover_mode="FixedTtt", ho_ttt_ms=40), "ho_mode"),
    ("ho:timers_tight",
     lambda c: _s(c, ho_joining_timeout_ms=2.0, ho_leaving_timeout_ms=2.0), "ho_join_ms"),
    ("ho:timers_loose",
     lambda c: _s(c, ho_joining_timeout_ms=4000.0, ho_leaving_timeout_ms=4000.0),
     "ho_join_ms"),

    # ── mobility ─────────────────────────────────────────────────────────────
    ("mobility:waypoints_once",
     lambda c: _wp(c, "once", 1.4), "ue_waypoints"),
    ("mobility:waypoints_pingpong_fast",
     lambda c: _wp(c, "pingpong", 25.0), "ue_waypoints"),
    ("mobility:waypoints_repeat_drone",
     lambda c: _wp(c, "repeat", 12.0, z=60.0), "ue_waypoints"),
    ("mobility:static_zero_speed",
     lambda c: _mob(c, "static", 0.0), "ue_speeds"),
    ("mobility:vehicle_fast",
     lambda c: _mob(c, "vehicle", 30.0), "ue_speeds"),

    # ── traffic / slices ─────────────────────────────────────────────────────
    ("traffic:multi_service_per_ue",
     lambda c: _svc(c, [{"slice_type": "voice", "qos_id": 1,
                         "target_throughput_gbps": 0.001},
                        {"slice_type": "eMBB", "qos_id": 9,
                         "target_throughput_gbps": 0.02}]), "ue_slices"),
    ("traffic:bursty_calls",
     lambda c: _svc(c, [{"slice_type": "eMBB", "qos_id": 9,
                         "target_throughput_gbps": 0.02,
                         "call_distribution": "exponential",
                         "avg_call_duration_s": 30, "avg_idle_duration_s": 10}]),
     "ue_slices"),
    ("traffic:tcp",
     lambda c: _svc(c, [{"slice_type": "eMBB", "qos_id": 9,
                         "target_throughput_gbps": 0.02, "protocol": "tcp"}]),
     "ue_slices"),
    ("slices:cap_embb",
     lambda c: c | {"slices": [{"qos_id": 9, "max_pct": 20.0, "min_pct": 0.0,
                                "dedicated_pct": 0.0}]}, "slice_quota"),
    ("slices:dedicated_for_unused_qci",
     lambda c: c | {"slices": [{"qos_id": 82, "max_pct": 50.0, "min_pct": 30.0,
                                "dedicated_pct": 30.0}]}, "slice_quota"),

    # ── radio / band edges ───────────────────────────────────────────────────
    ("band:bw_min",
     lambda c: _band(c, bandwidth_mhz=10.0), "bw_hz"),
    ("band:bw_over_envelope",
     lambda c: _band(c, bandwidth_mhz=100.0), "bw_hz"),
    ("band:numerology_out_of_engine_range",
     lambda c: _band(c, numerology=0), "numerology"),
    ("band:low_band",
     lambda c: _band(c, frequency_mhz=700.0), "freq_hz"),
    ("band:mmwave",
     lambda c: _band(c, frequency_mhz=28000.0), "freq_hz"),

    # ── antenna / PHY ────────────────────────────────────────────────────────
    ("antenna:3gpp_element_with_tilt",
     lambda c: _ant(_s(c, antenna_element="3gpp"), azimuth_deg=90.0, tilt_deg=12.0),
     "gnb_antenna"),
    ("phy:error_model_nr",
     lambda c: _s(c, error_model="nr_cc_table2"), "error_model_type"),
    ("phy:harq_off",
     lambda c: _s(c, harq_enabled=False), "harq_enabled"),
    ("phy:amc_shannon",
     lambda c: _s(c, amc_model="shannon"), "amc_model"),

    # ── envelope edges: these must FAIL HONESTLY, not silently ───────────────
    ("envelope:one_ue",
     lambda c: _one_ue(c), "n_ue"),
    ("envelope:two_cells",
     lambda c: _cells(c, 2), "n_gnb"),
    ("envelope:seven_cells",
     lambda c: _cells(c, 7), "n_gnb"),
    ("envelope:sixteen_ues",
     lambda c: _ue_count(c, 6), "n_ue"),
]


# ── mutation helpers ─────────────────────────────────────────────────────────

def _sleep(cfg, cell_id, mode, timer):
    for b in cfg["base_stations"]:
        if b["id"] == cell_id:
            b["radio"]["sleep"] = {"mode": mode, "idle_timer_ms": timer}
    return cfg


def _sleep_all(cfg, mode, timer):
    for b in cfg["base_stations"]:
        b["radio"]["sleep"] = {"mode": mode, "idle_timer_ms": timer}
    return cfg


def _wp(cfg, mode, speed, z=1.5):
    g = cfg["ue_groups"][0]
    g["mobility"] = {
        "pattern": "pedestrian", "speed_mps": speed, "waypoint_mode": mode,
        "waypoints": [
            {"position": {"x": 480.0, "y": 20.0, "z": z}, "speed_mps": speed},
            {"position": {"x": 20.0, "y": 480.0, "z": z}, "speed_mps": speed},
        ],
    }
    return cfg


def _mob(cfg, pattern, speed):
    for g in cfg["ue_groups"]:
        g["mobility"] = {"pattern": pattern, "speed_mps": speed}
    return cfg


def _svc(cfg, services):
    cfg["ue_groups"][0]["services"] = services
    return cfg


def _band(cfg, **kw):
    for b in cfg["base_stations"]:
        b["radio"]["band"].update(kw)
    return cfg


def _ant(cfg, **kw):
    for b in cfg["base_stations"]:
        b["radio"]["antenna"] = dict(kw)
    return cfg


def _one_ue(cfg):
    cfg["ue_groups"] = [cfg["ue_groups"][0]]
    cfg["ue_groups"][0]["count"] = 1
    return cfg


def _cells(cfg, n):
    grid = [(0.0, 0.0), (500.0, 0.0), (0.0, 500.0), (500.0, 500.0),
            (250.0, 250.0), (750.0, 250.0), (250.0, 750.0)]
    proto = cfg["base_stations"][0]
    cfg["base_stations"] = []
    for i, (x, y) in enumerate(grid[:n], start=1):
        b = copy.deepcopy(proto)
        b["id"], b["name"] = f"c{i}", f"C{i}"
        b["position"] = {"x": x, "y": y, "z": 25.0}
        cfg["base_stations"].append(b)
    cfg["ue_groups"] = [g for g in cfg["ue_groups"]][:max(1, n - 1)]
    return cfg


def _ue_count(cfg, per_group):
    for g in cfg["ue_groups"]:
        g["count"] = per_group
    return cfg


# ── pairs worth trying together ──────────────────────────────────────────────
# Not the full cross product (that is thousands of engine-minutes) — the pairs
# where one mutation could plausibly hide or break the other.
PAIRS: list[tuple[str, str]] = [
    ("anomaly:outage", "sleep:deep_on_idle_cell"),      # fault a cell that also sleeps
    ("anomaly:noise", "sleep:micro_everywhere"),        # noise vs the sleep noise figure
    ("anomaly:ho_block", "ho:a3_eager"),                # block what the trigger asks for
    ("anomaly:ho_block", "mobility:waypoints_once"),    # the demo's failure mode
    ("mobility:waypoints_pingpong_fast", "ho:a3_eager"),
    ("mobility:waypoints_once", "ho:evaluation_off"),   # path with HO evaluation off
    ("mobility:waypoints_once", "ho:mode_noauto"),
    ("ho:timers_tight", "mobility:waypoints_pingpong_fast"),
    ("slices:cap_embb", "traffic:multi_service_per_ue"),
    ("slices:dedicated_for_unused_qci", "traffic:multi_service_per_ue"),
    ("band:bw_min", "traffic:bursty_calls"),            # narrow band + bursty demand
    ("band:mmwave", "antenna:3gpp_element_with_tilt"),
    ("band:numerology_out_of_engine_range", "band:mmwave"),
    ("envelope:two_cells", "mobility:waypoints_once"),  # the reported demo shape
    ("envelope:one_ue", "mobility:waypoints_once"),
    ("envelope:seven_cells", "sleep:micro_everywhere"),
    ("envelope:sixteen_ues", "slices:cap_embb"),
    ("phy:harq_off", "band:bw_min"),
    ("phy:amc_shannon", "phy:error_model_nr"),          # AMC and receiver disagreeing
]

# Mutations that intentionally reach the engine UNCHANGED, because the envelope
# clamps them. Listed so the harness does not cry wolf about the two behaviours
# the platform documents as deliberate — and so that a THIRD one appearing is
# immediately visible as something nobody decided.
EXPECTED_CLAMPS: dict[str, str] = {
    "band:bw_over_envelope":
        "100 MHz clamped to the envelope's 50 (clamp_bw_mhz) — wide bandwidth "
        "under-loads cells and PRB goes sparse",
    "band:numerology_out_of_engine_range":
        "numerology 0 clamped to 2 — the engine defines NrNumerology2/3 only",
    "band:numerology_out_of_engine_range + band:mmwave":
        "numerology clamp again; the frequency change is what mmwave contributes",
}

# The subset that gets a real engine run in tier 2 without --all: the ones whose
# failure would be invisible in the payload.
ENGINE_SUBSET = [
    "anomaly:three_at_once", "anomaly:overlapping_same_cell",
    "sleep:deep_on_loaded_cell", "ho:timers_tight", "ho:mode_noauto",
    "mobility:waypoints_pingpong_fast", "traffic:multi_service_per_ue",
    "slices:dedicated_for_unused_qci", "band:numerology_out_of_engine_range",
    "envelope:one_ue", "envelope:two_cells",
    "anomaly:outage + sleep:deep_on_idle_cell",
    "anomaly:ho_block + mobility:waypoints_once",
    "phy:amc_shannon + phy:error_model_nr",
]


# ── plumbing ─────────────────────────────────────────────────────────────────

def normalize(cfg: dict) -> tuple[dict | None, str | None]:
    """The editor's Sync path. Returns (config, error)."""
    body = json.dumps(cfg).encode()
    req = urllib.request.Request(f"{API}/config/normalize", body,
                                 {"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))["config"], None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        return None, f"HTTP {exc.code}: {detail}"
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def translate(cfg: dict) -> tuple[dict | None, str | None]:
    from main import nsoran_params                      # noqa: PLC0415
    from schema import NSGSimulationConfig              # noqa: PLC0415
    try:
        return nsoran_params(NSGSimulationConfig(**cfg)), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def engine_run(params: dict, sim_time: int = 15) -> dict:
    body = json.dumps({"sim_time": sim_time, **params}).encode()
    for _ in range(6):
        req = urllib.request.Request(ENGINE_RUN, body,
                                     {"Content-Type": "application/json"})
        d = json.load(urllib.request.urlopen(req, timeout=900))
        if d.get("status") != "busy":
            return d
        time.sleep(20)
    return {"status": "busy", "error": "engine stayed busy"}


def build(names: list[str]) -> dict:
    cfg = base()
    by_name = {n: fn for n, fn, _ in MUTATIONS}
    for n in names:
        cfg = by_name[n](cfg) or cfg
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", action="store_true", help="also run tier 2")
    ap.add_argument("--all", action="store_true", help="tier 2 on every case")
    args = ap.parse_args()

    cases: list[tuple[str, list[str]]] = [(n, [n]) for n, _, _ in MUTATIONS]
    cases += [(f"{a} + {b}", [a, b]) for a, b in PAIRS]

    baseline_params, err = translate(base())
    if err:
        print(f"{RED}the BASE config does not even translate: {err}{RESET}")
        return 2
    fingerprints = {n: fp for n, _, fp in MUTATIONS}

    print("\n" + "=" * 78)
    print(f"TIER 1 — normalize + translate  ({len(cases)} cases)")
    print("=" * 78)

    failures: list[tuple[str, str]] = []
    inert: list[tuple[str, str]] = []
    ok_names: list[str] = []

    for label, names in cases:
        cfg = build(names)
        normalized, err = normalize(cfg)
        if err:
            print(f"  {RED}REJECTED{RESET}  {label}\n            {err[:200]}")
            failures.append((label, f"normalize: {err[:160]}"))
            continue
        params, err = translate(normalized)
        if err:
            print(f"  {RED}TRANSLATE{RESET} {label}\n            {err[:200]}")
            failures.append((label, f"translate: {err[:160]}"))
            continue

        # Did the mutation reach the payload AT ALL? Diff every key rather than a
        # guessed one: a wrong guess reports a false gap, which is the same
        # mistake as trusting an unmeasured field. The changed keys are printed so
        # a real gap is distinguishable from a documented clamp.
        changed = sorted(k for k in set(params) | set(baseline_params)
                         if params.get(k) != baseline_params.get(k))
        if not changed:
            why = EXPECTED_CLAMPS.get(label)
            if why:
                print(f"  {GREEN}clamped{RESET}   {label}  {DIM}{why}{RESET}")
            else:
                print(f"  {YELLOW}NO EFFECT{RESET} {label}  "
                      f"{DIM}nothing in the engine payload changed{RESET}")
                inert.append((label, "payload identical to baseline"))
        else:
            shown = ", ".join(changed[:4]) + (" …" if len(changed) > 4 else "")
            print(f"  {GREEN}ok{RESET}        {label}  {DIM}→ {shown}{RESET}")
        ok_names.append(label)

    print(f"\n  {len(ok_names)}/{len(cases)} built and translated · "
          f"{len(failures)} rejected · {len(inert)} reached the engine unchanged")

    if args.engine:
        subset = ok_names if args.all else [c for c in ok_names if c in ENGINE_SUBSET]
        print("\n" + "=" * 78)
        print(f"TIER 2 — real engine runs  ({len(subset)} cases, minutes each)")
        print("=" * 78)
        for label in subset:
            names = next(ns for lb, ns in cases if lb == label)
            normalized, _ = normalize(build(names))
            params, _ = translate(normalized)
            t0 = time.time()
            d = engine_run(params)
            status = d.get("status")
            m = (d.get("measurements") or {})
            thp = (m.get("DRB.UEThpDl") or {}).get("mean")
            sinr = (m.get("L3servingSINR") or {}).get("mean")
            took = time.time() - t0
            if status != "ok":
                print(f"  {RED}{status.upper():9s}{RESET} {label}  "
                      f"{DIM}{(d.get('error') or '')[:90]}{RESET}")
                failures.append((label, f"engine: {status} {(d.get('error') or '')[:120]}"))
            elif not thp:
                print(f"  {YELLOW}NO DATA{RESET}   {label}  "
                      f"{DIM}status ok but no throughput ({took:.0f}s){RESET}")
                failures.append((label, "engine: ok but no throughput reported"))
            else:
                print(f"  {GREEN}ok{RESET}        {label}  "
                      f"{DIM}thp={thp:.0f} kbps sinr={sinr} ({took:.0f}s){RESET}")

    print("\n" + "=" * 78)
    if failures:
        print(f"{RED}{len(failures)} PROBLEM(S){RESET}")
        for label, why in failures:
            print(f"  · {label}\n      {why}")
    else:
        print(f"{GREEN}no failures{RESET}")
    if inert:
        print(f"\n{YELLOW}{len(inert)} mutation(s) reached the engine unchanged{RESET} "
              f"— each is either a tier-scoped field or a wiring gap:")
        for label, keys in inert:
            print(f"  · {label}  ({keys})")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
