import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { selectCaptionProjection, StageSubtitle } from "@/components/stage-caption-projection";
import type { CaptionSegment, Room } from "@/lib/types";

const baseRoom = {
  seq: 1,
  status: "running",
  active_speech: { id: "speech-current", seat_key: "aff_1", speaker_type: "human", status: "speaking", content: "" },
  caption_segments: [],
} as unknown as Room;

function renderSubtitle(room: Room, liveEvent: Record<string, unknown> | null = null) {
  return <StageSubtitle
    room={room}
    liveEvent={liveEvent}
    capturing={false}
    localCaption=""
    aiPreparing={false}
    idleText="等待下一位辩手"
    attribution="正方一辩 · 实时字幕"
  />;
}

function segment(overrides: Partial<CaptionSegment>): CaptionSegment {
  return {
    segment_id: "segment-1",
    speech_id: "speech-current",
    seat_key: "aff_1",
    text: "当前字幕",
    is_final: false,
    start_ms: 0,
    end_ms: 1000,
    updated_at: "2026-07-21T01:00:00Z",
    source: "asr",
    ...overrides,
  };
}

describe("StageCaptionProjection", () => {
  it("deduplicates by segment id, prefers the newest revision and ignores another speech", () => {
    const room = {
      ...baseRoom,
      caption_segments: [
        segment({ text: "旧临时字幕", updated_at: "2026-07-21T01:00:00Z" }),
        segment({ text: "最终字幕", is_final: true, updated_at: "2026-07-21T01:00:01Z" }),
        segment({ segment_id: "other", speech_id: "speech-old", text: "其他发言字幕", updated_at: "2026-07-21T01:00:02Z" }),
      ],
    } as Room;

    expect(selectCaptionProjection(room, null)).toMatchObject({
      text: "最终字幕",
      isFinal: true,
      segmentId: "segment-1",
    });
  });

  it("uses the latest matching live segment for both ASR and AI-compatible events", () => {
    expect(selectCaptionProjection(baseRoom, {
      type: "caption.segment",
      segment_id: "live-ai-1",
      speech_id: "speech-current",
      text: "AI 实时逐句字幕",
      is_final: false,
      timestamp_ms: 3000,
    })).toEqual({ text: "AI 实时逐句字幕", isFinal: false, segmentId: "live-ai-1", updatedAt: 3000 });

    expect(selectCaptionProjection(baseRoom, {
      type: "asr",
      speech_id: "speech-current",
      text: "第一句已经确认。第二句正在识别，而且不会把整段文字铺满舞台",
      is_final: false,
    })).toMatchObject({ text: "而且不会把整段文字铺满舞台" });

    expect(selectCaptionProjection(baseRoom, {
      type: "asr",
      speech_id: "speech-old",
      text: "迟到的旧发言字幕",
      is_final: true,
    })).toBeNull();

    expect(selectCaptionProjection(baseRoom, {
      type: "asr",
      text: "缺少发言标识的迟到字幕",
      is_final: false,
    })).toBeNull();
  });

  it("advances stable WAV captions against playback time and clears them while paused", () => {
    const playbackStartedAt = "2026-07-21T01:00:00Z";
    const room = {
      ...baseRoom,
      active_speech: {
        ...baseRoom.active_speech,
        speaker_type: "ai",
        status: "playing",
        content: "这一整篇固定稿件绝不能一次显示。",
        playback_started_at: playbackStartedAt,
        stream_generation: "generation-a",
      },
      caption_segments: [
        segment({ segment_id: "audio-1", text: "第一句。", source: "agent", timing_basis: "audio_duration", start_ms: 0 }),
        segment({ segment_id: "audio-2", text: "第二句。", source: "agent", timing_basis: "audio_duration", start_ms: 2000 }),
        segment({ segment_id: "audio-3", text: "第三句。", source: "agent", timing_basis: "audio_duration", start_ms: 5000 }),
      ],
    } as Room;
    const started = Date.parse(playbackStartedAt);

    expect(selectCaptionProjection(room, null, started + 1000)?.text).toBe("第一句。");
    expect(selectCaptionProjection(room, null, started + 2500)?.text).toBe("第二句。");
    expect(selectCaptionProjection(room, null, started + 7000)?.text).toBe("第三句。");
    expect(selectCaptionProjection({ ...room, status: "paused" } as Room, null, started + 7000)).toBeNull();
    expect(selectCaptionProjection({ ...room, active_speech: null } as Room, null, started + 7000)).toBeNull();
  });

  it("paces prefetched streaming captions instead of flashing the final clause", () => {
    const playbackStartedAt = "2026-07-21T01:00:00Z";
    const room = {
      ...baseRoom,
      active_speech: {
        ...baseRoom.active_speech,
        speaker_type: "ai",
        status: "synthesizing",
        playback_started_at: playbackStartedAt,
      },
      caption_segments: [
        segment({ segment_id: "prefetch-1", text: "第一句。", source: "agent", timing_basis: "estimated_playback", start_ms: 0 }),
        segment({ segment_id: "prefetch-2", text: "第二句。", source: "agent", timing_basis: "estimated_playback", start_ms: 1800 }),
        segment({ segment_id: "prefetch-3", text: "最后一句。", source: "agent", timing_basis: "estimated_playback", start_ms: 3600 }),
      ],
    } as Room;
    const started = Date.parse(playbackStartedAt);

    expect(selectCaptionProjection(room, null, started + 500)?.text).toBe("第一句。");
    expect(selectCaptionProjection(room, null, started + 2200)?.text).toBe("第二句。");
    expect(selectCaptionProjection(room, null, started + 2200)?.text).not.toBe("最后一句。");
    expect(selectCaptionProjection(room, null, started + 4200)?.text).toBe("最后一句。");
    expect(selectCaptionProjection(room, {
      type: "caption.segment",
      speech_id: "speech-current",
      segment_id: "prefetch-3",
      text: "最后一句。",
      is_final: true,
      timing_basis: "estimated_playback",
      start_ms: 3600,
    }, started + 500)?.text).toBe("第一句。");
  });

  it("never flashes the complete AI answer before the first timed clause is audible", () => {
    const futureStart = new Date(Date.now() + 5000).toISOString();
    const room = {
      ...baseRoom,
      active_speech: {
        ...baseRoom.active_speech,
        speaker_type: "ai",
        status: "playing",
        content: "不可提前暴露的完整 AI 发言稿。",
        playback_started_at: futureStart,
      },
      caption_segments: [
        segment({ text: "第一句。", source: "agent", timing_basis: "audio_duration", start_ms: 0 }),
      ],
    } as Room;
    render(renderSubtitle(room));

    const subtitle = document.querySelector<HTMLElement>(".subtitle-stage p");
    expect(subtitle).toHaveTextContent("当前发言暂时没有逐句字幕");
    expect(subtitle).not.toHaveTextContent("不可提前暴露的完整 AI 发言稿");
  });

  it("clears an old clause across pause and playback-generation changes", () => {
    const startedAt = new Date(Date.now() - 1000).toISOString();
    const playing = {
      ...baseRoom,
      active_speech: {
        ...baseRoom.active_speech,
        speaker_type: "ai",
        status: "playing",
        playback_started_at: startedAt,
        stream_generation: "generation-old",
      },
      caption_segments: [
        segment({ text: "旧代次字幕。", source: "agent", timing_basis: "audio_duration", start_ms: 0 }),
      ],
    } as Room;
    const { rerender } = render(renderSubtitle(playing));
    const subtitle = document.querySelector<HTMLElement>(".subtitle-stage p");
    expect(subtitle).toHaveTextContent("旧代次字幕。");

    rerender(renderSubtitle({ ...playing, status: "paused" } as Room));
    expect(subtitle).toHaveTextContent("等待下一位辩手");
    expect(subtitle).not.toHaveTextContent("旧代次字幕。");

    rerender(renderSubtitle({
      ...playing,
      active_speech: {
        ...playing.active_speech!,
        playback_started_at: new Date(Date.now() + 5000).toISOString(),
        stream_generation: "generation-new",
      },
    } as Room));
    expect(subtitle).toHaveTextContent("当前发言暂时没有逐句字幕");
    expect(subtitle).not.toHaveTextContent("旧代次字幕。");
  });

  it("renders one authoritative subtitle node and an explicit empty state without inventing content", () => {
    const { rerender, unmount } = render(renderSubtitle(baseRoom));
    const subtitle = document.querySelector<HTMLElement>(".subtitle-stage p");
    expect(subtitle).toHaveTextContent("当前发言暂时没有逐句字幕");
    expect(subtitle).toHaveAttribute("data-caption-projection", "empty");
    expect(subtitle).toHaveAttribute("aria-live", "polite");
    expect(document.querySelector(".subtitle-stage")).toHaveAttribute("data-caption-line", "single");

    rerender(renderSubtitle(
      { ...baseRoom, seq: 2 } as Room,
      { type: "asr", speech_id: "speech-current", text: "真人临时字幕", is_final: false },
    ));
    expect(subtitle).toHaveTextContent("真人临时字幕");
    expect(subtitle).toHaveAttribute("data-caption-projection", "interim");

    unmount();
    expect(document.querySelector(".subtitle-stage p")).not.toBeInTheDocument();
  });

  it("keeps a cumulative local ASR hypothesis to one bounded television-style line", () => {
    render(<StageSubtitle
      room={baseRoom}
      liveEvent={null}
      capturing
      localCaption="第一句已经确认。第二句仍在持续识别，而且舞台只保留当前短句"
      aiPreparing={false}
      idleText="等待下一位辩手"
      attribution="正方一辩 · 实时字幕"
    />);

    const line = document.querySelector<HTMLElement>(".subtitle-stage p");
    expect(line).toHaveTextContent("而且舞台只保留当前短句");
    expect(line).not.toHaveTextContent("第一句已经确认");
    expect(line).toHaveAttribute("data-caption-projection", "interim");
  });

  it("shows the pause state instead of a stale stage cue", () => {
    render(<StageSubtitle
      room={{
        ...baseRoom,
        status: "paused",
        current_stage: { key: "fixed", name: "正方立论", cue: "上一阶段提示音" },
      } as Room}
      liveEvent={null}
      capturing={false}
      localCaption=""
      aiPreparing={false}
      idleText="比赛已暂停，恢复后将从当前进度继续。"
      attribution="正方一辩"
    />);

    expect(document.querySelector(".subtitle-stage p")).toHaveTextContent("比赛已暂停");
    expect(document.querySelector(".subtitle-stage p")).not.toHaveTextContent("上一阶段提示音");
  });
});
