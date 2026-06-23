import { describe, expect, it } from "vitest";
import {
  DEFAULT_PLAYBACK_CONFIG,
  emptyPosition,
  reconcile,
  type ActiveMediaState,
  type PlaybackConfig,
  type PlaybackDecision,
  type PlaybackPosition,
  type PlaybackSpeech,
} from "./playbackReducer";

function mkSpeech(overrides: Partial<PlaybackSpeech> = {}): PlaybackSpeech {
  return {
    speechId: "S1",
    speakerId: "spk_aff_1",
    taskId: "T1",
    source: "agent_text",
    state: "speaking",
    expectedSentences: null,
    createdSentences: 0,
    skippedSentences: [],
    chunks: [],
    ...overrides,
  };
}

function chunks(...idxs: number[]) {
  return idxs.map((i) => ({ sentenceIdx: i, audioUrl: `/api/audio/s${i}.mp3` }));
}

/**
 * 模拟"胶水层 + 真实播放"：把一段发言播放到结束。每个 PLAY 视为瞬间播完（media→ended），
 * WAIT 时把时钟推过看门狗以触发自愈，返回完整决策序列。仅用于 expected 已知（会收敛）的用例。
 */
function playToEnd(
  speech: PlaybackSpeech,
  opts: { config?: Partial<PlaybackConfig>; maxSteps?: number; now?: number } = {}
): PlaybackDecision[] {
  const config: PlaybackConfig = { ...DEFAULT_PLAYBACK_CONFIG, ...(opts.config ?? {}) };
  let pos: PlaybackPosition = emptyPosition();
  let now = opts.now ?? 1000;
  let media: ActiveMediaState = "idle";
  const out: PlaybackDecision[] = [];
  for (let step = 0; step < (opts.maxSteps ?? 200); step++) {
    const d = reconcile({ speech, position: pos, nowMs: now, audioEnabled: true, suppressed: false, activeMediaState: media }, config);
    out.push(d);
    pos = d.position;
    if (d.kind === "DONE" || d.kind === "STOP") break;
    if (d.kind === "PLAY") {
      media = "ended"; // 瞬间播完
      now += 1;
    } else if (d.kind === "WAIT") {
      media = "idle";
      now += config.perSentenceWatchdogMs + 1; // 推过看门狗
    } else {
      media = "idle"; // NOTIFY_START / SKIP / IDLE
    }
  }
  return out;
}

const playedIdx = (ds: PlaybackDecision[]) => ds.filter((d) => d.kind === "PLAY").map((d: any) => d.sentenceIdx);
const skippedIdx = (ds: PlaybackDecision[]) => ds.filter((d) => d.kind === "SKIP").map((d: any) => d.sentenceIdx);
const kinds = (ds: PlaybackDecision[]) => ds.map((d) => d.kind);

describe("reconcile — 顺序播放与完成", () => {
  it("1. 按序播放全部分段并完成", () => {
    const ds = playToEnd(mkSpeech({ expectedSentences: 3, chunks: chunks(0, 1, 2) }));
    expect(playedIdx(ds)).toEqual([0, 1, 2]);
    expect(kinds(ds)).toContain("DONE");
    expect(skippedIdx(ds)).toEqual([]);
  });

  it("2. 乱序到达：chunks 顺序无关，仍严格按序播放", () => {
    const ds = playToEnd(mkSpeech({ expectedSentences: 3, chunks: [chunks(2), chunks(0), chunks(1)].flat() }));
    expect(playedIdx(ds)).toEqual([0, 1, 2]);
  });

  it("16. 恰好完成：nextIdx 到达 expected 触发一次 DONE", () => {
    const ds = playToEnd(mkSpeech({ expectedSentences: 2, chunks: chunks(0, 1) }));
    expect(playedIdx(ds)).toEqual([0, 1]);
    expect(ds.filter((d) => d.kind === "DONE")).toHaveLength(1);
  });

  it("24. expected=0：立即完成、不播放", () => {
    const ds = playToEnd(mkSpeech({ expectedSentences: 0, chunks: [] }));
    expect(playedIdx(ds)).toEqual([]);
    expect(kinds(ds)).toContain("DONE");
  });

  it("23. 首次到达可播段先 NOTIFY_START，仅一次", () => {
    const ds = playToEnd(mkSpeech({ expectedSentences: 2, chunks: chunks(0, 1) }));
    expect(ds.filter((d) => d.kind === "NOTIFY_START")).toHaveLength(1);
    expect((ds.find((d) => d.kind === "NOTIFY_START") as any).sentenceIdx).toBe(0);
  });
});

