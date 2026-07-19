from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.security import hash_password, validate_account
from app.models.entities import AgentProfile, AutomationTemplate, Competition, CompetitionTopic, JudgeProfile, ProviderConfig, Season, User
from app.services.provider_config import default_agent_health_endpoint
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

DAILY_STAGES = [
    {"key": "opening", "name": "开场与规则", "kind": "announcement", "duration": 12, "cue": "欢迎来到稷下辩论日常赛，比赛即将开始。"},
    {"key": "aff_1_case", "name": "正方一辩立论", "kind": "speech", "seat": "aff_1", "duration": 180},
    {"key": "neg_1_case", "name": "反方一辩立论", "kind": "speech", "seat": "neg_1", "duration": 180},
    {"key": "aff_2_rebuttal", "name": "正方二辩驳论", "kind": "speech", "seat": "aff_2", "duration": 120},
    {"key": "neg_2_rebuttal", "name": "反方二辩驳论", "kind": "speech", "seat": "neg_2", "duration": 120},
    {"key": "aff_3_question", "name": "正方三辩质询", "kind": "speech", "seat": "aff_3", "duration": 120},
    {"key": "neg_3_question", "name": "反方三辩质询", "kind": "speech", "seat": "neg_3", "duration": 120},
    {"key": "free_debate", "name": "自由辩论", "kind": "free", "side": "aff", "duration": 300, "turn_duration": 45},
    {"key": "neg_4_summary", "name": "反方四辩总结", "kind": "speech", "seat": "neg_4", "duration": 180},
    {"key": "aff_4_summary", "name": "正方四辩总结", "kind": "speech", "seat": "aff_4", "duration": 180},
    {"key": "judging", "name": "AI 裁判评议", "kind": "judging", "duration": 30},
]

TRAINING_STAGES = [
    {"key": "opening", "name": "训练开始", "kind": "announcement", "duration": 8, "cue": "一对一辩论训练即将开始。"},
    {"key": "aff_1_case", "name": "正方立论", "kind": "speech", "seat": "aff_1", "duration": 150},
    {"key": "neg_1_case", "name": "反方立论", "kind": "speech", "seat": "neg_1", "duration": 150},
    {"key": "free_debate", "name": "自由辩论", "kind": "free", "side": "aff", "duration": 240, "turn_duration": 40},
    {"key": "neg_1_summary", "name": "反方总结", "kind": "speech", "seat": "neg_1", "duration": 120},
    {"key": "aff_1_summary", "name": "正方总结", "kind": "speech", "seat": "aff_1", "duration": 120},
    {"key": "judging", "name": "训练点评", "kind": "judging", "duration": 30},
]


