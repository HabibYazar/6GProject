#!/usr/bin/env python3
"""
NSG engine-service — HTTP API around the ns-O-RAN engine (FlexRIC + ns-3 RAN +
KPM xApp). This is the container's API surface: the NSG platform (on the 24.04
side) POSTs a run request, the service executes one real closed-loop E2 scenario
inside the container and returns a KPM summary.

Zero dependencies (Python 3.8 stdlib) so it runs in the runtime image as-is.

  GET  /health          -> {"status":"ok"}
  POST /run  {sim_time}  -> {e2_agents, kpm_indications, ue_count, measurements, status}

`measurements` are the REAL per-UE E2SM-KPM v3 values decoded by the patched
xApp: DRB.UEThpDl (throughput kbps), RRU.PrbUsedDl (PRB), L3servingSINR (dB),
DRB.PdcpSdu{BitRate,Delay,Volume}Dl. Each is summarised as count/min/max/mean
over the run.
"""
# The engine distro is Ubuntu 20.04, i.e. PYTHON 3.8 — `int | None` in an
# annotation is a 3.10 feature and raises TypeError at import there, which
# takes the whole service down on start. This makes annotations lazy so the
# syntax is legal whatever the platform side happens to be running.
from __future__ import annotations

import json
import os
import re
import threading
import time
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT = "/root/nsoran/run_scenario.sh"

