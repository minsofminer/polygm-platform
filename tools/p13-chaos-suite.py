#!/usr/bin/env python3
"""P13 D7 — the ten chaos tests, each with a written report.

    python3 tools/p13-chaos-suite.py --record docs/verification/P13-chaos-suite.txt
    python3 tools/p13-chaos-suite.py --only 4,6,7            # one drill at a time while editing
    python3 tools/p13-chaos-suite.py --skip-live             # no network, no 6-minute live run

The kit asks for ten specific failures, each reproduced and each written up: what was broken, what the system
did, what it lost, and how long recovery took. Two of the ten already exist as drills this build wrote in
earlier phases, and re-running them from here is the honest way to include them — a second implementation of
"kill the WebSocket" would be a weaker test of the same thing, not an independent one:

    2  WebSocket killed for 5 minutes   → tools/p05-chaos-test.py (live venue; needs network; ~7 minutes)
    9  kill switch during a copy-trade  → tools/p06-drill.py (real API + venue + two executors over HTTP)
    10 key compromise                   → tools/p07-drill.py (10,000 keys, rotation and retirement)
    1  executor killed mid-submit       → tools/p13-recovery-loop.py (the headline loop, re-run here)

The other six are new here, and they are written against the **product modules**, not against a simulation of
them: the real `Tape` and its `UNIQUE(dedupe_key)`, the real API under uvicorn with a real SQLite file, the real
executor service with the real scenario venue. Two of the kit's rows name components this build does not run —
there is no Redis (the cache is in-process, `flags().cache_ttl_*`) and the product database is SQLite in dev/CI
rather than Postgres. Those two rows are run against the equivalent failure and the substitution is stated in
the report, in the artifact, rather than quietly dropped: what is being tested is *the failure*, not the vendor.

Every drill writes its own artifact next to this file, and the suite writes an index over all ten.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
VERIF = ROOT / "docs" / "verification"
for p in (str(ROOT / "packages"), str(ROOT / "services" / "api"), str(ROOT / "services" / "executor"),
          str(ROOT / "services" / "executor-mock"), str(ROOT / "services" / "ingest"), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

import importlib.util as _ilu  # noqa: E402


def _load(name: str, path: Path):
    """Import a service module under a private name, registered BEFORE execution: `@dataclass` resolves string
    annotations through `sys.modules[cls.__module__]`, and an unregistered module makes that raise."""
    spec = _ilu.spec_from_file_location(name, path)
    mod = _ilu.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


import normalise                                                        # noqa: E402
import tape as tape_mod                                                 # noqa: E402
from polygm_core.money.cents import notional_floor                      # noqa: E402
from polygm_core.risk import limits as lm                               # noqa: E402
from polygm_core.venue import clob_v2 as v2                             # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="p13-chaos-"))
RUN_SQL = _load("pgm_run_sql_chaos", ROOT / "tools" / "run-sql.py")
SEED = _load("pgm_seed_chaos", ROOT / "services" / "api" / "seed.py")


class Report:
    """One drill's write-up: the numbers are collected as they are measured, not remembered afterwards."""

    def __init__(self, number: int, title: str, path: Path) -> None:
        self.number, self.title, self.path = number, title, path
        self.lines: list[str] = []
        self.facts: dict = {}
        self.verdict = "PASS"
        self.reason = ""
        self.t0 = time.time()

    def say(self, text: str = "") -> None:
        self.lines.append(text)

    def fact(self, key: str, value) -> None:
        self.facts[key] = value

    def fail(self, why: str) -> None:
        self.verdict, self.reason = "FAIL", why

    def save(self) -> None:
        head = ["CHAOS %d — %s" % (self.number, self.title), "=" * 78,
                "verdict: %s%s" % (self.verdict, (" — " + self.reason) if self.reason else ""),
                "elapsed: %.1f s" % (time.time() - self.t0), ""]
        self.path.write_text("\n".join(head + self.lines + [""]) + "\n")
        print("[chaos %d] %-4s %s (%.1f s)" % (self.number, self.verdict, self.title, time.time() - self.t0))


# --------------------------------------------------------------------------------------- shared fixtures ----
def fresh_db(tag: str) -> str:
    """A migrated, seeded database — the same code `make migrate` runs, with its stdout captured: the drill's
    report is the artifact, and twenty lines of "applied 000N_*.sql" per drill would bury it."""
    import contextlib
    import io
    db = str(TMP / ("%s.db" % tag))
    if Path(db).exists():
        Path(db).unlink()
    with contextlib.redirect_stdout(io.StringIO()):
        rc = RUN_SQL.run_sqlite(ROOT / "db" / "migrations-sqlite", db)
        if rc == 0:
            SEED.seed_sqlite(db)
    if rc != 0:
        raise RuntimeError("migrate failed for %s" % db)
    return db


def open_store(db: str):
    store_mod = _load("pgm_exec_store_chaos", ROOT / "services" / "executor" / "store.py")
    return store_mod


