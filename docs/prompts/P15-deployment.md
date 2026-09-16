# P15 — Deployment, Observability & Operations

> Paste `00-SHARED-CONTEXT.md` and the P4/P13/P14 outputs first, then this.

## Role
You are an SRE who runs a system where other people's money moves. You optimise for one thing: the ability to know something is wrong before a user tells you, and to stop it fast.

## Objective
Build the deployment pipeline, environments, observability, alerting, and operational runbooks. Target: **one engineer, on a phone, can tell whether the system is healthy and can stop trading in under 30 seconds.**

## Budget constraint
Under $10k total, so under ~$300/month of infrastructure at launch. Design for that. Every recommendation carries a monthly cost.

---

## Deliverables

### D1. Environments
| Env | Purpose | Data | Cost |
|---|---|---|---|
| local | dev, `docker compose up` | mock + fixtures | $0 |
| preview | per-PR, ephemeral | mock | ~$0 |
| staging | pre-prod, **real Polymarket APIs, mock executor** | real market data, fake money | ~$60/mo |
| canary | real money, our $50, internal users only | real | shared with prod |
| prod | live | real | ~$150–250/mo |

Specify for each: what is real, what is faked, who can access it, how it is promoted from, and the exact list of configuration differences. **A staging environment that differs from prod in ways nobody has written down is how you get a production incident on deploy day.**

### D2. Infrastructure — concrete and costed
Recommend and justify:
- Compute: Hetzner (cheapest sane option) vs Fly.io vs Railway vs AWS. Show the monthly cost for the launch topology and for the 10× topology.
- Postgres: managed (Neon/Supabase/RDS) vs self-hosted with backups. State the failure mode of each.
- ClickHouse: ClickHouse Cloud free/dev tier vs TimescaleDB on the same Postgres. Given the tape volume (~21 fills/sec sustained, 10× during news), do the arithmetic on 90-day retention and say which fits the budget.
- Redis, queue, object storage
- The executor's **isolated network** — no ingress, egress allowlisted to the CLOB, the wallet provider, and the DB. Specify how this is enforced in the chosen platform, and how you test it.
- DNS, TLS (automatic), CDN for static assets

Provide `terraform` or `pulumi` (pick one) for the whole thing, committed to the repo. No console-clicked infrastructure.

### D3. CI/CD
- Pipeline: lint → types → unit → contract → component → build → integration → E2E → deploy-staging → smoke → **manual gate** → canary → prod
- **The manual gate for anything that touches the executor.** Data-layer and UI changes can auto-promote; the money path cannot.
- Migration strategy: expand-contract only, never a breaking migration in the same deploy as the code that depends on it, always backward-compatible for one release
- Rollback: one command, under 60 seconds, tested monthly. **Specify what rollback does to in-flight orders** — this is not a normal web app rollback.
- Zero-downtime deploy for the API (drain, wait for in-flight, replace). **The executor must NOT be replaced while it may hold an ambiguous order** — specify the drain protocol and how it interacts with the P6 D3 reconciliation.
- Feature flags for anything risky, with instant off
- Config and secret management: injected at deploy, never baked into images, rotated on a schedule
- Deploy audit: who, what, when, from which commit, with a link to the diff

### D4. Observability — the four things that matter
Everything else is nice. These four must be perfect:

**a) Money correctness**
- `unreconciled_orders` — **alarm when non-zero for >60s.** This is the single most important metric in the system.
- `orders_unknown_state` — same treatment
- `position_drift` — our DB vs on-chain, sampled
- `fee_estimate_delta` — our estimate vs actual, trending (if it drifts, users get surprised)
- `builder_attribution_volume` and `builder_expected_fees` vs `builder_actual_fees` — **this is our revenue; if we cannot measure it independently of Polymarket's dashboard we are flying blind**

**b) Order path health**
- Latency p50/p95/p99 per hop: intent → risk → sign → submit → ack
- Rejection rate by reason code (a spike in one code means something upstream changed)
- Rate-limit rejections from the CLOB, and our own remaining budget per bucket
- **Kill-switch state**, surfaced on every dashboard and in every alert

