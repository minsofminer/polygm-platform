"""Per-chat state: the half-typed order, the chosen market, the step of the key-export disclaimer.

A chat is a state machine with no screen to hold state, so the state lives here, persisted, with an expiry. Three
rules make it safe:

* **A session carries the *step*, never the money.** What is stored is "waiting for a size on market X", not a
  signed order, not a price, not a balance. Every number that reaches an order is re-read from the ledger when the
  confirm is pressed, because a stored price is a price from the past and the user is confirming the *current* one.
* **Every session expires.** A conversation left mid-flow overnight must not drop the user back into a confirm card
  whose buttons are a day old; `expires_ms` is checked on every read, and an expired session restarts the flow with
  one line explaining why.
* **One session per chat, keyed by chat id, not by user id.** In a group, the same user may talk to the bot from a
  different chat, and the two conversations must not share state — which is also why the *account* mapping is a
  separate lookup (P07's identities) rather than something stored in here.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

#: Steps, as a closed vocabulary. A step is a promise about what the next message means, so an unknown step is a
#: session the router cannot safely continue, and it restarts rather than guesses.
STEPS = ("idle", "choose_market", "choose_side", "choose_size", "confirm_order", "custom_size", "withdraw_amount",
         "withdraw_address", "export_disclaimer", "export_confirm", "alert_rule", "copy_config", "verify_handle")

DEFAULT_TTL_MS = 15 * 60_000          # fifteen minutes: long enough to think, short enough that a stale tap is odd
EXPORT_TTL_MS = 5 * 60_000            # the key-export flow is short on purpose
MAX_STATE_BYTES = 2_048

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")


@dataclass
class Session:
    chat_id: str
    step: str = "idle"
    payload: dict = field(default_factory=dict)
    message_id: int = 0
    started_ms: int = 0
    updated_ms: int = 0
    expires_ms: int = 0
    version: int = 1

    @property
    def live(self) -> bool:
        return self.step != "idle" and self.expires_ms > 0

    def is_expired(self, at_ms: int) -> bool:
        return bool(self.expires_ms) and int(at_ms) >= int(self.expires_ms)

    def as_row(self) -> dict:
        """The row that goes to `telegram_sessions`, with the payload serialised and the size capped."""
        blob = json.dumps(self.payload or {}, sort_keys=True, separators=(",", ":"))
        if len(blob.encode("utf-8")) > MAX_STATE_BYTES:
            raise ValueError("session payload is %d bytes; a session is a step, not a document"
                             % len(blob.encode("utf-8")))
        return {"chat_id": str(self.chat_id), "step": self.step, "payload_json": blob,
                "message_id": int(self.message_id), "started_ms": int(self.started_ms),
                "updated_ms": int(self.updated_ms), "expires_ms": int(self.expires_ms),
                "version": int(self.version)}


def start(*, chat_id: str, step: str, at_ms: int, message_id: int = 0, payload: dict | None = None,
          ttl_ms: int = DEFAULT_TTL_MS) -> Session:
    """Open a session on a step. `export_*` gets the shorter TTL, because a key on screen is a key to get rid of."""
    if step not in STEPS:
        raise ValueError("%r is not a step this router understands" % step)
    ttl = EXPORT_TTL_MS if step.startswith("export") else int(ttl_ms)
    return Session(chat_id=str(chat_id), step=step, payload=dict(payload or {}), message_id=int(message_id),
                   started_ms=int(at_ms), updated_ms=int(at_ms), expires_ms=int(at_ms) + ttl)


def advance(session: Session, *, step: str, at_ms: int, payload: dict | None = None,
            ttl_ms: int = DEFAULT_TTL_MS) -> Session:
    """Move a session to its next step, replacing the payload (never merging into it).

    Replacing rather than merging is deliberate: a merged payload is how yesterday's chosen market survives into
    today's flow, which is the bug where a user confirms a card about a market they picked three messages ago.
    """
    if step not in STEPS:
        raise ValueError("%r is not a step this router understands" % step)
    ttl = EXPORT_TTL_MS if step.startswith("export") else int(ttl_ms)
    return Session(chat_id=session.chat_id, step=step, payload=dict(payload or {}),
                   message_id=session.message_id, started_ms=session.started_ms, updated_ms=int(at_ms),
                   expires_ms=int(at_ms) + ttl, version=session.version + 1)


def step_findings(step: str, payload: dict) -> list:
    """What a step requires of its payload before the router may act on the next message.

    This is the small function that stops a half-built order from reaching the risk gate: an order needs a market,
    a side and a size, and each step checks the pieces it owns rather than trusting that the earlier step happened
    — because the earlier step may have been in a different chat, or a different week.
    """
    out = []
    if step not in STEPS:
        return ["%r is not a step" % step]
    p = payload or {}
    if step in ("choose_side", "choose_size", "confirm_order", "custom_size"):
        if not SLUG_RE.match(str(p.get("slug") or "")):
            out.append("the order flow has no usable market slug")
    if step in ("confirm_order", "custom_size"):
        if str(p.get("side") or "").lower() not in ("yes", "no", "buy", "sell"):
            out.append("the order flow has no side")
    if step == "confirm_order":
        amount = str(p.get("amount") or "")
        if not re.match(r"^\d{1,9}(?:\.\d{1,2})?$", amount):
            out.append("the confirm step has no amount to confirm")
        if not str(p.get("action_id") or ""):
            out.append("the confirm step has no action id, so the tap cannot be deduped")
    if step == "withdraw_address" and not str(p.get("address_or_alias") or ""):
        out.append("the withdraw flow has no destination")
    if step == "export_confirm" and not p.get("acknowledged"):
        out.append("key export cannot be confirmed before the warning is acknowledged")
    return out
