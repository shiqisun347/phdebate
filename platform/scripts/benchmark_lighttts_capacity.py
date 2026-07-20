#!/usr/bin/env python3
"""Measure isolated LightTTS FIFO capacity without touching the production gate.

The target endpoint must be an independently launched LightTTS instance.  The
script uses unique Redis keys, one synthetic room per job, and the production
application provider for splitting, retries, WAV validation, merging, atomic
publication and cancellation behavior.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import subprocess
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import redis.asyncio as redis
from app.core.config import settings
from app.services.lighttts_admission import (
    SNAPSHOT_SCRIPT,
    LightTTSAdmissionCancelled,
    LightTTSAdmissionQueueFull,
    LightTTSAdmissionQueueTimeout,
    RedisLightTTSAdmissionGate,
)
from app.services.providers import LightTTSProvider, ProviderCancelled, ProviderError


def percentile(values: list[float], proportion: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * proportion
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def distribution(values: list[float]) -> dict[str, float | None]:
    return {
        "p50": round(percentile(values, 0.50), 3) if values else None,
        "p95": round(percentile(values, 0.95), 3) if values else None,
        "max": round(max(values), 3) if values else None,
    }


def wav_evidence(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        sample_width = audio.getsampwidth()
        sample_rate = audio.getframerate()
        frames = audio.getnframes()
        compression = audio.getcomptype()
    duration = frames / max(1, sample_rate)
    content = path.read_bytes()
    return {
        "valid": bool(content.startswith(b"RIFF") and frames > 0 and duration >= 0.2),
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
        "duration_seconds": round(duration, 3),
        "channels": channels,
        "sample_width_bytes": sample_width,
        "sample_rate_hz": sample_rate,
        "compression": compression,
    }


async def raw_snapshot(client: redis.Redis, prefix: str) -> dict[str, Any]:
    values = await client.eval(
        SNAPSHOT_SCRIPT,
        4,
        f"{prefix}:active",
        f"{prefix}:queue",
        f"{prefix}:deadlines",
        f"{prefix}:enqueued",
    )
    return {
        "active": int(values[0]),
        "queue_depth": int(values[1]),
        "oldest_wait_seconds": round(float(values[2]) / 1000, 3),
    }


async def clear_prefix(client: redis.Redis, prefix: str) -> None:
    await client.delete(
        f"{prefix}:active",
        f"{prefix}:queue",
        f"{prefix}:deadlines",
        f"{prefix}:sequence",
        f"{prefix}:enqueued",
    )


def process_group_resources(process_group: int | None) -> dict[str, float | int | None]:
    result: dict[str, float | int | None] = {
        "process_count": None,
        "rss_mib": None,
        "cpu_percent": None,
        "gpu_process_memory_mib": None,
        "gpu_total_memory_mib": None,
        "gpu_utilization_percent": None,
    }
    if process_group is None:
        return result
    try:
        rows = subprocess.run(
            ["ps", "-eo", "pgid=,pid=,rss=,pcpu="],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.splitlines()
        pids: set[int] = set()
        rss_kib = 0
        cpu_percent = 0.0
        for row in rows:
            columns = row.split()
            if len(columns) != 4 or int(columns[0]) != process_group:
                continue
            pids.add(int(columns[1]))
            rss_kib += int(columns[2])
            cpu_percent += float(columns[3])
        result.update(
            process_count=len(pids),
            rss_mib=round(rss_kib / 1024, 1),
            cpu_percent=round(cpu_percent, 1),
        )
        compute_rows = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,used_memory",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.splitlines()
        gpu_process_memory = 0
        for row in compute_rows:
            columns = [item.strip() for item in row.split(",")]
            if len(columns) == 2 and columns[0].isdigit() and int(columns[0]) in pids:
                gpu_process_memory += int(columns[1])
        result["gpu_process_memory_mib"] = gpu_process_memory
        gpu_row = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip().splitlines()
        if gpu_row:
            columns = [item.strip() for item in gpu_row[0].split(",")]
            if len(columns) == 2:
                result["gpu_total_memory_mib"] = int(columns[0])
                result["gpu_utilization_percent"] = int(columns[1])
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return result


class CapacityRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.output_dir = args.output_dir.resolve()
        self.audio_dir = self.output_dir / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.client = redis.from_url(settings.redis_url, decode_responses=True)
        self.run_id = f"capacity-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        self.production_prefix = "jixia:{lighttts-admission-v1}"
        self.production_activity_detected = asyncio.Event()
        self.resource_samples: list[dict[str, Any]] = []
        self.queue_samples: list[dict[str, Any]] = []

    async def production_idle(self) -> bool:
        snapshot = await raw_snapshot(self.client, self.production_prefix)
        return snapshot["active"] == 0 and snapshot["queue_depth"] == 0

    async def monitor(self, scenario_name: str, prefix: str, stop: asyncio.Event) -> None:
        started = time.perf_counter()
        while not stop.is_set():
            scenario = await raw_snapshot(self.client, prefix)
            production = await raw_snapshot(self.client, self.production_prefix)
            sample = {
                "scenario": scenario_name,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                **scenario,
                "production_active": production["active"],
                "production_queue_depth": production["queue_depth"],
            }
            self.queue_samples.append(sample)
            self.resource_samples.append(
                {
                    "scenario": scenario_name,
                    "elapsed_seconds": sample["elapsed_seconds"],
                    **process_group_resources(self.args.server_pgid),
                }
            )
            if production["active"] or production["queue_depth"]:
                self.production_activity_detected.set()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.args.sample_interval)
            except asyncio.TimeoutError:
                pass

    async def scenario(
        self,
        name: str,
        jobs: int,
        *,
        cancel_mode: str | None = None,
        queue_timeout_seconds: float = 300,
        max_pending: int | None = None,
    ) -> dict[str, Any]:
        prefix = f"jixia:{{lighttts-capacity-{self.run_id}-{name}}}"
        await clear_prefix(self.client, prefix)
        gate = RedisLightTTSAdmissionGate(
            key_prefix=prefix,
            max_pending=max_pending if max_pending is not None else max(2, jobs),
            queue_timeout_seconds=queue_timeout_seconds,
            lease_seconds=330,
            poll_seconds=0.05,
        )
        provider = LightTTSProvider()
        stop_monitor = asyncio.Event()
        monitor_task = asyncio.create_task(self.monitor(name, prefix, stop_monitor))
        cancel_flags = [False] * jobs
        start_gate = asyncio.Event()
        records = [
            {
                "job_index": index,
                "room_code": f"{name}-room-{index + 1:02d}",
                "speech_id": f"speech-{index + 1:02d}",
            }
            for index in range(jobs)
        ]

        async def worker(index: int) -> None:
            record = records[index]
            await start_gate.wait()
            submitted = time.perf_counter()
            record["submitted_offset_ms"] = round((submitted - scenario_started) * 1000, 1)
            lease = None
            try:
                lease = await gate.acquire(
                    lambda: cancel_flags[index] or self.production_activity_detected.is_set()
                )
                acquired = time.perf_counter()
                record["acquired_offset_ms"] = round((acquired - scenario_started) * 1000, 1)
                record["queue_wait_seconds"] = round(acquired - submitted, 3)
                output = await provider.synthesize(
                    self.args.text,
                    room_code=record["room_code"],
                    speech_id=record["speech_id"],
                    voice=self.args.voice,
                    provider_config={
                        "endpoint": self.args.endpoint,
                        "enabled": True,
                        "settings": {"read_timeout_seconds": self.args.read_timeout, "speed": 1.0},
                    },
                    should_cancel=lambda: cancel_flags[index] or self.production_activity_detected.is_set(),
                )
                completed = time.perf_counter()
                record.update(
                    status="success",
                    output=output,
                    service_seconds=round(completed - acquired, 3),
                    total_seconds=round(completed - submitted, 3),
                )
                target = self.audio_dir / record["room_code"] / f"{record['speech_id']}.wav"
                record["audio"] = wav_evidence(target)
                record["part_files"] = len(list(target.parent.glob("*.part")))
            except LightTTSAdmissionCancelled as exc:
                record.update(status="queue_cancelled", error=str(exc), total_seconds=round(time.perf_counter() - submitted, 3))
            except LightTTSAdmissionQueueTimeout as exc:
                record.update(status="queue_timeout", error=str(exc), total_seconds=round(time.perf_counter() - submitted, 3))
            except LightTTSAdmissionQueueFull as exc:
                record.update(status="queue_full", error=str(exc), total_seconds=round(time.perf_counter() - submitted, 3))
            except ProviderCancelled as exc:
                record.update(status="active_cancelled", error=str(exc), total_seconds=round(time.perf_counter() - submitted, 3))
            except (ProviderError, Exception) as exc:
                record.update(status="error", error=f"{type(exc).__name__}: {exc}", total_seconds=round(time.perf_counter() - submitted, 3))
            finally:
                if lease is not None:
                    await lease.release()
                record["finished_offset_ms"] = round((time.perf_counter() - scenario_started) * 1000, 1)

        scenario_started = time.perf_counter()
        tasks = [asyncio.create_task(worker(index)) for index in range(jobs)]
        start_gate.set()
        if cancel_mode:
            deadline = time.perf_counter() + 15
            while time.perf_counter() < deadline:
                snapshot = await raw_snapshot(self.client, prefix)
                if cancel_mode == "queued" and snapshot["queue_depth"] >= min(2, jobs - 1):
                    cancel_flags[-1] = True
                    break
                if cancel_mode == "active" and snapshot["active"] == 1:
                    cancel_flags[0] = True
                    break
                await asyncio.sleep(0.05)
        await asyncio.gather(*tasks)
        stop_monitor.set()
        await monitor_task
        final_snapshot = await raw_snapshot(self.client, prefix)
        await clear_prefix(self.client, prefix)

        successes = [record for record in records if record.get("status") == "success"]
        acquisition_order = [
            record["job_index"]
            for record in sorted(
                (record for record in records if record.get("acquired_offset_ms") is not None),
                key=lambda record: record["acquired_offset_ms"],
            )
        ]
        overtakes = sum(
            1
            for left in range(len(acquisition_order))
            for right in range(left + 1, len(acquisition_order))
            if acquisition_order[left] > acquisition_order[right]
        )
        scenario_samples = [sample for sample in self.queue_samples if sample["scenario"] == name]
        statuses = sorted({str(record.get("status")) for record in records})
        rejected = sum(record.get("status") == "queue_full" for record in records)
        timed_out = sum(record.get("status") == "queue_timeout" for record in records)
        return {
            "name": name,
            "jobs": jobs,
            "cancel_mode": cancel_mode,
            "queue_timeout_seconds": queue_timeout_seconds,
            "max_pending": max_pending if max_pending is not None else max(2, jobs),
            "wall_seconds": round(time.perf_counter() - scenario_started, 3),
            "summary": {
                "successes": len(successes),
                "failures": jobs - len(successes),
                "error_rate": round((jobs - len(successes)) / max(1, jobs), 4),
                "accepted": jobs - rejected,
                "rejected": rejected,
                "timed_out": timed_out,
                "rejection_rate": round(rejected / max(1, jobs), 4),
                "statuses": {
                    status: sum(record.get("status") == status for record in records) for status in statuses
                },
                "queue_wait_seconds": distribution([float(record["queue_wait_seconds"]) for record in successes]),
                "service_seconds": distribution([float(record["service_seconds"]) for record in successes]),
                "total_seconds": distribution([float(record["total_seconds"]) for record in successes]),
                "max_queue_depth": max((sample["queue_depth"] for sample in scenario_samples), default=0),
                "max_oldest_wait_seconds": max((sample["oldest_wait_seconds"] for sample in scenario_samples), default=0),
                "acquisition_order": acquisition_order,
                "fifo_overtakes": overtakes,
                "fifo_fair": overtakes == 0,
                "valid_wavs": sum(bool(record.get("audio", {}).get("valid")) for record in successes),
                "part_files": sum(int(record.get("part_files", 0)) for record in records),
                "final_gate": final_snapshot,
            },
            "jobs_detail": records,
        }

    async def run(self) -> dict[str, Any]:
        if not await self.production_idle():
            raise RuntimeError("production LightTTS gate is active; isolated benchmark refused to start")
        original_media_root = settings.media_root
        original_global_gate = settings.lighttts_global_gate_enabled
        original_job_timeout = settings.lighttts_job_timeout_seconds
        settings.media_root = str(self.audio_dir)
        settings.lighttts_global_gate_enabled = False
        settings.lighttts_job_timeout_seconds = self.args.job_timeout
        started = datetime.now(timezone.utc)
        scenarios: list[dict[str, Any]] = []
        try:
            for capacity in self.args.capacities:
                scenarios.append(
                    await self.scenario(
                        f"capacity-{capacity}",
                        capacity,
                        queue_timeout_seconds=self.args.capacity_queue_timeout,
                        max_pending=self.args.capacity_max_pending or None,
                    )
                )
                if self.production_activity_detected.is_set():
                    raise RuntimeError("production LightTTS activity appeared; benchmark cancelled")
            if not self.args.capacity_canary_only and not self.args.skip_behavior_scenarios:
                scenarios.append(await self.scenario("queued-cancellation", 4, cancel_mode="queued"))
                scenarios.append(await self.scenario("active-cancellation", 3, cancel_mode="active"))
                scenarios.append(
                    await self.scenario(
                        "queue-timeout",
                        2,
                        queue_timeout_seconds=self.args.timeout_scenario_seconds,
                    )
                )
        finally:
            settings.media_root = original_media_root
            settings.lighttts_global_gate_enabled = original_global_gate
            settings.lighttts_job_timeout_seconds = original_job_timeout
            await self.client.aclose()
        resources = {
            key: distribution([float(sample[key]) for sample in self.resource_samples if sample.get(key) is not None])
            for key in (
                "process_count",
                "rss_mib",
                "cpu_percent",
                "gpu_process_memory_mib",
                "gpu_total_memory_mib",
                "gpu_utilization_percent",
            )
        }
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "started_at": started.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "endpoint": self.args.endpoint,
            "endpoint_kind": self.args.endpoint_kind,
            "evidence_label": self.args.evidence_label,
            "endpoint_isolation_required": True,
            "latency_represents_real_lighttts_model": self.args.endpoint_kind == "real_lighttts",
            "redis_prefix_isolated": True,
            "production_activity_detected": self.production_activity_detected.is_set(),
            "text": self.args.text,
            "voice": self.args.voice,
            "capacities": self.args.capacities,
            "scenarios": scenarios,
            "queue_samples": self.queue_samples,
            "resource_samples": self.resource_samples,
            "resource_summary": resources,
        }


def markdown_report(run: dict[str, Any]) -> str:
    mock_endpoint = run["endpoint_kind"] == "controlled_mock"
    boundary = (
        "可控 mock LightTTS endpoint + 真实 V2 provider/admission/Redis；"
        "这里只验证排队机制、取消、超时、隔离与原子 WAV，不代表真实模型吞吐、RTF、音质或 GPU 容量。"
        if mock_endpoint
        else "真实 LightTTS 独立进程 + V2 provider + 独立 Redis FIFO gate；未修改生产配置或生产 gate。"
    )
    lines = [
        "# LightTTS 4/10/20 房隔离容量与排队体验",
        "",
        f"- 证据分栏：{run['evidence_label']}",
        f"- 执行时间（UTC）：{run['started_at']} → {run['completed_at']}",
        f"- 独立端点：`{run['endpoint']}`",
        f"- 端点类型：{run['endpoint_kind']}",
        f"- 固定语料：{run['text']}",
        f"- 生产 gate 活动检测：{'出现，测试应判无效' if run['production_activity_detected'] else '全程为 0'}",
        f"- 证据边界：{boundary}",
        "",
        "| 场景 | jobs | max pending | 接受/拒绝/超时 | 成功 | Queue P50/P95 | E2E P50/P95 | 最大队列 | 最长等待 | FIFO | WAV |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|",
    ]
    for scenario in run["scenarios"]:
        summary = scenario["summary"]
        queue = summary["queue_wait_seconds"]
        total = summary["total_seconds"]
        lines.append(
            f"| {scenario['name']} | {scenario['jobs']} | {scenario['max_pending']} | "
            f"{summary['accepted']}/{summary['rejected']}/{summary['timed_out']} | {summary['successes']} | "
            f"{queue['p50']}/{queue['p95']}s | {total['p50']}/{total['p95']}s | "
            f"{summary['max_queue_depth']} | {summary['max_oldest_wait_seconds']}s | "
            f"{'PASS' if summary['fifo_fair'] else 'FAIL'} | {summary['valid_wavs']}/{summary['successes']} |"
        )
    lines.extend(
        [
            "",
            "## 资源采样",
            "",
            (
                "> 这是 mock 端点及宿主机采样，只用于确认测试未失控；不能解释为真实 LightTTS 模型 CPU/GPU/内存需求。"
                if mock_endpoint
                else "> 这是独立真实 LightTTS 进程及宿主机采样。"
            ),
            "",
            "```json",
            json.dumps(run["resource_summary"], ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
            "## 判定说明",
            "",
            "- 容量场景的失败率、FIFO 越序、无效 WAV、残留 `.part` 任一非零即失败。",
            "- queued cancellation 应快速移除等待任务；active cancellation 应继续占用 endpoint 处理槽直到 HTTP 返回，再丢弃产物。",
            "- queue timeout 应只失败等待任务，不能打断持槽任务或留下 Redis ghost key。",
            "- 单槽 FIFO 的高并发成功不等于课堂体验达标；尾任务 E2E 与最长等待仍是发布决策依据。",
            (
                "- 本报告不产生真实模型音质、RTF 或 GPU 容量结论。"
                if mock_endpoint
                else "- 真实模型结论仍需结合固定语料内容与听感证据。"
            ),
            "",
        ]
    )
    return "\n".join(lines)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--endpoint-kind", choices=("controlled_mock", "real_lighttts"), default="controlled_mock")
    parser.add_argument("--evidence-label", default="mechanism-capacity")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--server-pgid", type=int)
    parser.add_argument("--capacities", default="4,10,20")
    parser.add_argument("--text", default="证据充分才能形成可靠判断，公平程序同样重要。")
    parser.add_argument("--voice", default="debate_voice_1")
    parser.add_argument("--read-timeout", type=int, default=120)
    parser.add_argument("--job-timeout", type=float, default=300)
    parser.add_argument("--timeout-scenario-seconds", type=float, default=1.0)
    parser.add_argument("--sample-interval", type=float, default=0.25)
    parser.add_argument("--capacity-canary-only", action="store_true")
    parser.add_argument("--skip-behavior-scenarios", action="store_true")
    parser.add_argument("--capacity-max-pending", type=int, default=0)
    parser.add_argument("--capacity-queue-timeout", type=float, default=300)
    args = parser.parse_args()
    args.capacities = [int(item) for item in args.capacities.split(",") if item.strip()]
    if args.capacity_canary_only and (not args.capacities or max(args.capacities) > 4):
        raise SystemExit("real capacity canary is capped at four jobs")
    if not args.capacity_canary_only and args.capacities != [4, 10, 20]:
        raise SystemExit("this verifier requires capacities exactly 4,10,20")
    return args


async def main_async(args: argparse.Namespace) -> None:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    runner = CapacityRunner(args)
    run = await runner.run()
    json_path = args.output_dir / "lighttts-capacity-4-10-20.json"
    markdown_path = args.output_dir / "lighttts-capacity-4-10-20.md"
    json_path.write_text(json.dumps(run, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(markdown_report(run), encoding="utf-8")
    print(
        json.dumps(
            {
                "json": str(json_path),
                "markdown": str(markdown_path),
                "scenarios": [item["summary"] for item in run["scenarios"]],
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    if os.geteuid() == 0:
        raise SystemExit("请使用 V2 服务账号运行 LightTTS 容量基准。")
    asyncio.run(main_async(parse_arguments()))


if __name__ == "__main__":
    main()
