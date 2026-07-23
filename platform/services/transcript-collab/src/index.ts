import { loadConfig } from "./config.js";
import { log } from "./logger.js";
import { MemoryDocumentRepository, PostgresDocumentRepository } from "./repository.js";
import { createCollabServer } from "./server.js";

const config = loadConfig();
const repository = config.databaseUrl
  ? new PostgresDocumentRepository(config.databaseUrl)
  : new MemoryDocumentRepository();
const server = createCollabServer(config, repository);

let shuttingDown = false;
async function shutdown(signal: string): Promise<void> {
  if (shuttingDown) return;
  shuttingDown = true;
  log("info", "collab_shutdown_started", { signal });
  const hardStop = setTimeout(() => process.exit(1), 10_000).unref();
  try {
    await server.destroy();
    await repository.close();
    clearTimeout(hardStop);
    log("info", "collab_shutdown_complete");
    process.exit(0);
  } catch (error) {
    log("error", "collab_shutdown_failed", { error: error instanceof Error ? error.message : "unknown" });
    process.exit(1);
  }
}

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => void shutdown(signal));
}

process.on("uncaughtException", (error) => {
  log("error", "collab_uncaught_exception", { error: error.message });
  void shutdown("uncaughtException");
});
process.on("unhandledRejection", (error) => {
  log("error", "collab_unhandled_rejection", { error: error instanceof Error ? error.message : "unknown" });
  void shutdown("unhandledRejection");
});

await server.listen();
log("info", "collab_started", { host: config.host, port: server.address.port, persistence: config.databaseUrl ? "postgres" : "memory" });
