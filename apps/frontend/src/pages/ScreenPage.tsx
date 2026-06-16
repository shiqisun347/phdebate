import { useEffect, useState } from "react";
import QRCode from "qrcode";
import { ClockTile } from "../components/ClockTile";
import { AuthPrompt } from "../components/AuthPrompt";
import { StatusPill } from "../components/StatusPill";
import { clockByName, seatLabel, sideClass, sideLabel, speakerLabel } from "../state/format";
import type { MatchSnapshot, ScreenScene, Side, Speaker } from "../types/contracts";
import { useMatch } from "../realtime/useMatch";
import { playBellCue } from "../utils/audioCue";

interface ScreenPageProps {
  matchId: string;
}

type RuntimeScreenScene = "idle" | "live" | "paused" | "judge_commentary" | "judge_result" | "audience_result";

export function ScreenPage({ matchId }: ScreenPageProps) {
  const { snapshot, loadError, lastEvent } = useMatch(matchId, "screen");

  useEffect(() => {
    if (!lastEvent || lastEvent.type !== "clock.bell_triggered") return;
    const durationMs = Number(lastEvent.payload.duration_ms ?? 800);
    playBellCue(durationMs);
  }, [lastEvent]);

  if (!snapshot && loadError) return <AuthPrompt role="screen" message={loadError} />;
  if (!snapshot) return <div className="loading">正在连接大屏状态...</div>;
  return <ScreenView snapshot={snapshot} />;
}

function ScreenView({ snapshot }: { snapshot: MatchSnapshot }) {
  const scene = snapshot.match.status === "paused" ? "paused" : normalizeScreenScene(snapshot.match.screen_scene);

  return (
    <main className="screen-stage">
      <div className="screen-bg" />
      {scene === "idle" && <IdleScene snapshot={snapshot} />}
      {scene === "paused" && <PausedScene snapshot={snapshot} />}
      {scene === "judge_commentary" && <JudgeCommentaryScene snapshot={snapshot} />}
      {scene === "judge_result" && <JudgeResultScene snapshot={snapshot} />}
      {scene === "audience_result" && <AudienceResultScene snapshot={snapshot} />}
      {scene === "live" && <LiveScene snapshot={snapshot} />}
    </main>
  );
}

function ScreenChrome() {
  return (
    <header className="screen-top">
      <div className="screen-event-wordmark">第一届人机辩论赛</div>
      <img src="/assets/logo-full-white.png" alt="中国科学院计算技术研究所" />
    </header>
  );
}

function IdleScene({ snapshot }: { snapshot: MatchSnapshot }) {
  return (
    <section className="screen-scene">
      <ScreenChrome />
      <Topic snapshot={snapshot} />
      <div className="live-grid">
        <RosterPanel snapshot={snapshot} side="affirmative" />
        <div className="live-center">
          <div className="mode-panel idle-wait-mode">
            <div className="phase-name">候场</div>
            <h3>比赛即将开始</h3>
            <p>请等待主持人宣布开始</p>
          </div>
        </div>
        <RosterPanel snapshot={snapshot} side="negative" />
      </div>
    </section>
  );
}

function LiveScene({ snapshot }: { snapshot: MatchSnapshot }) {
  const phase = snapshot.phases.find((item) => item.id === snapshot.match.current_phase_id);
  const currentSpeaker = snapshot.speakers.find((item) => item.id === snapshot.current_speech?.speaker_id);
  const liveMode = snapshot.match.live_mode;

  return (
    <section className="screen-scene">
      <ScreenChrome />
      <Topic snapshot={snapshot} />
      <div className="live-grid">
        <RosterPanel snapshot={snapshot} side="affirmative" activeSpeaker={currentSpeaker} />
        <div className="live-center">
          {snapshot.flow.awaiting_host_confirm ? (
            <FlowWaitMode snapshot={snapshot} />
          ) : liveMode === "prep" ? (
            <PrepMode speaker={currentSpeaker} phaseName={phase?.name ?? "AI 准备"} />
          ) : liveMode === "free" ? (
            <FreeMode snapshot={snapshot} currentSpeaker={currentSpeaker} />
          ) : (
            <SingleMode snapshot={snapshot} currentSpeaker={currentSpeaker} phaseName={phase?.name ?? "当前环节"} />
          )}
        </div>
        <RosterPanel snapshot={snapshot} side="negative" activeSpeaker={currentSpeaker} />
      </div>
      <Subtitle snapshot={snapshot} currentSpeaker={currentSpeaker} />
    </section>
  );
}

