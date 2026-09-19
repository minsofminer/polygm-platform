"""P11 · The leaderboard: six boards, their gates, and the integrity rules that make them mean something.

This module is the *specification*, in the form the engine and the published methodology page both read. A board
declared here carries its own formula sentence, its eligibility gate, its tie-breaks, its window and its recompute
cadence — and the API serves this same structure at `/v1/leaderboard/methodology`. A methodology page maintained
by hand beside a ranking engine maintained in code is two authorities over one number, and the first disagreement
is the one users screenshot.

Three decisions shape everything else here:

**Volume alone is not a ranking.** Polymarket publishes an all-time volume board (`lb-api.polymarket.com/volume`,
top wallet $1.85B lifetime [CTX: the kit's measurement]). Volume promotes churn: a wallet that round-trips its own
money looks like a whale. Our default board is risk-adjusted PnL, and our volume board counts only *verified*
volume — the part that a round trip could not manufacture (see `integrity.py`).

**One sample gate, not two.** `MIN_RESOLVED` is `terminal.metrics.SAMPLE_GATE` — the same 20 settled markets that
every win rate on the platform already waits for. A second, leaderboard-only gate would be a second answer to
"is this a rate or a coin flip", and the two would drift.

**A 24-hour *skill* board does not exist.** The window list for skill boards is 7d/30d/90d/all, which is what
`trader_metrics` is computed for: ranking a win rate on a 24-hour sample would print a number with no sample
behind it and call it a leaderboard. The activity boards (`volume`, `rising`) do use 24h/7d, because a day's
volume is a fact regardless of whether a day's PnL is a signal.
"""
from __future__ import annotations

from ..terminal import metrics as tm

#: The one sample gate. Re-exported rather than re-declared so a change to the platform's gate is a change here.
MIN_RESOLVED = tm.SAMPLE_GATE

#: Verified lifetime turnover a wallet must have to appear at all. Justification, with the distribution it came
#: from, is in the phase document §2.2 — the short version: a resolved-market count can be manufactured with $1
#: positions, so the count alone is not an eligibility test. Sensitivity, stated because the constant is a
#: judgement: on our seeded tape (median fill $0.02 [MEASURED: db/seed.sql, 1,090 fills]) $500 is 25,000 fills,
#: and on the kit's cited venue median ($5 [CTX]) it is 100 fills. Re-derive on production data before launch.
MIN_VERIFIED_VOLUME_MICRO = 500 * 1_000_000

#: A wallet younger than this is provisional: shown, labelled, and excluded from nothing but the "rising" board's
#: win (a seven-day-old wallet has no seven-day history to have improved on).
PROVISIONAL_DAYS = 7

#: The single-best-trade share at which a PnL board says so out loud.
LUCKY_TRADE_SHARE_BPS = 5_000

#: A category board only admits a specialist: at least half of the wallet's resolved markets in that category.
CATEGORY_SHARE_BPS = 5_000
CATEGORIES = ("Politics", "Sports", "Crypto", "Finance")

#: The denominator is `max(drawdown, VOLATILITY_MULTIPLE * sigma)`: the risk that happened, floored by the risk
#: the ledger shows was typical. At `sigma` small this reduces to the copy screen's own rule (net per unit of
#: drawdown), which is what makes the two surfaces agree by construction rather than by coincidence.
VOLATILITY_MULTIPLE = 2

#: Settled markets required *inside* the 7-day window before a rising-board placing counts as a placing.
RISING_SAMPLE = 5

WINDOWS = ("7d", "30d", "90d", "all")
ACTIVITY_WINDOWS = ("24h", "7d", "30d")

