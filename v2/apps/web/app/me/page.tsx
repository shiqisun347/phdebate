"use client";

import Link from "next/link";
import { ArrowLeft, ArrowRight, History, KeyRound, MonitorOff, Radio, ShieldCheck, UserRound } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { LoadError } from "@/components/load-error";
import { apiFetch } from "@/lib/api";
import { ratingReasonLabel, roomStatusLabel } from "@/lib/status-labels";
import type { User } from "@/lib/types";

type HistoryItem = {
  match_id: string;
  room_code: string;
  topic: string;
  status: string;
  winner: string | null;
  completed_at: string | null;
};

type MeData = {
  user: User;
  summary: { history_total: number; active_total: number; total_points: number };
  pagination: { page: number; page_size: number; total: number; pages: number };
  active_rooms: { code: string; topic: string; status: string; seat_key: string; occupant_type: string; can_resume: boolean; restore_request: { id: string; status: string; resolution_reason: string } | null }[];
  history: HistoryItem[];
  rating_changes: { match_id: string; points_delta: number; score: number; reason: string; source: "initial" | "correction"; created_at: string }[];
};

function resultLabel(item: HistoryItem): string {
  if (item.status === "terminated") return "已终止";
  if (item.winner === "aff") return "正方胜";
  if (item.winner === "neg") return "反方胜";
  if (item.winner === "draw") return "平局";
  return "待复核";
}

