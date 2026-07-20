"use client";

import type { MouseEvent } from "react";

export function SkipLink() {
  const focusMainContent = (event: MouseEvent<HTMLAnchorElement>) => {
    // Leave the anchor's native fragment navigation intact so the URL hash,
    // browser history and built-in scrolling continue to work. Focus after
    // that navigation, without causing a second scroll jump.
    if (
      event.button !== 0
      || event.metaKey
      || event.ctrlKey
      || event.shiftKey
      || event.altKey
    ) return;
    window.requestAnimationFrame(() => {
      document.getElementById("main-content")?.focus({ preventScroll: true });
    });
  };

  return (
    <a className="skip-link" href="#main-content" onClick={focusMainContent}>
      跳到主要内容
    </a>
  );
}
