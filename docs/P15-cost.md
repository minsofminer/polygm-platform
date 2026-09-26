# P15 D8 — cost control

The envelope is the kit's: **under $10k total and under $300/month at launch**. The projection is **$119.38/month
point (range $109–129)**, it is computed from one file, and three separate documents are checked against it so the
number cannot quietly become a rumour.

| | |
| --- | --- |
| the arithmetic | `config/costs.json` — every line with its unit and where the price came from |
| the projector and the gates | `tools/p15-cost.py --project --gate` |
| the drift gate | `python3 tools/p15-cost.py --check` (config ↔ `docs/P15-deployment.md` ↔ `infra/terraform/outputs.tf`) |
| the actuals | `tools/p15-cost.py --actual <bill>` writes what an operator read off the console |

## Per-service attribution at launch

| line | what | $/month |
| --- | --- | --- |
| `hetzner-api` | API + ingest + Caddy + pgbouncer (1 × CPX32, FSN1) | 12.00 |
| `hetzner-executor` | executor + signer + reconciler, no ingress (1 × CPX22) | 9.00 |
| `neon-postgres` | Postgres, 1 CU always-on (730 h × $0.106) | 77.38 |
| `upstash-redis` | cache, queue, locks (5–15 M commands) | 20.00 (10–30) |
| `r2-backups` | ~30 GB of age-encrypted dumps, free egress | 1.00 |
| `tls-cdn` | Cloudflare free tier + Caddy for certs | 0.00 |
| `errors-dashboards` | Sentry + Grafana Cloud free tiers | 0.00 |
| **total** | | **118.40** (range 108–138) |

Two lines carry honest caveats rather than false precision: the Hetzner EU CPX32 figure is an interpolation
between two published prices and is `[UNVERIFIED exact]` until `terraform plan` prints it, and the 10× column's
Sentry team line is `[UNVERIFIED price]` — which is why the 10× total is a planning figure, not a quote. The cost
config file enforces that: a line marked unverified must say `UNVERIFIED` in its own text, and the checker fails
otherwise.

## Budget alerts at 50 / 80 / 100%

`tools/p15-cost.py --project --gate` exits non-zero only when a gate is crossed **for the first time** (it compares
against the last projection recorded in `var/cost-projection.json`). A gate that fires every month is a gate nobody
reads; the state file is what makes the first crossing an event.

* **50% ($150):** a look, in the weekly review. No action.
* **80% ($240):** the first cut becomes eligible (below), and the projection is re-run with real usage per line
  using `--usage neon-postgres=110.50`.
* **100% ($300):** the owner decides. It is a decision about the product's runway, not a knob an on-call turns at
  3am.

## What we cut first when money runs out

The order is decided now, in `config/costs.json`, so that it is not a negotiation during an incident:

1. **Analytics retention** — shorten the rollup window. Analysis is the cheapest thing to lose; nothing
   user-facing reads it.
2. **Long-tail market tracking** — stop polling markets nobody trades. One flag, flipped with a reason that lands
   in `flag_audit`, and the same flag the P05 backpressure design already uses for drop-order decisions.
3. **Never:** the executor, the money path, the ledger, the reconciliation loop, or their backups. A cheap money
   path is an expensive incident, and the first two cuts exist precisely so this one never has to be considered.

## The scaling plan

| scale | shape | $/month |
| --- | --- | --- |
| 1k users | 1 × CPX32, Neon 1 CU, Upstash PAYG — the launch column | ~119 |
| 10k users | 2 × CPX32, same Neon shape, cache self-hosted on the second executor host | ~150 |
| 100k users | the 10× column: 3 × CPX32 + LB, 2 × CPX22 executors, Neon capped at 2 CU + a 1 CU analytics replica, tape trimmed to 30 days | ~270 |

The failure at 10× is not CPU and not the tape: it is **Postgres**. Left at autoscale-ceiling 4 CU always-on it is
$309/month by itself, which breaks the envelope with nothing else attached. That is why the ceiling move (2 CU +
replica) is written down in D2 *before* it is needed, and why the self-hosted CloudNativePG option is described
with its real price — 4–8 engineer-hours a month and one 3am restore nobody has rehearsed.
