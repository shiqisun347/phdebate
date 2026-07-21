# Transcript Collaboration Service

Independent Hocuspocus + Yjs service skeleton for collaborative transcript correction. It does not own platform authentication or database migrations. The main platform must mint short-lived HMAC tokens and later provide the `collab_documents` table.

Document schema:

- document name: `room:<room_id>`
- editable root: `Y.Map("speeches")`
- map key: immutable speech id
- map value: client-managed Yjs value or JSON-compatible transcript value

On the first editable connection, the client initializes an absent speech by
atomically setting `speeches.set(speechId, new Y.Text(initialText))`. A client
may use the non-sensitive `role` and `editable_speech_ids` returned beside the
token to decide which editors to render, but the Hocuspocus `beforeSync` ACL is
always authoritative.

All updates are checked before application by cloning the current Y.Doc and applying the candidate update. Changes outside `speeches`, changes to speech ids outside the token scope, unresolved/pending Yjs structs, oversized messages, and oversized documents are rejected.

## Development

```bash
npm install
COLLAB_HMAC_SECRET='replace-with-at-least-32-random-characters' npm test
COLLAB_HMAC_SECRET='replace-with-at-least-32-random-characters' npm run dev
```

Without `DATABASE_URL` the service uses an injectable in-memory repository only in development and tests. `NODE_ENV=production` fails closed unless `COLLAB_DATABASE_URL` (or `DATABASE_URL`) is a valid PostgreSQL URL, so a deployment cannot silently lose drafts after restart. PostgreSQL mode assumes the platform migration creates:

```sql
CREATE TABLE collab_documents (
  document_name text PRIMARY KEY,
  state bytea NOT NULL,
  version bigint NOT NULL DEFAULT 1,
  updated_at timestamptz NOT NULL DEFAULT now()
);
```

The SQL above is documentation only; this service intentionally does not run migrations.
