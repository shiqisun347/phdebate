let csrfToken = "";

function cookieCsrfToken(): string {
  if (typeof document === "undefined") return "";
  const prefix = "debate_agent_csrf=";
  const item = document.cookie.split(";").map((value) => value.trim()).find((value) => value.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : "";
}

export function setCsrfToken(value: string) {
  csrfToken = value;
  if (typeof sessionStorage !== "undefined") sessionStorage.setItem("debate-agent-csrf", value);
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = csrfToken || cookieCsrfToken() || (typeof sessionStorage !== "undefined" ? sessionStorage.getItem("debate-agent-csrf") || "" : "");
  const headers = new Headers(init.headers);
  if (!(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  if (init.method && init.method !== "GET" && token) headers.set("X-CSRF-Token", token);
  const response = await fetch(`/debate/api${path}`, { ...init, headers, credentials: "include", cache: "no-store" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `请求失败（HTTP ${response.status}）`);
  return payload as T;
}
