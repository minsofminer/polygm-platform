"""The anonymous budget: what one caller may fetch from pages that have no session to throttle.

This is the module that decides "serve it", "serve it and count it" or "refuse with a retry-after", and it is
pure so the decision can be tested without a clock, a request or a database.

The arithmetic is **not re-derived**: `decide_hits` calls `polygm_core.security.passwords.lock_state`, the
fixed-window + hard-lock function P07 wrote for login attempts, with the budget passed in as its limit. The two
places share one property that matters and one that only looks like a detail:

* the window is fixed from the first hit rather than sliding, so an honest burst pays for nothing but its own
  window and "am I blocked?" has one answer instead of an order-dependent one;
* the same function counts a *hit* here and a *failed attempt* there, and the only thing that differs is which
  is being limited. Re-implementing it for pages would have produced a second fixed-window function, and the
  second one is always the one that is wrong on the day somebody adds a lock duration.

Two numbers carry the abuse argument, and both are stated because a limit nobody can explain becomes a support
ticket:

* **Per kind, generous.** 600 trader pages a minute is not a limit a reader can reach; it is a limit a walk of
  the sitemap reaches immediately.
* **Blocks are scoped and expiring.** A block with no scope would refuse a caller's market page because they
  scraped boards; a block with no expiry is a page that is "broken for me" with no way back.
"""
from __future__ import annotations

from ..referrals import sybil                          # one salt, one digest function, one place to rotate it
from ..security import passwords                       # one fixed-window implementation

#: kind -> (max hits per window, window ms, lock ms). The window is short and the lock is short: a page that
#: refuses a reader for fifteen minutes because they refreshed a live odds page is worse than the load.
BUDGET = {
    "trader": (600, 60_000, 120_000),
    "market": (1200, 60_000, 120_000),      # odds pages refresh, and a burst here is normal reading
    "leaderboard": (300, 60_000, 120_000),
    "sitemap": (10, 60_000, 600_000),       # the whole point of a crawler limit: this is the walk, not a page
    "og": (3000, 60_000, 300_000),          # unfurlers fan out across every link in a message
}
#: Everything one caller may do across every kind inside a window. The per-kind numbers stop one surface being
#: walked; this one is what stops a loop over all four, because a crawler does not politely stay in one kind.
TOTAL = (5000, 60_000, 300_000)


def subject_hash(value: str, salt: str) -> str:
    """The only identity a public read keeps: a salted digest of the caller's address.

    Delegated to the referral plane's `hash_` so there is exactly one answer to "how do we digest a signal, and
    what salt". A second implementation would be a second salt to rotate, and the two would drift in the only
    direction that matters: one of them would end up unsalted.
    """
    return sybil.hash_(value, salt, kind="ip")


def decide_hits(*, kind: str, hits: int, window_start_ms: int, at_ms: int) -> dict:
    """Whether this hit is allowed, given the window's own numbers. Pure; the caller reads and writes the row."""
    kind = str(kind or "")
    spec = BUDGET.get(kind, BUDGET["leaderboard"])
    verdict = passwords.lock_state(int(hits), int(window_start_ms or at_ms), int(at_ms),
                                   limit=int(spec[0]), window_ms=int(spec[1]), lock_ms=int(spec[2]))
    allowed = not verdict["locked"]
    return {
        "allowed": allowed,
        "kind": kind,
        "hits": int(hits),
        "limit": int(spec[0]),
        "windowMs": int(spec[1]),
        "retryAfterMs": int(verdict["retry_after_ms"]) if verdict["locked"] else 0,
        "remaining": int(verdict["tries_left"]),
        "sentence": ("too many public page requests from this address; try again shortly"
                     if not allowed else ""),
    }


def decide_total(*, hits: int, window_start_ms: int, at_ms: int) -> dict:
    """The cross-kind budget. Same arithmetic, and it exists because the per-kind limits are escapable."""
    verdict = passwords.lock_state(int(hits), int(window_start_ms or at_ms), int(at_ms),
                                   limit=int(TOTAL[0]), window_ms=int(TOTAL[1]), lock_ms=int(TOTAL[2]))
    return {"allowed": not verdict["locked"], "kind": "all", "hits": int(hits), "limit": int(TOTAL[0]),
            "retryAfterMs": int(verdict["retry_after_ms"]) if verdict["locked"] else 0,
            "remaining": int(verdict["tries_left"]),
            "sentence": ("too many public page requests from this address; try again shortly"
                         if verdict["locked"] else "")}


def block_for(blocks: list[dict], *, scope: str, at_ms: int) -> dict:
    """The live block matching this scope, if any.

    `scope='all'` outranks a kind-specific block by construction: a block recorded against everything is
    returned for every kind, and the caller does not need to know which case it is in.
    """
    at = int(at_ms)
    live = [b for b in (blocks or [])
            if int(b.get("until_ms") or 0) > at and str(b.get("scope") or "") in ("all", str(scope or ""))]
    if not live:
        return {}
    live.sort(key=lambda b: (0 if str(b.get("scope")) == "all" else 1, -int(b.get("until_ms") or 0)))
    b = live[0]
    wait = max(0, int(b.get("until_ms") or 0) - at)
    return {"blocked": True, "scope": str(b.get("scope") or ""), "reason": str(b.get("reason") or ""),
            "untilMs": int(b.get("until_ms") or 0), "retryAfterMs": wait,
            "sentence": "this address is not being served public pages right now"}


def should_auto_block(*, findings: list, hits: int, limit: int) -> tuple[bool, str]:
    """Whether a caller that keeps going *after* a lock is worth blocking outright.

    The rule is deliberately narrow: a caller who was refused and came straight back ten times is a scraper,
    and the honest reader this could catch is one whose browser retried a page they had open. Ten retries is
    past that, and the block expires, so the cost of being wrong is bounded and the cost of doing nothing is a
    walk of the whole public surface.
    """
    over = int(hits) - int(limit)
    if int(hits) > int(limit) and over >= 10:
        return True, "%d requests past a %d-request budget" % (int(hits), int(limit))
    if findings:
        return False, ""
    return False, ""