def seed_database(db: Session) -> None:
    season = db.scalar(select(Season).where(Season.slug == "season-1"))
    if not season:
        season = Season(
            name="第一赛季",
            slug="season-1",
            starts_at=datetime.now(timezone.utc),
            ends_at=datetime.now(timezone.utc) + timedelta(days=180),
        )
        db.add(season)
        db.flush()

    daily_template = _template(db, "daily-4v4", "4v4 自动辩论流程", DAILY_STAGES, version=2)
    training_template = _template(db, "training-1v1", "1v1 自动训练流程", TRAINING_STAGES, version=2)

    daily = db.scalar(select(Competition).where(Competition.slug == "daily-4v4"))
    if not daily:
        daily = Competition(
            slug="daily-4v4",
            name="4v4 人机辩论正式赛",
            tagline="与真人并肩，让 AI 补齐阵容，完成一场完整辩论。",
            description="面向所有辩论爱好者的常态化赛事。创建者选择一个人类席位，其他玩家可加入，开始时空席自动由 AI 补齐。",
            rules="采用四人制流程，包括立论、驳论、质询、自由辩论和总结陈词。文明发言，禁止人身攻击。",
            format="4v4",
            seat_count=8,
            ranked=True,
            allow_custom_topic=False,
            accent="violet",
            automation_template_id=daily_template.id,
            season_id=season.id,
        )
        db.add(daily)
        db.flush()
    elif daily.name == "4v4 人机辩论日常赛":
        # Keep databases created by early V2 builds aligned with the public
        # competition name without overwriting an administrator's custom name.
        daily.name = "4v4 人机辩论正式赛"
    _ensure_topics(
        db,
        daily,
        [
            "人工智能时代，还要不要学编程？",
            "AI 的迅猛发展提升了还是降低了人类创作者存在的意义？",
            "信息爆炸时代，深度思考是否正在变得更稀缺？",
        ],
    )

    training = db.scalar(select(Competition).where(Competition.slug == "training-1v1"))
    if not training:
        training = Competition(
            slug="training-1v1",
            name="1v1 辩论训练赛",
            tagline="随时开局，与真人或 AI 打磨论证与临场反应。",
            description="轻量化一对一训练，可选择题库题目或自定义辩题，支持人机和人人对练。",
            rules="双方依次立论，进入自由辩论并完成总结。训练结果默认不计入赛季排行榜。",
            format="1v1",
            seat_count=2,
            ranked=False,
            allow_custom_topic=True,
            accent="cyan",
            automation_template_id=training_template.id,
            season_id=season.id,
        )
        db.add(training)
        db.flush()
    _ensure_topics(
        db,
        training,
        [
            "技术进步是否让人更自由？",
            "短视频正在提升还是削弱公众表达能力？",
            "年轻人应优先选择热爱还是稳定？",
        ],
    )

    if not db.scalar(select(AgentProfile).limit(1)):
        names = ["陈思远", "乾元", "明川", "知微", "景行", "若谷", "清和", "见山"]
        for index, name in enumerate(names, start=1):
            db.add(
                AgentProfile(
                    name=name,
                    profile_key=f"debater-{index}",
                    provider="debate_agent",
                    model_name=settings.agent_model_name,
                    endpoint=settings.agent_api_url,
                    voice_id=f"debate_voice_{index}",
                )
            )

    if settings.judge_api_url and not db.scalar(select(JudgeProfile).limit(1)):
        db.add(
            JudgeProfile(
                name="默认 AI 裁判",
                endpoint=settings.judge_api_url,
                model_name="judge-default",
                system_prompt="请依据论证质量、回应能力、事实准确性和表达清晰度进行公平裁决。",
                timeout_seconds=120,
                is_active=True,
            )
        )

    _ensure_provider_config(
        db,
        "agent",
        settings.agent_api_url,
        {
            "method": "POST",
            "protocol": "restful",
            "health_endpoint": default_agent_health_endpoint(settings.agent_api_url),
            "timeout_seconds": settings.agent_timeout_seconds,
            "stream": True,
        },
    )
    _ensure_provider_config(
        db,
        "funasr",
        settings.funasr_ws_url,
        {"final_wait_seconds": 30.0},
    )
    _ensure_provider_config(
        db,
        "lighttts",
        settings.lighttts_url,
        {"read_timeout_seconds": 180, "speed": 1.0},
    )

    if settings.v2_admin_account and settings.v2_admin_password:
        account = validate_account(settings.v2_admin_account)
        admin = db.scalar(select(User).where(User.account == account))
        if not admin:
            db.add(
                User(
                    account=account,
                    real_name=settings.v2_admin_real_name,
                    password_hash=hash_password(settings.v2_admin_password),
                    role="system_admin",
                )
            )
    db.commit()


def _template(db: Session, slug: str, name: str, stages: list[dict], *, version: int) -> AutomationTemplate:
    template = db.scalar(select(AutomationTemplate).where(AutomationTemplate.slug == slug, AutomationTemplate.version == version))
    if not template:
        template = AutomationTemplate(slug=slug, name=name, version=version, stages=stages)
        db.add(template)
        db.flush()
    return template


def _ensure_topics(db: Session, competition: Competition, titles: list[str]) -> None:
    existing = set(db.scalars(select(CompetitionTopic.title).where(CompetitionTopic.competition_id == competition.id)).all())
    for title in titles:
        if title not in existing:
            db.add(CompetitionTopic(competition_id=competition.id, title=title))


def _ensure_provider_config(db: Session, kind: str, endpoint: str, values: dict) -> None:
    if db.scalar(select(ProviderConfig.id).where(ProviderConfig.kind == kind)):
        return
    try:
        with db.begin_nested():
            db.add(
                ProviderConfig(
                    kind=kind,
                    endpoint=endpoint,
                    settings=values,
                    is_active=bool(endpoint),
                )
            )
            db.flush()
    except IntegrityError:
        # API and engine can seed concurrently on a fresh deployment. The
        # unique kind written by the other process is the desired outcome.
        pass
