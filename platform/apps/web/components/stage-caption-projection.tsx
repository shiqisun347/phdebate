"use client";

import { useLayoutEffect } from "react";

import type { CaptionSegment, Room } from "@/lib/types";

type CaptionProjection = {
  text: string;
  isFinal: boolean;
  segmentId: string | null;
  updatedAt: number;
};

const CAPTION_TARGET = ".stage-page .subtitle-stage p";
const MANAGED_ATTRIBUTE = "data-caption-projection";

function timestamp(segment: CaptionSegment) {
  const parsed = Date.parse(segment.updated_at);
  if (Number.isFinite(parsed)) return parsed;
  return segment.end_ms ?? segment.start_ms ?? 0;
}

function validSegment(segment: CaptionSegment, activeSpeechId: string) {
  return Boolean(
    segment
    && segment.segment_id
    && segment.speech_id === activeSpeechId
    && segment.text.trim(),
  );
}

export function selectCaptionProjection(
  room: Room,
  liveEvent?: Record<string, unknown> | null,
): CaptionProjection | null {
  const activeSpeechId = room.active_speech?.id;
  if (!activeSpeechId) return null;

  if (
    liveEvent
    && ["asr", "caption.segment"].includes(String(liveEvent.type || ""))
    && typeof liveEvent.text === "string"
    && liveEvent.text.trim()
    && (typeof liveEvent.speech_id !== "string" || liveEvent.speech_id === activeSpeechId)
  ) {
    const eventTimestamp = typeof liveEvent.timestamp_ms === "number"
      ? liveEvent.timestamp_ms
      : typeof liveEvent.updated_at === "string" && Number.isFinite(Date.parse(liveEvent.updated_at))
        ? Date.parse(liveEvent.updated_at)
        : 0;
    return {
      text: liveEvent.text.trim(),
      isFinal: liveEvent.is_final === true,
      segmentId: typeof liveEvent.segment_id === "string" ? liveEvent.segment_id : null,
      updatedAt: eventTimestamp,
    };
  }

  const deduplicated = new Map<string, CaptionSegment>();
  for (const segment of room.caption_segments || []) {
    if (!validSegment(segment, activeSpeechId)) continue;
    const current = deduplicated.get(segment.segment_id);
    if (
      !current
      || timestamp(segment) > timestamp(current)
      || (timestamp(segment) === timestamp(current) && segment.is_final && !current.is_final)
    ) deduplicated.set(segment.segment_id, segment);
  }
  const ordered = [...deduplicated.values()].sort((left, right) => timestamp(left) - timestamp(right));
  const latest = ordered.at(-1);
  if (!latest) return null;
  return {
    text: latest.text.trim(),
    isFinal: latest.is_final,
    segmentId: latest.segment_id,
    updatedAt: timestamp(latest),
  };
}

export function StageCaptionProjection({ room, liveEvent }: { room: Room; liveEvent?: Record<string, unknown> | null }) {
  const projection = selectCaptionProjection(room, liveEvent);
  const activeSpeechId = room.active_speech?.id || "";
  const captionState = projection?.isFinal ? "final" : projection ? "interim" : "empty";
  const displayText = activeSpeechId
    ? projection?.text || "当前发言暂时没有逐句字幕"
    : "";

  useLayoutEffect(() => {
    const target = document.querySelector<HTMLElement>(CAPTION_TARGET);
    if (!target || !activeSpeechId) return;
    const originalText = target.textContent || "";
    const originalState = target.getAttribute(MANAGED_ATTRIBUTE);
    target.textContent = displayText;
    target.setAttribute(MANAGED_ATTRIBUTE, captionState);
    target.scrollLeft = target.scrollWidth;
    return () => {
      if (!target.hasAttribute(MANAGED_ATTRIBUTE)) return;
      target.textContent = originalText;
      if (originalState === null) target.removeAttribute(MANAGED_ATTRIBUTE);
      else target.setAttribute(MANAGED_ATTRIBUTE, originalState);
    };
  }, [activeSpeechId, captionState, displayText, room.seq]);

  return null;
}
