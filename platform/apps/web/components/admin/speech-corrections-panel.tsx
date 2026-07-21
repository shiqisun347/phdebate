"use client";

import { Check, FilePenLine, X } from "lucide-react";
import Link from "next/link";
import { type FormEvent, useState } from "react";

import type { AdminSpeechCorrection } from "@/components/admin/admin-module-types";

type Decision = "approve" | "reject";

const statusLabel: Record<AdminSpeechCorrection["status"], string> = {
  pending: "等待审核",
  approved: "已批准",
  rejected: "已拒绝",
  cancelled: "参赛者已撤销",
};

export function SpeechCorrectionsPanel({
  pending,
  recent,
  saving,
  onReview,
}: {
  pending: AdminSpeechCorrection[];
  recent: AdminSpeechCorrection[];
  saving: boolean;
  onReview: (item: AdminSpeechCorrection, decision: Decision, reason: string) => Promise<void>;
}) {
  const [target, setTarget] = useState<AdminSpeechCorrection | null>(null);
  const [decision, setDecision] = useState<Decision>("approve");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");

  function openReview(item: AdminSpeechCorrection, nextDecision: Decision) {
    setTarget(item);
    setDecision(nextDecision);
    setReason("");
    setError("");
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!target || reason.trim().length < 2) return;
    const action = decision === "approve" ? "批准并更新发言文字" : "拒绝本次修正申请";
    if (!window.confirm(`确认${action}？\n\n房间 #${target.room_code} · ${target.requester_name}\n该操作会记录管理员身份、理由和申请版本。`)) return;
    setError("");
    try {
      await onReview(target, decision, reason.trim());
      setTarget(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "审核操作失败");
    }
  }

  return (
    <>
      <div className="panel-title admin-review-title admin-review-history-title">
        <div>
          <h2><FilePenLine size={18} />真人发言修正</h2>
          <p className="admin-section-copy">逐条对照原始文字与参赛者建议。批准只更新规范化逐字稿，原始录音、分段快照和审计记录都会保留。</p>
        </div>
        <span className={`badge ${pending.length ? "closed" : "live"}`}>{pending.length} 项待处理</span>
      </div>
      <div className="admin-review-list">
        {pending.map((item) => (
          <article className="admin-review-card attention" key={item.id}>
            <div className="admin-review-card-head">
              <span className="room-code">#{item.room_code}</span>
              <span className="room-status-chip attention">等待文字核验</span>
              <time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString("zh-CN")}</time>
            </div>
            <h3>{item.topic}</h3>
            <p>{item.requester_name} · {item.seat_key} · 申请理由：{item.reason}</p>
            <div className="correction-compare">
              <div><b>原始发言</b><p>{item.original_content}</p></div>
              <div><b>建议文字</b><p>{item.proposed_content}</p></div>
            </div>
            <div className="admin-review-actions">
              <Link href={`/rooms/${item.room_code}/result`} target="_blank">查看完整记录</Link>
              <button className="button button-small button-secondary" disabled={saving} onClick={() => openReview(item, "reject")}><X size={15} />拒绝申请</button>
              <button className="button button-small" disabled={saving} onClick={() => openReview(item, "approve")}><Check size={15} />批准修正</button>
            </div>
          </article>
        ))}
        {!pending.length && <div className="empty">没有等待审核的真人发言修正</div>}
      </div>

      <div className="panel-title admin-review-title admin-review-history-title">
        <div><h2>近期发言修正记录</h2><p className="admin-section-copy">最近 50 条审核、拒绝或撤销记录。</p></div>
      </div>
      <div className="admin-review-list compact">
        {recent.map((item) => (
          <article className="admin-review-card" key={item.id}>
            <div className="admin-review-card-head"><span className="room-code">#{item.room_code}</span><span className="room-status-chip final">{statusLabel[item.status]}</span><time dateTime={item.updated_at}>{new Date(item.updated_at).toLocaleString("zh-CN")}</time></div>
            <h3>{item.requester_name} · {item.topic}</h3>
            <p>{item.review_reason || item.reason}</p>
            <div className="correction-compare compact"><div><b>原始发言</b><p>{item.original_content}</p></div><div><b>建议文字</b><p>{item.proposed_content}</p></div></div>
          </article>
        ))}
        {!recent.length && <div className="empty">还没有已处理的发言修正记录</div>}
      </div>

      {target && (
        <div className="dialog-overlay" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && setTarget(null)}>
          <form className="dialog" role="dialog" aria-modal="true" aria-labelledby="speech-correction-review-title" onSubmit={submit}>
            <div className="dialog-head">
              <div><span className="eyebrow">Transcript Review</span><h2 id="speech-correction-review-title">{decision === "approve" ? "批准发言修正" : "拒绝发言修正"}</h2></div>
              <button type="button" className="icon-button" aria-label="关闭" onClick={() => setTarget(null)}>×</button>
            </div>
            <p className="muted">房间 #{target.room_code} · {target.requester_name} · {target.seat_key}</p>
            <div className="correction-compare"><div><b>原始发言</b><p>{target.original_content}</p></div><div><b>建议文字</b><p>{target.proposed_content}</p></div></div>
            <div className="field">
              <label htmlFor="speech-correction-review-reason">审核理由</label>
              <textarea id="speech-correction-review-reason" className="textarea" minLength={2} maxLength={1000} required autoFocus value={reason} onChange={(event) => setReason(event.target.value)} placeholder={decision === "approve" ? "说明已核对的证据，例如录音、上下文与原始分段。" : "说明拒绝原因，例如录音证据与建议文字不符。"} />
            </div>
            <div className="notice-box">提交时会携带当前申请的更新时间。若其他管理员已处理，服务器会拒绝旧页面操作并要求刷新。</div>
            {error && <div className="error-box" role="alert">{error}</div>}
            <div className="dialog-actions">
              <button type="button" className="button button-secondary" disabled={saving} onClick={() => setTarget(null)}>取消</button>
              <button className={decision === "approve" ? "button" : "button button-danger"} disabled={saving || reason.trim().length < 2}>{saving ? "正在提交…" : decision === "approve" ? "确认批准" : "确认拒绝"}</button>
            </div>
          </form>
        </div>
      )}
    </>
  );
}
