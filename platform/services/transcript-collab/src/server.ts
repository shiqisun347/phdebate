import { Server } from "@hocuspocus/server";
import * as Y from "yjs";
import { assertAuthorizedUpdate } from "./acl.js";
import type { AuthContext } from "./auth.js";
import { verifyToken } from "./auth.js";
import type { Config } from "./config.js";
import { log } from "./logger.js";
import type { DocumentRepository } from "./repository.js";

export function createCollabServer(config: Config, repository: DocumentRepository): Server<AuthContext> {
  return new Server<AuthContext>({
    name: "phdebate-transcript-collab",
    address: config.host,
    port: config.port,
    quiet: true,
    stopOnSignals: false,
    timeout: config.authTimeoutMs,
    debounce: config.storeDebounceMs,
    maxDebounce: config.storeMaxDebounceMs,
    maxUnauthenticatedQueueSize: config.maxUnauthenticatedQueueBytes,
    maxUnauthenticatedQueueMessages: config.maxUnauthenticatedQueueMessages,
    maxPendingDocuments: config.maxPendingDocuments,
    websocketOptions: { maxPayload: config.maxMessageBytes },

    async onAuthenticate({ token, documentName, connectionConfig }) {
      const context = verifyToken(token, config.hmacSecret, documentName, {
        maxTtlSeconds: config.maxTokenTtlSeconds,
      });
      connectionConfig.readOnly = context.readOnly;
      log("info", "collab_authenticated", {
        room_id: context.room_id,
        user_id: context.user_id,
        role: context.role,
        jti: context.jti,
        read_only: context.readOnly,
      });
      return context;
    },

    async connected({ connection, context }) {
      // A token authenticates a connection, not an unlimited browser session.
      // Closing at expiry makes the provider reconnect and fetch a fresh token
      // from the platform API, while also bounding stale seat/admin privileges.
      const delay = Math.max(0, context.exp * 1000 - Date.now());
      const expiryTimer = setTimeout(() => {
        connection.close({ code: 4001, reason: "authorization expired" });
      }, delay);
      expiryTimer.unref();
      connection.onClose(() => clearTimeout(expiryTimer));
    },

    async beforeHandleMessage({ update }) {
      if (update.byteLength > config.maxMessageBytes) throw new Error("message size limit exceeded");
    },

    async beforeSync({ type, payload, document, context }) {
      // Hocuspocus v4.4 exposes the decoded y-sync payload here. Type 0 is a
      // state-vector request and cannot mutate the document; types 1 and 2
      // carry Yjs updates and must pass field-level authorization.
      if (type === 0) return;
      if (type !== 1 && type !== 2) throw new Error("unsupported sync message type");
      if (context.exp <= Math.floor(Date.now() / 1000)) throw new Error("authorization expired");
      if (payload.byteLength > config.maxMessageBytes) throw new Error("message size limit exceeded");
      assertAuthorizedUpdate(document, payload, context, config.maxDocumentBytes);
    },

    async onLoadDocument({ documentName, document }) {
      const state = await repository.load(documentName);
      if (state) Y.applyUpdate(document, state);
      if (Y.encodeStateAsUpdate(document).byteLength > config.maxDocumentBytes) {
        throw new Error("stored document exceeds size limit");
      }
    },

    async onStoreDocument({ documentName, document }) {
      const state = Y.encodeStateAsUpdate(document);
      if (state.byteLength > config.maxDocumentBytes) throw new Error("document size limit exceeded");
      await repository.store(documentName, state);
    },

    async onRequest({ request, response }) {
      const path = new URL(request.url ?? "/", "http://localhost").pathname;
      if (path === "/health") {
        response.writeHead(200, { "content-type": "application/json", "cache-control": "no-store" });
        response.end(JSON.stringify({ ok: true, service: "transcript-collab" }));
        throw null;
      }
      if (path === "/ready") {
        const ready = await repository.ready();
        response.writeHead(ready ? 200 : 503, { "content-type": "application/json", "cache-control": "no-store" });
        response.end(JSON.stringify({ ok: ready, service: "transcript-collab" }));
        throw null;
      }
      response.writeHead(404, { "content-type": "application/json", "cache-control": "no-store" });
      response.end(JSON.stringify({ detail: "not found" }));
      throw null;
    },
  });
}
