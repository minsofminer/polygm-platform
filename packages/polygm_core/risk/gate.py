"""The synchronous risk gate. Rule 3 of P04: *every* order path goes through this — including admin
tooling, including tests that hit production. There is deliberately no flag, role, or code path that
skips it; the only way past a decision here is to change a limit, which is an audited config write (D7).

Pure: `evaluate(snapshot, intent)` touches no clock, no DB, no network. The caller assembles the
snapshot; that keeps the gate measurable at the <50ms budget (D5.3) and makes it testable without a
database, which is what lets P13 assert the money path as a matrix rather than an integration story.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal

from ..money.cents import SCALE, MoneyError, notional_floor, price_ticks


class Deny(Exception):
    """A denial, with the machine code the client sees and the human message. Messages never contain
    balances, limits' absolute values, or another user's data — see D4's error envelope rule."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.retryable = code, message, retryable


@dataclass(frozen=True)
class Limits:
    """Every value here is a config key, never a literal in service code (D7)."""
    min_order_size_shares_micro: int = 5 * 10**SCALE          # minimum_order_size = 5 (measured, P01)
    max_order_notional_micro: int = 2_500 * 10**SCALE
    #: A *reduce-only* sell gets its own ceiling instead of the entry cap. The entry cap bounds new exposure;
    #: refusing a close does not reduce risk, it *is* risk — a stop-loss that cannot fire because the position
    #: it protects is worth more than the cap is the failure mode this exists to prevent. It is not an
    #: exemption: the order is still capped (at one position's worth), the size must not exceed what is
    #: actually held, and the holding is read from the lots by the caller rather than asserted by the client.
    max_close_notional_micro: int = 25_000 * 10**SCALE
    max_open_orders_per_user: int = 24
    max_24h_notional_micro: int = 25_000 * 10**SCALE
    max_book_depth_bars: int = 400                             # design-system ladder cap
    max_price_band_from_mid: int = 200_000                     # 0.20 USDC; catches decimal typos
    max_snap_age_ms: int = 5_000                               # stale book => fail closed (design D5)
    max_tick_sizes: tuple[str, ...] = ("0.001", "0.01")        # observed values, P01


@dataclass(frozen=True)
class MarketState:
    accepting_orders: bool
    seconds_delay: int
    enable_order_book: bool
    minimum_tick_size: str
    minimum_order_size: str
    fee_type: str
    best_bid_micro: int | None
    best_ask_micro: int | None
    snap_age_ms: int


@dataclass(frozen=True)
class Intent:
    user_id: str
    token_id: str
    side: str                     # "BUY" | "SELL" (uppercase; the venue is case-sensitive)
    price_micro: int
    size_shares_micro: int
    idempotency_key: str
    market_id: str = ""


@dataclass
class Decision:
    allowed: bool
    code: str
    message: str
    notional_micro: int = 0
    latency_ms: float = 0.0
    checks_run: tuple[str, ...] = field(default_factory=tuple)
    #: True when the order was evaluated as a close (a sell for no more than the position held). It is on the
    #: Decision because "which ceiling applied" is a question an operator asks after an incident, and a boolean
    #: buried in the branch that chose it cannot answer it from a log line.
    reduce_only: bool = False


def norm_tick(value) -> str:
    """Canonical text for a market's tick size, whatever the driver handed back.

    This is NOT defensive programming for its own sake: Postgres returns NUMERIC(6,4) as the STRING "0.0100"
    and SQLite returns the same column as the FLOAT 0.01. A gate that compares `minimum_tick_size in
    ("0.001", "0.01")` therefore fails on both engines while passing against hand-written test fixtures -
    which is exactly what happened here. Normalising at the boundary keeps the comparison a string
    comparison and makes the two engines agree.
    """
    if isinstance(value, str):
        v = Decimal(value)
    elif value is None:
        raise ValueError("tick size missing")
    else:
        v = Decimal(str(value))                    # str() first: Decimal(0.01) is binary-exact garbage
    if v <= 0 or v >= 1:
        raise ValueError("tick size out of range")
    t = format(v.normalize(), "f")
    return t if "." in t else t + ".0"


def _aligned(price_micro: int, tick: str) -> bool:
    """Tick alignment, in integers. The venue rejects off-tick orders, and a rejected order after a
    signed one is worse than never sending it (the executor would then have to reason about an order it
    cannot prove does not exist)."""
    tick_micro = price_ticks(Decimal(tick)) if Decimal(tick) < 1 else int(Decimal(tick).scaleb(SCALE))
    return price_micro % tick_micro == 0


