# `tools/` — what each one answers, and when to reach for it

Thirty files with no index is a drawer, not a toolbox. This is the index.

**If you only remember two:** `preflight.py` when something looks dead,
`verify.py` before you call a change done.

---

## Start here

| Tool | The question it answers |
|---|---|
| `verify.py` | *Did I break anything?* One entry point, three tiers (`--fast` / default / `--all`), and it always prints what it did **not** check. |
| `preflight.py` | *Why is nothing working?* Proves every link — both engine ports, the cross-distro path, the database and its migration, the backend, the frontend — from where each is actually needed, and prints the fix for each failure. `--fix` repairs what it can. |

---

## Bringing the system up

| Tool | What it does |
|---|---|
| `nsg_demo_up.bat` | Windows entry point for a demo. Brings up the engine distro (dockerd + container + the port-proxy rebind a cold dockerd leaves dead), then the platform half. Ends in a blocking keepalive — **keep the window open**, or WSL suspends the distro and the UI goes dead. |
| `demo_app_up.sh` | The platform-distro half of the above: verifies the engine is reachable across distros, then starts backend and frontend if down. Idempotent. |
| `engine_pool_up.sh` | Starts N engine instances so campaigns run in parallel (~3.8× measured). One run per instance — the engine uses fixed `/tmp` paths that would cross-contaminate. |
| `nsoran_gui_up.sh` | Brings up **upstream's own** dashboard + Grafana + InfluxDB and runs a sim. Useful as provenance for a sceptic — but it runs its own sim on the host tree, **not** our container's run. Say so if you show it. |

## Is the engine still the engine

| Tool | The question it answers |
|---|---|
| `engine_sync.py` | *Does the product actually carry my engine change?* Engine code lives in three places (repo → host → image) and reaches the product only through the image. Drift is silent. `--push` verifies by md5. |
| `engine_acceptance.py` | *Is the engine still telling the truth?* Compares the engine's own roster against the platform's view, plus physical invariants. Takes positional args (`[n_gnb] [n_ue]`), not flags. |
| `test_acceptance_detects.py` | *Would that suite notice if it were lying?* Feeds each acceptance check data that violates it and requires a failure. Tests the detector, not the engine — no container needed. |
| `param_audit.py` | *Which schema fields actually reach ns-3?* Perturbs each field and diffs the payload. `--cached --check` is the CI form and needs no engine. |
| `test_path_parity.py` | *Does the live path drop keys the batch path honours?* One payload, two consumers. Fourteen keys were once dropped by live, including fault injection — and every one passed `param_audit`. |
| `fidelity_report.py` | *Does each config lever move the real engine?* Proves levers end to end and compares tiers. Run after any engine or config change. |

## Finding the limits

| Tool | The question it answers |
|---|---|
| `envelope_sweep.py` | *Which points in the declared envelope actually run?* Only `--via-api` tells the truth — the bare binary installs no traffic and passes points the product aborts on. |
| `horizon_sweep.py` | *How much simulated time does a batch run observe?* The KPM collection window **is** the horizon, and the trade is linear. Uses `horizon_base.json` as the one scenario it stretches, so a cell count is never confounded with a different topology. |
| `combo_stress.py` | *Do parameters break each other?* Every parameter is tested alone; the bugs that survive live in combinations — a fault on a sleeping cell, a slice policy on a 5QI no UE carries. |
| `target_search.py` | *What config gets me 50 Mbps?* Greedy coordinate search over the real engine, with the measured noise floor, busy-retry and confirmation runs for near-noise candidates. |
| `qa_api.py` | *Does a bad request get a 4xx with a reason?* A 500 is an unhandled path; a 200 on nonsense is worse, because it surfaces somewhere else entirely. |
| `security_probe.py` | *Can anything a user sends through the backend reach a shell?* Config values become environment variables and the run script interpolates many of them unquoted. This has found a real command execution once. |

## Accounts

| Tool | What it does |
|---|---|
| `nsg_user.py` | User administration from the command line: `list`, `create`, `passwd`, `rename`, `role`, `disable`, `enable`, `delete`. Talks to the database directly, so it works when the backend is down and needs no token — the escape hatch if the only administrator is locked out. Passwords come from a prompt, never an argument. |

## Scheduled / long-running

| Tool | What it does |
|---|---|
| `nightly_fidelity.sh` | The night watchman: brings the engine up and runs the fidelity and audit passes into `reports/`. |
| `nightly_campaign.py` | Scenario regression watch, driven by `nightly_campaign.json`. |
| `fidelity_boot_code.txt` | One line: the `wsl -d Ubuntu -e bash …` invocation to paste into Windows Task Scheduler for the nightly. |

## Tests that `verify.py` runs for you

`test_geometry.py` (a UE on a base station returns zero DU metrics *silently*)
runs in the fast tier along with `pytest nsg_ai/`, `param_audit.py --cached
--check`, the frontend type check and `test_map_import.mjs`. You rarely need to
call any of them directly.

`test_map_import.mjs` covers the map's **Import buildings** button in sparse
areas — the far-north bbox the cos(lat) transform pushes past ±180, and the size
filter that eats rural sheds and greenhouses so a real fetch imports nothing. It
reads `NetworkMap.tsx` off disk and runs its helpers, so it follows the file. The
`fetch` to Overpass needs a browser and is **not** covered: click the button.

---

## Data files, and which are inputs

| File | Role |
|---|---|
| `horizon_base.json` | **Input.** The one full platform config that `horizon_sweep.py` and `target_search.py` stretch. |
| `nightly_campaign.json` | **Input.** The nightly suite definition. |
| `envelope_sweep.latest.json` | Last sweep result; `envelope_sweep.py` reads it back. |
| `envelope_sweep.api.json` | The `--via-api` sweep result — the product-path one, so the more meaningful of the two. |
| `horizon.1_12.json` | Measured spans, read by `horizon_sweep.py` **and** `nsg_ai/engine_envelope.py`. |
| `horizon.window_ladder.json` | Recorded evidence that the collection window is the horizon, with the numbers behind it. |
| `.engine_params.json` | Cache for `param_audit --cached`. Git-ignored, regenerable. |

---

## Two things to know before trusting a measurement here

**WSL suspends the engine distro.** A measurement driven from the platform distro
will sit blocked while the container makes no progress, and the run's own
wall-clock timeouts can then fire against a simulation that never advanced. Hold
one blocking `wsl.exe -d Ubuntu-2004-nsoran -- tail -f /dev/null` open for the
whole measurement, and check elapsed time **inside** the container rather than a
local file's mtime.

**Never inspect the engine container mid-measurement.** Killing a `docker exec`
does not kill the process inside the container, and a probe that reads a blocking
source (`/proc/1/fd/1`, `tail -f`) hangs forever. Put `timeout N` in front of any
container-side command.
