import { useCallback, useEffect, useRef, type MutableRefObject } from "react";
import { post } from "../api/client";
import type { MatchSnapshot, RealtimeMessage } from "../types/contracts";
import {
  emptyPosition,
  reconcile,
  type ActiveMediaState,
  type PlaybackPosition,
  type PlaybackSpeech,
} from "./playbackReducer";

/**
 * 大屏 TTS 播放的薄胶水：把快照/事件/1 秒看门狗三个触发器汇到一次纯函数对账（reducer），
 * 再把决策落到真实音频上。播放"逻辑"全在 reducer；这里只剩"起播/停/上报"等副作用。
 *
 * 关键点：
 *  - 快照是唯一真相；live MSE 流式已废弃（忽略 tts.sentence_stream_started）。
 *  - **单个可复用的 <audio> 元素**：每段直接 `src=url; play()`。绝不为多段并行创建多个
 *    <audio> 各自 preload —— 那会在投影机浏览器上同时打开多个取流，叠加生成期间的快照刷新
 *    撑爆「同一主机并发连接上限（HTTP/1.1 约 6）」，导致中间段音频取不到而 stall/error 连环
 *    跳过、最后"直接跳到发言结束"（线上实测：只有首段/末段取到流，中间段从未发起请求）。
 *    单元素任一时刻只占 1 个音频连接，最稳。
 *  - onended/onerror 只是优化；即便都不触发，1 秒看门狗也会按"真实播放进度"强制推进，绝不卡死。
 *  - skip/chunk 都走事件快路即时并入，缺口不必等快照刷新。
 *  - 「停止播放」靠 suppress；发言结束/换人/下一阶段靠快照对账截断。
 */
const SILENCE_URL = "/assets/silence-24k-1s.mp3";
const PLAYBACK_PROGRESS_EPSILON = 0.05;
const PLAYBACK_HEARTBEAT_MS = 5000;
const SCREEN_TTS_VOLUME = 0.86;
export const SCREEN_TTS_PLAYBACK_RATE = 1.0;