function PausedScene({ snapshot }: { snapshot: MatchSnapshot }) {
  const phase = snapshot.phases.find((item) => item.id === snapshot.match.current_phase_id);
  return (
    <section className="screen-scene paused-scene">
      <ScreenChrome />
      <Topic snapshot={snapshot} />
      <div className="paused-panel">
        <span>现场暂停</span>
        <h1>比赛暂停</h1>
        <p>{phase ? `当前停留在「${phase.name}」` : "请等待主持人继续比赛"}</p>
      </div>
    </section>
  );
}

function JudgeCommentaryScene({ snapshot }: { snapshot: MatchSnapshot }) {
  const voteUrl = `${window.location.origin}/vote`;
  return (
    <section className="screen-scene">
      <ScreenChrome />
      <Topic snapshot={snapshot} />
      <div className="live-grid commentary-grid">
        <RosterPanel snapshot={snapshot} side="affirmative" />
        <div className="commentary-panel">
          <span>赛后环节</span>
          <h1>评委点评</h1>
          <p>
            {snapshot.vote_state.window_status === "open" ? "学生扫码投票已开启" : "学生投票暂未开启"}
            {" · 当前收到 "}{snapshot.vote_state.audience_count} 票
          </p>
          <VoteQr url={voteUrl} />
          <div className="vote-url">{voteUrl}</div>
        </div>
        <RosterPanel snapshot={snapshot} side="negative" />
      </div>
    </section>
  );
}

function JudgeResultScene({ snapshot }: { snapshot: MatchSnapshot }) {
  const judgePublished = snapshot.vote_state.judge_published;
  const winner = snapshot.teams.find((team) => team.side === snapshot.vote_state.winner_side);
  const best = snapshot.speakers.find((speaker) => speaker.id === snapshot.vote_state.best_speaker_id);
  const judge = snapshot.vote_state.judge_summary;
  return (
    <section className="screen-scene official-result-scene">
      <ScreenChrome />
      <div className="result-center official-result">
        <span>官方评委结果</span>
        {judgePublished ? (
          <>
            <h1>{sideLabel(snapshot.vote_state.winner_side)} · {winner?.name}</h1>
            <div className="best-speaker-award">
              <div className="best-speaker-label">最佳辩手</div>
              <div className="best-speaker-name">{speakerLabel(best)}</div>
            </div>
          </>
        ) : (
          <>
            <h1>结果待公布</h1>
            <p>请等待评委合议结果</p>
          </>
        )}
        <div className="judge-vote-scores">
          <JudgeVoteScore label="立论" affirmative={judge.constructive.affirmative} negative={judge.constructive.negative} published={judgePublished} />
          <JudgeVoteScore label="过程" affirmative={judge.process.affirmative} negative={judge.process.negative} published={judgePublished} />
          <JudgeVoteScore label="结辩" affirmative={judge.conclusion.affirmative} negative={judge.conclusion.negative} published={judgePublished} />
        </div>
      </div>
    </section>
  );
}

function AudienceResultScene({ snapshot }: { snapshot: MatchSnapshot }) {
  const audience = snapshot.vote_state.audience_summary;
  const total = Math.max(1, audience.total);
  const affPercent = Math.round((audience.winner.affirmative / total) * 100);
  const negPercent = Math.round((audience.winner.negative / total) * 100);
  const winnerSide: Side = audience.winner.negative > audience.winner.affirmative ? "negative" : "affirmative";
  const winner = snapshot.teams.find((team) => team.side === winnerSide);
  return (
    <section className="screen-scene result-scene audience-result-scene">
      <ScreenChrome />
      <div className="result-center audience-result">
        <span>学生投票结果</span>
        <h1>{sideLabel(winnerSide)} · {winner?.name}</h1>
        <p>共收到 <strong>{audience.total}</strong> 票</p>
        <div className="audience-bars">
          <AudienceBar side="affirmative" label="正方" count={audience.winner.affirmative} percent={affPercent} />
          <AudienceBar side="negative" label="反方" count={audience.winner.negative} percent={negPercent} />
        </div>
        <div className="audience-ranking">
          <h2>最佳辩手排行</h2>
          {audience.best_speaker.slice(0, 4).map((item, index) => {
            const speaker = snapshot.speakers.find((candidate) => candidate.id === item.speaker_id);
            return (
              <div className="audience-rank-row" key={`${item.speaker_id}-${index}`}>
                <span>{index + 1}</span>
                <strong>{speakerLabel(speaker)}</strong>
                <em>{item.count} 票</em>
              </div>
            );
          })}
          {!audience.best_speaker.length && <p>等待学生投票统计。</p>}
        </div>
      </div>
    </section>
  );
}

