import { render, screen } from "@testing-library/react";
import axe from "axe-core";
import { describe, expect, it } from "vitest";

import { StageAnnouncement } from "@/components/stage-announcement";
import type { Room } from "@/lib/types";

const room = {
  status: "running",
  current_stage: { key: "neg_2", name: "反方二辩驳论", kind: "speech", duration: 120, seat: "neg_2" },
  active_speech: { id: "speech", seat_key: "neg_2", speaker_type: "human", status: "speaking", content: "" },
  seats: [{
    seat_key: "neg_2", side: "neg", position: 2, label: "反方二辩",
    occupant_type: "human", display_name: "孙诗奇", is_ready: true, connected: true,
  }],
} as Room;

describe("StageAnnouncement", () => {
  it("announces stage and speaker changes without exposing the full transcript", async () => {
    const { container, rerender } = render(<StageAnnouncement room={room} />);

    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveAttribute("aria-atomic", "true");
    expect(status).toHaveTextContent("比赛进行中。当前环节：反方二辩驳论。当前发言：孙诗奇。");
    expect(status).not.toHaveTextContent("完整发言内容");

    rerender(<StageAnnouncement room={{
      ...room,
      current_stage: { key: "free", name: "自由辩论", kind: "free", duration: 240, side: "aff" },
      active_speech: null,
    } as Room} />);
    expect(status).toHaveTextContent("比赛进行中。当前环节：自由辩论。自由辩论当前轮到正方。");
    expect((await axe.run(container, { rules: { "color-contrast": { enabled: false } } })).violations).toEqual([]);
  });

  it("announces one consistent state when a service failure pauses before the first stage", () => {
    render(<StageAnnouncement room={{
      ...room,
      status: "paused",
      current_stage: null,
      active_speech: null,
      failure_reason: "private provider detail",
    } as Room} />);

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("服务异常暂停。当前环节：比赛已安全暂停。");
    expect(status).not.toHaveTextContent("等待比赛开始");
    expect(status).not.toHaveTextContent("private provider detail");
  });

  it("announces a participant disconnect as a human pause instead of a service failure", () => {
    render(<StageAnnouncement room={{
      ...room,
      status: "paused",
      active_speech: null,
      failure_reason: "participant_disconnected:neg_2",
      pause_health: {
        paused_at: "2026-07-22T16:00:00Z",
        paused_duration_seconds: 4,
        is_stale: false,
        capacity_consuming: false,
        reason_code: "participant_disconnected",
        recommended_action: "resume",
        can_terminate_to_release_capacity: false,
      },
    } as Room} />);

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("真人断线暂停。当前环节：反方二辩驳论，等待真人重新连接。");
    expect(status).not.toHaveTextContent("服务异常");
    expect(status).not.toHaveTextContent("participant_disconnected");
  });
});
