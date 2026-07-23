import assert from "node:assert/strict";
import test from "node:test";
import { HocuspocusProvider, type HocuspocusProviderConfiguration } from "@hocuspocus/provider";
import WebSocket from "ws";
import * as Y from "yjs";

import { signToken } from "../src/auth.js";
import type { Config } from "../src/config.js";
import { MemoryDocumentRepository } from "../src/repository.js";
import { createCollabServer } from "../src/server.js";

async function eventually(predicate: () => boolean, timeout = 4000): Promise<void> {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error("timed out waiting for collaborative state");
}

type ProviderToken = string | (() => string) | (() => Promise<string>);

function connect(url: string, name: string, token: ProviderToken, doc: Y.Doc, providers: HocuspocusProvider[]) {
  return new Promise<HocuspocusProvider>((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("provider sync timeout")), 4000);
    const provider = new HocuspocusProvider({
      url,
      name,
      token,
      document: doc,
      WebSocketPolyfill: WebSocket,
      onSynced: ({ state }) => {
        if (!state) return;
        clearTimeout(timeout);
        resolve(provider);
      },
      onAuthenticationFailed: ({ reason }) => {
        clearTimeout(timeout);
        reject(new Error(reason));
      },
    } as HocuspocusProviderConfiguration);
    providers.push(provider);
  });
}

function testConfig(secret: string): Config {
  return {
    host: "127.0.0.1", port: 0, hmacSecret: secret, maxTokenTtlSeconds: 300,
    maxDocumentBytes: 1024 * 1024, maxMessageBytes: 64 * 1024,
    maxUnauthenticatedQueueBytes: 32 * 1024, maxUnauthenticatedQueueMessages: 8,
    maxPendingDocuments: 4, authTimeoutMs: 1000, storeDebounceMs: 50, storeMaxDebounceMs: 100,
  };
}

test("real providers sync while the server rejects a viewer write", { timeout: 15_000 }, async () => {
  const secret = "provider-integration-secret-at-least-32-bytes";
  const config = testConfig(secret);
  const server = createCollabServer(config, new MemoryDocumentRepository());
  const providers: HocuspocusProvider[] = [];
  await server.listen();
  const roomId = "integration-room";
  const documentName = `room:${roomId}`;
  const exp = Math.floor(Date.now() / 1000) + 120;
  const writerToken = signToken({ room_id: roomId, user_id: "writer", role: "participant", editable_speech_ids: ["speech-1"], exp, jti: "writer-jti" }, secret);
  const viewerToken = signToken({ room_id: roomId, user_id: "viewer", role: "viewer", editable_speech_ids: [], exp, jti: "viewer-jti" }, secret);
  const url = `ws://127.0.0.1:${server.address.port}`;
  try {
    const firstDoc = new Y.Doc();
    await connect(url, documentName, writerToken, firstDoc, providers);
    firstDoc.getMap("speeches").set("speech-1", new Y.Text("权威文字"));
    const secondDoc = new Y.Doc();
    await connect(url, documentName, writerToken, secondDoc, providers);
    await eventually(() => (secondDoc.getMap("speeches").get("speech-1") as Y.Text | undefined)?.toString() === "权威文字");
    (firstDoc.getMap("speeches").get("speech-1") as Y.Text).insert(4, "·甲");
    (secondDoc.getMap("speeches").get("speech-1") as Y.Text).insert(4, "·乙");
    await eventually(() => {
      const first = (firstDoc.getMap("speeches").get("speech-1") as Y.Text).toString();
      const second = (secondDoc.getMap("speeches").get("speech-1") as Y.Text).toString();
      return first === second && first.includes("甲") && first.includes("乙");
    });

    const viewerDoc = new Y.Doc();
    await connect(url, documentName, viewerToken, viewerDoc, providers);
    await eventually(() => Boolean(viewerDoc.getMap("speeches").get("speech-1")));
    (viewerDoc.getMap("speeches").get("speech-1") as Y.Text).insert(0, "越权");
    await new Promise((resolve) => setTimeout(resolve, 150));
    const observerDoc = new Y.Doc();
    await connect(url, documentName, writerToken, observerDoc, providers);
    await eventually(() => Boolean(observerDoc.getMap("speeches").get("speech-1")));
    assert.doesNotMatch((observerDoc.getMap("speeches").get("speech-1") as Y.Text).toString(), /越权/);
  } finally {
    providers.forEach((provider) => provider.destroy());
    await server.destroy();
  }
});

