"""D4 · authorisation as data, so that "an endpoint with no decision" is a red gate and not a review miss.

Three ideas carry the weight here:

1. **Every operation declares a level.** `LEVELS` is the registry, and `route_auth_levels` is its mirror in the
   database. `coverage()` reconciles both against the *served* OpenAPI document, so shipping a route without a
   decision fails a check that cannot be argued with — a `None` in a table is invisible in a code review, a
   missing row in a diff is not.
2. **Ownership failures answer 404, never 403.** A 403 says "this exists and it is not yours", which is a
   enumeration oracle for market ids, intent ids and referral codes. `assert_owns` returns a boolean and the
   caller is expected to render the same body it renders for a genuinely missing resource.
3. **Admin is a role, not a bypass.** `ADMIN_FORBIDDEN` names the actions no admin endpoint may take, and the
   gate asserts none of them is reachable from an `/v1/admin/*` path. The reason this is a *list* rather than a
   sentence is that the tempting version of the admin tooling is exactly the one that skips the risk gate.
"""
from __future__ import annotations
import re

from dataclasses import dataclass

PUBLIC, USER, OWNS, ADMIN, SERVICE = "public", "user", "user-owns-resource", "admin", "service"
LEVELS: tuple[str, ...] = (PUBLIC, USER, OWNS, ADMIN, SERVICE)

# "METHOD /path" -> (level, the id an ownership test is applied to). Public is a decision too: it is the
# decision that an anonymous caller may see this, and it is the one that gets changed by accident.
LEVELS_TABLE: dict[str, tuple[str, str]] = {
    "GET /healthz": (PUBLIC, ""),
    "GET /readyz": (PUBLIC, ""),
    "GET /openapi.json": (PUBLIC, ""),
    "GET /v1/meta/status": (PUBLIC, ""),
    "GET /v1/search": (PUBLIC, ""),
    "GET /v1/tape": (PUBLIC, ""),
    "GET /v1/markets/{market_id}": (PUBLIC, ""),
    "GET /v1/markets/{market_id}/book": (PUBLIC, ""),
    # P09's three read surfaces. All PUBLIC for the same reason the book is: they are market data, and the
    # one thing they must never contain is somebody's address - holders are pseudonymised exactly as /v1/tape
    # pseudonymises them, which is why a `holders` route can be public at all.
    "GET /v1/markets/{market_id}/history": (PUBLIC, ""),
    "GET /v1/markets/{market_id}/holders": (PUBLIC, ""),
    "GET /v1/events/{event_id}": (PUBLIC, ""),
    "GET /v1/markets/{market_id}/fills": (PUBLIC, ""),
    "POST /v1/auth/login": (PUBLIC, ""),
    "POST /v1/auth/telegram": (PUBLIC, ""),
    "POST /v1/auth/refresh": (PUBLIC, ""),                # the refresh token *is* the credential
    "POST /v1/auth/logout": (USER, ""),
    "GET /v1/auth/sessions": (USER, ""),
    "POST /v1/auth/sessions/revoke": (USER, ""),
    "POST /v1/auth/totp/enroll": (USER, ""),
    "POST /v1/auth/totp/verify": (USER, ""),
    "GET /v1/markets": (PUBLIC, ""),
    "GET /v1/books": (PUBLIC, ""),
    "GET /v1/alerts/feed": (PUBLIC, ""),
    "GET /v1/me/positions": (USER, ""),
    "GET /v1/me/activity": (USER, ""),
    "POST /v1/orders": (USER, ""),
    "GET /v1/orders": (USER, ""),
    "GET /v1/orders/{intentId}": (OWNS, "intent_id"),
    "GET /v1/orders/intents/{intent_id}": (USER, "scoped:intent_id"),
    "POST /v1/orders/{intentId}/cancel": (OWNS, "intent_id"),
    # The two address routes are `POST …/add` and `POST …/remove` because one OpenAPI path carries one schema,
    # and they are `USER` with a `scoped:` note rather than `OWNS`: the owner is not knowable before the handler
    # runs, so the object check is the store's own `WHERE id=? AND user_id=?`, which answers 404 for a foreign
    # id. `tools/p07-gate-check.py` requires a test per `scoped:` route for exactly that reason - the level
    # alone does not prove the query is scoped.
    "GET /v1/wallet/withdrawal-addresses": (USER, ""),
    "POST /v1/wallet/withdrawal-addresses/add": (USER, "scoped:withdrawal_address_id"),
    "POST /v1/wallet/withdrawal-addresses/remove": (USER, "scoped:withdrawal_address_id"),
    "POST /v1/wallet/withdraw": (USER, ""),
    # P08 wrote this row as `POST /v1/wallet/export` and never served it. P12 D6 serves the export at
    # `POST /v1/wallet/keys/export` (the ledger, the contract and the route all say `keys/`), so the old
    # spelling is GONE rather than kept beside it: two rows for one endpoint, one of them dead, is exactly
    # the corpse this table's staleness check exists to find.
    "GET /v1/copy/record": (PUBLIC, ""),
    # P06 wrote this row as `POST /v1/copy/config` (singular) and never served it. P10 serves the collection:
    # `POST /v1/copy/configs` creates, `GET /v1/copy/configs` lists, and both are declared with the P10 block
    # at the bottom of this table. The singular key is GONE rather than kept "just in case" - a registry that
    # keeps a row for a path nobody serves is the corpse the coverage check exists to find, and the plural is a
    # different path, not a spelling of this one.
    "POST /v1/automation/rules": (USER, ""),
    "POST /v1/admin/kill-switch": (ADMIN, ""),
    "POST /v1/admin/revoke-sessions": (ADMIN, ""),
    "POST /v1/admin/revoke-keys": (ADMIN, ""),
    "POST /v1/admin/flags": (ADMIN, ""),
}

