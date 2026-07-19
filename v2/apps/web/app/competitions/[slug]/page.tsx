"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowRight, CalendarClock, Eye, Radio, ShieldCheck, Trophy, Users } from "lucide-react";
import { KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import { ParticipateDialog } from "@/components/participate-dialog";
import { LoadError } from "@/components/load-error";
import { apiFetch } from "@/lib/api";
import { competitionDisplayName } from "@/lib/primary-competition";
import { roomCreationBlockedReason, seasonStatusLabel } from "@/lib/seasons";
import { roomStatusLabel } from "@/lib/status-labels";
import type { Competition, Ranking } from "@/lib/types";

type Detail = { competition: Competition; leaderboard: Ranking[]; live_rooms: { code: string; topic: string; status: string; updated_at: string }[] };
type DetailTab = "intro" | "ranking" | "live" | "rules";

const detailTabs = [
  ["intro", "赛事介绍"],
  ["ranking", "排行榜"],
  ["live", "观战列表"],
  ["rules", "规则说明"],
] as const satisfies readonly (readonly [DetailTab, string])[];

export default function CompetitionDetailPage() {
  const { slug } = useParams<{ slug: string }>();
  const [data, setData] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<DetailTab>("intro");
  const tabRefs = useRef<Partial<Record<DetailTab, HTMLButtonElement | null>>>({});
  const [join, setJoin] = useState(false);
  const load = useCallback(async () => {
    setError("");
    try { setData(await apiFetch<Detail>(`/api/competitions/${slug}`)); }
    catch (err) { setError(err instanceof Error ? err.message : "赛事详情载入失败"); }
  }, [slug]);
  useEffect(() => { void load(); }, [load]);
  if (!data && error) return <LoadError message={error} retry={() => void load()} />;
  if (!data) return <div className="loading-screen">正在载入赛事…</div>;
  const item = data.competition;
  const creationBlockedReason = roomCreationBlockedReason(item);
  const moveTabFocus = (event: KeyboardEvent<HTMLButtonElement>, current: DetailTab) => {
    const currentIndex = detailTabs.findIndex(([key]) => key === current);
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % detailTabs.length;
    if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + detailTabs.length) % detailTabs.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = detailTabs.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    const nextTab = detailTabs[nextIndex][0];
    setTab(nextTab);
    tabRefs.current[nextTab]?.focus();
  };
  return (
    <div className="page-shell">
      <section className="detail-hero">
        <span className="eyebrow">Competition · {item.format}</span><h1>{competitionDisplayName(item)}</h1><p>{item.description}</p>
        <div className="detail-meta"><span className="badge"><Users size={13} />{item.seat_count} 个席位</span><span className="badge"><Trophy size={13} />{item.ranked ? "计入赛季排行" : "训练模式"}</span>{item.ranked && <span className={`badge ${creationBlockedReason ? "closed" : "live"}`}><CalendarClock size={13} />{seasonStatusLabel(item.season)}</span>}<span className="badge live"><Radio size={12} />{item.live_count} 场可观战</span></div>
        {creationBlockedReason && <div className="warning-box" style={{ marginBottom: 16 }}>{creationBlockedReason} 你仍可搜索并进入已有房间。</div>}
        <button className="button" onClick={() => setJoin(true)}>{creationBlockedReason ? "搜索已有房间" : "立即参赛"}<ArrowRight size={17} /></button>
      </section>
      <section style={{ paddingTop: 28 }}>
        <div className="tabs" role="tablist" aria-label="赛事详情栏目">{detailTabs.map(([key,label]) => <button ref={(node) => { tabRefs.current[key] = node; }} id={`competition-tab-${key}`} role="tab" aria-controls={`competition-panel-${key}`} aria-selected={tab===key} tabIndex={tab === key ? 0 : -1} className={`tab ${tab === key ? "active" : ""}`} onClick={() => setTab(key)} onKeyDown={(event) => moveTabFocus(event, key)} key={key}>{label}</button>)}</div>
        <div id={`competition-panel-${tab}`} role="tabpanel" aria-labelledby={`competition-tab-${tab}`} tabIndex={0}>
        {tab === "intro" && <div className="dashboard-grid"><article className="panel"><div className="panel-title"><h2>关于赛事</h2><ShieldCheck color="#8c6cff" /></div><p className="hero-copy" style={{ fontSize: 15 }}>{item.description}</p><h3>比赛方式</h3><p className="muted">登录用户创建房间并认领人类席位，其他玩家通过六位房间号加入。房主开始比赛后，所有空席由 AI 自动填充，系统按预设流程播报和推进，无需主持人。</p></article><aside className="panel"><div className="panel-title"><h3>可选辩题</h3></div><div className="history-list">{item.topics?.length ? item.topics.map((topic,index) => <div className="history-row" key={topic.id}><span className="badge">{String(index+1).padStart(2,'0')}</span><span>{topic.title}</span></div>) : <div className="empty">{item.allow_custom_topic ? "训练赛支持自定义辩题" : "管理员尚未添加可用辩题"}</div>}</div></aside></div>}
        {tab === "ranking" && <div className="panel"><div className="panel-title"><h2>赛季排行榜</h2><Trophy color="#ffcc6d" /></div><div className="table-wrap"><table><thead><tr><th>排名</th><th>选手</th><th>积分</th><th>胜 / 平 / 负</th><th>平均分</th><th>场次</th></tr></thead><tbody>{data.leaderboard.map((row) => <tr key={row.user_id}><td>#{row.rank}</td><td><strong>{row.real_name}</strong></td><td>{row.points}</td><td>{row.wins} / {row.draws} / {row.losses}</td><td>{row.average_score}</td><td>{row.matches}</td></tr>)}</tbody></table>{!data.leaderboard.length && <div className="empty">排行榜等待首场有效比赛</div>}</div></div>}
        {tab === "live" && <div className="panel"><div className="panel-title"><h2>可观战的比赛</h2><Eye /></div><div className="live-list">{data.live_rooms.map((room) => <Link href={`/rooms/${room.code}/watch`} className="live-row" key={room.code}><span className="room-code">#{room.code}</span><span className="row-main"><strong>{room.topic}</strong><small>{roomStatusLabel[room.status]||room.status}</small></span><ArrowRight size={16} /></Link>)}{!data.live_rooms.length && <div className="empty">暂无可观战的公开比赛</div>}</div></div>}
        {tab === "rules" && <article className="panel"><div className="panel-title"><h2>赛事规则</h2><ShieldCheck /></div><p className="hero-copy" style={{ whiteSpace: "pre-wrap", fontSize: 15 }}>{item.rules}</p><h3>通用约定</h3><ul className="muted" style={{ lineHeight: 2 }}><li>系统按固定阶段自动计时和切换。</li><li>只有当前轮次指定的人类辩手可以开启发言。</li><li>断线后保留席位 60 秒，超时由 AI 接替后续轮次。</li><li>禁止人身攻击、冒用身份和绕过客户端权限。</li></ul></article>}
        </div>
      </section>
      {join && <ParticipateDialog competition={item} onClose={() => setJoin(false)} />}
    </div>
  );
}
