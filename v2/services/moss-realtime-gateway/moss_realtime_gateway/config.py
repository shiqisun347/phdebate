from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

OPENMOSS_UPSTREAM_REVISION = "ad99ec5f26debf1d6c1a4dc8461b2bcb787ec9af"
OPENMOSS_MODEL_REVISION = "6acbc7f161a0db71c291f2d0aaa9eee59334cab2"
OPENMOSS_CODEC_REVISION = "3cd226ba2947efa357ef453bcad111b6eafba782"
VOICE_ID_PATTERN = re.compile(r"^debate_voice_[1-8]$")


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _voice_prompts_env() -> dict[str, str]:
    raw = os.getenv("MOSS_GATEWAY_VOICE_PROMPTS_JSON", "{}").strip() or "{}"
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("MOSS_GATEWAY_VOICE_PROMPTS_JSON must contain an object")
    return {str(key): str(filename) for key, filename in value.items()}


@dataclass(frozen=True)
class GatewaySettings:
    backend: str = "disabled"
    prompt_dir: Path = Path("/opt/phdebate/moss-prompts")
    voice_prompts: dict[str, str] = field(default_factory=dict)
    require_eight_prompts: bool = True
    api_key: str = ""
    sample_rate: int = 24_000
    control_ack_timeout_seconds: float = 30.0
    # A normal final command includes generation of the remainder of the
    # continuous utterance.  It must not share the short abort/close grace.
    final_generation_timeout_seconds: float = 180.0
    terminal_grace_seconds: float = 15.0
    startup_timeout_seconds: float = 600.0
    fail_fast_url: str = ""
    fail_fast_timeout_seconds: float = 2.0
    warmup_text: str = "现在开始普通话实时语音预热。"
    model_path: str = "OpenMOSS-Team/MOSS-TTS-Realtime"
    tokenizer_path: str = "OpenMOSS-Team/MOSS-TTS-Realtime"
    codec_model_path: str = "OpenMOSS-Team/MOSS-Audio-Tokenizer"
    upstream_checkout: Path = Path("/opt/OpenMOSS/MOSS-TTS")
    upstream_revision: str = OPENMOSS_UPSTREAM_REVISION
    model_revision: str = OPENMOSS_MODEL_REVISION
    codec_revision: str = OPENMOSS_CODEC_REVISION
    device: str = "cuda:0"
    attention_implementation: str = "sdpa"
    minimum_gpu_total_memory_gb: float = 24.0
    minimum_gpu_free_memory_gb: float = 20.0
    allow_sub24gb_diagnostic: bool = False
    diagnostic_codec_device: str = ""
    diagnostic_codec_encoder_offload: bool = False
    # Match the fixed OpenMOSS realtime streaming example.  A frame represents
    # about 80 ms of audio, so 12 decoder frames turn every network chunk into
    # an approximately 960 ms burst and make continuous playback impossible.
    prompt_chunk_duration_seconds: float = 0.24
    decode_chunk_frames: int = 3
    decode_overlap_frames: int = 0
    initial_chunk_frames: int = 6
    max_length: int = 10_000
    temperature: float = 0.8
    top_p: float = 0.6
    top_k: int = 30
    do_sample: bool = True
    repetition_penalty: float = 1.1
    repetition_window: int = 50
    # Match the fixed upstream app's compile cache and prime the full
    # repetition-window text-token path before accepting user traffic.
    dynamo_cache_size_limit: int = 64
    # Canary opt-in for the rebuilt execution kernel. The talker remains on
    # its default CUDA stream while codec decode runs on one dedicated stream.
    async_decoder_enabled: bool = False
    async_decoder_queue_size: int = 2
    async_decoder_join_timeout_seconds: float = 5.0
    # Canary-only wall-clock instrumentation. Sessions must additionally opt
    # in over the WebSocket start message before any metadata is emitted.
    canary_stage_observability: bool = False

    @classmethod
    def from_env(cls) -> GatewaySettings:
        return cls(
            backend=os.getenv("MOSS_GATEWAY_BACKEND", "disabled").strip().lower(),
            prompt_dir=Path(os.getenv("MOSS_GATEWAY_PROMPT_DIR", "/opt/phdebate/moss-prompts")).expanduser(),
            voice_prompts=_voice_prompts_env(),
            require_eight_prompts=os.getenv("MOSS_GATEWAY_REQUIRE_EIGHT_PROMPTS", "true").lower() == "true",
            api_key=os.getenv("MOSS_GATEWAY_API_KEY", ""),
            sample_rate=_int_env("MOSS_GATEWAY_SAMPLE_RATE", 24_000),
            control_ack_timeout_seconds=_float_env("MOSS_GATEWAY_CONTROL_ACK_TIMEOUT_SECONDS", 30.0),
            final_generation_timeout_seconds=_float_env(
                "MOSS_GATEWAY_FINAL_GENERATION_TIMEOUT_SECONDS", 180.0
            ),
            terminal_grace_seconds=_float_env("MOSS_GATEWAY_TERMINAL_GRACE_SECONDS", 15.0),
            startup_timeout_seconds=_float_env("MOSS_GATEWAY_STARTUP_TIMEOUT_SECONDS", 600.0),
            fail_fast_url=os.getenv("MOSS_GATEWAY_FAIL_FAST_URL", "").strip(),
            fail_fast_timeout_seconds=_float_env("MOSS_GATEWAY_FAIL_FAST_TIMEOUT_SECONDS", 2.0),
            warmup_text=os.getenv("MOSS_GATEWAY_WARMUP_TEXT", "现在开始普通话实时语音预热。"),
            model_path=os.getenv("MOSS_GATEWAY_MODEL_PATH", "OpenMOSS-Team/MOSS-TTS-Realtime"),
            tokenizer_path=os.getenv("MOSS_GATEWAY_TOKENIZER_PATH", "OpenMOSS-Team/MOSS-TTS-Realtime"),
            codec_model_path=os.getenv("MOSS_GATEWAY_CODEC_MODEL_PATH", "OpenMOSS-Team/MOSS-Audio-Tokenizer"),
            upstream_checkout=Path(
                os.getenv("MOSS_GATEWAY_UPSTREAM_CHECKOUT", "/opt/OpenMOSS/MOSS-TTS")
            ).expanduser(),
            upstream_revision=os.getenv("MOSS_GATEWAY_UPSTREAM_REVISION", OPENMOSS_UPSTREAM_REVISION),
            model_revision=os.getenv(
                "MOSS_GATEWAY_MODEL_REVISION", OPENMOSS_MODEL_REVISION
            ),
            codec_revision=os.getenv(
                "MOSS_GATEWAY_CODEC_REVISION", OPENMOSS_CODEC_REVISION
            ),
            device=os.getenv("MOSS_GATEWAY_DEVICE", "cuda:0"),
            attention_implementation=os.getenv("MOSS_GATEWAY_ATTN_IMPL", "sdpa"),
            minimum_gpu_total_memory_gb=_float_env("MOSS_GATEWAY_MIN_GPU_TOTAL_MEMORY_GB", 24.0),
            minimum_gpu_free_memory_gb=_float_env("MOSS_GATEWAY_MIN_GPU_FREE_MEMORY_GB", 20.0),
            allow_sub24gb_diagnostic=os.getenv(
                "MOSS_GATEWAY_ALLOW_SUB24GB_DIAGNOSTIC", "false"
            ).lower()
            == "true",
            diagnostic_codec_device=os.getenv(
                "MOSS_GATEWAY_DIAGNOSTIC_CODEC_DEVICE", ""
            ).strip().lower(),
            diagnostic_codec_encoder_offload=os.getenv(
                "MOSS_GATEWAY_DIAGNOSTIC_CODEC_ENCODER_OFFLOAD", "false"
            ).lower()
            == "true",
            prompt_chunk_duration_seconds=_float_env("MOSS_GATEWAY_PROMPT_CHUNK_SECONDS", 0.24),
            decode_chunk_frames=_int_env("MOSS_GATEWAY_DECODE_CHUNK_FRAMES", 3),
            decode_overlap_frames=_int_env("MOSS_GATEWAY_DECODE_OVERLAP_FRAMES", 0),
            initial_chunk_frames=_int_env("MOSS_GATEWAY_INITIAL_CHUNK_FRAMES", 6),
            max_length=_int_env("MOSS_GATEWAY_MAX_LENGTH", 10_000),
            temperature=_float_env("MOSS_GATEWAY_TEMPERATURE", 0.8),
            top_p=_float_env("MOSS_GATEWAY_TOP_P", 0.6),
            top_k=_int_env("MOSS_GATEWAY_TOP_K", 30),
            do_sample=_bool_env("MOSS_GATEWAY_DO_SAMPLE", True),
            repetition_penalty=_float_env("MOSS_GATEWAY_REPETITION_PENALTY", 1.1),
            repetition_window=_int_env("MOSS_GATEWAY_REPETITION_WINDOW", 50),
            dynamo_cache_size_limit=_int_env("MOSS_GATEWAY_DYNAMO_CACHE_SIZE_LIMIT", 64),
            async_decoder_enabled=os.getenv(
                "MOSS_GATEWAY_ASYNC_DECODER_ENABLED", "false"
            ).lower()
            == "true",
            async_decoder_queue_size=_int_env("MOSS_GATEWAY_ASYNC_DECODER_QUEUE_SIZE", 2),
            async_decoder_join_timeout_seconds=_float_env(
                "MOSS_GATEWAY_ASYNC_DECODER_JOIN_TIMEOUT_SECONDS", 5.0
            ),
            canary_stage_observability=os.getenv(
                "MOSS_GATEWAY_CANARY_STAGE_OBSERVABILITY", "false"
            ).lower()
            == "true",
        )

    def validate(self) -> None:
        if self.backend not in {"disabled", "fake", "openmoss"}:
            raise ValueError("MOSS_GATEWAY_BACKEND must be disabled, fake, or openmoss")
        if self.sample_rate != 24_000:
            raise ValueError("MOSS-TTS-Realtime gateway output is fixed to 24000 Hz")
        if min(
            self.control_ack_timeout_seconds,
            self.final_generation_timeout_seconds,
            self.terminal_grace_seconds,
            self.startup_timeout_seconds,
        ) <= 0:
            raise ValueError("gateway timeouts must be positive")
        if self.dynamo_cache_size_limit < 8:
            raise ValueError("MOSS_GATEWAY_DYNAMO_CACHE_SIZE_LIMIT must be at least 8")
        if not 1 <= self.async_decoder_queue_size <= 16:
            raise ValueError("MOSS_GATEWAY_ASYNC_DECODER_QUEUE_SIZE must be between 1 and 16")
        if self.async_decoder_join_timeout_seconds <= 0:
            raise ValueError("MOSS_GATEWAY_ASYNC_DECODER_JOIN_TIMEOUT_SECONDS must be positive")
        if self.backend == "openmoss":
            if not self.api_key:
                raise ValueError("openmoss backend requires MOSS_GATEWAY_API_KEY")
            if not self.require_eight_prompts:
                raise ValueError("openmoss backend cannot disable the fixed eight-prompt requirement")
            if self.upstream_revision != OPENMOSS_UPSTREAM_REVISION:
                raise ValueError(f"OpenMOSS upstream revision must be {OPENMOSS_UPSTREAM_REVISION}")
            if self.model_revision != OPENMOSS_MODEL_REVISION:
                raise ValueError(f"OpenMOSS model revision must be {OPENMOSS_MODEL_REVISION}")
            if self.codec_revision != OPENMOSS_CODEC_REVISION:
                raise ValueError(f"OpenMOSS codec revision must be {OPENMOSS_CODEC_REVISION}")
            if not self.device.startswith("cuda"):
                raise ValueError("openmoss backend requires a CUDA device")
            if self.attention_implementation != "sdpa":
                raise ValueError("openmoss backend requires the fixed SDPA acceleration path")
            if self.minimum_gpu_total_memory_gb < 24 and not self.allow_sub24gb_diagnostic:
                raise ValueError(
                    "openmoss backend requires at least 24GB configured GPU memory unless the explicit "
                    "diagnostic-only override is enabled"
                )
            if self.allow_sub24gb_diagnostic and self.minimum_gpu_total_memory_gb < 10:
                raise ValueError("sub-24GB diagnostic mode still requires at least 10GB configured GPU memory")
            if self.diagnostic_codec_device:
                if not self.allow_sub24gb_diagnostic:
                    raise ValueError(
                        "diagnostic codec placement requires MOSS_GATEWAY_ALLOW_SUB24GB_DIAGNOSTIC=true"
                    )
                if self.diagnostic_codec_device != "cpu":
                    raise ValueError("diagnostic codec device currently supports only cpu")
                if self.async_decoder_enabled:
                    raise ValueError("async decoder requires the codec decoder to remain on CUDA")
            if self.diagnostic_codec_encoder_offload:
                if not self.allow_sub24gb_diagnostic:
                    raise ValueError(
                        "diagnostic codec encoder offload requires "
                        "MOSS_GATEWAY_ALLOW_SUB24GB_DIAGNOSTIC=true"
                    )
                if self.diagnostic_codec_device:
                    raise ValueError(
                        "diagnostic codec encoder offload cannot be combined with whole-codec CPU placement"
                    )
            if not 0 < self.minimum_gpu_free_memory_gb <= self.minimum_gpu_total_memory_gb:
                raise ValueError("openmoss free-memory threshold must be positive and not exceed total memory")
            expected = {f"debate_voice_{index}" for index in range(1, 9)}
            if set(self.voice_prompts) != expected:
                raise ValueError("openmoss backend requires fixed debate_voice_1..8 prompt mapping")
        for voice_id, filename in self.voice_prompts.items():
            if not VOICE_ID_PATTERN.fullmatch(voice_id):
                raise ValueError(f"invalid fixed voice id: {voice_id}")
            if Path(filename).name != filename or not filename.lower().endswith(".wav"):
                raise ValueError(f"voice prompt must be a WAV basename: {voice_id}")


class PromptRegistry:
    def __init__(self, settings: GatewaySettings) -> None:
        self.root = settings.prompt_dir.expanduser().resolve()
        self.mapping = dict(settings.voice_prompts)

    def validate_all(self) -> dict[str, Path]:
        return {voice_id: self.resolve(voice_id) for voice_id in self.mapping}

    def resolve(self, requested: str) -> Path:
        raw = requested.strip()
        filename = self.mapping.get(raw, raw)
        if not filename or Path(filename).name != filename or not filename.lower().endswith(".wav"):
            raise ValueError("prompt_audio must be a configured voice id or WAV basename")
        if filename not in self.mapping.values():
            raise ValueError("prompt_audio is not in the fixed voice allowlist")
        unresolved = self.root / filename
        candidate = unresolved.resolve()
        if unresolved.is_symlink() or candidate.parent != self.root or not candidate.is_file():
            raise ValueError("configured prompt audio is missing or unsafe")
        return candidate
