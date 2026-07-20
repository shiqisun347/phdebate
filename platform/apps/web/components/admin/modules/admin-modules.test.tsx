import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import AuditModule from "@/components/admin/audit-panel";
import MediaModule from "@/components/admin/modules/media-module";

describe("admin lazy modules", () => {
  it("keeps destructive media actions disabled when the inventory is incomplete", () => {
    render(
      <MediaModule
        media={{
          scanned_files: 10,
          scanned_bytes: 1024,
          referenced_files: 5,
          referenced_existing_files: 5,
          missing_reference_count: 0,
          missing_references: [],
          orphan_candidate_count: 2,
          orphan_candidate_bytes: 512,
          orphan_candidates: [],
          unsafe_entries: 0,
          truncated: true,
          disk_total_bytes: 4096,
          disk_used_bytes: 2048,
          disk_free_bytes: 2048,
          deleted_files: 0,
          deleted_bytes: 0,
        }}
        archives={null}
        dataQuality={null}
        saving={false}
        onRefreshMedia={vi.fn()}
        onCleanupMedia={vi.fn()}
        onRefreshArchives={vi.fn()}
        onRefreshDataQuality={vi.fn()}
        onUpdateDataIssue={vi.fn()}
        onRepairArchives={vi.fn()}
        onCleanupArchives={vi.fn()}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("超过安全扫描上限");
    expect(screen.getByRole("button", { name: "清理 24 小时以上孤儿" })).toBeDisabled();
  });

  it("offers an admin-only production archive index without mixing it into destructive actions", () => {
    render(
      <MediaModule
        media={null}
        archives={{
          scanned_files: 3,
          scanned_bytes: 2048,
          expected_matches: 1,
          complete_archives: 1,
          invalid_archive_count: 0,
          invalid_archives: [],
          orphan_candidate_count: 0,
          orphan_candidate_bytes: 0,
          orphan_candidates: [],
          unsafe_entries: 0,
          unmanaged_entries: 0,
          truncated: false,
          deleted_files: 0,
          deleted_bytes: 0,
        }}
        dataQuality={null}
        saving={false}
        onRefreshMedia={vi.fn()}
        onCleanupMedia={vi.fn()}
        onRefreshArchives={vi.fn()}
        onRefreshDataQuality={vi.fn()}
        onUpdateDataIssue={vi.fn()}
        onRepairArchives={vi.fn()}
        onCleanupArchives={vi.fn()}
      />,
    );

    expect(screen.getByRole("link", { name: "下载正式比赛索引" })).toHaveAttribute("href", "/api/admin/archive-index.csv");
  });

  it("shows actionable production data coverage and records a durable disposition", async () => {
    const onUpdateDataIssue = vi.fn().mockResolvedValue(undefined);
    render(
      <MediaModule
        media={null}
        archives={null}
        dataQuality={{
          scope: "production",
          matches: { total: 12, active: 2, completed: 8, review_required: 1, terminated: 1 },
          speeches: {
            human_completed: 20,
            human_with_transcript: 19,
            human_with_audio: 18,
            ai_completed: 36,
            transcript_coverage_percent: 95,
            audio_coverage_percent: 90,
          },
          attention: {
            published_without_scorecard: 0,
            published_without_speeches: 0,
            human_missing_transcript: 1,
            human_missing_audio: 2,
            human_missing_segments: 1,
            unreviewed_sample_issues: 2,
            samples: [{
              room_code: "381526",
              match_id: "match",
              speech_id: "speech",
              seat_key: "aff_1",
              stage_key: "aff_case",
              issues: ["missing_transcript", "missing_audio"],
              dispositions: {},
            }],
          },
        }}
        saving={false}
        onRefreshMedia={vi.fn()}
        onCleanupMedia={vi.fn()}
        onRefreshArchives={vi.fn()}
        onRefreshDataQuality={vi.fn()}
        onUpdateDataIssue={onUpdateDataIssue}
        onRepairArchives={vi.fn()}
        onCleanupArchives={vi.fn()}
      />,
    );

    expect(screen.getByText("真人逐字稿覆盖").closest(".stat-card")).toHaveTextContent("95%");
    expect(screen.getByText(/真人缺逐字稿 1 段/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看记录" })).toHaveAttribute("href", "/rooms/381526/result");
    fireEvent.click(screen.getByRole("button", { name: "缺少真人录音 · 未处置" }));
    expect(screen.getByRole("dialog", { name: "记录比赛数据异常处置" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("处置说明"), { target: { value: "联系选手安排补采" } });
    fireEvent.submit(screen.getByRole("button", { name: "保存处置记录" }).closest("form")!);
    await waitFor(() => expect(onUpdateDataIssue).toHaveBeenCalledWith(
      "speech",
      "missing_audio",
      "needs_recollection",
      "联系选手安排补采",
    ));
  });

  it("forwards the current audit query and page without losing keyboard semantics", () => {
    const onLoad = vi.fn();
    render(
      <AuditModule
        items={[]}
        query="seat.restore"
        pagination={{ page: 2, page_size: 100, total: 201, pages: 3 }}
        loading={false}
        onQueryChange={vi.fn()}
        onLoad={onLoad}
      />,
    );

    fireEvent.submit(screen.getByRole("button", { name: "搜索" }).closest("form")!);
    expect(onLoad).toHaveBeenCalledWith(1, "seat.restore");
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(onLoad).toHaveBeenCalledWith(3);
    expect(screen.getByRole("region", { name: "审计日志表格" })).toHaveAttribute("tabindex", "0");
  });
});
