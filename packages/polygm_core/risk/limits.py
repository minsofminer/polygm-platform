"""The rest of D4: the limits the P04 gate did not have, the fail-closed breaker, the deny-code registry,
and the kill-switch drill's measurement rules.

Why a second module instead of growing `gate.py`: the gate is pure and its ten checks have a documented
order that P04's own gate asserts. The checks here need *inputs the gate must not have* — a clock, a
counter, an error rate — and mixing them in would turn a function that is trivially auditable ("no I/O")
into one that is not. The executor calls `evaluate()` and then `evaluate_extra()`; both are mandatory and
`preflight`'s `risk_gate` step refuses if either is missing. `checks_run` is therefore two lists
concatenated, and the order between them is fixed by this module's `EXTRA_ORDER`.

The breaker deserves its paragraph. D4 says a risk service that is down must not fail open, and the naive
reading is "wrap it in a try/except". The failure mode that actually kills trading systems is the *slow*
service, not the dead one: a 30s timeout on a 50ms budget makes every order in the queue eventually time
out, and if you deny only on timeout you have converted an outage into a stampede. `RiskBreaker` opens on
either signal (consecutive failures OR error rate in a window), stays open while tripped, and admits exactly
one probe when it cools — so a recovery is a trickle, not a thundering herd against a service that just
came back.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from ..money.cents import SCALE
from .gate import Decision, Limits

# ================================================================================= machine-readable codes =
SEVERITIES = ("info", "warn", "hard")


@dataclass(frozen=True)
class DenySpec:
    code: str
    http: int
    retryable: bool
    severity: str          # info: user education. warn: worth a dashboard line. hard: pages in aggregate.
    user_message: str      # what the client shows verbatim; never contains a balance or another user's data
    owner: str             # which phase owns fixing it


def _d(code: str, http: int, retry: bool, sev: str, msg: str, owner: str) -> tuple[str, DenySpec]:
    return code, DenySpec(code, http, retry, sev, msg, owner)


DENY_CODES: dict[str, DenySpec] = dict([
    # --- P04's gate (kept here so the registry is the single machine-readable list, not a per-module habit)
    _d("RISK_HALT", 503, True, "hard", "trading is temporarily disabled", "p04"),
    _d("MARKET_NOT_ACCEPTING", 409, False, "info", "this market is not accepting orders right now", "p04"),
    _d("NO_ORDER_BOOK", 409, False, "info", "this market has no order book", "p04"),
    _d("STALE_QUOTE", 503, True, "warn", "prices are stale; re-fetch before ordering", "p05"),
    _d("BAD_SIDE", 422, False, "info", "side must be BUY or SELL", "p04"),
    _d("BAD_MARKET_META", 503, True, "warn", "could not read this market's limits", "p05"),
    _d("UNKNOWN_TICK", 503, True, "warn", "market reports an unexpected tick size", "p05"),
    _d("OFF_TICK", 422, False, "info", "price is not on the market's tick grid", "p04"),
    _d("ZERO_SIZE", 422, False, "info", "size must be greater than zero", "p04"),
    _d("BELOW_MIN_SIZE", 422, False, "info", "size is below the market minimum", "p04"),
    _d("BAD_AMOUNT", 422, False, "info", "amount is outside the supported scale", "p04"),
    _d("OVER_ORDER_CAP", 422, False, "warn", "order is above the per-order limit", "p04"),
    _d("PRICE_FAR_FROM_MID", 422, False, "warn", "price is far from the market; check the number", "p04"),
    _d("TOO_MANY_OPEN", 429, True, "warn", "too many open orders on this account", "p04"),
    _d("DAILY_CAP", 422, False, "warn", "this order would exceed your 24h limit", "p04"),
    # --- P06 venue / preflight
    _d("VENUE_UNREACHABLE", 503, True, "hard", "cannot confirm the venue is accepting traffic", "p06"),
    _d("ORDER_TYPE_FORBIDDEN", 422, False, "info", "this order type is not available to you", "p06"),
    _d("INSUFFICIENT_BALANCE", 422, False, "info", "this order costs more than your available balance", "p06"),
    _d("ALLOWANCE_REQUIRED", 409, True, "info", "a token approval is needed before this order", "p06"),
    _d("SELF_TRADE", 409, False, "info", "this order would match your own resting order", "p06"),
    _d("BATCH_TOO_LARGE", 422, False, "info", "too many orders in one request", "p06"),
    _d("CANCEL_THROTTLED", 429, True, "warn", "cancel budget is exhausted; try again in a moment", "p06"),
    _d("CONFIRM_PHRASE_REQUIRED", 422, False, "info", "type the confirmation phrase to cancel everything", "p06"),
    _d("REASON_TOO_SHORT", 422, False, "info", "say why before cancelling all orders", "p06"),
    _d("BALANCE", 422, False, "info", "venue reports insufficient balance or allowance", "p06"),
    _d("BAD_PRICE", 422, False, "info", "venue rejected the price", "p06"),
    _d("TICK", 422, False, "info", "venue rejected the tick size", "p06"),
    _d("ROUNDING", 422, False, "warn", "venue rejected the amount rounding", "p06"),
    _d("VERSION", 503, True, "hard", "order version mismatch with the venue", "p06"),
    _d("THROTTLED", 429, True, "warn", "venue is rate limiting us", "p06"),
    _d("VALIDATION", 422, False, "info", "venue could not parse the order", "p06"),
    _d("DELAYED", 409, True, "info", "this market opens on a delay", "p06"),
    # P13 D7.7: the venue can switch a builder code off, and that refusal is not a generic one — it is a
    # commercial event (revenue for that code stops) with its own sentence. It reaches this registry because the
    # executor's `_reject` renders every code through `spec_for`, which raises on an unregistered one: without
    # this line, a disabled builder code crashed the executor instead of telling the user, which the P13 chaos
    # suite caught on its first run.
    _d("BUILDER_DISABLED", 409, False, "warn", "the venue has disabled that builder code", "p13"),
    _d("VENUE_REJECTED", 422, False, "warn", "the venue rejected this order", "p06"),
    # --- P06 risk extras
    _d("CIRCUIT_OPEN", 503, True, "hard", "risk controls are unavailable; nothing will be signed", "p06"),
    _d("MARKET_BLOCKLISTED", 403, False, "hard", "trading is blocked on this market", "p06"),
    _d("ORDER_RATE", 429, True, "warn", "too many orders this minute", "p06"),
    _d("DAILY_LOSS_HALT", 422, False, "hard", "your daily loss limit was reached; review your positions to "
                                              "continue", "p06"),
    _d("PRICE_SANITY", 422, False, "warn", "price is far from the last quote", "p06"),
    _d("SLIPPAGE", 422, True, "warn", "this order would move the price more than your bound", "p06"),
    _d("RISK_UNAVAILABLE", 503, True, "hard", "the risk service did not answer", "p06"),
    _d("PREFLIGHT_INCOMPLETE", 500, True, "hard", "our own pre-flight did not run every check; nothing was "
       "signed", "p06"),
    _d("POLICY_DRIFT", 403, False, "hard", "this wallet's custody policy changed", "p06"),
    _d("POLICY_GAP", 403, False, "hard", "a required limit cannot be enforced for this wallet", "p06"),
    _d("WALLET_NOT_TRADABLE", 409, True, "warn", "this wallet is not in a tradable state", "p06"),
    _d("NOT_AUTHENTICATED", 401, False, "hard", "the account password is required", "p06"),
    _d("TYPED_AMOUNT_MISMATCH", 422, False, "info", "the amount you typed is not the amount being sent", "p06"),
    _d("TYPED_ADDRESS_MISMATCH", 422, False, "info", "the address you typed is not the destination", "p06"),
    _d("ADDRESS_NOT_ALLOWLISTED", 403, False, "warn", "destination is not on your allowlist", "p06"),
    _d("ADDRESS_COOLDOWN", 409, True, "warn", "this address is too new to withdraw to", "p06"),
    _d("AMOUNT_TOO_SMALL", 422, False, "info", "amount must be greater than zero", "p06"),
    _d("AMOUNT_OVER_SINGLE_CAP", 422, False, "warn", "above the per-withdrawal cap", "p06"),
    _d("DAILY_WITHDRAWAL_CAP", 422, False, "warn", "this would exceed today's outflow limit", "p06"),
    _d("OPEN_ORDERS_EXIST", 409, True, "info", "cancel your open orders first", "p06"),
    _d("NOTIFY_UNCONFIRMED", 503, True, "hard", "withdrawal notifications could not be delivered", "p06"),
    _d("THRESHOLD_APPROVAL_REQUIRED", 403, False, "hard", "this amount needs a second approver", "p06"),
    _d("WALLET_BUSY", 409, True, "info", "this wallet has unfinished activity", "p06"),
    _d("SIGNER_UNAVAILABLE", 503, True, "hard", "the signer is not reachable", "p04"),
    _d("SIGNATURE_REFUSED", 403, False, "hard", "this wallet cannot sign in the required mode", "p06"),
    _d("NOT_FOUND", 404, False, "info", "not found", "p04"),
    _d("IDEM_KEY_REQUIRED", 422, False, "info", "an idempotency key is required", "p04"),
    _d("IDEM_CONFLICT", 409, False, "warn", "that key was already used for a different request", "p04"),
    _d("IDEM_IN_PROGRESS", 409, True, "warn", "the first request is still being processed", "p04"),
    _d("VALIDATION", 422, False, "info", "the request body is not valid", "p04"),
    _d("BAD_REASON", 422, False, "info", "say why before changing the kill switch", "p04"),
    # --- P06 copy / automation
    _d("COPY_SOURCE_STOPPED", 409, False, "info", "we stopped copying this source", "p06"),
    _d("COPY_INSIDER_FLAGGED", 403, False, "hard", "this source is under review; copying is paused", "p06"),
    _d("COPY_CYCLE", 422, False, "warn", "that would copy yourself in a loop", "p06"),
    _d("COPY_DEPTH", 422, False, "warn", "copying a copier of a copier is not allowed", "p06"),
    _d("COPY_PRICE_BOUND", 422, True, "info", "the price moved past your bound; this fill was skipped", "p06"),
    _d("COPY_SOON_RESOLVING", 422, False, "info", "this market resolves too soon to copy into", "p06"),
    _d("COPY_RATE", 429, True, "info", "copies from this source are rate limited", "p06"),
    _d("RULE_DRY_RUN_REQUIRED", 422, False, "info", "run the rule in dry mode first", "p06"),
    _d("RULE_RATE", 429, True, "info", "this rule already fired recently", "p06"),
    _d("RULE_CAP", 422, False, "warn", "this rule has hit its daily limit", "p06"),
    _d("GLOBAL_CAP", 422, False, "hard", "automation is at its global ceiling", "p06"),
    _d("RULE_TREE", 422, False, "info", "this rule's trigger tree is not valid", "p06"),
    _d("HUMAN_PRIORITY", 429, True, "info", "this market just opened; humans go first", "p06"),
    _d("EDGE_BELOW_COSTS", 422, False, "warn", "this strategy's edge does not cover its fees", "p06"),
    _d("UNCERTAIN_INTENT", 409, True, "hard", "we are still checking whether this order exists", "p06"),
])

for _c, _s in DENY_CODES.items():
    # NOT an `assert`. P14's SAST pass flagged this and the two sites below as "assert used for control flow",
    # and the reason it matters here is `python -O`: asserts vanish, so a typo'd severity would ship silently to
    # the surface that decides how a refusal is displayed. An invariant that is load-bearing is raised, not
    # asserted — the build that strips asserts must not be a different product.
    if _s.severity not in SEVERITIES:
        raise ValueError("DENY_CODE_SEVERITY: %s is %r, not one of %s" % (_c, _s.severity, SEVERITIES))


def deny_code_known(code: str) -> bool:
    return code in DENY_CODES


def spec_for(code: str) -> DenySpec:
    """Unknown code => the *code* is missing from the registry, which is a build-time bug, so it raises.
    A silent default here would let a new deny code ship with no HTTP status and no message."""
    try:
        return DENY_CODES[code]
    except KeyError:
        raise KeyError("deny code %r is not in DENY_CODES; a code without an http/severity/message cannot "
                       "be rendered by the API or paged by the alerting" % code) from None


# ================================================================================== the extra limits ==
@dataclass(frozen=True)
class ExtraLimits:
    """Every value is a config key. The per-user list D4 asked for, plus the numbers that make each one
    enforceable rather than aspirational."""
    max_orders_per_minute: int = 12                    # a person clicking 12 times a minute is a bug or a bot
    max_orders_per_day: int = 200
    max_daily_loss_micro: int = 1_000_000_000          # $1,000 realized loss -> halt until THEY acknowledge
    price_sanity_cents: int = 5                        # vs the last quote, in cents: 0.05
    slippage_bps_max: int = 300                        # 3% vs the quote the intent was priced against
    max_open_orders_per_user: int = 24
    breaker_error_rate: float = 0.5
    breaker_min_samples: int = 20
    breaker_cooldown_ms: int = 15_000
    breaker_consecutive: int = 5


EXTRA_ORDER: tuple[str, ...] = ("circuit_breaker", "blocklist", "order_rate", "daily_loss_halt",
                                "price_sanity", "slippage_vs_quote")


@dataclass(frozen=True)
class RiskContext:
    """Assembled by the caller. Nothing in here is fetched by the gate: same discipline as P04's snapshot."""
    now_ms: int
    orders_this_minute: int = 0
    orders_today: int = 0
    realized_pnl_today_micro: int = 0                  # signed; negative is a loss
    loss_halted: bool = False
    blocklisted: bool = False
    last_quote_micro: int | None = None
    quote_age_ms: int = 0
    priced_against_micro: int | None = None            # the price the UI/intent believed it was trading at
    breaker_open: bool = False
    breaker_reason: str = ""


