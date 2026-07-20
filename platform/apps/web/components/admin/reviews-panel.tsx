import Link from "next/link";
import { AlertTriangle, CheckCircle2, ExternalLink } from "lucide-react";

import type { Review } from "@/components/admin/admin-module-types";

type ReviewsPanelProps = {
  reviews: Review[];
  recentReviews: Review[];
  retryingIds: Set<string>;
  onRetry: (review: Review) => void;
  onReview: (review: Review, mode?: "approve" | "correct") => void;
};

function reviewTime(value: string) {
  const time = new Date(value);
  if (Number.isNaN(time.getTime())) return "更新时间未知";
  return time.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export default function ReviewsPanel({ reviews, recentReviews, retryingIds, onRetry, onReview }: ReviewsPanelProps) {
  return (
    <>
      <div className="panel-title admin-review-title">
        <div>
          <h2><AlertTriangle size={18} />比赛结果复核</h2>
          <p className="admin-section-copy">待复核比赛不会公布胜负或写入排行榜，请先核对逐字稿和裁判失败原因。</p>
        </div>
        <span className={`badge ${reviews.length ? "closed" : "live"}`}>{reviews.length} 项待处理</span>
      </div>
      <div className="admin-review-list">
        {reviews.map((item) => {
          const retrying = retryingIds.has(item.scorecard_id);
          return (
            <article className="admin-review-card attention" key={item.scorecard_id}>
              <div className="admin-review-card-head">
                <span className="room-code">#{item.room_code}</span>
                <span className="room-status-chip attention">等待人工复核</span>
                <time dateTime={item.updated_at}>{reviewTime(item.updated_at)}</time>
              </div>
              <h3>{item.topic}</h3>
              <p>{item.reason || "自动裁判未形成可发布结果，请查看比赛记录后人工判定。"}</p>
              <div className="admin-review-actions">
                <Link className="button button-small button-secondary" href={`/rooms/${item.room_code}/result`} target="_blank">查看比赛记录<ExternalLink size={14} /></Link>
                <button className="button button-small button-secondary" aria-busy={retrying} disabled={retrying} onClick={() => onRetry(item)}>
                  {retrying ? "正在重新排队…" : "重试 AI 裁判"}
                </button>
                <button className="button button-small" disabled={retrying} onClick={() => onReview(item)}>人工复核</button>
              </div>
            </article>
          );
        })}
        {!reviews.length && <div className="empty empty-guidance"><CheckCircle2 size={28} color="#40df9c" /><span>没有等待复核的比赛，正式赛果均已处理</span></div>}
      </div>
      <div className="panel-title admin-review-title admin-review-history-title">
        <div><h2>近期已确认赛果</h2><p className="admin-section-copy">发现误判时以补偿记录修正，原始结算与审计日志会保留。</p></div>
      </div>
      <div className="admin-review-list compact">
        {recentReviews.map((item) => (
          <article className="admin-review-card" key={item.scorecard_id}>
            <div className="admin-review-card-head"><span className="room-code">#{item.room_code}</span><span className="room-status-chip final">赛果已确认</span><time dateTime={item.updated_at}>{reviewTime(item.updated_at)}</time></div>
            <h3>{item.topic}</h3>
            <p>{item.winner === "aff" ? "正方胜" : item.winner === "neg" ? "反方胜" : "平局"} · 正方 {item.affirmative_score.toFixed(1)} / 反方 {item.negative_score.toFixed(1)} · {item.reason}</p>
            <div className="admin-review-actions"><Link href={`/rooms/${item.room_code}/result`}>查看记录</Link><button className="button button-small button-secondary" onClick={() => onReview(item, "correct")}>修正结果</button></div>
          </article>
        ))}
        {!recentReviews.length && <div className="empty">还没有可修正的已确认赛果</div>}
      </div>
    </>
  );
}
