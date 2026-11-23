#!/usr/bin/env python3
"""
Parameter audit — what the engine accepts vs what we actually send.

Built after an audit found ~21 of 100 schema fields were rendered, validated,
accepted by the AI generator and then dropped before ns-3 ever saw them. Every
one of those was discovered by hand, months late, by reading scenario source.
That failure mode is what this turns into a test.

It works ENGINE-FIRST rather than config-first: instead of asking "VIAVI has X,
do we?", it asks ns-3 "what do you accept?" — `--PrintAttributes` and
`--PrintGlobals` on the scenario binary are the authoritative, machine-readable
answer — and diffs that against what the platform sends and what the schema
offers. Three lists come out:

  WIRED    schema field -> nsoran_params -> engine.        Working.
  INERT    schema field the config translator never reads. THE BUG CLASS.
  BACKLOG  engine attribute nothing drives.                The to-do list, sized.

Usage:
  python tools/param_audit.py                 # refresh the engine dump, report
  python tools/param_audit.py --cached        # reuse the last dump (fast)
  python tools/param_audit.py --backlog       # also print the full backlog
  python tools/param_audit.py --check         # exit 1 if any INERT field exists

The engine dump needs the scenario binary, which lives in the Ubuntu-2004-nsoran
distro; this reaches it over WSL interop and caches the result, so the report
itself runs anywhere.
"""
from __future__ import annotations

import argparse
import copy
import datetime as _dt
import json
import re
import textwrap
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "nsg_ai"))

CACHE = ROOT / "tools" / ".engine_params.json"
WSL = "/mnt/c/Windows/System32/wsl.exe"
NS3DIR = "/home/builder/ns-O-RAN-flexric/mmwave-LENA-oran"
BIN = f"{NS3DIR}/build/scratch/ns3.42-scenario-zero-with_parallel_loging-default"

# TypeIds whose attributes are RAN/scenario configuration a user could
# legitimately want to control. The binary carries ~984 TypeIds; the rest are
# ns-3 internals (sockets, queues, tracing) that are not product surface.
TYPEID_PREFIXES = (
    "ns3::Lte", "ns3::MmWave", "ns3::ThreeGpp", "ns3::UniformPlanarArray",
    "ns3::Building", "ns3::EpsBearer", "ns3::Epc",
)
TYPEID_SUFFIXES = ("MobilityModel", "AntennaModel", "PropagationLossModel",
                   "ChannelConditionModel")

# Schema fields that are REAL, but for the fast analytical tier rather than the
# ns-O-RAN engine. Each was checked against the engine source; the list itself
# now lives in schema.py with a per-field reason, because the config UI has to
# show the same fact to the user ("this knob only moves the analytical tier").
# One list, two consumers — a field wired later stops being tier-scoped in the
# audit and in the UI at the same moment. See engines/nsoran/inert-fields.patch.
from schema import TIER_SCOPED as _TIER_SCOPED_REASONS  # noqa: E402

TIER_SCOPED = set(_TIER_SCOPED_REASONS)
# Identity/labelling, not physics.
NON_PARAM = {
    "name", "description", "base_stations", "ue_groups", "buildings", "area",
    "settings", "base_stations[].id", "base_stations[].name",
    "base_stations[].position", "base_stations[].radio",
    "base_stations[].radio.band", "base_stations[].radio.antenna",
    "base_stations[].radio.antenna.massive_mimo", "base_stations[].radio.sleep",
    "ue_groups[].id", "ue_groups[].name", "ue_groups[].spawn_position",
    "ue_groups[].mobility", "ue_groups[].services", "ue_groups[].mobility.waypoints",
    "ue_groups[].mobility.waypoints[].position", "buildings[].id",
    "buildings[].bounds", "base_stations[].radio.band.label",
}


# ── engine side ──────────────────────────────────────────────────────────────
RED, DIM, RESET = "\033[31m", "\033[2m", "\033[0m"


def _wsl(cmd: str) -> str:
    """Run a command in the engine distro. Returns "" if it is unreachable."""
    try:
        r = subprocess.run([WSL, "-d", "Ubuntu-2004-nsoran", "-e", "bash", "-lc", cmd],
                           capture_output=True, text=True, timeout=600)
        return r.stdout.replace("\x00", "")
    except (OSError, subprocess.SubprocessError):
        return ""


def dump_engine() -> dict:
    """Every attribute and GlobalValue the scenario binary accepts."""
    run = f"cd {NS3DIR} && runuser -u builder -- bash -lc 'cd {NS3DIR} && LD_LIBRARY_PATH=build/lib {BIN}"
    tids = [t.strip() for t in _wsl(f"{run} --PrintTypeIds' 2>/dev/null").splitlines()
            if t.strip().startswith("ns3::")]
    keep = [t for t in tids
            if t.startswith(TYPEID_PREFIXES) or t.endswith(TYPEID_SUFFIXES)]
    if not keep:
        sys.exit("engine unreachable — start the Ubuntu-2004-nsoran distro, or use --cached")

    # One binary launch per TypeId, driven by a single-line bash loop over the
    # list. Two fancier approaches silently produced NOTHING and read as "the
    # engine exposes no attributes": inlining hundreds of commands into a nested
    # bash -lc string, and writing a heredoc script file across the distro
    # boundary. Keep the remote command boring.
    tid_list = " ".join(keep)
    out = _wsl(
        f"runuser -u builder -- bash -c 'cd {NS3DIR}; export LD_LIBRARY_PATH=build/lib; "
        f'for t in {tid_list}; do echo "@@$t"; ./build/scratch/'
        f"ns3.42-scenario-zero-with_parallel_loging-default --PrintAttributes=$t 2>/dev/null; done'")
    attrs: dict[str, list[str]] = {}
    cur = None
    for line in out.splitlines():
        if line.startswith("@@"):
            cur = line[2:].strip()
            attrs[cur] = []
        elif cur and line.startswith("    --ns3::"):
            attrs[cur].append(line.strip().split("=")[0].lstrip("-"))

    globals_ = [m.group(1) for m in re.finditer(
        r"^    --([A-Za-z_][A-Za-z0-9_]*)=", _wsl(f"{run} --PrintGlobals' 2>/dev/null"), re.M)]
    # WHICH CLASSES THE SCENARIO ACTUALLY BUILDS. Exposing an attribute on a
    # model nothing instantiates produces an INERT field — the exact bug class
    # this tool exists to prevent — and no amount of reading attribute names
    # tells you which is which. `BuildingsPropagationLossModel` is the trap that
    # caught me: buildings are a first-class feature, so its four wall-loss and
    # shadow-sigma attributes look like obvious things to expose, and the
    # scenario never creates one. Drawing a building switches the CONDITION
    # model (BuildingsChannelConditionModel); the path loss stays 3GPP.
    src = _wsl(f"cat {NS3DIR}/scratch/scenario-zero-with_parallel_loging.cc 2>/dev/null")
    # Every capitalised identifier the scenario mentions. Deliberately crude: a
    # false POSITIVE only leaves an attribute in the to-do list, while a false
    # negative would hide one, so err towards mentioning.
    referenced = sorted(set(re.findall(r"\b([A-Z][A-Za-z0-9]{3,})\b", src)))
    data = {"attributes": {k: v for k, v in attrs.items() if v},
            "globals": sorted(set(globals_)),
            "scenario_classes": referenced}
    CACHE.write_text(json.dumps(data, indent=1))
    return data


