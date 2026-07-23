import { render, screen } from "@testing-library/react";
import axe from "axe-core";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MainContent } from "@/components/main-content";

const mocks = vi.hoisted(() => ({ pathname: "/" }));

vi.mock("next/navigation", () => ({
  usePathname: () => mocks.pathname,
}));

describe("MainContent", () => {
  beforeEach(() => {
    mocks.pathname = "/";
  });

  it("provides the document main landmark on regular pages", () => {
    render(<MainContent>赛事大厅内容</MainContent>);

    expect(screen.getByRole("main")).toHaveAttribute("id", "main-content");
    expect(screen.getByRole("main")).toHaveAttribute("tabindex", "-1");
  });

  it.each([
    "/rooms/357930/watch",
    "/rooms/357930/debate",
  ])("lets the debate stage own the single main landmark on %s", async (pathname) => {
    mocks.pathname = pathname;
    const { container } = render(<MainContent><main className="stage-center">比赛舞台</main></MainContent>);

    expect(screen.getAllByRole("main")).toHaveLength(1);
    expect(screen.getByRole("main")).toHaveClass("stage-center");
    const mainContent = container.querySelector<HTMLElement>("#main-content");
    expect(mainContent).toHaveProperty("tagName", "DIV");
    expect(mainContent).toHaveAttribute("tabindex", "-1");
    mainContent?.focus();
    expect(mainContent).toHaveFocus();
    expect((await axe.run(container, { rules: { "color-contrast": { enabled: false } } })).violations).toEqual([]);
  });

  it("does not remove the main landmark from room administration pages", () => {
    mocks.pathname = "/rooms/357930/control";
    render(<MainContent>比赛控制台</MainContent>);

    expect(screen.getByRole("main")).toHaveAttribute("id", "main-content");
  });
});
