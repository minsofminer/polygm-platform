"""P11 · Where a board's evidence comes from: our own ledger, read once, shaped for the engine.

`rank.py` is pure and takes evidence; this module is the only thing that decides *what evidence* a board sees.
That decision has four parts, and each one is a way a leaderboard lies if it is left to a caller:

* **Which trades count as a result.** A fill is not a result. A *market* is: a wallet that bought forty times
  into one question made one decision, and ranking the fills would reward being busy. `settled_results()` folds
  the tape into one row per (wallet, market) — the same "per settled MARKET, not per fill" rule the dossier and
  every win rate on the platform already use.
* **What a fill was worth.** `realised_micro()` is the one implementation of "shares − cost for a winning buy,
  −cost for a losing one, mirrored for a sell", and the dossier calls it too. Two copies of this arithmetic
  would disagree about a sell first, and the disagreement would be found by a user.
* **Which window a board reads.** `read_plan()` is the window in milliseconds, and it is not one number for all
  boards: the volume board reads the window's own fills (that is the fact it ranks), every other board reads
  *lifetime* fills, because their eligibility floor is a lifetime turnover floor. The rising board reads
  fourteen days of settled results — its metric is a subtraction of two weeks, and a seven-day read would make
  the second half of that subtraction structurally zero.
* **Unknown is not zero.** A disputed market's result is withheld by the engine, and a wallet that never
  resolved anything has no realised PnL rather than a PnL of zero. `evidence()` never fabricates a result for a
  market the tape does not resolve.

The scaling note, stated plainly: this reads the whole tape and is correct at our size. When it stops being
fast, the answer is the recompute worker writing `leaderboard_snapshots` (the table exists for exactly that
reason) and not a smaller denominator here.
"""
from __future__ import annotations

from . import boards as bd

DAY_MS = 86_400_000

#: The windows a board may be read at, in days. `all` is 0 and means "from the beginning of what we hold".
WINDOW_DAYS = {"24h": 1, "7d": 7, "30d": 30, "90d": 90, "all": 0}

#: Days of settled results the rising board needs: its metric is "the last 7 days minus the 7 before", so the
#: read reaches 14 days back. This is the kind of constant that looks like a typo until the second week is zero.
RISING_READ_DAYS = 14


def window_start_ms(window: str, at_ms: int) -> int:
    days = WINDOW_DAYS.get(str(window))
    if days is None:
        raise ValueError("unknown window %r; one of %s" % (window, ", ".join(sorted(WINDOW_DAYS))))
    return 0 if days == 0 else int(at_ms) - days * DAY_MS


def read_plan(*, board_id: str, window: str, at_ms: int) -> dict:
    """What a board at this window reads: the settled-results floor and the fills floor, with the reasons.

    Returned as a dict rather than applied inside the query so the API can log/serve it: "which rows was this
    rank computed from" is the first question in any leaderboard dispute, and a plan that only exists inside a
    WHERE clause cannot be shown to the person asking.
    """
    board = bd.board(board_id)
    if board is None:
        raise ValueError("unknown board %r" % board_id)
    windows = tuple(board["windows"])
    key = str(window or board["window"])
    if key not in windows:
        raise ValueError("board %s reads %s, not %s" % (board_id, "/".join(windows), key))
    at = int(at_ms)
    settled_from = window_start_ms(key, at)
    fills_from = window_start_ms(key, at) if board_id == "volume" else 0
    if board_id == "rising":
        settled_from = at - RISING_READ_DAYS * DAY_MS
    return {
        "board": board_id,
        "window": key,
        "windowMs": 0 if key == "all" else WINDOW_DAYS[key] * DAY_MS,
        "settledFromMs": settled_from,
        "fillsFromMs": fills_from,
        "settledRule": {
            "rising": ("settled markets from the last %d days, because the metric subtracts the 7 days before "
                       "the window from the window itself" % RISING_READ_DAYS),
        }.get(board_id, ("settled markets inside the %s window, per market rather than per fill" % key)),
        "fillsRule": ("the window's own fills: the volume board ranks the window, so a round trip outside it is "
                      "not volume inside it" if board_id == "volume" else
                      "lifetime fills: this board's eligibility floor is a LIFETIME verified turnover floor, and "
                      "a windowed turnover would make the same wallet eligible on one window and not another"),
    }


