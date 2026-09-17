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


def _now_ms() -> int:
    return int(time.time() * 1000)


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
]


def books_for(m: dict) -> list[tuple]:
    """Build a ladder at the observed median spread (~71 ticks of 0.001 => ~$0.071 wide)."""
    tick = 1000 if m["tick"] == "0.001" else 10000
    mid = 500_000
    spread = 71 * (1000 if m["tick"] == "0.001" else 10000)
    n = 3 if m.get("thin") else 12
    rows, base_bid, base_ask = [], mid - spread // 2, mid + spread // 2
    for i in range(n):
        bp, ap = base_bid - i * tick, base_ask + i * tick
        if bp <= 0 or ap >= 1_000_000:
            break
        size_bid = (40_000_000 // (i + 1)) if not m.get("thin") else (1_200_000 if i == 0 else 0)
        size_ask = (36_000_000 // (i + 1)) if not m.get("thin") else (900_000 if i == 0 else 0)
        rows.append((m["id"], "bid", bp, size_bid, 1 + (i % 3)))
        rows.append((m["id"], "ask", ap, size_ask, 1 + (i % 4)))
    return rows


def trades_for(m: dict) -> list[tuple]:
    """Recent fills for the tape card, at the sizes P01 actually measured (median fill $5-6 against a book
    whose mid sits near 50 cents, so ~10 shares).

    `exchange_ts` (the venue clock, what a cursor pages on) and `ingest_ms` (our clock, what freshness reads)
    are DIFFERENT numbers on purpose, a few hundred ms apart: a seed where they are equal is a seed where a
    developer can accidentally code freshness off the venue timestamp and have it look right until a
    backfill arrives with 19-second-old trades.
    """
    if not m["book"]:
        return []
    tick = 1000 if m["tick"] == "0.001" else 10000
    mid = 500_000
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


def rows() -> dict:
    now = _now_ms()
    markets, tokens, book, events, trades = [], [], [], [], []
    # events first: markets.event_id is a FOREIGN KEY, and a seed that references a row it never created
    # fails on the first statement that touches the constraint. Only 0xM5 belongs to an event; the others
    # use NULL, which is a legal FK value and the honest one (a single-market event is not an event).
    events.append(("0xEV1", "mayor-2027", "The 2027 mayoral race", 1, now, now))
    for m in MARKETS:
        event = "0xEV1" if m["neg_risk"] else None
        markets.append((m["id"], m["condition"], event, m["question"], m["slug"], m["accepting"],
                        m["delay"], m["book"], m["tick"], m["min_size"], m["fee"], m["neg_risk"],
                        now + m["end_days"] * 86_400_000, json.dumps(m["outcomes"]), now, now))
        for i, o in enumerate(m["outcomes"]):
            tokens.append(("0xT%s%d" % (m["id"][-1], i), m["id"], o, i,
                           (1 if m["id"] == "0xM6" and o == "Yes" else None)))
        book += books_for(m)
        trades += trades_for(m)
    return {"markets": markets, "tokens": tokens, "book": book, "events": events, "trades": trades}


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
        con.execute("DELETE FROM tape_trades")
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
    counts = {t: con.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
              for t in ("markets", "tokens", "book_levels", "tape_trades", "feature_flags", "entitlements",
                        "events")}
    con.close()
    return counts


NOW_TOKEN = "{{NOW_MS}}"


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
    base = _now_ms()
    return NOW_TOKEN if v == base else "%s + %d" % (NOW_TOKEN, int(v) - base)


def emit_sql() -> str:
    """Postgres seed. Same rows, ON CONFLICT DO NOTHING so re-running a deploy is safe.

    Order is dependency order, not preference: events, users, then markets (whose event_id is a FK), then
    tokens/book/tape. An earlier draft of this function inserted `users` twice and never inserted the event
    at all while a comment claimed it had - which is exactly why this function generates db/seed.sql instead
    of it being hand-maintained, and why tests/test_migrations.py executes the result.
    """
    r = rows()
    now = _now_ms()
    out = ["-- GENERATED by services/api/seed.py -- do not edit. P01-measured values; see that file.",
           "-- {{NOW_MS}} is expanded by tools/run-sql.py to the ENGINE's own clock (Postgres now()), so this",
           "-- file stays fresh no matter when it is applied. Never replace it with a literal timestamp.", ""]
    out.append("-- events before markets: markets.event_id REFERENCES events(id)")
    ev = r["events"][0]
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
    ap.add_argument("--out", default=str(ROOT / "db" / "seed.sql"))
    a = ap.parse_args()
    if a.emit_sql:
        Path(a.out).write_text(emit_sql())
        print("wrote", a.out)
    else:
        print("seeded:", seed_sqlite())
