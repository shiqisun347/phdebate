import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
} | null) {
  socket?.onopen?.();
  socket?.onmessage?.({ data: JSON.stringify({ type: "ready" }) } as MessageEvent);
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
  markAsrReady(FakeSocket.latest);
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
    state: RecordingState = "inactive";
    mimeType = "audio/webm";
    ondataavailable: ((event: BlobEvent) => void) | null = null;
    onstop: (() => void) | null = null;
    constructor(public stream: MediaStream) {}
    start() { this.state = "recording"; }
    stop() { FakeRecorder.stopCalls += 1; this.state = "inactive"; this.onstop?.(); }
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
    send = vi.fn();
    close = vi.fn();
    constructor() { FakeSocket.latest = this; }
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

  it("surfaces a new recording policy before the participant starts another speech", async () => {
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
    expect(await screen.findByRole("heading", { name: "比赛录音政策 v2" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /当前 v2/ })).not.toBeChecked();
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
    expect(screen.getByText(/本轮剩余 00:44/)).toBeInTheDocument();
    expect(screen.getByText("AI 准备中 · 计时暂停")).toBeInTheDocument();
    expect(screen.getByText("AI 正在组织论点并合成语音…")).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(5_000); });
    expect(screen.getByRole("timer")).toHaveAccessibleName("AI 正在准备发言，计时暂停在 03:58");
    expect(screen.getByText(/本轮剩余 00:44/)).toBeInTheDocument();

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
    expect(screen.getByText(/本轮剩余 00:39/)).toBeInTheDocument();
  });

  it("shows an AI-substituted participant as read-only until an administrator restores the seat", async () => {
    const substituted = room({
      can_speak: false,
      speak_reason: "你的席位已由 AI 接替，等待管理员恢复真人控制",
      seats: [
        {
          seat_key: "aff_1",
          side: "aff",
          position: 1,
          label: "正方一辩",
          occupant_type: "ai_substitute",
          display_name: "AI 接替·张三",
          is_ready: true,
          connected: true,
          is_me: true,
        },
        room().seats[1],
      ],
    });
    render(<DebateStage room={substituted} connected mode="debate" />);
    expect(screen.getByText("正方一辩 · AI 接替")).toBeInTheDocument();
    const waiting = await screen.findByRole("button", { name: /等待轮次/ });
    expect(waiting).toBeDisabled();
    expect(waiting).toHaveTextContent("等待管理员恢复真人控制");
    fireEvent.click(screen.getByRole("button", { name: "比赛操作" }));
    expect(screen.getByText(/设备控制：只读席位/)).toBeInTheDocument();
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
    expect(String(fetchMock.mock.calls[1][0])).toContain("/api/rooms/123456/control/retry");
    expect(fetchMock.mock.calls[1][1]).toEqual(expect.objectContaining({ method: "POST" }));
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
    expect(takenOver).toHaveTextContent("该席位已在其他设备接管");
    fireEvent.click(screen.getByRole("button", { name: "比赛操作" }));
    fireEvent.click(screen.getByRole("button", { name: "确认接管到当前设备" }));
    await waitFor(() => {
      const calls = vi.mocked(fetch).mock.calls;
      expect(calls.some(([, init]) => init?.body === JSON.stringify({ force: true }))).toBe(true);
    });
  });

  it("opens settings with keyboard focus and restores focus when Escape closes it", async () => {
    render(<DebateStage room={room()} connected mode="debate" />);
    const settings = screen.getByRole("button", { name: "比赛操作" });
    fireEvent.click(settings);
    await screen.findByRole("button", { name: "关闭比赛操作" });
    await waitFor(() => expect(screen.getByRole("button", { name: "暂停比赛" })).toHaveFocus());
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "比赛操作" })).not.toBeInTheDocument();
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

    fireEvent.click(screen.getByRole("button", { name: "比赛操作" }));
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
    fireEvent.click(screen.getByRole("button", { name: "比赛操作" }));
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
    fireEvent.click(screen.getByRole("button", { name: "比赛操作" }));
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
    expect(screen.queryByRole("button", { name: "退出比赛页面" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "进入比赛控制" })).toHaveAttribute("href", "/rooms/123456/control");
  });

  it("keeps match-wide controls owner-only while every participant can leave the page", () => {
    render(<DebateStage room={room({ can_control: false })} connected mode="debate" onLeave={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "比赛操作" }));
    expect(screen.queryByRole("button", { name: "暂停比赛" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "提前结束比赛" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "退出比赛页面" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "仅退出比赛页面" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "放弃本场并由 AI 接替" })).toBeEnabled();
  });

  it("lets a non-owner abandon a started seat only after explicit confirmation", async () => {
    const substituted = room({
      seq: 4,
      can_control: false,
      can_speak: false,
      speak_reason: "你的席位已由 AI 接替",
      seats: room().seats.map((seat) => seat.seat_key === "aff_1"
        ? { ...seat, occupant_type: "ai_substitute", display_name: "AI 接替·张三", is_me: true }
        : seat),
    });
    const onRoomChanged = vi.fn();
    const fetchMock = vi.fn((url: RequestInfo | URL, _init?: RequestInit) => {
      if (String(url).includes("control-lease")) {
        return Promise.resolve(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      if (String(url).includes("abandon-seat")) {
        return Promise.resolve(new Response(JSON.stringify({ room: substituted }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      return Promise.reject(new Error(`unexpected request: ${String(url)}`));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<DebateStage room={room({ can_control: false })} connected mode="debate" onRoomChanged={onRoomChanged} onLeave={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "比赛操作" }));
    fireEvent.click(screen.getByRole("button", { name: "放弃本场并由 AI 接替" }));
    expect(screen.getByRole("alertdialog", { name: "确认放弃本场比赛？" })).toHaveTextContent("旧比赛转为只读观战");
    fireEvent.click(screen.getByRole("button", { name: "确认放弃并由 AI 接替" }));
    await waitFor(() => expect(onRoomChanged).toHaveBeenCalledWith(substituted));
    const abandonCall = fetchMock.mock.calls.find(([url]) => String(url).includes("abandon-seat"));
    expect(abandonCall?.[1]).toEqual(expect.objectContaining({ method: "POST" }));
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

    fireEvent.click(screen.getByRole("button", { name: "比赛操作" }));
    fireEvent.click(screen.getByRole("button", { name: "提前结束比赛" }));
    const terminateDialog = screen.getByRole("alertdialog", { name: "确认提前结束比赛？" });
    expect(terminateDialog).toHaveTextContent("不可恢复");
    expect(document.querySelectorAll('[aria-modal="true"]')).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("alertdialog", { name: "确认提前结束比赛？" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "比赛操作" })).toBeInTheDocument();

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

  it("shows the free-debate side turn countdown before recording starts", async () => {
    render(<DebateStage room={room({
      can_speak: true,
      speak_reason: "轮到你发言",
      current_stage: { key: "free", name: "自由辩论", kind: "free", duration: 300, side: "aff", turn_duration: 40 },
      turn_remaining_seconds: 25,
    })} connected mode="debate" />);
    const button = await screen.findByRole("button", { name: /开始发言/ });
    expect(button).toHaveTextContent("本轮剩余 00:25");
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
    const recorderStops: ReturnType<typeof vi.fn>[] = [];
    class FakeRecorder {
      state: RecordingState = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      private stopSpy = vi.fn();
      constructor(public stream: MediaStream) { recorderStops.push(this.stopSpy); }
      start() { this.state = "recording"; }
      stop() { this.state = "inactive"; this.stopSpy(); this.onstop?.(); }
    }
    vi.stubGlobal("MediaRecorder", FakeRecorder);
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
    expect(recorderStops[0]).toHaveBeenCalledOnce();
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

  it("stops an already opened recorder and aborts speech-start when the stage advances", async () => {
    const stopTrack = vi.fn();
    const stream = { getTracks: () => [{ stop: stopTrack }] } as unknown as MediaStream;
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    });
    const recorderStop = vi.fn();
    class FakeRecorder {
      state: RecordingState = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      constructor(public stream: MediaStream) {}
      start() { this.state = "recording"; }
      stop() { this.state = "inactive"; recorderStop(); this.onstop?.(); }
    }
    vi.stubGlobal("MediaRecorder", FakeRecorder);
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
    expect(recorderStop).toHaveBeenCalledOnce();
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
    FakeSocket.latest?.onopen?.();
    FakeSocket.latest?.onmessage?.({ data: JSON.stringify({ type: "ready" }) } as MessageEvent);
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
    markAsrReady(FakeSocket.latest);
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
    markAsrReady(FakeSocket.latest);
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

    const expiredDialog = screen.getByRole("dialog", { name: "本次发言未能提交" });
    expect(expiredDialog).toHaveTextContent("比赛已终止，本次未提交录音无法再保存");
    expect(screen.getByLabelText("发言文字")).toHaveValue("比赛结束前尚未提交的发言文字。");
    expect(screen.getByLabelText("发言文字")).toHaveAttribute("readonly");
    expect(screen.getByRole("button", { name: /本次发言无法提交/ })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "确认文字并提交" })).not.toBeInTheDocument();
    const discard = screen.getByRole("button", { name: "放弃未提交内容并查看结果" });
    expect(discard).toHaveFocus();
    expect((await axe.run(container, { rules: { "color-contrast": { enabled: false } } })).violations).toEqual([]);
    fireEvent.click(discard);

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "本次发言未能提交" })).not.toBeInTheDocument());
    expect(onPendingFinishChange).toHaveBeenLastCalledWith(false);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(stopTrack).toHaveBeenCalled();
  });

  it.each(["completed", "review_required"])("allows a timed-out speech to be late-finalized after the room becomes %s", async (status) => {
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

    const editor = screen.getByRole("dialog", { name: "提交前核对发言文字" });
    expect(editor).toHaveTextContent("服务端仍允许补交这次超时发言");
    expect(screen.getByRole("button", { name: /提交保留的发言/ })).toHaveTextContent("比赛流程已结束，本次超时发言仍可补交");
    expect(screen.queryByRole("button", { name: /放弃未提交内容/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认文字并提交" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(String(fetchMock.mock.calls[2][0])).toContain("/speech/finish");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "提交前核对发言文字" })).not.toBeInTheDocument());
    expect(onPendingFinishChange).toHaveBeenLastCalledWith(false);
  });

  it("keeps an in-flight late finalization alive when the websocket reports room completion", async () => {
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
    expect(screen.getByRole("button", { name: /正在整理发言/ })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /放弃未提交内容/ })).not.toBeInTheDocument();

    await act(async () => {
      resolveFinish?.(new Response(JSON.stringify({ speech_id: "speech-unsubmitted", timed_out: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    });
    await waitFor(() => expect(onPendingFinishChange).toHaveBeenLastCalledWith(false));
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
    expect(editor).toHaveTextContent("录音仍保留在当前页面");
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

    const dialog = screen.getByRole("dialog", { name: "本次发言未能提交" });
    expect(dialog).toHaveTextContent("本次发言已被比赛控制中断");
    expect(screen.queryByRole("button", { name: "确认文字并提交" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "放弃未提交内容并查看结果" }));
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
    markAsrReady(FakeSocket.latest);
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
  });

  it("keeps a real recording when VAD is unavailable and uploads it after manual transcription", async () => {
    const stream = { getTracks: () => [{ stop: vi.fn() }] } as unknown as MediaStream;
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia: vi.fn().mockResolvedValue(stream) } });
    class FakeRecorder {
      state: RecordingState = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      constructor(public stream: MediaStream) {}
      start() { this.state = "recording"; }
      stop() {
        this.state = "inactive";
        this.ondataavailable?.({ data: new Blob([new Uint8Array([0x1a, 0x45, 0xdf, 0xa3, 1, 2, 3])]) } as BlobEvent);
        this.onstop?.();
      }
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
    class FailingContext { constructor() { throw new Error("audio context unavailable"); } }
    vi.stubGlobal("MediaRecorder", FakeRecorder);
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal("AudioContext", FailingContext);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-vad-unknown" }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-vad-unknown" }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ audio_url: "/media/123456/speech-vad-unknown.webm" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言" })} connected mode="debate" />);
    const start = await screen.findByRole("button", { name: /开始发言/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    fireEvent.click(await screen.findByRole("button", { name: /结束发言/ }));
    const editor = await screen.findByRole("dialog", { name: "提交前核对发言文字" });
    expect(editor).toHaveTextContent("录音仍会正常保存");
    fireEvent.change(screen.getByLabelText("发言文字"), { target: { value: "VAD 不可用时的人工补文。" } });
    fireEvent.click(screen.getByRole("button", { name: "确认文字并提交" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    expect(String(fetchMock.mock.calls[3][0])).toContain("/speech/speech-vad-unknown/audio");
  });

  it("preserves the recovered server transcript for manual review when ASR rejects resumed audio", async () => {
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
    markAsrReady(FakeSocket.latest);
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
    markAsrReady(FakeSocket.latest);
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
    markAsrReady(newSocket);
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

  it("ignores asynchronous dataavailable chunks from an aborted capture generation", async () => {
    const streams = [0, 1].map(() => ({ getTracks: () => [{ stop: vi.fn() }] }) as unknown as MediaStream);
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValueOnce(streams[0]).mockResolvedValueOnce(streams[1]) },
    });
    let recorderIndex = 0;
    class FakeRecorder {
      state: RecordingState = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      readonly index = recorderIndex++;
      constructor(public stream: MediaStream) {}
      start() { this.state = "recording"; }
      stop() {
        this.state = "inactive";
        if (this.index === 0) {
          window.setTimeout(() => this.ondataavailable?.({ data: new Blob([new Uint8Array([7, 7, 7])]) } as BlobEvent), 0);
        } else {
          this.ondataavailable?.({ data: new Blob([new Uint8Array([0x1a, 0x45, 0xdf, 0xa3, 9])]) } as BlobEvent);
        }
        this.onstop?.();
      }
    }
    class FailingSocket { constructor() { throw new Error("ASR unavailable"); } }
    vi.stubGlobal("MediaRecorder", FakeRecorder);
    vi.stubGlobal("WebSocket", FailingSocket);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, lease_fingerprint: "own-device", seq: 3 }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "其他辩手正在发言" }), { status: 409, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-new-generation" }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-new-generation" }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ audio_url: "/media/123456/speech-new-generation.webm" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言" })} connected mode="debate" />);
    const start = await screen.findByRole("button", { name: /开始发言/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    expect(await screen.findByRole("alert")).toHaveTextContent("其他辩手正在发言");
    fireEvent.click(screen.getByRole("button", { name: /开始发言/ }));
    await screen.findByRole("button", { name: /结束发言/ });
    await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 0)); });
    fireEvent.click(screen.getByRole("button", { name: /结束发言/ }));
    await screen.findByRole("dialog", { name: "提交前核对发言文字" });
    fireEvent.change(screen.getByLabelText("发言文字"), { target: { value: "新一轮录音对应的人工补文。" } });
    fireEvent.click(screen.getByRole("button", { name: "确认文字并提交" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(5));
    const form = (fetchMock.mock.calls[4][1] as RequestInit).body as FormData;
    const audio = form.get("audio") as Blob;
    const bytes = await new Promise<Uint8Array>((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(reader.error);
      reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer));
      reader.readAsArrayBuffer(audio);
    });
    expect([...bytes]).toEqual([0x1a, 0x45, 0xdf, 0xa3, 9]);
  });

  it("locks finalized text and retries only the authoritative audio upload after a failure", async () => {
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
      stop() {
        this.state = "inactive";
        this.ondataavailable?.({ data: new Blob([new Uint8Array([0x1a, 0x45, 0xdf, 0xa3, 1, 2, 3])]) } as BlobEvent);
        this.onstop?.();
      }
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
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-started" }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ speech_id: "speech-authoritative" }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "录音存储暂时不可用" }), { status: 503, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ audio_url: "/media/123456/speech-authoritative.webm" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    render(<DebateStage room={room({ can_speak: true, speak_reason: "轮到你发言" })} connected mode="debate" />);
    const start = await screen.findByRole("button", { name: /开始发言/ });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    await screen.findByRole("button", { name: /结束发言/ });
    markAsrReady(FakeSocket.latest);
    const voicedInput = new Float32Array(4096).fill(0.1);
    FakeAsrCaptureNode.latest()?.emitFrames(voicedInput);
    fireEvent.click(screen.getByRole("button", { name: /结束发言/ }));
    await waitFor(() => expect(FakeSocket.latest?.send).toHaveBeenCalledWith(JSON.stringify({ type: "finish" })));
    await act(async () => {
      FakeSocket.latest?.onmessage?.({ data: JSON.stringify({ type: "asr_rejected", reason: "low_confidence" }) } as MessageEvent);
    });

    const editor = await screen.findByRole("dialog", { name: "提交前核对发言文字" });
    expect(editor).toHaveTextContent("可信度不足");
    fireEvent.change(screen.getByLabelText("发言文字"), { target: { value: "人工核对后的有效发言。" } });
    const confirm = screen.getByRole("button", { name: "确认文字并提交" });
    fireEvent.click(confirm);
    fireEvent.click(confirm);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    expect(JSON.parse(String((fetchMock.mock.calls[2][1] as RequestInit).body))).toEqual({ speech_id: "speech-started", content: "人工核对后的有效发言。" });
    expect(String(fetchMock.mock.calls[3][0])).toContain("/speech/speech-authoritative/audio");
    expect((fetchMock.mock.calls[3][1] as RequestInit).body).toBeInstanceOf(FormData);
    const finalizedEditor = await screen.findByRole("dialog", { name: "发言文字已提交" });
    expect(finalizedEditor).toHaveTextContent("录音仍保留在当前页面");
    expect(screen.getByLabelText("发言文字")).toHaveAttribute("readonly");
    fireEvent.change(screen.getByLabelText("发言文字"), { target: { value: "不应覆盖已提交文字。" } });
    expect(screen.getByLabelText("发言文字")).toHaveValue("人工核对后的有效发言。");
    fireEvent.click(screen.getByRole("button", { name: "重试上传录音" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(5));
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("/speech/finish"))).toHaveLength(1);
    expect(String(fetchMock.mock.calls[4][0])).toContain("/speech/speech-authoritative/audio");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "发言文字已提交" })).not.toBeInTheDocument());
  });

  it("buffers PCM until ready, drains the tail block, and sends finish last", async () => {
    const { FakeContext, FakeSocket, fetchMock, process, stop } = await renderAsrHarness();
    const socket = FakeSocket.latest!;
    socket.onopen?.();
    const authentication = JSON.parse(String(socket.send.mock.calls[0][0]));
    expect(authentication).toMatchObject({
      type: "authenticate",
      protocol_version: 1,
      encoding: "pcm_s16le",
      channels: 1,
      sample_rate: 16_000,
    });
    expect(FakeAsrCaptureNode.latest()?.name).toBe("jixia-asr-pcm-capture");

    process(0.1);
    process(0.2);
    expect(socket.send.mock.calls.filter(([payload]) => payload instanceof ArrayBuffer)).toHaveLength(0);
    socket.onmessage?.({ data: JSON.stringify({ type: "ready" }) } as MessageEvent);
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
    const { FakeRecorder, FakeSocket, process, stop, stopTrack } = await renderAsrHarness();
    const socket = FakeSocket.latest!;
    socket.onopen?.();
    process(0.1);
    fireEvent.click(stop);
    fireEvent.click(stop);
    process(0.2);
    expect(socket.send).not.toHaveBeenCalledWith(JSON.stringify({ type: "finish" }));

    socket.onmessage?.({ data: JSON.stringify({ type: "ready" }) } as MessageEvent);
    await waitFor(() => expect(socket.send).toHaveBeenCalledWith(JSON.stringify({ type: "finish" })));
    const wirePayloads = socket.send.mock.calls.map(([payload]) => payload);
    expect(wirePayloads.filter((payload) => payload instanceof ArrayBuffer)).toHaveLength(2);
    expect(wirePayloads.filter((payload) => payload === JSON.stringify({ type: "finish" }))).toHaveLength(1);
    expect(wirePayloads.at(-1)).toBe(JSON.stringify({ type: "finish" }));

    await act(async () => {
      socket.onmessage?.({ data: JSON.stringify({ type: "asr", text: "", is_final: true }) } as MessageEvent);
    });
    await screen.findByRole("dialog", { name: "提交前核对发言文字" });
    expect(FakeRecorder.stopCalls).toBe(1);
    expect(stopTrack).toHaveBeenCalledOnce();
  });

  it("falls back to manual review after two seconds when ASR never becomes ready", async () => {
    const { FakeRecorder, FakeSocket, process, stop } = await renderAsrHarness();
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
    expect(FakeRecorder.stopCalls).toBe(1);
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
    expect(FakeRecorder.stopCalls).toBe(1);
    expect(FakeRecorder.instances[1].state).toBe("recording");
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
  });

  it("uses the generation-scoped audio WebSocket and flushes its worklet on stage change", async () => {
    const ports: Array<{
      onmessage: ((event: MessageEvent) => void) | null;
      messages: Record<string, unknown>[];
      postMessage: (message: Record<string, unknown>) => void;
    }> = [];
    class FakeWorkletNode {
      port = {
        onmessage: null as ((event: MessageEvent) => void) | null,
        messages: [] as Record<string, unknown>[],
        postMessage(message: Record<string, unknown>) { this.messages.push(message); },
      };
      connect = vi.fn();
      disconnect = vi.fn();
      constructor() { ports.push(this.port); }
    }
    class FakeContext {
      sampleRate = 48_000;
      state: AudioContextState = "running";
      destination = {};
      audioWorklet = { addModule: vi.fn().mockResolvedValue(undefined) };
      resume = vi.fn().mockResolvedValue(undefined);
      close = vi.fn().mockResolvedValue(undefined);
    }
    class FakeSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSING = 2;
      static CLOSED = 3;
      static instances: FakeSocket[] = [];
      readyState = FakeSocket.CONNECTING;
      binaryType = "";
      sent: string[] = [];
      close = vi.fn((code = 1000) => { this.readyState = FakeSocket.CLOSED; this.onclose?.({ code } as CloseEvent); });
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      constructor(public url: string) { FakeSocket.instances.push(this); }
      send(value: string) { this.sent.push(value); }
      open() { this.readyState = FakeSocket.OPEN; this.onopen?.(); }
      message(data: string | ArrayBuffer) { this.onmessage?.({ data } as MessageEvent); }
    }
    vi.stubGlobal("AudioContext", FakeContext);
    vi.stubGlobal("AudioWorkletNode", FakeWorkletNode);
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal("Audio", vi.fn(() => { throw new Error("streaming speech must not create HTMLAudio"); }));
    const generation = "12345678" + "a".repeat(24);
    const playbackStartedAt = new Date().toISOString();
    const active = {
      id: "speech-stream", seat_key: "neg_1", speaker_type: "ai", status: "synthesizing", content: "实时音频",
      playback_started_at: playbackStartedAt, stream_generation: generation, stream_sample_rate: 24_000,
    };
    const { rerender } = render(<DebateStage room={room({ active_speech: active })} connected mode="debate" />);
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(1));
    const socket = FakeSocket.instances[0];
    socket.open();
    expect(JSON.parse(socket.sent[0])).toMatchObject({
      type: "subscribe",
      speech_id: "speech-stream",
      generation,
    });
    socket.message(JSON.stringify({ type: "audio.start", frame_samples: 960, start_seq: 0, start_pts_samples: 0 }));
    const frame = new ArrayBuffer(24 + 1_920);
    const view = new DataView(frame);
    view.setUint8(0, 0x4a); view.setUint8(1, 0x58); view.setUint8(2, 1);
    view.setUint32(4, 0x12345678, false);
    view.setUint32(8, 0, false);
    view.setUint32(12, 0, false); view.setUint32(16, 0, false);
    view.setUint32(20, 960, false);
    socket.message(frame);
    await waitFor(() => expect(ports[0].messages.some((message) => message.type === "enqueue")).toBe(true));

    rerender(<DebateStage room={room({ seq: 4, active_speech: null })} connected mode="debate" />);
    await waitFor(() => expect(socket.close).toHaveBeenCalledWith(1000, "stream reset"));
    expect(ports[0].messages.some((message) => message.type === "flush")).toBe(true);
  });

  it("plays only the active authoritative AI speech and stops it when the server completes playback", async () => {
    const instances: { currentTime: number; play: ReturnType<typeof vi.fn>; pause: ReturnType<typeof vi.fn> }[] = [];
    class FakeAudio {
      currentTime = 0;
      play = vi.fn().mockResolvedValue(undefined);
      pause = vi.fn();
      constructor(public src: string) { instances.push(this); }
    }
    vi.stubGlobal("Audio", FakeAudio);
    const completedSpeech = {
      id: "speech-1", seat_key: "neg_1", speaker: "反方一辩", stage_key: "neg_case", content: "历史发言",
      audio_url: "/media/123456/history.wav", duration_seconds: 4, playback_started_at: null, playback_ends_at: null,
      status: "completed", created_at: new Date().toISOString(),
    };
    const { rerender } = render(<DebateStage room={room({ speeches: [completedSpeech] })} connected mode="debate" liveEvent={{ type: "speech.audio.ready" }} />);
    expect(instances).toHaveLength(0);

    const startedAt = new Date(Date.now() - 1000).toISOString();
    const playingSpeech = {
      ...completedSpeech,
      status: "playing",
      audio_url: "/media/123456/current.wav",
      playback_started_at: startedAt,
      playback_ends_at: new Date(Date.now() + 3000).toISOString(),
    };
    rerender(<DebateStage room={room({
      active_speech: { id: playingSpeech.id, seat_key: playingSpeech.seat_key, speaker_type: "ai", status: "playing", content: playingSpeech.content },
      speeches: [completedSpeech, playingSpeech],
    })} connected mode="debate" liveEvent={{ type: "speech.audio.ready" }} />);
    await waitFor(() => expect(instances).toHaveLength(1));
    expect(instances[0].play).toHaveBeenCalledOnce();
    expect(instances[0].currentTime).toBeGreaterThan(0.5);

    rerender(<DebateStage room={room({
      active_speech: null,
      speeches: [{ ...playingSpeech, status: "completed" }],
    })} connected mode="debate" liveEvent={{ type: "speech.completed" }} />);
    await waitFor(() => expect(instances[0].pause).toHaveBeenCalled());
    expect(instances).toHaveLength(1);
  });

  it("does not autoplay an uploaded human recording or resurrect an old cue", async () => {
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

  it("does not call play again for unrelated snapshots with the same playback identity", async () => {
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
    const playing = {
      id: "speech-stable", seat_key: "neg_1", speaker: "反方一辩", stage_key: "neg_case", content: "稳定播放",
      audio_url: "/media/123456/stable.wav", duration_seconds: 20, playback_started_at: new Date().toISOString(),
      playback_ends_at: new Date(Date.now() + 20_000).toISOString(), status: "playing", created_at: new Date().toISOString(),
    };
    const active = { id: playing.id, seat_key: playing.seat_key, speaker_type: "ai", status: "playing", content: playing.content };
    const { rerender } = render(<DebateStage room={room({ active_speech: active, speeches: [playing] })} connected mode="debate" />);
    await waitFor(() => expect(instances[0].play).toHaveBeenCalledOnce());

    rerender(<DebateStage room={room({
      seq: 4,
      active_speech: active,
      speeches: [{ ...playing }],
      recent_events: [{ seq: 4, type: "presence.changed", payload: {}, created_at: new Date().toISOString() }],
    })} connected mode="debate" />);
    await act(async () => undefined);
    expect(instances).toHaveLength(1);
    expect(instances[0].play).toHaveBeenCalledOnce();
  });

  it("ignores an older same-session attempt after a newer retry succeeds", async () => {
    let rejectFirst: ((reason?: unknown) => void) | undefined;
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
        .mockImplementationOnce(() => new Promise<void>((_resolve, reject) => { rejectFirst = reject; }))
        .mockResolvedValue(undefined);
      constructor(public src: string) {}
    }
    vi.stubGlobal("Audio", FakeAudio);
    const playing = {
      id: "speech-attempt-order", seat_key: "neg_1", speaker: "反方一辩", stage_key: "neg_case", content: "尝试顺序",
      audio_url: "/media/123456/order.wav", duration_seconds: 20, playback_started_at: new Date().toISOString(),
      playback_ends_at: new Date(Date.now() + 20_000).toISOString(), status: "playing", created_at: new Date().toISOString(),
    };
    render(<DebateStage room={room({
      active_speech: { id: playing.id, seat_key: playing.seat_key, speaker_type: "ai", status: "playing", content: playing.content },
      speeches: [playing],
    })} connected mode="debate" />);
    await waitFor(() => expect(screen.getByRole("button", { name: "关闭比赛声音" })).toHaveAttribute("aria-busy", "true"));

    act(() => { listeners.get("error")?.(new Event("error")); });
    const retry = await screen.findByRole("button", { name: "重试比赛声音" });
    fireEvent.click(retry);
    await waitFor(() => expect(screen.getByRole("button", { name: "关闭比赛声音" })).toHaveAttribute("aria-pressed", "true"));
    await act(async () => { rejectFirst?.(new DOMException("late rejection", "NotAllowedError")); });
    expect(screen.getByRole("button", { name: "关闭比赛声音" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByText(/比赛音频播放失败/)).not.toBeInTheDocument();
  });

  it("unmutes watch mode with exactly one synchronous play request", async () => {
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
    const playing = {
      id: "speech-watch", seat_key: "neg_1", speaker: "反方一辩", stage_key: "neg_case", content: "观战播放",
      audio_url: "/media/123456/watch.wav", duration_seconds: 20, playback_started_at: new Date().toISOString(),
      playback_ends_at: new Date(Date.now() + 20_000).toISOString(), status: "playing", created_at: new Date().toISOString(),
    };
    render(<DebateStage room={room({
      active_speech: { id: playing.id, seat_key: playing.seat_key, speaker_type: "ai", status: "playing", content: playing.content },
      speeches: [playing],
    })} connected mode="watch" />);
    expect(instances).toHaveLength(1);
    expect(instances[0].play).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "开启比赛声音" }));
    expect(instances[0].play).toHaveBeenCalledOnce();
    await act(async () => undefined);
    expect(instances[0].play).toHaveBeenCalledOnce();
  });

  it("catches up five seconds before resuming a muted authoritative speech", () => {
    vi.useFakeTimers();
    const startedAt = new Date("2026-07-17T08:00:00.000Z");
    vi.setSystemTime(startedAt);
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
    const playing = {
      id: "speech-catch-up", seat_key: "neg_1", speaker: "反方一辩", stage_key: "neg_case", content: "追赶播放",
      audio_url: "/media/123456/catch-up.wav", duration_seconds: 20, playback_started_at: startedAt.toISOString(),
      playback_ends_at: new Date(startedAt.getTime() + 20_000).toISOString(), status: "playing", created_at: startedAt.toISOString(),
    };
    render(<DebateStage room={room({
      active_speech: { id: playing.id, seat_key: playing.seat_key, speaker_type: "ai", status: "playing", content: playing.content },
      speeches: [playing],
    })} connected mode="watch" />);
    expect(instances[0].currentTime).toBe(0);
    vi.setSystemTime(new Date(startedAt.getTime() + 5_000));
    fireEvent.click(screen.getByRole("button", { name: "开启比赛声音" }));
    expect(instances[0].currentTime).toBe(5);
    expect(instances[0].play).toHaveBeenCalledOnce();
  });

  it.each([
    ["equal to", 20_000],
    ["greater than", 21_000],
  ])("does not play when authoritative elapsed time is %s the speech duration", (_label, elapsedMs) => {
    vi.useFakeTimers();
    const startedAt = new Date("2026-07-17T08:00:00.000Z");
    vi.setSystemTime(new Date(startedAt.getTime() + elapsedMs));
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
    const playing = {
      id: `speech-expired-${elapsedMs}`, seat_key: "neg_1", speaker: "反方一辩", stage_key: "neg_case", content: "已经到达权威终点",
      audio_url: `/media/123456/expired-${elapsedMs}.wav`, duration_seconds: 20, playback_started_at: startedAt.toISOString(),
      playback_ends_at: new Date(startedAt.getTime() + 20_000).toISOString(), status: "playing", created_at: startedAt.toISOString(),
    };
    render(<DebateStage room={room({
      active_speech: { id: playing.id, seat_key: playing.seat_key, speaker_type: "ai", status: "playing", content: playing.content },
      speeches: [playing],
    })} connected mode="debate" />);
    expect(instances).toHaveLength(1);
    expect(instances[0].play).not.toHaveBeenCalled();
    expect(instances[0].currentTime).toBe(0);
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

  it("blocks a double click while the direct playback attempt is pending", () => {
    const instances: FakeAudio[] = [];
    class FakeAudio {
      currentTime = 0;
      preload = "";
      pause = vi.fn();
      load = vi.fn();
      removeAttribute = vi.fn();
      addEventListener = vi.fn();
      removeEventListener = vi.fn();
      play = vi.fn(() => new Promise<void>(() => undefined));
      constructor(public src: string) { instances.push(this); }
    }
    vi.stubGlobal("Audio", FakeAudio);
    const playing = {
      id: "speech-pending", seat_key: "neg_1", speaker: "反方一辩", stage_key: "neg_case", content: "等待播放",
      audio_url: "/media/123456/pending.wav", duration_seconds: 20, playback_started_at: new Date().toISOString(),
      playback_ends_at: new Date(Date.now() + 20_000).toISOString(), status: "playing", created_at: new Date().toISOString(),
    };
    render(<DebateStage room={room({
      active_speech: { id: playing.id, seat_key: playing.seat_key, speaker_type: "ai", status: "playing", content: playing.content },
      speeches: [playing],
    })} connected mode="watch" />);
    const sound = screen.getByRole("button", { name: "开启比赛声音" });
    fireEvent.click(sound);
    fireEvent.click(sound);
    expect(instances[0].play).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "关闭比赛声音" })).toBeDisabled();
  });

  it("does not replay an ended element until the authoritative identity changes", async () => {
    const listeners = new Map<string, EventListener>();
    const instances: FakeAudio[] = [];
    class FakeAudio {
      currentTime = 0;
      preload = "";
      pause = vi.fn();
      load = vi.fn();
      removeAttribute = vi.fn();
      addEventListener = vi.fn((type: string, listener: EventListener) => listeners.set(type, listener));
      removeEventListener = vi.fn();
      play = vi.fn().mockResolvedValue(undefined);
      constructor(public src: string) { instances.push(this); }
    }
    vi.stubGlobal("Audio", FakeAudio);
    const playing = {
      id: "speech-ended", seat_key: "neg_1", speaker: "反方一辩", stage_key: "neg_case", content: "已经播完",
      audio_url: "/media/123456/ended.wav", duration_seconds: 20, playback_started_at: new Date().toISOString(),
      playback_ends_at: new Date(Date.now() + 20_000).toISOString(), status: "playing", created_at: new Date().toISOString(),
    };
    const active = { id: playing.id, seat_key: playing.seat_key, speaker_type: "ai", status: "playing", content: playing.content };
    const { rerender } = render(<DebateStage room={room({ active_speech: active, speeches: [playing] })} connected mode="debate" />);
    await waitFor(() => expect(instances[0].play).toHaveBeenCalledOnce());
    act(() => { listeners.get("ended")?.(new Event("ended")); });
    rerender(<DebateStage room={room({ seq: 4, active_speech: active, speeches: [{ ...playing }] })} connected mode="debate" />);
    fireEvent.click(screen.getByRole("button", { name: "关闭比赛声音" }));
    fireEvent.click(screen.getByRole("button", { name: "开启比赛声音" }));
    expect(instances[0].play).toHaveBeenCalledOnce();
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

  it("ignores a deferred AbortError from a superseded audio without muting the new speech", async () => {
    let rejectFirstPlay: ((reason?: unknown) => void) | undefined;
    const instances: FakeAudio[] = [];
    class FakeAudio {
      currentTime = 0;
      preload = "";
      pause = vi.fn();
      load = vi.fn();
      removeAttribute = vi.fn();
      addEventListener = vi.fn();
      removeEventListener = vi.fn();
      play = vi.fn(() => instances[0] === this
        ? new Promise<void>((_resolve, reject) => { rejectFirstPlay = reject; })
        : Promise.resolve());
      constructor(public src: string) { instances.push(this); }
    }
    vi.stubGlobal("Audio", FakeAudio);
    const speech = (id: string, url: string) => ({
      id, seat_key: "neg_1", speaker: "反方一辩", stage_key: "neg_case", content: `发言 ${id}`,
      audio_url: url, duration_seconds: 20, playback_started_at: new Date().toISOString(),
      playback_ends_at: new Date(Date.now() + 20_000).toISOString(), status: "playing", created_at: new Date().toISOString(),
    });
    const first = speech("speech-a", "/media/123456/a.wav");
    const { rerender } = render(<DebateStage room={room({
      active_speech: { id: first.id, seat_key: first.seat_key, speaker_type: "ai", status: "playing", content: first.content },
      speeches: [first],
    })} connected mode="debate" />);
    await waitFor(() => expect(instances).toHaveLength(1));

    const second = speech("speech-b", "/media/123456/b.wav");
    rerender(<DebateStage room={room({
      seq: 4,
      active_speech: { id: second.id, seat_key: second.seat_key, speaker_type: "ai", status: "playing", content: second.content },
      speeches: [first, second],
    })} connected mode="debate" />);
    await waitFor(() => expect(instances).toHaveLength(2));
    expect(instances[0].pause).toHaveBeenCalled();
    expect(instances[0].load).toHaveBeenCalled();

    await act(async () => { rejectFirstPlay?.(new DOMException("interrupted", "AbortError")); });
    expect(instances[1].pause).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "关闭比赛声音" })).toHaveAttribute("aria-pressed", "true");
  });

  it("retries a NotAllowedError directly from the sound button user gesture", async () => {
    const instances: FakeAudio[] = [];
    class FakeAudio {
      currentTime = 0;
      preload = "";
      pause = vi.fn();
      load = vi.fn();
      removeAttribute = vi.fn();
      addEventListener = vi.fn();
      removeEventListener = vi.fn();
      play = vi.fn()
        .mockRejectedValueOnce(new DOMException("gesture required", "NotAllowedError"))
        .mockResolvedValue(undefined);
      constructor(public src: string) { instances.push(this); }
    }
    vi.stubGlobal("Audio", FakeAudio);
    const playing = {
      id: "speech-gesture", seat_key: "neg_1", speaker: "反方一辩", stage_key: "neg_case", content: "需要手势播放",
      audio_url: "/media/123456/gesture.wav", duration_seconds: 20, playback_started_at: new Date().toISOString(),
      playback_ends_at: new Date(Date.now() + 20_000).toISOString(), status: "playing", created_at: new Date().toISOString(),
    };
    render(<DebateStage room={room({
      active_speech: { id: playing.id, seat_key: playing.seat_key, speaker_type: "ai", status: "playing", content: playing.content },
      speeches: [playing],
    })} connected mode="debate" />);

    const retry = await screen.findByRole("button", { name: "播放比赛声音" });
    fireEvent.click(retry);
    expect(instances).toHaveLength(1);
    expect(instances[0].play).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(screen.getByRole("button", { name: "关闭比赛声音" })).toHaveAttribute("aria-pressed", "true"));
  });

  it("fully disposes the old media element when authoritative playback ends", async () => {
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
    const playing = {
      id: "speech-dispose", seat_key: "neg_1", speaker: "反方一辩", stage_key: "neg_case", content: "即将切换",
      audio_url: "/media/123456/dispose.wav", duration_seconds: 20, playback_started_at: new Date().toISOString(),
      playback_ends_at: new Date(Date.now() + 20_000).toISOString(), status: "playing", created_at: new Date().toISOString(),
    };
    const { rerender } = render(<DebateStage room={room({
      active_speech: { id: playing.id, seat_key: playing.seat_key, speaker_type: "ai", status: "playing", content: playing.content },
      speeches: [playing],
    })} connected mode="debate" />);
    await waitFor(() => expect(instances).toHaveLength(1));

    rerender(<DebateStage room={room({
      seq: 4,
      active_speech: null,
      speeches: [{ ...playing, status: "completed" }],
      current_stage: { key: "aff_summary", name: "正方总结", kind: "speech", duration: 120, seat: "aff_1" },
    })} connected mode="debate" />);
    await waitFor(() => expect(instances[0].load).toHaveBeenCalled());
    expect(instances[0].removeEventListener).toHaveBeenCalledWith("error", expect.any(Function));
    expect(instances[0].removeAttribute).toHaveBeenCalledWith("src");
    expect(instances[0].src).toBe("");
  });
});