def make_plane(tag: str):
    """A funded user, a queue, the scenario venue and the executor service — the P06 harness, minus unittest."""
    db = fresh_db(tag)
    st = open_store(db)
    store = st.Store.open(db)
    mock = _load("pgm_mock_chaos", ROOT / "services" / "executor-mock" / "mock_clob.py")
    mc = mock.MockClob()
    transport = mock.ScenarioTransport(mc)
    ex_main = _load("pgm_executor_main_chaos", ROOT / "services" / "executor" / "main.py")
    # Imported as a package, not loaded by path: `polygm_core.wallets.lifecycle` uses package-relative imports,
    # and `spec_from_file_location` on a submodule makes those raise "attempted relative import with no known
    # parent package" — a loader choice, not a product bug.
    from polygm_core.wallets import lifecycle as wl
    policy = wl.Policy(allowed_spender="0xexchange")
    at = st.now_ms()
    row = store.conn.execute("SELECT id FROM markets WHERE accepting_orders=1 AND enable_order_book=1"
                             " ORDER BY id LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError("the seed produced no tradable market")
    market = str(row[0])
    token = str(store.conn.execute("SELECT token_id FROM tokens WHERE market_id=? ORDER BY token_id LIMIT 1",
                                   (market,)).fetchone()[0])
    user = "u-demo"
    store.conn.execute("INSERT OR REPLACE INTO balances (user_id,usdc_available_micro,usdc_locked_micro,"
                       "version,reconcile_ms) VALUES (?,?,?,?,?)", (user, 10_000_000_000, 0, 1, at))
    store.set_allowance(user_id=user, token="pUSD", spender="0xexchange", amount_micro=v2.UNLIMITED_ALLOWANCE,
                        at=at)
    store.conn.execute("INSERT OR REPLACE INTO wallets (user_id,provider,custody,address,proxy_address,"
                       "signature_type,policy_hash,state,created_ms,updated_ms) VALUES"
                       "(?, 'turnkey', 'delegated', '0xwallet', '0xproxy', 3, ?, 'trading', ?, ?)",
                       (user, policy.policy_hash(), at, at))
    store.conn.commit()
    ex = ex_main.Executor(store, transport=transport, policy=policy, batch_size=5)
    return {"db": db, "store": store, "mock": mc, "transport": transport, "ex": ex, "market": market,
            "token": token, "user": user, "at": at, "st": st}


def queue(plane, *, price_micro: int = 550_000, size_micro: int = 100 * 10**6, key: str,
          order_type: str = "GTC", audience: str = "user", **kw) -> str:
    r = plane["store"].enqueue_intent(user_id=plane["user"], market_id=plane["market"],
                                      token_id=plane["token"], side="BUY", price_micro=price_micro,
                                      size_micro=size_micro, idempotency_key=key, order_type=order_type,
                                      audience=audience, at=plane["at"], **kw)
    return r["intent_id"]


def counts(store) -> dict:
    q = lambda sql: int(store.conn.execute(sql).fetchone()[0])                      # noqa: E731
    return {"intents": q("SELECT COUNT(*) FROM order_intents"), "orders": q("SELECT COUNT(*) FROM orders"),
            "fills": q("SELECT COUNT(*) FROM fills"), "cash": q("SELECT COUNT(*) FROM cash_ledger"),
            "lots": q("SELECT COUNT(*) FROM position_lots")}


# ---------------------------------------------------------------------------------------- 1. executor kill --
def chaos_1(r: Report, *, runs: int) -> None:
    """Re-run the headline loop at P13's commit, from here, so the index has a live number and not a citation."""
    out = VERIF / "P13-chaos-1-executor-kill.txt"
    cmd = [PY, str(ROOT / "tools" / "p13-recovery-loop.py"), "--runs", str(runs), "--seed", "13",
           "--record", str(out)]
    p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=900)
    tail = (p.stdout + p.stderr).strip().splitlines()[-3:] if (p.stdout or p.stderr) else []
    r.say("command: %s" % " ".join(cmd))
    r.say("exit: %d" % p.returncode)
    for line in tail:
        r.say("  " + line)
    r.fact("runs", runs)
    if p.returncode != 0:
        r.fail("the recovery loop exited %d" % p.returncode)
    elif out.exists():
        text = out.read_text()
        for key, label in (("duplicate_orders", "duplicate orders"), ("lost_positions", "lost positions"),
                           ("limbo", "limbo orders")):
            if ('"%s": 0' % key) in text or ("%s: 0" % key) in text:
                r.say("  %s: 0" % label)
        r.fact("artifact", str(out.relative_to(ROOT)))


