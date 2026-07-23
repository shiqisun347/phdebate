import { describe, expect, it } from "vitest";

import { compactCaptionLine } from "@/lib/caption-line";

describe("compactCaptionLine", () => {
  it("shows only the newest clause from a cumulative ASR hypothesis", () => {
    expect(compactCaptionLine("第一句已经确认。第二句正在实时识别"))
      .toBe("第二句正在实时识别");
  });

  it("collapses provider newlines and bounds a long current clause", () => {
    const result = compactCaptionLine("这是一个非常长的实时识别片段，它仍在持续增加并且不应该把整段文字铺满比赛舞台", 16);
    expect(Array.from(result)).toHaveLength(16);
    expect(result.startsWith("…")).toBe(true);
    expect(result).not.toContain("\n");
  });

  it("joins a tiny trailing fragment to its preceding clause", () => {
    expect(compactCaptionLine("论证已经完成，但是。好。"))
      .toBe("但是。好。");
    expect(compactCaptionLine("论证已经完成，了。"))
      .toBe("论证已经完成，了。");
  });

  it("returns an empty line for whitespace-only updates", () => {
    expect(compactCaptionLine("  \n ")).toBe("");
  });
});
