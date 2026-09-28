"""The replay ledger: one row per processed payment event, and the only reason a webhook can be delivered twice.

Why a table rather than a set in memory: the tolerance window is not a replay defence. A captured request stays
valid for five minutes, and a restart between the two deliveries would forget an in-process set. The unique key is
the event id the provider assigns, which is stable across retries — and for Telegram Stars it is the charge id,
because that flow has no event id of its own.

The `claim` callable this module returns is what `verify_stripe` calls. It is passed in rather than imported
because `webhooks.py` must not know about a database (the same split `security/telegram.py` uses for its nonce
store): the API has a Postgres connection, the bot worker has another, and the attack-surface probe runs against
SQLite.

**The insert is the claim.** A read-then-write would race: two deliveries arriving together both read "not seen"
and both fulfil. The unique constraint decides the winner, and the loser is told so by the exception — which is
why this module is written around INSERT rather than around SELECT.
"""
from __future__ import annotations

import sqlite3
from typing import Any

INSERT = ("INSERT INTO payment_events (event_id, provider, event_type, received_ms) VALUES (?, ?, ?, ?)")

# What a unique-violation looks like on each driver, without importing either: SQLite raises `IntegrityError`,
# psycopg raises `UniqueViolation` (a subclass of IntegrityError, but the class name is the portable check and
# `polygm_core` is dependency-free by rule — the lint gate enforces it).
_UNIQUE_ERROR_NAMES = {"IntegrityError", "UniqueViolation", "UniqueViolationError", "IntegrityError_"}


def _is_unique_violation(err: BaseException) -> bool:
    for klass in type(err).__mro__:
        if klass.__name__ in _UNIQUE_ERROR_NAMES:
            return True
    return "unique" in str(err).lower() or "duplicate key" in str(err).lower()


def claim_event(conn: Any, event_id: str, provider: str, received_ms: int, event_type: str = "") -> bool:
    """True if this event is new (and is now claimed); False if it has been seen before.

    The row is written even when the caller goes on to refuse fulfilment — "we saw this event and refused it" is
    the fact an investigation needs, and a ledger that only records successes cannot answer "did the forged
    delivery arrive twice".
    """
    if not event_id:
        # Never claim an empty id: every id-less event would then be "the same event" as every other, and the
        # first one through would refuse all the rest.
        return False
    try:
        conn.execute(INSERT, (event_id, provider, event_type, int(received_ms)))
    except sqlite3.IntegrityError:
        return False
    except Exception as err:  # noqa: BLE001 — see _is_unique_violation: the driver's class is the check
        if _is_unique_violation(err):
            return False
        raise
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()
    return True


def seen_event(conn: Any, event_id: str) -> bool:
    """Read-only view of the ledger, for the probe and for an operator asking "have we processed this?"."""
    row = conn.execute("SELECT 1 FROM payment_events WHERE event_id=?", (event_id,)).fetchone()
    return row is not None
