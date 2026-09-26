#!/usr/bin/env python3
"""Seed data for the dev engine (SQLite) and a generator for db/seed.sql (Postgres).

Values are the ones MEASURED in P01 (docs/verification/P01-probe.json), not invented: a 5-share minimum,
0.001/0.01 ticks, six outcome markets, a neg-risk multi-candidate market, and a book whose spread sits at
the observed median of ~71 ticks. Seeding with pretty numbers is how a dev environment starts agreeing with
a UI that would break against the real venue.

Run:
    python3 services/api/seed.py                # seed the SQLite dev DB at $PGM_DB_PATH
    python3 services/api/seed.py --emit-sql     # print db/seed.sql for Postgres
"""
from __future__ import annotations
import argparse, json, os, sqlite3, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT / "services" / "api"))


#: While `emit_sql` is writing the file, the clock is PINNED to one value. Every timestamp in the generated
#: file is then a difference from that single instant, which is what makes the artefact byte-stable: without the
#: pin, `rows()` and `_pg_time` each read the wall clock, so regenerating the file a second later rewrote two
#: thousand lines with offsets one millisecond different — an artefact nobody can review and everybody conflicts
#: on. The pin lasts exactly as long as the emission; every other caller gets the real clock.
_PINNED_NOW_MS: int | None = None


def _now_ms() -> int:
    return _PINNED_NOW_MS if _PINNED_NOW_MS is not None else int(time.time() * 1000)


# P10's fixtures live in their own module: this file is the phase-agnostic writer (it knows column orders and
# trigger juggling), and `seed_terminal` knows what the terminal needs. Merging them here rather than having
# the terminal module write its own rows keeps ONE writer, which is what makes `make seed` idempotent.
from seed_terminal import all_rows as terminal_rows                # noqa: E402


# token ids are venue-shaped (large decimal strings) because code that assumes they are small ints is the
# bug we most need a seed to catch.
MARKETS = [
    dict(id="0xM1", question="Will the Fed cut rates at the September meeting?", slug="fed-cut-sept",
         tick="0.01", min_size="5", fee="None", neg_risk=False, accepting=True, delay=0, book=True,
         outcomes=["Yes", "No"], condition="0xC1", end_days=12),
    dict(id="0xM2", question="Will BTC close above $150k on 30 September?", slug="btc-150k-sep",
         tick="0.001", min_size="5", fee="None", neg_risk=False, accepting=True, delay=0, book=True,
         outcomes=["Yes", "No"], condition="0xC2", end_days=13),
    dict(id="0xM3", question="Illiquid meme-coin listing market", slug="meme-listing",
         tick="0.001", min_size="5", fee="maker_rebate", neg_risk=False, accepting=True, delay=0, book=True,
         outcomes=["Yes", "No"], condition="0xC3", end_days=5, thin=True),
    dict(id="0xM4", question="Governance vote outcome (no order book)", slug="gov-vote",
         tick="0.01", min_size="5", fee="None", neg_risk=False, accepting=True, delay=3, book=False,
         outcomes=["Yes", "No"], condition="0xC4", end_days=2),
    dict(id="0xM5", question="Who wins the 2027 mayoral race? (neg-risk, 6 candidates)",
         slug="mayor-2027", tick="0.01", min_size="5", fee="none", neg_risk=True, accepting=True,
         delay=0, book=True,
         outcomes=["Amina", "Bhargava", "Castellanos", "Dube", "Eze", "Farrow"],
         condition="0xC5", end_days=210),
    dict(id="0xM6", question="Closed market (not accepting orders)", slug="closed",
         tick="0.01", min_size="5", fee="None", neg_risk=False, accepting=False, delay=0, book=True,
         outcomes=["Yes", "No"], condition="0xC6", end_days=-1),
    # P09's quality gate needs a book that a naive ladder renders as broken. This is the P01 observation, with
    # the numbers P01 measured: 94 ask levels climbing away from the 0.001 tick, $21.9M of notional, and ZERO
    # bids. It is not a bug in our ingest and not a gap in the feed - it is what a market looks like when one
    # side has genuinely walked away - so the fixture has to contain it or the treatment is never exercised.
    dict(id="0xM9", question="Will the incumbent carry the recount? (one-sided book)", slug="recount-incumbent",
         tick="0.001", min_size="5", fee="None", neg_risk=False, accepting=False, delay=0, book=True,
         outcomes=["Yes", "No"], condition="0xC9", end_days=-1, one_sided=True),
]

# --------------------------------------------------------------------------- #
# P09 · the market surfaces
#
# `mid` is optional and defaults to 0.50 (the one price the earlier phases were written against). A multi-
# outcome event whose every candidate sits at 0.50 is not a fixture, it is a fiction: the whole point of D2 is
# that the outcomes sum to 1, and a fixture that cannot sum to anything cannot test that.
#
# The 128-outcome event is modelled as 128 MARKETS under one event, which is the venue's own shape and the
# prompt's language ("128 markets under one event"). That is also the only shape our tables can carry: a book
# is keyed (market_id, side, price), so per-outcome quotes need one market per outcome. A single market with
# 128 outcome TOKENS would have one book and 128 unpriced outcomes, i.e. a table where every row but one says
# "unknown" - and an invariant about the sum of 128 prices cannot be checked against one.
NOMINEE_EVENT = dict(id="0xEV128", slug="nominee-2028", title="Republican Presidential Nominee 2028",
                     category="Politics", neg_risk=True)
NOMINEE_COUNT = 128
# Prices are ON the 0.001 tick grid and sum to 1.002 - a deviation inside the band the tick sizes imply
# (128 outcomes x 1 tick), which is the honest default state of a real event: the sum is never exactly 1, and
# a screen that renders "100%" while the true sum is 99.4% is telling its reader something false.
NOMINEE_PRICES = [340_000, 200_000, 120_000, 70_000] + [2_000] * 100 + [3_000] * 24
NOMINEE_VOL24H = [620_000_000_000, 210_000_000_000, 96_000_000_000, 44_000_000_000] + \
                 [5_000_000_000] * 10 + [400_000_000] * 114                   # last 114: the dead tail
