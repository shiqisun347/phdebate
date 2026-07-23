import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SkipLink } from "@/components/skip-link";

describe("skip link", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("moves keyboard focus to main content without cancelling fragment navigation", () => {
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      callback(0);
      return 1;
    });
    render(
      <>
        <SkipLink />
        <main id="main-content" tabIndex={-1}>主要内容</main>
      </>,
    );
    const link = screen.getByRole("link", { name: "跳到主要内容" });
    const click = new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      button: 0,
    });

    link.dispatchEvent(click);

    expect(click.defaultPrevented).toBe(false);
    expect(link).toHaveAttribute("href", "#main-content");
    expect(screen.getByRole("main")).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("main")).toHaveFocus();
  });

  it("does not hijack modified clicks", () => {
    const requestAnimationFrame = vi.fn();
    vi.stubGlobal("requestAnimationFrame", requestAnimationFrame);
    render(
      <>
        <SkipLink />
        <main id="main-content" tabIndex={-1}>主要内容</main>
      </>,
    );

    fireEvent.click(screen.getByRole("link", { name: "跳到主要内容" }), {
      ctrlKey: true,
    });

    expect(requestAnimationFrame).not.toHaveBeenCalled();
    expect(document.body).toHaveFocus();
  });
});
