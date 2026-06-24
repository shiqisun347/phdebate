#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import quote, urlparse, urlunparse

import httpx
import websockets


ALL_SPEAKER_IDS = [
    "spk_aff_1",
    "spk_aff_2",
    "spk_aff_3",
    "spk_aff_4",
    "spk_neg_1",
    "spk_neg_2",
    "spk_neg_3",
    "spk_neg_4",
]
AGENT_ENDPOINT = "http://47.93.206.109:8000/api/debate"
DEFAULT_REFERENCE_FLOW = Path(__file__).resolve().parents[2] / "用于参考的辩论过程.md"


def load_reference_flow(path: Optional[str]) -> Dict[str, str]:
    if not path:
        return {}
    file_path = Path(path).expanduser()
    if not file_path.exists():
        return {}
    text = file_path.read_text(encoding="utf-8")
    matches = list(re.finditer(r"^【([^】]+)】\s*$", text, flags=re.M))
    sections: Dict[str, str] = {}
    for idx, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        if content:
            sections[title] = content
    return sections


def section_text(
    sections: Dict[str, str],
    titles: Iterable[str],
    fallback: str,
    *,
    max_chars: int = 1600,
) -> str:
    for title in titles:
        text = sections.get(title, "").strip()
        if text:
            return text[:max_chars].strip()
    return fallback


def free_debate_excerpt(sections: Dict[str, str], speaker_label: str, fallback: str, *, max_chars: int = 520) -> str:
    text = sections.get("自由辩论", "")
    if not text:
        return fallback
    lines = [line.strip() for line in text.splitlines() if speaker_label in line]
    if not lines:
        return text[:max_chars].strip()
    return "\n".join(lines)[:max_chars].strip()


def progressive_partials(text: str, count: int) -> list[str]:
    cleaned = text.strip()
    if not cleaned:
        return []
    target = max(2, count)
    chunks: list[str] = []
    for idx in range(1, target):
        cut = max(12, min(len(cleaned), int(len(cleaned) * idx / target)))
        chunks.append(cleaned[:cut])
    return chunks


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def bearer(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def speaker_tokens() -> Dict[str, str]:
    raw = os.getenv("PHDEBATE_SPEAKER_TOKENS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except json.JSONDecodeError:
            pass
        tokens: Dict[str, str] = {}
        for item in raw.split(","):
            if ":" in item:
                sid, tok = item.split(":", 1)
                tokens[sid.strip()] = tok.strip()
        if tokens:
            return tokens
    shared = os.getenv("PHDEBATE_SPEAKER_TOKEN", "").strip()
    return {speaker_id: shared for speaker_id in ALL_SPEAKER_IDS}


def ws_base_from_http(base_url: str) -> str:
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, "", "", "", ""))


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""
    latency_ms: Optional[int] = None
    data: Dict[str, Any] = field(default_factory=dict)


class Recorder:
    def __init__(self) -> None:
        self.steps: list[Step] = []

    def add(self, name: str, ok: bool, detail: str = "", latency_ms: Optional[int] = None, **data: Any) -> None:
        self.steps.append(Step(name=name, ok=ok, detail=detail, latency_ms=latency_ms, data=data))
        marker = "PASS" if ok else "FAIL"
        suffix = f" ({latency_ms} ms)" if latency_ms is not None else ""
        print(f"[{marker}] {name}{suffix}: {detail}", flush=True)

    def require(self, name: str, condition: bool, detail: str = "", **data: Any) -> None:
        self.add(name, condition, detail, **data)

    def exit_code(self) -> int:
        return 0 if all(step.ok for step in self.steps if not step.data.get("allowed_failure")) else 1


class Api:
    def __init__(self, base_url: str, recorder: Recorder) -> None:
        self.base = base_url.rstrip("/")
        self.rec = recorder
        self.admin_token = os.getenv("PHDEBATE_ADMIN_TOKEN") or os.getenv("PHDEBATE_ADMIN_PASSWORD", "")
        self.host_token = os.getenv("PHDEBATE_HOST_TOKEN") or os.getenv("PHDEBATE_HOST_PASSWORD", "")
        self.screen_token = os.getenv("PHDEBATE_SCREEN_TOKEN", "")
        self.speaker_tokens = speaker_tokens()
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=180.0))

    async def close(self) -> None:
        await self.client.aclose()

    def token_for(self, role: str, speaker_id: str = "") -> str:
        if role == "admin":
            return self.admin_token
        if role == "host":
            return self.host_token or self.admin_token
        if role == "screen":
            return self.screen_token or self.host_token or self.admin_token
        if role == "speaker":
            return self.speaker_tokens.get(speaker_id, "") or self.host_token or self.admin_token
        return ""

    async def request(
        self,
        method: str,
        path: str,
        *,
        role: str = "host",
        speaker_id: str = "",
        json_body: Optional[Dict[str, Any]] = None,
        expected: Iterable[int] = (200,),
        name: Optional[str] = None,
        allowed_failure: bool = False,
    ) -> Dict[str, Any]:
        url = f"{self.base}{path}"
        started = time.perf_counter()
        try:
            response = await self.client.request(
                method,
                url,
                headers=bearer(self.token_for(role, speaker_id)),
                json=json_body,
            )
            latency = int((time.perf_counter() - started) * 1000)
            ok = response.status_code in set(expected)
            detail = f"{method} {path} -> {response.status_code}"
            try:
                data = response.json()
            except ValueError:
                data = {"raw": response.text[:500]}
            self.rec.add(
                name or f"{method} {path}",
                ok or allowed_failure,
                detail,
                latency,
                status=response.status_code,
                body=safe_report_body(data),
                allowed_failure=allowed_failure,
            )
            if not ok and not allowed_failure:
                raise AssertionError(f"{detail}: {data}")
            return data
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            self.rec.add(name or f"{method} {path}", allowed_failure, f"{type(exc).__name__}: {exc}", latency, allowed_failure=allowed_failure)
            if not allowed_failure:
                raise
            return {"ok": False, "error": str(exc)}


