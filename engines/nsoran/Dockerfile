# ─────────────────────────────────────────────────────────────────────────────
# NSG ns-O-RAN engine — LEAN runtime image  (builds nsg-engine:1.0)
#
# Pairs with build-slim.sh, which assembles the build context from the 20.04
# distro's own runtime artifacts (FlexRIC binaries + Service Models, the ns-3.42
# scenario binary + build/lib, the engine scripts). Run:  bash build-slim.sh
#
# WHY: the old nsg-engine:0.1/0.2 were 16.9 GB — a `tar | docker import` snapshot
# of the ENTIRE build distro (compilers, -dev pkgs, .o object files, apt cache).
# The runtime needs only the binaries + a handful of shared libs. Runtime deps
# were derived with `ldd` on the three binaries: libc6/libgcc-s1/libstdc++6 come
# with the base; the only extras are libsctp1 + libsqlite3-0, plus procps (pkill,
# used by run_scenario.sh) and python3 (engine_service.py). No boost/eigen/gsl.
#
# Those packages and the `builder` user live in Dockerfile.base, so nothing below
# needs the network: build-slim.sh builds this with --network none.
# ─────────────────────────────────────────────────────────────────────────────
ARG BASE=nsg-engine-base:20.04
FROM ${BASE}

# FlexRIC near-RT RIC + KPM/RC xApp + Service Models + config
COPY flexric/nearRT-RIC   /root/nsoran/flexric/build/examples/ric/nearRT-RIC
COPY flexric/xapp_kpm_rc  /root/nsoran/flexric/build/examples/xApp/c/kpm_rc/xapp_kpm_rc
COPY flexric/lib/         /usr/local/lib/flexric/
COPY flexric.conf         /usr/local/etc/flexric/flexric.conf

# ns-3.42 mmWave scenario binary + its shared libs (context ns3/ = {scratch,lib})
COPY --chown=builder ns3/ /home/builder/ns-O-RAN-flexric/mmwave-LENA-oran/build/

# Engine glue (orchestrator + HTTP service)
COPY nsoran/  /root/nsoran/

# ns-3 runs as 'builder' and writes trace .txt files to its cwd ($NS3DIR); the
# COPY-created parent dirs are root-owned, so make them builder-writable (else
# NS_FATAL "Could not open tracefile"). Only 2 dirs chowned, NOT the 1.3GB tree,
# and folded into one RUN — no layer bloat.
# /var/lib/nsg is where a run publishes itself as it goes (engine_service's
# snapshot). Deliberately NOT a VOLUME instruction: that would make an anonymous
# volume on every run, which is durable but unfindable. Mount a NAMED one over it
# to keep a run past `docker rm`; without one this is an ordinary directory and
# the snapshot behaves as it did, surviving a restart but not a recreate.
RUN mkdir -p /var/lib/nsg \
 && chmod +x /root/nsoran/run_scenario.sh /root/nsoran/engine_service.py \
      /root/nsoran/run_live_session.sh /root/nsoran/live_bridge_server.py \
 && chown builder:builder /home/builder/ns-O-RAN-flexric /home/builder/ns-O-RAN-flexric/mmwave-LENA-oran

ENV LD_LIBRARY_PATH=/usr/local/lib:/usr/local/lib/flexric
# 8090 = batch KPM/validation (engine_service.py); 8091 = live external-xApp
# bridge (live_bridge_server.py) — a customer's xApp controls the running sim.
EXPOSE 8090 8091
CMD ["bash", "-c", "python3 /root/nsoran/live_bridge_server.py & exec python3 /root/nsoran/engine_service.py"]
