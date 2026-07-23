import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { apiFetch } from "@/lib/api";
import { notifySessionChanged, useSession } from "@/lib/use-session";

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(),
}));

const mockedApiFetch = vi.mocked(apiFetch);

function SessionProbe({ name }: { name: string }) {
  const { user, activeRoom, loading } = useSession();
  return <span>{name}:{loading ? "loading" : `${user?.real_name || "anonymous"}:${activeRoom?.code || "none"}`}</span>;
}

describe("useSession", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("deduplicates concurrent session reads across navigation and page consumers", async () => {
    mockedApiFetch.mockResolvedValue({
      user: { id: "u-1", account: "debater", real_name: "测试辩手", role: "user", is_active: true },
      active_room: { code: "381526", topic: "测试辩题", status: "running", seat_key: "aff_1" },
    });

    render(<><SessionProbe name="nav" /><SessionProbe name="page" /></>);

    await waitFor(() => expect(screen.getByText("nav:测试辩手:381526")).toBeInTheDocument());
    expect(screen.getByText("page:测试辩手:381526")).toBeInTheDocument();
    expect(mockedApiFetch).toHaveBeenCalledTimes(1);
    expect(mockedApiFetch).toHaveBeenCalledWith("/api/auth/session-state");
  });

  it("deduplicates a session-change refresh for all mounted consumers", async () => {
    mockedApiFetch.mockResolvedValue({ user: null });
    render(<><SessionProbe name="nav" /><SessionProbe name="dialog" /></>);
    await waitFor(() => expect(screen.getByText("nav:anonymous:none")).toBeInTheDocument());
    expect(mockedApiFetch).toHaveBeenCalledTimes(1);

    mockedApiFetch.mockResolvedValue({
      user: { id: "u-2", account: "new-user", real_name: "新辩手", role: "user", is_active: true },
    });
    act(() => notifySessionChanged());

    await waitFor(() => expect(screen.getByText("dialog:新辩手:none")).toBeInTheDocument());
    expect(mockedApiFetch).toHaveBeenCalledTimes(2);
  });
});
