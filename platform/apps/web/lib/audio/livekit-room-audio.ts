import { apiFetch } from "@/lib/api";
import type {
  RemoteAudioTrack,
  RemoteParticipant,
  RemoteTrack,
  RemoteTrackPublication,
  Room,
} from "livekit-client";
type RtcTokenResponse = {
  enabled: boolean;
  url?: string;
  token?: string;
  room_name?: string;
};

export type LiveKitAudioCallbacks = {
  onNeedsGesture?: () => void;
  onReady?: () => void;
  onReconnecting?: () => void;
  onRecovered?: () => void;
  onFatalError?: (reason: string) => void;
};

export const RTC_RECOVERY_DELAYS_MS = [500, 1_000, 2_000] as const;

type RtcRecoveryCallbacks = {
  onAttempt: (attempt: number, delayMs: number, reason: string) => void;
  onRecovered: () => void;
  onExhausted: (reason: string) => void;
};

/**
 * Runs one application-level RTC recovery incident at a time. The epoch makes
 * late results from a cancelled room/speech generation harmless.
 */
export class BoundedRtcRecovery {
  private epoch = 0;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private inFlight = false;

  constructor(
    private readonly reconnect: () => Promise<boolean>,
    private readonly callbacks: RtcRecoveryCallbacks,
  ) {}

  start(reason: string) {
    if (this.timer !== null || this.inFlight) return;
    this.schedule(reason, this.epoch);
  }

  cancel() {
    this.epoch += 1;
    this.attempt = 0;
    this.inFlight = false;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }

  private schedule(reason: string, epoch: number) {
    if (epoch !== this.epoch || this.timer !== null || this.inFlight) return;
    if (this.attempt >= RTC_RECOVERY_DELAYS_MS.length) {
      this.callbacks.onExhausted(reason);
      return;
    }
    const attempt = ++this.attempt;
    const delayMs = RTC_RECOVERY_DELAYS_MS[attempt - 1];
    this.callbacks.onAttempt(attempt, delayMs, reason);
    this.timer = setTimeout(() => {
      this.timer = null;
      if (epoch !== this.epoch) return;
      this.inFlight = true;
      void this.reconnect().then((connected) => {
        if (epoch !== this.epoch) return;
        this.inFlight = false;
        if (connected) this.callbacks.onRecovered();
        else this.schedule(reason, epoch);
      }, () => {
        if (epoch !== this.epoch) return;
        this.inFlight = false;
        this.schedule(reason, epoch);
      });
    }, delayMs);
  }
}

const RTC_PRECONNECT_TIMEOUT_MS = 2_500;

function interruptGateWorkletUrl() {
  const basePath = process.env.NEXT_PUBLIC_BASE_PATH || "";
  return `${window.location.origin}${basePath}/worklets/livekit-interrupt-gate.js`;
}

export class LiveKitRoomAudio {
  private room: Room | null = null;
  private decoderAudio: HTMLAudioElement | null = null;
  private track: RemoteAudioTrack | null = null;
  private roomCode = "";
  private preparing: Promise<boolean> | null = null;
  private serviceEnabled: boolean | null = null;
  private callbacks: LiveKitAudioCallbacks = {};
  private unlocked = false;
  private muted = false;
  private context: AudioContext | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private gate: AudioWorkletNode | null = null;
  private mediaTrackEnded: { track: MediaStreamTrack; handler: () => void } | null = null;
  private gateInitialization: Promise<boolean> | null = null;
  private gateSetupVersion = 0;
  private gateEpoch = 0;
  private activeGeneration = "";
  private blockedGeneration = "";
  private statsTimer: number | null = null;
  private previousStatsBytes: number | null = null;
  private previousStatsAtMs: number | null = null;
  private previousPacketsReceived: number | null = null;
  private previousPacketsLost: number | null = null;
  private previousConcealedSamples: number | null = null;
  private previousJitterBufferDelaySeconds: number | null = null;
  private previousJitterBufferEmittedCount: number | null = null;

  private emitDiagnostic(action: string, detail: Record<string, unknown> = {}) {
    window.dispatchEvent(new CustomEvent("jixia:agent-audio-diagnostic", {
      detail: {
        action,
        roomCode: this.roomCode,
        generation: this.activeGeneration,
        muted: this.muted,
        contextState: this.context?.state || "missing",
        receivedAtMs: performance.timeOrigin + performance.now(),
        ...detail,
      },
    }));
  }

