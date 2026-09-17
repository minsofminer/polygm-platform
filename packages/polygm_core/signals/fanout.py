"""Delivery fan-out: turn `alert_deliveries` rows into claims, fairly, at-least-once.

This module is deliberately free of I/O. It takes rows and a clock and returns a plan, because the part that has
to be right — who gets paged first when 400 alerts land in one second, and what happens when a push provider
time-outs mid-flight — is a scheduling argument, and a scheduling argument tested against a live provider is a
test that fails for reasons outside the product. The worker that executes the plan is P10's transport; it owns
the network and none of these decisions.

Three rules the shape of this product forces:

1. **Priority is a tier, not a queue-jump without limit.** `alert_deliveries.priority` comes from
   `entitlements.plan` (0 trader/pro, 1 trial, 2 free/default). Strict priority would starve the free tier the
   moment two pro users got excited, and an alert that never arrives is worse than an alert that arrives late.
   So: drain by class, but each class gets a per-worker budget per cycle, and a user cannot take more than
   `per_user_per_cycle` of it. A free-tier user's alert is late by one cycle, never indefinitely.
2. **At-least-once, with the duplicate made harmless.** A claim that is not acked within `invisibility_ms` is
   reclaimable, because a worker that dies mid-send must not swallow a page. The consequence is a duplicate
   *delivery attempt*, and the dedupe for that lives at the channel boundary: `idempotency_key(signal_id,
   channel)` is what a transport passes to APNs/Telegram/Firebase, so a redelivery is a no-op at the provider
   rather than a second buzz in someone's pocket.
3. **Retries back off, and then stop.** Exponential with a ceiling, and a `dead` state after `max_attempts` —
   silently retrying a channel the user has since uninstalled is how an ingest service becomes a load generator
   aimed at a provider that already told us no.

No floats in the money path applies here too: `queued_ms`/`due_ms`/`sent_ms` are integers of milliseconds, and
the backoff is integer arithmetic on powers of two with a hash-derived jitter, so a plan is reproducible.
"""
from __future__ import annotations

import hashlib

STATUS_QUEUED = "queued"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_RETRY = "retry"
STATUS_DEAD = "dead"

# Claimable statuses. `sent` is absent by construction: an acked delivery is finished, and letting a visibility
# timeout reclaim it is how a user gets paged twice a minute forever.
CLAIMABLE = (STATUS_QUEUED, STATUS_RETRY)
TERMINAL = (STATUS_SENT, STATUS_DEAD)

_KEY_SEP = "\x1f"


def idempotency_key(signal_id: str, channel: str) -> str:
    """The key a transport hands the provider so a redelivery is not a second notification.

    Scope is (signal, channel), not (signal, user): one signal can legitimately page the same user on two
    channels, and those are two deliveries, both wanted.
    """
    # \x1f (unit separator) for the same reason `tape.dedupe_key` uses it: a join on a character that can appear
    # in the data is how two different deliveries end up with one key.
    return hashlib.sha256((_KEY_SEP.join([signal_id, channel])).encode()).hexdigest()[:32]


def backoff_ms(attempts: int, *, base_ms: int = 5_000, ceiling_ms: int = 600_000, key: str = "") -> int:
    """Exponential backoff with deterministic jitter, in integer milliseconds.

    The jitter is derived from the delivery key rather than from `random` for two reasons that are not about
    test convenience: a schedule that changes on every restart makes "why was this late" unanswerable, and two
    workers that both compute a retry time for the same row must not disagree about it. `hash()` is unusable for
    that (PYTHONHASHSEED), so this uses sha256 and takes a 0..1023 ms spread.
    """
    a = max(0, int(attempts))
    raw = base_ms * (2 ** min(a, 20))
    spread = 1024 if raw < ceiling_ms else 0
    if spread:
        h = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)
        raw += h % spread
    return int(min(raw, ceiling_ms))


def _due(row: dict, now_ms: int) -> int:
    """When this row may next be claimed. A row in the future is not lost, it is waiting."""
    queued = int(row.get("queued_ms") or 0)
    attempts = int(row.get("attempts") or 0)
    if str(row.get("status") or STATUS_QUEUED) == STATUS_RETRY and attempts > 0:
        return queued + backoff_ms(attempts - 1, key=str(row.get("signal_id", "")) + str(row.get("channel", "")),
                                   base_ms=int(row.get("backoff_base_ms") or 5_000),
                                   ceiling_ms=int(row.get("backoff_ceiling_ms") or 600_000))
    return queued


