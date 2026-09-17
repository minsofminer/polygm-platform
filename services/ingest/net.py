"""The only way ingest talks to the outside world: metered, timed, breakered.

Three rules, and they are the reason this module exists rather than a `urllib.request.urlopen` at each call
site (P05 constraint: "no polling loop without a limit-aware token bucket — name the bucket and its rate for
every source"):

1. A request that is not allowed by its bucket WAITS; it does not fire anyway. The venue's limits are per-IP
   and per-signer, so one hot market must not be able to spend the whole company's budget.
2. Every call carries an explicit connect+read timeout. A socket that stalls is a stalled ingest process,
   and a stalled ingest process looks exactly like a quiet market from the outside (see freshness.py, whose
   entire job is telling those apart).
3. Retries are per-source and small, with the jittered exponential the P01 probe settled on (500 ms x2 then a
   30 s ceiling, +/-20 % jitter). A POST that changes state is never retried here, and a submit is never
   retried at all — that rule lives in the executor, and this module is not it.

No third-party imports: this runs in a container whose whole dependency set is the standard library plus
FastAPI for the API image, and "one less thing to audit" is a security property, not a preference.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_TIMEOUT_S = (1.0, 3.0)          # (connect, read); per-source overrides are declared by the caller
BREAKER_FAILURES_OPEN = 5               # consecutive failures before the source is declared dead
BREAKER_COOLDOWN_S = 30.0               # how long it stays open before a trial request is allowed
RETRY_BASE_MS = 500
RETRY_FACTOR = 2
RETRY_CEILING_MS = 30_000
JITTER = 0.20


@dataclass
class Bucket:
    """A token bucket: `capacity` burst, `per_sec` sustained. Named by the source it guards.

    `wait()` blocks, deliberately. A queue of unsent requests would be worse for the venue than a slow
    consumer, and it would let a burst scheduled minutes ago still hit them at full speed.
    """
    name: str
    capacity: float
    per_sec: float
    tokens: float = field(init=False)
    last: float = field(init=False, default_factory=time.monotonic)
    waits: int = 0
    slept_s: float = 0.0

    def __post_init__(self) -> None:
        self.tokens = float(self.capacity)

    def _fill(self, now: float) -> None:
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.per_sec)
        self.last = now

    def reserve(self, now: float | None = None) -> float:
        """Seconds this caller must wait before it may send. 0 means go now."""
        now = time.monotonic() if now is None else now
        self._fill(now)
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        self.waits += 1
        need = (1 - self.tokens) / self.per_sec
        self.tokens = 0.0                    # consumed by the sleep, not by this bookkeeping
        return need

    def wait(self, now: float | None = None) -> float:
        d = self.reserve(now)
        if d:
            self.slept_s += d
            time.sleep(d)
        return d


# The buckets, named, with the rate each source was measured to allow (docs/P01-product-spec.md, re-verified
# by `make probe-fresh`). `capacity` is the burst the docs grant; `per_sec` is what WE spend, which is always
# below the venue's cap: the headroom is for the endpoint we have not written yet, and for retries.
BUCKETS: dict[str, Bucket] = {
    "gamma.markets":  Bucket("gamma.markets",  capacity=100, per_sec=20),    # cap 300/10s; we use <1/3
    "gamma.events":   Bucket("gamma.events",   capacity=100, per_sec=20),
    "gamma.tags":     Bucket("gamma.tags",     capacity=50,  per_sec=1),
    "clob.book":      Bucket("clob.book",      capacity=200, per_sec=60),
    "clob.history":   Bucket("clob.history",   capacity=20,  per_sec=2),
    "data.trades":    Bucket("data.trades",    capacity=60,  per_sec=5),     # edge-cached: a poll is cheap for
    "data.activity":  Bucket("data.activity",  capacity=60,  per_sec=5),     # them and expensive for us
    "lb.volume":      Bucket("lb.volume",      capacity=20,  per_sec=1),
}


class CircuitOpen(Exception):
    """Raised instead of sending: the source has failed BREAKER_FAILURES_OPEN times in a row."""

    def __init__(self, source: str, retry_after: float):
        super().__init__("breaker open for %s; retry in %.1fs" % (source, retry_after))
        self.source, self.retry_after = source, retry_after


@dataclass
class Breaker:
    source: str
    failures: int = 0
    opened_at: float | None = None
    trip_log: list[tuple[float, str]] = field(default_factory=list)

    def check(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self.opened_at is not None:
            left = BREAKER_COOLDOWN_S - (now - self.opened_at)
            if left > 0:
                raise CircuitOpen(self.source, left)
            self.opened_at = None                     # half-open: let exactly one probe through

    def outcome(self, ok: bool, why: str = "", now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if ok:
            if self.failures >= BREAKER_FAILURES_OPEN:
                self.trip_log.append((now, "closed after %d-failure episode" % self.failures))
            self.failures, self.opened_at = 0, None
            return
        self.failures += 1
        if self.failures >= BREAKER_FAILURES_OPEN and self.opened_at is None:
            self.opened_at = now
            self.trip_log.append((now, why or "failure"))


def backoff_ms(attempt: int) -> int:
    """500 ms, 1000 ms, ... capped at 30 s, each +/-20 % jitter. `attempt` is 0-based."""
    ms = min(RETRY_CEILING_MS, RETRY_BASE_MS * (RETRY_FACTOR ** attempt))
    return int(ms * (1 + random.uniform(-JITTER, JITTER)))


@dataclass
class Response:
    status: int
    json: object
    headers: dict
    ms: float
    from_cache: bool
    attempts: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300


class Fetcher:
    """One place that knows how to be polite to a server. Everything else in ingest goes through `get`."""

    def __init__(self, buckets: dict[str, Bucket] | None = None, breakers: dict[str, Breaker] | None = None,
                 transport=None, sleep=time.sleep, clock=time.monotonic):
        self.buckets = buckets if buckets is not None else BUCKETS
        self.breakers = breakers if breakers is not None else {}
        self.transport = transport or self._urllib
        self._sleep, self._clock = sleep, clock
        self.sends = 0
        self.retry_count = 0

    def breaker(self, source: str) -> Breaker:
        return self.breakers.setdefault(source, Breaker(source))

    def get(self, url: str, *, source: str, timeout: tuple[float, float] = DEFAULT_TIMEOUT_S,
            retries: int = 2, params: dict | None = None) -> Response:
        b = self.buckets.get(source)
        br = self.breaker(source)
        if b is None:
            # A source with no bucket is the one bug that can exhaust the whole company's budget, so it is an
            # error here rather than a default. Add the source to BUCKETS with a rate you can defend.
            raise KeyError("no token bucket for source %r — refusing to poll unbounded" % source)
        qs = ("&" if "?" in url else "?") + "&".join("%s=%s" % (k, v) for k, v in (params or {}).items()) \
            if params else ""
        full = url + qs
        last: Response | None = None
        for attempt in range(retries + 1):
            br.check(self._clock())
            if attempt:
                self._sleep(backoff_ms(attempt - 1) / 1000.0)
                self.retry_count += 1
            b.wait(self._clock())
            self.sends += 1
            try:
                status, payload, headers, ms = self.transport(full, timeout)
            except Exception as e:                                    # noqa: BLE001 - transport shapes vary
                br.outcome(False, "%s: %s" % (type(e).__name__, str(e)[:80]), self._clock())
                last = Response(0, None, {}, 0.0, False, attempt + 1, error="%s: %s" % (type(e).__name__,
                                                                                          str(e)[:160]))
                continue
            cf = str(headers.get("cf-cache-status") or headers.get("CF-Cache-Status") or "")
            br.outcome(status < 500, "HTTP %d" % status, self._clock())
            r = Response(status, payload, headers, ms, cf.upper() in ("HIT", "STALE"), attempt + 1)
            if status == 429 or status >= 500:
                last = r                                              # retryable; the loop decides
                continue
            return r
        return last if last is not None else Response(0, None, {}, 0.0, False, retries + 1, error="no attempt")

    @staticmethod
    def _urllib(url: str, timeout: tuple[float, float]) -> tuple[int, object, dict, float]:
        req = urllib.request.Request(url, headers={"user-agent": "openout-ingest/0.1 (+ops contact in "
                                                                   "docs/P04-backend-architecture.md)"})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout[1]) as r:     # read timeout; see note above
                body = r.read().decode("utf-8", "replace")
                headers = {k.lower(): v for k, v in dict(r.headers).items()}
                return r.status, (json.loads(body) if body.strip()[:1] in "[{" else body), headers, \
                    (time.monotonic() - t0) * 1000
        except urllib.error.HTTPError as e:
            return e.code, None, {k.lower(): v for k, v in dict(e.headers or {}).items()}, \
                (time.monotonic() - t0) * 1000
