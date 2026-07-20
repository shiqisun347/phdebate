import { render, screen } from "@testing-library/react";
import axe from "axe-core";
import { describe, expect, it } from "vitest";

import { PageLoading } from "@/components/page-loading";

describe("PageLoading", () => {
  it("announces a calm, accessible loading state", async () => {
    const { container } = render(<PageLoading label="正在载入赛事大厅…" />);
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-busy", "true");
    expect(screen.getByText("正在载入赛事大厅…")).toBeInTheDocument();
    expect((await axe.run(container, { rules: { "color-contrast": { enabled: false } } })).violations).toEqual([]);
  });

  it("supports context-specific loading guidance", () => {
    render(<PageLoading label="正在载入管理系统…" detail="正在同步服务状态与比赛数据" />);
    expect(screen.getByText("正在同步服务状态与比赛数据")).toBeInTheDocument();
    expect(screen.queryByText("正在同步最新比赛数据")).not.toBeInTheDocument();
  });
});
