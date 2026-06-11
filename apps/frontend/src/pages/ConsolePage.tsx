import { Bot, Mic, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { post, uploadAudioChunk } from "../api/client";
import { AuthPrompt } from "../components/AuthPrompt";
import { ClockTile } from "../components/ClockTile";
import { StatusPill } from "../components/StatusPill";
import { clockByName, seatLabel, sideClass, sideLabel } from "../state/format";
import { useMatch } from "../realtime/useMatch";

interface ConsolePageProps {
  matchId: string;
  speakerId: string;
}

const PCM_SAMPLE_RATE = 16000;
const PCM_CHUNK_MS = 500;
const PCM_SAMPLES_PER_CHUNK = PCM_SAMPLE_RATE * (PCM_CHUNK_MS / 1000);
const PCM_MIME_TYPE = "audio/L16;rate=16000";

interface BrowserWindowWithAudioContext extends Window {
  webkitAudioContext?: typeof AudioContext;
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
  if (sourceRate <= PCM_SAMPLE_RATE) {
    return Int16Array.from(input, floatToPcmSample);
  }
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

function pcmBlob(samples: number[]): Blob {
  const pcm = new Int16Array(samples);
  const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  return new Blob([bytes], { type: PCM_MIME_TYPE });
}

export function ConsolePage({ matchId, speakerId }: ConsolePageProps) {
  const { snapshot, socketStatus, loadError, refresh, send } = useMatch(matchId, "speaker", speakerId);
  const [error, setError] = useState<string | null>(null);
  const [audioStatus, setAudioStatus] = useState("等待发言");
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunkIndexRef = useRef(0);
  const pendingUploadsRef = useRef<Promise<unknown>[]>([]);
  const sendRef = useRef(send);
  sendRef.current = send;

  const speaker = snapshot?.speakers.find((item) => item.id === speakerId) ?? snapshot?.speakers[0] ?? null;
  const active = Boolean(snapshot?.current_speech?.speaker_id && speaker && snapshot.current_speech.speaker_id === speaker.id);
  const activeSpeechId = active ? snapshot?.current_speech?.id ?? null : null;
  const activeSpeakerId = speaker?.id ?? null;
  const speakerType = speaker?.speaker_type;

  useEffect(() => {
    if (!activeSpeechId || !activeSpeakerId || speakerType !== "human") {
      setAudioStatus("等待发言");
      return;
    }

    let cancelled = false;
    let localRecorder: MediaRecorder | null = null;
    let localStream: MediaStream | null = null;
    let stopPcmArchive: (() => void) | null = null;
    let completionQueued = false;
    const currentSpeechId = activeSpeechId;
    const currentSpeakerId = activeSpeakerId;

    function enqueueUpload(blob: Blob, durationMs: number, filename?: string) {
      const chunkIndex = chunkIndexRef.current;
      chunkIndexRef.current += 1;
      const upload = uploadAudioChunk(matchId, currentSpeechId, currentSpeakerId, chunkIndex, blob, durationMs, filename)
        .then((nextSnapshot) => {
          const asset = nextSnapshot.audio_assets.find((item) => item.speech_id === currentSpeechId);
          if (!cancelled) setAudioStatus(`已归档 ${asset?.chunk_count ?? chunkIndex + 1} 段`);
        })
        .catch((err) => {
          if (!cancelled) {
            setAudioStatus("音频归档上传失败");
            setError(err instanceof Error ? err.message : "音频归档上传失败");
          }
        });
      pendingUploadsRef.current.push(upload);
      upload.finally(() => {
        pendingUploadsRef.current = pendingUploadsRef.current.filter((item) => item !== upload);
      });
    }

    function queueArchiveComplete() {
      if (completionQueued || chunkIndexRef.current === 0) return;
      completionQueued = true;
      const uploads = [...pendingUploadsRef.current];
      void Promise.allSettled(uploads).then(() =>
        post(`/api/matches/${matchId}/speeches/${currentSpeechId}/audio/complete`, { speaker_id: currentSpeakerId })
          .then(() => {
            if (!cancelled) setAudioStatus("音频归档已完成");
          })
          .catch(() => {
            if (!cancelled) setAudioStatus("音频归档完成确认失败");
          })
      );
    }

    function startPcmArchive(stream: MediaStream): (() => void) | null {
      const audioContext = createBrowserAudioContext();
      if (!audioContext) return null;
      void audioContext.resume().catch(() => undefined);
      const source = audioContext.createMediaStreamSource(stream);
      const processor = audioContext.createScriptProcessor(4096, 1, 1);
      const mute = audioContext.createGain();
      const sampleBuffer: number[] = [];
      mute.gain.value = 0;

      function flush(force = false) {
        while (sampleBuffer.length >= PCM_SAMPLES_PER_CHUNK || (force && sampleBuffer.length > 0)) {
          const take = force ? Math.min(sampleBuffer.length, PCM_SAMPLES_PER_CHUNK) : PCM_SAMPLES_PER_CHUNK;
          const samples = sampleBuffer.splice(0, take);
          const chunkNumber = chunkIndexRef.current;
          const filename = `chunk_${String(chunkNumber).padStart(5, "0")}.pcm`;
          enqueueUpload(pcmBlob(samples), Math.round((samples.length / PCM_SAMPLE_RATE) * 1000), filename);
        }
      }

      processor.onaudioprocess = (event) => {
        if (cancelled) return;
        const input = event.inputBuffer.getChannelData(0);
        const pcm = downsampleToPcm(input, audioContext.sampleRate);
        for (const sample of pcm) sampleBuffer.push(sample);
        flush(false);
      };

      source.connect(processor);
      processor.connect(mute);
      mute.connect(audioContext.destination);
      setAudioStatus("PCM/L16 归档中");

      return () => {
        processor.onaudioprocess = null;
        flush(true);
        queueArchiveComplete();
        source.disconnect();
        processor.disconnect();
        mute.disconnect();
        void audioContext.close();
      };
    }

    function startMediaRecorderArchive(stream: MediaStream): boolean {
      if (typeof MediaRecorder === "undefined") return false;
      const preferredMimeType = "audio/webm;codecs=opus";
      const options = MediaRecorder.isTypeSupported(preferredMimeType) ? { mimeType: preferredMimeType } : undefined;
      localRecorder = new MediaRecorder(stream, options);
      recorderRef.current = localRecorder;
      setAudioStatus("webm 归档中");

      localRecorder.ondataavailable = (event) => {
        if (!event.data.size) return;
        const chunkNumber = chunkIndexRef.current;
        const filename = `chunk_${String(chunkNumber).padStart(5, "0")}.webm`;
        enqueueUpload(event.data, PCM_CHUNK_MS, filename);
      };

      localRecorder.onerror = () => {
        if (!cancelled) setAudioStatus("录音归档异常");
      };

      localRecorder.onstop = queueArchiveComplete;
      localRecorder.start(PCM_CHUNK_MS);
      return true;
    }

    async function startArchive() {
      if (!navigator.mediaDevices?.getUserMedia) {
        setAudioStatus("浏览器不支持录音归档");
        sendRef.current("speaker.mic_error", {
          speaker_id: currentSpeakerId,
          mic_permission: "unknown",
          device_label: "browser microphone",
          message: "getUserMedia unavailable"
        });
        return;
      }

      try {
        setAudioStatus("麦克风授权中");
        localStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        if (cancelled) {
          localStream.getTracks().forEach((track) => track.stop());
          return;
        }

        streamRef.current = localStream;
        chunkIndexRef.current = 0;
        pendingUploadsRef.current = [];
        try {
          stopPcmArchive = startPcmArchive(localStream);
        } catch {
          stopPcmArchive = null;
        }
        if (stopPcmArchive) return;
        if (startMediaRecorderArchive(localStream)) return;
        setAudioStatus("浏览器不支持录音归档");
        sendRef.current("speaker.mic_error", {
          speaker_id: currentSpeakerId,
          mic_permission: "unknown",
          device_label: "browser microphone",
          message: "AudioContext and MediaRecorder unavailable"
        });
      } catch (err) {
        if (!cancelled) {
          setAudioStatus("麦克风不可用，已上报主持人");
          setError(err instanceof Error ? err.message : "麦克风不可用");
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
          // Browser may already have flushed data during stop.
        }
        recorder.stop();
      }
      (localStream ?? streamRef.current)?.getTracks().forEach((track) => track.stop());
      recorderRef.current = null;
      streamRef.current = null;
    };
  }, [activeSpeechId, activeSpeakerId, matchId, speakerType]);

  async function action(path: string, body: Record<string, unknown> = {}) {
    try {
      setError(null);
      await post(path, body);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    }
  }

  if (!snapshot && loadError) return <AuthPrompt role="speaker" speakerId={speakerId} message={loadError} />;
  if (!snapshot || !speaker) return <div className="loading">正在连接辩手控制台...</div>;

  const currentSpeakerId = speaker.id;
  const currentPhase = snapshot.phases.find((item) => item.id === snapshot.match.current_phase_id);
  const isFree = currentPhase?.phase_type === "free_debate";
  const isCurrentTurnSide = !isFree || snapshot.free_debate.current_turn_side === speaker.side;
  const canSpeak = snapshot.match.status === "running" && isCurrentTurnSide && (active || isFree || (!snapshot.current_speech && currentPhase?.side === speaker.side && currentPhase.speaker_seat === speaker.seat));
  const aiTeammate = snapshot.speakers.find((item) => item.side === speaker.side && item.speaker_type === "agent");
  const currentSpeechText = active ? snapshot.current_speech?.content_partial || snapshot.current_speech?.content_final : "";
  const currentAudioAsset = snapshot.audio_assets.find((item) => item.speech_id === activeSpeechId) ?? snapshot.audio_assets.find((item) => item.speaker_id === speaker.id);
  const micPermission = speaker.mic_permission ?? "unknown";

  function reportMicError() {
    const sent = send("speaker.mic_error", {
      speaker_id: currentSpeakerId,
      mic_permission: "denied",
      device_label: "browser microphone",
      message: "Speaker reported microphone issue"
    });
    if (!sent) {
      setError("WebSocket 未连接，暂不能上报麦克风异常。");
      return;
    }
    window.setTimeout(() => void refresh(), 300);
  }

  return (
    <main className="console-shell">
      <section className="console-card identity-card">
        <div className={`avatar big ${sideClass(speaker.side)}`}>{speaker.name.slice(0, 1)}</div>
        <div>
          <h1>{speaker.name}</h1>
          <p>{snapshot.teams.find((team) => team.id === speaker.team_id)?.name} · {seatLabel(speaker.seat)}</p>
        </div>
        <StatusPill tone={speaker.side === "affirmative" ? "blue" : "red"}>{sideLabel(speaker.side)}</StatusPill>
      </section>

      <section className="console-card">
        <div className="console-phase">
          <span>当前环节</span>
          <strong>{currentPhase?.name}</strong>
          <StatusPill tone={socketStatus === "open" ? "green" : "red"}>{socketStatus}</StatusPill>
        </div>
        <div className={`console-banner ${active ? "speaking" : canSpeak ? "ready" : "waiting"}`}>
          {active ? "发言中 · 正在记录" : canSpeak ? "轮到你或本方发言" : `等待中 · 当前发言：${snapshot.current_speech?.speaker_id ?? "未指定"}`}
        </div>
        {isFree && (
          <div className="console-turn">当前轮次：{sideLabel(snapshot.free_debate.current_turn_side)} · 第 {snapshot.free_debate.turn_index} 轮</div>
        )}
      </section>
      {error && <div className="console-error">{error}</div>}

      <section className="console-card timer-card">
        {isFree ? (
          <>
            <ClockTile label="单次上限" clock={clockByName(snapshot.clocks, "turn")} tone="turn" />
            <div className="console-free-row">
              <span>本方剩余</span>
              <ClockTile
                label={sideLabel(speaker.side)}
                clock={clockByName(snapshot.clocks, `${speaker.side}_total`)}
                tone={speaker.side === "affirmative" ? "aff" : "neg"}
                compact
              />
            </div>
          </>
        ) : (
          <ClockTile label="本环节剩余" clock={clockByName(snapshot.clocks, "main")} tone={speaker.side === "affirmative" ? "aff" : "neg"} />
        )}
      </section>

      <section className="console-card speech-monitor">
        <div>
          <span>ASR</span>
          <strong>{snapshot.speech_service.asr.status} · {snapshot.speech_service.asr.latency_ms}ms</strong>
        </div>
        <div>
          <span>麦克风</span>
          <strong>{micPermission}</strong>
        </div>
        <div>
          <span>录音归档</span>
          <strong>{audioStatus}{currentAudioAsset ? ` · ${currentAudioAsset.chunk_count} 段` : ""}</strong>
        </div>
        {active && <p>{currentSpeechText || "等待转写输入..."}</p>}
      </section>

      <section className="console-actions">
        {active ? (
          <button className="mic-button stop" onClick={() => action(`/api/matches/${matchId}/speakers/${speaker.id}/stop-speaking`)}>
            <Square size={28} />结束发言
          </button>
        ) : (
          <button
            className={`mic-button ${canSpeak ? "start" : "disabled"}`}
            disabled={!canSpeak}
            onClick={() => action(`/api/matches/${matchId}/speakers/${speaker.id}/start-speaking`)}
          >
            <Mic size={28} />开始发言
          </button>
        )}
        {isFree && aiTeammate && (
          <button
            className="ai-request"
            disabled={!canSpeak}
            onClick={() => action(`/api/matches/${matchId}/speakers/${speaker.id}/request-ai-teammate`, { agent_speaker_id: aiTeammate.id })}
          >
            <Bot size={20} />让 AI 队友发言（{aiTeammate.name}）
          </button>
        )}
        <button className="ai-request" onClick={reportMicError}>
          <Mic size={20} />报告麦克风异常
        </button>
      </section>
    </main>
  );
}
