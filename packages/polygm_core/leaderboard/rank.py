"""P11 · The ranking engine: per-market results in, one board out, with the sentence that explains each rank.

Pure, integer-only, deterministic. The engine takes evidence (per-wallet settled results and tape fills) rather
than reading a database, for the same reason `radar/rankings.py` does: a ranking rule that can only be exercised
against a live database is a rule nobody tests, and the two things this file must never do — divide by a float or
order two wallets by anything but a stated tie-break — are exactly what a unit test can pin.

**What the ordering is allowed to depend on.** The score, and then the stated tie-breaks. Never the sample size
on its own: a wallet with 12 settled markets and a strong risk-adjusted record ranks above one with 61 settled
markets and a weak one, and that is the correct answer, not a bug to be smoothed over. The sample gate decides
*eligibility*, not order — which is why `explain()` exists and prints the two component sets side by side.
"""
from __future__ import annotations

from math import isqrt

from ..terminal import metrics as tm
from . import boards as bd
from . import integrity as ig


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def volatility_micro(results: list[int]) -> int:
    """The standard deviation of per-market results, in micro, as an integer (`isqrt` of the exact variance).

    Per-market, not per-fill: a wallet that traded the same market forty times made one decision, and counting the
    fills would make an active wallet look volatile for being active. The variance is computed with integers
    throughout (`(n*Σx² − (Σx)²) / n²`); a float here would put a rounding difference between two wallets into the
    ordering, and the ordering is what the product sells.
    """
    xs = [_int(x) for x in (results or [])]
    n = len(xs)
    if n == 0:
        return 0
    total = sum(xs)
    total_sq = sum(x * x for x in xs)
    var = (n * total_sq - total * total) // (n * n)
    return isqrt(max(0, var))


def trim_micro(results: list[int]) -> int:
    """What the numerator removes: the single best market, when that market was a profit.

    Uniform rather than conditional-on-suspicion. A rule that trimmed only the wallets it found suspicious would
    need a threshold for suspicion and a human to defend it; "the best market does not count" is one sentence,
    applies to everyone, and cannot be gamed by staying just under a line. It is skipped when the best market is a
    loss, because then there is no lucky win to remove and trimming would only punish a losing wallet twice.
    """
    xs = [_int(r) for r in (results or [])]
    best = max(xs, default=0)
    return max(0, best)


def score_micro(*, settled: list[dict], curve: list[dict] | None = None) -> dict:
    """The default board's score for one wallet, with every component it was built from.

    The denominator is `max(drawdown, VOLATILITY_MULTIPLE * sigma)` — the risk that happened, floored by the risk
    the ledger shows was typical. Stated as a `max` rather than a sum so that it reduces to the copy screen's own
    rule (net per unit of drawdown) when volatility is the smaller of the two: two surfaces ranking the same
    wallets must not disagree about what risk is.
    """
    rows = [r for r in (settled or []) if not r.get("disputed")]
    results = [_int(r.get("realisedMicro")) for r in rows]
    net = sum(results)
    best = max(results, default=0)
    trim = trim_micro(results)
    trimmed = net - trim
    curve = curve if curve is not None else [{"cumMicro": sum(results[: i + 1])} for i in range(len(results))]
    drawdown = tm.max_drawdown_micro(curve)
    sigma = volatility_micro(results)
    scale = max(drawdown, bd.VOLATILITY_MULTIPLE * sigma, 1)
    wins = sum(1 for r in results if r > 0)
    gate = tm.win_rate(wins, len(rows))
    return {
        "realisedMicro": net,
        "trimmedMicro": trimmed,
        "trimmedMicro_removed": trim,
        "bestMicro": best,
        "trimMicro": trim,
        "volatilityMicro": sigma,
        "maxDrawdownMicro": drawdown,
        "scaleMicro": scale,
        "scoreBps": trimmed * 10_000 // scale,
        "settledMarkets": len(rows),
        "wins": wins,
        "winRateBps": gate["bps"],
        "insufficientSample": gate["insufficientSample"],
        "sampleNote": gate["reason"],
        "formula": ("net realised after fees excluding the single best market, per unit of risk, where risk = "
                    "max(largest drawdown, %d x the standard deviation of per-market results)" % bd.VOLATILITY_MULTIPLE),
    }


