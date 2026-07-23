import { describe, expect, it, vi } from "vitest";
import { installVoiceTelemetryReporter } from "./voice-telemetry";

describe("voice telemetry reporter", () => {
  it("reports first audible once and throttles network samples per speech generation", async () => {
    let time = 10_000;
    const post = vi.fn().mockResolvedValue({ accepted: true });
    const active = { speechId: "speech-1", generation: "generation-1" };
    const stop = installVoiceTelemetryReporter("123456", () => active, {
      now: () => time,
      post,
      networkIntervalMs: 5_000,
    });

    window.dispatchEvent(new CustomEvent("jixia:agent-audio-audibility", {
      detail: { audible: true, generation: active.generation, rms: 0.2, peak: 0.4 },
    }));
    window.dispatchEvent(new CustomEvent("jixia:agent-audio-audibility", {
      detail: { audible: true, generation: active.generation, rms: 0.3, peak: 0.5 },
    }));
    window.dispatchEvent(new CustomEvent("jixia:agent-audio-network", {
      detail: {
        bitrateBps: 48_000,
        packetsLost: 1,
        jitterSeconds: 0.012,
        jitterBufferDelayTotalSeconds: 2,
        jitterBufferDelayAverageSeconds: 0.02,
        jitterBufferDelayCurrentSeconds: 0.03,
        jitterBufferEmittedCount: 100,
      },
    }));
    time += 1_000;
    window.dispatchEvent(new CustomEvent("jixia:agent-audio-network", {
      detail: { bitrateBps: 40_000, packetsLost: 2 },
    }));
    time += 5_000;
    window.dispatchEvent(new CustomEvent("jixia:agent-audio-network", {
      detail: { bitrateBps: 42_000, packetsLost: 3 },
    }));

    expect(post).toHaveBeenCalledTimes(3);
    expect(JSON.parse(post.mock.calls[0][1].body)).toMatchObject({
      speech_id: "speech-1",
      generation: "generation-1",
      event: "browser_first_audible",
    });
    expect(JSON.parse(post.mock.calls[1][1].body)).toMatchObject({
      event: "browser_network_sample",
      metrics: {
        bitrate_bps: 48_000,
        packets_lost: 1,
        jitter_ms: 12,
        jitter_buffer_delay_ms: 20,
        jitter_buffer_delay_avg_ms: 20,
        jitter_buffer_delay_current_ms: 30,
        jitter_buffer_delay_total_ms: 2_000,
        jitter_buffer_emitted_count: 100,
      },
    });
    stop();
  });

  it("normalizes a legacy cumulative jitter-buffer report to an average", () => {
    const post = vi.fn().mockResolvedValue({ accepted: true });
    const active = { speechId: "speech-legacy", generation: "generation-legacy" };
    const stop = installVoiceTelemetryReporter("123456", () => active, { post });

    window.dispatchEvent(new CustomEvent("jixia:agent-audio-network", {
      detail: {
        jitterBufferDelaySeconds: 4.5,
        jitterBufferEmittedCount: 150,
      },
    }));

    const body = JSON.parse(post.mock.calls[0][1].body);
    expect(body.metrics).toMatchObject({
      jitter_buffer_delay_ms: 30,
      jitter_buffer_delay_avg_ms: 30,
      jitter_buffer_delay_current_ms: 30,
      jitter_buffer_delay_total_ms: 4_500,
      jitter_buffer_emitted_count: 150,
    });
    stop();
  });

  it("ignores stale generation events and silence", () => {
    const post = vi.fn().mockResolvedValue({ accepted: true });
    const stop = installVoiceTelemetryReporter(
      "123456",
      () => ({ speechId: "speech-2", generation: "current" }),
      { post },
    );
    window.dispatchEvent(new CustomEvent("jixia:agent-audio-audibility", {
      detail: { audible: false, generation: "current" },
    }));
    window.dispatchEvent(new CustomEvent("jixia:agent-audio-audibility", {
      detail: { audible: true, generation: "stale" },
    }));
    expect(post).not.toHaveBeenCalled();
    stop();
  });
});
