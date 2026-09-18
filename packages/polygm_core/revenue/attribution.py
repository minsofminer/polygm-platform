"""D8: builder-code revenue — what we are owed, what the venue actually paid, and who gets a share.

Four rules hold this module together, and each is a *refusal* somewhere:

1. **A code is issued, never typed.** `validate_code` refuses a code that is not in the registry, one that is
   revoked, and one whose `revoked_ms` is before the order's own timestamp. A free-text builder field is a
   fee field, and a fee field anyone can name is how revenue reconciliation becomes archaeology.
2. **Attribution counts fills, not intentions.** An order that was placed and never matched paid nothing, so
   it earns nothing — but it is still recorded (`builder_attribution` writes for rejected orders too, per
   P04), because "we tried and the venue said no" is the evidence that settles a dispute about coverage.
3. **A payout can only come from money we actually received.** Sources are paid a share of the *observed*
   builder fee, never the expected one. If the venue has not paid us, nobody's revenue share is owed, and a
   ledger that promises payouts against an unreceived fee is a liability we invented.
4. **The second measurement may not look at the first.** `measure_from_chain` takes venue/chain events and
   returns what *they* say. It has no parameter for our expectation, which is the only way to keep the delta
   meaningful: a reconciliation job that reads `fee_micro_expected` to compute `fee_micro_observed` is a
   circular argument with a CHECK constraint on it.

Self-dealing is excluded structurally rather than by threshold: an order whose maker and taker resolve to the
same wallet, and a copy whose source and copier are the same user, produce no attribution row at all. A
threshold ("ignore rounds under $1") is a rate card for wash trading.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace

from ..money.cents import SCALE
from ..venue.clob_v2 import estimate_fees

# What a code is for. The kind decides which anti-gaming rules apply and what the UI may promise, so it is a
# closed set at the registry boundary even though the code string itself is free-form (the venue's taxonomy is
# open, and P05 §15 says we do not invent CHECK lists over someone else's vocabulary).
CODE_KINDS = ("copy_source", "affiliate", "internal")


@dataclass(frozen=True)
class BuilderCode:
    code: str
    label: str
    kind: str
    fee_bps: int                              # what this code entitles us to, in basis points of notional
    source_user: str = ""                     # who earns a revenue share from this code ('' = platform only)
    source_share_bps: int = 0                 # their cut of OUR fee, not of the user's cost
    owner: str = "platform"
    active: bool = True
    issued_ms: int = 0
    revoked_ms: int | None = None
    max_fee_bps: int = 200                    # a code may not claim more than this, whatever it says

    def __post_init__(self) -> None:
        if self.kind not in CODE_KINDS:
            raise ValueError("BAD_CODE_KIND: %r (allowed: %s)" % (self.kind, ", ".join(CODE_KINDS)))
        if not (0 <= self.fee_bps <= self.max_fee_bps):
            raise ValueError("FEE_BPS_OUT_OF_RANGE: %d not in 0..%d" % (self.fee_bps, self.max_fee_bps))
        if not (0 <= self.source_share_bps <= 10_000):
            raise ValueError("SOURCE_SHARE_OUT_OF_RANGE: %d" % self.source_share_bps)
        if self.source_share_bps and not self.source_user:
            raise ValueError("SOURCE_SHARE_WITHOUT_SOURCE: a revenue share needs somebody to pay")
        if self.kind == "internal" and self.source_user:
            raise ValueError("INTERNAL_CODE_HAS_SOURCE: an internal code pays nobody")

    @property
    def fingerprint(self) -> str:
        """Content hash of the money-bearing fields. Stored on the attribution row so a later edit of the code
        (a changed `fee_bps`, a quiet revocation) is *visible* against the orders that were placed under the
        old terms — the alternative is a dispute in which neither side can say what the rate was."""
        core = "|".join(str(v) for v in (self.code, self.kind, self.fee_bps, self.source_user,
                                        self.source_share_bps, self.max_fee_bps))
        return hashlib.sha256(core.encode()).hexdigest()[:16]


class CodeRegistry:
    """Issuance and revocation. There is no `update_rate`: a code's economics are fixed at issue, and what
    looks like flexibility here is a way for yesterday's orders to be re-priced by today's admin."""

    def __init__(self, codes: list[BuilderCode] | None = None) -> None:
        self._by_code: dict[str, BuilderCode] = {}
        for c in codes or []:
            self.issue(c)

    def issue(self, c: BuilderCode) -> BuilderCode:
        if c.code in self._by_code:
            raise ValueError("CODE_ALREADY_ISSUED: %s (revoke it and issue a new one; a code whose rate "
                             "changed mid-flight cannot be reconciled)" % c.code)
        if not c.issued_ms:
            raise ValueError("CODE_NOT_DATED: %s" % c.code)
        self._by_code[c.code] = c
        return c

    def revoke(self, code: str, *, at: int, actor: str = "operator") -> dict:
        c = self._by_code.get(code)
        if c is None:
            return {"revoked": False, "reason": "no such code"}
        if c.revoked_ms:
            return {"revoked": False, "reason": "already revoked at %d" % c.revoked_ms}
        self._by_code[code] = replace(c, active=False, revoked_ms=at)
        return {"revoked": True, "code": code, "at": at, "actor": actor,
                "note": "orders placed before the revocation keep the rate they were placed under"}

    def get(self, code: str) -> BuilderCode | None:
        return self._by_code.get(code)

    def validate(self, code: str, *, at: int, fee_bps_claimed: int | None = None) -> dict:
        """The only gate between a request and an attributable order. Denials here are quiet in the venue
        response (the order still goes through) but loud in the ledger: an unattributable order is ours to
        execute and nobody else's revenue."""
        c = self._by_code.get(code)
        if c is None:
            return {"ok": False, "code": "BUILDER_CODE_UNKNOWN",
                    "message": "that builder code was not issued by us; the order will still run, unattributed"}
        if c.revoked_ms is not None and at >= c.revoked_ms:
            return {"ok": False, "code": "BUILDER_CODE_REVOKED",
                    "message": "revoked at %d; orders after that earn nothing" % c.revoked_ms}
        if not c.active:
            return {"ok": False, "code": "BUILDER_CODE_INACTIVE", "message": "code is not active"}
        if fee_bps_claimed is not None and int(fee_bps_claimed) != c.fee_bps:
            # The request may state a rate; if it disagrees with the registry, the registry wins and the
            # mismatch is a flag, not a silent re-price. A client that can set its own fee rate is a client
            # that will eventually set it to zero.
            return {"ok": True, "code": "BUILDER_RATE_MISMATCH", "builder": c, "used_bps": c.fee_bps,
                    "claimed_bps": int(fee_bps_claimed),
                    "message": "request claimed %d bps; the issued code says %d and the issued code is used"
                              % (int(fee_bps_claimed), c.fee_bps)}
        return {"ok": True, "code": "", "builder": c, "used_bps": c.fee_bps}

    def attributable_codes(self, *, at: int) -> list[BuilderCode]:
        return [c for c in self._by_code.values() if c.active and (c.revoked_ms is None or c.revoked_ms > at)]