  prepare(roomCode: string, callbacks: LiveKitAudioCallbacks = {}) {
    this.callbacks = callbacks;
    if (this.room && this.roomCode === roomCode) return Promise.resolve(true);
    if (this.preparing && this.roomCode === roomCode) return this.preparing;
    this.roomCode = roomCode;
    const preparing = this.connect(roomCode);
    this.preparing = preparing;
    void preparing.finally(() => {
      if (this.preparing === preparing) this.preparing = null;
    });
    return preparing;
  }

  isDisabled() {
    return this.serviceEnabled === false;
  }

  reconnect() {
    const roomCode = this.roomCode;
    if (!roomCode) return Promise.resolve(false);
    if (this.preparing) return this.preparing;
    // The replacement AudioWorklet gate starts empty. Force the current room
    // projection to re-arm its generation after reconnection; otherwise an
    // equal generation would be treated as already active and stay silent.
    this.activeGeneration = "";
    const preparing = this.connect(roomCode, false);
    this.preparing = preparing;
    void preparing.finally(() => {
      if (this.preparing === preparing) this.preparing = null;
    });
    return preparing;
  }

  private async connect(roomCode: string, reportConnectionFailure = true) {
    try {
      this.serviceEnabled = null;
      // Create the AudioContext before the first network await.  A user can
      // click "enable sound" while the RTC token or LiveKit connection is
      // still pending; the trusted gesture must be able to resume this
      // context before the first continuous WebRTC samples arrive.
      if (this.room || this.context || this.gate) await this.disconnectRoom();
      if (this.roomCode !== roomCode) return false;
      const gatePreparation = this.ensureInterruptGate();
      this.emitDiagnostic("rtc-bootstrap-started");
      const liveKitModule = import("livekit-client");
      const credentialsRequest = apiFetch<RtcTokenResponse>(`/api/rooms/${roomCode}/rtc-token`, {
        method: "POST",
        body: "{}",
      });
      void liveKitModule.then(
        () => this.emitDiagnostic("rtc-module-ready"),
        (error) => this.emitDiagnostic("rtc-module-failed", {
          reason: error instanceof Error ? error.message : String(error),
        }),
      );
      void credentialsRequest.then(
        (value) => this.emitDiagnostic("rtc-credentials-ready", { enabled: Boolean(value.enabled) }),
        (error) => this.emitDiagnostic("rtc-credentials-failed", {
          reason: error instanceof Error ? error.message : String(error),
        }),
      );
      const [{ Room: LiveKitRoom, RoomEvent, Track }, credentials] = await Promise.all([
        liveKitModule,
        credentialsRequest,
      ]);
      if (!credentials.enabled) {
        this.serviceEnabled = false;
        await this.disconnectRoom();
        return false;
      }
      this.serviceEnabled = true;
      if (!credentials.url || !credentials.token || this.roomCode !== roomCode) return false;
      const gateReady = await gatePreparation;
      this.emitDiagnostic("rtc-gate-prepared", { gateReady });
      if (this.roomCode !== roomCode) return false;
      if (!gateReady) throw new Error("当前浏览器不支持比赛所需的 WebRTC AudioWorklet 播放链");
      const room = new LiveKitRoom({
        adaptiveStream: false,
        dynacast: false,
        stopLocalTrackOnUnpublish: true,
        // LiveKit Server 1.13.3 predates the upstream fix that routes
        // subscriber answers back to the publisher transport in single-PC
        // mode. Chrome can otherwise fail renegotiation with a stale m-line
        // transceiver and disconnect. This browser is subscribe-only, so the
        // mature dual-PC path is the safer compatibility mode.
        singlePeerConnection: false,
      });
      this.room = room;
      let subscribedPublication: RemoteTrackPublication | null = null;
      const selectAgentPublication = (publication: RemoteTrackPublication) => {
        if (publication.trackName !== "agent-tts") {
          publication.setSubscribed?.(false);
          return;
        }
        if (subscribedPublication && subscribedPublication !== publication) {
          publication.setSubscribed?.(false);
          this.emitDiagnostic("duplicate-agent-publication-rejected", {
            trackSid: publication.trackSid || "",
          });
          this.callbacks.onFatalError?.("检测到重复的 AI 语音发布轨道，请由房主重试当前发言");
          return;
        }
        subscribedPublication = publication;
        void publication.setSubscribed?.(true);
      };
      room.on(RoomEvent.TrackPublished, (publication: RemoteTrackPublication) => {
        if (publication.kind === Track.Kind.Audio) selectAgentPublication(publication);
      });
      room.on(RoomEvent.TrackSubscribed, (
        remoteTrack: RemoteTrack,
        publication: RemoteTrackPublication,
        _participant: RemoteParticipant,
      ) => {
        if (remoteTrack.kind !== Track.Kind.Audio || publication.trackName !== "agent-tts") return;
        const track = remoteTrack as RemoteAudioTrack;
        if (this.track === track) return;
        if (this.track) {
          // One room has one authoritative, long-lived publisher track. Never
          // switch an in-flight speech to a second player/track. A duplicate
          // publication is a room fault and must be resolved by retry/reset.
          publication.setSubscribed?.(false);
          this.emitDiagnostic("duplicate-agent-track-rejected", {
            trackSid: publication.trackSid || "",
          });
          this.callbacks.onFatalError?.("检测到重复的 AI 语音轨道，请由房主重试当前发言");
          return;
        }
        this.track = track;
        this.startNetworkStats(track);
        // A small non-zero jitter lead is more reliable than "zero latency".
        // The 120 ms canary still observed one startup concealment burst with
        // zero packet loss. 180 ms remains inside the three-second first-sound
        // budget while giving shared Wi-Fi and browser scheduling headroom.
        track.setPlayoutDelay(0.18);
        const mediaStreamTrack = track.mediaStreamTrack;
        this.emitDiagnostic("track-subscribed", {
          trackSid: publication.trackSid || "",
          mediaTrackId: mediaStreamTrack?.id || "",
          mediaTrackEnabled: mediaStreamTrack?.enabled ?? null,
          mediaTrackMuted: mediaStreamTrack?.muted ?? null,
          mediaTrackReadyState: mediaStreamTrack?.readyState || "",
          gateReady: Boolean(this.context && this.gate),
        });
        if (this.context && this.gate && mediaStreamTrack) {
          // Chrome may keep an unattached remote track at the RTP receiver but
          // not advance the decoded audio playout pipeline.  A permanently
          // muted sink keeps decoding active; the only audible path remains
          // the AudioWorklet, where generation flushes are deterministic.
          this.decoderAudio = document.createElement("audio");
          this.decoderAudio.autoplay = true;
          this.decoderAudio.muted = true;
          this.decoderAudio.setAttribute("playsinline", "");
          this.decoderAudio.dataset.jixiaAgentAudioDecoder = roomCode;
          this.decoderAudio.style.display = "none";
          document.body.appendChild(this.decoderAudio);
          track.attach(this.decoderAudio);
          void this.decoderAudio.play().catch(() => undefined);
          this.source = this.context.createMediaStreamSource(new MediaStream([mediaStreamTrack]));
          this.source.connect(this.gate);
          const handleTrackEnded = () => {
            if (this.track !== track || this.room !== room) return;
            this.emitDiagnostic("agent-media-track-ended", {
              mediaTrackId: mediaStreamTrack.id,
            });
            this.detachTrack();
            this.callbacks.onFatalError?.("WebRTC AI 语音轨道已中断");
          };
          mediaStreamTrack.addEventListener?.("ended", handleTrackEnded, { once: true });
          this.mediaTrackEnded = { track: mediaStreamTrack, handler: handleTrackEnded };
          this.emitDiagnostic("worklet-source-connected", {
            mediaTrackId: mediaStreamTrack.id,
          });
          if (this.context.state !== "running") this.callbacks.onNeedsGesture?.();
        } else {
          this.track = null;
          this.stopNetworkStats();
          publication.setSubscribed?.(false);
          this.callbacks.onFatalError?.("WebRTC AI 语音轨道无法连接到连续播放链");
          return;
        }
        this.callbacks.onReady?.();
      });
      room.on(RoomEvent.TrackUnsubscribed, (remoteTrack: RemoteTrack) => {
        if (remoteTrack !== this.track) return;
        this.detachTrack();
        if (this.room === room) {
          this.emitDiagnostic("agent-track-unsubscribed");
          this.callbacks.onFatalError?.("WebRTC AI 语音轨道已取消订阅");
        }
      });
      room.on(RoomEvent.AudioPlaybackStatusChanged, () => {
        if (!room.canPlaybackAudio) this.callbacks.onNeedsGesture?.();
      });
      room.on(RoomEvent.Reconnecting, () => this.callbacks.onReconnecting?.());
      room.on(RoomEvent.Reconnected, () => this.callbacks.onRecovered?.());
      room.on(RoomEvent.Disconnected, () => {
        if (this.room === room) this.callbacks.onFatalError?.("WebRTC 实时音频连接已断开");
      });
      this.emitDiagnostic("rtc-preconnect-started");
      let preconnectTimer: number | null = null;
      const preconnectResult = await Promise.race([
        room.prepareConnection(credentials.url, credentials.token).then(
          () => "ready" as const,
          (error: unknown) => {
            this.emitDiagnostic("rtc-preconnect-failed", {
              reason: error instanceof Error ? error.message : String(error),
            });
            return "failed" as const;
          },
        ),
        new Promise<"timeout">((resolve) => {
          preconnectTimer = window.setTimeout(() => resolve("timeout"), RTC_PRECONNECT_TIMEOUT_MS);
        }),
      ]);
      if (preconnectTimer !== null) window.clearTimeout(preconnectTimer);
      this.emitDiagnostic("rtc-preconnect-finished", { result: preconnectResult });
      if (this.room !== room || this.roomCode !== roomCode) return false;
      this.emitDiagnostic("rtc-connect-started");
      await room.connect(credentials.url, credentials.token, { autoSubscribe: false });
      // TrackPublished is not replayed by every LiveKit client for tracks that
      // were already published before join. Inspect existing publications once
      // after connect, while still subscribing to at most one agent-tts track.
      room.remoteParticipants?.forEach((participant) => {
        participant.trackPublications?.forEach((publication) => {
          if (publication.kind === Track.Kind.Audio) selectAgentPublication(publication);
        });
      });
      this.emitDiagnostic("rtc-connect-finished");
      if (this.unlocked) {
        await Promise.all([
          room.startAudio(),
          this.context && this.context.state !== "running" ? this.context.resume() : Promise.resolve(),
        ]);
      }
      if (!room.canPlaybackAudio) this.callbacks.onNeedsGesture?.();
      return true;
    } catch (error) {
      if (reportConnectionFailure && this.roomCode === roomCode) {
        this.callbacks.onFatalError?.(error instanceof Error ? error.message : "WebRTC 实时音频初始化失败");
      }
      await this.disconnectRoom();
      return false;
    }
  }

