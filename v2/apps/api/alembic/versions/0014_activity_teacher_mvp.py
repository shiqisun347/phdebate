"""Add classroom activity planning and bulk room provisioning.

Revision ID: 0014_activity_teacher_mvp
Revises: 0013_classroom_foundation
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_activity_teacher_mvp"
down_revision = "0013_classroom_foundation"
branch_labels = None
depends_on = None


def _timestamp_columns() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def _column_names(table_name: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    # 0001 imports current metadata. The guards preserve a real 0013 -> 0014
    # upgrade while keeping new-database upgrades repeatable.
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "activities" not in tables:
        op.create_table(
            "activities",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("classroom_id", sa.String(length=36), nullable=False),
            sa.Column("competition_id", sa.String(length=36), nullable=False),
            sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("topic", sa.String(length=300), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="draft"),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("seat_count", sa.Integer(), nullable=False),
            sa.Column("seat_keys_snapshot", sa.JSON(), nullable=False, server_default="[]"),
            *_timestamp_columns(),
            sa.ForeignKeyConstraint(["classroom_id"], ["classrooms.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["competition_id"], ["competitions.id"]),
            sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_activities_classroom_id", "activities", ["classroom_id"], unique=False)
        op.create_index("ix_activities_competition_id", "activities", ["competition_id"], unique=False)
        op.create_index("ix_activities_created_by_user_id", "activities", ["created_by_user_id"], unique=False)
        op.create_index("ix_activities_status", "activities", ["status"], unique=False)

    if "activity_participants" not in tables:
        op.create_table(
            "activity_participants",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("activity_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("group_no", sa.Integer(), nullable=True),
            sa.Column("seat_key", sa.String(length=24), nullable=True),
            *_timestamp_columns(),
            sa.CheckConstraint("group_no IS NULL OR group_no > 0", name="ck_activity_participant_group_positive"),
            sa.ForeignKeyConstraint(["activity_id"], ["activities.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("activity_id", "group_no", "seat_key", name="uq_activity_group_seat"),
            sa.UniqueConstraint("activity_id", "user_id", name="uq_activity_participant_user"),
        )
        op.create_index("ix_activity_participants_activity_id", "activity_participants", ["activity_id"], unique=False)
        op.create_index("ix_activity_participants_user_id", "activity_participants", ["user_id"], unique=False)
        op.create_index("ix_activity_participants_group_no", "activity_participants", ["group_no"], unique=False)

    if "activity_operations" not in tables:
        op.create_table(
            "activity_operations",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("activity_id", sa.String(length=36), nullable=False),
            sa.Column("operation_kind", sa.String(length=32), nullable=False),
            sa.Column("idempotency_key", sa.String(length=128), nullable=False),
            sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("response", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["activity_id"], ["activities.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("activity_id", "operation_kind", "idempotency_key", name="uq_activity_operation_key"),
        )
        op.create_index("ix_activity_operations_activity_id", "activity_operations", ["activity_id"], unique=False)

    room_columns = _column_names("rooms")
    room_unique_constraints = {item["name"] for item in sa.inspect(op.get_bind()).get_unique_constraints("rooms")}
    with op.batch_alter_table("rooms") as batch:
        if "activity_id" not in room_columns:
            batch.add_column(sa.Column("activity_id", sa.String(length=36), nullable=True))
            batch.create_foreign_key("fk_rooms_activity_id", "activities", ["activity_id"], ["id"], ondelete="SET NULL")
            batch.create_index("ix_rooms_activity_id", ["activity_id"], unique=False)
        if "activity_group_no" not in room_columns:
            batch.add_column(sa.Column("activity_group_no", sa.Integer(), nullable=True))
        if "created_by_user_id" not in room_columns:
            batch.add_column(sa.Column("created_by_user_id", sa.String(length=36), nullable=True))
            batch.create_foreign_key("fk_rooms_created_by_user_id", "users", ["created_by_user_id"], ["id"])
            batch.create_index("ix_rooms_created_by_user_id", ["created_by_user_id"], unique=False)
        if "uq_rooms_activity_group" not in room_unique_constraints:
            batch.create_unique_constraint("uq_rooms_activity_group", ["activity_id", "activity_group_no"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "rooms" in tables:
        room_columns = _column_names("rooms")
        with op.batch_alter_table("rooms") as batch:
            batch.drop_constraint("uq_rooms_activity_group", type_="unique")
            if "created_by_user_id" in room_columns:
                batch.drop_index("ix_rooms_created_by_user_id")
                batch.drop_constraint("fk_rooms_created_by_user_id", type_="foreignkey")
                batch.drop_column("created_by_user_id")
            if "activity_group_no" in room_columns:
                batch.drop_column("activity_group_no")
            if "activity_id" in room_columns:
                batch.drop_index("ix_rooms_activity_id")
                batch.drop_constraint("fk_rooms_activity_id", type_="foreignkey")
                batch.drop_column("activity_id")
    for table_name in ("activity_operations", "activity_participants", "activities"):
        if table_name in tables:
            op.drop_table(table_name)
