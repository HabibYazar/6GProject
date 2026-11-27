"""
NSG AI Platform — FastAPI Application  v2.0

Three parallel simulation paths coexist here:

  ┌─ NS-3 PATH (static REM heatmap) ───────────────────────────────────────────┐
  │  POST /ai/modify       LLM generates/modifies NSGSimulationConfig          │
  │  POST /run-simulation  ns3_bridge runs ns3-dev-rem-gen, returns .npy path  │
  │  GET  /schema          JSON schema for the frontend form builder            │
  └────────────────────────────────────────────────────────────────────────────┘

  ┌─ LIVE TRAFFIC PATH (analytical estimator, no subprocess) ──────────────────┐
  │  WS   /ws/live-traffic  streams {time, eMBB, uRLLC, mMTC, voice} frames    │
  │       every settings.report_interval_ms via ns3_bridge.                    │
  │       estimate_live_throughput() — see that function's docstring for the   │
  │       "why not the legacy NS-3 engine" rationale.                          │
  └────────────────────────────────────────────────────────────────────────────┘

  ┌─ LIVE METRICS PATH (InfluxDB analytics engine — legacy/unused) ────────────┐
  │  GET  /live-metrics    InfluxDB KPI query (live analytics stream)          │
  │  POST /generate-report LLM executive summary of simulation metrics         │
  │  GET  /engine-status   Analytics engine health check                       │
  │  POST /stop            Stop the running simulation                         │
  └────────────────────────────────────────────────────────────────────────────┘

  POST /reset   Reinitialise the LLM agent
  GET  /health  Liveness probe
"""
from __future__ import annotations

import asyncio
import datetime
import io
import json
import logging
import math
import os
import traceback
import threading
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import requests
from openai import APIError
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError

import ns3_bridge
import store
import topology
import partition_advice
import partition_run
import partition_live
import partition_pool
from llm_agent import NSGAgent, decide_xapp_actions, AIUnavailableError, ai_configured
from ric_session import RicSessionManager
from nsoran_live import (run_live_proxy, run_live_traffic,
                         run_batch_simulation, summarise_batch_frames)
from campaign import (CampaignSpec, campaign_history, campaign_verdict, diff_configs,
                      diff_campaigns, evaluate, expand, horizon_violations,
                      partition_groups_of)
from engine_pool import pool
import auth as auth_mod
from auth import (Principal, current_principal, require_user, require_admin,
                  auth_required, require_ws)
from schema import (NSGSimulationConfig, WallMaterial, BuildingType,
                    PropagationScenario, QOS_5QI_CATALOGUE)
from engine_envelope import (clamp_gnb, clamp_ue, clamp_bw_mhz, clamp_freq_mhz,
                             ue_alloc, UE_PER_CELL_MAX, config_bw_mhz,
                             config_numerology, GNB_MIN, GNB_MAX, UE_MIN,
                             UE_MAX, UE_PER_CELL_MIN,
                             BW_MHZ_MIN, BW_MHZ_MAX, MIN_CELL_SEPARATION_M,
                             max_ue_per_cell_for_bw)
from xapp_template import XAPP_CONNECTOR_TEMPLATE, XAPP_CONNECTOR_REAL_TEMPLATE


# ═══════════════════════════════════════════════════════════════════════════════
# APPLICATION LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════════════

logger = logging.getLogger(__name__)

_agent: NSGAgent | None = None

# Registry of active RIC test sessions (browser + external xApps share one sim).
ric_manager = RicSessionManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent
    _agent = NSGAgent()
    _hydrate_runs_from_disk()
    _hydrate_campaigns_from_disk()
    yield
    _agent = None


app = FastAPI(
    title="NSG AI Platform",
    version="2.0.0",
    description="AI-driven NS-3 Network Simulation API",
    lifespan=lifespan,
)

# Explicit origins, not "*". The wildcard is invalid together with
# allow_credentials (the browser rejects the pair), and once auth ships the
# frontend sends an Authorization header, which is a credentialed request. The
# list comes from NSG_CORS_ORIGINS (comma-separated) and defaults to the local
# dev frontend.
_cors_origins = [o.strip() for o in os.environ.get(
    "NSG_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
    if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════════════════════
# REQUEST / RESPONSE MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class ModifyConfigRequest(BaseModel):
    """
    Body for POST /ai/modify.

    current_config:  The current NSGSimulationConfig serialised as JSON.
                     Pass {} to have the LLM generate a fresh configuration.
    prompt:          Natural-language modification request.
    history:         Optional prior conversation turns for multi-turn UX.
                     Each entry: {"role": "user"|"assistant", "content": "..."}.
                     Assistant turns should carry the previously returned config
                     as a JSON string so the model retains context.
    """
    current_config: dict[str, Any] = {}
    prompt: str
    history: list[dict[str, str]] = []


class RemPoint(BaseModel):
    """One sampled point from the NS-3 Radio Environment Map (REM)."""
    x: float
    y: float
    rsrp: float


class SimulationRunResult(BaseModel):
    """Response from POST /run-simulation."""
    status: str
    rem_hash: str
    config_path: str
    output_npy: str
    stdout: str
    heatmap: list[RemPoint] = []


class ReportMetrics(BaseModel):
    """Body for POST /generate-report."""
    tput: float
    power: float
    prb: float
    qos: float


# ═══════════════════════════════════════════════════════════════════════════════
# NS-3 PATH ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

# ── Per-user data ownership ──────────────────────────────────────────────────
# Runs, campaigns and scenarios carry an owner_id. Work is stamped with its
# creator, and listings show a user only their own — except an admin, who sees
# everything. When enforcement is OFF the whole thing is transparent: owner_id is
# whatever the (possibly anonymous) caller is, and nothing is filtered, which is
# exactly today's single-tenant behaviour.

def _owner_id(principal: "Principal") -> str | None:
    """Who owns work this caller creates. None when anonymous."""
    return principal.user_id


def _may_see(principal: "Principal", owner_id: str | None,
             include_others: bool = False) -> bool:
    """Ownership visibility for reads.

    Your own row is always visible. Un-owned rows (owner_id None) predate
    authentication and belong to nobody, so an admin sees them and a normal user
    does not. Rows owned by SOMEONE ELSE are visible only to an admin who asked
    for them — `include_others`.

    That last clause is the point: an admin who can see everything by default
    gets a list where their own three campaigns sit among a dozen of other
    people's with nothing to tell them apart. Oversight is still available, it
    is just requested rather than imposed.
    """
    if not auth_required():
        return True
    if owner_id is not None and owner_id == principal.user_id:
        return True
    if not principal.is_admin:
        return False
    return owner_id is None or include_others


# ═══════════════════════════════════════════════════════════════════════════════
# AUTHENTICATION — local login backed by this box's Postgres (see auth.py)
# ═══════════════════════════════════════════════════════════════════════════════

class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


def _public_user(u: dict) -> dict:
    return {"id": u["id"], "username": u["username"], "role": u["role"],
            "disabled": u.get("disabled", False)}


@app.post("/auth/register")
def auth_register(body: RegisterRequest,
                  principal: Principal = Depends(current_principal)) -> dict:
    """Create a login.

    Bootstrap rule: the FIRST user on an empty table becomes the admin, with no
    credential required — a fresh deployment has no one to authorise it. After
    that, only an admin may create users, so registration is not an open door.
    With enforcement off and users already present, an anonymous caller is still
    refused here: creating accounts is the one thing that stays admin-only
    regardless, or the switch would be meaningless.
    """
    username = (body.username or "").strip()
    if not username or len(username) > 64:
        raise HTTPException(status_code=422, detail="username must be 1-64 chars")
    if len(body.password or "") < 8:
        raise HTTPException(status_code=422, detail="password must be at least 8 chars")

    first_user = store.user_count() == 0
    if not first_user:
        if principal.anonymous or not principal.is_admin:
            raise HTTPException(status_code=403,
                                detail="only an administrator may create users")
    if store.get_user_by_username(username):
        raise HTTPException(status_code=409, detail="username already taken")

    role = "admin" if first_user else "user"
    user = store.create_user(uuid.uuid4().hex[:16], username,
                             auth_mod.hash_password(body.password), role)
    logger.info("user created: %s (role=%s, bootstrap=%s)", username, role, first_user)
    return _public_user(user)


@app.post("/auth/login")
def auth_login(body: LoginRequest) -> dict:
    """Exchange username + password for a bearer token.

    One message for both a missing user and a wrong password, so the endpoint
    does not confirm which usernames exist.
    """
    user = store.get_user_by_username((body.username or "").strip())
    if (user is None or user.get("disabled")
            or not auth_mod.verify_password(body.password or "", user["password_hash"])):
        raise HTTPException(status_code=401, detail="invalid username or password")
    store.touch_last_login(user["id"])
    token = auth_mod.create_token(user["id"], user["username"], user["role"])
    return {"access_token": token, "token_type": "bearer",
            "user": _public_user(user)}


@app.get("/auth/me")
def auth_me(principal: Principal = Depends(current_principal)) -> dict:
    """Who the caller is. Anonymous is a valid answer, not a 401 — the frontend
    uses it to decide whether to show a login form, and it reports whether this
    deployment enforces auth at all."""
    # `bootstrap` lets the login screen ask for a first admin instead of a
    # password nobody has yet — the register endpoint allows exactly one
    # anonymous creation while the table is empty.
    return {"authenticated": not principal.anonymous,
            "enforced": auth_required(),
            "bootstrap": store.user_count() == 0,
            "username": principal.username, "role": principal.role,
            "user_id": principal.user_id}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@app.post("/auth/password")
def auth_change_own_password(body: ChangePasswordRequest,
                             principal: Principal = Depends(require_user)) -> dict:
    """Change your OWN password, proving it with the current one.

    The current password is the only verification asked for, and that is
    deliberate. Anything else a reset flow might check — an email address, a
    username, a phone number — is not secret, so accepting it would let anyone
    who knows those facts take the account. A user who has forgotten their
    password cannot prove anything here; on an appliance with no signup and often
    no outbound mail, that case is an administrator's job (/auth/users/{id}/
    password, or tools/nsg_user.py on the box).
    """
    if principal.anonymous:
        raise HTTPException(status_code=401, detail="authentication required")
    if len(body.new_password or "") < 8:
        raise HTTPException(status_code=422,
                            detail="new password must be at least 8 chars")
    user = store.get_user(principal.user_id or "")
    if user is None:
        raise HTTPException(status_code=404, detail="no such user")
    if not auth_mod.verify_password(body.current_password or "",
                                    user["password_hash"]):
        raise HTTPException(status_code=403, detail="current password is wrong")
    if body.current_password == body.new_password:
        raise HTTPException(status_code=422,
                            detail="the new password must differ from the current one")
    store.set_user_password(user["id"], auth_mod.hash_password(body.new_password))
    return {"ok": True,
            "note": f"other sessions stay valid for up to "
                    f"{auth_mod.TOKEN_TTL_HOURS}h"}


@app.get("/auth/users")
def auth_list_users(principal: Principal = Depends(require_admin)) -> dict:
    return {"users": store.list_users()}


@app.post("/auth/users/{user_id}/disable")
def auth_disable_user(user_id: str,
                      principal: Principal = Depends(require_admin)) -> dict:
    if user_id == principal.user_id:
        raise HTTPException(status_code=400, detail="cannot disable your own account")
    if _last_active_admin(user_id):
        raise HTTPException(status_code=409,
                            detail="this is the last active administrator")
    if not store.set_user_disabled(user_id, True):
        raise HTTPException(status_code=404, detail="no such user")
    return {"ok": True}


class SetRoleRequest(BaseModel):
    role: str


class SetPasswordRequest(BaseModel):
    password: str


class SetUsernameRequest(BaseModel):
    username: str


def _last_active_admin(user_id: str) -> bool:
    """Would acting on this user leave the platform with no administrator?

    Guarded on every path that can remove admin access, because there is no
    self-service signup: an appliance with no active admin cannot grant anyone
    access again except through tools/nsg_user.py on the box itself.
    """
    admins = [u for u in store.list_users()
              if u["role"] == "admin" and not u["disabled"]]
    return len(admins) <= 1 and any(u["id"] == user_id for u in admins)


@app.post("/auth/users/{user_id}/role")
def auth_set_role(user_id: str, body: SetRoleRequest,
                  principal: Principal = Depends(require_admin)) -> dict:
    if body.role not in ("admin", "user"):
        raise HTTPException(status_code=422, detail="role must be 'admin' or 'user'")
    if body.role != "admin" and _last_active_admin(user_id):
        raise HTTPException(status_code=409,
                            detail="this is the last active administrator")
    if not store.set_user_role(user_id, body.role):
        raise HTTPException(status_code=404, detail="no such user")
    return {"ok": True, "role": body.role}


@app.post("/auth/users/{user_id}/password")
def auth_set_password(user_id: str, body: SetPasswordRequest,
                      principal: Principal = Depends(require_admin)) -> dict:
    """An administrator sets someone's password — the only reset path, since
    there is no email to send a link to on an on-prem appliance."""
    if len(body.password or "") < 8:
        raise HTTPException(status_code=422,
                            detail="password must be at least 8 chars")
    if not store.set_user_password(user_id, auth_mod.hash_password(body.password)):
        raise HTTPException(status_code=404, detail="no such user")
    # Existing tokens stay valid until they expire; say so rather than implying
    # the change cuts off an active session.
    return {"ok": True,
            "note": f"existing sessions remain valid for up to "
                    f"{auth_mod.TOKEN_TTL_HOURS}h"}


@app.post("/auth/users/{user_id}/username")
def auth_set_username(user_id: str, body: SetUsernameRequest,
                      principal: Principal = Depends(require_admin)) -> dict:
    """Rename a login.

    Renaming is safe for the person's work: every owned table keys on
    `owner_id`, so the name is a label and nothing follows it. A token already
    issued carries the OLD name in `preferred_username` until it expires, which
    is why the reply says so instead of pretending the change is instant.

    The same validation as registration, deliberately: a name that could not be
    created should not be reachable by renaming into it either.
    """
    username = (body.username or "").strip()
    if not username or len(username) > 64:
        raise HTTPException(status_code=422, detail="username must be 1-64 chars")
    existing = store.get_user_by_username(username)
    if existing and existing["id"] != user_id:
        raise HTTPException(status_code=409, detail="username already taken")
    if existing:
        return {"ok": True, "username": username, "note": "unchanged"}
    before = store.get_user(user_id)
    if not store.set_user_username(user_id, username):
        raise HTTPException(status_code=404, detail="no such user")
    logger.info("user renamed: %s -> %s",
                (before or {}).get("username", user_id), username)
    return {"ok": True, "username": username,
            "note": f"existing sessions keep the old name for up to "
                    f"{auth_mod.TOKEN_TTL_HOURS}h"}


@app.delete("/auth/users/{user_id}")
def auth_delete_user(user_id: str,
                     principal: Principal = Depends(require_admin)) -> dict:
    if user_id == principal.user_id:
        raise HTTPException(status_code=400, detail="cannot delete your own account")
    if _last_active_admin(user_id):
        raise HTTPException(status_code=409,
                            detail="this is the last active administrator")
    if not store.delete_user(user_id):
        raise HTTPException(status_code=404, detail="no such user")
    return {"ok": True}


@app.post("/auth/users/{user_id}/enable")
def auth_enable_user(user_id: str,
                     principal: Principal = Depends(require_admin)) -> dict:
    if not store.set_user_disabled(user_id, False):
        raise HTTPException(status_code=404, detail="no such user")
    return {"ok": True}


def _close_ai_turn(config: NSGSimulationConfig, principal: "Principal | None",
                   run_id: str | None = None) -> None:
    """Attribute a run to the prompt that produced its config, if there is one.

    Done by INFERENCE — the caller's newest turn that nothing has been attributed
    to, within the hour — so the data accumulates without the UI having to
    cooperate, which is the difference between a dataset and an intention. Every
    row linked this way is stamped `outcome_source="inferred"`, because a guess
    recorded as a fact is worse than no row.

    The outcome is read from the DIFF rather than asserted: identical config means
    the user accepted what the AI built, any difference means they corrected it.
    That distinction is the whole training signal.
    """
    try:
        turn = store.latest_pending_ai_interaction(
            _owner_id(principal) if principal else None)
        if not turn:
            return
        final = config.model_dump(mode="json")
        delta = diff_configs(turn.get("ai_config"), final)
        turn.update({
            "final_config": final,
            "config_diff": delta,
            "outcome": "accepted" if delta.get("total") == 0 else "edited",
            "outcome_source": "inferred",
            "run_id": run_id,
        })
        store.save_ai_interaction(turn)
    except Exception:  # noqa: BLE001
        logger.exception("could not close the AI interaction")


def _record_ai_turn(prompt: str, cfg: NSGSimulationConfig,
                    principal: "Principal | None") -> str | None:
    """Write down what the generator was asked and what it produced.

    A correction is the only signal that says where the model is wrong, and none
    of them were being kept. Recorded at ANSWER time with `outcome: "pending"`,
    because what the user does next — keep it, edit it, throw it away — is the
    label, and it does not exist yet.

    Never allowed to fail the request it observes: a generator that stops working
    because its telemetry broke is a worse product than one that loses a row.
    """
    try:
        iid = uuid.uuid4().hex[:12]
        store.save_ai_interaction({
            "id": iid,
            "created_at": _now_iso(),
            "owner_id": _owner_id(principal) if principal else None,
            "model": getattr(_agent, "model_name", None) or os.environ.get("NSG_LLM_MODEL"),
            "prompt": prompt,
            "ai_config": cfg.model_dump(mode="json"),
            "outcome": "pending",
        })
        return iid
    except Exception:  # noqa: BLE001
        logger.exception("could not record the AI interaction")
        return None


@app.post("/ai/modify", response_model=NSGSimulationConfig, dependencies=[Depends(require_user)])
def ai_modify(request: ModifyConfigRequest, response: Response,
              principal: Principal = Depends(current_principal)) -> NSGSimulationConfig:
    """
    Apply a natural-language modification to the current simulation configuration.

    The LLM uses structured output (tool-calling) to guarantee a valid
    NSGSimulationConfig is returned — no regex extraction, no YAML merging.

    Typical call cycle:
      1. Frontend sends {} on first load → LLM generates a starter config.
      2. User edits → frontend sends the full current config + the change request.
      3. Backend returns the updated config → frontend re-renders the topology.
      4. When happy, user calls POST /run-simulation with the final config.
    """
    try:
        cfg = _agent.modify_configuration(
            current_config=request.current_config,
            user_prompt=request.prompt,
            history=request.history or None,
        )
        # In a HEADER, not the body: the response model is the config itself and
        # a caller that ignores this keeps working exactly as before.
        iid = _record_ai_turn(request.prompt, cfg, principal)
        if iid:
            response.headers["X-NSG-Interaction"] = iid
        return cfg
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "LLM returned a config that failed schema validation.",
                "errors": json.loads(exc.json()),
            },
        )
    except (AIUnavailableError, APIError):
        logger.warning("AI service unavailable", exc_info=True)
        raise HTTPException(status_code=503, detail="AI service unavailable")
    except Exception:
        logger.exception("AI configuration failed")
        raise HTTPException(status_code=500, detail="AI request failed")


@app.post("/ai/interactions/{interaction_id}/outcome")
def ai_interaction_outcome(interaction_id: str, config: NSGSimulationConfig | None = None,
                           outcome: str = "edited",
                           principal: Principal = Depends(require_user)) -> dict:
    """Say explicitly what happened to a generated config.

    The run hook infers this well enough for the common path, but inference cannot
    see the case that matters most: a config the user looked at and THREW AWAY.
    That never reaches a run, so without this it is invisible — and "the AI
    produced something nobody would run" is the strongest correction there is.
    """
    turn = store.get_ai_interaction(interaction_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="interaction not found")
    if not _may_see(principal, turn.get("owner_id")):
        raise HTTPException(status_code=404, detail="interaction not found")
    if outcome not in ("accepted", "edited", "discarded"):
        raise HTTPException(status_code=422, detail="outcome must be accepted|edited|discarded")
    final = config.model_dump(mode="json") if config else None
    turn.update({
        "final_config": final,
        "config_diff": diff_configs(turn.get("ai_config"), final) if final else None,
        "outcome": outcome,
        "outcome_source": "explicit",
    })
    store.save_ai_interaction(turn)
    return {"id": interaction_id, "outcome": outcome, "outcome_source": "explicit"}


# A failed run says one of two completely different things, and lumping them
# together would poison the dataset in the direction that matters most.
_BENCH_FAILURES = ("error", "busy", "timeout", "engine-closed", "stopped", "interrupted")
_CONFIG_VERDICTS = ("crashed", "partial", "no_data", "truncated", "wall-capped")


