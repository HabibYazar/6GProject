#!/bin/bash
# NSG engine orchestrator (core of the containerised engine-worker).
# Runs FlexRIC + ns-3 RAN (E2 agent) + KPM xApp, prints the E2 handshake and
# KPM indication values, then cleans up. Bounded everywhere so it cannot hang.
# In a script file, pkill -f is safe: this bash's cmdline is "bash run_scenario.sh".
set -u
mkdir -p /tmp && chmod 1777 /tmp 2>/dev/null || true   # image excludes /tmp; ensure it exists
# WSL pipes core_pattern to a capture helper that writes the dump to the Windows
# side, where it does NOT count against the distro's own df. Each ns-3 abort cost
# ~5 GB there; ten of them quietly ate 31 GB of host disk. run_live_session.sh has
# had this since that was found — the batch path never did, which is the path
# campaigns and every sweep run on, so it produced nearly all of those dumps. The
# engine's crashes are diagnosed from its log, not from a core.
# NSG_KEEP_CORES=1 turns them back on for a HUNT. Off is the right default —
# each ns-3 abort writes ~5 GB to the Windows side and ten of them quietly ate
# 31 GB — but a core is not a nuisance, it is the tool: the density bug that
# survived memcheck, TSan and gdb was cracked by reading an ELF core post-mortem,
# and the e2sim subscription segfault that has never reproduced in 32 attempts
# has no other handle. Switching it off without leaving a way back on would have
# removed the only thing that has ever solved this class of bug.
if [ "${NSG_KEEP_CORES:-0}" = "1" ]; then
  ulimit -c unlimited 2>/dev/null || true
  echo "[cfg] core dumps KEPT (NSG_KEEP_CORES=1) — each abort costs ~5 GB on the Windows side"
  CORE_LIMIT=unlimited
else
  ulimit -c 0 2>/dev/null || true
  CORE_LIMIT=0
fi
# NSG mutual exclusion: only one engine session at a time — batch (this) AND the
# live bridge share the RIC on SCTP 36421. Acquire BEFORE the cleanup trap so a
# busy exit never pkills the other session's RIC. flock auto-releases on exit.
exec 9>/tmp/nsg_engine.lock
flock -n 9 || { echo "[BUSY] engine already running a session; try again shortly"; exit 3; }
RIC=/root/nsoran/flexric/build/examples/ric/nearRT-RIC
XAPP=/root/nsoran/flexric/build/examples/xApp/c/kpm_rc/xapp_kpm_rc
NS3DIR=/home/builder/ns-O-RAN-flexric/mmwave-LENA-oran
SIMTIME=${1:-${NSG_SIMTIME:-30}}

# Config-driven scenario knobs (env from engine_service; defaults = the original
# fixed scenario-zero, so a bare `bash run_scenario.sh` is unchanged). These map
# 1:1 to ns-3 GlobalValues the scenario already exposes. Area is fixed 4000x4000
# with the hub at (2000,2000), so the interside distances must stay < ~1900.
N_GNB=${NSG_N_GNB:-4}          # -> N_MmWaveEnbNodes (1 LTE hub + this many mmWave)
N_UE=${NSG_N_UE:-3}            # -> N_Ues
FREQ=${NSG_FREQ_HZ:-3.5e9}     # -> CenterFrequency (Hz)
BW=${NSG_BW_HZ:-20e6}          # -> Bandwidth (Hz)
ISD_CELL=${NSG_ISD_CELL:-1000} # -> IntersideDistanceCells (ring radius, m)
ISD_UE=${NSG_ISD_UE:-1000}     # -> IntersideDistanceUEs (UE disc radius, m)

