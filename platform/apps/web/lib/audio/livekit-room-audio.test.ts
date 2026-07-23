import { afterEach, beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  const apiFetchMock = vi.fn();
  const roomInstances: FakeRoom[] = [];
  class FakeRoom {
    options: Record<string, unknown>;
    handlers = new Map<string, Array<(...args: never[]) => void>>();
    remoteParticipants = new Map();
    canPlaybackAudio = false;
    prepareConnection = vi.fn().mockResolvedValue(undefined);
    connect = vi.fn().mockResolvedValue(undefined);
    startAudio = vi.fn(async () => { this.canPlaybackAudio = true; });
    disconnect = vi.fn().mockResolvedValue(undefined);
    constructor(options: Record<string, unknown>) {
      this.options = options;
      roomInstances.push(this);
    }
    on(event: string, handler: (...args: never[]) => void) {
      this.handlers.set(event, [...(this.handlers.get(event) || []), handler]);
      return this;
    }
    emit(event: string, ...args: unknown[]) {
      for (const handler of this.handlers.get(event) || []) handler(...args as never[]);
    }
  }
  return { apiFetchMock, roomInstances, FakeRoom };
});

vi.mock("@/lib/api", () => ({ apiFetch: mocks.apiFetchMock }));
vi.mock("livekit-client", () => ({
  Room: mocks.FakeRoom,
  RoomEvent: {
    TrackSubscribed: "trackSubscribed",
    TrackUnsubscribed: "trackUnsubscribed",
    TrackPublished: "trackPublished",
    AudioPlaybackStatusChanged: "audioPlaybackChanged",
    Reconnecting: "reconnecting",
    Reconnected: "reconnected",
    Disconnected: "disconnected",
  },
  Track: { Kind: { Audio: "audio" } },
}));

import { BoundedRtcRecovery, LiveKitRoomAudio } from "@/lib/audio/livekit-room-audio";

beforeEach(() => {
  mocks.roomInstances.length = 0;
  mocks.apiFetchMock.mockReset().mockResolvedValue({
    enabled: true,
    url: "wss://example.test/rtc",
    token: "short-lived-token",
    room_name: "debate:123456",
  });
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => undefined);
  class BasicContext {
    sampleRate = 48_000;
    state: AudioContextState = "suspended";
    destination = {};
    audioWorklet = { addModule: vi.fn().mockResolvedValue(undefined) };
    createMediaStreamSource = vi.fn(() => ({ connect: vi.fn(), disconnect: vi.fn() }));
    resume = vi.fn(async () => { this.state = "running"; });
    close = vi.fn().mockResolvedValue(undefined);
  }
  class BasicGate {
    port = { postMessage: vi.fn(), onmessage: null };
    connect = vi.fn();
    disconnect = vi.fn();
  }
  class BasicMediaStream {
    constructor(public tracks: unknown[]) {}
  }
  vi.stubGlobal("AudioContext", BasicContext);
  vi.stubGlobal("AudioWorkletNode", BasicGate);
  vi.stubGlobal("MediaStream", BasicMediaStream);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.querySelectorAll("audio[data-jixia-agent-audio], audio[data-jixia-agent-audio-decoder]").forEach((element) => element.remove());
});

it("retries application-level RTC recovery after 0.5/1/2 seconds and exhausts exactly once", async () => {
  vi.useFakeTimers();
  const reconnect = vi.fn().mockResolvedValue(false);
  const onAttempt = vi.fn();
  const onRecovered = vi.fn();
  const onExhausted = vi.fn();
  const recovery = new BoundedRtcRecovery(reconnect, { onAttempt, onRecovered, onExhausted });

  recovery.start("fatal disconnect");
  recovery.start("duplicate disconnect");
  expect(onAttempt).toHaveBeenNthCalledWith(1, 1, 500, "fatal disconnect");
  await vi.advanceTimersByTimeAsync(499);
  expect(reconnect).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(1);
  expect(reconnect).toHaveBeenCalledTimes(1);
  expect(onAttempt).toHaveBeenNthCalledWith(2, 2, 1_000, "fatal disconnect");
  await vi.advanceTimersByTimeAsync(1_000);
  expect(reconnect).toHaveBeenCalledTimes(2);
  expect(onAttempt).toHaveBeenNthCalledWith(3, 3, 2_000, "fatal disconnect");
  await vi.advanceTimersByTimeAsync(2_000);

  expect(reconnect).toHaveBeenCalledTimes(3);
  expect(onExhausted).toHaveBeenCalledOnce();
  expect(onExhausted).toHaveBeenCalledWith("fatal disconnect");
  expect(onRecovered).not.toHaveBeenCalled();
});

