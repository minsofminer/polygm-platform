"""D8's API-side logic: what a rule is, why it is in the state it is, and what the visual builder may express.

The engine (`engine.py`) decides whether a rule fires. This module decides what a *screen* is allowed to say
and to offer, and it is pure for the same reason the rest of this repo's decision code is pure: the interesting
claims — "a new rule cannot be live", "every run row carries a reason", "the builder cannot express something
the engine would refuse" — are claims about functions, and they can be tested without a venue, a clock or a
database.

Three things are deliberate rather than incidental:

1. **The builder's vocabulary IS the engine's vocabulary.** `BUILDER_TRIGGERS` and `BUILDER_ACTIONS` are
   generated from `TRIGGER_KINDS` / `ACTION_KINDS` (and the field lists the validator enforces), and
   `validate_builder` compiles rows into the engine's own rule document and then hands it to `validate_rule`.
   A UI that offered a trigger the engine refuses would fail at fire time, which is the silent-failure shape
   D8 exists to prevent: a rule that dies unnoticed is worse than a rule that cannot be saved.
2. **A row is not an expression.** The builder sends rows and a joiner (`all`/`any`), never a string; there is
   no parser here because there is nothing to parse. The one nesting the engine allows (depth 2) is a group
   row, and a group row is still rows.
3. **The engine's own sentence is what the user reads.** `reason_sentence` returns the recorded `reason`
   verbatim and adds a next step only for the deny codes where a user can actually do something. Re-wording
   the engine's explanation here would mean two vocabularies for one decision, and the run log is the place
   where that divergence becomes a support ticket.
"""
from __future__ import annotations

from .engine import (ACTION_KINDS, GLOBAL_RUNS_PER_DAY, MAX_LEAVES, MAX_NEST_DEPTH, MIN_INTERVAL_MS,
                     TRIGGER_KINDS, RuleError, crypto_5m_template, evaluate, validate_rule)

#: The four states a rule can be in, in the order a list should show them: the ones that act on their own first,
#: then the ones a human parked, then the ones that have never run for real, then the ones the risk service
#: stopped. A "halted" rule is not a "paused" one: the first was stopped BY us, and the distinction is the whole
#: point of the daily-loss banner.
STATUSES = ("active", "paused", "dry_run", "halted")

#: What each action kind means, in one sentence, for the builder's action row. The engine's validator is the
#: authority on the fields; this is the copy that makes a row readable, and it is here (not in the web app) so
#: the API's own templates and the screen cannot describe the same action differently.
ACTION_COPY = {
    "limit": "place a limit order at a price you name",
    "market": "place a market order now",
    "cancel_open": "cancel the orders this rule's market already has",
    "close_position": "sell whatever the position is when the rule fires",
    "set_alert": "send an alert instead of trading",
    "tp_sl_set": "arm a take-profit and a stop-loss on the position",
}

TRIGGER_COPY = {
    "price_cross": "a price crosses a level you name",
    "time": "a clock time arrives",
    "signal": "one of our signals appears on the market",
    "book_imbalance": "the book becomes one-sided",
    "new_market": "a market is younger than a window you name",
}

#: The builder's field schema, per row kind. `type` is what the control is (a select of `options`, a price in
#: micro, a duration in ms, a boolean); `min`/`max` are the engine's own bounds so the control can refuse in the
#: UI what `validate_rule` would refuse at save time. Nothing here is an expression: every field is a literal.
BUILDER_TRIGGERS: dict[str, list[dict]] = {
    "price_cross": [
        {"name": "uses", "label": "price", "type": "select", "options": ["mid", "last", "bid", "ask"],
         "default": "mid"},
        {"name": "op", "label": "crosses", "type": "select", "options": ["<", "<=", ">", ">="],
         "default": "<="},
        {"name": "price_micro", "label": "level", "type": "price", "min": 1, "max": 999_999, "required": True},
    ],
    "time": [
        {"name": "at_ms", "label": "at", "type": "time_ms", "required": True},
        {"name": "once", "label": "only once", "type": "bool", "default": True},
    ],
    "signal": [
        {"name": "label", "label": "signal", "type": "select", "required": True,
         "options": ["smart_money", "insider_suspect", "wash_like", "large_whale", "volume_spike"]},
    ],
    "book_imbalance": [
        {"name": "op", "label": "is", "type": "select", "options": [">", ">="], "default": ">="},
        {"name": "imbalance_bps", "label": "one-sided by", "type": "percent", "min": 1, "max": 10_000,
         "required": True},
    ],
    "new_market": [
        {"name": "within_ms", "label": "within", "type": "duration_ms", "min": 1_000, "required": True},
    ],
}

