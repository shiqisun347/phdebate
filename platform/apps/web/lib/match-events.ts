export type MatchTimelineEvent = {
  seq: number;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

const labels: Record<string, string> = {
  "room.created": "房间创建",
  "room.locked": "席位锁定并完成 AI 补位",
  "room.cancelled": "房间已取消",
  "seat.claimed": "选手入席",
  "seat.abandoned": "选手退出比赛",
  "seat.released": "选手离席",
  "seat.removed_by_owner": "房主移出选手",
  "seat.expired": "离线席位已释放",
  "seat.ready_changed": "准备状态更新",
  "seat.control_acquired": "辩手设备取得控制权",
  "seat.control_taken_over": "辩手设备控制权已接管",
  "presence.connected": "辩手上线",
  "presence.disconnected": "辩手离线",
  "presence.expired": "断线宽限到期，比赛暂停",
  "match.started": "比赛正式开始",
  "stage.started": "阶段开始",
  "stage.completed": "阶段结束",
  "stage.advanced": "阶段自动推进",
  "speech.started": "发言开始",
  "speech.completed": "发言完成",
  "speech.audio.ready": "发言音频已就绪",
  "speech.content.reused": "复用已生成发言",
  "speech.interrupted": "发言被中断",
  "speech.timed_out": "发言超时",
  "speech.late_finalized": "超时发言已补交",
  "free.side_changed": "自由辩论交换发言方",
  "free.turn_timed_out": "自由辩论单轮超时",
  "audio.cue.ready": "预设语音已就绪",
  "audio.cue.invalidated": "预设语音需要重新生成",
  "audio.rtc.started": "实时语音播放开始",
  "audio.rtc.interrupt": "实时语音播放已中断",
  "audio.stream.started": "语音流开始",
  "audio.stream.aborted": "语音流已取消",
  "audio.start": "真人发言识别开始",
  "audio.final": "真人发言文字已确认",
  "audio.abort": "真人发言已取消",
  "control.pause": "比赛暂停",
  "control.resume": "比赛恢复",
  "control.retry": "重试当前步骤",
  "control.skip": "跳过当前阶段",
  "control.terminate": "比赛终止",
  "engine.quarantined": "状态机异常暂停",
  "provider.failed": "外部服务异常",
  "provider.retrying": "外部服务正在重试",
  "judge.review_required": "比赛转人工复核",
  "judge.interrupted": "自动裁判已中断",
  "judge.reviewed": "管理员确认赛果",
  "judge.corrected": "管理员修正赛果",
  "match.completed": "比赛完成",
  "match.result": "赛果已生成",
};

const categoryFallbacks: Record<string, string> = {
  audio: "音频状态更新",
  control: "比赛控制更新",
  engine: "比赛引擎状态更新",
  free: "自由辩论状态更新",
  judge: "裁判状态更新",
  match: "比赛状态更新",
  presence: "在线状态更新",
  provider: "外部服务状态更新",
  room: "房间状态更新",
  seat: "席位状态更新",
  speech: "发言状态更新",
  stage: "比赛阶段更新",
};

function winnerLabel(value: unknown): string {
  return value === "aff" ? "正方胜" : value === "neg" ? "反方胜" : value === "draw" ? "平局" : "未判定";
}

function sideLabel(value: unknown): string {
  return value === "aff" ? "正方" : value === "neg" ? "反方" : String(value || "未知阵营");
}

function seatLabel(value: unknown): string {
  const seat = String(value || "");
  const matched = /^(aff|neg)_(\d+)$/.exec(seat);
  return matched ? `${matched[1] === "aff" ? "正方" : "反方"}${matched[2]}辩` : seat;
}

export function matchEventLabel(type: string): string {
  if (labels[type]) return labels[type];
  return categoryFallbacks[type.split(".", 1)[0]] || "系统状态更新";
}

export function matchEventDetail(event: MatchTimelineEvent): string {
  const payload = event.payload;
  if (event.type === "engine.quarantined") {
    const attempts = typeof payload.attempts === "number" ? payload.attempts : 3;
    return `连续 ${attempts} 次异常，已隔离本房间`;
  }
  if (event.type === "judge.corrected" && typeof payload.old === "object" && payload.old && typeof payload.new === "object" && payload.new) {
    const oldResult = payload.old as Record<string, unknown>;
    const newResult = payload.new as Record<string, unknown>;
    return `${winnerLabel(oldResult.winner)} → ${winnerLabel(newResult.winner)}`;
  }
  if (typeof payload.stage === "object" && payload.stage) {
    const stage = payload.stage as Record<string, unknown>;
    if (typeof stage.name === "string") return stage.name;
  }
  if (typeof payload.stage === "string") return payload.stage;
  if (typeof payload.message === "string") return payload.message;
  if (typeof payload.seat_key === "string") return seatLabel(payload.seat_key);
  if (typeof payload.side === "string") return `${sideLabel(payload.side)}获得发言权`;
  if (typeof payload.reason === "string") return payload.reason;
  if (typeof payload.winner === "string") return winnerLabel(payload.winner);
  if (typeof payload.ready === "boolean") return payload.ready ? "已准备" : "取消准备";
  if (typeof payload.ai_filled === "number") return `AI 自动补位 ${payload.ai_filled} 席`;
  return "系统自动记录";
}
