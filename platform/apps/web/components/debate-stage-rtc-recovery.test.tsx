import { act, render, screen } from "@testing-library/react";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { Room } from "@/lib/types";

const mocks = vi.hoisted(() => {
  let prepareResult = true;
  let disabled = false;
  const liveKitInstances: Array<{
    callbacks: Record<string, (...args: unknown[]) => void>;
    reconnect: ReturnType<typeof vi.fn>;
    setMuted: ReturnType<typeof vi.fn>;
    activateGeneration: ReturnType<typeof vi.fn>;
    flush: ReturnType<typeof vi.fn>;
    isDisabled: () => boolean;
  }> = [];
  class FakeLiveKitRoomAudio {
    callbacks: Record<string, (...args: unknown[]) => void> = {};
    reconnect = vi.fn().mockResolvedValue(false);
    setMuted = vi.fn();
    activateGeneration = vi.fn();
    flush = vi.fn();
    unlock = vi.fn().mockResolvedValue(undefined);
    dispose = vi.fn().mockResolvedValue(undefined);
    isDisabled = () => disabled;
    constructor() { liveKitInstances.push(this); }
    prepare(_roomCode: string, callbacks: Record<string, (...args: unknown[]) => void>) {
      this.callbacks = callbacks;
      return Promise.resolve(prepareResult);
    }
  }
  return {
    FakeLiveKitRoomAudio,
    liveKitInstances,
    setAvailability: (result: boolean, isDisabled: boolean) => {
      prepareResult = result;
      disabled = isDisabled;
    },
  };
});

vi.mock("@/lib/audio/livekit-room-audio", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/audio/livekit-room-audio")>();
  return { ...original, LiveKitRoomAudio: mocks.FakeLiveKitRoomAudio };
});
let DebateStage: typeof import("@/components/debate-stage")["DebateStage"];

function activeRoom(startedAt: string): Room {
  return {
    id: "room-id",
    code: "123456",
    topic: "人工智能时代是否仍应学习编程？",
    status: "running",
    visibility: "public",
    seq: 3,
    competition: {
      id: "competition-id", slug: "training-1v1", name: "1v1 辩论训练赛", tagline: "",
      description: "", rules: "", format: "1v1", seat_count: 2, ranked: false,
      allow_custom_topic: true, accent: "blue", live_count: 1,
    },
    season: null,
    owner: { id: "owner-id", real_name: "房主" },
    seats: [
      { seat_key: "aff_1", side: "aff", position: 1, label: "正方一辩", occupant_type: "ai", display_name: "乾元", is_ready: true, connected: true, is_me: false },
      { seat_key: "neg_1", side: "neg", position: 1, label: "反方一辩", occupant_type: "ai", display_name: "陈思远", is_ready: true, connected: true, is_me: false },
    ],
    current_stage: { key: "aff_case", name: "正方立论", kind: "speech", duration: 120, seat: "aff_1", ai_preparing: false },
    current_stage_index: 0,
    remaining_seconds: 110,
    turn_remaining_seconds: null,
    active_speech: {
      id: "speech-ai", seat_key: "aff_1", speaker_type: "ai", status: "playing", content: "AI 正在发言",
      playback_started_at: startedAt, stream_generation: "a".repeat(32), stream_sample_rate: 24_000,
    },
    my_seat: null,
    can_speak: false,
    speak_reason: "当前为 AI 发言",
    can_control: true,
    match_audio_archive_enabled: true,
    recent_events: [],
    speeches: [{
      id: "speech-ai", seat_key: "aff_1", speaker: "乾元", stage_key: "aff_case", content: "AI 正在发言",
      audio_url: "/media/123456/speech-ai.wav", duration_seconds: 20,
      playback_started_at: startedAt, playback_ends_at: new Date(new Date(startedAt).getTime() + 20_000).toISOString(),
      status: "playing", created_at: startedAt,
    }],
  };
}

