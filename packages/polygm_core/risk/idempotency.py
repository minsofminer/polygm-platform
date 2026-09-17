"""Idempotency for every mutating endpoint (P04 rule 5).

The design constraint that makes this non-trivial: retries are normal (D5 says the client retries on
timeout), and the executor can die *after* signing and *before* reading the response. So an idempotency
record cannot be "did I run the handler?" — it has to be "what did the world end up doing?", which is why
the stored outcome is a status plus the venue's order hash, not just a boolean.

Backed by a UNIQUE index on (user_id, key) in `db/migrations/0004_idempotency.sql`; this class only
serialises. The DB is the arbiter — a process-local dict would let two pods both accept the same retry.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Record:
    """`state` is the row's state; `mine` is who is allowed to act. They are different questions and
    collapsing them is a bug: a fresh insert leaves the row in state='in_progress', so a caller that treats
    "in_progress" as "someone else is running it" 409s its OWN first request. Every real client of this
    class needs both, and the API's order endpoint did exactly that until a test caught it.
    """
    user_id: str
    key: str
    request_hash: str
    state: str            # in_progress | done
    response_json: str    # stored verbatim so a retry replays the SAME body, not a re-derived one
    order_hash: str | None
    mine: bool = False              # True only for the request that inserted the row: the ONLY one
                                    # allowed to do work
    @property
    def replay(self) -> bool:
        return self.state == "done"

    @property
    def busy(self) -> bool:
        return self.state == "in_progress" and not self.mine

    @property
    def mismatch(self) -> bool:
        return self.state == "mismatch"


def request_hash(body: dict) -> str:
    """Stable over dict ordering and whitespace. Two retries of the same logical request must collide;
    a retry with a *different* amount under the same key must NOT be silently served the old answer."""
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


#: `INSERT ... ON CONFLICT DO NOTHING` says nothing about whether it INSERTED, and `cursor.rowcount` is 0
#: for BOTH "conflicted" and "inserted" on several drivers (sqlite3 does the latter here). Reading rowcount
#: made every first request look like a replay-in-progress, i.e. the endpoint could never accept an order at
#: all. Caught by test_api, not by review. The portable answer is RETURNING (Postgres >= 9.5, SQLite >=
#: 3.35); Postgres additionally distinguishes them with `xmax = 0`, which is why the two statements differ.
SQL_BEGIN = """
INSERT INTO idempotency_keys (user_id, key, request_hash, state, response_json, order_hash)
VALUES (?, ?, ?, 'in_progress', NULL, NULL)
ON CONFLICT (user_id, key) DO NOTHING
RETURNING key
"""
SQL_BEGIN_PG = """
INSERT INTO idempotency_keys (user_id, key, request_hash, state, response_json, order_hash)
VALUES ($1, $2, $3, 'in_progress', NULL, NULL)
ON CONFLICT (user_id, key) DO UPDATE SET key = idempotency_keys.key
RETURNING (xmax = 0) AS inserted
"""

SQL_LOOKUP = "SELECT state, request_hash, response_json, order_hash FROM idempotency_keys WHERE user_id = ? AND key = ?"
SQL_FINISH = "UPDATE idempotency_keys SET state='done', response_json=?, order_hash=? WHERE user_id=? AND key=?"


class Idem:
    """Usage (and the ONLY correct usage):

        r = idem.begin(user, key, body)
        if r.replay:   return r.response      # never touch the venue again
        if r.mismatch: return 409 IDEM_CONFLICT
        try: ...work...; idem.finish(...)
        except: idem.abandon(...)             # so a retry can actually run

    Usage in a handler:

        rec = idem.begin(user, key, body)
        if rec.replay:   return json.loads(rec.response_json)   # the stored answer, byte for byte
        if rec.mismatch: return 409
        if rec.busy:     return 409 retryable
        ...work...;  idem.finish(...)   on success
        ...          idem.abandon(...)  on ANY early return, or the key is poisoned until a janitor
                                          sweeps it and the user cannot retry

    `abandon` deletes the in_progress row rather than marking it failed: a *crash* mid-order must not
    permanently poison a key, because the retry after a crash is the whole point of the key.

    Drivers: SQL_BEGIN (RETURNING, no xmax) is what the dev engine and tests use; SQL_BEGIN_PG is the
    Postgres form used by the api service when POLYGM_ENGINE=postgres. Both answer the same question
    ("did I just create this row?") and both are exercised: the pg text is checked by p04-gate-check.
    """

    def __init__(self, conn) -> None:
        self.c = conn

    def begin(self, user_id: str, key: str, body: dict) -> Record:
        h = request_hash(body)
        cur = self.c.execute(SQL_BEGIN, (user_id, key, h))
        if cur.fetchone() is not None:
            # RETURNING gave us a row: we are the owner of this key, nobody else is running it
            return Record(user_id, key, h, "in_progress", "", None, mine=True)
        row = self.c.execute(SQL_LOOKUP, (user_id, key)).fetchone()
        if row is None:  # pragma: no cover - race: inserted then deleted
            return self.begin(user_id, key, body)
        state, rh, resp, oh = row
        if rh != h:
            return Record(user_id, key, rh, "mismatch", "", oh)
        if state == "in_progress":
            # Still running elsewhere. Replaying a half-finished order is how you double-spend, and
            # answering "OK" is how you lie. The client retries; we do not guess.
            return Record(user_id, key, rh, "in_progress", "", oh)
        return Record(user_id, key, rh, "done", resp or "", oh)

    def finish(self, user_id: str, key: str, response: dict, order_hash: str | None = None) -> None:
        self.c.execute(SQL_FINISH, (json.dumps(response, sort_keys=True), order_hash, user_id, key))

    def abandon(self, user_id: str, key: str) -> None:
        self.c.execute("DELETE FROM idempotency_keys WHERE user_id=? AND key=? AND state='in_progress'",
                       (user_id, key))
