import { useEffect, useState } from "react";
import { getVoteOptions, submitAudienceVote } from "../api/client";
import { useActionFeedback } from "../components/Feedback";
import { StatusPill } from "../components/StatusPill";
import { seatLabel, sideLabel } from "../state/format";
import type { Side, VoteOptions } from "../types/contracts";

interface VotePageProps {
  matchId: string;
}

export function VotePage({ matchId }: VotePageProps) {
  const [options, setOptions] = useState<VoteOptions | null>(null);
  const [winnerSide, setWinnerSide] = useState<Side>("affirmative");
  const [bestSpeakerId, setBestSpeakerId] = useState("");
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
        setWinnerSide((current) => data.teams.some((team) => team.side === current) ? current : "affirmative");
        setBestSpeakerId((current) => current || data.speakers[0]?.id || "");
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
          winner_side: winnerSide,
          best_speaker_id: bestSpeakerId,
          client_fingerprint: window.navigator.userAgent
        });
        window.localStorage.setItem(voteSubmittedKey(actualMatchId), "1");
        setSubmitted(true);
      }, {
        successText: "投票已提交"
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "投票提交失败");
    } finally {
      setSubmitting(false);
    }
  }

  if (!options) return <div className="loading">正在加载投票页...</div>;

  const voteUnavailableReason = audienceVoteUnavailableReason(options);
  const canSubmit = !submitting && !voteUnavailableReason && Boolean(bestSpeakerId);

  return (
    <main className="vote-shell">
      <section className="vote-card">
        <div className="vote-event-mark" aria-label="第一届人机辩论赛">
          <span>第一届</span>
          <strong>人机辩论赛</strong>
          <em>AI Debate</em>
        </div>
        <StatusPill tone={!voteUnavailableReason ? "green" : "gold"}>
          {voteStatusLabel(options)}
        </StatusPill>
        <h1>{options.match.topic}</h1>
        {submitted ? (
          <div className="vote-done">已收到投票，谢谢参与。</div>
        ) : (
          <>
            <label>
              优胜方
              <select value={winnerSide} onChange={(event) => setWinnerSide(event.target.value as Side)}>
                {options.teams.map((team) => (
                  <option key={team.id} value={team.side}>{sideLabel(team.side)} · {team.name}</option>
                ))}
              </select>
            </label>
            <label>
              最佳辩手
              <select value={bestSpeakerId} onChange={(event) => setBestSpeakerId(event.target.value)}>
                {options.speakers.map((speaker) => (
                  <option key={speaker.id} value={speaker.id}>{sideLabel(speaker.side)}{seatLabel(speaker.seat)} · {speaker.name}</option>
                ))}
              </select>
            </label>
            {voteUnavailableReason && <div className="vote-hint">{voteUnavailableReason}</div>}
            <button {...busyProps("submit-audience-vote")} disabled={!canSubmit} title={voteUnavailableReason || undefined} onClick={submit}>
              {submitting ? "提交中" : "提交投票"}
            </button>
            {error && <div className="vote-error">{error}</div>}
          </>
        )}
      </section>
    </main>
  );
}

function voteStatusLabel(options: VoteOptions): string {
  if (options.match.status === "paused") return "比赛暂停";
  if (options.match.status === "intervention") return "应急处理中";
  return options.vote_state.window_status === "open" ? "投票中" : "投票未开启";
}

function audienceVoteUnavailableReason(options: VoteOptions): string | null {
  if (options.match.status === "paused") return "比赛暂停中，继续后才能投票。";
  if (options.match.status === "intervention") return "现场正在应急处理，投票暂不可用。";
  if (options.vote_state.window_status !== "open") return "学生投票尚未开启。";
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
