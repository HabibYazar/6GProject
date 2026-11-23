"""
Is the 7-cell ceiling a real engine limit, or a measurement artefact?

The rulebook records it as a hard fork limit: "gNB 3-7; >7 (9+ E2 nodes): PRB &
throughput read 0 — not fixed by simTime/topology/load". But "reads 0" is a
MEASUREMENT symptom, and that verdict was reached before we understood how the
KPM frame is assembled — the same misunderstanding that made cell_off look
broken for a whole session.

This asks the engine directly instead of trusting the platform's view. It talks
to the live bridge on :8091, bypassing the backend's clamp, and reports what the
PLATFORM sees. The engine's own answer is in the container's /tmp/scn0.log —
the `[NSG kpm] t=... cell N reports M UEs` roster, which is written before any
of the KPM assembly this probe exercises. Comparing the two separates:

  engine produces data, platform shows zero   -> measurement bug, fixable
  engine itself produces zero                 -> real limit, rulebook stands

Usage:  python probe_gnb_ceiling.py [n_gnb] [n_ue]
"""
from __future__ import annotations

import json
import pathlib
import socket
import sys
import time

# The bridge on :8091 streams RAW xApp stdout, not JSON — the frames are built by
# the platform's parser. An earlier version of this probe waited for JSON on the
# socket and reported "PLATFORM SAW NO CELLS" after 20 minutes; that was the
# probe's bug, not a finding. Run the real parser over the real stream instead,
# which also sidesteps the backend's 3-7 clamp.
# nsoran_live imports its siblings flat (the backend runs with cwd=nsg_ai), so
# that directory goes on the path, not the repo root.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "nsg_ai"))
from nsoran_live import _KpmAccumulator  # noqa: E402

HOST, PORT = "127.0.0.1", 8091
FRAMES = 120
BOOT_TIMEOUT = 300.0


