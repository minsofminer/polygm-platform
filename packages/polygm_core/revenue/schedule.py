"""D6: the builder-fee ramp, and the venue mechanics that make it slow on purpose.

The kit's sequencing is the whole point, and it is a sequence we cannot rush even if we wanted to:

1. **Launch at 0 bps.** Betmoar and Stand.trade both charge nothing and sit in the top five. At this stage we are
   buying volume share, not margin, and the leaderboard position is the moat.
2. **Raise only after retention holds.** A fee raised on an audience that is not yet returning is a fee raised on
   an audience that is about to leave.
3. **Raise in steps, inside 10-25 bps, and obey the venue.** One change per seven days, three days of advance
   notice, one pending change at a time, and the rate is publicly queryable - so a rate decision is a public
   product decision made a week before it takes effect.

This module is the part of that plan a machine can hold us to. It is pure: no database, no clock of its own. It
answers two questions and refuses to answer a third:

* what is the rate *now* (`current_bps`), from the step list and a timestamp;
* is this proposed change legal (`review`), against the venue mechanics and the retention rule;
* and it deliberately does **not** decide whether retention holds - that is a fact about the business, and a
  module that guessed it would be a module that could raise fees by itself.

Two asymmetries are intentional and worth stating out loud, because they look like bugs otherwise:

* the retention requirement gates **increases only**. A fee cut is the one rate change that is never against the
  user's interest, and refusing to let us cut quickly would be perverse.
* the venue mechanics gate **everything**, including cuts. We do not control them; the venue does, and a cut
  applied a day early is exactly as invalid as a raise applied a day early.

The default steps are read from `config/gtm.json` (the same file `tools/p16-gtm-check.py` reads) so the ramp cannot
exist in two versions. `load_steps()` is the only I/O in this module, and it is on the edge.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parents[3]
GTM_PATH = ROOT / "config" / "gtm.json"

#: The kit's ceiling: 10-25 bps. A rate above this is not a business decision, it is a migration to a competitor.
MAX_BPS = 2500
#: Launch rate, from the same sequencing: zero until retention holds.
LAUNCH_BPS = 0

DAY_MS = 86_400_000


@dataclass(frozen=True)
class Step:
    """One point on the ramp. `at_day` is days from launch, which is the only clock that matters here."""

    id: str
    at_day: int
    bps: int
    requires: str = ""
    why: str = ""


@dataclass(frozen=True)
class Change:
    """A rate change, as the venue sees it: what rate, when it takes effect, and when it was announced."""

    bps: int
    effective_ms: int
    announced_ms: int
    note: str = ""


def load_steps(path: pathlib.Path | None = None) -> list[Step]:
    """The ramp, from `config/gtm.json`. Sorted by day; the file's order is not load-bearing."""
    raw = json.loads((path or GTM_PATH).read_text())
    steps = [Step(id=s["id"], at_day=int(s["at_day"]), bps=int(s["bps"]), requires=s.get("requires", ""),
                  why=" ".join((s.get("why") or "").split())) for s in raw["monetisation"]["steps"]]
    return sorted(steps, key=lambda s: s.at_day)


def load_mechanics(path: pathlib.Path | None = None) -> dict:
    raw = json.loads((path or GTM_PATH).read_text())
    return raw["monetisation"]["venue_mechanics"]


def current_bps(steps: list[Step], now_ms: int, launch_ms: int) -> int:
    """The rate that should be in force at `now_ms`. Before launch there is no rate: the code is not registered
    for the public yet, and answering 0 would imply we were taking nothing from live orders."""
    if now_ms < launch_ms:
        return LAUNCH_BPS
    day = (now_ms - launch_ms) // DAY_MS
    active = [s for s in steps if s.at_day <= day]
    return max(active, key=lambda s: s.at_day).bps if active else LAUNCH_BPS


def reached(steps: list[Step], now_ms: int, launch_ms: int) -> Step | None:
    """The step whose `at_day` has arrived - i.e. the next scheduled change, if any has come due."""
    day = (now_ms - launch_ms) // DAY_MS
    due = [s for s in steps if s.at_day <= day]
    return max(due, key=lambda s: s.at_day) if due else None


