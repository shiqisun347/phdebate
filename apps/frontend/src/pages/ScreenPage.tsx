import { useEffect, useState } from "react";
import QRCode from "qrcode";
import { ClockTile } from "../components/ClockTile";
import { AuthPrompt } from "../components/AuthPrompt";
import { StatusPill } from "../components/StatusPill";
import { clockByName, seatLabel, sideClass, sideLabel, speakerLabel } from "../state/format";
import type { MatchSnapshot, Speaker } from "../types/contracts";
import { useMatch } from "../realtime/useMatch";

interface ScreenPageProps {
  matchId: string;
}

export function ScreenPage({ matchId }: ScreenPageProps) {
  const { snapshot, socketStatus, loadError } = useMatch(matchId, "screen");
  if (!snapshot && loadError) return <AuthPrompt role="screen" message={loadError} />;
  if (!snapshot) return <div className="loading">正在连接大屏状态...</div>;
  return <ScreenView snapshot={snapshot} socketStatus={socketStatus} />;
}

function ScreenView({ snapshot, socketStatus }: { snapshot: MatchSnapshot; socketStatus: string }) {
  const { match } = snapshot;
  const scene = match.screen_scene;

  return (
    <main className="screen-stage">
      <div className="screen-bg" />
      {scene === "idle" && <IdleScene snapshot={snapshot} />}
      {scene === "teams" && <TeamsScene snapshot={snapshot} />}
      {scene === "intermission" && <IntermissionScene snapshot={snapshot} />}
      {scene === "result" && <ResultScene snapshot={snapshot} />}
      {(scene === "live" || scene === "opening") && <LiveScene snapshot={snapshot} socketStatus={socketStatus} />}
    </main>
  );
}

function ScreenTop({ snapshot, label, socketStatus }: { snapshot: MatchSnapshot; label: string; socketStatus?: string }) {
  return (
    <header className="screen-top">
      <img src="/assets/logo-full-white.png" alt="中国科学院计算技术研究所" />
      <div className="screen-top-right">
        <StatusPill tone="gold">{label}</StatusPill>
        {socketStatus && <StatusPill tone={socketStatus === "open" ? "green" : "red"}>{socketStatus}</StatusPill>}
      </div>
    </header>
  );
}