def balanced_layout(n_gnb: int, n_ue: int) -> tuple[str, str]:
    """Cells spread on rings, UEs split evenly and pinned close to their own cell.

    This mirrors `_balance_ues` in main.py. Without it the engine's own scatter
    lets a middle cell win far more UEs than an even split predicts — and the
    mmWave stack corrupts its heap somewhere around a dozen UEs on one cell
    (`UE_PER_CELL_MAX` in engine_envelope.py documents ~7 safe / ~12 abort). So
    a ceiling test WITHOUT this geometry measures the clustering bug, not the
    ceiling: exactly the mistake that made 16 gNB look like a cell-count limit
    when its log showed `cell 2 reports 11 UEs`.
    """
    import math
    cx = cy = 2000.0
    gnb: list[tuple[float, float]] = []
    ring, idx, per = 0, 0, 1
    while len(gnb) < n_gnb:
        if idx >= per:
            ring, idx = ring + 1, 0
            per = max(1, 6 * ring)
        if ring == 0:
            gnb.append((cx, cy))
        else:
            r = 600.0 * ring
            a = 2 * math.pi * idx / per
            gnb.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        idx += 1

    alloc = [n_ue // n_gnb + (1 if i < n_ue % n_gnb else 0) for i in range(n_gnb)]
    ue: list[tuple[float, float]] = []
    for c, (gx, gy) in enumerate(gnb):
        k = alloc[c]
        for j in range(k):
            r = 30.0 + 45.0 * math.sqrt((j + 0.5) / max(1, k))
            ang = j * 2.399963229728653 + c
            ue.append((gx + r * math.cos(ang), gy + r * math.sin(ang)))

    fmt = lambda pts: ";".join(f"{x:.1f},{y:.1f}" for x, y in pts)  # noqa: E731
    return fmt(gnb), fmt(ue)


def main() -> int:
    n_gnb = int(sys.argv[1]) if len(sys.argv) > 1 else 9
    n_ue = int(sys.argv[2]) if len(sys.argv) > 2 else 18
    balanced = "--balanced" in sys.argv
    print(f"asking the bridge for n_gnb={n_gnb} n_ue={n_ue} "
          f"(backend clamp is 3-7, bypassed on purpose)"
          + ("  [BALANCED geometry, as the backend would send]" if balanced else
             "  [engine default scatter]"))

    params: dict = {"n_gnb": n_gnb, "n_ue": n_ue}
    if balanced:
        g, u = balanced_layout(n_gnb, n_ue)
        params["gnb_positions"], params["ue_positions"] = g, u
        print(f"  even split: {n_ue // n_gnb}-{n_ue // n_gnb + 1} UEs per cell")

    s = socket.create_connection((HOST, PORT), timeout=BOOT_TIMEOUT)
    s.settimeout(BOOT_TIMEOUT)
    s.sendall((json.dumps({"nsg_params": params}) + "\n").encode())

    buf = b""
    frames = 0
    t0 = time.monotonic()
    seen_cells: dict[str, dict] = {}
    kpm_lines = 0
    acc = _KpmAccumulator()

    try:
        while frames < FRAMES:
            chunk = s.recv(65536)
            if not chunk:
                print("bridge closed the connection")
                break
            buf += chunk
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                txt = line.decode("utf-8", "replace").rstrip()
                if "KPM ind_msg" in txt:
                    kpm_lines += 1
                msg = acc.feed(txt)
                if msg is None or msg.get("type") != "kpm":
                    continue
                frames += 1
                for c in msg.get("cells") or []:
                    cid = str(c.get("cell"))
                    d = seen_cells.setdefault(
                        cid, {"ues": 0, "prb": 0.0, "thp": 0.0, "seen": 0})
                    d["seen"] += 1
                    d["ues"] = max(d["ues"], c.get("ues") or 0)
                    d["prb"] = max(d["prb"], c.get("prb") or 0.0)
                    d["thp"] = max(d["thp"], c.get("thp_kbps") or 0.0)
                if frames % 5 == 0:
                    # mmwave_cells is what gates the POS serving cell. If it is
                    # empty the platform reports every UE as on-LTE and `cells`
                    # comes out empty however healthy the engine is — so print it
                    # rather than infer it.
                    on_lte = sum(1 for u in (msg.get("ues") or []) if u.get("on_lte"))
                    print(f"  frame {frames:>3}  {time.monotonic()-t0:>6.1f}s  "
                          f"cells={len(msg.get('cells') or [])} "
                          f"ues={len(msg.get('ues') or [])} "
                          f"on_lte={on_lte} "
                          f"mmwave_cells={sorted(acc.mmwave_cells)} "
                          f"energy={len(acc.energy)}")
    finally:
        s.close()

    print(f"\n{frames} KPM frames in {time.monotonic()-t0:.0f}s "
          f"({kpm_lines} ind_msg markers)")
    if not seen_cells:
        print("\nPLATFORM SAW NO CELLS AT ALL")
        return 1

    print(f"\nPLATFORM view — peak per cell over the run")
    print(f"  {'cell':>5} {'frames':>7} {'UEs':>5} {'PRB':>9} {'Mbps':>9}")
    live = 0
    for cid in sorted(seen_cells, key=lambda x: int(x) if x.isdigit() else 0):
        d = seen_cells[cid]
        ok = d["prb"] > 0 or d["thp"] > 0
        live += ok
        print(f"  {cid:>5} {d['seen']:>7} {d['ues']:>5} {d['prb']:>9.1f} "
              f"{d['thp']/1000:>9.2f}  {'' if ok else '<- all zero'}")

    print(f"\n  cells carrying anything: {live} of {len(seen_cells)}")
    print("\nNow compare with the ENGINE's own roster, which is written before\n"
          "any of this assembly:\n"
          "  docker exec nsg-engine grep -a 'NSG kpm' /tmp/scn0.log | tail -20\n"
          "If the engine lists UEs on cells the platform shows as zero, the\n"
          "ceiling is a measurement bug, not a fork limit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
