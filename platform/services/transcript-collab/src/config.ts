export interface Config {
  host: string;
  port: number;
  hmacSecret: string;
  databaseUrl?: string;
  maxTokenTtlSeconds: number;
  maxDocumentBytes: number;
  maxMessageBytes: number;
  maxUnauthenticatedQueueBytes: number;
  maxUnauthenticatedQueueMessages: number;
  maxPendingDocuments: number;
  authTimeoutMs: number;
  storeDebounceMs: number;
  storeMaxDebounceMs: number;
}

function integer(name: string, fallback: number, minimum: number, maximum: number): number {
  const parsed = Number.parseInt(process.env[name] ?? String(fallback), 10);
  if (!Number.isFinite(parsed) || parsed < minimum || parsed > maximum) {
    throw new Error(`${name} must be between ${minimum} and ${maximum}`);
  }
  return parsed;
}

export function loadConfig(): Config {
  const hmacSecret = process.env.COLLAB_HMAC_SECRET ?? "";
  const databaseUrl = process.env.COLLAB_DATABASE_URL ?? process.env.DATABASE_URL;
  if (hmacSecret.length < 32) throw new Error("COLLAB_HMAC_SECRET must contain at least 32 characters");
  if (process.env.NODE_ENV === "production" && !databaseUrl) {
    throw new Error("COLLAB_DATABASE_URL is required in production");
  }
  if (databaseUrl) {
    let protocol = "";
    try {
      protocol = new URL(databaseUrl).protocol;
    } catch {
      throw new Error("COLLAB_DATABASE_URL must be a valid PostgreSQL URL");
    }
    if (protocol !== "postgres:" && protocol !== "postgresql:") {
      throw new Error("COLLAB_DATABASE_URL must use postgres:// or postgresql://");
    }
  }
  return {
    host: process.env.COLLAB_HOST ?? "127.0.0.1",
    port: integer("COLLAB_PORT", 8400, 1, 65535),
    hmacSecret,
    ...(databaseUrl ? { databaseUrl } : {}),
    maxTokenTtlSeconds: integer("COLLAB_MAX_TOKEN_TTL_SECONDS", 300, 30, 900),
    maxDocumentBytes: integer("COLLAB_MAX_DOCUMENT_BYTES", 2 * 1024 * 1024, 64 * 1024, 16 * 1024 * 1024),
    maxMessageBytes: integer("COLLAB_MAX_MESSAGE_BYTES", 128 * 1024, 4096, 1024 * 1024),
    maxUnauthenticatedQueueBytes: integer("COLLAB_MAX_UNAUTH_QUEUE_BYTES", 256 * 1024, 4096, 2 * 1024 * 1024),
    maxUnauthenticatedQueueMessages: integer("COLLAB_MAX_UNAUTH_QUEUE_MESSAGES", 64, 1, 256),
    maxPendingDocuments: integer("COLLAB_MAX_PENDING_DOCUMENTS", 4, 1, 16),
    authTimeoutMs: integer("COLLAB_AUTH_TIMEOUT_MS", 10_000, 1000, 60_000),
    storeDebounceMs: integer("COLLAB_STORE_DEBOUNCE_MS", 500, 50, 5000),
    storeMaxDebounceMs: integer("COLLAB_STORE_MAX_DEBOUNCE_MS", 2000, 250, 10_000),
  };
}
