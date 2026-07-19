import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import RankingsPage from "@/app/rankings/page";

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

const competition = {
  id: "competition-1",
  slug: "daily-4v4",
  name: "4v4 人机辩论正式赛",
  description: "",
  rules: "",
  format_key: "daily_4v4",
  team_size: 4,
  ranked: true,
  allow_custom_topic: false,
  is_active: true,
  season: { id: "season-a", slug: "season-a", name: "第一赛季", is_open: true },
};

const seasons = [
  { id: "season-a", slug: "season-a", name: "第一赛季", is_open: true },
  { id: "season-b", slug: "season-b", name: "第二赛季", is_open: false },
  { id: "season-c", slug: "season-c", name: "第三赛季", is_open: false },
];

function ranking(name: string, userId: string) {
  return { user_id: userId, real_name: name, points: 3, wins: 1, draws: 0, losses: 0, average_score: 88, matches: 1, rank: 1 };
}

describe("rankings", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("keeps the latest selected season when an older request finishes late", async () => {
    let resolveSeasonB!: (value: Response) => void;
    let resolveSeasonC!: (value: Response) => void;
    const seasonB = new Promise<Response>((resolve) => { resolveSeasonB = resolve; });
    const seasonC = new Promise<Response>((resolve) => { resolveSeasonC = resolve; });

    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/competitions")) return Promise.resolve(response({ items: [competition] }));
      if (url.endsWith("/api/seasons")) return Promise.resolve(response({ items: seasons }));
      if (url.includes("season_slug=season-b")) return seasonB;
      if (url.includes("season_slug=season-c")) return seasonC;
      if (url.includes("/api/rankings")) return Promise.resolve(response({ items: [ranking("初始榜首", "initial")] }));
      return Promise.resolve(response({ items: [] }));
    }));

    render(<RankingsPage />);
    expect(await screen.findByText("初始榜首")).toBeInTheDocument();
    const selector = screen.getByRole("combobox", { name: "选择赛季" });
    fireEvent.change(selector, { target: { value: "season-b" } });
    fireEvent.change(selector, { target: { value: "season-c" } });

    resolveSeasonC(response({ items: [ranking("第三赛季榜首", "season-c-user")] }));
    expect(await screen.findByText("第三赛季榜首")).toBeInTheDocument();

    resolveSeasonB(response({ items: [ranking("迟到的第二赛季榜首", "season-b-user")] }));
    await Promise.resolve();
    expect(screen.queryByText("迟到的第二赛季榜首")).not.toBeInTheDocument();
    expect(screen.getByText("第三赛季榜首")).toBeInTheDocument();
  });
});
