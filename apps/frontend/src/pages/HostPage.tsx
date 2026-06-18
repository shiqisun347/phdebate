import { Bell, CheckCircle2, Clapperboard, Clock3, FastForward, Mic, Monitor, Pause, Play, RotateCcw, Square, Vote } from "lucide-react";
import { useEffect, useState } from "react";
import { post } from "../api/client";
import { AuthPrompt } from "../components/AuthPrompt";
import { ClockTile } from "../components/ClockTile";
import { useActionFeedback } from "../components/Feedback";
import { StageHistory } from "../components/StageHistory";
import { StatusPill } from "../components/StatusPill";
import { useClockRemaining } from "../hooks/useClockRemaining";
import { useMatch } from "../realtime/useMatch";
import { clockByName, clockStateLabel, seatLabel, sideClass, sideLabel, speakerLabel } from "../state/format";
import type { Clock as MatchClock, FlowState, MatchInfo, MatchSnapshot, Phase, Speaker } from "../types/contracts";

interface HostPageProps {
  matchId: string;
}

export function HostPage({ matchId }: HostPageProps) {
  const { snapshot, socketStatus, lastEvent, loadError, refresh } = useMatch(matchId, "host");
  const [error, setError] = useState<string | null>(null);
  const { busyProps, notify, runAction } = useActionFeedback();

  async function action(path: string, body: Record<string, unknown> = {}, confirmText?: string) {
    try {
      const label = hostActionLabel(path, body);
      await runAction(actionKey(path, body), label, async () => {
        setError(null);
        await postHostAction(path, body);
        await refresh();
      }, {
        confirmText,
        successText: `${label}已完成`
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    }
  }

  function feedbackProps(path: string, body: Record<string, unknown> = {}) {
    return busyProps(actionKey(path, body));
  }

  if (!snapshot && loadError) return <AuthPrompt role="host" message={loadError} />;
  if (!snapshot) return <div className="loading">正在加载主持导播台...</div>;
  if (!snapshot.match.id) return <div className="loading">还没有比赛，请在控制台「比赛管理」新建比赛。</div>;

  const match = snapshot.match;
  const flow = snapshot.flow;
  const phase = snapshot.phases.find((item) => item.id === match.current_phase_id);
  const currentSpeaker = snapshot.speakers.find((item) => item.id === snapshot.current_speech?.speaker_id);
  const currentSpeechPaused = snapshot.current_speech?.state === "paused";
  const primaryClock = match.live_mode === "free" ? clockByName(snapshot.clocks, "turn") : clockByName(snapshot.clocks, "main");
  const visibleClocks = match.live_mode === "free"
    ? snapshot.clocks.filter((clock) => ["affirmative_total", "turn", "negative_total"].includes(clock.name))
    : snapshot.clocks.filter((clock) => clock.name === "main");
  const orderedPhases = [...snapshot.phases].sort((left, right) => left.display_order - right.display_order);
  const nextPhase = orderedPhases.find((item) => item.display_order > (phase?.display_order ?? 0));
  const allowedSpeakers = snapshot.speakers.filter((speaker) => isSpeakerAllowed(speaker, phase, snapshot.free_debate.current_turn_side));
  const controlDisabledReason = hostControlDisabledReason(match);
  const voteControlsLocked = Boolean(controlDisabledReason);
  const voteDisabledReason = controlDisabledReason ? `${controlDisabledReason}继续比赛后才能操作投票与结果。` : undefined;
  const nextStep = buildNextStep({
    matchId,
    snapshot,
    match,
    phase,
    nextPhase,
    primaryClock,
    currentSpeaker,
    currentSpeechPaused,
    allowedSpeakers,
    controlDisabledReason,
    flow
  });
  const startDisabledReason =
    match.status === "running" ? "比赛已经在进行中。" :
    match.status === "paused" ? "比赛已暂停，请使用继续比赛。" :
    match.status === "intervention" ? "应急处理中，请到技术后台处理后再开始。" :
    match.status === "finished" || match.status === "archived" ? "比赛已结束，不能重新开始。请到技术后台重置比赛。" :
    undefined;
  const pauseDisabledReason = match.status !== "running" ? "只有比赛进行中才能暂停。" : undefined;
  const resumeDisabledReason =
    match.status === "paused" ? undefined :
    match.status === "running" ? "比赛已经在进行中。" :
    match.status === "finished" || match.status === "archived" ? "比赛已结束，不能继续。" :
    match.status === "intervention" ? "应急处理中，请到技术后台处理。" :
    "只有比赛暂停时才能继续。";
  const bellDisabledReason = match.status === "intervention" ? "应急处理中，手动铃声暂不可用。" : undefined;
  const stopSpeechDisabledReason = speechActionDisabledReason(match, snapshot, "stop", currentSpeechPaused);
  const pauseSpeechDisabledReason = speechActionDisabledReason(match, snapshot, "pause", currentSpeechPaused);
  const resumeSpeechDisabledReason = speechActionDisabledReason(match, snapshot, "resume", currentSpeechPaused);
  const resetSpeechDisabledReason = speechActionDisabledReason(match, snapshot, "reset", currentSpeechPaused);
  const judgeResultDisabledReason = voteDisabledReason;
  const audienceResultDisabledReason = voteDisabledReason ?? (!snapshot.vote_state.judge_published ? "请先公布评委结果，再展示学生投票结果。" : undefined);
  const finishDisabledReason =
    controlDisabledReason ??
    (!snapshot.vote_state.judge_published ? "请先公布评委结果。" :
    !snapshot.vote_state.audience_published ? "请先公布学生投票结果，再宣布比赛结束。" :
    undefined);

  return (
    <main className="host-shell">
      <header className="host-topbar">
        <div>
          <span className="host-eyebrow">主持导播台</span>
          <h1>{match.title}</h1>
          <p>{match.topic}</p>
        </div>
        <div className="host-status-rail">
          <StatusPill tone={match.status === "running" ? "green" : match.status === "paused" ? "gold" : "muted"}>{matchStatusLabel(match.status)}</StatusPill>
          {flow.awaiting_host_confirm && <StatusPill tone="gold">等待确认</StatusPill>}
          <StatusPill tone={socketStatus === "open" ? "green" : "red"}>WS {socketStatusLabel(socketStatus)}</StatusPill>
          <StatusPill tone="blue">{screenSceneLabel(match.screen_scene)}</StatusPill>
        </div>
      </header>

      {error && <div className="error-banner">{error}</div>}
      {flow.awaiting_host_confirm && (
        <div className="host-flow-alert">
          <strong>{flow.message || "时间到，等待主持确认下一步。"}</strong>
          <span>{flow.next_action === "free_turn_next" ? "确认后系统会按赛制自动授权下一方辩手端。" : flow.next_action === "phase_next" ? "确认后进入下一环节，并自动授权对应辩手端。" : "确认后进入评委点评和投票流程。"}</span>
        </div>
      )}

      <section className="host-now">
        <div className="host-now-main">
          <div>
            <span>当前环节</span>
            <h2>{phase?.name ?? "未指定环节"}</h2>
            <p>
              {match.live_mode === "free"
                ? `自由辩论 · ${sideLabel(snapshot.free_debate.current_turn_side)}第 ${snapshot.free_debate.turn_index} 轮`
                : `${phase?.phase_type ?? "single"} · ${phase?.status ?? "pending"}`}
            </p>
          </div>
          <div className="host-clock-big">
            <HostClockValue clock={primaryClock} />
            <span>{clockStateLabel(primaryClock?.state)}</span>
          </div>
        </div>
        <div className="host-speaker-now">
          <span>当前发言</span>
          <strong>{speakerLabel(currentSpeaker)}</strong>
          <em>{speechSourceLabel(snapshot.current_speech?.source)}</em>
        </div>
        <div className="host-primary-actions">
          <button {...feedbackProps(`/api/matches/${matchId}/resume`, { reason: "host_start" })} onClick={() => action(`/api/matches/${matchId}/resume`, { reason: "host_start" })} disabled={Boolean(startDisabledReason)} title={startDisabledReason}>
            <Play size={16} />开始
          </button>
          <button {...feedbackProps(`/api/matches/${matchId}/pause`)} onClick={() => action(`/api/matches/${matchId}/pause`)} disabled={Boolean(pauseDisabledReason)} title={pauseDisabledReason}>
            <Pause size={16} />暂停
          </button>
          <button {...feedbackProps(`/api/matches/${matchId}/resume`)} onClick={() => action(`/api/matches/${matchId}/resume`)} disabled={Boolean(resumeDisabledReason)} title={resumeDisabledReason}>
            <Play size={16} />继续
          </button>
          <button {...feedbackProps(`/api/matches/${matchId}/bell`, { kind: "manual", label: "主持手动铃" })} disabled={Boolean(bellDisabledReason)} title={bellDisabledReason} onClick={() => action(`/api/matches/${matchId}/bell`, { kind: "manual", label: "主持手动铃" })}>
            <Bell size={16} />手动铃
          </button>
        </div>
      </section>

      <section className="host-layout">
        <aside className="host-flow">
          <div className="section-title"><Clock3 size={16} />流程</div>
          <div className="host-phase-list">
            {orderedPhases.map((item) => (
              <div
                key={item.id}
                className={`host-phase ${item.status} ${item.id === match.current_phase_id ? "current" : ""}`}
              >
                <span>{item.display_order}</span>
                <strong>{item.name}</strong>
                <em>{Math.round(item.duration_seconds / 60)}m</em>
              </div>
            ))}
          </div>
        </aside>

        <section className="host-control-stack">
          <div className="host-panel">
            <div className="section-title"><Mic size={16} />发言权限</div>
            <p className="host-permission-note">主持人只确认流程节奏；本轮可发言人由赛制规则自动授权，辩手端会出现“开始发言”。</p>
            <div className="host-speaker-grid">
              {allowedSpeakers.map((speaker) => (
                <SpeakerPermissionCard key={speaker.id} speaker={speaker} active={speaker.id === currentSpeaker?.id} disabled={Boolean(controlDisabledReason)} />
              ))}
              {!allowedSpeakers.length && <p className="muted-line">当前环节暂无可授权发言人，请检查赛制配置。</p>}
            </div>
            <div className="host-action-row">
              <button
                disabled={Boolean(stopSpeechDisabledReason)}
                title={stopSpeechDisabledReason}
                onClick={() => action(`/api/matches/${matchId}/speeches/current/stop`, { reason: "host_stop" }, "确认结束当前发言？")}
              >
                <Square size={16} />结束当前发言
              </button>
              <button
                disabled={Boolean(pauseSpeechDisabledReason)}
                title={pauseSpeechDisabledReason}
                onClick={() => currentSpeaker && action(`/api/matches/${matchId}/speakers/${currentSpeaker.id}/pause-speaking`, { reason: "host_pause" })}
              >
                <Pause size={16} />暂停发言
              </button>
              <button
                disabled={Boolean(resumeSpeechDisabledReason)}
                title={resumeSpeechDisabledReason}
                onClick={() => currentSpeaker && action(`/api/matches/${matchId}/speakers/${currentSpeaker.id}/resume-speaking`, { reason: "host_resume" })}
              >
                <Play size={16} />继续发言
              </button>
              <button
                disabled={Boolean(resetSpeechDisabledReason)}
                title={resetSpeechDisabledReason}
                onClick={() => action(`/api/matches/${matchId}/speeches/current/reset`, { reason: "host_reset" }, "确认重置当前发言并清空临时字幕？")}
              >
                <RotateCcw size={16} />重置当前发言
              </button>
            </div>
          </div>

          <div className="host-panel">
            <div className="section-title"><Clock3 size={16} />计时</div>
            <div className="host-clock-row">
              {visibleClocks.map((clock) => (
                <ClockTile key={clock.name} label={clockLabel(clock)} clock={clock} compact />
              ))}
            </div>
          </div>

          <div className="host-panel">
            <div className="section-title"><Clapperboard size={16} />现场显示</div>
            <div className="host-segmented">
              <button className={match.screen_scene === "idle" ? "active" : ""} onClick={() => action(`/api/matches/${matchId}/screen/scene`, { scene: "idle" })}>候场</button>
              <button className={match.screen_scene === "live" ? "active" : ""} onClick={() => action(`/api/matches/${matchId}/screen/scene`, { scene: "live", live_mode: match.live_mode })}>实况</button>
              <button className={match.screen_scene === "paused" ? "active" : ""} onClick={() => action(`/api/matches/${matchId}/screen/scene`, { scene: "paused" })}>暂停</button>
            </div>
          </div>
        </section>

        <aside className="host-side">
          <div className="host-panel host-next-panel">
            <div className="section-title"><FastForward size={16} />下一步建议</div>
            <div className="host-next-card">
              <span>{nextStep.kicker}</span>
              <strong>{nextStep.title}</strong>
              <p>{nextStep.description}</p>
              {nextStep.path ? (
                <button
                  {...feedbackProps(nextStep.path, nextStep.body)}
                  className="primary"
                  disabled={Boolean(nextStep.disabledReason)}
                  title={nextStep.disabledReason}
                  onClick={() => nextStep.path && action(nextStep.path, nextStep.body, nextStep.confirmText)}
                >
                  <FastForward size={16} />{nextStep.buttonLabel}
                </button>
              ) : (
                <button disabled title={nextStep.disabledReason || "请在左侧完成当前步骤。"}>
                  <FastForward size={16} />{nextStep.buttonLabel}
                </button>
              )}
              {nextStep.disabledReason && <em>{nextStep.disabledReason}</em>}
            </div>
          </div>

          <div className="host-panel">
            <div className="section-title"><Vote size={16} />赛后流程</div>
            <div className="host-metric">学生投票 <strong>{voteWindowStatusLabel(snapshot.vote_state.window_status)} · {snapshot.vote_state.audience_count} 票</strong></div>
            <div className="host-button-stack">
              <button className={match.screen_scene === "judge_commentary" ? "active" : ""} disabled={voteControlsLocked} title={voteDisabledReason} onClick={() => action(`/api/matches/${matchId}/screen/scene`, { scene: "judge_commentary" }, "切到评委点评并开启学生投票？")}><Monitor size={16} />评委点评并投票</button>
              <button className={match.screen_scene === "judge_result" ? "active" : ""} disabled={Boolean(judgeResultDisabledReason)} title={judgeResultDisabledReason} onClick={() => action(`/api/matches/${matchId}/votes/publish`, { scope: "judge" }, "确认公布评委结果并关闭学生投票？")}><CheckCircle2 size={16} />公布评委结果</button>
              <button className={match.screen_scene === "audience_result" ? "active" : ""} disabled={Boolean(audienceResultDisabledReason)} title={audienceResultDisabledReason} onClick={() => action(`/api/matches/${matchId}/votes/publish`, { scope: "audience" }, "确认公布学生投票结果？")}>公布学生结果</button>
              <button className={`finish ${match.status === "finished" ? "active" : ""}`} disabled={Boolean(finishDisabledReason)} title={finishDisabledReason} onClick={() => action(`/api/matches/${matchId}/finish`, {}, "确认宣布本场比赛结束？结束后主持台将锁定正常流程。")}><CheckCircle2 size={16} />宣布比赛结束</button>
            </div>
            <p className="host-flow-note">按顺序执行：评委点评会开启学生投票，公布评委结果会关闭学生投票，展示学生投票结果后宣布比赛结束。</p>
          </div>

          <div className="host-panel">
            <div className="section-title"><Clock3 size={16} />阶段发言记录</div>
            <div className="host-transcript">
              <StageHistory snapshot={snapshot} />
            </div>
          </div>
        </aside>
      </section>
    </main>
  );
}

function HostClockValue({ clock }: { clock?: MatchClock }) {
  const remaining = useClockRemaining(clock);
  return <strong>{formatClockShort(remaining)}</strong>;
}

function SpeakerPermissionCard({
  speaker,
  active,
  disabled
}: {
  speaker: Speaker;
  active: boolean;
  disabled?: boolean;
}) {
  return (
    <div className={`host-speaker ${sideClass(speaker.side)} ${active ? "active" : ""} ${disabled ? "muted" : ""}`}>
      <strong>{speaker.name}</strong>
      <span>{sideLabel(speaker.side)} · {seatLabel(speaker.seat)} · {speaker.speaker_type === "agent" ? "Agent 辩手" : "人类选手"}</span>
      <em>{active ? "正在发言" : disabled ? "比赛暂停，授权暂不可用" : "已自动授权"}</em>
    </div>
  );
}

async function postHostAction(path: string, body: Record<string, unknown>) {
  try {
    await post(path, body);
  } catch (err) {
    if (!isTransientControlError(err)) throw err;
    await new Promise((resolve) => window.setTimeout(resolve, 600));
    await post(path, body);
  }
}

function isTransientControlError(err: unknown): boolean {
  const message = err instanceof Error ? err.message : String(err);
  return /502|503|Bad Gateway|Failed to fetch|接口返回格式异常|Load failed/i.test(message);
}

interface NextStep {
  kicker: string;
  title: string;
  description: string;
  buttonLabel: string;
  path?: string;
  body: Record<string, unknown>;
  confirmText?: string;
  disabledReason?: string;
}

function buildNextStep({
  matchId,
  snapshot,
  match,
  phase,
  nextPhase,
  primaryClock,
  currentSpeaker,
  currentSpeechPaused,
  allowedSpeakers,
  controlDisabledReason,
  flow
}: {
  matchId: string;
  snapshot: MatchSnapshot;
  match: MatchInfo;
  phase?: Phase;
  nextPhase?: Phase;
  primaryClock?: MatchClock;
  currentSpeaker?: Speaker;
  currentSpeechPaused: boolean;
  allowedSpeakers: Speaker[];
  controlDisabledReason?: string;
  flow: FlowState;
}): NextStep {
  const body: Record<string, unknown> = {};
  if (match.status === "intervention") {
    return {
      kicker: "应急",
      title: "应急处理中",
      description: "主持台已锁定正常流程操作，请到技术后台完成应急处理后再继续。",
      buttonLabel: "等待处理",
      body,
      disabledReason: "应急处理中，主持台不可推进流程。"
    };
  }
  if (match.status === "paused") {
    return {
      kicker: "暂停",
      title: "比赛已暂停",
      description: "大屏、辩手端和投票都处于暂停态。确认现场准备好后继续比赛。",
      buttonLabel: "继续比赛",
      path: `/api/matches/${matchId}/resume`,
      body
    };
  }
  if (match.status !== "running") {
    const finished = match.status === "finished" || match.status === "archived";
    return {
      kicker: finished ? "结束" : "候场",
      title: finished ? "比赛已结束" : "准备开始比赛",
      description: finished ? "如需重置或归档，请到技术后台处理。" : "确认大屏、辩手端、语音链路准备完成后开始比赛。",
      buttonLabel: finished ? "流程已完成" : "开始比赛",
      path: finished ? undefined : `/api/matches/${matchId}/resume`,
      body: finished ? body : { reason: "host_start" },
      disabledReason: finished ? "比赛已结束，主持台不再推进。" : undefined
    };
  }
  if (flow.awaiting_host_confirm) {
    if (flow.next_action === "free_turn_next") {
      return {
        kicker: "时间到",
        title: "等待主持确认下一轮",
        description: flow.message || "系统已结束当前发言并切换轮次。确认后会自动授权下一方辩手端。",
        buttonLabel: "确认下一轮",
        path: `/api/matches/${matchId}/flow/confirm`,
        body: { reason: "host_confirm_next_turn" },
        disabledReason: controlDisabledReason
      };
    }
    if (flow.next_action === "judge_commentary") {
      return {
        kicker: "赛后",
        title: "等待主持进入评委点评",
        description: flow.message || "全部发言流程已结束。确认现场节奏后进入评委点评并开启学生投票。",
        buttonLabel: "评委点评并投票",
        path: `/api/matches/${matchId}/screen/scene`,
        body: { scene: "judge_commentary" },
        confirmText: "切到评委点评并开启学生投票？",
        disabledReason: controlDisabledReason
      };
    }
    return {
      kicker: "时间到",
      title: "等待主持确认下一环节",
      description: flow.message || "系统已结束当前发言并锁定计时。确认现场节奏后进入下一环节。",
      buttonLabel: "进入下一环节",
      path: `/api/matches/${matchId}/phases/next`,
      body,
      confirmText: "确认进入下一环节？",
      disabledReason: controlDisabledReason
    };
  }
  if (snapshot.current_speech && currentSpeaker) {
    if (currentSpeechPaused) {
      return {
        kicker: "发言暂停",
        title: `${speakerLabel(currentSpeaker)} 发言已暂停`,
        description: "如现场确认继续，由主持人恢复该次发言；如需作废，可使用重置当前发言。",
        buttonLabel: "继续当前发言",
        path: `/api/matches/${matchId}/speakers/${currentSpeaker.id}/resume-speaking`,
        body: { reason: "host_next_resume" },
        disabledReason: controlDisabledReason
      };
    }
    return {
      kicker: "发言中",
      title: `${speakerLabel(currentSpeaker)} 正在发言`,
      description: "等待发言完成或倒计时到 0。需要人工截断时，可结束当前发言。",
      buttonLabel: "结束当前发言",
      path: `/api/matches/${matchId}/speeches/current/stop`,
      body: { reason: "host_next_stop" },
      confirmText: "确认结束当前发言？",
      disabledReason: controlDisabledReason
    };
  }
  if (primaryClock?.state === "expired") {
    if (nextPhase) {
      return {
        kicker: "时间到",
        title: "等待主持确认下一环节",
        description: "系统已结束当前发言并锁定计时。确认现场节奏后进入下一环节。",
        buttonLabel: "进入下一环节",
        path: `/api/matches/${matchId}/phases/next`,
        body,
        confirmText: "确认进入下一环节？",
        disabledReason: controlDisabledReason
      };
    }
    return {
      kicker: "赛后",
      title: "发言流程已结束",
      description: "下一步进入评委点评，并同步开启学生投票。",
      buttonLabel: "评委点评并投票",
      path: `/api/matches/${matchId}/screen/scene`,
      body: { scene: "judge_commentary" },
      confirmText: "切到评委点评并开启学生投票？",
      disabledReason: controlDisabledReason
    };
  }
  if (phase?.phase_type === "free_debate") {
    return {
      kicker: "自由辩论",
      title: `已授权${sideLabel(snapshot.free_debate.current_turn_side)}发言`,
      description: "本方符合规则的辩手端已获得开始发言权限。主持人无需选择发言人，只需等待辩手开始或口头提醒。",
      buttonLabel: "等待辩手开始",
      body,
      disabledReason: "发言人由赛制规则自动授权，无需在主持台选择。"
    };
  }
  if (allowedSpeakers.length === 1) {
    const speaker = allowedSpeakers[0];
    if (speaker.speaker_type === "agent") {
      return {
        kicker: "待发言",
        title: `已授权 ${speakerLabel(speaker)}`,
        description: "当前环节已按赛制锁定该 AI 席位。权限已下发到对应 AI 辩手端，主持人无需在主持台启动。",
        buttonLabel: "等待 AI 辩手端启动",
        body,
        disabledReason: controlDisabledReason || "发言权限已下发到 AI 辩手端。"
      };
    }
    return {
      kicker: "待发言",
      title: `已授权 ${speakerLabel(speaker)}`,
      description: "当前环节已按赛制锁定该席位。辩手端已获得开始发言权限，主持人无需在主持台启动。",
      buttonLabel: "等待辩手开始",
      body,
      disabledReason: controlDisabledReason || "发言权限已下发到辩手端。"
    };
  }
  return {
    kicker: "待发言",
    title: "已自动授权可发言席位",
    description: "当前环节允许多个候选辩手，系统已按规则向符合条件的辩手端开放开始发言。",
    buttonLabel: "等待辩手开始",
    body,
    disabledReason: "发言权限已下发到辩手端。"
  };
}

function hostControlDisabledReason(match: MatchInfo): string | undefined {
  if (match.status === "paused") return "比赛暂停中，";
  if (match.status === "intervention") return "应急处理中，";
  if (match.status === "finished" || match.status === "archived") return "比赛已结束，";
  return undefined;
}

function speechActionDisabledReason(match: MatchInfo, snapshot: MatchSnapshot, actionName: "stop" | "pause" | "resume" | "reset", currentSpeechPaused: boolean): string | undefined {
  const controlReason = hostControlDisabledReason(match);
  if (controlReason) return `${controlReason}当前发言控制不可用。`;
  if (!snapshot.current_speech) return "当前没有正在进行的发言。";
  if (actionName === "pause" && currentSpeechPaused) return "当前发言已经暂停。";
  if (actionName === "resume" && !currentSpeechPaused) return "当前发言未暂停。";
  return undefined;
}

function isSpeakerAllowed(speaker: Speaker, phase: Phase | undefined, freeTurnSide: Speaker["side"]): boolean {
  if (!phase) return true;
  if (phase.phase_type === "free_debate") return speaker.side === freeTurnSide;
  if (phase.side !== "neutral" && speaker.side !== phase.side) return false;
  if (phase.speaker_seat !== null && speaker.seat !== phase.speaker_seat) return false;
  return true;
}

function clockLabel(clock: MatchClock): string {
  if (clock.name === "affirmative_total") return "正方总时钟";
  if (clock.name === "negative_total") return "反方总时钟";
  if (clock.name === "turn") return "单次时钟";
  return "主时钟";
}

function formatClockShort(remainingMs?: number): string {
  if (remainingMs === undefined) return "--:--";
  const totalSeconds = Math.max(0, Math.ceil(remainingMs / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function actionKey(path: string, body: Record<string, unknown> = {}): string {
  return `${path}:${JSON.stringify(body)}`;
}

function hostActionLabel(path: string, body: Record<string, unknown>): string {
  if (path.endsWith("/start")) return "开始比赛";
  if (path.endsWith("/begin")) return "开始比赛";
  if (path.endsWith("/pause")) return "暂停比赛";
  if (path.endsWith("/resume")) return body.reason === "host_start" ? "开始比赛" : "继续比赛";
  if (path.endsWith("/finish")) return "宣布比赛结束";
  if (path.endsWith("/phases/next")) return "进入下一环节";
  if (path.endsWith("/flow/confirm")) return "确认下一步";
  if (path.endsWith("/bell")) return "手动铃";
  if (path.includes("/speeches/current/stop")) return "结束当前发言";
  if (path.includes("/speeches/current/reset")) return "重置当前发言";
  if (path.includes("/speakers/") && path.endsWith("/activate")) return "切换发言人";
  if (path.includes("/speakers/") && path.endsWith("/start-speaking")) return "开始发言";
  if (path.includes("/speakers/") && path.endsWith("/pause-speaking")) return "暂停发言";
  if (path.includes("/speakers/") && path.endsWith("/resume-speaking")) return "继续发言";
  if (path.includes("/agent/") && path.endsWith("/retry")) return "请求 AI 发言";
  if (path.includes("/screen/scene")) {
    const scene = typeof body.scene === "string" ? body.scene : "大屏";
    return `切换${screenSceneLabel(scene)}`;
  }
  if (path.includes("/audience-votes/open")) return "开启学生投票";
  if (path.includes("/audience-votes/close")) return "关闭学生投票";
  if (path.includes("/votes/publish")) return body.scope === "audience" ? "公布学生结果" : "公布评委结果";
  return "现场操作";
}

function screenSceneLabel(scene: string): string {
  if (scene === "idle") return "候场页";
  if (scene === "live") return "实况页";
  if (scene === "paused") return "暂停页";
  if (scene === "judge_commentary") return "评委点评页";
  if (scene === "judge_result") return "评委结果页";
  if (scene === "audience_result") return "学生结果页";
  if (scene === "teams") return "候场页";
  if (scene === "intermission") return "评委点评页";
  if (scene === "result") return "评委结果页";
  return "大屏";
}

function matchStatusLabel(status: string): string {
  if (status === "draft") return "草稿";
  if (status === "ready") return "待开始";
  if (status === "running") return "进行中";
  if (status === "paused") return "已暂停";
  if (status === "intervention") return "应急中";
  if (status === "finished") return "已结束";
  if (status === "archived") return "已归档";
  return status;
}

function socketStatusLabel(status: string): string {
  if (status === "open") return "已连接";
  if (status === "connecting") return "连接中";
  if (status === "reconnecting") return "重连中";
  if (status === "closed") return "已断开";
  return status;
}

function voteWindowStatusLabel(status: string): string {
  if (status === "open") return "开放";
  if (status === "closed") return "关闭";
  return status;
}

function speechSourceLabel(source?: string): string {
  if (!source) return "等待指定";
  if (source === "human_asr") return "人类实时转写";
  if (source === "agent_text") return "AI 文本发言";
  if (source === "manual") return "手动录入";
  return source;
}
