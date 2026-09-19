"""P10's arithmetic: the rules the terminal's numbers are computed by, in one stdlib-only module.

Everything here is integer. Not "integer except when it is convenient": the module has no float literal, no
true division and no `round()`, because each of these functions ends up next to a dollar figure on a screen and
a screen is where a float stops being invisible. `micro` is 1e-6 of a unit (micro-USDC), which is the smallest
unit the venue's own client deals in, so nothing is lost by staying integral.

The four rules the phase is graded on live here rather than in a component, so no client can skip them:

  * the whale threshold is RELATIVE with an absolute fallback (`whale_threshold_micro`);
  * a win rate below the sample gate is `None` WITH A REASON, never a percentage (`win_rate`);
  * a PnL curve cannot exist without its drawdown, because the overlay is what builds the points
    (`drawdown_overlay`);
  * a copy decision quotes the slippage actually measured on OUR copies, and defaults to skipping
    (`copy_slippage_warning`).
"""
from __future__ import annotations

from typing import Iterable, Sequence

MICRO = 1_000_000                       # micro-USDC per USDC; the money path's unit everywhere

#: Settled markets needed before a win rate is shown at all. 20 is the phase's number: below it, a
#: 4-for-5 record and a 5-for-5 record are the same evidence, which is none.
SAMPLE_GATE = 20

#: The absolute floor under the relative whale term, in micro-USDC ($500). Measured in P05: the median fill is
#: $5 and p95 is $133, so a $1,000 fixed threshold is simultaneously too high for a small market and too low
#: for a big one — the percentile is the part that adapts, and this is the part that stops a thin market's
#: p99.5 from being a $3 fill.
WHALE_FLOOR_MICRO = 500 * MICRO

#: Fills in the window needed before a percentile of that window means anything. Below it the relative term is
#: DISCARDED and the floor applies alone (reason `absolute_fallback`) — a p99.5 of twelve fills is a statement
#: about twelve fills, and the day the fixture had three fills it crowned the largest one a whale.
WHALE_MIN_SAMPLE = 40

#: Severity is the fill's ratio to ITS OWN market's threshold: at the threshold it is a whale and not a big
#: one (`info`), 1.5x is worth a notification, 4x is worth interrupting somebody. The boundaries are inclusive
#: lower bounds, stated once here and echoed into the UI's tooltip by `whale_severity`'s own `rule`.
SEVERITY_NOTICE_BPS = 15_000
SEVERITY_URGENT_BPS = 40_000

#: Per-size-bucket floors (D4's "defaults per market-size bucket, tunable"). The bucket comes from the market's
#: median fill, so the floor scales with the market rather than being one number for the whole venue.
BUCKET_FLOOR_MICRO = {"small": 500 * MICRO, "mid": 2_000 * MICRO, "large": 10_000 * MICRO}
_BUCKET_BOUNDS = ((25_000 * MICRO, "small"), (1_000_000 * MICRO, "mid"))

WINDOW_KEYS = ("7d", "30d", "90d", "all")
WINDOW_DAYS = {"7d": 7, "30d": 30, "90d": 90, "all": None}


