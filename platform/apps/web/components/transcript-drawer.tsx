"use client";

import { FileText, LockKeyhole, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { selectCaptionProjection } from "@/components/stage-caption-projection";
import { TranscriptCollaboration } from "@/components/transcript-collaboration";
import type { Room } from "@/lib/types";

function speakerName(room: Room, seatKey: string, fallback: string) {
  return room.seats?.find((seat) => seat.seat_key === seatKey)?.display_name || fallback || seatKey;
}

export function TranscriptDrawer({ room, liveEvent }: { room: Room; liveEvent?: Record<string, unknown> | null }) {
  const [open, setOpen] = useState(false);
  const [collaborationOpen, setCollaborationOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const currentStageKey = room.current_stage?.key || "";
  const speeches = useMemo(
    () => (room.speeches || [])
      .filter((speech) => currentStageKey && speech.stage_key === currentStageKey && speech.status === "completed")
      .slice()
      .sort((left, right) => new Date(left.created_at).getTime() - new Date(right.created_at).getTime()),
    [currentStageKey, room.speeches],
  );
  const liveCaption = selectCaptionProjection(room, liveEvent);
  const activeSpeaker = room.active_speech
    ? speakerName(room, room.active_speech.seat_key, room.active_speech.seat_key)
    : "";

  useEffect(() => {
    if (!open) return;
    closeRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setOpen(false);
      requestAnimationFrame(() => triggerRef.current?.focus());
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [open]);

  function close() {
    setOpen(false);
    setCollaborationOpen(false);
    requestAnimationFrame(() => triggerRef.current?.focus());
  }

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className="stage-transcript-trigger"
        aria-controls="stage-transcript-drawer"
        aria-expanded={open}
        onClick={() => setOpen(true)}
      >
        <FileText size={18} />文字记录
      </button>
      {open && (
        <div id="stage-transcript-drawer" className="stage-transcript-drawer" role="dialog" aria-modal="false" aria-labelledby="stage-transcript-title">
          <div className="stage-transcript-drawer-head">
            <div><span className="eyebrow">Live Transcript</span><h2 id="stage-transcript-title">文字记录</h2></div>
            <button ref={closeRef} type="button" className="icon-button" aria-label="关闭文字记录" onClick={close}><X size={18} /></button>
          </div>
          <div className="stage-transcript-context">
            <span>当前阶段</span>
            <strong>{room.current_stage?.name || "比赛尚未开始"}</strong>
            <small aria-live="polite">{speeches.length} 条已完成发言</small>
          </div>
          {room.active_speech && liveCaption && (
            <section className="stage-transcript-live" aria-label="当前发言实时字幕" aria-live="polite">
              <div className="stage-transcript-entry-head">
                <strong>{activeSpeaker} · 正在发言</strong>
                <span>{liveCaption.isFinal ? "已确认字幕" : "实时识别中"}</span>
              </div>
              <p>{liveCaption.text}</p>
            </section>
          )}
          {collaborationOpen ? <TranscriptCollaboration room={room} speeches={speeches} /> : (
            <div className="stage-transcript-list" role={speeches.length ? "list" : undefined} aria-label={speeches.length ? "当前阶段发言记录" : undefined}>
              {speeches.map((speech) => (
                <div key={speech.id} role="listitem" className="stage-transcript-entry">
                  <div className="stage-transcript-entry-head"><strong>{speakerName(room, speech.seat_key, speech.speaker)}</strong><time dateTime={speech.created_at}>{new Date(speech.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</time></div>
                  <p>{speech.content || "该发言暂时没有可用文字记录"}</p>
                  {speech.can_request_correction && <small>本人发言可进入协同编辑后提交修正申请，原始记录将保留。</small>}
                </div>
              ))}
              {!room.current_stage && <div className="empty">比赛阶段尚未开始，暂时没有文字记录</div>}
              {room.current_stage && !speeches.length && <div className="empty">当前阶段还没有已完成的发言记录</div>}
            </div>
          )}
          <button
            type="button"
            className="button button-secondary stage-collaboration-toggle"
            aria-expanded={collaborationOpen}
            onClick={() => setCollaborationOpen((value) => !value)}
          >
            <LockKeyhole size={16} />{collaborationOpen ? "关闭协同编辑" : "协同编辑"}
          </button>
        </div>
      )}
    </>
  );
}
