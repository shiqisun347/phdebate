import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GlobalNav } from "@/components/global-nav";
import type { User } from "@/lib/types";

const session = vi.hoisted(() => ({
  user: { id: "admin", account: "admin", real_name: "系统管理员", role: "system_admin", is_active: true } as User,
}));
const navigation = vi.hoisted(() => ({ push: vi.fn(), refresh: vi.fn() }));

vi.mock("next/navigation", () => ({
  usePathname: () => "/",
  useRouter: () => navigation,
}));

vi.mock("@/lib/use-session", () => ({
  notifySessionChanged: vi.fn(),
  useSession: () => ({
    user: session.user,
    loading: false,
    refresh: vi.fn(),
  }),
}));

describe("global navigation", () => {
  afterEach(() => {
    session.user = { id: "admin", account: "admin", real_name: "系统管理员", role: "system_admin", is_active: true };
    navigation.push.mockReset();
    navigation.refresh.mockReset();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("provides an accessible compact menu with the administrator entry", async () => {
    const { container } = render(<GlobalNav />);
    const toggle = screen.getByRole("button", { name: "打开导航菜单" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(screen.getByRole("button", { name: "关闭导航菜单" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("navigation", { name: "主要导航" })).toHaveClass("open");
    expect(screen.getByRole("link", { name: "赛事大厅" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("link", { name: "赛事大厅" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "排行榜" })).toHaveAttribute("href", "/rankings");
    expect(screen.getByRole("link", { name: "系统管理" })).toHaveAttribute("href", "/admin");
    expect(screen.getByRole("link", { name: "系统管理员" })).toHaveAttribute("href", "/me");
    const result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(result.violations).toEqual([]);
  });

  it("closes the compact menu with Escape and restores focus to its toggle", () => {
    render(<GlobalNav />);
    const toggle = screen.getByRole("button", { name: "打开导航菜单" });
    fireEvent.click(toggle);

    fireEvent.keyDown(document, { key: "Escape" });

    const closedToggle = screen.getByRole("button", { name: "打开导航菜单" });
    expect(closedToggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("navigation", { name: "主要导航" })).not.toHaveClass("open");
    expect(closedToggle).toHaveFocus();
  });

  it.each([
    ["普通用户", "普通用户"],
  ])("does not show management entries for %s", (_accountType, realName) => {
    session.user = { id: "user-1", account: "user", real_name: realName, role: "user", is_active: true };
    render(<GlobalNav />);
    expect(screen.getByRole("link", { name: "赛事大厅" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("link", { name: "排行榜" })).toHaveAttribute("href", "/rankings");
    expect(screen.getByRole("link", { name: realName })).toHaveAttribute("href", "/me");
    expect(screen.queryByRole("link", { name: /教学活动|政策管理|研究导出|系统管理/ })).not.toBeInTheDocument();
  });

  it("keeps the user signed in and offers a retry when logout fails", async () => {
    let attempts = 0;
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => {
      attempts += 1;
      if (attempts === 1) return Promise.reject(new Error("network offline"));
      return Promise.resolve(new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    }));
    render(<GlobalNav />);

    fireEvent.click(screen.getByRole("button", { name: "退出登录" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("网络连接失败");
    expect(navigation.push).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "重试退出" }));
    await waitFor(() => expect(navigation.push).toHaveBeenCalledWith("/"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(attempts).toBe(2);
  });
});