@dataclass(frozen=True)
class OrderFacts:
    """What an order must look like for its fee to be ours. Kept separate from `IntentRow` so the attribution
    rules can be reasoned about (and tested) without the executor."""
    intent_id: str
    order_id: str
    user_id: str
    market_id: str
    token_id: str
    side: str
    builder_code: str
    notional_micro: int
    filled_shares_micro: int = 0
    placed_ms: int = 0
    filled_ms: int | None = None
    maker_address: str = ""
    taker_address: str = ""
    source_user: str = ""                     # who the user was copying, if anyone
    counterparty_source: str = ""             # the source's own source, for a chain
    audience: str = "user"


def exclusion(f: OrderFacts) -> str:
    """Why this order earns nothing, or '' if it does.

    The order of these matters less than their being here at all: each is a way to make volume that is not
    trading, and a fee schedule that pays for invented volume pays for it forever.
    """
    if f.filled_shares_micro <= 0:
        # Rule 2: nothing matched, nothing was charged, nothing is owed. `placed_ms` is still recorded.
        return "unfilled"
    if f.maker_address and f.taker_address and f.maker_address.lower() == f.taker_address.lower():
        return "self_cross"
    if f.source_user and f.source_user == f.user_id:
        return "self_copy"
    if f.counterparty_source and f.counterparty_source == f.user_id:
        return "cycle"
    return ""


