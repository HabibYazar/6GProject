#!/usr/bin/env python3
"""WebSocket authentication, end to end against a running backend.

HTTP was guarded first and the sockets were not, which left the live RIC-test
path open on a deployment that refused every REST call — the kind of gap that
looks closed from the outside.

Needs a running server, so it SKIPS (exit 0) when none is reachable or when the
backend does not enforce; CI runs it against an enforcing one.

    python3 nsg_ai/test_ws_auth.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("NSG_API", "http://localhost:8000")
WS = API.replace("http", "ws", 1)
PATHS = ["/ws/live-traffic", "/ws/e2", "/ws/e2-real"]
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILS.append(name)


def _login() -> str | None:
    req = urllib.request.Request(
        f"{API}/auth/login",
        data=json.dumps({"username": os.environ.get("NSG_QA_USER", "admin"),
                         "password": os.environ.get("NSG_QA_PASS", "admin12345")}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())["access_token"]
    except Exception:  # noqa: BLE001
        return None


async def _opens(url: str, headers: dict | None = None) -> bool:
    import websockets  # noqa: PLC0415
    try:
        async with websockets.connect(url, open_timeout=15,
                                      additional_headers=headers or None):
            return True
    except Exception:  # noqa: BLE001
        return False


async def main() -> int:
    try:
        with urllib.request.urlopen(f"{API}/auth/me", timeout=10) as r:
            me = json.loads(r.read())
    except Exception:  # noqa: BLE001
        print("no backend reachable — skipping")
        return 0
    if not me.get("enforced"):
        print("backend is not enforcing auth — skipping")
        return 0

    token = _login()
    if not token:
        print("cannot log in — skipping")
        return 0

    print("=" * 60)
    print("WEBSOCKET AUTH")
    print("=" * 60)
    for path in PATHS:
        check(f"{path} refuses an unauthenticated socket",
              not await _opens(f"{WS}{path}"))
        check(f"{path} accepts ?token= (what a browser must use)",
              await _opens(f"{WS}{path}?token={token}"))
        check(f"{path} accepts a bearer header (what an xApp should use)",
              await _opens(f"{WS}{path}", {"Authorization": f"Bearer {token}"}))
    check("a garbage token is refused",
          not await _opens(f"{WS}/ws/e2?token=not.a.real.token"))

    print()
    if FAILS:
        print(f"{len(FAILS)} CHECK(S) FAILED: {'; '.join(FAILS)}")
        return 1
    print("ALL WEBSOCKET AUTH CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
