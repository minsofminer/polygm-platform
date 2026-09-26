# P15 D5 — alerting: few, loud, actionable

The kit's rule for this phase is short and unforgiving: **an alarm without a runbook gets deleted, and fewer than
three pages a week or the thresholds are wrong.** Both are enforced by a program rather than a promise, because
both are the kind of rule that everyone agrees with and nobody notices breaking.

| what | where | how it is checked |
| --- | --- | --- |
| the alarm registry | `ops/alerts.yaml` | `tools/p15-alerts.py --check` |
| the alarms themselves | the on-call dashboard | `tools/p15-dashboards.py` renders the same rules |
| the runbooks | `docs/runbooks/*.md` | `tools/p15-runbooks-check.py` (5 sections, ≥3 runnable commands, every path and flag verified) |
| the page budget | `ops/alerts.yaml` (`budget`) | the checker sums every rule's `expected_pages_per_week` and fails at or over 3.00 |
| the deliberate firings | `docs/verification/p15-alerts-fired.jsonl` | `tools/p15-alerts.py --record <id> --evidence "…"` |

## The five properties an alarm must have

1. **A runbook**, checked in both directions: the rule names a file that exists, and that file's front matter
   claims the rule's id. One side can then never rot alone.
2. **An owner** (`money-path`, `data-plane`, `platform`, `product`, `revenue`, `security`), from a map that also
   says who that is. An alarm whose owner is "the team" is an alarm nobody owns.
3. **A threshold with a stated reason.** Every rule carries `why:`. The reason is what stops a threshold being
   tuned away at the first inconvenient page — and what makes the next-tuning-an argument possible.
4. **A severity and a route.** SEV1 and SEV2 must route to the on-call channel; the checker fails a page-class
   alarm that is configured to go somewhere a sleeping human will not see it.
5. **A number.** `expected_pages_per_week` per rule, summed against the budget. An alarm added without a number is
   an alarm nobody counted; the total is printed on every check run.

## What is actually paged

**SEV1 — page now** (a DM to the on-call, repeated every five minutes until acknowledged, kill-switch state in
every message):

| alarm | fires on | runbook |
| --- | --- | --- |
| `unreconciled-orders` | `money.unreconciled.page` true for 60 s — the single most important metric in the system | [unreconciled-orders](runbooks/unreconciled-orders.md) |
| `orders-unknown-state` | any intent in `submitting`/`uncertain` for 60 s | [unreconciled-orders](runbooks/unreconciled-orders.md) |
| `kill-switch-engaged` | engaged, with the reason and how long | [kill-switch](runbooks/kill-switch.md) |
| `executor-down` | no heartbeat for 90 s | [executor-down](runbooks/executor-down.md) |
| `key-compromise-indicator` | an indicator in the last 24 h | [key-compromise](runbooks/key-compromise.md) |
| `withdrawal-spike` | this hour ≥ 3× the mean hour of the last day | [withdrawal-spike](runbooks/withdrawal-spike.md) |
| `all-websocket-sources-dead` | every `ws` feed silent at once | [source-silent](runbooks/source-silent.md) |
| `builder-code-disabled` | our builder code is `disabled`/`unknown` | [builder-code-disabled](runbooks/builder-code-disabled.md) |
| `deposit-stuck` | a deposit seen and uncredited for 15 min | [stuck-bridge](runbooks/stuck-bridge.md) |
| `synthetic-order-failed` | three consecutive probe misses | [order-path-degradation](runbooks/order-path-degradation.md) |
| `deploy-verification-failed` | the post-deploy smoke watch fails | [rollback](runbooks/rollback.md) |

**SEV2 — page in hours:** one feed lagging its own threshold, venue `THROTTLED` rejections, our own rate budget
above 80% used, elevated rejections, end-to-end p99 over 8 s, the wallet provider refusing, withdrawals in flight
for 15 min, Telegram delivery failing, a growing queue, replication lag, cache age.

**SEV3 — ticket:** the slow p99 tail that nobody is woken for, disk filling, the cost projection crossing a
budget gate, and a dependency advisory. Tickets exist so that the *trend* is looked at while it is still a trend.

## The synthetic order is the one that proves the product works

`tools/p15-synthetic.py` places the cheapest order the system will accept, from **outside the fleet**, through the
same public edge a user's session uses, every 60 seconds. It is deliberately not admin-authenticated: an admin
token would still work while user auth is broken, which is precisely the failure it exists to catch.

Its failure policy matters more than the probe: one miss is recorded and not paged (a pager that fires on one lost
packet gets muted), three consecutive misses page, and a **rejection is not a failure** — the probe's job is the
path, not the outcome. Rejections are counted separately because a run of them is a signal about the venue or the
market rather than about us.

## How the numbers are produced, and what "unknown" means

`tools/p15-alerts.py` evaluates the registry against a sourced payload: `metrics` (the same single
`/v1/admin/metrics` read the dashboards render — so a red block on screen and a page on the phone cannot
disagree), `synthetic` (the probe's own state file), `budgets` (our declared venue buckets in
`services/ingest/net.py` against the counters we have actually spent in `rate_counters`), `host` (disk and, where
available, replication lag and cache age) and `tool` (another tool's exit code).

An alarm is **fired**, **quiet**, or **unknown**, and unknown exits 2 and is printed loudly. It is not silence: the
classic monitoring failure is a renamed field that turns a page into a shrug, and a collection that cannot reach
its source (no Redis on this box, no `rate_counters` table in a fresh checkout) must never render as green.

## Bringing up a new alarm

1. Write the runbook first, with its front matter naming the alarm id.
2. Add the rule to `ops/alerts.yaml` with `why`, `owner`, `route` and `expected_pages_per_week`.
3. `python3 tools/p15-alerts.py --check` — links, owner, budget, and the metric path against the sample payload.
4. `python3 tools/p15-alerts.py --from docs/verification/p15-metrics-sample.json --all --notify` — see the
   notification text, which is what the on-call will actually read.
5. Fire it deliberately in staging and record it:
   `python3 tools/p15-alerts.py --record <id> --evidence "staging, INC-1, notification verified in @polygm-ops"`.
   The D9 readiness checklist reads that file, so "we tested the alerts" is a list of ids and times rather than a
   sentence.

## What this does not prove

The delivery path (Telegram bot → the on-call's phone) is owner-gated: it needs `PGM_TELEGRAM_BOT_TOKEN` and a real
phone. Until that token exists, every notification in this phase is verified as *text* and not as a *delivery*, and
that distinction is stated here rather than smoothed over. The alerting design is complete; its last mile is a
credential.
