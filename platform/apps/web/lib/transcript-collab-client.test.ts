import { describe, expect, it } from "vitest";
import * as Y from "yjs";

import { initializeSpeechTexts, replaceSpeechText, speechTextSnapshot } from "@/lib/transcript-collab-client";

const speeches = [
  { id: "speech-a", content: "甲" },
  { id: "speech-b", content: "乙" },
];

describe("transcript collab Yjs model", () => {
  it("atomically initializes authoritative Y.Text values and merges two client updates", () => {
    const first = new Y.Doc();
    initializeSpeechTexts(first, speeches, ["speech-a", "speech-b"]);
    const second = new Y.Doc();
    Y.applyUpdate(second, Y.encodeStateAsUpdate(first));

    (first.getMap("speeches").get("speech-a") as Y.Text).insert(1, "·客户端甲");
    (second.getMap("speeches").get("speech-a") as Y.Text).insert(1, "·客户端乙");
    const firstUpdate = Y.encodeStateAsUpdate(first, Y.encodeStateVector(second));
    const secondUpdate = Y.encodeStateAsUpdate(second, Y.encodeStateVector(first));
    Y.applyUpdate(first, secondUpdate);
    Y.applyUpdate(second, firstUpdate);

    const firstText = speechTextSnapshot(first, speeches)["speech-a"];
    expect(speechTextSnapshot(second, speeches)["speech-a"]).toBe(firstText);
    expect(firstText).toContain("客户端甲");
    expect(firstText).toContain("客户端乙");
  });

  it("keeps speech keys isolated and refuses edits outside the token scope", () => {
    const doc = new Y.Doc();
    initializeSpeechTexts(doc, speeches, ["speech-a", "speech-b"]);
    replaceSpeechText(doc, "speech-a", "只修改甲", ["speech-a"]);
    expect(speechTextSnapshot(doc, speeches)).toEqual({ "speech-a": "只修改甲", "speech-b": "乙" });
    expect(() => replaceSpeechText(doc, "speech-b", "越权修改", ["speech-a"])).toThrow("不在当前协同编辑权限内");
  });

  it("does not overwrite an existing collaborative value during authoritative initialization", () => {
    const doc = new Y.Doc();
    const existing = new Y.Text("已有协同草稿");
    doc.getMap("speeches").set("speech-a", existing);
    initializeSpeechTexts(doc, speeches, ["speech-a"]);
    expect(speechTextSnapshot(doc, speeches)["speech-a"]).toBe("已有协同草稿");
  });
});
