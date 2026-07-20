import { afterEach, expect, it, vi } from "vitest";

it("declicks WebRTC generation edges, keeps the full interrupt guard, and ignores stale epochs", async () => {
  type ProcessorInstance = {
    port: { onmessage: ((event: { data: Record<string, unknown> }) => void) | null; sent: Record<string, unknown>[] };
    process: (inputs: Float32Array[][], outputs: Float32Array[][]) => boolean;
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
  vi.resetModules();
  // @ts-expect-error Public worklet JavaScript is executed directly by AudioWorkletGlobalScope.
  await import("../../public/worklets/livekit-interrupt-gate.js");
  const Constructor = Processor;
  if (!Constructor) throw new Error("interrupt gate worklet was not registered");
  const processor = new Constructor();
  const input = new Float32Array(128).fill(0.75);

  const before = new Float32Array(128);
  expect(processor.process([[input]], [[before]])).toBe(true);
  expect([...before]).toEqual([...input]);

  processor.port.onmessage?.({ data: { type: "flush", epoch: 1, guardFrames: 256 } });
  processor.port.onmessage?.({ data: { type: "activate", epoch: 2 } });
  const guardedFirst = new Float32Array(128);
  const guardedLast = new Float32Array(128);
  processor.process([[input]], [[guardedFirst]]);
  processor.process([[input]], [[guardedLast]]);
  expect(guardedFirst[0]).toBeGreaterThan(0);
  expect(guardedFirst[0]).toBeLessThan(input[0]);
  expect(guardedFirst[4]).toBe(0);
  expect(Math.max(...guardedFirst.slice(5))).toBe(0);
  expect([...guardedLast]).toEqual([...new Float32Array(128)]);
  expect(processor.port.sent).toContainEqual({ type: "flushed", epoch: 2 });

  const resumed = new Float32Array(128);
  processor.process([[input]], [[resumed]]);
  expect(resumed[0]).toBeGreaterThan(0);
  expect(resumed[0]).toBeLessThan(input[0]);
  expect(resumed[4]).toBe(input[4]);
  expect(Math.max(
    Math.abs(resumed[0]),
    ...resumed.slice(1, 6).map((value, index) => Math.abs(value - resumed[index])),
  )).toBeLessThanOrEqual(0.150001);
  expect([...resumed.slice(5)]).toEqual([...input.slice(5)]);

  processor.port.onmessage?.({ data: { type: "flush", epoch: 1, guardFrames: 1_000 } });
  const afterStaleFlush = new Float32Array(128);
  processor.process([[input]], [[afterStaleFlush]]);
  expect([...afterStaleFlush]).toEqual([...input]);
});

it("reports only sustained audible and silent transitions from the rendered output", async () => {
  type ProcessorInstance = {
    port: { onmessage: ((event: { data: Record<string, unknown> }) => void) | null; sent: Record<string, unknown>[] };
    process: (inputs: Float32Array[][], outputs: Float32Array[][]) => boolean;
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
  vi.stubGlobal("currentTime", 1.25);
  vi.stubGlobal("AudioWorkletProcessor", FakeAudioWorkletProcessor);
  vi.stubGlobal("registerProcessor", (_name: string, constructor: ProcessorConstructor) => { Processor = constructor; });
  vi.resetModules();
  // @ts-expect-error Public worklet JavaScript is executed directly by AudioWorkletGlobalScope.
  await import("../../public/worklets/livekit-interrupt-gate.js");
  if (!Processor) throw new Error("interrupt gate worklet was not registered");
  const processor = new Processor();
  processor.port.onmessage?.({ data: { type: "activate", epoch: 1, generation: "generation-a" } });
  const audible = new Float32Array(960).fill(0.25);
  for (let index = 0; index < 3; index += 1) processor.process([[audible]], [[new Float32Array(960)]]);
  expect(processor.port.sent).toContainEqual(expect.objectContaining({
    type: "audibility",
    audible: true,
    generation: "generation-a",
  }));
  const silence = new Float32Array(960);
  for (let index = 0; index < 8; index += 1) processor.process([[silence]], [[new Float32Array(960)]]);
  expect(processor.port.sent).toContainEqual(expect.objectContaining({
    type: "audibility",
    audible: false,
    generation: "generation-a",
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
});
