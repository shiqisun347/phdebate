"""Freeze speech-service configuration per match.

Revision ID: 0008_service_snapshots
Revises: 0007_judge_profiles
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_service_snapshots"
down_revision = "0007_judge_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "service_snapshot" in {item["name"] for item in sa.inspect(op.get_bind()).get_columns("matches")}:
        return
    op.add_column("matches", sa.Column("service_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade() -> None:
    op.drop_column("matches", "service_snapshot")
