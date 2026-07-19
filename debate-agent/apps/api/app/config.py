from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../../.env"), extra="ignore")

    app_env: str = "development"
    app_secret: str = "development-agent-secret-change-me"
    encryption_secret: str = "development-agent-encryption-change-me"
    database_url: str = "sqlite:///./storage/debate-agent.db"
    redis_url: str = "redis://127.0.0.1:6379/4"
    public_origin: str = "http://127.0.0.1:3300"
    cookie_secure: bool = False
    session_hours: int = Field(default=12, ge=1, le=168)
    max_concurrent_generations: int = Field(default=8, ge=1, le=64)
    request_timeout_seconds: int = Field(default=180, ge=10, le=600)
    # LiteLLM waits for the first streamed chunk before returning its response
    # object on these gateways.  Debate generation uses a direct OpenAI SSE
    # path for latency-qualified models; judge/non-stream calls stay on LiteLLM.
    direct_openai_stream_models: str = "qwen-plus,qwen3.6-flash"
    interrupt_poll_interval_seconds: float = Field(default=0.1, ge=0.02, le=1.0)
    startup_model_warmup_enabled: bool = True
    startup_model_warmup_requests: int = Field(default=3, ge=1, le=3)
    startup_model_warmup_timeout_seconds: float = Field(default=30, ge=5, le=60)
    admin_account: str = ""
    admin_real_name: str = "Agent 管理员"
    admin_password: str = ""
    bootstrap_gateway_key: str = ""
    memory_enabled: bool = True
    memory_candidate_max_chars: int = Field(default=1200, ge=100, le=10000)
    mem0_enabled: bool = False
    mem0_config_json: str = "{}"
    auto_create_schema: bool = True

    @property
    def direct_openai_models(self) -> set[str]:
        return {item.strip() for item in self.direct_openai_stream_models.split(",") if item.strip()}

    @property
    def storage_path(self) -> Path:
        path = Path(__file__).resolve().parents[2] / "storage"
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
