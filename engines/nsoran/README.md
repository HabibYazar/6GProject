# ns-O-RAN engine (real E2AP / SCTP)

The **highest-fidelity** engine: EURECOM **FlexRIC** (a real near-RT RIC) +
**ns-3.42 mmWave** RAN acting as a real **E2 agent**, talking real **E2AP/SCTP**
with real **E2SM-KPM v3 / E2SM-RC** service models. It measures throughput / PRB
/ SINR from an actual 5G stack instead of estimating them.

## Files here (version-controlled source of truth)

| File | Role |
|---|---|
| `run_scenario.sh`   | Orchestrator: boots RIC → ns-3 RAN → KPM xApp, prints the E2 handshake + KPM values, self-cleans. Bounded so it can never hang. |
| `engine_service.py` | Zero-dep Python 3.8 HTTP API around the orchestrator. `GET /health`, `POST /run {sim_time}` → KPM summary JSON. |
| `Dockerfile`        | Lean runtime image recipe (**built & verified**, 1.48 GB — see "Slimming"). |
| `build-slim.sh`     | Assembles the context from the distro's artifacts and builds `nsg-engine:1.0`. |

## Where it actually runs

The compiled engine (FlexRIC + ns-3.42 build tree, ~GBs) lives in a **separate
isolated WSL distro**, `Ubuntu-2004-nsoran`, so it can never break the 24.04 dev
box. It is packaged as the Docker image **`nsg-engine:1.0`** (lean, 1.48 GB) and
run there:

```bash
# inside distro Ubuntu-2004-nsoran (start docker once: `nohup dockerd &`)
docker run -d --name nsg-engine \
  --security-opt seccomp=unconfined \    # SCTP needs relaxed seccomp
  -p 8090:8090 \
  nsg-engine:1.0 python3 /root/nsoran/engine_service.py
```

The NSG backend (24.04) reaches it at `http://localhost:8090` thanks to **WSL
mirrored networking** (both distros + Windows share `localhost`). Backend
endpoint: `POST /ric/validate-nsoran?sim_time=20` (`NSG_NSORAN_URL` overrides
the target). UI: the amber **“ns-O-RAN — Real E2AP Validation”** panel on the
RIC Test tab.

## Config-driven scenario

The engine is driven by the platform's `NSGSimulationConfig`, not a fixed scenario:

- **Level 1** (no rebuild — plain ns-3 GlobalValues): cell count (`N_MmWaveEnbNodes`),
  UE count (`N_Ues`), `CenterFrequency`, `Bandwidth`, `IntersideDistanceCells/UEs`.
- **Level 2** (needs the scenario C++ + a rebuild): **exact per-gNB and per-UE
  positions**. The backend translates the config's coordinates so their centroid
  maps to the scenario centre (fixed 4000×4000 area, hub at 2000,2000), clamps
  them, and passes them as `;`-separated `x,y` strings. `run_scenario.sh` writes
  those to files and passes `--gnbPositionsFile` / `--uePositionsFile`; the
  scenario places each node exactly there (empty ⇒ falls back to the auto
  ring/disc). A 1 LTE hub is always added at the centre.

The Level-2 change to `scratch/scenario-zero-with_parallel_loging.cc` (in the
external builder tree, not this repo) is captured as **`scenario-zero-level2.patch`**.
To reproduce: `patch -p1 < scenario-zero-level2.patch` in `mmwave-LENA-oran/`, then
rebuild (`runuser -u builder -- ./ns3 build`) and rebuild the image (`build-slim.sh`).

**Reproducibility:** the scenario fixes the ns-3 RNG (`RngSeedManager::SetSeed(1)`,
run = `rngRun` GlobalValue, default 1) so the **same config reproduces the same
run** — SINR is identical run-to-run, throughput/PRB within ~0.2% (residual comes
from the real-time E2 sampling instant). Vary `rngRun` for multi-seed averaging.

**Data completeness:** `engine_service.py` captures **every** decoded E2SM-KPM
measurement (~31, aggregated per canonical name across UEs/cells), not a curated
subset — excludes only non-KPM metadata (UE ids, indication latency). The UI shows
a curated subset; the full set is always in the `measurements` response.

**Still engine defaults (Level 3 TODO):** per-slice/QoS traffic (currently a fixed
UDP DL per UE), per-gNB tx power, buildings, and **UL metrics** (no UL traffic +
the DU E2 report only lists `*Dl` names, though `ulPrbUsage` is computed internally).

## Slimming the image (16.9 GB → 1.48 GB) — DONE

The old `nsg-engine:0.1`/`:0.2` were whole-distro `tar | docker import` snapshots
(compilers, `-dev` pkgs, `.o` object files, apt caches — all of it). `build-slim.sh`
rebuilds from **only the runtime artifacts**, producing **`nsg-engine:1.0` at
1.48 GB** (11.4× smaller, same measured KPM output). Just run it in the distro:

```bash
bash build-slim.sh              # -> nsg-engine:1.0
```

How it got lean:
- **Base:** `ubuntu:20.04` (72 MB) instead of the whole build distro.
- **Runtime deps only:** `ldd` on the three binaries showed the *only* extra
  shared libs are `libsctp1` + `libsqlite3-0` (libc6/libgcc-s1/libstdc++6 come
  with the base) — **no boost/eigen/gsl**. Plus `procps` (`pkill`) + `python3`.
  apt adds just ~26 MB.
- **Artifacts copied:** the two FlexRIC binaries (10 MB) + Service Models (8 MB),
  the ns-3 scenario binary (4 MB) + its `build/lib` (**1.3 GB — the bulk**), and
  the engine scripts.

**Gotcha baked into the recipe:** ns-3 runs as user `builder` and writes trace
`.txt` files to its cwd (`$NS3DIR`); the COPY-created parent dirs are root-owned,
so the Dockerfile chowns those two dirs to `builder` (else the run dies with
`NS_FATAL "Could not open tracefile"` and returns 0 E2 agents).

Further shrink (optional): `strip` the `build/lib/*.so` (they're unstripped
debug builds) could cut the 1.3 GB substantially — a future step.
