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
#: How much of the candidate's own tape in the paired markets may be left unexplained before the pairing
#: stops looking like following. Tight on purpose: a false farm label is an accusation about a person.
FARM_UNPAIRED_TOLERANCE_BPS = 1_000

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
              min_fills: int = FARM_MIN_FILLS,
              unpaired_tolerance_bps: int = FARM_UNPAIRED_TOLERANCE_BPS) -> dict | None:
    """Is this wallet's tape mechanically derived from another wallet's?

    Mechanical means, after P16's re-test: **a one-to-one pairing** in the same market and on the same side,
    inside `window_ms`, where the other wallet's fill came first — and where that pairing accounts for the other
    wallet's tape in those markets, not just for ours.

    Three things are load-bearing, and each was a bug first:

    * **A candidate fill can explain at most one of ours.** The first version counted every fill of ours that had
      *any* candidate fill inside the window, so one busy candidate explained an unbounded number of our fills.
    * **The pairing is per market and side, nearest first.** "Nearest" is the pairing a copier's behaviour actually
      produces: our fill follows the candidate's most recent one, not an arbitrary earlier one.
    * **Coverage.** If, after pairing, the candidate still has fills left over in the markets where we paired, then
      our fills were not *following* its fills — they were merely near some of them. This is the discriminator that
      P14 measured but could not isolate: two wallets trading the same side on a similar cadence look identical to
      a follower under any per-fill rule, because a 60-second cadence always has a fill inside a two-minute window.
      Pairing alone fixes nothing there (10 of 12 still pair); coverage does (2 of its fills are left over).

    `unpaired_tolerance_bps` is therefore the sensitivity dial, and it is deliberately tight. The row this rule
    produces is a public suspicion about a person, so a missed farm costs nothing (the wallet simply ranks) while a
    false one accuses somebody — the asymmetry is the reason the rule is conservative rather than eager. A farm
    that copies a *subset* of a leader's fills is already below `mirror_bps` of our own tape, so the tolerance only
    has to absorb a fill or two, not half a leader's history.
    """
    if len(own) < min_fills:
        return None
    best: dict | None = None
    for other, rows in (candidates or {}).items():
        if str(other) == str(wallet) or not rows:
            continue
        pools: dict[tuple[str, str], list[int]] = {}
        for r in rows:
            pools.setdefault((str(r.get("conditionId")), str(r.get("side"))), []).append(_int(r.get("tsMs")))
        for times in pools.values():
            times.sort()
        used: dict[tuple[str, str], set[int]] = {}
        deltas: list[int] = []
        for f in sorted(own, key=lambda r: _int(r.get("tsMs"))):
            key = (str(f.get("conditionId")), str(f.get("side")))
            t = _int(f.get("tsMs"))
            pick, pick_delta = None, None
            for ct in pools.get(key, ()):                    # ascending: the first non-negative delta is nearest
                delta = t - ct
                if delta < 0:
                    break
                if delta > window_ms or ct in used.get(key, ()):
                    continue
                if pick_delta is None or delta < pick_delta:
                    pick, pick_delta = ct, delta
            if pick is not None:
                used.setdefault(key, set()).add(pick)
                deltas.append(int(pick_delta))
        paired = len(deltas)
        share = paired * 10_000 // max(1, len(own))
        if share < mirror_bps:
            continue
        keys = list(used)
        candidates_in_played_keys = sum(len(pools[k]) for k in keys)
        leftover = candidates_in_played_keys - sum(len(used[k]) for k in keys)
        leftover_bps = leftover * 10_000 // max(1, candidates_in_played_keys)
        if leftover_bps > unpaired_tolerance_bps:
            continue
        deltas.sort()
        lead_median = deltas[len(deltas) // 2] if deltas else 0
        if best is None or paired > best["mirroredFills"]:
            best = {"derivedFrom": str(other), "mirroredFills": paired, "mirrorBps": share,
                    "windowMs": window_ms, "fills": len(own),
                    "leadMedianMs": lead_median,
                    "candidateFillsInPairedMarkets": candidates_in_played_keys,
                    "candidateUnpairedBps": leftover_bps,
                    "rule": ("%d of %d fills pair one-to-one with one wallet's fills in the same market and side, "
                             "each of its fills used at most once and %d of its %d fills in those markets "
                             "accounted for (median lead %.0fs): a copy, not a coincidence"
                             % (paired, len(own), candidates_in_played_keys - leftover,
                                candidates_in_played_keys, lead_median / 1000.0))}
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
