import { describe, expect, it } from "vitest";
import { StreamingLinearResampler } from "@/lib/audio/streaming-resampler";

describe("StreamingLinearResampler", () => {
  it("preserves continuity across arbitrary input chunks", () => {
    const input = Float32Array.from({ length: 2_400 }, (_value, index) => Math.sin(index / 17));
    const whole = new StreamingLinearResampler(24_000, 48_000).process(input);
    const chunkedResampler = new StreamingLinearResampler(24_000, 48_000);
    const parts = [
      chunkedResampler.process(input.slice(0, 317)),
      chunkedResampler.process(input.slice(317, 1_201)),
      chunkedResampler.process(input.slice(1_201)),
    ];
    const chunked = Float32Array.from(parts.flatMap((part) => [...part]));
    expect(chunked.length).toBe(whole.length);
    expect(Math.max(...chunked.map((value, index) => Math.abs(value - whole[index])))).toBeLessThan(1e-6);
  });

  it("supports Safari-style 44.1 kHz output without unbounded sample drift", () => {
    const resampler = new StreamingLinearResampler(24_000, 44_100);
    const output = resampler.process(new Float32Array(24_000));
    expect(Math.abs(output.length - 44_100)).toBeLessThanOrEqual(2);
  });

  it("rejects invalid rates and copies equal-rate input", () => {
    expect(() => new StreamingLinearResampler(0, 48_000)).toThrow("采样率");
    const input = Float32Array.of(0.1, -0.2);
    const output = new StreamingLinearResampler(24_000, 24_000).process(input);
    expect([...output]).toEqual([...input]);
    expect(output).not.toBe(input);
  });
});