function IdleScene({ snapshot }: { snapshot: MatchSnapshot }) {
  return (
    <section className="screen-scene hero-scene">
      <img className="hero-logo" src="/assets/logo-mark-white.png" alt="ICT" />
      <img className="hero-org" src="/assets/logo-full-white.png" alt="中国科学院计算技术研究所" />
      <h1>{snapshot.match.title.replace("中科院计算所", "")}</h1>
      <p>{snapshot.match.topic}</p>
      <div className="hero-vs">
        {snapshot.teams.map((team) => (
          <div className={`hero-team ${sideClass(team.side)}`} key={team.id}>
            <strong>{team.name}</strong>
            <span>{sideLabel(team.side)} · {team.position}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

function TeamsScene({ snapshot }: { snapshot: MatchSnapshot }) {
  return (
    <section className="screen-scene">
      <ScreenTop snapshot={snapshot} label="双方阵容" />
      <Topic snapshot={snapshot} />
      <div className="teams-presentation">
        {snapshot.teams.filter((team) => team.side !== "neutral").map((team) => {
          const side = team.side === "affirmative" ? "affirmative" : "negative";
          return <RosterPanel key={team.id} snapshot={snapshot} side={side} expanded />;
        })}
      </div>
    </section>
  );
}

function LiveScene({ snapshot, socketStatus }: { snapshot: MatchSnapshot; socketStatus: string }) {
  const phase = snapshot.phases.find((item) => item.id === snapshot.match.current_phase_id);
  const currentSpeaker = snapshot.speakers.find((item) => item.id === snapshot.current_speech?.speaker_id);
  const liveMode = snapshot.match.live_mode;

  return (
    <section className="screen-scene">
      <ScreenTop snapshot={snapshot} label={phase ? `环节 ${phase.display_order} / ${snapshot.phases.length}` : "比赛实况"} socketStatus={socketStatus} />
      <Topic snapshot={snapshot} />
      <div className="live-grid">
        <RosterPanel snapshot={snapshot} side="affirmative" activeSpeaker={currentSpeaker} />
        <div className="live-center">
          {liveMode === "prep" ? (
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
        <span>{sideLabel(side)}</span>
        <strong>{team?.name}</strong>
      </div>
      <p>{team?.position}</p>
      <div className="roster-list">
        {speakers.map((speaker) => (
          <div className={`roster-row ${activeSpeaker?.id === speaker.id ? "speaking" : ""}`} key={speaker.id}>
            <div className="avatar">{speaker.name.slice(0, 1)}</div>
            <div>
              <strong>{speaker.name}</strong>
              <span>{seatLabel(speaker.seat)}{speaker.model_name ? ` · ${speaker.model_name}` : ""}</span>
            </div>
            <em>{speaker.speaker_type === "agent" ? "AI" : "人类"}</em>
          </div>
        ))}
      </div>
    </aside>
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

function IntermissionScene({ snapshot }: { snapshot: MatchSnapshot }) {
  const voteUrl = `${window.location.origin}/vote/${encodeURIComponent(snapshot.match.id)}`;
  return (
    <section className="screen-scene hero-scene">
      <h1>评委合议</h1>
      <p>
        {snapshot.vote_state.window_status === "open" ? "学生扫码投票已开启" : "学生投票暂未开启"}
        {" · 当前收到 "}{snapshot.vote_state.audience_count} 票
      </p>
      <VoteQr url={voteUrl} />
      <div className="vote-url">{voteUrl}</div>
    </section>
  );
}

function ResultScene({ snapshot }: { snapshot: MatchSnapshot }) {
  const judgePublished = snapshot.vote_state.judge_published;
  const winner = snapshot.teams.find((team) => team.side === snapshot.vote_state.winner_side);
  const best = snapshot.speakers.find((speaker) => speaker.id === snapshot.vote_state.best_speaker_id);
  const judge = snapshot.vote_state.judge_summary;
  const audience = snapshot.vote_state.audience_summary;
  const topAudienceBest = snapshot.speakers.find((speaker) => speaker.id === audience.best_speaker[0]?.speaker_id);
  return (
    <section className="screen-scene result-scene">
      <ScreenTop snapshot={snapshot} label="比赛结果" />
      <div className="result-center">
        <span>{judgePublished ? "优胜方" : "评委合议中"}</span>
        {judgePublished ? (
          <>
            <h1>{sideLabel(snapshot.vote_state.winner_side)} · {winner?.name}</h1>
            <p>最佳辩手 <strong>{speakerLabel(best)}</strong></p>
          </>
        ) : (
          <>
            <h1>结果待公布</h1>
            <p>评委结果公布后展示优胜方与最佳辩手</p>
          </>
        )}
        <div className="result-ballots">
          <div>立论票 <b>{judgePublished ? `正 ${judge.constructive.affirmative} / 反 ${judge.constructive.negative}` : "待公布"}</b></div>
          <div>过程票 <b>{judgePublished ? `正 ${judge.process.affirmative} / 反 ${judge.process.negative}` : "待公布"}</b></div>
          <div>结辩票 <b>{judgePublished ? `正 ${judge.conclusion.affirmative} / 反 ${judge.conclusion.negative}` : "待公布"}</b></div>
          <div>
            同学投票
            <b>{snapshot.vote_state.audience_published ? `正 ${audience.winner.affirmative} / 反 ${audience.winner.negative}` : judgePublished ? "待公布" : "评委后公布"}</b>
          </div>
          {snapshot.vote_state.audience_published && (
            <div>同学最佳 <b>{speakerLabel(topAudienceBest)} · {audience.best_speaker[0]?.count ?? 0} 票</b></div>
          )}
        </div>
      </div>
    </section>
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