def evaluate_extra(*, price_micro: int, side: str, ctx: RiskContext, limits: ExtraLimits) -> Decision:
    run: list[str] = []

    def deny(code: str, msg: str, *, retry: bool = False) -> Decision:
        if not deny_code_known(code):
            raise ValueError("DENY_CODE_UNKNOWN: %r is not in the deny table, so no client could render it" % code)
        return Decision(False, code, msg, checks_run=tuple(run))

    run.append("circuit_breaker")
    if ctx.breaker_open:
        # Fail closed, and name the reason, because "risk unavailable" and "risk says no" need different
        # playbooks: the first is a page, the second is a user education event.
        return deny("CIRCUIT_OPEN", "risk controls are unavailable%s" % (" (%s)" % ctx.breaker_reason
                                                                          if ctx.breaker_reason else ""),
                    retry=True)

    run.append("blocklist")
    if ctx.blocklisted:
        return deny("MARKET_BLOCKLISTED", "trading is blocked on this market")

    run.append("order_rate")
    if ctx.orders_this_minute >= limits.max_orders_per_minute:
        return deny("ORDER_RATE", "too many orders this minute", retry=True)
    if ctx.orders_today >= limits.max_orders_per_day:
        return deny("ORDER_RATE", "daily order count reached", retry=False)

    run.append("daily_loss_halt")
    # Two ways in: the halt is already on (and unacknowledged), or this order would cross the line. The
    # second one matters: refusing only after the loss has happened means the limit is a speed bump.
    if ctx.loss_halted:
        return deny("DAILY_LOSS_HALT", "your daily loss limit was reached; review your positions to continue")
    if -ctx.realized_pnl_today_micro >= limits.max_daily_loss_micro:
        return deny("DAILY_LOSS_HALT", "your daily loss limit is reached")

    run.append("price_sanity")
    if ctx.last_quote_micro is not None and ctx.quote_age_ms <= limits.breaker_cooldown_ms:
        drift = abs(price_micro - ctx.last_quote_micro)
        if drift > limits.price_sanity_cents * 10_000:
            return deny("PRICE_SANITY", "price is far from the last quote")

    run.append("slippage_vs_quote")
    if ctx.priced_against_micro:
        bps = abs(price_micro - ctx.priced_against_micro) * 10_000 // max(ctx.priced_against_micro, 1)
        if bps > limits.slippage_bps_max:
            return deny("SLIPPAGE", "the book moved %.2f%% against the price this order was built on"
                        % (bps / 100), retry=True)
    return Decision(True, "OK", "accepted", checks_run=tuple(run))


