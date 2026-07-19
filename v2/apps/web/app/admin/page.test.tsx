import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import AdminPage from "@/app/admin/page";

const navigation = vi.hoisted(() => ({ replace: vi.fn() }));

vi.mock("next/navigation", () => ({ useRouter: () => navigation }));
vi.mock("@/lib/use-session", () => ({
  useSession: () => ({
    user: { id: "admin", account: "admin", real_name: "管理员", role: "system_admin", is_active: true },
    loading: false,
  }),
}));

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

const competition = {
  id: "competition-1",
  slug: "daily-4v4",
  name: "4v4 人机辩论日常赛",
  tagline: "正式比赛",
  description: "",
  rules: "",
  format: "4v4",
  seat_count: 8,
  ranked: true,
  allow_custom_topic: false,
  accent: "violet",
  live_count: 0,
  is_active: true,
  season: { id: "season-1", name: "第一赛季", slug: "season-1", starts_at: new Date(0).toISOString(), ends_at: null, is_active: true, is_open: true, created_at: new Date(0).toISOString(), updated_at: new Date(0).toISOString(), competition_count: 1, match_count: 0 },
  topics: [{ id: "topic-1", title: "技术进步是否让人更自由？", is_active: true }],
};

const legacyCompetition = {
  ...competition,
  id: "competition-legacy",
  slug: "legacy-archive",
  name: "旧系统历史比赛",
  tagline: "V1 迁移的只读比赛记录",
  format: "legacy",
  ranked: false,
  is_active: false,
  season: null,
  topics: [],
};

