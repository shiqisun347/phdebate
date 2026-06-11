# 02 · Data Model

本章给出 MVP SQLite 数据模型。字段名是后端、前端、事件 payload 的共同契约；后续迁移 PostgreSQL 时应保持语义不变。

## 1. 通用约定

- 主键为字符串 ID，格式见 `00-overview.md`。
- `created_at`、`updated_at` 为 UTC ISO8601 文本。
- JSON 字段使用 SQLite `TEXT` 存储，内容必须是合法 JSON。
- 外键在 SQLite 初始化时启用：`PRAGMA foreign_keys = ON;`。
- 事件不删除；回退、修正、作废均追加新记录。

## 2. DDL

```sql
CREATE TABLE matches (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  topic TEXT NOT NULL,
  affirmative_position TEXT NOT NULL,
  negative_position TEXT NOT NULL,
  organizer TEXT,
  venue TEXT,
  scheduled_at TEXT,
  status TEXT NOT NULL CHECK (status IN (
    'draft','ready','running','paused','intervention','finished','archived'
  )),
  current_phase_id TEXT,
  screen_scene TEXT NOT NULL DEFAULT 'idle',
  live_mode TEXT,
  config_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (current_phase_id) REFERENCES phases(id)
);

CREATE TABLE teams (
  id TEXT PRIMARY KEY,
  match_id TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('affirmative','negative')),
  name TEXT NOT NULL,
  position TEXT NOT NULL,
  description TEXT,
  display_order INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE,
  UNIQUE (match_id, side)
);

CREATE TABLE speakers (
  id TEXT PRIMARY KEY,
  match_id TEXT NOT NULL,
  team_id TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('affirmative','negative')),
  seat INTEGER NOT NULL CHECK (seat BETWEEN 1 AND 4),
  name TEXT NOT NULL,
  speaker_type TEXT NOT NULL CHECK (speaker_type IN ('human','agent')),
  model_name TEXT,
  model_kind TEXT CHECK (model_kind IN ('open_source','closed_source')),
  agent_endpoint TEXT,
  avatar_url TEXT,
  console_token_hash TEXT,
  status TEXT NOT NULL DEFAULT 'offline',
  config_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE,
  FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE,
  UNIQUE (match_id, side, seat)
);

CREATE TABLE phases (
  id TEXT PRIMARY KEY,
  match_id TEXT NOT NULL,
  phase_key TEXT NOT NULL,
  name TEXT NOT NULL,
  phase_type TEXT NOT NULL CHECK (phase_type IN (
    'constructive','statement','free_debate','summary','commentary'
  )),
  display_order INTEGER NOT NULL,
  side TEXT CHECK (side IN ('affirmative','negative','neutral')),
  speaker_seat INTEGER CHECK (speaker_seat BETWEEN 1 AND 4),
  duration_seconds INTEGER NOT NULL,
  speaker_selector TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN (
    'pending','active','speaker_active','waiting_next_speaker',
    'completed','skipped','reopened'
  )),
  rules_json TEXT NOT NULL DEFAULT '{}',
  started_at TEXT,
  ended_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE,
  UNIQUE (match_id, display_order),
  UNIQUE (match_id, phase_key)
);

CREATE TABLE clocks (
  id TEXT PRIMARY KEY,
  match_id TEXT NOT NULL,
  phase_id TEXT NOT NULL,
  name TEXT NOT NULL,
  total_seconds INTEGER NOT NULL,
  remaining_ms INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('idle','running','paused','expired','stopped')),
  deadline_at TEXT,
  started_at TEXT,
  paused_at TEXT,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE,
  FOREIGN KEY (phase_id) REFERENCES phases(id) ON DELETE CASCADE,
  UNIQUE (phase_id, name)
);

CREATE TABLE speeches (
  id TEXT PRIMARY KEY,
  match_id TEXT NOT NULL,
  phase_id TEXT NOT NULL,
  speaker_id TEXT,
  side TEXT NOT NULL CHECK (side IN ('affirmative','negative','neutral')),
  turn_index INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL CHECK (source IN ('human_asr','agent_text','manual')),
  content_raw TEXT NOT NULL DEFAULT '',
  content_final TEXT NOT NULL DEFAULT '',
  started_at TEXT,
  ended_at TEXT,
  duration_ms INTEGER,
  valid INTEGER NOT NULL DEFAULT 1,
  invalid_reason TEXT,
  audio_asset_id TEXT,
  agent_task_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE,
  FOREIGN KEY (phase_id) REFERENCES phases(id) ON DELETE CASCADE,
  FOREIGN KEY (speaker_id) REFERENCES speakers(id),
  FOREIGN KEY (audio_asset_id) REFERENCES audio_assets(id),
  FOREIGN KEY (agent_task_id) REFERENCES agent_tasks(id)
);

CREATE TABLE transcript_segments (
  id TEXT PRIMARY KEY,
  match_id TEXT NOT NULL,
  speech_id TEXT,
  phase_id TEXT NOT NULL,
  speaker_id TEXT,
  source TEXT NOT NULL CHECK (source IN ('human_asr','agent_text','manual')),
  is_final INTEGER NOT NULL,
  text TEXT NOT NULL,
  start_ms INTEGER,
  end_ms INTEGER,
  seq INTEGER,
  created_at TEXT NOT NULL,
  FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE,
  FOREIGN KEY (speech_id) REFERENCES speeches(id),
  FOREIGN KEY (phase_id) REFERENCES phases(id),
  FOREIGN KEY (speaker_id) REFERENCES speakers(id)
);

CREATE TABLE speech_revisions (
  id TEXT PRIMARY KEY,
  speech_id TEXT NOT NULL,
  editor_actor_id TEXT NOT NULL,
  before_text TEXT NOT NULL,
  after_text TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (speech_id) REFERENCES speeches(id) ON DELETE CASCADE
);

CREATE TABLE audio_assets (
  id TEXT PRIMARY KEY,
  match_id TEXT NOT NULL,
  speech_id TEXT,
  speaker_id TEXT,
  file_path TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  duration_ms INTEGER,
  size_bytes INTEGER,
  chunk_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'recording' CHECK (status IN ('recording','completed','failed')),
  chunks_json TEXT NOT NULL DEFAULT '[]',
  completed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE,
  FOREIGN KEY (speech_id) REFERENCES speeches(id),
  FOREIGN KEY (speaker_id) REFERENCES speakers(id)
);

CREATE TABLE agent_tasks (
  id TEXT PRIMARY KEY,
  match_id TEXT NOT NULL,
  phase_id TEXT NOT NULL,
  speaker_id TEXT NOT NULL,
  speech_id TEXT,
  task_type TEXT NOT NULL DEFAULT 'official' CHECK (task_type IN ('official','prefetch','speculative')),
  status TEXT NOT NULL CHECK (status IN (
    'created','sent','streaming','completed','failed','timeout','interrupted','discarded'
  )),
  endpoint TEXT NOT NULL,
  request_json TEXT NOT NULL,
  response_json TEXT,
  error_message TEXT,
  first_delta_at TEXT,
  completed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE,
  FOREIGN KEY (phase_id) REFERENCES phases(id),
  FOREIGN KEY (speaker_id) REFERENCES speakers(id),
  FOREIGN KEY (speech_id) REFERENCES speeches(id)
);

CREATE TABLE events (
  id TEXT PRIMARY KEY,
  match_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  actor_type TEXT NOT NULL CHECK (actor_type IN (
    'system','admin','host','speaker','agent','audience'
  )),
  actor_id TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE,
  UNIQUE (match_id, seq)
);

CREATE TABLE votes (
  id TEXT PRIMARY KEY,
  match_id TEXT NOT NULL,
  voter_type TEXT NOT NULL CHECK (voter_type IN ('judge','audience','ai_judge')),
  voter_id TEXT,
  vote_type TEXT NOT NULL CHECK (vote_type IN (
    'constructive','process','conclusion','winner','best_speaker'
  )),
  target_side TEXT CHECK (target_side IN ('affirmative','negative')),
  target_speaker_id TEXT,
  comment TEXT,
  published INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE,
  FOREIGN KEY (target_speaker_id) REFERENCES speakers(id)
);

CREATE TABLE audience_vote_tokens (
  id TEXT PRIMARY KEY,
  match_id TEXT NOT NULL,
  token_hash TEXT NOT NULL,
  used_at TEXT,
  client_fingerprint TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE,
  UNIQUE (match_id, token_hash)
);

CREATE TABLE vote_windows (
  id TEXT PRIMARY KEY,
  match_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('closed','open')),
  opened_at TEXT,
  closed_at TEXT,
  judge_published_at TEXT,
  audience_published_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE
);

CREATE TABLE audit_logs (
  id TEXT PRIMARY KEY,
  match_id TEXT,
  actor_type TEXT NOT NULL,
  actor_id TEXT,
  action TEXT NOT NULL,
  target_type TEXT,
  target_id TEXT,
  request_json TEXT NOT NULL DEFAULT '{}',
  result TEXT NOT NULL CHECK (result IN ('success','failure')),
  error_message TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE SET NULL
);

CREATE INDEX idx_events_match_seq ON events(match_id, seq);
CREATE INDEX idx_phases_match_order ON phases(match_id, display_order);
CREATE INDEX idx_speeches_match_phase ON speeches(match_id, phase_id, turn_index);
CREATE INDEX idx_transcript_match_seq ON transcript_segments(match_id, seq);
CREATE INDEX idx_votes_match_type ON votes(match_id, voter_type, vote_type);
```

