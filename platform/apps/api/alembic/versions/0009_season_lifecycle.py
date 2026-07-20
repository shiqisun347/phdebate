"""Freeze room seasons and track leaderboard recency.

Revision ID: 0009_season_lifecycle
Revises: 0008_service_snapshots
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_season_lifecycle"
down_revision = "0008_service_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "season_id" in {item["name"] for item in sa.inspect(op.get_bind()).get_columns("rooms")}:
        return
    op.add_column("rooms", sa.Column("season_id", sa.String(length=36), nullable=True))
    op.create_foreign_key("fk_rooms_season_id", "rooms", "seasons", ["season_id"], ["id"])
    op.create_index("ix_rooms_season_id", "rooms", ["season_id"], unique=False)
    op.execute(
        """
        UPDATE rooms
        SET season_id = competitions.season_id
        FROM competitions
        WHERE rooms.competition_id = competitions.id
          AND rooms.season_id IS NULL
        """
    )
    op.add_column("leaderboard_entries", sa.Column("last_match_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        """
        UPDATE leaderboard_entries AS entries
        SET last_match_at = settlements.last_match_at
        FROM (
            SELECT
                matches.competition_id,
                matches.season_id,
                rating_changes.user_id,
                MAX(COALESCE(rooms.completed_at, matches.created_at)) AS last_match_at
            FROM rating_changes
            JOIN matches ON matches.id = rating_changes.match_id
            JOIN rooms ON rooms.id = matches.room_id
            WHERE rating_changes.source = 'initial'
            GROUP BY matches.competition_id, matches.season_id, rating_changes.user_id
        ) AS settlements
        WHERE entries.competition_id = settlements.competition_id
          AND entries.user_id = settlements.user_id
          AND entries.season_id IS NOT DISTINCT FROM settlements.season_id
        """
    )


def downgrade() -> None:
    op.drop_column("leaderboard_entries", "last_match_at")
    op.drop_index("ix_rooms_season_id", table_name="rooms")
    op.drop_constraint("fk_rooms_season_id", "rooms", type_="foreignkey")
    op.drop_column("rooms", "season_id")
