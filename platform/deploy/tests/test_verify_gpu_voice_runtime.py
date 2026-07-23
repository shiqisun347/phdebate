from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "verify_gpu_voice_runtime.py"
SPEC = importlib.util.spec_from_file_location("verify_gpu_voice_runtime", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_funasr_config_requires_cuda(tmp_path: Path) -> None:
    config = tmp_path / "funasr.conf"
    config.write_text(
        '[program:funasr]\ncommand=python local_funasr_ws.py --device cuda:0\n'
        'environment=CUDA_VISIBLE_DEVICES="0"\n',
        encoding="utf-8",
    )
    assert MODULE.verify_funasr_config(config) == {
        "device": "cuda:0",
        "cuda_visible_devices": "0",
    }


@pytest.mark.parametrize(
    "command,environment",
    [
        ("python local_funasr_ws.py --device cpu", 'CUDA_VISIBLE_DEVICES="0"'),
        ("python local_funasr_ws.py --device cuda:0", 'CUDA_VISIBLE_DEVICES="-1"'),
        ("python local_funasr_ws.py", 'CUDA_VISIBLE_DEVICES="0"'),
    ],
)
def test_funasr_config_rejects_cpu_fallback(tmp_path: Path, command: str, environment: str) -> None:
    config = tmp_path / "funasr.conf"
    config.write_text(f"command={command}\nenvironment={environment}\n", encoding="utf-8")
    with pytest.raises(MODULE.VerificationError):
        MODULE.verify_funasr_config(config)


def test_moss_config_requires_reliable_runtime_baseline(tmp_path: Path) -> None:
    config = tmp_path / "moss.conf"
    config.write_text(
        '[program:moss]\n'
        'environment=MOSS_GATEWAY_DECODE_CHUNK_FRAMES="12",'
        'MOSS_GATEWAY_INITIAL_CHUNK_FRAMES="6",'
        'MOSS_GATEWAY_DO_SAMPLE="true",'
        'MOSS_GATEWAY_LOCAL_COMPILE_MODE="reduce-overhead",'
        'MOSS_GATEWAY_LOCAL_COMPILE_DYNAMIC="false",'
        'MOSS_GATEWAY_ASYNC_DECODER_ENABLED="false"\n',
        encoding="utf-8",
    )
    assert MODULE.verify_moss_config(config) == MODULE.MOSS_RELIABLE_RUNTIME_ENV


@pytest.mark.parametrize(
    "name,value",
    [
        ("MOSS_GATEWAY_DECODE_CHUNK_FRAMES", "3"),
        ("MOSS_GATEWAY_INITIAL_CHUNK_FRAMES", "12"),
        ("MOSS_GATEWAY_DO_SAMPLE", "false"),
        ("MOSS_GATEWAY_LOCAL_COMPILE_MODE", "default"),
        ("MOSS_GATEWAY_LOCAL_COMPILE_DYNAMIC", "true"),
        ("MOSS_GATEWAY_ASYNC_DECODER_ENABLED", "true"),
    ],
)
def test_moss_config_rejects_runtime_drift(tmp_path: Path, name: str, value: str) -> None:
    environment = dict(MODULE.MOSS_RELIABLE_RUNTIME_ENV)
    environment[name] = value
    config = tmp_path / "moss.conf"
    config.write_text(
        "[program:moss]\nenvironment="
        + ",".join(f'{key}="{item}"' for key, item in environment.items())
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(MODULE.VerificationError, match=name):
        MODULE.verify_moss_config(config)


def test_moss_health_requires_every_component_on_cuda() -> None:
    payload = {
        "ok": True,
        "status": "ready",
        "backend": "openmoss",
        "model_warmed": True,
        "warmed_up": True,
        "orphan_count": 0,
        "capacity": 1,
        "placement": {
            "mode": "production_all_cuda",
            "realtime_model": "cuda:0",
            "codec_encoder": "cuda:0",
            "codec_quantizer": "cuda:0",
            "codec_decoder": "cuda:0",
            "codec_streaming_context": "full_codec",
        },
    }
    result = MODULE.verify_moss_health(payload)
    assert result["placement"]["codec_decoder"] == "cuda:0"

    payload["placement"]["codec_encoder"] = "cpu"
    with pytest.raises(MODULE.VerificationError):
        MODULE.verify_moss_health(payload)
