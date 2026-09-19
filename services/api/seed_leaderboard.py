"""P11's fixture: sixty wallets, built so the integrity rules have something to refuse.

Why a separate seed rather than four more rows in `db/seed.sql`: a leaderboard needs a *population*, and a
population big enough to have a rank 47 is 60 wallets x ~30 settled markets — which would put thousands of
markets and fills into every other phase's fixtures, and the phases that count things (the tape's page, the
whale hour's 400 fills, the copy monitor's history) would start failing for reasons that have nothing to do
with them. So this is opt-in:

    python3 services/api/seed_leaderboard.py --sqlite        # on a database that has been migrated

Five adversarial cases are present in the data on purpose, because a rule that is only ever tested against
well-behaved wallets is a rule nobody has run:

  * **the lucky gambler** (`WALLET_LUCKY`) — one 0.05-price position of 20,000 shares is most of its PnL, so the
    row has to say so out loud and the trim has to take that market out of the numerator;
  * **the blown-up account** (`WALLET_BLOWN`) — twenty wins, then three losses that take it under water. It stays
    on the board with a negative score, and the board's own summary counts it;
  * **the washer** (`WALLET_WASH`) — ten round trips inside ten minutes at the same price, so the volume board
    discounts $20,000 of notional and prints the subtraction;
  * **the copy farm** (`WALLET_FARM`) — twelve fills mirroring `WALLET_LEAD`'s market and side within 90 seconds,
    with a real copier following it, so the ONLY thing refusing it on the copied board is the farm rule;
  * **the provisional wallet** (`WALLET_NEW`) — three days old with twenty-one settled markets: labelled, and
    barred from the rising board for having no seven-day history to have improved on.

Two refusals are also built in, because "why am I not on it" has two answers: `WALLET_THIN` has a real record on
nine markets (the SAMPLE refuses it) and `WALLET_DUST` has twenty-one markets of forty cents each (the TURNOVER
floor refuses it).

The shape that the phase's gate asks about — a wallet at rank 47 with fewer settled markets than the one at rank
12 — is a property of this population, not a coincidence, and `tests/test_leaderboard_api.py` asserts it against
the ranked board rather than against the plan table below.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from polygm_core.money.cents import notional_floor          # noqa: E402
from polygm_core.security.pseudonym import anon             # noqa: E402

MICRO = 10 ** 6
DAY_MS = 86_400_000
HOUR_MS = 3_600_000

#: Every wallet bought at 0.40 and held: a win is +$600 on 1,000 shares and a loss is −$400, so a row's whole
#: arithmetic is legible in one line and the sample gate is the only thing that decides eligibility.
PRICE_MICRO = 400_000
SHARES_MICRO = 1_000 * MICRO
WIN_MICRO = SHARES_MICRO - PRICE_MICRO * 1_000          # 600e6
LOSS_MICRO = -(PRICE_MICRO * 1_000)                     # −400e6

#: Market counts, cycled against the quality order. Deliberately NOT sorted by quality: the gate's question is a
#: wallet at rank 47 with FEWER settled markets than the wallet at rank 12, and if the sample size tracked the
#: quality there would be no such pair to show.
SIZES = (22, 48, 26, 34, 40, 30, 25, 44, 36, 21)

#: Wallets whose last 21 markets resolved inside the last week: they are what gives the 7-day window a board
#: (everyone else has six results there, which the sample gate correctly refuses).
HOT = 5
RECENT_MARKETS = 6

CATEGORIES = ("Politics", "Sports", "Crypto", "Finance")

#: Who is being copied, and by how many accounts. The farm is copied once — which is what makes the farm rule
#: the ONLY thing refusing it on the copied board (with no copier at all, the board would refuse it for having
#: no copiers and the fixture would be demonstrating a different sentence).

#: The specimens' addresses are deliberately OUTSIDE the generated range (`wallet_for()` repeats 0x10..0x4b), and
#: that is not cosmetic: the first version of this fixture used `0x11..0x66`, which silently aliased four
#: specimens onto four generated wallets — the lucky gambler's record was added to somebody else's, and the
#: provisional wallet's age came from a wallet that had been trading for a month. A fixture whose identities
#: collide tests the population it accidentally built.
WALLET_LEAD = "0x" + "0a" * 20
WALLET_FARM = "0x" + "0b" * 20
WALLET_LUCKY = "0x" + "0c" * 20
WALLET_BLOWN = "0x" + "0d" * 20
WALLET_WASH = "0x" + "0e" * 20
WALLET_NEW = "0x" + "0f" * 20
WALLET_THIN = "0x" + "7a" * 20
WALLET_DUST = "0x" + "7b" * 20

#: The wallet whose highest-numbered market is under an active UMA dispute: its result is withheld and counted,
#: never settled to zero.
INDEX_DISPUTED = 8


def wallet_for(i: int) -> str:
    return "0x" + ("%02x" % (0x10 + i)) * 20


def _quality_record(i: int) -> tuple[int, int]:
    """`(settled markets, wins)` for the i-th wallet in the quality order.

    Quality is a win rate falling from 0.86 to 0.36 across the population; the market count comes from `SIZES`,
    so a wallet's rank is decided by the risk-adjusted score (net after the single best market, per unit of
    drawdown) rather than by how much it traded.
    """
    n = SIZES[i % len(SIZES)]
    p_bps = 8_600 - (5_000 * i) // 59
    wins = max(1, (n * p_bps) // 10_000)
    return n, min(wins, n - 3)          # the first three results of every record are losses


def plan() -> list[dict]:
    """The population: one record per wallet, in the order the comments above describe.

    Two of the ranks the gate asks about are placed explicitly rather than left to the formula: the wallet
    intended for rank 12 gets a large sample (48 markets) and the one intended for rank 47 a small one (22),
    with the win rates that put them there. That is what makes "fewer resolved markets, better rank" a
    demonstration with real numbers instead of a fixture tuned until it agreed.
    """
    out = []
    for i in range(60):
        n, wins = _quality_record(i)
        out.append({"wallet": wallet_for(i), "markets": n, "wins": wins, "shape": "plain"})
    out[0] = {"wallet": WALLET_LEAD, "markets": 48, "wins": 40, "shape": "plain"}
    # Rank 12: a large sample with a mid-strong record — the wallet the gate compares against. Sitting it at
    # rank 12 rather than in the top three is the point: the answer to "why is 47 above 12" has to be a metric,
    # not a sample, so the pair has to be two REAL rows from a dense part of the board.
    out[11] = {"wallet": wallet_for(11), "markets": 48, "wins": 31, "shape": "plain"}
    out[46] = {"wallet": wallet_for(46), "markets": 22, "wins": 11, "shape": "plain"}
    out[47] = {"wallet": wallet_for(47), "markets": 24, "wins": 12, "shape": "plain"}
    # The specimens. `hot` puts their results inside the last week; `new` is the 3-day-old wallet, whose whole
    # record is inside the 7-day window and which the rising board must refuse for its age rather than its size.
    out.append({"wallet": WALLET_LUCKY, "markets": 24, "wins": 17, "shape": "lucky"})
    out.append({"wallet": WALLET_BLOWN, "markets": 24, "wins": 18, "shape": "blowup"})
    out.append({"wallet": WALLET_NEW, "markets": 21, "wins": 15, "shape": "new", "hot": True})
    for seq, rec in enumerate(out):
        rec.setdefault("hot", seq < HOT)
        rec["seq"] = seq
        rec["primary"] = CATEGORIES[seq % len(CATEGORIES)]
    return out


def _market_id(i: int, k: int) -> str:
    return "0xLB%02dM%02d" % (i, k)


def _condition(i: int, k: int) -> str:
    return "0xLB%02dC%02d" % (i, k)


def _token(condition: str, yes: bool) -> str:
    return "%s%s" % (condition, "Y" if yes else "N")


def _at_ms(now: int, i: int, k: int, hot: bool) -> int:
    """When the k-th result of the i-th wallet happened: the most recent block inside the last week, the rest
    spread over the three weeks before it.

    The split is what makes the windows differ: the 7-day board sees only the recent block (and correctly
    refuses everyone but the hot wallets, which have twenty-one results there), while the 30-day board sees
    everything.
    """
    recent = 21 if hot else RECENT_MARKETS
    if k < recent:
        return now - (1 + (k % 6)) * DAY_MS - (i % 7) * HOUR_MS
    older = k - recent
    return now - (8 + (older % 21)) * DAY_MS - (i % 7) * HOUR_MS


def rows(now: int) -> dict:
    """Every row this fixture writes, in the tables' own column orders."""
    markets, tokens, meta, fills, pseudonyms = [], [], [], [], []
    everyone = plan()
    for rec in everyone:
        seq, wallet, n, wins = rec["seq"], rec["wallet"], rec["markets"], rec["wins"]
        hot, shape, primary = rec["hot"], rec["shape"], rec["primary"]
        # The shape of a record: the first three results are losses, then the wins, then losses again. Written as
        # a sequence rather than as a set of wins so the drawdown is a real number, and so "the best market was a
        # profit" (the trim rule's condition) is true for every wallet that has a profit at all.
        for k in range(n):
            won = 3 <= k < 3 + wins
            mid, cond = _market_id(seq, k), _condition(seq, k)
            at = _at_ms(now, seq, k, hot)
            price, shares = PRICE_MICRO, SHARES_MICRO
            if shape == "lucky" and k == n - 1:
                price, shares = 50_000, 20_000 * MICRO     # 0.05 x 20,000 shares: a $19,000 win on one market
                won = True
            elif shape == "blowup" and k >= n - 3:
                price, shares = 900_000, 10_000 * MICRO    # −$9,000 a loss, three of them, after twenty wins
                won = False
            # 60% of a wallet's markets are in its primary category, so the category boards have genuine
            # specialists over the 50% share rule and the generalists are correctly refused.
            category = primary if (k * 7) % 10 < 6 else CATEGORIES[(seq + k) % len(CATEGORIES)]
            markets.append((mid, cond, None, "%s #%02d: will it resolve Yes?" % (category, k),
                            "lb-%02d-%02d" % (seq, k), 0, 0, 1, "0.01", "5", "None", 0,
                            at + 3 * DAY_MS, json.dumps(["Yes", "No"]), at - 2 * DAY_MS, now))
            tokens.append((_token(cond, True), mid, "Yes", 0, 1 if won else 0))
            tokens.append((_token(cond, False), mid, "No", 1, 0 if won else 1))
            meta.append((mid, category, "https://example.org/resolution",
                         "Resolves Yes if the referenced event occurs.", None, now))
            fills.append((cond, _token(cond, True), "Yes", 0, wallet, "BUY", price, shares,
                          notional_floor(shares, price), at, at + 120, "ws", 0))
        pseudonyms.append((wallet, anon(wallet),
                           now - (2 * DAY_MS if shape == "new" else 40 * DAY_MS)))

    # ---- the washer: ten round trips in the same ten minutes. Opposite side, same market, prices identical, so
    # every pair is a wash by the published rule and the volume board must discount all of it.
    for k in range(10):
        cond = "0xLBWASHC%02d" % k
        mid = "0xLBWASHM%02d" % k
        at = now - 5 * DAY_MS + k * 60_000
        markets.append((mid, cond, None, "Will the wash market #%02d resolve Yes?" % k, "lb-wash-%02d" % k,
                        0, 0, 1, "0.01", "5", "None", 0, at + 2 * DAY_MS, json.dumps(["Yes", "No"]),
                        at - DAY_MS, now))
        tokens.append((_token(cond, True), mid, "Yes", 0, 1))
        tokens.append((_token(cond, False), mid, "No", 1, 0))
        meta.append((mid, "Finance", "https://example.org/resolution", "Resolves Yes.", None, now))
        for side, offset in (("BUY", 0), ("SELL", 120_000)):
            fills.append((cond, _token(cond, True), "Yes", 0, WALLET_WASH, side, PRICE_MICRO, 5_000 * MICRO,
                          notional_floor(5_000 * MICRO, PRICE_MICRO), at + offset, at + offset + 90, "ws", 0))

    # ---- the copy farm: twelve fills, each 90 seconds AFTER a fill the lead wallet made in the same market on
    # the same side. That is the detection's signature, and it is reproduced exactly rather than approximated.
    lead = next(r for r in everyone if r["wallet"] == WALLET_LEAD)
    for k in range(12):
        cond = _condition(lead["seq"], k)
        at = _at_ms(now, lead["seq"], k, lead["hot"]) + 90_000
        fills.append((cond, _token(cond, True), "Yes", 0, WALLET_FARM, "BUY", PRICE_MICRO, SHARES_MICRO,
                      notional_floor(SHARES_MICRO, PRICE_MICRO), at, at + 60, "ws", 0))


    # ---- the two refusals and the disputed market.
    thin, dust = [], []
    pseudonyms.append((WALLET_FARM, anon(WALLET_FARM), now - 30 * DAY_MS))
    for k in range(9):                                     # nine settled markets: the SAMPLE refuses it
        mid, cond = _market_id(90, k), _condition(90, k)
        at = _at_ms(now, 90, k, False)
        markets.append((mid, cond, None, "Thin record #%02d" % k, "lb-thin-%02d" % k, 0, 0, 1, "0.01", "5",
                        "None", 0, at + 2 * DAY_MS, json.dumps(["Yes", "No"]), at - DAY_MS, now))
        tokens.append((_token(cond, True), mid, "Yes", 0, 1 if k < 6 else 0))
        tokens.append((_token(cond, False), mid, "No", 1, 0 if k < 6 else 1))
        meta.append((mid, "Sports", "https://example.org/resolution", "Resolves Yes.", None, now))
        thin.append((cond, _token(cond, True), "Yes", 0, WALLET_THIN, "BUY", PRICE_MICRO, SHARES_MICRO,
                     notional_floor(SHARES_MICRO, PRICE_MICRO), at, at + 60, "ws", 0))
    for k in range(21):                                    # twenty-one markets of forty cents: the FLOOR refuses it
        mid, cond = _market_id(91, k), _condition(91, k)
        at = _at_ms(now, 91, k, False)
        markets.append((mid, cond, None, "Dust record #%02d" % k, "lb-dust-%02d" % k, 0, 0, 1, "0.01", "5",
                        "None", 0, at + 2 * DAY_MS, json.dumps(["Yes", "No"]), at - DAY_MS, now))
        tokens.append((_token(cond, True), mid, "Yes", 0, 1 if k < 8 else 0))
        tokens.append((_token(cond, False), mid, "No", 1, 0 if k < 8 else 1))
        meta.append((mid, "Crypto", "https://example.org/resolution", "Resolves Yes.", None, now))
        dust.append((cond, _token(cond, True), "Yes", 0, WALLET_DUST, "BUY", PRICE_MICRO, 1 * MICRO,
                     notional_floor(1 * MICRO, PRICE_MICRO), at, at + 60, "ws", 0))
    fills += thin + dust
    pseudonyms.append((WALLET_THIN, anon(WALLET_THIN), now - 40 * DAY_MS))
    pseudonyms.append((WALLET_DUST, anon(WALLET_DUST), now - 40 * DAY_MS))

    # The most-copied board needs wallets that are actually being copied: the top of the plan, by name rather
    # than by index, so a change to the ladder cannot leave the board empty.
    top = [r["wallet"] for r in everyone[:3]]
    copies = [(WALLET_FARM, 1)] + [(w, n) for w, n in zip(top, (3, 2, 1))]
    disputed = _condition(INDEX_DISPUTED, 0)
    return {"markets": markets, "tokens": tokens, "meta": meta, "fills": fills, "pseudonyms": pseudonyms,
            "blocklist": [(disputed, "UMA dispute opened on the resolution source", "uma_dispute", "seed", now,
                           None)],
            "copiers": copies, "plan": everyone}


