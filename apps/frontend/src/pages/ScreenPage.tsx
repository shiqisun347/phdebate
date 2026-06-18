import { useEffect, useState } from "react";
import QRCode from "qrcode";
import { Volume2, VolumeX } from "lucide-react";
import { ClockTile } from "../components/ClockTile";
import { AuthPrompt } from "../components/AuthPrompt";
import { clockByName, seatLabel, sideClass, sideLabel, speakerLabel } from "../state/format";
import { resolveAvatar, defaultAvatarDataUri } from "../state/avatar";
import type { MatchInfo, MatchSnapshot, ScreenScene, Side, Speaker } from "../types/contracts";
import { useMatch } from "../realtime/useMatch";
import { usePlayback } from "../screen/usePlayback";
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
  const [audioEnabled, setAudioEnabled] = useState(() => window.localStorage.getItem("phdebate_screen_audio_enabled") === "1");

  useEffect(() => {
    window.localStorage.setItem("phdebate_screen_audio_enabled", audioEnabled ? "1" : "0");
  }, [audioEnabled]);

  // 大屏 TTS 播放：全部交给确定性的对账状态机（src/screen/playbackReducer.ts + usePlayback）。
  // 快照唯一真相、看门狗自愈、无 live MSE —— 历史上的卡死/超慢/停不下来在那里被单测覆盖。
  const { unlock } = usePlayback(matchId, snapshot, lastEvent, audioEnabled, setAudioEnabled);

  // 切换扬声器：开启时必须在「点击手势」内解锁音频（浏览器自动播放策略），否则程序化 play() 被拦。
  const toggleAudio = () => {
    const next = !audioEnabled;
    setAudioEnabled(next);
    if (next) unlock();
  };

  // 打铃提示音（与 TTS 播放无关，独立处理）。
  useEffect(() => {
    if (!lastEvent || lastEvent.type !== "clock.bell_triggered") return;
    const durationMs = Number(lastEvent.payload.duration_ms ?? 800);
    playBellCue(durationMs);
  }, [lastEvent]);

  if (!snapshot && loadError) return <AuthPrompt role="screen" message={loadError} />;
  if (!snapshot) return <div className="loading">正在连接大屏状态...</div>;
  return <ScreenView snapshot={snapshot} audioEnabled={audioEnabled} onToggleAudio={toggleAudio} />;
}

function ScreenView({ snapshot, audioEnabled, onToggleAudio }: { snapshot: MatchSnapshot; audioEnabled: boolean; onToggleAudio: () => void }) {
  // 空白起步（无比赛）：大屏显示候场提示，不渲染空赛场。
  if (!snapshot.match.id) {
    return (
      <main className="screen-stage">
        <div className="screen-bg" />
        <section className="screen-scene">
          <ScreenChrome match={snapshot.match} />
          <div className="live-grid">
            <div className="live-center">
              <div className="mode-panel idle-wait-mode">
                <div className="phase-name">候场</div>
                <h3>等待创建比赛</h3>
                <p>请在控制台「比赛管理」新建比赛后开始。</p>
              </div>
            </div>
          </div>
        </section>
      </main>
    );
  }
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
      <button
        className={`screen-audio-btn ${audioEnabled ? "active" : ""}`}
        onClick={onToggleAudio}
        title={audioEnabled ? "关闭扬声器（AI发言朗读）" : "开启扬声器（AI发言朗读）"}
      >
        {audioEnabled ? <Volume2 size={14} /> : <VolumeX size={14} />}
      </button>
    </main>
  );
}

