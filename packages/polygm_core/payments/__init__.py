"""Payments: the two inbound webhooks that will one day move money, and the checks they must pass first.

Nothing in this package fulfils a payment yet — there is no Stripe integration and no Telegram Stars product. That
is exactly why it exists now. Both of those integrations arrive as **attacker-reachable POSTs**, and the P14 review
recorded payment-webhook forgery as an OPEN item with a specific list of requirements for the day either ships:

* verify the signature over the **raw bytes** (a re-serialised body has different bytes and a valid MAC over them
  proves nothing about the body that arrived),
* **constant-time** compare,
* Stripe's `Stripe-Signature` carries a timestamp — refuse outside a **five-minute** tolerance,
* **store every event id** to refuse replays,
* Telegram Stars arrives as `successful_payment` **inside a verified update**, never as its own form post,
* and **never read the amount, the user id or the plan from the body**: cross-check against the record the provider
  keeps.

The review's phrasing for the first of those was "before either ships". Writing them now, with tests, turns a
latent hole into a control that the attack-surface probe can *run* — which is the difference between a promise in a
document and a thing that refuses a forged request.
"""
from __future__ import annotations

from polygm_core.payments.webhooks import (  # noqa: F401  (the package's public surface)
    Fulfilment,
    ParsedSignature,
    StarsPayment,
    WebhookError,
    Verified,
    fulfilment,
    parse_stripe_signature,
    sign_stripe,
    stars_payment,
    verify_stripe,
    verify_update_secret,
)

__all__ = [
    "Fulfilment",
    "ParsedSignature",
    "StarsPayment",
    "WebhookError",
    "Verified",
    "fulfilment",
    "parse_stripe_signature",
    "sign_stripe",
    "stars_payment",
    "verify_stripe",
    "verify_update_secret",
]
