"""Separate QA identities and rooms from production data.

Revision ID: 0020_test_data_scope
Revises: 0019_audio_streaming
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_test_data_scope"
down_revision = "0019_audio_streaming"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    room_columns = {column["name"] for column in inspector.get_columns("rooms")}
    with op.batch_alter_table("users") as batch:
        if "is_test_account" not in user_columns:
            batch.add_column(sa.Column("is_test_account", sa.Boolean(), nullable=False, server_default=sa.false()))
            batch.create_index("ix_users_is_test_account", ["is_test_account"], unique=False)
    with op.batch_alter_table("rooms") as batch:
        if "is_test_data" not in room_columns:
            batch.add_column(sa.Column("is_test_data", sa.Boolean(), nullable=False, server_default=sa.false()))
            batch.create_index("ix_rooms_is_test_data", ["is_test_data"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    room_columns = {column["name"] for column in inspector.get_columns("rooms")}
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    with op.batch_alter_table("rooms") as batch:
        if "is_test_data" in room_columns:
            batch.drop_index("ix_rooms_is_test_data")
            batch.drop_column("is_test_data")
    with op.batch_alter_table("users") as batch:
        if "is_test_account" in user_columns:
            batch.drop_index("ix_users_is_test_account")
            batch.drop_column("is_test_account")