function Topic({ snapshot }: { snapshot: MatchSnapshot }) {
  return (
    <div className="topic-line">
      <h2>{snapshot.match.topic}</h2>
    </div>
  );
}

function RosterPanel({
  snapshot,
  side,
  activeSpeaker,
  expanded = false
}: {
  snapshot: MatchSnapshot;
  side: "affirmative" | "negative";
  activeSpeaker?: Speaker;
  expanded?: boolean;
}) {
  const team = snapshot.teams.find((item) => item.side === side);
  const speakers = snapshot.speakers.filter((item) => item.side === side);
  return (
    <aside className={`roster-panel ${sideClass(side)} ${expanded ? "expanded" : ""}`}>
      <div className="roster-head">
        <div className="roster-side-mark">{sideLabel(side)}</div>
        <div className="roster-team-copy">
          <span>{team?.position}</span>
          <strong>{team?.name}</strong>
        </div>
      </div>
      <div className="roster-list">
        {speakers.map((speaker) => (
          <div className={`roster-row ${activeSpeaker?.id === speaker.id ? "speaking" : ""}`} key={speaker.id}>
            <div className="roster-seat">{seatLabel(speaker.seat)}</div>
            <div className="roster-person">
              <strong>{speaker.name}</strong>
              <span className={`roster-meta ${speaker.speaker_type}`}>
                {speaker.speaker_type === "agent" ? speaker.model_name || "AI 模型" : "人类选手"}
              </span>
            </div>
          </div>
        ))}
      </div>
    </aside>
  );
}

function FlowWaitMode({ snapshot }: { snapshot: MatchSnapshot }) {
  const nextText =
    snapshot.flow.next_action === "free_turn_next" ? `下一轮：${sideLabel(snapshot.free_debate.current_turn_side)}发言` :
    snapshot.flow.next_action === "phase_next" ? "下一步：进入下一环节" :
    "下一步：评委点评与投票";
  return (
    <div className="mode-panel flow-wait-mode">
      <div className="phase-name">时间到</div>
      <h3>等待主持确认</h3>
      <p>{snapshot.flow.message || "请等待主持导播台确认下一步"}</p>
      <strong>{nextText}</strong>
    </div>
  );
}

function SingleMode({ snapshot, currentSpeaker, phaseName }: { snapshot: MatchSnapshot; currentSpeaker?: Speaker; phaseName: string }) {
  return (
    <div className="mode-panel">
      <div className="phase-name">{phaseName}</div>
      <p>当前发言 · {speakerLabel(currentSpeaker)}</p>
      <ClockTile label="本环节剩余" clock={clockByName(snapshot.clocks, "main")} tone={currentSpeaker?.side === "negative" ? "neg" : "aff"} />
    </div>
  );
}

function FreeMode({ snapshot, currentSpeaker }: { snapshot: MatchSnapshot; currentSpeaker?: Speaker }) {
  return (
    <div className="mode-panel free-mode">
      <div className="phase-name">自由辩论</div>
      <p>当前发言 · {speakerLabel(currentSpeaker)}</p>
      <div className="free-clocks">
        <ClockTile label="正方剩余" clock={clockByName(snapshot.clocks, "affirmative_total")} tone="aff" />
        <ClockTile label="单次上限" clock={clockByName(snapshot.clocks, "turn")} tone="turn" />
        <ClockTile label="反方剩余" clock={clockByName(snapshot.clocks, "negative_total")} tone="neg" />
      </div>
      <p>当前轮次方 · {sideLabel(snapshot.free_debate.current_turn_side)} · 第 {snapshot.free_debate.turn_index} 轮</p>
    </div>
  );
}

