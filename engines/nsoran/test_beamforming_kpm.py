"""Beamforming model, control-plane realism, and which KPM families are collected.

Two of these are honesty knobs rather than performance ones:

  beamforming_model  the engine defaults to SVD, which solves for the BEST
                     POSSIBLE beam from perfect channel knowledge. That is an
                     upper bound, not a product. DFT steers from a grid and
                     codebook is restricted to a finite set, which is what real
                     hardware does.
  use_ideal_rrc      was hardcoded true, so every attach and handover completed
                     instantly and the control plane cost nothing. Turning it
                     off makes RRC signalling pay its delays.

The KPM toggles decide the MEASUREMENT surface, so switching DU off must remove
the DU metrics — that is the assertion, and it doubles as a check that the four
toggles are not silently ignored.

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

    rrc = re.search(r"\[NSG\] ideal RRC (\w+)", scn)
    thp, prb = mean("DRB.UEThpDl"), mean("RRU.PrbUsedDl")
    fams = [k for k in ("DRB.UEThpDl", "RRU.PrbUsedDl", "L3servingSINR")
            if k in m]
    print(f"  {label}")
    print(f"    status={r.get('status')}  idealRRC={rrc.group(1) if rrc else '?'}  "
          f"thp={thp if thp is None else round(thp / 1000, 2)} Mbps/UE  "
          f"PRB={prb if prb is None else round(prb, 2)}  DU-metrics={len(fams)}/3")
    return {"status": r.get("status"), "thp": thp, "prb": prb,
            "rrc": rrc.group(1) if rrc else None, "du_metrics": len(fams),
            "keys": set(m.keys())}


print("Beamforming / control plane / KPM families\n")
base = run("A  defaults (SVD beamforming, ideal RRC)")
dft = run("B  DFT beamforming (grid-steered, not an upper bound)",
          beamforming="ns3::MmWaveDftBeamforming")
realrrc = run("C  ideal RRC OFF (signalling pays its delays)", ideal_rrc="0")
nodu = run("D  DU KPM reporting OFF", kpm_du="0")
red = run("E  reduced PM values", kpm_reduced="1")

print()
shipped = (base, dft, realrrc)
ok_status = all(r["status"] == "ok" for r in shipped)
bad = [i for i, r in enumerate(shipped) if r["status"] != "ok"]
ok_rrc = base["rrc"] == "on" and realrrc["rrc"] == "OFF"
# Switching a KPM family off must remove those metrics: that is what the toggle
# is FOR, and it is why the run is not expected to report "ok" here.
ok_du_off = nodu["du_metrics"] < base["du_metrics"]


def rel(a, b, key="thp"):
    if None in (a[key], b[key]) or not a[key]:
        return None
    return (b[key] - a[key]) / a[key]


dft_d = rel(base, dft)
# SVD is an optimum; a grid-steered beam should not beat it by much, and any
# measurable move proves the model swap reached the engine.
ok_dft = dft_d is not None and abs(dft_d) > 0.02

print(f"  [{'PASS' if ok_status else 'FAIL'}] every shipped variant runs healthy"
      + (f" - bad: {bad}" if bad else ""))
print(f"  [{'PASS' if ok_dft else 'FAIL'}] the beamforming model reaches the engine "
      f"(SVD {base['thp'] / 1000:.2f} -> DFT {dft['thp'] / 1000:.2f} Mbps/UE, "
      f"{100 * dft_d:+.1f}%) — SVD is an upper bound, DFT is grid-steered"
      if dft_d is not None else "  [FAIL] beamforming: NO DATA")
print(f"  [{'PASS' if ok_rrc else 'FAIL'}] ideal RRC is now a switch "
      f"(engine reports {base['rrc']} -> {realrrc['rrc']}, was hardcoded on)")
print(f"  [{'PASS' if ok_du_off else 'FAIL'}] switching DU KPM off removes the DU "
      f"metrics ({base['du_metrics']}/3 -> {nodu['du_metrics']}/3) — the toggle "
      f"changes the measurement surface, so status={nodu['status']} is expected")
# Upstream defect, asserted so a fix surfaces: the reduced PM container fails to
# ASN.1-encode the indication and aborts the run, which is why there is no
# kpm_reduced_values schema field. The run scripts always passed 0, so this path
# was never exercised upstream.
ok_red_broken = red["status"] != "ok"
print(f"  [{'PASS' if ok_red_broken else 'FAIL'}] reduced PM values still fail to "
      f"encode upstream (status={red['status']}), so the schema is right not to "
      f"expose them")
ok = ok_status and ok_dft and ok_rrc and ok_du_off and ok_red_broken
print("RESULT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
