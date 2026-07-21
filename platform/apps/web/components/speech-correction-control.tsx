"use client";

import { CheckCircle2, FilePenLine, RotateCcw, XCircle } from "lucide-react";
import { type FormEvent, useRef, useState } from "react";

import { apiFetch } from "@/lib/api";

export type SpeechCorrectionRequest = {
  id: string;
  speech_id: string;
  room_id: string;
  original_content: string;
  proposed_content: string;
  reason: string;
  status: "pending" | "approved" | "rejected" | "cancelled";
  review_reason: string;
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
};

type SpeechSummary = {
  id: string;
  stage_name: string;
  content: string;
};

const statusLabel: Record<SpeechCorrectionRequest["status"], string> = {
  pending: "等待管理员审核",
  approved: "修正已批准",
  rejected: "修正未通过",
  cancelled: "申请已撤销",
};

export function SpeechCorrectionControl({
  roomCode,
  speech,
  request,
  onChanged,
}: {
  roomCode: string;
  speech: SpeechSummary;
  request?: SpeechCorrectionRequest;
  onChanged: (request: SpeechCorrectionRequest) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [proposedContent, setProposedContent] = useState(speech.content);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<"submit" | "cancel" | "">("");
  const [error, setError] = useState("");
  const operationKey = useRef<string | null>(null);
  const pending = request?.status === "pending";
  const changed = proposedContent.trim() !== speech.content.trim();

  function openEditor() {
    setProposedContent(speech.content);
    setReason("");
    setError("");
    operationKey.current = null;
    setEditing(true);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const normalizedContent = proposedContent.trim();
    const normalizedReason = reason.trim();
    if (!changed || normalizedContent.length < 2 || normalizedReason.length < 2) return;
    if (!window.confirm(`确认提交“${speech.stage_name}”的发言文字修正申请？原始记录会保留，管理员审核通过后才会更新。`)) return;
    setBusy("submit");
    setError("");
    try {
      operationKey.current ||= crypto.randomUUID();
      const result = await apiFetch<{ request: SpeechCorrectionRequest }>(
        `/api/rooms/${roomCode}/speeches/${speech.id}/correction-requests`,
        {
          method: "POST",
          headers: { "X-Idempotency-Key": operationKey.current },
          body: JSON.stringify({ proposed_content: normalizedContent, reason: normalizedReason }),
        },
      );
      operationKey.current = null;
      onChanged(result.request);
      setEditing(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "修正申请提交失败");
    } finally {
      setBusy("");
    }
  }

  async function cancel() {
    if (!request || !window.confirm("确认撤销这条待审核的发言修正申请？")) return;
    setBusy("cancel");
    setError("");
    try {
      const result = await apiFetch<{ request: SpeechCorrectionRequest }>(
        `/api/rooms/${roomCode}/speech-correction-requests/${request.id}/cancel`,
        { method: "POST", body: JSON.stringify({}) },
      );
      onChanged(result.request);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "暂时无法撤销申请");
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="speech-correction-control">
      {request && (
        <div className={`speech-correction-summary ${request.status}`} role={pending ? "status" : undefined}>
          <span>
            {request.status === "approved" ? <CheckCircle2 size={16} /> : request.status === "rejected" ? <XCircle size={16} /> : <FilePenLine size={16} />}
            <strong>{statusLabel[request.status]}</strong>
          </span>
          <small>申请理由：{request.reason}</small>
          <div className="correction-compare compact">
            <div><b>原始文字</b><p>{request.original_content}</p></div>
            <div><b>建议文字</b><p>{request.proposed_content}</p></div>
          </div>
          {request.review_reason && <small>审核说明：{request.review_reason}</small>}
          {pending && (
            <button type="button" className="button button-small button-secondary" disabled={Boolean(busy)} onClick={() => void cancel()}>
              {busy === "cancel" ? "正在撤销…" : "撤销申请"}
            </button>
          )}
        </div>
      )}
      {!pending && !editing && (
        <button type="button" className="button button-small button-secondary" onClick={openEditor}>
          {request ? <RotateCcw size={15} /> : <FilePenLine size={15} />}
          {request ? "再次申请修正" : "申请修正发言文字"}
        </button>
      )}
      {editing && (
        <form className="speech-correction-form" onSubmit={submit}>
          <div className="notice-box">只能修正你本人的真人发言文字。原始文字、录音和审核记录都会保留。</div>
          <div className="field">
            <label htmlFor={`speech-correction-content-${speech.id}`}>建议修正后的完整发言</label>
            <textarea
              id={`speech-correction-content-${speech.id}`}
              className="textarea"
              minLength={2}
              maxLength={20000}
              required
              autoFocus
              value={proposedContent}
              onChange={(event) => setProposedContent(event.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor={`speech-correction-reason-${speech.id}`}>修正原因</label>
            <textarea
              id={`speech-correction-reason-${speech.id}`}
              className="textarea"
              minLength={2}
              maxLength={500}
              required
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="例如：语音识别把关键结论中的“不应该”转写成了“应该”。"
            />
          </div>
          {error && <div className="error-box" role="alert">{error}</div>}
          <div className="dialog-actions">
            <button type="button" className="button button-small button-secondary" disabled={Boolean(busy)} onClick={() => setEditing(false)}>取消</button>
            <button className="button button-small" disabled={Boolean(busy) || !changed || proposedContent.trim().length < 2 || reason.trim().length < 2}>
              {busy === "submit" ? "正在提交…" : "提交修正申请"}
            </button>
          </div>
        </form>
      )}
      {error && !editing && <div className="error-box" role="alert">{error}</div>}
    </div>
  );
}
