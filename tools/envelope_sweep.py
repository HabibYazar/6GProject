#!/usr/bin/env python3
"""Does the engine actually survive everything the ENVELOPE promises?

engine_acceptance.py takes one configuration and checks ten invariants on it —
depth. This is the breadth counterpart: it walks representative points INSIDE
engine_envelope.py and reports which ones the engine survives.

Why it exists: on 2026-09-01 a single day of probing turned up an e2sim buffer
overflow at ~10 UEs/cell, an unclamped SCTP length, a scheduler abort below
2 MHz per UE, and a null-pointer path behind a config flag. Every new corner we
looked into had something in it. Discovering that one crash at a time is slow and
leaves the envelope making promises nobody checked.

The points come FROM the envelope (clamp_ue, max_ue_per_cell_for_bw), so this
cannot drift away from what the platform actually allows. A failure here does not
mean "the engine is broken" — it means the envelope is lying and must be tightened.

Usage:  venv/bin/python tools/envelope_sweep.py [--quick] [--out results.json]
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "nsg_ai"))
from engine_envelope import (GNB_MIN, GNB_MAX, BW_MHZ_MIN, BW_MHZ_MAX,  # noqa: E402
                             UE_PER_CELL_MIN, clamp_ue, max_ue_per_cell_for_bw)
# The product's OWN config->engine translation. Imported rather than reimplemented:
# posting {n_gnb, n_ue, bw, freq} and letting the engine default everything else
# measures a bare topology, and a real config covers about four times less
# simulated time at the same cell and UE count (measured 2026-09-13, 3 cells /
# 50 MHz: bare 0.8 s at 30 UEs and 0.4 s at 60, a full config 0.2 s and 0.1 s).
# Every constant fitted to the bare numbers therefore described a run nobody makes.
from main import nsoran_params  # noqa: E402
from schema import NSGSimulationConfig  # noqa: E402

WSL = "/mnt/c/Windows/System32/wsl.exe"
DISTRO = "Ubuntu-2004-nsoran"
NS3DIR = "/home/builder/ns-O-RAN-flexric/mmwave-LENA-oran"
BIN = f"{NS3DIR}/build/scratch/ns3.42-scenario-zero-with_parallel_loging-default"
RIC = "/root/nsoran/flexric/build/examples/ric/nearRT-RIC"


def wsl(cmd: str, timeout: int = 120) -> str:
    """Run a command in the engine distro. Everything here is read-only or a
    scenario run; nothing mutates the platform."""
    r = subprocess.run([WSL, "-d", DISTRO, "-u", "root", "-e", "bash", "-lc", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout


def balanced_layout(n_gnb: int, n_ue: int) -> tuple[str, str]:
    """Cells on rings, UEs split evenly and pinned near their own cell — the same
    geometry main.py builds, because a clustered scatter tests the RSRP attach
    imbalance rather than the point of this sweep."""
    import math
    cx = cy = 2000.0
    gnb: list[tuple[float, float]] = []
    ring, idx, per = 0, 0, 1
    while len(gnb) < n_gnb:
        if idx >= per:
            ring, idx = ring + 1, 0
            per = max(1, 6 * ring)
        if ring == 0:
            gnb.append((cx, cy))
        else:
            a = 2 * math.pi * idx / per
            gnb.append((cx + ring * 600 * math.cos(a), cy + ring * 600 * math.sin(a)))
        idx += 1
    ue: list[tuple[float, float]] = []
    golden = math.pi * (3 - math.sqrt(5))
    for i in range(n_ue):
        gx, gy = gnb[i % n_gnb]
        k = i // n_gnb
        r = 30.0 + 45.0 * ((k % 5) / 4.0)
        a = k * golden
        ue.append((gx + r * math.cos(a), gy + r * math.sin(a)))
    fmt = lambda pts: ";".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    return fmt(gnb), fmt(ue)


def sweep_config(n_gnb: int, n_ue: int, bw_mhz: float) -> "NSGSimulationConfig":
    """A REPRESENTATIVE product config for this point, not a bare topology.

    Cells on a grid with a UE group beside each one — the geometry that separates
    "this many UEs is too many" from "these UEs were all parked on one cell
    boundary", which is a distinction this sweep kept confusing (measured
    2026-09-13: the same 20 UEs/cell was `partial` clustered and `ok` distributed).
    """
    import math
    side = max(1, math.ceil(math.sqrt(n_gnb)))
    step = 400.0
    sites = [(100.0 + (i % side) * step, 100.0 + (i // side) * step)
             for i in range(n_gnb)]
    span = 100.0 + side * step
    per = max(1, n_ue // n_gnb)
    extra = n_ue - per * n_gnb
    return NSGSimulationConfig(
        name=f"sweep-{n_gnb}c-{n_ue}u-{bw_mhz:.0f}mhz",
        description="envelope sweep point",
        area={"x_min": -200.0, "x_max": span, "y_min": -200.0, "y_max": span,
              "resolution_m": 25.0},
        base_stations=[
            {"id": f"gnb-{i}", "name": f"Site-{i}",
             "position": {"x": x, "y": y, "z": 25.0},
             "radio": {"band": {"label": "n78", "bandwidth_mhz": bw_mhz,
                                "frequency_mhz": 3500}}}
            for i, (x, y) in enumerate(sites)
        ],
        ue_groups=[
            {"id": f"g{i}", "name": f"G{i}",
             "count": per + (1 if i < extra else 0),
             "spawn_position": {"x": x + 40.0, "y": y + 40.0},
             "services": [{"slice_type": "eMBB", "qos_id": 9,
                           "target_throughput_gbps": 0.05}]}
            for i, (x, y) in enumerate(sites)
        ],
    )


# Metrics only the DU reports. If the xApp LOG carries them but the parsed result
# does not, nothing is wrong with the RAN — the loss is in the pipe between them.
# That is not hypothetical: `head -25` in run_scenario.sh cut the xApp output at
# 25 lines while the xApp prints a line per metric per UE, so past ~72 UEs the
# first DU metric fell outside the window and every run above that read as an
# engine ceiling. It survived a long time because nothing compared the two ends.
_DU_ONLY = ("RRU.PrbUsedDl", "L3servingSINR", "DRB.UEThpDl")


def du_in_xapp_log() -> int:
    """How many DU metric lines the xApp actually received, read in the container."""
    pat = "|".join(_DU_ONLY)
    out = wsl(f"docker exec nsg-engine bash -lc \"grep -acE '{pat}' /tmp/xapp.log\"",
              timeout=60)
    try:
        return int(out.strip().splitlines()[-1])
    except Exception:
        return -1


CRASH_PATTERNS = [
    ("scheduler-zero-pdu", "m_rlcPduInfo[i].m_size > 0"),
    ("null-ptr", 'msg="Attempted to dereference zero pointer"'),
    ("heap-abort", "malloc()"),
    ("heap-free", "free()"),
    ("segfault", "Segmentation fault"),
    ("ns-fatal", "NS_FATAL"),
    ("bad-arg", "Invalid command-line argument"),
]


def classify(log: str) -> tuple[str, str]:
    """Name the failure, or call it clean. Grouping by signature matters more
    than the count: five runs failing the same way is one bug.

    "Healthy" here is "simulated time advanced without a crash", NOT "KPM was
    produced" — this sweep runs the RIC and ns-3 but no xApp, and without a KPM
    subscription the `[NSG kpm]` lines never appear. A first attempt used them as
    the success signal and reported a perfectly good run as NO-KPM.
    """
    for name, pat in CRASH_PATTERNS:
        if pat in log:
            return "CRASH", name
    stamps = set(re.findall(r"at time ([0-9.]+)", log))
    if len(stamps) >= 2:
        return "OK", ""
    # A big scenario can be alive and simply too slow to cross 0.1 simulated
    # seconds inside the window: at 150 UEs the engine advances ~0.0006 sim-s per
    # wall second, so 0.1 s takes ~170 s. The first version of this called that
    # STALLED, which reads as a defect. Output volume tells them apart — a live
    # run keeps printing whether or not the clock has ticked over.
    if log.count("\n") > 200:
        return "SLOW", "alive but did not reach t=0.1 in the window"
    if stamps:
        return "STALLED", "reached t=0 and stopped producing output"
    return "NO-START", "never reached the simulation"


def run_point(n_gnb: int, n_ue: int, bw_mhz: float, secs: int) -> dict:
    gnb, ue = balanced_layout(n_gnb, n_ue)
    setup = (f"pkill -9 -x nearRT-RIC 2>/dev/null; sleep 1; "
             f"stdbuf -oL -eL {RIC} >/tmp/sw_ric.log 2>&1 & sleep 4; "
             f"printf '%s' '{gnb}' > /tmp/nsg_gnb_pos.txt; "
             f"printf '%s' '{ue}' > /tmp/nsg_ue_pos.txt; "
             f"chmod 644 /tmp/nsg_gnb_pos.txt /tmp/nsg_ue_pos.txt")
    args = (f"--e2TermIp=127.0.0.1 --e2nrEnabled=1 --e2du=true --simTime=100000 "
            f"--KPM_E2functionID=2 --RC_E2functionID=3 --N_MmWaveEnbNodes={n_gnb} "
            f"--N_Ues={n_ue} --CenterFrequency=3.5e9 --Bandwidth={bw_mhz:g}e6 "
            f"--IntersideDistanceCells=1000 --IntersideDistanceUEs=1000 "
            f"--gnbPositionsFile=/tmp/nsg_gnb_pos.txt --uePositionsFile=/tmp/nsg_ue_pos.txt "
            f"--harqEnabled=true --numHarqProcess=100 --propScenario=umi "
            f"--shadowingEnabled=false --nsgNumerology=2 --rngRun=1")
    cmd = (f"{setup}; runuser -u builder -- bash -lc "
           f"\"ulimit -c 0; cd {NS3DIR} && timeout -s KILL {secs} "
           f"env LD_LIBRARY_PATH={NS3DIR}/build/lib {BIN} {args}\" > /tmp/sw_scn.log 2>&1; "
           f"pkill -9 -x nearRT-RIC 2>/dev/null; cat /tmp/sw_scn.log")
    t0 = time.time()
    log = wsl(cmd, timeout=secs + 90)
    verdict, why = classify(log)
    return {"n_gnb": n_gnb, "n_ue": n_ue, "bw_mhz": bw_mhz, "verdict": verdict,
            "signature": why, "wall_s": round(time.time() - t0, 1),
            "log_lines": log.count("\n")}


def run_point_api(n_gnb: int, n_ue: int, bw_mhz: float, url: str, timeout: int) -> dict:
    """Same point, but through the PRODUCT path: POST /run on the engine service
    inside the container, which is what main.py drives.

    Worth testing separately from the bare binary: this exercises the deployed
    image, the xApp, and the wall-clock budget in run_scenario.sh, none of which
    the direct-binary sweep touches. A configuration can pass the binary and still
    fail here.
    """
    body = json.dumps({"n_gnb": n_gnb, "n_ue": n_ue,
                       "bw_hz": int(bw_mhz * 1e6), "freq_hz": 3.5e9}).encode()
    t0 = time.time()
    d = None
    # The engine serialises sessions behind one lock, and a finished run does not
    # release it instantly — leftover RIC/xApp children hold it for a few seconds.
    # Firing the next point immediately gets `status=busy`, which is the harness
    # talking, not the scenario. The first version of this mode did exactly that
    # and reported 7 of 18 points as failures. main.py retries for the same reason.
    for attempt in range(6):
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode())
        except Exception as ex:  # noqa: BLE001 - any failure is a result, not a crash
            return {"n_gnb": n_gnb, "n_ue": n_ue, "bw_mhz": bw_mhz, "verdict": "NO-START",
                    "signature": f"{type(ex).__name__}: {str(ex)[:60]}",
                    "wall_s": round(time.time() - t0, 1), "span_s": None}
        if d.get("status") != "busy":
            break
        time.sleep(20)
    d = d or {}
    span = ((d.get("energy") or {}).get("span_s"))
    status = d.get("status")
    meas = d.get("measurements") or {}
    du_parsed = any(k.startswith(m) for m in _DU_ONLY for k in meas)
    if status == "busy":
        verdict, sig = "BUSY", "engine never freed its lock — harness, not scenario"
    elif not du_parsed and du_in_xapp_log() > 0:
        # The RAN reported the DU metrics and the result does not carry them, so
        # the loss is between the xApp and the parser. Checked BEFORE the crash
        # branch because it wears the same clothes: a `partial` with only CU-UP
        # names reads exactly like an engine ceiling.
        verdict, sig = "PIPE-LOSS", ("xApp received DU metrics but the run parsed none "
                                     "— look at the xApp output filter, not the engine")
    elif status != "ok":
        verdict, sig = "CRASH", f"status={status}: {str(d.get('error'))[:60]}"
    elif not span:
        verdict, sig = "NO-KPM", "ok but the engine reported no horizon"
    else:
        verdict, sig = "OK", ""
    return {"n_gnb": n_gnb, "n_ue": n_ue, "bw_mhz": bw_mhz, "verdict": verdict,
            "signature": sig, "wall_s": round(time.time() - t0, 1), "span_s": span,
            "du_parsed": du_parsed, "names": len(meas)}


def points(quick: bool) -> list[tuple[int, int, float]]:
    """Representative points inside the envelope, derived from the envelope.

    Tested at the CEILING the envelope allows for each (cells, bandwidth), since
    that is where it makes its strongest promise, plus a half-load control so a
    failure can be told apart from "this size never works".
    """
    cells = [GNB_MIN, GNB_MAX] if quick else [GNB_MIN, 7, GNB_MAX]
    bws = [BW_MHZ_MIN, 20.0] if quick else [BW_MHZ_MIN, 20.0, BW_MHZ_MAX]
    out = []
    for g in cells:
        for bw in bws:
            top = clamp_ue(g * max_ue_per_cell_for_bw(bw), g, bw)
            out.append((g, top, bw))
            half = max(g * UE_PER_CELL_MIN, top // 2)
            if half != top:
                out.append((g, half, bw))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="fewer points (~4 runs)")
    ap.add_argument("--secs", type=int, default=90, help="wall seconds per point")
    ap.add_argument("--via-api", default="", metavar="URL",
                    help="sweep through the product path instead of the bare "
                         "binary, e.g. http://localhost:8090/run")
    # Defaults into the REPO, not a temp dir. The first run of this sweep wrote
    # its results to a session scratchpad and they were gone the next day, so the
    # findings survived only because they had already been folded into the
    # envelope. Results are the point of the tool; keep them.
    ap.add_argument("--out", default=str(pathlib.Path(__file__).with_name("envelope_sweep.latest.json")),
                    help="write JSON results here (default: alongside this tool)")
    a = ap.parse_args()

    pts = points(a.quick)
    print(f"sweeping {len(pts)} envelope points, {a.secs}s each "
          f"(~{len(pts) * (a.secs + 20) // 60} min)\n")
    if a.via_api:
        print(f"via product path: {a.via_api}\n")
    print(f"{'cells':>5} {'UEs':>5} {'MHz':>5} {'UE/cell':>8}  {'verdict':<8} "
          f"{'span_s':>7}  signature")
    print("-" * 74)

    results = []
    for g, u, bw in pts:
        r = (run_point_api(g, u, bw, a.via_api, a.secs + 300) if a.via_api
             else run_point(g, u, bw, a.secs))
        results.append(r)
        mark = "" if r["verdict"] == "OK" else "  <<<"
        print(f"{g:>5} {u:>5} {bw:>5.0f} {u / g:>8.1f}  {r['verdict']:<8} "
              f"{str(r.get('span_s', '')):>7}  {r['signature']}{mark}")
        if a.out:
            pathlib.Path(a.out).write_text(json.dumps(results, indent=2))

    # SLOW is a speed fact, not an envelope failure — do not count it as one.
    bad = [r for r in results if r["verdict"] not in ("OK", "SLOW", "BUSY")]
    print("\n" + "=" * 66)
    if not bad:
        print(f"ALL {len(results)} ENVELOPE POINTS RAN CLEAN — the envelope's promise holds.")
        return 0
    sigs: dict[str, int] = {}
    for r in bad:
        sigs[r["signature"]] = sigs.get(r["signature"], 0) + 1
    print(f"{len(bad)} of {len(results)} POINTS FAILED — the envelope permits configurations "
          f"the engine does not survive.")
    for sig, n in sorted(sigs.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>2} x  {sig}")
    print("\nEach distinct signature is one bug. Fix it, or tighten the envelope "
          "so the platform stops promising it.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
