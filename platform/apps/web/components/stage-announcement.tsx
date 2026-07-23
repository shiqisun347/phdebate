import { roomStatusLabel } from "@/lib/status-labels";
import type { Room } from "@/lib/types";

const TERMINAL_ROOM_STATUSES = new Set(["completed", "review_required", "terminated", "cancelled"]);

function currentSpeakerLabel(room: Room) {
  if (TERMINAL_ROOM_STATUSES.has(room.status)) return "比赛流程已经停止";
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
  const terminal = TERMINAL_ROOM_STATUSES.has(room.status);
  const participantDisconnectPaused = room.status === "paused"
    && room.pause_health?.reason_code === "participant_disconnected";
  const serviceFailurePaused = Boolean(room.failure_reason) && !participantDisconnectPaused;
  const status = participantDisconnectPaused
    ? "真人断线暂停"
    : serviceFailurePaused
      ? "服务异常暂停"
      : roomStatusLabel[room.status] || room.status;
  const stage = participantDisconnectPaused
    ? room.current_stage?.name
      ? `${room.current_stage.name}，等待真人重新连接`
      : "等待真人重新连接"
    : serviceFailurePaused
    ? room.current_stage?.name
      ? `${room.current_stage.name}，等待恢复`
      : "比赛已安全暂停"
    : terminal
      ? roomStatusLabel[room.status] || "比赛已经结束"
    : room.status === "preparing"
      ? "正在准备比赛"
      : room.current_stage?.name || "比赛即将开始";

  return (
    <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">
      {status}。当前环节：{stage}。{currentSpeakerLabel(room)}。
    </p>
  );
}