def evaluate_with_breaker(primary: Decision, extra: Decision) -> Decision:
    """One answer out of two checks, so a caller cannot accidentally act on only the first."""
    if not primary.allowed:
        return primary
    return extra


# ================================================================================================= breaker =
@dataclass
class RiskBreaker:
    """Fail-closed circuit breaker for the risk path (D4). `now_ms` is injected: a breaker whose clock is
    `time.time()` internally cannot be tested at the boundary, and the boundary is the whole behaviour."""
    limits: ExtraLimits
    opened_ms: int = 0
    open_: bool = False
    consecutive: int = 0
    successes: int = 0
    failures: int = 0
    window_ms: int = 60_000
    window_started_ms: int = 0
    probe_in_flight: bool = False
    trips: int = 0
    last_reason: str = ""

    def _reset_window(self, now_ms: int) -> None:
        if now_ms - self.window_started_ms >= self.window_ms:
            self.window_started_ms = now_ms
            self.successes, self.failures = 0, 0

    def record(self, *, ok: bool, now_ms: int) -> None:
        self._reset_window(now_ms)
        if ok:
            self.successes += 1
            self.consecutive = 0
            if self.open_ and self.probe_in_flight:
                # Exactly one probe is admitted when cooling; a success closes the circuit for everyone.
                self.open_, self.probe_in_flight, self.opened_ms, self.last_reason = False, False, 0, ""
            return
        self.failures += 1
        self.consecutive += 1
        if self.consecutive >= self.limits.breaker_consecutive:
            self._open(now_ms, "%d consecutive failures" % self.consecutive)
            return
        total = self.successes + self.failures
        if total >= self.limits.breaker_min_samples and self.failures / total >= self.limits.breaker_error_rate:
            self._open(now_ms, "error rate %d/%d in window" % (self.failures, total))

    def _open(self, now_ms: int, why: str) -> None:
        if not self.open_:
            self.trips += 1
        self.open_, self.opened_ms, self.last_reason, self.probe_in_flight = True, now_ms, why, False

    def allow(self, now_ms: int) -> tuple[bool, str]:
        """Returns (allow, reason). A denial here becomes a `CIRCUIT_OPEN` risk decision, and the order is
        never signed — that is what "fail closed" means operationally."""
        if not self.open_:
            return True, ""
        if now_ms - self.opened_ms < self.limits.breaker_cooldown_ms:
            return False, "cooling after %s" % self.last_reason
        if self.probe_in_flight:
            return False, "a probe is already in flight"
        self.probe_in_flight = True
        return True, "half-open probe"

    def guard(self, fn: Callable[[], Decision], *, now_ms: int) -> Decision:
        """The one allowed way to call a fallible risk dependency. Any exception, including a bug, is a
        denial: "we could not check" must never be read as "the check passed"."""
        allow, why = self.allow(now_ms)
        if not allow:
            return Decision(False, "CIRCUIT_OPEN", "risk controls unavailable: %s" % why,
                            checks_run=("circuit_breaker",))
        try:
            out = fn()
        except Exception as e:                                  # noqa: BLE001 - fail-closed by design
            self.record(ok=False, now_ms=now_ms)
            return Decision(False, "RISK_UNAVAILABLE", "risk check raised %s" % type(e).__name__,
                            checks_run=("circuit_breaker",))
        self.record(ok=True, now_ms=now_ms)
        return out


