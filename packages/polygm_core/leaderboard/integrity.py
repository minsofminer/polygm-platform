"""P11 · Integrity: the filters that decide what a ranking is allowed to count.

Everything here is computed from our own ledger and is checkable with a query. There is no model and no score
without named factors — the same discipline `security/abuse.py` uses for the builder-fee side, applied to
rankings. Where that module already owns a threshold (the round-trip window, the concentration share, the Sybil
cluster size), this one imports it: a ranking that used its own definition of a round trip would disagree with the
payout gate about the same wallet, and the disagreement would be found by a user, not by us.

The three things this module refuses to do:

* **hide a wallet.** A flagged wallet is still ranked (unless the rule is explicitly a board's admission rule,
  like the copy-farm filter on the copied board). Every flag is a sentence on the row.
* **call a person a fraud.** The labels are mechanical ("derived from", "removed 41,200 USDC of round trips") and
  the accusation, if there is one, is made by a human on the anti-gaming dashboard.
* **treat unknown as zero.** A disputed market's result is unknown; it is excluded and counted, never settled.
"""
from __future__ import annotations

from ..security import abuse
from . import boards as bd

#: Reused from the payout gate: the same 10 minutes, so "round trip" means one thing across the product.
ROUND_TRIP_WINDOW_MS = abuse.ROUND_TRIP_WINDOW_MS

#: A round trip's economic signature: the wallet ends where it started, so the two prices are within a tick of
#: each other. Half a cent on a 0.50 market is 100 bps; a genuine scalper who caught a move is outside this.
WASH_PRICE_TOLERANCE_BPS = 50

#: Copy-farm detection: mirrored market and side inside this window, on at least this share of the wallet's fills.
FARM_WINDOW_MS = 120_000
FARM_MIRROR_BPS = 8_000
FARM_MIN_FILLS = 10

#: The blown-up line: the wallet was up at some point and is now at or below zero.
BLOWUP_EQUITY_MICRO = 0


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def round_trip_pairs(fills: list[dict], *, window_ms: int = ROUND_TRIP_WINDOW_MS,
                     tolerance_bps: int = WASH_PRICE_TOLERANCE_BPS) -> list[tuple[dict, dict]]:
    """Pair each fill with the opposite-side fill that closes it as a wash, oldest first.

    A pair is: same wallet, same condition, opposite side, inside the window, and prices within tolerance. Each
    fill is consumed once, so three buy-and-sells produce at most two pairs — a pairing that let one fill close
    two round trips would subtract the same notional twice.
    """
    by_market: dict[tuple, list[dict]] = {}
    for f in fills:
        by_market.setdefault((str(f.get("wallet")), str(f.get("conditionId"))), []).append(f)
    pairs: list[tuple[dict, dict]] = []
    for rows in by_market.values():
        rows = sorted(rows, key=lambda r: _int(r.get("tsMs")))
        used: set[int] = set()
        for i, first in enumerate(rows):
            if i in used:
                continue
            a = _int(first.get("priceMicro"))
            for j in range(i + 1, len(rows)):
                if j in used:
                    continue
                second = rows[j]
                if _int(second.get("tsMs")) - _int(first.get("tsMs")) > window_ms:
                    break
                if str(second.get("side")) == str(first.get("side")):
                    continue
                span = abs(_int(second.get("priceMicro")) - a)
                reference = max(1, min(a, _int(second.get("priceMicro"))))
                if span * 10_000 // reference > tolerance_bps:
                    continue
                used.add(i)
                used.add(j)
                pairs.append((first, second))
                break
    return pairs