BUILDER_ACTIONS: dict[str, list[dict]] = {
    "limit": [
        {"name": "side", "label": "side", "type": "select", "options": ["BUY", "SELL"], "required": True},
        {"name": "price_micro", "label": "limit price", "type": "price", "min": 1, "max": 999_999,
         "required": True},
        {"name": "size_shares_micro", "label": "size", "type": "shares", "min": 1, "required": True},
        {"name": "max_slippage_bps", "label": "max slippage", "type": "bps", "min": 0, "max": 1_000,
         "default": 100},
    ],
    "market": [
        {"name": "side", "label": "side", "type": "select", "options": ["BUY", "SELL"], "required": True},
        {"name": "size_shares_micro", "label": "size", "type": "shares", "min": 1, "required": True},
        {"name": "max_slippage_bps", "label": "max slippage", "type": "bps", "min": 0, "max": 1_000,
         "default": 100},
    ],
    "cancel_open": [
        # `all` is deliberately absent: the engine refuses a user's own rule cancelling every order they have.
        # Offering it in a select and refusing it at save time is how a user learns to distrust the form.
        {"name": "scope", "label": "cancel", "type": "select", "options": ["order", "batch", "market"],
         "default": "market", "required": True},
    ],
    "close_position": [
        {"name": "method", "label": "how", "type": "select", "options": ["market", "limit_at_mid"],
         "default": "market", "required": True},
        {"name": "max_slippage_bps", "label": "max slippage", "type": "bps", "min": 0, "max": 1_000,
         "default": 100},
    ],
    "set_alert": [
        {"name": "message", "label": "message", "type": "text", "max": 200, "required": True},
        {"name": "channel", "label": "channel", "type": "select", "options": ["in_app", "email"],
         "default": "in_app", "required": True},
    ],
    "tp_sl_set": [
        {"name": "take_profit_bp", "label": "take profit", "type": "bp", "min": 1, "max": 10_000},
        {"name": "stop_loss_bp", "label": "stop loss", "type": "bp", "min": 1, "max": 10_000},
    ],
}

#: Joiners, in the words a user sees. `all` is AND, `any` is OR — and the reason the API returns these rather
#: than the UI hardcoding them is that a joiner is a semantic decision, not a label.
JOINERS = {"all": "AND", "any": "OR"}


def builder_vocabulary() -> dict:
    """Everything the visual builder needs to render itself, including the engine's own limits.

    The limits ride along so the form can stop a rule the engine would refuse (nine leaf rows, three levels of
    nesting, a 30-second interval) *while the user can still fix it*, instead of answering a save with a 422
    that names a field they cannot see.
    """
    return {
        "triggers": [{"kind": k, "label": TRIGGER_COPY.get(k, k), "fields": BUILDER_TRIGGERS.get(k, [])}
                     for k in TRIGGER_KINDS],
        "actions": [{"kind": k, "label": ACTION_COPY.get(k, k), "fields": BUILDER_ACTIONS.get(k, [])}
                    for k in ACTION_KINDS],
        "joiners": JOINERS,
        "limits": {"maxLeaves": MAX_LEAVES, "maxNestDepth": MAX_NEST_DEPTH, "minIntervalMs": MIN_INTERVAL_MS,
                   "maxRunsPerDay": GLOBAL_RUNS_PER_DAY},
        "note": ("every row is a literal: a price, a level, a duration. Nothing here is an expression, and the "
                 "engine refuses anything this vocabulary cannot express."),
    }


