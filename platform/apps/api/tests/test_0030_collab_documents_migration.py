from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql, sqlite


def _load_migration():
    migration_path = (
        Path(__file__).parents[1] / "alembic" / "versions" / "0030_collab_documents.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0030_collab_documents", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_0030_uses_database_appropriate_binary_types() -> None:
    migration = _load_migration()

    assert isinstance(
        migration.STATE_TYPE.dialect_impl(postgresql.dialect()),
        postgresql.BYTEA,
    )
    assert isinstance(
        migration.STATE_TYPE.dialect_impl(sqlite.dialect()),
        sa.LargeBinary,
    )


def test_0030_upgrade_and_downgrade_on_sqlite(tmp_path: Path) -> None:
    migration = _load_migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'collab.db'}")

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        migration.op = Operations(context)

        migration.upgrade()
        migration.upgrade()

        inspector = sa.inspect(connection)
        assert "collab_documents" in inspector.get_table_names()
        columns = {column["name"]: column for column in inspector.get_columns("collab_documents")}
        assert set(columns) == {"document_name", "state", "version", "updated_at"}
        assert columns["document_name"]["nullable"] is False
        assert columns["state"]["nullable"] is False
        assert isinstance(columns["state"]["type"], sa.LargeBinary)
        assert columns["version"]["nullable"] is False
        assert isinstance(columns["version"]["type"], sa.BigInteger)
        assert columns["version"]["default"] == "1"
        assert columns["updated_at"]["nullable"] is False
        assert inspector.get_pk_constraint("collab_documents")["constrained_columns"] == [
            "document_name"
        ]

        connection.execute(
            sa.text("INSERT INTO collab_documents (document_name, state) VALUES (:name, :state)"),
            {"name": "match:1", "state": b"yjs-state"},
        )
        row = connection.execute(
            sa.text(
                "SELECT document_name, state, version, updated_at "
                "FROM collab_documents WHERE document_name = :name"
            ),
            {"name": "match:1"},
        ).mappings().one()
        assert row["document_name"] == "match:1"
        assert row["state"] == b"yjs-state"
        assert row["version"] == 1
        assert row["updated_at"] is not None

        migration.downgrade()
        migration.downgrade()
        assert "collab_documents" not in sa.inspect(connection).get_table_names()
