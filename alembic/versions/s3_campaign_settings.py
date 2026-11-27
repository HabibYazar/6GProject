"""Persist campaign execution settings.

Revision ID: s3_campaign_settings
Revises: e5f6a7b8c9d0
"""

from alembic import op
import sqlalchemy as sa

revision = "s3_campaign_settings"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade():
    # Nullable: legacy campaigns have no trustworthy settings history.
    op.add_column(
        "campaigns",
        sa.Column("tier", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "campaigns",
        sa.Column("partition_groups", sa.Integer(), nullable=True),
    )
    op.add_column(
        "campaigns",
        sa.Column("wall_cap_s", sa.Float(), nullable=True),
    )


def downgrade():
    op.drop_column("campaigns", "wall_cap_s")
    op.drop_column("campaigns", "partition_groups")
    op.drop_column("campaigns", "tier")