function PrepMode({ speaker, phaseName }: { speaker?: Speaker; phaseName: string }) {
  return (
    <div className="mode-panel prep-mode">
      <div className="phase-name">{phaseName}</div>
      <div className="prep-orb"><i /><i /></div>
      <h3>AI 思考中</h3>
      <p>{speakerLabel(speaker)} · 生成完成后开始播报并计时</p>
    </div>
  );
}

function Subtitle({ snapshot, currentSpeaker }: { snapshot: MatchSnapshot; currentSpeaker?: Speaker }) {
  const segment = snapshot.recent_transcript[0];
  const text = snapshot.current_speech?.content_partial || segment?.text || "等待发言内容...";
  const source = snapshot.current_speech?.source ?? segment?.source;
  const isAgent = source === "agent_text";
  const degraded = snapshot.speech_service.tts.status === "failed" && snapshot.speech_service.tts.speaker_id === currentSpeaker?.id;
  const asrFailed = snapshot.speech_service.asr.status === "failed" && source === "human_asr";
  return (
    <footer className="subtitle-panel">
      <div>
        <strong>{currentSpeaker ? speakerLabel(currentSpeaker) : segment?.speaker_label ?? "等待指定"}</strong>
        <StatusPill tone={isAgent ? "blue" : "green"}>{isAgent ? "AI 发言" : "实时转写"}</StatusPill>
        {degraded && <StatusPill tone="red">TTS 降级</StatusPill>}
        {asrFailed && <StatusPill tone="red">ASR 异常</StatusPill>}
        <span>{snapshot.current_speech ? "进行中" : "最近发言"}</span>
      </div>
      <p>{text}</p>
    </footer>
  );
}

function JudgeVoteScore({ label, affirmative, negative, published }: { label: string; affirmative: number; negative: number; published: boolean }) {
  return (
    <div className="judge-vote-score-card">
      <strong>{label}</strong>
      <div className="judge-score-counts">
        <div className="judge-score-side aff">
          <span className="judge-score-num">{published ? affirmative : "—"}</span>
          <span className="judge-score-side-label">正方</span>
        </div>
        <div className="judge-score-divider">:</div>
        <div className="judge-score-side neg">
          <span className="judge-score-num">{published ? negative : "—"}</span>
          <span className="judge-score-side-label">反方</span>
        </div>
      </div>
    </div>
  );
}

function AudienceBar({ side, label, count, percent }: { side: "affirmative" | "negative"; label: string; count: number; percent: number }) {
  return (
    <div className={`audience-bar ${sideClass(side)}`}>
      <div>
        <strong>{label}</strong>
        <span>{count} 票 · {percent}%</span>
      </div>
      <i style={{ width: `${percent}%` }} />
    </div>
  );
}

function VoteQr({ url }: { url: string }) {
  const [dataUrl, setDataUrl] = useState("");

  useEffect(() => {
    let cancelled = false;
    QRCode.toDataURL(url, {
      errorCorrectionLevel: "M",
      margin: 1,
      width: 260,
      color: {
        dark: "#11151b",
        light: "#ffffff"
      }
    }).then((next) => {
      if (!cancelled) setDataUrl(next);
    });
    return () => {
      cancelled = true;
    };
  }, [url]);

  return (
    <div className="qr-placeholder">
      {dataUrl ? <img src={dataUrl} alt="学生投票二维码" /> : "生成二维码中"}
    </div>
  );
}

function normalizeScreenScene(scene: ScreenScene): RuntimeScreenScene {
  if (scene === "opening") return "live";
  if (scene === "teams") return "idle";
  if (scene === "intermission") return "judge_commentary";
  if (scene === "result") return "judge_result";
  if (scene === "paused") return "paused";
  if (scene === "judge_commentary") return "judge_commentary";
  if (scene === "judge_result") return "judge_result";
  if (scene === "audience_result") return "audience_result";
  return scene === "idle" ? "idle" : "live";
}
