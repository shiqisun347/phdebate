import { useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import { ClockTile } from "../components/ClockTile";
import { AuthPrompt } from "../components/AuthPrompt";
import { StatusPill } from "../components/StatusPill";
import { clockByName, seatLabel, sideClass, sideLabel, speakerLabel } from "../state/format";
import { resolveAvatar, defaultAvatarDataUri } from "../state/avatar";
import type { MatchSnapshot, ScreenScene, Side, Speaker } from "../types/contracts";
import { useMatch } from "../realtime/useMatch";
import { playBellCue } from "../utils/audioCue";

interface ScreenPageProps {
  matchId: string;
}

type RuntimeScreenScene =
  | "idle"
  | "opening"
  | "teams"
  | "live"
  | "paused"
  | "xiaoqi_commentary"
  | "xiaoqi_result"
  | "judge_commentary"
  | "judge_result"
  | "audience_result"
  | "acknowledgment";

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
      {scene === "opening" && <OpeningScene snapshot={snapshot} />}
      {scene === "teams" && <TeamsScene snapshot={snapshot} />}
      {scene === "paused" && <PausedScene snapshot={snapshot} />}
      {scene === "xiaoqi_commentary" && <XiaoqiSpeakingScene snapshot={snapshot} kicker="小七点评" title="小七正在点评" />}
      {scene === "xiaoqi_result" && <XiaoqiResultScene snapshot={snapshot} />}
      {scene === "judge_commentary" && <JudgeCommentaryScene snapshot={snapshot} />}
      {scene === "judge_result" && <JudgeResultScene snapshot={snapshot} />}
      {scene === "audience_result" && <AudienceResultScene snapshot={snapshot} />}
      {scene === "acknowledgment" && <AcknowledgmentScene snapshot={snapshot} />}
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

/* ----------------------------- 形象 + 声波动效 ----------------------------- */
function SpeakingAvatar({
  src,
  name,
  side,
  lively,
  active,
  size = "md",
  caption,
}: {
  src: string;
  name: string;
  side?: Side;
  lively?: boolean;
  active?: boolean;
  size?: "sm" | "md" | "lg";
  caption?: React.ReactNode;
}) {
  const bars = lively ? 9 : 6;
  return (
    <div className={`speaking-avatar ${size} ${sideClass(side ?? "neutral")} ${lively ? "lively" : ""} ${active ? "is-active" : "is-idle"}`}>
      <div className="sa-portrait">
        <span className="sa-ring" />
        <span className="sa-ring two" />
        <img src={src} alt={name} />
      </div>
      <div className="sa-wave" aria-hidden="true">
        {Array.from({ length: bars }).map((_, i) => (
          <i key={i} style={{ animationDelay: `${(i % bars) * 0.08}s` }} />
        ))}
      </div>
      {caption && <div className="sa-caption">{caption}</div>}
    </div>
  );
}

function xiaoqiAvatar(snapshot: MatchSnapshot): string {
  const xq = snapshot.xiaoqi;
  if (xq?.image_url?.trim()) return xq.image_url;
  return defaultAvatarDataUri("agent", "neutral", xq?.name || "小七");
}

/* ----------------------------- 候场 ----------------------------- */
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
          </div>
        </div>
        <RosterPanel snapshot={snapshot} side="negative" />
      </div>
    </section>
  );
}

/* ----------------------------- 辩题介绍 ----------------------------- */
function OpeningScene({ snapshot }: { snapshot: MatchSnapshot }) {
  return (
    <section className="screen-scene opening-scene">
      <ScreenChrome />
      <div className="opening-center">
        <span className="opening-kicker">本场辩题</span>
        <h1 className="opening-topic">{snapshot.match.topic}</h1>
        <div className="opening-sides">
          <div className="opening-side aff">
            <span>正方</span>
            <strong>{snapshot.teams.find((t) => t.side === "affirmative")?.position}</strong>
          </div>
          <div className="opening-vs">VS</div>
          <div className="opening-side neg">
            <span>反方</span>
            <strong>{snapshot.teams.find((t) => t.side === "negative")?.position}</strong>
          </div>
        </div>
      </div>
    </section>
  );
}

