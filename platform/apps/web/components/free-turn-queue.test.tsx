import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FreeTurnQueue, FreeTurnSeatQueueAdapter } from "@/components/free-turn-queue";
import { apiFetch } from "@/lib/api";
import type { Room } from "@/lib/types";

vi.mock("@/lib/api", () => ({ apiFetch: vi.fn() }));

function room(overrides: Partial<Room> = {}): Room {
  return {
    id: "room",
    code: "123456",
    status: "running",
    seq: 3,
    current_stage: { key: "free", name: "自由辩论", kind: "free", duration: 300, side: "aff" },
    active_speech: { id: "speech", seat_key: "aff_1", speaker_type: "human", status: "speaking", content: "" },
    seats: [
      { seat_key: "aff_1", side: "aff", position: 1, label: "正方一辩", occupant_type: "human", display_name: "张同学", is_ready: true, connected: true, is_me: false },
      { seat_key: "neg_1", side: "neg", position: 1, label: "反方一辩", occupant_type: "human", display_name: "李同学", is_ready: true, connected: true, is_me: true },
      { seat_key: "neg_2", side: "neg", position: 2, label: "反方二辩", occupant_type: "human", display_name: "王同学", is_ready: true, connected: true, is_me: false },
    ],
    free_turn_queue: {
      items: [
        { request_id: "request-mine", side: "neg", seat_key: "neg_1", order: 1, requested_at: "2026-07-21T01:02:03Z", is_me: true },
        { side: "neg", seat_key: "neg_2", order: 2, requested_at: "2026-07-21T01:02:04Z", is_me: false },
      ],
      window_deadline_at: new Date(Date.now() + 3000).toISOString(),
      window_remaining_ms: 3000,
      my_request: { request_id: "request-mine", side: "neg", seat_key: "neg_1", order: 1, requested_at: "2026-07-21T01:02:03Z", is_me: true },
      target_side: "neg",
      target_turn_seq: 2,
      can_request: false,
      request_reason: "已申请",
    },
    ...overrides,
  } as Room;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("FreeTurnQueue", () => {
  it("renders only in free debate and keeps watch mode read-only", async () => {
    const view = render(<FreeTurnQueue room={room()} interactive={false} />);
    expect(screen.getByRole("heading", { name: "自由辩论举手队列" })).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "当前举手顺序" })).toHaveTextContent("李同学");
    expect(screen.getByText("观战模式仅展示队列，不能申请发言。")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /举手申请|取消举手/ })).not.toBeInTheDocument();
    expect((await axe.run(view.container, { rules: { "color-contrast": { enabled: false } } })).violations).toEqual([]);

    view.rerender(<FreeTurnQueue room={room({ current_stage: { key: "case", name: "立论", kind: "speech", duration: 120 } })} interactive={false} />);
    expect(screen.queryByRole("heading", { name: "自由辩论举手队列" })).not.toBeInTheDocument();
  });

  it.each(["completed", "review_required", "terminated", "cancelled"])(
    "removes stale free-debate actions and seat badges when the room is %s",
    (status) => {
      const terminalRoom = room({ status });
      const view = render(
        <>
          <div className="stage-page">
            <aside className="team-column neg">
              <div className="stage-seat">反方一辩</div>
              <div className="stage-seat">反方二辩</div>
            </aside>
          </div>
          <FreeTurnSeatQueueAdapter room={terminalRoom} />
          <FreeTurnQueue room={terminalRoom} interactive={false} />
        </>,
      );

      expect(screen.queryByRole("heading", { name: "自由辩论举手队列" })).not.toBeInTheDocument();
      expect(view.container.querySelectorAll("[data-free-turn-seat-badge]")).toHaveLength(0);
    },
  );

  it("cancels my pending request once, uses a fresh UUID and applies the REST room snapshot", async () => {
    const updated = room({ seq: 4, free_turn_queue: { ...room().free_turn_queue!, items: [], my_request: null, can_request: true, request_reason: "可申请下一轮发言" } });
    vi.mocked(apiFetch).mockResolvedValue({ room: updated });
    const uuid = vi.spyOn(crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000001");
    const changed = vi.fn();
    render(<FreeTurnQueue room={room()} interactive onRoomChanged={changed} />);

    const cancel = screen.getByRole("button", { name: "取消举手（第 1 位）" });
    fireEvent.click(cancel);
    fireEvent.click(cancel);

    await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(1));
    expect(apiFetch).toHaveBeenCalledWith("/api/rooms/123456/free-turn-requests/request-mine/cancel", expect.objectContaining({
      method: "POST",
      headers: { "X-Idempotency-Key": "00000000-0000-4000-8000-000000000001" },
    }));
    expect(uuid).toHaveBeenCalledTimes(1);
    expect(changed).toHaveBeenCalledWith(updated);
  });

  it("keeps the speaking action visible at the bottom while queue details stay collapsible", () => {
    render(<FreeTurnQueue room={room()} interactive />);

    expect(screen.getByRole("button", { name: "取消举手（第 1 位）" })).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "当前举手顺序" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "查看举手队列" }));
    expect(screen.getByRole("list", { name: "当前举手顺序" })).toHaveTextContent("李同学");
    expect(screen.getByRole("button", { name: "收起举手队列" })).toHaveAttribute("aria-expanded", "true");
  });

  it("counts down the three-second request window from the authoritative deadline", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-21T01:00:00Z"));
    const timed = room({
      free_turn_queue: {
        ...room().free_turn_queue!,
        window_deadline_at: "2026-07-21T01:00:03Z",
      },
    });
    render(<FreeTurnQueue room={timed} interactive={false} />);
    expect(screen.getByLabelText("举手窗口剩余 3 秒")).toHaveTextContent("3.0s");

    act(() => vi.advanceTimersByTime(2200));
    expect(screen.getByLabelText("举手窗口剩余 1 秒")).toHaveTextContent("0.8s");
  });

  it("disables a stale hand action when the authoritative window reaches zero", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-21T01:00:00Z"));
    const timed = room({
      free_turn_queue: {
        ...room().free_turn_queue!,
        items: [],
        my_request: null,
        can_request: true,
        request_reason: "可申请下一轮发言",
        window_deadline_at: "2026-07-21T01:00:01Z",
      },
    });
    render(<FreeTurnQueue room={timed} interactive />);
    const request = screen.getByRole("button", { name: "举手申请下一轮" });
    expect(request).toBeEnabled();

    act(() => vi.advanceTimersByTime(1100));

    expect(request).toBeDisabled();
    expect(screen.getByText("本轮举手窗口已结束，请等待下一次申请")).toBeInTheDocument();
  });

  it("also freezes cancellation when an existing request reaches the authoritative deadline", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-21T01:00:00Z"));
    const timed = room({
      free_turn_queue: {
        ...room().free_turn_queue!,
        window_deadline_at: "2026-07-21T01:00:01Z",
      },
    });
    render(<FreeTurnQueue room={timed} interactive />);
    const cancel = screen.getByRole("button", { name: "取消举手（第 1 位）" });
    expect(cancel).toBeEnabled();

    act(() => vi.advanceTimersByTime(1100));

    expect(cancel).toBeDisabled();
    expect(screen.getByText("本轮举手窗口已结束，请等待下一次申请")).toBeInTheDocument();
    expect(apiFetch).not.toHaveBeenCalled();
  });

  it("submits a new request, explains disabled states and recovers after an error", async () => {
    const canRequest = room({
      free_turn_queue: {
        ...room().free_turn_queue!,
        items: [],
        my_request: null,
        can_request: true,
        request_reason: "可申请下一轮发言",
      },
    });
    vi.spyOn(crypto, "randomUUID")
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000002")
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000003");
    vi.mocked(apiFetch).mockRejectedValueOnce(new Error("网络波动，请重试")).mockResolvedValueOnce({ room: room({ seq: 4 }) });
    render(<FreeTurnQueue room={canRequest} interactive />);
    const request = screen.getByRole("button", { name: "举手申请下一轮" });

    fireEvent.click(request);
    expect(await screen.findByRole("alert")).toHaveTextContent("网络波动，请重试");
    expect(request).toBeEnabled();
    fireEvent.click(request);
    await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(2));

    const disabled = room({ free_turn_queue: { ...canRequest.free_turn_queue!, can_request: false, request_reason: "只有下一发言阵营的真人辩手可以申请。" } });
    render(<FreeTurnQueue room={disabled} interactive />);
    expect(screen.getAllByRole("button", { name: "举手申请下一轮" }).at(-1)).toBeDisabled();
    expect(screen.getByText("只有下一发言阵营的真人辩手可以申请。")).toBeInTheDocument();
  });

  it("shows the selected seat separately and adds ordered, timestamped badges beside frozen stage seats", () => {
    const selected = room({ current_stage: { ...room().current_stage!, selected_human_seat: "neg_1" } });
    const view = render(
      <>
        <div className="stage-page">
          <aside className="team-column aff"><div className="stage-seat">正方</div></aside>
          <aside className="team-column neg"><div className="stage-seat">反方一辩</div><div className="stage-seat">反方二辩</div></aside>
        </div>
        <FreeTurnSeatQueueAdapter room={selected} />
        <FreeTurnQueue room={selected} interactive={false} />
      </>,
    );

    expect(screen.getByRole("status")).toHaveTextContent("李同学 已获得下一轮发言权，其余辩手可等待下次申请");
    expect(view.container.querySelector('[data-free-turn-seat-badge="neg_1"]')).toHaveTextContent("已选中");
    expect(view.container.querySelector('[data-free-turn-seat-badge="neg_2"]')).toHaveTextContent("第 2 位");
    expect(view.container.querySelector('[data-free-turn-seat-badge="neg_2"]')).toHaveAttribute("title", expect.stringContaining("申请时间"));

    view.rerender(<div className="stage-page"><aside className="team-column neg"><div className="stage-seat">反方一辩</div></aside></div>);
    expect(view.container.querySelectorAll("[data-free-turn-seat-badge]")).toHaveLength(0);
  });
});
