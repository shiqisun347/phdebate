#!/usr/bin/env node

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import process from "node:process";

const args = process.argv.slice(2);
const value = (name, fallback = "") => {
  const index = args.indexOf(name);
  return index >= 0 && index + 1 < args.length ? args[index + 1] : fallback;
};
const has = (name) => args.includes(name);
const url = value("--url");
const output = resolve(value("--output", "docs/qa/audio/browser-realtime-audio-gate.json"));
const session = value("--session", "jixia-realtime-audio-gate");
const mode = value("--mode", "first-sound");
const timeoutMs = Number(value("--timeout-ms", "45000"));
const agentFirstDeltaAt = value("--agent-first-delta-at");
const soundLimitMs = Number(value("--sound-limit-ms", "3000"));
const interruptLimitMs = Number(value("--interrupt-limit-ms", "250"));
const staleGuardMs = Number(value("--stale-guard-ms", "750"));
const maxAudioBitrateBps = Number(value("--max-audio-bitrate-bps", "128000"));
const localInterruptButtonName = value("--local-interrupt-button-name");
const probePath = resolve("scripts/browser/realtime_audio_probe.js");

if (!url) {
  console.error("usage: run_browser_realtime_audio_gate.mjs --url <room-url> [--mode first-sound|interrupt] [--output path]");
  process.exit(2);
}
if (!new Set(["first-sound", "interrupt"]).has(mode)) {
  console.error("--mode must be first-sound or interrupt");
  process.exit(2);
}
if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
  console.error("--timeout-ms must be positive");
  process.exit(2);
}

const run = (commandArgs, options = {}) => {
  const result = spawnSync("agent-browser", ["--session", session, ...commandArgs], {
    encoding: "utf8",
    input: options.input,
    env: process.env,
  });
  if (!options.allowFailure && result.status !== 0) {
    throw new Error(`agent-browser ${commandArgs.join(" ")} failed: ${result.stderr || result.stdout}`);
  }
  return result;
};

const evalJson = (script) => {
  const result = run(["eval", "--stdin"], { input: script });
  const raw = result.stdout.trim();
  try {
    const parsed = JSON.parse(raw);
    return parsed?.data ?? parsed;
  } catch {
    throw new Error(`could not parse agent-browser eval output: ${raw}`);
  }
};

const snapshotPath = output.replace(/\.json$/i, ".png");
mkdirSync(dirname(output), { recursive: true });

