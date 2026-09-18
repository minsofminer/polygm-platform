"""D3 · TOTP (RFC 6238) with the two things implementations usually forget: replay and the attempt budget.

Mandatory for withdrawals, key export, adding a withdrawal address, and admin break-glass. Optional at login,
because forcing it at login moves users to a weaker practice (a shared "company authenticator" on a work
phone) and the thing it protects is *money leaving*, not reading a leaderboard.

No SMS, anywhere: the SS7/SIM-swap class of attacks is exactly our threat model (an attacker who wants one
account and is willing to spend an afternoon on a carrier store), and a support channel that can re-send an SMS
code is a support channel that can move money.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct
from dataclasses import dataclass

PERIOD_S = 30
DIGITS = 6
LEEWAY_STEPS = 1                     # one step either side: 90 s of acceptance, not 30
MAX_ATTEMPTS = 5                     # then the enrollment is locked, not the account
LOCK_MS = 15 * 60 * 1000
SECRET_BYTES = 20                    # 160 bits, the RFC's minimum for HMAC-SHA1

# Withdrawals, key export, address changes, break-glass. A second factor on *reads* would be theatre: nothing
# an attacker can read lets them take money, and every extra prompt trains the user to approve without looking.
REQUIRED_FOR: tuple[str, ...] = ("withdraw", "key_export", "address_add", "address_remove", "break_glass",
                                 "revoke_keys", "un-halt")


class TotpError(ValueError):
    pass


def new_secret(rng_bytes: bytes) -> str:
    """base32 of exactly 20 bytes, unpadded, uppercase — the shape every authenticator app expects.

    `rng_bytes` is injected rather than drawn from `secrets` here so that the *policy* (length, alphabet) is
    testable without a randomness hook, and so the caller cannot accidentally pass 8 bytes. The service layer
    passes `secrets.token_bytes(20)`.
    """
    raw = bytes(rng_bytes)
    if len(raw) < SECRET_BYTES:
        raise TotpError("a TOTP secret must be at least %d bytes; got %d" % (SECRET_BYTES, len(raw)))
    return base64.b32encode(raw[:SECRET_BYTES]).decode("ascii").rstrip("=")


def _key(secret_b32: str) -> bytes:
    s = re.sub(r"[\s-]", "", (secret_b32 or "").strip()).upper()
    if not s:
        raise TotpError("no secret")
    try:
        raw = base64.b32decode(s + "=" * ((8 - len(s) % 8) % 8), casefold=True)
    except Exception as exc:                                 # noqa: BLE001 - the message is the point
        raise TotpError("the stored secret is not valid base32: %s" % type(exc).__name__) from exc
    if len(raw) < SECRET_BYTES:
        raise TotpError("the stored secret is short (%d bytes); re-enrol" % len(raw))
    return raw


def hotp(key: bytes, counter: int, digits: int = DIGITS) -> str:
    msg = struct.pack(">Q", int(counter) & (2**64 - 1))
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    val = struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFF_FFFF
    return str(val % 10**digits).zfill(digits)


def code_for(secret_b32: str, at: int, *, period_s: int = PERIOD_S, digits: int = DIGITS) -> str:
    return hotp(_key(secret_b32), int(at) // (1000 * period_s), digits)


def step_for(at: int, *, period_s: int = PERIOD_S) -> int:
    return int(at) // (1000 * period_s)


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: str
    step: int = -1
    retry_after_ms: int = 0

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "retry_after_ms": self.retry_after_ms}


_CODE_RE = re.compile(r"^[0-9]{6}$|^[0-9]{8}$")


def verify(secret_b32: str, presented: str, *, at: int, last_step: int = -1, digits: int = DIGITS,
           period_s: int = PERIOD_S, leeway: int = LEEWAY_STEPS, attempts: int = 0, locked_until_ms: int = 0,
           limit: int = MAX_ATTEMPTS) -> VerifyResult:
    """The one function a route may call. Every way to get this wrong is a way to lose money, so all of them
    are named in the return value rather than collapsed into `False`.

    * `locked`   — the enrollment is throttled; the code is not even looked at.
    * `reused`   — a *correct* code for a step at or before the last one accepted. Without this rule a code
                   seen over a shoulder, in a screenshot, or on a phishing page is a withdrawal for up to
                   `period * (2*leeway+1)` seconds, and the attacker needs only one of them.
    * `expired`  — correct but older than the window (clock drift on the user's phone: say so, do not just fail).
    * `bad_code` — wrong. Counted, and the count is what trips the lock.
    """
    if locked_until_ms and int(at) < int(locked_until_ms):
        return VerifyResult(False, "locked", retry_after_ms=int(locked_until_ms) - int(at))
    # Past the window, the counter is void. Without this line the stored `attempts` keeps re-arming the lock
    # forever: the honest reading of "locked for 15 minutes" is that after 15 minutes you get 15 minutes again,
    # not that you never get another try.
    if locked_until_ms:
        attempts = 0
    if attempts >= limit:
        return VerifyResult(False, "locked", retry_after_ms=LOCK_MS)
    got = re.sub(r"[\s-]", "", str(presented or ""))
    if not _CODE_RE.match(got) or len(got) != digits:
        return VerifyResult(False, "malformed_code")
    cur = step_for(at, period_s=period_s)
    key = _key(secret_b32)
    want_lo, want_hi = cur - leeway, cur + leeway
    for st in range(max(0, want_lo), want_hi + 1):
        if hmac.compare_digest(hotp(key, st, digits), got):
            if st <= int(last_step):
                return VerifyResult(False, "reused")
            return VerifyResult(True, "ok", step=st)
    # A match outside the window at all means the user's clock (or ours) has moved; the distinction matters to
    # support, and the answer is still no.
    lo = step_for(at, period_s=period_s) - 3 * leeway - 1
    for st in range(max(0, lo), max(0, want_lo)):
        if hmac.compare_digest(hotp(key, st, digits), got):
            return VerifyResult(False, "expired")
    return VerifyResult(False, "bad_code")


def attempt_state(attempts: int, *, limit: int = MAX_ATTEMPTS) -> dict:
    """What a caller stores after any verification: the new count, and whether it just tripped the lock."""
    n = int(attempts or 0)
    return {"failed_count": 0 if n < limit else n, "locked": n >= limit, "tries_left": max(0, limit - n)}


def provisioning_uri(*, label: str, secret_b32: str, issuer: str = "Openout", digits: int = DIGITS,
                     period_s: int = PERIOD_S) -> str:
    """`otpauth://` for the authenticator app. The label is URL-escaped because user ids can contain `:` and
    `/`, and an unescaped colon is how an app ends up showing a different issuer than the one that issued."""
    from urllib.parse import quote
    return ("otpauth://totp/%s:%s?secret=%s&issuer=%s&algorithm=SHA1&digits=%d&period=%d"
            % (quote(issuer, safe=""), quote(label, safe=""), secret_b32, quote(issuer, safe=""), digits,
               period_s))


def is_mandatory(action: str) -> bool:
    return action in REQUIRED_FOR
