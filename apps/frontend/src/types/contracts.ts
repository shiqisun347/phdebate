export type MatchStatus = "draft" | "ready" | "running" | "paused" | "intervention" | "finished" | "archived";
export type Side = "affirmative" | "negative" | "neutral";
export type SpeakerType = "human" | "agent";
export type ClockState = "idle" | "running" | "paused" | "expired" | "stopped";
export type ScreenScene = "idle" | "live" | "paused" | "audience_vote" | "judge_commentary" | "judge_result" | "audience_result" | "opening" | "teams" | "intermission" | "result" | "xiaoqi_commentary" | "xiaoqi_result" | "acknowledgment";
export type LiveMode = "single" | "free" | "prep";
export type AudioOutputMode = "host" | "admin" | "screen" | "off";

export type BrandDisplay = "text" | "image";

export interface MatchInfo {
  id: string;
  title: string;
  title_display: BrandDisplay;
  title_image_url: string;
  topic: string;
  affirmative_position: string;
  negative_position: string;
  organizer: string;
  organizer_display: BrandDisplay;
  organizer_image_url: string;
  venue: string;
  status: MatchStatus;
  screen_scene: ScreenScene;
  live_mode: LiveMode;
  current_phase_id: string;
  created_at?: string;
  updated_at?: string;
}

export interface CurrentMatchSummary {
  id: string;
  title: string;
  topic: string;
  status: MatchStatus;
  screen_scene: ScreenScene;
  current_phase_id: string;
}

export interface Team {
  id: string;
  side: Side;
  name: string;
  position: string;
  description: string;
}

export interface Speaker {
  id: string;
  team_id: string;
  side: Side;
  seat: number;
  name: string;
  speaker_type: SpeakerType;
  model_name: string | null;
  model_kind: "open_source" | "closed_source" | null;
  status: string;
  image_url?: string;
  agent_config_id?: string | null;
  agent_endpoint?: string;
  tts_voice_preset_id?: string | null;
  mic_permission?: "granted" | "denied" | "prompt" | "unknown" | null;
  device_label?: string | null;
  last_seen_at?: string | null;
  mic_error_message?: string;
}

export interface Phase {
  id: string;
  phase_key: string;
  name: string;
  phase_type: string;
  display_order: number;
  side: Side;
  speaker_seat: number | null;
  duration_seconds: number;
  side_total_seconds?: number;
  turn_seconds?: number;
  speaker_selector: string;
  status: string;
}

export interface Clock {
  id: string;
  phase_id: string;
  name: string;
  total_seconds: number;
  remaining_ms: number;
  state: ClockState;
  deadline_at: string | null;
}

export interface Speech {
  id: string;
  phase_id: string;
  speaker_id: string;
  side: Side;
  turn_index: number;
  source: "human_asr" | "agent_text" | "manual";
  state?: "speaking" | "paused" | "ended" | string;
  content_final: string;
  content_partial?: string;
  started_at: string | null;
  paused_at?: string | null;
  ended_at?: string | null;
  tts_task_id?: string | null;
  tts_expected_sentences?: number | null;
  tts_created_sentences?: number | null;
  tts_streaming_sentences?: number | null;
  tts_ready_sentences?: number | null;
  tts_playing_sentence_idx?: number | null;
  tts_played_sentences?: number | null;
  tts_played_sentence_indices?: number[] | null;
  tts_skipped_sentences?: number[] | null;
  tts_last_playback_status?: string | null;
  tts_last_progress_at?: string | null;
  tts_resume_requested_at?: string | null;
}

export interface TranscriptSegment {
  id: string;
  speech_id: string;
  phase_id: string;
  speaker_id: string;
  speaker_label: string;
  source: "human_asr" | "agent_text" | "manual";
  is_final: boolean;
  turn_index: number | null;
  valid: boolean;
  invalid_reason: string | null;
  text: string;
  created_at: string;
  updated_at?: string;
}

export interface SpeechRevision {
  id: string;
  speech_id: string;
  before_text: string;
  after_text: string;
  valid: boolean;
  reason: string;
  created_at: string;
  editor_actor_id: string;
}

export interface AudioAsset {
  id: string;
  match_id: string;
  phase_id: string;
  speech_id: string;
  speaker_id: string;
  file_path: string;
  mime_type: string;
  duration_ms: number | null;
  size_bytes: number;
  chunk_count: number;
  status: "recording" | "completed" | string;
  chunks?: Array<{
    chunk_index: number;
    file_path: string;
    audio_url?: string;
    size_bytes: number;
    mime_type: string;
    duration_ms: number | null;
    created_at: string;
  }>;
  created_at: string;
  updated_at: string;
  completed_at?: string;
}

