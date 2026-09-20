"""The rule behind each kind of finding, in the words the dashboard prints.

Separate from `detect.py` and from the package docstring for one reason: the rules are *data* that the API
serves and the screen renders, and a rule that only exists inside the function that applies it is a rule
nobody can quote back at us. `detect` imports this module, the package re-exports it, and `tests/test_gaming.py`
asserts every sentence carries a reading that is not an accusation.
"""
from __future__ import annotations

#: The rule behind each kind of finding, in the words the dashboard prints. Every sentence says what was measured,
#: what the threshold is, and what the same shape looks like when nothing is wrong — a rule that only accuses
#: cannot be argued with, and a rule nobody can argue with is a rule nobody can review.
RULES: dict[str, str] = {
    "fast_climb": (
        "A wallet is listed when it gains at least 25 places inside seven days and the gain is either three times "
        "the median climb on that board for the same window or at or above the 99th percentile of every climb on "
        "it — the board's own churn sets the bar, never a fixed number, because 25 places in a 10,000-wallet board "
        "and 25 places in a 64-wallet one are different events. A settled record smaller than the median of the "
        "wallets it climbed past raises it to the top of the list; a lucky three weeks looks exactly like that, and "
        "so does a manufactured record, which is why this is a question for a person and not a removal. The same "
        "comparison is what the win-rate board's sample gate makes publicly."),
    "correlated_cluster": (
        "Two wallets are joined when at least 8 of the smaller wallet's fills land on the same token, on the same "
        "side, within 60 seconds of the other's, for at least 60% of that smaller tape. Mirrored accounts, copy "
        "traders and one person's two wallets all produce this shape — and so does a real market maker working the "
        "same book, which is why the finding is a cluster with its worst pair printed, not a verdict."),
    "synthetic_chain": (
        "A referral tree is listed when one referrer has three or more referees and at least one of these holds: "
        "two of them share a funding or device digest, two or more qualified within 48 hours of signing up, or two "
        "or more qualified at the $25 floor (within 2%). Paying for recruiting is not a payout here, so a tree made "
        "of floor-price orders is chasing nothing — but the same shape arises when a referrer's friends join the "
        "same day from one house, and the digests are compared as one-way values, never printed."),
    "builder_anomaly": (
        "A wallet is listed when its attributed volume is at least $25 and either it trades five or more distinct "
        "markets inside ten minutes or at least half its attributed orders never observed a fee. Bursts are a "
        "script and they are also a market maker we should be pleased to have, so the finding prints the market "
        "count and the window and leaves the reading to the reviewer. The unpaid reading is the one that matters "
        "for revenue: expected against observed is the only pair of numbers that separates \"the venue charged "
        "less\" from \"the venue charged nothing\", which is the shape a farm takes when it wants a leaderboard "
        "position without paying for one."),
}


#: The other reading of the same shape — the *disclaimer* half of the product rule that every classification
#: label carries a visible rule **and** a disclaimer. It is data rather than a sentence inside `RULES` because the
#: dashboard renders the two side by side and a reviewer has to be able to see both without scrolling: a list of
#: findings that only ever shows the damning reading is a list that gets acted on before it gets read.
INNOCENT: dict[str, str] = {
    "fast_climb": (
        "A lucky streak, a genuine edge that just arrived, or a wallet that was simply unknown to us and is being "
        "discovered by the tape. Rank is a comparison, so somebody must climb every week."),
    "correlated_cluster": (
        "A copy-trader following a wallet they admire, a friend who trades what a friend trades, or one desk "
        "running two accounts against the same book — none of which is a farm, though the copy engine's own "
        "rules are what decide whether the second wallet is *our* copier or just a spectator."),
    "synthetic_chain": (
        "A referrer whose friends joined from one house on one afternoon, or a trading group that signed up "
        "together and each put in the minimum to try the product. The $25 floor is the product's own threshold, "
        "so hitting it exactly is not by itself suspicious."),
    "builder_anomaly": (
        "A market maker doing what market makers do, or a bot the trader wrote for themselves — legitimate "
        "volume that is simply not shaped like a person clicking. Unpaid orders are frequently orders that were "
        "placed and never filled."),
}

__all__ = ["INNOCENT", "RULES"]
