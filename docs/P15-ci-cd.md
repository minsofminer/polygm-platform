# P15 D3 — CI/CD, rollback, flags and the deploy audit

The phase asks for a pipeline, and for one property that is worth more than the pipeline: **the executor never
auto-promotes**. Everything below is arranged around that. A green build may reach staging on its own; the thing
that signs and submits orders reaches production only through a person, and only when the running system says it
is safe to replace.

Artefacts in this phase (all of them checked, not just written):

| what | where | how it is checked |
| --- | --- | --- |
| the pipeline | `.github/workflows/pipeline.yml` | `tools/p15-pipeline-check.py` (81 assertions) + its `--self-test` (8 planted breakages, each must trip a rule) |
| the deploy | `deploy/deploy.sh` | `tests/test_p15_pipeline.py` drives it: a tag is refused, no admin token is refused |
| the rollback | `deploy/rollback.sh` | same, plus the monthly drill in `docs/runbooks/rollback.md` |
| the drain guard | `tools/p15-drain-guard.py` | `tests/test_p15_drain_guard.py`: 13 tests over the four states, including **fail-closed** |
| the audit ledger | `tools/p15-deploy-audit.py` | hash-chain + commit existence, tamper test included |
| the money-path classifier | `tools/p15-money-path.py` | `tests/test_p15_pipeline.py`: fails towards the gate |
| the synthetic order | `tools/p15-synthetic.py` | 3 consecutive misses alert, 1 does not |
| the ops surfaces | `services/api/app.py` (`/v1/admin/drain-status`, `/flags`, `/metrics`) | `tests/test_p15_ops_api.py` (27 tests) |

## The pipeline, in order

```
lint → types → unit → contract → migrations → component → build → integration → E2E
     → deploy-staging → smoke → [MANUAL GATE] → canary → production     (rollback on production failure)
```

Each stage `needs:` the previous one, so the order is the *graph* rather than a convention, and the checker
asserts exactly that. `migrations` is ours, and it sits with the contract stage because a migration is a
contract change that happens to be SQL.

* **lint / types / unit / contract / component** are the same commands `make check` runs locally — one definition
  of green, so "green on CI" and "green on my laptop" are the same claim.
* **migrations** runs `tools/p15-migration-check.py --since origin/main`: expand-contract only, no destructive
  step, no lock on a big table, and the SQLite twin must be regenerated in the same commit.
* **build** writes `deploy/image-digests.txt` from the registry's own `RepoDigests` — a digest is a fact, a tag is
  a promise — and asserts the image runs as non-root with no `PGM_*`/`POLYGM_*`/`AWS_*`/`TURNKEY_*` environment
  baked in.
* **integration** brings the real stack up, migrates, seeds, and places an order through the mock executor.
* **E2E** runs the error-envelope demo over real sockets.
* **deploy-staging** and **smoke** are automatic: the deploy runs `deploy/deploy.sh`, then `tools/p15-smoke.py`
  watches `/v1/admin/metrics` for five minutes and fails the run on unreconciled work, an unknown-state order, a
  silent feed or an engaged kill switch.

## The manual gate, and why it is an environment

```yaml
manual-gate:
  needs: smoke
  environment: canary-approval   # required reviewers, configured on the environment in GitHub
```

A shell `read -p "promote?"` can be satisfied by a bot, by a `yes |`, or by whoever is holding the terminal. A
protected environment pauses the run, records the approver's name in the deployment history, and cannot be
self-approved by pushing a commit. The job then writes the approver's briefing into the run summary: the actor,
the commit, and the classifier's verdict.

`tools/p15-money-path.py` classifies the change set — `services/executor/**`, `packages/polygm_core/{risk,copy,
venue,wallets,ledger,reconcile,money}/**`, `db/migrations/**`, `services/api/app.py`, `contracts/openapi.yaml`,
`docker-compose.prod.yml`, `deploy/{venue-addresses.json,image-digests.txt}` — and it **fails towards the gate**:
an empty or un-derivable change set (a shallow clone, a fresh branch) returns `money-path`, because "I could not
tell" must never be the same answer as "it is safe".

Both canary and production sit behind the approval. The classifier decides what the approver has to *look at*,
not whether a human looks: a change that is not on the money path still gets a person, because the cost of one
approval is a minute and the cost of the alternative is a lesson about the difference between "not on the money
path" and "we did not notice".

## The executor drain protocol

The kit is specific: a deploy must not replace the process that may be holding an ambiguous order. That is not a
comment in a script, it is the exit code of a program:

```
$ tools/p15-drain-guard.py --api $HOST --token $TOKEN --timeout 300
safe: 3 open intent(s) queued (durable, not blocking), 0 in submitting, uncertain     → 0
waiting: 2 intent(s) in submitting, uncertain … TIMED OUT after 300s                  → 3
drain-status unreachable / refused                                                    → 2
```

