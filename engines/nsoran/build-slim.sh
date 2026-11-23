#!/bin/bash
# Build the LEAN ns-O-RAN engine image from THIS distro's runtime artifacts
# (not the 16.9GB fat snapshot). Reproducible while the FlexRIC + ns-3 build
# trees exist here. Runtime deps derived by ldd: libc6/libgcc-s1/libstdc++6 (base)
# + libsctp1 + libsqlite3-0; plus procps (pkill) + python3 (engine_service.py).
#
# Run inside the isolated distro Ubuntu-2004-nsoran (where dockerd + the build
# trees live):   bash build-slim.sh [tag]      (default tag: nsg-engine:1.5)
set -euo pipefail
TAG=${1:-nsg-engine:1.5}
# The runtime base (Ubuntu + apt packages) is the only part that needs the
# network. Built here once if missing; on a machine with no internet it has to
# be loaded first (docker load) — the engine image itself never downloads.
BASE=${NSG_ENGINE_BASE:-nsg-engine-base:20.04}
if ! docker image inspect "$BASE" >/dev/null 2>&1; then
  echo "[*] base image $BASE missing — building it (needs the network once)"
  docker build -t "$BASE" - < "$(dirname "$0")/Dockerfile.base"
fi
FLEXRIC=/root/nsoran/flexric
NS3=/home/builder/ns-O-RAN-flexric/mmwave-LENA-oran
CTX=$(mktemp -d /tmp/nsg-slim.XXXXXX)
echo "[*] assembling context at $CTX"

install -D "$FLEXRIC/build/examples/ric/nearRT-RIC"            "$CTX/flexric/nearRT-RIC"
install -D "$FLEXRIC/build/examples/xApp/c/kpm_rc/xapp_kpm_rc" "$CTX/flexric/xapp_kpm_rc"
mkdir -p "$CTX/flexric/lib"; cp -a /usr/local/lib/flexric/.    "$CTX/flexric/lib/"
install -D /usr/local/etc/flexric/flexric.conf                 "$CTX/flexric.conf"

mkdir -p "$CTX/ns3/scratch" "$CTX/ns3/lib"
cp -a "$NS3/build/scratch/ns3.42-scenario-zero-with_parallel_loging-default" "$CTX/ns3/scratch/"
cp -a "$NS3/build/lib/." "$CTX/ns3/lib/"

mkdir -p "$CTX/nsoran"
# Batch API (engine_service.py) + live bridge (live_bridge_server.py + run_live_session.sh).
cp -a /root/nsoran/run_scenario.sh /root/nsoran/engine_service.py \
      /root/nsoran/run_live_session.sh /root/nsoran/live_bridge_server.py "$CTX/nsoran/"

cp "$(dirname "$0")/Dockerfile" "$CTX/Dockerfile" 2>/dev/null || cat > "$CTX/Dockerfile" <<'DOCKER'
FROM ubuntu:20.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3 libsctp1 libsqlite3-0 libstdc++6 procps \
 && rm -rf /var/lib/apt/lists/*
RUN useradd -m builder
COPY flexric/nearRT-RIC   /root/nsoran/flexric/build/examples/ric/nearRT-RIC
COPY flexric/xapp_kpm_rc  /root/nsoran/flexric/build/examples/xApp/c/kpm_rc/xapp_kpm_rc
COPY flexric/lib/         /usr/local/lib/flexric/
COPY flexric.conf         /usr/local/etc/flexric/flexric.conf
COPY --chown=builder ns3/ /home/builder/ns-O-RAN-flexric/mmwave-LENA-oran/build/
COPY nsoran/              /root/nsoran/
RUN chmod +x /root/nsoran/run_scenario.sh /root/nsoran/engine_service.py \
 && chown builder:builder /home/builder/ns-O-RAN-flexric /home/builder/ns-O-RAN-flexric/mmwave-LENA-oran
ENV LD_LIBRARY_PATH=/usr/local/lib:/usr/local/lib/flexric
EXPOSE 8090
CMD ["python3", "/root/nsoran/engine_service.py"]
DOCKER

echo "[*] building $TAG from $BASE (no network)"
# --network none: everything the image needs is in the context or the base, and
# this proves it on every build instead of leaving an offline machine to find out.
docker build --network none --build-arg BASE="$BASE" -t "$TAG" "$CTX"
rm -rf "$CTX"
docker images "$TAG" --format "[done] {{.Repository}}:{{.Tag}} -> {{.Size}}"
