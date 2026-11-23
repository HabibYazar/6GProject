"""
Baseline "stand-in AI" xApps — for TESTING the RIC comparison feature before you
have a real trained model. Each is a simple fixed policy that produces a
distinct performance curve, so you can run several and see the comparison
table/chart rank them.

Usage (run each against the SAME session, saving each run in the UI between):
    pip install websockets
    python baseline_xapps.py <session_id> baseline     # do nothing (reference)
    python baseline_xapps.py <session_id> energy        # sleep idle cells
    python baseline_xapps.py <session_id> qos           # protect premium slices
    python baseline_xapps.py <session_id> aggressive    # sleep cells recklessly (should score WORST)

Expected ranking (best→worst): energy / qos  >  baseline  >  aggressive.
Ctrl-C each after ~15-20s, click "Save run" in the UI, then start the next.
"""
import asyncio
import json
import sys

import websockets

WS_URL = "ws://localhost:8000/ws/e2"


def decide(strategy: str, kpm: dict) -> list[dict]:
    cells = kpm["cells"]
    active = [c for c in cells if c["active"]]

    if strategy == "baseline":
        return []  # observe only — the reference score

    if strategy == "energy":
        # Sleep cells with no load (safe energy saving).
        return [{"action": "cell_power", "cell": c["id"], "state": "off"}
                for c in active if c["connected_ues"] == 0]

    if strategy == "qos":
        # Under congestion, throttle low-priority slices to protect eMBB/uRLLC.
        congested = any(c["prb_used_pct"] >= 95 for c in active)
        if not congested:
            return []
        return [{"action": "slice_quota", "slice": s, "prb_percent": 30}
                for s in ("mMTC", "voice")]

    if strategy == "aggressive":
        # Sleep the least-loaded active cell regardless of load — likely causes
        # congestion/outages elsewhere → the score should punish this.
        if len(active) <= 1:
            return []
        victim = min(active, key=lambda c: c["connected_ues"])
        return [{"action": "cell_power", "cell": victim["id"], "state": "off"}]

    return []


async def run(session_id: str, strategy: str) -> None:
    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({"join": session_id, "label": f"baseline:{strategy}"}))
        print(f"[{strategy}] joined {session_id} — Ctrl-C after ~20s, then 'Save run' in the UI")
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "error":
                print("error:", msg.get("message")); return
            if msg.get("type") == "kpm":
                for cmd in decide(strategy, msg):
                    await ws.send(json.dumps(cmd))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python baseline_xapps.py <session_id> baseline|energy|qos|aggressive")
        sys.exit(1)
    try:
        asyncio.run(run(sys.argv[1], sys.argv[2]))
    except KeyboardInterrupt:
        print("\nstopped")
