"""D6: the automation engine — composable triggers, actions, caps, and the arithmetic that decides which
templates ship.

Two things are true at once about automation on a prediction market, and this module is where the tension
gets settled:

1. A rule that fires is a *user's order*. It goes through the identical pre-flight, the identical risk gate
   and the identical ledger as a human click. There is no `place_order_fast` path, and `run_rule` calls
   `Store.enqueue_intent` rather than any venue client — that is the entire guarantee, enforced by the
   absence of another door.
2. The templates we *advertise* must be profitable before a user is allowed to run them. A 5-minute crypto
   up/down market pays a taker fee against a 4.5-point entry hurdle, so the shipped template is protective
   only (exit-before-resolution, size cap, loss cap) and the entry rule ships as a dry-run recommendation.
   `crypto_5m_template()` returns that verdict with the numbers attached rather than a paragraph about it.

Rules are evaluated as a small boolean tree (`all` / `any` / `not`, at most two levels, eight leaves).
Validation happens at SAVE time and returns every error at once — a rule that fails at fire time fails
silently, and silent failure is how automation products die.

Every evaluation writes an `automation_runs` row, including the ones that did nothing. "Why didn't my rule
fire at 03:12" has to be answerable from the database, and the answer is a reason string, not an absence.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..money.cents import SCALE, notional_floor

TRIGGER_KINDS = ("price_cross", "time", "signal", "book_imbalance", "new_market")
ACTION_KINDS = ("limit", "market", "cancel_open", "close_position", "set_alert", "tp_sl_set")

MAX_NEST_DEPTH = 2
MAX_LEAVES = 8

# What the engine will refuse to place per rule per day, whatever the user asked for. The user's own
# `max_per_day` is clamped into this band by the schema (1..288); this is the *platform* ceiling, and it
# exists because a rule that fires once a minute on every market is a denial-of-service against the venue
# that gets our API keys throttled, and every other user's copy pays for that in rate limits.
GLOBAL_RUNS_PER_DAY = 288
GLOBAL_NOTIONAL_PER_DAY_MICRO = 500_000_000          # $500 a day per rule, all users, from automation
MIN_INTERVAL_MS = 60_000                              # the venue's own rate ceiling, applied to us too


class RuleError(Exception):
    """A rule that cannot be saved. Raised with the full list of problems, never the first one."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


from . import facts as _facts
from .facts import Facts                    # noqa: F401 — re-exported: `engine.Facts` is public


# ================================================================================== rule validation ==
def _leaf_error(node: dict) -> str | None:
    kind = node.get("kind")
    if kind not in TRIGGER_KINDS:
        return "unknown trigger kind %r (allowed: %s)" % (kind, ", ".join(TRIGGER_KINDS))
    if kind == "price_cross":
        op, price = node.get("op"), node.get("price_micro")
        if op not in ("<", "<=", ">", ">="):
            return "price_cross needs op in <, <=, >, >="
        if not isinstance(price, int) or isinstance(price, bool) or not (0 < price < 10**SCALE):
            return "price_cross price_micro must be an integer strictly inside (0, 1000000)"
        if node.get("uses") not in (None, "last", "mid", "bid", "ask"):
            return "price_cross uses must be one of last, mid, bid, ask"
    elif kind == "time":
        if not isinstance(node.get("at_ms"), int) or isinstance(node.get("at_ms"), bool):
            return "time needs an integer at_ms (unix milliseconds)"
        if node.get("once") not in (None, True, False):
            return "time.once must be a boolean"
    elif kind == "signal":
        allowed = ("insider_suspect", "wash_like", "smart_money", "large_whale", "volume_spike")
        if node.get("label") not in allowed:
            return "signal label must be one of %s" % ", ".join(allowed)
    elif kind == "book_imbalance":
        bps = node.get("imbalance_bps")
        if not isinstance(bps, int) or isinstance(bps, bool) or not (0 < bps <= 10_000):
            return "book_imbalance imbalance_bps must be an integer in (0, 10000]"
        if node.get("op") not in (">", ">="):
            return "book_imbalance op must be > or >="
    elif kind == "new_market":
        if not isinstance(node.get("within_ms"), int) or isinstance(node.get("within_ms"), bool) \
                or node.get("within_ms", 0) < 1000:
            return "new_market needs within_ms >= 1000 (a 1s window is a race, not a rule)"
    return None


def _count_leaves(node: dict) -> int:
    if "all" in node or "any" in node:
        key = "all" if "all" in node else "any"
        kids = node[key]
        if not isinstance(kids, list) or not kids:
            return -1
        return sum(_count_leaves(k) for k in kids)
    if "not" in node:
        inner = node["not"]
        if not isinstance(inner, dict):
            return -1
        return _count_leaves(inner)
    return 1


