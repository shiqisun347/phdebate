#!/usr/bin/env bash
set -euo pipefail

MANIFEST="${1:-}"
if [[ -z "$MANIFEST" || ! -f "$MANIFEST" ]]; then
  echo "Usage: $0 <recovery-set.manifest> [artifact-root ...]" >&2
  exit 2
fi
shift

if (($# == 0)); then
  set -- "$(dirname "$MANIFEST")" \
    /home/ubuntu/sunsq/phdebate-v2/runtime/backups \
    /home/ubuntu/sunsq/phdebate-v2/runtime/deploy-backups \
    /home/ubuntu/sunsq/debate-agent/backups
fi

value_for() {
  local key="$1"
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 1}' "$MANIFEST"
}

test "$(value_for schema_version)" = "2" || {
  echo "Unsupported recovery manifest schema; expected 2." >&2
  exit 1
}

roles=(v2_database agent_database data_volumes private_config reliable_voice moss_offline)
for role in "${roles[@]}"; do
  filename="$(value_for "artifact.$role.filename")"
  expected_bytes="$(value_for "artifact.$role.bytes")"
  expected_sha="$(value_for "artifact.$role.sha256")"
  [[ "$filename" =~ ^[A-Za-z0-9._-]+$ && "$filename" != .* ]] || {
    echo "Unsafe artifact filename for $role: $filename" >&2
    exit 1
  }

  matches=()
  for root in "$@"; do
    [[ -d "$root" ]] || continue
    candidate="$root/$filename"
    [[ -f "$candidate" ]] && matches+=("$candidate")
  done
  if ((${#matches[@]} != 1)); then
    echo "Expected exactly one $role artifact named $filename; found ${#matches[@]}." >&2
    exit 1
  fi

  file="${matches[0]}"
  actual_bytes="$(stat -c %s "$file" 2>/dev/null || stat -f %z "$file")"
  actual_sha="$(sha256sum "$file" | awk '{print $1}')"
  test "$actual_bytes" = "$expected_bytes" || {
    echo "Size mismatch for $role: $file" >&2
    exit 1
  }
  test "$actual_sha" = "$expected_sha" || {
    echo "SHA-256 mismatch for $role: $file" >&2
    exit 1
  }
  printf 'artifact_verified role=%s file=%s bytes=%s\n' "$role" "$file" "$actual_bytes"
done

echo "recovery_manifest_verified artifacts=${#roles[@]} code_commit=$(value_for code_commit)"
