"""
NSG AI — LangChain Agent

Strategy: plain LLM call with JSON-in-prompt, then Pydantic parse.
Avoids with_structured_output() which compiles the full schema into an Anthropic
grammar — too large for our nested NSGSimulationConfig.

Model routing (both overridable — NSG_TEXT_MODEL / NSG_CONFIG_MODEL):
  _text_llm    →  claude-haiku-4.5 — fast, free-text (report generation)
  _config_llm  →  claude-opus-5    — JSON config generation, the quality-critical one

API key: set OPENROUTER_API_KEY in your environment (or .env file).
"""
from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, AIMessage
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from schema import NSGSimulationConfig
from engine_envelope import RULEBOOK_PROMPT


# ═══════════════════════════════════════════════════════════════════════════════
# LLM INSTANCES
# ═══════════════════════════════════════════════════════════════════════════════

# Load nsg_ai/.env (dependency-free — python-dotenv isn't a project dep) so the
# API key lives in config, not code. Real environment variables take precedence.
def _load_env_file(path: str) -> None:
    try:
        with open(path) as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_env_file(os.path.join(os.path.dirname(__file__), ".env"))

_API_KEY = os.environ.get("OPENROUTER_API_KEY")
if not _API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY is not set — add it to nsg_ai/.env or the environment")
_BASE_URL = "https://openrouter.ai/api/v1"

# Both models are overridable so a run can be pinned or rolled back without an
# edit — `NSG_TEXT_MODEL` / `NSG_CONFIG_MODEL`. OpenRouter normalises the hyphen
# form to its own dotted ids (verified: `claude-sonnet-4-6` is served as
# `anthropic/claude-sonnet-4.6`), so either spelling resolves.
_text_llm = ChatOpenAI(
    model=os.environ.get("NSG_TEXT_MODEL", "anthropic/claude-haiku-4.5"),
    temperature=0,
    max_tokens=1000,
    api_key=_API_KEY,
    base_url=_BASE_URL,
)

# Plain LLM — no with_structured_output. We parse the JSON response ourselves.
#
# Config generation is the quality-critical call: structured JSON against a
# 168-field schema where an invalid value reaches a real ns-3 engine. Opus 5 is
# the top of the current line and thinks by default (measured through
# OpenRouter: reasoning_tokens > 0 with no extra parameter).
#
# Cost, measured 2026-09-16 at 11.7k system-prompt tokens + ~1.3k out:
#   sonnet-4.6 (what this used to be)  ~$0.055 / generation
#   opus-5                             ~$0.069 / generation
#   opus-5 WITH prompt caching         ~$0.039 / generation  <- cheaper than before
# Caching is not wired yet: the system prompt goes through ChatPromptTemplate as
# a plain string, and cache_control needs it as a content block. Worth doing when
# volume matters; it buys cost, not quality (12x on input, measured).
_config_llm = ChatOpenAI(
    model=os.environ.get("NSG_CONFIG_MODEL", "anthropic/claude-opus-5"),
    temperature=0,
    max_tokens=8000,
    api_key=_API_KEY,
    base_url=_BASE_URL,
)


# ═══════════════════════════════════════════════════════════════════════════════
# PROMPTS
# ═══════════════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """\
You are a Senior 5G Network Simulation Engineer.

You receive a current simulation configuration (as JSON) and a natural-language \
modification request. Your job: apply the requested changes and return the \
COMPLETE updated configuration object.

Rules:
- Return the FULL object — not just the changed fields.
- Preserve every unchanged field exactly as it is.
- Apply ONLY the modifications the user explicitly requested.
- If current_config is empty, generate a sensible new configuration from scratch.