def _label_quality(run: dict) -> str:
    """Does this run's outcome say something about the CONFIG, or about the bench?

    A config that crashed the engine is the most clearly labelled row in the
    table — "this does not work" is exactly what the envelope rules were
    hand-derived from. A run that failed because the engine was unreachable says
    NOTHING about the config, and filing it as a negative example teaches the
    generator that a perfectly good scenario is bad.

    The real case that prompted this: an interaction came back `run_status=error`
    and the error was `live engine unreachable at 127.0.0.1:8091` — the WSL distro
    had suspended. Same distinction target_search needed for "busy".
    """
    status = (run or {}).get("status")
    if status == "ok":
        return "ok"
    if status in _BENCH_FAILURES:
        return "bench_failure"
    if status in _CONFIG_VERDICTS:
        return "config_verdict"
    return "unknown"


@app.get("/ai/interactions")
def ai_interactions(limit: int = 200, mine: bool = True,
                    run_status: str | None = None,
                    principal: Principal = Depends(require_user)) -> dict:
    """The generator's own record: what it was asked, what it built, what was kept.

    Read this before training anything on it. `corrected_fields` is the point of
    the whole table — the fields a user keeps having to fix are a prompt problem
    today, long before there is enough data to be a fine-tuning problem.
    """
    rows = store.load_ai_interactions(
        limit=limit, owner_id=_owner_id(principal) if mine else None)

    # WHAT THE KEPT CONFIG ACTUALLY DID. A correction that produced a scenario the
    # engine could not run is not a bad row — it is the most clearly labelled row
    # in the table, "this config does not work" — but it has to be DISTINGUISHABLE
    # from a correction that produced a healthy run, or the two average into
    # nothing. The join was already possible (`run_id`) and nobody was making it,
    # which meant "my random experiments will pollute the data" was a fear with no
    # way to act on it. Now it is a filter.
    for r in rows:
        rid = r.get("run_id")
        if not rid:
            r["run_status"] = None
            r["label_quality"] = "not_run"
            continue
        rec = _SIM_RUNS.get(rid) or store.load_run(rid) or {}
        r["run_status"] = rec.get("status")
        summ = rec.get("summary") or {}
        r["run_dl_mbps_mean"] = summ.get("dl_mbps_mean")
        r["run_sim_span_s"] = summ.get("sim_span_s")
        r["label_quality"] = _label_quality(rec)

    by_run_status: dict[str, int] = {}
    by_label_quality: dict[str, int] = {}
    for r in rows:
        by_run_status[r.get("run_status") or "not run"] = (
            by_run_status.get(r.get("run_status") or "not run", 0) + 1)
        lq = r.get("label_quality") or "not_run"
        by_label_quality[lq] = by_label_quality.get(lq, 0) + 1

    field_counts: dict[str, int] = {}
    for r in rows:
        for c in ((r.get("config_diff") or {}).get("changed") or []):
            field_counts[c["path"]] = field_counts.get(c["path"], 0) + 1
    by_outcome: dict[str, int] = {}
    for r in rows:
        by_outcome[r.get("outcome") or "pending"] = by_outcome.get(r.get("outcome") or "pending", 0) + 1
    # Only the rows whose config was measured to WORK. This is the subset anyone
    # would train a generator on: prompt -> config -> a run that produced
    # readable DU metrics. The rest are kept, and kept separable.
    usable = [r for r in rows if r.get("run_status") == "ok"]
    if run_status:
        # e.g. ?run_status=ok for the training pool, or =crashed for the
        # configs that broke the engine — the envelope's raw material.
        rows = [r for r in rows if (r.get("run_status") or "not run") == run_status]
    return {
        "interactions": rows,
        "count": len(rows),
        "by_outcome": by_outcome,
        # "ok" is the trainable pool; "crashed"/"partial"/"no_data" are the
        # negative examples the envelope rules were hand-derived from.
        "by_run_status": by_run_status,
        # ok            -> the positive pool
        # config_verdict-> the NEGATIVE pool, and the envelope's raw material
        # bench_failure -> says nothing about the config; exclude from BOTH
        # not_run       -> no label yet
        "by_label_quality": by_label_quality,
        "usable_for_training": len(usable),
        "usable_as_negatives": sum(
            1 for r in rows if r.get("label_quality") == "config_verdict"),
        "unlabelled_bench_failures": sum(
            1 for r in rows if r.get("label_quality") == "bench_failure"),
        "corrected_fields": sorted(field_counts.items(), key=lambda kv: -kv[1])[:30],
    }


@app.post("/run-simulation", response_model=SimulationRunResult, dependencies=[Depends(require_user)])
def run_simulation(config: NSGSimulationConfig) -> dict:
    """
    Translate an NSGSimulationConfig to NS-3 JSON and run ns3-dev-rem-gen.

    This route is a regular `def` — FastAPI runs it in a thread pool
    automatically, so it will not block the event loop.

    Output: a NumPy array at /tmp/remPoints.npy with shape (N, 4):
      columns → [x_m, y_m, z_m, RSRP_dBm]
    This array is parsed and returned inline as `heatmap`: [{x, y, rsrp}, ...]
    so the frontend doesn't need filesystem access. Empty list if the binary
    produced no REM output.

    Production upgrade path:
      For simulations longer than ~30 s, offload to a Celery task and return
      a job ID here. Add GET /simulation-status/{job_id} for polling.
    """
    try:
        return ns3_bridge.run_simulation(config)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"NS-3 binary not reachable: {exc}",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def _layout_spread(points: list) -> float | None:
    """Max distance of any point from the centroid — a rough scale of the layout."""
    pts = [p for p in points if p is not None and math.isfinite(p[0]) and math.isfinite(p[1])]
    if len(pts) < 2:
        return None
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    return max(((p[0] - cx) ** 2 + (p[1] - cy) ** 2) ** 0.5 for p in pts)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# The ns-O-RAN scenario is a FIXED 4000x4000 area with the hub at its centre.
_SCEN_CENTER = 2000.0
_SCEN_MIN, _SCEN_MAX = 100.0, 3900.0


def _scene_centroid(points: list) -> tuple:
    """Centroid of ALL config points (cells + UE spawns). This is the ONE reference
    the whole scene is translated by. Using a per-group centroid instead would map
    the cells' centroid AND the UEs' centroid both onto the scenario centre, which
    destroys their relative geometry — UEs clustered off to one side would land in
    the middle of the ring."""
    pts = [p for p in points if p is not None and math.isfinite(p[0]) and math.isfinite(p[1])]
    if not pts:
        return (0.0, 0.0)
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _apply_offset(points: list, cx: float, cy: float) -> list:
    """Translate config (x,y) by the shared scene centroid into the engine's fixed
    area, preserving relative geometry; clamp to the area bounds."""
    out = []
    for p in points:
        if p is None or not (math.isfinite(p[0]) and math.isfinite(p[1])):
            continue
        out.append((_clamp(_SCEN_CENTER + (p[0] - cx), _SCEN_MIN, _SCEN_MAX),
                    _clamp(_SCEN_CENTER + (p[1] - cy), _SCEN_MIN, _SCEN_MAX)))
    return out


def _to_scenario(points: list) -> list:
    """Translate points about their own centroid (kept for callers that translate a
    single set). Multi-set callers must share one centroid — see _scene_centroid."""
    return _apply_offset(points, *_scene_centroid(points))


# The engine pins its LTE anchor at the scenario centre. A mmWave cell landing on
# top of it kills the run — no DU metrics at all, or ns-3 aborts outright (both
# verified on the engine; a cell 141 m clear runs fine, 25 m does not). And since
# the scene is translated by its centroid, ANY design with a cell at its centre of
# mass — 4 corners + a middle cell, a symmetric ring — lands exactly there.
_MIN_BS_HUB_M = 150.0
# Rigid translations to try, in order. (0,0) first: never move a scene that is
# already clear. Magnitudes stay well inside the 4000 m area so nothing clamps.
_HUB_SHIFTS = [(0.0, 0.0)] + [
    (r * math.cos(a), r * math.sin(a))
    for r in (200.0, 350.0)
    for a in (math.pi * k / 4 for k in range(8))
]


def _clear_hub_centroid(gnb_pts: list, cx: float, cy: float) -> tuple:
    """Nudge the shared scene centroid so no cell sits on the engine's LTE anchor.

    Shifting the centroid translates the WHOLE scene — cells, UEs and buildings
    alike — so relative geometry is preserved exactly; only the scene's placement
    inside the engine's fixed area changes, which nothing measures.
    """
    for dx, dy in _HUB_SHIFTS:
        c = (cx + dx, cy + dy)
        if all(math.hypot(x - _SCEN_CENTER, y - _SCEN_CENTER) >= _MIN_BS_HUB_M
               for (x, y) in _apply_offset(gnb_pts, *c)):
            return c
    return (cx, cy)   # dense design with no clear placement: run it as designed


# Per-UE DL traffic rate envelope (Mbps). ns-3 mmWave is heavy: a UE group asking
# for 1 Gbps each would bury the sim, and a 0-rate UE tells the RAN nothing.
_UE_RATE_MIN, _UE_RATE_MAX = 0.5, 50.0


# Slot allocation moved to engine_envelope.ue_alloc (shared with nsoran_live).
_ue_alloc = ue_alloc


# Call-model bounds (s). The lower bound used to be 0.5 s "because shorter than
# that is below the engine's reporting resolution". That was wrong in a way
# nothing caught until a constant call model measured EXACTLY ZERO on every
# metric — PRB, PDCP PDUs, throughput.
#
# The measured KPM window is only about 0.35 s of simulated time (a baseline run
# carries ~340 x 1400 B per UE at ~11 Mbps). OnOffApplication always begins with
# an OFF period, so a 0.5 s floor meant the first burst could not fit in the
# window at all; with an exponential model some UEs drew short enough off times
# to squeak in, and with a deterministic one none did. Same offered load, and
# the answer came back 0 vs 16.5 vs 63.9 Mbps depending only on how lucky the
# draws were.
#
# 0.05 s puts several complete on/off cycles inside the window, which is what
# makes the SHAPE of the call process measurable rather than its luck.
_CALL_MIN_S, _CALL_MAX_S = 0.05, 600.0
# What the on+off period is compressed to. Must stay well inside the window
# above for the same reason.
_CALL_PERIOD_S = 0.15


# A UE carries at most this many parallel service streams in the engine.
_MAX_SVCS_PER_UE = 3

# Engine-sane walk speeds (m/s). NOTE the honest limit, measured: MOTION-DRIVEN
# handover churn is NOT observable in a batch run — the sim advances only
# ~0.4-1.5 simulated seconds, and the random walk DIFFUSES (direction changes
# every leg) rather than traverses, so even a x40 speed compression produced
# zero handovers; the compression was therefore removed rather than kept as a
# useless magic constant. What IS real: the configured speed drives the actual
# motion (position-trace step lengths scale with it), which the engine test
# asserts. Handover-margin effects belong to longer/live horizons.
_UE_SPEED_MIN, _UE_SPEED_MAX = 0.5, 40.0


def _ue_speed_specs(ues: list, n_ue: int) -> str:
    """MRO: per-UE walk speeds (m/s), aligned slot-for-slot with ue_positions —
    each UE inherits its group's mobility.speed_mps. The engine used to hardcode
    2-4 m/s, which made the config's speed a silent fake lever."""
    out = []
    for g, k in zip(ues, _ue_alloc(ues, n_ue)):
        try:
            v = float(g.mobility.speed_mps)
        except (AttributeError, TypeError, ValueError):
            v = 1.4
        if not math.isfinite(v) or v <= 0:
            v = 1.4
        out.extend([f"{_clamp(v, _UE_SPEED_MIN, _UE_SPEED_MAX):.1f}"] * k)
    return ";".join(out[:n_ue])


# A waypoint path is expanded (repeat / pingpong) until it covers the run, but
# never past this many points — a long run at a short path would otherwise emit
# thousands of waypoints the sim will never reach.
_MAX_WP_PER_UE = 200
_UE_Z_MIN, _UE_Z_MAX = 1.5, 300.0


def _ue_waypoint_specs(ues: list, n_ue: int, cx: float, cy: float,
                       ue_scen: list, duration_s: float) -> str:
    """Phase 2b: per-UE WAYPOINT paths, aligned slot-for-slot with ue_positions.

    Format per UE slot: '|'-separated 'x,y,z,t' (t = arrival time in seconds,
    t=0 at the UE's own start position); an empty slot leaves that UE on the
    engine's random walk. Empty overall => byte-identical to a pre-2b run.

    `mobility.waypoints` was in the schema and the UI from the start but never
    reached ns-3 — a completely silent lever. It matters more than the speed
    lever it sits next to: the engine's RandomWalk2dOutdoor DIFFUSES (a new
    direction every leg) and nets a few tens of metres against ~350 m cell
    spacing, so no speed makes it cross a boundary, while a waypoint path
    TRAVERSES and does.

    Every UE in a group flies the SAME path translated to its own start
    position, so a group stays spread out. Sharing one absolute path would pile
    the whole group onto a single point, which makes the engine report zero DU
    metrics or abort (see engines/nsoran notes on UE placement).
    """
    out: list[str] = []
    slot = 0
    for g, k in zip(ues, _ue_alloc(ues, n_ue)):
        mob = getattr(g, "mobility", None)
        wps = list(getattr(mob, "waypoints", None) or [])
        # (x, y, z, speed-to-reach) in SCENARIO coordinates, invalid points dropped
        legs: list[tuple] = []
        for w in wps:
            p = getattr(w, "position", None)
            try:
                x, y, z = float(p.x), float(p.y), float(getattr(p, "z", 0.0) or 0.0)
                v = float(getattr(w, "speed_mps", 5.0) or 5.0)
            except (AttributeError, TypeError, ValueError):
                continue
            if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z) and math.isfinite(v)):
                continue
            legs.append((_SCEN_CENTER + (x - cx), _SCEN_CENTER + (y - cy),
                         _clamp(z, _UE_Z_MIN, _UE_Z_MAX),
                         _clamp(v, _UE_SPEED_MIN, _UE_SPEED_MAX)))
        if len(legs) < 2:
            # One point (or none) is a path the UE never moves along once it is
            # anchored to its own start — keep the random walk instead.
            out.extend([""] * k)
            slot += k
            continue

        mode = str(getattr(mob, "waypoint_mode", "pingpong") or "pingpong")
        # One full lap to append each time the path runs out, expanded until it
        # outlasts the run. 'repeat' closes the loop back to the start (a
        # WaypointMobilityModel interpolates between waypoints, so it flies home
        # rather than teleporting); 'pingpong' retraces the path in reverse. A
        # reverse leg is flown at the speed of the waypoint it starts from.
        cycle = list(legs) if mode == "repeat" else list(reversed(legs[:-1])) + legs[1:]
        path = list(legs)
        if mode != "once":
            while len(path) < _MAX_WP_PER_UE:
                path.extend(cycle)
                # One cycle of wall time is enough to cover the run.
                if _path_seconds(path) >= float(duration_s or 0):
                    break
        path = path[:_MAX_WP_PER_UE]

        for _ in range(k):
            if slot >= len(ue_scen):
                break
            # Anchor the path at THIS UE's start position (already past the
            # building / base-station / pile-up guards), so the group keeps its
            # spread and no UE teleports at t=0.
            ox, oy = ue_scen[slot][0] - path[0][0], ue_scen[slot][1] - path[0][1]
            pts, t, px, py, pz = [], 0.0, path[0][0] + ox, path[0][1] + oy, path[0][2]
            pts.append(f"{px:.1f},{py:.1f},{pz:.1f},0.000")
            for (x, y, z, v) in path[1:]:
                nx = _clamp(x + ox, _SCEN_MIN, _SCEN_MAX)
                ny = _clamp(y + oy, _SCEN_MIN, _SCEN_MAX)
                d = math.dist((px, py, pz), (nx, ny, z))
                if d < 0.5:      # same point after clamping — nothing to fly
                    continue
                t += d / max(v, _UE_SPEED_MIN)
                pts.append(f"{nx:.1f},{ny:.1f},{z:.1f},{t:.3f}")
                px, py, pz = nx, ny, z
            out.append("|".join(pts) if len(pts) > 1 else "")
            slot += 1

    out = out[:n_ue]
    out.extend([""] * (n_ue - len(out)))
    return ";".join(out) if any(out) else ""


def _path_seconds(path: list) -> float:
    """Wall time a waypoint path takes at its per-leg speeds (scenario frame)."""
    t = 0.0
    for i in range(1, len(path)):
        x0, y0, z0, _ = path[i - 1]
        x1, y1, z1, v = path[i]
        t += math.dist((x0, y0, z0), (x1, y1, z1)) / max(v, _UE_SPEED_MIN)
    return t


# Link-level error models the fork ships. The default is an LTE
# mutual-information model on a 5G engine; the rest are 3GPP NR EESM —
# CC = Chase Combining (retransmit the same bits), IR = Incremental Redundancy
# (new parity, which is what NR does), T1/T2 = MCS table 1 (64QAM) / 2 (256QAM).
_ERROR_MODEL = {
    "lte_mi": "ns3::MmWaveLteMiErrorModel",
    "nr_cc_table1": "ns3::MmWaveEesmCcT1",
    "nr_cc_table2": "ns3::MmWaveEesmCcT2",
    "nr_ir_table1": "ns3::MmWaveEesmIrT1",
    "nr_ir_table2": "ns3::MmWaveEesmIrT2",
}


# Beamforming models the fork actually ships. SVD is the engine default and an
# optimistic upper bound (best beam from perfect channel knowledge); codebook is
# what real hardware is restricted to.
_BEAMFORMING = {
    "svd": "ns3::MmWaveSvdBeamforming",
    "dft": "ns3::MmWaveDftBeamforming",
    "codebook": "ns3::MmWaveCodebookBeamforming",
}


def _gnb_antenna_specs(bss: list) -> str:
    """Phase 2c: per-gNB antenna panel, ';'-sep 'azimuth_deg,tilt_deg,rows,cols'
    aligned with gnb_positions. Azimuth stays in the CONFIG's compass convention
    (0 = North, clockwise) — the scenario converts it to ns-3's phi.

    `antenna.azimuth_deg` / `tilt_deg` were in the schema and the UI from the
    start and never reached ns-3. They were written off as "N/A by engine
    paradigm — the beam is steered at the UE", which is only half the story: the
    array's GCS->LCS rotation is applied, but the PhasedArrayModel element
    defaults to isotropic, and rotating isotropic elements cancels out exactly.
    Orientation therefore only bites with settings.antenna_element = '3gpp'.

    Emits a spec whenever any cell asks for a non-default panel, so an untouched
    config still runs on the engine's global 8x8 default.
    """
    specs, any_set = [], False
    for b in bss:
        a = getattr(b.radio, "antenna", None)
        az = float(getattr(a, "azimuth_deg", 0.0) or 0.0) if a else 0.0
        tilt = float(getattr(a, "tilt_deg", 0.0) or 0.0) if a else 0.0
        mm = getattr(a, "massive_mimo", None) if a else None
        rows = int(getattr(mm, "rows", 8) or 8) if mm else 8
        cols = int(getattr(mm, "cols", 8) or 8) if mm else 8
        az = az % 360.0 if math.isfinite(az) else 0.0
        tilt = _clamp(tilt, -90.0, 90.0) if math.isfinite(tilt) else 0.0
        rows, cols = int(_clamp(rows, 1, 16)), int(_clamp(cols, 1, 16))
        pol = float(getattr(mm, "polarization_deg", 0.0) or 0.0) if mm else 0.0
        pol = _clamp(pol, -90.0, 90.0) if math.isfinite(pol) else 0.0
        hs = float(getattr(mm, "h_spacing_wl", 0.5) or 0.5) if mm else 0.5
        vs = float(getattr(mm, "v_spacing_wl", 0.5) or 0.5) if mm else 0.5
        hs = _clamp(hs, 0.1, 4.0) if math.isfinite(hs) else 0.5
        vs = _clamp(vs, 0.1, 4.0) if math.isfinite(vs) else 0.5
        # Element beamwidth + boresight gain, read only by the elements that
        # have them (cosine, parabolic, circular). Sent unconditionally because
        # the element is a network-wide setting and the panel spec is per cell:
        # deciding here which fields "apply" would duplicate that logic in two
        # places and let them drift.
        hbw = float(getattr(a, "horizontal_beamwidth_deg", 65.0) or 65.0) if a else 65.0
        vbw = float(getattr(a, "vertical_beamwidth_deg", 65.0) or 65.0) if a else 65.0
        gain = float(getattr(a, "gain_dbi", 8.0) or 8.0) if a else 8.0
        hbw = _clamp(hbw, 1.0, 360.0) if math.isfinite(hbw) else 65.0
        vbw = _clamp(vbw, 1.0, 360.0) if math.isfinite(vbw) else 65.0
        gain = _clamp(gain, -30.0, 60.0) if math.isfinite(gain) else 8.0
        specs.append(f"{az:.1f},{tilt:.1f},{rows},{cols},{pol:.1f},{hs:.2f},"
                     f"{vs:.2f},{hbw:.1f},{vbw:.1f},{gain:.1f}")
        any_set = (any_set or az != 0.0 or tilt != 0.0 or rows != 8 or cols != 8
                   or pol != 0.0 or hs != 0.5 or vs != 0.5
                   or hbw != 65.0 or vbw != 65.0 or gain != 8.0)
    return ";".join(specs) if any_set else ""