describe("reconcile — 缺口与自愈（绝不卡死）", () => {
  it("3. 缺一段但在 skip 列表 → 立即跳过", () => {
    const ds = playToEnd(mkSpeech({ expectedSentences: 2, chunks: chunks(0), skippedSentences: [1] }));
    expect(playedIdx(ds)).toEqual([0]);
    expect(skippedIdx(ds)).toEqual([1]);
    expect(kinds(ds)).toContain("DONE");
    expect((ds.find((d) => d.kind === "SKIP") as any).reason).toBe("in_skip_list");
  });

  it("4. 缺一段且未在 skip 列表 → 看门狗超时后跳过（这是历史上的永久卡死 bug）", () => {
    const ds = playToEnd(mkSpeech({ expectedSentences: 2, chunks: chunks(0) }));
    expect(playedIdx(ds)).toEqual([0]);
    expect(skippedIdx(ds)).toEqual([1]);
    expect((ds.find((d) => d.kind === "SKIP") as any).reason).toBe("watchdog_timeout");
    expect(kinds(ds)).toContain("DONE");
  });

  it("5. 全部跳过：无 PLAY、直接完成", () => {
    const ds = playToEnd(mkSpeech({ expectedSentences: 3, chunks: [], skippedSentences: [0, 1, 2] }));
    expect(playedIdx(ds)).toEqual([]);
    expect(skippedIdx(ds)).toEqual([0, 1, 2]);
    expect(kinds(ds)).toContain("DONE");
  });

  it("6. 中间段失败：播-跳-播", () => {
    const ds = playToEnd(mkSpeech({ expectedSentences: 3, chunks: chunks(0, 2), skippedSentences: [1] }));
    expect(playedIdx(ds)).toEqual([0, 2]);
    expect(skippedIdx(ds)).toEqual([1]);
  });

  it("7. 尾段失败：播-播-跳-完成", () => {
    const ds = playToEnd(mkSpeech({ expectedSentences: 3, chunks: chunks(0, 1), skippedSentences: [2] }));
    expect(playedIdx(ds)).toEqual([0, 1]);
    expect(skippedIdx(ds)).toEqual([2]);
    expect(kinds(ds)).toContain("DONE");
  });

  it("25. skip 列表含越界序号：被忽略，仍在 expected 处完成", () => {
    const ds = playToEnd(mkSpeech({ expectedSentences: 2, chunks: chunks(0, 1), skippedSentences: [5] }));
    expect(playedIdx(ds)).toEqual([0, 1]);
    expect(kinds(ds)).toContain("DONE");
  });
});

