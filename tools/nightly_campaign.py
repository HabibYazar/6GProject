#!/usr/bin/env python3
"""
Nightly campaign runner.

`fidelity_report.py` proves the ENGINE still honours each lever. This proves the
SCENARIOS still meet their thresholds — the same question a customer asks of
their own suite, run against ours every night so a regression surfaces the next
morning instead of in a demo.

  python tools/nightly_campaign.py                    # run tools/nightly_campaign.json
  python tools/nightly_campaign.py --spec my.json     # a different suite
  python tools/nightly_campaign.py --write-default    # emit a starter spec

The spec is the same body POST /campaigns takes. `--write-default` builds one
from the fidelity probe scenario, which is already inside the engine's validated
envelope — a nightly that fails because the scenario was out of envelope teaches
nobody anything.

Exit codes: 0 pass (or nothing asserted), 1 a scenario FAILED its thresholds,
2 the bench itself broke (engine down, backend unreachable, run errored).
"""
from __future__ import annotations
import pathlib as _pl, sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
from _backend_auth import auth_headers  # noqa: E402

import argparse
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

API = "http://localhost:8000"
DEFAULT_SPEC = ROOT / "tools" / "nightly_campaign.json"
REPORTS = ROOT / "reports"
POLL_S = 20
MAX_WAIT_S = 4 * 3600  # a 10-run campaign is ~40 min; 4 h is a hang, not a queue


def default_spec() -> dict:
    """A starter suite: the fidelity probe scenario, plus an energy variant.

    Two scenarios rather than one because a suite with a single item cannot show
    what a campaign is for — one passing while the other regresses is the whole
    point. Thresholds are deliberately loose: a nightly that cries wolf on night
    one gets switched off on night two. Tighten them once you have a week of
    numbers to set them from.
    """
    from fidelity_report import baseline_config  # noqa: PLC0415

    base = baseline_config()
    base["name"] = "Baseline — 5 cells, 10 UEs"

    energy = json.loads(json.dumps(base))
    energy["name"] = "Energy saving — idle cell sleeps"
    for cell in energy["base_stations"]:
        cell["radio"].setdefault("sleep", {})
        cell["radio"]["sleep"] = {"mode": "deep", "idle_timer_ms": 200.0}

    return {
        "name": "Nightly suite",
        "description": "Scenario regression watch — see tools/nightly_campaign.py",
        "base_seed": 42,
        "thresholds": [
            {"metric": "dl_mbps_mean", "op": ">=", "value": 1.0,
             "label": "Downlink (mean)"},
            {"metric": "measurements.L3servingSINR.mean", "op": ">=", "value": 0.0,
             "label": "Serving SINR (mean)"},
            {"metric": "ticks", "op": ">=", "value": 5,
             "label": "KPM windows captured"},
        ],
        "items": [
            {"label": "Baseline", "config": base, "frames": 30, "repeats": 2},
            {"label": "Energy saving", "config": energy, "frames": 30, "repeats": 2},
        ],
    }


def previous_campaign(name: str, exclude_id: str) -> str | None:
    """The most recent finished campaign with the same name — the baseline.

    Matching by name is what makes "last night's run" a stable reference without
    anyone having to nominate a baseline by hand every evening.
    """
    try:
        items = requests.get(f"{API}/campaigns", headers=auth_headers(API), timeout=20).json()["campaigns"]
    except requests.RequestException:
        return None
    for c in items:  # already newest-first
        if c["id"] != exclude_id and c["name"] == name and c.get("run_count", 0) > 0 \
                and c["status"] in ("done", "stopped"):
            return c["id"]
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default=str(DEFAULT_SPEC))
    ap.add_argument("--write-default", action="store_true",
                    help="write the starter spec to --spec and exit")
    args = ap.parse_args()

    spec_path = Path(args.spec)
    if args.write_default:
        spec_path.write_text(json.dumps(default_spec(), indent=2))
        print(f"wrote {spec_path}")
        return 0

    if not spec_path.exists():
        print(f"no spec at {spec_path} — run with --write-default first")
        return 2

    spec = json.loads(spec_path.read_text())
    REPORTS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M")

    try:
        started = requests.post(f"{API}/campaigns", json=spec, headers=auth_headers(API), timeout=60)
        started.raise_for_status()
    except requests.RequestException as exc:
        print(f"could not start campaign: {exc}")
        return 2
    campaign_id = started.json()["campaign_id"]
    print(f"campaign {campaign_id}: {started.json()['planned_runs']} run(s) queued")

    deadline = time.time() + MAX_WAIT_S
    record: dict = {}
    while time.time() < deadline:
        time.sleep(POLL_S)
        try:
            record = requests.get(f"{API}/campaigns/{campaign_id}", headers=auth_headers(API), timeout=30).json()
        except requests.RequestException as exc:
            print(f"poll failed ({exc}) — retrying")
            continue
        done = len(record.get("runs", []))
        print(f"  {done}/{len(record.get('planned', []))} runs · {record.get('status')}")
        if record.get("status") in ("done", "stopped"):
            break
    else:
        print("campaign did not finish within the time budget")
        return 2

    verdict = record.get("verdict")
    line = f"{stamp}  campaign {campaign_id}  {spec['name']}  VERDICT: {verdict}"

    # Compare against the previous run of the same suite — the point of a
    # nightly is the delta, not the absolute.
    baseline_id = previous_campaign(spec["name"], campaign_id)
    diff = None
    if baseline_id:
        try:
            diff = requests.get(f"{API}/campaigns/{campaign_id}/compare", headers=auth_headers(API),
                                params={"to": baseline_id}, timeout=60).json()
            line += f"  vs {baseline_id}: {diff['overall']}"
        except requests.RequestException as exc:
            print(f"could not diff against {baseline_id}: {exc}")

    print(line)
    (REPORTS / "campaign-nightly.log").open("a").write(line + "\n")

    if diff:
        for item in diff["items"]:
            if item["change"] == "regressed":
                print(f"  REGRESSED: {item['label']}")
                for m in item["metrics"]:
                    if m["is_threshold"] and not m["within_noise"]:
                        print(f"    {m['metric']}: {m['baseline']} -> {m['current']} "
                              f"({m['pct']}%)")

    red = REPORTS / "CAMPAIGN_RED"
    if verdict == "fail" or (diff and diff["overall"] == "regressed"):
        red.write_text(line + "\n" + json.dumps(
            [i["label"] for i in (diff or {}).get("items", [])
             if i["change"] == "regressed"]) + "\n")
        return 1
    if verdict == "error":
        red.write_text(line + "\n(bench error — engine or backend, not the scenario)\n")
        return 2
    red.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