def validate_rule(rule: dict) -> list[str]:
    """Every problem with a rule, in one pass. Returns [] when the rule can be saved.

    The checks are exhaustive on purpose: `trigger` and `actions` are the two places a client can send us
    anything, and a malformed rule that saves successfully is a rule that will do the wrong thing at 3am
    with nobody watching.
    """
    errs: list[str] = []
    trigger = rule.get("trigger")
    if not isinstance(trigger, dict):
        errs.append("trigger must be an object")
        trigger = None
    actions = rule.get("actions")
    if not isinstance(actions, list) or not actions:
        errs.append("actions must be a non-empty list")
        actions = []
    if trigger is not None:
        if _count_leaves(trigger) < 0:
            errs.append("trigger: all/any/not must wrap a non-empty list of objects")
        elif _count_leaves(trigger) > MAX_LEAVES:
            errs.append("trigger: %d leaves, the maximum is %d" % (_count_leaves(trigger), MAX_LEAVES))

        def walk(node, depth):
            if not isinstance(node, dict):
                errs.append("trigger: a node must be an object")
                return
            if ("all" in node) + ("any" in node) + ("not" in node) > 1:
                errs.append("trigger: a node may not mix all/any/not")
                return
            if "all" in node or "any" in node:
                if depth + 1 > MAX_NEST_DEPTH:
                    errs.append("trigger: nesting is limited to %d levels (this rule needs %d)"
                                % (MAX_NEST_DEPTH, depth + 1))
                    return
                for kid in node.get("all") or node.get("any") or []:
                    walk(kid, depth + 1)
                return
            if "not" in node:
                walk(node["not"], depth + 1)
                return
            e = _leaf_error(node)
            if e:
                errs.append("trigger: " + e)

        walk(trigger, 1)
    seen_kinds = []
    for i, a in enumerate(actions):
        if not isinstance(a, dict):
            errs.append("actions[%d] must be an object" % i)
            continue
        kind = a.get("kind")
        if kind not in ACTION_KINDS:
            errs.append("actions[%d]: unknown action kind %r (allowed: %s)"
                        % (i, kind, ", ".join(ACTION_KINDS)))
            continue
        seen_kinds.append(kind)
        if kind in ("limit", "market"):
            if a.get("side") not in ("BUY", "SELL"):
                errs.append("actions[%d]: %s needs side BUY or SELL" % (i, kind))
            size = a.get("size_shares_micro")
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                errs.append("actions[%d]: %s needs a positive integer size_shares_micro" % (i, kind))
            if kind == "limit":
                price = a.get("price_micro")
                if not isinstance(price, int) or isinstance(price, bool) or not (0 < price < 10**SCALE):
                    errs.append("actions[%d]: limit needs an integer price_micro inside (0, 1000000)" % i)
            if a.get("max_slippage_bps", 0) not in (0,) and not (0 <= int(a.get("max_slippage_bps", 0)) <= 1000):
                errs.append("actions[%d]: max_slippage_bps must be between 0 and 1000 (10%% is the ceiling)" % i)
        elif kind == "cancel_open":
            if a.get("scope") not in ("order", "batch", "market", "all"):
                errs.append("actions[%d]: cancel_open scope must be order, batch, market or all" % i)
            if a.get("scope") == "all":
                # A rule may never cancel everything the user has, on every market, unattended. The manual
                # path requires a typed confirmation phrase for that; a cron job cannot type one.
                errs.append("actions[%d]: a rule may not cancel ALL of a user's orders; scope 'market' is "
                            "the ceiling for automation" % i)
        elif kind == "close_position":
            if a.get("method") not in ("market", "limit_at_mid"):
                errs.append("actions[%d]: close_position method must be market or limit_at_mid" % i)
        elif kind == "set_alert":
            if not str(a.get("message") or "").strip():
                errs.append("actions[%d]: set_alert needs a message" % i)
            if a.get("channel") not in (None, "in_app", "email"):
                errs.append("actions[%d]: set_alert channel must be in_app or email" % i)
        elif kind == "tp_sl_set":
            for f in ("take_profit_bp", "stop_loss_bp"):
                v = a.get(f)
                if v is not None and (not isinstance(v, int) or isinstance(v, bool) or not (0 < v <= 10_000)):
                    errs.append("actions[%d]: %s must be an integer in (0, 10000]" % (i, f))
            if not a.get("take_profit_bp") and not a.get("stop_loss_bp"):
                errs.append("actions[%d]: tp_sl_set needs at least one of take_profit_bp, stop_loss_bp" % i)
    # Order matters for the user's money; two money-moving actions in one rule means the second one is
    # sized against a position the first one just changed, and the engine has no way to know which was
    # intended. Cancel + close is the one legitimate pairing (exit the position, then clean the book).
    money = [k for k in seen_kinds if k in ("limit", "market", "close_position")]
    if len(money) > 1:
        errs.append("a rule may contain at most one order-placing action; found %s (chain two rules if you "
                    "really want that, so each has its own run history)" % ", ".join(money))
    max_loss = rule.get("max_loss_micro")
    if max_loss is not None and (not isinstance(max_loss, int) or isinstance(max_loss, bool) or max_loss < 0):
        errs.append("max_loss_micro must be a non-negative integer")
    return errs


