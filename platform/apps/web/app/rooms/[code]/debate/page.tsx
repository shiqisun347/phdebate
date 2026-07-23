"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { DebateStage } from "@/components/debate-stage";
import { FreeTurnQueue, FreeTurnSeatQueueAdapter } from "@/components/free-turn-queue";
import { LoadError } from "@/components/load-error";
import { StageAnnouncement } from "@/components/stage-announcement";
import { StageSeatAccessibility } from "@/components/stage-seat-accessibility";
import { TextSpeechFallback } from "@/components/text-speech-fallback";
import { useRoom } from "@/lib/use-room";

export default function DebatePage() {
  const { code } = useParams<{ code: string }>();
  const router = useRouter();
  const [stagePendingFinish, setStagePendingFinish] = useState(false);
  const [textPendingFinish, setTextPendingFinish] = useState(false);
  const hasPendingFinish = stagePendingFinish || textPendingFinish;
  const { room, setRoom, connected, error, reconnect, liveEvent } = useRoom(code);

  useEffect(() => {
    if (!room) return;
    if (room.status === "lobby") {
      router.replace(`/rooms/${code}/lobby`);
      return;
    }
    if (room.status === "cancelled") {
      router.replace(`/?room_closed=${code}`);
      return;
    }
    if (!room.my_seat) {
      router.replace(`/rooms/${code}/watch`);
      return;
    }
    if (!hasPendingFinish && ["completed", "review_required", "terminated"].includes(room.status)) {
      router.push(`/rooms/${code}/result`);
    }
  }, [room, hasPendingFinish, code, router]);

  if (!room && error) return <LoadError message={error} retry={reconnect} />;
  if (!room) return <div className="loading-screen">正在同步比赛舞台…</div>;
  if (
    ["lobby", "cancelled"].includes(room.status)
    || !room.my_seat
    || (!hasPendingFinish && ["completed", "review_required", "terminated"].includes(room.status))
  ) {
    return <div className="loading-screen">正在返回正确的比赛页面…</div>;
  }
  const applyRoom = (updatedRoom: typeof room) => setRoom((current) => !current || updatedRoom.seq >= current.seq ? updatedRoom : current);
  return (
    <>
      <DebateStage
        room={room}
        connected={connected}
        connectionError={error}
        onReconnect={reconnect}
        onPendingFinishChange={setStagePendingFinish}
        onRoomChanged={applyRoom}
        onLeave={() => router.push("/me")}
        mode="debate"
        liveEvent={liveEvent}
      />
      <FreeTurnSeatQueueAdapter room={room} />
      <FreeTurnQueue room={room} interactive onRoomChanged={applyRoom} />
      <StageAnnouncement room={room} />
      <StageSeatAccessibility room={room} />
      <TextSpeechFallback
        room={room}
        connected={connected}
        onPendingChange={setTextPendingFinish}
        onRoomChanged={applyRoom}
      />
    </>
  );
}
