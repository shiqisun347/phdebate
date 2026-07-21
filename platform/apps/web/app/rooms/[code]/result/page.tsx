"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import {
  ArrowLeft,
  Archive,
  Award,
  CirclePause,
  CircleStop,
  Clock3,
  Gavel,
  History,
  LoaderCircle,
  Radio,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
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
import { SpeechCorrectionControl, type SpeechCorrectionRequest } from "@/components/speech-correction-control";
import { ApiRequestError, apiFetch, apiOrigin } from "@/lib/api";
import {
  matchEventDetail,
  matchEventLabel,
  type MatchTimelineEvent,
} from "@/lib/match-events";
import { competitionDisplayName } from "@/lib/primary-competition";
import { ratingReasonLabel, resultStatusLabel, roomStatusLabel } from "@/lib/status-labels";
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
    can_request_correction?: boolean;
  }[];
  speech_pagination: {
    page: number;
    page_size: number;
    total: number;
    pages: number;
    next_cursor: string | null;
    has_more: boolean;
  };
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

function mergeSpeeches(...groups: Result["speeches"][]) {
  return [
    ...new Map(
      groups.flat().map((speech) => [speech.id, speech]),
    ).values(),
  ].sort((left, right) => {
    const timestampDifference =
      new Date(left.created_at).getTime() - new Date(right.created_at).getTime();
    return timestampDifference || left.id.localeCompare(right.id);
  });
}