def _leaf_from_row(row: dict) -> tuple[dict | None, str | None]:
    if not isinstance(row, dict):
        return None, "each trigger row must be an object"
    kind = row.get("kind")
    if kind not in TRIGGER_KINDS:
        return None, "unknown trigger %r (the builder offers %s)" % (kind, ", ".join(TRIGGER_KINDS))
    allowed = {f["name"] for f in BUILDER_TRIGGERS.get(kind, [])}
    out = {"kind": kind}
    for key, value in row.items():
        if key in ("kind", "group", "rows"):
            continue
        if key not in allowed:
            return None, "trigger %r has no field %r (the builder offers %s)" % (kind, key, ", ".join(sorted(allowed)))
        out[key] = value
    return out, None


def compile_builder(payload: dict) -> tuple[dict, list[str]]:
    """Rows -> the engine's rule document, plus every problem found. Never raises, never returns the first
    problem only: a form that names one error per submit teaches people to submit four times.

    Shape: `{match: "all"|"any", triggers: [row | {group: "any", rows: [...]}], actions: [...], targets: [...]}`
    where a row is `{kind, ...fields}`. Nesting is one group deep — the engine's `MAX_NEST_DEPTH` is 2, and the
    builder's depth is the tree's depth, so a group inside a group is what "too deep" means in rows.
    """
    errs: list[str] = []
    match = payload.get("match") or "all"
    if match not in JOINERS:
        errs.append("match must be %s" % " or ".join(sorted(JOINERS)))

    def rows_to_nodes(rows, depth: int) -> list:
        nodes = []
        if not isinstance(rows, list) or not rows:
            errs.append("a trigger group needs at least one row")
            return nodes
        for row in rows:
            if isinstance(row, dict) and "group" in row:
                if depth + 1 > MAX_NEST_DEPTH:
                    errs.append("a group may not sit inside another group (the engine evaluates %d levels)"
                                % MAX_NEST_DEPTH)
                    continue
                if row.get("group") not in JOINERS:
                    errs.append("a group's joiner must be %s" % " or ".join(sorted(JOINERS)))
                    continue
                nodes.append({row["group"]: rows_to_nodes(row.get("rows"), depth + 1)})
                continue
            leaf, err = _leaf_from_row(row)
            if err:
                errs.append(err)
                continue
            nodes.append(leaf)
        return nodes

    trigger_nodes = rows_to_nodes(payload.get("triggers"), 1)
    trigger = {match: trigger_nodes} if match in JOINERS else {"all": trigger_nodes}

    actions = []
    for i, a in enumerate(payload.get("actions") or []):
        if not isinstance(a, dict):
            errs.append("actions[%d] must be an object" % i)
            continue
        kind = a.get("kind")
        if kind not in ACTION_KINDS:
            errs.append("actions[%d]: unknown action %r (the builder offers %s)"
                        % (i, kind, ", ".join(ACTION_KINDS)))
            continue
        allowed = {f["name"] for f in BUILDER_ACTIONS.get(kind, [])}
        unknown = [k for k in a if k not in allowed and k != "kind"]
        if unknown:
            errs.append("actions[%d]: %s has no field %s" % (i, kind, ", ".join(sorted(unknown))))
        actions.append({k: v for k, v in a.items() if k == "kind" or k in allowed})

    targets = []
    for t in payload.get("targets") or []:
        if not isinstance(t, dict) or not str(t.get("marketId") or "").strip():
            errs.append("every target needs a marketId")
            continue
        targets.append({"marketId": str(t["marketId"]).strip(), "tokenId": str(t.get("tokenId") or "")})
    if not targets:
        errs.append("a rule needs at least one market to watch: an automation with no target is a rule about "
                    "nothing")

    rule_doc = {"trigger": trigger, "actions": actions}
    if payload.get("maxLossMicro") is not None:
        rule_doc["max_loss_micro"] = payload["maxLossMicro"]
    # The engine's own validator is the authority on fields, ops, ranges and ceilings. Running it here means the
    # builder cannot accept a document the engine would refuse — one vocabulary, checked in one place.
    errs.extend(validate_rule(rule_doc))
    return {"rule": rule_doc, "targets": targets}, errs


