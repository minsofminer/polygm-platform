"""P10's fixtures: the terminal's data, kept in its own module so a phase that only reads P09's seed does not
have to carry it.

Five things this file builds, and why each one is shaped the way it is:

  * **The whale's own record** (24 settled markets + one open position). The phase's quality gate is "find a
    whale → open their profile → see the win rate is REAL → see the drawdown", which needs a wallet with
    enough settled markets to clear the sample gate (20) and a PnL path that goes under water. The plan is
    deliberately L L L L L L then fifteen wins then three losses: a fixture where the curve only rises cannot
    demonstrate a drawdown, and a fixture where the sample gate never fires cannot demonstrate the refusal.

  * **The whale hour** (400 fills in one hour on one market). The threshold is a p99.5 *with a floor*, so a
    fixture with a handful of fills is not a thin distribution — it is an unwinnable one: with three fills the
    p99.5 IS the largest fill, the threshold equals it, and the market has zero whales. 400 fills are what it
    takes for the percentile to be a percentile, and the three big ones on top are sized to land on the three
    severity bands exactly (`info` at the threshold, `notice` at 1.5x, `urgent` at 4x).

  * **A settled-market window with a shape** (6-29 days ago, none in the last week). The 7-day window must
    come up short of the gate while the 30-day window clears it, or "the switcher recomputes rather than
    relabels" is untestable.

  * **A copy config with a history** (`cfg-seed01`): guards, real events including skips with reasons, what-ifs
    in `copy_dry_runs`, and a source record whose 7-day window is NEGATIVE. The prompt asks for per-source
    performance "honestly, including when it is negative", and a fixture where every source wins cannot
    demonstrate that — or test it.

  * **Saved whale views**, both legal shapes: one bound to an alert rule (market-scoped, because P04's schema
    requires a target) and one that is only a filter.

Money is integer micro-USDC everywhere and every notional is `price * size // 10**6`, computed rather than
typed: the tape's own test asserts that identity on the rows it reads, so a fixture with a rounding discrepancy
would fail the thing it exists to support.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from polygm_core.money.cents import notional_floor          # noqa: E402
from polygm_core.security.pseudonym import anon             # noqa: E402

MICRO = 10 ** 6
DAY_MS = 86_400_000
HOUR_MS = 3_600_000

DEMO_USER = "u-demo"
WHALE_WALLET = "0x" + "aa" * 20          # `whale` + the settled record the dossier shows
SMART_WALLET = "0x" + "bb" * 20          # `smart_money`, and the source of the seeded copy config
INSIDER_WALLET = "0x" + "cc" * 20        # labelled, never publishable
QUIET_WALLET = "0x" + "dd" * 20          # unlabelled: the control group

# --------------------------------------------------------------------------------------------- the record
SETTLED_COUNT = 24
# Six losses, fifteen wins, three losses. The first six take the curve under water (peak 0, trough −$2,400);
# the fifteen carry it to +$6,600; the last three give back $1,200. Both the maximum drawdown (−$2,400) and a
# second, smaller one (−$1,200) therefore exist in the fixture, so a screen that renders only the last drop is
# visibly wrong rather than subtly wrong.
SETTLE_PLAN = "L" * 6 + "W" * 15 + "L" * 3
SETTLE_DAYS_AGO = list(range(29, 5, -1))         # 29..6 days: inside 30d and 90d, outside 7d
SETTLE_PRICE_MICRO = 400_000                     # 0.40, on the 0.01 tick
SETTLE_SHARES_MICRO = 1_000 * MICRO              # 1,000 shares → $400 at risk, +/-$600 on a win


def settled_markets(now: int) -> tuple[list[tuple], list[tuple]]:
    """(markets, tokens) for the 24 settled markets the whale's record is made of.

    16 fields, positional, matching `seed.MARKETS`' order — a short tuple here is an `OperationalError` that
    reads like a schema bug, which is why the column list lives in `seed.py` and this file only feeds it.
    """
    markets, tokens = [], []
    for i in range(1, SETTLED_COUNT + 1):
        mid = "0xSET%02d" % i
        won = SETTLE_PLAN[i - 1] == "W"
        end = now - SETTLE_DAYS_AGO[i - 1] * DAY_MS
        markets.append((mid, "0xSET%02d" % i, None, "Will settlement #%02d resolve Yes?" % i,
                        "settled-%02d" % i, 0, 0, 1, "0.01", "5", "None", 0, end,
                        json.dumps(["Yes", "No"]), end - 3 * DAY_MS, now))
        # `is_winner` is what makes a fill settled: the API reads it, and NULL means "we do not know yet",
        # which is a different state from "No".
        tokens.append(("%sY" % mid, mid, "Yes", 0, 1 if won else 0))
        tokens.append(("%sN" % mid, mid, "No", 1, 0 if won else 1))
    return markets, tokens


def settled_fills(now: int, conditions: dict[str, str]) -> list[tuple]:
    """The whale's buy in each settled market, six hours before that market settled.

    Buying Yes at 0.40 and holding to settlement makes the realised number legible in one line: a win is
    shares − cost, a loss is −cost, both integers, both visible in the dossier's trade history.
    """
    rows = []
    for i in range(1, SETTLED_COUNT + 1):
        mid = "0xSET%02d" % i
        at = now - SETTLE_DAYS_AGO[i - 1] * DAY_MS - 6 * HOUR_MS
        rows.append(_fill(conditions[mid], "%sY" % mid, "Yes", 0, WHALE_WALLET, "BUY",
                          SETTLE_PRICE_MICRO, SETTLE_SHARES_MICRO, at))
    return rows


def _fill(condition: str, token: str, outcome: str, index: int, wallet: str, side: str,
          price_micro: int, size_micro: int, at: int) -> tuple:
    """One `tape_fills` row, in the table's own column order, with the notional COMPUTED.

    `notional_floor` rather than `price * size // 10**6`: it is the same integer the money path uses, so a
    fixture row can never disagree with the product about what a fill was worth.
    """
    return (str(condition), str(token), str(outcome), int(index), str(wallet), str(side), int(price_micro),
            int(size_micro), notional_floor(int(size_micro), int(price_micro)), int(at), int(at) + 340,
            "ws", 0)


# ---------------------------------------------------------------------------------------- the whale hour
#: 397 background fills + the 3 whales = 400. The percentile is nearest-rank, so the 398th of 400 is the
#: threshold: exactly the smallest whale. 401 fills would move the rank onto the second whale and change every
#: severity band; the count is load-bearing.
BACKGROUND_FILLS = 397
WHALE_MARKET = "0xM1"
WHALE_HOUR_PRICE_MICRO = 500_000                 # 0.50, on that market's 0.01 tick
#: Background sizes 200..300 shares: a $100-$150 fill, median $125. The median matters twice — it decides the
#: market's size bucket, and it is the term D4's tunable view multiplies (5x median = $625, which clears the
#: $500 floor, so the multiple mode has a market where the relative term actually wins).
BACKGROUND_SIZES = (200, 225, 250, 275, 300)
WHALES = ((12_000, "BUY"), (18_000, "BUY"), (50_000, "BUY"))    # $6,000 / $9,000 / $25,000 at 0.50


def whales_last_hour(now: int, conditions: dict[str, str]) -> list[tuple]:
    """400 fills in the last few minutes, one market, three of them whales.

    Timestamps sit 90-190 seconds back, which is inside the 1-hour window the tests filter on and *older* than
    the generated tape's newest rows: the default tape page must stay "whatever traded last" (a burst of small
    fills), because the phase's own test pins that the whale is found through a FILTER and a FEED rather than
    by scrolling. A fixture where a whale happens to be first would let a broken filter pass.
    """
    cond = conditions[WHALE_MARKET]
    rows, base = [], now - 90_000
    for k in range(BACKGROUND_FILLS):
        wallet = SMART_WALLET if k % 2 else QUIET_WALLET
        side = "SELL" if k % 3 == 0 else "BUY"       # both sides exist, so the side facet is not one-sided
        size = BACKGROUND_SIZES[k % len(BACKGROUND_SIZES)] * MICRO
        rows.append(_fill(cond, "0xT10", "Yes", 0, wallet, side, WHALE_HOUR_PRICE_MICRO, size,
                          base - k * 250))
    for j, (shares, side) in enumerate(WHALES):
        # 30s apart, newest last, so the feed's sort (notional desc) is the only thing deciding the order.
        rows.append(_fill(cond, "0xT10", "Yes", 0, WHALE_WALLET, side, WHALE_HOUR_PRICE_MICRO,
                          shares * MICRO, base - (len(WHALES) - j) * 30_000))
    return rows


def open_position_fills(now: int, conditions: dict[str, str]) -> list[tuple]:
    """One unsettled buy, so the dossier and the portfolio both have a position to mark.

    0xM2 trades on a 0.001 tick and this fill is on it: a fixture price that is off its own market's tick is a
    fixture that quietly exercises the off-tick branch in every screen that renders a mark.
    """
    return [_fill(conditions["0xM2"], "0xT20", "Yes", 0, WHALE_WALLET, "BUY", 425_000, 250 * MICRO,
                  now - 2 * DAY_MS)]


# ------------------------------------------------------------------------------- the copy engine's state
def copy_state(now: int) -> dict:
    """The copy config, its guards, what it really did, what it would have done, and the source's record.

    `copy_source_stats` windows: 7d NEGATIVE (−$120), 30d positive with a drawdown, 90d positive. The negative
    window is the fixture's most important row — it is what makes "honest when it is negative" testable.
    """
    return {
        "config": ("cfg-seed01", DEMO_USER, SMART_WALLET, "ratio", 2_500, 250 * MICRO, 1_000 * MICRO,
                   json.dumps(["0xM5"]), 0, now - 3 * DAY_MS),
        "guard": ("cfg-seed01", 1, 2, 24, "Politics", 50_000, 900_000, 850_000, 200_000, None,
                  now - 3 * DAY_MS),
        # (copier_config, source, source_intent, intent, action, reason, deviation_bps, at_ms) — REAL events
        # only. `copier_id` carries the CONFIG id and not the user id: that is how the engine's own
        # `_day_counts`/`_last_copy_ms` read the column (`copier_id = cfg.id`), and a per-user key would go
        # blind the moment somebody ran two configs against the same source.
        "events": [
            ("cfg-seed01", SMART_WALLET, "src-1", "int-1", "copied", "", 12, now - 3 * DAY_MS + 1000),
            ("cfg-seed01", SMART_WALLET, "src-2", "int-2", "copied", "", 30, now - 3 * DAY_MS + 2000),
            ("cfg-seed01", SMART_WALLET, "src-3", "int-3", "copied", "", 45, now - 2 * DAY_MS + 1000),
            ("cfg-seed01", SMART_WALLET, "src-4", "", "skipped", "moved_past_limit", 0,
             now - 2 * DAY_MS + 2000),
            ("cfg-seed01", SMART_WALLET, "src-5", "int-5", "copied", "", 60, now - 2 * DAY_MS + 3000),
            ("cfg-seed01", SMART_WALLET, "src-6", "int-6", "copied", "", 18, now - DAY_MS + 1000),
            ("cfg-seed01", SMART_WALLET, "src-7", "", "skipped", "resolved_soon", 0, now - DAY_MS + 2000),
            ("cfg-seed01", SMART_WALLET, "src-8", "int-8", "copied", "", 25, now - DAY_MS + 3000),
            ("cfg-seed01", SMART_WALLET, "src-9", "int-9", "copied", "", 90, now - 6 * HOUR_MS),
            ("cfg-seed01", SMART_WALLET, "src-10", "", "skipped", "category_filtered", 0,
             now - 5 * HOUR_MS),
            ("cfg-seed01", SMART_WALLET, "src-11", "int-11", "copied", "", 40, now - 4 * HOUR_MS),
            ("cfg-seed01", SMART_WALLET, "src-12", "", "skipped", "daily_cap_reached", 0,
             now - 2 * HOUR_MS),
        ],
        # What the engine WOULD have done, in its own table: a simulation is not a fill, and a `dry_run` column
        # on `copy_events` would be the thing that lets a monitor merge the two by forgetting a WHERE.
        "dry_runs": [
            ("cfg-seed01", DEMO_USER, SMART_WALLET, "src-13", "0xM9", "enter", 120 * MICRO, 42_000, 41_000,
             24, "would copy: within the limit", now - 3 * HOUR_MS),
            ("cfg-seed01", DEMO_USER, SMART_WALLET, "src-14", "0xM2", "enter", 80 * MICRO, 410_000, 408_000,
             49, "would copy: within the limit", now - 2 * HOUR_MS),
            ("cfg-seed01", DEMO_USER, SMART_WALLET, "src-15", "0xSET07", "skip", 0, 0, 505_000, 0,
             "do_not_enter_within_24h: resolves soon", now - 90 * 60_000),
            ("cfg-seed01", DEMO_USER, SMART_WALLET, "src-16", "0xM5", "skip", 0, 0, 610_000, 0,
             "category_filter: Politics is not in this config's filter", now - 60 * 60_000),
            ("cfg-seed01", DEMO_USER, SMART_WALLET, "src-17", "0xM9", "skip", 0, 0, 12_000, 120,
             "skip_if_moved: source moved 12 cents past our entry", now - 30 * 60_000),
        ],
        # `copy_source_stats`' own columns. The 7-day window is NEGATIVE on purpose.
        #
        # Three sources, because D7's discovery list needs a sort it can be wrong about. The gambler
        # (INSIDER_WALLET, `0xcc…`) has the LARGEST net PnL of the three and the WORST risk-adjusted number:
        # $1.9M earned through a $1.9M drawdown. A list ordered by raw PnL puts it first; the list D7 asks for
        # puts it last, and the fixture is what makes "ranked risk-adjusted" a testable claim rather than a
        # sentence in a tooltip. QUIET_WALLET stays below the sample gate on purpose: its win rate is `null`
        # with the reason, in the same list as two rates that are not.
        "source_stats": [
            (SMART_WALLET, 7, 7, 4285, -120 * MICRO, 2 * MICRO, -122 * MICRO, 180 * MICRO, 3, 820, now),
            (SMART_WALLET, 30, 21, 5714, 640 * MICRO, 9 * MICRO, 631 * MICRO, 410 * MICRO, 4, 760, now),
            (SMART_WALLET, 90, 44, 6136, 1_450 * MICRO, 18 * MICRO, 1_432 * MICRO, 520 * MICRO, 5, 910, now),
            (INSIDER_WALLET, 7, 9, 6666, 900 * MICRO, 4 * MICRO, 896 * MICRO, 1_100 * MICRO, 2, 1_400, now),
            (INSIDER_WALLET, 30, 31, 5806, 1_900 * MICRO, 12 * MICRO, 1_888 * MICRO, 1_900 * MICRO, 6, 1_350,
             now),
            (INSIDER_WALLET, 90, 52, 5577, 1_650 * MICRO, 21 * MICRO, 1_629 * MICRO, 2_050 * MICRO, 9, 1_500,
             now),
            (QUIET_WALLET, 7, 2, 5000, 40 * MICRO, 0, 40 * MICRO, 30 * MICRO, 1, 600, now),
            (QUIET_WALLET, 30, 4, 5000, 85 * MICRO, 1 * MICRO, 84 * MICRO, 60 * MICRO, 2, 640, now),
            (QUIET_WALLET, 90, 5, 6000, 96 * MICRO, 1 * MICRO, 95 * MICRO, 70 * MICRO, 2, 700, now),
        ],
    }


def whale_views(now: int) -> tuple[list[tuple], list[tuple]]:
    """(alert_rules, whale_views): one view bound to a rule, one that is only a filter.

    The bound one is market-scoped because `alert_rules` has a CHECK requiring a target — a global notifying
    view is refused by the schema, and the fixture shows the two legal shapes so the UI's two states are both
    representable.
    """
    rules = [("rule-whale-01", DEMO_USER, "0xM1", None, "whale_fill", 4, HOUR_MS,
              json.dumps({"severity": "urgent", "channel": "telegram", "source": "whale_view"}), 1,
              now - DAY_MS)]
    views = [("wv-seed-01", DEMO_USER, "Fed-market whales", json.dumps({"minSeverity": "urgent"}),
              "telegram", "urgent", "market", "0xM1", "rule-whale-01", now - DAY_MS),
             # No channel: a global view cannot notify (`rule_has_target`, and now also the view's own CHECK),
             # so the fixture ships it as what it legally is - a filter you look at.
             ("wv-seed-02", DEMO_USER, "Everything over notice", json.dumps({"minSeverity": "notice"}),
              None, "notice", "global", None, None, now - 2 * DAY_MS)]
    return rules, views


def pseudonyms(now: int) -> list[tuple]:
    """The address↔pseudonym pairs, so a `?wallet=w_…` lookup is a read rather than a hash of the whole tape.

    Every wallet the seed trades as, not just the labelled ones: an unlabelled wallet is still a trader whose
    own fills the dossier has to answer for.
    """
    seen = []
    for w in (WHALE_WALLET, SMART_WALLET, INSIDER_WALLET, QUIET_WALLET):
        seen.append((w, anon(w), now))
    return seen


def all_rows(now: int, live_fills: list[tuple], conditions: dict[str, str]) -> dict:
    """Everything this fixture contributes, ready for `seed.py` to merge.

    `live_fills` and `conditions` come from the seed's own tables rather than being re-typed here: a condition
    id invented in this module (they look like `0xM9` to a human) joins to no market, and a fill that joins to
    no market is a row the tape renders as blank rather than as an error.
    """
    markets, tokens = settled_markets(now)
    # The settled markets are OURS, so their conditions come from this module and not from the caller: a
    # condition the caller was asked to invent for a market it had never heard of is a condition nobody can
    # check. Anything the caller passed for an existing market is kept.
    conditions = dict(conditions, **{m[0]: m[1] for m in markets})
    fills = settled_fills(now, conditions) + open_position_fills(now, conditions) + \
        whales_last_hour(now, conditions)
    # The settled markets carry a category: a tape row whose market has no category renders in "Other", and the
    # category facet would then be missing the very market the feed's biggest fills came from.
    meta = [(m[0], "Politics", "https://example.org/settlement-record",
             "Resolves Yes if the settlement is certified by the returning officer; the record is published "
             "with the count.", None, now) for m in markets]
    rules, views = whale_views(now)
    return {"markets": markets, "tokens": tokens, "fills": fills, "rules": rules, "views": views,
            "meta": meta,
            "pseudonyms": pseudonyms(now), "copy": copy_state(now)}


if __name__ == "__main__":                      # a fixture you cannot run is a fixture nobody checks
    now = 1_789_000_000_000
    conds = {"0xM1": "0xC1", "0xM2": "0xC2"}
    for i in range(1, SETTLED_COUNT + 1):
        conds["0xSET%02d" % i] = "0xSET%02d" % i
    r = all_rows(now, [], conds)
    by_size = sorted((f[8] for f in r["fills"]), reverse=True)[:4]
    print("markets=%d tokens=%d fills=%d rules=%d views=%d pseudonyms=%d" %
          (len(r["markets"]), len(r["tokens"]), len(r["fills"]), len(r["rules"]), len(r["views"]),
           len(r["pseudonyms"])))
    print("largest notionals (micro):", by_size)
