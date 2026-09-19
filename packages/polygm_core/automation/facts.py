"""The facts an automation trigger may read, in one place, for every caller that has to agree about them.

`Facts` itself lives here rather than in `engine.py` so this module can be imported by anything that has to
agree about the numbers without importing the engine — the direction of that dependency is the whole point of
splitting it out, and getting it backwards is an import cycle.

There are two callers and they must not disagree. The executor evaluates rules against facts at fire time; the
API's builder preview evaluates a rule the user has not saved yet. If those two read different numbers, the
preview is a preview of a different rule — and the failure mode is the worst kind: the rule fires in production
when the preview said it would not, which is exactly the "why did the bot do that" that D8 exists to answer.

This module used to be `AutomationEngine.facts_for` with its SQL inline. The API needs the same four reads, so
the reads live here and both sides call the same functions. `Store.market_quote` delegates to `quote()` too,
for the same reason: three copies of "what is the best bid" is three chances to disagree about whether an empty
book is fresh.

Two of these reads are worth naming, because P06 shipped a trigger vocabulary the executor could not fully
satisfy:

  * **the last price.** `price_cross` accepts `uses: "last"`, and `quote()` returned no `last_price_micro` at
    all, so `uses: "last"` could never fire in live mode — a rule the builder offers and the engine can never
    honour. It is now read from the tape.
  * **labels.** The `signal` trigger reads `Facts.labels`, and nothing populated it, so every template that
    watches a signal — including the protective "cancel the entry on a volume spike" rule — could only ever
    report "trigger not met". They are now assembled from the two places a label can come from: the alerts that
    fired on this market, and the wallet labels of the wallets that traded it.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..dbtypes import TimeColumnError, seconds_until

@dataclass(frozen=True)
class Facts:
    """Everything a trigger may look at. Deliberately a value object: a rule cannot call a function, because
    a rule that can call a function can fail in a way the run log cannot show."""
    last_price_micro: int | None = None
    best_bid_micro: int | None = None
    best_ask_micro: int | None = None
    quote_age_ms: int = 0
    stale_ms: int = 3_000
    now_ms: int = 0
    market_open_ms: int | None = None
    seconds_to_resolution: int | None = None
    accepting_orders: bool = True
    imbalance_bps: int = 0
    labels: tuple[str, ...] = ()
    position_shares_micro: int = 0
    avg_entry_micro: int | None = None

    @property
    def mid_micro(self) -> int | None:
        if self.best_bid_micro is None or self.best_ask_micro is None:
            return self.last_price_micro
        return (self.best_bid_micro + self.best_ask_micro) // 2

    @property
    def quote_usable(self) -> bool:
        return (self.last_price_micro is not None or self.mid_micro is not None) and self.quote_age_ms <= self.stale_ms

#: How long a label stays true. Long enough that a rule watching for "smart money" is not racing the ingest
#: loop's own cadence, short enough that a signal from this morning does not open a position this afternoon.
LABEL_WINDOW_MS = 30 * 60_000

#: The labels a rule may name, and where each one comes from. `signals.kind` is the ingest plane's word for
#: something that happened; `wallet_labels.label` is the same for a wallet. Both are mapped into the single
#: vocabulary `validate_rule` accepts, so a rule says `large_whale` and the log shows which signal produced it.
SIGNAL_LABELS = {"large_fill": "large_whale", "volume_spike": "volume_spike", "watched_wallet": "smart_money",
                 "negrisk_divergence": "negrisk_divergence"}
WALLET_LABELS = ("insider_suspect", "wash_like", "smart_money", "large_whale")


def quote(conn, market_id: str, *, at: int, depth: int = 25) -> dict:
    """The last book we hold for a market, plus the last tape print.

    An empty book returns `age_ms = 10**12`, not 0: a caller that reads "everything is fine" from a missing
    book has just approved an order with no reference price, and that is the hole `STALE_QUOTE` closes. The
    sentinel is deliberately absurd rather than merely large — it survives any subtraction a caller does to it.
    """
    r = conn.execute("SELECT side, price_micro, size_shares_micro, updated_ms FROM book_levels "
                     "WHERE market_id=? ORDER BY side, price_micro DESC LIMIT ?",
                     (market_id, depth * 2)).fetchall()
    last = conn.execute("SELECT price_micro FROM tape_fills WHERE condition_id = "
                        "(SELECT condition_id FROM markets WHERE id=?) AND price_micro IS NOT NULL "
                        "ORDER BY ts_ms DESC LIMIT 1", (market_id,)).fetchone()
    last_price = int(last[0]) if last and last[0] is not None else None
    if not r:
        return {"best_bid_micro": None, "best_ask_micro": None, "mid_micro": None, "age_ms": 10**12,
                "last_price_micro": last_price, "ask_ladder": [], "bid_ladder": []}
    bids = [(x[1], x[2]) for x in r if x[0] == "bid"]
    asks = sorted([(x[1], x[2]) for x in r if x[0] == "ask"])[:depth]
    bid = max((x[1] for x in r if x[0] == "bid"), default=None)
    ask = min((x[1] for x in r if x[0] == "ask"), default=None)
    age = at - max(int(x[3]) for x in r)
    mid = ((bid + ask) // 2) if (bid is not None and ask is not None) else None
    return {"best_bid_micro": bid, "best_ask_micro": ask, "mid_micro": mid, "age_ms": age,
            "last_price_micro": last_price, "ask_ladder": asks, "bid_ladder": sorted(bids, reverse=True)}


def market_labels(conn, market_id: str, *, at: int, window_ms: int = LABEL_WINDOW_MS) -> tuple[str, ...]:
    """The labels that are true of this market right now, in the engine's vocabulary.

    Two sources, because a label can be about the market or about the people trading it, and the trigger
    vocabulary does not distinguish: an alert that fired here recently (`signals`, mapped by `SIGNAL_LABELS`)
    and a wallet that traded here recently carrying a publishable label (`wallet_labels`). Only labels the
    engine accepts are returned — a label no rule can name is noise in a run log.
    """
    if not market_id:
        return ()
    since = at - window_ms
    labels: set[str] = set()
    cond = conn.execute("SELECT condition_id FROM markets WHERE id=?", (market_id,)).fetchone()
    if cond and cond[0]:
        for (kind,) in conn.execute("SELECT DISTINCT kind FROM signals WHERE condition_id=? AND fired_ms>=?",
                                    (str(cond[0]), since)).fetchall():
            mapped = SIGNAL_LABELS.get(str(kind))
            if mapped:
                labels.add(mapped)
        marks = ",".join("?" * len(WALLET_LABELS))
        for (label,) in conn.execute(
                "SELECT DISTINCT w.label FROM wallet_labels w WHERE w.label IN (%s) AND w.last_seen_ms >= ? "
                "AND EXISTS (SELECT 1 FROM tape_fills f WHERE f.wallet = w.wallet AND f.condition_id = ? "
                "            AND f.ts_ms >= ?)" % marks,
                tuple(WALLET_LABELS) + (since, str(cond[0]), since)).fetchall():
            labels.add(str(label))
    allowed = set(SIGNAL_LABELS.values()) | set(WALLET_LABELS)
    return tuple(sorted(labels & allowed))


def facts_for(conn, user_id: str, market_id: str, *, at: int) -> Facts:
    """Everything a trigger may read. Reads only our own tables — never the venue.

    That is not an optimisation: the ingest plane is the only thing that talks to Polymarket, and a rule that
    fetched its own price would be a rule with its own freshness bug, a second source of truth about "what is
    this worth", and no way for the run log to say which one it used.
    """
    if not market_id:
        return Facts(now_ms=at, accepting_orders=True, quote_age_ms=0)
    q = quote(conn, market_id, at=at)
    row = conn.execute("SELECT accepting_orders, first_seen_ms, end_ts FROM markets WHERE id=?",
                       (market_id,)).fetchone()
    pos = conn.execute("SELECT COALESCE(SUM(shares_open_micro),0), COALESCE(SUM(basis_micro),0) "
                       "FROM position_lots WHERE user_id=? AND market_id=? AND shares_open_micro>0",
                       (user_id, market_id)).fetchone()
    shares = int(pos[0] or 0) if pos else 0
    basis = int(pos[1] or 0) if pos else 0
    avg = (basis * 10**6 // shares) if shares > 0 and basis else None
    imbalance = 0
    depth = conn.execute("SELECT side, COALESCE(SUM(size_shares_micro),0) FROM book_levels "
                         "WHERE market_id=? GROUP BY side", (market_id,)).fetchall()
    # `book_levels.side` is 'bid'/'ask' (lowercase, the venue's own words), not 'BUY'/'SELL'. Getting this wrong
    # does not throw: it returns 0 depth on both sides, an imbalance of 0, and a book_imbalance rule that never
    # fires.
    per = {r[0]: int(r[1] or 0) for r in depth}
    bid_sz, ask_sz = per.get("bid", 0), per.get("ask", 0)
    if bid_sz + ask_sz > 0:
        imbalance = ((bid_sz - ask_sz) * 10_000) // (bid_sz + ask_sz)
    secs = None
    if row and row[2] is not None:
        try:
            secs = seconds_until(row[2], now_ms=at, column="markets.end_ts")
        except TimeColumnError:
            secs = None
    return Facts(last_price_micro=q.get("last_price_micro"), best_bid_micro=q["best_bid_micro"],
                 best_ask_micro=q["best_ask_micro"], quote_age_ms=q["age_ms"], now_ms=at,
                 market_open_ms=(int(row[1]) if row and row[1] else None),
                 seconds_to_resolution=secs, accepting_orders=bool(row[0]) if row else False,
                 imbalance_bps=imbalance, labels=market_labels(conn, market_id, at=at),
                 position_shares_micro=shares, avg_entry_micro=avg)


__all__ = ["LABEL_WINDOW_MS", "SIGNAL_LABELS", "WALLET_LABELS", "quote", "market_labels", "facts_for"]
