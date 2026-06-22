import { useCallback, useEffect, useRef, useState } from "react";
import { getMatch, websocketUrl } from "../api/client";
import type { MatchSnapshot, RealtimeMessage } from "../types/contracts";

export function useMatch(matchId: string, channel: string, speakerId?: string) {
  const [snapshot, setSnapshot] = useState<MatchSnapshot | null>(null);
  const [socketStatus, setSocketStatus] = useState<"connecting" | "open" | "closed" | "reconnecting">("connecting");
  const [lastEvent, setLastEvent] = useState<RealtimeMessage | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const lastSeqRef = useRef(0);
  const snapshotRef = useRef<MatchSnapshot | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  // 刷新合并：agent.speech.delta 是逐 token 的高频事件，一段 AI 发言会发几十上百个。
  // 若每个 delta 都重拉整张 ~157KB 快照（且要抢后端锁），会拖慢真正驱动出声的
  // tts.sentence_ready 广播——这正是"首句很慢"的残留根因之一。这里把 delta 触发的刷新
  // 节流到每 DELTA_REFRESH_MIN_INTERVAL_MS 一次（字幕仍 ~3 次/秒，肉眼连贯）；其余事件
  // （sentence_ready / speech.* / 阶段切换等）仍即时刷新，绝不增加音频/控制时延。
  const deltaRefreshTimerRef = useRef<number | undefined>(undefined);
  const lastRefreshAtRef = useRef(0);

  const applySnapshot = useCallback((next: MatchSnapshot) => {
    setLoadError(null);
    // 防快照回退：事件触发的 getMatch、WebSocket snapshot、重连初始快照都可能乱序抵达。
    // 较旧快照若覆盖较新快照，会让 current_speech 短暂消失或 TTS 进度倒退，播放端就可能从头播。
    const prev = snapshotRef.current;
    if (shouldIgnoreSnapshot(prev, next)) {
      return;
    }
    snapshotRef.current = next;
    lastSeqRef.current = next.last_seq;
    setSnapshot(next);
  }, []);

  const refresh = useCallback(async () => {
    const next = await getMatch(matchId);
    applySnapshot(next);
  }, [applySnapshot, matchId]);

  useEffect(() => {
    let cancelled = false;
    let socket: WebSocket | null = null;
    let retry: number | undefined;
    let reconnectAttempt = 0;

    // 指数退避重连：固定 1.2s 死循环重连在弱网/被服务端因「慢客户端队列满」主动断开时会形成
    // 重连风暴（每次重连还要拉一次全量快照），把后端越拖越慢。退避到最长 ~15s + 抖动，连上后清零。
    function scheduleReconnect() {
      if (cancelled) return;
      setSocketStatus("reconnecting");
      const base = Math.min(15000, 1200 * 2 ** Math.min(reconnectAttempt, 4));
      const delay = base + Math.floor(Math.random() * 400);
      reconnectAttempt += 1;
      retry = window.setTimeout(open, delay);
    }

    async function open() {
      try {
        const initial = await getMatch(matchId);
        if (cancelled) return;
        applySnapshot(initial);
        setSocketStatus("connecting");

        socket = new WebSocket(websocketUrl(matchId, lastSeqRef.current, channel, speakerId));
        socketRef.current = socket;
        socket.onopen = () => {
          setSocketStatus("open");
          reconnectAttempt = 0; // 连上即清零退避计数，下次断开从最短间隔重连。
          if (channel === "speaker" && speakerId && socketRef.current) {
            void sendSpeakerHeartbeat(socketRef.current, speakerId);
          }
        };
        socket.onclose = () => {
          scheduleReconnect();
        };
        socket.onerror = () => socket?.close();
        socket.onmessage = async (event) => {
          const message = JSON.parse(event.data) as RealtimeMessage;
          if (message.type === "snapshot") {
            const state = message.payload.state as MatchSnapshot | undefined;
            if (state) {
              applySnapshot(state);
            }
            return;
          }
          lastSeqRef.current = message.seq;
          setLastEvent(message);
          // 收到实时事件后重新拉取快照，否则 UI（实时辩论过程、当前发言、计时、流式转写
          // 等依赖 snapshot 的部分）只会在手动刷新时更新。事件本身只更新 lastEvent。
          if (isCoalescableEvent(message.type)) {
            // 高频 token 流：合并到固定节流间隔，避免刷新风暴抢占后端锁、拖慢 TTS 出声。
            if (deltaRefreshTimerRef.current == null) {
              const since = Date.now() - lastRefreshAtRef.current;
              const wait = Math.max(0, DELTA_REFRESH_MIN_INTERVAL_MS - since);
              deltaRefreshTimerRef.current = window.setTimeout(() => {
                deltaRefreshTimerRef.current = undefined;
                lastRefreshAtRef.current = Date.now();
                void refresh();
              }, wait);
            }
          } else {
            // 关键事件即时刷新；顺手取消挂起的 delta 节流定时器（这次刷新已覆盖其更新）。
            if (deltaRefreshTimerRef.current != null) {
              window.clearTimeout(deltaRefreshTimerRef.current);
              deltaRefreshTimerRef.current = undefined;
            }
            lastRefreshAtRef.current = Date.now();
            await refresh();
          }
        };
      } catch (err) {
        if (cancelled) return;
        setLoadError(err instanceof Error ? err.message : "连接失败");
        scheduleReconnect();
      }
    }

    open();
    const heartbeat = window.setInterval(() => {
      if (channel === "speaker" && speakerId && socketRef.current?.readyState === WebSocket.OPEN) {
        void sendSpeakerHeartbeat(socketRef.current, speakerId);
      }
    }, 5000);

    return () => {
      cancelled = true;
      if (retry) window.clearTimeout(retry);
      window.clearInterval(heartbeat);
      if (deltaRefreshTimerRef.current != null) {
        window.clearTimeout(deltaRefreshTimerRef.current);
        deltaRefreshTimerRef.current = undefined;
      }
      socket?.close();
      socketRef.current = null;
      setSocketStatus("closed");
    };
  }, [applySnapshot, channel, matchId, refresh, speakerId]);

  function send(type: string, payload: Record<string, unknown>) {
    if (socketRef.current?.readyState !== WebSocket.OPEN) return false;
    socketRef.current.send(JSON.stringify({ type, payload }));
    return true;
  }

  return { snapshot, socketStatus, lastEvent, loadError, refresh, send };
}

