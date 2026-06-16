import type { ApiResponse, ASRArchiveRecognitionResult, ASRProbeResult, AuditLog, AudienceVotePayload, CurrentMatchSummary, DataSummary, ExportBundle, MatchSnapshot, PreflightReport, RuntimeAuthStatus, SpeechDiagnostics, TTSProbeResult, VoteOptions } from "../types/contracts";

const apiBase = import.meta.env.VITE_API_BASE ?? "";

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = authTokenForCurrentPage();
  const response = await fetch(`${apiBase}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers ?? {})
    },
    ...init
  });

  const rawBody = await response.text();
  let body: ApiResponse<T> | null = null;
  if (rawBody) {
    try {
      body = JSON.parse(rawBody) as ApiResponse<T>;
    } catch {
      body = null;
    }
  }
  if (!response.ok || body?.ok === false) {
    const message = body?.error?.message ?? rawBody.slice(0, 120) ?? response.statusText;
    throw new Error(`请求失败：${response.status}${message ? ` · ${message}` : ""}`);
  }
  if (!body) {
    throw new Error("接口返回格式异常，请稍后重试。");
  }
  return body.data;
}

export function getMatch(matchId: string): Promise<MatchSnapshot> {
  return request<MatchSnapshot>(`/api/matches/${matchId}`);
}

export function getCurrentMatchSummary(): Promise<CurrentMatchSummary> {
  return request<CurrentMatchSummary>("/api/current-match");
}

export function getVoteOptions(matchId: string): Promise<VoteOptions> {
  return request<VoteOptions>(`/api/public/matches/${matchId}/vote-options`);
}

export function post<T = MatchSnapshot>(path: string, body: object = {}): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: JSON.stringify(body)
  });
}

export function patch<T = MatchSnapshot>(path: string, body: object = {}): Promise<T> {
  return request<T>(path, {
    method: "PATCH",
    body: JSON.stringify(body)
  });
}

export function put<T = MatchSnapshot>(path: string, body: object = {}): Promise<T> {
  return request<T>(path, {
    method: "PUT",
    body: JSON.stringify(body)
  });
}

export function remove<T = MatchSnapshot>(path: string): Promise<T> {
  return request<T>(path, {
    method: "DELETE"
  });
}

export function getAuditLogs(matchId: string, limit = 12): Promise<{ items: AuditLog[] }> {
  return request<{ items: AuditLog[] }>(`/api/matches/${matchId}/audit-logs?limit=${limit}`);
}

export function getDataSummary(matchId: string): Promise<DataSummary> {
  return request<DataSummary>(`/api/matches/${matchId}/data-summary`);
}

export function getSpeechDiagnostics(matchId: string): Promise<SpeechDiagnostics> {
  return request<SpeechDiagnostics>(`/api/matches/${matchId}/speech/diagnostics`);
}

export function getPreflightReport(matchId: string): Promise<PreflightReport> {
  return request<PreflightReport>(`/api/matches/${matchId}/preflight-report`);
}

export function probeTts(matchId: string, text = "人机辩论赛语音合成自检。"): Promise<TTSProbeResult> {
  return post<TTSProbeResult>(`/api/matches/${matchId}/speech/tts/probe`, { text });
}

export function probeAsr(matchId: string): Promise<ASRProbeResult> {
  return post<ASRProbeResult>(`/api/matches/${matchId}/speech/asr/probe`, {});
}

export function recognizeArchivedSpeech(matchId: string, speechId: string): Promise<ASRArchiveRecognitionResult> {
  return post<ASRArchiveRecognitionResult>(`/api/matches/${matchId}/speeches/${speechId}/asr/recognize`, {});
}

export function createExportBundle(matchId: string): Promise<ExportBundle> {
  return post<ExportBundle>(`/api/matches/${matchId}/exports`);
}

export function getRuntimeAuthStatus(): Promise<RuntimeAuthStatus> {
  return request<RuntimeAuthStatus>("/api/admin/security/auth");
}

export function updateRuntimeAuthStatus(body: {
  auth_required: boolean;
  token_hashes?: Record<string, unknown>;
  tokens?: Record<string, unknown>;
  reason?: string;
}): Promise<RuntimeAuthStatus> {
  return request<RuntimeAuthStatus>("/api/admin/security/auth", {
    method: "PUT",
    body: JSON.stringify(body)
  });
}

export function withCurrentAuthQuery(path: string): string {
  const token = authTokenForCurrentPage();
  if (!token) return path;
  const url = new URL(path, window.location.origin);
  url.searchParams.set("token", token);
  return `${url.pathname}${url.search}${url.hash}`;
}

export function submitAudienceVote(matchId: string, body: AudienceVotePayload): Promise<{ received: boolean }> {
  return post<{ received: boolean }>(`/api/public/matches/${matchId}/audience-votes`, body);
}

export async function uploadAudioChunk(
  matchId: string,
  speechId: string,
  speakerId: string,
  chunkIndex: number,
  blob: Blob,
  durationMs?: number,
  filename?: string
): Promise<MatchSnapshot> {
  const token = authTokenForCurrentPage();
  const form = new FormData();
  form.set("speaker_id", speakerId);
  form.set("chunk_index", String(chunkIndex));
  if (durationMs !== undefined) form.set("duration_ms", String(durationMs));
  form.set("file", blob, filename ?? defaultAudioFilename(chunkIndex, blob.type));

  const response = await fetch(`${apiBase}/api/matches/${matchId}/speeches/${speechId}/audio-chunks`, {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    body: form
  });
  const body = (await response.json()) as ApiResponse<MatchSnapshot>;
  if (!response.ok || body.ok === false) {
    throw new Error(body.error?.message ?? `Request failed: ${response.status}`);
  }
  return body.data;
}

function defaultAudioFilename(chunkIndex: number, mimeType: string): string {
  const value = mimeType.toLowerCase();
  const extension = value.includes("l16") || value.includes("pcm") || value.includes("raw") ? "pcm" : "webm";
  return `chunk_${String(chunkIndex).padStart(5, "0")}.${extension}`;
}

export function websocketUrl(matchId: string, lastSeq: number, channel: string, speakerId?: string): string {
  const explicit = import.meta.env.VITE_WS_BASE as string | undefined;
  const base = explicit || `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}`;
  const params = new URLSearchParams({ channel, last_seq: String(lastSeq) });
  if (speakerId) params.set("speaker_id", speakerId);
  const token = authTokenForRole(channel === "speaker" ? "speaker" : channel === "admin" ? "admin" : channel === "host" ? "host" : "screen", speakerId);
  if (token) params.set("token", token);
  return `${base}/ws/matches/${matchId}?${params.toString()}`;
}

export type AuthRole = "admin" | "host" | "screen" | "speaker";

export function authStorageKey(role: AuthRole, speakerId?: string): string {
  if (role === "speaker") return `phdebate_auth_speaker_${speakerId ?? "unknown"}`;
  return `phdebate_auth_${role}`;
}

export function authTokenForRole(role: AuthRole, speakerId?: string): string {
  const query = new URLSearchParams(window.location.search);
  const queryToken = query.get("token") ?? query.get("auth_token") ?? query.get(`${role}_token`);
  const key = authStorageKey(role, speakerId);
  if (queryToken) {
    window.localStorage.setItem(key, queryToken);
    return queryToken;
  }
  return window.localStorage.getItem(key) ?? "";
}

export function saveAuthToken(role: AuthRole, token: string, speakerId?: string): void {
  window.localStorage.setItem(authStorageKey(role, speakerId), token.trim());
}

function authTokenForCurrentPage(): string {
  const path = window.location.pathname;
  if (path.startsWith("/admin")) return authTokenForRole("admin");
  if (path.startsWith("/host")) return authTokenForRole("host");
  if (path.startsWith("/console")) {
    const speakerId = path.split("/").filter(Boolean)[1];
    return authTokenForRole("speaker", speakerId);
  }
  if (path.startsWith("/screen") || path === "/") return authTokenForRole("screen");
  return "";
}
