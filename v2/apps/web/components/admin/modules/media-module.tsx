import { Download, RefreshCw, Trash2 } from "lucide-react";

import { bytes, type ArchiveStatus, type MediaStatus } from "@/components/admin/admin-module-types";

type Props = {
  media: MediaStatus | null;
  archives: ArchiveStatus | null;
  saving: boolean;
  onRefreshMedia: () => void;
  onCleanupMedia: () => void;
  onRefreshArchives: () => void;
  onRepairArchives: () => void;
  onCleanupArchives: () => void;
};

export default function MediaModule({ media, archives, saving, onRefreshMedia, onCleanupMedia, onRefreshArchives, onRepairArchives, onCleanupArchives }: Props) {
  return (
    <>
      <div className="panel-title"><h2>媒体存储与一致性</h2><span className="badge">默认只读盘点</span></div>
      {media ? (
        <>
          <div className="stats-grid">
            <div className="stat-card"><span className="muted">媒体文件</span><strong>{media.scanned_files}</strong><small>{bytes(media.scanned_bytes)}</small></div>
            <div className="stat-card"><span className="muted">有效引用</span><strong>{media.referenced_existing_files}/{media.referenced_files}</strong></div>
            <div className="stat-card"><span className="muted">孤儿候选</span><strong>{media.orphan_candidate_count}</strong><small>{bytes(media.orphan_candidate_bytes)}</small></div>
            <div className="stat-card"><span className="muted">磁盘剩余</span><strong>{bytes(media.disk_free_bytes)}</strong></div>
          </div>
          <div className="hero-actions" style={{ marginTop: 18 }}>
            <button className="button button-secondary" disabled={saving} onClick={onRefreshMedia}><RefreshCw size={16} />重新盘点</button>
            <button className="button button-danger" disabled={saving || media.orphan_candidate_count === 0 || media.truncated} onClick={onCleanupMedia}><Trash2 size={16} />清理 24 小时以上孤儿</button>
          </div>
          {media.truncated && <div className="error-box" role="alert">文件数量超过安全扫描上限，本次禁止执行清理。</div>}
          {media.unsafe_entries > 0 && <div className="error-box" role="alert">发现 {media.unsafe_entries} 个符号链接或不安全条目，系统已跳过。</div>}
          <h3 style={{ marginTop: 26 }}>缺失引用</h3>
          <div className="history-list">{media.missing_references.map((item) => <div className="history-row" key={item}>{item}</div>)}{media.missing_reference_count === 0 && <div className="empty">数据库引用的媒体文件均存在</div>}</div>
          <h3 style={{ marginTop: 26 }}>可清理候选</h3>
          <div className="table-wrap" role="region" aria-label="媒体清理候选表格" tabIndex={0}>
            <table><thead><tr><th>相对路径</th><th>大小</th><th>最后修改</th></tr></thead><tbody>{media.orphan_candidates.map((item) => <tr key={item.path}><td>{item.path}</td><td>{bytes(item.bytes)}</td><td>{new Date(item.modified_at).toLocaleString("zh-CN")}</td></tr>)}</tbody></table>
            {media.orphan_candidate_count === 0 && <div className="empty">没有超过 24 小时的孤儿媒体</div>}
          </div>
        </>
      ) : <div className="empty">正在盘点媒体存储…</div>}
      <div className="panel-title" style={{ marginTop: 36 }}><h2>比赛归档与校验</h2><span className="badge">终局记录</span></div>
      {archives ? (
        <>
          <div className="stats-grid">
            <div className="stat-card"><span className="muted">应归档比赛</span><strong>{archives.expected_matches}</strong></div>
            <div className="stat-card"><span className="muted">校验完整</span><strong>{archives.complete_archives}/{archives.expected_matches}</strong></div>
            <div className="stat-card"><span className="muted">缺失或损坏</span><strong>{archives.invalid_archive_count}</strong></div>
            <div className="stat-card"><span className="muted">孤儿候选</span><strong>{archives.orphan_candidate_count}</strong><small>{bytes(archives.orphan_candidate_bytes)}</small></div>
          </div>
          <div className="hero-actions" style={{ marginTop: 18 }}>
            <button className="button button-secondary" disabled={saving} onClick={onRefreshArchives}><RefreshCw size={16} />盘点比赛归档</button>
            <a className="button button-secondary" href="/api/admin/archive-index.csv"><Download size={16} />下载正式比赛索引</a>
            <button className="button" disabled={saving || archives.invalid_archive_count === 0 || archives.truncated} onClick={onRepairArchives}><RefreshCw size={16} />修复缺失或损坏归档</button>
            <button className="button button-danger" disabled={saving || archives.orphan_candidate_count === 0 || archives.truncated} onClick={onCleanupArchives}><Trash2 size={16} />清理孤儿归档</button>
          </div>
          {(archives.truncated || archives.unsafe_entries > 0) && <div className="error-box" role="alert">{archives.truncated ? "归档文件数量超过安全扫描上限，修复和清理已禁用。" : `发现 ${archives.unsafe_entries} 个不安全条目，系统已跳过。`}</div>}
          <h3 style={{ marginTop: 26 }}>缺失或校验失败</h3>
          <div className="history-list">{archives.invalid_archives.map((item) => <div className="history-row" key={item.match_id}><code>{item.match_id}</code><span>{item.reason}</span></div>)}{archives.invalid_archive_count === 0 && <div className="empty">全部终局比赛归档均通过校验</div>}</div>
          <h3 style={{ marginTop: 26 }}>归档清理候选</h3>
          <div className="table-wrap" role="region" aria-label="归档清理候选表格" tabIndex={0}>
            <table><thead><tr><th>文件</th><th>大小</th><th>最后修改</th></tr></thead><tbody>{archives.orphan_candidates.map((item) => <tr key={item.path}><td>{item.path}</td><td>{bytes(item.bytes)}</td><td>{new Date(item.modified_at).toLocaleString("zh-CN")}</td></tr>)}</tbody></table>
            {archives.orphan_candidate_count === 0 && <div className="empty">没有超过 24 小时的孤儿归档</div>}
          </div>
        </>
      ) : <div className="empty">正在盘点比赛归档…</div>}
    </>
  );
}