it("cancels a stale RTC recovery generation and ignores its late success", async () => {
  vi.useFakeTimers();
  let resolveReconnect: ((connected: boolean) => void) | undefined;
  const reconnect = vi.fn(() => new Promise<boolean>((resolve) => { resolveReconnect = resolve; }));
  const onAttempt = vi.fn();
  const onRecovered = vi.fn();
  const onExhausted = vi.fn();
  const recovery = new BoundedRtcRecovery(reconnect, { onAttempt, onRecovered, onExhausted });

  recovery.start("old room");
  await vi.advanceTimersByTimeAsync(500);
  expect(reconnect).toHaveBeenCalledOnce();
  recovery.cancel();
  resolveReconnect?.(true);
  await Promise.resolve();

  expect(onRecovered).not.toHaveBeenCalled();
  expect(onExhausted).not.toHaveBeenCalled();
  expect(vi.getTimerCount()).toBe(0);
});

it("stops the bounded recovery immediately after a successful reconnect", async () => {
  vi.useFakeTimers();
  const reconnect = vi.fn()
    .mockResolvedValueOnce(false)
    .mockResolvedValueOnce(true);
  const onAttempt = vi.fn();
  const onRecovered = vi.fn();
  const onExhausted = vi.fn();
  const recovery = new BoundedRtcRecovery(reconnect, { onAttempt, onRecovered, onExhausted });

  recovery.start("temporary outage");
  await vi.advanceTimersByTimeAsync(500);
  await vi.advanceTimersByTimeAsync(1_000);

  expect(reconnect).toHaveBeenCalledTimes(2);
  expect(onRecovered).toHaveBeenCalledOnce();
  expect(onExhausted).not.toHaveBeenCalled();
  expect(onAttempt).toHaveBeenCalledTimes(2);
});

it("reconnects LiveKit with fresh credentials while preserving the room callbacks", async () => {
  const ready = vi.fn();
  const client = new LiveKitRoomAudio();
  await expect(client.prepare("123456", { onReady: ready })).resolves.toBe(true);
  const firstRoom = mocks.roomInstances[0];
  const generation = "c".repeat(32);
  expect(client.activateGeneration(generation)).toBe(true);

  mocks.apiFetchMock.mockResolvedValueOnce({
    enabled: true,
    url: "wss://example.test/rtc-recovered",
    token: "replacement-token",
    room_name: "debate:123456",
  });
  await expect(client.reconnect()).resolves.toBe(true);
  expect(firstRoom.disconnect).toHaveBeenCalledOnce();
  expect(mocks.roomInstances).toHaveLength(2);
  expect(mocks.roomInstances[1].connect).toHaveBeenCalledWith(
    "wss://example.test/rtc-recovered",
    "replacement-token",
    { autoSubscribe: false },
  );
  expect(client.activateGeneration(generation)).toBe(true);

  const track = { kind: "audio", mediaStreamTrack: { id: "reconnected-track" }, setPlayoutDelay: vi.fn(), attach: vi.fn(), detach: vi.fn() };
  mocks.roomInstances[1].emit("trackSubscribed", track, { trackName: "agent-tts" }, {});
  expect(ready).toHaveBeenCalledOnce();
  await client.dispose();
});

it("marks an intentionally disabled RTC service without constructing a retryable room", async () => {
  mocks.apiFetchMock.mockResolvedValueOnce({ enabled: false });
  const client = new LiveKitRoomAudio();

  await expect(client.prepare("123456")).resolves.toBe(false);

  expect(client.isDisabled()).toBe(true);
  expect(mocks.roomInstances).toHaveLength(0);
  expect(mocks.apiFetchMock).toHaveBeenCalledTimes(1);
  await client.dispose();
});