export default function ResultPage() {
  const { code } = useParams<{ code: string }>();
  const router = useRouter();
  const [data, setData] = useState<Result | null>(null);
  const [error, setError] = useState("");
  const [loadingSpeeches, setLoadingSpeeches] = useState(false);
  const [loadingEvents, setLoadingEvents] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [rematchBusy, setRematchBusy] = useState(false);
  const [speechCorrections, setSpeechCorrections] = useState<SpeechCorrectionRequest[]>([]);
  const [speechCorrectionError, setSpeechCorrectionError] = useState("");
  const rematchKey = useRef<string | null>(null);
  const loadSequence = useRef(0);
  const loadedSpeechIds = useRef(new Set<string>());
  const speechLoadInFlight = useRef<string | null>(null);
  const audioElements = useRef(new Map<string, HTMLAudioElement>());
  const { room: liveRoom, error: connectionError, reconnect } = useRoom(code);

  const updateSpeechCorrection = useCallback((request: SpeechCorrectionRequest) => {
    setSpeechCorrections((current) => [
      request,
      ...current.filter((item) => item.id !== request.id),
    ]);
  }, []);

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

  const load = useCallback(async (preserveLoadedSpeeches = false) => {
    const sequence = ++loadSequence.current;
    const speechIdsToPreserve = preserveLoadedSpeeches
      ? new Set(loadedSpeechIds.current)
      : new Set<string>();
    setError("");
    try {
      let next = await apiFetch<Result>(
        `/api/rooms/${code}/result?speech_page=1&speech_page_size=50&event_page=1&event_page_size=50`,
      );
      let speeches = next.speeches;
      let pagination = next.speech_pagination;
      while (
        [...speechIdsToPreserve].some(
          (speechId) => !speeches.some((speech) => speech.id === speechId),
        ) &&
        pagination.has_more &&
        pagination.next_cursor
      ) {
        const older = await apiFetch<Result>(
          `/api/rooms/${code}/result?speech_page_size=${pagination.page_size}&speech_cursor=${encodeURIComponent(pagination.next_cursor)}&event_page=1&event_page_size=50`,
        );
        speeches = mergeSpeeches(older.speeches, speeches);
        pagination = older.speech_pagination;
      }
      next = { ...next, speeches, speech_pagination: pagination };
      if (sequence === loadSequence.current) setData(next);
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 409) {
        router.replace(`/rooms/${code}/watch`);
        return;
      }
      if (sequence === loadSequence.current)
        setError(err instanceof Error ? err.message : "比赛结果载入失败");
    }
  }, [code, router]);

  useEffect(() => {
    if (!data?.speeches.some((speech) => speech.can_request_correction)) {
      setSpeechCorrections([]);
      setSpeechCorrectionError("");
      return;
    }
    let cancelled = false;
    setSpeechCorrectionError("");
    apiFetch<{ items: SpeechCorrectionRequest[] }>(`/api/rooms/${code}/speech-correction-requests`)
      .then((response) => {
        if (!cancelled) setSpeechCorrections(response.items);
      })
      .catch((caught) => {
        if (!cancelled) setSpeechCorrectionError(caught instanceof Error ? caught.message : "修正申请载入失败");
      });
    return () => { cancelled = true; };
  }, [code, data?.speeches]);

  useEffect(() => {
    void load(false);
  }, [load]);

  useEffect(
    () => () => {
      stopAudio(undefined, true);
    },
    [code, stopAudio],
  );

  useEffect(() => {
    loadedSpeechIds.current = new Set(
      data?.speeches.map((speech) => speech.id) || [],
    );
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
    if (data && liveRoom && liveRoom.seq > data.room.seq) void load(true);
  }, [data, liveRoom, load]);

  async function refreshResult() {
    setRefreshing(true);
    try {
      await load(true);
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

  async function loadOlderSpeeches() {
    if (
      !data ||
      !data.speech_pagination.has_more ||
      !data.speech_pagination.next_cursor ||
      speechLoadInFlight.current
    )
      return;
    const cursor = data.speech_pagination.next_cursor;
    speechLoadInFlight.current = cursor;
    setLoadingSpeeches(true);
    setError("");
    try {
      const older = await apiFetch<Result>(
        `/api/rooms/${code}/result?speech_page_size=${data.speech_pagination.page_size}&speech_cursor=${encodeURIComponent(cursor)}&event_page=1&event_page_size=50`,
      );
      setData((current) =>
        current?.speech_pagination.next_cursor === cursor
          ? {
              ...current,
              speeches: mergeSpeeches(older.speeches, current.speeches),
              speech_pagination: older.speech_pagination,
            }
          : current,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "更早的发言记录载入失败");
    } finally {
      speechLoadInFlight.current = null;
      setLoadingSpeeches(false);
    }
  }

  if (!data && error)
    return <LoadError message={error} retry={() => void load()} />;
  if (!data) return <div className="loading-screen">正在整理比赛结果…</div>;

  const resultIsFinal = ["completed", "review_required", "terminated"].includes(
    data.match.status,
  );
  const hasParticipantAccess = Boolean(data.room.my_seat || data.room.can_control);
  const canViewTranscript = data.room.can_view_transcript ?? hasParticipantAccess;
  const liveRoomHref = `/rooms/${code}/${hasParticipantAccess ? "debate" : "watch"}`;
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
  const ResultIcon = data.match.status === "terminated"
    ? CircleStop
    : data.match.status === "review_required"
      ? ShieldAlert
      : data.room.status === "judging"
        ? Gavel
        : data.room.status === "paused"
          ? CirclePause
          : ["running", "preparing", "lobby"].includes(data.room.status)
            ? Radio
            : Trophy;
  const resultTone = data.match.status === "terminated"
    ? "terminated"
    : data.match.status === "review_required"
      ? "attention"
      : ["running", "preparing", "lobby", "paused", "judging"].includes(data.room.status)
        ? "live"
        : "settled";
  const resultGuidance = data.match.status === "terminated"
    ? "比赛记录已封存，不会计入正常胜负结算；管理员仍可下载归档排查终止原因。"
    : data.match.status === "review_required"
      ? "AI 裁判未能自动形成可发布赛果。管理员完成复核前，不展示胜负和个人评分，也不更新排行榜。"
      : data.room.status === "paused"
        ? "比赛记录仍在保存。参赛者请返回现场等待房主或管理员恢复，不要重复创建房间。"
        : data.room.status === "judging"
          ? "发言已经结束，裁判正在整理评分。页面会在赛果生成后自动同步。"
          : ["running", "preparing", "lobby"].includes(data.room.status)
            ? "这是比赛过程记录，不是最终成绩。返回现场可继续当前比赛。"
            : canViewTranscript
              ? "赛果已经确认，逐字稿、评分和积分变化均可在下方回溯。"
              : "赛果已经确认，评分、比赛录音和积分变化均可在下方回溯。";
  const liveRecordSummary = data.room.status === "paused"
    ? `当前停在“${data.room.current_stage?.name || "比赛阶段"}”，已有发言和事件均已保存；恢复后将从这里继续。`
    : `当前阶段为“${data.room.current_stage?.name || roomStatusLabel[data.room.status] || data.room.status}”，本页只记录过程，不代表最终成绩。`;

  return (
    <div className="page-shell">
      <section className={`result-hero panel ${resultTone}`}>
        <span className="result-state-icon" aria-hidden="true"><ResultIcon size={34} /></span>
        <span className="eyebrow">{resultIsFinal ? "比赛记录" : "实时记录"} · #{code}</span>
        <h1>{winner}</h1>
        <p>{data.room.topic}</p>
        <p className="result-guidance">{resultGuidance}</p>
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
            {refreshing ? "刷新中…" : resultIsFinal ? "刷新赛果" : "刷新记录"}
          </button>
          {!resultIsFinal && (
            <Link href={liveRoomHref} className="button">
              <ArrowLeft />
              返回比赛现场
            </Link>
          )}
          {hasParticipantAccess && (
            <Link href="/me" className={resultIsFinal ? "button" : "button button-secondary"}>
              <Award />
              返回个人中心
            </Link>
          )}
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
          当前页面展示的是实时比赛记录，正式赛果尚未生成；比赛恢复或阶段推进后，本页会继续同步。
        </div>
      )}
      <div className="dashboard-grid">
        <section className="panel">
          <div className="panel-title">
            <h2>{resultIsFinal ? "裁判评议" : "当前比赛状态"}</h2>
            <span className="badge">
              {resultIsFinal
                ? resultStatusLabel[data.scorecard?.status || data.match.status] || data.scorecard?.status || data.match.status
                : "比赛尚未结束"}
            </span>
          </div>
          <p className="hero-copy" style={{ fontSize: 15 }}>
            {resultIsFinal
              ? data.scorecard?.reasoning || data.match.reason || "结果等待管理员复核后发布。"
              : liveRecordSummary}
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
            <strong>{data.speech_pagination.total}</strong>
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
          <h2>{canViewTranscript ? "完整辩论记录" : "比赛录音回放"}</h2>
          <span className="badge">
            已加载 {data.speeches.length} / {data.speech_pagination.total}
          </span>
        </div>
        {speechCorrectionError && <div className="error-box" role="alert">{speechCorrectionError}</div>}
        <div className="transcript-list" role={data.speeches.length ? "list" : undefined} aria-label={data.speeches.length ? (canViewTranscript ? "完整辩论记录" : "比赛录音回放") : undefined}>
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
              {canViewTranscript && (
                <p>
                  {speech.content ||
                    (["failed", "failed_retried"].includes(speech.status)
                      ? "（服务失败，未生成可用发言）"
                      : "（该发言仅保存了音频）")}
                </p>
              )}
              {speech.can_request_correction && (
                <SpeechCorrectionControl
                  roomCode={code}
                  speech={{ id: speech.id, stage_name: speech.stage_name || speech.stage_key, content: speech.content }}
                  request={speechCorrections
                    .filter((item) => item.speech_id === speech.id)
                    .sort((left, right) => new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime())[0]}
                  onChanged={updateSpeechCorrection}
                />
              )}
            </article>
          ))}
          {!data.speeches.length && (
            <div className="empty">本场没有已完成的发言记录</div>
          )}
        </div>
        {data.speech_pagination.has_more && (
          <div
            className="hero-actions"
            style={{ justifyContent: "center", marginTop: 16 }}
          >
            <button
              className="button button-secondary"
              disabled={loadingSpeeches}
              onClick={() => void loadOlderSpeeches()}
            >
              {loadingSpeeches ? (
                <LoaderCircle className="spin" size={17} />
              ) : (
                <History size={17} />
              )}
              {loadingSpeeches ? "正在载入…" : "加载更早发言"}
            </button>
          </div>
        )}
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
        <div className="timeline-list result-timeline-list" role={data.events.length ? "list" : undefined} aria-label={data.events.length ? "比赛时间线" : undefined}>
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
          {!data.events.length && <div className="empty">比赛时间线正在生成，阶段推进后会显示在这里</div>}
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
