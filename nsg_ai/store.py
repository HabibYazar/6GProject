"""
Persistence for runs and campaigns.

Every function here takes and returns the SAME dict shapes the application
already passes around — the ones that used to be written to
`sim_runs/*.json` and `campaigns/*.json`. That is deliberate: the callers in
main.py should not learn about the ORM, and swapping the backing store again
later should touch this file only.

What changes, and what does not:

Runs are still held in memory while they execute. Progress arrives many times a
second and belongs in a process-local dict, not in a table. What moves here is
the durable record, and the INDEX — the thing that used to be rebuilt by
globbing a directory at startup.

The startup pass does not disappear, it becomes a query. Marking interrupted
campaigns was a loop over every file that mutated records in place; here it is
one UPDATE against the rows that are actually wrong, and it can no longer miss a
row because a file failed to parse.
"""
from __future__ import annotations

import datetime as _dt
import logging

from sqlalchemy import delete, select, update

# Flat imports, matching the rest of main.py's module set: uvicorn is started
# with the working directory inside nsg_ai/, so these are top-level modules
# there. db.py and models.py deliberately import nothing local, which is what
# lets alembic/env.py import them as `nsg_ai.db` / `nsg_ai.models` from the
# project root without either style breaking the other.
from db import session_scope
from models import AiInteraction, Campaign, CampaignRun, Run, RunFrame, Scenario, User

logger = logging.getLogger(__name__)


def _to_dt(v) -> _dt.datetime | None:
    """ISO string (what the API speaks) -> aware datetime (what the column is)."""
    if not v:
        return None
    if isinstance(v, _dt.datetime):
        return v
    try:
        d = _dt.datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)


def _iso(v: _dt.datetime | None) -> str | None:
    """Always UTC on the way out.

    Postgres renders timestamptz in the server's timezone, so the same instant
    came back as +03:00 having been stored as +00:00. Harmless in isolation, but
    /sim/runs orders results by comparing these as STRINGS — and ISO timestamps
    with different offsets do not string-sort into chronological order. Pinning
    the representation makes the API output independent of where the database
    happens to run.
    """
    if not v:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=_dt.timezone.utc)
    return v.astimezone(_dt.timezone.utc).isoformat()


# ── runs ────────────────────────────────────────────────────────────────────

def save_run(dataset: dict) -> None:
    """Persist a finished run and its frames.

    Frames are replaced rather than appended: a run is written once when it
    completes, and a re-save means the previous attempt's rows are stale.
    """
    run_id = dataset["id"]
    frames = dataset.get("frames") or []
    with session_scope() as s:
        s.merge(Run(
            id=run_id,
            owner_id=dataset.get("owner_id"),
            config=dataset.get("config") or {},
            requested=dataset.get("requested"),
            status=dataset.get("status", "unknown"),
            # Default "live" because that is what every caller before batch runs
            # were stored was doing; an explicit kind is only set by the batch
            # path. Never leave it null — the listing filters on it.
            kind=dataset.get("kind") or "live",
            error=dataset.get("error"),
            n_frames=int(dataset.get("n_frames") or 0),
            summary=dataset.get("summary"),
            status_lines=dataset.get("status_lines"),
            started_at=_to_dt(dataset.get("started_at")),
            finished_at=_to_dt(dataset.get("finished_at")),
        ))
        s.flush()
        s.execute(delete(RunFrame).where(RunFrame.run_id == run_id))
        if frames:
            s.bulk_insert_mappings(RunFrame, [
                {"run_id": run_id, "tick": f.get("tick", i), "payload": f}
                for i, f in enumerate(frames)
            ])