test("real providers reject an out-of-scope speech and isolate room documents", { timeout: 15_000 }, async () => {
  const secret = "provider-scope-secret-at-least-32-bytes";
  const repository = new MemoryDocumentRepository();
  const server = createCollabServer(testConfig(secret), repository);
  const providers: HocuspocusProvider[] = [];
  await server.listen();
  const exp = Math.floor(Date.now() / 1000) + 120;
  const token = (roomId: string, editable: string[], jti: string) => signToken({
    room_id: roomId, user_id: "writer", role: "participant", editable_speech_ids: editable, exp, jti,
  }, secret);
  const url = `ws://127.0.0.1:${server.address.port}`;
  try {
    const roomOne = new Y.Doc();
    await connect(url, "room:room-one", token("room-one", ["speech-1"], "room-one-writer"), roomOne, providers);
    roomOne.getMap("speeches").set("speech-1", new Y.Text("房间一文字"));
    await eventually(() => Boolean(roomOne.getMap("speeches").get("speech-1")));

    // A writer cannot smuggle another speech into the same room document.
    roomOne.getMap("speeches").set("speech-2", new Y.Text("越权文字"));
    await new Promise((resolve) => setTimeout(resolve, 150));
    const roomOneObserver = new Y.Doc();
    await connect(url, "room:room-one", token("room-one", ["speech-1"], "room-one-observer"), roomOneObserver, providers);
    await eventually(() => Boolean(roomOneObserver.getMap("speeches").get("speech-1")));
    assert.equal(roomOneObserver.getMap("speeches").has("speech-2"), false);

    // Identical map keys in another room remain a completely separate Y.Doc.
    const roomTwo = new Y.Doc();
    await connect(url, "room:room-two", token("room-two", ["speech-1"], "room-two-writer"), roomTwo, providers);
    assert.equal(roomTwo.getMap("speeches").has("speech-1"), false);
    roomTwo.getMap("speeches").set("speech-1", new Y.Text("房间二文字"));
    await eventually(() => (roomTwo.getMap("speeches").get("speech-1") as Y.Text | undefined)?.toString() === "房间二文字");
    assert.equal((roomOneObserver.getMap("speeches").get("speech-1") as Y.Text).toString(), "房间一文字");
  } finally {
    providers.forEach((provider) => provider.destroy());
    await server.destroy();
  }
});

test("an expired live session reconnects with a freshly minted token", { timeout: 15_000 }, async () => {
  const secret = "provider-expiry-secret-at-least-32-bytes";
  const server = createCollabServer(testConfig(secret), new MemoryDocumentRepository());
  const providers: HocuspocusProvider[] = [];
  await server.listen();
  const roomId = "expiry-room";
  let tokenCalls = 0;
  const freshToken = () => {
    tokenCalls += 1;
    return signToken({
      room_id: roomId,
      user_id: "writer",
      role: "participant",
      editable_speech_ids: ["speech-1"],
      exp: Math.floor(Date.now() / 1000) + 2,
      jti: `expiry-${tokenCalls}`,
    }, secret);
  };
  const url = `ws://127.0.0.1:${server.address.port}`;
  try {
    const doc = new Y.Doc();
    await connect(url, `room:${roomId}`, freshToken, doc, providers);
    doc.getMap("speeches").set("speech-1", new Y.Text("到期前"));
    await eventually(() => tokenCalls >= 2, 8000);
    await eventually(() => providers[0]?.isSynced === true, 8000);
    (doc.getMap("speeches").get("speech-1") as Y.Text).insert(3, "与重连后");

    const observer = new Y.Doc();
    await connect(url, `room:${roomId}`, freshToken(), observer, providers);
    await eventually(() => (observer.getMap("speeches").get("speech-1") as Y.Text | undefined)?.toString().includes("重连后") === true);
  } finally {
    providers.forEach((provider) => provider.destroy());
    await server.destroy();
  }
});