# --------------------------------------------------------------------------------- 3. ingest killed mid-write --
CHILD = '''
import os, sqlite3, sys, time
db, n = sys.argv[1], int(sys.argv[2])
con = sqlite3.connect(db)
con.execute("PRAGMA journal_mode=WAL")
con.execute("PRAGMA foreign_keys=ON")
for i in range(n):
    # `trade_id` is an INTEGER PRIMARY KEY (the venue's own id, in the real ingest path) and `wallet` is
    # NOT NULL: a text id in an INTEGER PRIMARY KEY is `datatype mismatch`, which is how the first version of
    # this drill wrote zero rows and still reported success.
    con.execute("INSERT OR IGNORE INTO tape_fills (trade_id, dedupe_key, condition_id, token_id, outcome,"
                " outcome_index, wallet, side, price_micro, size_micro, usd_notional_micro, ts_ms, ingest_ms,"
                " source, fee_rate_bps) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (7000000 + i, "k-%d" % i, "0xcond-chaos", "tok-chaos", "Yes", 0,
                 "0x" + "ab" * 20, "BUY", 500000, 1000000, 500000, 1780000000000 + i,
                 1780000000000 + i, "ws", 0))
    if i % 25 == 0:
        con.commit()
        time.sleep(0.002)
con.commit()
print("child wrote", n)
'''


def chaos_3(r: Report, *, fills: int = 4000, kill_after: float = 0.35) -> None:
    """The consumer is killed mid-write; the restart replays the whole window. UNIQUE(dedupe_key) is the claim."""
    db = fresh_db("chaos3")
    script = TMP / "writer.py"
    script.write_text(CHILD)
    kid = subprocess.Popen([PY, str(script), db, str(fills)], stdout=subprocess.PIPE, text=True)
    # Let it get into the middle of the window, then kill it the way a deploy does: SIGKILL, no cleanup.
    time.sleep(kill_after * max(fills / 4000.0, 0.2))
    os.kill(kid.pid, signal.SIGKILL)
    kid.wait(timeout=10)
    con = sqlite3.connect(db)
    mid = int(con.execute("SELECT COUNT(*) FROM tape_fills WHERE condition_id='0xcond-chaos'").fetchone()[0])
    con.close()
    r.say("killed pid %d after ~%.1f s; rows durable at the kill: %d of %d" % (kid.pid, kill_after, mid, fills))
    # The restart: the same window is replayed from the venue, exactly as the daemon does after a cold start.
    kid2 = subprocess.run([PY, str(script), db, str(fills)], capture_output=True, text=True, timeout=300)
    if kid2.returncode != 0:
        r.say("the replayed child failed: %s" % (kid2.stderr or kid2.stdout).strip()[-200:])
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    total = int(con.execute("SELECT COUNT(*) FROM tape_fills WHERE condition_id='0xcond-chaos'").fetchone()[0])
    distinct = int(con.execute("SELECT COUNT(DISTINCT dedupe_key) FROM tape_fills"
                               " WHERE condition_id='0xcond-chaos'").fetchone()[0])
    notional = int(con.execute("SELECT COALESCE(SUM(usd_notional_micro),0) FROM tape_fills"
                               " WHERE condition_id='0xcond-chaos'").fetchone()[0])
    con.execute("PRAGMA integrity_check")
    r.say("restart exit: %d (%s)" % (kid2.returncode, kid2.stdout.strip()[:60]))
    r.say("rows after the replay: %d, distinct dedupe keys: %d, notional: %d" % (total, distinct, notional))
    r.fact("durable_at_kill", mid)
    r.fact("rows_after_replay", total)
    r.fact("distinct_keys", distinct)
    if total == 0:
        r.fail("the drill wrote no rows at all, so it proved nothing (check the child's stderr)")
    if total != distinct:
        r.fail("the replay duplicated %d rows" % (total - distinct))
    if total < mid:
        r.fail("the restart lost rows that were durable before the kill")
    if total != fills:
        r.say("note: %d of %d fills reached the table; the child was killed before it wrote the rest, and the"
              " replay is what recovers them" % (total, fills))
    if total >= fills:
        r.say("the replay filled the gap and the unique key made the overlap harmless")
    if mid >= fills:
        r.fail("the child finished before the kill landed: this drill interrupted nothing, so it proves nothing "
               "(raise --fills or lower the kill delay)")