export function usePlayback(
  matchId: string,
  snapshot: MatchSnapshot | null,
  lastEvent: RealtimeMessage | null,
  audioEnabled: boolean,
  setAudioEnabled: (value: boolean) => void,
  onPlaybackBlocked?: () => void
): { unlock: () => void } {
  const positionRef = useRef<PlaybackPosition>(emptyPosition());
  const mediaRef = useRef<ActiveMediaState>("idle");
  const retryRef = useRef<Map<string, number>>(new Map());
  const suppressRef = useRef<Set<string>>(new Set());
  const startReportedRef = useRef<string>(""); // `${speechId}:${taskId}` 已上报"开始播放"
  // 单个可复用 <audio> 元素 + 当前正在播放段的标识（供一次性绑定的处理器读取）。
  const activeElRef = useRef<HTMLAudioElement | null>(null);
  const activeSegmentRef = useRef<string>("");
  const urlRef = useRef<string>("");
  const currentPlayRef = useRef<{ speechId: string; taskId: string; speakerId: string; idx: number } | null>(null);
  // 预加载下一句（双缓冲，仅预热缓存，绝不播放）：当前句播放时把下一句 MP3 预取进浏览器缓存，
  // onended 切到下一句时 el.src 命中缓存→秒开，消除「单 <audio> 换 src 才取流+解码」的句间停顿。
  const preloaderRef = useRef<HTMLAudioElement | null>(null);
  const preloadedUrlRef = useRef<string>("");
  const playbackProgressRef = useRef<{ segment: string; currentTime: number; atMs: number }>({
    segment: "",
    currentTime: 0,
    atMs: 0,
  });
  const playbackHeartbeatRef = useRef<{ segment: string; atMs: number }>({ segment: "", atMs: 0 });
  // 事件快路：tts.sentence_ready 里直接带 audio_url / skipped，立刻并入 reducer 视图，省去快照 GET 往返。
  const eventChunksRef = useRef<{ key: string; map: Map<number, string> }>({ key: "", map: new Map() });
  const eventSkipsRef = useRef<{ key: string; set: Set<number> }>({ key: "", set: new Set() });
  const audioEnabledRef = useRef(audioEnabled);
  const onPlaybackBlockedRef = useRef(onPlaybackBlocked);
  const snapshotRef = useRef<MatchSnapshot | null>(snapshot);
  const runnerRef = useRef<(now: number) => void>(() => {});

  audioEnabledRef.current = audioEnabled;
  onPlaybackBlockedRef.current = onPlaybackBlocked;
  snapshotRef.current = snapshot;

  const markPlaybackBlocked = useCallback(() => {
    positionRef.current = {
      ...positionRef.current,
      activeIdx: null,
      activeStartedMs: null,
      waitingSinceMs: null,
    };
    mediaRef.current = "idle";
    currentPlayRef.current = null;
    urlRef.current = "";
    clearActiveAudio(activeElRef, activeSegmentRef, playbackProgressRef);
    onPlaybackBlockedRef.current?.();
  }, []);

  // 单元素的一次性事件处理器：用 currentPlayRef/activeSegmentRef 读取"当前段"，不靠闭包捕获。
  function attachHandlers(el: HTMLAudioElement): void {
    el.onplaying = () => {
      if (activeElRef.current !== el) return;
      const cur = currentPlayRef.current;
      if (!cur) return;
      mediaRef.current = "playing";
      const startKey = `${cur.speechId}:${cur.taskId}`;
      if (startReportedRef.current !== startKey) {
        startReportedRef.current = startKey;
        void postWithRetry(`/api/matches/${matchId}/speeches/${cur.speechId}/tts/playback-started`, {
          task_id: cur.taskId,
          speaker_id: cur.speakerId,
          reason: "screen_audio_play_started",
        });
      }
      void postWithRetry(`/api/matches/${matchId}/speeches/${cur.speechId}/tts/playback-progress`, {
        task_id: cur.taskId,
        sentence_idx: cur.idx,
        speaker_id: cur.speakerId,
        status: "playing",
      });
    };
    el.onended = () => {
      if (activeElRef.current !== el) return;
      const cur = currentPlayRef.current;
      mediaRef.current = "ended";
      if (cur) {
        void postWithRetry(`/api/matches/${matchId}/speeches/${cur.speechId}/tts/playback-progress`, {
          task_id: cur.taskId,
          sentence_idx: cur.idx,
          speaker_id: cur.speakerId,
          status: "played",
        });
      }
      runnerRef.current(nowEpoch());
    };
    el.onwaiting = () => {
      if (activeElRef.current !== el) return;
      mediaRef.current = "stalled";
      runnerRef.current(nowEpoch());
    };
    el.onstalled = () => {
      if (activeElRef.current !== el) return;
      mediaRef.current = "stalled";
      runnerRef.current(nowEpoch());
    };
    el.oncanplay = () => {
      if (activeElRef.current !== el) return;
      if (!el.paused && !el.ended) mediaRef.current = "playing";
    };
    el.onerror = () => {
      if (activeElRef.current !== el) return;
      const cur = currentPlayRef.current;
      const rk = cur ? `${cur.taskId}:${cur.idx}` : "";
      const tries = (rk && retryRef.current.get(rk)) || 0;
      if (rk && tries < 1 && urlRef.current) {
        retryRef.current.set(rk, tries + 1);
        try {
          el.src = urlRef.current;
          el.load();
          void el.play().catch(() => {
            mediaRef.current = "errored";
            runnerRef.current(nowEpoch());
          });
          return;
        } catch {
          /* fall through */
        }
      }
      mediaRef.current = "errored";
      runnerRef.current(nowEpoch());
    };
  }

  function ensureEl(): HTMLAudioElement {
    let el = activeElRef.current;
    if (!el) {
      el = new Audio();
      el.preload = "auto";
      el.volume = SCREEN_TTS_VOLUME;
      applyScreenTtsPlaybackRate(el);
      activeElRef.current = el;
      attachHandlers(el);
    }
    return el;
  }

  // 预热下一句的音频缓存（不播放）。配合后端可缓存的归档音频，主元素切到该 url 时直接命中缓存。
  function preloadNext(url: string): void {
    if (!url || preloadedUrlRef.current === url) return;
    try {
      let p = preloaderRef.current;
      if (!p) {
        p = new Audio();
        p.preload = "auto";
        p.muted = true;
        preloaderRef.current = p;
      }
      preloadedUrlRef.current = url;
      p.src = url;
      p.load();
    } catch {
      /* 预加载失败无所谓：主播放仍会自行取流，只是少了这点提速 */
    }
  }

  const runReconcile = useCallback(
    (now: number) => {
      const progressed = observeActiveAudioProgress(now, positionRef, activeElRef, activeSegmentRef, playbackProgressRef, mediaRef);
      const speech = projectSpeech(snapshotRef.current);
      if (speech) {
        if (progressed) {
          maybeReportPlaybackHeartbeat(now, speech, positionRef, activeSegmentRef, playbackHeartbeatRef, mediaRef, matchId);
        }
        const key = `${speech.speechId}:${speech.taskId}`;
        const ev = eventChunksRef.current;
        if (ev.key === key && ev.map.size) {
          const have = new Set(speech.chunks.map((c) => c.sentenceIdx));
          ev.map.forEach((url, idx) => {
            if (!have.has(idx)) speech.chunks.push({ sentenceIdx: idx, audioUrl: url });
          });
        }
        const sk = eventSkipsRef.current;
        if (sk.key === key && sk.set.size) {
          const skipped = new Set(speech.skippedSentences);
          sk.set.forEach((idx) => skipped.add(idx));
          speech.skippedSentences = [...skipped];
        }
      }
      const suppressed = speech
        ? suppressRef.current.has(`speech:${speech.speechId}`) || suppressRef.current.has(`task:${speech.taskId}`)
        : false;

      for (let guard = 0; guard < 256; guard += 1) {
        const decision = reconcile({
          speech,
          position: positionRef.current,
          nowMs: now,
          audioEnabled: audioEnabledRef.current,
          suppressed,
          activeMediaState: mediaRef.current,
        });
        positionRef.current = decision.position;

        if (decision.kind === "PLAY") {
          if (speech) playSegment(speech, decision.sentenceIdx, decision.audioUrl);
          return;
        }

        if (decision.kind === "STOP") {
          if (decision.reason === "suppressed" || decision.reason === "audio_disabled") {
            const el = activeElRef.current;
            if (el) {
              try {
                el.pause();
              } catch {
                /* ignore */
              }
            }
          } else {
            clearActiveAudio(activeElRef, activeSegmentRef, playbackProgressRef);
          }
          mediaRef.current = "idle";
          return;
        }

        if (decision.kind === "DONE") {
          clearActiveAudio(activeElRef, activeSegmentRef, playbackProgressRef);
          void postWithRetry(`/api/matches/${matchId}/speeches/${decision.speechId}/tts/playback-complete`, {
            task_id: decision.taskId,
            speaker_id: speech?.speakerId,
            reason: "screen_playback_complete",
          });
          return;
        }

        if (decision.kind === "NOTIFY_START") {
          continue; // 上报留给真实 onplaying；这里直接进入下一拍 → PLAY
        }

        if (decision.kind === "SKIP") {
          if (speech) {
            const segment = `${speech.speechId}:${speech.taskId}:${decision.sentenceIdx}`;
            if (activeSegmentRef.current === segment) {
              clearActiveAudio(activeElRef, activeSegmentRef, playbackProgressRef);
            }
          }
          if (speech && (decision.reason === "watchdog_timeout" || decision.reason === "media_error")) {
            void postWithRetry(`/api/matches/${matchId}/speeches/${speech.speechId}/tts/playback-progress`, {
              task_id: speech.taskId,
              sentence_idx: decision.sentenceIdx,
              speaker_id: speech.speakerId,
              status: decision.reason === "media_error" ? "error" : "stalled",
            });
          }
          mediaRef.current = "idle";
          continue; // 立即解析下一段
        }

        return; // WAIT / IDLE
      }

      // 播放当前段：复用唯一 <audio>，直接 src=url; play()。任一时刻只占 1 个音频连接。
      function playSegment(sp: PlaybackSpeech, idx: number, url: string) {
        const segment = `${sp.speechId}:${sp.taskId}:${idx}`;
        const el = ensureEl();
        currentPlayRef.current = { speechId: sp.speechId, taskId: sp.taskId, speakerId: sp.speakerId, idx };
        el.volume = SCREEN_TTS_VOLUME;
        applyScreenTtsPlaybackRate(el);
        // 预热下一句，缩小句间空隙（命中后端可缓存的归档音频）。
        const nextUrl = sp.chunks.find((c) => c.sentenceIdx === idx + 1)?.audioUrl;
        if (nextUrl) preloadNext(nextUrl);
        if (activeSegmentRef.current === segment && urlRef.current === url && !el.ended) {
          mediaRef.current = el.paused ? "idle" : "playing";
          if (el.paused) {
            void el.play().catch((err: unknown) => {
              if (err && (err as { name?: string }).name === "NotAllowedError") {
                markPlaybackBlocked();
                return;
              }
              mediaRef.current = "errored";
              runnerRef.current(nowEpoch());
            });
          }
          return;
        }
        activeSegmentRef.current = segment;
        urlRef.current = url;
        playbackProgressRef.current = { segment, currentTime: 0, atMs: now };
        mediaRef.current = "playing";
        retryRef.current.delete(`${sp.taskId}:${idx}`);
        try {
          if (el.getAttribute("src") !== url) el.src = url;
        } catch {
          /* ignore */
        }
        try {
          el.currentTime = 0;
        } catch {
          /* ignore */
        }
        void el.play().catch((err: unknown) => {
          if (err && (err as { name?: string }).name === "NotAllowedError") {
            markPlaybackBlocked();
            return;
          }
          mediaRef.current = "errored";
          runnerRef.current(nowEpoch());
        });
      }
    },
    [markPlaybackBlocked, matchId]
  );

  runnerRef.current = runReconcile;

  // 用户手势（点扬声器）内同步解锁音频：跑一拍对账，首段若已就绪 play() 就发生在手势里；
  // 若暂无可播段，用独立静音元素在手势里播一下激活媒体权限。
  const unlock = useCallback(() => {
    audioEnabledRef.current = true;
    runnerRef.current(nowEpoch());
    if (positionRef.current.activeIdx == null) {
      try {
        const primer = new Audio(SILENCE_URL);
        void primer
          .play()
          .then(() => {
            try {
              primer.pause();
            } catch {
              /* ignore */
            }
          })
          .catch(() => onPlaybackBlockedRef.current?.());
      } catch {
        onPlaybackBlockedRef.current?.();
      }
    }
  }, []);

  // 音频开关变化时立即对账（开→尽快开播/解锁，关→立即截断）。
  useEffect(() => {
    runnerRef.current(nowEpoch());
  }, [audioEnabled]);

  // 卸载时释放音频元素。
  useEffect(() => {
    return () => {
      clearActiveAudio(activeElRef, activeSegmentRef, playbackProgressRef);
      const p = preloaderRef.current;
      if (p) {
        try {
          p.removeAttribute("src");
          p.load();
        } catch {
          /* ignore */
        }
        preloaderRef.current = null;
        preloadedUrlRef.current = "";
      }
    };
  }, []);

  // 触发器 1：快照变化（唯一真相）。
  useEffect(() => {
    runnerRef.current(nowEpoch());
  }, [snapshot]);

  // 触发器 2：实时事件——更新 suppress、并把 chunk/skip 即时并入快路；其余靠下一帧快照对账。
  useEffect(() => {
    if (!lastEvent) {
      return;
    }
    const p = (lastEvent.payload ?? {}) as Record<string, unknown>;
    const speechId = String(p.speech_id ?? "");
    const taskId = String(p.task_id ?? "");
    if (lastEvent.type === "tts.playback_stop_requested") {
      if (speechId) suppressRef.current.add(`speech:${speechId}`);
      if (taskId) suppressRef.current.add(`task:${taskId}`);
    } else if (lastEvent.type === "tts.playback_resume_requested") {
      if (speechId) suppressRef.current.delete(`speech:${speechId}`);
      if (taskId) suppressRef.current.delete(`task:${taskId}`);
      // 「继续播放」也重新打开音频开关：若此前因自动播放被拦而被置 false，这里恢复。
      audioEnabledRef.current = true;
      setAudioEnabled(true);
    } else if (lastEvent.type === "tts.sentence_ready") {
      const url = String(p.audio_url ?? "");
      const idx = Number(p.sentence_idx ?? NaN);
      if (Number.isFinite(idx) && speechId && taskId) {
        const key = `${speechId}:${taskId}`;
        if (url) {
          if (eventChunksRef.current.key !== key) eventChunksRef.current = { key, map: new Map() };
          eventChunksRef.current.map.set(idx, url);
        } else if (p.skipped) {
          if (eventSkipsRef.current.key !== key) eventSkipsRef.current = { key, set: new Set() };
          eventSkipsRef.current.set.add(idx);
        }
      }
    }
    runnerRef.current(nowEpoch());
  }, [lastEvent, setAudioEnabled]);

  // 触发器 3：1 秒看门狗——即便所有媒体事件都不触发，也能按时间强制推进，永不永久卡死。
  useEffect(() => {
    const id = window.setInterval(() => runnerRef.current(nowEpoch()), 1000);
    return () => window.clearInterval(id);
  }, []);

  return { unlock };
}

