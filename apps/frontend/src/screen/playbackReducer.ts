/**
 * 大屏 TTS 播放的纯函数对账"大脑"。无 DOM / 无 React / 无网络 / 无 Date.now()。
 *
 * 设计：快照是唯一真相，实时事件只是"触发重算"。严格按分段序号顺序播放归档音频；
 * 当某个序号「有 url」或「在 skipped 列表」或「等待超时（看门狗）」时推进——因此永远
 * 不会因为缺一个分段而永久卡死。停止/截断与完成判定都在这里集中、确定地完成。
 *
 * 因为是纯函数，所有历史复发 bug（卡死、停不下来、不流式）都可在 Node 里用极小的字面量
 * 输入做毫秒级断言，无需浏览器/投影机/手工点击。
 */

export interface PlaybackChunk {
  sentenceIdx: number;
  audioUrl: string; // 空串表示尚未就绪
}

export interface PlaybackSpeech {
  speechId: string;
  speakerId: string;
  taskId: string;
  source: string; // agent_text or fixed fallback audio
  state: string; // 期望 "speaking"
  expectedSentences: number | null; // tts_expected_sentences；null = 仍在生成
  createdSentences: number; // tts_created_sentences；已排入 TTS 队列，可对缺口做超时裁决
  skippedSentences: number[]; // tts_skipped_sentences
  chunks: PlaybackChunk[]; // 来自 audio_assets[].chunks
  // 续播起点：页面中途刷新、首次绑定本段时从该序号继续（= 首个未播放/未跳过的句）。
  // 默认 0；只有"刷新时该段已播了一部分"才会 > 0，因此全新发言行为与以前完全一致。
  resumeIdx?: number;
}

export interface PlaybackPosition {
  speechId: string | null;
  taskId: string | null;
  nextIdx: number; // 下一个待解决的序号
  activeIdx: number | null; // 正在播放的序号（null = 空闲）
  activeStartedMs: number | null; // 开播时刻（active 卡死看门狗）
  waitingSinceMs: number | null; // nextIdx 开始等待的时刻（缺口看门狗）
  completeNotifiedKey: string | null; // 已上报完成的 `${speechId}:${taskId}`
  startNotifiedKey: string | null; // 已上报开播的 `${speechId}:${taskId}`
}

export type ActiveMediaState = "idle" | "playing" | "ended" | "errored" | "stalled";

export interface PlaybackInput {
  speech: PlaybackSpeech | null;
  position: PlaybackPosition;
  nowMs: number;
  audioEnabled: boolean;
  suppressed: boolean;
  activeMediaState: ActiveMediaState;
}

export interface PlaybackConfig {
  perSentenceWatchdogMs: number; // 缺口最长等待（已知 expected 时）
  activeStallWatchdogMs: number; // 单段最长"播放中"而不结束
}

export const DEFAULT_PLAYBACK_CONFIG: PlaybackConfig = {
  perSentenceWatchdogMs: 12000,
  activeStallWatchdogMs: 8000,
};

export type StopReason =
  | "no_speech"
  | "not_speaking"
  | "wrong_source"
  | "speech_changed"
  | "task_changed"
  | "suppressed"
  | "audio_disabled";

export type SkipReason = "in_skip_list" | "watchdog_timeout" | "media_error";

export type PlaybackDecision =
  | { kind: "IDLE"; position: PlaybackPosition }
  | { kind: "STOP"; position: PlaybackPosition; reason: StopReason }
  | { kind: "PLAY"; position: PlaybackPosition; sentenceIdx: number; audioUrl: string }
  | { kind: "SKIP"; position: PlaybackPosition; sentenceIdx: number; reason: SkipReason }
  | { kind: "WAIT"; position: PlaybackPosition; sentenceIdx: number }
  | { kind: "DONE"; position: PlaybackPosition; speechId: string; taskId: string }
  | { kind: "NOTIFY_START"; position: PlaybackPosition; speechId: string; taskId: string; sentenceIdx: number };

export function emptyPosition(): PlaybackPosition {
  return seedFor(null);
}

function seedFor(speech: PlaybackSpeech | null): PlaybackPosition {
  const resume = speech && typeof speech.resumeIdx === "number" && speech.resumeIdx > 0 ? speech.resumeIdx : 0;
  return {
    speechId: speech ? speech.speechId : null,
    taskId: speech ? speech.taskId : null,
    nextIdx: resume,
    activeIdx: null,
    activeStartedMs: null,
    waitingSinceMs: null,
    completeNotifiedKey: null,
    startNotifiedKey: null,
  };
}

