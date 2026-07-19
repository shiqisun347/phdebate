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
  });
});
