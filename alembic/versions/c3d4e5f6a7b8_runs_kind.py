"""runs.kind — which engine path produced this run

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-14

A batch run (`/ric/validate-nsoran`) existed ONLY as its HTTP response: nothing
wrote it down, so closing the tab lost it and a four-hour run staked everything
on one connection. Batch runs are stored in `runs` now — the columns already fit,
with the engine's whole result in `summary` and no frames — which leaves the
listing unable to tell the two kinds apart. Hence this column rather than a flag
buried in the JSON: the discriminator has to be visible where runs are listed.

Existing rows are all from the live path, so they backfill to 'live'.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("kind", sa.String(length=16), nullable=True))
    op.execute("UPDATE runs SET kind = 'live' WHERE kind IS NULL")
    op.create_index("ix_runs_kind", "runs", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_runs_kind", table_name="runs")
    op.drop_column("runs", "kind")
