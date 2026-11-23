#!/bin/bash
# NSG Phase-1b: PERSISTENT live session for the external-xApp bridge.
# Runs RIC + ns-3 (E2 agent) + the BRIDGE xApp. The bridge xApp reads control
# commands from the FIFO and streams KPM on stdout — which is THIS script's stdout,
# captured by live_bridge_server.py and relayed to the backend. Bounded everywhere
# so it can never hang. Defaults sit inside the validated 3-7 gNB envelope.
set -u
RIC=/root/nsoran/flexric/build/examples/ric/nearRT-RIC
XAPP=/root/nsoran/flexric/build/examples/xApp/c/kpm_rc/xapp_kpm_rc
NS3DIR=/home/builder/ns-O-RAN-flexric/mmwave-LENA-oran
# Which ns-3 output directory to run. Defaults to the production `build`, so
# nothing changes for a normal session; set NSG_NS3_BUILD=build-asan (or any
# other --out= profile) to run an instrumented tree without disturbing it.
NS3_BUILD=${NSG_NS3_BUILD:-build}
# Which build profile's binary to run. ns-3 suffixes artifacts with the profile,
# so the `optimized` build lands beside the default one rather than replacing it —
# selecting it is a name change, not a path change.
# Default stays `default` so nothing moves until asked. `optimized` was configured
# with --enable-asserts --enable-logs, so it keeps BOTH the NS_ASSERT that guards
# the scheduler's zero-size PDU path and the NS_LOG_UNCOND lines the platform
# parses; verified in the binary (assert strings present, 23 [NSG strings).
NSG_NS3_PROFILE="${NSG_NS3_PROFILE:-default}"
SCN_BIN="$NS3DIR/$NS3_BUILD/scratch/ns3.42-scenario-zero-with_parallel_loging-$NSG_NS3_PROFILE"

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

FIFO=${NSG_CTRL_FIFO:-/tmp/nsg_live_ctrl.fifo}
mkdir -p /tmp && chmod 1777 /tmp 2>/dev/null || true
# NSG mutual exclusion: one engine session at a time (batch + live share the RIC
# on SCTP 36421). Acquire BEFORE the cleanup trap so a busy exit never pkills the
# other session's RIC. flock auto-releases when this process exits.
# WSL pipes core_pattern to a capture helper that writes the dump to the
# Windows side, where it does NOT count against the distro's own df. Each
# ns-3 abort cost ~5 GB there; ten of them had quietly eaten 31 GB of the
# host disk. The engine's crashes are diagnosed from its log, not a core.
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

exec 9>/tmp/nsg_engine.lock
flock -n 9 || { echo "[BUSY] engine already running a session; stop it first"; exit 3; }

N_GNB=${NSG_N_GNB:-3}
N_UE=${NSG_N_UE:-6}
FREQ=${NSG_FREQ_HZ:-3.5e9}
BW=${NSG_BW_HZ:-20e6}
ISD_CELL=${NSG_ISD_CELL:-1000}
ISD_UE=${NSG_ISD_UE:-1000}
# How long this session may run. 1200 s (20 min) is a comfortable demo window and
# a manual Stop frees it early, but it is only a DEFAULT — the live path is where
# a run that needs hours belongs, since it streams its KPM out as it goes instead
# of holding one request open like the batch path does. The caller sets it via
# `bridge_secs` in nsg_params; clamped here to two days, which is a sanity bound
# rather than a measured limit (the longest session ever verified alive is 300 s).
BRIDGE_SECS=${NSG_BRIDGE_SECS:-1200}
case "$BRIDGE_SECS" in (''|*[!0-9]*) BRIDGE_SECS=1200 ;; esac
[ "$BRIDGE_SECS" -lt 10 ] && BRIDGE_SECS=10
[ "$BRIDGE_SECS" -gt 172800 ] && BRIDGE_SECS=172800

# -f for ns-3: its comm is truncated to 15 chars, so a -x match on the full
# binary name can never hit (the old line was a silent no-op).
cleanup(){ pkill -9 -x nearRT-RIC 2>/dev/null; pkill -9 -x xapp_kpm_rc 2>/dev/null; pkill -9 -f scenario-zero-with_parallel_loging 2>/dev/null; }
trap cleanup EXIT
cleanup; sleep 1
# The bridge server owns the FIFO (creates + holds it open); reuse that exact
# inode. Only create one if run standalone. NEVER rm/recreate it here — doing so
# orphans the server's write end and silently drops control commands.
[ -p "$FIFO" ] || mkfifo "$FIFO"; chmod 666 "$FIFO" 2>/dev/null || true

