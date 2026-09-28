"""D3 · The payment webhooks: `Stripe-Signature` and Telegram's `successful_payment`.

Written before either integration exists, from the requirements P14 recorded, because the failure mode of getting
this wrong is silent. A webhook handler that trusts its body fulfils orders for people who did not pay, and it
looks like a working feature: the order appears, the ledger balances, and nothing anywhere says a stranger POSTed
it. Every decision below is one of those requirements made executable, and each carries the reason it is not the
obvious alternative.

**Raw bytes, never a re-serialisation.** Stripe signs `"<timestamp>.<body>"` where the body is the bytes that
arrived. A handler that parses to a dict and re-encodes breaks the MAC for every honest request (`json.dumps`
orders keys differently, drops the original spacing, and re-escapes strings), and the temptation is then to "fix"
it by scanning the parsed fields for the signature instead — which verifies a *reconstruction* of the request. The
module therefore takes `bytes` and has no JSON parse on the verification path at all.

**Constant-time, and over every candidate.** Stripe sends several `v1=` values during a secret rotation, and the
comparison runs against all of them with the result accumulated rather than returned early. An early `return` on
the first match leaks, through timing, *which* of the values matched; `compare_digest` removes the length-and-byte
leak inside a comparison but says nothing about the loop around it.

**Five minutes, both directions.** Stripe's timestamp is inside the signed payload precisely so a captured request
cannot be replayed later. A future-dated timestamp is refused too: it is either a wrong clock or an attempt to buy
a longer window, and neither is a reason to trust a request more.

**Replay by event id.** The tolerance window is not a replay defence — a captured request is valid for five
minutes. `claim` is a callable the caller supplies (this module must not know about SQLite or Postgres: the API,
the bot worker and the probe each have their own store), and it is called **after** the signature and the window
pass and before any fulfilment, so a forged event never writes a row and never re-arms a legitimate one.

**Telegram Stars is not a form post.** A Stars payment arrives inside a verified bot *update*, so the only
legitimate source is the update body the webhook already authenticates with the secret header. `stars_payment`
reads it from `message.successful_payment` and refuses every other shape, including a top-level
`successful_payment` — a route that accepted one directly would be a payment endpoint with no bot behind it.

**The amount is cross-checked, never believed.** `fulfilment` takes the provider's figure and compares it to the
record we already hold (our order intent, our subscription row). It returns the *record's* user and plan for the
caller to act on, never anything from the event, so an attacker who controls the event body controls none of the
three numbers that decide what they get.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Callable

SIGNATURE_VERSION = "v1"
TOLERANCE_S = 300                     # five minutes, the window P14 names
FUTURE_SKEW_S = 60                    # a provider clock a minute ahead is a clock, not an attack
_HEX_LEN = 64                         # HMAC-SHA256 renders as 64 hex characters
_V1_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# Providers, as they appear in the replay ledger. A closed set: an unknown provider in that table would mean
# something wrote a row without going through one of the two verifiers above it.
PROVIDER_STRIPE = "stripe"
PROVIDER_STARS = "telegram_stars"


class WebhookError(ValueError):
    """Raised only for *malformed* input — a header that cannot be parsed at all, a body that is not bytes.

    A wrong signature is a **result**, not an exception, for the same reason `security/telegram.py` says so: the
    caller has to count it, log it and possibly block a source, and an exception invites a broad `except` that
    turns a failed check into a passed one.
    """


@dataclass(frozen=True)
class ParsedSignature:
    """The header, split. `t` is the signed timestamp; `v1` are the HMAC-SHA256 candidates."""

    timestamp: int | None
    v1: tuple[str, ...]
    pairs: tuple[tuple[str, str], ...] = ()

    @property
    def known_versions(self) -> tuple[str, ...]:
        return tuple(sorted({k for k, _ in self.pairs}))


@dataclass(frozen=True)
class Verified:
    ok: bool
    reason: str
    event_id: str = ""
    event_type: str = ""
    age_s: int = 0
    candidates: int = 0
    payload: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        """The audit-safe view: the event id and the verdict, never the payload.

        A webhook body carries customer identifiers, amounts and sometimes an email address. Anything that logs a
        verdict logs this, so the payload stays out of it — the same rule `security/redact.py` applies to the rest
        of the trail.
        """
        return {"ok": self.ok, "reason": self.reason, "event_id": self.event_id,
                "event_type": self.event_type, "age_s": self.age_s, "candidates": self.candidates}


def parse_stripe_signature(header: str | None) -> ParsedSignature:
    """`t=...,v1=...,v1=...` into its parts. Unknown versions (`v0`) are kept but never used to authenticate."""
    if not header:
        return ParsedSignature(None, ())
    pairs: list[tuple[str, str]] = []
    for chunk in header.split(","):
        piece = chunk.strip()
        if not piece:
            continue
        key, sep, value = piece.partition("=")
        if not sep:
            # A header with no `=` anywhere is malformed rather than merely unknown, and silently ignoring it
            # would turn a truncated header (a proxy that ate the rest, a client that sent half) into a request
            # that is "missing a signature" — a different incident with a different log line.
            raise WebhookError("Stripe-Signature has an element with no '=': %r" % piece[:24])
        pairs.append((key.strip(), value.strip()))
    ts: int | None = None
    for key, value in pairs:
        if key == "t":
            ts = int(value) if value.isdigit() else None
            break
    v1 = tuple(value for key, value in pairs if key == SIGNATURE_VERSION and _V1_RE.match(value))
    return ParsedSignature(ts, v1, tuple(pairs))


def sign_stripe(raw_body: bytes, secret: str, at_s: int) -> str:
    """The header value a provider would send. Test-only, and marked as such for the reason
    `security/telegram.py`'s `sign` is: a helper that can mint a valid signature is one bad import away from being
    a bypass. It is here so the tests and the probe sign what Stripe signs, rather than hand-rolling the scheme
    and testing that hand-rolled thing.
    """
    digest = hmac.new(secret.encode(), b"%d." % int(at_s) + raw_body, hashlib.sha256).hexdigest()
    return "t=%d,v1=%s" % (int(at_s), digest)


def verify_stripe(raw_body: bytes | str, header: str | None, secret: str, *, at_ms: int,
                  tolerance_s: int = TOLERANCE_S, claim: Callable[[str, str, int], bool] | None = None,
                  require_event_id: bool = True) -> Verified:
    """Verify a Stripe webhook. Returns a `Verified`; never raises for a bad signature.

    `claim(event_id, provider, at_ms) -> bool` is the replay store, called only after everything else has passed.
    """
    if isinstance(raw_body, str):
        # The one conversion this module allows, and it is a refusal of the common mistake: a `str` body means the
        # caller already decoded the request, and re-encoding it may or may not reproduce the bytes Stripe signed
        # depending on the server. Refuse rather than guess.
        raise WebhookError("verify_stripe takes the raw request bytes, not a decoded string")
    if not isinstance(raw_body, (bytes, bytearray)):
        raise WebhookError("verify_stripe takes the raw request bytes; got %s" % type(raw_body).__name__)
    if not secret:
        # An unconfigured secret must not verify anything. With an empty key, every HMAC over an empty secret
        # matches — the same trap `check_secret_key` refuses for the bot token.
        return Verified(False, "no_secret_configured")
    parsed = parse_stripe_signature(header)
    if not parsed.pairs:
        return Verified(False, "no_signature")
    if parsed.timestamp is None:
        return Verified(False, "no_timestamp")
    if not parsed.v1:
        return Verified(False, "no_supported_signature", candidates=0)

    signed = b"%d." % parsed.timestamp + bytes(raw_body)
    matched = False
    for candidate in parsed.v1:
        expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        # No early exit: every candidate is compared, so the time taken says nothing about which one matched.
        matched = hmac.compare_digest(expected, candidate) or matched
    if not matched:
        return Verified(False, "bad_signature", candidates=len(parsed.v1))

    now_s = int(at_ms) // 1000
    age = now_s - parsed.timestamp
    if age < -FUTURE_SKEW_S:
        return Verified(False, "timestamp_in_the_future", age_s=age, candidates=len(parsed.v1))
    if age > tolerance_s:
        return Verified(False, "timestamp_outside_tolerance", age_s=age, candidates=len(parsed.v1))

    try:
        payload = json.loads(bytes(raw_body))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return Verified(False, "malformed_body", age_s=age, candidates=len(parsed.v1))
    if not isinstance(payload, dict):
        return Verified(False, "malformed_body", age_s=age, candidates=len(parsed.v1))
    event_id = str(payload.get("id") or "")
    event_type = str(payload.get("type") or "")
    if require_event_id and not event_id:
        # The id is what makes the replay ledger possible. An event without one cannot be claimed, so fulfilling
        # it would fulfil a body that could be sent again for ever.
        return Verified(False, "no_event_id", age_s=age, candidates=len(parsed.v1))
    if claim is not None and event_id and not claim(event_id, PROVIDER_STRIPE, int(at_ms)):
        return Verified(False, "replay", event_id=event_id, event_type=event_type, age_s=age,
                        candidates=len(parsed.v1))
    return Verified(True, "ok", event_id=event_id, event_type=event_type, age_s=age,
                    candidates=len(parsed.v1), payload=payload)


def verify_update_secret(presented: str | None, expected: str) -> bool:
    """Telegram's `X-Telegram-Bot-Api-Secret-Token`, compared in constant time.

    Constant time matters more here than the length of the secret suggests: this single header is the whole
    authentication of the bot webhook, and a byte-by-byte comparison leaks it one character at a time to anybody
    willing to send a few thousand requests. An empty configured secret refuses everything rather than accepting
    an absent header (an empty string compares equal to an empty string).
    """
    if not expected:
        return False
    return hmac.compare_digest(str(presented or ""), str(expected))


@dataclass(frozen=True)
class StarsPayment:
    ok: bool
    reason: str
    telegram_payment_charge_id: str = ""
    provider_payment_charge_id: str = ""
    total_amount: int = 0                 # Stars, as the provider states them (the *smallest* unit Telegram sends)
    currency: str = ""
    user_id: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "charge_id": self.telegram_payment_charge_id,
                "total_amount": self.total_amount, "currency": self.currency}


def stars_payment(update: dict) -> StarsPayment:
    """The `successful_payment` inside a verified bot update, and nowhere else.

    Telegram delivers a Stars payment as a *message* in an update; there is no payment webhook of its own. So the
    extraction refuses a top-level `successful_payment` (the shape somebody would invent if they were building a
    payment endpoint and wanted the same field to arrive directly), refuses a payment attached to anything but a
    message, and refuses a payment with no charge id — because the charge id is that flow's replay key.
    """
    if not isinstance(update, dict):
        return StarsPayment(False, "malformed_update")
    if "successful_payment" in update:
        return StarsPayment(False, "payment_outside_a_message")
    message = update.get("message")
    if not isinstance(message, dict):
        return StarsPayment(False, "no_message")
    payment = message.get("successful_payment")
    if not isinstance(payment, dict):
        return StarsPayment(False, "no_successful_payment")
    charge = str(payment.get("telegram_payment_charge_id") or "")
    if not charge:
        return StarsPayment(False, "no_charge_id")
    sender = message.get("from")
    user_id = str((sender or {}).get("id") or "") if isinstance(sender, dict) else ""
    amount = payment.get("total_amount")
    if not isinstance(amount, int) or amount <= 0:
        return StarsPayment(False, "bad_amount")
    return StarsPayment(True, "ok", telegram_payment_charge_id=charge,
                        provider_payment_charge_id=str(payment.get("provider_payment_charge_id") or ""),
                        total_amount=amount, currency=str(payment.get("currency") or ""), user_id=user_id)


@dataclass(frozen=True)
class Fulfilment:
    """What the caller may act on. `user_id` and `plan` come from **our** record, never from the event."""

    ok: bool
    reason: str
    user_id: str = ""
    plan: str = ""
    amount_micro: int = 0

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "plan": self.plan}


def fulfilment(*, provider_amount_micro: int, provider_currency: str, recorded: dict | None,
               recorded_amount_micro: int | None = None, currency: str = "usd") -> Fulfilment:
    """Decide whether a verified event may grant anything, by **cross-checking the provider's figure against the
    record we already hold** — the P14 requirement, expressed as the only function that can say yes.

    The rule it enforces: a webhook is a *notification* that something happened, not an instruction about what to
    grant. So the amount must equal what our own row says the customer owes, the currency must match, and the
    user and the plan come out of the row. An attacker who can edit the event body — or who finds a provider
    object whose metadata we never validated — has no lever on any of the three values that decide what they
    receive.
    """
    if not recorded:
        # An event for something we never wrote down. This is the case a naive handler fulfils "helpfully".
        return Fulfilment(False, "no_recorded_intent")
    if not isinstance(recorded_amount_micro, int):
        return Fulfilment(False, "no_recorded_amount")
    if str(recorded.get("currency") or currency) != str(provider_currency):
        return Fulfilment(False, "currency_mismatch")
    if int(provider_amount_micro) != int(recorded_amount_micro):
        # The provider's number and ours disagree. Refuse and reconcile: one of the two is wrong, and granting
        # the difference in either direction is a decision a human should make.
        return Fulfilment(False, "amount_mismatch", amount_micro=int(provider_amount_micro))
    return Fulfilment(True, "ok", user_id=str(recorded.get("user_id") or ""),
                      plan=str(recorded.get("plan") or ""), amount_micro=int(recorded_amount_micro))
