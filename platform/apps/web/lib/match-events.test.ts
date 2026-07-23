import { describe, expect, it } from "vitest";

import { matchEventDetail, matchEventLabel } from "@/lib/match-events";

describe("match event presentation", () => {
  it("renders stage objects and seat keys as readable Chinese", () => {
    expect(matchEventLabel("stage.started")).toBe("阶段开始");
    expect(matchEventDetail({
      seq: 1,
      type: "stage.started",
      payload: { stage: { key: "aff_case", name: "正方一辩立论" } },
      created_at: "2026-01-01T00:00:00Z",
    })).toBe("正方一辩立论");
    expect(matchEventDetail({
      seq: 2,
      type: "speech.started",
      payload: { seat_key: "neg_2" },
      created_at: "2026-01-01T00:00:00Z",
    })).toBe("反方2辩");
  });

  it("describes service failures, side changes and score corrections", () => {
    expect(matchEventLabel("seat.control_taken_over")).toBe("辩手设备控制权已接管");
    expect(matchEventLabel("judge.interrupted")).toBe("自动裁判已中断");
    expect(matchEventDetail({
      seq: 3,
      type: "provider.failed",
      payload: { message: "LightTTS 暂时不可用" },
      created_at: "2026-01-01T00:00:00Z",
    })).toBe("LightTTS 暂时不可用");
    expect(matchEventDetail({
      seq: 4,
      type: "free.side_changed",
      payload: { side: "neg" },
      created_at: "2026-01-01T00:00:00Z",
    })).toBe("反方获得发言权");
    expect(matchEventDetail({
      seq: 5,
      type: "judge.corrected",
      payload: { old: { winner: "aff" }, new: { winner: "draw" } },
      created_at: "2026-01-01T00:00:00Z",
    })).toBe("正方胜 → 平局");
    expect(matchEventLabel("engine.quarantined")).toBe("状态机异常暂停");
    expect(matchEventDetail({
      seq: 6,
      type: "engine.quarantined",
      payload: { attempts: 3 },
      created_at: "2026-01-01T00:00:00Z",
    })).toBe("连续 3 次异常，已隔离本房间");
  });

  it("localizes realtime audio events and never leaks unknown internal enums", () => {
    expect(matchEventLabel("audio.rtc.started")).toBe("实时语音播放开始");
    expect(matchEventLabel("audio.stream.aborted")).toBe("语音流已取消");
    expect(matchEventLabel("provider.retrying")).toBe("外部服务正在重试");
    expect(matchEventLabel("speech.future_internal_event")).toBe("发言状态更新");
    expect(matchEventLabel("unexpected.internal_event")).toBe("系统状态更新");
  });
});
