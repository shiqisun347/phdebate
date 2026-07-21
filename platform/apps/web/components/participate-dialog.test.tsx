import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ParticipateDialog } from "@/components/participate-dialog";
import type { Competition } from "@/lib/types";

const navigation = vi.hoisted(() => ({ push: vi.fn() }));
const sessionState = vi.hoisted(() => ({ user: { id: "user" } as { id: string } | null, loading: false }));

vi.mock("next/navigation", () => ({
  useRouter: () => navigation,
}));

vi.mock("@/lib/use-session", () => ({
  useSession: () => sessionState,
}));

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const training: Competition = {
  id: "training",
  slug: "training-1v1",
  name: "1v1 辩论训练赛",
  tagline: "",
  description: "",
  rules: "",
  format: "1v1",
  seat_count: 2,
  ranked: false,
  allow_custom_topic: true,
  accent: "cyan",
  live_count: 0,
  topics: [{ id: "topic", title: "默认训练辩题" }],
};

describe("participation dialog", () => {
  afterEach(() => {
    navigation.push.mockReset();
    sessionState.user = { id: "user" };
    sessionState.loading = false;
    document.body.style.overflow = "";
    vi.unstubAllGlobals();
  });

  it("normalizes custom-topic feedback and blocks a too-short submission", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ competition: training })));
    render(<ParticipateDialog competition={training} onClose={vi.fn()} />);
    const topic = await screen.findByLabelText("自定义辩题（可选）");
    fireEvent.click(screen.getByRole("radio", { name: "正方1辩" }));
    fireEvent.change(topic, { target: { value: "太短" } });
    expect(screen.getByText("自定义辩题至少需要 4 个字符。")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /创建比赛/ }).at(-1)).toBeDisabled();
    fireEvent.change(topic, { target: { value: "  人工智能 是否提升创造力  " } });
    expect(screen.getByText("12/300")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /创建比赛/ }).at(-1)).toBeEnabled();
  });

  it("requires an explicit seat choice and exposes the selection as a radio group", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ competition: training })));
    render(<ParticipateDialog competition={training} onClose={vi.fn()} />);
    await screen.findByLabelText("自定义辩题（可选）");

    const group = screen.getByRole("group", { name: "选择你的人类辩手席位" });
    const affirmative = screen.getByRole("radio", { name: "正方1辩" });
    const negative = screen.getByRole("radio", { name: "反方1辩" });
    expect(group).toContainElement(affirmative);
    expect(affirmative).not.toBeChecked();
    expect(negative).not.toBeChecked();
    expect(screen.getByRole("button", { name: "请先选择席位" })).toBeDisabled();

    fireEvent.click(negative);
    expect(negative).toBeChecked();
    expect(affirmative).not.toBeChecked();
    expect(screen.getByRole("status")).toHaveTextContent("你将作为：反方1辩");
    expect(screen.getByRole("button", { name: "创建比赛" })).toBeEnabled();
  });

  it("lets a participant choose any managed topic and sends one unambiguous topic source", async () => {
    const detailed = {
      ...training,
      topics: [
        { id: "topic-1", title: "默认训练辩题" },
        { id: "topic-2", title: "第二个训练辩题" },
      ],
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ competition: detailed }))
      .mockResolvedValueOnce(response({ room: { code: "381526" } }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ParticipateDialog competition={detailed} onClose={vi.fn()} />);

    const managedTopic = await screen.findByLabelText("题库辩题");
    fireEvent.change(managedTopic, { target: { value: "topic-2" } });
    fireEvent.click(screen.getByRole("radio", { name: "正方1辩" }));
    fireEvent.click(screen.getAllByRole("button", { name: "创建比赛" }).at(-1)!);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const managedBody = JSON.parse(String(fetchMock.mock.calls[1][1]?.body));
    expect(managedBody).toMatchObject({ topic_id: "topic-2", custom_topic: null });
  });

  it("uses custom content instead of also sending a hidden managed topic", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ competition: training }))
      .mockResolvedValueOnce(response({ room: { code: "381526" } }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ParticipateDialog competition={training} onClose={vi.fn()} />);

    fireEvent.change(await screen.findByLabelText("自定义辩题（可选）"), {
      target: { value: "  AI 是否提升学生的思辨能力  " },
    });
    expect(screen.getByLabelText("题库辩题")).toBeDisabled();
    expect(screen.getByText("已填写自定义辩题，本场将优先使用自定义内容。")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "正方1辩" }));
    fireEvent.click(screen.getAllByRole("button", { name: "创建比赛" }).at(-1)!);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const customBody = JSON.parse(String(fetchMock.mock.calls[1][1]?.body));
    expect(customBody).toMatchObject({ topic_id: null, custom_topic: "AI 是否提升学生的思辨能力" });
  });

  it("shows a load error instead of leaving an unhandled rejection", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ detail: "赛事设置暂时不可用" }, 503)));
    render(<ParticipateDialog competition={training} onClose={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("赛事设置暂时不可用");
  });

  it("explains a closed ranked season before room creation while keeping room search available", async () => {
    const closed: Competition = {
      ...training,
      id: "daily",
      slug: "daily-4v4",
      name: "4v4 人机辩论日常赛",
      ranked: true,
      allow_custom_topic: false,
      seat_count: 8,
      season: {
        id: "closed-season",
        name: "第一赛季",
        slug: "season-1",
        starts_at: new Date(0).toISOString(),
        ends_at: new Date(1).toISOString(),
        is_active: true,
        is_open: false,
        created_at: new Date(0).toISOString(),
        updated_at: new Date(0).toISOString(),
      },
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ competition: closed })));
    render(<ParticipateDialog competition={closed} onClose={vi.fn()} />);
    expect(await screen.findByText(/“第一赛季”已结束/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /创建 4v4 比赛/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /搜索房间/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("六位房间号")).toBeEnabled();
  });

  it.each([
    ["running", "/rooms/381526/debate"],
    ["completed", "/rooms/381526/result"],
    ["lobby", "/rooms/381526/lobby"],
  ])("returns directly to a %s room", async (status, destination) => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ competition: training }))
      .mockResolvedValueOnce(response({ room: { status, my_seat: status === "running" ? "aff_1" : null } }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ParticipateDialog competition={training} onClose={vi.fn()} />);
    await screen.findByLabelText("自定义辩题（可选）");
    fireEvent.click(screen.getByRole("button", { name: /搜索房间/ }));
    fireEvent.change(screen.getByLabelText("六位房间号"), { target: { value: "381526" } });
    fireEvent.click(screen.getByRole("button", { name: "进入房间" }));
    await waitFor(() => expect(navigation.push).toHaveBeenCalledWith(destination));
  });

  it("sends a logged-in non-participant in an active room directly to watch mode", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ competition: training }))
      .mockResolvedValueOnce(response({ room: { status: "paused", my_seat: null } }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ParticipateDialog competition={training} onClose={vi.fn()} />);
    await screen.findByLabelText("自定义辩题（可选）");
    fireEvent.click(screen.getByRole("button", { name: /搜索房间/ }));
    fireEvent.change(screen.getByLabelText("六位房间号"), { target: { value: "381526" } });
    fireEvent.click(screen.getByRole("button", { name: "进入房间" }));
    await waitFor(() => expect(navigation.push).toHaveBeenCalledWith("/rooms/381526/watch"));
  });

  it("does not navigate a cancelled room into a broken watch page", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ competition: training }))
      .mockResolvedValueOnce(response({ room: { status: "cancelled" } }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ParticipateDialog competition={training} onClose={vi.fn()} />);
    await screen.findByLabelText("自定义辩题（可选）");
    fireEvent.click(screen.getByRole("button", { name: /搜索房间/ }));
    fireEvent.change(screen.getByLabelText("六位房间号"), { target: { value: "381526" } });
    fireEvent.click(screen.getByRole("button", { name: "进入房间" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("该房间已取消，请输入其他房间号。");
    expect(navigation.push).not.toHaveBeenCalledWith("/rooms/381526/debate");
  });

  it("preserves an anonymous room code through authentication", async () => {
    sessionState.user = null;
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(response({ competition: training }))
      .mockResolvedValueOnce(response({ room: { status: "lobby", my_seat: null } })));
    render(<ParticipateDialog competition={training} onClose={vi.fn()} />);
    await screen.findByText("先完成登录或注册");
    fireEvent.click(screen.getByRole("button", { name: /搜索房间/ }));
    fireEvent.change(screen.getByLabelText("六位房间号"), { target: { value: "381526" } });
    fireEvent.click(screen.getByRole("button", { name: "登录或注册后进入" }));
    await waitFor(() => expect(navigation.push).toHaveBeenCalledWith("/login?next=%2Frooms%2F381526%2Flobby"));
  });

  it("rejects a nonexistent room before asking an anonymous user to log in", async () => {
    sessionState.user = null;
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(response({ competition: training }))
      .mockResolvedValueOnce(response({ detail: "房间不存在。" }, 404)));
    render(<ParticipateDialog competition={training} onClose={vi.fn()} />);
    await screen.findByText("先完成登录或注册");
    fireEvent.click(screen.getByRole("button", { name: /搜索房间/ }));
    fireEvent.change(screen.getByLabelText("六位房间号"), { target: { value: "000000" } });
    fireEvent.click(screen.getByRole("button", { name: "登录或注册后进入" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("没有找到房间 #000000");
    expect(alert).toHaveTextContent("请核对房间号");
    expect(navigation.push).not.toHaveBeenCalled();
  });

  it("gives a logged-in student an actionable nonexistent-room error", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(response({ competition: training }))
      .mockResolvedValueOnce(response({ detail: "房间不存在。" }, 404)));
    render(<ParticipateDialog competition={training} onClose={vi.fn()} />);
    await screen.findByLabelText("自定义辩题（可选）");
    fireEvent.click(screen.getByRole("button", { name: /搜索房间/ }));
    fireEvent.change(screen.getByLabelText("六位房间号"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "进入房间" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("没有找到房间 #123456");
    expect(alert).toHaveTextContent("向房主确认比赛是否已关闭");
    expect(navigation.push).not.toHaveBeenCalled();
  });

  it("lets an anonymous user watch an active public room without an unnecessary login", async () => {
    sessionState.user = null;
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(response({ competition: training }))
      .mockResolvedValueOnce(response({ room: { status: "running", my_seat: null } })));
    render(<ParticipateDialog competition={training} onClose={vi.fn()} />);
    await screen.findByText("先完成登录或注册");
    fireEvent.click(screen.getByRole("button", { name: /搜索房间/ }));
    fireEvent.change(screen.getByLabelText("六位房间号"), { target: { value: "381526" } });
    fireEvent.click(screen.getByRole("button", { name: "登录或注册后进入" }));
    await waitFor(() => expect(navigation.push).toHaveBeenCalledWith("/rooms/381526/watch"));
  });

  it("moves focus into the room-code field when search mode is selected", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ competition: training })));
    render(<ParticipateDialog competition={training} onClose={vi.fn()} />);
    await screen.findByLabelText("自定义辩题（可选）");
    fireEvent.click(screen.getByRole("button", { name: /搜索房间/ }));
    await waitFor(() => expect(screen.getByLabelText("六位房间号")).toHaveFocus());
  });

  it("returns an anonymous creator to the same competition without losing a draft", async () => {
    sessionState.user = null;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ competition: training })));
    render(<ParticipateDialog competition={training} onClose={vi.fn()} />);
    await screen.findByText("先完成登录或注册");
    fireEvent.click(screen.getByRole("button", { name: "登录或注册后创建" }));
    expect(navigation.push).toHaveBeenCalledWith("/login?next=%2F%3Fparticipate%3Dtraining-1v1");
  });

  it("does not flash topic or seat controls before the anonymous session state is known", async () => {
    sessionState.user = null;
    sessionState.loading = true;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ competition: training })));
    const view = render(<ParticipateDialog competition={training} onClose={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent("正在确认登录状态");
    expect(screen.queryByLabelText("自定义辩题（可选）")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "正方1辩" })).not.toBeInTheDocument();

    sessionState.loading = false;
    view.rerender(<ParticipateDialog competition={training} onClose={vi.fn()} />);
    expect(await screen.findByText("先完成登录或注册")).toBeInTheDocument();
  });

  it("disables creation when a managed competition has no active topic", async () => {
    const noTopics = { ...training, allow_custom_topic: false, topics: [] };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ competition: noTopics })));
    render(<ParticipateDialog competition={noTopics} onClose={vi.fn()} />);
    expect(await screen.findByText("请联系管理员补充该赛事题库。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "暂无可用辩题" })).toBeDisabled();
  });

  it("keeps keyboard focus inside the modal, closes on Escape, and restores focus", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ competition: training })));
    const trigger = document.createElement("button");
    trigger.textContent = "打开参赛窗口";
    document.body.appendChild(trigger);
    trigger.focus();
    const onClose = vi.fn();
    const view = render(<ParticipateDialog competition={training} onClose={onClose} />);
    const close = screen.getByRole("button", { name: "关闭参赛窗口" });
    expect(close).toHaveFocus();
    trigger.focus();
    expect(close).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(trigger).toHaveFocus();
    trigger.remove();
  });

  it("locks background scrolling while open and restores the previous body style", () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ competition: training })));
    document.body.style.overflow = "clip";
    const view = render(<ParticipateDialog competition={training} onClose={vi.fn()} />);
    expect(document.body.style.overflow).toBe("hidden");
    view.unmount();
    expect(document.body.style.overflow).toBe("clip");
  });

  it("has no automatically detectable accessibility violations", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ competition: training })));
    const { container } = render(<ParticipateDialog competition={training} onClose={vi.fn()} />);
    await screen.findByLabelText("自定义辩题（可选）");
    const result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(result.violations).toEqual([]);
  });
});
