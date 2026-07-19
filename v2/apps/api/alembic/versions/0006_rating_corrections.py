"""Allow append-only rating corrections.

Revision ID: 0006_rating_corrections
Revises: 0005_room_creation_idempotency
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_rating_corrections"
down_revision = "0005_room_creation_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "source" in {item["name"] for item in sa.inspect(op.get_bind()).get_columns("rating_changes")}:
        return
    op.add_column(
        "rating_changes",
        sa.Column("source", sa.String(length=24), nullable=False, server_default="initial"),
    )
    op.add_column("rating_changes", sa.Column("idempotency_key", sa.String(length=120), nullable=True))
    op.drop_constraint("uq_rating_change_match_user", "rating_changes", type_="unique")
    op.create_index(
        "uq_rating_change_initial",
        "rating_changes",
        ["match_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("source = 'initial'"),
    )
    op.create_index(
        "uq_rating_change_idempotency",
        "rating_changes",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    # Multiple correction rows cannot satisfy the former one-row-per-match
    # constraint. Keep only the initial settlement when explicitly rolling back.
    op.execute("DELETE FROM rating_changes WHERE source <> 'initial'")
    op.drop_index("uq_rating_change_idempotency", table_name="rating_changes")
    op.drop_index("uq_rating_change_initial", table_name="rating_changes")
    op.create_unique_constraint(
        "uq_rating_change_match_user",
        "rating_changes",
        ["match_id", "user_id"],
    )
    op.drop_column("rating_changes", "idempotency_key")
    op.drop_column("rating_changes", "source")