it("preconnects once, subscribes only the agent track, unlocks, and flushes on interrupt", async () => {
  const needsGesture = vi.fn();
  const ready = vi.fn();
  const client = new LiveKitRoomAudio();
  await expect(client.prepare("123456", { onNeedsGesture: needsGesture, onReady: ready })).resolves.toBe(true);
  const room = mocks.roomInstances[0];
  expect(room.options).toMatchObject({
    adaptiveStream: false,
    dynacast: false,
    stopLocalTrackOnUnpublish: true,
    singlePeerConnection: false,
  });
  expect(room.prepareConnection).toHaveBeenCalledWith("wss://example.test/rtc", "short-lived-token");
  expect(room.connect).toHaveBeenCalledWith("wss://example.test/rtc", "short-lived-token", { autoSubscribe: false });
  expect(needsGesture).toHaveBeenCalledOnce();

  const unrelatedPublication = { kind: "audio", trackName: "human-mic", setSubscribed: vi.fn() };
  const agentPublication = { kind: "audio", trackName: "agent-tts", setSubscribed: vi.fn() };
  room.emit("trackPublished", unrelatedPublication);
  room.emit("trackPublished", agentPublication);
  expect(unrelatedPublication.setSubscribed).toHaveBeenCalledWith(false);
  expect(agentPublication.setSubscribed).toHaveBeenCalledWith(true);

  const track = {
    kind: "audio",
    mediaStreamTrack: { id: "agent-track" },
    setPlayoutDelay: vi.fn(),
    attach: vi.fn(),
    detach: vi.fn(),
  };
  room.emit("trackSubscribed", track, { trackName: "agent-tts" }, {});
  expect(track.setPlayoutDelay).toHaveBeenCalledWith(0.18);
  expect(track.attach).toHaveBeenCalledOnce();
  expect(ready).toHaveBeenCalledOnce();

  await client.unlock();
  expect(room.startAudio).toHaveBeenCalledOnce();
  client.flush();
  expect(track.detach).not.toHaveBeenCalled();
  expect(track.attach).toHaveBeenCalledOnce();
  await client.dispose();
  expect(room.disconnect).toHaveBeenCalledOnce();
});

