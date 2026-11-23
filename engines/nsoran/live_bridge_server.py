#!/usr/bin/env python3
"""
NSG Phase-1b: live-session bridge server (runs INSIDE the ns-O-RAN container).

One TCP client at a time (the NSG backend). On connect it starts a persistent
live session (RIC + ns-3 + the bridge xApp) and relays bidirectionally:

  bridge xApp stdout (KPM + [NSG-BRIDGE] markers) --> socket   (line-delimited)
  socket (control commands, line-delimited)       --> control FIFO --> xApp

So a customer's own xApp, connected to the backend over /ws/e2, drives REAL
E2SM-RC control on the live engine and sees real KPM stream back. On disconnect
or session exit the whole session process tree is torn down.

Zero dependencies (Python 3.8 stdlib), so it runs in the lean runtime image.
"""
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time

SCRIPT = "/root/nsoran/run_live_session.sh"
FIFO = "/tmp/nsg_live_ctrl.fifo"
PORT = int(os.environ.get("NSG_LIVE_PORT", "8091"))

# Scenario knobs the client may send as a first {"nsg_params": {...}} line, mapped
# to the env run_live_session.sh reads (so the live session runs the user's config).
_PARAM_ENV = {
    # HOW LONG THE SESSION MAY RUN. Without this the engine's own default (1200 s)
    # was the real ceiling on the live path and nothing on the platform side could
    # see or move it: a caller asking for a longer run got its session killed at
    # twenty minutes and read the result as the engine closing on it.
    "bridge_secs": "NSG_BRIDGE_SECS",
    "n_gnb": "NSG_N_GNB", "n_ue": "NSG_N_UE", "freq_hz": "NSG_FREQ_HZ",
    "bw_hz": "NSG_BW_HZ", "isd_cell": "NSG_ISD_CELL", "isd_ue": "NSG_ISD_UE",
    "gnb_positions": "NSG_GNB_POS", "ue_positions": "NSG_UE_POS", "gnb_powers": "NSG_GNB_POW",
    "buildings": "NSG_BUILDINGS",
    # Bursty external interference: the cells a PARTITIONED run cannot see.
    # Burstiness is the whole point — a constant term of the same mean was
    # measured across 40 dB and does not produce block errors at all (0.55%
    # against 9.72% bursty), because errors come from the TTIs where SINR dips
    # under the MCS threshold, not from a lower mean.
    "burst_dbm": "NSG_BURST_DBM",
    "burst_load": "NSG_BURST_LOAD",
    "ue_slices": "NSG_UE_SLICES",  # Level-5: per-UE 5QI + DL demand
    "slice_quotas": "NSG_SLICE_QUOTAS",  # per-slice DL symbol ceilings 'qci:pct;...'
    "ho_sinr_db": "NSG_HO_SINR",     # MRO: SINR margin a neighbour must beat (dB)
    "ho_mode": "NSG_HO_MODE",        # HO trigger mode: FixedTtt/DynamicTtt/Threshold/NoAuto
    "ho_ttt_ms": "NSG_HO_TTT",       # HO time-to-trigger (ms), FixedTtt mode
    "ue_speeds": "NSG_UE_SPEED",     # MRO: per-UE walk speeds (m/s), ';'-sep
    # Phase 2b mobility: per-UE waypoint paths + the 3GPP measurement-event layer
    "ue_waypoints": "NSG_UE_WAYPOINTS",  # ';'-sep per UE, '|'-sep 'x,y,z,t'
    "ho_check_ms": "NSG_HO_CHECK_MS",    # RRC handover re-evaluation period (0=once)
    "ho_a3_entry": "NSG_HO_A3",          # require the A3 entry condition (1/0)
    "ho_hyst_db": "NSG_HO_HYST",         # A3 hysteresis (dB)
    "ho_a2_db": "NSG_HO_A2",             # A2 threshold (dB), -1000 = off
    # The rest of LteEnbRrc. These reached the batch API and were dropped here,
    # so a RIC-test session silently ignored them — gnb_faults included, which
    # meant fault injection against the live engine did nothing at all while
    # reporting success. Both paths run the SAME scenario binary, so there was
    # never an engine reason for the gap. Guarded by tools/test_path_parity.py.
    "rsrp_filter_coeff": "NSG_RSRP_FILTER",   # L3 filter k for RSRP (TS 36.331)
    "rsrq_filter_coeff": "NSG_RSRQ_FILTER",
    "conn_req_timeout_ms": "NSG_CONN_REQ_MS",
    "conn_setup_timeout_ms": "NSG_CONN_SETUP_MS",
    "conn_rej_timeout_ms": "NSG_CONN_REJ_MS",
    "srs_periodicity": "NSG_SRS_PERIOD",      # TS 36.213 Table 8.2-1 value
    "sib_periodicity_ms": "NSG_SIB_MS",
    "default_tx_mode": "NSG_TX_MODE",
    "report_all_ue_meas": "NSG_REPORT_ALL_MEAS",
    "admit_handover": "NSG_ADMIT_HO",
    "gnb_faults": "NSG_GNB_FAULTS",           # per-cell fault schedule
    # Phase 2c antenna: per-gNB panel + the array element pattern
    "gnb_antenna": "NSG_GNB_ANTENNA",    # ';'-sep 'azimuth_deg,tilt_deg,rows,cols'
    "antenna_element": "NSG_ANT_ELEMENT",  # isotropic (default) | 3gpp
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
    # Not in the schema (UlSchedDelay>1 crashes), kept for direct engine use
    "ul_sched_delay": "NSG_UL_SCHED_DELAY",
    "svd_iters": "NSG_SVD_ITERS", "svd_tol": "NSG_SVD_TOL",
    "svd_cache": "NSG_SVD_CACHE",
    # Phase 2a energy: per-gNB RU sleep profile + controller thresholds + draws.
    "gnb_sleep": "NSG_GNB_SLEEP",
    "sleep_wake_load_pct": "NSG_SLEEP_WAKE_PCT",
    "sleep_idle_duty_pct": "NSG_SLEEP_IDLE_DUTY",
    "sleep_wake_hold_ms": "NSG_SLEEP_HOLD_MS",
    "sleep_micro_a": "NSG_SLEEP_MICRO_A",
    "sleep_light_a": "NSG_SLEEP_LIGHT_A",
    "sleep_deep_a": "NSG_SLEEP_DEEP_A",
}