  async unlock() {
    const room = this.room;
    const context = this.context;
    if (!room && !context) throw new Error("WebRTC 实时音频尚未连接");
    // Preserve the user's trusted gesture immediately.  If room.startAudio()
    // is still waiting on RTC setup, connect() will retry it once joined.
    this.unlocked = true;
    await Promise.all([
      room?.startAudio(),
      context && context.state !== "running" ? context.resume() : Promise.resolve(),
    ]);
    this.emitDiagnostic("unlocked");
  }

  setMuted(muted: boolean) {
    this.muted = muted;
    this.gate?.port.postMessage({ type: "muted", muted });
    this.emitDiagnostic("muted-changed", { muted });
  }

  activateGeneration(generation: string) {
    const normalized = generation.trim();
    if (!normalized || normalized === this.blockedGeneration) return false;
    if (normalized === this.activeGeneration && !this.blockedGeneration) return true;
    this.activeGeneration = normalized;
    this.blockedGeneration = "";
    this.gateEpoch += 1;
    this.gate?.port.postMessage({ type: "activate", epoch: this.gateEpoch, generation: normalized });
    this.emitDiagnostic("generation-activated", { generation: normalized, epoch: this.gateEpoch });
    return true;
  }

  flush(generation = "") {
    const normalized = generation.trim();
    if (normalized && this.activeGeneration && normalized !== this.activeGeneration) return false;
    this.blockedGeneration = normalized || this.activeGeneration;
    this.gateEpoch += 1;
    if (this.gate && this.context) {
      this.gate.port.postMessage({
        type: "flush",
        epoch: this.gateEpoch,
        generation: this.blockedGeneration,
        guardFrames: Math.ceil(this.context.sampleRate * 0.22),
      });
      this.emitDiagnostic("generation-flushed", {
        generation: this.blockedGeneration,
        epoch: this.gateEpoch,
      });
      return true;
    }
    return false;
  }

