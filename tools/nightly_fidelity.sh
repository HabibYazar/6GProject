#!/bin/bash
# NSG nightly fidelity watchdog. Runs the full lever harness against the real
# engine and records the verdict — so a change that silently un-wires a lever
# (it has happened twice) shows up the next morning instead of weeks later.
#
# Run from the Ubuntu WSL distro (the Windows scheduled task calls it via
# wsl.exe). Ensures its own prerequisites: the engine container in the
# Ubuntu-2004-nsoran distro and the platform backend on :8000.
#
# Usage: bash tools/nightly_fidelity.sh [--quick]   (--quick = cross-tier only)
set -u
ROOT=/home/chancelor/nsg_ai_platform
REPORTS=$ROOT/reports
mkdir -p "$REPORTS"
STAMP=$(date +%Y-%m-%d_%H%M)
LOG=$REPORTS/fidelity-$STAMP.log

# 1) engine distro: docker daemon + container up (idempotent).
wsl.exe -d Ubuntu-2004-nsoran -u root -e bash -c '
  docker info >/dev/null 2>&1 || { service docker start >/dev/null 2>&1 || (nohup dockerd >/var/log/dockerd.log 2>&1 &); }
  for i in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
  docker start nsg-engine >/dev/null 2>&1 || docker run -d --init --name nsg-engine -v nsg-run-state:/var/lib/nsg -p 127.0.0.1:8090:8090 -p 127.0.0.1:8091:8091 nsg-engine:1.30-guard >/dev/null 2>&1
  sleep 3; curl -s --max-time 5 http://localhost:8090/health' >>"$LOG" 2>&1

# 2) backend up (start it if the platform isn't running).
if ! curl -s --max-time 4 http://localhost:8000/health >/dev/null 2>&1; then
  (cd "$ROOT/nsg_ai" && nohup ../venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000 \
     >>"$REPORTS/backend-nightly.log" 2>&1 &)
  for i in $(seq 1 20); do curl -s --max-time 3 http://localhost:8000/health >/dev/null 2>&1 && break; sleep 1; done
fi

# 2b) preflight gate. Without it a night where the engine was simply unreachable
# produces a RED that looks like a fidelity regression — the most expensive kind
# of false alarm, because the morning is spent looking in the wrong place.
cd "$ROOT"
if ! ./venv/bin/python tools/preflight.py --fix --quiet >>"$LOG" 2>&1; then
  VERDICT="RESULT: PREFLIGHT FAILED (engine/backend unreachable — not a fidelity result)"
  echo "$STAMP  $VERDICT" >>"$REPORTS/fidelity-nightly.log"
  { echo "$STAMP  $VERDICT"; echo "full log: $LOG"; } >"$REPORTS/FIDELITY_RED"
  echo "$STAMP  $VERDICT"
  exit 2
fi

# 3) the harness itself.
timeout 2400 ./venv/bin/python -u tools/fidelity_report.py "$@" >>"$LOG" 2>&1
RC=$?

# 3b) parameter audit. The harness proves the levers it KNOWS about still move
# the engine; this proves no NEW schema field was added that reaches nothing.
# Cheap (reuses the cached engine dump) and it is the regression that let ~21
# silently-dropped fields accumulate before anyone noticed.
./venv/bin/python tools/param_audit.py --cached --check >>"$LOG" 2>&1
AUDIT_RC=$?
if [ $AUDIT_RC -ne 0 ]; then
  echo "$STAMP  PARAM AUDIT: INERT FIELDS PRESENT (see $LOG)" >>"$REPORTS/fidelity-nightly.log"
  { echo "$STAMP  PARAM AUDIT FAILED — a schema field reaches no engine";
    echo "full log: $LOG"; } >"$REPORTS/PARAMS_RED"
else
  rm -f "$REPORTS/PARAMS_RED"
fi

# 3b2) run-summary contract. Cheap, no engine run: pins the SHAPE three layers
# depend on (dotted measurement names, object-valued energy_final) and checks the
# UI's own metric list against a real summary. Two contract breaks got through in
# one afternoon, both presenting as a legitimate FAIL rather than a bug.
./venv/bin/python engines/nsoran/test_summary_contract.py >>"$LOG" 2>&1 \
  || echo "$STAMP  SUMMARY CONTRACT BROKEN (see $LOG)" >>"$REPORTS/fidelity-nightly.log"

# 3b2b) batch/live path parity. One payload, two consumers: 14 keys once reached
# the batch path and were dropped by the live one — including every staged
# anomaly, so no campaign run could inject a fault at all. param_audit cannot see
# this (it measures the payload, not which consumer honours it).
./venv/bin/python tools/test_path_parity.py >>"$LOG" 2>&1 \
  || echo "$STAMP  PATH PARITY GAP — a param reaches one engine path and not the other (see $LOG)" \
       >>"$REPORTS/fidelity-nightly.log"

# 3b3) AI prompt contract. No model call, no engine: asserts the generator can
# still NAME every capability the schema offers. The schema grew to 168 fields
# while the prompt skeleton still showed 54, and the visible symptom was an AI
# that produced a static scenario when asked for a handover test.
(cd nsg_ai && ../venv/bin/python test_prompt_contract.py) >>"$LOG" 2>&1 \
  || echo "$STAMP  AI PROMPT CONTRACT BROKEN — the schema moved ahead of the prompt (see $LOG)" \
       >>"$REPORTS/fidelity-nightly.log"

# 3c) scenario campaign. The harness above proves the ENGINE still honours each
# lever; this proves the SCENARIOS still meet their thresholds, and diffs the
# result against last night's run of the same suite. Skipped silently until a
# spec exists (tools/nightly_campaign.py --write-default).
if [ -f "$ROOT/tools/nightly_campaign.json" ]; then
  ./venv/bin/python tools/nightly_campaign.py >>"$LOG" 2>&1
  echo "   campaign: $(tail -1 "$REPORTS/campaign-nightly.log" 2>/dev/null || echo 'no verdict')"
fi

VERDICT=$(grep -E "^RESULT:" "$LOG" | tail -1)
[ -z "$VERDICT" ] && VERDICT="RESULT: NO VERDICT (harness died, rc=$RC)"
echo "$STAMP  $VERDICT" >>"$REPORTS/fidelity-nightly.log"

# 4) loud marker on failure — check reports/FIDELITY_RED in the morning.
if echo "$VERDICT" | grep -qE "ALL LEVERS WIRED|QUICK OK"; then
  rm -f "$REPORTS/FIDELITY_RED"
else
  { echo "$STAMP  $VERDICT"; echo "full log: $LOG"; } >"$REPORTS/FIDELITY_RED"
fi
echo "$STAMP  $VERDICT"
