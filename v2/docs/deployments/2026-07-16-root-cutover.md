# 2026-07-16 V2 root-path cutover

## Result

V2 now serves `https://117.50.218.251/` directly. Root-path `/api`, `/media` and `/ws` traffic is routed to the V2 API. Existing `/v2` page URLs redirect to equivalent root paths, while `/v2/api`, `/v2/media` and `/v2/ws` remain proxied for already-open parallel-deployment tabs.

Previously issued singular room links under `/room/:code/...` redirect to the corresponding V2 watch, debate or control page.

The legacy application is no longer the public root upstream. Its local process remains available for proxy-only rollback and was not deleted.

## Versioned releases and rollback

- Active root build: `runtime/web-releases/20260716-root-cutover`
- Active link: `.web-current`
- Parallel rollback build: `runtime/web-releases/20260716-parallel-rollback`
- Nginx rollback file: `/etc/nginx/jixia-nginx.conf.before-v2-root-20260716T031437Z`
- Environment rollback file: `.env.before-v2-root-20260716T031437Z`
- Supervisor rollback file: `/etc/supervisor/conf.d/jixia-v2.conf.before-v2-root-20260716T031437Z`

The first switch attempt intentionally rolled back when its Nginx reload assertion sampled the old worker before reload completion. No production state was lost. The assertion was changed to poll for the new route signature, and the second cutover completed successfully.

## Verification

- Root-path desktop and mobile Playwright suite: 6/6 passed.
- Root-path anonymous read-only route probe: 33 routes, no 5xx.
- Root-path abrupt WebSocket disconnects: 500/500 connected and aborted cleanly.
- Root-path sustained public WebSockets: 500/500 connected.
- `/v2/ws` compatibility WebSockets: 50/50 connected.
- API, engine, worker, PostgreSQL, Redis and web services remained healthy.
- Formal paused room `278571` was not advanced or mutated by the cutover tests.
- Concurrent root-path write tests covered four-room participant exclusivity, six-user seat races, start/cancel races, device takeover, dynamic presence binding and disconnect-to-AI-to-human recovery without calling the external Agent.
- Disposable verifiers now remove their room-code reservations and canonical archive locks; production verification artifacts were reduced to zero orphan room-code reservations and three locks belonging to the three formal archives.
- Post-cleanup backup `auto-20260716T034117Z.dump` restored with all 23 tables and matching row counts.
