import { act, fireEvent, render, screen } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import HomePage from "@/app/page";
import CompetitionDetailPage from "@/app/competitions/[slug]/page";
import RankingsPage from "@/app/rankings/page";

vi.mock("next/navigation", () => ({
  useParams: () => ({ slug: "daily-4v4" }),
}));

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("public pages", () => {
  beforeEach(() => {
    vi.stubGlobal("scrollTo", vi.fn());
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    window.history.replaceState({}, "", "/");
  });

  it("shows a retryable error instead of an endless loading screen", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network unavailable")));
    render(<HomePage />);
    expect(await screen.findByRole("heading", { name: "页面暂时无法打开" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新尝试" })).toBeEnabled();
  });

  it("presents participant-led competitions and labels paused rooms as watchable rather than running", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/competitions")) {
        return Promise.resolve(response({ items: [
          {
            id: "daily", slug: "daily-4v4", name: "4v4 人机辩论日常赛", tagline: "", description: "", rules: "",
            format: "4v4", seat_count: 8, ranked: true, allow_custom_topic: false, accent: "violet", live_count: 0,
          },
          {
            id: "training", slug: "training-1v1", name: "1v1 辩论训练赛", tagline: "", description: "", rules: "",
            format: "1v1", seat_count: 2, ranked: false, allow_custom_topic: true, accent: "cyan", live_count: 0,
          },
        ] }));
      }
      if (url.includes("/api/live-rooms")) {
        return Promise.resolve(response({ items: [
          { code: "123456", topic: "测试辩题", status: "running", competition_name: "4v4 人机辩论日常赛", stage: "正方一辩立论" },
          { code: "123457", topic: "暂停中的测试辩题", status: "paused", competition_name: "1v1 辩论训练赛", stage: "自由辩论", paused_at: "2026-07-19T10:30:00+08:00" },
        ] }));
      }
      return Promise.resolve(response({ items: [] }));
    }));
    render(<HomePage />);
    expect(await screen.findAllByRole("heading", { name: /4v4 人机辩论正式赛/ })).toHaveLength(2);
    expect(screen.getByText("参赛者自主组局，", { selector: ".hero-title-line" })).toBeInTheDocument();
    expect(screen.getByText("系统自动开赛", { selector: ".hero-title-line" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "创建 4v4 比赛" })).toBeEnabled();
    expect(screen.getByRole("heading", { name: "1v1 辩论训练赛" })).toBeInTheDocument();
    expect(screen.getAllByText("0 场可观战")).toHaveLength(2);
    expect(screen.queryByText(/场进行中/)).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "公开比赛" })).toBeInTheDocument();
    expect(screen.getByText("比赛进行中")).toBeInTheDocument();
    expect(screen.getByText("比赛已暂停")).toBeInTheDocument();
    expect(screen.getByText("正方一辩立论")).toBeInTheDocument();
    expect(screen.getByText(/暂停于 .*等待房主恢复/)).toBeInTheDocument();
    expect(screen.queryByText(/4v4 人机辩论日常赛/)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /教学活动|政策管理|研究导出/ })).not.toBeInTheDocument();
  });

  it("keeps core competition actions available when live rooms and rankings fail", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/competitions")) {
        return Promise.resolve(response({ items: [{
          id: "daily", slug: "daily-4v4", name: "4v4 人机辩论日常赛", tagline: "", description: "", rules: "",
          format: "4v4", seat_count: 8, ranked: true, allow_custom_topic: false, accent: "violet", live_count: 0,
        }] }));
      }
      return Promise.reject(new Error("secondary service unavailable"));
    }));

    render(<HomePage />);

    expect(await screen.findByRole("button", { name: "创建 4v4 比赛" })).toBeEnabled();
    expect(screen.getAllByRole("heading", { name: /4v4 人机辩论正式赛/ })).toHaveLength(2);
    expect(await screen.findByRole("button", { name: "重新载入公开比赛" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "重新载入排行榜" })).toBeEnabled();
    expect(screen.queryByRole("heading", { name: "页面暂时无法打开" })).not.toBeInTheDocument();
  });

  it("does not wait for slow secondary panels before enabling competition actions", async () => {
    const never = new Promise<Response>(() => undefined);
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/competitions")) {
        return Promise.resolve(response({ items: [{
          id: "daily", slug: "daily-4v4", name: "4v4 人机辩论日常赛", tagline: "", description: "", rules: "",
          format: "4v4", seat_count: 8, ranked: true, allow_custom_topic: false, accent: "violet", live_count: 0,
        }] }));
      }
      return never;
    }));

    render(<HomePage />);

    expect(await screen.findByRole("button", { name: "创建 4v4 比赛" })).toBeEnabled();
    expect(screen.queryByText("正在载入赛事大厅…")).not.toBeInTheDocument();
    expect(screen.getByText("正在同步公开比赛…")).toBeInTheDocument();
    expect(screen.getByText("正在同步赛季榜单…")).toBeInTheDocument();
  });

  it("refreshes public rooms in the background only while the page is visible", async () => {
    vi.useFakeTimers();
    let liveRequestCount = 0;
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/competitions")) {
        return Promise.resolve(response({ items: [{
          id: "daily", slug: "daily-4v4", name: "4v4 人机辩论日常赛", tagline: "", description: "", rules: "",
          format: "4v4", seat_count: 8, ranked: true, allow_custom_topic: false, accent: "violet", live_count: 0,
        }] }));
      }
      if (url.includes("/api/live-rooms")) {
        liveRequestCount += 1;
        if (liveRequestCount === 3) return Promise.reject(new Error("temporary sync failure"));
        return Promise.resolve(response({ items: liveRequestCount === 1 ? [] : [
          { code: "654321", topic: "自动同步后的公开比赛", status: "running", competition_name: "4v4 人机辩论日常赛", stage: "自由辩论" },
        ] }));
      }
      return Promise.resolve(response({ items: [] }));
    }));

    render(<HomePage />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByRole("button", { name: "创建 4v4 比赛" })).toBeEnabled();
    expect(screen.getByText("暂无可观战的公开比赛")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(screen.getByText("自动同步后的公开比赛")).toBeInTheDocument();
    expect(liveRequestCount).toBe(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(screen.getByText("自动同步后的公开比赛")).toBeInTheDocument();
    expect(screen.getByText("实时状态同步失败，以下为最近一次成功结果。")).toBeInTheDocument();
    expect(liveRequestCount).toBe(3);
  });

  it("supports standard keyboard navigation across competition detail tabs", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      competition: {
        id: "daily", slug: "daily-4v4", name: "4v4 人机辩论日常赛", tagline: "", description: "赛事说明", rules: "赛事规则",
        format: "4v4", seat_count: 8, ranked: true, allow_custom_topic: false, accent: "violet", live_count: 0, topics: [],
      },
      leaderboard: [],
      live_rooms: [],
    })));
    render(<CompetitionDetailPage />);

    const intro = await screen.findByRole("tab", { name: "赛事介绍" });
    const ranking = screen.getByRole("tab", { name: "排行榜" });
    const live = screen.getByRole("tab", { name: "观战列表" });
    const rules = screen.getByRole("tab", { name: "规则说明" });

    expect(intro).toHaveAttribute("aria-selected", "true");
    expect(intro).toHaveAttribute("tabindex", "0");
    expect(ranking).toHaveAttribute("tabindex", "-1");

    intro.focus();
    fireEvent.keyDown(intro, { key: "ArrowRight" });
    expect(ranking).toHaveFocus();
    expect(ranking).toHaveAttribute("aria-selected", "true");
    expect(ranking).toHaveAttribute("tabindex", "0");
    expect(intro).toHaveAttribute("tabindex", "-1");

    fireEvent.keyDown(ranking, { key: "End" });
    expect(rules).toHaveFocus();
    expect(rules).toHaveAttribute("aria-selected", "true");

    fireEvent.keyDown(rules, { key: "ArrowRight" });
    expect(intro).toHaveFocus();
    expect(intro).toHaveAttribute("aria-selected", "true");

    fireEvent.keyDown(intro, { key: "ArrowLeft" });
    expect(rules).toHaveFocus();
    expect(rules).toHaveAttribute("aria-selected", "true");

    fireEvent.keyDown(rules, { key: "Home" });
    expect(intro).toHaveFocus();
    expect(intro).toHaveAttribute("aria-selected", "true");
    expect(live).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", "competition-tab-intro");
  });

  it("starts a competition detail at the top after client-side navigation", async () => {
    const scrollTo = vi.fn();
    vi.stubGlobal("scrollTo", scrollTo);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      competition: {
        id: "daily", slug: "daily-4v4", name: "4v4 人机辩论日常赛", tagline: "", description: "赛事说明", rules: "赛事规则",
        format: "4v4", seat_count: 8, ranked: true, allow_custom_topic: false, accent: "violet", live_count: 0, topics: [],
      },
      leaderboard: [],
      live_rooms: [],
    })));

    render(<CompetitionDetailPage />);

    expect(await screen.findByRole("heading", { name: "4v4 人机辩论正式赛" })).toBeInTheDocument();
    expect(scrollTo).toHaveBeenCalledWith({ top: 0, left: 0, behavior: "auto" });
  });

  it("explains why a cancelled room deep link returned to the lobby", async () => {
    window.history.replaceState({}, "", "/?room_closed=123456");
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
      if (String(input).includes("/api/competitions")) {
        return Promise.resolve(response({ items: [{
          id: "training", slug: "training-1v1", name: "1v1 辩论训练赛", tagline: "", description: "", rules: "",
          format: "1v1", seat_count: 2, ranked: false, allow_custom_topic: true, accent: "cyan", live_count: 0,
        }] }));
      }
      return Promise.resolve(response({ items: [] }));
    }));
    render(<HomePage />);
    expect(await screen.findByRole("status")).toHaveTextContent("房间 #123456 已关闭");
    expect(window.location.search).toBe("");
  });

  it("labels the ranked competition selector and renders API data", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/seasons")) {
        return Promise.resolve(response({ items: [{
          id: "season", name: "第一赛季", slug: "season-1", starts_at: new Date(0).toISOString(), ends_at: null,
          is_active: true, is_open: true, created_at: new Date(0).toISOString(), updated_at: new Date(0).toISOString(),
        }] }));
      }
      if (url.includes("/api/competitions")) {
        return Promise.resolve(response({ items: [{
          id: "competition", slug: "daily-4v4", name: "4v4 人机辩论日常赛", tagline: "", description: "", rules: "",
          format: "4v4", seat_count: 8, ranked: true, allow_custom_topic: false, accent: "violet", live_count: 0,
          season: { id: "season", name: "第一赛季", slug: "season-1", starts_at: new Date(0).toISOString(), ends_at: null, is_active: true, is_open: true, created_at: new Date(0).toISOString(), updated_at: new Date(0).toISOString() },
        }] }));
      }
      return Promise.resolve(response({ items: [{
        rank: 1, user_id: "user", real_name: "测试辩手", points: 3, wins: 1, draws: 0, losses: 0, average_score: 88, matches: 1,
      }] }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const { container } = render(<RankingsPage />);
    expect(await screen.findByRole("combobox", { name: "选择积分赛事" })).toHaveValue("daily-4v4");
    expect(screen.getByRole("option", { name: "4v4 人机辩论正式赛" })).toBeInTheDocument();
    expect(await screen.findByRole("combobox", { name: "选择赛季" })).toHaveValue("season-1");
    expect(await screen.findByText("测试辩手")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual(expect.arrayContaining([
      expect.stringContaining("/api/competitions"),
      expect.stringContaining("/api/seasons"),
      expect.stringContaining("/api/rankings?competition_slug=daily-4v4"),
    ]));
    const result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(result.violations).toEqual([]);
  });
});
