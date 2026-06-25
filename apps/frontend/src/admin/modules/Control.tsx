import * as React from "react";
import {
  Play, Pause, SkipForward, RotateCcw, Square, Bot, Sparkles,
  UserCircle2, RefreshCw, Hand, Trophy, Megaphone, Clock, Undo2,
  ArrowRight, Monitor, Vote, ChevronRight, Activity, Radio, VolumeX, Upload, ClipboardCheck,
  Mic, MicOff,
} from "lucide-react";
import { Button, Card, CardContent, CardHeader, CardTitle, CardDescription, Badge, Select, Textarea, Input, Separator, Spinner } from "../ui/primitives";
import { Dialog, DialogHeader, DialogBody, DialogFooter } from "../ui/Dialog";
import { useConfirm } from "../ui/ConfirmDialog";
import { Tabs } from "../ui/Tabs";
import { useToast } from "../lib/toast";
import { useAdminData } from "../lib/data";
import { useAction } from "../lib/actions";
import { post, pushXiaoqiMatchRecord, request } from "../../api/client";
import { SCENE_LABELS, STATUS_LABELS, sideLabel } from "../lib/labels";
import { resolveAvatar } from "../../state/avatar";
import type { MatchSnapshot, Speaker } from "../../types/contracts";

const SEAT_LABELS = ["", "一辩", "二辩", "三辩", "四辩"];
const seatLabel = (seat: number) => SEAT_LABELS[seat] ?? `${seat}号位`;

interface FallbackSpeechItem {
  phase_id: string;
  phase_name?: string;
  speaker_id: string;
  speaker_label: string;
  speaker_name?: string;
  speech_id: string;
  text_loaded: boolean;
  audio_ready: boolean;
  chunk_count: number;
  voice_preset_id?: string | null;
}

interface FallbackFreeItem {
  index: number;
  phase_id: string;
  speaker_id?: string | null;
  speaker_label: string;
  speaker_type?: string | null;
  side?: string | null;
  text_loaded: boolean;
  audio_required: boolean;
  audio_ready: boolean;
  chunk_count: number;
}

interface FallbackStatus {
  history_loaded: boolean;
  history_path: string;
  load_error: string;
  overall_ready: boolean;
  missing_audio_count: number;
  checks: Record<string, boolean>;
  self_intro_items: FallbackSpeechItem[];
  agent_phase_items: FallbackSpeechItem[];
  free_debate_items: FallbackFreeItem[];
}

/* 阶段导航条：6 个页内选项（赛前→比赛过程→观众投票→小七评价→评委点评→结果展示） */
type StageTab = "pre" | "live" | "audience" | "xiaoqi" | "judge" | "result";
const STAGE_TABS: Array<{ value: StageTab; label: string }> = [
  { value: "pre", label: "赛前阶段" },
  { value: "live", label: "比赛过程" },
  { value: "audience", label: "观众投票" },
  { value: "xiaoqi", label: "小七评价" },
  { value: "judge", label: "评委点评" },
  { value: "result", label: "结果展示" },
];

/** 根据当前比赛阶段/场景推导应聚焦的页内选项。 */
function tabForState(s: MatchSnapshot): StageTab {
  const scene = s.match.screen_scene;
  const status = s.match.status;
  if (scene === "audience_vote") return "audience";
  if (scene === "xiaoqi_commentary" || scene === "xiaoqi_result") return "xiaoqi";
  if (scene === "judge_commentary") return "judge";
  if (["judge_result", "audience_result", "acknowledgment"].includes(scene)) return "result";
  if (scene === "debate_process") return "live";
  if (status === "running" || scene === "live") return "live";
  return "pre";
}

interface NextStep {
  label: string;
  enabled: boolean;
  urgent: boolean;
  run?: (base: string) => Promise<unknown>;
}

/** 计算「下一步骤」：动态文案 + 是否需要控场人员操作（urgent → 闪烁）。 */
function computeNextStep(s: MatchSnapshot): NextStep {
  const status = s.match.status;
  if (status === "draft" || status === "ready") {
    return { label: "比赛未开始（点右侧开始比赛）", enabled: false, urgent: false };
  }
  if (status === "finished" || status === "archived") {
    return { label: "比赛已结束", enabled: false, urgent: false };
  }
  if (status === "paused") {
    return { label: "比赛已暂停（点继续恢复）", enabled: false, urgent: false };
  }

  // 赛后流程链：进入观众投票后，"下一步骤"依次推进 观众投票→小七评价→评委点评→结果展示→致谢。
  const scene = s.match.screen_scene;
  if (scene === "audience_vote") {
    return {
      label: "进入小七评价",
      enabled: true,
      urgent: false,
      run: async (base) => {
        await post(`${base}/audience-votes/close`).catch(() => undefined); // 收口投票窗口
        await post(`${base}/screen/scene`, { scene: "xiaoqi_commentary" });
      },
    };
  }
  if (scene === "xiaoqi_commentary" || scene === "xiaoqi_result") {
    return { label: "进入评委点评", enabled: true, urgent: false, run: (base) => post(`${base}/screen/scene`, { scene: "judge_commentary" }) };
  }
  if (scene === "judge_commentary" || scene === "judge_result") {
    return { label: "进入结果展示", enabled: true, urgent: false, run: (base) => post(`${base}/screen/scene`, { scene: "audience_result" }) };
  }
  if (scene === "audience_result") {
    return { label: "进入致谢环节", enabled: true, urgent: false, run: (base) => post(`${base}/screen/scene`, { scene: "acknowledgment" }) };
  }
  if (scene === "acknowledgment") {
    return { label: "比赛流程已完成", enabled: false, urgent: false };
  }

  const flow = s.flow;
  const ordered = [...s.phases].sort((a, b) => a.display_order - b.display_order);
  const idx = ordered.findIndex((p) => p.id === s.match.current_phase_id);
  const nextPhase = idx >= 0 ? ordered[idx + 1] : undefined;

  if (flow.awaiting_host_confirm) {
    if (flow.next_action === "free_turn_next") {
      return {
        label: `下一轮：${sideLabel(s.free_debate.current_turn_side)}发言`,
        enabled: true,
        urgent: true,
        run: (base) => post(`${base}/flow/confirm`, { reason: "host_confirm" }),
      };
    }
    if (flow.next_action === "judge_commentary") {
      return {
        label: "比赛结束 · 进入观众投票",
        enabled: true,
        urgent: true,
        run: async (base) => {
          await post(`${base}/flow/confirm`, { reason: "host_confirm" });
          await post(`${base}/speeches/current/stop`).catch(() => undefined); // 切断当前阶段进行中的请求
          await post(`${base}/audience-votes/open`).catch(() => undefined);
          await post(`${base}/screen/scene`, { scene: "audience_vote" });
        },
      };
    }
    return {
      label: nextPhase ? `进入下一环节：${nextPhase.name}` : "进入下一环节",
      enabled: true,
      urgent: true,
      run: (base) => post(`${base}/phases/next`),
    };
  }

  const speech = s.current_speech;
  if (speech && speech.state !== "ended") {
    return { label: "发言进行中…", enabled: false, urgent: false };
  }
  if (nextPhase) {
    return {
      label: `进入下一环节：${nextPhase.name}`,
      enabled: true,
      urgent: false,
      run: (base) => post(`${base}/phases/next`),
    };
  }
  return {
    label: "全部环节完成 · 进入观众投票",
    enabled: true,
    urgent: true,
    run: async (base) => {
      await post(`${base}/speeches/current/stop`).catch(() => undefined); // 切断当前阶段进行中的请求
      await post(`${base}/audience-votes/open`).catch(() => undefined);
      await post(`${base}/screen/scene`, { scene: "audience_vote" });
    },
  };
}

