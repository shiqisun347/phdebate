import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SystemOverview, type AdminDashboard } from "@/components/admin/system-overview";

describe("SystemOverview", () => {
  it("renders business counts and health details from a dashboard projection", () => {
    const dashboard: AdminDashboard = {
      counts: { users: 42, competitions: 3, live_rooms: 5, review_required: 2 },
      rooms: [],
      providers: {
        agent: { endpoint: "https://agent.example/api/debate", enabled: true, healthy: true, status: "ready", latency_ms: 83 },
        lighttts: { endpoint: "legacy", enabled: false, healthy: null, status: "disabled" },
      },
      leaderboard: [],
      system_health: {
        ok: true,
        checked_at: new Date(0).toISOString(),
        checks: {
          schema: { ok: true, current: "0024", expected: "0024" },
          moss_tts_realtime: { ok: true, ready_endpoints: 1, required_endpoints: 1 },
        },
      },
    };

    render(<SystemOverview dashboard={dashboard} />);

    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByText("结构版本已同步")).toBeInTheDocument();
    expect(screen.getByText("1/1 个实时端点已就绪")).toBeInTheDocument();
    expect(screen.getByText(/辩手 Agent · 可达/)).toBeInTheDocument();
    expect(screen.queryByText(/兼容 LightTTS/)).not.toBeInTheDocument();
    expect(screen.getByText("有运营事项需要处理")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /结果复核/ })).toHaveAttribute("href", "/admin?module=reviews");
  });

  it("surfaces paused matches and failed checks without hiding the recovery entry", () => {
    const dashboard: AdminDashboard = {
      counts: { users: 12, competitions: 2, live_rooms: 1, review_required: 0 },
      rooms: [{ code: "551958", topic: "暂停比赛", status: "paused", updated_at: "2026-07-20T00:00:00Z" }],
      providers: {},
      leaderboard: [],
      system_health: {
        ok: false,
        checked_at: "2026-07-20T00:00:00Z",
        checks: { worker: { ok: false, workers: 0, queued_messages: 4, dead_letters: 1 } },
      },
    };
    render(<SystemOverview dashboard={dashboard} />);

    expect(screen.getByText(/1 项服务异常 · 0 项运维预警 · 1 场近期比赛暂停或待处理/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /比赛监管/ })).toHaveAttribute("href", "/admin?module=rooms");
    expect(screen.queryByRole("link", { name: /结果复核/ })).not.toBeInTheDocument();
  });

  it("shows a recent Agent backup failure as an operational warning without calling the platform unavailable", () => {
    const dashboard: AdminDashboard = {
      counts: { users: 12, competitions: 2, live_rooms: 0, review_required: 0 },
      rooms: [], providers: {}, leaderboard: [],
      system_health: {
        ok: true,
        checked_at: "2026-07-20T00:00:00Z",
        checks: {
          agent_backup: { ok: true, warning: true, status: "failed", age_hours: 3.2 },
          storage: { ok: true, warning: true, status: "warning", free_percent: 18.4, warning_percent: 20, failure_percent: 10 },
        },
      },
    };
    render(<SystemOverview dashboard={dashboard} />);

    expect(screen.getByText(/0 项服务异常 · 2 项运维预警/)).toBeInTheDocument();
    expect(screen.getByText("Agent 数据库备份 · 预警")).toBeInTheDocument();
    expect(screen.getByText(/最近执行失败 · 上次成功在 3.2 小时前/)).toBeInTheDocument();
    expect(screen.getByText(/18.4% · 20% 告警 \/ 10% 阻断/)).toBeInTheDocument();
  });
});
