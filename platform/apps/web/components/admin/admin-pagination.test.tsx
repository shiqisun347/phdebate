import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AdminPagination } from "@/components/admin/admin-pagination";

describe("AdminPagination", () => {
  it("exposes named navigation and changes page without submitting a form", () => {
    const onPageChange = vi.fn();
    render(
      <AdminPagination
        label="用户管理"
        page={2}
        pages={4}
        loading={false}
        onPageChange={onPageChange}
      />,
    );

    expect(screen.getByRole("navigation", { name: "用户管理分页" })).toBeInTheDocument();
    expect(screen.getByText("第 2 / 4 页")).toHaveAttribute("aria-live", "polite");
    expect(screen.getByRole("button", { name: "上一页" })).toHaveAttribute("type", "button");

    fireEvent.click(screen.getByRole("button", { name: "上一页" }));
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(onPageChange).toHaveBeenNthCalledWith(1, 1);
    expect(onPageChange).toHaveBeenNthCalledWith(2, 3);
  });

  it("disables navigation at boundaries and while loading", () => {
    const { rerender } = render(
      <AdminPagination
        label="审计日志"
        page={1}
        pages={3}
        loading={false}
        onPageChange={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "下一页" })).toBeEnabled();

    rerender(
      <AdminPagination
        label="审计日志"
        page={2}
        pages={3}
        loading
        onPageChange={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
  });
});
