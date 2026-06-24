import { useEffect, useState } from "react";
import QRCode from "qrcode";
import { Volume2 } from "lucide-react";
import { ClockTile } from "../components/ClockTile";
import { AuthPrompt } from "../components/AuthPrompt";
import { clockByName, seatLabel, sideClass, sideLabel, speakerLabel } from "../state/format";
import { resolveAvatar, defaultAvatarDataUri } from "../state/avatar";
import type { MatchInfo, MatchSnapshot, ScreenScene, Side, Speaker } from "../types/contracts";
import { useLiveKitAudio } from "../livekit/useLiveKitAudio";
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
  | "debate_process"
  | "paused"
  | "audience_vote"
  | "xiaoqi_commentary"
  | "xiaoqi_result"
  | "judge_commentary"
  | "judge_result"
  | "audience_result"
  | "acknowledgment";

export function ScreenPage({ matchId }: ScreenPageProps) {
  const { snapshot, loadError, lastEvent } = useMatch(matchId, "screen");
  const [audioEnabled, setAudioEnabled] = useState(true);
  // 本次页面加载是否已通过「点击手势」解锁音频。浏览器自动播放策略要求每次加载都需一次手势，
  // localStorage 里的开关状态会跨刷新保留，但手势解锁不会 —— 因此用独立的会话级状态跟踪。
  const [audioUnlocked, setAudioUnlocked] = useState(false);

  useEffect(() => {
    window.localStorage.setItem("phdebate_screen_audio_enabled", audioEnabled ? "1" : "0");
  }, [audioEnabled]);

  const livekitAudio = useLiveKitAudio({ matchId, role: "screen", enabled: audioEnabled });
  const livekitHasAudio = livekitAudio.status === "connected" && livekitAudio.audioTrackCount > 0;

  // 大屏 TTS 播放：全部交给确定性的对账状态机（src/screen/playbackReducer.ts + usePlayback）。
  // 快照唯一真相、看门狗自愈、无 live MSE —— 历史上的卡死/超慢/停不下来在那里被单测覆盖。
  // LiveKit 只有真正订阅到 voice-agent/AI TTS 音轨时才接管声音；人类辩手麦克风不会在大屏外放，
  // 也不会关闭后端归档音频兜底播放。
  const { unlock } = usePlayback(matchId, snapshot, lastEvent, audioEnabled && !livekitHasAudio, setAudioEnabled, () => {
    setAudioEnabled(true);
    setAudioUnlocked(false);
  });

  // 全屏遮罩：开关为「开」但本次加载尚未手势解锁 → 引导操作员开赛前先点一次，避免第一段被浏览器拦截。
  const enableAudioFromGate = () => {
    setAudioEnabled(true);
    unlock();
    setAudioUnlocked(true);
  };

  // 打铃提示音（与 TTS 播放无关，独立处理）。
  useEffect(() => {
    if (!lastEvent || lastEvent.type !== "clock.bell_triggered") return;
    const durationMs = Number(lastEvent.payload.duration_ms ?? 800);
    playBellCue(durationMs);
  }, [lastEvent]);

  if (!snapshot && loadError) return <AuthPrompt role="screen" message={loadError} />;
  if (!snapshot) return <div className="loading">正在连接大屏状态...</div>;
  return (
    <>
      <ScreenView snapshot={snapshot} />
      {audioEnabled && !audioUnlocked && <AudioUnlockGate onEnable={enableAudioFromGate} />}
    </>
  );
}

function AudioUnlockGate({ onEnable }: { onEnable: () => void }) {
  return (
    <div className="screen-audio-gate">
      <button type="button" className="screen-audio-gate-btn" onClick={onEnable} autoFocus>
        <Volume2 size={28} />
        <span>启用现场声音</span>
      </button>
    </div>
  );
}

function ScreenView({ snapshot }: { snapshot: MatchSnapshot }) {
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
      {scene === "audience_vote" && <AudienceVoteScene snapshot={snapshot} />}
      {scene === "xiaoqi_commentary" && <XiaoqiSpeakingScene snapshot={snapshot} kicker="小七点评" title="小七正在点评" />}
      {scene === "xiaoqi_result" && <XiaoqiResultScene snapshot={snapshot} />}
      {scene === "judge_commentary" && <JudgeCommentaryScene snapshot={snapshot} />}
      {scene === "judge_result" && <JudgeResultScene snapshot={snapshot} />}
      {scene === "audience_result" && <AudienceResultScene snapshot={snapshot} />}
      {scene === "acknowledgment" && <AcknowledgmentScene snapshot={snapshot} />}
      {scene === "debate_process" && <DebateProcessScene snapshot={snapshot} />}
      {scene === "live" && <LiveScene snapshot={snapshot} />}
    </main>
  );
}

