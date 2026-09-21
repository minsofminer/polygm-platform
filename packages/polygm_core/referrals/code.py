"""Codes and links: one code per referrer, a short one to say out loud, and the shape rules that bound both.

Two codes exist for one reason the kit names: a **link** for anywhere a URL can be pasted, and a **short code**
for a podcast, a stream, or a voice note. They are different strings with different jobs, so they are different
rows rather than one clever code:

* a **link token** is `ref_` + 22 characters of base32 (derived, high-entropy, never chosen), which is what a
  stranger sees and what an attribution is actually keyed by;
* a **short code** is a vanity string a referrer picks (`pgm.to/<code>` in the product's own copy), which is
  guessable *by design* and therefore carries no authority on its own — an attribution needs the token OR a code
  plus a signed-in referee, and the code is resolved through the owner's row rather than accepted as an identity.

The shape rules exist to stop three specific things: a code that looks like a different brand (`reserved`), a code
that is a slur or a phishing hook (the moderator list is a data file, not a regex), and a code that collides with
the venue's own vocabulary. `normalise` lower-cases and strips separators, because `Polymarket-Mike` and
`polymarketmike` must not be two codes: a duplicate that differs by punctuation is how a lookalike claim works.
"""
from __future__ import annotations

import hashlib
import re
import secrets

#: Link tokens are generated, not chosen. The prefix keeps them greppable in logs and unmistakable in a URL.
TOKEN_PREFIX = "ref_"
#: 22 base32 characters of entropy after the prefix — the length is part of the format, not a coincidence, so
#: `is_link_token` can reject a row that predates or postdates the format instead of building a URL from it.
TOKEN_LEN = 22

#: A short code: 4-16 characters, lower-case, digits and `_`. 4 because the space must be big enough to allocate
#: without collisions at our scale (36^4 is 1.6M) and 16 because a code read aloud has to fit in a sentence.
CODE_MIN, CODE_MAX = 4, 16

#: Reserved: everything that is somebody else's name, or ours. Kept short and honest — this list is checked
#: against the same vocabulary the D4 handle list uses, so a referrer cannot claim a code that would be a handle
#: by another route.
RESERVED = frozenset({
    "polygm", "polygmteam", "admin", "administrator", "support", "help", "official", "staff", "team", "mod",
    "moderator", "root", "system", "billing", "security", "abuse", "legal", "press", "sales", "polymarket",
    "referral", "referrals", "invite", "promo", "bonus", "affiliate", "partners", "api", "docs", "status",
    "settings", "account", "wallet", "leaderboard", "terms", "privacy", "login", "signup", "signin", "www",
})

_SHAPE = re.compile(r"^[a-z0-9_]+$")


def normalise(value: str) -> str:
    """`Polymarket-Mike` and `polymarket mike` are the same code, and both are `polymarketmike`.

    Stripping separators rather than rejecting them is the decision: a user typing a code they heard on a podcast
    should not have to guess our punctuation rules, and the alternative — two codes that differ only by a dash — is
    precisely the lookalike claim the `reserved` list exists to prevent.
    """
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def validate_short_code(value: str) -> tuple[str, str]:
    """(normalised code, refusal sentence). The sentence is the one the API serves, so it says what to do.

    None of these sentences echoes what was typed, and that is a rule rather than a style: the API serves a
    `VALIDATION` refusal's sentence to the browser (`CODE_INVALID` is in `_PUBLIC_DETAIL_CODES`), and a code is a
    string a stranger chose. Repeating a rejected one back into a response body is how a phishing string gets a
    second life in a screenshot, so the refusal states the RULE and leaves the input in the request that made it.
    """
    code = normalise(value)
    if not code:
        return "", "a short code is 4 to 16 characters of a-z, 0-9 and _"
    if len(code) < CODE_MIN or len(code) > CODE_MAX:
        return "", "a short code is 4 to 16 characters (a-z, 0-9 and _); %d is outside that" % len(code)
    if not _SHAPE.match(code):
        return "", "a short code uses a-z, 0-9 and _ only"
    if code in RESERVED:
        return "", "that code is reserved (it reads as us, or as somebody else's name)"
    if code.isdigit():
        return "", "an all-digit code is refused: it is indistinguishable from an account number"
    return code, ""


def make_token(seed: str = "") -> str:
    """A link token. 22 base32 characters = 110 bits, which is not guessable at any rate we could serve.

    The seed parameter exists for tests and for re-deriving a token in a fixture; the entropy comes from
    `secrets` even then (the seed is mixed in, not used as the source).
    """
    raw = secrets.token_bytes(16)
    if seed:
        raw = hashlib.sha256(raw + seed.encode()).digest()[:16]
    alphabet = "abcdefghijklmnopqrstuvwxyz234567"
    bits = int.from_bytes(raw, "big")
    out = []
    for _ in range(TOKEN_LEN):
        out.append(alphabet[bits & 31])
        bits >>= 5
    return TOKEN_PREFIX + "".join(reversed(out))


def is_link_token(token: str) -> bool:
    """Whether a stored string is one of *our* link tokens.

    Exists as a predicate rather than a `startswith` at each call site because the answer is used to decide
    whether a row is usable at all: a `referral_links` row whose token is not a link token cannot be turned into
    a shareable URL, and the caller has to be able to ask before `link_for` raises.
    """
    t = str(token or "")
    return t.startswith(TOKEN_PREFIX) and len(t) == len(TOKEN_PREFIX) + TOKEN_LEN


def link_for(token: str, base: str = "https://openout.app") -> str:
    """The shareable link. One place builds it, so the OG page (D6) and the dashboard cannot disagree."""
    t = str(token or "")
    if not is_link_token(t):
        raise ValueError("not a link token: %r" % t)
    return "%s/r/%s" % (str(base).rstrip("/"), t)


def code_link(code: str, base: str = "https://openout.app") -> str:
    return "%s/r/c/%s" % (str(base).rstrip("/"), normalise(code))


def owner_of_click(*, token_owner: str = "", code_owner: str = "") -> str:
    """Whose click it is when both a token and a code arrive.

    The **token wins**, and that is a decision rather than a fallback: the token is the high-entropy thing the
    visitor actually followed, and a code that has changed hands (a referrer renamed their code, or a shared device
    kept a stale `?c=`) must not be able to steal an attribution from the link somebody clicked. Ties are resolved
    for the visitor's clicked link, which is what the click table records.
    """
    if token_owner:
        return str(token_owner)
    return str(code_owner or "")
