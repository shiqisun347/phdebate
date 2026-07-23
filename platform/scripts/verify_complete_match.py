#!/usr/bin/env python3
"""Verify a complete public-API debate lifecycle against a running platform.

The verifier uses the same REST and WebSocket boundaries as browsers.  It can
exercise a 1v1 human/AI match, a 1v1 two-human match, or a mixed 4v4 match.  It
never edits the database directly and always releases an unfinished room.

The default run is deliberately bounded: it covers both sides of free debate,
then uses the room owner's documented skip control to continue through every
summary and the real judge.  ``--full-free-duration`` disables that shortcut.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import secrets
import ssl
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
import websockets

PASSWORD = "Complete-match-1234"
TERMINAL_STATUSES = {"completed", "review_required", "terminated", "cancelled"}
FORBIDDEN_SUCCESS_EVENTS = {
    "provider.failed",
    "engine.quarantined",
    "judge.review_required",
    # ``--exercise-reconnect`` deliberately reconnects within 60 seconds. A
    # timeout event means the short-disconnect contract failed even if an
    # automatic recovery later made the final room look completed.
    "participant.disconnect_timeout",
}
TRAINING_STAGE_KEYS = (
    "opening",
    "aff_1_case",
    "neg_1_case",
    "free_debate",
    "neg_1_summary",
    "aff_1_summary",
    "judging",
)
DAILY_STAGE_KEYS = (
    "opening",
    "aff_1_case",
    "neg_1_case",
    "aff_2_rebuttal",
    "neg_2_rebuttal",
    "aff_3_question",
    "neg_3_question",
    "free_debate",
    "neg_4_summary",
    "aff_4_summary",
    "judging",
)


class VerificationFailure(RuntimeError):
    """A product invariant failed during the end-to-end run."""


@dataclass(frozen=True)
class Scenario:
    name: str
    competition_slug: str
    human_seats: tuple[str, ...]
    expected_stage_keys: tuple[str, ...]
    require_ai_speech: bool


SCENARIOS = {
    "1v1-human-ai": Scenario(
        name="1v1-human-ai",
        competition_slug="training-1v1",
        human_seats=("aff_1",),
        expected_stage_keys=TRAINING_STAGE_KEYS,
        require_ai_speech=True,
    ),
    "1v1-two-human": Scenario(
        name="1v1-two-human",
        competition_slug="training-1v1",
        human_seats=("aff_1", "neg_1"),
        expected_stage_keys=TRAINING_STAGE_KEYS,
        require_ai_speech=False,
    ),
    "4v4-mixed": Scenario(
        name="4v4-mixed",
        competition_slug="daily-4v4",
        # Four independent browsers exercise joining, readiness, turn
        # ownership and free-debate ordering; the remaining four seats are AI.
        human_seats=("aff_1", "neg_1", "aff_2", "neg_2"),
        expected_stage_keys=DAILY_STAGE_KEYS,
        require_ai_speech=True,
    ),
}


@dataclass
class Audit:
    scenario: str
    room_code: str = ""
    started_at: float = field(default_factory=time.monotonic)
    stage_keys_seen: list[str] = field(default_factory=list)
    stage_transitions: list[dict[str, Any]] = field(default_factory=list)
    human_speech_ids: set[str] = field(default_factory=set)
    ai_speech_ids: set[str] = field(default_factory=set)
    ai_playback_ids: set[str] = field(default_factory=set)
    free_request_ids: set[str] = field(default_factory=set)
    free_speech_ids: set[str] = field(default_factory=set)
    recovery_actions: list[dict[str, Any]] = field(default_factory=list)
    opening_latency_seconds: float | None = None
    reconnect_duration_seconds: float | None = None

    def observe(self, state: dict[str, Any]) -> None:
        current = state.get("current_stage") or {}
        key = str(current.get("key") or "")
        if key and (not self.stage_keys_seen or self.stage_keys_seen[-1] != key):
            self.stage_keys_seen.append(key)
            self.stage_transitions.append(
                {
                    "stage": key,
                    "status": state.get("status"),
                    "elapsed_seconds": round(time.monotonic() - self.started_at, 3),
                }
            )
        active = state.get("active_speech") or {}
        speech_id = str(active.get("id") or "")
        if not speech_id:
            return
        if active.get("speaker_type") == "ai":
            self.ai_speech_ids.add(speech_id)
            if active.get("playback_started_at"):
                self.ai_playback_ids.add(speech_id)
        elif active.get("speaker_type") == "human":
            self.human_speech_ids.add(speech_id)
        if current.get("key") == "free_debate":
            self.free_speech_ids.add(speech_id)

    def summary(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "room_code": self.room_code,
            "opening_latency_seconds": self.opening_latency_seconds,
            "reconnect_duration_seconds": self.reconnect_duration_seconds,
            "stage_keys_seen": self.stage_keys_seen,
            "human_speech_count": len(self.human_speech_ids),
            "ai_speech_count": len(self.ai_speech_ids),
            "ai_playback_count": len(self.ai_playback_ids),
            "free_speech_count": len(self.free_speech_ids),
            "free_request_count": len(self.free_request_ids),
            "recovery_actions": self.recovery_actions,
            "elapsed_seconds": round(time.monotonic() - self.started_at, 3),
        }


def csrf(client: httpx.AsyncClient) -> dict[str, str]:
    token = client.cookies.get("jixia_csrf")
    return {"X-CSRF-Token": token} if token else {}


def error_body(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(payload, dict) and payload.get("detail"):
        return str(payload["detail"])[:500]
    return json.dumps(payload, ensure_ascii=False)[:500]


def require_response(response: httpx.Response, label: str, expected: set[int] | None = None) -> httpx.Response:
    expected = expected or {200}
    if response.status_code not in expected:
        raise VerificationFailure(f"{label}: HTTP {response.status_code}: {error_body(response)}")
    return response


def state_summary(state: dict[str, Any]) -> dict[str, Any]:
    """Return diagnostics without copying transcripts or private event payloads."""

    current = state.get("current_stage") or {}
    active = state.get("active_speech") or {}
    queue = state.get("free_turn_queue") or {}
    return {
        "status": state.get("status"),
        "stage": current.get("key"),
        "kind": current.get("kind"),
        "host_announcement_pending": bool(current.get("host_announcement_pending")),
        "side": current.get("side"),
        "remaining_seconds": state.get("remaining_seconds"),
        "turn_remaining_seconds": state.get("turn_remaining_seconds"),
        "active_speech": {
            "id": active.get("id"),
            "seat_key": active.get("seat_key"),
            "speaker_type": active.get("speaker_type"),
            "status": active.get("status"),
            "playback_started": bool(active.get("playback_started_at")),
        }
        if active
        else None,
        "can_speak": state.get("can_speak"),
        "speak_reason": state.get("speak_reason"),
        "free_request": {
            "can_request": queue.get("can_request"),
            "request_reason": queue.get("request_reason"),
            "target_side": queue.get("target_side"),
            "window_remaining_ms": queue.get("window_remaining_ms"),
        },
        "failure_reason": state.get("failure_reason"),
        "seq": state.get("seq"),
    }


@dataclass
class Actor:
    account: str
    real_name: str
    client: httpx.AsyncClient
    seat_key: str | None = None
    socket: Any = None
    socket_task: asyncio.Task | None = None

    async def room(self, code: str) -> dict[str, Any]:
        response = require_response(await self.client.get(f"/api/rooms/{code}"), f"{self.real_name} 读取房间")
        return response.json()["room"]

    async def connect_presence(self, base_url: str, code: str, insecure: bool) -> None:
        await self.disconnect_presence()
        cookie_header = "; ".join(f"{key}={value}" for key, value in self.client.cookies.items())
        ws_url = base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + f"/ws/rooms/{code}"
        kwargs: dict[str, Any] = {"additional_headers": {"Cookie": cookie_header}, "open_timeout": 20}
        if ws_url.startswith("wss://"):
            ssl_context = ssl.create_default_context()
            if insecure:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
            kwargs["ssl"] = ssl_context
        self.socket = await websockets.connect(ws_url, **kwargs)
        raw = await asyncio.wait_for(self.socket.recv(), timeout=20)
        snapshot = json.loads(raw)
        if (snapshot.get("room") or {}).get("code") != code:
            raise VerificationFailure(f"{self.real_name} WebSocket 未返回目标房间快照")
        self.socket_task = asyncio.create_task(self._drain_socket(), name=f"verify-ws-{code}-{self.account}")

    async def _drain_socket(self) -> None:
        try:
            async for _message in self.socket:
                pass
        except Exception:
            # The authoritative assertion happens through REST. A closed socket
            # is detected by the connected-seat checks before a human turn.
            return

    async def disconnect_presence(self) -> None:
        if self.socket is not None:
            with contextlib.suppress(Exception):
                await self.socket.close()
        if self.socket_task is not None:
            self.socket_task.cancel()
            await asyncio.gather(self.socket_task, return_exceptions=True)
        self.socket = None
        self.socket_task = None

    async def close(self) -> None:
        await self.disconnect_presence()
        await self.client.aclose()


class CompleteMatchVerifier:
    def __init__(
        self,
        *,
        base_url: str,
        insecure: bool,
        scenario: Scenario,
        timeout_seconds: float,
        max_opening_seconds: float,
        free_turns: int,
        full_free_duration: bool,
        exercise_pause_resume: bool,
        exercise_reconnect: bool,
        exercise_ai_reset: bool,
        auto_recover: bool,
        allow_unclassified_test_data: bool,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.insecure = insecure
        self.verify_tls = not insecure
        self.scenario = scenario
        self.timeout_seconds = timeout_seconds
        self.max_opening_seconds = max_opening_seconds
        self.free_turns = free_turns
        self.full_free_duration = full_free_duration
        self.exercise_pause_resume = exercise_pause_resume
        self.exercise_reconnect = exercise_reconnect
        self.exercise_ai_reset = exercise_ai_reset
        self.auto_recover = auto_recover
        self.allow_unclassified_test_data = allow_unclassified_test_data
        self.suffix = f"{int(time.time())}{secrets.token_hex(3)}"
        self.actors: list[Actor] = []
        self.actor_by_seat: dict[str, Actor] = {}
        self.owner: Actor | None = None
        self.admin: Actor | None = None
        self.code: str | None = None
        self.audit = Audit(scenario=scenario.name)
        self._pause_exercised = False
        self._reconnect_exercised = False
        self._ai_reset_exercised = False
        self._recovery_attempts: dict[str, int] = {}
        self._free_request_keys: set[str] = set()

    def client(self, *, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            verify=self.verify_tls,
            timeout=httpx.Timeout(timeout or 30, connect=20),
            follow_redirects=True,
            headers={"User-Agent": "phdebate-complete-match-verifier/1"},
        )

    async def preflight(self) -> None:
        async with self.client(timeout=45) as client:
            response = await client.get("/api/health/ready")
            if response.status_code != 200:
                checks: list[str] = []
                with contextlib.suppress(ValueError):
                    body = response.json()
                    checks = [name for name, value in (body.get("checks") or {}).items() if not value.get("ok")]
                suffix = f"；失败项：{', '.join(checks)}" if checks else ""
                raise VerificationFailure(f"系统 readiness 未通过（HTTP {response.status_code}）{suffix}")
            if not response.json().get("ok"):
                raise VerificationFailure("系统 readiness 返回 ok=false")

    async def _register_actor(self, seat_key: str, index: int) -> Actor:
        client = self.client()
        actor = Actor(
            account=f"complete_{self.suffix}_{index}",
            real_name=f"全流程验收{index + 1}",
            client=client,
            seat_key=seat_key,
        )
        response = await client.post(
            "/api/auth/register",
            json={
                "account": actor.account,
                "real_name": actor.real_name,
                "password": PASSWORD,
                "confirm_password": PASSWORD,
            },
        )
        require_response(response, f"注册 {actor.real_name}")
        self.actors.append(actor)
        return actor

    async def _open_admin(self) -> Actor | None:
        account = os.environ.get("PHDEBATE_ADMIN_ACCOUNT", "")
        password = os.environ.get("PHDEBATE_ADMIN_PASSWORD", "")
        if not account or not password:
            if self.allow_unclassified_test_data:
                return None
            raise VerificationFailure(
                "缺少 PHDEBATE_ADMIN_ACCOUNT/PHDEBATE_ADMIN_PASSWORD；"
                "生产验收必须先把临时账号标为测试数据，或在本地显式使用 --allow-unclassified-test-data"
            )
        client = self.client()
        response = await client.post("/api/auth/login", json={"account": account, "password": password})
        require_response(response, "管理员登录")
        actor = Actor(account=account, real_name="系统管理员", client=client)
        self.admin = actor
        return actor

    async def _classify_test_actor(self, actor: Actor) -> None:
        if self.admin is None:
            return
        response = require_response(
            await self.admin.client.get("/api/admin/users", params={"q": actor.account, "page_size": 20}),
            f"查询测试账号 {actor.account}",
        )
        matches = [item for item in response.json().get("items", []) if item.get("account") == actor.account]
        if len(matches) != 1:
            raise VerificationFailure(f"无法唯一定位测试账号 {actor.account}")
        patch = await self.admin.client.patch(
            f"/api/admin/users/{matches[0]['id']}",
            headers=csrf(self.admin.client),
            json={"is_test_account": True},
        )
        require_response(patch, f"标记测试账号 {actor.account}")
        if not patch.json().get("user", {}).get("is_test_account"):
            raise VerificationFailure(f"测试账号 {actor.account} 未被正确分类")

    async def setup(self) -> None:
        await self._open_admin()
        for index, seat_key in enumerate(self.scenario.human_seats):
            actor = await self._register_actor(seat_key, index)
            await self._classify_test_actor(actor)
            self.actor_by_seat[seat_key] = actor
        self.owner = self.actor_by_seat[self.scenario.human_seats[0]]

        payload: dict[str, Any] = {
            "competition_slug": self.scenario.competition_slug,
            "seat_key": self.scenario.human_seats[0],
            "visibility": "public",
        }
        if self.scenario.competition_slug == "training-1v1":
            payload["custom_topic"] = "完整比赛验收：人工智能时代，还要不要学习编程？"
        else:
            competition = require_response(
                await self.owner.client.get(f"/api/competitions/{self.scenario.competition_slug}"),
                "读取 4v4 赛事题库",
            ).json()["competition"]
            topics = [item for item in competition.get("topics", []) if item.get("is_active", True)]
            if not topics:
                raise VerificationFailure("4v4 赛事没有可用题目")
            payload["topic_id"] = topics[0]["id"]

        created = await self.owner.client.post(
            "/api/rooms",
            headers=csrf(self.owner.client) | {"X-Idempotency-Key": f"complete-create-{self.suffix}"},
            json=payload,
        )
        require_response(created, "创建验收房间")
        self.code = created.json()["room"]["code"]
        self.audit.room_code = self.code

        for seat_key, actor in self.actor_by_seat.items():
            if actor is self.owner:
                continue
            claim = await actor.client.post(
                f"/api/rooms/{self.code}/claim-seat",
                headers=csrf(actor.client),
                json={"seat_key": seat_key},
            )
            require_response(claim, f"{actor.real_name} 认领 {seat_key}")

        # Presence is part of free-debate eligibility. Merely calling REST is
        # not a realistic participant session and leaves connected=false.
        await asyncio.gather(
            *(actor.connect_presence(self.base_url, self.code, self.insecure) for actor in self.actors)
        )
        for actor in self.actors:
            await self.wait_for(
                actor,
                lambda value, seat=actor.seat_key: any(
                    item.get("seat_key") == seat and item.get("connected") for item in value.get("seats", [])
                ),
                f"{actor.real_name} 实时在线",
                timeout=20,
                recover=False,
            )

        for actor in self.actors:
            ready = await actor.client.post(
                f"/api/rooms/{self.code}/ready", headers=csrf(actor.client), json={"ready": True}
            )
            require_response(ready, f"{actor.real_name} 准备")

    async def state(self, actor: Actor | None = None) -> dict[str, Any]:
        selected_actor = actor or self.owner
        if not self.code or not selected_actor:
            raise VerificationFailure("房间尚未创建")
        value = await selected_actor.room(self.code)
        self.audit.observe(value)
        return value

    async def _recover_pause(self, state: dict[str, Any], label: str) -> None:
        if not self.auto_recover or not self.owner or not self.code:
            raise VerificationFailure(f"{label}: 比赛异常暂停：{state_summary(state)}")
        stage_key = str((state.get("current_stage") or {}).get("key") or "preparing")
        attempts = self._recovery_attempts.get(stage_key, 0)
        if attempts >= 1:
            raise VerificationFailure(f"{label}: 同一阶段重复异常暂停：{state_summary(state)}")
        self._recovery_attempts[stage_key] = attempts + 1
        action = "retry" if state.get("failure_reason") else "resume"
        response = await self.owner.client.post(
            f"/api/rooms/{self.code}/control/{action}",
            headers=csrf(self.owner.client) | {"X-Idempotency-Key": f"complete-recover-{stage_key}-{attempts}"},
            json={"reason": f"全流程验收自动恢复：{label}"},
        )
        require_response(response, f"{label} 自动{action}")
        self.audit.recovery_actions.append({"stage": stage_key, "action": action})

    async def wait_for(
        self,
        actor: Actor,
        predicate: Callable[[dict[str, Any]], bool],
        label: str,
        *,
        timeout: float | None = None,
        recover: bool = True,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + (timeout or self.timeout_seconds)
        latest: dict[str, Any] = {}
        while time.monotonic() < deadline:
            latest = await self.state(actor)
            if latest.get("status") == "paused" and recover:
                await self._recover_pause(latest, label)
                await asyncio.sleep(0.5)
                continue
            if predicate(latest):
                return latest
            await asyncio.sleep(0.5)
        raise VerificationFailure(f"{label}: 等待超时：{state_summary(latest)}")

    async def start(self) -> None:
        assert self.owner and self.code
        started_at = time.monotonic()
        response = await self.owner.client.post(f"/api/rooms/{self.code}/start", headers=csrf(self.owner.client), json={})
        require_response(response, "房主开始比赛")
        state = await self.wait_for(
            self.owner,
            lambda value: value.get("status") == "running" and bool(value.get("current_stage")),
            "开场进入运行态",
            timeout=max(self.max_opening_seconds + 5, 20),
        )
        latency = round(time.monotonic() - started_at, 3)
        self.audit.opening_latency_seconds = latency
        if latency > self.max_opening_seconds:
            raise VerificationFailure(
                f"开场准备耗时 {latency}s，超过上限 {self.max_opening_seconds}s；"
                f"主持提示音必须是全局预生成资产：{state_summary(state)}"
            )
        if (state.get("current_stage") or {}).get("key") != "opening":
            raise VerificationFailure(f"比赛没有从 opening 开始：{state_summary(state)}")

    def actor_for_seat(self, seat_key: str | None) -> Actor | None:
        return self.actor_by_seat.get(str(seat_key or ""))

    @staticmethod
    def side_for_seat(state: dict[str, Any], seat_key: str | None) -> str | None:
        return next(
            (item.get("side") for item in state.get("seats", []) if item.get("seat_key") == seat_key),
            None,
        )

    def first_human_on_side(self, state: dict[str, Any], side: str) -> Actor | None:
        for seat in state.get("seats", []):
            if seat.get("side") == side and seat.get("occupant_type") == "human":
                actor = self.actor_for_seat(seat.get("seat_key"))
                if actor:
                    return actor
        return None

    async def request_free_turn_if_possible(self, actor: Actor | None) -> bool:
        if not actor or not self.code:
            return False
        state = await self.state(actor)
        queue = state.get("free_turn_queue") or {}
        if queue.get("my_request"):
            return True
        if not queue.get("can_request"):
            return False
        turn = queue.get("target_turn_seq")
        key = f"complete-free-{self.code}-{actor.seat_key}-{turn}"
        response = await actor.client.post(
            f"/api/rooms/{self.code}/free-turn-requests",
            headers=csrf(actor.client) | {"X-Idempotency-Key": key},
            json={},
        )
        require_response(response, f"{actor.real_name} 申请自由辩论")
        request_id = str(response.json().get("request_id") or "")
        if request_id:
            self.audit.free_request_ids.add(request_id)
        self._free_request_keys.add(key)
        return True

    async def acquire_lease(self, actor: Actor) -> str:
        assert self.code
        lease = f"complete-{self.suffix}-{actor.seat_key}"
        response = await actor.client.post(
            f"/api/rooms/{self.code}/control-lease",
            headers=csrf(actor.client) | {"X-Control-Lease": lease},
            json={},
        )
        require_response(response, f"{actor.real_name} 获取设备控制权")
        return lease

    async def speak(
        self,
        actor: Actor,
        text: str,
        key: str,
        *,
        request_next_actor: Actor | None = None,
    ) -> str:
        assert self.code
        ready = await self.wait_for(
            actor,
            lambda value: bool(value.get("can_speak")) and not value.get("active_speech"),
            f"等待 {actor.real_name} 可发言",
        )
        seat = next((item for item in ready.get("seats", []) if item.get("is_me")), {})
        if not seat.get("connected"):
            raise VerificationFailure(f"{actor.real_name} 发言前已离线")
        lease = await self.acquire_lease(actor)
        common = csrf(actor.client) | {"X-Control-Lease": lease}
        started = await actor.client.post(
            f"/api/rooms/{self.code}/speech/start",
            headers=common | {"X-Idempotency-Key": f"complete-start-{self.code}-{key}"},
            json={},
        )
        require_response(started, f"{actor.real_name} 开始发言")
        speech_id = str(started.json().get("speech_id") or "")
        if not speech_id:
            raise VerificationFailure(f"{actor.real_name} 开始发言未返回 speech_id")
        self.audit.human_speech_ids.add(speech_id)
        if (ready.get("current_stage") or {}).get("key") == "free_debate":
            self.audit.free_speech_ids.add(speech_id)
            # A next-side human must raise their hand while the opponent is
            # actually speaking. Finishing first closes the realistic window.
            await self.request_free_turn_if_possible(request_next_actor)
        finished = await actor.client.post(
            f"/api/rooms/{self.code}/speech/finish",
            headers=common | {"X-Idempotency-Key": f"complete-finish-{self.code}-{key}"},
            json={"speech_id": speech_id, "content": text},
        )
        require_response(finished, f"{actor.real_name} 结束发言")
        return speech_id

    async def exercise_controls(self, actor: Actor) -> None:
        if not self.exercise_pause_resume or self._pause_exercised or not self.owner or not self.code:
            return
        before = await self.state(self.owner)
        if before.get("status") != "running" or before.get("active_speech"):
            return
        paused = await self.owner.client.post(
            f"/api/rooms/{self.code}/control/pause",
            headers=csrf(self.owner.client) | {"X-Idempotency-Key": f"complete-pause-{self.code}"},
            json={"reason": "全流程验收：验证房主暂停与倒计时冻结"},
        )
        require_response(paused, "房主暂停比赛")
        paused_state = paused.json()["room"]
        frozen = paused_state.get("remaining_seconds")
        await asyncio.sleep(1.2)
        still_paused = await self.state(self.owner)
        if still_paused.get("status") != "paused" or still_paused.get("remaining_seconds") != frozen:
            raise VerificationFailure(
                f"暂停后倒计时没有冻结：before={frozen}, after={still_paused.get('remaining_seconds')}"
            )
        resumed = await self.owner.client.post(
            f"/api/rooms/{self.code}/control/resume",
            headers=csrf(self.owner.client) | {"X-Idempotency-Key": f"complete-resume-{self.code}"},
            json={"reason": "全流程验收：恢复比赛"},
        )
        require_response(resumed, "房主恢复比赛")
        await self.wait_for(actor, lambda value: value.get("status") == "running", "暂停后恢复运行", timeout=15)
        self.audit.recovery_actions.append({"stage": (before.get("current_stage") or {}).get("key"), "action": "pause_resume"})
        self._pause_exercised = True

    async def exercise_reconnect_once(self, actor: Actor) -> None:
        if not self.exercise_reconnect or self._reconnect_exercised or not self.code:
            return
        disconnected_at = time.monotonic()
        await actor.disconnect_presence()
        await self.wait_for(
            actor,
            lambda value: any(
                item.get("seat_key") == actor.seat_key and not item.get("connected") for item in value.get("seats", [])
            ),
            f"{actor.real_name} 断线状态落盘",
            timeout=20,
            recover=False,
        )
        await actor.connect_presence(self.base_url, self.code, self.insecure)
        restored = await self.wait_for(
            actor,
            lambda value: any(
                item.get("seat_key") == actor.seat_key
                and item.get("connected")
                and item.get("occupant_type") == "human"
                for item in value.get("seats", [])
            ),
            f"{actor.real_name} 60 秒内断线恢复",
            timeout=20,
            recover=False,
        )
        reconnect_duration = round(time.monotonic() - disconnected_at, 3)
        self.audit.reconnect_duration_seconds = reconnect_duration
        if reconnect_duration >= 60:
            raise VerificationFailure(f"{actor.real_name} 短断线恢复耗时 {reconnect_duration}s，已超过 60 秒边界")
        if restored.get("status") != "running" or restored.get("failure_reason"):
            raise VerificationFailure(
                f"{actor.real_name} 60 秒内重连却改变了比赛运行态：{state_summary(restored)}"
            )
        if not any(item.get("is_me") for item in restored.get("seats", [])):
            raise VerificationFailure(f"{actor.real_name} 重连后丢失席位身份")
        self.audit.recovery_actions.append({"stage": (restored.get("current_stage") or {}).get("key"), "action": "reconnect"})
        self._reconnect_exercised = True

    async def run_fixed_human_stage(self, state: dict[str, Any], actor: Actor) -> None:
        assert self.owner
        current = state.get("current_stage") or {}
        key = str(current.get("key"))
        await self.exercise_controls(actor)
        await self.exercise_reconnect_once(actor)
        await self.speak(
            actor,
            f"{actor.real_name} 在 {current.get('name')} 中完成真实流程验收发言。"
            "我方将定义、论据和结论清楚连接，并回应本场辩题。",
            key,
        )
        await self.wait_for(
            self.owner,
            lambda value: (value.get("current_stage") or {}).get("key") != key or value.get("status") in TERMINAL_STATUSES,
            f"{key} 真人发言后推进",
        )

    async def run_ai_stage(self, state: dict[str, Any]) -> None:
        assert self.owner
        key = str((state.get("current_stage") or {}).get("key"))
        observed_ai = False
        observed_playback = False

        if self.exercise_ai_reset and not self._ai_reset_exercised:
            playing = await self.wait_for(
                self.owner,
                lambda value: (
                    (value.get("active_speech") or {}).get("speaker_type") == "ai"
                    and bool((value.get("active_speech") or {}).get("playback_started_at"))
                ),
                f"{key} 等待 AI 实际播放后执行房主重置",
            )
            previous_id = str((playing.get("active_speech") or {}).get("id") or "")
            reset = await self.owner.client.post(
                f"/api/rooms/{self.code}/control/reset-speech",
                headers=csrf(self.owner.client) | {"X-Idempotency-Key": f"complete-reset-ai-{self.code}"},
                json={"reason": "全流程验收：模拟 AI 声音完全卡住"},
            )
            require_response(reset, f"{key} 房主重置当前 AI 发言")
            restarted = await self.wait_for(
                self.owner,
                lambda value: (
                    (value.get("active_speech") or {}).get("speaker_type") == "ai"
                    and str((value.get("active_speech") or {}).get("id") or "") not in {"", previous_id}
                    and bool((value.get("active_speech") or {}).get("playback_started_at"))
                ),
                f"{key} 重置后在同阶段重新播放",
            )
            if (restarted.get("current_stage") or {}).get("key") != key:
                raise VerificationFailure(f"{key} 重置 AI 发言时错误推进了阶段")
            observed_ai = True
            observed_playback = True
            self.audit.recovery_actions.append({"stage": key, "action": "reset_ai_speech"})
            self._ai_reset_exercised = True

        def completed(value: dict[str, Any]) -> bool:
            nonlocal observed_ai, observed_playback
            active = value.get("active_speech") or {}
            if active.get("speaker_type") == "ai":
                observed_ai = True
                observed_playback = observed_playback or bool(active.get("playback_started_at"))
            return (value.get("current_stage") or {}).get("key") != key or value.get("status") in TERMINAL_STATUSES

        await self.wait_for(self.owner, completed, f"{key} AI 生成、播放并推进")
        if not observed_ai:
            raise VerificationFailure(f"{key} 没有观察到 AI 发言活动")
        if not observed_playback:
            raise VerificationFailure(f"{key} 没有观察到 AI 实际播放开始")

    async def run_free_stage(self) -> None:
        assert self.owner and self.code
        turns_observed: set[str] = set()
        skip_requested = False
        deadline = time.monotonic() + max(self.timeout_seconds * 3, 480)
        while time.monotonic() < deadline:
            state = await self.state(self.owner)
            if state.get("status") == "paused":
                await self._recover_pause(state, "自由辩论")
                continue
            current = state.get("current_stage") or {}
            if current.get("key") != "free_debate":
                if len(turns_observed) < self.free_turns:
                    raise VerificationFailure(
                        f"自由辩论仅覆盖 {len(turns_observed)} 轮，要求至少 {self.free_turns} 轮"
                    )
                return
            active = state.get("active_speech") or {}
            active_id = str(active.get("id") or "")
            if active_id:
                turns_observed.add(active_id)
                if active.get("speaker_type") == "ai" and active.get("playback_started_at"):
                    active_side = self.side_for_seat(state, active.get("seat_key"))
                    next_side = "neg" if active_side == "aff" else "aff"
                    await self.request_free_turn_if_possible(self.first_human_on_side(state, next_side))
                await asyncio.sleep(0.5)
                continue

            if len(turns_observed) >= self.free_turns and not self.full_free_duration:
                if not skip_requested:
                    skipped = await self.owner.client.post(
                        f"/api/rooms/{self.code}/control/skip",
                        headers=csrf(self.owner.client)
                        | {"X-Idempotency-Key": f"complete-skip-free-{self.code}"},
                        json={"reason": f"全流程验收已覆盖 {len(turns_observed)} 轮自由辩论"},
                    )
                    require_response(skipped, "覆盖双边发言后跳过剩余自由辩论时长")
                    skip_requested = True
                await asyncio.sleep(0.5)
                continue

            if current.get("host_announcement_pending") or current.get("intermission_deadline_at"):
                target_side = str(current.get("intermission_side") or "")
                if target_side:
                    await self.request_free_turn_if_possible(self.first_human_on_side(state, target_side))
                await asyncio.sleep(0.25)
                continue

            side = str(current.get("side") or "")
            actor = self.first_human_on_side(state, side)
            if actor:
                actor_state = await self.state(actor)
                if actor_state.get("can_speak"):
                    next_side = "neg" if side == "aff" else "aff"
                    next_actor = self.first_human_on_side(state, next_side)
                    speech_id = await self.speak(
                        actor,
                        f"{actor.real_name} 完成自由辩论第 {len(turns_observed) + 1} 轮。"
                        "我方直接回应上一轮，并提出一个可以继续交锋的核心判断。",
                        f"free-{len(turns_observed) + 1}-{current.get('turn_seq', 0)}",
                        request_next_actor=next_actor,
                    )
                    turns_observed.add(speech_id)
                    continue
            # No eligible human: the authoritative engine must start an AI
            # fallback. Do not issue owner controls that would hide a stuck AI.
            await asyncio.sleep(0.5)
        raise VerificationFailure(f"自由辩论整体超时：{state_summary(await self.state(self.owner))}")

    async def drive(self) -> None:
        assert self.owner
        overall_deadline = time.monotonic() + max(self.timeout_seconds * len(self.scenario.expected_stage_keys), 1200)
        while time.monotonic() < overall_deadline:
            state = await self.state(self.owner)
            status = state.get("status")
            if status == "paused":
                await self._recover_pause(state, "主流程")
                continue
            if status in TERMINAL_STATUSES:
                if status != "completed":
                    raise VerificationFailure(f"比赛以 {status} 结束：{state_summary(state)}")
                return
            current = state.get("current_stage") or {}
            key = str(current.get("key") or "")
            kind = current.get("kind")
            if not key:
                await asyncio.sleep(0.5)
                continue
            if current.get("host_announcement_pending") or kind == "announcement":
                await self.wait_for(
                    self.owner,
                    lambda value, stage_key=key: (
                        (value.get("current_stage") or {}).get("key") != stage_key
                        or (
                            not (value.get("current_stage") or {}).get("host_announcement_pending")
                            and (value.get("current_stage") or {}).get("kind") != "announcement"
                        )
                    ),
                    f"{key} 系统预设主持提示音播放完成",
                )
                continue
            if kind == "judging" or status == "judging":
                await self.wait_for(
                    self.owner,
                    lambda value: value.get("status") in TERMINAL_STATUSES,
                    "真实裁判返回结果",
                    timeout=max(self.timeout_seconds, 240),
                )
                continue
            if kind == "free":
                await self.run_free_stage()
                continue
            if kind == "speech":
                actor = self.actor_for_seat(current.get("seat"))
                if actor:
                    await self.run_fixed_human_stage(state, actor)
                else:
                    await self.run_ai_stage(state)
                continue
            raise VerificationFailure(f"未知比赛阶段：{state_summary(state)}")
        raise VerificationFailure(f"完整比赛超过总时限：{state_summary(await self.state(self.owner))}")

    async def fetch_complete_result(self) -> dict[str, Any]:
        assert self.owner and self.code
        first = require_response(
            await self.owner.client.get(
                f"/api/rooms/{self.code}/result",
                params={"speech_page_size": 200, "event_page_size": 200},
            ),
            "读取比赛结果",
        ).json()
        speeches = list(first.get("speeches", []))
        speech_page = 2
        while first.get("speech_pagination", {}).get("has_more"):
            page = require_response(
                await self.owner.client.get(
                    f"/api/rooms/{self.code}/result",
                    params={"speech_page": speech_page, "speech_page_size": 200, "event_page_size": 1},
                ),
                f"读取发言结果第 {speech_page} 页",
            ).json()
            speeches.extend(page.get("speeches", []))
            first["speech_pagination"] = page.get("speech_pagination", {})
            speech_page += 1
        events = list(first.get("events", []))
        cursor = first.get("event_pagination", {}).get("next_before_seq")
        has_more = bool(first.get("event_pagination", {}).get("has_more"))
        while has_more and cursor:
            page = require_response(
                await self.owner.client.get(
                    f"/api/rooms/{self.code}/result",
                    params={"speech_page_size": 1, "event_page_size": 200, "event_before_seq": cursor},
                ),
                "读取完整事件日志",
            ).json()
            events = list(page.get("events", [])) + events
            cursor = page.get("event_pagination", {}).get("next_before_seq")
            has_more = bool(page.get("event_pagination", {}).get("has_more"))
        first["speeches"] = speeches
        first["events"] = events
        return first

    def validate_result(self, result: dict[str, Any]) -> dict[str, Any]:
        match = result.get("match") or {}
        scorecard = result.get("scorecard") or {}
        room = result.get("room") or {}
        if match.get("status") != "completed" or room.get("status") != "completed":
            raise VerificationFailure(f"结果页不是 completed：match={match.get('status')}, room={room.get('status')}")
        if scorecard.get("status") != "approved":
            raise VerificationFailure(f"裁判结果未批准：{scorecard.get('status')}")
        if scorecard.get("winner") not in {"aff", "neg", "draw"}:
            raise VerificationFailure(f"裁判 winner 无效：{scorecard.get('winner')}")

        events = result.get("events") or []
        event_types = [str(item.get("type") or "") for item in events]
        forbidden = sorted(FORBIDDEN_SUCCESS_EVENTS.intersection(event_types))
        if forbidden:
            raise VerificationFailure(f"成功比赛仍包含异常事件：{forbidden}")
        started_stage_keys = [
            str(((item.get("payload") or {}).get("stage") or {}).get("key") or "")
            for item in events
            if item.get("type") == "stage.started"
        ]
        missing_stages = [key for key in self.scenario.expected_stage_keys if key not in started_stage_keys]
        if missing_stages:
            raise VerificationFailure(f"未进入完整阶段：{missing_stages}")
        seqs = [item.get("seq") for item in events if isinstance(item.get("seq"), int)]
        if seqs and seqs != list(range(min(seqs), max(seqs) + 1)):
            raise VerificationFailure("比赛事件 seq 不连续")
        if "match.completed" not in event_types:
            raise VerificationFailure("事件日志缺少 match.completed")
        if self.exercise_pause_resume:
            if not self._pause_exercised:
                raise VerificationFailure("验收未实际执行房主暂停恢复")
            missing_control_events = {"control.pause", "control.resume"} - set(event_types)
            if missing_control_events:
                raise VerificationFailure(f"暂停恢复缺少事件：{sorted(missing_control_events)}")
        if self.exercise_reconnect:
            if not self._reconnect_exercised or self.audit.reconnect_duration_seconds is None:
                raise VerificationFailure("验收未实际执行 60 秒内短断线恢复")
            missing_presence_events = {"presence.disconnected", "presence.connected"} - set(event_types)
            if missing_presence_events:
                raise VerificationFailure(f"短断线恢复缺少事件：{sorted(missing_presence_events)}")

        speeches = result.get("speeches") or []
        if room.get("match_audio_archive_enabled") is not False:
            raise VerificationFailure("正式比赛仍启用了逐场音频归档，未满足只保存文字记录的约束")
        archived_audio = [item.get("id") for item in speeches if str(item.get("audio_url") or "").strip()]
        if archived_audio:
            raise VerificationFailure(f"结果仍暴露逐场发言音频：{archived_audio[:10]}")
        incomplete = [item.get("id") for item in speeches if item.get("status") != "completed"]
        if incomplete:
            raise VerificationFailure(f"结果中仍有未完成发言：{incomplete[:10]}")
        fixed_speech_stages = {
            key for key in self.scenario.expected_stage_keys if key not in {"opening", "free_debate", "judging"}
        }
        covered_fixed = {str(item.get("stage_key") or "") for item in speeches}
        missing_speeches = sorted(fixed_speech_stages - covered_fixed)
        if missing_speeches:
            raise VerificationFailure(f"固定发言阶段缺少有效发言：{missing_speeches}")
        free_speeches = [item for item in speeches if item.get("stage_key") == "free_debate"]
        if len(free_speeches) < self.free_turns:
            raise VerificationFailure(f"结果仅保存 {len(free_speeches)} 轮自由辩论，要求 {self.free_turns}")
        speaker_types = {str(item.get("speaker_type") or "") for item in speeches}
        if "human" not in speaker_types:
            raise VerificationFailure("结果没有真人发言")
        if self.scenario.require_ai_speech and "ai" not in speaker_types:
            raise VerificationFailure("真人+AI 场景没有 AI 发言")
        if not self.scenario.require_ai_speech and "ai" in speaker_types:
            raise VerificationFailure("双真人场景出现了不应存在的 AI 发言")
        if self.scenario.require_ai_speech and not self.audit.ai_playback_ids:
            raise VerificationFailure("没有在实时房间投影中观察到 AI 实际播放")
        if self.scenario.require_ai_speech:
            ai_speech_ids = {str(item.get("id") or "") for item in speeches if item.get("speaker_type") == "ai"}
            rtc_events = [item for item in events if item.get("type") == "audio.rtc.started"]
            rtc_by_speech = {
                str((item.get("payload") or {}).get("speech_id") or ""): item
                for item in rtc_events
            }
            missing_rtc = sorted(ai_speech_ids - set(rtc_by_speech))
            if missing_rtc:
                raise VerificationFailure(f"AI 发言没有通过唯一 WebRTC 音轨播放：{missing_rtc[:10]}")
            invalid_rtc = [
                speech_id
                for speech_id, item in rtc_by_speech.items()
                if speech_id in ai_speech_ids
                and (
                    (item.get("payload") or {}).get("transport") != "livekit"
                    or (item.get("payload") or {}).get("synthesis_mode") != "single_session_incremental"
                    or bool((item.get("payload") or {}).get("stream_url"))
                    or bool((item.get("payload") or {}).get("audio_url"))
                )
            ]
            if invalid_rtc:
                raise VerificationFailure(f"AI 发言未使用单会话真流式 LiveKit 音轨：{sorted(invalid_rtc)[:10]}")
            retired_playback_events = {
                "audio.stream.started",
                "speech.audio.prepared",
            }.intersection(event_types)
            if retired_playback_events:
                raise VerificationFailure(
                    f"比赛仍进入已淘汰的 PCM/WAV 播放路径：{sorted(retired_playback_events)}"
                )
        if self.admin and not room.get("is_test_data"):
            raise VerificationFailure("验收房间没有标记为测试数据")

        return {
            "ok": True,
            **self.audit.summary(),
            "status": match.get("status"),
            "winner": scorecard.get("winner"),
            "speech_count": len(speeches),
            "event_count": len(events),
            "fixed_stages_covered": sorted(fixed_speech_stages),
            "full_free_duration": self.full_free_duration,
        }

    async def cleanup(self) -> None:
        # Close presence first so a failed verifier cannot leave fake students
        # occupying a lobby. Then release the active room through public APIs.
        await asyncio.gather(*(actor.disconnect_presence() for actor in self.actors), return_exceptions=True)
        if self.code and self.owner:
            with contextlib.suppress(Exception):
                current = await self.owner.room(self.code)
                status = current.get("status")
                if status in {"lobby", "preparing"}:
                    await self.owner.client.post(
                        f"/api/rooms/{self.code}/cancel",
                        headers=csrf(self.owner.client),
                        json={"reason": "完整比赛验收失败清理"},
                    )
                elif status in {"running", "paused", "judging"}:
                    await self.owner.client.post(
                        f"/api/rooms/{self.code}/control/terminate",
                        headers=csrf(self.owner.client),
                        json={"reason": "完整比赛验收失败清理"},
                    )
        await asyncio.gather(*(actor.close() for actor in self.actors), return_exceptions=True)
        if self.admin:
            await self.admin.close()

    async def run(self) -> dict[str, Any]:
        try:
            await self.preflight()
            await self.setup()
            await self.start()
            await self.drive()
            result = await self.fetch_complete_result()
            return self.validate_result(result)
        finally:
            await self.cleanup()


def is_remote_url(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower()
    return hostname not in {"", "localhost", "127.0.0.1", "::1"}


def validate_args(args: argparse.Namespace) -> None:
    if is_remote_url(args.base_url) and not args.allow_production:
        raise SystemExit("远程验收会创建真实房间；请显式添加 --allow-production")
    if args.free_turns < 2 or args.free_turns > 20:
        raise SystemExit("--free-turns 必须在 2 到 20 之间，至少覆盖正反双方")
    if args.timeout_seconds < 30 or args.timeout_seconds > 900:
        raise SystemExit("--timeout-seconds 必须在 30 到 900 之间")
    if args.max_opening_seconds < 5 or args.max_opening_seconds > 60:
        raise SystemExit("--max-opening-seconds 必须在 5 到 60 之间")
    if is_remote_url(args.base_url) and args.allow_unclassified_test_data:
        raise SystemExit("远程环境禁止 --allow-unclassified-test-data，以免污染赛事与排行榜")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://117.50.192.216")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="1v1-human-ai")
    parser.add_argument("--insecure", action="store_true", help="仅用于自签名证书环境")
    parser.add_argument("--allow-production", action="store_true", help="确认允许在远程环境创建测试房间")
    parser.add_argument("--allow-unclassified-test-data", action="store_true", help="仅限本地：无管理员账号时继续")
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--max-opening-seconds", type=float, default=15)
    parser.add_argument("--free-turns", type=int, default=3)
    parser.add_argument("--full-free-duration", action="store_true")
    parser.add_argument("--exercise-pause-resume", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--exercise-reconnect", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--exercise-ai-reset", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--no-auto-recover", action="store_true")
    parser.add_argument("--output", type=Path, help="可选：保存机器可读 JSON 结果")
    args = parser.parse_args(argv)
    validate_args(args)
    return args


async def async_main(args: argparse.Namespace) -> dict[str, Any]:
    verifier = CompleteMatchVerifier(
        base_url=args.base_url,
        insecure=args.insecure,
        scenario=SCENARIOS[args.scenario],
        timeout_seconds=args.timeout_seconds,
        max_opening_seconds=args.max_opening_seconds,
        free_turns=args.free_turns,
        full_free_duration=args.full_free_duration,
        exercise_pause_resume=args.exercise_pause_resume,
        exercise_reconnect=args.exercise_reconnect,
        exercise_ai_reset=args.exercise_ai_reset,
        auto_recover=not args.no_auto_recover,
        allow_unclassified_test_data=args.allow_unclassified_test_data,
    )
    return await verifier.run()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = asyncio.run(async_main(args))
    except (VerificationFailure, httpx.HTTPError, OSError, asyncio.TimeoutError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
