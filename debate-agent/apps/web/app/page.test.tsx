import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import AgentAdminPage from "./page";

const dashboard = {
  counts: { providers: 1, personas: 8, published_prompts: 1, pending_memories: 0, running_tasks: 0 },
  providers: [{
    id: "provider-1",
    name: "正式模型",
    provider: "openai",
    base_url: "http://llm.test/v1",
    model_id: "qwen3.6-27b",
    has_api_key: true,
    timeout_seconds: 180,
    priority: 1,
    max_retries: 1,
    rpm_limit: 60,
    is_active: true,
  }],
  prompts: [],
  message_templates: [],
  presets: [],
  memory_policies: [],
  personas: [],
  memories: [],
  tasks: [],
  gateway_keys: [],
  admins: [],
  audit: [],
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe("Debate Agent admin", () => {
  it("shows independent login and loads the control plane after authentication", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ detail: "管理员会话无效或已过期。" }, 401))
      .mockResolvedValueOnce(jsonResponse({ user: { real_name: "Agent 管理员", role: "owner" }, csrf_token: "csrf-value" }))
      .mockResolvedValueOnce(jsonResponse(dashboard));
    vi.stubGlobal("fetch", fetchMock);

    render(<AgentAdminPage />);
    expect(await screen.findByRole("heading", { name: "Debate Agent 管理平台" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("管理员账号"), { target: { value: "agent_admin" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "strong-password" } });
    fireEvent.click(screen.getByRole("button", { name: "登录控制台" }));

    expect(await screen.findByRole("heading", { name: "辩论 Agent 简易设置" })).toBeInTheDocument();
    expect(screen.getByText("Agent 管理员")).toBeInTheDocument();
    expect(screen.getByText("OpenAI 兼容 LLM API（推荐）")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByLabelText("API 地址")).toHaveValue("http://llm.test/v1");
      expect(screen.getByLabelText("模型名称")).toHaveValue("qwen3.6-27b");
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(sessionStorage.getItem("debate-agent-csrf")).toBe("csrf-value");
  });
});
