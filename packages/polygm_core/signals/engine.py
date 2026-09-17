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
               "negrisk_divergence", "watched_wallet", "resolution_imminent")
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
    if severity is not None and severity not in SEVERITIES:
        raise RuleError("severity must be one of %s" % ", ".join(SEVERITIES))
    if cooldown_s is not None and (not isinstance(cooldown_s, int) or cooldown_s < 0):
        raise RuleError("cooldown_s must be a non-negative integer")
    if channels is not None:
        unknown = sorted(set(channels) - {"push", "telegram", "email", "digest", "inapp"})
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

    @staticmethod
    def matches(rule: Rule, event: dict) -> bool:
        for k, v in rule.market_filter:
            if str(event.get(k) or "") != str(v):
                return False
        return True

    def evaluate(self, rules: list[Rule], event: dict, now_ms: int) -> list[Alert]:
        out: list[Alert] = []
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
