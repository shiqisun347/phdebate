"""Add versioned organization consent policies and audit records.

Revision ID: 0015_consent_privacy_mvp
Revises: 0014_activity_teacher_mvp
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_consent_privacy_mvp"
down_revision = "0014_activity_teacher_mvp"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    # 0001 imports current metadata on a fresh database. Guards keep that path
    # repeatable while preserving a real 0014 -> 0015 production-shaped upgrade.
    tables = _tables()
    if "consent_policies" not in tables:
        op.create_table(
            "consent_policies",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("organization_id", sa.String(length=36), nullable=False),
            sa.Column("policy_type", sa.String(length=32), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=160), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "policy_type IN ('recording', 'research_use', 'public_display')",
                name="ck_consent_policy_type",
            ),
            sa.CheckConstraint("version > 0", name="ck_consent_policy_version_positive"),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "id",
                "organization_id",
                "policy_type",
                "version",
                name="uq_consent_policy_identity_scope",
            ),
            sa.UniqueConstraint("organization_id", "policy_type", "version", name="uq_consent_policy_version"),
        )
        op.create_index("ix_consent_policies_organization_id", "consent_policies", ["organization_id"], unique=False)
        op.create_index("ix_consent_policies_policy_type", "consent_policies", ["policy_type"], unique=False)
        op.create_index("ix_consent_policies_is_active", "consent_policies", ["is_active"], unique=False)
        op.create_index("ix_consent_policies_effective_at", "consent_policies", ["effective_at"], unique=False)
        op.create_index("ix_consent_policies_created_by_user_id", "consent_policies", ["created_by_user_id"], unique=False)

    tables = _tables()
    if "consent_records" not in tables:
        op.create_table(
            "consent_records",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("organization_id", sa.String(length=36), nullable=False),
            sa.Column("policy_id", sa.String(length=36), nullable=False),
            sa.Column("policy_type", sa.String(length=32), nullable=False),
            sa.Column("policy_version", sa.Integer(), nullable=False),
            sa.Column("subject_user_id", sa.String(length=36), nullable=False),
            sa.Column("decision", sa.String(length=16), nullable=False),
            sa.Column("actor_user_id", sa.String(length=36), nullable=False),
            sa.Column("transition_key", sa.String(length=64), nullable=False),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "policy_type IN ('recording', 'research_use', 'public_display')",
                name="ck_consent_record_policy_type",
            ),
            sa.CheckConstraint("decision IN ('grant', 'revoke')", name="ck_consent_record_decision"),
            sa.CheckConstraint("policy_version > 0", name="ck_consent_record_policy_version_positive"),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(
                ["policy_id", "organization_id", "policy_type", "policy_version"],
                [
                    "consent_policies.id",
                    "consent_policies.organization_id",
                    "consent_policies.policy_type",
                    "consent_policies.version",
                ],
                name="fk_consent_record_policy_scope",
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(["subject_user_id"], ["users.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_consent_records_organization_id", "consent_records", ["organization_id"], unique=False)
        op.create_index("ix_consent_records_policy_id", "consent_records", ["policy_id"], unique=False)
        op.create_index("ix_consent_records_policy_type", "consent_records", ["policy_type"], unique=False)
        op.create_index("ix_consent_records_subject_user_id", "consent_records", ["subject_user_id"], unique=False)
        op.create_index("ix_consent_records_decision", "consent_records", ["decision"], unique=False)
        op.create_index("ix_consent_records_actor_user_id", "consent_records", ["actor_user_id"], unique=False)
        op.create_index("ix_consent_records_transition_key", "consent_records", ["transition_key"], unique=True)
        op.create_index("ix_consent_records_decided_at", "consent_records", ["decided_at"], unique=False)
        op.create_index(
            "ix_consent_records_subject_policy_time",
            "consent_records",
            ["subject_user_id", "policy_id", "decided_at"],
            unique=False,
        )


def downgrade() -> None:
    tables = _tables()
    if "consent_records" in tables:
        op.drop_table("consent_records")
    if "consent_policies" in tables:
        op.drop_table("consent_policies")
