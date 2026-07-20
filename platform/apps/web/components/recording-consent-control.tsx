"use client";

import { CheckCircle2, FileText, LoaderCircle, LockKeyhole, RotateCcw, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { ApiRequestError, apiFetch } from "@/lib/api";
import type { RecordingConsent } from "@/lib/types";

/**
 * Dormant backwards-compatibility renderer for archived room projections.
 * New competition rooms never receive this payload or API route.
 */
export function RecordingConsentControl({ consent, onChanged, compact = false, autoOpen = true }: {
  consent: RecordingConsent; onChanged: () => Promise<unknown>; compact?: boolean; autoOpen?: boolean;
}) {
  const policyKey = `${consent.policy?.id || "missing"}:${consent.policy?.version || 0}`;
  const [open, setOpen] = useState(autoOpen && consent.required && !consent.granted);
  const [accepted, setAccepted] = useState(false);
  const [busy, setBusy] = useState<"grant" | "revoke" | "refresh" | "">("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const dialogRef = useRef<HTMLElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const suppressAutoOpen = useRef(false);

  useEffect(() => {
    const suppressed = suppressAutoOpen.current;
    suppressAutoOpen.current = false;
    setAccepted(false); setError("");
    if (!suppressed) setNotice("");
    if (autoOpen && consent.required && !consent.granted && !suppressed) setOpen(true);
    if (suppressed) setOpen(false);
  }, [autoOpen, consent.granted, consent.required, policyKey]);

  useEffect(() => {
    if (!open) return;
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); setOpen(false); window.setTimeout(() => triggerRef.current?.focus(), 0); return; }
      if (event.key !== "Tab") return;
      const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])') || []);
      if (!focusable.length) return;
      const first = focusable[0]; const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open]);

  if (!consent.required) return null;
  const closeDialog = () => { setOpen(false); window.setTimeout(() => triggerRef.current?.focus(), 0); };
  async function refresh(message = "房间状态已刷新，请核对当前规则。") {
    setBusy("refresh"); setError("");
    try { await onChanged(); setNotice(message); }
    catch (err) { setError(err instanceof Error ? err.message : "刷新状态失败，请稍后重试。"); }
    finally { setBusy(""); }
  }
  async function decide(decision: "grant" | "revoke") {
    if (!consent.organization_id || !consent.policy) { setError("当前赛事录音规则尚未同步完整，请刷新后重试。"); return; }
    setBusy(decision); setError(""); setNotice("");
    try {
      await apiFetch(`/api/consents/organizations/${encodeURIComponent(consent.organization_id)}/recording/${decision}`, { method: "POST", body: "{}" });
      if (decision === "revoke") suppressAutoOpen.current = true;
      await onChanged(); setAccepted(false);
      setNotice(decision === "grant" ? "已记录你对当前版本录音规则的同意。" : "已撤回录音同意；新的录音将被阻止。正在进行的录音请先安全结束。");
      if (decision === "grant") closeDialog();
    } catch (err) {
      if (err instanceof ApiRequestError && (err.status === 404 || err.status === 409)) {
        setError(err.status === 404 ? "录音规则已撤下或兼容接口不可用，请刷新后重试。" : `规则状态已变化：${err.message}`);
        await onChanged().catch(() => undefined); setAccepted(false);
      } else setError(err instanceof Error ? err.message : "录音同意操作失败，请稍后重试。");
    } finally { setBusy(""); }
  }
  const trigger = consent.granted ? `录音同意：已同意 v${consent.policy?.version || "-"}` : "阅读并同意录音规则";
  return <div className={`recording-consent-control ${compact ? "compact" : ""}`}>
    <button ref={triggerRef} type="button" className={consent.granted ? "button button-small button-secondary" : "button button-small"} onClick={() => setOpen(true)}>{consent.granted ? <CheckCircle2 size={16} /> : <FileText size={16} />}{trigger}</button>
    {!compact && <span className={consent.granted ? "consent-granted" : "consent-required"}>{consent.granted ? "当前版本已同意，可进行新的录音。" : "未同意前不能准备或开始发言。"}</span>}
    {notice && !open && <span className="consent-granted" role="status">{notice}</span>}
    {open && <div className="dialog-overlay consent-dialog-overlay" role="presentation"><section ref={dialogRef} className="dialog consent-dialog" role="dialog" aria-modal="true" aria-labelledby="recording-consent-title" aria-describedby="recording-consent-help">
      <div className="dialog-head"><div><span className="eyebrow">Recording consent</span><h2 id="recording-consent-title">{consent.policy?.title || "赛事录音规则尚未发布"}</h2></div><button ref={closeRef} type="button" className="icon-button" aria-label="关闭录音规则" onClick={closeDialog}><X size={18} /></button></div>
      <p id="recording-consent-help" className="muted">{consent.policy ? `当前版本 v${consent.policy.version}。请完整阅读后自行决定；勾选框不会默认选中。` : "当前没有可供同意的有效录音规则。"}</p>
      {consent.policy ? <><div className="consent-policy-content" tabIndex={0}>{consent.policy.content}</div>{!consent.granted && <label className="check-row consent-check"><input type="checkbox" checked={accepted} onChange={(event) => setAccepted(event.target.checked)} /><span>我已阅读并同意当前 v{consent.policy.version} 录音规则，理解发言音频和文字将按规则处理。</span></label>}</> : <div className="warning-box"><LockKeyhole size={17} />当前赛事尚未发布有效录音规则，系统已阻止新的录音。请联系赛事管理员。</div>}
      {error && <div className="error-box" role="alert">{error}</div>}{notice && <div className="success-box" role="status">{notice}</div>}
      <div className="dialog-actions consent-actions">{!consent.granted && <button type="button" className="button button-secondary" disabled={Boolean(busy)} onClick={closeDialog}>暂不同意</button>}<button type="button" className="button button-secondary" disabled={Boolean(busy)} onClick={() => void refresh()}>{busy === "refresh" ? <LoaderCircle className="spin" size={16} /> : <RotateCcw size={16} />}刷新规则</button>{consent.granted ? <button type="button" className="button button-danger" disabled={Boolean(busy)} onClick={() => void decide("revoke")}>{busy === "revoke" && <LoaderCircle className="spin" size={16} />}撤回录音同意</button> : <button type="button" className="button button-green" disabled={!accepted || !consent.policy || !consent.organization_id || Boolean(busy)} onClick={() => void decide("grant")}>{busy === "grant" && <LoaderCircle className="spin" size={16} />}同意当前版本</button>}</div>
    </section></div>}
  </div>;
}