# Attributes the platform DOES drive, but not through a --ns3::X::Y argument, so
# scanning the run scripts cannot see them. Two mechanisms:
#   * a spec FILE the scenario parses and applies per object (per-gNB antenna
#     panels, buildings, per-UE speeds and waypoints), and
#   * a scenario GlobalValue the scenario reads and Config::SetDefault's — the
#     route forced on us whenever the scenario would otherwise overwrite a
#     command-line value after cmd.Parse.
# Without this map the backlog is overstated by ~40 attributes and the roadmap
# built on it aims at work already done.
DRIVEN_INDIRECTLY = {
    # gnbAntennaFile + --antennaElement -> per-device UniformPlanarArray factory
    "ns3::UniformPlanarArray::BearingAngle", "ns3::UniformPlanarArray::DowntiltAngle",
    "ns3::UniformPlanarArray::NumColumns", "ns3::UniformPlanarArray::NumRows",
    "ns3::UniformPlanarArray::PolSlantAngle",
    "ns3::UniformPlanarArray::AntennaHorizontalSpacing",
    "ns3::UniformPlanarArray::AntennaVerticalSpacing",
    "ns3::PhasedArrayModel::AntennaElement", "ns3::UniformPlanarArray::AntennaElement",
    # gnbAntennaFile -> the per-cell antenna ELEMENT the scenario builds. Same
    # route as UniformPlanarArray above and just as invisible to a script scan:
    # the scenario reads the file and calls SetAttribute itself
    # (scenario-zero-with_parallel_loging.cc, around the antenna element switch),
    # so these were listed as unwired while every one of them is driven by the
    # antenna block in the config.
    "ns3::CosineAntennaModel::HorizontalBeamwidth",
    "ns3::CosineAntennaModel::VerticalBeamwidth",
    "ns3::CosineAntennaModel::MaxGain",
    "ns3::ParabolicAntennaModel::Beamwidth",
    "ns3::CircularApertureAntennaModel::OperatingFrequency",
    "ns3::CircularApertureAntennaModel::AntennaCircularApertureRadius",
    "ns3::CircularApertureAntennaModel::AntennaMaxGainDb",
    # buildingsFile -> ns3::Building per object
    "ns3::Building::NFloors", "ns3::Building::NRoomsX", "ns3::Building::NRoomsY",
    "ns3::Building::Type", "ns3::Building::ExternalWallsType",
    # ueSpeedsFile / ueWaypointsFile -> per-UE mobility model
    "ns3::RandomWalk2dOutdoorMobilityModel::Speed",
    "ns3::RandomWalk2dMobilityModel::Speed",
    # scenario GlobalValues -> Config::SetDefault
    "ns3::MmWavePhyMacCommon::Bandwidth", "ns3::MmWavePhyMacCommon::CenterFreq",
    "ns3::MmWavePhyMacCommon::Numerology", "ns3::MmWavePhyMacCommon::NumHarqProcess",
    "ns3::MmWaveHelper::HarqEnabled", "ns3::MmWaveHelper::UseIdealRrc",
    "ns3::MmWaveFlexTtiMacScheduler::HarqEnabled",
    "ns3::LteEnbRrc::OutageThreshold",
    "ns3::MmWaveEnbNetDevice::E2Periodicity",
    "ns3::MmWaveEnbNetDevice::EnableCuCpReport",
    "ns3::MmWaveEnbNetDevice::EnableCuUpReport",
    "ns3::MmWaveEnbNetDevice::EnableDuReport",
    "ns3::MmWaveEnbNetDevice::ReducedPmValues",
    "ns3::MmWaveEnbMac::NumberOfRaPreambles",
    "ns3::ThreeGppChannelModel::UpdatePeriod",
    "ns3::LteRlcAm::MaxTxBufferSize", "ns3::LteRlcUm::MaxTxBufferSize",
    "ns3::ThreeGppPropagationLossModel::ShadowingEnabled",
}


def script_args() -> tuple[set[str], set[str]]:
    """(full attribute paths, bare scenario CLI names) the run scripts pass."""
    paths: set[str] = set()
    names: set[str] = set()
    for name in ("run_scenario.sh", "run_live_session.sh"):
        try:
            txt = (ROOT / "engines" / "nsoran" / name).read_text()
        except OSError:
            continue
        paths |= set(re.findall(r"--(ns3::[A-Za-z0-9_:]+)=", txt))
        names |= set(re.findall(r"--([a-zA-Z][A-Za-z0-9_]*)=", txt))
    return paths, names


def driven_by_scripts(all_attrs: list[str] | None = None) -> set[str]:
    """What the platform actually drives: run-script arguments, the
    file/GlobalValue routes the scripts cannot show, and the scenario's OWN CLI
    flags where they name an engine attribute.

    That last route was a blind spot, and it overstated the backlog. The scenario
    takes flags like `--hoSinrDifference` and applies them to an attribute
    (`ns3::LteEnbRrc::HoSinrDifference`) itself, so the flag is what the platform
    sends and the attribute never appears in any `--ns3::...=` we write. Matching
    a CLI flag to an attribute of the same name, case-insensitively, recovers
    those instead of listing them as work nobody has done.
    """
    paths, names = script_args()
    driven: set[str] = set(DRIVEN_INDIRECTLY) | paths | names
    if all_attrs:
        lower = {n.lower() for n in names}
        driven |= {a for a in all_attrs if a.split("::")[-1].lower() in lower}
    return driven


