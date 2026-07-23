from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "operator_credentials.py"
SPEC = importlib.util.spec_from_file_location("operator_credentials_under_test", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_canonical_names_are_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHDEBATE_ADMIN_ACCOUNT", "current-admin")
    monkeypatch.setenv("PHDEBATE_ADMIN_PASSWORD", "current-secret")

    assert MODULE.admin_credentials() == ("current-admin", "current-secret")


def test_retired_versioned_names_are_not_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHDEBATE_ADMIN_ACCOUNT", raising=False)
    monkeypatch.delenv("PHDEBATE_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("V2_ADMIN_ACCOUNT", "compat-admin")
    monkeypatch.setenv("V2_ADMIN_PASSWORD", "compat-secret")

    with pytest.raises(RuntimeError, match="credentials are not configured"):
        MODULE.admin_credentials()


def test_missing_credentials_fail_without_disclosing_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "PHDEBATE_ADMIN_ACCOUNT",
        "PHDEBATE_ADMIN_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match="credentials are not configured"):
        MODULE.admin_credentials()
