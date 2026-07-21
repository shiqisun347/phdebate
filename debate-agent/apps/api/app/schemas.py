from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LoginRequest(BaseModel):
    account: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class LLMProviderInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: str = Field(default="openai", min_length=1, max_length=64)
    base_url: str = Field(default="", max_length=500)
    api_key: str | None = Field(default=None, max_length=1000)
    clear_api_key: bool = False
    model_id: str = Field(min_length=1, max_length=160)
    timeout_seconds: int = Field(default=120, ge=10, le=600)
    priority: int = Field(default=100, ge=1, le=10000)
    max_retries: int = Field(default=2, ge=0, le=10)
    rpm_limit: int = Field(default=60, ge=1, le=100000)
    is_active: bool = True

    @field_validator("model_id")
    @classmethod
    def forbid_qwen3_8b(cls, value: str) -> str:
        normalized = "".join(character for character in value.lower() if character.isalnum())
        if normalized.startswith("qwen38b"):
            raise ValueError("系统禁止使用 Qwen3 8B。")
        return value.strip()


class PromptInput(BaseModel):
    prompt_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,99}$")
    name: str = Field(min_length=1, max_length=120)
    task_type: str = Field(default="debate", max_length=64)
    system_template: str = Field(min_length=1, max_length=50000)
    user_template: str = Field(min_length=1, max_length=50000)
    variables_schema: dict[str, Any] = Field(default_factory=dict)


class PromptPreviewInput(BaseModel):
    system_template: str = Field(min_length=1, max_length=50000)
    user_template: str = Field(min_length=1, max_length=50000)
    variables: dict[str, Any] = Field(default_factory=dict)


class MessageTemplateInput(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,99}$")
    name: str = Field(min_length=1, max_length=120)
    task_type: str = Field(default="debate", max_length=64)
    stage_pattern: str = Field(default="*", max_length=160)
    role: str = Field(pattern=r"^(system|user|assistant)$")
    template: str = Field(min_length=1, max_length=50000)
    position: int = Field(default=100, ge=0, le=10000)
    is_active: bool = True


class ModelPresetInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    temperature: float = Field(default=0.7, ge=0, le=2)
    top_p: float = Field(default=0.9, gt=0, le=1)
    max_tokens: int = Field(default=700, ge=16, le=32000)
    seed: int | None = None
    stop: list[str] = Field(default_factory=list, max_length=20)
    presence_penalty: float = Field(default=0, ge=-2, le=2)
    frequency_penalty: float = Field(default=0, ge=-2, le=2)
    reasoning: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True


class MemoryPolicyInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    mode: str = Field(default="layered", pattern=r"^(disabled|match|layered)$")
    match_window_messages: int = Field(default=16, ge=1, le=200)
    retrieval_count: int = Field(default=5, ge=0, le=50)
    max_context_chars: int = Field(default=12000, ge=1000, le=100000)
    retention_days: int = Field(default=180, ge=1, le=3650)
    long_term_requires_review: bool = True
    is_active: bool = True


class PersonaInput(BaseModel):
    profile_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,99}$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=10000)
    style_prompt: str = Field(default="", max_length=20000)
    provider_id: str
    prompt_version_id: str
    model_preset_id: str
    memory_policy_id: str
    is_active: bool = True


class GatewayKeyInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class AdminCreateInput(BaseModel):
    account: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    real_name: str = Field(min_length=2, max_length=100)
    password: str = Field(min_length=10, max_length=128)
    role: str = Field(default="admin", pattern=r"^(admin|owner)$")


class DebateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model_name: str | None = None
    agent_profile: str | None = None
    debater_name: str = Field(min_length=1, max_length=100)
    debate_position: str = Field(min_length=1, max_length=50)
    debate_topic: str = Field(min_length=1, max_length=1000)
    current_stage: str = Field(min_length=1, max_length=200)
    next_stage: str = Field(default="比赛结束", max_length=200)
    holder: str = Field(pattern=r"^(正方|反方)$")
    debate_history: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    task_type: str = Field(default="debate", max_length=64)
    max_token: int = Field(default=700, ge=16, le=32000)
    match_id: str | None = Field(default=None, max_length=100)
    room_code: str | None = Field(default=None, max_length=32)
    task_id: str | None = Field(default=None, max_length=100)
    output: dict[str, Any] = Field(default_factory=lambda: {"stream": True, "language": "zh-CN"})


class ShouldSpeakRequest(DebateRequest):
    """A bounded free-debate intent check.

    The candidate speech is generated by a separate ``DebateRequest`` with a
    different task id.  Keeping the two operations independent lets the caller
    start them concurrently and cancel/discard the candidate when this decision
    is negative.
    """

    task_type: str = Field(default="should_speak", pattern=r"^should_speak$")
    max_token: int = Field(default=48, ge=16, le=96)


class JudgeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_type: str = Field(default="judge", pattern=r"^judge$")
    debate_topic: str = Field(min_length=1, max_length=1000)
    debate_history: list[dict[str, Any]] = Field(min_length=1, max_length=500)
    model_name: str | None = Field(default=None, max_length=160)
    system_prompt: str = Field(default="", max_length=10000)
    output: dict[str, Any] = Field(default_factory=lambda: {"stream": False, "language": "zh-CN"})
