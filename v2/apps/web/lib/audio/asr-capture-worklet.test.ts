import { afterEach, expect, it, vi } from "vitest";

it("emits ordered 20 ms capture chunks and flushes the final tail", async () => {
  type ProcessorInstance = {
    port: {
      onmessage: ((event: { data: Record<string, unknown> }) => void) | null;
      sent: Array<{ message: Record<string, unknown>; transfer?: ArrayBuffer[] }>;
    };
    process: (inputs: Float32Array[][], outputs: Float32Array[][]) => boolean;
  };
  type ProcessorConstructor = new (options?: { processorOptions?: Record<string, unknown> }) => ProcessorInstance;
  let Processor: ProcessorConstructor | undefined;
  class FakeAudioWorkletProcessor {
    port = {
      onmessage: null as ((event: { data: Record<string, unknown> }) => void) | null,
      sent: [] as Array<{ message: Record<string, unknown>; transfer?: ArrayBuffer[] }>,
      postMessage(message: Record<string, unknown>, transfer?: ArrayBuffer[]) {
        this.sent.push({ message, transfer });
      },
    };
  }
  vi.stubGlobal("sampleRate", 48_000);
  vi.stubGlobal("AudioWorkletProcessor", FakeAudioWorkletProcessor);
  vi.stubGlobal("registerProcessor", (_name: string, constructor: ProcessorConstructor) => { Processor = constructor; });
  // @ts-expect-error Public worklet JavaScript is executed directly by AudioWorkletGlobalScope.
  await import("../../public/worklets/asr-pcm-capture.js");
  const Constructor = Processor;
  if (!Constructor) throw new Error("capture worklet processor was not registered");
  const processor = new Constructor({ processorOptions: { chunkMs: 20, generation: 11 } });
  const first = new Float32Array(700).fill(0.25);
  const second = new Float32Array(500).fill(-0.5);
  expect(processor.process([[first]], [[new Float32Array(128)]])).toBe(true);
  expect(processor.port.sent).toHaveLength(0);
  processor.process([[second]], [[new Float32Array(128)]]);
  const chunks = processor.port.sent.filter((item) => item.message.type === "frames");
  expect(chunks).toHaveLength(1);
  expect((chunks[0].message.frames as Float32Array).length).toBe(960);
  expect((chunks[0].message.frames as Float32Array)[0]).toBe(0.25);
  expect((chunks[0].message.frames as Float32Array)[959]).toBe(-0.5);

  processor.port.onmessage?.({ data: { type: "flush", generation: 11 } });
  const flushedChunks = processor.port.sent.filter((item) => item.message.type === "frames");
  expect(flushedChunks).toHaveLength(2);
  expect((flushedChunks[1].message.frames as Float32Array).length).toBe(240);
  expect(processor.port.sent.at(-1)?.message).toMatchObject({ type: "flushed", generation: 11 });
});

afterEach(() => {
  vi.resetModules();
  vi.unstubAllGlobals();
});
