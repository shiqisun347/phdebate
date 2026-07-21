"use client";

import { Hand, LoaderCircle, X } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useState } from "react";

import { apiFetch } from "@/lib/api";
import type { FreeTurnQueueItem, Room } from "@/lib/types";

const SEAT_BADGE_ATTRIBUTE = "data-free-turn-seat-badge";

function seatName(room: Room, seatKey: string) {
  return room.seats?.find((seat) => seat.seat_key === seatKey)?.display_name || seatKey;
}

function requestedTime(value: string) {
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return value;
  return parsed.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function FreeTurnSeatQueueAdapter({ room }: { room: Room }) {
  const queue = room.free_turn_queue;
  const selectedSeat = room.current_stage?.selected_human_seat;

  useLayoutEffect(() => {
    if (room.current_stage?.kind !== "free") return;
    const managed: HTMLElement[] = [];
    for (const side of ["aff", "neg"] as const) {
      const seats = (room.seats || []).filter((seat) => seat.side === side).sort((a, b) => a.position - b.position);
      const nodes = document.querySelectorAll<HTMLElement>(`.stage-page .team-column.${side} .stage-seat`);
      nodes.forEach((node, index) => {
        const seat = seats[index];
        if (!seat) return;
        const request = queue?.items.find((item) => item.seat_key === seat.seat_key);
        const selected = selectedSeat === seat.seat_key;
        if (!request && !selected) return;
        const badge = document.createElement("span");
        badge.className = `free-turn-seat-badge${selected ? " selected" : ""}`;
        badge.setAttribute(SEAT_BADGE_ATTRIBUTE, seat.seat_key);
        badge.textContent = selected ? "✋ 已选中" : `✋ 第 ${request?.order} 位`;
        badge.setAttribute(
          "aria-label",
          selected
            ? `${seat.display_name}已被选中为下一位发言者`
            : `${seat.display_name}已举手，当前排第${request?.order}位`,
        );
        if (request?.requested_at) badge.title = `申请时间 ${requestedTime(request.requested_at)}`;
        node.append(badge);
        managed.push(badge);
      });
    }
    return () => managed.forEach((node) => node.remove());
  }, [queue?.items, room.current_stage?.kind, room.seq, room.seats, selectedSeat]);

  return null;
}

export function FreeTurnQueue({
  room,
  interactive,
  onRoomChanged,
}: {
  room: Room;
  interactive: boolean;
  onRoomChanged?: (room: Room) => void;
}) {
  const queue = room.free_turn_queue;
  const [now, setNow] = useState(() => Date.now());
  const [busy, setBusy] = useState<"request" | "cancel" | "">("");
  const [error, setError] = useState("");
  const deadline = queue?.window_deadline_at ? Date.parse(queue.window_deadline_at) : Number.NaN;
  // The engine contract caps intermissions at three seconds. Clamp a skewed
  // browser clock instead of ever showing an impossible multi-minute window.
  const remainingMs = Number.isFinite(deadline) ? Math.min(3000, Math.max(0, deadline - now)) : null;

  useEffect(() => {
    if (!Number.isFinite(deadline)) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 100);
    return () => window.clearInterval(timer);
  }, [deadline]);

  useEffect(() => setError(""), [room.code, room.current_stage?.key]);

  const selectedSeat = room.current_stage?.selected_human_seat || "";
  const selectedName = selectedSeat ? seatName(room, selectedSeat) : "";
  const targetSide = queue?.target_side === "aff" ? "正方" : queue?.target_side === "neg" ? "反方" : "下一方";
  const orderedItems = useMemo(
    () => [...(queue?.items || [])].sort((a, b) => a.order - b.order),
    [queue?.items],
  );

  if (room.current_stage?.kind !== "free") return null;

  async function requestTurn() {
    if (!queue?.can_request || busy) return;
    setBusy("request");
    setError("");
    try {
      const result = await apiFetch<{ room: Room }>(`/api/rooms/${room.code}/free-turn-requests`, {
        method: "POST",
        headers: { "X-Idempotency-Key": crypto.randomUUID() },
        body: "{}",
      });
      onRoomChanged?.(result.room);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "举手申请失败，请重试。");
    } finally {
      setBusy("");
    }
  }

  async function cancelRequest(item: FreeTurnQueueItem) {
    if (!item.request_id || busy) return;
    setBusy("cancel");
    setError("");
    try {
      const result = await apiFetch<{ room: Room }>(`/api/rooms/${room.code}/free-turn-requests/${item.request_id}/cancel`, {
        method: "POST",
        headers: { "X-Idempotency-Key": crypto.randomUUID() },
        body: "{}",
      });
      onRoomChanged?.(result.room);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "取消举手失败，请重试。");
    } finally {
      setBusy("");
    }
  }

  const myRequest = queue?.my_request;
  const actionReason = !queue
    ? "举手队列状态正在同步"
    : myRequest && !myRequest.request_id
      ? "正在同步你的申请凭证"
      : queue.request_reason;
  const actionEnabled = Boolean(
    interactive
    && !busy
    && queue
    && (myRequest?.request_id || queue.can_request),
  );

  return (
    <aside className="free-turn-queue" aria-labelledby="free-turn-queue-title">
      <header>
        <span className="free-turn-queue-icon" aria-hidden="true"><Hand size={17} /></span>
        <div><h2 id="free-turn-queue-title">自由辩论举手队列</h2><small>{targetSide} · 下一轮发言</small></div>
        {remainingMs !== null && (
          <span className={`free-turn-countdown${remainingMs <= 1000 ? " urgent" : ""}`} aria-label={`举手窗口剩余 ${Math.ceil(remainingMs / 1000)} 秒`}>
            {(remainingMs / 1000).toFixed(1)}s
          </span>
        )}
      </header>

      {selectedSeat && (
        <div className="free-turn-selected" role="status">
          <Hand size={15} aria-hidden="true" /><span><strong>{selectedName}</strong> 已被选中，其余席位等待下一轮。</span>
        </div>
      )}

      <ol className="free-turn-queue-list" aria-label="当前举手顺序">
        {orderedItems.map((item) => (
          <li key={`${item.seat_key}-${item.requested_at}`} className={item.is_me ? "mine" : undefined}>
            <span className="free-turn-order" aria-label={`第 ${item.order} 位`}>{item.order}</span>
            <Hand size={15} aria-hidden="true" />
            <strong>{seatName(room, item.seat_key)}</strong>
            <time dateTime={item.requested_at} title={item.requested_at}>{requestedTime(item.requested_at)}</time>
          </li>
        ))}
        {!orderedItems.length && <li className="free-turn-empty">当前暂时无人举手</li>}
      </ol>

      {interactive ? (
        <div className="free-turn-action">
          <button
            type="button"
            className={`button ${myRequest ? "button-secondary" : "button-green"}`}
            disabled={!actionEnabled}
            aria-describedby="free-turn-action-reason"
            onClick={() => myRequest ? void cancelRequest(myRequest) : void requestTurn()}
          >
            {busy ? <LoaderCircle className="free-turn-spinner" size={16} aria-hidden="true" /> : myRequest ? <X size={16} aria-hidden="true" /> : <Hand size={16} aria-hidden="true" />}
            {busy === "request" ? "正在举手…" : busy === "cancel" ? "正在取消…" : myRequest ? `取消举手（第 ${myRequest.order} 位）` : "举手申请下一轮"}
          </button>
          <small id="free-turn-action-reason">{actionEnabled ? (myRequest ? "再次点击可取消本轮申请" : "服务端将按申请时间确定顺序") : actionReason}</small>
        </div>
      ) : (
        <small className="free-turn-watch-note">观战模式仅展示队列，不能申请发言。</small>
      )}
      {error && <div className="free-turn-error" role="alert">{error}</div>}
    </aside>
  );
}