════════════════════════════════════════════════════════
EXACT SCHEMA — copy field names exactly, including nesting
════════════════════════════════════════════════════════
{{
  "name": "Scenario name",
  "description": "Free text",
  "base_stations": [
    {{
      "id": "gnb-site-a-s1",
      "name": "Site-A / N77 / S1",
      "position": {{"x": 0.0, "y": 0.0, "z": 30.0}},
      "radio": {{
        "band": {{"label": "NR-N77", "frequency_mhz": 3600, "bandwidth_mhz": 100, "numerology": 1}},
        "tx_power_dbm": 46,
        "antenna": {{
          "type": "cosine",
          "azimuth_deg": 0, "tilt_deg": 3, "gain_dbi": 17,
          "horizontal_beamwidth_deg": 65, "vertical_beamwidth_deg": 7.5
        }},
        "sleep": {{"mode": "none", "idle_timer_ms": 200.0}}
      }}
    }}
  ],
  "ue_groups": [
    {{
      "id": "ue-group-1",
      "name": "Downtown Pedestrians",
      "count": 50,
      "spawn_position": {{"x": 100.0, "y": 100.0}},
      "height_m": 1.5,
      "mobility": {{
        "pattern": "pedestrian", "speed_mps": 1.4,
        "waypoints": [
          {{"position": {{"x": 0.0, "y": 0.0, "z": 1.5}}, "speed_mps": 1.4}},
          {{"position": {{"x": 400.0, "y": 0.0, "z": 1.5}}, "speed_mps": 1.4}}
        ],
        "waypoint_mode": "pingpong"
      }},
      "services": [{{
        "slice_type": "eMBB", "qos_id": 8, "target_throughput_gbps": 0.1,
        "protocol": "udp", "call_distribution": "exponential",
        "avg_call_duration_s": 30, "avg_idle_duration_s": 10
      }}]
    }}
  ],
  "buildings": [
    {{
      "id": "bldg-office-a",
      "bounds": {{"x_min": 80, "x_max": 200, "y_min": 60, "y_max": 140, "z_min": 0, "z_max": 40}},
      "building_type": "office",
      "wall_material": "concrete_with_windows",
      "floors": 10, "rooms_x": 4, "rooms_y": 4
    }}
  ],
  "anomalies": [
    {{
      "type": "cell_outage", "cell_id": "gnb-site-a-s1",
      "start_s": 10.0, "duration_s": 20.0
    }}
  ],
  "slices": [
    {{"qos_id": 9, "max_pct": 40, "min_pct": 10, "dedicated_pct": 0}}
  ],
  "area": {{"x_min": -600, "x_max": 700, "y_min": -500, "y_max": 500, "resolution_m": 10}},
  "settings": {{
    "duration_s": 60, "report_interval_ms": 1000,
    "scenario": "UMa-Buildings",
    "shadowing_enabled": true, "random_seed": 42,
    "handover_mode": "DynamicTtt", "ho_sinr_margin_db": 3.0,
    "ho_check_period_ms": 100, "ho_a3_entry": true, "ho_hysteresis_db": 0.0,
    "ho_a2_threshold_db": null, "ho_ttt_ms": 110,
    "ho_joining_timeout_ms": 45.0, "ho_leaving_timeout_ms": 45.0
  }}
}}

`anomalies`, `slices`, `mobility.waypoints` and the `settings.ho_*` block are
OPTIONAL — omit them unless the scenario calls for them. Everything else above
should be present.

Valid enum values (use EXACTLY these strings):
  antenna.type        : "cosine" | "massive_mimo" | "isotropic"
  mobility.pattern    : "static" | "pedestrian" | "vehicle" | "drone" | "random_walk"
  mobility.waypoint_mode : "once" | "repeat" | "pingpong"
  services.slice_type : "eMBB" | "uRLLC" | "voice" | "mMTC"
  services.protocol   : "udp" | "tcp"
  services.call_distribution : "exponential" | "constant" | "uniform" | "pareto"
  building_type       : "residential" | "office" | "industrial"
  wall_material       : "wood" | "concrete" | "concrete_with_windows"
  settings.scenario   : "UMa" | "UMa-Buildings" | "UMi" | "RMa"
  settings.handover_mode : "FixedTtt" | "DynamicTtt" | "Threshold" | "NoAuto"
  radio.sleep.mode    : "none" | "micro" | "light" | "deep"
  anomalies.type      : "radio_degradation" | "cell_outage" | "noise_rise" | "handover_block"

