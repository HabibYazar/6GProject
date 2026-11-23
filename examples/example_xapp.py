"""
Example xApp — a minimal external AI controller for the NSG RIC test bed.

This is what a user plugs in: a standalone process that connects to the
platform's simplified E2 interface, reads live KPM reports, and issues RC
control commands based on its own policy. Swap the `decide()` function for your
real AI/ML model — everything else is just the E2 plumbing.

Usage:
  1. Start the backend:   uvicorn main:app          (from nsg_ai/)
  2. In the browser, open Analytics → RIC Test → "Connect E2".
     Copy the session id shown in the banner.
  3. Run this script:     python example_xapp.py <session_id>
     (optionally: python example_xapp.py <session_id> ws://host:8000/ws/e2)

The browser will show this xApp's control actions live in its control log,
tagged with this xApp's label — the closed loop, driven by an external AI.

Requires:  pip install websockets
"""
import asyncio
import json
import sys

import websockets

LABEL = "example-load-balancer"


def decide(kpm: dict) -> list[dict]:
    """
    The 'AI'. Replace with your model. This demo policy does two things:

      • Energy saving: if a cell carries no active UEs, switch it off.
      • Load balancing: if a cell is at 100% PRB (congested) while another
        co-exists, hand its heaviest UE group over to the least-loaded cell.

    Returns a list of control commands (possibly empty).
    """
    commands: list[dict] = []
    cells = {c["id"]: c for c in kpm["cells"] if c["active"]}
    if not cells:
        return commands

    # 1) Energy saving — turn off idle active cells (keep at least one on).
    idle = [c for c in cells.values() if c["connected_ues"] == 0]
    if idle and len(cells) - len(idle) >= 1:
        for c in idle:
            commands.append({"action": "cell_power", "cell": c["id"], "state": "off"})
        return commands  # apply one kind of action per tick, then observe

    # 2) Load balancing — relieve a fully-congested cell.
    congested = [c for c in cells.values() if c["prb_used_pct"] >= 99.0]
    if congested and len(cells) >= 2:
        hot = max(congested, key=lambda c: c["connected_ues"])
        # Least-loaded other cell = handover target.
        target = min((c for c in cells.values() if c["id"] != hot["id"]),
                     key=lambda c: c["prb_used_pct"], default=None)
        if target:
            # Move the UE group whose serving cell is the hot cell.
            for ue in kpm["ue_groups"]:
                if ue["serving_cell"] == hot["id"]:
                    commands.append({
                        "action": "handover",
                        "ue_group": ue["id"],
                        "target_cell": target["id"],
                    })
                    break
    return commands


async def run(session_id: str, url: str) -> None:
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"join": session_id, "label": LABEL}))
        print(f"[xApp] joined session {session_id} as '{LABEL}'")

        async for raw in ws:
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "error":
                print(f"[xApp] error: {msg.get('message')}")
                return

            if mtype == "kpm":
                for cmd in decide(msg):
                    await ws.send(json.dumps(cmd))
                    print(f"[xApp] → {cmd}")

            elif mtype == "control_ack":
                status = "ok" if msg.get("success") else "FAIL"
                print(f"[xApp] ack [{status}]: {msg.get('detail')}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python example_xapp.py <session_id> [ws_url]")
        sys.exit(1)
    session = sys.argv[1]
    ws_url = sys.argv[2] if len(sys.argv) > 2 else "ws://localhost:8000/ws/e2"
    try:
        asyncio.run(run(session, ws_url))
    except KeyboardInterrupt:
        print("\n[xApp] stopped")
