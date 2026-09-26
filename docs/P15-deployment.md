# P15 — deployment, observability and operations

**The SRE target, in the kit's words:** *at 2am, one page, from a phone, in under five minutes — is the system
healthy, is any user's money inconsistent, should I stop trading?*

That sentence is the acceptance test for everything below. A dashboard nobody can read at 2am fails it. An alert
without an owner fails it. A rollback that needs a laptop fails it. The budget is the second constraint: **under
$10k of total spend, under $300/month of infrastructure**. Every decision in D2 is written against that ceiling,
and the ones that would break it are written down with the trigger that forces them.

| Deliverable | State |
|---|---|
| D1 environments table, real-vs-faked, access, promotion path, exact config diff | **done** — `docs/P15-environments.md` (generated), `config/environments.json` |
| D2 costed infrastructure, terraform committed, executor isolation | **done, static** — `infra/terraform/**`, `tools/p15-isolation-check.py`; runtime probe is an owner step |
| D3 CI/CD: the manual gate, expand-contract, rollback, drain, flags, secrets, deploy audit | next |
| D4 the four observability pillars | next |
| D5 alerting and the page budget | next |
| D6 fourteen runbooks | next |
| D7 disaster recovery with tested restores | next |
| D8 cost attribution and the cut order | next |
| D9 the signed readiness checklist | last, and it will not be signed until D4–D8 are true |

---

## D1 — environments

The kit's warning is the reason this is a *generated* document: *"a staging environment that differs from prod in
ways nobody has written down is how you get a production incident on deploy day."*

| | local | preview | staging | canary | prod |
|---|---|---|---|---|---|
| **Purpose** | development | one per PR, ephemeral | pre-prod rehearsal | our $50, internal accounts | other people's money |
| **Real vs faked** | everything faked: mock venue, seeded fixtures | market data real, orders mocked | market data and tape **real**; every order goes to the mock executor | everything real, including money | everything real |
| **Database** | SQLite (`var/polygm.db`) | Postgres branch per PR, fixtures only | Postgres branch, prod schema | prod database, separate accounts | prod database |
| **Who has access** | each engineer on their own laptop | the CI bot (creates/destroys) + any org member with Vercel project access | any engineer; credentials are read-only to the venue | **one** operator (the founder) + the release gate | break-glass: two SSH keys, every application write goes through the API and is audited |
| **Cost** | $0 | ~$0 (branch compute while it lives) | ~$10/month | shares prod | ~$120–140/month at launch |
| **Promotion path** | PR → preview is automatic | merge to `main` → staging is automatic | staging green + smoke → **manual gate** → canary | canary green for 1 h of synthetic orders → prod | — |

**The exact config diff** is not a table somebody maintains by hand. `config/environments.json` is the matrix the
deploy reads, `tools/p15-env-diff.py` is what turns it into the document, and the tool **refuses to pass while any
difference between two environments has no written reason** — "it's staging" is not a reason; "the mock executor,
because staging must never hold a real order" is. Today it reports **57 cross-environment differences, all with a
reason, and 18 checks passing** (the full list is in `docs/P15-environments.md`).

Two of those checks are the ones that would have caught a real incident:

* every deployed environment sets **`PGM_REQUIRE_SECURITY_ENV=1`** — P14's F9 was exactly the opposite: a deployed
  API running the development identity shape because an environment variable was absent, and nothing anywhere said
  it must be present;
* **`PGM_MOCK_CLOB_URL` is empty wherever the transport is live** — a mock endpoint pointed at from production is a
  misroute waiting for a typo.

The promotion path in one line each:

1. **PR → preview.** Automatic. Runs migrations up and down against a throwaway branch, then boots. A migration that
   only works on SQLite cannot pass here, because preview runs Postgres.
2. **Merge → staging.** Automatic on `main`. Full stack, real market data, mock executor. The smoke suite plus the
   P13 rehearsal run against it.
3. **Staging → canary.** Manual, and *required* for anything touching the executor. The rule the kit states and we
   adopt literally: **the money path never auto-promotes.**
