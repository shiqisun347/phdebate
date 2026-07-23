import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import DebatePage from "@/app/rooms/[code]/debate/page";
import type { Room } from "@/lib/types";

const mocks = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn(), room: null as Room | null }));

vi.mock("next/navigation", () => ({
  useParams: () => ({ code: "123456" }),
  useRouter: () => ({ push: mocks.push, replace: mocks.replace }),
}));
vi.mock("@/lib/use-room", () => ({
  useRoom: () => ({
    room: mocks.room,
    connected: true,
    error: "",
    reconnect: vi.fn(),
    liveEvent: null,
  }),
}));
vi.mock("@/components/debate-stage", () => ({
  DebateStage: ({ onPendingFinishChange }: { onPendingFinishChange?: (pending: boolean) => void }) => (
    <div className="stage-page">
      <aside className="team-column aff">
        <div className="team-title">正方</div>
        <div className="stage-seat active"><span className="sr-only">当前发言席位</span></div>
      </aside>
      <div className="subtitle-stage"><p>比赛舞台</p></div>
      <button type="button" onClick={() => onPendingFinishChange?.(true)}>保留录音</button>
      <button type="button" onClick={() => onPendingFinishChange?.(false)}>提交完成</button>
    </div>
  ),
}));

const runningRoom = {
  id: "room",
  code: "123456",
  status: "running",
  seq: 1,
  my_seat: "aff_1",
  seats: [{ seat_key: "aff_1", is_me: true, occupant_type: "human" }],
} as unknown as Room;

describe("debate result redirect", () => {
  beforeEach(() => {
    mocks.push.mockReset();
    mocks.replace.mockReset();
    mocks.room = runningRoom;
  });

  it("keeps a locally retained recording on the debate page until submission succeeds", async () => {
    const { rerender } = render(<DebatePage />);
    fireEvent.click(screen.getByRole("button", { name: "保留录音" }));
    mocks.room = { ...runningRoom, status: "completed", seq: 2 } as Room;
    rerender(<DebatePage />);
    expect(mocks.push).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "提交完成" }));
    await waitFor(() => expect(mocks.push).toHaveBeenCalledWith("/rooms/123456/result"));
  });

  it("applies free-debate seat semantics on the participant debate page", () => {
    mocks.room = {
      ...runningRoom,
      current_stage: { key: "free", name: "自由辩论", kind: "free", duration: 300, side: "aff" },
      active_speech: null,
    } as Room;
    render(<DebatePage />);
    expect(screen.getByText("本轮可发言席位")).toBeInTheDocument();
    expect(screen.getByText(/当前可发言阵营/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "自由辩论举手队列" })).toBeInTheDocument();
  });

  it.each([
    ["lobby", "/rooms/123456/lobby"],
    ["cancelled", "/?room_closed=123456"],
  ])("returns a %s room to its canonical page", async (status, destination) => {
    mocks.room = { ...runningRoom, status } as Room;
    render(<DebatePage />);
    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith(destination));
    expect(screen.queryByRole("button", { name: "保留录音" })).not.toBeInTheDocument();
  });

  it("redirects an anonymous or non-participant visitor to the read-only watch page", async () => {
    mocks.room = { ...runningRoom, my_seat: null } as Room;
    render(<DebatePage />);
    await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/rooms/123456/watch"));
    expect(screen.queryByRole("button", { name: "保留录音" })).not.toBeInTheDocument();
  });

});
