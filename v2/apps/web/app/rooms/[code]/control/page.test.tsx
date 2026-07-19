import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";

import ControlPage from "@/app/rooms/[code]/control/page";
import type { Room } from "@/lib/types";

const mocks = vi.hoisted(() => ({
  room: null as Room | null,
  replace: vi.fn(),
  setRoom: vi.fn(),
  apiFetch: vi.fn(),
}));

const runningRoom = {
  id: "room-id",
  code: "123456",
  topic: "控制台在断线时是否仍能展示操作结果？",
  status: "running",
  visibility: "private",
  seq: 1,
  competition: {
    id: "competition",
    slug: "training-1v1",
    name: "1v1 辩论训练赛",
    tagline: "",
    description: "",
    rules: "",
    format: "1v1",
    seat_count: 2,
    ranked: false,
    allow_custom_topic: true,
    accent: "blue",
    live_count: 1,
  },
  season: null,
  owner: { id: "admin", real_name: "管理员" },
  seats: [],
  current_stage: { key: "stage", name: "正方立论", kind: "speech", duration: 120, seat: "aff_1" },
  current_stage_index: 0,
  remaining_seconds: 90,
  turn_remaining_seconds: null,
  active_speech: null,
  my_seat: null,
  can_speak: false,
  speak_reason: "等待轮次",
  can_control: true,
  failure_reason: "",
  recent_events: [],
  speeches: [],
} as Room;

vi.mock("next/navigation", () => ({
  useParams: () => ({ code: "123456" }),
  useRouter: () => ({ replace: mocks.replace }),
}));
vi.mock("@/lib/use-session", () => ({
  useSession: () => ({ user: { id: "admin", role: "system_admin" } }),
}));
vi.mock("@/lib/use-room", () => ({
  useCountdown: (value: number | null) => value,
  useRoom: () => ({
    room: mocks.room || runningRoom,
    setRoom: mocks.setRoom,
    connected: false,
    error: "实时连接已断开，正在重新连接…",
    refresh: vi.fn(),
  }),
}));
vi.mock("@/lib/api", () => ({ apiFetch: mocks.apiFetch }));

describe("room control console", () => {
  afterEach(() => {
    mocks.room = null;
    mocks.apiFetch.mockReset();
    mocks.setRoom.mockReset();
    mocks.replace.mockReset();
  });

  it("applies the REST response while WebSocket is unavailable and has no permanently disabled retry control", async () => {
    const paused = { ...runningRoom, status: "paused", seq: 2, remaining_seconds: 88 } as Room;
    mocks.apiFetch.mockResolvedValueOnce({ room: paused });
    const { container } = render(<ControlPage />);
    expect(screen.getByRole("alert")).toHaveTextContent("实时连接已断开");
    expect(screen.getByText(/FunASR \/ MOSS 实时语音/)).toBeInTheDocument();
    expect(screen.queryByText(/LightTTS/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试当前步骤" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "暂停比赛" }));
    await waitFor(() => expect(mocks.apiFetch).toHaveBeenCalledOnce());
    const updater = mocks.setRoom.mock.calls[0][0] as (current: Room) => Room;
    expect(updater(runningRoom)).toEqual(paused);
    const accessibility = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(accessibility.violations).toEqual([]);
  });

  it("lets the room owner approve an explicit restoration request after speech ends", async () => {
    const substitutedSeat = {
      seat_key: "aff_1",
      side: "aff" as const,
      position: 1,
      label: "正方一辩",
      occupant_type: "ai_substitute" as const,
      display_name: "AI 接替·张三",
      is_ready: true,
      connected: false,
      is_me: false,
    };
    const approved = { ...runningRoom, seq: 2, seats: [{ ...substitutedSeat, occupant_type: "human" as const }] } as Room;
    mocks.apiFetch.mockResolvedValueOnce({ room: approved });
    mocks.room = {
      ...runningRoom,
      seats: [substitutedSeat],
      seat_restore_requests: [{
        id: "request-1", seat_key: "aff_1", seat_label: "正方一辩",
        requester: { id: "student", real_name: "张三" }, status: "pending",
        requester_connected: false,
        resolution_reason: "", created_at: "2026-07-19T08:00:00Z", resolved_at: null,
        can_cancel: false, can_review: true,
      }],
    };
    render(<ControlPage />);
    expect(screen.queryByText("等待原辩手发起申请")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "批准" }));
    await waitFor(() => expect(mocks.apiFetch).toHaveBeenCalledWith(
      "/api/rooms/123456/seat-restore-requests/request-1/approve",
      expect.objectContaining({ method: "POST" }),
    ));
    expect(mocks.setRoom).toHaveBeenCalledOnce();
  });
});
