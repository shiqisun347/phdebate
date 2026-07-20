# Round 7 Production Browser QA Report

| Field | Value |
|---|---|
| Date | 2026-07-19 |
| App URL | `https://117.50.192.216` |
| Sessions | `round7-anon`, `round7-mobile`, `round7-user`, `round7-user-mobile`, `round7-next`, `round7-invalid` |
| Scope | Anonymous and ordinary-user journeys, result/history, rankings, 390×844 mobile layout, keyboard/focus, perceived latency, console/network health, and authorization boundaries |
| Safety boundary | No microphone test, no audio playback, no TTS interaction, no product-code inspection, and no modification of pre-existing active matches |

## Summary

| Severity | Count |
|---|---:|
| Critical | 0 |
| High | 1 |
| Medium | 3 |
| Low | 0 |
| **Total** | **4** |

## Issues

### ISSUE-001: Result timeline says 48/48 loaded but only the first 10 events are reachable

| Field | Value |
|---|---|
| Severity | high |
| Category | functional / ux / accessibility |
| URL | `https://117.50.192.216/rooms/551958/result` |
| Repro Video | `videos/issue-001-repro.webm` |

**Description**

The result page reports “已加载 48 / 48” and exposes all 48 events to the accessibility tree, but a sighted mouse/touch user who scrolls to the physical bottom of the document can only reach events #1–#10. Events #11–#48 are laid out beyond the document's scrollable height. This makes most of the match audit trail unavailable and is especially damaging when a participant needs to understand a pause, interruption, or recovery action.

Browser geometry at the bottom of the page reproduced the contradiction: document height was 2195 px with `scrollY=1295`, while later timeline items were positioned thousands of pixels below the viewport. The page could not scroll further.

**Repro Steps**

1. Open the public result page and observe the timeline count “已加载 48 / 48”.
   ![Step 1](screenshots/issue-001-step-1.png)

2. Scroll down to the timeline.
   ![Step 2](screenshots/issue-001-step-2.png)

3. Continue scrolling to the physical bottom of the page.
   ![Step 3](screenshots/issue-001-step-3.png)

4. **Observe:** only events #1–#10 are reachable even though the badge says 48 / 48; the footer is already visible and the page cannot scroll further.
   ![Result](screenshots/issue-001-result.png)

**Expected**

All loaded events must contribute to normal document height or appear in an explicitly scrollable, keyboard-accessible region. If pagination is intended, show a visible “加载更多” control and an accurate count.

---

### ISSUE-002: Watch stage highlights the current opposition speaker while showing the previous proposition speech without a stale-content label

| Field | Value |
|---|---|
| Severity | medium |
| Category | ux / content |
| URL | `https://117.50.192.216/rooms/551958/watch` |
| Repro Video | N/A (visible on load) |

**Description**

The paused watch page says the current stage is “反方一辩立论” and visually highlights 反方1辩 as the current seat, but the central quotation is the completed proposition speech and is labeled “正方1辩”. The data may be historically correct, yet the presentation makes it look as though the highlighted current speaker is delivering the opposite side's text. A viewer needs an explicit “上一位发言 / 最近完成的发言” label, or the current interrupted speaker's available text.

**Evidence**

Desktop and mobile both reproduce the mismatch:

- ![Desktop](screenshots/anonymous-watch-desktop.png)
- ![Mobile](screenshots/mobile-watch-390x844.png)

**Expected**

The central content should clearly distinguish `当前发言` from `上一段已完成发言`, including the speaker and completion/interruption state.

---

### ISSUE-003: Anonymous result view exposes the internal operational audit trail

| Field | Value |
|---|---|
| Severity | medium |
| Category | functional / privacy / content |
| URL | `https://117.50.192.216/rooms/551958/result` |
| Repro Video | N/A (visible without authentication) |

**Description**

An unauthenticated visitor can open the result URL and inspect internal operational events such as `辩手设备取得控制权`, exact online/offline timestamps, audio playback interruption, and the room owner's recovery action. Public transcripts and scores are reasonable for an open competition, but device-control and presence telemetry belong in the owner/admin audit view. On a student platform this unnecessarily exposes behavioural metadata and makes the public result harder to understand.

**Evidence**

- The anonymous page identifies itself as public through the login/register header while rendering the operational timeline: ![Timeline](screenshots/result-timeline-desktop-in-viewport.png)
- The same public page exposes repeated presence events on mobile: ![Mobile timeline](screenshots/result-timeline-mobile-in-viewport.png)

**Expected**

Project a public match narrative (stage changes, speeches, score and adjudication) separately from the privileged audit log (presence, control transfer, retries, service and interruption diagnostics).

---

### ISSUE-004: A long-paused room remains promoted as the only public match without a freshness or recovery state

| Field | Value |
|---|---|
| Severity | medium |
| Category | ux / content / lifecycle |
| URL | `https://117.50.192.216/` |
| Repro Video | N/A (visible on load) |

**Description**

The public lobby advertises one match as available to watch, but it has been paused since 14:17 and was still the only promoted match roughly five hours later. The card says only “比赛已暂停”; it does not say when it paused, whether recovery is expected, or whether the room is effectively abandoned. A new student entering the platform sees a stalled contest as the primary live-content signal.

**Evidence**

- Homepage promotion: ![Homepage](screenshots/anonymous-home.png)
- Result timeline showing the pause timestamp and long subsequent presence churn: ![Timeline](screenshots/result-timeline-desktop-in-viewport.png)

**Expected**

Add public-room freshness policy: show “暂停于 HH:mm / 等待房主恢复”, de-list or archive rooms after a configurable inactivity window, and provide owner/admin recovery reminders before automatic cleanup.

---

## Verified Passes

- Anonymous `/me` redirects to `/login?next=/me`, and successful login returns to `/me`.
- Anonymous and ordinary users cannot enter `/admin`; they are redirected to the public lobby.
- An ordinary non-owner opening another room's `/control` is redirected to `/watch`.
- Registration, login, profile empty state, room creation, ready state, and “continue match” recovery entry work.
- A second device is prevented from controlling the same seat until the user explicitly confirms takeover.
- Mobile navigation opens with correct expanded state, closes on Escape, and restores focus to the menu button.
- Participation modal is announced as a modal dialog, focuses its close control, closes on Escape, and restores focus to the invoking button.
- Homepage, profile, rankings, result and watch pages showed no JavaScript exceptions or unexpected failed application requests during the tested flows. The observed 401/404 responses came only from deliberate auth-boundary and nonexistent-room checks.
- Observed warm production timings were healthy: homepage DCL 59 ms/load 95 ms; profile DCL 41 ms/load 74 ms; mobile watch DCL 214 ms/load 322 ms. Profile API and navigation fetches were generally below 50 ms in this run.
- 390×844 layouts for homepage, rankings, lobby, watch, and result did not introduce horizontal overflow (`bodyWidth=viewportWidth=390` on the watch page).

## Test Data and Cleanup

- QA account: `round7qa0719a` / display name `Round7普通用户甲`.
- Isolated QA room: `898724`, 1v1 training, never started.
- The QA room is closed during cleanup at the end of this run.
- Pre-existing active/paused room `551958` was only viewed; no controls, audio, microphone, or match actions were used.
