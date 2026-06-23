import { describe, expect, it } from "vitest";
import { Track } from "livekit-client";
import { shouldAutoSubscribe, shouldPlayOnScreen, shouldSubscribeToPublicationOnScreen } from "./useLiveKitAudio";

describe("useLiveKitAudio screen audio policy", () => {
  it("does not play human speaker microphone participants on the screen", () => {
    expect(shouldPlayOnScreen({ identity: "speaker-spk_aff_3", metadata: JSON.stringify({ role: "speaker" }) })).toBe(false);
    expect(shouldPlayOnScreen({ identity: "speaker-spk_neg_1" })).toBe(false);
  });

  it("plays voice-agent audio participants on the screen", () => {
    expect(shouldPlayOnScreen({ identity: "voice-agent-a1b2c3" })).toBe(true);
    expect(shouldPlayOnScreen({ identity: "custom-worker", metadata: JSON.stringify({ role: "voice-agent" }) })).toBe(true);
    expect(shouldPlayOnScreen({ identity: "custom-agent", metadata: JSON.stringify({ role: "agent" }) })).toBe(true);
  });

  it("ignores malformed metadata unless the identity is the voice agent", () => {
    expect(shouldPlayOnScreen({ identity: "speaker-spk_aff_1", metadata: "not-json" })).toBe(false);
    expect(shouldPlayOnScreen({ identity: "voice-agent", metadata: "not-json" })).toBe(true);
  });

  it("subscribes the screen only to voice-agent audio publications", () => {
    const audio = { kind: Track.Kind.Audio };
    const video = { kind: Track.Kind.Video };

    expect(shouldSubscribeToPublicationOnScreen({ identity: "speaker-spk_aff_3" }, audio)).toBe(false);
    expect(shouldSubscribeToPublicationOnScreen({ identity: "voice-agent-a1b2c3" }, audio)).toBe(true);
    expect(shouldSubscribeToPublicationOnScreen({ identity: "voice-agent-a1b2c3" }, video)).toBe(false);
  });

  it("does not auto-subscribe screen or speaker rooms", () => {
    expect(shouldAutoSubscribe("screen")).toBe(false);
    expect(shouldAutoSubscribe("speaker")).toBe(false);
  });
});