def load_run(run_id: str) -> dict | None:
    """The full dataset, in the shape the endpoint has always returned."""
    with session_scope() as s:
        r = s.get(Run, run_id)
        if r is None:
            return None
        frames = s.execute(
            select(RunFrame.payload).where(RunFrame.run_id == run_id)
            .order_by(RunFrame.tick)
        ).scalars().all()
        return {
            "id": r.id,
            "owner_id": r.owner_id,
            "started_at": _iso(r.started_at),
            "finished_at": _iso(r.finished_at),
            "config": r.config,
            "requested": r.requested,
            "status": r.status,
            "kind": r.kind or "live",
            "error": r.error,
            "n_frames": r.n_frames,
            "summary": r.summary,
            "status_lines": r.status_lines or [],
            "frames": list(frames),
        }


def load_run_index(limit: int = 500) -> dict[str, dict]:
    """Metadata for the most recent runs, keyed by id — no frames.

    This replaces the startup glob. Frames are the bulk of a run and the index
    never needed them; reading them anyway is what made the old pass slow enough
    to notice.

    Bounded on purpose. The file version loaded every run that had ever been
    stored, which was fine at thirty and is not at ten thousand — and the index
    is a convenience, not the record: `load_run` goes to the database by id, so
    a run older than the window is still fully retrievable.
    """
    out: dict[str, dict] = {}
    with session_scope() as s:
        rows = s.execute(
            select(Run.id, Run.status, Run.started_at, Run.finished_at,
                   Run.config, Run.requested, Run.n_frames, Run.summary, Run.error,
                   Run.owner_id, Run.kind)
            .order_by(Run.started_at.desc().nullslast())
            .limit(max(1, limit))
        ).all()
    for (rid, status, started, finished, config, requested,
         n_frames, summary, error, owner_id, kind) in rows:
        out[rid] = {
            "id": rid,
            "status": status,
            "kind": kind or "live",
            "owner_id": owner_id,
            "started_at": _iso(started),
            "finished_at": _iso(finished),
            "config_name": (config or {}).get("name"),
            "requested": requested or {},
            "frames_target": (requested or {}).get("frames"),
            "n_frames": n_frames,
            "summary": summary,
            "error": error,
        }
    return out


# ── campaigns ───────────────────────────────────────────────────────────────

def save_campaign(record: dict) -> None:
    """Upsert a campaign and the full ordered list of its runs.

    The run rows are rewritten wholesale on every save. A campaign has tens of
    rows, saves happen once per completed run, and rewriting removes a class of
    bug where the in-memory list and the stored list drift apart.
    """
    cid = record["id"]
    with session_scope() as s:
        s.merge(Campaign(
            id=cid,
            owner_id=record.get("owner_id"),
            planned=record.get("planned"),
            name=record.get("name") or "",
            description=record.get("description"),
            status=record.get("status", "unknown"),
            verdict=record.get("verdict"),
            base_seed=record.get("base_seed"),
            tier=record.get("tier"),
            partition_groups=record.get("partition_groups"),
            wall_cap_s=record.get("wall_cap_s"),
            thresholds=record.get("thresholds"),
            stop_requested=bool(record.get("stop_requested")),
            created_at=_to_dt(record.get("created_at")) or _dt.datetime.now(_dt.timezone.utc),
            finished_at=_to_dt(record.get("finished_at")),
        ))
        s.flush()
        s.execute(delete(CampaignRun).where(CampaignRun.campaign_id == cid))
        rows = []
        for i, r in enumerate(record.get("runs") or []):
            rows.append({
                "campaign_id": cid, "position": i,
                "label": r.get("label") or "", "item_label": r.get("item_label"),
                "repeat": r.get("repeat"), "seed": r.get("seed"),
                "run_id": r.get("run_id"), "status": r.get("status", "queued"),
                "verdict": r.get("verdict"), "violations": r.get("violations"),
                "summary": r.get("summary"), "error": r.get("error"),
                "config": r.get("config"),
                "started_at": _to_dt(r.get("started_at")),
                "finished_at": _to_dt(r.get("finished_at")),
            })
        if rows:
            s.bulk_insert_mappings(CampaignRun, rows)


