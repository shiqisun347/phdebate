import { AlertTriangle, Bot, Check, Clock, Download, FileArchive, KeyRound, LifeBuoy, Mic, Plus, RadioTower, RotateCcw, Settings, ShieldCheck, SkipForward, SlidersHorizontal, UsersRound, Vote, X } from "lucide-react";
import { useEffect, useState } from "react";
import { createExportBundle, getAuditLogs, getDataSummary, getPreflightReport, getRuntimeAuthStatus, getSpeechDiagnostics, patch, post, probeAsr, probeTts, put, recognizeArchivedSpeech, remove, updateRuntimeAuthStatus, withCurrentAuthQuery } from "../api/client";
import { AccessLinksPanel } from "../components/AccessLinksPanel";
import { AuthPrompt } from "../components/AuthPrompt";
import { ClockTile } from "../components/ClockTile";
import { useActionFeedback } from "../components/Feedback";
import { RehearsalPanel } from "../components/RehearsalPanel";
import { StatusPill } from "../components/StatusPill";
import { useMatch } from "../realtime/useMatch";
import { clockByName, clockStateLabel, seatLabel, sideClass, sideLabel, speakerLabel } from "../state/format";
import type { AgentConfig, AgentRequestSummary, AuditLog, DataSummary, EventSummary, ExportBundle, MatchSnapshot, Phase, PreflightReport, RuntimeAuthStatus, Side, Speaker, SpeakerType, SpeechDiagnostics, SpeechServiceRequestSummary, Team, VoteState } from "../types/contracts";

interface AdminPageProps {
  matchId: string;
}

type AdminSection = "overview" | "matches" | "setup" | "speakers" | "agents" | "speech" | "votes" | "emergency" | "security" | "data";
type EventFilter = "all" | "control" | "speech" | "vote" | "error";
type AuditFilter = "all" | "failed" | "host" | "admin";
type ReplaySelection =
  | { kind: "agent_request"; item: AgentRequestSummary }
  | { kind: "speech_service_request"; item: SpeechServiceRequestSummary }
  | { kind: "event"; item: EventSummary }
  | { kind: "audit"; item: AuditLog };
type ExportPreviewBundle = NonNullable<DataSummary["latest_export"]>;

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
  speaker_type: SpeakerType;
  name: string;
  agent_config_id: string;
  model_name: string;
  model_kind: "" | "open_source" | "closed_source";
  agent_endpoint: string;
};