def rule_status(*, enabled: bool, paused_reason: str, dry_run_completed_ms: int | None,
                halted: bool) -> tuple[str, str]:
    """`(status, why)` — where `why` is the sentence the list shows beside the badge.

    The order matters and is the product decision: a halted rule is *halted* even if it is also enabled (the
    risk service stopped it, and calling that "active" would hide the one state D8 asks to be loud about); a
    paused rule says who parked it; a rule with no completed dry run is a dry run whatever its `enabled` flag
    claims, because that is the flag the engine reads before it will go live.
    """
    if halted:
        return "halted", "stopped by the daily-loss halt — trading resumes when you acknowledge it"
    if paused_reason:
        return "paused", "paused: %s" % paused_reason
    if not dry_run_completed_ms:
        return "dry_run", "no completed dry run yet, so it cannot go live"
    if enabled:
        return "active", "armed and firing"
    return "dry_run", "dry run finished; switch it on when you are ready"


def next_evaluation_ms(*, last_fire_ms: int | None, min_interval_ms: int, at_ms: int) -> int:
    """When the engine will look at this rule again. The honest answer for a rule that has never fired is
    "now": an interval measured from a fire that never happened is a number with no meaning."""
    interval = max(int(min_interval_ms or MIN_INTERVAL_MS), MIN_INTERVAL_MS)
    last = int(last_fire_ms or 0)
    return at_ms if not last else max(at_ms, last + interval)


#: For the deny codes where a user can do something. Everything else shows the engine's sentence alone —
#: inventing a next step for `STALE_QUOTE` ("wait for a fresh quote") would be advice about our own ingest, and
#: the run log is the wrong place to be reassuring.
NEXT_STEP = {
    "AUTOMATION_NO_DRY_RUN": "run it in dry mode for a while, then switch it on",
    "AUTOMATION_DAY_CAP": "raise the daily cap or wait for tomorrow",
    "AUTOMATION_NOTIONAL_CAP": "the platform cap for the day is spent; nothing to change on this rule",
    "RISK_HALT": "the daily-loss halt is on: acknowledge it in the banner above to resume",
    "RULE_INVALID": "edit the rule: the engine refuses it as saved",
}


def reason_sentence(run: dict) -> str:
    """One sentence for a run row: the engine's reason, and a next step where one exists."""
    outcome = str(run.get("outcome") or "")
    reason = str(run.get("reason") or "").strip()
    code = str(run.get("deny_code") or "")
    head = {"placed": "fired", "would_place": "would have fired", "skipped": "skipped",
            "failed": "failed"}.get(outcome, outcome or "?")
    sentence = "%s: %s" % (head, reason) if reason else head
    step = NEXT_STEP.get(code)
    return "%s — %s" % (sentence, step) if step else sentence


def leaves_sentence(leaves: list[dict] | None) -> str:
    """Which parts of the trigger were true when it was evaluated, in the order the engine recorded them.

    This is the answer to "why did the bot do that" for the case that has no action in it: a skip whose
    trigger was half-true. The engine records every leaf (never short-circuits) exactly so this can be said.
    """
    if not leaves:
        return "the rule was stopped before its trigger was read (a cap, a pause, or a stale quote)"
    parts = []
    for leaf in leaves:
        observed = leaf.get("observed")
        parts.append("%s %s%s" % (leaf.get("kind"), "met" if leaf.get("ok") else "not met",
                                  "" if observed is None else " (%s)" % observed))
    return "; ".join(parts)


