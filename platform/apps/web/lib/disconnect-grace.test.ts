import { describe, expect, it } from "vitest";

import {
  disconnectGraceRemainingSeconds,
  disconnectGraceTimingLabel,
} from "@/lib/disconnect-grace";
import type { Room } from "@/lib/types";

describe("disconnect grace projection", () => {
  it("uses the earliest participant deadline without exposing a different seat", () => {
    const room = {
      disconnect_grace: {
        will_pause: true,
        pending: [
          { seat_key: "aff_1", display_name: "甲", remaining_seconds: 42 },
          { seat_key: "neg_2", display_name: "乙", remaining_seconds: 17 },
        ],
      },
    } as Room;

    expect(disconnectGraceRemainingSeconds(room)).toBe(17);
  });

  it("supports the anonymous aggregate projection and clamps expired values", () => {
    expect(disconnectGraceRemainingSeconds({
      disconnect_grace: {
        will_pause: true,
        pending_count: 2,
        minimum_remaining_seconds: -3,
      },
    } as Room)).toBe(0);
  });

  it("uses actionable timing labels instead of displaying a misleading zero", () => {
    expect(disconnectGraceTimingLabel(null)).toBe("60 秒内自动暂停");
    expect(disconnectGraceTimingLabel(23)).toBe("23 秒后自动暂停");
    expect(disconnectGraceTimingLabel(0)).toBe("正在自动暂停");
  });
});
