"use client";

import { Keyboard, LoaderCircle, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { apiFetch } from "@/lib/api";
import type { Room } from "@/lib/types";

import styles from "./text-speech-fallback.module.css";

type StartResponse = { speech_id: string; room?: Room; resumed?: boolean };
type FinishResponse = { speech_id?: string; room?: Room };

function controlLease(room: Room) {
  const key = `jixia-control:${room.code}:${room.my_seat || "watch"}`;
  let value = sessionStorage.getItem(key);
  if (!value) {
    value = crypto.randomUUID();
    sessionStorage.setItem(key, value);
  }
  return value;
}

export function TextSpeechFallback({
  room,
  connected,
  onPendingChange,
  onRoomChanged,
}: {
  room: Room;
  connected: boolean;
  onPendingChange?: (pending: boolean) => void;
  onRoomChanged?: (room: Room) => void;
}) {
  const [open, setOpen] = useState(false);
  const [starting, setStarting] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [speechId, setSpeechId] = useState("");
  const [content, setContent] = useState("");
  const [error, setError] = useState("");
  const startKey = useRef("");
  const finishKey = useRef("");
  const textarea = useRef<HTMLTextAreaElement | null>(null);
  const mySeat = room.seats.find((seat) => seat.is_me);
  const available = Boolean(
    connected
      && room.status === "running"
      && room.can_speak
      && !room.active_speech
      && room.my_seat
      && mySeat?.occupant_type === "human",
  );
  const pending = Boolean(speechId);
  const canSubmit = useMemo(() => content.trim().length >= 2 && !starting && !submitting, [content, starting, submitting]);

  useEffect(() => {
    onPendingChange?.(pending);
    return () => onPendingChange?.(false);
  }, [onPendingChange, pending]);

  useEffect(() => {
    if (open && speechId) textarea.current?.focus();
  }, [open, speechId]);

  async function beginTextSpeech() {
    if (starting || speechId || !available) return;
    setOpen(true);
    setStarting(true);
    setError("");
    const lease = controlLease(room);
    startKey.current ||= crypto.randomUUID();
    finishKey.current ||= crypto.randomUUID();
    try {
      await apiFetch(`/api/rooms/${room.code}/control-lease`, {
        method: "POST",
        headers: { "X-Control-Lease": lease },
        body: JSON.stringify({ force: false }),
      });
      const result = await apiFetch<StartResponse>(`/api/rooms/${room.code}/speech/start`, {
        method: "POST",
        headers: {
          "X-Control-Lease": lease,
          "X-Idempotency-Key": startKey.current,
        },
        body: "{}",
      });
      setSpeechId(result.speech_id);
      if (result.room) onRoomChanged?.(result.room);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "暂时无法开始文字发言，请重试。");
    } finally {
      setStarting(false);
    }
  }

  async function submitTextSpeech() {
    if (!speechId || !canSubmit) return;
    setSubmitting(true);
    setError("");
    try {
      const result = await apiFetch<FinishResponse>(`/api/rooms/${room.code}/speech/finish`, {
        method: "POST",
        headers: {
          "X-Control-Lease": controlLease(room),
          "X-Idempotency-Key": finishKey.current,
        },
        body: JSON.stringify({ speech_id: speechId, content: content.trim() }),
      });
      if (result.room) onRoomChanged?.(result.room);
      setOpen(false);
      setSpeechId("");
      setContent("");
      setError("");
      startKey.current = "";
      finishKey.current = "";
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "文字发言提交失败，内容已保留，请重试。");
    } finally {
      setSubmitting(false);
    }
  }

  function closeEditor() {
    setOpen(false);
    setError("");
  }

  if (!available && !open && !pending) return null;

  return (
    <>
      {!open && (available || pending) && (
        <button type="button" className={styles.launcher} onClick={() => pending ? setOpen(true) : void beginTextSpeech()}>
          <Keyboard size={17} aria-hidden="true" />
          <span>{pending ? "文字内容仍在本页" : "麦克风不可用？"}<strong>{pending ? "继续文字发言" : "改用文字发言"}</strong></span>
        </button>
      )}
      {open && (
        <div className={styles.backdrop}>
          <section className={styles.dialog} role="dialog" aria-modal="true" aria-labelledby="text-speech-title" aria-describedby="text-speech-help">
            <header>
              <div>
                <span className={styles.eyebrow}>HUMAN SPEECH RECOVERY</span>
                <h2 id="text-speech-title">改用文字完成本轮发言</h2>
              </div>
              <button type="button" className={styles.close} aria-label="返回语音发言" onClick={closeEditor}>
                <X size={18} />
              </button>
            </header>
            <p id="text-speech-help" className={styles.help}>
              {starting
                ? "正在锁定当前席位和轮次…"
                : speechId
                  ? "当前轮次已为你保留。输入完整观点后提交；即使本轮计时结束，已输入内容也不会丢失。"
                  : "麦克风权限、设备或浏览器暂时不可用时，可以直接提交文字发言。"}
            </p>
            <label htmlFor="text-speech-content">本轮发言文字</label>
            <textarea
              ref={textarea}
              id="text-speech-content"
              value={content}
              maxLength={20_000}
              disabled={!speechId || submitting}
              placeholder={starting ? "正在准备文字输入…" : "请完整输入你的论点、论据与结论"}
              onChange={(event) => setContent(event.target.value)}
            />
            <div className={styles.meta}>
              <span>{content.trim().length.toLocaleString("zh-CN")} / 20,000 字</span>
              <span>服务端仍会校验房间、席位和当前轮次</span>
            </div>
            {error && <div className={styles.error} role="alert">{error}</div>}
            <footer>
              {!speechId && (
                <button type="button" className="button button-secondary" disabled={starting || !available} onClick={() => void beginTextSpeech()}>
                  {starting ? <><LoaderCircle className={styles.spinner} size={17} />正在准备…</> : "重试进入文字发言"}
                </button>
              )}
              <button type="button" className="button button-green" disabled={!canSubmit || !speechId} onClick={() => void submitTextSpeech()}>
                {submitting ? <><LoaderCircle className={styles.spinner} size={17} />正在提交…</> : "提交本轮发言"}
              </button>
            </footer>
          </section>
        </div>
      )}
    </>
  );
}