# ── Why an engine attribute is NOT product surface ───────────────────────────
# The raw backlog reads as "478 things nobody wired", and that number is honest
# about the count and misleading about the work: most of it is not a missing
# feature, and never will be. Each entry here is a DECLINE WITH A REASON, in the
# same spirit as TIER_SCOPED — the point is that what remains is an actual to-do
# list rather than a pile nobody has read.
BACKLOG_DECLINED: list[tuple[str, str, str]] = [
    ("lte-freq-reuse", "ns3::LteFfr|ns3::LteFr",
     "LTE fractional frequency reuse. The LTE cell here is an RRC/EPC anchor: "
     "the traffic the platform installs and every KPM it reads are on the mmWave "
     "cells, so a sub-band reuse scheme on the anchor has no path to a measured "
     "result."),
    ("dead-upstream", "ns3::MmWaveFlexTtiPfMacScheduler|"
                      "ns3::MmWaveFlexTtiMaxRateMacScheduler|"
                      "ns3::MmWaveFlexTtiMaxWeightMacScheduler",
     "The fork's alternative schedulers, MEASURED dead 2026-07-26: Pf and MaxRate "
     "run clean, report E2 and allocate nothing (0 PRB, 0 throughput); MaxWeight "
     "aborts ns-3. Exposing them would let a config produce a healthy-looking "
     "zero-throughput run (see schema.py)."),
    ("measured-declined",
     r"ns3::ThreeGppPropagationLossModel::EnforceParameterRanges|"
     r"ns3::LteRlcAm::EnableAQM",
     "Measured through extra_args and declined on the evidence. "
     "EnforceParameterRanges=true CRASHES the run (2026-09-14: status `crashed`, "
     "no KPM at all). EnableAQM=true is worse because it looks fine: measured "
     "2026-09-15 WITH the bearer actually in AM — its own condition — it returns "
     "`status: ok` with 43-45 KPM names and 0.0 Mbps, 0.0 PRB, twice out of "
     "twice, against 12.6 Mbps with it off. The tell is that `span_s` RISES from "
     "1.4 to 4.1-4.8 s: nothing is being scheduled, so the sim runs fast. Same "
     "rule as the dead schedulers — a config must not be able to produce a "
     "healthy-looking zero-throughput run."),
    ("trace-files", r"BearerStats|FileName|OutputFilename|TraceFile",
     "Paths for ns-3's own trace files. The platform reads KPM over real E2AP, "
     "not from trace output, so these change where a file nobody opens is "
     "written."),
    ("plumbing", r"::(E2[A-Z]|CellId$|BasicCellId$|BasicImsi$|Imsi$|UeMap$|"
                 r"mmWaveDevice$|mmWaveUeRrc$|Device$|Antenna$|Variables$|"
                 r"EpcUeNas$|LteUeRrc$|MmWaveEnbMac$|MmWaveEnbPhy$|"
                 r"MmWaveMacScheduler$|.*SpectrumPhy$|.*Map$|.*Manager$|"
                 r"ComponentCarrier.*$|PrimaryCarrier$|LocalAddress$|"
                 r"LocalPort$|RemoteServerAddress$|RemoteServerPort$|"
                 r"Csg(Id|Indication)$|(Dl|Ul)Earfcn$|Start(Time)?$|StopTime$)",
     "Object pointers, identities and wiring the helper assigns while building "
     "the scenario — not values a scenario author chooses."),
    ("lte-anchor", r"ns3::(LteEnbNetDevice|LteUeNetDevice|LteUePhy|LteEnbPhy|"
                   r"LteUePowerControl|LteUeRrc)::",
     "The LTE leg. In this deployment the LTE cell anchors RRC and the EPC "
     "bearer while the user plane and every KPM the product reads live on the "
     "mmWave cells, so these are exposable but would not move a measured "
     "result. Worth revisiting only if the LTE leg ever carries traffic we "
     "report on."),
    ("model-not-built", "ns3::Nakagami|ns3::ThreeLogDistance|ns3::ItuR1411|"
                        "ns3::HybridBuildings|ns3::Cost231|ns3::Okumura|"
                        "ns3::Kun2600|ns3::GaussMarkov|ns3::RandomWalk2d|"
                        "ns3::SteadyStateRandomWaypoint|ns3::RandomDirection|"
                        "ns3::Waypoint|ns3::LteHexGridEnbTopologyHelper",
     "Models the scenario never builds — checked against the scenario source, "
     "not guessed. It constructs 3GPP UMa/UMi/RMa/InH and drives mobility from "
     "waypoints and per-UE speeds. Exposing an attribute on a class nothing "
     "instantiates produces an INERT field, which is the bug class this tool "
     "exists to prevent: BuildingsPropagationLossModel is the trap — drawing a "
     "building switches the CONDITION model, while the path loss stays 3GPP, so "
     "its wall-loss and shadow-sigma attributes would be dead. Offering one of "
     "these models is a FEATURE decision, not an unwired knob."),
]


# Coherent FEATURES hiding in the backlog, as opposed to loose knobs. Counting
# these one attribute at a time is what makes the backlog look like hundreds of
# small omissions instead of a short list of things we have not built.
BACKLOG_FEATURES: list[tuple[str, str, str]] = [
    ("http-traffic", "ns3::ThreeGppHttp",
     "3GPP HTTP traffic model (client, server, and the full page/object size "
     "distribution). The platform installs UDP flows; this is web browsing with "
     "realistic page and object sizes and reading-time gaps. ONE feature, not "
     "one-per-attribute — and a real one for a RAN test product."),
]


def classify_backlog(missing: list[str],
                     scenario_classes: list[str] | None = None) -> dict[str, list[str]]:
    """Split the backlog into declined-with-a-reason and what is actually left.

    `scenario_classes` is what the scenario source mentions. An attribute on a
    class it never names cannot move a result no matter how sensible the
    attribute looks, so it is declined here rather than sitting in a to-do list
    waiting to become an INERT field.
    """
    out: dict[str, list[str]] = {k: [] for k, _, _ in BACKLOG_DECLINED}
    out.update({k: [] for k, _, _ in BACKLOG_FEATURES})
    out["model-not-built"] = out.get("model-not-built", [])
    out["real"] = []
    known = set(scenario_classes or [])
    for attr in missing:
        for key, pattern, _ in BACKLOG_FEATURES + BACKLOG_DECLINED:
            if re.search(pattern, attr):
                out[key].append(attr)
                break
        else:
            cls = attr.split("::")[-2] if "::" in attr else ""
            if known and cls not in known:
                out["model-not-built"].append(attr)
            else:
                out["real"].append(attr)
    return out