# Actions no admin route may perform, and the sentence to put in the review when someone asks for one.
ADMIN_FORBIDDEN: tuple[str, ...] = (
    "place_order",                     # admin tooling goes through the risk gate like everyone else
    "bypass_risk_gate",
    "move_user_funds",                 # a withdrawal destination is never chosen by us
    "delete_ledger_row",               # append-only means append-only, including for the person on call
    "skip_withdrawal_cooldown",
    "un-enroll_totp",                  # support cannot turn off the thing that stops them
    "export_key_without_user",
)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    status: int
    code: str
    reason: str = ""

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "status": self.status, "code": self.code, "reason": self.reason}


def _op_pattern(operation: str) -> "re.Pattern[str]":
    """`GET /v1/orders/{intentId}` must match the served `GET /v1/orders/{intent_id}`.

    A registry keyed on strings that the router never emits is worse than no registry: every lookup misses, and
    a miss is either a 401 on a public route (noticed in a day) or, if the fallback were ever "allow", an open
    door (noticed never). Placeholders are therefore wildcards on both sides, and the match is anchored.
    """
    out, i = [], 0
    while i < len(operation):
        j = operation.find("{", i)
        if j < 0:
            out.append(re.escape(operation[i:])); break
        out.append(re.escape(operation[i:j]))
        k = operation.find("}", j)
        out.append(r"\{[^/]+\}")
        i = k + 1
    return re.compile("^" + "".join(out) + "$")


_MATCHERS: list[tuple["re.Pattern[str]", str]] = []


def _matchers() -> list[tuple["re.Pattern[str]", str]]:
    global _MATCHERS
    if not _MATCHERS:
        _MATCHERS = [(_op_pattern(op), op) for op in sorted(LEVELS_TABLE)]
    return _MATCHERS


def lookup(operation: str) -> tuple[str, str, str] | None:
    """(the table's key, level, object check) for a served operation, or None when nothing declares it."""
    hit = LEVELS_TABLE.get(operation)
    if hit is not None:
        return operation, hit[0], hit[1]
    for rx, key in _matchers():
        if rx.match(operation):
            return key, LEVELS_TABLE[key][0], LEVELS_TABLE[key][1]
    return None


def level_for(operation: str) -> str | None:
    hit = lookup(operation)
    return hit[1] if hit else None


def coverage(operations) -> dict:
    """Reconcile a list of served operations against the registry.

    `undeclared` is the security finding (a route exists with no decision). `stale` is the honesty finding (the
    registry claims a route that the API does not serve): both are kept, because a table that is allowed to
    gather corpses is a table nobody trusts.
    """
    served = {o.strip() for o in operations if o and o.strip()}
    declared = set(LEVELS_TABLE)
    bad_level = {op: lv for op, (lv, _c) in LEVELS_TABLE.items() if lv not in LEVELS}
    # `served - declared` is exact-string; a placeholder rename on one side is not a security hole, so the
    # pattern matcher decides. `stale` is computed the same way, and the *test* asserts both halves.
    matched = {k for o in served for (op, k) in ((o, lookup(o)) for _ in [0]) if op == o and k}
    matched = {lookup(o)[0] for o in served if lookup(o)}
    return {"served": len(served), "declared": len(declared),
            "undeclared": sorted(o for o in served if lookup(o) is None),
            "stale": sorted(declared - matched),
            "bad_level": bad_level,
            "admin_routes": sorted(op for op, (lv, _c) in LEVELS_TABLE.items() if lv == ADMIN)}


