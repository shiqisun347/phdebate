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
    expect(screen.getByRole("heading", { level: 1, name: "房间 #123456 控制台" })).toBeInTheDocument();
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

  it("explains the read-only transition instead of flashing controls to a non-owner", async () => {
    mocks.room = { ...runningRoom, can_control: false } as Room;

    render(<ControlPage />);

    expect(screen.getByText("你没有本房间控制权限，正在切换到只读观战…")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "应急控制" })).not.toBeInTheDocument();
    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/rooms/123456/watch?notice=no-control"));
  });

  it("does not skip or pause a stage while a human is still speaking", () => {
    mocks.room = {
      ...runningRoom,
      active_speech: {
        id: "speech-human",
        seat_key: "aff_1",
        speaker_type: "human",
        status: "speaking",
        content: "尚未结束的真人发言",
      },
    } as Room;

    render(<ControlPage />);

    expect(screen.getByRole("button", { name: "真人发言中" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "真人发言中，不可跳过" })).toBeDisabled();
    expect(mocks.apiFetch).not.toHaveBeenCalled();
  });

  it("requires confirmation before skipping an irreversible stage", async () => {
    const confirmMock = vi.fn(() => false);
    vi.stubGlobal("confirm", confirmMock);
    render(<ControlPage />);

    fireEvent.click(screen.getByRole("button", { name: "跳过当前阶段" }));
    expect(confirmMock).toHaveBeenCalledWith(expect.stringContaining("正方立论"));
    expect(mocks.apiFetch).not.toHaveBeenCalled();

    confirmMock.mockReturnValue(true);
    mocks.apiFetch.mockResolvedValueOnce({ room: { ...runningRoom, seq: 2 } });
    fireEvent.click(screen.getByRole("button", { name: "跳过当前阶段" }));
    await waitFor(() => expect(mocks.apiFetch).toHaveBeenCalledWith(
      "/api/rooms/123456/control/skip",
      expect.objectContaining({ method: "POST" }),
    ));
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
        requester_connected: true,
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

  it("does not approve a seat restoration until the returning debater is connected", () => {
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
    mocks.room = {
      ...runningRoom,
      seats: [substitutedSeat],
      seat_restore_requests: [{
        id: "request-offline", seat_key: "aff_1", seat_label: "正方一辩",
        requester: { id: "student", real_name: "张三" }, status: "pending",
        requester_connected: false,
        resolution_reason: "", created_at: "2026-07-19T08:00:00Z", resolved_at: null,
        can_cancel: false, can_review: true,
      }],
    };

    render(<ControlPage />);

    expect(screen.getByRole("button", { name: "等待辩手连接" })).toBeDisabled();
    expect(screen.getByText(/尚未打开比赛页/)).toBeInTheDocument();
    expect(mocks.apiFetch).not.toHaveBeenCalled();
  });

  it("lets a system administrator directly restore a connected AI-substituted seat", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const substitutedSeat = {
      seat_key: "aff_1", side: "aff" as const, position: 1, label: "正方一辩",
      occupant_type: "ai_substitute" as const, display_name: "AI 接替·张三",
      is_ready: true, connected: true, is_me: false,
    };
    const restored = {
      ...runningRoom,
      seq: 2,
      can_admin_restore: true,
      seats: [{ ...substitutedSeat, occupant_type: "human" as const, display_name: "张三" }],
    } as Room;
    mocks.room = { ...runningRoom, can_admin_restore: true, seats: [substitutedSeat] } as Room;
    mocks.apiFetch.mockResolvedValueOnce({ room: restored });

    render(<ControlPage />);
    fireEvent.click(screen.getByRole("button", { name: "直接恢复真人" }));

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("张三"));
    await waitFor(() => expect(mocks.apiFetch).toHaveBeenCalledWith(
      "/api/admin/rooms/123456/seats/aff_1/restore",
      { method: "POST", body: "{}" },
    ));
    expect(mocks.setRoom).toHaveBeenCalledOnce();
    expect(screen.getByRole("status")).toHaveTextContent("张三 已恢复为真人辩手");
  });

  it("safely transfers room recovery control to a connected human participant", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const ownerSeat = {
      seat_key: "aff_1", side: "aff" as const, position: 1, label: "正方一辩",
      occupant_type: "human" as const, display_name: "原房主", is_ready: true,
      connected: true, is_me: true, is_owner: true,
    };
    const successorSeat = {
      seat_key: "neg_1", side: "neg" as const, position: 1, label: "反方一辩",
      occupant_type: "human" as const, display_name: "接任辩手", is_ready: true,
      connected: true, is_me: false, is_owner: false,
    };
    const offlineSeat = {
      ...successorSeat, seat_key: "neg_2", position: 2, label: "反方二辩",
      display_name: "离线辩手", connected: false,
    };
    const transferred = {
      ...runningRoom,
      seq: 2,
      owner: { id: "successor", real_name: "接任辩手" },
      can_control: false,
      seats: [
        { ...ownerSeat, is_owner: false },
        { ...successorSeat, is_owner: true },
        offlineSeat,
      ],
    } as Room;
    mocks.room = { ...runningRoom, seats: [ownerSeat, successorSeat, offlineSeat] } as Room;
    mocks.apiFetch.mockResolvedValueOnce({ room: transferred });

    render(<ControlPage />);
    expect(screen.getByText(/需要离开时，应先把异常恢复权限/)).toBeInTheDocument();
    expect(screen.getByText("接任辩手")).toBeInTheDocument();
    expect(screen.queryByText("离线辩手")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "移交房主" }));

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("接任辩手（反方一辩）"));
    await waitFor(() => expect(mocks.apiFetch).toHaveBeenCalledWith(
      "/api/rooms/123456/transfer-owner",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ seat_key: "neg_1" }),
        headers: expect.objectContaining({ "X-Idempotency-Key": expect.any(String) }),
      }),
    ));
    expect(mocks.setRoom).toHaveBeenCalledOnce();
    expect(screen.getByRole("status")).toHaveTextContent(/房主控制权已移交给\s*接任辩手/);
  });

  it("lets the owner hand their own human seat to AI without touching the debate player", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const ownerSeat = {
      seat_key: "aff_1", side: "aff" as const, position: 1, label: "正方一辩",
      occupant_type: "human" as const, display_name: "原房主", is_ready: true,
      connected: true, is_me: true, is_owner: true,
    };
    const abandoned = {
      ...runningRoom,
      seq: 2,
      seats: [{ ...ownerSeat, occupant_type: "ai_substitute", display_name: "AI 接替·原房主" }],
    } as Room;
    mocks.room = { ...runningRoom, seats: [ownerSeat] } as Room;
    mocks.apiFetch.mockResolvedValueOnce({ room: abandoned });

    render(<ControlPage />);
    fireEvent.click(screen.getByRole("button", { name: "让 AI 接替我的席位" }));

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("若有其他在线真人，房主控制权会自动移交"));
    await waitFor(() => expect(mocks.apiFetch).toHaveBeenCalledWith(
      "/api/rooms/123456/abandon-seat",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({}),
        headers: expect.objectContaining({ "X-Idempotency-Key": expect.any(String) }),
      }),
    ));
    expect(mocks.setRoom).toHaveBeenCalledOnce();
  });

  it("disables seat abandonment and ownership transfer after the match is terminal", () => {
    const ownerSeat = {
      seat_key: "aff_1", side: "aff" as const, position: 1, label: "正方一辩",
      occupant_type: "human" as const, display_name: "原房主", is_ready: true,
      connected: true, is_me: true, is_owner: true,
    };
    const successorSeat = {
      seat_key: "neg_1", side: "neg" as const, position: 1, label: "反方一辩",
      occupant_type: "human" as const, display_name: "接任辩手", is_ready: true,
      connected: true, is_me: false, is_owner: false,
    };
    mocks.room = {
      ...runningRoom,
      status: "terminated",
      seats: [ownerSeat, successorSeat],
    } as Room;

    render(<ControlPage />);

    expect(screen.getAllByRole("button", { name: "比赛已结束" })).toHaveLength(2);
    for (const button of screen.getAllByRole("button", { name: "比赛已结束" })) {
      expect(button).toBeDisabled();
    }
    expect(mocks.apiFetch).not.toHaveBeenCalled();
  });
});