# Config knobs the scenario exposes as ns-3 GlobalValues; forwarded to
# run_scenario.sh as env (which passes them as --Name=Value to the binary).
_PARAM_ENV = {
    "n_gnb": "NSG_N_GNB", "n_ue": "NSG_N_UE", "freq_hz": "NSG_FREQ_HZ",
    "bw_hz": "NSG_BW_HZ", "isd_cell": "NSG_ISD_CELL", "isd_ue": "NSG_ISD_UE",
    "gnb_positions": "NSG_GNB_POS", "ue_positions": "NSG_UE_POS",  # Level-2 exact layout
    "gnb_powers": "NSG_GNB_POW",     # Level-3: per-gNB TX power (dBm), 0 = sleep
    "buildings": "NSG_BUILDINGS",    # Level-4: real buildings -> real LOS/NLOS
    # Bursty external interference — the cells a PARTITIONED run cannot see.
    # Both paths run the same binary, so both accept it; a key that reaches one
    # and not the other is a bug this repo has already shipped once.
    "burst_dbm": "NSG_BURST_DBM",
    "burst_load": "NSG_BURST_LOAD",
    "ue_slices": "NSG_UE_SLICES",    # Level-5: per-UE 5QI + DL demand
    # per-slice DL symbol policy 'qci:max[:min[:dedicated]];...' in percent.
    # The two-field 'qci:pct' form still means a ceiling only.
    "slice_quotas": "NSG_SLICE_QUOTAS",
    "ho_sinr_db": "NSG_HO_SINR",     # MRO: SINR margin a neighbour must beat (dB)
    "ue_speeds": "NSG_UE_SPEED",     # MRO: per-UE walk speeds (m/s), ';'-sep
    # Phase 2b mobility: per-UE waypoint paths + the 3GPP measurement-event layer
    "ue_waypoints": "NSG_UE_WAYPOINTS",  # ';'-sep per UE, '|'-sep 'x,y,z,t'
    "ho_check_ms": "NSG_HO_CHECK_MS",    # RRC handover re-evaluation period (0=once)
    "ho_a3_entry": "NSG_HO_A3",          # require the A3 entry condition (1/0)
    "ho_hyst_db": "NSG_HO_HYST",         # A3 hysteresis (dB)
    "ho_a2_db": "NSG_HO_A2",             # A2 threshold (dB), -1000 = off
    # The rest of LteEnbRrc: L3 measurement smoothing + connection timers
    "rsrp_filter_coeff": "NSG_RSRP_FILTER",   # L3 filter k for RSRP (TS 36.331)
    "rsrq_filter_coeff": "NSG_RSRQ_FILTER",
    "conn_req_timeout_ms": "NSG_CONN_REQ_MS",
    "conn_setup_timeout_ms": "NSG_CONN_SETUP_MS",
    "conn_rej_timeout_ms": "NSG_CONN_REJ_MS",
    "srs_periodicity": "NSG_SRS_PERIOD",      # TS 36.213 Table 8.2-1 value
    "sib_periodicity_ms": "NSG_SIB_MS",
    "default_tx_mode": "NSG_TX_MODE",
    "report_all_ue_meas": "NSG_REPORT_ALL_MEAS",
    # Anomaly injection: ';'-sep 'cell,start_s,duration_s,type,value'
    # (type 0 = degrade to `value` dBm, 1 = outage) + admission switches
    # Research escape hatch — raw '--ns3::Class::Attr=value' args, for probing a
    # candidate parameter before it earns a schema field. Engine-side only.
    "extra_args": "NSG_EXTRA_ARGS",
    "gnb_faults": "NSG_GNB_FAULTS",
    "admit_handover": "NSG_ADMIT_HO",
    # Phase 2c antenna: per-gNB panel + the array element pattern
    # 'azimuth_deg,tilt_deg,rows,cols[,polslant,hspace,vspace[,hbw,vbw,gain]]'
    "gnb_antenna": "NSG_GNB_ANTENNA",
    # isotropic (default) | 3gpp | cosine | parabolic | circular
    "antenna_element": "NSG_ANT_ELEMENT",
    # Phase 2c radio: scheduler / AMC / HARQ
    "scheduler": "NSG_SCHEDULER",        # ns-3 MAC scheduler TypeId
    "amc_model": "NSG_AMC",              # ErrorModel (default) | ShannonModel
    "amc_ber": "NSG_AMC_BER",            # target BER, ShannonModel only
    "harq_enabled": "NSG_HARQ",          # 1 (default) | 0
    "harq_processes": "NSG_HARQ_PROC",   # concurrent HARQ processes per UE
    # Fields that were in the schema from the start but hardcoded in the engine
    "prop_scenario": "NSG_PROP",         # umi (default) | uma | rma | inh
    "shadowing": "NSG_SHADOWING",        # 1 | 0 (engine hardcoded 0)
    "numerology": "NSG_NUMEROLOGY",      # 2 | 3 (all this engine defines)
    "rng_run": "NSG_RNG_RUN",            # reproducible seed
    "ind_period_s": "NSG_IND_PERIOD",    # KPM reporting cadence (s)
    # Mobility robustness / cell selection + the DynamicTtt curve
    "outage_db": "NSG_OUTAGE_DB",        # serving SINR floor -> LTE fallback
    "qrxlevmin": "NSG_QRXLEVMIN",        # min RSRP to camp (dBm)
    "ttt_min_ms": "NSG_TTT_MIN",         # DynamicTtt curve
    "ttt_max_ms": "NSG_TTT_MAX",
    "ttt_diff_min_db": "NSG_TTT_DIFF_MIN",
    "ttt_diff_max_db": "NSG_TTT_DIFF_MAX",
    # RU power profile (only sent when overridden)
    "ru_tx_a": "NSG_RU_TX_A",
    "ru_rx_a": "NSG_RU_RX_A",
    "ru_idle_a": "NSG_RU_IDLE_A",
    # PHY/MAC on the mmWave DATA PATH (the LTE namesakes would only touch the
    # idle anchor cell — all user traffic rides the mmWave gNBs)
    "ue_nf_db": "NSG_UE_NF", "gnb_nf_db": "NSG_GNB_NF",
    "ue_tx_dbm": "NSG_UE_TX", "ue_outage_db": "NSG_UE_OUTAGE",
    "ue_n310": "NSG_UE_N310", "cqi_period_ms": "NSG_CQI_PERIOD",
    "cqi_timer_ms": "NSG_CQI_TIMER", "sinr_period_us": "NSG_SINR_PERIOD",
    "ue_sinr_period_us": "NSG_UE_SINR_PERIOD", "sym_per_slot": "NSG_SYM_PER_SLOT",
    "ra_preambles": "NSG_RA_PREAMBLES", "ra_trans_max": "NSG_RA_TRANS_MAX",
    "ra_resp_window": "NSG_RA_RESP_WIN", "data_err_model": "NSG_DATA_ERR",
    "sched_dl_only": "NSG_DL_ONLY", "sched_ul_only": "NSG_UL_ONLY",
    "fixed_mcs_dl": "NSG_MCS_DL", "fixed_mcs_ul": "NSG_MCS_UL",
    # The PHY's per-transmission trace, a path to write it to. On BOTH maps
    # deliberately: `extra_args` is on the batch map only, and reaching for
    # it here is how a capability ends up existing on one path and not the
    # other — the failure this repo has already shipped once.
    "rx_trace": "NSG_RX_TRACE",
    # Channel realism (blockage is the characteristic mmWave impairment) + RLC
    "blockage": "NSG_BLOCKAGE", "blocker_speed": "NSG_BLOCKER_SPEED",
    "n_blockers": "NSG_N_BLOCKERS", "portrait_mode": "NSG_PORTRAIT",
    "v_scatt": "NSG_VSCATT", "o2i_loss": "NSG_O2I",
    "channel_update_ms": "NSG_CH_UPDATE_MS",
    "rlc_am": "NSG_RLC_AM", "rlc_buffer_mb": "NSG_RLC_BUF_MB",
    "rlc_reorder_ms": "NSG_RLC_REORDER", "rlc_poll_ms": "NSG_RLC_POLL",
    "rlc_status_ms": "NSG_RLC_STATUS",
    # Beamforming, control plane, and which KPM families the engine collects
    "beamforming": "NSG_BEAMFORMING", "ideal_rrc": "NSG_IDEAL_RRC",
    "pdsch_cqi": "NSG_PDSCH_CQI", "rrc_t300_ms": "NSG_T300",
    "kpm_du": "NSG_KPM_DU", "kpm_cu_up": "NSG_KPM_CUUP",
    "kpm_cu_cp": "NSG_KPM_CUCP", "kpm_reduced": "NSG_KPM_REDUCED",
    # Transport network (backhaul), building propagation detail, PHY timing,
    # and the SVD beamforming solver's cost
    "x2_delay_ms": "NSG_X2_DELAY", "x2_gbps": "NSG_X2_GBPS",
    "s1u_delay_ms": "NSG_S1U_DELAY", "s1u_gbps": "NSG_S1U_GBPS",
    "s1u_mtu": "NSG_S1U_MTU",
    "s1ap_delay_ms": "NSG_S1AP_DELAY", "s1ap_gbps": "NSG_S1AP_GBPS",
    "x2_mtu": "NSG_X2_MTU",
    # Link-level error model (one field drives both ns-3 attributes) + PHY detail
    "error_model_type": "NSG_ERR_MODEL",
    "gnb_transient_ns": "NSG_TRANSIENT", "gnb_noise_filter": "NSG_NOISE_FILTER",
    "ue_mac_sinr_us": "NSG_UE_MAC_SINR", "fixed_tti": "NSG_FIXED_TTI",
    "o2i_threshold": "NSG_O2I_TH", "o2i_low_loss": "NSG_O2I_LOW",
    "harq_dl_timeout": "NSG_HARQ_DL_TO", "tb_decode_us": "NSG_TB_DECODE",
    # How long the xApp collects KPM — and therefore how many SIMULATED seconds
    # the run covers, since the two are linear (~0.10 sim-s per wall-s at 3
    # cells / 6 UEs, measured 2026-09-13 across windows of 10-120 s). This is
    # the only way to get a batch run past a sub-second horizon, so a scenario
    # whose point is elapsed time sets it. Omit it and run_scenario.sh sizes it
    # from the UE count as before.
    "kpm_collect_s": "NSG_KPM_COLLECT_SECS",
    # Keep the core when this run aborts, for a deliberate hunt. Costs ~5 GB per
    # abort on the Windows side, which is why it is off by default — and why it
    # has to be reachable, since a core is the only thing that has ever cracked
    # a bug in this class.
    "keep_cores": "NSG_KEEP_CORES",
    # Not in the schema (UlSchedDelay>1 crashes), kept for direct engine use
    "ul_sched_delay": "NSG_UL_SCHED_DELAY",
    "svd_iters": "NSG_SVD_ITERS", "svd_tol": "NSG_SVD_TOL",
    "svd_cache": "NSG_SVD_CACHE",
    "ho_mode": "NSG_HO_MODE",        # HO trigger mode: FixedTtt/DynamicTtt/Threshold/NoAuto
    "ho_ttt_ms": "NSG_HO_TTT",       # HO time-to-trigger (ms), FixedTtt mode
    # Phase 2a energy: per-gNB RU sleep profile 'depth,idle_ms,wake_ms;...'
    # (depth 0=none 1=micro 2=light 3=deep) + the controller's thresholds and
    # the per-depth power draws.
    "gnb_sleep": "NSG_GNB_SLEEP",
    "sleep_wake_load_pct": "NSG_SLEEP_WAKE_PCT",
    "sleep_idle_duty_pct": "NSG_SLEEP_IDLE_DUTY",
    "sleep_wake_hold_ms": "NSG_SLEEP_HOLD_MS",
    "sleep_micro_a": "NSG_SLEEP_MICRO_A",
    "sleep_light_a": "NSG_SLEEP_LIGHT_A",
    "sleep_deep_a": "NSG_SLEEP_DEEP_A",
}
# Scalar knobs echoed back to the UI (positions omitted — long strings).
_ECHO_KEYS = ("n_gnb", "n_ue", "freq_hz", "bw_hz", "isd_cell", "isd_ue")


