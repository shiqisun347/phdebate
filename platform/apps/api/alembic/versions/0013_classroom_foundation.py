"""Add tenant-scoped organization and classroom membership foundations.

Revision ID: 0013_classroom_foundation
Revises: 0012_agent_gateway_secret
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_classroom_foundation"
down_revision = "0012_agent_gateway_secret"
branch_labels = None
depends_on = None


def _timestamp_columns() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def upgrade() -> None:
    # 0001 historically builds Base.metadata, so a brand-new database can
    # already contain later tables before their explicit migration is reached.
    # Production databases at 0012 do not; these guards support both paths.
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "organizations" not in tables:
        op.create_table(
            "organizations",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("slug", sa.String(length=80), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            *_timestamp_columns(),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_organizations_slug", "organizations", ["slug"], unique=True)
        op.create_index("ix_organizations_is_active", "organizations", ["is_active"], unique=False)

    if "organization_memberships" not in tables:
        op.create_table(
            "organization_memberships",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("organization_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("role", sa.String(length=24), nullable=False, server_default="member"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            *_timestamp_columns(),
            sa.CheckConstraint("role IN ('owner', 'admin', 'member')", name="ck_organization_membership_role"),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("organization_id", "user_id", name="uq_organization_membership_user"),
        )
        op.create_index(
            "ix_organization_memberships_organization_id",
            "organization_memberships",
            ["organization_id"],
            unique=False,
        )
        op.create_index("ix_organization_memberships_user_id", "organization_memberships", ["user_id"], unique=False)
        op.create_index("ix_organization_memberships_role", "organization_memberships", ["role"], unique=False)
        op.create_index("ix_organization_memberships_is_active", "organization_memberships", ["is_active"], unique=False)

    if "classrooms" not in tables:
        op.create_table(
            "classrooms",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("organization_id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("slug", sa.String(length=80), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            *_timestamp_columns(),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("organization_id", "slug", name="uq_classroom_organization_slug"),
        )
        op.create_index("ix_classrooms_organization_id", "classrooms", ["organization_id"], unique=False)
        op.create_index("ix_classrooms_is_active", "classrooms", ["is_active"], unique=False)

    if "classroom_memberships" not in tables:
        op.create_table(
            "classroom_memberships",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("classroom_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("role", sa.String(length=24), nullable=False, server_default="student"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            *_timestamp_columns(),
            sa.CheckConstraint("role IN ('teacher', 'student')", name="ck_classroom_membership_role"),
            sa.ForeignKeyConstraint(["classroom_id"], ["classrooms.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("classroom_id", "user_id", name="uq_classroom_membership_user"),
        )
        op.create_index("ix_classroom_memberships_classroom_id", "classroom_memberships", ["classroom_id"], unique=False)
        op.create_index("ix_classroom_memberships_user_id", "classroom_memberships", ["user_id"], unique=False)
        op.create_index("ix_classroom_memberships_role", "classroom_memberships", ["role"], unique=False)
        op.create_index("ix_classroom_memberships_is_active", "classroom_memberships", ["is_active"], unique=False)


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table_name in ("classroom_memberships", "classrooms", "organization_memberships", "organizations"):
        if table_name in tables:
            op.drop_table(table_name)