# Two pumps write to one socket (xApp stdout + the energy tail), so sends are
# serialised — interleaved partial lines would corrupt the stream.
_send_lock = threading.Lock()


def _send(conn, line: str) -> bool:
    try:
        with _send_lock:
            conn.sendall(line.encode("utf-8", "replace"))
        return True
    except OSError:
        return False


# Measured per-cell energy in the ns-3 log (the radio energy model prints every
# 0.1 simulated seconds): forwarded to the client as compact [NSG-ENERGY] lines
# so live sessions can show MEASURED power instead of the config-derived guess.
_ENERGY_LOG_RE = re.compile(
    r"Total energy consumption for mmWave cell (\d+): ([\d.eE+-]+)J at time "
    r"([\d.]+), diff from last measurement is: ([\d.eE+-]+)J")
# Real UE positions from the same log (printed every 0.1 simulated seconds):
# forwarded as [NSG-POS] lines so the UI can animate the actual sim motion.
_POS_LOG_RE = re.compile(
    r"Position of UE with IMSI (\d+) is (-?[\d.]+):(-?[\d.]+):-?[\d.]+ at time "
    r"([\d.]+), UE connected to Cell: (\d+)")
# Phase 2a: RU sleep-state transitions and the per-cell duty summary, forwarded
# so a live session can show WHY a cell's measured power dropped (asleep at a
# depth) instead of only that it did.
_SLEEP_LOG_RE = re.compile(
    r"\[NSG-SLEEP-SUM\] t=([\d.]+) cell=(\d+) depth=(\w+) state=(\w+) "
    r"sleeps=(\d+) wakes=(\d+) sleep_s=([\d.eE+-]+) sleep_pct=([\d.eE+-]+)")
_CRASH_RE = re.compile(
    r"NS_FATAL|NS_ASSERT failed|malloc\(\)|free\(\)|corrupted|Segmentation fault")


