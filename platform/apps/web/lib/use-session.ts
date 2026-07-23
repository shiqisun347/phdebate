"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import type { User } from "@/lib/types";

const SESSION_CHANGED_EVENT = "jixia:session-changed";
export type ActiveRoomSession = { code: string; topic: string; status: string; seat_key: string };
type SessionSnapshot = { user: User | null; activeRoom: ActiveRoomSession | null };
let sessionRequest: Promise<SessionSnapshot> | null = null;

function requestSession(): Promise<SessionSnapshot> {
  if (!sessionRequest) {
    sessionRequest = apiFetch<{ user: User | null; active_room?: ActiveRoomSession | null }>("/api/auth/session-state")
      .then((data) => ({ user: data.user, activeRoom: data.active_room || null }))
      .catch(() => ({ user: null, activeRoom: null }))
      .finally(() => {
        sessionRequest = null;
      });
  }
  return sessionRequest;
}

export function notifySessionChanged() {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(SESSION_CHANGED_EVENT));
}

export function useSession() {
  const [user, setUser] = useState<User | null>(null);
  const [activeRoom, setActiveRoom] = useState<ActiveRoomSession | null>(null);
  const [loading, setLoading] = useState(true);
  const refresh = useCallback(async () => {
    const snapshot = await requestSession();
    setUser(snapshot.user);
    setActiveRoom(snapshot.activeRoom);
    setLoading(false);
  }, []);
  useEffect(() => {
    const onSessionChanged = () => void refresh();
    void refresh();
    window.addEventListener(SESSION_CHANGED_EVENT, onSessionChanged);
    return () => window.removeEventListener(SESSION_CHANGED_EVENT, onSessionChanged);
  }, [refresh]);
  return { user, activeRoom, loading, refresh };
}
