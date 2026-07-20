"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, websocketUrl } from "@/lib/api";
import type { Room } from "@/lib/types";

export function useRoom(code: string) {
  const [room, setRoom] = useState<Room | null>(null);
  const [connected, setConnected] = useState(false);
  const [connectionError, setConnectionError] = useState("");
  const [snapshotError, setSnapshotError] = useState("");
  const [liveEvent, setLiveEvent] = useState<Record<string, unknown> | null>(null);
  const [connectionAttempt, setConnectionAttempt] = useState(0);
  const retry = useRef(0);
  const mounted = useRef(false);
  const currentCode = useRef(code);
  currentCode.current = code;
  const reconnect = useCallback(() => setConnectionAttempt((value) => value + 1), []);
  const refresh = useCallback(async () => {
    const data = await apiFetch<{ room: Room }>(`/api/rooms/${code}`);
    if (mounted.current && currentCode.current === code) {
      setRoom((current) => (!current || data.room.seq >= current.seq ? data.room : current));
    }
    return data.room;
  }, [code]);

  // Next.js may preserve this hook instance while only the dynamic room code
  // changes. Never expose the previous room during that transition, even for
  // the single render before the connection effect performs its cleanup.
  const visibleRoom = room?.code === code ? room : null;

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    setRoom(null);
    setLiveEvent(null);
    setConnected(false);
    setConnectionError("");
    setSnapshotError("");
  }, [code]);

  useEffect(() => {
    let active = true;
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let heartbeat: ReturnType<typeof setInterval> | null = null;
    let lastMessageAt = Date.now();
    let heartbeatTimedOut = false;
    let roomMismatch = false;
    let terminalClosed = false;
    retry.current = 0;
    const isOnline = () => typeof navigator === "undefined" || navigator.onLine !== false;
    const clearRetry = () => {
      if (timer) clearTimeout(timer);
      timer = null;
    };
    const clearHeartbeat = () => {
      if (heartbeat) clearInterval(heartbeat);
      heartbeat = null;
    };
    const closeCurrentSocket = (code = 1000, reason = "connection reset") => {
      const socket = ws;
      ws = null;
      clearHeartbeat();
      if (socket && socket.readyState !== WebSocket.CLOSED) socket.close(code, reason);
    };
    const loadAuthoritativeSnapshot = () => {
      void refresh()
        .then(() => {
          if (active && !terminalClosed) setSnapshotError("");
        })
        .catch((err) => {
          if (active && !terminalClosed)
            setSnapshotError(err instanceof Error ? err.message : "房间载入失败");
        });
    };
    const connect = () => {
      if (!active || !isOnline() || terminalClosed) return;
      if (ws && ws.readyState !== WebSocket.CLOSED) return;
      clearRetry();
      heartbeatTimedOut = false;
      const socket = new WebSocket(websocketUrl(`/ws/rooms/${code}`));
      ws = socket;
      socket.onopen = () => {
        if (!active || ws !== socket) return;
        setConnected(false);
        lastMessageAt = Date.now();
        clearHeartbeat();
        heartbeat = setInterval(() => {
          if (socket.readyState !== WebSocket.OPEN) return;
          if (Date.now() - lastMessageAt > 45_000) {
            heartbeatTimedOut = true;
            setConnectionError("实时连接响应超时，正在重新连接…");
            socket.close(4000, "heartbeat timeout");
            return;
          }
          socket.send(JSON.stringify({ type: "ping" }));
        }, 20_000);
      };
      socket.onmessage = (event) => {
        if (!active || ws !== socket) return;
        lastMessageAt = Date.now();
        try {
          const message = JSON.parse(event.data);
          if (message.room?.code && message.room.code !== code) {
            // A room-scoped socket must never be allowed to populate another
            // room's UI, even if a proxy or fanout regression misroutes one
            // payload. Keep the last authoritative state and reconnect.
            roomMismatch = true;
            setConnectionError("实时数据房间不匹配，正在重新连接…");
            socket.close(4002, "room mismatch");
            return;
          }
          if (message.room) {
            setRoom((current) => (!current || message.room.seq >= current.seq ? message.room : current));
            setConnected(true);
            setConnectionError("");
            setSnapshotError("");
            retry.current = 0;
          }
          if (message.event) setLiveEvent(message.event);
        } catch { /* ignore malformed event */ }
      };
      socket.onerror = () => {
        if (active && ws === socket)
          setConnectionError(isOnline() ? "实时连接出现波动，正在重连…" : "网络连接已断开，恢复联网后将自动重连。");
      };
      socket.onclose = (event) => {
        if (!active || ws !== socket) return;
        ws = null;
        clearHeartbeat();
        setConnected(false);
        const terminalMessage: Record<number, string> = {
          4401: "登录状态已失效，或当前账号无权访问该房间。",
          4403: "当前账号无权访问该房间。",
          4404: "比赛房间不存在或已被删除。",
        };
        if (terminalMessage[event.code]) {
          terminalClosed = true;
          setConnectionError(terminalMessage[event.code]);
          return;
        }
        if (!isOnline()) {
          setConnectionError("网络连接已断开，恢复联网后将自动重连。");
          return;
        }
        setConnectionError(
          roomMismatch
            ? "实时数据房间不匹配，正在重新连接…"
            : heartbeatTimedOut
              ? "实时连接响应超时，正在重新连接…"
              : "实时连接已断开，正在重新连接…",
        );
        roomMismatch = false;
        const delay = Math.min(8000, 600 * 2 ** retry.current++);
        clearRetry();
        timer = setTimeout(connect, delay);
      };
    };
    const handleOffline = () => {
      if (!active) return;
      clearRetry();
      retry.current = 0;
      setConnected(false);
      setConnectionError("网络连接已断开，恢复联网后将自动重连。");
      closeCurrentSocket(4001, "browser offline");
    };
    const handleOnline = () => {
      if (!active || !isOnline() || terminalClosed) return;
      clearRetry();
      retry.current = 0;
      loadAuthoritativeSnapshot();
      if (!ws || ws.readyState === WebSocket.CLOSED) connect();
    };
    window.addEventListener("offline", handleOffline);
    window.addEventListener("online", handleOnline);
    if (isOnline()) {
      loadAuthoritativeSnapshot();
      connect();
    } else {
      handleOffline();
    }
    return () => {
      active = false;
      window.removeEventListener("offline", handleOffline);
      window.removeEventListener("online", handleOnline);
      clearRetry();
      closeCurrentSocket(1000, "hook cleanup");
    };
  }, [code, refresh, connectionAttempt]);

  return {
    room: visibleRoom,
    setRoom,
    connected,
    error: connectionError || snapshotError,
    connectionError,
    snapshotError,
    refresh,
    reconnect,
    liveEvent,
  };
}

export function useCountdown(initial: number | null, seq: number, active = true) {
  const [remaining, setRemaining] = useState(initial);
  useEffect(() => {
    setRemaining(initial);
    if (!active || initial === null || initial <= 0) return;
    const startedAt = Date.now();
    const update = () => setRemaining(Math.max(0, initial - Math.floor((Date.now() - startedAt) / 1000)));
    const timer = setInterval(update, 250);
    document.addEventListener("visibilitychange", update);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", update);
    };
  }, [active, initial, seq]);
  return remaining;
}