/* ----------------------------- 阵容介绍（重新设计） ----------------------------- */
function TeamsScene({ snapshot }: { snapshot: MatchSnapshot }) {
  const currentSpeaker = snapshot.speakers.find((item) => item.id === snapshot.current_speech?.speaker_id);
  const speaking = Boolean(snapshot.current_speech && snapshot.current_speech.state !== "ended");
  return (
    <section className="screen-scene teams-scene">
      <ScreenChrome />
      <Topic snapshot={snapshot} />
      <div className="live-grid">
        <RosterPanel snapshot={snapshot} side="affirmative" activeSpeaker={currentSpeaker} />
        <div className="live-center">
          {currentSpeaker ? (
            <div className="teams-spotlight">
              <SpeakingAvatar
                src={resolveAvatar(currentSpeaker)}
                name={currentSpeaker.name}
                side={currentSpeaker.side}
                lively={currentSpeaker.speaker_type === "agent"}
                active={speaking}
                size="lg"
              />
              <div className="teams-spotlight-meta">
                <span>{sideLabel(currentSpeaker.side)}{seatLabel(currentSpeaker.seat)} 自我介绍</span>
                <strong>{currentSpeaker.name}</strong>
                <em>{currentSpeaker.speaker_type === "agent" ? currentSpeaker.model_name || "AI 辩手" : "人类辩手"}</em>
              </div>
            </div>
          ) : (
            <div className="mode-panel teams-intro">
              <div className="phase-name">阵容介绍</div>
              <h3>本场辩手依次登场</h3>
            </div>
          )}
        </div>
        <RosterPanel snapshot={snapshot} side="negative" activeSpeaker={currentSpeaker} />
      </div>
    </section>
  );
}

/* ----------------------------- 比赛实况（改进） ----------------------------- */
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

/* ----------------------------- 小七点评（重新设计，无字幕窗口） ----------------------------- */
function XiaoqiSpeakingScene({ snapshot, kicker, title }: { snapshot: MatchSnapshot; kicker: string; title: string }) {
  return (
    <section className="screen-scene xiaoqi-scene">
      <ScreenChrome />
      <Topic snapshot={snapshot} />
      <div className="xiaoqi-center">
        <SpeakingAvatar
          src={xiaoqiAvatar(snapshot)}
          name={snapshot.xiaoqi?.name || "小七"}
          side="neutral"
          lively
          active
          size="lg"
        />
        <span className="xiaoqi-kicker">{kicker}</span>
        <h1 className="xiaoqi-title">{title}</h1>
        <p className="xiaoqi-name">{snapshot.xiaoqi?.name || "小七"} · 智能裁判</p>
      </div>
    </section>
  );
}

/* ----------------------------- 小七评判（获胜方 + 最佳辩手，无理由） ----------------------------- */
function XiaoqiResultScene({ snapshot }: { snapshot: MatchSnapshot }) {
  const vs = snapshot.vote_state;
  const published = vs.judge_published;
  const winner = snapshot.teams.find((team) => team.side === vs.winner_side);
  const best = snapshot.speakers.find((speaker) => speaker.id === vs.best_speaker_id);
  return (
    <section className="screen-scene official-result-scene">
      <ScreenChrome />
      <div className="result-center official-result">
        <div className="xiaoqi-result-head">
          <img className="xiaoqi-result-avatar" src={xiaoqiAvatar(snapshot)} alt={snapshot.xiaoqi?.name || "小七"} />
          <span>{snapshot.xiaoqi?.name || "小七"} 评判结果</span>
        </div>
        {published && vs.winner_side ? (
          <>
            <h1>{sideLabel(vs.winner_side)} · {winner?.name}</h1>
            <div className="best-speaker-award">
              <div className="best-speaker-label">最佳辩手</div>
              <div className="best-speaker-name">{speakerLabel(best)}</div>
            </div>
          </>
        ) : (
          <>
            <h1>结果待公布</h1>
            <p>请等待小七给出评判</p>
          </>
        )}
      </div>
    </section>
  );
}

