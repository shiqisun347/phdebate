import { CheckCircle2, ClipboardCheck, Mic, Pause, Play, RadioTower, SkipForward, Square, UserRound } from "lucide-react";
import { type ButtonHTMLAttributes, useEffect, useRef, useState } from "react";
import { getMatch, patch, post, uploadAudioChunk } from "../api/client";
import { AuthPrompt } from "../components/AuthPrompt";
import { ClockTile } from "../components/ClockTile";
import { useActionFeedback } from "../components/Feedback";
import { StatusPill } from "../components/StatusPill";
import { useLiveKitAudio } from "../livekit/useLiveKitAudio";
import { useMatch } from "../realtime/useMatch";
import { clockByName, clockStateLabel, seatLabel, sideClass, sideLabel, speakerLabel } from "../state/format";
import type { AgentStatus, Clock as MatchClock, MatchSnapshot, Phase, Side, Speaker } from "../types/contracts";

interface ConsolePageProps {
  matchId: string;
  speakerId: string;
}

const PCM_SAMPLE_RATE = 16000;
const PCM_CHUNK_MS = 500;
const PCM_SAMPLES_PER_CHUNK = PCM_SAMPLE_RATE * (PCM_CHUNK_MS / 1000);
const PCM_MIME_TYPE = "audio/L16;rate=16000";
const PCM_SILENCE_PEAK_THRESHOLD = 1;
const PCM_SILENCE_WARNING_CHUNKS = 3;
const MIC_AUDIO_CONSTRAINTS: MediaTrackConstraints = {
  channelCount: 1,
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
};
const PCM_WORKLET_SOURCE = `
class PhdebatePcmCapture extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (input && input.length) {
      const frame = new Float32Array(input);
      this.port.postMessage(frame, [frame.buffer]);
    }
    return true;
  }
}
registerProcessor("phdebate-pcm-capture", PhdebatePcmCapture);
`;

type EntryStep = "identity" | "mic" | "ready";
type CheckStatus = "idle" | "testing" | "passed" | "failed";

interface BrowserWindowWithAudioContext extends Window {
  webkitAudioContext?: typeof AudioContext;
}

interface PcmChunkStats {
  peak: number;
  rms: number;
  silent: boolean;
}

interface PcmChunkPayload extends PcmChunkStats {
  blob: Blob;
}

function createBrowserAudioContext(): AudioContext | null {
  const contextWindow = window as BrowserWindowWithAudioContext;
  const AudioContextCtor = window.AudioContext ?? contextWindow.webkitAudioContext;
  return AudioContextCtor ? new AudioContextCtor() : null;
}

function floatToPcmSample(value: number): number {
  const clamped = Math.max(-1, Math.min(1, value));
  return Math.round(clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff);
}

function downsampleToPcm(input: Float32Array, sourceRate: number): Int16Array {
  if (sourceRate <= PCM_SAMPLE_RATE) return Int16Array.from(input, floatToPcmSample);
  const ratio = sourceRate / PCM_SAMPLE_RATE;
  const length = Math.floor(input.length / ratio);
  const result = new Int16Array(length);
  let sourceOffset = 0;
  for (let index = 0; index < length; index += 1) {
    const nextOffset = Math.min(input.length, Math.round((index + 1) * ratio));
    let sum = 0;
    let count = 0;
    for (let sampleIndex = sourceOffset; sampleIndex < nextOffset; sampleIndex += 1) {
      sum += input[sampleIndex] ?? 0;
      count += 1;
    }
    result[index] = floatToPcmSample(count ? sum / count : 0);
    sourceOffset = nextOffset;
  }
  return result;
}

function pcmStats(samples: number[]): PcmChunkStats {
  if (!samples.length) return { peak: 0, rms: 0, silent: true };
  let peak = 0;
  let squareSum = 0;
  for (const sample of samples) {
    const value = Math.abs(sample);
    if (value > peak) peak = value;
    squareSum += sample * sample;
  }
  return {
    peak,
    rms: Math.sqrt(squareSum / samples.length),
    silent: peak <= PCM_SILENCE_PEAK_THRESHOLD,
  };
}

function pcmChunk(samples: number[]): PcmChunkPayload {
  const pcm = new Int16Array(samples);
  const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  return {
    blob: new Blob([bytes], { type: PCM_MIME_TYPE }),
    ...pcmStats(samples),
  };
}