# Level-2 exact positions: the ";"-separated "x,y" strings (scenario coords) are
# written to files and passed by PATH — avoids shell-quoting the ';'/','. Empty
# => the scenario keeps its auto ring/disc layout.
GNB_POS_ARG=""; UE_POS_ARG=""; GNB_POW_ARG=""; BLD_ARG=""; SLICE_ARG=""; QUOTA_ARG=""; SPEED_ARG=""
if [ -n "${NSG_GNB_POS:-}" ]; then printf '%s' "$NSG_GNB_POS" > /tmp/nsg_gnb_pos.txt; chmod 644 /tmp/nsg_gnb_pos.txt; GNB_POS_ARG="--gnbPositionsFile=/tmp/nsg_gnb_pos.txt"; fi
if [ -n "${NSG_UE_POS:-}"  ]; then printf '%s' "$NSG_UE_POS"  > /tmp/nsg_ue_pos.txt;  chmod 644 /tmp/nsg_ue_pos.txt;  UE_POS_ARG="--uePositionsFile=/tmp/nsg_ue_pos.txt";  fi
# Level-3: per-gNB TX power (dBm), ';'-separated; 0 = sleep that cell. Empty => defaults.
if [ -n "${NSG_GNB_POW:-}" ]; then printf '%s' "$NSG_GNB_POW" > /tmp/nsg_gnb_pow.txt; chmod 644 /tmp/nsg_gnb_pow.txt; GNB_POW_ARG="--gnbPowerFile=/tmp/nsg_gnb_pow.txt"; fi
# Level-4: real buildings -> real LOS/NLOS (BuildingsChannelConditionModel).
if [ -n "${NSG_BUILDINGS:-}" ]; then printf '%s' "$NSG_BUILDINGS" > /tmp/nsg_bld.txt; chmod 644 /tmp/nsg_bld.txt; BLD_ARG="--buildingsFile=/tmp/nsg_bld.txt"; fi
# Level-5: per-UE slice (5QI) + DL demand from the config.
if [ -n "${NSG_UE_SLICES:-}" ]; then printf '%s' "$NSG_UE_SLICES" > /tmp/nsg_slices.txt; chmod 644 /tmp/nsg_slices.txt; SLICE_ARG="--ueSlicesFile=/tmp/nsg_slices.txt"; fi
# NSG slicing: initial per-slice DL symbol ceilings 'qci:pct;...' (also settable live via E2SM-RC style 2).
if [ -n "${NSG_SLICE_QUOTAS:-}" ]; then printf '%s' "$NSG_SLICE_QUOTAS" > /tmp/nsg_quotas.txt; chmod 644 /tmp/nsg_quotas.txt; QUOTA_ARG="--sliceQuotasFile=/tmp/nsg_quotas.txt"; fi
# NSG MRO: per-UE speeds (m/s) — real mobility instead of the hardcoded 2-4 m/s walk.
if [ -n "${NSG_UE_SPEED:-}" ]; then printf '%s' "$NSG_UE_SPEED" > /tmp/nsg_speeds.txt; chmod 644 /tmp/nsg_speeds.txt; SPEED_ARG="--ueSpeedsFile=/tmp/nsg_speeds.txt"; fi
# NSG Phase 2b: per-UE waypoint paths + the 3GPP measurement-event handover
# layer. HoCheckPeriodMs is the load-bearing one — upstream disabled the RRC's
# periodic handover re-evaluation, so at 0 the RAN decides once at t=0 and
# mobility can never trigger a handover.
WP_ARG=""
if [ -n "${NSG_UE_WAYPOINTS:-}" ]; then printf '%s' "$NSG_UE_WAYPOINTS" > /tmp/nsg_waypoints.txt; chmod 644 /tmp/nsg_waypoints.txt; WP_ARG="--ueWaypointsFile=/tmp/nsg_waypoints.txt"; fi
HO_ARG="--ns3::LteEnbRrc::HoCheckPeriodMs=${NSG_HO_CHECK_MS:-0}"
HO_ARG="$HO_ARG --ns3::LteEnbRrc::HoA3Entry=$([ "${NSG_HO_A3:-0}" = "1" ] && echo true || echo false)"
HO_ARG="$HO_ARG --ns3::LteEnbRrc::HoHysteresisDb=${NSG_HO_HYST:-0}"
HO_ARG="$HO_ARG --ns3::LteEnbRrc::HoA2ThresholdDb=${NSG_HO_A2:--1000}"
# The rest of LteEnbRrc: L3 measurement smoothing (the INPUT to the events
# above, where hysteresis/TTT delay the decision), the connection-setup timers,
# and the signalling knobs. SrsPeriodicity is a TS 36.213 table value, not a
# free duration — the schema pins it to that set.
RRC_ARG="--ns3::LteEnbRrc::RsrpFilterCoefficient=${NSG_RSRP_FILTER:-4}"
RRC_ARG="$RRC_ARG --ns3::LteEnbRrc::RsrqFilterCoefficient=${NSG_RSRQ_FILTER:-4}"
RRC_ARG="$RRC_ARG --ns3::LteEnbRrc::ConnectionRequestTimeoutDuration=+${NSG_CONN_REQ_MS:-15}ms"
RRC_ARG="$RRC_ARG --ns3::LteEnbRrc::ConnectionSetupTimeoutDuration=+${NSG_CONN_SETUP_MS:-150}ms"
RRC_ARG="$RRC_ARG --ns3::LteEnbRrc::ConnectionRejectedTimeoutDuration=+${NSG_CONN_REJ_MS:-30}ms"
RRC_ARG="$RRC_ARG --ns3::LteEnbRrc::SrsPeriodicity=${NSG_SRS_PERIOD:-320}"
RRC_ARG="$RRC_ARG --ns3::LteEnbRrc::SystemInformationPeriodicity=+${NSG_SIB_MS:-5}ms"
RRC_ARG="$RRC_ARG --ns3::LteEnbRrc::DefaultTransmissionMode=${NSG_TX_MODE:-0}"
RRC_ARG="$RRC_ARG --ns3::LteEnbRrc::ReportAllUeMeas=$([ "${NSG_REPORT_ALL_MEAS:-1}" = "0" ] && echo false || echo true)"
# Anomaly injection. The admission switches are network-wide (LteEnbRrc holds
# them for the whole scene); per-cell faults ride the schedule file, where the
# power really changes mid-run rather than being annotated afterwards.
# NOT DRIVEN: AdmitRrcConnectionRequest. false blocks every attach and the
# scenario core-dumps during startup - see schema.py.
RRC_ARG="$RRC_ARG --ns3::LteEnbRrc::AdmitHandoverRequest=$([ "${NSG_ADMIT_HO:-1}" = "0" ] && echo false || echo true)"
# NSG research escape hatch: raw extra arguments appended to the scenario's
# command line. ns-3's CommandLine::Parse accepts --ns3::Class::Attribute=value
# for ANY registered attribute, so this is how a candidate parameter gets tried
# BEFORE it earns a schema field — measure first, expose second.
#
# Deliberately engine-side only. It is not a config field and must never become
# one: it is unvalidated pass-through, and a user-facing field that can hand
# arbitrary arguments to the simulator is a way to break a run with no error
# anyone can read.
# extra_args is an unquoted pass-through into the ns-3 command line — the one
# field that is by design raw. The port binding (loopback only) is the real
# control, since this is engine-side and never a config field; this is
# defence in depth. Reject the shell metacharacters that would turn a research
# argument into a command, and let a clean value through unchanged.
EXTRA_ARG="${NSG_EXTRA_ARGS:-}"
case "$EXTRA_ARG" in
  *[\;\&\|\`]*|*'$('*|*$'\n'*)
    echo "[run_scenario] refusing extra_args with shell metacharacters" >&2
    EXTRA_ARG="" ;;
esac
FAULT_ARG=""
if [ -n "${NSG_GNB_FAULTS:-}" ]; then printf '%s' "$NSG_GNB_FAULTS" > /tmp/nsg_faults.txt; chmod 644 /tmp/nsg_faults.txt; FAULT_ARG="--gnbFaultsFile=/tmp/nsg_faults.txt"; fi
# NSG Phase 2c: per-gNB antenna panel + the array element. Orientation is inert
# with the isotropic element (rotating isotropic elements cancels out) — the
# 3gpp element is what makes azimuth/tilt and sectorisation real.
ANT_ARG="--antennaElement=${NSG_ANT_ELEMENT:-isotropic}"
if [ -n "${NSG_GNB_ANTENNA:-}" ]; then printf '%s' "$NSG_GNB_ANTENNA" > /tmp/nsg_antenna.txt; chmod 644 /tmp/nsg_antenna.txt; ANT_ARG="$ANT_ARG --gnbAntennaFile=/tmp/nsg_antenna.txt"; fi
# NSG Phase 2c (radio): scheduler / AMC / HARQ. Scheduler and AMC ride the plain
# --ns3::X::Y route; HARQ must not, because the scenario Config::SetDefault's it
# AFTER cmd.Parse and would overwrite whatever the command line set — so those
# two go through GlobalValues the scenario reads back.
RADIO_ARG="--ns3::MmWaveHelper::Scheduler=${NSG_SCHEDULER:-ns3::MmWaveFlexTtiMacScheduler}"
RADIO_ARG="$RADIO_ARG --ns3::MmWaveAmc::AmcModel=${NSG_AMC:-ErrorModel}"
RADIO_ARG="$RADIO_ARG --ns3::MmWaveAmc::Ber=${NSG_AMC_BER:-0.00001}"
RADIO_ARG="$RADIO_ARG --harqEnabled=$([ "${NSG_HARQ:-1}" = "0" ] && echo false || echo true)"
RADIO_ARG="$RADIO_ARG --numHarqProcess=${NSG_HARQ_PROC:-100}"
# NSG: config fields the engine used to hardcode — propagation environment (was
# always UMi street canyon), shadowing (was always OFF while the schema
# defaulted it ON), numerology, and the RNG run that makes a config reproducible.
RADIO_ARG="$RADIO_ARG --propScenario=${NSG_PROP:-umi}"
RADIO_ARG="$RADIO_ARG --shadowingEnabled=$([ "${NSG_SHADOWING:-0}" = "1" ] && echo true || echo false)"
RADIO_ARG="$RADIO_ARG --nsgNumerology=${NSG_NUMEROLOGY:-2}"
RADIO_ARG="$RADIO_ARG --rngRun=${NSG_RNG_RUN:-1}"
# NSG: mobility robustness + cell selection + the DynamicTtt curve. DynamicTtt is
# the DEFAULT handover mode and none of its four parameters were reachable.
# outageThreshold rides the scenario GlobalValue, NOT the attribute: the
# scenario Config::SetDefault's ns3::LteEnbRrc::OutageThreshold AFTER
# cmd.Parse from that global, so an --ns3:: argument was silently
# overwritten and the field only LOOKED wired. Same trap as HARQ.
MRO_ARG="--outageThreshold=${NSG_OUTAGE_DB:--5}"
MRO_ARG="$MRO_ARG --ns3::LteEnbRrc::QRxLevMin=${NSG_QRXLEVMIN:--70}"
MRO_ARG="$MRO_ARG --ns3::LteEnbRrc::MinDynTttValue=${NSG_TTT_MIN:-25}"
MRO_ARG="$MRO_ARG --ns3::LteEnbRrc::MaxDynTttValue=${NSG_TTT_MAX:-150}"
MRO_ARG="$MRO_ARG --ns3::LteEnbRrc::MinDiffValue=${NSG_TTT_DIFF_MIN:-3}"
MRO_ARG="$MRO_ARG --ns3::LteEnbRrc::MaxDiffValue=${NSG_TTT_DIFF_MAX:-20}"
# RU power profile — only when the config overrides the engine's own model.
[ -n "${NSG_RU_TX_A:-}" ]   && MRO_ARG="$MRO_ARG --ns3::MmWaveRadioEnergyModelEnb::TxCurrentA=$NSG_RU_TX_A"
[ -n "${NSG_RU_RX_A:-}" ]   && MRO_ARG="$MRO_ARG --ns3::MmWaveRadioEnergyModelEnb::RxCurrentA=$NSG_RU_RX_A"
[ -n "${NSG_RU_IDLE_A:-}" ] && MRO_ARG="$MRO_ARG --ns3::MmWaveRadioEnergyModelEnb::DeepSleepA=$NSG_RU_IDLE_A"
# NSG: PHY/MAC on the mmWave DATA PATH. Deliberately the MmWave* classes, not
# their Lte* namesakes: every UE is dual-connected and all user traffic rides the
# mmWave gNBs, so LteUePhy/LteEnbPhy knobs would only touch the idle anchor cell.
PHY_ARG="--ns3::MmWaveUePhy::NoiseFigure=${NSG_UE_NF:-5}"
PHY_ARG="$PHY_ARG --ns3::MmWaveEnbPhy::NoiseFigure=${NSG_GNB_NF:-5}"
PHY_ARG="$PHY_ARG --ns3::MmWaveUePhy::TxPower=${NSG_UE_TX:-30}"
PHY_ARG="$PHY_ARG --ns3::MmWaveUePhy::OutageThreshold=${NSG_UE_OUTAGE:--5}"
PHY_ARG="$PHY_ARG --ns3::MmWaveUePhy::n310=${NSG_UE_N310:-2}"
PHY_ARG="$PHY_ARG --ns3::MmWaveUePhy::CqiReportPeriod=${NSG_CQI_PERIOD:-10}"
PHY_ARG="$PHY_ARG --ns3::MmWaveFlexTtiMacScheduler::CqiTimerThreshold=${NSG_CQI_TIMER:-100}"
# Feeds the handover evaluation loop: it can only be as fresh as the SINR estimate.
PHY_ARG="$PHY_ARG --ns3::MmWaveEnbPhy::UpdateSinrEstimatePeriod=${NSG_SINR_PERIOD:-1600}"
PHY_ARG="$PHY_ARG --ns3::MmWaveEnbPhy::UpdateUeSinrEstimatePeriod=${NSG_UE_SINR_PERIOD:-25.6}"
PHY_ARG="$PHY_ARG --ns3::MmWaveFlexTtiMacScheduler::SymPerSlot=${NSG_SYM_PER_SLOT:-6}"
# Random access. numberOfRaPreambles is a scenario GlobalValue, not an attribute:
# the scenario Config::SetDefault's that one AFTER cmd.Parse and would overwrite it.
PHY_ARG="$PHY_ARG --numberOfRaPreambles=${NSG_RA_PREAMBLES:-50}"
PHY_ARG="$PHY_ARG --ns3::MmWaveEnbMac::PreambleTransMax=${NSG_RA_TRANS_MAX:-50}"
PHY_ARG="$PHY_ARG --ns3::MmWaveEnbMac::RaResponseWindowSize=${NSG_RA_RESP_WIN:-3}"
PHY_ARG="$PHY_ARG --ns3::MmWaveSpectrumPhy::DataErrorModelEnabled=$([ "${NSG_DATA_ERR:-1}" = "0" ] && echo false || echo true)"
# Flexible-TTI derives the DL/UL symbol split from demand, so there is no frame
# pattern to set — restricting the scheduler is the real DL/UL control it offers.
PHY_ARG="$PHY_ARG --ns3::MmWaveFlexTtiMacScheduler::DlSchedOnly=$([ "${NSG_DL_ONLY:-0}" = "1" ] && echo true || echo false)"
PHY_ARG="$PHY_ARG --ns3::MmWaveFlexTtiMacScheduler::UlSchedOnly=$([ "${NSG_UL_ONLY:-0}" = "1" ] && echo true || echo false)"
# MCS pinning needs the flag AND the value; only when the config asks.
[ -n "${NSG_MCS_DL:-}" ] && PHY_ARG="$PHY_ARG --ns3::MmWaveFlexTtiMacScheduler::FixedMcsDl=true --ns3::MmWaveFlexTtiMacScheduler::McsDefaultDl=$NSG_MCS_DL"
[ -n "${NSG_MCS_UL:-}" ] && PHY_ARG="$PHY_ARG --ns3::MmWaveFlexTtiMacScheduler::FixedMcsUl=true --ns3::MmWaveFlexTtiMacScheduler::McsDefaultUl=$NSG_MCS_UL"
# NSG: the PHY's own per-transmission trace. `MmWavePhyTrace` writes one line
# per transport block with SINR(dB), the MCS the AMC chose, the TB size, the
# corrupt flag and the model's own TBler — the ground truth behind every
# aggregate the KPM report carries, and the only place the SINR appears
# unbinned (the L1M.RS-SINR counters are seven 6 dB buckets with two open
# ends). OFF by default: it is one line per block per UE, hundreds of
# thousands for a few seconds, and nothing in the product reads it.
if [ -n "${NSG_RX_TRACE:-}" ]; then
  PHY_ARG="$PHY_ARG --enableTraces=1 --ns3::MmWavePhyTrace::OutputFilename=$NSG_RX_TRACE"
fi
# NSG: channel realism. Blockage (TR 38.901) is the characteristic mmWave
# impairment - a hand or a passing body costs tens of dB - and ships OFF.
CH_ARG="--ns3::ThreeGppChannelModel::Blockage=$([ "${NSG_BLOCKAGE:-0}" = "1" ] && echo true || echo false)"
CH_ARG="$CH_ARG --ns3::ThreeGppChannelModel::BlockerSpeed=${NSG_BLOCKER_SPEED:-1}"
CH_ARG="$CH_ARG --ns3::ThreeGppChannelModel::NumNonselfBlocking=${NSG_N_BLOCKERS:-4}"
CH_ARG="$CH_ARG --ns3::ThreeGppChannelModel::PortraitMode=$([ "${NSG_PORTRAIT:-1}" = "0" ] && echo false || echo true)"
CH_ARG="$CH_ARG --ns3::ThreeGppChannelModel::vScatt=${NSG_VSCATT:-0}"
CH_ARG="$CH_ARG --ns3::ThreeGppPropagationLossModel::BuildingPenetrationLossesEnabled=$([ "${NSG_O2I:-1}" = "0" ] && echo false || echo true)"
# GlobalValues, not attributes: the scenario Config::SetDefault's the channel
# update period and the RLC buffer AFTER cmd.Parse and would overwrite them.
CH_ARG="$CH_ARG --channelUpdateMs=${NSG_CH_UPDATE_MS:-100}"
CH_ARG="$CH_ARG --bufferSize=${NSG_RLC_BUF_MB:-10}"
CH_ARG="$CH_ARG --ns3::MmWaveHelper::RlcAmEnabled=$([ "${NSG_RLC_AM:-0}" = "1" ] && echo true || echo false)"
CH_ARG="$CH_ARG --ns3::LteRlcUm::ReorderingTimer=+${NSG_RLC_REORDER:-1}ms"
CH_ARG="$CH_ARG --ns3::LteRlcAm::ReorderingTimer=+${NSG_RLC_REORDER:-1}ms"
CH_ARG="$CH_ARG --ns3::LteRlcAm::PollRetransmitTimer=+${NSG_RLC_POLL:-2}ms"
CH_ARG="$CH_ARG --ns3::LteRlcAm::StatusProhibitTimer=+${NSG_RLC_STATUS:-1}ms"
# NSG: beamforming, control plane, and which KPM families are collected.
# BeamformingModel/T300/PdschCqi are plain attributes; ideal RRC and the four
# KPM toggles ride GlobalValues because the scenario Config::SetDefault's them
# after cmd.Parse.
CP_ARG="--ns3::MmWaveHelper::BeamformingModel=${NSG_BEAMFORMING:-ns3::MmWaveSvdBeamforming}"
CP_ARG="$CP_ARG --ns3::MmWaveHelper::UsePdschForCqiGeneration=$([ "${NSG_PDSCH_CQI:-0}" = "1" ] && echo true || echo false)"
CP_ARG="$CP_ARG --ns3::LteUeRrc::T300=+${NSG_T300:-100}ms"
CP_ARG="$CP_ARG --useIdealRrc=$([ "${NSG_IDEAL_RRC:-1}" = "0" ] && echo false || echo true)"
CP_ARG="$CP_ARG --e2cuUp=$([ "${NSG_KPM_CUUP:-1}" = "0" ] && echo false || echo true)"
CP_ARG="$CP_ARG --e2cuCp=$([ "${NSG_KPM_CUCP:-1}" = "0" ] && echo false || echo true)"
# NSG: transport network. X2 carries handover signalling between gNBs, so its
# delay is part of the real handover latency - the engine's 1 ms default is a
# co-located ideal. S1-U delay adds to every packet's end-to-end latency.
NET_ARG="--ns3::MmWavePointToPointEpcHelper::X2LinkDelay=+${NSG_X2_DELAY:-1}ms"
NET_ARG="$NET_ARG --ns3::MmWavePointToPointEpcHelper::X2LinkDataRate=${NSG_X2_GBPS:-100}Gbps"
NET_ARG="$NET_ARG --ns3::MmWavePointToPointEpcHelper::S1uLinkDelay=+${NSG_S1U_DELAY:-1}ms"
NET_ARG="$NET_ARG --ns3::MmWavePointToPointEpcHelper::S1uLinkDataRate=${NSG_S1U_GBPS:-10}Gbps"
NET_ARG="$NET_ARG --ns3::MmWavePointToPointEpcHelper::S1uLinkMtu=${NSG_S1U_MTU:-2000}"
NET_ARG="$NET_ARG --ns3::MmWavePointToPointEpcHelper::S1apLinkDelay=+${NSG_S1AP_DELAY:-15}ms"
NET_ARG="$NET_ARG --ns3::MmWavePointToPointEpcHelper::S1apLinkDataRate=${NSG_S1AP_GBPS:-10}Gbps"
NET_ARG="$NET_ARG --ns3::MmWavePointToPointEpcHelper::X2LinkMtu=${NSG_X2_MTU:-10000}"
# Link-level error model. ONE platform field drives BOTH attributes: ns-3
# requires the AMC and the receiver to share an MCS table, and letting them
# disagree is a footgun the platform can just prevent. The default is an LTE
# mutual-information model on a 5G engine; the alternatives are 3GPP NR EESM.
NET_ARG="$NET_ARG --ns3::MmWaveSpectrumPhy::ErrorModelType=${NSG_ERR_MODEL:-ns3::MmWaveLteMiErrorModel}"
NET_ARG="$NET_ARG --ns3::MmWaveAmc::ErrorModelType=${NSG_ERR_MODEL:-ns3::MmWaveLteMiErrorModel}"
NET_ARG="$NET_ARG --ns3::MmWaveEnbPhy::Transient=${NSG_TRANSIENT:-320000}"
NET_ARG="$NET_ARG --ns3::MmWaveEnbPhy::NoiseAndFilter=$([ "${NSG_NOISE_FILTER:-0}" = "1" ] && echo true || echo false)"
NET_ARG="$NET_ARG --ns3::MmWaveUeMac::UpdateUeSinrEstimatePeriod=${NSG_UE_MAC_SINR:-1.6}"
NET_ARG="$NET_ARG --ns3::MmWaveFlexTtiMacScheduler::FixedTti=$([ "${NSG_FIXED_TTI:-0}" = "1" ] && echo true || echo false)"
NET_ARG="$NET_ARG --ns3::ThreeGppChannelConditionModel::O2iThreshold=${NSG_O2I_TH:-0}"
NET_ARG="$NET_ARG --ns3::ThreeGppChannelConditionModel::O2iLowLossThreshold=${NSG_O2I_LOW:-1}"
# PHY/MAC timing and the SVD solver cost. No BuildingsPropagationLossModel
# args: this scenario never instantiates that model (ThreeGpp* computes its
# own O2I loss). No UlSchedDelay: values >1 crash the engine.
NET_ARG="$NET_ARG --ns3::MmWavePhyMacCommon::HarqDlTimeout=${NSG_HARQ_DL_TO:-20}"
NET_ARG="$NET_ARG --ns3::MmWavePhyMacCommon::TbDecodeLatency=${NSG_TB_DECODE:-100}"
# UlSchedDelay is NOT a schema field (values >1 crash the engine); only sent
# when a caller asks the engine API for it directly.
[ -n "${NSG_UL_SCHED_DELAY:-}" ] && NET_ARG="$NET_ARG --ns3::MmWavePhyMacCommon::UlSchedDelay=$NSG_UL_SCHED_DELAY"
NET_ARG="$NET_ARG --ns3::MmWaveSvdBeamforming::MaxIterations=${NSG_SVD_ITERS:-30}"
NET_ARG="$NET_ARG --ns3::MmWaveSvdBeamforming::Tolerance=${NSG_SVD_TOL:-1e-08}"
NET_ARG="$NET_ARG --ns3::MmWaveSvdBeamforming::UseCache=$([ "${NSG_SVD_CACHE:-1}" = "0" ] && echo false || echo true)"
# Phase 2a: per-gNB RU sleep profile 'depth,idle_ms,wake_ms;...' (depth 0=none
# 1=micro 2=light 3=deep). Empty => no sleep management (cells stay up).
SLEEP_ARG=""
if [ -n "${NSG_GNB_SLEEP:-}" ]; then printf '%s' "$NSG_GNB_SLEEP" > /tmp/nsg_sleep.txt; chmod 644 /tmp/nsg_sleep.txt; SLEEP_ARG="--gnbSleepFile=/tmp/nsg_sleep.txt --sleepWakeLoadPct=${NSG_SLEEP_WAKE_PCT:-60} --sleepIdleDutyPct=${NSG_SLEEP_IDLE_DUTY:-0} --sleepWakeHoldMs=${NSG_SLEEP_HOLD_MS:-500}"; fi
# Per-depth power draws (Amperes) — the sleep profile's energy side. Only passed
# when the platform overrides the model's built-in defaults.
[ -n "${NSG_SLEEP_MICRO_A:-}" ] && SLEEP_ARG="$SLEEP_ARG --ns3::MmWaveRadioEnergyModelEnb::SleepMicroA=$NSG_SLEEP_MICRO_A"
[ -n "${NSG_SLEEP_LIGHT_A:-}" ] && SLEEP_ARG="$SLEEP_ARG --ns3::MmWaveRadioEnergyModelEnb::SleepLightA=$NSG_SLEEP_LIGHT_A"
[ -n "${NSG_SLEEP_DEEP_A:-}"  ] && SLEEP_ARG="$SLEEP_ARG --ns3::MmWaveRadioEnergyModelEnb::SleepDeepA=$NSG_SLEEP_DEEP_A"

cleanup() {
  # pkill -x matches the exact process name (comm), never the invoking bash —
  # so this is safe even if the caller command line mentions these names.
  pkill -9 -x nearRT-RIC 2>/dev/null
  pkill -9 -x xapp_kpm_rc 2>/dev/null
  pkill -9 -x ns3.42-scenario 2>/dev/null
}
trap cleanup EXIT
cleanup; sleep 1

# 1) near-RT RIC (line-buffered so its log flushes to file continuously)
stdbuf -oL -eL "$RIC" >/tmp/ric.log 2>&1 &
sleep 4
if grep -q 36421 /tmp/ric.log; then echo "[ok] RIC listening (SCTP 36421)"; else echo "[FAIL] RIC not up"; cat /tmp/ric.log; exit 1; fi

# 2) ns-3 RAN = E2 agent. Run the PREBUILT binary directly (not ./ns3 run, which
#    triggers a build-check that needlessly recompiles — and fails in a runtime-
#    only image). Bounded so it can't run forever.
# Bigger scenarios simulate MUCH slower in real time (mmWave gNBs are heavy), so
# scale the ns-3 bound and the xApp collection window with the gNB count — else
# ns-3 barely advances and the DU throughput/PRB/SINR reports get cut off (only
# CU-UP/PDCP survive and everything reads ~0).
# Collection window scales with gNB (bigger scenario = slower ns-3 = needs longer
# to reach steady-state DU reports). Validated across the 3-7 gNB envelope.
XAPP_WINDOW=$((40 + N_GNB * 15))
# ns-3 MUST outlive the xApp: if ns-3 reaches --simTime and exits before the xApp's
# teardown, the xApp's unsubscribe waits 5s on a now-dead E2 node -> SIGABRT (takes
# down the xApp AND the RIC). So give ns-3 a large sim time (it is killed by
# NS3_TIMEOUT well after the xApp anyway) and bound its WALL time to outlast the
# xApp window. This removes the small/fast-scenario crash.
# How long the xApp COLLECTS KPM before it moves on to the one-shot RC. This
# was a hardcoded sleep(5) in the xApp and it was the platform's UE ceiling:
# indications arrive once per reporting period of SIMULATED time, and a heavy
# scenario advances slowly, so five wall seconds bought 0.1 simulated seconds
# at 3 cells / 72 UEs — zero complete E2 rounds, only the two startup
# indications, and the platform read that as an engine limit. With a window
# sized to the scenario the identical config returns 86 KPM names and full DU
# metrics. Scaled by UE count because that is what sets the sim speed; small
# scenarios stay nearly as fast as before so campaigns do not pay for this.
# The window is also the run's SIMULATED HORIZON, and that relationship is
# linear with no diminishing return: measured 2026-09-13 at 3 cells / 6 UEs,
# spans of 4.1 / 5.8 / 9.2 / 13.9 simulated seconds for windows of 10 / 30 /
# 60 / 120 s, i.e. a flat ~0.10 simulated seconds per wall second throughout.
# So a scenario that needs elapsed time (a fault at start_s, a handover
# sequence, an energy duty cycle) can BUY it here — which is why an explicit
# NSG_KPM_COLLECT_SECS is allowed far past the auto-sized ceiling. The auto
# sizing stays capped at 180 s so nothing accidentally runs for a quarter of
# an hour; only an explicit request can go above it.
#
# THE EXPLICIT CEILING IS 4 HOURS, and that is a policy choice about the shape
# of this path rather than anything the engine cannot do. A batch run is ONE
# blocking request the whole way up (platform -> engine -> here) with a single
# salvage point at the end, so the longer it runs the more it stakes on one
# connection surviving. Four hours is roughly 24 minutes of network time on a
# bare 3-cell topology and about 6 on a full config — already far past what this
# path could reach before. Wanting more than that is not a request for a bigger
# number here; it is a request for streaming persistence, so the answer is the
# live path until a batch run can write its KPM out as it arrives.
#
# The xApp used to clamp at 3600 s of its own, which made an hour the real
# ceiling no matter what this said. That is lifted (172800, a sanity bound) so
# the policy lives in exactly one place: this line.
KPM_AUTO=$(( N_UE * 8 / 5 ))
[ "$KPM_AUTO" -lt 10 ] && KPM_AUTO=10
[ "$KPM_AUTO" -gt 180 ] && KPM_AUTO=180
KPM_COLLECT=${NSG_KPM_COLLECT_SECS:-$KPM_AUTO}
[ "$KPM_COLLECT" -lt 10 ] && KPM_COLLECT=10
[ "$KPM_COLLECT" -gt 14400 ] && KPM_COLLECT=14400
echo "[cfg] KPM collection window ${KPM_COLLECT}s (UE=$N_UE, auto ${KPM_AUTO}s)"
NS3_SIM=${NSG_NS3_SIM:-600}
# ns-3's wall bound has to cover the COLLECTION WINDOW, not just the xApp
# window: the xApp collects for KPM_COLLECT seconds after the E2 setup wait,
# and if ns-3 is killed underneath it the run is truncated exactly on the
# long-horizon scenarios the window was raised for.
NS3_TIMEOUT=$((XAPP_WINDOW + KPM_COLLECT + 120))
# E2 indication periodicity (s). 0.1s floods SCTP with many E2 nodes (a node drops
# -> no DU throughput); 0.5s is stable across the validated 3-7 gNB envelope.
IND_PERIOD=${NSG_IND_PERIOD:-0.5}
# Which build profile's binary to run. ns-3 suffixes artifacts with the profile,
# so the `optimized` build lands beside the default one rather than replacing it —
# selecting it is a name change, not a path change.
# Default stays `default` so nothing moves until asked. `optimized` was configured
# with --enable-asserts --enable-logs, so it keeps BOTH the NS_ASSERT that guards
# the scheduler's zero-size PDU path and the NS_LOG_UNCOND lines the platform
# parses; verified in the binary (assert strings present, 23 [NSG strings).
NSG_NS3_PROFILE="${NSG_NS3_PROFILE:-default}"
SCN_BIN="$NS3DIR/build/scratch/ns3.42-scenario-zero-with_parallel_loging-$NSG_NS3_PROFILE"

# THE BINARY FOR THIS PROFILE MUST EXIST. Without this the run dies with a bare
# "No such file or directory" inside /tmp/scn0.log and the caller sees a run
# that produced nothing — the failure this repo has a memory note about. The
# profile is selectable (NSG_NS3_PROFILE) and only `default` is ever built and
# shipped, so naming another one has to say so rather than look like an empty
# result.
if [ ! -x "$SCN_BIN" ]; then
  echo "[NSG-ERROR] no scenario binary for profile '${NSG_NS3_PROFILE}': $SCN_BIN"
  echo "[NSG-ERROR] only 'default' is built and shipped in this image."
  exit 3
fi

# NSG: prune transmitter-receiver pairs whose path loss puts them far below the
# noise floor, BEFORE the 3GPP channel matrix is built for them. ns-3 provides
# this for exactly that reason ("reduce the computational load by not propagating
# signals that are far beyond the interference range"); its default 1e9 means
# every negligible link still gets the full computation, and the channel stack is
# ~57% of CPU.
# 110 dB is measured, not guessed: at 20 MHz with NF 5 dB the noise floor is
# -96 dBm, and a sweep at 30 UEs gave bit-identical results down to 110 dB --
# same UE->serving-cell map, same handover count, energy within 0.01% -- for
# ~2x the speed. At 90 dB it breaks badly (map changes, handovers 30 -> 9), so
# the margin below 110 is real but not large. Override with NSG_MAX_LOSS_DB.
LOSS_ARG="--ns3::SpectrumChannel::MaxLossDb=${NSG_MAX_LOSS_DB:-110}"
SCN_ARGS="--e2TermIp=127.0.0.1 --e2nrEnabled=1 --e2du=$([ "${NSG_KPM_DU:-1}" = "0" ] && echo false || echo true) --reducedPmValues=$([ "${NSG_KPM_REDUCED:-0}" = "1" ] && echo true || echo false) --hoSinrDifference=${NSG_HO_SINR:-3} --handoverMode=${NSG_HO_MODE:-DynamicTtt} --ns3::LteEnbRrc::FixedTttValue=${NSG_HO_TTT:-110} --indicationPeriodicity=$IND_PERIOD --simTime=$NS3_SIM --KPM_E2functionID=2 --RC_E2functionID=3 --N_MmWaveEnbNodes=$N_GNB --N_Ues=$N_UE --CenterFrequency=$FREQ --Bandwidth=$BW --IntersideDistanceCells=$ISD_CELL --IntersideDistanceUEs=$ISD_UE $GNB_POS_ARG $UE_POS_ARG $GNB_POW_ARG $BLD_ARG $SLICE_ARG $QUOTA_ARG $SPEED_ARG $WP_ARG $HO_ARG $RRC_ARG $FAULT_ARG $ANT_ARG $RADIO_ARG $MRO_ARG $PHY_ARG $CH_ARG $CP_ARG $NET_ARG $SLEEP_ARG $EXTRA_ARG $LOSS_ARG"
echo "[cfg] gNB=$N_GNB UE=$N_UE freq=${FREQ}Hz bw=${BW}Hz isd_cell=${ISD_CELL}m isd_ue=${ISD_UE}m window=${XAPP_WINDOW}s ns3_sim=${NS3_SIM}s pos=${GNB_POS_ARG:+gnb}${UE_POS_ARG:+,ue}"
# Same two knobs as the live path, validated the same way — the batch and live
# paths run the SAME binary, and a key that reaches one and not the other is a
# bug this repo has already shipped once.
BURST_ENV=""
_bnum() { [[ "$1" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; }
if [ -n "${NSG_BURST_DBM:-}" ] && _bnum "${NSG_BURST_DBM}"; then
  BURST_ENV="NSG_BURST_DBM=${NSG_BURST_DBM}"
  if [ -n "${NSG_BURST_LOAD:-}" ] && _bnum "${NSG_BURST_LOAD}"; then
    BURST_ENV="$BURST_ENV NSG_BURST_LOAD=${NSG_BURST_LOAD}"
  fi
fi
runuser -u builder -- bash -lc "ulimit -c $CORE_LIMIT; cd $NS3DIR && timeout -s KILL $NS3_TIMEOUT env LD_LIBRARY_PATH=$NS3DIR/build/lib ${BURST_ENV:-} $SCN_BIN $SCN_ARGS" >/tmp/scn0.log 2>&1 &

# 3) wait for ALL E2 SETUP handshakes (agents connected) — bigger scenarios take
#    longer to boot, so wait for N_GNB setups (bounded), not just the first.
for i in $(seq 1 45); do [ "$(grep -c 'E2 SETUP-REQUEST' /tmp/ric.log)" -ge "$N_GNB" ] && break; sleep 1; done
echo "[ok] E2 agents connected: $(grep -c 'E2 SETUP-REQUEST' /tmp/ric.log) (E2SM-KPM/RC accepted)"

# 4) KPM xApp subscribes and receives indications; force-killed after XAPP_WINDOW
#    (it ignores SIGTERM). ns-3 keeps running (large --simTime) so the xApp's
#    teardown finds a live E2 node.
NSG_KPM_COLLECT_SECS=$KPM_COLLECT timeout -s KILL $((XAPP_WINDOW + KPM_COLLECT)) "$XAPP" >/tmp/xapp.log 2>&1
echo "=== xApp KPM output (values / indications) ==="
# The cap used to be 25 lines, and that WAS the engine's UE ceiling. The xApp
# prints one line per metric per UE, so the count scales with the scenario: at
# 72 UEs the filtered stream is 172 lines and the first DU metric lands on line
# 46, so everything the platform needs — RRU.PrbUsedDl, L3servingSINR,
# DRB.UEThpDl — was cut off and the run was classified `partial` with six
# CU-UP names. Nothing was wrong with the engine; it reported all of it.
# The whole filtered stream is 6.7 KB at 72 UEs, so the bound only has to stop
# a pathological run, not shape a normal one.
grep -iE "value =|Name =|measRecord|Indication|RAN Container|Reader|Latency|Prb|Throughput|cell" /tmp/xapp.log | head -4000
echo "=== xApp tail ==="; tail -12 /tmp/xapp.log
echo "=== DONE ==="
