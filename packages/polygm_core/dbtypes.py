"""How the same column arrives on the two engines, and what we do about it.

`markets.end_ts` is `TIMESTAMPTZ` in Postgres and `INTEGER` (epoch **milliseconds**) in the dev SQLite twin —
that is the transpiler's documented, deliberate mapping (a `timestamptz` has no lossless SQLite type, and
milliseconds is what our own clocks speak). The consequence is that any code reading a time column gets back
a `datetime` on one engine and an `int` on the other, and an ISO string from the venue on a third path.

Guessing the unit of a timestamp is the same class of bug as guessing the unit of money, and it is quieter: a
60x error in `seconds_to_resolution` does not throw, it just makes the "never copy into a market about to
resolve" rule decide at the wrong time — usually *in favour* of trading, which is the direction that costs
money. So every read goes through `to_epoch_ms`, which refuses what it cannot place inside a plausible window.
"""
from __future__ import annotations

import datetime as _dt

# A "now" that is not between these is a unit mistake, not a strange market. 2001-09-09 is the Unix epoch
# written in a 32-bit year field; 2100 is past any market we will list. Both ends are generous on purpose:
# the check exists to catch a *scale* error (x1000 or /1000), and every scale error moves a real value outside
# this band, while no legitimate market's end date does.
PLAUSIBLE_MIN_MS = 1_000_000_000_000
PLAUSIBLE_MAX_MS = 4_102_444_800_000


class TimeColumnError(ValueError):
    """The value in a time column is not interpretable. Refuse; do not assume seconds."""


def to_epoch_ms(value, *, column: str = "time") -> int | None:
    """Normalise a datetime / ISO string / epoch seconds / epoch milliseconds into epoch **milliseconds**.

    Returns None only for NULL, which callers must treat as *unknown* — not as "no deadline", not as zero. A
    missing `end_ts` on a market we are about to copy into means "we cannot see the clock", and the safe
    reading of that is the one the engines already use: skip.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise TimeColumnError("%s is a boolean, not a timestamp" % column)
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        return int(value.timestamp() * 1000)
    if isinstance(value, _dt.date):
        return int(_dt.datetime(value.year, value.month, value.day,
                                tzinfo=_dt.timezone.utc).timestamp() * 1000)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return to_epoch_ms(_dt.datetime.fromisoformat(text), column=column)
        except ValueError:
            # Some feeds hand back a bare number in a text column. That is a timestamp with lost typing, not a
            # different kind of value, so it goes back through the numeric path below rather than being
            # rejected here — where the plausible-window check can still catch a scale error.
            try:
                value = int(float(text))
            except ValueError:
                raise TimeColumnError("%s is not a timestamp: %r" % (column, value[:40])) from None
    if isinstance(value, (int, float)):
        v = float(value)
        if v < 0:
            raise TimeColumnError("%s is negative (%r)" % (column, value))
        # One magnitude of slack between the two readings is impossible, so the band decides: anything above
        # 4.1e12 cannot be seconds (that is the year 130,000), and anything below 1e9 cannot be milliseconds.
        if v >= 1e12:
            ms = int(round(v))
        elif v >= 1e9:
            ms = int(round(v * 1000))
        else:
            raise TimeColumnError("%s=%r is too small to be a wall-clock timestamp in either unit; "
                                  "if a market really ends here, it ended before this product existed"
                                  % (column, value))
        if not (PLAUSIBLE_MIN_MS <= ms <= PLAUSIBLE_MAX_MS):
            raise TimeColumnError("%s=%r normalises to %d ms, outside the plausible window — a unit mistake, "
                                  "most likely seconds passed where milliseconds were stored"
                                  % (column, value, ms))
        return ms
    raise TimeColumnError("%s is of unsupported type %s" % (column, type(value).__name__))


def seconds_until(value, *, now_ms: int, column: str = "time") -> int | None:
    """Whole seconds from `now_ms` until `value`. Negative means it already passed, which is a fact the
    caller needs (an ended market is not an unbounded one), so it is not clamped to zero here."""
    ms = to_epoch_ms(value, column=column)
    if ms is None:
        return None
    return (ms - now_ms) // 1000