export function ConsolePage({ matchId, speakerId }: ConsolePageProps) {
  const [selectedSpeakerId, setSelectedSpeakerId] = useState(() => initialSpeakerId(speakerId));
  const { snapshot, socketStatus, loadError, refresh, send } = useMatch(matchId, "speaker", selectedSpeakerId);
  const [error, setError] = useState<string | null>(null);
  const [entryStep, setEntryStep] = useState<EntryStep>(() => initialEntryStep(matchId, selectedSpeakerId));
  const [displayName, setDisplayName] = useState(() => initialDisplayName(matchId, selectedSpeakerId));
  const [draftSpeakerId, setDraftSpeakerId] = useState(selectedSpeakerId);
  const [draftName, setDraftName] = useState(displayName);
  const [promoteAgentToHuman, setPromoteAgentToHuman] = useState(false);
  const [apiTestStatus, setApiTestStatus] = useState<CheckStatus>("idle");
  const [micTestStatus, setMicTestStatus] = useState<CheckStatus>("idle");
  const [audioStatus, setAudioStatus] = useState("待命");
  const [confirmStopOpen, setConfirmStopOpen] = useState(false);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunkIndexRef = useRef(0);
  const activeAudioChunkCountRef = useRef(0);
  const pendingUploadsRef = useRef<Promise<unknown>[]>([]);
  const uploadChainRef = useRef<Promise<unknown>>(Promise.resolve());
  const identityInitializedRef = useRef(false);
  const { busyProps, notify, runAction } = useActionFeedback();
  const sendRef = useRef(send);
  sendRef.current = send;

  const speaker = snapshot?.speakers.find((item) => item.id === selectedSpeakerId) ?? snapshot?.speakers[0] ?? null;
  const active = Boolean(snapshot?.current_speech?.speaker_id && speaker && snapshot.current_speech.speaker_id === speaker.id);
  const speechPaused = active && snapshot?.current_speech?.state === "paused";
  const activeSpeechId = active ? snapshot?.current_speech?.id ?? null : null;
  const activeSpeakerId = speaker?.id ?? null;
  const speakerType = speaker?.speaker_type;
  const activeAudioAsset = snapshot?.audio_assets.find((item) => item.speech_id === activeSpeechId);
  const shouldPublishLiveKitMicrophone = Boolean(activeSpeechId && activeSpeakerId && !speechPaused);
  useLiveKitAudio({
    matchId,
    role: "speaker",
    speakerId: selectedSpeakerId,
    enabled: entryStep === "ready" && speakerType === "human" && shouldPublishLiveKitMicrophone,
    publishMicrophone: shouldPublishLiveKitMicrophone,
  });

  useEffect(() => {
    activeAudioChunkCountRef.current = activeAudioAsset?.chunk_count ?? 0;
  }, [activeAudioAsset?.chunk_count]);

  useEffect(() => {
    setDraftSpeakerId(selectedSpeakerId);
    setPromoteAgentToHuman(false);
    identityInitializedRef.current = false;
    const cachedName = initialDisplayName(matchId, selectedSpeakerId);
    setDraftName(cachedName);
    setDisplayName(cachedName);
  }, [matchId, selectedSpeakerId]);

  useEffect(() => {
    if (identityInitializedRef.current || promoteAgentToHuman) return;
    if (!snapshot) return;
    const current = snapshot.speakers.find((item) => item.id === draftSpeakerId) ?? snapshot.speakers.find((item) => item.id === selectedSpeakerId);
    if (!current) return;
    identityInitializedRef.current = true;
    if (!draftName) setDraftName(current.name);
  }, [draftName, draftSpeakerId, promoteAgentToHuman, selectedSpeakerId, snapshot]);

  useEffect(() => {
    if (!snapshot || !speaker || draftName || promoteAgentToHuman) return;
    setDraftName(speaker.name);
  }, [draftName, promoteAgentToHuman, snapshot, speaker]);

  useEffect(() => {
    if (!activeSpeechId || !activeSpeakerId || speakerType !== "human" || entryStep !== "ready" || speechPaused) {
      setAudioStatus("待命");
      return;
    }

    let cancelled = false;
    let localRecorder: MediaRecorder | null = null;
    let localStream: MediaStream | null = null;
    let stopPcmArchive: (() => void) | null = null;
    let completionQueued = false;
    const currentSpeechId = activeSpeechId;
    const currentSpeakerId = activeSpeakerId;

    function enqueueUpload(blob: Blob, durationMs: number, filename?: string, stats?: PcmChunkStats) {
      const chunkIndex = chunkIndexRef.current;
      chunkIndexRef.current += 1;
      const upload = uploadChainRef.current.catch(() => undefined).then(() => uploadAudioChunk(matchId, currentSpeechId, currentSpeakerId, chunkIndex, blob, durationMs, filename))
        .then((chunk) => {
          // 后端只回这一片的归档结果(含累计 chunk_count)，不再回传整张快照。
          if (!cancelled) setAudioStatus(stats?.silent ? "麦克风无输入" : chunk.chunk_count ? "记录中" : "待命");
        })
        .catch((err) => {
          if (!cancelled) {
            setAudioStatus("记录异常");
            setError(err instanceof Error ? err.message : "录音上传失败");
          }
        });
      pendingUploadsRef.current.push(upload);
      upload.finally(() => {
        pendingUploadsRef.current = pendingUploadsRef.current.filter((item) => item !== upload);
      });
      uploadChainRef.current = upload;
    }

    function queueArchiveComplete() {
      if (completionQueued || chunkIndexRef.current === 0) return;
      completionQueued = true;
      const uploads = [...pendingUploadsRef.current];
      void Promise.allSettled(uploads).then(() =>
        post(`/api/matches/${matchId}/speeches/${currentSpeechId}/audio/complete`, { speaker_id: currentSpeakerId })
          .then(() => {
            if (!cancelled) setAudioStatus("已记录");
          })
          .catch(() => {
            if (!cancelled) setAudioStatus("记录确认失败");
          })
      );
    }

    async function startPcmArchive(stream: MediaStream): Promise<(() => void) | null> {
      const audioContext = createBrowserAudioContext();
      if (!audioContext) return null;
      const context = audioContext;
      void context.resume().catch(() => undefined);
      const source = context.createMediaStreamSource(stream);
      const mute = context.createGain();
      const sampleBuffer: number[] = [];
      mute.gain.value = 0;
      let closed = false;
      let silentChunkStreak = 0;
      let silenceWarningSent = false;
      let silenceErrorActive = false;

      function reportInputLevel(stats: PcmChunkStats): boolean {
        if (stats.silent) {
          silentChunkStreak += 1;
          if (silentChunkStreak >= PCM_SILENCE_WARNING_CHUNKS && !silenceWarningSent) {
            silenceWarningSent = true;
            silenceErrorActive = true;
            setAudioStatus("麦克风无输入");
            setError("检测到麦克风输入持续为静音，请检查是否选错设备、耳机/系统是否静音，或重新进入辩手端。");
            sendRef.current("speaker.mic_error", {
              speaker_id: currentSpeakerId,
              mic_permission: "granted",
              device_label: stream.getAudioTracks()[0]?.label || "browser microphone",
              message: "Microphone input is silent"
            });
          }
          return silenceWarningSent;
        }
        silentChunkStreak = 0;
        if (silenceErrorActive) {
          silenceErrorActive = false;
          setError(null);
        }
        setAudioStatus("记录中");
        return false;
      }

      function flush(force = false) {
        while (sampleBuffer.length >= PCM_SAMPLES_PER_CHUNK || (force && sampleBuffer.length > 0)) {
          const take = force ? Math.min(sampleBuffer.length, PCM_SAMPLES_PER_CHUNK) : PCM_SAMPLES_PER_CHUNK;
          const samples = sampleBuffer.splice(0, take);
          const chunkNumber = chunkIndexRef.current;
          const chunk = pcmChunk(samples);
          const silenceWarningActive = reportInputLevel(chunk);
          const uploadStats: PcmChunkStats = { peak: chunk.peak, rms: chunk.rms, silent: chunk.silent && silenceWarningActive };
          enqueueUpload(
            chunk.blob,
            Math.round((samples.length / PCM_SAMPLE_RATE) * 1000),
            `chunk_${String(chunkNumber).padStart(5, "0")}.pcm`,
            uploadStats
          );
        }
      }

      function appendInput(input: Float32Array) {
        if (cancelled) return;
        const pcm = downsampleToPcm(input, context.sampleRate);
        for (const sample of pcm) sampleBuffer.push(sample);
        flush(false);
      }

      async function startWorklet(): Promise<(() => void) | null> {
        if (!context.audioWorklet || typeof AudioWorkletNode === "undefined") return null;
        const moduleUrl = URL.createObjectURL(new Blob([PCM_WORKLET_SOURCE], { type: "text/javascript" }));
        try {
          await context.audioWorklet.addModule(moduleUrl);
        } finally {
          URL.revokeObjectURL(moduleUrl);
        }
        const node = new AudioWorkletNode(context, "phdebate-pcm-capture");
        node.port.onmessage = (event) => {
          const frame = event.data instanceof Float32Array ? event.data : new Float32Array(event.data);
          appendInput(frame);
        };
        source.connect(node);
        node.connect(mute);
        mute.connect(context.destination);
        setAudioStatus("记录中");
        return () => {
          if (closed) return;
          closed = true;
          node.port.onmessage = null;
          flush(true);
          queueArchiveComplete();
          source.disconnect();
          node.disconnect();
          mute.disconnect();
          void context.close();
        };
      }

      const workletStop = await startWorklet().catch(() => null);
      if (workletStop) return workletStop;

      const processor = context.createScriptProcessor(4096, 1, 1);
      processor.onaudioprocess = (event) => appendInput(event.inputBuffer.getChannelData(0));

      source.connect(processor);
      processor.connect(mute);
      mute.connect(context.destination);
      setAudioStatus("记录中");

      return () => {
        if (closed) return;
        closed = true;
        processor.onaudioprocess = null;
        flush(true);
        queueArchiveComplete();
        source.disconnect();
        processor.disconnect();
        mute.disconnect();
        void context.close();
      };
    }

    function startMediaRecorderArchive(stream: MediaStream): boolean {
      if (typeof MediaRecorder === "undefined") return false;
      const preferredMimeType = "audio/webm;codecs=opus";
      const options = MediaRecorder.isTypeSupported(preferredMimeType) ? { mimeType: preferredMimeType } : undefined;
      localRecorder = new MediaRecorder(stream, options);
      recorderRef.current = localRecorder;
      setAudioStatus("记录中");
      localRecorder.ondataavailable = (event) => {
        if (!event.data.size) return;
        const chunkNumber = chunkIndexRef.current;
        enqueueUpload(event.data, PCM_CHUNK_MS, `chunk_${String(chunkNumber).padStart(5, "0")}.webm`);
      };
      localRecorder.onerror = () => {
        if (!cancelled) setAudioStatus("记录异常");
      };
      localRecorder.onstop = queueArchiveComplete;
      localRecorder.start(PCM_CHUNK_MS);
      return true;
    }

    async function startArchive() {
      if (!navigator.mediaDevices?.getUserMedia) {
        setAudioStatus("浏览器不支持录音");
        return;
      }

      try {
        localStream = await navigator.mediaDevices.getUserMedia({ audio: MIC_AUDIO_CONSTRAINTS });
        if (cancelled) {
          localStream.getTracks().forEach((track) => track.stop());
          return;
        }
        streamRef.current = localStream;
        chunkIndexRef.current = activeAudioChunkCountRef.current;
        pendingUploadsRef.current = [];
        uploadChainRef.current = Promise.resolve();
        try {
          stopPcmArchive = await startPcmArchive(localStream);
        } catch {
          stopPcmArchive = null;
        }
        if (stopPcmArchive) return;
        if (startMediaRecorderArchive(localStream)) return;
        setAudioStatus("浏览器不支持录音");
      } catch (err) {
        if (!cancelled) {
          setAudioStatus("麦克风不可用");
          setError(err instanceof Error ? err.message : "麦克风不可用，请重新测试后进入。");
          sendRef.current("speaker.mic_error", {
            speaker_id: currentSpeakerId,
            mic_permission: "denied",
            device_label: "browser microphone",
            message: err instanceof Error ? err.message : "Microphone unavailable"
          });
        }
      }
    }

    void startArchive();

    return () => {
      cancelled = true;
      stopPcmArchive?.();
      const recorder = localRecorder ?? recorderRef.current;
      if (recorder && recorder.state !== "inactive") {
        try {
          recorder.requestData();
        } catch {
          // Some browsers already flush during stop.
        }
        recorder.stop();
      }
      (localStream ?? streamRef.current)?.getTracks().forEach((track) => track.stop());
      recorderRef.current = null;
      streamRef.current = null;
    };
  }, [activeSpeechId, activeSpeakerId, entryStep, matchId, speakerType, speechPaused]);

  async function action(path: string, body: Record<string, unknown> = {}) {
    try {
      const label = consoleActionLabel(path);
      await runAction(actionKey(path, body), label, async () => {
        setError(null);
        await post(path, body);
        await refresh();
      }, {
        successText: `${label}已同步`
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    }
  }

  async function testMicrophone() {
    try {
      await runAction("console-mic-test", "麦克风测试", async () => {
        setError(null);
        setMicTestStatus("testing");
        const stream = await navigator.mediaDevices.getUserMedia({ audio: MIC_AUDIO_CONSTRAINTS });
        await new Promise((resolve) => window.setTimeout(resolve, 450));
        stream.getTracks().forEach((track) => track.stop());
        setMicTestStatus("passed");
      }, {
        successText: "麦克风可用"
      });
    } catch (err) {
      setMicTestStatus("failed");
      setError(err instanceof Error ? err.message : "无法使用麦克风，请检查浏览器授权。");
    }
  }

  async function testApiRequest() {
    try {
      await runAction("console-api-test", "API 请求检测", async () => {
        setError(null);
        setApiTestStatus("testing");
        await getMatch(matchId);
        setApiTestStatus("passed");
      }, {
        successText: "API 请求正常"
      });
    } catch (err) {
      setApiTestStatus("failed");
      setError(err instanceof Error ? err.message : "API 请求失败，请联系工作人员。");
    }
  }

  async function saveIdentity() {
    const nextName = draftName.trim();
    const selected = snapshot?.speakers.find((item) => item.id === draftSpeakerId);
    if (!draftSpeakerId || !selected) {
      setError("请选择一个后台预设身份。");
      return;
    }
    if (selected.speaker_type === "agent" && !promoteAgentToHuman) {
      setError("请选择该 AI 辩手卡片并确认转为人类辩手。");
      return;
    }
    if ((selected.speaker_type === "human" || promoteAgentToHuman) && !nextName) {
      setError("请输入姓名。");
      return;
    }
    try {
      await runAction("console-save-identity", "确认身份", async () => {
        setError(null);
        if (selected.speaker_type === "human") {
          await patch(`/api/matches/${matchId}/speakers/${draftSpeakerId}/profile`, { name: nextName });
          await refresh();
        } else if (promoteAgentToHuman) {
          await patch(`/api/matches/${matchId}/speakers/${draftSpeakerId}/profile`, { name: nextName, speaker_type: "human" });
          await refresh();
        }
      }, {
        successText: "身份已确认"
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "身份确认失败");
      return;
    }
    window.localStorage.setItem(identityKey(matchId, draftSpeakerId), nextName);
    window.localStorage.setItem(activeSpeakerKey(matchId), draftSpeakerId);
    setSelectedSpeakerId(draftSpeakerId);
    setDisplayName(nextName || selected.name);
    setError(null);
    setEntryStep("mic");
    notify({
      tone: "success",
      title: "身份已确认",
      message: selected.speaker_type === "agent" ? "已转为人类辩手，请继续硬件测试。" : "请继续硬件测试。",
    });
    setPromoteAgentToHuman(false);
    setApiTestStatus("idle");
    setMicTestStatus("idle");

    const targetPath = `/console/${draftSpeakerId}`;
    if (window.location.pathname !== targetPath) {
      const params = new URLSearchParams(window.location.search);
      if (matchId === "current") {
        params.delete("match_id");
      } else {
        params.set("match_id", matchId);
      }
      const query = params.toString();
      window.history.replaceState(null, "", query ? `${targetPath}?${query}` : targetPath);
    }
  }

  function enterConsole() {
    if (speaker?.speaker_type === "agent" && apiTestStatus !== "passed") {
      setError("Agent 辩手请先完成 API 请求检测。");
      return;
    }
    if (speaker?.speaker_type === "human" && micTestStatus !== "passed" && !canSkipMicTest()) {
      setError("请先完成麦克风检测。");
      return;
    }
    window.localStorage.setItem(entryReadyKey(matchId, selectedSpeakerId), "1");
    setEntryStep("ready");
    notify({ tone: "success", title: "已进入辩手端", message: "现场状态会自动同步。" });
  }

  function resetEntry() {
    window.localStorage.removeItem(entryReadyKey(matchId, selectedSpeakerId));
    identityInitializedRef.current = false;
    setEntryStep("identity");
    setApiTestStatus("idle");
    setMicTestStatus("idle");
    notify({ tone: "info", title: "已返回身份选择", message: "可重新选择后台预设身份。" });
  }

  if (!snapshot && loadError) return <AuthPrompt role="speaker" speakerId={selectedSpeakerId} message={loadError} />;
  if (snapshot && !snapshot.match.id) return <div className="loading">比赛尚未创建，请等待主办方在控制台「比赛管理」新建比赛。</div>;
  if (!snapshot || !speaker) return <div className="loading">正在连接辩手端...</div>;

  const currentPhase = snapshot.phases.find((item) => item.id === snapshot.match.current_phase_id);
  const phaseTarget = phaseTargetLabel(currentPhase);
  const isFree = currentPhase?.phase_type === "free_debate";
  const isCurrentTurnSide = !isFree || snapshot.free_debate.current_turn_side === speaker.side;
  const currentSpeaker = snapshot.speakers.find((item) => item.id === snapshot.current_speech?.speaker_id);
  const hasOtherActiveSpeech = Boolean(snapshot.current_speech && snapshot.current_speech.speaker_id !== speaker.id);
  const fixedSeatAllowed = !isFree && currentPhase?.side === speaker.side && currentPhase.speaker_seat === speaker.seat;
  const canSpeak = snapshot.match.status === "running" && !snapshot.flow.awaiting_host_confirm && !hasOtherActiveSpeech && isCurrentTurnSide && (
    active ||
    isFree ||
    fixedSeatAllowed
  );
  const prompt = buildPrompt({
    active,
    canSpeak,
    displayName,
    matchStatus: snapshot.match.status,
    currentPhase,
    speaker,
    currentSpeaker,
    freeTurnSide: snapshot.free_debate.current_turn_side,
    flow: snapshot.flow
  });
  const currentSpeechText = active ? snapshot.current_speech?.content_partial || snapshot.current_speech?.content_final || "" : "";
  const ownTeam = snapshot.teams.find((team) => team.id === speaker.team_id);
  const clock = isFree ? clockByName(snapshot.clocks, "turn") : clockByName(snapshot.clocks, "main");
  const agentStatus = snapshot.agent_status.find((item) => item.speaker_id === speaker.id);

  if (entryStep !== "ready") {
    return (
      <main className="console-entry-shell">
        <section className="console-entry-card">
          <div className="entry-steps">
            <span className={entryStep === "identity" ? "active" : "done"}>1 身份选择</span>
            <span className={entryStep === "mic" ? "active" : ""}>2 硬件测试</span>
          </div>
          {entryStep === "identity" ? (
            <form
              className="console-entry-form"
              onSubmit={(event) => {
                event.preventDefault();
                void saveIdentity();
              }}
            >
              <UserRound size={34} />
              <h1>身份选择</h1>
              <p>请选择后台预设身份。选择 AI 席位时需确认转为人类辩手，并输入现场姓名。</p>
              <div className="identity-card-grid">
                {snapshot.speakers.map((item) => {
                  const isAgent = item.speaker_type === "agent";
                  return (
                    <button
                      type="button"
                      key={item.id}
                      className={`identity-choice ${sideClass(item.side)} ${draftSpeakerId === item.id ? "active" : ""} ${isAgent ? "is-agent-convertible" : ""}`}
                      onClick={() => {
                        if (isAgent) {
                          const confirmed = window.confirm(`你选择了 AI 辩手 ${sideLabel(item.side)}${seatLabel(item.seat)} · ${item.name}。是否将其转为人类辩手？`);
                          if (!confirmed) return;
                          setPromoteAgentToHuman(true);
                          setDraftName("");
                        } else {
                          setPromoteAgentToHuman(false);
                          setDraftName(item.name);
                        }
                        setDraftSpeakerId(item.id);
                      }}
                    >
                      <span>{sideLabel(item.side)}{seatLabel(item.seat)}</span>
                      <strong>{item.name}</strong>
                      <em>{isAgent ? `AI 辩手 · 可转为人类` : "人类选手"}</em>
                    </button>
                  );
                })}
              </div>
              {snapshot.speakers.find((item) => item.id === draftSpeakerId)?.speaker_type === "human" || promoteAgentToHuman ? (
                <label>
                  <span>{promoteAgentToHuman ? "转为人类后的姓名" : "姓名"}</span>
                  <input value={draftName} placeholder={promoteAgentToHuman ? "请输入转为人类后的姓名" : "请输入你的姓名"} onChange={(event) => setDraftName(event.target.value)} />
                </label>
              ) : (
                <div className="entry-empty">
                  请选择一个 AI 辩手后，可确认转换为人类辩手并输入姓名。
                </div>
              )}
              {error && <div className="console-error">{error}</div>}
              <button
                {...busyProps("console-save-identity")}
                type="submit"
                disabled={
                  !draftSpeakerId ||
                  (snapshot.speakers.find((item) => item.id === draftSpeakerId)?.speaker_type === "agent" && !promoteAgentToHuman) ||
                  ((snapshot.speakers.find((item) => item.id === draftSpeakerId)?.speaker_type === "human" || promoteAgentToHuman) && !draftName.trim())
                }
              >
                <ClipboardCheck size={18} />下一步
              </button>
            </form>
          ) : (
            <section className="console-entry-form">
              <RadioTower size={34} />
              <h1>硬件测试</h1>
              {speaker?.speaker_type === "agent" ? (
                <>
                  <p>Agent 辩手需要确认 API 请求连通。现场声音只从主持导播台或技术后台指定电脑输出，本机无需外放。</p>
                  <div className="entry-check-list">
                    <div className={`entry-check ${apiTestStatus}`}>
                      <strong>API 请求</strong>
                      <span>{checkLabel(apiTestStatus, "点击检测 API")}</span>
                      <button {...busyProps("console-api-test")} type="button" onClick={testApiRequest} disabled={apiTestStatus === "testing"}>
                        <RadioTower size={16} />检测
                      </button>
                    </div>
                    <div className="entry-check passed">
                      <strong>现场声音</strong>
                      <span>{snapshot?.audio_output?.label ?? "主持导播台电脑"}统一输出，辩手端不播放铃声或 TTS。</span>
                      <em>无需检测</em>
                    </div>
                  </div>
                </>
              ) : (
                <>
                  <p>人类选手需要确认麦克风可用。当前通过 HTTP 访问时，浏览器可能不开放麦克风权限，可直接跳过。</p>
                  <div className={`entry-check ${micTestStatus}`}>
                    <strong>麦克风</strong>
                    <span>{canSkipMicTest() ? "HTTP 访问，可跳过麦克风检测" : micTestLabel(micTestStatus)}</span>
                    <button {...busyProps("console-mic-test")} type="button" onClick={testMicrophone} disabled={micTestStatus === "testing"}>
                      <Mic size={16} />测试
                    </button>
                  </div>
                </>
              )}
              {error && <div className="console-error">{error}</div>}
              <div className="entry-button-row">
                <button type="button" onClick={() => setEntryStep("identity")}>返回</button>
                <button
                  type="button"
                  className="primary"
                  onClick={enterConsole}
                  disabled={speaker?.speaker_type === "agent" ? apiTestStatus !== "passed" : micTestStatus !== "passed" && !canSkipMicTest()}
                >
                  <CheckCircle2 size={18} />进入
                </button>
              </div>
            </section>
          )}
        </section>
      </main>
    );
  }

  return (
    <main className="console-shell">
      <section className="console-hero-card">
        <div className={`avatar big ${sideClass(speaker.side)}`}>{displayName.slice(0, 1) || speaker.name.slice(0, 1)}</div>
        <div className="console-identity">
          <span>辩手端</span>
          <h1>{displayName || speaker.name}</h1>
          <p>{ownTeam?.name} · {sideLabel(speaker.side)}{seatLabel(speaker.seat)}</p>
        </div>
        <button className="quiet-button" onClick={resetEntry}>切换身份</button>
      </section>

      {error && <div className="console-error">{error}</div>}

      <section className={`console-callout ${prompt.tone}`}>
        <span>{prompt.eyebrow}</span>
        <strong>{prompt.title}</strong>
        <p>{prompt.detail}</p>
      </section>

      {speaker.speaker_type === "agent" ? (
        <AgentConsoleView
          matchId={matchId}
          snapshot={snapshot}
          speaker={speaker}
          currentPhase={currentPhase}
          currentSpeaker={currentSpeaker}
          agentStatus={agentStatus}
          active={active}
          canSpeak={canSpeak}
          phaseTarget={phaseTarget}
          isFree={isFree}
          socketStatus={socketStatus}
          busyProps={busyProps}
          action={action}
        />
      ) : (
        <HumanConsoleView
          matchId={matchId}
          snapshot={snapshot}
          speaker={speaker}
          currentPhase={currentPhase}
          currentSpeaker={currentSpeaker}
          phaseTarget={phaseTarget}
          isFree={isFree}
          active={active}
          speechPaused={speechPaused}
          canSpeak={canSpeak}
          clock={clock}
          audioStatus={audioStatus}
          currentSpeechText={currentSpeechText}
          socketStatus={socketStatus}
          busyProps={busyProps}
          action={action}
          confirmStopOpen={confirmStopOpen}
          setConfirmStopOpen={setConfirmStopOpen}
        />
      )}
    </main>
  );
}

function HumanConsoleView({
  matchId,
  snapshot,
  speaker,
  currentPhase,
  currentSpeaker,
  phaseTarget,
  isFree,
  active,
  speechPaused,
  canSpeak,
  clock,
  audioStatus,
  currentSpeechText,
  socketStatus,
  busyProps,
  action,
  confirmStopOpen,
  setConfirmStopOpen
}: {
  matchId: string;
  snapshot: MatchSnapshot;
  speaker: Speaker;
  currentPhase?: Phase;
  currentSpeaker?: Speaker;
  phaseTarget: string;
  isFree: boolean;
  active: boolean;
  speechPaused: boolean;
  canSpeak: boolean;
  clock?: MatchClock;
  audioStatus: string;
  currentSpeechText: string;
  socketStatus: string;
  busyProps: (key: string) => ButtonHTMLAttributes<HTMLButtonElement>;
  action: (path: string, body?: Record<string, unknown>) => Promise<void>;
  confirmStopOpen: boolean;
  setConfirmStopOpen: (open: boolean) => void;
}) {
  async function confirmStopSpeaking() {
    await action(`/api/matches/${matchId}/speakers/${speaker.id}/stop-speaking`, { reason: "speaker_confirm_stop" });
    setConfirmStopOpen(false);
  }

  // 需求 5.md：自由辩论"预点跳过"。本方相关轮 = 本方就是当前轮(当前 idx) / 本方是下一方(当前+1)。
  // 可点窗口：对方发言期间（你是下一方）到对方说完后 2s（你方决定窗口、还没人开始）。已投/已被 AI 接管→不可点。
  const isCurrentTurnSide = !isFree || snapshot.free_debate.current_turn_side === speaker.side;
  const fd = snapshot.free_debate;
  const myUpcomingTurnIdx = isCurrentTurnSide ? fd.turn_index : fd.turn_index + 1;
  const myTurnKey = `${speaker.side}_${myUpcomingTurnIdx}`;
  const alreadySkipped = (fd.skip_votes?.[myTurnKey] ?? []).includes(speaker.id);
  const myTurnAutoHandled = Boolean(fd.auto_handled?.[myTurnKey]);
  const canPreSkip =
    isFree &&
    snapshot.match.status === "running" &&
    !active &&
    !alreadySkipped &&
    !myTurnAutoHandled &&
    (!isCurrentTurnSide || !snapshot.current_speech);

  return (
    <>
      <section className="console-main-grid human-view">
        <ConsoleStageCard
          snapshot={snapshot}
          currentPhase={currentPhase}
          currentSpeaker={currentSpeaker}
          phaseTarget={phaseTarget}
          isFree={isFree}
          socketStatus={socketStatus}
        />

        <div className="console-action-card">
          <ClockTile
            label={isFree ? "本次发言剩余" : "本环节剩余"}
            clock={clock}
            tone={speaker.side === "affirmative" ? "aff" : "neg"}
          />
          {isFree && (
            <ClockTile
              label="本方总时间"
              clock={clockByName(snapshot.clocks, `${speaker.side}_total`)}
              tone={speaker.side === "affirmative" ? "aff" : "neg"}
              compact
            />
          )}
          <div className="console-clock-note">
            状态：{clockStateLabel(clock?.state)}{audioStatus === "记录异常" ? " · 录音异常，请联系工作人员" : ""}
          </div>
          {active ? (
            <div className="console-speech-actions">
              {speechPaused ? (
                <button {...busyProps(actionKey(`/api/matches/${matchId}/speakers/${speaker.id}/resume-speaking`, { reason: "speaker_resume" }))} onClick={() => action(`/api/matches/${matchId}/speakers/${speaker.id}/resume-speaking`, { reason: "speaker_resume" })}>
                  <Play size={20} />继续发言
                </button>
              ) : (
                <button {...busyProps(actionKey(`/api/matches/${matchId}/speakers/${speaker.id}/pause-speaking`, { reason: "speaker_pause" }))} onClick={() => action(`/api/matches/${matchId}/speakers/${speaker.id}/pause-speaking`, { reason: "speaker_pause" })}>
                  <Pause size={20} />暂停发言
                </button>
              )}
              <button {...busyProps(actionKey(`/api/matches/${matchId}/speakers/${speaker.id}/stop-speaking`, { reason: "speaker_confirm_stop" }))} className="mic-button stop" onClick={() => setConfirmStopOpen(true)}>
                <Square size={28} />结束发言
              </button>
            </div>
          ) : (
            <div className="console-speech-actions">
              <button {...busyProps(actionKey(`/api/matches/${matchId}/speakers/${speaker.id}/start-speaking`))} className={`mic-button ${canSpeak ? "start" : "disabled"}`} disabled={!canSpeak} onClick={() => action(`/api/matches/${matchId}/speakers/${speaker.id}/start-speaking`)}>
                <Play size={28} />开始发言
              </button>
              {isFree && alreadySkipped ? (
                <button className="mic-button skip" disabled>
                  <SkipForward size={20} />已跳过 ✓
                </button>
              ) : isFree && canPreSkip ? (
                <button {...busyProps(actionKey(`/api/matches/${matchId}/speakers/${speaker.id}/free-debate-skip`))} className="mic-button skip" onClick={() => action(`/api/matches/${matchId}/speakers/${speaker.id}/free-debate-skip`)}>
                  <SkipForward size={20} />{isCurrentTurnSide ? "跳过本轮" : "跳过下一轮"}
                </button>
              ) : null}
            </div>
          )}
        </div>
      </section>

      {confirmStopOpen && (
        <div className="console-confirm-backdrop" role="presentation">
          <section className="console-confirm" role="dialog" aria-modal="true" aria-labelledby="stop-speaking-title">
            <Square size={28} />
            <h2 id="stop-speaking-title">确认结束发言？</h2>
            <p>结束后本次发言会被锁定并保存，不能再继续发言。</p>
            <div className="console-confirm-actions">
              <button type="button" onClick={() => setConfirmStopOpen(false)}>取消</button>
              <button
                {...busyProps(actionKey(`/api/matches/${matchId}/speakers/${speaker.id}/stop-speaking`, { reason: "speaker_confirm_stop" }))}
                type="button"
                className="danger"
                onClick={confirmStopSpeaking}
              >
                确认结束
              </button>
            </div>
          </section>
        </div>
      )}

      <section className="console-secondary">
        {snapshot.next_speaker && (
          <div>
            <h3>下一位发言</h3>
            <p>
              {snapshot.next_speaker.label}
              {snapshot.next_speaker.speaker_id === speaker.id ? "（下一位就是你，请提前准备）" : ""}
            </p>
          </div>
        )}
        <div>
          <h3>提示</h3>
          <p>{phaseHelpText(currentPhase, speaker, snapshot.free_debate.current_turn_side)}</p>
        </div>
        {active && (
          <div>
            <h3>当前转写</h3>
            <p>{currentSpeechText || "正在等待转写内容。"}</p>
          </div>
        )}
      </section>
    </>
  );
}

function AgentConsoleView({
  matchId,
  snapshot,
  speaker,
  currentPhase,
  currentSpeaker,
  agentStatus,
  active,
  canSpeak,
  phaseTarget,
  isFree,
  socketStatus,
  busyProps,
  action
}: {
  matchId: string;
  snapshot: MatchSnapshot;
  speaker: Speaker;
  currentPhase?: Phase;
  currentSpeaker?: Speaker;
  agentStatus?: AgentStatus;
  active: boolean;
  canSpeak: boolean;
  phaseTarget: string;
  isFree: boolean;
  socketStatus: string;
  busyProps: (key: string) => ButtonHTMLAttributes<HTMLButtonElement>;
  action: (path: string, body?: Record<string, unknown>) => Promise<void>;
}) {
  const allowed = isAgentAllowedForPhase(speaker, currentPhase, snapshot.free_debate.current_turn_side);
  const modelLabel = speaker.model_name || agentStatus?.model || "未配置模型";
  return (
    <>
      <section className="console-main-grid agent-view">
        <ConsoleStageCard
          snapshot={snapshot}
          currentPhase={currentPhase}
          currentSpeaker={currentSpeaker}
          phaseTarget={phaseTarget}
          isFree={isFree}
          socketStatus={socketStatus}
        />
        <div className="console-agent-card">
          <span>AI 辩手状态</span>
          <strong>{active ? "正在发言" : canSpeak ? "已获得发言权限" : allowed ? "等待当前发言结束" : "等待轮次"}</strong>
          <p>{active ? "AI 正在生成或播放发言，大屏同步显示文本。" : canSpeak ? "当前轮次已授权给该 AI 辩手，主持人激活后系统将自动开始生成。" : allowed ? "当前轮次匹配该 AI 辩手，等待当前发言结束后自动开始。" : phaseHelpText(currentPhase, speaker, snapshot.free_debate.current_turn_side)}</p>
          <div className="agent-info-grid">
            <div>
              <em>模型</em>
              <b>{modelLabel}</b>
            </div>
            <div>
              <em>API</em>
              <b>{speaker.agent_endpoint ? "已配置" : "未配置"}</b>
            </div>
            <div>
              <em>连接</em>
              <b>{agentStatusLabel(agentStatus?.status)}</b>
            </div>
          </div>

        </div>
      </section>

      <section className="console-secondary agent-secondary">
        <div>
          <h3>提示</h3>
          <p>AI 辩手无需手动操作。主持人激活发言席位后，系统将自动触发 AI 生成并在大屏输出。本页面仅用于监控状态。</p>
        </div>
        <div>
          <h3>当前任务</h3>
          <p>{currentSpeaker?.id === speaker.id ? "当前 AI 正在发言或等待生成完成。" : `当前发言：${speakerLabel(currentSpeaker)}`}</p>
        </div>
      </section>
    </>
  );
}

function ConsoleStageCard({
  snapshot,
  currentPhase,
  currentSpeaker,
  phaseTarget,
  isFree,
  socketStatus
}: {
  snapshot: MatchSnapshot;
  currentPhase?: Phase;
  currentSpeaker?: Speaker;
  phaseTarget: string;
  isFree: boolean;
  socketStatus: string;
}) {
  return (
    <div className="console-stage-card">
      <div className="console-section-head">
        <span>当前阶段</span>
        <StatusPill tone={socketStatus === "open" ? "green" : "red"}>{socketStatus === "open" ? "已连接" : "连接中"}</StatusPill>
      </div>
      <h2>{currentPhase?.name ?? "等待环节"}</h2>
      <div className="console-stage-meta">
        <p>本环节发言：<strong>{phaseTarget}</strong></p>
        {snapshot.flow.awaiting_host_confirm && <p>现场状态：<strong>{snapshot.flow.message || "时间到，等待主持确认下一步"}</strong></p>}
        {isFree && <p>当前轮到：<strong>{sideLabel(snapshot.free_debate.current_turn_side)} · 第 {snapshot.free_debate.turn_index} 轮</strong></p>}
        <p>当前发言：<strong>{speakerLabel(currentSpeaker)}</strong></p>
      </div>
      <div className="console-timeline">
        {snapshot.phases.map((item) => (
          <span key={item.id} className={`${item.id === snapshot.match.current_phase_id ? "active" : ""} ${item.status}`}>
            {item.display_order}
          </span>
        ))}
      </div>
    </div>
  );
}

function initialSpeakerId(fallback: string): string {
  const params = new URLSearchParams(window.location.search);
  const pathSpeakerId = window.location.pathname.match(/^\/console\/([^/]+)/)?.[1];
  return (
    params.get("speaker_id")
    ?? (pathSpeakerId ? decodeURIComponent(pathSpeakerId) : null)
    ?? window.localStorage.getItem(activeSpeakerKey(params.get("match_id") ?? "current"))
    ?? fallback
  );
}

function initialDisplayName(matchId: string, speakerId: string): string {
  return window.localStorage.getItem(identityKey(matchId, speakerId)) ?? "";
}

function initialEntryStep(matchId: string, speakerId: string): EntryStep {
  return window.localStorage.getItem(entryReadyKey(matchId, speakerId)) === "1" ? "ready" : "identity";
}

function identityKey(matchId: string, speakerId: string): string {
  return `phdebate_console_name_${matchId}_${speakerId}`;
}

function activeSpeakerKey(matchId: string): string {
  return `phdebate_console_speaker_${matchId}`;
}

function entryReadyKey(matchId: string, speakerId: string): string {
  return `phdebate_console_ready_${matchId}_${speakerId}`;
}

function micTestLabel(status: "idle" | "testing" | "passed" | "failed"): string {
  if (status === "testing") return "正在请求麦克风权限...";
  if (status === "passed") return "麦克风可用，可以进入。";
  if (status === "failed") return "麦克风不可用，请检查浏览器授权。";
  return "尚未测试";
}

function checkLabel(status: CheckStatus, idleText: string): string {
  if (status === "testing") return "检测中...";
  if (status === "passed") return "检测通过";
  if (status === "failed") return "检测失败";
  return idleText;
}

function actionKey(path: string, body: Record<string, unknown> = {}): string {
  return `${path}:${JSON.stringify(body)}`;
}

function consoleActionLabel(path: string): string {
  if (path.endsWith("/start-speaking")) return "开始发言";
  if (path.endsWith("/start-agent-speaking")) return "启动 AI 发言";
  if (path.endsWith("/stop-speaking")) return "结束发言";
  if (path.endsWith("/pause-speaking")) return "暂停发言";
  if (path.endsWith("/resume-speaking")) return "继续发言";
  return "同步状态";
}

function canSkipMicTest(): boolean {
  return window.location.protocol === "http:" && !["localhost", "127.0.0.1", "::1"].includes(window.location.hostname);
}

function phaseTargetLabel(phase?: Phase): string {
  if (!phase) return "等待主持人安排";
  if (phase.phase_type === "free_debate") return "自由辩论，本方轮次内可发言";
  if (phase.side === "neutral") return "主持/评委环节";
  return `${sideLabel(phase.side)}${phase.speaker_seat ? seatLabel(phase.speaker_seat) : ""}`;
}

function phaseHelpText(phase: Phase | undefined, speaker: Speaker, freeTurnSide: Side): string {
  if (!phase) return "请等待主持人开始比赛。";
  if (phase.phase_type === "free_debate") {
    return freeTurnSide === speaker.side
      ? "轮到本方：2 秒内点「开始发言」即由你发言；不点或本方全部跳过，则由 AI 接管。"
      : `当前轮到${sideLabel(freeTurnSide)}发言。本方下一轮——可现在就点「跳过下一轮」预跳过；否则待对方说完后 2 秒内点「开始发言」。`;
  }
  if (phase.side === speaker.side && phase.speaker_seat === speaker.seat) {
    return "本环节指定你发言。主持人示意后，点击开始发言。";
  }
  return `请等待${phaseTargetLabel(phase)}发言。`;
}

function isAgentAllowedForPhase(speaker: Speaker, phase: Phase | undefined, freeTurnSide: Side): boolean {
  if (!phase) return false;
  if (phase.phase_type === "free_debate") return speaker.side === freeTurnSide;
  return phase.side === speaker.side && phase.speaker_seat === speaker.seat;
}

function agentStatusLabel(status?: string): string {
  if (!status) return "未检测";
  if (status === "ready") return "就绪";
  if (status === "speech_only") return "就绪";
  if (status === "streaming") return "生成中";
  if (status === "failed") return "异常";
  if (status === "ok") return "正常";
  return status;
}

function buildPrompt({
  active,
  canSpeak,
  displayName,
  matchStatus,
  currentPhase,
  speaker,
  currentSpeaker,
  freeTurnSide,
  flow
}: {
  active: boolean;
  canSpeak: boolean;
  displayName: string;
  matchStatus: string;
  currentPhase?: Phase;
  speaker: Speaker;
  currentSpeaker?: Speaker;
  freeTurnSide: Side;
  flow: MatchSnapshot["flow"];
}): { tone: "ready" | "speaking" | "waiting"; eyebrow: string; title: string; detail: string } {
  if (!active && flow.awaiting_host_confirm) {
    return {
      tone: "waiting",
      eyebrow: "时间到",
      title: "请等待主持确认下一步",
      detail: flow.message || "现场正在切换节奏，页面会自动更新。"
    };
  }
  if (speaker.speaker_type === "agent") {
    if (active) {
      return {
        tone: "speaking",
        eyebrow: "AI 发言中",
        title: `${speaker.name} 正在生成或播放发言`,
        detail: "请保持页面在线，主持台会同步发言状态。"
      };
    }
    if (canSpeak) {
      return {
        tone: "ready",
        eyebrow: "已获授权",
        title: `${speaker.name} 当前轮次可在本端启动`,
        detail: "赛制规则已把发言权限授予该 AI 席位，主持人激活后系统将自动开始生成发言。"
      };
    }
    if (matchStatus !== "running") {
      return {
        tone: "waiting",
        eyebrow: "等待比赛",
        title: "请等待主持人开始或继续比赛",
        detail: "当前页面会自动更新。"
      };
    }
    return {
      tone: "waiting",
      eyebrow: "等待轮次",
      title: `请等待${phaseWaitTarget(currentPhase, speaker, freeTurnSide)}`,
      detail: currentSpeaker ? `当前发言：${speakerLabel(currentSpeaker)}` : "当前尚未指定发言人。"
    };
  }
  if (active) {
    return {
      tone: "speaking",
      eyebrow: "正在发言",
      title: `${displayName || speaker.name}，保持发言`,
      detail: "发言结束后点击“结束发言”，系统会自动停止计时并保存记录。"
    };
  }
  if (canSpeak) {
    return {
      tone: "ready",
      eyebrow: "可以发言",
      title: `${displayName || speaker.name}，现在轮到你或本方`,
      detail: "主持人示意后点击“开始发言”。"
    };
  }
  if (matchStatus !== "running") {
    return {
      tone: "waiting",
      eyebrow: "等待比赛",
      title: "请等待主持人开始或继续比赛",
      detail: "当前页面会自动更新。"
    };
  }
  return {
    tone: "waiting",
    eyebrow: "尚未轮到你",
    title: `请等待${phaseWaitTarget(currentPhase, speaker, freeTurnSide)}`,
    detail: currentSpeaker ? `当前发言：${speakerLabel(currentSpeaker)}` : "当前尚未指定发言人。"
  };
}

function phaseWaitTarget(phase: Phase | undefined, speaker: Speaker, freeTurnSide: Side): string {
  if (!phase) return "主持人安排";
  if (phase.phase_type === "free_debate") return `${sideLabel(freeTurnSide)}发言`;
  if (phase.side === speaker.side && phase.speaker_seat === speaker.seat) return "主持人示意";
  return `${phaseTargetLabel(phase)}`;
}
