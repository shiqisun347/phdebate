import assert from "node:assert/strict";
import test from "node:test";

import { loadConfig } from "../src/config.js";

const SECRET = "config-test-secret-at-least-thirty-two-bytes";

function withEnvironment(values: Record<string, string | undefined>, callback: () => void) {
  const original = Object.fromEntries(Object.keys(values).map((key) => [key, process.env[key]]));
  try {
    for (const [key, value] of Object.entries(values)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    callback();
  } finally {
    for (const [key, value] of Object.entries(original)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

test("production fails closed instead of silently using volatile memory", () => {
  withEnvironment({
    NODE_ENV: "production",
    COLLAB_HMAC_SECRET: SECRET,
    COLLAB_DATABASE_URL: undefined,
    DATABASE_URL: undefined,
  }, () => assert.throws(() => loadConfig(), /required in production/));
});

test("production accepts only a PostgreSQL persistence URL", () => {
  withEnvironment({
    NODE_ENV: "production",
    COLLAB_HMAC_SECRET: SECRET,
    COLLAB_DATABASE_URL: "sqlite:///temporary.db",
    DATABASE_URL: undefined,
  }, () => assert.throws(() => loadConfig(), /PostgreSQL URL|postgres/));

  withEnvironment({
    NODE_ENV: "production",
    COLLAB_HMAC_SECRET: SECRET,
    COLLAB_DATABASE_URL: "postgresql://user:pass@127.0.0.1:5433/phdebate",
    DATABASE_URL: undefined,
  }, () => assert.equal(loadConfig().databaseUrl, "postgresql://user:pass@127.0.0.1:5433/phdebate"));
});
