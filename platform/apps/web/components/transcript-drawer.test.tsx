import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";

import { TranscriptDrawer } from "@/components/transcript-drawer";
import type { Room, RoomSpeech } from "@/lib/types";

vi.mock("@/components/transcript-collaboration", () => ({
  TranscriptCollaboration: () => <section aria-label="协同文字草稿测试">协同编辑已按需打开</section>,
}));

function speech(id: string, stageKey: string, content: string): RoomSpeech {
  return {
    id,
    seat_key: "aff_1",
    speaker: "张同学",
    stage_key: stageKey,
    content,
    audio_url: "",
    duration_seconds: 10,
    playback_started_at: null,
    playback_ends_at: null,
    status: "completed",
    created_at: `2026-07-21T01:0${id.at(-1)}:00Z`,
  };
}

const room = {
  status: "running",
  my_seat: "aff_1",
  can_control: false,
  current_stage: { key: "aff_case", name: "正方立论", kind: "speech", duration: 120, seat: "aff_1" },
  seats: [{ seat_key: "aff_1", display_name: "张同学" }],
  speeches: [
    { ...speech("speech-1", "aff_case", "当前阶段第一段"), can_request_correction: true },
    speech("speech-2", "neg_case", "其他阶段内容"),
    { ...speech("speech-interrupted", "aff_case", "被中断的非正式内容"), status: "interrupted" },
    { ...speech("speech-failed", "aff_case", "生成失败的非正式内容"), status: "failed" },
  ],
} as unknown as Room;

describe("TranscriptDrawer", () => {
  it("filters by current stage, follows snapshots and never exposes collaborative editing prematurely", async () => {
    const view = render(<TranscriptDrawer room={room} />);
    const trigger = screen.getByRole("button", { name: "文字记录" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(trigger);

    expect(screen.getByRole("dialog", { name: "文字记录" })).toBeInTheDocument();
    expect(screen.getByText("当前阶段第一段")).toBeInTheDocument();
    expect(screen.queryByText("其他阶段内容")).not.toBeInTheDocument();
    expect(screen.queryByText("被中断的非正式内容")).not.toBeInTheDocument();
    expect(screen.queryByText("生成失败的非正式内容")).not.toBeInTheDocument();
    expect(screen.getByText(/本人发言可进入协同编辑后提交修正申请/)).toBeInTheDocument();
    const collaboration = screen.getByRole("button", { name: "协同编辑" });
    expect(collaboration).toBeEnabled();
    fireEvent.click(collaboration);
    expect(screen.getByLabelText("协同文字草稿测试")).toBeInTheDocument();
    const closeCollaboration = screen.getByRole("button", { name: "关闭协同编辑" });
    expect(closeCollaboration).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(closeCollaboration);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();

    view.rerender(<TranscriptDrawer room={{ ...room, speeches: [...room.speeches, speech("speech-3", "aff_case", "实时新增的第二段")] }} />);
    expect(screen.getByText("实时新增的第二段")).toBeInTheDocument();
    expect(screen.getByText("2 条已完成发言")).toBeInTheDocument();
    expect((await axe.run(view.container, { rules: { "color-contrast": { enabled: false } } })).violations).toEqual([]);
  });

  it("shows the active caption separately without counting it as a completed record", () => {
    const activeRoom = {
      ...room,
      active_speech: { id: "active-speech", seat_key: "aff_1", status: "speaking", speaker_type: "human", content: "" },
      caption_segments: [{
        segment_id: "segment-live",
        speech_id: "active-speech",
        seat_key: "aff_1",
        text: "这是尚未完成的实时字幕",
        is_final: false,
        start_ms: 0,
        end_ms: null,
        updated_at: "2026-07-21T01:10:00Z",
        source: "asr",
      }],
    } as Room;
    render(<TranscriptDrawer room={activeRoom} />);
    fireEvent.click(screen.getByRole("button", { name: "文字记录" }));

    expect(screen.getByRole("region", { name: "当前发言实时字幕" })).toHaveTextContent("这是尚未完成的实时字幕");
    expect(screen.getByText("1 条已完成发言")).toBeInTheDocument();
  });

  it("closes with Escape and restores focus to the trigger", async () => {
    render(<TranscriptDrawer room={room} />);
    const trigger = screen.getByRole("button", { name: "文字记录" });
    fireEvent.click(trigger);
    expect(screen.getByRole("button", { name: "关闭文字记录" })).toHaveFocus();

    fireEvent.keyDown(document, { key: "Escape" });

    expect(screen.queryByRole("dialog", { name: "文字记录" })).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("shows a precise empty state when segmented records are unavailable", () => {
    render(<TranscriptDrawer room={{ ...room, speeches: [] }} />);
    fireEvent.click(screen.getByRole("button", { name: "文字记录" }));
    expect(screen.getByText("当前阶段还没有已完成的发言记录")).toBeInTheDocument();
  });

  it("does not expose transcript history or collaboration controls to any spectator", () => {
    const { container } = render(<TranscriptDrawer room={{ ...room, code: "123456", my_seat: null, can_control: false } as Room} />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("button", { name: "文字记录" })).not.toBeInTheDocument();
  });
});