type AgentConfigDraft = {
  name: string;
  provider_type: "rest_api" | "openai_sdk";
  model_name: string;
  model_id: string;
  model_kind: "open_source" | "closed_source";
  endpoint: string;
  base_url: string;
  api_key_env: string;
  timeout_ms: number;
  enabled: boolean;
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

const sections: Array<{ id: AdminSection; label: string; icon: typeof Settings }> = [
  { id: "overview", label: "总览监控", icon: RadioTower },
  { id: "matches", label: "比赛管理", icon: FileArchive },
  { id: "setup", label: "展示与赛制", icon: Settings },
  { id: "speakers", label: "辩手管理", icon: UsersRound },
  { id: "agents", label: "Agent 管理", icon: Bot },
  { id: "speech", label: "TTS/ASR", icon: Mic },
  { id: "votes", label: "投票与结果", icon: Vote },
  { id: "emergency", label: "应急干预", icon: LifeBuoy },
  { id: "security", label: "访问安全", icon: ShieldCheck },
  { id: "data", label: "数据管理", icon: FileArchive }
];

export function AdminPage({ matchId }: AdminPageProps) {
  const { snapshot, socketStatus, lastEvent, loadError, refresh } = useMatch(matchId, "admin");
  const [activeSection, setActiveSection] = useState<AdminSection>("overview");
  const [error, setError] = useState<string | null>(null);
  const [settingsDraft, setSettingsDraft] = useState<MatchSettingsDraft | null>(null);
  const [settingsDirty, setSettingsDirty] = useState(false);
  const [teamDrafts, setTeamDrafts] = useState<Record<string, TeamSettingsDraft>>({});
  const [dirtyTeams, setDirtyTeams] = useState<Record<string, boolean>>({});
  const [speakerDrafts, setSpeakerDrafts] = useState<Record<string, SpeakerSettingsDraft>>({});
  const [dirtySpeakers, setDirtySpeakers] = useState<Record<string, boolean>>({});
  const [agentConfigDrafts, setAgentConfigDrafts] = useState<Record<string, AgentConfigDraft>>({});
  const [dirtyAgentConfigs, setDirtyAgentConfigs] = useState<Record<string, boolean>>({});
  const [newAgentDraft, setNewAgentDraft] = useState<AgentConfigDraft>(emptyAgentConfigDraft());
  const [agentCreateOpen, setAgentCreateOpen] = useState(false);
  const [speakerEditTarget, setSpeakerEditTarget] = useState<Speaker | null>(null);
  const [agentEditTarget, setAgentEditTarget] = useState<AgentConfig | "new" | null>(null);
  const [phaseDrafts, setPhaseDrafts] = useState<Record<string, PhaseSettingsDraft>>({});
  const [dirtyPhases, setDirtyPhases] = useState<Record<string, boolean>>({});
  const [judgeDraft, setJudgeDraft] = useState<JudgeVoteDraft | null>(null);
  const [judgeDirty, setJudgeDirty] = useState(false);
  const [auditLogs, setAuditLogs] = useState<AuditLog[]>([]);
  const [exportBundle, setExportBundle] = useState<ExportBundle | null>(null);
  const [dataSummary, setDataSummary] = useState<DataSummary | null>(null);
  const [dataSummaryLoading, setDataSummaryLoading] = useState(false);
  const [speechDiagnostics, setSpeechDiagnostics] = useState<SpeechDiagnostics | null>(null);
  const [speechDiagnosticsLoading, setSpeechDiagnosticsLoading] = useState(false);
  const [preflightReport, setPreflightReport] = useState<PreflightReport | null>(null);
  const [preflightLoading, setPreflightLoading] = useState(false);
  const [asrProbeLoading, setAsrProbeLoading] = useState(false);
  const [archiveAsrLoading, setArchiveAsrLoading] = useState(false);
  const [ttsProbeLoading, setTtsProbeLoading] = useState(false);
  const [authStatus, setAuthStatus] = useState<RuntimeAuthStatus | null>(null);
  const [authLoading, setAuthLoading] = useState(false);
  const [generatedTokens, setGeneratedTokens] = useState<Record<string, string> | null>(null);
  const [eventFilter, setEventFilter] = useState<EventFilter>("all");
  const [auditFilter, setAuditFilter] = useState<AuditFilter>("all");
  const [replaySelection, setReplaySelection] = useState<ReplaySelection | null>(null);
  const { busyProps, notify, runAction } = useActionFeedback();

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
        if (!dirtyTeams[team.id]) next[team.id] = teamDraftFromSnapshot(team);
      }
      return next;
    });
    setSpeakerDrafts((current) => {
      const next = { ...current };
      for (const speaker of snapshot.speakers) {
        if (!dirtySpeakers[speaker.id]) next[speaker.id] = speakerDraftFromSnapshot(speaker);
      }
      return next;
    });
    setAgentConfigDrafts((current) => {
      const next = { ...current };
      for (const config of snapshot.agent_configs ?? []) {
        if (!dirtyAgentConfigs[config.id]) next[config.id] = agentConfigDraftFromSnapshot(config);
      }
      return next;
    });
    setPhaseDrafts((current) => {
      const next = { ...current };
      for (const phase of snapshot.phases) {
        if (!dirtyPhases[phase.id]) next[phase.id] = phaseDraftFromSnapshot(phase);
      }
      return next;
    });
  }, [dirtyAgentConfigs, dirtyPhases, dirtySpeakers, dirtyTeams, snapshot]);

  useEffect(() => {
    if (!snapshot || judgeDirty) return;
    setJudgeDraft(judgeDraftFromSnapshot(snapshot.vote_state.judge_summary));
  }, [judgeDirty, snapshot]);

  useEffect(() => {
    if (!snapshot) return;
    let cancelled = false;
    getAuditLogs(matchId, activeSection === "data" ? 40 : 10)
      .then((data) => {
        if (!cancelled) setAuditLogs(data.items);
      })
      .catch(() => {
        if (!cancelled) setAuditLogs([]);
      });
    return () => {
      cancelled = true;
    };
  }, [activeSection, matchId, snapshot?.last_seq]);

  useEffect(() => {
    if (!snapshot || !["data", "matches"].includes(activeSection)) return;
    void refreshDataSummary();
  }, [activeSection, snapshot?.last_seq]);

  useEffect(() => {
    if (!snapshot || activeSection !== "security") return;
    void loadAuthStatus();
  }, [activeSection, snapshot?.last_seq]);

  async function action(path: string, body: Record<string, unknown> = {}, confirmText?: string) {
    try {
      const label = adminActionLabel(path, body);
      await runAction(actionKey(path, body), label, async () => {
        setError(null);
        await post(path, body);
        await refresh();
      }, {
        confirmText,
        danger: isDangerousAction(path),
        successText: `${label}已完成`
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    }
  }

  async function patchAction(path: string, body: Record<string, unknown> = {}, confirmText?: string) {
    try {
      const label = adminActionLabel(path, body);
      await runAction(actionKey(path, body), label, async () => {
        setError(null);
        await patch(path, body);
        await refresh();
      }, {
        confirmText,
        danger: isDangerousAction(path),
        successText: `${label}已完成`
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
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

  async function saveSettings() {
    if (!settingsDraft) return;
    try {
      await runAction("save-match-settings", "保存展示信息", async () => {
        setError(null);
        await patch(`/api/matches/${matchId}`, settingsDraft);
        setSettingsDirty(false);
        await refresh();
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存展示信息失败");
    }
  }

  async function saveTeam(team: Team) {
    const draft = teamDrafts[team.id];
    if (!draft) return;
    try {
      await runAction(`save-team-${team.id}`, `保存${sideLabel(team.side)}队伍`, async () => {
        setError(null);
        await patch(`/api/matches/${matchId}/teams/${team.id}`, draft);
        setDirtyTeams((current) => ({ ...current, [team.id]: false }));
        await refresh();
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存队伍失败");
    }
  }

  async function saveSpeaker(speaker: Speaker) {
    const draft = speakerDrafts[speaker.id];
    if (!draft) return;
    if (draft.speaker_type === "agent" && !draft.agent_config_id) {
      setError("Agent 辩手必须先在 Agent 管理中创建配置，再在辩手管理中绑定。");
      return;
    }
    const body = draft.speaker_type === "agent"
      ? { speaker_type: "agent", name: draft.name, agent_config_id: draft.agent_config_id }
      : { speaker_type: "human", name: draft.name };
    try {
      await runAction(`save-speaker-${speaker.id}`, `保存${speakerLabel(speaker)}`, async () => {
        setError(null);
        await patch(`/api/matches/${matchId}/speakers/${speaker.id}`, body);
        setDirtySpeakers((current) => ({ ...current, [speaker.id]: false }));
        await refresh();
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存辩手失败");
    }
  }

  async function createAgentConfig() {
    try {
      await runAction("create-agent-config", "新增 Agent 配置", async () => {
        setError(null);
        await post(`/api/matches/${matchId}/agents/configs`, newAgentDraft);
        setNewAgentDraft(emptyAgentConfigDraft());
        setAgentEditTarget(null);
        await refresh();
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "新增 Agent 配置失败");
    }
  }

  async function saveAgentConfig(config: AgentConfig) {
    const draft = agentConfigDrafts[config.id];
    if (!draft) return;
    try {
      await runAction(`save-agent-config-${config.id}`, `保存${config.name}`, async () => {
        setError(null);
        await patch(`/api/matches/${matchId}/agents/configs/${config.id}`, draft);
        setDirtyAgentConfigs((current) => ({ ...current, [config.id]: false }));
        await refresh();
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存 Agent 配置失败");
    }
  }

  async function deleteAgentConfig(config: AgentConfig) {
    try {
      await runAction(`delete-agent-config-${config.id}`, `删除${config.name}`, async () => {
        setError(null);
        await remove(`/api/matches/${matchId}/agents/configs/${config.id}`);
        setDirtyAgentConfigs((current) => {
          const next = { ...current };
          delete next[config.id];
          return next;
        });
        await refresh();
      }, {
        confirmText: `确认删除 Agent 配置「${config.name}」？已绑定辩手的配置会被后端拒绝删除。`,
        danger: true
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除 Agent 配置失败");
    }
  }

  async function savePhase(phase: Phase) {
    const draft = phaseDrafts[phase.id];
    if (!draft) return;
    const body = phase.phase_type === "free_debate"
      ? { name: draft.name, side_total_seconds: draft.side_total_seconds, turn_seconds: draft.turn_seconds }
      : { name: draft.name, duration_seconds: draft.duration_seconds };
    try {
      await runAction(`save-phase-${phase.id}`, `保存${phase.name}`, async () => {
        setError(null);
        await patch(`/api/matches/${matchId}/phases/${phase.id}`, body);
        setDirtyPhases((current) => ({ ...current, [phase.id]: false }));
        await refresh();
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存赛制失败");
    }
  }

  async function saveJudgeVotes() {
    if (!judgeDraft) return;
    try {
      await runAction("save-judge-votes", "保存评委票", async () => {
        setError(null);
        await post(`/api/matches/${matchId}/votes`, { judge_summary: judgeSummaryFromDraft(judgeDraft) });
        setJudgeDirty(false);
        await refresh();
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存评委票失败");
    }
  }

  async function refreshPreflightReport() {
    try {
      await runAction("refresh-preflight", "刷新赛前体检", async () => {
        setError(null);
        setPreflightLoading(true);
        setPreflightReport(await getPreflightReport(matchId));
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "赛前体检失败");
    } finally {
      setPreflightLoading(false);
    }
  }

  async function checkSpeechDiagnostics() {
    try {
      await runAction("speech-diagnostics", "语音配置检查", async () => {
        setError(null);
        setSpeechDiagnosticsLoading(true);
        setSpeechDiagnostics(await getSpeechDiagnostics(matchId));
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "语音配置检查失败");
    } finally {
      setSpeechDiagnosticsLoading(false);
    }
  }

  async function runTtsProbe() {
    try {
      await runAction("tts-probe", "TTS 试合成", async () => {
        setError(null);
        setTtsProbeLoading(true);
        await probeTts(matchId);
        await refresh();
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "TTS 试合成失败");
      await refresh();
    } finally {
      setTtsProbeLoading(false);
    }
  }

  async function runAsrProbe() {
    try {
      await runAction("asr-probe", "ASR 自检", async () => {
        setError(null);
        setAsrProbeLoading(true);
        await probeAsr(matchId);
        await refresh();
      });
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
      await runAction("archive-asr", "识别最新归档", async () => {
        setError(null);
        setArchiveAsrLoading(true);
        await recognizeArchivedSpeech(matchId, latestAudioAsset.speech_id);
        await refresh();
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "归档音频识别失败");
      await refresh();
    } finally {
      setArchiveAsrLoading(false);
    }
  }

  async function createExport() {
    try {
      await runAction("create-export", "生成导出包", async () => {
        setError(null);
        setExportBundle(await createExportBundle(matchId));
        setDataSummary(await getDataSummary(matchId));
        await refresh();
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "导出失败");
    }
  }

  async function refreshDataSummary() {
    try {
      setError(null);
      setDataSummaryLoading(true);
      setDataSummary(await getDataSummary(matchId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "读取数据管理摘要失败");
    } finally {
      setDataSummaryLoading(false);
    }
  }

  async function loadAuthStatus() {
    try {
      setError(null);
      setAuthLoading(true);
      setAuthStatus(await getRuntimeAuthStatus());
    } catch (err) {
      setError(err instanceof Error ? err.message : "读取访问安全状态失败");
    } finally {
      setAuthLoading(false);
    }
  }

  async function setAuthRequired(nextValue: boolean) {
    try {
      await runAction(`auth-required-${String(nextValue)}`, nextValue ? "开启 token 登录" : "关闭 token 登录", async () => {
        setError(null);
        setAuthLoading(true);
        setAuthStatus(await updateRuntimeAuthStatus({ auth_required: nextValue, reason: nextValue ? "admin_enable_auth" : "admin_disable_auth" }));
      }, {
        confirmText: nextValue ? "确认开启 token 登录？现场入口将需要口令。" : "确认关闭 token 登录？现场入口将不再要求口令。",
        danger: !nextValue
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "更新登录开关失败");
    } finally {
      setAuthLoading(false);
    }
  }

  async function rotateTokens() {
    const tokens = {
      admin: randomToken("adm"),
      host: randomToken("host"),
      screen: randomToken("screen"),
      speaker_shared: randomToken("spk")
    };
    try {
      await runAction("rotate-tokens", "生成并保存新 token", async () => {
        setError(null);
        setAuthLoading(true);
        setAuthStatus(await updateRuntimeAuthStatus({ auth_required: authStatus?.auth_required ?? true, tokens, reason: "admin_rotate_tokens" }));
        setGeneratedTokens(tokens);
      }, {
        confirmText: "确认轮换 token？旧的运行时 token 将失效，明文只显示这一次。",
        danger: true
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "轮换 token 失败");
    } finally {
      setAuthLoading(false);
    }
  }

  function updateSettingsField(field: keyof MatchSettingsDraft, value: string) {
    setSettingsDirty(true);
    setSettingsDraft((current) => ({ ...(current ?? emptyMatchDraft()), [field]: value }));
  }

  function updateTeamField(team: Team, field: keyof TeamSettingsDraft, value: string) {
    setDirtyTeams((current) => ({ ...current, [team.id]: true }));
    setTeamDrafts((current) => ({ ...current, [team.id]: { ...(current[team.id] ?? teamDraftFromSnapshot(team)), [field]: value } }));
  }

  function updateSpeakerField(speaker: Speaker, field: keyof SpeakerSettingsDraft, value: string) {
    setDirtySpeakers((current) => ({ ...current, [speaker.id]: true }));
    setSpeakerDrafts((current) => ({ ...current, [speaker.id]: { ...(current[speaker.id] ?? speakerDraftFromSnapshot(speaker)), [field]: value } }));
  }

  function updateSpeakerType(speaker: Speaker, value: SpeakerType) {
    const firstAgentConfigId = snapshot?.agent_configs?.[0]?.id ?? "";
    setDirtySpeakers((current) => ({ ...current, [speaker.id]: true }));
    setSpeakerDrafts((current) => {
      const previous = current[speaker.id] ?? speakerDraftFromSnapshot(speaker);
      if (value === "human") {
        return {
          ...current,
          [speaker.id]: {
            ...previous,
            speaker_type: "human",
            agent_config_id: "",
            model_name: "",
            model_kind: "",
            agent_endpoint: ""
          }
        };
      }
      return {
        ...current,
        [speaker.id]: {
          ...previous,
          speaker_type: "agent",
          agent_config_id: previous.agent_config_id || speaker.agent_config_id || firstAgentConfigId
        }
      };
    });
  }

  function updateAgentConfigField(config: AgentConfig, field: keyof AgentConfigDraft, value: string | number | boolean) {
    setDirtyAgentConfigs((current) => ({ ...current, [config.id]: true }));
    setAgentConfigDrafts((current) => ({
      ...current,
      [config.id]: {
        ...(current[config.id] ?? agentConfigDraftFromSnapshot(config)),
        [field]: field === "timeout_ms" ? Number(value) : value
      }
    }));
  }

  function updateNewAgentConfigField(field: keyof AgentConfigDraft, value: string | number | boolean) {
    setNewAgentDraft((current) => ({
      ...current,
      [field]: field === "timeout_ms" ? Number(value) : value
    }));
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

  function updateJudgeField(field: keyof JudgeVoteDraft, value: string | number) {
    setJudgeDirty(true);
    setJudgeDraft((current) => ({ ...(current ?? judgeDraft ?? emptyJudgeDraft()), [field]: typeof value === "number" ? safeVoteNumber(value) : value }) as JudgeVoteDraft);
  }

  if (!snapshot && loadError) return <AuthPrompt role="admin" message={loadError} />;
  if (!snapshot) return <div className="loading">正在加载技术后台...</div>;

  const match = snapshot.match;
  const phase = snapshot.phases.find((item) => item.id === match.current_phase_id);
  const currentSpeaker = snapshot.speakers.find((item) => item.id === snapshot.current_speech?.speaker_id);
  const primaryClock = match.live_mode === "free" ? clockByName(snapshot.clocks, "turn") : clockByName(snapshot.clocks, "main");
  const latestAudioAsset = snapshot.current_speech
    ? snapshot.audio_assets.find((item) => item.speech_id === snapshot.current_speech?.id) ?? snapshot.audio_assets[0]
    : snapshot.audio_assets[0];
  const agentReadyCount = snapshot.agent_status.filter((agent) => agent.status !== "failed").length;
  const micErrorCount = snapshot.speech_service.consoles.mic_errors?.length ?? 0;
  const preflightIssues = preflightReport ? preflightReport.score.fail + preflightReport.score.warn : null;
  const voteControlsLocked = match.status === "paused" || match.status === "intervention";
  const voteDisabledReason = voteControlsLocked ? "比赛暂停或应急处理中，继续比赛后才能操作投票。" : undefined;
  const agentConfigs = snapshot.agent_configs ?? [];
  const agentConfigById = new Map(agentConfigs.map((config) => [config.id, config]));
  const agentsBySpeakerId = new Map(snapshot.agent_status.map((agent) => [agent.speaker_id, agent]));
  const recentEvents = dataSummary?.recent_events ?? [];
  const filteredEvents = recentEvents.filter((event) => eventMatchesFilter(event.type, eventFilter));
  const filteredAuditLogs = auditLogs.filter((item) => auditMatchesFilter(item, auditFilter));
  const latestExportForReplay = exportBundle
    ? {
        export_id: exportBundle.export_id,
        match_id: exportBundle.match_id,
        download_url: exportBundle.download_url,
        size_bytes: exportBundle.size_bytes,
        entry_count: exportBundle.entries.length,
        entries: exportBundle.entries,
        created_at: exportBundle.created_at
      }
    : dataSummary?.latest_export ?? null;

  return (
    <main className="ops-shell">
      <aside className="ops-sidebar">
        <div className="ops-brand">
          <span>PhDebate</span>
          <strong>技术后台</strong>
        </div>
        <nav>
          {sections.map((item) => {
            const Icon = item.icon;
            return (
              <button key={item.id} className={activeSection === item.id ? "active" : ""} onClick={() => setActiveSection(item.id)}>
                <Icon size={16} />{item.label}
              </button>
            );
          })}
        </nav>
        <a className="ops-host-link" href="/host">打开主持导播台</a>
      </aside>

      <section className="ops-main">
        <header className="ops-header">
          <div>
            <h1>{match.title}</h1>
            <p>{match.topic}</p>
          </div>
          <div className="ops-header-status">
            <StatusPill tone={match.status === "running" ? "green" : match.status === "intervention" ? "red" : "gold"}>{matchStatusLabel(match.status)}</StatusPill>
            <StatusPill tone={socketStatus === "open" ? "green" : "red"}>实时 {socketStatusLabel(socketStatus)}</StatusPill>
            <StatusPill tone="blue">序号 {snapshot.last_seq}</StatusPill>
          </div>
        </header>

        {error && <div className="error-banner">{error}</div>}

        {activeSection === "overview" && (
          <section className="ops-grid overview">
            <div className="ops-card span-2">
              <div className="ops-card-head">
                <span><RadioTower size={16} />现场状态</span>
                <button {...busyProps("refresh-preflight")} onClick={refreshPreflightReport} disabled={preflightLoading}>{preflightLoading ? "检查中" : "刷新体检"}</button>
              </div>
              <div className="ops-kpi-row">
                <Kpi label="当前环节" value={phase?.name ?? "未指定"} detail={match.live_mode === "free" ? `${sideLabel(snapshot.free_debate.current_turn_side)}第 ${snapshot.free_debate.turn_index} 轮` : liveModeLabel(match.live_mode)} />
                <Kpi label="当前发言" value={speakerLabel(currentSpeaker)} detail={speechSourceLabel(snapshot.current_speech?.source)} />
                <Kpi label="关键计时" value={formatClockShort(primaryClock)} detail={clockStateLabel(primaryClock?.state)} />
                <Kpi label="现场健康" value={preflightIssues === null ? `${agentReadyCount}/${snapshot.agent_status.length} AI` : preflightIssues ? `${preflightIssues} 项待处理` : "体检通过"} detail={`辩手端 ${snapshot.speech_service.consoles.online}/${snapshot.speech_service.consoles.total} · 麦克风异常 ${micErrorCount}`} />
              </div>
              <div className="ops-clock-row">
                {(match.live_mode === "free"
                  ? snapshot.clocks.filter((clock) => ["affirmative_total", "turn", "negative_total"].includes(clock.name))
                  : snapshot.clocks.filter((clock) => clock.name === "main")
                ).map((clock) => <ClockTile key={clock.name} label={clockLabel(clock.name)} clock={clock} compact />)}
              </div>
            </div>

            <div className="ops-card">
              <div className="ops-card-head"><span><Check size={16} />赛前体检</span></div>
              {preflightReport ? <PreflightSummary report={preflightReport} /> : <p className="muted-line">刷新后生成后端权威体检报告。</p>}
            </div>

            <div className="ops-card">
              <div className="ops-card-head"><span><Bot size={16} />Agent 状态</span><button {...busyProps(actionKey(`/api/matches/${matchId}/agents/health`))} onClick={() => action(`/api/matches/${matchId}/agents/health`)}>全部检查</button></div>
              <div className="ops-compact-list">
                {snapshot.agent_status.map((agent) => (
                  <div key={agent.speaker_id}>
                    <strong>{agent.name}</strong>
                    <span>{agentStatusLabel(agent.status)} · {agent.detail}</span>
                  </div>
                ))}
              </div>
            </div>

            <RehearsalPanel snapshot={snapshot} socketStatus={socketStatus} />
          </section>
        )}

        {activeSection === "matches" && (
          <section className="ops-grid matches">
            <div className="ops-card span-2 ops-match-instance">
              <div className="ops-card-head">
                <span><FileArchive size={16} />比赛实例</span>
                <p className="muted-line">比赛管理只处理“一次比赛”的生命周期；题目、队伍和赛制请到“展示与赛制”维护。</p>
              </div>
              <div className="ops-kpi-row">
                <Kpi label="当前比赛 ID" value={match.id} detail={`创建 ${formatDateTime(match.created_at)}`} />
                <Kpi label="比赛状态" value={matchStatusLabel(match.status)} detail={`大屏 ${screenSceneLabel(match.screen_scene)}`} />
                <Kpi label="当前环节" value={phase?.name ?? "未指定"} detail={match.live_mode === "free" ? `自由辩论第 ${snapshot.free_debate.turn_index} 轮` : liveModeLabel(match.live_mode)} />
                <Kpi label="历史归档" value={`${dataSummary?.counts.archives ?? 0}`} detail={dataSummaryLoading ? "刷新中" : "重置比赛后自动生成"} />
              </div>
              <div className="ops-instance-actions">
                <button {...busyProps("create-export")} onClick={createExport}><Download size={16} />生成当前导出包</button>
                {exportBundle && <a href={withCurrentAuthQuery(exportBundle.download_url)} target="_blank" rel="noreferrer">下载刚生成的导出包</a>}
                <button
                  className="danger"
                  onClick={() => action(`/api/matches/${matchId}/reset`, { confirm_text: "重置比赛" }, "确认归档当前比赛并创建一场新比赛？新比赛会继承当前展示、赛制、席位和 Agent 配置。")}
                >
                  <RotateCcw size={16} />归档并创建新比赛
                </button>
              </div>
              <p className="ops-instance-note">执行“归档并创建新比赛”后，当前比赛的发言、转写、投票、音频和事件会进入历史归档；新比赛会生成新的 ID，并回到候场状态。</p>
            </div>

            <div className="ops-card">
              <div className="ops-card-head"><span>当前配置快照</span></div>
              <div className="ops-object-list">
                <div><strong>展示信息</strong><span>{match.title}</span><em>{match.topic}</em></div>
                <div><strong>双方队伍</strong><span>{snapshot.teams.map((team) => `${sideLabel(team.side)} ${team.name}`).join(" · ")}</span><em>队伍信息在“展示与赛制”维护</em></div>
                <div><strong>固定席位</strong><span>8 个席位 · {snapshot.speakers.filter((speaker) => speaker.speaker_type === "agent").length} 个 Agent</span><em>席位绑定在“辩手管理”维护</em></div>
              </div>
            </div>

            <div className="ops-card">
              <div className="ops-card-head"><span>最近历史归档</span><button onClick={refreshDataSummary} disabled={dataSummaryLoading}>{dataSummaryLoading ? "刷新中" : "刷新"}</button></div>
              <div className="archive-list compact">
                {dataSummary?.archives.slice(0, 4).map((archive) => (
                  <div key={archive.id} className="archive-row">
                    <div>
                      <strong>{archive.archived_match_id}</strong>
                      <span>{formatDateTime(archive.created_at)} · 投票 {archive.counts.audience_votes} · 转写 {archive.counts.transcript_segments}</span>
                    </div>
                    {archive.export_bundle ? <a href={withCurrentAuthQuery(archive.export_bundle.download_url)} target="_blank" rel="noreferrer">下载</a> : <span className="muted-line">未导出</span>}
                  </div>
                ))}
                {dataSummary && !dataSummary.archives.length && <p className="muted-line">暂无历史归档。归档并创建新比赛后会出现在这里。</p>}
                {!dataSummary && <p className="muted-line">正在等待数据摘要。</p>}
              </div>
            </div>
          </section>
        )}

        {activeSection === "setup" && settingsDraft && (
          <section className="ops-grid setup">
            <form className="ops-card span-2 ops-form" onSubmit={(event) => { event.preventDefault(); void saveSettings(); }}>
              <div className="ops-card-head"><span><Settings size={16} />展示信息</span><button {...busyProps("save-match-settings")} type="submit" disabled={!settingsDirty}>保存展示信息</button></div>
              <label><span>比赛名称</span><input value={settingsDraft.title} onChange={(event) => updateSettingsField("title", event.target.value)} /></label>
              <label><span>辩题</span><textarea rows={3} value={settingsDraft.topic} onChange={(event) => updateSettingsField("topic", event.target.value)} /></label>
              <div className="ops-two-col">
                <label><span>正方立场</span><input value={settingsDraft.affirmative_position} onChange={(event) => updateSettingsField("affirmative_position", event.target.value)} /></label>
                <label><span>反方立场</span><input value={settingsDraft.negative_position} onChange={(event) => updateSettingsField("negative_position", event.target.value)} /></label>
                <label><span>主办方</span><input value={settingsDraft.organizer} onChange={(event) => updateSettingsField("organizer", event.target.value)} /></label>
                <label><span>会场</span><input value={settingsDraft.venue} onChange={(event) => updateSettingsField("venue", event.target.value)} /></label>
              </div>
            </form>

            <div className="ops-card span-2">
              <div className="ops-card-head"><span><UsersRound size={16} />队伍展示</span></div>
              <div className="ops-team-grid">
                {snapshot.teams.map((team) => (
                  <form className={`ops-team ${sideClass(team.side)}`} key={team.id} onSubmit={(event) => { event.preventDefault(); void saveTeam(team); }}>
                    <div><strong>{sideLabel(team.side)}</strong><button {...busyProps(`save-team-${team.id}`)} type="submit" disabled={!dirtyTeams[team.id]}>保存</button></div>
                    <label><span>队伍名称</span><input value={(teamDrafts[team.id] ?? teamDraftFromSnapshot(team)).name} onChange={(event) => updateTeamField(team, "name", event.target.value)} /></label>
                    <label><span>队伍立场</span><input value={(teamDrafts[team.id] ?? teamDraftFromSnapshot(team)).position} onChange={(event) => updateTeamField(team, "position", event.target.value)} /></label>
                    <label><span>描述</span><textarea rows={2} value={(teamDrafts[team.id] ?? teamDraftFromSnapshot(team)).description} onChange={(event) => updateTeamField(team, "description", event.target.value)} /></label>
                  </form>
                ))}
              </div>
            </div>

            <div className="ops-card span-2">
              <div className="ops-card-head"><span><SlidersHorizontal size={16} />赛制规则</span></div>
              <div className="ops-phase-settings">
                {snapshot.phases.map((item) => (
                  <form key={item.id} onSubmit={(event) => { event.preventDefault(); void savePhase(item); }}>
                    <div><strong>{item.display_order}. {item.name}</strong><button {...busyProps(`save-phase-${item.id}`)} type="submit" disabled={!dirtyPhases[item.id]}>保存</button></div>
                    <label><span>环节名称</span><input value={(phaseDrafts[item.id] ?? phaseDraftFromSnapshot(item)).name} onChange={(event) => updatePhaseField(item, "name", event.target.value)} /></label>
                    {item.phase_type === "free_debate" ? (
                      <div className="ops-two-col">
                        <label><span>每方总时长（秒）</span><input type="number" min={30} max={1800} value={(phaseDrafts[item.id] ?? phaseDraftFromSnapshot(item)).side_total_seconds} onChange={(event) => updatePhaseField(item, "side_total_seconds", event.target.valueAsNumber)} /></label>
                        <label><span>单次上限（秒）</span><input type="number" min={5} max={120} value={(phaseDrafts[item.id] ?? phaseDraftFromSnapshot(item)).turn_seconds} onChange={(event) => updatePhaseField(item, "turn_seconds", event.target.valueAsNumber)} /></label>
                      </div>
                    ) : (
                      <label><span>环节时长（秒）</span><input type="number" min={30} max={3600} value={(phaseDrafts[item.id] ?? phaseDraftFromSnapshot(item)).duration_seconds} onChange={(event) => updatePhaseField(item, "duration_seconds", event.target.valueAsNumber)} /></label>
                    )}
                  </form>
                ))}
              </div>
            </div>
          </section>
        )}

        {activeSection === "speakers" && (
          <section className="ops-grid speakers">
            <div className="ops-card span-2">
              <div className="ops-card-head">
                <span><UsersRound size={16} />辩手管理</span>
                <p className="muted-line">管理 8 个固定比赛席位，点击编辑按钮设置姓名、类型和 Agent 绑定。</p>
              </div>
              {(["affirmative", "negative"] as Side[]).map((side) => {
                const team = snapshot.teams.find((item) => item.side === side);
                const sideSpeakers = snapshot.speakers.filter((item) => item.side === side).sort((a, b) => a.seat - b.seat);
                return (
                  <div key={side} className="ops-roster-group">
                    <div className={`ops-roster-group-head ${sideClass(side)}`}>
                      <span>{sideLabel(side)}</span>
                      <strong>{team?.name ?? "未配置队伍"}</strong>
                      {team?.position && <em>{team.position}</em>}
                    </div>
                    <table className="ops-table">
                      <thead>
                        <tr><th>席位</th><th>姓名</th><th>身份类型</th><th>绑定 Agent</th><th>运行状态</th><th>操作</th></tr>
                      </thead>
                      <tbody>
                        {sideSpeakers.map((speaker) => {
                          const agent = agentsBySpeakerId.get(speaker.id);
                          const boundConfig = speaker.agent_config_id ? agentConfigById.get(speaker.agent_config_id) : undefined;
                          return (
                            <tr key={speaker.id}>
                              <td className="ops-td-seat">{seatLabel(speaker.seat)}</td>
                              <td className="ops-td-name"><strong>{speaker.name || "未命名"}</strong></td>
                              <td><StatusPill tone={speaker.speaker_type === "agent" ? "blue" : "green"}>{speakerTypeLabel(speaker.speaker_type)}</StatusPill></td>
                              <td>{boundConfig ? <span className="ops-td-tag">{boundConfig.name}</span> : <span className="ops-td-muted">未绑定</span>}</td>
                              <td>{speaker.speaker_type === "agent" ? <StatusPill tone={agentStatusTone(agent?.status)}>{agentStatusLabel(agent?.status)}</StatusPill> : <span className="ops-td-muted">—</span>}</td>
                              <td>
                                <button type="button" className="ops-table-btn" onClick={() => { setSpeakerEditTarget(speaker); setSpeakerDrafts((c) => ({ ...c, [speaker.id]: speakerDraftFromSnapshot(speaker) })); }}>编辑</button>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                );
              })}
            </div>
          </section>
        )}

        {activeSection === "agents" && (
          <section className="ops-grid agents">
            <div className="ops-card span-2">
              <div className="ops-card-head">
                <span><Bot size={16} />Agent 配置库</span>
                <div className="ops-button-row">
                  <button type="button" className="primary" onClick={() => { setAgentEditTarget("new"); setNewAgentDraft(emptyAgentConfigDraft()); }}><Plus size={16} />新增 Agent</button>
                </div>
              </div>
              <p className="ops-object-intro">先配置可复用 Agent，再在"辩手管理"中绑定到 AI 席位。API Key 只填写环境变量名，不进入前端。</p>
              {agentConfigs.length > 0 ? (
                <table className="ops-table">
                  <thead>
                    <tr><th>名称</th><th>类型</th><th>模型</th><th>绑定席位</th><th>状态</th><th>操作</th></tr>
                  </thead>
                  <tbody>
                    {agentConfigs.map((config) => {
                      const boundSpeakers = snapshot.speakers.filter((speaker) => speaker.agent_config_id === config.id);
                      return (
                        <tr key={config.id}>
                          <td className="ops-td-name"><strong>{config.name}</strong></td>
                          <td><span className="ops-td-tag">{config.provider_type === "openai_sdk" ? "OpenAI SDK" : "REST API"}</span></td>
                          <td><code className="ops-td-code">{config.model_name || "—"}</code></td>
                          <td>{boundSpeakers.length ? <span className="ops-td-tag">{boundSpeakers.map((s) => `${sideLabel(s.side)}${seatLabel(s.seat)}`).join("、")}</span> : <span className="ops-td-muted">未绑定</span>}</td>
                          <td><StatusPill tone={config.enabled ? "green" : "muted"}>{config.enabled ? "启用" : "停用"}</StatusPill></td>
                          <td>
                            <div className="ops-table-actions">
                              <button type="button" className="ops-table-btn" onClick={() => { setAgentEditTarget(config); setAgentConfigDrafts((c) => ({ ...c, [config.id]: agentConfigDraftFromSnapshot(config) })); }}>编辑</button>
                              <button type="button" className="ops-table-btn danger" onClick={() => void deleteAgentConfig(config)}>删除</button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              ) : (
                <p className="muted-line">暂无 Agent 配置，请先点击「新增 Agent」创建。</p>
              )}
            </div>

            <div className="ops-card span-2">
              <div className="ops-card-head">
                <span><Bot size={16} />Agent 运行状态</span>
                <div className="ops-button-row">
                  <button {...busyProps(actionKey(`/api/matches/${matchId}/agents/health`))} onClick={() => action(`/api/matches/${matchId}/agents/health`)}>全部检查</button>
                </div>
              </div>
              <div className="ops-agent-management-list">
                {snapshot.speakers.filter((speaker) => speaker.speaker_type === "agent").map((speaker) => {
                  const agent = agentsBySpeakerId.get(speaker.id);
                  const boundConfig = speaker.agent_config_id ? agentConfigById.get(speaker.agent_config_id) : undefined;
                  return (
                    <div className={`ops-agent-card ${agent?.status ?? "pending"}`} key={speaker.id}>
                      <i />
                      <div className="ops-agent-main">
                        <span>{sideLabel(speaker.side)} · {seatLabel(speaker.seat)}</span>
                        <strong>{speaker.name}</strong>
                        <em>{boundConfig ? boundConfig.name : "未绑定配置"} · {speaker.model_name || "未配置模型"} · {speaker.model_kind === "open_source" ? "开源模型" : "闭源模型"}</em>
                        <code>{speaker.agent_endpoint || agent?.endpoint || "未配置 Agent API 地址"}</code>
                      </div>
                      <div className="ops-agent-health">
                        <StatusPill tone={agentStatusTone(agent?.status)}>{agentStatusLabel(agent?.status)}</StatusPill>
                        <span>{agent?.detail ?? "等待联调"}</span>
                        {agent?.latency_ms !== undefined && <em>{agent.latency_ms}ms</em>}
                      </div>
                      <div className="ops-agent-actions">
                        <button onClick={() => action(`/api/matches/${matchId}/agent/${speaker.id}/health`)}>检查</button>
                        <button onClick={() => action(`/api/matches/${matchId}/agent/${speaker.id}/retry`)}>重试</button>
                        <button onClick={() => action(`/api/matches/${matchId}/agent/${speaker.id}/interrupt`, { reason: "admin" })}>中断</button>
                        <button onClick={() => manualAgentInput(speaker)}>代输入</button>
                      </div>
                    </div>
                  );
                })}
                {!snapshot.speakers.some((speaker) => speaker.speaker_type === "agent") && <p className="muted-line">当前没有 Agent 辩手，请先在"辩手管理"中把席位设为 Agent。</p>}
              </div>
            </div>
          </section>
        )}

        {activeSection === "speech" && (
          <section className="ops-grid speech">
            <div className="ops-card span-2">
              <div className="ops-card-head">
                <span><Mic size={16} />语音链路</span>
                <div className="ops-button-row">
                  <button {...busyProps("speech-diagnostics")} onClick={checkSpeechDiagnostics} disabled={speechDiagnosticsLoading}>{speechDiagnosticsLoading ? "检查中" : "配置检查"}</button>
                  <button {...busyProps("asr-probe")} onClick={runAsrProbe} disabled={asrProbeLoading}>{asrProbeLoading ? "识别中" : "ASR 自检"}</button>
                  <button {...busyProps("tts-probe")} onClick={runTtsProbe} disabled={ttsProbeLoading}>{ttsProbeLoading ? "合成中" : "TTS 试合成"}</button>
                </div>
              </div>
              <div className="ops-kpi-row">
                <Kpi label="ASR" value={serviceStatusLabel(snapshot.speech_service.asr.status)} detail={`${snapshot.speech_service.asr.latency_ms} ms · ${snapshot.speech_service.asr.active_sessions ?? 0} 路`} />
                <Kpi label="TTS" value={serviceStatusLabel(snapshot.speech_service.tts.status)} detail={`队列 ${snapshot.speech_service.tts.queue_size ?? 0}`} />
                <Kpi label="音频归档" value={latestAudioAsset ? audioAssetStatusLabel(latestAudioAsset.status) : "未开始"} detail={latestAudioAsset ? `${latestAudioAsset.chunk_count} 段 · ${formatBytes(latestAudioAsset.size_bytes)}` : "无归档"} />
                <Kpi label="辩手端" value={`${snapshot.speech_service.consoles.online}/${snapshot.speech_service.consoles.total}`} detail={`麦克风异常 ${micErrorCount}`} />
              </div>
              {latestAudioAsset && <div className="ops-button-row"><button {...busyProps("archive-asr")} onClick={recognizeLatestArchive} disabled={archiveAsrLoading}>{archiveAsrLoading ? "识别中" : "识别最新归档"}</button><span className="path-line">{latestAudioAsset.file_path}</span></div>}
              {speechDiagnostics && <DiagnosticsSummary diagnostics={speechDiagnostics} />}
            </div>

            <div className="ops-card span-2">
              <div className="ops-card-head"><span>转写修订</span></div>
              <TranscriptList matchId={matchId} speakers={snapshot.speakers} segments={snapshot.recent_transcript} reviseSpeech={reviseSpeech} patchAction={patchAction} />
            </div>
          </section>
        )}

        {activeSection === "votes" && judgeDraft && (
          <section className="ops-grid votes">
            <form className="ops-card ops-form" onSubmit={(event) => { event.preventDefault(); void saveJudgeVotes(); }}>
              <div className="ops-card-head"><span><Vote size={16} />评委票录入</span><button {...busyProps("save-judge-votes")} type="submit" disabled={!judgeDirty || voteControlsLocked} title={voteDisabledReason}>保存评委票</button></div>
              <div className="ops-two-col">
                <label><span>立论 · 正方</span><input type="number" min={0} value={judgeDraft.constructive_affirmative} onChange={(event) => updateJudgeField("constructive_affirmative", event.target.valueAsNumber)} /></label>
                <label><span>立论 · 反方</span><input type="number" min={0} value={judgeDraft.constructive_negative} onChange={(event) => updateJudgeField("constructive_negative", event.target.valueAsNumber)} /></label>
                <label><span>过程 · 正方</span><input type="number" min={0} value={judgeDraft.process_affirmative} onChange={(event) => updateJudgeField("process_affirmative", event.target.valueAsNumber)} /></label>
                <label><span>过程 · 反方</span><input type="number" min={0} value={judgeDraft.process_negative} onChange={(event) => updateJudgeField("process_negative", event.target.valueAsNumber)} /></label>
                <label><span>结辩 · 正方</span><input type="number" min={0} value={judgeDraft.conclusion_affirmative} onChange={(event) => updateJudgeField("conclusion_affirmative", event.target.valueAsNumber)} /></label>
                <label><span>结辩 · 反方</span><input type="number" min={0} value={judgeDraft.conclusion_negative} onChange={(event) => updateJudgeField("conclusion_negative", event.target.valueAsNumber)} /></label>
              </div>
              <label><span>正式优胜方</span><select value={judgeDraft.winner_side} onChange={(event) => updateJudgeField("winner_side", event.target.value as Side)}><option value="affirmative">正方</option><option value="negative">反方</option></select></label>
              <label><span>最佳辩手</span><select value={judgeDraft.best_speaker_id} onChange={(event) => updateJudgeField("best_speaker_id", event.target.value)}>{snapshot.speakers.map((speaker) => <option value={speaker.id} key={speaker.id}>{sideLabel(speaker.side)}{seatLabel(speaker.seat)} · {speaker.name}</option>)}</select></label>
            </form>

            <div className="ops-card">
              <div className="ops-card-head"><span>公布控制</span></div>
              <div className="ops-button-stack">
                <button disabled={voteControlsLocked} title={voteDisabledReason} onClick={() => action(`/api/matches/${matchId}/audience-votes/open`)}>开启学生投票</button>
                <button disabled={voteControlsLocked} title={voteDisabledReason} onClick={() => action(`/api/matches/${matchId}/audience-votes/close`, {}, "确认关闭学生投票？")}>关闭学生投票</button>
                <button disabled={voteControlsLocked} title={voteDisabledReason} onClick={() => action(`/api/matches/${matchId}/screen/scene`, { scene: "judge_commentary" })}>切到评委点评</button>
                <button disabled={voteControlsLocked} title={voteDisabledReason} onClick={() => action(`/api/matches/${matchId}/votes/publish`, { scope: "judge" })}>公布评委结果</button>
                <button disabled={voteControlsLocked} title={voteDisabledReason} onClick={() => action(`/api/matches/${matchId}/votes/publish`, { scope: "audience" })}>公布学生结果</button>
              </div>
              <Kpi label="学生票" value={`${snapshot.vote_state.audience_count}`} detail={voteWindowStatusLabel(snapshot.vote_state.window_status)} />
            </div>

            <VoteSummary voteState={snapshot.vote_state} speakers={snapshot.speakers} />
          </section>
        )}

        {activeSection === "emergency" && (
          <section className="ops-grid emergency">
            <div className="ops-card danger-zone span-2">
              <div className="ops-card-head"><span><AlertTriangle size={16} />应急干预</span></div>
              <div className="ops-danger-grid">
                <button {...busyProps(actionKey(`/api/matches/${matchId}/emergency-stop`, { reason: "admin_emergency" }))} onClick={() => action(`/api/matches/${matchId}/emergency-stop`, { reason: "admin_emergency" }, "确认进入紧急停止状态？")}><AlertTriangle size={18} />紧急停止</button>
                <button onClick={() => action(`/api/matches/${matchId}/speeches/current/stop`, { reason: "admin_force_stop" }, "确认强制结束当前发言？")} disabled={!snapshot.current_speech}>强制结束当前发言</button>
                <button onClick={() => action(`/api/matches/${matchId}/phases/${match.current_phase_id}/skip`, { reason: "admin_skip" }, "确认跳过当前环节？")}><SkipForward size={16} />跳过当前环节</button>
                <button onClick={() => action(`/api/matches/${matchId}/phases/${match.current_phase_id}/rollback`, { reason: "admin_rollback" }, "确认回滚并作废后续转写？")}><RotateCcw size={16} />回滚当前环节</button>
                <button onClick={() => action(`/api/matches/${matchId}/reset`, { confirm_text: "重置比赛" }, "确认归档当前比赛并重置？此操作会清空当前运行数据。")}><RotateCcw size={16} />重置比赛</button>
              </div>
            </div>

            <div className="ops-card">
              <div className="ops-card-head"><span>时钟校准</span></div>
              {snapshot.clocks.map((clock) => (
                <div className="ops-clock-control" key={clock.name}>
                  <strong>{clockLabel(clock.name)} · {formatClockShort(clock)}</strong>
                  <button onClick={() => action(`/api/matches/${matchId}/clocks/${clock.name}/pause`, { reason: "admin_pause" })}>暂停</button>
                  <button onClick={() => action(`/api/matches/${matchId}/clocks/${clock.name}/resume`, { reason: "admin_resume" })}>继续</button>
                  <button onClick={() => action(`/api/matches/${matchId}/clocks/${clock.name}/adjust`, { remaining_ms: 15000, reason: "admin_adjust_15s" })}>15s</button>
                  <button onClick={() => action(`/api/matches/${matchId}/clocks/${clock.name}/adjust`, { remaining_ms: 60000, reason: "admin_adjust_60s" })}>60s</button>
                </div>
              ))}
            </div>

            <div className="ops-card">
              <div className="ops-card-head"><span>故障模拟/降级</span></div>
              <div className="ops-button-stack">
                {currentSpeaker?.speaker_type === "human" && <button onClick={() => action(`/api/matches/${matchId}/speakers/${currentSpeaker.id}/asr/fail`, { reason: "admin_manual_asr_fail" })}>标记 ASR 异常</button>}
                {currentSpeaker?.speaker_type === "agent" && <button onClick={() => action(`/api/matches/${matchId}/speakers/${currentSpeaker.id}/tts/fail`, { reason: "admin_manual_tts_fail", text_only: true })}>TTS 降级</button>}
                {!currentSpeaker && <p className="muted-line">指定当前发言人后可触发链路降级。</p>}
              </div>
            </div>
          </section>
        )}

        {activeSection === "security" && (
          <section className="ops-grid security">
            <div className="ops-card">
              <div className="ops-card-head"><span><KeyRound size={16} />登录开关</span><button onClick={loadAuthStatus} disabled={authLoading}>{authLoading ? "刷新中" : "刷新"}</button></div>
              {authStatus ? (
                <>
                  <Kpi label="当前状态" value={authStatus.auth_required ? "已开启" : "未开启"} detail={authStatus.runtime_configured ? "后台运行时配置" : "环境默认值"} />
                  <div className="ops-button-row">
                    <button {...busyProps("auth-required-true")} onClick={() => setAuthRequired(true)} disabled={authStatus.auth_required || authLoading}>开启 token 登录</button>
                    <button {...busyProps("auth-required-false")} onClick={() => setAuthRequired(false)} disabled={!authStatus.auth_required || authLoading}>关闭 token 登录</button>
                  </div>
                  <p className="path-line">{authStatus.runtime_path}</p>
                </>
              ) : <p className="muted-line">点击刷新读取当前权限状态。</p>}
            </div>

            <div className="ops-card">
              <div className="ops-card-head"><span>Token 轮换</span><button {...busyProps("rotate-tokens")} onClick={rotateTokens} disabled={authLoading}>生成并保存新 token</button></div>
              {generatedTokens ? (
                <div className="token-output">
                  {Object.entries(generatedTokens).map(([role, token]) => <p key={role}><strong>{role}</strong><code>{token}</code></p>)}
                </div>
              ) : <p className="muted-line">明文 token 只在生成后显示一次；后台只保存 hash。</p>}
              {authStatus && (
                <div className="ops-compact-list">
                  {Object.entries(authStatus.token_sources).map(([role, info]) => <div key={role}><strong>{role}</strong><span>env {String(Boolean(info.env || info.env_count))} · runtime {info.runtime_count ?? 0} · file {info.file_count ?? 0}</span></div>)}
                </div>
              )}
            </div>

            <AccessLinksPanel matchId={matchId} title={match.title} topic={match.topic} speakers={snapshot.speakers} />
          </section>
        )}

        {activeSection === "data" && (
          <section className="ops-grid data">
            <div className="ops-card span-2">
              <div className="ops-card-head">
                <span><FileArchive size={16} />数据管理</span>
                <div className="ops-button-row">
                  <button {...busyProps("refresh-data-summary")} onClick={refreshDataSummary} disabled={dataSummaryLoading}>{dataSummaryLoading ? "刷新中" : "刷新摘要"}</button>
                  <button {...busyProps("create-export")} onClick={createExport}>生成导出包</button>
                </div>
              </div>
              <p className="muted-line">查看当前比赛数据覆盖、历史归档和导出包。这里不展示明文 token 或 API key。</p>
              <div className="ops-kpi-row">
                <Kpi label="当前比赛" value={dataSummary?.match.id ?? match.id} detail={matchStatusLabel(dataSummary?.match.status ?? match.status)} />
                <Kpi label="发言文本" value={`${dataSummary?.counts.transcript_segments ?? snapshot.recent_transcript.length}`} detail={`定稿 ${dataSummary?.counts.final_transcript_segments ?? snapshot.recent_transcript.filter((item) => item.is_final).length}`} />
                <Kpi label="音频归档" value={`${dataSummary?.counts.audio_assets ?? snapshot.audio_assets.length}`} detail={`${dataSummary?.counts.audio_chunks ?? snapshot.audio_assets.reduce((sum, item) => sum + (item.chunks?.length ?? 0), 0)} 个分片`} />
                <Kpi label="历史归档" value={`${dataSummary?.counts.archives ?? 0}`} detail={`事件 ${dataSummary?.counts.events ?? snapshot.last_seq} · 审计 ${dataSummary?.counts.audit_logs ?? auditLogs.length}`} />
              </div>
              <div className="data-health-grid">
                <div><strong>席位结构</strong><span>{dataSummary?.counts.human_speakers ?? snapshot.speakers.filter((speaker) => speaker.speaker_type === "human").length} 人类 · {dataSummary?.counts.agent_speakers ?? snapshot.speakers.filter((speaker) => speaker.speaker_type === "agent").length} Agent</span></div>
                <div><strong>结构化表</strong><span>{dataSummary ? `环节 ${dataSummary.structured_counts.phases} · 席位 ${dataSummary.structured_counts.slots} · 投票 ${dataSummary.structured_counts.votes} · 运行设置 ${dataSummary.structured_counts.runtime_settings ?? 0}` : "等待摘要"}</span></div>
                <div><strong>复盘记录</strong><span>{dataSummary ? `Agent 请求 ${dataSummary.counts.agent_requests} · 语音请求 ${dataSummary.counts.speech_service_requests} · 导出 ${dataSummary.counts.export_bundles}` : "等待摘要"}</span></div>
                <div><strong>学生投票</strong><span>{dataSummary?.counts.audience_votes ?? snapshot.vote_state.audience_count} 票 · 去重键 {dataSummary?.counts.audience_vote_keys ?? 0}</span></div>
                <div>
                  <strong>持久化</strong>
                  <span>{dataSummary?.persistence.driver ?? snapshot.system?.persistence?.driver ?? "memory"}</span>
                  <em title={dataSummary?.persistence.database_path ?? snapshot.system?.persistence?.database_path ?? "not configured"}>
                    {formatStoragePath(dataSummary?.persistence.database_path ?? snapshot.system?.persistence?.database_path)}
                  </em>
                </div>
                <div><strong>最近事件</strong><span>{dataSummary?.latest_event?.type ?? lastEvent?.type ?? "等待事件"} · 序号 {dataSummary?.latest_event?.seq ?? snapshot.last_seq}</span></div>
              </div>
            </div>

            {dataSummary && <ReplayHealthPanel summary={dataSummary} onSelect={setReplaySelection} />}

            {replaySelection && (
              <ReplayDetailPanel
                selection={replaySelection}
                snapshot={snapshot}
                exportBundle={latestExportForReplay}
                onClose={() => setReplaySelection(null)}
              />
            )}

            <div className="ops-card">
              <div className="ops-card-head"><span><Download size={16} />导出包</span></div>
              {latestExportForReplay ? (
                <ExportBundlePreview
                  bundle={latestExportForReplay}
                  freshness={exportBundle?.export_id === latestExportForReplay.export_id ? "刚生成" : formatDateTime(latestExportForReplay.created_at)}
                />
              ) : <p className="muted-line">尚未生成当前比赛导出包。</p>}
            </div>

            <div className="ops-card">
              <div className="ops-card-head"><span>历史归档</span></div>
              <div className="archive-list">
                {dataSummary?.archives.map((archive) => (
                  <div key={archive.id} className="archive-row">
                    <div>
                      <strong>{archive.archived_match_id}</strong>
                      <span>{formatDateTime(archive.created_at)} · 转写 {archive.counts.transcript_segments} · 投票 {archive.counts.audience_votes}</span>
                      <em>{archive.title}</em>
                    </div>
                    {archive.export_bundle ? <a href={withCurrentAuthQuery(archive.export_bundle.download_url)} target="_blank" rel="noreferrer">下载归档</a> : <span className="muted-line">无导出包</span>}
                  </div>
                ))}
                {dataSummary && !dataSummary.archives.length && <p className="muted-line">暂无历史归档。执行“重置比赛”后会生成归档记录。</p>}
                {!dataSummary && <p className="muted-line">正在等待数据摘要。</p>}
              </div>
            </div>

            <div className="ops-card span-2">
              <div className="ops-card-head">
                <span>事件与审计</span>
                <StatusPill tone="blue">序号 {snapshot.last_seq}</StatusPill>
              </div>
              <div className="event-audit-toolbar">
                <div>
                  {(["all", "control", "speech", "vote", "error"] as EventFilter[]).map((filter) => (
                    <button key={filter} className={eventFilter === filter ? "active" : ""} onClick={() => setEventFilter(filter)}>{eventFilterLabel(filter)}</button>
                  ))}
                </div>
                <div>
                  {(["all", "failed", "host", "admin"] as AuditFilter[]).map((filter) => (
                    <button key={filter} className={auditFilter === filter ? "active" : ""} onClick={() => setAuditFilter(filter)}>{auditFilterLabel(filter)}</button>
                  ))}
                </div>
              </div>
              <p className="muted-line">
                最近事件：{lastEvent?.type ?? "等待事件"} · 类型 {eventCountsText(dataSummary?.event_type_counts)}
              </p>
              <div className="event-audit-grid">
                <div>
                  <h3>最近事件</h3>
                  <div className="event-list">
                    {filteredEvents.map((event) => (
                      <button type="button" className={`event-row ${eventCategory(event.type)}`} key={event.id} onClick={() => setReplaySelection({ kind: "event", item: event })}>
                        <strong>{event.type}</strong>
                        <span>{event.actor_type}{event.actor_id ? ` · ${event.actor_id}` : ""}</span>
                        <em>{formatDateTime(event.created_at)} · 序号 {event.seq}</em>
                      </button>
                    ))}
                    {dataSummary && !filteredEvents.length && <p className="muted-line">当前筛选下暂无事件。</p>}
                    {!dataSummary && <p className="muted-line">正在等待数据摘要。</p>}
                  </div>
                </div>
                <div>
                  <h3>审计记录</h3>
                  <div className="audit-list">
                    {filteredAuditLogs.map((item) => <button type="button" className={`audit-row ${item.result}`} key={item.id} onClick={() => setReplaySelection({ kind: "audit", item })}><strong>{item.action}</strong><span>{item.actor_type}{item.actor_id ? ` · ${item.actor_id}` : ""}</span><em>{formatDateTime(item.created_at)} · {auditResultLabel(item.result)}</em></button>)}
                    {!filteredAuditLogs.length && <p className="muted-line">当前筛选下暂无审计记录。</p>}
                  </div>
                </div>
              </div>
            </div>
          </section>
        )}
      </section>

      {speakerEditTarget && (() => {
        const speaker = speakerEditTarget;
        const draft = speakerDrafts[speaker.id] ?? speakerDraftFromSnapshot(speaker);
        const isAgent = draft.speaker_type === "agent";
        return (
          <div className="ops-modal-backdrop" role="presentation" onMouseDown={() => setSpeakerEditTarget(null)}>
            <form className="ops-modal speaker-edit-modal" onMouseDown={(event) => event.stopPropagation()}
              onSubmit={(event) => { event.preventDefault(); setSpeakerEditTarget(null); void saveSpeaker(speaker); }}>
              <div className="ops-modal-head">
                <div>
                  <span>辩手管理</span>
                  <strong>编辑席位 · {sideLabel(speaker.side)}{seatLabel(speaker.seat)}</strong>
                </div>
                <button type="button" aria-label="关闭" onClick={() => setSpeakerEditTarget(null)}><X size={18} /></button>
              </div>
              <div className="ops-modal-form">
                <div className="ops-form-row-2">
                  <label><span>显示名称</span><input value={draft.name} onChange={(event) => updateSpeakerField(speaker, "name", event.target.value)} autoFocus /></label>
                  <label><span>身份类型</span><select value={draft.speaker_type} onChange={(event) => updateSpeakerType(speaker, event.target.value as SpeakerType)}><option value="human">人类选手</option><option value="agent">Agent 辩手</option></select></label>
                </div>
                {isAgent && (
                  <>
                    <label className="ops-form-row-full"><span>绑定 Agent 配置</span>
                      <select value={draft.agent_config_id} onChange={(event) => updateSpeakerField(speaker, "agent_config_id", event.target.value)}>
                        <option value="">请选择已有 Agent 配置</option>
                        {agentConfigs.map((config) => (
                          <option value={config.id} key={config.id}>{config.name} · {config.model_name}{config.enabled ? "" : "（停用）"}</option>
                        ))}
                      </select>
                    </label>
                    {draft.agent_config_id && agentConfigById.get(draft.agent_config_id) && (
                      <div className="ops-modal-info-box">
                        <strong>{agentConfigById.get(draft.agent_config_id)?.name}</strong>
                        <span>{agentConfigById.get(draft.agent_config_id)?.model_name} · {agentConfigById.get(draft.agent_config_id)?.provider_type === "openai_sdk" ? "OpenAI SDK" : "REST API"}</span>
                        <code>{agentConfigById.get(draft.agent_config_id)?.endpoint || agentConfigById.get(draft.agent_config_id)?.base_url || "未配置请求地址"}</code>
                      </div>
                    )}
                  </>
                )}
              </div>
              <div className="ops-modal-actions">
                <button type="button" onClick={() => setSpeakerEditTarget(null)}>取消</button>
                <button {...busyProps(`save-speaker-${speaker.id}`)} type="submit" className="primary">保存席位</button>
              </div>
            </form>
          </div>
        );
      })()}

      {agentEditTarget && (() => {
        const isNew = agentEditTarget === "new";
        const config = isNew ? null : (agentEditTarget as AgentConfig);
        const draft = isNew ? newAgentDraft : (agentConfigDrafts[config!.id] ?? agentConfigDraftFromSnapshot(config!));
        const updateField = isNew
          ? (field: keyof AgentConfigDraft, value: string | number | boolean) => updateNewAgentConfigField(field, value)
          : (field: keyof AgentConfigDraft, value: string | number | boolean) => updateAgentConfigField(config!, field, value);
        return (
          <div className="ops-modal-backdrop" role="presentation" onMouseDown={() => setAgentEditTarget(null)}>
            <form className="ops-modal agent-edit-modal" onMouseDown={(event) => event.stopPropagation()}
              onSubmit={(event) => {
                event.preventDefault();
                setAgentEditTarget(null);
                if (isNew) void createAgentConfig();
                else void saveAgentConfig(config!);
              }}>
              <div className="ops-modal-head">
                <div>
                  <span>Agent 配置库</span>
                  <strong>{isNew ? "新增 Agent" : `编辑 · ${config!.name}`}</strong>
                </div>
                <button type="button" aria-label="关闭" onClick={() => setAgentEditTarget(null)}><X size={18} /></button>
              </div>
              {isNew && <p className="ops-object-intro">创建可复用 Agent 配置后，再回到"辩手管理"把它绑定到具体 AI 席位。</p>}
              <div className="ops-modal-form ops-agent-config-new">
                <label><span>名称</span><input value={draft.name} placeholder="如 正方二辩 Agent" onChange={(event) => updateField("name", event.target.value)} autoFocus /></label>
                <label><span>请求类型</span><select value={draft.provider_type} onChange={(event) => updateField("provider_type", event.target.value)}><option value="rest_api">REST Agent</option><option value="openai_sdk">OpenAI SDK Agent</option></select></label>
                <label><span>展示名称</span><input value={draft.model_name} placeholder="如 墨辩 Agent / Qwen-Max" onChange={(event) => updateField("model_name", event.target.value)} /></label>
                <label><span>请求模型 ID</span><input value={draft.model_id} placeholder="qwen3.6-plus" onChange={(event) => updateField("model_id", event.target.value)} /></label>
                <label><span>模型类型</span><select value={draft.model_kind} onChange={(event) => updateField("model_kind", event.target.value)}><option value="closed_source">闭源模型</option><option value="open_source">开源模型</option></select></label>
                <label><span>超时 ms</span><input type="number" min={1000} max={120000} step={500} value={draft.timeout_ms} onChange={(event) => updateField("timeout_ms", event.target.valueAsNumber)} /></label>
                {draft.provider_type === "rest_api" ? (
                  <label className="wide"><span>REST Endpoint</span><input value={draft.endpoint} placeholder="http://127.0.0.1:8100" onChange={(event) => updateField("endpoint", event.target.value)} /></label>
                ) : (
                  <>
                    <label><span>Base URL</span><input value={draft.base_url} placeholder="https://api.openai.com/v1" onChange={(event) => updateField("base_url", event.target.value)} /></label>
                    <label><span>Key 环境变量</span><input value={draft.api_key_env} placeholder="OPENAI_API_KEY" onChange={(event) => updateField("api_key_env", event.target.value)} /></label>
                  </>
                )}
                <label className="ops-toggle-line wide"><input type="checkbox" checked={draft.enabled} onChange={(event) => updateField("enabled", event.target.checked)} /><span>启用此配置</span></label>
              </div>
              <div className="ops-modal-actions">
                <button type="button" onClick={() => setAgentEditTarget(null)}>取消</button>
                {isNew
                  ? <button {...busyProps("create-agent-config")} type="submit" className="primary"><Plus size={16} />创建 Agent</button>
                  : <button {...busyProps(`save-agent-config-${config!.id}`)} type="submit" className="primary">保存配置</button>}
              </div>
            </form>
          </div>
        );
      })()}
    </main>
  );
}

function Kpi({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <div className="ops-kpi"><span>{label}</span><strong>{value}</strong><em>{detail}</em></div>;
}

function ExportBundlePreview({ bundle, freshness }: { bundle: ExportPreviewBundle; freshness: string }) {
  const entries = bundle.entries ?? [];
  const coverage = exportCoverage(entries);
  const visibleEntries = entries.slice(0, 14);

  return (
    <div className="export-preview">
      <div className="export-box">
        <strong>{bundle.export_id}</strong>
        <span>{formatBytes(bundle.size_bytes)} · {bundle.entry_count} 个条目 · {freshness}</span>
        <a href={withCurrentAuthQuery(bundle.download_url)} target="_blank" rel="noreferrer">下载 ZIP</a>
      </div>
      <div className="export-coverage">
        <h3>导出内容覆盖</h3>
        <div className="export-coverage-grid">
          {coverage.map((item) => (
            <div className={`export-coverage-item ${item.status}`} key={item.label}>
              <strong>{item.label}</strong>
              <StatusPill tone={item.status === "ready" ? "green" : item.status === "optional" ? "muted" : "red"}>{item.statusText}</StatusPill>
              <span>{item.detail}</span>
            </div>
          ))}
        </div>
      </div>
      <div className="export-entry-list">
        <h3>条目清单</h3>
        {visibleEntries.map((entry) => (
          <div className="export-entry-row" key={entry.path}>
            <span>{entry.path}</span>
            <em>{formatBytes(entry.size_bytes)}</em>
          </div>
        ))}
        {entries.length > visibleEntries.length && <p className="muted-line">还有 {entries.length - visibleEntries.length} 个条目未展开，下载 ZIP 可查看全部文件。</p>}
        {!entries.length && <p className="muted-line">摘要中暂无条目清单，请重新生成导出包。</p>}
      </div>
    </div>
  );
}

function ReplayHealthPanel({ summary, onSelect }: { summary: DataSummary; onSelect: (selection: ReplaySelection) => void }) {
  const health = summary.request_health;
  const hasFailures = health.failed_agent_requests.length > 0 || health.failed_speech_service_requests.length > 0;

  return (
    <div className="ops-card span-2 replay-panel">
      <div className="ops-card-head">
        <span><AlertTriangle size={16} />复盘健康</span>
        <div className="ops-button-row">
          <StatusPill tone={hasFailures ? "red" : "green"}>{hasFailures ? "存在异常" : "暂无异常"}</StatusPill>
          <StatusPill tone="blue">{summary.counts.agent_requests + summary.counts.speech_service_requests} 条请求</StatusPill>
        </div>
      </div>
      <div className="replay-status-grid">
        <div>
          <strong>Agent 请求</strong>
          <span>{statusCountsText(health.agent_status_counts)}</span>
        </div>
        <div>
          <strong>语音请求</strong>
          <span>{statusCountsText(health.speech_service_status_counts)}</span>
        </div>
      </div>
      <div className="replay-columns">
        <div>
          <h3>最近失败</h3>
          <ReplayRequestList
            agentRequests={health.failed_agent_requests}
            speechRequests={health.failed_speech_service_requests}
            emptyText="最近没有失败请求。"
            onSelect={onSelect}
          />
        </div>
        <div>
          <h3>最近请求</h3>
          <ReplayRequestList
            agentRequests={health.recent_agent_requests}
            speechRequests={health.recent_speech_service_requests}
            emptyText="暂无 Agent 或语音请求。"
            onSelect={onSelect}
          />
        </div>
      </div>
    </div>
  );
}

function ReplayRequestList({
  agentRequests,
  speechRequests,
  emptyText,
  onSelect
}: {
  agentRequests: AgentRequestSummary[];
  speechRequests: SpeechServiceRequestSummary[];
  emptyText: string;
  onSelect: (selection: ReplaySelection) => void;
}) {
  const rows = [
    ...agentRequests.map((item) => ({ kind: "agent" as const, started_at: item.started_at, item })),
    ...speechRequests.map((item) => ({ kind: "speech" as const, started_at: item.started_at, item }))
  ].sort((a, b) => new Date(b.started_at).getTime() - new Date(a.started_at).getTime()).slice(0, 5);

  if (!rows.length) return <p className="muted-line">{emptyText}</p>;

  return (
    <div className="replay-list">
      {rows.map((row) => row.kind === "agent"
        ? <AgentReplayRow key={`agent-${row.item.id}`} item={row.item} onSelect={onSelect} />
        : <SpeechReplayRow key={`speech-${row.item.id}`} item={row.item} onSelect={onSelect} />)}
    </div>
  );
}

function AgentReplayRow({ item, onSelect }: { item: AgentRequestSummary; onSelect: (selection: ReplaySelection) => void }) {
  return (
    <button type="button" className={`replay-row ${item.status}`} onClick={() => onSelect({ kind: "agent_request", item })}>
      <div>
        <strong>Agent · {item.speaker_id}</strong>
        <span>{item.task_id} · {formatDateTime(item.started_at)}</span>
        <em>{item.error_message || item.endpoint || "无错误信息"}</em>
      </div>
      <StatusPill tone={requestStatusTone(item.status)}>{requestStatusLabel(item.status)}</StatusPill>
      <small>{formatLatency(item.latency_ms)}</small>
    </button>
  );
}

function SpeechReplayRow({ item, onSelect }: { item: SpeechServiceRequestSummary; onSelect: (selection: ReplaySelection) => void }) {
  return (
    <button type="button" className={`replay-row ${item.status}`} onClick={() => onSelect({ kind: "speech_service_request", item })}>
      <div>
        <strong>{speechServiceLabel(item.service)} · {speechOperationLabel(item.operation)}</strong>
        <span>{item.speaker_id || item.speech_id || item.request_id} · {formatDateTime(item.started_at)}</span>
        <em>{item.error_message || "无错误信息"}</em>
      </div>
      <StatusPill tone={requestStatusTone(item.status)}>{requestStatusLabel(item.status)}</StatusPill>
      <small>{formatLatency(item.latency_ms)}</small>
    </button>
  );
}

function ReplayDetailPanel({
  selection,
  snapshot,
  exportBundle,
  onClose
}: {
  selection: ReplaySelection;
  snapshot: MatchSnapshot;
  exportBundle: DataSummary["latest_export"];
  onClose: () => void;
}) {
  const ids = replaySelectionIds(selection);
  const speaker = ids.speakerId ? snapshot.speakers.find((item) => item.id === ids.speakerId) : undefined;
  const transcript = ids.speechId ? snapshot.recent_transcript.find((item) => item.speech_id === ids.speechId) : undefined;
  const audioAssets = ids.speechId ? snapshot.audio_assets.filter((item) => item.speech_id === ids.speechId) : [];
  const audioChunks = audioAssets.reduce((sum, item) => sum + (item.chunks?.length ?? item.chunk_count ?? 0), 0);
  const title = replaySelectionTitle(selection);
  const subtitle = replaySelectionSubtitle(selection);
  const fields = replaySelectionFields(selection);

  return (
    <div className="ops-card span-2 replay-detail-card">
      <div className="ops-card-head">
        <span>定位详情</span>
        <button type="button" onClick={onClose}>关闭</button>
      </div>
      <div className="replay-detail-title">
        <strong>{title}</strong>
        <span>{subtitle}</span>
      </div>
      <div className="replay-detail-grid">
        {fields.map((field) => (
          <div key={field.label}>
            <strong>{field.label}</strong>
            <span>{field.value || "无"}</span>
          </div>
        ))}
      </div>
      <div className="replay-location-grid">
        <div>
          <strong>关联辩手</strong>
          <span>{speaker ? `${speakerLabel(speaker)} · ${speaker.speaker_type === "agent" ? speaker.model_name ?? "Agent" : "人类选手"}` : ids.speakerId || "未关联"}</span>
        </div>
        <div>
          <strong>关联发言</strong>
          <span>{ids.speechId || "未关联发言"}</span>
        </div>
        <div>
          <strong>转写定位</strong>
          <span>{transcript ? `${transcript.speaker_label} · ${transcript.is_final ? "定稿" : "临时"} · ${truncateText(transcript.text, 42)}` : "暂无匹配转写"}</span>
        </div>
        <div>
          <strong>音频定位</strong>
          <span>{audioAssets.length ? `${audioAssets.length} 条归档 · ${audioChunks} 个分片 · ${formatBytes(audioAssets.reduce((sum, item) => sum + (item.size_bytes ?? 0), 0))}` : "暂无匹配音频"}</span>
        </div>
        <div>
          <strong>导出包</strong>
          {exportBundle ? <a href={withCurrentAuthQuery(exportBundle.download_url)} target="_blank" rel="noreferrer">{exportBundle.export_id}</a> : <span>暂无导出包</span>}
        </div>
      </div>
    </div>
  );
}

function PreflightSummary({ report }: { report: PreflightReport }) {
  return (
    <div className="preflight-summary">
      <Kpi label="总体状态" value={preflightLabel(report.overall_status)} detail={`${report.score.ok}/${report.score.total} 项就绪`} />
      <p className="muted-line">{report.summary}</p>
      {report.sections.flatMap((section) => section.checks.filter((check) => check.status !== "ok").map((check) => (
        <div className={`preflight-issue ${check.status}`} key={`${section.id}-${check.id}`}>
          <strong>{section.label} · {check.label}</strong>
          <span>{check.detail}</span>
        </div>
      )))}
    </div>
  );
}

function DiagnosticsSummary({ diagnostics }: { diagnostics: SpeechDiagnostics }) {
  return (
    <div className="diagnostics-summary">
      <Kpi label="总体" value={diagnosticsLabel(diagnostics.overall_status)} detail={`服务提供方 ${diagnostics.provider}`} />
      <div className="ops-kpi-row">
        <Kpi label="ASR" value={serviceStatusLabel(diagnostics.asr.status)} detail={diagnostics.asr.detail} />
        <Kpi label="TTS" value={serviceStatusLabel(diagnostics.tts.status)} detail={diagnostics.tts.detail} />
        <Kpi label="音频归档" value={audioAssetStatusLabel(diagnostics.audio_archive.status)} detail={diagnostics.audio_archive.detail} />
      </div>
      <p className="path-line">{diagnostics.audio_archive.root_path}</p>
    </div>
  );
}

function TranscriptList({
  matchId,
  segments,
  reviseSpeech,
  patchAction
}: {
  matchId: string;
  speakers: Speaker[];
  segments: Array<{ id: string; speech_id: string; speaker_label: string; source: string; is_final: boolean; valid: boolean; text: string }>;
  reviseSpeech: (speechId: string, currentText: string) => Promise<void>;
  patchAction: (path: string, body?: Record<string, unknown>, confirmText?: string) => Promise<void>;
}) {
  return (
    <div className="ops-transcript-list">
      {segments.map((segment) => (
        <div key={segment.id} className={segment.valid === false ? "invalid" : ""}>
          <div>
            <strong>{segment.speaker_label}</strong>
            <StatusPill tone={segment.source === "agent_text" ? "blue" : "green"}>{segment.source === "agent_text" ? "AI" : "ASR"}</StatusPill>
            <StatusPill tone={segment.is_final ? "green" : "gold"}>{segment.is_final ? "定稿" : "临时"}</StatusPill>
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
      {!segments.length && <p className="muted-line">暂无转写。</p>}
    </div>
  );
}

function VoteSummary({ voteState, speakers }: { voteState: VoteState; speakers: Speaker[] }) {
  const judge = voteState.judge_summary;
  const audience = voteState.audience_summary;
  return (
    <div className="ops-card">
      <div className="ops-card-head"><span>结果摘要</span></div>
      <Kpi label="评委优胜方" value={sideLabel(judge.winner_side)} detail={voteState.judge_published ? "已公布" : "未公布"} />
      <Kpi label="最佳辩手" value={speakerLabel(speakers.find((speaker) => speaker.id === judge.best_speaker_id))} detail="评委结果" />
      <Kpi label="学生票" value={`${audience.total}`} detail={voteState.audience_published ? "已公布" : "未公布"} />
      <div className="ranking-list">
        {audience.best_speaker.map((item, index) => (
          <div className="ranking-row" key={item.speaker_id}><span>{index + 1}</span><strong>{speakerLabel(speakers.find((speaker) => speaker.id === item.speaker_id))}</strong><em>{item.count} 票</em></div>
        ))}
      </div>
    </div>
  );
}

function emptyMatchDraft(): MatchSettingsDraft {
  return { title: "", topic: "", affirmative_position: "", negative_position: "", organizer: "", venue: "" };
}

function teamDraftFromSnapshot(team: Team): TeamSettingsDraft {
  return { name: team.name, position: team.position, description: team.description };
}

function speakerDraftFromSnapshot(speaker: Speaker): SpeakerSettingsDraft {
  return {
    speaker_type: speaker.speaker_type,
    name: speaker.name,
    agent_config_id: speaker.agent_config_id ?? "",
    model_name: speaker.model_name ?? "",
    model_kind: speaker.model_kind ?? "closed_source",
    agent_endpoint: speaker.agent_endpoint ?? ""
  };
}

function emptyAgentConfigDraft(): AgentConfigDraft {
  return {
    name: "",
    provider_type: "rest_api",
    model_name: "",
    model_id: "qwen3.6-plus",
    model_kind: "closed_source",
    endpoint: "",
    base_url: "",
    api_key_env: "",
    timeout_ms: 30000,
    enabled: true
  };
}

function agentConfigDraftFromSnapshot(config: AgentConfig): AgentConfigDraft {
  return {
    name: config.name,
    provider_type: config.provider_type === "openai_sdk" ? "openai_sdk" : "rest_api",
    model_name: config.model_name ?? "",
    model_id: config.model_id ?? "qwen3.6-plus",
    model_kind: config.model_kind === "open_source" ? "open_source" : "closed_source",
    endpoint: config.endpoint ?? "",
    base_url: config.base_url ?? "",
    api_key_env: config.api_key_env ?? "",
    timeout_ms: config.timeout_ms ?? 30000,
    enabled: config.enabled !== false
  };
}

function phaseDraftFromSnapshot(phase: Phase): PhaseSettingsDraft {
  const sideTotal = phase.side_total_seconds ?? Math.max(1, Math.floor(phase.duration_seconds / 2));
  return { name: phase.name, duration_seconds: phase.duration_seconds, side_total_seconds: sideTotal, turn_seconds: phase.turn_seconds ?? 15 };
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
    constructive: { affirmative: draft.constructive_affirmative, negative: draft.constructive_negative },
    process: { affirmative: draft.process_affirmative, negative: draft.process_negative },
    conclusion: { affirmative: draft.conclusion_affirmative, negative: draft.conclusion_negative },
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

function formatDateTime(value?: string): string {
  if (!value) return "未知时间";
  const time = new Date(value);
  if (Number.isNaN(time.getTime())) return value;
  return time.toLocaleString();
}

const exportCoverageGroups = [
  { label: "比赛快照", paths: ["match.json"] },
  { label: "环节配置", paths: ["phases.json", "phases.csv"] },
  { label: "发言记录", paths: ["speeches.json", "speeches.csv"] },
  { label: "转写文本", paths: ["transcripts.json", "transcripts.csv"] },
  { label: "投票结果", paths: ["votes.json"] },
  { label: "Agent/语音请求", paths: ["agent_requests.jsonl", "speech_service_requests.jsonl"] },
  { label: "事件与审计", paths: ["events.jsonl", "audit_logs.jsonl"] },
  { label: "音频清单", paths: ["audio_manifest.json"] },
  { label: "结构化数据", paths: ["structured/summary.json", "structured/matches.json", "structured/runtime_settings.json"] },
  { label: "音频文件", prefixes: ["audio/"], optional: true }
];

function exportCoverage(entries: Array<{ path: string; size_bytes: number }>): Array<{ label: string; status: "ready" | "missing" | "optional"; statusText: string; detail: string }> {
  const paths = new Set(entries.map((entry) => entry.path));
  return exportCoverageGroups.map((group) => {
    const missingPaths = (group.paths ?? []).filter((path) => !paths.has(path));
    const prefixHits = (group.prefixes ?? []).flatMap((prefix) => entries.filter((entry) => entry.path.startsWith(prefix)).map((entry) => entry.path));
    if (group.optional && !prefixHits.length) {
      return { label: group.label, status: "optional", statusText: "本场暂无", detail: "音频清单仍会记录归档状态" };
    }
    if (!missingPaths.length && (!(group.prefixes?.length) || prefixHits.length)) {
      const examples = [...(group.paths ?? []), ...prefixHits.slice(0, 2)];
      return { label: group.label, status: "ready", statusText: "已包含", detail: examples.join(" · ") };
    }
    const missing = [...missingPaths, ...(group.prefixes?.length && !prefixHits.length ? group.prefixes.map((prefix) => `${prefix}*`) : [])];
    return { label: group.label, status: "missing", statusText: "缺失", detail: missing.join(" · ") };
  });
}

function replaySelectionTitle(selection: ReplaySelection): string {
  if (selection.kind === "agent_request") return `Agent 请求 · ${selection.item.speaker_id}`;
  if (selection.kind === "speech_service_request") return `${speechServiceLabel(selection.item.service)} · ${speechOperationLabel(selection.item.operation)}`;
  if (selection.kind === "event") return `事件 · ${selection.item.type}`;
  return `审计 · ${selection.item.action}`;
}

function replaySelectionSubtitle(selection: ReplaySelection): string {
  if (selection.kind === "agent_request") return `${requestStatusLabel(selection.item.status)} · ${formatDateTime(selection.item.started_at)}`;
  if (selection.kind === "speech_service_request") return `${requestStatusLabel(selection.item.status)} · ${formatDateTime(selection.item.started_at)}`;
  if (selection.kind === "event") return `${actorTypeLabel(selection.item.actor_type)}${selection.item.actor_id ? ` · ${selection.item.actor_id}` : ""} · 序号 ${selection.item.seq}`;
  return `${selection.item.actor_type}${selection.item.actor_id ? ` · ${selection.item.actor_id}` : ""} · ${auditResultLabel(selection.item.result)}`;
}

function replaySelectionIds(selection: ReplaySelection): { speakerId?: string; speechId?: string } {
  if (selection.kind === "agent_request") return { speakerId: selection.item.speaker_id, speechId: selection.item.speech_id ?? undefined };
  if (selection.kind === "speech_service_request") return { speakerId: selection.item.speaker_id ?? undefined, speechId: selection.item.speech_id ?? undefined };
  if (selection.kind === "event") {
    const actor = selection.item.actor_id ?? "";
    return { speakerId: actor.startsWith("spk_") ? actor : undefined };
  }
  const target = selection.item.target_id ?? "";
  const actor = selection.item.actor_id ?? "";
  return {
    speakerId: selection.item.target_type === "speaker" ? target : actor.startsWith("spk_") ? actor : undefined,
    speechId: selection.item.target_type === "speech" || target.startsWith("speech_") ? target : undefined
  };
}

function replaySelectionFields(selection: ReplaySelection): Array<{ label: string; value: string }> {
  if (selection.kind === "agent_request") {
    const item = selection.item;
    return [
      { label: "状态", value: requestStatusLabel(item.status) },
      { label: "任务", value: item.task_id },
      { label: "端点", value: item.endpoint },
      { label: "耗时", value: formatLatency(item.latency_ms) },
      { label: "错误", value: item.error_message || item.error_code || "" }
    ];
  }
  if (selection.kind === "speech_service_request") {
    const item = selection.item;
    return [
      { label: "服务", value: `${speechServiceLabel(item.service)} · ${speechOperationLabel(item.operation)}` },
      { label: "状态", value: requestStatusLabel(item.status) },
      { label: "请求", value: item.request_id },
      { label: "耗时", value: formatLatency(item.latency_ms) },
      { label: "错误", value: item.error_message || item.error_code || "" }
    ];
  }
  if (selection.kind === "event") {
    const item = selection.item;
    return [
      { label: "事件类型", value: item.type },
      { label: "分类", value: eventFilterLabel(eventCategory(item.type)) },
      { label: "序号", value: String(item.seq) },
      { label: "来源", value: item.actor_type },
      { label: "时间", value: formatDateTime(item.created_at) }
    ];
  }
  const item = selection.item;
  return [
    { label: "动作", value: item.action },
    { label: "结果", value: auditResultLabel(item.result) },
    { label: "目标", value: [item.target_type, item.target_id].filter(Boolean).join(" · ") },
    { label: "请求字段", value: Object.keys(item.request ?? {}).join(" · ") },
    { label: "错误", value: item.error_message ?? "" }
  ];
}

function truncateText(value: string, maxLength: number): string {
  if (value.length <= maxLength) return value;
  return `${value.slice(0, Math.max(0, maxLength - 1))}…`;
}

function statusCountsText(counts: Record<string, number>): string {
  const entries = Object.entries(counts).filter(([, count]) => count > 0);
  if (!entries.length) return "暂无请求";
  return entries.map(([status, count]) => `${requestStatusLabel(status)} ${count}`).join(" · ");
}

function eventCountsText(counts?: Record<string, number>): string {
  const total = counts ? Object.values(counts).reduce((sum, count) => sum + count, 0) : 0;
  if (!counts || total === 0) return "暂无";
  const top = Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3)
    .map(([type, count]) => `${type} ${count}`)
    .join(" · ");
  return `${total} 条 · ${top}`;
}

function eventMatchesFilter(type: string, filter: EventFilter): boolean {
  if (filter === "all") return true;
  const category = eventCategory(type);
  return category === filter;
}

function eventCategory(type: string): EventFilter {
  if (type.includes("failed") || type.includes("error") || type.includes("timeout") || type.includes("emergency")) return "error";
  if (type.startsWith("speech.") || type.startsWith("asr.") || type.startsWith("tts.") || type.startsWith("agent.")) return "speech";
  if (type.startsWith("vote.") || type.includes("vote") || type.includes("audience")) return "vote";
  return "control";
}

function eventFilterLabel(filter: EventFilter): string {
  const labels: Record<EventFilter, string> = {
    all: "全部事件",
    control: "流程控制",
    speech: "语音/Agent",
    vote: "投票",
    error: "异常"
  };
  return labels[filter];
}

function auditMatchesFilter(item: AuditLog, filter: AuditFilter): boolean {
  if (filter === "all") return true;
  if (filter === "failed") return item.result !== "success";
  return item.actor_type === filter;
}

function auditFilterLabel(filter: AuditFilter): string {
  const labels: Record<AuditFilter, string> = {
    all: "全部审计",
    failed: "失败/拦截",
    host: "主持操作",
    admin: "后台操作"
  };
  return labels[filter];
}

function auditResultLabel(result: string): string {
  if (result === "success") return "成功";
  if (result === "failed") return "失败";
  return result;
}

function matchStatusLabel(status?: string | null): string {
  if (status === "draft") return "草稿";
  if (status === "ready") return "待开始";
  if (status === "running") return "进行中";
  if (status === "paused") return "已暂停";
  if (status === "intervention") return "应急中";
  if (status === "finished") return "已结束";
  if (status === "archived") return "已归档";
  return status ? `未知状态 ${status}` : "未知状态";
}

function socketStatusLabel(status?: string | null): string {
  if (status === "open") return "已连接";
  if (status === "connecting") return "连接中";
  if (status === "reconnecting") return "重连中";
  if (status === "closed") return "已断开";
  return status ? `未知状态 ${status}` : "未知状态";
}

function liveModeLabel(mode?: string | null): string {
  if (mode === "single") return "单人发言";
  if (mode === "free") return "自由辩论";
  if (mode === "prep") return "AI 准备";
  return mode ? `模式 ${mode}` : "未指定模式";
}

function screenSceneLabel(scene?: string | null): string {
  if (scene === "idle") return "候场";
  if (scene === "live") return "实况";
  if (scene === "paused") return "暂停";
  if (scene === "judge_commentary") return "评委点评";
  if (scene === "judge_result") return "评委结果";
  if (scene === "audience_result") return "学生结果";
  if (scene === "opening") return "实况";
  if (scene === "teams") return "候场";
  if (scene === "intermission") return "评委点评";
  if (scene === "result") return "评委结果";
  return scene ? `场景 ${scene}` : "未指定";
}

function speechSourceLabel(source?: string | null): string {
  if (!source) return "等待指定";
  if (source === "human_asr") return "人类实时转写";
  if (source === "agent_text") return "AI 文本";
  if (source === "manual") return "人工录入";
  return `来源 ${source}`;
}

function serviceStatusLabel(status?: string | null): string {
  if (!status) return "未开始";
  const labels: Record<string, string> = {
    ok: "正常",
    idle: "未开始",
    ready: "就绪",
    pending: "等待中",
    running: "运行中",
    streaming: "流式处理中",
    completed: "已完成",
    failed: "异常",
    cancelled: "已取消",
    disabled: "未启用"
  };
  return labels[status] ?? `状态 ${status}`;
}

function audioAssetStatusLabel(status?: string | null): string {
  if (!status) return "未开始";
  const labels: Record<string, string> = {
    pending: "等待中",
    recording: "录制中",
    completed: "已完成",
    failed: "异常",
    archived: "已归档",
    idle: "未开始",
    ok: "正常"
  };
  return labels[status] ?? serviceStatusLabel(status);
}

function voteWindowStatusLabel(status?: string | null): string {
  if (status === "open") return "开放中";
  if (status === "closed") return "已关闭";
  return status ? `状态 ${status}` : "未知状态";
}

function actorTypeLabel(type?: string | null): string {
  if (type === "admin") return "技术后台";
  if (type === "host") return "主持导播台";
  if (type === "speaker") return "辩手端";
  if (type === "screen") return "大屏";
  if (type === "audience") return "学生投票";
  if (type === "system") return "系统";
  return type || "未知来源";
}

function requestStatusTone(status: string): "green" | "blue" | "red" | "gold" | "muted" {
  if (status === "completed" || status === "ready") return "green";
  if (status === "failed") return "red";
  if (status === "cancelled") return "gold";
  if (status === "running" || status === "streaming") return "blue";
  return "muted";
}

function requestStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    completed: "完成",
    failed: "失败",
    cancelled: "取消",
    ready: "就绪",
    pending: "等待中",
    ok: "正常",
    idle: "未开始",
    running: "进行中",
    streaming: "流式中"
  };
  return labels[status] ?? (status || "未知");
}

function speechServiceLabel(service: string): string {
  if (service.toLowerCase() === "asr") return "ASR";
  if (service.toLowerCase() === "tts") return "TTS";
  return service || "语音";
}

function speechOperationLabel(operation: string): string {
  const labels: Record<string, string> = {
    probe: "自检",
    archive_recognition: "归档识别",
    realtime_stream: "实时转写",
    agent_synthesis: "AI 合成"
  };
  return labels[operation] ?? (operation || "请求");
}

function formatLatency(value?: number | null): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "-- ms";
  return `${Math.max(0, Math.round(value))} ms`;
}

function formatClockShort(clock?: { remaining_ms: number }): string {
  if (!clock) return "--:--";
  const totalSeconds = Math.max(0, Math.ceil(clock.remaining_ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function formatStoragePath(value?: string): string {
  if (!value) return "未配置";
  const parts = value.split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : value;
}

function clockLabel(name: string): string {
  if (name === "affirmative_total") return "正方总时钟";
  if (name === "negative_total") return "反方总时钟";
  if (name === "turn") return "单次时钟";
  return "主时钟";
}

function diagnosticsLabel(status: string): string {
  if (status === "ready") return "真实服务就绪";
  if (status === "mock_fallback") return "模拟降级可用";
  if (status === "failed") return "需要处理";
  return status;
}

function preflightLabel(status: string): string {
  if (status === "ok") return "就绪";
  if (status === "warn") return "待确认";
  if (status === "fail") return "需处理";
  return status;
}

function speakerTypeLabel(type: SpeakerType): string {
  return type === "agent" ? "Agent 辩手" : "人类选手";
}

function agentStatusLabel(status?: string): string {
  if (!status) return "未联调";
  if (status === "ready") return "可用";
  if (status === "streaming") return "生成中";
  if (status === "failed") return "异常";
  if (status === "ok") return "正常";
  if (status === "pending") return "等待中";
  return status;
}

function agentStatusTone(status?: string): "green" | "blue" | "red" | "gold" | "muted" {
  if (status === "ready") return "green";
  if (status === "streaming") return "blue";
  if (status === "failed") return "red";
  if (!status) return "muted";
  return "gold";
}

function actionKey(path: string, body: Record<string, unknown> = {}): string {
  return `${path}:${JSON.stringify(body)}`;
}

function adminActionLabel(path: string, body: Record<string, unknown>): string {
  if (path.includes("/agents/health")) return "检查全部 Agent";
  if (path.includes("/agent/") && path.endsWith("/health")) return "检查 Agent";
  if (path.includes("/agent/") && path.endsWith("/retry")) return "重试 Agent";
  if (path.includes("/agent/") && path.endsWith("/interrupt")) return "中断 Agent";
  if (path.includes("/agent/") && path.endsWith("/manual-input")) return "代输入 Agent 文本";
  if (path.includes("/audience-votes/open")) return "开启学生投票";
  if (path.includes("/audience-votes/close")) return "关闭学生投票";
  if (path.includes("/votes/publish")) return body.scope === "audience" ? "公布学生结果" : "公布评委结果";
  if (path.includes("/screen/scene")) return "切换大屏";
  if (path.includes("/emergency-stop")) return "紧急停止";
  if (path.includes("/speeches/current/stop")) return "强制结束当前发言";
  if (path.includes("/skip")) return "跳过当前环节";
  if (path.includes("/rollback")) return "回滚当前环节";
  if (path.endsWith("/reset")) return "重置比赛";
  if (path.includes("/clocks/") && path.endsWith("/pause")) return "暂停时钟";
  if (path.includes("/clocks/") && path.endsWith("/resume")) return "继续时钟";
  if (path.includes("/clocks/") && path.endsWith("/adjust")) return "校准时钟";
  if (path.includes("/asr/fail")) return "标记 ASR 异常";
  if (path.includes("/tts/fail")) return "TTS 降级";
  if (path.includes("/speeches/")) return body.valid === false ? "作废发言" : body.valid === true ? "恢复发言" : "修订发言";
  return "后台操作";
}

function isDangerousAction(path: string): boolean {
  return path.includes("/emergency-stop") ||
    path.includes("/rollback") ||
    path.includes("/skip") ||
    path.endsWith("/reset") ||
    path.includes("/speeches/current/stop") ||
    path.includes("/interrupt");
}

function randomToken(prefix: string): string {
  const bytes = new Uint8Array(18);
  window.crypto.getRandomValues(bytes);
  const value = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `phd_${prefix}_${value}`;
}
