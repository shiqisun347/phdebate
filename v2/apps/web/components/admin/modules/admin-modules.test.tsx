import { fireEvent, render, screen } from "@testing-library/react";
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
        saving={false}
        onRefreshMedia={vi.fn()}
        onCleanupMedia={vi.fn()}
        onRefreshArchives={vi.fn()}
        onRepairArchives={vi.fn()}
        onCleanupArchives={vi.fn()}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("超过安全扫描上限");
    expect(screen.getByRole("button", { name: "清理 24 小时以上孤儿" })).toBeDisabled();
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
