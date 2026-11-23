"""
The downloadable xApp connector template.

Served by GET /ric/xapp-connector with __WS_URL__ and __SESSION_ID__ filled in,
so the user gets a turnkey file pre-wired to their session. Their model runs on
THEIR machine and connects over WebSocket — nothing of theirs runs on our
servers, and our servers only relay tiny JSON control messages (no inference
load).
"""

XAPP_CONNECTOR_TEMPLATE = r'''"""
NSG RIC Test — xApp Connector

Plug YOUR AI/model into the NSG RIC test bed. Your model runs HERE, on your
machine (your framework, your GPU) and connects to the RIC test over a
WebSocket, driving control in a closed loop. Nothing of yours runs on the NSG
servers — they only relay your control actions.

QUICK START
  1. pip install websockets
  2. Put your model in decide() below (load your trained model, map KPM -> actions).
  3. python nsg_xapp.py
  4. Watch your actions appear live in the RIC Test console, tagged "my-xapp".

This file is pre-filled with your session id and server URL from the UI.
"""
import asyncio
import json

import websockets

# ── Pre-filled from the RIC Test UI ─────────────────────────────────────────
WS_URL     = "__WS_URL__"
# Only set when the platform requires a login. It is a bearer token for the
# account that downloaded this file and it EXPIRES — when it does, download the
# connector again or paste a fresh token here.
API_TOKEN  = "__API_TOKEN__"
SESSION_ID = "__SESSION_ID__"
LABEL      = "my-xapp"


# ── YOUR AI GOES HERE ───────────────────────────────────────────────────────
# Load your trained model ONCE at module scope, e.g.:
#     import onnxruntime, numpy as np
#     _sess = onnxruntime.InferenceSession("my_policy.onnx")
# or a PyTorch / TF / stable-baselines model — anything, it runs on your machine.
#
# KPM report you receive each step (the network state):
#   kpm["cells"]:     [{id, name, active, faulted, tx_power_dbm, power_w,
#                       prb_used_pct, throughput_mbps, connected_ues}]
#   kpm["ue_groups"]: [{id, name, serving_cell, rsrp_dbm, sinr_db,
#                       throughput_mbps, count}]
#   kpm["slices"]:    [{slice, throughput_mbps, prb_pct, qos_score}]
#   kpm["anomalies"]: {faulted_cells: [...], demand_multiplier: <float>}
#
# The operator can inject incidents you must respond to: a FAULTED cell goes
# offline (faulted=True) and you can't power it back on — route UEs around it;
# a traffic surge raises demand_multiplier (congestion — rebalance / cap slices).
#
# Return a list of control commands (zero or more), any of:
#   {"action": "handover",    "ue_group": "<id>", "target_cell": "<cell id>"}
#   {"action": "cell_power",  "cell": "<id>", "state": "off" | "on"}
#   {"action": "slice_quota", "slice": "eMBB"|"uRLLC"|"mMTC"|"voice", "prb_percent": <0-100>}
#   {"action": "reset"}
def decide(kpm: dict) -> list[dict]:
    # >>> Replace this stub with your model's inference. <<<
    # Demo policy: switch off idle cells (energy saving).
    return [
        {"action": "cell_power", "cell": c["id"], "state": "off"}
        for c in kpm["cells"] if c["active"] and c["connected_ues"] == 0
    ]


# ── Connection plumbing — you shouldn't need to edit below ───────────────────
async def _run() -> None:
    _auth = {"Authorization": f"Bearer {API_TOKEN}"} if API_TOKEN else None
    async with websockets.connect(WS_URL, additional_headers=_auth) as ws:
        await ws.send(json.dumps({"join": SESSION_ID, "label": LABEL}))
        print(f"[xApp] joined session {SESSION_ID} as '{LABEL}'")
        async for raw in ws:
            msg = json.loads(raw)
            mtype = msg.get("type")
            if mtype == "error":
                print("[xApp] error:", msg.get("message"))
                return
            if mtype == "kpm":
                for cmd in decide(msg):
                    await ws.send(json.dumps(cmd))
                    print("[xApp] ->", cmd)
            elif mtype == "control_ack":
                tag = "ok" if msg.get("success") else "FAIL"
                print(f"[xApp] ack [{tag}]: {msg.get('detail')}")


if __name__ == "__main__":
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\n[xApp] stopped")
'''


