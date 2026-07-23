import type { Room } from "@/lib/types";

/**
 * Returns the server-authored shortest disconnect grace remaining time.
 *
 * Participants and room owners receive per-seat values; anonymous watchers
 * receive only an aggregate value. Keeping that distinction here prevents UI
 * components from accidentally depending on participant-only identity data.
 */
export function disconnectGraceRemainingSeconds(room: Room | null | undefined) {
  const grace = room?.disconnect_grace;
  if (!grace) return null;

  const perSeat = grace.pending
    ?.map((item) => item.remaining_seconds)
    .filter((value) => Number.isFinite(value));
  const projected = perSeat?.length
    ? Math.min(...perSeat)
    : grace.minimum_remaining_seconds;

  return Number.isFinite(projected) ? Math.max(0, Math.ceil(projected!)) : null;
}

export function disconnectGraceTimingLabel(remainingSeconds: number | null) {
  if (remainingSeconds === null) return "60 秒内自动暂停";
  if (remainingSeconds <= 0) return "正在自动暂停";
  return `${remainingSeconds} 秒后自动暂停`;
}
