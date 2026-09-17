"""L2 book maintenance, and the part of ingest where being quietly wrong is fatal.

A snapshot plus deltas is the only affordable design, and it has exactly one failure mode that matters: a
dropped delta leaves a book that looks fresh and is not. The venue gives us no sequence number, so this module
uses the three checks it does give us, in this order of trust:

1. `price_change` frames carry `best_bid`/`best_ask` — the venue's own top of book, next to every delta. If
   our derived top disagrees, a delta was lost. This is the detector that actually fires; the others are
   backstops.
2. A TTL (`stale_ms_book`, 3 s from P01's measurement). Silence is not freshness.
3. The `hash` field. Present on both the book snapshot and each price_change, but its exact scope is not
   documented and a single observation cannot establish what it hashes, so it is recorded and diffed as a
   *diagnostic* — a mismatch schedules a resync, and it is never the sole reason an order is allowed.

One-sided books are not an error state: a market at 0.999 legitimately has 63 bid levels and zero asks (the
live capture in tests/fixtures/p05/clob_book.json is exactly that), and the Fed market P01 recorded had 94 ask
levels totalling $21.9M and no bids. Depth, spread and imbalance all have to answer something sensible for a
book with one side empty, because "mid = null" on the terminal during a resolution run is a bug report.
"""
from __future__ import annotations

from dataclasses import dataclass, field

TICK_CROSS_HIGH, TICK_CROSS_LOW = 960_000, 40_000     # 0.96 / 0.04: where the venue swaps 0.001 -> 0.01


@dataclass
class Book:
    token_id: str
    bids: dict = field(default_factory=dict)          # price_micro -> size_micro (shares)
    asks: dict = field(default_factory=dict)
    ts_ms: int = 0
    hash: str = ""
    tick_size: str = "0.01"
    min_order_size: str = "5"
    applied_deltas: int = 0
    resyncs: int = 0
    resync_reasons: list[str] = field(default_factory=list)
    last_declared_best: tuple | None = None           # (best_bid, best_ask) the venue said in the last delta
    declared_best_at_ms: int = 0
    snapshot_at_ms: int = 0
    # The counters the chaos test reads. They are here rather than in a log line because a log cannot be
    # asserted on: `dup_prices` proves the delta reducer is idempotent, and `gap_detections` proves the
    # detector fired at all (a silent resync that never triggered is the same as no resync).
    dup_prices: int = 0
    gap_detections: int = 0
    gap_detections_last: str = ""
    stale_reads: int = 0

    @property
    def best_bid(self) -> int | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> int | None:
        return min(self.asks) if self.asks else None

    @property
    def one_sided(self) -> bool:
        return not (self.bids and self.asks)

    def mid_micro(self) -> int | None:
        b, a = self.best_bid, self.best_ask
        if b is None and a is None:
            return None
        if b is None or a is None:
            # One-sided book: there is no mid, and inventing one (say, the sole side +/- a tick) draws a line
            # on a chart that the venue did not price. Return None and let the UI say "no book".
            return None
        return (b + a) // 2

    def spread_micro(self) -> int | None:
        b, a = self.best_bid, self.best_ask
        return None if b is None or a is None else a - b

    def depth_at(self, cents: int) -> tuple[int, int]:
        """Cumulative size (shares, micro) within `cents` of the touch, per side. A depth number without the
        window it was taken over is meaningless, so the window is an argument and never a default."""
        tick = int(cents * 10_000)
        b, a = self.best_bid, self.best_ask
        bid_depth = sum(v for p, v in self.bids.items() if b is not None and p > b - tick)
        ask_depth = sum(v for p, v in self.asks.items() if a is not None and p < a + tick)
        return bid_depth, ask_depth

    def imbalance(self, cents: int = 5) -> float | None:
        bd, ad = self.depth_at(cents)
        if not (bd or ad):
            return None
        return (bd - ad) / (bd + ad)

    # ------------------------------------------------------------------ apply
    def apply_snapshot(self, snap: dict, now_ms: int) -> None:
        """Full replace. Never merge: a snapshot is the truth at `snap.ts_ms`, and a level that vanished must
        vanish here (a stale resting order that we keep showing is a user placing against a ghost)."""
        self.bids = dict(snap["bids"])
        self.asks = dict(snap["asks"])
        self.ts_ms = snap["ts_ms"]
        self.hash = snap.get("hash") or ""
        self.tick_size = snap.get("tick_size") or self.tick_size
        self.min_order_size = snap.get("min_order_size") or self.min_order_size
        self.snapshot_at_ms = now_ms
        self.last_declared_best = (max(self.bids) if self.bids else None,
                                   min(self.asks) if self.asks else None)
        self.declared_best_at_ms = snap["ts_ms"]

    def apply_delta(self, ch: dict, now_ms: int) -> str:
        """Fold one venue price_change in. Returns applied | ignored | resync.

        Out-of-order deltas are DROPPED, not applied: the venue stamps each frame, and replaying an old price
        over a new one is how a book walks backwards. A drop is not a loss if the next snapshot fixes it, which
        is why `ignored` bumps the resync counter rather than being silent.
        """
        ts = ch.get("ts_ms") or 0
        if ts and ts < self.ts_ms:
            self.resync_reasons.append("out-of-order delta at %d (book at %d)" % (ts, self.ts_ms))
            if len(self.resync_reasons) > 20:
                del self.resync_reasons[20:]
            return "ignored"
        table = self.bids if ch["side"] == "BUY" else self.asks
        size = ch["size_micro"]
        if size == 0:
            table.pop(ch["price_micro"], None)
        else:
            if ch["price_micro"] in table:
                self.dup_prices += 1               # venue sends the new size for a level, not an increment
            table[ch["price_micro"]] = size
        self.applied_deltas += 1
        if ch.get("best_bid_micro") is not None or ch.get("best_ask_micro") is not None:
            self.last_declared_best = (ch.get("best_bid_micro"), ch.get("best_ask_micro"))
            self.declared_best_at_ms = ts or now_ms
        if ch["price_micro"] >= TICK_CROSS_HIGH or ch["price_micro"] <= TICK_CROSS_LOW:
            return "resync"                        # tick size changes across 0.96/0.04: the grid we assumed
        return "applied"                           # may no longer be the grid we hold

    def note_snapshot_tick_change(self, new_tick: str) -> bool:
        changed = new_tick and new_tick != self.tick_size
        if changed:
            self.tick_size = new_tick
        return changed

    # ------------------------------------------------------------------ health
    def needs_resync(self, now_ms: int, stale_ms: int) -> str | None:
        """Why we must re-snapshot, or None if this book may be trusted. Checked in order of reliability."""
        if not self.ts_ms:
            return "never snapshotted"
        if now_ms - self.ts_ms > stale_ms:
            self.stale_reads += 1
            return "age %dms > %dms" % (now_ms - self.ts_ms, stale_ms)
        if self.last_declared_best is not None:
            bb, ba = self.last_declared_best
            if bb is not None and self.bids and bb != self.best_bid:
                self.gap_detections += 1
                self.gap_detections_last = "declared best_bid %s vs ours %s" % (bb, self.best_bid)
                return "declared best_bid disagrees (venue %s, book %s)" % (bb, self.best_bid)
            if ba is not None and self.asks and ba != self.best_ask:
                self.gap_detections += 1
                self.gap_detections_last = "declared best_ask %s vs ours %s" % (ba, self.best_ask)
                return "declared best_ask disagrees (venue %s, book %s)" % (ba, self.best_ask)
        return None

    def snapshot_age_ms(self, now_ms: int) -> int:
        return max(0, now_ms - self.ts_ms)

    def as_dict(self, now_ms: int) -> dict:
        """What the API serves and what the UI renders. Every field a trader can act on carries its own age,
        because a price without a timestamp is a rumour (shared context, freshness rule)."""
        bd, ad = self.depth_at(1), self.depth_at(5)
        return {
            "tokenId": self.token_id,
            "bestBid": self.best_bid, "bestAsk": self.best_ask,
            "mid": self.mid_micro(), "spreadMicro": self.spread_micro(),
            "oneSided": self.one_sided,
            "depth1c": {"bid": bd[0], "ask": bd[1]},
            "depth5c": {"bid": ad[0], "ask": ad[1]},
            "imbalance": self.imbalance(5),
            "levels": {"bids": len(self.bids), "asks": len(self.asks)},
            "asOf": self.ts_ms, "ageMs": self.snapshot_age_ms(now_ms),
            "tickSize": self.tick_size, "appliedDeltas": self.applied_deltas, "resyncs": self.resyncs,
        }


