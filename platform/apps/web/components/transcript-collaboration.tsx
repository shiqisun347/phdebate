"use client";

import { CircleAlert, CloudOff, LoaderCircle, RefreshCw, Save, Users } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { SpeechCorrectionRequest } from "@/components/speech-correction-control";
import { apiFetch } from "@/lib/api";
import type { Room, RoomSpeech } from "@/lib/types";
import type {
  TranscriptCollabAccess,
  TranscriptCollabConnectionState,
  TranscriptCollabSession,
} from "@/lib/transcript-collab-client";

const stateLabel: Record<TranscriptCollabConnectionState, string> = {
  connecting: "正在连接协同服务",
  connected: "已连接，正在同步",
  synced: "协同文字已同步",
  disconnected: "连接中断，正在自动恢复",
  error: "协同连接失败",
};

function currentDisplayName(room: Room) {
  return room.seats?.find((seat) => seat.is_me)?.display_name || (room.can_control ? "房间管理员" : "协同查看者");
}

export function TranscriptCollaboration({ room, speeches }: { room: Room; speeches: RoomSpeech[] }) {
  const [retryNonce, setRetryNonce] = useState(0);
  const [access, setAccess] = useState<TranscriptCollabAccess | null>(null);
  const [connectionState, setConnectionState] = useState<TranscriptCollabConnectionState>("connecting");
  const [editors, setEditors] = useState<string[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>(() => Object.fromEntries(speeches.map((speech) => [speech.id, speech.content])));
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [requests, setRequests] = useState<Record<string, SpeechCorrectionRequest>>({});
  const [busySpeechId, setBusySpeechId] = useState("");
  const [error, setError] = useState("");
  const sessionRef = useRef<TranscriptCollabSession | null>(null);
  const speechesRef = useRef(speeches);
  const operationKeys = useRef(new Map<string, string>());
  const displayName = currentDisplayName(room);
  const speechSignature = speeches.map((speech) => `${speech.id}\u0000${speech.content}`).join("\u0001");
  speechesRef.current = speeches;

  useEffect(() => {
    let active = true;
    let unsubscribe: () => void = () => {};
    const sessionSpeeches = speechesRef.current;
    setAccess(null);
    setConnectionState("connecting");
    setEditors([]);
    setError("");
    void import("@/lib/transcript-collab-client")
      .then(({ createTranscriptCollabSession }) => createTranscriptCollabSession({
        roomCode: room.code,
        speeches: sessionSpeeches,
        displayName,
        onAccess: (next) => { if (active) setAccess(next); },
        onState: (next) => { if (active) setConnectionState(next); },
        onAwareness: (next) => { if (active) setEditors(next); },
        onError: (message) => { if (active) setError(message); },
      }))
      .then((session) => {
        if (!active) {
          session.destroy();
          return;
        }
        sessionRef.current = session;
        setAccess(session.access);
        unsubscribe = session.subscribe(() => setDrafts(session.snapshot()));
        void apiFetch<{ items: SpeechCorrectionRequest[] }>(`/api/rooms/${room.code}/speech-correction-requests`)
          .then((result) => {
            if (!active) return;
            setRequests(Object.fromEntries(result.items.map((request) => [request.speech_id, request])));
          })
          .catch(() => undefined);
      })
      .catch((reason) => {
        if (!active) return;
        setConnectionState("error");
        setError(reason instanceof Error ? reason.message : "协同编辑暂时不可用。");
      });
    return () => {
      active = false;
      unsubscribe();
      sessionRef.current?.destroy();
      sessionRef.current = null;
    };
  }, [displayName, retryNonce, room.code, speechSignature]);

  function updateDraft(speechId: string, value: string) {
    try {
      sessionRef.current?.setText(speechId, value);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "协同文字更新失败。");
    }
  }

  async function submitCorrection(speech: RoomSpeech) {
    if (access?.role !== "participant" || !access.editable_speech_ids.includes(speech.id)) return;
    const proposedContent = (drafts[speech.id] || "").trim();
    const reason = (reasons[speech.id] || "").trim();
    if (proposedContent.length < 2 || proposedContent === speech.content.trim() || reason.length < 2) return;
    if (!window.confirm("确认把当前协同草稿提交为发言修正申请？原始记录会保留，管理员审核通过后才会更新。")) return;
    setBusySpeechId(speech.id);
    setError("");
    try {
      let operationKey = operationKeys.current.get(speech.id);
      if (!operationKey) {
        operationKey = crypto.randomUUID();
        operationKeys.current.set(speech.id, operationKey);
      }
      const result = await apiFetch<{ request: SpeechCorrectionRequest }>(
        `/api/rooms/${room.code}/speeches/${speech.id}/correction-requests`,
        {
          method: "POST",
          headers: { "X-Idempotency-Key": operationKey },
          body: JSON.stringify({ proposed_content: proposedContent, reason }),
        },
      );
      operationKeys.current.delete(speech.id);
      setRequests((current) => ({ ...current, [speech.id]: result.request }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "修正申请提交失败，请重试。");
    } finally {
      setBusySpeechId("");
    }
  }

  const synced = connectionState === "synced";
  const editableIds = new Set(access?.editable_speech_ids || []);

  return (
    <section className="transcript-collaboration" aria-labelledby="transcript-collaboration-title">
      <div className="transcript-collaboration-status">
        <div>
          <h3 id="transcript-collaboration-title">协同文字草稿</h3>
          <span className={`collab-state ${connectionState}`} role="status">
            {connectionState === "connecting" ? <LoaderCircle className="inline-spinner" size={13} /> : connectionState === "disconnected" ? <CloudOff size={13} /> : null}
            {stateLabel[connectionState]}
          </span>
        </div>
        <span className="collab-awareness" aria-label={`${editors.length} 位其他编辑者在线`}><Users size={14} />{editors.length ? editors.join("、") : "暂无其他编辑者"}</span>
      </div>

      {access?.role === "admin" && <div className="notice-box">管理员可以协同查看和编辑草稿，但不能以辩手作者身份提交修正申请。</div>}
      {access?.role === "viewer" && <div className="notice-box">当前账号没有本场可编辑的本人发言，协同文档以只读方式打开。</div>}
      {error && (
        <div className="collab-error" role="alert">
          <CircleAlert size={15} /><span>{error}<small>上方正式文字记录仍保持只读可用。</small></span>
          <button type="button" className="button button-small button-secondary" onClick={() => setRetryNonce((value) => value + 1)}><RefreshCw size={14} />重新连接</button>
        </div>
      )}

      <div className="collab-speech-list">
        {speeches.map((speech) => {
          const editable = editableIds.has(speech.id);
          const request = requests[speech.id];
          const pending = request?.status === "pending";
          const draft = drafts[speech.id] ?? speech.content;
          const changed = draft.trim() !== speech.content.trim();
          const canSubmit = access?.role === "participant" && editable && synced && changed && draft.trim().length >= 2 && (reasons[speech.id] || "").trim().length >= 2 && !pending;
          return (
            <article key={speech.id} className={`collab-speech${editable ? " editable" : " readonly"}`}>
              <header><strong>{speech.speaker}</strong><span>{editable ? "可协同编辑" : "只读"}</span></header>
              {editable ? (
                <textarea
                  aria-label={`${speech.speaker}的协同文字草稿`}
                  value={draft}
                  disabled={!synced || pending}
                  maxLength={20_000}
                  onChange={(event) => updateDraft(speech.id, event.target.value)}
                />
              ) : <p>{draft || speech.content || "该发言暂时没有可用文字记录"}</p>}
              {pending && <div className="collab-pending" role="status"><strong>修正申请等待管理员审核</strong><small>申请理由：{request.reason}</small></div>}
              {access?.role === "participant" && editable && !pending && (
                <div className="collab-submit">
                  <label htmlFor={`collab-reason-${speech.id}`}>{speech.speaker}的修正原因（必填）</label>
                  <textarea
                    id={`collab-reason-${speech.id}`}
                    value={reasons[speech.id] || ""}
                    maxLength={500}
                    disabled={!synced || Boolean(busySpeechId)}
                    placeholder="说明语音识别错误或需要修正的具体原因"
                    onChange={(event) => setReasons((current) => ({ ...current, [speech.id]: event.target.value }))}
                  />
                  <button type="button" className="button button-small" disabled={!canSubmit || Boolean(busySpeechId)} onClick={() => void submitCorrection(speech)}>
                    {busySpeechId === speech.id ? <><LoaderCircle className="inline-spinner" size={14} />正在提交…</> : <><Save size={14} />提交修正申请</>}
                  </button>
                </div>
              )}
            </article>
          );
        })}
        {!speeches.length && <div className="empty">当前阶段还没有可协同处理的已完成发言。</div>}
      </div>
    </section>
  );
}
