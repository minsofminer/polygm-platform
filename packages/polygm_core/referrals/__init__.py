"""P11 · Referrals: the reward model, the funnel, and the arithmetic a payout is made of.

D5's hard part is not the link. It is deciding **what a referral is worth, and to whom**, in a way that cannot be
farmed. The kit offers three models and asks for one, with its consequences:

* a share of our builder fee on the referee's volume for N months,
* a flat bounty on the referee's first funded trade,
* Pro credit.

**The model here is the first one: a share of the builder fee we are actually paid** (`SHARE_BPS` of the fee
`revenue.attribution` *observed* on that referee's own attributable fills, for `TERM_DAYS` after the referee's
qualifying order). Three properties decided it, and each one is a whole class of abuse that never has to be
detected later:

1. **No fixed cost, so there is no Sybil equilibrium.** A flat bounty pays the moment a stranger crosses the
   threshold, and the cost of manufacturing a stranger — one small matched order — can be *less than the bounty*.
   A share of an observed fee pays only when the referee generates fees the venue actually collected, so to earn
   $X an attacker must cause roughly `10_000 / SHARE_BPS` × X to be paid in real builder fees, out of their own
   money, to us. The attacker is the customer. The threshold below is still required (an unmatchable dust order
   must not open a 12-month revenue claim), but it is a rate limit on claims, not the thing standing between us
   and a farm.
2. **Deposits are invisible to it**, which is the kit's explicit trap: "that attracts people who deposit,
   withdraw, and never trade, and it looks like a pyramid". A deposit of any size earns a referrer exactly zero
   until it trades, and the numbers on the dashboard are therefore about *trading*, not about money moved in.
3. **Nothing is ever paid for recruiting.** There is no second level. A referrer earns from referees' fees and
   from nothing else, so "recruit recruiters" has no payout behind it and cannot be dressed up as one.

The honest costs of the model, because a document that lists only upsides is a brochure:

* **It is a liability with a tail.** Accrual runs for a year from qualification and scales with a whale's volume;
  payments are therefore paced (`SETTLE_HOLD_DAYS` after the day they accrue, monthly, `PAYOUT_MIN_MICRO` minimum)
  and a month above `REVIEW_THRESHOLD_MICRO` for one referrer is reviewed before it is paid, not after.
* **It pays slowly at the bottom.** A referrer who brings one small trader earns cents in month one. That is the
  point (the alternative is paying dust for dust), and the dashboard says so rather than hiding it behind a
  "pending" that never clears: below the minimum the accrual **carries forward**, and the panel names the amount
  still to go before the next payout.

Everything in this module is integer arithmetic on micro-dollars. A referral payout computed in floats is a
payout that can disagree with the ledger it came from.
"""
from __future__ import annotations

from . import code, sybil, terms

__all__ = ["code", "sybil", "terms"]