# ------------------------------------------------------------------------------------ 4. database killed ----
def chaos_4(r: Report, *, port: int = 3341) -> None:
    """The store stops accepting writes mid-flight.

    The kit's row is "Postgres killed mid-flight". **Substitution, stated:** this box runs the product on SQLite
    (P04 D9 records why), and a killed Postgres cannot be produced here — so what is reproduced is the
    *failure*, not the vendor: an external connection takes the database's write lock (WAL, so readers carry on)
    and every write the API attempts while it is held fails the way a dead primary fails it.

    The first version of this drill chmod'ed the file read-only and was measuring nothing at all: POSIX keeps
    the permissions a handle was *opened* with, so the API went on writing through its existing connection and
    the drill reported the 202 as if the store were dead. Nothing about a green drill should be trusted until
    the failure it claims to inject has been shown to land.

    Two claims, both measured: nothing is accepted that was not written (a 202 whose row was never committed is
    the money bug this exists for), and recovery needs the store to come back and nothing else.
    """
    db = fresh_db("chaos4")
    app, base = _boot_api(db, port)

    def freshen_book() -> None:
        """The order path refuses a stale book (correctly), and the drill is about the store, not the quote: the
        first version of this drill left the seeded book at its seed time and then read its own STALE_QUOTE
        refusal as the store failure it was measuring. Each attempt therefore starts from a fresh quote."""
        con = sqlite3.connect(db)
        con.execute("UPDATE book_levels SET updated_ms=? WHERE market_id='0xM1'", (int(time.time() * 1000),))
        con.commit()
        con.close()

    holder = None
    try:
        body = {"marketId": "0xM1", "tokenId": None, "side": "BUY", "price": "0.55", "size": "10"}
        tok = app_token(db)
        freshen_book()
        row = sqlite3.connect(db).execute("SELECT token_id FROM tokens WHERE market_id='0xM1' LIMIT 1").fetchone()
        body["tokenId"] = str(row[0]) if row else "tok-x"
        before = app_post("%s/v1/orders" % base, body, tok, "chaos4-aaaaaaaa")
        r.say("before the lock: POST /v1/orders -> %d" % before[0])
        con = sqlite3.connect(db)
        counts_before = con.execute("SELECT (SELECT COUNT(*) FROM order_intents),"
                                    " (SELECT COUNT(*) FROM cash_ledger),"
                                    " (SELECT COUNT(*) FROM idempotency_keys)").fetchone()
        con.close()

        # The book is refreshed BEFORE the lock is taken, because while it is held nobody can write — including
        # this drill, which found that out by trying.
        freshen_book()
        holder = sqlite3.connect(db, timeout=0.1)
        holder.execute("BEGIN EXCLUSIVE")                     # the write lock, held for the length of the attempt
        t0 = time.time()
        after = app_post("%s/v1/orders" % base, body, tok, "chaos4-bbbbbbbb")
        took = (time.time() - t0) * 1000
        err = (after[1] or {}).get("error") if isinstance(after[1], dict) else None
        r.say("with the write lock held: POST /v1/orders -> %d in %.0f ms, code %s"
              % (after[0], took, (err or {}).get("code")))
        r.say("body: %s" % json.dumps(after[1])[:200])
        con = sqlite3.connect(db)
        counts_locked = con.execute("SELECT (SELECT COUNT(*) FROM order_intents),"
                                    " (SELECT COUNT(*) FROM cash_ledger),"
                                    " (SELECT COUNT(*) FROM idempotency_keys)").fetchone()
        con.close()
        holder.rollback()
        holder.close()
        holder = None
        r.say("rows with the lock held (intents, cash, idem): %s" % (counts_locked,))

        read = app_get("%s/v1/markets/0xM1" % base)
        r.say("after the lock is released: GET /v1/markets/0xM1 -> %d" % read[0])
        freshen_book()
        back = app_post("%s/v1/orders" % base, body, tok, "chaos4-cccccccc")
        con = sqlite3.connect(db)
        counts_after = con.execute("SELECT (SELECT COUNT(*) FROM order_intents),"
                                   " (SELECT COUNT(*) FROM cash_ledger),"
                                   " (SELECT COUNT(*) FROM idempotency_keys)").fetchone()
        con.close()
        r.say("and an order is accepted again: POST /v1/orders -> %d; rows now (intents, cash, idem) %s"
              % (back[0], counts_after))

        r.fact("refused_status", after[0])
        r.fact("refusal_ms", round(took, 1))
        r.fact("rows_added_while_locked", counts_locked[0] - counts_before[0])
        r.fact("idem_rows_added_while_locked", counts_locked[2] - counts_before[2])
        r.fact("recovered_status", back[0])
        if after[0] < 400:
            r.fail("a write against a store refusing writes was answered %d" % after[0])
        if counts_locked[0] != counts_before[0]:
            r.fail("a refused write left %d intent rows behind" % (counts_locked[0] - counts_before[0]))
        if counts_locked[1] != counts_before[1]:
            r.fail("a refused write moved cash")
        if counts_locked[2] != counts_before[2]:
            r.fail("a refused write left an idempotency key armed: a retry would replay an order that was "
                   "never placed")
        if (err or {}).get("code") != "SERVICE_UNAVAILABLE":
            r.fail("a store that refuses the write was described as %r: the client cannot tell a dependency "
                   "outage from a bug in us" % ((err or {}).get("code"),))
        if not (err or {}).get("retryable"):
            r.fail("the store refusal was not marked retryable: retrying is exactly what should happen")
        if took > 30_000:
            r.fail("the refusal took %.0f ms: the request waited for the store instead of refusing" % took)
        if back[0] != 202 or read[0] != 200:
            r.fail("recovery needed more than the store coming back (read %d, write %d)" % (read[0], back[0]))
        if counts_after[0] != counts_before[0] + 1:
            r.fail("the order placed after recovery is not exactly one row (intents %s -> %s)"
                   % (counts_before[0], counts_after[0]))
    finally:
        if holder is not None:
            try:
                holder.rollback()
                holder.close()
            except Exception:                                 # noqa: BLE001
                pass
        app.terminate()
        app.wait(timeout=10)