NOMINEE_LIQ = [26_400_000_000_000, 12_200_000_000_000, 7_100_000_000_000, 4_000_000_000_000] + \
              [200_000_000_000] * 10 + [9_400_000_000] * 114

# market_id -> (category, resolution source URL, resolution criteria, liquidity, volume24h, open interest).
# The volumes and the OI are the P01-scale numbers the prompt quotes for this event ($1.0M 24h, $52.8M
# liquidity); the liquidity column below is what the venue reports, NOT the sum of the seeded book, and the
# API keeps those two apart on purpose - a fixture that makes the book sum to the reported liquidity would
# let a wrong join look right.
SURFACE = {
    "0xM1": ("Economics", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
             "Resolves YES if the FOMC statement released at the September meeting announces a reduction in "
             "the target range for the federal funds rate. Resolves NO if the range is unchanged or raised.",
             1_820_000_000_000, 412_500_000_000, 1_284_000_000_000),
    "0xM2": ("Crypto", "https://www.coinbase.com/price/bitcoin",
             # ATTACKER-INFLUENCED TEXT ON PURPOSE. The resolution criteria are written by whoever created
             # the market, so they are the one string on the page an outsider controls. This fixture carries
             # markup, a script tag, an entity and a markdown link so the sanitiser has something to strip:
             # a fixture of plain sentences proves only that plain sentences render.
             "Resolves YES if the 23:59 UTC close on <b>Coinbase</b> &amp; the Binance 1m candle both print "
             "above $150,000. <script>steal()</script> See [the source](https://example.org/btc) for the "
             "candle used. <img src=x onerror=alert(1)>",
             640_000_000_000, 96_400_000_000, 388_000_000_000),
    "0xM3": ("Culture", "https://example.org/listing-notice",
             "Resolves YES if the token is listed on any venue in the named exchange group before the close.",
             900_000_000, 420_000_000, 12_000_000),                  # $420/day: the dead tail, by default
    "0xM4": ("Politics", "https://example.org/governance-record",
             "Resolves YES if the motion passes on the recorded vote; abstentions do not count as votes cast.",
             44_000_000_000, 8_200_000_000, 21_000_000_000),
    "0xM5": ("Politics", "https://example.org/mayoral-return",
             "Resolves YES for the candidate certified by the returning officer. negRisk: exactly one "
             "outcome can resolve YES, and the others resolve NO together.",
             3_100_000_000_000, 1_040_000_000_000, 2_200_000_000_000),
    "0xM6": ("Politics", "https://example.org/closed-market",
             "This market is closed and its outcome is recorded; the page keeps rendering it because links to "
             "resolved markets must not 404.",
             0, 0, 0),
    "0xM9": ("Politics", "https://example.org/recount",
             "Resolves NO. The book is one-sided because every remaining holder of YES is asking to leave; a "
             "screen that renders this as broken is a screen that will be blamed for the market's outcome.",
             21_900_000_000_000, 1_300_000_000_000, 21_900_000_000_000),
}


def nominee_markets(now: int) -> list[dict]:
    """128 markets under one event, in the venue's shape.

    Names are synthetic on purpose (`Candidate 007`) and stay synthetic: a plausible list of real names for a
    real race would be a fixture that makes a claim about the world, and the next person to read these numbers
    would have no way to tell which parts were measured.
    """
    out = []
    for i in range(NOMINEE_COUNT):
        out.append(dict(id="0xN%03d" % i, question="Will Candidate %03d win the 2028 nomination?" % i,
                        slug="nominee-2028-%03d" % i, tick="0.001", min_size="5", fee="None",
                        neg_risk=True, accepting=True, delay=0, book=True, outcomes=["Yes", "No"],
                        condition="0xNN%03d" % i, end_days=430, mid=NOMINEE_PRICES[i],
                        event=NOMINEE_EVENT["id"], vol24h=NOMINEE_VOL24H[i], liquidity=NOMINEE_LIQ[i],
                        token_ids=["9%011d%s" % (410_000_000_000 + i, s) for s in ("1", "2")]))
    return out


def tick_micro(m: dict) -> int:
    return 1000 if m["tick"] == "0.001" else 10000


def mid_micro(m: dict) -> int:
    """The price this market is quoted around. An outcome priced at 0.002 has a book two ticks wide and no
    wider: a bid cannot sit at or below zero, so the seed narrows the ladder near the price bounds - which is
    the same thing the venue does (P01 measured the tick_size_change event at 0.96/0.04, where the ladder
    re-forms rather than continuing past the edge). The observed median spread of ~71 ticks at 0.001 is kept
    wherever it fits, because that is the number P01 actually measured."""
    return m.get("mid", 500_000)


