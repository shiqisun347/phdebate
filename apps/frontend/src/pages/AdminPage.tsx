import { AlertTriangle, Bot, Check, CircleStop, Clock, Download, LayoutDashboard, Mic, Pause, Play, RotateCcw, Settings, SkipBack, SkipForward, Vote } from "lucide-react";
import { useEffect, useState } from "react";
import { createExportBundle, getAuditLogs, getPreflightReport, getSpeechDiagnostics, patch, post, probeAsr, probeTts, recognizeArchivedSpeech, withCurrentAuthQuery } from "../api/client";
import { AccessLinksPanel } from "../components/AccessLinksPanel";
import { AuthPrompt } from "../components/AuthPrompt";
import { ClockTile } from "../components/ClockTile";
import { RehearsalPanel } from "../components/RehearsalPanel";
import { StatusPill } from "../components/StatusPill";
import { clockByName, seatLabel, sideClass, sideLabel, speakerLabel } from "../state/format";
import { useMatch } from "../realtime/useMatch";
import type { AuditLog, Clock as MatchClock, ExportBundle, Phase, PreflightReport, Side, Speaker, SpeechDiagnostics, Team, VoteState } from "../types/contracts";

interface AdminPageProps {
  matchId: string;
}

type AdminTab = "monitor" | "votes" | "settings";

type MatchSettingsDraft = {
  title: string;
  topic: string;
  affirmative_position: string;
  negative_position: string;
  organizer: string;
  venue: string;
};

type TeamSettingsDraft = {
  name: string;
  position: string;
  description: string;
};

type SpeakerSettingsDraft = {
  name: string;
  model_name: string;
  model_kind: "" | "open_source" | "closed_source";
  agent_endpoint: string;
};

type PhaseSettingsDraft = {
  name: string;
  duration_seconds: number;
  side_total_seconds: number;
  turn_seconds: number;
};

type JudgeVoteDraft = {
  constructive_affirmative: number;
  constructive_negative: number;
  process_affirmative: number;
  process_negative: number;
  conclusion_affirmative: number;
  conclusion_negative: number;
  winner_side: Side;
  best_speaker_id: string;
};

