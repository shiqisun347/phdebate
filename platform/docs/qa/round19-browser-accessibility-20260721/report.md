# Round 19 Read-only Browser / Accessibility Dogfood

| Field | Value |
|---|---|
| Date | 2026-07-21 |
| Production | `https://117.50.192.216` |
| Real match observed | `#357930` — “AI 的迅猛发展提升了还是降低了人类创作者存在的意义？” |
| Mode | Strictly read-only while a real match and MOSS were active |
| Scope | Anonymous watch, reconnect behavior, keyboard/screen-reader structure, mobile layout, competition/ranking/result boundaries, console errors, LCP/INP/request volume |

## Safety boundary

- No login, registration, room creation, seat claim, room mutation, speech, audio playback, fullscreen, settings, or control action occurred.
- The observed room contained one real human and seven AI debaters. Testing did not alter its state.
- No QA data was created because the real match was still running during the test window.
- No audio/TTS/MOSS/LiveKit/browser-playback implementation was modified.
- All Round 19 browser sessions were closed after evidence collection.

## Summary

The public product remained stable during a real AI-heavy match. Anonymous viewers received current stages and transcripts, a forced browser-offline transition produced an actionable message, and connectivity recovered automatically without clicking. Desktop and 390×844 layouts had no horizontal overflow. Public competition tabs were keyboard-operable, and measured Web Vitals were comfortably within good thresholds.

Two screen-reader issues were confirmed and fixed locally without touching the frozen debate stage:

| Severity | Count |
|---|---:|
| Critical | 0 |
| High | 0 |
| Medium | 2 |
| Low | 0 |
| **Total** | **2** |

## Coverage and outcomes

| Area | Outcome | Evidence |
|---|---|---|
| Anonymous lobby | Pass; the active room was discoverable with room, topic, competition, state and stage | `screenshots/home-desktop.png` |
| Anonymous real-match watch | Pass; read-only projection showed all eight seats, human/AI identity, current stage, timer and transcript | `screenshots/watch-desktop-live.png` |
| Offline/reconnect | Pass; offline mode displayed an exact explanation and retry affordance; restoring network automatically returned to “实时连接” without interaction | `screenshots/watch-offline.png`, `screenshots/watch-reconnected.png` |
| Mobile watch | Pass at 390×844; `scrollWidth === viewportWidth === 390`, bottom controls were 44×44px, and no content overlapped them | `screenshots/watch-mobile-live.png` |
| Keyboard navigation | Pass; order was skip link → lobby return → scrollable transcript → sound → fullscreen → settings. No trap was observed | DOM focus audit |
| Competition detail | Pass; tabs had `aria-selected`, roving `tabIndex`, `aria-controls`, and ArrowLeft/ArrowRight navigation | `screenshots/competition-mobile-live-count.png`, `screenshots/competition-live-tab-keyboard.png` |
| Rankings | Pass; empty season state was explicit; selectors were labeled and mobile width remained 390px | `screenshots/rankings-desktop.png`, `screenshots/rankings-mobile.png` |
| Result boundary | Pass; an anonymous request to a private terminated QA archive returned a recoverable “无权查看该房间” page rather than leaking data | `screenshots/result-terminated-desktop.png` |
| Public result after real match | Not observed; the real room was still in its summary stages when the read-only sessions were closed | — |
| Browser console | Pass; no JavaScript exceptions, failed page requests, CORS, mixed-content or unhandled-promise errors were observed | agent-browser console/errors output |
| Axe on public home | Pass; 36 rules passed and no violations were reported | axe browser audit |
| Axe on watch | Failed only for the duplicate/nested main-landmark root cause described in ISSUE-001 | axe browser audit |

## Performance measurements

Measurements were taken in production Chrome using buffered PerformanceObserver entries. They are point-in-time diagnostics, not a substitute for field RUM.

