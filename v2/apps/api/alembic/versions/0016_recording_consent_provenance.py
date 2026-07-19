"""Snapshot recording-consent provenance on matches and speeches.

Revision ID: 0016_consent_provenance
Revises: 0015_consent_privacy_mvp
"""

import sqlalchemy as sa
from alembic import op

revision = "0016_consent_provenance"
down_revision = "0015_consent_privacy_mvp"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    if "recording_consent_snapshot" not in _columns("matches"):
        op.add_column(
            "matches",
            sa.Column(
                "recording_consent_snapshot",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )
    if "recording_consent_snapshot" not in _columns("speeches"):
        op.add_column(
            "speeches",
            sa.Column(
                "recording_consent_snapshot",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )


def downgrade() -> None:
    if "recording_consent_snapshot" in _columns("speeches"):
        op.drop_column("speeches", "recording_consent_snapshot")
    if "recording_consent_snapshot" in _columns("matches"):
        op.drop_column("matches", "recording_consent_snapshot")
