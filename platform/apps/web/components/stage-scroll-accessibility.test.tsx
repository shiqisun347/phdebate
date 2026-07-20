import { render, waitFor } from "@testing-library/react";
import { expect, it } from "vitest";

import { StageScrollAccessibility } from "@/components/stage-scroll-accessibility";

it("makes the live subtitle scroll region keyboard focusable and cleans up its marker", async () => {
  const { unmount } = render(
    <>
      <div className="stage-page">
        <div className="subtitle-stage"><p>一段较长的实时字幕</p></div>
      </div>
      <StageScrollAccessibility />
    </>,
  );
  const subtitle = document.querySelector<HTMLElement>(".subtitle-stage p");
  await waitFor(() => expect(subtitle).toHaveAttribute("tabindex", "0"));
  expect(subtitle).toHaveAttribute("data-stage-scroll-a11y", "true");

  unmount();
  expect(subtitle).not.toHaveAttribute("tabindex");
  expect(subtitle).not.toHaveAttribute("data-stage-scroll-a11y");
});
