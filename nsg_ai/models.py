"""
The persistent model, derived from the records the platform already writes.

The shapes here are not a redesign: `nsg_ai/sim_runs/*.json` and
`nsg_ai/campaigns/*.json` already carry exactly these fields, and a campaign's
`runs[]` list is already a join table between a campaign and the runs it
launched. Naming them as tables mostly makes explicit what the JSON implied.

Three decisions worth stating, because they are not obvious from the JSON:

`runs.config` is a SNAPSHOT, not a reference. A run must record the
configuration it actually executed, even if the scenario it came from is edited
or deleted afterwards — otherwise a result cannot be reproduced or defended.
`scenario_id` records provenance only, and is nullable because plenty of runs
never came from a saved scenario.

Frames live in their own table, one row per indication. Every analysis in the
conflict study read whole run files off disk to answer questions about a few
ticks; at thirty runs that is merely wasteful, and it does not survive a few
thousand. Keyed on (run_id, tick) it becomes a range scan.

Identifiers stay strings. The application already mints short hex ids and they
appear in URLs and on disk; swapping to UUIDs would churn code for no gain.

Timestamps are real timestamps rather than the ISO strings in the JSON. The
whole point of moving off files is being able to ask "what ran this week", and
that needs a date the database understands. Conversion happens at the API
boundary, not here.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer,
                        String, Text, UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# JSONB on Postgres, plain JSON anywhere else, so the models stay usable against
# SQLite in a test without a second set of definitions.
JsonB = JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    pass


class Scenario(Base):
    """A saved configuration. Until now these lived only in browser localStorage,
    which meant they could not be shared, survived only on one machine, and kept
    stale shapes indefinitely as the config schema evolved."""

    __tablename__ = "scenarios"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict] = mapped_column(JsonB)

    # Provenance, so the question the demo raised — do the AI's configurations
    # actually run, and which of the ~165 fields does anyone ever touch — becomes
    # a query rather than a guess. `changed_fields` is filled by diffing against
    # the schema defaults; nullable because that pass is not written yet.
    source: Mapped[str | None] = mapped_column(String(16))       # ai | manual | preset
    changed_fields: Mapped[dict | None] = mapped_column(JsonB)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
    # Nullable throughout until authentication exists. Adding the column now
    # costs nothing; adding it later would be a migration over live data.
    owner_id: Mapped[str | None] = mapped_column(String(64), index=True)

    runs: Mapped[list["Run"]] = relationship(back_populates="scenario")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scenario_id: Mapped[str | None] = mapped_column(
        ForeignKey("scenarios.id", ondelete="SET NULL"), index=True)

    config: Mapped[dict] = mapped_column(JsonB)          # snapshot, see module docstring
    requested: Mapped[dict | None] = mapped_column(JsonB)
    status: Mapped[str] = mapped_column(String(24), index=True)
    # Which engine path produced this: "live" (the streaming /sim/run path, one
    # row per KPM indication in `frames`) or "batch" (/ric/validate-nsoran, whose
    # whole result is an aggregate and lands in `summary` with no frames).
    # A column rather than a key inside `summary`, because the listing has to be
    # able to tell them apart without loading every run.
    kind: Mapped[str | None] = mapped_column(String(16), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    n_frames: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[dict | None] = mapped_column(JsonB)
    status_lines: Mapped[list | None] = mapped_column(JsonB)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner_id: Mapped[str | None] = mapped_column(String(64), index=True)

    scenario: Mapped["Scenario | None"] = relationship(back_populates="runs")
    frames: Mapped[list["RunFrame"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True)


class RunFrame(Base):
    """One KPM indication. The payload keeps the engine's own shape rather than
    being flattened into columns: the measurement set varies with what the engine
    reported that day, and a fixed column list would silently drop whatever is
    new. Aggregates that matter are extracted into `runs.summary` at write time."""

    __tablename__ = "run_frames"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    tick: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict] = mapped_column(JsonB)

    run: Mapped["Run"] = relationship(back_populates="frames")


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), index=True)
    verdict: Mapped[str | None] = mapped_column(String(24))
    base_seed: Mapped[int | None] = mapped_column(Integer)
    thresholds: Mapped[list | None] = mapped_column(JsonB)
    # The plan the campaign was started with: which scenario at which seed, and
    # how many frames each. Held only in memory until now, so a backend restart
    # lost it and the console crashed reading `planned.length` on a stored
    # campaign. It is part of the record — "4 of 10 runs" and the pending list
    # are both unanswerable without it.
    planned: Mapped[list | None] = mapped_column(JsonB)
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner_id: Mapped[str | None] = mapped_column(String(64), index=True)

    items: Mapped[list["CampaignRun"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan",
        passive_deletes=True, order_by="CampaignRun.position")


class CampaignRun(Base):
    """One planned or executed run inside a campaign.

    A queued run is a row whose `run_id` is still null — which is what the file
    format already did with its `planned[]` list, and why the two are one table
    here. It also removes the startup pass that had to notice interrupted
    campaigns and correct their rows: state lives in one place and is read on
    demand rather than rebuilt from disk."""

    __tablename__ = "campaign_runs"
    __table_args__ = (UniqueConstraint("campaign_id", "position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)     # execution order

    label: Mapped[str] = mapped_column(String(200))
    item_label: Mapped[str | None] = mapped_column(String(200))
    repeat: Mapped[int | None] = mapped_column(Integer)
    seed: Mapped[int | None] = mapped_column(Integer)
    # The scenario THIS run executed, seed included. Written by the runner from
    # the start and dropped by save_campaign until 2026-09-14, so it lived only
    # as long as the process — which is why comparing two campaigns could report
    # what moved but never which parameter moved it.
    config: Mapped[dict | None] = mapped_column(JsonB)

    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    verdict: Mapped[str | None] = mapped_column(String(24))
    violations: Mapped[list | None] = mapped_column(JsonB)
    summary: Mapped[dict | None] = mapped_column(JsonB)
    error: Mapped[str | None] = mapped_column(Text)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    campaign: Mapped["Campaign"] = relationship(back_populates="items")


class User(Base):
    """A login. Local to this box's Postgres — see nsg_ai/auth.py for why the
    identities do not live in a cloud provider.

    The columns are named for their OIDC equivalents where one exists
    (`username` -> preferred_username, `role` -> a realm role) so that migrating
    to a self-hosted IdP later maps a claim to a column rather than a rewrite.
    `password_hash` is bcrypt; it is the one thing an OIDC deployment drops,
    because the IdP owns the credential then.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(16), default="user")   # admin | user
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AiInteraction(Base):
    """One turn of the scenario generator: what it was asked, what it built, what
    the user kept, and what that measured.

    Collected because a correction is the only signal that says where the model is
    wrong, and nothing was recording it. Two fields do the work that a pair of
    config blobs cannot:

    `config_diff` makes "the AI config and the edit differ a lot" a QUERY rather
    than a worry — which fields get corrected, how often. That list improves the
    prompt long before there is enough data to train anything.

    `outcome` records what the edit MEANT. A diff cannot tell "the AI was wrong"
    from "I changed my mind about what I wanted", and a dataset that conflates the
    two teaches the model to imitate indecision.
    """

    __tablename__ = "ai_interactions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    owner_id: Mapped[str | None] = mapped_column(String(64), index=True)
    model: Mapped[str | None] = mapped_column(String(120))

    prompt: Mapped[str | None] = mapped_column(Text)
    ai_config: Mapped[dict | None] = mapped_column(JsonB)
    final_config: Mapped[dict | None] = mapped_column(JsonB)
    config_diff: Mapped[dict | None] = mapped_column(JsonB)

    # pending | accepted | edited | discarded
    outcome: Mapped[str | None] = mapped_column(String(16), index=True)
    # explicit (the UI said so) | inferred (we linked it to a run) — never hide
    # which, or an inference reads as a statement of fact.
    outcome_source: Mapped[str | None] = mapped_column(String(16))
    # The run that measured the kept config: prompt -> config -> MEASURED RESULT.
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