def expected_builder_fee_micro(notional_micro: int, builder_bps: int) -> int:
    """Our entitlement, computed with the venue's own rounding.

    `estimate_fees` is the single place that formula lives (P04), and calling it here rather than restating
    `notional * bps / 10000` is the difference between a match and a drift nobody notices for a quarter: the
    venue rounds UP, and a restatement that rounds down under-claims on every odd micro-unit. Since the
    builder fee is a flat rate on notional, evaluating the venue's per-share formula at a $1 payoff with the
    notional as the share count is the same arithmetic in one implementation.

    Zero notional is zero fee, not an error: unfilled orders are attributed with 0 and excluded, so a rollup
    can say "we placed N, we earned on M".
    """
    if notional_micro < 0:
        raise ValueError("NEGATIVE_NOTIONAL: %r" % notional_micro)
    if notional_micro == 0:
        return 0
    return estimate_fees(size_shares_micro=notional_micro, price_micro=10**SCALE,
                        fee_rate_bps=0, builder_bps=builder_bps).builder_micro


def attribution_row(f: OrderFacts, builder: BuilderCode | None, *, at: int) -> dict:
    """The row for `builder_attribution`, plus the reason it may be worth nothing.

    Written for every order we submit, including ones the venue rejected: the row proves coverage, and a
    dispute about "you never sent us the builder code on these" is settled by a table that says we did.
    """
    excluded = exclusion(f)
    fee = 0 if (excluded or builder is None) else expected_builder_fee_micro(f.notional_micro, builder.fee_bps)
    return {"order_id": f.order_id, "intent_id": f.intent_id, "user_id": f.user_id,
            "builder_code": builder.code if builder else "", "fee_bps_expected": builder.fee_bps if builder else 0,
            "fee_micro_expected": fee, "market_id": f.market_id, "token_id": f.token_id,
            "placed_ms": f.placed_ms, "excluded_reason": excluded,
            "code_fingerprint": builder.fingerprint if builder else ""}


def measure_from_chain(events: list[dict], *, code: str) -> dict:
    """What the venue/chain says we were paid for one builder code. Signature has no way to receive our own
    expectation, and that is the point: see rule 4.

    An event's `builder_fee_micro` is the amount the settlement log attributes to the code. Events that do not
    name our code are ignored rather than guessed at, and an event whose value is missing is reported as a
    gap in `incomplete`, because a partial sum presented as a measurement is worse than no measurement.
    """
    total = 0
    counted = 0
    incomplete = 0
    for e in events:
        if str(e.get("builder_code") or "") != code:
            continue
        raw = e.get("builder_fee_micro")
        if raw is None:
            incomplete += 1
            continue
        # Strict on the money path (P04's rule, and the opposite of the tape reader's leniency): a value that
        # is not an exact number of micro units means somebody averaged, apportioned or float-divided a
        # settlement figure upstream, and the delta we are about to compute would be measuring their rounding.
        if isinstance(raw, float) and not raw.is_integer():
            raise ValueError("NON_INTEGER_FEE_EVENT: %r is not a whole number of micro units" % raw)
        v = int(raw)
        if v < 0:
            raise ValueError("NEGATIVE_FEE_EVENT: %r (a refund is its own event kind)" % raw)
        total += v
        counted += 1
    return {"code": code, "measured_micro": total, "events_counted": counted, "events_incomplete": incomplete,
            "complete": incomplete == 0}