it("reports jitter-buffer lifetime and interval averages instead of a cumulative latency", async () => {
  vi.useFakeTimers();
  const client = new LiveKitRoomAudio();
  await expect(client.prepare("123456")).resolves.toBe(true);
  const room = mocks.roomInstances[0];
  const reports = [
    new Map<string, Record<string, unknown>>([
      ["inbound", {
        id: "inbound",
        type: "inbound-rtp",
        kind: "audio",
        isRemote: false,
        bytesReceived: 1_000,
        packetsReceived: 100,
        packetsLost: 4,
        concealedSamples: 2_000,
        jitter: 0.01,
        jitterBufferDelay: 2,
        jitterBufferEmittedCount: 100,
      }],
    ]),
    new Map<string, Record<string, unknown>>([
      ["inbound", {
        id: "inbound",
        type: "inbound-rtp",
        kind: "audio",
        isRemote: false,
        bytesReceived: 7_000,
        packetsReceived: 140,
        packetsLost: 5,
        concealedSamples: 2_960,
        jitter: 0.012,
        jitterBufferDelay: 3.2,
        jitterBufferEmittedCount: 140,
      }],
    ]),
  ];
  const getRTCStatsReport = vi.fn()
    .mockResolvedValueOnce(reports[0])
    .mockResolvedValueOnce(reports[1]);
  const networkEvents: Array<Record<string, number>> = [];
  const onNetwork = (event: Event) => networkEvents.push(
    (event as CustomEvent<Record<string, number>>).detail,
  );
  window.addEventListener("jixia:agent-audio-network", onNetwork);
  const track = {
    kind: "audio",
    mediaStreamTrack: { id: "stats-track" },
    setPlayoutDelay: vi.fn(),
    attach: vi.fn(),
    detach: vi.fn(),
    getRTCStatsReport,
  };

  room.emit("trackSubscribed", track, { trackName: "agent-tts", trackSid: "TR_stable" }, {});
  await vi.waitFor(() => expect(networkEvents).toHaveLength(1));
  await vi.advanceTimersByTimeAsync(1_000);
  await vi.waitFor(() => expect(networkEvents).toHaveLength(2));

  expect(networkEvents[0]).toMatchObject({
    packetsReceived: 0,
    packetsLost: 0,
    concealedSamples: 0,
    packetsReceivedTotal: 100,
    packetsLostTotal: 4,
    concealedSamplesTotal: 2_000,
    jitterBufferDelayTotalSeconds: 2,
    jitterBufferDelayAverageSeconds: 0.02,
    jitterBufferDelayCurrentSeconds: 0.02,
    jitterBufferEmittedCount: 100,
  });
  expect(networkEvents[1]).toMatchObject({
    packetsReceived: 40,
    packetsLost: 1,
    concealedSamples: 960,
    packetsReceivedTotal: 140,
    packetsLostTotal: 5,
    concealedSamplesTotal: 2_960,
    jitterBufferDelayTotalSeconds: 3.2,
    jitterBufferEmittedCount: 140,
  });
  expect(networkEvents[1].jitterBufferDelayAverageSeconds).toBeCloseTo(3.2 / 140, 8);
  expect(networkEvents[1].jitterBufferDelayCurrentSeconds).toBeCloseTo(1.2 / 40, 8);

  window.removeEventListener("jixia:agent-audio-network", onNetwork);
  await client.dispose();
});

it("keeps one authoritative agent track and rejects a duplicate instead of switching players", async () => {
  const fatal = vi.fn();
  const client = new LiveKitRoomAudio();
  await expect(client.prepare("123456", { onFatalError: fatal })).resolves.toBe(true);
  const room = mocks.roomInstances[0];
  const first = {
    kind: "audio",
    mediaStreamTrack: { id: "first-agent-track" },
    setPlayoutDelay: vi.fn(),
    attach: vi.fn(),
    detach: vi.fn(),
  };
  const second = {
    kind: "audio",
    mediaStreamTrack: { id: "duplicate-agent-track" },
    setPlayoutDelay: vi.fn(),
    attach: vi.fn(),
    detach: vi.fn(),
  };
  const duplicatePublication = {
    trackName: "agent-tts",
    trackSid: "duplicate-publication",
    setSubscribed: vi.fn(),
  };

  room.emit("trackSubscribed", first, { trackName: "agent-tts", trackSid: "authoritative-publication" }, {});
  room.emit("trackSubscribed", second, duplicatePublication, {});

  expect(first.attach).toHaveBeenCalledOnce();
  expect(first.detach).not.toHaveBeenCalled();
  expect(second.attach).not.toHaveBeenCalled();
  expect(document.querySelectorAll("audio[data-jixia-agent-audio-decoder='123456']")).toHaveLength(1);
  expect(duplicatePublication.setSubscribed).toHaveBeenCalledWith(false);
  expect(fatal).toHaveBeenCalledWith("检测到重复的 AI 语音轨道，请由房主重试当前发言");
  await client.dispose();
});

it("treats an unexpected authoritative track unsubscribe as a recoverable RTC failure", async () => {
  const fatal = vi.fn();
  const client = new LiveKitRoomAudio();
  await expect(client.prepare("123456", { onFatalError: fatal })).resolves.toBe(true);
  const room = mocks.roomInstances[0];
  const mediaTrack = new EventTarget() as MediaStreamTrack;
  Object.defineProperties(mediaTrack, {
    id: { value: "authoritative-media-track" },
    enabled: { value: true },
    muted: { value: false },
    readyState: { value: "live" },
  });
  const track = {
    kind: "audio",
    mediaStreamTrack: mediaTrack,
    setPlayoutDelay: vi.fn(),
    attach: vi.fn(),
    detach: vi.fn(),
  };

  room.emit("trackSubscribed", track, { trackName: "agent-tts" }, {});
  room.emit("trackUnsubscribed", track, { trackName: "agent-tts" }, {});

  expect(fatal).toHaveBeenCalledOnce();
  expect(fatal).toHaveBeenCalledWith("WebRTC AI 语音轨道已取消订阅");
  expect(track.detach).toHaveBeenCalledOnce();
  expect(document.querySelector("audio[data-jixia-agent-audio-decoder='123456']")).toBeNull();
  await client.dispose();
});

