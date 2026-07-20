"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import type { User } from "@/lib/types";

const SESSION_CHANGED_EVENT = "jixia:session-changed";
let sessionRequest: Promise<User | null> | null = null;

function requestSession(): Promise<User | null> {
  if (!sessionRequest) {
    sessionRequest = apiFetch<{ user: User | null }>("/api/auth/session-state")
      .then((data) => data.user)
      .catch(() => null)
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
  const [loading, setLoading] = useState(true);
  const refresh = useCallback(async () => {
    setUser(await requestSession());
    setLoading(false);
  }, []);
  useEffect(() => {
    const onSessionChanged = () => void refresh();
    void refresh();
    window.addEventListener(SESSION_CHANGED_EVENT, onSessionChanged);
    return () => window.removeEventListener(SESSION_CHANGED_EVENT, onSessionChanged);
  }, [refresh]);
  return { user, loading, refresh };
}