| Page / condition | TTFB | LCP | CLS | Requests | Transfer | Decoded |
|---|---:|---:|---:|---:|---:|---:|
| Home, cold session | 124ms | 592ms | 0.0016 | 31 | 199KB | 645KB |
| Watch, cold session | 173ms | 584ms | 0.00001 | 21 | 354KB | 1.26MB |
| Watch, warm navigation | 29ms | 204ms | 0.00001 | 20 | 11KB cache miss transfer | 1.26MB |
| Rankings, warm navigation | 116ms | 444ms | 0.0053 | 30 | 14KB cache miss transfer | — |

- Largest cold watch resource: approximately 135KB transferred / 520KB decoded JavaScript.
- Competition-tab interaction timing had a worst observed Event Timing duration of 112ms; this is below the 200ms “good” INP threshold.
- No meaningful layout shift or interaction jank was visible.
- The watch bundle is materially larger than public pages because it includes realtime/RTC support, but current cold-load latency is healthy. No bundle rewrite was justified by the measured experience.

## Confirmed issues and local fixes

### ISSUE-001: Watch/debate pages exposed nested duplicate main landmarks

| Field | Value |
|---|---|
| Severity | Medium |
| Category | Accessibility / semantics |
| Production status | Reproduced |
| Local status | Fixed and regression-tested |

The global layout rendered `<main id="main-content">`, while the frozen debate stage rendered its own `<main class="stage-center">`. Axe consistently reported:

- `landmark-main-is-top-level`
- `landmark-no-duplicate-main`
- `landmark-unique`

This makes “jump to main content” and landmark navigation ambiguous for screen-reader users.

Fix:

- Added a route-aware `MainContent` wrapper.
- Regular pages retain the global `main` landmark.
- `/rooms/:code/watch` and `/rooms/:code/debate` use a focusable neutral `div#main-content`, allowing the frozen stage to own the document’s single main landmark.
- The skip link still targets and focuses `#main-content`.
- Room lobby, control, result, and all public/admin pages retain their original global main landmark.

### ISSUE-002: Stage and speaker transitions had no screen-reader announcement

| Field | Value |
|---|---|
| Severity | Medium |
| Category | Accessibility / realtime feedback |
| Production status | Reproduced by DOM/accessibility-tree audit |
| Local status | Fixed and regression-tested |

Production exposed a polite live region for connection state, but the changing stage heading and current speaker were outside any live region. A screen-reader spectator could remain unaware that the debate moved from one stage or speaker to another unless they manually navigated back to the heading.

Fix:

- Added a visually hidden, atomic `aria-live="polite"` status for watch and debate pages.
- It announces only match status, current stage, and current speaker/side.
- It deliberately excludes full transcripts and timers to avoid continuous or disruptive speech.
- Free debate announces the currently eligible side when no speech is active.

## Local code changes

- `apps/web/components/main-content.tsx`
- `apps/web/components/main-content.test.tsx`
- `apps/web/components/stage-announcement.tsx`
- `apps/web/components/stage-announcement.test.tsx`
- `apps/web/app/layout.tsx`
- `apps/web/app/rooms/[code]/watch/page.tsx`
- `apps/web/app/rooms/[code]/watch/page.test.tsx`
- `apps/web/app/rooms/[code]/debate/page.tsx`
- `docs/qa/round19-browser-accessibility-20260721/**`

Protected files were not modified, including `components/debate-stage.tsx`, `lib/audio/**`, `public/worklets/**`, API voice runtime/providers/LiveKit code, MOSS prompts/deploy/gateway code, and configuration baseline files.

## Verification

- Final targeted Vitest: 4 files, 18 tests passed.
- New semantic/accessibility tests: 5 tests passed, including axe checks and skip-target focus behavior.
- TypeScript: `npx tsc --noEmit` passed.
- ESLint: 0 errors; 13 existing warnings only in frozen debate/audio files and PostCSS configuration.
- Full web suite before the final live-announcement addition: 42 files, 288 tests passed.
- Production build before the final live-announcement addition: passed.
- No commit, push, deployment, production mutation, or QA fixture creation was performed.

## Evidence inventory

Screenshots are under [`screenshots/`](./screenshots/). The browser performance observer used for measurements is under [`evidence/perf-observer.js`](./evidence/perf-observer.js). No credentials, cookies, access tokens, audio, or private room data were saved.