it("recovers when the subscribed browser MediaStreamTrack ends without an unsubscribe event", async () => {
  const fatal = vi.fn();
  const client = new LiveKitRoomAudio();
  await expect(client.prepare("123456", { onFatalError: fatal })).resolves.toBe(true);
  const room = mocks.roomInstances[0];
  const mediaTrack = new EventTarget() as MediaStreamTrack;
  Object.defineProperties(mediaTrack, {
    id: { value: "ended-media-track" },
    enabled: { value: true },
    muted: { value: false },
    readyState: { value: "live" },
  });
  const track = {
    kind: "audio",
    mediaStreamTrack: mediaTrack,
    setPlayoutDelay: vi.fn(),
    attach: vi.fn(),
    detach: vi.fn(),
  };

  room.emit("trackSubscribed", track, { trackName: "agent-tts" }, {});
  mediaTrack.dispatchEvent(new Event("ended"));

  expect(fatal).toHaveBeenCalledOnce();
  expect(fatal).toHaveBeenCalledWith("WebRTC AI 语音轨道已中断");
  expect(track.detach).toHaveBeenCalledOnce();
  expect(document.querySelector("audio[data-jixia-agent-audio-decoder='123456']")).toBeNull();
  await client.dispose();
});

it("routes LiveKit through an AudioWorklet interrupt gate and cannot reopen the aborted generation", async () => {
  const gateMessages: Record<string, unknown>[] = [];
  const gateConnect = vi.fn();
  const gateDisconnect = vi.fn();
  const sourceConnect = vi.fn();
  const sourceDisconnect = vi.fn();
  const resume = vi.fn().mockResolvedValue(undefined);
  const close = vi.fn().mockResolvedValue(undefined);
  class FakeContext {
    sampleRate = 48_000;
    state: AudioContextState = "suspended";
    destination = {};
    audioWorklet = { addModule: vi.fn().mockResolvedValue(undefined) };
    createMediaStreamSource = vi.fn(() => ({ connect: sourceConnect, disconnect: sourceDisconnect }));
    resume = resume;
    close = close;
  }
  class FakeGate {
    port = { postMessage: (message: Record<string, unknown>) => gateMessages.push(message) };
    connect = gateConnect;
    disconnect = gateDisconnect;
  }
  class FakeMediaStream {
    constructor(public tracks: unknown[]) {}
  }
  vi.stubGlobal("AudioContext", FakeContext);
  vi.stubGlobal("AudioWorkletNode", FakeGate);
  vi.stubGlobal("MediaStream", FakeMediaStream);

  const client = new LiveKitRoomAudio();
  await expect(client.prepare("123456")).resolves.toBe(true);
  const room = mocks.roomInstances[0];
  const track = {
    kind: "audio",
    mediaStreamTrack: { id: "agent-media-track" },
    setPlayoutDelay: vi.fn(),
    attach: vi.fn(),
    detach: vi.fn(),
  };
  room.emit("trackSubscribed", track, { trackName: "agent-tts" }, {});
  expect(sourceConnect).toHaveBeenCalledOnce();
  expect(gateConnect).toHaveBeenCalledOnce();
  expect(track.attach).toHaveBeenCalledOnce();
  const decoder = document.querySelector("audio[data-jixia-agent-audio-decoder='123456']");
  expect(decoder).toBeInstanceOf(HTMLAudioElement);
  expect((decoder as HTMLAudioElement).muted).toBe(true);

  const oldGeneration = "a".repeat(32);
  const nextGeneration = "b".repeat(32);
  expect(client.activateGeneration(oldGeneration)).toBe(true);
  expect(client.flush(oldGeneration)).toBe(true);
  expect(client.activateGeneration(oldGeneration)).toBe(false);
  const beforeStaleFlush = gateMessages.length;
  expect(client.flush(nextGeneration)).toBe(false);
  expect(gateMessages).toHaveLength(beforeStaleFlush);
  expect(client.activateGeneration(nextGeneration)).toBe(true);
  expect(gateMessages).toContainEqual(expect.objectContaining({
    type: "flush",
    generation: oldGeneration,
    guardFrames: 10_560,
  }));
  expect(gateMessages.at(-1)).toMatchObject({ type: "activate", generation: nextGeneration });

  await client.unlock();
  expect(resume).toHaveBeenCalledOnce();
  await client.dispose();
  expect(sourceDisconnect).toHaveBeenCalledOnce();
  expect(track.detach).toHaveBeenCalledWith(decoder);
  expect(document.querySelector("audio[data-jixia-agent-audio-decoder='123456']")).toBeNull();
  expect(gateDisconnect).toHaveBeenCalledOnce();
  expect(close).toHaveBeenCalledOnce();
});