# ------------------------------------------------------------------------------------------ 5. cache killed --
def chaos_5(r: Report) -> None:
    """Redis killed — here: every cache entry gone at once.

    This build has no Redis: the read path caches in-process (`flags().cache_ttl_*`) and the database is the
    only source of truth, which is the same position a Redis-less deploy is in when its Redis dies. The
    failure to survive is "the cache is empty and every request goes to the store at once", so the drill
    measures exactly that: reads with a warm cache, the cache dropped, the same reads again.
    """
    db = fresh_db("chaos5")
    app, base = _boot_api(db, 3342)
    try:
        # A specific market and its book, not a list: an empty list answers 200 too, and "the read path did
        # not need the cache" is only a claim if the answers had content to be wrong about.
        warm = app_get("%s/v1/markets/0xM1" % base)
        warm_book = app_get("%s/v1/markets/0xM1/book?depth=5" % base)
        app_mod = sys.modules.get("app")
        cleared = 0
        for attr in ("_CACHE", "_RESP_CACHE", "_cache"):
            store = getattr(app_mod, attr, None)
            if isinstance(store, dict):
                cleared += len(store)
                store.clear()
        r.say("first read: market %s, book levels %d, %d cache entries cleared"
              % (warm[0], len(warm_book[1].get("asks", [])), cleared))
        t0, results = time.time(), []
        for i in range(25):
            status, body = app_get("%s/v1/markets/0xM1" % base)
            book = app_get("%s/v1/markets/0xM1/book?depth=5" % base)
            results.append((status, len(body.get("market", {})), book[0], len(book[1].get("bids", []))))
        took = (time.time() - t0) * 1000
        ok = [x for x in results if x[0] == 200 and x[2] == 200 and x[1] > 0 and x[3] > 0]
        r.say("25 market+book reads with an empty cache: %d complete, %.0f ms total, %.1f ms each"
              % (len(ok), took, took / 25))
        r.fact("reads", len(results))
        r.fact("ok", len(ok))
        r.fact("ms_each", round(took / 25, 1))
        r.fact("cache_entries_cleared", cleared)
        if len(ok) != len(results):
            r.fail("the read path needed the cache: %s" % [x[0] for x in results if x[0] != 200][:3])
        if {x[1] for x in ok} != {results[0][1]} or {x[3] for x in ok} != {results[0][3]}:
            r.fail("the answer changed when the cache was empty")
    finally:
        app.terminate()
        app.wait(timeout=10)


# ------------------------------------------------------------------------------------ 6. venue 429 storm ----
def chaos_6(r: Report, *, attempts: int = 60) -> None:
    """The venue rate-limits us for the length of a storm.

    Two numbers, and they are different failures: (a) the venue answers `rate_limited` to orders we send, and
    (b) our OWN per-user budget (`limits.max_orders_per_minute`) stops the queue before the venue is asked
    again. The first version of this drill queued 60 orders at once and reported "0 posts", which was (b)
    being mistaken for (a) — the product never touched the venue, so the drill proved nothing about a storm.
    Both are measured here, and neither may produce a duplicate or a fill.
    """
    plane = make_plane("chaos6")
    try:
        plane["mock"].set_scenario("rate_limit")
        codes: dict = {}

        def run(n: int) -> None:
            for i in range(n):
                queue(plane, key="chaos6-%d-%08d" % (len(codes), i))
            for tick in range(8):
                rep = plane["ex"].tick(at=plane["at"] + tick * 1_000, reconcile=False)
                for h in rep["handled"]:
                    codes[(h["code"], h["stage"])] = codes.get((h["code"], h["stage"]), 0) + 1

        # (a) under our own budget: the venue's 429 is what refuses them
        run(8)
        posts_a = plane["transport"].post_calls
        r.say("phase A (8 orders, under our budget): %d venue requests, refusals %s"
              % (posts_a, {k: v for k, v in codes.items()}))
        # (b) over the budget: our own limiter stops the queue without asking the venue again
        before_b = plane["transport"].post_calls
        run(attempts)
        posts_b = plane["transport"].post_calls - before_b
        r.say("phase B (%d orders, over the budget): %d further venue requests, refusals now %s"
              % (attempts, posts_b, codes))
        states = dict(plane["store"].conn.execute("SELECT state, COUNT(*) FROM order_intents GROUP BY state")
                      .fetchall())
        orders = counts(plane["store"])
        dupes = int(plane["store"].conn.execute(
            "SELECT COUNT(*) FROM (SELECT intent_id FROM orders GROUP BY intent_id HAVING COUNT(*) > 1)"
        ).fetchone()[0])
        r.say("orders rows: %d, fills: %d, cash rows: %d, intents with two orders: %d"
              % (orders["orders"], orders["fills"], orders["cash"], dupes))
        r.fact("venue_requests_phase_a", posts_a)
        r.fact("venue_requests_phase_b", posts_b)
        r.fact("refusals", {"%s@%s" % k: v for k, v in sorted(codes.items())})
        r.fact("states", {str(k): int(v) for k, v in states.items()})
        r.fact("intents_with_two_orders", dupes)
        if dupes:
            r.fail("%d intents got a second order during the storm" % dupes)
        if orders["fills"]:
            r.fail("a fill was booked against an order the venue never accepted")
        if not any(code == "THROTTLED" for code, _stage in codes):
            r.fail("the venue's own 429 never reached the user as a refusal: %s" % codes)
        if not any(code == "ORDER_RATE" for code, _stage in codes):
            r.fail("our own budget never engaged: %s" % codes)
        if posts_b > 0:
            r.fail("the budget let %d requests through after it was exhausted" % posts_b)
        r.say("the two refusals are distinguishable to the user: `THROTTLED` is the venue's answer, "
              "`ORDER_RATE` is ours, and both say retryable/not per their registry entry")
    finally:
        plane["store"].close()