def half_spread_ticks(m: dict) -> int:
    """Half the spread in ticks, always >= 1 so both sides exist and both prices land ON the tick grid."""
    mid, tick = mid_micro(m), tick_micro(m)
    room = int(mid // (2 * tick))                       # levels that fit before a bid would pass through 0
    return max(1, min(71 // 2, room))


def walk_shares(i: int, thin: bool) -> tuple[int, int]:
    """Sizes taper with distance from the top of book, as a real ladder does."""
    bid = (40_000_000 // (i + 1)) if not thin else (1_200_000 if i == 0 else 0)
    ask = (36_000_000 // (i + 1)) if not thin else (900_000 if i == 0 else 0)
    return bid, ask


def one_sided_asks(m: dict, levels: int = 94, notional_usdc: float = 21_900_000.0) -> list[tuple]:
    """94 ask levels and no bids, totalling the $21.9M P01 measured.

    Every level carries the SAME notional (233k) rather than the same size: that is what makes the ladder
    climb smoothly as size shrinks with price, and it is why the sum below is a division and not a loop
    variable. `shares_micro_i = S / i` with `price_micro_i = 1000 * i` gives `notional_i = S / 1e9` USDC for
    every i, so S follows from the target directly.
    """
    tick = tick_micro(m)
    s_micro = int(round(notional_usdc * 10 ** 9 / levels))     # micro-shares, the same at every level
    rows = []
    for i in range(1, levels + 1):
        if i * tick > 1_000_000:
            break
        rows.append((m["id"], "ask", i * tick, s_micro // i, 1))
    return rows


def books_for(m: dict) -> list[tuple]:
    """A ladder at the observed median spread where it fits, narrowed near the price bounds otherwise.

    A market with `enable_order_book = false` gets NO levels. The seed used to build a ladder for it anyway,
    which made the "this market has no book" state unreachable in dev - the API's 404 could not be produced,
    so the client's empty-book screen was never renderable and the risk gate's NO_ORDER_BOOK branch was only
    ever exercised by a unit test with a hand-built fixture.
    """
    if not m.get("book"):
        return []
    if m.get("one_sided"):
        return one_sided_asks(m)
    tick, half = tick_micro(m), half_spread_ticks(m)
    mid = mid_micro(m)
    n = 3 if m.get("thin") else 12
    rows = []
    for i in range(n):
        bp, ap = mid - (i + 1) * tick, mid + (i + 1) * tick
        if bp <= 0 or ap >= 1_000_000:
            break
        size_bid, size_ask = walk_shares(i, bool(m.get("thin")))
        rows.append((m["id"], "bid", bp, size_bid, 1 + (i % 3)))
        rows.append((m["id"], "ask", ap, size_ask, 1 + (i % 4)))
        if i + 1 >= half:
            break
    return rows


def trades_for(m: dict) -> list[tuple]:
    """Recent fills for the tape card, at the sizes P01 actually measured (median fill $5-6 against a book
    whose mid sits near 50 cents, so ~10 shares).

    `exchange_ts` (the venue clock, what a cursor pages on) and `ingest_ms` (our clock, what freshness reads)
    are DIFFERENT numbers on purpose, a few hundred ms apart: a seed where they are equal is a seed where a
    developer can accidentally code freshness off the venue timestamp and have it look right until a
    backfill arrives with 19-second-old trades.
    """
    if not m["book"] or m.get("one_sided"):
        return []                     # a one-sided market has no trades inside the window we hold: the asks
    tick = tick_micro(m)              # are resting orders nobody has crossed, which is the whole point
    mid = mid_micro(m)
    now = _now_ms()
    rows = []
    for i in range(5):
        price = mid + ((i % 3) - 1) * tick                 # walks one tick either side of mid, on-tick
        size = 1_000_000 * (10 - i)                        # 10.000000 down to 6.000000 shares
        ts = now - i * 700                                 # newest first, 0.7s apart like the live tape
        rows.append((m["id"], "0xT%s%d" % (m["id"][-1], 0), "BUY" if i % 2 == 0 else "SELL",
                     price, size, 1 if i == 1 else 0, ts, ts + 340,
                     json.dumps({"source": "seed", "i": i})))
    return rows


# Wallets the seed trades between, and the labels that hang off them. Exactly one is NOT publishable: the
# venue-side rule is that `insider_suspect` is never publishable, and a seed where every label is publishable
# cannot show that the filter works. A dev database whose holders list would happily name a suspect wallet is
# a dev database where the leak ships.
SEED_WALLETS = [
    ("0x" + "aa" * 20, "whale", True, 820),
    ("0x" + "bb" * 20, "smart_money", True, 700),
    ("0x" + "cc" * 20, "insider_suspect", False, 640),
    ("0x" + "dd" * 20, None, True, 0),
]


def fills_for(m: dict, trades: list[tuple]) -> list[tuple]:
    """Mirror the volatile tape into `tape_fills`, the DURABLE log P05's ingest writes.

    Both tables exist and they are not the same thing: `tape_trades` is the P04 fixture the gate reads,
    `tape_fills` is what the ingestion writes and what the rollups, the holders list and the price history
    read. A seed that fills only the first leaves three P09 surfaces permanently empty in dev, and an empty
    chart looks exactly like a market that has never traded.
    """
    out = []
    for i, (_mid, token_id, side, price, size, _maker, ts, ingest_ms, _raw) in enumerate(trades):
        wallet, _label, _pub, _conf = SEED_WALLETS[i % len(SEED_WALLETS)]
        out.append((m["condition"], token_id, m["outcomes"][0], 0, wallet, side, price, size,
                    price * size // 10 ** 6, ts, ingest_ms, "ws", 0))
    return out


def rows() -> dict:
    now = _now_ms()
    markets, tokens, book, events, trades = [], [], [], [], []
    meta, activity, stats = [], [], []
    # events first: markets.event_id is a FOREIGN KEY, and a seed that references a row it never created
    # fails on the first statement that touches the constraint. Only 0xM5 belongs to an event; the others
    # use NULL, which is a legal FK value and the honest one (a single-market event is not an event).
    events.append(("0xEV1", "mayor-2027", "The 2027 mayoral race", 1, now, now))
    events.append((NOMINEE_EVENT["id"], NOMINEE_EVENT["slug"], NOMINEE_EVENT["title"],
                   int(NOMINEE_EVENT["neg_risk"]), now, now))
    fills: list[tuple] = []
    all_markets = list(MARKETS) + nominee_markets(now)
    for m in all_markets:
        event = m.get("event") or ("0xEV1" if m["neg_risk"] else None)
        markets.append((m["id"], m["condition"], event, m["question"], m["slug"], m["accepting"],
                        m["delay"], m["book"], m["tick"], m["min_size"], m["fee"], m["neg_risk"],
                        now + m["end_days"] * 86_400_000, json.dumps(m["outcomes"]), now, now))
        for i, o in enumerate(m["outcomes"]):
            # The hand-written markets keep the ids earlier phases referenced. `id[-1] + i` collides across 128
            # generated markets (every id ends in '0'), so a generated market brings its own token ids - the
            # collision would have been a duplicate-key failure, which is the good outcome; the bad one is a
            # token that silently belongs to two outcomes.
            tid = m.get("token_ids", [None] * len(m["outcomes"]))[i]
            tokens.append((tid or "0xT%s%d" % (m["id"][-1], i), m["id"], o, i,
                           (1 if m["id"] == "0xM6" and o == "Yes" else None)))
        book += books_for(m)
        mt = trades_for(m)
        trades += mt
        fills += fills_for(m, mt)
        if "vol24h" in m:                # generated markets carry their own surfaces
            meta.append((m["id"], "Politics", "https://example.org/nominee-2028",
                         "Resolves YES for the candidate certified as the nominee by the party's convention. "
                         "negRisk: exactly one outcome resolves YES.", None, now))
            activity.append((m["id"], m["liquidity"], m["liquidity"], m["liquidity"], m.get("mid"),
                             m.get("mid"), now))
            stats.append((m["condition"], m["vol24h"], m["liquidity"], 0, 0, now, "{}", now))
        elif m["id"] in SURFACE:
            cat, src, crit, liq, vol, oi = SURFACE[m["id"]]
            meta.append((m["id"], cat, src, crit, None, now))
            activity.append((m["id"], oi, vol * 7, vol * 30, m.get("mid") or 500_000,
                             m.get("mid") or 500_000, now))
            stats.append((m["condition"], vol, liq, 0, 0, now, "{}", now))
    labels = [(w, lab, conf, "{}", pub, now, now) for w, lab, pub, conf in SEED_WALLETS if lab]
    # P10. `conditions` is threaded from the markets this function just built, not re-typed in the fixture: a
    # hand-written condition id joins to no market, and a fill that joins to nothing renders as a blank row
    # rather than as an error.
    term = terminal_rows(now, fills, {m["id"]: m["condition"] for m in all_markets})
    markets += term["markets"]
    tokens += term["tokens"]
    meta += term["meta"]
    fills += term["fills"]
    return {"markets": markets, "tokens": tokens, "book": book, "events": events, "trades": trades,
            "fills": fills, "labels": labels, "meta": meta, "activity": activity, "stats": stats,
            "terminal": term}


# The dev seed must reset the volatile reads every time it runs (see the note further down), but one of those
# tables is APPEND-ONLY in the product: `tape_trades` has a BEFORE DELETE trigger that aborts, and rightly so -
# a fill log you can rewrite is a fill log you cannot trust after an incident. So `make seed` twice against the
# same database used to die with "append-only table: tape_trades is not deletable", which is the constraint
# working exactly as designed and the seed being wrong about whose job it is.
#
# The fix uses the only permission SQLite offers - DROP TRIGGER, then recreate it - and both statements run
# inside the seed's own transaction, so a failure anywhere rolls the drop back with everything else. The
# recreate is asserted below, because "I put it back" is a claim and `sqlite_master` is evidence.
APPEND_ONLY_RESET = ("tape_trades",)

#: Everything that points at a market row, deleted BEFORE the market itself. `ON DELETE CASCADE` is in the
#: Postgres DDL and is not in the generated SQLite twin (the transpiler drops it), so a per-engine cascade is
#: not something the writer can rely on: it deletes children explicitly and works the same on both.
_TERMINAL_MARKET_CHILDREN = ("tokens", "market_meta", "market_activity", "book_levels", "alert_rules",
                             "whale_views")

#: Tables the P10 fixture has to CLEAR before it can write its own rows, and which are append-only in the
#: product — so the seed lifts their triggers for the length of its transaction and puts them back (asserted
#: below). `copy_events` is here because it is P06's evidence table and the copy history is per-config data: a
#: re-seed that appended a second copy of it would double every number on the monitor screen.
_TERMINAL_RESET = ("wallet_pseudonyms", "copy_dry_runs", "copy_events")


def _append_only_trigger_sql(table: str) -> tuple[str, str]:
    """The exact statements tools/build-sqlite-migrations.py emits for an append-only table. Duplicated here
    on purpose: importing the transpiler to ask it for text would make the seed depend on a dev tool, and a
    subtly different trigger would be an invariant that exists only after a seed."""
    return ("CREATE TRIGGER append_only_%s_update BEFORE UPDATE ON %s BEGIN "
            "SELECT RAISE(ABORT,'append-only table: %s is not updatable'); END;" % (table, table, table),
            "CREATE TRIGGER append_only_%s_delete BEFORE DELETE ON %s BEGIN "
            "SELECT RAISE(ABORT,'append-only table: %s is not deletable'); END;" % (table, table, table))


def seed_sqlite(db_path: str | None = None) -> dict:
    path = db_path or os.environ.get("PGM_DB_PATH", str(ROOT / "var" / "polygm.db"))
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=ON")          # the dev engine must enforce FKs or it disagrees with
                                                   # Postgres about which rows are legal at all
    r = rows()
    now = _now_ms()
    con.execute("BEGIN")
    try:
        con.execute("INSERT OR IGNORE INTO events (id,slug,title,neg_risk,created_ms,updated_ms)"
                    " VALUES (?,?,?,?,?,?)", r["events"][0])
        for ev in r["events"]:            # the nominee event is a second row: the FK lives on markets
            con.execute("INSERT OR IGNORE INTO events (id,slug,title,neg_risk,created_ms,updated_ms)"
                        " VALUES (?,?,?,?,?,?)", ev)
        con.execute("INSERT OR IGNORE INTO users (id,stonks_address,created_ms,tier)"
                    " VALUES ('u-demo','0x' || 'ab' || 'cd', ?, 'trader')", (now,))
        for row in r["markets"]:
            con.execute("INSERT OR REPLACE INTO markets (id,condition_id,event_id,question,slug,"
                        "accepting_orders,seconds_delay,enable_order_book,minimum_tick_size,"
                        "minimum_order_size,fee_type,neg_risk,end_ts,outcomes_json,first_seen_ms,"
                        "updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        for row in r["tokens"]:
            con.execute("INSERT OR REPLACE INTO tokens (token_id,market_id,outcome,outcome_index,is_winner)"
                        " VALUES (?,?,?,?,?)", row)
        con.execute("DELETE FROM book_levels")
        con.executemany("INSERT INTO book_levels (market_id,side,price_micro,size_shares_micro,level_count,"
                        "updated_ms) VALUES (?,?,?,?,?,?)", [b + (now,) for b in r["book"]])
        # DELETE first: the seed is idempotent by RESETTING the volatile reads, not by skipping them. A
        # "INSERT OR IGNORE" tape would keep the previous run's rows and make the tape card show a market
        # that has not ticked in an hour, which is the one thing a trader must never see as fresh.
        # The P09 surfaces are RESET like the tape, not skipped: a re-seed that left yesterday's liquidity
        # in place would let the discovery screen's filters pass against numbers no one is maintaining.
        for tbl, cols in (("market_meta", "market_id,category,resolution_source,resolution_criteria,"
                                           "image_url,updated_ms"),
                          ("market_activity", "market_id,open_interest_micro,volume_7d_micro,"
                                              "volume_30d_micro,price_24h_ago_micro,last_price_micro,"
                                              "updated_ms"),
                          ("market_stats", "condition_id,volume_24h_micro,liquidity_micro,fill_count,"
                                           "last_fill_ms,last_book_change_ms,meta_json,updated_ms")):
            con.execute("DELETE FROM " + tbl)
            key = r["meta" if tbl == "market_meta" else "activity" if tbl == "market_activity" else "stats"]
            con.executemany("INSERT INTO %s (%s) VALUES (%s)"
                            % (tbl, cols, ",".join("?" * len(key[0]))), key)
        for table in APPEND_ONLY_RESET:
            con.execute("DROP TRIGGER IF EXISTS append_only_%s_update" % table)
            con.execute("DROP TRIGGER IF EXISTS append_only_%s_delete" % table)
        con.execute("DELETE FROM tape_trades")
        for table in APPEND_ONLY_RESET:
            for stmt in _append_only_trigger_sql(table):
                con.execute(stmt)
        con.execute("DELETE FROM tape_fills")
        cols_f = ("condition_id,token_id,outcome,outcome_index,wallet,side,price_micro,size_micro,"
                  "usd_notional_micro,ts_ms,ingest_ms,source,fee_rate_bps")
        con.executemany("INSERT INTO tape_fills (%s,dedupe_key) VALUES (%s,?)"
                        % (cols_f, ",".join("?" * 13)),
                        [row + ("seed-%s-%d" % (row[0], i),) for i, row in enumerate(r["fills"])])
        # ---- P10's fixture. Order follows the foreign keys: the copy config before its guards and its
        # dry runs, the alert rule before the view that points at it.
        term = r["terminal"]
        for table in _TERMINAL_RESET:
            for stmt in ("DROP TRIGGER IF EXISTS append_only_%s_update" % table,
                         "DROP TRIGGER IF EXISTS append_only_%s_delete" % table):
                con.execute(stmt)
        for mid in [m[0] for m in term["markets"]]:
            for child in _TERMINAL_MARKET_CHILDREN:
                con.execute("DELETE FROM %s WHERE market_id=?" % child, (mid,))
            con.execute("DELETE FROM markets WHERE id=?", (mid,))
        con.execute("DELETE FROM wallet_pseudonyms")
        con.execute("DELETE FROM copy_dry_runs")
        con.execute("DELETE FROM copy_events WHERE copier_id IN (SELECT id FROM copy_configs"
                    " WHERE source_user=? OR id='cfg-seed01')", (term["copy"]["config"][2],))
        for table in _TERMINAL_RESET:
            for stmt in _append_only_trigger_sql(table):
                con.execute(stmt)
        for row in term["markets"]:
            con.execute("INSERT INTO markets (id,condition_id,event_id,question,slug,accepting_orders,"
                        "seconds_delay,enable_order_book,minimum_tick_size,minimum_order_size,fee_type,"
                        "neg_risk,end_ts,outcomes_json,first_seen_ms,updated_ms)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        for row in term["tokens"]:
            con.execute("INSERT OR REPLACE INTO tokens (token_id,market_id,outcome,outcome_index,is_winner)"
                        " VALUES (?,?,?,?,?)", row)
        con.executemany("INSERT OR REPLACE INTO market_meta (market_id,category,resolution_source,"
                        "resolution_criteria,image_url,updated_ms) VALUES (?,?,?,?,?,?)", term["meta"])
        con.execute("INSERT OR REPLACE INTO copy_configs (id,user_id,source_user,mode,ratio_bps,"
                    "max_order_micro,max_daily_micro,blocked_markets,enabled,created_ms)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)", term["copy"]["config"])
        con.execute("INSERT OR REPLACE INTO copy_config_guards (config_id,dry_run,skip_if_moved_cents,"
                    "do_not_enter_within_hours,category_filter,min_price_micro,max_price_micro,"
                    "take_profit_micro,stop_loss_micro,live_since_ms,updated_ms)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)", term["copy"]["guard"])
        con.execute("DELETE FROM copy_source_stats WHERE source_user_id=?", (term["copy"]["config"][2],))
        con.executemany("INSERT OR REPLACE INTO copy_source_stats (source_user_id,window_days,closed_trades,"
                        "win_rate_bp,realized_pnl_micro,fees_micro,net_after_fees_micro,max_drawdown_micro,"
                        "longest_losing_streak,avg_latency_ms,updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        term["copy"]["source_stats"])
        con.execute("DELETE FROM alert_rules WHERE id LIKE 'rule-whale-%'")
        con.executemany("INSERT OR REPLACE INTO alert_rules (id,user_id,market_id,event_id,kind,"
                        "fires_per_window,window_ms,params_json,enabled,created_ms)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)", term["rules"])
        con.execute("DELETE FROM whale_views WHERE id LIKE 'wv-seed-%'")
        con.executemany("INSERT OR REPLACE INTO whale_views (id,user_id,name,filters_json,channel,severity,"
                        "scope,market_id,rule_id,created_ms) VALUES (?,?,?,?,?,?,?,?,?,?)", term["views"])
        con.executemany("INSERT OR REPLACE INTO wallet_pseudonyms (wallet_id,anon_id,first_seen_ms)"
                        " VALUES (?,?,?)", term["pseudonyms"])
        con.executemany("INSERT INTO copy_dry_runs (config_id,copier_id,source_user_id,source_intent_id,"
                        "market_id,would_action,would_size_micro,would_price_micro,source_price_micro,"
                        "deviation_bps,reason,at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", term["copy"]["dry_runs"])
        con.executemany("INSERT INTO copy_events (copier_id,source_user_id,source_intent_id,intent_id,"
                        "action,reason,deviation_bps,at_ms) VALUES (?,?,?,?,?,?,?,?)", term["copy"]["events"])
        con.execute("DELETE FROM wallet_labels")
        con.executemany("INSERT INTO wallet_labels (wallet,label,confidence,evidence_json,publishable,"
                        "first_seen_ms,last_seen_ms) VALUES (?,?,?,?,?,?,?)", r["labels"])
        con.executemany("INSERT INTO tape_trades (market_id,token_id,side,price_micro,size_shares_micro,"
                        "taker_is_maker,exchange_ts,ingest_ms,raw_json) VALUES (?,?,?,?,?,?,?,?,?)",
                        r["trades"])
        con.execute("INSERT OR IGNORE INTO entitlements (user_id,plan,max_alerts,max_watchlists,"
                    "max_automation_rules,radar_poll_ms,api_rpm,updated_ms) "
                    "VALUES ('u-demo','trader',12,4,3,5000,120,?)", (now,))
        for name, kind, val in (("tape_ws", "bool", {"on": True}), ("copy_trading", "bool", {"on": False}),
                                ("auto_redeem", "bool", {"on": False}),
                                ("max_order_notional_micro", "number", {"value": 2_500_000_000}),
                                ("stale_ms_book", "number", {"value": 3_000})):
            con.execute("INSERT OR REPLACE INTO feature_flags (name,kind,value_json) VALUES (?,?,?)",
                        (name, kind, json.dumps(val)))
        con.commit()
    except Exception:
        con.rollback()                             # a half-seeded dev DB is worse than an error: every
        raise                                      # subsequent symptom is unrelated to the real cause
    # Evidence, not a promise: the append-only triggers this seed had to lift must be back, or the next phase
    # develops against a database where history is editable.
    back = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger' "
                                      "AND name LIKE 'append_only_%_delete'")}
    missing = [t for t in APPEND_ONLY_RESET if ("append_only_%s_delete" % t) not in back]
    if missing:
        raise RuntimeError("seed lifted an append-only trigger and did not restore it: %s" % ",".join(missing))
    counts = {t: con.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
              for t in ("markets", "tokens", "book_levels", "tape_trades", "feature_flags", "entitlements",
                        "events", "market_meta", "market_activity", "market_stats", "tape_fills",
                        "wallet_labels", "wallet_pseudonyms", "copy_configs", "copy_config_guards",
                        "copy_events", "copy_dry_runs", "copy_source_stats", "whale_views", "alert_rules")}
    con.close()
    return counts


NOW_TOKEN = "{{NOW_MS}}"


#: ONE clock for a whole emission. `_pg_time` used to call `_now_ms()` per value, so the rows (built from one
#: `now`) were subtracted from a base that drifted by a millisecond or two while the file was being written —
#: `{{NOW_MS}} + -6000` and `{{NOW_MS}} + -5999` for the same row on two consecutive runs, i.e. a generated
#: artefact that was never byte-stable and produced a two-thousand-line diff every time somebody regenerated
#: it. Captured once here; `emit_sql` sets it, and anything that calls `_pg_time` outside an emission falls back
#: to the wall clock rather than crashing.
_SEED_BASE_MS: int | None = None


def _pg_time(v) -> str:
    """Absolute timestamps are a BUG in a generated seed file: db/seed.sql is applied whenever someone
    migrates a dev database, and a `updated_ms` frozen at generation time means a fresh dev DB starts with a
    book that is hours old — which the risk gate correctly refuses to trade against (STALE_QUOTE), so
    `make dev` looks broken for a reason that has nothing to do with the code.

    So every millisecond column is emitted as a token that each ENGINE expands to its own clock:
    `{{NOW_MS}}` plus the row's offset (0xM6's end_ts is deliberately in the past, hence offsets).
    """
    if v is None:
        return "NULL"
    base = _SEED_BASE_MS if _SEED_BASE_MS is not None else _now_ms()
    return NOW_TOKEN if int(v) == base else "%s + %d" % (NOW_TOKEN, int(v) - base)


def emit_sql() -> str:
    """Postgres seed. Same rows, ON CONFLICT DO NOTHING so re-running a deploy is safe.

    Order is dependency order, not preference: events, users, then markets (whose event_id is a FK), then
    tokens/book/tape. An earlier draft of this function inserted `users` twice and never inserted the event
    at all while a comment claimed it had - which is exactly why this function generates db/seed.sql instead
    of it being hand-maintained, and why tests/test_migrations.py executes the result.
    """
    global _SEED_BASE_MS, _PINNED_NOW_MS
    _PINNED_NOW_MS = int(time.time() * 1000)     # pinned for the whole emission: see `_now_ms`
    _SEED_BASE_MS = _PINNED_NOW_MS
    r = rows()
    now = _SEED_BASE_MS
    out = ["-- GENERATED by services/api/seed.py -- do not edit. P01-measured values; see that file.",
           "-- {{NOW_MS}} is expanded by tools/run-sql.py to the ENGINE's own clock (Postgres now()), so this",
           "-- file stays fresh no matter when it is applied. Never replace it with a literal timestamp.", ""]
    out.append("-- events before markets: markets.event_id REFERENCES events(id)")
    for ev in r["events"]:
        out.append("INSERT INTO events (id,slug,title,neg_risk,created_ms,updated_ms) VALUES (%s) "
                   "ON CONFLICT DO NOTHING;" % ", ".join(_pg_time(v) if isinstance(v, int) and v > 10 ** 11
                                                         else _pg(v) for v in ev))
    out.append("INSERT INTO users (id,created_ms,tier) VALUES ('u-demo', %s, 'trader') "
               "ON CONFLICT DO NOTHING;" % NOW_TOKEN)
    cols = ("id,condition_id,event_id,question,slug,accepting_orders,seconds_delay,enable_order_book,"
            "minimum_tick_size,minimum_order_size,fee_type,neg_risk,end_ts,outcomes_json,"
            "first_seen_ms,updated_ms")
    for row in r["markets"]:
        # columns 12..15 are end_ts, outcomes_json, first_seen_ms, updated_ms — the time-ish ones
        vals = [_pg_time(v) if isinstance(v, int) and v > 10 ** 11 else _pg(v) for v in row]
        out.append("INSERT INTO markets (%s) VALUES (%s) ON CONFLICT (id) DO NOTHING;"
                   % (cols, ", ".join(vals)))
    for row in r["tokens"]:
        out.append("INSERT INTO tokens (token_id,market_id,outcome,outcome_index,is_winner) "
                   "VALUES (%s) ON CONFLICT (token_id) DO NOTHING;" % ", ".join(_pg(v) for v in row))
    for b in r["book"]:
        out.append("INSERT INTO book_levels (market_id,side,price_micro,size_shares_micro,level_count,"
                   "updated_ms) VALUES (%s) ON CONFLICT DO NOTHING;"
                   % ", ".join([_pg(v) for v in b] + [NOW_TOKEN]))
    for t in r["trades"]:
        # exchange_ts / ingest_ms are the two timestamps a tape reader sorts and ages by; both must be
        # "recent" when the file is APPLIED, not when it was generated, or the tape renders as a hole
        vals = [_pg_time(v) if isinstance(v, int) and v > 10 ** 11 else _pg(v) for v in t]
        out.append("INSERT INTO tape_trades (market_id,token_id,side,price_micro,size_shares_micro,"
                   "taker_is_maker,exchange_ts,ingest_ms,raw_json) VALUES (%s);"
                   % ", ".join(vals))
    # P09 surfaces. `market_activity.price_24h_ago_micro` is the base of the 24h-move sort and is NULLABLE:
    # NULL (our tape does not reach back 24h) is not 0, and emitting 0 would render every market as +100%.
    act_cols = ("market_id,open_interest_micro,volume_7d_micro,volume_30d_micro,price_24h_ago_micro,"
                "last_price_micro,updated_ms")
    for row in r["meta"]:
        out.append("INSERT INTO market_meta (market_id,category,resolution_source,resolution_criteria,"
                   "image_url,updated_ms) VALUES (%s) ON CONFLICT (market_id) DO UPDATE SET category = "
                   "EXCLUDED.category, resolution_source = EXCLUDED.resolution_source, "
                   "resolution_criteria = EXCLUDED.resolution_criteria;"
                   % ", ".join([_pg(v) for v in row[:-1]] + [NOW_TOKEN]))
    for row in r["activity"]:
        out.append("INSERT INTO market_activity (%s) VALUES (%s) ON CONFLICT (market_id) DO UPDATE SET "
                   "open_interest_micro = EXCLUDED.open_interest_micro, volume_7d_micro = "
                   "EXCLUDED.volume_7d_micro, volume_30d_micro = EXCLUDED.volume_30d_micro, "
                   "price_24h_ago_micro = EXCLUDED.price_24h_ago_micro, last_price_micro = "
                   "EXCLUDED.last_price_micro;"
                   % (act_cols, ", ".join([_pg(v) for v in row[:-1]] + [NOW_TOKEN])))
    for row in r["stats"]:
        # Column by column rather than by a helper that guesses: indices 0-3 are money and counts, 4 and 5 are
        # CLOCKS (`last_fill_ms`, `last_book_change_ms`), 6 is json, 7 is the row's own stamp. The first version
        # of this line routed "any integer above 10^11" through `_pg_time` — which is a money value in micro-USDC
        # ($412.50 is 412500000000) as often as it is a millisecond, and it silently rewrote the volumes as
        # clock offsets. A shape test is not a type; the position is.
        # `last_fill_ms = 0` is not "the epoch": it is the fixture saying "this market has not traded". Turning
        # it into `{{NOW_MS}} + -1790125279241` would be a clock that is always true and a number nobody can
        # read, so the sentinel survives and only a real stamp becomes a token.
        last_fill = _pg(row[4]) if not row[4] else _pg_time(row[4])
        vals = [_pg(row[0]), _pg(row[1]), _pg(row[2]), _pg(row[3]),
                last_fill, _pg_time(row[5]), _pg(row[6]), NOW_TOKEN]
        out.append("INSERT INTO market_stats (condition_id,volume_24h_micro,liquidity_micro,fill_count,"
                   "last_fill_ms,last_book_change_ms,meta_json,updated_ms) VALUES (%s) "
                   "ON CONFLICT (condition_id) DO UPDATE SET volume_24h_micro = EXCLUDED.volume_24h_micro, "
                   "liquidity_micro = EXCLUDED.liquidity_micro;"
                   % ", ".join(vals))
    for f in r["fills"]:
        out.append("INSERT INTO tape_fills (condition_id,token_id,outcome,outcome_index,wallet,side,"
                   "price_micro,size_micro,usd_notional_micro,ts_ms,ingest_ms,source,fee_rate_bps,"
                   "dedupe_key) VALUES (%s) ON CONFLICT (dedupe_key) DO NOTHING;"
                   % ", ".join([_pg_time(v) if isinstance(v, int) and v > 10 ** 11 else _pg(v)
                                for v in f]
                               + [_pg("seed-%s-%d" % (f[0], r["fills"].index(f)))]))
    for lab in r["labels"]:
        out.append("INSERT INTO wallet_labels (wallet,label,confidence,evidence_json,publishable,"
                   "first_seen_ms,last_seen_ms) VALUES (%s) ON CONFLICT DO NOTHING;"
                   % ", ".join([_pg(v) for v in lab[:-2]] + [NOW_TOKEN, NOW_TOKEN]))
    # ---- P10's fixture, in the same dependency order as the SQLite writer.
    term = r["terminal"]
    for row in term["markets"]:
        vals = [_pg_time(v) if isinstance(v, int) and v > 10 ** 11 else _pg(v) for v in row]
        # `DO NOTHING`, not an upsert: a re-applied seed must not rewrite a market's end_ts under a user who is
        # watching it. The terminal fixture's own rows are deleted and re-inserted by the SQLite writer; here
        # the file is just applied twice, and idempotent-for-reads is the right shape for a dev database.
        out.append("INSERT INTO markets (%s) VALUES (%s) ON CONFLICT (id) DO NOTHING;"
                   % (cols, ", ".join(vals)))
    for row in term["tokens"]:
        # Conflict on the COMPOSITE key, not on token_id: a re-seed whose token ids changed would otherwise
        # collide with the old row on (market_id, outcome_index) and fail with an error that names the wrong
        # constraint. The composite key is the one that describes "one slot per outcome".
        out.append("INSERT INTO tokens (token_id,market_id,outcome,outcome_index,is_winner) VALUES (%s) "
                   "ON CONFLICT (market_id, outcome_index) DO UPDATE SET token_id = EXCLUDED.token_id, "
                   "is_winner = EXCLUDED.is_winner;" % ", ".join(_pg(v) for v in row))
    for row in term["meta"]:
        out.append("INSERT INTO market_meta (market_id,category,resolution_source,resolution_criteria,"
                   "image_url,updated_ms) VALUES (%s) ON CONFLICT (market_id) DO UPDATE SET category = "
                   "EXCLUDED.category;" % ", ".join([_pg(v) for v in row[:-1]] + [NOW_TOKEN]))
    out.append("INSERT INTO copy_configs (id,user_id,source_user,mode,ratio_bps,max_order_micro,"
               "max_daily_micro,blocked_markets,enabled,created_ms) VALUES (%s) ON CONFLICT (id) DO NOTHING;"
               % ", ".join([_pg(v) for v in term["copy"]["config"][:-1]] + [NOW_TOKEN]))
    out.append("INSERT INTO copy_config_guards (config_id,dry_run,skip_if_moved_cents,"
               "do_not_enter_within_hours,category_filter,min_price_micro,max_price_micro,take_profit_micro,"
               "stop_loss_micro,live_since_ms,updated_ms) VALUES (%s) ON CONFLICT (config_id) DO NOTHING;"
               % ", ".join([_pg(v) for v in term["copy"]["guard"][:-1]] + [NOW_TOKEN]))
    for row in term["copy"]["source_stats"]:
        out.append("INSERT INTO copy_source_stats (source_user_id,window_days,closed_trades,win_rate_bp,"
                   "realized_pnl_micro,fees_micro,net_after_fees_micro,max_drawdown_micro,"
                   "longest_losing_streak,avg_latency_ms,updated_ms) VALUES (%s) "
                   "ON CONFLICT (source_user_id,window_days) DO UPDATE SET closed_trades = "
                   "EXCLUDED.closed_trades;"
                   % ", ".join([_pg(v) for v in row[:-1]] + [NOW_TOKEN]))
    for row in term["rules"]:
        out.append("INSERT INTO alert_rules (id,user_id,market_id,event_id,kind,fires_per_window,window_ms,"
                   "params_json,enabled,created_ms) VALUES (%s) ON CONFLICT (id) DO NOTHING;"
                   % ", ".join([_pg(v) for v in row[:-1]] + [NOW_TOKEN]))
    for row in term["views"]:
        out.append("INSERT INTO whale_views (id,user_id,name,filters_json,channel,severity,scope,market_id,"
                   "rule_id,created_ms) VALUES (%s) ON CONFLICT (id) DO NOTHING;"
                   % ", ".join([_pg(v) for v in row[:-1]] + [NOW_TOKEN]))
    for row in term["pseudonyms"]:
        out.append("INSERT INTO wallet_pseudonyms (wallet_id,anon_id,first_seen_ms) VALUES (%s) "
                   "ON CONFLICT (wallet_id) DO NOTHING;" % ", ".join([_pg(v) for v in row[:-1]] + [NOW_TOKEN]))
    for row in term["copy"]["dry_runs"]:
        out.append("INSERT INTO copy_dry_runs (config_id,copier_id,source_user_id,source_intent_id,"
                   "market_id,would_action,would_size_micro,would_price_micro,source_price_micro,"
                   "deviation_bps,reason,at_ms) VALUES (%s) ON CONFLICT DO NOTHING;"
                   % ", ".join([_pg(v) for v in row[:-1]] + [_pg_time(row[-1])]))
    for row in term["copy"]["events"]:
        out.append("INSERT INTO copy_events (copier_id,source_user_id,source_intent_id,intent_id,action,"
                   "reason,deviation_bps,at_ms) VALUES (%s);"
                   % ", ".join([_pg(v) for v in row[:-1]] + [_pg_time(row[-1])]))
    out.append("INSERT INTO entitlements (user_id,plan,max_alerts,max_watchlists,max_automation_rules,"
               "radar_poll_ms,api_rpm,updated_ms) VALUES ('u-demo','trader',12,4,3,5000,120,%s) "
               "ON CONFLICT DO NOTHING;" % NOW_TOKEN)
    for name, kind, val in (("tape_ws", "bool", {"on": True}), ("copy_trading", "bool", {"on": False}),
                            ("auto_redeem", "bool", {"on": False}),
                            ("max_order_notional_micro", "number", {"value": 2_500_000_000}),
                            ("stale_ms_book", "number", {"value": 3_000})):
        out.append("INSERT INTO feature_flags (name,kind,value_json) VALUES (%s) "
                   "ON CONFLICT (name) DO UPDATE SET value_json = EXCLUDED.value_json;"
                   % ", ".join([_pg(name), _pg(kind), _pg(json.dumps(val))]))
    _PINNED_NOW_MS = None               # unpin: the next caller gets the real clock
    return "\n".join(out) + "\n"


def _pg(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-sql", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="is db/seed.sql current? (exit 1 and name the first difference)")
    ap.add_argument("--out", default=str(ROOT / "db" / "seed.sql"))
    a = ap.parse_args()
    if a.emit_sql:
        Path(a.out).write_text(emit_sql())
        print("wrote", a.out)
    elif a.check:
        # The file is a GENERATED ARTEFACT and it rotted for five phases: P10 changed the fixtures, nobody
        # regenerated, and the first thing to notice was the P04 gate's end-to-end step failing on a CHECK
        # constraint that had been in the schema since P10. Neither half of the fix alone would hold —
        # regeneration without a check rots again, and a check without a deterministic generator is red on
        # every run — so the pinned clock above is what makes this comparison meaningful.
        fresh = emit_sql()
        target = Path(a.out)
        on_disk = target.read_text() if target.is_file() else ""
        if on_disk == fresh:
            print("db/seed.sql is current (%d lines, identical to a fresh generation)" % len(fresh.splitlines()))
            raise SystemExit(0)
        import difflib
        diff = [l for l in difflib.unified_diff(on_disk.splitlines(), fresh.splitlines(),
                                                "db/seed.sql", "regenerated", n=0, lineterm="")
                if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
        print("db/seed.sql is STALE: %d line(s) differ from what the fixtures generate." % len(diff))
        for line in diff[:5]:
            print("   %s" % line[:160])
        print("run: make seed-sql")
        raise SystemExit(1)
    else:
        print("seeded:", seed_sqlite())
