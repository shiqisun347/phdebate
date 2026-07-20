"use client";

import Link from "next/link";
import { AlertTriangle, Clock3, Headphones, LogOut, Maximize, Mic, MicOff, Pause, Pencil, Play, RotateCcw, Settings, Square, Users, Volume2, VolumeX, Wifi, WifiOff } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch, apiOrigin, websocketUrl } from "@/lib/api";
import { LiveKitRoomAudio } from "@/lib/audio/livekit-room-audio";
import { PcmStreamPlayer, streamAfterSeq } from "@/lib/audio/pcm-stream-player";
import { RecordingConsentControl } from "@/components/recording-consent-control";
import { competitionDisplayName } from "@/lib/primary-competition";
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
const ASR_PREROLL_MAX_SAMPLES = 32_000;
const ASR_CAPTURE_CHUNK_MS = 20;
const ASR_TARGET_SAMPLE_RATE = 16_000;
const MICROPHONE_START_TIMEOUT_MS = 12_000;
const SPEECH_START_RESPONSE_TIMEOUT_MS = 12_000;
const MIN_VOICED_SAMPLES = 4_000;
const VOICE_RMS_THRESHOLD = 0.01;
const VOICE_PEAK_THRESHOLD = 0.03;
const WEBRTC_AUDIO_ENABLED = process.env.NEXT_PUBLIC_WEBRTC_AUDIO_ENABLED === "true";