  async dispose() {
    this.roomCode = "";
    this.activeGeneration = "";
    this.blockedGeneration = "";
    await this.disconnectRoom();
  }

  private async ensureInterruptGate() {
    if (this.context && this.gate) return true;
    if (this.gateInitialization) return this.gateInitialization;
    const setupVersion = ++this.gateSetupVersion;
    const initialization = (async () => {
      const AudioContextClass = window.AudioContext
        || (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!AudioContextClass || typeof AudioWorkletNode === "undefined" || typeof MediaStream === "undefined") return false;
      const context = new AudioContextClass({ latencyHint: "interactive" });
      // Expose the context immediately so unlock() can resume it synchronously
      // from a trusted click even while addModule() is still loading.
      this.context = context;
      try {
        await context.audioWorklet.addModule(interruptGateWorkletUrl());
        if (this.context !== context || this.gateSetupVersion !== setupVersion) {
          await context.close().catch(() => undefined);
          return false;
        }
        const gate = new AudioWorkletNode(context, "jixia-livekit-interrupt-gate", {
          numberOfInputs: 1,
          numberOfOutputs: 1,
          outputChannelCount: [1],
        });
        gate.port.onmessage = (event: MessageEvent<Record<string, unknown>>) => {
          const payload = event.data || {};
          if (payload.type === "state") {
            this.emitDiagnostic("worklet-state", payload);
            return;
          }
          if (payload.type !== "audibility") return;
          window.dispatchEvent(new CustomEvent("jixia:agent-audio-audibility", {
            detail: {
              audible: Boolean(payload.audible),
              generation: String(payload.generation || ""),
              epoch: Number(payload.epoch) || 0,
              rms: Number(payload.rms) || 0,
              peak: Number(payload.peak) || 0,
              audioTime: Number(payload.audioTime) || 0,
              receivedAtMs: performance.timeOrigin + performance.now(),
            },
          }));
        };
        gate.connect(context.destination);
        gate.port.postMessage({ type: "muted", muted: this.muted });
        this.gate = gate;
        this.emitDiagnostic("worklet-ready");
        return true;
      } catch (error) {
        this.emitDiagnostic("worklet-initialization-failed", {
          reason: error instanceof Error ? error.message : String(error),
        });
        if (this.context === context) this.context = null;
        await context.close().catch(() => undefined);
        return false;
      }
    })();
    this.gateInitialization = initialization;
    try {
      return await initialization;
    } finally {
      if (this.gateInitialization === initialization) this.gateInitialization = null;
    }
  }