# Real-engine variant: connects to /ws/e2-real (a LIVE ns-3 mmWave + FlexRIC
# session). No session id — connecting starts the session. KPM is the real
# E2SM-KPM measurement set; every command becomes real E2SM-RC on the running RAN.
XAPP_CONNECTOR_REAL_TEMPLATE = r'''"""
NSG RIC Test — xApp Connector (REAL ns-O-RAN engine)

Plug YOUR trained xApp into the REAL engine. Your model runs HERE (your
framework, your GPU) and connects over a WebSocket to a LIVE ns-3 mmWave RAN +
FlexRIC session — your control actions become real E2SM-RC over E2AP on the
running RAN. Nothing of yours runs on the NSG servers.

QUICK START
  1. pip install websockets
  2. Put your model in decide() below (map live KPM -> a control command).
  3. python nsg_xapp_real.py
  4. Watch it land: RIC Test -> Real engine shows your control echoes + live KPM.

No session id needed — connecting starts the live session.
"""
import asyncio
import json

import websockets

# ── Pre-filled from the RIC Test UI ─────────────────────────────────────────
WS_URL = "__WS_URL__"   # the /ws/e2-real endpoint
# Only set when the platform requires a login; it EXPIRES, so re-download the
# connector or paste a fresh token when it does.
API_TOKEN = "__API_TOKEN__"
# JOIN the live session the operator already started in the UI, so you drive the
# SAME engine run on THEIR scenario and they watch every action you take. Leave
# it empty to start your own session on the engine's default scenario instead.
SESSION_ID = "__SESSION_ID__"
LABEL      = "my-xapp"


# ── YOUR AI GOES HERE ───────────────────────────────────────────────────────
# Load your trained model ONCE at module scope (onnxruntime / torch / TF / SB3…),
# it runs on your machine.
#
# Each live KPM indication (real E2SM-KPM from the running RAN):
#   kpm["tick"]:         indication counter
#   kpm["ue_ids"]:       UE ids seen this indication
#   kpm["measurements"]: {name: {"count","min","max","mean"}}, real metrics incl.
#       "DRB.UEThpDl"    DL throughput per UE (kbps)
#       "RRU.PrbUsedDl"  PRB usage
#       "L3servingSINR"  serving SINR (dB)
#       (+ PDCP volume/bitrate/delay, TB counters, ...)
#   kpm["ues"]:   per-UE rows: {imsi, cell, sinr, thp_kbps, ul_kbps, delay_ms,
#                 prb, slice}  — slice = eMBB/uRLLC/mMTC/voice from the scenario
#   kpm["cells"]: per-cell:    {cell, ues, thp_kbps, ul_kbps, prb, sinr,
#                 power_w}     — power_w is MEASURED (radio energy model)
#   kpm["slices"]:    {slice: DL kbps} real per-slice totals
#   kpm["handovers"]: serving-cell changes since session start
#   kpm["energy"]:    {total_w, per_cell: {cell: {j, w}}} measured energy
#
# Return ONE control command dict to act this tick, or None to just observe:
#   {"cmd": "cell_off", "cell": N}              # sleep gNB cell N (Energy_state) — REAL
#   {"cmd": "cell_on",  "cell": N}              # wake cell N back up
#   {"cmd": "cell_power", "cell": N, "dbm": P}  # set cell N's live TX power to P dBm
#   {"cmd": "handover", "ue": IMSI, "cell": N}  # move a UE (its KPM UEID = IMSI) to cell N
#   {"cmd": "slice_quota", "qci": Q, "pct": P}  # REAL slicing: cap slice Q (its bearers'
#                                               # 5QI) at P% of the MAC's DL symbols —
#                                               # E2SM-RC style 2, applies to every cell
#   {"cmd": "rc_qos"}                           # QoS ping (proves the loop, no effect)
def decide(kpm: dict):
    m = kpm.get("measurements", {})
    sinr = m.get("L3servingSINR", {}).get("mean")
    # Demo policy: if serving SINR is comfortably high, sleep a redundant cell to
    # save energy. Replace this with your trained model's inference.
    if sinr is not None and sinr > 5:
        return {"cmd": "cell_off", "cell": 4}
    return None


# ── Connection plumbing — you shouldn't need to edit below ───────────────────
async def _run() -> None:
    _auth = {"Authorization": f"Bearer {API_TOKEN}"} if API_TOKEN else None
    async with websockets.connect(WS_URL, additional_headers=_auth) as ws:
        if SESSION_ID and not SESSION_ID.startswith("PASTE"):
            await ws.send(json.dumps({"join": SESSION_ID, "label": LABEL}))
            print(f"[xApp] joined the operator's live session {SESSION_ID} as '{LABEL}'")
        else:
            print("[xApp] connected to the REAL engine at", WS_URL)
        async for raw in ws:
            msg = json.loads(raw)
            mtype = msg.get("type")
            if mtype == "kpm":
                cmd = decide(msg)
                if cmd:
                    await ws.send(json.dumps(cmd))
                    print("[xApp] control ->", cmd)
            elif mtype == "live_status":
                print("[engine]", msg.get("line") or msg.get("state"))
            elif mtype == "error":
                print("[xApp] error:", msg.get("message"))
                return


if __name__ == "__main__":
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\n[xApp] stopped")
'''
