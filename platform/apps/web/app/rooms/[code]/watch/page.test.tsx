import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import WatchPage from "@/app/rooms/[code]/watch/page";
import type { Room } from "@/lib/types";

const mocks = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn(), room: null as Room | null }));

vi.mock("next/navigation", () => ({
  useParams: () => ({ code: "123456" }),
  useRouter: () => ({ push: mocks.push, replace: mocks.replace }),
}));
vi.mock("@/lib/use-room", () => ({
  useRoom: () => ({ room: mocks.room, connected: true, error: "", reconnect: vi.fn(), liveEvent: null }),
}));
vi.mock("@/components/debate-stage", () => ({ DebateStage: () => (
  <div className="stage-page">
    <aside className="team-column aff">
      <div className="team-title">正方</div>
      <div className="stage-seat active"><span className="sr-only">当前发言席位</span></div>
    </aside>
    <div className="subtitle-stage"><p>观战舞台</p></div>
  </div>
) }));

describe("watch result redirect", () => {
  beforeEach(() => {
    mocks.push.mockReset();
    mocks.replace.mockReset();
    window.history.replaceState({}, "", "/rooms/123456/watch");
  });

  it("redirects a published match to its result", async () => {
    mocks.room = { id: "room", code: "123456", status: "completed", seq: 2 } as unknown as Room;
    render(<WatchPage />);
    await waitFor(() => expect(mocks.push).toHaveBeenCalledWith("/rooms/123456/result"));
  });

  it.each(["review_required", "terminated"])("keeps an unpublished %s match on the watch projection", (status) => {
    mocks.room = { id: "room", code: "123456", status, seq: 2 } as unknown as Room;
    render(<WatchPage />);
    expect(mocks.push).not.toHaveBeenCalled();
    expect(screen.getByText("观战舞台")).toBeInTheDocument();
  });

  it.each(["review_required", "terminated"])("returns a participant from an unpublished %s match to its result", async (status) => {
    mocks.room = {
      id: "room",
      code: "123456",
      status,
      seq: 2,
      my_seat: "aff_1",
      can_control: false,
    } as unknown as Room;
    render(<WatchPage />);
    await waitFor(() => expect(mocks.push).toHaveBeenCalledWith("/rooms/123456/result"));
  });

  it("keeps an active match on the watch stage", () => {
    mocks.room = { id: "room", code: "123456", status: "running", seq: 1 } as unknown as Room;
    render(<WatchPage />);
    expect(mocks.push).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "文字记录" })).not.toBeInTheDocument();
  });

  it("tells a redirected non-owner why the control console became read-only", async () => {
    window.history.replaceState({}, "", "/rooms/123456/watch?notice=no-control");
    mocks.room = { id: "room", code: "123456", status: "running", seq: 1 } as unknown as Room;
    render(<WatchPage />);

    expect(await screen.findByText(/只有房主或系统管理员可以进入本房间控制台/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "知道了" }));
    expect(screen.queryByText(/只有房主或系统管理员可以进入本房间控制台/)).not.toBeInTheDocument();
    expect(window.location.search).toBe("");
  });

  it("applies free-debate seat semantics on the public watch page", () => {
    mocks.room = {
      id: "room",
      code: "123456",
      status: "running",
      seq: 1,
      current_stage: { key: "free", name: "自由辩论", kind: "free", duration: 300, side: "aff" },
      active_speech: null,
    } as unknown as Room;
    render(<WatchPage />);
    expect(screen.getByText("本轮可发言席位")).toBeInTheDocument();
    expect(screen.getByText(/当前可发言阵营/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "自由辩论举手队列" })).toBeInTheDocument();
    expect(screen.getByText("观战模式仅展示队列，不能申请发言。")).toBeInTheDocument();
  });

  it("returns a cancelled room to the public lobby", async () => {
    mocks.room = { id: "room", code: "123456", status: "cancelled", seq: 2 } as unknown as Room;
    render(<WatchPage />);
    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/?room_closed=123456"));
  });
});
