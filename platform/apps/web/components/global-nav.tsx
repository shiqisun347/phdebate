"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { LogOut, Menu, Shield, UserRound, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { apiFetch } from "@/lib/api";
import { notifySessionChanged, useSession } from "@/lib/use-session";

export function GlobalNav() {
  const { user, loading } = useSession();
  const pathname = usePathname();
  const router = useRouter();
  const [menuOpen, setMenuOpen] = useState(false);
  const [logoutBusy, setLogoutBusy] = useState(false);
  const [logoutError, setLogoutError] = useState("");
  const menuToggleRef = useRef<HTMLButtonElement | null>(null);
  const isStage = pathname.includes("/debate") || pathname.includes("/watch");
  const isRoomLobby = /^\/rooms\/[^/]+\/lobby$/.test(pathname);
  const isLobbyPage = pathname === "/";
  const isRankingsPage = pathname === "/rankings";
  const isAdminPage = pathname === "/admin" || pathname.startsWith("/admin/");
  useEffect(() => setMenuOpen(false), [pathname]);
  useEffect(() => {
    if (!menuOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setMenuOpen(false);
      menuToggleRef.current?.focus();
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [menuOpen]);
  if (isStage || isRoomLobby) return null;
  async function logout() {
    if (logoutBusy) return;
    setLogoutBusy(true);
    setLogoutError("");
    try {
      await apiFetch("/api/auth/logout", { method: "POST" });
      notifySessionChanged();
      router.push("/");
      router.refresh();
    } catch (error) {
      setLogoutError(error instanceof Error ? error.message : "退出失败，请检查网络后重试。");
    } finally {
      setLogoutBusy(false);
    }
  }
  return (
    <header className="global-nav">
      <Link href="/" className="brand">
        <span className="brand-mark">辩</span>
        <span><strong>稷下辩论</strong><small>JIXIA DEBATE</small></span>
      </Link>
      <nav id="primary-navigation" className={menuOpen ? "open" : ""} aria-label="主要导航">
        <Link href="/" aria-current={isLobbyPage ? "page" : undefined} onClick={() => setMenuOpen(false)}>赛事大厅</Link>
        <Link href="/rankings" aria-current={isRankingsPage ? "page" : undefined} onClick={() => setMenuOpen(false)}>排行榜</Link>
        {user?.role === "system_admin" && <Link href="/admin" aria-current={isAdminPage ? "page" : undefined} onClick={() => setMenuOpen(false)}><Shield size={16} />系统管理</Link>}
      </nav>
      <button ref={menuToggleRef} type="button" className="icon-button mobile-nav-toggle" aria-label={menuOpen ? "关闭导航菜单" : "打开导航菜单"} aria-controls="primary-navigation" aria-expanded={menuOpen} onClick={() => setMenuOpen((value) => !value)}>{menuOpen ? <X size={18} /> : <Menu size={18} />}</button>
      <div className="nav-account">
        {loading ? <span className="muted">载入中</span> : user ? (
          <>
            <Link href="/me" className="user-chip" title={user.real_name}><UserRound size={17} /><span>{user.real_name}</span></Link>
            <button className="icon-button" type="button" disabled={logoutBusy} aria-busy={logoutBusy} onClick={() => void logout()} aria-label={logoutBusy ? "正在退出登录" : "退出登录"}><LogOut size={17} /></button>
          </>
        ) : (
          <>
            <Link href="/login" className="text-button">登录</Link>
            <Link href="/register" className="button button-small">注册参赛</Link>
          </>
        )}
      </div>
      {logoutError && <div className="nav-session-error" role="alert"><span>{logoutError}</span><button type="button" className="text-button" disabled={logoutBusy} onClick={() => void logout()}>重试退出</button></div>}
    </header>
  );
}
