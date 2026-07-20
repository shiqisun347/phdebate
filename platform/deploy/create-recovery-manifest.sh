#!/usr/bin/env bash
set -euo pipefail

ROOT="${PHDEBATE_ROOT:-/home/ubuntu/sunsq/phdebate}"
AGENT_ROOT="${PHDEBATE_AGENT_ROOT:-/home/ubuntu/sunsq/debate-agent}"
BACKUP_DIR="${PHDEBATE_DEPLOY_BACKUP_DIR:-$ROOT/runtime/deploy-backups}"
TIMESTAMP="${PHDEBATE_RECOVERY_TIMESTAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
OUTPUT="${1:-$BACKUP_DIR/recovery-set-$TIMESTAMP.manifest}"

required_value() {
  local name="$1"
  local value="${!name:-}"
  if [[ -z "$value" ]]; then
    echo "missing required environment variable: $name" >&2
    exit 1
  fi
  printf '%s' "$value"
}

latest_file() {
  local directory="$1"
  local pattern="$2"
  find "$directory" -maxdepth 1 -type f -name "$pattern" -printf '%T@ %p\n' \
    | sort -nr | head -n 1 | cut -d' ' -f2-
}

CODE_BRANCH="$(required_value PHDEBATE_CODE_BRANCH)"
CODE_COMMIT="$(required_value PHDEBATE_CODE_COMMIT)"
DEPLOYED_COMMIT="$(required_value PHDEBATE_DEPLOYED_APPLICATION_COMMIT)"
DATABASE_SCHEMA="$(required_value PHDEBATE_DATABASE_SCHEMA)"
RELIABLE_AUDIO_FINGERPRINT="$(required_value PHDEBATE_RELIABLE_AUDIO_FINGERPRINT)"
API_RELEASE="${PHDEBATE_API_RELEASE:-$(basename "$(readlink -f "$ROOT/.api-primary")")}"
WEB_RELEASE="${PHDEBATE_WEB_RELEASE:-$(basename "$(readlink -f "$ROOT/.web-current")")}"

PLATFORM_DATABASE_BACKUP="${PHDEBATE_DATABASE_BACKUP:-$(latest_file "$ROOT/runtime/backups" 'auto-*.dump')}"
AGENT_DATABASE_BACKUP="${PHDEBATE_AGENT_DATABASE_BACKUP:-$(latest_file "$AGENT_ROOT/backups" 'agent-*.dump')}"
DATA_VOLUME_BACKUP="${PHDEBATE_DATA_VOLUME_BACKUP:-$(latest_file "$BACKUP_DIR" '*-data-volumes.tar.gz')}"
PRIVATE_CONFIG_BACKUP="$(required_value PHDEBATE_PRIVATE_CONFIG_BACKUP)"
RELIABLE_VOICE_BACKUP="$(required_value PHDEBATE_RELIABLE_VOICE_BACKUP)"
MOSS_OFFLINE_BACKUP="$(required_value PHDEBATE_MOSS_OFFLINE_BACKUP)"

FILES=(
  "$PLATFORM_DATABASE_BACKUP"
  "$AGENT_DATABASE_BACKUP"
  "$DATA_VOLUME_BACKUP"
  "$PRIVATE_CONFIG_BACKUP"
  "$RELIABLE_VOICE_BACKUP"
  "$MOSS_OFFLINE_BACKUP"
)
ROLES=(
  platform_database
  agent_database
  data_volumes
  private_config
  reliable_voice
  moss_offline
)

for file in "${FILES[@]}"; do
  if [[ ! -f "$file" ]]; then
    echo "missing recovery file: $file" >&2
    exit 1
  fi
done

install -d -m 700 "$(dirname "$OUTPUT")"
umask 077
TEMP="$OUTPUT.part"
cleanup() {
  find "$(dirname "$TEMP")" -maxdepth 1 -type f -name "$(basename "$TEMP")" -delete
}
trap cleanup EXIT

{
  printf 'schema_version=3\n'
  printf 'created_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'code_branch=%s\n' "$CODE_BRANCH"
  printf 'code_commit=%s\n' "$CODE_COMMIT"
  printf 'deployed_application_commit=%s\n' "$DEPLOYED_COMMIT"
  printf 'database_schema=%s\n' "$DATABASE_SCHEMA"
  printf 'api_release=%s\n' "$API_RELEASE"
  printf 'web_release=%s\n' "$WEB_RELEASE"
  printf 'reliable_audio_fingerprint=%s\n' "$RELIABLE_AUDIO_FINGERPRINT"
  for index in "${!FILES[@]}"; do
    file="${FILES[$index]}"
    role="${ROLES[$index]}"
    printf 'artifact.%s.filename=%s\n' "$role" "$(basename "$file")"
    printf 'artifact.%s.bytes=%s\n' "$role" "$(stat -c %s "$file" 2>/dev/null || stat -f %z "$file")"
    printf 'artifact.%s.sha256=%s\n' "$role" "$(sha256sum "$file" | awk '{print $1}')"
  done
} >"$TEMP"

mv "$TEMP" "$OUTPUT"
chmod 600 "$OUTPUT"
echo "recovery_manifest_ready file=$OUTPUT files=${#FILES[@]} code_commit=$CODE_COMMIT"
