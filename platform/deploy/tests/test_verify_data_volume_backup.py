from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "verify-data-volume-backup.sh"


def build_archive(path: Path, *, checksum: str | None = None, member_name: str = "storage/example.txt") -> None:
    content = b"debate evidence\n"
    digest = checksum or hashlib.sha256(content).hexdigest()
    manifest = json.dumps({"schema_version": 1}).encode()
    checksums = f"{digest}  {member_name}\n".encode()
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in (
            (member_name, content),
            ("manifest.json", manifest),
            ("files.sha256", checksums),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def test_verifier_extracts_and_checks_every_file(tmp_path: Path) -> None:
    archive = tmp_path / "data-volumes.tar.gz"
    build_archive(archive)

    result = subprocess.run([str(SCRIPT), str(archive)], text=True, capture_output=True, check=True)

    assert "data_restore_verified" in result.stdout
    assert "files=1" in result.stdout


def test_verifier_rejects_checksum_mismatch(tmp_path: Path) -> None:
    archive = tmp_path / "bad.tar.gz"
    build_archive(archive, checksum="0" * 64)

    result = subprocess.run([str(SCRIPT), str(archive)], text=True, capture_output=True)

    assert result.returncode != 0


def test_verifier_rejects_parent_directory_members(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    build_archive(archive, member_name="../escape.txt")

    result = subprocess.run([str(SCRIPT), str(archive)], text=True, capture_output=True)

    assert result.returncode != 0
    assert "Unsafe archive member" in result.stderr
