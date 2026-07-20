"""Align the primary competition name with the competition-only product.

Revision ID: 0023_competition_catalog_cleanup
Revises: 0022_remove_classroom_domain
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_competition_catalog_cleanup"
down_revision = "0022_remove_classroom_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    competition = sa.table(
        "competitions",
        sa.column("slug", sa.String()),
        sa.column("name", sa.String()),
    )
    op.execute(
        competition.update()
        .where(
            competition.c.slug == "daily-4v4",
            competition.c.name == "4v4 人机辩论日常赛",
        )
        .values(name="4v4 人机辩论正式赛")
    )


def downgrade() -> None:
    competition = sa.table(
        "competitions",
        sa.column("slug", sa.String()),
        sa.column("name", sa.String()),
    )
    op.execute(
        competition.update()
        .where(
            competition.c.slug == "daily-4v4",
            competition.c.name == "4v4 人机辩论正式赛",
        )
        .values(name="4v4 人机辩论日常赛")
    )
