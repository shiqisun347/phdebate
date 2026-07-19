from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RegisterRequest(BaseModel):
    account: str = Field(min_length=1, max_length=128)
    real_name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=128)
    confirm_password: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def passwords_match(self) -> "RegisterRequest":
        if self.password != self.confirm_password:
            raise ValueError("两次输入的密码不一致。")
        return self


class LoginRequest(BaseModel):
    account: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)
    confirm_password: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def passwords_match(self) -> "ChangePasswordRequest":
        if self.new_password != self.confirm_password:
            raise ValueError("两次输入的新密码不一致。")
        return self


class CreateRoomRequest(BaseModel):
    competition_slug: str = Field(min_length=1, max_length=80)
    topic_id: str | None = Field(default=None, max_length=36)
    custom_topic: str | None = Field(default=None, max_length=300)
    seat_key: str = Field(min_length=1, max_length=24)
    visibility: Literal["public", "private"] = "public"

    @field_validator("custom_topic")
    @classmethod
    def normalize_custom_topic(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.strip().split())
        if not normalized:
            return None
        if len(normalized) < 4:
            raise ValueError("自定义辩题至少需要 4 个字符。")
        return normalized


class SeatRequest(BaseModel):
    seat_key: str


class ReadyRequest(BaseModel):
    ready: bool = True


class ControlLeaseRequest(BaseModel):
    force: bool = False


class ControlRequest(BaseModel):
    reason: str = Field(default="", max_length=300)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        return value.strip()


class FinishSpeechRequest(BaseModel):
    speech_id: str = Field(min_length=1, max_length=36)
    content: str = Field(default="", max_length=20000)


class AdminUserPatch(BaseModel):
    is_active: bool | None = None
    role: Literal["user", "system_admin"] | None = None
    is_test_account: bool | None = None


class AdminRoomDataScopePatch(BaseModel):
    is_test_data: bool


class AdminPasswordResetRequest(BaseModel):
    new_password: str = Field(min_length=1, max_length=128)
    confirm_password: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def passwords_match(self) -> "AdminPasswordResetRequest":
        if self.new_password != self.confirm_password:
            raise ValueError("两次输入的新密码不一致。")
        return self


class TopicCreate(BaseModel):
    title: str = Field(min_length=4, max_length=300)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = " ".join(value.strip().split())
        if len(normalized) < 4:
            raise ValueError("辩题至少需要 4 个字符。")
        return normalized


class TopicPatch(BaseModel):
    title: str | None = Field(default=None, min_length=4, max_length=300)
    is_active: bool | None = None

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.strip().split())
        if len(normalized) < 4:
            raise ValueError("辩题至少需要 4 个字符。")
        return normalized

    @model_validator(mode="after")
    def has_change(self) -> "TopicPatch":
        if self.title is None and self.is_active is None:
            raise ValueError("至少需要修改一个题目字段。")
        return self


class AgentProfilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    profile_key: str | None = Field(default=None, min_length=2, max_length=100, pattern=r"^[a-z][a-z0-9_-]+$")
    voice_id: str | None = Field(default=None, min_length=1, max_length=100)
    is_active: bool | None = None

    @field_validator("name", "profile_key", "voice_id")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("voice_id")
    @classmethod
    def validate_fixed_voice(cls, value: str | None) -> str | None:
        if value is None:
            return None
        allowed = {f"debate_voice_{index}" for index in range(1, 9)}
        if value not in allowed:
            raise ValueError("voice_id 必须是 debate_voice_1 ... debate_voice_8。")
        return value

    @model_validator(mode="after")
    def has_change(self) -> "AgentProfilePatch":
        if not self.model_dump(exclude_none=True):
            raise ValueError("至少需要修改一个 AI 配置字段。")
        return self


class JudgeProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    endpoint: str = Field(min_length=1, max_length=500)
    model_name: str = Field(min_length=1, max_length=120)
    system_prompt: str = Field(default="", max_length=10000)
    timeout_seconds: int = Field(default=120, ge=10, le=300)
    is_active: bool = True

    @field_validator("name", "endpoint", "model_name", "system_prompt")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError("裁判地址必须使用 http:// 或 https://。")
        return value

    @field_validator("model_name")
    @classmethod
    def forbid_qwen3_8b(cls, value: str) -> str:
        normalized = "".join(character for character in value.lower() if character.isalnum())
        if normalized.startswith("qwen38b"):
            raise ValueError("系统禁止使用 Qwen3 8B。")
        return value


class JudgeProfilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    endpoint: str | None = Field(default=None, min_length=1, max_length=500)
    model_name: str | None = Field(default=None, min_length=1, max_length=120)
    system_prompt: str | None = Field(default=None, max_length=10000)
    timeout_seconds: int | None = Field(default=None, ge=10, le=300)
    is_active: bool | None = None

    @field_validator("name", "endpoint", "model_name", "system_prompt")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str | None) -> str | None:
        if value is not None and not value.lower().startswith(("http://", "https://")):
            raise ValueError("裁判地址必须使用 http:// 或 https://。")
        return value

    @field_validator("model_name")
    @classmethod
    def forbid_qwen3_8b(cls, value: str | None) -> str | None:
        normalized = "".join(character for character in (value or "").lower() if character.isalnum())
        if normalized.startswith("qwen38b"):
            raise ValueError("系统禁止使用 Qwen3 8B。")
        return value

    @model_validator(mode="after")
    def has_change(self) -> "JudgeProfilePatch":
        if not self.model_dump(exclude_none=True):
            raise ValueError("至少需要修改一个裁判配置字段。")
        return self


