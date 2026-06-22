import type { AgentConfigTestResult, ApiResponse, ASRArchiveRecognitionResult, ASRProbeResult, AudioChunkUploadResult, AuditLog, AudienceVotePayload, CurrentMatchSummary, DataSummary, ExportBundle, GeneratedFlow, IntegrationConfig, LiveKitToken, MatchList, MatchSnapshot, PreflightReport, RequestLogDetail, RequestLogKind, RequestLogs, Ruleset, RulesetList, RuntimeAuthStatus, SpeechDiagnostics, TTSProbeResult, VoteOptions, XiaoqiCommandResult, XiaoqiConfig } from "../types/contracts";

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

export function listMatches(): Promise<MatchList> {
  return request<MatchList>(`/api/matches`);
}

export function createMatch(body: { title?: string; topic?: string }): Promise<{ match_id: string; status: string }> {
  return post<{ match_id: string; status: string }>(`/api/matches`, body);
}

export function switchMatch(matchId: string): Promise<MatchSnapshot> {
  return post<MatchSnapshot>(`/api/matches/${matchId}/switch`, {});
}

export function deleteMatch(matchId: string): Promise<MatchList> {
  return remove<MatchList>(`/api/matches/${matchId}`);
}

export function getIntegrationConfig(matchId: string): Promise<IntegrationConfig> {
  return request<IntegrationConfig>(`/api/matches/${matchId}/integration-config`);
}

export function createLiveKitToken(matchId: string, body: { role: string; speaker_id?: string; ttl_seconds?: number }): Promise<LiveKitToken> {
  return post<LiveKitToken>(`/api/matches/${matchId}/livekit/token`, body);
}

export function startVoiceAgent(matchId: string, speechId: string, speakerId?: string): Promise<Record<string, unknown>> {
  return post<Record<string, unknown>>(`/api/matches/${matchId}/speeches/${speechId}/voice-agent/start`, speakerId ? { speaker_id: speakerId } : {});
}

export function stopVoiceAgent(matchId: string, speechId: string, speakerId?: string): Promise<Record<string, unknown>> {
  return post<Record<string, unknown>>(`/api/matches/${matchId}/speeches/${speechId}/voice-agent/stop`, speakerId ? { speaker_id: speakerId } : {});
}

export function patchIntegrationConfig(matchId: string, body: Partial<Record<"asr" | "tts" | "voice_presets", unknown>>): Promise<IntegrationConfig> {
  return patch<IntegrationConfig>(`/api/matches/${matchId}/integration-config`, body);
}

export function getPreflightReport(matchId: string): Promise<PreflightReport> {
  return request<PreflightReport>(`/api/matches/${matchId}/preflight-report`);
}

export function probeTts(matchId: string, text = "人机辩论赛语音合成自检。", voicePresetId = ""): Promise<TTSProbeResult> {
  return post<TTSProbeResult>(`/api/matches/${matchId}/speech/tts/probe`, { text, voice_preset_id: voicePresetId });
}

export function probeAsr(
  matchId: string,
  audioBase64?: string,
  format = "audio/L16;rate=16000"
): Promise<ASRProbeResult> {
  return post<ASRProbeResult>(
    `/api/matches/${matchId}/speech/asr/probe`,
    audioBase64 ? { audio_base64: audioBase64, format, encoding: "raw" } : {}
  );
}

export function testAgentConfig(
  matchId: string,
  configId: string,
  payload?: Record<string, unknown>
): Promise<AgentConfigTestResult> {
  return post<AgentConfigTestResult>(
    `/api/matches/${matchId}/agents/configs/${configId}/test`,
    payload ? { payload } : {}
  );
}

export function testAgentConfigInline(
  matchId: string,
  config: Record<string, unknown>,
  payload?: Record<string, unknown>
): Promise<AgentConfigTestResult> {
  return post<AgentConfigTestResult>(`/api/matches/${matchId}/agents/configs/test-inline`, { config, payload });
}

export function getRequestLogs(matchId: string, limit = 200): Promise<RequestLogs> {
  return request<RequestLogs>(`/api/matches/${matchId}/logs?limit=${limit}`);
}

export function getRequestLogDetail(matchId: string, kind: RequestLogKind, id: string): Promise<RequestLogDetail> {
  return request<RequestLogDetail>(`/api/matches/${matchId}/logs/${kind}/${encodeURIComponent(id)}`);
}

export function clearRequestLogs(matchId: string): Promise<RequestLogs> {
  return remove<RequestLogs>(`/api/matches/${matchId}/logs`);
}

