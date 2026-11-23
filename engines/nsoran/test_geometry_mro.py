"""Node heights, antenna spacing, cell selection, the DynamicTtt curve and the
RU power profile — the next batch of parameters wired to ns-3.

Heights are the notable one: NsgParsePositions hardcoded z=3 for every node, so
base_stations[].position.z and the UE height were both inert. A 32 m macro mast
and a 60 m drone were the same height to the channel model, which silently
flattens every elevation-dependent effect (downtilt included).

The DynamicTtt curve matters because DynamicTtt is the DEFAULT handover mode and
only the FixedTtt value was previously reachable — the mode the engine actually
runs had none of its four parameters exposed.

Assertions are picked for what each parameter can honestly show in a short run:
heights appear directly in the scenario's position trace; the LteEnbRrc
attributes are pass-through, and ns-3 aborts on a rejected attribute value, so a
healthy run IS the check that the value was accepted; the RU idle current is
behavioural and must move mean power.

Run inside Ubuntu-2004-nsoran (needs docker for the scenario log).
"""
import json
import re
import subprocess
import time
import urllib.request

GP = "1500.0,1700.0,32.0;2000.0,1700.0,18.0;2500.0,1700.0,25.0"
UE_XY = [(1500, 1350), (1600, 1300), (1900, 1330),
         (2100, 1300), (2400, 1350), (2500, 1300)]
SL = ";".join(["9,50"] * 6)


def ue_pos(height):
    return ";".join(f"{x},{y},{height}" for x, y in UE_XY)


def run(label, **over):
    body = {"sim_time": 20, "n_gnb": 3, "n_ue": 6, "bw_hz": "50000000.0",
            "gnb_positions": GP, "ue_positions": ue_pos(1.5),
            "gnb_powers": "44;44;44", "ue_slices": SL, "shadowing": "1"}
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

    # The trace prints x:y:z per UE — the z is the height that used to be 3.
    zs = {float(z) for z in re.findall(
        r"Position of UE with IMSI \d+ is [\d.]+:[\d.]+:([\d.]+)", scn)}
    thp = mean("DRB.UEThpDl")
    energy = r.get("energy") or {}
    watts = energy.get("mean_power_w")
    print(f"  {label}")
    print(f"    status={r.get('status')}  UE heights seen={sorted(zs) or '-'}  "
          f"thp={thp if thp is None else round(thp / 1000, 2)} Mbps/UE  "
          f"W={watts if watts is None else round(watts, 1)}")
    return {"status": r.get("status"), "zs": zs, "thp": thp, "watts": watts}


print("Node heights / antenna spacing / cell selection / TTT curve / RU power\n")
base = run("A  UE height 1.5 m (default profile)")
high = run("B  UE height 60 m (drone fleet)", ue_positions=ue_pos(60.0))
spac = run("C  antenna element spacing 0.9 wavelengths",
           gnb_antenna=";".join(["0,3,8,8,0,0.9,0.9"] * 3), antenna_element="3gpp")
# NOTE the unit: ns-3 takes QRxLevMin in the 3GPP TS 36.133 SIGNALLED unit
# (2 dB steps, -70..-22 = -140..-44 dBm), and this test talks to the engine API
# directly, so it passes -60 (= -120 dBm). The platform does that conversion for
# you; passing dBm straight through is rejected by the attribute checker and
# aborts ns-3 before it starts, which surfaces as an engine "no_data".
mro = run("D  outage -12 dB, QRxLevMin -60 (=-120 dBm), DynamicTtt 40-200 ms",
          outage_db="-12", qrxlevmin="-60", ttt_min_ms="40", ttt_max_ms="200",
          ttt_diff_min_db="4", ttt_diff_max_db="25")
# 400 A against a ~61 A default. Doubling it only moved network power ~9%,
# because on a loaded network the cells spend most of their time transmitting
# and the idle term is a minority share — a big multiplier is what makes this
# assertable without a purpose-built idle-cell geometry.
ru = run("E  RU idle current 400 A (vs the model's ~61 A)", ru_idle_a="400")

print()
runs = (base, high, spac, mro, ru)
ok_status = all(r["status"] == "ok" for r in runs)
bad = [i for i, r in enumerate(runs) if r["status"] != "ok"]
ok_h_default = base["zs"] == {1.5}
ok_h_drone = high["zs"] == {60.0}
ok_spacing = spac["status"] == "ok" and spac["thp"]
ok_mro = mro["status"] == "ok" and mro["thp"]
ru_d = ((ru["watts"] - base["watts"]) / base["watts"]
        if None not in (ru["watts"], base["watts"]) and base["watts"] else None)
ok_ru = ru_d is not None and ru_d > 0.20

print(f"  [{'PASS' if ok_status else 'FAIL'}] every variant runs healthy"
      + (f" - bad: {bad}" if bad else ""))
print(f"  [{'PASS' if ok_h_default else 'FAIL'}] the configured UE height reaches ns-3 "
      f"({sorted(base['zs'])} m, was hardcoded 3.0 for every node)")
print(f"  [{'PASS' if ok_h_drone else 'FAIL'}] a 60 m drone fleet really flies at 60 m "
      f"({sorted(high['zs'])} m)")
print(f"  [{'PASS' if ok_spacing else 'FAIL'}] per-cell element spacing is accepted "
      f"(ns-3 aborts on a rejected attribute)")
print(f"  [{'PASS' if ok_mro else 'FAIL'}] outage / QRxLevMin / DynamicTtt curve are accepted")
print(f"  [{'PASS' if ok_ru else 'FAIL'}] the RU idle current moves measured power "
      f"({base['watts']:.0f} -> {ru['watts']:.0f} W, {100 * ru_d:+.1f}%)"
      if ru_d is not None else "  [FAIL] RU power: NO DATA")
ok = ok_status and ok_h_default and ok_h_drone and ok_spacing and ok_mro and ok_ru
print("RESULT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
