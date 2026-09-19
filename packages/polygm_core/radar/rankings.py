"""P10 · D5's Wallet Radar rankings, as pure functions.

Four rankings over the same window, and each one is a *different question* about the same wallets:

  * **most active**   — who is actually trading here (fill counts, not dollars: a market-maker is activity);
  * **highest profit** — realised PnL, WITH the sample gate: a wallet with three resolved markets does not get a
    profit ranking at all, because "top earner" off three trades is a coin that landed twice;
  * **earliest**      — who was in before the crowd, measured by their FIRST fill in each matched market. This is
    the one ranking where a losing wallet can top the list, and that is the point: being early is a fact about
    when, not about whether it worked;
  * **shared exposure** — who holds more than one of the markets you picked. Those wallets are the ones where a
    single piece of news hits several of your positions at once.

Every row carries its reason sentence, so a ranking is explained by the data that produced it rather than by the
label on its tab.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Sequence

RANKINGS = ("active", "profit", "earliest", "overlap")
#: Cost control is part of the contract: one scan is up to ten markets, and the cache means a repeated scan for
#: the same set is free (the caller is told it was cached).
MAX_MARKETS = 10
CACHE_TTL_MS = 60_000
RANK_LIMIT = 25


def _int(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("radar arithmetic is integer-only, got %r" % (value,))
    return value


def validate_markets(market_ids: Sequence[str]) -> tuple[list[str], str | None]:
    """(the cleaned list, the refusal reason or None). An empty or oversized scan is refused, not clamped."""
    clean = [str(m).strip() for m in (market_ids or []) if str(m).strip()]
    if not clean:
        return [], "radar needs at least one market"
    if len(clean) > MAX_MARKETS:
        return clean[:MAX_MARKETS], "radar scans up to %d markets; %d were sent" % (MAX_MARKETS, len(clean))
    return clean, None


def rank(fills: Iterable[dict], *, market_ids: Sequence[str], sample_gate_n: int) -> dict:
    """The four rankings, from the fills we hold for the selected markets.

    `fills` are internal rows in the same shape `_trader_fills` already builds: `wallet`, `marketId`, `side`,
    `notionalMicro`, `tsMs`, `resolved`, `won`, `realisedMicro`. Deliberately a plain dict rather than a
    dataclass: a second shape for data the app already assembles is a second place to be wrong.
    """
    from polygm_core.terminal.metrics import win_rate as _win_rate

    wanted = {str(m) for m in market_ids}
    per_wallet: dict[str, dict] = {}
    for f in fills:
        market = str(f["marketId"])
        if wanted and market not in wanted:
            continue
        wallet = str(f["wallet"])
        row = per_wallet.setdefault(
            wallet,
            {"wallet": wallet, "fills": 0, "markets": set(), "boughtMicro": 0, "soldMicro": 0,
             "realisedMicro": 0, "resolved": {}, "firstMs": {}, "byMarketMicro": {}},
        )
        row["fills"] += 1
        row["markets"].add(market)
        row["byMarketMicro"][market] = row["byMarketMicro"].get(market, 0) + _int(f["notionalMicro"])
        if str(f["side"]) == "BUY":
            row["boughtMicro"] += _int(f["notionalMicro"])
        else:
            row["soldMicro"] += _int(f["notionalMicro"])
        if f.get("resolved"):
            row["realisedMicro"] += _int(f.get("realisedMicro") or 0)
            # One vote per MARKET per wallet, then the gate counts markets: a wallet that traded a market six
            # times made one decision about it.
            prev = row["resolved"].get(market)
            won = bool(f.get("won"))
            if prev is None:
                row["resolved"][market] = won
            elif won and not prev:
                row["resolved"][market] = True
        first = row["firstMs"].get(market)
        if first is None or _int(f["tsMs"]) < first:
            row["firstMs"][market] = _int(f["tsMs"])

    rows = []
    for wallet, row in per_wallet.items():
        settled = len(row["resolved"])
        gate = _win_rate(sum(1 for v in row["resolved"].values() if v), settled)
        rows.append(
            {
                "wallet": wallet,
                "fills": row["fills"],
                "markets": sorted(row["markets"]),
                "boughtMicro": row["boughtMicro"],
                "soldMicro": row["soldMicro"],
                "realisedMicro": row["realisedMicro"],
                "winRateBps": gate["bps"],
                "insufficientSample": gate["insufficientSample"],
                "sampleNote": gate["reason"],
                "firstMs": min(row["firstMs"].values()) if row["firstMs"] else 0,
            }
        )

    # Each ranking gets its OWN copies of the rows. The first draft assigned `reason` onto the shared row
    # objects, so the last ranking to run overwrote the sentence every other ranking had just written - four
    # tabs, one reason, and the reason belonged to whichever list happened to be built last.
    def row_of(r: dict, reason: str) -> dict:
        return {**r, "reason": reason}

    out: dict[str, list[dict]] = {}
    # most active: fill count first, then notional, then the pseudonym - a total order, so two calls for the same
    # window return the same page.
    active = sorted(rows, key=lambda r: (-r["fills"], -r["boughtMicro"] - r["soldMicro"], r["wallet"]))
    out["active"] = [row_of(r, "%d fill%s across %d of your markets"
                            % (r["fills"], "" if r["fills"] == 1 else "s", len(r["markets"])))
                     for r in active[:RANK_LIMIT]]
    # highest profit: only wallets that clear the sample gate are ranked at all. The rest are returned under
    # `unranked` carrying the gate's own refusal sentence, so the screen has something to show a user who asks
    # "why is this wallet not in the list" instead of an absence.
    gated = [r for r in rows if not r["insufficientSample"]]
    profit = sorted(gated, key=lambda r: (-r["realisedMicro"], r["wallet"]))
    out["profit"] = [row_of(r, "realised %s, win rate %s over %d settled markets (gate %d)"
                            % (_usd(r["realisedMicro"]), _bps(r["winRateBps"]), len(r["markets"]), sample_gate_n))
                     for r in profit[:RANK_LIMIT]]
    unranked = sorted((r for r in rows if r["insufficientSample"]),
                      key=lambda r: (-r["fills"], r["wallet"]))
    out["unranked"] = [row_of(r, r["sampleNote"]) for r in unranked[:RANK_LIMIT]]
    # earliest: the first fill in the set, with the moment stated so "early" is checkable. A losing wallet can
    # top this list, and that is the point of having it as its own ranking rather than a filter on the others.
    earliest = sorted(rows, key=lambda r: (r["firstMs"] or 1 << 62, r["wallet"]))
    out["earliest"] = [row_of(r, "first fill in the set at %s" % _iso(r["firstMs"]))
                       for r in earliest[:RANK_LIMIT]]
    # shared exposure: wallets in more than one of the selected markets - the ones where one headline moves
    # several of the user's markets at once.
    shared = [r for r in rows if len(r["markets"]) > 1]
    overlap = sorted(shared, key=lambda r: (-len(r["markets"]), -r["boughtMicro"], r["wallet"]))
    out["overlap"] = [row_of(r, "holds %d of the %d markets you picked: one piece of news moves all of them"
                             % (len(r["markets"]), len(market_ids)))
                      for r in overlap[:RANK_LIMIT]]
    out["rankingsMeta"] = [
        {"id": "active", "label": "most active", "question": "who is actually trading here"},
        {"id": "profit", "label": "highest profit", "question":
            "who made money, counting only wallets with %d or more settled markets" % sample_gate_n},
        {"id": "earliest", "label": "earliest", "question":
            "who was in before the crowd; a losing wallet can top this list, which is the point"},
        {"id": "overlap", "label": "shared exposure", "question":
            "who holds several of your markets at once"},
    ]
    out["scanned"] = len(rows)
    out["sampleGate"] = sample_gate_n
    return out


def cache_key(market_ids: Sequence[str]) -> str:
    """Order-insensitive: the same three markets in a different order is the same scan, and paying twice for it
    would make the quota a lie."""
    return ",".join(sorted(str(m) for m in market_ids))


def _usd(micro: int) -> str:
    sign = "-" if micro < 0 else ""
    abs_v = abs(micro)
    return "%s$ %d.%02d" % (sign, abs_v // 1_000_000, (abs_v % 1_000_000) // 10_000)


def _bps(bps) -> str:
    if bps is None:
        return "no sample"
    return "%d.%02d%%" % (bps // 100, bps % 100)


def _iso(ms: int) -> str:
    if not ms:
        return "-"
    import datetime
    # Integer seconds: `ms / 1000` is a float, and this file is inside the gate's no-float scan.
    return datetime.datetime.fromtimestamp(ms // 1000, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