class ProviderConfigUpsert(BaseModel):
    endpoint: str = Field(min_length=1, max_length=500)
    settings: dict[str, Any] = Field(default_factory=dict)
    secret: str | None = Field(default=None, max_length=500)
    clear_secret: bool = False
    is_active: bool = True

    @field_validator("endpoint")
    @classmethod
    def normalize_endpoint(cls, value: str) -> str:
        return value.strip()


class AudioCuePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    text: str | None = Field(default=None, max_length=2000)
    is_active: bool | None = None

    @field_validator("name", "text")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def has_change(self) -> "AudioCuePatch":
        if not self.model_dump(exclude_none=True):
            raise ValueError("至少需要修改一个预设语音字段。")
        return self


class AutomationStageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["announcement", "speech", "free", "judging"]
    duration: int = Field(ge=1, le=3600)
    cue: str | None = Field(default=None, max_length=500)
    seat: str | None = Field(default=None, pattern=r"^(aff|neg)_[1-9][0-9]*$")
    side: Literal["aff", "neg"] | None = None
    turn_duration: int | None = Field(default=None, ge=5, le=300)

    @field_validator("name", "cue")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def validate_kind_fields(self) -> "AutomationStageRequest":
        if self.kind == "speech" and not self.seat:
            raise ValueError("固定发言阶段必须指定 seat。")
        if self.kind != "speech" and self.seat:
            raise ValueError("只有固定发言阶段可以指定 seat。")
        if self.kind == "free" and (not self.side or not self.turn_duration):
            raise ValueError("自由辩论阶段必须指定初始阵营和单轮时长。")
        if self.kind != "free" and (self.side or self.turn_duration):
            raise ValueError("只有自由辩论阶段可以指定 side 和 turn_duration。")
        return self


class AutomationTemplateVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    stages: list[AutomationStageRequest] = Field(min_length=2, max_length=50)
    competition_ids: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def validate_flow(self) -> "AutomationTemplateVersionCreate":
        keys = [stage.key for stage in self.stages]
        if len(keys) != len(set(keys)):
            raise ValueError("自动流程阶段 key 不能重复。")
        judging_count = sum(stage.kind == "judging" for stage in self.stages)
        if judging_count != 1 or self.stages[-1].kind != "judging":
            raise ValueError("自动流程必须且只能有一个裁判阶段，并位于最后。")
        if not any(stage.kind in {"speech", "free"} for stage in self.stages):
            raise ValueError("自动流程至少需要一个发言或自由辩论阶段。")
        if len(self.competition_ids) != len(set(self.competition_ids)):
            raise ValueError("绑定赛事不能重复。")
        return self


class CompetitionPatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    tagline: str | None = Field(default=None, max_length=240)
    description: str | None = None
    rules: str | None = None
    is_active: bool | None = None
    season_id: str | None = Field(default=None, max_length=36)


class SeasonCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")
    starts_at: datetime
    ends_at: datetime | None = None
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("slug")
    @classmethod
    def normalize_slug(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("starts_at", "ends_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("赛季时间必须包含时区。")
        return value

    @model_validator(mode="after")
    def validate_dates(self) -> "SeasonCreate":
        if self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("赛季结束时间必须晚于开始时间。")
        return self


class SeasonPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("starts_at", "ends_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("赛季时间必须包含时区。")
        return value

    @model_validator(mode="after")
    def has_change(self) -> "SeasonPatch":
        if not self.model_fields_set:
            raise ValueError("至少需要修改一个赛季字段。")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("赛季名称不能为空。")
        if "starts_at" in self.model_fields_set and self.starts_at is None:
            raise ValueError("赛季开始时间不能为空。")
        if "is_active" in self.model_fields_set and self.is_active is None:
            raise ValueError("赛季启用状态不能为空。")
        return self


class JudgeReviewRequest(BaseModel):
    winner: Literal["aff", "neg", "draw"]
    affirmative_score: float = Field(ge=0, le=100)
    negative_score: float = Field(ge=0, le=100)
    reasoning: str = Field(min_length=2, max_length=5000)
    expected_updated_at: datetime

    @field_validator("reasoning")
    @classmethod
    def normalize_reasoning(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 2:
            raise ValueError("判定理由至少需要 2 个字符。")
        return normalized


class JudgeRetryRequest(BaseModel):
    expected_updated_at: datetime


class MediaCleanupRequest(BaseModel):
    dry_run: bool = True
    min_age_hours: int = Field(default=24, ge=1, le=8760)
    include_stale_parts: bool = True
