# P12 · Telegram: the bot, the channel, and a Mini App on its own URL

This phase is the kit's `/home/user/polygm/prompts/P12-telegram-bot.md`, built as D1 through D8 against the standing
constraints from P01–P11. It is the phase where the product gains a second *front door* — a chat where a person can
place an order and a public channel that anyone can watch — and where most of the work is refusing things correctly.

**Where it lives, in one page:**

| Deliverable | Code | Status |
| --- | --- | --- |
| D1 bot architecture | `services/api/app.py` (webhook, `_tg_claim`, outbox), `packages/polygm_core/telegrambot/updates.py`, `outbox.py`, `render.py`, `menu.py`, `ratelimit.py` | shipped, tested |
| D2 Mini App | `web/src/tma/` (`TradeSheet.tsx`, `trade.ts`, `market.ts`, `motion.ts`, `haptics.ts`, `TmaScreen.tsx`), `security/telegram.py` (P07's `initData` verification), surface middleware | shipped, deployed |
| D3 command surface | 20 commands with inline keyboards, `commands.py`, `nl.py` | shipped, tested |
| D4 trade execution | order card → `_order_from_card` → `_order_core`; notifications from `CODES` | shipped, tested |
| D5 alert delivery | `channel.py`, `ops.py`, broadcast routes reading `tape_fills` | shipped, tested |
| D6 wallet & custody UX | command surface + `/verify` (support-impersonation defence); Mini App screens | commands shipped; screens deferred to P13's wallet work |
| D7 onboarding | `/start` → first trade, instrumented steps, `telegram/metrics` | shipped, tested |
| D8 operations | kill switch, staged broadcast, ops page, username strategy | shipped, tested |

## The two surfaces, and why they are two deployments

The Mini App is its own Vercel project (`polygm-mini-app`) with its own URL, its own surface flag (`PGM_SURFACE=miniapp`)
and its own middleware: off-surface paths answer **404** with a sentence — not a redirect, because a redirect would make
the bot's URL a doorway to the main site and that is the second indexable copy this split exists to avoid. Its CSP
frames only Telegram origins and it carries two noindex signals (`X-Robots-Tag` and a `<meta robots>`), both scoped to
the surface: a noindex header in `web/vercel.json` would de-index the *main site*, which is why surface-specific headers
live in `next.config.mjs` behind the flag.

**Live:** https://polygm-mini-app.vercel.app · API: https://polygm-api.vercel.app

The Mini App and its API are separate projects on purpose. The Mini App can be redeployed or rolled back without
touching the backend it talks to, and the backend is the same ASGI application the local `uvicorn` serves —
`api/index.py` puts the repo on `sys.path`, runs the repo's own migration ledger and seed into `/tmp`, and then
exports `app` unchanged. No route, refusal code or auth level is a demo-only variant.

### What this deployment is honest about

Vercel's filesystem is per-instance and ephemeral. So: **fixtures are stable, records are disposable.** Every market,
book level and tape row comes from the repo's own seed and is identical on every cold start; wallets, orders and
sessions live in a `/tmp` database that an instance may not share with the next one, and that a recycled instance
loses. That is acceptable for what this deployment is for — letting the Mini App be opened and used end to end before
P13/P14 where real funds are gated — and it is written in `api/index.py` itself rather than left to be discovered. The
production path keeps Postgres (the SQLite twin has always been its dev stand-in).

## The bug this phase found in the phase before it

Every channel alert's trade button was dead, and it had been since P08.

The bot mints `https://t.me/<bot>/<app>?startapp=<payload>` in Python. The Mini App parses that payload in TypeScript.
The Python emitted a bare slug; the TypeScript expected `<tag>:<value>`. Each implementation was internally consistent
and neither had a test that crossed the boundary, so both suites stayed green while the product's acquisition loop —
tap an alert, land on the market, trade it — ended at "that link's payload did not look right". Three more faults sat
underneath it: a colon is not a character Telegram's `startapp` value is documented to carry; the target for a market
was `/markets/<id>`, the *list* route with a stray segment, a 404 no test had walked to; and the Mini App rendered
`payload.reason` — the string `bad-characters` — directly to the user, which is a machine token where this product
promises a sentence.

The grammar now lives once, in `contracts/startapp.json`: literal character set, first-separator split, one tag table.
Python reads it at import; TypeScript reads it through a generated mirror that `pretest` verifies the same way the
design tokens are verified. `tools/p12-gate-check.py` closes the loop the way the bug demands — it mints links with the
real Python function, runs the real TypeScript parser over them in node, and asserts the parser resolved the market the
link named, with a canary that the old shapes are refused.

Writing the contract caught a second bug of the same family: the first draft wrote the charset in *regex* notation
(`A-Za-z0-9_.-`), which TypeScript read as a class (correct) and Python read as a list of literal characters (also a
defensible reading) — so `fed-cut-sept` filtered down to `--` and every alert would have opened an empty market. The
contract now carries the literal set and each language derives what it needs from it.

## The 422 the web ticket had always returned

`src/screens/TradeTicket.tsx` posted `{market_id, side, amount_cents}` to `/v1/orders`, which requires
`{marketId, tokenId, side, price, size}`. Every trade the site attempted was a 422, and no test caught it because the
ticket had no test at all.

The fix is not a renamed field. A browser cannot honestly name a CLOB token id — it would be guessing which token
belongs to the market it is looking at — and a price it read seconds ago is the past. So the web ticket got the same
treatment the webview already had: `POST /v1/orders/amount` takes a slug, a side and an amount in USDC, resolves the
token, re-reads the best ask at that instant, and calls `_order_from_card` — the shared conversion that used to be
called `_tg_order_from_card`, renamed because it stopped being Telegram's the moment a second surface needed it. One
conversion, one risk gate, one ledger, three surfaces, and the same numbers from each: 50 USDC on a 0.62 ask is
80,645,161 micro-shares whether the confirm came from a chat tap, the Mini App, or the terminal.

## What a person can do today

From the bot: `/start`, `/market`, 20 commands with inline keyboards, a natural-language fallback that answers with a
confirmation card ("buy 50 yes on the fed market" → card → tap), `/stop` that cancels everything, `/verify` against
impersonation. From the channel: one market per message, the price with its age, a deep link and a trade button that
now lands on the right market. From the Mini App: the trade sheet with the ask, its age, the worst case and the fee
before the confirm; haptics on a fill or a refusal and nowhere else.

## The two things only the owner can finish

1. **`PGM_TELEGRAM_BOT_TOKEN` on `polygm-api`.** Every session-minting route refuses until that secret is set
   (`SECURITY_ENV_MISSING`, 503, in a body a client can read). The deployment is otherwise live and takes no real
   funds. The related pattern is that a *fake* token would be worse than none: the Mini App's launch would answer
   "invalid initData" for every real user, which reads as a bug in the Mini App rather than an unconfigured backend.
2. **BotFather.** Set the Mini App URL to `https://polygm-mini-app.vercel.app/` with short name `trade` — the code's
   deep links are `t.me/<bot>/trade?startapp=<slug>`, and there is no Bot API method for this part.

## Verification

* backend `pytest` **1219 passed**; web `vitest` **492 passed / 56 files**; `tsc --noEmit` clean; `next build` clean on
  both surfaces
* `tools/check-openapi.py` **617 passed, 0 failed** (every route documented, every status declared)
* `tools/p12-gate-check.py` **31 passed, 0 failed**; with `--live` **38 passed, 0 failed**, including the deployed
  Mini App's 404s, its noindex header and the API's health
* tampered `initData` → 401; a replayed payload → 409; duplicate `update_id` → no second execution
* the hop from the Mini App to the API is proven on the origin: a tampered payload comes back as the *API's* refusal,
  generated on the other side of the network
