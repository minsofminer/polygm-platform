"""The referral terms: what qualifies, what it pays, when it is paid, and the state machine in between.

This is the module the served `/v1/referrals/terms` page and the accrual engine both read, for the same reason the
leaderboard's methodology is served from `leaderboard.boards`: a rate card maintained by hand beside an engine
maintained in code is two authorities over one number, and the first disagreement is the one a referrer
screenshots.

**Integer micro-dollars throughout.** `share_of` floors, deliberately: rounding a share up pays out a micro-unit
that was never collected, and the sum of many rounded-up fractions is a liability that grows with volume. The
flooring rule is published, so a referrer who checks our arithmetic finds the same answer we do.

**A referral is one row per referee, for ever.** `referral_attributions` is keyed by the referee, an attribution
is never edited into a different referrer, and a referee who arrives twice is still one referee. That single
choice is what makes the clawback rule enforceable at all: "claw back the referral" needs a referral to point at.
"""
from __future__ import annotations

#: The first MATCHED order that makes the referral live. Justification, with the distribution it came from: the
#: seeded tape's median fill is $0.02 and its p90 is $137.50 [MEASURED: db/seed.sql, 1,090 fills], and the venue's
#: minimum order is a couple of dollars. $25 is therefore ~25x the median fill — an order an attacker has to mean —
#: while staying below a quarter of p90, so a genuine first trade clears it. It is a rate limit on claims, not the
#: anti-farm mechanism (see the package docstring: the fee share is). Re-derive on production fills before launch.
QUALIFY_NOTIONAL_MICRO = 25 * 1_000_000

#: The referrer's cut of the builder fee we are OBSERVED to have been paid on the referee's own attributable
#: fills — not of the referee's cost, and not of what we hoped to be paid. 25% is the copy-source rate the venue's
#: own affiliate programmes sit around [CTX: the kit's cited comparable], and it is low enough that the
#: attacker-pays-more-than-they-earn property in the package docstring holds with room to spare.
SHARE_BPS = 2_500

#: How long accrual runs after qualification. A year is long enough that a referrer who brings a real trader is
#: paid for the relationship rather than for the introduction, and short enough to bound the tail. Counted from
#: the QUALIFYING ORDER, not from signup: signup is free, and a term that starts at signup lets a farm stop the
#: clock by never trading.
TERM_DAYS = 365

#: A day's accrual is not payable for this long, so a fill that is reversed, disputed or re-attributed by the
#: venue inside the window is not money we have already sent. The window is the same 30 days the leaderboard's
#: longest board window uses, which is not a coincidence: both are "the period inside which the venue's own record
#: is still moving".
SETTLE_HOLD_DAYS = 30

#: Below this, a payout costs more to send than it pays. Accruals are NOT forfeited — they carry forward, and the
#: dashboard shows the amount still to go (`payable()` returns `to_minimum_micro`). A program that quietly drops
#: small balances is a program whose smallest referrers are the product.
PAYOUT_MIN_MICRO = 20 * 1_000_000

#: One referrer's month above this is reviewed by a human before it is paid, not after. The kit asks for a manual
#: review queue "above a threshold"; this is that threshold, and it is also the number that makes an anomalous
#: month visible without a rule that has to guess what "anomalous" means.
REVIEW_THRESHOLD_MICRO = 2_000 * 1_000_000

#: Below this, a detected clawback is written off rather than invoiced. A $3 invoice costs more than $3 to
#: collect, and the published rule says so, because a clawback nobody can predict the size of is a punishment.
CLAWBACK_MIN_MICRO = 5 * 1_000_000

#: The US reporting threshold for the form we file (1099-NEC). Stated on the payout page as a fact about us, not
#: as tax advice; `tax_requirement()` carries the wording, and the jurisdiction review is a launch item.
TAX_REPORT_THRESHOLD_MICRO = 600 * 1_000_000

#: The funnel, in order. Each step is a subset of the one before it, and `funnel_findings()` refuses a dashboard
#: that reports otherwise. **The money chain, not the click chain**: clicks are a leading counter, and a short code
#: said aloud on a stream produces signups with no click at all — an invariant that made clicks a ceiling on
#: signups would fire on a legitimate referrer, which is how a check gets switched off instead of fixed.
FUNNEL = ("signups", "funded", "trading", "earned")

#: Counted and displayed, never used as a ceiling. See FUNNEL.
LEADING = ("clicks",)

