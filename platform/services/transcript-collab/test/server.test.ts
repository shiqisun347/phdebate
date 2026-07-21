import assert from "node:assert/strict";
import test from "node:test";
import type { Config } from "../src/config.js";
import { MemoryDocumentRepository } from "../src/repository.js";
import { createCollabServer } from "../src/server.js";

const config: Config = {
  host: "127.0.0.1",
  port: 0,
  hmacSecret: "test-secret-that-is-at-least-thirty-two-characters",
  maxTokenTtlSeconds: 300,
  maxDocumentBytes: 1024 * 1024,
  maxMessageBytes: 64 * 1024,
  maxUnauthenticatedQueueBytes: 32 * 1024,
  maxUnauthenticatedQueueMessages: 8,
  maxPendingDocuments: 2,
  authTimeoutMs: 1000,
  storeDebounceMs: 50,
  storeMaxDebounceMs: 100,
};

test("serves health and repository-backed readiness", async () => {
  const server = createCollabServer(config, new MemoryDocumentRepository());
  await server.listen();
  try {
    const origin = `http://127.0.0.1:${server.address.port}`;
    const health = await fetch(`${origin}/health`);
    const ready = await fetch(`${origin}/ready`);
    const missing = await fetch(`${origin}/missing`);
    assert.equal(health.status, 200);
    assert.deepEqual(await health.json(), { ok: true, service: "transcript-collab" });
    assert.equal(ready.status, 200);
    assert.equal(missing.status, 404);
  } finally {
    await server.destroy();
  }
});
