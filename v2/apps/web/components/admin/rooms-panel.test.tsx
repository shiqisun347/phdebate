import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RoomsPanel } from "@/components/admin/rooms-panel";

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
});