def rule_summary(*, row: dict, state: dict | None = None, targets: list[dict] | None = None,
                 label: str = "", last_run: dict | None = None, halt: dict | None = None,
                 at_ms: int = 0) -> dict:
    """One rule as the list shows it. `row` is `automation_rules` + `automation_rule_policy` columns."""
    st = state or {}
    policy = row.get("policy") or row
    status, why = rule_status(enabled=bool(row.get("enabled")),
                              paused_reason=str(st.get("paused_reason") or ""),
                              dry_run_completed_ms=row.get("dry_run_completed_ms"),
                              halted=bool(halt and halt.get("halted")))
    last_fire = int(policy.get("last_fire_ms") or row.get("last_run_ms") or 0)
    return {
        "ruleId": str(row.get("id") or ""),
        "name": label or _derived_name(row),
        "kind": str(row.get("kind") or ""),
        "status": status,
        "statusWhy": why,
        "mode": "live" if status == "active" else "dry_run",
        "enabled": bool(row.get("enabled")),
        "pausedReason": str(st.get("paused_reason") or ""),
        "failureCount": int(st.get("failure_count") or 0),
        "lastError": str(st.get("last_error") or ""),
        "dryRunCompletedMs": row.get("dry_run_completed_ms"),
        "lastFiredMs": last_fire or None,
        # A rule that has never been evaluated has no last evaluation, and `last_run` is None on exactly that
        # first render. Reading through `(last_run or {})` keeps a brand-new rule's own summary from raising
        # inside the list it is being rendered into: "never evaluated" is a normal state, not an error state.
        "lastEvaluationMs": int((last_run or {}).get("atMs") or 0) or None,
        "nextEvaluationMs": next_evaluation_ms(last_fire_ms=last_fire,
                                               min_interval_ms=policy.get("min_interval_ms") or MIN_INTERVAL_MS,
                                               at_ms=at_ms),
        "runsToday": int(row.get("runs_today") or 0),
        "maxPerDay": int(policy.get("max_per_day") or 24),
        "minIntervalMs": max(int(policy.get("min_interval_ms") or MIN_INTERVAL_MS), MIN_INTERVAL_MS),
        "humanPriorityMs": int(policy.get("human_priority_ms") or 120_000),
        "maxLossMicro": int(row.get("max_loss_micro") or 0),
        "targets": targets or [],
        "trigger": row.get("trigger") or {},
        "actions": row.get("actions") or [],
        "lastRun": None if not last_run else {
            "outcome": str(last_run.get("outcome") or ""),
            "mode": str(last_run.get("mode") or ""),
            "reason": str(last_run.get("reason") or ""),
            "denyCode": str(last_run.get("deny_code") or ""),
            "atMs": int(last_run.get("atMs") or 0),
            "sentence": reason_sentence({"outcome": last_run.get("outcome"), "reason": last_run.get("reason"),
                                         "deny_code": last_run.get("deny_code")}),
            "leaves": str(last_run.get("leaves") or ""),
        },
    }


def _derived_name(row: dict) -> str:
    """A rule with no label still needs a name. Derived from what it does, so it is readable, and prefixed so
    it is obvious that nobody named it."""
    actions = row.get("actions") or []
    first = actions[0].get("kind") if actions and isinstance(actions[0], dict) else ""
    kind = str(row.get("kind") or "")
    return "unnamed %s rule%s" % (kind or "automation", "" if not first else " (%s)" % first)


def simulation(trigger: dict, facts) -> dict:
    """What the trigger would do right now, leaf by leaf. Used by the dry-run preview so a user sees the rule
    think before it is allowed to act."""
    ev = evaluate(trigger, facts)
    return {"fires": bool(ev["fires"]), "leaves": ev["leaves"], "sentence": leaves_sentence(ev["leaves"])}


