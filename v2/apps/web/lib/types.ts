export type User = {
  id: string;
  account: string;
  real_name: string;
  role: "user" | "system_admin";
  is_active: boolean;
  is_test_account?: boolean;
};

export type Season = {
  id: string;
  name: string;
  slug: string;
  starts_at: string;
  ends_at: string | null;
  is_active: boolean;
  is_open: boolean;
  created_at: string;
  updated_at: string;
  competition_count?: number;
  match_count?: number;
};

export type Competition = {
  id: string;
  slug: string;
  name: string;
  tagline: string;
  description: string;
  rules: string;
  format: string;
  seat_count: number;
  ranked: boolean;
  allow_custom_topic: boolean;
  accent: string;
  live_count: number;
  is_active?: boolean;
  season?: Season | null;
  topics?: { id: string; title: string; is_active?: boolean }[];
};

export type RoomSeat = {
  seat_key: string;
  side: "aff" | "neg";
  position: number;
  label: string;
  occupant_type: "open" | "human" | "ai" | "ai_substitute";
  display_name: string;
  is_ready: boolean;
  connected: boolean;
  is_me: boolean;
};

export type SeatRestoreRequest = {
  id: string;
  seat_key: string;
  seat_label: string;
  requester: { id: string; real_name: string };
  requester_connected: boolean;
  status: "pending" | "approved" | "rejected" | "cancelled" | "expired";
  resolution_reason: string;
  created_at: string;
  resolved_at: string | null;
  can_cancel: boolean;
  can_review: boolean;
};

export type Stage = {
  key: string;
  name: string;
  kind: "announcement" | "speech" | "free" | "judging";
  duration: number;
  seat?: string;
  side?: "aff" | "neg";
  cue?: string;
  turn_duration?: number;
  turn_started_at?: string;
  ai_preparing?: boolean;
  preparing_stage_remaining_seconds?: number;
  preparing_turn_remaining_seconds?: number;
};

/** @deprecated Dormant compatibility shape; current competition APIs never emit it. */
export type RecordingConsent = {
  organization_id: string | null;
  required: boolean;
  granted: boolean;
  policy: { id: string; version: number; title: string; content: string; effective_at?: string } | null;
  decision: "grant" | "revoke" | "system_admin" | null;
  decided_at: string | null;
  actor_user_id: string | null;
  record_id: string | null;
};

export type Room = {
  id: string;
  code: string;
  topic: string;
  status: string;
  visibility: string;
  is_test_data?: boolean;
  seq: number;
  competition: Competition;
  season: Season | null;
  owner: { id?: string; real_name: string };
  seats: RoomSeat[];
  current_stage: Stage | null;
  current_stage_index: number;
  remaining_seconds: number | null;
  turn_remaining_seconds: number | null;
  active_speech: { id: string; seat_key: string; speaker_type: string; status: string; content: string; playback_started_at?: string | null; stream_generation?: string; stream_sample_rate?: number } | null;
  my_seat: string | null;
  can_speak: boolean;
  speak_reason: string;
  can_control: boolean;
  /** @deprecated Not emitted by the competition-only backend. */
  recording_consent?: RecordingConsent;
  seat_restore_requests?: SeatRestoreRequest[];
  failure_reason?: string;
  recent_events: { seq: number; type: string; payload: Record<string, unknown>; created_at: string }[];
  speeches: { id: string; seat_key: string; speaker: string; stage_key: string; content: string; audio_url: string; duration_seconds: number; playback_started_at: string | null; playback_ends_at: string | null; stream_generation?: string; stream_sample_rate?: number; status: string; created_at: string }[];
};

export type Ranking = {
  rank: number;
  user_id: string;
  real_name: string;
  points: number;
  wins: number;
  draws: number;
  losses: number;
  matches: number;
  average_score: number;
  last_match_at?: string | null;
  season_id?: string;
};