def safe_report_body(data: Any) -> Any:
    if not isinstance(data, dict):
        return _safe_brief(data)
    if isinstance(data.get("data"), dict):
        payload = data["data"]
        if isinstance(payload.get("match"), dict):
            match = payload["match"]
            summary: Dict[str, Any] = {
                "ok": data.get("ok"),
                "match": {
                    "id": match.get("id"),
                    "status": match.get("status"),
                    "current_phase_id": match.get("current_phase_id"),
                    "live_mode": match.get("live_mode"),
                },
            }
            if isinstance(payload.get("counts"), dict):
                summary["counts"] = payload["counts"]
            if isinstance(payload.get("recent_transcript"), list):
                summary["recent_transcript_count"] = len(payload["recent_transcript"])
            if isinstance(payload.get("agent_status"), list):
                summary["agent_status"] = [
                    {"speaker_id": item.get("speaker_id"), "status": item.get("status")}
                    for item in payload["agent_status"]
                ]
            if isinstance(payload.get("speech_service"), dict):
                speech_service = payload["speech_service"]
                summary["speech_service"] = {
                    key: {
                        "status": value.get("status"),
                        "queue_size": value.get("queue_size"),
                        "active_sessions": value.get("active_sessions"),
                    }
                    for key, value in speech_service.items()
                    if isinstance(value, dict)
                }
            return summary
        if isinstance(payload.get("counts"), dict):
            return {"ok": data.get("ok"), "counts": payload["counts"]}
        if "token" in payload or "api_secret" in payload:
            redacted = dict(payload)
            for key in list(redacted):
                if _is_sensitive_key(key):
                    redacted[key] = "<redacted>"
            return {"ok": data.get("ok"), "data": _safe_brief(redacted)}
    return _safe_brief(data)


def _safe_brief(value: Any, depth: int = 0) -> Any:
    if _is_scalar(value):
        if isinstance(value, str) and len(value) > 180:
            return value[:180] + f"... <truncated {len(value)} chars>"
        return value
    if isinstance(value, list):
        if depth >= 2:
            return f"<list len={len(value)}>"
        return [_safe_brief(item, depth + 1) for item in value[:8]] + ([f"<truncated list len={len(value)}>"] if len(value) > 8 else [])
    if isinstance(value, dict):
        if depth >= 2:
            return f"<dict keys={','.join(list(value)[:8])}>"
        result: Dict[str, Any] = {}
        for key, item in list(value.items())[:20]:
            if _is_sensitive_key(str(key)):
                result[str(key)] = "<redacted>"
            else:
                result[str(key)] = _safe_brief(item, depth + 1)
        if len(value) > 20:
            result["<truncated>"] = f"{len(value)} keys"
        return result
    return str(value)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in {"token", "authorization"} or "secret" in lowered or lowered.endswith("_token")


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