**c) Data freshness**
- Per-source lag: `now − newest_event_ts`, per source, with an alarm threshold
- WebSocket connection state and resync count per consumer
- **Silent-death detection**: a socket that stays open but stops sending is the classic failure. Heartbeat per source, alarm on last-message-age.
- Cache hit rate and cache age

**d) Business**
- Attributed volume, active traders, deposits, withdrawals, Pro conversions, alert→trade conversion
- **Withdrawal anomaly detection:** a spike in withdrawals is the earliest signal of a trust event or a breach. Alarm on it.

Dashboards: one for on-call (health, money, freshness), one for product (business metrics), one for revenue (builder attribution). Every dashboard loads in under 3 seconds and works on a phone.

### D5. Alerting — few, loud, actionable
Alarm classes with severity, route, and the runbook link:
- **SEV1 (page immediately):** unreconciled orders, kill switch engaged unexpectedly, executor down, key-compromise indicator, withdrawal spike, all WebSocket sources dead, builder code disabled
- **SEV2 (page in hours):** one source lagging, risk service degraded, elevated rejection rate, database replication lag, cache age above threshold
- **SEV3 (ticket):** cost anomaly, disk filling, dependency CVE, degraded p99

Rules:
- Every alarm has a runbook link in the notification. An alarm without a runbook gets deleted.
- Every alarm has an owner.
- Alert fatigue budget: **fewer than 3 pages per week** or the thresholds are wrong and people will start ignoring them.
- Synthetic check: an external probe that places a paper order end-to-end every 60 seconds and alarms if it fails. **This is the check that catches "everything looks green but the product is broken."**

### D6. Runbooks — write them, do not list them
Each runbook: symptoms, diagnosis steps with exact commands/queries, remediation, verification, and who to escalate to.
1. Unreconciled orders
2. Executor down / crash loop
3. WebSocket source silent
4. Upstream Polymarket schema change (this **will** happen — V1→V2 broke every bot on the platform in April)
5. Upstream rate-limit ban (our IP got throttled)
6. Wallet provider outage
7. Builder code disabled
8. Kill switch activation and deactivation
9. Database failover
10. Suspected key compromise (cross-ref P14 D2)
11. Telegram bot restricted or banned
12. Stuck bridge / deposit not credited
13. Cost spike
14. Full rollback

### D7. Disaster recovery
- RPO and RTO stated per component. For the executor: **RPO zero** (no order intent may be lost) — specify how.
- Backup schedule, encryption, retention, and **the tested restore** with a recorded date and result
- Region failure: what survives, what does not, and the manual procedure
- The scenario where the wallet provider is gone: can users still get their funds out? **If the answer is no, that is a design defect, not a DR gap.**
- Annual DR test, with results

### D8. Cost control
- Per-service cost attribution
- Budget alerts at 50/80/100% of the monthly envelope
- The scaling plan: what we add at 1k users, 10k users, 100k users — with cost at each
- What we cut first when money runs out (there is a right answer: drop ClickHouse retention, drop the long-tail market tracking, keep the executor and the money path untouched)

### D9. Operational readiness review
A signed checklist before launch:
- [ ] Every dashboard reviewed by the person who will be on call
- [ ] Every alarm fired deliberately in staging and the notification verified
- [ ] Every runbook executed once, in staging, by someone other than its author
- [ ] Rollback tested, timed
- [ ] Synthetic order check running and alarming correctly
- [ ] On-call rotation staffed for 30 days
- [ ] Kill switch drilled from a phone
- [ ] Cost dashboard showing actual spend

---

## Constraints
- No infrastructure created by hand in a console.
- No alarm without a runbook and an owner.
- No deploy of the executor without the drain protocol.
- Under $300/month at launch.

## Quality gate
It is 2am. You get one page. From a phone, in under 5 minutes, you can say: is the system healthy, is any user's money in an inconsistent state, and should I stop trading? Demonstrate it.
