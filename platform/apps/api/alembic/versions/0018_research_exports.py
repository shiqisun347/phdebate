"""Add scoped asynchronous research dataset exports.

Revision ID: 0018_research_exports
Revises: 0017_retain_activity_room_scope
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_research_exports"
down_revision = "0017_retain_activity_room_scope"
branch_labels = None
depends_on = None


def _timestamp_columns() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def _organization_role_supports_researcher() -> bool:
    checks = sa.inspect(op.get_bind()).get_check_constraints("organization_memberships")
    role_check = next((item for item in checks if item["name"] == "ck_organization_membership_role"), None)
    return bool(role_check and "researcher" in (role_check.get("sqltext") or ""))


def upgrade() -> None:
    if not _organization_role_supports_researcher():
        with op.batch_alter_table("organization_memberships") as batch:
            batch.drop_constraint("ck_organization_membership_role", type_="check")
            batch.create_check_constraint(
                "ck_organization_membership_role",
                "role IN ('owner', 'admin', 'member', 'researcher')",
            )

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "research_subject_identities" not in tables:
        op.create_table(
            "research_subject_identities",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("organization_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("pseudonym", sa.String(length=40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("organization_id", "pseudonym", name="uq_research_subject_identity_pseudonym"),
            sa.UniqueConstraint("organization_id", "user_id", name="uq_research_subject_identity_user"),
        )
        op.create_index(
            "ix_research_subject_identities_organization_id",
            "research_subject_identities",
            ["organization_id"],
            unique=False,
        )
        op.create_index("ix_research_subject_identities_user_id", "research_subject_identities", ["user_id"], unique=False)

    if "research_export_jobs" not in tables:
        op.create_table(
            "research_export_jobs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("organization_id", sa.String(length=36), nullable=False),
            sa.Column("requested_by_user_id", sa.String(length=36), nullable=False),
            sa.Column("idempotency_key", sa.String(length=128), nullable=False),
            sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="queued"),
            sa.Column("authorization_mode", sa.String(length=32), nullable=False),
            sa.Column("purpose", sa.Text(), nullable=False),
            sa.Column("break_glass_reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("filter_snapshot", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("authorization_snapshot", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("consent_snapshot", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("matched_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("excluded_consent_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("artifact_name", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("artifact_sha256", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("artifact_size_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error_code", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            *_timestamp_columns(),
            sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'failed')", name="ck_research_export_job_status"),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("requested_by_user_id", "idempotency_key", name="uq_research_export_job_request_key"),
        )
        op.create_index("ix_research_export_jobs_organization_id", "research_export_jobs", ["organization_id"], unique=False)
        op.create_index(
            "ix_research_export_jobs_requested_by_user_id",
            "research_export_jobs",
            ["requested_by_user_id"],
            unique=False,
        )
        op.create_index("ix_research_export_jobs_status", "research_export_jobs", ["status"], unique=False)
        op.create_index(
            "ix_research_export_jobs_org_created",
            "research_export_jobs",
            ["organization_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "research_export_jobs" in tables:
        op.drop_table("research_export_jobs")
    if "research_subject_identities" in tables:
        op.drop_table("research_subject_identities")
    if _organization_role_supports_researcher():
        op.execute("UPDATE organization_memberships SET role = 'member' WHERE role = 'researcher'")
        with op.batch_alter_table("organization_memberships") as batch:
            batch.drop_constraint("ck_organization_membership_role", type_="check")
            batch.create_check_constraint(
                "ck_organization_membership_role",
                "role IN ('owner', 'admin', 'member')",
            )