4. **Canary → prod.** One hour of synthetic paper orders plus real orders from internal accounts, at most $50 of
   our money, with the reconciliation dashboard watched. Then a one-command promotion.

---

## D2 — infrastructure, costed

### The topology

```
                    ┌──────────────┐
   Telegram ───────▶│  Vercel      │  polygm-mini-app.vercel.app  (own project, own URL — the owner's instruction)
                    │  Mini App    │
                    └──────┬───────┘
                           │ HTTPS (fetch, CORS allowlisted)
   browsers ──────────────▶│
                    ┌──────▼───────┐        proxied DNS        ┌───────────────────┐
                    │ Cloudflare   │──────  api.‹domain›  ─────▶│ Hetzner ‹fsn1›    │
                    │ DNS/CDN/WAF  │   (TLS terminated by Caddy) │ polygm-api-1      │
                    └──────────────┘                             │ CPX32 4vCPU/8GB   │
                                                                 │ api · ingest ·    │
                                                                 │ Caddy · pgbouncer │
                                                                 └───┬──────────┬────┘
                                                                     │          │ internal network only
                                       ┌─────────────────────────────▼──┐       │ (no inbound rules at all)
                                       │ Hetzner polygm-executor-1      │◀──────┘ port 8443 wake-up
                                       │ CPX22 2vCPU/4GB               │
                                       │ executor · signer · reconciler│
                                       └────────────┬──────────────────┘
                                                    │ 443 to the venue + wallet provider only
                                       ┌────────────▼────────────┐   ┌──────────────────┐
                                       │ Neon Postgres (managed) │   │ Upstash Redis    │
                                       │ aws-eu-central-1, 1 CU  │   │ (rate limits,    │
                                       │ allowed_ips = the two ☝ │   │  locks, budget)  │
                                       └─────────────────────────┘   └──────────────────┘
                                                    │ nightly dump (age-encrypted)
                                       ┌────────────▼────────────┐
                                       │ Cloudflare R2 backups   │
                                       └─────────────────────────┘
```

### Compute at launch, and at 10×

| Component | Launch (≈5k DAU, 200 tracked markets) | Cost/month | At 10× (≈50k DAU, 2,000 markets) | Cost/month |
|---|---|---|---|---|
| API + ingest + Caddy + pgbouncer | 1 × Hetzner **CPX32** (4 vCPU/8 GB, FSN1) | ~$12 | 3 × CPX32 behind Hetzner LB11 | ~$48 |
| Executor + signer + reconciler | 1 × **CPX22** (2 vCPU/4 GB) | ~$9¹ | 2 × CPX22, partitioned by wallet | ~$18 |
| Postgres | Neon Launch, 1 CU·730 h, autoscale → 4 CU | ~$80² | 2 CU + 1 CU analytics replica | ~$167 |
| Cache / queue / locks | Upstash PAYG (~5–15 M commands) | ~$10–30 | self-hosted Valkey on a CPX22 (Upstash would be ~$80) | ~$9 |
| Object storage | Cloudflare R2, ~30 GB of backups | ~$1 | R2, ~120 GB | ~$2 |
| TLS / CDN / WAF / DNS | Cloudflare free tier + Caddy (automatic certs) | $0 | same | $0 |
| Errors + dashboards | Sentry (free tier covers launch) + Grafana Cloud free tier | $0 | Sentry team | ~$26 |
| **Total** | | **~$119 (range $109–129)** | | **~$270** |

The arithmetic behind this table is `config/costs.json` — one file, with every line's unit and where its price
came from — and `tools/p15-cost.py --check` fails if this table, that file and the Terraform output disagree. As
of this phase it projects **$119.38/month** point estimate (range $109–129 by line), against the kit's $300
envelope: 39.8% used, $180.62 of headroom. `docs/P15-cost.md` is the fuller treatment (attribution, the 50/80/100%
gates, the scaling plan and the cut order).