def load_campaign_datasets(campaign_id: str) -> dict[str, dict]:
    """Every frame of every run in one campaign, keyed by run id.

    Exporting a campaign used to mean one HTTP request per run, each returning
    that run's complete frame list — twenty-one requests and several megabytes
    for a three-seed matrix, issued one after another because each awaited the
    last. The data was always one join away: campaign_runs gives the run ids,
    run_frames gives their frames, and the database can return the lot ordered.

    The shape matches what the exporters already consume, so only the fetch
    changes.
    """
    with session_scope() as s:
        run_ids = s.execute(
            select(CampaignRun.run_id)
            .where(CampaignRun.campaign_id == campaign_id,
                   CampaignRun.run_id.is_not(None))
        ).scalars().all()
        if not run_ids:
            return {}

        meta = {
            r.id: r for r in s.execute(
                select(Run).where(Run.id.in_(run_ids))).scalars().all()
        }
        out: dict[str, dict] = {
            rid: {
                "id": rid,
                "started_at": _iso(meta[rid].started_at) if rid in meta else None,
                "finished_at": _iso(meta[rid].finished_at) if rid in meta else None,
                "config": meta[rid].config if rid in meta else {},
                "status": meta[rid].status if rid in meta else "unknown",
                "n_frames": meta[rid].n_frames if rid in meta else 0,
                "summary": meta[rid].summary if rid in meta else None,
                "frames": [],
            }
            for rid in run_ids if rid
        }
        rows = s.execute(
            select(RunFrame.run_id, RunFrame.payload)
            .where(RunFrame.run_id.in_(run_ids))
            .order_by(RunFrame.run_id, RunFrame.tick)
        ).all()
        for rid, payload in rows:
            if rid in out:
                out[rid]["frames"].append(payload)
        return out


def _campaign_to_dict(c: Campaign) -> dict:
    return {
        "id": c.id, "name": c.name, "description": c.description,
        "status": c.status, "verdict": c.verdict,
        "created_at": _iso(c.created_at), "finished_at": _iso(c.finished_at),
        "base_seed": c.base_seed, "thresholds": c.thresholds or [],
        "tier": c.tier, "partition_groups": c.partition_groups,
        "wall_cap_s": c.wall_cap_s,
        "owner_id": c.owner_id,
        "planned": c.planned or [],
        "stop_requested": c.stop_requested,
        "runs": [{
            "label": r.label, "item_label": r.item_label, "repeat": r.repeat,
            "seed": r.seed, "run_id": r.run_id, "status": r.status,
            "verdict": r.verdict, "violations": r.violations or [],
            "summary": r.summary, "error": r.error, "config": r.config,
            "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at),
        } for r in c.items],
    }


def load_campaigns() -> dict[str, dict]:
    with session_scope() as s:
        cs = s.execute(select(Campaign).order_by(Campaign.created_at)).scalars().all()
        return {c.id: _campaign_to_dict(c) for c in cs}


def delete_campaign(campaign_id: str) -> bool:
    with session_scope() as s:
        c = s.get(Campaign, campaign_id)
        if c is None:
            return False
        s.delete(c)          # campaign_runs cascade
        return True


