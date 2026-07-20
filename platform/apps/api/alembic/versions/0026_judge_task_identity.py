"""Persist the authoritative automatic-judge task identity.

Revision ID: 0026_judge_task_identity
Revises: 0025_speech_result_pagination
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_judge_task_identity"
down_revision: str | None = "0025_speech_result_pagination"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("judge_scorecards")}
    if "task_id" in columns:
        return
    op.add_column(
        "judge_scorecards",
        sa.Column("task_id", sa.String(length=36), nullable=False, server_default=""),
    )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("judge_scorecards")}
    if "task_id" not in columns:
        return
    op.drop_column("judge_scorecards", "task_id")