# ── platform side ────────────────────────────────────────────────────────────
def schema_fields() -> list[str]:
    from schema import NSGSimulationConfig  # noqa: E402
    s = NSGSimulationConfig.model_json_schema()
    defs = s.get("$defs", {})
    found: list[str] = []

    def walk(node, path, seen):
        ref = node.get("$ref")
        if ref:
            name = ref.split("/")[-1]
            if name not in seen:
                walk(defs[name], path, seen | {name})
            return
        for key in ("anyOf", "allOf", "oneOf"):
            if node.get(key):
                for sub in node[key]:
                    if sub.get("type") != "null":
                        walk(sub, path, seen)
                return
        if node.get("type") == "array":
            walk(node.get("items", {}), path + "[]", seen)
            return
        for name, sub in (node.get("properties") or {}).items():
            p = f"{path}.{name}" if path else name
            found.append(p)
            walk(sub, p, seen)

    walk(s, "", set())
    return found


def _probe_config() -> dict:
    """A config that exercises every branch of the translator: buildings, a
    massive-MIMO panel, sleep, waypoints, a multi-service group."""
    band = {"label": "n78", "frequency_mhz": 3500.0, "bandwidth_mhz": 50.0,
            "numerology": 2}
    return {
        "name": "probe", "description": "param audit probe",
        "base_stations": [
            {"id": f"b{i}", "name": f"c{i}",
             "position": {"x": x, "y": 0.0, "z": 25.0},
             "radio": {"band": dict(band), "tx_power_dbm": 44.0,
                       "antenna": {"type": "massive_mimo", "azimuth_deg": 30.0,
                                   "tilt_deg": 4.0, "gain_dbi": 17.0,
                                   "horizontal_beamwidth_deg": 65.0,
                                   "vertical_beamwidth_deg": 7.5,
                                   "massive_mimo": {"rows": 8, "cols": 8,
                                                    "beamforming": "analog",
                                                    "digital_azimuth_deg": 0.0,
                                                    "digital_tilt_deg": 0.0,
                                                    "polarization_deg": 0.0,
                                                    "h_spacing_wl": 0.5,
                                                    "v_spacing_wl": 0.5}},
                       "sleep": {"mode": "light", "idle_timer_ms": 200.0,
                                 "wake_delay_ms": 1.0}}}
            for i, x in enumerate((-400.0, 0.0, 400.0), start=1)],
        "ue_groups": [{
            "id": "g1", "name": "g1", "count": 6,
            "spawn_position": {"x": 0.0, "y": -200.0}, "height_m": 1.5,
            "mobility": {"pattern": "vehicle", "speed_mps": 12.0,
                         "waypoint_mode": "pingpong",
                         "waypoints": [{"position": {"x": -200.0, "y": -200.0, "z": 1.5},
                                        "speed_mps": 12.0},
                                       {"position": {"x": 200.0, "y": -200.0, "z": 1.5},
                                        "speed_mps": 12.0}]},
            "services": [{"service_type": "video_streaming", "slice_type": "eMBB",
                          "qos_id": 9, "target_throughput_gbps": 0.02,
                          "protocol": "udp", "call_distribution": "exponential",
                          "avg_call_duration_s": 10.0, "avg_idle_duration_s": 5.0}]},
           # A SECOND GROUP ON A SECOND 5QI. A slice policy that everyone is
           # inside partitions nothing — capping the only slice at 20% and at
           # 70% gave the identical number — so the probe could not measure
           # the quota fields however they were perturbed. Two classes is the
           # smallest scene in which a partition means anything.
           {"id": "u2", "name": "urllc", "count": 6,
            "spawn_position": {"x": 120.0, "y": 120.0, "z": 1.5},
            "mobility": {"pattern": "random_walk", "speed_mps": 3.0},
            "services": [{"service_type": "voice", "slice_type": "uRLLC",
                          "qos_id": 1, "target_throughput_gbps": 0.02,
                          "protocol": "udp"}]}],
        # A slice policy has to be present for its fields to be perturbable at
        # all; an empty list makes every ratio unreachable and the audit files
        # them "not covered" rather than measuring them.
        "slices": [{"qos_id": 9, "max_pct": 70.0, "min_pct": 20.0,
                    "dedicated_pct": 10.0},
                   # UNPROTECTED on purpose. A floor or a dedicated share
                   # is enforced by trimming the slices that have neither,
                   # so a policy where EVERY class is protected trims
                   # nothing — the engine's guard says so in as many words
                   # — and item 0's min_pct/dedicated_pct could not be
                   # measured against anything.
                   {"qos_id": 1, "max_pct": 30.0, "min_pct": 0.0,
                    "dedicated_pct": 0.0}],
        # Same reason as the slice policy: an empty anomaly list makes every
        # field under it unreachable, and the audit files them "not covered"
        # instead of measuring them.
        # The probe only ever perturbs item [0] of an array, so item 0 has to
        # carry every anomaly field that needs measuring. It can carry only ONE
        # magnitude (the schema rejects a magnitude on a type that cannot use
        # it), so `tx_power_dbm` rides on item 1 and the type-switch fixup in
        # measure_wired clears item 0's magnitude before changing its type.
        "anomalies": [{"type": "noise_rise", "cell_id": "b2",
                       "start_s": 0.1, "duration_s": 0.2,
                       "noise_rise_db": 25.0},
                      {"type": "radio_degradation", "cell_id": "b1",
                       "start_s": 0.1, "duration_s": 0.2,
                       "tx_power_dbm": 30.0}],
        "buildings": [{"id": "bl1",
                       "bounds": {"x_min": -120.0, "x_max": -60.0, "y_min": -120.0,
                                  "y_max": -60.0, "z_min": 0.0, "z_max": 30.0},
                       "building_type": "office", "wall_material": "concrete",
                       "floors": 3, "rooms_x": 2, "rooms_y": 2}],
        "area": {"x_min": -600.0, "x_max": 600.0, "y_min": -600.0, "y_max": 600.0,
                 "resolution_m": 40.0},
        "settings": {"duration_s": 60, "report_interval_ms": 500, "scenario": "UMi",
                     "shadowing_enabled": True, "fast_fading_enabled": False,
                     "random_seed": 7},
    }


