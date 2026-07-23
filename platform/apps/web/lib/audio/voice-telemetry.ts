import { apiFetch } from "@/lib/api";

export type ActiveVoiceGeneration = { speechId: string; generation: string };

type ReporterOptions = {
  now?: () => number;
  post?: (path: string, init: RequestInit) => Promise<unknown>;
  networkIntervalMs?: number;
};

const NETWORK_INTERVAL_MS = 5_000;

function finite(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(0, value) : 0;
}

/**
 * Bridges already-existing browser audio events to bounded server telemetry.
 * It never sends transcript text, user identity or audio bytes, and failures
 * are intentionally outside the media hot path.
 */
export function installVoiceTelemetryReporter(
  roomCode: string,
  getActive: () => ActiveVoiceGeneration | null,
  options: ReporterOptions = {},
) {
  const now = options.now || Date.now;
  const post = options.post || apiFetch;
  const networkIntervalMs = options.networkIntervalMs || NETWORK_INTERVAL_MS;
  const audibleReported = new Set<string>();
  const lastNetworkAt = new Map<string, number>();

  const send = (active: ActiveVoiceGeneration, event: "browser_first_audible" | "browser_network_sample", metrics: Record<string, number>) => {
    void post(`/api/rooms/${roomCode}/voice-telemetry`, {
      method: "POST",
      body: JSON.stringify({
        speech_id: active.speechId,
        generation: active.generation,
        event,
        metrics,
      }),
    }).catch(() => undefined);
  };

  const onAudibility = (raw: Event) => {
    const detail = (raw as CustomEvent<Record<string, unknown>>).detail || {};
    if (detail.audible !== true) return;
    const active = getActive();
    const generation = String(detail.generation || "");
    if (!active || !generation || generation !== active.generation) return;
    const key = `${active.speechId}:${active.generation}`;
    if (audibleReported.has(key)) return;
    audibleReported.add(key);
    send(active, "browser_first_audible", {
      rms: finite(detail.rms),
      peak: finite(detail.peak),
      audio_time_seconds: finite(detail.audioTime),
    });
  };

  const onNetwork = (raw: Event) => {
    const active = getActive();
    if (!active) return;
    const key = `${active.speechId}:${active.generation}`;
    const timestamp = now();
    if (timestamp - (lastNetworkAt.get(key) || 0) < networkIntervalMs) return;
    lastNetworkAt.set(key, timestamp);
    const detail = (raw as CustomEvent<Record<string, unknown>>).detail || {};
    const emittedCount = finite(detail.jitterBufferEmittedCount);
    const jitterBufferDelayTotalSeconds = finite(
      detail.jitterBufferDelayTotalSeconds ?? detail.jitterBufferDelaySeconds,
    );
    const jitterBufferDelayAverageSeconds = typeof detail.jitterBufferDelayAverageSeconds === "number"
      ? finite(detail.jitterBufferDelayAverageSeconds)
      : emittedCount > 0 ? jitterBufferDelayTotalSeconds / emittedCount : 0;
    const jitterBufferDelayCurrentSeconds = typeof detail.jitterBufferDelayCurrentSeconds === "number"
      ? finite(detail.jitterBufferDelayCurrentSeconds)
      : jitterBufferDelayAverageSeconds;
    send(active, "browser_network_sample", {
      bitrate_bps: finite(detail.bitrateBps),
      bytes_received: finite(detail.bytesReceived),
      packets_received: finite(detail.packetsReceived),
      packets_lost: finite(detail.packetsLost),
      jitter_ms: finite(detail.jitterSeconds) * 1_000,
      concealed_samples: finite(detail.concealedSamples),
      // Preserve the established field as an average, not the WebRTC
      // cumulative counter that used to be stored here.
      jitter_buffer_delay_ms: jitterBufferDelayAverageSeconds * 1_000,
      jitter_buffer_delay_avg_ms: jitterBufferDelayAverageSeconds * 1_000,
      jitter_buffer_delay_current_ms: jitterBufferDelayCurrentSeconds * 1_000,
      jitter_buffer_delay_total_ms: jitterBufferDelayTotalSeconds * 1_000,
      jitter_buffer_emitted_count: emittedCount,
    });
  };

  window.addEventListener("jixia:agent-audio-audibility", onAudibility);
  window.addEventListener("jixia:agent-audio-network", onNetwork);
  return () => {
    window.removeEventListener("jixia:agent-audio-audibility", onAudibility);
    window.removeEventListener("jixia:agent-audio-network", onNetwork);
  };
}