def assert_owns(authed_user: str | None, owner_id: str | None) -> bool:
    """True only when both sides exist and match. An empty `owner_id` is *not* a public resource."""
    return bool(authed_user) and bool(owner_id) and str(authed_user) == str(owner_id)


def require(operation: str, *, user_id: str | None = None, resource_owner: str | None = None,
            is_admin: bool = False, is_service: bool = False, at_ms: int | None = None,
            credential_gen: int | None = None, session_cred_gen: int | None = None) -> Decision:
    """The single entry point every route calls, so every route can be checked to call it.

    `session_cred_gen` vs `credential_gen` is the immediate-invalidation rule: a password change bumps the
    credential generation and any session minted before it stops resolving, without needing to find them.
    """
    hit = lookup(operation)
    if hit is None:
        return Decision(False, 500, "AUTHZ_UNDECLARED", "%s has no declared auth level" % operation)
    _key, level, object_check = hit
    if level == PUBLIC:
        return Decision(True, 200, "OK")
    if not user_id and level in (USER, OWNS):
        return Decision(False, 401, "UNAUTHENTICATED", "a session is required")
    if session_cred_gen is not None and credential_gen is not None and session_cred_gen != credential_gen:
        return Decision(False, 401, "SESSION_STALE", "credentials changed since this session was issued")
    if level == USER:
        return Decision(True, 200, "OK")
    if level == OWNS:
        if not assert_owns(user_id, resource_owner):
            # 404, not 403: see the module docstring. The body must be indistinguishable from a real miss.
            return Decision(False, 404, "NOT_FOUND", "no such %s" % (object_check or "resource"))
        return Decision(True, 200, "OK")
    if level == ADMIN:
        return Decision(is_admin, 403 if not is_admin else 200, "ADMIN_REQUIRED" if not is_admin else "OK",
                        "" if is_admin else "this route is admin-only")
    if level == SERVICE:
        return Decision(is_service, 403 if not is_service else 200, "SERVICE_REQUIRED" if not is_service else "OK",
                        "" if is_service else "this route is service-to-service")
    return Decision(False, 500, "AUTHZ_UNKNOWN_LEVEL", "level %r is not one of %s" % (level, LEVELS))


def admin_may_not(action: str) -> tuple[bool, str]:
    """(is_forbidden, why). Called by the admin surface; the gate calls it for every action it can name."""
    if action in ADMIN_FORBIDDEN:
        return True, ("`%s` is on the admin-forbidden list: admin tooling that can do it is a support-impersonation "
                      "payout with a login. If this is genuinely needed, it goes through the user's own "
                      "authenticated flow with a second approver." % action)
    return False, ""


def check_service_token(presented: str, expected: str) -> bool:
    """Constant-time compare, and an empty expected is *no* configured secret, not "match anything".

    The executor must reject an intent that did not come from the API. If the environment forgot to set the
    shared secret, the safe answer is to reject everything, and a service that fails closed on its own
    misconfiguration is the difference between an outage and a breach.
    """
    import hmac
    if not expected or len(expected) < 32:
        return False
    if not presented:
        return False
    return hmac.compare_digest(str(presented), str(expected))


def ip_hash(ip: str, pepper: str) -> str:
    """A truncated keyed hash. Enough to cluster "three accounts, one address", useless for reconstructing one.

    12 hex chars is deliberate: 2^48 space against a table of ~10^5 users is collision-free for clustering and
    too expensive to brute force per row. Storing the address itself would make our own database the thing a
    subpoena (or a leak) is embarrassing about.
    """
    import hashlib
    if not ip:
        return ""
    return hashlib.sha256(("%s|%s" % (pepper or "unset-pepper", ip.strip())).encode()).hexdigest()[:12]


def is_address_allowlisted(usable_ms: int | None, at_ms: int) -> bool:
    """The cooldown is a single integer comparison against a value set when the address was confirmed.

    Keeping the arithmetic here rather than in each route is what makes the gate able to test it once and mean
    it everywhere. `usable_ms is None` means "never confirmed", which is False, not "usable now".
    """
    return usable_ms is not None and at_ms >= int(usable_ms)


COOLDOWN_MS: int = 24 * 60 * 60 * 1000          # D3: 24 h on a new withdrawal destination
MAX_ADDRESSES_PER_USER: int = 10
