#!/bin/bash
# Bring up N engine instances and print the NSG_ENGINE_POOL for them.
#
# A run is single-core (ns-3 has one ordered event queue), so campaign
# throughput comes from more engines rather than a faster one: measured 5.8x at
# six concurrent runs for 4% more wall time each. One run per instance, never
# two per instance — run_scenario.sh writes fifteen fixed /tmp paths, so two
# runs sharing an engine would read each other's inputs.
#
#   bash tools/engine_pool_up.sh 4        # start 4, print the env line
#   bash tools/engine_pool_up.sh 0        # tear the extra instances down
#
# Instance 0 is the existing nsg-engine on 8090/8091; extras are nsg-pool-N on
# 810N/820N. Sizing: one run needs one core and ~300 MB, so N should stay at or
# below the PHYSICAL core count. At six concurrent on four physical cores one
# run in twelve came back degraded — treat `partial` results as the signal to
# come down, not the throughput curve.
set -euo pipefail

N=${1:-2}
IMAGE=${NSG_ENGINE_IMAGE:-nsg-engine:1.30-guard}
DISTRO=${NSG_ENGINE_DISTRO:-Ubuntu-2004-nsoran}
WSL=/mnt/c/Windows/System32/wsl.exe

run() { "$WSL" -d "$DISTRO" -u root -e bash -lc "cd /; $1" 2>/dev/null; }

# Fail loudly when the daemon is down. Without this every docker call below
# returns empty, the script prints a pool line for containers that do not exist,
# and the failure only surfaces later as a connection refused somewhere else.
if ! run "docker info >/dev/null 2>&1 && echo up" | grep -q up; then
  echo "dockerd is not running in $DISTRO — nothing was started." >&2
  echo "  fix: python3 tools/preflight.py --fix" >&2
  exit 1
fi

# Always clear extras first so the pool matches what was asked for rather than
# accumulating instances from previous, larger runs.
for i in $(seq 1 16); do
  run "docker rm -f nsg-pool-$i >/dev/null 2>&1 || true"
done

if [ "$N" -le 0 ]; then
  echo "extra instances removed; nsg-engine left running"
  echo "unset NSG_ENGINE_POOL   # back to the single engine"
  exit 0
fi

# `docker start` on an existing container reuses whatever image it was CREATED
# from, so a plain start-or-run silently pins instance 0 to an old build. That
# happened: pool instances came up on the new image while nsg-engine stayed a
# version behind, and the engine answering :8090 was not the engine we had just
# built. Recreate when the image does not match.
CUR=$(run "docker inspect -f '{{.Config.Image}}' nsg-engine 2>/dev/null" | tr -d '\r')
if [ -n "$CUR" ] && [ "$CUR" != "$IMAGE" ]; then
  echo "nsg-engine was created from $CUR, recreating on $IMAGE"
  run "docker rm -f nsg-engine >/dev/null 2>&1"
fi
# Each instance gets its OWN state volume. They would otherwise share one
# snapshot file and overwrite each other's in-flight run, which is the same
# cross-contamination the per-run id guards against on the reading side.
run "docker start nsg-engine >/dev/null 2>&1 || docker run -d --init --name nsg-engine \
     -v nsg-run-state:/var/lib/nsg \
     -p 127.0.0.1:8090:8090 -p 127.0.0.1:8091:8091 $IMAGE >/dev/null"
ADDRS="127.0.0.1:8091"

for i in $(seq 1 $((N - 1))); do
  if ! run "docker run -d --init --name nsg-pool-$i -p 127.0.0.1:$((8100 + i)):8090 \
            -v nsg-run-state-$i:/var/lib/nsg \
            -p 127.0.0.1:$((8200 + i)):8091 $IMAGE >/dev/null 2>&1"; then
    echo "instance $i failed to start — port $((8200 + i)) is probably held by"
    echo "another container. What is holding it:"
    run "docker ps --format '  {{.Names}} {{.Ports}}' | grep -E '82[0-9]{2}' || true"
    exit 1
  fi
  ADDRS="$ADDRS,127.0.0.1:$((8200 + i))"
done

echo "waiting for $N instance(s) to answer…"
for addr in ${ADDRS//,/ }; do
  port=${addr##*:}
  for _ in $(seq 1 30); do
    if run "timeout 2 bash -c '</dev/tcp/127.0.0.1/$port' 2>/dev/null" ; then break; fi
    sleep 2
  done
done

CORES=$(run "nproc" | tr -d '\r')
echo
echo "$N instance(s) up · distro reports $CORES logical CPUs"
[ "$N" -gt $((CORES / 2)) ] && echo "  WARNING: more instances than physical cores — expect degraded runs"
echo
echo "export NSG_ENGINE_POOL=\"$ADDRS\""
echo
echo "Then restart the backend so it picks the pool up."
