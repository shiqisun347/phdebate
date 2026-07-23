"use client";

import { useEffect, useState } from "react";

import { compactCaptionLine } from "@/lib/caption-line";
import type { CaptionSegment, Room } from "@/lib/types";

type CaptionProjection = {
  text: string;
  isFinal: boolean;
  segmentId: string | null;
  updatedAt: number;
};

function timestamp(segment: CaptionSegment) {
  if (isPlaybackTimedAgentSegment(segment)) return segment.start_ms ?? 0;
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

function isPlaybackTimedAgentSegment(segment: CaptionSegment) {
  return segment.source === "agent"
    && ["audio_duration", "estimated_playback"].includes(segment.timing_basis || "");
}

function playbackElapsedMs(room: Room, nowMs: number) {
  const playbackStartedAt = room.active_speech?.playback_started_at
    ? Date.parse(room.active_speech.playback_started_at)
    : Number.NaN;
  return Number.isFinite(playbackStartedAt) && nowMs >= playbackStartedAt
    ? nowMs - playbackStartedAt
    : null;
}

function playbackTimedSegmentEligible(room: Room, startMs: number, nowMs: number) {
  const elapsedMs = playbackElapsedMs(room, nowMs);
  return ["playing", "synthesizing"].includes(room.active_speech?.status || "")
    && elapsedMs !== null
    && startMs <= elapsedMs;
}

export function selectCaptionProjection(
  room: Room,
  liveEvent?: Record<string, unknown> | null,
  nowMs = Date.now(),
): CaptionProjection | null {
  const activeSpeechId = room.active_speech?.id;
  if (!activeSpeechId || room.status !== "running") return null;

  if (
    liveEvent
    && ["asr", "caption.segment"].includes(String(liveEvent.type || ""))
    && typeof liveEvent.text === "string"
    && liveEvent.text.trim()
    // Every current producer includes a speech id. Requiring the exact active
    // speech prevents the last event from the previous turn being painted into
    // a newly-created subtitle surface while the room snapshot catches up.
    && liveEvent.speech_id === activeSpeechId
  ) {
    const liveTimingBasis = typeof liveEvent.timing_basis === "string" ? liveEvent.timing_basis : "";
    const livePlaybackTimed = liveEvent.type === "caption.segment"
      && ["audio_duration", "estimated_playback"].includes(liveTimingBasis);
    if (
      !livePlaybackTimed
      || playbackTimedSegmentEligible(
        room,
        typeof liveEvent.start_ms === "number" ? liveEvent.start_ms : 0,
        nowMs,
      )
    ) {
      const eventTimestamp = typeof liveEvent.timestamp_ms === "number"
        ? liveEvent.timestamp_ms
        : typeof liveEvent.updated_at === "string" && Number.isFinite(Date.parse(liveEvent.updated_at))
          ? Date.parse(liveEvent.updated_at)
          : 0;
      return {
        text: compactCaptionLine(liveEvent.text),
        isFinal: liveEvent.is_final === true,
        segmentId: typeof liveEvent.segment_id === "string" ? liveEvent.segment_id : null,
        updatedAt: eventTimestamp,
      };
    }
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
  const eligible = ordered.filter((segment) => {
    if (!isPlaybackTimedAgentSegment(segment)) return true;
    return playbackTimedSegmentEligible(room, segment.start_ms ?? 0, nowMs);
  });
  const latest = eligible.at(-1);
  if (!latest) return null;
  return {
    text: compactCaptionLine(latest.text),
    isFinal: latest.is_final,
    segmentId: latest.segment_id,
    updatedAt: timestamp(latest),
  };
}

function useStageCaptionProjection(room: Room, liveEvent?: Record<string, unknown> | null) {
  const [clock, setClock] = useState(() => Date.now());
  const projection = selectCaptionProjection(room, liveEvent, clock);
  const activeSpeechId = room.active_speech?.id || "";
  const playbackIdentity = `${activeSpeechId}:${room.active_speech?.playback_started_at || ""}:${room.active_speech?.stream_generation || ""}`;

  useEffect(() => {
    setClock(Date.now());
    if (
      room.status !== "running"
      || !["playing", "synthesizing"].includes(room.active_speech?.status || "")
      || !(room.caption_segments || []).some(isPlaybackTimedAgentSegment)
    ) return;
    const timer = window.setInterval(() => setClock(Date.now()), 100);
    return () => window.clearInterval(timer);
  }, [playbackIdentity, room.caption_segments, room.status, room.active_speech?.status]);

  return projection;
}

type StageSubtitleProps = {
  room: Room;
  liveEvent?: Record<string, unknown> | null;
  capturing: boolean;
  localCaption: string;
  aiPreparing: boolean;
  idleText: string;
  attribution: string;
};

/**
 * The sole owner of the visible debate subtitle line.
 *
 * Keeping projection and rendering in one small component avoids the previous
 * race where React rendered one value while StageCaptionProjection mutated the
 * same DOM node in a layout effect. That race was most visible during pause,
 * reconnect and rapid stage transitions as a blank or stale subtitle.
 */
export function StageSubtitle({
  room,
  liveEvent,
  capturing,
  localCaption,
  aiPreparing,
  idleText,
  attribution,
}: StageSubtitleProps) {
  const projection = useStageCaptionProjection(room, liveEvent);
  const activeSpeechId = room.active_speech?.id || "";
  const projectedText = activeSpeechId && room.status === "running"
    ? projection?.text || (aiPreparing ? "AI 正在组织论点并合成语音…" : "当前发言暂时没有逐句字幕")
    : idleText;
  const displayText = capturing
    ? compactCaptionLine(localCaption) || "正在聆听你的发言…"
    : projectedText;
  const captionState = capturing
    ? localCaption.trim() ? "interim" : "listening"
    : projection?.isFinal ? "final" : projection ? "interim" : "empty";

  return (
    <div className="subtitle-stage" data-caption-speech={activeSpeechId || undefined} data-caption-line="single">
      <span className="quote-mark">“</span>
      <p data-caption-projection={captionState} aria-live="polite">{displayText}</p>
      <small>{attribution}</small>
    </div>
  );
}
