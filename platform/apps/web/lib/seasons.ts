import type { Competition, Season } from "@/lib/types";

function localTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString("zh-CN", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
}

export function seasonStatusLabel(season: Season | null | undefined): string {
  if (!season) return "暂未配置赛季";
  if (season.is_open) return `${season.name} · 进行中`;
  if (!season.is_active) return `${season.name} · 已停用`;
  const now = Date.now();
  const startsAt = new Date(season.starts_at).getTime();
  const endsAt = season.ends_at ? new Date(season.ends_at).getTime() : null;
  if (Number.isFinite(startsAt) && now < startsAt) return `${season.name} · ${localTime(season.starts_at)} 开始`;
  if (endsAt !== null && Number.isFinite(endsAt) && now >= endsAt) return `${season.name} · 已结束`;
  return `${season.name} · 未开放`;
}

export function roomCreationBlockedReason(competition: Competition): string | null {
  if (!competition.ranked || competition.season?.is_open) return null;
  if (!competition.season) return "该积分赛事暂未配置赛季，目前不能创建新房间。";
  if (!competition.season.is_active) return `“${competition.season.name}”已停用，目前不能创建新房间。`;
  const startsAt = new Date(competition.season.starts_at).getTime();
  if (Number.isFinite(startsAt) && Date.now() < startsAt) {
    return `“${competition.season.name}”将于 ${localTime(competition.season.starts_at)} 开始，届时可创建新房间。`;
  }
  return `“${competition.season.name}”已结束，目前不能创建新房间。`;
}
