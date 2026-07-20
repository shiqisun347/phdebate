"use client";

import Link from "next/link";
import { ArrowRight, Bot, CalendarClock, Eye, Radio, Sparkles, Trophy, Users } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "@/lib/api";
import type { Competition, Ranking } from "@/lib/types";
import { ParticipateDialog } from "@/components/participate-dialog";
import { LoadError } from "@/components/load-error";
import { PageLoading } from "@/components/page-loading";
import { competitionDisplayName, competitionDisplayNameFromStoredName, isPrimaryCompetition, PRIMARY_COMPETITION_NAME } from "@/lib/primary-competition";
import { roomCreationBlockedReason, seasonStatusLabel } from "@/lib/seasons";
import { roomStatusLabel } from "@/lib/status-labels";

type LiveRoom = { code: string; topic: string; status: string; competition_name: string; stage: string; paused_at?: string | null; updated_at?: string };
const LIVE_ROOM_REFRESH_MS = 15_000;

export default function HomePage() {
  const [competitions, setCompetitions] = useState<Competition[]>([]);
  const [liveRooms, setLiveRooms] = useState<LiveRoom[]>([]);
  const [rankings, setRankings] = useState<Ranking[]>([]);
  const [participate, setParticipate] = useState<Competition | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [liveRoomsError, setLiveRoomsError] = useState("");
  const [rankingsError, setRankingsError] = useState("");
  const [liveRoomsLoaded, setLiveRoomsLoaded] = useState(false);
  const [rankingsLoaded, setRankingsLoaded] = useState(false);
  const [liveRoomsLoading, setLiveRoomsLoading] = useState(false);
  const [lastLiveRoomsUpdatedAt, setLastLiveRoomsUpdatedAt] = useState<Date | null>(null);
  const [notice, setNotice] = useState("");
  const liveRoomsInFlight = useRef(false);
  const loadLiveRooms = useCallback(async () => {
    if (liveRoomsInFlight.current) return;
    liveRoomsInFlight.current = true;
    setLiveRoomsLoading(true);
    setLiveRoomsError("");
    try {
      const data = await apiFetch<{ items: LiveRoom[] }>("/api/live-rooms");
      setLiveRooms(data.items);
      setLastLiveRoomsUpdatedAt(new Date());
    } catch (err) {
      setLiveRoomsError(err instanceof Error ? err.message : "公开比赛载入失败");
    } finally {
      setLiveRoomsLoaded(true);
      setLiveRoomsLoading(false);
      liveRoomsInFlight.current = false;
    }
  }, []);
  const loadRankings = useCallback(async () => {
    setRankingsError("");
    try {
      const data = await apiFetch<{ items: Ranking[] }>("/api/rankings");
      setRankings(data.items);
    } catch (err) {
      setRankingsError(err instanceof Error ? err.message : "排行榜载入失败");
    } finally {
      setRankingsLoaded(true);
    }
  }, []);
  const load = useCallback(async () => {
    setError("");
    void loadLiveRooms();
    void loadRankings();
    try {
      const competitionResult = await apiFetch<{ items: Competition[] }>("/api/competitions");
      setCompetitions(competitionResult.items);
      setLoaded(true);
    } catch (err) { setError(err instanceof Error ? err.message : "赛事大厅载入失败"); }
  }, [loadLiveRooms, loadRankings]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") void loadLiveRooms();
    };
    const timer = window.setInterval(refreshWhenVisible, LIVE_ROOM_REFRESH_MS);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [loadLiveRooms]);
  useEffect(() => {
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    const closedRoom = url.searchParams.get("room_closed");
    const accessNotice = url.searchParams.get("notice");
    if (closedRoom && /^\d{6}$/.test(closedRoom)) {
      setNotice(`房间 #${closedRoom} 已关闭，无法继续进入。你可以创建新比赛或输入其他房间号。`);
      url.searchParams.delete("room_closed");
    } else if (accessNotice === "admin-required") {
      setNotice("该页面仅限系统管理员访问。你仍可参加赛事、加入房间或查看自己的比赛记录。");
      url.searchParams.delete("notice");
    } else return;
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  }, []);
  useEffect(() => {
    if (!competitions.length || typeof window === "undefined") return;
    const slug = new URLSearchParams(window.location.search).get("participate");
    if (!slug) return;
    const requested = competitions.find((item) => item.slug === slug);
    if (!requested) return;
    setParticipate(requested);
    window.history.replaceState(window.history.state, "", "/");
  }, [competitions]);
  if (!loaded && error) return <LoadError message={error} retry={() => void load()} />;
  if (!loaded) return <PageLoading label="正在载入赛事大厅…" />;
  const primaryCompetition = competitions.find(isPrimaryCompetition) || null;
  return (
    <div className="page-shell">
      {notice && <div className="warning-box home-return-notice" role="status">{notice}</div>}
      <section className="hero">
        <div>
          <span className="eyebrow">Human × AI Debate Arena</span>
          <h1>
            <span className="hero-title-line">{PRIMARY_COMPETITION_NAME}</span>
            <span className="gradient-text hero-title-line">参赛者自主组局，</span>
            <span className="gradient-text hero-title-line">系统自动开赛</span>
          </h1>
          <p className="hero-copy">登录后选择席位和辩题即可创建比赛。把六位房间号分享给队友；房主开始时，剩余席位由 AI 自动补齐，赛程与裁判由系统自动推进。</p>
          <div className="hero-actions">
            <button type="button" className="button" disabled={!primaryCompetition} onClick={() => primaryCompetition && setParticipate(primaryCompetition)}><Sparkles size={18} />创建 4v4 比赛</button>
            <Link href="/rankings" className="button button-secondary"><Trophy size={18} />查看排行榜</Link>
          </div>
        </div>
        <div className="hero-orbit" aria-hidden="true">
          <div className="orbit-ring one"><i className="orbit-dot" /></div>
          <div className="orbit-ring two" />
          <div className="hero-emblem"><span>辩</span></div>
        </div>
      </section>

      <section id="competitions">
        <div className="section-head"><div><span className="eyebrow">Competition</span><h2>选择赛事</h2><p>正式赛与训练赛都可由参赛者自主创建；认领人类席位后，其余空席由 AI 补齐。</p></div></div>
        <div className="competition-grid">
          {competitions.map((item) => (
            <article className="competition-card" key={item.id} style={{ "--card-accent": item.accent === "cyan" ? "#3bd8e8" : "#8c6cff" } as React.CSSProperties}>
              <div className="card-topline"><span className="badge"><Bot size={13} />{item.format}</span><span className="badge live"><Radio size={12} />{item.live_count} 场可观战</span></div>
              <h3>{competitionDisplayName(item)}</h3><p>{item.tagline}</p>
              <div className="card-meta"><span><Users size={14} /> {item.seat_count} 个席位</span><span><Trophy size={14} /> {item.ranked ? "赛季积分" : "训练模式"}</span></div>
              {item.ranked && <div className={`season-line ${roomCreationBlockedReason(item) ? "closed" : ""}`}><CalendarClock size={14} />{seasonStatusLabel(item.season)}</div>}
              <div className="card-actions"><button className="button" onClick={() => setParticipate(item)}>{roomCreationBlockedReason(item) ? "加入已有房间" : "创建比赛"}</button><Link href={`/competitions/${item.slug}`} className="button button-secondary">查看规则<ArrowRight size={15} /></Link></div>
            </article>
          ))}
          {!competitions.length && <div className="empty">赛事暂未开放，请稍后刷新。</div>}
        </div>
      </section>

      <section className="dashboard-grid">
        <div className="panel">
          <div className="panel-title"><h2>公开比赛</h2><span className="muted"><Eye size={15} /> {liveRoomsLoading ? "正在同步" : lastLiveRoomsUpdatedAt ? `${lastLiveRoomsUpdatedAt.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })} 更新` : "自动同步"}</span></div>
          <div className="live-list" aria-busy={liveRoomsLoading}>
            {liveRoomsError && liveRooms.length > 0 && <div className="warning-box" role="status"><span>实时状态同步失败，以下为最近一次成功结果。</span><button type="button" className="text-button" onClick={() => void loadLiveRooms()}>立即重试</button></div>}
            {liveRoomsError && !liveRooms.length
              ? <div className="empty" role="status"><p>公开比赛暂时未载入，不影响创建或加入比赛。</p><button type="button" className="button button-small button-secondary" onClick={() => void loadLiveRooms()}>重新载入公开比赛</button></div>
              : !liveRoomsLoaded
                ? <div className="empty" role="status">正在同步公开比赛…</div>
                : liveRooms.length ? liveRooms.map((room) => <Link href={`/rooms/${room.code}/watch`} className="live-row" key={room.code}><span className="room-code">#{room.code}</span><span className="row-main"><strong>{room.topic}</strong><small><span>{competitionDisplayNameFromStoredName(room.competition_name)}</span><span className={`live-room-status ${room.status}`}>{roomStatusLabel[room.status] || room.status}</span><span>{room.status === "paused" && room.paused_at ? `暂停于 ${new Date(room.paused_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })} · 等待房主恢复` : room.stage}</span></small></span><ArrowRight size={17} /></Link>) : <div className="empty">暂无可观战的公开比赛</div>}
          </div>
        </div>
        <div className="panel">
          <div className="panel-title"><h2>赛季领先者</h2><Link href="/rankings" className="muted">完整榜单</Link></div>
          <div className="ranking-list">
            {rankingsError && rankings.length > 0 && <div className="warning-box" role="status"><span>榜单同步失败，以下为最近一次成功结果。</span><button type="button" className="text-button" onClick={() => void loadRankings()}>立即重试</button></div>}
            {rankingsError && !rankings.length
              ? <div className="empty" role="status"><p>排行榜暂时未载入，参赛功能仍可正常使用。</p><button type="button" className="button button-small button-secondary" onClick={() => void loadRankings()}>重新载入排行榜</button></div>
              : !rankingsLoaded
                ? <div className="empty" role="status">正在同步赛季榜单…</div>
                : <>{rankings.slice(0, 6).map((item) => <div className="ranking-row" key={item.user_id}><span className="rank">{item.rank}</span><strong>{item.real_name}</strong><span>{item.points} 分</span><span className="muted">{item.wins} 胜</span></div>)}{!rankings.length && <div className="empty">完成首场积分赛后，你的名字会出现在这里</div>}</>}
          </div>
        </div>
      </section>
      {participate && <ParticipateDialog competition={participate} onClose={() => setParticipate(null)} />}
    </div>
  );
}
