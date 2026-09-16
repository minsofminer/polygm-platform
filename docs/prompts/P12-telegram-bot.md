# P12 — Telegram Bot & Mini App

> Paste `00-SHARED-CONTEXT.md` and the P2/P3/P6/P8 outputs first, then this.

## Role
You are an engineer who builds Telegram products that people trust with money. You have read the Bot API rate-limit docs twice. You know that a Telegram trading bot's real job is not the trading — it's making a user feel safe enough to deposit.

## Objective
Build the Telegram surface: the bot, the Mini App integration, the alert channel, and the trade execution path. **This is the distribution engine.** Every successful product in this category grew through Telegram, and Betmoar reached $101M of monthly routed volume with no press coverage at all — just a bot.

## Product shape
Two surfaces, one account, one wallet: the **web terminal** for scanning and analysis, the **Telegram bot** for fast execution and alerts. Same balance, same positions, either surface. This is exactly gmgn's model and it is why it works.

---

## Deliverables

### D1. Bot architecture
- Framework choice (grammY vs aiogram vs telegraf) — justify
- **Update handling:** webhooks, not long-polling, in production. Specify verification of the webhook secret token, and the local-dev path.
- Concurrency model: Telegram retries on non-2xx. Make every handler **idempotent by update_id** or you will double-execute trades on a retry. This is the bug that loses money.
- Rate limits (⚠️ verify current values before launch): ~30 messages/sec globally, ~1 msg/sec per chat, ~20 msgs/min per group. Design the outbound queue with per-chat and global buckets, and priority so a paying user's fill notification is not stuck behind the free channel's broadcast.
- Session state per chat, persisted, with expiry
- Error handling: a handler that throws must not kill the worker, and must not silently swallow a trade command

### D2. Mini App integration
The differentiator. Every competitor has a button menu; **we ship a real terminal inside Telegram.**
- The P8 web app runs as a Mini App from the same codebase
- `initData` validation **server-side** with HMAC against the bot token, `auth_date` freshness window, and replay defence. ⚠️ A broken check means anyone can impersonate any Telegram user. Write the routine and a test proving a tampered payload is rejected.
- `MainButton` for trade confirmation, `BackButton` for navigation, haptics on fill — and nowhere else
- Deep links: `t.me/<bot>/<app>?startapp=<payload>` where payload is a market slug, a trader handle, or a referral code. Validate and length-limit the payload; never eval it.
- Theme sync, safe-area insets, viewport-height handling in the webview
- **Payments:** Pro purchase inside Telegram **must** use Telegram Stars (Telegram requires this for digital goods inside the app, for App Store/Play compliance). No crypto, no Stripe inside the Mini App. Same entitlement as the web Stripe purchase, reconciled server-side. Stars ≈ $0.013–0.015; app stores take up to 30%; withdrawal via Fragment has a 21-day hold and a 1,000-Star minimum — factor that into pricing.
- Degradation: opened outside Telegram, it is a normal web app.

### D3. Command surface
Design the full command set. Every command: syntax, auth requirement, response format, error cases, and the inline-keyboard alternative (Telegram users tap, they do not type).

Minimum set:
- `/start` — onboarding, wallet creation, first-deposit prompt
- `/wallet` — balance, deposit address, withdraw, key export
- `/deposit` `/withdraw`
- `/balance` `/positions` `/pnl` `/orders`
- `/market <slug or link>` — paste any Polymarket URL, get prices + one-tap BUY/SELL. **This is the single highest-value command.** Competitors prove it.
- `/search <query>`
- `/price <slug>`
- `/copy` — list sources, add, config, pause, stop
- `/alerts` — list, add, pause
- `/follow <address>` `/unfollow`
- `/top` — leaderboard snapshot
- `/settings` — defaults, slippage, confirm threshold, notifications
- `/stop` — **the panic command.** Cancels all open orders and halts automation instantly. Must work with no confirmation prompt and must be tested. Put it in the main menu, not buried.
- `/help` `/support`

