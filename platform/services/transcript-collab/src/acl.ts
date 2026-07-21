import * as Y from "yjs";
import type { AuthContext } from "./auth.js";

export class PermissionDenied extends Error {}

function stable(value: unknown): string {
  if (value === undefined) return "undefined";
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  return `{${Object.entries(value as Record<string, unknown>)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, item]) => `${JSON.stringify(key)}:${stable(item)}`)
    .join(",")}}`;
}

function clone(document: Y.Doc): Y.Doc {
  const result = new Y.Doc({ gc: false });
  Y.applyUpdate(result, Y.encodeStateAsUpdate(document));
  return result;
}

function roots(document: Y.Doc): Map<string, string> {
  const result = new Map<string, string>();
  for (const [name, type] of document.share.entries()) {
    result.set(name, stable(type.toJSON()));
  }
  return result;
}

function hasPending(document: Y.Doc): boolean {
  const store = document.store as unknown as { pendingStructs?: unknown; pendingDs?: unknown };
  return Boolean(store.pendingStructs || store.pendingDs);
}

export function assertAuthorizedUpdate(
  document: Y.Doc,
  update: Uint8Array,
  context: AuthContext,
  maxDocumentBytes: number,
): void {
  if (update.byteLength === 0) return;
  if (context.readOnly) throw new PermissionDenied("read-only connection cannot update documents");

  const before = clone(document);
  const after = clone(document);
  const afterSpeechesType = after.getMap("speeches");
  const touchedSpeechIds = new Set<string>();
  let touchedOtherRoot = false;
  afterSpeechesType.observeDeep((events) => {
    for (const event of events) {
      if (event.target === afterSpeechesType) {
        for (const key of event.changes.keys.keys()) touchedSpeechIds.add(key);
      } else if (typeof event.path[0] === "string") {
        touchedSpeechIds.add(event.path[0]);
      } else {
        touchedOtherRoot = true;
      }
    }
  });
  after.on("afterTransaction", (transaction) => {
    for (const changedType of transaction.changedParentTypes.keys()) {
      let root = changedType;
      while (root.parent) root = root.parent;
      const rootName = [...after.share.entries()].find(([, type]) => type === root)?.[0];
      if (rootName && rootName !== "speeches") touchedOtherRoot = true;
    }
  });
  try {
    Y.applyUpdate(after, update);
  } catch {
    throw new PermissionDenied("invalid Yjs update");
  }
  if (hasPending(after)) throw new PermissionDenied("updates with unresolved dependencies are not accepted");
  if (Y.encodeStateAsUpdate(after).byteLength > maxDocumentBytes) throw new PermissionDenied("document size limit exceeded");
  if (touchedOtherRoot) throw new PermissionDenied("only the speeches map may be changed");

  const beforeRoots = roots(before);
  const afterRoots = roots(after);
  const rootNames = new Set([...beforeRoots.keys(), ...afterRoots.keys()]);
  for (const name of rootNames) {
    if (name !== "speeches" && beforeRoots.get(name) !== afterRoots.get(name)) {
      throw new PermissionDenied("only the speeches map may be changed");
    }
  }

  const beforeSpeeches = before.getMap("speeches").toJSON() as Record<string, unknown>;
  const afterSpeeches = after.getMap("speeches").toJSON() as Record<string, unknown>;
  const speechIds = new Set([...Object.keys(beforeSpeeches), ...Object.keys(afterSpeeches)]);
  const editable = new Set(context.editable_speech_ids);
  for (const speechId of touchedSpeechIds) {
    if (!editable.has(speechId)) throw new PermissionDenied("update touches a speech outside the token scope");
  }
  for (const speechId of speechIds) {
    if (stable(beforeSpeeches[speechId]) !== stable(afterSpeeches[speechId]) && !editable.has(speechId)) {
      throw new PermissionDenied("update touches a speech outside the token scope");
    }
  }
}
