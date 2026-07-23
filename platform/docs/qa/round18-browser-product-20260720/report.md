# Round 18 Browser / Product Dogfood Report

| Field | Value |
|---|---|
| Date | 2026-07-20 |
| Production URL | `https://117.50.192.216` |
| Test room | `#220192` — `QA Round18：人工智能是否应成为所有学生的必修工具？` |
| Test accounts | `qa_r18_owner_20260720`, `qa_r18_joiner_20260720` |
| Scope | Anonymous lobby, authentication, competition details, room creation/join, dual-user lobby, debate/recovery, desktop/mobile, error/empty/full states, control console, personal center |
| Audio boundary | TTS and browser audio implementation remained frozen; no protected audio file was changed |

## Summary

The production product was exercised as two real authenticated debaters plus 21 anonymous spectator tabs. The core path completed successfully from registration through room creation, seat claim, ready/start, human text fallback speeches, free debate, pause/resume, offline AI substitution, ownership transfer, human-seat restoration, termination, and archived result viewing.

| Severity | Count |
|---|---:|
| Critical | 0 |
| High | 0 |
| Medium | 1 |
| Low | 1 |
| **Total** | **2** |

Both findings have local code fixes and regression tests. They were not deployed by this browser-only task.

## Coverage and outcomes

| Area | Outcome | Evidence |
|---|---|---|
| Anonymous home and navigation | Pass; public competition cards, rankings entry, participate modal and mobile menu worked | `anonymous-home-desktop.png`, `anonymous-home-mobile.png`, `mobile-navigation-open.png` |
| Registration and login validation | Pass; password mismatch and invalid credentials produced specific messages | `register-password-mismatch.png`, `login-invalid.png` |
| Competition detail tabs | Pass; introduction, leaderboard, spectator list and rules were keyboard-addressable; empty states were explicit | `competition-daily-desktop.png`, `competition-ranking-empty.png`, `competition-watch-empty.png`, `competition-mobile.png` |
| Create and join room | Pass; custom training topic, fixed seat identity, six-digit room and second-user seat claim worked | `create-room-configured.png`, `owner-room-lobby-desktop.png`, `joiner-claimed-seat-settled.png` |
| Ready and start | Pass; only the owner could start after both human seats were ready; confirmation appeared before lock/start | `both-ready-owner.png`, `start-confirmation.png` |
| Role permissions | Pass; non-owner `/control` access was redirected to a read-only watch projection with a clear explanation | `joiner-control-forbidden.png` |
| Human turn controls | Pass; only the scheduled human received an enabled speech action; the opponent saw an exact disabled reason | `current-speaker-control.png`, `joiner-turn-after-text-submission.png` |
| Text fallback | Pass; both humans completed a turn through the microphone-unavailable text path and the stage advanced | `text-speech-fallback.png`, `joiner-turn-after-text-submission.png` |
| Pause and resume | Pass; owner control updated both the console and participant projection in real time | `control-paused.png`, `joiner-paused.png` |
| Disconnect recovery | Pass; after the owner stayed away for the grace period, AI substituted the seat and ownership moved to the remaining online human | `owner-return-after-ai-substitution.png`, `new-owner-control-restore-request.png` |
| Human restoration | Pass; returning human requested restoration, the new owner approved, and the original human identity returned | `owner-restore-requested.png`, `owner-restored-human.png` |
| Spectator cap | Pass; tabs 1–20 connected, tab 21 received the exact 20-person limit message, closing one tab and retrying immediately recovered the slot | `spectator-21st-rejected.png`, `spectator-slot-recovered.png` |
| Mobile responsive layout | Pass at 390×844; home, competition, debate and console had `scrollWidth === viewportWidth === 390` | `anonymous-home-mobile.png`, `competition-mobile.png`, `debate-mobile.png`, `control-mobile.png` |
| Invalid room | Pass; nonexistent room produced a recoverable search state instead of a blank/404 page | `invalid-room.png` |
| Personal center | Pass except ISSUE-002; active-match return path and security actions were present | `me-mobile-active-match.png` |
| Termination and archive | Pass; native confirmation was required, both clients transitioned, and the result archive retained 3 speeches and 79 events | `terminated-control.png` |

## Confirmed issues and fixes

### ISSUE-001: Terminal matches still offered ownership/seat mutation actions

| Field | Value |
|---|---|
| Severity | Medium |
| Category | Functional / UX |
| URL | `/rooms/220192/control` |
| Production status | Reproduced |
| Local fix | Implemented and tested |

After terminating the match, pause/skip/terminate were correctly disabled, but “让 AI 接替我的席位” and “移交房主” remained enabled. Ownership and participant-seat mutation has no valid purpose after a terminal state and invites rejected or confusing operations.

Reproduction:

1. Complete or terminate a running room as its owner.
2. Remain on the room control console.
3. Observe the terminal status and the still-active ownership/seat buttons in `terminated-control.png`.

Fix:

- Added a shared terminal-state guard for `completed`, `review_required`, and `terminated`.
- Disabled both ownership transfer and owner-seat AI substitution after terminal state.
- Replaced their labels with “比赛已结束”.
- Added a regression test covering both controls.

### ISSUE-002: Personal-center empty history CTA contradicted an active match

| Field | Value |
|---|---|
| Severity | Low |
| Category | Content / UX |
| URL | `/me` |
| Production status | Reproduced |
| Local fix | Implemented and tested |

With one active match and zero completed matches, the history empty state still said “参加第一场比赛”. The user was already participating in a match, so the message contradicted the page summary and active-match card.

Evidence: `me-mobile-active-match.png`.

Fix:

- When `active_rooms.length > 0`, the empty-history CTA now says “浏览更多赛事”.
- Users with no active or historical match still see “参加第一场比赛”.
- Added a regression assertion for the active-room case.

## Important observations that were not classified as defects

- A route transition can briefly render “离线/重连中” before the room WebSocket projection arrives. In every observed case it self-corrected in under one second and controls stayed safely disabled during the transition.
- Headless Chromium reports autoplay denial until explicit interaction. The product correctly presents “开启声音/点击播放”; no audio implementation was changed.
- The 21st spectator still receives the read-only REST snapshot so the page is not blank, but realtime entry is rejected and a clear retry action is shown. After a slot is released, retry succeeds without reloading.
- Leaving the live debate for more than the configured grace window correctly caused AI substitution. This was used to verify recovery, ownership transfer and restoration rather than treated as a failure.

## Local code changes

- `apps/web/app/rooms/[code]/control/page.tsx`
- `apps/web/app/rooms/[code]/control/page.test.tsx`
- `apps/web/app/me/page.tsx`
- `apps/web/app/me/page.test.tsx`
- This report and its screenshot evidence directory

No protected TTS, MOSS, LiveKit audio, browser playback, AudioWorklet, provider or voice-runtime file was modified.

## Verification

- Targeted Vitest: 2 files, 16 tests passed.
- TypeScript: `npx tsc --noEmit` passed.
- ESLint: 0 errors; 13 pre-existing warnings only in frozen `debate-stage`, audio, and PostCSS files.
- Production room `#220192`: explicitly terminated and archived.
- All 21 anonymous spectator tabs were closed after the cap test.
- QA accounts were handed to the production cleanup step for test marking, session revocation and deactivation.

## Evidence inventory

All screenshots are stored in [`screenshots/`](./screenshots/). No credentials, session cookies, API keys, or private tokens are included in the report.

