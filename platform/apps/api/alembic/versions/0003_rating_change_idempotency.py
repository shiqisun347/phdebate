"""Prevent duplicate rating changes for one participant and match.

Revision ID: 0003_rating_change_idempotency
Revises: 0002_active_speech_guard
"""

from alembic import op
from sqlalchemy import inspect

revision = "0003_rating_change_idempotency"
down_revision = "0002_active_speech_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "source" in {item["name"] for item in inspect(op.get_bind()).get_columns("rating_changes")}:
        return
    op.create_unique_constraint(
        "uq_rating_change_match_user",
        "rating_changes",
        ["match_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_rating_change_match_user", "rating_changes", type_="unique")
