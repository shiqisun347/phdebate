import { websocketUrl } from "@/lib/api";
import { StreamingLinearResampler } from "@/lib/audio/streaming-resampler";

const HEADER_BYTES = 24;
const PROTOCOL_VERSION = 1;
const CAPACITY_SECONDS = 3;
const START_THRESHOLD_SECONDS = 0.1;
const MAXIMUM_THRESHOLD_SECONDS = 0.24;

export type PcmStreamSpec = {
  roomCode: string;
  speechId: string;
  generation: string;
  sampleRate: number;
  afterSeq: number;
};

export type PcmStreamCallbacks = {
  onNeedsGesture?: () => void;
  onStarted?: () => void;
  onDrained?: () => void;
  onRecoverableError?: (reason: string) => void;
  onFatalError?: (reason: string) => void;
};

type WorkletMessage = {
  type?: string;
  generation?: number;
  bufferedFrames?: number;
  playedFrames?: number;
  targetBufferFrames?: number;
  underrunCount?: number;
};

function workletUrl() {
  const basePath = process.env.NEXT_PUBLIC_BASE_PATH || "";
  return `${window.location.origin}${basePath}/worklets/pcm-ring-player.js`;
}

function pcm16ToFloat32(payload: ArrayBuffer, byteOffset: number, sampleCount: number) {
  const view = new DataView(payload, byteOffset, sampleCount * 2);
  const output = new Float32Array(sampleCount);
  for (let index = 0; index < sampleCount; index += 1) output[index] = view.getInt16(index * 2, true) / 32768;
  return output;
}

export class PcmStreamPlayer {
  private context: AudioContext | null = null;
  private node: AudioWorkletNode | null = null;
  private socket: WebSocket | null = null;
  private transportGeneration = 0;
  private serverGeneration = "";
  private serverGenerationId = 0;
  private expectedSeq = -1;
  private expectedPts = 0;
  private frameSamples = 960;
  private resampler: StreamingLinearResampler | null = null;
  private callbacks: PcmStreamCallbacks = {};
  private initialization: Promise<void> | null = null;

  prepare() {
    return this.ensureWorklet();
  }