// 分片上传接口的返回：后端只回这一片的归档结果(不再回传整张快照，避免长录音逐片回传 ~157KB 拖垮连接)。
export interface AudioChunkUploadResult {
  audio_asset_id: string;
  speech_id: string;
  speaker_id: string;
  chunk_index: number;
  chunk_count: number;
  size_bytes: number;
  file_path: string;
  pcm_ready: boolean;
  ignored_after_complete?: boolean; // 发言已完成后到达的迟到分片被良性忽略时为 true
}

export interface FlowState {
  awaiting_host_confirm: boolean;
  reason: string | null;
  message: string;
  next_action: "free_turn_next" | "phase_next" | "judge_commentary" | string | null;
  phase_id: string | null;
  speech_id: string | null;
  speaker_id: string | null;
  expired_clocks: string[];
  created_at: string | null;
}

export interface AudioOutputState {
  mode: AudioOutputMode;
  label: string;
  updated_by?: string;
  updated_at: string;
}

export interface AgentConfig {
  id: string;
  name: string;
  provider_type: "rest_api" | "openai_sdk" | string;
  request_method?: "GET" | "POST" | "PUT" | "PATCH" | string;
  model_name: string;
  model_id?: string;
  model_kind: "open_source" | "closed_source" | string;
  endpoint: string;
  base_url: string;
  api_key_env: string;
  timeout_ms: number;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface AgentStatus {
  speaker_id: string;
  agent_config_id?: string | null;
  name: string;
  model: string;
  status: "ready" | "streaming" | "failed" | string;
  last_heartbeat_seconds: number;
  detail: string;
  endpoint?: string;
  latency_ms?: number;
  last_health_at?: string;
  version?: string;
}

export interface VoteState {
  window_status: "open" | "closed";
  audience_count: number;
  judge_published: boolean;
  audience_published: boolean;
  winner_side: Side;
  best_speaker_id: string;
  /** 小七结果录入（获胜方 + 最佳辩手）是否已完成；大屏切「小七评判」前必须为 true。 */
  xiaoqi_recorded: boolean;
  xiaoqi_summary?: {
    winner_side: Side;
    best_speaker_id: string;
  };
  judge_summary: {
    constructive: { affirmative: number; negative: number };
    process: { affirmative: number; negative: number };
    conclusion: { affirmative: number; negative: number };
    computed_winner_side: Side;
    winner_side: Side;
    best_speaker_id: string;
  };
  audience_summary: {
    total: number;
    winner: { affirmative: number; negative: number };
    best_speaker: Array<{ speaker_id: string; count: number }>;
  };
}

export interface VoteOptions {
  match: Pick<MatchInfo, "id" | "title" | "topic" | "status">;
  teams: Array<Pick<Team, "id" | "side" | "name" | "position">>;
  speakers: Array<Pick<Speaker, "id" | "side" | "seat" | "name" | "speaker_type" | "image_url">>;
  vote_state: Pick<VoteState, "window_status" | "audience_count" | "judge_published" | "audience_published">;
}

export interface AudienceVotePayload {
  token?: string;
  winner_side: Side;
  /** 8 名辩手的完整排序，rank1 在前（后端按 Borda 计分聚合）。 */
  ranking: string[];
  /** 兼容字段：排名第一的辩手，由后端从 ranking[0] 推导，无需前端单独传。 */
  best_speaker_id?: string;
  client_fingerprint?: string;
}

export interface SpeechServiceState {
  asr: { status: string; latency_ms: number; active_sessions?: number; detail?: string };
  tts: {
    status: string;
    latency_ms: number;
    queue_size?: number;
    speaker_id?: string | null;
    detail?: string;
    degraded_to?: string;
    last_progress_at?: string;
  };
  screen: { status: string };
  consoles: {
    online: number;
    total: number;
    mic_errors?: Array<{
      speaker_id: string;
      name: string;
      mic_permission: string;
      message: string;
      last_seen_at: string | null;
    }>;
  };
}

export interface SpeechDiagnostics {
  checked_at: string;
  overall_status: "ready" | "mock_fallback" | "failed" | string;
  provider: "xfyun" | "alicloud" | "mock" | string | { asr?: string; tts?: string };
  asr: SpeechDiagnosticsComponent;
  tts: SpeechDiagnosticsComponent;
  audio_archive: {
    status: "ready" | "failed" | string;
    root_path: string;
    writable: boolean;
    detail: string;
  };
  auto_recognize?: {
    enabled: boolean;
    mode: "explicit_on" | "explicit_off" | "auto_when_ready" | string;
    detail: string;
  };
  realtime_asr?: {
    enabled: boolean;
    mode: "explicit_on" | "explicit_off" | "auto_when_ready" | string;
    detail: string;
  };
  formal_tts?: {
    enabled: boolean;
    mode: "explicit_on" | "explicit_off" | "auto_when_ready" | string;
    detail: string;
  };
  fallbacks: {
    mock_agent: boolean;
    manual_asr_controls: boolean;
    text_only_tts: boolean;
    audio_recording_without_asr: boolean;
  };
  next_steps: string[];
}

export interface SpeechDiagnosticsComponent {
  component: "asr" | "tts" | string;
  provider?: string;
  status: "ready" | "missing_config" | string;
  configured: string[];
  missing: string[];
  url: string;
  auth_ready?: boolean;
  auth_preview?: {
    host: string;
    request_line: string;
    auth_algorithm: string;
  } | null;
  runtime_config?: {
    open_timeout_s: number;
    close_timeout_s: number;
    final_timeout_s: number;
  };
  detail: string;
}

export type PreflightStatus = "ok" | "warn" | "fail" | string;

export interface PreflightCheck {
  id: string;
  label: string;
  status: PreflightStatus;
  detail: string;
  action: string;
}

export interface PreflightSection {
  id: string;
  label: string;
  status: PreflightStatus;
  checks: PreflightCheck[];
}

export interface PreflightReport {
  checked_at: string;
  overall_status: PreflightStatus;
  summary: string;
  score: {
    ok: number;
    warn: number;
    fail: number;
    total: number;
  };
  sections: PreflightSection[];
  next_actions: string[];
}

export interface TTSProbeResult {
  result: {
    probe: boolean;
    mime_type: string;
    size_bytes: number;
    chunk_count: number;
    latency_ms: number;
    file_path: string;
    audio_base64?: string;
  };
  snapshot: MatchSnapshot;
}

export interface AgentConfigTestResult {
  ok: boolean;
  content?: string;
  chunks?: number;
  latency_ms?: number;
  endpoint?: string;
  model?: string;
  error_code?: string;
  error_message?: string;
  details?: Record<string, unknown>;
  request?: Record<string, unknown>;
}

/** Multi-level log classification (性质 / 时机) attached to every log record. */
export type LogOrigin = "live" | "test" | string;
export interface LogClassification {
  origin?: LogOrigin;
  phase_id?: string | null;
  phase_name?: string | null;
  screen_scene?: string | null;
}

export interface LogPayloadSummary {
  request_preview?: string | null;
  response_preview?: string | null;
  request_bytes?: number | null;
  response_bytes?: number | null;
}

export interface AgentRequestLogSummary extends LogClassification, LogPayloadSummary {
  id: string;
  match_id?: string;
  task_id: string;
  speech_id: string | null;
  speaker_id: string;
  endpoint: string;
  status: string;
  error_code: string | null;
  error_message: string | null;
  latency_ms: number | null;
  started_at: string;
  completed_at: string | null;
  updated_at?: string | null;
}

export interface AgentRequestLog extends AgentRequestLogSummary {
  request: Record<string, unknown>;
  response_text: string | null;
}

export interface SpeechServiceRequestLogSummary extends LogClassification, LogPayloadSummary {
  id: string;
  match_id?: string;
  request_id: string;
  service: string;
  operation: string;
  speech_id: string | null;
  speaker_id: string | null;
  status: string;
  error_code: string | null;
  error_message: string | null;
  latency_ms: number | null;
  started_at: string;
  completed_at: string | null;
  updated_at?: string | null;
}

export interface SpeechServiceRequestLog extends SpeechServiceRequestLogSummary {
  request: Record<string, unknown>;
  response: Record<string, unknown>;
}

export interface AuditLogSummary extends LogClassification, LogPayloadSummary {
  id: string;
  match_id: string;
  actor_type: string;
  actor_id: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  result: string;
  error_message: string | null;
  created_at: string;
}

export interface RequestLogs {
  match_id: string;
  agent_requests: AgentRequestLogSummary[];
  speech_service_requests: SpeechServiceRequestLogSummary[];
  audit_logs: AuditLogSummary[];
}

export type RequestLogKind = "agent" | "speech" | "xiaoqi" | "audit";

export interface ASRProbeResult {
  result: {
    probe: boolean;
    text: string;
    text_length: number;
    latency_ms: number;
    chunk_count: number;
    audio_bytes: number;
  };
  snapshot: MatchSnapshot;
}

export interface ASRArchiveRecognitionResult {
  result: {
    speech_id: string;
    text: string;
    text_length: number;
    latency_ms: number;
    chunk_count: number;
    audio_bytes: number;
  };
  snapshot: MatchSnapshot;
}

export interface MatchListEntry {
  id: string;
  title?: string;
  topic?: string;
  status: string;
  screen_scene?: string;
  current_phase_id?: string;
  created_at?: string;
  updated_at?: string;
  active: boolean;
}

export interface MatchList {
  matches: MatchListEntry[];
  active_match_id: string;
}

export interface NextSpeaker {
  phase_id: string;
  phase_name: string;
  phase_type: string;
  speaker_id?: string;
  speaker_name?: string;
  speaker_type?: SpeakerType;
  side?: Side;
  seat?: number;
  label: string;
}

export interface IntegrationSecretStatus {
  configured: boolean;
  redacted: string;
}

export type SpeechProvider = "xfyun" | "alicloud" | "local_qwen" | "funasr";

export interface IntegrationSecretGroup {
  app_id?: IntegrationSecretStatus;
  api_key?: IntegrationSecretStatus;
  api_secret?: IntegrationSecretStatus;
  workspace_id?: IntegrationSecretStatus;
}

export interface IntegrationSection {
  enabled: boolean;
  provider: SpeechProvider | string;
  endpoint: string;
  lang?: string;
  voice?: string;
  settings?: Record<string, unknown>;
  secrets: {
    app_id: IntegrationSecretStatus;
    api_key: IntegrationSecretStatus;
    api_secret: IntegrationSecretStatus;
    xfyun?: IntegrationSecretGroup;
    alicloud?: IntegrationSecretGroup;
  };
}

export interface VoicePreset {
  id: string;
  name: string;
  provider: SpeechProvider | string;
  model: string;
  voice: string;
  response_format: string;
  mode: string;
  language_type: string;
  enabled: boolean;
  is_default: boolean;
  description?: string;
  sample_rate?: number;
  speech_rate?: number;
  volume?: number;
  pitch_rate?: number;
  instructions?: string;
}

export interface IntegrationConfig {
  asr: IntegrationSection;
  tts: IntegrationSection;
  voice_presets: VoicePreset[];
}

export interface LiveKitToken {
  enabled: boolean;
  configured: boolean;
  url: string;
  room: string;
  identity: string;
  role: string;
  token: string;
  expires_at: number;
}

export interface MatchSnapshot {
  match: MatchInfo;
  teams: Team[];
  speakers: Speaker[];
  phases: Phase[];
  clocks: Clock[];
  current_speech: Speech | null;
  next_speaker: NextSpeaker | null;
  integration_config: IntegrationConfig;
  free_debate: {
    current_turn_side: Side;
    turn_index: number;
    assignment_mode: string;
    skip_votes?: Record<string, string[]>;
    auto_handled?: Record<string, string>;
  };
  flow: FlowState;
  audio_output: AudioOutputState;
  recent_transcript: TranscriptSegment[];
  speech_revisions: SpeechRevision[];
  audio_assets: AudioAsset[];
  agent_configs: AgentConfig[];
  agent_status: AgentStatus[];
  vote_state: VoteState;
  speech_service: SpeechServiceState;
  xiaoqi?: { name: string; image_url: string; enabled: boolean };
  system?: {
    persistence?: {
      driver: string;
      database_path: string;
    };
  };
  last_seq: number;
}

export interface RulesetFlowNode {
  key: string;
  name: string;
  side: Side;
  speaker: string;
  duration_seconds: number;
  phase_type: string;
}

export interface Ruleset {
  id: string;
  name: string;
  summary: string;
  template: string;
  flow: RulesetFlowNode[];
  other_info: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface RulesetList {
  rulesets: Ruleset[];
  flow_template: string;
}

export interface GeneratedFlow {
  nodes: RulesetFlowNode[];
  warnings: string[];
  mermaid: string;
  ai_used: boolean;
  normalized_template?: string;
}

export type XiaoqiCommand = "intro" | "commentary" | "result" | "custom";

export interface XiaoqiConfig {
  enabled: boolean;
  name: string;
  image_url: string;
  endpoint: string;
  /** 给小七推送接口（celebration-api match_record/update）。比赛记录取自当前辩论实况。 */
  match_record_endpoint: string;
  session_id: string;
  request_method: string;
  api_key_env: string;
  timeout_ms: number;
  prompts: Record<XiaoqiCommand, string>;
  request_template: Record<string, unknown>;
  api_key_configured?: boolean;
  updated_at: string;
}

export interface XiaoqiCommandResult {
  sent: boolean;
  reason?: string;
  status_code?: number;
  response?: unknown;
  payload: Record<string, unknown>;
}

export interface ApiResponse<T> {
  ok: boolean;
  data: T;
  error?: { code: string; message: string; details?: unknown };
}

export interface RealtimeMessage {
  type: string;
  match_id: string;
  seq: number;
  server_time_ms: number;
  payload: Record<string, unknown>;
}

export interface AuditLog extends LogClassification {
  id: string;
  match_id: string;
  actor_type: string;
  actor_id: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  request: Record<string, unknown>;
  result: string;
  error_message: string | null;
  created_at: string;
}

export type RequestLogDetail = AgentRequestLog | SpeechServiceRequestLog | AuditLog;

export interface ExportEntry {
  path: string;
  size_bytes: number;
}

export interface ExportBundle {
  export_id: string;
  match_id: string;
  file_path: string;
  download_url: string;
  size_bytes: number;
  entries: ExportEntry[];
  created_at: string;
}

export interface CompactExportBundle {
  export_id: string;
  match_id: string;
  download_url: string;
  size_bytes: number;
  entry_count: number;
  entries?: ExportEntry[];
  created_at: string;
}

export interface DataSummaryArchive {
  id: string;
  archived_match_id: string;
  new_match_id: string;
  created_at: string;
  title: string;
  topic: string;
  counts: {
    transcript_segments: number;
    audio_assets: number;
    audience_votes: number;
  };
  export_bundle: CompactExportBundle | null;
}

export interface AgentRequestSummary {
  id: string;
  task_id: string;
  speech_id?: string | null;
  speaker_id: string;
  endpoint: string;
  status: string;
  error_code?: string | null;
  error_message?: string | null;
  latency_ms?: number | null;
  started_at: string;
  completed_at?: string | null;
}

export interface SpeechServiceRequestSummary {
  id: string;
  request_id: string;
  service: string;
  operation: string;
  speech_id?: string | null;
  speaker_id?: string | null;
  status: string;
  error_code?: string | null;
  error_message?: string | null;
  latency_ms?: number | null;
  started_at: string;
  completed_at?: string | null;
}

export interface EventSummary {
  id: string;
  match_id: string;
  seq: number;
  type: string;
  actor_type: string;
  actor_id?: string | null;
  created_at: string;
}

export interface DataSummary {
  generated_at: string;
  match: Pick<MatchInfo, "id" | "title" | "topic" | "status" | "screen_scene" | "current_phase_id">;
  persistence: {
    driver: string;
    database_path: string;
  };
  counts: {
    phases: number;
    speakers: number;
    human_speakers: number;
    agent_speakers: number;
    agent_configs: number;
    transcript_segments: number;
    final_transcript_segments: number;
    speech_revisions: number;
    audio_assets: number;
    audio_chunks: number;
    audience_votes: number;
    audience_vote_keys: number;
    agent_requests: number;
    speech_service_requests: number;
    export_bundles: number;
    events: number;
    audit_logs: number;
    archives: number;
  };
  structured_counts: {
    matches: number;
    phases: number;
    slots: number;
    speeches: number;
    transcript_segments: number;
    speech_revisions: number;
    agent_configs: number;
    agent_status: number;
    audio_assets: number;
    audio_chunks: number;
    votes: number;
    runtime_settings: number;
    agent_requests: number;
    speech_service_requests: number;
    export_bundles: number;
  };
  request_health: {
    agent_status_counts: Record<string, number>;
    speech_service_status_counts: Record<string, number>;
    recent_agent_requests: AgentRequestSummary[];
    recent_speech_service_requests: SpeechServiceRequestSummary[];
    failed_agent_requests: AgentRequestSummary[];
    failed_speech_service_requests: SpeechServiceRequestSummary[];
  };
  event_type_counts: Record<string, number>;
  recent_events: EventSummary[];
  latest_event: {
    id: string;
    match_id: string;
    seq: number;
    type: string;
    payload: Record<string, unknown>;
    actor_type: string;
    actor_id?: string | null;
    created_at: string;
  } | null;
  latest_export: CompactExportBundle | null;
  archives: DataSummaryArchive[];
}

export interface RuntimeAuthStatus {
  auth_required: boolean;
  runtime_configured: boolean;
  env_default_auth_required: boolean;
  runtime_path: string;
  token_file_path: string | null;
  roles: string[];
  token_sources: Record<string, {
    env?: boolean;
    runtime_count?: number;
    file_count?: number;
    env_count?: number;
  }>;
  updated_at?: number;
  updated_by?: string;
}
