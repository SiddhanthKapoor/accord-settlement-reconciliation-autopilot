# Manual QA checklist

Most of the console is verified automatically by
`frontend/e2e/verify-ui.mjs`, which drives a real Chromium against a
running stack. This document covers two things that file cannot: how to
run it, and what it does **not** check and therefore still needs a human.

## Running the automated pass

```bash
# terminal 1
cd backend && uvicorn app.main:app --port 8000
# terminal 2
cd frontend && npm run dev
# terminal 3
cd frontend && SCREENSHOT_DIR=/tmp node e2e/verify-ui.mjs
```

It resets the database first, so point it at a development instance
only. It locates a Chromium already cached by Playwright rather than
downloading one (Playwright's installer needs privileged install on some
machines); override with `PLAYWRIGHT_CHROMIUM=/path/to/binary`.

### What it covers

| Flow | Check |
|---|---|
| Empty state | Console renders with no batch, prompts to run one |
| Data provenance | Banner present, states the live API returns zero records |
| Batch selection | Dataset and size selects change |
| Batch execution | Run starts, completes, table populates |
| Live progress | A **partial** count is observed mid-run (proves SSE, not a jump to 100%) |
| Record detail | Opens; shows merchant side, deterministic checks, policy threshold, audit history |
| Reconciliation explanation | Named checks with expected/observed values |
| Exception explanation | Failing record states why it failed |
| Human-review queue | Filter returns only HUMAN_REVIEW rows |
| Outcome filters | Each filter's rows all carry the matching badge |
| Audit trail | Events listed; chain integrity reported |
| Error state | Backend unreachable does not white-screen |
| Responsive | No horizontal overflow at 390px |
| Console health | No unexpected HTTP errors |

Last run: **22/22 passing**.

## What still needs a human

These are the gaps. They are listed because they are genuinely
unverified, not because they are expected to be broken.

### Visual and motion quality
- [ ] Progress bar animates smoothly rather than jumping between values.
- [ ] The record detail panel slides in and out cleanly; nothing flashes
      or reflows as it opens.
- [ ] Tab indicator animates between Console and Audit Trail.
- [ ] Type, spacing and colour hold together — the automated pass asserts
      structure and layout bounds, never whether it looks right.

### Reduced motion
- [ ] With **System Settings → Accessibility → Display → Reduce motion**
      enabled, transitions are suppressed. `MotionConfig reducedMotion="user"`
      is set at the root, but the effect is only observable with the OS
      setting actually on, which the headless run does not emulate.

### Real-device responsive
- [ ] Open on an actual phone. The automated check measures document
      overflow at a 390px viewport; it does not catch touch targets that
      are too small, sticky-header behaviour on a real scroll, or Safari
      viewport quirks. The desktop-only status chips are hidden below
      720px by design.

### SSE under adverse conditions
- [ ] Start a large batch, kill the backend mid-run, restart it. The
      browser's `EventSource` should reconnect and resume from
      `Last-Event-ID` without duplicating rows. The resume *logic* is
      unit-tested (`resume_point`) and the wire format is verified with
      curl, but a genuine mid-batch network drop is not automated.
- [ ] Leave the console open and idle for a few minutes with no batch
      running; the connection should stay open on keepalive frames.
- [ ] Open the console in two tabs during one batch; both should track
      progress independently.

### Long-running batch
- [ ] Run the full 5,000-record set with `GEMINI_API_KEY` set. The
      automated pass uses 100 records on the deterministic backend, so
      real model latency (p95 near the 10s timeout ceiling) and the UI's
      behaviour over several minutes are not covered.

### Data provenance accuracy
- [ ] Confirm the banner still reflects reality if credentials change —
      remove `RAZORPAY_KEY_ID` and check it reports the source as
      unconfigured rather than claiming live data.

## Known cosmetic gaps

- The status chips in the header are hidden below 720px. That is the fix
  for the 193px overflow described in
  `ENGINEERING_FAILURES_AND_FIXES.md`, not an oversight, but it does mean
  backend status is desktop-only.
- The records table scrolls horizontally on narrow screens rather than
  reflowing into cards.
