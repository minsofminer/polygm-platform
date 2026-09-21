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

## The Mini App URL in a plain browser

A deep link is the product's front door, and it gets opened in the wrong building constantly: a channel post is read in
a desktop client, a link is pasted into a chat with a friend, somebody's Telegram opens it in the system browser. In all
three there is no `initData`, so there is no session — and the first version of the screen answered that by refusing to
show the market at all. The Mini App's own URL, the one registered with BotFather, looked broken outside Telegram.

The card is now read-only rather than absent: the question, both asks, the age of the quote and the book all render, and
the confirm slot is replaced by the one route that can place an order. The interesting part is *why the reads were
failing*, and it was not the screen. The web app's proxy acquires a session before every upstream call — that is how the
bearer token stays out of the browser — and it did so for reads the API's own contract serves to nobody in particular.
Two rows of the route ledger are now declared `anonymous: true` (`/v1/public/market/{slug}`, `x-auth: public`, and
`/v1/markets/{id}/book`, `x-auth: none`) and matched against the ledger's template by `src/auth/anonymous.ts`, so a
public read cannot be session-gated by accident. It is a template match and never a prefix, because
`/v1/public/blocks` shares the prefix and is `x-auth: admin` — the shortcut would have opened the one route that
mattered. The CSRF guard still runs first, and the terminal's own reads stay behind the layout that redirects a
signed-out visitor to sign-in.

Verified on the deployed alias with no cookies and no `Origin`: the market page and the book answer 200 with their
freshness stamps intact, while `/v1/public/blocks`, `/v1/markets/0xM1/fills` and `/v1/orders` all still answer 401.

## The fill that was recorded and never sent

D4 promises two messages the product exists to deliver: "your order filled" and "your order was refused, and here is
why". Every piece of that existed — the executor wrote a notification row when it refused an order, `Store.book_fill`
was the single door money came through, `render.fill_card` and `refusal_card` drew both messages, and
`_tg_notify_fill` queued one at the top priority. What did not exist was the join. **Nothing called any of it.** A fill
was booked, recorded, and never sent; the unit tests all passed because each was testing its own end of a wire that
was not attached, and the phase's own acceptance run would have found it on a phone.

So the join exists now, and it is in the one process that is allowed to talk to Telegram:

* **Two rows, two owners.** The executor writes the event into `order_notifications` and may not send; the worker
  writes the message into `telegram_outbox` and may not book a fill. The bridge between them is the row id —
  `dedupe_key = order-event:<id>` — which is what makes a second drain a no-op instead of a second message.
* **The executor records, the worker sends.** `book_fill` writes the ledger and, in the same commit's wake, the order
  event (`filled`, or `partial_fill` when the print did not complete the order). The drain —
  `POST /v1/telegram/drain` — absorbs queued events before it plans its queue, renders the message, and sends it in the
  same pass. Sending straight from the executor would have put a Bot API round-trip inside the money path.
* **The card is a report, not a relay.** The first version of this code printed the numbers the notification carried.
  A test now hands it a fill whose notification says "1 micro-share at 0.0¢ for a 0.000001 USDC fee" next to a booked
  fill of 40 shares at 0.50 for 0.04 USDC, and asserts the card prints the *ledger's* numbers. Two prints average by
  size, in integer micros, so a fill that went through at two prices reports what was actually paid.
* **One message per event, enforced by the database.** The queue's `note` column cannot serve as the marker — the drain
  appends `mid=<id>` to it on success and *replaces* it with the client's explanation on failure, so a second drain
  found the marker gone and queued the fill again. `telegram_outbox` grew a `dedupe_key` with a partial unique index
  (0019, still undeployed anywhere, so no ALTER was needed); the bridge keys on the notification's row id, and the test
  that drains twice found that bug in the first place.
