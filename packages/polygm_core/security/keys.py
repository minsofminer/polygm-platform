"""D2 · the key policy, the envelope format, and the numbers a compromise response is judged on.

The decision this file encodes, in order of how much it costs to get wrong:

1. **A key that can do more than two things is a key we should not hold.** The policy allows calls to the CLOB
   exchange contract and the pUSD token on one chain id, and nothing else — no `approve` to an arbitrary
   spender, no `transferFrom`, no arbitrary payload signing. `policy_is_sufficient` is where that becomes a
   boolean a provider evaluation has to pass, and `provider_can_enforce` is where "our provider cannot express
   this" becomes a **disqualifying finding** rather than a footnote in a comparison table.
2. **What leaves the signing boundary is a signature. The key does not leave at all.** In the self-hosted
   envelope, the DEK is wrapped by a KEK held outside the DB, and the only code path that unwraps it is the
   signer, in the executor's process, for one call. `messages_wrapped` is counted per DEK because AES-GCM with
   a reused (key, nonce) pair is not "slightly weaker", it is a total loss of confidentiality and authenticity.
3. **The response is measured, not promised.** "How fast can we revoke 10,000 keys?" has an answer here as
   arithmetic over the batch size and the provider's per-call latency, and `tools/p07-drill.py` runs it against
   real processes so the number in the doc is the number in the artefact.

No seed phrase for a user's external wallet is ever stored, and no withdrawal destination is ever chosen by us.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, replace

# [UNVERIFIED — confirm before launch] the two addresses below are placeholders in this repository: they are
# the *shape* the policy constrains, not the values we deploy with. P13 replaces them from the venue's own
# documentation and the gate check compares them against a pinned file rather than this source.
# Both are 40-hex placeholders so the policy validator can be run and tested today; P13 replaces them from the
# venue's published docs and pins the result in `var/venue-addresses.json`, which the gate then compares against.
# Built from counts rather than typed, because "0x" + 39 hex characters is a policy that validates to False and
# an operator who has just survived an incident does not need that as the second surprise.
CLOB_EXCHANGE = "0x" + "0" * 36 + "c10b"          # the exchange's CLOB settlement contract
PUSD_TOKEN = "0x" + "0" * 37 + "e50"              # the pUSD ERC-20 we settle in
POLICY_VERSION = "p07.1"
ALLOWED_CHAIN_IDS: tuple[int, ...] = (137,)
# The complete set of things a platform key may ever be pointed at. A policy naming anything else is not
# "slightly looser", it is a different key for a different job, and the jobs that need more are not ours.
MAX_CALL_TARGETS: tuple[str, ...] = (CLOB_EXCHANGE, PUSD_TOKEN)


@dataclass(frozen=True)
class KeyPolicy:
    """What a key may sign. Anything not in this structure is not expressible, which is the point."""
    chain_ids: tuple[int, ...] = ALLOWED_CHAIN_IDS
    call_targets: tuple[str, ...] = (CLOB_EXCHANGE, PUSD_TOKEN)
    allow_arbitrary_call: bool = False
    allow_new_approvals: bool = False         # an approval is how a "trading" key becomes a draining key
    allow_transfer_from: bool = False
    allow_message_signing: bool = False        # personal_sign on an attacker's typed data is a signature too
    rate_limit_per_min: int = 60
    may_be_exported_by: tuple[str, ...] = ("owner",)   # never 'support', never 'admin'

    def hash(self) -> str:
        return policy_hash(self)

    def as_dict(self) -> dict:
        return {"policy_version": POLICY_VERSION, "chain_ids": list(self.chain_ids),
                "call_targets": list(self.call_targets), "allow_arbitrary_call": self.allow_arbitrary_call,
                "allow_new_approvals": self.allow_new_approvals, "allow_transfer_from": self.allow_transfer_from,
                "allow_message_signing": self.allow_message_signing, "rate_limit_per_min": self.rate_limit_per_min,
                "may_be_exported_by": list(self.may_be_exported_by)}


def policy_hash(policy: KeyPolicy) -> str:
    """Canonical JSON, keys sorted, then sha256. This is the value the executor compares against on every sign
    (`POLICY_DRIFT` in P06), so the encoding has to be stable across processes and Python versions."""
    import json
    return hashlib.sha256(json.dumps(policy.as_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


DEFAULT_POLICY = KeyPolicy()


def policy_is_sufficient(policy: KeyPolicy) -> tuple[bool, list[str]]:
    """(ok, reasons). Written as a list of reasons because a single `False` gets argued with at 3am."""
    bad: list[str] = []
    if policy.allow_arbitrary_call:
        bad.append("arbitrary calls turn a wallet key into a drain key on the first SSRF we forget to close")
    if policy.allow_new_approvals:
        bad.append("the key can approve a third party to spend the balance: no target allowlist means anything")
    if policy.allow_transfer_from:
        bad.append("transferFrom is how an approved-token bug becomes a loss")
    if policy.allow_message_signing:
        bad.append("message signing off-policy is a signature oracle (EIP-712 phishing against the venue itself)")
    if not policy.call_targets:
        bad.append("no call targets: the policy says nothing, so it enforces nothing")
    for t in policy.call_targets:
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", t or ""):
            bad.append("call target %r is not an address" % t)
    # The prompt's sentence is "the key may only call the CLOB exchange contract and the pUSD token", and *only*
    # is the part a validator has to enforce. Without this branch a policy that adds a stablecoin transfer
    # target still passes `policy_is_sufficient`, and the drift check in P06 compares hashes of a policy that
    # was never supposed to exist. Narrower is fine (a key that cannot deposit is a support ticket); wider is not.
    wider = sorted({t.lower() for t in policy.call_targets} - {t.lower() for t in MAX_CALL_TARGETS})
    if wider:
        bad.append("call targets beyond the CLOB exchange and pUSD (%s): the policy is wider than P07 permits"
                   % ", ".join(wider[:4]))
    if sorted(x.lower() for x in policy.call_targets) != sorted(set(x.lower() for x in policy.call_targets)):
        bad.append("duplicate call target (case differences hide real ones)")
    if not set(policy.chain_ids) <= set(ALLOWED_CHAIN_IDS):
        bad.append("chain ids outside %s" % (ALLOWED_CHAIN_IDS,))
    if policy.rate_limit_per_min <= 0 or policy.rate_limit_per_min > 600:
        bad.append("rate limit %r is either 'no limit' or a typo" % policy.rate_limit_per_min)
    if "support" in policy.may_be_exported_by or "admin" in policy.may_be_exported_by:
        bad.append("support or admin may export: that is the support-impersonation payout, in the policy")
    return (not bad), bad


def tighten(policy: KeyPolicy, **fixes) -> KeyPolicy:
    return replace(policy, **fixes)


def provider_can_enforce(caps: dict) -> dict:
    """Judge a provider against the policy. `{"target_allowlist": bool, "per_key_rate_limit": bool, ...}`.

    A provider that cannot restrict call targets is not "less good", it is disqualified: the whole premise of
    holding these keys is that the worst thing a compromised process can do with them is bounded.
    """
    missing: list[str] = []
    if not caps.get("target_allowlist"):
        missing.append("cannot restrict which contracts a key may call")
    if not caps.get("policy_hash_readable"):
        missing.append("cannot report the policy hash a signature was made under (P06's POLICY_DRIFT check dies)")
    if not caps.get("per_key_rate_limit"):
        missing.append("no per-key rate limit: one exploited key can mint 10^6 signatures while we sleep")
    if not caps.get("instant_revoke"):
        missing.append("revocation is not immediate (compare `revoke_all`; a 15-minute propagation is a "
                       "15-minute window on 10,000 wallets)")
    if not caps.get("export_requires_user"):
        missing.append("export can be triggered without the end user: disqualifying, not a preference")
    disqualifying = not caps.get("target_allowlist") or not caps.get("export_requires_user")
    return {"verdict": "DISQUALIFYING" if disqualifying else ("acceptable" if not missing else "conditional"),
            "missing": missing, "note": "every capability here is [UNVERIFIED] against a vendor until P13; this "
                                        "function encodes what we require, not what anyone has promised"}


# ------------------------------------------------------------------------------ the envelope (self-host) ----
NONCE_BYTES = 12                     # AES-GCM standard 96-bit
TAG_BYTES = 16
KEY_BYTES = 32                       # AES-256
WRAPPED_LEN = KEY_BYTES + TAG_BYTES  # a DEK wrapped under a KEK is 48 bytes; assert it, do not hope
NONCE_BUDGET = 2**32 - 1             # 2^32 messages per (key, nonce-set); below the NIST guidance for GCM
DEK_GENERATION = "aes-256-gcm/envelope-v1"


@dataclass(frozen=True)
class Wrapped:
    ciphertext: bytes
    nonce: bytes
    tag: bytes
    kek_version: int
    dek_version: int

    def as_row(self) -> dict:
        import base64
        return {"wrapped_dek": base64.b64encode(self.ciphertext).decode(),
                "nonce": base64.b64encode(self.nonce).decode(), "tag": base64.b64encode(self.tag).decode(),
                "kek_version": self.kek_version, "dek_version": self.dek_version}


def new_dek(rng_bytes: bytes) -> bytes:
    raw = bytes(rng_bytes)
    if len(raw) < KEY_BYTES:
        raise ValueError("a DEK is %d bytes; the caller passed %d — refusing to generate a short key"
                         % (KEY_BYTES, len(raw)))
    return raw[:KEY_BYTES]


def check_nonce(nonce: bytes) -> tuple[bool, str]:
    n = bytes(nonce)
    if len(n) != NONCE_BYTES:
        return False, "a GCM nonce must be %d bytes, got %d" % (NONCE_BYTES, len(n))
    if not any(n):
        return False, "an all-zero nonce is how a broken counter starts"
    return True, ""


def nonce_budget_ok(messages_wrapped: int, extra: int = 1) -> tuple[bool, str]:
    """Refuse to wrap with a key that has run out of safe nonces. The refusal is the control; the re-wrap is
    the rotation ceremony, and neither is optional."""
    used = int(messages_wrapped or 0) + int(extra)
    if used > NONCE_BUDGET:
        return False, ("DEK has wrapped %d messages (budget %d): rotate before the next wrap. A reused (key, "
                       "nonce) pair in GCM is not weaker encryption, it is no encryption."
                       % (used, NONCE_BUDGET))
    return True, ""


def encrypt_error_names() -> tuple[str, ...]:
    """The exception names the backend must translate. Catching them by name at the boundary is how a tampered
    blob becomes `KEYSTORE_TAMPER` (a security event, an alarm, a suspension) instead of a 500."""
    return ("InvalidTag", "ValueError", "DecryptionError")


def unwrap_verifies_aad(aad: dict) -> bool:
    """The AAD binds the blob to its row: user id + versions + policy hash. Without it, a wrapped DEK copied
    between rows decrypts — which is how "we got the wrong user's key" becomes possible at all."""
    return bool(aad.get("user_id") and aad.get("policy_hash") and "kek_version" in aad and "dek_version" in aad)


# ------------------------------------------------------------------------- rotation, break-glass, export ----
MIN_APPROVERS = 2
BREAK_GLASS_COOLDOWN_MS = 0            # an emergency path with a cooldown is not an emergency path
BREAK_GLASS_WINDOW_MS = 30 * 60 * 1000  # ...but it is time-boxed, logged, and every session it mints dies with it


def break_glass_ok(approvers: list[str], *, reason: str, now_ms: int, opened_ms: int | None = None) -> dict:
    """Two named humans, a ≥20-character reason, and a 30-minute window. An empty or duplicated approver list
    is refused: 'me and the runbook' is one approver."""
    names = [str(a).strip() for a in (approvers or []) if str(a).strip()]
    uniq = sorted({n.lower() for n in names})
    reasons = {"approvers": "need %d distinct approvers, got %s" % (MIN_APPROVERS, uniq or "none")
               if len(uniq) < MIN_APPROVERS else "",
               "reason": "a break-glass reason must be at least 20 characters (it is the audit trail)"
               if len((reason or "").strip()) < 20 else "",
               "window": "the break-glass window expired"
               if opened_ms is not None and now_ms - int(opened_ms) > BREAK_GLASS_WINDOW_MS else ""}
    return {"ok": not any(reasons.values()), "denied": {k: v for k, v in reasons.items() if v},
            "audit": "the moment the window closes, every break-glass session is revoked by the same call"}


def rotation_plan(rows: list[dict], *, new_kek_version: int, batch: int = 200) -> dict:
    """Re-wrap every DEK under the new KEK, verify each one, and refuse to retire the old KEK until every row
    has been read back with the new key.

    The order is the whole procedure: a rotation that deletes the old KEK after "successfully" writing the new
    wrap is how you lose every key in the table when the write succeeded and the *content* was wrong.
    """
    live = [r for r in rows if not r.get("revoked_ms")]
    return {"total": len(live), "batches": (len(live) + batch - 1) // batch, "batch": batch,
            "steps": ("create kek_versions row for v%d" % new_kek_version,
                      "for each DEK: unwrap under v%d, seal under v%d, write both the new row and a journal "
                      "entry" % (new_kek_version - 1, new_kek_version),
                      "re-read every new wrap and unwrap it, before anything is deleted",
                      "retire v%d only when `unverified == 0 and unwrapped_ok == total`" % (new_kek_version - 1)),
            "refuse_retire_if": ("unverified > 0", "any row still pointing at the old KEK",
                                  "the journal is non-empty"),
            "idempotent": "re-running with the same journal resumes; it never re-wraps a row already verified"}


def can_retire_kek(rows: list[dict], kek_version: int) -> tuple[bool, str]:
    still = sum(1 for r in rows if int(r.get("kek_version") or 0) == int(kek_version) and not r.get("revoked_ms"))
    if still:
        return False, "%d live key wraps are still under KEK v%d: retiring it now makes them unreadable" % (
            still, kek_version)
    return True, "no live wraps reference it"


def export_gate(*, state: str, open_orders: int, unfinished_intents: int, pending_withdrawal: bool,
                delayed_deposit: bool, totp_ok: bool, requested_by: str) -> tuple[bool, str]:
    """The P06 rule (two owners of one wallet means one of them is surprised) plus the second factor and the
    identity of the requester. `requested_by` is checked against the *policy*, so an admin cannot ask.
    """
    if not totp_ok:
        return False, ("TOTP_REQUIRED: a key export with a 6-digit code behind it is the only thing standing "
                       "between a session token and the wallet")
    if open_orders > 0 or unfinished_intents > 0:
        return False, "WALLET_BUSY: %d open order(s), %d unfinished intent(s)" % (open_orders, unfinished_intents)
    if pending_withdrawal:
        return False, "WITHDRAWAL_IN_FLIGHT: the export would move the money under a pending payout"
    if delayed_deposit:
        return False, "DEPOSIT_UNRECONCILED: we do not hand over custody of a balance we cannot yet explain"
    if state not in ("funded", "trading", "closing"):
        return False, "STATE: exporting from state %r proves nothing about what the key is guarding" % state
    if requested_by not in DEFAULT_POLICY.may_be_exported_by:
        return False, "REQUESTER: only %s may export, and %r is not them" % (
            list(DEFAULT_POLICY.may_be_exported_by), requested_by)
    return True, "ok"


# -------------------------------------------------------------------------------- compromise response ----
REVOKE_BATCH_DEFAULT = 500
# The number the doc quotes and the drill re-measures: 10,000 keys. It is not a hypothetical size, it is the
# first year's plan, so "we would be fine" is checked against a run.
REVOCATION_TARGET_KEYS = 10_000


def revocation_throughput(total: int = REVOCATION_TARGET_KEYS, *, batch: int = REVOKE_BATCH_DEFAULT,
                          per_call_ms: int = 120, concurrency: int = 4) -> dict:
    """How long it takes to revoke N keys, from the batch size and the provider's latency — with the honest
    caveat that the provider's own rate limit, not our arithmetic, is usually the binding constraint.
    """
    calls = (int(total) + int(batch) - 1) // int(batch) if total else 0
    wall = int(calls) * int(per_call_ms) // max(1, int(concurrency))
    return {"keys": int(total), "batches": int(batch), "calls": int(calls), "per_call_ms": int(per_call_ms),
            "concurrency": int(concurrency), "wall_ms": int(wall), "wall_s": round(wall / 1000, 1),
            "bounded_by": "the provider's revoke rate limit ([UNVERIFIED] until P13); if it is 1 call/s and "
                          "batch=1, this arithmetic is a fantasy and the answer is %.1f hours"
                          % (calls * 1000 / 3600000.0)}


def compromise_steps(revoked_sessions: int = 0) -> tuple[str, ...]:
    """The first 60 minutes, in the order that limits loss. See `incident.py` for the same list with owners."""
    return ("engage the kill switch (stops new orders, sweeps the queue)",
            "revoke every session and every refresh token (the attacker's foothold, not the key)",
            "freeze withdrawals and exports at the policy level (not by asking the provider politely)",
            "revoke the affected keys at the provider; then all of them if the blast radius is unknown",
            "preserve: snapshot the DB, the auth_events window, the executor's log files, and the venue's "
            "order list; write the hash of each snapshot into the incident record",
            "notify users with the template in `incident.py` — not after the forensics, at the 60-minute mark")
