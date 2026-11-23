"""Phase 2c radio test — scheduler / AMC / HARQ actually reach the engine.

These are plain ns-3 attributes (MmWaveHelper::Scheduler, MmWaveAmc::AmcModel,
MmWaveHelper::HarqEnabled, MmWavePhyMacCommon::NumHarqProcess) and only needed
plumbing — except HARQ, which the scenario Config::SetDefault'd AFTER cmd.Parse
and so silently overwrote anything the command line set. That is now a
GlobalValue the scenario reads back.

It also pins the SCHEDULER finding. The fork ships three alternatives to
MmWaveFlexTtiMacScheduler, and measured here they are dead code: Pf and MaxRate
run clean — E2 connects, the log has no error, the run reports "ok" — and
allocate NOTHING (0 PRB, 0 throughput); MaxWeight aborts ns-3. So the platform
schema deliberately has no scheduler field, and this test asserts the broken
state rather than health. If a fork update ever fixes them, this test fails and
tells you the field can be exposed.

Note the harness's own run-to-run noise floor is ~8-10% (each run reaches a
slightly different sim span and averages a different number of samples — see
antenna-2c.patch), so only effects well past it are asserted.

Run inside Ubuntu-2004-nsoran (needs docker for the scenario log).
"""
import json
import re
import subprocess
import time
import urllib.request

GP = "1500.0,1700.0;2000.0,1700.0;2500.0,1700.0"
UP = "1500,1350;1600,1300;1900,1330;2100,1300;2400,1350;2500,1300"
# 50 Mbps/UE so the runs are LINK-limited: at the old 10 the UEs sat at the
# offered load and no radio lever could show anything.
SL = ";".join(["9,50"] * 6)


def run(label, **over):
    body = {
        "sim_time": 20, "n_gnb": 3, "n_ue": 6, "bw_hz": "50000000.0",
        "gnb_positions": GP, "ue_positions": UP, "gnb_powers": "44;44;44",
        "ue_slices": SL,
    }
    body.update(over)
    req = urllib.request.Request("http://localhost:8090/run",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    for _ in range(8):
        r = json.load(urllib.request.urlopen(req, timeout=400))
        if r.get("status") != "busy":
            break
        time.sleep(10)
    scn = subprocess.run(["docker", "exec", "nsg-engine", "cat", "/tmp/scn0.log"],
                         capture_output=True, text=True).stdout
    m = r.get("measurements", {})

    def mean(k):
        v = m.get(k, {}).get("mean")
        return float(v) if v is not None else None

    harq = re.search(r"\[NSG\] HARQ (\w+), (\d+) processes", scn)
    thp, prb = mean("DRB.UEThpDl"), mean("RRU.PrbUsedDl")
    print(f"  {label}")
    print(f"    status={r.get('status')}  engine-reports HARQ="
          f"{harq.group(1) + '/' + harq.group(2) if harq else '?'}  "
          f"PRB={prb if prb is None else round(prb, 2)}  "
          f"thp={thp if thp is None else round(thp / 1000, 2)} Mbps/UE")
    return {"status": r.get("status"), "thp": thp, "prb": prb,
            "harq": harq.group(1) if harq else None,
            "harq_n": int(harq.group(2)) if harq else None}


print("Phase 2c radio — scheduler / AMC / HARQ\n")
base = run("A  defaults (flex-TTI, ErrorModel AMC, HARQ on/100)")
pf = run("B  scheduler = proportional fair",
         scheduler="ns3::MmWaveFlexTtiPfMacScheduler")
mr = run("C  scheduler = max rate",
         scheduler="ns3::MmWaveFlexTtiMaxRateMacScheduler")
mw = run("D  scheduler = max weight",
         scheduler="ns3::MmWaveFlexTtiMaxWeightMacScheduler")
sh = run("E  AMC = ShannonModel", amc_model="ShannonModel")
nh = run("F  HARQ off", harq_enabled="0")
h8 = run("G  HARQ on, 8 processes", harq_processes="8")

print()
shipped = {"defaults": base, "shannon": sh, "harq-off": nh, "harq-8": h8}
ok_status = all(r["status"] == "ok" and r["thp"] for r in shipped.values())
bad = [k for k, r in shipped.items() if r["status"] != "ok" or not r["thp"]]
ok_harq_flag = nh["harq"] == "off" and base["harq"] == "on"
ok_harq_n = h8["harq_n"] == 8 and base["harq_n"] == 100


def d(a, b):
    if None in (a["thp"], b["thp"]) or not a["thp"]:
        return None
    return (b["thp"] - a["thp"]) / a["thp"]


harq_d, amc_d = d(base, nh), d(base, sh)
ok_harq_effect = harq_d is not None and abs(harq_d) > 0.15
ok_amc_effect = amc_d is not None and abs(amc_d) > 0.15
# The shipped levers must work; the three unshipped schedulers must stay broken
# exactly as measured, so a fork update that repairs them is visible here.
ok_sched_dead = (not pf["thp"]) and (not mr["thp"]) and mw["status"] != "ok"

print(f"  [{'PASS' if ok_status else 'FAIL'}] every SHIPPED variant carries traffic"
      + (f" - bad: {bad}" if bad else ""))
print(f"  [{'PASS' if ok_harq_flag else 'FAIL'}] HARQ enable reaches the engine "
      f"(default {base['harq']}, off-variant {nh['harq']})")
print(f"  [{'PASS' if ok_harq_n else 'FAIL'}] HARQ process count reaches the engine "
      f"(default {base['harq_n']}, 8-variant {h8['harq_n']}) "
      f"- was hardcoded 100 and overwrote the command line")
print(f"  [{'PASS' if ok_harq_effect else 'FAIL'}] HARQ off moves throughput past the "
      f"~10% noise floor ({base['thp'] / 1000:.2f} -> {nh['thp'] / 1000:.2f} Mbps/UE, "
      f"{100 * harq_d:+.1f}%)" if harq_d is not None else "  [FAIL] HARQ effect: NO DATA")
print(f"  [{'PASS' if ok_amc_effect else 'FAIL'}] Shannon AMC moves throughput "
      f"({base['thp'] / 1000:.2f} -> {sh['thp'] / 1000:.2f} Mbps/UE, {100 * amc_d:+.1f}%)"
      if amc_d is not None else "  [FAIL] AMC effect: NO DATA")
print(f"  [{'PASS' if ok_sched_dead else 'FAIL'}] the 3 alternative schedulers are still "
      f"dead upstream, so the schema is right not to expose them "
      f"(pf thp={pf['thp']}, maxrate thp={mr['thp']}, maxweight status={mw['status']})")
ok = (ok_status and ok_harq_flag and ok_harq_n and ok_harq_effect
      and ok_amc_effect and ok_sched_dead)
print("RESULT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
