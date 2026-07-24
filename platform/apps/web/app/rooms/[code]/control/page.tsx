"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import {
  AlertTriangle,
  Clock3,
  Eye,
  Mic,
  Pause,
  Play,
  RotateCcw,
  ShieldCheck,
  SkipForward,
  Square,
  Wrench,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { LoadError } from "@/components/load-error";
import { apiFetch } from "@/lib/api";
import {
  disconnectGraceRemainingSeconds,
  disconnectGraceTimingLabel,
} from "@/lib/disconnect-grace";
import { matchEventDetail, matchEventLabel } from "@/lib/match-events";
import { roomStatusLabel } from "@/lib/status-labels";
import type { Room } from "@/lib/types";
import { useCountdown, useRoom } from "@/lib/use-room";

import styles from "../room-operations.module.css";

type MatchControlAction =
  | "pause"
  | "safe-pause"
  | "resume"
  | "reset-speech"
  | "retry"
  | "skip"
  | "terminate";
type ConfirmedControlAction = Extract<
  MatchControlAction,
  "safe-pause" | "reset-speech" | "retry" | "skip" | "terminate"
>;

const TERMINAL_ROOM_STATUSES = new Set([
  "completed",
  "review_required",
  "terminated",
  "cancelled",
]);

const CONTROL_REASONS: Record<MatchControlAction, string> = {
  pause: "房主暂停比赛并保留当前进度",
  "safe-pause": "房主紧急暂停异常真人发言并保留剩余时间",
  resume: "房主确认现场就绪并继续比赛",
  "reset-speech": "房主重置完全卡住的当前 AI 发言",
  retry: "房主重置未完成的当前步骤",
  skip: "房主确认跳过当前阶段",
  terminate: "房主确认提前结束比赛",
};