function ScreenChrome({ match }: { match: MatchInfo }) {
  // 左上角=比赛名称：logo 与文字可同时设置，logo 显示在文字左边（任一为空则只显示另一个）。
  // 右上角=主办机构（图片/文本），主办机构未设置时回退为主办方 logo。
  const titleLogo = match.title_image_url || "";
  const titleText = match.title || "";
  const organizerImage = match.organizer_display === "image" && match.organizer_image_url ? match.organizer_image_url : "";
  return (
    <header className="screen-top">
      <div className="screen-event-title">
        {titleLogo && <img className="screen-title-logo" src={titleLogo} alt={titleText || "比赛 logo"} />}
        {titleText ? (
          <div className="screen-event-wordmark">{titleText}</div>
        ) : (
          !titleLogo && <div className="screen-event-wordmark">人机辩论赛</div>
        )}
      </div>
      {organizerImage ? (
        <img className="screen-organizer-logo" src={organizerImage} alt={match.organizer || "主办机构"} />
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
      {caption && <div className="sa-caption">{caption}</div>}
      <div className="sa-wave" aria-hidden="true">
        {Array.from({ length: bars }).map((_, i) => (
          <i key={i} style={{ animationDelay: `${(i % bars) * 0.08}s` }} />
        ))}
      </div>
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
  const showPrepMode = liveMode === "prep" && currentSpeaker?.speaker_type === "agent";
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
          ) : showPrepMode ? (
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

function DebateProcessScene({ snapshot }: { snapshot: MatchSnapshot }) {
  const phase = snapshot.phases.find((item) => item.id === snapshot.match.current_phase_id);
  const items = snapshot.recent_transcript
    .filter((item) => item.valid !== false && item.text.trim())
    .slice(0, 14)
    .reverse();

  return (
    <section className="screen-scene debate-process-scene">
      <ScreenChrome match={snapshot.match} />
      <Topic snapshot={snapshot} />
      <div className="debate-process-shell">
        <div className="debate-process-head">
          <span>当前辩论过程</span>
          <strong>{phase?.name ?? "实时记录"}</strong>
        </div>
        <div className="debate-process-list">
          {items.length > 0 ? (
            items.map((item) => {
              const speaker = snapshot.speakers.find((entry) => entry.id === item.speaker_id);
              const side = speaker?.side ?? "neutral";
              return (
                <article className={`debate-process-item ${sideClass(side)} ${item.is_final ? "final" : "partial"}`} key={item.id}>
                  <img src={speaker ? resolveAvatar(speaker) : defaultAvatarDataUri("human", "neutral", item.speaker_label)} alt={item.speaker_label} />
                  <div>
                    <header>
                      <strong>{speaker ? speakerLabel(speaker) : item.speaker_label}</strong>
                      <span>{item.is_final ? "定稿" : "实时"}</span>
                    </header>
                    <p>{item.text}</p>
                  </div>
                </article>
              );
            })
          ) : (
            <div className="debate-process-empty">暂无辩论记录，开始发言后会在这里滚动展示。</div>
          )}
        </div>
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
  const summary = vs.xiaoqi_summary ?? { winner_side: vs.winner_side, best_speaker_id: vs.best_speaker_id };
  const published = vs.xiaoqi_recorded;
  const winner = snapshot.teams.find((team) => team.side === summary.winner_side);
  const best = snapshot.speakers.find((speaker) => speaker.id === summary.best_speaker_id);
  return (
    <section className="screen-scene official-result-scene">
      <ScreenChrome match={snapshot.match} />
      <div className="result-center official-result">
        <div className="xiaoqi-result-head">
          <img className="xiaoqi-result-avatar" src={xiaoqiAvatar(snapshot)} alt={snapshot.xiaoqi?.name || "小七"} />
          <span>{snapshot.xiaoqi?.name || "小七"} 评判结果</span>
        </div>
        {published && summary.winner_side ? (
          <>
            <h1>{sideLabel(summary.winner_side)} · {winner?.name}</h1>
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
  return (
    <section className="screen-scene">
      <ScreenChrome match={snapshot.match} />
      <Topic snapshot={snapshot} />
      <div className="live-grid commentary-grid">
        <RosterPanel snapshot={snapshot} side="affirmative" />
        <div className="commentary-panel">
          <span>赛后环节</span>
          <h1>评委点评</h1>
          <p>评委正在对本场辩论进行点评与合议。</p>
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

function AudienceVoteScene({ snapshot }: { snapshot: MatchSnapshot }) {
  const voteUrl = `${window.location.origin}/vote`;
  const open = snapshot.vote_state.window_status === "open";
  return (
    <section className="screen-scene audience-vote-scene">
      <ScreenChrome match={snapshot.match} />
      <Topic snapshot={snapshot} />
      <div className="live-grid audience-vote-grid">
        <RosterPanel snapshot={snapshot} side="affirmative" />
        <div className="live-center audience-vote-center">
          <div className="av-body">
            <div className="av-kicker">观众投票</div>
            <h1 className="av-title">扫码为你心中的胜方与最佳辩手投票</h1>
            <div className="av-qr-wrap">
              <VoteQr url={voteUrl} size={360} />
              <div className={`av-status ${open ? "open" : "closed"}`}>{open ? "投票进行中" : "投票即将开启"}</div>
            </div>
            <div className="av-url">{voteUrl}</div>
            <div className="av-count">
              已收到 <strong>{snapshot.vote_state.audience_count}</strong> 票
            </div>
            {!open && <div className="av-hint">请等待主持人点击「开始观众投票」后即可提交</div>}
          </div>
        </div>
        <RosterPanel snapshot={snapshot} side="negative" />
      </div>
    </section>
  );
}

function VsGloves() {
  // 两只带角度对撞的拳击手套（蓝=正方 / 紫=反方）+ 金色冲击星芒，替代 "VS"。
  const glove = (grad: string) => (
    <g stroke="rgba(255,255,255,0.85)" strokeWidth="1.8" strokeLinejoin="round" strokeLinecap="round">
      {/* 护腕 */}
      <rect x="2" y="24" width="15" height="26" rx="7" fill={`url(#${grad})`} />
      <line x1="3" y1="37" x2="16" y2="37" stroke="rgba(255,255,255,0.4)" strokeWidth="2.4" />
      {/* 拳套主体（含拇指） */}
      <path
        d="M14 18 C 31 9 53 14 55 33 C 56 49 43 55 31 53 C 25 52 22 48 23 43 C 16 48 8 44 9 35 C 10 28 18 28 23 34 C 18 26 12 22 14 18 Z"
        fill={`url(#${grad})`}
      />
      {/* 指节棱线（拳击手套标志） */}
      <path d="M45 21 q6 11 -2 23" fill="none" stroke="rgba(255,255,255,0.45)" strokeWidth="1.8" />
      <path d="M38 20 q6 12 -2 24" fill="none" stroke="rgba(255,255,255,0.32)" strokeWidth="1.6" />
      {/* 拇指折痕 */}
      <path d="M24 35 q-4 4 -1 9" fill="none" stroke="rgba(255,255,255,0.4)" strokeWidth="1.6" />
    </g>
  );
  return (
    <svg className="ar-vs-gloves-svg" viewBox="0 0 156 84" width="150" height="80" aria-label="对决">
      <defs>
        <linearGradient id="ar-glove-aff" x1="0" y1="0" x2="0.7" y2="1">
          <stop offset="0" stopColor="#9bd6ff" />
          <stop offset="1" stopColor="#2f7fd6" />
        </linearGradient>
        <linearGradient id="ar-glove-neg" x1="0" y1="0" x2="0.7" y2="1">
          <stop offset="0" stopColor="#dcc0ff" />
          <stop offset="1" stopColor="#7a4fd0" />
        </linearGradient>
        <radialGradient id="ar-spark" cx="0.5" cy="0.5" r="0.5">
          <stop offset="0" stopColor="#fff7d6" />
          <stop offset="0.5" stopColor="#ffd45e" />
          <stop offset="1" stopColor="#ffd45e" stopOpacity="0" />
        </radialGradient>
      </defs>
      {/* 冲击星芒 */}
      <g transform="translate(78,46)">
        <path d="M0 -22 L6 -7 L22 -10 L10 2 L16 19 L0 7 L-16 19 L-10 2 L-22 -10 L-6 -7 Z" fill="url(#ar-spark)" />
        <circle r="7" fill="#fff" opacity="0.92" />
      </g>
      {/* 左拳（正方），向右下倾斜对撞 */}
      <g transform="translate(8,12) rotate(14 32 36)">{glove("ar-glove-aff")}</g>
      {/* 右拳（反方），镜像后向左下倾斜对撞 */}
      <g transform="translate(148,12) scale(-1,1) rotate(14 32 36)">{glove("ar-glove-neg")}</g>
    </svg>
  );
}

function SideEmblem({ side }: { side: Side }) {
  const aff = side === "affirmative";
  const gid = `ar-emblem-${side}`;
  return (
    <svg className="ar-emblem" viewBox="0 0 48 56" width="52" height="60" aria-hidden="true">
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={aff ? "#7fc8ff" : "#cfa6ff"} />
          <stop offset="1" stopColor={aff ? "#2f7fd6" : "#7a4fd0"} />
        </linearGradient>
      </defs>
      <path
        d="M24 2 L44 10 V27 C44 41 35 50 24 54 C13 50 4 41 4 27 V10 Z"
        fill={`url(#${gid})`}
        stroke="rgba(255,255,255,0.55)"
        strokeWidth="1.6"
      />
      <text x="24" y="34" textAnchor="middle" fontSize="22" fontWeight="900" fill="#fff" fontFamily="'Noto Serif SC',serif">
        {aff ? "正" : "反"}
      </text>
    </svg>
  );
}

function ArAvatar({ speaker, className }: { speaker: Speaker; className: string }) {
  return (
    <span className={className}>
      {speaker.image_url ? <img src={speaker.image_url} alt={speaker.name} /> : <span>{speaker.name.slice(0, 1)}</span>}
    </span>
  );
}

function CrownIcon() {
  return (
    <svg className="ar-crown" viewBox="0 0 64 50" width="50" height="39" aria-hidden="true">
      <defs>
        <linearGradient id="ar-crown-g" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#ffefb8" />
          <stop offset="0.5" stopColor="#ffce53" />
          <stop offset="1" stopColor="#f0a01e" />
        </linearGradient>
      </defs>
      <path d="M6 41 L10 16 L23 29 L32 7 L41 29 L54 16 L58 41 Z" fill="url(#ar-crown-g)" stroke="#b9791a" strokeWidth="1.6" strokeLinejoin="round" />
      <rect x="6" y="40" width="52" height="8" rx="2.5" fill="url(#ar-crown-g)" stroke="#b9791a" strokeWidth="1.6" />
      <circle cx="10" cy="14" r="3.4" fill="#fff4d2" stroke="#b9791a" strokeWidth="1" />
      <circle cx="32" cy="5" r="3.8" fill="#fff4d2" stroke="#b9791a" strokeWidth="1" />
      <circle cx="54" cy="14" r="3.4" fill="#fff4d2" stroke="#b9791a" strokeWidth="1" />
      <circle cx="20" cy="44" r="2" fill="#fff" opacity="0.85" />
      <circle cx="32" cy="44" r="2" fill="#fff" opacity="0.85" />
      <circle cx="44" cy="44" r="2" fill="#fff" opacity="0.85" />
    </svg>
  );
}

function TrophyIcon({ flip }: { flip?: boolean }) {
  return (
    <svg className={`ar-winner-trophy ${flip ? "ar-flip" : ""}`} viewBox="0 0 48 48" width="36" height="36" aria-hidden="true">
      <defs>
        <linearGradient id="ar-trophy-g" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#ffefb8" />
          <stop offset="1" stopColor="#f0a01e" />
        </linearGradient>
      </defs>
      <path d="M15 6 H33 V17 A9 9 0 0 1 15 17 Z" fill="url(#ar-trophy-g)" stroke="#b9791a" strokeWidth="1.6" strokeLinejoin="round" />
      <path d="M15 9 H8 V13 A7 7 0 0 0 15 19" fill="none" stroke="#b9791a" strokeWidth="2.2" />
      <path d="M33 9 H40 V13 A7 7 0 0 1 33 19" fill="none" stroke="#b9791a" strokeWidth="2.2" />
      <rect x="22" y="25" width="4" height="6" fill="#cf9320" />
      <rect x="15" y="31" width="18" height="5" rx="2" fill="url(#ar-trophy-g)" stroke="#b9791a" strokeWidth="1.2" />
      <rect x="12" y="37" width="24" height="6" rx="2.5" fill="url(#ar-trophy-g)" stroke="#b9791a" strokeWidth="1.2" />
    </svg>
  );
}

type ArRankItem = { speaker_id: string; count: number; speaker: Speaker };

function DebaterRow({ item, rank, champion }: { item: ArRankItem; rank: number; champion: boolean }) {
  const sp = item.speaker;
  return (
    <div className={`ar-row ${champion ? "ar-row-champ" : ""} ar-${sp.side}`}>
      {champion ? <span className="ar-best-ribbon"><b>最佳</b><b>辩手</b></span> : <span className="ar-rank-no">{rank}</span>}
      <span className="ar-avatar-wrap">
        {champion && <CrownIcon />}
        <ArAvatar speaker={sp} className="ar-avatar" />
      </span>
      <div className="ar-card-info">
        <span className="ar-name">{sp.name}</span>
        <span className={`ar-seat ar-seat-${sp.side}`}>{sideLabel(sp.side)}{seatLabel(sp.seat)}</span>
      </div>
      <span className="ar-votes">{item.count} 分</span>
    </div>
  );
}

function AudienceResultScene({ snapshot }: { snapshot: MatchSnapshot }) {
  const audience = snapshot.vote_state.audience_summary;
  const aff = audience.winner.affirmative;
  const neg = audience.winner.negative;
  const total = Math.max(1, aff + neg);
  const affPercent = Math.round((aff / total) * 100);
  const negPercent = 100 - affPercent;
  const hasVotes = aff + neg > 0;
  // 拳套对撞徽标定位到双方分界处；为可读性夹在 [14%, 86%]，随占比动态滑动。
  const vsPos = !hasVotes ? 50 : Math.min(86, Math.max(14, affPercent));
  const leading = aff === neg ? "" : aff > neg ? "aff" : "neg";
  const winnerSide: Side | null = !hasVotes || aff === neg ? null : aff > neg ? "affirmative" : "negative";
  const affTeam = snapshot.teams.find((t) => t.side === "affirmative");
  const negTeam = snapshot.teams.find((t) => t.side === "negative");
  const winnerTeam = winnerSide === "affirmative" ? affTeam : winnerSide === "negative" ? negTeam : undefined;
  const ranked = [...audience.best_speaker]
    .map((item) => ({ ...item, speaker: snapshot.speakers.find((s) => s.id === item.speaker_id) }))
    .filter((item): item is typeof item & { speaker: Speaker } => Boolean(item.speaker))
    .slice(0, 8);
  const champion = ranked[0];
  const rest = ranked.slice(1);
  const aspectRows = [
    { key: "constructive", title: "立论" },
    { key: "process", title: "过程" },
    { key: "conclusion", title: "结辩" },
  ] as const;
  return (
    <section className="screen-scene audience-result-scene">
      <ScreenChrome match={snapshot.match} />
      <div className="ar-headline">
        <div className="ar-head-row">
          <div className="ar-head">观众投票结果</div>
          <span className="ar-total-badge">总投票数 <strong>{aff + neg}</strong> 票</span>
        </div>
        {winnerTeam && (
          <div className={`ar-winner ar-winner-${winnerSide}`}>
            <TrophyIcon />
            <span className="ar-winner-label">获胜方</span>
            <strong>{sideLabel(winnerSide!)} · {winnerTeam.name}</strong>
            <TrophyIcon flip />
          </div>
        )}
      </div>

      <div className="ar-vs">
        <div className={`ar-vs-side ar-aff ${leading === "aff" ? "lead" : ""}`}>
          <SideEmblem side="affirmative" />
          <span className="ar-vs-text">
            <span className="ar-vs-name">正方</span>
            <span className="ar-vs-team">{affTeam?.name ?? "正方"}</span>
          </span>
        </div>

        <div className="ar-vs-bar-wrap" role="img" aria-label={`正方 ${aff} 票，反方 ${neg} 票`}>
          <div className="ar-vs-track">
            <i className="ar-bar-aff" style={{ width: `${hasVotes ? affPercent : 50}%` }}><b>{aff} 票</b></i>
            <i className="ar-bar-neg" style={{ width: `${hasVotes ? negPercent : 50}%` }}><b>{neg} 票</b></i>
          </div>
          <span className="ar-vs-badge" style={{ left: `${vsPos}%` }}><VsGloves /></span>
        </div>

        <div className={`ar-vs-side ar-neg ${leading === "neg" ? "lead" : ""}`}>
          <span className="ar-vs-text">
            <span className="ar-vs-name">反方</span>
            <span className="ar-vs-team">{negTeam?.name ?? "反方"}</span>
          </span>
          <SideEmblem side="negative" />
        </div>
      </div>

      <div className="ar-aspect-grid">
        {aspectRows.map((aspect) => {
          const row = audience.aspects?.[aspect.key] ?? { affirmative: 0, negative: 0 };
          const totalAspect = Math.max(1, row.affirmative + row.negative);
          const affAspectPercent = Math.round((row.affirmative / totalAspect) * 100);
          const negAspectPercent = 100 - affAspectPercent;
          const lead = row.affirmative === row.negative ? "" : row.affirmative > row.negative ? "affirmative" : "negative";
          return (
            <div key={aspect.key} className="ar-aspect-card">
              <div className="ar-aspect-head">
                <strong>{aspect.title}</strong>
                <span>{row.affirmative + row.negative} 票</span>
              </div>
              <div className="ar-aspect-bar">
                <i className="ar-aspect-aff" style={{ width: `${affAspectPercent}%` }} />
                <i className="ar-aspect-neg" style={{ width: `${negAspectPercent}%` }} />
              </div>
              <div className="ar-aspect-foot">
                <span className={lead === "affirmative" ? "lead" : ""}>正方 {row.affirmative}</span>
                <span className={lead === "negative" ? "lead" : ""}>反方 {row.negative}</span>
              </div>
            </div>
          );
        })}
      </div>

      <div className="ar-rank-title">辩手投票排行</div>
      {champion && <DebaterRow item={champion} rank={1} champion />}
      <div className="ar-rank-grid">
        {rest.map((item, index) => (
          <DebaterRow key={item.speaker_id} item={item} rank={index + 2} champion={false} />
        ))}
      </div>
      {!ranked.length && <p className="ar-empty">等待观众投票统计。</p>}
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
            <div className="roster-avatar-block">
              <img className="roster-avatar" src={resolveAvatar(speaker)} alt={speaker.name} />
              <span className="roster-seat-badge">{seatLabel(speaker.seat)}</span>
            </div>
            <div className="roster-person">
              <div className="roster-person-header">
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
  const thinking = Boolean(currentSpeaker?.speaker_type === "agent" && snapshot.current_speech?.state === "thinking");
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
          caption={`${sideLabel(currentSpeaker.side)}${seatLabel(currentSpeaker.seat)}`}
        />
      )}
      <div className="phase-name">{phaseName}</div>
      <p>{currentSpeaker ? `${thinking ? "思考中" : "当前发言"} · ${speakerLabel(currentSpeaker)}` : completedSpeaker ? "发言完毕" : "当前发言 · 等待开始"}</p>
      <ClockTile label="本环节剩余" clock={clockByName(snapshot.clocks, "main")} tone={tone} />
    </div>
  );
}

function FreeMode({ snapshot, currentSpeaker }: { snapshot: MatchSnapshot; currentSpeaker?: Speaker }) {
  const thinking = Boolean(currentSpeaker?.speaker_type === "agent" && snapshot.current_speech?.state === "thinking");
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
          caption={`${sideLabel(currentSpeaker.side)}${seatLabel(currentSpeaker.seat)}`}
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
  const isAgent = speaker?.speaker_type === "agent";
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
          caption={`${sideLabel(speaker.side)}${seatLabel(speaker.seat)}`}
        />
      ) : (
        <div className="prep-orb"><i /><i /></div>
      )}
      <h3>{isAgent ? "AI 思考中" : "等待发言"}</h3>
      <p>{speakerLabel(speaker)} · {isAgent ? "生成完成后开始播报并计时" : "请等待辩手开始发言"}</p>
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

function VoteQr({ url, size = 260 }: { url: string; size?: number }) {
  const [dataUrl, setDataUrl] = useState("");

  useEffect(() => {
    let cancelled = false;
    QRCode.toDataURL(url, {
      errorCorrectionLevel: "M",
      margin: 1,
      width: size,
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
  }, [url, size]);

  return (
    <div className="qr-placeholder">
      {dataUrl ? <img src={dataUrl} alt="观众投票二维码" /> : "生成二维码中"}
    </div>
  );
}

function normalizeScreenScene(scene: ScreenScene): RuntimeScreenScene {
  switch (scene) {
    case "opening":
      return "opening";
    case "teams":
      return "teams";
    case "debate_process":
      return "debate_process";
    case "idle":
      return "idle";
    case "paused":
      return "paused";
    case "audience_vote":
      return "audience_vote";
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
