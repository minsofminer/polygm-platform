"""The executor: the only process that can hold a key, and the only writer of `orders`.

It consumes intents from the queue and nothing else. It has no listen socket, no health endpoint of its
own beyond /healthz on a unix-ish internal port, and no route to the public API — rule 1 of P04.

THE CASE THAT LOSES MONEY (P04 D5, last bullet): the executor signs, POSTs, and dies before reading the
response. At that instant the order may exist at the venue or may not, and *both* are plausible. The
wrong recovery is any action that could send the order a second time. So:

    state = UNCERTAIN  →  never re-POST  →  find out by RECONCILING

Reconciliation is by client order hash, not by idempotency key: the hash is deterministic from the intent,
so it survives the death of the process and can be looked up at the venue. That is the only property that
makes "what did I actually send?" answerable after a crash.

Every hop has an explicit timeout/retry budget (constraint: name the values). See HOPS.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field

from ..money.cents import SCALE, to_float_for_sdk
from ..ledger.ledger import RECONCILE_ACCEPTED_STATES, VENUE_ORDER_STATUS
from ..risk.gate import Deny, Decision

# (hop, timeout_ms, retries, backoff, on_exhaustion). Named here, not in YAML, so a reviewer can see that
# no hop retries an *unconfirmed* POST — retrying a POST whose response was lost is the second way to
# double-spend, and the executor must not be able to do it by configuration.
HOPS: dict[str, dict] = {
    "intent_read":      {"timeout_ms": 1000, "retries": 0, "backoff": None,
                         "on_exhaustion": "leave on the queue (at-least-once is fine; the key is not consumed until we act)"},
    "risk_call":        {"timeout_ms": 50,   "retries": 0, "backoff": None,
                         "on_exhaustion": "reject RISK_UNAVAILABLE (fail closed; the client may retry)"},
    "market_data_read": {"timeout_ms": 250,  "retries": 1, "backoff": "fixed 50ms",
                         "on_exhaustion": "reject STALE_QUOTE — never sign against a book we could not read"},
    "sign":             {"timeout_ms": 200,  "retries": 0, "backoff": None,
                         "on_exhaustion": "reject SIGNER_UNAVAILABLE and pull the kill switch (a signer that "
                                          "fails mid-run is an integrity event, not a transient one)"},
    "post_order":       {"timeout_ms": 1500, "retries": 0, "backoff": None,
                         "on_exhaustion": "UNCERTAIN + reconcile; a timeout here is NOT a retryable error"},
    "reconcile_lookup": {"timeout_ms": 2000, "retries": 5, "backoff": "exp 500ms cap 8s",
                         "on_exhaustion": "leave UNCERTAIN, alert (human), and keep the order blocked"},
    "cancel_all":       {"timeout_ms": 2000, "retries": 3, "backoff": "exp 250ms cap 4s",
                         "on_exhaustion": "alert; cancels are always safe to retry, unlike posts"},
}


@dataclass(frozen=True)
class OrderIntent:
    id: str
    user_id: str
    token_id: str
    side: str
    price_micro: int
    size_shares_micro: int
    idempotency_key: str
    condition_id: str = ""
    tick_size: str = "0.001"
    builder_code: str = "0x" + "0" * 64
    metadata: str = "0x" + "0" * 64
    expiration: int = 0

    def client_order_hash(self, signer_pubkey: str) -> str:
        """Deterministic in the intent and nothing else. This is what makes post-crash reconciliation
        possible: recomputing it in a new process yields the same value the dead process used."""
        core = json.dumps({"t": self.token_id, "p": self.price_micro, "s": self.size_shares_micro,
                           "d": self.side, "u": self.user_id, "k": self.idempotency_key,
                           "x": self.expiration, "b": self.builder_code, "g": signer_pubkey},
                          sort_keys=True, separators=(",", ":"))
        return "0x" + hashlib.sha256(core.encode()).hexdigest()


@dataclass
class Submission:
    intent: OrderIntent
    order_hash: str
    state: str                       # submitted | uncertain | rejected
    venue_order_id: str | None = None
    raw_error_code: str | None = None
    notes: list[str] = field(default_factory=list)


class Transport:
    """Injected so the mock and the real client are the same shape. `post_order` may raise TimeoutError;
    that is a normal outcome, and the caller's handling of it is the whole point of this module."""

    def post_order(self, signed: dict, *, timeout_ms: int) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    def find_order(self, client_order_hash: str, *, timeout_ms: int) -> dict | None:  # pragma: no cover
        raise NotImplementedError

    def cancel_all(self, *, timeout_ms: int) -> dict:  # pragma: no cover
        raise NotImplementedError


