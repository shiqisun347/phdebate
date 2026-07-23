import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

function source(relativePath: string) {
  return readFileSync(resolve(process.cwd(), relativePath), "utf8");
}

describe("retired runtime scope", () => {
  it("does not expose AI-substitution restoration actions in current user routes", () => {
    const currentRoutes = [
      source("app/me/page.tsx"),
      source("app/rooms/[code]/watch/page.tsx"),
      source("app/rooms/[code]/control/page.tsx"),
    ].join("\n");

    expect(currentRoutes).not.toContain("seat-restore-requests");
    expect(currentRoutes).not.toContain("SeatRestorePanel");
    expect(currentRoutes).not.toContain("申请恢复真人席位");
    expect(currentRoutes).not.toContain("直接恢复真人");
  });

  it("does not carry the retired seat type in current web runtime", () => {
    const runtime = [
      source("lib/types.ts"),
      source("components/debate-stage.tsx"),
      source("app/rooms/[code]/debate/page.tsx"),
      source("lib/match-events.ts"),
    ].join("\n");

    expect(runtime).not.toContain("ai_substitute");
    expect(runtime).not.toContain("seat.restore_");
    expect(runtime).not.toContain("seat.ai_substituted");
  });

  it("keeps current competition pages free of retired classroom terminology", () => {
    const currentRoutes = [
      source("app/page.tsx"),
      source("app/me/page.tsx"),
      source("app/rooms/[code]/lobby/page.tsx"),
      source("app/rooms/[code]/debate/page.tsx"),
      source("app/rooms/[code]/watch/page.tsx"),
      source("app/rooms/[code]/control/page.tsx"),
      source("app/rooms/[code]/result/page.tsx"),
      source("app/competitions/[slug]/page.tsx"),
    ].join("\n");

    expect(currentRoutes).not.toMatch(/课堂|教师|课程|教学活动|主持人/);
  });

  it("does not expose retired recording uploads or versioned product routes", () => {
    const runtime = [
      source("lib/api.ts"),
      source("app/rooms/[code]/debate/page.tsx"),
      source("app/rooms/[code]/result/page.tsx"),
    ].join("\n");

    expect(runtime).not.toContain("jixia_v2_");
    expect(runtime).not.toMatch(/speech\/.*\/audio/);
    expect(runtime).not.toMatch(/href=["'`]\/v2/);
  });

  it("keeps the retired global free-turn panel skin out of the active stylesheet", () => {
    const globals = source("app/globals.css");

    expect(globals).not.toContain(".free-turn-queue");
    expect(globals).not.toContain(".free-turn-action");
    expect(globals).not.toContain(".free-turn-countdown");
    expect(globals).toContain(".free-turn-seat-badge");
  });
});
