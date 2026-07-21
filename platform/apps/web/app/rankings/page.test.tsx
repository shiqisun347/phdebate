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

  it("exposes a textual rank alongside podium medals", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/competitions")) return Promise.resolve(response({ items: [competition] }));
      if (url.includes("/api/seasons")) return Promise.resolve(response({ items: seasons }));
      if (url.includes("/api/rankings")) return Promise.resolve(response({ items: [ranking("榜首", "first")] }));
      return Promise.reject(new Error(`unexpected request: ${url}`));
    }));

    render(<RankingsPage />);

    expect(await screen.findByText("#1")).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: /#1/ })).toBeInTheDocument();
  });

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

  it("does not display the primary warm-up response for a different ranked competition", async () => {
    const alternate = {
      ...competition,
      id: "competition-alternate",
      slug: "regional-4v4",
      name: "区域 4v4 正式赛",
    };
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/competitions")) return Promise.resolve(response({ items: [alternate] }));
      if (url.endsWith("/api/seasons")) return Promise.resolve(response({ items: seasons }));
      if (url.includes("competition_slug=daily-4v4")) return Promise.resolve(response({ items: [ranking("错误预热榜首", "wrong")] }));
      if (url.includes("competition_slug=regional-4v4") && url.includes("season_slug=season-a")) {
        return Promise.resolve(response({ items: [ranking("区域赛事榜首", "correct")] }));
      }
      return Promise.resolve(response({ items: [] }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<RankingsPage />);

    expect(await screen.findByText("区域赛事榜首")).toBeInTheDocument();
    expect(screen.queryByText("错误预热榜首")).not.toBeInTheDocument();
    expect(screen.getByText("区域赛事榜首").closest("td")).toHaveAttribute("data-label", "辩手");
  });
});
