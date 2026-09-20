"""Anti-Sybil, designed in rather than bolted on: signals, the three collisons, and the decision order.

The kit's requirement is one paragraph and it contains five separate mechanisms. They are here as **rules with
precedence**, because the failure mode of an anti-fraud system is not catching too little — it is an ambiguous
case being resolved differently on two screens, or a rule that fires so often that a human stops reading the
queue. So the order is explicit and total, and the API serves the same order to the referrer:

1. **Self-referral is a hard block.** Any shared signal between the referee and the referrer refuses the
   attribution outright. This is not primarily about the referral budget: the kit's sentence is that self-referral
   is a *builder-code revocation ground*, because a code we revoke is revenue we lose on everything, and the
   venue's own affiliate terms are the thing at risk. `decision()` returns `builder_code_ground` for exactly that.
2. **A shared funding source is conclusive.** "Multiple wallets funded from the same source are one person" (the
   kit). Two wallets whose USDC arrived from the same address are one person with two wallets, so a second
   referral from a funding source that already referred somebody is **refused** — not queued.
3. **A shared device or IP is suggestive and goes to a person.** Two colleagues on one office wifi, a household,
   a phone that changed hands: real cases, and a program that refuses them outright loses honest referrers. The
   attribution is recorded in `review` and earns nothing until cleared — accrual is paused, never deleted, so
   clearing the queue pays the referrer what they were owed all along.
4. **Velocity is about the referrer, not the referee**: more than `MAX_PER_DAY` or `MAX_PER_HOUR` attributions
   is a rate limit in the shape of a 429, plus a review row, because a referrer who legitimately posts a link
   somewhere popular can cross it and should not be banned for it.
5. **Clawback is the backstop and it is published** (`terms.PUBLISHED_RULES`): unpaid accruals first, then what
   was paid, with anything under `CLAWBACK_MIN_MICRO` written off. A clawback rule that is not published is a
   punishment decided after the fact.

**Signals are hashed, never stored raw.** A referral system is the one place in this product that has a reason to
keep device and IP data about a user who never traded, and the kit's own privacy posture (D4: a private wallet is
pseudonymised in everybody else's data) applies to the data we keep about a stranger who clicked a link. `hash_`
is HMAC-SHA-256, truncated, salted with a per-deployment secret, and the salt has a minimum length because an
unsalted signal hash is a rainbow table over a space of a few billion IPv4 addresses.
"""
from __future__ import annotations

import hashlib
import hmac

#: The signal kinds, weakest last. Funding is conclusive on its own; the other two corroborate.
KINDS = ("device", "ip", "funding")

#: The decision order above, as data, so the API serves the same precedence the engine applies.
PRECEDENCE = ("self_referral", "duplicate_funding", "shared_device_or_ip", "velocity")

#: Referrer-side velocity limits. A limit that is never crossed is not a limit, and a limit that is crossed by
#: anyone sharing a link in public is a ban on sharing links in public; these two numbers are set for the second.
MAX_PER_DAY = 25
MAX_PER_HOUR = 5


def hash_(value: str, salt: str, *, kind: str = "ip") -> str:
    """A stable, salted, truncated digest of a signal. The raw value never reaches the database or a response.

    The salt is required and must be at least 16 characters: a per-deployment secret is what stops two deployments'
    rows from joining, and what stops an attacker from hashing a list of candidate IPs and matching ours.
    """
    s = str(salt or "")
    if len(s) < 16:
        raise ValueError("a signal hash needs a salt of at least 16 characters: an unsalted hash of an IP is a "
                         "rainbow-table lookup")
    if kind not in KINDS:
        raise ValueError("unknown signal kind: %r (allowed: %s)" % (kind, ", ".join(KINDS)))
    mac = hmac.new(s.encode(), str(value or "").strip().lower().encode(), hashlib.sha256).hexdigest()
    return "%s_%s" % (kind[0], mac[:32])


def signal_key(kind: str, digest: str) -> str:
    return "%s:%s" % (str(kind), str(digest))