def compare(expected: dict, measured: dict, *, tol_micro: int = 1, tol_bps: int = 25) -> dict:
    """The delta between what we should have been paid and what the chain says we were.

    Both a tolerance in micros AND one in basis points: on a $4 order a cent of rounding is 25% of the fee,
    and on a $400,000 block a cent is noise. A single threshold would either cry wolf or miss a real gap.
    """
    e, m = int(expected["fee_micro_expected"]), int(measured["measured_micro"])
    delta = e - m
    bps = (abs(delta) * 10_000) // e if e else (0 if delta == 0 else 10_000)
    ok = abs(delta) <= max(tol_micro, (e * tol_bps) // 10_000)
    return {"expected_micro": e, "measured_micro": m, "delta_micro": delta, "delta_bps": bps,
            "reconciled": bool(ok and not measured["events_incomplete"]),
            "dispute": "" if ok else ("we_expected_more" if delta > 0 else "venue_paid_more"),
            "unmeasured_events": measured["events_incomplete"],
            "note": ("within tolerance" if ok else
                     "the venue paid %d micro against an expectation of %d; open a ticket with the order ids"
                     % (m, e))}


@dataclass
class DailyRollup:
    day: str
    orders: int = 0
    fills: int = 0
    volume_micro: int = 0
    expected_micro: int = 0
    observed_micro: int = 0
    excluded: dict[str, int] = field(default_factory=dict)
    by_code: dict[str, int] = field(default_factory=dict)
    payout_micro: int = 0
    platform_micro: int = 0
    reconciled: bool = False
    disputes: list[str] = field(default_factory=list)
    by_source: dict[str, int] = field(default_factory=dict)
    capped: bool = False
    unmeasured: int = 0
    measured_rows: int = 0
    per_code_delta: dict[str, int] = field(default_factory=dict)

    # The engine's three states, and the words P04's `builder_revenue_daily.status` CHECK allows. The
    # mapping lives here rather than in the writer because a status that does not exist in that list is an
    # IntegrityError in a nightly job — the failure surfaces at 02:00 as a missing day, which is precisely
    # the silence this table exists to prevent.
    STATUS_WORDS = {"unmeasured": "unreconciled", "disputed": "investigating", "reconciled": "matched"}

    @property
    def delta_micro(self) -> int:
        """What we think we are owed minus what the chain says we were. A property, not a stored field: a
        total that can drift from the two columns it summarises is how a revenue table starts lying."""
        return self.expected_micro - self.observed_micro

    @property
    def engine_status(self) -> str:
        """The state in the engine's own words, which are more specific than the schema's CHECK list:
        `unreconciled` in the table cannot tell a reader whether anybody looked, and `unmeasured` can."""
        return ("disputed" if self.disputes else
                "unmeasured" if (self.fills and not self.measured_rows) else "reconciled")

    def status_word(self) -> str:
        return self.STATUS_WORDS["disputed" if self.disputes else
                                 "unmeasured" if (self.fills and not self.measured_rows) else "reconciled"]

    def to_daily_row(self) -> dict:
        """The kwargs `Store.save_daily` takes, in the shape that table enforces.

        `status` is three-valued on purpose: `unmeasured` (nobody has compared yet) must never be rendered as
        `disputed` (we compared and the venue is short) nor as `reconciled`. The three states are the whole
        difference between a revenue report and a wish.
        """
        return {"day": self.day, "orders": self.orders, "fills": self.fills,
                "volume_micro": self.volume_micro, "expected_micro": self.expected_micro,
                "chain_micro": self.observed_micro, "platform_fee_micro": self.platform_micro,
                "status": self.status_word()}

    def as_dict(self) -> dict:
        return {"day": self.day, "orders": self.orders, "fills": self.fills,
                "volume_micro": self.volume_micro, "expected_micro": self.expected_micro,
                "observed_micro": self.observed_micro, "delta_micro": self.delta_micro,
                "excluded": dict(self.excluded), "by_code": dict(self.by_code),
                "payout_micro": self.payout_micro, "platform_micro": self.platform_micro,
                "reconciled": self.reconciled, "disputes": list(self.disputes),
                "status": self.status_word(), "engine_status": self.engine_status,
                "unmeasured_orders": self.unmeasured, "measured_orders": self.measured_rows,
                "per_code_delta_micro": dict(self.per_code_delta),
                # The share of placed volume that earned nothing, and why. A number a finance reader can
                # check without asking us what the exclusions were.
                "excluded_order_share_bps": (sum(self.excluded.values()) * 10_000) // self.orders
                if self.orders else 0}


def rollup(rows: list[dict], measures: list[dict], *, day: str, registry: CodeRegistry | None = None,
           tol_micro: int = 1, tol_bps: int = 25) -> DailyRollup:
    """One day of revenue, from the two tables that describe it.

    `rows` are `builder_attribution` rows (our expectation) and `measures` are `builder_attribution_measures`
    rows (what the chain said), joined on `attribution_id` — the measure table deliberately has no builder_code
    column, so a measurement can only ever be attached to an order we already expected to be paid on, and
    nobody can invent a revenue day out of an unmatched log line.

    A day whose measures have not arrived yet reports `measured_rows=0` and `reconciled=False`, NOT a delta of
    zero. "We have not looked" and "it matches" are different facts, and a dashboard that conflates them is how
    a quarter of uncollected fees stays invisible.
    """
    r = DailyRollup(day=day)
    by_id = {int(m["attribution_id"]): m for m in measures if m.get("attribution_id") is not None}
    per_code_measured: dict[str, int] = {}
    per_code_expected: dict[str, int] = {}
    for row in rows:
        r.orders += 1
        r.volume_micro += int(row.get("notional_micro") or 0)
        excl = str(row.get("excluded_reason") or "")
        if excl:
            r.excluded[excl] = r.excluded.get(excl, 0) + 1
            continue
        r.fills += 1
        code = str(row.get("builder_code") or "")
        # `expected_micro` is what `Store.attribution_rows` returns; `fee_micro_expected` is the column name.
        # Accepting only one of the two means the nightly job raises KeyError on the first row it reads and a
        # unit test written against the other spelling stays green.
        expected = int(row.get("fee_micro_expected") or row.get("expected_micro") or 0)
        r.expected_micro += expected
        r.by_code[code] = r.by_code.get(code, 0) + expected
        per_code_expected[code] = per_code_expected.get(code, 0) + expected
        row_id = row.get("id") if row.get("id") is not None else row.get("attribution_id")
        m = by_id.get(int(row_id)) if row_id is not None else None
        if m is None:
            r.unmeasured += 1
            continue
        measured = int(m.get("chain_measured_micro") or 0)
        r.observed_micro += measured
        r.measured_rows += 1
        per_code_measured[code] = per_code_measured.get(code, 0) + measured
        delta = expected - measured
        if abs(delta) > max(tol_micro, (expected * tol_bps) // 10_000):
            tag = "%s:%s(%d)" % (code, "we_expected_more" if delta > 0 else "venue_paid_more", delta)
            if tag not in r.disputes:
                r.disputes.append(tag)
    # Reconciled means every attributable order in the day has a measurement AND none of them is disputed.
    r.reconciled = r.unmeasured == 0 and not r.disputes and (r.fills == 0 or r.measured_rows > 0)
    split = payout_split(rows, {c: {"micro": v} for c, v in per_code_measured.items()}, registry=registry,
                        observed_total_micro=r.observed_micro)
    r.payout_micro = split["payout_micro"]
    r.platform_micro = split["platform_micro"]
    r.by_source = split["by_source"]
    r.capped = split["capped"]
    r.per_code_delta = {c: per_code_expected.get(c, 0) - per_code_measured.get(c, 0)
                       for c in sorted(set(per_code_expected) | set(per_code_measured))}
    return r


def payout_split(rows: list[dict], measured: dict[str, dict], *, registry: CodeRegistry | None = None,
                 observed_total_micro: int | None = None) -> dict:
    """Who gets what, and it is bounded by money we can see.

    Aggregated BY CODE before the split: `measured` is a per-code total, so a day with 400 attributable orders
    on one code must not multiply that code's observed fee by 400 (an earlier draft did exactly that, and it
    only shows up as a payout larger than the fee, which the cap below then has to catch — a number that needs
    a cap to be sane is not a number, it is a bug with a seatbelt on).

    Each source's share is `source_share_bps` of the OBSERVED fee for their code, floored, with the remainder
    kept by the platform and named. Flooring is a choice: rounding a payout up pays a source for a micro-unit
    that was never collected, and the sum of many rounded-up fractions is a liability that grows with volume.

    Excluded orders (unfilled, self-cross, self-copy, cycle) are skipped entirely: they earned nothing, so
    there is nothing to share.
    """
    expected_by_code: dict[str, int] = {}
    for row in rows:
        if str(row.get("excluded_reason") or ""):
            continue
        code = str(row.get("builder_code") or "")
        expected_by_code[code] = expected_by_code.get(code, 0) + int(row.get("fee_micro_expected") or 0)
    out: dict[str, int] = {}
    total_payout = 0
    total_expected = 0
    total_observed = 0
    for code, expected in expected_by_code.items():
        total_expected += expected
        builder = registry.get(code) if registry else None
        observed = int((measured.get(code) or {}).get("micro") or 0)
        total_observed += observed
        if builder is None or not builder.source_user or builder.source_share_bps == 0 or observed <= 0:
            continue
        share = (observed * builder.source_share_bps) // 10_000
        share = min(share, observed)                         # a source can never be owed more than was paid
        out[builder.source_user] = out.get(builder.source_user, 0) + share
        total_payout += share
    gross = total_observed if observed_total_micro is None else int(observed_total_micro)
    return {"by_source": out, "payout_micro": total_payout,
            # Cash view: what the venue paid us, minus what we owe sources. This is the number that can be
            # reconciled against a bank balance. The accrual view (against what we EXPECTED to be paid) is
            # reported next to it so a shortfall is visible instead of being absorbed into "platform revenue".
            "platform_micro": max(0, gross - total_payout), "expected_micro": total_expected,
            "observed_micro": gross, "unpaid_expectation_micro": max(0, total_expected - gross),
            "capped": False, "flooring": "per source per code, remainder to platform"}


def payout_capped(payout: dict, *, observed_total_micro: int) -> dict:
    """The last check before anything is written to a ledger: total payout may not exceed observed fees."""
    over = payout["payout_micro"] - max(0, observed_total_micro)
    if over <= 0:
        return dict(payout, capped=False, over_micro=0)
    scale_down = max(0, observed_total_micro)
    out = {}
    running = 0
    items = sorted(payout["by_source"].items())
    for i, (user, amount) in enumerate(items):
        if i == len(items) - 1:
            give = max(0, scale_down - running)               # the last line absorbs the rounding
        else:
            give = (amount * scale_down) // payout["payout_micro"] if payout["payout_micro"] else 0
            running += give
        out[user] = give
    return {"by_source": out, "payout_micro": sum(out.values()), "platform_micro": scale_down - sum(out.values()),
            "capped": True, "over_micro": over,
            "note": "the venue has not paid %d micro of the fee these payouts were promised against; payouts "
                    "were scaled to what was collected and the shortfall is reported, not invented" % over}