## 3. 默认赛制种子数据

创建比赛时生成 10 个 `phases`：

| order | phase_key | name | type | side | seat | duration |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `aff_constructive_1` | 正方一辩立论 | `constructive` | `affirmative` | 1 | 180 |
| 2 | `neg_constructive_1` | 反方一辩立论 | `constructive` | `negative` | 1 | 180 |
| 3 | `aff_statement_2` | 正方二辩陈词 | `statement` | `affirmative` | 2 | 90 |
| 4 | `neg_statement_2` | 反方二辩陈词 | `statement` | `negative` | 2 | 90 |
| 5 | `aff_statement_3` | 正方三辩陈词 | `statement` | `affirmative` | 3 | 90 |
| 6 | `neg_statement_3` | 反方三辩陈词 | `statement` | `negative` | 3 | 90 |
| 7 | `free_debate` | 自由辩论 | `free_debate` | `neutral` | null | 480 |
| 8 | `neg_summary_4` | 反方四辩总结 | `summary` | `negative` | 4 | 180 |
| 9 | `aff_summary_4` | 正方四辩总结 | `summary` | `affirmative` | 4 | 180 |
| 10 | `commentary_vote` | 点评与评委合票 | `commentary` | `neutral` | null | 1020 |

自由辩论 `rules_json`：

