import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/audio/livekit-room-audio", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/audio/livekit-room-audio")>();
  class FakeLiveKitRoomAudio {
    prepare() { return Promise.resolve(true); }
    reconnect() { return Promise.resolve(true); }
    unlock() { return Promise.resolve(); }
    dispose() { return Promise.resolve(); }
    setMuted() {}
    activateGeneration() { return true; }
    flush() { return true; }
  }
  return { ...original, LiveKitRoomAudio: FakeLiveKitRoomAudio };
});

import { DebateStage } from "@/components/debate-stage";
import type { Room } from "@/lib/types";

class FakeAsrCaptureNode {
  static instances: FakeAsrCaptureNode[] = [];
  connect = vi.fn();
  disconnect = vi.fn();
  generation: number;
  port = {
    onmessage: null as ((event: MessageEvent) => void) | null,
    messages: [] as Record<string, unknown>[],
    postMessage: (message: Record<string, unknown>) => {
      this.port.messages.push(message);
      if (message.type === "flush" || message.type === "stop") {
        queueMicrotask(() => this.port.onmessage?.({
          data: { type: "flushed", generation: this.generation },
        } as MessageEvent));
      }
    },
  };

  constructor(_context: AudioContext, public name: string, options?: AudioWorkletNodeOptions) {
    this.generation = Number((options?.processorOptions as { generation?: number } | undefined)?.generation) || 0;
    FakeAsrCaptureNode.instances.push(this);
  }

  emitFrames(frames: Float32Array) {
    this.port.onmessage?.({ data: { type: "frames", generation: this.generation, frames } } as MessageEvent);
  }

  static latest() {
    return [...this.instances].reverse().find((item) => item.name === "jixia-asr-pcm-capture") || null;
  }
}

function room(overrides: Partial<Room> = {}): Room {
  return {
    id: "room-id",
    code: "123456",
    topic: "人工智能时代是否仍应学习编程？",
    status: "running",
    visibility: "public",
    seq: 3,
    competition: {
      id: "competition-id",
      slug: "training-1v1",
      name: "1v1 辩论训练赛",
      tagline: "",
      description: "",
      rules: "",
      format: "1v1",
      seat_count: 2,
      ranked: false,
      allow_custom_topic: true,
      accent: "blue",
      live_count: 1,
    },
    season: null,
    owner: { id: "user-id", real_name: "张三" },
    seats: [
      {
        seat_key: "aff_1",
        side: "aff",
        position: 1,
        label: "正方一辩",
        occupant_type: "human",
        display_name: "张三",
        is_ready: true,
        connected: true,
        is_me: true,
      },
      {
        seat_key: "neg_1",
        side: "neg",
        position: 1,
        label: "反方一辩",
        occupant_type: "ai",
        display_name: "乾元",
        is_ready: true,
        connected: true,
        is_me: false,
      },
    ],
    current_stage: { key: "aff_case", name: "正方立论", kind: "speech", duration: 120, seat: "aff_1" },
    current_stage_index: 0,
    remaining_seconds: 110,
    turn_remaining_seconds: null,
    active_speech: null,
    my_seat: "aff_1",
    can_speak: false,
    speak_reason: "比赛尚未开始",
    can_control: true,
    match_audio_archive_enabled: false,
    recent_events: [],
    speeches: [],
    ...overrides,
  };
}

function storedSpeech(status: string): Room["speeches"][number] {
  return {
    id: "speech-unsubmitted",
    seat_key: "aff_1",
    speaker: "正方一辩",
    stage_key: "aff_case",
    content: "",
    audio_url: "",
    duration_seconds: 0,
    playback_started_at: null,
    playback_ends_at: null,
    status,
    created_at: new Date().toISOString(),
  };
}

function markAsrReady(socket: {
  onopen: (() => void) | null;
  onmessage: ((event: MessageEvent) => void) | null;
} | null, speechId = "speech-asr-harness", stageKey = "aff_case") {
  socket?.onopen?.();
  socket?.onmessage?.({
    data: JSON.stringify({
      type: "ready",
      protocol_version: 2,
      speech_id: speechId,
      stage_key: stageKey,
    }),
  } as MessageEvent);
}

async function renderPendingFinish(finishResponse: Promise<Response> = Promise.resolve(new Response(JSON.stringify({ speech_id: "speech-unsubmitted" }), {
  status: 200,
  headers: { "Content-Type": "application/json" },
}))) {
  const stopTrack = vi.fn();
  const stream = { getTracks: () => [{ stop: stopTrack }] } as unknown as MediaStream;
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
  });
  class FakeRecorder {
    state: RecordingState = "inactive";
    mimeType = "audio/webm";
    ondataavailable: ((event: BlobEvent) => void) | null = null;
    onstop: (() => void) | null = null;
    constructor(public stream: MediaStream) {}
    start() { this.state = "recording"; }
    stop() { this.state = "inactive"; this.onstop?.(); }
  }
  class FakeSocket {
    static OPEN = 1;
    static latest: FakeSocket | null = null;
    readyState = FakeSocket.OPEN;
    bufferedAmount = 0;
    binaryType = "";
    onopen: (() => void) | null = null;
    onmessage: ((event: MessageEvent) => void) | null = null;
    onerror: (() => void) | null = null;
    onclose: ((event: CloseEvent) => void) | null = null;
    constructor() { FakeSocket.latest = this; }
    send = vi.fn();
    close = vi.fn();
  }
  class FakeContext {
    sampleRate = 16_000;
    destination = {};
    createMediaStreamSource() { return { connect: vi.fn() }; }
    createScriptProcessor() { return { connect: vi.fn(), onaudioprocess: null }; }
    close = vi.fn().mockResolvedValue(undefined);
  }
  vi.stubGlobal("MediaRecorder", FakeRecorder);
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal("AudioContext", FakeContext);
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-unsubmitted" }), { status: 200, headers: { "Content-Type": "application/json" } }))
    .mockImplementationOnce(() => finishResponse);
  vi.stubGlobal("fetch", fetchMock);
  const onPendingFinishChange = vi.fn();
  const rendered = render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言" })} connected mode="debate" onPendingFinishChange={onPendingFinishChange} />);
  const start = await screen.findByRole("button", { name: /开始发言/ });
  await waitFor(() => expect(start).toBeEnabled());
  fireEvent.click(start);
  const stop = await screen.findByRole("button", { name: /结束发言/ });
  markAsrReady(FakeSocket.latest, "speech-unsubmitted");
  fireEvent.click(stop);
  await waitFor(() => expect(FakeSocket.latest?.send).toHaveBeenCalledWith(JSON.stringify({ type: "finish" })));
  await act(async () => { FakeSocket.latest?.onerror?.(); });
  await screen.findByRole("dialog", { name: "提交前核对发言文字" });
  fireEvent.change(screen.getByLabelText("发言文字"), { target: { value: "比赛流程推进前保留的完整发言。" } });
  return { ...rendered, fetchMock, onPendingFinishChange, stopTrack };
}