def review(change: Change, *, history: list[Change], retention_ok: bool,
           mechanics: dict | None = None, now_ms: int | None = None) -> list[str]:
    """Why this change may not happen, as a list of reasons (empty means it may).

    `history` is what the venue has already applied, newest last - not what we intended. A change that is announced
    but never applied is not history, which is why `pending` is separate.
    """
    m = mechanics or load_mechanics()
    reasons: list[str] = []
    now = change.announced_ms if now_ms is None else now_ms

    if change.bps < 0:
        reasons.append("a negative rate is us paying the venue per trade; if that is the intent, it is a rebate "
                       "programme and needs its own design, not a fee schedule")
    if change.bps > MAX_BPS:
        reasons.append("rate %d bps is above the kit's ceiling of %d bps - at that point the fee is a reason not "
                       "to route through us, and the builder programme's value proposition inverts"
                       % (change.bps, MAX_BPS))

    applied = sorted(history, key=lambda c: c.effective_ms)
    if applied:
        last = applied[-1]
        days_since = (change.effective_ms - last.effective_ms) / DAY_MS
        if days_since < m["min_days_between_changes"]:
            reasons.append("the venue allows one change per %d days; this one lands %.1f days after the last"
                           % (m["min_days_between_changes"], days_since))
        pending = [c for c in applied if c.effective_ms > now]
        if m.get("one_pending_change_at_a_time") and pending and change.effective_ms > now:
            reasons.append("%d change(s) are already announced and not yet effective (%s); the venue allows one "
                           "pending change at a time" % (len(pending), ", ".join(str(c.bps) for c in pending)))

    notice_days = (change.effective_ms - change.announced_ms) / DAY_MS
    if notice_days < m["advance_notice_days"]:
        reasons.append("the venue requires %d days of advance notice; this announces %.1f days ahead"
                       % (m["advance_notice_days"], notice_days))

    # The retention rule applies to increases only. A cut is the one rate change that is never against the user's
    # interest; requiring a retention gate to lower a fee would be perverse, and the asymmetry is deliberate.
    current = applied[-1].bps if applied else LAUNCH_BPS
    if change.bps > current and not retention_ok:
        reasons.append("a rate increase requires retention to hold first (the ramp's own condition); retention "
                       "does not hold yet, so the honest options are to hold the rate or to cut it")
    return reasons


def describe(steps: list[Step], launch_ms: int, now_ms: int) -> str:
    """One line for a human: where we are on the ramp and what the next step needs."""
    day = max(0, (now_ms - launch_ms) // DAY_MS)
    bps = current_bps(steps, now_ms, launch_ms)
    nxt = next((s for s in steps if s.at_day > day), None)
    if nxt is None:
        return "day %d: %d bps (the ramp is complete)" % (day, bps)
    return "day %d: %d bps; next step %s at day %d is %d bps and requires: %s" % (
        day, bps, nxt.id, nxt.at_day, nxt.bps, nxt.requires or "nothing stated")


def validate_steps(steps: list[Step], mechanics: dict | None = None) -> list[str]:
    """Is the ramp itself legal? Runs in the gate check on every commit, so an illegal step list fails a build
    rather than a rate change at the venue."""
    m = mechanics or load_mechanics()
    problems: list[str] = []
    if not steps:
        return ["the ramp is empty - a plan with no fee steps has no fee plan"]
    if steps[0].bps != LAUNCH_BPS:
        problems.append("the first step is %d bps; the kit's sequence says launch at %d" % (steps[0].bps, LAUNCH_BPS))
    for prev, cur in zip(steps, steps[1:]):
        gap = cur.at_day - prev.at_day
        if gap < m["min_days_between_changes"]:
            problems.append("%s -> %s is %d days apart; the venue allows one change per %d days"
                            % (prev.id, cur.id, gap, m["min_days_between_changes"]))
        if cur.bps < prev.bps:
            problems.append("%s lowers the fee from %d to %d bps: legal, but a ramp that goes down needs the "
                            "reason written down or it reads as a mistake" % (cur.id, prev.bps, cur.bps))
    if max(s.bps for s in steps) > MAX_BPS:
        problems.append("the ramp reaches %d bps, above the %d bps ceiling" % (max(s.bps for s in steps), MAX_BPS))
    for s in steps[1:]:
        if not s.requires:
            problems.append("%s raises the rate with no stated condition; the ramp's rule is that a rise requires "
                            "retention to hold" % s.id)
    return problems
