"""The signal engine: pure, synchronous, and boring enough to be run inside the API if we ever want to.

Two rules this file is built around, because they are the two that get violated in real systems:

1. **An alert that fires 400 times is worse than no alert.** Every rule carries a cooldown AND a dedupe
   signature; a signature that repeats within the cooldown is suppressed even if the underlying number got
   bigger, and the suppression count is returned so the UI can say "3 more" instead of pretending nothing
   happened.
2. **Users must be able to compose rules without us deploying code.** A rule is data: `kind` + `params`,
   validated against a whitelist, evaluated by the same code as the built-ins. That is what makes the paid
   tier "make your own alert" instead of "ask us to ship a config change".
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field, replace

VALID_KINDS = ("large_fill", "volume_spike", "rapid_move", "new_market", "imbalance_flip",
               "negrisk_divergence", "watched_wallet", "resolution_imminent",
               # P10 D9. The three kinds the alert screen sells that this engine did not compute. Without them
               # the screen offered a rule the engine could not evaluate, which is a promise nobody can keep and
               # a rule that never fires — the failure is invisible from the UI, which is what makes it the kind
               # worth writing down.
               "price_level", "spread_widen", "illiquid_top")
SEVERITIES = ("info", "notice", "urgent")
DEFAULTS: dict[str, dict] = {
    "large_fill":          {"abs_usd_micro": 10_000 * 10**6, "rel_multiple": 20.0, "min_sample": 20,
                            "cooldown_s": 300, "severity": "urgent", "channels": ("push", "telegram")},
    "volume_spike":        {"window_min": 60, "min_sample": 30, "z": 3.0, "cooldown_s": 900,
                            "severity": "notice", "channels": ("push",)},
    "rapid_move":          {"pct": 5.0, "depth_floor_usd_micro": 2_000 * 10**6, "window_s": 120,
                            "cooldown_s": 600, "severity": "urgent", "channels": ("push", "telegram")},
    "new_market":          {"categories": (), "cooldown_s": 0, "severity": "info", "channels": ("digest",)},
    "imbalance_flip":      {"from": -0.35, "to": 0.35, "window_s": 60, "cooldown_s": 300,
                            "severity": "notice", "channels": ("push",)},
    "negrisk_divergence":  {"tolerance_micro": 6_000, "cooldown_s": 120, "severity": "urgent",
                            "channels": ("push", "telegram")},
    "watched_wallet":      {"min_usd_micro": 500 * 10**6, "cooldown_s": 60, "severity": "notice",
                            "channels": ("telegram",)},
    "resolution_imminent": {"hours": 6.0, "cooldown_s": 3600, "severity": "notice", "channels": ("push",)},
    # P10 D9. Each of these fires on a CROSSING and not on a state, which is the difference between an alert and
    # a subscription: "the price is above 0.62" is true all afternoon, and an engine that re-fires it is an engine
    # that gets muted. `price_level` is the one a user reaches for first ("tell me if Yes crosses 60"), so its
    # crossing semantics are the ones to get right.
    "price_level":         {"price_micro": 500_000, "op": ">=", "cooldown_s": 300, "severity": "notice",
                            "channels": ("push", "telegram")},
    "spread_widen":        {"spread_bp": 300, "cooldown_s": 600, "severity": "notice", "channels": ("push",)},
    "illiquid_top":        {"min_depth_usd_micro": 500 * 10**6, "cooldown_s": 900, "severity": "notice",
                            "channels": ("push",)},
}

#: The alert kinds D9's screen offers, and the engine kind each one is evaluated by. `None` means "no engine
#: rule": a manual alert fires only when its owner asks for it, and saying so is better than mapping it onto
#: something that would fire on its own.
ALERT_KIND_MAP: dict[str, str | None] = {
    "price_level": "price_level",
    "spread_widen": "spread_widen",
    "whale_fill": "large_fill",
    "resolve_lead": "resolution_imminent",
    "illiquid_top": "illiquid_top",
    "new_market": "new_market",
    "manual": None,
}


class RuleError(ValueError):
    """A user-authored rule that asks for something we do not compute. 422 material, not a 500."""


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str
    owner: str = "system"
    params: tuple = ()                       # a tuple of pairs so the dataclass stays hashable/frozen
    cooldown_s: int | None = None
    severity: str | None = None
    channels: tuple | None = None
    market_filter: tuple = ()                # exact-match predicates, e.g. (("event_slug", "fed-decision"),)
    suppressed: int = 0

    def param(self, name, default=None):
        for k, v in self.params:
            if k == name:
                    return v
        base = DEFAULTS[self.kind]
        return base[name] if name in base else default

    @property
    def cooldown(self) -> int:
        return self.cooldown_s if self.cooldown_s is not None else int(DEFAULTS[self.kind]["cooldown_s"])

    @property
    def severity_level(self) -> str:
        return self.severity or DEFAULTS[self.kind]["severity"]

    @property
    def channel_list(self) -> tuple:
        return tuple(self.channels or DEFAULTS[self.kind]["channels"])


def build_rule(rule_id: str, kind: str, params: dict | None = None, *, owner: str = "user",
               market_filter: dict | None = None, cooldown_s: int | None = None, severity: str | None = None,
               channels: list | None = None) -> Rule:
    """Validate and freeze a user-supplied rule. Unknown keys are ERRORS, not ignored: a rule that silently
    drops `min_sample` would fire on one trade and its author would never learn why."""
    if kind not in VALID_KINDS:
        raise RuleError("unknown rule kind %r (this engine computes: %s)" % (kind, ", ".join(VALID_KINDS)))
    allowed = set(DEFAULTS[kind]) | {"cooldown_s", "severity", "channels"}
    p = params or {}
    bad = sorted(set(p) - allowed)
    if bad:
        raise RuleError("%s does not take %s" % (kind, ", ".join(bad)))
    for key, val in p.items():
        if key.endswith("_usd_micro") or key.endswith("_floor_usd_micro"):
            if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
                raise RuleError("%s must be a positive integer of micro-USDC, got %r" % (key, val))
        if key in ("z", "rel_multiple", "pct", "hours"):
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or val <= 0:
                raise RuleError("%s must be a positive finite number, got %r" % (key, val))
        if key == "min_sample" and (not isinstance(val, int) or isinstance(val, bool) or val < 2):
            raise RuleError("min_sample must be an integer >= 2 (a z-score of one sample is not a z-score)")
        if key == "price_micro" and (not isinstance(val, int) or isinstance(val, bool)
                                     or not 0 < val < 10 ** 6):
            raise RuleError("price_micro must be an integer inside (0, 1000000), got %r" % (val,))
        if key == "op" and val not in (">=", "<="):
            raise RuleError("op must be '>=' or '<=' (a level with no direction is not a crossing), got %r"
                            % (val,))
        if key == "spread_bp" and (not isinstance(val, int) or isinstance(val, bool) or val <= 0):
            raise RuleError("spread_bp must be a positive integer of basis points, got %r" % (val,))
    if severity is not None and severity not in SEVERITIES:
        raise RuleError("severity must be one of %s" % ", ".join(SEVERITIES))
    if cooldown_s is not None and (not isinstance(cooldown_s, int) or cooldown_s < 0):
        raise RuleError("cooldown_s must be a non-negative integer")
    if channels is not None:
        # `webhook` is Pro's channel (D9) and `alert_fires.channel` has allowed it since P04; an engine
        # that refused it while the product sells it would be a rule the paying plan cannot save.
        unknown = sorted(set(channels) - {"push", "telegram", "email", "digest", "inapp", "webhook"})
        if unknown:
            raise RuleError("unknown channels: %s" % ", ".join(unknown))
    return Rule(rule_id, kind, owner, tuple(sorted(p.items())), cooldown_s, severity,
                tuple(channels) if channels else None, tuple(sorted((market_filter or {}).items())))


@dataclass
class Alert:
    rule_id: str
    kind: str
    market_id: str
    token_id: str
    severity: str
    title: str
    body: dict
    dedupe_key: str
    at_ms: int
    channels: tuple = ()
    # Set when the alert was muted by a cooldown. It is a FIELD and not a dropped return value because "3 more
    # like this" is information the user wants, and the engine that swallows a suppressed alert entirely has
    # no way to say so.
    suppressed: int = 0


@dataclass
class Engine:
    """`evaluate(event)` returns the alerts this event justifies. `state` is the suppression memory, and it is
    kept OUTSIDE the event so a restart replays cleanly: the durable copy is `signal_state` in the schema, and
    an engine that only remembered in RAM would re-fire every alert after a deploy (which is the same mistake
    as having no cooldown at all, on the day of a big market, in the worst possible hour)."""
    state: dict = field(default_factory=dict)
    fired: int = 0
    suppressed: int = 0
    #: Sweep the cooldown memory once it is bigger than this. See `prune()` for why the sweep is free.
    PRUNE_AT: int = 50_000

    def prune(self, now_ms: int, rules: list) -> int:
        """Drop cooldown entries that can no longer suppress anything. Returns how many went.

        This is a load finding, not a tidy-up. P13's 30-minute soak showed the tape flat (bounded ring,
        bounded dedupe window) and the books flat while `state` grew for the whole run: the key carries the
        rule id and the dedupe key, and a `large_fill` key includes a notional bucket, so a liquid market
        mints a new entry per bucket and never removes one. At 200 fills/s that is unbounded RAM in the
        process that must not fall over.

        The sweep cannot change behaviour. Suppression is decided by `now_ms - last < rule.cooldown * 1000`,
        so an entry whose own rule's cooldown has already elapsed is a value nothing will ever read again.
        A rule that is no longer in the list is kept for an hour rather than assumed gone: a rule that is
        missing from one evaluation (a user edited it, a page of rules failed to load) must not silently
        un-suppress an alert.
        """
        horizon = {r.id: max(0, int(r.cooldown)) * 1000 for r in rules}
        dropped = 0
        for key, ts in list(self.state.items()):
            rid = key.rsplit("|", 1)[-1]
            keep_ms = horizon.get(rid, 3_600_000)
            if now_ms - int(ts) >= keep_ms:
                del self.state[key]
                dropped += 1
        return dropped

    @staticmethod
    def matches(rule: Rule, event: dict) -> bool:
        """Exact match on the event's fields, with one addition: a value may be a LIST.

        It exists for event-wide alerts (P10 D9): a rule watching an event must watch every market in it, and a
        filter that only ever matched the first condition id would silently drop the other legs — the alert would
        look armed and be wrong in the direction nobody checks.
        """
        for k, v in rule.market_filter:
            got = str(event.get(k) or "")
            if isinstance(v, (list, tuple)):
                if got not in {str(x) for x in v}:
                    return False
            elif got != str(v):
                return False
        return True

    def evaluate(self, rules: list[Rule], event: dict, now_ms: int) -> list[Alert]:
        out: list[Alert] = []
        if len(self.state) > self.PRUNE_AT:
            self.prune(now_ms, rules)
        for rule in rules:
            if not self.matches(rule, event):
                continue
            cand = getattr(self, "_eval_" + rule.kind)(rule, event, now_ms)
            if cand is None:
                continue
            for a in ([cand] if isinstance(cand, Alert) else cand):
                key = "%s|%s" % (a.dedupe_key, rule.id)
                last = self.state.get(key)
                if last is not None and now_ms - last < rule.cooldown * 1000:
                    self.suppressed += 1
                    a = replace(a, suppressed=1)
                    continue
                self.state[key] = now_ms
                self.fired += 1
                out.append(a)
        return out

    # ---------------------------------------------------------------- detectors
    def _eval_large_fill(self, rule: Rule, e: dict, now_ms: int) -> Alert | None:
        if e.get("type") != "fill":
            return None
        abs_usd = int(rule.param("abs_usd_micro"))
        rel = float(rule.param("rel_multiple"))
        n = int(e.get("usd_notional_micro") or 0)
        med = int(e.get("market_median_fill_micro") or 0)
        sample = int(e.get("market_fill_sample") or 0)
        if n < abs_usd:
            return None
        # The relative arm needs a sample: without one, "20x the median" is 20x zero. `min_sample` is not a
        # nicety; it is what stops a market's first trade from being called whale activity.
        if sample < int(rule.param("min_sample")):
            return None
        if med and n < rel * med:
            return None
        return Alert(rule.id, rule.kind, e.get("market", ""), e.get("token_id", ""), rule.severity_level,
                     "large fill %s" % e.get("side", ""), {"notional_micro": n, "median_micro": med,
                                                            "multiple": round(n / med, 1) if med else None},
                     "large:%s:%s" % (e.get("market", ""), n // max(1, abs_usd // 10)), now_ms,
                     rule.channel_list)

    def _eval_volume_spike(self, rule: Rule, e: dict, now_ms: int) -> Alert | None:
        if e.get("type") != "volume_bucket":
            return None
        hist = [int(x) for x in (e.get("history_micro") or [])]
        if len(hist) < int(rule.param("min_sample")):
            return None
        mu, sd = statistics.fmean(hist), statistics.pstdev(hist)
        if sd <= 0:
            return None                    # a market that trades the same amount every minute has no spikes;
        z = (int(e.get("value_micro") or 0) - mu) / sd   # a z-score of 0/0 would call every minute one
        if z < float(rule.param("z")):
            return None
        return Alert(rule.id, rule.kind, e.get("market", ""), "", rule.severity_level,
                     "volume spike %+.1f sigma" % z, {"z": round(z, 2), "samples": len(hist),
                                                        "window_min": rule.param("window_min")},
                     "spike:%s:%d" % (e.get("market", ""), int(now_ms // 60000)), now_ms, rule.channel_list)

    def _eval_rapid_move(self, rule: Rule, e: dict, now_ms: int) -> Alert | None:
        """A move is only interesting if there was depth to move through. `depth_floor_usd_micro` is the
        difference between "somebody bought the ask" and "somebody could not have sold it back"."""
        if e.get("type") != "price_move":
            return None
        old, new = int(e.get("old_micro") or 0), int(e.get("new_micro") or 0)
        if not old:
            return None
        pct = 100.0 * (new - old) / old
        if abs(pct) < float(rule.param("pct")):
            return None
        depth = int(e.get("usd_depth_micro") or 0)
        if depth < int(rule.param("depth_floor_usd_micro")):
            return None
        if e.get("resolution_event"):
            return None                    # 0.999 -> 1.000 is a settlement, not a signal (see universe.Resolution)
        return Alert(rule.id, rule.kind, e.get("market", ""), e.get("token_id", ""), rule.severity_level,
                     "price %+.1f%% in %ds" % (pct, rule.param("window_s")),
                     {"from": old, "to": new, "depth_usd_micro": depth}, "move:%s" % (e.get("market", "")),
                     now_ms, rule.channel_list)

    def _eval_new_market(self, rule: Rule, e: dict, now_ms: int) -> Alert | None:
        if e.get("type") != "new_market":
            return None
        cats = tuple(rule.param("categories") or ())
        if cats and not (set(e.get("tags") or []) & set(cats)):
            return None
        return Alert(rule.id, rule.kind, e.get("market", ""), "", rule.severity_level,
                     "new market: %s" % str(e.get("question") or "")[:60],
                     {"question": e.get("question"), "tags": list(e.get("tags") or [])[:6]},
                     "new:%s" % e.get("market", ""), now_ms, rule.channel_list)

    def _eval_imbalance_flip(self, rule: Rule, e: dict, now_ms: int) -> Alert | None:
        if e.get("type") != "book":
            return None
        prev, now = e.get("imbalance_prev"), e.get("imbalance")
        if prev is None or now is None:
            return None
        if not (float(prev) <= float(rule.param("from")) and float(now) >= float(rule.param("to"))):
            return None
        return Alert(rule.id, rule.kind, e.get("market", ""), e.get("token_id", ""), rule.severity_level,
                     "book imbalance flipped %.2f -> %.2f" % (prev, now),
                     {"from": prev, "to": now, "window_s": rule.param("window_s")},
                     "flip:%s" % e.get("market", ""), now_ms, rule.channel_list)

    def _eval_negrisk_divergence(self, rule: Rule, e: dict, now_ms: int) -> Alert | None:
        """The one genuinely actionable signal here: in a negRisk event the YES prices must sum to 1, so a sum
        outside the tolerance is either a fee artifact or an arbitrage. Tolerance is expressed in MICRO and the
        caller sets it from the market's own tick size, because 6 ticks of 0.001 and 6 of 0.01 are not the same
        opportunity and we must not tell a user to take one they cannot execute."""
        if e.get("type") != "negrisk_sum":
            return None
        total = int(e.get("sum_micro") or 0)
        dev = abs(total - 10 ** 6)
        if dev < int(rule.param("tolerance_micro")):
            return None
        return Alert(rule.id, rule.kind, e.get("event", ""), "", rule.severity_level,
                     "negRisk sum %.4f (dev %d ticks)" % (total / 1e6, dev // max(1, int(e.get("tick_micro") or 1))),
                     {"sum_micro": total, "deviation_micro": dev, "outcomes": len(e.get("prices_micro") or []),
                      "tick_micro": e.get("tick_micro")}, "negrisk:%s" % e.get("event", ""), now_ms,
                     rule.channel_list)

    def _eval_watched_wallet(self, rule: Rule, e: dict, now_ms: int) -> Alert | None:
        if e.get("type") != "fill":
            return None
        watched = {str(w) for w in (e.get("watched_wallets") or [])}
        if not watched or str(e.get("wallet") or "") not in watched:
            return None
        if int(e.get("usd_notional_micro") or 0) < int(rule.param("min_usd_micro")):
            return None
        return Alert(rule.id, rule.kind, e.get("market", ""), e.get("token_id", ""), rule.severity_level,
                     "watched wallet traded %s" % e.get("side", ""),
                     {"wallet": e.get("wallet"), "notional_micro": e.get("usd_notional_micro")},
                     "wallet:%s:%s" % (e.get("wallet", ""), now_ms // 60000), now_ms, rule.channel_list)

    def _eval_resolution_imminent(self, rule: Rule, e: dict, now_ms: int) -> Alert | None:
        if e.get("type") != "market_clock":
            return None
        if not int(e.get("user_open_position_micro") or 0):
            return None
        left_ms = int(e.get("end_ts_ms") or 0) - now_ms
        if left_ms < 0 or left_ms > float(rule.param("hours")) * 3600_000:
            return None
        return Alert(rule.id, rule.kind, e.get("market", ""), "", rule.severity_level,
                     "resolves in %.1f h and you are in it" % (left_ms / 3600000.0),
                     {"ms_left": left_ms, "position_micro": e.get("user_open_position_micro")},
                     "resolm:%s:%d" % (e.get("market", ""), int(left_ms // 3600000)), now_ms,
                     rule.channel_list)


    # ---------------------------------------------------------------- P10 D9's three kinds
    def _eval_price_level(self, rule: Rule, e: dict, now_ms: int) -> Alert | None:
        """A level CROSSING, not a level state.

        The event carries the previous and the new mid, so "crossed 0.62" is a fact the engine can state rather
        than infer: a rule that fired whenever the price happened to be above the level would re-fire on every
        book update until the user muted it, and a muted alert is worse than no alert because the user believes
        it is on.
        """
        if e.get("type") != "price_move":
            return None
        old, new = int(e.get("old_micro") or 0), int(e.get("new_micro") or 0)
        if not old:
            return None
        level, op = int(rule.param("price_micro")), str(rule.param("op"))
        crossed = (old < level <= new) if op == ">=" else (old > level >= new)
        if not crossed:
            return None
        return Alert(rule.id, rule.kind, e.get("market", ""), e.get("token_id", ""), rule.severity_level,
                     "price crossed %s %.4f" % (op, level / 10 ** 6),
                     {"level_micro": level, "op": op, "from_micro": old, "to_micro": new,
                      "usd_depth_micro": e.get("usd_depth_micro")},
                     "level:%s:%s:%s" % (e.get("market", ""), op, level), now_ms, rule.channel_list)

    def _eval_spread_widen(self, rule: Rule, e: dict, now_ms: int) -> Alert | None:
        """Fires when the spread crosses the threshold going WIDER, and says both numbers.

        The spread is a cost: a user who asked to hear about a 300 bp spread wants the moment it happened, not a
        bell that rings for as long as the market stays bad. The previous value travels in the alert body, because
        "widened 240 -> 410" is a sentence a user can act on and "410 bp" is not.
        """
        if e.get("type") != "book":
            return None
        prev, now = e.get("spread_prev_micro"), e.get("spread_micro")
        mid = int(e.get("mid_micro") or 0)
        if prev is None or now is None or mid <= 0:
            return None
        now_bp = (int(now) * 10_000) // mid
        prev_bp = (int(prev) * 10_000) // mid
        if not (prev_bp < int(rule.param("spread_bp")) <= now_bp):
            return None
        return Alert(rule.id, rule.kind, e.get("market", ""), e.get("token_id", ""), rule.severity_level,
                     "spread widened %d -> %d bp" % (prev_bp, now_bp),
                     {"from_bp": prev_bp, "to_bp": now_bp, "mid_micro": mid},
                     "spread:%s:%d" % (e.get("market", ""), now_bp), now_ms, rule.channel_list)

    def _eval_illiquid_top(self, rule: Rule, e: dict, now_ms: int) -> Alert | None:
        """Fires when the top of the book DROPS through the floor.

        Thin book is the condition where every other number on the screen stops meaning what it meant: the price
        is real, the size behind it is not. Only the drop is reported — a market that has been thin for a week
        would otherwise alert once per cooldown forever, and this is the alert whose whole value is that it is
        rare.
        """
        if e.get("type") != "book":
            return None
        prev, now = e.get("usd_depth_prev_micro"), e.get("usd_depth_micro")
        if prev is None or now is None:
            return None
        floor = int(rule.param("min_depth_usd_micro"))
        if not (int(prev) >= floor > int(now)):
            return None
        return Alert(rule.id, rule.kind, e.get("market", ""), e.get("token_id", ""), rule.severity_level,
                     "top of book thinned to %.0f USDC" % (int(now) / 10 ** 6),
                     {"from_usd_micro": int(prev), "to_usd_micro": int(now), "floor_usd_micro": floor},
                     "thin:%s:%d" % (e.get("market", ""), int(now) // max(1, floor // 10)),
                     now_ms, rule.channel_list)
