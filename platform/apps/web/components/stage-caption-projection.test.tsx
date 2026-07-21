import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { selectCaptionProjection, StageCaptionProjection } from "@/components/stage-caption-projection";
import type { CaptionSegment, Room } from "@/lib/types";

const baseRoom = {
  seq: 1,
  active_speech: { id: "speech-current", seat_key: "aff_1", speaker_type: "human", status: "speaking", content: "" },
  caption_segments: [],
} as unknown as Room;

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
      speech_id: "speech-old",
      text: "迟到的旧发言字幕",
      is_final: true,
    })).toBeNull();
  });

  it("overrides the frozen subtitle node and renders an explicit empty state without inventing content", () => {
    const { rerender, unmount } = render(<>
      <div className="stage-page"><div className="subtitle-stage"><p>冻结舞台原文</p></div></div>
      <StageCaptionProjection room={baseRoom} liveEvent={null} />
    </>);
    const subtitle = document.querySelector<HTMLElement>(".subtitle-stage p");
    expect(subtitle).toHaveTextContent("当前发言暂时没有逐句字幕");
    expect(subtitle).toHaveAttribute("data-caption-projection", "empty");

    rerender(<>
      <div className="stage-page"><div className="subtitle-stage"><p>冻结舞台更新</p></div></div>
      <StageCaptionProjection room={{ ...baseRoom, seq: 2 }} liveEvent={{ type: "asr", text: "真人临时字幕", is_final: false }} />
    </>);
    expect(subtitle).toHaveTextContent("真人临时字幕");
    expect(subtitle).toHaveAttribute("data-caption-projection", "interim");

    unmount();
    expect(document.querySelector(".subtitle-stage p")).not.toBeInTheDocument();
  });
});
