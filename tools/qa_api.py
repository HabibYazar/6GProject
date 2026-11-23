#!/usr/bin/env python3
"""
API robustness QA — every endpoint, with the inputs a real user eventually sends.

The rule being tested: a bad request gets a 4xx with a reason. A 500 means the
backend hit an unhandled path, and a 200 on nonsense means it accepted something
it will choke on later. Both are bugs; the second is worse, because it surfaces
somewhere else entirely (that is how a base station with no antenna reached the
canvas).

Run: python3 tools/qa_api.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = "http://localhost:8000"
GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

FAILS: list[str] = []

# Set by _sign_in() when the deployment enforces authentication. Without it every
# check against a guarded route answers 401 and this harness reports fifteen
# failures that say nothing about the API's behaviour — it was written before
# auth existed and would otherwise only work with enforcement switched off.
_TOKEN: str | None = None
QA_USER = os.environ.get("NSG_QA_USER", "admin")
QA_PASS = os.environ.get("NSG_QA_PASS", "admin12345")


def _sign_in() -> None:
    """Obtain a token if — and only if — this backend asks for one."""
    global _TOKEN
    st, body = call("GET", "/auth/me")
    if st != 200:
        return
    try:
        if not json.loads(body).get("enforced"):
            return
    except Exception:  # noqa: BLE001
        return
    st, body = call("POST", "/auth/login",
                    {"username": QA_USER, "password": QA_PASS})
    if st != 200:
        print(f"{RED}cannot sign in as {QA_USER}{RESET} — set NSG_QA_USER / "
              f"NSG_QA_PASS, or run against a backend with auth off")
        raise SystemExit(2)
    _TOKEN = json.loads(body)["access_token"]
    print(f"{DIM}authenticated as {QA_USER}{RESET}")


def call(method: str, path: str, body=None, timeout: int = 30):
    """The FULL body — callers truncate for display only.

    Reading a fixed 4000 bytes made this harness fail as soon as the campaign
    list outgrew it: the JSON came back cut in half and the parse error read like
    a backend bug. A test that breaks on its own success is worse than no test.
    """
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if _TOKEN:
        headers["Authorization"] = f"Bearer {_TOKEN}"
    req = urllib.request.Request(f"{API}{path}", data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {GREEN + 'PASS' + RESET if cond else RED + 'FAIL' + RESET}  {name}"
          + (f"  {DIM}{detail[:150]}{RESET}" if detail else ""))
    if not cond:
        FAILS.append(name)


def expect(name: str, method: str, path: str, body=None, *, want: set[int]) -> None:
    status, text = call(method, path, body)
    check(f"{name} -> {status}", status in want,
          "" if status in want else f"wanted {sorted(want)}: {text}")


MINIMAL = {
    "name": "qa", "base_stations": [
        {"id": "c1", "name": "C1", "position": {"x": 0, "y": 0, "z": 25},
         "radio": {"band": {"label": "b", "frequency_mhz": 3500,
                            "bandwidth_mhz": 50, "numerology": 2}}}],
    "ue_groups": [],
}


def main() -> int:
    _sign_in()
    print("\n[1] unknown ids are 404, not 500")
    expect("GET /campaigns/<unknown>", "GET", "/campaigns/does-not-exist", want={404})
    expect("GET /sim/run/<unknown>", "GET", "/sim/run/does-not-exist", want={404})
    expect("DELETE /campaigns/<unknown>", "DELETE", "/campaigns/does-not-exist", want={404})
    expect("POST /campaigns/<unknown>/stop", "POST", "/campaigns/nope/stop", want={404})
    expect("GET /campaigns/<unknown>/compare", "GET",
           "/campaigns/nope/compare?to=also-nope", want={404})

    print("\n[2] malformed config is rejected with a reason, never accepted")
    expect("normalize {}", "POST", "/config/normalize", {}, want={422})
    expect("normalize null fields", "POST", "/config/normalize",
           {"name": None, "base_stations": None}, want={422})
    expect("normalize wrong types", "POST", "/config/normalize",
           {"name": 5, "base_stations": "not-a-list", "ue_groups": {}}, want={422})
    expect("normalize no base stations", "POST", "/config/normalize",
           {"name": "x", "base_stations": [], "ue_groups": []}, want={422})
    expect("normalize cell outside the area", "POST", "/config/normalize",
           {**MINIMAL, "base_stations": [{**MINIMAL["base_stations"][0],
                                          "position": {"x": 99999, "y": 0, "z": 25}}]},
           want={422})
    expect("normalize negative frequency", "POST", "/config/normalize",
           {**MINIMAL, "base_stations": [
               {**MINIMAL["base_stations"][0],
                "radio": {"band": {"label": "b", "frequency_mhz": -1,
                                   "bandwidth_mhz": 50, "numerology": 2}}}]},
           want={422})
    expect("normalize a valid minimal config", "POST", "/config/normalize",
           MINIMAL, want={200})

    print("\n[3] campaigns reject what they cannot run")
    expect("campaign with no items", "POST", "/campaigns",
           {"name": "empty", "items": [], "thresholds": []}, want={422})
    expect("campaign with duplicate item labels", "POST", "/campaigns",
           {"name": "dupe", "thresholds": [], "items": [
               {"label": "a", "config": MINIMAL}, {"label": "a", "config": MINIMAL}]},
           want={422})
    expect("campaign with an invalid config", "POST", "/campaigns",
           {"name": "bad", "thresholds": [], "items": [
               {"label": "a", "config": {"name": "x"}}]}, want={422})
    expect("campaign with a bad threshold operator", "POST", "/campaigns",
           {"name": "op", "items": [{"label": "a", "config": MINIMAL}],
            "thresholds": [{"metric": "dl_mbps_mean", "op": "≥", "value": 1}]},
           want={422})
    expect("campaign with repeats out of range", "POST", "/campaigns",
           {"name": "rep", "thresholds": [], "items": [
               {"label": "a", "config": MINIMAL, "repeats": 99}]}, want={422})

    print("\n[4] history and compare handle empty / odd inputs")
    expect("history for an unknown suite", "GET",
           "/campaigns/history?name=no-such-suite", want={200})
    expect("history with a silly limit", "GET",
           "/campaigns/history?limit=99999", want={200})
    expect("history with a negative limit", "GET",
           "/campaigns/history?limit=-5", want={200})

    print("\n[5] the read endpoints answer")
    for path in ("/health", "/schema", "/campaigns", "/sim/runs", "/engine-status"):
        expect(f"GET {path}", "GET", path, want={200})

    print("\n[6] a campaign cannot be its own baseline")
    status, text = call("GET", "/campaigns")
    ids = [c["id"] for c in json.loads(text).get("campaigns", [])] if status == 200 else []
    if ids:
        expect("compare a campaign with itself", "GET",
               f"/campaigns/{ids[0]}/compare?to={ids[0]}", want={400})
    else:
        print(f"  {DIM}skip  no campaign stored to self-compare{RESET}")

    print("\n" + ("API QA PASSED" if not FAILS else f"{len(FAILS)} FAILURE(S): {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
