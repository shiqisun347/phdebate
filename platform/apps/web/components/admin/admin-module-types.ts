export type Review = {
  scorecard_id: string;
  match_id: string;
  room_code: string;
  topic: string;
  status: "review_required" | "approved";
  winner: "aff" | "neg" | "draw" | null;
  affirmative_score: number;
  negative_score: number;
  reason: string;
  updated_at: string;
};

export type AdminSpeechCorrection = {
  id: string;
  speech_id: string;
  room_id: string;
  room_code: string;
  topic: string;
  seat_key: string;
  requester_name: string;
  requester_user_id: string;
  original_content: string;
  proposed_content: string;
  reason: string;
  status: "pending" | "approved" | "rejected" | "cancelled";
  review_reason: string;
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
};

export type Audit = {
  id: string;
  actor_name: string;
  action: string;
  target_type: string;
  target_id: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type MediaStatus = {
  scanned_files: number;
  scanned_bytes: number;
  referenced_files: number;
  referenced_existing_files: number;
  missing_reference_count: number;
  missing_references: string[];
  orphan_candidate_count: number;
  orphan_candidate_bytes: number;
  orphan_candidates: { path: string; bytes: number; modified_at: string }[];
  unsafe_entries: number;
  truncated: boolean;
  disk_total_bytes: number;
  disk_used_bytes: number;
  disk_free_bytes: number;
  deleted_files: number;
  deleted_bytes: number;
};

export type ArchiveStatus = {
  scanned_files: number;
  scanned_bytes: number;
  expected_matches: number;
  complete_archives: number;
  invalid_archive_count: number;
  invalid_archives: { match_id: string; reason: string }[];
  orphan_candidate_count: number;
  orphan_candidate_bytes: number;
  orphan_candidates: { path: string; bytes: number; modified_at: string }[];
  unsafe_entries: number;
  unmanaged_entries: number;
  truncated: boolean;
  deleted_files: number;
  deleted_bytes: number;
};

export type DataQualityStatus = {
  scope: "production";
  matches: {
    total: number;
    active: number;
    completed: number;
    review_required: number;
    terminated: number;
  };
  speeches: {
    human_completed: number;
    human_with_transcript: number;
    human_with_audio: number;
    ai_completed: number;
    transcript_coverage_percent: number;
    audio_coverage_percent: number;
  };
  attention: {
    published_without_scorecard: number;
    published_without_speeches: number;
    human_missing_transcript: number;
    human_missing_audio: number;
    human_missing_segments: number;
    unreviewed_sample_issues: number;
    samples: {
      room_code: string;
      match_id: string;
      speech_id: string;
      seat_key: string;
      stage_key: string;
      issues: string[];
      dispositions: Record<string, {
        status: "needs_recollection" | "unrecoverable" | "verified";
        note: string;
        reviewed_at: string;
        updated_at: string;
      }>;
    }[];
  };
};

export type AutomationTemplate = {
  id: string;
  slug: string;
  name: string;
  version: number;
  stages: Record<string, unknown>[];
  is_active: boolean;
  competitions: { id: string; name: string }[];
  created_at: string;
};

export type AudioCue = {
  id: string;
  key: string;
  name: string;
  text: string;
  audio_url: string;
  is_active: boolean;
  updated_at: string;
};

export type JudgeProfile = {
  id: string;
  name: string;
  endpoint: string;
  model_name: string;
  system_prompt: string;
  timeout_seconds: number;
  is_active: boolean;
  created_at: string;
  updated_at: string;
};

export type ProviderConfig = {
  id: string;
  kind: "agent" | "funasr" | "lighttts";
  endpoint: string;
  settings: Record<string, number>;
  has_secret?: boolean;
  is_active: boolean;
  created_at: string;
  updated_at: string;
};

export type AgentProfileSummary = {
  id: string;
  name: string;
  profile_key: string;
  provider: string;
  voice_id: string;
  is_active: boolean;
};

export type SeasonForm = {
  name: string;
  slug: string;
  starts_at: string;
  ends_at: string;
};

export type JudgeForm = {
  name: string;
  endpoint: string;
  model_name: string;
  system_prompt: string;
  timeout_seconds: number;
  is_active: boolean;
};

export const bytes = (value: number) =>
  value < 1024
    ? `${value} B`
    : value < 1024 ** 2
      ? `${(value / 1024).toFixed(1)} KiB`
      : value < 1024 ** 3
        ? `${(value / 1024 ** 2).toFixed(1)} MiB`
        : `${(value / 1024 ** 3).toFixed(2)} GiB`;

export const auditActionLabels: Record<string, string> = {
  "archives.repair": "修复比赛归档",
  "automation_template.version_create": "创建自动流程版本",
  "judge.correct": "修正比赛结果",
  "judge.retry": "重试 AI 裁判",
  "judge_profile.create": "创建裁判配置",
  "judge_profile.patch": "修改裁判配置",
  "match.judge_snapshot.backfill": "补齐裁判配置快照",
  "provider_config.patch": "修改服务配置",
  "provider_config.test": "测试服务连接",
  "qa_provisioning.apply": "执行 QA 验收配置",
  "room.data_scope.patch": "修改比赛数据类型",
  "seat.restore_human": "恢复真人席位",
  "topic.create": "添加赛事题目",
  "topic.patch": "修改赛事题目",
  "user.patch": "修改用户",
  "user.password_reset": "重置用户密码",
};

export const auditTargetLabels: Record<string, string> = {
  archive_storage: "比赛归档",
  automation_template: "自动流程",
  judge_profile: "裁判配置",
  match: "比赛",
  provider_config: "服务配置",
  qa_provisioning: "QA 验收",
  room: "房间",
  room_seat: "比赛席位",
  scorecard: "裁判结果",
  topic: "赛事题目",
  user: "用户",
};
