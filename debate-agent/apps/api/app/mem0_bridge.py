from __future__ import annotations

import json
from typing import Any

from app.config import settings


class Mem0Bridge:
    """Optional reviewed-memory index backed by Mem0.

    SQL remains authoritative for review state and retention. Only approved
    long-term items are added to Mem0, with profile_key as the isolated user.
    """

    def __init__(self) -> None:
        self._client: Any | None = None

    def _get_client(self) -> Any | None:
        if not settings.mem0_enabled:
            return None
        if self._client is None:
            from mem0 import Memory

            config = json.loads(settings.mem0_config_json or "{}")
            self._client = Memory.from_config(config) if config else Memory()
        return self._client

    def add_approved(self, *, memory_id: str, profile_key: str, content: str, metadata: dict[str, Any]) -> str | None:
        try:
            client = self._get_client()
            if client is None:
                return None
            result = client.add(
                content,
                user_id=profile_key,
                metadata={"sql_memory_id": memory_id, "review_status": "approved"} | metadata,
            )
            records = result.get("results", result) if isinstance(result, dict) else result
            first = records[0] if isinstance(records, list) and records else records
            if isinstance(first, dict):
                return str(first.get("id") or first.get("memory_id") or "") or None
        except Exception:
            return None
        return None

    def search(self, *, profile_key: str, query: str, limit: int) -> list[str]:
        try:
            client = self._get_client()
            if client is None or not query.strip() or limit <= 0:
                return []
            result = client.search(query, user_id=profile_key, limit=limit)
            records = result.get("results", result) if isinstance(result, dict) else result
            if not isinstance(records, list):
                return []
            return [
                str(item.get("memory") or item.get("text") or item.get("content") or "").strip()
                for item in records
                if isinstance(item, dict) and str(item.get("memory") or item.get("text") or item.get("content") or "").strip()
            ]
        except Exception:
            return []

    def delete(self, memory_id: str | None) -> None:
        if not memory_id:
            return
        try:
            client = self._get_client()
            if client is not None:
                client.delete(memory_id)
        except Exception:
            return


mem0_bridge = Mem0Bridge()
