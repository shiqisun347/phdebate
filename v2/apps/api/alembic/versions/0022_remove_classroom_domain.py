"""Remove the retired classroom and teaching-activity domain.

Revision ID: 0022_remove_classroom_domain
Revises: 0021_control_session_lease

This migration is intentionally destructive. Production deployment must take a
verified database backup first; rollback is performed by restoring that backup,
not by recreating empty classroom tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_remove_classroom_domain"
down_revision = "0021_control_session_lease"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name: str) -> set[str]:
    if table_name not in _tables():
        return set()
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _constraint_names(table_name: str, kind: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    getter = inspector.get_unique_constraints if kind == "unique" else inspector.get_foreign_keys
    return {item["name"] for item in getter(table_name) if item.get("name")}


def _index_names(table_name: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table_name) if item.get("name")}


def _remove_room_activity_scope() -> None:
    columns = _columns("rooms")
    if not {"activity_id", "activity_group_no"} & columns:
        return

    uniques = _constraint_names("rooms", "unique")
    foreign_keys = _constraint_names("rooms", "foreign")
    indexes = _index_names("rooms")
    with op.batch_alter_table("rooms") as batch:
        if "uq_rooms_activity_group" in uniques:
            batch.drop_constraint("uq_rooms_activity_group", type_="unique")
        if "fk_rooms_activity_id" in foreign_keys:
            batch.drop_constraint("fk_rooms_activity_id", type_="foreignkey")
        if "ix_rooms_activity_id" in indexes:
            batch.drop_index("ix_rooms_activity_id")
        if "activity_group_no" in columns:
            batch.drop_column("activity_group_no")
        if "activity_id" in columns:
            batch.drop_column("activity_id")


def _remove_recording_consent_snapshots() -> None:
    for table_name in ("speeches", "matches"):
        if "recording_consent_snapshot" in _columns(table_name):
            with op.batch_alter_table(table_name) as batch:
                batch.drop_column("recording_consent_snapshot")


def upgrade() -> None:
    _remove_room_activity_scope()
    _remove_recording_consent_snapshots()

    tables = _tables()
    for table_name in (
        "research_export_jobs",
        "research_subject_identities",
        "consent_records",
        "consent_policies",
        "activity_operations",
        "activity_participants",
        "activities",
        "classroom_memberships",
        "classrooms",
        "organization_memberships",
        "organizations",
    ):
        if table_name in tables:
            op.drop_table(table_name)
            tables.remove(table_name)


def downgrade() -> None:
    raise RuntimeError(
        "0022 removed classroom-domain data permanently; restore the pre-migration database backup instead."
    )
