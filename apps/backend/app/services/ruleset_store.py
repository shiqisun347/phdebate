"""赛制规则库（全局，与具体比赛无关）。

需求 admin.md §2.2：管理员可增删改查赛制规则。每条规则包含：
- 赛制名称 / 赛制简介
- 赛制流程（提供可复制的流程模板，后台调用 AI 生成结构化规则，并展示程序流程图）
- 其他必要信息

设计：参照 integration_config，以独立 JSON 文件持久化于 storage/rulesets.json，
模块级单例，避免与 match_store（单场比赛状态）耦合。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.sqlite_repo import project_root


def _under_pytest() -> bool:
    return "pytest" in sys.modules


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# 可复制的流程模板：每行一个环节，用 | 分隔字段。
FLOW_TEMPLATE = """# 赛制流程模板：每行一个环节，使用 | 分隔字段
# 环节名称 | 持方(正方/反方/中立) | 发言人 | 时长(秒) | 类型(statement/free_debate/summary)
正方一辩立论 | 正方 | 一辩 | 180 | statement
反方一辩立论 | 反方 | 一辩 | 180 | statement
正方二辩陈词 | 正方 | 二辩 | 120 | statement
反方二辩陈词 | 反方 | 二辩 | 120 | statement
正方三辩陈词 | 正方 | 三辩 | 90 | statement
反方三辩陈词 | 反方 | 三辩 | 90 | statement
自由辩论 | 中立 | 双方交替 | 240 | free_debate
反方四辩总结 | 反方 | 四辩 | 180 | summary
正方四辩总结 | 正方 | 四辩 | 180 | summary
"""

_SIDE_MAP = {
    "正方": "affirmative",
    "affirmative": "affirmative",
    "反方": "negative",
    "negative": "negative",
    "中立": "neutral",
    "双方": "neutral",
    "neutral": "neutral",
}


def _default_path() -> Path:
    raw = os.getenv("PHDEBATE_RULESETS_FILE", "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else project_root() / p
    return project_root() / "apps" / "backend" / "storage" / "rulesets.json"


def parse_flow(template_text: str) -> Dict[str, Any]:
    """把流程模板文本解析为结构化环节列表 + 流程图节点。"""
    nodes: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for raw_line in (template_text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        name = parts[0] if parts else ""
        if not name:
            continue
        side_raw = parts[1] if len(parts) > 1 else "中立"
        speaker = parts[2] if len(parts) > 2 else ""
        duration_raw = parts[3] if len(parts) > 3 else "180"
        phase_type = parts[4] if len(parts) > 4 else ""
        side = _SIDE_MAP.get(side_raw, "neutral")
        try:
            duration = int(float(duration_raw))
        except (TypeError, ValueError):
            duration = 180
            warnings.append(f"环节「{name}」时长无法解析，已默认 180 秒。")
        if not phase_type:
            phase_type = "free_debate" if ("自由" in name or side == "neutral") else "statement"
        nodes.append(
            {
                "key": f"phase_{len(nodes) + 1}",
                "name": name,
                "side": side,
                "speaker": speaker,
                "duration_seconds": duration,
                "phase_type": phase_type,
            }
        )
    return {"nodes": nodes, "warnings": warnings}


def _llm_structify(prose: str) -> Optional[str]:
    """当模板是自由文字时，尝试用 OpenAI 兼容模型转成标准模板行。失败返回 None。"""
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip() or os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    base_url = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").strip()
    model = os.getenv("RULESET_LLM_MODEL", "qwen3.6-plus").strip()
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url)
        prompt = (
            "你是辩论赛制助手。请把下面的赛制流程描述整理为规范的流程模板，"
            "每行一个环节，字段用 | 分隔，格式为：环节名称 | 持方(正方/反方/中立) | 发言人 | 时长(秒) | 类型(statement/free_debate/summary)。"
            "只输出模板文本，不要解释。\n\n描述：\n" + prose
        )
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            timeout=30,
        )
        return resp.choices[0].message.content or None
    except Exception:
        return None


def generate_flow(template_text: str, *, use_ai: bool = True) -> Dict[str, Any]:
    """生成结构化流程。优先按模板解析；若没有可识别行且开启 AI，则调用模型结构化。"""
    parsed = parse_flow(template_text)
    ai_used = False
    if not parsed["nodes"] and use_ai:
        structured = _llm_structify(template_text)
        if structured:
            parsed = parse_flow(structured)
            parsed["normalized_template"] = structured
            ai_used = True
    mermaid = _to_mermaid(parsed["nodes"])
    return {**parsed, "mermaid": mermaid, "ai_used": ai_used}


def _to_mermaid(nodes: List[Dict[str, Any]]) -> str:
    lines = ["flowchart TD", "  start([比赛开始])"]
    prev = "start"
    for i, n in enumerate(nodes):
        nid = f"n{i}"
        label = f"{n['name']}<br/>{n.get('speaker') or ''} {n['duration_seconds']}s"
        lines.append(f'  {nid}["{label}"]')
        lines.append(f"  {prev} --> {nid}")
        prev = nid
    lines.append("  done([比赛结束])")
    lines.append(f"  {prev} --> done")
    return "\n".join(lines)


class RulesetStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _default_path()
        self._lock = threading.Lock()
        self.rulesets: List[Dict[str, Any]] = []
        self._load()
        if not self.rulesets:
            self._seed_defaults()

    def _seed_defaults(self) -> None:
        flow = parse_flow(FLOW_TEMPLATE)["nodes"]
        self.rulesets = [
            {
                "id": "ruleset_standard_4v4",
                "name": "标准 4v4 辩论赛制",
                "summary": "经典四对四对抗赛制：立论、驳论、自由辩论、总结陈词。",
                "template": FLOW_TEMPLATE,
                "flow": flow,
                "other_info": {"debater_per_side": 4},
                "created_at": _now(),
                "updated_at": _now(),
            }
        ]
        self._save()

    def _load(self) -> None:
        if _under_pytest():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self.rulesets = data
        except (OSError, ValueError):
            self.rulesets = []

    def _save(self) -> None:
        if _under_pytest():
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.rulesets, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.rulesets]

    def get(self, ruleset_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return next((dict(r) for r in self.rulesets if r.get("id") == ruleset_id), None)

    def _new_id(self, name: str) -> str:
        digest = hashlib.sha1(f"{name}:{_now()}:{len(self.rulesets)}".encode("utf-8")).hexdigest()[:8]
        return f"ruleset_{digest}"

    def create(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        name = str(fields.get("name") or "").strip()
        if not name:
            raise ValueError("赛制名称不能为空。")
        template = str(fields.get("template") or "")
        flow = fields.get("flow")
        if not isinstance(flow, list):
            flow = parse_flow(template)["nodes"]
        record = {
            "id": self._new_id(name),
            "name": name,
            "summary": str(fields.get("summary") or "").strip(),
            "template": template,
            "flow": flow,
            "other_info": fields.get("other_info") if isinstance(fields.get("other_info"), dict) else {},
            "created_at": _now(),
            "updated_at": _now(),
        }
        with self._lock:
            self.rulesets.append(record)
            self._save()
        return dict(record)

    def update(self, ruleset_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            record = next((r for r in self.rulesets if r.get("id") == ruleset_id), None)
            if not record:
                raise KeyError(ruleset_id)
            for key in ("name", "summary", "template"):
                if key in fields and fields[key] is not None:
                    record[key] = str(fields[key]).strip() if key != "template" else str(fields[key])
            if isinstance(fields.get("flow"), list):
                record["flow"] = fields["flow"]
            elif "template" in fields:
                record["flow"] = parse_flow(record["template"])["nodes"]
            if isinstance(fields.get("other_info"), dict):
                record["other_info"] = fields["other_info"]
            record["updated_at"] = _now()
            self._save()
            return dict(record)

    def delete(self, ruleset_id: str) -> None:
        with self._lock:
            before = len(self.rulesets)
            self.rulesets = [r for r in self.rulesets if r.get("id") != ruleset_id]
            if len(self.rulesets) == before:
                raise KeyError(ruleset_id)
            self._save()


ruleset_store = RulesetStore()