# Fields whose valid values are a SET the audit cannot guess. Arithmetic
# candidates all fail validation for these, so the audit would file them
# "untested" — honest, but useless, and one step from the older bug where
# qos_id read as INERT because every perturbation was coerced back to 9.
#
# Only add a field here when its values are genuinely categorical. A numeric
# field that rejects the candidates is telling you something real about its
# range; do not paper over that.
FIELD_CANDIDATES: dict[str, list] = {
    # 5QI: a standardised catalogue, not a range. Several classes with
    # different GBR/non-GBR character so the perturbation is a real change.
    "ue_groups[].services[].qos_id": [1, 82, 65, 80],
    "slices[].qos_id": [1, 82, 65, 80],
    # str-Enums: the arithmetic and free-string candidates all fail validation,
    # which files a perfectly well-wired field as unmeasurable.
    "ue_groups[].services[].protocol": ["tcp", "udp"],
    "anomalies[].type": ["cell_outage", "handover_block", "noise_rise",
                         "radio_degradation"],
    "anomalies[].cell_id": ["b1", "b3"],
    "settings.antenna_element": ["cosine", "parabolic", "circular", "3gpp"],
    # A TS 36.213 table value, not a range — arithmetic candidates are all
    # invalid by design.
    "settings.srs_periodicity": [40, 80, 160, 20],
    "ue_groups[].services[].call_distribution": ["pareto", "constant",
                                                 "uniform", "exponential"],
    # FIELDS WHOSE DEFAULT SITS AT AN INERT POINT. An arithmetic
    # perturbation of 0.0, None or "none" lands somewhere just as inert, so
    # the probe reported "nothing reads it" about levers that had just been
    # measured working by hand. The candidate has to be a value that
    # actually engages the mechanism.
    "settings.ho_hysteresis_db": [12.0, 6.0],
    "settings.ho_a2_threshold_db": [-5.0, 5.0],
    "settings.q_rx_lev_min_dbm": [-60, -90],
    "base_stations[].radio.sleep.mode": ["deep", "light", "micro"],
    # An int with ge=1: a sub-MB candidate never validates, and a field
    # whose every candidate is rejected is reported as read by NOBODY —
    # which it briefly was here, including by the slow tier that plainly
    # sends it. 1 MB is the smallest legal value and the only one this
    # probe's traffic can fill.
    "settings.rlc_buffer_mb": [1, 2],
    # Same reason: the shipped defaults sit where the mechanism does nothing.
    "settings.handover_mode": ["NoAuto", "Threshold", "FixedTtt"],
    "settings.ho_check_period_ms": [5000, 2000],
    "settings.rsrp_filter_coefficient": [12, 9],
    # 10 Gbps is far above any cell here; a cap only shows when it binds.
    # 0.05 Gbps is 50 Mbps a cell, still above what this scene carries;
    # a ceiling only shows when it is below the floor it sits over.
    "settings.s1u_link_gbps": [0.01, 0.005],
    # `dedicated` and `min` OVERLAP rather than stack — the engine's own
    # comment — so a dedicated share under the floor is dominated by it and
    # changes nothing. Only a value above the probe's 20% floor can show.
    "slices[].dedicated_pct": [60.0, 40.0],
    # `sym_per_slot` is quantisation the scheduler applies ONLY under
    # fixed TTI, so it can only be measured with that on.
    "settings.sym_per_slot": [12, 2],
    # The outage threshold clamps to 10 dB and the probe's links are far
    # above it; only the sparse scene can show this at all.
    "settings.outage_threshold_db": [10.0, 8.0],
    "settings.ue_outage_threshold_db": [10.0, 8.0],
}

# Which array item a field is measured on. Defaults to [0]; listed here when a
# field cannot coexist with item 0's other values. An anomaly carries exactly
# ONE magnitude, so `noise_rise_db` and `tx_power_dbm` have to live on different
# items — and a field measured on an item the probe never touches is a field
# this audit cannot judge, which is the failure mode it exists to prevent.
ITEM_INDEX: dict[str, int] = {
    "anomalies[].tx_power_dbm": 1,
    # A sleep state only pays on a cell serving NOBODY, and cell 0 carries
    # UEs in every probe scene. Measured on the cell the moving scene leaves
    # empty, which is the only place the mechanism can show.
    "base_stations[].radio.sleep.mode": 2,
}


