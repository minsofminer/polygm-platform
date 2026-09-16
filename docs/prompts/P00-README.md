# Build Prompt Pack — PolyGM
### 16 sequential prompts to build a Polymarket trading terminal + Telegram bot

A gmgn-style product: analytics terminal + one-tap execution + Telegram bot, on Polymarket's order book. Branded in Polymarket's visual language.

---

## First, one legal correction

You asked for "the same ui/ux as gmgn.ai" and "the same telegram bot." I won't write a prompt that says *clone this site* — copying another product's markup, icons, copy text, or visual assets is copyright/trade-dress infringement, and it's the kind of thing that gets an app-store listing pulled or a C&D sent two weeks after you get traction.

What these prompts **do** give you is the thing that actually matters: **gmgn's information architecture and interaction model, rebuilt from scratch.** I pulled their own documentation and extracted the real patterns — the three-column terminal layout, the left-rail watchlist/trending split, wallet classification taxonomy (smart money / KOL / whale / sniper / new wallet / rat warehouse), Wallet Radar's four ranking modes, the 7D/30D PnL + win-rate profile, phishing-check metrics, copy-trade config, AFK automation. Those are **product patterns, not protected expression.** You get the same product; you own the code.

Everything here is original work in a documented pattern, wearing Polymarket's colours.

---

## How to use this pack

**Order matters.** Each prompt assumes the output of the ones before it. Don't skip P0.

| # | File | What you get out of it |
|---|---|---|
| — | `00-SHARED-CONTEXT.md` | **Paste this into every single session first.** Verified API facts, brand tokens, constraints. |
| P0 | `P00-README.md` | This file |
| P1 | `P01-research.md` | Validated wedge, competitor teardown, feature spec, data model |
| P2 | `P02-branding.md` | Name, logo, voice, full brand kit |
| P3 | `P03-design-system.md` | Design tokens, component library, every screen spec'd |
| P4 | `P04-backend-architecture.md` | Repo scaffold, schema, API contracts, infra |
| P5 | `P05-data-ingestion.md` | Ingest + WebSocket + signals + alert fanout |
| P6 | `P06-trading-engine.md` | Wallets, CLOB V2 orders, risk service, copy-trading |
| P7 | `P07-security.md` | Key management, authN/Z, threat model, hardening |
| P8 | `P08-frontend-shell.md` | Auth, signup/signin, profile, nav, billing |
| P9 | `P09-frontend-markets.md` | Markets, event detail, order book, charts |
| P10 | `P10-frontend-terminal.md` | The terminal: tape, traders, whale tracker, radar, portfolio |
| P11 | `P11-leaderboard.md` | Leaderboard + trader profiles + referrals |
| P12 | `P12-telegram-bot.md` | Mini App + bot commands + alerts channel |
| P13 | `P13-testing.md` | Unit → integration → E2E → load → chaos |
| P14 | `P14-security-testing.md` | Pentest, key-compromise drills, dependency audit |
| P15 | `P15-deployment.md` | CI/CD, staging→prod, observability, runbooks |
| P16 | `P16-launch-growth.md` | Go-to-market, distribution, retention, metrics |

---

## Operating rules for every session

Paste these at the top of each prompt:

1. **Do not invent API behaviour.** Every Polymarket endpoint, field name, and limit must be checked against `docs.polymarket.com` before you write code against it. CLOB V1 is dead.
2. **Fail closed.** If a price, balance, or book is stale or missing, the UI must say so and disable trading. Never render a stale number as if it were live.
3. **No money moves without the risk service.** Every order path goes through the risk gate. No exceptions, including admin tooling.
4. **No secrets in code, logs, errors, or telemetry.** Ever.
5. **Read-only by default.** New endpoints are read-only until explicitly made write.
6. **Write tests before you claim done.** "It works" without a test that ran is not done.
7. **Surface unknowns.** If you cannot verify something, say so in the deliverable rather than filling the gap with a plausible guess.

---

## Sequencing against budget

You have <$10k and you're hiring. Run it like this:

- **Weeks 1–2:** P1, P2, P3 (cheap — mostly you + AI, no dev needed)
- **Weeks 3–6:** P4, P5, P8, P9 → **read-only product live.** This is your Phase 0 distribution play.
- **Weeks 7–10:** P7, P6, P12 → trading + bot. First routed dollar.
- **Weeks 11–12:** P13, P14, P15 → harden and ship.
- **Ongoing:** P10, P11, P16.

If money runs out after week 6, you still have a live product with an audience. That's the point of the ordering.

---

## Reference implementation

`/home/user/polygm/` contains a working, verified prototype: zero-dependency Python backend pulling live Gamma + CLOB + Data + leaderboard APIs into a shared cache, plus a mobile-first Mini App front end. It is the thing P4–P6 replace, and the proof that the data layer works. Point your developer at it before they start.