class BookSet:
    """The tracked set, its sharding across connections, and its memory budget.

    Tracked = user watchlists (never dropped) + alerts with an open rule + top-N by 24h volume. The N is set by
    the arithmetic below, not by taste, and the per-connection subscription cap (200, measured) is what turns
    "many markets" into "several sockets".
    """

    SUBS_PER_CONNECTION = 200                       # measured; the venue closes a connection that exceeds it
    BYTES_PER_LEVEL = 56                            # two 28-byte ints in a dict entry, CPython 3.13 64-bit
    BYTES_PER_BOOK = 6_000                          # dict overhead + the dataclass + two sparse levels' worth

    def __init__(self, max_books: int = 2_000, stale_ms: int = 3_000):
        self.max_books = max_books
        self.stale_ms = stale_ms
        self.books: dict[str, Book] = {}
        self.priority: dict[str, int] = {}          # 0 watchlist, 1 alert, 2 topN  (lower = keep longer)
        self.dropped: list[str] = []

    def watch(self, token_id: str, priority: int = 2) -> None:
        self.priority[token_id] = min(priority, self.priority.get(token_id, 9))
        self.books.setdefault(token_id, Book(token_id))
        self.enforce_cap()

    def unwatch(self, token_id: str) -> None:
        self.books.pop(token_id, None)
        self.priority.pop(token_id, None)

    def enforce_cap(self) -> int:
        """Backpressure, by priority order: long tail out before watchlists. Returns how many were dropped.

        Dropping is the correct answer to overload; queueing is not, because a book that is 40 s behind is
        already useless and holding it costs the same memory as a fresh one.
        """
        over = len(self.books) - self.max_books
        if over <= 0:
            return 0
        victims = sorted((p, t) for t, p in self.priority.items() if p >= 2)[:over]
        for _p, t in victims:
            self.books.pop(t, None)
            self.priority.pop(t, None)
            self.dropped.append(t)
        return len(victims)

    def shard(self) -> list[list[str]]:
        ids = sorted(self.books)
        cap = self.SUBS_PER_CONNECTION
        return [ids[i:i + cap] for i in range(0, len(ids), cap)] or [[]]

    @classmethod
    def memory_bytes(cls, books: int, levels_per_book: int = 40) -> int:
        return books * (cls.BYTES_PER_BOOK + levels_per_book * cls.BYTES_PER_LEVEL)

    def note_resync(self, token_id: str) -> None:
        b = self.books.get(token_id)
        if b is not None:
            b.resyncs += 1

    def resync_due(self, now_ms: int) -> list[str]:
        return [t for t, b in self.books.items() if b.needs_resync(now_ms, self.stale_ms)]
