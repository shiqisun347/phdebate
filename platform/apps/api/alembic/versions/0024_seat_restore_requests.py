"""Add participant-driven human seat restoration requests.

Revision ID: 0024_seat_restore_requests
Revises: 0023_competition_catalog_cleanup
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_seat_restore_requests"
down_revision = "0023_competition_catalog_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The original 0001 migration builds current metadata for brand-new test
    # databases, so later migrations must remain safe when the table is
    # already present there.
    if "seat_restore_requests" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "seat_restore_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("room_id", sa.String(length=36), nullable=False),
        sa.Column("seat_id", sa.String(length=36), nullable=False),
        sa.Column("requester_user_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("idempotency_key", sa.String(length=160), nullable=True),
        sa.Column("resolution_reason", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("resolved_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["requester_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resolved_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["seat_id"], ["room_seats.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_seat_restore_requests_room_id", "seat_restore_requests", ["room_id"])
    op.create_index("ix_seat_restore_requests_seat_id", "seat_restore_requests", ["seat_id"])
    op.create_index("ix_seat_restore_requests_requester_user_id", "seat_restore_requests", ["requester_user_id"])
    op.create_index("ix_seat_restore_requests_status", "seat_restore_requests", ["status"])
    op.create_index("ix_seat_restore_requests_resolved_by_user_id", "seat_restore_requests", ["resolved_by_user_id"])


def downgrade() -> None:
    if "seat_restore_requests" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_index("ix_seat_restore_requests_resolved_by_user_id", table_name="seat_restore_requests")
    op.drop_index("ix_seat_restore_requests_status", table_name="seat_restore_requests")
    op.drop_index("ix_seat_restore_requests_requester_user_id", table_name="seat_restore_requests")
    op.drop_index("ix_seat_restore_requests_seat_id", table_name="seat_restore_requests")
    op.drop_index("ix_seat_restore_requests_room_id", table_name="seat_restore_requests")
    op.drop_table("seat_restore_requests")
