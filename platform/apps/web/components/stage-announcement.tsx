import { roomStatusLabel } from "@/lib/status-labels";
import type { Room } from "@/lib/types";

function currentSpeakerLabel(room: Room) {
  const seatKey = room.active_speech?.seat_key || room.current_stage?.seat;
  if (seatKey) {
    const seat = room.seats.find((candidate) => candidate.seat_key === seatKey);
    if (seat) return `当前发言：${seat.display_name}`;
  }

  if (room.current_stage?.kind === "free" && room.current_stage.side) {
    return `自由辩论当前轮到${room.current_stage.side === "aff" ? "正方" : "反方"}`;
  }

  return "等待下一位发言者";
}

export function StageAnnouncement({ room }: { room: Room }) {
  const status = roomStatusLabel[room.status] || room.status;
  const stage = room.current_stage?.name || "等待比赛开始";

  return (
    <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">
      {status}。当前环节：{stage}。{currentSpeakerLabel(room)}。
    </p>
  );
}
