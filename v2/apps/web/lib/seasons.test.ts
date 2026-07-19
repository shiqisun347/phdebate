import { describe, expect, it } from "vitest";

import { roomCreationBlockedReason, seasonStatusLabel } from "@/lib/seasons";
import type { Competition, Season } from "@/lib/types";

const baseSeason: Season = {
  id: "season",
  name: "测试赛季",
  slug: "test-season",
  starts_at: "2000-01-01T00:00:00.000Z",
  ends_at: "2000-01-02T00:00:00.000Z",
  is_active: true,
  is_open: false,
  created_at: "2000-01-01T00:00:00.000Z",
  updated_at: "2000-01-01T00:00:00.000Z",
};

const rankedCompetition: Competition = {
  id: "competition",
  slug: "daily-4v4",
  name: "日常赛",
  tagline: "",
  description: "",
  rules: "",
  format: "4v4",
  seat_count: 8,
  ranked: true,
  allow_custom_topic: false,
  accent: "violet",
  live_count: 0,
};

describe("season presentation", () => {
  it("uses the API open flag as the authority for an active season", () => {
    const season = { ...baseSeason, ends_at: null, is_open: true };
    expect(seasonStatusLabel(season)).toBe("测试赛季 · 进行中");
    expect(roomCreationBlockedReason({ ...rankedCompetition, season })).toBeNull();
  });

  it("explains missing, future, expired and disabled ranked seasons", () => {
    expect(roomCreationBlockedReason(rankedCompetition)).toContain("暂未配置赛季");
    expect(roomCreationBlockedReason({ ...rankedCompetition, season: baseSeason })).toContain("已结束");
    expect(
      roomCreationBlockedReason({
        ...rankedCompetition,
        season: { ...baseSeason, starts_at: "2999-01-01T00:00:00.000Z", ends_at: null },
      }),
    ).toContain("开始");
    expect(
      roomCreationBlockedReason({ ...rankedCompetition, season: { ...baseSeason, is_active: false } }),
    ).toContain("已停用");
  });

  it("never blocks unranked training rooms", () => {
    expect(roomCreationBlockedReason({ ...rankedCompetition, ranked: false })).toBeNull();
  });
});
