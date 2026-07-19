import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import LobbyPage from "@/app/rooms/[code]/lobby/page";
import type { Room } from "@/lib/types";

const mocks = vi.hoisted(() => ({
  room: null as Room | null,
  replace: vi.fn(),
  push: vi.fn(),
  setRoom: vi.fn(),
  reconnect: vi.fn(),
  refresh: vi.fn(),
  apiFetch: vi.fn(),
  user: { id: "user" } as { id: string } | null,
}));

vi.mock("next/navigation", () => ({
  useParams: () => ({ code: "123456" }),
  useRouter: () => ({ push: mocks.push, replace: mocks.replace }),
}));
vi.mock("@/lib/use-room", () => ({
  useRoom: () => ({
    room: mocks.room,
    setRoom: mocks.setRoom,
    connected: true,
    error: "",
    reconnect: mocks.reconnect,
    refresh: mocks.refresh,
  }),
}));
vi.mock("@/lib/api", () => ({ apiFetch: mocks.apiFetch }));
vi.mock("@/lib/use-session", () => ({ useSession: () => ({ user: mocks.user, loading: false }) }));

function lobbyRoom(overrides: Partial<Room> = {}): Room {
  return {
    id: "room-id",
    code: "123456",
    topic: "赛季关闭后是否还应允许新辩手加入？",
    status: "lobby",
    visibility: "public",
    seq: 3,
    competition: {
      id: "competition-id",
      slug: "daily-4v4",
      name: "4v4 人机辩论日常赛",
      tagline: "",
      description: "",
      rules: "",
      format: "4v4",
      seat_count: 8,
      ranked: true,
      allow_custom_topic: false,
      accent: "violet",
      live_count: 0,
    },
    season: {
      id: "season-id",
      name: "已结束赛季",
      slug: "closed-season",
      starts_at: "2026-01-01T00:00:00Z",
      ends_at: "2026-01-31T00:00:00Z",
      is_active: false,
      is_open: false,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-02-01T00:00:00Z",
    },
    owner: { id: "owner", real_name: "房主" },
    seats: [
      { seat_key: "aff_1", side: "aff", position: 1, label: "正方一辩", occupant_type: "human", display_name: "房主", is_ready: false, connected: true, is_me: true },
      { seat_key: "neg_1", side: "neg", position: 1, label: "反方一辩", occupant_type: "open", display_name: "待加入", is_ready: false, connected: false, is_me: false },
    ],
    current_stage: null,
    current_stage_index: -1,
    remaining_seconds: null,
    turn_remaining_seconds: null,
    active_speech: null,
    my_seat: "aff_1",
    can_speak: false,
    speak_reason: "比赛尚未开始",
    can_control: true,
    recent_events: [],
    speeches: [],
    ...overrides,
  };
}

describe("room lobby", () => {
  beforeEach(() => {
    mocks.apiFetch.mockResolvedValue({ seq: 3, lease_fingerprint: "current-device" });
  });

  afterEach(() => {
    mocks.room = null;
    mocks.replace.mockReset();
    mocks.push.mockReset();
    mocks.setRoom.mockReset();
    mocks.reconnect.mockReset();
    mocks.refresh.mockReset();
    mocks.apiFetch.mockReset();
    mocks.user = { id: "user" };
  });

  it.each([
    ["preparing", "/rooms/123456/debate"],
    ["running", "/rooms/123456/debate"],
    ["completed", "/rooms/123456/result"],
    ["review_required", "/rooms/123456/result"],
    ["terminated", "/rooms/123456/result"],
    ["cancelled", "/?room_closed=123456"],
  ])("redirects a %s room without flashing stale lobby controls", async (status, destination) => {
    mocks.room = lobbyRoom({ status });
    render(<LobbyPage />);
    expect(screen.getByText("正在返回当前比赛…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /确认准备|锁定席位并开始/ })).not.toBeInTheDocument();
    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith(destination));
  });

  it("prevents joining and becoming ready after the ranked season closes", async () => {
    mocks.room = lobbyRoom();
    const { container } = render(<LobbyPage />);
    expect(screen.getByText(/该房间仍可观战、释放席位或关闭/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /待加入.*反方一辩.*赛季已关闭/ })).toBeDisabled();
    for (const button of screen.getAllByRole("button", { name: "赛季已关闭" })) expect(button).toBeDisabled();
    const accessibility = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(accessibility.violations).toEqual([]);
  });

  it("does not expose recording policy UI or use consent state as a ready gate", async () => {
    mocks.room = lobbyRoom({
      competition: { ...lobbyRoom().competition, ranked: false },
      season: null,
      recording_consent: {
        organization_id: "org-1",
        required: true,
        granted: false,
        policy: { id: "policy-1", version: 3, title: "赛事录音说明", content: "请阅读完整说明后决定是否同意。" },
        decision: null,
        decided_at: null,
        actor_user_id: null,
        record_id: null,
      },
    });
    render(<LobbyPage />);
    expect(screen.queryByText("赛事录音说明")).not.toBeInTheDocument();
    expect(screen.queryByText("录音知情同意")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "确认准备" })).toBeEnabled());
  });

  it("offers an optional microphone preflight without making it a ready gate", async () => {
    mocks.room = lobbyRoom({
      competition: { ...lobbyRoom().competition, ranked: false },
      season: null,
    });
    render(<LobbyPage />);
    expect(screen.getByRole("heading", { name: "赛前麦克风检查" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "测试默认麦克风" })).toBeEnabled();
    expect(screen.getByText(/不是确认准备的硬门禁/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "确认准备" })).toBeEnabled());
  });

  it("returns an anonymous invitee to the same lobby after authentication", () => {
    mocks.user = null;
    mocks.room = lobbyRoom({
      competition: { ...lobbyRoom().competition, ranked: false },
      season: null,
      seats: lobbyRoom().seats.map((seat) => ({ ...seat, occupant_type: seat.seat_key === "aff_1" ? "open" : seat.occupant_type, is_me: false })),
      my_seat: null,
      can_control: false,
    });
    render(<LobbyPage />);
    const openSeat = screen.getByRole("button", { name: /正方一辩.*登录或注册后认领/ });
    fireEvent.click(openSeat);
    expect(mocks.push).toHaveBeenCalledWith("/login?next=%2Frooms%2F123456%2Flobby");
  });

  it("makes the old lobby read-only after another device takes over", async () => {
    mocks.room = lobbyRoom({
      competition: { ...lobbyRoom().competition, ranked: false },
      season: null,
    });
    const { rerender } = render(<LobbyPage />);
    await waitFor(() => expect(screen.getByRole("button", { name: "确认准备" })).toBeEnabled());

    mocks.room = lobbyRoom({
      competition: { ...lobbyRoom().competition, ranked: false },
      season: null,
      seq: 4,
      recent_events: [{
        seq: 4,
        type: "seat.control_taken_over",
        payload: { seat_key: "aff_1", lease_fingerprint: "other-device" },
        created_at: new Date().toISOString(),
      }],
    });
    rerender(<LobbyPage />);

    await waitFor(() => expect(screen.getByRole("button", { name: "确认准备" })).toBeDisabled());
    expect(screen.getByRole("alert")).toHaveTextContent("当前页面已切换为只读");
    fireEvent.click(screen.getByRole("button", { name: "确认接管到当前设备" }));
    await waitFor(() => {
      expect(mocks.apiFetch).toHaveBeenCalledWith(
        "/api/rooms/123456/control-lease",
        expect.objectContaining({ body: JSON.stringify({ force: true }) }),
      );
    });
  });
});
