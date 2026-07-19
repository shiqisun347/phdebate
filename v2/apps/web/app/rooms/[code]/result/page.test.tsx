import { fireEvent, render, screen } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";

import ResultPage, { type Result } from "@/app/rooms/[code]/result/page";

const roomSync = vi.hoisted(() => ({
  room: { seq: 8 },
  reconnect: vi.fn(),
}));

const navigation = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock("next/navigation", () => ({
  useParams: () => ({ code: "381526" }),
  useRouter: () => navigation,
}));

vi.mock("@/lib/use-room", () => ({
  useRoom: () => ({ room: roomSync.room, error: "", reconnect: roomSync.reconnect }),
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
    roomSync.reconnect.mockReset();
    navigation.push.mockReset();
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
  });

  it("lets a former participant create an idempotent same-topic rematch", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/rooms/381526/result?event_page=1&event_page_size=50")) {
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

  it("loads recordings on demand while showing the saved duration", async () => {
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
    const withAudio = result(null);
    withAudio.speeches = [audioSpeech("speech-1", "有效发言")];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(withAudio), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));
    const { container } = render(<ResultPage />);
    await screen.findByText("有效发言");
    const audio = container.querySelector("audio");
    expect(audio).toHaveAttribute("preload", "none");
    expect(audio).toHaveAttribute("src", expect.stringContaining("/media/381526/speech-1.wav"));
    expect(screen.getByText("录音时长 3.2 秒")).toBeInTheDocument();
  });

  it("does not eagerly initialize media metadata for a long result transcript", async () => {
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
    const withAudio = result(null);
    withAudio.speeches = Array.from({ length: 11 }, (_, index) => (
      audioSpeech(`speech-${index + 1}`, `第 ${index + 1} 条发言`, index % 2 ? "neg" : "aff")
    ));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(withAudio), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));
    const { container } = render(<ResultPage />);
    await screen.findByText("第 11 条发言");

    const audioElements = Array.from(container.querySelectorAll("audio"));
    expect(audioElements).toHaveLength(11);
    audioElements.forEach((audio) => expect(audio).toHaveAttribute("preload", "none"));
    expect(screen.getAllByText(/录音时长 \d+\.\d 秒/)).toHaveLength(11);
  });

  it("pauses every other recording when a result audio starts playing", async () => {
    const withAudio = result(null);
    withAudio.speeches = [
      audioSpeech("speech-1", "正方发言"),
      audioSpeech("speech-2", "反方发言", "neg"),
    ];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(withAudio), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));
    const { container } = render(<ResultPage />);
    await screen.findByText("反方发言");
    const [firstAudio, secondAudio] = Array.from(container.querySelectorAll("audio"));
    const firstPause = vi.spyOn(firstAudio, "pause").mockImplementation(() => undefined);
    const secondPause = vi.spyOn(secondAudio, "pause").mockImplementation(() => undefined);

    fireEvent.play(secondAudio);

    expect(firstPause).toHaveBeenCalledOnce();
    expect(secondPause).not.toHaveBeenCalled();
  });

  it("stops and rewinds every result audio when leaving the page", async () => {
    const withAudio = result(null);
    withAudio.speeches = [
      audioSpeech("speech-1", "正方发言"),
      audioSpeech("speech-2", "反方发言", "neg"),
    ];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(withAudio), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));
    const view = render(<ResultPage />);
    await screen.findByText("反方发言");
    const audioElements = Array.from(view.container.querySelectorAll("audio"));
    const pauseSpies = audioElements.map((audio) => vi.spyOn(audio, "pause").mockImplementation(() => undefined));
    audioElements.forEach((audio, index) => {
      audio.currentTime = index + 1;
    });

    view.unmount();

    pauseSpies.forEach((pause) => expect(pause).toHaveBeenCalledOnce());
    audioElements.forEach((audio) => expect(audio.currentTime).toBe(0));
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
    const pending = result(null);
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
    expect(screen.getByText(/正式赛果尚未生成/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "返回比赛现场" })).toHaveAttribute("href", "/rooms/381526/debate");
    expect(screen.queryByRole("heading", { name: "等待管理员复核" })).not.toBeInTheDocument();
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
    fireEvent.click(await screen.findByRole("button", { name: "加载更早事件" }));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith(
      expect.stringContaining("event_before_seq=5"),
      expect.anything(),
    ));
    expect(await screen.findByText("已加载 3 / 6")).toBeInTheDocument();
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