#: The boards, in the order the picker offers them. `sort` is the field the engine orders by; everything else is
#: documentation the methodology page reads.
BOARDS: tuple[dict, ...] = (
    {
        "id": "risk_adjusted",
        "label": "Risk-adjusted PnL",
        "isDefault": True,
        "kind": "skill",
        "window": "30d",
        "windows": WINDOWS,
        "sort": "scoreBps",
        "formula": ("score = net realised after fees EXCLUDING the single best market, per unit of risk, where "
                    "risk = max(largest peak-to-trough drawdown, 2 x the standard deviation of per-market "
                    "results). The best-market exclusion is uniform: it applies to every wallet, so no threshold "
                    "decides who gets trimmed, and it is skipped when the best market was a loss (there is no "
                    "lucky win to remove)."),
        "gate": ("%d settled markets in the window AND $%d verified lifetime turnover"
                 % (MIN_RESOLVED, MIN_VERIFIED_VOLUME_MICRO // 1_000_000)),
        "tieBreaks": ("score, then settled markets (more first), then smaller drawdown, then wallet id "
                      "(ascending, so the same data always produces the same board)"),
        "cadence": "hourly for 7d, daily for 30d/90d/all; every run appends a snapshot for the rank sparkline",
        "rewards": "consistent profit relative to the risk taken to get it",
        "punishes": ("churn (volume is not in the numerator), one-off luck (trimmed), and unpunished "
                     "blow-ups (a blown-up wallet keeps its negative score and stays on the board)"),
        "note": ("the default board, and the only one a copier should read as a shortlist: it is the same "
                 "risk-adjusted idea the /copy discovery list sorts by"),
    },
    {
        "id": "win_rate",
        "label": "Win rate",
        "isDefault": False,
        "kind": "skill",
        "window": "90d",
        "windows": WINDOWS,
        "sort": "winRateBps",
        "formula": "wins / settled markets, per settled MARKET (not per fill), suppressed below the gate",
        "gate": "%d settled markets in the window" % MIN_RESOLVED,
        "tieBreaks": "win rate, then settled markets, then smaller drawdown, then wallet id",
        "cadence": "daily",
        "rewards": "accuracy on a real sample",
        "punishes": "nothing directly — which is why it is not the default: a 20-for-25 record on 1-cent "
                    "longshots and a 20-for-25 record on 50-cent coins are the same 80% and different products",
        "note": "the sample gate is the board: below it there is no rate to show and the row says so",
    },
    {
        "id": "volume",
        "label": "Volume",
        "isDefault": False,
        "kind": "activity",
        "window": "30d",
        "windows": ACTIVITY_WINDOWS,
        "sort": "verifiedVolumeMicro",
        "formula": ("verified turnover = notional minus round-tripped notional (opposite-side fills in the same "
                    "market inside 10 minutes whose prices differ by no more than 0.5 cent)"),
        "gate": "any wallet with verified turnover in the window; no sample gate applies to a fact",
        "tieBreaks": "verified volume, then fills, then wallet id",
        "cadence": "hourly",
        "rewards": "the familiar board, for users who want the familiar board",
        "punishes": "self-trading: the round trip is subtracted and the subtraction is shown on the row",
        "note": ("stated on every row that wash volume was removed, because a volume board that silently "
                 "corrects is a volume board nobody can reconcile"),
    },
    {
        "id": "rising",
        "label": "Rising (7d)",
        "isDefault": False,
        "kind": "activity",
        "window": "7d",
        "windows": ("7d",),
        "sort": "improvementMicro",
        "formula": ("net realised in the last 7 days minus net realised in the 7 days before it, on the same "
                    "wallet, with both sides of the subtraction stated on the row"),
        "gate": "%d settled markets inside the 7-day window" % RISING_SAMPLE,
        "tieBreaks": "improvement, then settled markets in the window, then wallet id",
        "cadence": "hourly",
        "rewards": "new talent and genuine turnarounds",
        "punishes": ("nothing, and it is the board where a provisional wallet is most likely to appear, which is "
                     "why the provisional label is rendered on every row rather than in a footnote"),
        "note": "improvement computed from the same per-market results the other boards rank, never from a delta of deltas",
    },
    {
        "id": "category",
        "label": "Category specialists",
        "isDefault": False,
        "kind": "skill",
        "window": "30d",
        "windows": WINDOWS,
        "sort": "scoreBps",
        "categories": CATEGORIES,
        "formula": ("the risk-adjusted score computed on one category's settled markets only, for wallets whose "
                    "resolved markets in that category are at least half of their resolved markets overall"),
        "gate": ("%d settled markets in the category AND the category-share rule AND the lifetime turnover floor"
                 % MIN_RESOLVED),
        "tieBreaks": "score, then category settled markets, then wallet id",
        "cadence": "daily",
        "rewards": "people who are genuinely good at one thing, who a generalist board hides",
        "punishes": "nothing — the category-share rule is a definition of 'specialist', not a punishment",
        "note": "four boards in one: Politics, Sports, Crypto, Finance, each with its own gate",
    },
    {
        "id": "copied",
        "label": "Most copied",
        "isDefault": False,
        "kind": "social",
        "window": "30d",
        "windows": ACTIVITY_WINDOWS,
        "sort": "copiers",
        "formula": "distinct accounts currently copying this wallet (one active copy config = one copier)",
        "gate": "at least 1 active copier; the wallet must also pass the lifetime turnover floor",
        "tieBreaks": "copiers, then verified volume, then wallet id",
        "cadence": "hourly",
        "rewards": "wallets people actually trust with money — the board that feeds the copy discovery list",
        "punishes": ("copy farms: a wallet whose fills are mechanically derived from another wallet is flagged "
                     "and cannot rank here, because a farm copying a wallet is not demand for that wallet"),
        "note": ("this board is a growth loop and it is treated as one: being on it brings copiers, and copiers "
                 "bring volume. That is exactly why the farm filter is on it"),
    },
)

BOARD_IDS = tuple(b["id"] for b in BOARDS)
DEFAULT_BOARD = "risk_adjusted"


def board(board_id: str) -> dict | None:
    for b in BOARDS:
        if b["id"] == board_id:
            return b
    return None


def windows_for(board_id: str) -> tuple[str, ...]:
    b = board(board_id)
    return tuple(b["windows"]) if b else ()


def methodology(*, extra: dict | None = None) -> dict:
    """The published methodology, as data.

    Served whole, with no summarising: the page a user reads and the engine that ranks them are the same object,
    so "why am I 47th" has one answer rather than one per surface.
    """
    out = {
        "boards": [dict(b) for b in BOARDS],
        "defaultBoard": DEFAULT_BOARD,
        "sampleGate": MIN_RESOLVED,
        "minVerifiedVolumeMicro": MIN_VERIFIED_VOLUME_MICRO,
        "provisionalDays": PROVISIONAL_DAYS,
        "luckyTradeShareBps": LUCKY_TRADE_SHARE_BPS,
        "volatilityMultiple": VOLATILITY_MULTIPLE,
        "integrity": INTEGRITY_RULES,
        "note": ("every board here is computed from settled markets in our own ledger; a board that cannot be "
                 "recomputed by a user with a query is a board they have to take on faith"),
    }
    if extra:
        out.update(extra)
    return out


#: The integrity rules, each with what it does and what it does NOT do. Written as data because the
#: anti-gaming dashboard (D7) renders the same list beside the wallets it flags.
INTEGRITY_RULES: tuple[dict, ...] = (
    {
        "id": "wash",
        "label": "Wash and round-trip volume",
        "does": ("subtracts round-tripped notional (opposite side, same market, inside 10 minutes, price within "
                 "0.5 cent) from the volume every board reads"),
        "doesNot": "does not hide the wallet: it stays ranked, and the row states the amount that was removed",
        "detect": "pairwise on our own tape; no venue cooperation and no model",
    },
    {
        "id": "copy_farm",
        "label": "Copy farms",
        "does": ("flags a wallet whose fills are mechanically derived from another wallet (mirrored market and "
                 "side inside 2 minutes on at least 80% of its fills) and bars it from the copied board"),
        "doesNot": ("does not call the wallet dishonest: a farm is often a real person copying too closely, and "
                    "the row says 'derived from' rather than 'fraud'"),
        "detect": "fill-to-fill timing against each candidate source",
    },
    {
        "id": "provisional",
        "label": "New wallets",
        "does": "labels any wallet younger than 7 days provisional, with its age in days",
        "doesNot": "does not remove it from any board; the label travels with it",
        "detect": "first seen in the tape, or the wallet row's creation time, whichever is earlier",
    },
    {
        "id": "blown_up",
        "label": "Blown-up accounts",
        "does": ("shows a wallet whose equity went to zero or below after being positive as `blew up`, keeps its "
                 "real score, and counts it in the board's own summary"),
        "doesNot": ("does not drop it, rank it as unknown, or reset its score — quietly removing blown-up "
                    "accounts is how a leaderboard lies"),
        "detect": "the same cumulative curve the dossier draws",
    },
    {
        "id": "lucky",
        "label": "The lucky gambler",
        "does": ("trims the single best market in the risk-adjusted numerator and publishes that trade's share of "
                 "total PnL on the row"),
        "doesNot": "does not trim second-best results, and does not apply to the volume board, which is not a skill claim",
        "detect": "per-market realised results for the wallet, from the ledger",
    },
    {
        "id": "disputed",
        "label": "Markets under dispute",
        "does": ("excludes settled results from markets on the risk blocklist with an active uma_dispute entry, and "
                 "reports how many were excluded"),
        "doesNot": "does not silently re-settle: a disputed market's PnL is unknown, and unknown is not zero",
        "detect": "`risk_blocklists` — the same list the order path refuses on",
    },
)
