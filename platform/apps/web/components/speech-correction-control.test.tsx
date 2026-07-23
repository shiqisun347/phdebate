import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SpeechCorrectionControl, type SpeechCorrectionRequest } from "@/components/speech-correction-control";

const speech = { id: "speech-1", stage_name: "正方立论", content: "人工智能应该成为学习工具。" };
const pending: SpeechCorrectionRequest = {
  id: "correction-1", speech_id: speech.id, room_id: "room-1",
  original_content: speech.content, proposed_content: "人工智能不应该替代独立思考。",
  reason: "语音识别漏掉了否定词", status: "pending", review_reason: "",
  created_at: "2026-07-21T01:00:00Z", updated_at: "2026-07-21T01:00:00Z", resolved_at: null,
};

describe("SpeechCorrectionControl", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("submits an idempotent correction request only after explicit confirmation", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ request: pending }), { status: 200, headers: { "Content-Type": "application/json" } })));
    const onChanged = vi.fn();
    const { container } = render(<SpeechCorrectionControl roomCode="381526" speech={speech} onChanged={onChanged} />);

    fireEvent.click(screen.getByRole("button", { name: "申请修正发言文字" }));
    fireEvent.change(screen.getByLabelText("建议修正后的完整发言"), { target: { value: pending.proposed_content } });
    fireEvent.change(screen.getByLabelText("修正原因"), { target: { value: pending.reason } });
    fireEvent.click(screen.getByRole("button", { name: "提交修正申请" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(pending));
    const [, init] = vi.mocked(fetch).mock.calls[0];
    expect(String(vi.mocked(fetch).mock.calls[0][0])).toContain("/api/rooms/381526/speeches/speech-1/correction-requests");
    expect(init).toMatchObject({ method: "POST", body: JSON.stringify({ proposed_content: pending.proposed_content, reason: pending.reason }) });
    expect(new Headers(init?.headers).get("X-Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/i);
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("原始记录会保留"));
    expect((await axe.run(container, { rules: { "color-contrast": { enabled: false } } })).violations).toEqual([]);
  });

  it("shows and cancels only the supplied participant request", async () => {
    const cancelled = { ...pending, status: "cancelled" as const, updated_at: "2026-07-21T01:10:00Z" };
    vi.stubGlobal("confirm", vi.fn(() => true));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ request: cancelled }), { status: 200, headers: { "Content-Type": "application/json" } })));
    const onChanged = vi.fn();
    render(<SpeechCorrectionControl roomCode="381526" speech={speech} request={pending} onChanged={onChanged} />);

    expect(screen.getByText("等待管理员审核")).toBeInTheDocument();
    expect(screen.getByText(pending.proposed_content)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /再次申请/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "撤销申请" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(cancelled));
    expect(String(vi.mocked(fetch).mock.calls[0][0])).toContain("/speech-correction-requests/correction-1/cancel");
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("确认撤销"));
  });
});
