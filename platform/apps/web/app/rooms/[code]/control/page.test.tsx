import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";

import ControlPage from "@/app/rooms/[code]/control/page";
import type { Room } from "@/lib/types";

const mocks = vi.hoisted(() => ({
  room: null as Room | null,
  replace: vi.fn(),
  setRoom: vi.fn(),
  apiFetch: vi.fn(),
}));

const runningRoom = {
  id: "room-id",
  code: "123456",
  topic: "控制台在断线时是否仍能展示操作结果？",
  status: "running",
  visibility: "private",
  seq: 1,
  competition: {
    id: "competition",
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
    live_count: 1,
  },
  season: null,
  owner: { id: "admin", real_name: "管理员" },
  seats: [],
  current_stage: {
    key: "stage",
    name: "正方立论",
    kind: "speech",
    duration: 120,
    seat: "aff_1",
  },
  current_stage_index: 0,
  remaining_seconds: 90,
  turn_remaining_seconds: null,
  active_speech: null,
  my_seat: null,
  can_speak: false,
  speak_reason: "等待轮次",
  can_control: true,
  failure_reason: "",
  recent_events: [],
  speeches: [],
} as Room;

vi.mock("next/navigation", () => ({
  useParams: () => ({ code: "123456" }),
  useRouter: () => ({ replace: mocks.replace }),
}));
vi.mock("@/lib/use-session", () => ({
  useSession: () => ({ user: { id: "admin", role: "system_admin" } }),
}));
vi.mock("@/lib/use-room", () => ({
  useCountdown: (value: number | null) => value,
  useRoom: () => ({
    room: mocks.room || runningRoom,
    setRoom: mocks.setRoom,
    connected: false,
    error: "实时连接已断开，正在重新连接…",
    refresh: vi.fn(),
  }),
}));
vi.mock("@/lib/api", () => ({ apiFetch: mocks.apiFetch }));