Key units:
  Coordinates: meters (x=East, y=North, z=height)
  frequency_mhz / bandwidth_mhz in MHz
  tx_power_dbm in dBm  (typical macro gNB: 43–46)
  target_throughput_gbps in Gbps (per-UE max: 1.0)
  services may add call dynamics: avg_call_duration_s + avg_idle_duration_s
  (exponential on/off bursts — e.g. 30/10 for browsing, 60/5 for streaming;
  omit both for a continuous stream). Fluctuating load makes RIC tests
  realistic, so prefer setting them when the scenario mentions users/apps.
  A group's services list is REAL: each service (max 3) becomes its own
  traffic stream with its own rate and call model — e.g. a vehicle group can
  carry voice (qos_id 1, low rate, bursty) AND data (qos_id 9) side by side.
  mobility.speed_mps is REAL (pedestrians ~1.4, vehicles 15-30 — drives the
  UEs' actual motion) and settings.ho_sinr_margin_db sets the real handover
  margin (1 = aggressive, 3 = default, 10+ = sticky). Note: motion-driven
  handover churn only shows over longer/live horizons, not in short batch runs.
  radio.sleep is the REAL energy-saving lever: a cell with sleep.mode
  "micro"/"light"/"deep" descends into that RU sleep state once it has carried
  no traffic for sleep.idle_timer_ms, drawing ~69/50/33% of its idle power, and
  pays a wake-up delay before it can serve again. Use it for energy-saving
  scenarios (deep on capacity cells that are idle off-peak, micro/light where
  traffic can return at any moment); leave "none" when the scenario is about
  coverage or capacity. This is NOT the same as active:false — that switches a
  cell off entirely and nothing brings it back.
  area must enclose all base_station positions with ≥200 m margin on each side

════════════════════════════════════════════════════════
SCENARIO INTENT -> WHICH FIELDS TO SET
════════════════════════════════════════════════════════
A scenario is only a test of what its fields express. Name the intent behind the
request, then set THAT layer — a "handover test" with no waypoints and no HO
settings is a static scenario with a handover-shaped title.

HANDOVER / MOBILITY test ("a user walks from one cell to another", "test HO",
"ping-pong", "MRO"):
  - 2-3 cells at DISTINCT positions, **at least 200 m apart** — and note this is
    STRICTER than the rulebook's global "min 50 m" floor. That 50 m is a hard
    engine limit (co-located cells break the link budget); it is nowhere near
    enough for a handover to be observable, because the UE crosses the whole
    coverage boundary in a couple of metres. Measured 2026-09-16: a generation
    that placed the cells 60 m apart satisfied the 50 m floor and still produced
    a scenario no handover could be seen in.
    Making the source cell WEAKER (e.g. 40 dBm vs 46 dBm) is what makes the
    handover inevitable rather than marginal.
  - mobility.waypoints: a path that actually CROSSES from one cell's coverage
    into the next — start near cell A, end near cell B. This is the only
    mobility construct that produces handovers; `pattern` alone does not move a
    UE across a boundary (random walk diffuses and nets almost no displacement).
    waypoint_mode "once" for a single crossing, "pingpong" to repeat it.
  - settings.ho_check_period_ms: MUST stay > 0 (100 is the default and a
    realistic RRC cadence). 0 means "evaluate once at t=0", so mobility can then
    never trigger a handover — never set 0 in a handover scenario.
  - settings.ho_a3_entry: true to use the A3 (neighbour-better-than-serving)
    event; ho_hysteresis_db + ho_sinr_margin_db tune how eager it is (low =
    eager and ping-pong-prone, high = sticky).
  - settings.ho_a2_threshold_db: set it (e.g. -6) when the story is "the serving
    cell collapsed, leave immediately" rather than "a neighbour got better".
  - For MRO / handover-failure stories: ho_joining_timeout_ms (target side, "too
    late") and ho_leaving_timeout_ms (source side, "too early"), or an
    anomalies entry of type "handover_block".
  - the crossing must fit the WINDOW BUDGET, not duration_s. `duration_s` is
    used in full by the fast analytical tier but does NOT reach the ns-O-RAN
    engine, so it cannot buy a longer walk there — see the horizon arithmetic in
    the rulebook (path / speed_mps * 13 <= the budget, 40 windows by default).
    Sizing the walk against duration_s is what produced a design claiming a 20 s
    horizon for a run that measured 3.0 s.

ENERGY SAVING ("save power", "sleep idle cells", "off-peak"):
  - radio.sleep.mode per cell ("deep" on capacity cells that go idle,
    "micro"/"light" where traffic can return at any moment) + idle_timer_ms.
  - Give the sleeping cell NO nearby UE group — a cell with traffic never idles,
    and the lever measures nothing.

CAPACITY / CONGESTION ("busy", "stadium", "peak hour", "overload"):
  - Raise ue_groups[].count and services[].target_throughput_gbps, keep
    bandwidth_mhz at the LOW end of the range so demand exceeds supply.
  - Spread the UEs across cells (see the per-cell limits) rather than piling
    them on one spawn point.

SLICING / QoS ("guarantee the uRLLC slice", "protect voice"):
  - slices[] with the 5QI of the traffic to protect: max_pct is a ceiling,
    min_pct a work-conserving floor, dedicated_pct reserved outright.
  - The policy only bites if some UE actually carries that qos_id.

FAULT / RESILIENCE ("what if a cell fails", "interference", "degradation"):
  - anomalies[]: "cell_outage" (dark), "radio_degradation" (lower power ->
    coverage shrinks), "noise_rise" (power unchanged, SINR/quality falls — the
    interference story), "handover_block" (the network refuses handovers).
  - start_s is in SIMULATED seconds, and the simulated horizon of a batch run is
    a FRACTION OF A SECOND — not duration_s. Measured: a fault at start_s=5.0
    never fired and the run was byte-identical to one with no fault; the same
    fault at start_s=0.05 cost 9.2% throughput. So schedule faults in the first
    tenths of a second (0.02-0.2) unless the user asks for a long/live run.
  - duration_s 0 means "never recovers".

════════════════════════════════════════════════════════
REAL-E2AP (ns-O-RAN) ENGINE ENVELOPE — design within these
════════════════════════════════════════════════════════
__RULEBOOK__

RESPONSE FORMAT:
Output ONLY a single valid JSON object. No markdown fences, no explanations.\
"""

# Inject the ns-O-RAN engine envelope (single source of truth: engine_envelope.py)
# so generated scenarios validate cleanly on the real E2AP stack. Update the
# numbers in engine_envelope.py and both this prompt and the backend clamp follow.
_SYSTEM_PROMPT = _SYSTEM_PROMPT.replace("__RULEBOOK__", RULEBOOK_PROMPT.rstrip())

_MODIFY_PROMPT = ChatPromptTemplate.from_messages([
    ("system", _SYSTEM_PROMPT),
    MessagesPlaceholder("history", optional=True),
    ("user",
     "Current configuration:\n{current_config}\n\n"
     "Modification request: {user_prompt}"),
])

_MODIFY_CHAIN = _MODIFY_PROMPT | _config_llm


# ═══════════════════════════════════════════════════════════════════════════════
# JSON EXTRACTION HELPER
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_json(text: str) -> str:
    """Strip markdown fences and extract the raw JSON object from LLM output."""
    text = text.strip()
    # Remove ```json ... ``` or ``` ... ``` fences
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    # If there's leading/trailing prose, find the outermost { ... }
    start = text.find("{")
    end   = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return text


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════════

def modify_configuration(
    current_config: dict,
    user_prompt: str,
    history: list[dict] | None = None,
) -> NSGSimulationConfig:
    """
    Apply a natural-language modification request to the current configuration.

    Returns a fully validated NSGSimulationConfig.
    Retries once if the LLM output fails Pydantic validation, feeding the
    error details back so the model can self-correct.
    """
    lc_history: list[HumanMessage | AIMessage] = []
    if history:
        for turn in history:
            role    = turn.get("role", "user")
            content = turn.get("content", "")
            if not content:
                continue
            if role == "user":
                lc_history.append(HumanMessage(content=content))
            elif role == "assistant":
                lc_history.append(AIMessage(content=content))

    config_json = json.dumps(current_config, indent=2) if current_config else "{}"

    last_exc: Exception | None = None

    for attempt in range(2):
        effective_prompt = user_prompt
        if attempt == 1 and last_exc is not None:
            # Retry: tell the model exactly what went wrong
            err_summary = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                for e in (last_exc.errors() if hasattr(last_exc, "errors") else [])[:5]
            )
            effective_prompt = (
                f"{user_prompt}\n\n"
                f"[RETRY — your previous response failed validation: {err_summary}. "
                f"Fix only those fields and output the complete corrected JSON.]"
            )
            logger.warning("Attempt 1 failed (%s). Retrying with error context.", err_summary)

        result = _MODIFY_CHAIN.invoke({
            "current_config": config_json,
            "user_prompt":    effective_prompt,
            "history":        lc_history or [],
        })

        raw_text = result.content if hasattr(result, "content") else str(result)
        logger.debug("LLM raw output (attempt %d):\n%s", attempt + 1, raw_text)

        json_text = _extract_json(raw_text)

        try:
            return NSGSimulationConfig.model_validate_json(json_text)
        except Exception as exc:
            last_exc = exc
            logger.error("Validation failed (attempt %d): %s", attempt + 1, exc)

    raise last_exc  # type: ignore[misc]


def generate_text(prompt: str) -> str:
    """Free-form text generation — used for report analysis and summaries."""
    response = _text_llm.invoke(prompt)
    return response.content.strip()


# ═══════════════════════════════════════════════════════════════════════════════
# AI xApp — LLM as an autonomous RAN controller (RIC test)
# ═══════════════════════════════════════════════════════════════════════════════

_XAPP_SYSTEM = """\
You are an autonomous O-RAN xApp controlling a live RAN in a RIC test bed.
Each step you receive a KPM report and must issue RC control actions that move
the network toward the operator's GOAL. You are being validated — good, stable
decisions matter.

Available actions (return a JSON array of zero or more):
  {"action":"handover","ue_group":"<id>","target_cell":"<cell id>"}   // move a UE group to another cell
  {"action":"cell_power","cell":"<id>","state":"off"|"on"}            // energy saving: sleep/wake a cell
  {"action":"cell_power","cell":"<id>","dbm":<0-46>}                  // GRADED power: set exact TX dBm (0=sleep). Turn a cell DOWN to cut interference or save energy without a full outage, or UP to rescue a weak-SINR edge — finer than on/off.
  {"action":"slice_quota","slice":"eMBB|uRLLC|mMTC|voice","prb_percent":<0-100>}  // cap a slice's PRBs
  {"action":"reset"}                                                  // clear all your overrides

Reading the KPM (real-engine sessions carry MEASURED extras):
- cells[].power_w_measured / total_power_w_measured: real watts from the radio
  energy model — for energy goals, optimise THESE, and remember that sleeping a
  loaded cell can RAISE total power (its UEs fall to bad links and burn
  retransmissions); the safe savings are idle/low-traffic cells.
- cells[].ul_mbps and ue_groups[].ul_mbps/delay_ms: uplink and latency — a
  latency goal watches delay_ms, not just throughput. BUT DO NOT SCORE A
  DECISION ON delay_ms ALONE: measured 2026-09-14, six runs of the same config
  with the same seed gave delay means spanning 41 to 574 ms — a 14x spread with
  nothing changed, where throughput moved 1.1x. It is built from four or five
  samples and the first carries attach-time buffering. Use it to SEE latency,
  and judge a change by throughput, PRB or energy, which hold still.
- ue_groups[].slice + top-level slices[]: who belongs to which slice and each
  slice's real DL total — slice_quota decisions should be justified by these.
- handovers: serving-cell changes so far — rising fast means ping-pong; avoid
  adding churn.

Hard rules:
- Output ONLY a JSON array. No prose, no markdown. Example: [{"action":"cell_power","cell":"c1","state":"off"}]
- Be CONSERVATIVE. If the network already meets the goal, return [].
- Do NOT oscillate: never undo an action you just took, and never repeat an
  action that is already in effect (check control_state and your recent actions).
- Only handover to a cell that is active. You cannot fix cell congestion by
  handover if every cell is already at 100% PRB — in that case return [] or use
  slice_quota to prioritise the important slice.
- At most 3 actions per step.\
"""

_XAPP_PROMPT = ChatPromptTemplate.from_messages([
    # _XAPP_SYSTEM embeds literal JSON action examples ({"action":...}); LangChain's
    # f-string template would misread those braces as input variables and raise
    # KeyError on every decision, so escape them (the real vars live in the user msg).
    ("system", _XAPP_SYSTEM.replace("{", "{{").replace("}", "}}")),
    ("user",
     "GOAL: {goal}\n\n"
     "Recent actions you already took (do not repeat/undo):\n{recent}\n\n"
     "Current KPM report:\n{kpm}\n\n"
     "Return the JSON array of control actions for this step."),
])
_XAPP_CHAIN = _XAPP_PROMPT | _text_llm


def _cell_state(c: dict) -> str:
    if c.get("faulted"):
        return "FAULT"
    return "on" if c["active"] else "OFF"


def _summarise_kpm(kpm: dict) -> str:
    """Compact KPM view for the xApp prompt — keeps tokens (and latency) low."""
    cells = "; ".join(
        f"{c['id']}({_cell_state(c)},PRB{c['prb_used_pct']}%,{c['connected_ues']}ue)"
        for c in kpm.get("cells", [])
    )
    ues = "; ".join(
        f"{u['id']}→{u['serving_cell']}(RSRP{u['rsrp_dbm']},{u['throughput_mbps']}Mbps)"
        for u in kpm.get("ue_groups", [])
    )
    # Tolerate BOTH slice shapes: the analytical KPM carries qos_score/prb_pct,
    # the real engine only carries throughput (its slices are {slice, throughput_
    # mbps}). Accessing the analytical-only keys with [] crashed the real-engine
    # AI loop every cycle, so it never fired a single control. Use .get().
    slices = "; ".join(
        f"{s['slice']}({s.get('throughput_mbps', 0)}Mbps"
        + (f",QoS{s['qos_score']}" if s.get('qos_score') is not None else "")
        + (f",cap{s['prb_pct']}%" if s.get('prb_pct') is not None else "") + ")"
        for s in kpm.get("slices", [])
        if (s.get("throughput_mbps") or 0) > 0 or (s.get("qos_score") or 0) > 0
    )
    # Surface any injected incident up top so the model reacts to it.
    anom = kpm.get("anomalies", {})
    faulted, surge = anom.get("faulted_cells", []), anom.get("demand_multiplier", 1)
    incident = ""
    if faulted or surge != 1:
        parts = []
        if faulted:
            parts.append(f"cells {faulted} FAULTED — offline and CANNOT be powered on; steer UEs to healthy cells")
        if surge != 1:
            parts.append(f"traffic surge ×{surge} (congestion — rebalance load or cap low-priority slices)")
        incident = "INCIDENT: " + "; ".join(parts) + "\n"
    return (
        f"{incident}"
        f"cells: {cells}\n"
        f"ue_groups: {ues}\n"
        f"slices: {slices}\n"
        f"control_state: {json.dumps(kpm.get('control_state', {}))}"
    )


def decide_xapp_actions(
    goal: str,
    kpm: dict,
    recent: list[dict] | None = None,
    llm_config: dict | None = None,
) -> list[dict]:
    """
    Ask an LLM, acting as an xApp, what control actions to take this step given
    the goal and current KPM. Returns a list of command dicts (possibly empty).
    Never raises — on any error returns [] so the control loop keeps running.

    llm_config lets the USER bring their own model (from the UI):
      {"model": "...", "base_url": "...", "api_key": "..."}
    Any OpenAI-compatible endpoint works (OpenAI, OpenRouter, a local Ollama /
    vLLM server, etc.). When omitted, the platform's built-in model is used.
    """
    if llm_config and llm_config.get("model") and llm_config.get("api_key"):
        llm = ChatOpenAI(
            model=llm_config["model"],
            temperature=0,
            max_tokens=1000,
            api_key=llm_config["api_key"],
            base_url=llm_config.get("base_url") or _BASE_URL,
        )
        chain = _XAPP_PROMPT | llm
    else:
        chain = _XAPP_CHAIN  # built-in model

    try:
        result = chain.invoke({
            "goal": goal,
            "recent": json.dumps(recent[-5:]) if recent else "(none)",
            "kpm": _summarise_kpm(kpm),
        })
        raw = result.content if hasattr(result, "content") else str(result)
        raw = raw.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw).strip()
        start, end = raw.find("["), raw.rfind("]")
        if start == -1 or end == -1:
            return []
        parsed = json.loads(raw[start : end + 1])
        return [c for c in parsed if isinstance(c, dict) and "action" in c][:3]
    except Exception:
        logger.exception("xApp decision failed")
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT CLASS  (thin wrapper for backwards-compatible call sites in main.py)
# ═══════════════════════════════════════════════════════════════════════════════

class NSGAgent:
    def modify_configuration(
        self,
        current_config: dict,
        user_prompt: str,
        history: list[dict] | None = None,
    ) -> NSGSimulationConfig:
        return modify_configuration(current_config, user_prompt, history)

    def generate_text(self, prompt: str) -> str:
        return generate_text(prompt)