def realised_micro(*, side: str, size_micro: int, notional_micro: int, winner, resolved: bool) -> int:
    """What one fill realised, in micro-USDC. The single implementation, called by the dossier as well.

    A winning BUY realises `shares − cost` (each share pays one dollar), a losing BUY realises `−cost`; a SELL
    is the mirror. An unresolved market realises **0 because nothing has been realised**, which is a different
    statement from "this trade broke even" — the caller carries `resolved` so the two can never be conflated.
    """
    if not resolved:
        return 0
    shares, cost = int(size_micro), int(notional_micro)
    won = bool(winner)
    if str(side) == "BUY":
        return (shares - cost) if won else -cost
    return (cost - shares) if won else cost


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def by_wallet(fills: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for f in fills or []:
        out.setdefault(str(f.get("wallet")), []).append(f)
    return out


def settled_results(fills: list[dict], *, since_ms: int = 0, until_ms: int | None = None) -> list[dict]:
    """One row per (wallet, settled market) in the window — the unit every board ranks.

    `atMs` is the market's LAST fill time, which is when the wallet stopped changing the position rather than
    when the market resolved (we do not hold a resolution timestamp for every market, and inventing one would
    put a date we do not have into the curve). Ordering the curve by it is therefore "the order the wallet
    finished with each market", which is what a realised curve is about.
    """
    grouped: dict[tuple, dict] = {}
    for f in fills or []:
        winner = f.get("winner")
        resolved = winner is not None
        ts = _int(f.get("tsMs"))
        if not resolved or ts < int(since_ms) or (until_ms is not None and ts >= int(until_ms)):
            continue
        key = (str(f.get("wallet")), str(f.get("conditionId")))
        row = grouped.get(key)
        if row is None:
            row = grouped[key] = {"wallet": key[0], "conditionId": key[1], "category": str(f.get("category") or ""),
                                  "atMs": ts, "realisedMicro": 0, "fills": 0}
        row["realisedMicro"] += realised_micro(side=str(f.get("side")), size_micro=_int(f.get("sizeMicro")),
                                               notional_micro=_int(f.get("notionalMicro")),
                                               winner=winner, resolved=True)
        row["fills"] += 1
        row["atMs"] = max(row["atMs"], ts)
        if not row["category"] and f.get("category"):
            row["category"] = str(f.get("category"))
    return [grouped[k] for k in sorted(grouped)]


def evidence(*, fills: list[dict], at_ms: int, plan: dict, copy_configs: list[dict] | None = None,
             blocked_conditions=(), created_ms: dict | None = None) -> list[dict]:
    """The whole population, as the engine's evidence dicts, for one board at one window.

    `anons` is deliberately absent: the engine ranks pseudonyms, and the API is the only layer that ever knows
    the address — so a `why` sentence returned to a user cannot leak one by accident.
    """
    at = int(at_ms)
    grouped = by_wallet(fills)
    copiers: dict[str, int] = {}
    for cfg in copy_configs or []:
        if not cfg.get("enabled", True):
            continue
        src = str(cfg.get("source") or "")
        if src:
            copiers[src] = copiers.get(src, 0) + 1
    created = dict(created_ms or {})
    out = []
    for wallet, rows in grouped.items():
        # Sorted, because "the same tape" has no order of its own: two reads of the same ledger (or a test that
        # reverses a list) must produce the same evidence, or a rank could move because a query plan changed.
        rows = sorted(rows, key=lambda f: (_int(f.get("tsMs")), str(f.get("conditionId")), str(f.get("tokenId")),
                                           str(f.get("side"))))
        window_fills = [f for f in rows if _int(f.get("tsMs")) >= plan["fillsFromMs"]]
        settled = settled_results(rows, since_ms=plan["settledFromMs"])
        first = min([_int(f.get("tsMs")) for f in rows] + [_int(created.get(wallet, 0)) or _int(at)])
        out.append({
            "wallet": wallet,
            "atMs": at,
            "firstSeenMs": first,
            "fills": window_fills if plan["board"] == "volume" else rows,
            "settled": settled,
            "copiers": copiers.get(wallet, 0),
            "blockedConditions": set(blocked_conditions or ()),
            "walletCreatedMs": _int(created.get(wallet, 0)),
        })
    out.sort(key=lambda ev: ev["wallet"])
    return out


def summarise(board_out: dict, *, excluded: int = 0) -> dict:
    """The board's own summary: what the counts are, so no screen has to derive them from a page of rows.

    `disputedWithheld` counts results the engine held back rather than zeroed, and it is summed over the WHOLE
    eligible set — so the caller passes the unpaged board (the engine's `limit` is a page size, and a count that
    depended on the page size would be a different number at limit=50 and limit=200).
    """
    rows = list(board_out.get("rows") or [])
    settled = sum(_int(r.get("settledMarkets")) for r in rows)
    return {
        "rankedTotal": _int(board_out.get("rankedTotal")),
        "unrankedTotal": _int(board_out.get("unrankedTotal")),
        "blewUpCount": _int(board_out.get("blewUpCount")),
        "provisionalCount": _int(board_out.get("provisionalCount")),
        "disputedWithheld": sum(_int(r.get("disputedExcluded")) for r in rows),
        "washedMicro": sum(_int(r.get("washedMicro")) for r in rows),
        "settledMarkets": settled,
        "excludedTotal": _int(excluded),
        "medianSettledMarkets": _median([_int(r.get("settledMarkets")) for r in rows]),
        "note": ("counts are over the whole eligible board, not the page; a blown-up wallet is counted here "
                 "because it is still on the board"),
    }


def _median(values: list[int]) -> int:
    xs = sorted(_int(v) for v in values or [])
    if not xs:
        return 0
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) // 2
