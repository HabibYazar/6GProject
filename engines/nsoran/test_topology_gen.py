#!/usr/bin/env python3
"""Roadmap item 1 — procedural topology generation.

The generator's promise is not "emits a valid config" but "emits a config the
ENGINE runs". So this checks the two things that make the difference, both
through the platform's real translation rather than the generator's own
coordinates:

  1. the envelope traps — a UE on top of a base station, a cell left with no
     UE, overlapping buildings, a cell on the LTE anchor;
  2. that the layout actually reaches ns-3 — a Manhattan grid full of concrete
     must come back with worse modulation than open hex, or the buildings were
     decoration.

Run:  python3 test_topology_gen.py            # geometry only, seconds
      python3 test_topology_gen.py --engine   # + three real engine runs, ~6 min
"""
import json
import math
import sys
import urllib.request

sys.path.insert(0, "/home/chancelor/nsg_ai_platform")
sys.path.insert(0, "/home/chancelor/nsg_ai_platform/nsg_ai")

from nsg_ai.main import nsoran_params                        # noqa: E402
from nsg_ai.schema import NSGSimulationConfig                # noqa: E402
import topology                                              # noqa: E402

ENGINE = "http://localhost:8090/run"
LAYOUTS = ("hex", "manhattan", "scatter")
SEEDS = (5, 11, 42, 77, 128)
# The measured floor: a UE closer than this to its cell is a log10(0) link.
UE_CELL_CLEAR_M = 25.0

fails: list[str] = []


def check(ok: bool, msg: str) -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {msg}")
    if not ok:
        fails.append(msg)
    return ok


def points(p: dict, key: str) -> list[tuple[float, float]]:
    return [tuple(float(v) for v in s.split(",")[:2]) for s in p[key].split(";") if s]


def geometry() -> None:
    print("\n=== geometry: every layout x seed lands inside the envelope ===")
    for layout in LAYOUTS:
        for seed in SEEDS:
            cfg = topology.generate(topology.TopologyRequest(
                layout=layout, cells=7, isd_m=350, ues_per_cell=2, seed=seed))
            p = nsoran_params(NSGSimulationConfig(**cfg))   # must validate
            ue, gnb = points(p, "ue_positions"), points(p, "gnb_positions")

            near = min(math.dist(u, g) for u in ue for g in gnb)
            nearest = [min(range(len(gnb)), key=lambda i: math.dist(u, gnb[i]))
                       for u in ue]
            per_cell = [nearest.count(i) for i in range(len(gnb))]

            tag = f"{layout}/seed {seed}"
            check(near >= UE_CELL_CLEAR_M,
                  f"{tag}: closest UE-cell {near:.0f} m >= {UE_CELL_CLEAR_M:.0f} m")
            # The one that used to fail. A starved cell is not a crash, it is
            # worse: the engine reports no DU metrics and it reads as a dead
            # engine. 'scatter' re-rolls until this holds.
            check(min(per_cell) >= 1,
                  f"{tag}: every cell keeps a UE {per_cell}")

            # Overlapping footprints NS_FATAL the run before any KPM appears.
            b = [x["bounds"] for x in cfg["buildings"]]
            overlap = any(
                b[i]["x_min"] < b[j]["x_max"] and b[j]["x_min"] < b[i]["x_max"] and
                b[i]["y_min"] < b[j]["y_max"] and b[j]["y_min"] < b[i]["y_max"]
                for i in range(len(b)) for j in range(i + 1, len(b)))
            check(not overlap, f"{tag}: {len(b)} buildings, none overlapping")


def clamping() -> None:
    print("\n=== clamping: an over-ambitious ask comes back runnable ===")
    cfg = topology.generate(topology.TopologyRequest(
        layout="hex", cells=7, isd_m=350, ues_per_cell=8, seed=1))
    p = nsoran_params(NSGSimulationConfig(**cfg))
    # 7 x 8 = 56 UEs is far past the ceiling; the generator must shrink the
    # per-cell count itself rather than let the platform truncate silently.
    check(p["n_ue"] <= 16, f"56 UEs asked -> {p['n_ue']} emitted (ceiling 16)")
    check(p["n_gnb"] == 7, f"7 cells preserved (got {p['n_gnb']})")


def server_import_layout() -> None:
    """The generator must work under the SERVER's import layout, not just the
    test's.

    The server is `uvicorn main:app` launched from inside nsg_ai/, so the module
    is `main`; this file puts the parent on sys.path, so it is `nsg_ai.main`.
    A deferred import hardcoded to either name passes here and fails there —
    which is exactly what happened, and only on `scatter`, the one layout that
    calls back into the platform.
    """
    print("\n=== the generator runs under the server's import layout ===")
    code = (
        "import topology;"
        "cfg = topology.generate(topology.TopologyRequest("
        "layout='scatter', cells=7, isd_m=350, ues_per_cell=2, seed=42));"
        "print(len(cfg['base_stations']), 'WARNING' not in cfg['description'])"
    )
    import subprocess
    r = subprocess.run([sys.executable, "-c", code],
                       cwd="/home/chancelor/nsg_ai_platform/nsg_ai",
                       capture_output=True, text=True)
    detail = r.stdout.strip() or (r.stderr.strip().splitlines() or [""])[-1]
    check(r.returncode == 0 and r.stdout.strip() == "7 True",
          f"scatter generates from inside nsg_ai/ -> {detail}")


def run(layout: str) -> dict:
    cfg = topology.generate(topology.TopologyRequest(
        layout=layout, cells=5, isd_m=350, ues_per_cell=2,
        dl_mbps_per_ue=25, seed=42))
    p = nsoran_params(NSGSimulationConfig(**cfg))
    body = json.dumps({"sim_time": 20, **p}).encode()
    req = urllib.request.Request(ENGINE, body, {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=900).read())


def engine() -> None:
    print("\n=== engine: each generated scenario actually runs ===")
    got = {}
    for layout in LAYOUTS:
        d = run(layout)
        got[layout] = d
        thp = (d.get("measurements") or {}).get("DRB.UEThpDl", {})
        check(d.get("status") == "ok" and d.get("e2_agents", 0) >= 5
              and thp.get("mean", 0) > 0,
              f"{layout}: status={d.get('status')} agents={d.get('e2_agents')} "
              f"UEs={d.get('ue_count')} thp={thp.get('mean', 0)/1000:.1f} Mbps")

    # Buildings are the whole point of the Manhattan layout. If concrete does
    # not cost modulation, the generator is emitting scenery, not geometry.
    d_m = (got["manhattan"].get("derived") or {})
    d_h = (got["hex"].get("derived") or {})
    check(d_m.get("mod_64qam_pct", 100) < d_h.get("mod_64qam_pct", 0),
          f"manhattan NLOS costs modulation: 64QAM "
          f"{d_m.get('mod_64qam_pct')}% < hex {d_h.get('mod_64qam_pct')}%")


if __name__ == "__main__":
    geometry()
    clamping()
    server_import_layout()
    if "--engine" in sys.argv:
        engine()
    else:
        print("\n(skipping engine runs; pass --engine to include them)")
    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
    for f in fails:
        print("  -", f)
    sys.exit(1 if fails else 0)
