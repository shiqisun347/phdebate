"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { FormEvent, useEffect, useRef, useState } from "react";
import { apiFetch, safeNextPath } from "@/lib/api";
import { notifySessionChanged, useSession } from "@/lib/use-session";

export function AuthForm({ mode }: { mode: "login" | "register" }) {
  const router = useRouter();
  const params = useSearchParams();
  const { user, loading: sessionLoading } = useSession();
  const [account, setAccount] = useState("");
  const [realName, setRealName] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [confirmError, setConfirmError] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submittedAuth = useRef(false);
  const nextPath = safeNextPath(params.get("next"));
  const authenticatedDestination = nextPath === "/" ? "/me" : nextPath;
  const confirmationMismatch = mode === "register" && confirm.length > 0 && password !== confirm;
  const alternateAuthHref = `${mode === "login" ? "/register" : "/login"}?next=${encodeURIComponent(nextPath)}`;
  useEffect(() => {
    if (!sessionLoading && user && !submittedAuth.current) router.replace(authenticatedDestination);
  }, [authenticatedDestination, router, sessionLoading, user]);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // Safari autofill and some assisted-input paths can update the visible input
    // without firing the React change event. Read the submitted DOM values so the
    // request always matches what the user sees in the form.
    const fields = new FormData(event.currentTarget);
    const submittedAccount = String(fields.get("account") ?? account);
    const submittedRealName = String(fields.get("real_name") ?? realName);
    const submittedPassword = String(fields.get("password") ?? password);
    const submittedConfirm = String(fields.get("confirm_password") ?? confirm);
    if (mode === "register" && submittedPassword !== submittedConfirm) {
      setConfirmError("两次输入的密码不一致。");
      return;
    }
    submittedAuth.current = true;
    setBusy(true); setError("");
    try {
      await apiFetch(`/api/auth/${mode}`, {
        method: "POST",
        body: JSON.stringify(mode === "login"
          ? { account: submittedAccount, password: submittedPassword }
          : {
              account: submittedAccount,
              real_name: submittedRealName,
              password: submittedPassword,
              confirm_password: submittedConfirm,
            }),
      });
      notifySessionChanged();
      router.push(nextPath);
      router.refresh();
    } catch (err) {
      submittedAuth.current = false;
      setError(err instanceof Error ? err.message : "操作失败");
    }
    finally { setBusy(false); }
  }
  if (sessionLoading || user) {
    return (
      <div className={`success-box auth-session-state ${mode}`} role="status">
        <strong>{sessionLoading ? "正在确认登录状态…" : "你已登录"}</strong>
        <span>{sessionLoading ? "确认后再显示账号表单。" : "正在返回你的辩论档案。"}</span>
      </div>
    );
  }
  return (
    <form className="form-stack" onSubmit={submit}>
      <div className="field"><label htmlFor="auth-account">登录账号</label><input id="auth-account" name="account" className="input" value={account} onChange={(e) => setAccount(e.target.value)} placeholder="字母、数字、点或下划线" autoComplete="username" autoCapitalize="none" spellCheck={false} required /></div>
      {mode === "register" && <div className="field"><label htmlFor="auth-real-name">真实姓名</label><input id="auth-real-name" name="real_name" className="input" value={realName} onChange={(e) => setRealName(e.target.value)} placeholder="比赛和排行榜将展示此姓名" autoComplete="name" required /></div>}
      <div className="field"><label htmlFor="auth-password">密码</label><input id="auth-password" name="password" className="input" type="password" value={password} onChange={(e) => { setPassword(e.target.value); setConfirmError(""); }} autoComplete={mode === "login" ? "current-password" : "new-password"} autoCapitalize="none" spellCheck={false} minLength={8} required aria-describedby={mode === "register" ? "auth-password-requirement" : undefined} />{mode === "register" && <small id="auth-password-requirement" className="field-hint">至少 8 个字符，请勿使用与登录账号相同的密码。</small>}</div>
      {mode === "register" && <div className="field"><label htmlFor="auth-confirm-password">确认密码</label><input id="auth-confirm-password" name="confirm_password" className="input" type="password" value={confirm} onChange={(e) => { setConfirm(e.target.value); setConfirmError(""); }} autoComplete="new-password" autoCapitalize="none" spellCheck={false} minLength={8} required aria-invalid={confirmationMismatch || Boolean(confirmError)} aria-describedby={(confirmationMismatch || confirmError) ? "auth-confirm-password-error" : undefined} />{(confirmationMismatch || confirmError) && <small id="auth-confirm-password-error" className="field-error" role="alert">{confirmError || "两次输入的密码不一致。"}</small>}</div>}
      {error && <div className="error-box" role="alert">{error}</div>}
      <button type="submit" className="button" disabled={busy || confirmationMismatch}>{busy ? "请稍候…" : mode === "login" ? "登录" : "注册并进入赛场"}</button>
      <p className="auth-switch muted">{mode === "login" ? <>还没有账号？<Link href={alternateAuthHref}>立即注册</Link></> : <>已有账号？<Link href={alternateAuthHref}>返回登录</Link></>}</p>
    </form>
  );
}
