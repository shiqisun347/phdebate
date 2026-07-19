import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiRequestError, apiErrorMessage, apiFetch, apiOrigin, csrfToken, safeNextPath, websocketUrl } from "@/lib/api";

describe("API client", () => {
  afterEach(() => {
    document.cookie = "jixia_v2_csrf=; Max-Age=0; path=/";
    vi.unstubAllGlobals();
  });

  it("uses the current origin and builds a matching websocket URL", () => {
    expect(apiOrigin()).toBe("http://localhost:3200");
    expect(websocketUrl("/ws/rooms/123456")).toBe("ws://localhost:3200/ws/rooms/123456");
  });

  it("attaches the CSRF cookie to state-changing requests", async () => {
    document.cookie = "jixia_v2_csrf=token%2Bvalue; path=/";
    expect(csrfToken()).toBe("token+value");
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/api/example", { method: "POST", body: "{}" });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = init.headers as Headers;
    expect(headers.get("X-CSRF-Token")).toBe("token+value");
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(init.credentials).toBe("include");
  });

  it("surfaces the server error detail", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "席位已被占用" }), {
      status: 409,
      headers: { "Content-Type": "application/json" },
    })));
    await expect(apiFetch("/api/example")).rejects.toThrow("席位已被占用");
  });

  it("preserves HTTP status and structured detail for recoverable workflows", async () => {
    const detail = { message: "赛事配置已被其他管理员更新。", revision: 4 };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail }), {
      status: 409,
      headers: { "Content-Type": "application/json" },
    })));
    const error = await apiFetch("/api/example").catch((value) => value);
    expect(error).toBeInstanceOf(ApiRequestError);
    expect(error).toMatchObject({ status: 409, detail, message: "赛事配置已被其他管理员更新。" });
  });

  it("turns browser network failures into actionable Chinese feedback", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const error = await apiFetch("/api/rooms/123456/ready", { method: "POST", body: "{}" }).catch((value) => value);
    expect(error).toBeInstanceOf(ApiRequestError);
    expect(error).toMatchObject({
      status: 0,
      detail: { code: "network_unavailable" },
      message: "网络连接失败，本次操作可能尚未提交。请检查网络后重试。",
    });
  });

  it("keeps AbortError available to cancellation-aware callers", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new DOMException("cancelled", "AbortError")));
    await expect(apiFetch("/api/example")).rejects.toMatchObject({ name: "AbortError" });
  });

  it("serializes structured server errors without rendering object placeholders", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      detail: [
        { type: "missing", loc: ["body", "password"], msg: "Field required" },
        { type: "missing", loc: ["body", "confirm_password"], msg: "Field required" },
      ],
    }), {
      status: 422,
      headers: { "Content-Type": "application/json" },
    })));
    await expect(apiFetch("/api/auth/register", { method: "POST", body: "{}" }))
      .rejects.toThrow("密码为必填项。；确认密码为必填项。");
  });

  it("extracts safe messages from nested error objects", () => {
    expect(apiErrorMessage({ message: "账号或密码错误。" })).toBe("账号或密码错误。");
    expect(apiErrorMessage({ detail: { msg: "服务暂时不可用。" } })).toBe("服务暂时不可用。");
    expect(apiErrorMessage({ internal: { stack: "secret" } })).toBe("请求失败，请稍后重试。");
  });

  it("accepts only local post-login destinations", () => {
    expect(safeNextPath("/me")).toBe("/me");
    expect(safeNextPath("/rooms/123456/lobby?seat=aff_1")).toBe("/rooms/123456/lobby?seat=aff_1");
    expect(safeNextPath("https://evil.example")).toBe("/");
    expect(safeNextPath("//evil.example/path")).toBe("/");
    expect(safeNextPath("/\\evil.example")).toBe("/");
  });
});
