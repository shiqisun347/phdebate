export const STATUS_LABELS: Record<string, string> = {
  draft: "草稿",
  ready: "就绪",
  running: "进行中",
  paused: "已暂停",
  intervention: "人工干预",
  finished: "已结束",
  archived: "已归档",
};

export const SCENE_LABELS: Record<string, string> = {
  idle: "候场",
  opening: "辩题介绍",
  teams: "阵容介绍",
  live: "比赛实况",
  debate_process: "当前辩论过程",
  paused: "暂停",
  intermission: "中场",
  audience_vote: "观众投票",
  judge_commentary: "评委点评",
  judge_result: "评委结果",
  audience_result: "观众投票结果",
  xiaoqi_commentary: "小七点评",
  xiaoqi_result: "小七评判",
  acknowledgment: "致谢环节",
  result: "结果揭晓",
};

export const SIDE_LABELS: Record<string, string> = {
  affirmative: "正方",
  negative: "反方",
  neutral: "中立",
};

export function sideLabel(side: string): string {
  return SIDE_LABELS[side] ?? side;
}
