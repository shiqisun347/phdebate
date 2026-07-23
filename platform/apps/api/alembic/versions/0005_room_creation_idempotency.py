"""Add room creation idempotency metadata.

Revision ID: 0005_room_creation_idempotency
Revises: 0004_speech_playback_state
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_room_creation_idempotency"
down_revision = "0004_speech_playback_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "creation_key" in {item["name"] for item in sa.inspect(op.get_bind()).get_columns("rooms")}:
        return
    op.add_column("rooms", sa.Column("creation_key", sa.String(length=96), nullable=True))
    op.add_column("rooms", sa.Column("creation_fingerprint", sa.String(length=64), nullable=True))
    op.create_index("uq_rooms_creation_key", "rooms", ["creation_key"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_rooms_creation_key", table_name="rooms")
    op.drop_column("rooms", "creation_fingerprint")
    op.drop_column("rooms", "creation_key")