# --------------------------------------------------------------------------- the guard every function uses
def _int(value, what: str = "value") -> int:
    """An integer, or a `TypeError`. This is the module's one door, and floats do not fit through it.

    Strings are parsed because the wire is decimal strings by contract (a JSON number is a float the moment a
    client touches it) and `None` is 0 because a NULL column means "we have no number", which is what every
    caller wants to add.
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value.strip() or 0)
    raise TypeError("%s must be an integer, got %s (%r)" % (what, type(value).__name__, value))


def _ints(values: Iterable, what: str = "values") -> list[int]:
    return [_int(v, what) for v in values]


# --------------------------------------------------------------------------- distributions
def median_micro(values: Sequence[int]) -> int:
    """The median, rounding DOWN for an even count: (2 + 3) / 2 is 2, not 3.

    Down rather than half-up because this feeds a threshold that decides what the tape calls a whale. Rounding
    up occasionally promotes the fill sitting exactly at the boundary, and the error a threshold is allowed to
    make is the one that shows fewer whales than more.
    """
    vals = sorted(_ints(values))
    if not vals:
        return 0
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) // 2


def percentile_micro(values: Sequence[int], num: int, den: int) -> int:
    """Nearest-rank percentile: the value at rank ceil(n * num / den), never an interpolation.

    Between two real fills there is no fill. A linear-interpolated p99.5 invents a number the market never
    printed, and the threshold derived from it would be a threshold nothing can be checked against.
    """
    vals = sorted(_ints(values))
    if not vals:
        return 0
    num, den = _int(num, "num"), _int(den, "den")
    if den <= 0:
        raise ValueError("den must be positive")
    rank = -((-num * len(vals)) // den)              # ceiling division, integer-only
    rank = max(1, min(rank, len(vals)))
    return vals[rank - 1]


# --------------------------------------------------------------------------- the whale rule
def market_size_bucket(median_micro: int) -> str:
    """`small` / `mid` / `large`, from the market's own median fill.

    A market whose typical fill is $5 and one whose typical fill is $5,000 cannot share one whale number: the
    first would call every $600 fill a whale and the second would call none. The bucket is the input to the
    per-size floors, and it is stated in the facets payload so a user can see which rule they are under.
    """
    med = _int(median_micro, "median_micro")
    for bound, name in _BUCKET_BOUNDS:
        if med < bound:
            return name
    return "large"


def bucket_floor_micro(median_micro: int) -> int:
    return BUCKET_FLOOR_MICRO[market_size_bucket(median_micro)]


def whale_threshold_micro(*, p995: int = 0, median: int = 0, fills: int = 0,
                          mode: str = "relative", multiple: int | None = None,
                          floor_micro: int | None = None) -> dict:
    """`max(relative term, absolute floor)`, with the term's provenance returned alongside it.

    Three reasons, and the UI shows `rule` verbatim, because "why is this $500" is a question the badge has to
    answer next to the badge:
      * `relative`          — the window's own p99.5 (or `multiple` x its median) is above the floor;
      * `absolute_floor`    — the term exists but does not clear the floor;
      * `absolute_fallback` — the window is too thin for a percentile at all (< `WHALE_MIN_SAMPLE` fills), so
                              the floor applies alone. Reporting the percentile here would be reporting a
                              number computed from a handful of fills as if it were a distribution.
    """
    p995, median, fills = _int(p995, "p995"), _int(median, "median"), _int(fills, "fills")
    floor = WHALE_FLOOR_MICRO if floor_micro is None else _int(floor_micro, "floor_micro")
    mult = None if multiple is None else _int(multiple, "multiple")
    if mode == "multiple" and mult is not None:
        relative, term = median * mult, "%d× this market's median fill of %s" % (mult, _micro_str(median))
    else:
        relative, term = p995, "the p99.5 fill of this market's %d fills (%s)" % (fills, _micro_str(p995))
    if fills < WHALE_MIN_SAMPLE:
        return {"thresholdMicro": floor, "reason": "absolute_fallback", "sampleOk": False,
                "relativeMicro": relative, "floorMicro": floor, "p995Micro": p995, "medianMicro": median,
                "fills": fills, "mode": mode, "multiple": mult,
                "rule": ("whale = max(%s, %s absolute floor) = %s: only %d fills are in the window and a "
                         "percentile needs at least %d, so the floor applies alone"
                         % (term, _micro_str(floor), _micro_str(floor), fills, WHALE_MIN_SAMPLE))}
    if relative <= floor:
        return {"thresholdMicro": floor, "reason": "absolute_floor", "sampleOk": True,
                "relativeMicro": relative, "floorMicro": floor, "p995Micro": p995, "medianMicro": median,
                "fills": fills, "mode": mode, "multiple": mult,
                "rule": ("whale = max(%s, %s absolute floor) = %s: the relative term does not clear the floor"
                         % (term, _micro_str(floor), _micro_str(floor)))}
    return {"thresholdMicro": relative, "reason": "relative", "sampleOk": True,
            "relativeMicro": relative, "floorMicro": floor, "p995Micro": p995, "medianMicro": median,
            "fills": fills, "mode": mode, "multiple": mult,
            "rule": ("whale = max(%s, %s absolute floor) = %s: this market's own fills set the bar"
                     % (term, _micro_str(floor), _micro_str(relative)))}


def whale_severity(notional_micro: int, threshold_micro: int) -> dict:
    """How far past its own threshold a fill is, as a ratio in bps.

    A ratio, not a dollar amount: $5,000 is an ordinary fill in one market and the largest of the week in
    another, and severity has to mean the same thing on both rows of the same feed.
    """
    notional, threshold = _int(notional_micro, "notional_micro"), _int(threshold_micro, "threshold_micro")
    ratio = (notional * 10_000 // threshold) if threshold > 0 else 0
    if ratio >= SEVERITY_URGENT_BPS:
        sev = "urgent"
    elif ratio >= SEVERITY_NOTICE_BPS:
        sev = "notice"
    else:
        sev = "info"
    return {"severity": sev, "ratioBps": ratio,
            "rule": "severity is the fill's ratio to the threshold: urgent at 4×, notice at 1.5×"}


# --------------------------------------------------------------------------- a win rate, or a refusal
def win_rate(wins: int, settled: int) -> dict:
    """A win rate, or `None` with the reason it is not one.

    Below `SAMPLE_GATE` settled markets there is no rate to show: 3-for-4 is 75% and also 3-for-4. Returning
    `None` rather than a low-confidence number is the whole point — a percentage on screen is read as a
    percentage, and the disclaimer under it is not read at all.
    """
    wins, settled = _int(wins, "wins"), _int(settled, "settled")
    if settled < SAMPLE_GATE:
        return {"bps": None, "insufficientSample": True, "sampleGate": SAMPLE_GATE, "resolved": settled,
                "wins": wins,
                "reason": ("insufficient sample: %d settled markets; a win rate needs %d before it is a rate "
                           "and not a coin flip with a story" % (settled, SAMPLE_GATE))}
    return {"bps": wins * 10_000 // settled, "insufficientSample": False, "sampleGate": SAMPLE_GATE,
            "resolved": settled, "wins": wins, "reason": ""}


# --------------------------------------------------------------------------- PnL, and its drawdown
def drawdown_overlay(points: Sequence[dict]) -> list[dict]:
    """Add `peakMicro` and `drawdownMicro` to every point of a cumulative curve.

    The curve and its drawdown are built together because they cannot be separated: `peakMicro` is the running
    high-water mark (starting at 0, so a curve that never rises is entirely under water) and `drawdownMicro`
    is the distance below it. A caller who wants a PnL line therefore already has the overlay in hand, which is
    the structural version of "drawdown appears wherever PnL appears".
    """
    out, peak = [], 0
    for p in points:
        cum = _int(p.get("cumMicro"), "cumMicro")
        peak = max(peak, cum)
        row = dict(p)
        row["cumMicro"] = cum
        row["peakMicro"] = peak
        row["drawdownMicro"] = peak - cum
        out.append(row)
    return out


def max_drawdown_micro(points: Sequence[dict]) -> int:
    """The worst distance below the high-water mark, or 0 for a curve that only rises (and for no curve)."""
    rows = list(points or [])
    if rows and any("drawdownMicro" not in r for r in rows):
        rows = drawdown_overlay(rows)
    return max((_int(r.get("drawdownMicro")) for r in rows), default=0)


# --------------------------------------------------------------------------- holding, categories, positions
def hold_stats(fills: Sequence[dict]) -> dict:
    """Entry→exit spans, with open positions COUNTED rather than averaged in as zeros.

    A zero-hold entry that has not exited is not a fast trade; it is an unfinished one, and folding it into the
    average would make every trader look faster than they are.
    """
    spans, open_fills = [], 0
    for f in fills:
        entry = _int(f.get("entryMs"), "entryMs")
        exit_ms = f.get("exitMs")
        if exit_ms is None:
            open_fills += 1
            continue
        spans.append(max(0, _int(exit_ms, "exitMs") - entry))     # clamped: a negative span is clock skew
    if not spans:
        return {"avgHoldMs": 0, "medianHoldMs": 0, "matchedPositions": 0, "openFills": open_fills}
    return {"avgHoldMs": sum(spans) // len(spans), "medianHoldMs": median_micro(spans),
            "matchedPositions": len(spans), "openFills": open_fills}


def category_breakdown(rows: Sequence[dict]) -> list[dict]:
    """Notional and realised PnL per category, with shares in bps over the category TOTALS.

    The share denominator is the sum of |realised| across categories, not across fills: a per-fill denominator
    makes a book with two categories share 8,000 bps between them (a P10 finding, from a fixture with eight
    fills), and a breakdown whose parts do not add up to the whole is a breakdown nobody can check.
    """
    agg: dict[str, dict] = {}
    for r in rows:
        cat = (str(r.get("category") or "").strip() or "Uncategorised")
        d = agg.setdefault(cat, {"category": cat, "fills": 0, "notionalMicro": 0, "realisedMicro": 0})
        d["fills"] += 1
        d["notionalMicro"] += _int(r.get("notionalMicro"), "notionalMicro")
        d["realisedMicro"] += _int(r.get("realisedMicro"), "realisedMicro")
    out = sorted(agg.values(), key=lambda d: (-d["realisedMicro"], d["category"]))
    if not out:
        return []
    total = sum(abs(d["realisedMicro"]) for d in out)
    if total:
        shares, running = [], 0
        # Largest-remainder, so the shares of any book sum to exactly 10,000 bps. Integer division alone can
        # leave a few bps on the floor, and a breakdown that sums to 9,997 looks like a rounding bug to the one
        # person who adds it up - which is the person the breakdown is for.
        for d in out:
            exact = abs(d["realisedMicro"]) * 10_000
            share = exact // total
            shares.append([share, exact % total, d["category"]])
            running += share
        for row in sorted(shares, key=lambda s: (-s[1], s[2]))[:max(0, 10_000 - running)]:
            row[0] += 1
        for d, (share, _rem, _cat) in zip(out, shares):
            d["shareBps"] = share
    else:
        for d in out:
            d["shareBps"] = 10_000 // len(out)
    return out


def portfolio_row(*, size_micro: int, avg_entry_micro: int, mark_micro: int, tick_micro: int,
                  ends_in_ms: int, cost_basis_micro: int) -> dict:
    """One position, marked at the last price we hold. Unrealised is (mark − entry) x size, floored.

    `tick_micro` comes along so a mark that is off the tick can be labelled as such rather than printed as a
    price somebody could have traded: an off-tick mark is a stale book's number wearing a price's clothes.
    `cost_basis_micro` may be 0, in which case it is derived from size x entry - a caller with a real cost
    basis (fees included) passes it and gets the honest number.
    """
    size, entry, mark = _int(size_micro, "size_micro"), _int(avg_entry_micro, "avg_entry_micro"), \
        _int(mark_micro, "mark_micro")
    tick = _int(tick_micro, "tick_micro")
    cost = _int(cost_basis_micro, "cost_basis_micro") or (size * entry // MICRO)
    value = size * mark // MICRO
    unrealised = value - cost
    return {"sizeMicro": size, "avgEntryMicro": entry, "markMicro": mark, "tickMicro": tick,
            "costBasisMicro": cost, "valueMicro": value, "unrealisedMicro": unrealised,
            "unrealisedBps": (unrealised * 10_000 // cost) if cost else 0,
            "onTick": tick <= 0 or mark % tick == 0,
            "endsInMs": _int(ends_in_ms, "ends_in_ms")}


def neg_risk_group(rows: Sequence[dict], *, event_id: str, event_title: str) -> dict:
    """Event-level exposure for a negRisk event, where exactly one outcome pays.

    The ceiling is the BEST SINGLE LEG, not the sum of the legs' quoted values. Summing them shows a payout
    that cannot happen - the same class of error as a PnL curve drawn without its drawdown, and the reason the
    `note` travels with the number.
    """
    legs = list(rows or [])
    value = sum(_int(r.get("valueMicro")) for r in legs)
    cost = sum(_int(r.get("costBasisMicro")) for r in legs)
    return {"eventId": str(event_id), "eventTitle": str(event_title), "legs": len(legs),
            "valueMicro": value, "costBasisMicro": cost, "unrealisedMicro": value - cost,
            "maxPayoutMicro": max((_int(r.get("sizeMicro")) for r in legs), default=0),
            "note": ("only one outcome in this event pays, so the ceiling is the best single leg - not the sum "
                     "of every leg's quoted value")}


# --------------------------------------------------------------------------- copy trading's honest bit
def copy_slippage_warning(*, deviations_bps: Sequence[int], copied: int, skipped: int,
                          fill_micros: Sequence[int] = ()) -> dict:
    """What copying this source has actually cost US, with the default stated in the payload.

    The default is `skip_instead_of_chase` and it is not a preference: a copier who chases turns a good source's
    record into a bad copier's result, and the fee arithmetic only works if the fill is near the source's. The
    warning quotes our own measurements - median, p90 and worst - because the question "is this worth it" is
    answered by the distribution, not by the mean.
    """
    devs = _ints(deviations_bps, "deviations_bps")
    copied, skipped = _int(copied, "copied"), _int(skipped, "skipped")
    total = copied + skipped
    if not devs:
        warning = ("we have copied this source 0 times, so there is no slippage history to show; that is not "
                   "the same as zero slippage. The default policy is to skip instead of chase.")
    else:
        warning = ("we copied this source %d times and skipped %d, landing a median slippage of %d bps "
                   "adverse to the source's price (worst %d bps). The default policy is to skip instead of "
                   "chase: a copier who chases turns a good source's record into a bad copier's result."
                   % (copied, skipped, median_micro(devs), max(devs)))
    return {"samples": len(devs), "medianSlippageBps": median_micro(devs),
            "p90SlippageBps": percentile_micro(devs, 90, 100), "worstSlippageBps": max(devs, default=0),
            "copied": copied, "skipped": skipped,
            "skipRateBps": (skipped * 10_000 // total) if total else 0,
            "medianFillMicro": median_micro(list(fill_micros)) if fill_micros else 0,
            "default": "skip_instead_of_chase", "warning": warning}


# --------------------------------------------------------------------------- formatting (integers only)
def _micro_str(micro: int) -> str:
    """Micro-USDC as `$1.23`, floored toward zero, no float anywhere in the conversion.

    Only for sentences and tooltips. Anything a user reads as a number goes through `/web/src/money/cents.ts`,
    because a formatted string cannot be added up and a screen that adds up strings is the bug this module is
    written to avoid.
    """
    v = _int(micro, "micro")
    sign = "-" if v < 0 else ""
    a = abs(v)
    return "%s$%d.%02d" % (sign, a // MICRO, (a % MICRO) // 10_000)


def bps_str(bps: int) -> str:
    """Basis points as `62.5%`, truncated at one decimal. Truncated, not rounded: a win rate that rounds up is
    a win rate that flatters."""
    v = _int(bps, "bps")
    sign = "-" if v < 0 else ""
    a = abs(v)
    return "%s%d.%d%%" % (sign, a // 100, (a % 100) // 10)
