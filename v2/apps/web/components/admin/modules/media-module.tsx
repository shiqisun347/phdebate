import Link from "next/link";
import { Download, RefreshCw, ShieldCheck, TriangleAlert, Trash2 } from "lucide-react";

import { bytes, type ArchiveStatus, type DataQualityStatus, type MediaStatus } from "@/components/admin/admin-module-types";

type Props = {
  media: MediaStatus | null;
  archives: ArchiveStatus | null;
  dataQuality: DataQualityStatus | null;
  saving: boolean;
  onRefreshMedia: () => void;
  onCleanupMedia: () => void;
  onRefreshArchives: () => void;
  onRefreshDataQuality: () => void;
  onRepairArchives: () => void;
  onCleanupArchives: () => void;
};

const issueLabels: Record<string, string> = {
  missing_transcript: "缺少逐字稿",
  missing_audio: "缺少真人录音",
  missing_segments: "缺少分段记录",
};

export default function MediaModule({ media, archives, dataQuality, saving, onRefreshMedia, onCleanupMedia, onRefreshArchives, onRefreshDataQuality, onRepairArchives, onCleanupArchives }: Props) {
  const qualityAttention = dataQuality
    ? dataQuality.attention.published_without_scorecard
      + dataQuality.attention.published_without_speeches
      + dataQuality.attention.human_missing_transcript
      + dataQuality.attention.human_missing_audio
      + dataQuality.attention.human_missing_segments
    : 0;
  return (
    <>
      <div className="panel-title">
        <div>
          <h2>比赛数据质量</h2>
          <p className="admin-section-copy">仅统计正式比赛，检查赛果、逐字稿、真人录音和分段记录是否可用于复盘与分析。</p>
        </div>
        <span className={`badge ${qualityAttention ? "closed" : ""}`}>{qualityAttention ? `${qualityAttention} 项需关注` : "数据链路完整"}</span>
      </div>
      {dataQuality ? (
        <>
          <div className="stats-grid">
            <div className="stat-card"><span className="muted">已发布比赛</span><strong>{dataQuality.matches.completed}</strong><small>待复核 {dataQuality.matches.review_required} 场</small></div>
            <div className="stat-card"><span className="muted">真人逐字稿覆盖</span><strong>{dataQuality.speeches.transcript_coverage_percent}%</strong><small>{dataQuality.speeches.human_with_transcript}/{dataQuality.speeches.human_completed} 段</small></div>
            <div className="stat-card"><span className="muted">真人录音覆盖</span><strong>{dataQuality.speeches.audio_coverage_percent}%</strong><small>{dataQuality.speeches.human_with_audio}/{dataQuality.speeches.human_completed} 段</small></div>
            <div className="stat-card"><span className="muted">AI 完成发言</span><strong>{dataQuality.speeches.ai_completed}</strong><small>正式比赛记录</small></div>
          </div>
          <div className="hero-actions" style={{ marginTop: 18 }}>
            <button className="button button-secondary" disabled={saving} onClick={onRefreshDataQuality}><RefreshCw size={16} />重新检查数据质量</button>
          </div>
          {qualityAttention ? (
            <div className="error-box" role="status">
              <TriangleAlert size={17} />
              已发布无有效裁判 {dataQuality.attention.published_without_scorecard} 场；无发言 {dataQuality.attention.published_without_speeches} 场；真人缺逐字稿 {dataQuality.attention.human_missing_transcript} 段；缺录音 {dataQuality.attention.human_missing_audio} 段；缺分段记录 {dataQuality.attention.human_missing_segments} 段。
            </div>
          ) : (
            <div className="success-box" role="status"><ShieldCheck size={17} />正式比赛的赛果、真人逐字稿、录音和分段记录均通过完整性检查。</div>
          )}
          {!!dataQuality.attention.samples.length && (
            <div className="history-list" style={{ marginTop: 16 }}>
              {dataQuality.attention.samples.map((item) => (
                <div className="history-row" key={item.speech_id}>
                  <span className="row-main"><strong>房间 #{item.room_code} · {item.seat_key}</strong><small>{item.stage_key} · {item.issues.map((issue) => issueLabels[issue] || issue).join("、")}</small></span>
                  <Link className="button button-small button-secondary" href={`/rooms/${item.room_code}/result`}>查看记录</Link>
                </div>
              ))}
            </div>
          )}
        </>
      ) : <div className="empty">正在检查正式比赛数据质量…</div>}
      <div className="panel-title" style={{ marginTop: 36 }}><h2>媒体存储与一致性</h2><span className="badge">默认只读盘点</span></div>
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
