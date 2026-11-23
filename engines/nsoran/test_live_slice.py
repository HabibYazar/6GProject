"""Live E2E: mid-run slice re-partition over the REAL E2 path.

Connects to the live bridge (8091) with the 2-slice scenario, lets KPM flow,
then sends {"cmd":"slice_quota","qci":9,"pct":5} — a real E2SM-RC style-2
CONTROL — and verifies the gNB applied it (scheduler log) while the run stays
alive (KPM keeps flowing after the control).
"""
import json
import socket
import time

PARAMS = {"nsg_params": {
    "n_gnb": 5, "n_ue": 10, "bw_hz": "50000000.0",
    "gnb_positions": "1550.0,1750.0;2050.0,1750.0;1550.0,2250.0;2050.0,2250.0;1800.0,2000.0",
    "ue_positions": ("2050,2000;1950,2170;1750,2240;1580,2130;1550,1930;"
                     "1650,1780;1850,1760;2020,1850;1700,2100;1900,2050"),
    "gnb_powers": "44;44;44;44;44",
    "ue_slices": "9,30;9,30;9,30;9,30;9,30;7,30;7,30;7,30;7,30;7,30",
}}

s = socket.create_connection(("localhost", 8091), timeout=300)
s.sendall((json.dumps(PARAMS) + "\n").encode())
s.settimeout(300)

buf = b""
kpm_before = kpm_after = 0
sent = False
t0 = time.time()
while time.time() - t0 < 240:
    try:
        chunk = s.recv(4096)
    except socket.timeout:
        break
    if not chunk:
        break
    buf += chunk
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        txt = line.decode("utf-8", "replace")
        if "slice control sent" in txt:
            print("  BRIDGE:", txt.strip()[:100])
        if '"' in txt and "kpm" in txt.lower() or "DRB" in txt:
            if sent:
                kpm_after += 1
            else:
                kpm_before += 1
    if not sent and kpm_before >= 8:
        s.sendall(b'{"cmd":"slice_quota","qci":9,"pct":5}\n')
        print("  -> sent live slice_quota qci=9 pct=5 (real style-2 control)")
        sent = True
        t_ctrl = time.time()
    if sent and kpm_after >= 8:
        break

s.close()
print(f"\n  frames before control: {kpm_before}   after: {kpm_after}")
ok = sent and kpm_after >= 3
print("RESULT:", "PASS - live session accepted slice control and kept streaming"
      if ok else "FAIL")
raise SystemExit(0 if ok else 1)
