#!/usr/bin/env python3
"""
The AI prompt CONTRACT: can the generator express what the schema offers?

The generator copies the prompt's "EXACT SCHEMA" skeleton. A field that is not in
that skeleton is a field the AI will never emit — no matter that the backend
accepts it, the editor renders it and the engine honours it. Measured: the schema
had grown to 168 parameter fields while the skeleton still showed 54, and the four
newest capability layers (anomalies, slices, waypoints, the whole handover-event
block) were absent. The visible symptom was a demo: asked for a handover test, the
AI produced a correct-looking static scenario, because it had no way to say
"handover".

So this test asserts, in both directions:
  1. Every field the AI MUST be able to set appears in the skeleton.
  2. Every enum the skeleton names lists exactly the schema's values — an invented
     value is a 422 the user sees as "the AI failed".
  3. Defaults shown in the skeleton match the schema's, because the model copies
     them. Showing ho_check_period_ms: 0 would hand it the one value that
     silently disables handover evaluation.

Run: cd nsg_ai && ../venv/bin/python test_prompt_contract.py
"""
from __future__ import annotations

import json
import re
import pathlib
import sys

sys.path.insert(0, "/home/chancelor/nsg_ai_platform/nsg_ai")

from schema import (AnomalyType, MobilityPattern, NetworkSlice,  # noqa: E402
                    PropagationScenario, SimulationSettings, WallMaterial)

FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILS.append(name)


def prompt_text() -> str:
    """The prompt as the LLM sees it: braces unescaped and the rulebook injected.

    Read from source rather than imported, so this runs without an API key and
    without building LLM clients — but the `__RULEBOOK__` substitution that
    llm_agent does at import time has to be applied here too, or the test checks
    a placeholder and passes on a rulebook that says nothing.
    """
    from engine_envelope import RULEBOOK_PROMPT  # noqa: PLC0415

    # Resolve against this file, not the cwd: run from the repo root (as CI and
    # any sweep harness does) the bare relative path raises FileNotFoundError
    # and the whole contract check silently looks like a failure.
    src = (pathlib.Path(__file__).parent / "llm_agent.py").read_text()
    body = re.search(r'_SYSTEM_PROMPT = """\\\n(.*?)\n"""', src, re.S)
    assert body, "could not find _SYSTEM_PROMPT in llm_agent.py"
    text = body.group(1).replace("{{", "{").replace("}}", "}")
    assert "__RULEBOOK__" in text, "the rulebook placeholder moved — update this test"
    return text.replace("__RULEBOOK__", RULEBOOK_PROMPT.rstrip())


PROMPT = prompt_text()
SKELETON = re.search(r"EXACT SCHEMA(.*?)`anomalies`", PROMPT, re.S)
SKELETON_TEXT = SKELETON.group(1) if SKELETON else ""


def main() -> None:
    print("\n[1] capability layers the AI must be able to express")
    # Each of these is a layer that exists end to end — schema, translator,
    # engine, and a measured effect. If the AI cannot name it, the platform
    # cannot be driven by natural language, only by hand.
    for field, why in (
        ("anomalies", "fault injection"),
        ("slices", "per-5QI PRB policy"),
        ("waypoints", "the only mobility construct that produces handovers"),
        ("waypoint_mode", "what happens at the end of the path"),
        ("height_m", "UE antenna height (drones, rooftop relays)"),
        ("sleep", "RU sleep energy lever"),
        ("ho_check_period_ms", "handover re-evaluation cadence"),
        ("ho_a3_entry", "A3 neighbour-better event"),
        ("ho_a2_threshold_db", "A2 serving-cell-collapsed event"),
        ("ho_hysteresis_db", "ping-pong suppression"),
        ("ho_sinr_margin_db", "handover eagerness"),
        ("handover_mode", "which HO algorithm runs"),
        ("ho_joining_timeout_ms", "MRO too-late window"),
        ("ho_leaving_timeout_ms", "MRO too-early window"),
        ("protocol", "udp vs tcp closes the loop or not"),
        ("call_distribution", "burstiness shape"),
    ):
        check(f"skeleton names `{field}` ({why})", field in SKELETON_TEXT)

    print("\n[2] enums match the schema exactly")
    for label, enum_cls in (
        ("mobility.pattern", MobilityPattern),
        ("services.slice_type", NetworkSlice),
        ("settings.scenario", PropagationScenario),
        ("wall_material", WallMaterial),
        ("anomalies.type", AnomalyType),
    ):
        line = next((ln for ln in PROMPT.splitlines()
                     if ln.strip().startswith(label)), None)
        if line is None:
            check(f"{label} enum is listed", False)
            continue
        listed = set(re.findall(r'"([^"]+)"', line))
        real = {m.value for m in enum_cls}
        check(f"{label}: prompt lists exactly the schema's values"
              + (f" (extra={sorted(listed - real)}, missing={sorted(real - listed)})"
                 if listed != real else ""),
              listed == real)

    # handover_mode is a Literal, not an Enum — read it off the model field.
    hm_line = next((ln for ln in PROMPT.splitlines()
                    if ln.strip().startswith("settings.handover_mode")), "")
    listed = set(re.findall(r'"([^"]+)"', hm_line))
    real = set(SimulationSettings.model_fields["handover_mode"].annotation.__args__)
    check(f"settings.handover_mode: exactly the schema's values"
          + (f" (got {sorted(listed)}, want {sorted(real)})" if listed != real else ""),
          listed == real)

    print("\n[3] skeleton defaults match the schema")
    # The model copies these numbers. A skeleton default that differs from the
    # schema's is a value the AI will emit believing it is neutral —
    # ho_check_period_ms: 0 disables handover evaluation entirely.
    defaults = SimulationSettings()
    for field in ("ho_check_period_ms", "ho_a3_entry", "ho_sinr_margin_db",
                  "ho_ttt_ms", "handover_mode", "ho_joining_timeout_ms",
                  "ho_leaving_timeout_ms", "ho_hysteresis_db"):
        shown = re.search(rf'"{field}":\s*([^,\n}}]+)', SKELETON_TEXT)
        if not shown:
            check(f"{field} appears with a value in the skeleton", False)
            continue
        raw = shown.group(1).strip()
        try:
            got = json.loads(raw)
        except json.JSONDecodeError:
            got = raw.strip('"')
        want = getattr(defaults, field)
        check(f"{field}: skeleton {got!r} == schema default {want!r}", got == want)

    print("\n[4] the intent guide covers the scenario kinds we support")
    for heading in ("HANDOVER / MOBILITY", "ENERGY SAVING", "CAPACITY / CONGESTION",
                    "SLICING / QoS", "FAULT / RESILIENCE"):
        check(f"intent guide has a `{heading}` recipe", heading in PROMPT)

    print("\n[5] the rulebook separates crashes from measurement quality")
    for marker, why in (
        ("HARD LIMITS", "what crashes the engine"),
        ("MEASUREMENT QUALITY", "what only degrades the numbers"),
        ("WHEN THE USER'S REQUEST CONFLICTS", "who wins — the user"),
    ):
        check(f"rulebook states `{marker}` ({why})", marker in PROMPT)

    print("\n" + ("ALL PROMPT CONTRACT CHECKS PASSED" if not FAILS
                  else f"{len(FAILS)} CHECK(S) FAILED: {FAILS}"))
    raise SystemExit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
