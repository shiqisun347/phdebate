"""Separate remote Agent persona keys from legacy model and endpoint fields.

Revision ID: 0011_agent_gateway_profiles
Revises: 0010_room_code_reservations
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_agent_gateway_profiles"
down_revision = "0010_room_code_reservations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "profile_key" in {item["name"] for item in sa.inspect(op.get_bind()).get_columns("agent_profiles")}:
        return
    op.add_column("agent_profiles", sa.Column("profile_key", sa.String(length=100), nullable=True))
    connection = op.get_bind()
    profile_ids = connection.execute(sa.text("SELECT id FROM agent_profiles ORDER BY created_at, id")).scalars().all()
    for index, profile_id in enumerate(profile_ids, start=1):
        connection.execute(
            sa.text("UPDATE agent_profiles SET profile_key = :profile_key WHERE id = :profile_id"),
            {"profile_key": f"debater-{index}", "profile_id": profile_id},
        )
    with op.batch_alter_table("agent_profiles") as batch:
        batch.alter_column("profile_key", nullable=False)
    op.create_index("ix_agent_profiles_profile_key", "agent_profiles", ["profile_key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_agent_profiles_profile_key", table_name="agent_profiles")
    with op.batch_alter_table("agent_profiles") as batch:
        batch.drop_column("profile_key")
