import { createHmac, timingSafeEqual } from "node:crypto";

export const ROLES = ["admin", "editor", "participant", "viewer", "anonymous"] as const;
export type Role = (typeof ROLES)[number];

export interface TokenClaims {
  room_id: string;
  user_id: string;
  role: Role;
  editable_speech_ids: string[];
  exp: number;
  jti: string;
}

export interface AuthContext extends TokenClaims {
  readOnly: boolean;
}

const ROOM_ID = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/;
const ENTITY_ID = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$/;

function encode(value: string | Buffer): string {
  return Buffer.from(value).toString("base64url");
}

function signature(payload: string, secret: string): Buffer {
  return createHmac("sha256", secret).update(payload).digest();
}

export function documentNameForRoom(roomId: string): string {
  if (!ROOM_ID.test(roomId)) throw new Error("invalid room_id");
  return `room:${roomId}`;
}

export function signToken(claims: TokenClaims, secret: string): string {
  const payload = encode(JSON.stringify(claims));
  return `${payload}.${encode(signature(payload, secret))}`;
}

function claimsFrom(value: unknown): TokenClaims {
  if (!value || typeof value !== "object") throw new Error("invalid token claims");
  const input = value as Record<string, unknown>;
  if (typeof input.room_id !== "string" || !ROOM_ID.test(input.room_id)) throw new Error("invalid room_id");
  if (typeof input.user_id !== "string" || !ENTITY_ID.test(input.user_id)) throw new Error("invalid user_id");
  if (typeof input.role !== "string" || !(ROLES as readonly string[]).includes(input.role)) throw new Error("invalid role");
  if (!Array.isArray(input.editable_speech_ids) || input.editable_speech_ids.length > 64) {
    throw new Error("invalid editable_speech_ids");
  }
  const editable = input.editable_speech_ids.map((item) => {
    if (typeof item !== "string" || !ENTITY_ID.test(item)) throw new Error("invalid speech id");
    return item;
  });
  if (new Set(editable).size !== editable.length) throw new Error("duplicate speech id");
  if (typeof input.exp !== "number" || !Number.isSafeInteger(input.exp)) throw new Error("invalid exp");
  if (typeof input.jti !== "string" || !ENTITY_ID.test(input.jti)) throw new Error("invalid jti");
  return {
    room_id: input.room_id,
    user_id: input.user_id,
    role: input.role as Role,
    editable_speech_ids: editable,
    exp: input.exp,
    jti: input.jti,
  };
}

export function verifyToken(
  token: string,
  secret: string,
  documentName: string,
  options: { nowSeconds?: number; maxTtlSeconds: number },
): AuthContext {
  const parts = token.split(".");
  if (parts.length !== 2 || !parts[0] || !parts[1]) throw new Error("invalid token");
  const expected = signature(parts[0], secret);
  const supplied = Buffer.from(parts[1], "base64url");
  if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) throw new Error("invalid token");
  let decoded: unknown;
  try {
    decoded = JSON.parse(Buffer.from(parts[0], "base64url").toString("utf8"));
  } catch {
    throw new Error("invalid token");
  }
  const claims = claimsFrom(decoded);
  const now = options.nowSeconds ?? Math.floor(Date.now() / 1000);
  if (claims.exp <= now) throw new Error("token expired");
  if (claims.exp - now > options.maxTtlSeconds) throw new Error("token lifetime too long");
  if (documentName !== documentNameForRoom(claims.room_id)) throw new Error("document does not match room");
  const readOnly = !["admin", "editor", "participant"].includes(claims.role) || claims.editable_speech_ids.length === 0;
  return { ...claims, readOnly };
}
