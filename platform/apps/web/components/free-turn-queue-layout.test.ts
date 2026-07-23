import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const queueCss = readFileSync(resolve(process.cwd(), "components/free-turn-queue.module.css"), "utf8");
const globalCss = readFileSync(resolve(process.cwd(), "app/globals.css"), "utf8");

describe("free-turn bottom dock layout contract", () => {
  it("keeps the queue above the fixed stage stacking context", () => {
    const stageZ = Number(globalCss.match(/\.stage-page\s*\{[^}]*z-index:\s*(\d+)/s)?.[1]);
    const queueZ = Number(queueCss.match(/\.queue\s*\{[^}]*z-index:\s*(\d+)/s)?.[1]);

    expect(stageZ).toBe(80);
    expect(queueZ).toBeGreaterThan(stageZ);
  });

  it("keeps the full queue above the dock and the short hand action inside its upper row", () => {
    expect(queueCss).toMatch(/bottom:\s*calc\(118px\s*\+\s*env\(safe-area-inset-bottom\)\)/);
    expect(queueCss).toMatch(/@media\s*\(max-width:\s*720px\)[\s\S]*?\.queue\s*\{[^}]*bottom:\s*calc\(166px\s*\+\s*env\(safe-area-inset-bottom\)\)/s);
    expect(queueCss).toMatch(/\.queue\[data-actionable="true"\]:not\(:has\(\.detail\)\)\s*\{[^}]*bottom:\s*calc\(80px\s*\+\s*env\(safe-area-inset-bottom\)\)/s);
  });

  it("keeps secondary match actions readable on the light settings sheet", () => {
    expect(globalCss).toMatch(
      /\.stage-settings\s+\.button-secondary\s*\{[^}]*color:\s*#35577d;[^}]*background:\s*#f7f9fc;/s,
    );
    expect(globalCss).toMatch(
      /\.stage-settings\s+\.button-secondary:disabled\s*\{[^}]*color:\s*#8290a8;[^}]*background:\s*#f3f5f8;/s,
    );
  });

  it("keeps the desktop transcript action on one readable line", () => {
    expect(globalCss).toMatch(
      /\.control-tools\s+\.stage-transcript-trigger\s*\{[^}]*white-space:\s*nowrap;[^}]*flex-shrink:\s*0;/s,
    );
  });
});
