# NSG AI Platform

AI-assisted **5G/RAN scenario design, simulation and RIC testing**. You describe a
network in prose, an LLM turns it into a validated configuration, real simulation
engines run it, and the results come back as measured KPM — throughput, PRB, SINR,
energy — over a real E2AP/SCTP link to a real near-RT RIC.

The thing that makes this more than a config generator: **the numbers are
measured, not computed.** The high-fidelity path is ns-3 mmWave + FlexRIC, and
the KPIs it reports come off the wire as E2SM-KPM indications from an actual E2
agent, not from a formula.

---

## What it is honestly good at, and what it is not

Two engines, and the difference matters more than the speed column suggests:

| Tier | What it is | Speed | Use it for |
|---|---|---|---|
| **Analytical core** (`nsg_ai/ric_sim.py`) | our own per-tick model, in-process | instant | the RIC Test live loop, xApp development, quick iteration |
| **ns-O-RAN** (`nsg-engine` container) | ns-3.42 mmWave RAN + FlexRIC, real E2AP/SCTP | ~0.04 simulated seconds per wall second | final validation, anything a customer will question |
| **Coverage grid** (`ns3_bridge.rsrp_heatmap`) | analytic path loss over a grid, in-process | seconds | the RSRP heatmap in Analytics |

The ns-O-RAN tier is **slow on purpose** — it simulates a real channel, PHY, MAC,
scheduler and HARQ per TTI. Measured levers to speed it up are exhausted and
recorded: the best single configuration lever is ~1.3×, everything stacked is
~1.3×, and real time is ~26× away. Do not plan on making it fast; plan on running
it in parallel (`tools/engine_pool_up.sh`, ~3.8× measured across instances).

A batch run therefore covers **a few simulated seconds**, not minutes. That is a
property of the design, not a bug — the KPM collection window *is* the horizon,
and `settings.kpm_collect_s` trades wall clock for simulated time linearly.

---

## Architecture

```
  ui/          Next.js + React + Zustand
               chat · Visual · Analytics · RIC Test · Campaign · Compare
                  │  HTTP + WebSocket
  nsg_ai/      FastAPI backend
               LLM scenario agent · config schema + validation · REST · WS
               local JWT auth · PostgreSQL persistence
                  │  HTTP to the engine's two ports
  engines/     external compiled trees (NOT in this repo — they are GBs)
               nsg-engine container: :8090 batch validation, :8091 live bridge
```

The engine lives in its own WSL distro (`Ubuntu-2004-nsoran`) as a Docker
container. Engine source is version-controlled here in `engines/nsoran/`, but it
only reaches the product through a rebuilt image — see `engines/README.md`.

---

## Services and ports

| Service | Where | Port |
|---|---|---|
| Frontend (Next.js) | platform distro | 3000 |
| Backend (FastAPI) | platform distro | 8000 |
| Engine — batch validation | `Ubuntu-2004-nsoran`, container | 8090 |
| Engine — live bridge | `Ubuntu-2004-nsoran`, container | 8091 |
| PostgreSQL | platform distro | 5433 |

Engine ports bind to loopback only, deliberately: the engine API trusts its
caller by design, so it must not be reachable from outside the box.

---

## Quick start

```bash
# everything, in order, from Windows — engine distro then platform distro
tools/nsg_demo_up.bat
```

Or by hand:

```bash
# backend — must run from nsg_ai/, the top-level `import ns3_bridge` needs it
cd nsg_ai && ../venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000

# frontend
cd ui && npm run dev            # http://localhost:3000

# engine (only when you need the real E2 path)
wsl -d Ubuntu-2004-nsoran
  dockerd &                     # not started automatically
  docker start nsg-engine
```

**When anything looks dead, run this first** — it proves every link from where it
is actually needed, and tells you the fix for each broken one:

```bash
python3 tools/preflight.py          # add --fix to repair what it can
```

A known trap: WSL suspends an idle distro, which kills a detached backend or
frontend and makes the UI say "Failed to fetch". `nsg_demo_up.bat` ends in a
blocking keepalive for exactly this reason — keep that window open.

---

## Verifying a change

One command, three tiers, and it always prints what it did **not** check:

```bash
python3 tools/verify.py --fast      # unit + contract, geometry, schema, UI types
python3 tools/verify.py             # + preflight and engine/repo sync
python3 tools/verify.py --all       # + engine acceptance (slow, needs a container)
```

`tools/README.md` indexes every tool and says when to reach for it.

---

## Where things are

| Piece | Path |
|---|---|
| Frontend | `ui/` |
| Backend, schema, LLM agent | `nsg_ai/` |
| Engine glue (scripts, Dockerfile, patches) | `engines/nsoran/` |
| Engine map — what each engine is and where it lives | `engines/README.md` |
| Tool index | `tools/README.md` |
| System overview (Turkish) | `docs/SISTEM.md` |
| Data inventory and config maturity report (Turkish) | `docs/engine-veri-ve-config-durumu.md` |
| Database migrations | `alembic/` |

---

## Working on this codebase

Three habits this repo is built around, each learned from a defect that hid for
weeks:

**Measure, don't derive.** Constants fitted to old measurements have been wrong
here more than once — a UE ceiling that was really a hardcoded sleep in the xApp,
a span law fitted to that same window. When a claim matters, re-measure it.

**A parameter can be clamped in six places.** Schema, `nsoran_params`,
`run_scenario.sh`, the scenario's GlobalValue checker, the scenario's consumer,
and the C++ model. A source read is not proof that a value reaches the engine —
run it and check the log. `tools/param_audit.py` measures this for the whole
schema.

**When a run comes back empty, read `/tmp/scn0.log` first.** ns-3 rejects an
out-of-range argument at parse time and exits before the RAN starts; the run then
returns a bare `no_data` and nothing says why.

Engine changes reach the product **only** through a rebuilt image
(`engines/nsoran/build-slim.sh`). A host rebuild changes nothing that the product
runs — `tools/engine_sync.py` checks repo, host and image agree, and `verify.py`
fails on drift.
