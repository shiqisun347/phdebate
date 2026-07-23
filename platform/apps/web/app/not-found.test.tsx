import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import NotFound from "@/app/not-found";

describe("not found page", () => {
  it("keeps expired and legacy links inside the single current product entry", () => {
    render(<NotFound />);

    expect(screen.getByRole("heading", { name: "没有找到这个页面" })).toBeInTheDocument();
    expect(screen.getByText(/所有赛事、房间和观战入口现在都从赛事大厅进入/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "返回赛事大厅" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("link", { name: "查看排行榜" })).toHaveAttribute("href", "/rankings");
    expect(screen.queryByText(/V2|旧版|新版/)).not.toBeInTheDocument();
  });
});