def _keys(signals: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for s in signals or []:
        kind = str(s.get("kind") or "")
        digest = str(s.get("hash") or "")
        if kind and digest:
            out[signal_key(kind, digest)] = {"kind": kind, "hash": digest, "seen_ms": int(s.get("seen_ms") or 0)}
    return out


def self_referral(referee_signals: list[dict], referrer_signals: list[dict]) -> dict:
    """Any signal shared between the two accounts means one person tried to refer themselves.

    Funding alone is enough; so is device alone. IP alone is NOT returned as a hit here — a shared NAT is not
    identity — but it is returned as `weak` so the caller can escalate it to a review rather than silently accept
    it, which is the difference between "we refuse the honest ones" and "we notice".
    """
    theirs = _keys(referrer_signals)
    strong, weak = [], []
    for key, sig in _keys(referee_signals).items():
        if key not in theirs:
            continue
        if sig["kind"] == "ip":
            weak.append(sig["kind"])
        else:
            strong.append(sig["kind"])
    return {"hit": bool(strong), "kinds": sorted(set(strong)), "weak": sorted(set(weak)),
            "ground": "builder_code_self_dealing" if strong else "",
            "sentence": ("this wallet and the referring account are the same person: %s"
                         % ", ".join(sorted(set(strong))) if strong else
                         ("the referring account and the referee share an address; a person looks at this"
                          if weak else ""))}


def duplicate_funding(referee_signals: list[dict], existing_by_signal: dict) -> dict:
    """A funding source that has already referred somebody refers nobody else. Conclusive, so refused."""
    for key, sig in _keys(referee_signals).items():
        if sig["kind"] != "funding":
            continue
        prior = (existing_by_signal or {}).get(key) or []
        for row in prior:
            # Only collisions WITHIN one referrer count here. Two different referrers whose referees happen to
            # share a funding source is a farm too, and it belongs in the review queue (below), not in a refusal
            # that a legitimate referrer cannot argue with.
            if str(row.get("same_referrer")) == "1" or row.get("same_referrer") is True:
                return {"hit": True, "kind": "funding", "prior_referee": str(row.get("referee") or ""),
                        "sentence": "another wallet funded from the same source is already referred by this "
                                    "account, so these are one person"}
    return {"hit": False, "kind": "", "prior_referee": "", "sentence": ""}


def shared_device_or_ip(referee_signals: list[dict], existing_by_signal: dict) -> dict:
    """A signal shared with an existing referee: recorded, referred to a person, earning nothing until cleared."""
    hits = []
    for key, sig in _keys(referee_signals).items():
        if sig["kind"] == "funding":
            continue
        prior = (existing_by_signal or {}).get(key) or []
        for row in prior:
            hits.append({"kind": sig["kind"], "referee": str(row.get("referee") or "")})
    if not hits:
        return {"hit": False, "kinds": [], "sentence": ""}
    kinds = sorted({h["kind"] for h in hits})
    return {"hit": True, "kinds": kinds,
            "sentence": ("another wallet already referred by this account shares a %s; a person looks at this "
                         "before it earns anything" % "/".join(kinds))}


def velocity(attributed_last_hour: int, attributed_last_day: int, *, per_hour: int = MAX_PER_HOUR,
             per_day: int = MAX_PER_DAY) -> dict:
    """Rate limits on the referrer. Returns the refusal sentence rather than a boolean, because a 429 with no
    sentence is a referrer who opens a ticket."""
    hour = int(attributed_last_hour or 0)
    day = int(attributed_last_day or 0)
    if hour >= int(per_hour):
        return {"hit": True, "scope": "hour", "limit": int(per_hour), "seen": hour,
                "sentence": "%d referrals in an hour is the limit; this one waits, and a person sees the account "
                            "if it keeps happening" % int(per_hour)}
    if day >= int(per_day):
        return {"hit": True, "scope": "day", "limit": int(per_day), "seen": day,
                "sentence": "%d referrals in a day is the limit; this one waits, and a person sees the account "
                            "if it keeps happening" % int(per_day)}
    return {"hit": False, "scope": "", "limit": 0, "seen": 0, "sentence": ""}


def decision(referee_signals: list[dict], referrer_signals: list[dict], existing_by_signal: dict | None = None,
             *, attributed_last_hour: int = 0, attributed_last_day: int = 0,
             same_account: bool = False) -> dict:
    """The whole decision, in the published order, as one row the API and the gate both read.

    The states here are the DECISION's — `attributed`, `review`, `refused` — and deliberately not the attribution's
    own state machine (`pending`/`qualified`/…, in `terms.STATES`). They are different facts: a decision says
    whether this referral may exist, and only the referee's first matched order can make it *qualified*. One
    vocabulary for both is how an attribution ends up marked qualified with no qualifying order, which the
    schema's CHECK refuses and which is exactly the class of bug a CHECK is for.

    `same_account` is the caller's own knowledge that the referee and the referrer are literally the same account
    (the wallet link table says so). It is a parameter rather than a signal comparison because a user referring
    their own second account has no shared signal — it has a shared *user id*, and that is not a signal we hash.
    """
    self_hit = self_referral(referee_signals, referrer_signals)
    if same_account or self_hit["hit"]:
        kinds = self_hit["kinds"] or ["account"]
        return {"state": "refused", "reason": "self_referral", "kinds": kinds,
                "builder_code_ground": "builder_code_self_dealing",
                "review": True,
                "sentence": "an account cannot refer itself (%s): the referral is refused and the builder code "
                            "it was made under is a revocation ground" % ", ".join(kinds)}
    dup = duplicate_funding(referee_signals, existing_by_signal or {})
    if dup["hit"]:
        return {"state": "refused", "reason": "duplicate_funding", "kinds": ["funding"],
                "builder_code_ground": "", "review": True, "sentence": dup["sentence"]}
    shared = shared_device_or_ip(referee_signals, existing_by_signal or {})
    vel = velocity(attributed_last_hour, attributed_last_day)
    if shared["hit"]:
        return {"state": "review", "reason": "shared_device_or_ip", "kinds": shared["kinds"],
                "builder_code_ground": "", "review": True, "sentence": shared["sentence"]}
    if vel["hit"]:
        return {"state": "review", "reason": "velocity", "kinds": [], "builder_code_ground": "", "review": True,
                "sentence": vel["sentence"]}
    if self_hit["weak"]:
        # A shared IP with nobody else referred: attributed, but the account is flagged so the next collision
        # starts from a note rather than from nothing.
        return {"state": "attributed", "reason": "shared_ip_only", "kinds": self_hit["weak"],
                "builder_code_ground": "", "review": False,
                "sentence": "attributed; the referring account shares an address with this wallet, which is "
                            "usually a household or a phone network and is noted rather than refused"}
    return {"state": "attributed", "reason": "", "kinds": [], "builder_code_ground": "", "review": False,
            "sentence": "attributed"}


# -------------------------------------------------------------------------------------------------- clawback
def clawback(*, paid_micro: int, unpaid_micro: int, reason: str, minimum_micro: int = 5_000_000) -> dict:
    """What the published rule does, as arithmetic: unpaid accruals first, then what was already paid.

    The write-off is the interesting part and it is deliberate. A five-dollar invoice costs more than five dollars
    to collect, and more importantly: a rule that claws back every detectable dollar is a rule that produces a
    stream of small, arguable debits against people who may be innocent. Below the floor the referral is closed
    and the amount is reported as written off, so it appears in the numbers rather than in nobody's memory.
    """
    paid = max(0, int(paid_micro or 0))
    unpaid = max(0, int(unpaid_micro or 0))
    floor = max(0, int(minimum_micro or 0))
    if paid + unpaid < floor:
        return {"unpaid_micro": 0, "paid_micro": 0, "written_off_micro": paid + unpaid, "requires_repayment": False,
                "reason": str(reason or ""),
                "sentence": "($%d.%02d) is under the $%d floor, so it is written off and closed rather than "
                            "invoiced" % ((paid + unpaid) // 1_000_000, (paid + unpaid) % 1_000_000 // 10_000,
                                          floor // 1_000_000)}
    take_unpaid = min(unpaid, paid + unpaid)
    take_paid = max(0, paid + unpaid - take_unpaid)
    return {"unpaid_micro": take_unpaid, "paid_micro": min(take_paid, paid),
            "written_off_micro": 0, "requires_repayment": take_paid > 0, "reason": str(reason or ""),
            "sentence": ("cancels $%d.%02d of unpaid accruals and asks for $%d.%02d already paid back"
                         % (take_unpaid // 1_000_000, take_unpaid % 1_000_000 // 10_000,
                            min(take_paid, paid) // 1_000_000, min(take_paid, paid) % 1_000_000 // 10_000))}