export function Control() {
  const { snapshot, matchId } = useAdminData();
  const { run, pending } = useAction();
  const { confirm, dialog } = useConfirm();

  const autoTab = snapshot ? tabForState(snapshot) : "pre";
  const [tab, setTab] = React.useState<StageTab>(autoTab);
  const lastAuto = React.useRef(autoTab);
  React.useEffect(() => {
    if (autoTab !== lastAuto.current) {
      lastAuto.current = autoTab;
      setTab(autoTab);
    }
  }, [autoTab]);

  if (!snapshot) {
    return (
      <div className="flex items-center gap-2 py-20 text-muted-foreground">
        <Spinner /> 正在加载控场台…
      </div>
    );
  }

  const m = snapshot.match;
  const base = `/api/matches/${matchId}`;
  const canStart = m.status === "draft" || m.status === "ready";
  const isPaused = m.status === "paused";
  const isRunning = m.status === "running";
  const next = computeNextStep(snapshot);
  const currentHumanSpeaker =
    snapshot.current_speech
      ? snapshot.speakers.find((speaker) => speaker.id === snapshot.current_speech?.speaker_id && speaker.speaker_type === "human")
      : undefined;

  return (
    <div className="space-y-4">
      {/* 1 顶部信息条（压缩，不换行） */}
      <div className="flex items-center gap-2 px-1">
        <Monitor className="size-4 shrink-0 text-primary" />
        <span className="shrink-0 text-sm font-medium text-muted-foreground">当前比赛：</span>
        <span className="truncate text-sm font-semibold text-foreground">{m.title}</span>
      </div>

      {/* 2 比赛实况状态栏 */}
      <LiveStatusBar />
      <HumanSpeakerMonitor />

      {/* 3 流程控制 */}
      <Card>
        <CardContent className="space-y-3 p-4">
          {/* 下一步骤：最高亮度、最显眼；需要操作时闪烁，否则灰色不可点 */}
          <div className="flex flex-wrap items-stretch gap-2">
            <button
              disabled={!next.enabled || pending}
              onClick={() => {
                if (next.run) void run(() => next.run!(base), { success: "已执行下一步" });
              }}
              className={[
                "flex flex-1 items-center justify-center gap-2 rounded-lg px-5 py-3 text-base font-semibold",
                next.enabled && !pending
                  ? "control-next-step bg-primary text-primary-foreground shadow-lg hover:bg-primary hover:text-primary-foreground hover:brightness-110 hover:shadow-xl active:brightness-95"
                  : "cursor-not-allowed bg-muted text-muted-foreground",
                next.urgent && !pending ? "next-step-blink" : "",
              ].join(" ")}
            >
              {pending ? (
                <svg className="size-5 animate-spin" viewBox="0 0 24 24" fill="none"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4l3-3-3-3v4a8 8 0 100 16v-4l-3 3 3 3v-4a8 8 0 01-8-8z" /></svg>
              ) : (
                <ArrowRight className="size-5" />
              )}
              <span className="flex flex-col items-start leading-tight">
                <span className="text-[11px] font-normal opacity-80">下一步骤</span>
                <span>{pending ? "执行中…" : next.label}</span>
              </span>
            </button>

            {/* 开始 / 重置 归到最右侧一组 */}
            <div className="flex items-center gap-2">
              <Button
                variant="success"
                loading={pending}
                disabled={!canStart}
                onClick={() =>
                  run(async () => {
                    await post(`${base}/start`);
                    await post(`${base}/screen/scene`, { scene: "live" });
                  }, { success: "比赛已开始" })
                }
              >
                <Play /> 开始比赛
              </Button>
              <Button
                variant="destructive"
                onClick={async () => {
                  if (await confirm({ title: "重置比赛？", description: "将清空比赛进度，回到初始状态。", confirmText: "确认重置", tone: "destructive" }))
                    void run(() => post(`${base}/reset`, { confirm_text: "重置比赛" }), { success: "比赛已重置" });
                }}
              >
                <Square /> 重置比赛
              </Button>
            </div>
          </div>

          {/* 次级控制 */}
          <div className="flex flex-wrap gap-2">
            {isPaused ? (
              <Button variant="outline" onClick={() => run(() => post(`${base}/resume`), { success: "已继续" })}>
                <Play /> 继续
              </Button>
            ) : (
              <Button variant="outline" disabled={!isRunning} onClick={() => run(() => post(`${base}/pause`), { success: "已暂停" })}>
                <Pause /> 暂停
              </Button>
            )}
            <Button variant="outline" disabled={!isRunning} onClick={() => run(() => post(`${base}/phases/next`), { success: "已进入下一阶段" })}>
              <SkipForward /> 下一阶段
            </Button>
            <Button
              variant="outline"
              onClick={async () => {
                if (await confirm({ title: "回退到上一阶段？", description: "用于撤销误操作。" }))
                  void run(() => post(`${base}/phases/${m.current_phase_id}/rollback`), { success: "已回退上一步" });
              }}
            >
              <Undo2 /> 回退上一步
            </Button>
            <Button
              variant="outline"
              disabled={!currentHumanSpeaker}
              onClick={async () => {
                if (await confirm({ title: "结束当前人工发言？", description: currentHumanSpeaker ? `将结束 ${currentHumanSpeaker.name} 的发言并保存当前转写。` : "当前没有人工辩手在发言。" }))
                  void run(() => post(`${base}/speeches/current/stop`, { reason: "host_end_current_human_speech" }), { success: "已结束当前人工发言" });
              }}
            >
              <Square /> 结束当前人工发言
            </Button>
            <Button
              variant="outline"
              onClick={async () => {
                if (await confirm({ title: "重置当前发言？" }))
                  void run(() => post(`${base}/speeches/current/reset`), { success: "已重置当前发言" });
              }}
            >
              <RotateCcw /> 重置当前发言
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* 4 阶段导航条 */}
      <Tabs value={tab} onValueChange={(v) => setTab(v as StageTab)} items={STAGE_TABS} className="flex w-full" />

      {/* 5 阶段内容区 */}
      {(tab === "pre" || tab === "live") && (
        <div className="space-y-4">
          {tab === "pre" && <PreScreenControl />}
          {tab === "live" && (
            <>
              <LiveScreenControl />
              <TTSRealtimeModule />
            </>
          )}
          <FallbackControlPanel />
          <AgentControlPanel />
        </div>
      )}
      {tab === "audience" && <AudienceVoteStage />}
      {tab === "xiaoqi" && <XiaoqiStage />}
      {tab === "judge" && <JudgeStage />}
      {tab === "result" && <ResultStage />}

      {dialog}
    </div>
  );
}

