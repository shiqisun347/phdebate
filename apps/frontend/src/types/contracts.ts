export type MatchStatus = "draft" | "ready" | "running" | "paused" | "intervention" | "finished" | "archived";
export type Side = "affirmative" | "negative" | "neutral";
export type SpeakerType = "human" | "agent";
export type ClockState = "idle" | "running" | "paused" | "expired" | "stopped";
export type ScreenScene = "idle" | "opening" | "teams" | "live" | "intermission" | "result";
export type LiveMode = "single" | "free" | "prep";

export interface MatchInfo {
  id: string;
  title: string;
  topic: string;
  affirmative_position: string;
  negative_position: string;
  organizer: string;
  venue: string;
  status: MatchStatus;
  screen_scene: ScreenScene;
  live_mode: LiveMode;
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
  agent_endpoint?: string;
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
  content_final: string;
  content_partial?: string;
  started_at: string | null;
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
    size_bytes: number;
    mime_type: string;
    duration_ms: number | null;
    created_at: string;
  }>;
  created_at: string;
  updated_at: string;
  completed_at?: string;
}

export interface AgentStatus {
  speaker_id: string;
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
  match: Pick<MatchInfo, "id" | "title" | "topic">;
  teams: Array<Pick<Team, "id" | "side" | "name" | "position">>;
  speakers: Array<Pick<Speaker, "id" | "side" | "seat" | "name" | "speaker_type">>;
  vote_state: Pick<VoteState, "window_status" | "audience_count" | "judge_published" | "audience_published">;
}

export interface AudienceVotePayload {
  token?: string;
  winner_side: Side;
  best_speaker_id: string;
  client_fingerprint?: string;
}

export interface SpeechServiceState {
  asr: { status: string; latency_ms: number; active_sessions?: number; detail?: string };
  tts: { status: string; latency_ms: number; queue_size?: number; speaker_id?: string | null; detail?: string; degraded_to?: string };
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
  provider: "xfyun" | "mock" | string;
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
  };
  snapshot: MatchSnapshot;
}

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

export interface MatchSnapshot {
  match: MatchInfo;
  teams: Team[];
  speakers: Speaker[];
  phases: Phase[];
  clocks: Clock[];
  current_speech: Speech | null;
  free_debate: {
    current_turn_side: Side;
    turn_index: number;
    assignment_mode: string;
  };
  recent_transcript: TranscriptSegment[];
  speech_revisions: SpeechRevision[];
  audio_assets: AudioAsset[];
  agent_status: AgentStatus[];
  vote_state: VoteState;
  speech_service: SpeechServiceState;
  system?: {
    persistence?: {
      driver: string;
      database_path: string;
    };
  };
  last_seq: number;
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

export interface AuditLog {
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
