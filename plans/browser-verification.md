# The browser pass — what running the suite for the first time found

Method: `microsoft/playwright-cli`'s skill, applied as the repository's own `@playwright/test` suite rather than as
ad-hoc CLI calls, because a browser run that proves something should leave a spec behind that keeps proving it.

This is the first time the suite has run against this repository. The other three passes are
`plans/animation-audit.md`, `plans/design-review.md` and `plans/react-review.md`; this one is different in kind,
because it is the first that could disagree with them. Three of its four findings are in **product code**, and none
of them was reachable from a unit test.

---

## 0. The environment claim was wrong, and that is finding zero

`playwright.config.ts` carried a paragraph saying the browser **cannot** run here: chromium needs `libxkbcommon0`,
`libasound2t64`, `libnss3` and friends, "and this sandbox runs as a non-root user with no way to install them".

The libraries were already installed, `sudo` works, and `npx playwright install chromium` +
`sudo npx playwright install-deps chromium` produced a browser that launches. The paragraph was corrected in place
rather than quietly deleted, because a stale "we cannot check this" is the most expensive kind of wrong note: it
stops the next person from trying.

**Reproduction:** `npm ci` → `npx playwright install chromium` → `sudo npx playwright install-deps chromium` →
`PGM_E2E_BASE_URL=http://127.0.0.1:3100 npx playwright test` against `next start -p 3100`.

## 1. An unreachable API turned every authenticated page into a 500 (product, HIGH · **fixed**)

`src/auth/server.ts`'s `callUpstream` awaited `fetch` with no `try`/`catch`. When the API was not reachable the
rejection escaped `proxy()` → `serverRead()` → the RSC render, and every signed-in page answered **HTTP 500 with
Next's error document** — no shell, no explanation, no product copy. A deploy, a restart or a dropped connection is
not a rare event; that path was the difference between "the app is having a moment" and "the app is broken".

The client half has always handled this carefully (`src/api/client.ts` maps failures to `NETWORK`/`TIMEOUT` with
copy that says the request may or may not have landed, and `src/api/public-read.ts` catches for the public pages).
The server hop was the half that did not.

Fixed: the fetch is wrapped, `AbortSignal.timeout(8_000)` matches the client's own timeout, and the failure answers
a **503** with a `NETWORK`/`TIMEOUT` envelope. 503 and not 0, because `serverRead` treats `status >= 400` as the
error path and a zero would have read as *success with an empty body*.

## 2. A failed sign-in left the button spinning for ever (product, HIGH · **fixed**)

`src/auth/SignInForm.tsx`'s submit handler awaited `fetch` with no `try`/`catch` either. Offline, the rejection took
`setBusy(false)` with it: the button stayed disabled and spinning, with no message, for as long as the page lived.
The person cannot tell a hung request from a rejected password, and the retry they want is impossible because the
control is disabled.

Fixed for all three call sites (password form, Telegram button, and `src/telegram/reauth.ts`'s silent re-auth),
with a new dictionary key `auth.signin.unreachable` — "The sign-in request did not reach the server…". It is
deliberately **not** `auth.signin.failed`: a network failure is not a wrong password and must not be worded like
one.

## 3. Escape raced the dialog it had just opened (product, MEDIUM · **fixed**)

The motion pass gave the dialog an exit animation: `requestClose()` sets `leaving`, the panel plays
`pgm-panel-out`, and only its `animationend` unmounts it. In the browser, closing the command palette still
**vanished it between two frames** — because `Shell.tsx` has a global `keydown` listener whose "close" action calls
`setPalette(false)`, and it runs on `window` while the dialog listens on `document`. Both saw the same Escape: the
dialog started its exit and the shell unmounted it on the same tick.

Fixed by ownership: while a dialog is open, the shell's global `close` action is a no-op and the dialog's own
handler is the only thing that closes it. `e2e/shell-fixes.spec.ts` asserts the exit class *and* that the element
survives until the animation ends — the exact claim no unit test could make, since a unit test mounts a Dialog with
no Shell above it.

## 4. Two specs had never reached the screens they describe (suite, MEDIUM · **partly fixed**)

`buy-flow.spec.ts` and `wallet-ceremony.spec.ts` stub the API at the browser's network boundary — the right way to
test a client — but their screens live behind `app/(app)/layout.tsx`, whose `serverAuth()` decides **on the server
from the `pgm_at` cookie** and redirects to `/sign-in` when there is none. A browser route intercept cannot
influence that. Both specs were asserting against the sign-in page and had been since they were written; the gates
never noticed because they check that the files exist and that a workflow runs them, and the browser never ran here.

**Fixed in part:** `e2e/session.ts` now provides the cookie, and both specs sign in first — that removed one whole
class of failure and got each spec past its first assertion. They still fail on **route and selector drift** (the
ticket and withdrawal surfaces moved after P13, in P12's ticket rewrite and the P16 route changes). They are marked
`test.fixme` with that reason in the file itself rather than deleted or silently red: the flows are still the right
flows to assert, and re-pointing them is a tracked follow-up.

## The suite now

`18 passed, 8 skipped` (the skips are the two marked specs across two projects), including the new
`e2e/shell-fixes.spec.ts` — **13 cases, all green** — which converts the previously-unverified claims of the
design and motion passes into browser facts:

| Claim | How the browser proves it |
|---|---|
| the rail handles resize from the keyboard | one ArrowRight = +0.005 of the viewport in `pgm.rails.v1` (the same store the drag writes), Shift = ×5, Home/End to the bounds, `aria-valuenow` following |
| the right rail's arrows are inverted | ArrowRight *shrinks* it — a separator moving right takes width from its right-hand panel |
| Enter collapses the rail from the handle | the aside gains `rail--collapsed` |
| a finger drag is not a scroll | `touch-action: none` computed on the live element |
| the skip link is the first stop and lands in the content | Tab → focused & visible → Enter → focus inside `#content` |
| `color-scheme` follows the theme | `dark` by default, `light` with the theme cookie, on the real `<html>` |
| the dialog's exit plays before removal | `pgm-panel-in` → Escape → `pgm-panel-out` → detached only afterwards |
| reduced motion skips the exit | under the webview project's `reducedMotion: reduce`, the dialog still closes, immediately |
| the API being down is not a 500 | the real production build with no backend: 200, shell visible, the app's own unavailable copy |

## `[UNVERIFIED]`, still

* **The feel of the motion.** The mechanics are now browser-verified; whether 8px of rise and a 125ms toast *read*
  well is a judgement about taste that no assertion carries, and this box has no eyes.
* **A real finger on the handle.** `touch-action: none` is asserted as computed style in a touch-emulating project;
  no human has dragged the rail on a phone.
* **The older two specs' flows** — marked `fixme`, with the reason, until they are re-pointed.

## Follow-ups this pass created

1. Re-point `buy-flow.spec.ts` and `wallet-ceremony.spec.ts` at the current screens and un-`fixme` them.
2. Run the suite in the nightly job as it stands — it is now known to be runnable, so a green nightly means
   something it did not mean before.
