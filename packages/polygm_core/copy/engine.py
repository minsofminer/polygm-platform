"""D5: copy trading. What is copied, what is *not* copied, and the numbers a copier must see.

The premise this module accepts: copying is a latency business. The source's fill happens, we learn about it
seconds later, and the price we would pay is not the price they paid. Every rule here follows from that:

* **Skip, never chase.** `max_entry_deviation_bps` is the copier's protection. "Buy anyway" converts the
  source's edge into the copier's cost, and on a book with $30 of visible depth the chase is the slippage.
  The default is 150 bps; a skipped copy is visible in the copier's history with the reason, because a
  silently-skipped trade teaches the user nothing about whether the strategy works for *them*.
* **No quote, no copy.** A stale or missing book is a skip, not a guess. This is the same fail-closed rule
  the risk gate has for a human order; a copier is a user.
* **Never copy into something about to resolve.** `min_seconds_to_resolution` (15 min default) exists because
  the source may have had two minutes of thought and the copier has a queue.
* **The track record shows the bad numbers.** Win rate is the number every product shows and the least useful
  one on binary outcomes; drawdown, longest losing streak and net-after-fees are what decide whether a
  copier can survive the strategy. `track_record` computes them from closed trades in integer micros and is
  the function the leaderboard in P09 will render — no separate "display" path that could round differently.
* **Copying a copier is refused at depth 2, and a cycle is refused at any depth.** A chain of copiers turns
  one source's $10 fill into $10,000 of the same position at prices that only the last link pays.

Fees are the copier's real cost and are computed here rather than in the UI, because a copier's economics are
what the leaderboard owes them: `economics()` says whether the aggregate of copying a source is net-positive
*after* the venue fee and our builder fee.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace

from ..dbtypes import TimeColumnError, seconds_until
from ..money.cents import SCALE, notional_floor
from ..venue.clob_v2 import estimate_fees

# What the API and the leaderboard may show, and what they must show alongside it. Kept as data so
# tools/p06-gate-check.py can assert the UI contract is expressed in code, not in a paragraph.
TRACK_RECORD_FIELDS: tuple[str, ...] = ("closed_trades", "win_rate_bp", "realized_pnl_micro", "fees_micro",
                                        "net_after_fees_micro", "max_drawdown_micro", "longest_losing_streak",
                                        "avg_latency_ms")
REQUIRED_DISCLOSURE = ("net_after_fees_micro", "max_drawdown_micro", "longest_losing_streak")

SKIP_REASONS = ("disabled", "paused", "source_stopped", "insider_flagged", "market_not_accepting",
                "resolving_soon", "blocked_market", "min_interval", "no_quote", "stale_quote", "price_bound",
                "deviation", "per_trade_cap", "daily_cap", "daily_count", "cycle", "depth", "zero_size",
                "duplicate")


@dataclass(frozen=True)
class SourceFill:
    """The event we react to. `at_ms` is the venue's timestamp, not ours: latency is measured from the fact,
    or the number flatters us."""
    wallet: str
    intent_id: str
    token_id: str
    market_id: str
    side: str
    price_micro: int
    size_shares_micro: int
    at_ms: int


@dataclass(frozen=True)
class Quote:
    best_bid_micro: int | None
    best_ask_micro: int | None
    age_ms: int
    stale_ms: int = 3_000

    @property
    def missing(self) -> bool:
        return self.best_bid_micro is None or self.best_ask_micro is None

    @property
    def stale(self) -> bool:
        return self.age_ms > self.stale_ms

    def entry_price_micro(self, side: str) -> int | None:
        """The price the copier would actually pay: the ask for a BUY, the bid for a SELL. Anything else is a
        number from a UI, not a price from a market."""
        if self.missing:
            return None
        return self.best_ask_micro if side == "BUY" else self.best_bid_micro


@dataclass(frozen=True)
class MarketClock:
    accepting_orders: bool
    seconds_to_resolution: int
    # The market's own price grid, in micro units (0.01 -> 10_000). Optional because the pure sizing rules
    # are testable without it, but a copy engine that omits it produces orders the venue refuses for
    # OFF_TICK: the best ask in a real book is a level, not necessarily a grid point, and a copied limit is
    # a NEW order that must be on the grid the market publishes.
    tick_micro: int | None = None


@dataclass(frozen=True)
class CopyConfig:
    """The union of P04's `copy_configs` and this phase's `copy_config_policy`."""
    id: str
    user_id: str
    source_user: str
    mode: str = "ratio"                     # mirror | ratio | cap
    ratio_bps: int = 10_000
    max_order_micro: int = 25_000_000       # $25 per copied order
    max_daily_micro: int = 250_000_000      # $250 a day
    max_copies_per_day: int = 20
    blocked_markets: tuple[str, ...] = ()
    enabled: bool = True
    max_entry_deviation_bps: int = 150
    min_interval_ms: int = 0
    take_profit_bp: int | None = None
    stop_loss_bp: int | None = None
    min_seconds_to_resolution: int = 900
    max_price_micro: int | None = None
    copy_up_to_side: bool = False
    hop_depth: int = 1
    paused_reason: str = ""

    @classmethod
    def from_rows(cls, config_row: dict) -> "CopyConfig":
        return cls(
            id=str(config_row["id"]), user_id=str(config_row["user_id"]),
            source_user=str(config_row["source_user"]), mode=str(config_row["mode"]),
            ratio_bps=int(config_row["ratio_bps"] or 10_000),
            max_order_micro=int(config_row["max_order_micro"]),
            max_daily_micro=int(config_row["max_daily_micro"]),
            max_copies_per_day=int(config_row.get("max_copies_per_day") or 20),
            blocked_markets=tuple(config_row.get("blocked_markets") or ()),
            enabled=bool(config_row["enabled"]),
            max_entry_deviation_bps=int(config_row.get("max_entry_deviation_bps") or 150),
            min_interval_ms=int(config_row.get("min_interval_ms") or 0),
            take_profit_bp=(int(config_row["take_profit_bp"]) if config_row.get("take_profit_bp") else None),
            stop_loss_bp=(int(config_row["stop_loss_bp"]) if config_row.get("stop_loss_bp") else None),
            min_seconds_to_resolution=int(config_row.get("min_seconds_to_resolution") or 900),
            max_price_micro=(int(config_row["max_price_micro"]) if config_row.get("max_price_micro") else None),
            copy_up_to_side=bool(config_row.get("copy_up_to_side")),
            hop_depth=int(config_row.get("hop_depth") or 1),
            paused_reason=str(config_row.get("paused_reason") or ""))


@dataclass
class Decision:
    action: str                              # 'copied' | 'skipped'
    reason: str = ""
    size_shares_micro: int = 0
    entry_price_micro: int = 0
    notional_micro: int = 0
    est_fee_micro: int = 0
    deviation_bps: int = 0
    latency_ms: int = 0
    idempotency_key: str = ""
    notes: tuple[str, ...] = ()

    @property
    def copied(self) -> bool:
        return self.action == "copied"

    def as_dict(self) -> dict:
        return {"action": self.action, "reason": self.reason, "size_shares_micro": self.size_shares_micro,
                "entry_price_micro": self.entry_price_micro, "notional_micro": self.notional_micro,
                "est_fee_micro": self.est_fee_micro, "deviation_bps": self.deviation_bps,
                "latency_ms": self.latency_ms, "idempotency_key": self.idempotency_key,
                "notes": list(self.notes)}


def idempotency_key_for(cfg: CopyConfig, fill: SourceFill) -> str:
    """Keyed on the source's fill, not on our clock. Two evaluations of the same source trade — the retry
    after a restart, the duplicate WS frame — must produce ONE copy, and "same trade" is a fact about the
    venue, not about us."""
    core = "%s|%s|%s|%s|%s" % (cfg.user_id, fill.wallet, fill.intent_id or fill.at_ms, fill.price_micro,
                              fill.size_shares_micro)
    return "copy:" + hashlib.sha256(core.encode()).hexdigest()[:24]


# Below one whole share a "position" cannot be closed, merged or redeemed, so a copy that sizes down to less
# than this is refused rather than placed. The floor is a floor on the *user's* actionability, not on the
# venue's minimum, which the risk gate owns.
MIN_COPIED_SHARES_MICRO = 10**6


def size_for(cfg: CopyConfig, fill: SourceFill, *, remaining_daily_micro: int,
             entry_price_micro: int | None = None) -> tuple[int, str]:
    """Shares to copy. Floors, never rounds up: an over-sized copy is a bigger position than the user
    agreed to, and "we copied 6.4 instead of 6.39 shares" is not a defence.

    A `cap` config means "same side, our own ceiling", which is the mode for a user who wants the *idea*
    without the exposure; `ratio` is proportional to the source's size; `mirror` is the same share count and
    is the mode that turns a whale into a margin call, so it is only offered when the per-trade cap binds —
    enforced here rather than in the UI, because the UI is the part a user can misconfigure.
    """
    # The share COUNT comes from the source's fill; the money is measured at the price the copier will
    # actually pay. Sizing the cap against the source's price would let a $25 ceiling buy $25.60 of stock
    # when the book moved, which is precisely the movement the deviation bound exists to notice.
    price = int(entry_price_micro or fill.price_micro)
    if cfg.mode == "mirror":
        want = fill.size_shares_micro
    elif cfg.mode == "ratio":
        want = (fill.size_shares_micro * cfg.ratio_bps) // 10_000
    elif cfg.mode == "cap":
        # `max_order_micro` is a DOLLAR cap, and `want` is SHARES. Comparing them directly (an earlier draft
        # did exactly that) makes a $10 cap look like a 10-share cap on a 50c market, i.e. half the position
        # the user asked for. Convert through the price, in integers, flooring.
        max_shares = (cfg.max_order_micro * 10**SCALE) // fill.price_micro if fill.price_micro > 0 else 0
        want = min(fill.size_shares_micro, max(0, max_shares))
    else:
        return 0, "bad mode %r" % cfg.mode
    # A cap is a ceiling on NOTIONAL, and `notional_floor` rounds up (it must: an under-stated spend is how
    # an all-in limit stops meaning anything). Scaling shares by the cap ratio therefore lands above the cap
    # on the rounded-up notional, so the ceiling is converted to a share count with a floor divide instead —
    # the one direction that cannot overshoot.
    cap = min(cfg.max_order_micro, remaining_daily_micro)
    if price > 0:
        max_want = (cap * 10**SCALE) // price
        want = min(int(want), max_want)
    want = max(int(want), 0)
    if want < MIN_COPIED_SHARES_MICRO:
        # A copy of less than one share is a signed order, a lifecycle trail, a venue round trip and a
        # position the user cannot close, merge or redeem. Say which cap bound it, because that is the number
        # the copier needs to raise (or the reason they should stop copying this source today).
        return 0, ("below one share after the %s cap" % ("daily" if cap == remaining_daily_micro
                                                         else "per-trade"))
    return want, ""


def deviation_bps(source_price_micro: int, entry_price_micro: int) -> int:
    """How far the copier's price has moved against them, in basis points of the source's price. Positive =
    worse. Signed deliberately: an *improvement* never trips the bound, and reporting it keeps the number
    honest in the history.

    Rounded half-away-from-zero rather than floor-divided. Floor division is not sign-neutral: it would call a
    -382.98 bps improvement "-383" (flattering the copy) while calling a +1300.4 bps chase "1300"
    (flattering it too). Both signs must be rounded the same way or the bound silently moves depending on
    which side of the source's price the book is on.
    """
    if source_price_micro <= 0:
        return 10_000
    num = (entry_price_micro - source_price_micro) * 10_000
    den = source_price_micro
    scaled = (abs(num) * 2 + den) // (2 * den)
    return -scaled if num < 0 else scaled


def decide(cfg: CopyConfig, fill: SourceFill, *, quote: Quote, market: MarketClock,
           day_spent_micro: int, day_copies: int, last_copy_ms: int, now_ms: int,
           fee_rate_bps: int = 0, builder_bps: int = 0,
           source_state: str = "active") -> Decision:
    """The whole decision, in one pure function, in the order the checks matter.

    Cheap state checks first (a paused config should not cost a quote read), then the things that protect
    the user's money (price bound, deviation, resolution window), then the caps — because a user who is
    capped out deserves to see "daily cap" and not a lie about price.
    """
    d = Decision("skipped")
    if not cfg.enabled:
        d.reason = "disabled"; return d
    if cfg.paused_reason:
        d.reason = "paused:%s" % cfg.paused_reason; return d
    if source_state == "stopped":
        d.reason = "source_stopped"; return d
    if source_state == "insider_flagged":
        d.reason = "insider_flagged"; return d
    if not market.accepting_orders:
        d.reason = "market_not_accepting"; return d
    # A missing clock is the most dangerous value of all, because `None < 900` is a TypeError and a crash in a
    # copy worker stops every follower, not just this one order. Coerce to "unknown" and refuse.
    secs = market.seconds_to_resolution if isinstance(market.seconds_to_resolution, int) else -1
    if secs < cfg.min_seconds_to_resolution:
        d.reason = "resolving_soon:%ds" % secs; return d
    if fill.market_id in cfg.blocked_markets:
        d.reason = "blocked_market"; return d
    if cfg.min_interval_ms and now_ms - last_copy_ms < cfg.min_interval_ms:
        d.reason = "min_interval"; return d
    if quote.missing:
        d.reason = "no_quote"; return d
    if quote.stale:
        d.reason = "stale_quote:%dms" % quote.age_ms; return d

    entry = quote.entry_price_micro(fill.side)
    if entry is None or entry <= 0:
        d.reason = "no_quote"; return d
    notes: list[str] = []
    if market.tick_micro:
        tick = int(market.tick_micro)
        if tick > 0 and entry % tick:
            # Align AGAINST crossing, never with it: a BUY is floored to the grid (we never offer more than
            # the book asked) and a SELL is ceiled (we never accept less). If that leaves the order unfilled,
            # the honest outcome is a skip on the next quote, not a better price for us out of the user's
            # fill probability. A copied order that pays 0.5 cent more than it needed to, every time, is a
            # fee the source's track record does not contain.
            aligned = (entry // tick) * tick if fill.side == "BUY" else ((entry + tick - 1) // tick) * tick
            if aligned <= 0 or aligned >= 10**SCALE:
                d.reason = "bad_entry_price:%d" % aligned
                return d
            notes.append("tick-aligned %d->%d (tick %d)" % (entry, aligned, tick))
            entry = aligned
    if cfg.max_price_micro and fill.side == "BUY" and entry > cfg.max_price_micro:
        d.reason = "price_bound:%s>%s" % (entry, cfg.max_price_micro); return d
    if cfg.copy_up_to_side and fill.side == "BUY" and entry > (cfg.max_price_micro or 10**SCALE):
        d.reason = "price_bound"; return d

    dev = deviation_bps(fill.price_micro, entry)
    d.deviation_bps = dev
    if dev > cfg.max_entry_deviation_bps:
        # The skip-not-chase rule. Note what is NOT here: no "buy a bit less", no "retry when it pulls back".
        # Both of those make the copier's entry price depend on our retry policy, which is how a copy product
        # starts losing money in ways the source's record does not explain.
        d.reason = "deviation:%dbps>%dbps" % (dev, cfg.max_entry_deviation_bps)
        return d

    remaining = cfg.max_daily_micro - day_spent_micro
    if remaining <= 0:
        d.reason = "daily_cap"; return d
    if day_copies >= cfg.max_copies_per_day:
        d.reason = "daily_count"; return d
    size, why = size_for(cfg, fill, remaining_daily_micro=remaining, entry_price_micro=entry)
    if size <= 0:
        d.reason = "per_trade_cap" if not why else "zero_size:%s" % why
        return d
    fees = estimate_fees(size_shares_micro=size, price_micro=entry, fee_rate_bps=fee_rate_bps,
                         builder_bps=builder_bps)
    d.action = "copied"
    d.notes = tuple(notes)
    d.size_shares_micro = size
    d.entry_price_micro = entry
    d.notional_micro = notional_floor(size, entry)
    d.est_fee_micro = fees.total_micro
    d.latency_ms = max(now_ms - fill.at_ms, 0)
    d.idempotency_key = idempotency_key_for(cfg, fill)
    d.reason = ""
    return d


# ---------------------------------------------------------------------------- track record (the part) ==
@dataclass(frozen=True)
class ClosedTrade:
    realized_micro: int                       # signed: net cash from close less cost basis
    fee_micro: int = 0                        # the venue fee AND our builder fee, both, or the record lies
    source_fill_ms: int = 0
    our_fill_ms: int = 0


def track_record(trades: list[ClosedTrade], *, window_days: int = 30, source_user_id: str = "",
                 latencies_ms: list[int] | None = None) -> dict:
    """Win rate, drawdown, losing streaks, and net after fees — the four numbers that decide whether a
    strategy is survivable, computed on integer micros so the leaderboard and the ledger cannot disagree.

    `max_drawdown_micro` is the largest peak-to-trough on the *cumulative* curve of realised results. It is
    the number a user most needs and least wants: a strategy that wins 60% of the time and has a 4x drawdown
    relative to its monthly profit will end a copier's participation, not their winning streak.
    """
    if not trades:
        return {"source_user_id": source_user_id, "window_days": window_days, "closed_trades": 0,
                "win_rate_bp": 0, "realized_pnl_micro": 0, "fees_micro": 0, "net_after_fees_micro": 0,
                "max_drawdown_micro": 0, "longest_losing_streak": 0, "avg_latency_ms": 0,
                "sample": "no closed trades; a track record of zero is not a track record of 100% wins"}
    wins = sum(1 for t in trades if t.realized_micro - t.fee_micro > 0)
    realized = sum(t.realized_micro for t in trades)
    fees = sum(t.fee_micro for t in trades)
    net = realized - fees
    equity = 0
    peak = 0
    drawdown = 0
    streak = 0
    worst_streak = 0
    for t in trades:
        equity += t.realized_micro - t.fee_micro
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        if t.realized_micro - t.fee_micro < 0:
            streak += 1
            worst_streak = max(worst_streak, streak)
        else:
            streak = 0
    lats = list(latencies_ms or [])
    lats += [max(t.our_fill_ms - t.source_fill_ms, 0) for t in trades if t.our_fill_ms and t.source_fill_ms]
    return {"source_user_id": source_user_id, "window_days": window_days, "closed_trades": len(trades),
            "win_rate_bp": (wins * 10_000) // len(trades), "realized_pnl_micro": realized,
            "fees_micro": fees, "net_after_fees_micro": net, "max_drawdown_micro": drawdown,
            "longest_losing_streak": worst_streak,
            "avg_latency_ms": (sum(lats) // len(lats)) if lats else 0,
            # The ratio a copier should actually read: can the drawdown be survived at their size?
            "drawdown_over_profit": (drawdown // net) if net > 0 else None,
            "sample": "n=%d over %dd" % (len(trades), window_days)}


# ============================================================================ per-source economics =====
def economics(source_user_id: str, stats: dict, *, copier_count: int, copier_volume_micro: int,
               builder_bps: int, fee_rate_bps: int = 700, payout_bps: int = 0) -> dict:
    """What copying this source costs the copiers, in total, and what it pays them.

    The comparison that matters is `copier_net_micro` versus the fees they paid — a source with a strong
    record and 400 copiers can be a net-negative product for those 400 people, and the leaderboard has to be
    able to say so. `payout_bps` is what we would owe the source if we ever share revenue, kept separate so
    the arithmetic never pretends a payout is a cost the copiers avoid.
    """
    per_copier = (copier_volume_micro // copier_count) if copier_count else 0
    # Worst-case venue fee: the taker rate, with no maker rebate credit. A rebate is a maybe; a fee paid is a
    # number, and the whole point of this table is that it never flatters the strategy.
    fees = (copier_volume_micro * fee_rate_bps) // 10_000
    builder = (copier_volume_micro * builder_bps) // 10_000
    net_after = int(stats.get("net_after_fees_micro", 0))
    return {"source_user_id": source_user_id, "copier_count": copier_count,
            "copier_volume_micro": copier_volume_micro, "fees_micro": fees, "builder_fees_micro": builder,
            "copier_net_micro": net_after, "source_payout_micro": (copier_volume_micro * payout_bps) // 10_000,
            "median_volume_per_copier_micro": per_copier,
            "verdict": ("positive" if net_after - fees - builder > 0 else
                        "negative: the copiers pay more than they earn"),
            "shown_to_users": True}


# ==================================================================================== chain detection ==
def walk_chain(configs: list[dict], copier_user: str, source_user: str, *, max_hops: int = 1) -> dict:
    """Is the copier copying a copier? Detected from the config graph, before the config is saved.

    Refused at depth > 1 by default. The reason is not tidiness: in a chain, the last link pays the price the
    second-to-last link moved, so a two-hop chain of identical strategies is a *worse* strategy by
    construction, and nobody in the chain can see why. A cycle is refused at any depth because it is an
    order generator with no input.
    """
    by_user = {c["user_id"]: c for c in configs if c.get("enabled")}
    hops = 1
    seen = {copier_user, source_user}
    cur = source_user
    while True:
        nxt = by_user.get(cur)
        if nxt is None:
            return {"hop_depth": hops, "cycle": False, "allowed": True, "chain": [copier_user, source_user]}
        nxt_src = str(nxt["source_user"])
        if nxt_src in seen:
            return {"hop_depth": hops + 1, "cycle": True, "allowed": False,
                    "chain": [copier_user] + [nxt_src],
                    "code": "COPY_CYCLE",
                    "message": "that source copies someone who copies you; the loop would generate orders "
                               "with no market input"}
        seen.add(nxt_src)
        hops += 1
        if hops > max_hops:
            return {"hop_depth": hops, "cycle": False, "allowed": False, "code": "COPY_DEPTH",
                    "chain": sorted(seen),
                    "message": "copying a copier of a copier is not allowed: each hop pays the previous "
                               "hop's slippage"}
        cur = nxt_src


# ========================================================================================== the engine ==
@dataclass
class EngineStats:
    evaluated: int = 0
    copied: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    paused: int = 0
    intents: list[str] = field(default_factory=list)

    def skip(self, reason: str) -> None:
        key = reason.split(":")[0]
        assert key in SKIP_REASONS, "an undocumented skip reason: %r" % reason
        self.skipped[key] = self.skipped.get(key, 0) + 1


class CopyEngine:
    """Reacts to source fills, writes intents, and keeps the record. It does not talk to the venue: every
    copy goes through the queue and therefore through the same pre-flight a human order passes (D5.5's "same
    risk gate, no exceptions" answer)."""

    def __init__(self, store, *, builder_bps: int = 100, fee_rate_bps: int = 0, clock=None) -> None:
        self.store = store
        self.builder_bps = builder_bps
        self.fee_rate_bps = fee_rate_bps
        self.stats = EngineStats()

    def config_for(self, config_id: str) -> CopyConfig | None:
        row = self.store.copy_config(config_id)
        return CopyConfig.from_rows(row) if row else None

    def on_source_fill(self, fill: SourceFill, *, at: int) -> list[dict]:
        """One source fill, every copier. Returns per-copier outcomes, and writes the same rows a user can
        read in Activity — a skipped copy is a visible skip, never an absence."""
        out: list[dict] = []
        # Read once per fill, not per copier: the switch is a fact about the plane, and re-reading it for the
        # 40th copier is a query that cannot change the answer. `force=True` skips the 250 ms cache, because a
        # copier that acts on a 250 ms-old "not engaged" is 250 ms of orders the operator already stopped.
        halted = bool(self.store.kill_switch_engaged(at_ms=at, force=True))
        for row in self.store.copy_configs_for_source(fill.wallet):
            cfg = CopyConfig.from_rows(row)
            self.stats.evaluated += 1
            q = self.store.market_quote(fill.market_id, at=at)
            quote = Quote(best_bid_micro=q["best_bid_micro"], best_ask_micro=q["best_ask_micro"],
                          age_ms=q["age_ms"])
            mrow = self.store.conn.execute("SELECT accepting_orders FROM markets WHERE id=?",
                                           (fill.market_id,)).fetchone()
            market = MarketClock(accepting_orders=bool(mrow and mrow[0]),
                                 seconds_to_resolution=self._seconds_to_resolution(fill.market_id, at=at),
                                 tick_micro=self._tick_micro(fill.market_id))
            day = self._day_counts(cfg, at=at)
            last = self._last_copy_ms(cfg, at=at)
            d = decide(cfg, fill, quote=quote, market=market, day_spent_micro=day["spent_micro"],
                       day_copies=day["count"], last_copy_ms=last, now_ms=at,
                       fee_rate_bps=self.fee_rate_bps, builder_bps=self.builder_bps)
            if halted and d.copied:
                # The executor would reject the intent anyway, and that is the argument AGAINST queueing it:
                # the copier's Activity line would say "copied" for two seconds before saying "rejected", and
                # "trading is disabled" is the truth at the moment the decision is made, not three rows later.
                # `disabled:` is the registered skip vocabulary (EngineStats.skip asserts on the key before the
                # colon); the detail after it is the sentence the copier reads in Activity.
                d = replace(d, action="skipped", reason="disabled:trading is halted by the kill switch; the "
                                                        "source fill was not copied")
            if not d.copied:
                self.stats.skip(d.reason)
                self.store.record_copy_event(copier_id=cfg.id, source_user_id=fill.wallet, action="skipped",
                                            reason=d.reason, at=at, deviation_bps=d.deviation_bps,
                                            source_intent_id=fill.intent_id)
                out.append({"config_id": cfg.id, **d.as_dict()})
                continue
            r = self.store.enqueue_intent(user_id=cfg.user_id, market_id=fill.market_id,
                                         token_id=fill.token_id, side=fill.side,
                                         price_micro=d.entry_price_micro, size_micro=d.size_shares_micro,
                                         idempotency_key=d.idempotency_key, order_type="FAK",
                                         audience="copy", config_id=cfg.id, builder_bps=self.builder_bps,
                                         fee_rate_bps=self.fee_rate_bps, at=at)
            if not r["created"]:
                d = replace(d, action="skipped", reason="duplicate:replayed fill")
                self.stats.skip("duplicate")
            else:
                self.stats.copied += 1
                self.stats.intents.append(r["intent_id"])
            self.store.record_copy_event(copier_id=cfg.id, source_user_id=fill.wallet,
                                        action="copied" if r["created"] else "skipped",
                                        reason=d.reason, at=at, deviation_bps=d.deviation_bps,
                                        source_intent_id=fill.intent_id, intent_id=r["intent_id"])
            self.manage_tp_sl(cfg, fill, at=at, entry_price_micro=d.entry_price_micro)
            out.append({"config_id": cfg.id, "intent_id": r["intent_id"], **d.as_dict()})
        return out

    def manage_tp_sl(self, cfg: CopyConfig, fill: SourceFill, *, at: int,
                     entry_price_micro: int | None = None) -> dict | None:
        """Take-profit / stop-loss on the copied position. Both are *exits*, and an exit is allowed to chase
        (leaving is not a slippage risk to the user in the way an entry is), so the deviation bound does not
        apply here — the bound that applies is the market's own price grid, handled by the gate.
        """
        if not (cfg.take_profit_bp or cfg.stop_loss_bp):
            return None
        q = self.store.market_quote(fill.market_id, at=at)
        # The copier's take-profit is a percentage of what THE COPIER paid, not of what the source paid. If
        # the book moved 3% between their fill and ours, a target measured on their price is a target the
        # copier reaches for the wrong reason (and a stop that fires too early).
        entry = int(entry_price_micro or fill.price_micro)
        if entry <= 0:
            return None
        tp = (entry * (10_000 + (cfg.take_profit_bp or 0))) // 10_000 if cfg.take_profit_bp else None
        sl = (entry * (10_000 - (cfg.stop_loss_bp or 0))) // 10_000 if cfg.stop_loss_bp else None
        bid = q["best_bid_micro"]
        if bid is None:
            return None
        if tp and bid >= tp:
            key = "copy-tp:%s:%s" % (cfg.id, fill.intent_id)
            r = self.store.enqueue_intent(user_id=cfg.user_id, market_id=fill.market_id, token_id=fill.token_id,
                                         side="SELL", price_micro=min(tp, 10**SCALE - 1),
                                         size_micro=fill.size_shares_micro, idempotency_key=key,
                                         order_type="FAK", audience="copy", config_id=cfg.id,
                                         builder_bps=self.builder_bps, fee_rate_bps=self.fee_rate_bps, at=at)
            self.store.record_copy_event(copier_id=cfg.id, source_user_id=fill.wallet, action="tp_filled",
                                        reason="bid reached the take-profit at %d" % tp, at=at,
                                        source_intent_id=fill.intent_id, intent_id=r["intent_id"])
            return {"action": "tp", "intent_id": r["intent_id"]}
        if sl and bid <= sl:
            key = "copy-sl:%s:%s" % (cfg.id, fill.intent_id)
            r = self.store.enqueue_intent(user_id=cfg.user_id, market_id=fill.market_id, token_id=fill.token_id,
                                         side="SELL", price_micro=max(sl, 1), size_micro=fill.size_shares_micro,
                                         idempotency_key=key, order_type="FAK", audience="copy",
                                         config_id=cfg.id, builder_bps=self.builder_bps,
                                         fee_rate_bps=self.fee_rate_bps, at=at)
            self.store.record_copy_event(copier_id=cfg.id, source_user_id=fill.wallet, action="sl_filled",
                                        reason="bid fell to the stop-loss at %d" % sl, at=at,
                                        source_intent_id=fill.intent_id, intent_id=r["intent_id"])
            return {"action": "sl", "intent_id": r["intent_id"]}
        return None

    def on_source_stopped(self, source_user: str, *, at: int, reason: str = "source stopped copying") -> dict:
        """The source opted out (or was muted). Every copier pauses and is told, because a copier watching a
        portfolio move on its own is worse than a copier told 'this source is no longer followed'."""
        n = 0
        for row in self.store.copy_configs_for_source(source_user):
            self.store.save_copy_config_policy(config_id=row["id"], at=at, paused_reason=reason[:200])
            self.store.record_copy_event(copier_id=row["id"], source_user_id=source_user,
                                        action="source_stopped", reason=reason[:200], at=at)
            n += 1
        self.stats.paused += n
        return {"paused_copiers": n, "notified": n}

    def on_label(self, wallet: str, label: str, *, at: int, publishable: bool = True) -> dict:
        """P05's labels feed D5. `insider_suspect` is not publishable, and here that distinction becomes a
        behaviour: a suspect source's copiers pause, and the label is never shown to anyone as a reason."""
        if label != "insider_suspect":
            return {"paused_copiers": 0, "ignored_reason": "no copy action for label %r" % label}
        n = 0
        for row in self.store.copy_configs_for_source(wallet):
            self.store.save_copy_config_policy(config_id=row["id"], at=at, paused_reason="insider_flagged")
            self.store.record_copy_event(copier_id=row["id"], source_user_id=wallet, action="insider_flagged",
                                        reason="source flagged by the label engine; copies paused", at=at)
            n += 1
        return {"paused_copiers": n, "publishable": False,
                "note": "the user sees 'paused: under review', never the accusation"}

    def recompute_stats(self, source_user: str, *, at: int, window_days: int = 30,
                        trades: list[ClosedTrade] | None = None) -> dict:
        stats = track_record(trades or [], window_days=window_days, source_user_id=source_user)
        self.store.save_source_stats(stats, at=at)
        return stats

    def _day_counts(self, cfg: CopyConfig, at: int) -> dict:
        rows = self.store.conn.execute("SELECT o.notional_micro FROM copy_events e JOIN order_intents o "
                                       "ON o.id = e.intent_id WHERE e.copier_id=? AND e.action='copied' "
                                       "AND e.at_ms > ?", (cfg.id, at - 86_400_000)).fetchall()
        return {"spent_micro": sum(int(r[0] or 0) for r in rows), "count": len(rows)}

    def _last_copy_ms(self, cfg: CopyConfig, at: int) -> int:
        r = self.store.conn.execute("SELECT MAX(at_ms) FROM copy_events WHERE copier_id=? AND "
                                    "action='copied'", (cfg.id,)).fetchone()
        return int(r[0] or 0)

    def _tick_micro(self, market_id: str) -> int | None:
        """The market's tick, in micro units. `markets.minimum_tick_size` is a float (0.01), and a float read
        straight into an integer grid is how 0.07000000001 becomes an off-by-one tick; the same norm the risk
        gate uses is applied here so both agree on which prices are legal."""
        from decimal import Decimal
        r = self.store.conn.execute("SELECT minimum_tick_size FROM markets WHERE id=?", (market_id,)).fetchone()
        if not r or r[0] is None:
            return None
        try:
            return int((Decimal(str(r[0])) * 10**SCALE).to_integral_value())
        except Exception:                                              # noqa: BLE001 - unknown tick = no alignment
            return None

    def _seconds_to_resolution(self, market_id: str, at: int) -> int:
        """Seconds until the market resolves, or -1 for "we cannot see the clock".

        -1 is not 0 and not infinity: `decide` refuses any resolution window shorter than the config's floor,
        and an unknown clock is worse than a short one, so it refuses too. No end time is not an infinite
        amount of time to trade in.
        """
        r = self.store.conn.execute("SELECT end_ts FROM markets WHERE id=?", (market_id,)).fetchone()
        if not r or r[0] is None:
            return -1
        try:
            secs = seconds_until(r[0], now_ms=at, column="markets.end_ts")
        except TimeColumnError:
            return -1
        return -1 if secs is None else secs