# DU-level KPM. The CU-UP/PDCP metrics arrive in the first indication, but these
# only exist once the RAN actually schedules — so their absence means the run
# never produced usable data even if some measurements came back.
_DU_KEYS = ("DRB.UEThpDl", "L3servingSINR", "RRU.PrbUsedDl")
# The headline this whole tier exists to measure, and what the Compare
# leaderboard ranks on. Gate "ok" on THIS, not on any-DU-metric: a run has been
# seen to report L3servingSINR while DRB.UEThpDl never arrived, which passed an
# any() check and still left the leaderboard blank.
_DU_REQUIRED = "DRB.UEThpDl"


# The explicit collection-window ceiling, matching run_scenario.sh. Kept as a
# name because three places clamp to it and a bare 14400 in each is how they
# drift apart.
KPM_COLLECT_MAX_S = 14400   # 4 hours


def _script_budget(params: dict) -> int:
    """
    Wall budget for one run_scenario.sh call. Its runtime is driven by the xApp
    window (XAPP_WINDOW=40+15/gNB there) plus a bounded (<=45 s) E2-setup wait —
    NOT by sim_time, which the script no longer uses. The old fixed sim_time+160
    therefore timed out at >=6 gNB while looking like an engine failure.
    """
    n_gnb = int(params.get("n_gnb") or 4)
    # + the KPM collection window run_scenario.sh sizes from the UE count
    # (N_UE*8/5, clamped 10..180). Without this term the budget kills the
    # script mid-collection on exactly the heavy scenarios the window exists
    # for, and the run comes back looking like an engine failure again.
    n_ue = int(params.get('n_ue') or 4)
    collect = max(10, min(180, n_ue * 8 // 5))
    # An explicit window overrides the auto sizing and may go far past it (up to
    # 4 hours), because the window IS the simulated horizon. The budget has to
    # follow it, or asking for a long-horizon run kills the script partway
    # through collection and reports it as an engine failure — which is exactly
    # the failure mode the window was introduced to remove. Overrunning is no
    # longer fatal (the result comes back `truncated` with what it collected),
    # but a budget that is too small still throws away time that was paid for.
    if params.get("kpm_collect_s") is not None:
        try:
            collect = max(10, min(KPM_COLLECT_MAX_S, int(params["kpm_collect_s"])))
        except (TypeError, ValueError):
            pass
    return 40 + n_gnb * 15 + collect + 100


# The three processes run_scenario.sh starts. `pkill -x` matches the exact
# process name, so this never touches the invoking shell — the script's own EXIT
# trap uses the same three names.
_SESSION_PROCS = ("nearRT-RIC", "xapp_kpm_rc", "ns3.42-scenario")


def _kill_session() -> None:
    """Do by hand what run_scenario.sh's EXIT trap would have done.

    `subprocess.run` kills a timed-out child with SIGKILL, so the trap never
    fires and ns-3 / the RIC / the xApp are orphaned. They inherit fd 9 — the
    flock the script took — which means leaving them alive WEDGES THE ENGINE:
    every later run reads [BUSY] from a session nobody is running.
    """
    for name in _SESSION_PROCS:
        try:
            subprocess.run(["pkill", "-9", "-x", name], check=False,
                           capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001 — cleanup must not raise
            pass


def _text(x) -> str:
    """Partial output off a TimeoutExpired, whatever type it arrives as.

    CPython attaches what it had read before the timeout, but whether that is
    str or bytes depends on where inside communicate() the clock ran out — so
    this is defensive on purpose rather than trusting text=True.
    """
    if x is None:
        return ""
    if isinstance(x, bytes):
        return x.decode("utf-8", "replace")
    return str(x)


def _read(path: str, tail_bytes: int | None = None) -> str:
    """Read a log, optionally only its last `tail_bytes`.

    The tail exists for SNAPSHOTS of a run still in flight. /tmp/scn0.log grows
    at roughly 50 KB/s, so a four-hour run leaves ~700 MB behind it and reading
    all of that on a timer would steal CPU from the single-threaded sim it is
    watching. Only use it where the answer comes from the LAST sample (energy,
    sleep state) — never for anything counted, which a tail would undercount.
    """
    try:
        with open(path, "rb") as f:
            if tail_bytes:
                f.seek(0, os.SEEK_END)
                # SEEK BACK unconditionally. Measuring the size leaves the handle
                # at EOF, so a file SMALLER than the tail — which is the common
                # case — read as empty, and the snapshot silently lost the entire
                # sim log while still looking like a valid result.
                start = max(0, f.tell() - tail_bytes)
                f.seek(start)
                if start:
                    f.readline()          # drop the partial first line
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _classify(measurements: dict, crash, truncated: bool = False) -> tuple:
    """
    Honest run status, plus WHAT ENDED THE RUN — which is a different question
    from whether the result is usable, and conflating them hid a real failure.

    "ok" REQUIRES the DU metrics the caller actually reads: a run that aborted
    early still delivers the first CU-UP/PDCP indication, and that used to be
    reported as a healthy run that merely answered "—" for throughput/SINR/PRB.

    A CRASH WITH DATA USED TO REPORT "ok". The old rule was `if crash and not
    du_ok`, so an ns-3 that died once DU metrics existed fell through to a clean
    pass with no mention of the crash. That was nearly harmless while runs were
    ninety seconds — a crash then is almost always at startup, before any DU
    metric. It stops being harmless now that a run can be hours: ns-3 dying at
    hour three leaves plenty of DU metrics behind it, and the caller would be
    told the run was fine.

    So "truncated" now means ENDED EARLY, whichever way — the wall budget ran out
    or ns-3 died — with usable data up to the point it reached. Deliberately not
    folded into "ok" (it is shorter than the scenario asked for, and nothing
    should read it as a clean pass) and not into "crashed" (the numbers are real
    and a caller can use them). `ended_by` says which cause it was.

    Returns (status, error, ended_by).
    """
    du_ok = _DU_REQUIRED in measurements
    ended_by = "budget" if truncated else ("crash" if crash else None)

    if du_ok and ended_by == "budget":
        return "truncated", (
            "the run overran its wall budget and was stopped; the KPM below is "
            "real but covers less simulated time than the scenario asked for — "
            "check energy.span_s, and raise settings.kpm_collect_s only if the "
            "engine can be given more wall clock"), ended_by
    if du_ok and ended_by == "crash":
        return "truncated", (
            "ns-3 DIED PART WAY THROUGH and the KPM below is what it had reached "
            "by then — real, but stopping wherever the crash fell rather than "
            "where the scenario ended. Check energy.span_s against what the "
            "scenario needed: " + crash.group(0).strip()[:160]), ended_by
    if ended_by == "crash":
        return "crashed", ("ns-3 aborted before the RAN scheduled anything "
                           "readable: " + crash.group(0).strip()[:160]), ended_by
    if ended_by == "budget":
        return "truncated", ("the run overran its wall budget before the RAN "
                             "scheduled anything readable — no DU metrics. Try a "
                             "smaller topology rather than a longer budget"), ended_by
    if not measurements:
        return "no_data", "engine returned no KPM measurements", ended_by
    if not du_ok:
        missing = ", ".join(k for k in _DU_KEYS if k not in measurements)
        return "partial", (f"the RAN never scheduled — missing {missing}. Usually "
                           "the UEs are clustered on one cell; spread them so every "
                           "cell carries traffic."), ended_by
    return "ok", None, ended_by


def _parse_logs(sim_time: int, params: dict, out: str,
                truncated: bool = False, live: bool = False) -> dict:
    """Build a result from whatever the run has written to disk so far.

    Split out of `_run_once` so that a run IN FLIGHT can be read the same way a
    finished one is — the snapshot thread calls this with `live=True` and an
    empty `out`, because the script's stdout does not exist until it exits.

    `live` only changes how much of the sim log is read: a snapshot tails it,
    a final parse reads all of it.
    """
    # Mutual exclusion: run_scenario.sh couldn't get the engine lock (a live
    # session is running). Surface it clearly instead of returning empty data.
    if "[BUSY]" in out:
        return {"engine": "ns-O-RAN (FlexRIC + ns-3.42 mmWave, real E2AP/SCTP)",
                "status": "busy",
                "error": "engine busy with a live session — stop it and retry"}

    agents = 0
    m = re.search(r"E2 agents connected: (\d+)", out)
    if m:
        agents = int(m.group(1))
    else:
        # The script prints that line ONCE, at the end. A snapshot of a run in
        # flight never has it, and neither does a timeout whose stdout did not
        # reach us — so count the handshakes in the RIC own log instead, which
        # is where the script counted them from.
        agents = len(re.findall(r"E2 SETUP-REQUEST", _read("/tmp/ric.log")))

    # ALWAYS read the xApp log in full, snapshot or not: the numbers taken from
    # it are COUNTS (indications, RIC CONTROL requests and acks, UE ids), and a
    # tail would undercount them silently. It is also the smaller of the two —
    # roughly 4.6 KB/s against the sim log's 50.
    xapp = _read("/tmp/xapp.log")

    # ns-3 aborting mid-run used to be invisible here: the xApp still delivered
    # the first (PDCP-only) indication, so `measurements` was non-empty and the
    # run was reported "ok" with every DU metric missing. Read the sim's own log.
    # The sim log IS tailed for snapshots. Everything taken from it is either
    # the LAST sample per cell (energy, sleep state), which a tail gives
    # correctly, or the crash line — and a crash ends the run, so the final
    # parse reads the whole file and cannot miss one.
    scn = _read("/tmp/scn0.log", tail_bytes=(8 << 20) if live else None)
    crash = re.search(
        r"^.*(NS_ASSERT failed|NS_FATAL|Segmentation fault|dumped core).*$",
        scn, re.M)

    indications = len(re.findall(r"KPM ind_msg latency", xapp))
    # Count UEs by IMSI, which suffixes every per-UE measurement name and is the
    # key the gNB itself uses. amf_ue_ngap_id is a CONNECTION id — it is reissued
    # on handover, so counting those reported 16 UEs for a 10-UE scenario and
    # inflated anything derived per UE.
    ue_ids = set(re.findall(r"UEID_(\d+)", xapp))
    # ...and that same reissue-on-handover behaviour makes the NGAP ids a free
    # mobility metric: distinct connection ids minus distinct UEs = handovers
    # OBSERVED in the sampled KPM (a lower bound on the RAN's total).
    ngap_ids = set(re.findall(r"amf_ue_ngap_id = (\d+)", xapp))
    handovers = max(0, len(ngap_ids) - len(ue_ids))

    # Collect every "name = value" measurement pair the xApp decoded (E2SM-KPM v3).
    pairs = []
    for line in xapp.splitlines():
        m = re.match(r"\s*([\w.:-]+)\s*=\s*(-?[\d.]+)\s*$", line)
        if m:
            pairs.append((m.group(1), float(m.group(2))))

    # Capture EVERY decoded KPM measurement, aggregated per CANONICAL name (strip
    # the per-UE / per-cell / _Filter suffixes so values collapse across UEs).
    # Exclude non-KPM metadata (UE ids, E2 indication latency, log noise). NOTE:
    # L1M.RS-SINR.Bin* and CARR.PDSCHMCSDist.Bin* are histogram BIN COUNTS, not
    # scalar dB/MCS — kept for completeness; the real serving SINR is L3servingSINR.
    _META = {"Address", "ID", "Nodes", "PORT", "nodes",
             "amf_ue_ngap_id", "latency", "ran_ue_id"}

    def _canon(nm):
        nm = re.sub(r"3gpp_cell_\d+_UEID_\d+", "", nm)  # L3servingSINR per cell/UE
        nm = nm.replace("_Filter", "")
        nm = re.sub(r"\.UEID$", "", nm)
        return nm

    samples = {}
    for n, v in pairs:
        c = _canon(n)
        if c and c not in _META:
            samples.setdefault(c, []).append(v)
    # RSRP comes from the SIM LOG, not the xApp, and that is deliberate.
    #
    # The mmWave module never computed RSRP at all — MmWaveUePhy fires its
    # ReportCurrentCellRsrpSinr trace as (imsi, sinr, sinr), so the RSRP slot
    # carried SINR and nothing consumed it. The engine now computes a real RSRP
    # in MmWaveEnbPhy::UpdateUeSinrEstimate, from the received PSD that loop
    # already holds, with the same arithmetic the LTE stack uses.
    #
    # It rides a [NSG-RSRP] line rather than an E2SM-KPM field because getting it
    # into KPM means a new field on the X2 message that carries per-UE SINR from
    # the mmWave gNB to the LTE anchor RRC, then a new trace, map and ASN.1 item
    # — five files through the chain that carries the product's headline KPI.
    # That is the right change and it is NOT this one: this was added the day
    # before a demo, and the cheap path gives the platform a real RSRP without
    # touching KPM reporting. An external xApp over E2 will not see it; the
    # platform, the UI and any comparison will. Promote it when there is room.
    # The pattern is anchored to END OF LINE on purpose. Several producers write
    # into this log concurrently and ns-3 does not serialise them, so a line can
    # be SPLICED — measured on the first run that emitted RSRP:
    #     [NSG-RSRP] t=   0.1568 cell=3 imsi=5 rsrp_dbm=44 01 20 00 00 63 ...
    #                <id>[NSG-RSRP] t=0 cell=4 imsi=6 rsrp_dbm=8</id>
    # An E2AP hex dump and an XML fragment landed mid-line, and a loose
    # `rsrp_dbm=(-?[\d.]+)` read "44" and "8" out of them. That put a +44 dBm
    # RSRP — above the transmit power, physically impossible — into the reported
    # max. Requiring the number to be the last thing on its line rejects both.
    rsrp, rsrp_rejected = [], 0
    for m in re.finditer(r"^[^\n]*\[NSG-RSRP\] t=\s*[\d.]+ cell=\d+ imsi=\d+ "
                         r"rsrp_dbm=(-?\d+(?:\.\d+)?)\s*$", scn, re.M):
        v = float(m.group(1))
        # Second guard, cheap and physical: RSRP is power per resource element,
        # always far below 0 dBm. Anything at or above 0 is a parse artefact.
        if -200.0 <= v < 0.0:
            rsrp.append(v)
        else:
            rsrp_rejected += 1
    if rsrp:
        samples["RSRP"] = rsrp
    if rsrp_rejected:
        print(f"[engine-service] dropped {rsrp_rejected} malformed RSRP sample(s)",
              flush=True)

    measurements = {
        name: {
            "count": len(v),
            "min": round(min(v), 2),
            "max": round(max(v), 2),
            "mean": round(sum(v) / len(v), 2),
        }
        for name, v in sorted(samples.items())
    }

    # Derived KPIs (the engine emits the ingredients, not the named metric).
    def _sum(pred):
        return sum(v for (n, v) in pairs if pred(n))
    tb_tot = _sum(lambda n: bool(re.match(r"TB\.TotNbrDl\.\d", n)))   # not ...Initial
    tb_err = _sum(lambda n: n.startswith("TB.ErrTotalNbrDl"))
    m_qpsk = _sum(lambda n: n.startswith("TB.TotNbrDlInitial.Qpsk"))
    m_16 = _sum(lambda n: n.startswith("TB.TotNbrDlInitial.16Qam"))
    m_64 = _sum(lambda n: n.startswith("TB.TotNbrDlInitial.64Qam"))
    m_tot = m_qpsk + m_16 + m_64
    derived = {
        "bler_pct": round(100.0 * tb_err / tb_tot, 2) if tb_tot > 0 else 0.0,
        "mod_64qam_pct": round(100.0 * m_64 / m_tot, 1) if m_tot > 0 else 0.0,
    }

    # MEASURED energy: the scenario runs a real per-gNB radio energy model
    # (MmWaveRadioEnergyModelEnb — state-based idle/ctrl/data/TX currents) and
    # logs cumulative Joules per cell. Parse the LAST line per cell. Until now
    # the platform's "power" was computed from the config; this is the sim's own
    # measurement, so cell sleeps / load changes show up in it.
    energy_cells = {}   # cell -> (t, joules): keep the latest sample
    for em in re.finditer(
            r"Total energy consumption for mmWave cell (\d+): ([\d.eE+-]+)J at time ([\d.]+)",
            scn):
        c, j, t = int(em.group(1)), float(em.group(2)), float(em.group(3))
        if c not in energy_cells or t >= energy_cells[c][0]:
            energy_cells[c] = (t, j)
    energy = None
    if energy_cells:
        span = max(t for (t, _) in energy_cells.values())
        total = sum(j for (_, j) in energy_cells.values())
        energy = {
            "per_cell_j": {str(c): round(j, 1) for c, (_, j) in sorted(energy_cells.items())},
            "total_j": round(total, 1),
            "span_s": round(span, 2),
            "mean_power_w": round(total / span, 1) if span > 0 else None,
            "source": "measured (ns-3 MmWaveRadioEnergyModelEnb)",
        }

    # RU sleep states (Phase 2a): the scenario's sleep controller prints a
    # summary line per cell every simulated second. Take the LAST one per cell —
    # it carries the duty cycle the energy saving actually came from, plus the
    # per-depth seconds the radio energy model itself accumulated.
    sleep_cells = {}
    for sm in re.finditer(
            r"\[NSG-SLEEP-SUM\] t=([\d.]+) cell=(\d+) depth=(\w+) state=(\w+) "
            r"sleeps=(\d+) wakes=(\d+) sleep_s=([\d.eE+-]+) sleep_pct=([\d.eE+-]+) "
            r"duty_pct=([\d.eE+-]+) ues=(\d+) micro_s=([\d.eE+-]+) light_s=([\d.eE+-]+) "
            r"deep_s=([\d.eE+-]+) off_s=([\d.eE+-]+)",
            scn):
        t, cell = float(sm.group(1)), int(sm.group(2))
        if cell in sleep_cells and t < sleep_cells[cell]["t"]:
            continue
        sleep_cells[cell] = {
            "t": t, "depth": sm.group(3), "state": sm.group(4),
            "sleeps": int(sm.group(5)), "wakes": int(sm.group(6)),
            "sleep_s": round(float(sm.group(7)), 2),
            "sleep_pct": round(float(sm.group(8)), 1),
            "duty_pct": round(float(sm.group(9)), 1),
            "ues": int(sm.group(10)),
            "micro_s": round(float(sm.group(11)), 2),
            "light_s": round(float(sm.group(12)), 2),
            "deep_s": round(float(sm.group(13)), 2),
            "off_s": round(float(sm.group(14)), 2),
        }
    # The transition log is the authoritative event record: a batch run only
    # advances a few tenths of a second of sim time, so a cell can descend AFTER
    # the last periodic summary and the summary alone would report it awake with
    # zero sleeps — while the energy it saved is already in the totals.
    # The log carries EVERY transition since t=0, so its counts stand on their
    # own — take the larger of the two rather than adding, which would double
    # count a transition that landed on the same timestep as a summary.
    events = {}
    for tm in re.finditer(
            r"\[NSG-SLEEP\] t=([\d.]+) cell=(\d+) (?:-> (\w+) sleep|(AWAKE|WAKING))", scn):
        t, cell = float(tm.group(1)), int(tm.group(2))
        ev = events.setdefault(cell, {"sleeps": 0, "wakes": 0, "t": -1.0})
        if tm.group(3):                       # descended into a sleep depth
            ev.update(t=t, state="asleep", depth=tm.group(3))
            ev["sleeps"] += 1
        elif tm.group(4) == "AWAKE":
            ev.update(t=t, state="awake")
            ev["wakes"] += 1
        else:
            ev.update(t=t, state="waking")
    for cell, ev in events.items():
        d = sleep_cells.get(cell)
        if d is None:
            continue
        d["sleeps"] = max(d["sleeps"], ev["sleeps"])
        d["wakes"] = max(d["wakes"], ev["wakes"])
        if ev["t"] >= d["t"]:                 # a transition after the last summary
            d["state"] = ev["state"]
            d["depth"] = ev.get("depth", d["depth"])

    sleep = None
    if sleep_cells:
        span = max(c["t"] for c in sleep_cells.values()) or 1.0
        slept = sum(c["sleep_s"] for c in sleep_cells.values())
        sleep = {
            "per_cell": {str(c): {k: v for k, v in d.items() if k != "t"}
                         for c, d in sorted(sleep_cells.items())},
            "cells_asleep": sum(1 for d in sleep_cells.values() if d["state"] == "asleep"),
            "sleep_events": sum(d["sleeps"] for d in sleep_cells.values()),
            "wake_events": sum(d["wakes"] for d in sleep_cells.values()),
            # Share of the whole cell-layer's airtime spent asleep — the single
            # number an energy-saving run is judged on.
            "layer_sleep_pct": round(100.0 * slept / (span * len(sleep_cells)), 1),
            "span_s": round(span, 2),
        }

    # RC closed loop (E2SM-RC, down-link control): the xApp sends real RIC
    # CONTROL-REQUESTs and the gNB applies them over real E2AP — prove the loop
    # by counting requests vs acks. Action is fixed (QoS flow mapping) for now.
    rc_req = len(re.findall(r"CONTROL-REQUEST tx", xapp))
    rc_ack = len(re.findall(r"Successfully received CONTROL-ACK", xapp))
    rc_control = {
        "requests_sent": rc_req,
        "acks_received": rc_ack,
        "action": "QoS flow mapping (E2SM-RC 7.6.2.1)",
        "closed_loop": rc_req > 0 and rc_ack >= rc_req,
    }

    status, error, ended_by = _classify(measurements, crash, truncated)

    return {
        "engine": "ns-O-RAN (FlexRIC + ns-3.42 mmWave, real E2AP/SCTP)",
        "sim_time": sim_time,
        "requested": {k: params.get(k) for k in _ECHO_KEYS},  # config-derived knobs
        "e2_agents": agents,          # gNBs that completed the E2 SETUP handshake
        "kpm_indications": indications,  # E2SM-KPM indication messages received
        "ue_count": len(ue_ids),
        "handovers": handovers,          # HOs observed in the sampled KPM (NGAP id churn)
        "measurements": measurements,    # real per-UE KPM values (E2SM-KPM v3)
        "derived": derived,              # BLER %, 64QAM share % (from TB counters)
        "energy": energy,                # MEASURED per-cell Joules (radio energy model)
        "sleep": sleep,                  # RU sleep-state duty cycle per cell (Phase 2a)
        "rc_control": rc_control,        # real E2SM-RC down-link control loop
        "status": status,
        "error": error,
        # Said out loud so a caller never has to infer it from a short span:
        # WHY the run ended, and the crash line itself when there was one. A
        # short span with no explanation is exactly what made a mid-run death
        # look like a healthy result.
        "truncated": truncated,
        "ended_by": ended_by,          # "budget" | "crash" | None (ran to the end)
        "crash_signature": crash.group(0).strip()[:200] if crash else None,
        # Told to the caller at the moment it matters: a rare crash just happened
        # and there IS (or is NOT) a core to read. Finding that out afterwards is
        # too late — the run that produced it is gone.
        "core_kept": bool(crash) and str(params.get("keep_cores") or "0") == "1",
        "budget_s": _script_budget(params),
    }


# ── Streaming persistence ────────────────────────────────────────────────────
# A batch run used to exist only as an HTTP response. Everything it collected
# lived in the engine's memory until the script exited, so the client hanging up,
# the backend restarting or the service being killed all cost the whole run — the
# logs were on disk the entire time and nobody was reading them. Tolerable at
# ninety seconds. Not tolerable now that a run can be four hours.
#
# So the run PUBLISHES ITSELF while it is still going: every snapshot interval,
# parse what is on disk and write the same result shape the caller would get, to
# a file, atomically. `GET /run/snapshot` then reads it back, in flight or after
# the fact, with no reference to the request that started it.
#
# What this does and does not survive: the HTTP client disconnecting, the backend
# restarting, the engine service being killed, and `docker restart` (the file is
# on the container's writable layer). NOT `docker rm` — durability past that
# needs a volume, which is a deployment decision rather than a code one.
# /tmp holds it inside the container's writable layer, which survives a restart
# but not `docker rm`. A NAMED VOLUME mounted here outlives the container
# entirely, and the code does not need to know which it got: with no volume this
# is an ordinary directory and behaviour is exactly what it was.
_STATE_DIR = os.environ.get("NSG_STATE_DIR", "/var/lib/nsg")
_SNAPSHOT_PATH = os.path.join(_STATE_DIR, "run_snapshot.json")
# Backs off deliberately. The sim log grows ~50 KB/s and ns-3 is single-threaded,
# so a fixed 30 s interval would have a four-hour run parsing ~700 MB 480 times
# and stealing CPU from the thing it is watching. 30 s -> 300 s gives ~20 parses
# over four hours while still catching an early crash quickly.
_SNAPSHOT_FIRST_S = 30.0
_SNAPSHOT_MAX_S = 300.0


def _write_snapshot(obj: dict) -> None:
    """Publish the current view of a run, atomically.

    Write-then-rename so a reader never sees a half-written file: whoever reads
    /run/snapshot while this is running gets either the previous complete
    snapshot or this one, never a fragment.
    """
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        tmp = _SNAPSHOT_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, _SNAPSHOT_PATH)
    except Exception:  # noqa: BLE001 — a failed snapshot must never fail the run
        pass


def _snapshot_loop(sim_time: int, params: dict, stop: threading.Event) -> None:
    """Publish the in-flight run until `stop` is set."""
    interval = _SNAPSHOT_FIRST_S
    while not stop.wait(interval):
        t0 = time.time()
        try:
            snap = _parse_logs(sim_time, params, "", truncated=False, live=True)
            snap["in_flight"] = True
            snap["run_id"] = _RUN_ID[0]
            snap["snapshot_age_s"] = round(time.time() - _RUN_STARTED[0], 1)
            # A snapshot of a running sim has no verdict yet — the run has not
            # ended, so "ok" or "crashed" would both be claims about a question
            # still open.
            snap["status"] = "running"
            snap["error"] = None
            snap["parse_s"] = round(time.time() - t0, 2)
            _write_snapshot(snap)
        except Exception:  # noqa: BLE001
            pass
        interval = min(_SNAPSHOT_MAX_S, interval * 1.5)


_RUN_STARTED = [0.0]
# WHOSE RUN THE SNAPSHOT DESCRIBES. The engine serves one run at a time (flock),
# but several callers can have requests in flight against it, and each of them
# polling /run/snapshot would otherwise read whichever run holds the lock and
# file it under their own id. A caller passes `run_id` and only persists a
# snapshot that carries it back.
_RUN_ID = [None]


def _run_once(sim_time: int, params: dict) -> dict:
    env = os.environ.copy()
    for src, dst in _PARAM_ENV.items():
        if params.get(src) is not None:
            env[dst] = str(params[src])

    _RUN_ID[0] = params.get("run_id")
    _RUN_STARTED[0] = time.time()
    stop = threading.Event()
    threading.Thread(target=_snapshot_loop, args=(sim_time, params, stop),
                     daemon=True).start()

    # A LONG RUN MUST NOT LOSE WHAT IT COLLECTED. The budget is sized from the
    # scenario, but a run can still overrun it — and the KPM the xApp already
    # delivered is sitting in /tmp/xapp.log either way. Returning 504 with no
    # body threw all of it away, which is survivable when a run is two minutes
    # and unacceptable once `kpm_collect_s` lets one be four hours.
    # So a timeout becomes `truncated`: kill the session, parse what is on disk,
    # and hand back the partial result with the horizon it reached.
    truncated = False
    try:
        proc = subprocess.run(
            ["bash", SCRIPT, str(sim_time)],
            capture_output=True, text=True, timeout=_script_budget(params), env=env,
        )
        out = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        _kill_session()
        out = _text(exc.stdout) + _text(exc.stderr)
        truncated = True
    finally:
        stop.set()

    res = _parse_logs(sim_time, params, out, truncated)
    res["in_flight"] = False
    res["run_id"] = _RUN_ID[0]
    _write_snapshot(res)
    return res


def run_scenario(sim_time: int, params: dict = None) -> dict:
    params = params or {}
    res = _run_once(sim_time, params)
    # ns-3 has a rare startup race that aborts the sim in its first ~0.2 s. It is
    # not config-driven — identical params succeed on the next attempt — so retry
    # once instead of handing the caller a dead run. Never retry "busy": that is
    # a real live session holding the lock, and a retry would just wait again.
    # "truncated" is deliberately NOT in that list. A run that used its whole
    # budget will use it again, so retrying doubles the wall clock to reproduce
    # the same short result — and throws away the partial data we just salvaged.
    if res.get("status") in ("crashed", "no_data"):
        first = res.get("error")
        res = _run_once(sim_time, params)
        res["retried"] = True
        res["first_attempt_error"] = first
    return res


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok", "service": "nsg-engine"})
        elif self.path == "/run/snapshot":
            # The run as it stands, reachable WITHOUT the request that started
            # it. This is how a caller recovers data it would otherwise have
            # lost: its POST timed out, the backend restarted mid-run, someone
            # closed the connection. `in_flight` says whether the run that wrote
            # it has ended, and `status` is "running" while it has not.
            try:
                with open(_SNAPSHOT_PATH) as f:
                    self._send(200, json.load(f))
            except (OSError, ValueError):
                self._send(404, {"error": "no run snapshot yet"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/run":
            return self._send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        sim_time = int(body.get("sim_time", 20))
        params = {k: body.get(k) for k in _PARAM_ENV}
        # Not an engine knob — an identity the caller gives us so its snapshot
        # polling cannot pick up a different caller's run.
        params["run_id"] = body.get("run_id")
        try:
            self._send(200, run_scenario(sim_time, params))
        except subprocess.TimeoutExpired:
            # Should no longer be reachable for the RUN itself — _run_once now
            # catches its own timeout and returns a `truncated` result with the
            # KPM already on disk. Kept for the cleanup subprocesses, and so a
            # future caller of run_scenario cannot silently reintroduce the old
            # behaviour of discarding a long run's data.
            self._send(504, {"error": "engine run timed out before any result "
                                      "could be salvaged"})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": str(exc)})

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8090"))
    print(f"[engine-service] listening on :{port}", flush=True)
    # THREADING, because a run holds its request open for as long as the run
    # lasts. On the single-threaded HTTPServer that meant /run/snapshot could not
    # be served until the run it describes had already finished — the snapshot
    # was written to disk correctly and was unreachable exactly when it was
    # needed. Concurrency here is safe: run_scenario.sh takes an flock, so a
    # second /run gets a clean "[BUSY]" rather than two sims fighting.
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
