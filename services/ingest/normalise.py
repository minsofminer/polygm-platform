"""Upstream shapes in, our shapes out. Every name here was read off a live payload
(tools/p05-capture-fixtures.py, recorded under tests/fixtures/p05/), not off the docs.

The three things this module is for:

1. Gamma hands back JSON **encoded as strings** (`outcomes`, `outcomePrices`, `clobTokenIds`) and floats where
   we need integers, so the parse-and-reject step has to be in one place or every consumer re-implements it
   and one of them forgets.
2. Timestamp units differ per source *on purpose*: `/trades` is seconds, the WebSocket is milliseconds
   (verified: 1789645254 vs "1789645560001"). Two functions that say which unit they accept, and a range
   check that fails loudly when someone feeds one to the other, is the only defence that survives a refactor.
3. Money and prices cross into micro-units here and never come back as floats.
"""
from __future__ import annotations

import json
import time
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

SCALE = 6                                    # must equal polygm_core.money.cents.SCALE; asserted in a test
USDC_MAX = 2 ** 53 - 1
UPDOWN_SLUG_MARKERS = ("-updown-5m-", "-updown-15m-", "-updown-1h-", "-updown-4h-", "-updown-daily-")


class ShapeError(ValueError):
    """The payload is not the shape we recorded. Say which field, never dump the row."""


def to_micro(value, *, field: str, allow_bool: bool = False) -> int:
    """Exact decimal text/number in, integer micro-units out. Floats are accepted ONLY when they round-trip.

    A venue price like 0.9995 has to survive the trip: `Decimal(str(0.9995))` is the honest read of what the
    JSON parser produced, and `int(0.9995 * 1e6)` is 999499, which is the bug this function exists to prevent.
    """
    if isinstance(value, bool) and not allow_bool:
        raise ShapeError("%s: bool where a number was expected" % field)
    if value is None:
        raise ShapeError("%s: missing" % field)
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ShapeError("%s: %r is not a number" % (field, value)) from None
    micro = d.scaleb(SCALE)
    if micro != micro.to_integral_value():
        raise ShapeError("%s: %r has more than %d decimals" % (field, value, SCALE))
    n = int(micro)
    if abs(n) > USDC_MAX:
        raise ShapeError("%s: %d exceeds the float-transport-safe bound" % (field, n))
    return n