let finalReport;
try {
  run([...(has("--headless") ? [] : ["--headed"]), "open", url, "--init-script", probePath]);
  run(["wait", "--load", "domcontentloaded"]);
  // The stage uses a compact visible label (for example “开启声音”) and a
  // fuller accessible name (“开启比赛声音”), so innerText isn't a reliable
  // readiness signal. Allow the client-rendered stage to mount before using
  // role/name lookup below.
  run(["wait", "1000"]);
  evalJson("window.__jixiaRealtimeAudioGate.configure({ audibleRms: 0.003, silenceRms: 0.001, audibleFrames: 3, silenceFrames: 8 })");

  // Force one trusted playback-unlock click. If sound was already enabled,
  // toggle it off first so the second click resumes both LiveKit and the probe.
  const disableSound = run(
    ["find", "role", "button", "click", "--name", "关闭比赛声音"],
    { allowFailure: true },
  );
  if (disableSound.status === 0) {
    // React updates the accessible label after the click. Give the committed
    // button state a moment to reach the browser automation snapshot before
    // looking for either of the two supported enable labels.
    run(["wait", "200"]);
  }
  const enableSound = run(
    ["find", "role", "button", "click", "--name", "开启比赛声音"],
    { allowFailure: true },
  );
  if (enableSound.status !== 0) {
    run(["find", "role", "button", "click", "--name", "播放比赛声音"]);
  }

  const deadline = Date.now() + timeoutMs;
  let report = null;
  let localInterruptTriggered = false;
  while (Date.now() < deadline) {
    report = evalJson("window.__jixiaRealtimeAudioGate.report()");
    if (agentFirstDeltaAt && report?.turn && !report.turn.agentFirstReadableDeltaAt) {
      evalJson(`window.__jixiaRealtimeAudioGate.markAgentFirstReadableDelta(${JSON.stringify(agentFirstDeltaAt)})`);
      report = evalJson("window.__jixiaRealtimeAudioGate.report()");
    }
    const soundReady = report?.turn?.firstNonSilentAtMs != null;
    const networkReady = Boolean(report?.network?.last?.mimeType);
    const interruptSilenceAtMs = report?.interrupt?.sustainedSilenceAtMs;
    const interruptReady = interruptSilenceAtMs != null && (
      report?.interrupt?.staleAudioAfterSilenceAtMs != null
      || report?.interrupt?.newGenerationAtMs != null
      || Date.now() - interruptSilenceAtMs >= staleGuardMs
    );
    if (mode === "interrupt" && soundReady && localInterruptButtonName && !localInterruptTriggered) {
      const target = new URL(url);
      if (!(target.protocol === "file:" || ["localhost", "127.0.0.1", "[::1]"].includes(target.hostname))) {
        throw new Error("--local-interrupt-button-name is restricted to file:// or localhost targets");
      }
      run(["find", "role", "button", "click", "--name", localInterruptButtonName]);
      localInterruptTriggered = true;
    }
    if (mode === "first-sound" ? soundReady && networkReady : soundReady && networkReady && interruptReady) break;
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 200);
  }
  report = evalJson("window.__jixiaRealtimeAudioGate.report()");
  const firstSoundMs = report?.derived?.agent_delta_to_browser_non_silent_clock_estimate_ms;
  const captureToSoundMs = report?.derived?.server_capture_to_browser_non_silent_clock_estimate_ms;
  const interruptMs = report?.derived?.interrupt_event_to_sustained_silence_ms;
  const staleGuardCompleted = mode !== "interrupt" || Boolean(
    report?.interrupt?.sustainedSilenceAtMs != null
    && (
      report?.interrupt?.newGenerationAtMs != null
      || Date.now() - report.interrupt.sustainedSilenceAtMs >= staleGuardMs
    ),
  );
  const missingAgentDelta = !report?.turn?.agentFirstReadableDeltaAt;
  const activeRtcTurnObserved = Boolean(report?.turn);
  const codec = String(report?.network?.last?.mimeType || "").toLowerCase();
  const measuredBitrateBps = Number(report?.network?.last?.bitrateBps) || 0;
  const codecStatsAvailable = Boolean(codec);
  const compressedAudioGatePass = codecStatsAvailable
    && (codec.includes("opus") || codec.includes("red"))
    && measuredBitrateBps <= maxAudioBitrateBps;
  const firstSoundComponentPass = report?.turn?.firstNonSilentAtMs != null;
  const firstSoundGatePass = !missingAgentDelta
    && firstSoundMs != null
    && firstSoundMs >= 0
    && firstSoundMs <= soundLimitMs;
  const interruptGatePass = mode !== "interrupt" || Boolean(
    report?.interrupt?.audibleBeforeInterrupt
    && report?.interrupt?.firstFlushActionAtMs != null
    && interruptMs != null
    && interruptMs >= 0
    && interruptMs <= interruptLimitMs
    && staleGuardCompleted
    && report?.interrupt?.staleAudioAfterSilenceAtMs == null,
  );
  const gateStatus = !activeRtcTurnObserved
    ? "BLOCKED_NO_ACTIVE_RTC_TURN"
    : !codecStatsAvailable
      ? "BLOCKED_MISSING_RTC_CODEC_STATS"
      : !compressedAudioGatePass
        ? "FAIL_AUDIO_CODEC_OR_BITRATE"
    : !firstSoundComponentPass
    ? "FAIL_BROWSER_FIRST_SOUND"
    : missingAgentDelta
      ? "BLOCKED_MISSING_AGENT_DELTA_TIMESTAMP"
      : !firstSoundGatePass
        ? "FAIL_END_TO_END_FIRST_SOUND"
        : !interruptGatePass
          ? "FAIL_INTERRUPT_FLUSH"
          : "PASS";
  finalReport = {
    generated_at: new Date().toISOString(),
    browser: "Chromium via agent-browser (headed unless --headless)",
    url,
    mode,
    thresholds: {
      agent_first_readable_delta_to_browser_non_silent_ms: soundLimitMs,
      interrupt_event_to_sustained_silence_ms: interruptLimitMs,
      stale_audio_guard_ms: staleGuardMs,
      maximum_audio_bitrate_bps: maxAudioBitrateBps,
    },
    gate_status: gateStatus,
    checks: {
      active_rtc_turn_observed: activeRtcTurnObserved,
      compressed_webrtc_audio: compressedAudioGatePass,
      digital_non_silent_sample_after_playback_unlock: firstSoundComponentPass,
      agent_delta_timestamp_available: !missingAgentDelta,
      end_to_end_first_sound: firstSoundGatePass,
      interrupt_flush: interruptGatePass,
      stale_audio_guard_completed: staleGuardCompleted,
    },
    limitations: [
      "WebAudio measures decoded non-silent samples after a trusted playback-unlock click; it cannot prove OS speaker volume or human audibility.",
      "The direct server/client wall-clock estimates require synchronized clocks. rtc_event_to_non_silent_ms is monotonic within the browser and remains valid without clock sync.",
      "Interrupt mode never triggers a match action. Run it against a controlled canary and pause/terminate externally after audio becomes audible.",
    ],
    probe: report,
  };
  writeFileSync(output, `${JSON.stringify(finalReport, null, 2)}\n`);
  run(["screenshot", snapshotPath]);
} catch (error) {
  finalReport = {
    generated_at: new Date().toISOString(),
    browser: "Chromium via agent-browser",
    url,
    mode,
    gate_status: "ERROR",
    error: error instanceof Error ? error.message : String(error),
  };
  writeFileSync(output, `${JSON.stringify(finalReport, null, 2)}\n`);
  console.error(finalReport.error);
  process.exitCode = 1;
} finally {
  if (!has("--keep-open")) run(["close"], { allowFailure: true });
}

console.log(output);
