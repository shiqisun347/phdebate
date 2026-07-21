from __future__ import annotations

import hashlib
import json
from typing import Any

PARTICIPANT_ARCHIVE_PROJECTION = "participant"
PARTICIPANT_EVENT_TYPES = frozenset(
    {
        "room.started",
        "room.cancelled",
        "stage.started",
        "stage.completed",
        "stage.advanced",
        "speech.started",
        "speech.completed",
        "speech.interrupted",
        "speech.timed_out",
        "speech.late_finalized",
        "speech.corrected",
        "free.side_changed",
        "free.turn_timed_out",
        "control.pause",
        "control.resume",
        "control.terminate",
        "judge.review_required",
        "judge.reviewed",
        "judge.corrected",
        "match.completed",
    }
)
PARTICIPANT_EVENT_FIELDS = frozenset(
    {
        "topic",
        "seat_key",
        "speaker_type",
        "stage_key",
        "stage_index",
        "side",
        "content",
        "audio_url",
        "duration_seconds",
        "winner",
        "affirmative_score",
        "negative_score",
        "reason",
    }
)
STAGE_FIELDS = frozenset({"key", "name", "kind", "side", "seat", "duration", "turn_duration", "cue"})


def _primitive(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _safe_event_payload(event_type: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = {
        key: value
        for key, value in payload.items()
        if key in PARTICIPANT_EVENT_FIELDS and _primitive(value)
    }
    stage = payload.get("stage")
    if isinstance(stage, dict):
        result["stage"] = {
            key: value for key, value in stage.items() if key in STAGE_FIELDS and _primitive(value)
        }
    if event_type == "judge.corrected":
        for key in ("old", "new"):
            value = payload.get(key)
            if isinstance(value, dict):
                result[key] = {
                    field: value[field]
                    for field in ("winner", "affirmative_score", "negative_score")
                    if field in value and _primitive(value[field])
                }
    return result


def _stage_projection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: field for key, field in value.items() if key in STAGE_FIELDS and _primitive(field)}


def _scorecard_projection(scorecard: Any, seat_keys: set[str]) -> dict[str, Any] | None:
    if not isinstance(scorecard, dict):
        return None
    individual = scorecard.get("individual_scores")
    provenance = scorecard.get("transcript_provenance")
    return {
        "status": scorecard.get("status"),
        "winner": scorecard.get("winner"),
        "affirmative_score": scorecard.get("affirmative_score", 0),
        "negative_score": scorecard.get("negative_score", 0),
        "individual_scores": {
            key: value
            for key, value in (individual.items() if isinstance(individual, dict) else [])
            if key in seat_keys and isinstance(value, (int, float)) and not isinstance(value, bool)
        },
        "reasoning": scorecard.get("reasoning", ""),
        "created_at": scorecard.get("created_at"),
        "updated_at": scorecard.get("updated_at"),
        "transcript_provenance": (
            {
                key: provenance.get(key)
                for key in (
                    "basis",
                    "judged_transcript_sha256",
                    "current_transcript_sha256",
                    "correction_after_judging_count",
                )
            }
            if isinstance(provenance, dict)
            else None
        ),
    }


def participant_archive_document(document: dict[str, Any], *, viewer_user_id: str | None = None) -> dict[str, Any]:
    """Create the deterministic student-readable projection of a research archive."""

    data = document.get("data") if isinstance(document.get("data"), dict) else {}
    match = data.get("match") if isinstance(data.get("match"), dict) else {}
    room = data.get("room") if isinstance(data.get("room"), dict) else {}
    competition = data.get("competition") if isinstance(data.get("competition"), dict) else None
    season = data.get("season") if isinstance(data.get("season"), dict) else None
    seats = data.get("seats") if isinstance(data.get("seats"), list) else []
    participant_snapshots = data.get("participant_snapshots") if isinstance(data.get("participant_snapshots"), list) else []
    seat_keys = {
        item.get("seat_key")
        for item in seats
        if isinstance(item, dict) and isinstance(item.get("seat_key"), str)
    }
    speeches = data.get("speeches") if isinstance(data.get("speeches"), list) else []
    corrections = data.get("speech_corrections") if isinstance(data.get("speech_corrections"), list) else []
    events = data.get("events") if isinstance(data.get("events"), list) else []
    ratings = data.get("rating_changes") if isinstance(data.get("rating_changes"), list) else []

    return {
        "schema": document.get("schema"),
        "version": document.get("version"),
        "projection": PARTICIPANT_ARCHIVE_PROJECTION,
        "generated_at": document.get("generated_at"),
        "data": {
            "match": {
                "id": match.get("id"),
                "status": match.get("status"),
                "winner": match.get("winner"),
                "result_reason": match.get("result_reason", ""),
                "legacy": bool(match.get("legacy", False)),
                "created_at": match.get("created_at"),
                "updated_at": match.get("updated_at"),
            },
            "room": {
                "code": room.get("code"),
                "topic": room.get("topic"),
                "status": room.get("status"),
                "visibility": room.get("visibility"),
                "is_test_data": bool(room.get("is_test_data", False)),
                "template_snapshot": [
                    projected
                    for item in (room.get("template_snapshot") if isinstance(room.get("template_snapshot"), list) else [])
                    if (projected := _stage_projection(item))
                ],
                "started_at": room.get("started_at"),
                "completed_at": room.get("completed_at"),
            },
            "competition": (
                {
                    "slug": competition.get("slug"),
                    "name": competition.get("name"),
                    "format": competition.get("format"),
                    "ranked": bool(competition.get("ranked", False)),
                }
                if competition
                else None
            ),
            "season": (
                {"slug": season.get("slug"), "name": season.get("name")}
                if season
                else None
            ),
            "seats": [
                {
                    "seat_key": item.get("seat_key"),
                    "side": item.get("side"),
                    "position": item.get("position"),
                    "occupant_type": item.get("occupant_type"),
                    "display_name": item.get("display_name"),
                }
                for item in seats
                if isinstance(item, dict)
            ],
            "participant_snapshots": [
                {
                    "seat_key": item.get("seat_key"),
                    "display_name": item.get("display_name", "参赛选手"),
                    "created_at": item.get("created_at"),
                }
                for item in participant_snapshots
                if isinstance(item, dict)
            ],
            "speeches": [
                {
                    "seat_key": item.get("seat_key"),
                    "stage_key": item.get("stage_key"),
                    "speaker_type": item.get("speaker_type"),
                    "content": item.get("content", ""),
                    "audio_url": item.get("audio_url", ""),
                    "duration_seconds": item.get("duration_seconds", 0),
                    "status": item.get("status"),
                    "created_at": item.get("created_at"),
                    "transcript_segments": [
                        {
                            "start_ms": segment.get("start_ms", 0),
                            "end_ms": segment.get("end_ms", 0),
                            "text": segment.get("text", ""),
                            "is_final": bool(segment.get("is_final", False)),
                        }
                        for segment in item.get("transcript_segments", [])
                        if isinstance(segment, dict)
                    ],
                }
                for item in speeches
                if isinstance(item, dict)
            ],
            "speech_corrections": [
                {
                    "speech_id": item.get("speech_id"),
                    "original_content": item.get("original_content", ""),
                    "proposed_content": item.get("proposed_content", ""),
                    "reason": item.get("reason", ""),
                    "status": item.get("status"),
                    "review_reason": item.get("review_reason", ""),
                    "resolved_at": item.get("resolved_at"),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "after_judging": bool(item.get("after_judging", False)),
                }
                for item in corrections
                if isinstance(item, dict) and viewer_user_id is not None and item.get("requester_user_id") == viewer_user_id
            ],
            "timeline": [
                {
                    "seq": item.get("seq"),
                    "type": item.get("type"),
                    "payload": _safe_event_payload(str(item.get("type", "")), item.get("payload")),
                    "created_at": item.get("created_at"),
                }
                for item in events
                if isinstance(item, dict) and item.get("type") in PARTICIPANT_EVENT_TYPES
            ],
            "scorecard": _scorecard_projection(data.get("scorecard"), seat_keys),
            "rating_changes": [
                {
                    "display_name": item.get("display_name", "参赛选手"),
                    "points_delta": item.get("points_delta", 0),
                    "score": item.get("score", 0),
                    "reason": item.get("reason", ""),
                    "source": item.get("source", ""),
                    "created_at": item.get("created_at"),
                }
                for item in ratings
                if isinstance(item, dict)
            ],
        },
    }


def serialize_participant_archive(document: dict[str, Any], *, viewer_user_id: str | None = None) -> tuple[bytes, str]:
    content = (
        json.dumps(
            participant_archive_document(document, viewer_user_id=viewer_user_id),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    return content, hashlib.sha256(content).hexdigest()
