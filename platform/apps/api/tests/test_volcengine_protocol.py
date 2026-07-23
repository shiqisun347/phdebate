from __future__ import annotations

import json
import struct

from app.services.voice_runtime.volcengine_protocol import (
    EventType,
    MessageFlag,
    MessageType,
    cancel_session_frame,
    parse_message,
    start_connection_frame,
    task_request_frame,
)


def _server_frame(
    message_type: MessageType,
    event: EventType,
    payload: bytes,
    *,
    session_id: str = "",
    connect_id: str = "",
    serialization: int = 1,
) -> bytes:
    frame = bytearray((0x11, (message_type << 4) | MessageFlag.WITH_EVENT, serialization << 4, 0))
    frame.extend(struct.pack(">i", event))
    if event not in {
        EventType.START_CONNECTION,
        EventType.FINISH_CONNECTION,
        EventType.CONNECTION_STARTED,
        EventType.CONNECTION_FAILED,
        EventType.CONNECTION_FINISHED,
    }:
        encoded_session = session_id.encode()
        frame.extend(struct.pack(">I", len(encoded_session)))
        frame.extend(encoded_session)
    if event in {
        EventType.CONNECTION_STARTED,
        EventType.CONNECTION_FAILED,
        EventType.CONNECTION_FINISHED,
    }:
        encoded_connect = connect_id.encode()
        frame.extend(struct.pack(">I", len(encoded_connect)))
        frame.extend(encoded_connect)
    frame.extend(struct.pack(">I", len(payload)))
    frame.extend(payload)
    return bytes(frame)


def test_client_frames_keep_credentials_out_of_payload() -> None:
    frame = start_connection_frame()
    assert frame[:4] == bytes((0x11, 0x14, 0x10, 0x00))
    assert b"api" not in frame.lower()

    request = {"req_params": {"speaker": "voice", "audio_params": {"format": "pcm"}}}
    task = task_request_frame("session-1", request, "你好")
    assert b"session-1" in task
    assert "你好" in task.decode("utf-8", errors="ignore")
    assert cancel_session_frame("session-1")


def test_parse_full_response_and_audio_frame() -> None:
    response = parse_message(
        _server_frame(
            MessageType.FULL_SERVER_RESPONSE,
            EventType.CONNECTION_STARTED,
            json.dumps({"ok": True}).encode(),
            connect_id="connect-1",
        )
    )
    assert response.event == EventType.CONNECTION_STARTED
    assert response.connect_id == "connect-1"
    assert response.json_payload() == {"ok": True}

    pcm = b"\x01\x00\x02\x00"
    audio = parse_message(
        _server_frame(
            MessageType.AUDIO_ONLY_SERVER,
            EventType.TTS_RESPONSE,
            pcm,
            session_id="session-1",
            serialization=0,
        )
    )
    assert audio.session_id == "session-1"
    assert audio.payload == pcm
    assert audio.json_payload() == {}


def test_parse_rejects_truncated_payload() -> None:
    frame = _server_frame(
        MessageType.FULL_SERVER_RESPONSE,
        EventType.SESSION_FINISHED,
        b"{}",
        session_id="session-1",
    )
    broken = frame[:-1]
    try:
        parse_message(broken)
    except ValueError as exc:
        assert "payload length" in str(exc)
    else:
        raise AssertionError("truncated frame was accepted")