def _ns3_alive() -> bool:
    try:
        return subprocess.run(
            ["pgrep", "-f", "scenario-zero-with_parallel_loging"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def tail_energy(conn, stop, streaming) -> None:
    path = "/tmp/scn0.log"
    # start at the current end: anything already there is a PREVIOUS session's
    # log (this session's ns-3 truncates the file when it starts, which the
    # size<pos check below detects as a rewind).
    try:
        pos = os.path.getsize(path)
    except OSError:
        pos = 0
    crash_seen = False      # a crash signature has appeared in the log
    crash_reported = False  # alarm the client at most once per session
    crash_line = ""        # the signature line itself, reported verbatim
    while not stop.is_set():
        try:
            if os.path.getsize(path) < pos:
                pos = 0  # truncated by this session's ns-3 start
                crash_seen = crash_reported = False  # new ns-3 (retry) — reset
                crash_line = ""
            with open(path) as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
            for m in _ENERGY_LOG_RE.finditer(chunk):
                line = (f"[NSG-ENERGY] cell={m.group(1)} j={m.group(2)} "
                        f"diff={m.group(4)} t={m.group(3)}\n")
                if not _send(conn, line):
                    return
            for m in _POS_LOG_RE.finditer(chunk):
                line = (f"[NSG-POS] imsi={m.group(1)} x={m.group(2)} y={m.group(3)} "
                        f"cell={m.group(5)} t={m.group(4)}\n")
                if not _send(conn, line):
                    return
            for m in _SLEEP_LOG_RE.finditer(chunk):
                line = (f"[NSG-SLEEP] cell={m.group(2)} depth={m.group(3)} "
                        f"state={m.group(4)} sleeps={m.group(5)} wakes={m.group(6)} "
                        f"sleep_s={m.group(7)} sleep_pct={m.group(8)} t={m.group(1)}\n")
                if not _send(conn, line):
                    return
            # ns-3 abort (heap corruption) AFTER the xApp began streaming. A
            # dozen-plus UEs on one cell corrupts the mmWave heap and it aborts
            # mid-run while encoding an indication; the xApp then just hangs (E2
            # peer gone), so nothing else surfaces a reason and the UI showed a
            # bare "session ended" / stuck power. `streaming` gates this to the
            # post-connect phase where NO startup-retry is possible (startup
            # crashes are handled by the script's own retry + [NSG-ERROR]).
            # NOTE: the aborting process is still alive while it dumps core, so
            # a crash signature and a dead ns-3 do NOT coincide — latch the
            # signature, then alarm on a later pass once ns-3 is truly gone.
            if streaming.is_set():
                m = _CRASH_RE.search(chunk)
                if m:
                    crash_seen = True
                    # Keep the line that matched. The signatures cover very
                    # different faults — NS_ASSERT is a violated model invariant,
                    # a segfault or heap corruption is something else entirely —
                    # and the old message attributed all of them to scenario
                    # density. That reading was wrong at least once in testing,
                    # on a three-cell nine-UE run, and it sends the operator to
                    # shrink a scenario that was never large.
                    crash_line = chunk[max(0, m.start() - 120):m.end() + 160]
                    crash_line = " ".join(crash_line.split())
            if crash_seen and not crash_reported and not _ns3_alive():
                crash_reported = True
                _send(conn, "[NSG-ERROR] ns-3 aborted mid-session. The engine "
                            "reported: " + (crash_line or "(no signature captured)") +
                            " — a very dense scenario is one cause, but so is a "
                            "parameter the model rejects; check the reported line "
                            "before changing the scenario.\n")
        except OSError:
            pass
        stop.wait(2.0)


def handle(conn) -> bool:
    """Serve one client. Returns True if a SESSION was actually started, so the
    accept loop can log honestly — "session ended" after a connection that never
    ran one is exactly the noise that made a wedged engine hard to read."""
    _send(conn, '{"type":"live_status","state":"starting"}\n')

    env = os.environ.copy()
    env["NSG_CTRL_FIFO"] = FIFO

    # First line MAY be {"nsg_params": {...}} — the user's scenario. Read it (short
    # timeout) and map to env. Bytes after that newline start the control stream.
    conn.settimeout(6.0)
    lead = b""
    try:
        while b"\n" not in lead:
            chunk = conn.recv(4096)
            if not chunk:
                break
            lead += chunk
    except (socket.timeout, OSError):
        pass
    # NOBODY IS THERE -> START NOTHING. A client that connected and closed without
    # sending a byte cannot receive a session, and starting one for it is not
    # harmless: it spawns ns-3 + the RIC + the xApp, takes /tmp/nsg_engine.lock,
    # and is then torn down the moment the first KPM line fails to send. The
    # platform's own liveness probe was a bare TCP connect, so EVERY health check
    # and every campaign acquire churned a full session through the engine —
    # measured 2026-09-09, three probes produced three "session ended" pairs in
    # the engine log — and repeatedly half-killing that process tree is the most
    # plausible cause of the wedged state where the bridge accepts connections
    # but every session dies instantly.
    if not lead:
        print("[live-bridge] client left before asking for anything — no session",
              flush=True)
        return False

    ctrl_leftover = lead
    if b"\n" in lead:
        first_line, rest = lead.split(b"\n", 1)
        try:
            msg = json.loads(first_line.decode("utf-8", "replace"))
            # An explicit liveness probe: answer and hang up. This is what makes
            # a health check able to prove the HANDLER is responsive — not merely
            # that the accept loop is — without paying for a session.
            if isinstance(msg, dict) and msg.get("nsg_probe"):
                _send(conn, '{"type":"probe_ok"}\n')
                return False
            prm = msg.get("nsg_params") if isinstance(msg, dict) else None
            if isinstance(prm, dict):
                for k, envk in _PARAM_ENV.items():
                    if prm.get(k) is not None:
                        env[envk] = str(prm[k])
                ctrl_leftover = rest  # params consumed; rest is control
                print(f"[live-bridge] scenario params: gNB={prm.get('n_gnb')} UE={prm.get('n_ue')}", flush=True)
        except Exception:
            pass  # not params — treat the whole lead as control below

    # OWN the FIFO: create it and hold it open BEFORE starting the session, so the
    # session script reuses this exact inode instead of rm/recreating it — a race
    # that silently dropped control (server wrote a dead inode, xApp read the new).
    # O_RDWR never blocks on a FIFO open; the server only ever writes, so the xApp
    # (the sole reader) receives every command.
    try:
        os.remove(FIFO)
    except OSError:
        pass
    os.mkfifo(FIFO)
    os.chmod(FIFO, 0o666)
    ctrl_fd = os.open(FIFO, os.O_RDWR)

    # own process group so we can kill the entire session tree on teardown.
    proc = subprocess.Popen(
        ["bash", SCRIPT], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env, start_new_session=True, bufsize=1, text=True,
    )
    stop = threading.Event()
    streaming = threading.Event()  # set once the xApp emits its first KPM

    def pump_kpm() -> None:
        # bridge xApp stdout -> socket, line by line (xApp fflushes each indication)
        try:
            for line in proc.stdout:
                if stop.is_set():
                    break
                if not streaming.is_set() and "KPM ind_msg" in line:
                    streaming.set()  # past startup: a mid-run abort is a real crash
                if not _send(conn, line):
                    break
        finally:
            stop.set()

    threading.Thread(target=pump_kpm, daemon=True).start()
    threading.Thread(target=tail_energy, args=(conn, stop, streaming), daemon=True).start()

    # socket -> FIFO (control commands from the external xApp, via the backend)
    try:
        conn.settimeout(1.0)
        buf = ctrl_leftover  # any bytes read past the params line are control
        while not stop.is_set():
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break  # client disconnected
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if ctrl_fd >= 0 and line.strip():
                    try:
                        os.write(ctrl_fd, line + b"\n")
                    except OSError:
                        pass
    finally:
        stop.set()
        if ctrl_fd >= 0:
            try:
                os.close(ctrl_fd)
            except OSError:
                pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.kill()
        except OSError:
            pass
        # Belt and braces: `timeout` used to move children into their own
        # process group (fixed with --foreground in the script, but a stray
        # survivor here kept the engine lock held for minutes). Kill the
        # session's known binaries by name — nothing else in this container
        # runs them while a live session holds the lock.
        for args in (["pkill", "-9", "-x", "nearRT-RIC"],
                     ["pkill", "-9", "-x", "xapp_kpm_rc"],
                     ["pkill", "-9", "-f", "scenario-zero-with_parallel_loging"]):
            try:
                subprocess.run(args, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            except OSError:
                pass
        try:
            conn.close()
        except OSError:
            pass
    return True   # a session really did run


def main() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))
    srv.listen(1)
    print(f"[live-bridge] listening on :{PORT}", flush=True)
    while True:
        conn, addr = srv.accept()
        print(f"[live-bridge] client {addr} connected", flush=True)
        ran = False
        try:
            ran = handle(conn)
        except Exception as exc:  # noqa: BLE001
            print(f"[live-bridge] session error: {exc}", flush=True)
        if ran:
            print("[live-bridge] session ended", flush=True)


if __name__ == "__main__":
    main()
