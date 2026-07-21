import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { readFileSync } from "node:fs";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TranscriptCollaboration } from "@/components/transcript-collaboration";
import { apiFetch } from "@/lib/api";
import type { TranscriptCollabAccess } from "@/lib/transcript-collab-client";
import type { Room, RoomSpeech } from "@/lib/types";

const mocks = vi.hoisted(() => ({
  create: vi.fn(),
  destroy: vi.fn(),
  setText: vi.fn(),
  callback: null as null | Record<string, (value: never) => void>,
  snapshot: { "speech-own": "原始本人文字", "speech-other": "其他辩手文字" } as Record<string, string>,
  listener: null as null | (() => void),
}));

vi.mock("@/lib/transcript-collab-client", () => ({ createTranscriptCollabSession: mocks.create }));
vi.mock("@/lib/api", () => ({ apiFetch: vi.fn() }));

const participantAccess: TranscriptCollabAccess = {
  document_name: "room:room-id",
  token: "token",
  expires_at: "2026-07-21T02:00:00Z",
  ws_path: "/collab",
  role: "participant",
  editable_speech_ids: ["speech-own"],
};

const speeches: RoomSpeech[] = [
  { id: "speech-own", seat_key: "aff_1", speaker: "张同学", stage_key: "case", content: "原始本人文字", audio_url: "", duration_seconds: 12, playback_started_at: null, playback_ends_at: null, status: "completed", created_at: "2026-07-21T01:00:00Z" },
  { id: "speech-other", seat_key: "neg_1", speaker: "李同学", stage_key: "case", content: "其他辩手文字", audio_url: "", duration_seconds: 12, playback_started_at: null, playback_ends_at: null, status: "completed", created_at: "2026-07-21T01:01:00Z" },
];

const room = {
  id: "room-id",
  code: "123456",
  can_control: false,
  seats: [{ seat_key: "aff_1", display_name: "张同学", is_me: true }],
} as Room;

