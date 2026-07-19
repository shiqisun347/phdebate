"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { AlertTriangle, Bot, Check, Eye, Pause, Play, RotateCcw, SkipForward, Square, UserRoundCheck, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { LoadError } from "@/components/load-error";
import { apiFetch } from "@/lib/api";
import { matchEventDetail, matchEventLabel } from "@/lib/match-events";
import { roomStatusLabel } from "@/lib/status-labels";
import type { Room } from "@/lib/types";
import { useCountdown, useRoom } from "@/lib/use-room";

export default function ControlPage() {
  const { code } = useParams<{ code: string }>();
  const router = useRouter();
  const { room, setRoom, connected, error: roomError, reconnect } = useRoom(code);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const controlAttempts = useRef<Record<string, string>>({});
  const remaining = useCountdown(
    room?.remaining_seconds ?? null,
    room?.seq ?? 0,
    room?.status === "running" || room?.status === "judging",
  );

  useEffect(() => {
    if (room && !room.can_control) router.replace(`/rooms/${code}/watch`);
  }, [room, code, router]);

  async function control(action: string) {
    setBusy(action);
    setError("");
    try {
      const operationKey = controlAttempts.current[action] || crypto.randomUUID();
      controlAttempts.current[action] = operationKey;
      const result = await apiFetch<{ room: Room }>(`/api/rooms/${code}/control/${action}`, {
        method: "POST",
        headers: { "X-Idempotency-Key": operationKey },
        body: JSON.stringify({ reason: "房间控制台操作" }),
      });
      setRoom((current) => !current || result.room.seq >= current.seq ? result.room : current);
      delete controlAttempts.current[action];
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    } finally {
      setBusy("");
    }
  }

  async function reviewRestore(requestId: string, approve: boolean) {
    const action = approve ? "approve" : "reject";
    setBusy(`restore:${requestId}:${action}`);
    setError("");
    try {
      const result = await apiFetch<{ room: Room }>(`/api/rooms/${code}/seat-restore-requests/${requestId}/${action}`, {
        method: "POST",
        body: JSON.stringify({ reason: approve ? "房间控制台批准恢复" : "房间控制台暂不批准" }),
      });
      setRoom((current) => !current || result.room.seq >= current.seq ? result.room : current);
    } catch (err) {
      setError(err instanceof Error ? err.message : "恢复申请处理失败");
    } finally {
      setBusy("");
    }
  }

  if (!room && roomError) return <LoadError message={roomError} retry={reconnect} />;
  if (!room) return <div className="loading-screen">正在载入比赛控制台…</div>;

  const substitutes = room.seats.filter((seat) => seat.occupant_type === "ai_substitute");
  const pendingRestoreRequests = (room.seat_restore_requests || []).filter((request) => request.status === "pending");
  const humanSpeaking = room.active_speech?.speaker_type === "human";
  const failurePaused = room.status === "paused" && Boolean(room.failure_reason);
  const canPause = ["running", "judging"].includes(room.status) && !humanSpeaking;
  const canSkip = ["running", "paused", "judging"].includes(room.status);
  const canTerminate = ["preparing", "running", "paused", "judging"].includes(room.status);

  return (
    <div className="page-shell">
      <div className="section-head">
        <div>
          <span className="eyebrow">Automation Console</span>
          <h2>房间 #{room.code} 控制台</h2>
          <p>正常流程由系统自动推进，仅在异常或应急情况下干预。</p>
        </div>
        <Link href={`/rooms/${code}/watch`} className="button button-secondary"><Eye size={17} />打开观战</Link>
      </div>

      {room.failure_reason && <div className="error-box"><AlertTriangle size={16} />{room.failure_reason}</div>}
      {roomError && !connected && <div className="warning-box" role="alert"><AlertTriangle size={16} />{roomError}<button type="button" className="text-button" onClick={reconnect}>立即重连</button></div>}
      <div className="stats-grid" style={{ marginTop: 18 }}>
        <div className="stat-card"><span className="muted">比赛状态</span><strong className="control-stat-text">{roomStatusLabel[room.status] || room.status}</strong></div>
        <div className="stat-card"><span className="muted">当前阶段</span><strong className="control-stat-text">{room.current_stage?.name || "未开始"}</strong></div>
        <div className="stat-card"><span className="muted">剩余时间</span><strong>{remaining ?? "--"}s</strong></div>
        <div className="stat-card"><span className="muted">事件序号</span><strong>#{room.seq}</strong></div>
      </div>

      <div className="dashboard-grid">
        <section className="panel">
          <div className="panel-title"><h2>自动流程时间线</h2><span className={`connection ${connected ? "ok" : ""}`}>{connected ? "实时同步" : "重连中"}</span></div>
          <div className="timeline-list">
            {room.recent_events.slice().reverse().map((event) => (
              <div className="timeline-item" key={event.seq}>
                <span>#{event.seq}</span><i />
                <div><strong>{matchEventLabel(event.type)}</strong><small>{matchEventDetail(event)} · {new Date(event.created_at).toLocaleTimeString("zh-CN")}</small></div>
              </div>
            ))}
          </div>
        </section>

        <aside className="panel">
          <div className="panel-title"><h2>应急控制</h2></div>
          <div className="form-stack">
            {failurePaused ? (
              <button className="button button-green" disabled={Boolean(busy)} onClick={() => control("retry")}><RotateCcw />重试异常步骤</button>
            ) : room.status === "paused" ? (
              <button className="button button-green" disabled={Boolean(busy)} onClick={() => control("resume")}><Play />恢复比赛</button>
            ) : (
              <button className="button button-secondary" disabled={Boolean(busy) || !canPause} onClick={() => control("pause")}><Pause />{humanSpeaking?"真人发言中":"暂停比赛"}</button>
            )}
            <button className="button button-secondary" disabled={Boolean(busy) || !canSkip} onClick={() => control("skip")}><SkipForward />跳过当前阶段</button>
            <button className="button button-danger" disabled={Boolean(busy) || !canTerminate} onClick={() => confirm("确定终止本场比赛？") && control("terminate")}><Square />终止比赛</button>
            {error && <div className="error-box" role="alert">{error}</div>}
          </div>

          <hr className="panel-divider" />
          <h3>真人席位恢复申请</h3>
          {pendingRestoreRequests.length ? (
            <div className="substitute-list">
              {pendingRestoreRequests.map((request) => (
                <div className="substitute-row" key={request.id}>
                  <span><UserRoundCheck size={15} /><strong>{request.requester.real_name}</strong><small>{request.seat_label} · {request.requester_connected ? "已连接比赛" : "尚未打开比赛页"} · {new Date(request.created_at).toLocaleTimeString("zh-CN")} 申请</small></span>
                  <span className="restore-review-actions">
                    <button className="button button-small button-green" disabled={Boolean(busy) || Boolean(room.active_speech)} onClick={() => void reviewRestore(request.id, true)}><Check size={15} />{room.active_speech ? "发言结束后审批" : "批准"}</button>
                    <button className="button button-small button-secondary" disabled={Boolean(busy)} onClick={() => void reviewRestore(request.id, false)}><X size={15} />暂不批准</button>
                  </span>
                </div>
              ))}
            </div>
          ) : <p className="muted">当前没有待审批的恢复申请。</p>}

          <hr className="panel-divider" />
          <h3>AI 接替席位</h3>
          {substitutes.length ? (
            <div className="substitute-list">
              {substitutes.map((seat) => (
                <div className="substitute-row" key={seat.seat_key}>
                  <span><Bot size={15} /><strong>{seat.display_name}</strong><small>{seat.label} · {seat.connected ? "原辩手已返回" : "原辩手离线"}</small></span>
                  <small>{pendingRestoreRequests.some((request) => request.seat_key === seat.seat_key) ? "等待审批" : "等待原辩手发起申请"}</small>
                </div>
              ))}
            </div>
          ) : <p className="muted">当前没有 AI 接替的真人席位。</p>}

          <hr className="panel-divider" />
          <h3>运行状态</h3>
          <div className="service-list">
            <p><i className={`status-dot ${connected ? "online" : ""}`} />房间 WebSocket：{connected ? "已连接" : "重连中"}</p>
            <p><i className={`status-dot ${["running", "judging"].includes(room.status) ? "online" : ""}`} />自动状态机：{roomStatusLabel[room.status] || room.status}</p>
            <p><i className="status-dot" />辩手 Agent：由系统后台按步骤调用</p>
            <p><i className="status-dot" />FunASR / MOSS 实时语音：发言时按需连接</p>
          </div>
        </aside>
      </div>
    </div>
  );
}