¹ The EU CPX line rises on **2026-04-01** (CPX22 €5.99 → €7.99), so this column budgets the post-rise price. Hetzner
EU and US pricing diverge sharply — the US CPX21 is ~$37/month against €5.99 in Falkenstein — which is why the
region is `fsn1` and why the 10× plan scales *out* in Europe rather than out to a US region.

² The honest number, not the optimistic one: with `suspend_timeout_seconds = 0` (production must never suspend — a
cold connection on the money path is an outage) the floor is what the instance actually bills. At 0.25 CU the floor
is $19.35/month, but a trading API's connection pool keeps compute hot, so **budget 1 CU always-on: 730 h ×
$0.106 = $77.38**. The 0.25 CU floor is a ceiling on the savings, not a forecast.

**What breaks first at 10×, and what we do about it.** Not CPU, and not the tape: the **Postgres line**. Left at
autoscale-ceiling 4 CU always-on it is $309/month *by itself*, which breaks the $300 ceiling with nothing else
attached. The order of moves is therefore written down before it is needed:

1. Cap Postgres at 2 CU + one 1 CU replica for analytics (−$155 vs 4 CU) — this is the 10× column above.
2. Self-host the database as a CloudNativePG pair on 2 × CCX33 (8 vCPU/32 GB, €46 each) ≈ $100/month, keeping the
   managed instance as a logical replica until a restore drill has been run on the pair. That is the real 10× move,
   and it trades $67/month for an operator burden — only worth it once the restore drill passes twice.
