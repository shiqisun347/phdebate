"""Retire the former human-seat AI takeover state.

Revision ID: 0034_retire_ai_takeover
Revises: 0033_remove_live_ai_substitution

The application no longer exposes takeover or restoration workflows. Preserve
the append-only match event history, but normalize every current seat back to
its real human identity and close any obsolete pending restoration request.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_retire_ai_takeover"
down_revision: str | None = "0033_remove_live_ai_substitution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "room_seats" in tables:
        seats = sa.table(
            "room_seats",
            sa.column("occupant_type", sa.String()),
            sa.column("user_id", sa.String()),
            sa.column("display_name", sa.String()),
            sa.column("connected", sa.Boolean()),
            sa.column("disconnected_at", sa.DateTime(timezone=True)),
            sa.column("agent_profile_id", sa.String()),
        )
        op.execute(
            sa.update(seats)
            .where(seats.c.occupant_type == "ai_substitute")
            .values(
                occupant_type=sa.case(
                    (seats.c.user_id.is_not(None), "human"),
                    else_="open",
                ),
                display_name=sa.case(
                    (seats.c.user_id.is_not(None), sa.func.replace(seats.c.display_name, "AI 接替·", "")),
                    else_="待加入",
                ),
                connected=False,
                disconnected_at=sa.case(
                    (seats.c.user_id.is_not(None), sa.func.coalesce(seats.c.disconnected_at, sa.func.now())),
                    else_=None,
                ),
                agent_profile_id=None,
            )
        )

    if "seat_restore_requests" in tables:
        requests = sa.table(
            "seat_restore_requests",
            sa.column("status", sa.String()),
            sa.column("resolution_reason", sa.String()),
            sa.column("resolved_at", sa.DateTime(timezone=True)),
        )
        op.execute(
            sa.update(requests)
            .where(requests.c.status == "pending")
            .values(
                status="expired",
                resolution_reason="席位接管机制已永久下线；真人断线满 60 秒后整场比赛自动暂停。",
                resolved_at=sa.func.now(),
            )
        )


def downgrade() -> None:
    # Intentionally irreversible: restoring takeover state could grant an AI
    # control of a real participant's locked seat.
    pass
