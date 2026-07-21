import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Review } from "@/components/admin/admin-module-types";
import ReviewsPanel from "@/components/admin/reviews-panel";

const pending: Review = {
  scorecard_id: "score-1",
  match_id: "match-1",
  room_code: "381526",
  topic: "AI 是否提升了人类创作者存在的意义",
  status: "review_required",
  winner: null,
  affirmative_score: 0,
  negative_score: 0,
  reason: "裁判服务连续超时",
  updated_at: "2026-07-20T08:30:00Z",
};

describe("ReviewsPanel", () => {
  it("explains the publication hold and provides evidence-first recovery actions", () => {
    const onRetry = vi.fn();
    const onReview = vi.fn();
    render(<ReviewsPanel reviews={[pending]} recentReviews={[]} speechCorrections={[]} recentSpeechCorrections={[]} retryingIds={new Set()} saving={false} onRetry={onRetry} onReview={onReview} onSpeechCorrectionReview={vi.fn()} />);

    expect(screen.getByText(/不会公布胜负或写入排行榜/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /查看比赛记录/ })).toHaveAttribute("href", "/rooms/381526/result");
    fireEvent.click(screen.getByRole("button", { name: "重试 AI 裁判" }));
    fireEvent.click(screen.getByRole("button", { name: "人工复核" }));
    expect(onRetry).toHaveBeenCalledWith(pending);
    expect(onReview).toHaveBeenCalledWith(pending);
  });

  it("shows an explicit healthy empty state", () => {
    render(<ReviewsPanel reviews={[]} recentReviews={[]} speechCorrections={[]} recentSpeechCorrections={[]} retryingIds={new Set()} saving={false} onRetry={vi.fn()} onReview={vi.fn()} onSpeechCorrectionReview={vi.fn()} />);
    expect(screen.getByText("没有等待复核的比赛，正式赛果均已处理")).toBeInTheDocument();
  });
});
