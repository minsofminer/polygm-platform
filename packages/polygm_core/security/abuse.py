"""D8 · abuse, fraud, and the platform risk that can end the revenue line.

Four detectors, each with a threshold in code rather than in prose, because a threshold nobody can read is a
threshold nobody can argue with at 3am:

* **Wash trading through our own builder code.** Polymarket's terms revoke builder codes for self-referred or
  non-genuine activity — so a user gaming volume does not cost us a fee line, it costs the *whole* line. The
  score is the sum of four named factors with weights, and the action it triggers is on the payout path, not on
  the order path: we never block a user's trade because our accounting is suspicious, we hold our own money.
* **Referral and Sybil abuse.** Payouts need genuine activity, not an account that exists.
* **The poisoned alert channel.** Anyone can create a market; our free channel broadcasts to thousands. The
  gate is loud about the difference between *hold* (re-check later) and *refuse* (never), because "we are
  quiet today" and "we will never show this" are different products.
* **The builder code being switched off by the venue.** Existential dependency, so the response is a state
  machine with a user-facing message that does not lie about whose problem it is.
"""
from __future__ import annotations

from dataclasses import dataclass

# ----------------------------------------------------------------------------------------------- wash trading ----
WASH_WEIGHTS = {"self_cross": 4500, "round_trip": 2500, "counterparty_concentration": 1800,
                "size_regularisation": 1200}          # bps out of 10,000
WASH_HOLD_PAYOUT_BPS = 4_000                          # hold our money, never theirs
WASH_FREEZE_BPS = 7_000
WASH_CLEAR_BPS = 1_000
ROUND_TRIP_WINDOW_MS = 10 * 60 * 1000                  # buy then sell, same user, same market, inside 10 min
CONCENTRATION_BPS = 8_000                             # 80 % of a user's volume against one counterparty


@dataclass(frozen=True)
class WashVerdict:
    score_bps: int
    factors: tuple[str, ...]
    action: str

    def as_dict(self) -> dict:
        return {"score_bps": self.score_bps, "factors": list(self.factors), "action": self.action}


def _addr(a: object) -> str:
    return str(a or "").strip().lower()


def wash_score(*, user_id: str, maker_address: str = "", taker_address: str = "", counterparty_user: str = "",
               side: str = "", prior_side: str = "", prior_ms: int = 0, at_ms: int = 0,
               window_volume_micro: int = 0, window_fills: int = 0, top_counterparty_share_bps: int = 0,
               sizes: tuple[int, ...] = ()) -> WashVerdict:
    """The four factors, computed from what we already store. No probabilistic model, no ML, no "suspicion":
    each factor is a fact about the ledger that a human can re-check with a SELECT."""
    flags: list[str] = []
    score = 0
    if maker_address and _addr(maker_address) == _addr(taker_address):
        score += WASH_WEIGHTS["self_cross"]
        flags.append("self_cross")
    elif counterparty_user and str(counterparty_user) == str(user_id):
        score += WASH_WEIGHTS["self_cross"]
        flags.append("self_referred")
    if prior_side and side and prior_side != side and 0 < at_ms - prior_ms <= ROUND_TRIP_WINDOW_MS:
        score += WASH_WEIGHTS["round_trip"]
        flags.append("round_trip_in_%dms" % (at_ms - prior_ms))
    if top_counterparty_share_bps >= CONCENTRATION_BPS and window_fills >= 5:
        score += WASH_WEIGHTS["counterparty_concentration"]
        flags.append("counterparty_%dbps" % top_counterparty_share_bps)
    # Identical sizes repeated: "wash 100 shares, then 100 shares, then 100 shares" is a machine, and a human
    # who happens to trade round numbers twice is not a machine — hence the count, not the equality.
    if len(sizes) >= 3 and len({int(s) for s in sizes}) == 1:
        score += WASH_WEIGHTS["size_regularisation"]
        flags.append("identical_size_x%d" % len(sizes))
    score = min(10_000, score)
    action = ("freeze" if score >= WASH_FREEZE_BPS else "hold_payout" if score >= WASH_HOLD_PAYOUT_BPS
              else "clear" if score < WASH_CLEAR_BPS and "self_cross" not in flags else "watch")
    return WashVerdict(score, tuple(flags), action)


def payout_gate(verdict: WashVerdict) -> tuple[bool, str]:
    """Where the score bites: the payout, not the order.

    A false positive here costs a creator a week of revenue and nothing else; a false positive on the order path
    costs a user a fill they needed. That asymmetry is why `freeze` means "no payout, tell a human", never
    "no trading".
    """
    if verdict.action in ("freeze", "hold_payout"):
        return False, ("payout held: %s (score %d bps: %s)"
                       % (verdict.action, verdict.score_bps, ", ".join(verdict.factors) or "no factors"))
    return True, "ok"


# -------------------------------------------------------------------------------------- referral / sybil ----
MIN_GENUINE_TRADES_FOR_PAYOUT = 3
MIN_GENUINE_VOLUME_MICRO = 500 * 1_000_000              # $500 of real turnover
SYBIL_CLUSTER_SIZE = 5                                   # this many accounts behind one fingerprint