#: What each funnel step means, because "funded" and "trading" are two different claims about a stranger.
FUNNEL_MEANING = {
    "clicks": "hits on the link, counted from the click table; a shouted code produces signups without a click",
    "signups": "accounts attributed to this referrer (one row per referee, ever)",
    "funded": "referees whose first matched order cleared the notional threshold",
    "trading": "referees that have generated at least one accrual — a fee we were actually paid",
    "earned": "referees whose accruals are still owed after any clawback",
}

#: A referral's life. `review` is not a failure: it is "a human has not looked yet", and it is the state the kit's
#: manual queue reads. `refused` is terminal and always carries a reason.
STATES = ("pending", "qualified", "review", "refused", "expired", "clawed_back")

#: The states a referral can be paid from, and the states it cannot. Kept here rather than inline so the accrual
#: engine and the dashboard cannot disagree about whether `review` earns.
ACCRUING_STATES = ("qualified",)

PUBLISHED_RULES = (
    "A referral pays a share of the builder fee we are paid on the referee's own orders, for one year from the "
    "referee's first matched order of $25 or more. Signups, deposits and withdrawals pay nothing.",
    "Nothing is paid for recruiting a recruiter. There is no second level and no recruitment bonus.",
    "Multiple wallets funded from the same source are one person: a second referral from the same funding source "
    "is refused, and the first is clawed back if the collision is found later.",
    "A referral made by an account to itself is refused, and is a ground for revoking the builder code it was "
    "made under — it is a revenue-integrity matter, not a referral-budget matter.",
    "Accruals become payable 30 days after the day they accrue, monthly, once the balance is at least $20. The "
    "balance carries forward; it is never forfeited.",
    "A month above $2,000 for one referrer is reviewed by a person before it is paid.",
    "Detected abuse claws back unpaid accruals first, then what was already paid. Under $5 is written off, and "
    "this rule is published so a clawback is not a surprise.",
)


def _int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be an integer, got %r" % (field, value))
    return value


