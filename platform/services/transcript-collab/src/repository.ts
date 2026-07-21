import { Pool } from "pg";

export interface DocumentRepository {
  load(documentName: string): Promise<Uint8Array | null>;
  store(documentName: string, state: Uint8Array): Promise<void>;
  ready(): Promise<boolean>;
  close(): Promise<void>;
}

export class MemoryDocumentRepository implements DocumentRepository {
  private readonly documents = new Map<string, Uint8Array>();

  async load(documentName: string): Promise<Uint8Array | null> {
    const state = this.documents.get(documentName);
    return state ? new Uint8Array(state) : null;
  }

  async store(documentName: string, state: Uint8Array): Promise<void> {
    this.documents.set(documentName, new Uint8Array(state));
  }

  async ready(): Promise<boolean> {
    return true;
  }

  async close(): Promise<void> {}
}

export class PostgresDocumentRepository implements DocumentRepository {
  private readonly pool: Pool;

  constructor(connectionString: string) {
    this.pool = new Pool({ connectionString, max: 5, application_name: "phdebate-transcript-collab" });
  }

  async load(documentName: string): Promise<Uint8Array | null> {
    const result = await this.pool.query<{ state: Buffer }>(
      "SELECT state FROM collab_documents WHERE document_name = $1",
      [documentName],
    );
    return result.rows[0] ? new Uint8Array(result.rows[0].state) : null;
  }

  async store(documentName: string, state: Uint8Array): Promise<void> {
    await this.pool.query(
      `INSERT INTO collab_documents (document_name, state, version, updated_at)
       VALUES ($1, $2, 1, NOW())
       ON CONFLICT (document_name) DO UPDATE
       SET state = EXCLUDED.state,
           version = collab_documents.version + 1,
           updated_at = NOW()`,
      [documentName, Buffer.from(state)],
    );
  }

  async ready(): Promise<boolean> {
    try {
      await this.pool.query("SELECT 1 FROM collab_documents LIMIT 1");
      return true;
    } catch {
      return false;
    }
  }

  async close(): Promise<void> {
    await this.pool.end();
  }
}
