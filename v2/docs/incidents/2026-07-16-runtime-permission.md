# 2026-07-16 PostgreSQL runtime directory permission incident

## Impact

At 2026-07-16 02:07 UTC, PostgreSQL stopped after it could no longer traverse the shared `runtime` directory. The API became unavailable because it could not connect to PostgreSQL. Redis, the web process, the match engine, the worker, and all database files remained present.

## Root cause

`deploy/upgrade-python-runtime.sh` stored Python virtual environments below `runtime/python-venvs` and changed the shared `runtime` directory to group `ubuntu` with mode `750`. PostgreSQL runs as `postgres`; it therefore lost directory traversal access even though `runtime/postgres` and its files still had the correct `postgres:postgres` ownership and restrictive modes.

## Recovery

- Restored the shared `runtime` parent to mode `751`, preserving `runtime/postgres` as `postgres:postgres` mode `700`.
- Verified `pg_control` before starting PostgreSQL.
- Restarted PostgreSQL, API, match engine, and worker, then verified readiness and the formal-data baseline.
- Created and fully restored a fresh PostgreSQL backup to a temporary database.

No database reinitialization, data-file replacement, or rollback was performed.

## Prevention

- Python releases now live under `.python-venvs`, outside the database runtime hierarchy.
- The upgrade script no longer changes permissions or ownership on the shared `runtime` directory.
- Before building or switching a Python release, the script verifies that the application user can traverse the project and that the owner of the PostgreSQL data directory can read `global/pg_control`.
- `deploy/verify-runtime-permissions.sh` provides an explicit deployment check.
- A negative deployment test confirms that an inaccessible PostgreSQL control file aborts the upgrade before a virtual environment is created or services are switched.

## Verification

- API readiness reported database, schema, Redis, engine, worker, storage, and backup checks healthy.
- Alembic revision: `0010_room_code_reservations`.
- Final post-cutover backup `auto-20260716T034117Z.dump` restored successfully with 23 tables and matching row counts.
- 500 concurrent public WebSocket watchers connected successfully.
- 500 WebSockets aborted before reading their initial snapshot without producing API errors.
- Desktop and mobile production browser tests passed 6/6.