async function renderAsrHarness(sampleRate = 16_000, roomOverrides: Partial<Room> = {}) {
  const stopTrack = vi.fn();
  const stream = { getTracks: () => [{ stop: stopTrack }] } as unknown as MediaStream;
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
  });
  class FakeRecorder {
    static stopCalls = 0;
    static instances: FakeRecorder[] = [];
    state: RecordingState = "inactive";
    mimeType = "audio/webm";
    ondataavailable: ((event: BlobEvent) => void) | null = null;
    onstop: (() => void) | null = null;
    constructor(public stream: MediaStream) { FakeRecorder.instances.push(this); }
    start() { this.state = "recording"; }
    stop() { FakeRecorder.stopCalls += 1; this.state = "inactive"; this.onstop?.(); }
  }
  class FakeSocket {
    static OPEN = 1;
    static latest: FakeSocket | null = null;
    static instances: FakeSocket[] = [];
    readyState = FakeSocket.OPEN;
    bufferedAmount = 0;
    binaryType = "";
    onopen: (() => void) | null = null;
    onmessage: ((event: MessageEvent) => void) | null = null;
    onerror: (() => void) | null = null;
    onclose: ((event: CloseEvent) => void) | null = null;
    send = vi.fn();
    close = vi.fn();
    constructor() { FakeSocket.latest = this; FakeSocket.instances.push(this); }
  }
  class FakeContext {
    sampleRate = sampleRate;
    state: AudioContextState = "running";
    destination = {};
    resume = vi.fn().mockResolvedValue(undefined);
    createMediaStreamSource() { return { connect: vi.fn(), disconnect: vi.fn() }; }
    close = vi.fn().mockResolvedValue(undefined);
  }
  vi.stubGlobal("MediaRecorder", FakeRecorder);
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal("AudioContext", FakeContext);
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-asr-harness" }), { status: 200, headers: { "Content-Type": "application/json" } }))
    .mockResolvedValue(new Response(JSON.stringify({ speech_id: "speech-asr-harness" }), { status: 200, headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);
  const rendered = render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言", ...roomOverrides })} connected mode="debate" />);
  const start = await screen.findByRole("button", { name: /开始发言|恢复发言/ });
  await waitFor(() => expect(start).toBeEnabled());
  fireEvent.click(start);
  const stop = await screen.findByRole("button", { name: /结束发言/ });
  const process = (value: number, length = 1_024) => {
    FakeAsrCaptureNode.latest()?.emitFrames(new Float32Array(length).fill(value));
  };
  return { ...rendered, FakeContext, FakeRecorder, FakeSocket, fetchMock, process, stop, stopTrack };
}

describe("DebateStage", () => {
  beforeEach(() => {
    FakeAsrCaptureNode.instances = [];
    vi.stubGlobal("AudioWorkletNode", FakeAsrCaptureNode);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });
  it("shows an explicit disabled reason until the server grants the speaking turn", async () => {
    const { rerender } = render(<DebateStage room={room()} connected mode="debate" />);
    const waiting = await screen.findByRole("button", { name: /等待轮次/ });
    expect(waiting).toBeDisabled();
    expect(waiting).toHaveTextContent("比赛尚未开始");

    rerender(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言" })} connected mode="debate" />);
    await waitFor(() => expect(screen.getByRole("button", { name: /开始发言/ })).toBeEnabled());
  });

  it("does not mislabel a transient room lock as another-device takeover", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "房间正在处理其他操作，请稍后重试。" }), {
        status: 409,
        headers: { "Content-Type": "application/json" },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    vi.stubGlobal("fetch", fetchMock);

    render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言" })} connected mode="debate" />);

    await waitFor(() => expect(screen.getByRole("button", { name: /开始发言/ })).toBeEnabled());
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/确认接管当前席位|其他设备已接管/)).not.toBeInTheDocument();
  });

  it("offers a non-forcing retry when device binding remains temporarily unavailable", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "数据库暂时不可用，请稍后重试。" }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);

    render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言" })} connected mode="debate" />);

    const retry = await screen.findByRole("button", { name: "重试绑定当前设备" });
    expect(screen.getByRole("button", { name: /设备绑定失败/ })).toBeDisabled();
    expect(screen.queryByText(/确认接管当前席位|其他设备已接管/)).not.toBeInTheDocument();
    fireEvent.click(retry);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    for (const [, init] of fetchMock.mock.calls) expect(init?.body).toBe(JSON.stringify({ force: false }));
  });

  it("labels the pre-match state and exposes the blocked reason on the control", async () => {
    render(<DebateStage room={room({
      status: "preparing",
      current_stage: null,
      remaining_seconds: null,
      can_speak: false,
      speak_reason: "等待所有辩手准备完成",
    })} connected mode="debate" />);

    expect(screen.getByRole("heading", { name: "比赛准备中" })).toBeInTheDocument();
    expect(screen.getByText(/正在建立实时语音.*不会开始比赛计时.*自动进入第一阶段/)).toBeInTheDocument();
    expect(screen.getByRole("timer", { name: "比赛准备中，完成后自动开场" })).toHaveTextContent("准备中完成后自动开场");
    const control = await screen.findByRole("button", { name: /等待轮次/ });
    expect(control).toHaveAttribute("data-state", "blocked");
    expect(control).toHaveAttribute("title", "等待所有辩手准备完成");
  });

  it("presents an automatic stage cue as a system prompt instead of a host speech timer", async () => {
    render(<DebateStage room={room({
      remaining_seconds: 6,
      can_speak: false,
      speak_reason: "主持人正在播报下一环节",
      current_stage: {
        key: "neg_case",
        name: "反方立论",
        kind: "announcement",
        duration: 6,
        cue: "主持人正在播报下一环节",
      },
    })} connected mode="debate" />);

    expect(screen.getAllByText("系统正在播放阶段提示")).toHaveLength(2);
    expect(screen.getByRole("timer", { name: "系统阶段提示还剩 00:06" })).toHaveTextContent("阶段提示音");
    await waitFor(() => expect(screen.getByTitle("系统正在播放阶段提示")).toHaveTextContent("等待轮次"));
    expect(document.body).not.toHaveTextContent("主持人");
  });

  it.each([
    ["new speech", room({ can_speak: true, speak_reason: "轮到你发言" }), /开始发言/],
    ["speech recovery", room({
      can_speak: false,
      speak_reason: "该席位正在另一设备发言",
      active_speech: { id: "speech-recover", seat_key: "aff_1", speaker_type: "human", status: "speaking", content: "已保存字幕" },
    }), /恢复发言/],
  ])("blocks %s until the realtime connection returns", async (_label, targetRoom, enabledName) => {
    const { rerender } = render(<DebateStage room={targetRoom} connected={false} mode="debate" />);
    const disconnected = await screen.findByRole("button", { name: /等待实时连接/ });
    expect(disconnected).toBeDisabled();
    expect(disconnected).toHaveTextContent("实时连接已断开，请等待重连后再发言");

    rerender(<DebateStage room={targetRoom} connected mode="debate" />);
    await waitFor(() => expect(screen.getByRole("button", { name: enabledName })).toBeEnabled());
  });

  it("never starts a microphone or speech merely because realtime reconnects", async () => {
    const getUserMedia = vi.fn();
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia },
    });
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      ok: true,
      lease_fingerprint: "own-device",
      seq: 3,
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const targetRoom = room({ can_speak: true, speak_reason: "轮到你发言" });
    const { rerender } = render(<DebateStage room={targetRoom} connected={false} mode="debate" />);

    expect(await screen.findByRole("button", { name: /等待实时连接/ })).toBeDisabled();
    rerender(<DebateStage room={{ ...targetRoom, seq: 4 }} connected mode="debate" />);

    await waitFor(() => expect(screen.getByRole("button", { name: /开始发言/ })).toBeEnabled());
    expect(getUserMedia).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/speech/start"))).toBe(false);
  });

  it("does not show a recording policy in the text-only stage", async () => {
    render(<DebateStage
      room={room({
        recording_consent: {
          organization_id: "org-1",
          required: true,
          granted: false,
          policy: { id: "policy-2", version: 2, title: "比赛录音政策 v2", content: "请重新阅读并确认当前版本。" },
          decision: null,
          decided_at: null,
          actor_user_id: null,
          record_id: null,
        },
      })}
      connected
      mode="debate"
      onConsentChanged={vi.fn().mockResolvedValue(undefined)}
    />);
    expect(screen.queryByRole("heading", { name: "比赛录音政策 v2" })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /当前 v2/ })).not.toBeInTheDocument();
  });

  it("keeps anonymous watch mode read-only", () => {
    const privateTranscript = "这段文字只能供辩手核对，绝不能出现在观战页";
    const { container } = render(<DebateStage room={room({
      my_seat: null,
      can_speak: false,
      can_control: false,
      active_speech: { id: "private-speech", seat_key: "aff_1", speaker_type: "human", status: "speaking", content: privateTranscript },
      speeches: [{
        id: "private-speech",
        seat_key: "aff_1",
        speaker: "张三",
        stage_key: "aff_case",
        content: privateTranscript,
        audio_url: "",
        duration_seconds: 0,
        playback_started_at: null,
        playback_ends_at: null,
        status: "speaking",
        created_at: new Date().toISOString(),
      }],
    })} connected mode="watch" />);
    expect(screen.queryByRole("button", { name: /开始发言|等待轮次|结束发言/ })).not.toBeInTheDocument();
    expect(screen.getByText("公开只读画面")).toBeInTheDocument();
    expect(screen.getByLabelText("当前赛况")).toHaveTextContent("张三");
    expect(screen.getByLabelText("当前赛况")).toHaveTextContent("正在发言");
    expect(screen.queryByText(privateTranscript)).not.toBeInTheDocument();
    expect(screen.queryByText("比赛发言将在这里实时呈现")).not.toBeInTheDocument();
    expect(container.querySelector(".subtitle-stage")).not.toBeInTheDocument();
    expect(container.firstElementChild).toHaveClass("watch-mode");
  });

  it("keeps a concrete speaker focus while a 1v1 free-debate turn waits to start", () => {
    const { container } = render(<DebateStage room={room({
      current_stage: {
        key: "free",
        name: "自由辩论",
        kind: "free",
        duration: 240,
        side: "neg",
        awaiting_human_start: true,
      },
      remaining_seconds: 240,
      turn_remaining_seconds: 30,
      active_speech: null,
      can_speak: false,
    })} connected mode="debate" />);

    expect(screen.getByLabelText("当前发言人：乾元")).toHaveTextContent("乾元反方一辩 · AI 辩手");
    expect(container.querySelector(".team-column.neg .stage-seat.active")).toHaveTextContent("乾元");
    expect(container.querySelector(".team-column.aff .stage-seat.active")).not.toBeInTheDocument();
  });

  it("uses the server-selected free-debate seat without guessing among several teammates", () => {
    const extraNegSeat = {
      ...room().seats[1],
      seat_key: "neg_2",
      position: 2,
      label: "反方二辩",
      display_name: "玄策",
    };
    const { container } = render(<DebateStage room={room({
      seats: [...room().seats, extraNegSeat],
      current_stage: {
        key: "free",
        name: "自由辩论",
        kind: "free",
        duration: 240,
        side: "neg",
        selected_human_seat: "neg_2",
        awaiting_human_start: true,
      },
      active_speech: null,
    })} connected mode="debate" />);

    expect(screen.getByLabelText("当前发言人：玄策")).toBeInTheDocument();
    expect(container.querySelectorAll(".team-column.neg .stage-seat.active")).toHaveLength(1);
    expect(container.querySelector(".team-column.neg .stage-seat.active")).toHaveTextContent("玄策");
  });

  it.each([
    ["completed", "比赛已完成"],
    ["review_required", "等待人工复核"],
    ["terminated", "比赛已终止"],
    ["cancelled", "房间已取消"],
  ] as const)("removes stale turn semantics from a %s watch projection", (status, label) => {
    const { container } = render(<DebateStage room={room({
      status,
      my_seat: null,
      can_control: false,
      can_speak: true,
      speak_reason: "本轮可发言",
      current_stage: {
        key: "free",
        name: "自由辩论",
        kind: "free",
        duration: 240,
        side: "neg",
        awaiting_human_start: true,
        selected_human_seat: "neg_1",
      },
      active_speech: null,
      remaining_seconds: 0,
      turn_remaining_seconds: 0,
    })} connected mode="watch" />);

    expect(screen.getByRole("heading", { name: label })).toBeInTheDocument();
    expect(screen.getByLabelText("比赛终态")).toHaveTextContent("比赛流程已经停止");
    expect(screen.getByRole("timer")).toHaveTextContent(`已结束${label}`);
    expect(screen.queryByText("00:00")).not.toBeInTheDocument();
    expect(screen.queryByText(/开始发言后计时|点击“开始发言”|等待下一位辩手/)).not.toBeInTheDocument();
    expect(container.querySelector(".stage-seat.active")).not.toBeInTheDocument();
    expect(container.querySelector(".stage-progress .current")).not.toBeInTheDocument();
  });

  it("never renders transcript or live caption text in spectator mode", () => {
    render(<DebateStage room={room({
      my_seat: null,
      can_control: false,
      can_view_transcript: false,
      active_speech: {
        id: "private-speech",
        seat_key: "neg_1",
        speaker_type: "ai",
        status: "playing",
        content: "观众不可见的完整发言文字",
        playback_started_at: new Date().toISOString(),
        stream_generation: "c".repeat(32),
        stream_sample_rate: 24_000,
      },
      caption_segments: [{
        segment_id: "private-caption",
        speech_id: "private-speech",
        seat_key: "neg_1",
        text: "观众不可见的实时字幕",
        is_final: false,
        start_ms: 0,
        end_ms: 1_000,
        updated_at: new Date().toISOString(),
        source: "agent",
        timing_basis: "audio_duration",
      }],
    })} connected mode="watch" liveEvent={{
      type: "caption.segment",
      speech_id: "private-speech",
      text: "观众不可见的事件字幕",
    }} />);

    expect(screen.queryByText(/观众不可见的/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /文字稿|字幕/ })).not.toBeInTheDocument();
    expect(screen.getByText("可通过比赛声音了解现场进程。")).toBeInTheDocument();
  });

  it("presents a service failure as one consistent paused state", () => {
    render(<DebateStage room={room({
      status: "paused",
      current_stage: null,
      failure_reason: "private provider stack trace",
      my_seat: null,
      can_control: false,
    })} connected mode="watch" />);

    expect(screen.getByText("服务异常暂停")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "比赛已安全暂停" })).toBeInTheDocument();
    expect(screen.queryByText("等待比赛开始")).not.toBeInTheDocument();
    expect(screen.queryByText("比赛已暂停")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("比赛因临时服务异常暂停");
  });

  it("does not offer reconnect controls after spectator capacity rejection", () => {
    const onReconnect = vi.fn();
    render(<DebateStage
      room={room({ my_seat: null, can_control: false })}
      connected={false}
      connectionError="系统观战总人数已达 5 人，请稍后重试。"
      connectionBlockedReason="capacity_full"
      onReconnect={onReconnect}
      mode="watch"
    />);

    expect(screen.getByText("观战已满")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("当前连接不会自动重试");
    expect(screen.queryByRole("button", { name: "立即重连" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "返回赛事大厅" })).toHaveAttribute("href", "/");
  });

  it("freezes and resumes the fixed-stage clock around AI preparation", () => {
    vi.useFakeTimers();
    const preparingStage = {
      key: "neg_case",
      name: "反方立论",
      kind: "speech" as const,
      duration: 120,
      seat: "neg_1",
      ai_preparing: true,
    };
    const preparingSpeech = {
      id: "ai-fixed-speech",
      seat_key: "neg_1",
      speaker_type: "ai" as const,
      status: "synthesizing" as const,
      content: "",
    };
    const { rerender } = render(<DebateStage room={room({
      current_stage: preparingStage,
      remaining_seconds: 110,
      active_speech: preparingSpeech,
    })} connected mode="watch" />);

    expect(screen.getByRole("timer")).toHaveAccessibleName("AI 正在准备发言，计时暂停在 01:50");
    act(() => { vi.advanceTimersByTime(5_000); });
    expect(screen.getByRole("timer")).toHaveAccessibleName("AI 正在准备发言，计时暂停在 01:50");

    rerender(<DebateStage room={room({
      current_stage: { ...preparingStage, ai_preparing: false },
      remaining_seconds: 110,
      active_speech: { ...preparingSpeech, status: "playing" },
    })} connected mode="watch" />);
    act(() => { vi.advanceTimersByTime(5_000); });
    expect(screen.getByRole("timer")).toHaveAccessibleName("剩余时间 01:45");
  });

  it("keeps the full human speaking time frozen until the speaker starts", () => {
    vi.useFakeTimers();
    const waitingStage = {
      key: "aff_case",
      name: "正方立论",
      kind: "speech" as const,
      duration: 120,
      seat: "aff_1",
      awaiting_human_start: true,
      human_speech_duration_seconds: 120,
    };
    const { rerender } = render(<DebateStage room={room({
      current_stage: waitingStage,
      remaining_seconds: 120,
      can_speak: true,
      speak_reason: "轮到你发言",
    })} connected mode="watch" />);

    const frozen = screen.getByRole("timer", { name: "完整发言时间 02:00，点击开始发言后计时" });
    expect(frozen).toHaveTextContent("02:00开始发言后计时");
    expect(frozen).not.toHaveTextContent("本环节剩余");
    expect(screen.getAllByText("张三点击“开始发言”后正式计时")).toHaveLength(2);
    act(() => { vi.advanceTimersByTime(8_000); });
    expect(screen.getByRole("timer")).toHaveAccessibleName("完整发言时间 02:00，点击开始发言后计时");

    rerender(<DebateStage room={room({
      seq: 5,
      current_stage: { ...waitingStage, awaiting_human_start: false },
      remaining_seconds: 120,
      active_speech: { id: "speech-human", seat_key: "aff_1", speaker_type: "human", status: "speaking", content: "" },
      can_speak: false,
    })} connected mode="watch" />);
    act(() => { vi.advanceTimersByTime(5_000); });
    expect(screen.getByRole("timer")).toHaveAccessibleName("剩余时间 01:55");
  });

  it("freezes and resumes both free-debate clocks around AI preparation", () => {
    vi.useFakeTimers();
    const preparingStage = {
      key: "free",
      name: "自由辩论",
      kind: "free" as const,
      duration: 300,
      turn_duration: 45,
      side: "neg" as const,
      ai_preparing: true,
      preparing_stage_remaining_seconds: 238,
      preparing_turn_remaining_seconds: 44,
    };
    const preparingSpeech = {
      id: "ai-free-speech",
      seat_key: "neg_1",
      speaker_type: "ai" as const,
      status: "synthesizing" as const,
      content: "",
    };
    const readOnlySeats = room().seats.map((seat) => ({ ...seat, is_me: false }));
    const { rerender } = render(<DebateStage room={room({
      current_stage: {
        ...preparingStage,
      },
      remaining_seconds: 238,
      turn_remaining_seconds: 44,
      active_speech: preparingSpeech,
      seats: readOnlySeats,
      my_seat: null,
      can_control: false,
    })} connected mode="debate" />);

    expect(screen.getByRole("timer")).toHaveAccessibleName("AI 正在准备发言，计时暂停在 03:58");
    expect(screen.getByText(/本轮剩余 00:30/)).toBeInTheDocument();
    expect(screen.getByText("AI 准备中 · 计时暂停")).toBeInTheDocument();
    expect(screen.getByText("AI 正在组织论点并合成语音…")).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(5_000); });
    expect(screen.getByRole("timer")).toHaveAccessibleName("AI 正在准备发言，计时暂停在 03:58");
    expect(screen.getByText(/本轮剩余 00:30/)).toBeInTheDocument();

    rerender(<DebateStage room={room({
      current_stage: { ...preparingStage, ai_preparing: false },
      remaining_seconds: 238,
      turn_remaining_seconds: 44,
      active_speech: { ...preparingSpeech, status: "playing" },
      seats: readOnlySeats,
      my_seat: null,
      can_control: false,
    })} connected mode="debate" />);
    act(() => { vi.advanceTimersByTime(5_000); });
    expect(screen.getByRole("timer")).toHaveAccessibleName("剩余时间 03:53");
    expect(screen.getByText(/本轮剩余 00:25/)).toBeInTheDocument();
  });

  it("shows a persistent connection failure after an authoritative snapshot is already visible", () => {
    render(<DebateStage room={room()} connected={false} connectionError="比赛房间不存在或已被删除。" mode="watch" />);
    expect(screen.getByRole("alert")).toHaveTextContent("比赛房间不存在");
  });

  it("lets the room owner retry a safely described failure over REST while realtime is disconnected", async () => {
    const recoveredRoom = room({ status: "running", seq: 8, failure_reason: "" });
    const onRoomChanged = vi.fn();
    const idempotencyKeys: string[] = [];
    let retryAttempts = 0;
    let resolveSecondAttempt: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn().mockImplementation((_url: RequestInfo | URL, init?: RequestInit) => {
      if (String(_url).includes("control-lease")) {
        return Promise.resolve(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 7 }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      retryAttempts += 1;
      idempotencyKeys.push(new Headers(init?.headers).get("X-Idempotency-Key") || "");
      if (retryAttempts === 1) {
        return Promise.resolve(new Response(JSON.stringify({ detail: "redis://secret-host:6379 timeout at /internal/retry" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        }));
      }
      return new Promise<Response>((resolve) => { resolveSecondAttempt = resolve; });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DebateStage
      room={room({
        status: "paused",
        seq: 7,
        failure_reason: "RedisConnectionError redis://secret-host:6379 /api/internal/provider",
        can_control: true,
        my_seat: null,
      })}
      connected={false}
      connectionError="实时连接已断开，正在重新连接…"
      mode="debate"
      onRoomChanged={onRoomChanged}
    />);

    expect(screen.getByText("比赛因临时服务异常暂停。")).toBeInTheDocument();
    expect(screen.getByText("比赛进度已保存，确认后可重试当前步骤。")).toBeInTheDocument();
    expect(screen.queryByText(/RedisConnectionError|secret-host|internal\/provider/)).not.toBeInTheDocument();
    const notices = screen.getAllByRole("alert");
    expect(notices).toHaveLength(2);
    expect(notices[0].parentElement).toBe(notices[1].parentElement);
    expect(notices[0].parentElement).toHaveClass("stage-notice-stack");

    fireEvent.click(screen.getByRole("button", { name: "重试异常步骤" }));
    expect(screen.getByRole("alertdialog")).toHaveTextContent("确认重试异常步骤");
    fireEvent.click(screen.getByRole("button", { name: "确认重试" }));
    expect(await screen.findByText("暂时无法重试，请稍后再试；比赛进度已安全保留。")).toBeInTheDocument();
    expect(screen.queryByText(/redis:\/\/|internal\/retry/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重试异常步骤" }));
    fireEvent.click(screen.getByRole("button", { name: "确认重试" }));
    await waitFor(() => expect(retryAttempts).toBe(2));
    expect(screen.getByRole("button", { name: "正在重试…" })).toBeDisabled();
    const retryCalls = fetchMock.mock.calls.filter(([url]) => String(url).includes("/api/rooms/123456/control/retry"));
    expect(retryCalls).toHaveLength(2);
    expect(retryCalls[1][1]).toEqual(expect.objectContaining({ method: "POST" }));
    expect(idempotencyKeys[0]).not.toBe("");
    expect(idempotencyKeys[1]).toBe(idempotencyKeys[0]);

    await act(async () => {
      resolveSecondAttempt?.(new Response(JSON.stringify({ room: recoveredRoom }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    });
    await waitFor(() => expect(onRoomChanged).toHaveBeenCalledWith(recoveredRoom));
  });

  it("uses a new retry idempotency key after a lost response is reconciled and a new failure occurs", async () => {
    const idempotencyKeys: string[] = [];
    const recoveredRoom = room({ status: "running", seq: 8, failure_reason: "" });
    const secondRecoveredRoom = room({ status: "running", seq: 10, failure_reason: "" });
    const onRoomChanged = vi.fn();
    const fetchMock = vi.fn().mockImplementation((_url: RequestInfo | URL, init?: RequestInit) => {
      if (String(_url).includes("control-lease")) {
        return Promise.resolve(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 7 }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      idempotencyKeys.push(new Headers(init?.headers).get("X-Idempotency-Key") || "");
      if (idempotencyKeys.length === 1) return Promise.reject(new TypeError("response lost"));
      return Promise.resolve(new Response(JSON.stringify({ room: secondRecoveredRoom }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    });
    vi.stubGlobal("fetch", fetchMock);

    const { rerender } = render(<DebateStage room={room({
      status: "paused",
      seq: 7,
      failure_reason: "first provider failure",
      can_control: true,
      my_seat: null,
    })} connected={false} mode="debate" onRoomChanged={onRoomChanged} />);
    fireEvent.click(screen.getByRole("button", { name: "重试异常步骤" }));
    fireEvent.click(screen.getByRole("button", { name: "确认重试" }));
    expect(await screen.findByText("暂时无法重试，请稍后再试；比赛进度已安全保留。")).toBeInTheDocument();

    rerender(<DebateStage room={recoveredRoom} connected mode="debate" onRoomChanged={onRoomChanged} />);
    expect(screen.queryByText("比赛因临时服务异常暂停。")).not.toBeInTheDocument();
    rerender(<DebateStage room={room({
      status: "paused",
      seq: 9,
      failure_reason: "second provider failure",
      can_control: true,
      my_seat: null,
    })} connected={false} mode="debate" onRoomChanged={onRoomChanged} />);
    fireEvent.click(screen.getByRole("button", { name: "重试异常步骤" }));
    fireEvent.click(screen.getByRole("button", { name: "确认重试" }));

    await waitFor(() => expect(onRoomChanged).toHaveBeenCalledWith(secondRecoveredRoom));
    expect(idempotencyKeys).toHaveLength(2);
    expect(idempotencyKeys[0]).not.toBe("");
    expect(idempotencyKeys[1]).not.toBe(idempotencyKeys[0]);
  });

  it("asks non-owners to wait for the owner and keeps the failure banner accessible", async () => {
    const { container } = render(<DebateStage room={room({
      status: "paused",
      failure_reason: "private provider stack trace /srv/secret/config",
      can_control: false,
      my_seat: null,
    })} connected mode="watch" />);

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("比赛因临时服务异常暂停。");
    expect(alert).toHaveTextContent("请等待房主在比赛控制页处理；恢复后页面会自动同步");
    expect(alert).not.toHaveTextContent("stack trace");
    expect(screen.queryByRole("button", { name: /重试异常步骤/ })).not.toBeInTheDocument();
    expect((await axe.run(container, { rules: { "color-contrast": { enabled: false } } })).violations).toEqual([]);
  });

  it("blocks failure retry while a human participant is still disconnected", async () => {
    render(<DebateStage room={room({
      status: "paused",
      failure_reason: "Agent 服务异常",
      can_control: true,
      seats: room().seats.map((seat) => seat.seat_key === "neg_1" ? { ...seat, occupant_type: "human", connected: false } : seat),
      pause_health: {
        paused_at: new Date().toISOString(),
        paused_duration_seconds: 3,
        is_stale: false,
        capacity_consuming: true,
        reason_code: "service_failure_and_participant_disconnected",
        recommended_action: "retry",
        can_terminate_to_release_capacity: true,
      },
    })} connected mode="debate" />);

    expect(screen.getByText("比赛同时遇到服务异常和真人断线。")).toBeInTheDocument();
    expect(screen.getByText(/先等待 乾元 重新连接/)).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: "等待真人重连" });
    expect(retry).toBeDisabled();
    expect(retry).toHaveAttribute("title", "等待全部真人重新连接");
  });

  it("has no automatically detectable accessibility violations", async () => {
    const { container } = render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言" })} connected mode="debate" />);
    await screen.findByRole("button", { name: /开始发言/ });
    const result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(result.violations).toEqual([]);
  });

  it("turns the old page read-only after another device takes over", async () => {
    const { rerender } = render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言" })} connected mode="debate" />);
    await waitFor(() => expect(screen.getByRole("button", { name: /开始发言/ })).toBeEnabled());
    rerender(<DebateStage room={room({
      can_speak: true,
      speak_reason: "轮到你发言",
      seq: 4,
      recent_events: [{ seq: 4, type: "seat.control_taken_over", payload: { seat_key: "aff_1", lease_fingerprint: "other-device" }, created_at: new Date().toISOString() }],
    })} connected mode="debate" />);
    const takenOver = await screen.findByRole("button", { name: /其他设备已接管/ });
    expect(takenOver).toBeDisabled();
    expect(takenOver).toHaveTextContent("该席位已由另一设备控制");
    const recoveryAction = screen.getByRole("button", { name: "确认接管当前席位" });
    const recoverySurface = recoveryAction.closest(".stage-device-recovery");
    expect(recoverySurface?.parentElement).toBe(document.body);
    await waitFor(() => expect(recoveryAction).toHaveFocus());
    fireEvent.click(recoveryAction);
    await waitFor(() => {
      const calls = vi.mocked(fetch).mock.calls;
      expect(calls.some(([, init]) => init?.body === JSON.stringify({ force: true }))).toBe(true);
    });
  });

  it("opens settings with keyboard focus and restores focus when Escape closes it", async () => {
    render(<DebateStage room={room()} connected mode="debate" />);
    const settings = screen.getByRole("button", { name: "比赛控制" });
    fireEvent.click(settings);
    await screen.findByRole("button", { name: "关闭比赛控制" });
    expect(screen.getByRole("link", { name: "进入完整比赛控制台" })).toHaveAttribute("href", "/rooms/123456/control");
    await waitFor(() => expect(screen.getByRole("button", { name: "提前结束比赛" })).toHaveFocus());
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "比赛控制" })).not.toBeInTheDocument();
    expect(settings).toHaveFocus();
  });

  it("lets only the room owner pause from the debate stage and suppresses rapid duplicate requests", async () => {
    let resolvePause: ((response: Response) => void) | undefined;
    const pausedRoom = room({ status: "paused", seq: 4, remaining_seconds: 109 });
    const pauseResponse = new Promise<Response>((resolve) => { resolvePause = resolve; });
    const fetchMock = vi.fn((url: RequestInfo | URL, _init?: RequestInit) => {
      if (String(url).includes("control-lease")) {
        return Promise.resolve(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      if (String(url).includes("/control/pause")) return pauseResponse;
      return Promise.reject(new Error(`unexpected request: ${String(url)}`));
    });
    vi.stubGlobal("fetch", fetchMock);
    const onRoomChanged = vi.fn();
    render(<DebateStage room={room()} connected mode="debate" onRoomChanged={onRoomChanged} onLeave={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "比赛控制" }));
    const pause = screen.getByRole("button", { name: "暂停比赛" });
    fireEvent.click(pause);
    fireEvent.click(pause);
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("/control/pause"))).toHaveLength(1));
    resolvePause?.(new Response(JSON.stringify({ room: pausedRoom }), { status: 200, headers: { "Content-Type": "application/json" } }));
    await waitFor(() => expect(onRoomChanged).toHaveBeenCalledWith(pausedRoom));
    expect(screen.getByText("比赛已暂停。")).toBeInTheDocument();
  });

  it("keeps REST match controls available while realtime is reconnecting", async () => {
    const pausedRoom = room({ status: "paused", seq: 4, remaining_seconds: 109 });
    const fetchMock = vi.fn((url: RequestInfo | URL) => {
      if (String(url).includes("control-lease")) {
        return Promise.resolve(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      if (String(url).includes("/control/pause")) {
        return Promise.resolve(new Response(JSON.stringify({ room: pausedRoom }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      return Promise.reject(new Error(`unexpected request: ${String(url)}`));
    });
    vi.stubGlobal("fetch", fetchMock);
    const onRoomChanged = vi.fn();
    render(<DebateStage room={room()} connected={false} connectionError="实时连接正在重连" mode="debate" onRoomChanged={onRoomChanged} />);
    fireEvent.click(screen.getByRole("button", { name: "比赛控制" }));
    const pause = screen.getByRole("button", { name: "暂停比赛" });
    expect(pause).toBeEnabled();
    fireEvent.click(pause);
    await waitFor(() => expect(onRoomChanged).toHaveBeenCalledWith(pausedRoom));
  });

  it("reuses an action idempotency key after a lost response", async () => {
    const keys: string[] = [];
    let attempts = 0;
    const pausedRoom = room({ status: "paused", seq: 4, remaining_seconds: 109 });
    const fetchMock = vi.fn((url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes("control-lease")) {
        return Promise.resolve(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      if (String(url).includes("/control/pause")) {
        attempts += 1;
        keys.push(new Headers(init?.headers).get("X-Idempotency-Key") || "");
        if (attempts === 1) return Promise.reject(new Error("响应丢失"));
        return Promise.resolve(new Response(JSON.stringify({ room: pausedRoom, replayed: true }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      return Promise.reject(new Error(`unexpected request: ${String(url)}`));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<DebateStage room={room()} connected mode="debate" onRoomChanged={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "比赛控制" }));
    fireEvent.click(screen.getByRole("button", { name: "暂停比赛" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "暂停比赛" }));
    await waitFor(() => expect(keys).toHaveLength(2));
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
  });

  it("keeps watch mode read-only and links owners to the explicit control page", () => {
    render(<DebateStage room={room({ my_seat: "aff_1", can_control: true })} connected={false} mode="watch" />);
    expect(screen.getByText("房主观战 · 只读")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试异常步骤" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "观看设置" }));
    expect(screen.queryByRole("button", { name: "暂停比赛" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "提前结束比赛" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "进入完整比赛控制台" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "退出比赛页面" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "进入比赛控制" })).toHaveAttribute("href", "/rooms/123456/control");
  });

  it("keeps match-wide controls owner-only while every participant can leave the page", () => {
    render(<DebateStage room={room({ can_control: false })} connected mode="debate" onLeave={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "更多操作" }));
    expect(screen.getByRole("dialog", { name: "更多操作" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "关闭更多操作" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "暂停比赛" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "提前结束比赛" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "退出比赛页面" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "仅退出比赛页面" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /AI 接替/ })).not.toBeInTheDocument();
  });

  it("shows the authoritative disconnect countdown and keeps the seat visibly human", () => {
    const disconnected = room({
      seats: room().seats.map((seat) => seat.seat_key === "neg_1" ? { ...seat, occupant_type: "human", display_name: "李四", connected: false } : seat),
      disconnect_grace: {
        will_pause: true,
        pending: [
          {
            seat_key: "neg_1",
            display_name: "李四",
            remaining_seconds: 37,
          },
        ],
      },
    });
    const { container } = render(<DebateStage room={disconnected} connected mode="debate" onLeave={vi.fn()} />);

    expect(screen.getByText("李四已断线，真人席位保持不变。")).toBeInTheDocument();
    expect(screen.getByText(/37 秒后自动暂停/)).toBeInTheDocument();
    expect(screen.getByText("反方一辩 · 真人 · 已断线")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "比赛控制" }));
    expect(screen.getByText(/真人断线后席位保持真人身份.*60 秒超时后自动暂停/)).toBeInTheDocument();
    expect(container).not.toHaveTextContent(/AI (接管|接替|代打)/);
    expect(screen.queryByRole("button", { name: /AI 接替/ })).not.toBeInTheDocument();
  });

  it("offers resume after a disconnect timeout once every human is back", () => {
    render(<DebateStage room={room({
      status: "paused",
      failure_reason: "真人辩手断线超过 60 秒，比赛已安全暂停。",
      pause_health: {
        paused_at: new Date().toISOString(),
        paused_duration_seconds: 2,
        is_stale: false,
        capacity_consuming: true,
        reason_code: "participant_disconnected",
        recommended_action: "resume",
        can_terminate_to_release_capacity: true,
      },
    })} connected mode="debate" onLeave={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "全部真人已重新连接" })).toBeInTheDocument();
    expect(screen.getByText(/全部真人已重新连接；确认现场就绪后/)).toBeInTheDocument();
    expect(screen.queryByText("比赛因临时服务异常暂停。")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "比赛控制" }));
    expect(screen.getByRole("button", { name: "继续比赛" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /异常暂停，请先重试/ })).not.toBeInTheDocument();
  });

  it("describes a participant-disconnect pause accurately to anonymous watchers", () => {
    render(<DebateStage room={room({
      status: "paused",
      my_seat: null,
      can_control: false,
      can_view_transcript: false,
      failure_reason: "真人辩手断线超过 60 秒，比赛已安全暂停。",
      pause_health: {
        paused_at: new Date().toISOString(),
        paused_duration_seconds: 2,
        is_stale: false,
        capacity_consuming: true,
        reason_code: "participant_disconnected",
        recommended_action: "resume",
        can_terminate_to_release_capacity: true,
      },
    })} connected mode="watch" />);

    expect(screen.getByRole("heading", { name: "全部真人已重新连接" })).toBeInTheDocument();
    expect(screen.getByText(/真人断线超过 60 秒，比赛已自动暂停/)).toBeInTheDocument();
    expect(screen.queryByText(/临时服务异常/)).not.toBeInTheDocument();
  });

  it("does not let the owner resume a disconnect timeout while a human is still absent", () => {
    const paused = room({
      status: "paused",
      seats: room().seats.map((seat) => seat.seat_key === "neg_1"
        ? { ...seat, occupant_type: "human", display_name: "李四", connected: false }
        : seat),
      failure_reason: "真人辩手断线超过 60 秒，比赛已安全暂停。",
      pause_health: {
        paused_at: new Date().toISOString(),
        paused_duration_seconds: 2,
        is_stale: false,
        capacity_consuming: true,
        reason_code: "participant_disconnected",
        recommended_action: "resume",
        can_terminate_to_release_capacity: true,
      },
    });
    render(<DebateStage room={paused} connected mode="debate" onLeave={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "等待真人辩手重新连接" })).toBeInTheDocument();
    expect(screen.getByText(/仍在等待 李四 重新连接/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "比赛控制" }));
    expect(screen.getByRole("button", { name: "等待全部真人重新连接" })).toBeDisabled();
  });

  it("requires an explicit accessible confirmation before ending or leaving a match", async () => {
    const terminatedRoom = room({ status: "terminated", seq: 4, remaining_seconds: 0 });
    const fetchMock = vi.fn((url: RequestInfo | URL) => {
      if (String(url).includes("control-lease")) {
        return Promise.resolve(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      if (String(url).includes("/control/terminate")) {
        return Promise.resolve(new Response(JSON.stringify({ room: terminatedRoom }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      return Promise.reject(new Error(`unexpected request: ${String(url)}`));
    });
    vi.stubGlobal("fetch", fetchMock);
    const onLeave = vi.fn();
    const onRoomChanged = vi.fn();
    render(<DebateStage room={room()} connected mode="debate" onLeave={onLeave} onRoomChanged={onRoomChanged} />);

    fireEvent.click(screen.getByRole("button", { name: "比赛控制" }));
    fireEvent.click(screen.getByRole("button", { name: "提前结束比赛" }));
    const terminateDialog = screen.getByRole("alertdialog", { name: "确认提前结束比赛？" });
    expect(terminateDialog).toHaveTextContent("不可恢复");
    expect(document.querySelectorAll('[aria-modal="true"]')).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("alertdialog", { name: "确认提前结束比赛？" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "比赛控制" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "提前结束比赛" }));
    fireEvent.click(screen.getByRole("button", { name: "确认提前结束" }));
    await waitFor(() => expect(onRoomChanged).toHaveBeenCalledWith(terminatedRoom));

    const { unmount } = render(<DebateStage room={room()} connected mode="debate" onLeave={onLeave} />);
    const brandExit = screen.getAllByRole("button", { name: "退出比赛页面" }).at(-1)!;
    fireEvent.click(brandExit);
    expect(screen.getByRole("alertdialog", { name: "确认退出比赛页面？" })).toHaveTextContent("席位归属会保留");
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    await waitFor(() => expect(brandExit).toHaveFocus());
    fireEvent.click(brandExit);
    fireEvent.click(screen.getByRole("button", { name: "确认退出页面" }));
    expect(onLeave).toHaveBeenCalledOnce();
    unmount();
  });

  it("lets a speaker explicitly stop and correct even a clean ASR result before submission", async () => {
    const { FakeSocket, fetchMock, process } = await renderAsrHarness();
    markAsrReady(FakeSocket.latest);
    process(0.1, 4_096);
    const beforeUnload = new Event("beforeunload", { cancelable: true });
    expect(window.dispatchEvent(beforeUnload)).toBe(false);
    act(() => { window.dispatchEvent(new PopStateEvent("popstate")); });
    expect(screen.getByRole("alert")).toHaveTextContent("当前发言尚未提交");
    fireEvent.click(screen.getByRole("button", { name: "退出比赛页面" }));
    expect(screen.queryByRole("alertdialog", { name: "确认退出比赛页面？" })).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("请先结束并提交当前发言");
    fireEvent.click(screen.getByRole("button", { name: "结束发言并修改文字" }));
    await waitFor(() => expect(FakeSocket.latest?.send).toHaveBeenCalledWith(JSON.stringify({ type: "finish" })));
    await act(async () => {
      FakeSocket.latest?.onmessage?.({ data: JSON.stringify({ type: "asr", text: "自动识别的完整文字。", is_final: true }) } as MessageEvent);
    });

    const editor = await screen.findByRole("dialog", { name: "提交前核对发言文字" });
    expect(editor).toHaveTextContent("请核对并修正语音识别文字");
    expect(screen.getByLabelText("发言文字")).toHaveValue("自动识别的完整文字。");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    fireEvent.change(screen.getByLabelText("发言文字"), { target: { value: "人工修正后的完整文字。" } });
    fireEvent.click(screen.getByRole("button", { name: "确认文字并提交" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(JSON.parse(String((fetchMock.mock.calls[2][1] as RequestInit).body))).toEqual({
      speech_id: "speech-asr-harness",
      content: "人工修正后的完整文字。",
    });
  });

  it("keeps a text-only room on AudioWorklet ASR without MediaRecorder or an audio upload", async () => {
    const { FakeRecorder, FakeSocket, fetchMock, process, stop } = await renderAsrHarness(16_000, {
      match_audio_archive_enabled: false,
    });
    expect(FakeRecorder.instances).toHaveLength(0);

    markAsrReady(FakeSocket.latest);
    process(0.1, 4_096);
    fireEvent.click(stop);
    await waitFor(() => expect(FakeSocket.latest?.send).toHaveBeenCalledWith(JSON.stringify({ type: "finish" })));
    await act(async () => {
      FakeSocket.latest?.onmessage?.({ data: JSON.stringify({ type: "asr", text: "只保存这一段识别文字。", is_final: true }) } as MessageEvent);
    });

    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/speech/finish"))).toBe(true));
    const requestedUrls = fetchMock.mock.calls.map(([url]) => String(url));
    expect(requestedUrls.some((url) => /\/speech\/[^/]+\/audio$/.test(url))).toBe(false);
    const finishCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/speech/finish"));
    expect(JSON.parse(String((finishCall?.[1] as RequestInit).body))).toEqual({
      speech_id: "speech-asr-harness",
      content: "只保存这一段识别文字。",
    });
  });

  it("ignores a duplicated ASR final instead of duplicating the submitted transcript", async () => {
    const { FakeSocket, fetchMock, process, stop } = await renderAsrHarness();
    const socket = FakeSocket.latest!;
    markAsrReady(socket);
    process(0.1, 4_096);
    fireEvent.click(stop);
    await waitFor(() => expect(socket.send).toHaveBeenCalledWith(JSON.stringify({ type: "finish" })));
    await act(async () => {
      const final = { data: JSON.stringify({ type: "asr", text: "最终字幕只应提交一次。", is_final: true }) } as MessageEvent;
      socket.onmessage?.(final);
      socket.onmessage?.(final);
    });

    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/speech/finish"))).toBe(true));
    const finishCalls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/speech/finish"));
    expect(finishCalls).toHaveLength(1);
    expect(JSON.parse(String((finishCalls[0][1] as RequestInit).body))).toEqual({
      speech_id: "speech-asr-harness",
      content: "最终字幕只应提交一次。",
    });
  });

  it("renders only the latest ASR segment on the live stage while retaining full text for submission", async () => {
    const { FakeSocket, process, container } = await renderAsrHarness();
    markAsrReady(FakeSocket.latest);
    process(0.1, 4_096);
    const subtitle = container.querySelector<HTMLElement>(".subtitle-stage p");
    if (!subtitle) throw new Error("live subtitle was not rendered");
    Object.defineProperty(subtitle, "scrollWidth", { configurable: true, value: 640 });
    subtitle.scrollLeft = 0;

    await act(async () => {
      FakeSocket.latest?.onmessage?.({ data: JSON.stringify({ type: "asr", text: "第一句正在说", is_final: false }) } as MessageEvent);
    });
    expect(subtitle).toHaveTextContent("第一句正在说");
    expect(subtitle.scrollLeft).toBe(0);
    expect(subtitle.closest(".subtitle-stage")).toHaveAttribute("data-caption-line", "single");

    await act(async () => {
      FakeSocket.latest?.onmessage?.({ data: JSON.stringify({ type: "asr", text: "第一句。", is_final: true }) } as MessageEvent);
      FakeSocket.latest?.onmessage?.({ data: JSON.stringify({ type: "asr", text: "第一句。第二句正在说，而且舞台只显示最新短句", is_final: false }) } as MessageEvent);
    });
    expect(subtitle).toHaveTextContent("而且舞台只显示最新短句");
    expect(subtitle).not.toHaveTextContent("第一句。");
  });

  it.each([
    ["paused", "比赛已暂停", "比赛已暂停，恢复后将从当前进度继续。"],
    ["terminated", "比赛已经结束", "比赛已经结束，发言和实时字幕已停止。"],
  ] as const)("cleans microphone, AudioWorklet, ASR socket and subtitle when the room becomes %s", async (status, alertText, idleText) => {
    const harness = await renderAsrHarness();
    const socket = harness.FakeSocket.latest!;
    const worklet = FakeAsrCaptureNode.latest();
    markAsrReady(socket);
    harness.process(0.1, 4_096);
    await act(async () => {
      socket.onmessage?.({ data: JSON.stringify({ type: "asr", text: "不应残留的实时字幕", is_final: false }) } as MessageEvent);
    });
    expect(harness.container.querySelector(".subtitle-stage p")).toHaveTextContent("不应残留的实时字幕");

    harness.rerender(<DebateStage room={room({
      status,
      seq: 4,
      can_speak: false,
      speak_reason: status === "paused" ? "比赛已暂停" : "比赛已结束",
      active_speech: null,
      remaining_seconds: status === "paused" ? 90 : 0,
    })} connected mode="debate" />);

    expect(await screen.findByRole("alert")).toHaveTextContent(alertText);
    await waitFor(() => expect(harness.stopTrack).toHaveBeenCalledOnce());
    expect(socket.close).toHaveBeenCalled();
    expect(worklet?.port.messages).toContainEqual(expect.objectContaining({ type: "stop" }));
    expect(worklet?.disconnect).toHaveBeenCalled();
    expect(harness.container.querySelector(".subtitle-stage p")).toHaveTextContent(idleText);
    expect(harness.container.querySelector(".subtitle-stage p")).not.toHaveTextContent("不应残留的实时字幕");
    expect(screen.queryByRole("button", { name: /结束发言/ })).not.toBeInTheDocument();
  });

  it("reconnects the duplex ASR stream during capture and ignores late frames from the old socket", async () => {
    const { FakeSocket, container } = await renderAsrHarness();
    const first = FakeSocket.latest!;
    markAsrReady(first);
    await act(async () => {
      first.onerror?.();
      first.onclose?.({ code: 1006 } as CloseEvent);
      await new Promise((resolve) => window.setTimeout(resolve, 300));
    });

    const second = FakeSocket.latest!;
    expect(second).not.toBe(first);
    expect(FakeSocket.instances).toHaveLength(2);
    act(() => second.onopen?.());
    expect(second.send).toHaveBeenCalledWith(expect.stringContaining('"type":"authenticate"'));
    markAsrReady(second);

    await act(async () => {
      first.onmessage?.({ data: JSON.stringify({ type: "asr", text: "旧连接迟到字幕", is_final: false }) } as MessageEvent);
      second.onmessage?.({ data: JSON.stringify({ type: "asr", text: "重连后的实时字幕", is_final: false }) } as MessageEvent);
    });
    expect(container.querySelector(".subtitle-stage p")).toHaveTextContent("重连后的实时字幕");
    expect(container.querySelector(".subtitle-stage p")).not.toHaveTextContent("旧连接迟到字幕");
  });

  it("keeps one AudioWorklet graph across ASR reconnect and preserves the interrupted partial for review", async () => {
    const { FakeSocket, process, stop } = await renderAsrHarness();
    const first = FakeSocket.latest!;
    markAsrReady(first);
    process(0.1, 4_096);
    await act(async () => {
      first.onmessage?.({
        data: JSON.stringify({ type: "asr", text: "断线前已经说出的半句话", is_final: false }),
      } as MessageEvent);
      first.onclose?.({ code: 1006 } as CloseEvent);
      // Capture must continue while the replacement WebSocket is opening.
      process(0.2, 1_024);
      await new Promise((resolve) => window.setTimeout(resolve, 300));
    });

    const second = FakeSocket.latest!;
    expect(second).not.toBe(first);
    expect(FakeAsrCaptureNode.instances).toHaveLength(1);
    markAsrReady(second);
    expect(second.send.mock.calls.some(([payload]) => payload instanceof ArrayBuffer)).toBe(true);
    process(0.1, 4_096);
    fireEvent.click(stop);
    await waitFor(() => expect(second.send).toHaveBeenCalledWith(JSON.stringify({ type: "finish" })));
    await act(async () => {
      second.onmessage?.({
        data: JSON.stringify({ type: "asr", text: "，重连后继续完成。", is_final: true }),
      } as MessageEvent);
    });

    const review = await screen.findByRole("dialog", { name: "提交前核对发言文字" });
    expect(review).toHaveTextContent("字幕连接已恢复");
    expect(screen.getByLabelText("发言文字")).toHaveValue("断线前已经说出的半句话，重连后继续完成。");
  });

  it("surfaces a bounded reconnect failure instead of leaving an endless reconnect notice", async () => {
    const { FakeSocket } = await renderAsrHarness();
    for (let attempt = 0; attempt <= 3; attempt += 1) {
      const socket = FakeSocket.latest!;
      socket.onclose?.({ code: 1006 } as CloseEvent);
      await act(async () => {
        await new Promise((resolve) => window.setTimeout(resolve, attempt < 3 ? 300 * (2 ** attempt) : 0));
      });
    }

    expect(await screen.findByRole("alert")).toHaveTextContent("字幕连接已中断");
    expect(screen.queryByText("正在重连…")).not.toBeInTheDocument();
  });

  it("bounds the browser ASR socket buffer instead of accumulating PCM indefinitely", async () => {
    const { FakeSocket, process } = await renderAsrHarness();
    markAsrReady(FakeSocket.latest);
    FakeSocket.latest!.bufferedAmount = 600 * 1024;

    await act(async () => process(0.2));

    expect(await screen.findByRole("alert")).toHaveTextContent("字幕网络发送缓慢");
    expect(FakeSocket.latest!.send).not.toHaveBeenCalledWith(expect.any(ArrayBuffer));
  });

  it("reports unsupported fullscreen instead of throwing an unhandled rejection", async () => {
    Object.defineProperty(document, "fullscreenEnabled", { configurable: true, value: false });
    render(<DebateStage room={room()} connected mode="watch" />);
    fireEvent.click(screen.getByRole("button", { name: "切换全屏" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("不支持网页全屏");
  });

  it("allows the same device to recover its authoritative in-progress speech", async () => {
    render(<DebateStage room={room({
      can_speak: false,
      speak_reason: "该席位正在另一设备发言",
      active_speech: { id: "speech-recover", seat_key: "aff_1", speaker_type: "human", status: "speaking", content: "已保存的前半段字幕" },
    })} connected mode="debate" />);
    const recover = await screen.findByRole("button", { name: /恢复发言/ });
    await waitFor(() => expect(recover).toBeEnabled());
    expect(recover).toHaveTextContent("恢复同一设备的进行中发言");
  });

  it("identifies a matching active speech as recording on the current device", async () => {
    const stream = { getTracks: () => [{ stop: vi.fn() }] } as unknown as MediaStream;
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    class FakeRecorder {
      state: RecordingState = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      constructor(public stream: MediaStream) {}
      start() { this.state = "recording"; }
      stop() { this.state = "inactive"; this.onstop?.(); }
    }
    class FakeSocket {
      static OPEN = 1;
      readyState = 0;
      binaryType = "";
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      send = vi.fn();
      close = vi.fn();
    }
    class FakeContext {
      sampleRate = 16_000;
      destination = {};
      createMediaStreamSource() { return { connect: vi.fn() }; }
      createScriptProcessor() { return { connect: vi.fn(), onaudioprocess: null }; }
      close = vi.fn().mockResolvedValue(undefined);
    }
    vi.stubGlobal("MediaRecorder", FakeRecorder);
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal("AudioContext", FakeContext);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-local" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    const { rerender } = render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言" })} connected mode="debate" />);
    const start = await screen.findByRole("button", { name: /开始发言/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    await screen.findByRole("button", { name: /结束发言/ });

    rerender(<DebateStage room={room({
      can_speak: false,
      speak_reason: "该席位正在另一设备发言",
      active_speech: { id: "speech-local", seat_key: "aff_1", speaker_type: "human", status: "speaking", content: "" },
    })} connected mode="debate" />);
    expect(screen.getByText("当前设备正在发言")).toBeInTheDocument();
    expect(screen.queryByText("该席位正在另一设备发言")).not.toBeInTheDocument();

    rerender(<DebateStage room={room({
      can_speak: false,
      speak_reason: "该席位正在另一设备发言",
      active_speech: { id: "speech-other", seat_key: "aff_1", speaker_type: "human", status: "speaking", content: "" },
    })} connected mode="debate" />);
    expect(screen.getByText("该席位正在另一设备发言")).toBeInTheDocument();
  });

  it("keeps the capped free-debate turn visible until the selected human starts", () => {
    vi.useFakeTimers();
    render(<DebateStage room={room({
      can_speak: true,
      speak_reason: "轮到你发言",
      current_stage: {
        key: "free",
        name: "自由辩论",
        kind: "free",
        duration: 300,
        side: "aff",
        turn_duration: 40,
        awaiting_human_start: true,
      },
      turn_remaining_seconds: 40,
    })} connected mode="debate" />);
    const button = screen.getByRole("button", { name: /点击开始发言后计时.*单轮时长 00:30/ });
    expect(button).toHaveTextContent("点击开始发言后计时 · 单轮时长 00:30");

    act(() => { vi.advanceTimersByTime(45_000); });

    expect(button).toHaveTextContent("单轮时长 00:30");
    expect(button).not.toHaveTextContent("00:00");
  });

  it("counts down only after an authoritative free-debate turn has started", () => {
    vi.useFakeTimers();
    render(<DebateStage room={room({
      can_speak: false,
      speak_reason: "其他辩手正在发言",
      current_stage: {
        key: "free",
        name: "自由辩论",
        kind: "free",
        duration: 300,
        side: "aff",
        turn_duration: 40,
        turn_started_at: "2026-07-24T00:00:00+08:00",
      },
      turn_remaining_seconds: 25,
      active_speech: { id: "speech-active", seat_key: "aff_2", speaker_type: "human", status: "speaking", content: "" },
    })} connected mode="debate" />);

    expect(screen.getByRole("button", { name: /本轮剩余 00:25/ })).toHaveTextContent("本轮剩余 00:25");
    act(() => { vi.advanceTimersByTime(5_000); });
    expect(screen.getByRole("button", { name: /本轮剩余 00:20/ })).toHaveTextContent("本轮剩余 00:20");
  });

  it("shows the next human action instead of a misleading subtitle loading state between speeches", () => {
    const base = room();
    const { container } = render(<DebateStage room={room({
      active_speech: null,
      current_stage: { key: "neg_case", name: "反方立论", kind: "speech", duration: 120, seat: "neg_1" },
      seats: [
        ...base.seats.filter((seat) => seat.seat_key !== "neg_1"),
        {
          seat_key: "neg_1",
          side: "neg",
          position: 1,
          label: "反方一辩",
          occupant_type: "human",
          display_name: "李同学",
          is_ready: true,
          connected: true,
          is_me: false,
        },
      ],
    })} connected mode="debate" />);

    expect(container.querySelector(".subtitle-stage p")).toHaveTextContent("等待李同学开始发言");
    expect(container.querySelector(".subtitle-stage small")).toHaveTextContent("李同学 · 待发言");
    expect(container.querySelector(".subtitle-stage p")).not.toHaveTextContent("字幕准备中");
  });

  it("shows an actionable error when free-debate microphone permission is denied", async () => {
    vi.stubGlobal("MediaRecorder", class {});
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockRejectedValue(new DOMException("denied", "NotAllowedError")) },
    });
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    render(<DebateStage room={room({
      can_speak: true,
      speak_reason: "轮到你发言",
      current_stage: { key: "free", name: "自由辩论", kind: "free", duration: 300, side: "aff", turn_duration: 40 },
      turn_remaining_seconds: 40,
    })} connected mode="debate" />);
    const start = await screen.findByRole("button", { name: /开始发言/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    expect(await screen.findByRole("alert")).toHaveTextContent("麦克风权限被拒绝");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: /开始发言/ })).toBeEnabled();
  });

  it("times out a hanging microphone request and restores the start control", async () => {
    vi.stubGlobal("MediaRecorder", class {});
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockReturnValue(new Promise<MediaStream>(() => undefined)) },
    });
    const { rerender } = render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言" })} connected mode="debate" />);
    const start = await screen.findByRole("button", { name: /开始发言/ });
    await waitFor(() => expect(start).toBeEnabled());
    vi.useFakeTimers();
    fireEvent.click(start);
    expect(screen.getByRole("button", { name: /正在启动麦克风/ })).toBeDisabled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(12_000);
    });
    expect(screen.getByRole("alert")).toHaveTextContent("麦克风启动超时");
    expect(screen.getByRole("button", { name: /开始发言/ })).toBeEnabled();
  });

  it("times out speech-start, releases the microphone, and retries with the same idempotency key", async () => {
    const stopTracks = [vi.fn(), vi.fn()];
    const streams = stopTracks.map((stop) => ({ getTracks: () => [{ stop }] }) as unknown as MediaStream);
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValueOnce(streams[0]).mockResolvedValueOnce(streams[1]) },
    });
    const speechStartKeys: string[] = [];
    let speechStartAttempts = 0;
    const fetchMock = vi.fn().mockImplementation((url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).endsWith("/control-lease")) {
        return Promise.resolve(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      if (String(url).endsWith("/speech/start")) {
        speechStartAttempts += 1;
        speechStartKeys.push(new Headers(init?.headers).get("X-Idempotency-Key") || "");
        if (speechStartAttempts === 1) {
          return new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
          });
        }
        return Promise.resolve(new Response(JSON.stringify({ speech_id: "speech-after-retry", resumed: true }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      return Promise.resolve(new Response(JSON.stringify({ detail: "unexpected request" }), { status: 500, headers: { "Content-Type": "application/json" } }));
    });
    vi.stubGlobal("fetch", fetchMock);

    const { rerender } = render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言" })} connected mode="debate" />);
    const start = await screen.findByRole("button", { name: /开始发言/ });
    await waitFor(() => expect(start).toBeEnabled());
    vi.useFakeTimers();
    fireEvent.click(start);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByRole("button", { name: /正在启动麦克风/ })).toBeDisabled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(12_000);
    });
    expect(screen.getByRole("alert")).toHaveTextContent("发言启动的网络响应超时");
    expect(screen.getByRole("alert")).toHaveTextContent("沿用同一请求");
    expect(stopTracks[0]).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: /开始发言/ })).toBeEnabled();

    vi.useRealTimers();
    fireEvent.click(screen.getByRole("button", { name: /开始发言/ }));
    await screen.findByRole("button", { name: /结束发言/ });
    expect(speechStartKeys).toHaveLength(2);
    expect(speechStartKeys[0]).not.toBe("");
    expect(speechStartKeys[1]).toBe(speechStartKeys[0]);

    rerender(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言" })} connected={false} mode="debate" />);
    expect(screen.getByRole("button", { name: /结束发言/ })).toBeEnabled();
  });

  it("surfaces the authoritative free-debate speech race and releases the microphone", async () => {
    const stopTrack = vi.fn();
    const stream = { getTracks: () => [{ stop: stopTrack }] } as unknown as MediaStream;
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    class FakeRecorder {
      state: RecordingState = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      constructor(public stream: MediaStream) {}
      start() { this.state = "recording"; }
      stop() { this.state = "inactive"; this.onstop?.(); }
    }
    vi.stubGlobal("MediaRecorder", FakeRecorder);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "其他辩手正在发言" }), { status: 409, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    render(<DebateStage room={room({
      can_speak: true,
      speak_reason: "轮到你发言",
      current_stage: { key: "free", name: "自由辩论", kind: "free", duration: 300, side: "aff", turn_duration: 40 },
    })} connected mode="debate" />);
    const start = await screen.findByRole("button", { name: /开始发言/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    expect(await screen.findByRole("alert")).toHaveTextContent("其他辩手正在发言");
    expect(stopTrack).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: /开始发言/ })).toBeEnabled();
  });

  it("cancels a pending microphone request when the free-debate side changes", async () => {
    vi.stubGlobal("MediaRecorder", class {});
    const stopTrack = vi.fn();
    const lateStream = { getTracks: () => [{ stop: stopTrack }] } as unknown as MediaStream;
    let resolveMicrophone!: (stream: MediaStream) => void;
    const microphone = new Promise<MediaStream>((resolve) => { resolveMicrophone = resolve; });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockReturnValue(microphone) },
    });
    const initialStage = { key: "free", name: "自由辩论", kind: "free" as const, duration: 300, side: "aff" as const, turn_duration: 40 };
    const { rerender } = render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言", current_stage: initialStage })} connected mode="debate" />);
    const start = await screen.findByRole("button", { name: /开始发言/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    expect(screen.getByRole("button", { name: /正在启动麦克风/ })).toBeDisabled();

    rerender(<DebateStage room={room({
      can_speak: false,
      speak_reason: "自由辩论当前轮到反方",
      current_stage: { ...initialStage, side: "neg" },
    })} connected mode="debate" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("轮次已切换");
    resolveMicrophone(lateStream);
    await waitFor(() => expect(stopTrack).toHaveBeenCalledOnce());
    expect(screen.getByRole("button", { name: /等待轮次/ })).toBeDisabled();
  });

  it("releases the microphone and aborts speech-start when the stage advances", async () => {
    const stopTrack = vi.fn();
    const stream = { getTracks: () => [{ stop: stopTrack }] } as unknown as MediaStream;
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    let speechStartSignal: AbortSignal | undefined;
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockImplementationOnce((_url: string, init?: RequestInit) => {
        speechStartSignal = init?.signal || undefined;
        return new Promise<Response>((_resolve, reject) => {
          speechStartSignal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
        });
      });
    vi.stubGlobal("fetch", fetchMock);
    const initialStage = { key: "aff_case", name: "正方立论", kind: "speech" as const, duration: 120, seat: "aff_1" };
    const { rerender } = render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言", current_stage: initialStage })} connected mode="debate" />);
    const start = await screen.findByRole("button", { name: /开始发言/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("button", { name: /正在启动麦克风/ })).toBeDisabled();

    rerender(<DebateStage room={room({
      can_speak: false,
      speak_reason: "当前轮到反方一辩",
      current_stage: { key: "neg_case", name: "反方立论", kind: "speech", duration: 120, seat: "neg_1" },
    })} connected mode="debate" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("阶段或自由辩论轮次已切换");
    expect(speechStartSignal?.aborted).toBe(true);
    expect(stopTrack).toHaveBeenCalledOnce();
  });

  it("stops and releases the microphone when the server interrupts the speech", async () => {
    const stopTrack = vi.fn();
    const stream = { getTracks: () => [{ stop: stopTrack }] } as unknown as MediaStream;
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    class FakeRecorder {
      state: RecordingState = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      constructor(public stream: MediaStream) {}
      start() { this.state = "recording"; }
      stop() { this.state = "inactive"; this.onstop?.(); }
    }
    class FakeSocket {
      static OPEN = 1;
      readyState = 0;
      binaryType = "";
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      send = vi.fn();
      close = vi.fn();
    }
    class FakeContext {
      static latestProcessor: { connect: ReturnType<typeof vi.fn>; onaudioprocess: ((event: AudioProcessingEvent) => void) | null } | null = null;
      sampleRate = 16_000;
      destination = {};
      createMediaStreamSource() { return { connect: vi.fn() }; }
      createScriptProcessor() {
        FakeContext.latestProcessor = { connect: vi.fn(), onaudioprocess: null };
        return FakeContext.latestProcessor;
      }
      close = vi.fn().mockResolvedValue(undefined);
    }
    vi.stubGlobal("MediaRecorder", FakeRecorder);
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal("AudioContext", FakeContext);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-human" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    const { rerender } = render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言" })} connected mode="debate" />);
    const start = await screen.findByRole("button", { name: /开始发言/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    await screen.findByRole("button", { name: /结束发言/ });

    rerender(<DebateStage room={room({
      seq: 4,
      can_speak: false,
      speak_reason: "比赛已终止",
      status: "terminated",
      active_speech: null,
      recent_events: [{
        seq: 4,
        type: "speech.interrupted",
        payload: { speech_id: "speech-human", seat_key: "aff_1", reason: "match_terminated" },
        created_at: new Date().toISOString(),
      }],
    })} connected mode="debate" />);

    await screen.findByRole("alert");
    expect(screen.getByRole("alert")).toHaveTextContent("麦克风已关闭");
    expect(stopTrack).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: /重试提交发言/ })).not.toBeInTheDocument();
  });

  it("waits for the final ASR sentence before submitting a human speech", async () => {
    const stream = { getTracks: () => [{ stop: vi.fn() }] } as unknown as MediaStream;
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    class FakeRecorder {
      state: RecordingState = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      constructor(public stream: MediaStream) {}
      start() { this.state = "recording"; }
      stop() { this.state = "inactive"; this.onstop?.(); }
    }
    class FakeSocket {
      static OPEN = 1;
      static latest: FakeSocket | null = null;
      readyState = FakeSocket.OPEN;
      binaryType = "";
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      constructor() { FakeSocket.latest = this; }
      send = vi.fn();
      close = vi.fn();
    }
    class FakeContext {
      static latestProcessor: { connect: ReturnType<typeof vi.fn>; onaudioprocess: ((event: AudioProcessingEvent) => void) | null } | null = null;
      sampleRate = 16_000;
      destination = {};
      createMediaStreamSource() { return { connect: vi.fn() }; }
      createScriptProcessor() {
        FakeContext.latestProcessor = { connect: vi.fn(), onaudioprocess: null };
        return FakeContext.latestProcessor;
      }
      close = vi.fn().mockResolvedValue(undefined);
    }
    vi.stubGlobal("MediaRecorder", FakeRecorder);
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal("AudioContext", FakeContext);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-final" }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言" })} connected mode="debate" />);
    const start = await screen.findByRole("button", { name: /开始发言/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    const stop = await screen.findByRole("button", { name: /结束发言/ });
    markAsrReady(FakeSocket.latest, "speech-final");
    const voicedInput = new Float32Array(4096).fill(0.1);
    FakeAsrCaptureNode.latest()?.emitFrames(voicedInput);
    fireEvent.click(stop);
    const finishing = await screen.findByRole("button", { name: /正在整理发言/ });
    expect(finishing).toBeDisabled();
    await waitFor(() => expect(FakeSocket.latest?.send).toHaveBeenCalledWith(JSON.stringify({ type: "finish" })));
    await act(async () => {
      FakeSocket.latest?.onmessage?.({ data: JSON.stringify({ type: "asr", text: "最后一句。", is_final: true }) } as MessageEvent);
    });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    const finishRequest = fetchMock.mock.calls[2][1] as RequestInit;
    expect(JSON.parse(String(finishRequest.body))).toEqual({ speech_id: "speech-final", content: "最后一句。" });
  });

  it("requires manual review when a partial ASR fragment is never finalized", async () => {
    const stream = { getTracks: () => [{ stop: vi.fn() }] } as unknown as MediaStream;
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    class FakeRecorder {
      state: RecordingState = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      constructor(public stream: MediaStream) {}
      start() { this.state = "recording"; }
      stop() { this.state = "inactive"; this.onstop?.(); }
    }
    class FakeSocket {
      static OPEN = 1;
      static latest: FakeSocket | null = null;
      readyState = FakeSocket.OPEN;
      binaryType = "";
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      constructor() { FakeSocket.latest = this; }
      send = vi.fn();
      close = vi.fn();
    }
    class FakeContext {
      sampleRate = 16_000;
      destination = {};
      createMediaStreamSource() { return { connect: vi.fn() }; }
      createScriptProcessor() { return { connect: vi.fn(), onaudioprocess: null }; }
      close = vi.fn().mockResolvedValue(undefined);
    }
    vi.stubGlobal("MediaRecorder", FakeRecorder);
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal("AudioContext", FakeContext);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-partial" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    const { rerender } = render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言" })} connected mode="debate" />);
    const start = await screen.findByRole("button", { name: /开始发言/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    const stop = await screen.findByRole("button", { name: /结束发言/ });
    markAsrReady(FakeSocket.latest, "speech-partial");
    await act(async () => {
      FakeSocket.latest?.onmessage?.({ data: JSON.stringify({ type: "asr", text: "去。", is_final: false }) } as MessageEvent);
    });
    expect(screen.getByText("去。")).toBeInTheDocument();

    vi.useFakeTimers();
    fireEvent.click(stop);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(31_000);
    });

    const editor = screen.getByRole("dialog", { name: "提交前核对发言文字" });
    expect(editor).toHaveTextContent("语音识别未确认最后一段字幕");
    expect(screen.getByLabelText("发言文字")).toHaveValue("");
    fireEvent.change(screen.getByLabelText("发言文字"), { target: { value: "弱网期间保留的发言文字。" } });
    rerender(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言" })} connected={false} mode="debate" />);
    expect(screen.getByRole("button", { name: /提交保留的发言/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: "确认文字并提交" })).toBeEnabled();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("requires an explicit discard after a match is terminated with an unsubmitted recording", async () => {
    const stopTrack = vi.fn();
    const stream = { getTracks: () => [{ stop: stopTrack }] } as unknown as MediaStream;
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    class FakeRecorder {
      state: RecordingState = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      constructor(public stream: MediaStream) {}
      start() { this.state = "recording"; }
      stop() { this.state = "inactive"; this.onstop?.(); }
    }
    class FakeSocket {
      static OPEN = 1;
      static latest: FakeSocket | null = null;
      readyState = FakeSocket.OPEN;
      binaryType = "";
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      constructor() { FakeSocket.latest = this; }
      send = vi.fn();
      close = vi.fn();
    }
    class FakeContext {
      sampleRate = 16_000;
      destination = {};
      createMediaStreamSource() { return { connect: vi.fn() }; }
      createScriptProcessor() { return { connect: vi.fn(), onaudioprocess: null }; }
      close = vi.fn().mockResolvedValue(undefined);
    }
    vi.stubGlobal("MediaRecorder", FakeRecorder);
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal("AudioContext", FakeContext);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-unsubmitted" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const onPendingFinishChange = vi.fn();

    const { container, rerender } = render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言" })} connected mode="debate" onPendingFinishChange={onPendingFinishChange} />);
    const start = await screen.findByRole("button", { name: /开始发言/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    const stop = await screen.findByRole("button", { name: /结束发言/ });
    markAsrReady(FakeSocket.latest, "speech-unsubmitted");
    fireEvent.click(stop);
    await waitFor(() => expect(FakeSocket.latest?.send).toHaveBeenCalledWith(JSON.stringify({ type: "finish" })));
    await act(async () => {
      FakeSocket.latest?.onmessage?.({ data: JSON.stringify({ type: "asr", text: "", is_final: true }) } as MessageEvent);
    });
    await screen.findByRole("dialog", { name: "提交前核对发言文字" });
    fireEvent.change(screen.getByLabelText("发言文字"), { target: { value: "比赛结束前尚未提交的发言文字。" } });
    expect(onPendingFinishChange).toHaveBeenCalledWith(true);

    rerender(<DebateStage room={room({
      status: "terminated",
      seq: 4,
      remaining_seconds: 0,
      active_speech: null,
      can_speak: false,
      speak_reason: "比赛已结束",
    })} connected mode="debate" onPendingFinishChange={onPendingFinishChange} />);

    const expiredDialog = screen.getByRole("dialog", { name: "本次发言已停止提交" });
    expect(expiredDialog).toHaveTextContent("比赛已终止，本次未提交文字无法再保存");
    expect(screen.getByLabelText("发言文字")).toHaveValue("比赛结束前尚未提交的发言文字。");
    expect(screen.getByLabelText("发言文字")).toHaveAttribute("readonly");
    expect(screen.getByRole("button", { name: /本次发言无法提交/ })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "确认文字并提交" })).not.toBeInTheDocument();
    const discard = screen.getByRole("button", { name: "清除未提交内容并查看结果" });
    expect(discard).toHaveFocus();
    expect((await axe.run(container, { rules: { "color-contrast": { enabled: false } } })).violations).toEqual([]);
    fireEvent.click(discard);

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "本次发言已停止提交" })).not.toBeInTheDocument());
    expect(onPendingFinishChange).toHaveBeenLastCalledWith(false);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(stopTrack).toHaveBeenCalled();
  });

  it.each(["completed", "review_required"])("blocks a timed-out speech from being submitted after the room becomes %s", async (status) => {
    const { rerender, fetchMock, onPendingFinishChange } = await renderPendingFinish();
    rerender(<DebateStage room={room({
      status,
      seq: 4,
      remaining_seconds: 0,
      active_speech: null,
      can_speak: false,
      speak_reason: "比赛流程已结束",
      speeches: [storedSpeech("timed_out")],
    })} connected mode="debate" onPendingFinishChange={onPendingFinishChange} />);

    const editor = screen.getByRole("dialog", { name: "本次发言已停止提交" });
    expect(editor).toHaveTextContent("本次发言已经超时关闭");
    expect(screen.getByLabelText("发言文字")).toHaveAttribute("readonly");
    expect(screen.queryByRole("button", { name: "确认文字并提交" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "清除未提交内容并查看结果" }));

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(onPendingFinishChange).toHaveBeenLastCalledWith(false);
  });

  it("binds manual transcript recovery to its original stage key", async () => {
    const { rerender, fetchMock } = await renderPendingFinish();
    rerender(<DebateStage room={room({
      seq: 5,
      can_speak: false,
      speak_reason: "当前轮到反方",
      active_speech: null,
      current_stage: { key: "neg_case", name: "反方立论", kind: "speech", duration: 120, seat: "neg_1" },
    })} connected mode="debate" />);

    const dialog = screen.getByRole("dialog", { name: "本次发言已停止提交" });
    expect(dialog).toHaveTextContent("比赛已从“正方立论”切换到“反方立论”");
    expect(screen.getByLabelText("发言文字")).toHaveAttribute("readonly");
    expect(screen.queryByRole("button", { name: "确认文字并提交" })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("stops an in-flight text submission when the websocket reports room completion", async () => {
    let resolveFinish: ((response: Response) => void) | undefined;
    const finishResponse = new Promise<Response>((resolve) => { resolveFinish = resolve; });
    const { rerender, fetchMock, onPendingFinishChange } = await renderPendingFinish(finishResponse);
    fireEvent.click(screen.getByRole("button", { name: "确认文字并提交" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(screen.getByRole("button", { name: /正在整理发言/ })).toBeDisabled();

    rerender(<DebateStage room={room({
      status: "completed",
      seq: 4,
      remaining_seconds: 0,
      active_speech: null,
      can_speak: false,
      speak_reason: "比赛已结束",
      speeches: [storedSpeech("timed_out")],
    })} connected mode="debate" onPendingFinishChange={onPendingFinishChange} />);
    expect(await screen.findByRole("dialog", { name: "本次发言已停止提交" })).toHaveTextContent("本次发言已经超时关闭");
    expect(screen.getByRole("button", { name: "清除未提交内容并查看结果" })).toBeInTheDocument();

    await act(async () => {
      resolveFinish?.(new Response(JSON.stringify({ speech_id: "speech-unsubmitted", timed_out: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    });
    expect(screen.getByRole("dialog", { name: "本次发言已停止提交" })).toBeInTheDocument();
    expect(onPendingFinishChange).toHaveBeenLastCalledWith(true);
  });

  it("keeps the recording editable and retryable after a recoverable finish conflict", async () => {
    const conflict = Promise.resolve(new Response(JSON.stringify({ detail: "该席位已在其他设备上接管。" }), {
      status: 409,
      headers: { "Content-Type": "application/json" },
    }));
    const { fetchMock } = await renderPendingFinish(conflict);

    fireEvent.click(screen.getByRole("button", { name: "确认文字并提交" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    const editor = await screen.findByRole("dialog", { name: "提交前核对发言文字" });
    expect(editor).toHaveTextContent("该席位已在其他设备上接管");
    expect(editor).toHaveTextContent("发言文字仍保留在当前页面");
    expect(screen.getByLabelText("发言文字")).not.toHaveAttribute("readonly");
    expect(screen.getByRole("button", { name: "确认文字并提交" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /放弃未提交内容/ })).not.toBeInTheDocument();
  });

  it("allows explicit discard when the matching pending speech is interrupted", async () => {
    const { rerender, fetchMock, onPendingFinishChange } = await renderPendingFinish();
    rerender(<DebateStage room={room({
      status: "completed",
      seq: 4,
      remaining_seconds: 0,
      active_speech: null,
      can_speak: false,
      speak_reason: "比赛已结束",
      speeches: [storedSpeech("interrupted")],
    })} connected mode="debate" onPendingFinishChange={onPendingFinishChange} />);

    const dialog = screen.getByRole("dialog", { name: "本次发言已停止提交" });
    expect(dialog).toHaveTextContent("本次发言已被比赛控制中断");
    expect(screen.queryByRole("button", { name: "确认文字并提交" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "清除未提交内容并查看结果" }));
    await waitFor(() => expect(onPendingFinishChange).toHaveBeenLastCalledWith(false));
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("keeps an empty-ASR recording and requires a manual transcript before submission", async () => {
    const stream = { getTracks: () => [{ stop: vi.fn() }] } as unknown as MediaStream;
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    class FakeRecorder {
      state: RecordingState = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      constructor(public stream: MediaStream) {}
      start() { this.state = "recording"; }
      stop() { this.state = "inactive"; this.onstop?.(); }
    }
    class FakeSocket {
      static OPEN = 1;
      static latest: FakeSocket | null = null;
      readyState = FakeSocket.OPEN;
      binaryType = "";
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      constructor() { FakeSocket.latest = this; }
      send = vi.fn();
      close = vi.fn();
    }
    class FakeContext {
      sampleRate = 16_000;
      destination = {};
      createMediaStreamSource() { return { connect: vi.fn() }; }
      createScriptProcessor() { return { connect: vi.fn(), onaudioprocess: null }; }
      close = vi.fn().mockResolvedValue(undefined);
    }
    vi.stubGlobal("MediaRecorder", FakeRecorder);
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal("AudioContext", FakeContext);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-empty" }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言" })} connected mode="debate" />);
    const start = await screen.findByRole("button", { name: /开始发言/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    const stop = await screen.findByRole("button", { name: /结束发言/ });
    markAsrReady(FakeSocket.latest, "speech-empty");
    fireEvent.click(stop);
    await waitFor(() => expect(FakeSocket.latest?.send).toHaveBeenCalledWith(JSON.stringify({ type: "finish" })));
    await act(async () => {
      FakeSocket.latest?.onmessage?.({ data: JSON.stringify({ type: "asr", text: "", is_final: true }) } as MessageEvent);
    });

    const editor = await screen.findByRole("dialog", { name: "提交前核对发言文字" });
    expect(editor).toHaveTextContent("未能完成语音活动检测");
    expect(screen.getByLabelText("发言文字")).toHaveFocus();
    expect(screen.getByRole("button", { name: "确认文字并提交" })).toBeDisabled();
    const submit = screen.getByRole("button", { name: /提交保留的发言/ });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByLabelText("发言文字"), { target: { value: "手动补充的完整发言。" } });
    expect(submit).toBeEnabled();
    fireEvent.click(submit);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(JSON.parse(String((fetchMock.mock.calls[2][1] as RequestInit).body))).toEqual({ speech_id: "speech-empty", content: "手动补充的完整发言。" });
  });  it("preserves the recovered server transcript for manual review when ASR rejects resumed audio", async () => {
    const stream = { getTracks: () => [{ stop: vi.fn() }] } as unknown as MediaStream;
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia: vi.fn().mockResolvedValue(stream) } });
    class FakeRecorder {
      state: RecordingState = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      constructor(public stream: MediaStream) {}
      start() { this.state = "recording"; }
      stop() { this.state = "inactive"; this.onstop?.(); }
    }
    class FakeSocket {
      static OPEN = 1;
      static latest: FakeSocket | null = null;
      readyState = FakeSocket.OPEN;
      binaryType = "";
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      constructor() { FakeSocket.latest = this; }
      send = vi.fn();
      close = vi.fn();
    }
    class FakeContext {
      sampleRate = 16_000;
      destination = {};
      createMediaStreamSource() { return { connect: vi.fn() }; }
      createScriptProcessor() { return { connect: vi.fn(), onaudioprocess: null }; }
      close = vi.fn().mockResolvedValue(undefined);
    }
    vi.stubGlobal("MediaRecorder", FakeRecorder);
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal("AudioContext", FakeContext);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-recover", resumed: true }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-recover" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    render(<DebateStage room={room({
      can_speak: false,
      speak_reason: "该席位正在另一设备发言",
      active_speech: { id: "speech-recover", seat_key: "aff_1", speaker_type: "human", status: "speaking", content: "服务端已保存的前半段。" },
    })} connected mode="debate" />);
    const recover = await screen.findByRole("button", { name: /恢复发言/ });
    await waitFor(() => expect(recover).toBeEnabled());
    fireEvent.click(recover);
    const stop = await screen.findByRole("button", { name: /结束发言/ });
    markAsrReady(FakeSocket.latest, "speech-recover");
    fireEvent.click(stop);
    await waitFor(() => expect(FakeSocket.latest?.send).toHaveBeenCalledWith(JSON.stringify({ type: "finish" })));
    await act(async () => {
      FakeSocket.latest?.onmessage?.({ data: JSON.stringify({ type: "asr_rejected", reason: "silence" }) } as MessageEvent);
    });
    const editor = await screen.findByRole("dialog", { name: "提交前核对发言文字" });
    expect(editor).toHaveTextContent("未检测到清晰语音");
    expect(screen.getByLabelText("发言文字")).toHaveValue("服务端已保存的前半段。");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("appends a new ASR final to the recovered server transcript", async () => {
    const stream = { getTracks: () => [{ stop: vi.fn() }] } as unknown as MediaStream;
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia: vi.fn().mockResolvedValue(stream) } });
    class FakeRecorder {
      state: RecordingState = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      constructor(public stream: MediaStream) {}
      start() { this.state = "recording"; }
      stop() { this.state = "inactive"; this.onstop?.(); }
    }
    class FakeSocket {
      static OPEN = 1;
      static latest: FakeSocket | null = null;
      readyState = FakeSocket.OPEN;
      binaryType = "";
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      constructor() { FakeSocket.latest = this; }
      send = vi.fn();
      close = vi.fn();
    }
    class FakeContext {
      sampleRate = 16_000;
      destination = {};
      createMediaStreamSource() { return { connect: vi.fn() }; }
      createScriptProcessor() { return { connect: vi.fn(), onaudioprocess: null }; }
      close = vi.fn().mockResolvedValue(undefined);
    }
    vi.stubGlobal("MediaRecorder", FakeRecorder);
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal("AudioContext", FakeContext);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-recover-final", resumed: true }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-recover-final" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    render(<DebateStage room={room({
      can_speak: false,
      speak_reason: "该席位正在另一设备发言",
      active_speech: { id: "speech-recover-final", seat_key: "aff_1", speaker_type: "human", status: "speaking", content: "服务端前半段。" },
    })} connected mode="debate" />);
    const recover = await screen.findByRole("button", { name: /恢复发言/ });
    await waitFor(() => expect(recover).toBeEnabled());
    fireEvent.click(recover);
    const stop = await screen.findByRole("button", { name: /结束发言/ });
    markAsrReady(FakeSocket.latest, "speech-recover-final");
    fireEvent.click(stop);
    await waitFor(() => expect(FakeSocket.latest?.send).toHaveBeenCalledWith(JSON.stringify({ type: "finish" })));
    await act(async () => {
      FakeSocket.latest?.onmessage?.({ data: JSON.stringify({ type: "asr", text: "新增后半段。", is_final: true }) } as MessageEvent);
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(JSON.parse(String((fetchMock.mock.calls[2][1] as RequestInit).body))).toEqual({
      speech_id: "speech-recover-final",
      content: "服务端前半段。新增后半段。",
    });
  });

  it("ignores late final and 4409 close callbacks from an old ASR socket", async () => {
    const streams = [0, 1].map(() => ({ getTracks: () => [{ stop: vi.fn() }] }) as unknown as MediaStream);
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValueOnce(streams[0]).mockResolvedValueOnce(streams[1]) },
    });
    class FakeRecorder {
      state: RecordingState = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      constructor(public stream: MediaStream) {}
      start() { this.state = "recording"; }
      stop() { this.state = "inactive"; this.onstop?.(); }
    }
    class FakeSocket {
      static OPEN = 1;
      static instances: FakeSocket[] = [];
      readyState = FakeSocket.OPEN;
      binaryType = "";
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      send = vi.fn();
      close = vi.fn();
      constructor() { FakeSocket.instances.push(this); }
    }
    class FakeContext {
      sampleRate = 16_000;
      destination = {};
      createMediaStreamSource() { return { connect: vi.fn() }; }
      createScriptProcessor() { return { connect: vi.fn(), onaudioprocess: null }; }
      close = vi.fn().mockResolvedValue(undefined);
    }
    vi.stubGlobal("MediaRecorder", FakeRecorder);
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal("AudioContext", FakeContext);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-old" }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-new" }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-new" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const firstStage = { key: "aff_case", name: "正方立论", kind: "speech" as const, duration: 120, seat: "aff_1" };
    const secondStage = { key: "aff_summary", name: "正方总结", kind: "speech" as const, duration: 120, seat: "aff_1" };
    const { rerender } = render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言", current_stage: firstStage })} connected mode="debate" />);
    const start = await screen.findByRole("button", { name: /开始发言/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    await screen.findByRole("button", { name: /结束发言/ });
    const oldSocket = FakeSocket.instances[0];
    rerender(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言", current_stage: secondStage })} connected mode="debate" />);
    await screen.findByRole("alert");
    const restart = screen.getByRole("button", { name: /开始发言/ });
    await waitFor(() => expect(restart).toBeEnabled());
    fireEvent.click(restart);
    await screen.findByRole("button", { name: /结束发言/ });
    const newSocket = FakeSocket.instances[1];
    oldSocket.onmessage?.({ data: JSON.stringify({ type: "asr", text: "旧连接迟到字幕。", is_final: true }) } as MessageEvent);
    oldSocket.onclose?.({ code: 4409 } as CloseEvent);
    expect(screen.getByRole("button", { name: /结束发言/ })).toBeEnabled();
    expect(screen.queryByText("旧连接迟到字幕。")).not.toBeInTheDocument();
    markAsrReady(newSocket, "speech-new", "aff_summary");
    fireEvent.click(screen.getByRole("button", { name: /结束发言/ }));
    await waitFor(() => expect(newSocket.send).toHaveBeenCalledWith(JSON.stringify({ type: "finish" })));
    await act(async () => {
      newSocket.onmessage?.({ data: JSON.stringify({ type: "asr", text: "新连接最终字幕。", is_final: true }) } as MessageEvent);
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    expect(JSON.parse(String((fetchMock.mock.calls[3][1] as RequestInit).body))).toEqual({
      speech_id: "speech-new",
      content: "新连接最终字幕。",
    });
  });

  it("buffers PCM until ready, drains the tail block, and sends finish last", async () => {
    const { FakeContext, FakeSocket, fetchMock, process, stop } = await renderAsrHarness();
    const socket = FakeSocket.latest!;
    socket.onopen?.();
    const authentication = JSON.parse(String(socket.send.mock.calls[0][0]));
    expect(authentication).toMatchObject({
      type: "authenticate",
      protocol_version: 2,
      encoding: "pcm_s16le",
      channels: 1,
      sample_rate: 16_000,
      speech_id: "speech-asr-harness",
      stage_key: "aff_case",
    });
    expect(FakeAsrCaptureNode.latest()?.name).toBe("jixia-asr-pcm-capture");

    process(0.1);
    process(0.2);
    expect(socket.send.mock.calls.filter(([payload]) => payload instanceof ArrayBuffer)).toHaveLength(0);
    markAsrReady(socket);
    process(0.3);
    fireEvent.click(stop);
    process(0.4);

    await waitFor(() => expect(socket.send).toHaveBeenCalledWith(JSON.stringify({ type: "finish" })));
    const wirePayloads = socket.send.mock.calls.map(([payload]) => payload);
    const pcmPayloads = wirePayloads.filter((payload): payload is ArrayBuffer => payload instanceof ArrayBuffer);
    expect(pcmPayloads.map((payload) => new Int16Array(payload)[0])).toEqual([3276, 6553, 9830, 13106]);
    expect(wirePayloads.at(-1)).toBe(JSON.stringify({ type: "finish" }));

    await act(async () => {
      socket.onmessage?.({ data: JSON.stringify({ type: "asr", text: "顺序完整。", is_final: true }) } as MessageEvent);
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("flushes buffered PCM before a single finish when stop happens before ready", async () => {
    const { FakeSocket, process, stop, stopTrack } = await renderAsrHarness();
    const socket = FakeSocket.latest!;
    socket.onopen?.();
    process(0.1);
    fireEvent.click(stop);
    fireEvent.click(stop);
    process(0.2);
    expect(socket.send).not.toHaveBeenCalledWith(JSON.stringify({ type: "finish" }));

    markAsrReady(socket);
    await waitFor(() => expect(socket.send).toHaveBeenCalledWith(JSON.stringify({ type: "finish" })));
    const wirePayloads = socket.send.mock.calls.map(([payload]) => payload);
    expect(wirePayloads.filter((payload) => payload instanceof ArrayBuffer)).toHaveLength(2);
    expect(wirePayloads.filter((payload) => payload === JSON.stringify({ type: "finish" }))).toHaveLength(1);
    expect(wirePayloads.at(-1)).toBe(JSON.stringify({ type: "finish" }));

    await act(async () => {
      socket.onmessage?.({ data: JSON.stringify({ type: "asr", text: "", is_final: true }) } as MessageEvent);
    });
    await screen.findByRole("dialog", { name: "提交前核对发言文字" });
    expect(stopTrack).toHaveBeenCalledOnce();
  });

  it("falls back to manual review after two seconds when ASR never becomes ready", async () => {
    const { FakeSocket, process, stop, stopTrack } = await renderAsrHarness();
    const socket = FakeSocket.latest!;
    socket.onopen?.();
    process(0.1);
    vi.useFakeTimers();
    fireEvent.click(stop);
    process(0.2);
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });

    expect(socket.send).not.toHaveBeenCalledWith(JSON.stringify({ type: "finish" }));
    const editor = screen.getByRole("dialog", { name: "提交前核对发言文字" });
    expect(editor).toHaveTextContent("语音识别连接未及时就绪");
    expect(stopTrack).toHaveBeenCalledOnce();
  });

  it("requires manual review when the ASR socket closes before a resumed speech receives its final", async () => {
    const { FakeSocket, fetchMock, process, stop } = await renderAsrHarness(16_000, {
      can_speak: false,
      speak_reason: "恢复当前发言",
      active_speech: {
        id: "speech-asr-harness",
        seat_key: "aff_1",
        speaker_type: "human",
        status: "speaking",
        content: "服务端已保存的前半段。",
      },
    });
    const socket = FakeSocket.latest!;
    markAsrReady(socket);
    process(0.1);
    fireEvent.click(stop);
    process(0.2);
    await waitFor(() => expect(socket.send).toHaveBeenCalledWith(JSON.stringify({ type: "finish" })));
    await act(async () => { socket.onclose?.({ code: 1000 } as CloseEvent); });

    const editor = await screen.findByRole("dialog", { name: "提交前核对发言文字" });
    expect(editor).toHaveTextContent("最终字幕返回前中断");
    expect(screen.getByLabelText("发言文字")).toHaveValue("服务端已保存的前半段。");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not wait for the final timeout when sending ASR finish throws", async () => {
    const { FakeSocket, process, stop } = await renderAsrHarness();
    const socket = FakeSocket.latest!;
    markAsrReady(socket);
    socket.send.mockImplementation((payload) => {
      if (payload === JSON.stringify({ type: "finish" })) throw new Error("socket send failed");
    });
    process(0.1);
    fireEvent.click(stop);
    process(0.2);

    const editor = await screen.findByRole("dialog", { name: "提交前核对发言文字" });
    expect(editor).toHaveTextContent("结束信号发送失败");
  });

  it("does not flush buffered PCM when an old generation receives a late ready", async () => {
    const { FakeSocket, process, rerender } = await renderAsrHarness();
    const oldSocket = FakeSocket.latest!;
    oldSocket.onopen?.();
    process(0.1);
    rerender(<DebateStage room={room({
      can_speak: false,
      speak_reason: "阶段已切换",
      current_stage: { key: "neg_case", name: "反方立论", kind: "speech", duration: 120, seat: "neg_1" },
    })} connected mode="debate" />);
    await screen.findByRole("alert");

    oldSocket.onmessage?.({ data: JSON.stringify({ type: "ready" }) } as MessageEvent);
    expect(oldSocket.send.mock.calls.filter(([payload]) => payload instanceof ArrayBuffer)).toHaveLength(0);
  });

  it("does not restore a zombie recording after a suspended AudioContext resumes for an aborted generation", async () => {
    let resolveResume: (() => void) | undefined;
    const resumePromise = new Promise<void>((resolve) => { resolveResume = resolve; });
    const stopTracks = [vi.fn(), vi.fn()];
    const streams = stopTracks.map((stop) => ({ getTracks: () => [{ stop }] }) as unknown as MediaStream);
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValueOnce(streams[0]).mockResolvedValueOnce(streams[1]) },
    });
    class FakeRecorder {
      static stopCalls = 0;
      static instances: FakeRecorder[] = [];
      state: RecordingState = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      constructor(public stream: MediaStream) { FakeRecorder.instances.push(this); }
      start() { this.state = "recording"; }
      stop() { FakeRecorder.stopCalls += 1; this.state = "inactive"; this.onstop?.(); }
    }
    class FakeSocket {
      static OPEN = 1;
      static instances: FakeSocket[] = [];
      readyState = FakeSocket.OPEN;
      binaryType = "";
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      send = vi.fn();
      close = vi.fn();
      constructor() { FakeSocket.instances.push(this); }
    }
    class SuspendedContext {
      static latest: SuspendedContext | null = null;
      static created: SuspendedContext[] = [];
      static instances = 0;
      sampleRate = 16_000;
      state: AudioContextState;
      destination = {};
      resume: ReturnType<typeof vi.fn>;
      close = vi.fn().mockResolvedValue(undefined);
      constructor() {
        const index = SuspendedContext.instances++;
        this.state = index === 0 ? "suspended" : "running";
        this.resume = vi.fn(() => index === 0 ? resumePromise : Promise.resolve());
        SuspendedContext.latest = this;
        SuspendedContext.created.push(this);
      }
      createMediaStreamSource() { return { connect: vi.fn(), disconnect: vi.fn() }; }
      createScriptProcessor() { return { connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null }; }
    }
    vi.stubGlobal("MediaRecorder", FakeRecorder);
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal("AudioContext", SuspendedContext);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-suspended" }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-current" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const firstStage = { key: "aff_case", name: "正方立论", kind: "speech" as const, duration: 120, seat: "aff_1" };
    const secondStage = { key: "aff_summary", name: "正方总结", kind: "speech" as const, duration: 120, seat: "aff_1" };
    const { rerender } = render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言", current_stage: firstStage })} connected mode="debate" />);
    const start = await screen.findByRole("button", { name: /开始发言/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    await waitFor(() => expect(SuspendedContext.created[0]?.resume).toHaveBeenCalledOnce());

    rerender(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言", current_stage: secondStage })} connected mode="debate" />);
    await screen.findByRole("alert");
    const restart = screen.getByRole("button", { name: /开始发言/ });
    await waitFor(() => expect(restart).toBeEnabled());
    fireEvent.click(restart);
    await screen.findByRole("button", { name: /结束发言/ });
    await act(async () => { resolveResume?.(); await resumePromise; });

    expect(screen.getByRole("button", { name: /结束发言/ })).toBeEnabled();
    expect(FakeAsrCaptureNode.instances).toHaveLength(1);
    expect(stopTracks[0]).toHaveBeenCalledOnce();
    expect(stopTracks[1]).not.toHaveBeenCalled();
    expect(FakeSocket.instances).toHaveLength(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it.each([
    [44_100, 2_205],
    [48_000, 2_400],
  ])("resamples %i Hz input continuously to 16 kHz across processor blocks", async (sampleRate, halfBlock) => {
    const { FakeSocket, process } = await renderAsrHarness(sampleRate);
    const socket = FakeSocket.latest!;
    markAsrReady(socket);
    process(0.1, halfBlock);
    process(0.1, halfBlock);
    const pcmPayloads = socket.send.mock.calls
      .map(([payload]) => payload)
      .filter((payload): payload is ArrayBuffer => payload instanceof ArrayBuffer);
    const outputSamples = pcmPayloads.reduce((total, payload) => total + payload.byteLength / 2, 0);
    expect(outputSamples).toBeGreaterThanOrEqual(1_599);
    expect(outputSamples).toBeLessThanOrEqual(1_600);
  });  it("does not use PCM or archived WAV speech fallback for a text-only room", async () => {
    const audioConstructor = vi.fn(() => { throw new Error("formal AI speech must stay on LiveKit"); });
    const sockets: string[] = [];
    class FakeSocket {
      constructor(url: string) { sockets.push(url); }
    }
    vi.stubGlobal("Audio", audioConstructor);
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ enabled: false }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));
    const generation = "87654321" + "b".repeat(24);
    const playing = {
      id: "speech-livekit-only",
      seat_key: "neg_1",
      speaker: "反方一辩",
      stage_key: "neg_case",
      content: "这段正式发言只能走单一 LiveKit 音轨。",
      audio_url: "/media/123456/legacy-fallback.wav",
      duration_seconds: 12,
      playback_started_at: new Date().toISOString(),
      playback_ends_at: new Date(Date.now() + 12_000).toISOString(),
      stream_generation: generation,
      stream_sample_rate: 24_000,
      status: "playing",
      created_at: new Date().toISOString(),
    };
    const { unmount } = render(<DebateStage room={room({
      match_audio_archive_enabled: false,
      active_speech: {
        id: playing.id,
        seat_key: playing.seat_key,
        speaker_type: "ai",
        status: "playing",
        content: playing.content,
        playback_started_at: playing.playback_started_at,
        stream_generation: generation,
        stream_sample_rate: 24_000,
      },
      speeches: [playing],
    })} connected mode="debate" />);

    await act(async () => undefined);
    expect(audioConstructor).not.toHaveBeenCalled();
    expect(sockets.some((url) => url.includes(`/ws/rooms/123456/audio`))).toBe(false);
    unmount();
  });  it("does not autoplay an uploaded human recording or resurrect an old cue", async () => {
    const instances: { play: ReturnType<typeof vi.fn>; pause: ReturnType<typeof vi.fn> }[] = [];
    class FakeAudio {
      currentTime = 0;
      play = vi.fn().mockResolvedValue(undefined);
      pause = vi.fn();
      constructor(public src: string) { instances.push(this); }
    }
    vi.stubGlobal("Audio", FakeAudio);
    const humanRecording = {
      id: "speech-human", seat_key: "aff_1", speaker: "正方一辩", stage_key: "aff_case", content: "真人发言",
      audio_url: "/media/123456/human.webm", duration_seconds: 8, playback_started_at: null, playback_ends_at: null,
      status: "completed", created_at: new Date().toISOString(),
    };
    const staleCueEvents = [
      { seq: 1, type: "audio.cue.ready", payload: { stage_key: "opening", audio_url: "/media/123456/opening.wav" }, created_at: new Date(Date.now() - 30_000).toISOString() },
      { seq: 2, type: "stage.started", payload: { stage: { key: "opening" } }, created_at: new Date(Date.now() - 20_000).toISOString() },
    ];

    render(<DebateStage room={room({
      current_stage: { key: "opening", name: "开场", kind: "announcement", duration: 30 },
      recent_events: staleCueEvents,
      speeches: [humanRecording],
    })} connected mode="debate" liveEvent={{ type: "speech.audio.ready" }} />);

    await act(async () => undefined);
    expect(instances).toHaveLength(0);
  });

  it("plays a freshly started announcement cue once and stops it on the next stage", async () => {
    const instances: { play: ReturnType<typeof vi.fn>; pause: ReturnType<typeof vi.fn> }[] = [];
    class FakeAudio {
      currentTime = 0;
      play = vi.fn().mockResolvedValue(undefined);
      pause = vi.fn();
      constructor(public src: string) { instances.push(this); }
    }
    vi.stubGlobal("Audio", FakeAudio);
    const cueEvents = [
      { seq: 1, type: "audio.cue.ready", payload: { stage_key: "opening", audio_url: "/media/123456/opening.wav" }, created_at: new Date(Date.now() - 60_000).toISOString() },
      { seq: 2, type: "stage.started", payload: { stage: { key: "opening" } }, created_at: new Date().toISOString() },
    ];
    const { rerender } = render(<DebateStage room={room({
      current_stage: { key: "opening", name: "开场", kind: "announcement", duration: 30 },
      recent_events: cueEvents,
    })} connected mode="debate" />);

    await waitFor(() => expect(instances).toHaveLength(1));
    expect(instances[0].play).toHaveBeenCalledOnce();

    rerender(<DebateStage room={room({
      current_stage: { key: "aff_case", name: "正方立论", kind: "speech", duration: 120, seat: "aff_1" },
      current_stage_index: 1,
      recent_events: cueEvents,
    })} connected mode="debate" />);
    await waitFor(() => expect(instances[0].pause).toHaveBeenCalled());
    expect(instances).toHaveLength(1);
  });

  it("expires a cue after ten seconds even without a room update and refuses a delayed gesture retry", async () => {
    vi.useFakeTimers();
    const startedAt = new Date("2026-07-17T08:00:00.000Z");
    vi.setSystemTime(startedAt);
    const instances: FakeAudio[] = [];
    const listeners = new Map<string, EventListener>();
    class FakeAudio {
      currentTime = 0;
      preload = "";
      pause = vi.fn();
      load = vi.fn();
      removeAttribute = vi.fn();
      addEventListener = vi.fn((type: string, listener: EventListener) => listeners.set(type, listener));
      removeEventListener = vi.fn();
      play = vi.fn()
        .mockRejectedValueOnce(new DOMException("gesture required", "NotAllowedError"))
        .mockResolvedValue(undefined);
      constructor(public src: string) { instances.push(this); }
    }
    vi.stubGlobal("Audio", FakeAudio);
    const events = [
      { seq: 1, type: "audio.cue.ready", payload: { stage_key: "opening", audio_url: "/media/123456/expiring-cue.wav" }, created_at: startedAt.toISOString() },
      { seq: 2, type: "stage.started", payload: { stage: { key: "opening" } }, created_at: startedAt.toISOString() },
    ];
    render(<DebateStage room={room({
      current_stage: { key: "opening", name: "开场", kind: "announcement", duration: 30 },
      recent_events: events,
    })} connected mode="debate" />);
    await act(async () => undefined);
    expect(instances[0].play).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "播放比赛声音" })).toBeEnabled();

    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    const sound = screen.getByRole("button", { name: "关闭比赛声音" });
    fireEvent.click(sound);
    fireEvent.click(screen.getByRole("button", { name: "开启比赛声音" }));
    expect(instances[0].play).toHaveBeenCalledOnce();
    act(() => { listeners.get("error")?.(new Event("error")); });
    expect(screen.queryByText(/比赛音频加载或解码失败/)).not.toBeInTheDocument();
  });

  it("creates a new cue generation when the same stage key starts again", async () => {
    const instances: FakeAudio[] = [];
    class FakeAudio {
      currentTime = 0;
      preload = "";
      pause = vi.fn();
      load = vi.fn();
      removeAttribute = vi.fn();
      addEventListener = vi.fn();
      removeEventListener = vi.fn();
      play = vi.fn().mockResolvedValue(undefined);
      constructor(public src: string) { instances.push(this); }
    }
    vi.stubGlobal("Audio", FakeAudio);
    const firstTime = new Date().toISOString();
    const firstEvents = [
      { seq: 1, type: "audio.cue.ready", payload: { stage_key: "opening", audio_url: "/media/123456/opening.wav" }, created_at: firstTime },
      { seq: 2, type: "stage.started", payload: { stage: { key: "opening" } }, created_at: firstTime },
    ];
    const currentStage = { key: "opening", name: "开场", kind: "announcement" as const, duration: 30 };
    const { rerender } = render(<DebateStage room={room({ current_stage: currentStage, recent_events: firstEvents })} connected mode="debate" />);
    await waitFor(() => expect(instances).toHaveLength(1));

    const secondTime = new Date().toISOString();
    rerender(<DebateStage room={room({
      seq: 4,
      current_stage: currentStage,
      recent_events: [
        ...firstEvents,
        { seq: 3, type: "audio.cue.ready", payload: { stage_key: "opening", audio_url: "/media/123456/opening.wav" }, created_at: secondTime },
        { seq: 4, type: "stage.started", payload: { stage: { key: "opening" } }, created_at: secondTime },
      ],
    })} connected mode="debate" />);
    await waitFor(() => expect(instances).toHaveLength(2));
    expect(instances[0].pause).toHaveBeenCalled();
    expect(instances[1].play).toHaveBeenCalledOnce();
  });
});
