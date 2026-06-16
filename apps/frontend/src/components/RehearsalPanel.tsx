import { AlertCircle, CheckCircle2, ClipboardCheck, XCircle } from "lucide-react";
import type { MatchSnapshot } from "../types/contracts";

interface RehearsalPanelProps {
  snapshot: MatchSnapshot;
  socketStatus: string;
}

type RehearsalItem = {
  id: string;
  label: string;
  detail: string;
  status: "ok" | "warn" | "fail";
};

export function RehearsalPanel({ snapshot, socketStatus }: RehearsalPanelProps) {
  const items = rehearsalItems(snapshot, socketStatus);
  const ok = items.filter((item) => item.status === "ok").length;
  const fail = items.filter((item) => item.status === "fail").length;

  return (
    <div className="panel rehearsal-panel">
      <div className="panel-head">
        <span><ClipboardCheck size={16} />现场演练清单</span>
        <strong>{ok} / {items.length}</strong>
      </div>
      <div className="rehearsal-score">
        <div>
          <span>当前状态</span>
          <strong>{fail ? "需处理" : ok === items.length ? "就绪" : "待确认"}</strong>
        </div>
        <i style={{ width: `${Math.round((ok / items.length) * 100)}%` }} />
      </div>
      <div className="rehearsal-list">
        {items.map((item) => (
          <div className={`rehearsal-row ${item.status}`} key={item.id}>
            {item.status === "ok" ? <CheckCircle2 size={16} /> : item.status === "warn" ? <AlertCircle size={16} /> : <XCircle size={16} />}
            <div>
              <strong>{item.label}</strong>
              <span>{item.detail}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function rehearsalItems(snapshot: MatchSnapshot, socketStatus: string): RehearsalItem[] {
  const humanSpeakers = snapshot.speakers.filter((speaker) => speaker.speaker_type === "human");
  const agentSpeakers = snapshot.speakers.filter((speaker) => speaker.speaker_type === "agent");
  const readyAgents = snapshot.agent_status.filter((agent) => agent.status !== "failed").length;
  const micErrors = snapshot.speech_service.consoles.mic_errors ?? [];
  const hasTranscript = snapshot.recent_transcript.some((segment) => segment.valid !== false);
  const hasAudio = snapshot.audio_assets.some((asset) => asset.chunk_count > 0);
  const hasJudgeVotes = snapshot.vote_state.judge_summary.best_speaker_id && snapshot.vote_state.winner_side;
  const publicVoteReady = snapshot.vote_state.window_status === "open" || snapshot.vote_state.audience_count > 0;

  return [
    {
      id: "ws",
      label: "实时连接",
      detail: `管理端实时通道${socketStatusLabel(socketStatus)}`,
      status: socketStatus === "open" ? "ok" : "fail"
    },
    {
      id: "persistence",
      label: "持久化",
      detail: snapshot.system?.persistence?.driver === "sqlite" ? "SQLite 快照和事件日志已启用" : "未检测到 SQLite 持久化",
      status: snapshot.system?.persistence?.driver === "sqlite" ? "ok" : "fail"
    },
    {
      id: "format",
      label: "赛制配置",
      detail: `${snapshot.phases.length} 个环节，${snapshot.speakers.length} 位辩手`,
      status: snapshot.phases.length === 10 && snapshot.speakers.length === 8 ? "ok" : "warn"
    },
    {
      id: "humans",
      label: "人类辩手端",
      detail: `${snapshot.speech_service.consoles.online} / ${snapshot.speech_service.consoles.total} 在线，${humanSpeakers.length} 位人类辩手`,
      status: micErrors.length ? "fail" : snapshot.speech_service.consoles.online >= humanSpeakers.length ? "ok" : "warn"
    },
    {
      id: "agents",
      label: "AI 辩手",
      detail: `${readyAgents} / ${agentSpeakers.length} 可用`,
      status: readyAgents === agentSpeakers.length ? "ok" : readyAgents > 0 ? "warn" : "fail"
    },
    {
      id: "speech",
      label: "语音链路",
      detail: `ASR ${serviceStatusLabel(snapshot.speech_service.asr.status)}，TTS ${serviceStatusLabel(snapshot.speech_service.tts.status)}`,
      status: snapshot.speech_service.asr.status === "failed" || snapshot.speech_service.tts.status === "failed" ? "fail" : "ok"
    },
    {
      id: "transcript",
      label: "转写/发言记录",
      detail: hasTranscript ? "已有有效转写文本，可导出复盘" : "尚未产生有效发言记录",
      status: hasTranscript ? "ok" : "warn"
    },
    {
      id: "audio",
      label: "音频归档",
      detail: hasAudio ? `${snapshot.audio_assets.length} 条音频归档记录` : "尚未检测到音频分片",
      status: hasAudio ? "ok" : "warn"
    },
    {
      id: "votes",
      label: "投票流程",
      detail: `${publicVoteReady ? "学生入口可用" : "学生投票未开启"}；${hasJudgeVotes ? "评委结果已录入" : "评委结果未录入"}`,
      status: publicVoteReady && hasJudgeVotes ? "ok" : "warn"
    },
    {
      id: "screen",
      label: "大屏场景",
      detail: `${screenSceneLabel(snapshot.match.screen_scene)} / ${liveModeLabel(snapshot.match.live_mode)}`,
      status: snapshot.match.screen_scene ? "ok" : "warn"
    }
  ];
}

function socketStatusLabel(status: string): string {
  if (status === "open") return "已连接";
  if (status === "connecting") return "连接中";
  if (status === "reconnecting") return "重连中";
  if (status === "closed") return "已断开";
  return `状态 ${status}`;
}

function serviceStatusLabel(status?: string | null): string {
  if (!status) return "未开始";
  if (status === "ok") return "正常";
  if (status === "idle") return "未开始";
  if (status === "ready") return "就绪";
  if (status === "running") return "运行中";
  if (status === "streaming") return "流式处理中";
  if (status === "failed") return "异常";
  return `状态 ${status}`;
}

function screenSceneLabel(scene?: string | null): string {
  if (scene === "idle") return "候场";
  if (scene === "live") return "实况";
  if (scene === "paused") return "暂停";
  if (scene === "judge_commentary") return "评委点评";
  if (scene === "judge_result") return "评委结果";
  if (scene === "audience_result") return "学生结果";
  return scene ? `场景 ${scene}` : "未指定场景";
}

function liveModeLabel(mode?: string | null): string {
  if (mode === "single") return "单人发言";
  if (mode === "free") return "自由辩论";
  if (mode === "prep") return "AI 准备";
  return mode ? `模式 ${mode}` : "未指定模式";
}
