import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { UsersPanel } from "@/components/admin/users-panel";
import type { User } from "@/lib/types";

const users = [
  { id: "admin", account: "admin", real_name: "管理员", role: "system_admin", is_active: true, is_test_account: false },
  { id: "student", account: "student", real_name: "参赛学生", role: "user", is_active: true, is_test_account: false },
] as User[];

describe("UsersPanel", () => {
  it("does not announce a false empty result while the first page is loading", () => {
    render(
      <UsersPanel
        currentUserId="admin"
        users={[]}
        query=""
        pagination={{ page: 1, page_size: 100, total: 0, pages: 1 }}
        loading
        saving={false}
        onQueryChange={vi.fn()}
        onSearch={vi.fn()}
        onPasswordReset={vi.fn()}
        onPatch={vi.fn()}
      />,
    );

    expect(screen.getByText("正在载入用户…")).toBeInTheDocument();
    expect(screen.queryByText("没有符合条件的用户")).not.toBeInTheDocument();
  });

  it("keeps the current account protected and delegates search and mutations", () => {
    const onSearch = vi.fn();
    const onPatch = vi.fn();
    const onQueryChange = vi.fn();

    render(
      <UsersPanel
        currentUserId="admin"
        users={users}
        query="学生"
        pagination={{ page: 2, page_size: 100, total: 150, pages: 2 }}
        loading={false}
        saving={false}
        onQueryChange={onQueryChange}
        onSearch={onSearch}
        onPasswordReset={vi.fn()}
        onPatch={onPatch}
      />,
    );

    expect(screen.getByText("受保护")).toBeInTheDocument();
    fireEvent.submit(screen.getByRole("button", { name: "搜索" }).closest("form")!);
    expect(onSearch).toHaveBeenCalledWith(1, "学生");
    fireEvent.click(screen.getByRole("button", { name: "停用账号 参赛学生" }));
    expect(onPatch).toHaveBeenCalledWith(users[1], { is_active: false });
    fireEvent.click(screen.getByRole("button", { name: "上一页" }));
    expect(onSearch).toHaveBeenCalledWith(1);
    fireEvent.change(screen.getByLabelText("搜索真实姓名或账号"), { target: { value: "新查询" } });
    expect(onQueryChange).toHaveBeenCalledWith("新查询");
    fireEvent.click(screen.getByRole("button", { name: "清除搜索" }));
    expect(onQueryChange).toHaveBeenCalledWith("");
    expect(onSearch).toHaveBeenCalledWith(1, "");
  });
});
