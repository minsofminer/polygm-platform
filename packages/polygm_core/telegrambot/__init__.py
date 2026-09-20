"""P12 · The Telegram surface: the bot that has to feel safe enough to deposit into.

The kit's P12 is the distribution engine — a bot, a Mini App, an alert channel, and a trade path — and it is the
one phase where a bug is *immediately* visible to a user, in a chat, next to their money. Three facts shape every
decision in this package:

**1. Telegram retries.** The Bot API re-delivers an update whenever a webhook does not answer 2xx (including when
we time out, when a deploy restarts the process, or when Telegram simply decides the answer was too slow), and the
retry carries the same `update_id`. A handler that is not idempotent by `update_id` therefore double-executes —
and the update most likely to arrive twice is the one where the user *pressed Confirm*. Every handler here is
wrapped by `updates.py`, and the trade path is the reason it exists rather than being a nice-to-have.

**2. Frameworks.** The kit asks for a choice between grammY, aiogram and telegraf, so the choice is made and
recorded — and it is **none of the three as the system of record**, for reasons that are about this codebase rather
than about taste:

* The money path is Python (`packages/polygm_core`: the risk gate, the ledger, the executor, the idempotency
  store). A TypeScript bot (grammY, telegraf) would either call our own HTTP API for every action — adding a
  network hop and a second place for the request contract to drift — or reimplement parts of the money path in a
  second language, which this project has refused since P01.
* aiogram is the right Python framework here, and it is still not what the update loop is built on: its value is
  routing, FSM state and middleware, and this package needs the *same* idempotency, rate limiting and priority
  rules that the rest of the product already implements against the same database. Two runners would mean two
  dedupe tables and two limiters disagreeing about one bot.
* What a framework actually buys us at the Bot API level is eight HTTP methods (`sendMessage`, `editMessageText`,
  `answerCallbackQuery`, `sendChatAction`, `setWebhook`, `deleteWebhook`, `getMe`, `answerWebAppQuery`) behind a
  thin client. That is `client.py`. If the surface grows past a framework's worth of routing, adding aiogram later
  is a routing change, not a money-path change — which is the property that decides it.

The webhook itself lives inside the existing FastAPI service (`services/api`), behind the same process, database
and audit trail as everything else, and P07's `security/telegram.py` already validates Mini App `initData` for the
login path. `tools/p12-gate-check.py` is the phase's floor.

**3. The UI is the product.** In a chat there is no layout engine, so the interface is: what the message says, what
the buttons under it do, and how the message *changes* while the answer is being fetched. `menu.py` is the command
surface as data (syntax, auth, response, error cases and the inline alternative for every command), `render.py`
builds the message plans (HTML-escaped text, keyboard markup, the two-beat skeleton→edit sequence), and `motion.py`
is the motion spec — including the honest statement of what Telegram can and cannot animate.

Nothing in this package talks to Telegram. It builds plans and records decisions; `client.py` is the only module
that speaks HTTP, so every rule above is testable without a network and without a bot token.
"""
from __future__ import annotations

from . import menu, motion, outbox, render, sessions, updates

__all__ = ["menu", "motion", "outbox", "render", "sessions", "updates"]
