"use client";

import { useEffect } from "react";

const SUBTITLE_SELECTOR = ".stage-page .subtitle-stage p";
const MANAGED_ATTRIBUTE = "data-stage-scroll-a11y";

/**
 * Keeps the frozen debate-stage implementation untouched while making its
 * overflowed live-caption region reachable by keyboard users.
 */
export function StageScrollAccessibility() {
  useEffect(() => {
    const subtitle = document.querySelector<HTMLElement>(SUBTITLE_SELECTOR);
    if (!subtitle || subtitle.hasAttribute("tabindex")) return;
    subtitle.tabIndex = 0;
    subtitle.setAttribute(MANAGED_ATTRIBUTE, "true");

    return () => {
      if (subtitle.getAttribute(MANAGED_ATTRIBUTE) !== "true") return;
      subtitle.removeAttribute("tabindex");
      subtitle.removeAttribute(MANAGED_ATTRIBUTE);
    };
  }, []);

  return null;
}
