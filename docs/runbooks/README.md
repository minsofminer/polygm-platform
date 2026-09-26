# Runbooks

Fourteen pages the kit asks for, plus four the design turned out to need. Each one is written to be *used* at 2am
by a person who did not write it: symptoms first, then diagnosis with the exact command, then remediation, then the
check that proves it is over, then who to wake up.

| # | runbook | severity | owner |
| --- | --- | --- | --- |
| 1 | [unreconciled-orders.md](unreconciled-orders.md) | SEV1 | on-call (money path) |
| 2 | [executor-down.md](executor-down.md) | SEV1 | on-call (money path) |
| 3 | [source-silent.md](source-silent.md) | SEV1 | on-call (data plane) |
| 4 | [upstream-schema-change.md](upstream-schema-change.md) | SEV1/SEV2 | data plane + money path |
| 5 | [rate-limit-ban.md](rate-limit-ban.md) | SEV1/SEV2 | on-call (money path) |
| 6 | [wallet-provider-outage.md](wallet-provider-outage.md) | SEV1 | on-call (money path) |
| 7 | [builder-code-disabled.md](builder-code-disabled.md) | SEV1 | on-call (revenue path) |
| 8 | [kill-switch.md](kill-switch.md) | SEV1 | on-call (money path) |
| 9 | [database-failover.md](database-failover.md) | SEV1 | on-call (platform) |
| 10 | [key-compromise.md](key-compromise.md) | SEV1 | security lead + money path |
| 11 | [telegram-restricted.md](telegram-restricted.md) | SEV2 | on-call (product) |
| 12 | [stuck-bridge.md](stuck-bridge.md) | SEV1 | on-call (money path) |
| 13 | [cost-spike.md](cost-spike.md) | SEV3 | platform owner |
| 14 | [rollback.md](rollback.md) | SEV1 | on-call (money path) |
| 15 | [order-path-degradation.md](order-path-degradation.md) | SEV2 | on-call (money path) |
| 16 | [cache-queue-age.md](cache-queue-age.md) | SEV2 | on-call (platform) |
| 17 | [dependency-cve.md](dependency-cve.md) | SEV3 | security owner |
| 18 | [withdrawal-spike.md](withdrawal-spike.md) | SEV1 | on-call (money path) |

Rows 15-18 exist because the alarm registry needed somewhere to point: "risk service degraded", "cache age above
threshold", "a dependency CVE" and "a withdrawal spike" are in the kit's own severity table, and an alarm with no
runbook is an alarm that gets deleted.

## What keeps these honest

`tools/p15-runbooks-check.py` reads every page here and the alarm registry (`ops/alerts.yaml`), and fails on any of
these:

* a runbook missing one of the five sections (symptoms, diagnosis, remediation, verification, escalation);
* a runbook with fewer than three runnable command blocks — prose that cannot be typed is not a procedure;
* a command naming a file that does not exist in this repository (`tools/…`, `deploy/…`, `docs/…`);
* a command invoking one of our tools or services with a flag that does not exist — the checker runs the tool's own
  `--help` and compares, which is how three wrong commands were caught while these pages were being written;
* an alarm whose `runbook:` points at a file that does not exist, or a runbook claiming an alarm id the registry
  does not define — the link is checked in **both** directions, so neither side can rot alone;
* a runbook whose `last_drilled` is older than ninety days — an undrilled runbook is a hypothesis.

`python3 tools/p15-runbooks-check.py --self-test` plants six breakages (a removed section, a misspelled path, a
nonexistent flag, a stale drill date, a bogus alarm id, missing front matter) and requires the checker to catch
each one. A checker that cannot fail is a rubber stamp.

## Who still has to run these

The pages are written and the commands are verified against the tools that exist; running them **in staging, by
someone other than the author** is D9's checklist item and an owner step, listed in `docs/P15-readiness.md`. The
`last_drilled` dates in each page's front matter are the record — they move when a drill happens, and the checker
refuses to let one go ninety days stale.
