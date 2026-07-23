from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1]


def test_engine_worker_restart_fails_closed_before_supervisor_restart() -> None:
    script = (DEPLOY / "restart-engine-workers.sh").read_text()
    guard_position = script.index("assert-no-active-matches.py")
    restart_position = script.index("restart jixia-engine jixia-worker")
    assert guard_position < restart_position
    assert "set -euo pipefail" in script


def test_active_match_guard_covers_every_engine_owned_match_state() -> None:
    script = (DEPLOY / "assert-no-active-matches.py").read_text()
    for status in ("preparing", "running", "paused", "judging"):
        assert f'"{status}"' in script
    assert "engine_restart_blocked" in script
    assert "raise SystemExit(2)" in script