# ----------------------------------------------------------------------------------- does an order qualify
def qualifies(*, matched: bool, notional_micro: int, excluded_reason: str = "", self_cross: bool = False,
              at_ms: int = 0) -> tuple[bool, str]:
    """Whether one order opens (or feeds) a referral, and the sentence saying why not.

    `matched` is the whole point: an order that was placed and never filled paid no builder fee, so it can neither
    qualify a referral nor accrue one. `self_cross` is refused structurally rather than by threshold — an order
    whose maker and taker are the same wallet is a round trip, and `revenue.attribution` already refuses to write
    an attribution row for it. A threshold ("ignore rounds under $1") is a rate card for wash trading.
    """
    notional = _int(notional_micro, "notional_micro")
    del at_ms
    if not matched:
        return False, "an unfilled order pays no fee, so it cannot qualify a referral"
    if self_cross:
        return False, "an order that crosses itself earns nobody a referral"
    if str(excluded_reason or ""):
        return False, str(excluded_reason)
    if notional < QUALIFY_NOTIONAL_MICRO:
        return False, ("$%d.%02d notional is below the $%d qualifying order"
                       % (notional // 1_000_000, (notional % 1_000_000) // 10_000, QUALIFY_NOTIONAL_MICRO // 1_000_000))
    return True, ""


# ----------------------------------------------------------------------------------------------- the clock
def term_ends_ms(qualified_ms: int) -> int:
    """The instant accrual stops, from the qualifying order rather than from signup."""
    return _int(qualified_ms, "qualified_ms") + TERM_DAYS * 86_400_000


def in_term(qualified_ms: int, at_ms: int) -> bool:
    return _int(at_ms, "at_ms") < term_ends_ms(qualified_ms)


def term_days_left(qualified_ms: int, at_ms: int) -> int:
    left = term_ends_ms(qualified_ms) - _int(at_ms, "at_ms")
    return max(0, -(-left // 86_400_000))         # ceil: a part-day of accrual is a day the term is still open


# ---------------------------------------------------------------------------------------------- the money
def share_of(observed_fee_micro: int, share_bps: int = SHARE_BPS) -> int:
    """The referrer's cut of a fee we were actually paid. Floors, and never exceeds the fee itself."""
    observed = max(0, _int(observed_fee_micro, "observed_fee_micro"))
    bps = _int(share_bps, "share_bps")
    if not (0 <= bps <= 10_000):
        raise ValueError("share_bps out of range: %d" % bps)
    return (observed * bps) // 10_000


def accrual(*, referrer: str, referee: str, day: str, observed_fee_micro: int, state: str,
            qualified_ms: int, at_ms: int, share_bps: int = SHARE_BPS) -> dict | None:
    """One day's accrual row, or None when the day earns nothing.

    Returns None — rather than a zero row — for an order outside the term, for a referral that is not accruing, and
    for a day with no observed fee. A table of zero rows is a table nobody can sum without filtering, and the
    filter is exactly where a bug hides. The reasons are not lost: the caller logs the refusal.
    """
    if state not in ACCRUING_STATES:
        return None
    if not in_term(qualified_ms, at_ms):
        return None
    share = share_of(observed_fee_micro, share_bps)
    if share <= 0:
        return None
    return {"referrer": referrer, "referee": referee, "day": str(day), "fee_observed_micro": int(observed_fee_micro),
            "share_bps": int(share_bps), "share_micro": share, "created_ms": _int(at_ms, "at_ms")}


def payable(accruals: list[dict], *, at_ms: int, minimum_micro: int = PAYOUT_MIN_MICRO,
            hold_days: int = SETTLE_HOLD_DAYS) -> dict:
    """What can be sent now, what is still settling, and what is short of the minimum.

    The three buckets are disjoint and their sum is the referrer's accrued total, which is the property the
    dashboard is checked against: a dashboard whose "earned" is not the sum of its parts is a dashboard that will
    be believed anyway.
    """
    at = _int(at_ms, "at_ms")
    hold_ms = _int(hold_days, "hold_days") * 86_400_000
    settled = held = 0
    for row in accruals:
        if str(row.get("state") or "qualified") == "clawed_back":
            continue
        if at - _int(row.get("created_ms", 0), "created_ms") >= hold_ms:
            settled += _int(row.get("share_micro") or 0, "share_micro")
        else:
            held += _int(row.get("share_micro") or 0, "share_micro")
    minimum = _int(minimum_micro, "minimum_micro")
    return {"settled_micro": settled, "holding_micro": held, "accrued_micro": settled + held,
            "payable_micro": settled if settled >= minimum else 0,
            "to_minimum_micro": 0 if settled >= minimum else minimum - settled,
            "review_required": settled > REVIEW_THRESHOLD_MICRO,
            "minimum_micro": minimum, "hold_days": hold_days,
            # The one sentence a referrer reads instead of a spreadsheet.
            "note": ("$%d.%02d is payable now; $%d.%02d is inside the %d-day settlement hold"
                     % (settled // 1_000_000, (settled % 1_000_000) // 10_000,
                        held // 1_000_000, (held % 1_000_000) // 10_000, hold_days)
                     if settled + held else "nothing has accrued yet")}


def payout_period(ts_ms: int) -> str:
    import datetime as _dt
    d = _dt.datetime.fromtimestamp(_int(ts_ms, "ts_ms") / 1000, _dt.timezone.utc)
    return "%04d-%02d" % (d.year, d.month)


def payout_at_ms(period: str) -> int:
    """The 10th of the following month, 09:00 UTC. A schedule a referrer can put in a calendar."""
    import datetime as _dt
    y, m = (int(p) for p in str(period).split("-"))
    if not (1 <= m <= 12):
        raise ValueError("bad period %r" % period)
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return int(_dt.datetime(y, m, 10, 9, 0, tzinfo=_dt.timezone.utc).timestamp() * 1000)


def next_payout_ms(at_ms: int) -> int:
    return payout_at_ms(payout_period(_int(at_ms, "at_ms")))


# --------------------------------------------------------------------------------------------- the funnel
def funnel_findings(counts: dict) -> list:
    """A dashboard whose money funnel is not monotone is a dashboard with a bug in a query, not a busy referrer.

    `signups >= funded >= trading >= earned` must hold, and the reason to check it rather than trust it is that
    every one of those numbers comes from a different table: a join that counts a row twice, or a filter applied
    to one step and not the next, shows up here as an impossible funnel rather than as a plausible number nobody
    re-derives.

    Clicks are checked too, but only for the thing a click count can be wrong about on its own: it cannot be
    negative, and it cannot be smaller than nothing. Whether it *dominates* signups is a question with a wrong
    answer — see FUNNEL.
    """
    problems = []
    for lead in LEADING:
        if _int(counts.get(lead, 0), lead) < 0:
            problems.append("%s is negative, which no counter can be" % lead)
    order = list(FUNNEL)
    for a, b in zip(order, order[1:]):
        if _int(counts.get(a, 0), a) < _int(counts.get(b, 0), b):
            problems.append("%s (%d) is below %s (%d) — a step cannot have more of something than the step before it"
                            % (a, counts.get(a, 0), b, counts.get(b, 0)))
    return problems


def state_text(state: str) -> str:
    """What each state means, in a sentence a referrer reads rather than a code they decode."""
    return {
        "pending": "signed up, but has not placed a matched order of $25 yet — this one has earned nothing",
        "qualified": "first matched order cleared, earning a share of the fee on every order for a year",
        "review": "held for a person to look at; nothing accrues until it is cleared, and nothing is lost either",
        "refused": "refused, with the reason on the row — no fee will ever accrue from it, and a self-referral is\n                also a builder-code revocation ground",
        "expired": "the one-year term ended; accrual stopped on schedule",
        "clawed_back": "reversed under the published clawback rule, with the reason on the row",
    }.get(state, "unknown state")


def tax_requirement(ytd_micro: int, *, country: str = "", taxable: bool = True) -> dict:
    """The tax reality, stated plainly rather than ignored.

    Product copy, not advice: what we file, when we file it, and what a referrer has to give us before the first
    payout. `[UNVERIFIED]` in the phase document — the thresholds and forms are a jurisdiction review that belongs
    to launch, and this function is where that review lands rather than in a markdown file nobody reads again.
    """
    ytd = max(0, _int(ytd_micro, "ytd_micro"))
    us = str(country or "").upper() in ("US", "USA", "UNITED STATES")
    if us:
        return {"required": True, "form": "W-9", "reportForm": "1099-NEC",
                "reportThresholdMicro": TAX_REPORT_THRESHOLD_MICRO,
                "reportable": ytd >= TAX_REPORT_THRESHOLD_MICRO, "withholding": "",
                "note": "we file a 1099-NEC for $600 or more in a calendar year; a W-9 before the first payout"}
    return {"required": True, "form": "W-8BEN (or W-8BEN-E for an entity)", "reportForm": "1042-S where applicable",
            "reportThresholdMicro": 0, "reportable": taxable, "withholding": "treaty rate where a form says so",
            "note": "a treaty claim needs the form on file before the first payout; we do not withhold unless the "
                    "form or the law requires it, and a referrer is responsible for their own return"}


TERMS = {
    "model": "builder-fee share",
    "model_sentence": "a share of the builder fee we are paid on the referee's own orders, for one year",
    "qualify_notional_micro": QUALIFY_NOTIONAL_MICRO,
    "share_bps": SHARE_BPS,
    "term_days": TERM_DAYS,
    "settle_hold_days": SETTLE_HOLD_DAYS,
    "payout_min_micro": PAYOUT_MIN_MICRO,
    "review_threshold_micro": REVIEW_THRESHOLD_MICRO,
    "clawback_min_micro": CLAWBACK_MIN_MICRO,
    "paid_from": "the fee the venue actually paid us (observed), never the fee we expected",
    "rejected_models": {
        "flat bounty on a first funded trade":
            "a fixed payment for crossing a threshold, where the cost of manufacturing the crossing can be less "
            "than the payment: the farm's marginal cost is one small trade and ours is the whole bounty",
        "deposit-size reward":
            "pays for money arriving, not for trading, which is what attracts deposit-and-withdraw accounts and "
            "reads as a pyramid",
        "Pro credit":
            "costs margin rather than cash, but it pays referrers in a currency they may not want and turns the "
            "program into an upsell funnel; kept as a plan benefit, not as a reward",
    },
    "rules": list(PUBLISHED_RULES),
    "tax_note": "referral earnings are taxable income to the referrer; forms and thresholds are stated on the "
                "payout page and confirmed by counsel before launch [UNVERIFIED]",
    "schedule": "monthly, by the 10th, for the month before; $20 minimum, carried forward below it",
    "no_referrer_leaderboard":
        "there is no public referrer leaderboard: a public contest over recruitment is a spam contest with a "
        "scoreboard, and the ranking it would print is a ranking of recruiting, not of trading",
}