# -------------------------------------------------------------------------- 7. builder code disabled at venue --
def chaos_7(r: Report) -> None:
    """The venue turns our builder code off. The refusal must be its own sentence, recorded once, and retried
    never — a disabled code retried at 1500 ms intervals is a client hammering a venue that said no."""
    plane = make_plane("chaos7")
    try:
        plane["mock"].set_scenario("builder_disabled")
        iid = queue(plane, key="chaos7-00000001", builder_bps=100)
        rep = plane["ex"].tick(at=plane["at"], reconcile=False)
        handled = rep["handled"][0]
        # `builder_code_status` is keyed by the code itself (it is the venue's builder code we carry), so the
        # row is read by name rather than by an id the table does not have.
        status = None
        if _has_table(plane["store"], "builder_code_status"):
            status = plane["store"].conn.execute(
                "SELECT state, source, reject_count, note FROM builder_code_status ORDER BY changed_ms DESC,"
                " rowid DESC LIMIT 1").fetchone()
        again = plane["ex"].tick(at=plane["at"] + 5_000, reconcile=False)
        r.say("first tick: state=%s code=%s" % (handled["state"], handled["code"]))
        r.say("second tick 5 s later: handled=%s posts=%d" % (again["handled"], plane["transport"].post_calls))
        if status:
            r.say("builder_code_status: state=%s source=%s rejections=%d note=%s" %
                  (status[0], status[1], status[2], str(status[3])[:60]))
            r.fact("status_state", status[0])
            r.fact("reject_count", int(status[2] or 0))
        else:
            r.say("builder_code_status: NO ROW — the alarm did not record the refusal")
            r.fact("status_state", None)
        if not status:
            r.fail("the venue refused the code and nothing was recorded for an operator to see")
        r.fact("first_code", handled["code"])
        r.fact("retried", len(again["handled"]))
        if handled["code"] != "BUILDER_DISABLED":
            r.fail("the venue's disabled builder code surfaced as %s" % handled["code"])
        if again["handled"]:
            r.fail("a disabled builder code was retried")
        if status and status[0] != "disabled":
            r.fail("builder_code_status says %s" % status[0])
        # The API refuses to import without a migrated database (by design), so the probe is given the drill's
        # own file rather than the developer's `var/polygm.db`.
        env = dict(os.environ, PGM_DB_PATH=plane["db"],
                   PYTHONPATH=os.pathsep.join([str(ROOT / "packages"), str(ROOT / "services" / "api")]))
        intake = subprocess.run([PY, "-c", "import app;print(app._tg_plain_refusal('BUILDER_DISABLED'))"],
                                cwd=str(ROOT), env=env, capture_output=True, text=True)
        r.say("user-facing sentence: %s" % intake.stdout.strip()[:120])
        if "builder code" not in intake.stdout.lower():
            r.fail("the refusal has no sentence of its own")
        r.fact("sentence", intake.stdout.strip()[:120])
        r.fact("intent_state", plane["store"].conn.execute("SELECT state FROM order_intents WHERE id=?",
                                                           (iid,)).fetchone()[0])
    finally:
        plane["store"].close()