def plan(rows: list[dict], *, now_ms: int, workers: int = 4, per_user_per_cycle: int = 2,
         invisibility_ms: int = 30_000, max_attempts: int = 5) -> dict:
    """Decide which rows to claim this cycle.

    Returns `{"claims": [...], "waiting": [...], "expired_claims": [...], "dead": [...]}` where each claim row
    carries `due_ms` (when its lease ends) and `idempotency_key`. Deterministic: same rows, same clock, same
    plan, which is the only way a scheduling change can be reviewed.
    """
    claims: list[dict] = []
    waiting: list[dict] = []
    dead: list[dict] = []
    # A lease that ran out is a worker that died (or is very slow). It goes back into the claimable pool, and
    # the count is reported, because a rising `expired_claims` means the transport is too slow for the volume
    # and the fix is fewer alerts per cycle, not a longer lease.
    expired: list[dict] = []
    per_user: dict[tuple[str, str], int] = {}

    live: list[tuple[int, int, str, dict]] = []
    for r in rows:
        row = dict(r)
        status = str(row.get("status") or STATUS_QUEUED)
        attempts = int(row.get("attempts") or 0)
        if status == STATUS_SENDING:
            lease_ends = int(row.get("claim_ms") or row.get("queued_ms") or 0) + invisibility_ms
            if now_ms >= lease_ends:
                row["status"] = STATUS_RETRY
                row["attempts"] = attempts + 1        # a lost lease consumed an attempt; pretending otherwise
                expired.append(row)                    # makes a wedged worker look like an infinite queue
                status = STATUS_RETRY
                attempts = int(row["attempts"])
            else:
                waiting.append(dict(row, reason="leased", due_ms=lease_ends))
                continue
        if status in TERMINAL:
            continue
        if attempts >= max_attempts:
            # Dead-lettered rather than retried, and kept: an alert the user never received is an incident with
            # a name, and `dead` rows are what the "my alerts stopped" support thread is answered from.
            row["status"] = STATUS_DEAD
            row["dead_reason"] = "max_attempts"
            dead.append(row)
            continue
        live.append((_due(row, now_ms), int(row.get("priority") or 0), str(row.get("user_id") or ""), row))

    # Order: earliest due, then priority class, then arrival, then key for total determinism. Priority is the
    # SECOND key, not the first, on purpose — a free-tier alert that is 10 minutes overdue is not made younger by
    # the arrival of a trader's alert, and a scheduler that pretends otherwise starves whoever has the least.
    live.sort(key=lambda t: (t[0], t[1], int(t[3].get("queued_ms") or 0), str(t[3].get("signal_id", ""))))
    for due, _prio, uid, row in live:
        if due > now_ms:
            # Not yet due. This check is the difference between "backoff" and "retries that ignore backoff":
            # without it a row in `retry` state is reclaimed on the very next cycle, the provider is hammered on
            # a channel that just said no, and `max_attempts` burns through in seconds instead of minutes.
            waiting.append(dict(row, reason="backoff", due_ms=due))
            continue
        if len(claims) >= workers:
            waiting.append(dict(row, reason="no-worker", due_ms=due))
            continue
        if per_user.get((uid, str(row.get("channel", ""))), 0) >= per_user_per_cycle:
            # Fairness cap. The alternative is a burst of 40 alerts from one user's rules occupying every worker
            # while everyone else waits, which turns a paid feature into an outage for everyone else.
            waiting.append(dict(row, reason="per-user-cap", due_ms=due))
            continue
        key = (uid, str(row.get("channel", "")))
        per_user[key] = per_user.get(key, 0) + 1
        claims.append(dict(row, due_ms=due, lease_expires_ms=now_ms + invisibility_ms,
                           idempotency_key=idempotency_key(str(row.get("signal_id", "")),
                                                           str(row.get("channel", "")))))
    return {"claims": claims, "waiting": waiting, "expired_claims": expired, "dead": dead,
            "queued_total": len([r for r in rows if str(r.get("status") or STATUS_QUEUED) in CLAIMABLE])}


def ack(row: dict, *, now_ms: int) -> dict:
    """Successful send. Terminal, and the only transition that writes `sent_ms`."""
    return dict(row, status=STATUS_SENT, sent_ms=int(now_ms), attempts=int(row.get("attempts") or 0) + 1)


def fail(row: dict, *, now_ms: int, max_attempts: int = 5, base_ms: int = 5_000,
         ceiling_ms: int = 600_000, error: str = "") -> dict:
    """Failed send. Either schedule a retry or dead-letter it, and never both."""
    if str(row.get("status") or STATUS_QUEUED) == STATUS_DEAD:
        # Dead is terminal, in both directions: a late `fail` from a worker that had the row in flight when it
        # was dead-lettered must not advance `attempts` or clear `dead_reason`, or the row stops being a
        # readable answer to "when did we stop trying".
        return dict(row)
    attempts = int(row.get("attempts") or 0) + 1
    key = "%s%s" % (row.get("signal_id", ""), row.get("channel", ""))
    if attempts >= max_attempts:
        return dict(row, status=STATUS_DEAD, attempts=attempts, dead_reason=error[:160] or "max_attempts")
    return dict(row, status=STATUS_RETRY, attempts=attempts, failed_ms=int(now_ms),
                retry_in_ms=backoff_ms(attempts, key=key, base_ms=base_ms, ceiling_ms=ceiling_ms),
                last_error=error[:160])


def lag_ms(rows: list[dict], *, now_ms: int) -> dict:
    """Oldest unsent delivery's age, by priority class. What the ops page reads as "delivery is backing up".

    Measured per class because a 30 s lag on free-tier alerts during a burst is the fairness cap working, while
    30 s on a trader's alert is a provider problem. One blended average cannot tell those apart, and the second
    is the one somebody has to look at.
    """
    out = {0: 0, 1: 0, 2: 0}
    for r in rows:
        if str(r.get("status") or STATUS_QUEUED) in TERMINAL:
            continue
        p = int(r.get("priority") or 0)
        age = int(now_ms) - int(r.get("queued_ms") or now_ms)
        if age > out.get(p, 0):
            out[p] = age
    return {"worst_ms": max(out.values()) if out else 0, "by_priority": out}
