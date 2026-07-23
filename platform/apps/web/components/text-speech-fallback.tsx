"use client";

import { Keyboard, LoaderCircle, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { apiFetch } from "@/lib/api";
import { controlLeaseFor } from "@/lib/control-lease";
import type { Room } from "@/lib/types";

import styles from "./text-speech-fallback.module.css";

type StartResponse = { speech_id: string; room?: Room; resumed?: boolean };
type FinishResponse = { speech_id?: string; room?: Room };
type SpeechBinding = {
  speechId: string;
  stageKey: string;
  stageName: string;
  seatKey: string;
};

function controlLease(room: Room) {
  return controlLeaseFor(room.code, room.my_seat || "");
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
  const [binding, setBinding] = useState<SpeechBinding | null>(null);
  const [content, setContent] = useState("");
  const [error, setError] = useState("");
  const [controlConflict, setControlConflict] = useState(false);
  const [observedBoundSpeech, setObservedBoundSpeech] = useState(false);
  const startKey = useRef("");
  const finishKey = useRef("");
  const textarea = useRef<HTMLTextAreaElement | null>(null);
  const mySeat = room.seats.find((seat) => seat.is_me);
  const terminal = ["completed", "review_required", "terminated"].includes(room.status);
  const available = Boolean(
    connected
      && room.status === "running"
      && room.can_speak
      && !room.active_speech
      && room.my_seat
      && mySeat?.occupant_type === "human",
  );
  const pending = Boolean(binding);
  const boundSpeechEvent = binding
    ? [...room.recent_events].reverse().find((event) => (
        ["speech.completed", "speech.interrupted", "speech.timed_out"].includes(event.type)
        && event.payload.speech_id === binding.speechId
      ))
    : undefined;
  const boundSpeechStatus = binding
    ? room.speeches.find((speech) => speech.id === binding.speechId)?.status
    : undefined;
  const lateFinalizable = Boolean(
    binding
      && (boundSpeechStatus === "timed_out" || boundSpeechEvent?.type === "speech.timed_out"),
  );
  const staleReason = !binding
    ? ""
    : ["terminated", "cancelled"].includes(room.status)
      ? "比赛已经终止，这份未提交文字不能再计入比赛。"
      : room.my_seat !== binding.seatKey
        ? "当前页面绑定的辩手席位已经变化，这份文字不能继续提交。"
        : lateFinalizable
          ? ""
          : room.active_speech && room.active_speech.id !== binding.speechId
            ? "当前轮次已有另一条发言记录，这份旧文字不能继续提交。"
            : boundSpeechEvent
              ? boundSpeechEvent.type === "speech.completed"
                ? "本次发言已经完成，这份本地文字不会重复提交。"
                : "本次发言已被中断，这份文字不能再提交。"
              : terminal
                ? "比赛已经结束，这份未提交文字不能再计入比赛。"
                : !room.current_stage || room.current_stage.key !== binding.stageKey
                  ? `比赛已从“${binding.stageName}”切换到“${room.current_stage?.name || "下一环节"}”，上一环节的文字不能提交到当前比赛进度。`
                  : observedBoundSpeech && !room.active_speech
                    ? "服务端已结束本次发言，这份文字不能再提交到旧轮次。"
                    : "";
  const stale = Boolean(staleReason);
  const canSubmit = useMemo(
    () => content.trim().length >= 2 && !starting && !submitting && !stale,
    [content, stale, starting, submitting],
  );

  useEffect(() => {
    onPendingChange?.(pending);
    return () => onPendingChange?.(false);
  }, [onPendingChange, pending]);

  useEffect(() => {
    if (open && binding) textarea.current?.focus();
  }, [binding, open]);

  useEffect(() => {
    if (!binding) {
      setObservedBoundSpeech(false);
      return;
    }
    if (room.active_speech?.id === binding.speechId) setObservedBoundSpeech(true);
  }, [binding, room.active_speech?.id]);

  useEffect(() => {
    if (!binding || !stale) return;
    // Once the authoritative speech or stage changes, this editor no longer
    // owns a valid turn. Leaving the stale modal mounted over the next stage
    // made a successful text submission look unfinished and blocked the
    // speaker's controls. The server already guards the old speech id; clear
    // the matching local draft as soon as that ownership boundary changes.
    setOpen(false);
    setBinding(null);
    setContent("");
    setError("");
    setControlConflict(false);
    setObservedBoundSpeech(false);
    startKey.current = "";
    finishKey.current = "";
  }, [binding, stale]);

  async function beginTextSpeech(force = false) {
    if (starting || binding || !available) return;
    setOpen(true);
    setStarting(true);
    setError("");
    setControlConflict(false);
    const lease = controlLease(room);
    startKey.current ||= crypto.randomUUID();
    finishKey.current ||= crypto.randomUUID();
    try {
      await apiFetch(`/api/rooms/${room.code}/control-lease`, {
        method: "POST",
        headers: { "X-Control-Lease": lease },
        body: JSON.stringify({ force }),
      });
      const result = await apiFetch<StartResponse>(`/api/rooms/${room.code}/speech/start`, {
        method: "POST",
        headers: {
          "X-Control-Lease": lease,
          "X-Idempotency-Key": startKey.current,
        },
        body: "{}",
      });
      const stage = room.current_stage;
      setBinding({
        speechId: result.speech_id,
        stageKey: stage?.key || "",
        stageName: stage?.name || "当前环节",
        seatKey: room.my_seat || "",
      });
      if (result.room) onRoomChanged?.(result.room);
    } catch (reason) {
      if (reason && typeof reason === "object" && "status" in reason && (reason as { status?: number }).status === 409) {
        setControlConflict(true);
      }
      setError(reason instanceof Error ? reason.message : "暂时无法开始文字发言，请重试。");
    } finally {
      setStarting(false);
    }
  }

  async function submitTextSpeech() {
    if (!binding || !canSubmit || stale) return;
    setSubmitting(true);
    setError("");
    try {
      const result = await apiFetch<FinishResponse>(`/api/rooms/${room.code}/speech/finish`, {
        method: "POST",
        headers: {
          "X-Control-Lease": controlLease(room),
          "X-Idempotency-Key": finishKey.current,
        },
        body: JSON.stringify({ speech_id: binding.speechId, content: content.trim() }),
      });
      if (result.room) onRoomChanged?.(result.room);
      setOpen(false);
      setBinding(null);
      setContent("");
      setError("");
      setControlConflict(false);
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
    setControlConflict(false);
  }

  // Do not paint even one stale-dialog frame while the cleanup effect above
  // runs after a WebSocket stage snapshot.
  if (stale || (!available && !open && !pending)) return null;

  return (
    <>
      {!open && (available || pending) && (
        <button type="button" className={styles.launcher} data-text-speech-launcher onClick={() => pending ? setOpen(true) : void beginTextSpeech()}>
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
                : lateFinalizable
                  ? `“${binding?.stageName || "本轮发言"}”已到时，输入内容仍保留在本页。提交后会补写到原发言记录，不会归入当前阶段。`
                : binding
                  ? `已绑定“${binding?.stageName || "当前环节"}”和本次发言记录。阶段或轮次改变后将自动停止提交，避免文字归入错误环节。`
                  : "麦克风权限、设备或浏览器暂时不可用时，可以直接提交文字发言。"}
            </p>
            <label htmlFor="text-speech-content">本轮发言文字</label>
            <textarea
              ref={textarea}
              id="text-speech-content"
              value={content}
              maxLength={20_000}
              disabled={!binding || submitting}
              placeholder={starting ? "正在准备文字输入…" : "请完整输入你的论点、论据与结论"}
              onChange={(event) => setContent(event.target.value)}
            />
            <div className={styles.meta}>
              <span>{content.trim().length.toLocaleString("zh-CN")} / 20,000 字</span>
              <span>{binding ? `目标环节：${binding.stageName}` : "服务端仍会校验房间、席位和当前轮次"}</span>
            </div>
            {error && <div className={styles.error} role="alert">{error}</div>}
            <footer>
              {!binding && (
                <>
                  {controlConflict && <button type="button" className="button button-secondary" disabled={starting || !available} onClick={() => void beginTextSpeech(true)}>
                    确认接管当前席位
                  </button>}
                  <button type="button" className="button button-secondary" disabled={starting || !available} onClick={() => void beginTextSpeech()}>
                    {starting ? <><LoaderCircle className={styles.spinner} size={17} />正在准备…</> : controlConflict ? "再次尝试（不接管）" : "重试进入文字发言"}
                  </button>
                </>
              )}
              <button type="button" className="button button-green" disabled={!canSubmit || !binding} onClick={() => void submitTextSpeech()}>
                {submitting ? <><LoaderCircle className={styles.spinner} size={17} />正在提交…</> : lateFinalizable ? "补交已超时发言" : "提交本轮发言"}
              </button>
            </footer>
          </section>
        </div>
      )}
    </>
  );
}
