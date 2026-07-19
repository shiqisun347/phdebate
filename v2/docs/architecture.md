# V2 Architecture

- PostgreSQL is the authoritative store for users, competitions, rooms, append-only events, speeches and rankings.
- Redis carries room-scoped realtime messages, presence, control leases and Dramatiq jobs.
- FastAPI owns authentication and all authorization decisions. Anonymous clients receive a public room projection only.
- The match engine is server authoritative. Templates are copied into rooms at creation so later rule changes cannot alter an active or historical match.
- The worker handles non-interactive archival tasks. Agent and speech providers are accessed only by backend services.
- Next.js renders the public product and admin UI. Debate and watch routes share the same `DebateStage` component.
- Production completed parallel acceptance under `/v2` and now serves V2 at the root path. Compatibility proxies keep `/v2/api`, `/v2/media` and `/v2/ws` working for previously opened tabs; old page URLs redirect to their root-path equivalents. The previous frontend release and proxy configuration remain available for rollback.