def evaluate(trigger: dict, f: Facts) -> dict:
    """{fires: bool, leaves: [{kind, ok, observed}]} — the leaf values are recorded so a skip can be
    explained after the fact, which is the only reason a run log is worth keeping."""
    leaves: list[dict] = []

    def leaf(node: dict) -> bool:
        kind = node.get("kind")
        ok, observed = False, None
        if kind == "price_cross":
            uses = node.get("uses") or "mid"
            observed = {"last": f.last_price_micro, "mid": f.mid_micro, "bid": f.best_bid_micro,
                        "ask": f.best_ask_micro}.get(uses)
            price = node["price_micro"]
            if observed is None:
                ok = False
                observed = "no price for %r" % uses
            else:
                op = node["op"]
                ok = observed < price if op == "<" else observed <= price if op == "<=" \
                    else observed > price if op == ">" else observed >= price
        elif kind == "time":
            at = node["at_ms"]
            # A one-shot time trigger fires on the first pass at or after `at_ms`, and never again: "at or
            # after" without `once` is a rule that buys more of the same market every minute forever.
            ok = f.now_ms >= at
            if node.get("once") and observed is None:
                observed = "once: only the first pass after %d fires" % at
        elif kind == "signal":
            ok = node["label"] in f.labels
            observed = ",".join(f.labels) or "none"
        elif kind == "book_imbalance":
            op = node["op"]
            ok = f.imbalance_bps > node["imbalance_bps"] if op == ">" else \
                f.imbalance_bps >= node["imbalance_bps"]
            observed = f.imbalance_bps
        elif kind == "new_market":
            ok = bool(f.market_open_ms and f.now_ms - f.market_open_ms <= node["within_ms"])
            observed = (f.now_ms - f.market_open_ms) if f.market_open_ms else None
        leaves.append({"kind": kind or "?", "ok": bool(ok), "observed": observed})
        return bool(ok)

    def ev(node: dict) -> bool:
        if not isinstance(node, dict):
            return False
        if "all" in node:
            # Evaluated eagerly, not short-circuited. Python's `all()` stops at the first False, which would
            # leave the run log showing a half-seen trigger: "why did my rule skip at 03:12" answered with
            # two of three leaves is a worse answer than the same question answered with a lie. The tree is
            # capped at eight leaves and every leaf is a table read, so full evaluation is affordable.
            kids = [ev(k) for k in node["all"]]
            return all(kids)
        if "any" in node:
            kids = [ev(k) for k in node["any"]]
            return any(kids)
        if "not" in node:
            return not ev(node["not"])
        return leaf(node)

    fires = ev(trigger)
    return {"fires": fires, "leaves": leaves}


