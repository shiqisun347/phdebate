const SECRET_KEYS = new Set(["token", "authorization", "cookie", "secret", "state", "update", "content"]);

function sanitize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sanitize);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>).map(([key, item]) => [
      key,
      SECRET_KEYS.has(key.toLowerCase()) ? "[REDACTED]" : sanitize(item),
    ]),
  );
}

export function log(level: "info" | "warn" | "error", event: string, fields: Record<string, unknown> = {}): void {
  const safeFields = sanitize(fields) as Record<string, unknown>;
  const output = JSON.stringify({ timestamp: new Date().toISOString(), level, event, ...safeFields });
  (level === "error" ? console.error : level === "warn" ? console.warn : console.log)(output);
}
