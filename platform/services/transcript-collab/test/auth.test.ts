import assert from "node:assert/strict";
import test from "node:test";
import { documentNameForRoom, signToken, verifyToken, type TokenClaims } from "../src/auth.js";

const secret = "test-secret-that-is-at-least-thirty-two-characters";
const claims: TokenClaims = {
  room_id: "room_123",
  user_id: "user_1",
  role: "participant",
  editable_speech_ids: ["speech_1"],
  exp: 1_000,
  jti: "jti_1",
};

test("validates a scoped short-lived token", () => {
  const context = verifyToken(signToken(claims, secret), secret, documentNameForRoom(claims.room_id), {
    nowSeconds: 900,
    maxTtlSeconds: 300,
  });
  assert.equal(context.room_id, "room_123");
  assert.equal(context.readOnly, false);
});

test("rejects expired and cross-room tokens", () => {
  const token = signToken(claims, secret);
  assert.throws(() => verifyToken(token, secret, "room:room_123", { nowSeconds: 1_000, maxTtlSeconds: 300 }), /expired/);
  assert.throws(() => verifyToken(token, secret, "room:other", { nowSeconds: 900, maxTtlSeconds: 300 }), /match room/);
});

test("viewer and anonymous roles are read-only", () => {
  for (const role of ["viewer", "anonymous"] as const) {
    const token = signToken({ ...claims, role }, secret);
    const context = verifyToken(token, secret, "room:room_123", { nowSeconds: 900, maxTtlSeconds: 300 });
    assert.equal(context.readOnly, true);
  }
});
