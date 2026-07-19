"""Volcengine TTS V3 bidirectional WebSocket protocol primitives.

The binary framing follows Volcengine's public bidirectional TTS sample and
the Apache-2.0 reference implementation in volcengine/ai-app-lab. Credentials
are intentionally supplied by the caller and are never serialized here.
"""

from __future__ import annotations

import gzip
import json
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class MessageType(IntEnum):
    FULL_CLIENT_REQUEST = 0b0001
    AUDIO_ONLY_CLIENT = 0b0010
    FULL_SERVER_RESPONSE = 0b1001
    AUDIO_ONLY_SERVER = 0b1011
    FRONTEND_RESULT_SERVER = 0b1100
    ERROR = 0b1111


class MessageFlag(IntEnum):
    NO_SEQUENCE = 0b0000
    POSITIVE_SEQUENCE = 0b0001
    LAST_NO_SEQUENCE = 0b0010
    NEGATIVE_SEQUENCE = 0b0011
    WITH_EVENT = 0b0100


class EventType(IntEnum):
    START_CONNECTION = 1
    FINISH_CONNECTION = 2
    CONNECTION_STARTED = 50
    CONNECTION_FAILED = 51
    CONNECTION_FINISHED = 52
    START_SESSION = 100
    CANCEL_SESSION = 101
    FINISH_SESSION = 102
    SESSION_STARTED = 150
    SESSION_CANCELED = 151
    SESSION_FINISHED = 152
    SESSION_FAILED = 153
    USAGE_RESPONSE = 154
    TASK_REQUEST = 200
    TTS_SENTENCE_START = 350
    TTS_SENTENCE_END = 351
    TTS_RESPONSE = 352
    TTS_ENDED = 359
    TTS_SUBTITLE = 364


_CONNECTION_EVENTS_WITHOUT_SESSION = {
    EventType.START_CONNECTION,
    EventType.FINISH_CONNECTION,
    EventType.CONNECTION_STARTED,
    EventType.CONNECTION_FAILED,
    EventType.CONNECTION_FINISHED,
}
_CONNECTION_RESPONSE_EVENTS = {
    EventType.CONNECTION_STARTED,
    EventType.CONNECTION_FAILED,
    EventType.CONNECTION_FINISHED,
}


@dataclass(frozen=True)
class Message:
    message_type: MessageType
    flag: MessageFlag
    event: int | None
    session_id: str
    connect_id: str
    sequence: int | None
    error_code: int | None
    payload: bytes
    serialization: int
    compression: int

    def json_payload(self) -> dict[str, Any]:
        if self.serialization != 1 or not self.payload:
            return {}
        data = gzip.decompress(self.payload) if self.compression == 1 else self.payload
        value = json.loads(data.decode("utf-8"))
        return value if isinstance(value, dict) else {"value": value}


def _pack_sized(value: bytes) -> bytes:
    return struct.pack(">I", len(value)) + value


def encode_message(
    event: EventType,
    *,
    session_id: str = "",
    payload: dict[str, Any] | None = None,
) -> bytes:
    header = bytes((0x11, (MessageType.FULL_CLIENT_REQUEST << 4) | MessageFlag.WITH_EVENT, 0x10, 0x00))
    body = bytearray(struct.pack(">i", int(event)))
    if event not in _CONNECTION_EVENTS_WITHOUT_SESSION:
        body.extend(_pack_sized(session_id.encode("utf-8")))
    encoded_payload = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    body.extend(_pack_sized(encoded_payload))
    return header + bytes(body)


def parse_message(frame: bytes | str) -> Message:
    if isinstance(frame, str):
        frame = frame.encode("utf-8")
    if len(frame) < 8:
        raise ValueError("Volcengine TTS frame is too short")

    header_words = frame[0] & 0x0F
    header_size = header_words * 4
    if header_words < 1 or len(frame) < header_size + 4:
        raise ValueError("Volcengine TTS frame has an invalid header size")
    try:
        message_type = MessageType(frame[1] >> 4)
        flag = MessageFlag(frame[1] & 0x0F)
    except ValueError as exc:
        raise ValueError("Volcengine TTS frame uses an unsupported message type") from exc
    serialization = frame[2] >> 4
    compression = frame[2] & 0x0F
    offset = header_size

    sequence: int | None = None
    error_code: int | None = None
    if flag in {MessageFlag.POSITIVE_SEQUENCE, MessageFlag.NEGATIVE_SEQUENCE}:
        sequence = struct.unpack_from(">i", frame, offset)[0]
        offset += 4
    elif message_type == MessageType.ERROR:
        error_code = struct.unpack_from(">I", frame, offset)[0]
        offset += 4

    event: int | None = None
    session_id = ""
    connect_id = ""
    if flag == MessageFlag.WITH_EVENT:
        event = struct.unpack_from(">i", frame, offset)[0]
        offset += 4
        try:
            event_member = EventType(event)
        except ValueError:
            event_member = None
        if event_member not in _CONNECTION_EVENTS_WITHOUT_SESSION:
            size = struct.unpack_from(">I", frame, offset)[0]
            offset += 4
            session_id = frame[offset : offset + size].decode("utf-8")
            offset += size
        if event_member in _CONNECTION_RESPONSE_EVENTS:
            size = struct.unpack_from(">I", frame, offset)[0]
            offset += 4
            connect_id = frame[offset : offset + size].decode("utf-8")
            offset += size

    if offset + 4 > len(frame):
        raise ValueError("Volcengine TTS frame is missing payload length")
    payload_size = struct.unpack_from(">I", frame, offset)[0]
    offset += 4
    payload = frame[offset : offset + payload_size]
    if len(payload) != payload_size or offset + payload_size != len(frame):
        raise ValueError("Volcengine TTS frame payload length is invalid")

    return Message(
        message_type=message_type,
        flag=flag,
        event=event,
        session_id=session_id,
        connect_id=connect_id,
        sequence=sequence,
        error_code=error_code,
        payload=payload,
        serialization=serialization,
        compression=compression,
    )


def start_connection_frame() -> bytes:
    return encode_message(EventType.START_CONNECTION)


def start_session_frame(session_id: str, request: dict[str, Any]) -> bytes:
    payload = {**request, "event": int(EventType.START_SESSION)}
    return encode_message(EventType.START_SESSION, session_id=session_id, payload=payload)


def task_request_frame(session_id: str, request: dict[str, Any], text: str) -> bytes:
    payload = {
        **request,
        "event": int(EventType.TASK_REQUEST),
        "req_params": {**dict(request.get("req_params") or {}), "text": text},
    }
    return encode_message(EventType.TASK_REQUEST, session_id=session_id, payload=payload)


def finish_session_frame(session_id: str) -> bytes:
    return encode_message(EventType.FINISH_SESSION, session_id=session_id)


def cancel_session_frame(session_id: str) -> bytes:
    return encode_message(EventType.CANCEL_SESSION, session_id=session_id)


def finish_connection_frame() -> bytes:
    return encode_message(EventType.FINISH_CONNECTION)
