import type { AuditLog, MatchSnapshot, TranscriptSegment } from "../types/contracts";

type ChatItem =
  | { kind: "speech"; time: string; segment: TranscriptSegment; side: string }
  | { kind: "op"; time: string; audit: AuditLog };

const ACTION_LABELS: Record<string, string> = {
  start_match: "开始比赛",
  pause_match: "暂停比赛",
  resume_match: "继续比赛",
  finish_match: "结束比赛",
  reset_match: "重置比赛",
  start_phase: "进入环节",
  skip_phase: "跳过环节",
  rollback_phase: "回退环节",
  activate_speaker: "指定发言人",
  start_speaking: "开始发言",
  pause_speaking: "暂停发言",
  resume_speaking: "继续发言",
  stop_speaking: "结束发言",
  reset_current_speech: "重置发言",
  set_screen_scene: "切换大屏场景",
  agent_interrupt: "中断 AI",
  agent_retry: "重试 AI",
  manual_agent_input: "人工代输入",
  publish_votes: "公布投票",
  update_integration_config: "更新 ASR/TTS 配置"
};

function actionLabel(action: string): string {
  return ACTION_LABELS[action] ?? action;
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString("zh-CN", { hour12: false });
}

const sourceLabel: Record<TranscriptSegment["source"], string> = {
  human_asr: "人类发言",
  agent_text: "AI 发言",
  manual: "人工代输入"
};

/**
 * 需求 3：数据管理的"辩论过程"子页面 —— 像聊天框一样，按时间合并展示
 * 历史辩论记录（发言）与操作记录（审计日志）。
 */
export function DebateProcessChat({ snapshot, auditLogs }: { snapshot: MatchSnapshot; auditLogs: AuditLog[] }) {
  const sideBySpeaker = new Map(snapshot.speakers.map((s) => [s.id, s.side]));
  const phaseName = new Map(snapshot.phases.map((p) => [p.id, p.name]));

  const items: ChatItem[] = [
    ...snapshot.recent_transcript.map((segment) => ({
      kind: "speech" as const,
      time: segment.created_at,
      segment,
      side: sideBySpeaker.get(segment.speaker_id) ?? ""
    })),
    ...auditLogs.map((audit) => ({ kind: "op" as const, time: audit.created_at, audit }))
  ].sort((a, b) => a.time.localeCompare(b.time));

  if (!items.length) {
    return <p className="muted-line">暂无辩论记录或操作记录。比赛进行后会按时间在此累积。</p>;
  }

  return (
    <div className="debate-chat">
      {items.map((item, index) => {
        if (item.kind === "op") {
          const failed = item.audit.result && item.audit.result !== "ok" && item.audit.result !== "success";
          return (
            <div key={`op-${item.audit.id}-${index}`} className={`debate-chat-op ${failed ? "failed" : ""}`}>
              <span>{actionLabel(item.audit.action)}</span>
              <em>
                {item.audit.actor_type}
                {item.audit.target_id ? ` · ${item.audit.target_id}` : ""} · {formatTime(item.time)}
                {failed ? ` · ${item.audit.error_message ?? item.audit.result}` : ""}
              </em>
            </div>
          );
        }
        const seg = item.segment;
        return (
          <div
            key={`sp-${seg.id}-${index}`}
            className={`debate-chat-msg ${item.side} ${seg.valid === false ? "invalid" : ""}`}
          >
            <div className="debate-chat-meta">
              <span>{seg.speaker_label}</span>
              <em>
                {phaseName.get(seg.phase_id) ?? ""} · {sourceLabel[seg.source] ?? seg.source} · {formatTime(item.time)}
              </em>
            </div>
            <p>{seg.text || "（无文本）"}</p>
            {seg.valid === false && <span className="debate-chat-flag">已作废 · {seg.invalid_reason ?? "reset"}</span>}
          </div>
        );
      })}
    </div>
  );
}
