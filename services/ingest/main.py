#!/usr/bin/env python3
"""The ingest daemon: one process, four loops, one clock.

Everything in this file is the part that has to live in a *process* rather than a library: what runs when, what
survives a restart, and what the page is told about how sure we are. The parsing is in `normalise.py`, the book
maths in `books.py`, the tape in `tape.py`, the freshness ladder in `freshness.py`, the universe policy in
`universe.py`, the rule maths in `polygm_core.signals.engine` — none of them know this file exists, which is
why all of them are testable without a socket.

Four loops, deliberately not four threads sharing state:

    sync_universe()   Gamma keyset backfill -> discovery poll -> prune/wake -> metadata diff
    poll_tape()       the public tape over REST, bounded ranges, the only writer of `tape_fills`
    ws_tick()         the socket: book snapshots/deltas and live trades (latency and alerts, never the tape)
    evaluate()        the rule engine over the events the three above produced, then `signals` + deliveries

Two measured facts shape most of the code, both re-checkable with `python3 tools/p05-cache-probe.py`:

  * `GET data-api.polymarket.com/trades` **without** `start`/`end` serves a view that is minutes old and never
    refreshes (newest fill 236 s / 257 s / 277 s old at samples 20 s apart; byte-identical with the CDN cache
    bypassed, so it is the origin's materialised view). With `start`/`end` the same endpoint measured 0-1 s old,
    3 of 3. So the poller asks for a range and the resume window is a range, and cache-busting appears nowhere
    in this file — it was the wrong tool for a different theory.
  * `market=` on that endpoint filters on the CONDITION id and returns nothing for a token id.

Id rule, because it is the one thing a reader will otherwise get wrong: ingest keys on the venue's `conditionId`
(`tape_fills.condition_id`, `market_rollups.condition_id`, `signals.condition_id`) while the product keys on
Gamma's market id (`markets.id`, `tokens.market_id`, `book_levels.market_id`). `markets.condition_id` carries a
UNIQUE index and is the only allowed hop between the two — `self.cid_to_mid` below is that hop, loaded from the
database rather than guessed.

No secrets, and no kill-switch interaction on purpose: the kill switch stops *orders* (P04 risk gate), never
ingestion — a paused feed is a user looking at a dead market with no explanation, which is worse than a dead
feed.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _p in (str(ROOT / "packages"), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import books as B                                    # noqa: E402
import freshness as FR                               # noqa: E402
import net as NET                                    # noqa: E402
import normalise as N                                 # noqa: E402
import tape as T                                     # noqa: E402
import universe as U                                  # noqa: E402
import wsclient as W                                  # noqa: E402
from polygm_core.classify import labels as LB         # noqa: E402
from polygm_core.signals import engine as E           # noqa: E402

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

RESUME_OVERLAP_S = 300          # replay five minutes on restart; dedupe makes it cheap, a hole is permanent
LOOKBACK_CAP_S = 6 * 3600       # and never more than six hours, or a weekend outage costs the next hour of boot
TAPE_PAGE = 500                 # the venue's max on /trades (measured)
MAX_TAPE_PAGES = 20             # 10k fills is well past the busiest minute the venue has ever shown us
DEPTH_CENTS = 5                 # depth is meaningless without the window it was taken over, so it is an argument
FORWARD_SLACK_S = 60            # the venue's indexer stamps some fills a few seconds ahead of our clock
# `alert_deliveries.priority` is the queue's only fairness input (0 paid, 1 trial, 2 free), and the tier lives
# on `entitlements.plan`. It is read per rule rather than assumed, because "everyone on the free plan waits"
# is a product decision that must not be hard-coded in the wrong service.
PLAN_PRIORITY = {"trader": 0, "pro": 0, "trial": 1}


@dataclass
class Config:
    db_path: str = str(ROOT / "var" / "polygm.db")
    ws_url: str = WS_URL
    tape_capacity: int = 2_000
    max_books: int = 2_000
    tape_poll_s: float = 2.0
    universe_budget: int = 40           # Gamma pages per pass, so a restart does not spend 300 requests at once
    ws_wait_s: float = 0.25
    ws_burst_s: float = 2.0             # how long a --once pass listens to the socket
    stale_book_ms: int = 3_000
    label_every_passes: int = 30

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        e = dict(os.environ if env is None else env)
        c = cls()
        c.db_path = e.get("PGM_DB_PATH") or c.db_path
        c.ws_url = e.get("PGM_WS_URL") or c.ws_url

        def num(name, cast, default):
            raw = e.get(name)
            if raw in (None, ""):
                return default
            try:
                return cast(raw)
            except ValueError:
                # A typo in a number must not quietly become the default on the box that pages at 3am.
                raise SystemExit("%s is %r, which does not parse as a number" % (name, raw))
        c.tape_capacity = num("PGM_TAPE_CAP", int, c.tape_capacity)
        c.max_books = num("PGM_MAX_BOOKS", int, c.max_books)
        c.tape_poll_s = num("PGM_TAPE_POLL_S", float, c.tape_poll_s)
        c.universe_budget = num("PGM_UNIVERSE_PAGES", int, c.universe_budget)
        c.stale_book_ms = num("PGM_BOOK_STALE_MS", int, c.stale_book_ms)
        return c


class Store:
    """The database. The only place in ingest that knows SQL, and deliberately SQLite-shaped.

    Statements sit in the intersection of SQLite and Postgres (`ON CONFLICT ... DO UPDATE`, `?` placeholders
    translated at one point, no `RETURNING` on a path that must work on both). `db/migrations/0006_ingest.sql`
    is the Postgres truth; `db/migrations-sqlite/` is generated from it, so "works on my sqlite" and "works on
    the deployment engine" are the same claim rather than two.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.statement_count = 0

    @classmethod
    def open(cls, path: str) -> "Store":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")        # the dev engine must agree with Postgres about what is legal
        conn.execute("PRAGMA busy_timeout=5000")      # the API process reads while we write
        return cls(conn)

    # ------------------------------------------------------------------ boot
    def ready(self) -> list[str]:
        """Missing tables this process writes. Not a nicety: starting against a half-applied schema and
        discovering it on the first fill means the tape is silently missing its first hour."""
        want = ("ingest_cursors", "tape_fills", "market_rollups", "signal_rules", "signals", "signal_state",
                "alert_deliveries", "wallet_labels", "wallet_label_history", "market_meta_versions", "markets",
                "tokens", "events", "book_levels")
        have = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return [t for t in want if t not in have]

    def resume(self, source: str) -> dict:
        row = self.conn.execute("SELECT cursor_json, last_event_ms, state FROM ingest_cursors WHERE source=?",
                                (source,)).fetchone()
        if row is None:
            return {"cursor": {}, "last_event_ms": 0, "state": "down"}
        try:
            cur = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
        except (json.JSONDecodeError, TypeError):
            # A corrupt cursor is not a licence to invent one: resuming from 0 replays and dedupes, which is
            # merely slow, while a guessed cursor loses fills forever and looks healthy doing it.
            cur = {}
        return {"cursor": cur, "last_event_ms": int(row[1] or 0), "state": row[2] or "down"}

    def save_cursor(self, source: str, *, cursor: dict, last_event_ms: int, state: str,
                    now_ms: int | None = None) -> None:
        now_ms = now_ms or int(time.time() * 1000)
        self._run("INSERT INTO ingest_cursors (source, cursor_json, last_event_ms, last_frame_ms, state,"
                  " updated_ms) VALUES (?,?,?,?,?,?) ON CONFLICT (source) DO UPDATE SET"
                  " cursor_json=excluded.cursor_json,"
                  " last_event_ms=MAX(ingest_cursors.last_event_ms, excluded.last_event_ms),"
                  " last_frame_ms=excluded.last_frame_ms, state=excluded.state, updated_ms=excluded.updated_ms",
                  (source, json.dumps(cursor, sort_keys=True, default=str), int(last_event_ms), now_ms,
                   state, now_ms))

    def _run(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn_execute(sql, params)

    def conn_execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        self.statement_count += 1
        return self.conn.execute(sql, params)

    # --------------------------------------------------------------- markets
    def upsert_universe(self, markets: list[dict], raw_by_cid: dict[str, dict] | None = None
                        ) -> tuple[int, int, int]:
        """(markets_new, markets_changed, events_new). The metadata diff is done by the caller, which is the
        only place that still has the raw Gamma rows.

        Events first, because `markets.event_id` has a foreign key: inserting a market whose event has not
        arrived would be rejected on Postgres and silently accepted on a dev database with the pragma off,
        which is the worst possible pair of behaviours.
        """
        now_ms = int(time.time() * 1000)
        events_new = new = changed = 0
        resolved_at: dict[str, int] = {}
        for m in markets:
            for ev in m.get("event_refs") or []:
                # `events_slug_uq` is a second unique constraint on this row, and it can collide WITHOUT the id
                # colliding (Gamma has reused slugs across events). `ON CONFLICT (id)` alone would raise on that
                # path, so the slug is checked first: an event we already have under another id is not a reason
                # to lose the market row that points at it.
                if self.conn_execute("SELECT 1 FROM events WHERE slug = ?", (ev["slug"],)).fetchone():
                    continue
                cur = self._run("INSERT INTO events (id, slug, title, neg_risk, created_ms, updated_ms)"
                                " VALUES (?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING",
                                (ev["id"], ev["slug"], ev["title"][:300], 1 if m["neg_risk"] else 0,
                                 now_ms, now_ms))
                events_new += 1 if cur.rowcount == 1 else 0
            event_id = (m.get("event_refs") or [{}])[0].get("id")
            tick = _tick_micro(m["min_tick"])
            cur = self._run("SELECT question, accepting_orders, end_ts, fee_type FROM markets WHERE id=?",
                            (m["id"],))
            old = cur.fetchone()
            vals = (m["id"], m["condition_id"], event_id, m["question"], m["slug"],
                    1 if m["accepting_orders"] else 0, m["seconds_delay"], 1 if m["enable_order_book"] else 0,
                    tick / 10 ** 6, _order_size(m["min_order_size"]), m["fee_type"] or "",
                    1 if m["neg_risk"] else 0, m.get("neg_risk_group_id"), m.get("end_ts"),
                    json.dumps(m["outcomes"]), now_ms, now_ms)
            if old is None:
                self._run("INSERT INTO markets (id, condition_id, event_id, question, slug, accepting_orders,"
                          " seconds_delay, enable_order_book, minimum_tick_size, minimum_order_size, fee_type,"
                          " neg_risk, neg_risk_group_id, end_ts, outcomes_json, first_seen_ms, updated_ms)"
                          " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vals)
                new += 1
            else:
                changed_here = (str(old[0] or "") != m["question"]
                                or bool(old[1]) != bool(m["accepting_orders"])
                                or str(old[2] or "") != str(m.get("end_ts") or ""))
                self._run("UPDATE markets SET question=?,condition_id=?,event_id=?,slug=?,accepting_orders=?,"
                          " seconds_delay=?,enable_order_book=?,minimum_tick_size=?,minimum_order_size=?,"
                          "fee_type=?,neg_risk=?,end_ts=?,updated_ms=? WHERE id=?",
                          (m["question"], m["condition_id"], event_id, m["slug"],
                           1 if m["accepting_orders"] else 0, m["seconds_delay"],
                           1 if m["enable_order_book"] else 0, tick / 10 ** 6,
                           _order_size(m["min_order_size"]), m["fee_type"] or "", 1 if m["neg_risk"] else 0,
                           m.get("end_ts"), now_ms, m["id"]))
                changed += 1 if changed_here else 0
            prices = m.get("outcome_price_micro") or []
            for idx, tok in enumerate(m["tokens"]):
                # A resolved binary market's outcome price is exactly 1 or 0, which is the winner, stated by the
                # venue, on a field we already store. Deriving `is_winner` here (rather than waiting for a
                # resolution service we do not have yet) is what makes `smart_money` and `insider_suspect`
                # computable from our own rows. Only set on a CLOSED market: an 0.999 price is not a winner.
                # A resolved binary market's outcome price IS the winner, stated by the venue on a field we
                # already store, so the two resolution-dependent labels are computable from our own rows.
                # `is_winner` stays NULL until then, because the column's own comment is the rule: NULL must not
                # read as FALSE, or every unresolved market becomes a loss the wallet made.
                if not m["closed"]:
                    winner = None
                elif idx < len(prices):
                    winner = 1 if prices[idx] >= 999_000 else 0
                else:
                    winner = None
                self._run("INSERT INTO tokens (token_id, market_id, outcome, outcome_index, is_winner)"
                          " VALUES (?,?,?,?,?) ON CONFLICT (token_id) DO UPDATE SET outcome=excluded.outcome,"
                          " outcome_index=excluded.outcome_index, is_winner=excluded.is_winner",
                          (tok, m["id"], (m["outcomes"][idx] if idx < len(m["outcomes"]) else ""), idx, winner))
                if winner is not None:
                    resolved_at[m["id"]] = now_ms
            raw = (raw_by_cid or {}).get(m["condition_id"], {})
            self._run("INSERT INTO market_stats (condition_id,volume_24h_micro,liquidity_micro,meta_json,"
                      "updated_ms) VALUES (?,?,?,?,?) ON CONFLICT (condition_id) DO UPDATE SET"
                      " volume_24h_micro=excluded.volume_24h_micro,"
                      " liquidity_micro=excluded.liquidity_micro, meta_json=excluded.meta_json,"
                      " updated_ms=excluded.updated_ms",
                      (m["condition_id"], m["volume_24h_micro"], m["liquidity_micro"],
                       json.dumps({k: raw.get(k) for k in META_FIELDS}, sort_keys=True, default=str), now_ms))
        for mid, seen_ms in resolved_at.items():
            self._run("UPDATE market_stats SET resolved_seen_ms = ? WHERE condition_id = ("
                      " SELECT condition_id FROM markets WHERE id = ?)", (seen_ms, mid))
        return new, changed, events_new

    def meta_versions(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        now_ms = int(time.time() * 1000)
        self.conn.executemany("INSERT INTO market_meta_versions (market_id,field,old_value,new_value,seen_ms)"
                              " VALUES (?,?,?,?,?)",
                              [(r["market_id"], r["field"], str(r["old"]), str(r["new"]), now_ms) for r in rows])
        self.statement_count += 1
        return len(rows)

    def stored_market_view(self, ids: list[str]) -> dict[str, dict]:
        """The venue's tracked fields for these markets, verbatim, as last seen.

        Read from `market_stats.meta_json` rather than re-derived from the `markets` columns: the comparison is
        between two observations of the same source, not between an observation and our rendering of it. The
        first version of this function rebuilt the dict from `minimum_order_size` and friends and wrote a
        metadata change for every market on every pass, because `5.0` != `"5"`.
        """
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            ph = ", ".join("?" * len(chunk))
            for cid, meta in self.conn.execute("SELECT condition_id, meta_json FROM market_stats WHERE"
                                               " condition_id IN (%s)" % ph, chunk):
                try:
                    d = json.loads(meta or "{}")
                except json.JSONDecodeError:
                    continue
                if d:
                    out[str(cid)] = d
        return out

    # ------------------------------------------------------------------ tape
    def insert_fills(self, fills: list[dict]) -> int:
        """Write the tape; return how many rows are NEW.

        Counted with `total_changes` because `rowcount` under `ON CONFLICT DO NOTHING` is 0 for both "nothing
        happened" and "everything conflicted", and telling those apart is the entire point of the dedupe.
        """
        if not fills:
            return 0
        now_ms = int(time.time() * 1000)
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT INTO tape_fills (dedupe_key,condition_id,token_id,outcome,outcome_index,wallet,side,"
            "price_micro,size_micro,usd_notional_micro,ts_ms,ingest_ms,source,fee_rate_bps)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (dedupe_key) DO NOTHING",
            [(T.dedupe_key(f), f["market"], f["token_id"], f.get("outcome") or "", f.get("outcome_index"),
              f.get("wallet") or "", f["side"], f["price_micro"], f["size_micro"], f["usd_notional_micro"],
              f["ts_ms"], now_ms, f.get("source") or "rest", int(f.get("fee_rate_bps") or 0)) for f in fills])
        self.statement_count += 1
        return self.conn.total_changes - before

    def rollups(self, condition_ids: list[str], since_ms: int) -> int:
        """Recompute 1m/5m/1h/1d aggregates for the markets that moved, FROM THE STORED ROWS.

        Recomputing rather than accumulating in RAM is the P05 constraint that every derived number is
        reproducible from stored data: an in-memory accumulator is faster and drifts on the first restart, and
        a volume chart that silently disagrees with the tape under it is a trust failure, not a rounding one.
        """
        n = 0
        for interval, width in (("1m", 60_000), ("5m", 300_000), ("1h", 3_600_000), ("1d", 86_400_000)):
            for cid in condition_ids:
                rows = self.conn.execute(
                    "SELECT (ts_ms / ?) * ? AS bucket, COUNT(*), SUM(usd_notional_micro),"
                    " SUM(price_micro * size_micro / 1000000), SUM(size_micro), MAX(usd_notional_micro)"
                    " FROM tape_fills WHERE condition_id = ? AND ts_ms >= ? GROUP BY bucket",
                    (width, width, cid, since_ms)).fetchall()
                self.statement_count += 1
                for bucket, fills, vol, notional, shares, mx in rows:
                    med = self._median_notional(cid, bucket, bucket + width)
                    self._run("INSERT INTO market_rollups (condition_id,bucket_ms,interval,fills,volume_micro,"
                              "vwap_micro,max_fill_micro,median_fill_micro) VALUES (?,?,?,?,?,?,?,?)"
                              " ON CONFLICT (condition_id,interval,bucket_ms) DO UPDATE SET"
                              " fills=excluded.fills, volume_micro=excluded.volume_micro,"
                              " vwap_micro=excluded.vwap_micro, max_fill_micro=excluded.max_fill_micro,"
                              " median_fill_micro=excluded.median_fill_micro",
                              (cid, bucket, interval, fills, vol or 0,
                               # sum(p*q)/sum(q) in micro-units, integer division at the END, never a float
                               (notional or 0) * 10 ** 6 // shares if shares else 0, mx or 0, med))
                    n += 1
        return n

    def _median_notional(self, condition_id: str, lo: int, hi: int) -> int:
        vals = [r[0] for r in self.conn.execute(
            "SELECT usd_notional_micro FROM tape_fills WHERE condition_id=? AND ts_ms>=? AND ts_ms<?"
            " ORDER BY usd_notional_micro", (condition_id, lo, hi))]
        self.statement_count += 1
        return vals[len(vals) // 2] if vals else 0

    def market_stats(self, condition_id: str, now_ms: int, window_ms: int = 3_600_000) -> dict:
        """Sample size and median fill size for one market: `large_fill`'s two false-positive controls. The
        median is read from stored rows so the alert says the same thing after a restart as before it."""
        vals = [r[0] for r in self.conn.execute(
            "SELECT usd_notional_micro FROM tape_fills WHERE condition_id=? AND ts_ms>=?"
            " ORDER BY usd_notional_micro", (condition_id, now_ms - window_ms))]
        return {"sample": len(vals), "median_micro": vals[len(vals) // 2] if vals else 0,
                "max_micro": vals[-1] if vals else 0}

    def recent_volume(self, condition_id: str, now_ms: int, buckets: int = 60) -> list[int]:
        rows = self.conn.execute("SELECT bucket_ms, volume_micro FROM market_rollups WHERE condition_id=?"
                                 " AND interval='1m' ORDER BY bucket_ms DESC LIMIT ?",
                                 (condition_id, buckets)).fetchall()
        return [int(r[1] or 0) for r in reversed(rows)]

    def wallet_rows(self, condition_ids: list[str], since_ms: int, limit: int = 400) -> list[dict]:
        ph = ", ".join("?" * len(condition_ids))
        return [dict(zip(("wallet", "fills", "volume_micro", "markets", "first_ms", "buy_micro", "sell_micro"),
                         r)) for r in self.conn.execute(
            "SELECT wallet, COUNT(*), SUM(usd_notional_micro), COUNT(DISTINCT condition_id), MIN(ts_ms),"
            " SUM(CASE WHEN side='BUY' THEN usd_notional_micro ELSE 0 END),"
            " SUM(CASE WHEN side='SELL' THEN usd_notional_micro ELSE 0 END) FROM tape_fills"
            " WHERE ts_ms >= ? AND condition_id IN (%s) AND wallet <> '' GROUP BY wallet"
            " ORDER BY SUM(usd_notional_micro) DESC LIMIT ?" % ph,
            [since_ms] + condition_ids + [limit])]

    def fills_for(self, condition_id: str, since_ms: int, limit: int = 2_000) -> list[dict]:
        return [dict(zip(("wallet", "side", "price_micro", "size_micro", "usd_notional_micro", "ts_ms",
                          "token_id", "condition_id"), r)) for r in self.conn.execute(
            "SELECT wallet, side, price_micro, size_micro, usd_notional_micro, ts_ms, token_id, condition_id"
            " FROM tape_fills WHERE condition_id=? AND ts_ms>=? ORDER BY ts_ms DESC LIMIT ?",
            (condition_id, since_ms, limit))]

    # ---------------------------------------------------------------- books
    def replace_book(self, market_id: str, snap: dict, now_ms: int) -> int:
        """Aggregate-by-level snapshot into `book_levels`, which is what the API serves.

        DELETE-then-insert inside one transaction, because the venue's truth at `ts_ms` includes levels that
        are GONE: an UPSERT-only strategy leaves phantom resting orders on the ladder, and a user sizing a
        trade against a ghost is a loss we caused.
        """
        # Only open a transaction when one is not already open: this connection is shared with readers in the dev
        # shell and in tests, and `BEGIN` inside an open transaction is an error that would take ingest down over
        # a book update. When a caller owns the transaction, the rows land in theirs.
        began = not self.conn.in_transaction
        if began:
            self.conn.execute("BEGIN")
        self.conn.execute("DELETE FROM book_levels WHERE market_id=?", (market_id,))
        rows = []
        for side in ("bids", "asks"):
            for price, size in sorted((snap[side] or {}).items(), key=lambda kv: int(kv[0])):
                rows.append((market_id, "bid" if side == "bids" else "ask", int(price), int(size), 1, now_ms))
        if rows:
            self.conn.executemany("INSERT INTO book_levels (market_id,side,price_micro,size_shares_micro,"
                                  "level_count,updated_ms) VALUES (?,?,?,?,?,?)", rows)
        if began:
            self.conn.execute("COMMIT")
        self.statement_count += 1
        return len(rows)

    # ----------------------------------------------------------------- rules
    def load_rules(self) -> tuple[list[E.Rule], list[str], dict[str, int], dict[str, str]]:
        """(rules, errors, cooldown_s per rule, owner per rule).

        A row that fails validation is KEPT and reported, never deleted or silently skipped: a user whose alert
        quietly stopped working has no way to find out, and the table is the only place the answer lives.
        """
        out, errors, cooldowns, owners = [], [], {}, {}
        try:
            cur = self.conn.execute("SELECT id, owner, kind, params_json, market_filter_json, cooldown_s,"
                                    " severity, channels_json FROM signal_rules WHERE enabled = 1")
        except sqlite3.OperationalError as e:            # a schema we did not migrate is not our fault to hide
            return [], ["signal_rules unreadable: %s" % str(e)[:120]], {}, {}
        for rid, owner, kind, params, filt, cooldown_s, severity, channels in cur.fetchall():
            try:
                out.append(E.build_rule(rid, kind, json.loads(params or "{}"), owner=owner or "system",
                                       market_filter=json.loads(filt or "{}"),
                                       cooldown_s=int(cooldown_s) if cooldown_s is not None else None,
                                       severity=severity, channels=json.loads(channels or "[]")))
            except (E.RuleError, json.JSONDecodeError, TypeError, ValueError) as e:
                errors.append("%s: %s" % (rid, str(e)[:160]))
            cooldowns[rid] = max(1, int(cooldown_s or 300))
            owners[rid] = owner or "system"
        return out, errors, cooldowns, owners

    def record_alerts(self, alerts: list[E.Alert], cooldowns: dict[str, int], owners: dict[str, str]) -> int:
        """Insert `signals`, refresh `signal_state`, and enqueue one `alert_deliveries` row per channel.

        `fired_bucket` is the cooldown window the fire belongs to, and the UNIQUE (rule_id, dedupe_key,
        fired_bucket) is what makes "at most one alert per window" a database property rather than a code path
        someone has to remember to call.
        """
        written = 0
        for a in alerts:
            width = cooldowns.get(a.rule_id, 300) * 1000
            bucket = int(a.at_ms // width)
            cur = self._run("INSERT INTO signals (rule_id,kind,condition_id,token_id,severity,title,body_json,"
                            " dedupe_key,fired_bucket,fired_ms) VALUES (?,?,?,?,?,?,?,?,?,?)"
                            " ON CONFLICT (rule_id,dedupe_key,fired_bucket) DO NOTHING",
                            (a.rule_id, a.kind, a.market_id, a.token_id, a.severity, a.title,
                             json.dumps(a.body, sort_keys=True, default=str), a.dedupe_key, bucket, a.at_ms))
            if cur.rowcount != 1:
                continue                                # that window already holds this alert; that IS the dedupe
            written += 1
            sid = self.conn.execute("SELECT id FROM signals WHERE rule_id=? AND dedupe_key=? AND fired_bucket=?",
                                    (a.rule_id, a.dedupe_key, bucket)).fetchone()[0]
            self._run("INSERT INTO signal_state (rule_id,dedupe_key,last_fired_ms) VALUES (?,?,?)"
                      " ON CONFLICT (rule_id,dedupe_key) DO UPDATE"
                      " SET last_fired_ms=excluded.last_fired_ms", (a.rule_id, a.dedupe_key, a.at_ms))
            owner = owners.get(a.rule_id, "system")
            prio = self.priority_for(owner)
            for ch in (a.channels or ("inapp",)):
                self._run("INSERT INTO alert_deliveries (signal_id,user_id,channel,priority,queued_ms,status)"
                          " VALUES (?,?,?,?,?,'queued')", (sid, owner, ch, prio, a.at_ms))
        return written

    def priority_for(self, user_id: str) -> int:
        row = self.conn_execute("SELECT plan FROM entitlements WHERE user_id=?", (user_id,)).fetchone()
        return PLAN_PRIORITY.get(str(row[0]) if row else "", 2)

    def load_cooldown_state(self) -> dict[str, int]:
        """`Engine.state`, rebuilt from the durable table.

        The key format has to match the engine's exactly (`dedupe_key|rule_id`, in that order). Building it in
        the wrong order is invisible in a test and means every alert re-fires once after each deploy, which is
        the precise failure this table exists to prevent.
        """
        out: dict[str, int] = {}
        for rid, key, last in self.conn.execute("SELECT rule_id, dedupe_key, last_fired_ms FROM signal_state"):
            out["%s|%s" % (key, rid)] = int(last)
        return out

    def save_labels(self, rows: list[dict]) -> int:
        """`Label.as_row()` carries a 0..1 float confidence; the column is an integer 0-1000 per P04's rule that
        no float is stored where a number can be owed. The conversion happens here, once."""
        now_ms = int(time.time() * 1000)
        for r in rows:
            conf = r["confidence"]
            r["confidence_int"] = int(round(conf * 1000)) if isinstance(conf, float) else int(conf)
            r.setdefault("first_seen_ms", now_ms)
            r.setdefault("last_seen_ms", now_ms)
        self.conn.executemany(
            "INSERT INTO wallet_labels (wallet,label,confidence,evidence_json,publishable,first_seen_ms,"
            "last_seen_ms) VALUES (?,?,?,?,?,?,?) ON CONFLICT (wallet,label) DO UPDATE SET"
            " confidence=excluded.confidence, evidence_json=excluded.evidence_json,"
            " publishable=excluded.publishable, last_seen_ms=excluded.last_seen_ms",
            [(r["wallet"], r["label"], r["confidence_int"], json.dumps(r.get("evidence") or {}, sort_keys=True,
                                                                       default=str), 1 if r["publishable"] else 0,
              r["first_seen_ms"], r["last_seen_ms"]) for r in rows])
        self.conn.executemany("INSERT INTO wallet_label_history (wallet,label,confidence,publishable,seen_ms)"
                              " VALUES (?,?,?,?,?)",
                              [(r["wallet"], r["label"], r["confidence_int"], 1 if r["publishable"] else 0,
                                now_ms) for r in rows])
        self.statement_count += 2
        return len(rows)

    # ------------------------------------------------------------- universe io
    def prune_inputs(self, now_ms: int) -> list[dict]:
        """Everything `Prune` needs, from stored rows: activity, alerts, watchlists, open orders.

        `volume_24h_micro` here is OUR tape's 24 h sum, not Gamma's `volume24hr`, and the difference is the
        point: the venue's number counts fills we never recorded, and a market we have never seen trade is not
        something to hold a socket subscription open for.
        """
        rows = self.conn.execute(
            "SELECT m.id, m.condition_id, COALESCE(s.last_fill_ms,0), COALESCE(s.volume_24h_micro,0),"
            " (SELECT COUNT(*) FROM signal_state st JOIN signals g ON g.rule_id = st.rule_id"
            "   WHERE g.condition_id = m.condition_id AND st.last_fired_ms > ?),"
            " (SELECT COUNT(*) FROM watchlist_items w WHERE w.market_id = m.id),"
            " (SELECT COUNT(*) FROM orders o WHERE o.market_id = m.id AND o.state IN ('live','partial')),"
            " COALESCE(s.last_book_change_ms,0) FROM markets m LEFT JOIN market_stats s"
            " ON s.condition_id = m.condition_id WHERE m.accepting_orders = 1 OR s.condition_id IS NOT NULL",
            (now_ms - 86_400_000,)).fetchall()
        return [{"id": r[0], "condition_id": r[1], "last_fill_ms": int(r[2] or 0),
                 "volume_24h_micro": int(r[3] or 0), "has_open_alert": bool(r[4]), "in_watchlist": bool(r[5]),
                 "has_open_order": bool(r[6]), "last_book_change_ms": int(r[7] or 0)} for r in rows]

    def yes_tokens(self) -> dict[str, dict]:
        """condition id -> {token, market, event, tick_micro}. `outcome_index` 0 is the first outcome, which is
        the YES leg on every two-outcome market we have read; the neg-risk sum is over those."""
        out = {}
        for cid, mid, tok, ev, tick in self.conn.execute(
                "SELECT m.condition_id, m.id, t.token_id, m.event_id, m.minimum_tick_size FROM markets m"
                " JOIN tokens t ON t.market_id = m.id WHERE t.outcome_index = 0 AND m.accepting_orders = 1"):
            out[str(cid)] = {"token": tok, "market": mid, "event": ev, "tick_micro": _tick_micro(tick)}
        return out

    def settled_records(self, since_ms: int, limit: int = 300) -> dict[str, dict]:
        """Per wallet, over markets we have seen RESOLVE: settled count, win rate, realised PnL, breadth.

        Realised PnL is integer: a winning share pays $1, so `pnl = shares - cost`, and a losing one is `-cost`.
        Both terms are already stored in micro-units, so there is no float and no re-derivation of the venue's
        payout anywhere in this function.
        """
        # `+ RESOLVED_SQL +` rather than a `%s` in the middle of an implicitly-concatenated literal: the
        # replacement lands on whichever fragment it is written next to, and the fragment after it silently
        # stops being part of the string. That is a syntax error here, which is the good outcome; in an f-string
        # it would have been a truncated query.
        rows = self.conn.execute(
            "SELECT f.wallet, f.condition_id, f.token_id, f.side, SUM(f.size_micro), SUM(f.usd_notional_micro),"
            " t.is_winner FROM tape_fills f JOIN markets m ON m.condition_id = f.condition_id"
            " JOIN tokens t ON t.token_id = f.token_id WHERE " + RESOLVED_SQL +
            " AND f.ts_ms >= ? AND f.wallet <> '' GROUP BY f.wallet, f.condition_id, f.token_id, f.side",
            (since_ms,)).fetchall()
        per_market: dict[tuple[str, str], dict] = {}
        for wallet, cid, tok, side, shares, cost, is_winner in rows:
            key = (wallet, str(cid))
            agg = per_market.setdefault(key, {"pnl": 0, "won": False, "shares": 0})
            shares, cost = int(shares or 0), int(cost or 0)
            if str(side) == "BUY":
                agg["shares"] += shares
                agg["pnl"] += (shares - cost) if is_winner else -cost
                agg["won"] = agg["won"] or bool(is_winner)
            else:
                agg["shares"] -= shares
                agg["pnl"] += (cost - shares) if is_winner else cost
                agg["won"] = agg["won"] or bool(is_winner)
        out: dict[str, dict] = {}
        for (wallet, _cid), agg in per_market.items():
            d = out.setdefault(wallet, {"settled_positions": 0, "wins": 0, "realized_pnl_micro": 0,
                                        "distinct_markets": 0})
            d["settled_positions"] += 1
            d["wins"] += 1 if agg["won"] else 0
            d["realized_pnl_micro"] += int(agg["pnl"])
        for w, d in out.items():
            d["win_rate"] = (d["wins"] / d["settled_positions"]) if d["settled_positions"] else 0.0
            d["distinct_markets"] = sum(1 for (wal, _c) in per_market if wal == w)
            if len(out) >= limit:
                break
        return out

    def early_entries(self, wallet: str, now_ms: int, *, move_pct: float = 0.15,
                      window_s: int = 6 * 3600) -> tuple[list[dict], dict[str, dict]]:
        """(hits, market context) for `insider_suspect`, from the tape alone.

        An "entry" is their first BUY in a market; the "move" is the first 15% mid change after it; the market
        must have since resolved. `pre_existing_position` is any earlier fill at all in that market, so a wallet
        that sold before buying is not counted as an early entrant. Anything we cannot see in stored rows is NOT counted as
        a hit — a missing ladder is not a thin market, and a missing earlier fill is not proof of no prior
        position. The classifier's own conjunction then has to clear a bar we did not inflate.
        """
        fills = [dict(zip(("condition_id", "ts_ms", "side", "price_micro", "size_micro", "usd_notional_micro"),
                          r)) for r in self.conn.execute(
            "SELECT condition_id, ts_ms, side, price_micro, size_micro, usd_notional_micro FROM tape_fills"
            " WHERE wallet = ? AND ts_ms >= ? ORDER BY condition_id, ts_ms",
            (wallet, now_ms - 30 * 86_400_000))]
        by_market: dict[str, list[dict]] = {}
        for f in fills:
            by_market.setdefault(str(f["condition_id"]), []).append(f)
        hits, ctx = [], {}
        for cid, seq in by_market.items():
            resolved = self.conn.execute("SELECT %s FROM markets m WHERE m.condition_id = ?" % RESOLVED_SQL,
                                         (cid,)).fetchone()
            if not resolved or not resolved[0]:
                continue                                   # unresolved: `resolved_his_way` is unknowable
            won = self.conn.execute("SELECT t.is_winner FROM tokens t JOIN markets m ON m.id = t.market_id"
                                    " WHERE m.condition_id=? AND t.outcome_index=0", (cid,)).fetchone()
            entry = next((f for f in seq if str(f["side"]) == "BUY"), None)
            if entry is None or won is None:
                continue
            start_px = entry["price_micro"]
            mover = next((f for f in seq if f["ts_ms"] > entry["ts_ms"]
                          and abs(f["price_micro"] - start_px) / max(1, start_px) >= move_pct), None)
            depth_row = self.conn.execute(
                "SELECT SUM(size_shares_micro * price_micro) / 1000000 FROM book_levels WHERE market_id = ("
                " SELECT id FROM markets WHERE condition_id=?)", (cid,)).fetchone()
            depth = int(depth_row[0] or 0) if depth_row else 0
            if mover is None or not depth:
                continue                                   # no recorded move or no ladder: not a claim to make
            ctx[cid] = {"depth_micro": depth}
            hits.append({"market": cid,
                         "lead_s": int((mover["ts_ms"] - entry["ts_ms"]) / 1000),
                         "pre_existing_position": any(f["ts_ms"] < entry["ts_ms"] for f in seq),
                         "resolved_his_way": bool(won[0])})
        return hits, ctx

    def markets_by_condition(self) -> dict[str, str]:
        return {str(r[0]): str(r[1]) for r in self.conn.execute(
            "SELECT condition_id, id FROM markets WHERE condition_id IS NOT NULL")}

    def open_position_users(self) -> list[dict]:
        """(user, market, condition, remaining size) for live/partial orders — the only users who should ever
        be told a market resolves soon.

        P07 owns the accurate position (lots, settlements, neg-risk). This is deliberately narrower: enough to
        avoid sending "resolves in 4h" to someone who never traded it, and a comment on the join rather than a
        second ledger implementation here.
        """
        return [dict(zip(("user", "market", "condition_id", "size_micro", "end_ts"), r)) for r in
                self.conn.execute("SELECT o.user_id, o.market_id, m.condition_id,"
                                  " SUM(o.size_micro - o.size_matched_micro), m.end_ts FROM orders o"
                                  " JOIN markets m ON m.id = o.market_id"
                                  " WHERE o.state IN ('live','partial') AND m.accepting_orders = 1"
                                  " GROUP BY o.user_id, o.market_id, m.condition_id, m.end_ts")]


# The Gamma fields whose movement a user can be affected by, and therefore the ones kept verbatim for the
# version diff. Derived from `universe.MetaVersion.tracked` at import so the policy has one owner: a field
# added to the diff list is stored here automatically, and a field that is NOT stored could never be compared.
META_FIELDS: tuple = U.MetaVersion.tracked

# P04 has no `markets.closed`. Resolution is a property of the outcomes: a market is resolved when one of its
# tokens has a winner. Inlining this in six places is how two of them end up disagreeing, so it is one constant.
RESOLVED_SQL = "EXISTS (SELECT 1 FROM tokens w WHERE w.market_id = m.id AND w.is_winner IS NOT NULL)"


def _order_size(value) -> float:
    """`orderMinSize`, or the venue's floor.

    Gamma omits the field on some rows, and the normaliser's `str(...)` turns that into the STRING "None" — a
    value that reaches `float()` here and raises `ValueError` two frames away from where it should have been
    reported. `markets.minimum_order_size` is NOT NULL with DEFAULT 5, so 5.0 is the documented answer to "we
    were not told", not an invention.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 5.0
    return v if v > 0 else 5.0


def _tick_micro(tick) -> int:
    """`0.001` -> 1000. Kept as a function because `minimum_tick_size` is NUMERIC in Postgres and TEXT from the
    venue, and the two must not become two different numbers."""
    try:
        return int(round(float(tick) * 10 ** 6))
    except (TypeError, ValueError):
        return 10_000                       # the venue's floor; a wrong tick is caught by P04's gate, a 0 is not


class Daemon:
    def __init__(self, cfg: Config, store: Store, fetcher: NET.Fetcher | None = None, ws_factory=None,
                 now=time.time) -> None:
        self.cfg, self.store = cfg, store
        self._now = now
        self.f = fetcher or NET.Fetcher(buckets={k: NET.Bucket(k, capacity=b.capacity, per_sec=b.per_sec)
                                                 for k, b in NET.BUCKETS.items()})
        self.tape = T.Tape(capacity=cfg.tape_capacity)
        self.bookset = B.BookSet(max_books=cfg.max_books, stale_ms=cfg.stale_book_ms)
        self.fresh = FR.Freshness()
        for name, kind, stale in (("ws.tape", "tape", 3_000), ("ws.book", "book", 3_000),
                                  ("data.trades", "tape", 10_000), ("gamma.markets", "metadata", 300_000),
                                  ("clob.book", "book", 30_000)):
            self.fresh.add(FR.Source(name, kind, stale))
        self.updown = T.UpDownLifecycle()
        self.prune = U.Prune()
        self.backfill = U.Backfill()
        self.discover = U.Discover()
        self.meta = U.MetaVersion()
        self.rules, self.rule_errors, self.cooldowns, self.owners = store.load_rules()
        self.engine = E.Engine(state=store.load_cooldown_state())
        self.classifier = LB.Classifier()
        self.events: list[dict] = []
        self.watch: dict[str, dict] = {}              # token id -> {condition, market, priority}
        self.cid_to_mid = store.markets_by_condition()
        self.ws = None
        self.ws_error = ""
        self.ws_dead_at: float | None = None
        self._stop = threading.Event()
        self.universe_error = ""      # last row the universe pass could not read, for the status endpoint
        # Injected for the same reason the fetcher is: the socket's failure modes are the part of this file that
        # matters most and is hardest to produce on demand, so a test has to be able to hand back a transport
        # that raises on connect. `Daemon.ws` is then the fake, and every branch below runs unchanged.
        self.ws_factory = ws_factory or (lambda on_message: W.WsClient(self.cfg.ws_url, on_message))
        self._dirty: set[str] = set()                  # condition ids with new fills, for the rollup pass
        # Per-token baselines for the two events that are *differences*, plus the error sinks. These are
        # instance state and not module state on purpose: two daemons in one process (a test, a backfill run)
        # must not inherit each other's "previous imbalance".
        self.prev_imbalance: dict[str, object] = {}
        self.last_mid: dict[str, int] = {}
        self.resolving: dict[str, bool] = {}
        self.event_errors: list[str] = []
        self.tape_from_ms, self.resume_note = self._resume()

    # ---------------------------------------------------------------- boot
    def _resume(self) -> tuple[int, str]:
        """Resume, don't restart from now (P04 D8). A consumer that starts from *now* opens a hole in the tape
        at exactly the moment the process was least healthy, and nothing downstream can see the hole."""
        cur = self.store.resume("data.trades")
        now_ms = int(self._now() * 1000)
        newest = int(cur["last_event_ms"] or 0)
        if not newest:
            return now_ms - RESUME_OVERLAP_S * 1000, "no cursor: starting from the last %ds" % RESUME_OVERLAP_S
        start = max(now_ms - LOOKBACK_CAP_S * 1000, newest - RESUME_OVERLAP_S * 1000)
        if now_ms - newest > LOOKBACK_CAP_S * 1000:
            return start, ("cursor is %ds old, capped at %ds: fills in between are NOT recovered"
                           % ((now_ms - newest), LOOKBACK_CAP_S))
        return start, "resumed from %ds ago (overlap %ds, dedupe handles the repeat)" % (
            (now_ms - start) // 1000, RESUME_OVERLAP_S)

    # ------------------------------------------------------------- universe
    def fetch_universe(self) -> list[dict]:
        """Backfill + discovery, returning the RAW Gamma rows that survived the universe policy. Kept apart from
        `apply_universe` so a test can drive the stored half with recorded payloads."""
        raw: list[dict] = []
        start_pages = self.backfill.pages
        while not self.backfill.done and self.backfill.pages - start_pages < self.cfg.universe_budget:
            r = self.f.get(GAMMA + "/markets", source="gamma.markets", params=self.backfill.params())
            if not r.ok:
                # The source is marked dead by the clock running out, not by this line: one 502 on a 40-page
                # backfill is a Tuesday, and a flag that flips on every transient gets muted inside a day.
                break
            # Liveness on the evidence, not on a hopeful connect: this source is a poll, so the poll is the
            # proof. Leaving it False would pin the composite at `down` forever, and an alarm that is always on
            # is an alarm nobody reads.
            self.fresh.sources["gamma.markets"].transport_alive = True
            self.fresh.note_event("gamma.markets", int(self._now() * 1000))
            raw.extend(self.backfill.feed(r.json if isinstance(r.json, list) else []))
        d = self.f.get(GAMMA + "/markets", source="gamma.markets", params=self.discover.params())
        if d.ok:
            raw.extend(self.discover.feed(d.json if isinstance(d.json, list) else []))
        return raw

    def apply_universe(self, raw_rows: list[dict]) -> dict:
        markets, dropped = [], 0
        by_condition = {str(r.get("conditionId") or ""): r for r in raw_rows}
        for row in raw_rows:
            try:
                markets.append(N.market_from_gamma(row))
            except (N.ShapeError, ArithmeticError, ValueError, TypeError) as e:
                # counted, never coerced: a refusal that is invisible is a bug. Same reasoning as the tape loop —
                # the venue's JSON is not a type system, and a row we cannot read must cost one market, not a pass.
                dropped += 1
                self.universe_error = repr(e)[:160]
        stored = self.store.stored_market_view([m["condition_id"] for m in markets])
        diffs: list[dict] = []
        for m in markets:
            old = stored.get(m["condition_id"])
            if old is None:
                continue                      # a market we have never stored has no history to diff against
            fresh_view = {k: v for k, v in by_condition.get(m["condition_id"], {}).items()
                          if k in META_FIELDS}
            for ch in self.meta.diff(old, fresh_view):
                diffs.append(dict(ch, market_id=m["id"]))
        new, changed, events_new = self.store.upsert_universe(markets, by_condition)
        self.store.meta_versions(diffs)
        self.cid_to_mid.update({m["condition_id"]: m["id"] for m in markets})
        expired = 0
        now_ms = int(self._now() * 1000)
        for m in markets:
            # An up/down bucket that has passed its own window is CLOSED as far as the UI is concerned, but its
            # rows stay: 8,640 markets a day per asset are the tape the leaderboard is built from.
            if m["is_updown"] and self.updown.should_prune(m["slug"], now_ms):
                expired += 1
                self.unwatch(m)
        self.rebuild_watch()
        return {"markets": len(markets), "new": new, "changed": changed, "events_new": events_new,
                "meta_versions": len(diffs), "shape_dropped": dropped, "updown_expired": expired,
                "backfill": self.backfill.snapshot(), "discovered": self.discover.seen_new,
                "overlap": self.discover.overlap, "tracked": len(self.watch),
                "backfill_done": self.backfill.done}

    def sync_universe(self) -> dict:
        return self.apply_universe(self.fetch_universe())

    def rebuild_watch(self) -> None:
        """Tracked = watchlist markets (priority 0) + markets with a live alert (1) + the activity-ranked rest
        (2). `Prune.rank` is the policy; this is only the I/O around it."""
        rows = self.store.prune_inputs(int(self._now() * 1000))
        keep = [r for r in rows if not self.prune.is_dead(r) or self.prune.should_wake(r)]
        keep = self.prune.rank(keep)
        by_cid = {r["condition_id"]: r for r in keep}
        toks: dict[str, dict] = {}
        for r in self.conn_execute("SELECT m.condition_id, t.token_id FROM markets m JOIN tokens t"
                                    " ON t.market_id = m.id WHERE m.accepting_orders = 1"):
            if r[0] in by_cid:
                prio = 0 if by_cid[r[0]]["in_watchlist"] else (1 if by_cid[r[0]]["has_open_alert"] else 2)
                toks[str(r[1])] = {"condition": str(r[0]), "market": self.cid_to_mid.get(str(r[0]), ""),
                                   "priority": prio}
        self.watch = toks
        for tok, meta in toks.items():
            self.bookset.watch(tok, priority=meta["priority"])
        self.bookset.enforce_cap()

    def conn_execute(self, sql: str, params: tuple = ()):
        self.store.statement_count += 1
        return self.store.conn.execute(sql, params)

    def unwatch(self, market: dict) -> None:
        for tok in market.get("tokens") or []:
            self.bookset.unwatch(tok)
            self.watch.pop(tok, None)

    # ------------------------------------------------------------------ tape
    def poll_tape(self) -> dict:
        now_ms = int(self._now() * 1000)
        since_s = max(0, self.tape_from_ms // 1000 - 5)
        until_s = now_ms // 1000 + FORWARD_SLACK_S
        fetched = added = written = 0
        for page in range(MAX_TAPE_PAGES):
            r = self.f.get(DATA + "/trades", source="data.trades",
                           params={"limit": TAPE_PAGE, "takerOnly": "true", "start": since_s, "end": until_s,
                                   "offset": page * TAPE_PAGE})
            if not r.ok:
                break
            rows = r.json if isinstance(r.json, list) else []
            fetched += len(rows)
            batch = []
            for row in rows:
                try:
                    fill = N.fill_from_rest(row)
                except (N.ShapeError, ArithmeticError, ValueError, TypeError) as e:
                    # Defence in depth, and the reason is a bug this phase actually shipped: `fill_micro` is
                    # total now (a non-number and an unrepresentable magnitude both arrive as ShapeError), but
                    # `Decimal("abc")` used to escape here as `InvalidOperation`, which `except ShapeError` does
                    # not catch, and one unreadable field in one row of a 500-row page stopped the tape, the
                    # volume statistics and every alert built on them. A loop that books other users' fills must
                    # not be one bad field away from exiting; catch wide, count, continue.
                    self.tape.refusals += 1
                    self.tape.last_refusal = repr(e)[:160]
                    continue
                if self.tape.add(fill):
                    batch.append(fill)
            added += len(batch)
            written += self.store.insert_fills(batch)
            for fill in batch:
                self.events.append(self.fill_event(fill))
                self._dirty.add(fill["market"])
            if rows:
                newest = max(int(rw.get("timestamp") or 0) for rw in rows) * 1000
                self.fresh.note_event("data.trades", newest)
            if len(rows) < TAPE_PAGE:
                break
        newest = self.tape.newest_ts_ms
        if newest:
            self.tape_from_ms = max(0, newest - RESUME_OVERLAP_S * 1000)
        self.store.save_cursor("data.trades", cursor={"from_ms": self.tape_from_ms, "fetched": fetched},
                               last_event_ms=newest or now_ms, state=self.fresh.composite(now_ms)["status"])
        rolled = self.store.rollups(sorted(self._dirty), now_ms - 86_400_000) if self._dirty else 0
        for cid in sorted(self._dirty):
            hist = self.store.recent_volume(cid, now_ms)
            if len(hist) >= 2:
                self.events.append({"type": "volume_bucket", "market": cid, "value_micro": hist[-1],
                                    "history_micro": hist[:-1]})
        self._dirty.clear()
        return {"fetched": fetched, "added": added, "written": written,
                # Reported on every pass, not only when non-zero: "0 refused" is the answer to "is the venue
                # sending us rows we cannot read?", and a counter nobody sees until it moves is a rumour.
                "refused": self.tape.refusals, "last_refusal": self.tape.last_refusal,
                "dups": self.tape.dups,
                "late": self.tape.late, "lag_ms": self.tape.lag_ms(now_ms), "rollup_rows": rolled,
                "from_ms": self.tape_from_ms}

    def fill_event(self, fill: dict) -> dict:
        """The engine's event shape, built in one place so both sources produce identical events. The median
        and sample come from the stored tape, which is what makes an alert reproducible after the fact."""
        st = self.store.market_stats(fill["market"], int(self._now() * 1000))
        return {"type": "fill", "market": fill["market"], "token_id": fill["token_id"],
                "wallet": fill.get("wallet") or "", "side": fill["side"],
                "usd_notional_micro": fill["usd_notional_micro"], "ts_ms": fill["ts_ms"],
                "market_median_fill_micro": st["median_micro"], "market_fill_sample": st["sample"],
                "watched_wallets": sorted(self.watched_wallets())[:500]}

    def watched_wallets(self) -> set[str]:
        # P07 owns the watchlist; until then a wallet is watched only if it has already traded something we
        # labelled, which keeps `watched_wallet` firing on evidence instead of on an empty table.
        return {r["wallet"] for r in self.store.conn.execute(
            "SELECT DISTINCT wallet FROM wallet_labels WHERE label IN ('whale','smart_money')")}

    # --------------------------------------------------------------- socket
    def connect_ws(self) -> None:
        if self.ws is not None:
            return
        try:
            # through the injected factory, so the failure paths here are testable without a socket; a hard
            # `W.WsClient(...)` turns every "what happens when the venue hangs up" case into a live test.
            self.ws = self.ws_factory(self.on_message)
            self.ws.connect()
        except Exception as e:  # noqa: BLE001 - a socket that will not open is a STATE, not an exception for main
            self.ws = None
            self.ws_error = repr(e)[:160]
            self.fresh.sources["ws.tape"].transport_alive = False
            self.fresh.sources["ws.book"].transport_alive = False
            return
        toks = list(self.watch.keys())[:B.BookSet.SUBS_PER_CONNECTION]
        if toks:
            self.ws.subscribe_market(toks)
        self.fresh.sources["ws.tape"].transport_alive = True
        self.fresh.sources["ws.book"].transport_alive = True
        if self.ws_dead_at is not None:
            self.ws.resumed = self.ws.resumed + 1
            self.ws_dead_at = None

    def ws_tick(self, max_wait: float | None = None) -> int:
        if self.ws is None:
            return 0
        try:
            return self.ws.poll(self.cfg.ws_wait_s if max_wait is None else max_wait)
        except NET.CircuitOpen:
            raise
        except Exception as e:  # noqa: BLE001 - same reason as connect: report it, do not propagate it
            self.ws_error = repr(e)[:160]
            self.ws_dead_at = time.monotonic()
            for name in ("ws.tape", "ws.book"):
                self.fresh.sources[name].transport_alive = False
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass
            self.ws = None
            return 0

    def on_message(self, msg: dict) -> None:
        """One frame in, state updated. Everything here is idempotent by construction because the REST poll may
        book the same fill a moment later, and because a resync can land on top of a delta we already applied."""
        now_ms = int(self._now() * 1000)
        # A frame arriving IS the proof the socket is up, so the flag is set here and not only in `connect_ws`:
        # a connection that was established but never delivers must not be reported as live, and one that
        # delivers after a silent gap must be reported alive again by the same rule.
        kind = msg.get("event_type") if isinstance(msg, dict) else None
        if kind in ("book", "price_change"):
            self.fresh.sources["ws.book"].transport_alive = True
        elif kind == "last_trade_price":
            self.fresh.sources["ws.tape"].transport_alive = True
        if kind == "book":
            try:
                snap = N.book_from_clob(msg, now_ms=now_ms)
            except (N.ShapeError, KeyError):
                return
            tok = snap["token_id"]
            bk = self.bookset.books.setdefault(tok, B.Book(tok))
            bk.apply_snapshot(snap, now_ms)
            self.fresh.note_event("ws.book", snap["ts_ms"])
            mid = self.cid_to_mid.get(tok and self.watch.get(tok, {}).get("condition", ""), "")
            if mid:
                self.store.replace_book(mid, {k: dict(v) for k, v in
                                              (("bids", bk.bids), ("asks", bk.asks))}, now_ms)
            self.note_book(bk, now_ms)
        elif kind == "price_change":
            for ch in N.price_changes(msg):
                bk = self.bookset.books.get(ch["token_id"])
                if bk is None:
                    continue
                res = bk.apply_delta(ch, now_ms)
                self.fresh.note_event("ws.book", ch.get("ts_ms") or now_ms)
                reason = "resync" if res == "resync" else bk.needs_resync(now_ms, self.cfg.stale_book_ms)
                if reason:
                    self.bookset.note_resync(ch["token_id"])
                    self.resync(ch["token_id"], reason)
                elif res == "applied":
                    self.note_book(bk, now_ms)
        elif kind == "last_trade_price":
            # The frame has no transaction hash, so it cannot BE a tape row — the tape's identity is the hash
            # tuple, and inventing a key is a permanent duplicate. The socket is the latency layer: it fires
            # alerts now, and REST writes the record.
            try:
                lt = N.last_trade(msg)
            except (N.ShapeError, KeyError, IndexError):
                return
            cid = self.watch.get(lt["token_id"], {}).get("condition", "") or lt.get("market", "")
            live = dict(lt, usd_notional_micro=lt["price_micro"] * lt["size_micro"] // 10 ** 6,
                        market=cid, wallet="", tx_hash="", outcome="", outcome_index=None)
            booked = not self.tape.add_live(live)
            self.fresh.note_event("ws.tape", lt["ts_ms"])
            if not booked:
                self.events.append(self.fill_event(dict(live, source="ws")))

    def resync(self, token_id: str, reason: str = "") -> None:
        """Re-snapshot one book. Never a loop over the whole set: during a venue hiccup that is how an ingest
        pays for its own outage with a rate limit, and the outage it is recovering from gets longer."""
        r = self.f.get(CLOB + "/book", source="clob.book", params={"token_id": token_id})
        if not r.ok:
            return
        now_ms = int(self._now() * 1000)
        try:
            snap = N.book_from_clob(r.json, now_ms=now_ms)
        except (N.ShapeError, KeyError):
            return
        bk = self.bookset.books.setdefault(token_id, B.Book(token_id))
        bk.apply_snapshot(snap, now_ms)
        # A successful REST snapshot is liveness for the REST book source and for nothing else. Marking
        # `ws.book` here instead would be the bug my own chaos harness had: the page reads the composite, so a
        # poll that advances the socket's clock hides an outage behind a healthy-looking number.
        self.fresh.sources["clob.book"].transport_alive = True
        self.fresh.note_event("clob.book", snap["ts_ms"])
        if reason:
            bk.resync_reasons.append("rest resync: %s" % reason[:120])
        cid = self.watch.get(token_id, {}).get("condition", "")
        mid = self.cid_to_mid.get(cid, "")
        if mid:
            self.store.replace_book(mid, {"bids": bk.bids, "asks": bk.asks}, now_ms)

    def note_book(self, bk: B.Book, now_ms: int) -> None:
        """Emit the two book-derived events. Both need a PREVIOUS value, so the first frame after a boot
        establishes a baseline and fires nothing — the alternative is an "imbalance flip" alert on startup."""
        cid = self.watch.get(bk.token_id, {}).get("condition", "")
        imb = bk.imbalance(DEPTH_CENTS)
        prev = self.prev_imbalance.get(bk.token_id, "__unset__")
        self.prev_imbalance[bk.token_id] = imb
        if prev != "__unset__" and imb is not None and prev is not None:
            self.events.append({"type": "book", "market": cid, "token_id": bk.token_id,
                                "imbalance": imb, "imbalance_prev": prev})
        mid = bk.mid_micro()          # a method, not a property: `depth_at`-style args are why
        if mid is not None:
            old = self.last_mid.get(bk.token_id)
            if old is not None:
                self.events.append({"type": "price_move", "market": cid, "token_id": bk.token_id,
                                    "old_micro": old, "new_micro": mid,
                                    "usd_depth_micro": sum(bk.depth_at(DEPTH_CENTS)) * mid // 10 ** 6,
                                    # a resolution moves a price to 0/1 and is not a market event; suppressing
                                    # here is what stops every resolution looking like a whale attack
                                    "resolution_event": bool(self.resolving.get(cid, False))})
            self.last_mid[bk.token_id] = mid
        # `negrisk_sum` and `market_clock` are NOT emitted here. They are per-pass, not per-frame: the first
        # needs every leg of an event to be present, and the second needs a join to `orders` that no single
        # book update changes. Emitting them per frame would produce ~15-33 events/s of identical alerts.
        # Both are emitted from `run_once`, below.

    def emit_negrisk(self, now_ms: int) -> None:
        """neg-risk events: the sum of YES mid prices across an event's markets should sit near 1.0.

        Skipped when any member has no book, because a partial sum is exactly the kind of number that produces
        a confident, wrong "divergence" alert.
        """
        groups: dict[str, list[tuple[str, int]]] = defaultdict(list)
        incomplete: set[str] = set()
        info_by_cid = self.store.yes_tokens()
        for cid, info in info_by_cid.items():
            ev = str(info.get("event") or "")
            if not ev:
                continue
            bk = self.bookset.books.get(info["token"])
            mid = bk.mid_micro() if bk is not None else None
            if mid is None:
                incomplete.add(ev)               # a partial sum is a confident wrong alert, so the group is out
                continue
            groups[ev].append((cid, mid))
        for ev, legs in groups.items():
            if ev in incomplete or len(legs) < 2:
                continue
            self.events.append({"type": "negrisk_sum", "event": ev,
                                "sum_micro": sum(m for _c, m in legs),
                                "prices_micro": [m for _c, m in legs],
                                "tick_micro": info_by_cid[legs[0][0]]["tick_micro"]})

    def emit_market_clock(self, now_ms: int) -> int:
        n = 0
        for row in self.store.open_position_users():
            end = _to_ms(row.get("end_ts"))
            if not end:
                continue
            self.events.append({"type": "market_clock", "market": str(row["condition_id"]),
                                "end_ts_ms": end, "user_open_position_micro": int(row["size_micro"] or 0)})
            n += 1
        return n

    # ------------------------------------------------------------- signals
    def evaluate(self) -> dict:
        if not self.rules:
            return {"evaluated": len(self.events), "fired": 0, "written": 0,
                    "suppressed": self.engine.suppressed, "rule_errors": self.rule_errors,
                    "note": "no enabled signal_rules rows"}
        events, self.events = self.events, []
        fired: list[E.Alert] = []
        now_ms = int(self._now() * 1000)
        for ev in events:
            try:
                fired.extend(self.engine.evaluate(self.rules, ev, now_ms))
            except Exception as e:  # noqa: BLE001 - one bad event must never stop the tape
                self.event_errors.append(repr(e)[:160])
        written = self.store.record_alerts(fired, self.cooldowns, self.owners)
        return {"evaluated": len(events), "fired": len(fired), "written": written,
                "suppressed": self.engine.suppressed, "rule_errors": self.rule_errors,
                "event_errors": self.event_errors[-5:]}

    # -------------------------------------------------------------- labels
    def labels(self) -> dict:
        """Six labels, each with the false-positive control that the phase requires, run over stored rows."""
        now_ms = int(self._now() * 1000)
        cids = sorted({c for c in self.cid_to_mid})[:self.cfg.max_books]
        if not cids:
            return {"labels": 0, "skipped": "no tracked markets yet"}
        rows: list[dict] = []
        for cid in cids:
            rows.extend(self.store.fills_for(cid, now_ms - 86_400_000, limit=500))
        if not rows:
            return {"labels": 0, "skipped": "no fills in the last 24h"}
        by_wallet: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            if r.get("wallet"):
                by_wallet[r["wallet"]].append(r)
        out: list[dict] = []
        settled = self.store.settled_records(now_ms - 90 * 86_400_000)
        market_fills = {cid: self.store.fills_for(cid, now_ms - 86_400_000, limit=2_000)
                        for cid in sorted({r["condition_id"] for r in rows})[:200]}
        for w, rs in by_wallet.items():
            span = {"fills": len(rs), "volume_micro": sum(r["usd_notional_micro"] for r in rs),
                    "first_seen_s": (rs[-1]["ts_ms"] // 1000) if rs else 0,
                    "trade_count": len(rs), "distinct_markets": len({r["condition_id"] for r in rs})}
            labs = [self.classifier.wash_like(rs), self.classifier.new_wallet(span, int(now_ms / 1000))]
            # `whale` is per fill against the MARKET's own distribution, not against a global one: a $20k fill
            # is nothing on the Super Bowl and everything on a regional election.
            for r in rs:
                labs.append(self.classifier.whale(r, market_fills.get(r["condition_id"], [])))
            if w in settled:
                labs.append(self.classifier.smart_money(settled[w]))
                hits, ctx = self.store.early_entries(w, now_ms)
                if hits:
                    labs.append(self.classifier.insider_suspect({"early_entries": hits}, ctx))
            for lab in [x for x in labs if x is not None]:
                out.append(lab.as_row(w))
        # A cluster is a property of a GROUP, and `wallet_labels` is keyed per wallet, so each member gets the
        # row (with the same evidence). Writing one row for an arbitrary member would make the label appear and
        # disappear depending on sort order.
        for c in self.classifier.cluster(rows):
            ev = dict(c.evidence)
            for w in ev.get("wallets") or []:
                out.append(c.as_row(str(w)))
        written = self.store.save_labels(out) if out else 0
        return {"labels": written, "candidate_wallets": len(by_wallet),
                "wash_reported_to_ops": sum(1 for r in out if r["label"] == "wash_like")}

    # ------------------------------------------------------------------- run
    def run_once(self) -> dict:
        out = {"resume": self.resume_note}
        try:
            out["universe"] = self.sync_universe()
        except Exception as e:  # noqa: BLE001 - the universe can fail and the tape must keep running
            out["universe_error"] = repr(e)[:160]
        out["tape"] = self.poll_tape()
        self.emit_negrisk(int(self._now() * 1000))
        out["market_clock_events"] = self.emit_market_clock(int(self._now() * 1000))
        try:
            self.connect_ws()
            if self.ws is not None:
                t0 = time.monotonic()
                while time.monotonic() - t0 < self.cfg.ws_burst_s:
                    self.ws_tick(0.25)
        except NET.CircuitOpen as e:
            out["ws_circuit_open"] = str(e)[:120]
        out["signals"] = self.evaluate()
        return out

    def run_forever(self) -> dict:
        self._stop.clear()
        passes = 0
        while not self._stop.is_set():
            t0 = time.monotonic()
            self.run_once()
            passes += 1
            if passes % self.cfg.label_every_passes == 0:
                self.labels()
            while time.monotonic() - t0 < max(self.cfg.tape_poll_s, 0.5) and not self._stop.is_set():
                self.ws_tick(0.2)
                if self.ws is None:
                    self.connect_ws()
                    break
        return {"passes": passes}

    def stop(self) -> None:
        self._stop.set()
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001 - we are already shutting down
                pass

    def status(self) -> dict:
        """What the API will serve as the freshness badge and what `make p05-gate` reads. No DSN, no venue
        payloads, no user addresses: a status blob is a public surface in most deployments."""
        now_ms = int(self._now() * 1000)
        comp = self.fresh.composite(now_ms, now_mono=time.monotonic())
        return {"freshness": {"status": comp["status"], "sources": comp["sources"],
                              "reason": comp["worst_reason"]},
                "pageable": self.fresh.pageable(now_ms, now_mono=time.monotonic()),
                "tape": {"rows": len(self.tape.rows), "dups": self.tape.dups, "late": self.tape.late,
                         "live_seen": self.tape.live_seen, "live_suppressed": self.tape.live_suppressed,
                         "lag_ms": self.tape.lag_ms(now_ms)},
                "books": {"tracked": len(self.bookset.books), "dropped": len(self.bookset.dropped),
                          "resyncs": sum(b.resyncs for b in self.bookset.books.values()),
                          "gap_detections": sum(b.gap_detections for b in self.bookset.books.values()),
                          "budget_bytes": B.BookSet.memory_bytes(len(self.bookset.books))},
                "socket": {"connected": self.ws is not None, "frames": getattr(self.ws, "frames", 0),
                           "data_age_s": self.ws.data_age_s() if self.ws else None,
                           "handler_errors": getattr(self.ws, "handler_errors", [])[:3],
                           "last_error": self.ws_error or None},
                "universe": {"markets": len(self.cid_to_mid), "watching": len(self.watch),
                             "backfill_done": self.backfill.done, "backfill_rows": self.backfill.rows,
                             "discovered": self.discover.seen_new},
                "signals": {"rules": len(self.rules), "fired": self.engine.fired,
                            "suppressed": self.engine.suppressed, "rule_errors": self.rule_errors[:5],
                            "event_errors": self.event_errors[-3:]},
                "fetcher": {"sends": self.f.sends, "retries": self.f.retry_count,
                            "open_breakers": sorted(self.f.breakers)}}


def _to_ms(value) -> int:
    """Gamma's `endDate` is an ISO string; rollups and cursors are epoch ms. Accept both and refuse a float,
    because a 0 here silently disables `resolution_imminent` for every market."""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        v = int(value)
        return v if v > 10 ** 11 else v * 1000
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ingest", description="Openout market-data ingest")
    ap.add_argument("--once", action="store_true", help="one pass, then print status JSON")
    ap.add_argument("--db", default=None, help="sqlite path (default $PGM_DB_PATH or var/polygm.db)")
    ap.add_argument("--status", action="store_true", help="with --once: print status and exit 0")
    args = ap.parse_args(argv)

    env = dict(os.environ)
    if args.db:
        env["PGM_DB_PATH"] = args.db
    cfg = Config.from_env(env)
    if not Path(cfg.db_path).exists():
        print("no database at %s — run `make migrate` first" % cfg.db_path, file=sys.stderr)
        return 2
    store = Store.open(cfg.db_path)
    missing = store.ready()
    if missing:
        print("schema is missing %s — the ingest migration has not been applied" % ", ".join(missing),
              file=sys.stderr)
        return 2
    d = Daemon(cfg, store)
    if args.once:
        d.run_once()
        print(json.dumps(d.status(), indent=2, default=str))
        d.stop()
        return 0

    def bye(signum, _frame):
        print("ingest: got %s, finishing the pass" % signal.Signals(signum).name, file=sys.stderr)
        d.stop()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, bye)
    print(json.dumps(d.run_forever()))
    d.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