def _wallet_evidence(w: dict) -> dict:
    """Normalise one wallet's evidence, computing what the caller did not supply."""
    fills = list(w.get("fills") or [])
    excluded = ig.disputed_exclusions(settled=list(w.get("settled") or []),
                                      blocked_conditions=set(w.get("blockedConditions") or set()))
    settled = excluded["settled"]
    results = [_int(r.get("realisedMicro")) for r in settled]
    curve = w.get("curve")
    if curve is None:
        running, curve = 0, []
        for r in sorted(settled, key=lambda x: _int(x.get("atMs"))):
            running += _int(r.get("realisedMicro"))
            curve.append({"cumMicro": running})
    score = score_micro(settled=settled, curve=curve)
    volume = (ig.wash_volume(fills) if fills else
              {"washedMicro": 0, "verifiedMicro": _int(w.get("verifiedVolumeMicro")), "roundTrips": 0,
               "rule": "", "note": ""})
    state = ig.wallet_state(first_seen_ms=_int(w.get("firstSeenMs")), at_ms=_int(w.get("atMs")),
                            curve=curve, best_micro=max(results, default=0), realised_micro=sum(results))
    return {"wallet": str(w.get("wallet") or ""), "score": score, "volume": volume, "state": state,
            "excluded": excluded, "settled": settled, "results": results, "curve": curve,
            "firstSeenMs": _int(w.get("firstSeenMs")), "copiers": _int(w.get("copiers")),
            "medianWinMicro": tm.median_micro([x for x in results if x > 0])}


def _net_between(results: list[dict], *, since_ms: int, until_ms: int) -> int:
    return sum(_int(r.get("realisedMicro")) for r in results
               if since_ms <= _int(r.get("atMs")) < until_ms)


def _rising(ev: dict, *, at_ms: int) -> dict:
    """Net realised in the last 7 days minus the 7 days before it, with both halves shown."""
    day = 86_400_000
    now = _net_between(ev["settled"], since_ms=at_ms - 7 * day, until_ms=at_ms + 1)
    before = _net_between(ev["settled"], since_ms=at_ms - 14 * day, until_ms=at_ms - 7 * day)
    in_window = [r for r in ev["settled"] if _int(r.get("atMs")) >= at_ms - 7 * day]
    return {"improvementMicro": now - before, "weekMicro": now, "priorWeekMicro": before,
            "settledInWindow": len(in_window),
            "formula": "net realised in the last 7 days minus net realised in the 7 days before it"}


def _category_rows(ev: dict, category: str) -> dict:
    mine = [r for r in ev["settled"] if str(r.get("category") or "") == category]
    share = len(mine) * 10_000 // max(1, len(ev["settled"]))
    return {"settled": mine, "shareBps": share,
            "score": score_micro(settled=mine) if mine else None}