def wash_volume(fills: list[dict]) -> dict:
    """How much of a wallet's notional was washed, and the sentence that says so on the row."""
    pairs = round_trip_pairs(fills)
    washed = sum(min(_int(a.get("notionalMicro")), _int(b.get("notionalMicro"))) for a, b in pairs)
    total = sum(_int(f.get("notionalMicro")) for f in fills)
    return {
        "washedMicro": washed,
        "verifiedMicro": max(0, total - washed),
        "roundTrips": len(pairs),
        "windowMs": ROUND_TRIP_WINDOW_MS,
        "rule": ("a round trip is an opposite-side fill in the same market within %d minutes whose price moved no "
                 "more than %d bps: the position went nowhere, so the volume is subtracted"
                 % (ROUND_TRIP_WINDOW_MS // 60_000, WASH_PRICE_TOLERANCE_BPS)),
        "note": ("" if not washed else
                 "removed %d micro of round-tripped volume from %d pair(s) before ranking this wallet" % (washed, len(pairs))),
    }


def copy_farm(*, wallet: str, own: list[dict], candidates: dict[str, list[dict]],
              window_ms: int = FARM_WINDOW_MS, mirror_bps: int = FARM_MIRROR_BPS,
              min_fills: int = FARM_MIN_FILLS) -> dict | None:
    """Is this wallet's tape mechanically derived from another wallet's?

    Mechanical means: a fill in the same market, on the same side, within `window_ms` of a fill the OTHER wallet
    made FIRST, on at least `mirror_bps` of this wallet's fills. The candidate has to lead: copying is a
    follower's behaviour, and a source that trails the farm is the farm by another name.
    """
    if len(own) < min_fills:
        return None
    best: dict | None = None
    for other, rows in (candidates or {}).items():
        if str(other) == str(wallet) or not rows:
            continue
        index: list[tuple[str, str, int]] = sorted(
            (str(r.get("conditionId")), str(r.get("side")), _int(r.get("tsMs"))) for r in rows)
        mirrored = 0
        for f in own:
            key_c, key_s, t = str(f.get("conditionId")), str(f.get("side")), _int(f.get("tsMs"))
            for (oc, os_, ot) in index:
                if oc != key_c or os_ != key_s:
                    continue
                delta = t - ot
                if 0 <= delta <= window_ms:
                    mirrored += 1
                    break
        share = mirrored * 10_000 // max(1, len(own))
        if share >= mirror_bps and (best is None or mirrored > best["mirroredFills"]):
            best = {"derivedFrom": str(other), "mirroredFills": mirrored, "mirrorBps": share,
                    "windowMs": window_ms, "fills": len(own),
                    "rule": ("%d of %d fills mirror one wallet's market and side inside %d seconds, which is a "
                             "copy, not a coincidence" % (mirrored, len(own), window_ms // 1_000))}
    return best


def age_days(*, first_seen_ms: int, at_ms: int) -> int:
    return max(0, (_int(at_ms) - _int(first_seen_ms)) // 86_400_000)


def wallet_state(*, first_seen_ms: int, at_ms: int, curve: list[dict], best_micro: int,
                 realised_micro: int) -> dict:
    """`(state, labels)` for one wallet: provisional, blew up, or neither — plus the lucky-trade share.

    `curve` is the wallet's cumulative realised curve (`[{cumMicro}]`, oldest first), the same series the dossier
    draws. Blown up means the curve was above zero at some point and is at or below zero now: a wallet that has
    only ever lost is not "blown up", it is a losing wallet, and calling that a blow-up would flatten the one
    state the kit asks to be visible.
    """
    labels: list[str] = []
    days = age_days(first_seen_ms=first_seen_ms, at_ms=at_ms)
    state = "ranked"
    if days < bd.PROVISIONAL_DAYS:
        labels.append("provisional: %d of %d days on the platform" % (days, bd.PROVISIONAL_DAYS))
    cums = [_int(p.get("cumMicro")) for p in (curve or [])]
    ever_up = any(c > 0 for c in cums)
    now = cums[-1] if cums else _int(realised_micro)
    if ever_up and now <= BLOWUP_EQUITY_MICRO:
        state = "blew_up"
        labels.append("blew up: this wallet was up and is now at or below zero, and it stays on the board")
    best = max(0, _int(best_micro))
    # Clamped at 100%: a wallet can have a best market larger than its realised total (everything else lost
    # money), and "140% of the PnL" is not a share of anything — the row says 100%, which is the true statement
    # ("one trade is all of it, and then some"). It is also what the schema's CHECK allows, so the clamp is
    # enforced in the engine rather than discovered by an INSERT.
    share = min(10_000, best * 10_000 // _int(realised_micro)) if _int(realised_micro) > 0 else 0
    lucky = share >= bd.LUCKY_TRADE_SHARE_BPS
    if lucky:
        labels.append("one trade: %d%% of this wallet's realised PnL is a single market" % (share // 100))
    return {"state": state, "labels": labels, "ageDays": days, "bestTradeShareBps": share,
            "luckyGambler": lucky}


def disputed_exclusions(*, settled: list[dict], blocked_conditions: set[str]) -> dict:
    """Settled results withheld from a ranking because their market is under dispute.

    Withheld, not zeroed: the outcome of a disputed market is unknown, and a zero would be a claim about a result
    nobody has.
    """
    blocked = {str(c) for c in (blocked_conditions or set())}
    kept = [s for s in (settled or []) if str(s.get("conditionId")) not in blocked]
    removed = [s for s in (settled or []) if str(s.get("conditionId")) in blocked]
    return {"settled": kept, "excluded": len(removed),
            "excludedConditions": [str(s.get("conditionId")) for s in removed],
            "rule": ("results from markets on the risk blocklist (an active UMA dispute) are withheld from "
                     "rankings and counted on the row: unknown is not zero")}
