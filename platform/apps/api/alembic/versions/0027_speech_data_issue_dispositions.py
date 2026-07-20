"""Track human disposition of missing speech data artifacts.

Revision ID: 0027_speech_data_issue_dispositions
Revises: 0026_judge_task_identity
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_speech_data_issue_dispositions"
down_revision: str | None = "0026_judge_task_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "speech_data_issue_dispositions" in tables:
        return
    op.create_table(
        "speech_data_issue_dispositions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("speech_id", sa.String(length=36), nullable=False),
        sa.Column("issue_code", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("reviewed_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["speech_id"], ["speeches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("speech_id", "issue_code", name="uq_speech_data_issue"),
    )
    op.create_index(
        "ix_speech_data_issue_dispositions_speech_id",
        "speech_data_issue_dispositions",
        ["speech_id"],
        unique=False,
    )
    op.create_index(
        "ix_speech_data_issue_dispositions_status",
        "speech_data_issue_dispositions",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_speech_data_issue_dispositions_reviewed_by_user_id",
        "speech_data_issue_dispositions",
        ["reviewed_by_user_id"],
        unique=False,
    )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "speech_data_issue_dispositions" in tables:
        op.drop_table("speech_data_issue_dispositions")