def stat_micro(value, *, field: str) -> int:
    """Micro-units for the venue's OWN statistics: volume, liquidity, spread, best bid/ask as Gamma reports
    them. Same integer storage as money, but rounding is allowed and said out loud.

    Why the split: Gamma returns `volume24hr = 2101490.4147200002`, which is float noise produced on their
    side, not a value with 16 real decimals. `to_micro` refusing it would be correct-but-useless (the whole
    universe sync dies on a market nobody trades), and silently rounding it in `to_micro` would destroy the
    guarantee that matters, which is that no float may sneak into an ORDER. So: orders are exact and strict,
    statistics are rounded and marked approximate — and a UI must never show more precision than the venue
    actually measured, which is why the number is stored in micro but described as an estimate.
    """
    if value is None or value == "":
        return 0
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation:
        raise ShapeError("%s: %r is not a number" % (field, value)) from None
    n = int(d.scaleb(SCALE).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    if abs(n) > USDC_MAX:
        raise ShapeError("%s: %d exceeds the float-transport-safe bound" % (field, n))
    return n


def ts_ms_from_ws(raw) -> int:
    """The WS `timestamp` field: a STRING of MILLISECONDS. Verified against a live frame, not the docs."""
    try:
        n = int(str(raw))
    except (TypeError, ValueError):
        raise ShapeError("ws timestamp %r" % (raw,)) from None
    if n < 10 ** 12:                        # a seconds value passed here would read as 1970 forever
        raise ShapeError("ws timestamp %s looks like seconds, not milliseconds" % n)
    return n


def ts_s_from_rest(raw) -> int:
    """/trades and /activity timestamps: integer SECONDS. Same trap, opposite direction."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise ShapeError("rest timestamp %r" % (raw,)) from None
    if n >= 10 ** 12:
        raise ShapeError("rest timestamp %s looks like milliseconds; divide it at the source, not here" % n)
    return n


def age_ms(now_ms: int, ts_ms: int) -> int:
    """Age with the sign error made impossible: `now` first, and a future stamp is a bug worth surfacing."""
    if ts_ms > now_ms + 60_000:
        raise ShapeError("event is %d ms in the future (unit mismatch?)" % (ts_ms - now_ms))
    return max(0, now_ms - ts_ms)


def _json_list(row: dict, key: str) -> list:
    v = row.get(key)
    if isinstance(v, str):
        try:
            v = json.loads(v) if v.strip() else []
        except json.JSONDecodeError:
            raise ShapeError("%s: not JSON-encoded list (%r…)" % (key, v[:20])) from None
    if v is None:
        return []
    if not isinstance(v, list):
        raise ShapeError("%s: expected a list, got %s" % (key, type(v).__name__))
    return v


def market_from_gamma(row: dict) -> dict:
    """A Gamma `/markets` row to our `markets` insert. Ignores ~40 fields we have no use for (image, icon,
    pagerDutyNotificationEnabled, …): an ingest that copies everything turns every upstream rename into an
    outage, and the ignored list is what makes that a decision rather than an accident."""
    outcomes = _json_list(row, "outcomes")
    prices = _json_list(row, "outcomePrices")
    tokens = _json_list(row, "clobTokenIds")
    if len(outcomes) != len(tokens):
        raise ShapeError("outcomes/tokens length mismatch (%d/%d) for %s"
                         % (len(outcomes), len(tokens), row.get("conditionId", "?")))
    cond = row.get("conditionId")
    if not cond:
        raise ShapeError("row without conditionId")
    slug = str(row.get("slug") or "")
    evs = row.get("events") or []
    events = [e.get("slug") for e in evs if isinstance(e, dict)] if isinstance(evs, list) else []
    try:
        price_micro = [stat_micro(p, field="outcomePrices[%d]" % i) for i, p in enumerate(prices)]
    except ShapeError:
        price_micro = []                     # a market mid-resolution can carry "null"; the book is the truth
    return {
        "id": str(cond),
        "question": str(row.get("question") or "")[:300],
        "slug": slug,
        "event_slugs": events,
        "tokens": [str(t) for t in tokens],
        "outcomes": [str(o) for o in outcomes],
        "outcome_price_micro": price_micro,
        "end_ts": row.get("endDate"),
        "start_ts": row.get("startDate"),
        "accepting_orders": bool(row.get("acceptingOrders")),
        "closed": bool(row.get("closed")),
        "archived": bool(row.get("archived")),
        "enable_order_book": bool(row.get("enableOrderBook")),
        "seconds_delay": int(row.get("secondsDelay") or 0),
        "min_tick": str(row.get("orderPriceMinTickSize")),
        "min_order_size": str(row.get("orderMinSize")),
        "neg_risk": bool(row.get("negRisk")),
        "fee_type": str(row.get("feeType") or ""),
        "fees_enabled": bool(row.get("feesEnabled")),
        "volume_24h_micro": stat_micro(row.get("volume24hr"), field="volume24hr"),
        "liquidity_micro": stat_micro(row.get("liquidityNum"), field="liquidityNum"),
        "spread": stat_micro(row.get("spread"), field="spread"),
        "best_bid": stat_micro(row.get("bestBid"), field="bestBid"),
        "best_ask": stat_micro(row.get("bestAsk"), field="bestAsk"),
        "is_updown": any(m in slug for m in UPDOWN_SLUG_MARKERS),
        # `version: "v1"` IS NOT the CLOB version. It is Gamma's own row-version marker; CLOB V1 is dead and
        # no code path here reads this field (shared context, hard constraint 1). Asserted in a test so nobody
        # "fixes" a bug that does not exist by branching on it.
        "_gamma_row_version": str(row.get("version") or ""),
    }


def book_from_clob(payload: dict, now_ms: int | None = None) -> dict:
    """`GET /book` and the WS `book` frame have the same body, which is the only reason one function can
    serve both. Sizes are shares (not USDC) — the venue's own unit for depth."""
    if not isinstance(payload, dict) or "asset_id" not in payload:
        raise ShapeError("book payload has no asset_id")
    ts = ts_ms_from_ws(payload.get("timestamp"))
    return {
        "market": str(payload.get("market") or ""),
        "token_id": str(payload["asset_id"]),
        "ts_ms": ts,
        "hash": str(payload.get("hash") or ""),
        "bids": [(to_micro(l["price"], field="bid.price"), to_micro(l["size"], field="bid.size"))
                 for l in payload.get("bids") or []],
        "asks": [(to_micro(l["price"], field="ask.price"), to_micro(l["size"], field="ask.size"))
                 for l in payload.get("asks") or []],
        "min_order_size": str(payload.get("min_order_size") or ""),
        "tick_size": str(payload.get("tick_size") or ""),
        "last_trade_price": payload.get("last_trade_price"),
        "neg_risk": bool(payload.get("neg_risk")),
        "age_ms": age_ms(now_ms if now_ms is not None else int(time.time() * 1000), ts),
    }


def price_changes(msg: dict) -> list[dict]:
    """A `price_change` frame: N changes, each carrying its own hash and the venue's view of best bid/ask.

    Those two fields are the reason gap detection is possible at all — see books.Book.note_declared_best.
    """
    out = []
    for ch in msg.get("price_changes") or []:
        out.append({
            "token_id": str(ch["asset_id"]),
            "price_micro": to_micro(ch["price"], field="price"),
            "size_micro": to_micro(ch["size"], field="size"),
            "side": str(ch.get("side") or "").upper(),
            "hash": str(ch.get("hash") or ""),
            "best_bid_micro": to_micro(ch["best_bid"], field="best_bid") if ch.get("best_bid") else None,
            "best_ask_micro": to_micro(ch["best_ask"], field="best_ask") if ch.get("best_ask") else None,
            "ts_ms": ts_ms_from_ws(msg.get("timestamp")),
        })
    return out


def last_trade(msg: dict) -> dict:
    """The WS `last_trade_price` frame. Note the venue spells the size `size` and the side `side` here, and
    sends a `fee_rate_bps` we keep: attribution maths needs it, and re-deriving it later means trusting a
    market row that may have changed."""
    return {
        "token_id": str(msg["asset_id"]),
        "market": str(msg.get("market") or ""),
        "price_micro": to_micro(msg["price"], field="price"),
        "size_micro": to_micro(msg["size"], field="size"),
        "side": str(msg.get("side") or "").upper(),
        "ts_ms": ts_ms_from_ws(msg.get("timestamp")),
        "fee_rate_bps": int(Decimal(str(msg.get("fee_rate_bps") or 0))),
    }


def fill_from_rest(row: dict) -> dict:
    """A `/trades` row to our normalised fill. `outcomeIndex: 999` is the venue's "not applicable" sentinel
    (it appears on up/down markets and on aggregate rows), so it is NOT an outcome index and must never be
    used as one."""
    if not isinstance(row, dict):
        raise ShapeError("trade row is not an object")
    price, size = row.get("price"), row.get("size")
    if price is None or size is None:
        raise ShapeError("trade row without price/size")
    ts = ts_s_from_rest(row.get("timestamp"))
    # The product of two exact venue numbers is NOT exact at 6 dp: price 0.001 x size 1.000001 needs 9.
    # A fill's notional is a statistic that feeds labels and rollups, so it is rounded at the boundary and
    # stored in micro-units; the ORDER path in P04 keeps the strict rule because there a wrong product is a
    # rejected or double-spent order, not a chart.
    notional = stat_micro(Decimal(str(price)) * Decimal(str(size)), field="usd_notional")
    oi = row.get("outcomeIndex")
    return {
        "source": "rest",
        "tx_hash": str(row.get("transactionHash") or ""),
        "wallet": str(row.get("proxyWallet") or ""),
        "token_id": str(row.get("asset") or ""),
        "market": str(row.get("conditionId") or ""),
        "side": str(row.get("side") or "").upper(),
        "outcome": str(row.get("outcome") or ""),
        "outcome_index": None if oi in (999, "999", None) else int(oi),
        "price_micro": to_micro(price, field="price"),
        "size_micro": to_micro(size, field="size"),
        "usd_notional_micro": notional,
        "ts_ms": ts * 1000,
        "slug": str(row.get("slug") or ""),
        "is_updown": any(m in str(row.get("slug") or "") for m in UPDOWN_SLUG_MARKERS),
        "title": str(row.get("title") or "")[:300],
        "trader": {k: str(row.get(k) or "") for k in ("name", "pseudonym")},  # embedded: no profile join
    }


def history_points(payload: dict) -> list[tuple[int, int]]:
    """/prices-history → [(unix_s, price_micro)]. `fidelity` is in MINUTES, and the endpoint returns `t`/`p`
    only — no size, which is why a volume chart cannot be built from it and must come from the tape."""
    rows = payload.get("history") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ShapeError("history is not a list")
    out = []
    for r in rows:
        out.append((ts_s_from_rest(r.get("t")), to_micro(r.get("p"), field="history.p")))
    out.sort()
    return out