describe("DebateStage RTC recovery", () => {
  beforeAll(async () => {
    vi.stubEnv("NEXT_PUBLIC_WEBRTC_AUDIO_ENABLED", "true");
    ({ DebateStage } = await import("@/components/debate-stage"));
  });

  afterAll(() => {
    vi.unstubAllEnvs();
  });

  beforeEach(() => {
    vi.useFakeTimers();
    mocks.liveKitInstances.length = 0;
    mocks.setAvailability(true, false);
  });

  it("waits for the room WebSocket before requesting spectator RTC credentials", async () => {
    const current = activeRoom(new Date().toISOString());
    const { rerender } = render(
      <DebateStage room={current} connected={false} mode="watch" />,
    );
    await act(async () => undefined);

    expect(mocks.liveKitInstances).toHaveLength(0);

    rerender(<DebateStage room={current} connected mode="watch" />);
    await act(async () => undefined);

    expect(mocks.liveKitInstances).toHaveLength(1);
  });

  it("does not create a reconnect storm when the server explicitly disables RTC", async () => {
    mocks.setAvailability(false, true);
    render(<DebateStage room={activeRoom(new Date().toISOString())} connected mode="debate" />);
    await act(async () => undefined);
    const rtc = mocks.liveKitInstances[0];

    expect(screen.getByText("实时比赛声音未启用，请联系系统管理员。")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(rtc.reconnect).not.toHaveBeenCalled();
  });

  it("keeps the single WebRTC path after recovery exhaustion and never falls back to PCM/WAV", async () => {
    const initialTime = new Date("2026-07-22T03:00:00.000Z");
    vi.setSystemTime(initialTime);
    render(<DebateStage room={activeRoom(initialTime.toISOString())} connected mode="debate" />);
    await act(async () => undefined);
    const rtc = mocks.liveKitInstances[0];
    expect(rtc).toBeDefined();
    expect(document.querySelectorAll("audio")).toHaveLength(0);
    expect(screen.queryByText(/已切换/)).not.toBeInTheDocument();

    act(() => rtc.callbacks.onFatalError?.("RTC fatal"));
    expect(screen.getByText("实时音频连接中断，正在进行第 1 次重连…")).toBeInTheDocument();
    expect(screen.queryByText(/已切换/)).not.toBeInTheDocument();

    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(rtc.reconnect).toHaveBeenCalledTimes(1);
    expect(screen.getByText("实时音频连接中断，正在进行第 2 次重连…")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(rtc.reconnect).toHaveBeenCalledTimes(2);
    expect(screen.getByText("实时音频连接中断，正在进行第 3 次重连…")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });

    await act(async () => { await Promise.resolve(); });
    expect(document.querySelectorAll("audio")).toHaveLength(0);
    expect(screen.getByText(/本次发言不会切换播放器/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "打开比赛控制台重试当前发言" })).toHaveAttribute("href", "/rooms/123456/control");
  });

  it("binds a late interrupt to its old generation instead of flushing the new active speech", async () => {
    const initialTime = new Date("2026-07-22T03:00:00.000Z");
    const current = activeRoom(initialTime.toISOString());
    const oldGeneration = "a".repeat(32);
    const newGeneration = "b".repeat(32);
    current.active_speech = {
      ...current.active_speech!,
      id: "speech-new",
      stream_generation: newGeneration,
    };
    current.speeches = [
      { ...current.speeches[0], id: "speech-old", stream_generation: oldGeneration, status: "interrupted" },
      { ...current.speeches[0], id: "speech-new", stream_generation: newGeneration, status: "playing" },
    ];

    render(<DebateStage
      room={current}
      connected
      mode="debate"
      liveEvent={{
        seq: 4,
        type: "speech.interrupted",
        payload: { speech_id: "speech-old" },
      }}
    />);
    await act(async () => undefined);

    const rtc = mocks.liveKitInstances[0];
    expect(rtc.activateGeneration).toHaveBeenCalledWith(newGeneration);
    expect(rtc.flush).toHaveBeenCalledWith(oldGeneration);
    expect(rtc.flush).not.toHaveBeenCalledWith("");
    expect(rtc.flush).not.toHaveBeenCalledWith(newGeneration);
  });

  it("cancels a pending reconnect when the authoritative track recovers by itself", async () => {
    const initialTime = new Date("2026-07-22T03:00:00.000Z");
    render(<DebateStage room={activeRoom(initialTime.toISOString())} connected mode="debate" />);
    await act(async () => undefined);
    const rtc = mocks.liveKitInstances[0];

    act(() => rtc.callbacks.onFatalError?.("temporary track loss"));
    expect(screen.getByText("实时音频连接中断，正在进行第 1 次重连…")).toBeInTheDocument();
    act(() => rtc.callbacks.onReady?.());
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });

    expect(rtc.reconnect).not.toHaveBeenCalled();
    expect(screen.queryByText(/正在进行第 1 次重连/)).not.toBeInTheDocument();
  });
});
