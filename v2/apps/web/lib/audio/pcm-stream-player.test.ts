import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PcmStreamPlayer, streamAfterSeq } from "@/lib/audio/pcm-stream-player";

class FakePort {
  onmessage: ((event: MessageEvent) => void) | null = null;
  messages: Array<{ message: Record<string, unknown>; transfer?: Transferable[] }> = [];
  postMessage(message: Record<string, unknown>, transfer?: Transferable[]) { this.messages.push({ message, transfer }); }
  emit(message: Record<string, unknown>) { this.onmessage?.({ data: message } as MessageEvent); }
}

class FakeAudioWorkletNode {
  static instances: FakeAudioWorkletNode[] = [];
  port = new FakePort();
  connect = vi.fn();
  disconnect = vi.fn();
  constructor() { FakeAudioWorkletNode.instances.push(this); }
}

class FakeAudioContext {
  state: AudioContextState = "suspended";
  sampleRate = 48_000;
  destination = {} as AudioDestinationNode;
  audioWorklet = { addModule: vi.fn().mockResolvedValue(undefined) };
  resume = vi.fn(async () => { this.state = "running"; });
  close = vi.fn().mockResolvedValue(undefined);
}

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances: FakeWebSocket[] = [];
  readyState = FakeWebSocket.CONNECTING;
  binaryType = "";
  sent: string[] = [];
  closeCalls: Array<{ code: number; reason: string }> = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  constructor(public url: string) { FakeWebSocket.instances.push(this); }
  open() { this.readyState = FakeWebSocket.OPEN; this.onopen?.(); }
  send(value: string) { this.sent.push(value); }
  message(data: string | ArrayBuffer) { this.onmessage?.({ data } as MessageEvent); }
  close(code = 1000, reason = "") {
    this.closeCalls.push({ code, reason });
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({ code } as CloseEvent);
  }
}

function audioFrame(generation: string, seq: number, pts: number, samples: number) {
  const buffer = new ArrayBuffer(24 + samples * 2);
  const view = new DataView(buffer);
  view.setUint8(0, 0x4a);
  view.setUint8(1, 0x58);
  view.setUint8(2, 1);
  view.setUint8(3, 0);
  view.setUint32(4, Number.parseInt(generation.slice(0, 8), 16), false);
  view.setUint32(8, seq, false);
  view.setUint32(12, Math.floor(pts / 2 ** 32), false);
  view.setUint32(16, pts >>> 0, false);
  view.setUint32(20, samples, false);
  for (let index = 0; index < samples; index += 1) view.setInt16(24 + index * 2, index % 2 ? -1000 : 1000, true);
  return buffer;
}

describe("PcmStreamPlayer", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    FakeAudioWorkletNode.instances = [];
    vi.stubGlobal("AudioContext", FakeAudioContext);
    vi.stubGlobal("AudioWorkletNode", FakeAudioWorkletNode);
    vi.stubGlobal("WebSocket", FakeWebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("subscribes with generation and sends continuous PCM to one bounded worklet generation", async () => {
    const needsGesture = vi.fn();
    const recoverable = vi.fn();
    const generation = "12345678" + "a".repeat(24);
    const player = new PcmStreamPlayer();
    await player.start(
      { roomCode: "123456", speechId: "speech-1", generation, sampleRate: 24_000, afterSeq: -1 },
      { onNeedsGesture: needsGesture, onRecoverableError: recoverable },
    );
    const socket = FakeWebSocket.instances[0];
    socket.open();
    expect(JSON.parse(socket.sent[0])).toEqual({
      type: "subscribe",
      protocol_version: 1,
      speech_id: "speech-1",
      generation,
      after_seq: -1,
      flow_control: true,
    });
    expect(needsGesture).toHaveBeenCalledTimes(1);
    socket.message(JSON.stringify({ type: "audio.start", frame_samples: 960, start_seq: 0, start_pts_samples: 0 }));
    socket.message(audioFrame(generation, 0, 0, 960));
    socket.message(audioFrame(generation, 1, 960, 960));
    const port = FakeAudioWorkletNode.instances[0].port;
    const enqueues = port.messages.filter((item) => item.message.type === "enqueue");
    expect(enqueues).toHaveLength(2);
    expect((enqueues[0].message.frames as Float32Array).length).toBeGreaterThan(1_900);
    const reset = port.messages.find((item) => item.message.type === "reset")?.message;
    port.emit({ type: "buffer", generation: reset?.generation, bufferedFrames: 24_000, playedFrames: 12_000 });
    expect(JSON.parse(socket.sent.at(-1) || "{}")).toEqual({
      type: "flow",
      protocol_version: 1,
      generation,
      buffered_ms: 500,
      played_ms: 250,
    });
    expect(recoverable).not.toHaveBeenCalled();
    socket.message(JSON.stringify({ type: "audio.final" }));
    expect(port.messages.at(-1)?.message.type).toBe("end");
  });

  it("drops stale transports and recovers from sequence gaps", async () => {
    const recoverable = vi.fn();
    const generation = "abcdef12" + "0".repeat(24);
    const player = new PcmStreamPlayer();
    await player.start(
      { roomCode: "123456", speechId: "speech-1", generation, sampleRate: 24_000, afterSeq: -1 },
      { onRecoverableError: recoverable },
    );
    const first = FakeWebSocket.instances[0];
    first.open();
    await player.start(
      { roomCode: "123456", speechId: "speech-1", generation, sampleRate: 24_000, afterSeq: 2 },
      { onRecoverableError: recoverable },
    );
    const second = FakeWebSocket.instances[1];
    second.open();
    first.message(audioFrame(generation, 0, 0, 960));
    second.message(audioFrame(generation, 4, 3_840, 960));
    expect(recoverable).toHaveBeenCalledWith(expect.stringContaining("断帧"));
    expect(second.closeCalls).toContainEqual({ code: 1000, reason: "stream reset" });
  });

  it("accepts the server live-edge clamp before validating the first PCM frame", async () => {
    const recoverable = vi.fn();
    const generation = "fedcba98" + "1".repeat(24);
    const player = new PcmStreamPlayer();
    await player.start(
      { roomCode: "123456", speechId: "speech-live-edge", generation, sampleRate: 24_000, afterSeq: 999 },
      { onRecoverableError: recoverable },
    );
    const socket = FakeWebSocket.instances[0];
    socket.open();
    socket.message(JSON.stringify({
      type: "audio.start",
      frame_samples: 960,
      start_seq: 2,
      start_pts_samples: 1_920,
    }));
    socket.message(audioFrame(generation, 2, 1_920, 960));
    expect(recoverable).not.toHaveBeenCalled();
    expect(FakeAudioWorkletNode.instances[0].port.messages.some((item) => item.message.type === "enqueue")).toBe(true);
  });

  it("resumes the pre-created AudioContext only from the caller's unlock action", async () => {
    const player = new PcmStreamPlayer();
    await player.start({ roomCode: "123456", speechId: "speech", generation: "1".repeat(32), sampleRate: 24_000, afterSeq: -1 });
    const context = (player as unknown as { context: FakeAudioContext }).context;
    expect(context.resume).not.toHaveBeenCalled();
    await player.unlock();
    expect(context.resume).toHaveBeenCalledTimes(1);
  });
});

describe("streamAfterSeq", () => {
  it("starts about one priming window behind the authoritative live edge", () => {
    vi.spyOn(Date, "now").mockReturnValue(new Date("2026-07-17T10:00:10.000Z").getTime());
    expect(streamAfterSeq("2026-07-17T10:00:00.000Z", 24_000)).toBe(246);
  });
});