describe("room control console", () => {
  afterEach(() => {
    mocks.room = null;
    mocks.apiFetch.mockReset();
    mocks.setRoom.mockReset();
    mocks.replace.mockReset();
  });

  it("applies the REST response while WebSocket is unavailable and has no permanently disabled retry control", async () => {
    const paused = {
      ...runningRoom,
      status: "paused",
      seq: 2,
      remaining_seconds: 88,
    } as Room;
    mocks.apiFetch.mockResolvedValueOnce({ room: paused });
    const { container } = render(<ControlPage />);
    expect(
      screen.getByRole("heading", { level: 1, name: "房间 #123456 控制台" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("实时连接已断开");
    expect(screen.getByText("比赛正在自动进行")).toBeInTheDocument();
    expect(
      screen.getByText(/除非现场出现异常，否则不需要操作/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/WebSocket|LLM|TTS|WebRTC/),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/LightTTS/)).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "重试当前步骤" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "暂停比赛" }));
    await waitFor(() => expect(mocks.apiFetch).toHaveBeenCalledOnce());
    const updater = mocks.setRoom.mock.calls[0][0] as (current: Room) => Room;
    expect(updater(runningRoom)).toEqual(paused);
    const accessibility = await axe.run(container, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(accessibility.violations).toEqual([]);
  });

  it("separates the normal primary control from recovery and irreversible actions", () => {
    render(<ControlPage />);

    const primary = screen.getByRole("navigation", { name: "比赛主要控制" });
    expect(within(primary).getAllByRole("button")).toHaveLength(1);
    expect(
      within(primary).getByRole("button", { name: "暂停比赛" }),
    ).toBeEnabled();
    expect(
      within(primary).queryByRole("button", { name: "跳过当前阶段" }),
    ).not.toBeInTheDocument();
    expect(
      within(primary).queryByRole("button", { name: "提前结束" }),
    ).not.toBeInTheDocument();

    const recovery = screen.getByRole("region", { name: "异常处理" });
    expect(
      within(recovery).getByRole("button", { name: "跳过当前阶段" }),
    ).toBeEnabled();
    const danger = screen.getByRole("region", { name: "结束比赛" });
    expect(
      within(danger).getByRole("button", { name: "提前结束" }),
    ).toBeEnabled();
  });

  it("explains the read-only transition instead of flashing controls to a non-owner", async () => {
    mocks.room = { ...runningRoom, can_control: false } as Room;

    render(<ControlPage />);

    expect(
      screen.getByText("你没有本房间控制权限，正在切换到只读观战…"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "应急控制" }),
    ).not.toBeInTheDocument();
    await waitFor(() =>
      expect(mocks.replace).toHaveBeenCalledWith(
        "/rooms/123456/watch?notice=no-control",
      ),
    );
  });

  it("blocks destructive skip but offers an explicit safe pause while a human is speaking", async () => {
    mocks.room = {
      ...runningRoom,
      active_speech: {
        id: "speech-human",
        seat_key: "aff_1",
        speaker_type: "human",
        status: "speaking",
        content: "尚未结束的真人发言",
      },
    } as Room;

    render(<ControlPage />);

    const emergencyPause = screen.getByRole("button", {
      name: "紧急暂停当前发言",
    });
    expect(emergencyPause).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "真人发言中，不可跳过" }),
    ).toBeDisabled();
    expect(
      screen.getByText(/真人辩手正在发言；结束并提交后才能跳过阶段/),
    ).toBeInTheDocument();
    fireEvent.click(emergencyPause);
    expect(
      screen.getByRole("alertdialog", {
        name: "确认紧急暂停当前真人发言？",
      }),
    ).toHaveTextContent("保留已经确认的文字和剩余时间");
    mocks.apiFetch.mockResolvedValueOnce({
      room: { ...runningRoom, status: "paused", seq: 2 },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认紧急暂停" }));
    await waitFor(() =>
      expect(mocks.apiFetch).toHaveBeenCalledWith(
        "/api/rooms/123456/control/safe-pause",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("shows a concrete visible reason for every unavailable recovery action", () => {
    mocks.room = {
      ...runningRoom,
      status: "preparing",
      current_stage: null,
      current_stage_index: -1,
      remaining_seconds: null,
    } as Room;

    render(<ControlPage />);

    const skip = screen.getByRole("button", { name: "跳过当前阶段" });
    expect(skip).toBeDisabled();
    expect(skip).toHaveAccessibleDescription(
      "系统正在完成开场准备，进入正式阶段后才可跳过。",
    );
    expect(
      screen.getAllByText(/系统正在建立实时语音并加载预设开场提示/)
        .length,
    ).toBeGreaterThan(0);
    expect(screen.getByText("自动流程当前步骤")).toBeInTheDocument();
    expect(
      screen.getByText(/完成后会自动进入第一阶段/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "暂停准备流程" }),
    ).toBeEnabled();
  });

  it("lets the owner pause the automatic opening preparation and resume it from the saved position", async () => {
    mocks.room = {
      ...runningRoom,
      status: "preparing",
      current_stage: null,
      current_stage_index: -1,
      remaining_seconds: null,
    } as Room;
    const paused = {
      ...mocks.room,
      status: "paused",
      seq: 2,
    } as Room;
    mocks.apiFetch
      .mockResolvedValueOnce({ room: paused })
      .mockResolvedValueOnce({ room: mocks.room });

    const view = render(<ControlPage />);
    fireEvent.click(screen.getByRole("button", { name: "暂停准备流程" }));

    await waitFor(() =>
      expect(mocks.apiFetch).toHaveBeenCalledWith(
        "/api/rooms/123456/control/pause",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    expect(mocks.setRoom).toHaveBeenCalledOnce();

    mocks.room = paused;
    view.rerender(<ControlPage />);
    fireEvent.click(screen.getByRole("button", { name: "继续比赛" }));
    await waitFor(() =>
      expect(mocks.apiFetch).toHaveBeenLastCalledWith(
        "/api/rooms/123456/control/resume",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("explains which human action the automatic flow is waiting for", () => {
    mocks.room = {
      ...runningRoom,
      seats: [
        {
          seat_key: "aff_1",
          side: "aff",
          position: 1,
          label: "正方一辩",
          occupant_type: "human",
          display_name: "林知夏",
          is_ready: true,
          connected: true,
          is_me: false,
        },
      ],
    } as Room;

    render(<ControlPage />);

    expect(screen.getByText("自动流程当前步骤")).toBeInTheDocument();
    expect(
      screen.getByText("正在等待林知夏（正方一辩）点击开始发言。"),
    ).toBeInTheDocument();
  });

  it("offers one explicit reset action for a failed AI speech and retries from the saved step", async () => {
    const failed = {
      ...runningRoom,
      status: "paused",
      seq: 8,
      failure_reason: "AI 语音服务暂时不可用",
      active_speech: {
        id: "speech-ai",
        seat_key: "aff_1",
        speaker_type: "ai",
        status: "failed",
        content: "",
      },
    } as Room;
    mocks.room = failed;
    mocks.apiFetch.mockResolvedValueOnce({
      room: { ...failed, status: "running", seq: 9, failure_reason: "" },
    });

    render(<ControlPage />);

    fireEvent.click(screen.getByRole("button", { name: "重置当前发言" }));
    expect(
      screen.getByRole("alertdialog", { name: "确认重置当前发言？" }),
    ).toHaveTextContent("从已保存的位置重新开始");
    fireEvent.click(screen.getByRole("button", { name: "确认重置当前发言" }));

    await waitFor(() =>
      expect(mocks.apiFetch).toHaveBeenCalledWith(
        "/api/rooms/123456/control/retry",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ reason: "房主重置未完成的当前步骤" }),
          headers: expect.objectContaining({
            "X-Idempotency-Key": expect.any(String),
          }),
        }),
      ),
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "当前步骤已重置",
    );
  });

  it("lets the owner reset a currently stuck AI speech without switching players", async () => {
    const active = {
      ...runningRoom,
      active_speech: {
        id: "speech-ai-playing",
        seat_key: "neg_1",
        speaker_type: "ai",
        status: "playing",
        content: "已经固定的 AI 发言",
        stream_generation: "generation-a",
      },
    } as Room;
    mocks.room = active;
    mocks.apiFetch.mockResolvedValueOnce({
      room: { ...active, seq: 2, active_speech: null },
    });

    render(<ControlPage />);

    fireEvent.click(screen.getByRole("button", { name: "重置当前发言" }));
    expect(
      screen.getByRole("alertdialog", { name: "确认重置当前 AI 发言？" }),
    ).toHaveTextContent("清除尚未播放的内容");
    expect(
      screen.getByRole("alertdialog", { name: "确认重置当前 AI 发言？" }),
    ).not.toHaveTextContent(/WebRTC|播放器/);
    fireEvent.click(screen.getByRole("button", { name: "确认重置发言" }));

    await waitFor(() =>
      expect(mocks.apiFetch).toHaveBeenCalledWith(
        "/api/rooms/123456/control/reset-speech",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ reason: "房主重置完全卡住的当前 AI 发言" }),
          headers: expect.objectContaining({
            "X-Idempotency-Key": expect.any(String),
          }),
        }),
      ),
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "从同一段文字重新播放",
    );
  });

  it("separates a manual pause from a service failure and offers continue instead of retry", () => {
    mocks.room = {
      ...runningRoom,
      status: "paused",
      failure_reason: "",
    } as Room;
    render(<ControlPage />);

    expect(screen.getByRole("button", { name: "继续比赛" })).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: "重置当前发言" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("比赛暂时暂停")).toBeInTheDocument();
    expect(screen.queryByText("自动流程运行中")).not.toBeInTheDocument();
    expect(
      screen.getAllByText(/计时和当前步骤均已保存/).length,
    ).toBeGreaterThan(0);
  });

  it("uses a keyboard-contained irreversible confirmation for early termination", async () => {
    render(<ControlPage />);

    const terminate = screen.getByRole("button", { name: "提前结束" });
    fireEvent.click(terminate);
    const dialog = screen.getByRole("alertdialog", {
      name: "确认提前结束比赛？",
    });
    expect(dialog).toHaveTextContent("不可恢复");
    expect(dialog).toHaveTextContent("如果只是暂时异常，请选择取消");
    const cancel = screen.getByRole("button", { name: "取消" });
    const confirm = screen.getByRole("button", { name: "确认提前结束" });
    await waitFor(() => expect(cancel).toHaveFocus());
    expect(document.body.style.overflow).toBe("hidden");
    confirm.focus();
    fireEvent.keyDown(window, { key: "Tab" });
    expect(cancel).toHaveFocus();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(document.body.style.overflow).toBe("");
    await waitFor(() => expect(terminate).toHaveFocus());
    expect(mocks.apiFetch).not.toHaveBeenCalled();
  });

  it("requires an accessible confirmation before skipping an irreversible stage", async () => {
    render(<ControlPage />);

    fireEvent.click(screen.getByRole("button", { name: "跳过当前阶段" }));
    expect(
      screen.getByRole("alertdialog", { name: "确认跳过当前阶段？" }),
    ).toHaveTextContent("正方立论");
    expect(mocks.apiFetch).not.toHaveBeenCalled();

    mocks.apiFetch.mockResolvedValueOnce({ room: { ...runningRoom, seq: 2 } });
    fireEvent.click(screen.getByRole("button", { name: "确认跳过" }));
    await waitFor(() =>
      expect(mocks.apiFetch).toHaveBeenCalledWith(
        "/api/rooms/123456/control/skip",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("keeps the room owner stable after the match starts", () => {
    mocks.room = {
      ...runningRoom,
      seats: [
        {
          seat_key: "aff_1",
          side: "aff",
          position: 1,
          label: "正方一辩",
          occupant_type: "human",
          display_name: "原房主",
          is_ready: true,
          connected: true,
          is_me: true,
          is_owner: true,
        },
        {
          seat_key: "neg_1",
          side: "neg",
          position: 1,
          label: "反方一辩",
          occupant_type: "human",
          display_name: "其他辩手",
          is_ready: true,
          connected: true,
          is_me: false,
          is_owner: false,
        },
      ],
    } as Room;

    render(<ControlPage />);

    expect(screen.queryByRole("heading", { name: "移交房主" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "移交房主" })).not.toBeInTheDocument();
    expect(mocks.apiFetch).not.toHaveBeenCalled();
  });

  it("does not let skip conceal a paused service failure", () => {
    mocks.room = {
      ...runningRoom,
      status: "paused",
      failure_reason: "Agent 请求超时",
    } as Room;

    render(<ControlPage />);

    const skip = screen.getByRole("button", { name: "跳过当前阶段" });
    expect(skip).toBeDisabled();
    expect(skip).toHaveAccessibleDescription(/请先重置当前发言.*不能用跳过掩盖异常/);
    expect(screen.getByRole("button", { name: "重置当前发言" })).toBeEnabled();
  });

  it("distinguishes AI voice preparation from audio playback", () => {
    mocks.room = {
      ...runningRoom,
      active_speech: {
        id: "speech-ai-synth",
        seat_key: "neg_1",
        speaker_type: "ai",
        status: "synthesizing",
        content: "已经固定的发言文字",
      },
    } as Room;

    render(<ControlPage />);

    expect(screen.getAllByText(/AI 发言正在生成或合成语音/)).not.toHaveLength(0);
    expect(screen.getByText(/生成发言或合成语音；准备完成后自动播放/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试 AI 语音准备" }));
    expect(screen.getByRole("alertdialog", { name: "确认重置当前 AI 发言？" })).toHaveTextContent(/停止当前生成或语音合成任务/);
  });

  it("shows the authoritative disconnect countdown while keeping the seat human", () => {
    mocks.room = {
      ...runningRoom,
      seats: [
        {
          seat_key: "aff_1",
          side: "aff",
          position: 1,
          label: "正方一辩",
          occupant_type: "human",
          display_name: "张三",
          is_ready: true,
          connected: false,
          is_me: false,
        },
      ],
      disconnect_grace: {
        will_pause: true,
        pending: [
          {
            seat_key: "aff_1",
            display_name: "张三",
            remaining_seconds: 37,
          },
        ],
      },
    } as Room;
    const { container } = render(<ControlPage />);
    expect(screen.getByRole("status")).toHaveTextContent(
      "已断线，正在等待返回",
    );
    expect(screen.getByRole("status")).toHaveTextContent("37 秒后自动暂停");
    expect(screen.getByRole("status")).toHaveTextContent(
      "真人席位和身份保持不变",
    );
    expect(container).not.toHaveTextContent(/AI (接管|接替|代打)/);
    expect(
      screen.queryByRole("button", { name: /让 AI 接替/ }),
    ).not.toBeInTheDocument();
    const skip = screen.getByRole("button", { name: "跳过当前阶段" });
    expect(skip).toBeDisabled();
    expect(skip).toHaveAccessibleDescription(
      "正在等待张三重新连接，真人断线期间不能跳过当前阶段。",
    );
  });

  it("turns an expired disconnect grace into a non-clickable automatic-pause state", () => {
    mocks.room = {
      ...runningRoom,
      seats: [
        {
          seat_key: "aff_1",
          side: "aff",
          position: 1,
          label: "正方一辩",
          occupant_type: "human",
          display_name: "张三",
          is_ready: true,
          connected: false,
          is_me: true,
          is_owner: true,
        },
      ],
      disconnect_grace: {
        will_pause: true,
        pending: [
          {
            seat_key: "aff_1",
            display_name: "张三",
            remaining_seconds: 0,
          },
        ],
      },
    } as Room;

    render(<ControlPage />);

    expect(screen.getAllByText("正在自动暂停").length).toBeGreaterThan(0);
    expect(
      screen.getByRole("button", {
        name: "真人断线倒计时结束，正在自动暂停",
      }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "跳过当前阶段" })).toBeDisabled();
  });

  it("treats a disconnect timeout as resumeable pause rather than a service retry", async () => {
    const participant = {
      seat_key: "aff_1",
      side: "aff" as const,
      position: 1,
      label: "正方一辩",
      occupant_type: "human" as const,
      display_name: "张三",
      is_ready: true,
      connected: true,
      is_me: true,
      is_owner: true,
    };
    const paused = {
      ...runningRoom,
      status: "paused",
      seq: 9,
      seats: [participant],
      failure_reason: "真人辩手断线超过 60 秒，比赛已安全暂停。",
      pause_health: {
        paused_at: new Date().toISOString(),
        paused_duration_seconds: 3,
        is_stale: false,
        capacity_consuming: true,
        reason_code: "participant_disconnected",
        recommended_action: "resume",
        can_terminate_to_release_capacity: true,
      },
    } as Room;
    mocks.room = paused;
    mocks.apiFetch.mockResolvedValueOnce({
      room: {
        ...paused,
        status: "running",
        failure_reason: "",
        pause_health: null,
      },
    });

    render(<ControlPage />);
    expect(screen.getByText(/全部真人已重新连接/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /重试当前步骤|重置当前发言/ }),
    ).not.toBeInTheDocument();
    const resume = screen.getByRole("button", { name: "继续比赛" });
    expect(resume).toBeEnabled();
    fireEvent.click(resume);
    await waitFor(() =>
      expect(mocks.apiFetch).toHaveBeenCalledWith(
        "/api/rooms/123456/control/resume",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("treats a human start timeout as a resumeable personnel pause rather than a service retry", async () => {
    const paused = {
      ...runningRoom,
      status: "paused",
      seq: 10,
      current_stage: {
        ...runningRoom.current_stage!,
        awaiting_human_start: true,
        human_speech_duration_seconds: 120,
      },
      remaining_seconds: 120,
      failure_reason: "真人获得轮次后长时间未开始发言，比赛已安全暂停。",
      pause_health: {
        paused_at: new Date().toISOString(),
        paused_duration_seconds: 3,
        is_stale: false,
        capacity_consuming: true,
        reason_code: "participant_start_timeout" as const,
        recommended_action: "resume" as const,
        can_terminate_to_release_capacity: true,
      },
    } as Room;
    mocks.room = paused;
    mocks.apiFetch.mockResolvedValueOnce({
      room: {
        ...paused,
        status: "running",
        failure_reason: "",
        pause_health: null,
      },
    });

    render(<ControlPage />);
    expect(screen.getByRole("status")).toHaveTextContent(
      "真人长时间未开始发言，比赛已安全暂停",
    );
    expect(screen.getAllByText(/发言时间尚未消耗/).length).toBeGreaterThan(0);
    expect(
      screen.queryByRole("button", { name: /重试当前步骤|重置当前发言/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "跳过当前阶段" })).toBeDisabled();
    const resume = screen.getByRole("button", { name: "继续比赛" });
    expect(resume).toBeEnabled();
    fireEvent.click(resume);
    await waitFor(() =>
      expect(mocks.apiFetch).toHaveBeenCalledWith(
        "/api/rooms/123456/control/resume",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("keeps resume and skip disabled until every timed-out human reconnects", () => {
    mocks.room = {
      ...runningRoom,
      status: "paused",
      seq: 10,
      seats: [
        {
          seat_key: "aff_1",
          side: "aff",
          position: 1,
          label: "正方一辩",
          occupant_type: "human",
          display_name: "张三",
          is_ready: true,
          connected: false,
          is_me: true,
          is_owner: true,
        },
      ],
      failure_reason: "真人辩手断线超过 60 秒，比赛已安全暂停。",
      pause_health: {
        paused_at: new Date().toISOString(),
        paused_duration_seconds: 3,
        is_stale: false,
        capacity_consuming: true,
        reason_code: "participant_disconnected",
        recommended_action: "resume",
        can_terminate_to_release_capacity: true,
      },
    } as Room;

    render(<ControlPage />);
    expect(
      screen.getByRole("button", { name: "等待全部真人重新连接" }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "跳过当前阶段" })).toBeDisabled();
    expect(mocks.apiFetch).not.toHaveBeenCalled();
  });

  it("does not offer a doomed retry when service failure and human disconnect overlap", () => {
    mocks.room = {
      ...runningRoom,
      status: "paused",
      seq: 11,
      seats: [
        {
          seat_key: "aff_1",
          side: "aff",
          position: 1,
          label: "正方一辩",
          occupant_type: "human",
          display_name: "张三",
          is_ready: true,
          connected: false,
          is_me: true,
          is_owner: true,
        },
      ],
      failure_reason: "实时语音服务暂时不可用",
      pause_health: {
        paused_at: new Date().toISOString(),
        paused_duration_seconds: 8,
        is_stale: false,
        capacity_consuming: true,
        reason_code: "service_failure_and_participant_disconnected",
        recommended_action: "retry",
        can_terminate_to_release_capacity: true,
      },
    } as Room;

    render(<ControlPage />);

    expect(
      screen.getByText(/服务异常发生时，还有真人辩手处于断线状态/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "等待全部真人重新连接后重试" }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "跳过当前阶段" })).toBeDisabled();
    expect(
      screen.getAllByText(/人员齐全后再重置当前发言/).length,
    ).toBeGreaterThan(0);
    expect(mocks.apiFetch).not.toHaveBeenCalled();
  });

  it("offers retry after every human reconnects from a combined failure pause", async () => {
    const paused = {
      ...runningRoom,
      status: "paused",
      seq: 12,
      seats: [
        {
          seat_key: "aff_1",
          side: "aff" as const,
          position: 1,
          label: "正方一辩",
          occupant_type: "human" as const,
          display_name: "张三",
          is_ready: true,
          connected: true,
          is_me: true,
          is_owner: true,
        },
      ],
      failure_reason: "实时语音服务暂时不可用",
      pause_health: {
        paused_at: new Date().toISOString(),
        paused_duration_seconds: 8,
        is_stale: false,
        capacity_consuming: true,
        reason_code: "service_failure_and_participant_disconnected" as const,
        recommended_action: "retry" as const,
        can_terminate_to_release_capacity: true,
      },
    } as Room;
    mocks.room = paused;
    mocks.apiFetch.mockResolvedValueOnce({
      room: { ...paused, status: "running", seq: 13, failure_reason: "" },
    });

    render(<ControlPage />);

    const retry = screen.getByRole("button", { name: "重置当前发言" });
    expect(retry).toBeEnabled();
    fireEvent.click(retry);
    fireEvent.click(screen.getByRole("button", { name: "确认重置当前发言" }));
    await waitFor(() =>
      expect(mocks.apiFetch).toHaveBeenCalledWith(
        "/api/rooms/123456/control/retry",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("does not offer seat abandonment after the match is terminal", () => {
    const ownerSeat = {
      seat_key: "aff_1",
      side: "aff" as const,
      position: 1,
      label: "正方一辩",
      occupant_type: "human" as const,
      display_name: "原房主",
      is_ready: true,
      connected: true,
      is_me: true,
      is_owner: true,
    };
    const successorSeat = {
      seat_key: "neg_1",
      side: "neg" as const,
      position: 1,
      label: "反方一辩",
      occupant_type: "human" as const,
      display_name: "接任辩手",
      is_ready: true,
      connected: true,
      is_me: false,
      is_owner: false,
    };
    mocks.room = {
      ...runningRoom,
      status: "terminated",
      seats: [ownerSeat, successorSeat],
    } as Room;

    render(<ControlPage />);

    expect(screen.getByRole("button", { name: "比赛已结束（主操作不可用）" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "提前结束不可用（比赛已结束）" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "移交房主" })).not.toBeInTheDocument();
    expect(mocks.apiFetch).not.toHaveBeenCalled();
  });
});