it("preserves an early enable-sound gesture while the worklet and RTC credentials are still loading", async () => {
  let resolveModule: (() => void) | undefined;
  let resolveCredentials: ((value: {
    enabled: boolean;
    url: string;
    token: string;
    room_name: string;
  }) => void) | undefined;
  const moduleReady = new Promise<void>((resolve) => { resolveModule = resolve; });
  const credentialsReady = new Promise<{
    enabled: boolean;
    url: string;
    token: string;
    room_name: string;
  }>((resolve) => { resolveCredentials = resolve; });
  mocks.apiFetchMock.mockReturnValueOnce(credentialsReady);

  const resume = vi.fn(async function (this: FakeContext) { this.state = "running"; });
  const close = vi.fn().mockResolvedValue(undefined);
  class FakeContext {
    sampleRate = 48_000;
    state: AudioContextState = "suspended";
    destination = {};
    audioWorklet = { addModule: vi.fn(() => moduleReady) };
    createMediaStreamSource = vi.fn();
    resume = resume;
    close = close;
  }
  class FakeGate {
    port = { postMessage: vi.fn(), onmessage: null };
    connect = vi.fn();
    disconnect = vi.fn();
  }
  class FakeMediaStream {}
  vi.stubGlobal("AudioContext", FakeContext);
  vi.stubGlobal("AudioWorkletNode", FakeGate);
  vi.stubGlobal("MediaStream", FakeMediaStream);

  const client = new LiveKitRoomAudio();
  const preparing = client.prepare("123456");

  // Neither the token request nor addModule() has completed, but the trusted
  // click can already resume the context and its intent survives connection.
  await expect(client.unlock()).resolves.toBeUndefined();
  expect(resume).toHaveBeenCalledOnce();
  expect(mocks.roomInstances).toHaveLength(0);

  resolveModule?.();
  resolveCredentials?.({
    enabled: true,
    url: "wss://example.test/rtc",
    token: "short-lived-token",
    room_name: "debate:123456",
  });
  await expect(preparing).resolves.toBe(true);
  expect(mocks.roomInstances[0].startAudio).toHaveBeenCalledOnce();

  await client.dispose();
  expect(close).toHaveBeenCalledOnce();
});

it("continues to the authoritative RTC connection when optional preconnect stalls", async () => {
  vi.useFakeTimers();
  const client = new LiveKitRoomAudio();
  const preparing = client.prepare("123456");
  await vi.waitFor(() => expect(mocks.roomInstances).toHaveLength(1));
  const room = mocks.roomInstances[0];
  room.prepareConnection.mockReturnValueOnce(new Promise(() => undefined));

  await vi.advanceTimersByTimeAsync(2_500);
  await expect(preparing).resolves.toBe(true);
  expect(room.connect).toHaveBeenCalledWith(
    "wss://example.test/rtc",
    "short-lived-token",
    { autoSubscribe: false },
  );
  await client.dispose();
});
