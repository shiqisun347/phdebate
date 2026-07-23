import { fireEvent, render, screen } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";

import ResultPage, { type Result } from "@/app/rooms/[code]/result/page";

const roomSync = vi.hoisted(() => ({
  room: { seq: 8 },
  error: "",
  options: undefined as { enabled?: boolean } | undefined,
  reconnect: vi.fn(),
}));

const navigation = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  useParams: () => ({ code: "381526" }),
  useRouter: () => navigation,
}));

vi.mock("@/lib/use-room", () => ({
  useRoom: (_code: string, options?: { enabled?: boolean }) => {
    roomSync.options = options;
    return { room: roomSync.room, error: roomSync.error, reconnect: roomSync.reconnect };
  },
}));

function result(mySeat: string | null): Result {
  return {
    room: {
      id: "room-id",
      code: "381526",
      topic: "人工智能时代，还要不要学编程？",
      status: "completed",
      visibility: "public",
      seq: 8,
      competition: {
        id: "competition-id",
        slug: "training-1v1",
        name: "1v1 辩论训练赛",
        tagline: "",
        description: "",
        rules: "",
        format: "1v1",
        seat_count: 2,
        ranked: false,
        allow_custom_topic: true,
        accent: "blue",
        live_count: 0,
      },
      season: null,
      owner: { real_name: "测试房主" },
      seats: [],
      current_stage: null,
      current_stage_index: 3,
      remaining_seconds: 0,
      turn_remaining_seconds: 0,
      active_speech: null,
      my_seat: mySeat,
      can_speak: false,
      speak_reason: "比赛已结束",
      can_control: false,
      can_view_transcript: Boolean(mySeat),
      recent_events: [],
      speeches: [],
    },
    match: { id: "match-id", status: "completed", winner: "aff" as string | null, reason: "正方论证更完整。" },
    scorecard: {
      status: "approved",
      winner: "aff" as string | null,
      affirmative_score: 88 as number | null,
      negative_score: 84 as number | null,
      individual_scores: {},
      reasoning: "正方论证更完整。",
    },
    speeches: [],
    speech_pagination: { page: 1, page_size: 50, total: 0, pages: 1, next_cursor: null, has_more: false },
    rating_changes: [],
    events: [],
    event_pagination: { page: 1, page_size: 50, total: 0, pages: 1, next_before_seq: null, has_more: false },
  };
}

function audioSpeech(
  id: string,
  content: string,
  side: "aff" | "neg" = "aff",
): Result["speeches"][number] {
  return {
    id,
    seat_key: `${side}_1`,
    stage_key: `${side}_case`,
    stage_name: side === "aff" ? "正方立论" : "反方立论",
    speaker_type: side === "aff" ? "human" : "ai",
    status: "completed",
    content,
    audio_url: `/media/381526/${id}.wav`,
    duration_seconds: side === "aff" ? 3.2 : 4.1,
    created_at: new Date().toISOString(),
  };
}