function detachHandlers(el: HTMLAudioElement): void {
  el.onended = null;
  el.onerror = null;
  el.onplaying = null;
  el.onwaiting = null;
  el.onstalled = null;
  el.oncanplay = null;
}

function detachAndStop(el: HTMLAudioElement): void {
  detachHandlers(el);
  try {
    el.pause();
  } catch {
    /* ignore */
  }
  try {
    el.removeAttribute("src");
    el.load();
  } catch {
    /* ignore */
  }
}

export function clearActiveAudio(
  activeElRef: MutableRefObject<HTMLAudioElement | null>,
  activeSegmentRef: MutableRefObject<string>,
  playbackProgressRef: MutableRefObject<{ segment: string; currentTime: number; atMs: number }>
): void {
  const el = activeElRef.current;
  if (el) detachAndStop(el);
  activeElRef.current = null;
  activeSegmentRef.current = "";
  playbackProgressRef.current = { segment: "", currentTime: 0, atMs: 0 };
}

export function observeActiveAudioProgress(
  now: number,
  positionRef: MutableRefObject<PlaybackPosition>,
  activeElRef: MutableRefObject<HTMLAudioElement | null>,
  activeSegmentRef: MutableRefObject<string>,
  playbackProgressRef: MutableRefObject<{ segment: string; currentTime: number; atMs: number }>,
  mediaRef: MutableRefObject<ActiveMediaState>
): boolean {
  const pos = positionRef.current;
  const el = activeElRef.current;
  if (!el || pos.activeIdx == null || !pos.speechId || !pos.taskId) return false;
  const segment = `${pos.speechId}:${pos.taskId}:${pos.activeIdx}`;
  if (activeSegmentRef.current !== segment) return false;

  if (el.ended) {
    mediaRef.current = "ended";
    return false;
  }

  const currentTime = Number.isFinite(el.currentTime) ? el.currentTime : 0;
  const previous = playbackProgressRef.current;
  const advanced =
    previous.segment === segment && currentTime > previous.currentTime + PLAYBACK_PROGRESS_EPSILON;
  if (advanced) {
    playbackProgressRef.current = { segment, currentTime, atMs: now };
    if (!el.paused) mediaRef.current = "playing";
    // 看门狗的基准是"最近一次真实播放进度"，不是分段开始时间。长句正常播放时不会被误跳。
    positionRef.current = { ...positionRef.current, activeStartedMs: now };
    return true;
  }

  if (previous.segment !== segment) {
    playbackProgressRef.current = { segment, currentTime, atMs: now };
  }

  if (mediaRef.current === "playing" && el.paused) {
    mediaRef.current = "stalled";
  }
  return false;
}

