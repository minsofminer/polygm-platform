# PolyGM — build workspace

Implementation of the PolyGM build kit (`minsofminer/polygm`), one prompt at a time, in order.
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
| P02 branding | `docs/P02-brand.md` | brand lock + asset inventory diff | pending |
| P03 design system | `docs/P03-design-system.md` | tokens compile | pending |
| P04 backend arch | `docs/P04-backend.md` | scaffold + migrations apply | pending |
| P05 ingestion | `server/ingest/` | live tape freshness < 2 s | pending |
| P06 trading engine | `server/engine/` | every order through the risk gate | pending |
| P07 security | `docs/P07-security.md` | no secrets in code/logs | pending |
| P08–P11 frontend | `web/` | animation review + no raw numbers | pending |
| P12 Telegram bot | `server/bot/` | Mini App launches | pending |
| P13 testing | `tests/` | money-path matrix green in CI | pending |
| P14 security testing | `docs/P14-*.md` | dependency audit clean | pending |
| P15 deploy | `docs/P15-*.md`, CI | staging → prod runbook | pending |
| P16 launch/growth | `docs/P16-*.md` | — | pending |

## Hard rules carried from the kit (not optional)

- **CLOB V2 only** — V1 died 2026-04-28; `py-clob-client-v2`, `timestamp`/`metadata`/`builder`, pUSD collateral.
- **No floats in the money path** — integer/decimal arithmetic end to end.
- **Every order through the risk gate**, including admin tooling.
- **Fail closed** — stale or missing price/book/balance ⇒ show it, disable trading.
- **No secrets in code, logs, errors, or telemetry.**
- **Never redraw the mark** with an image model — composite `brand/svg/mark.svg`.
- **No copied identity** — neither Polymarket's nor gmgn's logos, icons, copy or assets.
- **`[UNVERIFIED]` beats a plausible guess**, every time.
