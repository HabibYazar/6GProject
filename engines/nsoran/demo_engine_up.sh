#!/bin/bash
# NSG demo bring-up, ENGINE distro half (run as root in Ubuntu-2004-nsoran).
# dockerd + nsg-engine container + a restart that rebinds the WSL port-proxy: a
# cold dockerd start leaves the proxy dead, so the engine answers on its own
# distro but is UNREACHABLE from the platform distro.

# ── The one place the engine's identity is written ───────────────────────────
# Every `docker run` below uses these. They were drifting apart: this script's
# fallbacks still said nsg-engine:1.0 — two releases before RSRP existed — on
# 0.0.0.0 instead of loopback (undoing the 1.4 security fix) and without the
# named state volume. `docker start` reuses whatever container exists, so none of
# that showed until something removed it. Then bring-up would quietly hand the
# demo a 1.0 engine, publicly bound, with no run snapshot.
IMAGE=nsg-engine:1.32-rxtrace
RUN_ARGS=(-d --init --name nsg-engine
          -v nsg-run-state:/var/lib/nsg
          -p 127.0.0.1:8090:8090 -p 127.0.0.1:8091:8091)

echo "[1/2] Docker daemon..."
# setsid, not a bare nohup: a background child of the calling wsl.exe session is
# killed when that session ends, so `nohup dockerd &` left nothing running. This
# distro has no `docker` service unit, so the fallback IS the path.
# A stale /var/run/docker.pid blocks startup after a distro restart (the PID in it
# belongs to something else by then), and the trailing sleep is load-bearing:
# returning the instant after the fork ends the wsl.exe session while dockerd is
# still coming up, and it dies with it.
docker info >/dev/null 2>&1 || {
  pgrep -x dockerd >/dev/null || rm -f /var/run/docker.pid
  service docker start >/dev/null 2>&1 || (setsid nohup dockerd >/var/log/dockerd.log 2>&1 </dev/null & sleep 8)
}
for i in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
docker info >/dev/null 2>&1 && echo "   dockerd UP" || { echo "   dockerd FAILED"; exit 1; }

echo "[2/2] Engine container (+ port-proxy rebind)..."
# Ports are fixed at CREATION — `docker start` cannot add one. A container left
# over from an older run published only 8090, so the engine answered /health and
# every batch/live run failed with "live engine unreachable at 127.0.0.1:8091",
# which reads as a dead engine. Check the bindings and recreate if they are wrong
# rather than starting a container that cannot serve.
BINDINGS=$(docker inspect nsg-engine --format '{{json .HostConfig.PortBindings}}' 2>/dev/null || echo '{}')
case "$BINDINGS" in
  *'"8090/tcp"'*'"8091/tcp"'*|*'"8091/tcp"'*'"8090/tcp"'*) : ;;
  *)
    if [ "$BINDINGS" != "{}" ]; then
      echo "   container publishes $BINDINGS — recreating with 8090 + 8091"
    fi
    docker rm -f nsg-engine >/dev/null 2>&1 || true
    docker run "${RUN_ARGS[@]}" "$IMAGE" >/dev/null 2>&1
    ;;
esac
docker start nsg-engine >/dev/null 2>&1 || docker run "${RUN_ARGS[@]}" "$IMAGE" >/dev/null 2>&1
sleep 3
# Rebind the published ports. After a COLD dockerd start one restart is not
# always enough: the container answers on its own distro while docker-proxy has
# not bound yet, so the PLATFORM distro still cannot reach it — and a health
# check done here would wrongly report success. Restart until docker-proxy is
# actually LISTENING on 8090/8091 (that is the cross-distro path).
proxy_up() { ss -tln 2>/dev/null | grep -q ":8090 " && ss -tln 2>/dev/null | grep -q ":8091 "; }
for attempt in 1 2 3; do
  docker restart nsg-engine >/dev/null 2>&1
  for i in $(seq 1 12); do proxy_up && break; sleep 1; done
  proxy_up && break
  echo "   port-proxy not bound yet (attempt $attempt) — retrying"
done
# `ss` sees a LISTEN socket only when docker runs its userland proxy; with the
# proxy disabled the path is DNAT and there is no socket to see. So a missing
# listener is not proof of a broken port — the authority is whether the PLATFORM
# distro can actually reach both ports, which tools/preflight.py checks from
# there. Say which one this is instead of printing a WARN that means neither.
if proxy_up; then
  echo "   port-proxy bound on 8090/8091"
else
  echo "   note: no LISTEN socket on 8090/8091 (normal when docker-proxy is off;"
  echo "         the cross-distro check in tools/preflight.py is the authority)"
fi
# Say which engine is actually RUNNING, not which one we would have started.
RUNNING=$(docker inspect nsg-engine --format '{{.Config.Image}}' 2>/dev/null)
if [ "$RUNNING" = "$IMAGE" ]; then
  echo "   image $RUNNING"
else
  echo "   WARN: running $RUNNING but this demo expects $IMAGE"
fi
printf '   engine (local): '
curl -s --max-time 6 http://localhost:8090/health || echo UNREACHABLE
echo
