from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

test_runtime = Path(tempfile.mkdtemp(prefix="phdebate-pytest-"))
db_path = test_runtime / "pytest.db"
os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
os.environ["APP_ENV"] = "test"
os.environ["APP_SECRET"] = "pytest-only-secret"
os.environ["AGENT_MOCK"] = "true"
os.environ["AGENT_API_URL"] = "http://127.0.0.1:1/api/debate"
os.environ["JUDGE_API_URL"] = ""
os.environ["ENGINE_ENABLED"] = "false"
os.environ["PRESENCE_RESET_ON_STARTUP"] = "false"
os.environ["COOKIE_SECURE"] = "false"
os.environ["REDIS_URL"] = "redis://127.0.0.1:1/15"
os.environ["PUBLIC_ORIGIN"] = "http://localhost:3200"
os.environ["ALLOWED_ORIGINS"] = "http://127.0.0.1:3200,http://localhost:3200"
os.environ["FUNASR_WS_URL"] = "ws://127.0.0.1:1"
os.environ["LIGHTTTS_URL"] = "http://127.0.0.1:1/inference_zero_shot"
# Most existing regression cases intentionally exercise the legacy audio
# archive surface.  Production defaults to text-only; dedicated policy tests
# below explicitly switch this setting off.
os.environ["MATCH_AUDIO_ARCHIVE_ENABLED"] = "true"
archive_path = test_runtime / "archives"
os.environ["ARCHIVE_ROOT"] = str(archive_path)
media_path = test_runtime / "audio"
os.environ["MEDIA_ROOT"] = str(media_path)
os.environ["BACKUP_STATUS_FILE"] = str(test_runtime / "backup-status.json")
os.environ["AGENT_BACKUP_STATUS_FILE"] = str(test_runtime / "agent-backup-status.json")
os.environ["ADMIN_ACCOUNT"] = "admin_test"
os.environ["ADMIN_REAL_NAME"] = "测试管理员"
os.environ["ADMIN_PASSWORD"] = "Admin-test-1234"

import pytest
from app.api.auth import attempts
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Remove only this pytest process' isolated runtime after all fixtures close."""
    del session, exitstatus
    shutil.rmtree(test_runtime, ignore_errors=True)


def csrf(client: TestClient) -> dict[str, str]:
    value = client.cookies.get("jixia_csrf")
    return {"X-CSRF-Token": value} if value else {}


@pytest.fixture
def register_user(client: TestClient):
    counter = {"value": 0}

    def register(prefix: str = "user") -> TestClient:
        attempts.clear()
        counter["value"] += 1
        isolated = TestClient(app)
        isolated.__enter__()
        response = isolated.post(
            "/api/auth/register",
            json={
                "account": f"{prefix}_{counter['value']}",
                "real_name": f"测试选手{prefix}{counter['value']}",
                "password": "Password-1234",
                "confirm_password": "Password-1234",
            },
        )
        assert response.status_code == 200, response.text
        return isolated

    clients = []

    def tracked(prefix: str = "user") -> TestClient:
        item = register(prefix)
        clients.append(item)
        return item

    yield tracked
    for item in clients:
        item.__exit__(None, None, None)
