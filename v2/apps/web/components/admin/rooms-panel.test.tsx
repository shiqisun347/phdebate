import { render, screen } from "@testing-library/react";
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
});