* **A case row is not a fill.** The reconciler writes its own `filled` row when a fill wins a race it was not supposed
  to win, carrying `{"case": "cancelled_race"}` and no numbers. Rendered naively that becomes "0 shares @ 0.0¢" — an
  invented fill, worse than silence — so the bridge renders a fill only from a row that carries a token and a size, and
  the test asserts the case row is skipped as "not a fill event".
* **An event with nowhere to go is not consumed.** No verified Telegram identity means no message and *no burnt row*:
  linking an account tomorrow delivers yesterday's fill rather than leaving a hole where it was.

Two smaller bugs fell out of writing the tests. The worker captured its clock before absorbing, so the message it had
just queued was "not due yet" and waited a whole tick — a fill sitting behind a millisecond. And `_tg_shares`'s
docstring claimed `80_645_161 → "80.65"` while the code truncated to `80.64`, which meant the chat and the Mini App
printed different numbers for the same fill (the webview used `toFixed(2)`, which rounds). The truncation is the rule
and now the only behaviour: `sharesText` in the webview and `_tg_shares` in Python are pinned to the same three strings
from both sides, and `0.50` no longer claims to be `0.5`.

## The wallet over HTTP

D6's backend is six routes — balance, transactions, a deposit quote, a deposit's progress, a withdrawal, and a key
export — and the interesting part is not that they exist. It is that a wallet is the half of a trading product where a
retry is worth money, so the ceremony is built around the idempotency key rather than beside it.

**The withdrawal ceremony runs *inside* `_idem_run`'s `work()`.** The ladder is allowlist → 24-hour destination
cooldown → balance → typed amount → typed address → password → authenticator. `_totp_gate` *consumes* an authenticator
step, so the first version — locks taken before the key was consulted — answered a retry of a completed withdrawal with
`TOTP_INVALID`: the user asked to wait for the next code for a withdrawal that had already been sent. With the work
inside the key, a replay is served from the stored body before the code can be consumed, and a *different* body under
the same key is a 409 rather than a second withdrawal. The refusals inside `work()` return `err(...)` so the partial
failure is stored together with the sentence the user was shown.

Four smaller things were wrong in the first pass and each is the kind that a route test finds and a phone does not:

* **`INSUFFICIENT_BALANCE` could not say the one useful thing.** "Available 10.000000 USDC" is our own ledger's
  number, nothing from the request, and it is the difference between "deposit more" and "ask for less" — so the code
  joined `_PUBLIC_DETAIL_CODES`. Every refusal whose detail is derived from a request stays out, deliberately.
* **Reserved cash was counted from the wrong states.** The balance route subtracted open intents in the states the
  *terminal* uses; the executor's are `pending|queued|submitting|uncertain|submitted` (`_OPEN_INTENT_STATES`), and
  `uncertain` is exactly the state that must stay reserved — it is a submission we cannot yet prove failed. Money a
  user can see but cannot spend is the one balance error that ends in a support conversation.
* **`destAddress` was refused as an unknown field.** The withdrawal body takes an `addressId` from the allowlist; a
  client that sent a raw address got a generic validation error naming a field it had been told to send, instead of
  `ADDRESS_NOT_ALLOWED` naming the rule. The field is now accepted *so that it can be refused* — the difference
  between "unknown: destAddress" and "withdrawals go to an addressId from your allowlist".
* **A stuck bridge had no state.** A deposit leg that failed was rendered from the status string; the progress route
  now reports `stopped` on the leg itself, so the sentence "the bridge stopped at *confirming* — nothing was lost and
  support can retry it" is derived from the ladder and not from prose.

The export is the honest version of "give me my key": password + authenticator + typing `EXPORT`, and what comes back
is the *wrapped* DEK with its KEK version and policy hash (never a private key — the unwrapping ceremony belongs to
P14), behind `cache-control: no-store`.

## The QR, and the reference that was wrong

A deposit address is a thing people photograph with another phone, so D6 ships a QR — and the encoder is
`web/src/tma/qr.ts`: byte mode, ECC M, versions 1–10, no dependency, no fetch, `QrTooLongError` past 213 bytes.

