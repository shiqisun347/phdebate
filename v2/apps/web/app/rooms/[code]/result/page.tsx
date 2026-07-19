"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import {
  ArrowLeft,
  Archive,
  Award,
  Clock3,
  History,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  Trophy,
} from "lucide-react";
import {
  type SyntheticEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import { LoadError } from "@/components/load-error";
import { ApiRequestError, apiFetch, apiOrigin } from "@/lib/api";
import {
  matchEventDetail,
  matchEventLabel,
  type MatchTimelineEvent,
} from "@/lib/match-events";
import { competitionDisplayName } from "@/lib/primary-competition";
import { ratingReasonLabel, resultStatusLabel } from "@/lib/status-labels";
import type { Room } from "@/lib/types";
import { useRoom } from "@/lib/use-room";

export type Result = {
  room: Room;
  match: { id: string; status: string; winner: string | null; reason: string };
  scorecard: {
    status: string;
    winner: string | null;
    affirmative_score: number | null;
    negative_score: number | null;
    individual_scores: Record<string, number>;
    reasoning: string;
  } | null;
  speeches: {
    id: string;
    seat_key: string;
    stage_key: string;
    stage_name: string;
    speaker_type: "human" | "ai";
    status: string;
    content: string;
    audio_url: string;
    duration_seconds: number;
    created_at: string;
  }[];
  rating_changes: {
    user_id?: string;
    display_name: string;
    points_delta: number;
    score: number;
    reason: string;
    source: "initial" | "correction";
  }[];
  events: MatchTimelineEvent[];
  event_pagination: {
    page: number;
    page_size: number;
    total: number;
    pages: number;
    next_before_seq: number | null;
    has_more: boolean;
  };
};

const speechStatusLabel: Record<string, string> = {
  completed: "正常完成",
  timed_out: "发言超时",
  interrupted: "已中断",
  failed: "服务失败，未播出",
  failed_retried: "服务失败，已重试",
  speaking: "生成或发言中",
  synthesizing: "语音合成中",
  playing: "正在播放",
};

export default function ResultPage() {
  const { code } = useParams<{ code: string }>();
  const router = useRouter();
  const [data, setData] = useState<Result | null>(null);
  const [error, setError] = useState("");
  const [loadingEvents, setLoadingEvents] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [rematchBusy, setRematchBusy] = useState(false);
  const rematchKey = useRef<string | null>(null);
  const audioElements = useRef(new Map<string, HTMLAudioElement>());
  const { room: liveRoom, error: connectionError, reconnect } = useRoom(code);

  const stopAudio = useCallback((except?: HTMLAudioElement, reset = false) => {
    audioElements.current.forEach((audio) => {
      if (audio === except) return;
      audio.pause();
      if (reset) audio.currentTime = 0;
    });
  }, []);

  const handleAudioPlay = useCallback(
    (event: SyntheticEvent<HTMLAudioElement>) => {
      stopAudio(event.currentTarget);
    },
    [stopAudio],
  );

  const load = useCallback(async () => {
    setError("");
    try {
      setData(
        await apiFetch<Result>(
          `/api/rooms/${code}/result?event_page=1&event_page_size=50`,
        ),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "比赛结果载入失败");
    }
  }, [code]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(
    () => () => {
      stopAudio(undefined, true);
    },
    [code, stopAudio],
  );

  useEffect(() => {
    const currentSpeechIds = new Set(
      data?.speeches
        .filter((speech) => speech.audio_url)
        .map((speech) => speech.id),
    );
    audioElements.current.forEach((_audio, speechId) => {
      if (!currentSpeechIds.has(speechId))
        audioElements.current.delete(speechId);
    });
  }, [data?.speeches]);

  useEffect(() => {
    if (data && liveRoom && liveRoom.seq > data.room.seq) void load();
  }, [data, liveRoom, load]);

  async function refreshResult() {
    setRefreshing(true);
    try {
      await load();
      reconnect();
    } finally {
      setRefreshing(false);
    }
  }

  async function createRematch() {
    if (rematchBusy) return;
    setRematchBusy(true);
    setError("");
    if (!rematchKey.current) rematchKey.current = crypto.randomUUID();
    try {
      const result = await apiFetch<{ room: Room }>(`/api/rooms/${code}/rematch`, {
        method: "POST",
        headers: { "X-Idempotency-Key": rematchKey.current },
        body: "{}",
      });
      rematchKey.current = null;
      router.push(`/rooms/${result.room.code}/lobby`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "再次比赛创建失败");
    } finally {
      setRematchBusy(false);
    }
  }

  async function loadOlderEvents() {
    if (
      !data ||
      !data.event_pagination.has_more ||
      data.event_pagination.next_before_seq === null
    )
      return;
    setLoadingEvents(true);
    setError("");
    try {
      const cursorPath = `/api/rooms/${code}/result?event_page=${data.event_pagination.page + 1}&event_page_size=${data.event_pagination.page_size}&event_before_seq=${data.event_pagination.next_before_seq}`;
      let older: Result;
      try {
        older = await apiFetch<Result>(cursorPath);
      } catch (err) {
        if (!(err instanceof TypeError) && !(err instanceof ApiRequestError && err.status === 0)) throw err;
        older = await apiFetch<Result>(
          `/api/rooms/${code}/result?event_page=${data.event_pagination.page + 1}&event_page_size=${data.event_pagination.page_size}`,
        );
      }
      setData((current) =>
        current
          ? {
              ...current,
              events: [
                ...new Map(
                  [...older.events, ...current.events].map((item) => [
                    item.seq,
                    item,
                  ]),
                ).values(),
              ].sort((left, right) => left.seq - right.seq),
              event_pagination: older.event_pagination,
            }
          : older,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "更早的比赛事件载入失败");
    } finally {
      setLoadingEvents(false);
    }
  }

  if (!data && error)
    return <LoadError message={error} retry={() => void load()} />;
  if (!data) return <div className="loading-screen">正在整理比赛结果…</div>;

  const resultIsFinal = ["completed", "review_required", "terminated"].includes(
    data.match.status,
  );
  const winner =
    data.match.status === "terminated"
      ? "比赛已终止"
      : data.match.winner === "aff"
        ? "正方胜利"
        : data.match.winner === "neg"
          ? "反方胜利"
          : data.match.winner === "draw"
            ? "双方战平"
            : data.match.status === "review_required"
              ? "等待管理员复核"
              : data.room.status === "judging"
                ? "裁判正在评议"
                : data.room.status === "paused"
                  ? "比赛已暂停"
                  : data.room.status === "preparing"
                    ? "比赛正在准备"
                    : data.room.status === "lobby"
                      ? "比赛尚未开始"
                      : "比赛正在进行";

  return (
    <div className="page-shell">
      <section className="result-hero panel">
        <Trophy size={48} color="#ffcc6d" />
        <span className="eyebrow">比赛结果 · #{code}</span>
        <h1>{winner}</h1>
        <p>{data.room.topic}</p>
        {data.scorecard?.status === "approved" &&
          data.scorecard.affirmative_score !== null &&
          data.scorecard.negative_score !== null && (
            <div className="score-versus">
              <strong>{data.scorecard.affirmative_score}</strong>
              <span>正方&nbsp;&nbsp;VS&nbsp;&nbsp;反方</span>
              <strong>{data.scorecard.negative_score}</strong>
            </div>
          )}
        <div className="hero-actions">
          <button
            type="button"
            className="button button-secondary"
            disabled={refreshing}
            onClick={() => void refreshResult()}
          >
            {refreshing ? <LoaderCircle className="spin" /> : <RefreshCw />}{" "}
            {refreshing ? "刷新中…" : "刷新赛果"}
          </button>
          <Link href="/me" className="button">
            <Award />
            返回个人中心
          </Link>
          {resultIsFinal && data.room.my_seat && (
            <button
              type="button"
              className="button"
              disabled={rematchBusy}
              onClick={() => void createRematch()}
            >
              {rematchBusy ? <LoaderCircle className="spin" /> : <RotateCcw />}
              {rematchBusy ? "正在创建…" : "同题再来一场"}
            </button>
          )}
          {(data.room.my_seat || data.room.can_control) && (
            <a
              className="button button-secondary"
              href={`${apiOrigin()}/api/matches/${data.match.id}/archive`}
              download
            >
              <Archive />
              下载完整归档
            </a>
          )}
          <Link href="/" className="button button-secondary">
            <ArrowLeft />
            赛事大厅
          </Link>
        </div>
      </section>

      {error && (
        <div className="error-box" role="alert">
          {error}
        </div>
      )}
      {connectionError && (
        <div className="stage-connection-warning" role="status">
          <span>
            {connectionError} 赛果仍可阅读；恢复连接后会自动同步管理员修正。
          </span>
          <button type="button" onClick={reconnect}>
            立即重连
          </button>
        </div>
      )}
      {!resultIsFinal && (
        <div className="notice-box" role="status">
          当前页面展示的是实时比赛记录，正式赛果尚未生成。
          <Link
            href={`/rooms/${code}/${data.room.my_seat || data.room.can_control ? "debate" : "watch"}`}
          >
            返回比赛现场
          </Link>
        </div>
      )}
      <div className="dashboard-grid">
        <section className="panel">
          <div className="panel-title">
            <h2>裁判评议</h2>
            <span className="badge">
              {resultStatusLabel[data.scorecard?.status || data.match.status] ||
                data.scorecard?.status ||
                data.match.status}
            </span>
          </div>
          <p className="hero-copy" style={{ fontSize: 15 }}>
            {data.scorecard?.reasoning ||
              data.match.reason ||
              "结果等待管理员复核后发布。"}
          </p>
        </section>
        <aside className="panel">
          <div className="panel-title">
            <h2>比赛摘要</h2>
            <Clock3 />
          </div>
          <div className="lobby-rule">
            <span>赛事</span>
            <strong>{competitionDisplayName(data.room.competition)}</strong>
          </div>
          <div className="lobby-rule">
            <span>发言数</span>
            <strong>{data.speeches.length}</strong>
          </div>
          <div className="lobby-rule">
            <span>时间线事件</span>
            <strong>{data.event_pagination.total}</strong>
          </div>
          <div className="lobby-rule">
            <span>参赛者积分净变动</span>
            <strong>
              {data.rating_changes.reduce(
                (total, item) => total + item.points_delta,
                0,
              )}
            </strong>
          </div>
          <div className="lobby-rule">
            <span>赛果修正</span>
            <strong>
              {
                data.rating_changes.filter(
                  (item) => item.source === "correction",
                ).length
              }{" "}
              条补偿
            </strong>
          </div>
        </aside>
      </div>

      {data.rating_changes.length > 0 && (
        <section className="panel" style={{ marginTop: 18 }}>
          <div className="panel-title">
            <h2>积分结算轨迹</h2>
            <span className="muted">
              修正记录以补偿方式追加，原始结算不会覆盖
            </span>
          </div>
          <div className="history-list">
            {data.rating_changes.map((item, index) => (
              <div
                className="history-row"
                style={{ gridTemplateColumns: "1fr auto" }}
                key={`${item.user_id || item.display_name}-${index}`}
              >
                <span className="row-main">
                  <strong>
                    {item.display_name} ·{" "}
                    {item.source === "correction" ? "赛果修正" : "首次结算"}
                  </strong>
                  <small>
                    评分 {item.score} · {ratingReasonLabel(item.reason)}
                  </small>
                </span>
                <strong
                  style={{
                    color:
                      item.points_delta > 0
                        ? "#40df9c"
                        : item.points_delta < 0
                          ? "#ff8092"
                          : "#9aa6c2",
                  }}
                >
                  {item.points_delta > 0 ? "+" : ""}
                  {item.points_delta}
                </strong>
              </div>
            ))}
          </div>
        </section>
      )}

      {data.scorecard?.status === "approved" && (
        <section className="panel" style={{ marginTop: 18 }}>
          <div className="panel-title">
            <h2>个人评分</h2>
            <span className="muted">未产生有效发言评分的席位显示暂无评分</span>
          </div>
          <div className="history-list">
            {data.room.seats.map((seat) => {
              const score = data.scorecard?.individual_scores?.[seat.seat_key];
              return (
                <div
                  className="history-row"
                  style={{ gridTemplateColumns: "1fr auto" }}
                  key={seat.seat_key}
                >
                  <span className="row-main">
                    <strong>{seat.display_name}</strong>
                    <small>{seat.label}</small>
                  </span>
                  <strong>{Number.isFinite(score) ? score : "暂无评分"}</strong>
                </div>
              );
            })}
          </div>
        </section>
      )}

      <section className="panel" style={{ marginTop: 18 }}>
        <div className="panel-title">
          <h2>完整辩论记录</h2>
        </div>
        <div className="transcript-list" role={data.speeches.length ? "list" : undefined} aria-label={data.speeches.length ? "完整辩论记录" : undefined}>
          {data.speeches.map((speech) => (
            <article className="transcript-item" role="listitem" key={speech.id}>
              <header>
                <span
                  className={`badge ${speech.seat_key.startsWith("aff") ? "" : "neg-badge"}`}
                >
                  {data.room.seats.find(
                    (seat) => seat.seat_key === speech.seat_key,
                  )?.display_name || speech.seat_key}
                </span>
                <small>
                  {speech.stage_name || speech.stage_key} ·{" "}
                  {speech.speaker_type === "human" ? "真人" : "AI"} ·{" "}
                  {speechStatusLabel[speech.status] || speech.status} ·{" "}
                  {new Date(speech.created_at).toLocaleString("zh-CN")}
                </small>
                {speech.audio_url && (
                  <span className="transcript-audio">
                    <span className="transcript-audio-duration">
                      录音时长 {Math.max(0, speech.duration_seconds).toFixed(1)}{" "}
                      秒
                    </span>
                    <audio
                      aria-label={`${speech.stage_name || speech.stage_key}录音`}
                      controls
                      preload="none"
                      ref={(element) => {
                        if (element)
                          audioElements.current.set(speech.id, element);
                      }}
                      src={`${apiOrigin()}${speech.audio_url}`}
                      onPlay={handleAudioPlay}
                    />
                  </span>
                )}
              </header>
              <p>
                {speech.content ||
                  (["failed", "failed_retried"].includes(speech.status)
                    ? "（服务失败，未生成可用发言）"
                    : "（该发言仅保存了音频）")}
              </p>
            </article>
          ))}
          {!data.speeches.length && (
            <div className="empty">本场没有已完成的发言记录</div>
          )}
        </div>
      </section>

      <section className="panel" style={{ marginTop: 18 }}>
        <div className="panel-title">
          <h2>
            <History size={19} />
            比赛时间线
          </h2>
          <span className="badge">
            已加载 {data.events.length} / {data.event_pagination.total}
          </span>
        </div>
        <div className="timeline-list" role={data.events.length ? "list" : undefined} aria-label={data.events.length ? "比赛时间线" : undefined}>
          {data.events.map((event) => (
            <div className="timeline-item" role="listitem" key={event.seq}>
              <span>#{event.seq}</span>
              <i />
              <div>
                <strong>{matchEventLabel(event.type)}</strong>
                <small>
                  {matchEventDetail(event)} ·{" "}
                  {new Date(event.created_at).toLocaleString("zh-CN")}
                </small>
              </div>
            </div>
          ))}
        </div>
        {data.event_pagination.has_more && (
          <div
            className="hero-actions"
            style={{ justifyContent: "center", marginTop: 16 }}
          >
            <button
              className="button button-secondary"
              disabled={loadingEvents}
              onClick={() => void loadOlderEvents()}
            >
              {loadingEvents ? (
                <LoaderCircle className="spin" size={17} />
              ) : (
                <History size={17} />
              )}
              {loadingEvents ? "正在载入…" : "加载更早事件"}
            </button>
          </div>
        )}
      </section>
    </div>
  );
}
