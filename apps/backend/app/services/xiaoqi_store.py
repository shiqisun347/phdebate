"""小七（自研智能体）配置与命令下发。

需求 admin.md §2.7 / §4.2：
- 管理员设置小七的 prompt（自我介绍 / 评价辩论 / 给出结果 / 自定义问题）、形象图、请求地址与请求体；
- 系统只负责发送命令/请求，小七的发音依赖小七自身；
- 每个功能可直接点击测试。

设计：全局单例 + storage/xiaoqi.json 持久化（同 integration_config / ruleset_store）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from app.services.sqlite_repo import project_root


def _under_pytest() -> bool:
    return "pytest" in sys.modules


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


COMMANDS = ("intro", "commentary", "result", "custom")

DEFAULT_PROMPTS = {
    "intro": "你是人机辩论赛的自研智能体「小七」。请用 30 秒做一个简洁、自信、友好的自我介绍。",
    "commentary": "你是辩论赛点评嘉宾「小七」。请基于本场辩论的完整过程，从立论、交锋、结辩三个维度做专业、客观的点评。",
    "result": "你是辩论赛评委「小七」。请基于本场辩论，给出你认为的获胜方与最佳辩手，并简要说明理由。",
    "custom": "",
}


def _default_path() -> Path:
    raw = os.getenv("PHDEBATE_XIAOQI_FILE", "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else project_root() / p
    return project_root() / "apps" / "backend" / "storage" / "xiaoqi.json"


class XiaoqiStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _default_path()
        self._lock = threading.Lock()
        self.config: Dict[str, Any] = self._defaults()
        self._load()

    def _defaults(self) -> Dict[str, Any]:
        return {
            "enabled": True,
            "name": "小七",
            "image_url": "",
            "endpoint": "",
            # 给小七推送接口（celebration-api match_record/update）。比赛记录无需单独接口，
            # 取当前辩论实况组装 {session_id, match_record} 后 POST 到这里。
            "match_record_endpoint": "",
            "session_id": "default",
            "request_method": "POST",
            "api_key_env": "",
            "timeout_ms": 30000,
            "prompts": dict(DEFAULT_PROMPTS),
            "request_template": {
                "command": "{command}",
                "prompt": "{prompt}",
                "debate_topic": "{debate_topic}",
                "debate_history": "{debate_history}",
            },
            "updated_at": _now(),
        }

    def _load(self) -> None:
        if _under_pytest():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            merged = self._defaults()
            merged.update({k: v for k, v in data.items() if k != "prompts"})
            if isinstance(data.get("prompts"), dict):
                merged["prompts"] = {**DEFAULT_PROMPTS, **data["prompts"]}
            # 清理已废弃字段（旧版「结果显示接口/模板」已并入单一推送接口），并回写持久化文件。
            deprecated = [k for k in ("result_endpoint", "result_template") if k in merged]
            for key in deprecated:
                merged.pop(key, None)
            self.config = merged
            if deprecated:
                self._save()

    def _save(self) -> None:
        if _under_pytest():
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def public(self) -> Dict[str, Any]:
        with self._lock:
            cfg = dict(self.config)
            cfg["api_key_configured"] = bool(os.getenv(cfg.get("api_key_env", ""), "")) if cfg.get("api_key_env") else False
            return cfg

    def update(self, body: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            for key in ("enabled", "name", "image_url", "endpoint", "match_record_endpoint", "session_id", "request_method", "api_key_env", "timeout_ms"):
                if key in body and body[key] is not None:
                    self.config[key] = body[key]
            if isinstance(body.get("prompts"), dict):
                self.config["prompts"] = {**self.config.get("prompts", {}), **body["prompts"]}
            if isinstance(body.get("request_template"), dict):
                self.config["request_template"] = body["request_template"]
            self.config["updated_at"] = _now()
            self._save()
        return self.public()

    @staticmethod
    def _fill_template(value: Any, substitutions: Dict[str, Any]) -> Any:
        """把模板里的整串占位符（形如 ``"{key}"``）替换为对应的值。

        仅当字符串本身就是单个 ``{key}`` 时替换为实际值（可为 list/dict），
        其它内容原样保留；dict / list 递归处理。"""
        if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
            return substitutions.get(value[1:-1], value)
        if isinstance(value, dict):
            return {k: XiaoqiStore._fill_template(v, substitutions) for k, v in value.items()}
        if isinstance(value, list):
            return [XiaoqiStore._fill_template(v, substitutions) for v in value]
        return value

    def build_payload(self, command: str, *, question: str = "", context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        cfg = self.public()
        prompts = cfg.get("prompts", {})
        prompt = question if command == "custom" and question else prompts.get(command, "")
        ctx = context or {}
        substitutions = {
            "command": command,
            "prompt": prompt,
            "debate_topic": ctx.get("debate_topic", ""),
            "debate_history": ctx.get("debate_history", []),
        }
        template = cfg.get("request_template") or {}
        payload = self._fill_template(template, substitutions)
        if isinstance(payload, dict):
            payload.setdefault("command", command)
            payload.setdefault("prompt", prompt)
        return payload

    async def send(self, command: str, *, question: str = "", context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if command not in COMMANDS:
            raise ValueError(f"未知命令：{command}")
        cfg = self.public()
        endpoint = (cfg.get("endpoint") or "").strip()
        payload = self.build_payload(command, question=question, context=context)
        if not endpoint:
            # 未配置请求地址：仅返回将要发送的请求体，便于调试。
            return {"sent": False, "reason": "未配置小七请求地址", "payload": payload}
        headers = {"Content-Type": "application/json"}
        env = cfg.get("api_key_env")
        if env and os.getenv(env):
            headers["Authorization"] = f"Bearer {os.getenv(env)}"
        method = (cfg.get("request_method") or "POST").upper()
        timeout = max(1.0, float(cfg.get("timeout_ms", 30000)) / 1000.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(method, endpoint, json=payload, headers=headers)
            body: Any
            try:
                body = resp.json()
            except ValueError:
                body = resp.text[:2000]
            return {"sent": True, "status_code": resp.status_code, "response": body, "payload": payload}
        except Exception as exc:  # noqa: BLE001
            return {"sent": False, "reason": str(exc), "payload": payload}

    async def push_match_record(
        self, match_record: list, *, session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """向小七推送本场完整比赛记录（celebration-api `match_record/update` 接口）。

        payload 形如 ``{"session_id": "default", "match_record": [...]}``；
        match_record 与 Agent 的 debate_history 同构
        （``[{"stage", "message": [{"speaker", "content"}]}]``）。
        仅手动触发（管理端/控制台按钮），不在发言流程里自动调用，
        与 `send()`（命令下发）相互独立，互不影响。
        """
        cfg = self.public()
        endpoint = (cfg.get("match_record_endpoint") or "").strip()
        sid = (session_id or cfg.get("session_id") or "default").strip() or "default"
        payload = {"session_id": sid, "match_record": match_record}
        if not endpoint:
            return {"sent": False, "reason": "未配置小七比赛记录接口地址", "payload": payload}
        headers = {"Content-Type": "application/json"}
        env = cfg.get("api_key_env")
        if env and os.getenv(env):
            headers["Authorization"] = f"Bearer {os.getenv(env)}"
        timeout = max(1.0, float(cfg.get("timeout_ms", 30000)) / 1000.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(endpoint, json=payload, headers=headers)
            body: Any
            try:
                body = resp.json()
            except ValueError:
                body = resp.text[:2000]
            return {"sent": True, "status_code": resp.status_code, "response": body, "payload": payload}
        except Exception as exc:  # noqa: BLE001
            return {"sent": False, "reason": str(exc), "payload": payload}


xiaoqi_store = XiaoqiStore()
