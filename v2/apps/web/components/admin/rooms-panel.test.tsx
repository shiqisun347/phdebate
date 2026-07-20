import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RoomsPanel, type AdminRoomSummary } from "@/components/admin/rooms-panel";

const room = (status: string, overrides: Partial<AdminRoomSummary> = {}): AdminRoomSummary => ({
  id: `room-${status}`,
  code: status === "paused" ? "123456" : "654321",
  topic: status === "paused" ? "需要人工恢复的比赛" : "人工智能是否提升创作价值",
  status,
  is_test_data: false,
  competition: { id: "competition-1", slug: "daily", name: "4v4 人机辩论正式赛" },
  current_stage: { key: "opening", name: "正方一辩立论" },
  connected_humans: 3,
  updated_at: "2026-07-20T08:30:00Z",
  ...overrides,
});

describe("RoomsPanel", () => {
  it("does not present the loading state as an empty search result", () => {
    render(
      <RoomsPanel
        rooms={[]}
        query=""
        status=""
        dataScope=""
        pagination={{ page: 1, page_size: 100, total: 0, pages: 1 }}
        loading
        saving={false}
        onQueryChange={vi.fn()}
        onStatusChange={vi.fn()}
        onDataScopeChange={vi.fn()}
        onLoad={vi.fn()}
        onMarkAsTestData={vi.fn()}
      />,
    );

    expect(screen.getByText("正在载入房间…")).toBeInTheDocument();
    expect(screen.queryByText("没有符合条件的房间")).not.toBeInTheDocument();
  });

  it("clears every room filter in one operation", () => {
    const onQueryChange = vi.fn();
    const onStatusChange = vi.fn();
    const onDataScopeChange = vi.fn();
    const onLoad = vi.fn();
    render(
      <RoomsPanel
        rooms={[]}
        query="人工智能"
        status="paused"
        dataScope="qa"
        pagination={{ page: 2, page_size: 100, total: 101, pages: 2 }}
        loading={false}
        saving={false}
        onQueryChange={onQueryChange}
        onStatusChange={onStatusChange}
        onDataScopeChange={onDataScopeChange}
        onLoad={onLoad}
        onMarkAsTestData={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "清除筛选" }));
    expect(onQueryChange).toHaveBeenCalledWith("");
    expect(onStatusChange).toHaveBeenCalledWith("");
    expect(onDataScopeChange).toHaveBeenCalledWith("");
    expect(onLoad).toHaveBeenCalledWith(1, "", "", "");
  });

  it("prioritizes recoverable and review-required matches with direct actions", () => {
    render(
      <RoomsPanel
        rooms={[
          room("paused", { paused_at: "2026-07-19T08:30:00Z", attention_reason: "stale_paused" }),
          room("review_required", {
            id: "review",
            code: "222222",
            topic: "等待裁判复核",
            connected_humans: 0,
            attention_reason: "review_required",
            failure_reason: "裁判服务超时",
          }),
          room("completed", { id: "done", code: "333333", topic: "已经结束的比赛", is_test_data: true }),
        ]}
        query=""
        status=""
        dataScope=""
        pagination={{ page: 1, page_size: 100, total: 3, pages: 1 }}
        loading={false}
        saving={false}
        onQueryChange={vi.fn()}
        onStatusChange={vi.fn()}
        onDataScopeChange={vi.fn()}
        onLoad={vi.fn()}
        onMarkAsTestData={vi.fn()}
      />,
    );

    expect(screen.getByLabelText("当前页比赛状态摘要")).toHaveTextContent("需处理 2");
    expect(screen.getByRole("link", { name: "处理房间 123456 的异常" })).toHaveAttribute("href", "/rooms/123456/control");
    expect(screen.getByRole("link", { name: "立即复核" })).toHaveAttribute("href", "/admin?module=reviews");
    expect(screen.getByRole("link", { name: "查看房间 333333 的结果" })).toHaveAttribute("href", "/rooms/333333/result");
    expect(screen.getByText("长期暂停")).toBeInTheDocument();
    expect(screen.getByText("已暂停超过 1 小时且当前无真人在线")).toBeInTheDocument();
    expect(screen.getByText("赛果尚未发布，需要人工复核")).toBeInTheDocument();
    expect(screen.queryByText("裁判服务超时")).not.toBeInTheDocument();
    expect(screen.getAllByText("3 位真人在线")).toHaveLength(2);
    expect(screen.getByText("QA 测试")).toBeInTheDocument();
  });

  it("offers a useful reset when a filter has no results", () => {
    const onLoad = vi.fn();
    const onQueryChange = vi.fn();
    const onStatusChange = vi.fn();
    const onDataScopeChange = vi.fn();
    render(
      <RoomsPanel
        rooms={[]}
        query="不存在"
        status="terminated"
        dataScope="production"
        pagination={{ page: 1, page_size: 100, total: 0, pages: 1 }}
        loading={false}
        saving={false}
        onQueryChange={onQueryChange}
        onStatusChange={onStatusChange}
        onDataScopeChange={onDataScopeChange}
        onLoad={onLoad}
        onMarkAsTestData={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "查看全部比赛" }));
    expect(onQueryChange).toHaveBeenCalledWith("");
    expect(onStatusChange).toHaveBeenCalledWith("");
    expect(onDataScopeChange).toHaveBeenCalledWith("");
    expect(onLoad).toHaveBeenCalledWith(1, "", "", "");
  });
});
