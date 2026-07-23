from __future__ import annotations

import ast
from pathlib import Path


def test_every_alembic_revision_fits_the_production_version_column() -> None:
    versions = Path(__file__).parents[2] / "apps" / "api" / "alembic" / "versions"
    revisions: list[tuple[Path, str]] = []
    for path in sorted(versions.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for statement in tree.body:
            if not isinstance(statement, ast.AnnAssign) or not isinstance(statement.target, ast.Name):
                continue
            if statement.target.id != "revision" or not isinstance(statement.value, ast.Constant):
                continue
            revisions.append((path, str(statement.value.value)))
            break

    assert revisions
    too_long = [(path.name, revision) for path, revision in revisions if len(revision) > 32]
    assert too_long == [], f"alembic_version.version_num is VARCHAR(32): {too_long}"