def evaluate(intent: Intent, m: MarketState, *, limits: Limits,
             open_orders: int, spent_24h_micro: int, kill_switch: bool,
             now_ms: int | None = None, position_shares_micro: int = 0) -> Decision:
    """Order matters: cheapest and most-certain checks first, so a rejected order costs microseconds and
    a rejected order never tells the user something is stale when it is actually disallowed."""
    t0 = time.perf_counter()
    run: list[str] = []

    # A close is a sell for no more than what is held, and `position_shares_micro` is read from the lots by the
    # caller (the API reads `position_lots`, the executor its own copy) — never from the request body, because a
    # client-supplied "I hold this much" is a cap bypass with extra steps. Selling *more* than is held opens a
    # short, which creates exposure, so it is not a close and keeps the entry cap.
    #
    # It is computed here, before the first check, so that a *refusal* can say whether it refused a close: that
    # is the question an operator asks after an incident ("which ceiling stopped it, and was it an exit?"), and
    # a boolean that only exists on the success path cannot answer it.
    held = max(0, int(position_shares_micro or 0))
    reduce_only = str(intent.side).upper() == "SELL" and 0 < int(intent.size_shares_micro) <= held

    def deny(code: str, msg: str, *, retryable: bool = False) -> Decision:
        return Decision(False, code, msg, latency_ms=(time.perf_counter() - t0) * 1000,
                        checks_run=tuple(run), reduce_only=reduce_only)

    run.append("kill_switch")
    if kill_switch:
        # Fail closed and do NOT explain: a kill switch is usually pulled during an incident, and the
        # last thing that needs to happen is a queue of retries against a system already in trouble.
        return deny("RISK_HALT", "trading is temporarily disabled", retryable=True)

    run.append("market_state")
    if not m.accepting_orders:
        return deny("MARKET_NOT_ACCEPTING", "this market is not accepting orders right now")
    if not m.enable_order_book:
        return deny("NO_ORDER_BOOK", "this market has no order book; nothing can be priced against it")

    run.append("freshness")
    if m.snap_age_ms > limits.max_snap_age_ms:
        # Design system D5: trading is disabled while the book is stale. A UI that allows submit there is
        # a bug report, not a preference.
        return deny("STALE_QUOTE", "prices are stale; re-fetch before ordering", retryable=True)

    run.append("side")
    if intent.side not in ("BUY", "SELL"):
        return deny("BAD_SIDE", "side must be BUY or SELL")

    run.append("tick_alignment")
    try:
        tick = norm_tick(m.minimum_tick_size)
    except Exception:
        return deny("BAD_MARKET_META", "could not read this market's tick size", retryable=True)
    if tick not in limits.max_tick_sizes:
        return deny("UNKNOWN_TICK", "market reports an unexpected tick size; refusing to guess", retryable=True)
    if not _aligned(intent.price_micro, tick):
        return deny("OFF_TICK", f"price must be a multiple of {tick}")

    run.append("min_size")
    if intent.size_shares_micro <= 0:
        # Not covered by the market's minimum: a venue that reports minimum_order_size 0 (the field is
        # per-market and we have seen it defaulted) would otherwise let a 0-share order be SIGNED, and a
        # signed order the venue rejects after the fact is worse than never sending it.
        return deny("ZERO_SIZE", "size must be greater than zero")
    try:
        min_shares = int(Decimal(m.minimum_order_size).scaleb(SCALE))
    except Exception:
        return deny("BAD_MARKET_META", "could not read this market's size limits", retryable=True)
    if intent.size_shares_micro < min_shares:
        return deny("BELOW_MIN_SIZE", f"minimum order is {m.minimum_order_size} shares")

    run.append("notional")
    try:
        notional = notional_floor(intent.size_shares_micro, intent.price_micro)
    except MoneyError as e:
        return deny("BAD_AMOUNT", "amount is outside the supported scale")
    cap = limits.max_close_notional_micro if reduce_only else limits.max_order_notional_micro
    if notional > cap:
        if reduce_only:
            return deny("OVER_CLOSE_CAP", "this close is above the close limit; split it")
        return deny("OVER_ORDER_CAP", "order is above the per-order limit")

    run.append("price_band")
    mid = None
    if m.best_bid_micro is not None and m.best_ask_micro is not None:
        mid = (m.best_bid_micro + m.best_ask_micro) // 2
    if mid is not None and abs(intent.price_micro - mid) > limits.max_price_band_from_mid:
        # The typo that costs real money is 0.6 vs 6.0 on a 0.99 market. A band check is cheap and
        # catches it; a "are you sure?" dialog does not, because people click through dialogs.
        return deny("PRICE_FAR_FROM_MID", "price is far from the market; check the number")

    run.append("user_limits")
    if open_orders >= limits.max_open_orders_per_user:
        return deny("TOO_MANY_OPEN", "too many open orders on this account", retryable=True)
    if spent_24h_micro + notional > limits.max_24h_notional_micro:
        return deny("DAILY_CAP", "this order would exceed your 24h limit")

    if m.seconds_delay > 0:
        run.append("delayed_market")   # not a denial: the venue opens the book on a timer, we just say so

    return Decision(True, "OK", "accepted", notional_micro=notional,
                    latency_ms=(time.perf_counter() - t0) * 1000, checks_run=tuple(run),
                    reduce_only=reduce_only)
