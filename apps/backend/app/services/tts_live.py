from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Set, Tuple


LiveKey = Tuple[str, str, str, int]


@dataclass
class _LiveStream:
    meta: Dict[str, Any] = field(default_factory=dict)
    chunks: List[bytes] = field(default_factory=list)
    subscribers: Set[asyncio.Queue] = field(default_factory=set)
    started: bool = False
    done: bool = False
    error: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)


class TTSLiveManager:
    """Short-lived in-memory fanout for live TTS chunks.

    Audio chunks are intentionally not persisted in the event log. The archived
    file URL remains the durable fallback path for screen playback and replay.
    """

    def __init__(self, ttl_seconds: int = 180) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = asyncio.Lock()
        self._streams: Dict[LiveKey, _LiveStream] = {}

    async def start(self, key: LiveKey, meta: Dict[str, Any]) -> None:
        async with self._lock:
            stream = self._streams.setdefault(key, _LiveStream())
            stream.meta = dict(meta)
            stream.started = True
            stream.error = None
            message = {"type": "ready", **stream.meta}
            subscribers = list(stream.subscribers)
        for queue in subscribers:
            await queue.put(message)

    async def publish_chunk(self, key: LiveKey, chunk: bytes, index: int) -> None:
        if not chunk:
            return
        async with self._lock:
            stream = self._streams.setdefault(key, _LiveStream())
            stream.chunks.append(bytes(chunk))
            message = {
                "type": "chunk",
                "index": index,
                "audio_base64": base64.b64encode(chunk).decode("ascii"),
            }
            subscribers = list(stream.subscribers)
        for queue in subscribers:
            await queue.put(message)

    async def finish(self, key: LiveKey, payload: Optional[Dict[str, Any]] = None) -> None:
        async with self._lock:
            stream = self._streams.setdefault(key, _LiveStream())
            stream.done = True
            message = {"type": "done", **(payload or {})}
            subscribers = list(stream.subscribers)
        for queue in subscribers:
            await queue.put(message)
        asyncio.create_task(self._cleanup_later(key))

    async def fail(self, key: LiveKey, message: str) -> None:
        async with self._lock:
            stream = self._streams.setdefault(key, _LiveStream())
            stream.error = message
            stream.done = True
            outgoing = {"type": "error", "message": message}
            subscribers = list(stream.subscribers)
        for queue in subscribers:
            await queue.put(outgoing)
        asyncio.create_task(self._cleanup_later(key))

    async def subscribe(self, key: LiveKey) -> AsyncIterator[Dict[str, Any]]:
        queue: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            stream = self._streams.setdefault(key, _LiveStream())
            stream.subscribers.add(queue)
            if stream.started:
                await queue.put({"type": "ready", **stream.meta})
            for index, chunk in enumerate(stream.chunks, start=1):
                await queue.put(
                    {
                        "type": "chunk",
                        "index": index,
                        "audio_base64": base64.b64encode(chunk).decode("ascii"),
                    }
                )
            if stream.error:
                await queue.put({"type": "error", "message": stream.error})
            elif stream.done:
                await queue.put({"type": "done", "chunk_count": len(stream.chunks)})
        try:
            while True:
                item = await queue.get()
                yield item
                if item.get("type") in {"done", "error"}:
                    break
        finally:
            async with self._lock:
                stream = self._streams.get(key)
                if stream:
                    stream.subscribers.discard(queue)

    async def _cleanup_later(self, key: LiveKey) -> None:
        await asyncio.sleep(self.ttl_seconds)
        async with self._lock:
            stream = self._streams.get(key)
            if stream and (time.monotonic() - stream.created_at) >= self.ttl_seconds:
                self._streams.pop(key, None)


tts_live_manager = TTSLiveManager()
