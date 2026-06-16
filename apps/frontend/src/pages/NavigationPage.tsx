import { ExternalLink, Mic2, Monitor, Settings2, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { getCurrentMatchSummary } from "../api/client";
import { StatusPill } from "../components/StatusPill";
import type { CurrentMatchSummary, MatchStatus, ScreenScene } from "../types/contracts";

function linkWithToken(path: string, token: string | null) {
  const params = new URLSearchParams();
  if (token) params.set("token", token);
  const query = params.toString();
  return query ? `${path}?${query}` : path;
}

export function NavigationPage() {
  const params = new URLSearchParams(window.location.search);
  const token = params.get("token");
  const origin = window.location.origin;
  const [summary, setSummary] = useState<CurrentMatchSummary | null>(null);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const primaryLinks = [
    {
      href: linkWithToken("/admin", token),
      title: "技术后台",
      detail: "系统设置 / 现场控场 / 权限与审计",
      icon: Settings2
    },
    {
      href: linkWithToken("/screen", token),
      title: "大屏",
      detail: "现场投影 / 实况字幕 / 赛后结果",
      icon: Monitor
    },
    {
      href: linkWithToken("/console", token),
      title: "辩手端",
      detail: "身份选择 / 硬件测试 / 发言提醒",
      icon: Mic2
    }
  ];

  useEffect(() => {
    let cancelled = false;
    async function loadSummary() {
      try {
        const data = await getCurrentMatchSummary();
        if (cancelled) return;
        setSummary(data);
        setSummaryError(null);
      } catch (err) {
        if (!cancelled) setSummaryError(err instanceof Error ? err.message : "当前比赛摘要加载失败");
      }
    }
    void loadSummary();
    const timer = window.setInterval(loadSummary, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  return (
    <main className="nav-shell">
      <section className="nav-hero">
        <div>
          <span className="nav-kicker"><ShieldCheck size={16} />PhDebate 控制系统</span>
          <h1>现场导航</h1>
          <p>{origin}</p>
        </div>
        <div className="nav-match-card">
          <span>当前比赛</span>
          <strong>{summary?.title ?? "正在读取当前比赛"}</strong>
          <p>{summary?.topic ?? summaryError ?? "请稍候，正在同步服务器状态。"}</p>
          <div className="nav-match-meta">
            <StatusPill tone={summary?.status === "running" ? "green" : summary?.status === "paused" ? "gold" : summaryError ? "red" : "blue"}>
              {summary ? matchStatusLabel(summary.status) : summaryError ? "连接异常" : "同步中"}
            </StatusPill>
            {summary && <StatusPill tone="blue">{screenSceneLabel(summary.screen_scene)}</StatusPill>}
          </div>
        </div>
      </section>

      <section className="nav-grid primary">
        {primaryLinks.map((item) => {
          const Icon = item.icon;
          return (
            <a className="nav-card" href={item.href} key={item.href}>
              <span className="nav-card-icon"><Icon size={22} /></span>
              <strong>{item.title}</strong>
              <em>{item.detail}</em>
              <ExternalLink size={16} />
            </a>
          );
        })}
      </section>
    </main>
  );
}

function matchStatusLabel(status: MatchStatus): string {
  if (status === "draft") return "草稿";
  if (status === "ready") return "待开始";
  if (status === "running") return "进行中";
  if (status === "paused") return "已暂停";
  if (status === "intervention") return "应急中";
  if (status === "finished") return "已结束";
  if (status === "archived") return "已归档";
  return "未知状态";
}

function screenSceneLabel(scene: ScreenScene): string {
  if (scene === "idle" || scene === "teams") return "候场";
  if (scene === "live" || scene === "opening") return "实况";
  if (scene === "paused") return "暂停";
  if (scene === "judge_commentary" || scene === "intermission") return "评委点评";
  if (scene === "judge_result" || scene === "result") return "评委结果";
  if (scene === "audience_result") return "学生结果";
  return "大屏";
}