def _gate_failure(ev: dict, board: dict) -> list[str]:
    """Why this wallet is not on this board. Empty means it is."""
    out: list[str] = []
    bid = board["id"]
    if bid == "copied":
        if ev["copiers"] <= 0:
            out.append("nobody is copying this wallet")
        if ev["volume"]["verifiedMicro"] < bd.MIN_VERIFIED_VOLUME_MICRO:
            out.append("turnover %d micro is below the %d micro floor"
                       % (ev["volume"]["verifiedMicro"], bd.MIN_VERIFIED_VOLUME_MICRO))
        if ev.get("farm"):
            out.append("fills are mechanically derived from %s, so it cannot rank as copied demand"
                       % ev["farm"]["derivedFrom"])
        return out
    if bid == "rising":
        if ev["state"]["ageDays"] < bd.PROVISIONAL_DAYS:
            out.append("a wallet %d days old has no 7-day history to have improved on" % ev["state"]["ageDays"])
        if ev["rising"]["settledInWindow"] < bd.RISING_SAMPLE:
            out.append("%d settled markets inside the window; this board needs %d"
                       % (ev["rising"]["settledInWindow"], bd.RISING_SAMPLE))
        return out
    if bid == "volume":
        if ev["volume"]["verifiedMicro"] <= 0:
            out.append("no verified turnover in the window")
        return out
    # The skill boards share the platform's sample gate and the turnover floor.
    wanted = ev["settled"]
    if bid == "category":
        cat = board.get("category") or ""
        mine = ev["category"]["settled"] if ev.get("category") else []
        if len(mine) < bd.MIN_RESOLVED:
            out.append("%d settled markets in %s; a category board needs %d"
                       % (len(mine), cat or "this category", bd.MIN_RESOLVED))
        if ev["category"]["shareBps"] < bd.CATEGORY_SHARE_BPS:
            out.append("%d%% of this wallet's resolved markets are in %s, below the %d%% a specialist needs"
                       % (ev["category"]["shareBps"] // 100, cat or "it", bd.CATEGORY_SHARE_BPS // 100))
    elif len(wanted) < bd.MIN_RESOLVED:
        out.append("%d settled markets; this board needs %d" % (len(wanted), bd.MIN_RESOLVED))
    if ev["volume"]["verifiedMicro"] < bd.MIN_VERIFIED_VOLUME_MICRO and not ev.get("skipTurnoverGate"):
        out.append("verified turnover %d micro is below the %d micro floor"
                   % (ev["volume"]["verifiedMicro"], bd.MIN_VERIFIED_VOLUME_MICRO))
    return out


def _row_for(board: dict, ev: dict, rank: int) -> dict:
    """One ranked row: the score, every component behind it, the labels, and the components of the order."""
    score = ev.get("boardScore") or ev["score"]
    # The rising board's evidence reaches 14 days so its subtraction has two halves; the number the row shows is
    # the one the board ranked: settled markets INSIDE the 7-day window.
    settled_count = (ev["rising"]["settledInWindow"] if board["id"] == "rising" and ev.get("rising")
                     else len(ev["settled"]))
    row = {
        "rank": rank,
        "wallet": ev["wallet"],
        "scoreBps": score["scoreBps"],
        "state": ev["state"]["state"],
        "labels": list(ev["state"]["labels"]),
        "settledMarkets": settled_count,
        "wins": score["wins"],
        "winRateBps": score["winRateBps"],
        "insufficientSample": score["insufficientSample"],
        "sampleNote": score["sampleNote"],
        "realisedMicro": score["realisedMicro"],
        "trimmedMicro": score["trimmedMicro"],
        "volatilityMicro": score["volatilityMicro"],
        "maxDrawdownMicro": score["maxDrawdownMicro"],
        "bestTradeShareBps": ev["state"]["bestTradeShareBps"],
        "ageDays": ev["state"]["ageDays"],
        "verifiedVolumeMicro": ev["volume"]["verifiedMicro"],
        # Which window that turnover was measured over. The volume board ranks the window it was asked for;
        # every other board states LIFETIME, because that is the window its eligibility floor is written in.
        "volumeWindow": board.get("volumeWindow") or "lifetime",
        "washedMicro": ev["volume"]["washedMicro"],
        "washNote": ev["volume"]["note"],
        "copiers": ev["copiers"],
        "disputedExcluded": ev["excluded"]["excluded"],
        "board": board["id"],
        "components": {"scaleMicro": score["scaleMicro"], "trimRemovedMicro": score["trimmedMicro_removed"],
                       "volatilityMultiple": bd.VOLATILITY_MULTIPLE, "formula": score["formula"],
                       "trimRule": ("the single best market is excluded from the numerator for every wallet, when "
                                    "that market was a profit")},
    }
    if ev.get("rising"):
        row["improvementMicro"] = ev["rising"]["improvementMicro"]
        row["weekMicro"] = ev["rising"]["weekMicro"]
        row["priorWeekMicro"] = ev["rising"]["priorWeekMicro"]
    if ev.get("category"):
        row["category"] = board.get("category")
        row["categoryShareBps"] = ev["category"]["shareBps"]
        row["categorySettled"] = len(ev["category"]["settled"])
    if ev.get("farm"):
        row["labels"].append("derived from %s" % ev["farm"]["derivedFrom"])
    return row


def _sort_key(board: dict):
    bid = board["id"]
    if bid == "volume":
        return lambda r: (-r["verifiedVolumeMicro"], -r["settledMarkets"], r["wallet"])
    if bid == "rising":
        return lambda r: (-r["improvementMicro"], -r["settledMarkets"], r["wallet"])
    if bid == "copied":
        return lambda r: (-r["copiers"], -r["verifiedVolumeMicro"], r["wallet"])
    return lambda r: (-r["scoreBps"], -r["settledMarkets"], r["maxDrawdownMicro"], r["wallet"])


def rank_board(*, board_id: str, wallets: list[dict], at_ms: int, limit: int = 100,
               category: str = "", window: str = "") -> dict:
    """Rank the eligible wallets on one board and say, for each, why they are where they are.

    `wallets` are raw evidence dicts (see `_wallet_evidence`); `limit` truncates the returned rows but never the
    eligibility decision — a board's size is a page size, not a definition of the board.

    `window` is the window the EVIDENCE was read at (`source.read_plan`), not a filter this function applies: the
    engine ranks what it is handed, and the caller that read a 7-day tape must not also have to remember to say
    so. It is validated against the board's own window list rather than trusted, and it travels into the board
    summary and into each row's `volumeWindow`.
    """
    b = bd.board(board_id)
    if b is None:
        raise ValueError("unknown board %r" % board_id)
    board = dict(b)
    if window:
        if window not in tuple(board["windows"]):
            raise ValueError("board %s reads %s, not %s" % (board_id, "/".join(board["windows"]), window))
        board["window"] = str(window)
    board["volumeWindow"] = board["window"] if board_id == "volume" else "lifetime"
    if board_id == "category":
        if category not in bd.CATEGORIES:
            raise ValueError("category board needs one of %s" % (", ".join(bd.CATEGORIES),))
        board["category"] = category
    # Copy farms need the whole population, not one wallet at a time: "derived from whom" is a question about the
    # other wallets' tapes.
    tapes = {str(w.get("wallet")): list(w.get("fills") or []) for w in wallets}
    rows, unranked = [], []
    for w in wallets:
        ev = _wallet_evidence(w)
        if board_id == "category":
            ev["category"] = _category_rows(ev, board["category"])
            if ev["category"]["score"]:
                ev["boardScore"] = ev["category"]["score"]
        if board_id == "rising":
            ev["rising"] = _rising(ev, at_ms=at_ms)
        if board_id == "copied":
            others = {k: v for k, v in tapes.items() if k != ev["wallet"]}
            ev["farm"] = ig.copy_farm(wallet=ev["wallet"], own=tapes.get(ev["wallet"], []), candidates=others)
        reasons = _gate_failure(ev, board)
        if reasons:
            unranked.append({"wallet": ev["wallet"], "reasons": reasons,
                             "settledMarkets": len(ev["settled"]),
                             "verifiedVolumeMicro": ev["volume"]["verifiedMicro"],
                             "note": "not ranked, and the reason is stated rather than the row being blank"})
        else:
            rows.append(ev)
    # Order the ROWS, not the evidence: the sort keys are the published fields (`scoreBps`, `verifiedVolumeMicro`),
    # and building them before sorting is what keeps the ordering and the thing displayed the same object.
    ordered = sorted((_row_for(board, ev, 0) for ev in rows), key=_sort_key(board))
    for i, row in enumerate(ordered):
        row["rank"] = i + 1
    out_rows = ordered
    visible = out_rows[:limit]
    return {
        "board": board["id"],
        "label": board["label"],
        "category": board.get("category") or None,
        "window": board["window"],
        "formula": board["formula"],
        "gate": board["gate"],
        "tieBreaks": board["tieBreaks"],
        "cadence": board["cadence"],
        "cadenceMs": _int(board.get("cadenceMs")),
        "windows": list(board["windows"]),
        "rows": visible,
        "rankedTotal": len(out_rows),
        "unranked": unranked[:limit],
        "unrankedTotal": len(unranked),
        "blewUpCount": sum(1 for r in out_rows if r["state"] == "blew_up"),
        "provisionalCount": sum(1 for r in out_rows if r["ageDays"] < bd.PROVISIONAL_DAYS),
        "note": ("ranked by %s, and every row carries the components it was ranked on: a leaderboard that cannot "
                 "explain a rank is a leaderboard nobody should copy from" % board["label"].lower()),
        "atMs": _int(at_ms),
    }


def explain(*, board_id: str, wallets: list[dict], at_ms: int, a: str, b: str,
            category: str = "", window: str = "") -> dict:
    """Why `a` is above (or below) `b`, in one sentence, from the two component sets.

    This is the D6 "why this rank" panel and the P11 gate's own question. It answers with numbers rather than
    adjectives, and it names the sample size explicitly in both directions — because the interesting case is the
    one a user will report as a bug: fewer settled markets, better rank.
    """
    board_out = rank_board(board_id=board_id, wallets=wallets, at_ms=at_ms, limit=10_000, category=category,
                           window=window)
    index = {r["wallet"]: r for r in board_out["rows"]}
    miss = [w for w in (a, b) if w not in index]
    if miss:
        return {"ok": False, "why": "not on this board: %s" % ", ".join(miss),
                "unranked": [u for u in board_out["unranked"] if u["wallet"] in miss]}
    ra, rb = index[a], index[b]
    key = {"volume": "verifiedVolumeMicro", "rising": "improvementMicro", "copied": "copiers"}.get(board_id, "scoreBps")
    better, worse = (ra, rb) if ra["rank"] < rb["rank"] else (rb, ra)
    sample_note = ("the sample size did not decide this: %s has %d settled markets to %s's %d, and the order comes "
                   "from %s" % (better["wallet"].split("_")[-1][:6], better["settledMarkets"],
                                worse["wallet"].split("_")[-1][:6], worse["settledMarkets"],
                                board_out["tieBreaks"].split(",")[0]))
    why = ("%s is rank %d with %s = %s; %s is rank %d with %s = %s. %s"
           % (better["wallet"], better["rank"], key, better.get(key),
              worse["wallet"], worse["rank"], key, worse.get(key),
              "Tie on the metric, broken by settled markets then drawdown." if better.get(key) == worse.get(key)
              else sample_note))
    return {"ok": True, "board": board_id, "why": why, "a": ra, "b": rb,
            "components": {"a": ra["components"], "b": rb["components"]},
            "note": ("a rank is a claim about a score, not about how long someone has been trading; the sample "
                     "gate decides who is on the board, never who is above whom")}
