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
    expect(editor).toHaveTextContent("已绑定“正方立论”和本次发言记录");
    expect(onPendingChange).toHaveBeenLastCalledWith(true);

    fireEvent.change(textarea, { target: { value: "技术工具只有在所有学生都能公平获得时，才会真正缩小教育差距。" } });
    fireEvent.click(screen.getByRole("button", { name: "提交本轮发言" }));

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "改用文字完成本轮发言" })).not.toBeInTheDocument());
    await waitFor(() => expect(onPendingChange).toHaveBeenLastCalledWith(false));
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

  it("offers an explicit force takeover when another page still owns the seat", async () => {
    const leaseRequests: RequestInit[] = [];
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/control-lease")) {
        leaseRequests.push(init || {});
        const forced = JSON.parse(String(init?.body || "{}"))?.force === true;
        return Promise.resolve(new Response(JSON.stringify(
          forced ? { ok: true, seq: 5 } : { detail: "该席位已由另一设备控制，如需切换请确认接管。" },
        ), { status: forced ? 200 : 409, headers: { "Content-Type": "application/json" } }));
      }
      if (url.endsWith("/speech/start")) {
        return Promise.resolve(new Response(JSON.stringify({ speech_id: "speech-after-takeover" }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      throw new Error(`unexpected request: ${url}`);
    }));

    render(<TextSpeechFallback room={room()} connected />);
    fireEvent.click(screen.getByRole("button", { name: /改用文字发言/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("确认接管");
    fireEvent.click(screen.getByRole("button", { name: "确认接管当前席位" }));

    await waitFor(() => expect(screen.getByLabelText("本轮发言文字")).toBeEnabled());
    expect(leaseRequests).toHaveLength(2);
    expect(JSON.parse(String(leaseRequests[1].body))).toEqual({ force: true });
  });

  it("automatically closes and clears a reserved text turn when the match terminates", async () => {
    const onPendingChange = vi.fn();
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/control-lease")) return Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }));
      if (url.endsWith("/speech/start")) return Promise.resolve(new Response(JSON.stringify({ speech_id: "speech-terminal" }), { status: 200 }));
      throw new Error(`unexpected request: ${url}`);
    }));

    const { rerender } = render(<TextSpeechFallback room={room()} connected onPendingChange={onPendingChange} />);
    fireEvent.click(screen.getByRole("button", { name: /改用文字发言/ }));
    await waitFor(() => expect(screen.getByLabelText("本轮发言文字")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("本轮发言文字"), { target: { value: "这段内容应在终止前明确处理。" } });

    rerender(<TextSpeechFallback room={room({ status: "terminated", can_speak: false, speak_reason: "比赛已终止" })} connected onPendingChange={onPendingChange} />);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(onPendingChange).toHaveBeenLastCalledWith(false);
  });

  it("automatically closes and clears the old draft when the stage changes", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/control-lease")) return Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }));
      if (url.endsWith("/speech/start")) return Promise.resolve(new Response(JSON.stringify({ speech_id: "speech-old-stage" }), { status: 200 }));
      if (url.endsWith("/speech/finish")) throw new Error("stale draft must never be submitted");
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const view = render(<TextSpeechFallback room={room()} connected />);
    fireEvent.click(screen.getByRole("button", { name: /改用文字发言/ }));
    const textarea = await screen.findByLabelText("本轮发言文字");
    await waitFor(() => expect(textarea).toBeEnabled());
    fireEvent.change(textarea, { target: { value: "这是一份只属于正方立论的本地草稿。" } });

    view.rerender(<TextSpeechFallback room={room({
      seq: 8,
      can_speak: false,
      speak_reason: "进入自由辩论",
      current_stage: { key: "free", name: "自由辩论", kind: "free", duration: 300, side: "neg" },
    })} connected />);

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.queryByDisplayValue("这是一份只属于正方立论的本地草稿。")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/speech/finish"))).toBe(false);

    view.rerender(<TextSpeechFallback room={room({ seq: 9 })} connected />);
    expect(screen.getByRole("button", { name: /改用文字发言/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /继续文字发言/ })).not.toBeInTheDocument();
  });

  it("preserves and late-finalizes a timed-out text draft after the stage advances", async () => {
    const onPendingChange = vi.fn();
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/control-lease")) return Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }));
      if (url.endsWith("/speech/start")) return Promise.resolve(new Response(JSON.stringify({ speech_id: "speech-timed-out" }), { status: 200 }));
      if (url.endsWith("/speech/finish")) return Promise.resolve(new Response(JSON.stringify({ speech_id: "speech-timed-out", timed_out: true }), { status: 200 }));
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const view = render(<TextSpeechFallback room={room({
      current_stage: { key: "free", name: "自由辩论", kind: "free", duration: 300, side: "aff" },
    })} connected onPendingChange={onPendingChange} />);
    fireEvent.click(screen.getByRole("button", { name: /改用文字发言/ }));
    const textarea = await screen.findByLabelText("本轮发言文字");
    await waitFor(() => expect(textarea).toBeEnabled());
    fireEvent.change(textarea, { target: { value: "这是截止时仍在本地编辑、必须补写到原发言记录的论点。" } });

    view.rerender(<TextSpeechFallback room={room({
      seq: 12,
      status: "completed",
      can_speak: false,
      speak_reason: "比赛已完成",
      current_stage: { key: "judging", name: "训练点评", kind: "judging", duration: 30 },
      speeches: [{
        id: "speech-timed-out",
        seat_key: "aff_1",
        speaker_type: "human",
        status: "timed_out",
        content: "",
      }],
      recent_events: [{
        seq: 12,
        type: "speech.timed_out",
        payload: { speech_id: "speech-timed-out" },
        created_at: new Date().toISOString(),
      }],
    })} connected onPendingChange={onPendingChange} />);

    expect(screen.getByRole("dialog", { name: "改用文字完成本轮发言" })).toHaveTextContent("已到时");
    expect(screen.getByDisplayValue("这是截止时仍在本地编辑、必须补写到原发言记录的论点。")).toBeInTheDocument();
    expect(onPendingChange).toHaveBeenLastCalledWith(true);
    fireEvent.click(screen.getByRole("button", { name: "补交已超时发言" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(onPendingChange).toHaveBeenLastCalledWith(false));
    const finishRequest = fetchMock.mock.calls.find(([input]) => String(input).endsWith("/speech/finish"));
    expect(JSON.parse(String(finishRequest?.[1]?.body))).toEqual({
      speech_id: "speech-timed-out",
      content: "这是截止时仍在本地编辑、必须补写到原发言记录的论点。",
    });
  });

  it("closes and clears the draft when another speech replaces its bound speech id", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/control-lease")) return Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }));
      if (url.endsWith("/speech/start")) return Promise.resolve(new Response(JSON.stringify({ speech_id: "speech-bound" }), { status: 200 }));
      throw new Error(`unexpected request: ${url}`);
    }));

    const view = render(<TextSpeechFallback room={room()} connected />);
    fireEvent.click(screen.getByRole("button", { name: /改用文字发言/ }));
    await waitFor(() => expect(screen.getByLabelText("本轮发言文字")).toBeEnabled());
    view.rerender(<TextSpeechFallback room={room({
      seq: 6,
      active_speech: { id: "speech-other", seat_key: "aff_1", speaker_type: "human", status: "speaking", content: "" },
      can_speak: false,
    })} connected />);

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.queryByLabelText("本轮发言文字")).not.toBeInTheDocument();
  });

  it("closes the editor when the bound speech completes before the next snapshot arrives", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/control-lease")) return Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }));
      if (url.endsWith("/speech/start")) return Promise.resolve(new Response(JSON.stringify({ speech_id: "speech-completed" }), { status: 200 }));
      throw new Error(`unexpected request: ${url}`);
    }));

    const view = render(<TextSpeechFallback room={room()} connected />);
    fireEvent.click(screen.getByRole("button", { name: /改用文字发言/ }));
    const textarea = await screen.findByLabelText("本轮发言文字");
    await waitFor(() => expect(textarea).toBeEnabled());
    fireEvent.change(textarea, { target: { value: "已经由服务端成功完成的文字发言。" } });

    view.rerender(<TextSpeechFallback room={room({
      seq: 7,
      can_speak: false,
      recent_events: [{
        seq: 7,
        type: "speech.completed",
        payload: { speech_id: "speech-completed" },
        created_at: new Date().toISOString(),
      }],
    })} connected />);

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.queryByText("已经由服务端成功完成的文字发言。")).not.toBeInTheDocument();
  });

  it("does not offer text submission outside the current human turn", () => {
    const { rerender } = render(<TextSpeechFallback room={room({ can_speak: false, speak_reason: "当前轮到反方" })} connected />);
    expect(screen.queryByRole("button", { name: /文字发言/ })).not.toBeInTheDocument();
    rerender(<TextSpeechFallback room={room({ seats: [{ ...room().seats[0], occupant_type: "ai" }] })} connected />);
    expect(screen.queryByRole("button", { name: /文字发言/ })).not.toBeInTheDocument();
  });
});
