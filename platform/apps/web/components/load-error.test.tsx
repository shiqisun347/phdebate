import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LoadError } from "@/components/load-error";

const navigation = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => navigation,
}));

describe("load error", () => {
  afterEach(() => navigation.push.mockReset());

  it("treats a missing room as a permanent input problem with an actionable lookup", () => {
    const retry = vi.fn();
    render(<LoadError message="房间不存在或已被删除。" retry={retry} />);

    expect(screen.getByRole("heading", { level: 1, name: "房间号不存在" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重新尝试" })).not.toBeInTheDocument();
    const enter = screen.getByRole("button", { name: "进入房间" });
    expect(enter).toBeDisabled();
    fireEvent.change(screen.getByLabelText("查找其他房间"), { target: { value: "12a34567" } });
    expect(screen.getByLabelText("查找其他房间")).toHaveValue("123456");
    fireEvent.click(enter);
    expect(navigation.push).toHaveBeenCalledWith("/rooms/123456/lobby");
    expect(retry).not.toHaveBeenCalled();
  });

  it("keeps retry available for a transient service failure and uses a page h1", () => {
    const retry = vi.fn();
    render(<LoadError message="服务连接超时，请稍后重试。" retry={retry} />);

    expect(screen.getByRole("heading", { level: 1, name: "页面暂时无法打开" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新尝试" }));
    expect(retry).toHaveBeenCalledOnce();
    expect(screen.queryByLabelText("查找其他房间")).not.toBeInTheDocument();
  });
});