describe("admin operations", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("renders topic, agent and versioned automation controls", async () => {
    const confirm = vi.fn(() => false);
    vi.stubGlobal("confirm", confirm);
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/admin/dashboard")) return Promise.resolve(response({
        counts: { users: 3, competitions: 2, live_rooms: 1, review_required: 0 },
        rooms: [], providers: {}, leaderboard: [],
        system_health: { ok: true, checks: {
          schema: { ok: true, current: "0022_remove_classroom_domain", expected: "0022_remove_classroom_domain" },
          moss_tts_realtime: { ok: true, ready_endpoints: 1, required_endpoints: 1, service: "MOSS-TTS-Realtime" },
          funasr: { ok: true, latency_ms: 0.1, service: "FunASR" },
        }, checked_at: new Date().toISOString() },
      }));
      if (url.includes("/api/admin/users")) return Promise.resolve(response({ items: [{
        id: "user-1", account: "student", real_name: "参赛学生", role: "user",
        is_active: true, is_test_account: false,
      }], pagination: { page: 1, page_size: 100, total: 1, pages: 1 } }));
      if (url.endsWith("/api/admin/competitions")) return Promise.resolve(response({ items: [legacyCompetition, competition] }));
      if (url.endsWith("/api/admin/seasons")) return Promise.resolve(response({ items: [competition.season] }));
      if (url.endsWith("/api/admin/automation-templates")) return Promise.resolve(response({ items: [{
        id: "template-2", slug: "training-1v1", name: "1v1 自动训练流程", version: 2, is_active: true,
        stages: [
          { key: "aff", name: "正方发言", kind: "speech", seat: "aff_1", duration: 60 },
          { key: "judge", name: "裁判", kind: "judging", duration: 30 },
        ],
        competitions: [{ id: competition.id, name: competition.name }], created_at: new Date().toISOString(),
      }, {
        id: "template-legacy", slug: "legacy-import", name: "旧系统只读流程", version: 1, is_active: false,
        stages: [], competitions: [{ id: legacyCompetition.id, name: legacyCompetition.name }], created_at: new Date(0).toISOString(),
      }] }));
      if (url.includes("/api/admin/rooms")) return Promise.resolve(response({ items: [], pagination: { page: 1, page_size: 100, total: 0, pages: 1 } }));
      if (url.endsWith("/api/admin/agents/agent-1")) return Promise.resolve(response({ ok: true }));
      if (url.endsWith("/api/admin/agents")) return Promise.resolve(response({ items: [{
        id: "agent-1", name: "乾元", provider: "debate_agent", model_name: "qwen3.6-27b",
        endpoint: "http://agent.test/api/debate", voice_id: "debate_voice_1", is_active: true,
      }] }));
      if (url.endsWith("/api/admin/judges")) return Promise.resolve(response({ items: [{
        id: "judge-1", name: "正式赛裁判", endpoint: "http://judge.test/api/judge", model_name: "judge-27b",
        system_prompt: "依据论证质量公平裁决", timeout_seconds: 120, is_active: true,
        created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
      }] }));
      if (url.endsWith("/api/admin/providers")) return Promise.resolve(response({ items: [
        { id: "provider-asr", kind: "funasr", endpoint: "ws://127.0.0.1:10095", settings: { final_wait_seconds: 30 }, is_active: true, created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
        { id: "provider-tts", kind: "lighttts", endpoint: "http://127.0.0.1:8080/inference_zero_shot", settings: { read_timeout_seconds: 180, speed: 1 }, is_active: false, created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
      ] }));
      if (url.endsWith("/api/admin/audio-cues")) return Promise.resolve(response({ items: [{
        id: "cue-1", key: "opening", name: "开场播报", text: "欢迎进入比赛",
        audio_url: "/media/_cues/cue-1.wav", is_active: true, updated_at: new Date().toISOString(),
      }] }));
      if (url.endsWith("/api/admin/media")) return Promise.resolve(response({ media: null }));
      if (url.endsWith("/api/admin/archives")) return Promise.resolve(response({ archives: {
        scanned_files: 3, scanned_bytes: 4096, expected_matches: 1, complete_archives: 1,
        invalid_archive_count: 0, invalid_archives: [], orphan_candidate_count: 0,
        orphan_candidate_bytes: 0, orphan_candidates: [], unsafe_entries: 0,
        unmanaged_entries: 0, truncated: false, deleted_files: 0, deleted_bytes: 0,
      } }));
      if (url.endsWith("/api/admin/reviews/score-1/retry")) return Promise.resolve(response({ ok: true }));
      if (url.endsWith("/api/admin/reviews/score-2/retry")) return Promise.resolve(response({ ok: true }));
      if (url.endsWith("/api/admin/reviews")) return Promise.resolve(response({ items: [
        {
          scorecard_id: "score-1", match_id: "match-1", room_code: "381526", topic: "技术进步是否让人更自由？",
          status: "review_required", winner: null, affirmative_score: 0, negative_score: 0,
          reason: "裁判服务暂不可用", updated_at: new Date(0).toISOString(),
        },
        {
          scorecard_id: "score-2", match_id: "match-2", room_code: "381527", topic: "学校是否应该限制生成式人工智能？",
          status: "review_required", winner: null, affirmative_score: 0, negative_score: 0,
          reason: "裁判服务暂不可用", updated_at: new Date(0).toISOString(),
        },
      ], recent: [] }));
      if (url.includes("/api/admin/audit")) return Promise.resolve(response({ items: [{
        id: "audit-1", actor_name: "管理员", action: "provider_config.test", target_type: "provider_config",
        target_id: "provider-1", payload: { ok: true }, created_at: new Date(0).toISOString(),
      }], pagination: { page: 1, page_size: 100, total: 1, pages: 1 } }));
      return Promise.resolve(new Response(JSON.stringify({ detail: "unexpected request" }), { status: 500 }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<AdminPage />);
    expect(await screen.findByRole("heading", { name: "系统总览" })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/admin/dashboard");
    expect(screen.getByText(/MOSS 实时语音合成/)).toBeInTheDocument();
    expect(screen.getByText(/1\/1 个实时端点已就绪/)).toBeInTheDocument();
    expect(screen.getByText(/FunASR 语音识别/)).toBeInTheDocument();
    expect(screen.getByText("结构版本已同步")).toBeInTheDocument();
    expect(screen.queryByText(/remove_classroom_domain/)).not.toBeInTheDocument();

    const mobileModulePicker = screen.getByRole("combobox", { name: "管理模块" });
    expect(mobileModulePicker).toHaveValue("overview");
    expect(screen.getAllByRole("option")).toHaveLength(9);
    fireEvent.change(mobileModulePicker, { target: { value: "rooms" } });
    expect(screen.getByRole("button", { name: /比赛监管/ })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: /系统总览/ })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("region", { name: "比赛监管" })).toHaveAttribute("id", "admin-module-content");
    expect(await screen.findByRole("region", { name: "比赛监管表格" })).toHaveAttribute("tabindex", "0");
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/admin/rooms")),
      ).toHaveLength(1),
    );
    fireEvent.click(screen.getByRole("button", { name: /系统总览/ }));
    fireEvent.click(screen.getByRole("button", { name: /比赛监管/ }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/admin/rooms")),
      ).toHaveLength(1),
    );
    fireEvent.click(screen.getByRole("button", { name: "刷新当前模块" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/admin/rooms")),
      ).toHaveLength(2),
    );

    fireEvent.click(screen.getByRole("button", { name: /用户管理/ }));
    expect(await screen.findByRole("region", { name: "用户管理表格" })).toHaveAttribute("tabindex", "0");
    fireEvent.click(screen.getByRole("button", { name: "停用" }));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("立即退出所有设备"));
    expect(fetch).not.toHaveBeenCalledWith(
      expect.stringContaining("/api/admin/users/user-1"),
      expect.objectContaining({ method: "PATCH" }),
    );

    fireEvent.click(screen.getByRole("button", { name: /赛事与题库/ }));
    expect(await screen.findAllByText(/题库管理/)).toHaveLength(2);
    expect(screen.getByRole("button", { name: "创建赛季" })).toBeEnabled();
    expect(screen.getByLabelText("绑定赛季")).toHaveValue("season-1");
    expect(screen.getByRole("heading", { name: "4v4 人机辩论正式赛" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "4v4 人机辩论日常赛" })).not.toBeInTheDocument();
    const legacyCard = screen.getByRole("heading", { name: "旧系统历史比赛" }).closest("article");
    expect(legacyCard).toHaveTextContent("历史记录 · 永久只读");
    expect(legacyCard).toHaveTextContent("不能重新启用、绑定赛季或维护题库");
    expect(legacyCard?.querySelector("button")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /AI 与语音/ }));
    const agentDisableButton = (await screen.findAllByRole("button", { name: "停用" }))[0];
    expect(agentDisableButton).toBeEnabled();
    expect(screen.getByRole("heading", { name: /AI 裁判配置/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /语音服务运行配置/ })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "MOSS 实时语音合成运行状态" })).toHaveTextContent("当前运行");
    expect(screen.getByRole("button", { name: "保存 funasr" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "保存 lighttts" })).not.toBeInTheDocument();
    expect(screen.getByText("正式赛裁判")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "创建裁判配置" })).toBeEnabled();
    expect(screen.getByRole("heading", { name: /阶段预设语音/ })).toBeInTheDocument();
    expect(screen.getByText("开场播报")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上传预设语音" })).toBeEnabled();

    const callsBeforeAgentPatch = fetchMock.mock.calls.length;
    fireEvent.click(agentDisableButton);
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/admin/agents/agent-1"),
        expect.objectContaining({ method: "PATCH" }),
      );
    });
    await waitFor(() =>
      expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(
        callsBeforeAgentPatch + 2,
      ),
    );
    const agentPatchRequests = fetchMock.mock.calls
      .slice(callsBeforeAgentPatch)
      .map(([input]) => String(input));
    expect(agentPatchRequests).not.toContainEqual(
      expect.stringContaining("/api/admin/competitions"),
    );
    expect(agentPatchRequests).not.toContainEqual(
      expect.stringContaining("/api/admin/rooms"),
    );
    expect(agentPatchRequests).not.toContainEqual(
      expect.stringContaining("/api/admin/users"),
    );
    expect(agentPatchRequests).not.toContainEqual(
      expect.stringContaining("/api/admin/audit"),
    );

    fireEvent.click(screen.getByRole("button", { name: /媒体存储/ }));
    expect(await screen.findByRole("heading", { name: "比赛归档与校验" })).toBeInTheDocument();
    expect(screen.getByText("全部终局比赛归档均通过校验")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "修复缺失或损坏归档" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: /自动流程/ }));
    expect(await screen.findByText("v2")).toBeInTheDocument();
    const legacyTemplateRow = screen.getByText("旧系统只读流程").closest("tr");
    expect(legacyTemplateRow).toHaveTextContent("只读归档");
    expect(legacyTemplateRow?.querySelector("button")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "创建新版本" }));
    expect(await screen.findByRole("dialog", { name: "创建自动流程新版本" })).toBeInTheDocument();
    expect((screen.getByLabelText("阶段 JSON") as HTMLTextAreaElement).value).toContain('"kind": "judging"');
    expect(screen.getByText(/只影响新创建房间/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /结果复核/ }));
    const retryJudges = await screen.findAllByRole("button", { name: "重试 AI 裁判" });
    expect(retryJudges).toHaveLength(2);
    fireEvent.click(retryJudges[0]);
    fireEvent.click(retryJudges[1]);
    expect(screen.getAllByRole("button", { name: "正在重新排队…" })).toHaveLength(2);
    expect(await screen.findByText(/已使用当前启用裁判重新排队/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /审计日志/ }));
    expect(await screen.findByRole("region", { name: "审计日志表格" })).toBeInTheDocument();
    expect(screen.getByText("测试服务连接")).toBeInTheDocument();
    expect(screen.getByText(/服务配置 · provider-1/)).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/admin/audit?page=1&page_size=100"),
      expect.any(Object),
    );
  }, 15_000);
});
