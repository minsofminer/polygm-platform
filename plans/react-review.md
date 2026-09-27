# React best practices — the pass over the web app

Method: `vercel-labs/agent-skills`' `react-best-practices` (70 rules in 8 categories, ordered by impact). This is
the record, in the same shape as `plans/animation-audit.md` and `plans/design-review.md`: the findings, what was
changed, what was **checked and already right**, what is **refused or deferred with a reason**, and what stays
`[UNVERIFIED]`.

The product is small in dependencies and large in hand-written surface: `next`, `react`, `react-dom`, `zustand`
and `server-only`. That shapes the pass — there is no library to swap for a better one, so every finding is about
*this* code's behaviour, and the highest-impact category (waterfalls) is where the cost actually was.

---

## Findings

### R1 — the same read, twice per request (CRITICAL · **fixed**)

`app/market/[market]/page.tsx` and `app/trader/[who]/page.tsx` call `publicRead` in `generateMetadata` (title,
description, OG tags) and again in the page body, with identical URLs. Two round trips per page view, and Next
cannot collapse them because this transport sets `cache: "no-store"` on purpose — the API's own cache headers are
the ones that matter, and the fetch cache would answer from a copy that has forgotten them.

Fixed with `React.cache` on **both** server readers (`publicRead`, `serverRead`), behind a generic façade so no
call site changed. Scope is the request and nothing more: two callers in one render share one promise, nothing is
retained afterwards, and a per-request memo cannot serve one user another's render. `serverRead`'s memo is
per-URL too, so the account path is not weakened by it.

**What stands in for a test, and why.** `React.cache` attaches to a React request; outside one it is a
pass-through (verified: 4 invocations for 4 calls, in this very environment). So `src/api/read-dedupe.test.ts`
asserts the mechanism at the source level, asserts *the reason still holds* (those two pages still read twice —
if a refactor removes the second read, the memo stops paying for itself and the test says so), and asserts the
pass-through property that makes putting a cache in that module safe.

### R2 — independent reads awaited in turn (CRITICAL · **fixed**)

`app/(app)/alerts/page.tsx` awaited the rule list, then the delivery history. `app/(app)/automation/page.tsx`
awaited the rule list, then the template catalog. Neither pair had a dependency between its halves, so both pages
paid the sum of two round trips where they owed the slower one. This is the rule the category is named for, on the
two pages a trading product least wants to be slow: the console that arms rules and the screen that carries the
halt state.

Both now go through a loader that runs the pair with `Promise.all`. The loaders take the **reader as a
parameter**, and that is what makes the fix testable: `serverRead` imports `server-only`, which a test cannot
import, so a page that imported it could only be verified by reading its source. With the reader injected,
`read-dedupe.test.ts` records start and finish order and asserts that the second read begins *while the first is
still open* — the claim itself, on an observable timeline — plus that each half still fails alone.

`src/terminal/AlertsView.tsx`'s client `refresh()` had the same waterfall one layer down and gets the same fix.

### R3 — a poll that fires over its own request (MEDIUM-HIGH · **fixed**)

`src/screens/MarketView.tsx` polled the book every 2s, `src/terminal/TerminalScreen.tsx` its market detail every
10s, both with `setInterval(() => void load(), ms)`. That is not "every 2 seconds", it is "start a request every 2
seconds whether or not the last one answered". `request()` retries with backoff, so a slow or refusing API turns
the poll into a stack of in-flight reads, and unordered responses mean an **older book can land after a newer
one** — the screen freezes on stale data at exactly the moment the API is unwell.

Fixed with `src/live/usePoll.ts`: the next run is scheduled after the previous one *settles*, so there is at most
one request in flight per poll and the interval is a floor on the gap. It keeps the latest task in a ref (a
loader that depends on a market id must not re-arm the timer on every render), survives a failing task without
dying or leaking an unhandled rejection, and stops on unmount. `MarketView` and `TerminalScreen`'s `useMarket`
now use it; `TerminalScreen` keeps its `cancelled` guard in a ref, because a request already open when the screen
unmounts still resolves afterwards.

