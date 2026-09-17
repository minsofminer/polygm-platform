"""The tape: one row per fill, from two sources that overlap and neither of which is complete.

Dedupe key, and why it is this one: `/trades` rows carry no id (verified against the live payload), and a
transaction hash is not unique — one transaction can settle several fills across several markets. So the key is
(tx_hash, asset, side, price_micro, size_micro). Two genuinely distinct fills with that exact tuple are
indistinguishable to us *and to the chain*, and collapsing them costs a row of a histogram nobody reads while
duplicating them double-counts volume, which is the number the leaderboard, the whale label and the 24 h cap
all sit on. The asymmetry decides it: prefer the merge.

`source` is kept on every row (`ws` | `rest`) because when the numbers disagree at 3am, the first question is
which pipe said it.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field


def fill_key(f: dict) -> tuple:
    return (f.get("tx_hash") or "", f.get("token_id") or "", f.get("side") or "",
            int(f.get("price_micro") or 0), int(f.get("size_micro") or 0))


# \x1f (unit separator) because a wallet address and a tx hash can both contain anything hex, and joining on a
# character that can appear in the data is how two different fills end up with one key.
_KEY_SEP = "\x1f"


def dedupe_key(f: dict) -> str:
    """The TEXT identity stored in `tape_fills.dedupe_key`, and the ONLY place it is computed.

    The in-memory tape dedupes on the tuple; the database needs a fixed-width indexed value, and UNIQUE
    (dedupe_key) is what makes a replay of the last hour harmless after a restart. Both must agree by
    construction, not by two people writing the same hash twice.
    """
    return hashlib.sha256(_KEY_SEP.join(str(x) for x in fill_key(f)).encode()).hexdigest()


@dataclass
class Tape:
    """Ring-buffer-ish view of recent fills, ordered by venue timestamp, not arrival time.

    Late arrivals are the normal case on a WebSocket that resyncs, so "the newest row is the last row we got" is
    false, and a signal engine that assumes it fires on stale data. `newest_ts_ms` is therefore a maintained
    maximum rather than `rows[-1]`, and `lag_ms` is what the freshness alarm reads.
    """
    capacity: int = 500
    rows: list = field(default_factory=list)
    seen: set = field(default_factory=set)
    dups: int = 0
    late: int = 0
    out_of_order: int = 0
    newest_ts_ms: int = 0
    live: list = field(default_factory=list)
    live_capacity: int = 200
    live_seen: int = 0
    live_suppressed: int = 0
    # Rows the venue sent that we could not read. Kept on the tape rather than in the caller because the status
    # endpoint reports it and because a rising count with a `last_refusal` sample is how a field type change
    # ("price": "abc") is told apart from a market that genuinely had nothing in it.
    refusals: int = 0
    last_refusal: str = ""
    _high_water: int = 0

    def add(self, fill: dict) -> bool:
        """True if this fill was new. False means duplicate — and the count of those is a health metric, not a
        detail: during a 5-minute WS outage followed by a REST catch-up, the overlap is the whole window."""
        k = fill_key(fill)
        if k in self.seen:
            self.dups += 1
            return False
        ts = int(fill.get("ts_ms") or 0)
        self.seen.add(k)
        self.rows.append(dict(fill, key=k))
        self.rows.sort(key=lambda r: (r["ts_ms"], r["token_id"]))
        if ts < self._high_water:
            self.late += 1
        self._high_water = max(self._high_water, ts)
        if self.rows and ts < self.rows[-1]["ts_ms"]:
            self.out_of_order += 1
        self.newest_ts_ms = max(self.newest_ts_ms, ts)
        while len(self.rows) > self.capacity:
            old = self.rows.pop(0)
            self.seen.discard(old["key"])       # bounded memory, bounded dedupe window: both must be said
        return True

    def overlaps(self, fill: dict, window_ms: int = 30_000) -> bool:
        """Is this (token, side, price, size) already in the durable record, within `window_ms`?

        Fuzzy on purpose, and this is the reason the WS never *writes* the durable tape: a `last_trade_price`
        frame carries no transaction hash (verified against the live channel), so it cannot be reconciled
        exactly with the REST row that describes the same fill, and the two clocks differ by the settlement lag.
        Dedupe across sources with a fuzzy key would occasionally merge two genuine fills (under-counting
        volume, which the leaderboard and the risk cap both read) and occasionally split one (double-counting).
        Both errors are worse than the thing we are trying to avoid, so: REST is the record, the WS is the
        latency layer, and this method exists only to tell the UI "already booked" — never to decide a number.
        """
        key = (fill.get("token_id"), fill.get("side"), int(fill.get("price_micro") or 0),
               int(fill.get("size_micro") or 0))
        ts = int(fill.get("ts_ms") or 0)
        for r in reversed(self.rows):
            if ts - int(r.get("ts_ms") or 0) > window_ms:
                break
            if (r.get("token_id"), r.get("side"), int(r.get("price_micro") or 0),
                    int(r.get("size_micro") or 0)) == key:
                return True
        return False

    def add_live(self, fill: dict) -> bool:
        """Record a WebSocket-seen fill in the volatile live view. Returns True if it was NOT already booked.

        Kept small and separate: `capacity` rows of durable truth plus a bounded live ring. A live fill that
        never gets booked (a REST page that dropped it) is a metric, not a data loss, because the durable
        tape is the only thing the numbers are computed from.
        """
        self.live_seen += 1
        booked = self.overlaps(fill)
        if booked:
            self.live_suppressed += 1
        self.live.append(dict(fill, booked=booked, seen_ms=int(time.time() * 1000)))
        while len(self.live) > self.live_capacity:
            self.live.pop(0)
        return not booked

    def lag_ms(self, now_ms: int) -> int:
        if not self.newest_ts_ms:
            return 10 ** 15                        # never "0": no data is the worst lag, not the best
        return max(0, now_ms - self.newest_ts_ms)

    def fills_above(self, notional_micro: int, since_ms: int, until_ms: int) -> list:
        """Fills at or above a notional inside a window. The chaos test uses it to prove that the rows the
        dedupe merged are the same rows the WebSocket showed, in both directions."""
        return [r for r in self.rows
                if since_ms <= r["ts_ms"] <= until_ms and r["usd_notional_micro"] >= notional_micro]

    def wallet_span(self) -> dict:
        wallets = {r.get("wallet") for r in self.rows if r.get("wallet")}
        return {"rows": len(self.rows), "wallets": len(wallets),
                "fills_per_sec": round(len(self.rows) / max(1, (self.rows[-1]["ts_ms"] - self.rows[0]["ts_ms"]) / 1000), 2)
                if len(self.rows) > 1 else 0.0}


@dataclass
class UpDownLifecycle:
    """The 5-minute and 15-minute crypto Up/Down series (`btc-updown-5m-1789645500`-shaped slugs).

    A new market every 5 minutes, an enormous fill rate, and then the market resolves and disappears from the
    active list. Without explicit handling two things go wrong: (1) the universe sync treats the disappearance
    as a deletion and drops the history that the volume chart and the leaderboard are built from, and (2) every
    label recompute walks 8,640 dead markets a day per asset. So these rows are: never pruned for at least
    `retain_ms`, bucketed by their `bucket_start_s` (which is IN the slug — no extra lookup needed, verified on
    live rows), and excluded from "ends soon" sorts where they would otherwise drown the real markets.
    """
    retain_ms: int = 48 * 3600 * 1000
    bucket_s: int = 300

    @staticmethod
    def parse(slug: str) -> dict | None:
        # `-updown-<unit>-<unix_start_s>`: the timestamp is the market's own resolution time, so the lifecycle
        # can be driven from the slug alone. Anything that does not parse is not an updown market.
        for unit, secs in (("5m", 300), ("15m", 900), ("1h", 3600), ("4h", 14400), ("daily", 86400)):
            marker = "-updown-%s-" % unit
            if marker in slug:
                tail = slug.rsplit(marker, 1)[-1]
                digits = ""
                for ch in tail:
                    if not ch.isdigit():
                        break
                    digits += ch
                if len(digits) >= 9:
                    return {"asset": slug.split(marker)[0], "unit": unit, "bucket_s": secs,
                            "bucket_start_s": int(digits)}
                return {"asset": slug.split(marker)[0], "unit": unit, "bucket_s": secs, "bucket_start_s": None}
        return None

    def is_updown(self, slug: str) -> bool:
        return self.parse(slug) is not None

    def next_bucket_start_s(self, slug: str, now_s: int) -> int | None:
        p = self.parse(slug)
        if not p or p["bucket_start_s"] is None:
            return None
        return p["bucket_start_s"] + p["bucket_s"]

    def should_prune(self, slug: str, now_ms: int) -> bool:
        p = self.parse(slug)
        if not p or p["bucket_start_s"] is None:
            return False                      # not an updown market: the ordinary dead-market rule decides
        return now_ms - p["bucket_start_s"] * 1000 > self.retain_ms