def referral_flags(*, referee_trades: int, referee_volume_micro: int, referrer_user: str, referee_user: str,
                   ip_hash: str = "", ua_hash: str = "", payout_claimed: bool = False,
                   crossed_with_referrer: bool = False, cluster_size: int = 1) -> dict:
    reasons: list[str] = []
    if str(referee_user) == str(referrer_user):
        reasons.append("self_referral")
    if referee_trades < MIN_GENUINE_TRADES_FOR_PAYOUT:
        reasons.append("only %d trades" % referee_trades)
    if referee_volume_micro < MIN_GENUINE_VOLUME_MICRO:
        reasons.append("turnover %d µ below %d µ" % (referee_volume_micro, MIN_GENUINE_VOLUME_MICRO))
    if crossed_with_referrer:
        reasons.append("referee_traded_against_referrer")
    if payout_claimed and reasons:
        reasons.append("claimed_early")
    if cluster_size >= SYBIL_CLUSTER_SIZE:
        reasons.append("sybil_cluster_of_%d(%s)" % (cluster_size, "ip" if ip_hash else "ua"))
    return {"payable": not reasons, "reasons": reasons,
            "hold_days": 14 if reasons and "self_referral" not in reasons else 0}


def cluster_size_for(flags_by_user: dict[str, dict]) -> dict[str, int]:
    """Group by (ip_hash, ua_hash) — a fingerprint shared by many accounts is a farm, not a coincidence.

    Only non-empty fingerprints cluster: an unknown ip must never make somebody a Sybil.
    """
    buckets: dict[tuple, list[str]] = {}
    for uid, f in (flags_by_user or {}).items():
        key = (str(f.get("ip_hash") or ""), str(f.get("ua_hash") or ""))
        if key == ("", ""):
            continue
        buckets.setdefault(key, []).append(str(uid))
    return {uid: len(ids) for ids in buckets.values() for uid in ids}


# ------------------------------------------------------------------------- the builder code as a resource ----
BUILDER_STATES = ("active", "throttled", "disabled", "unknown")
DISABLE_AFTER_REJECTS = 5
DISABLE_WINDOW_MS = 60_000

# The message, in the file, so the product decision is reviewed with the code. It says whose problem it is
# (ours), what happened to the user's order (nothing), and it does not promise a fix date.
USER_MESSAGE_WHEN_DISABLED = ("Your order went through. The commission code we attach to orders was turned off "
                              "by the market's operator, so orders are running without it while we sort that "
                              "out. Nothing changes for you.")