def _candidates(value) -> list:
    """Several different-but-plausible values, tried in order until one passes
    validation.

    A single alternative is not enough, and getting this wrong makes the audit
    LIE. Three ways it did:
      * None stayed None, so every Optional field looked inert;
      * +1 on an int vanished into a clamp or a unit conversion that rounds
        (q_rx_lev_min_dbm is halved into 2 dB steps, so -140 and -139 are the
        same engine value);
      * value*0.5-3.0 went negative and failed a ge=0 constraint, which the
        try/except then filed as "untested".
    So: jump far, offer several shapes, and let validation pick.
    """
    if isinstance(value, bool):
        return [not value]
    if value is None:
        # Optional knobs are usually numeric overrides ("null = engine default").
        return [50.0, 5, -20.0, {"deep": 30.0}, "3gpp"]
    if isinstance(value, int):
        return [value * 2 + 7, value // 2 - 5, value + 11, value - 11, value + 1]
    if isinstance(value, float):
        return [value * 2.5 + 9.0, value * 0.25, value + 17.0, value - 17.0,
                -value, 3.0, 0.9]
    if isinstance(value, str):
        swaps = {"eMBB": "uRLLC", "UMi": "RMa", "concrete": "wood",
                 "office": "residential", "light": "deep", "vehicle": "pedestrian",
                 "massive_mimo": "cosine", "analog": "digital", "n78": "n77",
                 "pingpong": "once", "video_streaming": "voice_call",
                 "isotropic": "3gpp", "error_model": "shannon",
                 "DynamicTtt": "FixedTtt", "residential": "office",
                 "uRLLC": "eMBB", "RMa": "UMa", "both": "dl_only",
                 "dl_only": "ul_only", "once": "repeat", "svd": "dft", "dft": "codebook", "lte_mi": "nr_ir_table2"}
        return [swaps.get(value, value + "_x"), "uRLLC", "RMa", "deep",
                "dl_only", "shannon", "3gpp"]
    if isinstance(value, dict):
        return [{**value, "deep": 33.0}, {"deep": 33.0}]
    if isinstance(value, list):
        return [value[:1] if len(value) > 1 else value + value]
    return []


def _set_path(cfg: dict, path: str, value) -> bool:
    """Set a dotted path, [] meaning 'the first element'. False if absent."""
    node = cfg
    idx = ITEM_INDEX.get(path, 0)
    parts = path.split(".")
    for p in parts[:-1]:
        arr = p.endswith("[]")
        key = p[:-2] if arr else p
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
        if arr:
            if not isinstance(node, list) or len(node) <= idx:
                return False
            node = node[idx]
    last = parts[-1]
    if last.endswith("[]") or not isinstance(node, dict) or last not in node:
        return False
    node[last] = value
    return True


def _get_path(cfg: dict, path: str):
    idx = ITEM_INDEX.get(path, 0)
    node = cfg
    for p in path.split("."):
        arr = p.endswith("[]")
        key = p[:-2] if arr else p
        if not isinstance(node, dict) or key not in node:
            return KeyError
        node = node[key]
        if arr:
            if not isinstance(node, list) or len(node) <= idx:
                return KeyError
            node = node[idx]
    return node


def _clear_conflicts(cfg: dict, field: str) -> None:
    """Drop values that the new candidate cannot legally sit beside.

    An anomaly's magnitude belongs to exactly ONE type and the schema
    rejects the mismatch, so switching `type` has to drop the old magnitude
    or every candidate is invalid and the field falls out of this audit's
    coverage entirely — which is how `type` went untested the moment that
    rule was added.

    This lived inline in `measure_wired` only, while `measure_consumers`
    claimed in its own docstring to use "the same perturbation machinery".
    It did not, so `anomalies[].type` was measured against the slow tier and
    reported inert against the fast one — from a candidate set that never
    validated rather than from anything measured.
    """
    if field == "settings.amc_target_ber":
        # Gated on `amc_model`, exactly as the field's own description says.
        # Measuring it under the default error model measures the gate, not
        # the field, and reports a wired lever inert.
        cfg.setdefault("settings", {})["amc_model"] = "shannon"
    if field == "settings.sym_per_slot":
        # The scheduler quantises per-flow symbols to this ONLY inside
        # `if (m_fixedTti)`. Measuring it with flex TTI on measures nothing
        # and reports the field inert, when what is inert is the pairing.
        cfg.setdefault("settings", {})["fixed_tti"] = True
    if field == "anomalies[].type" and cfg.get("anomalies"):
        for item in cfg["anomalies"]:
            for magnitude in ("tx_power_dbm", "noise_rise_db"):
                item.pop(magnitude, None)


def measure_wired(fields: list[str]) -> tuple[set[str], set[str], set[str]]:
    """Which schema fields actually change what the engine receives.

    Not a heuristic: perturb ONE field, re-run the real config translator, and
    diff the engine payload. A field whose change moves nothing is a field the
    engine will never see, whatever the form says. Fields the probe config does
    not carry are reported separately rather than silently counted as inert.
    """
    from schema import NSGSimulationConfig  # noqa: E402
    from main import nsoran_params          # noqa: E402

    # Round-trip the probe through the model so EVERY field is present with its
    # schema default. Perturbing the hand-written dict alone left 31 settings
    # fields untested purely because the probe did not happen to mention them —
    # an audit that quietly skips a third of the surface is worse than none.
    base_cfg = NSGSimulationConfig(**_probe_config()).model_dump(mode="json")
    base = nsoran_params(NSGSimulationConfig(**base_cfg))
    base.pop("_scene_center", None)

    wired, inert, untested = set(), set(), set()
    for f in fields:
        current = _get_path(base_cfg, f)
        if current is KeyError:
            untested.add(f)
            continue
        moved = False
        tried = False
        for alt in FIELD_CANDIDATES.get(f) or _candidates(current):
            if alt == current:
                continue
            cfg = json.loads(json.dumps(base_cfg))
            _clear_conflicts(cfg, f)
            if not _set_path(cfg, f, alt):
                break
            try:
                got = nsoran_params(NSGSimulationConfig(**cfg))
            except Exception:        # this candidate violates the field's rules
                continue
            tried = True
            got.pop("_scene_center", None)
            if got != base:
                moved = True
                break               # one demonstration is enough
        if moved:
            wired.add(f)
        elif tried:
            inert.add(f)            # accepted a different value, engine saw nothing
        else:
            untested.add(f)         # no candidate was ever valid — cannot judge
    return wired, inert, untested


# ── report ───────────────────────────────────────────────────────────────────
def measure_consumers(fields: list[str]) -> dict[str, set[str]]:
    """Which CONSUMER reads each field — not just whether ns-3 receives it.

    `measure_wired` answers one question: does this field change the ENGINE
    PAYLOAD. That was the whole question while there was one engine. There
    are now three consumers of a config and they read different subsets:

        slow      ns-3 / ns-O-RAN, via the payload (that is `measure_wired`)
        fast      the per-window tier — `ric_sim` live and `tier_b` batch
        coverage  the REM heatmap

    A field can reach the payload and nothing else, or reach the fast tier
    and never the payload (`slice_type` does), or reach none of the three
    while looking perfectly wired — four fields were deleted for exactly
    that. Measuring only the payload cannot tell those apart, and the UI
    badge that tells a user whether their change will do anything needs to.

    Same perturbation machinery as `measure_wired`, so the two cannot
    disagree about what a field's alternative value is.
    """
    from schema import NSGSimulationConfig          # noqa: E402
    from main import nsoran_params                  # noqa: E402

    def fast_of(cfg_dict: dict) -> str:
        import tier_b
        from ric_sim import RicSimulation
        cfg = NSGSimulationConfig(**cfg_dict)
        sim = RicSimulation(cfg, window_s=1.0)
        rep = None
        for _ in range(2):
            rep = sim.get_kpm_report()
            sim.tick()
        live = [(c["id"], c["throughput_mbps"], c["prb_used_pct"], c["power_w"])
                for c in rep["cells"]]
        live += [(u["imsi"], u["cell"], u["sinr"], u["rsrp"], u["thp_kbps"],
                  u.get("ul_kbps"), u["mcs"], u["slice"]) for u in rep["ues"]]
        live += [(x["slice"], x["throughput_mbps"], x["qos_score"])
                 for x in rep["slices"]]
        params = nsoran_params(cfg, engine_clamped=False)
        # THE DEMAND THE PRODUCT OFFERS. `frames()` defaults to an unlimited
        # one, under which nothing is ever left waiting — so a fingerprint
        # that omitted this could not queue, and every field about queueing
        # came back inert from a run no user ever makes. `_run_fast_tier`
        # builds the map this way; so must the thing that audits it.
        from nsoran_live import _rate_of_imsi_map
        demand = {i: m * 1e6 for i, m in _rate_of_imsi_map(cfg).items()}
        batch = [f["measurements"]
                 for f in tier_b.frames(params, 2, demand_bps=demand)]
        # A LONGER RUN, because half this tier's levers only exist in time.
        # A two-window fingerprint cannot see a handover threshold (nothing
        # has moved yet), a sleep state (the cell has not been idle long
        # enough), an RLC buffer (the queue has not built), or an anomaly
        # scheduled at t=4 — and every one of them came back "slow tier
        # only" from a probe that had just been measured moving them by
        # hand. What the probe cannot reach, it must not call inert.
        batch += [(f["sim_t"], f["handovers"],
                   [(c["cell"], c["power_w"], c["thp_kbps"]) for c in f["cells"]],
                   f["measurements"].get("DRB.BufferSize.Qos"))
                  for f in tier_b.frames(params, 8, demand_bps=demand)]
        return json.dumps([live, batch], sort_keys=True, default=str)

    def cov_of(cfg_dict: dict) -> str:
        from ns3_bridge import rsrp_heatmap
        pts = rsrp_heatmap(NSGSimulationConfig(**cfg_dict))
        # THE WHOLE GRID, sampled evenly, plus the count. A prefix never
        # reaches the far edge, which made `area.y_max` look inert while
        # `y_min` moved.
        step = max(1, len(pts) // 300)
        return json.dumps({"n": len(pts),
                           "p": [(round(q["x"], 1), round(q["y"], 1),
                                  round(q["rsrp"], 2)) for q in pts[::step]]},
                          sort_keys=True)

    # TWO PROBE SCENES, AND THE ANSWER IS THEIR UNION.
    #
    # One scene cannot decide this. The default probe is dense and
    # interference-limited, and on it a sixteen-dB change to either noise
    # figure moves the SINR by nothing — thermal noise is far below the
    # interference — so `ue_noise_figure_db` and `gnb_noise_figure_db` came
    # back "slow tier only" when both demonstrably reach the fast tier on a
    # sparser scene. A field that only bites in one regime is still a field
    # the tier reads, and reporting it as inert is how a working lever gets
    # deleted.
    #
    # So: the dense probe as-is, plus a SPARSE, LOW-POWER copy of it where
    # links are noise-limited. A field counts as read if it moves EITHER.
    dense = NSGSimulationConfig(**_probe_config()).model_dump(mode="json")
    sparse = copy.deepcopy(dense)
    for b in sparse.get("base_stations", []):
        b["position"]["x"] = b["position"]["x"] * 6.0
        b["position"]["y"] = b["position"]["y"] * 6.0
        b["radio"]["tx_power_dbm"] = 12.0
    for g in sparse.get("ue_groups", []):
        g["spawn_position"]["x"] = g["spawn_position"]["x"] * 6.0
        g["spawn_position"]["y"] = g["spawn_position"]["y"] * 6.0
    a = sparse.get("area") or {}
    for k, mul in (("x_min", 6.0), ("x_max", 6.0), ("y_min", 6.0), ("y_max", 6.0)):
        if k in a:
            a[k] = a[k] * mul
    # ...and a MOVING scene with finite demand. The other two are static and
    # offered an infinite load, which pins every cell at saturation: a UE
    # never crosses a boundary, so no handover threshold can bind, and a
    # queue never fills, so no buffer size can. Both regimes are ordinary
    # product use and neither was probed.
    # ...and a scene built for the levers that only exist IN TIME. The other
    # two are static, fully occupied and offered more than they can carry, so
    # a UE never crosses a boundary (no handover threshold can bind), no cell
    # is ever empty (no sleep state can pay) and the queue never builds. All
    # three are ordinary product use; none was probed, and every field about
    # them was filed "slow tier only" while measurably working by hand.
    moving = copy.deepcopy(dense)
    _bs = moving.get("base_stations") or []
    if len(_bs) >= 2:
        # Between the first two cells, so the walk crosses; the rest stay
        # empty, which is the only place a sleep state shows up.
        for g in moving.get("ue_groups", []):
            g["spawn_position"]["x"] = (_bs[0]["position"]["x"]
                                        + _bs[1]["position"]["x"]) / 2.0
            g["spawn_position"]["y"] = (_bs[0]["position"]["y"]
                                        + _bs[1]["position"]["y"]) / 2.0
    for g in moving.get("ue_groups", []):
        g.setdefault("mobility", {})["speed_mps"] = 25.0
        for svc in (g.get("services") or []):
            # OVER capacity, but FINITE. The model treats an unlimited
            # demand as "nothing is ever left waiting" — `wanted == inf`
            # short-circuits the queue to zero — so the default probe can
            # never fill an RLC buffer and `rlc_buffer_mb` came back inert
            # from a scene that offers infinite traffic. A number the cell
            # cannot serve is what makes a queue.
            svc["rate_mbps"] = 400.0
    scenes = [dense]
    for extra in (sparse, moving):
        try:
            NSGSimulationConfig(**extra)
            scenes.append(extra)
        except Exception:                            # noqa: BLE001
            pass
    refs = [(c, fast_of(c), cov_of(c)) for c in scenes]
    out: dict[str, set[str]] = {"fast": set(), "coverage": set(),
                                "untested": set()}
    for f in fields:
        if _get_path(scenes[0], f) is KeyError:
            out["untested"].add(f)
            continue
        hit_fast = hit_cov = False
        for base_cfg, ref_fast, ref_cov in refs:
            current = _get_path(base_cfg, f)
            if current is KeyError:
                continue
            for alt in FIELD_CANDIDATES.get(f) or _candidates(current):
                if alt == current:
                    continue
                cand = copy.deepcopy(base_cfg)
                _clear_conflicts(cand, f)
                if not _set_path(cand, f, alt):
                    continue
                try:
                    NSGSimulationConfig(**cand)
                except Exception:                    # noqa: BLE001
                    continue
                try:
                    hit_fast = hit_fast or fast_of(cand) != ref_fast
                    hit_cov = hit_cov or cov_of(cand) != ref_cov
                except Exception:                    # noqa: BLE001
                    continue
                if hit_fast and hit_cov:
                    break
            if hit_fast and hit_cov:
                break
        if hit_fast:
            out["fast"].add(f)
        if hit_cov:
            out["coverage"].add(f)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cached", action="store_true", help="reuse the last engine dump")
    ap.add_argument("--backlog", action="store_true", help="print every unwired attribute")
    ap.add_argument("--check", action="store_true", help="exit 1 if any INERT field exists")
    ap.add_argument("--consumers", action="store_true",
                    help="also measure the FAST TIER and the COVERAGE MAP, "
                         "and report which consumer reads each field")
    args = ap.parse_args()

    if args.cached and CACHE.exists():
        eng = json.loads(CACHE.read_text())
    else:
        print("enumerating the engine (one-off, ~1-2 min)...")
        eng = dump_engine()

    all_attrs = sorted({a for v in eng["attributes"].values() for a in v})
    driven = driven_by_scripts(all_attrs)
    driven_attrs = {a for a in all_attrs if a in driven}
    driven_globals = [g for g in eng["globals"] if g in driven]

    fields = [f for f in schema_fields() if f not in NON_PARAM]
    wired_s, inert_s, untested = measure_wired(fields)
    wired = sorted(wired_s)
    inert_real = sorted(f for f in inert_s if f not in TIER_SCOPED)
    tier = sorted(f for f in inert_s if f in TIER_SCOPED)

    print("\n" + "=" * 78)
    print("PARAMETER AUDIT — engine capability vs what the platform sends")
    print("=" * 78)
    print(f"\n  engine TypeIds inspected        {len(eng['attributes']):>5}")
    print(f"  engine attributes exposed       {len(all_attrs):>5}")
    print(f"  engine GlobalValues (scenario)  {len(eng['globals']):>5}")
    print(f"  ...of those, driven by us       {len(driven_attrs) + len(driven_globals):>5}"
          f"   ({len(driven_attrs)} attrs + {len(driven_globals)} globals)")
    print(f"\n  schema parameter fields         {len(fields):>5}")
    print(f"  ...changing the engine payload  {len(wired):>5}   WIRED (measured)")
    print(f"  ...tier-scoped by decision      {len(tier):>5}   (analytical tier, documented)")
    print(f"  ...changing nothing             {len(inert_real):>5}   INERT")
    if untested:
        print(f"  ...not covered by the probe     {len(untested):>5}   (add them to _probe_config)")
        for f in sorted(untested):
            print(f"        {f}")

    print("\n" + "-" * 78)
    if inert_real:
        print("INERT — in the config form, never reaches ns-3. This is the bug class:")
        for f in inert_real:
            print(f"    {f}")
    else:
        print("INERT — none. Every schema parameter reaches the config translator.")

    print("\n" + "-" * 78)
    missing = [a for a in all_attrs if a not in driven]
    groups = classify_backlog(missing, eng.get("scenario_classes"))
    real = groups.pop("real")
    print(f"BACKLOG — engine attributes nothing drives ({len(missing)}), of which:")
    for key, _pattern, reason in BACKLOG_FEATURES:
        got = groups.get(key) or []
        if not got:
            continue
        print(f"\n  {key:<16} {len(got):>4}   ONE FEATURE, not {len(got)} gaps")
        for line in textwrap.wrap(reason, 70):
            print(f"       {line}")
    for key, _pattern, reason in BACKLOG_DECLINED:
        got = groups.get(key) or []
        if not got:
            continue
        print(f"\n  {key:<16} {len(got):>4}   DECLINED")
        for line in textwrap.wrap(reason, 70):
            print(f"       {line}")
    print(f"\n  {'real backlog':<16} {len(real):>4}   things on models the scenario "
          f"actually runs")
    print("\n  The declined groups are not work nobody has done — they are decisions,"
          "\n  each with its reason above, the same way TIER_SCOPED records a field"
          "\n  the engine tier does not read. Only `real backlog` is a to-do list.")

    print("\n" + "-" * 78)
    print(f"REAL BACKLOG by class ({len(real)} attributes on models we run):")
    by_tid = {}
    for tid, attrs in eng["attributes"].items():
        # Dedupe against the DECLARING class: an inherited attribute is exposed
        # by every subclass, so listing per TypeId showed one knob a dozen times
        # and made the list look far longer than the count above.
        miss = [a for a in attrs if a in set(real)]
        if miss:
            by_tid[tid] = miss
    for tid, miss in sorted(by_tid.items(), key=lambda kv: -len(kv[1]))[:12]:
        shown = ", ".join(a.split("::")[-1] for a in miss[:6])
        more = f" (+{len(miss) - 6})" if len(miss) > 6 else ""
        print(f"    {tid.split('::')[-1]:<34} {len(miss):>3}  {shown}{more}")
    if args.backlog:
        print("\n  full backlog:")
        for tid, miss in sorted(by_tid.items()):
            for a in miss:
                print(f"    {a}")
    print("\n  (not all of these are product surface — this is the pool to pick from,")
    print("   not a target. Every one we decline should get a reason, like TIER_SCOPED.)")
    print("=" * 78)

    if args.consumers:
        print("\n" + "=" * 78)
        print("WHICH CONSUMER READS EACH FIELD")
        print("=" * 78)
        print("  measuring the fast tier and the coverage map …", flush=True)
        con = measure_consumers(fields)
        slow, fast, cov = wired_s, con["fast"], con["coverage"]
        rows = []
        for f in fields:
            if f in con["untested"]:
                continue
            who = [n for n, sset in (("slow", slow), ("fast", fast),
                                     ("coverage", cov)) if f in sset]
            rows.append((f, who))
        nobody = [f for f, w in rows if not w]
        print(f"\n  {'consumer set':28}{'fields':>8}")
        combos: dict[tuple, int] = {}
        for _f, w in rows:
            combos[tuple(w)] = combos.get(tuple(w), 0) + 1
        for combo, n in sorted(combos.items(), key=lambda kv: -kv[1]):
            label = " + ".join(combo) if combo else "NOBODY"
            col = RED if not combo else ""
            end = RESET if not combo else ""
            print(f"  {col}{label:28}{n:>8}{end}")
        if nobody:
            print(f"\n  {RED}READ BY NOTHING{RESET} — each is either a field to "
                  f"delete or a consumer to wire:")
            for f in nobody:
                why = _TIER_SCOPED_REASONS.get(f)
                print(f"    {f}" + (f"   {DIM}{why[:60]}…{RESET}" if why else ""))
        fast_only = [f for f, w in rows if w == ["fast"]]
        if fast_only:
            print(f"\n  {DIM}fast tier ONLY (the engine never sees these):{RESET}")
            for f in fast_only:
                print(f"    {f}")
        # PERSIST IT, so the answer is not only on someone's terminal.
        # `schema` stamps `x_reads` from this file, the config editor shows
        # it, and a diff of it is what tells you a change moved a field from
        # one consumer to another.
        out_path = Path(__file__).with_name("param_consumers.json")
        out_path.write_text(json.dumps({
            "measured_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "note": "which consumer reads each config field, measured by "
                    "perturbing it: slow = the ns-3 payload, fast = the "
                    "per-window tier, coverage = the REM heatmap",
            "fields": {f: w for f, w in rows},
        }, indent=1, sort_keys=True))
        print(f"\n  written to {out_path.name}")
        print("=" * 78)

    if args.check and inert_real:
        print("\nFAIL: inert schema fields present.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