export function AdminPage({ matchId }: AdminPageProps) {
  const { snapshot, socketStatus, lastEvent, loadError, refresh } = useMatch(matchId, "admin");
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<AdminTab>("monitor");
  const [settingsDraft, setSettingsDraft] = useState<MatchSettingsDraft | null>(null);
  const [settingsDirty, setSettingsDirty] = useState(false);
  const [teamDrafts, setTeamDrafts] = useState<Record<string, TeamSettingsDraft>>({});
  const [dirtyTeams, setDirtyTeams] = useState<Record<string, boolean>>({});
  const [speakerDrafts, setSpeakerDrafts] = useState<Record<string, SpeakerSettingsDraft>>({});
  const [dirtySpeakers, setDirtySpeakers] = useState<Record<string, boolean>>({});
  const [phaseDrafts, setPhaseDrafts] = useState<Record<string, PhaseSettingsDraft>>({});
  const [dirtyPhases, setDirtyPhases] = useState<Record<string, boolean>>({});
  const [judgeDraft, setJudgeDraft] = useState<JudgeVoteDraft | null>(null);
  const [judgeDirty, setJudgeDirty] = useState(false);
  const [auditLogs, setAuditLogs] = useState<AuditLog[]>([]);
  const [exportBundle, setExportBundle] = useState<ExportBundle | null>(null);
  const [speechDiagnostics, setSpeechDiagnostics] = useState<SpeechDiagnostics | null>(null);
  const [speechDiagnosticsLoading, setSpeechDiagnosticsLoading] = useState(false);
  const [preflightReport, setPreflightReport] = useState<PreflightReport | null>(null);
  const [preflightLoading, setPreflightLoading] = useState(false);
  const [asrProbeLoading, setAsrProbeLoading] = useState(false);
  const [archiveAsrLoading, setArchiveAsrLoading] = useState(false);
  const [ttsProbeLoading, setTtsProbeLoading] = useState(false);

  useEffect(() => {
    if (!snapshot || settingsDirty) return;
    setSettingsDraft({
      title: snapshot.match.title,
      topic: snapshot.match.topic,
      affirmative_position: snapshot.match.affirmative_position,
      negative_position: snapshot.match.negative_position,
      organizer: snapshot.match.organizer,
      venue: snapshot.match.venue
    });
  }, [
    settingsDirty,
    snapshot?.match.affirmative_position,
    snapshot?.match.negative_position,
    snapshot?.match.organizer,
    snapshot?.match.title,
    snapshot?.match.topic,
    snapshot?.match.venue
  ]);

  useEffect(() => {
    if (!snapshot) return;
    setTeamDrafts((current) => {
      const next = { ...current };
      for (const team of snapshot.teams) {
        if (dirtyTeams[team.id]) continue;
        next[team.id] = teamDraftFromSnapshot(team);
      }
      return next;
    });
    setSpeakerDrafts((current) => {
      const next = { ...current };
      for (const speaker of snapshot.speakers) {
        if (dirtySpeakers[speaker.id]) continue;
        next[speaker.id] = speakerDraftFromSnapshot(speaker);
      }
      return next;
    });
    setPhaseDrafts((current) => {
      const next = { ...current };
      for (const phase of snapshot.phases) {
        if (dirtyPhases[phase.id]) continue;
        next[phase.id] = phaseDraftFromSnapshot(phase);
      }
      return next;
    });
  }, [dirtyPhases, dirtySpeakers, dirtyTeams, snapshot]);

  useEffect(() => {
    if (!snapshot || judgeDirty) return;
    setJudgeDraft(judgeDraftFromSnapshot(snapshot.vote_state.judge_summary));
  }, [judgeDirty, snapshot]);

  useEffect(() => {
    if (!snapshot) return;
    let cancelled = false;
    getAuditLogs(matchId, 8)
      .then((data) => {
        if (!cancelled) setAuditLogs(data.items);
      })
      .catch(() => {
        if (!cancelled) setAuditLogs([]);
      });
    return () => {
      cancelled = true;
    };
  }, [matchId, snapshot?.last_seq]);

  async function action(path: string, body: Record<string, unknown> = {}, confirmText?: string) {
    if (confirmText && !window.confirm(confirmText)) return;
    try {
      setError(null);
      await post(path, body);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    }
  }

  async function patchAction(path: string, body: Record<string, unknown> = {}, confirmText?: string) {
    if (confirmText && !window.confirm(confirmText)) return;
    try {
      setError(null);
      await patch(path, body);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    }
  }

  async function reviseSpeech(speechId: string, currentText: string) {
    const nextText = window.prompt("修订发言文本", currentText);
    if (nextText === null || nextText.trim() === currentText.trim()) return;
    await patchAction(`/api/matches/${matchId}/speeches/${speechId}`, {
      content_final: nextText.trim(),
      reason: "admin_text_revision"
    });
  }

  async function manualAgentInput(speaker: Speaker) {
    const content = window.prompt(`人工代输入：${speakerLabel(speaker)}`, "");
    if (content === null || !content.trim()) return;
    await action(`/api/matches/${matchId}/agent/${speaker.id}/manual-input`, {
      content: content.trim(),
      reason: "admin_manual_input"
    });
  }

  function updateSettingsField(field: keyof MatchSettingsDraft, value: string) {
    setSettingsDirty(true);
    setSettingsDraft((current) => ({
      ...(current ?? {
        title: "",
        topic: "",
        affirmative_position: "",
        negative_position: "",
        organizer: "",
        venue: ""
      }),
      [field]: value
    }));
  }

  async function saveSettings() {
    if (!settingsDraft) return;
    try {
      setError(null);
      await patch(`/api/matches/${matchId}`, settingsDraft);
      setSettingsDirty(false);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    }
  }

  function updateTeamField(team: Team, field: keyof TeamSettingsDraft, value: string) {
    setDirtyTeams((current) => ({ ...current, [team.id]: true }));
    setTeamDrafts((current) => ({
      ...current,
      [team.id]: {
        ...(current[team.id] ?? teamDraftFromSnapshot(team)),
        [field]: value
      }
    }));
  }

  async function saveTeam(team: Team) {
    const draft = teamDrafts[team.id];
    if (!draft) return;
    try {
      setError(null);
      await patch(`/api/matches/${matchId}/teams/${team.id}`, draft);
      setDirtyTeams((current) => ({ ...current, [team.id]: false }));
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存队伍失败");
    }
  }

  function updateSpeakerField(speaker: Speaker, field: keyof SpeakerSettingsDraft, value: string) {
    setDirtySpeakers((current) => ({ ...current, [speaker.id]: true }));
    setSpeakerDrafts((current) => ({
      ...current,
      [speaker.id]: {
        ...(current[speaker.id] ?? speakerDraftFromSnapshot(speaker)),
        [field]: value
      }
    }));
  }

  async function saveSpeaker(speaker: Speaker) {
    const draft = speakerDrafts[speaker.id];
    if (!draft) return;
    try {
      setError(null);
      await patch(`/api/matches/${matchId}/speakers/${speaker.id}`, draft);
      setDirtySpeakers((current) => ({ ...current, [speaker.id]: false }));
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存辩手失败");
    }
  }

  function updatePhaseField(phase: Phase, field: keyof PhaseSettingsDraft, value: string | number) {
    setDirtyPhases((current) => ({ ...current, [phase.id]: true }));
    setPhaseDrafts((current) => ({
      ...current,
      [phase.id]: {
        ...(current[phase.id] ?? phaseDraftFromSnapshot(phase)),
        [field]: typeof value === "number" ? value : Number.isNaN(Number(value)) ? value : Number(value)
      }
    }));
  }

  async function savePhase(phase: Phase) {
    const draft = phaseDrafts[phase.id];
    if (!draft) return;
    const body = phase.phase_type === "free_debate"
      ? {
          name: draft.name,
          side_total_seconds: draft.side_total_seconds,
          turn_seconds: draft.turn_seconds
        }
      : {
          name: draft.name,
          duration_seconds: draft.duration_seconds
        };
    try {
      setError(null);
      await patch(`/api/matches/${matchId}/phases/${phase.id}`, body);
      setDirtyPhases((current) => ({ ...current, [phase.id]: false }));
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存赛制失败");
    }
  }

  function updateJudgeField(field: keyof JudgeVoteDraft, value: string | number) {
    setJudgeDirty(true);
    setJudgeDraft((current) => ({
      ...(current ?? judgeDraft ?? emptyJudgeDraft()),
      [field]: typeof value === "number" ? safeVoteNumber(value) : value
    }) as JudgeVoteDraft);
  }

  async function saveJudgeVotes() {
    if (!judgeDraft) return;
    try {
      setError(null);
      await post(`/api/matches/${matchId}/votes`, {
        judge_summary: judgeSummaryFromDraft(judgeDraft)
      });
      setJudgeDirty(false);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存评委票失败");
    }
  }

  async function createExport() {
    try {
      setError(null);
      const bundle = await createExportBundle(matchId);
      setExportBundle(bundle);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "导出失败");
    }
  }

  async function checkSpeechDiagnostics() {
    try {
      setError(null);
      setSpeechDiagnosticsLoading(true);
      const diagnostics = await getSpeechDiagnostics(matchId);
      setSpeechDiagnostics(diagnostics);
    } catch (err) {
      setError(err instanceof Error ? err.message : "语音配置检查失败");
    } finally {
      setSpeechDiagnosticsLoading(false);
    }
  }

  async function refreshPreflightReport() {
    try {
      setError(null);
      setPreflightLoading(true);
      const report = await getPreflightReport(matchId);
      setPreflightReport(report);
    } catch (err) {
      setError(err instanceof Error ? err.message : "赛前体检失败");
    } finally {
      setPreflightLoading(false);
    }
  }

  async function runTtsProbe() {
    try {
      setError(null);
      setTtsProbeLoading(true);
      await probeTts(matchId);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "TTS 试合成失败");
      await refresh();
    } finally {
      setTtsProbeLoading(false);
    }
  }

  async function runAsrProbe() {
    try {
      setError(null);
      setAsrProbeLoading(true);
      await probeAsr(matchId);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "ASR 自检失败");
      await refresh();
    } finally {
      setAsrProbeLoading(false);
    }
  }

  async function recognizeLatestArchive() {
    if (!latestAudioAsset) return;
    try {
      setError(null);
      setArchiveAsrLoading(true);
      await recognizeArchivedSpeech(matchId, latestAudioAsset.speech_id);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "归档音频识别失败");
      await refresh();
    } finally {
      setArchiveAsrLoading(false);
    }
  }

  if (!snapshot && loadError) return <AuthPrompt role="admin" message={loadError} />;
  if (!snapshot) return <div className="loading">正在加载管理端...</div>;

  const match = snapshot.match;
  const phase = snapshot.phases.find((item) => item.id === match.current_phase_id);
  const currentSpeaker = snapshot.speakers.find((item) => item.id === snapshot.current_speech?.speaker_id);
  const visibleClocks = match.live_mode === "free"
    ? snapshot.clocks.filter((clock) => ["affirmative_total", "turn", "negative_total"].includes(clock.name))
    : snapshot.clocks.filter((clock) => clock.name === "main");
  const judgeSummary = snapshot.vote_state.judge_summary;
  const audienceSummary = snapshot.vote_state.audience_summary;
  const liveAudioAsset = snapshot.current_speech ? snapshot.audio_assets.find((item) => item.speech_id === snapshot.current_speech?.id) : undefined;
  const latestAudioAsset = liveAudioAsset ?? snapshot.audio_assets[0];

  return (
    <main className="admin-shell">
      <header className="admin-top">
        <div>
          <h1>{match.title}</h1>
          <p>{match.topic} · {snapshot.teams.map((team) => team.name).join(" vs ")}</p>
        </div>
        <StatusPill tone={match.status === "running" ? "green" : match.status === "intervention" ? "red" : "gold"}>{match.status}</StatusPill>
        <StatusPill tone={socketStatus === "open" ? "green" : "red"}>WS {socketStatus}</StatusPill>
        <div className="admin-actions">
          <button onClick={() => action(`/api/matches/${matchId}/pause`)}><CircleStop size={16} />暂停</button>
          <button onClick={() => action(`/api/matches/${matchId}/resume`)}><Play size={16} />继续</button>
          <button className="danger" onClick={() => action(`/api/matches/${matchId}/emergency-stop`, { reason: "manual" }, "确认进入紧急停止状态？")}><AlertTriangle size={16} />紧急停止</button>
        </div>
      </header>
      {error && <div className="error-banner">{error}</div>}

      <nav className="admin-tab-bar" aria-label="管理端页面">
        <button className={activeTab === "monitor" ? "active" : ""} onClick={() => setActiveTab("monitor")}>
          <LayoutDashboard size={16} />比赛监控
        </button>
        <button className={activeTab === "votes" ? "active" : ""} onClick={() => setActiveTab("votes")}>
          <Vote size={16} />投票结果
        </button>
        <button className={activeTab === "settings" ? "active" : ""} onClick={() => setActiveTab("settings")}>
          <Settings size={16} />比赛设置
        </button>
      </nav>

      {activeTab === "monitor" && (
      <section className="admin-grid">
        <aside className="panel timeline-panel">
          <div className="panel-head">比赛流程 <span>{phase?.display_order ?? "-"} / {snapshot.phases.length}</span></div>
          <div className="phase-list">
            {snapshot.phases.map((item) => (
              <button
                key={item.id}
                className={`phase-item ${item.status}`}
                onClick={() => action(`/api/matches/${matchId}/phases/${item.id}/start`)}
              >
                <span>{item.display_order}</span>
                <strong>{item.name}</strong>
                <em>{Math.round(item.duration_seconds / 60)}m</em>
              </button>
            ))}
          </div>
          <div className="button-row">
            <button onClick={() => action(`/api/matches/${matchId}/phases/${match.current_phase_id}/rollback`, { reason: "manual" }, "确认回滚并重置当前环节？")}><SkipBack size={16} />回滚本环节</button>
            <button onClick={() => action(`/api/matches/${matchId}/phases/${match.current_phase_id}/skip`, { reason: "manual" }, "确认跳过当前环节？")}><SkipForward size={16} />跳过</button>
            <button onClick={() => action(`/api/demo/reset`, {}, "确认重置 Demo 数据？")}><RotateCcw size={16} />重置 Demo</button>
          </div>
        </aside>

        <section className="admin-main">
          <div className="panel">
            <div className="panel-head">当前环节控制</div>
            <div className="now-line">
              <h2>{phase?.name}</h2>
              <p>当前发言：<strong>{speakerLabel(currentSpeaker)}</strong></p>
              {phase?.phase_type === "free_debate" && (
                <p>当前轮次：<strong>{sideLabel(snapshot.free_debate.current_turn_side)} · 第 {snapshot.free_debate.turn_index} 轮</strong></p>
              )}
            </div>
            <div className="clock-row">
              {match.live_mode === "free" ? (
                <>
                  <ClockTile label="正方总时间" clock={clockByName(snapshot.clocks, "affirmative_total")} tone="aff" compact />
                  <ClockTile label="单次发言" clock={clockByName(snapshot.clocks, "turn")} tone="turn" compact />
                  <ClockTile label="反方总时间" clock={clockByName(snapshot.clocks, "negative_total")} tone="neg" compact />
                </>
              ) : (
                <ClockTile label="主时钟" clock={clockByName(snapshot.clocks, "main")} compact />
              )}
            </div>
            <div className="clock-control-list">
              {visibleClocks.map((clock) => (
                <ClockControl
                  key={clock.name}
                  clock={clock}
                  onAction={action}
                  matchId={matchId}
                />
              ))}
            </div>
            <div className="button-row">
              <button onClick={() => action(`/api/matches/${matchId}/screen/scene`, { scene: "idle" })}>候场</button>
              <button onClick={() => action(`/api/matches/${matchId}/screen/scene`, { scene: "teams" })}>阵容</button>
              <button onClick={() => action(`/api/matches/${matchId}/screen/scene`, { scene: "live", live_mode: match.live_mode })}>实况</button>
              <button onClick={() => action(`/api/matches/${matchId}/screen/scene`, { scene: "intermission" })}>中场</button>
              <button onClick={() => action(`/api/matches/${matchId}/screen/scene`, { scene: "result" })}>结果</button>
            </div>
            {currentSpeaker && (
              <div className="button-row">
                {currentSpeaker.speaker_type === "human" && (
                  <>
                    <button onClick={() => action(`/api/matches/${matchId}/speakers/${currentSpeaker.id}/asr/partial`, { text: "这是一条现场联调的 ASR partial，会即时同步到大屏字幕。", latency_ms: 520 })}>模拟 partial</button>
                    <button onClick={() => action(`/api/matches/${matchId}/speakers/${currentSpeaker.id}/asr/final`, { text: "这是一条现场联调的 ASR final，会写入正式转写记录。", latency_ms: 680 })}>模拟 final</button>
                    <button onClick={() => action(`/api/matches/${matchId}/speakers/${currentSpeaker.id}/asr/fail`, { reason: "manual rehearsal failure" })}>ASR 异常</button>
                  </>
                )}
                {currentSpeaker.speaker_type === "agent" && (
                  <button onClick={() => action(`/api/matches/${matchId}/speakers/${currentSpeaker.id}/tts/fail`, { reason: "manual rehearsal failure", text_only: true })}>TTS 降级</button>
                )}
              </div>
            )}
          </div>

          <div className="panel">
            <div className="panel-head">指定发言人</div>
            <div className="speaker-grid">
              {snapshot.speakers.map((speaker) => (
                <button
                  key={speaker.id}
                  className={`${sideClass(speaker.side)} ${currentSpeaker?.id === speaker.id ? "active" : ""}`}
                  onClick={() => action(`/api/matches/${matchId}/speakers/${speaker.id}/activate`)}
                >
                  <strong>{speaker.name}</strong>
                  <span>{sideLabel(speaker.side)} · {seatLabel(speaker.seat)} · {speaker.speaker_type === "agent" ? "AI" : "人类"}</span>
                </button>
              ))}
            </div>
          </div>

          <div className="panel">
            <div className="panel-head">实时转写 / 发言流</div>
            <div className="transcript-list">
              {snapshot.recent_transcript.map((segment) => (
                <div key={segment.id} className={`transcript-item ${segment.valid === false ? "invalid" : ""}`}>
                  <div>
                    <strong>{segment.speaker_label}</strong>
                    <StatusPill tone={segment.source === "agent_text" ? "blue" : "green"}>{segment.source === "agent_text" ? "AI" : "ASR"}</StatusPill>
                    <StatusPill tone={segment.is_final ? "green" : "gold"}>{segment.is_final ? "final" : "partial"}</StatusPill>
                    {segment.valid === false && <StatusPill tone="red">invalid</StatusPill>}
                    <button onClick={() => reviseSpeech(segment.speech_id, segment.text)}>修订</button>
                    {segment.valid === false ? (
                      <button onClick={() => patchAction(`/api/matches/${matchId}/speeches/${segment.speech_id}`, { valid: true, reason: "admin_restore" })}>恢复</button>
                    ) : (
                      <button onClick={() => patchAction(`/api/matches/${matchId}/speeches/${segment.speech_id}`, { valid: false, reason: "admin_invalidate" }, "确认作废这段发言？")}>作废</button>
                    )}
                  </div>
                  <p>{segment.text}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <aside className="admin-side">
          <div className="panel preflight-panel">
            <div className="panel-head">
              <span><Check size={16} />赛前体检报告</span>
              <button onClick={refreshPreflightReport} disabled={preflightLoading}>
                <Check size={14} />{preflightLoading ? "检查中" : "刷新"}
              </button>
            </div>
            {preflightReport ? (
              <>
                <div className="rehearsal-score">
                  <div>
                    <span>总体状态</span>
                    <strong>{preflightLabel(preflightReport.overall_status)}</strong>
                  </div>
                  <i style={{ width: `${Math.round((preflightReport.score.ok / Math.max(preflightReport.score.total, 1)) * 100)}%` }} />
                </div>
                <p className="event-line">{preflightReport.summary}</p>
                <div className="diagnostics-grid">
                  {preflightReport.sections.map((section) => (
                    <div key={section.id}>
                      <span>{section.label}</span>
                      <strong>{preflightLabel(section.status)}</strong>
                      <em>{section.checks.filter((item) => item.status === "ok").length} / {section.checks.length} 项就绪</em>
                    </div>
                  ))}
                </div>
                <div className="rehearsal-list compact">
                  {preflightReport.sections.flatMap((section) => section.checks.filter((item) => item.status !== "ok").map((item) => (
                    <div className={`rehearsal-row ${item.status}`} key={`${section.id}-${item.id}`}>
                      {item.status === "fail" ? <AlertTriangle size={16} /> : <Clock size={16} />}
                      <div>
                        <strong>{section.label} · {item.label}</strong>
                        <span>{item.detail}</span>
                      </div>
                    </div>
                  )))}
                </div>
                <ul>
                  {preflightReport.next_actions.map((item) => <li key={item}>{item}</li>)}
                </ul>
                <p className="event-line">最近检查：{new Date(preflightReport.checked_at).toLocaleTimeString()}</p>
              </>
            ) : (
              <p className="event-line">点击刷新生成后端权威体检报告。</p>
            )}
          </div>

          <RehearsalPanel snapshot={snapshot} socketStatus={socketStatus} />

          <div className="panel">
            <div className="panel-head">
              <span><Bot size={16} />AI 辩手状态</span>
              <button onClick={() => action(`/api/matches/${matchId}/agents/health`)}>全部检查</button>
            </div>
            {snapshot.agent_status.map((agent) => (
              <div className={`agent-row ${agent.status}`} key={agent.speaker_id}>
                <i />
                <div>
                  <strong>{agent.name}</strong>
                  <span>{agent.model} · {agent.detail}</span>
                  {agent.endpoint && <em>{agent.endpoint}</em>}
                  {agent.last_health_at && <em>最近检查：{new Date(agent.last_health_at).toLocaleTimeString()} · {agent.latency_ms ?? 0}ms</em>}
                </div>
                <button onClick={() => action(`/api/matches/${matchId}/agent/${agent.speaker_id}/health`)}>检查</button>
                <button onClick={() => action(`/api/matches/${matchId}/agent/${agent.speaker_id}/retry`)}>重试</button>
                <button onClick={() => action(`/api/matches/${matchId}/agent/${agent.speaker_id}/interrupt`, { reason: "admin" })}>中断</button>
                <button
                  onClick={() => {
                    const speaker = snapshot.speakers.find((item) => item.id === agent.speaker_id);
                    if (speaker) void manualAgentInput(speaker);
                  }}
                >
                  代输入
                </button>
              </div>
            ))}
          </div>

          <div className="panel">
            <div className="panel-head">
              <span><Mic size={16} />语音链路</span>
              <div className="panel-head-actions">
                <button onClick={checkSpeechDiagnostics} disabled={speechDiagnosticsLoading}>
                  <Check size={14} />{speechDiagnosticsLoading ? "检查中" : "配置检查"}
                </button>
                <button onClick={runAsrProbe} disabled={asrProbeLoading}>
                  <Mic size={14} />{asrProbeLoading ? "识别中" : "ASR 自检"}
                </button>
                <button onClick={runTtsProbe} disabled={ttsProbeLoading}>
                  <Mic size={14} />{ttsProbeLoading ? "合成中" : "TTS 试合成"}
                </button>
              </div>
            </div>
            <div className="metric-row">ASR <strong>{snapshot.speech_service.asr.status} · {snapshot.speech_service.asr.latency_ms}ms · {snapshot.speech_service.asr.active_sessions ?? 0} 路</strong></div>
            {snapshot.speech_service.asr.detail && <p className="event-line">{snapshot.speech_service.asr.detail}</p>}
            <div className="metric-row">TTS <strong>{snapshot.speech_service.tts.status} · 队列 {snapshot.speech_service.tts.queue_size ?? 0}</strong></div>
            {snapshot.speech_service.tts.detail && <p className="event-line">{snapshot.speech_service.tts.detail}</p>}
            <div className="metric-row">音频归档 <strong>{latestAudioAsset ? `${latestAudioAsset.status} · ${latestAudioAsset.chunk_count} 段 · ${formatBytes(latestAudioAsset.size_bytes)}` : "未开始"}</strong></div>
            {latestAudioAsset?.file_path && <p className="event-line path">{latestAudioAsset.file_path}</p>}
            {latestAudioAsset && (
              <div className="button-row">
                <button onClick={recognizeLatestArchive} disabled={archiveAsrLoading}>
                  <Mic size={14} />{archiveAsrLoading ? "识别中" : "识别归档"}
                </button>
              </div>
            )}
            <div className="metric-row">大屏 <strong>{snapshot.speech_service.screen.status}</strong></div>
            <div className="metric-row">辩手端 <strong>{snapshot.speech_service.consoles.online} / {snapshot.speech_service.consoles.total}</strong></div>
            {(snapshot.speech_service.consoles.mic_errors ?? []).map((item) => (
              <p className="event-line warning" key={item.speaker_id}>
                {item.name} 麦克风异常：{item.message}
              </p>
            ))}
            {speechDiagnostics && (
              <div className="speech-diagnostics-card">
                <div className="diagnostics-head">
                  <strong>讯飞配置诊断</strong>
                  <StatusPill tone={diagnosticsTone(speechDiagnostics.overall_status)}>{diagnosticsLabel(speechDiagnostics.overall_status)}</StatusPill>
                </div>
                <div className="diagnostics-grid">
                  <DiagnosticMetric label="ASR" component={speechDiagnostics.asr} />
                  <DiagnosticMetric label="TTS" component={speechDiagnostics.tts} />
                  <div>
                    <span>音频归档</span>
                    <strong>{speechDiagnostics.audio_archive.status}</strong>
                    <em>{speechDiagnostics.audio_archive.detail}</em>
                  </div>
                  {speechDiagnostics.realtime_asr && (
                    <div>
                      <span>实时 ASR</span>
                      <strong>{speechDiagnostics.realtime_asr.enabled ? "enabled" : "archive"}</strong>
                      <em>{speechDiagnostics.realtime_asr.detail}</em>
                    </div>
                  )}
                  {speechDiagnostics.auto_recognize && (
                    <div>
                      <span>自动补识别</span>
                      <strong>{speechDiagnostics.auto_recognize.enabled ? "enabled" : "manual"}</strong>
                      <em>{speechDiagnostics.auto_recognize.detail}</em>
                    </div>
                  )}
                  {speechDiagnostics.formal_tts && (
                    <div>
                      <span>正式 TTS</span>
                      <strong>{speechDiagnostics.formal_tts.enabled ? "enabled" : "text"}</strong>
                      <em>{speechDiagnostics.formal_tts.detail}</em>
                    </div>
                  )}
                </div>
                <p className="event-line path">{speechDiagnostics.audio_archive.root_path}</p>
                {speechDiagnostics.asr.runtime_config && (
                  <p className="event-line">
                    ASR 超时：连接 {speechDiagnostics.asr.runtime_config.open_timeout_s}s · 关闭 {speechDiagnostics.asr.runtime_config.close_timeout_s}s · final {speechDiagnostics.asr.runtime_config.final_timeout_s}s
                  </p>
                )}
                <ul>
                  {speechDiagnostics.next_steps.map((item) => <li key={item}>{item}</li>)}
                </ul>
                <p className="event-line">最近检查：{new Date(speechDiagnostics.checked_at).toLocaleTimeString()} · provider {speechDiagnostics.provider}</p>
              </div>
            )}
          </div>

          <div className="panel">
            <div className="panel-head">
              <span>事件日志</span>
              <button onClick={createExport}><Download size={14} />生成导出包</button>
            </div>
            <p className="event-line">last seq: {snapshot.last_seq}</p>
            <p className="event-line">last event: {lastEvent?.type ?? "等待事件"}</p>
            <p className="event-line">persistence: {snapshot.system?.persistence?.driver ?? "memory"}</p>
            <p className="event-line path">{snapshot.system?.persistence?.database_path ?? "not configured"}</p>
            {exportBundle && (
              <div className="export-box">
                <strong>{exportBundle.export_id}</strong>
                <span>{formatBytes(exportBundle.size_bytes)} · {exportBundle.entries.length} 个条目</span>
                <a href={withCurrentAuthQuery(exportBundle.download_url)} target="_blank" rel="noreferrer">下载 ZIP</a>
              </div>
            )}
            <div className="audit-list">
              {auditLogs.map((item) => (
                <div className={`audit-row ${item.result}`} key={item.id}>
                  <strong>{item.action}</strong>
                  <span>{item.actor_type}{item.actor_id ? ` · ${item.actor_id}` : ""}</span>
                  <em>{new Date(item.created_at).toLocaleTimeString()} · {item.result}</em>
                </div>
              ))}
              {!auditLogs.length && <p className="event-line">暂无审计记录</p>}
            </div>
          </div>
        </aside>
      </section>
      )}

      {activeTab === "votes" && judgeDraft && (
        <section className="admin-detail-grid">
          <form
            className="panel judge-vote-form"
            onSubmit={(event) => {
              event.preventDefault();
              void saveJudgeVotes();
            }}
          >
            <div className="panel-head"><Vote size={16} />评委票录入</div>
            <div className="vote-input-grid">
              <label>
                <span>立论 · 正方</span>
                <input type="number" min={0} value={judgeDraft.constructive_affirmative} onChange={(event) => updateJudgeField("constructive_affirmative", event.target.valueAsNumber)} />
              </label>
              <label>
                <span>立论 · 反方</span>
                <input type="number" min={0} value={judgeDraft.constructive_negative} onChange={(event) => updateJudgeField("constructive_negative", event.target.valueAsNumber)} />
              </label>
              <label>
                <span>过程 · 正方</span>
                <input type="number" min={0} value={judgeDraft.process_affirmative} onChange={(event) => updateJudgeField("process_affirmative", event.target.valueAsNumber)} />
              </label>
              <label>
                <span>过程 · 反方</span>
                <input type="number" min={0} value={judgeDraft.process_negative} onChange={(event) => updateJudgeField("process_negative", event.target.valueAsNumber)} />
              </label>
              <label>
                <span>结辩 · 正方</span>
                <input type="number" min={0} value={judgeDraft.conclusion_affirmative} onChange={(event) => updateJudgeField("conclusion_affirmative", event.target.valueAsNumber)} />
              </label>
              <label>
                <span>结辩 · 反方</span>
                <input type="number" min={0} value={judgeDraft.conclusion_negative} onChange={(event) => updateJudgeField("conclusion_negative", event.target.valueAsNumber)} />
              </label>
            </div>
            <label>
              <span>正式优胜方</span>
              <select value={judgeDraft.winner_side} onChange={(event) => updateJudgeField("winner_side", event.target.value as Side)}>
                <option value="affirmative">正方</option>
                <option value="negative">反方</option>
              </select>
            </label>
            <label>
              <span>最佳辩手</span>
              <select value={judgeDraft.best_speaker_id} onChange={(event) => updateJudgeField("best_speaker_id", event.target.value)}>
                {snapshot.speakers.map((speaker) => (
                  <option value={speaker.id} key={speaker.id}>{sideLabel(speaker.side)}{seatLabel(speaker.seat)} · {speaker.name}</option>
                ))}
              </select>
            </label>
            <div className="button-row">
              <button type="submit" disabled={!judgeDirty}><Check size={16} />保存评委票</button>
              <button
                type="button"
                disabled={!judgeDirty}
                onClick={() => {
                  setJudgeDirty(false);
                  setJudgeDraft(judgeDraftFromSnapshot(snapshot.vote_state.judge_summary));
                }}
              >
                放弃修改
              </button>
            </div>
            <div className="metric-row">学生投票 <strong>{snapshot.vote_state.window_status} · {snapshot.vote_state.audience_count} 票</strong></div>
            <div className="button-stack">
              <button type="button" onClick={() => action(`/api/matches/${matchId}/audience-votes/open`)}>开启投票</button>
              <button type="button" onClick={() => action(`/api/matches/${matchId}/audience-votes/close`, {}, "确认关闭学生投票？")}>关闭投票</button>
              <button type="button" onClick={() => action(`/api/matches/${matchId}/votes/publish`, { scope: "judge" })}><Check size={16} />公布评委结果</button>
              <button type="button" onClick={() => action(`/api/matches/${matchId}/votes/publish`, { scope: "audience" })}>公布学生结果</button>
              <button type="button" onClick={() => action(`/api/matches/${matchId}/screen/scene`, { scene: "result" })}>切到结果大屏</button>
            </div>
          </form>

          <div className="panel">
            <div className="panel-head">评委结果</div>
            <div className="vote-summary-mini">
              <span>分项票</span>
              <p>立论 正 {judgeSummary.constructive.affirmative} / 反 {judgeSummary.constructive.negative}</p>
              <p>过程 正 {judgeSummary.process.affirmative} / 反 {judgeSummary.process.negative}</p>
              <p>结辩 正 {judgeSummary.conclusion.affirmative} / 反 {judgeSummary.conclusion.negative}</p>
              <p>建议优胜方：{sideLabel(judgeSummary.computed_winner_side)}</p>
              <p>正式优胜方：{sideLabel(judgeSummary.winner_side)}</p>
              <p>最佳辩手：{speakerLabel(snapshot.speakers.find((speaker) => speaker.id === judgeSummary.best_speaker_id))}</p>
            </div>
            <div className="metric-row">公布状态 <strong>{snapshot.vote_state.judge_published ? "已公布" : "未公布"}</strong></div>
          </div>

          <div className="panel">
            <div className="panel-head">学生投票结果</div>
            <div className="vote-summary-mini">
              <span>优胜方倾向</span>
              <p>正方 {audienceSummary.winner.affirmative} / 反方 {audienceSummary.winner.negative}</p>
              <p>总票数：{audienceSummary.total}</p>
            </div>
            <div className="ranking-list">
              {audienceSummary.best_speaker.map((item, index) => (
                <div className="ranking-row" key={item.speaker_id}>
                  <span>{index + 1}</span>
                  <strong>{speakerLabel(snapshot.speakers.find((speaker) => speaker.id === item.speaker_id))}</strong>
                  <em>{item.count} 票</em>
                </div>
              ))}
            </div>
            <div className="metric-row">公布状态 <strong>{snapshot.vote_state.audience_published ? "已公布" : "未公布"}</strong></div>
          </div>
        </section>
      )}

      {activeTab === "settings" && settingsDraft && (
        <section className="settings-grid">
          <form
            className="panel settings-form"
            onSubmit={(event) => {
              event.preventDefault();
              void saveSettings();
            }}
          >
            <div className="panel-head"><Settings size={16} />基础信息</div>
            <label>
              <span>比赛名称</span>
              <input value={settingsDraft.title} onChange={(event) => updateSettingsField("title", event.target.value)} />
            </label>
            <label>
              <span>辩题</span>
              <textarea rows={3} value={settingsDraft.topic} onChange={(event) => updateSettingsField("topic", event.target.value)} />
            </label>
            <div className="settings-two-col">
              <label>
                <span>正方立场</span>
                <input value={settingsDraft.affirmative_position} onChange={(event) => updateSettingsField("affirmative_position", event.target.value)} />
              </label>
              <label>
                <span>反方立场</span>
                <input value={settingsDraft.negative_position} onChange={(event) => updateSettingsField("negative_position", event.target.value)} />
              </label>
            </div>
            <div className="settings-two-col">
              <label>
                <span>主办方</span>
                <input value={settingsDraft.organizer} onChange={(event) => updateSettingsField("organizer", event.target.value)} />
              </label>
              <label>
                <span>会场</span>
                <input value={settingsDraft.venue} onChange={(event) => updateSettingsField("venue", event.target.value)} />
              </label>
            </div>
            <div className="button-row">
              <button type="submit" disabled={!settingsDirty}><Check size={16} />保存设置</button>
              <button
                type="button"
                disabled={!settingsDirty}
                onClick={() => {
                  setSettingsDirty(false);
                  setSettingsDraft({
                    title: match.title,
                    topic: match.topic,
                    affirmative_position: match.affirmative_position,
                    negative_position: match.negative_position,
                    organizer: match.organizer,
                    venue: match.venue
                  });
                }}
              >
                放弃修改
              </button>
            </div>
          </form>

          <div className="panel">
            <div className="panel-head">队伍与辩手</div>
            <div className="team-settings-list">
              {snapshot.teams.map((team) => (
                <form
                  className={`team-settings-card ${sideClass(team.side)}`}
                  key={team.id}
                  onSubmit={(event) => {
                    event.preventDefault();
                    void saveTeam(team);
                  }}
                >
                  <div className="settings-card-head">
                    <strong>{sideLabel(team.side)}</strong>
                    <button type="submit" disabled={!dirtyTeams[team.id]}><Check size={14} />保存</button>
                  </div>
                  <label>
                    <span>队伍名称</span>
                    <input value={(teamDrafts[team.id] ?? teamDraftFromSnapshot(team)).name} onChange={(event) => updateTeamField(team, "name", event.target.value)} />
                  </label>
                  <label>
                    <span>队伍立场</span>
                    <input value={(teamDrafts[team.id] ?? teamDraftFromSnapshot(team)).position} onChange={(event) => updateTeamField(team, "position", event.target.value)} />
                  </label>
                  <label>
                    <span>描述</span>
                    <textarea rows={2} value={(teamDrafts[team.id] ?? teamDraftFromSnapshot(team)).description} onChange={(event) => updateTeamField(team, "description", event.target.value)} />
                  </label>
                </form>
              ))}
            </div>
            <div className="settings-speaker-list">
              {snapshot.speakers.map((speaker) => (
                <form
                  className="settings-speaker-card"
                  key={speaker.id}
                  onSubmit={(event) => {
                    event.preventDefault();
                    void saveSpeaker(speaker);
                  }}
                >
                  <div className="settings-card-head">
                    <div>
                      <strong>{sideLabel(speaker.side)} · {seatLabel(speaker.seat)}</strong>
                      <StatusPill tone={speaker.speaker_type === "agent" ? "blue" : "green"}>{speaker.speaker_type === "agent" ? "AI" : "人类"}</StatusPill>
                    </div>
                    <button type="submit" disabled={!dirtySpeakers[speaker.id]}><Check size={14} />保存</button>
                  </div>
                  <label>
                    <span>显示名称</span>
                    <input value={(speakerDrafts[speaker.id] ?? speakerDraftFromSnapshot(speaker)).name} onChange={(event) => updateSpeakerField(speaker, "name", event.target.value)} />
                  </label>
                  {speaker.speaker_type === "agent" ? (
                    <>
                      <div className="settings-two-col">
                        <label>
                          <span>模型名称</span>
                          <input value={(speakerDrafts[speaker.id] ?? speakerDraftFromSnapshot(speaker)).model_name} onChange={(event) => updateSpeakerField(speaker, "model_name", event.target.value)} />
                        </label>
                        <label>
                          <span>模型类型</span>
                          <select value={(speakerDrafts[speaker.id] ?? speakerDraftFromSnapshot(speaker)).model_kind} onChange={(event) => updateSpeakerField(speaker, "model_kind", event.target.value)}>
                            <option value="closed_source">闭源模型</option>
                            <option value="open_source">开源模型</option>
                          </select>
                        </label>
                      </div>
                      <label>
                        <span>Agent URL</span>
                        <input value={(speakerDrafts[speaker.id] ?? speakerDraftFromSnapshot(speaker)).agent_endpoint} onChange={(event) => updateSpeakerField(speaker, "agent_endpoint", event.target.value)} placeholder="http://127.0.0.1:8100" />
                      </label>
                    </>
                  ) : (
                    <p className="event-line">人类辩手只允许运行中修改显示名称；辩位和麦克风链路由现场状态决定。</p>
                  )}
                </form>
              ))}
            </div>
          </div>

          <div className="panel">
            <div className="panel-head">赛制与现场链路</div>
            <div className="metric-row">环节数量 <strong>{snapshot.phases.length} 个</strong></div>
            <div className="metric-row">当前环节 <strong>{phase?.name ?? "未指定"}</strong></div>
            <div className="metric-row">自由辩论 <strong>{clockByName(snapshot.clocks, "affirmative_total")?.total_seconds ?? 240}s / 方 · {clockByName(snapshot.clocks, "turn")?.total_seconds ?? 15}s / 次</strong></div>
            <div className="metric-row">Agent <strong>{snapshot.agent_status.filter((agent) => agent.status !== "failed").length} / {snapshot.agent_status.length} 可用</strong></div>
            <div className="metric-row">ASR/TTS <strong>{snapshot.speech_service.asr.status} / {snapshot.speech_service.tts.status}</strong></div>
            <div className="phase-settings-list">
              {snapshot.phases.map((item) => (
                <form
                  className="phase-settings-card"
                  key={item.id}
                  onSubmit={(event) => {
                    event.preventDefault();
                    void savePhase(item);
                  }}
                >
                  <div className="settings-card-head">
                    <div>
                      <span>{item.display_order}</span>
                      <strong>{item.phase_type === "free_debate" ? "自由辩论" : item.phase_type}</strong>
                    </div>
                    <button type="submit" disabled={!dirtyPhases[item.id]}><Check size={14} />保存</button>
                  </div>
                  <label>
                    <span>环节名称</span>
                    <input value={(phaseDrafts[item.id] ?? phaseDraftFromSnapshot(item)).name} onChange={(event) => updatePhaseField(item, "name", event.target.value)} />
                  </label>
                  {item.phase_type === "free_debate" ? (
                    <div className="settings-two-col">
                      <label>
                        <span>每方总时长（秒）</span>
                        <input type="number" min={30} max={1800} value={(phaseDrafts[item.id] ?? phaseDraftFromSnapshot(item)).side_total_seconds} onChange={(event) => updatePhaseField(item, "side_total_seconds", event.target.valueAsNumber)} />
                      </label>
                      <label>
                        <span>单次上限（秒）</span>
                        <input type="number" min={5} max={120} value={(phaseDrafts[item.id] ?? phaseDraftFromSnapshot(item)).turn_seconds} onChange={(event) => updatePhaseField(item, "turn_seconds", event.target.valueAsNumber)} />
                      </label>
                    </div>
                  ) : (
                    <label>
                      <span>环节时长（秒）</span>
                      <input type="number" min={30} max={3600} value={(phaseDrafts[item.id] ?? phaseDraftFromSnapshot(item)).duration_seconds} onChange={(event) => updatePhaseField(item, "duration_seconds", event.target.valueAsNumber)} />
                    </label>
                  )}
                </form>
              ))}
            </div>
          </div>

          <AccessLinksPanel
            matchId={matchId}
            title={snapshot.match.title}
            topic={snapshot.match.topic}
            speakers={snapshot.speakers}
          />
        </section>
      )}
    </main>
  );
}

function teamDraftFromSnapshot(team: Team): TeamSettingsDraft {
  return {
    name: team.name,
    position: team.position,
    description: team.description
  };
}

function speakerDraftFromSnapshot(speaker: Speaker): SpeakerSettingsDraft {
  return {
    name: speaker.name,
    model_name: speaker.model_name ?? "",
    model_kind: speaker.model_kind ?? "closed_source",
    agent_endpoint: speaker.agent_endpoint ?? ""
  };
}

function phaseDraftFromSnapshot(phase: Phase): PhaseSettingsDraft {
  const sideTotal = phase.side_total_seconds ?? Math.max(1, Math.floor(phase.duration_seconds / 2));
  return {
    name: phase.name,
    duration_seconds: phase.duration_seconds,
    side_total_seconds: sideTotal,
    turn_seconds: phase.turn_seconds ?? 15
  };
}

function judgeDraftFromSnapshot(summary: VoteState["judge_summary"] | undefined): JudgeVoteDraft {
  if (!summary) return emptyJudgeDraft();
  return {
    constructive_affirmative: summary.constructive.affirmative,
    constructive_negative: summary.constructive.negative,
    process_affirmative: summary.process.affirmative,
    process_negative: summary.process.negative,
    conclusion_affirmative: summary.conclusion.affirmative,
    conclusion_negative: summary.conclusion.negative,
    winner_side: summary.winner_side,
    best_speaker_id: summary.best_speaker_id
  };
}

function judgeSummaryFromDraft(draft: JudgeVoteDraft): VoteState["judge_summary"] {
  return {
    constructive: {
      affirmative: draft.constructive_affirmative,
      negative: draft.constructive_negative
    },
    process: {
      affirmative: draft.process_affirmative,
      negative: draft.process_negative
    },
    conclusion: {
      affirmative: draft.conclusion_affirmative,
      negative: draft.conclusion_negative
    },
    computed_winner_side: draft.winner_side,
    winner_side: draft.winner_side,
    best_speaker_id: draft.best_speaker_id
  };
}

function emptyJudgeDraft(): JudgeVoteDraft {
  return {
    constructive_affirmative: 0,
    constructive_negative: 0,
    process_affirmative: 0,
    process_negative: 0,
    conclusion_affirmative: 0,
    conclusion_negative: 0,
    winner_side: "affirmative",
    best_speaker_id: ""
  };
}

function safeVoteNumber(value: number): number {
  if (!Number.isFinite(value) || value < 0) return 0;
  return Math.floor(value);
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function diagnosticsTone(status: string): "green" | "blue" | "red" | "gold" | "muted" {
  if (status === "ready") return "green";
  if (status === "mock_fallback") return "gold";
  if (status === "failed") return "red";
  return "muted";
}

function diagnosticsLabel(status: string): string {
  if (status === "ready") return "真实服务就绪";
  if (status === "mock_fallback") return "mock 降级可用";
  if (status === "failed") return "需要处理";
  return status;
}

function preflightLabel(status: string): string {
  if (status === "ok") return "就绪";
  if (status === "warn") return "待确认";
  if (status === "fail") return "需处理";
  return status;
}

function DiagnosticMetric({
  label,
  component
}: {
  label: string;
  component: SpeechDiagnostics["asr"];
}) {
  return (
    <div>
      <span>{label}</span>
      <strong>{component.status === "ready" ? "ready" : "missing"}</strong>
      <em>
        {component.missing.length
          ? `缺少 ${component.missing.join(", ")}`
          : component.auth_ready && component.auth_preview
            ? `签名预检 ${component.auth_preview.auth_algorithm} · ${component.auth_preview.request_line}`
            : component.url || component.detail}
      </em>
    </div>
  );
}

function ClockControl({
  clock,
  matchId,
  onAction
}: {
  clock: MatchClock;
  matchId: string;
  onAction: (path: string, body?: Record<string, unknown>, confirmText?: string) => Promise<void>;
}) {
  const label = clock.name === "affirmative_total" ? "正方总时钟" : clock.name === "negative_total" ? "反方总时钟" : clock.name === "turn" ? "单次时钟" : "主时钟";
  return (
    <div className="clock-control-row">
      <span><Clock size={14} />{label}</span>
      <strong>{clock.state}</strong>
      <button onClick={() => onAction(`/api/matches/${matchId}/clocks/${clock.name}/pause`, { reason: "admin_pause" })}><Pause size={14} />暂停</button>
      <button onClick={() => onAction(`/api/matches/${matchId}/clocks/${clock.name}/resume`, { reason: "admin_resume" })}><Play size={14} />继续</button>
      <button onClick={() => onAction(`/api/matches/${matchId}/clocks/${clock.name}/adjust`, { remaining_ms: 15000, reason: "admin_adjust_15s" })}>15s</button>
      <button onClick={() => onAction(`/api/matches/${matchId}/clocks/${clock.name}/adjust`, { remaining_ms: 60000, reason: "admin_adjust_60s" })}>60s</button>
      <button onClick={() => onAction(`/api/matches/${matchId}/clocks/${clock.name}/adjust`, { remaining_ms: 180000, reason: "admin_adjust_180s" })}>180s</button>
    </div>
  );
}
