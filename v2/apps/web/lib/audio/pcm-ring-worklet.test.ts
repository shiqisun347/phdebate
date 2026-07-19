import { afterEach, expect, it, vi } from "vitest";

it("keeps the AudioWorklet ring bounded and raises the adaptive target after one underrun episode", async () => {
  type ProcessorInstance = {
    port: { onmessage: ((event: { data: Record<string, unknown> }) => void) | null; sent: Record<string, unknown>[] };
    process: (inputs: unknown[], outputs: Float32Array[][]) => boolean;
  };
  type ProcessorConstructor = new () => ProcessorInstance;
  let Processor: ProcessorConstructor | undefined;
  class FakeAudioWorkletProcessor {
    port = {
      onmessage: null as ((event: { data: Record<string, unknown> }) => void) | null,
      sent: [] as Record<string, unknown>[],
      postMessage(message: Record<string, unknown>) { this.sent.push(message); },
    };
  }
  vi.stubGlobal("sampleRate", 1_000);
  vi.stubGlobal("AudioWorkletProcessor", FakeAudioWorkletProcessor);
  vi.stubGlobal("registerProcessor", (_name: string, constructor: ProcessorConstructor) => { Processor = constructor; });
  // @ts-expect-error Public worklet JavaScript is executed directly by AudioWorkletGlobalScope.
  await import("../../public/worklets/pcm-ring-player.js");
  expect(Processor).not.toBeNull();
  const Constructor = Processor as ProcessorConstructor | undefined;
  if (!Constructor) throw new Error("worklet processor was not registered");
  const processor = new Constructor();
  processor.port.onmessage?.({ data: { type: "reset", generation: 7, capacityFrames: 256, startThresholdFrames: 128 } });
  processor.port.onmessage?.({ data: { type: "enqueue", generation: 7, frames: new Float32Array(128).fill(0.25) } });
  const first = new Float32Array(128);
  expect(processor.process([], [[first]])).toBe(true);
  expect([...first]).toEqual([...new Float32Array(128).fill(0.25)]);
  const second = new Float32Array(128);
  processor.process([], [[second]]);
  const underruns = processor.port.sent.filter((message: Record<string, unknown>) => message.type === "underrun");
  expect(underruns).toHaveLength(1);
  expect(underruns[0].targetBufferFrames).toBeGreaterThan(128);
  processor.process([], [[new Float32Array(128)]]);
  expect(processor.port.sent.filter((message: Record<string, unknown>) => message.type === "underrun")).toHaveLength(1);

  processor.port.onmessage?.({ data: { type: "enqueue", generation: 7, frames: new Float32Array(300) } });
  expect(processor.port.sent.some((message: Record<string, unknown>) => message.type === "overflow")).toBe(true);
});

it("plays and drains a final tail that is shorter than the priming threshold", async () => {
  type ProcessorInstance = {
    port: { onmessage: ((event: { data: Record<string, unknown> }) => void) | null; sent: Record<string, unknown>[] };
    process: (inputs: unknown[], outputs: Float32Array[][]) => boolean;
  };
  type ProcessorConstructor = new () => ProcessorInstance;
  let Processor: ProcessorConstructor | undefined;
  class FakeAudioWorkletProcessor {
    port = {
      onmessage: null as ((event: { data: Record<string, unknown> }) => void) | null,
      sent: [] as Record<string, unknown>[],
      postMessage(message: Record<string, unknown>) { this.sent.push(message); },
    };
  }
  vi.stubGlobal("sampleRate", 48_000);
  vi.stubGlobal("AudioWorkletProcessor", FakeAudioWorkletProcessor);
  vi.stubGlobal("registerProcessor", (_name: string, constructor: ProcessorConstructor) => { Processor = constructor; });
  vi.resetModules();
  // @ts-expect-error Public worklet JavaScript is executed directly by AudioWorkletGlobalScope.
  await import("../../public/worklets/pcm-ring-player.js");
  const Constructor = Processor as ProcessorConstructor | undefined;
  if (!Constructor) throw new Error("worklet processor was not registered");
  const processor = new Constructor();
  processor.port.onmessage?.({ data: { type: "reset", generation: 9, capacityFrames: 512, startThresholdFrames: 400 } });
  processor.port.onmessage?.({ data: { type: "enqueue", generation: 9, frames: new Float32Array(128).fill(0.5) } });
  expect(processor.port.sent.filter((message) => message.type === "started")).toHaveLength(0);

  processor.port.onmessage?.({ data: { type: "end", generation: 9 } });
  expect(processor.port.sent.filter((message) => message.type === "started")).toHaveLength(1);
  const output = new Float32Array(128);
  expect(processor.process([], [[output]])).toBe(true);
  expect([...output]).toEqual([...new Float32Array(128).fill(0.5)]);
  processor.process([], [[new Float32Array(128)]]);
  expect(processor.port.sent.filter((message) => message.type === "drained")).toHaveLength(1);
  expect(processor.port.sent.filter((message) => message.type === "underrun")).toHaveLength(0);
});

afterEach(() => {
  vi.unstubAllGlobals();
});
