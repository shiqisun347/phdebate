"""Store the RESTful Agent gateway shared secret as encrypted ciphertext.

Revision ID: 0012_agent_gateway_secret
Revises: 0011_agent_gateway_profiles
"""

import sqlalchemy as sa
from alembic import op

revision = "0012_agent_gateway_secret"
down_revision = "0011_agent_gateway_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "secret_ciphertext" in {item["name"] for item in sa.inspect(op.get_bind()).get_columns("provider_configs")}:
        return
    op.add_column(
        "provider_configs",
        sa.Column("secret_ciphertext", sa.Text(), nullable=False, server_default=""),
    )
    with op.batch_alter_table("provider_configs") as batch:
        batch.alter_column("secret_ciphertext", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("provider_configs") as batch:
        batch.drop_column("secret_ciphertext")
