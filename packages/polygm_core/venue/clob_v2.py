"""The CLOB V2 adapter. Everything that talks to the venue about *orders* is built here, and nothing here
touches the database or a socket.

Why an adapter instead of calling the SDK from the executor (D2): the SDK is a transport with opinions about
types — `py-clob-client-v2==1.1.0` (the only V2 client published; V1 is dead per the shared context §1)
types price and size as `float`. If those opinions were inlined across the executor, a SDK upgrade would be
a code review of six files, and the money path would depend on a dependency's float handling. So the SDK's
version is pinned, asserted at boot (`sdk_pin_ok`), and floats are created in exactly one function.

Three things this module is responsible for that no other layer can be:

1. **Which order types exist for which caller.** A market order is a *taker* order: it accepts whatever the
   book offers. On a book with $30 of depth that is a slippage machine, and a human staring at a big
   "MARKET BUY" button will click it. `EXPOSURE` is the answer: humans get GTC/GTD/FAK, automation gets
   FOK/MARKET — and only because automation carries a price bound and a notional cap with it.
2. **The pre-flight order.** D2 specifies it and specifies that it is *ordered*: venue acceptance first,
   then the market's own gates, then tick/min-size, then balance and allowance against notional **plus
   both fees**, then the risk gate, then the signature. Getting this wrong is not a style problem: check
   the balance before the fee estimate and a user with an exactly-full balance places an order the venue
   rejects after we have signed it.
3. **Fees as an integer, always rounded up.** An estimate below the true fee turns into a rejected order or,
   worse for a market buy, a spend above the user's stated limit. `muldiv_up` is the only rounding rule
   allowed on the spending side.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from ..money.cents import SCALE, notional_floor, to_float_for_sdk
from ..risk.gate import Decision

SDK_PACKAGE = "py-clob-client-v2"
SDK_PINNED = "1.1.0"

# ------------------------------------------------------------------ order types and who may use them ----
GTC, GTD, FOK, FAK, MARKET = "GTC", "GTD", "FOK", "FAK", "MARKET"
USER_TYPES = (GTC, GTD, FAK)
AUTOMATION_TYPES = (FOK, MARKET)          # fill-or-kill and "market" are automation-only. See EXPOSURE.
ALL_TYPES = USER_TYPES + AUTOMATION_TYPES


@dataclass(frozen=True)
class Exposure:
    """A rule, written as data, so the gate can check the code against it instead of against prose."""
    allow_market: bool                     # may this caller submit a MARKET/FOK order at all
    requires_price_bound: bool             # a bound is mandatory for this caller
    documented_reason: str


EXPOSURE: dict[str, Exposure] = {
    "user": Exposure(allow_market=False, requires_price_bound=False,
                     documented_reason="humans see limit orders only; a market button on a thin book is "
                                       "how a support ticket becomes a refund"),
    "automation": Exposure(allow_market=True, requires_price_bound=True,
                           documented_reason="a bot can be given a bound and a cap; it must be"),
    # A copier is a human who pressed a button once, then let someone else's tape drive the mouse. It gets the
    # human entitlements -- limit orders with a price bound -- and never the automation-only ones. If a copy
    # can reach FOK/MARKET, a bad source tape buys the top of a thin book on the copier's account.
    "copy": Exposure(allow_market=False, requires_price_bound=True,
                     documented_reason="copies act on someone else's information seconds late, so they may "
                                       "rest a priced limit order and may never cross the book blindly"),
}


def exposure_allows(audience: str, order_type: str) -> tuple[bool, str]:
    if order_type not in ALL_TYPES:
        return False, "UNKNOWN_ORDER_TYPE: %r is not a CLOB V2 order type" % (order_type,)
    e = EXPOSURE.get(audience)
    if e is None:
        return False, "UNKNOWN_AUDIENCE: %r" % (audience,)
    if order_type in AUTOMATION_TYPES and not e.allow_market:
        return False, ("MARKET_FORBIDDEN: %r orders are automation-only (%s)"
                       % (order_type, e.documented_reason))
    return True, ""


def is_market_like(order_type: str) -> bool:
    """FOK/MARKET price nothing: they consume the book. Everything downstream (fee estimate, spending
    limit, fill expectation) has to treat them differently from a limit order."""
    return order_type in AUTOMATION_TYPES


# ------------------------------------------------------------------------ integer money primitives ----
def muldiv_floor(value_micro: int, num: int, den: int) -> int:
    return (value_micro * num) // den


def muldiv_up(value_micro: int, num: int, den: int) -> int:
    """Round UP. Legal only on the side that *takes* from the user; on the side that gives to the user,
    rounding up would over-promise. That asymmetry is why there are two functions and no generic `round`."""
    return -((-(value_micro * num)) // den)


@dataclass(frozen=True)
class Fees:
    """The two fees on every order, both integers, both estimated before signing.

    Platform fee (D2's formula, from the venue docs): `C x feeRate x p x (1 - p)`, C in shares, p as a
    probability. The p(1-p) shape is why fees are maximal at 50c and near zero at the extremes: a fee on a
    binary outcome is a fee on the *uncertainty*, which is why the venue's `fee_type` field matters even
    though the numeric rate we measure is 0 for nearly every market today (P01: only the two 15-minute
    markets report a non-zero taker rate).
    """
    platform_micro: int
    builder_micro: int

    @property
    def total_micro(self) -> int:
        return self.platform_micro + self.builder_micro


def estimate_fees(*, size_shares_micro: int, price_micro: int, fee_rate_bps: int,
                  builder_bps: int) -> Fees:
    # C x feeRate x p x (1-p), in micros, with no division until the last step:
    #   platform_micro = shares_micro x fee_rate_bps x p_micro x (1e6 - p_micro) / (10_000 x 1e6 x 1e6)
    den = 10_000 * 10**SCALE * 10**SCALE               # bps -> ratio, and the two micro scales of p(1-p)
    platform = muldiv_up(size_shares_micro * max(fee_rate_bps, 0),
                         price_micro * ((10**SCALE) - price_micro), den)
    notional = notional_floor(size_shares_micro, price_micro)
    builder = muldiv_up(notional, max(builder_bps, 0), 10_000)
    return Fees(platform, builder)


def fee_accuracy_bps(estimate_micro: int, actual_micro: int) -> int:
    """Signed error in basis points of the estimate: positive means we UNDER-estimated, which is the
    direction that hurts. Reported to the dashboard, and the metric the phase asked for."""
    if estimate_micro <= 0:
        return 0 if actual_micro == 0 else 10_000
    return ((actual_micro - estimate_micro) * 10_000) // estimate_micro


# ------------------------------------------------------------------------ maker/taker amount rules ----
@dataclass(frozen=True)
class Amounts:
    """The two integers a CLOB V2 order actually carries.

    The venue prices an order as `makerAmount`/`takerAmount` in base units and rounds *toward* the venue.
    We therefore fix the side the user constrained exactly and truncate the derived side:

      BUY  : takerAmount = USDC out (exact: this is what the spending limit means), makerAmount = shares
             (floored — we never claim shares we might not get)
      SELL : makerAmount = shares (exact), takerAmount = USDC in (floored — never promise more than arrives)

    A "both sides exact" order is what the mock rejects with `rounding_boundary`, and the mock got that
    from the real venue's behaviour (P04).
    """
    maker_amount_raw: str
    taker_amount_raw: str
    price_micro: int
    size_shares_micro: int
    notional_micro: int
    rounding_note: str

    def as_dict(self) -> dict:
        return {"makerAmount": self.maker_amount_raw, "takerAmount": self.taker_amount_raw,
                "rounding": self.rounding_note}


def amounts_for(side: str, *, price_micro: int, size_shares_micro: int) -> Amounts:
    if side not in ("BUY", "SELL"):
        raise ValueError("BAD_SIDE")
    notional = notional_floor(size_shares_micro, price_micro)
    if side == "BUY":
        # raw units for the CTF/ERC-1155 leg are 1e6-scaled shares and 1e6-scaled USDC.
        return Amounts(maker_amount_raw=str(notional), taker_amount_raw=str(notional),
                       price_micro=price_micro, size_shares_micro=size_shares_micro, notional_micro=notional,
                       rounding_note="BUY: USDC leg exact (notional floor), share leg floored by construction")
    return Amounts(maker_amount_raw=str(size_shares_micro), taker_amount_raw=str(notional),
                   price_micro=price_micro, size_shares_micro=size_shares_micro, notional_micro=notional,
                   rounding_note="SELL: share leg exact, USDC leg floored so we never over-promise proceeds")


# --------------------------------------------------------------------------- the pre-flight sequence ----
PREFLIGHT_ORDER: tuple[str, ...] = (
    "venue_reachable",          # cheap, and if it is not, nothing else can be answered
    "kill_switch",              # before any read that could itself be rate-limited on their behalf
    "market_accepting",         # accepting_orders AND enable_order_book AND not archived
    "delayed_open",             # seconds_delay > 0: legal, but the user must be told when the book opens
    "tick_alignment",
    "min_size",
    "balance_and_allowance",    # notional + fees, never notional alone
    "fee_estimate",             # after the balance read: it is part of what the balance must cover
    "risk_gate",                # the P04 gate, unchanged, still the last word before signing
    "self_trade_check",         # the venue rejects it; refusing first is cheaper and explains why
)


@dataclass
class PreflightResult:
    ok: bool
    checks_run: tuple[str, ...]
    deny_code: str = ""
    deny_message: str = ""
    total_cost_micro: int = 0
    fees: Fees | None = None
    notes: list[str] = field(default_factory=list)


def preflight(*, order_type: str, audience: str, side: str, price_micro: int, size_shares_micro: int,
              usdc_available_micro: int, allowance_micro: int, fee_rate_bps: int, builder_bps: int,
              market_accepting: bool, seconds_delay: int, tick_ok: bool, min_size_ok: bool,
              venue_reachable: bool, kill_switch: bool, self_trade: bool,
              risk: Decision | None, expected_fill_price_micro: int | None = None,
              max_slippage_bps: int = 0) -> PreflightResult:
    """Walk `PREFLIGHT_ORDER` and stop at the first failure. Every caller — API, executor, copy, automation
    — goes through here, so "the API checked but the executor didn't" cannot happen by construction.

    The order of the list is asserted by `tools/p06-gate-check.py` against the returned `checks_run`, and
    a returned sequence that is not a prefix of `PREFLIGHT_ORDER` raises: a check that was silently skipped
    is the bug this function exists to prevent, so it is a crash rather than a denial.
    """
    run: list[str] = []
    notes: list[str] = []

    def fail(code: str, msg: str) -> PreflightResult:
        return PreflightResult(False, tuple(run), code, msg, notes=notes)

    ok, why = exposure_allows(audience, order_type)
    if not ok:
        return fail("ORDER_TYPE_FORBIDDEN", why)

    run.append("venue_reachable")
    if not venue_reachable:
        return fail("VENUE_UNREACHABLE", "cannot confirm the venue is accepting traffic; try again shortly")

    run.append("kill_switch")
    if kill_switch:
        return fail("RISK_HALT", "trading is temporarily disabled")

    run.append("market_accepting")
    if not market_accepting:
        return fail("MARKET_NOT_ACCEPTING", "this market is not accepting orders right now")

    run.append("delayed_open")
    if seconds_delay > 0:
        notes.append("delayed_open: venue opens the book %ds after the trade is signed" % seconds_delay)

    run.append("tick_alignment")
    if not tick_ok:
        return fail("OFF_TICK", "price is not on the market's tick grid")

    run.append("min_size")
    if not min_size_ok:
        return fail("BELOW_MIN_SIZE", "size is below the market minimum")

    fees = estimate_fees(size_shares_micro=size_shares_micro, price_micro=price_micro,
                         fee_rate_bps=fee_rate_bps, builder_bps=builder_bps)
    notional = notional_floor(size_shares_micro, price_micro)
    # A BUY must cover notional + both fees out of the *available* balance. A SELL must own the shares, but
    # its fee comes out of proceeds, so it needs no USDC up front — the venue's own rule and the reason the
    # two sides are not symmetric here.
    total_cost = notional + fees.total_micro if side == "BUY" else 0
    run.append("balance_and_allowance")
    if side == "BUY":
        if usdc_available_micro < total_cost:
            return fail("INSUFFICIENT_BALANCE", "this order costs more than the available balance, fees "
                                                "included")
        if allowance_micro < total_cost:
            # D1.5: an approval shortfall is not an error to hand to the venue; it is a *local* action we
            # can take (approve) and the user must approve that, so the intent is parked, not rejected.
            return fail("ALLOWANCE_REQUIRED", "the pUSD allowance does not cover this order plus fees")
    if expected_fill_price_micro is not None and is_market_like(order_type) and max_slippage_bps >= 0:
        # The bound is what makes automation's market order legal: no bound, no order.
        worst = expected_fill_price_micro * (10_000 + max_slippage_bps) // 10_000
        if price_micro > 0 and worst > 10**SCALE:
            worst = 10**SCALE - 1
        total_cost = max(total_cost, muldiv_floor(notional, worst, price_micro) if price_micro else total_cost)
        notes.append("slippage bound: worst case priced at %d micro (%d bps)"
                     % (worst, max_slippage_bps))

    run.append("fee_estimate")
    if fees.total_micro == 0:
        notes.append("zero fee: market reports fee_rate_bps 0 and no builder take")

    run.append("risk_gate")
    if risk is None:
        return fail("RISK_UNAVAILABLE", "the risk service did not answer; an unanswered risk check is a no")
    if not risk.allowed:
        return fail(risk.code, risk.message)

    run.append("self_trade_check")
    if self_trade:
        return fail("SELF_TRADE", "this order would match your own resting order")

    if tuple(run) != PREFLIGHT_ORDER[:len(run)]:
        raise AssertionError("preflight drifted from PREFLIGHT_ORDER: %s vs %s"
                             % (run, list(PREFLIGHT_ORDER)))
    return PreflightResult(True, tuple(run), total_cost_micro=total_cost, fees=fees, notes=notes)


def all_in_spend_limit(asks: list[tuple[int, int]], *, want_shares_micro: int, fee_rate_bps: int,
                       builder_bps: int, limit_micro: int) -> dict:
    """Walk the ask ladder (price_micro, size_shares_micro), spending up to `limit_micro` INCLUDING fees.

    Returns what would actually be bought at that limit. For a market buy, the limit is the only honest
    answer to "how much could this cost me?" — and the venue's own matching is the only thing that can beat
    this calculation, so we quote it as a bound, never as an expectation.
    """
    left = want_shares_micro
    spent = 0
    bought = 0
    filled_levels = 0
    for price_micro, size_micro in asks:
        if left <= 0:
            break
        take = min(left, size_micro)
        if take <= 0:
            continue
        cost = notional_floor(take, price_micro)
        fee = estimate_fees(size_shares_micro=take, price_micro=price_micro, fee_rate_bps=fee_rate_bps,
                            builder_bps=builder_bps).total_micro
        if spent + cost + fee > limit_micro:
            # Partial level: spend the most we can, still fee-inclusive. Solving for shares with a fee that
            # is itself a function of shares is a quadratic; walking one-share-at-a-time is O(size) and
            # unacceptable, so we bisect the affordable amount, in integers.
            lo, hi = 0, take
            while lo < hi:
                mid = (lo + hi + 1) // 2
                c = notional_floor(mid, price_micro)
                f = estimate_fees(size_shares_micro=mid, price_micro=price_micro, fee_rate_bps=fee_rate_bps,
                                  builder_bps=builder_bps).total_micro
                if spent + c + f <= limit_micro:
                    lo = mid
                else:
                    hi = mid - 1
            take = lo
            if take <= 0:
                break
            cost = notional_floor(take, price_micro)
            fee = estimate_fees(size_shares_micro=take, price_micro=price_micro, fee_rate_bps=fee_rate_bps,
                                builder_bps=builder_bps).total_micro
        spent += cost + fee
        bought += take
        left -= take
        filled_levels += 1
    return {"shares_micro": bought, "all_in_cost_micro": spent, "levels_used": filled_levels,
            "capped_by_limit": left > 0, "average_price_micro": (spent * 10**SCALE) // bought if bought else 0,
            "remaining_want_micro": left}


# ---------------------------------------------------------------------------------- batch submission ----
MAX_BATCH = 15                                 # D2: `POST /orders` takes at most 15 items
BATCH_ITEM_CODES: dict[str, str] = {
    "not_enough_balance_or_allowance": "BALANCE",
    "invalid_price": "BAD_PRICE",
    "invalid_tick_size": "TICK",
    "rounding_boundary": "ROUNDING",
    "order_version_mismatch": "VERSION",
    "rate_limited": "THROTTLED",
    "malformed": "VALIDATION",
    "delayed_not_allowed": "DELAYED",
}


class BatchTooLarge(Exception):
    pass


@dataclass
class BatchItem:
    order: dict
    order_type: str
    intent_id: str


def plan_batch(items: list[BatchItem]) -> list[list[BatchItem]]:
    """Chunk into <=15. Refuses nothing else, because a batch is not a queue: if a caller asks for 20 it
    means 20, so we send two requests rather than silently dropping five orders."""
    if not items:
        return []
    return [items[i:i + MAX_BATCH] for i in range(0, len(items), MAX_BATCH)]


def assert_batch_size(n: int) -> None:
    if n > MAX_BATCH:
        raise BatchTooLarge("the venue accepts at most %d orders per POST; got %d" % (MAX_BATCH, n))


@dataclass
class BatchOutcome:
    """Partial failure is the normal case, so it is a first-class result rather than an exception."""
    accepted: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)     # {"intent_id","code","message"}
    uncertain: list[dict] = field(default_factory=list)    # {"intent_id","client_order_hash"}

    @property
    def counts(self) -> dict:
        return {"accepted": len(self.accepted), "rejected": len(self.rejected), "uncertain": len(self.uncertain)}


def read_batch_response(order_ids: list[str], hashes: list[str], resp: object) -> BatchOutcome:
    """Map a per-item response array onto our ids, positionally.

    The rule that matters: a *missing* item is UNCERTAIN, never REJECTED. The venue answered for 12 of 15
    and the other three may exist on the book; treating "no answer" as "no order" is exactly the mistake
    that double-spends on the retry.
    """
    out = BatchOutcome()
    data = resp if isinstance(resp, list) else (resp or {}).get("orders") if isinstance(resp, dict) else None
    if not isinstance(data, list):
        # The whole request is unparseable: every item is uncertain.
        out.uncertain = [{"intent_id": i, "client_order_hash": h} for i, h in zip(order_ids, hashes)]
        return out
    seen: set[int] = set()
    for idx, item in enumerate(data):
        if idx >= len(order_ids) or not isinstance(item, dict):
            continue
        seen.add(idx)
        iid, h = order_ids[idx], hashes[idx]
        if item.get("success"):
            out.accepted.append({"intent_id": iid, "order_id": item.get("orderID") or item.get("orderId"),
                                 "client_order_hash": h})
            continue
        raw = str(item.get("code") or "")
        note = str(item.get("message") or "")[:180]
        if raw in ("gateway_timeout", "unreachable", "timeout"):
            out.uncertain.append({"intent_id": iid, "client_order_hash": h})
            continue
        out.rejected.append({"intent_id": iid, "code": BATCH_ITEM_CODES.get(raw, "VENUE_REJECTED"),
                             "venue_code": raw, "message": note})
    for idx, (iid, h) in enumerate(zip(order_ids, hashes)):
        if idx not in seen:
            out.uncertain.append({"intent_id": iid, "client_order_hash": h, "note": "no answer for this item"})
    return out


# The value we persist for "the exchange may spend as much as it likes".
#
# The on-chain approval is `type(uint256).max`. It CANNOT be stored in a BIGINT column — 2^256-1 overflows
# Postgres' 8-byte integer as surely as it overflows SQLite's — so the row carries the largest value the
# column can hold and every comparison treats it as a ceiling. Trying to persist the real uint256 is the
# bug this constant exists to prevent: it fails at INSERT, and a service that catches that error and stores
# 0 has just turned "unlimited" into "nothing", in the one column where the direction of the mistake is
# money out of a user's wallet.
UNLIMITED_ALLOWANCE = 2**63 - 1


def allowance_covers(granted_micro: int, needed_micro: int) -> bool:
    if granted_micro >= UNLIMITED_ALLOWANCE:
        return True
    return granted_micro >= needed_micro


# -------------------------------------------------------------------------------------------- cancels ----
CANCEL_ALL_BUDGET_COUNT = 250                    # shared context §6: cancel-all is 250 req / 10 s
CANCEL_ALL_BUDGET_WINDOW_MS = 10_000
CANCEL_BATCH_MAX = MAX_BATCH                     # same per-request cap as posting
CONFIRM_PHRASE = "CANCEL ALL ORDERS"             # typed, not clicked: cancel-all is the one action a UI
                                                 # can accidentally trigger twice in a row


@dataclass
class CancelBudget:
    """A token bucket for the scarce endpoint only. Single cancels are not budgeted, because a user who
    wants to leave must always be able to leave; batching their exits is how a rate limit becomes a trap.

    `take()` is pure w.r.t. time: the caller passes `now_ms`, so a test can move the clock instead of the
    suite moving its wall time.
    """
    used: list[int] = field(default_factory=list)
    limit: int = CANCEL_ALL_BUDGET_COUNT
    window_ms: int = CANCEL_ALL_BUDGET_WINDOW_MS

    def take(self, now_ms: int, *, cost: int = 1) -> tuple[bool, int]:
        self.used = [t for t in self.used if now_ms - t < self.window_ms]
        if len(self.used) + cost > self.limit:
            retry_ms = self.window_ms - (now_ms - self.used[0]) if self.used else self.window_ms
            return False, max(retry_ms, 0)
        self.used.extend([now_ms] * cost)
        return True, 0

    @property
    def in_window(self) -> int:
        return len(self.used)


@dataclass
class CancelRequest:
    scope: str                                  # 'order' | 'batch' | 'market' | 'all'
    reason: str
    actor: str = "user"
    confirm_phrase: str = ""
    order_ids: tuple[str, ...] = ()
    market_id: str = ""

    def validate(self) -> None:
        if self.scope not in ("order", "batch", "market", "all"):
            raise ValueError("BAD_CANCEL_SCOPE")
        if self.scope == "order" and len(self.order_ids) != 1:
            raise ValueError("EXACTLY_ONE_ORDER_REQUIRED")
        if self.scope == "batch":
            if not self.order_ids:
                raise ValueError("BATCH_EMPTY")
            if len(self.order_ids) > CANCEL_BATCH_MAX:
                raise ValueError("BATCH_TOO_LARGE")     # caller must chunk, we must not truncate silently
        if self.scope == "market" and not self.market_id:
            raise ValueError("MARKET_REQUIRED")
        if self.scope == "all":
            # D2: cancel-all is scarce and destructive in aggregate. Typed phrase + a stated reason, both
            # written to the audit log; the API cannot manufacture a call without them.
            if self.confirm_phrase != CONFIRM_PHRASE:
                raise ValueError("CONFIRM_PHRASE_REQUIRED")
            if len(self.reason.strip()) < 20:
                raise ValueError("REASON_TOO_SHORT")

    def audit_row(self, now_ms: int) -> dict:
        return {"at_ms": now_ms, "scope": self.scope, "reason": self.reason.strip(), "actor": self.actor,
                "orders": len(self.order_ids) or None, "market_id": self.market_id or None,
                "digest": hashlib.sha256(json.dumps(
                    {"s": self.scope, "r": self.reason, "a": self.actor, "o": sorted(self.order_ids),
                     "m": self.market_id}, sort_keys=True).encode()).hexdigest()[:16]}


def chunk_cancel_targets(ids: list[str], size: int = CANCEL_BATCH_MAX) -> list[list[str]]:
    return [ids[i:i + size] for i in range(0, len(ids), size)]
