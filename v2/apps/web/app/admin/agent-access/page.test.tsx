import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import AgentAccessPage from "./page";

const navigation = vi.hoisted(() => ({ replace: vi.fn() }));

vi.mock("next/navigation", () => ({ useRouter: () => navigation }));
vi.mock("@/lib/use-session", () => ({
  useSession: () => ({
    user: { id: "admin", account: "admin", real_name: "管理员", role: "system_admin", is_active: true },
    loading: false,
  }),
}));

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

describe("RESTful Agent access", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    document.cookie = "jixia_v2_csrf=; Max-Age=0; path=/";
  });

  it("keeps protocol and method fixed, redacts the secret and tests health", async () => {
    document.cookie = "jixia_v2_csrf=csrf-value; path=/";
    const current = {
      id: "agent-provider",
      kind: "agent",
      endpoint: "https://117.50.218.251/debate/api/debate",
      settings: {
        method: "POST",
        protocol: "restful",
        health_endpoint: "https://117.50.218.251/debate/api/health",
        timeout_seconds: 120,
        stream: true,
      },
      has_secret: true,
      is_active: true,
      updated_at: new Date(0).toISOString(),
    };
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/admin/providers") && (!init?.method || init.method === "GET")) return Promise.resolve(response({ items: [current] }));
      if (url.endsWith("/api/admin/providers/agent") && init?.method === "PUT") return Promise.resolve(response({ provider: current }));
      if (url.endsWith("/api/admin/providers/agent/test")) return Promise.resolve(response({ latency_ms: 18.4, health: { status: "ready" } }));
      return Promise.resolve(new Response(JSON.stringify({ detail: "unexpected request" }), { status: 500 }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<AgentAccessPage />);
    expect(await screen.findByRole("heading", { name: "RESTful Agent 接入" })).toBeInTheDocument();
    expect(screen.getByDisplayValue("RESTful HTTP")).toBeDisabled();
    expect(screen.getByDisplayValue("POST")).toBeDisabled();
    expect(screen.getByLabelText(/^共享密钥$/)).toHaveAttribute("placeholder", "已配置；留空保持不变");
    expect(screen.getByText(/只保留姓名和语音音色标识/)).toBeInTheDocument();
    expect(screen.queryByText(/LightTTS 音色/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /保存接入配置/ }));
    expect(await screen.findByText(/只影响之后开始的比赛/)).toBeInTheDocument();
    const saveCall = fetchMock.mock.calls.find(([url, init]) => String(url).endsWith("/api/admin/providers/agent") && init?.method === "PUT");
    const savedBody = JSON.parse(String(saveCall?.[1]?.body));
    expect(savedBody.settings).toMatchObject({ health_endpoint: current.settings.health_endpoint, timeout_seconds: 120, stream: true });
    expect(savedBody.settings.method).toBeUndefined();
    expect(savedBody.secret).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /测试连接/ }));
    expect(await screen.findByText(/连接成功 · 18.4 ms · ready/)).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });
});