describe("result archive download", () => {
  afterEach(() => {
    roomSync.room = { seq: 8 };
    roomSync.error = "";
    roomSync.options = undefined;
    roomSync.reconnect.mockReset();
    navigation.push.mockReset();
    navigation.replace.mockReset();
    vi.unstubAllGlobals();
  });

  it("offers the authenticated participant a complete archive download", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(result("aff_1")), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));
    render(<ResultPage />);
    const download = await screen.findByRole("link", { name: "下载完整归档" });
    expect(download).toHaveAttribute("href", expect.stringContaining("/api/matches/match-id/archive"));
    expect(download).toHaveAttribute("download");
  });

  it("does not expose the private archive action to an anonymous watcher", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(result(null)), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));
    render(<ResultPage />);
    expect(await screen.findByRole("heading", { name: "正方胜利" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "下载完整归档" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "同题再来一场" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "返回个人中心" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "完整辩论文字记录" })).not.toBeInTheDocument();
    expect(screen.getByText("为保护参赛者数据，观众不能查看赛中字幕和赛后发言文字。")).toBeInTheDocument();
    expect(document.querySelector("audio")).not.toBeInTheDocument();
    expect(roomSync.options).toEqual({ enabled: false });
  });

  it("does not ask users to reconnect after a final result is already readable", async () => {
    roomSync.error = "实时连接已断开，正在重新连接…";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(result(null)), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));

    render(<ResultPage />);

    expect(await screen.findByRole("heading", { name: "正方胜利" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "立即重连" })).not.toBeInTheDocument();
    expect(screen.queryByText(/恢复连接后会自动同步/)).not.toBeInTheDocument();
    expect(roomSync.options).toEqual({ enabled: false });
  });

  it("keeps realtime recovery available while the result page is only a live record", async () => {
    const live = result(null);
    live.room.status = "running";
    live.match.status = "running";
    live.match.winner = null;
    live.scorecard = null;
    roomSync.error = "实时连接已断开，正在重新连接…";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(live), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));

    render(<ResultPage />);

    expect(await screen.findByRole("button", { name: "立即重连" })).toBeInTheDocument();
    expect(roomSync.options).toEqual({ enabled: true });
  });

  it("offers correction controls only on the participant's own eligible human speech", async () => {
    const participantResult = result("aff_1");
    participantResult.speeches = [
      { ...audioSpeech("speech-human", "本人的真人发言"), audio_url: "", can_request_correction: true },
      { ...audioSpeech("speech-ai", "AI 发言", "neg"), audio_url: "", can_request_correction: false },
    ];
    participantResult.speech_pagination = { page: 1, page_size: 50, total: 2, pages: 1, next_cursor: null, has_more: false };
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/speech-correction-requests")) {
        return Promise.resolve(new Response(JSON.stringify({ items: [] }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      return Promise.resolve(new Response(JSON.stringify(participantResult), { status: 200, headers: { "Content-Type": "application/json" } }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ResultPage />);

    expect(await screen.findByText("本人的真人发言")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "申请修正发言文字" })).toHaveLength(1);
    expect(screen.getByText("AI 发言").closest("article")).not.toHaveTextContent("申请修正发言文字");
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/rooms/381526/speech-correction-requests"),
      expect.anything(),
    ));
  });

  it("returns an anonymous watcher to the public projection while a result is not final", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "比赛尚未结束，请前往观战页面。" }), {
      status: 409,
      headers: { "Content-Type": "application/json" },
    })));

    render(<ResultPage />);

    await vi.waitFor(() => expect(navigation.replace).toHaveBeenCalledWith("/rooms/381526/watch"));
    expect(screen.queryByRole("heading", { name: "比赛已暂停" })).not.toBeInTheDocument();
  });

  it("lets a former participant create an idempotent same-topic rematch", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/rooms/381526/result?speech_page=1&speech_page_size=50&event_page=1&event_page_size=50")) {
        return Promise.resolve(new Response(JSON.stringify(result("aff_1")), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }));
      }
      if (url.endsWith("/api/rooms/381526/rematch") && init?.method === "POST") {
        return Promise.resolve(new Response(JSON.stringify({ room: { ...result("aff_1").room, code: "654321", status: "lobby" } }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }));
      }
      return Promise.resolve(new Response(JSON.stringify({ detail: "unexpected request" }), { status: 500 }));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ResultPage />);

    fireEvent.click(await screen.findByRole("button", { name: "同题再来一场" }));

    await vi.waitFor(() => expect(navigation.push).toHaveBeenCalledWith("/rooms/654321/lobby"));
    const rematchRequest = fetchMock.mock.calls.find(([input]) => String(input).endsWith("/api/rooms/381526/rematch"));
    expect(rematchRequest?.[1]).toMatchObject({ method: "POST", body: "{}" });
    expect(new Headers(rematchRequest?.[1]?.headers).get("X-Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/i);
  });

  it("never renders archived audio and keeps watcher transcripts private", async () => {
    const withAudio = result(null);
    withAudio.speeches = [audioSpeech("speech-1", "有效发言")];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(withAudio), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));
    const { container } = render(<ResultPage />);
    expect(await screen.findByText("为保护参赛者数据，观众不能查看赛中字幕和赛后发言文字。")).toBeInTheDocument();
    expect(screen.queryByText("有效发言")).not.toBeInTheDocument();
    expect(container.querySelector("audio")).not.toBeInTheDocument();
    expect(screen.queryByText(/录音时长/)).not.toBeInTheDocument();
  });

  it("shows participant text without rendering an audio player even for legacy audio_url data", async () => {
    const withAudio = result("aff_1");
    withAudio.speeches = [
      audioSpeech("speech-1", "正方发言"),
      audioSpeech("speech-2", "反方发言", "neg"),
    ];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(withAudio), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));
    const { container } = render(<ResultPage />);
    expect(await screen.findByRole("list", { name: "完整辩论文字记录" })).toBeInTheDocument();
    await screen.findByText("反方发言");
    expect(screen.getByText("正方发言")).toBeInTheDocument();
    expect(container.querySelector("audio")).not.toBeInTheDocument();
    expect(screen.queryByText(/录音时长/)).not.toBeInTheDocument();
  });

  it("has no automatically detectable accessibility violations", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(result(null)), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));
    const { container } = render(<ResultPage />);
    await screen.findByRole("heading", { name: "正方胜利" });
    const audit = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(audit.violations).toEqual([]);
  });

  it("does not render unpublished scores and reasoning as a final result", async () => {
    const pending = result("aff_1");
    pending.match.status = "review_required";
    pending.match.winner = null;
    pending.scorecard = {
      status: "review_required",
      winner: null,
      affirmative_score: null,
      negative_score: null,
      individual_scores: {},
      reasoning: "裁判结果等待管理员复核。",
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(pending), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));
    render(<ResultPage />);
    expect(await screen.findByRole("heading", { name: "等待管理员复核" })).toBeInTheDocument();
    expect(screen.queryByText(/正方\s*VS\s*反方/)).not.toBeInTheDocument();
    expect(screen.getByText("裁判结果等待管理员复核。")).toBeInTheDocument();
  });

  it("explains an approved draw when the displayed team scores differ", async () => {
    const drawn = result(null);
    drawn.match.winner = "draw";
    drawn.scorecard!.winner = "draw";
    drawn.scorecard!.affirmative_score = 86;
    drawn.scorecard!.negative_score = 87;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(drawn), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));

    render(<ResultPage />);

    expect(await screen.findByRole("heading", { name: "双方战平" })).toBeInTheDocument();
    expect(screen.getByText(/未认定差距足以形成明确胜负/)).toBeInTheDocument();
  });

  it("labels an in-progress record as live instead of incorrectly claiming review is required", async () => {
    const active = result("aff_1");
    active.room.status = "running";
    active.match.status = "running";
    active.match.winner = null;
    active.match.reason = "";
    active.scorecard = null;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(active), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));
    render(<ResultPage />);
    expect(await screen.findByRole("heading", { name: "比赛正在进行" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "当前比赛状态" })).toBeInTheDocument();
    expect(screen.getByText("比赛尚未结束")).toBeInTheDocument();
    expect(screen.getByText(/正式赛果尚未生成/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "返回比赛现场" })).toHaveAttribute("href", "/rooms/381526/debate");
    expect(screen.queryByRole("heading", { name: "等待管理员复核" })).not.toBeInTheDocument();
  });

  it("describes a participant's paused record as recoverable rather than under review", async () => {
    const paused = result("aff_1");
    paused.room.status = "paused";
    paused.match.status = "running";
    paused.match.winner = null;
    paused.scorecard = null;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(paused), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));
    render(<ResultPage />);

    expect(await screen.findByRole("heading", { name: "比赛已暂停" })).toBeInTheDocument();
    expect(screen.getByText(/房主或管理员恢复/)).toBeInTheDocument();
    expect(screen.getByText("比赛尚未结束")).toBeInTheDocument();
    expect(screen.queryByText("裁判评议中")).not.toBeInTheDocument();
    expect(screen.queryByText("等待管理员复核")).not.toBeInTheDocument();
  });

  it("reloads the result when a newer authoritative room sequence arrives", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(result(null)), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    const view = render(<ResultPage />);
    await screen.findByRole("heading", { name: "正方胜利" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    roomSync.room = { seq: 9 };
    view.rerender(<ResultPage />);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("loads older timeline events with a stable sequence cursor and removes duplicates", async () => {
    const first = result(null);
    first.events = [
      { seq: 5, type: "stage.advanced", payload: {}, created_at: "2026-07-16T01:00:00Z" },
      { seq: 6, type: "match.completed", payload: {}, created_at: "2026-07-16T01:01:00Z" },
    ];
    first.event_pagination = { page: 1, page_size: 2, total: 6, pages: 3, next_before_seq: 5, has_more: true };
    const older = result(null);
    older.events = [
      { seq: 4, type: "speech.completed", payload: {}, created_at: "2026-07-16T00:59:00Z" },
      { seq: 5, type: "stage.advanced", payload: {}, created_at: "2026-07-16T01:00:00Z" },
    ];
    older.event_pagination = { page: 2, page_size: 2, total: 6, pages: 3, next_before_seq: 4, has_more: true };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(first), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(older), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ResultPage />);
    expect(await screen.findByRole("list", { name: "比赛时间线" })).toHaveClass("result-timeline-list");
    fireEvent.click(await screen.findByRole("button", { name: "加载更早事件" }));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith(
      expect.stringContaining("event_before_seq=5"),
      expect.anything(),
    ));
    expect(await screen.findByText("已加载 3 / 6")).toBeInTheDocument();
  });

  it("uses the authoritative speech total and loads earlier speeches in stable chronological order", async () => {
    const first = result("aff_1");
    const speech3 = { ...audioSpeech("speech-3", "第三段发言"), audio_url: "", created_at: "2026-07-16T01:02:00Z" };
    const speech4 = { ...audioSpeech("speech-4", "第四段发言", "neg"), audio_url: "", created_at: "2026-07-16T01:03:00Z" };
    first.speeches = [speech3, speech4];
    first.speech_pagination = { page: 1, page_size: 2, total: 4, pages: 2, next_cursor: "signed-cursor", has_more: true };
    const older = result("aff_1");
    older.speeches = [
      { ...audioSpeech("speech-1", "第一段发言"), audio_url: "", created_at: "2026-07-16T01:00:00Z" },
      { ...audioSpeech("speech-2", "第二段发言", "neg"), audio_url: "", created_at: "2026-07-16T01:01:00Z" },
      speech3,
    ];
    older.speech_pagination = { page: 2, page_size: 2, total: 4, pages: 2, next_cursor: null, has_more: false };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(first), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(older), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const { container } = render(<ResultPage />);

    expect(await screen.findByText("已加载 2 / 4")).toBeInTheDocument();
    expect(screen.getByText("发言数").closest(".lobby-rule")).toHaveTextContent("4");
    fireEvent.click(screen.getByRole("button", { name: "加载更早发言" }));

    await screen.findByText("已加载 4 / 4");
    expect(fetchMock).toHaveBeenLastCalledWith(
      expect.stringContaining("speech_cursor=signed-cursor"),
      expect.anything(),
    );
    expect(screen.queryByRole("button", { name: "加载更早发言" })).not.toBeInTheDocument();
    const transcriptText = Array.from(container.querySelectorAll(".transcript-item")).map((item) => item.textContent);
    expect(transcriptText).toEqual([
      expect.stringContaining("第一段发言"),
      expect.stringContaining("第二段发言"),
      expect.stringContaining("第三段发言"),
      expect.stringContaining("第四段发言"),
    ]);
  });

  it("preserves already loaded speech pages when refreshing a live result", async () => {
    const speech = (id: number) => ({
      ...audioSpeech(`speech-${id}`, `第 ${id} 段发言`, id % 2 ? "aff" : "neg"),
      audio_url: "",
      created_at: `2026-07-16T01:0${id}:00Z`,
    });
    const first = result("aff_1");
    first.speeches = [speech(3), speech(4)];
    first.speech_pagination = { page: 1, page_size: 2, total: 4, pages: 2, next_cursor: "cursor-initial", has_more: true };
    const older = result("aff_1");
    older.speeches = [speech(1), speech(2)];
    older.speech_pagination = { page: 2, page_size: 2, total: 4, pages: 2, next_cursor: null, has_more: false };
    const refreshed = result("aff_1");
    refreshed.speeches = [speech(4), speech(5)];
    refreshed.speech_pagination = { page: 1, page_size: 2, total: 5, pages: 3, next_cursor: "cursor-refresh-1", has_more: true };
    const refreshedMiddle = result("aff_1");
    refreshedMiddle.speeches = [speech(2), speech(3)];
    refreshedMiddle.speech_pagination = { page: 2, page_size: 2, total: 5, pages: 3, next_cursor: "cursor-refresh-2", has_more: true };
    const refreshedOldest = result("aff_1");
    refreshedOldest.speeches = [speech(1)];
    refreshedOldest.speech_pagination = { page: 3, page_size: 2, total: 5, pages: 3, next_cursor: null, has_more: false };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(first), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(older), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(refreshed), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(refreshedMiddle), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(refreshedOldest), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ResultPage />);

    fireEvent.click(await screen.findByRole("button", { name: "加载更早发言" }));
    await screen.findByText("已加载 4 / 4");
    fireEvent.click(screen.getByRole("button", { name: "刷新赛果" }));

    await screen.findByText("已加载 5 / 5");
    for (const id of [1, 2, 3, 4, 5])
      expect(screen.getByText(`第 ${id} 段发言`)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(5);
    expect(roomSync.reconnect).toHaveBeenCalledOnce();
  });

  it("falls back to page pagination when a cursor fetch fails before reaching the server", async () => {
    const first = result(null);
    first.events = [{ seq: 5, type: "stage.advanced", payload: {}, created_at: "2026-07-16T01:00:00Z" }];
    first.event_pagination = { page: 1, page_size: 1, total: 2, pages: 2, next_before_seq: 5, has_more: true };
    const older = result(null);
    older.events = [{ seq: 4, type: "speech.completed", payload: {}, created_at: "2026-07-16T00:59:00Z" }];
    older.event_pagination = { page: 2, page_size: 1, total: 2, pages: 2, next_before_seq: 4, has_more: false };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(first), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(new Response(JSON.stringify(older), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ResultPage />);
    fireEvent.click(await screen.findByRole("button", { name: "加载更早事件" }));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(fetchMock.mock.calls[1][0]).toContain("event_before_seq=5");
    expect(fetchMock.mock.calls[2][0]).toContain("event_page=2&event_page_size=1");
    expect(await screen.findByText("已加载 2 / 2")).toBeInTheDocument();
    expect(screen.queryByText("Failed to fetch")).not.toBeInTheDocument();
  });

  it("shows all room seats and marks missing approved individual scores", async () => {
    const scored = result(null);
    scored.room.seats = [
      { seat_key: "aff_1", side: "aff", position: 1, label: "正方一辩", occupant_type: "human", display_name: "甲", is_ready: true, connected: false, is_me: false },
      { seat_key: "aff_2", side: "aff", position: 2, label: "正方二辩", occupant_type: "ai", display_name: "乙", is_ready: true, connected: false, is_me: false },
      { seat_key: "aff_3", side: "aff", position: 3, label: "正方三辩", occupant_type: "ai", display_name: "丙", is_ready: true, connected: false, is_me: false },
      { seat_key: "aff_4", side: "aff", position: 4, label: "正方四辩", occupant_type: "ai", display_name: "丁", is_ready: true, connected: false, is_me: false },
      { seat_key: "neg_1", side: "neg", position: 1, label: "反方一辩", occupant_type: "ai", display_name: "戊", is_ready: true, connected: false, is_me: false },
      { seat_key: "neg_2", side: "neg", position: 2, label: "反方二辩", occupant_type: "ai", display_name: "己", is_ready: true, connected: false, is_me: false },
      { seat_key: "neg_3", side: "neg", position: 3, label: "反方三辩", occupant_type: "ai", display_name: "庚", is_ready: true, connected: false, is_me: false },
      { seat_key: "neg_4", side: "neg", position: 4, label: "反方四辩", occupant_type: "ai", display_name: "辛", is_ready: true, connected: false, is_me: false },
    ];
    scored.scorecard!.individual_scores = { aff_1: 88, aff_3: 85, aff_4: 87, neg_1: 80, neg_2: 79, neg_3: 78, neg_4: 77 };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(scored), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));
    render(<ResultPage />);
    expect(await screen.findByRole("heading", { name: "个人评分" })).toBeInTheDocument();
    expect(screen.getByText("正方二辩").closest(".history-row")).toHaveTextContent("暂无评分");
    expect(screen.getByText("正方一辩").closest(".history-row")).toHaveTextContent("88");
    expect(screen.getAllByText(/正方|反方/).length).toBeGreaterThanOrEqual(8);
  });
});
