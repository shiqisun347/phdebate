from __future__ import annotations

from pathlib import Path

from scripts import create_reliable_audio_manifest as manifest


def test_protected_files_ignore_named_virtualenv_variants(tmp_path: Path, monkeypatch) -> None:
    protected = tmp_path / "voice-service"
    protected.mkdir()
    source = protected / "gateway.py"
    source.write_text("reliable source\n", encoding="utf-8")
    virtualenv = protected / ".venv-cu128" / "bin"
    virtualenv.mkdir(parents=True)
    (virtualenv / "python").write_text("third-party runtime\n", encoding="utf-8")
    monkeypatch.setattr(manifest, "PROTECTED_PATHS", ("voice-service",))

    assert manifest.protected_files(tmp_path) == [source]
