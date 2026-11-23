"""Channel realism (blockage, O2I penetration, channel ageing) and RLC.

Blockage is the point of this batch. At mmWave a hand or a passing body costs
tens of dB, and TR 38.901 models exactly that — and the engine ships it OFF, so
every run this project has ever measured has been optimistic about link
stability. It is now a config field.

Also covers the fix for a bug shipped two batches ago: settings.outage_threshold_db
was routed to --ns3::LteEnbRrc::OutageThreshold, but the scenario
Config::SetDefault's that attribute AFTER cmd.Parse from its own outageThreshold
GlobalValue, so the argument was silently overwritten and the field only LOOKED
wired. The earlier test passed it because "ns-3 accepted the argument" is not
the same claim as "the value took effect" — the exact limitation documented for
param_audit. It now rides the GlobalValue, and this test asserts the EFFECT.

Run inside Ubuntu-2004-nsoran (needs docker for the scenario log).
"""
import json
import re
import subprocess
import time
import urllib.request

GP = "1500.0,1700.0,25.0;2000.0,1700.0,25.0;2500.0,1700.0,25.0"
UP = ";".join(f"{x},{y},1.5" for x, y in
              [(1500, 1350), (1600, 1300), (1900, 1330),
               (2100, 1300), (2400, 1350), (2500, 1300)])
SL = ";".join(["9,50"] * 6)


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
    scn = subprocess.run(["docker", "exec", "nsg-engine", "cat", "/tmp/scn0.log"],
                         capture_output=True, text=True).stdout
    m = r.get("measurements", {})

    def mean(k):
        v = m.get(k, {}).get("mean")
        return float(v) if v is not None else None

    ch = re.search(r"\[NSG\] channel update period ([\d.]+) ms", scn)
    # The scenario echoes its own view of the outage threshold at startup, which
    # is how we prove the GlobalValue took effect rather than being overwritten.
    outage = re.search(r"OutageThreshold (-?[\d.]+)", scn)
    thp, prb = mean("DRB.UEThpDl"), mean("RRU.PrbUsedDl")
    print(f"  {label}")
    print(f"    status={r.get('status')}  chUpdate={ch.group(1) if ch else '?'} ms  "
          f"engine-outage={outage.group(1) if outage else '?'}  "
          f"thp={thp if thp is None else round(thp / 1000, 2)} Mbps/UE  "
          f"PRB={prb if prb is None else round(prb, 2)}")
    return {"status": r.get("status"), "thp": thp, "prb": prb,
            "ch": ch.group(1) if ch else None,
            "outage": outage.group(1) if outage else None}


print("Channel realism (blockage / O2I / ageing) + RLC\n")
base = run("A  defaults (blockage OFF, as the engine ships)")
blk = run("B  blockage ON, 8 blockers at 3 m/s", blockage="1", n_blockers="8",
          blocker_speed="3")
chu = run("C  channel regenerated every 10 ms (was 100)", channel_update_ms="10")
rlc = run("D  RLC AM + 40 MB buffer + slower timers", rlc_am="1", rlc_buffer_mb="40",
          rlc_reorder_ms="5", rlc_poll_ms="8", rlc_status_ms="3")
out = run("E  outage threshold -9 dB (the routing fix)", outage_db="-9")

print()
runs = (base, blk, chu, rlc, out)
ok_status = all(r["status"] == "ok" for r in runs)
bad = [i for i, r in enumerate(runs) if r["status"] != "ok"]


def rel(a, b, key="thp"):
    if None in (a[key], b[key]) or not a[key]:
        return None
    return (b[key] - a[key]) / a[key]


blk_d = rel(base, blk)
ok_blk = blk_d is not None and blk_d < -0.05
ok_ch = chu["ch"] == "10" and base["ch"] == "100"
ok_rlc = rlc["status"] == "ok" and rlc["thp"]
# The scenario prints the threshold it is actually using at startup.
ok_out = out["outage"] is not None and float(out["outage"]) == -9.0

print(f"  [{'PASS' if ok_status else 'FAIL'}] every variant runs healthy"
      + (f" - bad: {bad}" if bad else ""))
print(f"  [{'PASS' if ok_blk else 'FAIL'}] blockage costs throughput "
      f"({base['thp'] / 1000:.2f} -> {blk['thp'] / 1000:.2f} Mbps/UE, {100 * blk_d:+.1f}%) "
      f"- the impairment mmWave is defined by, and it ships OFF"
      if blk_d is not None else "  [FAIL] blockage: NO DATA")
print(f"  [{'PASS' if ok_ch else 'FAIL'}] the channel update period reaches the engine "
      f"({base['ch']} -> {chu['ch']} ms, was hardcoded 100)")
print(f"  [{'PASS' if ok_rlc else 'FAIL'}] RLC AM + buffer + timers still carry traffic "
      f"({rlc['thp'] / 1000:.2f} Mbps/UE)" if rlc["thp"] else "  [FAIL] RLC: NO DATA")
print(f"  [{'PASS' if ok_out else 'FAIL'}] outage_threshold_db now TAKES EFFECT "
      f"(engine reports {out['outage']} dB, asked for -9) - it was being silently "
      f"overwritten after cmd.Parse")
ok = ok_status and ok_blk and ok_ch and ok_rlc and ok_out
print("RESULT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