export default function MePage() {
  const router = useRouter();
  const [page, setPage] = useState(1);
  const [data, setData] = useState<MeData | null>(null);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [securityBusy, setSecurityBusy] = useState(false);
  const [restoreBusy, setRestoreBusy] = useState("");
  const restoreKeys = useRef<Record<string, string>>({});
  const loadSequence = useRef(0);
  const [securityFeedback, setSecurityFeedback] = useState<{ kind: "success" | "error"; message: string } | null>(null);
  const [passwordForm, setPasswordForm] = useState({ current_password: "", new_password: "", confirm_password: "" });

  const load = useCallback(async () => {
    const sequence = ++loadSequence.current;
    setRefreshing(true);
    setError("");
    try {
      const nextData = await apiFetch<MeData>(`/api/me?page=${page}&page_size=20`);
      if (sequence === loadSequence.current) setData(nextData);
    } catch (err) {
      if (sequence !== loadSequence.current) return;
      const message = err instanceof Error ? err.message : "个人记录载入失败";
      if (message.includes("登录") || message.includes("会话")) router.push("/login?next=/me");
      else setError(message);
    } finally {
      if (sequence === loadSequence.current) setRefreshing(false);
    }
  }, [page, router]);

  useEffect(() => {
    void load();
  }, [load]);

  async function changePassword(event: React.FormEvent) {
    event.preventDefault();
    setSecurityBusy(true);
    setSecurityFeedback(null);
    try {
      await apiFetch("/api/auth/password", { method: "POST", body: JSON.stringify(passwordForm) });
      setPasswordForm({ current_password: "", new_password: "", confirm_password: "" });
      setSecurityFeedback({ kind: "success", message: "密码已更新，其他设备的登录会话已全部撤销。" });
    } catch (err) {
      setSecurityFeedback({ kind: "error", message: err instanceof Error ? err.message : "密码更新失败" });
    } finally {
      setSecurityBusy(false);
    }
  }

  async function revokeOtherSessions() {
    if (!confirm("确定退出此账号在其他设备上的所有登录吗？当前设备会保持登录。")) return;
    setSecurityBusy(true);
    setSecurityFeedback(null);
    try {
      const result = await apiFetch<{ revoked: number }>("/api/auth/sessions/revoke-others", { method: "POST", body: "{}" });
      setSecurityFeedback({ kind: "success", message: result.revoked ? `已退出 ${result.revoked} 个其他设备。` : "当前没有其他已登录设备。" });
    } catch (err) {
      setSecurityFeedback({ kind: "error", message: err instanceof Error ? err.message : "设备会话撤销失败" });
    } finally {
      setSecurityBusy(false);
    }
  }

  async function requestRestore(code: string) {
    setRestoreBusy(code);
    setError("");
    try {
      const key = restoreKeys.current[code] || crypto.randomUUID();
      restoreKeys.current[code] = key;
      await apiFetch(`/api/rooms/${code}/seat-restore-requests`, {
        method: "POST",
        headers: { "X-Idempotency-Key": key },
        body: "{}",
      });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "恢复申请提交失败");
    } finally {
      setRestoreBusy("");
    }
  }

  async function cancelRestore(code: string, requestId: string) {
    setRestoreBusy(code);
    setError("");
    try {
      await apiFetch(`/api/rooms/${code}/seat-restore-requests/${requestId}/cancel`, { method: "POST", body: "{}" });
      delete restoreKeys.current[code];
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "撤销恢复申请失败");
    } finally {
      setRestoreBusy("");
    }
  }

  if (!data && error) return <LoadError message={error} retry={() => void load()} />;
  if (!data) return <div className="loading-screen">正在载入个人记录…</div>;

  return (
    <div className="page-shell">
      <div className="section-head">
        <div>
          <span className="eyebrow">My Debate</span>
          <h1>{data.user.real_name}的辩论档案</h1>
          <p>@{data.user.account} · 所有比赛身份均绑定至此账号</p>
        </div>
      </div>

      {error && <div className="error-box" role="alert">{error}</div>}
      <div className="stats-grid">
        <div className="stat-card"><span className="muted">累计积分</span><strong>{data.summary.total_points}</strong></div>
        <div className="stat-card"><span className="muted">历史比赛</span><strong>{data.summary.history_total}</strong></div>
        <div className="stat-card"><span className="muted">进行中</span><strong>{data.summary.active_total}</strong></div>
        <div className="stat-card"><span className="muted">账号角色</span><strong style={{ fontSize: 18 }}>{data.user.role === "system_admin" ? "系统管理员" : "认证辩手"}</strong></div>
      </div>

      <div className="dashboard-grid">
        <section className="panel">
          <div className="panel-title"><h2><Radio size={19} />继续比赛</h2></div>
          <div className="history-list">
            {data.active_rooms.map((room) => (
              <div className="active-room-entry" key={room.code}>
                <Link href={`/rooms/${room.code}/${room.can_resume ? room.status === "lobby" ? "lobby" : "debate" : "watch"}`} className="live-row">
                  <span className="room-code">#{room.code}</span>
                  <span className="row-main"><strong>{room.topic}</strong><small>{room.can_resume ? roomStatusLabel[room.status] || room.status : room.restore_request?.status === "pending" ? "AI 已接替 · 恢复申请等待审批" : "AI 已接替 · 返回观战或申请恢复"}</small></span>
                  <ArrowRight size={17} />
                </Link>
                {!room.can_resume && (room.restore_request?.status === "pending" ? (
                  <button type="button" className="button button-small button-secondary" aria-busy={restoreBusy === room.code} disabled={restoreBusy === room.code} onClick={() => void cancelRestore(room.code, room.restore_request!.id)}>{restoreBusy === room.code ? "正在撤销…" : "撤销恢复申请"}</button>
                ) : (
                  <button type="button" className="button button-small button-green" aria-busy={restoreBusy === room.code} disabled={restoreBusy === room.code} onClick={() => void requestRestore(room.code)}>{restoreBusy === room.code ? "正在提交…" : "申请恢复真人席位"}</button>
                ))}
              </div>
            ))}
            {!data.active_rooms.length && (
              <div className="empty empty-guidance">
                <span>当前没有需要继续的比赛</span>
                <Link className="button button-small button-secondary" href="/#competitions">选择赛事</Link>
              </div>
            )}
          </div>
        </section>

        <aside className="panel">
          <div className="panel-title"><h2><UserRound size={19} />最近积分</h2></div>
          <div className="history-list">
            {data.rating_changes.slice(0, 8).map((item, index) => (
              <div className="history-row" key={`${item.match_id}-${index}`} style={{ gridTemplateColumns: "1fr auto" }}>
                <span><strong>{ratingReasonLabel(item.reason)}</strong><small className="muted">评分 {item.score}{item.source === "correction" ? " · 保留原始结算" : ""}</small></span>
                <strong style={{ color: item.points_delta > 0 ? "#40df9c" : item.points_delta < 0 ? "#ff8092" : "#9aa6c2" }}>{item.points_delta > 0 ? "+" : ""}{item.points_delta}</strong>
              </div>
            ))}
            {!data.rating_changes.length && <div className="empty">暂无积分记录</div>}
          </div>
        </aside>
      </div>

      <section className="panel" style={{ marginTop: 18 }}>
        <div className="panel-title">
          <h2><History size={19} />历史比赛</h2>
          <span className="badge" role="status" aria-live="polite">{refreshing ? "正在更新…" : `第 ${data.pagination.page} / ${data.pagination.pages} 页`}</span>
        </div>
        <div className="match-history" aria-busy={refreshing}>
          <div className="match-history-head" aria-hidden="true">
            <span>房间</span><span>辩题</span><span>状态</span><span>结果</span><span>操作</span>
          </div>
          {data.history.length > 0 && <ul className="match-history-items" aria-label="历史比赛列表">
            {data.history.map((item) => (
              <li className="match-history-row" key={item.match_id}>
                <span className="room-code">#{item.room_code}</span>
                <div className="match-history-topic">
                  <strong>{item.topic}</strong>
                  {item.completed_at && <small>{new Date(item.completed_at).toLocaleDateString("zh-CN")}</small>}
                </div>
                <span className="match-history-state">{roomStatusLabel[item.status] || item.status}</span>
                <span className="match-history-result">{resultLabel(item)}</span>
                <Link className="match-history-action" href={`/rooms/${item.room_code}/result`} aria-label={`查看房间 ${item.room_code} 的比赛记录`}>
                  查看记录<ArrowRight size={16} aria-hidden="true" />
                </Link>
              </li>
            ))}
          </ul>}
          {!data.history.length && (
            <div className="empty empty-guidance">
              <span>完成比赛后，逐字稿、结果与积分变化会保存在这里</span>
              <Link className="button button-small button-secondary" href="/#competitions">参加第一场比赛</Link>
            </div>
          )}
        </div>
        {data.pagination.pages > 1 && (
          <div className="hero-actions" style={{ justifyContent: "flex-end", marginTop: 16 }}>
            <button className="button button-secondary" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}><ArrowLeft size={16} />上一页</button>
            <button className="button button-secondary" disabled={page >= data.pagination.pages} onClick={() => setPage((value) => value + 1)}>下一页<ArrowRight size={16} /></button>
          </div>
        )}
      </section>

      <section className="panel" style={{ marginTop: 18 }}>
        <div className="panel-title"><h2><ShieldCheck size={19} />账号安全</h2><span className="muted">修改密码后会自动退出其他设备</span></div>
        {securityFeedback && <div className={securityFeedback.kind === "error" ? "error-box" : "notice-box"} role={securityFeedback.kind === "error" ? "alert" : "status"}>{securityFeedback.message}</div>}
        <div className="dashboard-grid">
          <form onSubmit={changePassword}>
            <div className="field"><label htmlFor="current-password">当前密码</label><input id="current-password" className="input" type="password" autoComplete="current-password" minLength={8} maxLength={128} required value={passwordForm.current_password} onChange={(event) => setPasswordForm((value) => ({ ...value, current_password: event.target.value }))} /></div>
            <div className="password-grid"><div className="field"><label htmlFor="new-password">新密码</label><input id="new-password" className="input" type="password" autoComplete="new-password" minLength={8} maxLength={128} required value={passwordForm.new_password} onChange={(event) => setPasswordForm((value) => ({ ...value, new_password: event.target.value }))} /></div><div className="field"><label htmlFor="confirm-new-password">确认新密码</label><input id="confirm-new-password" className="input" type="password" autoComplete="new-password" minLength={8} maxLength={128} required value={passwordForm.confirm_password} onChange={(event) => setPasswordForm((value) => ({ ...value, confirm_password: event.target.value }))} /></div></div>
            <button className="button" disabled={securityBusy || passwordForm.new_password.length < 8 || passwordForm.new_password !== passwordForm.confirm_password}><KeyRound size={17} />{securityBusy ? "处理中…" : "更新密码"}</button>
          </form>
          <div className="security-device-card"><MonitorOff size={28} /><div><h3>其他登录设备</h3><p className="muted">如果曾在公共电脑登录，或怀疑账号被他人使用，可立即撤销除当前浏览器外的所有会话。</p></div><button type="button" className="button button-secondary" disabled={securityBusy} onClick={() => void revokeOtherSessions()}><MonitorOff size={17} />退出其他设备</button></div>
        </div>
      </section>
    </div>
  );
}