def mark_interrupted() -> int:
    """Close out work that died with a previous process.

    A campaign left 'running' has no job behind it, and neither has the run it
    was in the middle of — the UI would poll a corpse and show live progress for
    a queue that no longer exists.

    Standalone runs are closed too, and NOT only when a campaign was stale. The
    previous version returned early if no campaign needed fixing, so a /sim/run
    that died on its own stayed "running" for ever — three were sitting like that
    in the index, shown as live with nothing behind them.
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    died = "the backend restarted while this run was in flight"
    with session_scope() as s:
        stale = s.execute(
            select(Campaign.id).where(Campaign.status == "running")
        ).scalars().all()
        if stale:
            s.execute(update(Campaign).where(Campaign.id.in_(stale))
                      .values(status="interrupted", finished_at=now))
            s.execute(update(CampaignRun)
                      .where(CampaignRun.campaign_id.in_(stale),
                             CampaignRun.status == "running")
                      .values(status="interrupted", verdict="error",
                              finished_at=now,
                              error=died + ", so it produced no result"))
        orphans = s.execute(update(Run)
                            .where(Run.status.in_(("running", "queued")))
                            .values(status="interrupted", finished_at=now,
                                    error=died)).rowcount
    logger.info("marked %d interrupted campaign(s), %d interrupted run(s)",
                len(stale), orphans)
    return len(stale)


# ── scenarios ───────────────────────────────────────────────────────────────

def save_scenario(record: dict) -> None:
    with session_scope() as s:
        s.merge(Scenario(
            id=record["id"], name=record.get("name") or "",
            description=record.get("description"),
            config=record.get("config") or {},
            source=record.get("source"),
            changed_fields=record.get("changed_fields"),
            owner_id=record.get("owner_id"),
        ))


def load_scenarios() -> list[dict]:
    with session_scope() as s:
        rows = s.execute(select(Scenario).order_by(Scenario.created_at.desc())).scalars().all()
        return [{"id": x.id, "name": x.name, "description": x.description,
                 "config": x.config, "source": x.source, "owner_id": x.owner_id,
                 "created_at": _iso(x.created_at)} for x in rows]


def delete_scenario(scenario_id: str) -> bool:
    with session_scope() as s:
        x = s.get(Scenario, scenario_id)
        if x is None:
            return False
        s.delete(x)
        return True


# ── Users ────────────────────────────────────────────────────────────────────
# Local login records. See nsg_ai/auth.py for why identities are not in a cloud
# provider. These return plain dicts like the rest of the store, so nothing above
# the store layer touches an ORM object bound to a closed session.

def _user_to_dict(u: User) -> dict:
    return {"id": u.id, "username": u.username, "role": u.role,
            "disabled": u.disabled, "password_hash": u.password_hash,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None}


def user_count() -> int:
    with session_scope() as s:
        return s.query(User).count()


def get_user(user_id: str) -> dict | None:
    if not user_id:
        return None
    with session_scope() as s:
        u = s.get(User, user_id)
        return _user_to_dict(u) if u else None


def get_user_by_username(username: str) -> dict | None:
    with session_scope() as s:
        u = s.scalar(select(User).where(User.username == username))
        return _user_to_dict(u) if u else None


def create_user(user_id: str, username: str, password_hash: str,
                role: str = "user") -> dict:
    with session_scope() as s:
        u = User(id=user_id, username=username, password_hash=password_hash,
                 role=role)
        s.add(u)
        s.flush()
        return _user_to_dict(u)


def touch_last_login(user_id: str) -> None:
    with session_scope() as s:
        s.execute(update(User).where(User.id == user_id)
                  .values(last_login_at=_dt.datetime.now(_dt.timezone.utc)))


def list_users() -> list[dict]:
    with session_scope() as s:
        rows = s.scalars(select(User).order_by(User.created_at)).all()
        # Never hand the hash out of the store to a listing caller.
        return [{k: v for k, v in _user_to_dict(u).items() if k != "password_hash"}
                for u in rows]


def set_user_disabled(user_id: str, disabled: bool) -> bool:
    with session_scope() as s:
        r = s.execute(update(User).where(User.id == user_id)
                      .values(disabled=disabled))
        return r.rowcount > 0


def set_user_role(user_id: str, role: str) -> bool:
    with session_scope() as s:
        r = s.execute(update(User).where(User.id == user_id).values(role=role))
        return r.rowcount > 0


def set_user_password(user_id: str, password_hash: str) -> bool:
    with session_scope() as s:
        r = s.execute(update(User).where(User.id == user_id)
                      .values(password_hash=password_hash))
        return r.rowcount > 0


def set_user_username(user_id: str, username: str) -> bool:
    """Rename a login. Safe for their data: scenarios, campaigns, runs and
    ai_interactions are all owned by `owner_id`, never by the name, so a rename
    moves nothing and orphans nothing (checked against the live tables)."""
    with session_scope() as s:
        r = s.execute(update(User).where(User.id == user_id)
                      .values(username=username))
        return r.rowcount > 0


def delete_user(user_id: str) -> bool:
    """Remove the login. Their runs and campaigns are LEFT: they are evidence,
    and an experiment does not stop being valid because the person left. The rows
    keep an owner_id no account answers to, which only an admin then sees."""
    with session_scope() as s:
        u = s.get(User, user_id)
        if u is None:
            return False
        s.delete(u)
        return True


def usernames_by_id() -> dict[str, str]:
    """owner_id -> username, so a listing can say WHOSE a row is.

    An admin can see other people's work; without attribution that is just a
    longer list with no way to tell it apart from your own.
    """
    with session_scope() as s:
        return {u.id: u.username for u in s.scalars(select(User)).all()}


# ── AI interactions (scenario generator training signal) ─────────────────────

def save_ai_interaction(record: dict) -> None:
    """Upsert one generator turn. Merge, because the row is written when the AI
    answers and completed later when the user's edit or run lands."""
    with session_scope() as s:
        s.merge(AiInteraction(
            id=record["id"],
            created_at=_to_dt(record.get("created_at"))
            or _dt.datetime.now(_dt.timezone.utc),
            owner_id=record.get("owner_id"),
            model=record.get("model"),
            prompt=record.get("prompt"),
            ai_config=record.get("ai_config"),
            final_config=record.get("final_config"),
            config_diff=record.get("config_diff"),
            outcome=record.get("outcome") or "pending",
            outcome_source=record.get("outcome_source"),
            run_id=record.get("run_id"),
        ))


