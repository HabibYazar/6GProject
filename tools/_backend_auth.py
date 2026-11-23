"""One place for the tools to authenticate against the backend.

Every harness here predates authentication and sent no token, so the moment a
deployment sets NSG_AUTH_REQUIRED=1 they all report a wall of 401s that says
nothing about what they were testing. Rather than repeat a sign-in in six
scripts, they ask this for a header.

It is deliberately quiet when the backend does not enforce auth: the header is
empty and nothing changes, so the tools keep working unchanged on a development
box with the switch off.

    from _backend_auth import auth_headers
    requests.get(url, headers=auth_headers(API))
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

_CACHE: dict[str, dict[str, str]] = {}

USER = os.environ.get("NSG_QA_USER", "admin")
PASSWORD = os.environ.get("NSG_QA_PASS", "admin12345")


def _post(api: str, path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        api.rstrip("/") + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:  # noqa: BLE001
        return 0, {}


def auth_headers(api: str = "http://localhost:8000") -> dict[str, str]:
    """A bearer header when this backend enforces auth, else nothing.

    Cached per API base: a tool makes hundreds of calls and should not log in for
    each. A failure to sign in returns an empty header rather than raising — the
    caller's own checks then report the 401 honestly, which is more useful than
    this helper deciding the run is over.
    """
    if api in _CACHE:
        return dict(_CACHE[api])
    headers: dict[str, str] = {}
    try:
        with urllib.request.urlopen(api.rstrip("/") + "/auth/me", timeout=15) as r:
            enforced = json.loads(r.read() or b"{}").get("enforced")
    except Exception:  # noqa: BLE001
        enforced = False
    if enforced:
        st, body = _post(api, "/auth/login",
                         {"username": USER, "password": PASSWORD})
        if st == 200 and body.get("access_token"):
            headers = {"Authorization": f"Bearer {body['access_token']}"}
        else:
            print(f"[auth] could not sign in as {USER} — set NSG_QA_USER / "
                  f"NSG_QA_PASS; guarded routes will answer 401")
    _CACHE[api] = headers
    return dict(headers)
