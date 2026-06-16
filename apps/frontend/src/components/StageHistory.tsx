import type { MatchSnapshot, TranscriptSegment } from "../types/contracts";

function formatTime(iso: string): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

const sourceLabel: Record<TranscriptSegment["source"], string> = {
  human_asr: "人类发言",
  agent_text: "AI 发言",
  manual: "人工代输入"
};

/**
 * 需求 2.md：像聊天记录一样，按每个阶段展示发言内容与时间，方便主持/管理员
 * 在重置或干预前回看全局唯一的辩论过程。
 */
export function StageHistory({ snapshot }: { snapshot: MatchSnapshot }) {
  const sideBySpeaker = new Map(snapshot.speakers.map((speaker) => [speaker.id, speaker.side]));
  const orderedPhases = [...snapshot.phases].sort((a, b) => a.display_order - b.display_order);
  const byPhase = new Map<string, TranscriptSegment[]>();
  for (const segment of snapshot.recent_transcript) {
    const list = byPhase.get(segment.phase_id) ?? [];
    list.push(segment);
    byPhase.set(segment.phase_id, list);
  }
  const phasesWithContent = orderedPhases.filter((phase) => (byPhase.get(phase.id)?.length ?? 0) > 0);

  if (!phasesWithContent.length) {
    return <p className="muted-line">暂无发言记录，比赛开始后将按阶段累积。</p>;
  }

  return (
    <div className="stage-history">
      {phasesWithContent.map((phase) => {
        const segments = (byPhase.get(phase.id) ?? []).slice().sort((a, b) => a.created_at.localeCompare(b.created_at));
        return (
          <div key={phase.id} className={`stage-history-phase ${phase.id === snapshot.match.current_phase_id ? "current" : ""}`}>
            <div className="stage-history-head">
              <strong>{phase.name}</strong>
              <span>{segments.length} 段</span>
            </div>
            {segments.map((segment) => (
              <div
                key={segment.id}
                className={`stage-history-bubble ${sideBySpeaker.get(segment.speaker_id) ?? ""} ${segment.valid === false ? "invalid" : ""}`}
                title={segment.valid === false ? `已作废：${segment.invalid_reason ?? "reset"}` : undefined}
              >
                <div className="stage-history-meta">
                  <span>{segment.speaker_label}</span>
                  <em>{sourceLabel[segment.source] ?? segment.source} · {formatTime(segment.created_at)}</em>
                </div>
                <p>{segment.text || "（无文本）"}</p>
                {segment.valid === false && <span className="stage-history-flag">已作废 · {segment.invalid_reason ?? "reset"}</span>}
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}
