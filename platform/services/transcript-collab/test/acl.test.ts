import assert from "node:assert/strict";
import test from "node:test";
import * as Y from "yjs";
import { assertAuthorizedUpdate, PermissionDenied } from "../src/acl.js";
import type { AuthContext } from "../src/auth.js";

function context(editable: string[], readOnly = false): AuthContext {
  return {
    room_id: "room_1",
    user_id: "user_1",
    role: readOnly ? "viewer" : "participant",
    editable_speech_ids: editable,
    exp: 9999999999,
    jti: "jti_1",
    readOnly,
  };
}

function updateFrom(base: Y.Doc, mutate: (doc: Y.Doc) => void): Uint8Array {
  const client = new Y.Doc();
  Y.applyUpdate(client, Y.encodeStateAsUpdate(base));
  const vector = Y.encodeStateVector(base);
  mutate(client);
  return Y.encodeStateAsUpdate(client, vector);
}

test("allows only token-scoped speech map changes", () => {
  const server = new Y.Doc();
  server.getMap("speeches").set("speech_1", "before");
  server.getMap("speeches").set("speech_2", "protected");
  const allowed = updateFrom(server, (doc) => doc.getMap("speeches").set("speech_1", "after"));
  assert.doesNotThrow(() => assertAuthorizedUpdate(server, allowed, context(["speech_1"]), 1024 * 1024));

  const denied = updateFrom(server, (doc) => doc.getMap("speeches").set("speech_2", "changed"));
  assert.throws(() => assertAuthorizedUpdate(server, denied, context(["speech_1"]), 1024 * 1024), PermissionDenied);
});

test("read-only clients and unrelated root changes fail closed", () => {
  const server = new Y.Doc();
  const speechUpdate = updateFrom(server, (doc) => doc.getMap("speeches").set("speech_1", "text"));
  assert.throws(() => assertAuthorizedUpdate(server, speechUpdate, context([], true), 1024 * 1024), PermissionDenied);
  const metadataUpdate = updateFrom(server, (doc) => doc.getMap("metadata").set("secret", "changed"));
  assert.throws(() => assertAuthorizedUpdate(server, metadataUpdate, context(["speech_1"]), 1024 * 1024), PermissionDenied);
});

test("scopes nested Y.Text edits to their speech map key", () => {
  const server = new Y.Doc();
  const first = new Y.Text("first");
  const second = new Y.Text("second");
  server.getMap("speeches").set("speech_1", first);
  server.getMap("speeches").set("speech_2", second);

  const allowed = updateFrom(server, (doc) => {
    const text = doc.getMap("speeches").get("speech_1") as Y.Text;
    text.insert(text.length, " allowed");
  });
  assert.doesNotThrow(() => assertAuthorizedUpdate(server, allowed, context(["speech_1"]), 1024 * 1024));

  const denied = updateFrom(server, (doc) => {
    const text = doc.getMap("speeches").get("speech_2") as Y.Text;
    text.insert(text.length, " denied");
  });
  assert.throws(() => assertAuthorizedUpdate(server, denied, context(["speech_1"]), 1024 * 1024), PermissionDenied);
});
