from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../../.env"), extra="ignore")

    app_name: str = "Jixia Debate V2"
    app_env: str = "development"
    app_secret: str = "development-only-secret-change-me"
    database_url: str = "sqlite:///./storage/phdebate-v2.db"
    database_lock_timeout_seconds: int = Field(default=5, ge=1, le=30)
    redis_url: str = "redis://127.0.0.1:6379/2"
    public_origin: str = "http://127.0.0.1:3200"
    cookie_secure: bool = False
    session_days: int = 30

    v2_admin_account: str = ""
    v2_admin_real_name: str = "系统管理员"
    v2_admin_password: str = ""

    agent_api_url: str = "http://47.93.206.109:8000/api/debate"
    agent_model_name: str = "qwen3.6-27b"
    agent_timeout_seconds: int = 120
    agent_mock: bool = False
    judge_api_url: str = ""

    funasr_ws_url: str = "ws://127.0.0.1:10095"
    lighttts_url: str = "http://127.0.0.1:8080/inference_zero_shot"
    lighttts_prompt_wav_path: str = ""
    lighttts_prompt_text: str = ""
    lighttts_prompt_texts: dict[str, str] = Field(default_factory=dict)
    # Production remains at one until the active=2 GPU canary passes.  The
    # upper bound permits an isolated/shadow process to exercise 2--3 active
    # streams without weakening the default admission limit.
    lighttts_max_active: int = Field(default=1, ge=1, le=3)
    lighttts_streaming_enabled: bool = False
    # Upstream LightTTS bi-stream is separately gated because some builds leak
    # orphan requests after websocket disconnect and can poison the only slot.
    lighttts_bistream_enabled: bool = False
    # Separate kill switch for Agent delta -> one continuous TTS session.  It
    # remains off until candidate latency, transcript and browser gates pass.
    realtime_voice_pipeline_enabled: bool = False
    # The realtime pipeline can use the legacy LightTTS bi-stream candidate or
    # a dedicated MOSS-TTS-Realtime service.  Keeping the backend explicit
    # prevents a MOSS canary from accidentally inheriting the production
    # LightTTS endpoint and prompt-path semantics.
    realtime_voice_backend: str = "lighttts"
    moss_tts_realtime_enabled: bool = False
    # The formal candidate uses one persistent bidirectional WebSocket per
    # synthesis turn. HTTP remains an explicit diagnostic/rollback transport.
    moss_tts_realtime_transport: str = "websocket"
    moss_tts_realtime_url: str = ""
    moss_tts_realtime_urls: list[str] = Field(default_factory=list)
    moss_tts_realtime_api_key: str = ""
    # MOSS conditions delivery style through the turn's user instruction.
    # Keep this server-owned so clients cannot force shouting, role-play, or
    # an inconsistent voice. The wording was selected by same-voice A/B:
    # stronger active level and a slightly shorter utterance, without clipping.
    moss_tts_realtime_speech_instruction: str = Field(
        default=(
            "请用自信、坚定、有说服力的中文辩论语气，语速比自然朗读快约百分之十，"
            "重音清晰，停顿简短，句尾有力，但不要喊叫或夸张。"
        ),
        min_length=10,
        max_length=500,
    )
    # Prompt basenames are resolved only inside the dedicated gateway's fixed
    # allowlisted prompt directory.
    moss_tts_prompt_files: dict[str, str] = Field(default_factory=dict)
    moss_tts_realtime_max_active: int = Field(default=3, ge=1, le=3)
    # The official service shares one model/codec and has not proven concurrent
    # codec.streaming contexts safe.  Scale to 2--3 active turns by listing
    # independent endpoints; keep each endpoint at one until its own canary
    # demonstrates safe multi-session execution.
    moss_tts_realtime_max_active_per_endpoint: int = Field(default=1, ge=1, le=3)
    moss_tts_realtime_connect_timeout_seconds: float = Field(default=10, ge=1, le=60)
    # A text_delta ACK confirms model consumption, not just network delivery.
    # It may arrive after several seconds of audio has already streamed.
    moss_tts_realtime_ack_timeout_seconds: float = Field(default=30, ge=2, le=120)
    moss_tts_realtime_readiness_timeout_seconds: float = Field(default=2, ge=0.2, le=10)
    moss_tts_realtime_first_pcm_timeout_seconds: float = Field(default=1.2, ge=0.2, le=3)
    # Final model completion and WebSocket release are separate.  After the
    # final ACK, a long utterance may still have a few seconds of already
    # generated PCM in the egress/LiveKit queues.  Three seconds falsely
    # quarantines healthy sessions at the very end of otherwise continuous
    # 60-90 second speeches.
    moss_tts_realtime_close_timeout_seconds: float = Field(default=15, ge=1, le=60)
    moss_tts_realtime_job_timeout_seconds: float = Field(default=240, ge=5, le=300)
    # Authenticated, unstarted sockets are process-local and never consume a
    # gateway synthesis slot. They are replaced before long-lived NAT/proxy
    # idle timeouts can turn the first speech into a reconnect path.
    moss_tts_realtime_idle_ws_ttl_seconds: float = Field(default=60, ge=5, le=300)
    # WebRTC Agent audio remains a default-off rollout. ``websocket_pcm`` is
    # retained as the explicit rollback transport until Chrome/Safari and
    # multi-room gates pass against a real LiveKit deployment.
    webrtc_audio_enabled: bool = False
    webrtc_audio_backend: str = "websocket_pcm"
    livekit_url: str = ""
    livekit_public_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    livekit_audio_sample_rate: int = Field(default=48_000, ge=48_000, le=48_000)
    livekit_audio_frame_ms: int = Field(default=20, ge=20, le=20)
    # Keep enough native queue lead to absorb the 40-130 ms scheduling stalls
    # observed while the GPU gateway emits bursty PCM. The prior 180 ms canary
    # still produced one short concealment burst, so use the validated SDK cap.
    livekit_audio_source_queue_ms: int = Field(default=200, ge=40, le=200)
    # LiveKit receives PCM locally but publishes one compressed WebRTC audio
    # track. 64 kbps mono Opus avoids metallic high-frequency artifacts on
    # synthetic voices while remaining inexpensive for classroom spectators.
    livekit_audio_max_bitrate_bps: int = Field(default=64_000, ge=16_000, le=128_000)
    # TTS contains meaningful pauses and breath noise. Opus DTX repeatedly
    # stopping/restarting the encoder produced small clicks at those edges.
    livekit_audio_dtx_enabled: bool = False
    livekit_audio_red_enabled: bool = True
    # The application queue is intentionally larger than the SDK queue.  A
    # MOSS generation is allowed to accumulate a jitter buffer here before the
    # first real frame enters LiveKit; the SDK queue remains the small,
    # continuously clocked playout queue.
    # MOSS can cross the 1.2 s start threshold with a single 1.68 s write.
    # Keep enough application headroom to enqueue that first continuous batch
    # without delaying the authoritative rtc.started event behind queue drain.
    livekit_audio_app_queue_ms: int = Field(default=2_400, ge=40, le=4_000)
    moss_tts_livekit_start_buffer_ms: int = Field(default=1_200, ge=120, le=2_400)
    livekit_connect_timeout_seconds: float = Field(default=5, ge=1, le=30)
    livekit_token_ttl_seconds: int = Field(default=300, ge=60, le=900)
    livekit_publisher_token_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    lighttts_stream_sample_rate: int = Field(default=24_000, ge=8_000, le=48_000)
    lighttts_stream_poll_seconds: float = Field(default=0.05, ge=0.01, le=1.0)
    # Disabled until the Redis shadow verifier passes in the target environment.
    lighttts_global_gate_enabled: bool = False
    lighttts_global_gate_max_pending: int = Field(default=2, ge=1, le=256)
    lighttts_global_gate_queue_timeout_seconds: float = Field(default=20, ge=1, le=900)
    # Covers queueing, every chunk, fallback prompt and all transport retries.
    lighttts_job_timeout_seconds: float = Field(default=300, ge=5, le=900)
    lighttts_shutdown_drain_seconds: float = Field(default=10, ge=1, le=60)
    # Must outlive the maximum 300-second LightTTS read timeout so a transient
    # Redis outage cannot expire the active lease while the GPU call is alive.
    lighttts_global_gate_lease_seconds: float = Field(default=330, ge=330, le=900)
    media_root: str = "./storage/audio"
    archive_root: str = "./storage/archives"
    research_export_root: str = "./storage/research-exports"
    backup_status_file: str = "runtime/backup-status.json"
    backup_max_age_hours: int = Field(default=36, ge=1, le=720)
    health_min_disk_free_percent: float = Field(default=5.0, ge=1, le=50)

    engine_poll_seconds: float = 1.0
    engine_enabled: bool = True
    engine_max_concurrent_rooms: int = Field(default=8, ge=1, le=32)
    presence_reset_on_startup: bool = True
    allowed_origins: str = "http://127.0.0.1:3200,http://localhost:3200"

    @field_validator("agent_model_name")
    @classmethod
    def forbid_qwen3_8b(cls, value: str) -> str:
        normalized = "".join(character for character in value.lower() if character.isalnum())
        if normalized.startswith("qwen38b"):
            raise ValueError("系统禁止使用 Qwen3 8B。")
        return value

    @field_validator("lighttts_prompt_texts")
    @classmethod
    def validate_lighttts_prompt_texts(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for voice, prompt_text in value.items():
            voice = str(voice).strip()
            prompt_text = str(prompt_text).strip()
            if not voice or len(voice) > 100 or not all(character.isalnum() or character in "_-" for character in voice):
                raise ValueError("LightTTS 音色 ID 格式无效。")
            if not prompt_text or len(prompt_text) > 2_000:
                raise ValueError("LightTTS 音色提示文本不能为空且不得超过 2000 字符。")
            normalized[voice] = prompt_text
        return normalized

    @field_validator("realtime_voice_backend")
    @classmethod
    def validate_realtime_voice_backend(cls, value: str) -> str:
        normalized = value.strip().lower().replace("-", "_")
        if normalized not in {"lighttts", "moss_realtime"}:
            raise ValueError("实时语音后端必须是 lighttts 或 moss_realtime。")
        return normalized

    @field_validator("moss_tts_realtime_transport")
    @classmethod
    def validate_moss_tts_realtime_transport(cls, value: str) -> str:
        normalized = value.strip().lower().replace("-", "_")
        if normalized not in {"websocket", "http"}:
            raise ValueError("MOSS-TTS-Realtime transport 必须是 websocket 或 http。")
        return normalized

    @field_validator("webrtc_audio_backend")
    @classmethod
    def validate_webrtc_audio_backend(cls, value: str) -> str:
        normalized = value.strip().lower().replace("-", "_")
        if normalized not in {"websocket_pcm", "livekit"}:
            raise ValueError("实时音频传输后端必须是 websocket_pcm 或 livekit。")
        return normalized

    @field_validator("moss_tts_prompt_files")
    @classmethod
    def validate_moss_tts_prompt_files(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for voice, prompt_file in value.items():
            voice = str(voice).strip()
            prompt_file = str(prompt_file).strip()
            if not voice or len(voice) > 100 or not all(character.isalnum() or character in "_-" for character in voice):
                raise ValueError("MOSS-TTS 音色 ID 格式无效。")
            if not prompt_file or len(prompt_file) > 1_000 or "\x00" in prompt_file:
                raise ValueError("MOSS-TTS 提示音频路径无效。")
            normalized[voice] = prompt_file
        return normalized

    @model_validator(mode="after")
    def validate_lighttts_gate_deadlines(self) -> Settings:
        if self.lighttts_global_gate_lease_seconds < self.lighttts_job_timeout_seconds:
            raise ValueError("LightTTS 全局租约不得短于 whole-job timeout。")
        if self.livekit_audio_source_queue_ms % self.livekit_audio_frame_ms:
            raise ValueError("LiveKit AudioSource 队列必须是音频帧时长的整数倍。")
        if self.livekit_audio_app_queue_ms % self.livekit_audio_frame_ms:
            raise ValueError("LiveKit 应用队列必须是音频帧时长的整数倍。")
        if self.moss_tts_livekit_start_buffer_ms % self.livekit_audio_frame_ms:
            raise ValueError("MOSS LiveKit 起播缓冲必须是音频帧时长的整数倍。")
        if (
            self.webrtc_audio_enabled
            and self.webrtc_audio_backend == "livekit"
            and self.realtime_voice_backend == "moss_realtime"
            and self.moss_tts_realtime_enabled
            and self.livekit_audio_app_queue_ms < self.moss_tts_livekit_start_buffer_ms
        ):
            raise ValueError("LiveKit 应用队列不得短于 MOSS 起播缓冲。")
        if self.webrtc_audio_enabled and self.webrtc_audio_backend == "livekit":
            if not self.livekit_url.strip() or not self.livekit_api_key.strip() or not self.livekit_api_secret.strip():
                raise ValueError("启用 LiveKit 实时音频时必须配置 URL、API key 和 API secret。")
        if self.moss_tts_realtime_enabled:
            endpoints = {
                item.strip().rstrip("/")
                for item in [*self.moss_tts_realtime_urls, self.moss_tts_realtime_url]
                if item.strip()
            }
            if not endpoints:
                raise ValueError("启用 MOSS-TTS-Realtime 时必须配置至少一个 endpoint。")
            capacity = len(endpoints) * self.moss_tts_realtime_max_active_per_endpoint
            if self.moss_tts_realtime_max_active > capacity:
                raise ValueError("MOSS-TTS-Realtime 声明容量超过已配置 endpoint 的安全容量。")
            if self.moss_tts_realtime_max_active_per_endpoint != 1:
                raise ValueError("MOSS-TTS-Realtime 每个 endpoint 只允许 1 个活动会话。")
            required_voices = {f"debate_voice_{index}" for index in range(1, 9)}
            configured_voices = set(self.moss_tts_prompt_files)
            if configured_voices != required_voices:
                raise ValueError("启用 MOSS-TTS-Realtime 前必须完整配置 debate_voice_1 ... debate_voice_8。")
            if any(
                Path(prompt).name != prompt or not prompt.lower().endswith(".wav")
                for prompt in self.moss_tts_prompt_files.values()
            ):
                raise ValueError("MOSS-TTS-Realtime 音色提示必须是固定目录中的 WAV basename。")
        return self

    @property
    def origins(self) -> list[str]:
        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]

    @property
    def livekit_browser_url(self) -> str:
        return self.livekit_public_url.strip() or self.livekit_url.strip()

    @property
    def media_path(self) -> Path:
        path = Path(self.media_root).expanduser()
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[4] / path
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def archive_path(self) -> Path:
        path = Path(self.archive_root).expanduser()
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[4] / path
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def research_export_path(self) -> Path:
        path = Path(self.research_export_root).expanduser()
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[4] / path
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def backup_status_path(self) -> Path:
        path = Path(self.backup_status_file).expanduser()
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[4] / path
        return path.resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
