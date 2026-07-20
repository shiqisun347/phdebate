"use client";

import { Medal, Trophy } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { LoadError } from "@/components/load-error";
import { apiFetch } from "@/lib/api";
import {
  competitionDisplayName,
  isPrimaryCompetition,
  PRIMARY_COMPETITION_SLUG,
} from "@/lib/primary-competition";
import type { Competition, Ranking, Season } from "@/lib/types";

export default function RankingsPage() {
  const [items, setItems] = useState<Ranking[]>([]);
  const [competitions, setCompetitions] = useState<Competition[]>([]);
  const [seasons, setSeasons] = useState<Season[]>([]);
  const [selected, setSelected] = useState("");
  const [selectedSeason, setSelectedSeason] = useState("");
  const [catalogLoaded, setCatalogLoaded] = useState(false);
  const [loadingRankings, setLoadingRankings] = useState(false);
  const [error, setError] = useState("");
  const lastLoadedRankingKey = useRef("");
  const rankingRequestSequence = useRef(0);

  const loadCatalog = useCallback(async () => {
    setError("");
    try {
      const [data, seasonData, rankingData] = await Promise.all([
        apiFetch<{ items: Competition[] }>("/api/competitions"),
        apiFetch<{ items: Season[] }>("/api/seasons"),
        apiFetch<{ items: Ranking[] }>(`/api/rankings?competition_slug=${PRIMARY_COMPETITION_SLUG}`),
      ]);
      setCompetitions(data.items);
      setSeasons(seasonData.items);
      const ranked = data.items.find(isPrimaryCompetition) || data.items.find((item) => item.ranked);
      const initialCompetition = ranked?.slug || "";
      const initialSeason = ranked?.season?.slug || seasonData.items.find((item) => item.is_open)?.slug || seasonData.items[0]?.slug || "";
      setSelected((current) => current || initialCompetition);
      setSelectedSeason((current) => current || initialSeason);
      setItems(rankingData.items);
      lastLoadedRankingKey.current = `${initialCompetition}:${initialSeason}`;
      setCatalogLoaded(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "赛事列表载入失败");
    }
  }, []);

  const loadRankings = useCallback(async () => {
    if (!selected || !selectedSeason) return;
    const key = `${selected}:${selectedSeason}`;
    if (lastLoadedRankingKey.current === key) return;
    const sequence = ++rankingRequestSequence.current;
    setLoadingRankings(true);
    setError("");
    try {
      const data = await apiFetch<{ items: Ranking[] }>(
        `/api/rankings?competition_slug=${selected}&season_slug=${selectedSeason}`,
      );
      if (sequence !== rankingRequestSequence.current) return;
      setItems(data.items);
      lastLoadedRankingKey.current = key;
    } catch (err) {
      if (sequence !== rankingRequestSequence.current) return;
      setError(err instanceof Error ? err.message : "排行榜载入失败");
    } finally {
      if (sequence === rankingRequestSequence.current) setLoadingRankings(false);
    }
  }, [selected, selectedSeason]);

  useEffect(() => {
    void loadCatalog();
  }, [loadCatalog]);
  useEffect(() => {
    void loadRankings();
  }, [loadRankings]);

  if (!catalogLoaded && error)
    return <LoadError message={error} retry={() => void loadCatalog()} />;
  if (!catalogLoaded)
    return <div className="loading-screen">正在载入排行榜…</div>;

  return (
    <div className="page-shell">
      <div className="section-head">
        <div>
          <span className="eyebrow">Leaderboard</span>
          <h1>赛季排行榜</h1>
          <p>每场胜负都成为你思辨成长的坐标。</p>
        </div>
        <div className="ranking-selectors">
          <select
            aria-label="选择积分赛事"
            className="select"
            value={selected}
            onChange={(event) => {
              const slug = event.target.value;
              setSelected(slug);
              const competition = competitions.find(
                (item) => item.slug === slug,
              );
              if (competition?.season?.slug)
                setSelectedSeason(competition.season.slug);
            }}
          >
            {competitions
              .filter((item) => item.ranked)
              .map((item) => (
                <option key={item.id} value={item.slug}>
                  {competitionDisplayName(item)}
                </option>
              ))}
          </select>
          <select
            aria-label="选择赛季"
            className="select"
            value={selectedSeason}
            onChange={(event) => setSelectedSeason(event.target.value)}
          >
            {seasons.map((item) => (
              <option key={item.id} value={item.slug}>
                {item.name}
                {item.is_open ? " · 进行中" : ""}
              </option>
            ))}
          </select>
        </div>
      </div>
      {error && (
        <div className="error-box" role="alert">
          {error}
          <button
            type="button"
            className="text-button"
            onClick={() => void loadRankings()}
          >
            重试
          </button>
        </div>
      )}
      <div className="panel">
        <div className="table-wrap" aria-busy={loadingRankings}>
          {items.length > 0 && (
            <table className="ranking-table">
              <thead>
                <tr>
                  <th>排名</th>
                  <th>辩手</th>
                  <th>赛季积分</th>
                  <th>战绩</th>
                  <th>平均评分</th>
                  <th>有效场次</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.user_id}>
                    <td>
                      {item.rank <= 3 ? (
                        <Medal
                          size={20}
                          color={
                            item.rank === 1
                              ? "#ffcc6d"
                              : item.rank === 2
                                ? "#c9d1e4"
                                : "#cf8d62"
                          }
                        />
                      ) : (
                        `#${item.rank}`
                      )}
                    </td>
                    <td>
                      <strong>{item.real_name}</strong>
                    </td>
                    <td>
                      <strong>{item.points}</strong>
                    </td>
                    <td>
                      {item.wins} 胜 · {item.draws} 平 · {item.losses} 负
                    </td>
                    <td>{item.average_score}</td>
                    <td>{item.matches}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {!loadingRankings && !items.length && (
            <div className="empty">
              <Trophy size={30} style={{ margin: "0 auto 10px" }} />
              暂无排名数据
            </div>
          )}
          {loadingRankings && (
            <div className="empty" role="status">
              正在更新榜单…
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