export function reconcile(input: PlaybackInput, config: PlaybackConfig = DEFAULT_PLAYBACK_CONFIG): PlaybackDecision {
  const { speech, position, nowMs, audioEnabled, suppressed, activeMediaState } = input;

  // 1) STOP 守卫（最高优先级）。任何一条命中都清空播放、把 position 重置并重新绑定到当前
  //    speech（若有），这样下一拍即可立即开始新发言。不依赖会被合并丢弃的一次性事件。
  const stop = (reason: StopReason): PlaybackDecision => {
    if (
      (reason === "suppressed" || reason === "audio_disabled") &&
      speech &&
      position.speechId === speech.speechId &&
      position.taskId === speech.taskId
    ) {
      return {
        kind: "STOP",
        reason,
        position: {
          ...position,
          nextIdx: position.activeIdx ?? position.nextIdx,
          activeIdx: null,
          activeStartedMs: null,
          waitingSinceMs: null,
        },
      };
    }
    return { kind: "STOP", reason, position: seedFor(speech) };
  };
  if (!audioEnabled) return stop("audio_disabled");
  if (suppressed) return stop("suppressed");
  if (!speech) return stop("no_speech");
  if (!isPlayableSpeechSource(speech.source)) return stop("wrong_source");
  // 允许 "thinking" 与 "speaking" 都播放。关键：AI 发言在「大屏上报播放」之前一直是 "thinking"，
  // 正是首段播放（screen → playback-progress）把它翻成 "speaking"。若这里只认 "speaking" 就会死锁：
  // 大屏不播 → 状态不前进 → 永远不播（本次"点了也不发声"的真因）。只有 ended/paused 才截断音频。
  if (speech.state === "ended" || speech.state === "paused") return stop("not_speaking");
  if (position.speechId != null && position.speechId !== speech.speechId) return stop("speech_changed");
  if (position.taskId != null && position.taskId !== speech.taskId) return stop("task_changed");

  // 2) 绑定 speech（首次）。
  let pos: PlaybackPosition = position.speechId == null ? seedFor(speech) : position;

  // 3) 处理正在播放的分段。
  if (pos.activeIdx != null) {
    const stalledByTime = pos.activeStartedMs != null && nowMs - pos.activeStartedMs > config.activeStallWatchdogMs;
    if (activeMediaState === "ended") {
      // 正常结束：推进到下一段，继续往下解析（可能直接 PLAY 下一段）。
      pos = { ...pos, nextIdx: pos.activeIdx + 1, activeIdx: null, activeStartedMs: null, waitingSinceMs: null };
    } else if (activeMediaState === "errored" || stalledByTime) {
      // 播放失败 / 长时间没有播放进度 / onended 永不触发：强制跳过，保证队列前进。
      // 浏览器的 stalled/waiting 可能只是短暂缓冲，不能一收到就跳；只让时间看门狗裁决。
      const skippedIdx = pos.activeIdx;
      const next = { ...pos, nextIdx: skippedIdx + 1, activeIdx: null, activeStartedMs: null, waitingSinceMs: null };
      return {
        kind: "SKIP",
        position: next,
        sentenceIdx: skippedIdx,
        reason: activeMediaState === "errored" ? "media_error" : "watchdog_timeout",
      };
    } else {
      // 仍在合法播放。
      return { kind: "IDLE", position: pos };
    }
  }

  // 4) 解析 nextIdx。
  const expected = speech.expectedSentences;
  const idx = pos.nextIdx;

  // 4a) 完成判定（唯一出口，幂等）。
  if (expected != null && idx >= expected) {
    const key = `${speech.speechId}:${speech.taskId}`;
    if (pos.completeNotifiedKey === key) return { kind: "IDLE", position: pos };
    return { kind: "DONE", position: { ...pos, completeNotifiedKey: key }, speechId: speech.speechId, taskId: speech.taskId };
  }

  // 4b) 在 skip 列表里 → 立即跳过（无需等任何事件）。
  if (speech.skippedSentences.includes(idx)) {
    return { kind: "SKIP", position: { ...pos, nextIdx: idx + 1, waitingSinceMs: null }, sentenceIdx: idx, reason: "in_skip_list" };
  }

  // 4c) 有可播 url → 先一次性上报开播，再播放。
  const chunk = speech.chunks.find((c) => c.sentenceIdx === idx);
  if (chunk && chunk.audioUrl) {
    const startKey = `${speech.speechId}:${speech.taskId}`;
    if (pos.startNotifiedKey !== startKey) {
      return {
        kind: "NOTIFY_START",
        position: { ...pos, startNotifiedKey: startKey },
        speechId: speech.speechId,
        taskId: speech.taskId,
        sentenceIdx: idx,
      };
    }
    return {
      kind: "PLAY",
      position: { ...pos, activeIdx: idx, activeStartedMs: nowMs, waitingSinceMs: null },
      sentenceIdx: idx,
      audioUrl: chunk.audioUrl,
    };
  }

  // 4d) 尚未就绪。expected 未知时，idx >= created 表示这一段尚未被后端创建，必须等待；
  // idx < created 表示该分段已排入 TTS 队列，若长期没有 ready/skipped 就可以自愈跳过。
  if (expected == null && idx >= Math.max(0, speech.createdSentences)) {
    return { kind: "WAIT", position: pos.waitingSinceMs == null ? { ...pos, waitingSinceMs: nowMs } : pos, sentenceIdx: idx };
  }
  // expected 已知：idx < expected 却既无音频也未跳过 —— 等到看门狗超时后跳过（自愈）。
  if (pos.waitingSinceMs == null) {
    return { kind: "WAIT", position: { ...pos, waitingSinceMs: nowMs }, sentenceIdx: idx };
  }
  if (nowMs - pos.waitingSinceMs > config.perSentenceWatchdogMs) {
    return { kind: "SKIP", position: { ...pos, nextIdx: idx + 1, waitingSinceMs: null }, sentenceIdx: idx, reason: "watchdog_timeout" };
  }
  return { kind: "WAIT", position: pos, sentenceIdx: idx };
}

function isPlayableSpeechSource(source: string): boolean {
  return source === "agent_text" || source === "fallback_history";
}
