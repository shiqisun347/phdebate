"""Normalize legacy AI-substituted seats in rooms that are not finished.

Revision ID: 0033_remove_live_ai_substitution
Revises: 0032_voice_telemetry

The live match policy no longer replaces a human participant with an AI.  A
few rooms created by the retired policy can still be paused with
``occupant_type = 'ai_substitute'``.  Keep terminal matches readable as
historical data, but restore the authoritative identity in active rooms so a
future engine tick can only wait for the human and/or pause the room.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_remove_live_ai_substitution"
down_revision: str | None = "0032_voice_telemetry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "room_seats" not in tables or "rooms" not in tables:
        return

    rooms = sa.table("rooms", sa.column("id", sa.String()), sa.column("status", sa.String()))
    seats = sa.table(
        "room_seats",
        sa.column("id", sa.String()),
        sa.column("room_id", sa.String()),
        sa.column("occupant_type", sa.String()),
        sa.column("display_name", sa.String()),
        sa.column("connected", sa.Boolean()),
        sa.column("disconnected_at", sa.DateTime(timezone=True)),
        sa.column("agent_profile_id", sa.String()),
    )
    active_statuses = ("lobby", "preparing", "running", "paused", "judging")
    active_room_ids = sa.select(rooms.c.id).where(rooms.c.status.in_(active_statuses))
    # Do not rewrite completed/terminated/review-required records: those are
    # historical snapshots and must remain auditable exactly as recorded.
    op.execute(
        sa.update(seats)
        .where(
            seats.c.room_id.in_(active_room_ids),
            seats.c.occupant_type == "ai_substitute",
        )
        .values(
            occupant_type="human",
            # Older rows prefixed the real name with this display-only label.
            display_name=sa.func.replace(seats.c.display_name, "AI 接替·", ""),
            connected=False,
            disconnected_at=sa.func.coalesce(seats.c.disconnected_at, sa.func.now()),
            agent_profile_id=None,
        )
    )

    if "seat_restore_requests" in tables:
        requests = sa.table(
            "seat_restore_requests",
            sa.column("seat_id", sa.String()),
            sa.column("status", sa.String()),
            sa.column("resolution_reason", sa.String()),
            sa.column("resolved_at", sa.DateTime(timezone=True)),
        )
        normalized_seats = sa.select(seats.c.id).where(
            seats.c.room_id.in_(active_room_ids),
            seats.c.occupant_type == "human",
        )
        # Expire obsolete restoration requests. They represented the removed
        # takeover flow and must not reintroduce it after a restart.
        op.execute(
            sa.update(requests)
            .where(
                requests.c.status == "pending",
                requests.c.seat_id.in_(normalized_seats),
            )
            .values(
                status="expired",
                resolution_reason="真人席位已恢复为新规则，系统不再支持 AI 接管或恢复申请。",
                resolved_at=sa.func.now(),
            )
        )


def downgrade() -> None:
    # Deliberately irreversible: recreating an AI substitution would violate
    # the current match safety policy and could cause an active room to resume
    # with the wrong speaker identity.
    pass