function installSession(access = participantAccess) {
  mocks.create.mockImplementation(async (options: Record<string, unknown>) => {
    mocks.callback = options as typeof mocks.callback;
    (options.onAccess as (value: TranscriptCollabAccess) => void)(access);
    (options.onState as (value: string) => void)("synced");
    (options.onAwareness as (value: string[]) => void)(["王同学"]);
    return {
      access,
      snapshot: () => mocks.snapshot,
      setText: mocks.setText,
      subscribe: (listener: () => void) => {
        mocks.listener = listener;
        listener();
        return () => { mocks.listener = null; };
      },
      destroy: mocks.destroy,
    };
  });
  mocks.setText.mockImplementation((speechId: string, value: string) => {
    mocks.snapshot = { ...mocks.snapshot, [speechId]: value };
    mocks.listener?.();
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.snapshot = { "speech-own": "原始本人文字", "speech-other": "其他辩手文字" };
  mocks.listener = null;
  mocks.callback = null;
  installSession();
  vi.mocked(apiFetch).mockImplementation(async (path: string) => {
    if (path.endsWith("/speech-correction-requests")) return { items: [] };
    return {};
  });
});

describe("TranscriptCollaboration", () => {
  it("edits only token-scoped speeches, shows awareness and submits a confirmed idempotent correction", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.spyOn(crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000010");
    vi.mocked(apiFetch).mockImplementation(async (path: string) => {
      if (path.endsWith("/speech-correction-requests")) return { items: [] };
      if (path.includes("/speeches/speech-own/correction-requests")) return {
        request: {
          id: "correction-1", speech_id: "speech-own", room_id: "room-id", original_content: "原始本人文字",
          proposed_content: "协同修正后的本人文字", reason: "识别遗漏关键结论", status: "pending", review_reason: "",
          created_at: "2026-07-21T01:10:00Z", updated_at: "2026-07-21T01:10:00Z", resolved_at: null,
        },
      };
      return {};
    });
    const view = render(<TranscriptCollaboration room={room} speeches={speeches} />);

    expect(await screen.findByText("协同文字已同步")).toBeInTheDocument();
    expect(screen.getByLabelText("1 位其他编辑者在线")).toHaveTextContent("王同学");
    const own = screen.getByLabelText("张同学的协同文字草稿");
    expect(own).toBeEnabled();
    expect(screen.queryByLabelText("李同学的协同文字草稿")).not.toBeInTheDocument();
    expect(screen.getByText("其他辩手文字")).toBeInTheDocument();

    fireEvent.change(own, { target: { value: "协同修正后的本人文字" } });
    expect(mocks.setText).toHaveBeenCalledWith("speech-own", "协同修正后的本人文字");
    fireEvent.change(screen.getByLabelText("张同学的修正原因（必填）"), { target: { value: "识别遗漏关键结论" } });
    fireEvent.click(screen.getByRole("button", { name: "提交修正申请" }));

    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      "/api/rooms/123456/speeches/speech-own/correction-requests",
      expect.objectContaining({ headers: { "X-Idempotency-Key": "00000000-0000-4000-8000-000000000010" } }),
    ));
    expect(screen.getByText("修正申请等待管理员审核")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "提交修正申请" })).not.toBeInTheDocument();
    expect((await axe.run(view.container, { rules: { "color-contrast": { enabled: false } } })).violations).toEqual([]);
  });

  it("lets administrators edit shared drafts but never renders an author submission control", async () => {
    mocks.create.mockReset();
    installSession({ ...participantAccess, role: "admin", editable_speech_ids: ["speech-own", "speech-other"] });
    render(<TranscriptCollaboration room={{ ...room, can_control: true } as Room} speeches={speeches} />);

    expect(await screen.findByText(/管理员可以协同查看和编辑草稿/)).toBeInTheDocument();
    expect(screen.getByLabelText("张同学的协同文字草稿")).toBeEnabled();
    expect(screen.getByLabelText("李同学的协同文字草稿")).toBeEnabled();
    expect(screen.queryByRole("button", { name: "提交修正申请" })).not.toBeInTheDocument();
  });

  it("fails anonymous access closed, retains read-only transcript context and can retry", async () => {
    mocks.create.mockRejectedValue(new Error("请先登录后使用协同编辑。"));
    render(<TranscriptCollaboration room={room} speeches={speeches} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("请先登录后使用协同编辑");
    expect(screen.getByText(/上方正式文字记录仍保持只读可用/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新连接" }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(2));
  });

  it("announces disconnect recovery and destroys provider/doc on unmount", async () => {
    const view = render(<TranscriptCollaboration room={room} speeches={speeches} />);
    await screen.findByText("协同文字已同步");
    act(() => (mocks.callback?.onState as (value: string) => void)("disconnected"));
    expect(screen.getByText("连接中断，正在自动恢复")).toBeInTheDocument();
    act(() => (mocks.callback?.onState as (value: string) => void)("synced"));
    expect(screen.getByText("协同文字已同步")).toBeInTheDocument();
    view.unmount();
    expect(mocks.destroy).toHaveBeenCalledTimes(1);
  });

  it("does not reconnect when a room snapshot only replaces the speeches array identity", async () => {
    const view = render(<TranscriptCollaboration room={room} speeches={speeches} />);
    await screen.findByText("协同文字已同步");
    view.rerender(<TranscriptCollaboration room={{ ...room, seq: 99 } as Room} speeches={speeches.map((speech) => ({ ...speech }))} />);
    expect(mocks.create).toHaveBeenCalledTimes(1);
    expect(mocks.destroy).not.toHaveBeenCalled();

    view.rerender(<TranscriptCollaboration room={{ ...room, seq: 100 } as Room} speeches={speeches.map((speech, index) => index ? speech : { ...speech, content: "权威文字已批准更新" })} />);
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(2));
    expect(mocks.destroy).toHaveBeenCalledTimes(1);
  });

  it("keeps provider and Yjs in a lazy client chunk", () => {
    const component = readFileSync(`${process.cwd()}/components/transcript-collaboration.tsx`, "utf8");
    const client = readFileSync(`${process.cwd()}/lib/transcript-collab-client.ts`, "utf8");
    const pkg = JSON.parse(readFileSync(`${process.cwd()}/package.json`, "utf8"));
    expect(component).toContain('import("@/lib/transcript-collab-client")');
    expect(component).not.toMatch(/from ["'](?:@hocuspocus\/provider|yjs)["']/);
    expect(client).toContain('from "@hocuspocus/provider"');
    expect(client).toContain('from "yjs"');
    expect(client).not.toMatch(/\batob\b|\.split\(["']\.["']\)/);
    expect(pkg.dependencies["@hocuspocus/provider"]).toBe("4.4.0");
    expect(pkg.dependencies.yjs).toBe("13.6.31");
  });
});