/* ----------------------------- 2 · 比赛实况状态栏 ----------------------------- */
function LiveStatusBar() {
  const { snapshot } = useAdminData();
  const [now, setNow] = React.useState(Date.now());

  // Tick every 500ms so the countdown updates smoothly without server pushes
  React.useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(id);
  }, []);

  if (!snapshot) return null;
  const m = snapshot.match;
  const phase = snapshot.phases.find((p) => p.id === m.current_phase_id);
  const clock = snapshot.clocks[0];
  const speaker = snapshot.speakers.find((s) => s.id === snapshot.current_speech?.speaker_id);
  const speechState = snapshot.current_speech?.state;

  // Compute remaining seconds from deadline_at so it counts down without server pushes
  const remainingSec = React.useMemo(() => {
    if (!clock) return null;
    if (clock.state === "running" && clock.deadline_at) {
      return Math.max(0, Math.round((new Date(clock.deadline_at as string).getTime() - now) / 1000));
    }
    return Math.max(0, Math.round((clock.remaining_ms ?? 0) / 1000));
  }, [clock, now]);

  return (
    <Card>
      <CardContent className="flex flex-wrap items-center gap-x-6 gap-y-2 p-3">
        <StatusItem label="状态" value={<Badge variant={m.status === "running" ? "success" : m.status === "paused" ? "warning" : "muted"}>{STATUS_LABELS[m.status] ?? m.status}</Badge>} />
        <StatusItem label="大屏" value={<Badge variant="secondary">{SCENE_LABELS[m.screen_scene] ?? m.screen_scene}</Badge>} />
        <StatusItem label="当前环节" value={<span className="text-sm font-semibold text-foreground">{phase?.name ?? "—"}</span>} />
        <StatusItem label="当前发言人" value={<span className="text-sm font-medium text-foreground">{speaker ? `${sideLabel(speaker.side)}${seatLabel(speaker.seat)} · ${speaker.name}` : "—"}</span>} />
        <StatusItem
          label="发言状态"
          value={<span className="text-sm text-muted-foreground">{speechState === "speaking" ? "发言中" : speechState === "paused" ? "已暂停" : "—"}</span>}
        />
        {clock && remainingSec !== null && (
          <div className="ml-auto flex items-center gap-1.5 rounded-md bg-muted px-3 py-1.5">
            <Clock className="size-4 text-primary" />
            <span className={`font-mono text-sm font-semibold ${remainingSec <= 10 && clock.state === "running" ? "text-destructive" : ""}`}>{remainingSec}s</span>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function HumanSpeakerMonitor() {
  const { snapshot, matchId } = useAdminData();
  const { run, pending } = useAction();
  if (!snapshot) return null;
  const base = `/api/matches/${matchId}`;
  const humans = [...snapshot.speakers]
    .filter((speaker) => speaker.speaker_type === "human")
    .sort((a, b) => (a.side === b.side ? a.seat - b.seat : a.side === "affirmative" ? -1 : 1));
  const currentSpeech = snapshot.current_speech;
  const currentHuman = currentSpeech
    ? humans.find((speaker) => speaker.id === currentSpeech.speaker_id)
    : undefined;
  const micErrors = snapshot.speech_service.consoles.mic_errors?.length ?? 0;
  const total = snapshot.speech_service.consoles.total || humans.length;

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <CardTitle className="flex items-center gap-2 text-sm"><Mic className="size-4" /> 人工辩手状态与开麦</CardTitle>
            <CardDescription>赛前看是否进入页面和麦克风状态；比赛中可由控场台帮当前人工辩手开麦或结束。</CardDescription>
          </div>
          <div className="flex flex-wrap gap-2">
            <Badge variant={snapshot.speech_service.consoles.online >= total && total > 0 ? "success" : "warning"}>
              已进入 {snapshot.speech_service.consoles.online}/{total}
            </Badge>
            <Badge variant={micErrors ? "destructive" : "success"}>麦克风异常 {micErrors}</Badge>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {humans.length === 0 ? (
          <p className="py-2 text-sm text-muted-foreground">本场没有人工辩手。</p>
        ) : (
          <div className="grid gap-2 lg:grid-cols-2">
            {humans.map((speaker) => {
              const active = currentHuman?.id === speaker.id;
              const canStart = canHostStartHumanSpeech(snapshot, speaker);
              const micBad = speaker.status === "mic_error" || speaker.mic_permission === "denied";
              const entered = speaker.status === "online" || speaker.status === "mic_error" || Boolean(speaker.last_seen_at);
              return (
                <div key={speaker.id} className={`flex items-center gap-3 rounded-lg border p-2.5 ${active ? "border-success bg-success/5" : "border-border"}`}>
                  <img src={resolveAvatar(speaker)} alt={speaker.name} className="size-10 shrink-0 rounded-md object-cover" />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="truncate text-sm font-medium text-foreground">{speaker.name}</span>
                      <span className="text-xs text-muted-foreground">{sideLabel(speaker.side)}{seatLabel(speaker.seat)}</span>
                      {active && <Badge variant="success">当前发言</Badge>}
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                      <Badge variant={entered ? "success" : "muted"}>{entered ? "已进入页面" : "未进入页面"}</Badge>
                      <Badge variant={micBad ? "destructive" : speaker.mic_permission === "granted" ? "success" : "warning"}>
                        {micBad ? "麦克风异常" : speaker.mic_permission === "granted" ? "麦克风正常" : "麦克风待确认"}
                      </Badge>
                      <span className="truncate">{speaker.device_label || "未上报设备"}</span>
                      <span>{formatSeenAt(speaker.last_seen_at)}</span>
                    </div>
                    {speaker.mic_error_message && <p className="mt-1 truncate text-xs text-destructive">{speaker.mic_error_message}</p>}
                  </div>
                  <div className="flex shrink-0 flex-col gap-1">
                    {active ? (
                      <Button
                        size="sm"
                        variant="outline"
                        loading={pending}
                        onClick={() => run(() => post(`${base}/speeches/current/stop`, { reason: "host_end_current_human_speech" }), { success: "已结束当前人工发言" })}
                      >
                        <Square className="size-3" /> 结束
                      </Button>
                    ) : (
                      <Button
                        size="sm"
                        variant="outline"
                        loading={pending}
                        disabled={!canStart}
                        title={canStart ? "主持人帮该辩手开始当前发言，辩手端会进入录音状态。" : "当前环节暂不允许该辩手开麦。"}
                        onClick={() => run(() => post(`${base}/speakers/${speaker.id}/start-speaking`), { success: `${speaker.name} 已开麦` })}
                      >
                        {micBad ? <MicOff className="size-3" /> : <Mic className="size-3" />} 开麦
                      </Button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function canHostStartHumanSpeech(snapshot: MatchSnapshot, speaker: Speaker): boolean {
  if (snapshot.match.status !== "running" || snapshot.current_speech) return false;
  const phase = snapshot.phases.find((item) => item.id === snapshot.match.current_phase_id);
  if (!phase) return false;
  if (phase.phase_type === "free_debate") {
    return speaker.side === snapshot.free_debate.current_turn_side;
  }
  return phase.side === speaker.side && phase.speaker_seat === speaker.seat;
}

function formatSeenAt(value?: string | null): string {
  if (!value) return "无心跳";
  const ts = new Date(value).getTime();
  if (!Number.isFinite(ts)) return "心跳未知";
  const seconds = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (seconds < 60) return `${seconds}s 前`;
  const minutes = Math.round(seconds / 60);
  return `${minutes}min 前`;
}

function StatusItem({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-xs text-muted-foreground">{label}</span>
      {value}
    </div>
  );
}

/* ----------------------------- 赛前大屏画面 ----------------------------- */
function PreScreenControl() {
  const { matchId } = useAdminData();
  const { run } = useAction();
  const base = `/api/matches/${matchId}`;
  const scenes: Array<{ scene: string; label: string }> = [
    { scene: "idle", label: "候场" },
    { scene: "opening", label: "辩题介绍" },
    { scene: "teams", label: "阵容介绍" },
  ];
  return <ScreenSceneRow title="赛前大屏画面" scenes={scenes} base={base} run={run} />;
}

function LiveScreenControl() {
  const { matchId } = useAdminData();
  const { run } = useAction();
  const base = `/api/matches/${matchId}`;
  const scenes: Array<{ scene: string; label: string }> = [
    { scene: "live", label: "比赛实况" },
    { scene: "debate_process", label: "当前辩论过程" },
  ];
  return <ScreenSceneRow title="比赛过程大屏画面" scenes={scenes} base={base} run={run} />;
}

function FallbackControlPanel() {
  const { snapshot, matchId, refresh } = useAdminData();
  const { run, pending } = useAction();
  const toast = useToast();
  const [status, setStatus] = React.useState<FallbackStatus | null>(null);
  const [phaseId, setPhaseId] = React.useState(snapshot?.match.current_phase_id ?? "");
  const base = `/api/matches/${matchId}`;

  const loadStatus = React.useCallback(async () => {
    try {
      setStatus(await request<FallbackStatus>(`${base}/fallback/status`));
    } catch (err) {
      toast(err instanceof Error ? err.message : "兜底控制加载失败", "error");
    }
  }, [base, toast]);

  React.useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  React.useEffect(() => {
    if (snapshot?.match.current_phase_id && !phaseId) setPhaseId(snapshot.match.current_phase_id);
  }, [phaseId, snapshot?.match.current_phase_id]);

  if (!snapshot) return null;
  const currentPhase = snapshot.phases.find((item) => item.id === snapshot.match.current_phase_id);
  const selectedPhase = snapshot.phases.find((item) => item.id === phaseId) ?? currentPhase;
  const freePhase = snapshot.phases.find((item) => item.phase_type === "free_debate");
  const agentFallbacks = status?.agent_phase_items ?? [];

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm"><ClipboardCheck className="size-4" /> 现场兜底控制</CardTitle>
        <CardDescription>用于切换阶段、播放预设替代音频和启动兜底自由辩论编号流程。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-3 lg:grid-cols-[1.1fr_1fr]">
          <div className="space-y-2 rounded-md border border-border p-3">
            <div className="flex items-center gap-2">
              <Select value={phaseId} onChange={(event) => setPhaseId(event.target.value)} className="h-9">
                {[...snapshot.phases].sort((a, b) => a.display_order - b.display_order).map((phase) => (
                  <option key={phase.id} value={phase.id}>{phase.display_order}. {phase.name}</option>
                ))}
              </Select>
              <Button
                size="sm"
                variant="outline"
                disabled={!selectedPhase || pending}
                onClick={() =>
                  run(async () => {
                    await post(`${base}/fallback/phases/${phaseId}/select`);
                    await refresh();
                  }, { success: `已跳转到 ${selectedPhase?.name ?? "目标阶段"}` })
                }
              >
                <ArrowRight /> 切到阶段开始
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">切换时会停止当前发言、音频和计时；目标阶段之前缺失的真实历史会用兜底历史补齐并标记来源。</p>
          </div>

          <div className="space-y-2 rounded-md border border-border p-3">
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={!freePhase || currentPhase?.phase_type !== "free_debate" || pending}
                onClick={() =>
                  run(async () => {
                    await post(`${base}/fallback/free-debate/start`);
                    await refresh();
                  }, { success: "已启动兜底自由辩论" })
                }
              >
                <Megaphone /> 兜底自由辩论
              </Button>
              <span className="text-xs text-muted-foreground">
                {currentPhase?.phase_type === "free_debate" ? `${status?.free_debate_items?.length ?? 0} 个编号` : "需先进入自由辩论阶段"}
              </span>
            </div>
            <p className="text-xs text-muted-foreground">编号来自固定历史；人类按打印编号发言，AI 编号自动播放预设音频。</p>
          </div>
        </div>

        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-muted-foreground">全部 AI 阶段预设替代</span>
          </div>
          {agentFallbacks.length === 0 ? (
            <p className="rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted-foreground">当前比赛配置没有 AI 固定发言阶段，或无需预设替代音频。</p>
          ) : (
            <div className="grid gap-2 md:grid-cols-2">
              {agentFallbacks.map((item) => (
                <div key={item.speech_id} className="flex items-center gap-2 rounded-md border border-border px-3 py-2">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-foreground">{item.phase_name} · {item.speaker_label}</p>
                    <p className="truncate text-xs text-muted-foreground">
                      {item.audio_ready ? `${item.chunk_count} 段预设音频` : "固定音频不可用"}
                    </p>
                  </div>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={!item.audio_ready || pending}
                    onClick={() =>
                      run(async () => {
                        await post(`${base}/fallback/phases/${item.phase_id}/speakers/${item.speaker_id}/play`);
                        await refresh();
                      }, { success: `已强制切换到 ${item.phase_name} · ${item.speaker_label} 兜底播放` })
                    }
                  >
                    <Play /> 替代
                  </Button>
                </div>
              ))}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function ScreenSceneRow({
  title,
  scenes,
  base,
  run,
  disabledScenes,
}: {
  title: string;
  scenes: Array<{ scene: string; label: string }>;
  base: string;
  run: ReturnType<typeof useAction>["run"];
  /** 场景 → 禁用原因；命中时该场景按钮禁用并提示原因。 */
  disabledScenes?: Record<string, string>;
}) {
  const { snapshot } = useAdminData();
  const current = snapshot?.match.screen_scene;
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm"><Monitor className="size-4" /> {title}</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="flex flex-wrap items-center gap-2">
          {scenes.map((s) => {
            const reason = disabledScenes?.[s.scene];
            return (
              <Button
                key={s.scene}
                size="sm"
                variant={current === s.scene ? "default" : "outline"}
                disabled={!!reason}
                title={reason}
                onClick={() => run(() => post(`${base}/screen/scene`, { scene: s.scene }), { success: `已切换：${s.label}` })}
              >
                {s.label}
              </Button>
            );
          })}
          {disabledScenes && Object.values(disabledScenes)[0] && (
            <span className="text-xs text-muted-foreground">{Object.values(disabledScenes)[0]}</span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

/* ----------------------------- TTS 实时模块 ----------------------------- */
type TtsStats = {
  created: number; streaming: number; ready: number; played: number;
  playingIdx: number; expectedRaw: number; label: string; ended: boolean;
};

function TTSRealtimeModule() {
  const { snapshot, matchId, lastEvent } = useAdminData();
  const { run, pending } = useAction();
  // Keep the last speech's stats so the bar shows the finished state instead of
  // blanking to 0 the instant current_speech clears.
  const lastStatsRef = React.useRef<TtsStats | null>(null);
  if (!snapshot) return null;

  const speech = snapshot.current_speech;
  const speaker = snapshot.speakers.find((item) => item.id === speech?.speaker_id);
  const asset = speech ? snapshot.audio_assets.find((item) => item.speech_id === speech.id) : undefined;
  const chunks = [...(asset?.chunks ?? [])];
  const isLive = Boolean(speech && (speech.source === "agent_text" || speech.tts_task_id));

  let stats: TtsStats;
  if (isLive && speech) {
    stats = {
      created: Number(speech.tts_created_sentences ?? 0),
      streaming: Number(speech.tts_streaming_sentences ?? 0),
      // Archived count: take the larger of materialized chunks and the counter so
      // an early/lagging snapshot never under-reports progress.
      ready: Math.max(chunks.length, Number(speech.tts_ready_sentences ?? 0)),
      played: Number(speech.tts_played_sentences ?? 0),
      playingIdx: Number(speech.tts_playing_sentence_idx ?? -1),
      expectedRaw: Number(speech.tts_expected_sentences ?? 0),
      label: speaker ? `${sideLabel(speaker.side)}${seatLabel(speaker.seat)} · ${speaker.name}` : "—",
      ended: false,
    };
    lastStatsRef.current = { ...stats, ended: true };
  } else {
    stats = lastStatsRef.current ?? {
      created: 0, streaming: 0, ready: 0, played: 0, playingIdx: -1, expectedRaw: 0, label: "—", ended: true,
    };
  }

  const { created, streaming, ready, played, playingIdx, expectedRaw } = stats;
  // Prefer the final sentence count once known; before then use the largest counter
  // so fills stay within the bar.
  const expected = Math.max(expectedRaw > 0 ? expectedRaw : 0, created, streaming, ready, played, 1);
  // 归档(ready) and 播放(played) are independent tracks in live streaming (playback can
  // run ahead of archival), so they are drawn as overlapping fills, not nested.
  const createdPercent = percent(Math.max(created, streaming), expected);
  const generationPercent = percent(ready, expected);
  const playbackPercent = percent(played, expected);
  const currentTaskId = String(speech?.tts_task_id ?? "");
  const currentSpeechId = String(speech?.id ?? "");
  const canControl = Boolean(speech && currentTaskId && speech.state !== "ended");
  // 救援控制（重合成 / 强制跳过）独立于"音频控制"：只要有当前 AI 发言即可用，即便卡顿/状态异常。
  const canRescue = Boolean(speech && currentSpeechId && (speech.source === "agent_text" || currentTaskId));
  // 强制跳过的目标：从"下一句"起第一个既无音频也未跳过的缺口（最可能卡住的那一段）。
  const readySet = new Set(chunks.map((item) => Number(item.chunk_index)));
  const skippedSet = new Set((speech?.tts_skipped_sentences ?? []).map((value) => Number(value)));
  let forceSkipIdx = -1;
  for (let i = Math.max(0, played); i < expected; i += 1) {
    if (!readySet.has(i) && !skippedSet.has(i)) { forceSkipIdx = i; break; }
  }
  const base = `/api/matches/${matchId}`;
  const lastType = lastEvent?.type ?? "—";
  const lastAt = lastEvent ? `#${lastEvent.seq}` : "—";

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm"><Activity className="size-4" /> TTS 实时模块</CardTitle>
        <CardDescription>AI 发言的生成、归档与大屏播放进度。“停止/继续播放”只控制大屏音频，不会结束发言或推进流程。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-2 md:grid-cols-4">
          <TTSMetric label={stats.ended && !isLive ? "上次发言" : "当前发言"} value={stats.label} />
          <TTSMetric label="TTS 状态" value={serviceStatusText(snapshot.speech_service.tts.status)} detail={snapshot.speech_service.tts.detail || "—"} />
          <TTSMetric label="分段" value={`${ready}/${expectedRaw || created || "?"}`} detail={`创建 ${created || 0} · 流式 ${streaming || 0}`} />
          <TTSMetric label="大屏播放" value={played ? `${played}/${expected}` : "未开始"} detail={playingIdx >= 0 ? `当前第 ${playingIdx + 1} 段` : "等待播放"} />
        </div>

        <div className="rounded-lg border border-border bg-muted/30 p-3">
          <div className="relative h-3 rounded-full bg-background">
            <div className="absolute left-0 top-0 h-3 rounded-full bg-primary/20" style={{ width: `${createdPercent}%` }} />
            <div className="absolute left-0 top-0 h-3 rounded-full bg-primary/55" style={{ width: `${generationPercent}%` }} />
            <div className="absolute left-0 top-0 h-3 rounded-full bg-success" style={{ width: `${playbackPercent}%` }} />
            <ProgressPin value={createdPercent} tone="created" title="句段已创建" />
            <ProgressPin value={generationPercent} tone="ready" title="TTS 已归档" />
            <ProgressPin value={playbackPercent} tone="played" title="大屏已播放" />
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
            <span className="inline-flex items-center gap-1"><i className="size-2 rounded-full bg-primary/30" />句段创建</span>
            <span className="inline-flex items-center gap-1"><i className="size-2 rounded-full bg-primary/70" />TTS 归档</span>
            <span className="inline-flex items-center gap-1"><i className="size-2 rounded-full bg-success" />大屏播放</span>
            <span className="ml-auto inline-flex items-center gap-1"><Radio className="size-3" />{lastType} · {lastAt}</span>
          </div>
        </div>

        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={!canControl || pending}
            onClick={() => run(() => post(`${base}/speeches/${currentSpeechId}/tts/playback-resume`, { task_id: currentTaskId }), { success: "已请求大屏继续播放" })}
          >
            <Play /> 继续播放
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!canControl || pending}
            onClick={() => run(() => post(`${base}/speeches/${currentSpeechId}/tts/playback-complete`, { task_id: currentTaskId, reason: "host_force_playback_complete" }), { success: "已标记播放完成" })}
          >
            <SkipForward /> 标记完成
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!canControl || pending}
            onClick={() => run(() => post(`${base}/speeches/${currentSpeechId}/tts/playback-stop`, { task_id: currentTaskId, reason: "host_stop_tts_audio" }), { success: "已截断大屏音频" })}
          >
            <VolumeX /> 停止播放
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!canRescue || pending}
            title="用已生成的文本对当前发言重跑 TTS 合成，并让大屏从头重播"
            onClick={() => run(() => post(`${base}/speeches/${currentSpeechId}/tts/resynthesize`, { reason: "host_resynthesize" }), { success: "已重新合成当前发言的语音" })}
          >
            <RefreshCw /> 重新合成
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!canRescue || forceSkipIdx < 0 || pending}
            title={forceSkipIdx >= 0 ? `强制跳过卡住的第 ${forceSkipIdx + 1} 段` : "当前没有卡住的缺口分段"}
            onClick={() => run(() => post(`${base}/speeches/${currentSpeechId}/tts/skip-sentence`, { sentence_idx: forceSkipIdx, reason: "host_force_skip" }), { success: `已强制跳过第 ${forceSkipIdx + 1} 段` })}
          >
            <ChevronRight /> 强制跳过{forceSkipIdx >= 0 ? `第${forceSkipIdx + 1}段` : ""}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={!speaker || speaker.speaker_type !== "agent" || pending}
            onClick={() => speaker && run(() => post(`${base}/speakers/${speaker.id}/tts/fail`, { reason: "host_tts_degrade", text_only: true }), { success: "已将当前 TTS 降级为文本" })}
          >
            <Megaphone /> TTS 降级
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function TTSMetric({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <div className="rounded-md border border-border bg-card px-3 py-2">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="truncate text-sm font-semibold text-foreground">{value}</p>
      {detail && <p className="mt-0.5 truncate text-xs text-muted-foreground">{detail}</p>}
    </div>
  );
}

function ProgressPin({ value, tone, title }: { value: number; tone: "created" | "ready" | "played"; title: string }) {
  const color = tone === "played" ? "bg-success" : tone === "ready" ? "bg-primary" : "bg-primary/50";
  return <span title={title} className={`absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-card ${color}`} style={{ left: `${value}%` }} />;
}

function percent(value: number, total: number) {
  if (!Number.isFinite(value) || !Number.isFinite(total) || total <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((value / total) * 100)));
}

function serviceStatusText(status: string) {
  if (status === "playing") return "播放中";
  if (status === "synthesizing") return "合成中";
  if (status === "failed") return "失败";
  if (status === "idle") return "空闲";
  return status || "—";
}

/* ----------------------------- AI 辩手控制（重新设计，紧凑） ----------------------------- */
function AgentControlPanel() {
  const { snapshot, matchId } = useAdminData();
  const { run } = useAction();
  const toast = useToast();
  const [fallback, setFallback] = React.useState<Speaker | null>(null);
  const agents = (snapshot?.speakers ?? []).filter((s) => s.speaker_type === "agent");
  const base = `/api/matches/${matchId}`;
  const activeSpeakerId = snapshot?.current_speech?.speaker_id ?? snapshot?.next_speaker?.speaker_id;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm"><Bot className="size-4" /> AI 辩手控制</CardTitle>
        <CardDescription>当前轮到的 AI 辩手高亮显示，可发言 / 重试 / 替代 / 中断。</CardDescription>
      </CardHeader>
      <CardContent>
        {agents.length === 0 && <p className="py-2 text-sm text-muted-foreground">本场没有 AI 辩手。</p>}
        <div className="grid gap-2 sm:grid-cols-2">
          {agents.map((a) => {
            const isTurn = activeSpeakerId === a.id;
            const st = snapshot?.agent_status.find((s) => s.speaker_id === a.id);
            return (
              <div key={a.id} className={`flex items-center gap-3 rounded-lg border p-2.5 ${isTurn ? "border-primary bg-primary/5" : "border-border"}`}>
                <img src={resolveAvatar(a)} alt={a.name} className="size-10 shrink-0 rounded-md object-cover" />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="truncate text-sm font-medium text-foreground">{a.name}</span>
                    <span className="text-xs text-muted-foreground">{sideLabel(a.side)}{seatLabel(a.seat)}</span>
                    {isTurn && <Badge variant="success" className="ml-auto">当前</Badge>}
                  </div>
                  <p className="truncate text-xs text-muted-foreground">{a.model_name || "未绑定"}{st ? ` · ${st.status}` : ""}</p>
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    <Button size="sm" variant="outline" className="h-7 px-2 text-xs" onClick={() => run(() => post(`${base}/speakers/${a.id}/self-introduction`), { success: `${a.name} 开始自我介绍` })} title="赛前自我介绍：会朗读并展示，但不计入后续辩论历史">
                      <Sparkles className="size-3" /> 自我介绍
                    </Button>
                    <Button size="sm" variant="outline" className="h-7 px-2 text-xs" onClick={() => run(() => post(`${base}/speakers/${a.id}/start-agent-speaking`, { force: true, reason: "host_force_start_agent" }), { success: `${a.name} 已强制重新发言` })}>
                      <UserCircle2 className="size-3" /> 发言
                    </Button>
                    <Button size="sm" variant="outline" className="h-7 px-2 text-xs" onClick={() => { if (isTurn) run(() => post(`${base}/agent/${a.id}/retry`), { success: "已重新请求" }); else toast("并非该 agent 回答环节", "info"); }}>
                      <RefreshCw className="size-3" /> 重试
                    </Button>
                    <Button size="sm" variant="outline" className="h-7 px-2 text-xs" onClick={() => setFallback(a)}>
                      <Hand className="size-3" /> 替代
                    </Button>
                    <Button size="sm" variant="ghost" className="h-7 px-2 text-xs text-destructive" onClick={() => run(() => post(`${base}/agent/${a.id}/interrupt`), { success: "已中断" })}>
                      中断
                    </Button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </CardContent>
      {fallback && <FallbackDialog speaker={fallback} onClose={() => setFallback(null)} />}
    </Card>
  );
}

function FallbackDialog({ speaker, onClose }: { speaker: Speaker; onClose: () => void }) {
  const { matchId, refresh } = useAdminData();
  const toast = useToast();
  const [text, setText] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  async function submit() {
    setSaving(true);
    try {
      await post(`/api/matches/${matchId}/agent/${speaker.id}/manual-input`, { content: text, reason: "fallback_single_answer" });
      await refresh();
      toast("已使用替代方案代答", "success");
      onClose();
    } catch (err) {
      toast(err instanceof Error ? err.message : "失败", "error");
    } finally {
      setSaving(false);
    }
  }
  return (
    <Dialog open onClose={onClose} size="md">
      <DialogHeader title={`替代方案 · ${speaker.name}`} description="紧急情况下，由控场人员代该 AI 辩手进行单次回答。" onClose={onClose} />
      <DialogBody>
        <Textarea rows={5} value={text} onChange={(e) => setText(e.target.value)} className="font-sans" placeholder="输入替代发言内容…" />
      </DialogBody>
      <DialogFooter>
        <Button variant="outline" onClick={onClose}>取消</Button>
        <Button onClick={submit} loading={saving} disabled={!text.trim()}>提交代答</Button>
      </DialogFooter>
    </Dialog>
  );
}

/* ----------------------------- 小七评价阶段 ----------------------------- */
function XiaoqiStage() {
  const { matchId, snapshot } = useAdminData();
  const { run } = useAction();
  const base = `/api/matches/${matchId}`;
  const recorded = snapshot?.vote_state?.xiaoqi_recorded;
  return (
    <div className="space-y-4">
      <ScreenSceneRow
        title="小七大屏"
        scenes={[{ scene: "xiaoqi_commentary", label: "小七点评" }, { scene: "xiaoqi_result", label: "小七评判" }]}
        base={base}
        run={run}
        disabledScenes={recorded ? undefined : { xiaoqi_result: "请先完成「小七结果录入」后再切换到小七评判" }}
      />
      <div className="grid gap-4 lg:grid-cols-2">
        <XiaoqiControlPanel />
        <XiaoqiResultEntry />
      </div>
    </div>
  );
}

/* ----------------------------- 小七控制（给小七推送记录 + 返回结果框） ----------------------------- */
function XiaoqiControlPanel() {
  const { matchId } = useAdminData();
  const toast = useToast();
  const [pushing, setPushing] = React.useState(false);
  const [result, setResult] = React.useState<string | null>(null);

  async function pushRecord() {
    setPushing(true);
    setResult(null);
    try {
      const r = await pushXiaoqiMatchRecord(matchId);
      const stages = (r.payload as { match_record?: unknown[] }).match_record?.length ?? 0;
      if (r.sent) {
        toast(`已给小七推送记录（${stages} 个环节）`, "success");
        setResult(`HTTP ${r.status_code} · 响应：${typeof r.response === "string" ? r.response : JSON.stringify(r.response)}`);
      } else {
        toast(`未推送：${r.reason}`, "info");
        setResult(`未推送（${r.reason}）。将发送的请求体：\n${JSON.stringify(r.payload, null, 2)}`);
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : "推送失败", "error");
    } finally {
      setPushing(false);
    }
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm"><Sparkles className="size-4" /> 小七控制</CardTitle>
        <CardDescription>把本场辩论记录推送给小七（点评 / 评判 / 结果显示均由小七自身完成）。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <Button variant="outline" className="w-full justify-start" loading={pushing} onClick={pushRecord}>
          <Upload /> 给小七推送记录<ChevronRight className="ml-auto size-4 opacity-50" />
        </Button>
        <p className="-mt-1 text-xs text-muted-foreground">取当前辩论实况组装比赛记录，推送到「给小七推送接口」。</p>
        {result && <Textarea readOnly rows={5} value={result} className="text-xs font-mono" />}
      </CardContent>
    </Card>
  );
}

/* ----------------------------- 小七结果录入（获胜方 + 最佳辩手，无理由） ----------------------------- */
function XiaoqiResultEntry() {
  const { snapshot, matchId, refresh } = useAdminData();
  const toast = useToast();
  const vs = snapshot?.vote_state;
  const [winner, setWinner] = React.useState<"affirmative" | "negative">((vs?.winner_side as "affirmative" | "negative") || "affirmative");
  const [best, setBest] = React.useState(vs?.best_speaker_id || "");
  const [saving, setSaving] = React.useState(false);
  if (!snapshot) return null;
  const base = `/api/matches/${matchId}`;

  async function save() {
    if (!best) return toast("请选择最佳辩手", "error");
    setSaving(true);
    try {
      await post(`${base}/votes`, { winner_side: winner, best_speaker_id: best, scope: "xiaoqi" });
      await refresh();
      toast("已录入小七评判（获胜方 + 最佳辩手）", "success");
    } catch (err) {
      toast(err instanceof Error ? err.message : "保存失败", "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm"><Trophy className="size-4" /> 小七结果录入</CardTitle>
        <CardDescription>手工录入获胜方与最佳辩手（不含理由），大屏「小七评判」据此展示。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <WinnerBestFields winner={winner} setWinner={setWinner} best={best} setBest={setBest} speakers={snapshot.speakers} />
        <Button onClick={save} loading={saving}>保存评判结果</Button>
      </CardContent>
    </Card>
  );
}

function WinnerBestFields({
  winner, setWinner, best, setBest, speakers,
}: {
  winner: "affirmative" | "negative";
  setWinner: (v: "affirmative" | "negative") => void;
  best: string;
  setBest: (v: string) => void;
  speakers: Speaker[];
}) {
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <div className="space-y-1.5">
        <p className="text-xs text-muted-foreground">获胜方</p>
        <Select value={winner} onChange={(e) => setWinner(e.target.value as "affirmative" | "negative")}>
          <option value="affirmative">正方</option>
          <option value="negative">反方</option>
        </Select>
      </div>
      <div className="space-y-1.5">
        <p className="text-xs text-muted-foreground">最佳辩手</p>
        <Select value={best} onChange={(e) => setBest(e.target.value)}>
          <option value="">请选择…</option>
          {speakers.map((s) => (
            <option key={s.id} value={s.id}>{sideLabel(s.side)}{seatLabel(s.seat)} · {s.name}</option>
          ))}
        </Select>
      </div>
    </div>
  );
}

/* ----------------------------- 观众投票阶段 ----------------------------- */
function AudienceVoteStage() {
  const { matchId } = useAdminData();
  const { run } = useAction();
  const base = `/api/matches/${matchId}`;
  return (
    <div className="space-y-4">
      <ScreenSceneRow title="观众投票大屏" scenes={[{ scene: "audience_vote", label: "投票二维码大屏" }]} base={base} run={run} />
      <div className="grid gap-4 lg:grid-cols-2">
        <AudienceVoteControl />
        <AudienceStats />
      </div>
    </div>
  );
}

/* ----------------------------- 评委点评阶段 ----------------------------- */
function JudgeStage() {
  const { matchId } = useAdminData();
  const { run } = useAction();
  const base = `/api/matches/${matchId}`;
  return (
    <div className="space-y-4">
      <ScreenSceneRow
        title="评委点评大屏"
        scenes={[{ scene: "judge_commentary", label: "评委点评" }, { scene: "judge_result", label: "评委结果" }]}
        base={base}
        run={run}
      />
      <JudgeResultEntry />
    </div>
  );
}

/* ----------------------------- 结果展示阶段（含评委结果 + 观众投票结果） ----------------------------- */
function ResultStage() {
  const { matchId } = useAdminData();
  const { run } = useAction();
  const base = `/api/matches/${matchId}`;
  return (
    <div className="space-y-4">
      <ScreenSceneRow
        title="结果展示大屏"
        scenes={[
          { scene: "judge_result", label: "评委结果" },
          { scene: "audience_result", label: "观众投票结果" },
          { scene: "acknowledgment", label: "致谢环节" },
        ]}
        base={base}
        run={run}
      />
      <div className="grid gap-4 lg:grid-cols-2">
        <JudgeResultEntry />
        <AudienceStats />
      </div>
      <AudienceVoteControl />
    </div>
  );
}

const JUDGE_ASPECTS: Array<{ key: "constructive" | "process" | "conclusion"; label: string }> = [
  { key: "constructive", label: "立论" },
  { key: "process", label: "过程" },
  { key: "conclusion", label: "结辩" },
];

type AspectScores = Record<"constructive" | "process" | "conclusion", { affirmative: number; negative: number }>;

function JudgeResultEntry() {
  const { snapshot, matchId, refresh } = useAdminData();
  const toast = useToast();
  const vs = snapshot?.vote_state;
  const js = vs?.judge_summary;
  const [scores, setScores] = React.useState<AspectScores>(() => ({
    constructive: { affirmative: js?.constructive.affirmative ?? 0, negative: js?.constructive.negative ?? 0 },
    process: { affirmative: js?.process.affirmative ?? 0, negative: js?.process.negative ?? 0 },
    conclusion: { affirmative: js?.conclusion.affirmative ?? 0, negative: js?.conclusion.negative ?? 0 },
  }));
  const [winnerMode, setWinnerMode] = React.useState<"auto" | "affirmative" | "negative">("auto");
  const [best, setBest] = React.useState(vs?.best_speaker_id || "");
  const [saving, setSaving] = React.useState(false);
  if (!snapshot) return null;
  const base = `/api/matches/${matchId}`;

  const totalAff = JUDGE_ASPECTS.reduce((sum, a) => sum + scores[a.key].affirmative, 0);
  const totalNeg = JUDGE_ASPECTS.reduce((sum, a) => sum + scores[a.key].negative, 0);
  const computedWinner: "affirmative" | "negative" = totalAff >= totalNeg ? "affirmative" : "negative";
  const winner = winnerMode === "auto" ? computedWinner : winnerMode;

  function setScore(key: "constructive" | "process" | "conclusion", side: "affirmative" | "negative", value: string) {
    const n = Math.max(0, Math.floor(Number(value) || 0));
    setScores((prev) => ({ ...prev, [key]: { ...prev[key], [side]: n } }));
  }

  async function act(fn: () => Promise<unknown>, msg: string) {
    setSaving(true);
    try { await fn(); await refresh(); toast(msg, "success"); }
    catch (err) { toast(err instanceof Error ? err.message : "操作失败", "error"); }
    finally { setSaving(false); }
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm"><Trophy className="size-4" /> 评委结果录入</CardTitle>
        <CardDescription>按「立论 / 过程 / 结辩」三个环节录入各方票数，胜方默认按总票数自动判定。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="space-y-2 rounded-md border border-border p-3">
          <div className="grid grid-cols-[1fr_5rem_5rem] items-center gap-2 text-xs font-medium text-muted-foreground">
            <span>环节</span>
            <span className="text-center text-sky-400">正方</span>
            <span className="text-center text-rose-400">反方</span>
          </div>
          {JUDGE_ASPECTS.map((a) => (
            <div key={a.key} className="grid grid-cols-[1fr_5rem_5rem] items-center gap-2">
              <span className="text-sm font-medium text-foreground">{a.label}</span>
              <Input
                type="number" min={0} inputMode="numeric" className="h-8 text-center"
                value={scores[a.key].affirmative}
                onChange={(e) => setScore(a.key, "affirmative", e.target.value)}
              />
              <Input
                type="number" min={0} inputMode="numeric" className="h-8 text-center"
                value={scores[a.key].negative}
                onChange={(e) => setScore(a.key, "negative", e.target.value)}
              />
            </div>
          ))}
          <Separator />
          <div className="grid grid-cols-[1fr_5rem_5rem] items-center gap-2">
            <span className="text-sm font-semibold text-foreground">合计</span>
            <span className="text-center text-sm font-semibold text-sky-400">{totalAff}</span>
            <span className="text-center text-sm font-semibold text-rose-400">{totalNeg}</span>
          </div>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <p className="text-xs text-muted-foreground">获胜方</p>
            <Select value={winnerMode} onChange={(e) => setWinnerMode(e.target.value as "auto" | "affirmative" | "negative")}>
              <option value="auto">自动判定（{sideLabel(computedWinner)}）</option>
              <option value="affirmative">正方</option>
              <option value="negative">反方</option>
            </Select>
          </div>
          <div className="space-y-1.5">
            <p className="text-xs text-muted-foreground">最佳辩手</p>
            <Select value={best} onChange={(e) => setBest(e.target.value)}>
              <option value="">请选择…</option>
              {snapshot.speakers.map((s) => (
                <option key={s.id} value={s.id}>{sideLabel(s.side)}{seatLabel(s.seat)} · {s.name}</option>
              ))}
            </Select>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            onClick={() => {
              if (!best) return toast("请选择最佳辩手", "error");
              act(() => post(`${base}/votes`, {
                judge_summary: {
                  constructive: scores.constructive,
                  process: scores.process,
                  conclusion: scores.conclusion,
                  winner_side: winner,
                  best_speaker_id: best,
                },
              }), "已保存评委结果");
            }}
            loading={saving}
          >
            保存结果
          </Button>
          <Button variant="outline" onClick={() => act(() => post(`${base}/votes/publish`, { scope: "judge" }), "已公布评委结果")}>
            <Megaphone /> 公布评委结果
          </Button>
        </div>
        <div className="text-xs text-muted-foreground">
          当前获胜：{vs?.winner_side ? sideLabel(vs.winner_side) : "—"} · 评委已公布：{vs?.judge_published ? "是" : "否"}
        </div>
      </CardContent>
    </Card>
  );
}

function AudienceStats() {
  const { snapshot } = useAdminData();
  const vs = snapshot?.vote_state;
  const audience = vs?.audience_summary;
  if (!vs || !audience) return null;
  const aspectRows = JUDGE_ASPECTS.map((aspect) => ({
    ...aspect,
    value: audience.aspects?.[aspect.key] ?? { affirmative: 0, negative: 0 },
  }));
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm"><Vote className="size-4" /> 观众投票实时统计</CardTitle>
        <CardDescription>观众扫码投票实时汇总，含立论 / 过程 / 结辩三项选择。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="flex items-center justify-between text-sm">
          <span className="text-muted-foreground">投票窗口</span>
          <Badge variant={vs.window_status === "open" ? "success" : "muted"}>{vs.window_status === "open" ? "进行中" : "已关闭"}</Badge>
        </div>
        <div className="flex items-center justify-between text-sm">
          <span className="text-muted-foreground">总票数</span>
          <span className="font-semibold text-foreground">{audience.total}</span>
        </div>
        <div className="grid grid-cols-2 gap-2 pt-1">
          <div className="rounded-md border border-border p-2 text-center">
            <p className="text-xs text-muted-foreground">正方</p>
            <p className="text-lg font-semibold text-foreground">{audience.winner.affirmative}</p>
          </div>
          <div className="rounded-md border border-border p-2 text-center">
            <p className="text-xs text-muted-foreground">反方</p>
            <p className="text-lg font-semibold text-foreground">{audience.winner.negative}</p>
          </div>
        </div>
        <div className="space-y-1.5 rounded-md border border-border p-2">
          <div className="grid grid-cols-[1fr_4rem_4rem] gap-2 text-xs font-medium text-muted-foreground">
            <span>单项票</span>
            <span className="text-center text-sky-400">正方</span>
            <span className="text-center text-rose-400">反方</span>
          </div>
          {aspectRows.map((row) => (
            <div key={row.key} className="grid grid-cols-[1fr_4rem_4rem] items-center gap-2 text-sm">
              <span className="text-foreground">{row.label}</span>
              <span className="text-center font-semibold text-sky-400">{row.value.affirmative}</span>
              <span className="text-center font-semibold text-rose-400">{row.value.negative}</span>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function AudienceVoteControl() {
  const { snapshot, matchId, refresh } = useAdminData();
  const toast = useToast();
  const vs = snapshot?.vote_state;
  const base = `/api/matches/${matchId}`;
  const [busy, setBusy] = React.useState(false);

  async function act(fn: () => Promise<unknown>, msg: string) {
    setBusy(true);
    try { await fn(); await refresh(); toast(msg, "success"); }
    catch (err) { toast(err instanceof Error ? err.message : "操作失败", "error"); }
    finally { setBusy(false); }
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm"><Vote className="size-4" /> 观众投票管理</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant={vs?.window_status === "open" ? "outline" : "success"} loading={busy} disabled={vs?.window_status === "open"} onClick={() => act(() => post(`${base}/audience-votes/open`), "已开始观众投票")}>
          <Vote /> 开始观众投票
        </Button>
        <Button size="sm" variant="outline" loading={busy} disabled={vs?.window_status !== "open"} onClick={() => act(() => post(`${base}/audience-votes/close`), "已结束观众投票")}>
          结束观众投票
        </Button>
        <Button size="sm" variant="outline" onClick={() => act(() => post(`${base}/votes/publish`, { scope: "audience" }), "已公布观众投票结果")}>
          <Megaphone /> 公布观众投票结果
        </Button>
        <Badge variant={vs?.window_status === "open" ? "success" : "muted"} className="ml-auto">
          {vs?.window_status === "open" ? "投票进行中" : "投票未开启"} · {vs?.audience_count ?? 0} 票
        </Badge>
      </CardContent>
    </Card>
  );
}