def builder_code_event(*, state: str, reject_count: int, last_reject_ms: int, at_ms: int,
                       confirmed_active: bool = False) -> dict:
    """One transition per venue rejection batch. `unknown` (we have not seen it either way) is not `active`:
    treating "no evidence" as "fine" is how a revoked code keeps being attached to orders until a user reports
    that their order was rejected.
    """
    out = {"state": state, "alarm": False, "strip_code": False, "reject_count": int(reject_count)}
    if reject_count >= DISABLE_AFTER_REJECTS and at_ms - int(last_reject_ms or 0) <= DISABLE_WINDOW_MS:
        out.update(state="disabled", alarm=True, strip_code=True, message=USER_MESSAGE_WHEN_DISABLED,
                   why="%d rejections in %d s carrying this code" % (reject_count, DISABLE_WINDOW_MS // 1000))
    elif state == "disabled" and confirmed_active:
        out.update(state="active", alarm=False, message="the code is accepted again", why="venue confirmed")
    elif state == "unknown":
        out.update(alarm=False, strip_code=False, why="no observation yet; orders continue and we watch for "
                                                       "rejections rather than assuming health")
    elif reject_count and at_ms - int(last_reject_ms or 0) <= DISABLE_WINDOW_MS:
        out.update(state="throttled", why="%d rejections inside the window, below the disable threshold"
                   % reject_count)
    elif reject_count:
        # The state has to be able to come back on its own. A counter that only ever ratchets is how a venue
        # hiccup last Tuesday keeps a builder's commission code stripped in March, and the old wording claimed
        # "recent" about rejections that were the opposite: the sentence and the arithmetic must agree.
        out.update(state="active", why="%d rejections, all older than %d s: outside the window they are history, "
                                       "not a pattern" % (reject_count, DISABLE_WINDOW_MS // 1000))
    return out


# ---------------------------------------------------------------------------- the alert channel's gate ----
MIN_LIQUIDITY_MICRO = 500 * 1_000_000                   # $500 on the book
MIN_MARKET_AGE_MS = 30 * 60 * 1000                       # 30 minutes: a market created seconds ago is the attack
FRESH_WALLET_HOURS = 72                                  # funded by a wallet created an hour ago = suspicious
MAX_BROADCASTS_PER_HOUR = 5
BAD_FLAGS = ("markup", "nested_markup", "invisible_characters", "mixed_script_domain", "claim_airdrop",
             "connect_wallet", "send_funds_to", "fake_support", "urgency", "account_verification",
             "yield_promise", "free_tld_link")


def broadcast_gate(*, market_id: str, liquidity_micro: int, age_ms: int, resolution_trusted: bool,
                   flags=(), audience: int = 0, created_by_wallet_age_h: int | None = None,
                   duplicate_of_ms: int | None = None, at_ms: int = 0, broadcasts_last_hour: int = 0,
                   outcomes: int = 2) -> dict:
    """The quality gate a market must pass before *our* megaphone points at it.

    `hold` means the reason is time-based and will disappear (the market is young, the wallet is fresh, we
    already showed this). `refused` means the content itself is the problem and no amount of waiting fixes it.
    A broadcast that is refused silently is indistinguishable from a bug in the alert pipeline, so the verdict
    and its reasons go into `broadcast_gates` either way.
    """
    holds: list[str] = []
    refusals: list[str] = []
    if audience <= 0:
        refusals.append("no_audience")
    if liquidity_micro < MIN_LIQUIDITY_MICRO:
        holds.append("liquidity %d µ < %d µ" % (liquidity_micro, MIN_LIQUIDITY_MICRO))
    if age_ms < MIN_MARKET_AGE_MS:
        holds.append("age %ds < %ds" % (age_ms // 1000, MIN_MARKET_AGE_MS // 1000))
    if duplicate_of_ms is not None and at_ms - int(duplicate_of_ms) < 24 * 3600 * 1000:
        holds.append("already_broadcast_%ds_ago" % ((at_ms - int(duplicate_of_ms)) // 1000))
    if created_by_wallet_age_h is not None and created_by_wallet_age_h < FRESH_WALLET_HOURS:
        holds.append("creator_wallet_%dh_old" % created_by_wallet_age_h)
    if broadcasts_last_hour >= MAX_BROADCASTS_PER_HOUR:
        holds.append("our_own_rate_limit(%d/h)" % broadcasts_last_hour)
    if not resolution_trusted:
        refusals.append("resolution_source_untrusted")
    bad = [f for f in (flags or ()) if f in BAD_FLAGS or f.startswith("impersonates") or f.startswith("desc:")]
    if bad:
        refusals.append("metadata:" + ",".join(sorted(bad)[:4]))
    if outcomes < 2:
        refusals.append("malformed_outcomes")
    verdict = "refused" if refusals else ("hold" if holds else "broadcast")
    return {"market_id": str(market_id), "verdict": verdict, "holds": holds, "refusals": refusals,
            "reasons": refusals + holds,
            "recheck_ms": MIN_MARKET_AGE_MS if verdict == "hold" and holds else 0,
            "audience": int(audience)}


# --------------------------------------------------------------------- rate-limit exhaustion as a DoS (D1) ----
UPSTREAM_BUDGET_PER_MIN = 300            # our Cloudflare/provider budget per minute, shared
PER_USER_SHARE_CAP_BPS = 2_500          # 25 % of it: one user's 200 rules cannot own the pipe


def upstream_budget_guard(*, active_users: int, rules_per_user: int, polls_per_rule: int = 1,
                          budget_per_min: int | None = None, share_cap_bps: int | None = None) -> dict:
    """The arithmetic of "one user with 200 alert rules breaks the product for everyone".

    Demand is `users x rules x polls`. Capacity is the smaller of the global budget and this user's share of
    it, so the cap is what makes the budget *survivable* while one account is busy, and the headroom number is
    what the alarm page shows before the budget is gone.
    """
    budget = int(budget_per_min if budget_per_min is not None else UPSTREAM_BUDGET_PER_MIN)
    cap_bps = int(share_cap_bps if share_cap_bps is not None else PER_USER_SHARE_CAP_BPS)
    demand = max(0, int(active_users)) * max(0, int(rules_per_user)) * max(1, int(polls_per_rule))
    per_user_cap = max(1, budget * cap_bps // 10_000)
    served = min(demand, budget)
    return {"demand_per_min": demand, "budget_per_min": budget, "per_user_cap": int(per_user_cap),
            "exhausted": demand > budget, "headroom_bps": max(0, (budget - served) * 10_000 // max(1, budget)),
            # Deliberately *scheduled*, not dropped: an alert rule the user can see and we never run is a lie.
            "policy": "schedule round-robin at %d polls/min/user and say so in the rule's UI; never silently "
                      "drop a rule" % per_user_cap,
            "alarm_at_bps": 7_000}


def rules_within_budget(rules: int, *, per_user_cap: int = PER_USER_SHARE_CAP_BPS) -> dict:
    cap = max(1, UPSTREAM_BUDGET_PER_MIN * per_user_cap // 10_000)
    return {"rules": int(rules), "cap": cap, "over": int(rules) > cap,
            "message": ("this rule will run at %d/min at most, which is what keeps everyone else's alerts "
                        "alive" % cap) if int(rules) > cap else ""}
