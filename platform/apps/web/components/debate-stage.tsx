"use client";

import Link from "next/link";
import { AlertTriangle, Clock3, Headphones, LogOut, Maximize, Mic, MicOff, MonitorUp, Pause, Pencil, Play, RotateCcw, Settings, Square, Users, Volume2, VolumeX, Wifi, WifiOff } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { apiFetch, apiOrigin, websocketUrl } from "@/lib/api";
import { BoundedRtcRecovery, LiveKitRoomAudio } from "@/lib/audio/livekit-room-audio";
import { installVoiceTelemetryReporter } from "@/lib/audio/voice-telemetry";
import { compactCaptionLine } from "@/lib/caption-line";
import { mergeAsrText } from "@/lib/asr-text";
import { acquireControlLease, controlLeaseFor, isControlLeaseConflict } from "@/lib/control-lease";
import { StageSubtitle } from "@/components/stage-caption-projection";
import { TranscriptDrawer } from "@/components/transcript-drawer";
import { competitionDisplayName } from "@/lib/primary-competition";
import {
  disconnectGraceRemainingSeconds,
  disconnectGraceTimingLabel,
} from "@/lib/disconnect-grace";
import { roomStatusLabel } from "@/lib/status-labels";
import { useCountdown } from "@/lib/use-room";
import type { Room, RoomSeat } from "@/lib/types";

