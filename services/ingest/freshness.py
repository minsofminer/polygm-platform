"""Which of these four things is happening: quiet market, slow market, dead pipe, dead process.

They look identical from a dashboard, and only two of them should page a human. This module is the answer to
"you have been paged at 3am by a silent WebSocket and you design so that never happens again": the design is
that a source must *earn* the claim "nothing happened" by producing a heartbeat, and every number the UI shows
carries the reason it is stale.

Thresholds come from `polygm_core.config.flags` (stale_ms_book / _tape / _metadata / _positions), which P01's
measurements set: book and tape at 3 s, metadata at 120 s. The alarm thresholds below are the second and third
tier of the same ladder (>3 warn, >10 critical, >30 page) so an operator and a user see the same story.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

WARN_MS, CRIT_MS, PAGE_MS = 10_000, 30_000, 300_000


@dataclass
class Source:
    name: str                     # "ws.tape", "ws.book", "gamma.markets", "clob.history", ...
    kind: str                     # "book" | "tape" | "metadata" | "positions" -> which flag threshold applies
    stale_ms: int
    transport_alive: bool = False  # socket open / last HTTP call succeeded
    last_event_ms: int = 0         # newest EVENT timestamp (venue clock)
    last_frame_at: float = 0.0     # last time ANY bytes arrived (our clock), including a pong
    last_poll_ok_at: float = 0.0
    events: int = 0
    heartbeats: int = 0
    resyncs: int = 0
    notes: list = field(default_factory=list)

    def frame_age_ms(self, now_mono: float | None = None) -> int | None:
        """None means "no frame has ever been seen", which is worse than any number, and is therefore NOT 0.

        A negative age (a caller that stamped `last_frame_at` from a different clock than the one it passes as
        `now_mono` — the classic is a monotonic value persisted to a DB and read back after a reboot) returns
        "enormous" rather than "fresh". The direction of the lie is the whole decision: a source that reads as
        perfectly fresh will place orders, and a source that reads as dead will only ever page somebody.
        """
        if not self.last_frame_at:
            return None
        age = ((time.monotonic() if now_mono is None else now_mono) - self.last_frame_at) * 1000
        return int(age) if age >= 0 else 10 ** 15

    def event_lag_ms(self, now_ms: int) -> int:
        if not self.last_event_ms:
            return 10 ** 15
        return max(0, now_ms - self.last_event_ms)

    def state(self, now_ms: int, now_mono: float | None = None) -> str:
        """down > silent > stale > lagging > ok, and the ORDER is the point: a source that is down must not be
        reported as merely lagging, because that is the message that lets an incident sit unanswered."""
        if not self.transport_alive:
            return "down"
        age = self.frame_age_ms(now_mono)
        if age is None or age > PAGE_MS:
            return "silent"          # socket open, nothing arriving: the classic silent death
        if self.event_lag_ms(now_ms) > max(self.stale_ms, CRIT_MS):
            return "stale"
        if self.event_lag_ms(now_ms) > self.stale_ms:
            return "lagging"
        return "ok"

    def user_message(self, st: str) -> str | None:
        """What the UI says. A user-facing string never says "websocket": it says what they should conclude."""
        return {
            "down": "live data is disconnected; prices shown are the last received",
            "silent": "the feed stopped sending; prices shown may be minutes old",
            "stale": "data is more than %ds old" % (self.stale_ms // 1000),
            "lagging": "data is behind by a few seconds",
        }.get(st)


@dataclass
class Freshness:
    """All sources in one place, because the composite is what the UI and /readyz need: a page that shows a
    fresh book next to a dead tape is a page that will be believed about the wrong thing."""
    sources: dict[str, Source] = field(default_factory=dict)

    def add(self, src: Source) -> None:
        self.sources[src.name] = src

    def note_event(self, name: str, ts_ms: int, *, now_mono: float | None = None, heartbeat: bool = False) -> None:
        s = self.sources[name]
        s.last_frame_at = time.monotonic() if now_mono is None else now_mono
        if heartbeat:
            s.heartbeats += 1
            return                     # a pong is liveness, not news: it must not advance last_event_ms
        s.events += 1
        s.last_event_ms = max(s.last_event_ms, ts_ms)

    def composite(self, now_ms: int, now_mono: float | None = None) -> dict:
        order = {"down": 4, "silent": 3, "stale": 2, "lagging": 1, "ok": 0}
        per = {n: s.state(now_ms, now_mono) for n, s in self.sources.items()}
        worst = max(per.items(), key=lambda kv: order[kv[1]])[1] if per else "down"
        return {"status": worst, "sources": per,
                "stale": any(v != "ok" for v in per.values()),
                "worst_reason": next((self.sources[n].user_message(v) for n, v in per.items() if v != "ok"), None),
                "lags_ms": {n: (None if s.event_lag_ms(now_ms) > 10 ** 14 else s.event_lag_ms(now_ms))
                            for n, s in self.sources.items()}}

    def pageable(self, now_ms: int, now_mono: float | None = None) -> list[str]:
        """Only these wake a person. A lagging source is a UI message; a silent one is an outage."""
        return [n for n, s in self.sources.items() if s.state(now_ms, now_mono) in ("down", "silent")]

    def drop_order(self, priorities: dict[str, int]) -> list[str]:
        """Backpressure: what to stop tracking first. Lower number = keep longer (0 watchlist, 1 alert, 2 topN,
        3 long tail). Sorted descending so the tail is first out; ties broken by name so a restart replays the
        same decisions and an operator can diff two runs."""
        return [t for t, _p in sorted(priorities.items(), key=lambda kv: (-kv[1], kv[0]))]