function maybeReportPlaybackHeartbeat(
  now: number,
  speech: PlaybackSpeech,
  positionRef: MutableRefObject<PlaybackPosition>,
  activeSegmentRef: MutableRefObject<string>,
  playbackHeartbeatRef: MutableRefObject<{ segment: string; atMs: number }>,
  mediaRef: MutableRefObject<ActiveMediaState>,
  matchId: string
): void {
  const idx = positionRef.current.activeIdx;
  if (idx == null || mediaRef.current !== "playing") return;
  const segment = `${speech.speechId}:${speech.taskId}:${idx}`;
  if (activeSegmentRef.current !== segment) return;
  if (!shouldSendPlaybackHeartbeat(now, segment, playbackHeartbeatRef)) return;
  void postWithRetry(
    `/api/matches/${matchId}/speeches/${speech.speechId}/tts/playback-progress`,
    {
      task_id: speech.taskId,
      sentence_idx: idx,
      speaker_id: speech.speakerId,
      status: "playing",
    },
    2,
    250
  );
}

export function shouldSendPlaybackHeartbeat(
  now: number,
  segment: string,
  playbackHeartbeatRef: MutableRefObject<{ segment: string; atMs: number }>,
  intervalMs = PLAYBACK_HEARTBEAT_MS
): boolean {
  const previous = playbackHeartbeatRef.current;
  if (previous.segment === segment && now - previous.atMs < intervalMs) return false;
  playbackHeartbeatRef.current = { segment, atMs: now };
  return true;
}