Tested on the timeline (5 cases): with a task slower than the interval, two intervals of elapsed time produce
**no** second call; the next call comes one interval after settlement; a task that throws three times still polls;
unmount stops it; a fresh closure each render does not restart it.

---

## Checked and already right (negative results)

| Rule | State |
|---|---|
| `async-parallel` elsewhere | `TerminalScreen.useMarket` already fetched its three endpoints with `Promise.all` |
| `async-defer-await` in pages | `market`/`trader` branch on a cheap regex first and only then await — the fetch sits in the branch that uses it |
| `bundle-barrel-imports` | no barrel files and no `index.ts` re-export layers in `src/`; imports are direct and `@/`-aliased |
| `bundle-dynamic-imports` | `next/dynamic` is already the boundary where it matters (`TmaSurface`, `TmaScreen`) and the TMA chunk is budgeted by P08 |
| `client-localstorage-schema` | rail widths and theme/view preferences are parsed with a type guard and clamped (`clampFraction`) rather than trusted |
| `client-passive-event-listeners` | no `addEventListener("scroll"…`; scroll behaviour is CSS (`overscroll-behavior`, anchor logic in one bounded container) |
| `rerender-use-ref-transient-values` | the drag path already writes CSS custom properties on the frame instead of re-rendering React — the same rule, implemented in P12 |
| `rerender-no-inline-components` | no components declared inside components in `src/` |
| `rendering-conditional-render` (`&&` with numbers) | none; no `.length &&` in any render |
| `js-hoist-regexp` / `js-set-map-lookups` | the hot paths (ladder folding, tape filtering) use module-level `Set`/`Map` and hoisted patterns |
| `server-no-shared-module-state` | readers are stateless; the only module state is deliberately per-browser (`localStorage`) |
| `advanced-init-once` | the store (`zustand`) is module-level by design and carries no request-scoped data |

---

## Deferred, with the reason (and the one refusal)

**D1 — pausing a poll while the tab is hidden.** A real saving for a 2s poll, and deliberately *not* smuggled into
a fix about overlapping requests: background-tab behaviour differs by platform (some throttle timers, some don't)
and a reader would want it measured before it ships. Recorded in `usePoll`'s docstring as out of scope, with the
reason, rather than left as an implied TODO.

**D2 — `useTransition` for the tape's high-frequency updates.** `rendering-usetransition-loading` and
`rerender-transitions` both point here. The tape already avoids the problem differently: it renders at 20Hz from a
single store subscription and never blocks on a network read, so the state updates a transition would defer are
the ones the product is *for*. Changing it would be a redesign of the tape's timing, not a best-practice fix.

**The refusal.** `client-swr-dedup` says to use SWR for request deduplication on the client. This app has a
bespoke client (`src/api/client.ts`) that carries the product's own semantics — idempotency keys on mutations,
`AbortSignal.timeout` per attempt, a retry policy that will not retry a timed-out *mutation*, and envelope
stamping so a number can say how old it is. SWR would replace that with a cache and leave the semantics to be
re-implemented around it. The *deduplication* half is worth having and is now covered where it actually pays
(per-request on the server, R1); the cache half is a redesign, not a fix, and not this pass.

## `[UNVERIFIED]`

* **The dedupe (R1) has no unit-observable test.** It is asserted at source level, with the pass-through
  property proven; the round-trip saving is reasoned from the two call sites, not measured against a live API.
  A route-level measurement needs the running pair and is the owner's to take with the pair up.
* **The poll fix (R3) is verified against fake timers**, which is where the bug lived — but the 2s book poll
  under a genuinely slow API has not been watched in a browser from here.

## What this pass did not cover

`vercel-optimize` (bundle and delivery) and `playwright-cli` (browser-driven verification) are separate skills
with separate criteria. The design review that preceded this one is `plans/design-review.md`; the motion audit is
`plans/animation-audit.md`.
