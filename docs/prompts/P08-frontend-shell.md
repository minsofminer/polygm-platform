# P8 — Frontend: Shell, Auth, Profile, Billing

> Paste `00-SHARED-CONTEXT.md` and the P2/P3 outputs first, then this.

## Role
You are a senior frontend engineer who builds dense, fast, keyboard-driven trading interfaces. You care about time-to-interactive more than animation, and you have strong opinions about the correct way to render a number that changes 20 times a second.

## Objective
Build the application shell and the account layer: routing, auth flows, navigation, profile, settings, wallet, and billing. This is the frame everything else lives in — get the foundations right or every later screen inherits the mistakes.

## Stack
Decide and justify: **Next.js (App Router) + TypeScript + Tailwind + Zustand + TanStack Query + shadcn/ui**, or the alternative you prefer. Whatever you pick, it must:
- Run as a **Telegram Mini App** (inside Telegram's webview) *and* as a normal website, from one codebase
- Be server-rendered for the public/marketing pages (SEO is a real acquisition channel — competitors rank for "polymarket whale tracker")
- Handle 20+ updates/sec without dropping frames

---

## Deliverables

### D1. Project foundation
- Repo structure, path aliases, barrel-file policy (or the rule against them)
- Tailwind config wired to the P2/P3 tokens, with the dark theme as default
- Type generation from the OpenAPI spec in P4 — **no hand-written API types**
- Error boundary strategy: per-route and per-widget, so a broken order book does not blank the page
- Global loading and error states
- Environment config with a build-time assertion that no secret ever reaches the client bundle. Add a CI check that greps the built output.
- The `data-theme` mechanism, density setting, and the reduced-motion path

### D2. Telegram Mini App integration
- Detect the Telegram webview and read `initData` — **validate the HMAC server-side, never in the browser**
- `MainButton` / `BackButton` usage: which flows use the native button and which don't. Be consistent or users get confused.
- Theme sync from Telegram, safe-area insets, and the viewport-height problem in mobile webviews (`100dvh`, and the resize jank)
- Haptics on trade confirmation — and nowhere else
- Deep links: `t.me/<bot>/<app>?startapp=<payload>` carrying a market or a referral, with validation of the payload
- Behaviour when opened outside Telegram (must degrade to a normal web app, not break)
- **Payment path:** Pro purchase inside Telegram must use Telegram Stars; on the web it uses Stripe. Same entitlement, two purchase surfaces. Specify the reconciliation.

### D3. Authentication flows
- **Sign up:** email+password, Telegram OAuth, Google. The wallet is created *after* signup succeeds, not during — otherwise a wallet-provider outage blocks registration.
- **Sign in**, with 2FA challenge, device recognition, and the "new device" notification
- **Password reset** that does not leak which emails are registered
- **Linking an existing Polymarket wallet** — prove control by signature, then link. Prevent claiming a wallet you don't control.
- Session handling: token storage (httpOnly cookie on web; the webview constraint on Telegram — state the difference and the mitigation), refresh rotation, logout-everywhere
- **Auth state as a single source of truth** with explicit `unauthenticated | authenticating | authenticated | expired` and the UI for each
- Route protection that does not cause a flash of the logged-out state

### D4. Navigation & shell
- Desktop: three-column terminal frame with resizable rails, persisted per user
- Mobile: bottom tab bar, five tabs max. Decide the five. (Markets · Tape · Trade · Portfolio · Profile is the obvious set — argue if you disagree.)
- Command palette (`⌘K`): search markets, jump to traders, run actions. This is the single highest-leverage UX feature for power users and almost nobody in this space has it.
- Global search: markets, events, wallet addresses, traders. Specify the debounce, the result grouping, and the empty state.
- Connection status indicator (WebSocket health) always visible. **Trading is disabled while disconnected** and the UI says why.
- Keyboard shortcuts, with a discoverable help overlay. Every shortcut listed.
- Breadcrumbs / back behaviour that works in Telegram's webview (it has its own back button and users will use it)

### D5. Profile & settings
- Account: email, username, avatar, linked Telegram, linked wallets, sessions with revocation
- **Security:** password change, 2FA enrolment and recovery codes, withdrawal address allowlist with cooldown display, key export with typed confirmation, session audit log
- **Trading defaults:** default order size, default slippage tolerance, confirm-above threshold, one-click trading toggle (with a warning), builder-fee transparency display (Polymarket makes builder rates publicly queryable — show ours)
- **Notifications:** per-channel (Telegram / email / push), per-signal-type, quiet hours, digest toggle
- **Display:** theme, density, number format, colour-blind mode
- **API keys** for the future API product: create, scope, revoke, last-used
- **Danger zone:** delete account, with what actually happens to an open wallet and open positions spelled out

### D6. Wallet & funding screens
- Balance panel: pUSD balance, pending deposits, allowance status
- **Deposit:** network selector with explicit warnings, address + QR, copy button with verification, minimum deposit, expected confirmation time, and a live "waiting for your deposit" state
- **Withdraw:** address entry with allowlist, typed confirmation, password/2FA, fee estimate, and the cooldown warning for new addresses
- **Key export:** multi-step, typed confirmation, explicit "anyone with this key controls your funds" warning, and an audit entry the user can see
- Transaction history with on-chain links
- The **stale/unavailable** states: wallet provider down, allowance exhausted, bridge stuck

### D7. Billing
- Plan comparison page with an honest feature table
- Stripe checkout (web) and Telegram Stars purchase (Mini App) — one entitlement, two sources of truth, reconciled server-side
- Entitlement hook with optimistic UI and a graceful degraded mode
- Invoices, cancellation (state clearly what happens to open positions and stored keys on cancel), and the dunning flow
- Referral dashboard: link, stats, payout method
- **The rule:** no paywall in the middle of a trade. Monetise analytics depth and automation, never the ability to exit a position.

### D8. Shared frontend infrastructure
- API client with retry, timeout, idempotency-key injection, and typed errors that map to the P4 error envelope
- WebSocket client with reconnect, backoff, heartbeat, resync, and a `useLive<T>()` hook that exposes `data | stale | disconnected`
- **The number rendering layer** — one component, used everywhere: tabular figures, signed PnL, cent notation, SI suffixes, and **flash-on-change with a 600ms background decay**. No component may render a raw number.
- `StaleIndicator` wired into every price surface
- Virtualised list for the tape (thousands of rows, 20+/sec)
- Form library with the decimal-safe number input (no floats in the money path — specify the type)
- Toast system with dedupe
- i18n scaffolding, RTL readiness, and the decision on number localisation

### D9. Performance & quality
- Budget: LCP < 2.5s on a mid-range Android over 4G, TTI < 3.5s, bundle < 200KB gzipped for the initial route. **Telegram users are disproportionately on mid-range Android.** Measure and report.
- Route-level code splitting; the chart library is lazy-loaded
- No layout shift from live data. Specify the fixed-width strategy for numbers.
- 60fps under a 20-update/sec tape — prove it with a trace
- Lighthouse ≥ 90 on the public pages
- Storybook with every component in every state
- Visual regression tests for the 10 most important screens

---

## Constraints
- No secret, key, or L2 credential in the client bundle. Ever. Add the CI check.
- No raw number rendering. No unguarded price display without a freshness indicator.
- Every form has a loading state, an error state, and a success state.
- Every route works in the Telegram webview. Test in Telegram, not just in Chrome.

## Quality gate
`npm run build && npm run test` passes. You can sign up, create a wallet, deposit against a mock, see a stale indicator appear when you kill the WebSocket, and buy Pro through both the Stripe and Stars paths in a test environment.