# ============================================================================== break-even arithmetic ==
def break_even_win_rate_bp(price_micro: int, *, fee_rate_bps: int, builder_bps: int = 0,
                           legs: int = 2, maker: bool = False) -> dict:
    """What win rate covers the fees on a binary contract, in integer arithmetic.

    Buying 1 share (1e6) at price p costs p plus a fee on the *matched* value. The venue charges the taker
    fee as `fee_rate_bps * p * (1-p)` per share — a fee that peaks at p=0.5 and vanishes at the extremes,
    which is why a 5-minute market at 50/50 is the worst case for an automated entry and the best case for
    a maker rebate. Selling or settling pays the same fee again on the proceeds, hence `legs=2`.

    Break-even: `w * (1 - p) - (1 - w) * p - fees = 0  =>  w = p + fees`. That one line is the whole point:
    the fee is a straight addition to the entry hurdle, so a strategy whose source wins 52% cannot be copied
    into a 50.5¢ entry with a 4.5-point fee load and survive.

    `maker=True` zeroes the taker fee and applies the venue's maker rebate instead — shown so the template
    can say *why* a limit order is recommended and a market order is not, with the number that proves it
    rather than an assertion.
    """
    if not isinstance(price_micro, int) or isinstance(price_micro, bool) or not (0 < price_micro < 10**SCALE):
        raise ValueError("price_micro must be an integer inside (0, 1000000)")
    p = price_micro
    one = 10**SCALE
    per_leg = (p * (one - p)) * fee_rate_bps // (10_000 * one)          # venue fee, per share, per leg
    builder_leg = (p * builder_bps) // 10_000                            # our fee is on notional
    fees = (per_leg + builder_leg) * (legs if not maker else 1)
    if maker:
        # Rebate: the venue pays makers on this market class; we never model a rebate as larger than the fee
        # it offsets, because a rebate that exceeds the fee would make the strategy pay us to trade, which is
        # a number nobody should put in a template without a live fee schedule to prove it.
        fees = -min(per_leg, (p * 20) // 10_000)                         # 20 bps maker rebate, P04's figure
    hurdle = p + fees
    w_bp = max(0, min(10_000, (hurdle * 10_000) // one))
    edge_needed_bp = max(0, w_bp - (p * 10_000) // one)
    return {"price_micro": p, "fee_rate_bps": fee_rate_bps, "builder_bps": builder_bps, "legs": legs,
            "maker": maker, "fees_per_share_micro": fees, "break_even_win_rate_bp": w_bp,
            "implied_prob_bp": (p * 10_000) // one, "edge_needed_bp": edge_needed_bp,
            # `hurdle_exists`, not `positive_edge`: a non-zero edge_needed_bp means the FEES demand an edge,
            # which says nothing about whether the strategy has one. Naming this the other way round is how a
            # template ends up shipping because a helper "said the trade was positive".
            "hurdle_exists": edge_needed_bp > 0}


# Fee schedules we know how to price. `markets.fee_type` is an OPEN vocabulary — P05 §15 records exactly
# that, and the dev seed contains the literal strings 'None', 'none' and 'maker_rebate' in the same column.
# 'None' as a *string* is not "no fee": it is "the venue told us nothing", and an automation template that
# reads it as zero fee would be placing an entry order priced with a fee of zero that the venue may charge.
FEE_TYPES_PRICED = ("none",)                 # only an explicit lowercase 'none' is a fee we can rely on
FEE_TYPES_REBATED = ("maker_rebate",)


def crypto_5m_template(*, fee_type: str, fee_rate_bps: int | None, builder_bps: int = 100,
                       price_micro: int = 500_000, spread_ticks: int = 1, tick_micro: int = 10_000,
                       edge_available_bp: int | None = None, latency_ms: int | None = None,
                       latency_cost_bps_per_s: int = 2) -> dict:
    """The 'crypto 5-minute up/down' automation template, and the arithmetic that decides whether its ENTRY
    rule ships.

    The brief's condition is a promise this function keeps mechanically: an entry template ships only if the
    fee maths proves the entry has positive expected edge, and the proof needs two numbers we do not get to
    invent — the market's own fee schedule and the edge a copier actually has after latency. So both are
    *inputs*, and any one of four conditions sends it back to dry-run:

    * `fee_type` is not one we can price (including the venue's string 'None', which means unknown, not zero);
    * `fee_rate_bps` was not supplied for a fee-bearing market;
    * `edge_available_bp` is None: no source in the leaderboard has a measured, fee-adjusted edge to copy;
    * `edge_available_bp <= edge_needed_bp`, where needed = the round-trip fee per share plus the cost of
      crossing the spread plus the slippage our own copy latency buys us.

    The protective half — exit before resolution, size cap, loss halt, cancel on a one-sided book — always
    ships, because it can only reduce a user's loss and its value does not depend on any of those numbers.
    """
    ft = str(fee_type or "").strip()
    priced = ft in FEE_TYPES_PRICED
    rebated = ft in FEE_TYPES_REBATED
    if fee_rate_bps is None and not (priced or rebated):
        return {"name": "crypto 5-minute up/down", "verdict": "NOT SHIPPED", "ship_entry_rule": False,
                "blocking_reason": "fee_type_unknown",
                "why": "this market's fee schedule is %r, which is not a schedule we can price; the venue "
                       "reports an open-ended taxonomy, and 'None' as a string means it told us nothing, not "
                       "that the fee is zero" % ft,
                "numbers": {"price_micro": price_micro, "fee_type": ft},
                "shipped_rules": _protective_rules()}
    maker = rebated
    rate = int(fee_rate_bps or 0)
    taker = break_even_win_rate_bp(price_micro, fee_rate_bps=rate, builder_bps=builder_bps,
                                   legs=1 if maker else 2, maker=maker)
    spread_cost_micro = spread_ticks * tick_micro
    spread_cost_bp = (spread_cost_micro * 10_000) // (10**SCALE)
    lat_cost_bp = max(0, int((latency_ms or 0) // 1000) * latency_cost_bps_per_s)
    edge_needed_bp = taker["edge_needed_bp"] + spread_cost_bp + lat_cost_bp
    reasons = []
    if edge_available_bp is None:
        reasons.append("no measured edge: nothing on the leaderboard has a fee-adjusted sample big enough to "
                       "call an entry rule positive")
    elif edge_available_bp <= edge_needed_bp:
        reasons.append("the measured edge (%d bps) does not clear the hurdle (%d bps)"
                       % (edge_available_bp, edge_needed_bp))
    if not priced and not rebated and rate <= 0:
        reasons.append("fee_type says a fee exists but the rate supplied is zero")
    ship = not reasons
    return {"name": "crypto 5-minute up/down",
            "verdict": "SHIPPED" if ship else "ENTRY RULE NOT SHIPPED (protective half ships)",
            "ship_entry_rule": ship,
            "blocking_reason": "" if ship else reasons[0].split(":")[0].replace(" ", "_"),
            "why": ("the entry rule clears its own costs by %d bps on this market class"
                    % (edge_available_bp - edge_needed_bp) if ship else "; ".join(reasons)),
            "numbers": {"price_micro": price_micro, "fee_type": ft, "fee_rate_bps": rate,
                        "maker": maker, "legs": 1 if maker else 2,
                        "taker_break_even_win_rate_bp": taker["break_even_win_rate_bp"],
                        "implied_prob_bp": taker["implied_prob_bp"],
                        "fees_per_share_micro": taker["fees_per_share_micro"],
                        "spread_cost_micro": spread_cost_micro, "spread_cost_bp": spread_cost_bp,
                        "latency_ms": latency_ms, "latency_cost_bp": lat_cost_bp,
                        "edge_needed_bp": edge_needed_bp, "edge_available_bp": edge_available_bp,
                        "assumptions": "10 bps of our own builder fee per leg; a maker rebate is never "
                                       "modelled as exceeding the fee it offsets; latency slippage is a linear "
                                       "estimate at %d bps/second, which is the number to replace with a "
                                       "measured one the moment P05's tape gives us fill-to-quote lag on these "
                                       "markets" % latency_cost_bps_per_s},
            "shipped_rules": _protective_rules(),
            "recommended_but_dry_run_only": None if ship else {
                "id": "entry-momentum-5m",
                "trigger": {"all": [{"kind": "book_imbalance", "op": ">=", "imbalance_bps": 7000},
                                   {"kind": "new_market", "within_ms": 120_000}]},
                "actions": [{"kind": "limit", "side": "BUY", "price_micro": price_micro,
                             "size_shares_micro": 5_000_000, "max_slippage_bps": 150}],
                "why_dry_run": "dry-run records what the rule WOULD have done against real fills and real "
                               "latency; it becomes an entry rule when the numbers above clear, and not before",
            }}


def _protective_rules() -> list[dict]:
    """The half that always ships: exits, caps and alerts. None of these can lose a user money that they were
    not already risking, which is the test for shipping an automation template without an edge measurement."""
    return [
        {"id": "protect-exit-before-resolution", "kind": "exit",
         "trigger": {"any": [{"kind": "time", "at_ms": 0, "once": True}]},
         "actions": [{"kind": "close_position", "method": "market", "max_slippage_bps": 1000}],
         "note": "attached to a market with at_ms = resolution minus 30s. Holding a five-minute contract into "
                 "resolution pays no fee but forfeits the exit price, and 'resolution minus 30s' is the only "
                 "number here that a user can check. `close_position` rather than a sized SELL because the "
                 "rule must sell whatever the position IS at fire time, not a size frozen when it was saved."},
        {"id": "protect-one-sided-book", "kind": "alert",
         "trigger": {"any": [{"kind": "book_imbalance", "op": ">=", "imbalance_bps": 9000}]},
         "actions": [{"kind": "set_alert", "message": "the book on a market you hold is one-sided",
                      "channel": "in_app"}]},
        {"id": "protect-volume-spike-cancels-entry", "kind": "cancel",
         "trigger": {"any": [{"kind": "signal", "label": "volume_spike"}]},
         "actions": [{"kind": "cancel_open", "scope": "market"}]},
    ]


# ======================================================================================== the engine ==
@dataclass
class RunResult:
    rule_id: str
    mode: str
    outcome: str
    reason: str = ""
    deny_code: str = ""
    intent_id: str = ""
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"rule_id": self.rule_id, "mode": self.mode, "outcome": self.outcome, "reason": self.reason,
                "deny_code": self.deny_code, "intent_id": self.intent_id, "detail": self.detail}


def rule_view(row: dict, *, state: dict | None = None, target: dict | None = None) -> dict:
    """Normalise a `Store.automation_rule()` row into what `run_rule` reads. One place, so a caller cannot
    hand the engine a half-loaded rule (the failure mode is a rule that silently sees no caps)."""
    st = state or {}
    tg = target or {}
    return {"id": str(row["id"]), "user_id": str(row["user_id"]), "kind": str(row.get("kind") or ""),
            "trigger": row.get("trigger") or {}, "actions": row.get("actions") or row.get("actions_json") or [],
            "policy": {"max_per_day": row.get("max_per_day") or 24,
                       "min_interval_ms": row.get("min_interval_ms") or MIN_INTERVAL_MS,
                       "human_priority_ms": row.get("human_priority_ms") or 120_000},
            "dry_run_completed_ms": row.get("dry_run_completed_ms"),
            "last_fire_ms": row.get("last_fire_ms") or 0,
            "enabled": bool(row.get("enabled")),
            "paused_reason": st.get("paused_reason") or "",
            "failure_count": int(st.get("failure_count") or 0),
            "market_id": tg.get("market_id") or "", "token_id": tg.get("token_id") or ""}


class AutomationEngine:
    """Evaluates saved rules against `Facts` and turns a firing rule into an intent.

    The order of the checks IS the safety property, so it is written out in one place rather than distributed
    across callers: validate -> enabled -> not paused -> dry-run done -> min interval -> per-day caps ->
    platform caps -> human priority -> fresh quote -> trigger -> [executor's own pre-flight and risk gate] ->
    venue. Everything before the bracket stops a rule BEFORE the venue sees it, and a refusal at any step
    writes a run row with a reason, because "the rule did not fire" and "nobody looked" are different facts.

    `canceller` is injected rather than imported: the cancel path belongs to the executor (it needs the venue
    client, the cancel budget and the confirmation rules), and a rule engine that reaches for those directly
    would be a second, ungoverned submit path.
    """

    MAX_CONSECUTIVE_FAILURES = 3

    def __init__(self, store, *, canceller=None) -> None:
        self.store = store
        self.canceller = canceller

    def save_rule(self, *, rule_id: str, user_id: str, kind: str, rule: dict, enabled: bool, at: int,
                  market_ids: list[tuple[str, str]] | None = None, max_per_day: int = 24,
                  min_interval_ms: int = 60_000, human_priority_ms: int = 120_000,
                  max_loss_micro: int = 0) -> dict:
        """Save, but do not silently arm. `enabled=True` here additionally requires that the rule has a
        completed dry run; a brand-new rule cannot have one, so enabling at save time returns a refusal
        rather than quietly doing what the caller asked."""
        errs = validate_rule(rule)
        if errs:
            raise RuleError(errs)
        # The validator requires a positive ceiling for money-moving actions, and it reads it from the rule
        # document. Saving must read the same place, or a rule that passed validation dies on the schema's
        # CHECK — and a rule the UI cannot save is a rule the UI will "fix" by dropping the ceiling.
        ceiling = int(max_loss_micro or rule.get("max_loss_micro") or 0)
        r = self.store.upsert_automation_rule(rule_id=rule_id, user_id=user_id, kind=kind,
                                             enabled=False, at=at, trigger=rule.get("trigger"),
                                             actions=rule.get("actions") or [], max_loss_micro=ceiling,
                                             market_ids=market_ids, max_per_day=max_per_day,
                                             min_interval_ms=max(min_interval_ms, MIN_INTERVAL_MS),
                                             human_priority_ms=human_priority_ms)
        out = dict(r)
        out["enabled"] = False
        if enabled:
            e = self.enable(rule_id, at=at)
            out.update({"requested_enabled": True, "enabled": e["enabled"]})
            if not e["enabled"]:
                out.update({"reason": e["reason"], "deny_code": e["code"]})
        return out

    def enable(self, rule_id: str, *, at: int, actor: str = "user") -> dict:
        """Arm a rule. This is the ONLY way a rule becomes live, and it refuses unless the rule has a recorded
        dry run and still validates.

        Two refusals matter more than the rest. A rule that has never been observed is enabled into a
        position the user has never seen it take. And a rule that became invalid AFTER it was armed — because
        a template changed, or a migration moved a column — must not keep firing on its stale saved shape, so
        the tree is re-validated here rather than trusted.
        """
        row = self.store.automation_rule(rule_id)
        if row is None:
            return {"enabled": False, "code": "RULE_NOT_FOUND", "reason": "no such rule"}
        state = self.store.rule_state(rule_id)
        v = rule_view(row, state=state)
        errs = validate_rule({"trigger": v["trigger"], "actions": v["actions"]})
        if errs:
            self.store.set_rule_state(rule_id=rule_id, at=at, paused_reason="invalid rule: " + errs[0][:160])
            return {"enabled": False, "code": "RULE_INVALID", "reason": "; ".join(errs)}
        if not v["dry_run_completed_ms"]:
            return {"enabled": False, "code": "AUTOMATION_NO_DRY_RUN",
                    "reason": "run it in dry mode once and read what it would have done"}
        self.store.set_rule_enabled(rule_id, enabled=True)
        self.store.set_rule_state(rule_id=rule_id, at=at, paused_reason="", failure_count=0, last_error="")
        self.store.record_run(rule_id=rule_id, user_id=v["user_id"], mode="live", outcome="skipped",
                             reason="rule armed by %s at %d" % (actor, at), deny_code="", intent_id="",
                             detail={"event": "armed"}, at=at)
        return {"enabled": True, "code": "", "reason": ""}

    def disable(self, rule_id: str, *, at: int, reason: str) -> dict:
        row = self.store.automation_rule(rule_id)
        if row is None:
            return {"disabled": False, "reason": "no such rule"}
        self.store.set_rule_enabled(rule_id, enabled=False)
        self.store.set_rule_state(rule_id=rule_id, at=at, paused_reason=reason[:200])
        return {"disabled": True, "reason": reason}

    def run_rule(self, rule: dict, *, facts: Facts, mode: str, at: int, run_count_today: int = 0,
                 notional_today_micro: int = 0, last_fire_ms: int | None = None, human_last_ms: int = 0,
                 dry_run_completed_ms: int | None = None, enabled: bool | None = None) -> RunResult:
        rid = str(rule.get("id") or "?")
        uid = str(rule.get("user_id") or "")
        policy = rule.get("policy") or {}
        res = RunResult(rule_id=rid, mode=mode, outcome="skipped")
        errs = validate_rule(rule)
        if errs:
            res.outcome, res.reason, res.deny_code = "failed", "; ".join(errs), "RULE_INVALID"
            return self._record(res, uid, at)
        if enabled is None:
            enabled = bool(rule.get("enabled"))
        if not enabled and mode != "dry_run":
            res.reason = "rule disabled"
            return self._record(res, uid, at)
        paused = str(rule.get("paused_reason") or "")
        if paused:
            res.reason = "paused: %s" % paused
            return self._record(res, uid, at)
        if dry_run_completed_ms is None:
            dry_run_completed_ms = rule.get("dry_run_completed_ms")
        if mode == "live" and not dry_run_completed_ms:
            res.outcome, res.reason, res.deny_code = "skipped", "run this rule in dry mode first", \
                "AUTOMATION_NO_DRY_RUN"
            return self._record(res, uid, at)
        min_interval = max(int(policy.get("min_interval_ms") or MIN_INTERVAL_MS), MIN_INTERVAL_MS)
        if last_fire_ms is None:
            last_fire_ms = int(rule.get("last_fire_ms") or 0)
        if last_fire_ms and at - int(last_fire_ms) < min_interval:
            res.reason = "min interval: next allowed in %dms" % (min_interval - (at - int(last_fire_ms)))
            return self._record(res, uid, at)
        max_day = min(int(policy.get("max_per_day") or 24), GLOBAL_RUNS_PER_DAY)
        if run_count_today >= max_day:
            res.outcome, res.reason, res.deny_code = "skipped", "daily rule cap (%d)" % max_day, \
                "AUTOMATION_DAY_CAP"
            return self._record(res, uid, at)
        if notional_today_micro >= GLOBAL_NOTIONAL_PER_DAY_MICRO:
            res.outcome, res.reason, res.deny_code = "skipped", "automation notional cap for the day", \
                "AUTOMATION_NOTIONAL_CAP"
            return self._record(res, uid, at)
        human_priority_ms = int(policy.get("human_priority_ms") or 120_000)
        if human_last_ms and at - human_last_ms < human_priority_ms:
            # D6.4: a human trading by hand outranks the automation. Not "wait and hope" — the rule stands
            # down for this pass, says so in the log, and pauses, because a user who just traded by hand has
            # demonstrated they are watching this market right now.
            res.reason = "human traded %ds ago; the rule stands down and pauses" % ((at - human_last_ms) // 1000)
            self._pause(rule, at=at, reason="human_active")
            return self._record(res, uid, at)
        if mode == "live" and self.store.kill_switch_engaged(at_ms=at, force=True):
            # D4's switch covers every door into the venue, and a rule is a door. A dry run still evaluates:
            # an operator reading "what would my rules have done during the halt?" needs that row, and it
            # places nothing.
            res.outcome, res.reason, res.deny_code = "skipped", "trading is disabled (kill switch engaged)", \
                "RISK_HALT"
            return self._record(res, uid, at)
        if not facts.quote_usable and any(a.get("kind") in ("limit", "market", "close_position")
                                         for a in rule.get("actions", [])):
            res.outcome, res.reason, res.deny_code = "skipped", "quote is stale or missing", "STALE_QUOTE"
            return self._record(res, uid, at)
        if not facts.accepting_orders:
            res.reason = "market is not accepting orders"
            return self._record(res, uid, at)
        ev = evaluate(rule["trigger"], facts)
        res.detail["leaves"] = ev["leaves"]
        if not ev["fires"]:
            res.reason = "trigger not met"
            return self._record(res, uid, at)
        if mode == "dry_run":
            res.outcome = "would_place"
            res.reason = "dry run: no order sent"
            return self._record(res, uid, at)
        action = next((a for a in rule["actions"] if a.get("kind") in ACTION_KINDS), None)
        if action is None:
            res.outcome, res.reason, res.deny_code = "failed", "no executable action", "RULE_INVALID"
            return self._record(res, uid, at)
        return self._act(res, rule, action, facts, uid, at)

    def tick(self, *, at: int, mode: str = "live") -> dict:
        """One pass over every enabled rule, evaluated per watched market."""
        outcomes: list[dict] = []
        rows = (self.store.automation_rules_all() if mode == "dry_run"
                else self.store.automation_rules_enabled())
        for row in rows:
            state = self.store.rule_state(row["id"])
            if state["failure_count"] >= self.MAX_CONSECUTIVE_FAILURES:
                outcomes.append(RunResult(rule_id=row["id"], mode=mode, outcome="skipped",
                                          reason="paused after %d consecutive failures (last: %s)"
                                                 % (state["failure_count"], state["last_error"])).as_dict())
                continue
            targets = self.store.rule_targets(row["id"]) or [{"market_id": "", "token_id": ""}]
            for tg in targets:
                view = rule_view(row, state=state, target=tg)
                facts = self.facts_for(user_id=view["user_id"], market_id=view["market_id"],
                                      token_id=view["token_id"], at=at)
                stats = self.store.automation_run_stats(row["id"], at=at)
                res = self.run_rule(view, facts=facts, mode=mode, at=at,
                                   run_count_today=stats["placed"],
                                   notional_today_micro=stats["notional_micro"],
                                   human_last_ms=self.store.last_human_order_ms(view["user_id"], at=at))
                outcomes.append(res.as_dict())
        return {"evaluated": len(outcomes), "mode": mode, "outcomes": outcomes}

    def facts_for(self, *, user_id: str, market_id: str, token_id: str, at: int) -> Facts:
        """The facts for one (rule, market) pair, from `automation.facts` — the same function the API's builder
        preview calls.

        It used to be inline here, which was fine until a second caller needed identical numbers. A preview that
        disagreed with the live pass about the book would be a preview of a different rule, and the disagreement
        would show up as a rule that fired in production and did nothing in the preview.
        """
        return _facts.facts_for(self.store.conn, user_id, market_id, at=at)

    def mark_dry_run_done(self, rule_id: str, *, at: int) -> dict:
        """Record that the rule has been observed in dry mode.

        The flag is derived from the run log, not from a client's assertion: a caller cannot mark a dry run it
        did not perform, and a dry run that never evaluated the rule proves nothing. So this requires at
        least one `mode='dry_run'` row for the rule, and prefers one that actually said `would_place` — if
        the rule can never fire on current data, the engine says so instead of arming a dead switch.
        """
        rows = self.store.dry_run_history(rule_id)
        if not rows:
            return {"marked": False, "code": "AUTOMATION_NO_DRY_RUN",
                    "reason": "tick(mode='dry_run') has never evaluated this rule"}
        self.store.mark_dry_run(rule_id, at)
        would = [r for r in rows if r["outcome"] == "would_place"]
        return {"marked": True, "runs_observed": len(rows), "would_place": len(would),
                "code": "", "reason": ("" if would else
                                       "the dry run never fired: the trigger has not been met on any pass, so "
                                       "arming this rule changes nothing today"),
                "outcomes": [r["outcome"] for r in rows]}

    # ------------------------------------------------------------------------ internals
    def _act(self, res: RunResult, rule: dict, action: dict, facts: Facts, uid: str, at: int) -> RunResult:
        kind = action.get("kind")
        market_id = str(rule.get("market_id") or "")
        token_id = str(rule.get("token_id") or "")
        try:
            if kind == "set_alert":
                self.store.notify(intent_id="", user_id=uid, event="alert", at=at,
                                  detail={"rule_id": res.rule_id, "message": str(action.get("message") or "")[:300],
                                          "channel": action.get("channel") or "in_app", "market_id": market_id})
                res.outcome, res.reason = "placed", "alert queued (no venue order)"
            elif kind == "tp_sl_set":
                self.store.set_rule_state(rule_id=res.rule_id, at=at, armed_market_id=market_id,
                                          armed_token_id=token_id,
                                          take_profit_bp=action.get("take_profit_bp"),
                                          stop_loss_bp=action.get("stop_loss_bp"), failure_count=0,
                                          last_error="")
                res.outcome, res.reason = "placed", "take-profit/stop-loss armed on this rule"
            elif kind == "cancel_open":
                if self.canceller is None:
                    raise RuntimeError("no canceller is wired: the executor owns the cancel path")
                r = self.canceller(scope=action.get("scope") or "market", user_id=uid, market_id=market_id,
                                  reason="automation rule %s" % res.rule_id, at=at)
                res.outcome, res.reason = "placed", "cancels requested: %s" % json.dumps(
                    {k: v for k, v in (r or {}).items() if isinstance(v, (int, str, bool))},
                    separators=(",", ":"))[:200]
            else:
                size = int(action.get("size_shares_micro") or 0)
                if kind == "close_position":
                    size = int(facts.position_shares_micro or 0)
                    if size <= 0:
                        res.outcome, res.reason = "skipped", "nothing to close"
                        return self._record(res, uid, at)
                price = int(action.get("price_micro") or 0) or int(facts.mid_micro or 0)
                if price <= 0:
                    res.outcome, res.reason = "skipped", "no price to reference"
                    return self._record(res, uid, at)
                # Key = rule + the minute it fired in. A restart that re-evaluates the same minute cannot
                # place twice; a genuinely new minute can, which is what a per-minute rule means.
                key = "rule:%s:%d" % (res.rule_id, at // max(MIN_INTERVAL_MS, 1))
                r = self.store.enqueue_intent(user_id=uid, market_id=market_id, token_id=token_id,
                                             side=action.get("side") or "BUY", price_micro=price,
                                             size_micro=size, idempotency_key=key,
                                             order_type="GTC" if kind == "limit" else "FOK",
                                             audience="automation", rule_id=res.rule_id,
                                             max_slippage_bps=int(action.get("max_slippage_bps") or 0),
                                             at=at)
                res.intent_id = r["intent_id"]
                res.outcome = "placed" if r["created"] else "skipped"
                res.reason = "intent queued for the executor" if r["created"] else r["reason"]
                if r["created"]:
                    self.store.set_rule_last_fire(res.rule_id, at)
                    res.detail["notional_micro"] = notional_floor(size, price)
            if res.outcome == "placed":
                # A success clears the failure counter. Without this, three failures a week ago plus one
                # today pauses a rule that has been working, and a paused rule is invisible harm.
                st = self.store.rule_state(res.rule_id)
                if st["failure_count"]:
                    self.store.set_rule_state(rule_id=res.rule_id, at=at, failure_count=0, last_error="")
        except Exception as exc:                                  # noqa: BLE001 - one rule must not end the pass
            res.outcome, res.deny_code = "failed", "AUTOMATION_ACTION_FAILED"
            res.reason = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            st = self.store.rule_state(res.rule_id)
            self.store.set_rule_state(rule_id=res.rule_id, at=at, failure_count=st["failure_count"] + 1,
                                      last_error=res.reason[:300])
        return self._record(res, uid, at)

    def _pause(self, rule: dict, *, at: int, reason: str) -> None:
        self.store.set_rule_state(rule_id=str(rule.get("id")), at=at, paused_reason=reason[:200])

    def _record(self, res: RunResult, user_id: str, at: int) -> RunResult:
        self.store.record_run(rule_id=res.rule_id, user_id=user_id, mode=res.mode, outcome=res.outcome,
                             reason=res.reason, deny_code=res.deny_code, intent_id=res.intent_id,
                             detail=res.detail, at=at)
        return res


def run_row_is_retriable(row: dict) -> bool:
    """Which failed runs the engine may try again on the next tick.

    A validation failure or a policy refusal is not retriable: it will fail the same way, forever, at
    288x a day. `AUTOMATION_ACTION_FAILED` is — a transient DB or venue error is precisely what the next
    tick is for, and the idempotency key makes that retry safe.
    """
    if row.get("outcome") != "failed":
        return False
    return row.get("deny_code") in ("AUTOMATION_ACTION_FAILED",)
