"use client";

const explicitOrigin = process.env.NEXT_PUBLIC_API_ORIGIN || "";
const basePath = process.env.NEXT_PUBLIC_BASE_PATH || "";

export function apiOrigin(): string {
  if (explicitOrigin) return explicitOrigin.replace(/\/$/, "");
  return typeof window === "undefined" ? basePath : `${window.location.origin}${basePath}`;
}

export function csrfToken(): string {
  if (typeof document === "undefined") return "";
  const cookies = document.cookie.split("; ");
  const pair = cookies.find((item) => item.startsWith("jixia_csrf="))
    || cookies.find((item) => item.startsWith("jixia_v2_csrf="));
  return pair ? decodeURIComponent(pair.split("=").slice(1).join("=")) : "";
}

const errorFieldLabels: Record<string, string> = {
  account: "登录账号",
  real_name: "真实姓名",
  password: "密码",
  confirm_password: "确认密码",
};

function validationErrorMessage(value: unknown): string | null {
  if (!value || typeof value !== "object") return null;
  const error = value as Record<string, unknown>;
  const location = Array.isArray(error.loc) ? error.loc : [];
  const field = [...location].reverse().find((item): item is string => typeof item === "string" && item !== "body");
  const label = field ? errorFieldLabels[field] || field : "提交内容";
  const type = typeof error.type === "string" ? error.type : "";
  if (type === "missing") return `${label}为必填项。`;
  if (type === "string_too_short") return `${label}长度不足。`;
  if (type === "string_too_long") return `${label}长度超过限制。`;
  const message = typeof error.msg === "string" ? error.msg.replace(/^Value error,\s*/i, "") : "";
  return message ? `${label}：${message}` : null;
}

export function apiErrorMessage(detail: unknown, fallback = "请求失败，请稍后重试。"): string {
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => validationErrorMessage(item) || apiErrorMessage(item, ""))
      .filter((item, index, all) => item && all.indexOf(item) === index);
    return messages.length ? messages.join("；") : fallback;
  }
  if (detail && typeof detail === "object") {
    const value = detail as Record<string, unknown>;
    for (const key of ["message", "msg", "detail", "error"]) {
      if (key in value) {
        const message = apiErrorMessage(value[key], "");
        if (message) return message;
      }
    }
  }
  return fallback;
}

export class ApiRequestError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown, message = apiErrorMessage(detail)) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.detail = detail;
  }
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const method = (init.method || "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrf = csrfToken();
    if (csrf) headers.set("X-CSRF-Token", csrf);
  }
  let response: Response;
  try {
    response = await fetch(`${apiOrigin()}${path}`, { ...init, headers, credentials: "include", cache: "no-store" });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    const isReadRequest = ["GET", "HEAD", "OPTIONS"].includes(method);
    throw new ApiRequestError(
      0,
      { code: "network_unavailable" },
      isReadRequest
        ? "网络连接失败，暂时无法加载内容。请检查网络后重试。"
        : "网络连接失败，本次操作可能尚未提交。请检查网络后重试。",
    );
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? (data as { detail?: unknown }).detail : data;
    throw new ApiRequestError(response.status, detail);
  }
  return data as T;
}

export function websocketUrl(path: string): string {
  const base = explicitOrigin || (typeof window !== "undefined" ? `${window.location.origin}${basePath}` : `http://127.0.0.1:8200${basePath}`);
  return `${base.replace(/^http/, "ws").replace(/\/$/, "")}${path}`;
}

export function safeNextPath(value: string | null | undefined): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\") || /[\u0000-\u001f]/.test(value)) {
    return "/";
  }
  return value;
}
