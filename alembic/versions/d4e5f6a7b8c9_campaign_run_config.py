"""campaign_runs.config — the scenario each run actually executed

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-14

`_campaign_run_one` has been writing the exact config into each run entry, with
a comment saying why ("a result whose scenario cannot be identified is an
anecdote"), and `save_campaign` dropped it on the floor — every other field of
the entry was persisted and this one was not. So it survived in memory for the
length of a session and was gone after a restart, which is also why comparing
two campaigns could never say WHICH PARAMETER differed: the answer was being
computed and thrown away.

Existing rows have no config and stay null; the comparison reports that as
"not recorded" rather than as "no differences".
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("campaign_runs",
                  sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("campaign_runs", "config")