function formatTime(value: number | null) {
  if (value === null) return "--:--";
  return `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
}

const ASR_FINAL_WAIT_MS = 30_500;
const ASR_READY_STOP_WAIT_MS = 2_000;
const ASR_TAIL_DRAIN_MS = 150;
const ASR_RECONNECT_MAX_ATTEMPTS = 3;
const ASR_RECONNECT_BASE_DELAY_MS = 250;
const ASR_RECONNECTING_MESSAGE = "字幕连接暂时中断，正在重连…";
const ASR_RECONNECTED_REVIEW_MESSAGE = "字幕连接已恢复；断线前的文字已保留，请在结束发言后核对再提交。";
const ASR_PREROLL_MAX_SAMPLES = 32_000;
// A slow network must never turn the WebSocket implementation into an
// unbounded PCM buffer. If this limit is reached we keep the turn alive for
// manual text review instead of pretending that every sample was recognized.
const ASR_SOCKET_MAX_BUFFERED_BYTES = 512 * 1024;
const ASR_CAPTURE_CHUNK_MS = 20;
const ASR_TARGET_SAMPLE_RATE = 16_000;
const FREE_DEBATE_TURN_LIMIT_SECONDS = 30;
const TERMINAL_ROOM_STATUSES = new Set(["completed", "review_required", "terminated", "cancelled"]);
const MICROPHONE_START_TIMEOUT_MS = 12_000;
const SPEECH_START_RESPONSE_TIMEOUT_MS = 12_000;
const MIN_VOICED_SAMPLES = 4_000;
const VOICE_RMS_THRESHOLD = 0.01;
const VOICE_PEAK_THRESHOLD = 0.03;
type VoiceState = "voice" | "silence" | "unknown";
type PendingFinish = {
  content: string;
  voiceState: VoiceState;
  speechId: string;
  stageKey: string;
  stageName: string;
};
type StreamingResampler = { inputRate: number; buffer: Float32Array; position: number };
type PlaybackSession = {
  audio: HTMLAudioElement;
  identity: string;
  generation: number;
  attemptToken: number;
  playPending: boolean;
  ended: boolean;
  playbackStartedAtMs: number | null;
  durationSeconds: number;
  stageStartedAtMs: number | null;
  expiresAtMs: number | null;
  expiryTimer: number | null;
  retryingAfterMediaError: boolean;
  ignoreMediaErrorsUntilMs: number;
  onError: () => void;
  onEnded: () => void;
};
type AsrSession = {
  generation: number;
  ws: WebSocket;
  context: AudioContext;
  source: MediaStreamAudioSourceNode | null;
  processor: AudioWorkletNode | null;
  ready: boolean;
  aborted: boolean;
  stopping: boolean;
  overflowed: boolean;
  failed: boolean;
  finalCompleted: boolean;
  preReadyChunks: ArrayBuffer[];
  preReadySamples: number;
  readyPromise: Promise<boolean>;
  settleReady: (ready: boolean) => void;
  tailResolve: (() => void) | null;
  resampler: StreamingResampler;
};

function DeviceControlRecovery({
  visible,
  message,
  onConfirm,
}: {
  visible: boolean;
  message: string;
  onConfirm: () => void;
}) {
  const [mounted, setMounted] = useState(false);
  const confirmButton = useRef<HTMLButtonElement | null>(null);

  useEffect(() => setMounted(true), []);
  useEffect(() => {
    if (!visible) return;
    const frame = window.requestAnimationFrame(() => confirmButton.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [visible]);

  if (!mounted || !visible) return null;
  return createPortal(
    <section
      className="stage-device-recovery"
      role="alert"
      aria-live="assertive"
      aria-labelledby="stage-device-recovery-title"
    >
      <span className="stage-device-recovery-icon" aria-hidden="true">
        <MonitorUp size={20} />
      </span>
      <span className="stage-device-recovery-copy">
        <strong id="stage-device-recovery-title">当前页面为只读</strong>
        <small>{message}</small>
      </span>
      <button
        ref={confirmButton}
        type="button"
        className="button button-small button-primary"
        onClick={onConfirm}
      >
        确认接管当前席位
      </button>
    </section>,
    document.body,
  );
}

function resetAsrReadyBarrier(session: AsrSession) {
  let settled = false;
  session.readyPromise = new Promise<boolean>((resolve) => {
    session.settleReady = (ready) => {
      if (settled) return;
      settled = true;
      resolve(ready);
    };
  });
}

function asrCaptureWorkletUrl() {
  const basePath = process.env.NEXT_PUBLIC_BASE_PATH || "";
  return `${window.location.origin}${basePath}/worklets/asr-pcm-capture.js`;
}

function resampleForAsr(input: Float32Array, state: StreamingResampler): Float32Array {
  if (state.inputRate === ASR_TARGET_SAMPLE_RATE) return input;
  const combined = new Float32Array(state.buffer.length + input.length);
  combined.set(state.buffer);
  combined.set(input, state.buffer.length);
  const ratio = state.inputRate / ASR_TARGET_SAMPLE_RATE;
  const output: number[] = [];
  while (state.position < combined.length - 1) {
    const left = Math.floor(state.position);
    const fraction = state.position - left;
    output.push(combined[left] + (combined[left + 1] - combined[left]) * fraction);
    state.position += ratio;
  }
  const consumed = Math.min(Math.floor(state.position), Math.max(0, combined.length - 1));
  state.buffer = combined.slice(consumed);
  state.position -= consumed;
  return Float32Array.from(output);
}

function SeatCard({ seat, active }: { seat: RoomSeat; active: boolean }) {
  const connectionLabel = seat.occupant_type === "human"
    ? (seat.connected ? "真人在线" : "真人离线")
    : "系统席位";
  const occupantLabel = seat.occupant_type === "human"
    ? `真人 · ${seat.connected ? "在线" : "已断线"}`
    : seat.occupant_type === "open"
      ? "空席"
      : "AI";
  return (
    <div className={`stage-seat ${active ? "active" : ""} ${seat.is_me ? "me" : ""}`}>
      <span className="seat-avatar">{seat.display_name.slice(0, 1)}</span>
      <span className="seat-copy">
        <strong>{seat.display_name}</strong>
        <small>{seat.label} · {occupantLabel}</small>
        <span className="sr-only"> · {connectionLabel}{active ? " · 当前发言席位" : ""}</span>
      </span>
      {seat.occupant_type !== "open" && (
        <span className={`seat-type-badge ${seat.occupant_type === "human" ? "human" : "ai"}`} aria-hidden="true">
          {seat.occupant_type === "human" ? "人类" : "AI"}
        </span>
      )}
      <i className={`status-dot ${seat.connected ? "online" : ""}`} aria-hidden="true" />
    </div>
  );
}

function microphoneErrorMessage(error: unknown) {
  const name = error instanceof DOMException ? error.name : "";
  if (name === "NotAllowedError" || name === "SecurityError") return "麦克风权限被拒绝，请在浏览器地址栏的网站权限中允许麦克风后重试。";
  if (name === "NotFoundError" || name === "DevicesNotFoundError") return "未找到可用麦克风，请连接或启用麦克风后重试。";
  if (name === "NotReadableError" || name === "TrackStartError") return "麦克风正被其他应用占用，或系统暂时无法读取该设备。";
  if (name === "AbortError") return "浏览器中止了麦克风启动，请重试。";
  if (name === "TimeoutError") return "麦克风启动超时，请检查浏览器权限提示或系统麦克风设置后重试。";
  if (name === "SpeechStartTimeoutError") return "麦克风已就绪，但发言启动的网络响应超时。请检查网络后重试；系统会沿用同一请求，避免重复开始发言。";
  return error instanceof Error ? error.message : "无法启动麦克风";
}

function asrRejectedMessage(reason: string | undefined) {
  if (reason === "silence") return "未检测到清晰语音，请补充本次发言文字。";
  if (["empty", "too_short", "no_speech_characters"].includes(reason || "")) {
    return "没有识别到可用发言文字，请核对并补充本次发言。";
  }
  if (reason === "speech_inactive") return "比赛已暂停或轮次已经变化，本次识别结果未写入比赛。";
  return "语音识别结果可信度不足，请核对并补充本次发言文字。";
}

function systemFacingCopy(value: string | undefined | null) {
  if (!value) return "";
  return value
    .replaceAll("主持人正在播报下一环节", "系统正在播放阶段提示")
    .replaceAll("主持人口播", "系统规则播报")
    .replaceAll("主持人", "系统");
}

type RtcFlushRequest = { identity: string; generation: string };

export function rtcFlushRequest(room: Room, liveEvent?: Record<string, unknown> | null): RtcFlushRequest | null {
  if (["paused", "judging", "completed", "review_required", "terminated", "cancelled"].includes(room.status)) {
    return { identity: `status:${room.seq}:${room.status}`, generation: "" };
  }
  const eventType = typeof liveEvent?.type === "string" ? liveEvent.type : "";
  if (!["speech.interrupted", "audio.rtc.interrupt", "audio.realtime.aborted", "audio.stream.aborted"].includes(eventType)) {
    return null;
  }
  const payload = liveEvent?.payload && typeof liveEvent.payload === "object"
    ? liveEvent.payload as Record<string, unknown>
    : {};
  const speechId = typeof payload.speech_id === "string" ? payload.speech_id : "";
  let generation = typeof payload.generation === "string" ? payload.generation.trim() : "";
  if (!generation && speechId) {
    generation = room.active_speech?.id === speechId
      ? room.active_speech.stream_generation || ""
      : room.speeches.find((speech) => speech.id === speechId)?.stream_generation || "";
  }
  // An interrupt without a generation is normally a human speech event. It
  // must never be translated into "flush whichever AI turn happens to be
  // active now", because room snapshots can already be on the next stage.
  if (!generation) return null;
  const eventIdentity = typeof liveEvent?.seq === "number"
    ? liveEvent.seq
    : `${eventType}:${speechId}:${generation}`;
  return { identity: `event:${eventIdentity}`, generation };
}

export function DebateStage({ room, connected, mode, liveEvent, connectionError = "", connectionBlockedReason = null, onReconnect, onPendingFinishChange, onRoomChanged, onLeave }: { room: Room; connected: boolean; mode: "debate" | "watch"; liveEvent?: Record<string, unknown> | null; connectionError?: string; connectionBlockedReason?: "capacity_full" | null; onReconnect?: () => void; onPendingFinishChange?: (pending: boolean) => void; onConsentChanged?: () => Promise<unknown>; onRoomChanged?: (room: Room) => void; onLeave?: () => void }) {
  const terminal = TERMINAL_ROOM_STATUSES.has(room.status);
  const stagePreparing = Boolean(room.current_stage?.ai_preparing);
  const awaitingHumanStart = !terminal && Boolean(room.current_stage?.awaiting_human_start);
  const clockRunning = room.status === "running" || room.status === "judging";
  const remaining = useCountdown(room.remaining_seconds, room.seq, clockRunning && !stagePreparing && !awaitingHumanStart);
  // A free-debate turn is authoritative only after the server has opened its
  // clock. Before a human presses “start”, and during the inter-turn request
  // window, the API deliberately keeps the full turn budget frozen. Running
  // a client-only countdown in either state used to manufacture a misleading
  // `00:00` even though the server was still waiting with the full budget.
  const freeTurnClockRunning = Boolean(
    room.status === "running"
      && room.current_stage?.kind === "free"
      && !stagePreparing
      && !awaitingHumanStart
      && (room.current_stage.turn_started_at || room.active_speech),
  );
  // Old rooms and cached snapshots may still carry the former 40/45-second
  // free-debate budget. The current competition rule is a hard 30-second
  // maximum, so never let a stale projection promise extra speaking time.
  // Once a turn starts, the server remains authoritative below that cap.
  const projectedTurnRemaining = room.turn_remaining_seconds === null
    ? null
    : Math.min(
        FREE_DEBATE_TURN_LIMIT_SECONDS,
        Math.max(0, room.turn_remaining_seconds),
      );
  const turnRemaining = useCountdown(
    projectedTurnRemaining,
    room.seq,
    freeTurnClockRunning,
  );
  const disconnectGraceRemaining = useCountdown(
    disconnectGraceRemainingSeconds(room),
    room.seq,
    Boolean(
      room.disconnect_grace?.will_pause &&
        ["preparing", "running", "judging"].includes(room.status),
    ),
  );
  const failureFingerprint = room.failure_reason ? JSON.stringify([room.seq, room.failure_reason]) : "";
  const [muted, setMuted] = useState(mode === "watch");
  const [showSettings, setShowSettings] = useState(false);
  const [starting, setStarting] = useState(false);
  const [capturing, setCapturing] = useState(false);
  const [speechId, setSpeechId] = useState("");
  const [partial, setPartial] = useState("");
  // The transcript is the durable value used when a turn is submitted.  The
  // stage caption is deliberately a separate, one-segment projection: keeping
  // the whole transcript in the stage made every interim update grow into a
  // multi-line wall of text that users could not follow in real time.
  const [asrCaption, setAsrCaption] = useState("");
  const [captureError, setCaptureError] = useState("");
  const [deviceControl, setDeviceControl] = useState<"acquiring" | "owned" | "lost" | "unavailable">(mode === "watch" ? "unavailable" : "acquiring");
  const [deviceControlError, setDeviceControlError] = useState("");
  const [controlSeq, setControlSeq] = useState<number | null>(null);
  const [pendingFinish, setPendingFinish] = useState<PendingFinish | null>(null);
  const [finishing, setFinishing] = useState(false);
  const [retryingFailure, setRetryingFailure] = useState(false);
  const [retryFailureError, setRetryFailureError] = useState("");
  const [roomActionBusy, setRoomActionBusy] = useState("");
  const [roomActionError, setRoomActionError] = useState("");
  const [roomActionStatus, setRoomActionStatus] = useState("");
  const [confirmation, setConfirmation] = useState<"terminate" | "leave" | "retry" | null>(null);
  const [playbackNeedsGesture, setPlaybackNeedsGesture] = useState(false);
  const [playbackError, setPlaybackError] = useState("");
  const [playbackNotice, setPlaybackNotice] = useState("");
  const [playbackPending, setPlaybackPending] = useState(false);
  const [rtcAudioConnected, setRtcAudioConnected] = useState(false);
  const pendingSpeechStatus = pendingFinish
    ? room.speeches.find((item) => item.id === pendingFinish.speechId)?.status
    : undefined;
  const pendingStageChanged = Boolean(
    pendingFinish
      && room.current_stage?.key !== pendingFinish.stageKey,
  );
  const pendingFinishDiscardReason = !pendingFinish
    ? ""
    : pendingStageChanged
      ? `比赛已从“${pendingFinish.stageName}”切换到“${room.current_stage?.name || "下一环节"}”，这份文字不能提交到旧环节。`
      : room.status === "terminated"
      ? "比赛已终止，本次未提交文字无法再保存。"
      : pendingSpeechStatus && ["completed", "interrupted", "timed_out"].includes(pendingSpeechStatus)
        ? pendingSpeechStatus === "timed_out"
          ? "本次发言已经超时关闭，未提交文字不能再保存到旧轮次。"
          : pendingSpeechStatus === "completed"
            ? "本次发言已经完成，未提交文字不能重复保存。"
            : "本次发言已被比赛控制中断，未提交文字无法再保存。"
        : room.active_speech && room.active_speech.id !== pendingFinish.speechId
          ? "当前轮次已经开始另一条发言，这份未提交文字不能再保存。"
        : "";
  const pendingFinishMustDiscard = Boolean(pendingFinishDiscardReason);
  const discardLeadsToResult = ["completed", "review_required", "terminated"].includes(room.status);
  const asrSocket = useRef<WebSocket | null>(null);
  const asrSession = useRef<AsrSession | null>(null);
  const asrReconnectTimer = useRef<number | null>(null);
  const asrReconnectAttempts = useRef(0);
  const audioContext = useRef<AudioContext | null>(null);
  const asrAudioInitialization = useRef<Promise<AudioContext> | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const playbackIdentity = useRef("");
  const playbackGeneration = useRef(0);
  const playbackSession = useRef<PlaybackSession | null>(null);
  const rtcAudio = useRef<LiveKitRoomAudio | null>(null);
  const rtcLifecycleGeneration = useRef(0);
  const rtcFlushIdentity = useRef("");
  const roomRef = useRef(room);
  const mutedRef = useRef(muted);
  roomRef.current = room;
  mutedRef.current = muted;
  const finishKey = useRef("");
  const startKey = useRef("");
  const retryFailureAttempt = useRef<{ fingerprint: string; key: string } | null>(null);
  const timeoutHandled = useRef("");
  const captureActive = useRef(false);
  const stopPromise = useRef<Promise<void> | null>(null);
  const stopServerTimedOut = useRef(false);
  const startingRef = useRef(false);
  const startGeneration = useRef(0);
  const startController = useRef<AbortController | null>(null);
  const finishController = useRef<AbortController | null>(null);
  const finishPromise = useRef<Promise<boolean> | null>(null);
  const captureStageIdentity = useRef("");
  const captureStageKey = useRef("");
  const captureStageName = useRef("");
  const startCancellationMessage = useRef("");
  const transcriptRef = useRef("");
  const partialRef = useRef("");
  const voicedSamples = useRef(0);
  const observedSamples = useRef(0);

  useEffect(() => installVoiceTelemetryReporter(room.code, () => {
    const active = roomRef.current.active_speech;
    if (!active || active.speaker_type !== "ai" || !active.stream_generation) return null;
    return { speechId: active.id, generation: active.stream_generation };
  }), [room.code]);
  const recoveredTranscriptBaseline = useRef("");
  const asrRejectionMessage = useRef("");
  const asrFinalWaiter = useRef<(() => void) | null>(null);
  const settingsButton = useRef<HTMLButtonElement | null>(null);
  const brandButton = useRef<HTMLButtonElement | null>(null);
  const settingsDialog = useRef<HTMLDivElement | null>(null);
  const confirmationDialog = useRef<HTMLDivElement | null>(null);
  const confirmationCancel = useRef<HTMLButtonElement | null>(null);
  const roomActionAttempts = useRef<Record<string, string>>({});
  const roomActionInFlight = useRef("");
  const forceTranscriptReview = useRef(false);
  const confirmationOrigin = useRef<"brand" | "settings">("settings");
  const pendingTranscript = useRef<HTMLTextAreaElement | null>(null);
  const discardPendingButton = useRef<HTMLButtonElement | null>(null);
  const freeStageSeats = room.current_stage?.kind === "free"
    ? room.seats.filter((seat) => seat.side === room.current_stage?.side && seat.occupant_type !== "open")
    : [];
  const queuedFreeSeatKey = room.current_stage?.kind === "free"
    ? room.free_turn_queue?.items
      .filter((item) => item.side === room.current_stage?.side)
      .slice()
      .sort((left, right) => left.order - right.order)[0]?.seat_key || ""
    : "";
  // Free debate has no fixed `stage.seat`. Once the server selects a human
  // (or a 1v1 side has exactly one occupied seat), keep a concrete visual
  // focus instead of leaving the centre of the stage empty while the clock
  // waits for that debater to begin.
  const freeStageFocusSeatKey = room.current_stage?.kind === "free"
    ? room.current_stage.selected_human_seat
      || queuedFreeSeatKey
      || (freeStageSeats.length === 1 ? freeStageSeats[0].seat_key : "")
    : "";
  const activeSeatKey = terminal
    ? ""
    : room.active_speech?.seat_key
      || room.current_stage?.seat
      || freeStageFocusSeatKey
      || (room.current_stage?.kind === "free" ? `${room.current_stage.side}_` : "");
  const aiPreparing = Boolean(
    !terminal
      && stagePreparing
      && room.active_speech?.speaker_type === "ai"
      && ["speaking", "synthesizing"].includes(room.active_speech.status),
  );
  const aff = room.seats.filter((seat) => seat.side === "aff");
  const neg = room.seats.filter((seat) => seat.side === "neg");
  const mySeat = room.seats.find((seat) => seat.is_me);
  const stageIdentity = `${room.status}:${room.current_stage?.key || "none"}:${room.current_stage?.kind === "free" ? room.current_stage.side || "none" : "fixed"}:${room.my_seat || "none"}`;
  const lease = useMemo(
    () => controlLeaseFor(room.code, room.my_seat || ""),
    [room.code, room.my_seat],
  );

  const prepareAsrAudioGraph = useCallback(async () => {
    const current = audioContext.current;
    if (current && current.state !== "closed") return current;
    if (asrAudioInitialization.current) return asrAudioInitialization.current;
    const initialization = (async () => {
      const AudioContextClass = window.AudioContext
        || (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!AudioContextClass || typeof AudioWorkletNode === "undefined") {
        throw new Error("当前浏览器不支持 AudioWorklet 实时语音采集");
      }
      const context = new AudioContextClass({ latencyHint: "interactive" });
      const worklet = (context as AudioContext & { audioWorklet?: AudioWorklet }).audioWorklet;
      if (worklet?.addModule) await worklet.addModule(asrCaptureWorkletUrl());
      else if (process.env.NODE_ENV !== "test") throw new Error("当前浏览器不支持 AudioWorklet 实时语音采集");
      audioContext.current = context;
      return context;
    })();
    asrAudioInitialization.current = initialization;
    try {
      return await initialization;
    } catch (error) {
      audioContext.current = null;
      throw error;
    } finally {
      if (asrAudioInitialization.current === initialization) asrAudioInitialization.current = null;
    }
  }, []);

  useEffect(() => {
    void prepareAsrAudioGraph().catch(() => undefined);
  }, [prepareAsrAudioGraph, room.code]);

  useEffect(() => {
    const client = new LiveKitRoomAudio();
    rtcAudio.current = client;
    let disposed = false;
    const lifecycleGeneration = ++rtcLifecycleGeneration.current;
    setRtcAudioConnected(false);
    setPlaybackNotice("");

    const restoreRtc = () => {
      if (disposed || rtcLifecycleGeneration.current !== lifecycleGeneration) return;
      recovery.cancel();
      setRtcAudioConnected(true);
      setPlaybackPending(false);
      setPlaybackNotice("");
      setPlaybackError("");
      if (activeStreamDescriptor(roomRef.current)) disposePlayback();
      client.setMuted(mutedRef.current);
      const generation = roomRef.current.active_speech?.stream_generation || "";
      if (generation) client.activateGeneration(generation);
    };

    const failRtc = (reason: string) => {
      if (disposed || rtcLifecycleGeneration.current !== lifecycleGeneration) return;
      recovery.cancel();
      setRtcAudioConnected(false);
      setPlaybackPending(false);
      setPlaybackNotice("");
      setPlaybackError(`WebRTC 实时音频连接失败：${reason}。本次发言不会切换播放器；请由房主重试当前步骤。`);
      window.dispatchEvent(new CustomEvent("jixia:agent-audio-diagnostic", {
        detail: { action: "rtc-recovery-exhausted", reason, roomCode: roomRef.current.code },
      }));
    };

    const recovery = new BoundedRtcRecovery(
      () => client.reconnect(),
      {
        onAttempt: (attempt) => {
          if (disposed || rtcLifecycleGeneration.current !== lifecycleGeneration) return;
          setRtcAudioConnected(false);
          setPlaybackPending(true);
          setPlaybackNotice(`实时音频连接中断，正在进行第 ${attempt} 次重连…`);
          setPlaybackError("");
        },
        onRecovered: restoreRtc,
        onExhausted: failRtc,
      },
    );

    void client.prepare(room.code, {
      onNeedsGesture: () => {
        if (!disposed) setPlaybackNeedsGesture(true);
      },
      onReady: () => {
        restoreRtc();
      },
      onReconnecting: () => {
        if (!disposed) setPlaybackPending(true);
      },
      onRecovered: () => {
        restoreRtc();
      },
      onFatalError: (reason) => {
        if (disposed) return;
        setRtcAudioConnected(false);
        recovery.start(reason);
      },
    }).then((connected) => {
      if (disposed) return;
      if (connected) restoreRtc();
      else if (client.isDisabled()) {
        recovery.cancel();
        setRtcAudioConnected(false);
        setPlaybackPending(false);
        setPlaybackNotice("实时比赛声音未启用，请联系系统管理员。");
        setPlaybackError("");
      } else recovery.start("WebRTC 实时音频初始化失败");
    });
    return () => {
      disposed = true;
      recovery.cancel();
      if (rtcAudio.current === client) rtcAudio.current = null;
      void client.dispose();
    };
  }, [room.code]);

  const retryFailedStep = useCallback(async () => {
    if (mode !== "debate" || retryingFailure || !room.failure_reason || !room.can_control) return;
    setRetryingFailure(true);
    setRetryFailureError("");
    const operationKey = retryFailureAttempt.current?.fingerprint === failureFingerprint
      ? retryFailureAttempt.current.key
      : crypto.randomUUID();
    retryFailureAttempt.current = { fingerprint: failureFingerprint, key: operationKey };
    try {
      const result = await apiFetch<{ room: Room }>(`/api/rooms/${room.code}/control/retry`, {
        method: "POST",
        headers: { "X-Idempotency-Key": operationKey },
        body: JSON.stringify({ reason: "房主在比赛舞台重试异常步骤" }),
      });
      retryFailureAttempt.current = null;
      onRoomChanged?.(result.room);
    } catch {
      setRetryFailureError("暂时无法重试，请稍后再试；比赛进度已安全保留。");
    } finally {
      setRetryingFailure(false);
    }
  }, [failureFingerprint, mode, onRoomChanged, retryingFailure, room.can_control, room.code, room.failure_reason]);

  useEffect(() => {
    setRetryFailureError("");
    if (!failureFingerprint) retryFailureAttempt.current = null;
  }, [failureFingerprint]);

  const acquireDeviceControl = useCallback(async (force = false) => {
    if (mode !== "debate" || !lease || !mySeat || mySeat.occupant_type !== "human") {
      setDeviceControl("unavailable");
      return;
    }
    setDeviceControl("acquiring");
    try {
      const result = await acquireControlLease(room.code, lease, force);
      setControlSeq(result.seq);
      setDeviceControl("owned");
      setDeviceControlError("");
    } catch (err) {
      const conflict = isControlLeaseConflict(err);
      setDeviceControl(conflict ? "lost" : "unavailable");
      setDeviceControlError(
        conflict
          ? err instanceof Error ? err.message : "该席位已由另一设备控制。"
          : "设备控制绑定暂时失败，请检查网络并重试；当前页面不会自动接管其他设备。",
      );
    }
  }, [lease, mode, mySeat?.occupant_type, mySeat?.seat_key, room.code]);

  useEffect(() => {
    void acquireDeviceControl(false);
  }, [acquireDeviceControl]);

  useEffect(() => {
    if (deviceControl !== "owned" || controlSeq === null || !room.my_seat) return;
    const takenOver = room.recent_events.some(
      (event) => event.seq > controlSeq
        && event.type === "seat.control_taken_over"
        && event.payload.seat_key === room.my_seat,
    );
    if (takenOver) {
      setDeviceControl("lost");
      setDeviceControlError("该席位已由另一设备控制；确认现场无人使用旧设备后，可接管到本设备。");
    }
  }, [controlSeq, deviceControl, room.my_seat, room.recent_events]);

  function activeStreamDescriptor(source: Room = roomRef.current) {
    const active = source.active_speech;
    const generation = active?.stream_generation || "";
    const sampleRate = active?.stream_sample_rate || 0;
    const playbackStartedAt = active?.playback_started_at || "";
    if (
      source.status !== "running"
      || !active
      || active.speaker_type !== "ai"
      || !["synthesizing", "playing"].includes(active.status)
      || !generation
      || sampleRate <= 0
      || !playbackStartedAt
    ) return null;
    return {
      identity: `stream:${active.id}:${generation}`,
      speechId: active.id,
      generation,
      sampleRate,
      playbackStartedAt,
    };
  }

  useEffect(() => {
    if (["paused", "terminated", "completed", "review_required", "judging"].includes(room.status)) {
      disposePlayback();
      return;
    }
    const streamed = activeStreamDescriptor(room);
    if (streamed) {
      disposePlayback();
      return;
    }
    const stageStarted = [...room.recent_events].reverse().find((item) => {
      const startedStage = item.payload.stage as { key?: unknown } | undefined;
      return item.type === "stage.started" && startedStage?.key === room.current_stage?.key;
    });
    const stageStartedAt = stageStarted ? new Date(stageStarted.created_at).getTime() : Number.NaN;
    const cueAgeMs = Date.now() - stageStartedAt;
    const cueMayStart = room.status === "running"
      && room.current_stage?.kind === "announcement"
      && Number.isFinite(stageStartedAt)
      && cueAgeMs >= 0
      && cueAgeMs < 10_000;
    const cue = cueMayStart ? [...room.recent_events].reverse().find(
      (item) => item.type === "audio.cue.ready"
        && item.payload.stage_key === room.current_stage?.key
        && typeof item.payload.audio_url === "string",
    ) : null;
    const audioUrl = cue?.payload.audio_url as string | undefined;
    if (!audioUrl) {
      disposePlayback();
      return;
    }
    const identity = `cue:${room.current_stage?.key || ""}:${stageStarted?.seq || ""}:${stageStarted?.created_at || ""}:${cue?.seq || ""}:${cue?.created_at || ""}:${audioUrl}`;
    if (identity !== playbackIdentity.current) {
      disposePlayback();
      playbackIdentity.current = identity;
      const audio = new Audio(`${apiOrigin()}${audioUrl}`);
      audio.preload = "auto";
      const generation = ++playbackGeneration.current;
      const session: PlaybackSession = {
        audio,
        identity,
        generation,
        attemptToken: 0,
        playPending: false,
        ended: false,
        playbackStartedAtMs: null,
        durationSeconds: 0,
        stageStartedAtMs: stageStartedAt,
        expiresAtMs: stageStartedAt + 10_000,
        expiryTimer: null,
        retryingAfterMediaError: false,
        ignoreMediaErrorsUntilMs: 0,
        onError: () => {
          if (!isCurrentPlayback(session) || session.ended) return;
          if (Date.now() <= session.ignoreMediaErrorsUntilMs) return;
          invalidatePlaybackAttempt(session);
          setPlaybackNeedsGesture(false);
          setPlaybackError("比赛音频加载或解码失败，请点击“重试比赛声音”。");
        },
        onEnded: () => {
          if (!isCurrentPlayback(session)) return;
          if (session.expiryTimer !== null) {
            window.clearTimeout(session.expiryTimer);
            session.expiryTimer = null;
          }
          invalidatePlaybackAttempt(session);
          session.ended = true;
          setPlaybackNeedsGesture(false);
          setPlaybackError("");
        },
      };
      audio.addEventListener?.("error", session.onError);
      audio.addEventListener?.("ended", session.onEnded);
      playbackSession.current = session;
      setPlaybackNeedsGesture(false);
      setPlaybackError("");
      setPlaybackPending(false);
      if (session.expiresAtMs !== null) {
        session.expiryTimer = window.setTimeout(
          () => expirePlaybackSession(session),
          Math.max(0, session.expiresAtMs - Date.now()),
        );
      }
      const canPlay = catchUpPlayback(session);
      if (!muted && canPlay) attemptPlayback(session);
    }
  }, [room.recent_events, room.current_stage?.key, room.current_stage?.kind, room.active_speech?.id, room.active_speech?.status, room.active_speech?.stream_generation, room.status, muted]);

  function isCurrentPlayback(session: PlaybackSession) {
    return playbackSession.current === session
      && playbackGeneration.current === session.generation
      && playbackIdentity.current === session.identity;
  }

  function expirePlaybackSession(session: PlaybackSession) {
    if (!isCurrentPlayback(session) || session.ended) return;
    if (session.expiryTimer !== null) {
      window.clearTimeout(session.expiryTimer);
      session.expiryTimer = null;
    }
    const wasPending = session.playPending;
    invalidatePlaybackAttempt(session);
    session.ended = true;
    if (wasPending) {
      try { session.audio.pause(); } catch { /* Expired pending playback is best-effort cancelled. */ }
    }
    setPlaybackNeedsGesture(false);
    setPlaybackError("");
  }

  function catchUpPlayback(session: PlaybackSession) {
    if (session.ended) return false;
    if (session.expiresAtMs !== null && Date.now() >= session.expiresAtMs) {
      expirePlaybackSession(session);
      return false;
    }
    if (session.playbackStartedAtMs === null || !Number.isFinite(session.playbackStartedAtMs) || session.durationSeconds <= 0) return true;
    const elapsedSeconds = Math.max(0, (Date.now() - session.playbackStartedAtMs) / 1000);
    if (elapsedSeconds >= session.durationSeconds) {
      expirePlaybackSession(session);
      return false;
    }
    session.audio.currentTime = elapsedSeconds;
    return true;
  }

  function invalidatePlaybackAttempt(session: PlaybackSession) {
    session.attemptToken += 1;
    session.playPending = false;
    setPlaybackPending(false);
  }

  function settlePlaybackFailure(session: PlaybackSession, token: number, err: unknown) {
    if (!isCurrentPlayback(session) || session.attemptToken !== token) return;
    session.playPending = false;
    setPlaybackPending(false);
    const name = err instanceof DOMException ? err.name : err instanceof Error ? err.name : "";
    if (name === "AbortError") return;
    if (name === "NotAllowedError") {
      setPlaybackError("");
      setPlaybackNeedsGesture(true);
      return;
    }
    setPlaybackNeedsGesture(false);
    setPlaybackError("比赛音频播放失败，请点击“重试比赛声音”。");
  }

  function attemptPlayback(session: PlaybackSession) {
    if (!isCurrentPlayback(session) || session.ended || session.playPending) return;
    if (!catchUpPlayback(session)) return;
    const token = ++session.attemptToken;
    session.playPending = true;
    setPlaybackPending(true);
    let request: Promise<void>;
    try {
      request = session.audio.play();
    } catch (err) {
      settlePlaybackFailure(session, token, err);
      return;
    }
    void request.then(() => {
      if (!isCurrentPlayback(session) || session.attemptToken !== token) return;
      session.playPending = false;
      setPlaybackPending(false);
      setPlaybackNeedsGesture(false);
      setPlaybackError("");
      if (session.retryingAfterMediaError) {
        session.retryingAfterMediaError = false;
        session.ignoreMediaErrorsUntilMs = Date.now() + 500;
      }
    }, (err) => settlePlaybackFailure(session, token, err));
  }

  function disposePlayback(resetUi = true) {
    const session = playbackSession.current;
    playbackGeneration.current += 1;
    playbackSession.current = null;
    playbackIdentity.current = "";
    if (session) {
      session.attemptToken += 1;
      session.playPending = false;
      if (session.expiryTimer !== null) window.clearTimeout(session.expiryTimer);
      session.audio.removeEventListener?.("error", session.onError);
      session.audio.removeEventListener?.("ended", session.onEnded);
      try { session.audio.pause(); } catch { /* Best-effort media cleanup. */ }
      try { session.audio.removeAttribute?.("src"); } catch { /* Best-effort media cleanup. */ }
      try { session.audio.src = ""; } catch { /* Best-effort media cleanup. */ }
      try { session.audio.load?.(); } catch { /* Best-effort media cleanup. */ }
    }
    if (resetUi) {
      setPlaybackNeedsGesture(false);
      setPlaybackError("");
      setPlaybackPending(false);
    }
  }

  function togglePlaybackSound() {
    const session = playbackSession.current;
    if (session) {
      if (session.playPending) return;
      if (muted || playbackNeedsGesture || playbackError) {
        mutedRef.current = false;
        setMuted(false);
        setPlaybackError("");
        rtcAudio.current?.setMuted(false);
        if (playbackError) {
          session.retryingAfterMediaError = true;
          try { session.audio.load?.(); } catch { /* Retry can still call play below. */ }
        }
        attemptPlayback(session);
      } else {
        mutedRef.current = true;
        setMuted(true);
        rtcAudio.current?.setMuted(true);
        invalidatePlaybackAttempt(session);
        session.audio.pause();
      }
      return;
    }

    // The user may click before prepare() resolves. LiveKitRoomAudio creates
    // its AudioContext before requesting credentials, so route that trusted
    // gesture to the sole authoritative RTC path as soon as it exists.
    if (rtcAudio.current) {
      if (muted || playbackNeedsGesture || playbackError) {
        mutedRef.current = false;
        setMuted(false);
        setPlaybackError("");
        rtcAudio.current?.setMuted(false);
        const client = rtcAudio.current;
        const unlockRequest = client?.unlock() || Promise.reject(new Error("WebRTC 实时音频尚未初始化"));
        const reconnectRequest = playbackError
          ? client?.reconnect().then((restored) => {
            if (!restored) throw new Error("WebRTC 实时音频重连失败");
            setRtcAudioConnected(true);
            setPlaybackNotice("");
            const generation = roomRef.current.active_speech?.stream_generation || "";
            if (generation) client.activateGeneration(generation);
          })
          : Promise.resolve();
        void Promise.all([unlockRequest, reconnectRequest]).then(
          () => setPlaybackNeedsGesture(false),
          (error) => {
            setPlaybackNeedsGesture(false);
            setPlaybackError(error instanceof Error ? error.message : "WebRTC 实时音频重连失败");
          },
        );
      } else {
        mutedRef.current = true;
        setMuted(true);
        rtcAudio.current?.setMuted(true);
      }
      return;
    }
    setPlaybackError("WebRTC 实时音频尚未初始化，请稍后重试；比赛不会切换到其他播放器。");
  }

  function invalidateAsrSession(session: AsrSession | null) {
    if (!session) return;
    session.aborted = true;
    session.ready = false;
    session.preReadyChunks = [];
    session.preReadySamples = 0;
    session.settleReady(false);
    session.tailResolve?.();
    session.tailResolve = null;
    session.processor?.port.postMessage({ type: "stop", generation: session.generation });
    if (session.processor) session.processor.port.onmessage = null;
    session.processor?.disconnect?.();
    session.source?.disconnect?.();
    session.ws.close();
    if (asrSession.current === session) asrSession.current = null;
    if (asrSocket.current === session.ws) asrSocket.current = null;
  }

  useEffect(() => () => {
    startGeneration.current += 1;
    startController.current?.abort();
    startController.current = null;
    finishController.current?.abort();
    finishController.current = null;
    startingRef.current = false;
    captureActive.current = false;
    invalidateAsrSession(asrSession.current);
    if (asrReconnectTimer.current !== null) window.clearTimeout(asrReconnectTimer.current);
    asrReconnectTimer.current = null;
    disposePlayback(false);
    streamRef.current?.getTracks().forEach((track) => track.stop());
    asrSocket.current?.close();
    void audioContext.current?.close();
    audioContext.current = null;
  }, []);

  useEffect(() => {
    if (!rtcAudioConnected) return;
    const generation = room.active_speech?.stream_generation || "";
    if (generation) rtcAudio.current?.activateGeneration(generation);
  }, [room.active_speech?.stream_generation, rtcAudioConnected]);

  useEffect(() => {
    if (!rtcAudioConnected) return;
    const request = rtcFlushRequest(room, liveEvent);
    if (!request || rtcFlushIdentity.current === request.identity) return;
    rtcFlushIdentity.current = request.identity;
    rtcAudio.current?.flush(request.generation);
  }, [liveEvent, room, rtcAudioConnected]);

  useEffect(() => {
    if (!capturing || !speechId || timeoutHandled.current === speechId) return;
    const timedOut = room.recent_events.some(
      (event) => event.type === "speech.timed_out" && event.payload.speech_id === speechId,
    );
    const interrupted = room.recent_events.some(
      (event) => event.type === "speech.interrupted" && event.payload.speech_id === speechId,
    );
    if (timedOut) {
      timeoutHandled.current = speechId;
      void stopSpeaking(true);
    } else if (interrupted) {
      timeoutHandled.current = speechId;
      void abortSpeaking("比赛控制已中断本次发言，麦克风已关闭；本次未提交文字未计入比赛。");
    }
  }, [capturing, room.recent_events, speechId]);

  useEffect(() => {
    if (!capturing) return;
    if (deviceControl === "lost") {
      void abortSpeaking("当前设备已失去席位控制，麦克风已关闭；本次未提交文字未计入比赛。");
    } else if (mySeat && mySeat.occupant_type !== "human") {
      void abortSpeaking("该席位已进入历史只读状态，麦克风已关闭；本次未提交文字未计入比赛。");
    }
  }, [capturing, deviceControl, mySeat?.occupant_type]);

  useEffect(() => {
    if ((!startingRef.current && !capturing) || !captureStageIdentity.current) return;
    if (stageIdentity !== captureStageIdentity.current) {
      const reason = room.status === "paused"
        ? "比赛已暂停，麦克风和实时字幕已关闭；本次未提交文字未计入比赛。"
        : ["completed", "review_required", "terminated", "cancelled"].includes(room.status)
          ? "比赛已经结束，麦克风和实时字幕已关闭；本次未提交文字未计入比赛。"
          : "比赛阶段或自由辩论轮次已切换，麦克风已关闭；本次未提交文字未计入比赛。";
      void abortSpeaking(reason);
    }
  }, [capturing, room.status, stageIdentity]);

  useEffect(() => {
    if (!showSettings) return;
    const dialog = settingsDialog.current;
    const preferredButton = dialog?.querySelector<HTMLButtonElement>("[data-autofocus]:not(:disabled)") || dialog?.querySelector<HTMLButtonElement>("button:not(:disabled)");
    preferredButton?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setShowSettings(false);
        settingsButton.current?.focus();
        return;
      }
      if (event.key !== "Tab" || !dialog) return;
      const focusable = [...dialog.querySelectorAll<HTMLElement>("button:not(:disabled), [href], textarea:not(:disabled), [tabindex]:not([tabindex='-1'])")];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [showSettings]);

  useEffect(() => {
    if (!confirmation) return;
    confirmationCancel.current?.focus();
    const dialog = confirmationDialog.current;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        closeConfirmation();
        return;
      }
      if (event.key !== "Tab" || !dialog) return;
      const focusable = [...dialog.querySelectorAll<HTMLElement>("button:not(:disabled)")];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [confirmation]);

  useEffect(() => {
    if (!pendingFinish || finishing) return;
    if (pendingFinishMustDiscard) discardPendingButton.current?.focus();
    else pendingTranscript.current?.focus();
  }, [pendingFinish?.speechId, finishing, pendingFinishMustDiscard]);

  useEffect(() => {
    if (!pendingFinish || !finishing || !pendingFinishMustDiscard) return;
    finishController.current?.abort();
    finishController.current = null;
    setFinishing(false);
    setCaptureError("比赛阶段或发言记录已变化，已停止向旧环节提交。文字仍保留在本页供核对。");
  }, [finishing, pendingFinish, pendingFinishMustDiscard]);

  const hasPendingFinish = Boolean(pendingFinish);
  useEffect(() => {
    onPendingFinishChange?.(hasPendingFinish);
    return () => onPendingFinishChange?.(false);
  }, [hasPendingFinish, onPendingFinishChange]);

  async function startSpeaking() {
    if (startingRef.current || captureActive.current) return;
    startingRef.current = true;
    setStarting(true);
    const generation = ++startGeneration.current;
    const controller = new AbortController();
    startController.current?.abort();
    startController.current = controller;
    captureStageIdentity.current = stageIdentity;
    captureStageKey.current = room.current_stage?.key || "";
    captureStageName.current = room.current_stage?.name || "当前环节";
    startCancellationMessage.current = "";
    const recoveringExisting = room.active_speech?.speaker_type === "human" && room.active_speech.seat_key === room.my_seat;
    const recoveredTranscript = recoveringExisting ? room.active_speech?.content || "" : "";
    transcriptRef.current = recoveredTranscript;
    partialRef.current = "";
    voicedSamples.current = 0;
    observedSamples.current = 0;
    recoveredTranscriptBaseline.current = recoveredTranscript;
    asrRejectionMessage.current = "";
    asrReconnectAttempts.current = 0;
    if (asrReconnectTimer.current !== null) window.clearTimeout(asrReconnectTimer.current);
    asrReconnectTimer.current = null;
    forceTranscriptReview.current = false;
    stopPromise.current = null;
    stopServerTimedOut.current = false;
    setCaptureError(""); setPartial(""); setAsrCaption("");
    timeoutHandled.current = "";
    let stream: MediaStream | null = null;
    try {
      if (!navigator.mediaDevices?.getUserMedia) {
        throw new Error("当前浏览器不支持实时麦克风采集，请使用最新版 Chrome、Edge 或 Safari。");
      }
      let microphoneTimedOut = false;
      let timeoutId = 0;
      const microphoneRequest = navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      void microphoneRequest.then((lateStream) => {
        if (microphoneTimedOut || generation !== startGeneration.current || controller.signal.aborted) {
          lateStream.getTracks().forEach((track) => track.stop());
        }
      }, () => undefined);
      try {
        stream = await Promise.race([
          microphoneRequest,
          new Promise<never>((_resolve, reject) => {
            timeoutId = window.setTimeout(() => {
              microphoneTimedOut = true;
              reject(new DOMException("Microphone start timed out", "TimeoutError"));
            }, MICROPHONE_START_TIMEOUT_MS);
          }),
          new Promise<never>((_resolve, reject) => {
            controller.signal.addEventListener("abort", () => reject(new DOMException("Microphone start cancelled", "AbortError")), { once: true });
          }),
        ]);
      } finally {
        window.clearTimeout(timeoutId);
      }
      if (generation !== startGeneration.current || controller.signal.aborted) {
        stream.getTracks().forEach((track) => track.stop());
        throw new DOMException("Microphone start cancelled", "AbortError");
      }
      streamRef.current = stream;
      startKey.current ||= crypto.randomUUID();
      let speechStartTimedOut = false;
      const speechStartTimeoutId = window.setTimeout(() => {
        speechStartTimedOut = true;
        controller.abort();
      }, SPEECH_START_RESPONSE_TIMEOUT_MS);
      let data: { speech_id: string; resumed?: boolean };
      try {
        data = await apiFetch<{ speech_id: string; resumed?: boolean }>(`/api/rooms/${room.code}/speech/start`, {
          method: "POST",
          headers: { "X-Control-Lease": lease, "X-Idempotency-Key": startKey.current },
          body: "{}",
          signal: controller.signal,
        });
      } catch (err) {
        if (speechStartTimedOut) {
          throw new DOMException("Speech start response timed out", "SpeechStartTimeoutError");
        }
        throw err;
      } finally {
        window.clearTimeout(speechStartTimeoutId);
      }
      if (generation !== startGeneration.current || controller.signal.aborted) {
        throw new DOMException("Speech start cancelled", "AbortError");
      }
      setSpeechId(data.speech_id);
      finishKey.current = crypto.randomUUID();
      captureActive.current = true;
      await startAsr(stream, generation, data.speech_id, captureStageKey.current);
      if (
        generation !== startGeneration.current
        || controller.signal.aborted
        || !captureActive.current
        || streamRef.current !== stream
      ) {
        throw new DOMException("Speech capture cancelled", "AbortError");
      }
      setCapturing(true);
      startController.current = null;
      if (data.resumed && !asrRejectionMessage.current) setCaptureError("已恢复当前设备的进行中发言，先前字幕已保留。");
    } catch (err) {
      const ownsStream = Boolean(stream && streamRef.current === stream);
      if (stream && (ownsStream || (!startCancellationMessage.current && generation === startGeneration.current))) {
        stream.getTracks().forEach((track) => track.stop());
      }
      if (ownsStream) streamRef.current = null;
      if (generation === startGeneration.current || ownsStream) captureActive.current = false;
      const currentGeneration = generation === startGeneration.current;
      const cancelled = err instanceof DOMException
        && err.name === "AbortError"
        && (Boolean(startCancellationMessage.current) || controller.signal.aborted || !currentGeneration);
      if (!cancelled && currentGeneration) setCaptureError(microphoneErrorMessage(err));
    } finally {
      if (generation === startGeneration.current) {
        startingRef.current = false;
        setStarting(false);
        startController.current = null;
      }
    }
  }

  async function startAsr(
    stream: MediaStream,
    generation: number,
    expectedSpeechId: string,
    expectedStageKey: string,
    reusableSession?: AsrSession,
  ) {
    let context: AudioContext | null = reusableSession?.context || null;
    let source: MediaStreamAudioSourceNode | null = reusableSession?.source || null;
    let processor: AudioWorkletNode | null = reusableSession?.processor || null;
    let ws: WebSocket | null = null;
    try {
      if (!reusableSession) {
        context = await prepareAsrAudioGraph();
        if (context.state === "suspended") await context.resume();
        if (!Number.isFinite(context.sampleRate) || context.sampleRate <= 0) {
          throw new Error("invalid audio context sample rate");
        }
        if (generation !== startGeneration.current) {
          if (audioContext.current === context) audioContext.current = null;
          await context.close().catch(() => undefined);
          return;
        }
        source = context.createMediaStreamSource(stream);
        processor = new AudioWorkletNode(context, "jixia-asr-pcm-capture", {
          numberOfInputs: 1,
          numberOfOutputs: 1,
          outputChannelCount: [1],
          processorOptions: { chunkMs: ASR_CAPTURE_CHUNK_MS, generation },
        });
      }
      if (!context || !source || !processor) throw new Error("ASR audio graph is unavailable");
      const socket = new WebSocket(websocketUrl(`/ws/rooms/${room.code}/asr`));
      ws = socket;
      socket.binaryType = "arraybuffer";
      const session: AsrSession = reusableSession || {
        generation,
        ws: socket,
        context,
        source,
        processor,
        ready: false,
        aborted: false,
        stopping: false,
        overflowed: false,
        failed: false,
        finalCompleted: false,
        preReadyChunks: [],
        preReadySamples: 0,
        readyPromise: Promise.resolve(false),
        settleReady: () => undefined,
        tailResolve: null,
        resampler: { inputRate: context.sampleRate, buffer: new Float32Array(0), position: 0 },
      };
      session.ws = socket;
      session.context = context;
      session.source = source;
      session.processor = processor;
      if (!reusableSession) resetAsrReadyBarrier(session);
      asrSocket.current = socket;
      asrSession.current = session;
      const isCurrent = () => generation === startGeneration.current
        && asrSocket.current === socket
        && asrSession.current === session
        && !session.aborted;
      const rejectAsr = (message: string) => {
        if (!isCurrent()) return;
        session.failed = true;
        session.ready = false;
        session.preReadyChunks = [];
        session.preReadySamples = 0;
        session.settleReady(false);
        asrRejectionMessage.current ||= message;
        setCaptureError(asrRejectionMessage.current);
      };
      socket.onopen = () => {
        if (!isCurrent()) return;
        try {
          socket.send(JSON.stringify({
            type: "authenticate",
            lease,
            protocol_version: 2,
            encoding: "pcm_s16le",
            channels: 1,
            sample_rate: ASR_TARGET_SAMPLE_RATE,
            speech_id: expectedSpeechId,
            stage_key: expectedStageKey,
          }));
        } catch {
          rejectAsr("语音识别连接失败；请在结束后核对并补充发言文字。");
        }
      };
      socket.onmessage = (event) => {
        if (!isCurrent()) return;
        let data: { type?: string; text?: string; is_final?: boolean; reason?: string; speech_id?: string; stage_key?: string; protocol_version?: number };
        try {
          data = JSON.parse(event.data);
        } catch {
          return;
        }
        if (data.type === "ready") {
          if (session.failed) return;
          if (
            data.protocol_version !== 2
            || data.speech_id !== expectedSpeechId
            || data.stage_key !== expectedStageKey
          ) {
            rejectAsr("语音识别返回了不匹配的发言标识；连接已停止，请结束发言后核对并补充文字。");
            socket.close();
            return;
          }
          try {
            asrReconnectAttempts.current = 0;
            session.ready = true;
            for (const chunk of session.preReadyChunks) socket.send(chunk);
            session.preReadyChunks = [];
            session.preReadySamples = 0;
            session.settleReady(true);
          } catch {
            rejectAsr("语音识别音频发送失败；请在结束后核对并补充发言文字。");
          }
        } else if (data.type === "asr") {
          // A provider or browser event queue may deliver a partial after the
          // authoritative final. Never let that stale hypothesis replace the
          // final subtitle or enter the submitted transcript.
          if (session.finalCompleted) return;
          const recognized = data.text || "";
          if (data.is_final) {
            // The bridge defines one authoritative final per ASR context.
            // Ignore a duplicated delivery instead of appending the same
            // sentence twice to the submitted transcript.
            session.finalCompleted = true;
            transcriptRef.current = mergeAsrText(transcriptRef.current, recognized);
            partialRef.current = "";
            setAsrCaption(recognized.trim() ? compactCaptionLine(recognized) : "");
            setPartial("");
            asrFinalWaiter.current?.();
            asrFinalWaiter.current = null;
          } else {
            partialRef.current = recognized;
            setAsrCaption(recognized.trim() ? compactCaptionLine(recognized) : "");
            setPartial(recognized);
          }
        } else if (data.type === "asr_rejected") {
          session.finalCompleted = true;
          transcriptRef.current = recoveredTranscriptBaseline.current;
          partialRef.current = "";
          setAsrCaption("");
          setPartial("");
          asrRejectionMessage.current = asrRejectedMessage(data.reason);
          setCaptureError(asrRejectionMessage.current);
          asrFinalWaiter.current?.();
          asrFinalWaiter.current = null;
        } else if (data.type === "error") {
          rejectAsr("语音识别暂时不可用；请在结束后核对并补充发言文字。");
          asrFinalWaiter.current?.();
          asrFinalWaiter.current = null;
        }
      };
      socket.onerror = () => {
        if (!isCurrent()) return;
        // A transient browser/network error is followed by onclose. Keep the
        // capture alive and let that handler establish a fresh duplex ASR
        // stream. Confirmed transcript text is the durable recovery path.
        if (captureActive.current && !session.stopping) {
          asrRejectionMessage.current ||= ASR_RECONNECTING_MESSAGE;
          setCaptureError(asrRejectionMessage.current);
          return;
        }
        rejectAsr("语音识别连接失败；请在结束后核对并补充发言文字。");
        asrFinalWaiter.current?.();
        asrFinalWaiter.current = null;
      };
      socket.onclose = (event) => {
        if (!isCurrent()) return;
        if (!session.ready) session.settleReady(false);
        if (captureActive.current && !session.stopping && event.code !== 4409
          && generation === startGeneration.current
          && asrReconnectAttempts.current < ASR_RECONNECT_MAX_ATTEMPTS) {
          session.ready = false;
          resetAsrReadyBarrier(session);
          const interruptedPartial = partialRef.current.trim();
          if (interruptedPartial) {
            transcriptRef.current = mergeAsrText(transcriptRef.current, interruptedPartial);
            recoveredTranscriptBaseline.current = transcriptRef.current;
          }
          partialRef.current = "";
          setAsrCaption("");
          setPartial("");
          asrRejectionMessage.current = ASR_RECONNECTED_REVIEW_MESSAGE;
          setCaptureError(ASR_RECONNECTED_REVIEW_MESSAGE);
          const attempt = asrReconnectAttempts.current++;
          const delay = ASR_RECONNECT_BASE_DELAY_MS * (2 ** attempt);
          if (asrReconnectTimer.current === null) {
            asrReconnectTimer.current = window.setTimeout(() => {
              asrReconnectTimer.current = null;
              if (captureActive.current && generation === startGeneration.current) {
                void startAsr(stream, generation, expectedSpeechId, expectedStageKey, session);
              }
            }, delay);
          }
          return;
        }
        if (captureActive.current && !session.stopping && generation === startGeneration.current) {
          // Do not leave the user in an endless "正在重连" state after the
          // bounded retry budget is exhausted. Keep the microphone session
          // alive so they can finish, but make the fallback explicit: this
          // turn will require text review rather than silently losing ASR.
          session.failed = true;
          session.ready = false;
          session.settleReady(false);
          asrRejectionMessage.current = "字幕连接已中断，请结束发言后核对并补充文字。";
          setCaptureError(asrRejectionMessage.current);
        }
        if (session.stopping && !session.finalCompleted) {
          asrRejectionMessage.current ||= "语音识别连接在最终字幕返回前中断，请核对并补充本次发言文字。";
          setCaptureError(asrRejectionMessage.current);
        }
        asrFinalWaiter.current?.();
        asrFinalWaiter.current = null;
        if (event.code === 4409 && captureActive.current) {
          void abortSpeaking("当前设备已失去席位控制，麦克风已关闭；本次未提交文字未计入比赛。");
        }
      };
      processor.port.onmessage = (event: MessageEvent<{ type?: string; generation?: number; frames?: Float32Array }>) => {
        const message = event.data || {};
        if (!isCurrent() || message.generation !== generation) return;
        if (message.type === "flushed") {
          session.tailResolve?.();
          session.tailResolve = null;
          return;
        }
        if (message.type !== "frames" || !(message.frames instanceof Float32Array)) return;
        const input = resampleForAsr(message.frames, session.resampler);
        const pcm = new Int16Array(input.length);
        let squared = 0; let peak = 0;
        for (let i=0;i<input.length;i++) {
          const sample = Math.max(-1, Math.min(1,input[i]));
          squared += sample * sample;
          peak = Math.max(peak, Math.abs(sample));
          pcm[i] = sample * 0x7fff;
        }
        observedSamples.current += input.length;
        const rms = input.length ? Math.sqrt(squared / input.length) : 0;
        if (rms >= VOICE_RMS_THRESHOLD && peak >= VOICE_PEAK_THRESHOLD) voicedSamples.current += input.length;
        if (pcm.length) {
          const chunk = pcm.slice().buffer;
          try {
            if (socket.readyState === WebSocket.OPEN && session.ready) {
              if (socket.bufferedAmount > ASR_SOCKET_MAX_BUFFERED_BYTES) {
                session.overflowed = true;
                asrRejectionMessage.current ||= "字幕网络发送缓慢，部分音频未能及时识别；结束后请核对并补充文字。";
                setCaptureError(asrRejectionMessage.current);
              } else {
                socket.send(chunk);
              }
            } else if (!session.failed) {
              session.preReadyChunks.push(chunk);
              session.preReadySamples += pcm.length;
              while (session.preReadySamples > ASR_PREROLL_MAX_SAMPLES && session.preReadyChunks.length) {
                const dropped = session.preReadyChunks.shift();
                session.preReadySamples -= dropped ? dropped.byteLength / 2 : 0;
                session.overflowed = true;
              }
              if (session.overflowed) {
                asrRejectionMessage.current ||= "语音识别连接较慢，开头内容可能未完整识别；请在结束后核对文字。";
                setCaptureError(asrRejectionMessage.current);
              }
            }
          } catch {
            rejectAsr("语音识别音频发送失败；请在结束后核对并补充发言文字。");
          }
        }
      };
      if (!reusableSession) {
        source.connect(processor);
        processor.connect(context.destination);
      }
    } catch {
      const message = "当前浏览器无法启动实时字幕；请在结束后补充本次发言文字。";
      asrRejectionMessage.current ||= message;
      setCaptureError(asrRejectionMessage.current);
      const session = asrSession.current;
      if (session?.generation === generation) {
        session.aborted = true;
        session.settleReady(false);
        session.preReadyChunks = [];
        session.preReadySamples = 0;
        asrSession.current = null;
      }
      processor?.disconnect?.();
      source?.disconnect?.();
      ws?.close();
      if (asrSocket.current === ws) asrSocket.current = null;
      await context?.close().catch(() => undefined);
      if (audioContext.current === context) audioContext.current = null;
    }
  }

  function stopSpeaking(serverTimedOut = false, reviewTranscript = false): Promise<void> {
    if (serverTimedOut) stopServerTimedOut.current = true;
    if (reviewTranscript) forceTranscriptReview.current = true;
    if (stopPromise.current) return stopPromise.current;
    const task = performStopSpeaking();
    stopPromise.current = task;
    void task.finally(() => {
      if (stopPromise.current === task) stopPromise.current = null;
    });
    return task;
  }

  async function performStopSpeaking() {
    const generation = startGeneration.current;
    captureActive.current = false;
    if (asrReconnectTimer.current !== null) window.clearTimeout(asrReconnectTimer.current);
    asrReconnectTimer.current = null;
    setCapturing(false);
    setFinishing(true);
    const session = asrSession.current?.generation === generation ? asrSession.current : null;
    const socket = session?.ws || asrSocket.current;
    if (session) {
      session.stopping = true;
      const tailDrained = new Promise<void>((resolve) => { session.tailResolve = resolve; });
      session.processor?.port.postMessage({ type: "flush", generation });
      await Promise.race([
        tailDrained,
        new Promise<void>((resolve) => window.setTimeout(resolve, ASR_TAIL_DRAIN_MS)),
      ]);
      session.tailResolve = null;
      if (session.processor) session.processor.port.onmessage = null;
      session.processor?.disconnect?.();
      session.source?.disconnect?.();
    }
    let waitForFinal: Promise<void> | null = null;
    streamRef.current?.getTracks().forEach((track) => track.stop()); streamRef.current = null;
    let asrReady = Boolean(session?.ready);
    if (session && !asrReady && !session.failed && !session.aborted) {
      asrReady = await Promise.race([
        session.readyPromise,
        new Promise<boolean>((resolve) => window.setTimeout(() => resolve(false), ASR_READY_STOP_WAIT_MS)),
      ]);
    }
    if (session && asrReady && !session.aborted && socket?.readyState === WebSocket.OPEN) {
      try {
        waitForFinal = new Promise<void>((resolve) => { asrFinalWaiter.current = resolve; });
        socket.send(JSON.stringify({ type: "finish" }));
      } catch {
        asrRejectionMessage.current ||= "语音识别结束信号发送失败，请核对并补充发言文字。";
        asrFinalWaiter.current?.();
        asrFinalWaiter.current = null;
        waitForFinal = null;
      }
    } else if (session && !session.aborted && !session.failed) {
      asrRejectionMessage.current ||= "语音识别连接未及时就绪，请核对并补充本次发言文字。";
      setCaptureError(asrRejectionMessage.current);
    }
    if (waitForFinal) {
      const finalReceived = await Promise.race([
        waitForFinal.then(() => true),
        new Promise<boolean>((resolve) => window.setTimeout(() => resolve(false), ASR_FINAL_WAIT_MS)),
      ]);
      if (!finalReceived && !partialRef.current.trim()) {
        asrRejectionMessage.current ||= "语音识别最终字幕未及时返回，请核对并补充本次发言文字。";
        setCaptureError(asrRejectionMessage.current);
      }
    }
    asrFinalWaiter.current = null;
    if (session) {
      session.aborted = true;
      session.preReadyChunks = [];
      session.preReadySamples = 0;
      session.settleReady(false);
    }
    socket?.close();
    if (asrSocket.current === socket) asrSocket.current = null;
    if (asrSession.current === session) asrSession.current = null;
    if (generation !== startGeneration.current) {
      setFinishing(false);
      return;
    }
    const voiceState: VoiceState = observedSamples.current === 0
      ? "unknown"
      : voicedSamples.current >= MIN_VOICED_SAMPLES
        ? "voice"
        : "silence";
    const hasUnconfirmedPartial = Boolean(partialRef.current.trim());
    const recognizedContent = transcriptRef.current.trim();
    const content = voiceState === "silence" ? recoveredTranscriptBaseline.current : recognizedContent;
    const pending: PendingFinish = {
      content,
      voiceState,
      speechId,
      stageKey: captureStageKey.current,
      stageName: captureStageName.current,
    };
    setPendingFinish(pending);
    if (forceTranscriptReview.current || !content || hasUnconfirmedPartial || asrRejectionMessage.current) {
      forceTranscriptReview.current = false;
      setFinishing(false);
      setCaptureError(asrRejectionMessage.current || (hasUnconfirmedPartial
        ? "语音识别未确认最后一段字幕，请核对并补充本次发言文字。"
        : content
          ? "请核对并修正语音识别文字，确认后再提交本次发言。"
        : stopServerTimedOut.current
        ? "本轮时间已到，但未识别到字幕。请补充发言文字后提交。"
        : voiceState === "voice"
          ? "未识别到可用字幕。请补充发言文字后提交。"
          : voiceState === "silence"
            ? "未检测到清晰语音；请补充本次发言文字后提交。"
            : "未能完成语音活动检测；请补充本次发言文字后提交。"
      ));
      return;
    }
    forceTranscriptReview.current = false;
    const submitted = await submitFinish(pending);
    if (stopServerTimedOut.current) {
      setCaptureError(submitted ? "本轮发言时间已到，识别文字已自动提交。" : "本轮发言时间已到；文字仍保留在当前页面，可点击重试提交。");
    }
  }

  async function abortSpeaking(message: string) {
    if (!startingRef.current && !captureActive.current && !streamRef.current) return;
    const pendingAudioContext = startingRef.current && !asrSession.current ? audioContext.current : null;
    if (pendingAudioContext) {
      audioContext.current = null;
      void pendingAudioContext.close().catch(() => undefined);
    }
    startCancellationMessage.current = message;
    startGeneration.current += 1;
    startController.current?.abort();
    startController.current = null;
    finishController.current?.abort();
    finishController.current = null;
    captureActive.current = false;
    setCapturing(false);
    invalidateAsrSession(asrSession.current);
    const socket = asrSocket.current;
    asrSocket.current = null;
    socket?.close();
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    transcriptRef.current = "";
    partialRef.current = "";
    setAsrCaption("");
    voicedSamples.current = 0;
    observedSamples.current = 0;
    recoveredTranscriptBaseline.current = "";
    asrRejectionMessage.current = "";
    forceTranscriptReview.current = false;
    asrFinalWaiter.current?.();
    asrFinalWaiter.current = null;
    setPendingFinish(null);
    setFinishing(false);
    startingRef.current = false;
    setStarting(false);
    setSpeechId("");
    startKey.current = "";
    captureStageIdentity.current = "";
    captureStageKey.current = "";
    captureStageName.current = "";
    setCaptureError(message);
  }

  function discardPendingFinishAndViewResult() {
    startGeneration.current += 1;
    startController.current?.abort();
    startController.current = null;
    finishController.current?.abort();
    finishController.current = null;
    startingRef.current = false;
    captureActive.current = false;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    invalidateAsrSession(asrSession.current);
    asrSocket.current?.close();
    asrSocket.current = null;
    void audioContext.current?.close().catch(() => undefined);
    audioContext.current = null;
    disposePlayback();
    transcriptRef.current = "";
    partialRef.current = "";
    setAsrCaption("");
    recoveredTranscriptBaseline.current = "";
    asrRejectionMessage.current = "";
    forceTranscriptReview.current = false;
    asrFinalWaiter.current?.();
    asrFinalWaiter.current = null;
    finishKey.current = "";
    startKey.current = "";
    captureStageIdentity.current = "";
    captureStageKey.current = "";
    captureStageName.current = "";
    setPendingFinish(null);
    setFinishing(false);
    setStarting(false);
    setCapturing(false);
    setSpeechId("");
    setPartial("");
    setCaptureError("");
    onPendingFinishChange?.(false);
  }

  function submitFinish(pending: PendingFinish): Promise<boolean> {
    if (finishPromise.current) return finishPromise.current;
    const task = performSubmitFinish(pending);
    finishPromise.current = task;
    void task.finally(() => {
      if (finishPromise.current === task) finishPromise.current = null;
    });
    return task;
  }

  async function performSubmitFinish(pending: PendingFinish) {
    setCaptureError("");
    const currentRoom = roomRef.current;
    const currentSpeechStatus = currentRoom.speeches.find((item) => item.id === pending.speechId)?.status;
    if (
      currentRoom.current_stage?.key !== pending.stageKey
      || (currentSpeechStatus && ["completed", "interrupted", "timed_out"].includes(currentSpeechStatus))
      || (currentRoom.active_speech && currentRoom.active_speech.id !== pending.speechId)
    ) {
      setCaptureError("比赛阶段或发言记录已经变化，这份文字不能提交到旧环节。内容仍保留在本页供核对。请清除后按当前轮次重新开始发言。");
      return false;
    }
    setFinishing(true);
    const controller = new AbortController();
    finishController.current = controller;
    try {
      await apiFetch<{ speech_id?: string }>(`/api/rooms/${room.code}/speech/finish`, {
        method: "POST",
        headers: { "X-Control-Lease": lease, "X-Idempotency-Key": finishKey.current },
        body: JSON.stringify({ speech_id: pending.speechId, content: pending.content }),
        signal: controller.signal,
      });
      const latestRoom = roomRef.current;
      const latestSpeechStatus = latestRoom.speeches.find((item) => item.id === pending.speechId)?.status;
      if (
        latestRoom.current_stage?.key !== pending.stageKey
        || (latestSpeechStatus && ["completed", "interrupted", "timed_out"].includes(latestSpeechStatus))
        || (latestRoom.active_speech && latestRoom.active_speech.id !== pending.speechId)
      ) {
        setCaptureError("提交响应返回时比赛阶段或发言记录已经变化，页面不会把这份文字视为当前环节发言。请清除后按当前轮次重新开始。");
        return false;
      }
      setPendingFinish(null);
      setSpeechId("");
      startKey.current = "";
      captureStageIdentity.current = "";
      captureStageKey.current = "";
      captureStageName.current = "";
      recoveredTranscriptBaseline.current = "";
      return true;
    } catch (err) {
      if (controller.signal.aborted) return false;
      setCaptureError(`${err instanceof Error ? err.message : "提交失败"}。发言文字仍保留在当前页面，可点击重试。`);
      return false;
    } finally {
      if (finishController.current === controller) {
        finishController.current = null;
        setFinishing(false);
      }
    }
  }

  async function fullscreen() {
    try {
      if (!document.fullscreenEnabled) throw new Error("当前浏览器不支持网页全屏");
      if (!document.fullscreenElement) await document.documentElement.requestFullscreen();
      else await document.exitFullscreen();
    } catch (err) {
      setCaptureError(err instanceof Error ? err.message : "无法切换全屏模式");
    }
  }

  async function controlMatch(action: "pause" | "resume" | "terminate") {
    if (roomActionInFlight.current) return;
    roomActionInFlight.current = action;
    setRoomActionBusy(action);
    setRoomActionError("");
    setRoomActionStatus("");
    const operationKey = roomActionAttempts.current[action] || crypto.randomUUID();
    roomActionAttempts.current[action] = operationKey;
    try {
      const result = await apiFetch<{ room: Room }>(`/api/rooms/${room.code}/control/${action}`, {
        method: "POST",
        headers: { "X-Idempotency-Key": operationKey },
        body: JSON.stringify({ reason: "房主在比赛舞台执行必要控制" }),
      });
      delete roomActionAttempts.current[action];
      onRoomChanged?.(result.room);
      setRoomActionStatus(action === "pause" ? "比赛已暂停。" : action === "resume" ? "比赛已继续。" : "比赛已提前结束。");
      if (action === "terminate") setShowSettings(false);
    } catch (err) {
      setRoomActionError(err instanceof Error ? err.message : "比赛操作失败，请重试。");
      if (action === "terminate") setShowSettings(true);
    } finally {
      roomActionInFlight.current = "";
      setRoomActionBusy("");
      setConfirmation(null);
    }
  }

  const localSpeechOwned = Boolean(
    speechId
      && (capturing || finishing || pendingFinish)
      && (!room.active_speech || (
        room.active_speech.id === speechId
          && room.active_speech.seat_key === room.my_seat
          && room.active_speech.speaker_type === "human"
      )),
  );
  const deviceReason = localSpeechOwned
    ? capturing
      ? "当前设备正在发言"
      : finishing
        ? "当前设备正在整理发言…"
        : "当前设备已保留本次发言"
    : deviceControl === "acquiring"
      ? "正在绑定当前设备…"
      : deviceControl === "lost"
        ? deviceControlError || "该席位已在其他设备接管"
        : deviceControl === "unavailable" && Boolean(room.my_seat) && mySeat?.occupant_type === "human"
          ? deviceControlError || "设备控制绑定暂时失败"
          : systemFacingCopy(room.speak_reason);
  const canRecoverSpeaking = !terminal && room.active_speech?.speaker_type === "human" && room.active_speech.seat_key === room.my_seat;
  const canStartSpeaking = !terminal && connected && (room.can_speak || canRecoverSpeaking) && deviceControl === "owned";
  const freeTurnWaitingToStart = Boolean(
    !terminal
      && room.current_stage?.kind === "free"
      && !stagePreparing
      && !room.current_stage.turn_started_at
      && !room.active_speech,
  );
  const readyReason = !connected
    ? "实时连接已断开，请等待重连后再发言"
    : room.current_stage?.kind === "free" && turnRemaining !== null
    ? freeTurnWaitingToStart
      ? `${deviceReason} · ${room.can_speak ? "点击开始发言后计时" : "本轮尚未开始"} · 单轮时长 ${formatTime(turnRemaining)}`
      : `${deviceReason} · 本轮剩余 ${formatTime(turnRemaining)}`
    : deviceReason;
  const hasLocalSpeechWork = starting || capturing || finishing || Boolean(pendingFinish);
  const humanSpeaking = room.active_speech?.speaker_type === "human";
  const disconnectedHumans = room.seats.filter((seat) => seat.occupant_type === "human" && !seat.connected);
  const disconnectedHumanNames = disconnectedHumans.map((seat) => seat.display_name).join("、");
  const disconnectGraceActive = disconnectedHumans.length > 0 && ["preparing", "running", "judging"].includes(room.status);
  const disconnectPauseTiming = disconnectGraceTimingLabel(disconnectGraceRemaining);
  const participantDisconnectPaused = !terminal && room.status === "paused" && room.pause_health?.reason_code === "participant_disconnected";
  const participantDisconnectAndFailurePaused = !terminal && room.status === "paused" && room.pause_health?.reason_code === "service_failure_and_participant_disconnected";
  const participantDisconnectInvolved = participantDisconnectPaused || participantDisconnectAndFailurePaused;
  const recoveryBlockedByDisconnect = participantDisconnectInvolved && disconnectedHumans.length > 0;
  const settingsLabel = mode === "watch"
    ? "观看设置"
    : room.can_control
      ? "比赛控制"
      : "更多操作";
  const canPauseMatch = room.can_control && ["running", "judging"].includes(room.status) && !humanSpeaking && !hasLocalSpeechWork;
  const canResumeMatch = room.can_control && room.status === "paused" && (!room.failure_reason || participantDisconnectPaused) && disconnectedHumans.length === 0 && !hasLocalSpeechWork;
  const canTerminateMatch = room.can_control && ["preparing", "running", "paused", "judging"].includes(room.status) && !hasLocalSpeechWork;
  const serviceFailurePaused = !terminal && Boolean(room.failure_reason) && !participantDisconnectPaused;
  const announcementActive = room.status === "running" && room.current_stage?.kind === "announcement";
  const stageStatusLabel = participantDisconnectPaused
    ? "真人断线暂停"
    : serviceFailurePaused
    ? "服务异常暂停"
    : roomStatusLabel[room.status] || room.status;
  const stageHeading = participantDisconnectPaused
    ? disconnectedHumans.length
      ? "等待真人辩手重新连接"
      : "全部真人已重新连接"
    : serviceFailurePaused
    ? room.current_stage?.name
      ? `${room.current_stage.name} · 等待恢复`
      : "比赛已安全暂停"
      : room.status === "preparing"
      ? "比赛准备中"
      : terminal
      ? roomStatusLabel[room.status] || "比赛已经结束"
      : room.current_stage?.name || "比赛即将开始";
  const currentSpeaker = terminal
    ? null
    : room.seats.find((seat) => seat.seat_key === activeSeatKey) || null;
  // A paused state must take precedence over a stage cue. Otherwise a cue
  // left on the current stage can look like a live caption after a disconnect
  // or service pause, which is confusing and visually resembles stale ASR.
  const idleSubtitleText = room.status === "paused"
    ? "比赛已暂停，恢复后将从当前进度继续。"
    : ["completed", "review_required", "terminated", "cancelled"].includes(room.status)
      ? "比赛已经结束，发言和实时字幕已停止。"
    : systemFacingCopy(room.current_stage?.cue)
      || (room.current_stage?.kind === "free"
        ? `${room.current_stage.side === "neg" ? "反方" : "正方"}可开始本轮发言。`
        : currentSpeaker
          ? `等待${currentSpeaker.display_name}开始发言。`
          : "等待下一位辩手开始发言。");
  const subtitleAttribution = room.active_speech
    ? terminal
      ? "比赛终态"
      : currentSpeaker?.display_name || "当前发言"
    : terminal
      ? "比赛终态"
    : room.current_stage?.kind === "free"
      ? `${room.current_stage.side === "neg" ? "反方" : "正方"}当前轮次`
      : currentSpeaker
        ? `${currentSpeaker.display_name} · 待发言`
        : "自动赛程";
  const watchFocusLabel = participantDisconnectPaused
    ? disconnectedHumans.length
      ? "比赛进度已保存，等待全部真人返回"
      : "比赛进度已保存，等待房主继续"
    : serviceFailurePaused
    ? "比赛进度已保存，恢复后将自动继续"
    : terminal
      ? room.status === "review_required"
        ? "比赛流程已结束，赛果等待人工复核"
        : room.status === "cancelled"
          ? "房间已取消，比赛不会继续"
          : room.status === "terminated"
            ? "比赛已提前终止，不会继续发言"
            : "比赛已完成"
    : announcementActive
      ? "系统正在播放阶段提示"
    : awaitingHumanStart
      ? `${currentSpeaker?.display_name || "当前辩手"}点击“开始发言”后正式计时`
    : aiPreparing
      ? "AI 正在准备本轮发言"
      : room.active_speech
        ? "正在发言"
        : room.status === "paused"
          ? "比赛暂停中"
          : "等待下一位辩手";
  const preparingMessage = "正在建立实时语音并加载预设开场提示；此时不会开始比赛计时，也不需要点击发言，完成后会自动进入第一阶段。";
  const blockedSpeakLabel = !connected
    ? "等待实时连接"
    : terminal
      ? "比赛已结束"
      : room.status === "preparing"
        ? "比赛准备中"
        : room.status === "paused"
          ? "比赛已暂停"
          : announcementActive
            ? "系统提示播放中"
            : aiPreparing
              ? "AI 正在准备"
              : room.active_speech?.speaker_type === "ai"
                ? "AI 正在发言"
                : room.active_speech?.speaker_type === "human"
                  ? `${currentSpeaker?.display_name || "其他辩手"}正在发言`
                  : room.status === "judging"
                    ? "正在评审"
                    : deviceControl === "acquiring"
                      ? "绑定设备中"
                      : deviceControl === "lost"
                        ? "其他设备已接管"
                        : deviceControl === "unavailable" && room.my_seat
                          ? "设备绑定失败"
                          : !room.my_seat
                            ? "当前为只读状态"
                            : "等待轮次";
  const blockedSpeakDetail = !connected
    ? readyReason
    : terminal
      ? "比赛流程已经停止，不能继续发言"
      : room.status === "preparing"
        ? "系统完成语音连接与预设提示后会自动开场"
        : room.status === "paused"
          ? serviceFailurePaused
            ? "比赛进度已保存，请等待房主处理异常"
            : "比赛进度已保存，继续后仍从当前环节开始"
          : announcementActive
            ? "提示播放完成后会自动进入下一项"
            : aiPreparing
              ? `正在生成本轮内容和语音，无需手动操作${turnRemaining === null ? "" : ` · 本轮剩余 ${formatTime(turnRemaining)}`}`
              : room.active_speech?.speaker_type === "ai"
                ? `语音播放结束后系统会自动推进${turnRemaining === null ? "" : ` · 本轮剩余 ${formatTime(turnRemaining)}`}`
                : room.active_speech?.speaker_type === "human"
                  ? `当前发言结束后系统会显示下一步${turnRemaining === null ? "" : ` · 本轮剩余 ${formatTime(turnRemaining)}`}`
                  : room.status === "judging"
                    ? "比赛发言已结束，结果生成后自动跳转"
                    : deviceControl === "acquiring"
                      ? "正在确认本浏览器的席位控制权，完成后自动更新可用操作"
                      : deviceControl === "lost" || (deviceControl === "unavailable" && room.my_seat)
                        ? deviceReason
                        : readyReason;
  const showOwnerQuickControl = room.can_control
    && !terminal
    && (room.status === "paused" || canPauseMatch);
  const quickControlReason = roomActionBusy
    ? "正在处理上一项比赛操作"
    : hasLocalSpeechWork
      ? "请先提交或清除本页保留的发言文字"
      : room.status === "paused"
        ? serviceFailurePaused
          ? recoveryBlockedByDisconnect
            ? "等待全部真人重新连接后重试当前步骤"
            : "服务异常暂停需要先重试当前步骤"
          : disconnectedHumans.length
            ? "等待全部真人重新连接"
            : "继续比赛"
        : humanSpeaking
          ? "真人发言结束后可暂停"
          : "暂停比赛";
  const terminateMatchReason = hasLocalSpeechWork
    ? "请先提交或清除本页保留的发言文字，避免误删尚未提交的内容"
    : canTerminateMatch
      ? "提前结束比赛"
      : "当前比赛状态不能提前结束";

  useEffect(() => {
    if (room.status === "paused") delete roomActionAttempts.current.pause;
    if (room.status === "running") delete roomActionAttempts.current.resume;
    if (["completed", "review_required", "terminated", "cancelled"].includes(room.status)) delete roomActionAttempts.current.terminate;
  }, [room.status, room.seq]);

  useEffect(() => {
    if (!hasLocalSpeechWork) return;
    const protectLocalRecording = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", protectLocalRecording);
    return () => window.removeEventListener("beforeunload", protectLocalRecording);
  }, [hasLocalSpeechWork]);

  useEffect(() => {
    if (!hasLocalSpeechWork) return;
    const guardKey = crypto.randomUUID();
    const guardedUrl = window.location.href;
    window.history.pushState({ ...window.history.state, jixiaSpeechGuard: guardKey }, "", guardedUrl);
    const protectBackNavigation = () => {
      window.history.pushState({ ...window.history.state, jixiaSpeechGuard: guardKey }, "", guardedUrl);
      setRoomActionError("当前发言尚未提交，请先结束并提交后再离开比赛页面。");
      setShowSettings(true);
    };
    window.addEventListener("popstate", protectBackNavigation);
    return () => {
      window.removeEventListener("popstate", protectBackNavigation);
      if (window.history.state?.jixiaSpeechGuard === guardKey) window.history.back();
    };
  }, [hasLocalSpeechWork]);

  function requestLeave(origin: "brand" | "settings") {
    if (hasLocalSpeechWork) {
      setRoomActionError("请先结束并提交当前发言，再退出比赛页面。");
      setShowSettings(true);
      return;
    }
    confirmationOrigin.current = origin;
    setShowSettings(false);
    setConfirmation("leave");
  }

  function requestTerminate() {
    confirmationOrigin.current = "settings";
    setShowSettings(false);
    setConfirmation("terminate");
  }

  function requestRetry() {
    confirmationOrigin.current = "settings";
    setShowSettings(false);
    setConfirmation("retry");
  }

  function closeConfirmation() {
    setConfirmation(null);
    if (confirmationOrigin.current === "settings") setShowSettings(true);
    else window.requestAnimationFrame(() => brandButton.current?.focus());
  }

  return (
    <div className={`stage-page ${mode === "watch" ? "watch-mode" : "debate-mode"}`}>
      <div className="stage-gridlines" />
      <header className="stage-topbar">{mode === "debate" ? <button ref={brandButton} type="button" className="stage-brand" aria-label="退出比赛页面" onClick={() => requestLeave("brand")}><span>辩</span><strong>稷下辩论</strong></button> : <Link href="/" className="stage-brand" aria-label="返回稷下辩论赛事大厅"><span>辩</span><strong>稷下辩论</strong></Link>}<div className="stage-topic"><small>{competitionDisplayName(room.competition)} · 房间 {room.code}</small><strong>{room.topic}</strong></div><div className={`connection ${connected ? "ok" : connectionBlockedReason ? "blocked" : ""}`} role="status" aria-live="polite">{connected ? <Wifi size={15}/> : <WifiOff size={15}/>} {connected ? "实时连接" : connectionBlockedReason === "capacity_full" ? "观战已满" : "重连中"}</div></header>
      <section className="stage-arena">
        <aside className="team-column aff"><div className="team-title"><span>正方</span><strong>PROPOSITION</strong></div>{aff.map((seat) => <SeatCard seat={seat} active={activeSeatKey === seat.seat_key || activeSeatKey === "aff_"} key={seat.seat_key} />)}</aside>
        <main className="stage-center">
          <div className="stage-status"><span className={`badge stage-status-badge ${["running", "judging"].includes(room.status) && !serviceFailurePaused ? "live" : ""}`}><i aria-hidden="true" />{stageStatusLabel}</span><h1>{stageHeading}</h1><small className="stage-status-note" aria-live="polite">{terminal ? watchFocusLabel : playbackNotice || (serviceFailurePaused ? "比赛进度已保存，等待房主处理" : room.status === "preparing" ? preparingMessage : watchFocusLabel)}</small></div>
          {!terminal && currentSpeaker && room.status !== "preparing" && (
            <div className={`speaker-focus ${currentSpeaker.side}`} aria-label={`当前发言人：${currentSpeaker.display_name}`}>
              <span className="speaker-wave" aria-hidden="true">{Array.from({ length: 9 }, (_, index) => <i key={`left-${index}`} />)}</span>
              <span className="speaker-focus-core">
                <span className="speaker-focus-avatar">{currentSpeaker.display_name.slice(0, 1)}</span>
                <strong>{currentSpeaker.display_name}</strong>
                <small>{currentSpeaker.label} · {currentSpeaker.occupant_type === "human" ? "人类辩手" : "AI 辩手"}</small>
              </span>
              <span className="speaker-wave mirror" aria-hidden="true">{Array.from({ length: 9 }, (_, index) => <i key={`right-${index}`} />)}</span>
            </div>
          )}
          <div className={`timer-orb ${!terminal && room.status !== "preparing" && !announcementActive && !awaitingHumanStart && !aiPreparing && remaining !== null && remaining <= 15 ? "danger" : ""}`} role="timer" aria-live="off" aria-label={terminal ? `${roomStatusLabel[room.status] || "比赛已经结束"}，计时已停止` : room.status === "preparing" ? "比赛准备中，完成后自动开场" : announcementActive ? `系统阶段提示还剩 ${formatTime(remaining)}` : awaitingHumanStart ? `完整发言时间 ${formatTime(remaining)}，点击开始发言后计时` : aiPreparing ? `AI 正在准备发言，计时暂停在 ${formatTime(remaining)}` : `剩余时间 ${formatTime(remaining)}`}><Clock3 size={22}/><strong>{terminal ? "已结束" : room.status === "preparing" ? "准备中" : formatTime(remaining)}</strong><small>{terminal ? roomStatusLabel[room.status] || "比赛已经结束" : room.status === "preparing" ? "完成后自动开场" : announcementActive ? "阶段提示音" : awaitingHumanStart ? "开始发言后计时" : aiPreparing ? "AI 准备中 · 计时暂停" : room.current_stage?.kind === "free" ? "自由辩论总计时" : "本环节剩余"}</small>{!terminal && room.status !== "preparing" && !announcementActive && !awaitingHumanStart && !aiPreparing && remaining !== null && remaining <= 15 && <span className="sr-only">即将结束</span>}</div>
          {mode === "debate" ? <StageSubtitle
            room={room}
            liveEvent={liveEvent}
            capturing={capturing}
            localCaption={asrCaption || partial}
            aiPreparing={aiPreparing}
            idleText={idleSubtitleText}
            attribution={subtitleAttribution}
          /> : <div className="watch-stage-focus" aria-label={terminal ? "比赛终态" : "当前赛况"}>
            <span>{terminal ? "比赛终态" : currentSpeaker?.display_name || (room.current_stage?.kind === "free" ? `${room.current_stage.side === "neg" ? "反方" : "正方"}辩手` : "自动赛程")}</span>
            <strong>{watchFocusLabel}</strong>
            <small>{terminal ? "比赛流程已经停止，可返回赛事大厅。" : "可通过比赛声音了解现场进程。"}</small>
          </div>}
          <div className="stage-progress">{room.recent_events.filter((event) => event.type === "stage.started").slice(-8).map((event) => <i className="done" key={event.seq} />)}{!terminal && <i className="current" />}</div>
        </main>
        <aside className="team-column neg"><div className="team-title"><span>反方</span><strong>OPPOSITION</strong></div>{neg.map((seat) => <SeatCard seat={seat} active={activeSeatKey === seat.seat_key || activeSeatKey === "neg_"} key={seat.seat_key} />)}</aside>
      </section>
      <footer className="stage-controls">
        <div className="control-info"><span className="seat-avatar">{room.my_seat ? mySeat?.display_name.slice(0,1) : <Users size={18}/>}</span><span><strong>{mode === "watch" ? room.can_control ? "房主观战 · 只读" : "观战模式" : mySeat?.display_name || "未绑定席位"}</strong><small>{mode === "watch" ? room.can_control ? "需要操作时请进入比赛控制页" : "公开只读画面" : deviceReason}</small></span></div>
        {mode === "debate" && <button type="button" title={canStartSpeaking || (connected && canRecoverSpeaking) ? readyReason : blockedSpeakDetail} data-state={capturing ? "speaking" : starting ? "starting" : pendingFinish ? "review" : canStartSpeaking ? "ready" : "blocked"} className={`speak-button ${starting || capturing || pendingFinish || finishing ? "recording" : canStartSpeaking ? "ready" : ""}`} disabled={pendingFinishMustDiscard || starting || finishing || (pendingFinish ? !pendingFinish.content.trim() : !capturing && !canStartSpeaking)} onClick={pendingFinishMustDiscard ? undefined : pendingFinish ? () => void submitFinish(pendingFinish) : capturing ? () => void stopSpeaking(false) : () => void startSpeaking()}>{pendingFinishMustDiscard ? <><MicOff size={24}/><span>本次发言无法提交<small>请处理本页尚未提交的内容</small></span></> : starting ? <><Mic size={24}/><span>正在启动麦克风<small>请确认浏览器权限提示…</small></span></> : finishing ? <><MicOff size={24}/><span>正在整理发言<small>等待最终字幕并安全提交…</small></span></> : pendingFinish ? <><MicOff size={24}/><span>提交保留的发言<small>{pendingFinish.content.trim() ? "识别文字已保留在本页" : "请先补充发言文字"}</small></span></> : capturing ? <><MicOff size={24}/><span>结束发言<small>{turnRemaining === null ? "正在识别发言" : `本轮剩余 ${formatTime(turnRemaining)}`}</small></span></> : <><Mic size={24}/><span>{!connected ? blockedSpeakLabel : canRecoverSpeaking ? "恢复发言" : canStartSpeaking ? "开始发言" : blockedSpeakLabel}<small>{!connected ? blockedSpeakDetail : canRecoverSpeaking ? "恢复同一设备的进行中发言" : canStartSpeaking ? readyReason : blockedSpeakDetail}</small></span></>}</button>}
        <div className="control-tools">{mode === "debate" && <TranscriptDrawer room={room} liveEvent={liveEvent} />}{mode === "debate" && showOwnerQuickControl && <button type="button" className="owner-quick-control" title={quickControlReason} aria-label={quickControlReason} aria-busy={roomActionBusy === (room.status === "paused" ? "resume" : "pause")} disabled={Boolean(roomActionBusy) || (room.status === "paused" ? !canResumeMatch : !canPauseMatch)} onClick={() => void controlMatch(room.status === "paused" ? "resume" : "pause")}>{room.status === "paused" ? <Play/> : <Pause/>}<small>{room.status === "paused" ? "继续" : "暂停"}</small></button>}<button type="button" aria-label={muted ? "开启比赛声音" : playbackNeedsGesture ? "播放比赛声音" : playbackError ? "重试比赛声音" : "关闭比赛声音"} aria-pressed={!muted && !playbackNeedsGesture && !playbackError} aria-busy={playbackPending} disabled={playbackPending} onClick={togglePlaybackSound}>{muted || playbackNeedsGesture || playbackError ? <VolumeX/> : <Volume2/>}<small>{muted ? "开启声音" : playbackNeedsGesture ? "点击播放" : playbackError ? "重试声音" : "声音"}</small></button>{mode === "watch" && room.my_seat && <Link href={`/rooms/${room.code}/debate`} className="stage-tool-link" aria-label="返回辩手页面"><Mic/><small>参赛</small></Link>}{mode === "watch" && room.can_control && <Link href={`/rooms/${room.code}/control`} className="stage-tool-link" aria-label="打开比赛控制台"><Settings/><small>控制台</small></Link>}<button type="button" aria-label="切换全屏" onClick={fullscreen}><Maximize/><small>全屏</small></button><button ref={settingsButton} type="button" title={mode === "watch" ? "声音与观看设置" : room.can_control ? "暂停、继续或提前结束比赛" : "退出页面与设备状态"} aria-label={settingsLabel} aria-controls="stage-settings-dialog" aria-expanded={showSettings} onClick={() => setShowSettings((value) => !value)}><Settings/><small>{mode === "watch" ? "设置" : room.can_control ? "控制" : "更多"}</small></button></div>
      </footer>
      {(room.failure_reason || disconnectGraceActive || (connectionError && !connected)) && <div className="stage-notice-stack">
        {serviceFailurePaused && <div className="stage-failure-warning" role="alert" aria-live="assertive"><AlertTriangle size={18}/><span><strong>{participantDisconnectAndFailurePaused ? "比赛同时遇到服务异常和真人断线。" : "比赛因临时服务异常暂停。"}</strong><small>{recoveryBlockedByDisconnect ? `请先等待 ${disconnectedHumanNames} 重新连接，人员齐全后再重试当前步骤。` : mode === "debate" && room.can_control ? "比赛进度已保存，确认后可重试当前步骤。" : "比赛进度已保存，请等待房主在比赛控制页处理；恢复后页面会自动同步。"}</small>{retryFailureError && <small className="stage-failure-error">{retryFailureError}</small>}</span>{mode === "debate" && room.can_control && <button type="button" aria-busy={retryingFailure} disabled={retryingFailure || hasLocalSpeechWork || recoveryBlockedByDisconnect} title={hasLocalSpeechWork ? "请先处理本页保留的发言文字" : recoveryBlockedByDisconnect ? "等待全部真人重新连接" : undefined} onClick={requestRetry}><RotateCcw size={15}/>{retryingFailure ? "正在重试…" : recoveryBlockedByDisconnect ? "等待真人重连" : "重试异常步骤"}</button>}</div>}
        {participantDisconnectPaused && <div className="stage-failure-warning" role="status" aria-live="polite"><Clock3 size={18}/><span><strong>真人断线超过 60 秒，比赛已自动暂停。</strong><small>{disconnectedHumans.length ? `仍在等待 ${disconnectedHumanNames} 重新连接；真人席位和身份保持不变。` : "全部真人已重新连接；确认现场就绪后，由房主继续比赛。"}</small></span></div>}
        {disconnectGraceActive && <div className="stage-failure-warning" role="status" aria-live="polite"><Clock3 size={18}/><span><strong>{disconnectedHumanNames}已断线，真人席位保持不变。</strong><small>{disconnectPauseTiming}；辩手返回后可继续当前流程。</small></span></div>}
        {connectionError && !connected && <div className={`stage-connection-warning ${connectionBlockedReason ? "blocked" : ""}`} role="alert"><WifiOff size={16}/><span>{connectionError}{connectionBlockedReason === "capacity_full" ? " 当前连接不会自动重试，请稍后刷新页面。" : ""}</span>{onReconnect && !connectionBlockedReason && <button type="button" onClick={onReconnect}>立即重连</button>}{connectionBlockedReason === "capacity_full" && <Link href="/">返回赛事大厅</Link>}</div>}
      </div>}
      {pendingFinish && !finishing && <div className="stage-transcript-review panel" role="dialog" aria-labelledby="transcript-review-title" aria-describedby="transcript-review-help"><strong id="transcript-review-title">{pendingFinishMustDiscard ? "本次发言已停止提交" : "提交前核对发言文字"}</strong><p id="transcript-review-help">{pendingFinishMustDiscard ? `${pendingFinishDiscardReason}内容仍保留在本页供核对，确认清除后${discardLeadsToResult ? "进入比赛结果" : "按当前轮次重新开始"}。` : captureError || `正在提交到“${pendingFinish.stageName}”；可修正识别结果，确认后进入下一环节。`}</p><label htmlFor="pending-speech-transcript">发言文字</label><textarea ref={pendingTranscript} id="pending-speech-transcript" className="textarea" maxLength={20000} value={pendingFinish.content} readOnly={pendingFinishMustDiscard} onChange={(event) => setPendingFinish({ ...pendingFinish, content: event.target.value })} placeholder="未识别到字幕时，请在此补充本次发言文字" /><div className="card-actions">{pendingFinishMustDiscard ? <button ref={discardPendingButton} type="button" className="button button-small button-danger" onClick={discardPendingFinishAndViewResult}>{discardLeadsToResult ? "清除未提交内容并查看结果" : "清除旧轮次内容"}</button> : <button type="button" className="button button-small button-green" disabled={!pendingFinish.content.trim() || finishing} onClick={() => void submitFinish(pendingFinish)}>确认文字并提交</button>}</div></div>}
      <DeviceControlRecovery
        visible={mode === "debate" && deviceControl === "lost" && mySeat?.occupant_type === "human" && !hasLocalSpeechWork}
        message={deviceControlError || "当前席位由另一设备控制；确认现场无人使用旧设备后，可接管到本设备。"}
        onConfirm={() => void acquireDeviceControl(true)}
      />
      {mode === "debate" && deviceControl === "unavailable" && Boolean(room.my_seat) && mySeat?.occupant_type === "human" && <div className="stage-connection-warning blocked" role="alert"><WifiOff size={16}/><span>{deviceControlError || "设备控制绑定暂时失败，比赛状态不受影响。"}</span><button type="button" onClick={() => void acquireDeviceControl(false)}>重试绑定当前设备</button></div>}
      {(captureError || playbackError) && !pendingFinish && <div className="stage-error" role="alert"><span>{captureError || playbackError}</span>{playbackError && room.can_control && <Link href={`/rooms/${room.code}/control`}>打开比赛控制台重试当前发言</Link>}</div>}
      {showSettings && <div ref={settingsDialog} id="stage-settings-dialog" className="stage-settings panel" role="dialog" aria-modal="true" aria-label={settingsLabel}><div className="panel-title"><h3>{settingsLabel}</h3><button type="button" className="icon-button" aria-label={`关闭${settingsLabel}`} onClick={() => { setShowSettings(false); settingsButton.current?.focus(); }}>×</button></div><div className="stage-action-list">{mode === "debate" && capturing && <button type="button" className="button button-small button-secondary" data-autofocus onClick={() => { setShowSettings(false); void stopSpeaking(false, true); }}><Pencil size={16}/>结束发言并修改文字</button>}{mode === "debate" && room.can_control && <Link className="button button-small button-secondary" href={`/rooms/${room.code}/control`}><Settings size={16}/>进入完整比赛控制台</Link>}{mode === "debate" && room.can_control && <button type="button" className="button button-small button-danger" data-autofocus={!capturing ? "" : undefined} disabled={Boolean(roomActionBusy) || !canTerminateMatch} title={terminateMatchReason} aria-label={canTerminateMatch ? "提前结束比赛" : `提前结束比赛不可用：${terminateMatchReason}`} onClick={requestTerminate}><Square size={16}/>提前结束比赛</button>}{mode === "debate" && onLeave && <button type="button" className="button button-small button-secondary" data-autofocus={!capturing && !room.can_control ? "" : undefined} disabled={hasLocalSpeechWork} title={hasLocalSpeechWork ? "请先提交或清除本页保留的发言文字" : undefined} onClick={() => requestLeave("settings")}><LogOut size={16}/>仅退出比赛页面</button>}{mode === "watch" && room.can_control && <Link className="button button-small button-secondary" href={`/rooms/${room.code}/control`}><Settings size={16}/>进入比赛控制</Link>}</div>{mode === "debate" && room.can_control && !terminal && <p><Pause size={16}/> 暂停或继续比赛请使用底部控制栏的快捷按钮。</p>}<p><Headphones size={16}/> AI 语音：{muted ? "已静音" : "自动播放"}</p><p><Mic size={16}/> 麦克风：{capturing ? "正在采集" : "待机"}</p><p><Clock3 size={16}/> 真人断线后席位保持真人身份，60 秒超时后自动暂停；重连后由房主继续。</p>{mode === "debate" && <p><Users size={16}/> 设备控制：{deviceControl === "owned" ? "当前设备" : deviceControl === "acquiring" ? "正在绑定" : deviceControl === "lost" ? "其他设备" : "只读席位"}</p>}{mode === "debate" && deviceControl === "lost" && <><p className="muted">接管会让原设备立即变为只读；原设备正在发言时不能接管。</p><button type="button" className="button button-small button-secondary" onClick={() => void acquireDeviceControl(true)}>确认接管到当前设备</button></>}{roomActionError && <div className="error-box" role="alert">{roomActionError}</div>}{roomActionStatus && <div className="success-box" role="status" aria-live="polite">{roomActionStatus}</div>}</div>}
      {confirmation && <div className="stage-confirm-backdrop"><div ref={confirmationDialog} className="stage-confirm-dialog panel" role="alertdialog" aria-modal="true" aria-labelledby="stage-confirm-title" aria-describedby="stage-confirm-description"><strong id="stage-confirm-title">{confirmation === "terminate" ? "确认提前结束比赛？" : confirmation === "retry" ? "确认重试异常步骤？" : "确认退出比赛页面？"}</strong><p id="stage-confirm-description">{confirmation === "terminate" ? `比赛将立即终止且不可恢复${humanSpeaking ? "，当前真人发言会被中断且不计入比赛" : ""}。` : confirmation === "retry" ? "系统会从已保存的比赛进度重新执行当前异常步骤；请确认当前没有其他房主同时操作。" : "比赛会继续进行，席位归属会保留；稍后可从“我的”返回。真人断线满 60 秒后系统会自动暂停，真人席位和身份保持不变。"}</p><div className="card-actions"><button ref={confirmationCancel} type="button" className="button button-small button-secondary" disabled={Boolean(roomActionBusy) || retryingFailure} onClick={closeConfirmation}>取消</button>{confirmation === "terminate" ? <button type="button" className="button button-small button-danger" aria-busy={roomActionBusy === "terminate"} disabled={Boolean(roomActionBusy)} onClick={() => void controlMatch("terminate")}>{roomActionBusy === "terminate" ? "正在结束…" : "确认提前结束"}</button> : confirmation === "retry" ? <button type="button" className="button button-small button-green" aria-busy={retryingFailure} disabled={retryingFailure} onClick={() => { setConfirmation(null); void retryFailedStep(); }}>{retryingFailure ? "正在重试…" : "确认重试"}</button> : <button type="button" className="button button-small button-green" onClick={() => { setConfirmation(null); onLeave?.(); }}>确认退出页面</button>}</div></div></div>}
    </div>
  );
}
