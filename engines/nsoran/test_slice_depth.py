#!/usr/bin/env python3
"""Roadmap item 2 — slice and traffic depth.

Four things the platform could not express before, each checked for the same
property: does it reach ns-3 and CHANGE something there?

  1. the full standardised 5QI catalogue (21 classes, not the LTE 1-9 subset),
  2. slice min / dedicated ratios, not just a ceiling,
  3. the call process's distribution family, not just its mean,
  4. TCP as well as UDP per service.

Run:  python3 test_slice_depth.py            # translation only, seconds
      python3 test_slice_depth.py --engine   # + real engine runs, ~20 min
"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, "/home/chancelor/nsg_ai_platform")
sys.path.insert(0, "/home/chancelor/nsg_ai_platform/nsg_ai")

from nsg_ai.main import nsoran_params                       # noqa: E402
from nsg_ai.schema import NSGSimulationConfig, QOS_5QI_CATALOGUE  # noqa: E402

ENGINE = "http://localhost:8090/run"
fails: list[str] = []


def check(ok: bool, msg: str) -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {msg}", flush=True)
    if not ok:
        fails.append(msg)
    return ok


def cfg(*, slices=None, svc_a=None, svc_b=None) -> dict:
    """Two loaded cells' worth of UEs, one group per slice. Both groups
    saturate, because a slice policy that is never contended proves nothing."""
    return {
        "name": "slice depth",
        "base_stations": [
            {"id": f"c{i}", "name": f"C{i}",
             "position": {"x": x, "y": 0.0, "z": 25.0},
             "radio": {"band": {"label": "b", "frequency_mhz": 3500,
                                "bandwidth_mhz": 50, "numerology": 2},
                       "tx_power_dbm": 44.0}}
            for i, x in enumerate([-350.0, 0.0, 350.0], start=1)],
        "ue_groups": [
            {"id": "ua", "name": "slice A", "count": 4,
             "spawn_position": {"x": -200.0, "y": 0.0},
             "services": [svc_a or {"slice_type": "uRLLC", "qos_id": 82,
                                    "target_throughput_gbps": 0.04}]},
            {"id": "ub", "name": "slice B", "count": 4,
             "spawn_position": {"x": 200.0, "y": 0.0},
             "services": [svc_b or {"slice_type": "eMBB", "qos_id": 9,
                                    "target_throughput_gbps": 0.04}]}],
        "slices": slices or [],
        "area": {"x_min": -900.0, "x_max": 900.0, "y_min": -900.0,
                 "y_max": 900.0, "resolution_m": 40.0},
    }


def translation() -> None:
    print("\n=== translation: the four levers reach the engine's arguments ===")

    # 1. The catalogue. 82 is Discrete Automation — a 5G-only class that the
    # engine used to rewrite to 9 on the mmWave leg.
    p = nsoran_params(NSGSimulationConfig(**cfg()))
    check(len(QOS_5QI_CATALOGUE) == 21,
          f"catalogue carries {len(QOS_5QI_CATALOGUE)} standardised 5QIs (was 9)")
    check(p["ue_slices"].split(";")[0].startswith("82,"),
          f"5QI 82 survives translation: {p['ue_slices'].split(';')[0]}")

    # An unknown 5QI must be REFUSED, not quietly rewritten. The engine falls
    # back to 9 (its QoS table ends in NS_FATAL_ERROR, so it has to), and that
    # fallback is precisely how a mission-critical service could run as
    # best-effort video while the config still claimed otherwise.
    try:
        NSGSimulationConfig(**cfg(svc_a={"slice_type": "eMBB", "qos_id": 250,
                                         "target_throughput_gbps": 0.01}))
        check(False, "unknown 5QI 250 is rejected")
    except Exception:
        check(True, "unknown 5QI 250 is rejected instead of silently becoming 9")

    # 2. Slice policy: three ratios, and it must survive as a config field —
    # before this it existed only on the live E2 control path.
    p = nsoran_params(NSGSimulationConfig(**cfg(
        slices=[{"qos_id": 82, "max_pct": 60, "min_pct": 30, "dedicated_pct": 10},
                {"qos_id": 9, "max_pct": 80}])))
    check(p["slice_quotas"] == "82:60.0:30.0:10.0;9:80.0:0.0:0.0",
          f"slice policy -> {p['slice_quotas']}")

    # A floor above a ceiling is a contradiction, not a preference.
    try:
        NSGSimulationConfig(**cfg(slices=[{"qos_id": 9, "max_pct": 20,
                                           "min_pct": 50}]))
        check(False, "min above max is rejected")
    except Exception:
        check(True, "min above max is rejected at validation")

    # 3 + 4. Distribution family and protocol. Both are str-Enums, and str()
    # on those yields 'CallDistribution.PARETO' — which silently collapsed to
    # the default until this was measured rather than assumed.
    p = nsoran_params(NSGSimulationConfig(**cfg(
        svc_a={"slice_type": "uRLLC", "qos_id": 82,
               "target_throughput_gbps": 0.04, "call_distribution": "pareto",
               "avg_call_duration_s": 0.3, "avg_idle_duration_s": 0.2},
        svc_b={"slice_type": "eMBB", "qos_id": 9,
               "target_throughput_gbps": 0.04, "protocol": "tcp"})))
    a, b = p["ue_slices"].split(";")[0], p["ue_slices"].split(";")[-1]
    check(a.endswith(",3,0"), f"pareto call model reaches the engine: {a}")
    check(b.endswith(",0,1"), f"tcp reaches the engine: {b}")


def run(c: dict, sim_time: int = 20) -> dict:
    """One engine run that is actually a run.

    The engine serialises batch runs behind a lock and answers `status: busy`
    when it cannot get it, and that lock outlives the previous HTTP response by
    a moment — so back-to-back runs hit it. A busy reply carries no
    measurements, which reads as zero throughput.

    That is not theoretical: this suite's first green result compared a real
    run against a busy one and called the collapse to 0.0 Mbps proof that
    slice policy works. Comparisons only mean something between two runs that
    ran, so a non-ok status raises here instead of becoming a number.
    """
    p = nsoran_params(NSGSimulationConfig(**c))
    body = json.dumps({"sim_time": sim_time, **p}).encode()
    for _ in range(6):
        req = urllib.request.Request(ENGINE, body,
                                     {"Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=900).read())
        if d.get("status") == "ok":
            return d
        if d.get("status") != "busy":
            raise RuntimeError(f"engine returned {d.get('status')}: {d.get('error')}")
        time.sleep(20)
    raise RuntimeError("engine stayed busy across every retry")


def thp(d: dict) -> float:
    """Total DL throughput across the reporting UEs, in kbps.

    The SUM, not the per-UE mean. A slice policy redistributes capacity, and
    the mean over reporting UEs rises when the allocation gets lopsided even as
    the network carries less — measured: holding half a cell out of the pool
    read as a 14% throughput INCREASE on the mean. The mean answers "how did a
    typical UE do", which is a different question from "what did the network
    carry", and only the second one can judge a reservation.
    """
    m = (d.get("measurements") or {}).get("DRB.UEThpDl", {})
    total = float(m.get("mean", 0.0)) * int(m.get("count", 0))
    if not total:
        # A run that produced no throughput at all is a broken run, not a
        # measurement of zero — refuse to let it become one side of a ratio.
        raise RuntimeError("run reported no DL throughput; refusing to compare")
    return total


def guarded(name, fn) -> None:
    """A run that could not be made is a FAILED check, not a crashed suite —
    and never a silent zero."""
    try:
        fn()
    except Exception as exc:
        check(False, f"{name}: {exc}")


def engine() -> None:
    print("\n=== engine: each lever moves the real run ===")
    for name, fn in (("5QI catalogue", _eng_catalogue),
                     ("slice policy", _eng_policy),
                     ("call distribution", _eng_calls),
                     ("protocol", _eng_protocol)):
        guarded(name, fn)


def _eng_catalogue() -> None:
    """A 5G-only 5QI must run — not abort, and not be quietly rewritten.

    82 is Discrete Automation. Before the guard was widened it reached the
    mmWave leg as 9: the scenario said industrial automation and the engine
    scheduled buffered video.
    """
    d = run(cfg())
    check(thp(d) > 0,
          f"5QI 82 (discrete automation) runs: thp={thp(d) / 1000:.1f} Mbps, "
          f"agents={d.get('e2_agents')}")


def _eng_policy() -> None:
    """min vs dedicated, told apart by what they cost an IDLE slice.

    A ceiling and a floor both just look like "the numbers moved", which
    proves almost nothing. The sharp test is a policy on a 5QI that NO UE
    carries:

      * min 50%        work-conserving. Reserved only up to what the slice
                       asks for, and it asks for nothing — the network must be
                       unaffected.
      * dedicated 50%  not work-conserving. Held whether anyone wants it or
                       not, so half the symbols leave the pool and throughput
                       must drop.

    Same number, opposite consequence. If the scheduler treats the two as
    synonyms, exactly one of these two checks fails.
    """
    free = thp(run(cfg()))

    idle_min = thp(run(cfg(slices=[{"qos_id": 1, "min_pct": 50}])))
    check(abs(idle_min - free) / free < 0.10,
          f"min on an idle slice is free: {free / 1000:.1f} -> "
          f"{idle_min / 1000:.1f} Mbps ({(idle_min - free) / free * 100:+.1f}%)")

    idle_ded = thp(run(cfg(slices=[{"qos_id": 1, "dedicated_pct": 50}])))
    check((free - idle_ded) / free > 0.05,
          f"dedicated on an idle slice costs capacity: {free / 1000:.1f} -> "
          f"{idle_ded / 1000:.1f} Mbps ({(idle_ded - free) / free * 100:+.1f}%)")

    # And under real contention a hard ceiling on one of the two loaded slices
    # has to bite somewhere.
    capped = thp(run(cfg(slices=[{"qos_id": 9, "max_pct": 10}])))
    check((free - capped) / free > 0.05,
          f"a 10% ceiling on a loaded slice bites: {free / 1000:.1f} -> "
          f"{capped / 1000:.1f} Mbps")


def _eng_calls() -> None:
    """Distribution family at a fixed mean: same offered load, different shape."""
    def call(dist):
        svc = {"target_throughput_gbps": 0.04, "call_distribution": dist,
               "avg_call_duration_s": 0.3, "avg_idle_duration_s": 0.2}
        return cfg(svc_a={"slice_type": "uRLLC", "qos_id": 82, **svc},
                   svc_b={"slice_type": "eMBB", "qos_id": 9, **svc})

    got = {}
    for dist in ("constant", "exponential", "pareto"):
        try:
            got[dist] = thp(run(call(dist)))
        except Exception as exc:
            # Name the distribution that broke. A bare "a run reported no
            # throughput" leaves you re-running all three to find out which.
            check(False, f"call distribution '{dist}': {exc}")
    check(len(got) == 3 and all(v > 0 for v in got.values()),
          "every call distribution carries traffic: " +
          ", ".join(f"{k}={v / 1000:.1f}" for k, v in got.items()) + " Mbps")
    if len(got) < 3:
        return
    # Deliberately NOT asserting an ordering between them. At this sim horizon
    # the spread sits inside run-to-run noise, and inventing a threshold that
    # happens to hold once is how a lever gets called real before it is.
    check(max(got.values()) / min(got.values()) < 4.0,
          f"and none of them collapses: spread "
          f"{max(got.values()) / min(got.values()):.2f}x")


def _eng_protocol() -> None:
    """TCP is a closed loop, and the place that shows is the QUEUE.

    Not throughput: with both services offering far more than the radio can
    carry, the radio is the bottleneck either way and the two land on the same
    aggregate — measured 89.5 vs 86.1 Mbps, a 3.8% gap sitting inside the ~5%
    run-to-run spread. Asserting a difference there would have meant lowering
    the threshold until the answer came out right.

    The real signature of congestion control is that it stops sending before
    the buffer fills. So the prediction is a LOWER PDCP delay under TCP, and
    that is what is checked.
    """
    udp_run = run(cfg())
    tcp_run = run(cfg(svc_a={"slice_type": "uRLLC", "qos_id": 82,
                             "target_throughput_gbps": 0.04, "protocol": "tcp"},
                      svc_b={"slice_type": "eMBB", "qos_id": 9,
                             "target_throughput_gbps": 0.04, "protocol": "tcp"}))
    check(thp(tcp_run) > 0,
          f"tcp services carry traffic: {thp(tcp_run) / 1000:.1f} Mbps "
          f"(udp {thp(udp_run) / 1000:.1f})")

    def delay(d):
        return (d.get("measurements") or {}).get("DRB.PdcpSduDelayDl", {}).get("mean", 0.0)

    du, dt = delay(udp_run), delay(tcp_run)
    if du <= 0 or dt <= 0:
        check(False, f"PDCP delay not reported (udp={du}, tcp={dt}) — cannot "
                     f"judge the protocol lever on this run")
    else:
        check(dt < du,
              f"tcp keeps the queue shorter than udp: delay {du:.0f} -> {dt:.0f} "
              f"({(dt - du) / du * 100:+.1f}%)")

    # Two TCP services on ONE UE. The DL sink is per-node-and-port, so
    # installing it per service instead of per UE aborts the run at t=0 with
    # "Failed to bind socket" — an ordinary config, and it would never have
    # shown up in the single-service cases above.
    two = cfg()
    two["ue_groups"][0]["services"] = [
        {"slice_type": "uRLLC", "qos_id": 82, "protocol": "tcp",
         "target_throughput_gbps": 0.02},
        {"slice_type": "eMBB", "qos_id": 82, "protocol": "tcp",
         "target_throughput_gbps": 0.02}]
    both = thp(run(two))
    check(both > 0,
          f"a UE carrying two TCP services still runs: {both / 1000:.1f} Mbps")


if __name__ == "__main__":
    translation()
    if "--engine" in sys.argv:
        engine()
    else:
        print("\n(skipping engine runs; pass --engine to include them)")
    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
    for f in fails:
        print("  -", f)
    sys.exit(1 if fails else 0)
