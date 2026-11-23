#!/usr/bin/env python3
"""
Does the generator build the scenario the user ASKED for?

The prompt-contract test proves the AI *can name* every capability. This proves it
*reaches for the right one* — a different failure, and the one that showed up in a
demo: asked for "2 cells, one user walking from one to the other, first cell
weaker, so it hands over", the generator returned four users and no handover
machinery at all. Neither was a hallucination:

  * FOUR users came from the rulebook ("at least 2 UEs per cell" x 2 cells). It
    obeyed a measurement-quality rule as if it were a safety rule, over an
    explicit instruction.
  * NO handover machinery because waypoints and the settings.ho_* block were
    absent from the prompt's schema skeleton. It could not say "handover".

Assertions are STRUCTURAL, never exact numbers — an LLM is allowed to pick 500 m
instead of 600 m. What it is not allowed to do is silently change the user's UE
count, or ship a handover scenario nothing in it can produce.

Needs a live model, so it is opt-in:
    cd nsg_ai && ../venv/bin/python test_ai_scenarios.py --llm
    # or, under pytest:   NSG_LLM_TESTS=1 pytest nsg_ai/

THE OPT-IN USED TO APPLY ONLY TO THE SCRIPT. `__main__` honoured `--llm`, and
pytest collected the model-calling tests anyway — so the suite went red every
time the API ran out of credit, for a reason that says nothing about this
codebase. A permanently-red test is worse than a missing one: it teaches whoever
runs the suite to skim past red, which is the state that lets a real failure
through. They skip unless asked for now.
"""
from __future__ import annotations

import math
import os
import sys

import pytest

# The three tests below call the real model. Everything else in this file works
# offline, so the file stays useful without an API key.
_LLM_TESTS = os.environ.get("NSG_LLM_TESTS") == "1" or "--llm" in sys.argv
needs_llm = pytest.mark.skipif(
    not _LLM_TESTS,
    reason="calls the live model — set NSG_LLM_TESTS=1 (or pass --llm) to run")

sys.path.insert(0, "/home/chancelor/nsg_ai_platform/nsg_ai")

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(name)


def generate(prompt: str):
    from llm_agent import modify_configuration  # noqa: PLC0415
    return modify_configuration({}, prompt)


def dist(a, b) -> float:
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


# ── the demo that failed ──────────────────────────────────────────────────────

HANDOVER_PROMPT = (
    "2 cell sites, a single user. The first cell's transmit power is lower than "
    "the second. The user walks from the first cell towards the second and hands over."
)


@needs_llm
def test_handover() -> None:
    print("\n[handover / mobility]")
    cfg = generate(HANDOVER_PROMPT)

    total_ue = sum(g.count for g in cfg.ue_groups)
    check("the user's UE count is respected, not rounded up to satisfy load guidance",
          total_ue == 1, f"{total_ue} UE(s)")

    check("two or more cells", len(cfg.base_stations) >= 2,
          f"{len(cfg.base_stations)} cells")

    powers = [b.radio.tx_power_dbm for b in cfg.base_stations]
    check("the cells differ in transmit power (what makes the handover inevitable)",
          len(set(powers)) > 1, f"{powers} dBm")

    if len(cfg.base_stations) >= 2:
        a, b = cfg.base_stations[0].position, cfg.base_stations[1].position
        sep = dist({"x": a.x, "y": a.y}, {"x": b.x, "y": b.y})
        check("cells are at distinct positions, hundreds of metres apart",
              sep >= 100.0, f"{sep:.0f} m")

    group = cfg.ue_groups[0] if cfg.ue_groups else None
    wps = (group.mobility.waypoints if group else None) or []
    check("a waypoint path exists (the only construct that produces handovers)",
          len(wps) >= 2, f"{len(wps)} waypoints")

    if len(wps) >= 2 and len(cfg.base_stations) >= 2:
        start, end = wps[0].position, wps[-1].position
        c0, c1 = cfg.base_stations[0].position, cfg.base_stations[1].position
        s = {"x": start.x, "y": start.y}
        e = {"x": end.x, "y": end.y}
        p0 = {"x": c0.x, "y": c0.y}
        p1 = {"x": c1.x, "y": c1.y}
        # The path has to CROSS: it must start nearer one cell and end nearer the
        # other. A path that stays inside one cell's coverage is a walk, not a
        # handover.
        crosses = (dist(s, p0) < dist(s, p1)) != (dist(e, p0) < dist(e, p1))
        check("the path crosses from one cell's coverage into the other's", crosses,
              f"start {dist(s, p0):.0f}/{dist(s, p1):.0f} m, "
              f"end {dist(e, p0):.0f}/{dist(e, p1):.0f} m")

    check("handover re-evaluation is enabled (0 means decide once at t=0)",
          cfg.settings.ho_check_period_ms > 0,
          f"ho_check_period_ms={cfg.settings.ho_check_period_ms}")

    check("a handover trigger is configured (A3 event or an A2 threshold)",
          cfg.settings.ho_a3_entry or cfg.settings.ho_a2_threshold_db is not None,
          f"a3={cfg.settings.ho_a3_entry} a2={cfg.settings.ho_a2_threshold_db}")

    # The crossing has to fit inside the run, or the handover never happens.
    if len(wps) >= 2 and group:
        span = sum(dist({"x": wps[i].position.x, "y": wps[i].position.y},
                        {"x": wps[i + 1].position.x, "y": wps[i + 1].position.y})
                   for i in range(len(wps) - 1))
        speed = max(0.1, group.mobility.speed_mps)
        needed = span / speed
        # NOT duration_s: it is used in full by the analytical tier but never
        # reaches the ns-O-RAN engine, so a generous duration_s buys no walking
        # there. What bounds the crossing is the KPM window budget — about 13
        # windows per simulated second.
        #
        # This used to hardcode 40 windows, which was the DEFAULT budget and
        # stopped being the only one when settings.kpm_collect_s shipped
        # (engine 1.13): the window is the horizon, and the generator is told in
        # the system prompt to raise it when a scenario turns on elapsed time.
        # Hardcoding the default failed a scenario that had correctly bought
        # itself more room, so the check now reads what the config actually asks
        # for. The default auto window for a small scenario is 10 s and yields
        # ~40 windows, i.e. ~4 windows per collection second.
        kpm_s = getattr(cfg.settings, "kpm_collect_s", None) or 10
        budget = 4 * kpm_s
        check(f"the walk fits the window budget it asked for "
              f"(kpm_collect_s={kpm_s}s -> ~{budget} windows)",
              needed * 13 <= budget,
              f"~{needed:.0f}s of walking needs ~{needed * 13:.0f} windows")

    # The conflict rule asks for the cost to be stated, not hidden.
    check("the description is written and mentions the handover",
          len(cfg.description) > 60 and "handover" in cfg.description.lower(),
          f"{len(cfg.description)} chars")