```json
{
  "affirmative_total_seconds": 240,
  "negative_total_seconds": 240,
  "turn_limit_seconds": 15,
  "first_side": "affirmative",
  "turn_assignment": ["host_designate", "teammate_control"],
  "timeout_policy": "host_confirm",
  "exhausted_policy": "opponent_continuous_statement"
}
```

## 4. 事件溯源约定

- `events.seq` 是单场比赛内全局递增整数，由 Event Service 在同一事务内分配。
- 状态表保存当前快照，事件表保存事实历史。后端重启时优先读取状态表，必要时可用事件重放校验。
- 不删除 `events`、`speeches`、`transcript_segments`；作废发言通过 `speeches.valid = 0` 和 `speech_revisions` 记录。
- 回退环节时，把目标环节及之后已完成发言标记为无效，追加 `phase.rolled_back` 和对应审计日志。

## 5. 字段级说明

| 字段 | 说明 |
| --- | --- |
| `matches.config_json` | 全局配置：ASR/TTS、AI 时序、自动切换、主题视觉 |
| `speakers.config_json` | 单个辩手配置：Agent 超时、重试次数、TTS 发音人、控制台口令策略 |
| `phases.rules_json` | 环节规则扩展；自由辩论保存 `side_total_seconds`、`turn_seconds`、调度策略等 |
| `phases.speaker_selector` | 固定环节为 `fixed_seat`；自由辩论为 `free_debate`；点评合票为 `none` |
| `clocks.name` | 普通环节为 `main`；自由辩论为 `affirmative_total`、`negative_total`、`turn` |
| `speeches.turn_index` | 自由辩论轮次从 1 开始；普通环节为 0 |
| `transcript_segments.seq` | 可选绑定事件序号，用于前端排序和追溯 |
| `agent_tasks.task_type` | MVP 使用 `official` 和固定环节 `prefetch`；`speculative` 延后 |
| `vote_windows` | 控制学生投票窗口与评委/学生结果公布时序 |
