"""The market universe: backfill, new markets, resolutions, metadata edits, and the long tail we stop watching.

Design constraints the venue imposes (all measured, see docs/P01-product-spec.md and `make probe-fresh`):
Gamma returns at most 100 rows per page no matter what `limit` says, and it ignores `tag_slug`, so any
"markets by tag" idea is a client-side filter over a page, not a query. 300 requests per 10 s means the whole
universe fits in ~10 s of budget — which is why the backfill is allowed to be *slow on purpose* (see
`Backfill.page_delay_s`): spending the entire cap on a backfill starves the live pollers, and the live pollers
are the product.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

# Half the venue's smallest tick. Not a whole tick: with `1_000` here, a market quoted at 0.999 — which is
# what a nearly-resolved market looks like for hours while it is still trading — classifies as "resolved",
# which would suppress the very moves a user most needs to see. Exactly 0 or exactly 1 (or within half a tick,
# which only happens on the 0.01 grid) is a settlement.
PRICE_EPSILON = 500


@dataclass
class Backfill:
    """Keyset pagination over Gamma `id`, resumable via a durable cursor, with the budget respected by the
    caller's token bucket (this class does not fetch; it decides what to fetch next and what a crash means)."""
    page_size: int = 100
    page_delay_s: float = 0.05          # 20 pages/s against a 30/10s share of the 300/10s cap
    cursor_id: int = 0
    done: bool = False
    pages: int = 0
    rows: int = 0

    def params(self) -> dict:
        # `id` ascending + a `gt` filter is the only stable ordering Gamma offers; `offset` is documented and
        # shifts under live inserts, which is how a backfill silently skips markets that moved down a page.
        return {"limit": self.page_size, "order": "id", "ascending": "true", "id__gt": self.cursor_id,
                "active": "true", "closed": "false"}

    def feed(self, rows: list[dict]) -> list[dict]:
        """Return the rows to keep and advance the cursor. Empty list ends the run (and `done` stays False if
        the last page was full, because a full page means there is more)."""
        self.pages += 1
        self.rows += len(rows)
        out = []
        for r in rows:
            try:
                rid = int(r.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if rid <= self.cursor_id:
                continue                       # a row we have already stored: re-inserting is not idempotent
                                                 # for the meta-history table, so drop it here
            self.cursor_id = max(self.cursor_id, rid)
            out.append(r)
        if len(rows) < self.page_size:
            self.done = True
        return out

    def snapshot(self) -> dict:
        return {"cursor_id": self.cursor_id, "done": self.done, "pages": self.pages, "rows": self.rows}


@dataclass
class Discover:
    """How a new market is learned about without re-reading 30k rows.

    The `new_market` event on the user channel is NOT available to us: it is a user-scoped channel that
    requires an authenticated subscription, and unauthenticated clients get nothing. So the discovery path is
    a cheap poll on `createdAt` descending, and the WS is only a *speed-up* for markets we already track. The
    interval below is what 1 request per poll costs against a 20/s share: 5 s is 1 % of the budget and bounds
    a new market's blindness to one page of latency, which matters because a new market is where the edge is.
    """
    interval_s: float = 5.0
    page_size: int = 100
    last_seen_id: int = 0
    seen_new: int = 0
    overlap: int = 0

    def params(self) -> dict:
        return {"limit": self.page_size, "order": "id", "ascending": "false",
                "active": "true", "closed": "false"}

    def feed(self, rows: list[dict]) -> list[dict]:
        fresh = []
        for r in rows:
            try:
                rid = int(r.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if rid > self.last_seen_id:
                fresh.append(r)
            else:
                self.overlap += 1               # the count is the proof that paging overlaps; without it a
        if rows:                                # gap between polls is invisible
            self.last_seen_id = max(self.last_seen_id, max(int(r.get("id") or 0) for r in rows if r.get("id")))
            self.seen_new += len(fresh)
        return fresh


@dataclass
class Resolution:
    """Detecting a resolution, and NOT calling it a price move.

    The trap the prompt names: 0.999 -> 1.000 is a tick, not a signal, and 0.004 -> 0.0 is a settlement, not a
    crash. Both would fire "rapid price move" on a naive threshold. So the rule is expressed as: a price that
    lands within `PRICE_EPSILON` of 0 or 1 AND whose market row says closed/accepting_orders=false is a
    RESOLUTION event; anything else that moves is a move. The classification happens here, once, so no signal
    rule has to remember it.
    """

    @staticmethod
    def classify(old_micro: int, new_micro: int, *, closed: bool, accepting: bool) -> str:
        edge = new_micro <= PRICE_EPSILON or new_micro >= 10 ** 6 - PRICE_EPSILON
        if edge and closed and not accepting:
            return "resolution"
        if edge and not closed:
            return "resolved-looking"          # a price at the boundary on an OPEN market is a real quote;
        return "move" if _moved(old_micro, new_micro) else "flat"   # it must still move the tape

    @staticmethod
    def suppresses_signal(kind: str) -> bool:
        return kind in ("resolution", "flat")


def _moved(a: int, b: int) -> bool:
    return abs(a - b) > 0


@dataclass
class MetaVersion:
    """Versioning the two fields that actually change under us.

    A question or an end date that moves silently invalidates every alert and every PnL curve anchored to it,
    and Gamma's `updatedAt` tells you SOMETHING changed but not what. So we diff the fields we serve and write
    a history row with the old and new values; the alert email can then say "this market's end date moved"
    instead of gaslighting the user about a date they never saw.
    """
    tracked: tuple = ("question", "endDate", "acceptingOrders", "closed", "negRisk", "orderPriceMinTickSize",
                      "orderMinSize", "enableOrderBook", "secondsDelay", "feeType")
    changes: list = field(default_factory=list)

    def diff(self, old: dict, new: dict) -> list[dict]:
        out = []
        for f in self.tracked:
            a, b = _norm(old.get(f)), _norm(new.get(f))
            if f in old and a != b:
                out.append({"field": f, "old": a, "new": b})
        self.changes.extend(out)
        return out


def _norm(v) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True)
    return "" if v is None else str(v)


@dataclass
class Prune:
    """What we stop tracking, and how it comes back.

    The median event does ~$19,910/day and the tail is flat zero, so the tracked set is chosen by *activity*,
    not by existence. A market is dead when it has had no fill and no book change for `idle_s` AND its 24 h
    volume is under `floor_usd`; it is woken by either one reappearing. Dead markets are never deleted — the
    tape, the leaderboard and a user's PnL curve are all built on rows that must keep existing. `prune` means
    "no WS subscription, poll every hour", which is where the savings actually are.
    """
    idle_s: float = 6 * 3600
    floor_usd_micro: int = 50 * 10 ** 6         # $50 of 24h volume: below this, nothing a user can trade
    wake_volume_usd_micro: int = 500 * 10 ** 6  # and waking needs 10x that, so a single $60 fill cannot
    max_tracked: int = 2_000                    # the flapping guard: the set is capped, not merely filtered
    now_ms: int = 0

    def is_dead(self, row: dict, now_ms: int | None = None) -> bool:
        now_ms = now_ms or self.now_ms
        last = max(int(row.get("last_fill_ms") or 0), int(row.get("last_book_change_ms") or 0),
                   int(row.get("last_fill_ms") or 0))
        quiet = now_ms - last > self.idle_s * 1000 if last else True
        return bool(quiet and int(row.get("volume_24h_micro") or 0) < self.floor_usd_micro
                    and not row.get("has_open_alert") and not row.get("in_watchlist")
                    and not row.get("has_open_order"))

    def should_wake(self, row: dict) -> bool:
        return bool(int(row.get("volume_24h_micro") or 0) >= self.wake_volume_usd_micro
                    or row.get("last_fill_ms") and (self.now_ms - int(row["last_fill_ms"])) < 60_000)

    def rank(self, rows: list[dict]) -> list[dict]:
        """Cap the live set by activity. Sorting by volume alone would park a market that just started moving
        at rank 3,001, which is the exact moment a user is watching it, so recency is the primary key and
        volume the tiebreak."""
        return sorted(rows, key=lambda r: (-int(r.get("last_fill_ms") or 0),
                                           -int(r.get("volume_24h_micro") or 0)))[:self.max_tracked]