describe("reconcile — 媒体异常推进", () => {
  it("18. active 卡死看门狗：播放中但永不结束，超时后强制跳过", () => {
    const speech = mkSpeech({ expectedSentences: 2, chunks: chunks(0, 1) });
    // 先到 PLAY idx0
    let d = reconcile({ speech, position: emptyPosition(), nowMs: 1000, audioEnabled: true, suppressed: false, activeMediaState: "idle" });
    expect(d.kind).toBe("NOTIFY_START");
    d = reconcile({ speech, position: d.position, nowMs: 1000, audioEnabled: true, suppressed: false, activeMediaState: "idle" });
    expect(d.kind).toBe("PLAY");
    // 媒体一直 "playing" 但时间超过 activeStallWatchdogMs → 跳过 idx0
    const later = 1000 + DEFAULT_PLAYBACK_CONFIG.activeStallWatchdogMs + 1;
    d = reconcile({ speech, position: d.position, nowMs: later, audioEnabled: true, suppressed: false, activeMediaState: "playing" });
    expect(d.kind).toBe("SKIP");
    expect((d as any).sentenceIdx).toBe(0);
    expect((d as any).reason).toBe("watchdog_timeout");
  });

  it("19. 播放出错（errored）：跳过当前段并推进", () => {
    const speech = mkSpeech({ expectedSentences: 2, chunks: chunks(0, 1) });
    const pos: PlaybackPosition = { ...emptyPosition(), speechId: "S1", taskId: "T1", activeIdx: 0, activeStartedMs: 1000, startNotifiedKey: "S1:T1" };
    const d = reconcile({ speech, position: pos, nowMs: 1001, audioEnabled: true, suppressed: false, activeMediaState: "errored" });
    expect(d.kind).toBe("SKIP");
    expect((d as any).sentenceIdx).toBe(0);
    expect((d as any).reason).toBe("media_error");
    expect(d.position.nextIdx).toBe(1);
  });

  it("19b. 浏览器 stalled 事件只是缓冲信号，未超时不能立即跳过", () => {
    const speech = mkSpeech({ expectedSentences: 2, chunks: chunks(0, 1) });
    const pos: PlaybackPosition = { ...emptyPosition(), speechId: "S1", taskId: "T1", activeIdx: 0, activeStartedMs: 1000, startNotifiedKey: "S1:T1" };
    const d = reconcile({ speech, position: pos, nowMs: 1100, audioEnabled: true, suppressed: false, activeMediaState: "stalled" });
    expect(d.kind).toBe("IDLE");
    expect(d.position.activeIdx).toBe(0);
  });

  it("onended 正常结束：推进到下一段并直接 PLAY", () => {
    const speech = mkSpeech({ expectedSentences: 2, chunks: chunks(0, 1) });
    const pos: PlaybackPosition = { ...emptyPosition(), speechId: "S1", taskId: "T1", activeIdx: 0, activeStartedMs: 1000, startNotifiedKey: "S1:T1" };
    const d = reconcile({ speech, position: pos, nowMs: 1001, audioEnabled: true, suppressed: false, activeMediaState: "ended" });
    expect(d.kind).toBe("PLAY");
    expect((d as any).sentenceIdx).toBe(1);
  });
});

describe("reconcile — STOP 守卫（截断）", () => {
  const bound: PlaybackPosition = { ...emptyPosition(), speechId: "S1", taskId: "T1", nextIdx: 1 };
  const base = { nowMs: 1000, audioEnabled: true, suppressed: false, activeMediaState: "idle" as ActiveMediaState };

  it("8. current_speech 为空 → STOP(no_speech)，position 清空", () => {
    const d = reconcile({ ...base, speech: null, position: bound });
    expect(d.kind).toBe("STOP");
    expect((d as any).reason).toBe("no_speech");
    expect(d.position.speechId).toBeNull();
  });

  it("9. 已结束 → STOP(not_speaking)", () => {
    const d = reconcile({ ...base, speech: mkSpeech({ state: "ended" }), position: bound });
    expect((d as any).reason).toBe("not_speaking");
  });

  it("9b. 已暂停 → STOP(not_speaking)（截断音频）", () => {
    const d = reconcile({ ...base, speech: mkSpeech({ state: "paused" }), position: bound });
    expect((d as any).reason).toBe("not_speaking");
  });

  it("9c. thinking（AI 发言播放前状态）→ 必须能开播，不能截断", () => {
    const d = reconcile({ ...base, speech: mkSpeech({ state: "thinking", expectedSentences: 1, chunks: chunks(0) }), position: emptyPosition() });
    expect(["NOTIFY_START", "PLAY"]).toContain(d.kind);
    expect(d.kind).not.toBe("STOP");
  });

  it("10. 人声 ASR 来源 → STOP(wrong_source)", () => {
    const d = reconcile({ ...base, speech: mkSpeech({ source: "human_asr" }), position: bound });
    expect((d as any).reason).toBe("wrong_source");
  });

  it("10b. 固定兜底音频来源 fallback_history → 可以播放", () => {
    const d = reconcile({ ...base, speech: mkSpeech({ source: "fallback_history", expectedSentences: 1, chunks: chunks(0) }), position: emptyPosition() });
    expect(["NOTIFY_START", "PLAY"]).toContain(d.kind);
    expect(d.kind).not.toBe("STOP");
  });

  it("11. speech 变更 → STOP(speech_changed)，重置并绑定到新发言", () => {
    const d = reconcile({ ...base, speech: mkSpeech({ speechId: "S2", taskId: "T2" }), position: bound });
    expect((d as any).reason).toBe("speech_changed");
    expect(d.position.speechId).toBe("S2");
    expect(d.position.nextIdx).toBe(0);
  });

  it("12. task 变更（重新合成）→ STOP(task_changed)", () => {
    const d = reconcile({ ...base, speech: mkSpeech({ taskId: "T2" }), position: bound });
    expect((d as any).reason).toBe("task_changed");
    expect(d.position.taskId).toBe("T2");
  });

  it("13. suppressed → STOP(suppressed)", () => {
    const d = reconcile({ ...base, suppressed: true, speech: mkSpeech(), position: bound });
    expect((d as any).reason).toBe("suppressed");
  });

  it("14. audio 关闭 → STOP(audio_disabled)", () => {
    const d = reconcile({ ...base, audioEnabled: false, speech: mkSpeech(), position: bound });
    expect((d as any).reason).toBe("audio_disabled");
  });

  it("11b. thinking 全程播放：整段在 thinking 态也能播完（不依赖先翻 speaking）", () => {
    const ds = playToEnd(mkSpeech({ state: "thinking", expectedSentences: 2, chunks: chunks(0, 1) }));
    expect(playedIdx(ds)).toEqual([0, 1]);
    expect(kinds(ds)).toContain("DONE");
  });
});

