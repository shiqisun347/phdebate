import { HocuspocusProvider } from "@hocuspocus/provider";
import * as Y from "yjs";

import { apiFetch, websocketUrl } from "@/lib/api";
import type { RoomSpeech } from "@/lib/types";

export type TranscriptCollabAccess = {
  document_name: string;
  token: string;
  expires_at: string;
  ws_path: string;
  role: "admin" | "participant" | "viewer";
  editable_speech_ids: string[];
};

export type TranscriptCollabConnectionState = "connecting" | "connected" | "synced" | "disconnected" | "error";

export type TranscriptCollabSession = {
  access: TranscriptCollabAccess;
  snapshot: () => Record<string, string>;
  setText: (speechId: string, value: string) => void;
  subscribe: (listener: () => void) => () => void;
  destroy: () => void;
};

function validateAccess(value: TranscriptCollabAccess): TranscriptCollabAccess {
  if (
    !value
    || typeof value.document_name !== "string"
    || !value.document_name.startsWith("room:")
    || typeof value.token !== "string"
    || !value.token
    || typeof value.ws_path !== "string"
    || !value.ws_path.startsWith("/")
    || !["admin", "participant", "viewer"].includes(value.role)
    || !Array.isArray(value.editable_speech_ids)
  ) throw new Error("协同服务返回的连接信息无效，请重新连接。");
  return value;
}

function asText(value: unknown): string {
  if (value instanceof Y.Text) return value.toString();
  return typeof value === "string" ? value : "";
}

export function initializeSpeechTexts(
  doc: Y.Doc,
  speeches: Pick<RoomSpeech, "id" | "content">[],
  editableSpeechIds: Iterable<string>,
) {
  const editable = new Set(editableSpeechIds);
  const map = doc.getMap<Y.Text | string>("speeches");
  doc.transact(() => {
    for (const speech of speeches) {
      if (!editable.has(speech.id)) continue;
      const existing = map.get(speech.id);
      if (existing instanceof Y.Text) continue;
      map.set(speech.id, new Y.Text(typeof existing === "string" ? existing : speech.content));
    }
  }, "initialize-authoritative-speeches");
  return map;
}

export function speechTextSnapshot(
  doc: Y.Doc,
  speeches: Pick<RoomSpeech, "id" | "content">[],
): Record<string, string> {
  const map = doc.getMap<Y.Text | string>("speeches");
  return Object.fromEntries(speeches.map((speech) => [speech.id, map.has(speech.id) ? asText(map.get(speech.id)) : speech.content]));
}

export function replaceSpeechText(doc: Y.Doc, speechId: string, value: string, editableSpeechIds: Iterable<string>) {
  if (!new Set(editableSpeechIds).has(speechId)) throw new Error("该发言不在当前协同编辑权限内。");
  const map = doc.getMap<Y.Text | string>("speeches");
  const text = map.get(speechId);
  if (!(text instanceof Y.Text)) throw new Error("协同文字尚未同步完成，请稍后重试。");
  doc.transact(() => {
    text.delete(0, text.length);
    text.insert(0, value);
  }, `edit-speech:${speechId}`);
}

export async function createTranscriptCollabSession({
  roomCode,
  speeches,
  displayName,
  onAccess,
  onState,
  onAwareness,
  onError,
}: {
  roomCode: string;
  speeches: RoomSpeech[];
  displayName: string;
  onAccess: (access: TranscriptCollabAccess) => void;
  onState: (state: TranscriptCollabConnectionState) => void;
  onAwareness: (editors: string[]) => void;
  onError: (message: string) => void;
}): Promise<TranscriptCollabSession> {
  let destroyed = false;
  let access = validateAccess(await apiFetch<TranscriptCollabAccess>(`/api/rooms/${roomCode}/transcript-collab-token`, {
    method: "POST",
    body: "{}",
  }));
  onAccess(access);
  const doc = new Y.Doc();
  const speechesMap = doc.getMap<Y.Text | string>("speeches");
  const listeners = new Set<() => void>();
  const notify = () => listeners.forEach((listener) => listener());
  speechesMap.observeDeep(notify);

  async function refreshToken() {
    const refreshed = validateAccess(await apiFetch<TranscriptCollabAccess>(`/api/rooms/${roomCode}/transcript-collab-token`, {
      method: "POST",
      body: "{}",
    }));
    if (refreshed.document_name !== access.document_name) throw new Error("协同文档与当前房间不匹配。");
    access = refreshed;
    if (!destroyed) onAccess(refreshed);
    return refreshed.token;
  }

  onState("connecting");
  const provider = new HocuspocusProvider({
    url: websocketUrl(access.ws_path),
    name: access.document_name,
    document: doc,
    token: refreshToken,
    flushDelay: 120,
    onStatus: ({ status }) => {
      if (destroyed) return;
      onState(status === "connected" ? "connected" : status === "connecting" ? "connecting" : "disconnected");
    },
    onSynced: ({ state }) => {
      if (!state || destroyed) return;
      initializeSpeechTexts(doc, speeches, access.editable_speech_ids);
      onState("synced");
      notify();
    },
    onAuthenticationFailed: ({ reason }) => {
      if (destroyed) return;
      onState("error");
      onError(reason || "协同编辑鉴权失败，请重新连接。");
    },
    onAwarenessUpdate: ({ states }) => {
      if (destroyed) return;
      const names = states
        .filter((state) => state.clientId !== doc.clientID)
        .map((state) => state.user?.name)
        .filter((name): name is string => typeof name === "string" && Boolean(name.trim()));
      onAwareness([...new Set(names)]);
    },
  });
  provider.setAwarenessField("user", { name: displayName, role: access.role });

  return {
    get access() { return access; },
    snapshot: () => speechTextSnapshot(doc, speeches),
    setText: (speechId, value) => replaceSpeechText(doc, speechId, value, access.editable_speech_ids),
    subscribe(listener) {
      listeners.add(listener);
      listener();
      return () => listeners.delete(listener);
    },
    destroy() {
      destroyed = true;
      speechesMap.unobserveDeep(notify);
      listeners.clear();
      provider.destroy();
      doc.destroy();
    },
  };
}
