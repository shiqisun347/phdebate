import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TextSpeechFallback } from "@/components/text-speech-fallback";
import type { Room } from "@/lib/types";

function room(overrides: Partial<Room> = {}): Room {
  return {
    id: "room-id",
    code: "123456",
    topic: "技术进步是否让教育更加公平？",
    status: "running",
    visibility: "public",
    seq: 4,
    competition: { slug: "training-1v1", name: "1v1 辩论训练赛" } as Room["competition"],
    season: null,
    owner: { real_name: "测试房主" },
    seats: [{
      seat_key: "aff_1",
      side: "aff",
      position: 1,
      label: "正方1辩",
      occupant_type: "human",
      display_name: "测试学生",
      is_ready: true,
      connected: true,
      is_me: true,
    }],
    current_stage: { key: "aff_case", name: "正方立论", kind: "speech", duration: 120, seat: "aff_1" },
    current_stage_index: 1,
    remaining_seconds: 100,
    turn_remaining_seconds: null,
    active_speech: null,
    my_seat: "aff_1",
    can_speak: true,
    speak_reason: "轮到你发言",
    can_control: false,
    recent_events: [],
    speeches: [],
    ...overrides,
  };
}

describe("text speech fallback", () => {
  beforeEach(() => {
    sessionStorage.clear();
    vi.restoreAllMocks();
  });

  it("reserves the current human turn and submits text without uploading audio", async () => {
    const onPendingChange = vi.fn();
    const onRoomChanged = vi.fn();
    const fetchMock = vi.fn((...args: [RequestInfo | URL, RequestInit?]) => {
      const [input] = args;
      const url = String(input);
      if (url.endsWith("/control-lease")) {
        return Promise.resolve(new Response(JSON.stringify({ ok: true, seq: 4 }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      if (url.endsWith("/speech/start")) {
        return Promise.resolve(new Response(JSON.stringify({ speech_id: "speech-text" }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      if (url.endsWith("/speech/finish")) {
        return Promise.resolve(new Response(JSON.stringify({ speech_id: "speech-text" }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<TextSpeechFallback room={room()} connected onPendingChange={onPendingChange} onRoomChanged={onRoomChanged} />);
    fireEvent.click(screen.getByRole("button", { name: /改用文字发言/ }));

    const editor = await screen.findByRole("dialog", { name: "改用文字完成本轮发言" });
    const textarea = await screen.findByLabelText("本轮发言文字");
    await waitFor(() => expect(textarea).toBeEnabled());
    expect(editor).toHaveTextContent("当前轮次已为你保留");
    expect(onPendingChange).toHaveBeenLastCalledWith(true);

    fireEvent.change(textarea, { target: { value: "技术工具只有在所有学生都能公平获得时，才会真正缩小教育差距。" } });
    fireEvent.click(screen.getByRole("button", { name: "提交本轮发言" }));

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "改用文字完成本轮发言" })).not.toBeInTheDocument());
    expect(onPendingChange).toHaveBeenLastCalledWith(false);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/audio"))).toBe(false);
    const finishRequest = fetchMock.mock.calls.find(([input]) => String(input).endsWith("/speech/finish"));
    expect(JSON.parse(String(finishRequest?.[1]?.body))).toEqual({
      speech_id: "speech-text",
      content: "技术工具只有在所有学生都能公平获得时，才会真正缩小教育差距。",
    });
  });

  it("keeps a reserved text turn reopenable after the editor is closed", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => Promise.resolve(new Response(JSON.stringify(
      String(input).endsWith("/speech/start") ? { speech_id: "speech-text" } : { ok: true },
    ), { status: 200, headers: { "Content-Type": "application/json" } }))));

    render(<TextSpeechFallback room={room()} connected />);
    fireEvent.click(screen.getByRole("button", { name: /改用文字发言/ }));
    await waitFor(() => expect(screen.getByLabelText("本轮发言文字")).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "返回语音发言" }));
    expect(screen.getByRole("button", { name: /继续文字发言/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /继续文字发言/ }));
    expect(screen.getByRole("dialog", { name: "改用文字完成本轮发言" })).toBeInTheDocument();
  });

  it("does not offer text submission outside the current human turn", () => {
    const { rerender } = render(<TextSpeechFallback room={room({ can_speak: false, speak_reason: "当前轮到反方" })} connected />);
    expect(screen.queryByRole("button", { name: /文字发言/ })).not.toBeInTheDocument();
    rerender(<TextSpeechFallback room={room({ seats: [{ ...room().seats[0], occupant_type: "ai" }] })} connected />);
    expect(screen.queryByRole("button", { name: /文字发言/ })).not.toBeInTheDocument();
  });
});
