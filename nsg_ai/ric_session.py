"""
NSG AI Platform — RIC Test session sharing

Lets multiple WebSocket clients drive/observe ONE shared RicSimulation, which is
what turns the RIC test from a single-user console into a real test bed:

  - The browser opens a session (creator) → gets a short session id.
  - The user's own xApp (an external process — their AI under test) joins that
    same id and sends control commands over /ws/e2.
  - Both see the same sim: one broadcast KPM stream, and every control action
    (from the browser OR the xApp) is applied to the shared sim and echoed to
    all clients as a control_event, so the browser observes what the AI did.

Everything runs inside FastAPI's single asyncio event loop, so no locking is
needed — RicSimulation.apply_control is synchronous and therefore atomic
between await points.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import WebSocket

from ric_sim import RicSimulation
from schema import NSGSimulationConfig

logger = logging.getLogger(__name__)


class RicSession:
    """One shared RAN-under-test + its connected clients."""

    def __init__(self, cfg: NSGSimulationConfig, interval_s: float):
        self.id = uuid.uuid4().hex[:8]
        self.sim = RicSimulation(cfg, window_s=interval_s)
        self.interval_s = interval_s
        # HOW FAST SIMULATED TIME RUNS AGAINST THE WALL CLOCK.
        #
        # 1.0 is real time and it is the default for a reason that is the
        # whole point of a RIC test rig: what is under test is the xApp, and
        # an xApp is a CONTROL LOOP whose deadline O-RAN defines in wall
        # clock — near-real-time is 10 ms to 1 s. Run the RAN faster and the
        # loop's timing becomes fiction: at 100x, a control computed from
        # report N reaches a RAN that is already 100 simulated seconds past
        # it, so the closed loop being tested is not the one that will ship.
        #
        # Above 1.0 is for watching a scenario play out, not for testing a
        # controller, and `rate_warning()` says so rather than leaving the
        # reader to work it out.
        #
        # This tier can sustain about 200x on a six-cell scene (4.6 ms of
        # compute for a 1 s window). The ceiling is not the model, it is what
        # the loop's own consumers can take.
        self.rate = 1.0
        # Whether the loop is actually keeping the rate it was asked for.
        self.rate_achieved: float | None = None
        self.clients: dict[WebSocket, str] = {}   # ws -> label (e.g. "observer", "xapp")
        self._loop_task: asyncio.Task | None = None
        # AI xApp (LLM-driven controller running inside the session).
        self._ai_task: asyncio.Task | None = None
        self.ai_goal: str | None = None
        self._ai_recent: list[dict] = []

    # ── membership ────────────────────────────────────────────────────────────

    def add_client(self, ws: WebSocket, label: str) -> None:
        self.clients[ws] = label
        if self._loop_task is None:  # first client starts the broadcast loop
            self._loop_task = asyncio.create_task(self._broadcast_loop())

    def remove_client(self, ws: WebSocket) -> None:
        self.clients.pop(ws, None)
        if not self.clients and self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None

    @property
    def is_empty(self) -> bool:
        return not self.clients

    def roster(self) -> list[str]:
        return sorted(self.clients.values())

    # ── KPM broadcast loop ────────────────────────────────────────────────────

    def rate_warning(self) -> str | None:
        """What is untrue about the current rate, in one sentence, or None."""
        if self.rate <= 1.0:
            return None
        return (f"{self.rate:g}x real time: the RAN advances {self.rate:g} "
                f"simulated seconds per wall second while an xApp still "
                f"thinks and answers in wall time, so every control action "
                f"lands about {self.rate:g} simulated seconds late. Good for "
                f"watching a scenario, not for testing a controller.")

    async def _broadcast_loop(self) -> None:
        """One KPM window per `interval_s / rate` of wall clock.

        SLEEPING A FIXED AMOUNT AFTER THE WORK IS NOT PACING, it is pacing
        plus a leak. The old loop did `broadcast; tick; sleep(interval)`, so
        the period was compute PLUS the interval and the simulated clock fell
        behind the wall clock by the compute — 0.46% measured, which is 16
        seconds of simulated time lost per wall hour, and much more as the
        scenario grows. Sleeping to a DEADLINE spends the compute out of the
        interval instead of on top of it.
        """
        try:
            next_at = asyncio.get_running_loop().time()
            behind = 0
            while True:
                period = max(1e-3, self.interval_s / max(self.rate, 1e-6))
                await self.broadcast({
                    "type": "kpm", "rate": self.rate,
                    "rate_achieved": self.rate_achieved,
                    "rate_warning": self.rate_warning(),
                    **self.sim.get_kpm_report()})
                self.sim.tick()
                next_at += period
                slack = next_at - asyncio.get_running_loop().time()
                if slack > 0:
                    behind = 0
                    await asyncio.sleep(slack)
                else:
                    # The loop cannot keep this rate. Say so rather than
                    # quietly running as fast as it can and letting the
                    # session claim a speed it is not holding.
                    behind += 1
                    if behind in (5, 50, 500):
                        logger.warning(
                            "RIC session %s is %.1f s behind a %gx schedule "
                            "after %d windows — the rate is not being held",
                            self.id, -slack, self.rate, behind)
                    self.rate_achieved = self.interval_s / max(
                        1e-6, period - slack)
                    next_at = asyncio.get_running_loop().time()
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("RIC session %s broadcast loop crashed", self.id)

    async def broadcast(self, msg: dict) -> None:
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                # Client is gone; membership cleanup happens in the handler's
                # finally block, but drop it here too so we stop spamming it.
                self.clients.pop(ws, None)

    # ── control plane ─────────────────────────────────────────────────────────

    # How fast simulated time may be asked to run. The ceiling is not the
    # model's — it sustains ~200x on six cells — it is that past a point the
    # loop spends more time broadcasting than simulating and the number on
    # the screen stops being the number being held.
    RATE_CHOICES = (1.0, 2.0, 5.0, 10.0, 25.0, 50.0)

    async def set_rate(self, rate: float, source: str) -> dict:
        """Change how fast simulated time runs, and tell everyone.

        It is a SESSION property, not a client one: the RAN is shared, so a
        second observer cannot be shown a different clock from the xApp
        driving it.
        """
        try:
            r = float(rate)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"rate must be a number, got {rate!r}"}
        if not (0.1 <= r <= 100.0):
            return {"ok": False, "error": "rate must be between 0.1x and 100x"}
        self.rate = r
        self.rate_achieved = None
        ack = {"ok": True, "rate": r, "warning": self.rate_warning()}
        await self.broadcast({"type": "rate_event", "rate": r,
                              "warning": self.rate_warning(), "source": source})
        logger.info("RIC session %s rate -> %gx (%s)", self.id, r, source)
        return ack

    async def apply_control(self, cmd: dict, source: str) -> dict:
        """Apply a control command to the shared sim and echo it to every client
        as a control_event (so observers see what any controller did). Returns
        the ack for the direct sender."""
        # `rate` is a rig control, not a RAN control — it changes how fast the
        # world runs, not anything in it — so it is handled here rather than
        # being passed to the sim, which would not know what to do with it.
        if cmd.get("action") == "set_rate":
            return await self.set_rate(cmd.get("rate", 1.0), source)
        ack = self.sim.apply_control(cmd)
        await self.broadcast({
            "type": "control_event",
            "command": cmd,
            "ack": ack,
            "source": source,
        })
        return ack

    async def inject_anomaly(self, cmd: dict, source: str) -> dict:
        """Inject an operator anomaly (cell fault / traffic surge) into the shared
        sim and echo it to every client as an anomaly_event, so all observers —
        and the AI under test — see the incident begin."""
        ack = self.sim.inject_anomaly(cmd)
        await self.broadcast({
            "type": "anomaly_event",
            "command": cmd,
            "ack": ack,
            "source": source,
        })
        return ack

    # ── AI xApp (LLM-driven controller) ───────────────────────────────────────

    def start_ai(self, goal: str, decide_fn, llm_config: dict | None = None) -> None:
        """Run an internal LLM-driven xApp against this session. `decide_fn` is
        (goal, kpm, recent, llm_config) -> list[command dict] (injected so this
        module stays decoupled from the LLM layer). `llm_config` optionally
        selects the user's own model instead of the built-in one."""
        self.ai_goal = goal
        self._ai_recent = []
        if self._ai_task is None:
            self._ai_task = asyncio.create_task(self._ai_loop(decide_fn, llm_config))

    def stop_ai(self) -> None:
        self.ai_goal = None
        if self._ai_task is not None:
            self._ai_task.cancel()
            self._ai_task = None
        # Undo the AI's control actions so stopping it returns the network to baseline
        # (otherwise a cells-off / degraded state persisted and looked like a full outage
        # until the user reconnected for a fresh session).
        self.sim.reset_controls()

    async def _ai_loop(self, decide_fn, llm_config: dict | None) -> None:
        # LLM calls are slow/costly, so decide a few seconds apart, not per tick.
        ai_interval = max(3.0, self.interval_s * 4)
        try:
            while self.ai_goal:
                kpm = self.sim.get_kpm_report()
                # Run the (blocking) LLM call off the event loop.
                cmds = await asyncio.to_thread(
                    decide_fn, self.ai_goal, kpm, self._ai_recent, llm_config
                )
                for cmd in cmds:
                    await self.apply_control(cmd, "ai-xapp")
                    self._ai_recent.append(cmd)
                self._ai_recent = self._ai_recent[-10:]
                await asyncio.sleep(ai_interval)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("AI xApp loop crashed on session %s", self.id)

    # ── teardown ──────────────────────────────────────────────────────────────

    async def close_all(self) -> None:
        """End the whole session: stop AI + broadcast loop, close every client
        socket. Called when the session owner (browser) disconnects."""
        self.stop_ai()
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None
        for ws in list(self.clients):
            try:
                await ws.close()
            except Exception:
                pass
        self.clients.clear()


class RicSessionManager:
    """Registry of active RIC test sessions, keyed by short id."""

    def __init__(self) -> None:
        self.sessions: dict[str, RicSession] = {}

    def create(self, cfg: NSGSimulationConfig, interval_s: float) -> RicSession:
        session = RicSession(cfg, interval_s)
        self.sessions[session.id] = session
        logger.info("RIC session %s created", session.id)
        return session

    def get(self, session_id: str) -> RicSession | None:
        return self.sessions.get(session_id)

    def drop_if_empty(self, session: RicSession) -> None:
        if session.is_empty:
            self.sessions.pop(session.id, None)
            logger.info("RIC session %s closed (no clients)", session.id)

    def drop(self, session: RicSession) -> None:
        self.sessions.pop(session.id, None)
        logger.info("RIC session %s dropped", session.id)