Natural-language fallback: a user who types "buy 50 yes on the fed market" should get a confirmation card, not an error. Specify the parser, its confidence threshold, and the disambiguation flow when two markets match.

### D4. Trade execution over Telegram
- **Order card:** market, outcome, side, price, size, estimated fee, max loss, and Confirm / Cancel inline buttons
- Confirmation required above a threshold; one-click mode as an opt-in with a warning
- Fill notifications with realised price, size, fee, and position after
- Rejection notifications **in plain language**, mapped from the machine code
- The `unknown`-order state: the user must be told their order is in limbo and what we are doing
- `/stop` behaviour end to end
- **Every order carries our builder code**, same as web. Attribution must be identical across surfaces.

### D5. Alert delivery
- The free public channel: large fills, volume spikes, new markets in tracked categories, resolution-imminent. **This channel is the acquisition engine — treat it as a product, not a byproduct.**
  - Format: tight, scannable, one market per message, deep link into the Mini App, and a **one-tap trade button on the alert itself**. Alert → trade in one tap is the gap nobody in this market has closed.
  - The quality gate before broadcasting (see P7 D1): an attacker can create a market and trade it. If our channel amplifies junk to thousands of people, we lose the channel's credibility permanently.
  - Broadcast cadence caps so the channel does not become noise. A channel people mute is a channel that acquires nobody.
- Personal alerts: watched wallets, custom rules, per-channel routing, quiet hours, digest mode
- Priority queue so a fill notification beats a broadcast

### D6. Wallet & custody UX over Telegram
- Wallet creation on first use, with the deposit address and a QR
- Multi-chain deposit detection → bridge → pUSD, with progress messages and the stuck-bridge recovery path
- Key export: multi-step, typed confirmation, explicit warning. **Users check whether export works before depositing — competitors advertise it.**
- Withdrawal password and address allowlist
- **Support-impersonation defence, built into the product:** the bot must state, in `/start` and in `/help`, that no admin will ever DM first, never ask for a seed phrase, and never ask for a deposit. Include a `/verify <username>` command that tells a user whether an account claiming to be support is real. This is the most common attack on Telegram trading products and defending against it in-product is a genuine differentiator.

### D7. Onboarding
Target: **/start → first trade in under 90 seconds.** Specify every step and its expected drop-off, then cut a step.
1. `/start` → what this is in two lines + risk disclosure link
2. Wallet created automatically, deposit address shown
3. Deposit detected → confirmation with balance
4. A curated market card with a pre-filled trade, ready to confirm
5. First fill → the position view and the `/stop` command pointed out

Instrument each step. If step 3→4 conversion is below a threshold you set, the product is not ready for paid acquisition.

### D8. Operations
- Bot username strategy: the main bot, and whether to run separate bots per function (gmgn runs a main entry point plus per-chain trading bots). Argue your position; a second bot is a second thing to get banned.
- What happens if the bot is reported, restricted, or **banned**. Mirror to Discord and keep the web app as the primary hedge. Specify the recovery path and how users are told.
- Broadcast tooling for announcements, with dry-run and staged rollout
- Metrics: DAU, commands per user, alert→trade conversion, deposit→first-trade time, churn after first loss
- A **kill switch** that disables trading platform-wide from Telegram admin, separate from the P6 one, in case the API is unreachable

---

## Constraints
- Every handler idempotent by update_id.
- No private key or L2 credential in a Telegram message, log, or error.
- No trade command executes without the risk gate.
- No subscription sold in crypto inside the Mini App.
- Test in Telegram on a real device, not only in a browser.

## Quality gate
On a phone, in Telegram: `/start` → deposit against a mock → trade from a pasted market link → receive a fill notification → `/stop` cancels everything. Timed, with screenshots. Then show me the test that proves a tampered `initData` payload is rejected and a duplicated update_id does not double-execute.
