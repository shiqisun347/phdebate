import { describe, expect, it } from "vitest";

import { providerStatusLabel, ratingReasonLabel, resultStatusLabel, roomStatusLabel } from "@/lib/status-labels";

describe("business status labels", () => {
  it("does not expose internal room, result or provider status codes", () => {
    expect(roomStatusLabel.running).toBe("比赛进行中");
    expect(roomStatusLabel.review_required).toBe("等待人工复核");
    expect(resultStatusLabel.approved).toBe("赛果已确认");
    expect(providerStatusLabel.unreachable).toBe("服务不可达");
  });

  it("describes initial and compensating rating changes in Chinese", () => {
    expect(ratingReasonLabel("win")).toBe("获胜结算");
    expect(ratingReasonLabel("correction:loss->win")).toBe("赛果修正：落败结算 → 获胜结算");
    expect(ratingReasonLabel("")).toBe("积分结算");
  });
});