def template_catalog(*, fee_type: str = "taker", fee_rate_bps: int | None = 200, builder_bps: int = 100,
                     price_micro: int = 500_000, edge_available_bp: int | None = None,
                     latency_ms: int | None = None) -> dict:
    """D8's templates, with the fee arithmetic shown and the entry template withheld when it does not work.

    The brief's condition — "if it doesn't work at current fees, do not offer the template" — is kept by the
    engine's own `crypto_5m_template`, which returns `ship_entry_rule`. This function arranges that answer for a
    screen: every template carries `available`, and the withheld one is **still listed**, with the arithmetic
    that withheld it. A catalog that silently omitted it would make a user guess whether the feature is missing
    or the maths said no; the second is worth reading, so it is shown.
    """
    five = crypto_5m_template(fee_type=fee_type, fee_rate_bps=fee_rate_bps, builder_bps=builder_bps,
                              price_micro=price_micro, edge_available_bp=edge_available_bp,
                              latency_ms=latency_ms)
    n = five.get("numbers") or {}
    arithmetic = {
        "feeType": n.get("fee_type"), "feeRateBps": n.get("fee_rate_bps"), "maker": n.get("maker"),
        "legs": n.get("legs"), "priceMicro": n.get("price_micro"),
        "breakEvenWinRateBp": n.get("taker_break_even_win_rate_bp"), "impliedProbBp": n.get("implied_prob_bp"),
        "feesPerShareMicro": n.get("fees_per_share_micro"), "spreadCostMicro": n.get("spread_cost_micro"),
        "spreadCostBp": n.get("spread_cost_bp"), "latencyMs": n.get("latency_ms"),
        "latencyCostBp": n.get("latency_cost_bp"), "edgeNeededBp": n.get("edge_needed_bp"),
        "edgeAvailableBp": n.get("edge_available_bp"), "builderBps": builder_bps,
        "assumptions": n.get("assumptions"),
    }
    ship = bool(five.get("ship_entry_rule"))
    out = [{
        "templateId": "entry-momentum-5m",
        "name": "5-minute crypto entry (momentum)",
        "kind": "entry",
        "trigger": (five.get("recommended_but_dry_run_only") or {}).get("trigger") or {},
        "actions": (five.get("recommended_but_dry_run_only") or {}).get("actions") or [],
        "available": ship,
        "verdict": str(five.get("verdict") or ""),
        "blockingReason": str(five.get("blocking_reason") or ""),
        "why": str(five.get("why") or ""),
        "feeArithmetic": arithmetic,
    }]
    for rule in five.get("shipped_rules") or []:
        out.append({
            "templateId": str(rule.get("id") or ""),
            "name": _template_name(str(rule.get("id") or "")),
            "kind": str(rule.get("kind") or ""),
            "trigger": rule.get("trigger") or {},
            "actions": rule.get("actions") or [],
            "available": True,
            "verdict": "SHIPPED",
            "blockingReason": "",
            "why": str(rule.get("note") or
                       "ships because it can only reduce a loss the user was already risking"),
            "feeArithmetic": None,
        })
    return {"templates": out, "feeArithmetic": arithmetic, "ships": ship,
            "verdict": str(five.get("verdict") or ""), "why": str(five.get("why") or ""),
            "blockingReason": str(five.get("blocking_reason") or "")}


def _template_name(template_id: str) -> str:
    return {
        "protect-exit-before-resolution": "exit before resolution",
        "protect-one-sided-book": "alert on a one-sided book",
        "protect-volume-spike-cancels-entry": "cancel the entry on a volume spike",
    }.get(template_id, template_id)


def caps_view(*, rules: list[dict]) -> dict:
    """The per-user and platform caps, stated where the list can show them. A cap nobody can see is a cap that
    looks like a bug the first time it refuses."""
    return {
        "concurrentRuleCap": 10,
        "globalRunsPerDay": GLOBAL_RUNS_PER_DAY,
        "activeRules": sum(1 for r in rules if r.get("status") == "active"),
        "note": ("each rule carries its own daily cap and minimum interval; the platform caps run under them so "
                 "one user's rules cannot spend the signer budget"),
    }


__all__ = ["STATUSES", "BUILDER_TRIGGERS", "BUILDER_ACTIONS", "JOINERS", "builder_vocabulary", "compile_builder",
           "rule_status", "next_evaluation_ms", "reason_sentence", "leaves_sentence", "rule_summary",
           "simulation", "template_catalog", "caps_view", "RuleError"]
