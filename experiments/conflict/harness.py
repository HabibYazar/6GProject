"""
Closed-loop experiment harness — an EXTERNAL xApp, and nothing more.

This package deliberately imports NOTHING from nsg_ai. It speaks the same public
WebSocket contract a customer's xApp speaks (/ws/e2-real): the first message is
the scenario config, and every KPM indication after that is an opportunity to
write control. That is the entire reason the conflict experiment needs no change
to the platform — the capability it needs is one the platform already ships and
advertises. Delete this folder and the platform is bit-identical.

What the harness adds over the shipped connector template is bookkeeping: the
experiment has to distinguish what the xApps ASKED for from what the conflict
policy actually SENT, because the gap between those two is the measurement.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import websockets
import pathlib as _pl, sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[2] / 'tools'))
from _backend_auth import auth_headers  # noqa: E402

WS_URL = "ws://localhost:8000/ws/e2-real"

# The engine has to boot a real ns-3 mmWave scenario before the first indication
# lands; ~40-70 s is normal and a cold container is slower. Past that something
# is wrong and waiting longer only hides it.
BOOT_TIMEOUT_S = 240.0
# Between indications the engine is steady-state — but "steady" is not "prompt".
# Measured inter-indication gaps grew across a long batch of back-to-back
# sessions: ~57-75 s early, 93.7 s later, 119.3 s by the 30th run. At the old
# 120 s ceiling that aborted three healthy runs and reported them as "engine went
# quiet", which read like a crash and was not one. 300 s keeps the guard useful
# against a genuinely dead run while leaving room for a slow one.
TICK_TIMEOUT_S = 300.0


@dataclass
class Tick:
    """One KPM indication plus what control did with it."""

    tick: int
    wall_s: float
    energy_w: float | None
    per_cell_w: dict[str, float]
    cells: list[dict[str, Any]]
    ues: list[dict[str, Any]]
    measurements: dict[str, dict[str, float]]
    handovers: int | None
    # Sample identity, straight from the platform contract. A frame is NOT an
    # independent observation: measured 10.6 frames per simulated second against
    # a ~1 Hz E2 KPM round. `kpm_fresh` marks the one frame per round that
    # actually carried a new KPM report; `sim_t` groups the frames that describe
    # the same instant. Any statistic over E2 quantities (throughput, PRB, SINR,
    # delay) should filter or group on these rather than count ticks.
    sim_t: float | None = None
    kpm_fresh: bool | None = None
    # The cells whose report arrived in THIS frame. Per-cell statistics should
    # keep a frame only for the cells named here — a frame marked fresh carries
    # a new measurement for those cells and repeats everyone else's.
    kpm_fresh_cells: list[int] = field(default_factory=list)
    # Bookkeeping the experiment needs and the platform has no opinion about.
    requests: list[dict[str, Any]] = field(default_factory=list)
    applied: list[dict[str, Any]] = field(default_factory=list)
    note: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunLog:
    ticks: list[Tick] = field(default_factory=list)
    status_lines: list[str] = field(default_factory=list)
    error: str | None = None
    boot_s: float | None = None
    wall_s: float | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and len(self.ticks) > 0


def _energy(kpm: dict) -> tuple[float | None, dict[str, float]]:
    """Measured radio energy. `per_cell` is {cell: {j, w}} in the live contract."""
    e = kpm.get("energy") or {}
    total = e.get("total_w")
    per_cell: dict[str, float] = {}
    for cell, v in (e.get("per_cell") or {}).items():
        if isinstance(v, dict) and v.get("w") is not None:
            per_cell[str(cell)] = float(v["w"])
    return (float(total) if isinstance(total, (int, float)) else None), per_cell


async def run_session(
    config: dict,
    controller: Callable[[dict], Any] | None = None,
    *,
    max_ticks: int = 40,
    ws_url: str = WS_URL,
    on_tick: Callable[[Tick], None] | None = None,
    stop: Callable[[], bool] | None = None,
) -> RunLog:
    """Run ONE closed-loop session and return everything it produced.

    `controller(kpm)` may return a list of command dicts, or a
    (commands, note) pair when it wants to record why. Returning None or an
    empty list is a valid "observe only" tick — arm A (no xApp) is exactly that.

    Run length is set by the client, not by simulated time: the socket closes
    once `stop()` says the run has what it came for, or `max_ticks` is reached.
    Stopping on a condition rather than a fixed count is what makes the arms
    comparable — the attach ramp takes a different number of indications from
    seed to seed, so a fixed count would hand some arms more settled control
    intervals than others.
    """
    log = RunLog()
    t0 = time.monotonic()
    try:
        # The token, when the backend enforces auth. A browser has to put it in
        # the query string; this is not a browser, so it uses the header form.
        _hdrs = auth_headers("http://localhost:8000")
        async with websockets.connect(
                ws_url, max_size=16 * 1024 * 1024,
                additional_headers=_hdrs or None) as ws:
            # First message IS the scenario — this is what makes the external
            # xApp able to run its own experiment rather than only join one.
            await ws.send(json.dumps({"config": config}))

            deadline = t0 + BOOT_TIMEOUT_S
            while len(log.ticks) < max_ticks:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=max(1.0, deadline - time.monotonic())
                    )
                except asyncio.TimeoutError:
                    log.error = (
                        "no KPM indication before boot timeout"
                        if not log.ticks
                        else f"engine went quiet after tick {log.ticks[-1].tick}"
                    )
                    break

                msg = json.loads(raw)
                mtype = msg.get("type")

                if mtype == "live_status":
                    line = str(msg.get("line") or msg.get("state") or "")
                    if line:
                        log.status_lines.append(line)
                    continue
                if mtype == "error":
                    log.error = str(msg.get("message") or "engine error")
                    break
                if mtype != "kpm":
                    continue

                if log.boot_s is None:
                    log.boot_s = time.monotonic() - t0
                # Past the first indication the engine is producing; a stall from
                # here is a failure, not a slow boot.
                deadline = time.monotonic() + TICK_TIMEOUT_S

                total_w, per_cell_w = _energy(msg)
                t = Tick(
                    tick=int(msg.get("tick") or len(log.ticks)),
                    wall_s=round(time.monotonic() - t0, 3),
                    energy_w=total_w,
                    per_cell_w=per_cell_w,
                    cells=list(msg.get("cells") or []),
                    ues=list(msg.get("ues") or []),
                    measurements=dict(msg.get("measurements") or {}),
                    handovers=msg.get("handovers"),
                    sim_t=msg.get("sim_t"),
                    kpm_fresh=msg.get("kpm_fresh"),
                    kpm_fresh_cells=list(msg.get("kpm_fresh_cells") or []),
                )

                if controller is not None:
                    out = controller(msg)
                    if isinstance(out, tuple):
                        cmds, note = out
                        t.note = dict(note or {})
                    else:
                        cmds = out
                    for cmd in list(cmds or []):
                        await ws.send(json.dumps(cmd))
                        t.applied.append(cmd)
                    t.requests = list(t.note.pop("requests", []) or [])

                log.ticks.append(t)
                if on_tick is not None:
                    on_tick(t)
                if stop is not None and stop():
                    break

    except Exception as exc:  # noqa: BLE001 — the reason matters more than the type
        log.error = log.error or f"{type(exc).__name__}: {exc}"

    log.wall_s = round(time.monotonic() - t0, 2)
    return log


def run(config: dict, controller=None, **kw) -> RunLog:
    """Blocking wrapper — the experiment scripts are plain sequential code."""
    return asyncio.run(run_session(config, controller, **kw))
