# Openout — build workspace

Implementation of the Openout build kit (specified in `minsofminer/polygm`, working name
`PolyGM`), one prompt at a time, in order.
**This repo holds the product. `polygm` (the kit) holds the spec.**

Origin: `https://github.com/minsofminer/polygm` @ `470f729` (cloned 2026-09-16). Docs, brand kit and
vendored MIT skills are carried forward verbatim so the build and its binding rules never drift apart.

## Start every session here

```bash
. /home/user/env.sh          # CLIs on PATH + credentials
cd /home/user/polygm-platform
```

1. `docs/00-SHARED-CONTEXT.md` — verified facts. Always first.
2. `docs/AGENTS-BUILD.md` — the 16-phase protocol actually in force (order, gates, commits).
3. `docs/BUILD-LOG.md` — what is built, what is verified, what is `[UNVERIFIED]`.

## Layout

| Path | Purpose |
|---|---|
| `docs/` | Shared context, plan, prompt pack, and the numbered `P0N-*.md` deliverables |
| `docs/verification/` | Per-phase evidence: probe scripts' output, gate runs, unverified ledgers |
| `tools/` | The checks that make "done" a command rather than a claim |
| `brand/` | Approved Brand Lock + canonical `svg/mark.svg`; `fixed` fields immutable |
| `skills/` | Vendored MIT skills (higgsfield-brandkit, emilkowalski) — methodology, not deps |
| `server/` | Reference prototype → becomes the real backend at P4–P6 |
| `web/` | Frontend from P8 on (Mini App + web) |

## Phase status

| Phase | Deliverable | Gate | Status |
|---|---|---|---|
| P01 research & spec | `docs/P01-product-spec.md` | `python3 tools/p01-gate-check.py` | **done** ✅ |
| P02 branding | `docs/P02-brand.md` | `python3 tools/p02-gate-check.py` | **done** ✅ |
| P03 design system | `docs/P03-design-system.md` | `python3 tools/p03-gate-check.py` | **done** ✅ |
| P04 backend arch | `docs/P04-backend-architecture.md` | `python3 tools/p04-gate-check.py` | **done** ✅ |
| P05 ingestion | `docs/P05-data-ingestion.md` | `python3 tools/p05-gate-check.py` | **done** ✅ |
| P06 trading engine | `docs/P06-trading-plane.md` | `python3 tools/p06-gate-check.py` | **done** ✅ |
| P07 security | `docs/P07-security.md` | `python3 tools/p07-gate-check.py` | **done** ✅ |
| P08 frontend shell | `docs/P08-frontend-shell.md` | `python3 tools/p08-gate-check.py` | **done** ✅ |
| P09 markets | `docs/P09-frontend-markets.md` | `python3 tools/p09-gate-check.py` | **done** ✅ |
| P10 terminal | `docs/P10-frontend-terminal.md` | `python3 tools/p10-gate-check.py` | **done** ✅ |
| P11 leaderboard | `docs/P11-leaderboard.md` | `python3 tools/p11-gate-check.py` | **done** ✅ |
| P12 Telegram bot | `docs/P12-telegram-bot.md` | `python3 tools/p12-gate-check.py` | **done** ✅ |
| P13 testing | `docs/P13-testing.md` | `make p13` (`python3 tools/p13-gate-check.py`) | **done** ✅ |
| P14 security testing | `docs/P14-*.md` | dependency audit clean | pending |
| P15 deploy | `docs/P15-*.md`, CI | staging → prod runbook | pending |
| P16 launch/growth | `docs/P16-*.md` | — | pending |

P13's gate is unusual: its subject is the other gates, so `make p13` re-runs the chaos drills and the load suite
as well as reading every recorded artifact. `make p13-read` is the same gate without the half-hour, which is what
the pull-request workflow runs. The money-path matrix (`make p13-matrix`) is the only coverage gate in the repo:
43 rows, every one resolving to a test that exists and passes.

## Hard rules carried from the kit (not optional)

- **CLOB V2 only** — V1 died 2026-04-28; `py-clob-client-v2`, `timestamp`/`metadata`/`builder`, pUSD collateral.
- **No floats in the money path** — integer/decimal arithmetic end to end.
- **Every order through the risk gate**, including admin tooling.
- **Fail closed** — stale or missing price/book/balance ⇒ show it, disable trading.
- **No secrets in code, logs, errors, or telemetry.**
- **Never redraw the mark** with an image model — composite `brand/svg/mark.svg`.
- **No copied identity** — neither Polymarket's nor gmgn's logos, icons, copy or assets.
- **`[UNVERIFIED]` beats a plausible guess**, every time.
