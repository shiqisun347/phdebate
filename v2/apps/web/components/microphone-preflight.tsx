"use client";

import { Mic, MicOff } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

const TEST_DURATION_MS = 4_000;
const LEVEL_SAMPLE_MS = 160;
const VOICE_LEVEL_THRESHOLD = 6;

type Outcome = "idle" | "testing" | "success" | "silent" | "error";

function microphoneErrorMessage(error: unknown) {
  const name = error instanceof DOMException ? error.name : "";
  if (name === "NotAllowedError" || name === "SecurityError") {
    return "麦克风权限被拒绝。请在浏览器地址栏的网站权限中允许麦克风，然后重新测试。";
  }
  if (name === "NotFoundError" || name === "DevicesNotFoundError") {
    return "未找到可用麦克风。请连接或启用麦克风，并确认系统输入设备设置后重新测试。";
  }
  if (name === "NotReadableError" || name === "TrackStartError") {
    return "麦克风可能正被其他应用占用，或系统暂时无法读取该设备。请关闭占用麦克风的应用后重试。";
  }
  if (name === "AbortError") return "浏览器中止了麦克风测试，请重新测试。";
  return error instanceof Error && error.message
    ? `麦克风测试失败：${error.message}。请检查浏览器和系统麦克风设置后重试。`
    : "麦克风测试失败。请检查浏览器和系统麦克风设置后重试。";
}

function levelCopy(level: number) {
  if (level < 12) return "音量偏低";
  if (level < 78) return "音量正常";
  return "音量过高";
}

function levelColor(level: number) {
  if (level < 12) return "#ffbd68";
  if (level < 78) return "#40df9c";
  return "#ff8092";
}

export function MicrophonePreflight() {
  const [outcome, setOutcome] = useState<Outcome>("idle");
  const [message, setMessage] = useState("测试不会录制或上传内容，完成后会立即释放麦克风。");
  const [level, setLevel] = useState(0);
  const streamRef = useRef<MediaStream | null>(null);
  const contextRef = useRef<AudioContext | null>(null);
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const intervalRef = useRef<number | null>(null);
  const timeoutRef = useRef<number | null>(null);
  const waitResolverRef = useRef<(() => void) | null>(null);
  const generationRef = useRef(0);

  const releaseResources = useCallback(async () => {
    if (intervalRef.current !== null) window.clearInterval(intervalRef.current);
    intervalRef.current = null;
    if (timeoutRef.current !== null) window.clearTimeout(timeoutRef.current);
    timeoutRef.current = null;
    const resolveWait = waitResolverRef.current;
    waitResolverRef.current = null;
    resolveWait?.();
    try { sourceRef.current?.disconnect(); } catch { /* continue releasing the capture device */ }
    sourceRef.current = null;
    const stream = streamRef.current;
    streamRef.current = null;
    stream?.getTracks().forEach((track) => {
      try { track.stop(); } catch { /* continue releasing remaining resources */ }
    });
    const context = contextRef.current;
    contextRef.current = null;
    if (context) await context.close().catch(() => undefined);
  }, []);

  useEffect(() => () => {
    generationRef.current += 1;
    void releaseResources();
  }, [releaseResources]);

  async function runTest() {
    const generation = ++generationRef.current;
    await releaseResources();
    if (generation !== generationRef.current) return;
    setOutcome("testing");
    setMessage("正在检测默认麦克风，请用正常音量说一句话…");
    setLevel(0);
    let peakLevel = 0;
    try {
      if (
        !navigator.mediaDevices?.getUserMedia
        || typeof MediaRecorder === "undefined"
        || typeof AudioContext === "undefined"
      ) {
        throw new Error("当前浏览器不支持比赛所需的麦克风录音，请使用最新版 Chrome、Edge 或 Safari");
      }
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      if (generation !== generationRef.current) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      streamRef.current = stream;
      const context = new AudioContext();
      contextRef.current = context;
      if (context.state === "suspended") await context.resume();
      const source = context.createMediaStreamSource(stream);
      sourceRef.current = source;
      const analyser = context.createAnalyser();
      analyser.fftSize = 1024;
      source.connect(analyser);
      const samples = new Float32Array(analyser.fftSize);
      intervalRef.current = window.setInterval(() => {
        if (generation !== generationRef.current) return;
        analyser.getFloatTimeDomainData(samples);
        let squared = 0;
        for (const sample of samples) squared += sample * sample;
        const rms = samples.length ? Math.sqrt(squared / samples.length) : 0;
        const nextLevel = Math.min(100, Math.round(rms * 500));
        peakLevel = Math.max(peakLevel, nextLevel);
        setLevel(nextLevel);
      }, LEVEL_SAMPLE_MS);
      await new Promise<void>((resolve) => {
        waitResolverRef.current = resolve;
        timeoutRef.current = window.setTimeout(() => {
          timeoutRef.current = null;
          waitResolverRef.current = null;
          resolve();
        }, TEST_DURATION_MS);
      });
      if (generation !== generationRef.current) return;
      if (peakLevel >= VOICE_LEVEL_THRESHOLD) {
        setOutcome("success");
        setMessage("麦克风可用，已检测到声音。测试完成，可继续准备比赛。");
      } else {
        setOutcome("silent");
        setMessage("麦克风已打开，但未检测到清晰声音。请确认设备未静音、选择了正确输入设备，并靠近麦克风后重试。");
      }
    } catch (error) {
      if (generation === generationRef.current) {
        setOutcome("error");
        setMessage(microphoneErrorMessage(error));
      }
    } finally {
      await releaseResources();
    }
  }

  const statusClass = outcome === "success" ? "notice-box" : outcome === "idle" ? "muted" : outcome === "testing" ? "warning-box" : "error-box";
  return (
    <div aria-label="麦克风预检">
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <button type="button" className="button button-secondary" disabled={outcome === "testing"} onClick={() => void runTest()}>
          {outcome === "testing" ? <MicOff size={17} /> : <Mic size={17} />}
          {outcome === "testing" ? "正在测试麦克风…" : "测试默认麦克风"}
        </button>
        {(outcome === "testing" || outcome === "success" || outcome === "silent") && (
          <div style={{ flex: "1 1 180px", minWidth: 160 }}>
            <div
              role="meter"
              aria-label="麦克风输入音量"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={level}
              aria-valuetext={levelCopy(level)}
              style={{ height: 10, overflow: "hidden", borderRadius: 999, background: "rgba(140,155,191,.22)" }}
            >
              <span aria-hidden="true" style={{ display: "block", width: `${level}%`, height: "100%", background: levelColor(level), transition: `width ${LEVEL_SAMPLE_MS}ms linear` }} />
            </div>
            <small style={{ display: "block", marginTop: 5, color: levelColor(level) }}>{levelCopy(level)}</small>
          </div>
        )}
      </div>
      <div className={statusClass} role={outcome === "error" || outcome === "silent" ? "alert" : "status"} aria-live="polite" style={{ marginTop: 10 }}>{message}</div>
    </div>
  );
}
