import { useCallback, useEffect, useRef } from "react";
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
 * 大屏 TTS 播放的薄胶水：持有单个可复用的 <audio>，把快照/事件/1秒看门狗这三个触发器
 * 都汇到一次 reconcile，再把纯函数返回的决策落到真实音频上。所有播放"逻辑"在 reducer 里，
 * 这里只剩"开播放/停/上报"这类不可避免的副作用。
 *
 * 关键可靠性点：
 *  - 快照是唯一真相；live MSE 流式已废弃（忽略 tts.sentence_stream_started）。
 *  - onended/onerror 只是优化；即便它们都不触发，1 秒看门狗也会按时间强制推进，绝不永久卡死。
 *  - 「停止播放」靠 suppress（音频纯控制，不改发言状态）；发言结束/换人/下一阶段靠快照对账截断。
 */
const SILENCE_URL = "/assets/silence-24k-1s.mp3";

export function usePlayback(
  matchId: string,
  snapshot: MatchSnapshot | null,
  lastEvent: RealtimeMessage | null,
  audioEnabled: boolean,
  setAudioEnabled: (value: boolean) => void
): { unlock: () => void } {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const positionRef = useRef<PlaybackPosition>(emptyPosition());
  const mediaRef = useRef<ActiveMediaState>("idle");
  const urlRef = useRef<string>("");
  const retryRef = useRef<Map<string, number>>(new Map());
  const suppressRef = useRef<Set<string>>(new Set());
  const startReportedRef = useRef<string>(""); // `${speechId}:${taskId}` 已上报"开始播放"
  // 事件快路：tts.sentence_ready 里直接带了 audio_url，先用它立刻起播，不必等 157KB 快照 GET
  // 往返（首句"很晚"的主因之一）。快照仍是权威：skipped/expected/状态/截断都看快照，这里只补"已就绪的 url"。
  const eventChunksRef = useRef<{ key: string; map: Map<number, string> }>({ key: "", map: new Map() });
  const audioEnabledRef = useRef(audioEnabled);
  const snapshotRef = useRef<MatchSnapshot | null>(snapshot);
  const runnerRef = useRef<(now: number) => void>(() => {});

  audioEnabledRef.current = audioEnabled;
  snapshotRef.current = snapshot;

  const runReconcile = useCallback(
    (now: number) => {
      const audio = audioRef.current;
      const speech = projectSpeech(snapshotRef.current);
      // 合并事件快路里已知的分段 url（仅当前 speech:task），让首/各句无需等快照 GET 即可起播。
      if (speech) {
        const ev = eventChunksRef.current;
        if (ev.key === `${speech.speechId}:${speech.taskId}` && ev.map.size) {
          const have = new Set(speech.chunks.map((c) => c.sentenceIdx));
          ev.map.forEach((url, idx) => {
            if (!have.has(idx)) speech.chunks.push({ sentenceIdx: idx, audioUrl: url });
          });
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
          if (audio) {
            retryRef.current.delete(`${decision.position.taskId ?? ""}:${decision.sentenceIdx}`);
            urlRef.current = decision.audioUrl;
            mediaRef.current = "playing";
            try {
              audio.src = decision.audioUrl;
              void audio.play().catch((err: unknown) => {
                if (err && (err as { name?: string }).name === "NotAllowedError") {
                  // 浏览器自动播放策略：需要用户手势授权，关掉音频开关提示用户点一下扬声器。
                  mediaRef.current = "idle";
                  setAudioEnabled(false);
                  return;
                }
                mediaRef.current = "errored";
                runnerRef.current(nowEpoch());
              });
            } catch {
              mediaRef.current = "errored";
              runnerRef.current(nowEpoch());
            }
          }
          // 不在此处上报进度：上报必须等真实 onplaying（声音真的出来了）才发，否则会出现
          // "显示已开始播放、计时已走，但其实没声音"。见下方 audio.onplaying。
          return;
        }

        if (decision.kind === "STOP") {
          // 只 pause()，不 removeAttribute("src")+load()：后者在空 src 上会触发一个 error 事件，
          // 重新进入状态机（可能表现为"卡顿/重新发音/异常推进"）。pause() 足以静音；下一段播放会
          // 直接设置新的 src 覆盖，不会意外恢复旧音频。
          if (audio) {
            try {
              audio.pause();
            } catch {
              /* ignore */
            }
          }
          mediaRef.current = "idle";
          urlRef.current = "";
          return;
        }

        if (decision.kind === "DONE") {
          void post(`/api/matches/${matchId}/speeches/${decision.speechId}/tts/playback-complete`, {
            task_id: decision.taskId,
            speaker_id: speech?.speakerId,
            reason: "screen_playback_complete",
          }).catch(() => undefined);
          return;
        }

        if (decision.kind === "NOTIFY_START") {
          // 不在此处上报开始：同样等真实 onplaying 再发，确保"开始播放"与真实出声一致。
          continue; // 立即再跑一拍 → PLAY，不增加首句延迟
        }

        if (decision.kind === "SKIP") {
          mediaRef.current = "idle";
          continue; // 立即解析下一段
        }

        return; // WAIT / IDLE
      }
    },
    [matchId, setAudioEnabled]
  );

  runnerRef.current = runReconcile;

  // 在「用户手势」（点扬声器按钮）调用栈内同步解锁音频：
  //  1) 立刻把 audioEnabled 视为真并跑一次对账——若首段已就绪，audio.play() 就发生在手势里，
  //     满足浏览器自动播放策略（这是大屏"点了开关却不出声"的根因修复）。
  //  2) 若此刻还没有可播分段，则用一个独立的静音元素在手势里 play 一下，激活本页媒体权限，
  //     待分段到达后程序化 play() 即被允许（用独立元素，避免它的 ended 事件污染对账）。
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
          .catch(() => undefined);
      } catch {
        /* ignore */
      }
    }
  }, []);

  // 音频开关变化时立即对账（开→尽快开播/解锁，关→立即截断），不必等 1 秒看门狗。
  useEffect(() => {
    runnerRef.current(nowEpoch());
  }, [audioEnabled]);

  // 单个可复用的 audio 元素 + 一次性事件监听（处理器只用 ref，不怕闭包过期）。
  useEffect(() => {
    const audio = new Audio();
    audio.preload = "auto";
    audioRef.current = audio;

    audio.onended = () => {
      mediaRef.current = "ended";
      runnerRef.current(nowEpoch());
    };
    audio.onplaying = () => {
      // 真实出声的瞬间才上报：把"开始播放/进度"与真实声音对齐——后端正是据此把发言翻成
      // speaking、起计时、并让大屏进入发言动画页。绝不在还没出声时就上报。
      mediaRef.current = "playing";
      const pos = positionRef.current;
      const speech = projectSpeech(snapshotRef.current);
      if (!speech || pos.activeIdx == null) return;
      const startKey = `${speech.speechId}:${speech.taskId}`;
      if (startReportedRef.current !== startKey) {
        startReportedRef.current = startKey;
        void post(`/api/matches/${matchId}/speeches/${speech.speechId}/tts/playback-started`, {
          task_id: speech.taskId,
          speaker_id: speech.speakerId,
          reason: "screen_audio_play_started",
        }).catch(() => undefined);
      }
      void post(`/api/matches/${matchId}/speeches/${speech.speechId}/tts/playback-progress`, {
        task_id: speech.taskId,
        sentence_idx: pos.activeIdx,
        speaker_id: speech.speakerId,
        status: "playing",
      }).catch(() => undefined);
    };
    audio.onerror = () => {
      const pos = positionRef.current;
      const idx = pos.activeIdx;
      const key = `${pos.taskId ?? ""}:${idx}`;
      const tries = retryRef.current.get(key) ?? 0;
      if (idx != null && tries < 1 && urlRef.current) {
        // 一次轻量重试（重设 src 再播）；再失败就交给 reducer 跳过。
        retryRef.current.set(key, tries + 1);
        try {
          audio.src = urlRef.current;
          void audio.play().catch(() => {
            mediaRef.current = "errored";
            runnerRef.current(nowEpoch());
          });
          return;
        } catch {
          /* fall through to errored */
        }
      }
      mediaRef.current = "errored";
      runnerRef.current(nowEpoch());
    };

    return () => {
      try {
        audio.pause();
      } catch {
        /* ignore */
      }
      audio.onended = null;
      audio.onerror = null;
      audio.onplaying = null;
      audioRef.current = null;
    };
  }, []);

  // 触发器 1：快照变化（唯一真相）。
  useEffect(() => {
    runnerRef.current(nowEpoch());
  }, [snapshot]);

  // 触发器 2：实时事件——只更新 suppress（音频纯控制），其余靠下一帧快照对账。
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
    } else if (lastEvent.type === "tts.sentence_ready") {
      // 快路：事件里带的 audio_url 立刻记下，下面同一拍 reconcile 就能起播，省去快照 GET 往返。
      const url = String(p.audio_url ?? "");
      const idx = Number(p.sentence_idx ?? NaN);
      if (url && Number.isFinite(idx) && speechId && taskId) {
        const key = `${speechId}:${taskId}`;
        if (eventChunksRef.current.key !== key) eventChunksRef.current = { key, map: new Map() };
        eventChunksRef.current.map.set(idx, url);
      }
    }
    // tts.finished / speech.ended / speech.timeout 等无需特殊处理：useMatch 已在每个事件后重拉快照。
    // tts.sentence_stream_started 完全忽略（live 流式已废弃）。
    runnerRef.current(nowEpoch());
  }, [lastEvent]);

  // 触发器 3：1 秒看门狗——即便所有媒体事件都不触发，也能按时间强制推进，永不永久卡死。
  useEffect(() => {
    const id = window.setInterval(() => runnerRef.current(nowEpoch()), 1000);
    return () => window.clearInterval(id);
  }, []);

  return { unlock };
}

function projectSpeech(snapshot: MatchSnapshot | null): PlaybackSpeech | null {
  const cs = snapshot?.current_speech;
  if (!cs) return null;
  const asset = snapshot!.audio_assets.find((item) => item.speech_id === cs.id);
  const chunks = (asset?.chunks ?? [])
    .map((chunk) => ({ sentenceIdx: Number(chunk.chunk_index), audioUrl: String(chunk.audio_url ?? "") }))
    .filter((chunk) => Number.isFinite(chunk.sentenceIdx));
  return {
    speechId: cs.id,
    speakerId: cs.speaker_id,
    taskId: String(cs.tts_task_id ?? ""),
    source: cs.source,
    state: String(cs.state ?? ""),
    expectedSentences: cs.tts_expected_sentences ?? null,
    skippedSentences: (cs.tts_skipped_sentences ?? []).map((value) => Number(value)),
    chunks,
  };
}

function nowEpoch(): number {
  return new Date().getTime();
}