def build_signed_payload(i: OrderIntent, *, client_order_hash: str | None = None) -> dict:
    """The V2 struct. Note the float conversion is confined to this function and is asserted —
    `to_float_for_sdk` raises rather than shipping a value that cannot come back unchanged. The SDK
    (py-clob-client-v2 1.1.0) types these as float; our ledger never does.

    `client_order_hash` is a parameter on purpose. The executor reconciles by that hash, so the value it
    computes and the value the venue indexes MUST be produced by one call, not derived independently by two
    components (the transport that guessed it and the executor that recomputed it disagreed by exactly one
    field, and every post-crash lookup would have silently missed the order — caught only because the test
    asserted recovery succeeds, which is the reason these tests exist).
    """
    return {
        "version": "v2",
        "client_order_hash": client_order_hash or i.client_order_hash("unsigned"),
        "salt": int(hashlib.sha256(i.id.encode()).hexdigest()[:12], 16),
        "maker": i.user_id,
        "token_id": i.token_id,
        "market": i.condition_id,
        "side": i.side,
        "price": to_float_for_sdk(i.price_micro, field="price"),
        "size": to_float_for_sdk(i.size_shares_micro, field="size"),
        "fee_rate_bps": 0,
        "builder": i.builder_code,
        "metadata": i.metadata,
        "expiration": i.expiration,
        "timestamp": int(time.time() * 1000) // 1000,
    }


def submit(i: OrderIntent, t: Transport, decision: Decision, *, signer_pubkey: str,
           now_ms: int | None = None) -> Submission:
    """One attempt. Never retries the POST (see HOPS)."""
    h = i.client_order_hash(signer_pubkey)
    if not decision.allowed:
        return Submission(i, h, "rejected", raw_error_code=decision.code, notes=list(decision.checks_run))
    try:
        payload = build_signed_payload(i, client_order_hash=h)
        resp = t.post_order(payload, timeout_ms=HOPS["post_order"]["timeout_ms"])
    except (TimeoutError, ConnectionError) as e:
        # We do not know. Say so, block the intent, reconcile. Note what we do NOT do: re-POST, mark it
        # failed, or return an error to the user that implies their money is untouched.
        return Submission(i, h, "uncertain",
                          notes=["post raised; state=uncertain; reconcile by client_order_hash"])
    if resp.get("success"):
        return Submission(i, h, "submitted", venue_order_id=resp.get("orderID"))
    return Submission(i, h, "rejected", raw_error_code=resp.get("code"),
                      notes=[str(resp.get("message", ""))])


RECONCILE_DELAYS_MS = (0, 500, 1000, 2000, 4000, 8000)   # production schedule; tests pass their own.
# Injectable because a retry schedule that cannot be shortened is a schedule nobody tests, and this is the
# one loop whose *exhaustion* (not whose success) is the behaviour that keeps a duplicate order from being
# posted.


def reconcile(s: Submission, t: Transport, *, delays_ms: tuple[int, ...] | None = None) -> Submission:
    """Resolve UNCERTAIN without ever sending twice. The venue is the authority on whether the hash
    exists; if the lookup itself keeps failing we stay uncertain forever, which is the correct
    non-answer — a guess here is a duplicate order."""
    for attempt, delay_ms in enumerate(delays_ms if delays_ms is not None else RECONCILE_DELAYS_MS):
        if attempt:
            time.sleep(delay_ms / 1000)
        found = t.find_order(s.order_hash, timeout_ms=HOPS["reconcile_lookup"]["timeout_ms"])
        if found is not None:
            raw = found.get("status", "")
            # an unmapped status must not be read as "the order does not exist": that is how a new venue
            # status turns into a re-post. Treat unknown as UNCERTAIN and let a human decide.
            known = raw in RECONCILE_ACCEPTED_STATES or raw in VENUE_ORDER_STATUS
            if not known:
                return Submission(s.intent, s.order_hash, "uncertain",
                                  notes=s.notes + [f"unmapped venue status {raw!r}; staying uncertain"])
            state = "submitted" if raw in RECONCILE_ACCEPTED_STATES else "rejected"
            return Submission(s.intent, s.order_hash, state, venue_order_id=found.get("orderID"),
                              notes=s.notes + [f"reconciled on attempt {attempt + 1}"])
    return Submission(s.intent, s.order_hash, "uncertain",
                      notes=s.notes + ["reconcile exhausted; human alert raised; intent stays blocked"])
