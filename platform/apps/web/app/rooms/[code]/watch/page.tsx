"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { DebateStage } from "@/components/debate-stage";
import { FreeTurnQueue, FreeTurnSeatQueueAdapter } from "@/components/free-turn-queue";
import { LoadError } from "@/components/load-error";
import { StageAnnouncement } from "@/components/stage-announcement";
import { StageSeatAccessibility } from "@/components/stage-seat-accessibility";
import { useRoom } from "@/lib/use-room";

const TERMINAL_ROOM_STATUSES = new Set(["completed", "review_required", "terminated", "cancelled"]);

export default function WatchPage() {
  const { code } = useParams<{ code: string }>();
  const router = useRouter();
  const { room, setRoom, connected, error, reconnect, connectionBlockedReason, liveEvent } = useRoom(code);
  const [accessNotice, setAccessNotice] = useState(false);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    setAccessNotice(params.get("notice") === "no-control");
  }, []);

  useEffect(() => {
    if (room?.status === "cancelled") {
      router.replace(`/?room_closed=${code}`);
    } else if (
      room
      && ["completed", "review_required", "terminated"].includes(room.status)
      && (room.status === "completed" || Boolean(room.my_seat) || room.can_control)
    ) {
      router.push(`/rooms/${code}/result`);
    }
  }, [room, code, router]);

  if (!room && error) return <LoadError message={error} retry={connectionBlockedReason ? undefined : reconnect} />;
  if (!room) return <div className="loading-screen">正在连接公开观战…</div>;
  if (
    room.status === "cancelled"
    || (["completed", "review_required", "terminated"].includes(room.status)
      && (room.status === "completed" || Boolean(room.my_seat) || room.can_control))
  ) {
    return <div className="loading-screen">正在返回正确的比赛页面…</div>;
  }
  if (room.status === "lobby") {
    return (
      <main className="public-watch-waiting">
        <div className="public-watch-waiting-card">
          <span className="eyebrow">公开观战链接</span>
          <p className="public-watch-room-code">房间 #{room.code}</p>
          <h1>比赛尚未开始</h1>
          <p className="public-watch-topic">{room.topic}</p>
          <div className="public-watch-waiting-status" role="status" aria-live="polite">
            <span className="status-dot" aria-hidden="true" />
            房主正在准备席位，锁定后比赛会自动进入辩论舞台。
          </div>
          <div className="card-actions">
            <button type="button" className="button button-secondary" onClick={reconnect}>刷新状态</button>
            <button type="button" className="button button-primary" onClick={() => router.push("/")}>返回赛事大厅</button>
          </div>
        </div>
      </main>
    );
  }
  const terminal = TERMINAL_ROOM_STATUSES.has(room.status);
  return (
    <>
      <DebateStage
        room={room}
        connected={connected}
        connectionError={error}
        connectionBlockedReason={connectionBlockedReason}
        onReconnect={reconnect}
        onRoomChanged={(updatedRoom) => setRoom((current) => !current || updatedRoom.seq >= current.seq ? updatedRoom : current)}
        mode="watch"
        liveEvent={liveEvent}
      />
      {!terminal && <FreeTurnSeatQueueAdapter room={room} />}
      {!terminal && <FreeTurnQueue room={room} interactive={false} />}
      <StageAnnouncement room={room} />
      <StageSeatAccessibility room={room} />
      {accessNotice && (
        <div className="stage-access-notice" role="status">
          <span><strong>已切换为只读观战</strong>只有房主或系统管理员可以进入本房间控制台。</span>
          <button
            type="button"
            onClick={() => {
              setAccessNotice(false);
              const url = new URL(window.location.href);
              url.searchParams.delete("notice");
              window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
            }}
          >
            知道了
          </button>
        </div>
      )}
    </>
  );
}