describe("reconcile — 暂停/恢复与幂等", () => {
  it("15. suppress 后恢复：不回到第一段，只从被截断的当前段继续", () => {
    const speech = mkSpeech({ expectedSentences: 3, chunks: chunks(0, 1, 2) });
    const stop = reconcile({
      speech,
      position: { ...emptyPosition(), speechId: "S1", taskId: "T1", nextIdx: 1, activeIdx: 1, activeStartedMs: 1000, startNotifiedKey: "S1:T1" },
      nowMs: 1200,
      audioEnabled: true,
      suppressed: true,
      activeMediaState: "playing",
    });
    expect(stop.kind).toBe("STOP");
    expect(stop.position.nextIdx).toBe(1);
    expect(stop.position.activeIdx).toBeNull();
    const resumed = reconcile({ speech, position: stop.position, nowMs: 1001, audioEnabled: true, suppressed: false, activeMediaState: "idle" });
    expect(resumed.kind).toBe("PLAY");
    expect((resumed as any).sentenceIdx).toBe(1);
  });

  it("15b. 关闭音频后再打开：保留下一段位置，不从头播放", () => {
    const speech = mkSpeech({ expectedSentences: 3, chunks: chunks(0, 1, 2) });
    const stop = reconcile({
      speech,
      position: { ...emptyPosition(), speechId: "S1", taskId: "T1", nextIdx: 2, startNotifiedKey: "S1:T1" },
      nowMs: 1200,
      audioEnabled: false,
      suppressed: false,
      activeMediaState: "idle",
    });
    expect(stop.kind).toBe("STOP");
    expect(stop.position.nextIdx).toBe(2);
    const resumed = reconcile({ speech, position: stop.position, nowMs: 1300, audioEnabled: true, suppressed: false, activeMediaState: "idle" });
    expect(resumed.kind).toBe("PLAY");
    expect((resumed as any).sentenceIdx).toBe(2);
  });

  it("17. 完成幂等：DONE 后再次对账返回 IDLE，不重复通知", () => {
    const speech = mkSpeech({ expectedSentences: 1, chunks: chunks(0) });
    const ds = playToEnd(speech);
    const done = ds.find((d) => d.kind === "DONE")!;
    const again = reconcile({ speech, position: done.position, nowMs: 99999, audioEnabled: true, suppressed: false, activeMediaState: "idle" });
    expect(again.kind).toBe("IDLE");
  });

  it("21. WAIT 幂等：相同输入再次对账，决策与 position 不变", () => {
    const speech = mkSpeech({ expectedSentences: 2, chunks: chunks(0) }); // idx0 在播完后等 idx1
    const pos: PlaybackPosition = { ...emptyPosition(), speechId: "S1", taskId: "T1", nextIdx: 1, waitingSinceMs: 1000 };
    const a = reconcile({ speech, position: pos, nowMs: 1100, audioEnabled: true, suppressed: false, activeMediaState: "idle" });
    expect(a.kind).toBe("WAIT");
    const b = reconcile({ speech, position: a.position, nowMs: 1100, audioEnabled: true, suppressed: false, activeMediaState: "idle" });
    expect(b.kind).toBe("WAIT");
    expect(b.position).toEqual(a.position);
  });

  it("22. 正在播放时再次对账：IDLE，不重复 PLAY", () => {
    const speech = mkSpeech({ expectedSentences: 2, chunks: chunks(0, 1) });
    const pos: PlaybackPosition = { ...emptyPosition(), speechId: "S1", taskId: "T1", activeIdx: 0, activeStartedMs: 1000, startNotifiedKey: "S1:T1" };
    const d = reconcile({ speech, position: pos, nowMs: 1001, audioEnabled: true, suppressed: false, activeMediaState: "playing" });
    expect(d.kind).toBe("IDLE");
  });
});

