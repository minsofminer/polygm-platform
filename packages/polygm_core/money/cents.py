"""Integer-cent money arithmetic. THE source of truth for every value in this product.

Why not Decimal end-to-end: the ledger, the risk gate and the venue all speak integers (USDC has 6
decimals, share balances 6, prices are integer ticks), and an integer column cannot drift in precision
the way a NUMERIC with the wrong scale can. Why not float, ever: measured on this machine, 960 of the 999
prices in 0.01..9.99 are not exactly representable as a float, and `float` is the only type in the
official V2 SDK's order struct (`OrderArgsV2.price: float`, py-clob-client-v2==1.1.0). That is fine for
transport and fatal for accounting, so the boundary is explicit and asserted (see `to_float_for_sdk`).

Scale map, from P01's live probe (see docs/P01-product-spec.md D-facts):
    price      integer ticks of 1e-6 USDC (tick sizes observed: 0.001 and 0.01)
    size       shares, 1e-6 units
    usdc       cents are NOT the atomic unit here: USDC is 6dp, so the atomic unit is 1e-6
The one place a "cent" appears is the *displayed* dollar value, which is derived, never stored.
"""
from __future__ import annotations

from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, localcontext

# 1e-6 is USDC's and Polymarket's share precision; everything in this module is in those units.
SCALE = 6
_UNIT = Decimal(10) ** -SCALE
MAX_SAFE_INT = 2 ** 53 - 1          # above this, float transport cannot round-trip (see below)

PRICE_MIN_TICKS = 1                 # a price of 0 is not an order
PRICE_MAX_TICKS = 999_999           # prices live in (0, 1) USDC => at most 999999 x 1e-6


class MoneyError(ValueError):
    """Every money failure is this type or a subclass, so the API layer can map it to an error code
    without string matching (and without ever leaking the numeric detail into a client message)."""


class ScaleError(MoneyError):
    pass


class OverflowError_(MoneyError):
    pass


def parse_usdc(text: str | int | Decimal) -> int:
    """'0.51' | 51 (already micro) | Decimal -> integer micro-USDC. Non-negative amounts only.

    Rejects anything with more precision than the chain has, rather than silently rounding it: a
    truncated entry price is a real, small, cumulative loss.

    `float` is refused by TYPE, not by value. The obvious implementation (`Decimal(str(f))`) accepts
    0.5 happily and rejects 0.1, so it turns "was a float used in the money path" into a flaky test —
    and the case that actually breaks is the one near 2**53, where a value we accepted cannot come back
    through the SDK's float fields. Measured, not assumed: float(m)/1e6 round-trips for every m in
    [1, 2**53-2] and fails at m = 2**53-1, so the bound is `>=`, not `>`.
    """
    if isinstance(text, bool) or isinstance(text, float):
        raise ScaleError("floats are not accepted as money input; pass a decimal string")
    try:
        d = Decimal(str(text)) if not isinstance(text, Decimal) else text
    except Exception as e:                                   # noqa: BLE001 - reclassified, never swallowed
        raise ScaleError(f"{text!r} is not a decimal number ({type(e).__name__})") from e
    if not d.is_finite():
        raise ScaleError(f"{text!r} is not a finite number")
    if d < 0:
        raise ScaleError("use Decimal arithmetic for signed amounts; parse_usdc takes magnitudes")
    micro = d.scaleb(SCALE)
    if micro != micro.to_integral_value():
        raise ScaleError(f"{text!r} has more than {SCALE} decimal places")
    q = int(micro)
    if q >= MAX_SAFE_INT:
        raise OverflowError_(f"{text!r} exceeds the exact-integer range for float transport")
    return q


def fmt_usdc(micro: int) -> str:
    """Display form. Always '.', never locale-dependent (D6.3 of the design system): a terminal user
    pastes these into the venue, so a comma would be a wrong number, not a style choice."""
    if not isinstance(micro, int):
        raise ScaleError("format requires an integer micro-unit")
    sign = "-" if micro < 0 else ""
    a = abs(micro)
    return f"{sign}{a // 10**6}.{a % 10**6:06d}".rstrip("0").rstrip(".") or sign + "0"


def to_float_for_sdk(micro: int, *, field: str) -> float:
    """The ONLY place money becomes a float, and it is asserted.

    The SDK takes floats; we keep integers. If a value cannot cross that boundary and come back
    identical, the order is not signed — a wrong size at the venue is a money bug, and "the SDK wanted a
    float" is not a reason to ship one.
    """
    f = float(micro) / 10 ** SCALE
    if Decimal(repr(f)).scaleb(SCALE).to_integral_value() != micro:
        raise ScaleError(f"{field}={micro} cannot round-trip through float (repr -> {f!r})")
    return f


def price_ticks(price: str | Decimal) -> int:
    """'0.635' -> 635000 micro-USDC per share. Prices are stored as micro-USDC like everything else,
    because a tick size can be 0.001 and 0.01 in the same deployment (measured in P01)."""
    try:                                   # same reclassification as parse_usdc: decimal's own errors are
        d = Decimal(str(price))             # ArithmeticError subclasses, and an uncaught one is a 500 with a
    except Exception as e:                  # Python message in the response. Money failures stay MoneyErrors.
        raise ScaleError(f"price {price!r} is not a decimal number ({type(e).__name__})") from e
    if not d.is_finite():
        raise ScaleError(f"price {price!r} is not a finite number")
    if not (Decimal(0) < d < Decimal(1)):
        raise ScaleError(f"price {price!r} outside (0,1)")
    micro = d.scaleb(SCALE)
    if micro != micro.to_integral_value():
        raise ScaleError(f"price {price!r} finer than 1e-6")
    return int(micro)


def notional_floor(size_shares_micro: int, price_micro: int) -> int:
    """Cost of buying `size` shares at `price`, micro-USDC, ROUNDED DOWN.

    Rounding direction is the venue's, not ours to choose: py-clob-client-v2 exposes
    `order_builder.round_down` and `ROUNDING_CONFIG` for exactly this, and rounding *up* on a buy makes a
    user's balance check fail on an order the venue would accept. Floor is therefore the only safe choice
    for spend, and it is applied to buys; sells floor the *proceeds* for the same reason (never credit
    money that has not arrived).
    """
    with localcontext() as ctx:
        ctx.prec = 50
        raw = (Decimal(size_shares_micro) * Decimal(price_micro)) / Decimal(10 ** SCALE)
        return int(raw.to_integral_value(rounding=ROUND_FLOOR))


def ceil_div(a: int, b: int) -> int:
    return -((-a) // b)


def micros(v: Decimal | int, scale_in: int = SCALE) -> int:
    """Convert an INTEGER carrying `scale_in` decimals into canonical micro units.

    micros(150, scale_in=4) == 15_000: the input is the integer 150 and the reader is told it has 4
    decimals, i.e. it means 0.015. Used for venue payloads that arrive as scaled integers.
    """
    shift = SCALE - scale_in
    if shift >= 0:
        return int(Decimal(v) * Decimal(10) ** shift)
    raise ScaleError("canonical scale is the finest supported")