* `pending`/`queued` intents are **durable rows in Postgres** — the next executor drains them — so they are
  reported and do not block. Blocking on them would mean never deploying during a busy market.
* `submitting`/`uncertain` **block**: those are the states where this process may be mid-conversation with the
  venue (the P6 D3 hazard, as two strings).
* Anything other than a clean answer is **fail-closed** — the check not running is never read as the check
  passing, which is the failure mode that turns a deploy into an incident.

`docker-compose.prod.yml` sets `stop_grace_period: 120s` on the executor for the same reason the guard exists:
the drain window has to outlast an in-flight order.

## Rollback: one command, under a minute, specified for orders in flight

```
deploy/rollback.sh --env prod --host https://api.example
```

`deploy.sh` copies `deploy/image-digests.txt` to `deploy/image-digests.previous.txt` *before* anything moves, so
"roll back" means one thing: run the previously recorded digests. The specification, per component:

| component | rollback behaviour | why |
| --- | --- | --- |
| **API** | replace freely | stateless apart from Postgres (sessions are rows); the previous image starts and serves |
| **executor** | only behind the drain guard | if it holds a `submitting`/`uncertain` order, replacing it with a version whose reconciliation differs is how one ambiguous order becomes two. The script rolls back the API, **stops**, and prints "run P6 D3 reconciliation, then re-run" |
| **migrations** | never rolled back by a file | no migration drops or rewrites a money column without a `-- CONTRACT:` marker and a window (`tools/p15-migration-check.py`), so the previous image runs against the current schema. If a contract step is what broke, the fix is a forward deploy, and `docs/runbooks/rollback.md` says so |
| **money** | never | a ledger row is a fact. A rollback that rewrites balances is a different, worse incident |

The script measures its own duration, records the number in the ledger, and prints a NOTE when it exceeds the
60-second budget — the monthly drill in `docs/runbooks/rollback.md` is what turns that into a fact rather than an
aspiration. The failure path is drilled in `tests/test_p15_pipeline.py` (a missing previous-digest file is a
refusal, not an empty success).

## Feature flags with instant off

`POST /v1/admin/flags {"name": "executor_draining", "value": true, "reason": "…"}`, admin-only, reason required
(4–400 characters, enforced by `set_flag` itself and by the route's `BAD_REASON`), unknown names are a **404**
rather than a silent create — *a typo that creates a switch nobody reads looks like a fix and is not one*.

The "instant" part is the point: the route calls `STORE.refresh()` (which zeroes the loader's TTL stamp) instead
of waiting out the 5-second flag TTL, so the change is live on the **next request**. `tests/test_p15_ops_api.py`
asserts that with the store otherwise warm. Every write lands in `flag_audit` with `changed_by`, `reason` and the
old and new values, and the audit assertion finds its own row by reason string rather than "the newest row",
because the newest row is a claim about test ordering rather than about the audit.

## Secrets: injected at deploy, never baked

* Images carry no `PGM_*` / `POLYGM_*` / `AWS_*` / `TURNKEY_*` environment (asserted in the build job by reading
  `docker inspect`'s `Config.Env`).
* The production stack reads `/etc/polygm/*.env`, mode 0600, written on the host from the platform's secret
  store; `docker-compose.prod.yml` names them with `env_file:` and the deploy job is the only writer.
* The pipeline's own credentials come from `${{ secrets.* }}`; `tools/p15-pipeline-check.py` fails the build on a
  secret-shaped literal in the workflow file.
* Rotation is the P14 D2 drill (`docs/P14-security-testing.md`), and the runbook for a suspected compromise is
  `docs/runbooks/key-compromise.md` — a flag flip plus a rotation, both with a recorded time.

## The deploy audit

`deploy/audit.jsonl` — one line per event, **hash-chained**: each entry carries the digest of the previous one,
plus actor, environment, commit, tree, the commit being replaced, the image digests, the migrations added, and
the outcome with the operator's own note. `tools/p15-deploy-audit.py verify` recomputes the chain, checks that
every recorded commit exists in this repository, and reports the first entry that does not add up.
`tests/test_p15_drain_guard.py` proves the properties that make it an audit rather than a diary: editing one
field fails verification, deleting an entry breaks the chain, and an entry claiming a commit this repository does
not have is a failure.

Two entries for one deploy (begin, finish) is deliberate: if the deploy dies half-way — which is the interesting
case — the ledger still shows an attempt that never finished, which is exactly what somebody reading it at 2am
needs to see.

## What is not verified here

The deploy jobs have never executed: this repository's build sandbox has no Docker daemon and no SSH to a
staging host. Everything up to and including `build` is what `make check` runs locally (green), the scripts'
refusal paths are executed by `tests/test_p15_pipeline.py`, and the workflow's shape is asserted by
`tools/p15-pipeline-check.py` on every run. The first real `deploy-staging` is an owner step, listed in
`docs/P15-deployment.md`.
