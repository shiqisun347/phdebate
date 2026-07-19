import { render, screen, waitFor } from "@testing-library/react";
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
  });

  it.each(["completed", "review_required", "terminated"])("redirects a %s match to its result", async (status) => {
    mocks.room = { id: "room", code: "123456", status, seq: 2 } as unknown as Room;
    render(<WatchPage />);
    await waitFor(() => expect(mocks.push).toHaveBeenCalledWith("/rooms/123456/result"));
  });

  it("keeps an active match on the watch stage", () => {
    mocks.room = { id: "room", code: "123456", status: "running", seq: 1 } as unknown as Room;
    render(<WatchPage />);
    expect(mocks.push).not.toHaveBeenCalled();
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
  });

  it("returns a cancelled room to the public lobby", async () => {
    mocks.room = { id: "room", code: "123456", status: "cancelled", seq: 2 } as unknown as Room;
    render(<WatchPage />);
    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/?room_closed=123456"));
  });
});