/* ----------------------------- 致谢环节（一句致谢语） ----------------------------- */
function AcknowledgmentScene({ snapshot }: { snapshot: MatchSnapshot }) {
  return (
    <section className="screen-scene thanks-scene">
      <ScreenChrome />
      <div className="thanks-center">
        <span className="thanks-kicker">致谢</span>
        <h1 className="thanks-line">感谢各位的参与，我们下次再见</h1>
        <p className="thanks-topic">{snapshot.match.topic}</p>
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
            <img className="roster-avatar" src={resolveAvatar(speaker)} alt={speaker.name} />
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
    <div className="mode-panel single-mode">
      {currentSpeaker && (
        <SpeakingAvatar
          src={resolveAvatar(currentSpeaker)}
          name={currentSpeaker.name}
          side={currentSpeaker.side}
          lively={currentSpeaker.speaker_type === "agent"}
          active={Boolean(snapshot.current_speech && snapshot.current_speech.state !== "ended")}
          size="md"
        />
      )}
      <div className="phase-name">{phaseName}</div>
      <p>当前发言 · {speakerLabel(currentSpeaker)}</p>
      <ClockTile label="本环节剩余" clock={clockByName(snapshot.clocks, "main")} tone={currentSpeaker?.side === "negative" ? "neg" : "aff"} />
    </div>
  );
}

function FreeMode({ snapshot, currentSpeaker }: { snapshot: MatchSnapshot; currentSpeaker?: Speaker }) {
  return (
    <div className="mode-panel free-mode">
      {currentSpeaker && (
        <SpeakingAvatar
          src={resolveAvatar(currentSpeaker)}
          name={currentSpeaker.name}
          side={currentSpeaker.side}
          lively={currentSpeaker.speaker_type === "agent"}
          active={Boolean(snapshot.current_speech && snapshot.current_speech.state !== "ended")}
          size="sm"
        />
      )}
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
      {speaker ? (
        <SpeakingAvatar
          src={resolveAvatar(speaker)}
          name={speaker.name}
          side={speaker.side}
          lively={speaker.speaker_type === "agent"}
          active={false}
          size="md"
        />
      ) : (
        <div className="prep-orb"><i /><i /></div>
      )}
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
  const textRef = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    // 单行字幕：流式时把最新文字滚到可见末端。
    if (textRef.current) textRef.current.scrollLeft = textRef.current.scrollWidth;
  }, [text]);
  return (
    <footer className="subtitle-panel one-line">
      <div className="subtitle-head">
        <strong>{currentSpeaker ? speakerLabel(currentSpeaker) : segment?.speaker_label ?? "等待指定"}</strong>
        <StatusPill tone={isAgent ? "blue" : "green"}>{isAgent ? "AI 发言" : "实时转写"}</StatusPill>
        {degraded && <StatusPill tone="red">TTS 降级</StatusPill>}
        {asrFailed && <StatusPill tone="red">ASR 异常</StatusPill>}
      </div>
      <p ref={textRef}>{text}</p>
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
  switch (scene) {
    case "opening":
      return "opening";
    case "teams":
      return "teams";
    case "idle":
      return "idle";
    case "paused":
      return "paused";
    case "xiaoqi_commentary":
      return "xiaoqi_commentary";
    case "xiaoqi_result":
      return "xiaoqi_result";
    case "intermission":
    case "judge_commentary":
      return "judge_commentary";
    case "result":
    case "judge_result":
      return "judge_result";
    case "audience_result":
      return "audience_result";
    case "acknowledgment":
      return "acknowledgment";
    case "live":
    default:
      return "live";
  }
}
