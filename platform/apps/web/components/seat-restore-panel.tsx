"use client";

import { RotateCcw, X } from "lucide-react";
import Link from "next/link";
import { useRef, useState } from "react";

import { apiFetch } from "@/lib/api";
import type { Room } from "@/lib/types";

type Props = {
  room: Room;
  onRoomChanged: (room: Room) => void;
};

export function SeatRestorePanel({ room, onRoomChanged }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const requestKey = useRef(crypto.randomUUID());
  const mySeat = room.seats?.find((seat) => seat.is_me);
  const request = room.seat_restore_requests?.find((item) => item.requester.id && item.seat_key === mySeat?.seat_key);

  if (mySeat?.occupant_type === "human" && request?.status === "approved") {
    return (
      <aside className="seat-restore-panel" aria-label="真人席位恢复">
        <div><strong>真人席位已恢复</strong><p>你已重新取得本人席位控制权，可以返回比赛继续参与。</p></div>
        <Link className="button button-small button-green" href={`/rooms/${room.code}/debate`}>返回比赛</Link>
      </aside>
    );
  }
  if (mySeat?.occupant_type !== "ai_substitute") return null;

  async function apply() {
    setBusy(true);
    setError("");
    try {
      const result = await apiFetch<{ room: Room }>(`/api/rooms/${room.code}/seat-restore-requests`, {
        method: "POST",
        headers: { "X-Idempotency-Key": requestKey.current },
        body: "{}",
      });
      onRoomChanged(result.room);
    } catch (err) {
      setError(err instanceof Error ? err.message : "恢复申请提交失败");
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    if (!request) return;
    setBusy(true);
    setError("");
    try {
      const result = await apiFetch<{ room: Room }>(`/api/rooms/${room.code}/seat-restore-requests/${request.id}/cancel`, {
        method: "POST",
        body: "{}",
      });
      requestKey.current = crypto.randomUUID();
      onRoomChanged(result.room);
    } catch (err) {
      setError(err instanceof Error ? err.message : "撤销申请失败");
    } finally {
      setBusy(false);
    }
  }

  const pending = request?.status === "pending";
  return (
    <aside className="seat-restore-panel" aria-label="真人席位恢复">
      <div>
        <strong>{pending ? "恢复申请等待审批" : "你的席位当前由 AI 接替"}</strong>
        <p>{pending ? "房主或系统管理员会在当前发言结束后审批；页面将实时更新。" : "你可以继续观战，并申请重新取得本人席位的控制权。"}</p>
        {request && !pending && request.resolution_reason && <small>{request.resolution_reason}</small>}
        {error && <small className="seat-restore-error" role="alert">{error}</small>}
      </div>
      {pending ? (
        <button type="button" className="button button-small button-secondary" disabled={busy} onClick={() => void cancel()}><X size={15} />撤销申请</button>
      ) : (
        <button type="button" className="button button-small button-green" disabled={busy || Boolean(room.active_speech)} onClick={() => void apply()}><RotateCcw size={15} />{room.active_speech ? "发言结束后可申请" : "申请恢复真人"}</button>
      )}
    </aside>
  );
}
