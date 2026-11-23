# `engines/` — simulation engine map

The NSG platform is a thin AI/orchestration layer over several **external**
simulation engines. The engines themselves are large compiled trees (GBs) that
live **outside** this repo (correctly — you don't commit gigabytes of ns-3 build
output to git). This folder is the single place that documents **what each
engine is, where it physically lives, and how the backend calls it** — plus it
version-controls the small glue/config that isn't tracked anywhere else.

> ℹ️ The old **Tera5G/VIAVI** analytics stack (Grafana/InfluxDB docker-compose,
> once driven by the now-dead `config_merger.py`) is **RETIRED** — moved out of
> the repo to `~/engine` and no longer used by the system. Ignore it.

## The three engines

| Engine | Fidelity | What it produces | Physically lives in | Called by |
|---|---|---|---|---|
| **Analytical core** | fast (live) | RIC KPM per-tick (throughput/PRB/QoS/SINR est.) | in-repo Python (`nsg_ai/ric_sim.py`) | RIC Test live loop |
| **Coverage grid** | static | RSRP coverage heatmap | in process, no binary (`ns3_bridge.rsrp_heatmap`) | `POST /run-simulation` → `ns3_bridge.py` |
| **ns-O-RAN** | highest (real E2AP/SCTP) | measured throughput/PRB/SINR over real E2 | distro `Ubuntu-2004-nsoran` → `nsg-engine:1.25` | `:8090` batch validation · `:8091` live bridge |

> A former **ns-3.47 + 5G-LENA** validation tier (`POST /ric/validate-ns3`) was
> **removed** — it produced KPMs via FlowMonitor without touching the O-RAN
> interface, making it redundant with the ns-O-RAN engine (which gives the same
> throughput/PRB/SINR over *real* E2AP) and an extra host-path dependency that
> would only complicate cloud/K8s deploy. Fidelity ground-truth now comes solely
> from ns-O-RAN; the analytical core covers any topology size live.

Only **ns-O-RAN** has its source-of-truth glue checked in here (`nsoran/`),
because those scripts existed only inside the container. The other two are
invoked by env-var-configured paths from `nsg_ai/*.py`; see each engine's row
for the file that drives it.

## Why the engines aren't moved into the repo

They're multi-GB, environment-specific compiled artifacts (the ns-O-RAN one is
even built for a different OS/distro on purpose, for isolation). Git tracks the
**glue and the map**; the heavy trees stay where they're built. The ns-O-RAN
image was slimmed from a 16.9 GB whole-distro snapshot to **~1.6 GB**
(currently `nsg-engine:1.25`); see [`nsoran/README.md`](nsoran/README.md).

The tag moves with every engine change, so treat the number above as "whatever
`tools/engine_sync.py` says is running" rather than a constant — engine code
reaches the product ONLY through a rebuilt image, and drift is silent.