  private async ensureWorklet() {
    if (this.node && this.context) return;
    if (this.initialization) return this.initialization;
    this.initialization = (async () => {
      const AudioContextClass = window.AudioContext
        || (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!AudioContextClass || typeof AudioWorkletNode === "undefined") throw new Error("当前浏览器不支持实时音频播放");
      const context = new AudioContextClass({ latencyHint: "interactive" });
      await context.audioWorklet.addModule(workletUrl());
      const node = new AudioWorkletNode(context, "jixia-pcm-ring-player", { numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1] });
      node.connect(context.destination);
      node.port.onmessage = (event: MessageEvent<WorkletMessage>) => this.handleWorkletMessage(event.data);
      this.context = context;
      this.node = node;
    })();
    try {
      await this.initialization;
    } finally {
      this.initialization = null;
    }
  }

  async start(spec: PcmStreamSpec, callbacks: PcmStreamCallbacks = {}) {
    this.stop();
    const transportGeneration = this.transportGeneration;
    this.callbacks = callbacks;
    try {
      await this.ensureWorklet();
    } catch (error) {
      if (this.transportGeneration === transportGeneration) callbacks.onFatalError?.(error instanceof Error ? error.message : "实时音频初始化失败");
      return;
    }
    if (this.transportGeneration !== transportGeneration || !this.node || !this.context) return;
    this.serverGeneration = spec.generation;
    this.serverGenerationId = Number.parseInt(spec.generation.slice(0, 8), 16) >>> 0;
    this.frameSamples = Math.max(1, Math.round(spec.sampleRate * 0.04));
    this.expectedSeq = Math.max(0, spec.afterSeq + 1);
    this.expectedPts = this.expectedSeq * this.frameSamples;
    this.resampler = new StreamingLinearResampler(spec.sampleRate, this.context.sampleRate);
    this.node.port.postMessage({
      type: "reset",
      generation: transportGeneration,
      capacityFrames: Math.round(this.context.sampleRate * CAPACITY_SECONDS),
      startThresholdFrames: Math.round(this.context.sampleRate * START_THRESHOLD_SECONDS),
      maximumThresholdFrames: Math.round(this.context.sampleRate * MAXIMUM_THRESHOLD_SECONDS),
    });
    const socket = new WebSocket(websocketUrl(`/ws/rooms/${spec.roomCode}/audio`));
    socket.binaryType = "arraybuffer";
    this.socket = socket;
    socket.onopen = () => {
      if (!this.isCurrent(socket, transportGeneration)) return;
      socket.send(JSON.stringify({
        type: "subscribe",
        protocol_version: PROTOCOL_VERSION,
        speech_id: spec.speechId,
        generation: spec.generation,
        after_seq: spec.afterSeq,
        flow_control: true,
      }));
      if (this.context?.state !== "running") callbacks.onNeedsGesture?.();
    };
    socket.onmessage = (event) => this.handleSocketMessage(socket, transportGeneration, event);
    socket.onerror = () => {
      if (this.isCurrent(socket, transportGeneration)) this.fail("实时音频连接失败", true);
    };
    socket.onclose = (event) => {
      if (!this.isCurrent(socket, transportGeneration)) return;
      this.socket = null;
      if (![1000, 1005].includes(event.code)) this.fail("实时音频连接已中断", true);
    };
  }

  unlock(): Promise<void> {
    const request = this.context?.resume();
    if (!request) return Promise.reject(new Error("实时音频尚未初始化"));
    return request;
  }

  stop() {
    const generation = this.transportGeneration;
    this.transportGeneration += 1;
    const socket = this.socket;
    this.socket = null;
    if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, "stream reset");
    this.node?.port.postMessage({ type: "flush", generation });
    this.resampler = null;
    this.serverGeneration = "";
    this.expectedSeq = -1;
    this.expectedPts = 0;
  }

  async dispose() {
    this.stop();
    this.node?.disconnect();
    this.node = null;
    const context = this.context;
    this.context = null;
    await context?.close().catch(() => undefined);
  }

  private isCurrent(socket: WebSocket, generation: number) {
    return this.socket === socket && this.transportGeneration === generation;
  }

  private handleSocketMessage(socket: WebSocket, generation: number, event: MessageEvent) {
    if (!this.isCurrent(socket, generation) || !this.node) return;
    if (typeof event.data === "string") {
      let message: Record<string, unknown>;
      try { message = JSON.parse(event.data) as Record<string, unknown>; } catch { this.fail("实时音频控制消息无效", false); return; }
      if (message.type === "audio.start") {
        if (
          message.frame_samples !== this.frameSamples
          || !Number.isInteger(message.start_seq)
          || Number(message.start_seq) < 0
          || !Number.isInteger(message.start_pts_samples)
          || Number(message.start_pts_samples) < 0
        ) {
          this.fail("实时音频起始位置无效", false);
          return;
        }
        this.expectedSeq = Number(message.start_seq);
        this.expectedPts = Number(message.start_pts_samples);
        return;
      }
      if (message.type === "audio.final") {
        this.node.port.postMessage({ type: "end", generation });
        return;
      }
      if (message.type === "audio.abort") {
        this.fail("本轮音频已被比赛状态中断", true);
        return;
      }
      return;
    }
    if (!(event.data instanceof ArrayBuffer) || event.data.byteLength < HEADER_BYTES) {
      this.fail("实时音频帧无效", false);
      return;
    }
    const view = new DataView(event.data);
    if (view.getUint8(0) !== 0x4a || view.getUint8(1) !== 0x58 || view.getUint8(2) !== PROTOCOL_VERSION) {
      this.fail("实时音频协议不兼容", false);
      return;
    }
    const generationId = view.getUint32(4, false);
    const seq = view.getUint32(8, false);
    const pts = view.getUint32(12, false) * 2 ** 32 + view.getUint32(16, false);
    const sampleCount = view.getUint32(20, false);
    if (
      generationId !== this.serverGenerationId
      || seq !== this.expectedSeq
      || pts !== this.expectedPts
      || event.data.byteLength !== HEADER_BYTES + sampleCount * 2
    ) {
      this.fail("实时音频出现断帧，正在追赶比赛进度", true);
      return;
    }
    this.expectedSeq += 1;
    this.expectedPts += sampleCount;
    const input = pcm16ToFloat32(event.data, HEADER_BYTES, sampleCount);
    const frames = this.resampler?.process(input) || input;
    this.node.port.postMessage({ type: "enqueue", generation, frames }, [frames.buffer]);
  }

  private handleWorkletMessage(message: WorkletMessage) {
    if (message.generation !== this.transportGeneration) return;
    if (message.type === "buffer" || message.type === "started") this.sendFlowControl(message);
    if (message.type === "started") this.callbacks.onStarted?.();
    else if (message.type === "drained") this.callbacks.onDrained?.();
    else if (message.type === "underrun") this.sendFlowControl(message);
    else if (message.type === "overflow") this.fail("实时音频缓冲超过上限，正在追赶比赛进度", true);
  }

  private sendFlowControl(message: WorkletMessage) {
    const socket = this.socket;
    const context = this.context;
    if (!socket || socket.readyState !== WebSocket.OPEN || !context || !this.serverGeneration) return;
    const bufferedFrames = Math.max(0, Number(message.bufferedFrames) || 0);
    const playedFrames = Math.max(0, Number(message.playedFrames) || 0);
    socket.send(JSON.stringify({
      type: "flow",
      protocol_version: PROTOCOL_VERSION,
      generation: this.serverGeneration,
      buffered_ms: Math.round(bufferedFrames * 1000 / context.sampleRate),
      played_ms: Math.round(playedFrames * 1000 / context.sampleRate),
    }));
  }

  private fail(reason: string, recoverable: boolean) {
    const callbacks = this.callbacks;
    this.stop();
    if (recoverable) callbacks.onRecoverableError?.(reason);
    else callbacks.onFatalError?.(reason);
  }
}

export function streamAfterSeq(playbackStartedAt: string, sampleRate: number, frameSamples = 960) {
  const startedAt = new Date(playbackStartedAt).getTime();
  if (!Number.isFinite(startedAt) || sampleRate <= 0 || frameSamples <= 0) return -1;
  const elapsed = Math.max(0, (Date.now() - startedAt) / 1000 - START_THRESHOLD_SECONDS);
  return Math.max(-1, Math.floor(elapsed * sampleRate / frameSamples) - 1);
}