  private startNetworkStats(track: RemoteAudioTrack) {
    this.stopNetworkStats();
    const collect = async () => {
      if (this.track !== track) return;
      try {
        const report = await track.getRTCStatsReport();
        if (!report || this.track !== track) return;
        let inbound: RTCInboundRtpStreamStats | undefined;
        report.forEach((entry) => {
          if (
            entry.type === "inbound-rtp"
            && !entry.isRemote
            && (entry.kind === "audio" || entry.mediaType === "audio")
          ) inbound = entry as RTCInboundRtpStreamStats;
        });
        if (!inbound) return;
        const codec = inbound.codecId
          ? report.get(inbound.codecId) as (RTCStats & { mimeType?: string; clockRate?: number }) | undefined
          : undefined;
        const receivedAtMs = performance.timeOrigin + performance.now();
        const bytesReceived = Number(inbound.bytesReceived) || 0;
        const elapsedMs = this.previousStatsAtMs === null ? 0 : receivedAtMs - this.previousStatsAtMs;
        const bitrateBps = elapsedMs > 0 && this.previousStatsBytes !== null
          ? Math.max(0, (bytesReceived - this.previousStatsBytes) * 8_000 / elapsedMs)
          : 0;
        const packetsReceivedTotal = Math.max(0, Number(inbound.packetsReceived) || 0);
        const packetsLostTotal = Math.max(0, Number(inbound.packetsLost) || 0);
        const concealedSamplesTotal = Math.max(0, Number(inbound.concealedSamples) || 0);
        // WebRTC packet and concealment counters are cumulative for the
        // lifetime of this long-lived room track. Sending those totals under
        // each speech made a healthy later turn inherit every packet loss and
        // concealed sample from the whole match. Report interval deltas for
        // quality decisions while retaining explicit totals for diagnostics.
        const packetsReceivedDelta = this.previousPacketsReceived === null
          ? 0
          : Math.max(0, packetsReceivedTotal - this.previousPacketsReceived);
        const packetsLostDelta = this.previousPacketsLost === null
          ? 0
          : Math.max(0, packetsLostTotal - this.previousPacketsLost);
        const concealedSamplesDelta = this.previousConcealedSamples === null
          ? 0
          : Math.max(0, concealedSamplesTotal - this.previousConcealedSamples);
        // jitterBufferDelay and jitterBufferEmittedCount are cumulative WebRTC
        // counters.  Reporting the raw delay as if it were a point-in-time
        // latency made every long session look progressively worse.  Expose
        // both the lifetime average and the interval average; retain the raw
        // total under an explicit name for backwards-compatible diagnostics.
        const jitterBufferDelayTotalSeconds = Math.max(0, Number(inbound.jitterBufferDelay) || 0);
        const jitterBufferEmittedCount = Math.max(0, Number(inbound.jitterBufferEmittedCount) || 0);
        const jitterBufferDelayAverageSeconds = jitterBufferEmittedCount > 0
          ? jitterBufferDelayTotalSeconds / jitterBufferEmittedCount
          : 0;
        const hasComparableJitterSample = this.previousJitterBufferDelaySeconds !== null
          && this.previousJitterBufferEmittedCount !== null
          && jitterBufferDelayTotalSeconds >= this.previousJitterBufferDelaySeconds
          && jitterBufferEmittedCount > this.previousJitterBufferEmittedCount;
        const jitterBufferDelayCurrentSeconds = hasComparableJitterSample
          ? (jitterBufferDelayTotalSeconds - this.previousJitterBufferDelaySeconds!)
            / (jitterBufferEmittedCount - this.previousJitterBufferEmittedCount!)
          : jitterBufferDelayAverageSeconds;
        this.previousStatsBytes = bytesReceived;
        this.previousStatsAtMs = receivedAtMs;
        this.previousPacketsReceived = packetsReceivedTotal;
        this.previousPacketsLost = packetsLostTotal;
        this.previousConcealedSamples = concealedSamplesTotal;
        this.previousJitterBufferDelaySeconds = jitterBufferDelayTotalSeconds;
        this.previousJitterBufferEmittedCount = jitterBufferEmittedCount;
        window.dispatchEvent(new CustomEvent("jixia:agent-audio-network", {
          detail: {
            receivedAtMs,
            mimeType: String(codec?.mimeType || ""),
            clockRate: Number(codec?.clockRate) || 0,
            bitrateBps,
            bytesReceived,
            packetsReceived: packetsReceivedDelta,
            packetsLost: packetsLostDelta,
            concealedSamples: concealedSamplesDelta,
            packetsReceivedTotal,
            packetsLostTotal,
            concealedSamplesTotal,
            jitterSeconds: Number(inbound.jitter) || 0,
            // Kept for older telemetry listeners; this is the raw cumulative
            // WebRTC counter and must be divided by emitted count.
            jitterBufferDelaySeconds: jitterBufferDelayTotalSeconds,
            jitterBufferDelayTotalSeconds,
            jitterBufferDelayAverageSeconds,
            jitterBufferDelayCurrentSeconds,
            jitterBufferEmittedCount,
          },
        }));
      } catch {
        // Diagnostic stats must never become part of the media hot path.
      }
    };
    void collect();
    this.statsTimer = window.setInterval(() => void collect(), 1_000);
  }