`tests/…` is not where this was decided. The fixtures (`web/src/tma/qr.fixtures.json`, rebuilt by
`tools/build-qr-fixtures.py`) are generated by a reference encoder and compared **cell by cell** by the phase gate,
because a QR that is wrong by one module scans fine in a browser and fails on a cheap camera. The first two runs
failed, and the failure was not in `qr.ts`: the reference (`segno`) pads the data stream with `8 - (length % 8)` zero
bits, which appends a whole `0x00` codeword when the stream is already byte-aligned. ISO/IEC 18004 §7.4.10 pads only
as far as the boundary. The spurious codeword shifted every later one, moved the EC blocks, and even changed the mask
`segno` chose — so the fixture was wrong in a way that looked like the encoder was. The generator now patches that
function, cross-checks every stream byte-for-byte against Python `qrcode`'s `create_data`, and reads every matrix back
with its own spec-based reader (format info, module walk, de-interleave, syndromes, payload). Three fixtures, all
reproduced: `address` (29×29, v3), an EIP-681 URI (33×33, v4) and a long string (49×49, v8), all mask 2.

## The wallet in the webview

The wallet is a **view on the same document** (`?view=wallet`), not a route. The Mini App's surface gate 404s
everything that is not the root document — that 404 is what stops the deployment becoming a second indexable copy of
the site — and the URL registered with BotFather has to be `/`, so a second route would be a page that cannot be
opened. The view keeps the URL honest with `history.replaceState`, so a reload lands where the user was and a link
shared from the wallet opens the wallet. The refusal path for a browser-opened link is the same read-only sentence the
trade sheet uses.

Three bugs, all found by writing the screen's own test rather than by looking at it:

* **The ceremony walked one step too far.** `CEREMONY` includes the `done` receipt entry, and the walk used
  `index + 1` — so the last tap rendered `Step 6 of 5 · Requested` and sent nothing. The walk now runs over
  `CEREMONY_STEPS` (the five steps a user taps; `done` is a state the 202 reaches) and the arithmetic lives in the
  model as `ceremonyPosition`, never in JSX.
* **The first press minted the key and returned.** `send()` created the idempotency key on tap and sent on the *next*
  one, so the code step looked dead. It now mints and sends in one act, and the key survives into `done` so a re-send
  replays from the stored body instead of filing a second withdrawal.
* **A refusal has to land on its own step.** `stepForRefusal` maps a code back to the rung that raised it, so
  `PASSWORD_WRONG` re-enables the password field with the sentence and the code beside it, rather than dropping the
  user at the start of a seven-step ceremony.

`Qr.tsx` is the only QR in the product. Its two colour literals were replaced by a **light-theme subtree**
(`data-theme="light"`) reading `--pgm-bg-base` and `--pgm-text-primary`: the scanner's contrast requirement is a
theme exception, not a second palette, and the four-module quiet zone is drawn by the SVG's own `viewBox` rather than
by CSS margins a rounded card could eat.

Running the phase's own gates afterwards is what caught the rest, and two of the findings were in files the generators
own:

* `brand/tokens.css` was **stale** because the Mini App's motion values and three P09 layout tokens
  (`--pgm-book-max-block`, `--pgm-rail-left`, `--pgm-rail-right`) had been hand-added to generated files, so
  regenerating them from the real sources silently deleted the tokens the shell lays out with. They now live in
  `tools/build-foundations.py` and are emitted by `tools/build-tokens.mjs` — where the values have a home, the
  `--check` runs are controls rather than a race.