def _gnb_band_specs(bss: list) -> str:
    """Per-gNB carrier, ';'-sep 'freq_mhz,bw_mhz,numerology', gnb_positions-aligned.

    **THE SLOW TIER CANNOT READ THIS AND THAT IS THE POINT.** `scenario-zero`
    builds one `MmWaveHelper` with one `--frequency` and one `--bandwidth`, so
    ns-3 runs a single carrier for the whole scene and `nsoran_params` has
    always taken `bss[0].radio.band` for everybody. A config where the cells
    carry different bands was therefore silently flattened to the first one.

    Tier B has no such constraint — it computes a window per cell in closed
    form, so a per-cell carrier costs it nothing — and every reference
    scenario this project compares against is multi-band: VIAVI's captured
    runs are three sites x three sectors on N77 (3600 MHz, 40 MHz) and N28
    (728 MHz, 10 MHz), which is sixteen of the twenty on disk.

    So this is emitted for the tier that can use it and ignored by the tier
    that cannot — `_PARAM_ENV` has no entry for it, so nothing reaches ns-3
    and no slow-tier run changes. Emitted only when the cells actually
    DIFFER, so a single-band config produces the same payload it always has.
    """
    specs, differs = [], False
    first = None
    for b in bss:
        band = getattr(b.radio, "band", None)
        f = clamp_freq_mhz(getattr(band, "frequency_mhz", 3500.0) or 3500.0) if band else 3500.0
        w = clamp_bw_mhz(getattr(band, "bandwidth_mhz", 20.0) or 20.0) if band else 20.0
        mu = int(_clamp(getattr(band, "numerology", 2) if band else 2, 0, 6))
        specs.append(f"{f:.1f},{w:.1f},{mu}")
        first = first if first is not None else specs[0]
        differs = differs or specs[-1] != first
    return ";".join(specs) if differs else ""


def _ue_heights(ues: list, n_ue: int) -> list:
    """Per-UE antenna height (m), aligned slot-for-slot with ue_positions. The
    engine hardcoded 3 m for every node, so this was inert."""
    out = []
    for g, k in zip(ues, _ue_alloc(ues, n_ue)):
        try:
            h = float(getattr(g, "height_m", 1.5) or 1.5)
        except (TypeError, ValueError):
            h = 1.5
        if not math.isfinite(h):
            h = 1.5
        out.extend([_clamp(h, 0.5, 150.0)] * k)
    out = out[:n_ue]
    out.extend([1.5] * (n_ue - len(out)))
    return out


_CALL_DIST = {"exponential": 0, "constant": 1, "uniform": 2, "pareto": 3}


def _enum_str(v, default: str) -> str:
    """The wire value of a str-Enum field, whether it arrived as the enum or as
    a plain string.

    str() on a `class X(str, Enum)` member gives 'X.MEMBER', not its value —
    which silently turns every enum lookup into the default. Measured: a
    service asking for pareto bursts and TCP reached the engine as exponential
    UDP, and nothing anywhere reported a problem.
    """
    if v is None:
        return default
    return str(getattr(v, "value", v)) or default


def _qci_or_default(qci) -> int:
    """Keep only 5QIs the engine's QoS table can answer for.

    An unknown 5QI is not a validation nicety: EpsBearer's priority / delay
    budget / error-rate lookup ends in NS_FATAL_ERROR, so it would abort the
    run at the first bearer setup. The engine falls back to 9 as well — doing
    it here too means the spec string and the run agree on what was requested.
    """
    try:
        q = int(qci)
    except (TypeError, ValueError):
        return 9
    return q if q in QOS_5QI_CATALOGUE else 9


def _slice_policy_spec(config) -> str:
    """'qci:max[:min[:dedicated]]' in percent, ';'-separated — the initial
    slice partitioning the scheduler starts from.

    Until now this string only ever came from the live E2 control path, so a
    saved scenario could not carry a slice policy at all. Slices with every
    ratio at zero are dropped rather than sent as '0:0:0': an all-zero entry
    reads as "capped at nothing" and would starve the slice completely.
    """
    parts = []
    for p in (getattr(config, "slices", None) or []):
        mx = _clamp(float(p.max_pct or 0.0), 0.0, 100.0)
        mn = _clamp(float(p.min_pct or 0.0), 0.0, 100.0)
        dd = _clamp(float(p.dedicated_pct or 0.0), 0.0, 100.0)
        if mx <= 0.0 and mn <= 0.0 and dd <= 0.0:
            continue
        parts.append(f"{_qci_or_default(p.qos_id)}:{mx:.1f}:{mn:.1f}:{dd:.1f}")
    return ";".join(parts)


def _fault_spec(config, bss: list) -> str:
    """'cell,start_s,duration_s,type,value' per fault, ';'-separated.

    The config names the failing cell by id; the engine indexes cells by
    position order, so the id is resolved here. A fault on a cell that is not
    in the run is DROPPED rather than remapped to index 0 — silently breaking
    a different cell than the one asked for is worse than breaking none.
    """
    index = {b.id: i for i, b in enumerate(bss)}
    parts = []
    for a in (getattr(config, "anomalies", None) or []):
        kind = _enum_str(a.type, "radio_degradation")
        # A handover block is the anchor RRC's, not a cell's — one LTE anchor
        # admits handovers for every cell here — so it is emitted without the
        # cell lookup that would otherwise drop it when the id is stale.
        if kind == "handover_block":
            parts.append(f"0,{float(a.start_s):.3f},{float(a.duration_s):.3f},3,0")
            continue
        i = index.get(a.cell_id)
        if i is None:
            continue
        if kind == "cell_outage":
            parts.append(f"{i},{float(a.start_s):.3f},{float(a.duration_s):.3f},1,0")
            continue
        if kind == "noise_rise":
            rise = _clamp(float(getattr(a, "noise_rise_db", None) or 20.0), 1.0, 60.0)
            parts.append(
                f"{i},{float(a.start_s):.3f},{float(a.duration_s):.3f},2,{rise:.1f}")
            continue
        if a.tx_power_dbm is not None:
            to_dbm = _clamp(float(a.tx_power_dbm), 0.0, 50.0)
        else:
            # -3 dB: half the radiated power, the mildest degradation anyone
            # would call a fault.
            base = float(getattr(bss[i].radio, "tx_power_dbm", 44.0) or 44.0)
            to_dbm = _clamp(base - 3.0, 0.0, 50.0)
        parts.append(f"{i},{float(a.start_s):.3f},{float(a.duration_s):.3f},0,{to_dbm:.1f}")
    return ";".join(parts)


def _ue_slice_specs(ues: list, n_ue: int) -> str:
    """Level-5: per-UE service list, aligned slot-for-slot with ue_positions.

    Format per UE slot: 'qci,rate_mbps[,on_s,off_s]' per service, services
    joined by '|'. Until now a group's services were COLLAPSED into one stream
    (rates summed, first 5QI, first call model) — so "a car carrying voice +
    data" was unrepresentable. Each service is now its own traffic stream with
    its own rate and its own call model, so the per-UE traffic MIX is real.

    Honest limits: all of a UE's streams ride ONE bearer whose QCI is the FIRST
    service's (a second dedicated bearer reaches the MAC but segfaults the sim —
    see engines/nsoran notes), and the MAC scheduler treats a UE's LCs as a
    single flow anyway, so slicing granularity stays per-UE.

    The optional on/off pair is the call model, TIME COMPRESSED to a
    _CALL_PERIOD_S period with the on:off ratio preserved — OnOffApplication
    starts silent and the measured window is only ~0.35 s of simulated time, so
    literal human-scale call times measure zero. The ratio is the part of a call
    model that survives a horizon this short; the absolute durations are not.
    Two more trailing fields carry the distribution family and the transport.
    Old single-service spec strings stay valid.
    """
    out = []
    for g, k in zip(ues, _ue_alloc(ues, n_ue)):
        svcs = list(g.services or [])[:_MAX_SVCS_PER_UE]
        parts = []
        # Clamp each rate, then scale the set down if the UE's total exceeds the
        # engine envelope — proportions between services are preserved.
        rates = []
        for s in svcs:
            r = max(0.0, float(s.target_throughput_gbps)) * 1000.0
            rates.append(r if math.isfinite(r) and r > 0 else 0.0)
        total = sum(rates)
        scale = _UE_RATE_MAX / total if total > _UE_RATE_MAX else 1.0
        for s, r in zip(svcs, rates):
            if r <= 0:
                continue
            rate = _clamp(r * scale, _UE_RATE_MIN, _UE_RATE_MAX)
            spec = f"{int(_qci_or_default(s.qos_id))},{rate:.2f}"
            dist = _CALL_DIST.get(_enum_str(
                getattr(s, "call_distribution", None), "exponential"), 0)
            proto = 1 if _enum_str(getattr(s, "protocol", None), "udp") == "tcp" else 0
            if s.avg_call_duration_s and s.avg_call_duration_s > 0:
                on_s = _clamp(float(s.avg_call_duration_s), _CALL_MIN_S, _CALL_MAX_S)
                off_s = _clamp(float(s.avg_idle_duration_s or 0.0), 0.0, _CALL_MAX_S)
                period = on_s + off_s
                if period > _CALL_PERIOD_S:
                    # Compress to the window, keeping the on:off RATIO — which
                    # is the part of a call model that survives a horizon this
                    # short. Absolute human-scale durations cannot.
                    squeeze = _CALL_PERIOD_S / period
                    on_s, off_s = on_s * squeeze, off_s * squeeze
                spec += f",{on_s:.3f},{off_s:.3f},{dist},{proto}"
            elif proto:
                # No call model but TCP: the trailing fields are positional, so
                # the on/off pair has to be there for proto to be read at all.
                # 0/0 is the scenario's own "continuous" marker.
                spec += f",0,0,{dist},{proto}"
            parts.append(spec)
        out.extend(["|".join(parts) if parts else "9,3.00"] * k)
    return ";".join(out[:n_ue])


def _ue_spread_m(isd_cell: float) -> float:
    """How far a group's UEs fan out around its spawn point.

    This used to be a flat 60 m, which quietly broke the engine: a config with one
    UE group — the normal case — put every UE inside one cell's skirt and left the
    other cells with no traffic at all. ns-O-RAN does not survive that (verified:
    the same 5-cell scenario yields zero DU metrics with a 60 m ring and full
    throughput/SINR/PRB once the UEs are spread), and it violates the engine's own
    envelope rule that every cell must carry load. Scaling with the cell spacing
    also matches the schema, which calls the spawn a CENTRE that UEs distribute
    from — the 60 m was never the user's intent.
    """
    return _clamp(0.6 * isd_cell, 80.0, 500.0)


# A group that WALKS A PATH is a formation, not a population. Keep it loose
# enough that no two UEs are co-located and tight enough that the path still
# means something.
_UE_FORMATION_M = 15.0


def _group_spread_m(group, population_m: float) -> float:
    """How far THIS group fans out. A group with an explicit waypoint path gets a
    tight formation; a group with only a spawn point gets the population disc.

    The distinction matters because the two say different things. A bare spawn
    point is, per the schema, a CENTRE that UEs distribute from — spreading it
    wide is what the user meant, and it is what keeps every cell loaded. A
    WAYPOINT PATH is the opposite: an explicit statement of where these UEs are
    and where they go, and the population disc destroys it. Measured 2026-09-09
    on a handover design whose four pedestrians were meant to walk a 40 m line:
    the disc radius came out at 80 m, so they were scattered over 160 m — one
    parked on Site-A, two by Site-B, one at Site-C — each shuffling 4 m east.
    Nothing errored; the run reported zero handovers and read as a broken
    feature, when what actually shipped was four static UEs and no crossing.
    """
    mob = getattr(group, "mobility", None)
    if len(list(getattr(mob, "waypoints", None) or [])) >= 2:
        return _UE_FORMATION_M
    return population_m


def _ue_scenario_positions(ues: list, n_ue: int, cx: float, cy: float,
                           spread_m: float) -> list:
    """Expand UE groups into exactly n_ue per-UE scenario positions: split the
    n_ue slots across groups by count share, distribute each group's UEs over a
    disc around its spawn point, translated by the SHARED scene centroid (cx, cy)
    so the UEs keep their real position relative to the cells.

    `spread_m` is the POPULATION radius; a group that walks a waypoint path
    overrides it with a formation — see _group_spread_m."""
    if not ues:
        return []
    centres = _apply_offset([(g.spawn_position.x, g.spawn_position.y) for g in ues], cx, cy)
    alloc = _ue_alloc(ues, n_ue)
    out = []
    for grp, (gx, gy), k in zip(ues, centres, alloc):
        # Per group, never rebinding spread_m — one walking group must not turn
        # every later group into a formation too.
        radius = _group_spread_m(grp, spread_m)
        # Sunflower (golden-angle) placement: fills the disc evenly, so the
        # UEs cover an area like a real population instead of sitting on one
        # ring at a single radius.
        pts = []
        for j in range(k):
            r = radius * math.sqrt((j + 0.5) / max(1, k))
            ang = j * 2.399963229728653
            pts.append((r * math.cos(ang), r * math.sin(ang)))
        if not pts:
            continue
        # ...then re-centre it on the spawn point. The golden-angle spiral has no
        # point at the origin and its own centroid drifts off-centre at small k —
        # badly. A group of ONE (the single walking UE of a handover test) landed
        # a full spread_m * sqrt(0.5) away: measured 127 m east of where it was
        # placed, which moved it from 80 m off the serving cell to 207 m and
        # pushed the far end of its waypoint path 47 m PAST the target cell
        # instead of 80 m short of it. The docstring's promise — "the UEs keep
        # their real position relative to the cells" — only holds once the
        # group's centroid is the spawn point, so make it so for every k.
        mx = sum(q[0] for q in pts) / len(pts)
        my = sum(q[1] for q in pts) / len(pts)
        for (dx, dy) in pts:
            out.append((_clamp(gx + dx - mx, _SCEN_MIN, _SCEN_MAX),
                        _clamp(gy + dy - my, _SCEN_MIN, _SCEN_MAX)))
    return out[:n_ue]


# 3GPP TR 38.901 pathloss is only defined for d2D >= 10 m, and at d2D = 0 it is a
# log10(0): a UE dropped exactly on a cell NaNs that link and silently kills the
# run's whole DU reporting (no DRB.UEThpDl / PRB at all — verified on the engine).
# A map drag can put a UE spawn straight onto a cell, so keep every UE clear of
# every base station — including the LTE hub the engine pins at the scene centre.
_MIN_UE_BS_M = 10.0


def _separate_ues(ue_pts: list, bs_pts: list) -> list:
    """Push any UE that landed on top of a base station out to _MIN_UE_BS_M,
    radially (so it keeps its bearing). A 10 m nudge is invisible at scenario
    scale and never reorders which cell is nearest."""
    out = []
    for (x, y) in ue_pts:
        for (bx, by) in bs_pts:
            dx, dy = x - bx, y - by
            d = math.hypot(dx, dy)
            if d < _MIN_UE_BS_M:
                if d < 1e-9:
                    dx, dy, d = 1.0, 0.0, 1.0   # exactly co-located: any bearing
                x, y = bx + dx / d * _MIN_UE_BS_M, by + dy / d * _MIN_UE_BS_M
        out.append((_clamp(x, _SCEN_MIN, _SCEN_MAX), _clamp(y, _SCEN_MIN, _SCEN_MAX)))
    return out


