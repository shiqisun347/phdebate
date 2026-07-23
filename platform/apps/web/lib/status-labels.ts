export const roomStatusLabel: Record<string, string> = {
  lobby: "房间大厅",
  preparing: "赛前准备",
  running: "比赛进行中",
  paused: "比赛已暂停",
  judging: "裁判评议中",
  review_required: "等待人工复核",
  completed: "比赛已完成",
  terminated: "比赛已终止",
  cancelled: "房间已取消",
};

export const resultStatusLabel: Record<string, string> = {
  pending: "等待裁判",
  running: "裁判评议中",
  interrupted: "裁判已中断",
  review_required: "等待人工复核",
  approved: "赛果已确认",
  completed: "比赛已完成",
  terminated: "比赛已终止",
};

export const providerStatusLabel: Record<string, string> = {
  unconfigured: "未配置",
  disabled: "已停用",
  configured: "已配置，待检测",
  invalid: "地址无效",
  reachable: "服务可达",
  unreachable: "服务不可达",
  degraded: "服务降级",
};

const outcomeLabel: Record<string, string> = {
  win: "获胜结算",
  draw: "平局结算",
  loss: "落败结算",
};

export function ratingReasonLabel(reason: string): string {
  if (outcomeLabel[reason]) return outcomeLabel[reason];
  const correction = /^correction:(win|draw|loss)->(win|draw|loss)$/.exec(reason);
  if (correction) return `赛果修正：${outcomeLabel[correction[1]]} → ${outcomeLabel[correction[2]]}`;
  return reason || "积分结算";
}
