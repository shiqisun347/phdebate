import { useEffect, useState } from "react";
import { getVoteOptions, submitAudienceVote } from "../api/client";
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

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const data = await getVoteOptions(matchId);
        if (cancelled) return;
        setOptions(data);
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
      setError(null);
      const tokenKey = `phdebate_vote_token_${matchId}`;
      const token = window.localStorage.getItem(tokenKey) ?? window.crypto.randomUUID();
      window.localStorage.setItem(tokenKey, token);
      await submitAudienceVote(matchId, {
        token,
        winner_side: winnerSide,
        best_speaker_id: bestSpeakerId,
        client_fingerprint: window.navigator.userAgent
      });
      setSubmitted(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "投票提交失败");
    }
  }

  if (!options) return <div className="loading">正在加载投票页...</div>;

  return (
    <main className="vote-shell">
      <section className="vote-card">
        <img src="/assets/logo-full-color.png" alt="中国科学院计算技术研究所" />
        <StatusPill tone={options.vote_state.window_status === "open" ? "green" : "gold"}>
          {options.vote_state.window_status === "open" ? "投票中" : "投票未开启"}
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
            <button disabled={options.vote_state.window_status !== "open" || !bestSpeakerId} onClick={submit}>提交投票</button>
            {error && <div className="vote-error">{error}</div>}
          </>
        )}
      </section>
    </main>
  );
}
