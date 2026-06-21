import { describe, expect, it } from "vitest";
import type { MatchSnapshot } from "../types/contracts";
import { isCoalescableEvent, shouldIgnoreSnapshot } from "./useMatch";

function snap(matchId: string, lastSeq: number): MatchSnapshot {
  return { match: { id: matchId }, last_seq: lastSeq } as MatchSnapshot;
}

describe("useMatch snapshot ordering", () => {
  it("ignores any older snapshot for the same match", () => {
    expect(shouldIgnoreSnapshot(snap("match_1", 2000), snap("match_1", 1999))).toBe(true);
    expect(shouldIgnoreSnapshot(snap("match_1", 2000), snap("match_1", 25))).toBe(true);
  });

  it("accepts same-sequence/newer snapshots and snapshots for a different match", () => {
    expect(shouldIgnoreSnapshot(snap("match_1", 2000), snap("match_1", 2000))).toBe(false);
    expect(shouldIgnoreSnapshot(snap("match_1", 2000), snap("match_1", 2001))).toBe(false);
    expect(shouldIgnoreSnapshot(snap("match_1", 2000), snap("match_2", 1))).toBe(false);
    expect(shouldIgnoreSnapshot(null, snap("match_1", 1))).toBe(false);
  });
});

describe("useMatch refetch coalescing", () => {
  it("coalesces the high-frequency generation/playback progress events", () => {
    // 这些事件在 AI 生成/播放期间高频出现，刷新合并可把投影机的并发连接让给音频取流。
    for (const type of ["agent.speech.delta", "tts.sentence_ready", "tts.playback_progress"]) {
      expect(isCoalescableEvent(type)).toBe(true);
    }
  });

  it("never coalesces control/lifecycle/state-transition events (instant refresh)", () => {
    for (const type of [
      "tts.playback_stop_requested",
      "tts.playback_resume_requested",
      "tts.finished",
      "speech.started",
      "speech.ended",
      "agent.speech.final",
      "phase.changed",
    ]) {
      expect(isCoalescableEvent(type)).toBe(false);
    }
  });
});