export function shouldIgnoreSnapshot(prev: MatchSnapshot | null, next: MatchSnapshot): boolean {
  return Boolean(prev && next.match?.id === prev.match?.id && next.last_seq < prev.last_seq);
}

// 高频事件刷新的最小间隔（毫秒）。
export const DELTA_REFRESH_MIN_INTERVAL_MS = 300;

// 这些是 AI 生成/播放期间的高频进度类事件，刷新可合并节流：
//  - agent.speech.delta：逐 token 文本流；
//  - tts.sentence_ready：逐句音频就绪（大屏播放走事件快路 eventChunksRef，不依赖这次快照刷新）；
//  - tts.playback_progress：大屏自己上报后端回显的播放进度（reducer 不读快照里的这些计数）。
// 把它们节流可避免生成期间的快照刷新（每张 ~157KB）抢占投影机浏览器对同一主机的并发连接
// （HTTP/1.1 约 6 个），从而把连接让给真正出声的音频取流——这是中间段音频"取不到流→卡顿→
// 连环跳到结束"的根因之一。其余事件（发言起止、tts.finished、阶段切换、停止/继续等状态转换）
// 仍即时刷新，保证响应零额外时延。
const COALESCABLE_EVENT_TYPES = new Set<string>([
  "agent.speech.delta",
  "tts.sentence_ready",
  "tts.playback_progress",
]);

export function isCoalescableEvent(type: string): boolean {
  return COALESCABLE_EVENT_TYPES.has(type);
}

async function sendSpeakerHeartbeat(socket: WebSocket, speakerId: string) {
  const permission = await microphonePermission();
  socket.send(JSON.stringify({
    type: permission === "denied" ? "speaker.mic_error" : "speaker.heartbeat",
    payload: {
      speaker_id: speakerId,
      mic_permission: permission,
      device_label: "browser microphone",
      message: permission === "denied" ? "Microphone permission denied" : undefined
    }
  }));
}

async function microphonePermission(): Promise<"granted" | "denied" | "prompt" | "unknown"> {
  try {
    const permissions = navigator.permissions as Permissions | undefined;
    if (!permissions?.query) return "unknown";
    const result = await permissions.query({ name: "microphone" as PermissionName });
    if (result.state === "granted" || result.state === "denied" || result.state === "prompt") return result.state;
    return "unknown";
  } catch {
    return "unknown";
  }
}