3. Cut analytics retention before touching either (D8's first cut).

### Postgres: managed or self-hosted

The kit asks for the comparison *with failure modes*, so here it is with the failures, not the features.

| Failure | Managed (Neon) | Self-hosted (Hetzner + CloudNativePG) |
|---|---|---|
| Instance or node loss | provider fails over inside the region in ~30–60 s; we do nothing | CNPG promotes a replica in seconds; needs a second host, a witness, and knowledge that only exists if somebody wrote it down |
| Data corruption | PITR to any second in the last 7 days; the restore is a branch, not a procedure | PITR works if WAL archiving has been *restoring* — not just running — since the day it was enabled |
| Accidental `DELETE` | branch at `t-1` and copy the rows back | same, conditional on the above |
| Region/control-plane outage | we are down for the duration; DR is the nightly dump restored in another region (D7 RTO 4 h) | we are down only if the host is; the failure is ours to fix at 2am |
| Cost surprise | autoscale ceiling is explicit in the plan, plus 50/80/100% budget alerts (D8) | the bills are flat and boring |
| The real cost | $77/month | 4–8 engineer-hours/month and **one 3am restore nobody has rehearsed** |

**Decision: managed at launch.** The deciding argument is not the $77, it is that we have **one** engineer and the
kit's own failure-mode list ends with "a restore nobody has rehearsed". A managed database replaces the part of the
system we cannot rehearse cheaply: the failover. Revisit at 10× (move 2 above), and never mid-incident.

**Restricted access either way:** the Neon project sets `allowed_ips` to the two hosts' addresses at creation, so
the database is not reachable from the operator's laptop or from anywhere else. Admin work goes through an SSH
tunnel from the API host, which is auditable. This is the same control P14's F14 asked Supabase for — applied here
as a default rather than as a finding.

### The analytics tape: ClickHouse or Postgres?

The kit names the arithmetic: **~21 fills/s, 10× during news, 90-day retention**, and asks for the comparison
against it. So, first the arithmetic — and the most important line in it is what we choose *not* to store.

| Stream | Rate (baseline) | Rows/day | Rows/90 d | Compressed bytes/row | 90-day size |
|---|---|---|---|---|---|
| Public fills tape, every trade on tracked markets | 21/s | 1.81 M | 163 M | 18 B | **2.9 GB** |
| Book snapshots, sampled 1 per market per 10 s × 2,000 markets | 200/s | 17.3 M | 1.56 B | 12 B | **18.7 GB** |
| 1-minute candles per market | 33/s | 2.88 M | 259 M | 24 B | **6.2 GB** |
| Our own order lifecycle (intents, attempts, acks, reconciliations) | 0.5/s | 43 k | 3.9 M | 120 B | **0.5 GB** |
| Ops metrics (300 series × 15 s scrape) | 20/s | 1.73 M | 156 M | 20 B | **3.1 GB** |
| **Total** | **~275 rows/s** | **~21 M** | **~1.9 B** | | **~31 GB** |

Peak — a news window with the tape at 10× — is **~2,750 rows/s**, sustained for minutes at a time. Note what that
table is *not*: storing every L2 book update would be ~10,000 rows/s (≈ 400× the fills tape) and is the single
decision that would force a columnar store. **We sample.** That is the design choice the whole cost argument rests
on, and it is written here so that a future engineer adding a full-depth recorder knows they are changing it.

| | Postgres with monthly partitions (launch) | Self-hosted ClickHouse | ClickHouse Cloud Basic | TimescaleDB |
|---|---|---|---|---|
| 90-day fit | 31 GB fits; Neon storage $0.35/GB-month → **$11/month** | 40 GB volume €1.76 → **~$11/month all-in** (CPX22 + volume) | 8 GiB/2 vCPU always-on: 730 h × $0.2181 = **$159/month** + $25.30/TB-month | on Neon, the extension exists but compression and the hypertable feature set are limited; the provider's own advice is native partitioning + `pg_partman` |
| Write path | 275 rows/s average is unremarkable for Postgres; 2,750/s bursts are absorbed by batching | built for this | built for this | good |
| Analytical scans | can be isolated on a **read replica** at 0.25 CU ($19/month) so a leaderboard verification never shares CPU with order writes | excellent | excellent | good |
| New operational surface | **none** — it is the database we already run, and it is already backed up | a second store to back up, upgrade and monitor; a second consistency story | managed, so mostly none | one more extension to pin |
| Verdict | **launch here** | the move when the triggers below fire | over budget for this decision: 53% of the monthly ceiling for the tape's size | not worth a third vendor for one table family |

**Decision: partitioned Postgres at launch, and the move to ClickHouse is triggered by a number, not a feeling.**

* analytics-replica CPU above 50% for 7 consecutive days, **or**
* leaderboard-verification p95 above 2 s, **or**
* stored tape above **250 GB per 90 days**, **or**
* we start storing full L2 (which by itself makes every other number in the table moot).

When it fires, the migration is a self-hosted ClickHouse on a CPX22 + 40 GB volume (~$11/month) fed from the same
writer behind a flag, with the Postgres partitions kept as the system of record until a reconciliation report says
the two agree for a full week. The tape tables are append-only with a stable schema precisely so this stays a
weekend of work rather than a rewrite.

### Cache, queue and object storage

| Need | Choice | Why, and when it changes |
|---|---|---|
| Rate-limit buckets, locks, budget counters, the kill switch's fast path | **Upstash Redis**, pay-as-you-go, ~5–15 M commands at launch | serverless and multi-AZ without an operator; **alert at 40 M commands (~$80/month)**, because that is the point where a €7.99 self-hosted Valkey pays for itself — D8's cut order |
| Order-intent queue and the executor's wake-up | **Postgres** (`order_intents`, `SKIP LOCKED`) — no separate broker | the queue must be transactional with the money rows: an intent that exists in a broker but not in the database is the ambiguous order P6 D3 is designed around. A broker buys throughput we do not need and adds a place where an order can exist without a reconciliation trail |
| Backups, exports, audit archive, 90-day metrics export | **Cloudflare R2** | $0.015/GB-month and **zero egress**, which matters exactly once: the day a restore pulls a full dump down. `PGM_BACKUP_TARGET` is per environment, so restoring staging can never overwrite production's history |

### Executor isolation — three layers, and what is verified

The kit wants the executor on an isolated network with no ingress and an allowlisted egress, **tested**. There are
three places the claim can be true or false, and each fails differently, so `tools/p15-isolation-check.py` checks
all three and today reports **23 checks passing** (`docs/verification/P15-isolation.txt`):

1. **Platform firewall** (`infra/terraform/network.tf`) — `hcloud_firewall.executor` declares **no inbound rule at
   all**, and its egress is limited by port to 53 (pinned resolvers), 443 (venue + wallet provider), 5432 (database)
   and 8443 (the API's wake-up on the private network). The check parses the resource block brace-matched, so a rule
   added three lines down cannot slip past it.
2. **Host rules** (`cloud-init/executor.yaml`) — `iptables -P OUTPUT DROP` first, then named destinations including
   the database host read from the environment; `ufw default deny incoming`; failures logged with the prefix
   `polygm-egress-drop` so the runtime probe has evidence; and the whole allowlist **re-resolved every 60 s** so a
   vendor that moves an edge cannot look like silence from the executor.
3. **The container** (`docker-compose.prod.yml`) — the executor publishes **no ports**, is attached to the
   `internal` network **only** (a bridge with no route off the host), and the API is the single service bridging
   `internal` and `edge`. The check parses the compose file as YAML and reads `ports`/`expose`/`networks` from the
   service definition rather than grepping the text.

Both layers were canaried rather than trusted: adding one `direction = "in"` rule and one `ports: ["8091:8091"]`
turns the check red with exactly the right two reasons (and a first canary caught this harness matching `0.0.0.0/0`
inside its own prose comment — a check that fails on correct behaviour is a check somebody switches off, so comments
are stripped before any value is matched).

**What a file cannot prove, and is therefore still open:** the runtime probe from inside the subnet. The exact
commands are printed by the harness and recorded in `docs/verification/P15-isolation.txt`:

```bash
docker compose -f /srv/polygm/docker-compose.prod.yml exec executor sh -c \
  'for h in example.com github.com api.openai.com; do curl -s -m 3 -o /dev/null -w "$h %{http_code}\n" https://$h || echo "$h BLOCKED"; done'
# expected: every line BLOCKED, and grep polygm-egress-drop /var/log/kern.log shows the attempts.
# then the inverse: curl to clob.polymarket.com must answer 200.
```

Until that runs on the real hosts, D2's isolation row is **PASS (static) / PENDING (runtime)** — the same shape as
P14 D4's deployed-subnet egress probe, and it is listed as such in the P15 worklist. "The diagram says so" is not an
acceptable final state, which is exactly why the check exists rather than a paragraph.

### DNS, TLS, CDN

* **CDN/WAF/DDoS/DNS: Cloudflare free tier.** The API's DNS record is **proxied** (orange cloud), so the origin sees
  Cloudflare's edge rather than the public internet, and the certificate is not our problem.
* **TLS at the origin: Caddy**, automatic certificates, HSTS + `nosniff` + `Referrer-Policy` set at the edge of the
  app. The public host name is a **deploy-time knob** (`POLYGM_API_HOST`) rather than a hard-coded string.
* **The domain is an owner step, and the honest status is "not yet".** P02 found `openout.app` **live and owned by
  an unrelated link-in-bio product**, so this phase does not pretend we have it. The Terraform default is
  `polygm.trade`; on 2026-09-23 it had no A/AAAA record (the same first-pass filter P02 used — which is *not* proof
  of availability, since registration status is a paid search). Until the name is registered and delegated:
  production is reachable at the Hetzner origin's address and the Mini App at `polygm-mini-app.vercel.app`, and
  `deploy/Caddyfile` serves whatever `POLYGM_API_HOST` says.
* **The Mini App keeps its own project and its own URL** (the owner's instruction), with `trade.‹domain›` as a CNAME
  to `cname.vercel-dns.com` in `infra/terraform/dns.tf`, and the API's CORS allowlist naming that origin exactly
  rather than `*`.
* **No public name for the executor** — `dns.tf` contains the record it deliberately does *not* create, as a record
  of the decision.

### Infrastructure as code

```
infra/terraform/
├── versions.tf        providers (hcloud, cloudflare, neon), remote state in R2, version pins
├── variables.tf       every secret is a variable with no default; domain, region, host sizes
├── network.tf         the two-plane network + the firewalls (the executor's has no inbound rules at all)
├── compute.tf         api + executor servers, cloud-init, no key material on the API host
├── database.tf        Neon project: 1–4 CU autoscale, 7-day PITR, allowed_ips = the two hosts
├── storage.tf         R2 backup bucket (age-encrypted dumps written nightly)
├── dns.tf             proxied API record, Mini App CNAME, and the executor record that does not exist
├── outputs.tf         addresses + the expected monthly cost, printed at apply time
├── terraform.tfvars.example
└── cloud-init/        api.yaml, executor.yaml (package list, docker daemon hardening, the egress script + timer)
```

**No console-clicked infrastructure.** `make infra-check` (which CI runs on every PR touching `infra/`) is:
`fmt -check` → `init -backend=false` → `validate` → the environment matrix. Today: **formatting clean, configuration
valid, 18/18 environment checks.** Both canaries in this phase produced the right failures, not silent passes.

What is **owner-run, and marked as such**: `init` against the real backend, `plan` and `apply` — they need Hetzner,
Cloudflare and Neon credentials that do not exist in the build environment, and a plan run against no account would
be a green tick that proves nothing. Terraform **1.9.8** was used for the validation recorded here, and the CI workflow pins the same
version so "valid" means the same thing in both places.

Two honest caveats:

* **State locking.** Terraform 1.9 has no `use_lockfile` for this backend, so applies are serialized by the deploy
  job's `concurrency` group instead. Terraform ≥1.10 adds `use_lockfile = true` and adopting it is a one-line
  follow-up, recorded rather than assumed.
* **Image digests.** `deploy/image-digests.txt` is deliberately still empty — the build environment has no container
  daemon, so any digest written here would be invented. D3's build job writes it from the images it actually
  produced, and `tools/p07-gate-check.py` already refuses to let the two drift.

### What I could not verify, stated plainly

| Claim | Status |
|---|---|
| Hetzner EU CPX prices, and the 2026-04-01 rise | from published pricing pages during this phase; the CPX32 figure is an interpolation and is marked `[UNVERIFIED exact]` — `terraform plan` plus the cost output is the confirmation |
| Neon Launch rates ($0.106/CU-h, $0.35/GB-month, 0.25 CU floor) | published; the always-on arithmetic is ours and shown above |
| ClickHouse Cloud Basic $0.2181/CU-h, $25.30/TB-month | published, cross-checked against two independent teardowns that agree |
| Upstash $0.20/100k commands, $10/250 MB | published; "verified 2026-09-06" in the source |
| The executor really is unreachable and its egress really is closed | **static checks pass, runtime probe pending** — owner step, commands above |
| `polygm.trade` is registrable | **no A/AAAA record on 2026-09-23**; that is a filter, not proof — registration is an owner step |

---

## The quality gate, demonstrated

The kit's gate for P15 is a sentence, not a checklist: *"It is 2am. You get one page. From a phone, in under five
minutes, you can say: is the system healthy, is any user's money in an inconsistent state, and should I stop
trading? Demonstrate it."*

`make p15-2am` performs that demonstration against the real components and records it in
`docs/verification/P15-2am-drill.txt`. It writes one `orphan` — an order at the venue we cannot map to a user, ten
minutes old, past the registry's 60-second `for_ms` — into a scratch copy of the seeded database, leaving the rest
of the world healthy (`executor=live`, feeds fresh): **one page, one problem**. It then reads `/v1/admin/metrics`
through the app, evaluates `ops/alerts.yaml` with the same engine the dashboards import, renders the notification
with the same function the alert drill records, opens the runbook *that notification links to* and reads it, and
renders the on-call dashboard from the same payload. Eight checks, all of which can fail:

| check | what it refuses to accept |
|---|---|
| c1 | a rule that is not `SEV1`/`page_now` — the 2am gate is about the page, not the ticket |
| c2 | a page that does not carry its own summary, its reason, and its owner's name |
| c3 | a dashboard whose money number disagrees with the number that fired the alarm (the read is on the page's *text*, not its markup — the first version searched for `>1 <` and failed against a page that does carry the number) |
| c4 | a page without the kill-switch state, or a runbook with no remediation section: the third answer has to be in the page's own instructions |
| c5 | a runbook link that does not resolve, or resolves to something other than the page the registry names |
| c6 | a phone page that reaches out to the network, has no viewport, is over 64 KB, or lacks the money/order-path/freshness sections |
| c7 | a triage that took longer than five minutes |
| c8 | any firing alarm without an owner and an existing runbook |

`--self-test` plants ten failures in memory — a notification with no runbook link, a runbook that was renamed, an
owner-less alarm, a dashboard rendered from a drifted payload, a page with an external `<script>`, a page with no
viewport, a dropped kill-switch line, an eleven-minute triage, a summary the page does not carry, and a quiet rule
where a paging one was promised — and requires the owning check to go red for each. **It refuses to plant a single
canary until the unmutated drill is green**, because the c3 canary spent one run "passing" against a baseline where
c3 was already failing: a canary that goes red for the wrong reason proves nothing.

What the recorded run says (`2026-09-26`, local, seeded): triage **0.9 s** of a 300 s budget, the on-call page
**8.2 KB**, **1** alarm firing of 25 registered, page verbatim, the three answers with their numbers, and the
runbook line that answers the third one. What it does **not** say: that the page reached a phone. Delivery needs
`PGM_TELEGRAM_BOT_TOKEN` and a device, the transcript says so in its own header, and it stays an owner step below.

---

## Owner steps this phase has surfaced

1. **Register the domain** (default `polygm.trade`) and delegate it to Cloudflare, then set `POLYGM_API_HOST` and
   `POLYGM_WEB_ORIGIN` at deploy time.
2. **Provider credentials** for `terraform apply`: Hetzner (project-scoped), Cloudflare, Neon — plus an R2 bucket for
   state. Until they exist, D2 stays "validated, not applied".
3. **Run the runtime isolation probe** on the real hosts and attach the transcript to
   `docs/verification/P15-isolation.txt` (the harness prints the exact commands).
4. **Run the alert drill on a real staging box** (`make p15-alerts-drill`) and then verify *delivery*: the drill
   proves each rule fires and records the notification text, but reaching a phone needs `PGM_TELEGRAM_BOT_TOKEN`.
5. **Review the three dashboards on a phone** (`tools/p15-dashboards.py --out …`), and record the review in
   `docs/verification/p15-dashboard-review.jsonl` — the readiness checklist reads that file.
6. **Run each runbook once, in staging, by someone other than its author**, and set `drilled_by` in the page's front
   matter. Eighteen pages; `tools/p15-runbooks-check.py` refuses a page whose drill is over ninety days old.
7. **Throw the kill switch from a real phone**, and let `kill_switch_drills` record the propagation measurement.
8. **Run the restore drill against the production transport** (`pg_dump → age → R2 → restore`) — the local drill and
   its verification are recorded, and `docs/P15-dr.md` marks the transport `[UNVERIFIED]` until this happens.
9. **Record one real month of cost** with `tools/p15-cost.py --actual <bill>` so the projection has a fact beside it.
10. **Staff the on-call rotation for 30 days** (`docs/P15-oncall.md`): the readiness tool expects names and dates, and
   staffing is a decision this repository cannot make.
11. Still open from P14 and unchanged by this phase: GitHub 2FA, the Supabase CIDR restriction, the Turnkey provider
   rate, `PGM_TELEGRAM_BOT_TOKEN`, the BotFather Mini App URL, real-phone acceptance, and the $50/72 h canary which
   stays blocked while the P14 gate reads NO-GO.
