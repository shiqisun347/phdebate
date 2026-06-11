import { useCallback, useEffect, useRef, useState } from "react";
import { getMatch, websocketUrl } from "../api/client";
import type { MatchSnapshot, RealtimeMessage } from "../types/contracts";

export function useMatch(matchId: string, channel: string, speakerId?: string) {
  const [snapshot, setSnapshot] = useState<MatchSnapshot | null>(null);
  const [socketStatus, setSocketStatus] = useState<"connecting" | "open" | "closed" | "reconnecting">("connecting");
  const [lastEvent, setLastEvent] = useState<RealtimeMessage | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const lastSeqRef = useRef(0);
  const socketRef = useRef<WebSocket | null>(null);

  const refresh = useCallback(async () => {
    const next = await getMatch(matchId);
    lastSeqRef.current = next.last_seq;
    setSnapshot(next);
    setLoadError(null);
  }, [matchId]);

  useEffect(() => {
    let cancelled = false;
    let socket: WebSocket | null = null;
    let retry: number | undefined;

    async function open() {
      try {
        const initial = await getMatch(matchId);
        if (cancelled) return;
        lastSeqRef.current = initial.last_seq;
        setSnapshot(initial);
        setLoadError(null);
        setSocketStatus("connecting");

        socket = new WebSocket(websocketUrl(matchId, lastSeqRef.current, channel, speakerId));
        socketRef.current = socket;
        socket.onopen = () => {
          setSocketStatus("open");
          if (channel === "speaker" && speakerId && socketRef.current) {
            void sendSpeakerHeartbeat(socketRef.current, speakerId);
          }
        };
        socket.onclose = () => {
          if (cancelled) return;
          setSocketStatus("reconnecting");
          retry = window.setTimeout(open, 1200);
        };
        socket.onerror = () => socket?.close();
        socket.onmessage = async (event) => {
          const message = JSON.parse(event.data) as RealtimeMessage;
          if (message.type === "snapshot") {
            const state = message.payload.state as MatchSnapshot | undefined;
            if (state) {
              lastSeqRef.current = state.last_seq;
              setSnapshot(state);
            }
            return;
          }
          lastSeqRef.current = message.seq;
          setLastEvent(message);
          await refresh();
        };
      } catch (err) {
        if (cancelled) return;
        setLoadError(err instanceof Error ? err.message : "连接失败");
        setSocketStatus("reconnecting");
        retry = window.setTimeout(open, 1200);
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
      socket?.close();
      socketRef.current = null;
      setSocketStatus("closed");
    };
  }, [channel, matchId, refresh, speakerId]);

  function send(type: string, payload: Record<string, unknown>) {
    if (socketRef.current?.readyState !== WebSocket.OPEN) return false;
    socketRef.current.send(JSON.stringify({ type, payload }));
    return true;
  }

  return { snapshot, socketStatus, lastEvent, loadError, refresh, send };
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