type VoiceState = "voice" | "silence" | "unknown";
type PendingFinish = {
  blob: Blob;
  content: string;
  voiceState: VoiceState;
  speechId: string;
  finalizedSpeechId?: string;
  finalizedContent?: string;
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
  const connectionLabel = seat.occupant_type === "human" || seat.occupant_type === "ai_substitute"
    ? (seat.connected ? "原辩手在线" : "原辩手离线")
    : "系统席位";
  const occupantLabel = seat.occupant_type === "human"
    ? "真人"
    : seat.occupant_type === "open"
      ? "空席"
      : seat.occupant_type === "ai_substitute"
        ? "AI 接替"
        : "AI";
  return <div className={`stage-seat ${active ? "active" : ""} ${seat.is_me ? "me" : ""}`}><span className="seat-avatar">{seat.display_name.slice(0,1)}</span><span className="seat-copy"><strong>{seat.display_name}</strong><small>{seat.label} · {occupantLabel}</small><span className="sr-only"> · {connectionLabel}{active ? " · 当前发言席位" : ""}</span></span><i className={`status-dot ${seat.connected ? "online" : ""}`} aria-hidden="true" /></div>;
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

export function DebateStage({ room, connected, mode, liveEvent, connectionError = "", onReconnect, onPendingFinishChange, onConsentChanged, onRoomChanged, onLeave }: { room: Room; connected: boolean; mode: "debate" | "watch"; liveEvent?: Record<string, unknown> | null; connectionError?: string; onReconnect?: () => void; onPendingFinishChange?: (pending: boolean) => void; onConsentChanged?: () => Promise<unknown>; onRoomChanged?: (room: Room) => void; onLeave?: () => void }) {
  const stagePreparing = Boolean(room.current_stage?.ai_preparing);
  const clockRunning = room.status === "running" || room.status === "judging";
  const remaining = useCountdown(room.remaining_seconds, room.seq, clockRunning && !stagePreparing);
  const turnRemaining = useCountdown(room.turn_remaining_seconds, room.seq, room.status === "running" && !stagePreparing);
  const failureFingerprint = room.failure_reason ? JSON.stringify([room.seq, room.failure_reason]) : "";
  const [muted, setMuted] = useState(mode === "watch");
  const [showSettings, setShowSettings] = useState(false);
  const [starting, setStarting] = useState(false);
  const [capturing, setCapturing] = useState(false);
  const [speechId, setSpeechId] = useState("");
  const [transcript, setTranscript] = useState("");
  const [partial, setPartial] = useState("");
  const [captureError, setCaptureError] = useState("");
  const [deviceControl, setDeviceControl] = useState<"acquiring" | "owned" | "lost" | "unavailable">(mode === "watch" ? "unavailable" : "acquiring");
  const [controlSeq, setControlSeq] = useState<number | null>(null);
  const [liveCaption, setLiveCaption] = useState("");
  const [pendingFinish, setPendingFinish] = useState<PendingFinish | null>(null);
  const [finishing, setFinishing] = useState(false);
  const [retryingFailure, setRetryingFailure] = useState(false);
  const [retryFailureError, setRetryFailureError] = useState("");
  const [roomActionBusy, setRoomActionBusy] = useState("");
  const [roomActionError, setRoomActionError] = useState("");
  const [roomActionStatus, setRoomActionStatus] = useState("");
  const [confirmation, setConfirmation] = useState<"terminate" | "leave" | "retry" | "abandon" | null>(null);
  const [playbackNeedsGesture, setPlaybackNeedsGesture] = useState(false);
  const [playbackError, setPlaybackError] = useState("");
  const [playbackPending, setPlaybackPending] = useState(false);
  const [streamFailureIdentity, setStreamFailureIdentity] = useState("");
  const [rtcAudioConnected, setRtcAudioConnected] = useState(false);
  const pendingSpeechStatus = pendingFinish
    ? room.speeches.find((item) => item.id === pendingFinish.speechId)?.status
    : undefined;
  const pendingFinishFinalized = Boolean(pendingFinish?.finalizedSpeechId);
  const lateFinalizeAvailable = Boolean(
    pendingFinish
      && ["completed", "review_required"].includes(room.status)
      && pendingSpeechStatus === "timed_out",
  );
  const pendingFinishDiscardReason = !pendingFinish || finishing || pendingFinishFinalized
    ? ""
    : room.status === "terminated"
      ? "比赛已终止，本次未提交录音无法再保存。"
      : pendingSpeechStatus === "interrupted"
        ? "本次发言已被比赛控制中断，未提交录音无法再保存。"
        : "";
  const pendingFinishMustDiscard = Boolean(pendingFinishDiscardReason);
  const discardLeadsToResult = ["completed", "review_required", "terminated"].includes(room.status);
  const mediaRecorder = useRef<MediaRecorder | null>(null);
  const mediaChunks = useRef<Blob[]>([]);
  const mediaChunkGeneration = useRef(0);
  const asrSocket = useRef<WebSocket | null>(null);
  const asrSession = useRef<AsrSession | null>(null);
  const audioContext = useRef<AudioContext | null>(null);
  const asrAudioInitialization = useRef<Promise<AudioContext> | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const playbackIdentity = useRef("");
  const playbackGeneration = useRef(0);
  const playbackSession = useRef<PlaybackSession | null>(null);
  const streamPlayer = useRef<PcmStreamPlayer | null>(null);
  const rtcAudio = useRef<LiveKitRoomAudio | null>(null);
  const streamIdentity = useRef("");
  const streamRestartTimer = useRef<number | null>(null);
  const streamRestartAttempts = useRef(0);
  const rtcFlushIdentity = useRef("");
  const roomRef = useRef(room);
  const mutedRef = useRef(muted);
  roomRef.current = room;
  mutedRef.current = muted;
  if (streamPlayer.current === null) streamPlayer.current = new PcmStreamPlayer();
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
  const startCancellationMessage = useRef("");
  const transcriptRef = useRef("");
  const partialRef = useRef("");
  const voicedSamples = useRef(0);
  const observedSamples = useRef(0);
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
  const activeSeatKey = room.active_speech?.seat_key || room.current_stage?.seat || (room.current_stage?.kind === "free" ? `${room.current_stage.side}_` : "");
  const aiPreparing = Boolean(
    stagePreparing
      && room.active_speech?.speaker_type === "ai"
      && ["speaking", "synthesizing"].includes(room.active_speech.status),
  );
  const aff = room.seats.filter((seat) => seat.side === "aff");
  const neg = room.seats.filter((seat) => seat.side === "neg");
  const currentSpeech = [...room.speeches].reverse().find((item) => item.content) || null;
  const mySeat = room.seats.find((seat) => seat.is_me);
  const stageIdentity = `${room.status}:${room.current_stage?.key || "none"}:${room.current_stage?.kind === "free" ? room.current_stage.side || "none" : "fixed"}:${room.my_seat || "none"}`;
  const lease = useMemo(() => {
    if (typeof window === "undefined") return "";
    const key = `jixia-control:${room.code}:${room.my_seat || "watch"}`;
    let value = sessionStorage.getItem(key);
    if (!value) { value = crypto.randomUUID(); sessionStorage.setItem(key, value); }
    return value;
  }, [room.code, room.my_seat]);

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
    void streamPlayer.current?.prepare().catch(() => undefined);
  }, [prepareAsrAudioGraph, room.code]);

  useEffect(() => {
    if (!WEBRTC_AUDIO_ENABLED) return;
    const client = new LiveKitRoomAudio();
    rtcAudio.current = client;
    let disposed = false;
    void client.prepare(room.code, {
      onNeedsGesture: () => {
        if (!disposed) setPlaybackNeedsGesture(true);
      },
      onReady: () => {
        if (disposed) return;
        setPlaybackPending(false);
        setPlaybackError("");
      },
      onReconnecting: () => {
        if (!disposed) setPlaybackPending(true);
      },
      onRecovered: () => {
        if (disposed) return;
        setPlaybackPending(false);
        setPlaybackError("");
      },
      onFatalError: (reason) => {
        if (disposed) return;
        setRtcAudioConnected(false);
        setPlaybackPending(false);
        setPlaybackError(`${reason}，已切换到兼容播放。`);
      },
    }).then((connected) => {
      if (disposed) return;
      setRtcAudioConnected(connected);
      if (connected) {
        stopStreamPlayback();
        client.setMuted(mutedRef.current);
      }
    });
    return () => {
      disposed = true;
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
      const result = await apiFetch<{ lease_fingerprint: string; seq: number }>(`/api/rooms/${room.code}/control-lease`, {
        method: "POST",
        headers: { "X-Control-Lease": lease },
        body: JSON.stringify({ force }),
      });
      setControlSeq(result.seq);
      setDeviceControl("owned");
      setCaptureError("");
    } catch (err) {
      setDeviceControl("lost");
      setCaptureError(err instanceof Error ? err.message : "当前设备无法接管辩手席位");
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
    if (takenOver) setDeviceControl("lost");
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

  function stopStreamPlayback(clearIdentity = true) {
    const hadStream = Boolean(streamIdentity.current);
    if (streamRestartTimer.current !== null) {
      window.clearTimeout(streamRestartTimer.current);
      streamRestartTimer.current = null;
    }
    streamPlayer.current?.stop();
    if (clearIdentity) {
      streamIdentity.current = "";
      streamRestartAttempts.current = 0;
    }
    if (hadStream) setPlaybackPending(false);
  }

  function startStreamPlayback(force = false) {
    const descriptor = activeStreamDescriptor();
    const player = streamPlayer.current;
    if (!descriptor || !player || mutedRef.current || descriptor.identity === streamFailureIdentity) return;
    if (!force && streamIdentity.current === descriptor.identity) return;
    if (streamIdentity.current !== descriptor.identity) streamRestartAttempts.current = 0;
    stopStreamPlayback(false);
    disposePlayback();
    streamIdentity.current = descriptor.identity;
    setPlaybackPending(true);
    setPlaybackError("");
    const identity = descriptor.identity;
    void player.start(
      {
        roomCode: roomRef.current.code,
        speechId: descriptor.speechId,
        generation: descriptor.generation,
        sampleRate: descriptor.sampleRate,
        afterSeq: streamAfterSeq(descriptor.playbackStartedAt, descriptor.sampleRate, Math.max(1, Math.round(descriptor.sampleRate * 0.04))),
      },
      {
        onNeedsGesture: () => {
          if (streamIdentity.current !== identity) return;
          setPlaybackPending(false);
          setPlaybackNeedsGesture(true);
        },
        onStarted: () => {
          if (streamIdentity.current !== identity) return;
          streamRestartAttempts.current = 0;
          setPlaybackPending(false);
          setPlaybackNeedsGesture(false);
          setPlaybackError("");
        },
        onDrained: () => {
          if (streamIdentity.current !== identity) return;
          setPlaybackPending(false);
          setPlaybackError("");
        },
        onRecoverableError: (reason) => {
          if (streamIdentity.current !== identity || mutedRef.current) return;
          streamRestartAttempts.current += 1;
          if (streamRestartAttempts.current > 3) {
            stopStreamPlayback();
            setStreamFailureIdentity(identity);
            setPlaybackPending(false);
            setPlaybackError(`${reason}，已切换到兼容播放。`);
            return;
          }
          setPlaybackPending(true);
          setPlaybackError(reason);
          if (streamRestartTimer.current !== null) window.clearTimeout(streamRestartTimer.current);
          streamRestartTimer.current = window.setTimeout(() => {
            streamRestartTimer.current = null;
            if (streamIdentity.current === identity && !mutedRef.current) startStreamPlayback(true);
          }, Math.min(3_000, 750 * 2 ** (streamRestartAttempts.current - 1)));
        },
        onFatalError: (reason) => {
          if (streamIdentity.current !== identity) return;
          stopStreamPlayback();
          setStreamFailureIdentity(identity);
          setPlaybackNeedsGesture(false);
          setPlaybackError(`${reason}，已切换到兼容播放。`);
        },
      },
    );
  }

  useEffect(() => {
    if (rtcAudioConnected) {
      stopStreamPlayback();
      rtcAudio.current?.setMuted(muted);
      setPlaybackPending(false);
      return;
    }
    const descriptor = activeStreamDescriptor(room);
    if (!descriptor) {
      stopStreamPlayback();
      return;
    }
    if (streamFailureIdentity && streamFailureIdentity !== descriptor.identity) setStreamFailureIdentity("");
    if (descriptor.identity === streamFailureIdentity) return;
    if (muted) {
      if (streamIdentity.current !== descriptor.identity) streamRestartAttempts.current = 0;
      streamIdentity.current = descriptor.identity;
      streamPlayer.current?.stop();
      void streamPlayer.current?.prepare().catch(() => setStreamFailureIdentity(descriptor.identity));
      return;
    }
    startStreamPlayback(true);
  }, [
    muted,
    room.status,
    room.active_speech?.id,
    room.active_speech?.status,
    room.active_speech?.stream_generation,
    room.active_speech?.stream_sample_rate,
    room.active_speech?.playback_started_at,
    rtcAudioConnected,
    streamFailureIdentity,
  ]);

  useEffect(() => {
    if (["paused", "terminated", "completed", "review_required", "judging"].includes(room.status)) {
      disposePlayback();
      return;
    }
    const streamed = activeStreamDescriptor(room);
    if (streamed && streamed.identity !== streamFailureIdentity) {
      disposePlayback();
      return;
    }
    const playing = [...room.speeches].reverse().find(
      (item) => item.audio_url && item.status === "playing" && item.id === room.active_speech?.id,
    );
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
    const audioUrl = playing?.audio_url || cue?.payload.audio_url as string | undefined;
    if (!audioUrl) {
      disposePlayback();
      return;
    }
    const identity = playing
      ? `speech:${playing.id}:${audioUrl}:${playing.playback_started_at || ""}`
      : `cue:${room.current_stage?.key || ""}:${stageStarted?.seq || ""}:${stageStarted?.created_at || ""}:${cue?.seq || ""}:${cue?.created_at || ""}:${audioUrl}`;
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
        playbackStartedAtMs: playing?.playback_started_at ? new Date(playing.playback_started_at).getTime() : null,
        durationSeconds: playing?.duration_seconds || 0,
        stageStartedAtMs: playing ? null : stageStartedAt,
        expiresAtMs: playing ? null : stageStartedAt + 10_000,
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
  }, [room.speeches, room.recent_events, room.current_stage?.key, room.current_stage?.kind, room.active_speech?.id, room.status, muted, streamFailureIdentity]);

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
    // The user may click before prepare() resolves.  LiveKitRoomAudio creates
    // its AudioContext before requesting credentials, so route that trusted
    // gesture to RTC as soon as the client exists.  A fatal RTC error sets
    // playbackError and deliberately falls through to the compatibility path.
    if (rtcAudio.current && !playbackError) {
      if (muted || playbackNeedsGesture || playbackError) {
        mutedRef.current = false;
        setMuted(false);
        setPlaybackError("");
        rtcAudio.current?.setMuted(false);
        void rtcAudio.current?.unlock().then(
          () => setPlaybackNeedsGesture(false),
          () => setPlaybackNeedsGesture(true),
        );
      } else {
        mutedRef.current = true;
        setMuted(true);
        rtcAudio.current?.setMuted(true);
      }
      return;
    }
    const streamed = activeStreamDescriptor();
    if (streamed && streamed.identity !== streamFailureIdentity) {
      if (muted || playbackNeedsGesture || playbackError) {
        mutedRef.current = false;
        setMuted(false);
        setPlaybackError("");
        let request: Promise<void>;
        try {
          request = streamPlayer.current?.unlock() || Promise.reject(new Error("实时音频尚未初始化"));
        } catch (error) {
          request = Promise.reject(error);
        }
        void request.then(
          () => setPlaybackNeedsGesture(false),
          () => setPlaybackNeedsGesture(true),
        );
        return;
      }
      mutedRef.current = true;
      setMuted(true);
      stopStreamPlayback(false);
      return;
    }
    const session = playbackSession.current;
    if (session?.playPending) return;
    if (muted || playbackNeedsGesture || playbackError) {
      setMuted(false);
      setPlaybackError("");
      if (session) {
        if (playbackError) {
          session.retryingAfterMediaError = true;
          try { session.audio.load?.(); } catch { /* Retry can still call play below. */ }
        }
        attemptPlayback(session);
      }
      return;
    }
    setMuted(true);
    if (session) {
      invalidatePlaybackAttempt(session);
      session.audio.pause();
    }
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
    disposePlayback(false);
    if (streamRestartTimer.current !== null) window.clearTimeout(streamRestartTimer.current);
    streamRestartTimer.current = null;
    void streamPlayer.current?.dispose();
    streamPlayer.current = null;
    if (mediaRecorder.current?.state === "recording") mediaRecorder.current.stop();
    streamRef.current?.getTracks().forEach((track) => track.stop());
    asrSocket.current?.close();
    void audioContext.current?.close();
    audioContext.current = null;
  }, []);

  useEffect(() => {
    if (liveEvent?.type === "asr" && typeof liveEvent.text === "string") {
      setLiveCaption(liveEvent.text);
    } else if (liveEvent?.type === "speech.completed" || liveEvent?.type === "speech.timed_out" || liveEvent?.type === "stage.advanced") {
      setLiveCaption("");
    }
  }, [liveEvent]);

  useEffect(() => {
    if (!rtcAudioConnected) return;
    const generation = room.active_speech?.stream_generation || "";
    if (generation) rtcAudio.current?.activateGeneration(generation);
  }, [room.active_speech?.stream_generation, rtcAudioConnected]);

  useEffect(() => {
    if (!rtcAudioConnected) return;
    const eventType = typeof liveEvent?.type === "string" ? liveEvent.type : "";
    const shouldFlush = ["speech.interrupted", "audio.rtc.interrupt", "audio.realtime.aborted", "audio.stream.aborted"].includes(eventType)
      || ["paused", "terminated"].includes(room.status);
    if (!shouldFlush) return;
    const identity = `${room.seq}:${room.status}:${eventType}`;
    if (rtcFlushIdentity.current === identity) return;
    rtcFlushIdentity.current = identity;
    const generation = eventType.startsWith("audio.")
      ? [...room.recent_events].reverse().find((event) => event.type === eventType)?.payload.generation
      : "";
    rtcAudio.current?.flush(typeof generation === "string" ? generation : "");
  }, [liveEvent, room.recent_events, room.seq, room.status, rtcAudioConnected]);

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
      void abortSpeaking("比赛控制已中断本次发言，麦克风已关闭；该录音未计入比赛。");
    }
  }, [capturing, room.recent_events, speechId]);

  useEffect(() => {
    if (!capturing) return;
    if (deviceControl === "lost") {
      void abortSpeaking("当前设备已失去席位控制，麦克风已关闭；该录音未计入比赛。");
    } else if (mySeat && mySeat.occupant_type !== "human") {
      void abortSpeaking("你的席位已由 AI 接替，麦克风已关闭；该录音未计入比赛。");
    }
  }, [capturing, deviceControl, mySeat?.occupant_type]);

  useEffect(() => {
    if ((!startingRef.current && !capturing) || !captureStageIdentity.current) return;
    if (stageIdentity !== captureStageIdentity.current) {
      void abortSpeaking("比赛阶段或自由辩论轮次已切换，麦克风已关闭；本次未完成录音未计入比赛。");
    }
  }, [capturing, stageIdentity]);

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
  }, [pendingFinish?.blob, finishing, pendingFinishMustDiscard]);

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
    mediaChunkGeneration.current = generation;
    captureStageIdentity.current = stageIdentity;
    startCancellationMessage.current = "";
    const recoveringExisting = room.active_speech?.speaker_type === "human" && room.active_speech.seat_key === room.my_seat;
    const recoveredTranscript = recoveringExisting ? room.active_speech?.content || "" : "";
    transcriptRef.current = recoveredTranscript;
    partialRef.current = "";
    voicedSamples.current = 0;
    observedSamples.current = 0;
    recoveredTranscriptBaseline.current = recoveredTranscript;
    asrRejectionMessage.current = "";
    forceTranscriptReview.current = false;
    stopPromise.current = null;
    stopServerTimedOut.current = false;
    setCaptureError(""); setTranscript(recoveredTranscript); setPartial(""); mediaChunks.current = [];
    timeoutHandled.current = "";
    let stream: MediaStream | null = null;
    let recorder: MediaRecorder | null = null;
    try {
      if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
        throw new Error("当前浏览器不支持麦克风录音，请使用最新版 Chrome、Edge 或 Safari。");
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
      recorder = new MediaRecorder(stream);
      streamRef.current = stream;
      mediaRecorder.current = recorder;
      recorder.ondataavailable = (event) => {
        if (
          event.data.size
          && mediaChunkGeneration.current === generation
          && startGeneration.current === generation
          && mediaRecorder.current === recorder
        ) mediaChunks.current.push(event.data);
      };
      recorder.start(1000);
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
      await startAsr(stream, generation);
      if (
        generation !== startGeneration.current
        || controller.signal.aborted
        || !captureActive.current
        || mediaRecorder.current !== recorder
        || streamRef.current !== stream
      ) {
        throw new DOMException("Speech capture cancelled", "AbortError");
      }
      setCapturing(true);
      startController.current = null;
      if (data.resumed && !asrRejectionMessage.current) setCaptureError("已恢复当前设备的进行中发言，先前字幕已保留。");
    } catch (err) {
      const ownsRecorder = Boolean(recorder && mediaRecorder.current === recorder);
      const ownsStream = Boolean(stream && streamRef.current === stream);
      if (ownsRecorder && recorder?.state === "recording") recorder.stop();
      if (stream && (ownsStream || (!startCancellationMessage.current && generation === startGeneration.current))) {
        stream.getTracks().forEach((track) => track.stop());
      }
      if (ownsRecorder) mediaRecorder.current = null;
      if (ownsStream) streamRef.current = null;
      if (generation === startGeneration.current || ownsRecorder || ownsStream) captureActive.current = false;
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

  async function startAsr(stream: MediaStream, generation: number) {
    let context: AudioContext | null = null;
    let source: MediaStreamAudioSourceNode | null = null;
    let processor: AudioWorkletNode | null = null;
    let ws: WebSocket | null = null;
    try {
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
      const socket = new WebSocket(websocketUrl(`/ws/rooms/${room.code}/asr`));
      ws = socket;
      socket.binaryType = "arraybuffer";
      let readySettled = false;
      let settleReady!: (ready: boolean) => void;
      const readyPromise = new Promise<boolean>((resolve) => {
        settleReady = (ready) => {
          if (readySettled) return;
          readySettled = true;
          resolve(ready);
        };
      });
      const session: AsrSession = {
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
        readyPromise,
        settleReady,
        tailResolve: null,
        resampler: { inputRate: context.sampleRate, buffer: new Float32Array(0), position: 0 },
      };
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
            protocol_version: 1,
            encoding: "pcm_s16le",
            channels: 1,
            sample_rate: ASR_TARGET_SAMPLE_RATE,
          }));
        } catch {
          rejectAsr("语音识别连接失败，录音仍会正常保存；请在结束后核对文字。");
        }
      };
      socket.onmessage = (event) => {
        if (!isCurrent()) return;
        let data: { type?: string; text?: string; is_final?: boolean; reason?: string };
        try {
          data = JSON.parse(event.data);
        } catch {
          return;
        }
        if (data.type === "ready") {
          if (session.failed) return;
          try {
            session.ready = true;
            for (const chunk of session.preReadyChunks) socket.send(chunk);
            session.preReadyChunks = [];
            session.preReadySamples = 0;
            session.settleReady(true);
          } catch {
            rejectAsr("语音识别音频发送失败，录音仍会正常保存；请在结束后核对文字。");
          }
        } else if (data.type === "asr") {
          const recognized = data.text || "";
          if (data.is_final) {
            session.finalCompleted = true;
            transcriptRef.current = `${transcriptRef.current}${recognized}`;
            partialRef.current = "";
            setTranscript(transcriptRef.current);
            setPartial("");
            asrFinalWaiter.current?.();
            asrFinalWaiter.current = null;
          } else {
            partialRef.current = recognized;
            setPartial(recognized);
          }
        } else if (data.type === "asr_rejected") {
          session.finalCompleted = true;
          transcriptRef.current = recoveredTranscriptBaseline.current;
          partialRef.current = "";
          setTranscript(recoveredTranscriptBaseline.current);
          setPartial("");
          asrRejectionMessage.current = data.reason === "silence"
            ? "未检测到清晰语音，请补充本次发言文字。"
            : "语音识别结果可信度不足，请核对并补充本次发言文字。";
          setCaptureError(asrRejectionMessage.current);
          asrFinalWaiter.current?.();
          asrFinalWaiter.current = null;
        } else if (data.type === "error") {
          rejectAsr("语音识别暂时不可用，录音仍会正常保存；请在结束后核对文字。");
          asrFinalWaiter.current?.();
          asrFinalWaiter.current = null;
        }
      };
      socket.onerror = () => {
        if (!isCurrent()) return;
        rejectAsr("语音识别连接失败，录音仍会正常保存；请在结束后核对文字。");
        asrFinalWaiter.current?.();
        asrFinalWaiter.current = null;
      };
      socket.onclose = (event) => {
        if (!isCurrent()) return;
        if (!session.ready) session.settleReady(false);
        if (session.stopping && !session.finalCompleted) {
          asrRejectionMessage.current ||= "语音识别连接在最终字幕返回前中断，录音已保留，请核对并补充本次发言文字。";
          setCaptureError(asrRejectionMessage.current);
        }
        asrFinalWaiter.current?.();
        asrFinalWaiter.current = null;
        if (event.code === 4409 && captureActive.current) {
          void abortSpeaking("当前设备已失去席位控制，麦克风已关闭；该录音未计入比赛。");
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
              socket.send(chunk);
            } else if (!session.failed) {
              session.preReadyChunks.push(chunk);
              session.preReadySamples += pcm.length;
              while (session.preReadySamples > ASR_PREROLL_MAX_SAMPLES && session.preReadyChunks.length) {
                const dropped = session.preReadyChunks.shift();
                session.preReadySamples -= dropped ? dropped.byteLength / 2 : 0;
                session.overflowed = true;
              }
              if (session.overflowed) {
                asrRejectionMessage.current ||= "语音识别连接较慢，开头音频未完整识别；录音已保留，请在结束后核对文字。";
                setCaptureError(asrRejectionMessage.current);
              }
            }
          } catch {
            rejectAsr("语音识别音频发送失败，录音仍会正常保存；请在结束后核对文字。");
          }
        }
      };
      source.connect(processor); processor.connect(context.destination);
    } catch {
      const message = "当前浏览器无法启动实时字幕，录音仍会正常保存；请在结束后核对文字。";
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
    const recorder = mediaRecorder.current;
    const blobPromise = new Promise<Blob>((resolve) => {
      if (!recorder || recorder.state === "inactive") return resolve(new Blob(mediaChunks.current, { type: "audio/webm" }));
      recorder.onstop = () => resolve(new Blob(mediaChunks.current, { type: recorder.mimeType || "audio/webm" }));
      recorder.stop();
    });
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
        asrRejectionMessage.current ||= "语音识别结束信号发送失败，录音已保留，请核对文字。";
        asrFinalWaiter.current?.();
        asrFinalWaiter.current = null;
        waitForFinal = null;
      }
    } else if (session && !session.aborted && !session.failed) {
      asrRejectionMessage.current ||= "语音识别连接未及时就绪，录音已保留，请核对并补充本次发言文字。";
      setCaptureError(asrRejectionMessage.current);
    }
    if (waitForFinal) {
      const finalReceived = await Promise.race([
        waitForFinal.then(() => true),
        new Promise<boolean>((resolve) => window.setTimeout(() => resolve(false), ASR_FINAL_WAIT_MS)),
      ]);
      if (!finalReceived && !partialRef.current.trim()) {
        asrRejectionMessage.current ||= "语音识别最终字幕未及时返回，录音已保留，请核对并补充本次发言文字。";
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
    const blob = await blobPromise;
    if (mediaRecorder.current === recorder) mediaRecorder.current = null;
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
    const pending = { blob, content, voiceState, speechId };
    setPendingFinish(pending);
    if (forceTranscriptReview.current || !content || hasUnconfirmedPartial || asrRejectionMessage.current) {
      forceTranscriptReview.current = false;
      setFinishing(false);
      setCaptureError(asrRejectionMessage.current || (hasUnconfirmedPartial
        ? "语音识别未确认最后一段字幕，录音已保留，请核对并补充本次发言文字。"
        : content
          ? "请核对并修正语音识别文字，确认后再提交本次发言。"
        : stopServerTimedOut.current
        ? "本轮时间已到，但未识别到字幕。录音已保留，请补充发言文字后提交。"
        : voiceState === "voice"
          ? "未识别到可用字幕。录音已保留，请补充发言文字后提交。"
          : voiceState === "silence"
            ? "未检测到清晰语音，不会保存静音录音；请补充本次发言文字后提交。"
            : "未能完成语音活动检测，录音已保留；请补充本次发言文字后提交。"
      ));
      return;
    }
    forceTranscriptReview.current = false;
    const submitted = await submitFinish(pending);
    if (stopServerTimedOut.current) {
      setCaptureError(submitted ? "本轮发言时间已到，录音已自动停止并提交。" : "本轮发言时间已到；录音已保留，可点击重试提交。");
    }
  }

  async function abortSpeaking(message: string) {
    if (!startingRef.current && !captureActive.current && !mediaRecorder.current && !streamRef.current) return;
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
    const recorder = mediaRecorder.current;
    if (recorder && recorder.state !== "inactive") recorder.stop();
    mediaRecorder.current = null;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    mediaChunks.current = [];
    mediaChunkGeneration.current = startGeneration.current;
    transcriptRef.current = "";
    partialRef.current = "";
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
    if (mediaRecorder.current && mediaRecorder.current.state !== "inactive") mediaRecorder.current.stop();
    mediaRecorder.current = null;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    invalidateAsrSession(asrSession.current);
    asrSocket.current?.close();
    asrSocket.current = null;
    void audioContext.current?.close().catch(() => undefined);
    audioContext.current = null;
    disposePlayback();
    mediaChunks.current = [];
    mediaChunkGeneration.current = startGeneration.current;
    transcriptRef.current = "";
    partialRef.current = "";
    recoveredTranscriptBaseline.current = "";
    asrRejectionMessage.current = "";
    forceTranscriptReview.current = false;
    asrFinalWaiter.current?.();
    asrFinalWaiter.current = null;
    finishKey.current = "";
    startKey.current = "";
    captureStageIdentity.current = "";
    setPendingFinish(null);
    setFinishing(false);
    setStarting(false);
    setCapturing(false);
    setSpeechId("");
    setTranscript("");
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
    setFinishing(true);
    const controller = new AbortController();
    finishController.current = controller;
    let finalizedSpeechId = pending.finalizedSpeechId || "";
    let finalizedContent = pending.finalizedContent || pending.content;
    try {
      if (!finalizedSpeechId) {
        const finalized = await apiFetch<{ speech_id?: string }>(`/api/rooms/${room.code}/speech/finish`, {
          method: "POST",
          headers: { "X-Control-Lease": lease, "X-Idempotency-Key": finishKey.current },
          body: JSON.stringify({ speech_id: pending.speechId, content: pending.content }),
          signal: controller.signal,
        });
        finalizedSpeechId = finalized.speech_id || pending.speechId;
        finalizedContent = pending.content;
        pending = { ...pending, content: finalizedContent, finalizedSpeechId, finalizedContent };
        setPendingFinish(pending);
      }
      if (finalizedSpeechId && pending.voiceState !== "silence" && pending.blob.size) {
        const form = new FormData(); form.append("audio", pending.blob, "speech.webm");
        await apiFetch(`/api/rooms/${room.code}/speech/${finalizedSpeechId}/audio`, { method: "POST", body: form, signal: controller.signal });
      }
      setPendingFinish(null);
      setSpeechId("");
      startKey.current = "";
      captureStageIdentity.current = "";
      recoveredTranscriptBaseline.current = "";
      return true;
    } catch (err) {
      if (controller.signal.aborted) return false;
      setCaptureError(finalizedSpeechId
        ? `${err instanceof Error ? err.message : "录音上传失败"}。发言文字已经提交，录音仍保留在当前页面，可点击重试上传。`
        : `${err instanceof Error ? err.message : "提交失败"}。录音仍保留在当前页面，可点击重试。`);
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

  async function abandonSeat() {
    if (roomActionInFlight.current || hasLocalSpeechWork) return;
    const action = "abandon";
    roomActionInFlight.current = action;
    setRoomActionBusy(action);
    setRoomActionError("");
    const operationKey = roomActionAttempts.current[action] || crypto.randomUUID();
    roomActionAttempts.current[action] = operationKey;
    try {
      const result = await apiFetch<{ room: Room }>(`/api/rooms/${room.code}/abandon-seat`, {
        method: "POST",
        headers: { "X-Idempotency-Key": operationKey },
        body: JSON.stringify({}),
      });
      delete roomActionAttempts.current[action];
      onRoomChanged?.(result.room);
    } catch (err) {
      setRoomActionError(err instanceof Error ? err.message : "暂时无法退出本场，请重试。");
      setShowSettings(true);
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
        ? "该席位已在其他设备接管"
        : room.speak_reason;
  const canRecoverSpeaking = room.active_speech?.speaker_type === "human" && room.active_speech.seat_key === room.my_seat;
  const canStartSpeaking = connected && (room.can_speak || canRecoverSpeaking) && deviceControl === "owned";
  const readyReason = !connected
    ? "实时连接已断开，请等待重连后再发言"
    : room.current_stage?.kind === "free" && turnRemaining !== null
    ? `${deviceReason} · 本轮剩余 ${formatTime(turnRemaining)}`
    : deviceReason;
  const hasLocalSpeechWork = starting || capturing || finishing || Boolean(pendingFinish);
  const humanSpeaking = room.active_speech?.speaker_type === "human";
  const canPauseMatch = room.can_control && ["running", "judging"].includes(room.status) && !humanSpeaking && !hasLocalSpeechWork;
  const canResumeMatch = room.can_control && room.status === "paused" && !room.failure_reason && !hasLocalSpeechWork;
  const canTerminateMatch = room.can_control && ["preparing", "running", "paused", "judging"].includes(room.status) && !hasLocalSpeechWork;
  const canAbandonSeat = !room.can_control && mySeat?.occupant_type === "human" && ["preparing", "running", "paused", "judging"].includes(room.status) && !hasLocalSpeechWork;

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

  function requestAbandon() {
    confirmationOrigin.current = "settings";
    setShowSettings(false);
    setConfirmation("abandon");
  }

  function closeConfirmation() {
    setConfirmation(null);
    if (confirmationOrigin.current === "settings") setShowSettings(true);
    else window.requestAnimationFrame(() => brandButton.current?.focus());
  }

  return (
    <div className={`stage-page ${mode === "watch" ? "watch-mode" : "debate-mode"}`}>
      <div className="stage-gridlines" />
      <header className="stage-topbar">{mode === "debate" ? <button ref={brandButton} type="button" className="stage-brand" aria-label="退出比赛页面" onClick={() => requestLeave("brand")}><span>辩</span><strong>稷下辩论</strong></button> : <Link href="/" className="stage-brand" aria-label="返回稷下辩论赛事大厅"><span>辩</span><strong>稷下辩论</strong></Link>}<div className="stage-topic"><small>{competitionDisplayName(room.competition)} · 房间 {room.code}</small><strong>{room.topic}</strong></div><div className={`connection ${connected ? "ok" : ""}`} role="status" aria-live="polite">{connected ? <Wifi size={15}/> : <WifiOff size={15}/>} {connected ? "实时连接" : "重连中"}</div></header>
      <section className="stage-arena">
        <aside className="team-column aff"><div className="team-title"><span>正方</span><strong>PROPOSITION</strong></div>{aff.map((seat) => <SeatCard seat={seat} active={activeSeatKey === seat.seat_key || activeSeatKey === "aff_"} key={seat.seat_key} />)}</aside>
        <main className="stage-center">
          <div className="stage-status"><span className={`badge ${["running", "judging"].includes(room.status) ? "live" : ""}`}>{roomStatusLabel[room.status] || room.status}</span><h1>{room.current_stage?.name || "等待比赛开始"}</h1></div>
          <div className={`timer-orb ${!aiPreparing && remaining !== null && remaining <= 15 ? "danger" : ""}`} role="timer" aria-live="off" aria-label={aiPreparing ? `AI 正在准备发言，计时暂停在 ${formatTime(remaining)}` : `剩余时间 ${formatTime(remaining)}`}><Clock3 size={22}/><strong>{formatTime(remaining)}</strong><small>{aiPreparing ? "AI 准备中 · 计时暂停" : room.current_stage?.kind === "free" ? "自由辩论总计时" : "本环节剩余"}</small>{!aiPreparing && remaining !== null && remaining <= 15 && <span className="sr-only">即将结束</span>}</div>
          <div className="subtitle-stage">
            <span className="quote-mark">“</span>
            <p>{capturing ? `${transcript}${partial}` || "正在聆听你的发言…" : liveCaption || (room.active_speech ? `${room.active_speech.content || (aiPreparing ? "AI 正在组织论点并合成语音…" : "发言正在生成或进行中…")}` : currentSpeech?.content || room.current_stage?.cue || "比赛发言将在这里实时呈现")}</p>
            <small>{room.active_speech ? room.seats.find((seat) => seat.seat_key === room.active_speech?.seat_key)?.display_name : currentSpeech?.speaker || "自动赛程"}</small>
          </div>
          <div className="stage-progress">{room.recent_events.filter((event) => event.type === "stage.started").slice(-8).map((event) => <i className="done" key={event.seq} />)}<i className="current" /></div>
        </main>
        <aside className="team-column neg"><div className="team-title"><span>反方</span><strong>OPPOSITION</strong></div>{neg.map((seat) => <SeatCard seat={seat} active={activeSeatKey === seat.seat_key || activeSeatKey === "neg_"} key={seat.seat_key} />)}</aside>
      </section>
      <footer className="stage-controls">
        <div className="control-info"><span className="seat-avatar">{room.my_seat ? mySeat?.display_name.slice(0,1) : <Users size={18}/>}</span><span><strong>{mode === "watch" ? room.can_control ? "房主观战 · 只读" : "观战模式" : mySeat?.display_name || "未绑定席位"}</strong><small>{mode === "watch" ? room.can_control ? "需要操作时请进入比赛控制页" : "公开只读画面" : deviceReason}</small></span></div>
        {mode === "debate" && <button type="button" className={`speak-button ${starting || capturing || pendingFinish || finishing ? "recording" : canStartSpeaking ? "ready" : ""}`} disabled={pendingFinishMustDiscard || starting || finishing || (pendingFinish ? !pendingFinish.content.trim() : !capturing && !canStartSpeaking)} onClick={pendingFinishMustDiscard ? undefined : pendingFinish ? () => void submitFinish(pendingFinish) : capturing ? () => void stopSpeaking(false) : () => void startSpeaking()}>{pendingFinishMustDiscard ? <><MicOff size={24}/><span>本次发言无法提交<small>请处理本页尚未提交的内容</small></span></> : starting ? <><Mic size={24}/><span>正在启动麦克风<small>请确认浏览器权限提示…</small></span></> : finishing ? <><MicOff size={24}/><span>正在整理发言<small>{pendingFinishFinalized ? "文字已提交，正在上传录音…" : "等待最终字幕并安全提交…"}</small></span></> : pendingFinish ? <><MicOff size={24}/><span>{pendingFinishFinalized ? "重试上传录音" : "提交保留的发言"}<small>{pendingFinishFinalized ? "发言文字已提交且锁定" : pendingFinish.content.trim() ? lateFinalizeAvailable ? "比赛流程已结束，本次超时发言仍可补交" : "录音与文字已保留在本页" : "请先补充发言文字"}</small></span></> : capturing ? <><MicOff size={24}/><span>结束发言<small>{turnRemaining === null ? "正在录音与识别" : `本轮剩余 ${formatTime(turnRemaining)}`}</small></span></> : <><Mic size={24}/><span>{!connected ? "等待实时连接" : canRecoverSpeaking ? "恢复发言" : canStartSpeaking ? "开始发言" : deviceControl === "acquiring" ? "绑定设备中" : deviceControl === "lost" ? "其他设备已接管" : "等待轮次"}<small>{!connected ? readyReason : canRecoverSpeaking ? "恢复同一设备的进行中发言" : readyReason}</small></span></>}</button>}
        <div className="control-tools"><button type="button" aria-label={muted ? "开启比赛声音" : playbackNeedsGesture ? "播放比赛声音" : playbackError ? "重试比赛声音" : "关闭比赛声音"} aria-pressed={!muted && !playbackNeedsGesture && !playbackError} aria-busy={playbackPending} disabled={playbackPending} onClick={togglePlaybackSound}>{muted || playbackNeedsGesture || playbackError ? <VolumeX/> : <Volume2/>}<small>{muted ? "开启声音" : playbackNeedsGesture ? "点击播放" : playbackError ? "重试声音" : "声音"}</small></button><button type="button" aria-label="切换全屏" onClick={fullscreen}><Maximize/><small>全屏</small></button><button ref={settingsButton} type="button" aria-label={mode === "watch" ? "观看设置" : "比赛操作"} aria-controls="stage-settings-dialog" aria-expanded={showSettings} onClick={() => setShowSettings((value) => !value)}><Settings/><small>{mode === "watch" ? "设置" : "操作"}</small></button></div>
      </footer>
      {mode === "debate" && room.recording_consent?.required && onConsentChanged && <div className="stage-consent-control"><RecordingConsentControl consent={room.recording_consent} onChanged={onConsentChanged} compact /></div>}
      {(room.failure_reason || (connectionError && !connected)) && <div className="stage-notice-stack">{room.failure_reason && <div className="stage-failure-warning" role="alert" aria-live="assertive"><AlertTriangle size={18}/><span><strong>比赛遇到临时服务异常，已自动安全暂停。</strong><small>{mode === "debate" && room.can_control ? "比赛进度已保存，确认后可重试当前步骤。" : "比赛进度已保存，请等待房主在比赛控制页处理；恢复后页面会自动同步。"}</small>{retryFailureError && <small className="stage-failure-error">{retryFailureError}</small>}</span>{mode === "debate" && room.can_control && <button type="button" aria-busy={retryingFailure} disabled={retryingFailure} onClick={requestRetry}><RotateCcw size={15}/>{retryingFailure ? "正在重试…" : "重试异常步骤"}</button>}</div>}{connectionError && !connected && <div className="stage-connection-warning" role="alert"><WifiOff size={16}/><span>{connectionError}</span>{onReconnect && <button type="button" onClick={onReconnect}>立即重连</button>}</div>}</div>}
      {pendingFinish && !finishing && <div className="stage-transcript-review panel" role="dialog" aria-labelledby="transcript-review-title" aria-describedby="transcript-review-help"><strong id="transcript-review-title">{pendingFinishMustDiscard ? "本次发言未能提交" : pendingFinishFinalized ? "发言文字已提交" : "提交前核对发言文字"}</strong><p id="transcript-review-help">{pendingFinishMustDiscard ? `${pendingFinishDiscardReason}你仍可查看下方文字，确认放弃后${discardLeadsToResult ? "进入比赛结果" : "返回当前比赛"}。` : pendingFinishFinalized ? captureError || "发言文字已经提交并锁定，正在等待录音上传。" : lateFinalizeAvailable ? `${captureError ? `${captureError} ` : ""}比赛流程虽已结束，但服务端仍允许补交这次超时发言。提交成功后将进入比赛结果。` : captureError || "可修正识别结果；提交后将进入下一环节。"}</p><label htmlFor="pending-speech-transcript">发言文字</label><textarea ref={pendingTranscript} id="pending-speech-transcript" className="textarea" maxLength={20000} value={pendingFinish.content} readOnly={pendingFinishMustDiscard || pendingFinishFinalized} onChange={(event) => { if (!pendingFinishFinalized) setPendingFinish({ ...pendingFinish, content: event.target.value }); }} placeholder="未识别到字幕时，请在此补充本次发言文字" /><div className="card-actions">{pendingFinishMustDiscard ? <button ref={discardPendingButton} type="button" className="button button-small button-danger" onClick={discardPendingFinishAndViewResult}>{discardLeadsToResult ? "放弃未提交内容并查看结果" : "放弃未提交内容并返回比赛"}</button> : <button type="button" className="button button-small button-green" disabled={!pendingFinish.content.trim() || finishing} onClick={() => void submitFinish(pendingFinish)}>{pendingFinishFinalized ? "重试上传录音" : "确认文字并提交"}</button>}</div></div>}
      {(captureError || playbackError) && !pendingFinish && <div className="stage-error" role="alert">{captureError || playbackError}</div>}
      {showSettings && <div ref={settingsDialog} id="stage-settings-dialog" className="stage-settings panel" role="dialog" aria-modal="true" aria-label={mode === "watch" ? "观看设置" : "比赛操作"}><div className="panel-title"><h3>{mode === "watch" ? "观看设置" : "比赛操作"}</h3><button type="button" className="icon-button" aria-label={mode === "watch" ? "关闭观看设置" : "关闭比赛操作"} onClick={() => { setShowSettings(false); settingsButton.current?.focus(); }}>×</button></div><div className="stage-action-list">{mode === "debate" && capturing && <button type="button" className="button button-small button-secondary" data-autofocus onClick={() => { setShowSettings(false); void stopSpeaking(false, true); }}><Pencil size={16}/>结束发言并修改文字</button>}{mode === "debate" && room.can_control && (room.status === "paused" ? <button type="button" className="button button-small button-green" data-autofocus={!capturing ? "" : undefined} aria-busy={roomActionBusy === "resume"} disabled={Boolean(roomActionBusy) || !canResumeMatch} onClick={() => void controlMatch("resume")}><Play size={16}/>{room.failure_reason ? "异常暂停，请先重试" : roomActionBusy === "resume" ? "正在继续…" : "继续比赛"}</button> : <button type="button" className="button button-small button-secondary" data-autofocus={!capturing ? "" : undefined} aria-busy={roomActionBusy === "pause"} disabled={Boolean(roomActionBusy) || !canPauseMatch} onClick={() => void controlMatch("pause")}><Pause size={16}/>{humanSpeaking ? "真人发言结束后可暂停" : "暂停比赛"}</button>)}{mode === "debate" && room.can_control && <button type="button" className="button button-small button-danger" disabled={Boolean(roomActionBusy) || !canTerminateMatch} onClick={requestTerminate}><Square size={16}/>提前结束比赛</button>}{mode === "debate" && canAbandonSeat && <button type="button" className="button button-small button-danger" disabled={Boolean(roomActionBusy)} onClick={requestAbandon}><LogOut size={16}/>放弃本场并由 AI 接替</button>}{mode === "debate" && onLeave && <button type="button" className="button button-small button-secondary" disabled={hasLocalSpeechWork} onClick={() => requestLeave("settings")}><LogOut size={16}/>仅退出比赛页面</button>}{mode === "watch" && room.can_control && <Link className="button button-small button-secondary" href={`/rooms/${room.code}/control`}><Settings size={16}/>进入比赛控制</Link>}</div><p><Headphones size={16}/> AI 语音：{muted ? "已静音" : "自动播放"}</p><p><Mic size={16}/> 麦克风：{capturing ? "正在采集" : "待机"}</p><p><Clock3 size={16}/> 服务端权威计时，断线重连后自动同步。</p>{mode === "debate" && <p><Users size={16}/> 设备控制：{deviceControl === "owned" ? "当前设备" : deviceControl === "acquiring" ? "正在绑定" : deviceControl === "lost" ? "其他设备" : "只读席位"}</p>}{mode === "debate" && deviceControl === "lost" && <><p className="muted">接管会让原设备立即变为只读；原设备正在发言时不能接管。</p><button type="button" className="button button-small button-secondary" onClick={() => void acquireDeviceControl(true)}>确认接管到当前设备</button></>}{roomActionError && <div className="error-box" role="alert">{roomActionError}</div>}{roomActionStatus && <div className="success-box" role="status" aria-live="polite">{roomActionStatus}</div>}</div>}
      {confirmation && <div className="stage-confirm-backdrop"><div ref={confirmationDialog} className="stage-confirm-dialog panel" role="alertdialog" aria-modal="true" aria-labelledby="stage-confirm-title" aria-describedby="stage-confirm-description"><strong id="stage-confirm-title">{confirmation === "terminate" ? "确认提前结束比赛？" : confirmation === "retry" ? "确认重试异常步骤？" : confirmation === "abandon" ? "确认放弃本场比赛？" : "确认退出比赛页面？"}</strong><p id="stage-confirm-description">{confirmation === "terminate" ? `比赛将立即终止且不可恢复${humanSpeaking ? "，当前真人发言会被中断且不计入比赛" : ""}。` : confirmation === "retry" ? "系统会从已保存的比赛进度重新执行当前异常步骤；请确认当前没有其他房主同时操作。" : confirmation === "abandon" ? "你的席位会立即由 AI 接替，旧比赛转为只读观战；之后可以创建或加入其他比赛。" : "比赛会继续进行，席位归属会保留；稍后可从“我的”返回。"}</p><div className="card-actions"><button ref={confirmationCancel} type="button" className="button button-small button-secondary" disabled={Boolean(roomActionBusy) || retryingFailure} onClick={closeConfirmation}>取消</button>{confirmation === "terminate" ? <button type="button" className="button button-small button-danger" aria-busy={roomActionBusy === "terminate"} disabled={Boolean(roomActionBusy)} onClick={() => void controlMatch("terminate")}>{roomActionBusy === "terminate" ? "正在结束…" : "确认提前结束"}</button> : confirmation === "retry" ? <button type="button" className="button button-small button-green" aria-busy={retryingFailure} disabled={retryingFailure} onClick={() => { setConfirmation(null); void retryFailedStep(); }}>{retryingFailure ? "正在重试…" : "确认重试"}</button> : confirmation === "abandon" ? <button type="button" className="button button-small button-danger" aria-busy={roomActionBusy === "abandon"} disabled={Boolean(roomActionBusy)} onClick={() => void abandonSeat()}>{roomActionBusy === "abandon" ? "正在退出…" : "确认放弃并由 AI 接替"}</button> : <button type="button" className="button button-small button-green" onClick={() => { setConfirmation(null); onLeave?.(); }}>确认退出页面</button>}</div></div></div>}
    </div>
  );
}
