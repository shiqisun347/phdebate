import type { Review } from "@/components/admin/admin-module-types";

type ReviewsPanelProps = {
  reviews: Review[];
  recentReviews: Review[];
  retryingIds: Set<string>;
  onRetry: (review: Review) => void;
  onReview: (review: Review, mode?: "approve" | "correct") => void;
};

export default function ReviewsPanel({ reviews, recentReviews, retryingIds, onRetry, onReview }: ReviewsPanelProps) {
  return (
    <>
      <div className="panel-title"><h2>比赛结果复核</h2><span className="badge">{reviews.length} 项待处理</span></div>
      <div className="history-list">
        {reviews.map((item) => (
          <div className="history-row" style={{ gridTemplateColumns: "90px 1fr auto" }} key={item.scorecard_id}>
            <span className="room-code">#{item.room_code}</span>
            <span className="row-main"><strong>{item.topic}</strong><small>{item.reason}</small></span>
            <div className="card-actions">
              <button className="button button-small button-secondary" disabled={retryingIds.has(item.scorecard_id)} onClick={() => onRetry(item)}>
                {retryingIds.has(item.scorecard_id) ? "正在重新排队…" : "重试 AI 裁判"}
              </button>
              <button className="button button-small" disabled={retryingIds.has(item.scorecard_id)} onClick={() => onReview(item)}>复核判定</button>
            </div>
          </div>
        ))}
        {!reviews.length && <div className="empty">没有等待复核的比赛</div>}
      </div>
      <div className="panel-title" style={{ marginTop: 30 }}><h2>近期已确认赛果</h2><span className="muted">修正会追加积分补偿与审计记录</span></div>
      <div className="history-list">
        {recentReviews.map((item) => (
          <div className="history-row" style={{ gridTemplateColumns: "90px 1fr auto" }} key={item.scorecard_id}>
            <span className="room-code">#{item.room_code}</span>
            <span className="row-main">
              <strong>{item.topic}</strong>
              <small>{item.winner === "aff" ? "正方胜" : item.winner === "neg" ? "反方胜" : "平局"} · 正方 {item.affirmative_score.toFixed(1)} / 反方 {item.negative_score.toFixed(1)} · {item.reason}</small>
            </span>
            <button className="button button-small button-secondary" onClick={() => onReview(item, "correct")}>修正结果</button>
          </div>
        ))}
        {!recentReviews.length && <div className="empty">还没有可修正的已确认赛果</div>}
      </div>
    </>
  );
}