def _ai_to_dict(a: AiInteraction) -> dict:
    return {
        "id": a.id, "created_at": _iso(a.created_at), "owner_id": a.owner_id,
        "model": a.model, "prompt": a.prompt, "ai_config": a.ai_config,
        "final_config": a.final_config, "config_diff": a.config_diff,
        "outcome": a.outcome, "outcome_source": a.outcome_source,
        "run_id": a.run_id,
    }


def get_ai_interaction(interaction_id: str) -> dict | None:
    with session_scope() as s:
        a = s.get(AiInteraction, interaction_id)
        return _ai_to_dict(a) if a else None


def latest_pending_ai_interaction(owner_id: str | None,
                                  within_s: int = 3600) -> dict | None:
    """The caller's newest turn that nothing has been attributed to yet.

    Used to link a run back to the prompt that produced its config WITHOUT the UI
    having to cooperate. Bounded in time on purpose: attributing a run to a prompt
    from yesterday is a guess dressed as a record, and anything linked this way is
    marked `outcome_source="inferred"`.
    """
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=within_s)
    with session_scope() as s:
        row = s.execute(
            select(AiInteraction)
            .where(AiInteraction.owner_id == owner_id)
            .where(AiInteraction.run_id.is_(None))
            .where(AiInteraction.created_at >= cutoff)
            .order_by(AiInteraction.created_at.desc())
            .limit(1)
        ).scalars().first()
        return _ai_to_dict(row) if row else None


def load_ai_interactions(limit: int = 500, owner_id: str | None = None) -> list[dict]:
    """Newest first. This is the training/few-shot pool."""
    with session_scope() as s:
        q = select(AiInteraction).order_by(AiInteraction.created_at.desc()).limit(
            max(1, min(5000, limit)))
        if owner_id:
            q = q.where(AiInteraction.owner_id == owner_id)
        return [_ai_to_dict(a) for a in s.execute(q).scalars().all()]
