"""Add the keyset pagination index for match speeches.

Revision ID: 0025_speech_result_pagination
Revises: 0024_seat_restore_requests
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_speech_result_pagination"
down_revision = "0024_seat_restore_requests"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_speeches_match_created_id"


def _index_names() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "speeches" not in inspector.get_table_names():
        return set()
    return {str(item["name"]) for item in inspector.get_indexes("speeches")}


def upgrade() -> None:
    if INDEX_NAME in _index_names():
        return
    op.create_index(INDEX_NAME, "speeches", ["match_id", "created_at", "id"])


def downgrade() -> None:
    if INDEX_NAME not in _index_names():
        return
    op.drop_index(INDEX_NAME, table_name="speeches")
