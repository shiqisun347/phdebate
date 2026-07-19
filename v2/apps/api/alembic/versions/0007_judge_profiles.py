"""Freeze judge configuration per match and expose runtime settings.

Revision ID: 0007_judge_profiles
Revises: 0006_rating_corrections
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_judge_profiles"
down_revision = "0006_rating_corrections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "system_prompt" in {item["name"] for item in sa.inspect(op.get_bind()).get_columns("judge_profiles")}:
        return
    op.add_column("judge_profiles", sa.Column("system_prompt", sa.Text(), nullable=False, server_default=""))
    op.add_column("judge_profiles", sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="120"))
    op.add_column("matches", sa.Column("judge_profile_id", sa.String(length=36), nullable=True))
    op.add_column("matches", sa.Column("judge_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    op.create_foreign_key(
        "fk_matches_judge_profile_id",
        "matches",
        "judge_profiles",
        ["judge_profile_id"],
        ["id"],
    )
    op.create_index("ix_matches_judge_profile_id", "matches", ["judge_profile_id"], unique=False)
    op.create_index(
        "uq_judge_profiles_one_active",
        "judge_profiles",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index("uq_judge_profiles_one_active", table_name="judge_profiles")
    op.drop_index("ix_matches_judge_profile_id", table_name="matches")
    op.drop_constraint("fk_matches_judge_profile_id", "matches", type_="foreignkey")
    op.drop_column("matches", "judge_snapshot")
    op.drop_column("matches", "judge_profile_id")
    op.drop_column("judge_profiles", "timeout_seconds")
    op.drop_column("judge_profiles", "system_prompt")