type PitchPreservingAudioElement = HTMLAudioElement & {
  preservesPitch?: boolean;
  mozPreservesPitch?: boolean;
  webkitPreservesPitch?: boolean;
};

export function applyScreenTtsPlaybackRate(el: HTMLAudioElement, rate = SCREEN_TTS_PLAYBACK_RATE): void {
  const safeRate = Number.isFinite(rate) ? Math.min(1.6, Math.max(0.75, rate)) : 1;
  try {
    el.playbackRate = safeRate;
  } catch {
    /* ignore */
  }
  try {
    const pitchEl = el as PitchPreservingAudioElement;
    pitchEl.preservesPitch = true;
    pitchEl.mozPreservesPitch = true;
    pitchEl.webkitPreservesPitch = true;
  } catch {
    /* ignore */
  }
}

export async function postWithRetry(
  path: string,
  body: object,
  attempts = 4,
  delayMs = 500,
  sender: (path: string, body: object) => Promise<unknown> = post
): Promise<boolean> {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      await sender(path, body);
      return true;
    } catch {
      if (attempt >= attempts - 1) return false;
      await sleep(delayMs * Math.max(1, attempt + 1));
    }
  }
  return false;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => globalThis.setTimeout(resolve, ms));
}

function projectSpeech(snapshot: MatchSnapshot | null): PlaybackSpeech | null {
  const cs = snapshot?.current_speech;
  if (!cs) return null;
  const asset = snapshot!.audio_assets.find((item) => item.speech_id === cs.id);
  const chunks = (asset?.chunks ?? [])
    .map((chunk) => ({ sentenceIdx: Number(chunk.chunk_index), audioUrl: String(chunk.audio_url ?? "") }))
    .filter((chunk) => Number.isFinite(chunk.sentenceIdx));
  const skippedSentences = (cs.tts_skipped_sentences ?? []).map((value) => Number(value));
  return {
    speechId: cs.id,
    speakerId: cs.speaker_id,
    taskId: String(cs.tts_task_id ?? ""),
    source: cs.source,
    state: String(cs.state ?? ""),
    expectedSentences: cs.tts_expected_sentences ?? null,
    createdSentences: Number(cs.tts_created_sentences ?? 0),
    skippedSentences,
    chunks,
    resumeIdx: computeResumeIdx(cs, skippedSentences),
  };
}