describe("reconcile — 仍在生成（expected=null）", () => {
  it("20. expected=null：播完已有分段后耐心 WAIT，绝不 DONE、绝不越过", () => {
    const ds = playToEnd(mkSpeech({ expectedSentences: null, createdSentences: 2, chunks: chunks(0, 1) }), { maxSteps: 12 });
    expect(playedIdx(ds)).toEqual([0, 1]);
    expect(kinds(ds)).not.toContain("DONE");
    expect(kinds(ds)).not.toContain("SKIP"); // 不知道总数时不乱跳
    const lastWait = ds.filter((d) => d.kind === "WAIT").pop() as any;
    expect(lastWait.sentenceIdx).toBe(2);
  });

  it("20b. expected=null 但分段已创建：缺口超时后自动跳过，不等主持人强制跳过", () => {
    const ds = playToEnd(mkSpeech({ expectedSentences: null, createdSentences: 2, chunks: chunks(0) }), { maxSteps: 12 });
    expect(playedIdx(ds)).toEqual([0]);
    expect(skippedIdx(ds)).toEqual([1]);
    expect(kinds(ds)).not.toContain("DONE");
    const lastWait = ds.filter((d) => d.kind === "WAIT").pop() as any;
    expect(lastWait.sentenceIdx).toBe(2);
  });

  it("21. 刷新续播：resumeIdx>0 时从该段开始，而不是从 0 重头", () => {
    // 模拟刷新：前 3 句已播完、第 3 句正在播；归档 4 段都在。续播应从 idx=3 开始。
    const speech = mkSpeech({ expectedSentences: 4, chunks: chunks(0, 1, 2, 3), resumeIdx: 3 });
    const ds = playToEnd(speech, { maxSteps: 20 });
    expect(playedIdx(ds)).toEqual([3]); // 只播 3，不回放 0/1/2
    expect(kinds(ds)).toContain("DONE");
  });

  it("21b. resumeIdx 缺省（全新发言）仍从 0 开始，行为不变", () => {
    const ds = playToEnd(mkSpeech({ expectedSentences: 3, chunks: chunks(0, 1, 2) }), { maxSteps: 20 });
    expect(playedIdx(ds)).toEqual([0, 1, 2]);
    expect(kinds(ds)).toContain("DONE");
  });

  it("21c. resumeIdx 等于 expected：刷新时整段已播完 → 直接 DONE，不回放", () => {
    const speech = mkSpeech({ expectedSentences: 3, chunks: chunks(0, 1, 2), resumeIdx: 3 });
    const ds = playToEnd(speech, { maxSteps: 10 });
    expect(playedIdx(ds)).toEqual([]);
    expect(ds[0].kind).toBe("DONE");
  });
});
