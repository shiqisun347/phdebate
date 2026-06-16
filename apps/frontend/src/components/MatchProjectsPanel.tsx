import { useCallback, useEffect, useState } from "react";
import { createMatch, deleteMatch, listMatches, switchMatch } from "../api/client";
import type { MatchListEntry } from "../types/contracts";

function gotoMatch(matchId: string): void {
  const params = new URLSearchParams(window.location.search);
  params.set("match_id", matchId);
  window.location.href = `${window.location.pathname}?${params.toString()}`;
}

function statusLabel(status: string): string {
  const map: Record<string, string> = {
    draft: "草稿",
    ready: "就绪",
    running: "进行中",
    paused: "已暂停",
    intervention: "干预中",
    finished: "已结束",
    archived: "已归档"
  };
  return map[status] ?? status;
}

/**
 * 需求 3：比赛管理 = 项目化的增删改查 + 切换。每个比赛是一个项目，
 * 可新建、切换为当前比赛、删除（非当前）。切换会重定向到该比赛。
 */
export function MatchProjectsPanel({ matchId }: { matchId: string }) {
  const [matches, setMatches] = useState<MatchListEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [topic, setTopic] = useState("");

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listMatches();
      setMatches(data.matches);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载比赛列表失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function handleCreate() {
    setBusy("create");
    setError(null);
    try {
      const result = await createMatch({ title: title.trim(), topic: topic.trim() });
      gotoMatch(result.match_id); // 新建后即切换为当前比赛
    } catch (err) {
      setError(err instanceof Error ? err.message : "新建比赛失败");
      setBusy(null);
    }
  }

  async function handleSwitch(id: string) {
    setBusy(id);
    setError(null);
    try {
      await switchMatch(id);
      gotoMatch(id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "切换比赛失败");
      setBusy(null);
    }
  }

  async function handleDelete(id: string) {
    if (!window.confirm("确认删除该比赛项目？该比赛的发言、事件、投票等数据将被清除，且不可恢复。")) return;
    setBusy(id);
    setError(null);
    try {
      const data = await deleteMatch(id);
      setMatches(data.matches);
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除比赛失败");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="match-projects">
      <div className="match-create-row">
        <input value={title} placeholder="新比赛名称（可选，继承当前配置）" onChange={(e) => setTitle(e.target.value)} />
        <input value={topic} placeholder="辩题（可选）" onChange={(e) => setTopic(e.target.value)} />
        <button type="button" disabled={busy === "create"} onClick={() => void handleCreate()}>
          {busy === "create" ? "新建中…" : "+ 新建比赛"}
        </button>
      </div>
      {error && <div className="match-projects-error">{error}</div>}
      <div className="match-project-list">
        {matches.map((entry) => {
          const isActive = entry.id === matchId || entry.active;
          return (
            <div key={entry.id} className={`match-project-row ${isActive ? "active" : ""}`}>
              <div className="match-project-info">
                <strong>{entry.title || entry.id}</strong>
                <span>{entry.topic || "—"}</span>
                <em>
                  {entry.id} · {statusLabel(entry.status)}
                  {entry.created_at ? ` · ${new Date(entry.created_at).toLocaleString("zh-CN")}` : ""}
                </em>
              </div>
              <div className="match-project-actions">
                {isActive ? (
                  <span className="match-project-badge">当前比赛</span>
                ) : (
                  <>
                    <button type="button" disabled={busy === entry.id} onClick={() => void handleSwitch(entry.id)}>切换</button>
                    <button type="button" className="danger" disabled={busy === entry.id} onClick={() => void handleDelete(entry.id)}>删除</button>
                  </>
                )}
              </div>
            </div>
          );
        })}
        {!matches.length && !loading && <p className="muted-line">暂无比赛项目。</p>}
        {loading && <p className="muted-line">加载中…</p>}
      </div>
    </div>
  );
}