class WSClient:
    def __init__(self, name: str, url: str, recorder: Recorder) -> None:
        self.name = name
        self.url = url
        self.rec = recorder
        self.ws: Any = None
        self.reader: Optional[asyncio.Task] = None
        self.queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self.counts: Dict[str, int] = {}
        self.last_seq = 0

    async def connect(self) -> None:
        started = time.perf_counter()
        self.ws = await websockets.connect(self.url, ping_interval=15, ping_timeout=10, max_size=16 * 1024 * 1024)
        self.reader = asyncio.create_task(self._read_loop())
        msg = await self.wait_for("snapshot", timeout=5)
        self.last_seq = int(msg.get("seq") or 0)
        self.rec.add(f"ws connect {self.name}", True, f"snapshot seq={self.last_seq}", int((time.perf_counter() - started) * 1000))

    async def _read_loop(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                msg_type = str(msg.get("type") or "")
                self.counts[msg_type] = self.counts.get(msg_type, 0) + 1
                if "seq" in msg:
                    self.last_seq = max(self.last_seq, int(msg.get("seq") or 0))
                await self.queue.put(msg)
        except Exception:
            return

    async def send(self, payload: Dict[str, Any]) -> None:
        await self.ws.send(json.dumps(payload, ensure_ascii=False))

    async def wait_for(self, msg_type: str, timeout: float = 10, speaker_id: str = "") -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{self.name} timed out waiting for {msg_type}")
            msg = await asyncio.wait_for(self.queue.get(), timeout=remaining)
            if msg.get("type") != msg_type:
                continue
            if speaker_id and (msg.get("payload") or {}).get("speaker_id") != speaker_id:
                continue
            return msg

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()
        if self.reader:
            try:
                await asyncio.wait_for(self.reader, timeout=2)
            except Exception:
                self.reader.cancel()


def ws_url(base_url: str, channel: str, token: str, speaker_id: str = "") -> str:
    query = f"channel={quote(channel)}&token={quote(token)}"
    if speaker_id:
        query += f"&speaker_id={quote(speaker_id)}"
    return f"{ws_base_from_http(base_url)}/ws/matches/current?{query}"


async def wait_snapshot(api: Api) -> Dict[str, Any]:
    data = await api.request("GET", "/api/matches/current", role="screen", name="snapshot")
    return data["data"]


def phase_by_type(snapshot: Dict[str, Any], phase_type: str) -> Dict[str, Any]:
    for phase in snapshot.get("phases", []):
        if phase.get("phase_type") == phase_type:
            return phase
    raise AssertionError(f"phase_type {phase_type!r} not found")


def clock_by_name(snapshot: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(clock.get("name")): clock for clock in snapshot.get("clocks", [])}


async def wait_agent_failed(api: Api, speaker_id: str, timeout: float = 12) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        snapshot = await wait_snapshot(api)
        last = snapshot
        agent = next((item for item in snapshot.get("agent_status", []) if item.get("speaker_id") == speaker_id), {})
        if agent.get("status") == "failed":
            return snapshot
        await asyncio.sleep(0.5)
    raise TimeoutError(f"agent {speaker_id} did not fail within {timeout}s; last={last.get('agent_status')}")


async def wait_agent_outcome(api: Api, speaker_ids: Iterable[str], timeout: float = 90) -> tuple[str, str, Dict[str, Any]]:
    candidates = set(speaker_ids)
    deadline = time.monotonic() + timeout
    last: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        snapshot = await wait_snapshot(api)
        last = snapshot
        current_phase_id = (snapshot.get("match") or {}).get("current_phase_id")
        for item in snapshot.get("agent_status", []):
            if item.get("speaker_id") in candidates and item.get("status") == "failed":
                return "failed", str(item["speaker_id"]), snapshot
        current = snapshot.get("current_speech") or {}
        if current.get("speaker_id") in candidates and current.get("source") == "agent_text":
            if current.get("agent_final_ready") or current.get("content_final"):
                return "completed", str(current["speaker_id"]), snapshot
        for segment in snapshot.get("recent_transcript", []):
            if (
                segment.get("speaker_id") in candidates
                and segment.get("source") == "agent_text"
                and segment.get("is_final")
                and (not current_phase_id or segment.get("phase_id") == current_phase_id)
            ):
                return "completed", str(segment["speaker_id"]), snapshot
        await asyncio.sleep(0.5)
    raise TimeoutError(f"agents {sorted(candidates)} did not finish within {timeout}s; last={last.get('agent_status')}")


async def finish_agent_playback_if_needed(api: Api, snapshot: Dict[str, Any], speaker_id: str, name: str) -> Dict[str, Any]:
    speech = snapshot.get("current_speech") or {}
    if speech.get("speaker_id") != speaker_id or speech.get("source") != "agent_text":
        return snapshot
    speech_id = str(speech.get("id") or "")
    task_id = str(speech.get("tts_task_id") or "")
    if not speech_id:
        return snapshot
    await api.request(
        "POST",
        f"/api/matches/current/speeches/{speech_id}/tts/playback-started",
        role="screen",
        json_body={"task_id": task_id, "reason": "e2e_screen_playback_started"},
        name=f"{name} playback started",
    )
    await api.request(
        "POST",
        f"/api/matches/current/speeches/{speech_id}/tts/playback-progress",
        role="screen",
        json_body={"task_id": task_id, "sentence_idx": 0, "status": "playing"},
        name=f"{name} playback progress",
    )
    completed = await api.request(
        "POST",
        f"/api/matches/current/speeches/{speech_id}/tts/playback-complete",
        role="screen",
        json_body={"task_id": task_id, "reason": "e2e_screen_playback_complete"},
        name=name,
    )
    return completed["data"]


async def recover_or_finish_agent(api: Api, speaker_ids: Iterable[str], *, fallback_text: str, fallback_reason: str, label: str) -> Dict[str, Any]:
    outcome, speaker_id, snapshot = await wait_agent_outcome(api, speaker_ids, timeout=120)
    if outcome == "failed":
        api.rec.add(f"{label} agent failure detected", True, f"{speaker_id} entered failed state")
        recovered = await api.request(
            "POST",
            f"/api/matches/current/agent/{speaker_id}/manual-input",
            role="host",
            json_body={"content": fallback_text, "reason": fallback_reason},
            name=f"{label} manual fallback",
        )
        api.rec.require(f"{label} manual fallback finalized", recovered["data"]["current_speech"] is None, "current_speech cleared")
        return recovered["data"]
    api.rec.add(f"{label} remote agent completed", True, f"{speaker_id} generated final text")
    data = await finish_agent_playback_if_needed(api, snapshot, speaker_id, f"{label} simulated screen playback complete")
    api.rec.require(f"{label} agent transcript finalized", data.get("current_speech") is None, "agent speech closed")
    return data


async def run_self_introduction_probe(api: Api, speaker_id: str, *, label: str = "prematch self-introduction") -> bool:
    await api.request(
        "POST",
        f"/api/matches/current/speakers/{speaker_id}/self-introduction",
        role="host",
        name=f"{label} requested",
    )
    try:
        outcome, finished_speaker_id, snapshot = await wait_agent_outcome(api, [speaker_id], timeout=90)
    except Exception as exc:
        api.rec.add(f"{label} outcome", False, f"{type(exc).__name__}: {exc}", allowed_failure=True)
        return False
    if outcome == "failed":
        api.rec.add(f"{label} remote agent failed", False, finished_speaker_id, allowed_failure=True)
        return False
    data = await finish_agent_playback_if_needed(api, snapshot, speaker_id, f"{label} simulated screen")
    api.rec.require(f"{label} completed without advancing match", data.get("match", {}).get("status") == "ready", data.get("match", {}).get("status", ""))
    intro = next((seg for seg in data.get("recent_transcript", []) if seg.get("speaker_id") == speaker_id and seg.get("kind") == "self_intro"), None)
    api.rec.require(f"{label} transcript marked self_intro", bool(intro), "self_intro transcript present")
    return True


async def simulate_human_turn(
    api: Api,
    speaker_id: str,
    text: str,
    *,
    final_latency: int = 680,
    partial_count: int = 3,
    partial_delay_seconds: float = 0.08,
) -> Dict[str, Any]:
    started = await api.request("POST", f"/api/matches/current/speakers/{speaker_id}/start-speaking", role="speaker", speaker_id=speaker_id, name=f"{speaker_id} start speaking")
    speech_id = started["data"]["current_speech"]["id"]
    for idx, partial in enumerate(progressive_partials(text, partial_count), start=1):
        await api.request(
            "POST",
            f"/api/matches/current/speakers/{speaker_id}/asr/partial",
            role="speaker",
            speaker_id=speaker_id,
            json_body={"text": partial, "latency_ms": 180 + idx * 80},
            name=f"{speaker_id} asr partial {idx}",
        )
        await asyncio.sleep(partial_delay_seconds)
    await api.request(
        "POST",
        f"/api/matches/current/speakers/{speaker_id}/asr/final",
        role="speaker",
        speaker_id=speaker_id,
        json_body={"text": text, "latency_ms": final_latency},
        name=f"{speaker_id} asr final",
    )
    stopped = await api.request("POST", f"/api/matches/current/speakers/{speaker_id}/stop-speaking", role="speaker", speaker_id=speaker_id, name=f"{speaker_id} stop speaking")
    top = stopped["data"]["recent_transcript"][0]
    api.rec.require(f"{speaker_id} transcript finalized", top.get("speech_id") == speech_id and top.get("text") == text, top.get("text", "")[:80])
    return stopped["data"]


async def configure_all_agents(api: Api, snapshot: Dict[str, Any], endpoint: str) -> None:
    for config in snapshot.get("agent_configs", []):
        await api.request(
            "PATCH",
            f"/api/matches/current/agents/configs/{config['id']}",
            role="admin",
            json_body={"provider_type": "rest_api", "endpoint": endpoint, "timeout_ms": 120000},
            name=f"agent config {config['id']} endpoint",
        )


async def wait_human_consoles_online(api: Api, expected: int, timeout: float = 10) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        snapshot = await wait_snapshot(api)
        last = snapshot
        online = int(((snapshot.get("speech_service") or {}).get("consoles") or {}).get("online") or 0)
        if online >= expected:
            return snapshot
        await asyncio.sleep(0.3)
    raise TimeoutError(f"human consoles did not reach {expected}; last={last.get('speech_service', {}).get('consoles')}")


def side_label(side: str) -> str:
    return "正方" if side == "affirmative" else "反方" if side == "negative" else "中立"


def seat_label(seat: Any) -> str:
    return {1: "一辩", 2: "二辩", 3: "三辩", 4: "四辩"}.get(int(seat or 0), f"{seat}辩")


def speaker_label(speaker: Dict[str, Any]) -> str:
    return f"{side_label(str(speaker.get('side')))}{seat_label(speaker.get('seat'))}"


def speaker_for_phase(snapshot: Dict[str, Any], phase: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if phase.get("phase_type") == "free_debate":
        return None
    return next(
        (
            speaker
            for speaker in snapshot.get("speakers", [])
            if speaker.get("side") == phase.get("side") and speaker.get("seat") == phase.get("speaker_seat")
        ),
        None,
    )


def speakers_on_side(snapshot: Dict[str, Any], side: str, speaker_type: Optional[str] = None) -> list[Dict[str, Any]]:
    return [
        speaker
        for speaker in snapshot.get("speakers", [])
        if speaker.get("side") == side and (speaker_type is None or speaker.get("speaker_type") == speaker_type)
    ]


def phase_reference_text(sections: Dict[str, str], phase: Dict[str, Any], speaker: Optional[Dict[str, Any]]) -> str:
    name = str(phase.get("name") or "")
    candidates = [
        name,
        name.replace("总结", "结辩"),
        name.replace("总结", "总结陈词"),
        name.replace("开篇立论", "立论"),
        name.replace("驳论", "陈词"),
    ]
    label = speaker_label(speaker) if speaker else name
    fallback = f"{label}围绕本场辩题完成本阶段发言，回应对方核心观点并推进己方论证。"
    return section_text(sections, candidates, fallback)


async def maybe_start_agent_turn(api: Api, speaker_id: str, label: str) -> None:
    snapshot = await wait_snapshot(api)
    current = snapshot.get("current_speech") or {}
    if current.get("speaker_id") == speaker_id:
        return
    await api.request(
        "POST",
        f"/api/matches/current/speakers/{speaker_id}/start-agent-speaking",
        role="speaker",
        speaker_id=speaker_id,
        expected=(200, 409),
        allowed_failure=True,
        name=f"{label} ensure agent started",
    )


async def complete_configured_single_phase(
    api: Api,
    phase: Dict[str, Any],
    reference_sections: Dict[str, str],
    *,
    partial_count_fn,
    latency_fn,
) -> Dict[str, Any]:
    snapshot = await wait_snapshot(api)
    current_phase_id = snapshot["match"]["current_phase_id"]
    api.rec.require(f"{phase['name']} is current phase", current_phase_id == phase["id"], current_phase_id)
    speaker = speaker_for_phase(snapshot, phase)
    api.rec.require(f"{phase['name']} has configured speaker", bool(speaker), str(phase))
    if not speaker:
        return snapshot

    text = phase_reference_text(reference_sections, phase, speaker)
    label = f"{phase['name']} · {speaker_label(speaker)}"
    if speaker.get("speaker_type") == "human":
        data = await simulate_human_turn(
            api,
            str(speaker["id"]),
            text,
            final_latency=latency_fn(text),
            partial_count=partial_count_fn(text),
        )
        await api.request(
            "POST",
            "/api/matches/current/flow/confirm",
            role="host",
            json_body={"reason": f"e2e_{phase.get('phase_key')}_done"},
            name=f"host confirms after {phase['name']}",
        )
        return data

    await maybe_start_agent_turn(api, str(speaker["id"]), label)
    return await recover_or_finish_agent(
        api,
        [str(speaker["id"])],
        fallback_text=text,
        fallback_reason=f"e2e_{phase.get('phase_key')}_agent_unreachable",
        label=label,
    )


async def complete_free_debate_turn(
    api: Api,
    reference_sections: Dict[str, str],
    *,
    partial_count_fn,
    latency_fn,
) -> Dict[str, Any]:
    snapshot = await wait_snapshot(api)
    side = str((snapshot.get("free_debate") or {}).get("current_turn_side") or "affirmative")
    humans = speakers_on_side(snapshot, side, "human")
    agents = speakers_on_side(snapshot, side, "agent")
    if humans:
        speaker = humans[0]
        text = free_debate_excerpt(
            reference_sections,
            speaker_label(speaker),
            f"自由辩论中，{speaker_label(speaker)}继续追问对方论证中的关键漏洞。",
        )
        return await simulate_human_turn(
            api,
            str(speaker["id"]),
            text,
            final_latency=latency_fn(text),
            partial_count=partial_count_fn(text),
        )
    if agents:
        return await recover_or_finish_agent(
            api,
            [str(speaker["id"]) for speaker in agents],
            fallback_text=f"自由辩论中，{side_label(side)}AI 接管发言，继续推进本方论证并回应对方质询。",
            fallback_reason="e2e_free_debate_agent_unreachable",
            label=f"free debate {side_label(side)} auto-agent",
        )
    api.rec.add(f"free debate {side_label(side)} has no speaker", False, str(snapshot.get("speakers")), allowed_failure=True)
    return snapshot


async def run(args: argparse.Namespace) -> int:
    if args.env_file:
        load_env_file(Path(args.env_file))
    reference_sections = load_reference_flow(args.reference_flow_file)
    rec = Recorder()
    api = Api(args.base_url, rec)
    sockets: list[WSClient] = []
    try:
        await api.request("GET", "/api/health", role="screen", name="api health")
        await api.request("GET", "/api/livekit/status", role="screen", name="livekit status")
        lk = await api.request(
            "POST",
            "/api/matches/current/livekit/token",
            role="screen",
            json_body={"role": "screen", "ttl_seconds": 300},
            expected=(200, 409),
            name="livekit token explicit state",
            allowed_failure=True,
        )
        if lk.get("ok") is False:
            code = ((lk.get("error") or {}).get("code") or "")
            rec.require("livekit missing credentials exposed", code == "livekit_not_configured", code, allowed_failure=True)

        created = await api.request(
            "POST",
            "/api/matches",
            role="admin",
            json_body={
                "title": f"phdebate E2E Real Flow {int(time.time())}",
                "topic": "AI 时代，我们更应该培养编程思维还是提问思维",
            },
            name="create isolated e2e match",
        )
        match_id = created["data"]["match_id"]
        snapshot = await wait_snapshot(api)
        rec.require("isolated match active", snapshot["match"]["id"] == match_id, match_id)

        await configure_all_agents(api, snapshot, args.agent_endpoint)

        if args.self_intro:
            intro_agent = next((speaker for speaker in snapshot.get("speakers", []) if speaker.get("speaker_type") == "agent"), None)
            ok = True
            if intro_agent:
                ok = await run_self_introduction_probe(api, str(intro_agent["id"]))
            else:
                rec.add("prematch self-introduction skipped", True, "current lineup has no agent speaker")
            if intro_agent and not ok:
                recreated = await api.request(
                    "POST",
                    "/api/matches",
                    role="admin",
                    json_body={
                        "title": f"phdebate E2E Real Flow {int(time.time())} clean after self-intro failure",
                        "topic": "AI 时代，我们更应该培养编程思维还是提问思维",
                    },
                    name="create clean match after self-intro degradation",
                )
                match_id = recreated["data"]["match_id"]
                snapshot = await wait_snapshot(api)
                rec.require("clean match active after self-intro degradation", snapshot["match"]["id"] == match_id, match_id)
                await configure_all_agents(api, snapshot, args.agent_endpoint)

        ref_aff1 = section_text(
            reference_sections,
            ["正方一辩立论"],
            "正方一辩认为，编程思维并不是人人都要成为程序员，而是学会把复杂问题拆成可验证的步骤。",
        )
        ref_neg2 = section_text(
            reference_sections,
            ["反方二辩陈词"],
            "反方二辩认为，提问思维让人先定义目标、边界和评价标准，避免把时间投入到低收益的语法训练中。",
        )
        ref_aff3 = section_text(
            reference_sections,
            ["正方三辩陈词"],
            "正方三辩追问：只强调提问而不强调可执行拆解，如何保证答案能落地并被验证？",
        )
        ref_neg3 = section_text(
            reference_sections,
            ["反方三辩陈词"],
            "反方三辩认为，提问思维不是空泛表达，而是先确定问题边界、判断标准和行动优先级。",
        )
        ref_free_aff3 = free_debate_excerpt(
            reference_sections,
            "正方三辩",
            "自由辩论中，正方三辩追问：如果只强调提问而不强调可执行拆解，如何保证答案能落地并被验证？",
        )
        ref_neg4_summary = section_text(
            reference_sections,
            ["反方四辩结辩"],
            "反方四辩总结认为，AI 时代真正重要的是提出问题、设定标准和验收结果，而不是全民成为半吊子的程序员。",
        )
        ref_aff4_summary = section_text(
            reference_sections,
            ["正方四辩结辩"],
            "正方四辩总结认为，AI 让代码变得廉价，却让逻辑思维更昂贵；编程训练仍是人机协作中最精准的接口。",
        )
        rec.require(
            "reference debate flow loaded",
            bool(reference_sections) or not args.reference_flow_file,
            f"sections={len(reference_sections)} file={args.reference_flow_file or 'none'}",
            allowed_failure=bool(args.reference_flow_file and not reference_sections),
        )

        def partial_count(text: str) -> int:
            return max(4, min(args.max_partial_count, len(text) // args.partial_chars + 1))

        def synthetic_latency(text: str) -> int:
            return max(900, min(180000, int(len(text) / max(args.synthetic_chars_per_second, 1.0) * 1000)))

        if args.reference_flow_file and reference_sections:
            rec.add(
                "reference scenario selected",
                True,
                "using provided debate manuscript for long human turns and summaries",
            )
        phase_names = [phase.get("name") for phase in snapshot.get("phases", [])]
        rec.require(
            "standard flow names match rehearsal baseline",
            phase_names
            == [
                "正方一辩立论",
                "反方一辩立论",
                "正方二辩陈词",
                "反方二辩陈词",
                "正方三辩陈词",
                "反方三辩陈词",
                "自由辩论",
                "反方四辩总结",
                "正方四辩总结",
            ],
            " -> ".join(str(name) for name in phase_names),
        )

        screen = WSClient("screen", ws_url(args.base_url, "screen", api.token_for("screen")), rec)
        await screen.connect()
        sockets.append(screen)

        human_speakers = [speaker for speaker in snapshot.get("speakers", []) if speaker.get("speaker_type") == "human"]
        agent_speakers = [speaker for speaker in snapshot.get("speakers", []) if speaker.get("speaker_type") == "agent"]
        rec.require(
            "lineup role configuration loaded",
            len(human_speakers) + len(agent_speakers) == len(snapshot.get("speakers", [])),
            f"human={len(human_speakers)} agent={len(agent_speakers)}",
        )
        for speaker in human_speakers:
            speaker_id = str(speaker["id"])
            ws = WSClient(speaker_id, ws_url(args.base_url, "speaker", api.token_for("speaker", speaker_id), speaker_id), rec)
            await ws.connect()
            sockets.append(ws)
            await ws.send(
                {
                    "type": "speaker.heartbeat",
                    "payload": {
                        "speaker_id": speaker_id,
                        "mic_permission": "granted",
                        "device_label": f"E2E virtual microphone {speaker_id}",
                    },
                }
            )
        if human_speakers:
            snapshot = await wait_human_consoles_online(api, len(human_speakers))
            rec.require(
                "configured human consoles online",
                snapshot["speech_service"]["consoles"]["online"] >= len(human_speakers),
                str(snapshot["speech_service"]["consoles"]),
            )
        else:
            rec.add("configured human consoles online", True, "current lineup has no human speakers")

        await api.request("POST", "/api/matches/current/start", role="host", name="host starts match")
        await screen.wait_for("match.resumed", timeout=5)

        phases = sorted(snapshot.get("phases", []), key=lambda item: int(item.get("display_order") or 0))
        free_phase = next((phase for phase in phases if phase.get("phase_type") == "free_debate"), None)
        rec.require("flow contains free debate phase", bool(free_phase), str([phase.get("name") for phase in phases]))
        free_entered: Optional[Dict[str, Any]] = None
        for phase in phases:
            if phase.get("phase_type") == "free_debate":
                free_entered = await wait_snapshot(api)
                if free_entered["match"].get("current_phase_id") != phase["id"]:
                    free_entered = await api.request(
                        "POST",
                        f"/api/matches/current/phases/{phase['id']}/start",
                        role="host",
                        name="jump to free debate when no prior single phase",
                    )
                    free_entered = free_entered["data"]
                break
            await complete_configured_single_phase(
                api,
                phase,
                reference_sections,
                partial_count_fn=partial_count,
                latency_fn=synthetic_latency,
            )
            advanced = await api.request("POST", "/api/matches/current/phases/next", role="host", name=f"advance after {phase['name']}")
            if free_phase and advanced["data"]["match"].get("current_phase_id") == free_phase["id"]:
                free_entered = advanced["data"]
                break

        if free_entered is None:
            free_entered = await wait_snapshot(api)
        free_phase = phase_by_type(free_entered, "free_debate")
        rec.require("free debate entered by phase type", free_entered["match"]["current_phase_id"] == free_phase["id"], free_phase["id"])
        rec.require("screen live mode switches to free debate", free_entered["match"]["live_mode"] == "free", free_entered["match"]["live_mode"])
        free_clocks = clock_by_name(free_entered)
        expected_turn_seconds = int(free_phase.get("turn_seconds") or 15)
        expected_side_seconds = int(free_phase.get("side_total_seconds") or max(1, int(free_phase.get("duration_seconds") or 240) // 2))
        rec.require(
            "free debate clock model follows configured timing",
            free_clocks.get("turn", {}).get("total_seconds") == expected_turn_seconds
            and free_clocks.get("affirmative_total", {}).get("total_seconds") == expected_side_seconds
            and free_clocks.get("negative_total", {}).get("total_seconds") == expected_side_seconds,
            str(free_clocks),
        )

        current_side = str((free_entered.get("free_debate") or {}).get("current_turn_side") or "affirmative")
        opposite_side = "negative" if current_side == "affirmative" else "affirmative"
        wrong_human = next(iter(speakers_on_side(free_entered, opposite_side, "human")), None)
        if wrong_human:
            wrong_side = await api.request(
                "POST",
                f"/api/matches/current/speakers/{wrong_human['id']}/start-speaking",
                role="speaker",
                speaker_id=str(wrong_human["id"]),
                expected=(409,),
                name="wrong-side free debate rejected",
            )
            rec.require("wrong-side rejection code", (wrong_side.get("error") or {}).get("code") == "invalid_speaker", str(wrong_side.get("error")))
        else:
            rec.add("wrong-side free debate rejected", True, "opposite side has no human speaker to attempt wrong-side start")

        expected_auto_takeover = False
        auto_events_before = 0
        opposite_humans = speakers_on_side(free_entered, opposite_side, "human")
        opposite_agents = speakers_on_side(free_entered, opposite_side, "agent")
        if opposite_humans and opposite_agents:
            expected_auto_takeover = True
            auto_baseline = await api.request(
                "GET",
                "/api/matches/current/data-summary",
                role="host",
                name="free debate auto-agent event baseline",
            )
            auto_events_before = int((auto_baseline.get("data", {}).get("event_type_counts") or {}).get("free_debate.auto_agent", 0))
            for speaker in opposite_humans:
                await api.request(
                    "POST",
                    f"/api/matches/current/speakers/{speaker['id']}/free-debate-skip",
                    role="speaker",
                    speaker_id=str(speaker["id"]),
                    name=f"{speaker['id']} pre-skip next free turn",
                )

        await complete_free_debate_turn(
            api,
            reference_sections,
            partial_count_fn=partial_count,
            latency_fn=synthetic_latency,
        )
        free_snapshot = await wait_snapshot(api)
        free_after_speech_clocks = clock_by_name(free_snapshot)
        rec.require(
            "free debate remains in dual-countdown mode after human turn",
            free_snapshot["match"]["live_mode"] == "free"
            and "affirmative_total" in free_after_speech_clocks
            and "negative_total" in free_after_speech_clocks,
            f"live_mode={free_snapshot['match']['live_mode']} clocks={list(free_after_speech_clocks)}",
        )
        if expected_auto_takeover:
            auto_snapshot = await recover_or_finish_agent(
                api,
                [str(speaker["id"]) for speaker in opposite_agents],
                fallback_text=f"自由辩论中，{side_label(opposite_side)}AI 接管发言，指出目标定义比执行路径更先决定胜负。",
                fallback_reason="e2e_free_debate_agent_unreachable",
                label="free debate skipped-side auto-agent",
            )
            auto_summary = await api.request(
                "GET",
                "/api/matches/current/data-summary",
                role="host",
                name="free debate auto-agent event after skip",
            )
            auto_events_after = int((auto_summary.get("data", {}).get("event_type_counts") or {}).get("free_debate.auto_agent", 0))
            rec.require(
                "free debate auto-agent took skipped side",
                auto_events_after > auto_events_before,
                f"events_before={auto_events_before} events_after={auto_events_after} current_fd={auto_snapshot.get('free_debate')}",
            )
        else:
            rec.add("free debate auto-agent took skipped side", True, "current lineup has no mixed human+agent opposite side for pre-skip takeover")

        await api.request("POST", "/api/matches/current/emergency-stop", role="admin", json_body={"reason": "e2e emergency drill"}, name="admin emergency stop")
        emergency = await wait_snapshot(api)
        rec.require("emergency state entered", emergency["match"]["status"] == "intervention", emergency["match"]["status"])
        if human_speakers:
            blocked = await api.request(
                "POST",
                f"/api/matches/current/speakers/{human_speakers[0]['id']}/start-speaking",
                role="speaker",
                speaker_id=str(human_speakers[0]["id"]),
                expected=(409,),
                name="controls blocked during intervention",
            )
        elif agent_speakers:
            blocked = await api.request(
                "POST",
                f"/api/matches/current/speakers/{agent_speakers[0]['id']}/start-agent-speaking",
                role="speaker",
                speaker_id=str(agent_speakers[0]["id"]),
                expected=(409,),
                name="controls blocked during intervention",
            )
        else:
            raise AssertionError("lineup has neither human nor agent speakers")
        rec.require("intervention blocks controls", (blocked.get("error") or {}).get("code") == "invalid_state", str(blocked.get("error")))
        await api.request("POST", "/api/matches/current/resume", role="host", name="resume after emergency")

        advanced = await api.request("POST", "/api/matches/current/phases/next", role="host", name="advance after free debate")
        finished = advanced
        post_free_phases = [phase for phase in phases if free_phase and int(phase.get("display_order") or 0) > int(free_phase.get("display_order") or 0)]
        for phase in post_free_phases:
            snapshot = await wait_snapshot(api)
            if snapshot["match"].get("current_phase_id") != phase["id"]:
                continue
            await complete_configured_single_phase(
                api,
                phase,
                reference_sections,
                partial_count_fn=partial_count,
                latency_fn=synthetic_latency,
            )
            finished = await api.request("POST", "/api/matches/current/phases/next", role="host", name=f"advance after {phase['name']}")
        if finished["data"]["match"]["status"] != "finished":
            rec.add(
                "phase next left match running",
                True,
                f"current_phase_id={finished['data']['match'].get('current_phase_id')}; using explicit host finish control",
            )
            finished = await api.request("POST", "/api/matches/current/finish", role="host", name="host explicitly finishes match")
        rec.require("match finished after full scenario", finished["data"]["match"]["status"] == "finished", finished["data"]["match"]["status"])

        tts_probe = await api.request(
            "POST",
            "/api/matches/current/speech/tts/probe",
            role="host",
            json_body={"text": "语音链路自检：这是一段短句，用于确认本地 Qwen TTS 仍可合成。"},
            name="local Qwen TTS probe through phdebate",
            allowed_failure=True,
        )
        if tts_probe.get("ok"):
            rec.require("TTS probe returned snapshot", "snapshot" in tts_probe.get("data", {}), "tts probe ok")
        else:
            rec.add("TTS probe degradation recorded", False, str(tts_probe.get("error")), allowed_failure=True)

        logs = await api.request("GET", "/api/matches/current/data-summary", role="host", name="data summary after e2e")
        counts = logs["data"]["counts"]
        if agent_speakers:
            rec.require("agent requests logged", counts.get("agent_requests", 0) >= 1, str(counts))
        else:
            rec.add("agent requests logged", True, "current lineup has no agent speakers")
        rec.require("transcripts accumulated", counts.get("transcript_segments", 0) >= 7, str(counts))
        rec.require("screen received realtime events", screen.last_seq >= 10, f"screen last_seq={screen.last_seq} counts={screen.counts}")

        print("\n=== E2E_REAL_FLOW_REPORT ===")
        print(json.dumps([step.__dict__ for step in rec.steps], ensure_ascii=False, indent=2))
        return rec.exit_code()
    finally:
        for ws in sockets:
            await ws.close()
        await api.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a realistic phdebate debate flow with failure recovery checks.")
    parser.add_argument("--base-url", default=os.getenv("PHDEBATE_E2E_BASE_URL", "http://127.0.0.1:6006"))
    parser.add_argument("--env-file", default=os.getenv("PHDEBATE_E2E_ENV_FILE", ".env"))
    parser.add_argument("--agent-endpoint", default=os.getenv("PHDEBATE_AGENT_BASE_URL", AGENT_ENDPOINT))
    parser.add_argument("--reference-flow-file", default=os.getenv("PHDEBATE_E2E_REFERENCE_FLOW", str(DEFAULT_REFERENCE_FLOW) if DEFAULT_REFERENCE_FLOW.exists() else ""))
    parser.add_argument("--self-intro", action=argparse.BooleanOptionalAction, default=os.getenv("PHDEBATE_E2E_SELF_INTRO", "1").strip().lower() not in {"0", "false", "no", "off"})
    parser.add_argument("--partial-chars", type=int, default=int(os.getenv("PHDEBATE_E2E_PARTIAL_CHARS", "260")))
    parser.add_argument("--max-partial-count", type=int, default=int(os.getenv("PHDEBATE_E2E_MAX_PARTIAL_COUNT", "10")))
    parser.add_argument("--synthetic-chars-per-second", type=float, default=float(os.getenv("PHDEBATE_E2E_CHARS_PER_SECOND", "5.4")))
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
