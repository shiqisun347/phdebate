import { useEffect, useMemo, useState } from "react";
import { getVoteOptions, submitAudienceVote } from "../api/client";
import { useActionFeedback } from "../components/Feedback";
import { StatusPill } from "../components/StatusPill";
import { seatLabel, sideLabel } from "../state/format";
import type { Side, VoteOptions } from "../types/contracts";

interface VotePageProps {
  matchId: string;
}

type VoteSpeaker = VoteOptions["speakers"][number];

export function VotePage({ matchId }: VotePageProps) {
  const [options, setOptions] = useState<VoteOptions | null>(null);
  const [winnerSide, setWinnerSide] = useState<Side | null>(null);
  const [aspects, setAspects] = useState<Record<"constructive" | "process" | "conclusion", Side | null>>({
    constructive: null,
    process: null,
    conclusion: null,
  });
  const [ranking, setRanking] = useState<string[]>([]); // speaker_id，rank1 在前
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const { busyProps, runAction } = useActionFeedback();

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const data = await getVoteOptions(matchId);
        if (cancelled) return;
        setOptions(data);
        const locallySubmitted = window.localStorage.getItem(voteSubmittedKey(data.match.id)) === "1";
        setSubmitted((current) => current || locallySubmitted);
        setError(null);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "投票页加载失败");
      }
    }
    void load();
    const timer = window.setInterval(load, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [matchId]);

  // 稳定布局：正方按座次、反方按座次，互不跳动。
  const { affSpeakers, negSpeakers, allIds } = useMemo(() => {
    const speakers = options?.speakers ?? [];
    const bySeat = (a: VoteSpeaker, b: VoteSpeaker) => a.seat - b.seat;
    const aff = speakers.filter((s) => s.side === "affirmative").sort(bySeat);
    const neg = speakers.filter((s) => s.side === "negative").sort(bySeat);
    return { affSpeakers: aff, negSpeakers: neg, allIds: speakers.map((s) => s.id) };
  }, [options]);

  function toggleRank(id: string) {
    if (submitted) return;
    setRanking((current) =>
      current.includes(id) ? current.filter((x) => x !== id) : [...current, id]
    );
  }

  async function submit() {
    try {
      await runAction("submit-audience-vote", "提交投票", async () => {
        setError(null);
        setSubmitting(true);
        const actualMatchId = options?.match.id ?? matchId;
        const tokenKey = voteTokenKey(actualMatchId);
        const token = window.localStorage.getItem(tokenKey) ?? createVoteToken();
        window.localStorage.setItem(tokenKey, token);
        await submitAudienceVote(matchId, {
          token,
          winner_side: winnerSide ?? "affirmative",
          aspects: {
            constructive: aspects.constructive ?? winnerSide ?? "affirmative",
            process: aspects.process ?? winnerSide ?? "affirmative",
            conclusion: aspects.conclusion ?? winnerSide ?? "affirmative",
          },
          ranking,
          client_fingerprint: window.navigator.userAgent,
        });
        window.localStorage.setItem(voteSubmittedKey(actualMatchId), "1");
        setSubmitted(true);
      }, { successText: "投票已提交" });
    } catch (err) {
      setError(err instanceof Error ? err.message : "投票提交失败");
    } finally {
      setSubmitting(false);
    }
  }

  if (!options) return <div className="loading">正在加载投票页...</div>;

  const voteUnavailableReason = audienceVoteUnavailableReason(options);
  const fullyRanked = ranking.length === allIds.length && allIds.length > 0;
  const fullyPickedAspects = aspects.constructive != null && aspects.process != null && aspects.conclusion != null;
  const canSubmit = !submitting && !voteUnavailableReason && winnerSide != null && fullyPickedAspects && fullyRanked;

  return (
    <main className="vote-shell vote-shell--rank">
      <section className="vote-card vote-card--rank">
        <div className="vote-event-mark" aria-label="人机辩论赛">
          <span>{options.match.title || "人机辩论赛"}</span>
          <em>观众投票 · Audience Vote</em>
        </div>
        <StatusPill tone={!voteUnavailableReason ? "green" : "gold"}>{voteStatusLabel(options)}</StatusPill>
        <h1 className="vote-topic">{options.match.topic}</h1>

        {submitted ? (
          <div className="vote-done">
            <div className="vote-done-check">✓</div>
            已收到你的投票，谢谢参与！
          </div>
        ) : voteUnavailableReason && options.vote_state.window_status !== "open" ? (
          <div className="vote-waiting">{voteUnavailableReason}</div>
        ) : (
          <>
            {/* 步骤一：选胜方 */}
            <div className="vote-step">
              <div className="vote-step-head"><span className="vote-step-no">1</span>选出你认为胜利的一方</div>
              <div className="vote-side-picker">
                {options.teams.map((team) => (
                  <button
                    key={team.id}
                    type="button"
                    className={`vote-side-btn vote-side-btn--${team.side} ${winnerSide === team.side ? "active" : ""}`}
                    onClick={() => setWinnerSide(team.side)}
                  >
                    <span className="vote-side-tag">{sideLabel(team.side)}</span>
                    <strong>{team.name}</strong>
                  </button>
                ))}
              </div>
            </div>

            {/* 步骤二：三个维度 */}
            <div className="vote-step">
              <div className="vote-step-head">
                <span className="vote-step-no">2</span>分别投出立论、过程、结辩
                <em className="vote-step-tip">每一项都要选正方或反方</em>
              </div>
              <div className="vote-aspect-grid">
                <AspectPicker
                  title="立论"
                  value={aspects.constructive}
                  onChange={(side) => setAspects((current) => ({ ...current, constructive: side }))}
                />
                <AspectPicker
                  title="过程"
                  value={aspects.process}
                  onChange={(side) => setAspects((current) => ({ ...current, process: side }))}
                />
                <AspectPicker
                  title="结辩"
                  value={aspects.conclusion}
                  onChange={(side) => setAspects((current) => ({ ...current, conclusion: side }))}
                />
              </div>
            </div>

            {/* 步骤三：8 人点选排序 */}
            <div className="vote-step">
              <div className="vote-step-head">
                <span className="vote-step-no">3</span>给 8 位辩手排名
                <em className="vote-step-tip">按你的喜好依次点击（先点=名次靠前），再点可取消</em>
              </div>
              <div className="vote-rank-grid">
                <RankColumn title="正方" side="affirmative" speakers={affSpeakers} ranking={ranking} onTap={toggleRank} />
                <RankColumn title="反方" side="negative" speakers={negSpeakers} ranking={ranking} onTap={toggleRank} />
              </div>
              <div className="vote-rank-progress">
                已排 <strong>{ranking.length}</strong> / {allIds.length}
                {ranking.length > 0 && (
                  <button type="button" className="vote-rank-clear" onClick={() => setRanking([])}>清空重排</button>
                )}
              </div>
            </div>

            {voteUnavailableReason && <div className="vote-hint">{voteUnavailableReason}</div>}
            {error && <div className="vote-error">{error}</div>}
          </>
        )}
      </section>

      {!submitted && options.vote_state.window_status === "open" && (
        <div className="vote-submit-bar">
          <button
            className="vote-submit-btn"
            {...busyProps("submit-audience-vote")}
            disabled={!canSubmit}
            onClick={submit}
          >
            {submitting
              ? "提交中…"
              : winnerSide == null
                ? "请先选择胜方"
                : !fullyPickedAspects
                  ? "请补全三项投票"
                : !fullyRanked
                  ? `还需排 ${allIds.length - ranking.length} 位辩手`
                  : "提交投票"}
          </button>
        </div>
      )}
    </main>
  );
}

