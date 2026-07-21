import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AdminSpeechCorrection } from "@/components/admin/admin-module-types";
import { SpeechCorrectionsPanel } from "@/components/admin/speech-corrections-panel";

const request: AdminSpeechCorrection = {
  id: "correction-1", speech_id: "speech-1", room_id: "room-1", room_code: "381526",
  topic: "人工智能时代，还要不要学编程？", seat_key: "aff_1",
  requester_name: "张同学", requester_user_id: "user-1",
  original_content: "人工智能应该替代独立思考。", proposed_content: "人工智能不应该替代独立思考。",
  reason: "ASR 漏掉否定词", status: "pending", review_reason: "",
  created_at: "2026-07-21T01:00:00Z", updated_at: "2026-07-21T01:00:00Z", resolved_at: null,
};

describe("SpeechCorrectionsPanel", () => {
  afterEach(() => vi.unstubAllGlobals());

  it.each([
    ["批准修正", "approve" as const, "已核对录音，确认漏掉否定词", "确认批准并更新发言文字"],
    ["拒绝申请", "reject" as const, "录音证据不支持建议文字", "确认拒绝本次修正申请"],
  ])("requires reason, versioned callback and confirmation for %s", async (buttonName, decision, reason, confirmation) => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const onReview = vi.fn().mockResolvedValue(undefined);
    const { container } = render(<SpeechCorrectionsPanel pending={[request]} recent={[]} saving={false} onReview={onReview} />);

    expect(screen.getByText(request.original_content)).toBeInTheDocument();
    expect(screen.getByText(request.proposed_content)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: buttonName }));
    expect(screen.getByRole("button", { name: decision === "approve" ? "确认批准" : "确认拒绝" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("审核理由"), { target: { value: reason } });
    fireEvent.click(screen.getByRole("button", { name: decision === "approve" ? "确认批准" : "确认拒绝" }));

    await waitFor(() => expect(onReview).toHaveBeenCalledWith(request, decision, reason));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining(confirmation));
    expect((await axe.run(container, { rules: { "color-contrast": { enabled: false } } })).violations).toEqual([]);
  });
});
