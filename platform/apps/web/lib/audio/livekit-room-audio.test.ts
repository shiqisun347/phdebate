import { afterEach, beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  const apiFetchMock = vi.fn();
  const roomInstances: FakeRoom[] = [];
  class FakeRoom {
    options: Record<string, unknown>;
    handlers = new Map<string, Array<(...args: never[]) => void>>();
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
    AudioPlaybackStatusChanged: "audioPlaybackChanged",
    Reconnecting: "reconnecting",
    Reconnected: "reconnected",
    Disconnected: "disconnected",
  },
  Track: { Kind: { Audio: "audio" } },
}));

import { LiveKitRoomAudio } from "@/lib/audio/livekit-room-audio";

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
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.querySelectorAll("audio[data-jixia-agent-audio]").forEach((element) => element.remove());
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
  expect(room.connect).toHaveBeenCalledWith("wss://example.test/rtc", "short-lived-token", { autoSubscribe: true });
  expect(needsGesture).toHaveBeenCalledOnce();

  const track = {
    kind: "audio",
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
  vi.useFakeTimers();
  client.flush();
  expect(track.detach).toHaveBeenCalledOnce();
  await vi.advanceTimersByTimeAsync(220);
  expect(track.attach).toHaveBeenCalledTimes(2);
  await client.dispose();
  expect(room.disconnect).toHaveBeenCalledOnce();
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
    { autoSubscribe: true },
  );
  await client.dispose();
});
