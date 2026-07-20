import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SeatRestorePanel } from "@/components/seat-restore-panel";
import type { Room } from "@/lib/types";

const baseRoom = {
  code: "123456",
  seq: 4,
  seats: [{
    seat_key: "aff_1", side: "aff", position: 1, label: "正方一辩",
    occupant_type: "ai_substitute", display_name: "AI 接替·张三",
    is_ready: true, connected: true, is_me: true,
  }],
  active_speech: null,
  seat_restore_requests: [],
} as unknown as Room;

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

describe("seat restoration panel", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("submits an idempotent request and exposes an accessible waiting state", async () => {
    const pending = {
      ...baseRoom,
      seq: 5,
      seat_restore_requests: [{
        id: "request-1", seat_key: "aff_1", seat_label: "正方一辩",
        requester: { id: "student", real_name: "张三" }, status: "pending" as const,
        requester_connected: true,
        resolution_reason: "", created_at: "2026-07-19T08:00:00Z", resolved_at: null,
        can_cancel: true, can_review: false,
      }],
    };
    const fetchMock = vi.fn().mockResolvedValue(response({ room: pending }));
    vi.stubGlobal("fetch", fetchMock);
    const changed = vi.fn();
    const view = render(<SeatRestorePanel room={baseRoom} onRoomChanged={changed} />);

    fireEvent.click(screen.getByRole("button", { name: "申请恢复真人" }));
    await waitFor(() => expect(changed).toHaveBeenCalledWith(pending));
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/rooms/123456/seat-restore-requests");
    expect(fetchMock.mock.calls[0][1]?.method).toBe("POST");
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get("X-Idempotency-Key")).toEqual(expect.any(String));
    view.rerender(<SeatRestorePanel room={pending} onRoomChanged={changed} />);
    expect(screen.getByText("恢复申请等待审批")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "撤销申请" })).toBeEnabled();
    expect((await axe.run(view.container, { rules: { "color-contrast": { enabled: false } } })).violations).toEqual([]);
  });

  it("does not render for anonymous or non-substituted viewers and defers during active speech", () => {
    const { rerender } = render(<SeatRestorePanel room={{ ...baseRoom, seats: [] }} onRoomChanged={vi.fn()} />);
    expect(screen.queryByLabelText("真人席位恢复")).not.toBeInTheDocument();
    rerender(<SeatRestorePanel room={{ ...baseRoom, active_speech: { id: "speech" } } as Room} onRoomChanged={vi.fn()} />);
    expect(screen.getByRole("button", { name: "发言结束后可申请" })).toBeDisabled();
  });

  it("gives an approved participant an explicit path back to the debate", () => {
    render(<SeatRestorePanel room={{
      ...baseRoom,
      seats: [{ ...baseRoom.seats[0], occupant_type: "human" }],
      seat_restore_requests: [{
        id: "request-approved", seat_key: "aff_1", seat_label: "正方一辩",
        requester: { id: "student", real_name: "张三" }, status: "approved",
        requester_connected: true,
        resolution_reason: "已批准", created_at: "2026-07-19T08:00:00Z", resolved_at: "2026-07-19T08:01:00Z",
        can_cancel: false, can_review: false,
      }],
    } as Room} onRoomChanged={vi.fn()} />);
    expect(screen.getByText("真人席位已恢复")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "返回比赛" })).toHaveAttribute("href", "/rooms/123456/debate");
  });
});
