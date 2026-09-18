"""D3 · Telegram Mini App `initData`: the check that decides whether "I am user 123" is true.

A broken check here means anyone can impersonate any Telegram user with a stolen id, so the algorithm is written
against Telegram's documented construction rather than from memory — and the difference matters. The two
details that are easy to get wrong and impossible to notice when you do:

* **The key is `HMAC-SHA256(key="WebAppData", msg=bot_token)`**, not the bot token itself and not
  `SHA256(bot_token)`. A wrong key does not fail loudly: it fails *every* login, and the first sign is usually
  a "Telegram is down" report from a user whose traffic is perfectly valid.
* **The check string is the URL-decoded `key=value` lines, sorted alphabetically, joined with `\\n`, with
  `hash` (and `signature`, for third-party data) removed.** Sorting the *encoded* pairs, or leaving `hash` in,
  or joining with `&`, all produce a string that verifies nothing.

Freshness and replay are ours, not Telegram's: `auth_date` is checked (5 minutes for a login, 24 hours for a
data refresh, 60 s of tolerated future skew) and a payload that has already bought a session is refused by
`telegram_nonces`, because a captured query string is otherwise valid forever.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl

WEBAPP_DATA = b"WebAppData"
EXCLUDED = frozenset({"hash", "signature"})
LOGIN_MAX_AGE_S = 300              # a login must be minted now
REFRESH_MAX_AGE_S = 24 * 60 * 60   # `sendData` inside an open app may legitimately be a day old
FUTURE_SKEW_S = 60                 # a phone's clock ahead of ours by a minute is a clock, not an attack
TOKEN_RE = re.compile(r"^\d{4,12}:[A-Za-z0-9_-]{30,}$")
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


class InitDataError(ValueError):
    """Raised only for *malformed* input. A wrong signature is a result, not an exception: the caller has to
    log it, count it, and possibly lock something, and an exception path invites a broad `except` that turns a
    failed check into a passed one."""


@dataclass(frozen=True)
class Verified:
    ok: bool
    reason: str
    tg_user_id: str = ""
    first_name: str = ""
    username: str = ""
    auth_date: int = 0
    auth_hash: str = ""
    age_s: int = 0
    fields: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "tg_user_id": self.tg_user_id, "username": self.username,
                "age_s": self.age_s, "auth_hash": self.auth_hash[:16]}


def check_secret_key(bot_token: str) -> bytes:
    """`secret_key = HMAC_SHA256("WebAppData", bot_token)`."""
    token = (bot_token or "").strip()
    if not TOKEN_RE.match(token):
        # A malformed token is a deployment mistake. Refuse loudly rather than computing a signature over a
        # token nobody meant to configure: with an empty key here, every payload with an empty HMAC matches.
        raise InitDataError("PGM_TELEGRAM_BOT_TOKEN is missing or malformed (expected '<bot_id>:<32+ chars>')")
    return hmac.new(WEBAPP_DATA, token.encode(), hashlib.sha256).digest()


def parse(query: str) -> dict:
    """Decoded key/value pairs, first occurrence of each key winning (Telegram sends each once; a repeated
    key means something is being smuggled)."""
    if not isinstance(query, str) or not query.strip():
        raise InitDataError("empty initData")
    out: dict[str, str] = {}
    for k, v in parse_qsl(query.strip().lstrip("?"), keep_blank_values=True):
        if k not in out:
            out[k] = v
    if not out:
        raise InitDataError("initData parsed to no fields")
    return out


def check_string(fields: dict) -> str:
    """The data-check-string: decoded `key=value` lines, alphabetical, `\\n`-joined, `hash`/`signature` out."""
    lines = ["%s=%s" % (k, fields[k]) for k in sorted(fields) if k not in EXCLUDED]
    return "\n".join(lines)


def third_party_check_string(bot_id: int, fields: dict) -> str:
    """`{bot_id}:WebAppData\\n` then the same sorted lines — the form Telegram's Ed25519 signature covers, so a
    third party (the P16 growth partner, the affiliate report) can verify a payload without our bot token.
    """
    return "%d:%s\n%s" % (int(bot_id), WEBAPP_DATA.decode(), check_string(fields))


def sign(fields: dict, bot_token: str) -> str:
    """Mint a hash for `fields`. Test-only, and marked as such: production code must never sign initData, and a
    helper that can is one bad import away from being a login bypass."""
    return hmac.new(check_secret_key(bot_token), check_string(fields).encode(), hashlib.sha256).hexdigest()


def auth_hash(query: str) -> str:
    return hashlib.sha256((query or "").encode()).hexdigest()


def verify(query: str, bot_token: str, *, at: int, purpose: str = "login",
           seen_hashes=()) -> Verified:
    """Verify `initData` and, for a login, consume it so it cannot be presented twice.

    `seen_hashes` is a callable (hash -> bool) supplied by the caller: this module must not know about SQLite,
    because the same check runs in the API, in the bot webhook, and in the gate — each with a different store.
    """
    fields = parse(query)
    got = str(fields.get("hash") or "")
    if not got:
        return Verified(False, "no_hash")
    if not _HEX64.match(got):
        return Verified(False, "malformed_hash")
    want = hmac.new(check_secret_key(bot_token), check_string(fields).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got):
        return Verified(False, "bad_signature")
    raw_date = str(fields.get("auth_date") or "")
    if not raw_date.isdigit():
        return Verified(False, "no_auth_date")
    ts = int(raw_date)
    now_s = int(at) // 1000
    age = now_s - ts
    limit = LOGIN_MAX_AGE_S if purpose == "login" else REFRESH_MAX_AGE_S
    if age < -FUTURE_SKEW_S:
        return Verified(False, "auth_date_in_the_future")
    if age > limit:
        return Verified(False, "stale" if purpose != "login" else "too_old")
    try:
        user = json.loads(fields.get("user") or "{}")
    except json.JSONDecodeError:
        return Verified(False, "malformed_user")
    if not isinstance(user, dict) or "id" not in user:
        return Verified(False, "no_user")
    uid = str(user["id"])
    if not uid.isdigit():
        return Verified(False, "malformed_user_id")
    h = auth_hash(query)
    # `seen_hashes` is documented as either an iterable of hashes (the API's store hands us a set) or a
    # predicate, because a store that keeps the nonces somewhere expensive should be able to answer the one
    # question we ask it. Accepting only the iterable and crashing on the callable would be a 500 in a login
    # path, which is how "replay defence" quietly becomes "replay defence is disabled by the retry handler".
    try:
        already = bool(seen_hashes(h)) if callable(seen_hashes) else (h in set(seen_hashes or ()))
    except TypeError:
        raise InitDataError("seen_hashes must be an iterable of hashes or a callable") from None
    if purpose == "login" and seen_hashes is not None and already:
        # A valid signature is valid forever unless somebody remembers it was used. This is the branch that
        # turns a replayed capture into a refused login, and it is the reason the caller must pass a store and
        # not the default.
        return Verified(False, "replayed", tg_user_id=uid, auth_date=ts, auth_hash=h, age_s=age)
    return Verified(True, "ok", tg_user_id=uid, first_name=str(user.get("first_name") or ""),
                    username=str(user.get("username") or ""), auth_date=ts, auth_hash=h, age_s=age,
                    fields={k: v for k, v in fields.items() if k not in EXCLUDED})


def binding_token(verified: Verified) -> str:
    """What we hand the client as a session seed: a hash over the payload *and* our own verification, so a
    client cannot present a different payload it swapped in later."""
    return hashlib.sha256(("bind|%s|%s|%s" % (verified.tg_user_id, verified.auth_hash, verified.auth_date))
                          .encode()).hexdigest()[:32]
