"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { AlertTriangle, Bot, Check, Eye, LogOut, Pause, Play, RotateCcw, SkipForward, Square, UserRoundCheck, X } from "lucide-react";
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
  const [notice, setNotice] = useState("");
  const controlAttempts = useRef<Record<string, string>>({});
  const remaining = useCountdown(
    room?.remaining_seconds ?? null,
    room?.seq ?? 0,
    room?.status === "running" || room?.status === "judging",
  );

  useEffect(() => {
    if (room && !room.can_control) router.replace(`/rooms/${code}/watch?notice=no-control`);
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

  async function restoreSeatAsAdmin(seatKey: string, displayName: string) {
    if (!confirm(`确认直接恢复 ${displayName} 的真人控制权？恢复后，该辩手当前设备将重新获得发言资格。`)) return;
    const action = `admin-restore:${seatKey}`;
    setBusy(action);
    setError("");
    try {
      const result = await apiFetch<{ room: Room }>(`/api/admin/rooms/${code}/seats/${seatKey}/restore`, {
        method: "POST",
        body: "{}",
      });
      setRoom((current) => !current || result.room.seq >= current.seq ? result.room : current);
      setNotice(`${displayName} 已恢复为真人辩手。`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "真人席位恢复失败");
    } finally {
      setBusy("");
    }
  }

  async function transferOwnership(seatKey: string, displayName: string, seatLabel: string) {
    if (!confirm(`确认将房主控制权移交给 ${displayName}（${seatLabel}）？移交后，对方将负责异常暂停、恢复和终止等应急操作。`)) return;
    const action = `transfer-owner:${seatKey}`;
    setBusy(action);
    setError("");
    setNotice("");
    try {
      const operationKey = controlAttempts.current[action] || crypto.randomUUID();
      controlAttempts.current[action] = operationKey;
      const result = await apiFetch<{ room: Room }>(`/api/rooms/${code}/transfer-owner`, {
        method: "POST",
        headers: { "X-Idempotency-Key": operationKey },
        body: JSON.stringify({ seat_key: seatKey }),
      });
      delete controlAttempts.current[action];
      setRoom((current) => !current || result.room.seq >= current.seq ? result.room : current);
      setNotice(`房主控制权已移交给 ${displayName}。`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "房主控制权移交失败");
    } finally {
      setBusy("");
    }
  }

  async function abandonOwnerSeat(displayName: string) {
    if (!confirm(`确认让 AI 接替 ${displayName} 的辩手席位？若有其他在线真人，房主控制权会自动移交；比赛流程不会中断。`)) return;
    const action = "abandon-owner-seat";
    setBusy(action);
    setError("");
    setNotice("");
    try {
      const operationKey = controlAttempts.current[action] || crypto.randomUUID();
      controlAttempts.current[action] = operationKey;
      const result = await apiFetch<{ room: Room }>(`/api/rooms/${code}/abandon-seat`, {
        method: "POST",
        headers: { "X-Idempotency-Key": operationKey },
        body: JSON.stringify({}),
      });
      delete controlAttempts.current[action];
      setRoom((current) => !current || result.room.seq >= current.seq ? result.room : current);
      setNotice("你的真人席位已交由 AI 接替。");
    } catch (err) {
      setError(err instanceof Error ? err.message : "暂时无法让 AI 接替你的席位");
    } finally {
      setBusy("");
    }
  }

  if (!room && roomError) return <LoadError message={roomError} retry={reconnect} />;
  if (!room) return <div className="loading-screen">正在载入比赛控制台…</div>;
  if (!room.can_control) return <div className="loading-screen">你没有本房间控制权限，正在切换到只读观战…</div>;

  const substitutes = room.seats.filter((seat) => seat.occupant_type === "ai_substitute");
  const connectedHumanSuccessors = room.seats.filter(
    (seat) => seat.occupant_type === "human" && seat.connected && !seat.is_owner,
  );
  const ownerHumanSeat = room.seats.find(
    (seat) => seat.is_me && seat.is_owner && seat.occupant_type === "human",
  );
  const pendingRestoreRequests = (room.seat_restore_requests || []).filter((request) => request.status === "pending");
  const humanSpeaking = room.active_speech?.speaker_type === "human";
  const failurePaused = room.status === "paused" && Boolean(room.failure_reason);
  const isTerminal = ["completed", "review_required", "terminated"].includes(room.status);
  const canPause = ["running", "judging"].includes(room.status) && !humanSpeaking;
  const canSkip = ["running", "paused", "judging"].includes(room.status) && !humanSpeaking;
  const canTerminate = ["preparing", "running", "paused", "judging"].includes(room.status);

  return (
    <div className="page-shell">
      <div className="section-head">
        <div>
          <span className="eyebrow">Automation Console</span>
          <h1>房间 #{room.code} 控制台</h1>
          <p>正常流程由系统自动推进，仅在异常或应急情况下干预。</p>
        </div>
        <Link href={`/rooms/${code}/watch`} className="button button-secondary"><Eye size={17} />打开观战</Link>
      </div>

      {room.failure_reason && <div className="error-box" role="alert"><AlertTriangle size={16} />{room.failure_reason}</div>}
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
          <div className="timeline-list" role={room.recent_events.length ? "list" : undefined} aria-label={room.recent_events.length ? "自动流程时间线" : undefined}>
            {room.recent_events.slice().reverse().map((event) => (
              <div className="timeline-item" role="listitem" key={event.seq}>
                <span>#{event.seq}</span><i />
                <div><strong>{matchEventLabel(event.type)}</strong><small>{matchEventDetail(event)} · {new Date(event.created_at).toLocaleTimeString("zh-CN")}</small></div>
              </div>
            ))}
            {!room.recent_events.length && (
              <div className="empty empty-guidance">
                <strong>比赛事件尚未产生</strong>
                <span>开赛后，阶段推进、发言和异常恢复记录会实时显示在这里。</span>
              </div>
            )}
          </div>
        </section>

        <aside className="panel">
          <div className="panel-title"><h2>应急控制</h2></div>
          <div className="form-stack">
            {failurePaused ? (
              <button className="button button-green" aria-busy={busy === "retry"} disabled={Boolean(busy)} onClick={() => control("retry")}><RotateCcw />{busy === "retry" ? "正在重试…" : "重试异常步骤"}</button>
            ) : room.status === "paused" ? (
              <button className="button button-green" aria-busy={busy === "resume"} disabled={Boolean(busy)} onClick={() => control("resume")}><Play />{busy === "resume" ? "正在恢复…" : "恢复比赛"}</button>
            ) : (
              <button className="button button-secondary" aria-busy={busy === "pause"} disabled={Boolean(busy) || !canPause} onClick={() => control("pause")}><Pause />{busy === "pause" ? "正在暂停…" : humanSpeaking?"真人发言中":"暂停比赛"}</button>
            )}
            <button className="button button-secondary" aria-busy={busy === "skip"} disabled={Boolean(busy) || !canSkip} onClick={() => confirm(`确认跳过“${room.current_stage?.name || "当前阶段"}”？系统会立即进入下一阶段，此操作不能撤销。`) && control("skip")}><SkipForward />{busy === "skip" ? "正在跳过…" : humanSpeaking ? "真人发言中，不可跳过" : "跳过当前阶段"}</button>
            <button className="button button-danger" aria-busy={busy === "terminate"} disabled={Boolean(busy) || !canTerminate} onClick={() => confirm("确定终止本场比赛？") && control("terminate")}><Square />{busy === "terminate" ? "正在终止…" : "终止比赛"}</button>
            {error && <div className="error-box" role="alert">{error}</div>}
            {notice && <div className="notice-box" role="status">{notice}</div>}
          </div>

          <hr className="panel-divider" />
          <h3>房主控制权移交</h3>
          <p className="muted">房主需要离开时，应先把异常恢复权限交给一位当前在线的真人辩手。比赛自动流程不会因此中断。</p>
          {ownerHumanSeat && (
            <div className="warning-box">
              <span>如果你不再作为真人辩手继续本场，可让 AI 接替本人席位。系统会优先把应急控制移交给其他在线真人。</span>
              <button
                type="button"
                className="button button-small button-danger"
                aria-busy={busy === "abandon-owner-seat"}
                disabled={isTerminal || Boolean(busy) || room.active_speech?.seat_key === ownerHumanSeat.seat_key}
                onClick={() => void abandonOwnerSeat(ownerHumanSeat.display_name)}
              >
                <LogOut size={15} />
                {isTerminal ? "比赛已结束" : busy === "abandon-owner-seat" ? "正在交给 AI…" : room.active_speech?.seat_key === ownerHumanSeat.seat_key ? "本人发言结束后可退出" : "让 AI 接替我的席位"}
              </button>
            </div>
          )}
          {connectedHumanSuccessors.length ? (
            <div className="substitute-list" aria-label="可接任房主的在线真人辩手">
              {connectedHumanSuccessors.map((seat) => (
                <div className="substitute-row" key={seat.seat_key}>
                  <span><UserRoundCheck size={15} /><strong>{seat.display_name}</strong><small>{seat.label} · 当前在线</small></span>
                  <button
                    type="button"
                    className="button button-small button-secondary"
                    aria-busy={busy === `transfer-owner:${seat.seat_key}`}
                    disabled={isTerminal || Boolean(busy)}
                    onClick={() => void transferOwnership(seat.seat_key, seat.display_name, seat.label)}
                  >
                    {isTerminal ? "比赛已结束" : busy === `transfer-owner:${seat.seat_key}` ? "正在移交…" : "移交房主"}
                  </button>
                </div>
              ))}
            </div>
          ) : <p className="muted">当前没有其他在线真人辩手，暂时无法安全移交。</p>}

          <hr className="panel-divider" />
          <h3>真人席位恢复申请</h3>
          {pendingRestoreRequests.length ? (
            <div className="substitute-list">
              {pendingRestoreRequests.map((request) => (
                <div className="substitute-row" key={request.id}>
                  <span><UserRoundCheck size={15} /><strong>{request.requester.real_name}</strong><small>{request.seat_label} · {request.requester_connected ? "已连接比赛" : "尚未打开比赛页"} · {new Date(request.created_at).toLocaleTimeString("zh-CN")} 申请</small></span>
                  <span className="restore-review-actions">
                    <button
                      className="button button-small button-green"
                      disabled={Boolean(busy) || Boolean(room.active_speech) || !request.requester_connected}
                      onClick={() => void reviewRestore(request.id, true)}
                    >
                      <Check size={15} />
                      {!request.requester_connected ? "等待辩手连接" : room.active_speech ? "发言结束后审批" : "批准"}
                    </button>
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
                  {room.can_admin_restore ? (
                    <button
                      type="button"
                      className="button button-small button-green"
                      aria-busy={busy === `admin-restore:${seat.seat_key}`}
                      disabled={Boolean(busy) || !seat.connected || Boolean(room.active_speech)}
                      onClick={() => void restoreSeatAsAdmin(seat.seat_key, seat.display_name.replace(/^AI 接替·/, ""))}
                    >
                      <RotateCcw size={15} />
                      {!seat.connected ? "等待辩手连接" : room.active_speech ? "发言结束后恢复" : busy === `admin-restore:${seat.seat_key}` ? "正在恢复…" : "直接恢复真人"}
                    </button>
                  ) : <small>{pendingRestoreRequests.some((request) => request.seat_key === seat.seat_key) ? "等待审批" : "等待原辩手发起申请"}</small>}
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