function AspectPicker({
  title,
  value,
  onChange,
}: {
  title: string;
  value: Side | null;
  onChange: (side: Side) => void;
}) {
  return (
    <div className="vote-aspect-card">
      <div className="vote-aspect-title">{title}</div>
      <div className="vote-aspect-buttons">
        <button type="button" className={`vote-aspect-btn affirmative ${value === "affirmative" ? "active" : ""}`} onClick={() => onChange("affirmative")}>
          <span>正方</span>
        </button>
        <button type="button" className={`vote-aspect-btn negative ${value === "negative" ? "active" : ""}`} onClick={() => onChange("negative")}>
          <span>反方</span>
        </button>
      </div>
    </div>
  );
}

function RankColumn({
  title,
  side,
  speakers,
  ranking,
  onTap,
}: {
  title: string;
  side: Side;
  speakers: VoteSpeaker[];
  ranking: string[];
  onTap: (id: string) => void;
}) {
  return (
    <div className={`vote-rank-col vote-rank-col--${side}`}>
      <div className="vote-rank-col-title">{title}</div>
      {speakers.map((speaker) => {
        const rank = ranking.indexOf(speaker.id);
        const ranked = rank >= 0;
        return (
          <button
            key={speaker.id}
            type="button"
            className={`vote-rank-card ${ranked ? "ranked" : ""}`}
            onClick={() => onTap(speaker.id)}
          >
            <span className={`vote-rank-badge ${ranked ? "on" : ""}`}>
              <b>{ranked ? rank + 1 : "—"}</b>
              <em>{ranked ? "名" : "待选"}</em>
            </span>
            <span className="vote-rank-avatar">
              {speaker.image_url ? <img src={speaker.image_url} alt={speaker.name} /> : <span>{speaker.name.slice(0, 1)}</span>}
            </span>
            <span className="vote-rank-meta">
              <strong>{speaker.name}</strong>
              <em>{sideLabel(speaker.side)} · {seatLabel(speaker.seat)}</em>
            </span>
          </button>
        );
      })}
    </div>
  );
}

function voteStatusLabel(options: VoteOptions): string {
  if (options.match.status === "paused") return "比赛暂停";
  if (options.match.status === "intervention") return "应急处理中";
  return options.vote_state.window_status === "open" ? "投票进行中" : "投票未开启";
}

function audienceVoteUnavailableReason(options: VoteOptions): string | null {
  if (options.match.status === "paused") return "比赛暂停中，继续后才能投票。";
  if (options.match.status === "intervention") return "现场正在应急处理，投票暂不可用。";
  if (options.vote_state.window_status !== "open") return "观众投票尚未开启，请等待主持人开启。";
  return null;
}

function voteTokenKey(matchId: string): string {
  return `phdebate_vote_token_${matchId}`;
}

function voteSubmittedKey(matchId: string): string {
  return `phdebate_vote_submitted_${matchId}`;
}

function createVoteToken() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  if (window.crypto?.getRandomValues) {
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return `vote_${Array.from(bytes, (item) => item.toString(16).padStart(2, "0")).join("")}`;
  }
  return `vote_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
}
