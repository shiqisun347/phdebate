from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models.entities import Room, Speech, VoiceTelemetry
from sqlalchemy import select
from sqlalchemy.orm import Session

VOICE_PHASES = frozenset(
    {
        "speech_created",
        "request_start",
        "agent_first_readable_delta",
        "agent_final",
        "tts_start",
        "tts_first_pcm",
        "livekit_first_capture",
        "browser_first_audible",
        "playout_completed",
    }
)
NETWORK_FIELDS = frozenset(
    {
        "bitrate_bps",
        "bytes_received",
        "packets_received",
        "packets_lost",
        "jitter_ms",
        "concealed_samples",
        "jitter_buffer_delay_ms",
        "jitter_buffer_delay_avg_ms",
        "jitter_buffer_delay_current_ms",
        "jitter_buffer_delay_total_ms",
        "jitter_buffer_emitted_count",
    }
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def _row(db: Session, room: Room, speech: Speech) -> VoiceTelemetry:
    item = db.scalar(select(VoiceTelemetry).where(VoiceTelemetry.speech_id == speech.id))
    if item:
        return item
    item = VoiceTelemetry(room_id=room.id, match_id=speech.match_id, speech_id=speech.id)
    db.add(item)
    db.flush()
    return item


def mark_voice_phase(
    db: Session,
    room: Room,
    speech: Speech,
    phase: str,
    *,
    occurred_at: datetime | None = None,
    generation: str = "",
    detail: dict[str, Any] | None = None,
) -> VoiceTelemetry:
    if phase not in VOICE_PHASES:
        raise ValueError(f"unsupported voice phase: {phase}")
    item = _row(db, room, speech)
    phases = dict(item.phases or {})
    if phase not in phases:
        value: dict[str, Any] = {"at": _iso(occurred_at or utcnow())}
        if detail:
            value["detail"] = {
                key: detail[key]
                for key in ("mode", "source", "transport")
                if key in detail and isinstance(detail[key], (str, int, float, bool))
            }
        phases[phase] = value
        item.phases = phases
    if generation and not item.generation:
        item.generation = generation[:64]
    return item


def record_browser_report(
    db: Session,
    room: Room,
    speech: Speech,
    *,
    generation: str,
    event: str,
    metrics: dict[str, Any],
    received_at: datetime | None = None,
) -> VoiceTelemetry:
    at = received_at or utcnow()
    item = _row(db, room, speech)
    if item.generation and generation != item.generation:
        raise ValueError("generation_mismatch")
    if not item.generation:
        item.generation = generation[:64]
    if event == "browser_first_audible":
        mark_voice_phase(db, room, speech, event, occurred_at=at, generation=generation)
    elif event == "browser_network_sample":
        summary = dict(item.network_summary or {})
        last_at = _parse_phase_at(summary.get("last_report_at"))
        # A single global two-second write window protects the database from a
        # malicious or broken browser while still retaining useful worst-case
        # room diagnostics from the small admitted audience.
        if last_at is not None and (at - last_at).total_seconds() < 2:
            return item
        clean: dict[str, float | int] = {}
        for key in NETWORK_FIELDS:
            value = metrics.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            clean[key] = round(max(0.0, min(float(value), 1_000_000_000.0)), 3)
        # WebRTC exposes jitterBufferDelay as a cumulative number of seconds.
        # Older browsers sent that total in ``jitter_buffer_delay_ms``.  Accept
        # those reports, but normalize the established field to the useful
        # lifetime average and retain the raw total under an explicit key.
        emitted_count = float(clean.get("jitter_buffer_emitted_count") or 0)
        raw_total_ms = clean.get("jitter_buffer_delay_total_ms")
        if raw_total_ms is None and "jitter_buffer_delay_avg_ms" not in clean:
            raw_total_ms = clean.get("jitter_buffer_delay_ms")
        if raw_total_ms is not None:
            clean["jitter_buffer_delay_total_ms"] = raw_total_ms
        average_ms = clean.get("jitter_buffer_delay_avg_ms")
        if average_ms is None and raw_total_ms is not None:
            average_ms = round(float(raw_total_ms) / emitted_count, 3) if emitted_count > 0 else 0.0
        if average_ms is not None:
            clean["jitter_buffer_delay_avg_ms"] = average_ms
            clean["jitter_buffer_delay_ms"] = average_ms
        if "jitter_buffer_delay_current_ms" not in clean and raw_total_ms is not None:
            previous_last = dict(summary.get("last") or {})
            previous_total_ms = previous_last.get("jitter_buffer_delay_total_ms")
            previous_emitted_count = previous_last.get("jitter_buffer_emitted_count")
            if (
                isinstance(previous_total_ms, (int, float))
                and isinstance(previous_emitted_count, (int, float))
                and emitted_count > float(previous_emitted_count)
                and float(raw_total_ms) >= float(previous_total_ms)
            ):
                clean["jitter_buffer_delay_current_ms"] = round(
                    (float(raw_total_ms) - float(previous_total_ms))
                    / (emitted_count - float(previous_emitted_count)),
                    3,
                )
            elif average_ms is not None:
                clean["jitter_buffer_delay_current_ms"] = average_ms
        previous_worst = dict(summary.get("worst") or {})
        worst = {
            key: max(float(previous_worst.get(key) or 0), float(value))
            for key, value in clean.items()
            if key
            in {
                "packets_lost",
                "jitter_ms",
                "concealed_samples",
                "jitter_buffer_delay_ms",
                "jitter_buffer_delay_avg_ms",
                "jitter_buffer_delay_current_ms",
            }
        }
        summary.update({"last_report_at": _iso(at), "last": clean, "worst": worst})
        item.network_summary = summary
    else:
        raise ValueError("unsupported_browser_event")
    item.browser_report_count = min(1_000_000, int(item.browser_report_count or 0) + 1)
    return item


def _parse_phase_at(value: Any) -> datetime | None:
    if isinstance(value, dict):
        value = value.get("at")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def safe_voice_summary(item: VoiceTelemetry, speech: Speech) -> dict[str, Any]:
    phases = dict(item.phases or {})

    def elapsed(start: str, end: str) -> int | None:
        left = _parse_phase_at(phases.get(start))
        right = _parse_phase_at(phases.get(end))
        if left is None or right is None:
            return None
        return max(0, round((right - left).total_seconds() * 1000))

    return {
        "speech_id": speech.id,
        "seat_key": speech.seat_key,
        "stage_key": speech.stage_key,
        "status": speech.status,
        "generation": item.generation,
        "phases": phases,
        "latency_ms": {
            "request_to_agent_first_delta": elapsed("request_start", "agent_first_readable_delta"),
            "agent_first_delta_to_tts_first_pcm": elapsed("agent_first_readable_delta", "tts_first_pcm"),
            "tts_start_to_first_pcm": elapsed("tts_start", "tts_first_pcm"),
            "first_pcm_to_livekit_capture": elapsed("tts_first_pcm", "livekit_first_capture"),
            "livekit_capture_to_browser_audible": elapsed("livekit_first_capture", "browser_first_audible"),
            "request_to_browser_audible": elapsed("request_start", "browser_first_audible"),
            "request_to_playout_completed": elapsed("request_start", "playout_completed"),
        },
        "network": dict(item.network_summary or {}),
        "browser_report_count": item.browser_report_count,
        "updated_at": item.updated_at.isoformat(),
    }