# 1) near-RT RIC
stdbuf -oL -eL "$RIC" >/tmp/ric.log 2>&1 &
sleep 4
grep -q 36421 /tmp/ric.log || { echo "[FATAL] RIC not up"; exit 1; }
echo "[live] RIC up (SCTP 36421)"

# 2) ns-3 RAN = E2 agent. HUGE simTime so it runs for the whole session; wall-bounded.
# Level-2/3: exact per-gNB/UE positions + per-gNB power (from the user's config).
GNB_POS_ARG=""; UE_POS_ARG=""; GNB_POW_ARG=""; BLD_ARG=""; SLICE_ARG=""; QUOTA_ARG=""; SPEED_ARG=""
if [ -n "${NSG_GNB_POS:-}" ]; then printf '%s' "$NSG_GNB_POS" > /tmp/nsg_gnb_pos.txt; chmod 644 /tmp/nsg_gnb_pos.txt; GNB_POS_ARG="--gnbPositionsFile=/tmp/nsg_gnb_pos.txt"; fi
if [ -n "${NSG_UE_POS:-}"  ]; then printf '%s' "$NSG_UE_POS"  > /tmp/nsg_ue_pos.txt;  chmod 644 /tmp/nsg_ue_pos.txt;  UE_POS_ARG="--uePositionsFile=/tmp/nsg_ue_pos.txt";  fi
# BURSTY EXTERNAL INTERFERENCE — the cells a partitioned run cannot see.
#
# Two numbers, validated as numbers HERE and passed as environment rather than
# as arguments: the line below interpolates into `bash -lc`, and this platform
# has already shipped one proven shell pass-through through a field like this.
# Anything that is not a plain decimal is dropped, not quoted and hoped for.
#
#   NSG_BURST_DBM  power over the whole carrier when every RB is hit
#   NSG_BURST_LOAD fraction of RBs hit per evaluation, 0..1 (1.0 = constant,
#                  which is the control arm and is measured NOT to work)
BURST_ENV=""
_num() { [[ "$1" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; }
if [ -n "${NSG_BURST_DBM:-}" ] && _num "${NSG_BURST_DBM}"; then
  BURST_ENV="NSG_BURST_DBM=${NSG_BURST_DBM}"
  if [ -n "${NSG_BURST_LOAD:-}" ] && _num "${NSG_BURST_LOAD}"; then
    BURST_ENV="$BURST_ENV NSG_BURST_LOAD=${NSG_BURST_LOAD}"
  fi
  echo "[live] bursty external interference: $BURST_ENV"
fi

if [ -n "${NSG_GNB_POW:-}" ]; then printf '%s' "$NSG_GNB_POW" > /tmp/nsg_gnb_pow.txt; chmod 644 /tmp/nsg_gnb_pow.txt; GNB_POW_ARG="--gnbPowerFile=/tmp/nsg_gnb_pow.txt"; fi
if [ -n "${NSG_BUILDINGS:-}" ]; then printf '%s' "$NSG_BUILDINGS" > /tmp/nsg_bld.txt; chmod 644 /tmp/nsg_bld.txt; BLD_ARG="--buildingsFile=/tmp/nsg_bld.txt"; fi
# Level-5: per-UE slice (5QI) + DL demand from the config.
if [ -n "${NSG_UE_SLICES:-}" ]; then printf '%s' "$NSG_UE_SLICES" > /tmp/nsg_slices.txt; chmod 644 /tmp/nsg_slices.txt; SLICE_ARG="--ueSlicesFile=/tmp/nsg_slices.txt"; fi
if [ -n "${NSG_SLICE_QUOTAS:-}" ]; then printf '%s' "$NSG_SLICE_QUOTAS" > /tmp/nsg_quotas.txt; chmod 644 /tmp/nsg_quotas.txt; QUOTA_ARG="--sliceQuotasFile=/tmp/nsg_quotas.txt"; fi
# NSG MRO: per-UE speeds (m/s) — real mobility instead of the hardcoded 2-4 m/s walk.
if [ -n "${NSG_UE_SPEED:-}" ]; then printf '%s' "$NSG_UE_SPEED" > /tmp/nsg_speeds.txt; chmod 644 /tmp/nsg_speeds.txt; SPEED_ARG="--ueSpeedsFile=/tmp/nsg_speeds.txt"; fi
# NSG Phase 2b: per-UE waypoint paths — the mobility that actually traverses a
# cell boundary (the random walk diffuses and never does).
WP_ARG=""
if [ -n "${NSG_UE_WAYPOINTS:-}" ]; then printf '%s' "$NSG_UE_WAYPOINTS" > /tmp/nsg_waypoints.txt; chmod 644 /tmp/nsg_waypoints.txt; WP_ARG="--ueWaypointsFile=/tmp/nsg_waypoints.txt"; fi
# NSG Phase 2b: 3GPP measurement-event handover layer. HoCheckPeriodMs is the
# load-bearing one — upstream disabled the RRC's periodic handover re-evaluation,
# so at 0 the RAN decides once at t=0 and mobility can never trigger a handover.
HO_ARG="--ns3::LteEnbRrc::HoCheckPeriodMs=${NSG_HO_CHECK_MS:-0}"
HO_ARG="$HO_ARG --ns3::LteEnbRrc::HoA3Entry=$([ "${NSG_HO_A3:-0}" = "1" ] && echo true || echo false)"
HO_ARG="$HO_ARG --ns3::LteEnbRrc::HoHysteresisDb=${NSG_HO_HYST:-0}"
HO_ARG="$HO_ARG --ns3::LteEnbRrc::HoA2ThresholdDb=${NSG_HO_A2:--1000}"
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
# NSG: the PHY's per-transmission trace — and THE SAME BLOCK IS IN
# run_scenario.sh. These two scripts build the same argument list twice, so
# a capability added to one is simply absent from the other: this switch was
# added to the batch script, shipped in an image, and produced nothing,
# because the live bridge runs THIS file. `_PARAM_ENV` parity was closed in
# 1.2; the SCRIPTS were not.
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
# Phase 2a: per-gNB RU sleep profile 'depth,idle_ms,wake_ms;...' (0=none 1=micro
# 2=light 3=deep) + controller thresholds + optional per-depth power draws.
SLEEP_ARG=""
if [ -n "${NSG_GNB_SLEEP:-}" ]; then printf '%s' "$NSG_GNB_SLEEP" > /tmp/nsg_sleep.txt; chmod 644 /tmp/nsg_sleep.txt; SLEEP_ARG="--gnbSleepFile=/tmp/nsg_sleep.txt --sleepWakeLoadPct=${NSG_SLEEP_WAKE_PCT:-60} --sleepIdleDutyPct=${NSG_SLEEP_IDLE_DUTY:-0} --sleepWakeHoldMs=${NSG_SLEEP_HOLD_MS:-500}"; fi
[ -n "${NSG_SLEEP_MICRO_A:-}" ] && SLEEP_ARG="$SLEEP_ARG --ns3::MmWaveRadioEnergyModelEnb::SleepMicroA=$NSG_SLEEP_MICRO_A"
[ -n "${NSG_SLEEP_LIGHT_A:-}" ] && SLEEP_ARG="$SLEEP_ARG --ns3::MmWaveRadioEnergyModelEnb::SleepLightA=$NSG_SLEEP_LIGHT_A"
[ -n "${NSG_SLEEP_DEEP_A:-}"  ] && SLEEP_ARG="$SLEEP_ARG --ns3::MmWaveRadioEnergyModelEnb::SleepDeepA=$NSG_SLEEP_DEEP_A"
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

# The rest of LteEnbRrc, mirroring run_scenario.sh. These were reaching the batch
# script and being ignored here, so the same payload produced a differently
# configured RAN depending on which path ran it.
RRC_ARG="--ns3::LteEnbRrc::RsrpFilterCoefficient=${NSG_RSRP_FILTER:-4}"
RRC_ARG="$RRC_ARG --ns3::LteEnbRrc::RsrqFilterCoefficient=${NSG_RSRQ_FILTER:-4}"
RRC_ARG="$RRC_ARG --ns3::LteEnbRrc::ConnectionRequestTimeoutDuration=+${NSG_CONN_REQ_MS:-15}ms"
RRC_ARG="$RRC_ARG --ns3::LteEnbRrc::ConnectionSetupTimeoutDuration=+${NSG_CONN_SETUP_MS:-150}ms"
RRC_ARG="$RRC_ARG --ns3::LteEnbRrc::ConnectionRejectedTimeoutDuration=+${NSG_CONN_REJ_MS:-30}ms"
RRC_ARG="$RRC_ARG --ns3::LteEnbRrc::SrsPeriodicity=${NSG_SRS_PERIOD:-320}"
RRC_ARG="$RRC_ARG --ns3::LteEnbRrc::SystemInformationPeriodicity=+${NSG_SIB_MS:-5}ms"
RRC_ARG="$RRC_ARG --ns3::LteEnbRrc::DefaultTransmissionMode=${NSG_TX_MODE:-0}"
RRC_ARG="$RRC_ARG --ns3::LteEnbRrc::ReportAllUeMeas=$([ "${NSG_REPORT_ALL_MEAS:-1}" = "0" ] && echo false || echo true)"
RRC_ARG="$RRC_ARG --ns3::LteEnbRrc::AdmitHandoverRequest=$([ "${NSG_ADMIT_HO:-1}" = "0" ] && echo false || echo true)"

# Per-cell fault schedule. Its own file rather than the batch path's
# /tmp/nsg_faults.txt: the engine serialises sessions today, but a shared
# scratch path between two run scripts is a collision waiting to be written.
FAULT_ARG=""
if [ -n "${NSG_GNB_FAULTS:-}" ]; then
  printf '%s' "$NSG_GNB_FAULTS" > /tmp/nsg_faults_live.txt
  chmod 644 /tmp/nsg_faults_live.txt
  FAULT_ARG="--gnbFaultsFile=/tmp/nsg_faults_live.txt"
fi
SCN_ARGS="--e2TermIp=127.0.0.1 --e2nrEnabled=1 --e2du=$([ "${NSG_KPM_DU:-1}" = "0" ] && echo false || echo true) --reducedPmValues=$([ "${NSG_KPM_REDUCED:-0}" = "1" ] && echo true || echo false) --hoSinrDifference=${NSG_HO_SINR:-3} --handoverMode=${NSG_HO_MODE:-DynamicTtt} --ns3::LteEnbRrc::FixedTttValue=${NSG_HO_TTT:-110} --indicationPeriodicity=${NSG_IND_PERIOD:-0.25} --simTime=100000 --KPM_E2functionID=2 --RC_E2functionID=3 --N_MmWaveEnbNodes=$N_GNB --N_Ues=$N_UE --CenterFrequency=$FREQ --Bandwidth=$BW --IntersideDistanceCells=$ISD_CELL --IntersideDistanceUEs=$ISD_UE $GNB_POS_ARG $UE_POS_ARG $GNB_POW_ARG $BLD_ARG $SLICE_ARG $QUOTA_ARG $SPEED_ARG $WP_ARG $HO_ARG $ANT_ARG $RADIO_ARG $MRO_ARG $PHY_ARG $CH_ARG $CP_ARG $NET_ARG $SLEEP_ARG $LOSS_ARG $RRC_ARG $FAULT_ARG"
echo "[live] scenario: gNB=$N_GNB UE=$N_UE freq=${FREQ}Hz bw=${BW}Hz pos=${GNB_POS_ARG:+yes} pow=${GNB_POW_ARG:+yes}"
# ns-3 has a rare startup abort (heap corruption in its first ~0.2 s). The batch
# path always retried once; the live path just went dark — with the user's
# scenario class it was hitting 2/2. Same medicine here: launch, verify it
# survives its first seconds, relaunch once if it died.
launch_ns3() {
  # The budget covers E2_WAIT as well as the session: ns-3 builds every UE before
  # its agents register (101 s at 90 UEs, 164 s at 150), and a BRIDGE_SECS+60
  # limit killed it mid-session, which then read as a density failure.
  # NOTE --foreground: GNU timeout otherwise moves the child into its OWN
  # process group, which let the whole ns-3 tree escape the bridge's killpg on
  # teardown and kept the engine lock held for minutes after a crash/stop.
  # ulimit at the top of THIS script does not reach ns-3: runuser opens a new
  # PAM session and resets the core limit, so four ladder runs still wrote
  # 41 GB of dumps to the Windows side. It has to be set inside the shell
  # that actually execs the simulator.
  runuser -u builder -- bash -lc "ulimit -c ${CORE_LIMIT:-0}; cd $NS3DIR && timeout --foreground -s KILL $((BRIDGE_SECS+E2_WAIT+60)) env LD_LIBRARY_PATH=$NS3DIR/$NS3_BUILD/lib ${NSG_RUN_ENV:-} ${BURST_ENV:-} $SCN_BIN $SCN_ARGS" >/tmp/scn0.log 2>&1 &
}
# Budget for E2 registration, needed BEFORE launching: ns-3's own timeout has to
# cover startup as well as the session, or it gets killed mid-run.
E2_WAIT=$(( 90 + N_UE * 4 )); [ "$E2_WAIT" -gt 900 ] && E2_WAIT=900
for NSG_TRY in 1 2; do
  RIC_BASE=$(grep -c 'E2 SETUP-REQUEST' /tmp/ric.log)
  launch_ns3
  # ns-3 builds every UE before its E2 agents connect, so registration takes far
  # longer as the scenario grows. A flat 60 s made 75-UE runs give up waiting and
  # start the xApp against a RIC with no E2 nodes; FlexRIC then asserted
  # ("No global node conected??") and died, which read as an ns-3 density
  # ceiling at 25 UEs/cell. Scale the budget, and report what it actually took.
  E2_T0=$SECONDS
  for i in $(seq 1 "$E2_WAIT"); do
    [ "$(grep -c 'E2 SETUP-REQUEST' /tmp/ric.log)" -ge "$((RIC_BASE + N_GNB))" ] && break
    pgrep -f "$SCN_BIN" >/dev/null || break   # died before connecting
    sleep 1
  done
  E2_GOT=$(( $(grep -c 'E2 SETUP-REQUEST' /tmp/ric.log) - RIC_BASE ))
  echo "[live] E2 registration: ${E2_GOT}/${N_GNB} agents after $((SECONDS - E2_T0))s (budget ${E2_WAIT}s)"
  # Alive is not the same as registered. Continuing with too few E2 nodes is what
  # killed the RIC, so fail THIS attempt and let the retry logic below run.
  if [ "$E2_GOT" -lt "$N_GNB" ]; then
    pkill -9 -f "$SCN_BIN" 2>/dev/null; sleep 1
  fi
  if pgrep -f "$SCN_BIN" >/dev/null; then break; fi
  if [ "$NSG_TRY" = "1" ]; then
    # Restart the RIC too: the crashed attempt leaves dead SCTP associations
    # behind, and the xApp would otherwise subscribe to ghost gNBs.
    echo "[live] ns-3 died during startup — restarting RIC and retrying once"
    pkill -9 -x nearRT-RIC 2>/dev/null; sleep 1
    stdbuf -oL -eL "$RIC" >/tmp/ric.log 2>&1 &
    sleep 4
    grep -q 36421 /tmp/ric.log || { echo "[NSG-ERROR] RIC failed to restart - start the session again"; exit 1; }
  else
    echo "[NSG-ERROR] ns-3 aborted twice during startup - check the scenario geometry (overlapping cells / buildings), then start the session again"
    exit 1
  fi
done
echo "[live] E2 agents connected: $(grep -c 'E2 SETUP-REQUEST' /tmp/ric.log)"

# 3) BRIDGE xApp in the FOREGROUND: KPM -> our stdout, control <- FIFO. When it
#    returns (session end / timeout), the trap tears RIC + ns-3 down.
#    --foreground for the same teardown reason as ns-3 above.
echo "[live] bridge xApp starting (FIFO=$FIFO secs=$BRIDGE_SECS gNB=$N_GNB UE=$N_UE)"
NSG_CTRL_FIFO="$FIFO" NSG_BRIDGE_SECS="$BRIDGE_SECS" timeout --foreground -s KILL $((BRIDGE_SECS+20)) "$XAPP"
# Why did the xApp return? If ns-3 is already gone it crashed MID-SESSION (the
# xApp lost its E2 peer and exited early) — surface that as the reason instead
# of a bare "session ended". Printed on the script's stdout, so pump_kpm reads
# it BEFORE the process ends: no race with the socket teardown (unlike a tail
# thread trying to send while handle() is closing conn).
if ! pgrep -f scenario-zero-with_parallel_loging >/dev/null 2>&1; then
  # Report what ns-3 SAID, not a guess at why. This used to assert the scenario
  # was too dense and tell the operator to shrink it; that diagnosis was wrong
  # at least once on a three-cell nine-UE run, which is not dense by any
  # measure, and it points at the one thing that was already fine. The log is
  # right here — quote it.
  WHY=$(grep -aoE "NS_FATAL[^\"]{0,160}|NS_ASSERT failed[^\"]{0,160}|Segmentation fault|malloc\(\)[^\"]{0,80}|free\(\)[^\"]{0,80}|corrupted[^\"]{0,80}" /tmp/scn0.log 2>/dev/null | tail -1)
  if [ -z "$WHY" ]; then
    WHY=$(tail -c 400 /tmp/scn0.log 2>/dev/null | tr '\n' ' ' | tr -s ' ')
  fi
  echo "[NSG-ERROR] ns-3 exited before the session ended. Engine log: ${WHY:-(no output captured)}"
fi
echo "[live] session ended"
