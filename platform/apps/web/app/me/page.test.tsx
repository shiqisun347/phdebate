import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";

import MePage from "@/app/me/page";

const navigation = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => navigation,
}));

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const meData = {
  user: { id: "user-1", account: "real_user", real_name: "真实选手", role: "user", is_active: true },
  summary: { history_total: 0, active_total: 0, total_points: 0 },
  pagination: { page: 1, page_size: 20, total: 0, pages: 1 },
  active_rooms: [],
  history: [],
  rating_changes: [],
};

describe("personal account", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    navigation.push.mockReset();
  });

  it("changes the password and can revoke other devices", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/me?page=1&page_size=20")) return Promise.resolve(response(meData));
      if (url.endsWith("/api/auth/password") && init?.method === "POST") {
        return Promise.resolve(response({ ok: true, other_sessions_revoked: true }));
      }
      if (url.endsWith("/api/auth/sessions/revoke-others") && init?.method === "POST") {
        return Promise.resolve(response({ ok: true, revoked: 2 }));
      }
      return Promise.resolve(response({ detail: "unexpected request" }, 500));
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("confirm", vi.fn(() => true));

    render(<MePage />);
    expect(await screen.findByRole("heading", { name: "账号安全" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("当前密码"), { target: { value: "Password-1234" } });
    fireEvent.change(screen.getByLabelText("新密码"), { target: { value: "New-password-5678" } });
    fireEvent.change(screen.getByLabelText("确认新密码"), { target: { value: "New-password-5678" } });
    fireEvent.click(screen.getByRole("button", { name: /更新密码/ }));
    expect(await screen.findByText("密码已更新，其他设备的登录会话已全部撤销。")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /退出其他设备/ }));
    expect(await screen.findByText("已退出 2 个其他设备。")).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("describes a lobby without repeating the room status as its detail", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      ...meData,
      summary: { ...meData.summary, active_total: 1 },
      active_rooms: [{
        code: "250649",
        topic: "大厅状态说明测试",
        status: "lobby",
        seat_key: "aff_1",
        occupant_type: "human",
        can_resume: true,
      }],
    })));
    render(<MePage />);

    expect(await screen.findByText("等待房主锁定席位并开始比赛")).toBeInTheDocument();
    expect(screen.getByText("返回房间大厅")).toBeInTheDocument();
    expect(screen.getAllByText("房间大厅", { exact: true })).toHaveLength(1);
    expect(screen.getByRole("link", { name: "浏览更多赛事" })).toHaveAttribute("href", "/#competitions");
    expect(screen.queryByRole("link", { name: "参加第一场比赛" })).not.toBeInTheDocument();
  });

  it("keeps the personal center focused on competitions and account security", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(meData));
    vi.stubGlobal("fetch", fetchMock);
    const { container } = render(<MePage />);

    expect(await screen.findByRole("heading", { name: "继续比赛" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "最近积分" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "历史比赛" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "账号安全" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "选择赛事" })).toHaveAttribute("href", "/#competitions");
    expect(screen.getByRole("link", { name: "参加第一场比赛" })).toHaveAttribute("href", "/#competitions");
    expect(screen.queryByText(/课堂|教师|教学活动/)).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/me?page=1&page_size=20");
    expect((await axe.run(container, { rules: { "color-contrast": { enabled: false } } })).violations).toEqual([]);
  });

  it("ignores a late pagination response instead of replacing the current page", async () => {
    let resolvePageTwo!: (value: Response) => void;
    let resolveReturnToPageOne!: (value: Response) => void;
    const pageTwo = new Promise<Response>((resolve) => { resolvePageTwo = resolve; });
    const returnToPageOne = new Promise<Response>((resolve) => { resolveReturnToPageOne = resolve; });
    let pageOneRequests = 0;
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("page=2")) return pageTwo;
      pageOneRequests += 1;
      if (pageOneRequests === 1) {
        return Promise.resolve(response({
          ...meData,
          pagination: { page: 1, page_size: 20, total: 21, pages: 2 },
          history: [{ match_id: "first", room_code: "111111", topic: "第一页比赛", status: "completed", winner: "aff", completed_at: null }],
        }));
      }
      return returnToPageOne;
    }));

    render(<MePage />);
    expect(await screen.findByText("第一页比赛")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /下一页/ }));
    fireEvent.click(screen.getByRole("button", { name: /上一页/ }));

    resolveReturnToPageOne(response({
      ...meData,
      pagination: { page: 1, page_size: 20, total: 21, pages: 2 },
      history: [{ match_id: "latest", room_code: "333333", topic: "返回后的第一页", status: "completed", winner: "neg", completed_at: null }],
    }));
    expect(await screen.findByText("返回后的第一页")).toBeInTheDocument();

    resolvePageTwo(response({
      ...meData,
      pagination: { page: 2, page_size: 20, total: 21, pages: 2 },
      history: [{ match_id: "late", room_code: "222222", topic: "迟到的第二页", status: "completed", winner: "draw", completed_at: null }],
    }));
    await waitFor(() => expect(screen.queryByText("迟到的第二页")).not.toBeInTheDocument());
    expect(screen.getByText("返回后的第一页")).toBeInTheDocument();
  });

  it("renders historical matches as readable list items with a specific return link", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      ...meData,
      summary: { ...meData.summary, history_total: 1 },
      history: [{
        match_id: "match-1",
        room_code: "192885",
        topic: "人工智能是否提升了人类创作者的价值？",
        status: "terminated",
        winner: null,
        completed_at: "2026-07-19T07:30:04Z",
      }],
    })));
    render(<MePage />);

    expect(await screen.findByRole("list", { name: "历史比赛列表" })).toBeInTheDocument();
    const item = screen.getByRole("listitem");
    expect(item).toHaveTextContent("#192885");
    expect(item).toHaveTextContent("人工智能是否提升了人类创作者的价值？");
    expect(item).toHaveTextContent("比赛已终止");
    expect(item).toHaveTextContent("已终止");
    expect(screen.getByRole("link", { name: "查看房间 192885 的比赛记录" })).toHaveAttribute("href", "/rooms/192885/result");
  });

  it("does not mislabel an imported completed match without a winner as pending review", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      ...meData,
      summary: { ...meData.summary, history_total: 1 },
      history: [{
        match_id: "legacy-match",
        room_code: "194205",
        topic: "导入的历史比赛",
        status: "completed",
        winner: null,
        completed_at: "2025-01-03T12:00:00Z",
      }],
    })));
    render(<MePage />);

    expect(await screen.findByText("结果未记录")).toBeInTheDocument();
    expect(screen.queryByText("待复核")).not.toBeInTheDocument();
  });

  it("makes a paused match recoverable without implying that it has ended", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      ...meData,
      summary: { ...meData.summary, active_total: 1 },
      active_rooms: [{
        code: "551958",
        topic: "长期暂停的正式比赛",
        status: "paused",
        seat_key: "aff_1",
        occupant_type: "human",
        can_resume: true,
        restore_request: null,
      }],
    })));
    render(<MePage />);

    const link = await screen.findByRole("link", { name: /比赛已暂停 · 房主或管理员可在控制台恢复/ });
    expect(link).toHaveAttribute("href", "/rooms/551958/debate");
    expect(link).toHaveTextContent("查看并等待恢复");
    expect(screen.queryByText("比赛已终止")).not.toBeInTheDocument();
  });
});
