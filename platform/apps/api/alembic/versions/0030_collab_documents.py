"""Add persisted collaborative transcript documents.

Revision ID: 0030_collab_documents
Revises: 0029_free_turn_requests
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030_collab_documents"
down_revision: str | None = "0029_free_turn_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


STATE_TYPE = sa.LargeBinary().with_variant(postgresql.BYTEA(), "postgresql")


def upgrade() -> None:
    if "collab_documents" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "collab_documents",
        sa.Column("document_name", sa.Text(), nullable=False),
        sa.Column("state", STATE_TYPE, nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("document_name"),
    )


def downgrade() -> None:
    if "collab_documents" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("collab_documents")
