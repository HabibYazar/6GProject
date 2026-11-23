"""ai_interactions — prompt, what the AI built, what the user kept, what it measured

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-15

Nothing recorded what the generator was asked for or what happened to its answer,
so every correction a user made was thrown away — the one signal that says where
the model is wrong.

Two columns carry the weight. `config_diff` turns "the AI config and the edit
differ a lot" from a worry into a QUERY: which fields get corrected, how often,
by how much. And `outcome` records what the edit MEANT, because the diff alone
cannot tell "the AI was wrong" from "I changed my mind" — without it the dataset
is noise wearing a label.

`run_id` closes the chain prompt -> config -> MEASURED RESULT, which is the part
that makes the data worth more than preference pairs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_interactions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), index=True),
        sa.Column("owner_id", sa.String(length=64), index=True),
        sa.Column("model", sa.String(length=120)),
        sa.Column("prompt", sa.Text()),
        sa.Column("ai_config", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("final_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("config_diff", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("outcome", sa.String(length=16), index=True),
        sa.Column("outcome_source", sa.String(length=16), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True, index=True),
    )


def downgrade() -> None:
    op.drop_table("ai_interactions")