@needs_llm
def test_energy() -> None:
    print("\n[energy saving]")
    cfg = generate("Energy saving test: put the idle capacity cells to sleep "
                   "off-peak and show the power saving.")
    modes = [b.radio.sleep.mode for b in cfg.base_stations]
    check("at least one cell is given a sleep depth",
          any(m != "none" for m in modes), f"{modes}")


@needs_llm
def test_fault() -> None:
    print("\n[fault / resilience]")
    cfg = generate("What happens if one cell suffers interference halfway "
                   "through the run, without losing transmit power?")
    kinds = [a.type.value for a in (cfg.anomalies or [])]
    check("an anomaly is staged", len(kinds) > 0, f"{kinds}")
    # "interference without losing power" is exactly noise_rise; a power drop
    # would be the wrong fault for the question asked.
    check("the anomaly matches the question (noise, not a power drop)",
          "noise_rise" in kinds, f"{kinds}")


# ── does the design actually REACH the engine? ────────────────────────────────
#
# The tests above check what the generator writes. This checks what survives the
# translation to ns-3 parameters, which is a separate failure: a scenario can be
# designed perfectly and still measure the wrong thing if the pipeline moves it.
#
# It did. `_ue_scenario_positions` spreads a group over a golden-angle disc, and
# that spiral has no point at its origin — so a group of ONE landed
# spread * sqrt(0.5) away from the spawn point the user placed. Measured on the
# generator's own handover scenario: 127 m east, which took the walking UE from
# 80 m off its serving cell to 207 m and carried the far end of its path 47 m
# PAST the target cell instead of stopping 80 m short of it. Nothing errored; the
# run just answered a question nobody asked.
#
# Needs no model, so unlike everything above it runs by default.

def _geometry_config(count: int, waypoints: list | None = None):
    """Two cells 600 m apart, one UE group spawned 80 m east of the first.

    Waypoints are passed to the CONSTRUCTOR, never assigned afterwards: the model
    does not validate on assignment, so a list of plain dicts set on
    `mobility.waypoints` stays a list of dicts, and the payload builder — which
    reads `w.position` — silently drops every one and emits no path at all.
    """
    from schema import NSGSimulationConfig  # noqa: PLC0415
    band = {"label": "NR-N77", "frequency_mhz": 3600.0,
            "bandwidth_mhz": 20.0, "numerology": 1}
    mobility: dict = {"pattern": "pedestrian", "speed_mps": 1.4}
    if waypoints is not None:
        mobility["waypoints"] = waypoints
        mobility["waypoint_mode"] = "once"
    return NSGSimulationConfig(
        name="geometry", description="placement fidelity",
        base_stations=[
            {"id": "a", "name": "A", "position": {"x": 0.0, "y": 0.0, "z": 30.0},
             "radio": {"band": band, "tx_power_dbm": 40.0}},
            {"id": "b", "name": "B", "position": {"x": 600.0, "y": 0.0, "z": 30.0},
             "radio": {"band": band, "tx_power_dbm": 46.0}},
        ],
        ue_groups=[{"id": "g", "name": "G", "count": count,
                    "spawn_position": {"x": 80.0, "y": 0.0},
                    "mobility": mobility,
                    "services": [{"slice_type": "eMBB", "qos_id": 9,
                                  "protocol": "udp",
                                  "target_throughput_gbps": 0.05}]}],
        area={"x_min": -200.0, "x_max": 800.0, "y_min": -300.0, "y_max": 300.0,
              "resolution_m": 10.0},
    )