function formatDuration(seconds: number | null) {
  if (seconds === null) return "--:--";
  const safe = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(safe / 60)).padStart(2, "0")}:${String(safe % 60).padStart(2, "0")}`;
}

function failureRetryLabel(room: Room) {
  if (room.status !== "paused" || !room.failure_reason) return "重试当前步骤";
  if (room.current_stage?.kind === "judging") return "重试自动评审";
  if (
    room.current_stage?.kind === "speech" ||
    room.current_stage?.kind === "free" ||
    room.active_speech
  )
    return "重置当前发言";
  return "重试当前步骤";
}

export default function ControlPage() {
  const { code } = useParams<{ code: string }>();
  const router = useRouter();
  const {
    room,
    setRoom,
    connected,
    error: roomError,
    reconnect,
  } = useRoom(code);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [confirmation, setConfirmation] =
    useState<ConfirmedControlAction | null>(null);
  const controlAttempts = useRef<Record<string, string>>({});
  const controlInFlight = useRef("");
  const confirmationCancel = useRef<HTMLButtonElement | null>(null);
  const confirmationDialog = useRef<HTMLDivElement | null>(null);
  const confirmationOrigin = useRef<HTMLElement | null>(null);
  const remaining = useCountdown(
    room?.remaining_seconds ?? null,
    room?.seq ?? 0,
    room?.status === "running" || room?.status === "judging",
  );
  const disconnectGraceRemaining = useCountdown(
    disconnectGraceRemainingSeconds(room),
    room?.seq ?? 0,
    Boolean(
      room?.disconnect_grace?.will_pause &&
        ["preparing", "running", "judging"].includes(room.status),
    ),
  );

  useEffect(() => {
    if (room && !room.can_control)
      router.replace(`/rooms/${code}/watch?notice=no-control`);
  }, [room, code, router]);

  async function control(action: MatchControlAction) {
    if (
      controlInFlight.current ||
      TERMINAL_ROOM_STATUSES.has(room?.status || "")
    )
      return;
    controlInFlight.current = action;
    setBusy(action);
    setError("");
    setNotice("");
    try {
      const operationKey =
        controlAttempts.current[action] || crypto.randomUUID();
      controlAttempts.current[action] = operationKey;
      const result = await apiFetch<{ room: Room }>(
        `/api/rooms/${code}/control/${action}`,
        {
          method: "POST",
          headers: { "X-Idempotency-Key": operationKey },
          body: JSON.stringify({ reason: CONTROL_REASONS[action] }),
        },
      );
      setRoom((current) =>
        !current || result.room.seq >= current.seq ? result.room : current,
      );
      delete controlAttempts.current[action];
      setNotice(
        action === "pause"
          ? "比赛已暂停，计时和当前进度已保存。"
          : action === "safe-pause"
            ? "当前真人发言已紧急暂停，已确认的文字和剩余时间均已保留。"
          : action === "resume"
            ? "比赛已继续，将从刚才的位置自动推进。"
            : action === "reset-speech"
              ? "当前 AI 发言已重置，将从同一段文字重新播放。"
              : action === "retry"
                ? "当前步骤已重置，系统正在重新执行。"
                : action === "skip"
                  ? "当前阶段已跳过，系统正在进入下一阶段。"
                  : "比赛已终止，后续不会再产生新的发言。",
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    } finally {
      setBusy("");
      controlInFlight.current = "";
    }
  }

  function requestControl(action: MatchControlAction, origin?: HTMLElement) {
    if (
      action === "safe-pause" ||
      action === "reset-speech" ||
      action === "retry" ||
      action === "skip" ||
      action === "terminate"
    ) {
      confirmationOrigin.current =
        origin ||
        (document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null);
      setConfirmation(action);
      return;
    }
    void control(action);
  }

  useEffect(() => {
    if (!confirmation) return;
    const previousBodyOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    confirmationCancel.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !controlInFlight.current) {
        event.preventDefault();
        setConfirmation(null);
        return;
      }
      if (event.key !== "Tab" || !confirmationDialog.current) return;
      const focusable = [
        ...confirmationDialog.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
        ),
      ];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousBodyOverflow;
      confirmationOrigin.current?.focus();
    };
  }, [confirmation]);

  if (!room && roomError)
    return <LoadError message={roomError} retry={reconnect} />;
  if (!room) return <div className="loading-screen">正在载入比赛控制台…</div>;
  if (!room.can_control)
    return (
      <div className="loading-screen">
        你没有本房间控制权限，正在切换到只读观战…
      </div>
    );

  const disconnectedHumans = room.seats.filter(
    (seat) => seat.occupant_type === "human" && !seat.connected,
  );
  const disconnectedHumanNames = disconnectedHumans
    .map((seat) => seat.display_name)
    .join("、");
  const disconnectGraceActive =
    disconnectedHumans.length > 0 &&
    ["preparing", "running", "judging"].includes(room.status);
  const disconnectPauseTiming = disconnectGraceTimingLabel(
    disconnectGraceRemaining,
  );
  const disconnectPausePending =
    disconnectGraceActive && disconnectGraceRemaining === 0;
  const humanSpeaking = room.active_speech?.speaker_type === "human";
  const aiSpeechActive =
    room.status === "running" && room.active_speech?.speaker_type === "ai";
  const aiAudioPlaying = aiSpeechActive && room.active_speech?.status === "playing";
  const aiSpeechPreparing = aiSpeechActive && !aiAudioPlaying;
  const pauseReasonCode = room.pause_health?.reason_code;
  const participantDisconnectPaused =
    room.status === "paused" &&
    pauseReasonCode === "participant_disconnected";
  const participantDisconnectAndFailurePaused =
    room.status === "paused" &&
    pauseReasonCode === "service_failure_and_participant_disconnected";
  const participantStartTimeoutPaused =
    room.status === "paused" &&
    pauseReasonCode === "participant_start_timeout";
  const participantDisconnectInvolved =
    participantDisconnectPaused || participantDisconnectAndFailurePaused;
  const failurePaused =
    room.status === "paused" &&
    Boolean(room.failure_reason) &&
    !participantDisconnectPaused &&
    !participantStartTimeoutPaused;
  const needsAttention =
    failurePaused ||
    participantDisconnectPaused ||
    participantStartTimeoutPaused ||
    disconnectGraceActive ||
    room.status === "paused";
  const recoveryBlockedByDisconnect =
    participantDisconnectInvolved && disconnectedHumans.length > 0;
  const isTerminal = TERMINAL_ROOM_STATUSES.has(room.status);
  const canPause =
    ["preparing", "running", "judging"].includes(room.status) &&
    !humanSpeaking &&
    !disconnectPausePending;
  const canSafePause =
    room.status === "running" && humanSpeaking && !disconnectPausePending;
  const canSkip =
    ["running", "paused", "judging"].includes(room.status) &&
    !failurePaused &&
    !participantDisconnectInvolved &&
    !participantStartTimeoutPaused &&
    !humanSpeaking &&
    !disconnectPausePending &&
    disconnectedHumans.length === 0;
  const canTerminate = ["preparing", "running", "paused", "judging"].includes(
    room.status,
  );
  const canResume =
    room.status === "paused" &&
    (!room.failure_reason || participantDisconnectPaused || participantStartTimeoutPaused) &&
    disconnectedHumans.length === 0;
  const currentSpeaker = room.active_speech
    ? room.seats.find((seat) => seat.seat_key === room.active_speech?.seat_key)
        ?.display_name
    : room.current_stage?.seat
      ? room.seats.find((seat) => seat.seat_key === room.current_stage?.seat)
          ?.display_name
      : null;
  const currentStageSeat = room.current_stage?.seat
    ? room.seats.find((seat) => seat.seat_key === room.current_stage?.seat)
    : null;
  const skipDisabledReason = busy
    ? "正在处理上一项操作，请稍候。"
    : canSkip
      ? ""
      : humanSpeaking
        ? `${currentSpeaker || "真人辩手"}正在发言；结束并提交后才能跳过阶段。`
        : disconnectPausePending
          ? "真人断线等待已结束，系统正在自动暂停；暂停完成前不能跳过。"
        : disconnectGraceActive
          ? `正在等待${disconnectedHumanNames}重新连接，真人断线期间不能跳过当前阶段。`
        : participantDisconnectInvolved && disconnectedHumans.length > 0
            ? `仍在等待${disconnectedHumanNames}重新连接，不能跳过当前阶段。`
            : failurePaused
              ? `当前步骤存在服务异常，请先${failureRetryLabel(room)}，不能用跳过掩盖异常。`
              : participantDisconnectInvolved
                ? "比赛因真人断线暂停，请先恢复比赛，再决定是否跳过阶段。"
                : participantStartTimeoutPaused
                  ? "当前阶段需要由真人完成；请确认准备后继续比赛，不能直接跳过。"
            : isTerminal
              ? "比赛已经结束，不能再跳过阶段。"
              : room.status === "preparing"
                ? "系统正在完成开场准备，进入正式阶段后才可跳过。"
                : "当前比赛状态不允许跳过阶段。";
  const terminateDisabledReason = busy
    ? "正在处理上一项操作，请稍候。"
    : canTerminate
      ? ""
      : isTerminal
        ? "比赛已经结束，无需再次终止。"
        : "当前比赛状态不能提前结束。";
  const retryLabel = failureRetryLabel(room);
  const primaryAction: MatchControlAction | null = failurePaused
    ? "retry"
    : room.status === "paused"
      ? "resume"
      : ["preparing", "running", "judging"].includes(room.status)
        ? humanSpeaking
          ? "safe-pause"
          : "pause"
        : null;
  const primaryActionLabel =
    primaryAction === "retry"
      ? recoveryBlockedByDisconnect
        ? "等待全部真人重新连接"
        : retryLabel
      : primaryAction === "resume"
        ? "继续比赛"
        : primaryAction === "safe-pause"
          ? "紧急暂停当前发言"
        : primaryAction === "pause"
          ? disconnectPausePending
            ? "正在自动暂停"
            : canPause
            ? room.status === "preparing"
              ? "暂停准备流程"
              : "暂停比赛"
            : "真人发言中，结束后可暂停"
          : room.status === "preparing"
            ? "比赛准备中"
            : "比赛已结束";
  const controlGuidance = isTerminal
    ? "本场比赛已经结束，控制操作已关闭。"
    : participantStartTimeoutPaused
      ? "当前阶段已暂停，等待真人辩手准备。"
    : participantDisconnectAndFailurePaused
      ? disconnectedHumans.length
        ? `比赛同时遇到服务异常和真人断线。请先等待${disconnectedHumanNames}重新连接，人员齐全后再${retryLabel}。`
        : `全部真人已重新连接，但当前步骤仍有服务异常。请${retryLabel}，系统会从已保存的位置重新执行。`
    : participantDisconnectPaused
      ? disconnectedHumans.length
        ? "比赛已因真人断线自动暂停。真人席位和身份保持不变；请等待全部真人重新连接后再继续。"
        : "比赛已因真人断线自动暂停。全部真人已连接，请确认现场就绪后继续比赛。"
      : failurePaused
        ? `比赛已安全暂停。${retryLabel}会从已保存的位置重新执行，不会跳过当前环节。`
        : room.status === "paused"
          ? "比赛已暂停，计时和当前步骤均已保存；确认现场就绪后继续。"
          : humanSpeaking
            ? "真人正在发言。正常情况请由辩手结束并提交；仅在麦克风、识别或页面完全卡住时紧急暂停。"
            : aiAudioPlaying
              ? "AI 语音正在播放。正常情况下无需操作；只有声音完全卡住或提前中断时才重置。"
              : aiSpeechPreparing
                ? "AI 发言正在生成或合成语音。请等待自动开始播放；只有长时间没有进展时才重试。"
              : room.status === "preparing"
                ? "系统正在建立实时语音并加载预设开场提示；此时不会开始比赛计时，完成后自动进入第一阶段。"
                : room.status === "judging"
                  ? "自动评审正在处理结果，通常不需要手动操作。"
                  : "正常流程由系统自动推进，仅在异常或应急情况下操作。";
  const automaticStep = isTerminal
    ? "自动流程已结束。比赛记录和结果均已保存。"
    : participantStartTimeoutPaused
      ? `“${room.current_stage?.name || "当前步骤"}”已保留，发言计时尚未开始。`
    : participantDisconnectAndFailurePaused
      ? disconnectedHumans.length
        ? `流程已冻结在“${room.current_stage?.name || "当前步骤"}”，正在等待全部真人重新连接。`
        : `流程已冻结在“${room.current_stage?.name || "当前步骤"}”，等待房主重试异常步骤。`
      : participantDisconnectPaused
        ? disconnectedHumans.length
          ? `流程已冻结在“${room.current_stage?.name || "当前步骤"}”，正在等待全部真人重新连接。`
          : `人员已经齐全，等待房主从“${room.current_stage?.name || "当前步骤"}”继续。`
        : failurePaused
          ? `流程已冻结在“${room.current_stage?.name || "当前步骤"}”，等待房主${retryLabel}。`
          : room.status === "paused"
            ? `流程已暂停在“${room.current_stage?.name || "当前步骤"}”，等待房主继续。`
            : room.status === "preparing"
              ? "正在建立实时语音并加载预设开场提示；完成后会自动进入第一阶段。"
              : room.status === "judging"
                ? "正在自动评审本场比赛；完成后会直接生成结果。"
                : room.active_speech?.speaker_type === "human"
                  ? `正在等待${currentSpeaker || "当前真人辩手"}结束发言并确认文字。`
                  : room.active_speech?.speaker_type === "ai"
                    ? aiAudioPlaying
                      ? `正在播放${currentSpeaker || "当前 AI 辩手"}的发言；播放完成后自动进入下一步。`
                      : `正在为${currentSpeaker || "当前 AI 辩手"}生成发言或合成语音；准备完成后自动播放。`
                    : room.current_stage?.kind === "announcement"
                      ? `正在播放“${room.current_stage.name}”预设提示音。`
                      : currentStageSeat?.occupant_type === "human"
                        ? `正在等待${currentStageSeat.display_name}（${currentStageSeat.label}）点击开始发言。`
                        : room.current_stage?.ai_preparing
                          ? `正在生成${currentStageSeat?.display_name || "AI 辩手"}的发言内容并准备语音。`
                          : `正在执行“${room.current_stage?.name || "下一阶段"}”，完成后自动推进。`;
  const dockDetail = busy
    ? "正在安全提交操作，请勿重复点击。"
    : participantStartTimeoutPaused
      ? "确认辩手和麦克风就绪后继续比赛。"
    : disconnectGraceActive
      ? `${disconnectedHumanNames}已断线；${disconnectPauseTiming}。倒计时内返回无需房主操作，超时后比赛会自动暂停。`
      : participantDisconnectAndFailurePaused
        ? disconnectedHumans.length
          ? `先等待${disconnectedHumanNames}重新连接；人员齐全后再${retryLabel}。`
          : `全部真人已连接，请${retryLabel}。`
      : participantDisconnectPaused
        ? disconnectedHumans.length
          ? "比赛已自动暂停；真人席位保持不变，全部真人重新连接后才能继续。"
          : "比赛已自动暂停；全部真人已连接，房主可以继续比赛。"
        : humanSpeaking
          ? `${currentSpeaker || "真人辩手"}正在发言；正常结束会自动推进，设备或识别完全卡住时可紧急暂停并保留剩余时间。`
          : failurePaused
            ? `${retryLabel}后系统会从已保存的位置重新执行。`
            : room.status === "preparing"
              ? "系统正在建立实时语音并加载预设开场提示；计时尚未开始，无需重复操作。"
              : currentSpeaker
                ? `当前：${currentSpeaker}`
                : controlGuidance;

  return (
    <div className={`page-shell room-control-page ${styles.controlPage}`}>
      <header className={styles.controlHero}>
        <div>
          <span className="eyebrow">比赛控制</span>
          <h1>房间 #{room.code} 控制台</h1>
          <p>{controlGuidance}</p>
        </div>
        <div className={styles.heroActions}>
          {room.my_seat && (
            <Link
              href={`/rooms/${code}/debate`}
              className="button button-primary"
            >
              <Mic size={17} />
              返回辩手页面
            </Link>
          )}
          <Link
            href={`/rooms/${code}/watch`}
            className="button button-secondary"
          >
            <Eye size={17} />
            打开观战
          </Link>
        </div>
      </header>

      {room.failure_reason && !participantDisconnectPaused && !participantStartTimeoutPaused && (
        <div className="error-box" role="alert">
          <AlertTriangle size={16} />
          {room.failure_reason}
        </div>
      )}
      {participantDisconnectPaused && (
        <div className="warning-box" role="status">
          <Clock3 size={16} />
          <span>
            <strong>真人断线超过 60 秒，比赛已自动暂停。</strong>{" "}
            {disconnectedHumans.length
              ? `仍在等待：${disconnectedHumanNames}。`
              : "全部真人已重新连接。"}{" "}
            真人席位和身份保持不变；全部人员就绪后由房主继续比赛。
          </span>
        </div>
      )}
      {participantStartTimeoutPaused && (
        <div className="warning-box" role="status">
          <Clock3 size={16} />
          <span>
            <strong>真人长时间未开始发言，比赛已安全暂停。</strong>{" "}
            发言时间尚未消耗；请确认当前辩手和麦克风均已准备，再继续比赛。
          </span>
        </div>
      )}
      {participantDisconnectAndFailurePaused && (
        <div className="warning-box" role="status">
          <Clock3 size={16} />
          <span>
            <strong>服务异常发生时，还有真人辩手处于断线状态。</strong>{" "}
            {disconnectedHumans.length
              ? `先等待：${disconnectedHumanNames}。全部真人重新连接后再${retryLabel}。`
              : `全部真人已重新连接，现在可以${retryLabel}。`} {" "}
            比赛进度和真人席位均已保留。
          </span>
        </div>
      )}
      {disconnectGraceActive && (
        <div className="warning-box" role="status">
          <Clock3 size={16} />
          <span>
            <strong>{disconnectedHumanNames}已断线，正在等待返回。</strong>{" "}
            {disconnectPauseTiming}；真人席位和身份保持不变。倒计时内返回无需房主操作；
            超时自动暂停后，再由房主确认人员齐全并继续比赛。
          </span>
        </div>
      )}
      {roomError && !connected && (
        <div className="warning-box" role="alert">
          <AlertTriangle size={16} />
          {roomError}
          <button type="button" className="text-button" onClick={reconnect}>
            立即重连
          </button>
        </div>
      )}
      {error && (
        <div className="error-box room-control-feedback" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div
          className="notice-box room-control-feedback"
          role="status"
          aria-live="polite"
        >
          {notice}
        </div>
      )}
      <div
        className={`${styles.healthBanner} ${needsAttention ? styles.healthBannerAttention : ""}`}
        aria-live="polite"
      >
        {needsAttention ? <AlertTriangle /> : <ShieldCheck />}
        <span>
          <strong>
            {failurePaused || participantDisconnectPaused || participantStartTimeoutPaused
              ? "比赛需要房主确认"
              : disconnectGraceActive
                ? disconnectPausePending
                  ? "正在自动暂停比赛"
                  : "正在等待辩手返回"
                : room.status === "paused"
                  ? "比赛暂时暂停"
                  : isTerminal
                    ? "比赛已经结束"
                    : "比赛正在自动进行"}
          </strong>
          <span>
            {needsAttention
              ? participantStartTimeoutPaused
                ? "当前阶段和发言计时均已保留。"
                : controlGuidance
              : isTerminal
                ? "本场记录已保存，可返回结果或观战页面查看。"
                : "阶段、计时和发言会自动推进。除非现场出现异常，否则不需要操作。"}
          </span>
        </span>
      </div>
      <div className="stats-grid" style={{ marginTop: 18 }}>
        <div className="stat-card">
          <span className="muted">比赛状态</span>
          <strong className="control-stat-text">
            {roomStatusLabel[room.status] || room.status}
          </strong>
        </div>
        <div className="stat-card">
          <span className="muted">当前阶段</span>
          <strong className="control-stat-text">
            {room.current_stage?.name || "未开始"}
          </strong>
        </div>
        <div className="stat-card">
          <span className="muted">阶段剩余</span>
          <strong>{formatDuration(remaining)}</strong>
        </div>
        <div className="stat-card">
          <span className="muted">当前发言</span>
          <strong className="control-stat-text">
            {currentSpeaker || (room.active_speech ? "正在连接" : "等待下一位")}
          </strong>
        </div>
      </div>

      <div className={styles.controlMain}>
        <section className="panel">
          <div className="panel-title">
            <h2>比赛进程</h2>
            <span className={`connection ${connected ? "ok" : ""}`}>
              {connected ? "实时同步" : "正在重连"}
            </span>
          </div>
          <div className={styles.operationHint} aria-live="polite">
            <strong>自动流程当前步骤</strong>
            <span>{automaticStep}</span>
          </div>
          <div
            className="timeline-list"
            role={room.recent_events.length ? "list" : undefined}
            aria-label={room.recent_events.length ? "比赛进程" : undefined}
          >
            {room.recent_events
              .slice()
              .reverse()
              .map((event) => (
                <div className="timeline-item" role="listitem" key={event.seq}>
                  <span>#{event.seq}</span>
                  <i />
                  <div>
                    <strong>{matchEventLabel(event.type)}</strong>
                    <small>
                      {matchEventDetail(event)} ·{" "}
                      {new Date(event.created_at).toLocaleTimeString("zh-CN")}
                    </small>
                  </div>
                </div>
              ))}
            {!room.recent_events.length && (
              <div className="empty empty-guidance">
                <strong>比赛事件尚未产生</strong>
                <span>
                  开赛后，阶段推进、发言和异常恢复记录会实时显示在这里。
                </span>
              </div>
            )}
          </div>
        </section>

        <aside className={styles.controlSide}>
          <section
            className={`panel ${styles.recoveryPanel}`}
            aria-labelledby="recovery-tools-title"
          >
            <div className="panel-title">
              <h2 id="recovery-tools-title">异常处理</h2>
              <Wrench size={18} />
            </div>
            <div className={styles.operationHint} aria-live="polite">
              <strong>
                {failurePaused || participantDisconnectPaused || participantStartTimeoutPaused
                  ? "当前需要处理"
                  : "仅在比赛无法继续时使用"}
              </strong>
              <span>
                {participantStartTimeoutPaused
                  ? "确认辩手和麦克风就绪后，从底部继续比赛。"
                  : controlGuidance}
              </span>
            </div>
            <div className={styles.recoveryActions}>
              {aiSpeechActive && (
                <div className={styles.controlActionItem}>
                  <button
                    type="button"
                    className="button button-secondary"
                    disabled={Boolean(busy)}
                    aria-describedby={
                      busy ? "reset-speech-action-reason" : undefined
                    }
                    onClick={(event) =>
                      requestControl("reset-speech", event.currentTarget)
                    }
                  >
                    <RotateCcw size={17} />
                    {aiAudioPlaying ? "重置当前发言" : "重试 AI 语音准备"}
                  </button>
                  {busy && (
                    <small id="reset-speech-action-reason">
                      正在处理上一项操作，请稍候。
                    </small>
                  )}
                </div>
              )}
              <div className={styles.controlActionItem}>
                <button
                  type="button"
                  className="button button-secondary"
                  aria-busy={busy === "skip"}
                  aria-describedby={
                    !canSkip || busy ? "skip-stage-action-reason" : undefined
                  }
                  disabled={Boolean(busy) || !canSkip}
                  onClick={(event) =>
                    requestControl("skip", event.currentTarget)
                  }
                >
                  <SkipForward size={17} />
                  {busy === "skip"
                    ? "正在跳过…"
                    : humanSpeaking
                      ? "真人发言中，不可跳过"
                      : "跳过当前阶段"}
                </button>
                {(!canSkip || busy) && (
                  <small id="skip-stage-action-reason">
                    {skipDisabledReason}
                  </small>
                )}
              </div>
            </div>
          </section>

          <section
            className={`panel ${styles.dangerZone}`}
            aria-labelledby="danger-zone-title"
          >
            <div className="panel-title">
              <h2 id="danger-zone-title">结束比赛</h2>
            </div>
            <p id="terminate-match-guidance">
              只有确定本场无法继续时才提前结束。比赛会立即停止，并保留已产生的记录。
            </p>
            <button
              type="button"
              className="button button-danger"
              aria-busy={busy === "terminate"}
              aria-label={
                isTerminal ? "提前结束不可用（比赛已结束）" : undefined
              }
              aria-describedby={
                !canTerminate || busy
                  ? "terminate-match-action-reason"
                  : "terminate-match-guidance"
              }
              disabled={Boolean(busy) || !canTerminate}
              onClick={(event) =>
                requestControl("terminate", event.currentTarget)
              }
            >
              <Square size={17} />
              {busy === "terminate"
                ? "正在结束…"
                : isTerminal
                  ? "比赛已结束"
                  : "提前结束"}
            </button>
            {(!canTerminate || busy) && (
              <small
                id="terminate-match-action-reason"
                className={styles.dangerActionReason}
              >
                {terminateDisabledReason}
              </small>
            )}
          </section>
        </aside>
      </div>

      <nav className={styles.controlDock} aria-label="比赛主要控制">
        <div className={styles.dockSummary}>
          <span
            className={`control-dock-dot ${failurePaused || participantDisconnectPaused || participantStartTimeoutPaused ? "attention" : isTerminal ? "done" : ""}`}
            aria-hidden="true"
          />
          <span>
            <strong>
              {failurePaused
                ? "需要处理当前步骤"
                : participantStartTimeoutPaused
                  ? "等待真人确认准备"
                : participantDisconnectPaused
                  ? "真人断线，比赛已暂停"
                  : isTerminal
                    ? "本场比赛已结束"
                    : roomStatusLabel[room.status] || room.status}
            </strong>
            <small id="room-control-dock-reason">{dockDetail}</small>
          </span>
        </div>
        <button
          type="button"
          className={`button ${primaryAction === "retry" || primaryAction === "resume" ? "button-green" : "button-secondary"}`}
          aria-busy={Boolean(primaryAction && busy === primaryAction)}
          disabled={
            Boolean(busy) ||
            (primaryAction === "retry" && recoveryBlockedByDisconnect) ||
            (primaryAction === "safe-pause" && !canSafePause) ||
            (primaryAction === "pause" && !canPause) ||
            (primaryAction === "resume" && !canResume) ||
            !primaryAction
          }
          aria-describedby="room-control-dock-reason"
          aria-label={
            primaryAction === null && isTerminal
              ? "比赛已结束（主操作不可用）"
              : disconnectPausePending
                ? "真人断线倒计时结束，正在自动暂停"
              : primaryAction === "retry" && recoveryBlockedByDisconnect
                ? "等待全部真人重新连接后重试"
              : primaryAction === "safe-pause" && !canSafePause
                ? "当前没有可紧急暂停的真人发言"
              : primaryAction === "pause" && !canPause
                ? "真人发言中"
                : primaryAction === "resume" && !canResume
                  ? "等待全部真人重新连接"
                  : undefined
          }
          onClick={(event) =>
            primaryAction && requestControl(primaryAction, event.currentTarget)
          }
          title={
            disconnectPausePending
              ? "真人断线倒计时已经结束，系统正在自动暂停比赛"
              : primaryAction === "retry" && recoveryBlockedByDisconnect
                ? "全部真人重新连接后才能重试当前异常步骤"
              : primaryAction === "safe-pause" && !canSafePause
                ? "仅在真人发言进行中且比赛仍在运行时可紧急暂停"
              : primaryAction === "pause" && !canPause
              ? "真人发言结束后才能暂停比赛"
              : primaryAction === "resume" && !canResume
                ? "全部真人重新连接后才能继续比赛"
                : undefined
          }
        >
          {primaryAction === "retry" ? (
            <RotateCcw size={17} />
          ) : primaryAction === "resume" ? (
            <Play size={17} />
          ) : primaryAction === "pause" || primaryAction === "safe-pause" ? (
            <Pause size={17} />
          ) : (
            <Clock3 size={17} />
          )}
          {busy === primaryAction ? "正在处理…" : primaryActionLabel}
        </button>
      </nav>

      {confirmation && (
        <div className="room-control-confirm-backdrop" role="presentation">
          <div
            ref={confirmationDialog}
            className="room-control-confirm panel"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="room-control-confirm-title"
            aria-describedby="room-control-confirm-description"
          >
            <strong id="room-control-confirm-title">
              {confirmation === "safe-pause"
                ? "确认紧急暂停当前真人发言？"
                : confirmation === "reset-speech"
                ? "确认重置当前 AI 发言？"
                : confirmation === "retry"
                  ? `确认${retryLabel}？`
                  : confirmation === "skip"
                    ? "确认跳过当前阶段？"
                    : "确认提前结束比赛？"}
            </strong>
            <p id="room-control-confirm-description">
              {confirmation === "safe-pause"
                ? "系统会立即停止当前真人发言，保留已经确认的文字和剩余时间。恢复比赛后，仍由原辩手重新点击发言继续；仅在设备、识别或页面完全卡住时使用。"
                : confirmation === "reset-speech"
                ? aiAudioPlaying
                  ? "系统会停止当前声音、清除尚未播放的内容，并从同一段文字重新开始。只有声音完全卡住或提前结束时才需要这样做。"
                  : "系统会停止当前生成或语音合成任务，并重新准备这次 AI 发言。正常准备期间请耐心等待，仅在长时间没有进展时使用。"
                : confirmation === "retry"
                  ? `${retryLabel}会丢弃本次未完成的内容，从已保存的位置重新开始，不会影响其他房间。`
                  : confirmation === "skip"
                    ? `系统会跳过“${room.current_stage?.name || "当前阶段"}”并立即进入下一阶段，当前未完成的发言不会计入结果。`
                    : `比赛将立即终止且不可恢复${humanSpeaking ? "，当前真人发言会被中断且不会计入结果" : ""}。如果只是暂时异常，请选择取消并使用暂停或重置。`}
            </p>
            <div className="card-actions">
              <button
                ref={confirmationCancel}
                type="button"
                className="button button-small button-secondary"
                disabled={Boolean(busy)}
                onClick={() => setConfirmation(null)}
              >
                取消
              </button>
              <button
                type="button"
                className={`button button-small ${confirmation === "terminate" || confirmation === "skip" || confirmation === "safe-pause" ? "button-danger" : "button-green"}`}
                aria-busy={busy === confirmation}
                disabled={Boolean(busy)}
                onClick={() => {
                  const action = confirmation;
                  setConfirmation(null);
                  void control(action);
                }}
              >
                {busy === confirmation
                  ? "正在处理…"
                  : confirmation === "safe-pause"
                    ? "确认紧急暂停"
                  : confirmation === "reset-speech"
                    ? "确认重置发言"
                    : confirmation === "retry"
                      ? `确认${retryLabel}`
                      : confirmation === "skip"
                        ? "确认跳过"
                        : "确认提前结束"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
