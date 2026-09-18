"""D3 · the password rules that do not need a library, and the parameters the library must be given.

`hash`/`verify` live in `services/api/security_backends.py` because they call Argon2. What lives here is the
part that has an opinion: what a decent parameter set is *today*, how to read the parameters back out of a
stored hash, and when a stored hash is stale enough to re-hash on the next good login.

Why parameters are stored per credential rather than configured globally: the day we raise `memory_kib`, every
hash minted under the old setting is still verifiable (Argon2 is self-describing) and every one of them gets
quietly upgraded on a successful login. A global setting would either break existing logins or leave the old
hashes rotting in the table forever, which is the situation "we use Argon2" is supposed to prevent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# OWASP's 2024 minimum for Argon2id is m=19456 KiB, t=2, p=1; the recommended figure for an interactive login
# where ~100 ms of CPU is acceptable is m=64 MiB, t=3, p=4. We choose the recommendation and pin the minimum in
# the schema (`password_credentials.memory_kib >= 19456`, `time_cost >= 2`) so a misconfigured deploy cannot
# quietly write a fast hash — the CHECK and these constants are asserted to agree by
# `tests/test_security_auth.py::test_policy_and_schema_agree`.
PARAMS: dict[str, int] = {"memory_kib": 65_536, "time_cost": 3, "parallelism": 4, "hash_len": 32, "salt_len": 16}
MIN_MEMORY_KIB = 19_456
MIN_TIME_COST = 2
MIN_PARALLELISM = 1

MIN_LEN = 12                      # not 8: our threat model includes offline cracking of a stolen hash table
MAX_LEN = 1024                    # hashed in full; longer than this is a paste accident, refused not truncated
USERNAME_LIKE_MAX = 3

_PHC = re.compile(r"^\$(?P<algo>argon2id|argon2i)\$v=(?P<v>\d+)\$m=(?P<m>\d+),t=(?P<t>\d+),p=(?P<p>\d+)"
                  r"\$(?P<salt>[A-Za-z0-9+/=]+)\$(?P<hash>[A-Za-z0-9+/=]+)$")

# The words that must not appear alone. This is not a breach check (we cannot ship a corpus and we will not
# phone a third party with a candidate password); it is a check against the shapes people actually type when a
# form demands a symbol.
_BANNED_SUBSTRINGS = ("openout", "polymarket", "password", "qwerty", "123456", "letmein", "admin", "welcome")


@dataclass(frozen=True)
class PhcParams:
    algo: str
    version: int
    memory_kib: int
    time_cost: int
    parallelism: int

    def as_dict(self) -> dict:
        return {"algo": self.algo, "v": self.version, "m": self.memory_kib, "t": self.time_cost,
                "p": self.parallelism}


def parse_phc(phc: str) -> PhcParams | None:
    """Read the parameters back out of a stored hash. `None` means "this is not a hash we will accept" — the
    caller must treat that as a refusal to log in, not as a reason to fall back to something weaker."""
    m = _PHC.match((phc or "").strip())
    if not m:
        return None
    return PhcParams(m.group("algo"), int(m.group("v")), int(m.group("m")), int(m.group("t")),
                     int(m.group("p")))


def policy_check(pw: str, *, user_id: str = "", email: str = "") -> list[str]:
    errs: list[str] = []
    if not isinstance(pw, str) or not pw:
        return ["a password is required"]
    if len(pw) < MIN_LEN:
        errs.append("use at least %d characters (length is the only parameter an attacker cannot tune around)"
                    % MIN_LEN)
    if len(pw) > MAX_LEN:
        errs.append("at most %d characters — we hash what you give us, so this is a paste accident, not a cut"
                    % MAX_LEN)
    low = pw.lower()
    hits = [w for w in _BANNED_SUBSTRINGS if w in low]
    if hits:
        errs.append("contains a word that appears in every breach corpus: %s" % ", ".join(sorted(hits)[:3]))
    for other in (user_id, email.split("@")[0] if email else ""):
        if other and len(other) >= USERNAME_LIKE_MAX and other.lower() in low:
            errs.append("must not contain your user name or email")
    if pw.isdigit() or low.isalpha():
        errs.append("mix digits and letters: a single-class password is what a GPU is good at")
    if len(set(pw)) < 6:
        errs.append("too few distinct characters")
    return errs


def is_acceptable(pw: str, **kw) -> bool:
    return not policy_check(pw, **kw)


def needs_update(phc: str) -> bool:
    """True when the stored hash was minted with parameters below what we now consider acceptable.

    This is the re-hash-on-login trigger. It returns True for an unparseable string as well: if we cannot read
    the parameters, we cannot vouch for them, and the cheap fix is to mint a new hash the moment the user
    proves the password.
    """
    p = parse_phc(phc)
    if p is None:
        return True
    return (p.algo != "argon2id" or p.version != 19 or p.memory_kib < PARAMS["memory_kib"]
            or p.time_cost < PARAMS["time_cost"] or p.parallelism < PARAMS["parallelism"])


def below_floor(phc: str) -> tuple[bool, str]:
    """Refuse to *store* a hash under the schema floor, with the message the ops log should carry."""
    p = parse_phc(phc)
    if p is None:
        return True, "not a parseable PHC string"
    if p.memory_kib < MIN_MEMORY_KIB:
        return True, "memory_kib=%d is below the floor %d" % (p.memory_kib, MIN_MEMORY_KIB)
    if p.time_cost < MIN_TIME_COST:
        return True, "time_cost=%d is below the floor %d" % (p.time_cost, MIN_TIME_COST)
    if p.parallelism < MIN_PARALLELISM:
        return True, "parallelism=%d is below the floor %d" % (p.parallelism, MIN_PARALLELISM)
    return False, ""


# --------------------------------------------------------------------------- login throttling (per account) ----
LOCK = {"max_failed": 10, "window_ms": 15 * 60 * 1000, "lock_ms": 15 * 60 * 1000,
        # The address-side budget is 4x the account-side one on purpose: mobile users in Gujarat and Lagos share
        # carrier NAT exits, and an IP limit tighter than that turns an attacker's spray into a denial of service
        # against everybody behind the same exit. The account limit is what stops the guessing.
        "ip_max_failed": 40}
# Deliberately *not* locked to a single counter: 10 tries per account in 15 minutes is the account-side limit,
# and the per-IP budget is the alert-delivery bucket in `services/ingest/net.py`, which is where a spray
# actually hurts. Both exist because either one alone is a bypass: account-only locking lets an attacker spread
# across users, IP-only lets them burn one address's budget and move on.


def lock_state(failures: int, first_fail_ms: int, at_ms: int, *, limit: int | None = None,
               window_ms: int | None = None, lock_ms: int | None = None) -> dict:
    """(locked, tries_left, retry_after_ms) with the window arithmetic in one place.

    The window is *fixed from the first failure*, not sliding: a sliding window makes a determined attacker's
    cost grow without ever stopping them, and it makes the "am I locked?" answer depend on the order of events
    in a way that is miserable to support. Fixed window + a hard lock is honest and explainable.
    """
    lim = int(limit if limit is not None else LOCK["max_failed"])
    win = int(window_ms if window_ms is not None else LOCK["window_ms"])
    lk = int(lock_ms if lock_ms is not None else LOCK["lock_ms"])
    failures = int(failures or 0)
    if failures <= 0:
        return {"locked": False, "tries_left": lim, "retry_after_ms": 0}
    age = int(at_ms) - int(first_fail_ms or at_ms)
    if age >= win:
        return {"locked": False, "tries_left": lim, "retry_after_ms": 0}
    if failures >= lim:
        remain = max(0, lk - age)
        return {"locked": True, "tries_left": 0, "retry_after_ms": remain}
    return {"locked": False, "tries_left": lim - failures, "retry_after_ms": 0}


# ------------------------------------------------------------------------ recovery, as an ATO vector (D3) ----
RECOVERY_TOKEN_TTL_MS = 60 * 60 * 1000           # one hour, single use
RECOVERY_MIN_SECONDS_BETWEEN = 15 * 60            # and at most one email per user per 15 minutes


def recovery_token_ok(*, age_ms: int, used: bool, user_id: str) -> tuple[bool, str]:
    """The rules that stop "forgot my password" becoming "I know your email address".

    The token goes to the address already on the account and nowhere else, it is single-use even if it fails to
    complete, and consuming it *always* bumps the credential generation so every existing session dies. The
    last part is the one that matters after a takeover: an attacker who reset the password still loses the
    session they were sitting in, and the real user sees the notification first.
    """
    if not user_id:
        return False, "no account"
    if used:
        return False, "already used"
    if age_ms > RECOVERY_TOKEN_TTL_MS:
        return False, "expired"
    return True, "ok"