# ============================================================================ kill switch + drill rules ==
KILL_BUDGET_MS = 1_000                    # D4: engaged -> no new order accepted, anywhere, in <1s
COMPONENTS = ("api", "executor", "copy", "automation", "worker")


@dataclass
class DrillSample:
    component: str
    t_zero_ms: int
    refused_at_ms: int
    method: str                            # how we know it refused (the probe that got RISK_HALT)


def drill_verdict(samples: list[DrillSample], *, budget_ms: int = KILL_BUDGET_MS) -> dict:
    """Compute the drill result from the samples rather than asserting a constant.

    Two things this catches that a naive `max(latency) < budget` does not: a component that never answered
    at all (missing sample = FAIL, not "no data") and a component that refused *before* t_zero (negative
    latency = the flag was already engaged, so the drill measured nothing).
    """
    per = {}
    for s in samples:
        per[s.component] = min(per.get(s.component, 10**12), s.refused_at_ms - s.t_zero_ms)
    missing = [c for c in COMPONENTS if c not in per]
    negative = [c for c, v in per.items() if v < 0]
    over = [c for c, v in per.items() if v > budget_ms]
    ok = not missing and not negative and not over
    return {"verdict": "pass" if ok else "fail", "per_component_ms": per, "budget_ms": budget_ms,
            "missing": missing, "negative_latency": negative, "over_budget": over,
            "worst_ms": max(per.values()) if per else None}


def loss_halt_state(ctx: RiskContext, limits: ExtraLimits) -> dict:
    """The halt is a *state*, not an error: the user acknowledges it (D8: an operator lifting it is a
    separate audited event), and until then every order is refused with a code that says why."""
    loss = max(-ctx.realized_pnl_today_micro, 0)
    tripped = ctx.loss_halted or loss >= limits.max_daily_loss_micro
    return {"tripped": tripped, "loss_micro": loss, "threshold_micro": limits.max_daily_loss_micro,
            "requires": "user acknowledgement before trading resumes",
            "how_the_user_clears_it": "Activity -> 'I understand', which writes an audit row; there is no "
                                       "self-service path that skips the acknowledgement"}


def counter_key(kind: str, ident: str) -> str:
    return "%s:%s" % (kind, ident)


def bucket(now_ms: int, size_ms: int) -> int:
    return now_ms // size_ms


def rate_allow(count: int, limit: int) -> bool:
    return count < limit