/**
 * 续播起点：页面中途刷新时，从「首个既未播放、也未跳过的分段序号」继续，而不是从 0 重头开始。
 * 后端权威记录已播分段（tts_played_sentence_indices）与跳过分段；正在播放但尚未播完的那一句
 * 不在已播集合里，因此会从它的开头重新播放——即"接着讲"。全新发言时三者皆空，返回 0，行为不变。
 */
export function computeResumeIdx(cs: NonNullable<MatchSnapshot["current_speech"]>, skipped: number[]): number {
  const resolved = new Set<number>();
  (cs.tts_played_sentence_indices ?? []).forEach((v) => {
    const n = Number(v);
    if (Number.isFinite(n) && n >= 0) resolved.add(n);
  });
  skipped.forEach((n) => {
    if (Number.isFinite(n) && n >= 0) resolved.add(n);
  });
  // 旧版仅有计数（无明细列表）时，把前 N 段视为已播。
  if (!(cs.tts_played_sentence_indices ?? []).length) {
    const count = Number(cs.tts_played_sentences ?? 0);
    for (let i = 0; i < count; i += 1) resolved.add(i);
  }
  let idx = 0;
  while (resolved.has(idx)) idx += 1;
  return idx;
}

function nowEpoch(): number {
  return new Date().getTime();
}
