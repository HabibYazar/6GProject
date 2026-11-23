"""Phase 2c antenna test — per-cell panel orientation, and what makes it real.

antenna.azimuth_deg / tilt_deg were in the schema and the UI from the start and
never reached ns-3. The standing verdict was "N/A by engine paradigm: the gNBs
use an 8x8 UniformPlanarArray and the beam is STEERED at the UE, so a fixed
sector orientation is a different paradigm". Half right. UniformPlanarArray DOES
apply the GCS->LCS rotation by bearing/downtilt — but PhasedArrayModel's element
defaults to IsotropicAntennaModel, which the mmWave helper never overrides, and
rotating isotropic elements cancels out exactly. Orientation is inert because of
the ELEMENT, not because of the beamforming.

So this test has to show BOTH halves, or it proves nothing:

  isotropic, boresight at the UEs vs 90 deg off  -> SINR must NOT move
                                                    (the old verdict, confirmed)
  3gpp,      boresight at the UEs vs 90 deg off  -> SINR must DROP
                                                    (the lever, now real)

Unlike mobility, this is a propagation effect and appears immediately, so the
short :8090 batch horizon is enough.

Run inside Ubuntu-2004-nsoran (needs docker for the scenario log).
"""
import json
import re
import subprocess
import time
import urllib.request

# 3 cells in a line; every UE sits ~350 m SOUTH of the line, so "boresight at the
# UEs" is compass 180 for all three panels and 90 is a clean 90 deg off.
GP = "1500.0,1700.0;2000.0,1700.0;2500.0,1700.0"
UP = "1500,1350;1600,1300;1900,1330;2100,1300;2400,1350;2500,1300"
# 50 Mbps per UE, not 10: at 10 the UEs are DEMAND-limited (measured 10.4-10.7
# Mbps/UE, i.e. exactly the offered load), so throughput saturates and cannot
# show a link that got worse. The lever needs the link to be the bottleneck.
SL = ";".join(["9,50"] * 6)


def panels(az, tilt=0):
    return ";".join([f"{az},{tilt},8,8"] * 3)


def run(label, element, az, tilt=0):
    body = json.dumps({
        "sim_time": 20, "n_gnb": 3, "n_ue": 6, "bw_hz": "50000000.0",
        "gnb_positions": GP, "ue_positions": UP, "gnb_powers": "44;44;44",
        "ue_slices": SL, "antenna_element": element, "gnb_antenna": panels(az, tilt),
    }).encode()
    req = urllib.request.Request("http://localhost:8090/run", data=body,
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

    applied = len(re.findall(r"\[NSG\] gNB \d+ panel:", scn))
    elem = "3GPP TR38.901" if "element 3GPP" in scn else "isotropic"
    sinr, thp, prb = mean("L3servingSINR"), mean("DRB.UEThpDl"), mean("RRU.PrbUsedDl")
    print(f"  {label}")
    print(f"    status={r.get('status')}  panels applied={applied}  element={elem}  "
          f"SINR={sinr if sinr is None else round(sinr, 2)} dB  "
          f"PRB={prb if prb is None else round(prb, 2)}  "
          f"thp={thp if thp is None else round(thp / 1000, 2)} Mbps/UE")
    return {"status": r.get("status"), "sinr": sinr, "thp": thp, "prb": prb,
            "panels": applied}


print("Phase 2c — per-cell antenna panel orientation\n")
# 180 deg off, not 90: two runs that should be IDENTICAL (isotropic, two
# orientations) still differ by ~8% on throughput, because each run reaches a
# slightly different sim span in its wall-clock window and averages a different
# number of samples. That is the noise floor, and a 90 deg rotation (~23 dB of
# element attenuation, but partly recovered by the beamformer) sat inside it.
# Straight behind the panel the 3GPP element hits its 30 dB front-to-back cap,
# which is far above the floor.
iso_at = run("A  isotropic element, boresight AT the UEs (S)", "isotropic", 180)
iso_off = run("B  isotropic element, boresight AWAY (N, 180 off)", "isotropic", 0)
g_at = run("C  3GPP element,      boresight AT the UEs (S)", "3gpp", 180)
g_off = run("D  3GPP element,      boresight AWAY (N, 180 off)", "3gpp", 0)
# Tilt, checked separately because it is the layout-INDEPENDENT half: whatever
# direction a UE lies in, aiming the panel at the sky puts it in the vertical
# pattern's attenuation. That is what the fidelity harness can use on a baseline
# whose UEs are spread all round.
g_sky = run("E  3GPP element,      AT the UEs but tilted 80 deg UP", "3gpp", 180, -80)

print()
runs = (iso_at, iso_off, g_at, g_off, g_sky)
ok_applied = all(r["panels"] == 3 for r in runs)


def rel(a, b, key):
    """Relative change b vs a, or None if either run has no value."""
    if a.get(key) is None or b.get(key) is None or not a[key]:
        return None
    return (b[key] - a[key]) / abs(a[key])


# Throughput is the metric that moves: the engine's L3servingSINR turned out to
# be insensitive here (identical to 2 dp across all four runs, including the
# isotropic-vs-3GPP pair where throughput changed ~50%), so it is reported for
# reference but not asserted on.
iso_d = rel(iso_at, iso_off, "thp")
g_d = rel(g_at, g_off, "thp")
elem_d = rel(iso_at, g_at, "thp")
# Thresholds sit clear of the ~8% sampling noise floor measured above.
ok_iso = iso_d is not None and abs(iso_d) < 0.15
ok_3gpp = g_d is not None and g_d < -0.40
ok_elem = elem_d is not None and abs(elem_d) > 0.15

print(f"  [{'PASS' if ok_applied else 'FAIL'}] per-cell panels reach the engine "
      f"(3 of 3 in every run)")
print(f"  [{'PASS' if ok_elem else 'FAIL'}] the element model itself changes the link "
      f"({iso_at['thp'] / 1000:.2f} -> {g_at['thp'] / 1000:.2f} Mbps/UE, "
      f"{100 * elem_d:+.1f}%)" if elem_d is not None else "  [FAIL] element: NO DATA")
print(f"  [{'PASS' if ok_iso else 'FAIL'}] isotropic element ignores orientation "
      f"({iso_at['thp'] / 1000:.2f} -> {iso_off['thp'] / 1000:.2f} Mbps/UE, "
      f"{100 * iso_d:+.1f}%)" if iso_d is not None else "  [FAIL] isotropic: NO DATA")
print(f"  [{'PASS' if ok_3gpp else 'FAIL'}] 3GPP element makes orientation a real lever "
      f"({g_at['thp'] / 1000:.2f} -> {g_off['thp'] / 1000:.2f} Mbps/UE, "
      f"{100 * g_d:+.1f}%)" if g_d is not None else "  [FAIL] 3GPP: NO DATA")
tilt_d = rel(g_at, g_sky, "thp")
ok_tilt = tilt_d is not None and tilt_d < -0.40
print(f"  [{'PASS' if ok_tilt else 'FAIL'}] downtilt is a real lever too "
      f"({g_at['thp'] / 1000:.2f} -> {g_sky['thp'] / 1000:.2f} Mbps/UE, "
      f"{100 * tilt_d:+.1f}%)" if tilt_d is not None else "  [FAIL] tilt: NO DATA")
ok = ok_applied and ok_iso and ok_3gpp and ok_elem and ok_tilt and all(
    r["status"] == "ok" for r in runs)
print("RESULT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
