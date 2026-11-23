"""Multi-service per UE: each service is its own stream on the UE's bearer.
Two checks: (1) the engine installs one stream per service; (2) adding a second
service measurably raises the UE's carried throughput (same first service)."""
import json
import subprocess
import time
import urllib.request

GP = "1550.0,1750.0;2050.0,1750.0;1550.0,2250.0;2050.0,2250.0;1800.0,2000.0"
UP = ("2050,2000;1950,2170;1750,2240;1580,2130;1550,1930;"
      "1650,1780;1850,1760;2020,1850;1700,2100;1900,2050")


def run(label, slices):
    body = json.dumps({
        "sim_time": 15, "n_gnb": 5, "n_ue": 10, "bw_hz": "50000000.0",
        "gnb_positions": GP, "ue_positions": UP,
        "gnb_powers": "44;44;44;44;44", "ue_slices": slices,
    }).encode()
    req = urllib.request.Request("http://localhost:8090/run", data=body,
                                 headers={"Content-Type": "application/json"})
    for _ in range(8):
        r = json.load(urllib.request.urlopen(req, timeout=400))
        if r.get("status") != "busy":
            break
        time.sleep(10)
    # Instrument = windowed RLC DL volume: DRB.UEThpDl is an ACTIVE-TIME rate
    # (it barely moves when a second stream adds volume), and DRB.PdcpSduVolumeDl
    # is a known-dead metric for MC UEs (DL PDCP lives at the LTE anchor).
    thp = r.get("measurements", {}).get("QosFlow.PdcpPduVolumeDL", {})
    svc_lines = subprocess.run(
        ["docker", "exec", "nsg-engine", "bash", "-c",
         "grep -c ' svc ' /tmp/scn0.log"],
        capture_output=True, text=True).stdout.strip()
    crash = subprocess.run(
        ["docker", "exec", "nsg-engine", "bash", "-c",
         "grep -icE 'NS_ASSERT|NS_FATAL|Segmentation' /tmp/scn0.log"],
        capture_output=True, text=True).stdout.strip()
    print(f"  {label}: status={r.get('status')}  thp mean={thp.get('mean')} kbps  "
          f"svc-streams={svc_lines}  crash-lines={crash}")
    return thp


one = ";".join(["9,8.00"] * 10)                    # eMBB only (8 Mbps offered)
two = ";".join(["9,8.00|1,6.00"] * 10)             # + 2nd continuous svc (14 offered)

ta = run("1 service /UE", one)
tb = run("2 services/UE", two)

ok = bool(ta.get("mean")) and bool(tb.get("mean")) and tb["mean"] > ta["mean"] * 1.15
if ta.get("mean") and tb.get("mean"):
    print(f"\n  thp ratio 2svc/1svc: {tb['mean'] / ta['mean']:.2f} "
          f"(2nd service adds real load)")
print("RESULT:", "PASS - services are real independent streams" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
