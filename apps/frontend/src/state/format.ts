import type { Clock, ClockState, Side, Speaker } from "../types/contracts";

export function sideLabel(side: Side): string {
  if (side === "affirmative") return "正方";
  if (side === "negative") return "反方";
  return "中立";
}

export function sideClass(side: Side): string {
  if (side === "affirmative") return "aff";
  if (side === "negative") return "neg";
  return "neutral";
}

export function seatLabel(seat: number): string {
  return ["", "一辩", "二辩", "三辩", "四辩"][seat] ?? `${seat}辩`;
}

export function speakerLabel(speaker?: Speaker | null): string {
  if (!speaker) return "等待指定";
  return `${sideLabel(speaker.side)}${seatLabel(speaker.seat)} · ${speaker.name}`;
}

export function clockStateLabel(state?: ClockState | string | null): string {
  if (state === "idle") return "未开始";
  if (state === "running") return "计时中";
  if (state === "paused") return "已暂停";
  if (state === "expired") return "时间到";
  if (state === "stopped") return "已停止";
  return "未开始";
}

export function formatMs(ms: number): string {
  const safe = Math.max(0, Math.round(ms / 1000));
  const minutes = Math.floor(safe / 60);
  const seconds = safe % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

export function clockRemaining(clock?: Clock): number {
  return clockRemainingAt(clock, Date.now());
}

export function clockRemainingAt(clock: Clock | undefined, now: number): number {
  if (!clock) return 0;
  if (clock.state === "running" && clock.deadline_at) {
    return Math.max(0, new Date(clock.deadline_at).getTime() - now);
  }
  return clock.remaining_ms;
}

export function clockByName(clocks: Clock[], name: string): Clock | undefined {
  return clocks.find((clock) => clock.name === name);
}
