"""Live E2E: RU sleep states on a running session.

Connects to the live bridge (8091) with a scenario whose last two cells sit far
from the UE cluster (so they carry nothing), asks those cells for deep sleep,
and verifies the bridge streams [NSG-SLEEP] state lines while KPM keeps
flowing — i.e. the sleep controller runs in a LIVE session, not just in batch.
"""
import json
import socket
import time

PARAMS = {"nsg_params": {
    "n_gnb": 4, "n_ue": 8, "bw_hz": "50000000.0",
    "gnb_positions": "1700.0,1900.0;1900.0,1900.0;2600.0,2600.0;1300.0,1300.0",
    "ue_positions": ("1750,1950;1800,1880;1850,1930;1900,1960;"
                     "1780,1900;1830,1850;1880,1990;1930,1880"),
    "gnb_powers": "44;44;44;44",
    "ue_slices": ";".join(["9,10"] * 8),
    # cells 1-2 always on, cells 3-4 may sleep deep after 200 ms idle
    "gnb_sleep": "0,200,10;0,200,10;3,200,10;3,200,10",
    "sleep_wake_load_pct": 60,
}}

s = socket.create_connection(("localhost", 8091), timeout=300)
s.sendall((json.dumps(PARAMS) + "\n").encode())
s.settimeout(300)

buf = b""
kpm = 0
sleep_lines = []
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
        txt = line.decode("utf-8", "replace").strip()
        if txt.startswith("[NSG-SLEEP]"):
            sleep_lines.append(txt)
        elif "DRB" in txt or "KPM ind_msg" in txt:
            kpm += 1
    # Wait for a cell to actually descend — the shutdown timer has to expire
    # first, so the mere presence of a sleep line proves nothing yet.
    if any("state=asleep" in l for l in sleep_lines) and kpm >= 10:
        break

s.close()
asleep = [l for l in sleep_lines if "state=asleep" in l]
print(f"  KPM lines: {kpm}   sleep lines: {len(sleep_lines)}   asleep: {len(asleep)}")
for l in sleep_lines[:4]:
    print("   ", l[:110])
for l in asleep[:2]:
    print("    ASLEEP:", l[:110])
ok = kpm > 0 and len(asleep) > 0
print("RESULT:", "PASS - RU sleep states run and stream in a LIVE session"
      if ok else "FAIL - no cell reported asleep on the live path")
raise SystemExit(0 if ok else 1)