  private stopNetworkStats() {
    if (this.statsTimer !== null) window.clearInterval(this.statsTimer);
    this.statsTimer = null;
    this.previousStatsBytes = null;
    this.previousStatsAtMs = null;
    this.previousPacketsReceived = null;
    this.previousPacketsLost = null;
    this.previousConcealedSamples = null;
    this.previousJitterBufferDelaySeconds = null;
    this.previousJitterBufferEmittedCount = null;
  }

  private async disconnectRoom() {
    this.gateSetupVersion += 1;
    const room = this.room;
    this.room = null;
    this.detachTrack();
    if (this.gate) this.gate.port.onmessage = null;
    this.gate?.disconnect();
    this.gate = null;
    const context = this.context;
    this.context = null;
    await context?.close().catch(() => undefined);
    await room?.disconnect().catch(() => undefined);
  }

  private detachTrack() {
    this.stopNetworkStats();
    const track = this.track;
    const decoderAudio = this.decoderAudio;
    this.decoderAudio = null;
    const mediaTrackEnded = this.mediaTrackEnded;
    this.mediaTrackEnded = null;
    mediaTrackEnded?.track.removeEventListener?.("ended", mediaTrackEnded.handler);
    this.source?.disconnect();
    this.source = null;
    if (track && decoderAudio) track.detach(decoderAudio);
    if (decoderAudio) {
      decoderAudio.pause();
      decoderAudio.srcObject = null;
      decoderAudio.remove();
    }
    this.track = null;
  }
}