def _rebalance_ue_cells(ue_pts: list, gnb_pts: list) -> list:
    """Keep any one cell from serving too many UEs — the mmWave heap-abort guard.

    ns-O-RAN's mmWave stack corrupts its heap (malloc(): invalid size -> abort,
    no KPM) when ~a dozen UEs land on ONE cell, which a 'stadium crowd on a
    single spawn point' produces. Two things make a light touch insufficient:
    the engine attaches by RSRP (not geometry), so a cell in the MIDDLE of a
    cluster draws even the UEs geometrically nearest a neighbour; and the abort
    is PROBABILISTIC (verified on the user's exact geometry: one run put 7 UEs on
    the centre cell and survived, another put 12 and aborted). So relocating just
    the geometric overflow isn't enough — a centre cell still steals its
    neighbours' UEs and drifts back into the abort zone.

    When any cell would be nearest to more than the trigger cap, rebuild the UEs
    as a BALANCED scatter: an even share per cell, each pinned in a 30-75 m ring
    so that cell's RSRP unambiguously owns it. The engine is a fixed hub-and-ring
    LOAD VALIDATOR, not a stadium simulator; a dense venue really is served by
    several sectors sharing the crowd, so this preserves what the run measures
    (per-cell load, UE count, band) while keeping the stack alive. Well-spread
    designs (no cell over the cap) are returned untouched.
    """
    n = len(gnb_pts)
    if n < 2 or not ue_pts:
        return ue_pts
    m = len(ue_pts)
    cap = max(UE_PER_CELL_MAX, math.ceil(m / n))

    def nearest(p):
        return min(range(n), key=lambda c: math.hypot(p[0] - gnb_pts[c][0],
                                                       p[1] - gnb_pts[c][1]))

    counts = [0] * n
    for p in ue_pts:
        counts[nearest(p)] += 1
    if max(counts) <= cap:
        return ue_pts                       # already engine-safe; leave the design

    alloc = [m // n + (1 if i < m % n else 0) for i in range(n)]
    out = []
    for c in range(n):
        cxu, cyu = gnb_pts[c]
        k = alloc[c]
        for j in range(k):
            # golden-angle ring, 30-75 m out; per-cell phase so cells differ.
            r = 30.0 + 45.0 * math.sqrt((j + 0.5) / max(1, k))
            ang = j * 2.399963229728653 + c
            out.append((_clamp(cxu + r * math.cos(ang), _SCEN_MIN, _SCEN_MAX),
                        _clamp(cyu + r * math.sin(ang), _SCEN_MIN, _SCEN_MAX)))
    return out[:m]


def _fmt_positions(pts: list, heights: list | None = None) -> str:
    """';'-sep 'x,y' or 'x,y,z'. The engine's parser hardcoded z=3 for every node
    until 2026-07-26, so base_stations[].position.z and the UE height were both
    inert — a 25 m mast and a 1.5 m handset were the same height to the channel
    model. Two-field records still parse and keep the old 3 m."""
    if heights is None:
        return ";".join(f"{x:.1f},{y:.1f}" for (x, y) in pts)
    return ";".join(f"{x:.1f},{y:.1f},{_clamp(z, 0.5, 150.0):.1f}"
                    for (x, y), z in zip(pts, heights))


# Exterior wall material → the code the scenario maps to ns-3 Building::ExtWallsType.
_WALL_CODE = {WallMaterial.WOOD: 0, WallMaterial.CONCRETE_WITH_WINDOWS: 1, WallMaterial.CONCRETE: 2}
# Building type -> the code the scenario maps to ns-3 Building::Type. Was
# hardcoded Office for every building.
_BLD_TYPE_CODE = {BuildingType.RESIDENTIAL: 0, BuildingType.OFFICE: 1,
                  BuildingType.INDUSTRIAL: 2}
# The 3GPP propagation environment the engine should build. UMa-Buildings is the
# same UMa pathloss — the buildings themselves switch the condition model.
_PROP_SCENARIO = {PropagationScenario.UMA: "uma", PropagationScenario.UMA_BUILDINGS: "uma",
                  PropagationScenario.UMI: "umi", PropagationScenario.RMA: "rma"}
_MAX_BUILDINGS = 30   # keep ns-3 fast; the engine is a validation tier, not a GIS


def _building_rects(buildings: list, cx: float, cy: float) -> list:
    """Level-4: the config's buildings as (x0, x1, y0, y1, z0, z1, wall) in
    scenario coords — translated by the SAME scene offset as the cells/UEs so
    obstacles stay where the user drew them relative to the RAN."""
    # Overlapping boxes (an OSM import's adjacent footprints) NS_FATAL the whole
    # ns-3 run — same guard as the REM path, applied BEFORE the count cap so the
    # 30 kept are all valid.
    buildings, _ = ns3_bridge.deoverlap_buildings(buildings)
    rects = []
    for b in (buildings or [])[:_MAX_BUILDINGS]:
        bb = b.bounds
        vals = (bb.x_min, bb.x_max, bb.y_min, bb.y_max, bb.z_min, bb.z_max)
        if not all(math.isfinite(v) for v in vals):
            continue
        pts = _apply_offset([(bb.x_min, bb.y_min), (bb.x_max, bb.y_max)], cx, cy)
        if len(pts) != 2:
            continue
        (x0, y0), (x1, y1) = pts
        if x1 - x0 < 1.0 or y1 - y0 < 1.0:      # clamped away / degenerate
            continue
        z0 = max(0.0, bb.z_min)
        z1 = max(z0 + 1.0, bb.z_max)
        rects.append((x0, x1, y0, y1, z0, z1, _WALL_CODE.get(b.wall_material, 2),
                      _BLD_TYPE_CODE.get(b.building_type, 1),
                      int(_clamp(getattr(b, "floors", 1) or 1, 1, 30)),
                      int(_clamp(getattr(b, "rooms_x", 1) or 1, 1, 20)),
                      int(_clamp(getattr(b, "rooms_y", 1) or 1, 1, 20))))
    return rects


def _fmt_buildings(rects: list) -> str:
    """The scenario turns these into real ns-3 Buildings and switches the channel
    to BuildingsChannelConditionModel (real LOS/NLOS).

    Fields 8-11 (type, floors, roomsX, roomsY) used to be hardcoded Office/1/1/1
    in the scenario, so four schema fields were inert. ns-3 counts internal
    walls from the room and floor grid, so they are real propagation inputs.
    """
    return ";".join(f"{x0:.1f},{x1:.1f},{y0:.1f},{y1:.1f},{z0:.1f},{z1:.1f},{w},{t},{fl},{rx},{ry}"
                    for (x0, x1, y0, y1, z0, z1, w, t, fl, rx, ry) in rects)


# Clearance when evicting a UE from a building it was drawn inside.
_BLD_MARGIN_M = 2.0


def _push_out_of_buildings(ue_pts: list, rects: list) -> list:
    """Move any UE that starts inside a building out through its nearest wall.

    The engine walks UEs with ns-3's RandomWalk2dOutdoorMobilityModel, which
    asserts every node starts OUTDOORS — a UE inside a building aborts the whole
    run at t=0 (`NS_ASSERT cond="!inside"`), so one badly placed UE takes the
    scenario down. Drawing a building over a UE spawn is an easy thing to do on
    the map, so fix the placement rather than let the engine die.
    """
    if not rects:
        return ue_pts
    out = []
    for (x, y) in ue_pts:
        for _ in range(4):          # evicting from one box can enter another
            inside = next((r for r in rects if r[0] <= x <= r[1] and r[2] <= y <= r[3]),
                          None)
            if inside is None:
                break
            x0, x1, y0, y1 = inside[:4]
            _, (x, y) = min(
                ((x - x0, (x0 - _BLD_MARGIN_M, y)), (x1 - x, (x1 + _BLD_MARGIN_M, y)),
                 (y - y0, (x, y0 - _BLD_MARGIN_M)), (y1 - y, (x, y1 + _BLD_MARGIN_M))),
                key=lambda t: t[0])
        out.append((_clamp(x, _SCEN_MIN, _SCEN_MAX), _clamp(y, _SCEN_MIN, _SCEN_MAX)))
    return out


# RU sleep depths as the engine numbers them, with the physical wake-up latency
# each one implies (the flexible BS power model's SM1/SM2/SM3 sleep modes).
_SLEEP_DEPTH = {"none": 0, "micro": 1, "light": 2, "deep": 3}
_SLEEP_WAKE_MS = {"none": 0.0, "micro": 0.071, "light": 1.0, "deep": 10.0}


def _sleep_params(config: NSGSimulationConfig, bss: list) -> dict:
    """Per-cell RU sleep profiles + the network-wide controller thresholds.

    Returns an EMPTY spec unless at least one cell actually asks to sleep, so
    configs that don't use the lever produce byte-identical engine runs.
    """
    specs, any_on = [], False
    for b in bss:
        sp = getattr(b.radio, "sleep", None)
        mode = getattr(sp, "mode", "none") if sp else "none"
        depth = _SLEEP_DEPTH.get(mode, 0)
        idle = float(getattr(sp, "idle_timer_ms", 200.0) or 200.0) if sp else 200.0
        wake = getattr(sp, "wake_delay_ms", None) if sp else None
        if wake is None:
            wake = _SLEEP_WAKE_MS.get(mode, 10.0)
        specs.append(f"{depth},{max(1.0, idle):.1f},{max(0.0, float(wake)):.3f}")
        any_on = any_on or depth > 0
    if not any_on:
        return {"gnb_sleep": ""}
    s = config.settings
    draws = getattr(s, "sleep_power_a", None) or {}
    out = {
        "gnb_sleep": ";".join(specs),
        "sleep_wake_load_pct": round(_clamp(
            float(getattr(s, "sleep_wake_load_pct", 60.0) or 60.0), 0.0, 100.0), 1),
        "sleep_wake_hold_ms": round(_clamp(
            float(getattr(s, "sleep_wake_hold_ms", 500.0) or 0.0), 0.0, 60000.0), 1),
        "sleep_idle_duty_pct": round(_clamp(
            float(getattr(s, "sleep_idle_duty_pct", 0.0) or 0.0), 0.0, 100.0), 1),
    }
    for key, env in (("micro", "sleep_micro_a"), ("light", "sleep_light_a"),
                     ("deep", "sleep_deep_a")):
        if draws.get(key) is not None:
            out[env] = round(_clamp(float(draws[key]), 0.0, 2000.0), 2)
    return out


def _first_not_none(*vals):
    """The first argument that is not None. `a or b` cannot be used on a
    numeric config field: 0.0 and 0 are legitimate settings, and `or` turns
    them into the default."""
    for v in vals:
        if v is not None:
            return v
    return None


def nsoran_params(config: NSGSimulationConfig,
                  engine_clamped: bool = True) -> dict:
    """Derive the ns-O-RAN scenario knobs from a platform config — cell/UE count,
    band, spatial scale, exact positions, per-gNB power — clamped to the engine's
    validated envelope (engine_envelope.py). Shared by the BATCH validation and the
    LIVE session so both drive the real engine with the user's ACTUAL scenario, not
    a fixed default. Bigger designs use the first n_gnb cells (+1 LTE hub on top)."""
    bss, ues = config.base_stations, config.ue_groups
    # `engine_clamped=False` drops the caps that exist ONLY because ns-3 is
    # underneath — GNB_MAX 16, UE_MAX 150, the per-cell density trigger — and
    # marks the payload so nothing can hand it to the engine afterwards. The
    # fast tier computes a window in closed form and has no such ceilings, and
    # inheriting them is how a comparison against an eighteen-cell reference
    # ends up silently missing two sectors. Everything else is IDENTICAL,
    # including the recentring and the placement guards, because the point of
    # sharing this function is that both tiers see one scene.
    n_gnb = clamp_gnb(len(bss)) if engine_clamped else max(1, len(bss))
    n_ue = (clamp_ue(sum(g.count for g in ues), n_gnb, config_bw_mhz(config),
                     config_numerology(config))
            if engine_clamped else max(1, sum(g.count for g in ues)))
    band = bss[0].radio.band if bss else None
    bw_mhz = clamp_bw_mhz(band.bandwidth_mhz) if band else 20.0
    # ONE shared translation for the whole scene (cells + UEs + buildings) so the
    # design's relative geometry survives the move into the engine's fixed area.
    gnb_cfg = [(b.position.x, b.position.y) for b in bss[:n_gnb]]
    cx, cy = _scene_centroid(
        gnb_cfg + [(g.spawn_position.x, g.spawn_position.y) for g in ues]
    )
    # ...then slide the whole scene off the engine's LTE anchor if a cell is on it.
    cx, cy = _clear_hub_centroid(gnb_cfg, cx, cy)
    gnb_scen = _apply_offset(gnb_cfg, cx, cy)
    isd_cell = round(_clamp(_layout_spread(
        [(b.position.x, b.position.y) for b in bss]) or 500.0, 150.0, 1800.0))
    bld_rects = _building_rects(config.buildings, cx, cy)
    # Two placement guards, in order: get the UEs out of any building (the engine's
    # outdoor mobility model refuses to start otherwise), then off any base station
    # (a zero-distance link is a log10(0)). The engine pins its LTE anchor at the
    # scene centre, so that counts as a base station too.
    # Order: place UEs around their spawn -> redistribute any single-cell
    # pile-up that would crash the engine -> evict from buildings -> off cells.
    ue_scen = _separate_ues(
        _push_out_of_buildings(
            _rebalance_ue_cells(
                _ue_scenario_positions(ues, n_ue, cx, cy, _ue_spread_m(isd_cell)),
                gnb_scen),
            bld_rects),
        gnb_scen + [(_SCEN_CENTER, _SCEN_CENTER)])
    return {
        **({} if engine_clamped else {"_tier_b_only": True}),
        "n_gnb": n_gnb,
        "n_ue": n_ue,
        "freq_hz": (clamp_freq_mhz(band.frequency_mhz) * 1e6) if band else 3.5e9,
        "bw_hz": bw_mhz * 1e6,
        "isd_cell": isd_cell,
        "isd_ue": round(_clamp(_layout_spread([(g.spawn_position.x, g.spawn_position.y) for g in ues]) or 800.0, 150.0, 1800.0)),
        # Heights ride along as a third field — see _fmt_positions.
        "gnb_positions": _fmt_positions(
            gnb_scen, [b.position.z for b in bss[:n_gnb]]),
        "ue_positions": _fmt_positions(ue_scen, _ue_heights(ues, n_ue)),
        "gnb_powers": ";".join(
            f"{max(0.0, min(50.0, b.radio.tx_power_dbm)):.1f}" for b in bss[:n_gnb]
        ),
        # Level-4: real buildings → real LOS/NLOS in the engine.
        "buildings": _fmt_buildings(bld_rects),
        # Phase 2c: per-cell antenna panel. Orientation is only a real lever
        # with the directional element — see _gnb_antenna_specs.
        "gnb_antenna": _gnb_antenna_specs(bss[:n_gnb]),
        # Per-cell carrier, for the tier that can run more than one. Empty
        # for a single-band scene, and never forwarded to ns-3 — see
        # _gnb_band_specs.
        "gnb_bands": _gnb_band_specs(bss[:n_gnb]),
        "antenna_element": (_enum_str(getattr(
            config.settings, "antenna_element", None), "isotropic")
            if _enum_str(getattr(config.settings, "antenna_element", None), "isotropic")
            in ("isotropic", "3gpp", "cosine", "parabolic", "circular")
            else "isotropic"),
        # Phase 2c radio: AMC + HARQ, both real ns-3 attributes. The scheduler
        # is deliberately NOT passed — the fork's three alternatives are dead
        # code (see schema.py), so the platform always leaves the engine on its
        # working flex-TTI scheduler, which is also the only one that enforces
        # slice quotas.
        "amc_model": ("ShannonModel" if str(getattr(
            config.settings, "amc_model", "error_model") or "error_model") == "shannon"
            else "ErrorModel"),
        "amc_ber": round(_clamp(float(
            getattr(config.settings, "amc_target_ber", 1e-5) or 1e-5), 1e-9, 0.5), 9),
        "harq_enabled": "1" if getattr(config.settings, "harq_enabled", True) else "0",
        "harq_processes": max(1, min(200, int(
            getattr(config.settings, "harq_processes", 100) or 100))),
        # Fields that were in the schema and the UI from the start and never
        # reached ns-3 — the engine hardcoded each one. See the parity audit.
        "prop_scenario": _PROP_SCENARIO.get(
            getattr(config.settings, "scenario", None), "umi"),
        "shadowing": "1" if getattr(config.settings, "shadowing_enabled", True) else "0",
        # The engine defines NrNumerology2/3 only, so 0-6 in the schema was never
        # honest; clamp rather than pretend.
        "numerology": int(_clamp(
            getattr(band, "numerology", 2) if band else 2, 2, 3)),
        # rngRun already worked on the engine side — the platform simply never
        # sent it, which is what blocked multi-seed dataset generation.
        **({"rng_run": int(config.settings.random_seed)}
           if getattr(config.settings, "random_seed", None) is not None else {}),
        # KPM reporting cadence. Was fixed at 0.25 s (live) / 0.5 s (batch).
        # The upper bound is 2.0 because that is what the ENGINE accepts:
        # scenario-zero's GlobalValue is MakeDoubleChecker<double>(0.01, 2.0).
        # This clamp used to allow 5.0, so report_interval_ms above 2000 — a
        # value the schema calls legal (le=5000) — made ns-3 reject its own
        # command line, print usage and exit before the RAN ever started. The
        # run then came back `no_data` with ue_count 0 and no span, and nothing
        # said why: measured 2026-09-16, `Invalid command-line argument:
        # --indicationPeriodicity=5.0` in the sim log, zero [NSG-] lines, and an
        # xApp left resending Setup Requests into an empty RIC.
        "ind_period_s": round(_clamp(float(
            getattr(config.settings, "report_interval_ms", 500) or 500) / 1000.0,
            0.1, 2.0), 3),
        # How long the engine collects, which IS the run's simulated horizon.
        # Sent only when the scenario asked for one — omitted, the engine sizes
        # the window from the UE count, and that default is what every campaign
        # and every ordinary validation run should keep.
        **({"kpm_collect_s": int(_clamp(float(config.settings.kpm_collect_s),
                                        10, 14400))}
           if getattr(config.settings, "kpm_collect_s", None) else {}),
        # Mobility robustness / cell selection + the DynamicTtt curve. All plain
        # LteEnbRrc attributes; DynamicTtt is the DEFAULT handover mode and none
        # of its four parameters were reachable before.
        "outage_db": round(_clamp(float(
            getattr(config.settings, "outage_threshold_db", -5.0)), -30.0, 10.0), 1),
        # ns-3 takes QRxLevMin in the 3GPP TS 36.133 signalled UNIT (2 dB steps,
        # -70..-22 = -140..-44 dBm), not in dBm. Passing dBm straight through is
        # rejected outright by the attribute's checker and aborts the run before
        # ns-3 starts — which surfaces as an engine "no_data", not as a bad value.
        "qrxlevmin": int(_clamp(
            round(float(getattr(config.settings, "q_rx_lev_min_dbm", -140) or -140) / 2.0),
            -70, -22)),
        "ttt_min_ms": int(_clamp(getattr(config.settings, "ho_dyn_ttt_min_ms", 25), 1, 255)),
        "ttt_max_ms": int(_clamp(getattr(config.settings, "ho_dyn_ttt_max_ms", 150), 1, 255)),
        "ttt_diff_min_db": round(_clamp(float(
            getattr(config.settings, "ho_dyn_diff_min_db", 3.0)), 0.0, 30.0), 1),
        "ttt_diff_max_db": round(_clamp(float(
            getattr(config.settings, "ho_dyn_diff_max_db", 20.0)), 0.0, 60.0), 1),
        # Handover robustness timers — the two sides of the MRO trade-off. Plain
        # LteEnbRrc TimeValue attributes; the engine's own default is 45 ms.
        "ho_join_ms": round(_clamp(float(
            getattr(config.settings, "ho_joining_timeout_ms", 45.0) or 45.0),
            1.0, 5000.0), 1),
        "ho_leave_ms": round(_clamp(float(
            getattr(config.settings, "ho_leaving_timeout_ms", 45.0) or 45.0),
            1.0, 5000.0), 1),
        # Link-level error model. ONE field drives BOTH attributes on purpose:
        # ns-3 requires the AMC and the receiver to share an MCS table, and
        # letting them disagree is a footgun the platform can simply prevent.
        "error_model_type": _ERROR_MODEL.get(
            str(getattr(config.settings, "error_model", "lte_mi") or "lte_mi"),
            _ERROR_MODEL["lte_mi"]),
        # PHY / scheduler detail.
        "gnb_transient_ns": int(_clamp(
            getattr(config.settings, "gnb_transient_ns", 320000), 0, 10000000)),
        "gnb_noise_filter": "1" if getattr(
            config.settings, "gnb_noise_and_filter", False) else "0",
        "ue_mac_sinr_us": round(_clamp(float(
            getattr(config.settings, "ue_mac_sinr_period_us", 1.6)), 0.01, 1e4), 3),
        "fixed_tti": "1" if getattr(config.settings, "fixed_tti", False) else "0",
        # Transport network. X2 carries handover signalling, so its delay is part
        # of the real handover latency the 2b work measured over a perfect link.
        "x2_delay_ms": round(_clamp(float(
            getattr(config.settings, "x2_link_delay_ms", 1.0)), 0.0, 200.0), 3),
        "x2_gbps": round(_clamp(float(
            getattr(config.settings, "x2_link_gbps", 100.0)), 0.001, 400.0), 3),
        "s1u_delay_ms": round(_clamp(float(
            getattr(config.settings, "s1u_link_delay_ms", 1.0)), 0.0, 200.0), 3),
        "s1u_gbps": round(_clamp(float(
            getattr(config.settings, "s1u_link_gbps", 10.0)), 0.001, 400.0), 3),
        "s1u_mtu": int(_clamp(getattr(config.settings, "s1u_link_mtu", 2000), 576, 65535)),
        "s1ap_delay_ms": round(_clamp(float(
            getattr(config.settings, "s1ap_link_delay_ms", 15.0)), 0.0, 500.0), 3),
        "s1ap_gbps": round(_clamp(float(
            getattr(config.settings, "s1ap_link_gbps", 10.0)), 0.001, 400.0), 3),
        "x2_mtu": int(_clamp(getattr(config.settings, "x2_link_mtu", 10000), 576, 65535)),
        # No wall-loss / shadow-sigma params: BuildingsPropagationLossModel is
        # never instantiated by this scenario (see schema.py).
        "o2i_threshold": round(_clamp(float(
            getattr(config.settings, "o2i_threshold", 0.0)), 0.0, 1.0), 3),
        "o2i_low_loss": round(_clamp(float(
            getattr(config.settings, "o2i_low_loss_threshold", 1.0)), 0.0, 1.0), 3),
        # PHY/MAC timing budget.
        "harq_dl_timeout": int(_clamp(
            getattr(config.settings, "harq_dl_timeout", 20), 1, 200)),
        "tb_decode_us": int(_clamp(
            getattr(config.settings, "tb_decode_latency_us", 100), 1, 10000)),
        # ul_sched_delay deliberately not sent: UlSchedDelay>1 crashes the engine.
        # SVD beamforming solver cost. Only bites when beamforming_model='svd'.
        "svd_iters": int(_clamp(getattr(config.settings, "svd_max_iterations", 30), 1, 1000)),
        "svd_tol": float(_clamp(float(
            getattr(config.settings, "svd_tolerance", 1e-8)), 1e-15, 1.0)),
        "svd_cache": "1" if getattr(config.settings, "svd_use_cache", True) else "0",
        # Beamforming, control plane, and the KPM families collected. The KPM
        # toggles and ideal-RRC ride scenario GlobalValues, not attributes: the
        # scenario Config::SetDefault's all of them after cmd.Parse.
        "beamforming": _BEAMFORMING.get(
            str(getattr(config.settings, "beamforming_model", "svd") or "svd"),
            _BEAMFORMING["svd"]),
        "ideal_rrc": "1" if getattr(config.settings, "use_ideal_rrc", True) else "0",
        "pdsch_cqi": "1" if getattr(config.settings, "use_pdsch_for_cqi", False) else "0",
        "rrc_t300_ms": int(_clamp(getattr(config.settings, "rrc_t300_ms", 100), 10, 2000)),
        "kpm_du": "1" if getattr(config.settings, "kpm_report_du", True) else "0",
        "kpm_cu_up": "1" if getattr(config.settings, "kpm_report_cu_up", True) else "0",
        "kpm_cu_cp": "1" if getattr(config.settings, "kpm_report_cu_cp", True) else "0",
        # kpm_reduced deliberately not sent: the reduced PM container fails to
        # ASN.1-encode and aborts the run (see schema.py).
        # Channel realism. Blockage is the characteristic mmWave impairment and
        # ships OFF, so every run so far has been optimistic about link stability.
        "blockage": "1" if getattr(config.settings, "blockage_enabled", False) else "0",
        "blocker_speed": round(_clamp(float(
            getattr(config.settings, "blocker_speed_mps", 1.0)), 0.01, 30.0), 2),
        "n_blockers": int(_clamp(
            getattr(config.settings, "non_self_blocking_count", 4), 0, 20)),
        "portrait_mode": "1" if getattr(config.settings, "portrait_mode", True) else "0",
        "v_scatt": round(_clamp(float(
            getattr(config.settings, "scatterer_speed_mps", 0.0)), 0.0, 50.0), 2),
        "o2i_loss": "1" if getattr(
            config.settings, "building_penetration_loss", True) else "0",
        "channel_update_ms": round(_clamp(float(
            getattr(config.settings, "channel_update_ms", 100.0)), 1.0, 10000.0), 1),
        # RLC. rlc_buffer_mb rides the scenario's own bufferSize GlobalValue,
        # because the scenario Config::SetDefault's MaxTxBufferSize from it after
        # cmd.Parse and would overwrite an --ns3:: argument.
        "rlc_am": "1" if getattr(config.settings, "rlc_am_enabled", False) else "0",
        "rlc_buffer_mb": int(_clamp(getattr(config.settings, "rlc_buffer_mb", 10), 1, 1000)),
        "rlc_reorder_ms": round(_clamp(float(
            getattr(config.settings, "rlc_reordering_timer_ms", 1.0)), 0.01, 1000.0), 2),
        "rlc_poll_ms": round(_clamp(float(
            getattr(config.settings, "rlc_poll_retransmit_ms", 2.0)), 0.01, 1000.0), 2),
        "rlc_status_ms": round(_clamp(float(
            getattr(config.settings, "rlc_status_prohibit_ms", 1.0)), 0.01, 1000.0), 2),
        # PHY/MAC on the mmWave DATA PATH. The LTE namesakes of these would only
        # touch the idle anchor cell — every UE is dual-connected and all user
        # traffic rides the mmWave gNBs.
        "ue_nf_db": round(_clamp(float(
            getattr(config.settings, "ue_noise_figure_db", 5.0)), 0.0, 20.0), 2),
        "gnb_nf_db": round(_clamp(float(
            getattr(config.settings, "gnb_noise_figure_db", 5.0)), 0.0, 20.0), 2),
        "ue_tx_dbm": round(_clamp(float(
            getattr(config.settings, "ue_tx_power_dbm", 30.0)), -20.0, 40.0), 1),
        "ue_outage_db": round(_clamp(float(
            getattr(config.settings, "ue_outage_threshold_db", -5.0)), -30.0, 10.0), 1),
        "ue_n310": int(_clamp(getattr(config.settings, "ue_n310", 2), 1, 20)),
        "cqi_period_ms": int(_clamp(
            getattr(config.settings, "cqi_report_period_ms", 10), 1, 100)),
        "cqi_timer_ms": int(_clamp(
            getattr(config.settings, "cqi_timer_threshold_ms", 100), 1, 1000)),
        "sinr_period_us": int(_clamp(
            getattr(config.settings, "sinr_estimate_period_us", 1600), 100, 100000)),
        "ue_sinr_period_us": round(_clamp(float(
            getattr(config.settings, "ue_sinr_estimate_period_us", 25.6)), 1.0, 1e4), 2),
        "sym_per_slot": int(_clamp(getattr(config.settings, "sym_per_slot", 6), 1, 14)),
        "ra_preambles": int(_clamp(getattr(config.settings, "ra_preambles", 50), 1, 64)),
        "ra_trans_max": int(_clamp(
            getattr(config.settings, "ra_preamble_trans_max", 50), 1, 200)),
        "ra_resp_window": int(_clamp(
            getattr(config.settings, "ra_response_window", 3), 1, 20)),
        # data_err_model deliberately not sent: disabling the error model crashes
        # the engine (see schema.py). The env var stays for direct engine use.
        # Flexible-TTI has no configured frame pattern, so restricting the
        # scheduler to one direction is the real DL/UL control this MAC offers.
        "sched_dl_only": "1" if getattr(
            config.settings, "sched_direction", "both") == "dl_only" else "0",
        "sched_ul_only": "1" if getattr(
            config.settings, "sched_direction", "both") == "ul_only" else "0",
        # MCS pinning is two attributes: the flag and the value. Only sent when
        # the config asks, so AMC stays in charge by default.
        **({"fixed_mcs_dl": int(_clamp(config.settings.fixed_mcs_dl, 0, 28))}
           if getattr(config.settings, "fixed_mcs_dl", None) is not None else {}),
        **({"fixed_mcs_ul": int(_clamp(config.settings.fixed_mcs_ul, 0, 28))}
           if getattr(config.settings, "fixed_mcs_ul", None) is not None else {}),
        # RU power profile — only sent when overridden, so the engine's built-in
        # model stays in charge by default.
        **{k: round(_clamp(float(v), 0.01, 2000.0), 2)
           for k, v in (("ru_tx_a", getattr(config.settings, "ru_tx_current_a", None)),
                        ("ru_rx_a", getattr(config.settings, "ru_rx_current_a", None)),
                        ("ru_idle_a", getattr(config.settings, "ru_idle_current_a", None)))
           if v is not None},
        # Level-5: per-UE slice (5QI) + DL demand → slices really load the RAN.
        "ue_slices": _ue_slice_specs(ues, n_ue),
        # Config-driven initial slice partitioning. The live E2 control path
        # still overrides it at runtime; this is where the run STARTS.
        "slice_quotas": _slice_policy_spec(config),
        # The rest of LteEnbRrc: measurement smoothing and the connection
        # timers. srs_periodicity is a TABLE INDEX, not a duration — ns-3 looks
        # it up in TS 36.213 Table 8.2-1, so the schema pins it to that set.
        "rsrp_filter_coeff": int(_clamp(int(getattr(
            config.settings, "rsrp_filter_coefficient", 4) or 0), 0, 19)),
        "rsrq_filter_coeff": int(_clamp(int(getattr(
            config.settings, "rsrq_filter_coefficient", 4) or 0), 0, 19)),
        "conn_req_timeout_ms": round(_clamp(float(getattr(
            config.settings, "connection_request_timeout_ms", 15.0) or 15.0), 1.0, 1000.0), 3),
        "conn_setup_timeout_ms": round(_clamp(float(getattr(
            config.settings, "connection_setup_timeout_ms", 150.0) or 150.0), 1.0, 5000.0), 3),
        "conn_rej_timeout_ms": round(_clamp(float(getattr(
            config.settings, "connection_rejected_timeout_ms", 30.0) or 30.0), 1.0, 5000.0), 3),
        "srs_periodicity": int(getattr(config.settings, "srs_periodicity", 320) or 0)
        if int(getattr(config.settings, "srs_periodicity", 320) or 0)
        in (0, 2, 5, 10, 20, 40, 80, 160, 320) else 320,
        "sib_periodicity_ms": round(_clamp(float(getattr(
            config.settings, "system_information_periodicity_ms", 5.0) or 5.0), 1.0, 1000.0), 3),
        "default_tx_mode": int(_clamp(int(getattr(
            config.settings, "default_transmission_mode", 0) or 0), 0, 6)),
        "report_all_ue_meas": "1" if getattr(
            config.settings, "report_all_ue_meas", True) else "0",
        # Anomaly injection: faults staged inside the running simulation.
        "gnb_faults": _fault_spec(config, bss[:n_gnb]),
        "admit_handover": "1" if getattr(
            config.settings, "admit_handover_request", True) else "0",
        # MRO: handover SINR margin + per-UE walk speeds. Without these the
        # engine hardcoded margin 3 dB / speed 2-4 m/s, so the config's
        # mobility was a silent fake lever.
        "ho_sinr_db": round(_clamp(float(
            getattr(config.settings, "ho_sinr_margin_db", 3.0) or 3.0), 1.0, 20.0), 1),
        # HO trigger mode + time-to-trigger — real LteEnbRrc levers (no rebuild:
        # handoverMode is a scenario GlobalValue, FixedTttValue a direct ns-3 attr).
        "ho_mode": str(getattr(config.settings, "handover_mode", "DynamicTtt") or "DynamicTtt"),
        "ho_ttt_ms": max(1, min(255, int(getattr(config.settings, "ho_ttt_ms", 110) or 110))),
        "ue_speeds": _ue_speed_specs(ues, n_ue),
        # Phase 2b mobility: waypoint paths (the lever that actually crosses a
        # cell boundary) + the 3GPP measurement-event layer. ho_check_period_ms
        # is the load-bearing one: ns-O-RAN upstream disabled the RRC's periodic
        # handover re-evaluation, so with 0 the RAN decides once at t=0 and no
        # amount of motion can ever hand a UE over.
        "ue_waypoints": _ue_waypoint_specs(ues, n_ue, cx, cy, ue_scen,
                                           getattr(config.settings, "duration_s", 180) or 180),
        "ho_check_ms": max(0, min(60000, int(
            getattr(config.settings, "ho_check_period_ms", 100) or 0))),
        "ho_a3_entry": "1" if getattr(config.settings, "ho_a3_entry", True) else "0",
        "ho_hyst_db": round(_clamp(float(
            getattr(config.settings, "ho_hysteresis_db", 0.0) or 0.0), 0.0, 30.0), 1),
        # -1000 is the engine's "disabled" sentinel for the A2 threshold.
        # `or` would fold the legitimate value 0.0 dB into that sentinel, so
        # "hand over only once the serving cell is at 0 dB" silently became
        # "hand over whenever a neighbour is better" — the loosest setting
        # there is, from the field that asks for one of the strictest.
        "ho_a2_db": round(_clamp(float(
            _first_not_none(getattr(config.settings, "ho_a2_threshold_db", None),
                            -1000.0)), -1000.0, 30.0), 1),
        # Phase 2a energy: per-cell RU sleep profiles + the controller's
        # network-wide thresholds. Empty spec => no sleep management at all, so
        # a config that doesn't ask for it runs exactly as before.
        **_sleep_params(config, bss[:n_gnb]),
        # Not an engine knob (the engine ignores unknown keys): the shared scene
        # centroid, so the live proxy can convert the engine's scenario-frame UE
        # positions back into CONFIG coordinates for the map/flow overlays.
        "_scene_center": [round(cx, 1), round(cy, 1)],
    }


def _nsoran_snapshot(run_url: str) -> dict | None:
    """What the engine has of the current run, or None.

    Only worth returning if it carries the DU metrics a caller reads — a
    snapshot from before the RAN scheduled anything is not a recovery, it is an
    empty result wearing one. A snapshot still `in_flight` is returned anyway:
    the run is going on without us, and partial real numbers beat a 502.
    """
    try:
        r = requests.get(run_url.rsplit("/run", 1)[0] + "/run/snapshot", timeout=20)
        if r.status_code != 200:
            return None
        snap = r.json()
    except (requests.RequestException, ValueError):
        return None
    if "DRB.UEThpDl" not in (snap.get("measurements") or {}):
        return None
    return snap


def _save_batch_run(run_id: str, config: NSGSimulationConfig, payload: dict,
                    kpm: dict, started_at: str, owner_id: str | None,
                    finished: bool) -> None:
    """Write a batch run into the same `runs` table the live path uses.

    A batch run used to exist ONLY as its HTTP response. Nothing wrote it down,
    so closing the tab lost it — which was a shrug at ninety seconds and is not
    at four hours. It goes in `summary` whole: a batch result is an AGGREGATE
    (one measurements dict over the run), not a frame series, so there is nothing
    to put in `frames` and `n_frames` stays 0. `kind` is what lets the listing
    tell the two apart without loading every run.
    """
    try:
        store.save_run({
            "id": run_id,
            "kind": "batch",
            "owner_id": owner_id,
            "started_at": started_at,
            "finished_at": _now_iso() if finished else None,
            "config": config.model_dump(mode="json"),
            "requested": {k: payload.get(k)
                          for k in ("n_gnb", "n_ue", "freq_hz", "bw_hz",
                                    "kpm_collect_s", "sim_time")},
            "status": kpm.get("status") or ("running" if not finished else "unknown"),
            "error": kpm.get("error"),
            "n_frames": 0,
            "summary": kpm,
            "status_lines": [],
            "frames": [],
        })
    except Exception:  # noqa: BLE001 — persistence must never fail the run
        logger.exception("could not persist batch run %s", run_id)

    # ALSO in the in-memory index, because /sim/runs lists that rather than the
    # database. Writing only to Postgres made a batch run invisible until the
    # next restart hydrated it — present, listed, and only after a reboot, which
    # is worse than either one consistently.
    rec = _SIM_RUNS.setdefault(run_id, {
        "id": run_id, "kind": "batch", "started_at": started_at,
        "config_name": config.name, "owner_id": owner_id,
        "frames_target": 0, "n_frames": 0,
    })
    rec.update(status=kpm.get("status") or ("running" if not finished else "unknown"),
               error=kpm.get("error"),
               finished_at=_now_iso() if finished else None)


def _batch_run_poller(run_id: str, config: NSGSimulationConfig, payload: dict,
                      url: str, started_at: str, owner_id: str | None,
                      stop: threading.Event) -> None:
    """Persist the engine's snapshot of THIS run while it is still going.

    The engine publishes itself to /run/snapshot; this is what makes that
    durable past the container. Without it a four-hour run is only as safe as
    one process and one /tmp.

    Same backoff as the engine's own snapshot cadence, and for the same reason —
    there is nothing to gain from asking more often than it writes.
    """
    interval = 30.0
    while not stop.wait(interval):
        snap = _nsoran_snapshot(url)
        # ONLY OUR RUN. The engine serves one at a time, but several callers can
        # be waiting on it, and a snapshot that belongs to someone else's run
        # would otherwise be filed under this id.
        if snap and snap.get("run_id") == run_id:
            _save_batch_run(run_id, config, payload, snap, started_at, owner_id,
                            finished=False)
        interval = min(300.0, interval * 1.5)


@app.post("/ric/validate-nsoran")
def ric_validate_nsoran(config: NSGSimulationConfig, sim_time: int = 20,
                        partition_groups: int = 1,
                        principal: Principal = Depends(current_principal)) -> dict:
    """
    Run the REAL ns-O-RAN engine — FlexRIC (real near-RT RIC) + ns-3.42 mmWave RAN
    over real E2AP/SCTP, running as a container worker — DRIVEN BY THIS SCENARIO,
    and return its MEASURED 5G KPM (throughput / PRB / SINR). Highest-fidelity
    tier: real 5G stack, not a model.

    Config-driven (Level 1): cell count, UE count, centre frequency, bandwidth and
    spatial scale come from `config`. The engine's fixed 4000x4000 hub-and-ring
    layout can't reproduce exact per-gNB positions / per-slice traffic (that needs
    a C++ rebuild), but the RIC now sees the RIGHT number of cells + UEs at the
    right band and scale — so it validates the shape of the user's design.

    Reached at NSG_NSORAN_URL (default http://localhost:8090; mirrored WSL
    networking). Regular def → threadpool; the run takes ~1 min.
    """
    payload = {"sim_time": sim_time, **nsoran_params(config)}
    url = os.environ.get("NSG_NSORAN_URL", "http://localhost:8090").rstrip("/") + "/run"

    # PARTITIONED: the same scenario as N processes of whole cells, one engine
    # instance each, merged back into one result. Splitting is the only speed
    # lever with real headroom (measured 2.1x end to end at four groups, and the
    # partitions see three simulated seconds for every one the whole run
    # manages), but it is not free — `partition_advice` computes the trade and
    # the panel shows it, so by the time a number arrives here the choice has
    # been made knowingly.
    #
    # The pool is LEASED for the run and dropped afterwards. Leaving instances
    # standing would idle containers, and sizing it per run is what lets a route
    # accept this at all: until now the pool was a shell script run by hand.
    if partition_groups and partition_groups > 1:
        n_gnb = int(payload.get("n_gnb") or 1)
        if n_gnb % partition_groups:
            raise HTTPException(
                status_code=422,
                detail=f"{partition_groups} groups does not divide {n_gnb} cells evenly — "
                       f"a cell is the smallest unit a process can hold")
        run_id = uuid.uuid4().hex[:12]
        payload["run_id"] = run_id
        started_at = _now_iso()
        _close_ai_turn(config, principal, run_id)
        owner_id = _owner_id(principal) if principal else None
        try:
            with partition_pool.leased(partition_groups) as ports:
                merged = partition_run.run_partitioned(payload, partition_groups, ports)
        except ValueError as exc:            # refused, not broken: a bad shape or too few cores
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("partitioned run failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        merged["run_id"] = run_id
        _save_batch_run(run_id, config, payload, merged, started_at, owner_id,
                        finished=True)
        # SAME SHAPE AS THE WHOLE RUN. The un-partitioned path answers
        # {"status", "run_id", "kpm"} and everything downstream reads
        # `result.kpm`; returning the merged result flat made one route hand back
        # two shapes depending on an argument, and a caller that did not know
        # about partitioning would read nothing at all — silently, because the
        # keys are absent rather than wrong.
        return {"status": "ok", "run_id": run_id, "kpm": merged}

    # THE RUN IS WRITTEN DOWN, not just answered. It gets an id before it starts,
    # a poller persists the engine's snapshot of it while it goes, and the final
    # result is stored whichever way it ends — so a closed tab, a restarted
    # backend or a lost connection costs the reference to the run, not the run.
    run_id = uuid.uuid4().hex[:12]
    payload["run_id"] = run_id
    started_at = _now_iso()
    _close_ai_turn(config, principal, run_id)
    owner_id = _owner_id(principal) if principal else None
    stop = threading.Event()
    threading.Thread(target=_batch_run_poller,
                     args=(run_id, config, payload, url, started_at, owner_id, stop),
                     daemon=True).start()
    try:
        # MIRROR THE ENGINE'S OWN BUDGET, do not guess at it. engine_service's
        # `_script_budget` is 40 + 15/gNB + the KPM collection window + 100, and
        # run_scenario retries once when ns-3 hits its startup race — so giving
        # up earlier than twice that abandons a run the engine is still working
        # on, and now also throws away the partial result it would have salvaged
        # on its own timeout. The old constant 560 happened to cover two attempts
        # at 7 gNB and nothing else; `kpm_collect_s` can make one attempt alone
        # longer than that.
        n_gnb = int(payload.get("n_gnb") or 4)
        n_ue = int(payload.get("n_ue") or 4)
        collect = payload.get("kpm_collect_s")
        collect = (max(10, min(14400, int(collect))) if collect
                   else max(10, min(180, n_ue * 8 // 5)))
        budget = 2 * (40 + n_gnb * 15 + collect + 100) + 60
        resp = requests.post(url, json=payload, timeout=budget)
        resp.raise_for_status()
        kpm = resp.json()
        _save_batch_run(run_id, config, payload, kpm, started_at, owner_id,
                        finished=True)
        return {"status": "ok", "run_id": run_id, "kpm": kpm}
    except requests.RequestException as exc:
        # LOSING THE CONNECTION IS NOT LOSING THE RUN. The engine publishes the
        # run to disk as it goes and serves it at /run/snapshot, so before
        # reporting a failure, ask what it actually has. This is the difference
        # between a four-hour run whose POST timed out costing four hours and
        # costing nothing — and it also covers the backend being restarted
        # mid-run, which no timeout of ours can help with.
        snap = _nsoran_snapshot(url)
        if snap and snap.get("run_id") == run_id:
            snap["recovered_from_snapshot"] = True
            snap["recovery_reason"] = str(exc)[:200]
            _save_batch_run(run_id, config, payload, snap, started_at, owner_id,
                            finished=not snap.get("in_flight"))
            return {"status": "ok", "run_id": run_id, "kpm": snap}
        raise HTTPException(status_code=502, detail=f"ns-O-RAN engine unreachable/failed: {exc}")
    finally:
        stop.set()


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — BATCH SIMULATION RUNS (config → run to completion → full dataset)
# The pivot from RIC-test-only to a full RAN scenario simulator: a run executes
# the scenario headless on the real ns-O-RAN engine, captures the WHOLE per-tick
# per-cell/UE/slice time-series (not just a summary), and stores it as a dataset.
# Runs take minutes, so /sim/run is a background job you poll.
# ═══════════════════════════════════════════════════════════════════════════════

_SIM_RUNS: dict[str, dict] = {}
# Runs and campaigns now persist to Postgres (see store.py). The old
# sim_runs/ and campaigns/ directories are left on disk untouched: they hold
# development-era results, and deleting evidence to tidy a refactor is not a
# trade worth making. Nothing reads them any more.


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


async def _run_fast_tier(config: NSGSimulationConfig, params: dict,
                         frames: int) -> dict:
    """The fast tier as a batch run, shaped like the engine's.

    `tier_b.frames` already emits the shape the live bridge emits, so the
    same `summarise_batch_frames` reads it and a campaign scores the two
    tiers on one axis. What it CANNOT share is the horizon: ns-3 gives
    whatever its wall budget buys, this gives exactly `frames` windows of
    `ind_period_s` each, so the run reports its own span rather than
    inheriting a cap that does not apply to it.
    """
    import tier_b
    from nsoran_live import _rate_of_imsi_map
    demand = {i: m * 1e6 for i, m in _rate_of_imsi_map(config).items()}
    t0 = time.monotonic()
    got = await asyncio.to_thread(tier_b.frames, params, frames,
                                  demand_bps=demand)
    # The keys `_sim_job` reads, all of them. A partial dict here fails with
    # a KeyError deep in the persist step, after the run has already been
    # written as "running" — which is a campaign stuck forever rather than a
    # campaign that failed.
    span = round(got[-1]["sim_t"], 2) if got else 0.0
    # WHICH FAULTS ACTUALLY FIRED. The horizon audit compares
    # `anomalies_configured` against `anomalies_fired`, and a tier that
    # never reports the second is a tier where every run with an anomaly
    # warns that none of them happened — which, until this tier honoured
    # the schedule at all, was true. It is not any more.
    import tier_b as _tb
    fired, seen = [], set()
    for f in got:
        for ev in _tb.faults_firing(params, f["sim_t"], _tb.window_s(params)):
            if ev not in seen:
                seen.add(ev)
                fired.append(f"[NSG-FAULT] {ev} at sim_t={f['sim_t']:g}")
    return {
        "status": "ok", "error": None, "frames": got, "n_frames": len(got),
        "status_lines": [
            f"fast tier: {len(got)} windows, {span:g} simulated seconds "
            f"in {time.monotonic() - t0:.2f} s wall",
            "no ns-3 process, no engine lock, no wall budget — the horizon "
            "is exactly the frames requested",
        ],
        "fault_events": fired or None, "partitioned": False,
        "wall_s": time.monotonic() - t0, "tier": "fast",
    }


async def _sim_job(run_id: str, config: NSGSimulationConfig, params: dict, frames: int,
                   live_addr: str | None = None, wall_cap_s: float = 900.0,
                   partition_groups: int = 1, tier: str = "slow") -> None:
    """Background worker: run the batch, summarise, persist the dataset.

    `partition_groups` > 1 runs the scenario as that many ns-3 processes of whole
    cells and merges the streams. This is the STREAMING path — the one a
    campaign and the RIC real engine drive — so it leases live bridge addresses
    rather than batch ports, and the merged frames go through the same
    `summarise_batch_frames` as a whole run's.
    """
    try:
        # Progress is published into the run record as it happens, so GET
        # /sim/run/{id} answers "how far along is it" while the run is still going
        # — a campaign otherwise shows a spinner for minutes with no way to tell
        # a working run from a stuck one.
        def _progress(info: dict, _rid: str = run_id) -> None:
            rec = _SIM_RUNS.get(_rid)
            if rec is not None:
                rec["progress"] = info
                rec["n_frames"] = info.get("n_frames", rec.get("n_frames", 0))

        if tier == "fast":
            # No engine, no lock, no bridge — so no partitioning either:
            # partitioning exists to buy ns-3 a longer horizon per process
            # and this tier has no horizon problem.
            res = await _run_fast_tier(config, params, frames)
        elif partition_groups and partition_groups > 1:
            n_gnb = int(params.get("n_gnb") or 1)
            if n_gnb % partition_groups:
                raise ValueError(
                    f"{partition_groups} groups does not divide {n_gnb} cells evenly "
                    f"— a cell is the smallest unit a process can hold")
            async with partition_pool.aleased(partition_groups) as addrs:
                res = await partition_live.run_partitioned_live(
                    config, params, partition_groups, addrs,
                    frames_target=frames, wall_cap_s=wall_cap_s,
                    on_progress=_progress)
        else:
            res = await run_batch_simulation(config, params, frames_target=frames,
                                             wall_cap_s=wall_cap_s,
                                             on_progress=_progress, live_addr=live_addr)
        # Pass the staged-fault audit in: `anomalies_configured` vs
        # `anomalies_fired` is what exposes a fault scheduled beyond the run's
        # simulated horizon, which otherwise returns healthy metrics and passes.
        summary = summarise_batch_frames(
            res["frames"], res.get("fault_events"),
            len(getattr(config, "anomalies", None) or []))
        dataset = {
            "id": run_id,
            "started_at": _SIM_RUNS[run_id]["started_at"],
            "finished_at": _now_iso(),
            "config": config.model_dump(mode="json"),
            "requested": _SIM_RUNS[run_id]["requested"],
            "status": res["status"],
            "error": res.get("error"),
            "n_frames": res["n_frames"],
            # What the run was ALLOWED, stored beside what it got. "wall-capped"
            # with no cap recorded leaves the reader guessing whether a short
            # result was the scenario or the budget.
            "wall_cap_s": wall_cap_s,
            "summary": summary,
            "status_lines": res["status_lines"],
            "frames": res["frames"],
            # save_run MERGES, so a key left out here is written as NULL over the
            # start record. Without this a normal user got 404 on their own
            # finished run; an admin, who sees unowned rows, never noticed.
            "owner_id": _SIM_RUNS[run_id].get("owner_id"),
        }
        # The shape travels WITH the run. A partitioned result read next month —
        # in Compare, in a report, in the campaign history — has to say what it
        # cost, because the picker that warned about it is long gone.
        if res.get("partitioned"):
            dataset["summary"] = {**(summary or {}),
                                  "partitioned": True,
                                  "partitions": res.get("partitions"),
                                  "partition_shape": res.get("partition_shape"),
                                  "partition_notes": res.get("partition_notes"),
                                  "per_partition": res.get("per_partition")}
        store.save_run(dataset)
        _SIM_RUNS[run_id].update(status=res["status"], error=res.get("error"),
                                 n_frames=res["n_frames"],
                                 summary=dataset["summary"],
                                 finished_at=dataset["finished_at"])
    except Exception as exc:  # noqa: BLE001
        logger.exception("batch sim job %s failed", run_id)
        _SIM_RUNS[run_id].update(status="error", error=str(exc), finished_at=_now_iso())


@app.post("/sim/run")
async def sim_run(config: NSGSimulationConfig, frames: int = 80,
                  wall_cap_s: float = 900.0, partition_groups: int = 1,
                  principal: Principal = Depends(require_user)) -> dict:
    """Start a batch simulation run of the given scenario on the real engine.
    Returns immediately with a run id; poll GET /sim/run/{id} for status + data.

    `frames` is a CEILING on captured KPM windows, not a horizon. ns-3 mmWave
    runs at a few percent of real time (measured ~3.3% for 3 gNB / 6 UE, less
    for heavier scenes), so a run usually ends at the wall cap with status
    "wall-capped" rather than at `frames`.

    `wall_cap_s` IS the horizon, and it is what a long run moves. THIS is the
    path for one: it streams KPM out as it arrives and stores frames as they
    land, where a batch `/ric/validate-nsoran` run holds a single request open
    and is capped at four hours for that reason. The engine's own session cap
    now follows this value instead of sitting at a fixed 1200 s underneath it,
    which is what used to turn a longer request into a misreported
    "engine-closed" at twenty minutes.

    Two days is the ceiling here, and it is a sanity bound rather than a measured
    one — the longest session ever verified alive is 300 s, so treat anything
    approaching it as an experiment, not a promise."""
    frames = max(4, min(600, int(frames)))
    wall_cap_s = max(30.0, min(172800.0, float(wall_cap_s)))
    run_id = uuid.uuid4().hex[:12]
    params = nsoran_params(config)
    _SIM_RUNS[run_id] = {
        "id": run_id,
        "status": "running",
        "started_at": _now_iso(),
        "finished_at": None,
        "config_name": config.name,
        "requested": {k: params.get(k) for k in ("n_gnb", "n_ue", "freq_hz", "bw_hz")},
        "kind": "live",
        "frames_target": frames,
        "wall_cap_s": wall_cap_s,
        "n_frames": 0,
        "owner_id": _owner_id(principal),
    }
    # The scenario just became something someone RAN, which is the moment their
    # edit stops being a draft and becomes the label on the prompt behind it.
    _close_ai_turn(config, principal, run_id)
    asyncio.create_task(_sim_job(run_id, config, params, frames,
                                 wall_cap_s=wall_cap_s,
                                 partition_groups=partition_groups))
    return {"run_id": run_id, "status": "running", "frames_target": frames,
            "wall_cap_s": wall_cap_s, "partition_groups": partition_groups}


@app.get("/sim/runs")
def sim_runs(all: bool = False,
             principal: Principal = Depends(require_user)) -> dict:
    """List runs (metadata only, newest first) — no frames, so it stays small."""
    names = store.usernames_by_id()
    runs = [{**{k: v for k, v in r.items() if k != "summary"},
             "owner": names.get(r.get("owner_id") or "")}
            for r in _SIM_RUNS.values() if _may_see(principal, r.get("owner_id"), all)]
    runs.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return {"runs": runs}


@app.get("/sim/run/{run_id}")
def sim_run_get(run_id: str,
                principal: Principal = Depends(require_user)) -> dict:
    """Full dataset for a finished run (frames + summary), or the live record
    while it's still running. 404 rather than 403 when it belongs to someone
    else, so the endpoint does not confirm a run id exists for other users."""
    mem = _SIM_RUNS.get(run_id)
    if mem is not None and not _may_see(principal, mem.get("owner_id")):
        raise HTTPException(status_code=404, detail="run not found")
    stored = store.load_run(run_id)
    if stored is not None:
        if not _may_see(principal, stored.get("owner_id")):
            raise HTTPException(status_code=404, detail="run not found")
        return stored
    if mem is not None:
        return mem
    raise HTTPException(status_code=404, detail="run not found")


def _hydrate_runs_from_disk() -> None:
    """Load the run index at startup.

    The datasets were always persisted; the INDEX was not, so every backend
    restart emptied /sim/runs while the evidence sat on disk. Harmless for a
    single interactive session, fatal for a campaign — comparing this week's
    result to last week's requires last week's run to still be listed.

    A running run keeps its in-memory record: it holds live progress the stored
    row does not have yet, and overwriting it would make an active run look
    finished.
    """
    for run_id, meta in store.load_run_index().items():
        _SIM_RUNS.setdefault(run_id, meta)
    logger.info("loaded %d stored simulation run(s)", len(_SIM_RUNS))


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — TEST CAMPAIGNS (suite of runs -> thresholds -> verdict)
# One run says what happened; a campaign says whether it is still good enough.
# Runs execute SEQUENTIALLY: the engine serves one session at a time, so a
# parallel campaign would collide as [BUSY] rather than finish sooner.
# ═══════════════════════════════════════════════════════════════════════════════

_CAMPAIGNS: dict[str, dict] = {}

# How long to wait out the engine's own lock between runs. It releases only as
# the previous process exits, which is seconds after the run "finishes".
_BUSY_RETRIES = 6
_BUSY_WAIT_S = 15

# The recovery pause between consecutive runs now lives in engine_pool, because
# it is a property of ONE engine rather than of the campaign: sustained
# back-to-back sessions measurably slow an instance down (median wall 23s -> 29s
# over forty runs, recovering on its own when left alone). With a pool the next
# run goes to a rested instance instead of everyone waiting. See
# engine_pool.COOLDOWN_S / NSG_ENGINE_COOLDOWN_S.


def _inconclusive_reason(status: str, summary: dict | None) -> str | None:
    """Why this run cannot be scored — or None if it can.

    A threshold compares a MEASUREMENT. When the run produced none, comparing
    against 0 manufactures a verdict out of nothing: the campaign that prompted
    this reported "Downlink (mean) >= 10, measured 0" for a run that had timed
    out after 6 windows with no UE attached. The scenario was not tested.
    """
    if status not in ("done", "wall-capped"):
        return (f"the run ended as '{status}', so its numbers describe a broken "
                f"run rather than the scenario")
    s = summary or {}
    if not (s.get("ticks") or 0):
        return "the engine reported no KPM window at all"
    if not (s.get("ue_count_mean") or 0):
        return ("no UE ever reported — the RAN never scheduled anything, so "
                "throughput and PRB are absent rather than low. Below 3 cells "
                "this engine does not decode DU metrics at all")
    return None


def _bench_failure(run_state: dict) -> bool:
    """Did the BENCH fail, rather than the scenario?

    A run that produced ZERO frames never simulated anything, so it says nothing
    about the configuration under test. The engine does still crash: on
    2026-09-03 ns-3 segfaulted in e2sim's subscription handling, right after
    decoding the RIC subscription, and the run came back with 0 frames.

    That was ONE event, not a rate. It failed to reproduce in 32 further runs
    across three setups — isolated sessions, campaign-condition concurrency, and
    cold containers — so it is a rare race rather than the "one in four" the
    single observation first suggested. The retry stays: it is cheap, and a rare
    crash still ruins a long campaign that would otherwise have to be rerun.

    Retried ONCE, and the retry is logged: a configuration that genuinely crashes
    the engine every time still ends as an error, it just costs one extra run to
    establish that. Silently rerunning until something passes would be scoring
    the bench, not the scenario.
    """
    if run_state.get("status") not in ("timeout", "error"):
        return False
    return not (run_state.get("n_frames") or 0)


def _engine_busy(run_state: dict) -> bool:
    """Did this run come back because the engine was still holding its lock?

    Distinguished from a real failure on purpose: a busy engine says nothing
    about the scenario, so retrying is correct and scoring it as a FAIL is not.
    """
    if run_state.get("status") == "busy":
        return True
    return "busy" in (run_state.get("error") or "").lower()


def _campaign_save(record: dict) -> None:
    store.save_campaign(record)


def _hydrate_campaigns_from_disk() -> None:
    # A campaign that was mid-flight when the process died is not running any
    # more, and neither is the run it was in the middle of; leaving either
    # "running" makes the UI poll a job that no longer exists. Two UPDATEs do
    # this before anything is read, so the records load already correct.
    store.mark_interrupted()

    for record in store.load_campaigns().values():
        # Correct verdicts that the current rules say should never have been
        # issued: a run that measured nothing cannot pass or fail a threshold.
        # This is not rewriting a judgement — it is withdrawing one that was made
        # about a run which was never scoreable (a timed-out run with 0 UEs was
        # filed as "the scenario missed its throughput target").
        for run in record.get("runs", []):
            if run.get("verdict") in ("pass", "fail"):
                why = _inconclusive_reason(run.get("status", ""), run.get("summary"))
                if why:
                    run["verdict"] = "error"
                    run["violations"] = []
                    run["error"] = run.get("error") or why
        if record.get("runs"):
            record["verdict"] = campaign_verdict(
                [r.get("verdict") or "error" for r in record["runs"]])
        _CAMPAIGNS[record["id"]] = record
    logger.info("loaded %d stored campaign(s)", len(_CAMPAIGNS))


def _campaign_brief(record: dict) -> dict:
    """List view — drops per-run summaries so the listing stays small."""
    return {k: v for k, v in record.items() if k != "runs"} | {
        "run_count": len(record.get("runs", [])),
        "failed": sum(1 for r in record.get("runs", []) if r.get("verdict") == "fail"),
        # From the RESULTS, not the request: the campaigns table has no column
        # for the requested shape, so it came back None after a restart and the
        # History row forgot what it had shown an hour earlier.
        "partition_groups": partition_groups_of(record),
    }


async def _campaign_run_one(campaign_id: str, record: dict, spec: CampaignSpec,
                            planned, slot: int, live_addr: str) -> None:
    """Execute and score ONE planned run on ONE engine instance."""
    if True:
        entry = {
            "label": planned.label, "item_label": planned.item_label,
            "repeat": planned.repeat, "seed": planned.seed,
            "run_id": None, "status": "running", "verdict": None,
            "violations": [], "summary": None, "error": None,
            "started_at": _now_iso(), "finished_at": None,
            # The exact scenario this run executed, seed included. A result whose
            # scenario cannot be identified is an anecdote — and the report had
            # no way to describe what was under test.
            "config": planned.config.model_dump(mode="json"),
        }
        # The slot was pre-created in plan order; publish this run's "started"
        # state into it rather than appending a second copy.
        record["runs"][slot] = entry
        _campaign_save(record)

        run_id = uuid.uuid4().hex[:12]
        entry["run_id"] = run_id
        params = nsoran_params(planned.config)
        _SIM_RUNS[run_id] = {
            "id": run_id, "status": "running", "started_at": _now_iso(),
            "finished_at": None, "config_name": planned.config.name,
            "requested": {k: params.get(k) for k in ("n_gnb", "n_ue", "freq_hz", "bw_hz")},
            "frames_target": planned.frames, "n_frames": 0,
            "campaign_id": campaign_id,
        }
        # Persist the run row NOW, not when it finishes. campaign_runs.run_id is
        # a foreign key into runs, and with concurrent runs a campaign save
        # triggered by run A carries run B's freshly-assigned id — which did not
        # exist yet, so the whole campaign job died on a ForeignKeyViolation and
        # left B stuck "running" forever. Sequentially this never showed: only
        # one id was ever in flight, and it was already written by save time.
        store.save_run({**_SIM_RUNS[run_id],
                        "config": planned.config.model_dump(mode="json"),
                        "frames": []})
        # Awaited, not spawned: the engine runs one session at a time — and it
        # holds its lock until the previous process actually exits, so a
        # back-to-back campaign races itself. MEASURED: run 2 of a 2-repeat
        # campaign came back "busy" with zero ticks, which scored as a FAIL of
        # the scenario. Without this retry every campaign with more than one run
        # fails from its second run onward — i.e. every campaign worth running.
        # Same wait-it-out the fidelity harness already does (run_real).
        groups = int(getattr(spec, "partition_groups", 1) or 1)
        tier = str(getattr(spec, "tier", "slow") or "slow")
        # The wall budget is the REAL horizon on this path and it was never
        # passed, so every campaign run silently took the 900 s default while
        # the card promised whatever its frame count implied. A six-cell
        # scenario asked for 50 frames collected 39 and covered 1.1 simulated
        # seconds; the run itself said "timeout".
        cap = float(getattr(spec, "wall_cap_s", 900.0) or 900.0)
        # ONE BUDGET FOR THE WHOLE ENTRY, retries included. Each attempt used to
        # start the budget again, which was survivable at the old fixed 900 s
        # and is not once the budget is a choice: a run given an hour spent
        # THREE of them retrying, and the campaign sat "running" the whole time
        # with nothing to show. A retry is for an engine that was busy, which
        # costs seconds — it is not a reason to buy another full horizon.
        deadline = time.monotonic() + cap + _BUSY_RETRIES * _BUSY_WAIT_S
        for attempt in range(_BUSY_RETRIES):
            left = deadline - time.monotonic()
            if left < 30:
                logger.warning("campaign %s: %s out of budget after %d attempt(s)",
                               campaign_id, planned.label, attempt)
                break
            await _sim_job(run_id, planned.config, params, planned.frames,
                           live_addr=live_addr, partition_groups=groups,
                           wall_cap_s=min(cap, left), tier=tier)
            if tier == "fast" or not _engine_busy(_SIM_RUNS[run_id]):
                break
            logger.info("campaign %s: engine busy on %s, retry %d/%d",
                        campaign_id, planned.label, attempt + 1, _BUSY_RETRIES)
            await asyncio.sleep(_BUSY_WAIT_S)

        # One more attempt when the BENCH failed rather than the scenario. Kept
        # separate from the busy loop above and deliberately not a loop: this
        # retries a crash, and a configuration that crashes every time must still
        # be reported as an error rather than retried until the campaign gives up.
        if _bench_failure(_SIM_RUNS[run_id]):
            logger.warning("campaign %s: %s produced no frames (%s) — retrying "
                           "once; this is a bench failure, not a scenario result",
                           campaign_id, planned.label,
                           _SIM_RUNS[run_id].get("status"))
            entry["bench_retry"] = True
            await asyncio.sleep(_BUSY_WAIT_S)
            left = deadline - time.monotonic()
            if left >= 30:
                await _sim_job(run_id, planned.config, params, planned.frames,
                               live_addr=live_addr, partition_groups=groups,
                               wall_cap_s=min(cap, left))
            else:
                logger.warning("campaign %s: %s had no budget left to retry the "
                               "bench failure", campaign_id, planned.label)

        run_state = _SIM_RUNS[run_id]
        entry["status"] = run_state.get("status", "unknown")
        entry["summary"] = run_state.get("summary")
        entry["error"] = run_state.get("error")
        entry["finished_at"] = _now_iso()

        # Only a run that actually MEASURED something may be scored. Everything
        # else is a bench failure, and calling it a scenario FAIL is a lie the
        # report then repeats: a timed-out run came back with 6 ticks, 0 UEs and
        # dl_mbps_mean 0.0, and the campaign filed it as "the scenario missed its
        # throughput threshold". The scenario was never given the chance.
        inconclusive = _inconclusive_reason(entry["status"], entry["summary"])
        if entry["status"] == "error" or entry["summary"] is None or _engine_busy(run_state):
            entry["verdict"] = "error"
        elif inconclusive:
            entry["verdict"] = "error"
            entry["error"] = entry["error"] or inconclusive
        else:
            verdict, violations = evaluate(entry["summary"], spec.thresholds)
            entry["verdict"] = verdict
            entry["violations"] = [v.model_dump() for v in violations]
            # A scenario that staged faults none of which fired measured the
            # HEALTHY network — so a PASS here is about a test that did not
            # happen. Not downgraded to a failure (the thresholds were honestly
            # met) but never left silent, which is how it slipped through before.
            configured = entry["summary"].get("anomalies_configured") or 0
            fired = entry["summary"].get("anomalies_fired") or 0
            if configured and not fired:
                entry["warning"] = (
                    f"{configured} anomaly/anomalies were configured and NONE "
                    f"fired — start_s is in simulated seconds and this run only "
                    f"reached a fraction of one. The result describes a healthy "
                    f"network, not a faulted one."
                )
            elif configured:
                # Some fired, so the check above stays quiet — but a scenario can
                # stage one fault at 0.05 s and another at 5 s, and only the first
                # is ever reached. Partial coverage was the silent case.
                for hv in horizon_violations(planned.config.anomalies, entry["summary"]):
                    entry["warning"] = (
                        f"{fired} of {configured} anomalies fired: {hv.expected}. "
                        f"The later faults were never injected, so this run does "
                        f"not describe the scenario as written."
                    )

        record["verdict"] = campaign_verdict([r["verdict"] for r in record["runs"]])
        _campaign_save(record)
        # The entry was pre-created in plan order so the UI can show the whole
        # suite while it fills in; write this run's result into its own slot
        # rather than appending, which concurrency would scramble.
        record["runs"][slot] = entry
        record["verdict"] = campaign_verdict(
            [r.get("verdict") or "running" for r in record["runs"]])
        _campaign_save(record)


async def _campaign_job(campaign_id: str, spec: CampaignSpec) -> None:
    """Execute the campaign's runs, scoring each as it lands.

    Runs go to a POOL of engine instances rather than one engine in sequence.
    A run is single-core by construction (ns-3 has one ordered event queue), so
    the way to finish a suite sooner is more engines, not a faster engine:
    measured on this path, six concurrent runs returned 5.8x the throughput for
    4% more wall time each, all six clean.

    With a one-instance pool this behaves exactly as the sequential version did,
    which is the default until an operator sets NSG_ENGINE_POOL.
    """
    record = _CAMPAIGNS[campaign_id]
    plan = list(expand(spec))

    # Pre-create every entry in plan order. Concurrent runs finish out of order,
    # and appending as they land would leave the report in completion order with
    # no way to line a result up against the plan.
    record["runs"] = [{
        "label": pl.label, "item_label": pl.item_label, "repeat": pl.repeat,
        "seed": pl.seed, "run_id": None, "status": "queued", "verdict": None,
        "violations": [], "summary": None, "error": None,
        "started_at": None, "finished_at": None,
        "config": pl.config.model_dump(mode="json"),
    } for pl in plan]
    _campaign_save(record)

    engines = pool()
    logger.info("campaign %s: %d run(s) over %d engine instance(s), %d partition(s) each",
                campaign_id, len(plan), engines.size,
                int(getattr(spec, "partition_groups", 1) or 1))

    # A PARTITIONED campaign runs its scenarios one at a time, and that is the
    # design rather than a limitation. Partitioning already uses every core for
    # ONE scenario, so also running scenarios concurrently would oversubscribe
    # the machine — past the physical core count the wall clock keeps improving
    # while UEs stop being served and every run still reports ok. The suite
    # therefore takes about as long either way; what changes is that each run
    # covers several times more simulated time, which is what a campaign is
    # short of. The instances come from partition_pool, so the campaign pool is
    # not touched at all in this mode.
    groups = int(getattr(spec, "partition_groups", 1) or 1)

    async def one(slot: int, planned) -> None:
        # Stop between runs, not inside one: a run already on an engine is left
        # to finish, because killing it mid-flight leaves the container holding a
        # half-torn-down session and the next campaign meets [BUSY].
        if record.get("stop_requested"):
            record["runs"][slot]["status"] = "stopped"
            return
        if groups > 1:
            await _campaign_run_one(campaign_id, record, spec, planned, slot, None)
            return
        async with engines.acquire() as addr:
            if record.get("stop_requested"):
                record["runs"][slot]["status"] = "stopped"
                return
            await _campaign_run_one(campaign_id, record, spec, planned, slot, addr)

    # return_exceptions: one run blowing up must not abandon the rest of the
    # suite with no verdict and no explanation. Sequentially an exception ended
    # the campaign, which was survivable because nothing else was in flight;
    # with a pool it would strand every concurrent sibling.
    results = await asyncio.gather(*(one(i, pl) for i, pl in enumerate(plan)),
                                   return_exceptions=True)
    for slot, res in enumerate(results):
        if isinstance(res, BaseException):
            logger.exception("campaign %s: run %d failed", campaign_id, slot,
                             exc_info=res)
            row = record["runs"][slot]
            row["status"] = row.get("status") or "error"
            row["verdict"] = "error"
            row["error"] = row.get("error") or f"{type(res).__name__}: {res}"

    record["status"] = "stopped" if record.get("stop_requested") else "done"
    record["finished_at"] = _now_iso()
    record["verdict"] = campaign_verdict(
        [r.get("verdict") or "error" for r in record["runs"]
         if r.get("status") != "stopped"] or ["error"])
    _campaign_save(record)
def _campaign_or_404(campaign_id: str, principal: "Principal") -> dict:
    """Fetch a campaign the caller is allowed to see, else 404. One place, so
    every by-id route (get, delete, stop, compare, datasets) enforces ownership
    the same way and none can forget."""
    rec = _CAMPAIGNS.get(campaign_id)
    if rec is None or not _may_see(principal, rec.get("owner_id")):
        raise HTTPException(status_code=404, detail="campaign not found")
    return rec


@app.post("/campaigns")
async def campaign_create(spec: CampaignSpec,
                          principal: Principal = Depends(require_user)) -> dict:
    """Start a campaign: every item run at every seed, each scored against the
    thresholds. Returns immediately; poll GET /campaigns/{id}.

    Runs execute across the engine pool (engine_pool.py). One run is single-core
    by construction, so a suite finishes sooner by using more engine instances,
    not a faster engine: measured 5.8x throughput at six concurrent runs for 4%
    more wall time each. With the default one-instance pool this is sequential,
    exactly as before."""
    campaign_id = uuid.uuid4().hex[:12]
    planned = expand(spec)
    record = {
        "id": campaign_id,
        "name": spec.name,
        "description": spec.description,
        "status": "running",
        "verdict": None,
        "created_at": _now_iso(),
        "finished_at": None,
        "base_seed": spec.base_seed,
        # THE SHAPE IS PART OF WHAT THIS CAMPAIGN IS. A suite run as three
        # processes per scenario and one run whole are different instruments —
        # different handover retention, different horizon — and History lists
        # them side by side. Recorded like base_seed, for the same reason: a
        # result whose conditions cannot be read back is an anecdote.
        "partition_groups": int(getattr(spec, "partition_groups", 1) or 1),
        "wall_cap_s": float(getattr(spec, "wall_cap_s", 900.0) or 900.0),
        "thresholds": [t.model_dump() for t in spec.thresholds],
        "planned": [{"label": p.label, "seed": p.seed, "frames": p.frames}
                    for p in planned],
        "stop_requested": False,
        "owner_id": _owner_id(principal),
        "runs": [],
    }
    _CAMPAIGNS[campaign_id] = record
    _campaign_save(record)
    asyncio.create_task(_campaign_job(campaign_id, spec))
    return {"campaign_id": campaign_id, "status": "running",
            "planned_runs": len(planned)}


@app.get("/campaigns")
def campaign_list(all: bool = False,
                  principal: Principal = Depends(require_user)) -> dict:
    """Campaigns, newest first, without per-run detail.

    `all=true` is an administrator asking to see other people's campaigns too;
    without it the list is your own plus the un-owned rows that predate auth.
    """
    names = store.usernames_by_id()
    items = [{**_campaign_brief(c), "owner": names.get(c.get("owner_id") or "")}
             for c in _CAMPAIGNS.values()
             if _may_see(principal, c.get("owner_id"), all)]
    items.sort(key=lambda c: c.get("created_at") or "", reverse=True)
    return {"campaigns": items}


# Declared BEFORE /campaigns/{campaign_id}: FastAPI matches in definition order,
# so with the parameterised route first this would resolve as campaign_id
# "history" and 404.
@app.get("/campaigns/history")
def campaign_history_route(name: str | None = None, limit: int = 12,
                           principal: Principal = Depends(require_user)) -> dict:
    """A suite's campaigns lined up in time: scenario × campaign, with the
    metrics each was judged on.

    `name` picks the suite (campaigns sharing a name are reruns of it). Without
    it, the newest campaign's name is used — the common case is "show me how the
    thing I just ran has been trending"."""
    visible = [c for c in _CAMPAIGNS.values()
               if _may_see(principal, c.get("owner_id"))]
    if not visible:
        return {"suite": None, "suites": [], "scenarios": [], "campaigns": []}
    suites = sorted({c["name"] for c in visible})
    if name is None:
        newest = max(visible, key=lambda c: c.get("created_at") or "")
        name = newest["name"]
    records = [c for c in visible
               if c["name"] == name and c.get("runs")]
    return {"suite": name, "suites": suites,
            **campaign_history(records, max(2, min(50, limit)))}


@app.get("/campaigns/{campaign_id}")
def campaign_get(campaign_id: str,
                 principal: Principal = Depends(require_user)) -> dict:
    """Full campaign record: thresholds, per-run verdicts and violations."""
    return _campaign_or_404(campaign_id, principal)


@app.get("/campaigns/{campaign_id}/datasets")
def campaign_datasets(campaign_id: str,
                      principal: Principal = Depends(require_user)) -> dict:
    """Every run's frames for this campaign, in one response.

    The exporters need the whole time series; fetching it a run at a time meant
    one request per run, serialised, each carrying a full frame list. This is the
    same data as one query. Large by nature — call it when exporting, not while
    rendering.
    """
    _campaign_or_404(campaign_id, principal)
    return {"datasets": store.load_campaign_datasets(campaign_id)}


@app.post("/campaigns/{campaign_id}/stop")
def campaign_stop(campaign_id: str,
                  principal: Principal = Depends(require_user)) -> dict:
    """Ask a running campaign to stop after the current run.

    Not a kill: the run already on the engine finishes and is scored, then the
    queue is abandoned. The results collected so far stay — a campaign stopped
    after 4 of 10 runs is 4 runs of evidence, not a discarded one."""
    record = _campaign_or_404(campaign_id, principal)
    if record.get("status") != "running":
        return {"campaign_id": campaign_id, "status": record.get("status"),
                "stop_requested": False}
    record["stop_requested"] = True
    _campaign_save(record)
    return {"campaign_id": campaign_id, "status": "stopping", "stop_requested": True}


@app.get("/campaigns/{campaign_id}/compare")
def campaign_compare(campaign_id: str, to: str,
                     principal: Principal = Depends(require_user)) -> dict:
    """Diff this campaign against a baseline: per scenario, what moved and by how
    much, and whether the move is bigger than the baseline's own seed-to-seed
    spread. `to` is the BASELINE's id (the thing being compared against)."""
    # Both sides go through the ownership gate: comparing against a campaign you
    # cannot see would leak its metrics.
    current = _campaign_or_404(campaign_id, principal)
    baseline = _campaign_or_404(to, principal)
    if campaign_id == to:
        raise HTTPException(status_code=400, detail="a campaign cannot be its own baseline")
    return diff_campaigns(baseline, current)


@app.delete("/campaigns/{campaign_id}")
def campaign_delete(campaign_id: str,
                    principal: Principal = Depends(require_user)) -> dict:
    """Forget a campaign. The underlying run datasets are left alone — they are
    evidence, and other campaigns or comparisons may reference them."""
    _campaign_or_404(campaign_id, principal)
    if _CAMPAIGNS[campaign_id].get("status") == "running":
        raise HTTPException(status_code=409,
                            detail="campaign is still running — stop it first")
    _CAMPAIGNS.pop(campaign_id)
    store.delete_campaign(campaign_id)
    return {"deleted": campaign_id}


# ═══════════════════════════════════════════════════════════════════════════════
# SAVED SCENARIOS
# Named configurations, kept server-side. They used to live in the browser's
# localStorage, which meant they could not be opened on another machine, could
# not be shared, and — the reason the load path needs a normaliser — kept
# whatever shape the schema had on the day they were saved, indefinitely.
# ═══════════════════════════════════════════════════════════════════════════════

class ScenarioSave(BaseModel):
    name: str
    config: NSGSimulationConfig      # validated here, not merely stored
    description: str | None = None
    source: str | None = None        # 'ai' | 'manual' | 'preset'


@app.get("/scenarios")
def scenarios_list(all: bool = False,
                   principal: Principal = Depends(require_user)) -> dict:
    names = store.usernames_by_id()
    rows = [x for x in store.load_scenarios()
            if _may_see(principal, x.get("owner_id"), all)]
    for x in rows:
        x["owner"] = names.get(x.get("owner_id") or "")
    return {"scenarios": rows}


@app.post("/scenarios")
def scenario_save(req: ScenarioSave,
                  principal: Principal = Depends(require_user)) -> dict:
    """Store a named snapshot of a configuration.

    The config goes through the schema on the way in. A rejected save is far
    kinder than a stored one that cannot be loaded: the browser-side version
    accepted anything and surfaced the problem months later, on load, as a
    canvas crash reading a field that never existed in that record.
    """
    entry = {
        "id": uuid.uuid4().hex[:12],
        "name": (req.name or "").strip() or "Untitled scenario",
        "description": req.description,
        "config": req.config.model_dump(mode="json"),
        "source": req.source,
    }
    entry["owner_id"] = _owner_id(principal)
    store.save_scenario(entry)
    return {"scenario": entry}


@app.delete("/scenarios/{scenario_id}")
def scenario_delete(scenario_id: str,
                    principal: Principal = Depends(require_user)) -> dict:
    _owned = next((x for x in store.load_scenarios() if x["id"] == scenario_id), None)
    if _owned is not None and not _may_see(principal, _owned.get("owner_id")):
        raise HTTPException(status_code=404, detail="scenario not found")
    if not store.delete_scenario(scenario_id):
        raise HTTPException(status_code=404, detail="scenario not found")
    return {"deleted": scenario_id}


@app.post("/scenario/generate", dependencies=[Depends(require_user)])
def scenario_generate(req: topology.TopologyRequest) -> dict:
    """Procedural topology — hex, Manhattan street grid, or scattered.

    VIAVI's Scenario_Generation block: ask for "7 cells, hexagonal, 350 m apart,
    2 UEs each" instead of placing every cell by hand. Returns a complete
    NSGSimulationConfig, already validated, so the caller can run it or edit it.

    The generator's real job is the engine's validated envelope: cell count,
    UEs per cell, clearance from the LTE anchor, UEs off their own tower and
    spread across cells, and non-overlapping buildings that never swallow a UE
    spawn. Each of those is a way a hand-built scenario returns zero DU metrics
    or aborts ns-3 — see engines/nsoran. A generated scenario is one that RUNS.
    """
    cfg = topology.generate(req)
    # Validate here rather than at the caller: a generator that can emit an
    # invalid config is not doing its job.
    #
    # And return the VALIDATED MODEL, not the dict that went in. The generator
    # only writes the fields it cares about, so the raw dict is missing every
    # defaulted one — including nested objects like radio.antenna. Anything
    # downstream that reads a default it did not have to think about (the
    # topology canvas does exactly this) then crashes on undefined. Dumping the
    # model materialises the whole config, which is what a generator should
    # hand back anyway.
    model = NSGSimulationConfig(**cfg)
    cfg = model.model_dump(mode="json")
    return {"config": cfg,
            "summary": {"layout": req.layout, "cells": len(cfg["base_stations"]),
                        "ues": sum(g["count"] for g in cfg["ue_groups"]),
                        "buildings": len(cfg["buildings"])}}


@app.post("/partition/advice")
def partition_advice_route(config: NSGSimulationConfig) -> dict:
    """How should this scenario be split across processes, and what does it cost?

    A scenario can run as one ns-3 process or as N processes of whole cells.
    Splitting is the only speed lever with real headroom — every single-process
    lever stacked is about 2.2x — and the cost depends entirely on the shape:
    at twelve cells, two, three and four groups cost NOTHING in handovers while
    speed goes 2.2x to 6.7x, and only below three cells per group does handover
    start to disappear.

    This returns every shape with its estimated trade so the choice is made
    knowingly. The numbers are a law fitted to measurement (~7%), not a
    per-scenario prediction, and the response says so — a caller that presents
    them as measurements is misusing them.

    Note the counts are taken from the CLAMPED engine view, not the raw config,
    so the advice describes the run that would actually happen.
    """
    p = nsoran_params(config)
    n_cells = int(p.get("n_gnb") or 1)
    # The REAL split per shape, not UEs divided by groups. Computing it here
    # costs nothing (it is the same nearest-cell assignment the run would do)
    # and it is the difference between advising "50 UEs each" and showing the
    # 75 / 25 / 50 that actually happens.
    splits = {g: partition_run.split_counts(p, g)
              for g in range(1, n_cells + 1) if n_cells % g == 0}
    return partition_advice.advise(n_cells, int(p.get("n_ue") or 1), splits=splits,
                                   params=p)


@app.post("/config/normalize")
def config_normalize(raw: dict) -> dict:
    """Run a config through the schema and return it COMPLETE.

    The config editor writes its result straight into the browser's store, so
    nothing had ever filled in the fields pydantic fills. A field declared with
    `default_factory` shows up in the JSON schema with neither a `default` nor a
    place in `required` — `radio.antenna` and `radio.sleep` are exactly that — so
    a base station added in the editor arrived with no antenna and the canvas died
    on `bs.radio.antenna.type`.

    Normalising here closes the class rather than the instance: every future
    default_factory field, every omitted nested model and every out-of-range value
    is either filled or reported, by the same code that guards the engines.
    """
    try:
        cfg = NSGSimulationConfig.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": "Config failed schema validation.",
                    "errors": json.loads(exc.json())},
        )
    return {"config": cfg.model_dump(mode="json")}


@app.get("/schema")
def get_schema() -> dict:
    """
    Return the JSON schema for NSGSimulationConfig.
    Useful for:
      - Auto-generating frontend forms (react-hook-form + zod)
      - Validating configs client-side before submitting
      - Documenting the API contract
    """
    # ui_json_schema, not model_json_schema: the latter strips numeric bounds
    # for Anthropic's structured-output API, and the config editor needs them.
    return NSGSimulationConfig.ui_json_schema()


# ═══════════════════════════════════════════════════════════════════════════════
# LIVE TRAFFIC PATH ENDPOINTS (analytical estimator, no subprocess)
# ═══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/live-traffic")
async def ws_live_traffic(websocket: WebSocket) -> None:
    """
    Live Traffic Mode: streams per-slice throughput estimates.

    Protocol:
      1. Client connects, then sends one JSON message — the full
         NSGSimulationConfig to simulate (same shape as POST /run-simulation's
         body).
      2. Server validates it, then sends one frame every
         settings.report_interval_ms:
           {"time": tick, "eMBB": <Mbps>, "uRLLC": <Mbps>,
            "mMTC": <Mbps>, "voice": <Mbps>}
      3. Streaming continues until the client disconnects (closes the socket).
         There is no in-band stop command — closing the connection IS stop.

    No subprocess, no NS-3 binary, no event loop blocking: estimate_live_throughput()
    is a pure-Python analytical model (Option B from the Live Traffic Mode
    architecture review — see ns3_bridge.py's module docstring), so each tick
    is just a synchronous function call followed by asyncio.sleep().

    Guarded before accept(): with enforcement on, an unauthenticated socket is
    closed with 1008 and a readable reason rather than accepted and left silent,
    which is the hardest failure to diagnose from the client end.
    """
    if await require_ws(websocket) is None:
        return
    await websocket.accept()

    try:
        raw_config = await websocket.receive_json()
        cfg = NSGSimulationConfig.model_validate(raw_config)
    except WebSocketDisconnect:
        return
    except ValidationError as exc:
        await websocket.send_json({
            "type": "error",
            "message": "Config failed schema validation.",
            "errors": json.loads(exc.json()),
        })
        await websocket.close()
        return
    except Exception as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})
        await websocket.close()
        return

    # REAL engine: Live Traffic now streams MEASURED KPM from the ns-O-RAN
    # engine instead of the analytical estimator — per-slice throughput from
    # each UE's real slice tag, measured power, PRB%, QoS satisfaction. The
    # engine boots ~40 s before the first frame and shares its single-session
    # lock with the RIC Test tab (a busy engine yields a clear error frame).
    # The analytical estimator (ns3_bridge.estimate_live_throughput) stays for
    # the coverage/REM paths but no longer feeds this dashboard.
    try:
        await run_live_traffic(websocket, cfg, nsoran_params(cfg))
    except WebSocketDisconnect:
        logger.info("Live traffic client disconnected")
    except Exception as exc:
        logger.exception("Live traffic stream error")
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass  # socket is most likely already gone


# ═══════════════════════════════════════════════════════════════════════════════
# RIC TEST PATH — simplified E2 (bidirectional: KPM up, RC control down)
# ═══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/e2")
async def ws_e2(websocket: WebSocket) -> None:
    """
    Simplified E2 interface for RIC testing, with SHARED SESSIONS so a browser
    and a user's external xApp (their AI under test) can drive/observe the same
    emulated RAN.

    First message from the client selects create-or-join:
      • CREATE — send the NSGSimulationConfig (bare object, or {"config": {...},
        "label": "..."}). Server creates a session and replies
        {"type": "session", "session_id": "<8-hex>"}. The browser shows this id.
      • JOIN — send {"join": "<session_id>", "label": "my-xapp"}. The external
        xApp joins the browser's existing session.

    After that, for every connected client:
      • KPM report frames stream every report_interval_ms (broadcast to all):
          {"type": "kpm", "tick", "cells", "ue_groups", "slices", "control_state"}
      • The client may send RC control commands any time; each is applied to the
        SHARED sim and echoed to ALL clients as
          {"type": "control_event", "command", "ack", "source"}
        so the browser observes what the xApp did (and vice-versa). The sender
        also gets a direct {"type": "control_ack", ...}.

    NOT standards-compliant O-RAN E2 (no E2AP/ASN.1/SCTP) — our own JSON contract
    with the same closed-loop semantics. Full E2 compliance is a later milestone.
    """
    if await require_ws(websocket) is None:
        return
    await websocket.accept()

    try:
        first = await websocket.receive_json()
    except WebSocketDisconnect:
        return

    # ── Resolve session: join existing, or create new from config ─────────────
    if isinstance(first, dict) and "join" in first:
        session = ric_manager.get(first["join"])
        if session is None:
            await websocket.send_json({"type": "error", "message": f"session '{first['join']}' not found"})
            await websocket.close()
            return
        label = str(first.get("label", "xapp"))
        is_owner = False
    else:
        raw = first.get("config", first) if isinstance(first, dict) else first
        try:
            cfg = NSGSimulationConfig.model_validate(raw)
        except ValidationError as exc:
            await websocket.send_json({
                "type": "error",
                "message": "Config failed schema validation.",
                "errors": json.loads(exc.json()),
            })
            await websocket.close()
            return
        except Exception as exc:
            await websocket.send_json({"type": "error", "message": str(exc)})
            await websocket.close()
            return
        interval_s = max(0.2, cfg.settings.report_interval_ms / 1000)
        session = ric_manager.create(cfg, interval_s)
        label = str(first.get("label", "observer")) if isinstance(first, dict) else "observer"
        is_owner = True  # the creator (browser) owns the session lifecycle
        await websocket.send_json({"type": "session", "session_id": session.id})

    session.add_client(websocket, label)
    # Immediate snapshot so a joiner doesn't wait a full interval for first data.
    await websocket.send_json({"type": "kpm", **session.sim.get_kpm_report()})
    await session.broadcast({"type": "roster", "clients": session.roster()})

    try:
        while True:
            # A customer's xApp is ARBITRARY code — it will eventually emit a
            # malformed frame (non-JSON, a list, a wrong-typed field). Without a
            # per-message guard those raised out to the handler's broad except,
            # which silently dropped the client (and, for the session OWNER, tore
            # down the whole session). Reject the message, keep the connection.
            try:
                msg = await websocket.receive_json()
            except ValueError:      # includes json.JSONDecodeError
                await websocket.send_json({
                    "type": "control_ack", "success": False,
                    "detail": "malformed message: expected a JSON object"})
                continue
            if not isinstance(msg, dict):
                await websocket.send_json({
                    "type": "control_ack", "success": False,
                    "detail": f"malformed message: expected an object, got {type(msg).__name__}"})
                continue

            # Start/stop the built-in LLM-driven AI xApp (browser-triggered,
            # runs server-side so the LLM key never leaves the backend).
            if isinstance(msg, dict) and "ai_xapp" in msg:
                spec = msg["ai_xapp"] or {}
                if spec.get("enabled"):
                    goal = str(spec.get("goal") or "keep QoS high and save energy where possible")
                    llm_config = spec.get("llm")  # optional: user's own model
                    has_override = bool(
                        isinstance(llm_config, dict)
                        and llm_config.get("model")
                        and llm_config.get("api_key")
                    )
                    if not ai_configured() and not has_override:
                        await websocket.send_json({
                            "type": "ai_status",
                            "running": False,
                            "error": "AI service unavailable",
                        })
                        continue
                    session.start_ai(goal, decide_xapp_actions, llm_config)
                    using = (llm_config or {}).get("model") or "built-in"
                    await session.broadcast({"type": "ai_status", "running": True, "goal": goal, "model": using})
                else:
                    session.stop_ai()
                    await session.broadcast({"type": "ai_status", "running": False})
                continue

            # Operator injects an anomaly (cell fault / traffic surge) the AI
            # under test must respond to.
            if isinstance(msg, dict) and "inject" in msg:
                try:
                    ack = await session.inject_anomaly(msg["inject"] or {}, label)
                except Exception as exc:  # noqa: BLE001 — bad operator payload
                    ack = {"action": "inject", "success": False,
                           "detail": f"rejected: {type(exc).__name__}"}
                await websocket.send_json({"type": "inject_ack", **ack})
                continue

            # Same guard for control: a wrong-typed field (prb_percent "abc") or a
            # missing key must come back as a failed ack, not kill the xApp.
            try:
                ack = await session.apply_control(msg, label)
            except Exception as exc:  # noqa: BLE001 — arbitrary client payload
                logger.warning("rejected malformed control from %s: %r (%s)",
                               label, msg, exc)
                ack = {"action": msg.get("action"), "success": False,
                       "detail": f"rejected: {type(exc).__name__}"}
            await websocket.send_json({"type": "control_ack", **ack})
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("RIC E2 client (%s) error on session %s", label, session.id)
    finally:
        session.remove_client(websocket)
        if is_owner:
            # Owner left → end the whole session (kick xApps, stop the AI).
            await session.close_all()
            ric_manager.drop(session)
        else:
            await session.broadcast({"type": "roster", "clients": session.roster()})
            ric_manager.drop_if_empty(session)
        logger.info("RIC E2 client (%s) left session %s", label, session.id)


@app.websocket("/ws/e2-real")
async def ws_e2_real(websocket: WebSocket) -> None:
    """
    LIVE external-xApp session on the REAL ns-O-RAN engine (not the analytical
    RicSimulation behind /ws/e2). Proxies the WebSocket to the engine container's
    live bridge (:8091): the real ns-3 mmWave RAN runs, KPM frames stream out,
    and the client's control commands drive real E2SM-RC on the running RAN.

    Same accessible JSON contract as /ws/e2 (the customer's xApp needs no FlexRIC
    SDK) — but every command lands on real hardware-grade E2AP. One live session
    at a time (the engine runs a single ns-3 scenario).

    This is the contract a CUSTOMER's xApp speaks, so the token may arrive either
    as an Authorization header (what a non-browser client should use) or as
    `?token=` (what a browser is forced into). With enforcement off — the default
    — an xApp that predates authentication connects exactly as before.
    """
    if await require_ws(websocket) is None:
        return
    await websocket.accept()
    try:
        await run_live_proxy(websocket, nsoran_params)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("live e2-real proxy error")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


def _connector_token(request: Request) -> str:
    """The token to bake into a downloaded xApp connector.

    Only when this deployment enforces auth — otherwise the generated file stays
    byte-identical to what customers already have. It is the CALLER's own token,
    so the connector inherits exactly their access and nothing more, and it
    expires with it; the file says so, because a credential in a downloaded file
    that silently stops working is worse than one that explains itself.
    """
    if not auth_required():
        return ""
    header = request.headers.get("authorization") or ""
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


@app.get("/ric/xapp-connector", dependencies=[Depends(require_user)])
def xapp_connector(request: Request, session: str = "") -> Response:
    """
    Return a turnkey Python xApp connector, pre-filled with the WebSocket URL and
    the caller's session id. The user downloads this, drops their model into
    decide(), and runs it on their own machine — their AI connects over /ws/e2
    and drives control. Nothing of theirs runs on our servers.
    """
    host   = request.headers.get("host", "localhost:8000")
    scheme = "wss" if request.url.scheme == "https" else "ws"
    ws_url = f"{scheme}://{host}/ws/e2"

    content = (
        XAPP_CONNECTOR_TEMPLATE
        .replace("__WS_URL__", ws_url)
        .replace("__API_TOKEN__", _connector_token(request))
        .replace("__SESSION_ID__", session or "PASTE_SESSION_ID_HERE")
    )
    return Response(
        content=content,
        media_type="text/x-python",
        headers={"Content-Disposition": "attachment; filename=nsg_xapp.py"},
    )


@app.get("/ric/xapp-connector-real", dependencies=[Depends(require_user)])
def xapp_connector_real(request: Request, session: str = "") -> Response:
    """
    Turnkey Python xApp connector for the REAL engine (/ws/e2-real). The user
    drops their trained model into decide() and runs it; their control becomes
    real E2SM-RC on the running RAN, and they receive the real live KPM stream.

    Pass ?session=<id> (the UI does this while a live session is running) so the
    xApp JOINS that session: one engine run, the operator's scenario, and every
    action visible in the UI. Without it, connecting starts a fresh session on
    the engine's default scenario.
    """
    host   = request.headers.get("host", "localhost:8000")
    scheme = "wss" if request.url.scheme == "https" else "ws"
    ws_url = f"{scheme}://{host}/ws/e2-real"

    content = (XAPP_CONNECTOR_REAL_TEMPLATE
               .replace("__WS_URL__", ws_url)
               .replace("__API_TOKEN__", _connector_token(request))
               .replace("__SESSION_ID__", session or ""))
    return Response(
        content=content,
        media_type="text/x-python",
        headers={"Content-Disposition": "attachment; filename=nsg_xapp_real.py"},
    )


_NSG_AI_DIR       = Path(__file__).resolve().parent          # nsg_ai/
_TRAINING_KIT_DIR = _NSG_AI_DIR.parent / "training_kit"


@app.get("/ric/training-kit", dependencies=[Depends(require_user)])
def training_kit() -> Response:
    """
    Return the self-contained RL training kit as a zip: the Gymnasium
    environment + its simulation engine (physics, sim, schema) + a training
    example + docs. The user trains an agent LOCALLY against this (millions of
    fast local steps, zero server load), then deploys the trained model via the
    xApp connector.

    The engine files are read from the LIVE nsg_ai/ package at request time, so a
    downloaded kit always ships the current schema/sim — never a stale snapshot.
    (training_kit/ keeps them as symlinks into nsg_ai/ so local in-place runs use
    the same source.) Import chain bundled here: nsg_ran_env -> ric_sim ->
    ns3_bridge -> schema; if any of those grows a new nsg_ai dependency, add it to
    the engine list below or the downloaded kit will fail to import.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # Environment + example + docs (kit-specific, from training_kit/).
        for name in ("nsg_ran_env.py", "train_example.py", "requirements.txt", "README.md"):
            z.writestr(name, (_TRAINING_KIT_DIR / name).read_text(encoding="utf-8"))
        # The env's engine — pure-Python sim + physics + schema, LIVE from nsg_ai/.
        for name in ("ric_sim.py", "ns3_bridge.py", "schema.py"):
            z.writestr(name, (_NSG_AI_DIR / name).read_text(encoding="utf-8"))
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=nsg_training_kit.zip"},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# LIVE METRICS PATH ENDPOINTS (InfluxDB analytics stream)
# ═══════════════════════════════════════════════════════════════════════════════

_EMPTY_METRICS = {"results": [{"series": []}, {"series": []}, {"series": []}]}
_INFLUX_BASE   = "http://localhost:29501"


@app.get("/live-metrics", dependencies=[Depends(require_user)])
def get_live_metrics() -> dict:
    """
    Pull real-time KPIs from InfluxDB (populated by the analytics engine).
    Returns empty series when no simulation is running.

    Note: this endpoint is tied to the live analytics path.
    For NS-3 path metrics, parse the remPoints.npy output directly.
    """
    try:
        status = requests.get(
            f"{_INFLUX_BASE}/sba/tests/status/current", timeout=2
        )
        db_name = status.json().get("id")
        if not db_name:
            return _EMPTY_METRICS

        q = (
            'SELECT '
            'last("DRB.UEThpDl") AS tput, '
            'last("PEE.AvgPower") AS power, '
            'last("RRU.PrbUsedDl") AS prb_u, '
            'last("RRU.PrbAvailDl") AS prb_a, '
            'last("Viavi.QoS.Score") AS qos, '
            'last("RRC.ConnMean") AS conn_ues, '
            'last("Viavi.PEE.EnergyEfficiency") AS energy_eff, '
            'last("Viavi.UE.Rsrp") AS rsrp, '
            'last("MM.HoExeInterSucc") AS ho_inter, '
            'last("MM.HoExeIntraSucc") AS ho_intra '
            'FROM "CellReports"'
        )
        res = requests.get(
            f"{_INFLUX_BASE}/sba/influx/query",
            params={"db": db_name, "q": q},
            timeout=5,
        )
        if res.status_code != 200:
            return _EMPTY_METRICS

        outer   = res.json().get("results", [])
        if not outer or "series" not in outer[0]:
            return _EMPTY_METRICS

        series = outer[0]["series"][0]
        cols   = series["columns"]
        vals   = series["values"][0]

        def v(name: str) -> float:
            return vals[cols.index(name)] if name in cols else 0.0

        tput       = v("tput")
        power_mw   = v("power")
        prb_u      = v("prb_u")
        prb_a      = v("prb_a")
        raw_qos    = float(v("qos") or 100)
        conn_ues   = v("conn_ues")
        energy_eff = v("energy_eff")
        rsrp       = v("rsrp")
        handover   = int(v("ho_inter") + v("ho_intra"))

        power_dbm = 10 * math.log10(power_mw) if power_mw > 0 else 0.0
        prb_pct   = (prb_u / prb_a * 100) if prb_a > 0 else 0.0

        # Composite QoS formula (throughput + load + raw score)
        tput_score = min((tput / 0.5) * 100, 100)
        load_score = 100 - max(0.0, (prb_pct - 70) * 3.3)
        qos = max(5.0, min(tput_score * 0.45 + load_score * 0.45 + raw_qos * 0.1, 100.0))

        return {
            "results": [{
                "series": [{
                    "columns": [
                        "time", "tput", "power", "prb", "qos",
                        "conn_ues", "energy_eff", "rsrp", "handover",
                    ],
                    "values": [[
                        vals[0],
                        round(tput, 3),
                        round(power_dbm, 1),
                        round(prb_pct, 1),
                        round(qos, 1),
                        int(conn_ues or 0),
                        round(energy_eff or 0, 2),
                        round(rsrp or 0, 1),
                        handover,
                    ]],
                }],
            }, {"series": []}, {"series": []}],
        }

    except Exception as exc:
        print(f"[live-metrics] {exc}")
        return _EMPTY_METRICS


@app.post("/generate-report", dependencies=[Depends(require_user)])
def generate_report(metrics: ReportMetrics) -> dict:
    """LLM executive summary of the latest simulation KPIs."""
    prompt = (
        "Analyze the following 5G network simulation results as a senior telecom engineer:\n"
        f"  - Average Throughput    : {metrics.tput} Gbps\n"
        f"  - Average Power         : {metrics.power} dBm\n"
        f"  - PRB Utilization       : {metrics.prb}%\n"
        f"  - Network QoS Score     : {metrics.qos}/100\n\n"
        "Write a concise executive summary (2–3 sentences) highlighting key findings "
        "and any immediate optimization recommendations."
    )
    try:
        report = _agent.generate_text(prompt)
        return {"status": "success", "report": report}
    except (AIUnavailableError, APIError):
        logger.warning("AI service unavailable", exc_info=True)
        raise HTTPException(status_code=503, detail="AI service unavailable")
    except Exception:
        logger.exception("AI report failed")
        raise HTTPException(status_code=500, detail="Report generation failed")


@app.get("/envelope")
def engine_envelope_limits(bw_mhz: float | None = None,
                           numerology: int = 2,
                           n_gnb: int | None = None) -> dict:
    """The measured engine envelope, so the CANVAS can warn while a scenario is
    being drawn instead of the backend clamping it silently at run time.

    Drawing is where a scenario gets too big: a few clicks add UE groups of 40
    and 80, and nothing said the run would then be capped. The clamp is still
    the safety net — this is the part that lets someone avoid needing it.
    """
    return {
        # WHOSE LIMITS THESE ARE. Every number below was measured on ns-3 —
        # where the mmWave scheduler splits a transport block to zero, where
        # its heap corrupts, where a run is capped. The FAST TIER has none of
        # that code: `tools/fast_tier_envelope.py` runs 48 cells and 5,760
        # UEs clean, every UE served, 44 MB, and beats real time to 48 cells
        # / 1,440 UEs. Its only limit is that a window eventually costs more
        # wall clock than the simulated second it stands for — and it still
        # produces the window. A caller that shows these must say which tier.
        "tier": "slow",
        "fast_tier": {
            "aborts": False,
            "measured_clean": {"cells": 48, "ues": 5760, "rss_mb": 44},
            "real_time_to": {"cells": 48, "ues": 1440, "ms_per_window": 781},
            "note": "no hard ceiling — past the real-time point it is slower, "
                    "not broken",
        },
        "gnb_min": GNB_MIN, "gnb_max": GNB_MAX,
        "ue_min": UE_MIN, "ue_max": UE_MAX,
        "ue_per_cell_min": UE_PER_CELL_MIN, "ue_per_cell_max": UE_PER_CELL_MAX,
        "bw_mhz_min": BW_MHZ_MIN, "bw_mhz_max": BW_MHZ_MAX,
        "min_cell_separation_m": MIN_CELL_SEPARATION_M,
        # `ue_practical_max` is gone with the span-law fields. All three were
        # fitted to sweep data whose every span_s came through the engine's old
        # five-second KPM collection window, so they described the window; the
        # clean re-measurement on 1.12 found the simulated horizon is under one
        # second at EVERY size, which leaves no threshold for a "practical max"
        # to mark. The canvas must not warn on any of it.
        # What remains is the cap that depends on the scenario, computed HERE so
        # the canvas never reimplements the model: it interpolates measured
        # crash points and is not a flat ratio, and it is null when the caller
        # did not say what it is drawing.
        "ue_per_cell_max_for_bw": (
            max_ue_per_cell_for_bw(bw_mhz, numerology) if bw_mhz else None),
    }


@app.get("/engine-pool", dependencies=[Depends(require_user)])
async def engine_pool_status() -> dict:
    """How many ns-O-RAN engines will actually serve a campaign.

    Campaign runs go to a pool, and a configured instance that is not running is
    skipped — correct, but invisible: the campaign then runs sequentially and the
    only symptom is that runs which "started together" finish minutes apart. This
    says so before the run rather than leaving it to be inferred.
    """
    p = pool()
    instances = await p.health()
    alive = sum(1 for i in instances if i["alive"])

    # BUSY IS NOT DEAD, and the probe alone cannot tell them apart: the bridge
    # serves one connection at a time and listens with a backlog of 1, so while
    # a run is in flight it REFUSES the probe exactly as a stopped container
    # would. The backend knows the difference without asking — it started the
    # run — so it says so here, rather than letting a normal campaign report
    # "0 of 1 engines reachable" and read as a broken engine.
    in_flight = sum(1 for r in _SIM_RUNS.values() if r.get("status") == "running")
    return {"configured": p.size, "alive": alive, "instances": instances,
            "runs_in_flight": in_flight,
            "concurrent_runs": max(1, alive)}


@app.get("/engine-status", dependencies=[Depends(require_user)])
def get_engine_status() -> dict:
    """Analytics engine health check."""
    try:
        res = requests.get(
            f"{_INFLUX_BASE}/sba/tests/status/current", timeout=2
        )
        return res.json()
    except Exception:
        return {"status": "offline"}


@app.post("/stop", dependencies=[Depends(require_user)])
def stop_engine() -> dict:
    """Stop the currently running simulation."""
    for path in ["/sba/tests/run", "/sba/tests/status/all"]:
        try:
            requests.delete(f"{_INFLUX_BASE}{path}", timeout=5)
        except Exception:
            pass
    return {"status": "success"}


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITY ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/reset", dependencies=[Depends(require_user)])
def reset_agent() -> dict:
    """Reinitialise the LLM agent (clears any in-memory state)."""
    global _agent
    _agent = NSGAgent()
    return {"status": "success", "message": "Agent reinitialized"}


# ── What stays open, and why ─────────────────────────────────────────────────
# With NSG_AUTH_REQUIRED=1 everything that acts on someone's work, spends money,
# or returns the running network's data needs a token. These do not, and each one
# is a decision rather than an oversight:
#
#   /health             a liveness probe; a monitor that needs credentials to ask
#                       "are you alive" is a monitor that reports the wrong thing
#                       when the credentials expire
#   /auth/login         obviously
#   /schema, /envelope  product METADATA — the config editor's field list and the
#                       measured limits. No user data, and tools/preflight.py
#                       probes /schema unauthenticated precisely as a check that
#                       the backend still has its parameter list
#   /config/normalize   a pure function over a payload the caller already holds
#
# Everything else on this file carries require_user. The audit that found the gap
# is a route-by-route scan; re-run it after adding an endpoint.
@app.get("/ai/status", dependencies=[Depends(require_user)])
def ai_status() -> dict:
    configured = ai_configured()
    return {
        "configured": configured,
        "status": "configured" if configured else "unconfigured",
    }

@app.get("/health")
def health() -> dict:
    """Liveness probe for container orchestration."""
    return {"status": "ok", "agent_ready": _agent is not None}


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