* The Telegram bridge script is now declared once, in `app/tma/bridge.tsx`. Two layouts render it (the `/tma` subtree
  on the main site; the whole document on the Mini App's own deployment) and the gate's rule is that the URL may
  appear nowhere else, which is the rule that keeps `initDataUnsafe` and `HapticFeedback` out of pages that have no
  business calling them.
* A first `next build` failed on the QR's own CSS: the rules had been placed above `@import "../styles/tokens.css"`,
  and CSS requires imports first. The build said so in one line; nothing else would have.

## What a person can do today

From the bot: `/start`, `/market`, 20 commands with inline keyboards, a natural-language fallback that answers with a
confirmation card ("buy 50 yes on the fed market" → card → tap), `/stop` that cancels everything, `/verify` against
impersonation. From the channel: one market per message, the price with its age, a deep link and a trade button that
now lands on the right market. From the Mini App: the trade sheet with the ask, its age, the worst case and the fee
before the confirm; haptics on a fill or a refusal and nowhere else. And from the wallet view: the balance with what is
reserved for open orders, the deposit address with its QR and the bridge's five legs with the stuck one named, the
withdrawal ceremony (password, then the authenticator code) against the allowlist, and the key export behind a typed
`EXPORT` — every one of those refusals arriving as a sentence with its code beside it.

## The two things only the owner can finish

1. **`PGM_TELEGRAM_BOT_TOKEN` on `polygm-api`.** Every session-minting route refuses until that secret is set
   (`SECURITY_ENV_MISSING`, 503, in a body a client can read). The deployment is otherwise live and takes no real
   funds. The related pattern is that a *fake* token would be worse than none: the Mini App's launch would answer
   "invalid initData" for every real user, which reads as a bug in the Mini App rather than an unconfigured backend.
2. **BotFather.** Set the Mini App URL to `https://polygm-mini-app.vercel.app/` with short name `trade` — the code's
   deep links are `t.me/<bot>/trade?startapp=<slug>`, and there is no Bot API method for this part.

## Verification

* backend `pytest` **1284 passed**; web `vitest` **552 passed / 60 files**; `tsc --noEmit` clean
* `tools/check-openapi.py` **659 passed, 0 failed** (every route documented, every status declared)
* `tools/p12-gate-check.py` **41 passed, 0 failed**; with `--live` **49 passed, 0 failed**, including the deployed
  Mini App's 404s for `/wallet` and `/markets`, its `X-Robots-Tag: noindex`, and the API serving real market data
* the D6 backend's own file: `tests/test_wallet_api.py` **51 passed** over eight classes — the ceremony ladder, the
  replay-from-stored-body case, the watch-only `SIGNER_UNAVAILABLE`, the stepped deposit progress and the export's
  `typedConfirm`
* the QR is verified by construction, not by eye: `qr.test.ts` **9 passed**, and the gate's `wallet_qr()` section
  rebuilds the fixtures and compares **every cell** of three symbols (`address` v3, an EIP-681 URI v4, a long string
  v8) against the patched reference
* tampered `initData` → 401; a replayed payload → 409; duplicate `update_id` → no second execution
* the hop from the Mini App to the API is proven on the origin: a tampered payload comes back as the *API's* refusal,
  generated on the other side of the network
* **not verified on this machine:** `next build`. The P08 gate's c7/c8/c11 read `.next`, and this box is 2 vCPU with
  1,984 MB of RAM and no swap — the build reaches "Creating an optimized production build" and is OOM-killed
  (`BUILD_EXIT=137`), both with a 1,400 MB heap cap and with 850 MB. The one thing the attempt *did* buy is a real
  bug: the first run failed on `@import` ordering in `globals.css` (the QR rules had been added above the token
  import), which is fixed. **The build itself is fine — Vercel built this commit in 20 s**, which is why the alias
  below carries the wallet. P08's c8/c11 stay red on this machine and nowhere else.
* the deployed Mini App after this commit: `https://polygm-mini-app.vercel.app` answers 200 with `X-Robots-Tag:
  noindex`, `/wallet` and `/markets` 404, and the document carries the view switch (`>Trade<`, `>Wallet<`) — the
  wallet is a view on the root document, so the URL BotFather is registered with still opens the product. The phone
  run in the acceptance list is still the owner's step, and the two things under "only the owner can finish" are
  unchanged.
