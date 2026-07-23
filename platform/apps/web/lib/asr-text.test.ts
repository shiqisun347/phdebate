import { describe, expect, it } from "vitest";

import { mergeAsrText } from "@/lib/asr-text";

describe("mergeAsrText", () => {
  it("uses a cumulative reconnect hypothesis without duplicating its prefix", () => {
    expect(mergeAsrText("第一句已经确认。", "第一句已经确认。第二句正在识别"))
      .toBe("第一句已经确认。第二句正在识别");
  });

  it("joins an incremental reconnect result through the longest overlap", () => {
    expect(mergeAsrText("第一句已经确认。第二句", "第二句继续完成。"))
      .toBe("第一句已经确认。第二句继续完成。");
  });

  it("keeps new text when a repeated character is only coincidental", () => {
    expect(mergeAsrText("我们支持创新", "新观点需要证据"))
      .toBe("我们支持创新新观点需要证据");
  });

  it("does not duplicate an already committed update", () => {
    expect(mergeAsrText("完整发言。", "完整发言。"))
      .toBe("完整发言。");
  });
});
