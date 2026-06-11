from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, Set

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


app = FastAPI(title="Phdebate Mock Agent", version="0.1.0")
interrupted_tasks: Set[str] = set()


@app.get("/health")
async def health() -> Dict[str, Any]:
    profile = os.getenv("MOCK_AGENT_PROFILE", "normal")
    if profile == "down":
        return {"ok": False, "status": "unavailable", "model": "mock-agent", "latency_ms": 0}
    return {"ok": True, "status": "ready", "model": f"mock-agent-{profile}", "version": "0.1.0", "latency_ms": 24}


@app.post("/speech")
async def speech(body: Dict[str, Any], request: Request):
    profile = os.getenv("MOCK_AGENT_PROFILE", "normal")
    if profile == "http500":
        return JSONResponse(status_code=500, content={"task_id": body.get("task_id"), "status": "failed"})

    stream = bool(body.get("output", {}).get("stream", True))
    chunks = _chunks_for(body)
    if profile == "slow":
        delay = 0.9
    else:
        delay = 0.28

    if not stream:
        await asyncio.sleep(delay)
        return {
            "task_id": body.get("task_id"),
            "status": "completed",
            "content": "".join(chunks),
            "usage": {"model": "mock-agent", "latency_ms": int(delay * 1000)},
            "error": None,
        }

    async def event_stream():
        content = ""
        for index, delta in enumerate(chunks):
            if await request.is_disconnected():
                return
            if body.get("task_id") in interrupted_tasks:
                yield _sse({"type": "error", "task_id": body.get("task_id"), "error": {"code": "interrupted", "message": "task interrupted"}})
                return
            if profile == "flaky" and index == 1:
                yield _sse({"type": "error", "task_id": body.get("task_id"), "error": {"code": "mock_flaky", "message": "simulated stream interruption"}})
                return
            await asyncio.sleep(delay)
            content += delta
            yield _sse({"type": "delta", "task_id": body.get("task_id"), "delta": delta})
        yield _sse({"type": "final", "task_id": body.get("task_id"), "content": content, "usage": {"model": "mock-agent", "latency_ms": int(delay * 1000 * len(chunks))}})
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/interrupt")
async def interrupt(body: Dict[str, Any]) -> Dict[str, Any]:
    task_id = body.get("task_id", "")
    if task_id:
        interrupted_tasks.add(task_id)
    return {"ok": True, "task_id": task_id, "status": "interrupted"}


def _chunks_for(body: Dict[str, Any]) -> list[str]:
    side = body.get("side")
    phase_type = body.get("phase_type")
    if phase_type == "free_debate":
        chunks = (
            ["我方回应对方刚才的观点，", "AI 时代的问题不是少写代码，", "而是更需要能拆解、验证、复盘的行动框架。"]
            if side == "affirmative"
            else ["对方强调拆解步骤，", "但如果问题方向一开始就错了，", "再精密的步骤也只会更快抵达错误答案。"]
        )
    else:
        chunks = (
            ["各位评委、对方辩友，", "我方认为编程思维的核心是结构化解决问题，", "它让人与 AI 的协作更可验证、更可复盘。"]
            if side == "affirmative"
            else ["我方认为提问思维是 AI 时代的第一能力，", "因为问题定义决定了模型、工具与行动路径，", "会问，才可能让 AI 真正服务目标。"]
        )
    target_chars = int(body.get("target_chars") or 0)
    if target_chars <= 0:
        return chunks
    content = ""
    limited = []
    for chunk in chunks:
        if len(content) >= target_chars:
            break
        remaining = target_chars - len(content)
        piece = chunk[:remaining]
        content += piece
        limited.append(piece)
    return limited or [chunks[0][:target_chars]]


def _sse(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
