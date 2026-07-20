#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { resolve } from "node:path";

const session = `realtime-audio-probe-smoke-${process.pid}`;
const probe = resolve("scripts/browser/realtime_audio_probe.js");
const fixture = `file://${resolve("scripts/tests/realtime_audio_probe_fixture.html")}`;
const run = (args, allowFailure = false) => {
  const result = spawnSync("agent-browser", ["--session", session, ...args], { encoding: "utf8" });
  if (!allowFailure && result.status !== 0) throw new Error(result.stderr || result.stdout);
  return result.stdout.trim();
};
const parse = (value) => {
  const parsed = JSON.parse(value);
  return parsed?.data ?? parsed;
};

try {
  run(["--headed", "open", fixture, "--init-script", probe]);
  run(["wait", "--load", "domcontentloaded"]);
  run(["find", "role", "button", "click", "--name", "开启比赛声音"]);
  let report;
  for (let attempt = 0; attempt < 50; attempt += 1) {
    report = parse(run(["eval", "window.__jixiaRealtimeAudioGate.report()"]));
    if (report.turn?.firstNonSilentAtMs != null) break;
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 100);
  }
  if (report?.turn?.firstNonSilentAtMs == null) throw new Error("probe did not detect the first non-silent sample");
  run(["find", "role", "button", "click", "--name", "中断音频"]);
  for (let attempt = 0; attempt < 50; attempt += 1) {
    report = parse(run(["eval", "window.__jixiaRealtimeAudioGate.report()"]));
    if (report.interrupt?.sustainedSilenceAtMs != null) break;
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 100);
  }
  if (report?.interrupt?.firstFlushActionAtMs == null) throw new Error("probe did not observe the media flush action");
  if (report?.interrupt?.sustainedSilenceAtMs == null) throw new Error("probe did not observe sustained silence");
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 800);
  report = parse(run(["eval", "window.__jixiaRealtimeAudioGate.report()"]));
  if (report?.interrupt?.staleAudioAfterSilenceAtMs != null) throw new Error("probe observed stale audio after silence");
  const firstSoundMs = report.derived.agent_delta_to_browser_non_silent_clock_estimate_ms;
  const interruptToFlushMs = report.derived.interrupt_event_to_flush_ms;
  const interruptToSilenceMs = report.derived.interrupt_event_to_sustained_silence_ms;
  if (!Number.isFinite(firstSoundMs) || firstSoundMs < 0 || firstSoundMs > 2500) {
    throw new Error(`synthetic first-sound gate exceeded 2500ms: ${firstSoundMs}`);
  }
  if (!Number.isFinite(interruptToFlushMs) || interruptToFlushMs < 0 || interruptToFlushMs > 250) {
    throw new Error(`synthetic interrupt flush gate exceeded 250ms: ${interruptToFlushMs}`);
  }
  if (!Number.isFinite(interruptToSilenceMs) || interruptToSilenceMs < 0 || interruptToSilenceMs > 250) {
    throw new Error(`synthetic interrupt silence gate exceeded 250ms: ${interruptToSilenceMs}`);
  }
  console.log(JSON.stringify({
    ok: true,
    thresholds: {
      first_sound_ms: 2500,
      interrupt_ms: 250,
    },
    first_sound_ms: firstSoundMs,
    interrupt_to_flush_ms: interruptToFlushMs,
    interrupt_to_silence_ms: interruptToSilenceMs,
  }, null, 2));
} finally {
  run(["close"], true);
}
