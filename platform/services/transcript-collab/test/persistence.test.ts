import assert from "node:assert/strict";
import test from "node:test";
import * as Y from "yjs";
import { MemoryDocumentRepository } from "../src/repository.js";

test("restores a persisted Yjs document", async () => {
  const repository = new MemoryDocumentRepository();
  const original = new Y.Doc();
  original.getMap("speeches").set("speech_1", "persisted");
  await repository.store("room:room_1", Y.encodeStateAsUpdate(original));
  const restored = new Y.Doc();
  const state = await repository.load("room:room_1");
  assert.ok(state);
  Y.applyUpdate(restored, state);
  assert.equal(restored.getMap("speeches").get("speech_1"), "persisted");
});

test("merges concurrent Yjs edits without a custom CRDT", async () => {
  const base = new Y.Doc();
  const first = new Y.Doc();
  const second = new Y.Doc();
  const initial = Y.encodeStateAsUpdate(base);
  Y.applyUpdate(first, initial);
  Y.applyUpdate(second, initial);
  first.getMap("speeches").set("speech_1", "affirmative edit");
  second.getMap("speeches").set("speech_2", "negative edit");
  const merged = new Y.Doc();
  Y.applyUpdate(merged, Y.encodeStateAsUpdate(first));
  Y.applyUpdate(merged, Y.encodeStateAsUpdate(second));
  assert.equal(merged.getMap("speeches").get("speech_1"), "affirmative edit");
  assert.equal(merged.getMap("speeches").get("speech_2"), "negative edit");
});
