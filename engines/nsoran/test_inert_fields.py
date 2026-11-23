"""The fields the UI accepted and the engine ignored — now wired.

Each of these was in the schema and the config form from the start and was
hardcoded in the scenario, so setting it changed nothing. That is worse than a
missing field: the form claimed control it did not have. Audited and wired
2026-07-26.

  shadowing_enabled  -> was an unconditional ShadowingEnabled=false, while the
                        platform's own default for the field was TRUE
  scenario           -> was always ThreeGppUmiStreetCanyon, whichever of the
                        four environments the config asked for
  random_seed        -> the engine already honoured a rngRun GlobalValue; the
                        platform simply never sent it
  building type / floors / rooms -> hardcoded Office / 1 / 1 / 1
  numerology         -> never passed (and only 2-3 exist here, not 0-6)
  polarization       -> never passed (UniformPlanarArray::PolSlantAngle)

The seed check is the interesting one to design: run-level means cannot prove
reproducibility, because each run reaches a different sim span in its
wall-clock window and averages a different number of samples (~10% noise). So
it compares the FIRST 20 position samples of the random-walk UEs instead —
RNG-driven, wall-clock independent, and identical iff the seed took effect.

Run inside Ubuntu-2004-nsoran (needs docker for the scenario log).
"""
import hashlib
import json
import re
import subprocess
import time
import urllib.request

GP = "1500.0,1700.0;2000.0,1700.0;2500.0,1700.0"
UP = "1500,1350;1600,1300;1900,1330;2100,1300;2400,1350;2500,1300"
SL = ";".join(["9,50"] * 6)
# One building, deliberately not the old hardcoded Office/1/1/1:
# x0,x1,y0,y1,z0,z1,wall,type(0=residential),floors,roomsX,roomsY
BLD = "1700.0,1780.0,1450.0,1530.0,0.0,30.0,2,0,8,4,3"


def run(label, **over):
    body = {"sim_time": 20, "n_gnb": 3, "n_ue": 6, "bw_hz": "50000000.0",
            "gnb_positions": GP, "ue_positions": UP, "gnb_powers": "44;44;44",
            "ue_slices": SL}
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

    shadow = re.search(r"\[NSG\] shadowing (\w+)", scn)
    prop = re.search(r"\[NSG\] propagation (\w+) -> ns3::(\w+)", scn)
    # First 20 mobility samples, in trace order — deterministic per RNG run.
    pts = re.findall(r"IMSI \d+ is [\d.]+:[\d.]+:[\d.]+ at time [\d.]+", scn)[:20]
    trace = hashlib.sha1("|".join(pts).encode()).hexdigest()[:10] if pts else None
    thp = mean("DRB.UEThpDl")
    print(f"  {label}")
    print(f"    status={r.get('status')}  shadowing={shadow.group(1) if shadow else '?'}  "
          f"prop={prop.group(2) if prop else '?'}  trace={trace}  "
          f"thp={thp if thp is None else round(thp / 1000, 2)} Mbps/UE")
    return {"status": r.get("status"), "shadow": shadow.group(1) if shadow else None,
            "prop": prop.group(2) if prop else None, "trace": trace, "thp": thp}


print("Newly wired config fields — each was hardcoded in the engine\n")
base = run("A  UMi, shadowing off, seed 1, no buildings", prop_scenario="umi",
           shadowing="0", rng_run=1)
shad = run("B  same + shadowing ON", prop_scenario="umi", shadowing="1", rng_run=1)
rma = run("C  RMa propagation instead of UMi", prop_scenario="rma",
          shadowing="0", rng_run=1)
bld = run("D  a residential building, 8 floors, 4x3 rooms", prop_scenario="umi",
          shadowing="0", rng_run=1, buildings=BLD)
s1 = run("E  seed 4242", prop_scenario="umi", shadowing="0", rng_run=4242)
s1b = run("F  seed 4242 again", prop_scenario="umi", shadowing="0", rng_run=4242)
s2 = run("G  seed 7 ", prop_scenario="umi", shadowing="0", rng_run=7)

print()
runs = (base, shad, rma, bld, s1, s1b, s2)
ok_status = all(r["status"] == "ok" for r in runs)
ok_shadow = base["shadow"] == "off" and shad["shadow"] == "ON"
ok_prop = (base["prop"] == "ThreeGppUmiStreetCanyonPropagationLossModel"
           and rma["prop"] == "ThreeGppRmaPropagationLossModel")
ok_seed_same = s1["trace"] is not None and s1["trace"] == s1b["trace"]
ok_seed_diff = s1["trace"] is not None and s1["trace"] != s2["trace"]
ok_bld = bld["status"] == "ok" and bld["thp"]


def d(a, b):
    if None in (a["thp"], b["thp"]) or not a["thp"]:
        return None
    return (b["thp"] - a["thp"]) / a["thp"]


shad_d, rma_d = d(base, shad), d(base, rma)

print(f"  [{'PASS' if ok_status else 'FAIL'}] every variant runs healthy")
print(f"  [{'PASS' if ok_shadow else 'FAIL'}] shadowing_enabled reaches the engine "
      f"(off -> {base['shadow']}, on -> {shad['shadow']}); throughput "
      f"{100 * shad_d:+.1f}%" if shad_d is not None else "  [FAIL] shadowing: NO DATA")
print(f"  [{'PASS' if ok_prop else 'FAIL'}] scenario selects the propagation model "
      f"({base['prop']} -> {rma['prop']}); throughput "
      f"{100 * rma_d:+.1f}%" if rma_d is not None else "  [FAIL] propagation: NO DATA")
print(f"  [{'PASS' if ok_seed_same else 'FAIL'}] same seed reproduces the mobility trace "
      f"({s1['trace']} == {s1b['trace']})")
print(f"  [{'PASS' if ok_seed_diff else 'FAIL'}] a different seed changes it "
      f"({s1['trace']} != {s2['trace']})")
print(f"  [{'PASS' if ok_bld else 'FAIL'}] a building with a real type/floors/rooms "
      f"still runs (was hardcoded Office/1/1/1)")
ok = ok_status and ok_shadow and ok_prop and ok_seed_same and ok_seed_diff and ok_bld
print("RESULT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
