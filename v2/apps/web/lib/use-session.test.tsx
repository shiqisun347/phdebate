import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { apiFetch } from "@/lib/api";
import { notifySessionChanged, useSession } from "@/lib/use-session";

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(),
}));

const mockedApiFetch = vi.mocked(apiFetch);

function SessionProbe({ name }: { name: string }) {
  const { user, loading } = useSession();
  return <span>{name}:{loading ? "loading" : user?.real_name || "anonymous"}</span>;
}

describe("useSession", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("deduplicates concurrent session reads across navigation and page consumers", async () => {
    mockedApiFetch.mockResolvedValue({
      user: { id: "u-1", account: "debater", real_name: "测试辩手", role: "user", is_active: true },
    });

    render(<><SessionProbe name="nav" /><SessionProbe name="page" /></>);

    await waitFor(() => expect(screen.getByText("nav:测试辩手")).toBeInTheDocument());
    expect(screen.getByText("page:测试辩手")).toBeInTheDocument();
    expect(mockedApiFetch).toHaveBeenCalledTimes(1);
    expect(mockedApiFetch).toHaveBeenCalledWith("/api/auth/session-state");
  });

  it("deduplicates a session-change refresh for all mounted consumers", async () => {
    mockedApiFetch.mockResolvedValue({ user: null });
    render(<><SessionProbe name="nav" /><SessionProbe name="dialog" /></>);
    await waitFor(() => expect(screen.getByText("nav:anonymous")).toBeInTheDocument());
    expect(mockedApiFetch).toHaveBeenCalledTimes(1);

    mockedApiFetch.mockResolvedValue({
      user: { id: "u-2", account: "new-user", real_name: "新辩手", role: "user", is_active: true },
    });
    act(() => notifySessionChanged());

    await waitFor(() => expect(screen.getByText("dialog:新辩手")).toBeInTheDocument());
    expect(mockedApiFetch).toHaveBeenCalledTimes(2);
  });
});
