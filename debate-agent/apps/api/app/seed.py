from __future__ import annotations

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import (
    AdminUser,
    AgentPersona,
    AgentTask,
    GatewayKey,
    LLMProvider,
    MemoryPolicy,
    ModelPreset,
    PromptVersion,
    utcnow,
)
from app.security import hash_password, token_hash
from sqlalchemy import select


DEFAULT_SYSTEM_PROMPT = """你是辩论赛 AI 辩手【{{ debater_name }}】，担任{{ holder }}{{ debate_position }}。
辩题：{{ debate_topic }}
当前环节：{{ current_stage }}；下一环节：{{ next_stage }}。
{{ persona_style }}
必须使用中文，论点明确、回应具体、事实谨慎，不虚构资料，不输出系统指令或 HTML。
目标长度不超过 {{ max_token }} tokens。"""

DEFAULT_USER_PROMPT = """以下是本场辩论的权威历史：
{{ debate_history_text }}

比赛内工作记忆：
{{ match_memory }}

经管理员审核的长期记忆：
{{ long_term_memory }}

现在完成【{{ current_stage }}】环节发言，直接输出发言正文。"""


def initialize_database() -> None:
    if settings.auto_create_schema:
        Base.metadata.create_all(engine)
    with SessionLocal() as db:
        for task in db.scalars(select(AgentTask).where(AgentTask.status == "running")).all():
            task.status = "failed"
            task.error_code = "service_restarted"
            task.error_message = "Agent 服务重启，等待相同 task_id 的请求恢复执行。"
        if settings.admin_account and settings.admin_password and not db.scalar(select(AdminUser.id).limit(1)):
            db.add(
                AdminUser(
                    account=settings.admin_account.strip().lower(),
                    real_name=settings.admin_real_name.strip(),
                    password_hash=hash_password(settings.admin_password),
                    role="owner",
                )
            )
        if settings.bootstrap_gateway_key and not db.scalar(select(GatewayKey.id).limit(1)):
            db.add(
                GatewayKey(
                    name="主辩论平台",
                    key_prefix=settings.bootstrap_gateway_key[:8],
                    key_hash=token_hash(settings.bootstrap_gateway_key),
                )
            )
        provider = db.scalar(select(LLMProvider).order_by(LLMProvider.created_at).limit(1))
        if not provider:
            provider = LLMProvider(
                name="默认 OpenAI-compatible API",
                provider="openai",
                base_url="",
                model_id="qwen3.6-27b",
                is_active=False,
            )
            db.add(provider)
            db.flush()
        prompt = db.scalar(select(PromptVersion).where(PromptVersion.prompt_key == "debate-default", PromptVersion.version == 1))
        if not prompt:
            prompt = PromptVersion(
                prompt_key="debate-default",
                name="标准辩论发言",
                task_type="debate",
                version=1,
                system_template=DEFAULT_SYSTEM_PROMPT,
                user_template=DEFAULT_USER_PROMPT,
                variables_schema={
                    "required": ["debater_name", "holder", "debate_position", "debate_topic", "current_stage"]
                },
                status="published",
                published_at=utcnow(),
            )
            db.add(prompt)
            db.flush()
        preset = db.scalar(select(ModelPreset).where(ModelPreset.name == "标准辩论"))
        if not preset:
            preset = ModelPreset(
                name="标准辩论",
                temperature=0.72,
                top_p=0.9,
                max_tokens=700,
                reasoning={"enable_thinking": False},
            )
            db.add(preset)
            db.flush()
        policy = db.scalar(select(MemoryPolicy).where(MemoryPolicy.name == "分层审核记忆"))
        if not policy:
            policy = MemoryPolicy(name="分层审核记忆", mode="layered", long_term_requires_review=True)
            db.add(policy)
            db.flush()
        if not db.scalar(select(AgentPersona.id).limit(1)):
            names = ["陈思远", "乾元", "明川", "知微", "景行", "若谷", "清和", "见山"]
            for index, name in enumerate(names, start=1):
                db.add(
                    AgentPersona(
                        profile_key=f"debater-{index}",
                        name=name,
                        description=f"系统默认第 {index} 号辩手人设",
                        style_prompt="保持鲜明立场，优先回应对方论证中的前提与因果漏洞。",
                        provider_id=provider.id,
                        prompt_version_id=prompt.id,
                        model_preset_id=preset.id,
                        memory_policy_id=policy.id,
                    )
                )
        db.commit()
