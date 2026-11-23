"""Transport network (backhaul), building propagation detail, PHY timing and
the SVD solver cost.

The backhaul is the interesting one. X2 carries handover signalling between
gNBs, so its delay is part of how long a handover actually takes — and the
engine's 1 ms default is a co-located ideal. Every handover measured in Phase 2b
crossed a perfect backhaul. S1-U delay adds to every packet's end-to-end
latency, and its data rate is where a backhaul-limited site shows up: the
bottleneck moves off the radio and onto the transport network.

Building propagation detail (internal wall loss, the three shadow sigmas) pairs
with the rooms_x/rooms_y grid wired earlier — that grid is what decides how many
walls a path crosses, so the two only mean something together.

Run inside Ubuntu-2004-nsoran (needs docker for the scenario log).
"""
import json
import subprocess
import time
import urllib.request

GP = "1500.0,1700.0,25.0;2000.0,1700.0,25.0;2500.0,1700.0,25.0"
UE_XY = [(1500, 1350), (1600, 1300), (1900, 1330),
         (2100, 1300), (2400, 1350), (2500, 1300)]
UP = ";".join(f"{x},{y},1.5" for x, y in UE_XY)
SL = ";".join(["9,50"] * 6)
# A building between the middle cell and its UEs, with a real room grid so the
# internal-wall loss has walls to count.
BLD = "1900.0,2050.0,1450.0,1600.0,0.0,30.0,2,1,4,4,4"


def run(label, **over):
    body = {"sim_time": 20, "n_gnb": 3, "n_ue": 6, "bw_hz": "50000000.0",
            "gnb_positions": GP, "ue_positions": UP, "gnb_powers": "44;44;44",
            "ue_slices": SL, "shadowing": "1", "rng_run": "11"}
    body.update(over)
    req = urllib.request.Request("http://localhost:8090/run",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    for _ in range(8):
        r = json.load(urllib.request.urlopen(req, timeout=400))
        if r.get("status") != "busy":
            break
        time.sleep(10)
    m = r.get("measurements", {})

    def mean(k):
        v = m.get(k, {}).get("mean")
        return float(v) if v is not None else None

    thp, prb = mean("DRB.UEThpDl"), mean("RRU.PrbUsedDl")
    delay = mean("DRB.PdcpSduDelayDl")
    print(f"  {label}")
    print(f"    status={r.get('status')}  "
          f"thp={thp if thp is None else round(thp / 1000, 2)} Mbps/UE  "
          f"PRB={prb if prb is None else round(prb, 2)}  "
          f"PDCP delay={delay if delay is None else round(delay, 1)}")
    return {"status": r.get("status"), "thp": thp, "prb": prb, "delay": delay}


print("Transport / O2I / PHY timing / SVD solver\n")
base = run("A  defaults (ideal backhaul)")
# 50 Mbps, not 1 Gbps. The radio delivers ~14.5 Mbps/UE x 6 = ~87 Mbps here, so
# a 1 Gbps backhaul is ten times more than the cell can fill and cannot bind —
# the first attempt measured +5.5% and looked like the lever was backwards.
# A backhaul only limits when it is BELOW what the radio can carry.
slowbh = run("B  S1-U 50 Mbps + 15 ms (genuinely backhaul-limited)",
             s1u_gbps="0.05", s1u_delay_ms="15")
x2 = run("C  X2 delay 20 ms (non-co-located gNBs)", x2_delay_ms="20")
o2i = run("D  90% of UEs indoors (O2I)", o2i_threshold="0.9", o2i_low_loss="0.2")
timing = run("E  TB decode 250 us, HARQ DL timeout 40",
             tb_decode_us="250", harq_dl_timeout="40")
svd = run("F  SVD solver: 5 iterations, no cache", svd_iters="5", svd_cache="0")
# Not shipped, asserted so an upstream fix surfaces.
ulsd = run("G  UL grant delay 3 slots (not shipped - crashes)", ul_sched_delay="3")

print()
shipped = (base, slowbh, x2, o2i, timing, svd)
ok_status = all(r["status"] == "ok" for r in shipped)
bad = [i for i, r in enumerate(shipped) if r["status"] != "ok"]


def rel(a, b, key="thp"):
    if None in (a[key], b[key]) or not a[key]:
        return None
    return (b[key] - a[key]) / a[key]


bh_d, o2i_d = rel(base, slowbh), rel(base, o2i)
# The backhaul is asserted on PRB, not throughput. When the transport cannot
# feed the cell, the radio stops being ASKED for resources: PRB collapses (41.3
# -> 7.5 measured) and PDCP delay with it, while throughput moves only ~8%
# because the generator was offering more than either link could carry anyway.
# Same lesson as the UE noise figure in phy-mac.patch — measure the column the
# effect actually lands in.
bh_prb = rel(base, slowbh, "prb")
ok_bh = bh_prb is not None and bh_prb < -0.30
ok_o2i = o2i_d is not None and o2i_d < -0.05
ok_ulsd_broken = ulsd["status"] != "ok"

print(f"  [{'PASS' if ok_status else 'FAIL'}] every SHIPPED variant runs healthy"
      + (f" - bad: {bad}" if bad else "")
      + "  (a rejected attribute aborts ns-3 before it starts)")
print(f"  [{'PASS' if ok_bh else 'FAIL'}] a backhaul-limited site binds "
      f"(PRB {base['prb']:.1f} -> {slowbh['prb']:.1f}, {100 * bh_prb:+.1f}%; throughput "
      f"only {100 * bh_d:+.1f}%) - the radio stops being asked for resources it "
      f"cannot be fed for" if bh_prb is not None else "  [FAIL] backhaul: NO DATA")
print(f"  [{'PASS' if ok_o2i else 'FAIL'}] putting the UEs indoors costs throughput "
      f"({base['thp'] / 1000:.2f} -> {o2i['thp'] / 1000:.2f} Mbps/UE, {100 * o2i_d:+.1f}%)"
      if o2i_d is not None else "  [FAIL] O2I: NO DATA")
print(f"  [{'PASS' if ok_ulsd_broken else 'FAIL'}] UlSchedDelay>1 is still broken "
      f"upstream (status={ulsd['status']}), so the schema is right not to expose it")
print(f"  [INFO] X2 delay 20 ms: {x2['thp'] / 1000:.2f} Mbps/UE — X2 carries handover "
      f"signalling, so its cost shows in handover latency, not in steady-state "
      f"throughput on a 0.8 s batch" if x2["thp"] else "")
ok = ok_status and ok_bh and ok_o2i and ok_ulsd_broken
print("RESULT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
