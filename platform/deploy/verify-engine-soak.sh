#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PHDEBATE_TEST_PYTHON:-$ROOT/.venv/bin/python}"
CYCLES="${PHDEBATE_SOAK_CYCLES:-5}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python runtime is not executable: $PYTHON_BIN" >&2
  exit 1
fi
if [[ ! "$CYCLES" =~ ^[1-9][0-9]*$ ]]; then
  echo "PHDEBATE_SOAK_CYCLES must be a positive integer." >&2
  exit 1
fi

tests=(
  tests/test_multi_room_simulation.py::test_five_rooms_complete_concurrently_without_state_leakage
  tests/test_multi_room_simulation.py::test_five_mixed_rooms_advance_only_their_authoritative_state
  tests/test_multi_room_simulation.py::test_concurrent_rooms_keep_distinct_frozen_judge_profiles
  tests/test_multi_room_simulation.py::test_concurrent_room_preparation_uses_distinct_frozen_speech_services
  tests/test_multi_room_simulation.py::test_tick_does_not_wait_for_slow_provider_room_before_scanning_again
  tests/test_multi_room_simulation.py::test_scheduler_quarantines_only_the_repeatedly_failing_room
  tests/test_multi_room_simulation.py::test_scheduler_backs_off_transient_database_errors_without_quarantine
  tests/test_multi_room_simulation.py::test_ai_audio_playback_blocks_stage_advance
  tests/test_multi_room_simulation.py::test_free_debate_rotates_across_ai_teammates
)

cd "$ROOT/apps/api"
total_started="$(date +%s)"
for cycle in $(seq 1 "$CYCLES"); do
  started="$(date +%s)"
  "$PYTHON_BIN" -m pytest -q --disable-warnings "${tests[@]}"
  ended="$(date +%s)"
  echo "engine_soak_cycle=$cycle duration_seconds=$((ended - started)) tests=${#tests[@]}"
done
total_ended="$(date +%s)"
echo "engine_soak_ok cycles=$CYCLES scenarios_per_cycle=${#tests[@]} duration_seconds=$((total_ended - total_started))"