function ScreenChrome({ match }: { match: MatchInfo }) {
  // 左上角=比赛名称（图片/文本，来自比赛管理，实时同步）；右上角=主办机构（图片/文本），
  // 主办机构未设置时回退为主办方 logo。
  const titleImage = match.title_display === "image" && match.title_image_url ? match.title_image_url : "";
  const organizerImage = match.organizer_display === "image" && match.organizer_image_url ? match.organizer_image_url : "";
  return (
    <header className="screen-top">
      {titleImage ? (
        <img className="screen-event-logo" src={titleImage} alt={match.title || "比赛名称"} />
      ) : (
        <div className="screen-event-wordmark">{match.title || "人机辩论赛"}</div>
      )}
      {organizerImage ? (
        <img className="screen-event-logo" src={organizerImage} alt={match.organizer || "主办机构"} />
      ) : match.organizer ? (
        <div className="screen-event-organizer">{match.organizer}</div>
      ) : (
        <img src="/assets/logo-full-white.png" alt="中国科学院计算技术研究所" />
      )}
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
      <ScreenChrome match={snapshot.match} />
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
      <ScreenChrome match={snapshot.match} />
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
  const speaking = isSpeakingNow(snapshot);
  return (
    <section className="screen-scene teams-scene">
      <ScreenChrome match={snapshot.match} />
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
  // 自由辩论里"单个辩手发言结束、等待主持确认下一轮"（free_turn_next）只是阶段内的小过渡，
  // 不应跳到整阶段的「发言结束」大页——那只在某一方总计时归零（next_action=phase_next）时才出现。
  // 因此这种过渡仍留在 FreeMode 内显示一个小的"xxx 发言结束"。
  const freeTurnHandoff = snapshot.flow.awaiting_host_confirm && snapshot.flow.next_action === "free_turn_next";

  return (
    <section className="screen-scene">
      <ScreenChrome match={snapshot.match} />
      <Topic snapshot={snapshot} />
      <div className="live-grid">
        <RosterPanel snapshot={snapshot} side="affirmative" activeSpeaker={currentSpeaker} />
        <div className="live-center">
          {snapshot.flow.awaiting_host_confirm && !freeTurnHandoff ? (
            <FlowWaitMode snapshot={snapshot} />
          ) : liveMode === "prep" ? (
            <PrepMode speaker={currentSpeaker} phaseName={phase?.name ?? "AI 准备"} />
          ) : liveMode === "free" ? (
            <FreeMode snapshot={snapshot} currentSpeaker={currentSpeaker} />
          ) : (
            <SingleMode snapshot={snapshot} currentSpeaker={currentSpeaker} phaseName={phase?.name ?? "当前环节"} phaseSide={phase?.side} />
          )}
        </div>
        <RosterPanel snapshot={snapshot} side="negative" activeSpeaker={currentSpeaker} />
      </div>
    </section>
  );
}

function PausedScene({ snapshot }: { snapshot: MatchSnapshot }) {
  const phase = snapshot.phases.find((item) => item.id === snapshot.match.current_phase_id);
  return (
    <section className="screen-scene paused-scene">
      <ScreenChrome match={snapshot.match} />
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
      <ScreenChrome match={snapshot.match} />
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
      <ScreenChrome match={snapshot.match} />
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
      <ScreenChrome match={snapshot.match} />
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
      <ScreenChrome match={snapshot.match} />
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
      <ScreenChrome match={snapshot.match} />
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
      <ScreenChrome match={snapshot.match} />
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
              <div className="roster-person-header">
                <span className="roster-seat-badge">{seatLabel(speaker.seat)}</span>
                <strong>{speaker.name}</strong>
              </div>
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
  const phase =
    snapshot.phases.find((item) => item.id === snapshot.flow.phase_id) ??
    snapshot.phases.find((item) => item.id === snapshot.match.current_phase_id);
  return (
    <div className="mode-panel flow-wait-mode">
      <div className="phase-name">{phase?.name ?? "当前环节"}</div>
      <h3>发言完毕</h3>
    </div>
  );
}

function SingleMode({ snapshot, currentSpeaker, phaseName, phaseSide }: { snapshot: MatchSnapshot; currentSpeaker?: Speaker; phaseName: string; phaseSide?: Side }) {
  const completedSpeaker = !currentSpeaker ? lastCompletedSpeaker(snapshot) : undefined;
  const tone = (currentSpeaker?.side ?? completedSpeaker?.side ?? phaseSide) === "negative" ? "neg" : "aff";
  const thinking = Boolean(currentSpeaker && snapshot.current_speech?.state === "thinking");
  return (
    <div className="mode-panel single-mode">
      {currentSpeaker && (
        <SpeakingAvatar
          src={resolveAvatar(currentSpeaker)}
          name={currentSpeaker.name}
          side={currentSpeaker.side}
          lively={currentSpeaker.speaker_type === "agent"}
          active={isSpeakingNow(snapshot)}
          size="md"
        />
      )}
      <div className="phase-name">{phaseName}</div>
      <p>{currentSpeaker ? `${thinking ? "思考中" : "当前发言"} · ${speakerLabel(currentSpeaker)}` : completedSpeaker ? "发言完毕" : "当前发言 · 等待开始"}</p>
      <ClockTile label="本环节剩余" clock={clockByName(snapshot.clocks, "main")} tone={tone} />
    </div>
  );
}

function FreeMode({ snapshot, currentSpeaker }: { snapshot: MatchSnapshot; currentSpeaker?: Speaker }) {
  const thinking = Boolean(currentSpeaker && snapshot.current_speech?.state === "thinking");
  // 轮间空档（无人发言、2 秒窗口/等待对方开始）：显示上一位刚说完的小「xxx 发言结束」，保留双方计时
  // 与轮次信息（阶段仍在继续）。自由辩论轮内已全自动、不再 awaiting_host_confirm，故改用「本阶段
  // 最近一段定稿」来判断刚结束的发言人；本阶段还没人说过则显示「等待开始」（不串到上一环节）。
  const freePhaseId = snapshot.match.current_phase_id;
  const lastFreeFinal = !currentSpeaker
    ? snapshot.recent_transcript.find((seg) => seg.is_final && seg.phase_id === freePhaseId)
    : undefined;
  const finishedSpeaker = lastFreeFinal
    ? snapshot.speakers.find((item) => item.id === lastFreeFinal.speaker_id)
    : undefined;
  return (
    <div className="mode-panel free-mode">
      {currentSpeaker && (
        <SpeakingAvatar
          src={resolveAvatar(currentSpeaker)}
          name={currentSpeaker.name}
          side={currentSpeaker.side}
          lively={currentSpeaker.speaker_type === "agent"}
          active={isSpeakingNow(snapshot)}
          size="sm"
        />
      )}
      <div className="phase-name">自由辩论</div>
      <p>
        {currentSpeaker
          ? `${thinking ? "思考中" : "当前发言"} · ${speakerLabel(currentSpeaker)}`
          : finishedSpeaker
          ? `${speakerLabel(finishedSpeaker)} 发言结束`
          : "当前发言 · 等待开始"}
      </p>
      <div className="free-clocks">
        <ClockTile label="正方剩余" clock={clockByName(snapshot.clocks, "affirmative_total")} tone="aff" />
        <ClockTile label="单次上限" clock={clockByName(snapshot.clocks, "turn")} tone="turn" />
        <ClockTile label="反方剩余" clock={clockByName(snapshot.clocks, "negative_total")} tone="neg" />
      </div>
      <p>当前轮次方 · {sideLabel(snapshot.free_debate.current_turn_side)} · 第 {snapshot.free_debate.turn_index} 轮</p>
    </div>
  );
}

function isSpeakingNow(snapshot: MatchSnapshot): boolean {
  return snapshot.current_speech?.state === "speaking";
}

function lastCompletedSpeaker(snapshot: MatchSnapshot): Speaker | undefined {
  const segment = snapshot.recent_transcript.find((item) => item.is_final);
  if (!segment) return undefined;
  return snapshot.speakers.find((item) => item.id === segment.speaker_id);
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