export function testWsUrl(kind: "asr" | "tts", matchId: string): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/ws/${kind}-test/${matchId}`;
}

export function recognizeArchivedSpeech(matchId: string, speechId: string): Promise<ASRArchiveRecognitionResult> {
  return post<ASRArchiveRecognitionResult>(`/api/matches/${matchId}/speeches/${speechId}/asr/recognize`, {});
}

export function createExportBundle(matchId: string): Promise<ExportBundle> {
  return post<ExportBundle>(`/api/matches/${matchId}/exports`);
}

export function listRulesets(): Promise<RulesetList> {
  return request<RulesetList>("/api/admin/rulesets");
}

export function createRuleset(body: Partial<Ruleset>): Promise<Ruleset> {
  return post<Ruleset>("/api/admin/rulesets", body);
}

export function updateRuleset(id: string, body: Partial<Ruleset>): Promise<Ruleset> {
  return patch<Ruleset>(`/api/admin/rulesets/${id}`, body);
}

export function deleteRuleset(id: string): Promise<{ rulesets: Ruleset[] }> {
  return remove<{ rulesets: Ruleset[] }>(`/api/admin/rulesets/${id}`);
}

export function generateRulesetFlow(template: string, useAi = true): Promise<GeneratedFlow> {
  return post<GeneratedFlow>("/api/admin/rulesets/generate-flow", { template, use_ai: useAi });
}

export function getXiaoqi(): Promise<XiaoqiConfig> {
  return request<XiaoqiConfig>("/api/admin/xiaoqi");
}

export function updateXiaoqi(body: Partial<XiaoqiConfig>): Promise<XiaoqiConfig> {
  return put<XiaoqiConfig>("/api/admin/xiaoqi", body);
}

export function sendXiaoqiCommand(body: {
  command: string;
  question?: string;
  context?: Record<string, unknown>;
}): Promise<XiaoqiCommandResult> {
  return post<XiaoqiCommandResult>("/api/admin/xiaoqi/command", body);
}

export function pushXiaoqiMatchRecord(matchId: string): Promise<XiaoqiCommandResult> {
  return post<XiaoqiCommandResult>(`/api/matches/${matchId}/xiaoqi/match-record`, {});
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
): Promise<AudioChunkUploadResult> {
  const token = authTokenForCurrentPage();
  const url = `${apiBase}/api/matches/${matchId}/speeches/${speechId}/audio-chunks`;
  // 录音分片上传带退避重试：现场弱网下单次 fetch 抖动很常见，不重试会直接丢分片→录音/转写残缺。
  // 网络错误与 5xx 才重试；4xx（如说话人不符）是确定性错误，不重试。后端对“已完成发言”的迟到
  // 分片是幂等良性忽略，所以重试安全。
  const maxAttempts = 3;
  let lastErr: unknown;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const form = new FormData();
    form.set("speaker_id", speakerId);
    form.set("chunk_index", String(chunkIndex));
    if (durationMs !== undefined) form.set("duration_ms", String(durationMs));
    form.set("file", blob, filename ?? defaultAudioFilename(chunkIndex, blob.type));

    let response: Response | null = null;
    let body: ApiResponse<AudioChunkUploadResult> | null = null;
    try {
      response = await fetch(url, {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        body: form
      });
      body = (await response.json()) as ApiResponse<AudioChunkUploadResult>;
    } catch (err) {
      lastErr = err; // 网络层抖动 → 可重试
    }

    if (response && body) {
      if (response.ok && body.ok !== false) return body.data;
      const message = body.error?.message ?? `Request failed: ${response.status}`;
      if (response.status < 500) throw new Error(message); // 4xx 确定性错误：不重试
      lastErr = new Error(message); // 5xx：可重试
    }

    if (attempt < maxAttempts - 1) {
      await new Promise((resolve) => setTimeout(resolve, 300 * 2 ** attempt));
    }
  }
  throw lastErr instanceof Error ? lastErr : new Error("录音分片上传失败");
}

export async function uploadSpeakerImage(matchId: string, speakerId: string, file: File): Promise<MatchSnapshot> {
  const token = authTokenForCurrentPage();
  const form = new FormData();
  form.set("file", file, file.name);
  const response = await fetch(`${apiBase}/api/matches/${matchId}/speakers/${speakerId}/image`, {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    body: form
  });
  const body = (await response.json()) as ApiResponse<MatchSnapshot>;
  if (!response.ok || body.ok === false) {
    throw new Error(body.error?.message ?? `上传失败：${response.status}`);
  }
  return body.data;
}

export async function uploadMatchImage(matchId: string, kind: "title" | "organizer", file: File): Promise<MatchSnapshot> {
  const token = authTokenForCurrentPage();
  const form = new FormData();
  form.set("file", file, file.name);
  const response = await fetch(`${apiBase}/api/matches/${matchId}/image/${kind}`, {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    body: form
  });
  const body = (await response.json()) as ApiResponse<MatchSnapshot>;
  if (!response.ok || body.ok === false) {
    throw new Error(body.error?.message ?? `上传失败：${response.status}`);
  }
  return body.data;
}

/** Edit/correct a past speech (transcript) segment; edited text flows into future agent debate_history. */
export function patchSpeech(matchId: string, speechId: string, body: { text?: string; content_final?: string; valid?: boolean; reason?: string }): Promise<MatchSnapshot> {
  return patch<MatchSnapshot>(`/api/matches/${matchId}/speeches/${speechId}`, body);
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

export function ttsLiveWsUrl(matchId: string, speechId: string, taskId: string, sentenceIdx: number): string {
  const explicit = import.meta.env.VITE_WS_BASE as string | undefined;
  const base = explicit || `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}`;
  const params = new URLSearchParams();
  const token = authTokenForRole("screen");
  if (token) params.set("token", token);
  return `${base}/ws/tts-live/${matchId}/${speechId}/${taskId}/${sentenceIdx}?${params.toString()}`;
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