def test_placement_reaches_engine() -> None:
    """A group's UEs must sit ON the spawn point the design gave them."""
    from main import nsoran_params  # noqa: PLC0415
    print("\n[placement / design -> engine]")
    for count in (1, 2, 4, 8):
        p = nsoran_params(_geometry_config(count))
        ax = float(p["gnb_positions"].split(";")[0].split(",")[0])
        pts = [tuple(map(float, u.split(",")[:2]))
               for u in p["ue_positions"].split(";") if u]
        cx = sum(q[0] for q in pts) / len(pts)
        cy = sum(q[1] for q in pts) / len(pts)
        # 80 m east of cell A, and on its latitude — exactly as designed.
        check(f"a group of {count} is centred on its spawn point",
              abs((cx - ax) - 80.0) < 1.0 and abs(cy - 2000.0) < 1.0,
              f"{cx - ax:.1f} m east, {cy - 2000.0:+.1f} m north")


def test_waypoint_path_reaches_engine() -> None:
    """A waypoint path must keep its designed relationship to the CELLS, not just
    its shape: it is translated to each UE's start, so a displaced start silently
    slides the whole walk."""
    from main import nsoran_params  # noqa: PLC0415
    print("\n[waypoints / design -> engine]")
    cfg = _geometry_config(1, waypoints=[
        {"position": {"x": 80.0, "y": 0.0, "z": 1.5}, "speed_mps": 1.4},
        {"position": {"x": 520.0, "y": 0.0, "z": 1.5}, "speed_mps": 1.4},
    ])
    p = nsoran_params(cfg)
    check("a designed waypoint path reaches the engine at all",
          bool(p["ue_waypoints"].strip()))
    ax, bx = (float(g.split(",")[0]) for g in p["gnb_positions"].split(";"))
    legs = p["ue_waypoints"].split("|")
    start = float(legs[0].split(",")[0])
    end = float(legs[-1].split(",")[0])
    check("the walk starts 80 m off the serving cell, as designed",
          abs((start - ax) - 80.0) < 1.0, f"{start - ax:.1f} m")
    check("the walk stops SHORT of the target cell, not past it",
          end < bx, f"ends {end - bx:+.1f} m relative to the target cell")
    check("it stops 80 m short, as designed",
          abs((bx - end) - 80.0) < 1.0, f"{bx - end:.1f} m short")


def test_walking_group_stays_a_formation() -> None:
    """A group with a path is a formation; a group with only a spawn is a crowd.

    The population disc is right for a crowd — it is what keeps every cell
    loaded — and wrong for a group the user placed on a line. Measured before
    the fix: four pedestrians meant to walk a 40 m path were scattered over a
    160 m disc, one parked on each cell, and the campaign reported zero
    handovers. Same scenario after: five.
    """
    from main import nsoran_params  # noqa: PLC0415
    print("\n[formation / walkers vs crowd]")

    def radius(cfg) -> float:
        p = nsoran_params(cfg)
        pts = [tuple(map(float, u.split(",")[:2]))
               for u in p["ue_positions"].split(";") if u]
        cx = sum(q[0] for q in pts) / len(pts)
        cy = sum(q[1] for q in pts) / len(pts)
        return max(math.hypot(q[0] - cx, q[1] - cy) for q in pts)

    walkers = radius(_geometry_config(4, waypoints=[
        {"position": {"x": 55.0, "y": 0.0, "z": 1.5}, "speed_mps": 1.4},
        {"position": {"x": 95.0, "y": 0.0, "z": 1.5}, "speed_mps": 1.4},
    ]))
    crowd = radius(_geometry_config(4))

    check("a group WITH a path stays a tight formation",
          walkers <= 20.0, f"{walkers:.1f} m")
    check("a group with only a spawn point still spreads to load the cells",
          crowd >= 50.0, f"{crowd:.1f} m")
    check("the two are actually treated differently",
          crowd > walkers * 2, f"crowd {crowd:.1f} m vs walkers {walkers:.1f} m")


def main() -> int:
    for name, fn in (("placement", test_placement_reaches_engine),
                     ("waypoints", test_waypoint_path_reaches_engine),
                     ("formation", test_walking_group_stays_a_formation)):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(f"{name} checked without error", False, str(exc)[:90])

    if "--llm" not in sys.argv:
        print("\nThe generator tests call the real model. Re-run with --llm to "
              "include them.")
        print("\n" + ("PLACEMENT CHECKS PASSED" if not FAILS
                      else f"{len(FAILS)} CHECK(S) FAILED: {FAILS}"))
        return 1 if FAILS else 0
    for name, fn in (("handover", test_handover),
                     ("energy", test_energy),
                     ("fault", test_fault)):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(f"{name} scenario generated without error", False, str(exc)[:90])
    print("\n" + ("ALL AI SCENARIO CHECKS PASSED" if not FAILS
                  else f"{len(FAILS)} CHECK(S) FAILED: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
