"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { DebateStage } from "@/components/debate-stage";
import { FreeTurnQueue, FreeTurnSeatQueueAdapter } from "@/components/free-turn-queue";
import { LoadError } from "@/components/load-error";
import { SeatRestorePanel } from "@/components/seat-restore-panel";
import { StageAnnouncement } from "@/components/stage-announcement";
import { StageSeatAccessibility } from "@/components/stage-seat-accessibility";
import { useRoom } from "@/lib/use-room";

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
      <FreeTurnSeatQueueAdapter room={room} />
      <FreeTurnQueue room={room} interactive={false} />
      <StageAnnouncement room={room} />
      <StageSeatAccessibility room={room} />
      <SeatRestorePanel
        room={room}
        onRoomChanged={(updatedRoom) => setRoom((current) => !current || updatedRoom.seq >= current.seq ? updatedRoom : current)}
      />
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