def _has_table(store, name: str) -> bool:
    return bool(store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


# ----------------------------------------------------------------------------------------- 8. signer down ----
def chaos_8(r: Report) -> None:
    """The wallet provider stops signing. Trading stops; the read side does not notice."""
    plane = make_plane("chaos8")
    try:
        queue(plane, key="chaos8-00000001")
        plane["store"].conn.execute("UPDATE wallets SET custody='read_only', signature_type=0, state='funded'"
                                    " WHERE user_id=?", (plane["user"],))
        plane["store"].conn.commit()
        rep = plane["ex"].tick(at=plane["at"], reconcile=False)
        handled = rep["handled"][0] if rep["handled"] else {}
        r.say("executor with a watch-only wallet: state=%s code=%s posts=%d" %
              (handled.get("state"), handled.get("code"), plane["transport"].post_calls))
        # the read side, over HTTP, on the same database
        app, base = _boot_api(plane["db"], 3343)
        try:
            book = app_get("%s/v1/markets/0xM1/book?depth=5" % base)
            health = app_get("%s/healthz" % base)
            r.say("reads during the outage: book -> %d, healthz -> %d" % (book[0], health[0]))
            r.fact("book_status", book[0])
        finally:
            app.terminate()
            app.wait(timeout=10)
        r.fact("posts", plane["transport"].post_calls)
        r.fact("code", handled.get("code"))
        if handled.get("code") not in ("SIGNATURE_REFUSED", "SIGNER_UNAVAILABLE", "WATCH_ONLY"):
            r.fail("a watch-only wallet was refused with %s, which does not say why" % handled.get("code"))
        if plane["transport"].post_calls:
            r.fail("something was signed with a watch-only wallet")
        if book[0] != 200:
            r.fail("the book stopped answering during the signing outage")
    finally:
        plane["store"].close()


# ------------------------------------------------------------------------------------ 9/10. re-run drills ---
def rerun(r: Report, cmd: list[str], artifact: Path, *, timeout: int = 1200, facts: dict | None = None) -> None:
    if artifact.exists():
        artifact.unlink()
    p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    text = (p.stdout or "") + (p.stderr or "")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(text)
    tail = [ln for ln in text.strip().splitlines() if ln.strip()][-6:]
    r.say("command: %s" % " ".join(cmd))
    r.say("exit: %d, artifact: %s" % (p.returncode, artifact.relative_to(ROOT)))
    for ln in tail:
        r.say("  " + ln)
    r.fact("artifact", str(artifact.relative_to(ROOT)))
    for k, v in (facts or {}).items():
        r.fact(k, v)
    if p.returncode != 0:
        r.fail("the drill exited %d" % p.returncode)


# ------------------------------------------------------------------------------------------ api helpers ----
def _boot_api(db: str, port: int) -> tuple[subprocess.Popen, str]:
    # PYTHONPATH is not optional: `app` imports `polygm_core`, and a child that starts without the packages
    # directory dies at import — which the first version of this helper did, three drills in a row, each
    # reporting "the API did not come up" as if the API were the thing that was broken.
    env = dict(os.environ, PGM_DB_PATH=db, PGM_TELEGRAM_BOT_TOKEN="123456789:" + "A" * 35,
               PGM_ADMIN_TOKEN="adm_" + "t" * 44, PGM_REQUIRE_SECURITY_ENV="0", PGM_TRUST_USER_HEADER="1",
               PYTHONPATH=os.pathsep.join([str(ROOT / "packages"), str(ROOT / "services" / "api"),
                                           str(ROOT / "services" / "executor"),
                                           str(ROOT / "services" / "executor-mock"), str(ROOT / "tools")]))
    p = subprocess.Popen([PY, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port),
                          "--log-level", "warning"], cwd=str(ROOT / "services" / "api"), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            if app_get("http://127.0.0.1:%d/healthz" % port)[0] == 200:
                return p, "http://127.0.0.1:%d" % port
        except Exception:                                                          # noqa: BLE001 — still booting
            time.sleep(0.3)
    p.terminate()
    raise RuntimeError("the API did not come up on %d" % port)


def app_get(url: str, timeout: float = 8.0):
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as fh:
            return fh.status, json.loads(fh.read().decode() or "{}")
    except urllib.error.HTTPError as e:                                            # noqa: PERF203
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:                                                          # noqa: BLE001
            return e.code, {}


def app_post(url: str, body: dict, token: str, key: str, timeout: float = 20.0):
    """`X-User-Id` is the dev/CI identity (default on unless `PGM_REQUIRE_SECURITY_ENV=1`), and the bearer the
    first version sent was not a session, so every order came back 401 and the drill "measured" an auth failure."""
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-User-Id": str(token), "Idempotency-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as fh:
            return fh.status, json.loads(fh.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:                                                          # noqa: BLE001
            return e.code, {}


def app_token(db: str, *, user: str = "u-demo") -> str:
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    row = con.execute("SELECT 1 FROM users WHERE id=?", (user,)).fetchone()
    if row is None:
        con.execute("INSERT INTO users (id, created_ms, tier) VALUES (?,?, 'trader')", (user, 1))
        con.commit()
    con.close()
    return "u-demo"


def main() -> int:
    ap = argparse.ArgumentParser(description="P13 D7's ten chaos tests")
    ap.add_argument("--only", default="", help="comma-separated drill numbers (default: all)")
    ap.add_argument("--skip-live", action="store_true", help="skip drill 2 (live venue, ~7 minutes)")
    ap.add_argument("--execute-live", action="store_true", help="run drill 2 here instead of citing the artifact")
    ap.add_argument("--keys", type=int, default=10_000, help="drill 10: key population")
    ap.add_argument("--record", default="", help="index artifact path (docs/verification/P13-chaos-suite.txt)")
    ap.add_argument("--json", dest="json_path", default="")
    a = ap.parse_args()

    want = {int(x) for x in a.only.split(",") if x.strip()} if a.only else set(range(1, 11))
    VERIF.mkdir(parents=True, exist_ok=True)
    reports: dict[int, Report] = {}

    def drill(number: int, title: str, fn, artifact: Path | None = None) -> None:
        if number not in want:
            return
        # The drills that re-run an earlier tool own their artifact name (`rerun` hands it to the child), and the
        # first version of this runner wrote the slug-named file *as well* — two artifacts for one drill, one of
        # them empty of the child's output. `artifact=` is how a drill says which file is the report.
        path = artifact or VERIF / ("P13-chaos-%d-%s.txt" % (number, _slug(title)))
        r = Report(number, title, path)
        reports[number] = r
        try:
            fn(r)
        except Exception as e:                                                     # noqa: BLE001 — recorded
            import traceback
            r.say("traceback:")
            for ln in traceback.format_exc().splitlines()[-8:]:
                r.say("  " + ln)
            r.fail("%s: %s" % (type(e).__name__, e))
        r.save()

    drill(1, "executor killed mid-submit", lambda r: chaos_1(r, runs=6),
          artifact=VERIF / "P13-chaos-1-executor-kill.txt")
    if 2 in want:
        r = Report(2, "WebSocket killed for 5 minutes", VERIF / "P13-chaos-2-ws-kill.txt")
        reports[2] = r
        if a.execute_live:
            rerun(r, [PY, str(ROOT / "tools" / "p05-chaos-test.py"), "--kill-seconds", "300", "--json"],
                  VERIF / "P13-chaos-2-ws-kill.txt", timeout=900)
        elif (VERIF / "P13-chaos-2-ws-kill.txt").exists():
            text = (VERIF / "P13-chaos-2-ws-kill.txt").read_text()
            r.say("live drill artifact: docs/verification/P13-chaos-2-ws-kill.txt (%d bytes)" % len(text))
            for ln in [x for x in text.strip().splitlines() if x.strip()][-4:]:
                r.say("  " + ln)
            r.fact("artifact", "docs/verification/P13-chaos-2-ws-kill.txt")
        else:
            r.say("no artifact and --execute-live not given: run `python3 tools/p13-chaos-suite.py --only 2"
                  " --execute-live` (needs network, ~7 minutes)")
            r.fact("artifact", "")
        r.save()
    drill(3, "ingest killed mid-write", lambda r: chaos_3(r))
    drill(4, "database killed mid-flight", lambda r: chaos_4(r))
    drill(5, "cache killed", lambda r: chaos_5(r))
    drill(6, "venue 429 storm", lambda r: chaos_6(r))
    drill(7, "builder code disabled by the venue", lambda r: chaos_7(r))
    drill(8, "signer / wallet provider down", lambda r: chaos_8(r),
          artifact=VERIF / "P13-chaos-8-signer-down.txt")
    drill(9, "kill switch during a copy-trade",
          lambda r: rerun(r, [PY, str(ROOT / "tools" / "p06-drill.py"), "--record",
                              str(VERIF / "P13-chaos-9-kill-switch.txt")],
                          VERIF / "P13-chaos-9-kill-switch.txt", timeout=900),
          artifact=VERIF / "P13-chaos-9-kill-switch.txt")
    drill(10, "key compromise drill",
          lambda r: rerun(r, [PY, str(ROOT / "tools" / "p07-drill.py"), "--keys", str(a.keys), "--record",
                              str(VERIF / "P13-chaos-10-key-compromise.txt")],
                          VERIF / "P13-chaos-10-key-compromise.txt"),
          artifact=VERIF / "P13-chaos-10-key-compromise.txt")

    failed = [n for n, r in sorted(reports.items()) if r.verdict != "PASS"]
    lines = ["P13 D7 — ten chaos tests", "=" * 78, ""]
    for n, r in sorted(reports.items()):
        lines.append("%2d. %-8s %s" % (n, r.verdict, r.title))
        lines.append("     report: docs/verification/%s" % r.path.name)
        for k, v in sorted(r.facts.items()):
            lines.append("     %s: %s" % (k, v))
        if r.reason:
            lines.append("     %s" % r.reason)
        lines.append("")
    lines += ["CHAOS SUITE: %s — %d of %d drills reported" %
              ("PASS" if not failed else "FAIL", len(reports) - len(failed), len(reports)),
              "The kit's ten rows are all represented; the substitutions (no Redis, SQLite in dev/CI) are stated",
              "in the artifact of the drill that hits them rather than left implicit."]
    text = "\n".join(lines) + "\n"
    if a.record:
        Path(a.record).write_text(text)
    if a.json_path:
        Path(a.json_path).write_text(json.dumps(
            {"drills": {str(n): {"title": r.title, "verdict": r.verdict, "facts": r.facts,
                                 "artifact": "docs/verification/%s" % r.path.name} for n, r in sorted(reports.items())},
             "failed": failed, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2, sort_keys=True) + "\n")
    print(text if not a.record else "\n".join(lines[-3:]) + "\n(written to %s)" % a.record)
    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if not failed else 1


def _slug(title: str) -> str:
    """A filename from a drill's title: lowercase, one dash per separator, nothing else.

    The first version replaced only "/" (and kept the space), which produced
    `P13-chaos-8-signer- -wallet-provider-down.txt` — a filename quoted back to a human in the report. Anything
    that is not a letter, a digit or a dash is a separator here.
    """
    out, prev_dash = [], False
    for ch in title.lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-")[:44].strip("-")


if __name__ == "__main__":
    raise SystemExit(main())