def seed_sqlite(db_path: str | None = None, *, now: int | None = None) -> dict:
    """Write the population into an already-migrated database, replacing any previous run of itself.

    Idempotent by RESETTING its own rows (markets by id prefix, fills by wallet) rather than by skipping: a
    re-seed that kept yesterday's results would leave a board that is half one population and half another, and
    the ranks would be arithmetically correct and completely meaningless.
    """
    path = db_path or os.environ.get("PGM_DB_PATH", str(ROOT / "var" / "polygm.db"))
    now = int(now if now is not None else __import__("time").time() * 1000)
    r = rows(now)
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("BEGIN")
    try:
        for table, cols in (("tokens", "token_id"), ("market_meta", "market_id")):
            con.execute("DELETE FROM %s WHERE %s LIKE '0xLB%%'" % (table, cols))
        con.execute("DELETE FROM tokens WHERE market_id LIKE '0xLB%%'")
        con.execute("DELETE FROM alert_rules WHERE market_id LIKE '0xLB%%'")
        # Scoped to its OWN markets, so the base fixture's tape survives a re-seed. `tape_fills` is not an
        # append-only table in this schema (`tape_trades` is), and an earlier version of this file dropped and
        # recreated triggers for it anyway — which put a constraint on the database that no migration declares,
        # and broke the base seed on every later run. A fixture may delete its own rows; it may not invent rules.
        con.execute("DELETE FROM tape_fills WHERE condition_id LIKE '0xLB%%'")
        con.execute("DELETE FROM markets WHERE id LIKE '0xLB%%'")
        con.execute("DELETE FROM risk_blocklists WHERE added_by='seed' AND market_id LIKE '0xLB%%'")
        con.execute("DELETE FROM copy_configs WHERE id LIKE 'lb-farm%%'")
        for row in r["markets"]:
            con.execute("INSERT OR REPLACE INTO markets (id,condition_id,event_id,question,slug,"
                        "accepting_orders,seconds_delay,enable_order_book,minimum_tick_size,minimum_order_size,"
                        "fee_type,neg_risk,end_ts,outcomes_json,first_seen_ms,updated_ms)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        con.executemany("INSERT OR REPLACE INTO tokens (token_id,market_id,outcome,outcome_index,is_winner)"
                        " VALUES (?,?,?,?,?)", r["tokens"])
        con.executemany("INSERT OR REPLACE INTO market_meta (market_id,category,resolution_source,"
                        "resolution_criteria,image_url,updated_ms) VALUES (?,?,?,?,?,?)", r["meta"])
        con.executemany("INSERT INTO tape_fills (condition_id,token_id,outcome,outcome_index,wallet,side,"
                        "price_micro,size_micro,usd_notional_micro,ts_ms,ingest_ms,source,fee_rate_bps,dedupe_key)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [row + ("lb-%s-%d" % (row[0], i),) for i, row in enumerate(r["fills"])])
        con.executemany("INSERT OR REPLACE INTO risk_blocklists (market_id,reason,source,added_by,added_ms,"
                        "expires_ms) VALUES (?,?,?,?,?,?)", r["blocklist"])
        for i, (wallet, owner) in enumerate(r["copiers"]):
            for c in range(int(owner)):
                con.execute("INSERT OR REPLACE INTO copy_configs (id,user_id,source_user,mode,ratio_bps,"
                            "max_order_micro,max_daily_micro,blocked_markets,enabled,created_ms)"
                            " VALUES (?,?,?,'mirror',10000,?,?, '',1,?)",
                            ("lb-copy-%02d-%d" % (i, c), "u-demo", wallet, 50 * MICRO, 200 * MICRO,
                             now - 20 * DAY_MS))
        con.executemany("INSERT OR REPLACE INTO wallet_pseudonyms (wallet_id,anon_id,first_seen_ms)"
                        " VALUES (?,?,?)", r["pseudonyms"])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return {"wallets": len({f[4] for f in r["fills"]}), "markets": len(r["markets"]), "fills": len(r["fills"]),
            "blocklist": len(r["blocklist"]), "atMs": now}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="seed the P11 leaderboard population")
    ap.add_argument("--sqlite", action="store_true", help="write into $PGM_DB_PATH (or var/polygm.db)")
    ap.add_argument("--db", default=None, help="an explicit database path")
    args = ap.parse_args(argv)
    if not args.sqlite and not args.db:
        ap.error("pass --sqlite (or --db <path>) — this writes rows, it does not print them")
    print("seeded leaderboard population:", json.dumps(seed_sqlite(args.db)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
