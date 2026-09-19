#!/usr/bin/env python3
"""The executor's database access. One class, one engine surface: SQLite-shaped statements in the
intersection of SQLite and Postgres (`?` placeholders, `ON CONFLICT DO UPDATE`, no `RETURNING` on a path
that must work on both), exactly as in `services/ingest/main.py` — so "passes on the dev engine" and "works
on the deployment engine" are the same claim, and `db/migrations-sqlite/` is the generated proof.

Three invariants live here rather than in the callers, because every caller would get one of them wrong
eventually:

1. **`book_fill` is the only way a fill enters the ledger.** The venue WebSocket, the REST poll and the
   reconciler all call it, and its idempotency is structural: the fill row is inserted with
   `ON CONFLICT DO NOTHING` on `fills_dedupe_ix`, and the cash row is keyed by the venue's own trade
   identity. A re-run therefore changes nothing at all — not "changes little". The alternative, "check then
   insert", is a race between the check and the insert with real money on the other side.
2. **A claim is a lease, not a flag.** `claim_intents` moves rows `queued -> submitting` with a
   `lease_until_ms`. A crashed executor's claims expire and become claimable again; a crashed executor that
   leaves rows permanently `submitting` is a system where the only fix is a manual UPDATE at 3am, which is
   to say no fix.
3. **Nothing here decides whether an order is safe.** This file can write `orders`, `fills`, `cash_ledger`
   and the lifecycle trail; it cannot approve anything. The risk decisions are made above it, and the
   lifecycle rows it appends say which checks ran, so an order without a `preflight` row is visible as one.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from polygm_core.automation import facts as _facts
from polygm_core.config.flags import FlagStore
from polygm_core.ledger.ledger import VENUE_ORDER_STATUS, venue_status_to_state
from polygm_core.wallets import lifecycle as wl
from polygm_core.money.cents import SCALE, fmt_usdc, notional_floor

KILL_POLL_MS = 250                    # how stale a kill-switch read may be before we look again
CLAIM_LEASE_MS = 45_000               # one intent's worst legitimate execution time; then it is re-claimable
UNCERTAIN_BLOCK_MS = 0                # 0 = never auto-release; only reconciliation unblocks an intent

WANT_TABLES = ("order_intents", "orders", "fills", "cash_ledger", "position_lots", "builder_attribution",
               "order_lifecycle", "order_attempts", "venue_fees", "reconcile_cursors", "reconcile_actions",
               "reconcile_open", "wallets", "wallet_events", "allowances", "deposits", "withdrawals",
               "risk_blocklists", "loss_halts", "rate_counters", "kill_switch_state", "feature_flags",
               "book_levels", "markets", "users", "automation_runs", "copy_events", "chain_events",
               "builder_attribution_measures", "builder_revenue_daily", "flag_audit_p06", "idempotency_keys")


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class IntentRow:
    id: str
    user_id: str
    market_id: str
    token_id: str
    side: str
    price_micro: int
    size_micro: int
    notional_micro: int
    state: str
    idempotency_key: str
    venue_order_id: str
    client_order_hash: str
    order_type: str = "GTC"
    expiration_ts: int = 0
    builder_bps: int = 0
    fee_rate_bps: int = 0
    audience: str = "user"
    max_slippage_bps: int = 0
    rule_id: str = ""

    @property
    def is_automation(self) -> bool:
        return self.audience == "automation"


class Store:
    def __init__(self, conn: sqlite3.Connection, *, path: str = ":memory:") -> None:
        self.conn = conn
        self.path = path
        self.statement_count = 0
        self._kill_cache: tuple[int, bool] = (0, False)
        self.flags = FlagStore(conn)
        self.kill_poll_ms = int(os.environ.get("PGM_KILL_POLL_MS", KILL_POLL_MS))

    @classmethod
    def open(cls, path: str) -> "Store":
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            if not Path(path).exists():
                raise SystemExit("no database at %s — run `make migrate` first" % path)
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")      # the API reads while we write
        conn.execute("PRAGMA foreign_keys=ON")
        return cls(conn, path=path)

    def close(self) -> None:
        self.conn.close()

    def ready(self) -> list[str]:
        have = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return [t for t in WANT_TABLES if t not in have]

    # --------------------------------------------------------------------- config / global stops
    def current_flags(self):
        return self.flags.current()

    def kill_switch_engaged(self, *, at_ms: int | None = None, force: bool = False) -> bool:
        """Newest row wins, exactly as the API reads it, and the cache TTL is the only thing standing
        between a kill switch and our submit loop. 250ms is not a performance number: D4's budget is 1s
        end to end, and the drill (`tools/p06-drill.py`) measures the real propagation.

        A read failure says *engaged*. A kill switch whose error path is "carry on trading" is a kill switch
        that only works when the database is healthy.
        """
        t = at_ms if at_ms is not None else now_ms()
        if not force and t - self._kill_cache[0] < self.kill_poll_ms:
            return self._kill_cache[1]
        try:
            row = self.conn.execute("SELECT engaged FROM kill_switch_state ORDER BY at_ms DESC, id DESC "
                                    "LIMIT 1").fetchone()
            engaged = bool(row and row[0])
        except Exception:                                   # noqa: BLE001 - fail closed, whatever broke
            engaged = True
        self._kill_cache = (t, engaged)
        return engaged

    def note_flag_change(self, key: str, old, new, *, actor: str, reason: str, at: int | None = None) -> None:
        """D8: a limit change is an audited config change, and this phase's audit table is append-only, so
        an empty reason cannot be written at all (the CHECK would allow '' — the service does not)."""
        if not str(reason or "").strip():
            raise ValueError("a limit change without a reason is refused, not logged")
        self.execute("INSERT INTO flag_audit_p06 (key, old_value, new_value, actor, reason, at_ms) "
                     "VALUES (?,?,?,?,?,?)",
                     (key, None if old is None else str(old), None if new is None else str(new), actor,
                      str(reason).strip()[:400], at or now_ms()))

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        self.statement_count += 1
        return self.conn.execute(sql, params)

    def _begin(self) -> bool:
        outer = self.conn.in_transaction
        if not outer:
            self.conn.execute("BEGIN IMMEDIATE")
        return outer

    def _commit(self, outer: bool) -> None:
        if not outer:
            self.conn.execute("COMMIT")

    def _rollback(self, outer: bool) -> None:
        if not outer:
            self.conn.execute("ROLLBACK")

    # --------------------------------------------------------------------- the queue (D2's input)
    def claim_intents(self, *, limit: int, at: int | None = None,
                      kill_switch: bool = False) -> list[IntentRow]:
        """Take the intents this process may execute. `kill_switch=True` claims nothing and instead flips
        queued rows to rejected with RISK_HALT, because a queue that keeps filling while trading is
        disabled is a queue that will dump 400 orders the moment the switch is released."""
        t = at or now_ms()
        if kill_switch:
            cur = self.execute("UPDATE order_intents SET state='rejected', risk_code='RISK_HALT', "
                               "updated_ms=? WHERE state='queued'", (t,))
            return []
        outer = self._begin()
        try:
            rows = self.conn.execute(
                "SELECT id FROM order_intents WHERE state='queued' AND updated_ms <= ? "
                "ORDER BY created_ms LIMIT ?", (t, limit)).fetchall()
            ids = [r[0] for r in rows]
            if ids:
                ph = ",".join("?" * len(ids))
                # updated_ms doubles as the lease clock: `queued` rows whose lease expired are re-claimable,
                # which is what "the crashed executor's work picks back up" means without a second table.
                self.conn.execute("UPDATE order_intents SET state='submitting', updated_ms=? WHERE id IN (%s)"
                                  % ph, (t, *ids))
            self._commit(outer)
        except Exception:
            self._rollback(outer)
            raise
        out: list[IntentRow] = []
        for iid in ids:
            it = self.load_intent(iid)
            if it is not None:
                out.append(it)
        return out

    def load_intent(self, intent_id: str) -> IntentRow | None:
        r = self.conn.execute(
            "SELECT i.id,i.user_id,i.market_id,i.token_id,i.side,i.price_micro,i.size_micro,i.notional_micro,"
            "i.state,i.idempotency_key,COALESCE(i.venue_order_id,''),COALESCE(i.client_order_hash,''),"
            "COALESCE(d.order_type,'GTC'),COALESCE(d.expiration_ts,0),COALESCE(d.builder_bps,0),"
            "COALESCE(d.fee_rate_bps,0),COALESCE(d.audience,'user'),COALESCE(d.max_slippage_bps,0),"
            "COALESCE(d.rule_id,'') "
            "FROM order_intents i LEFT JOIN order_directives d ON d.intent_id = i.id WHERE i.id=?",
            (intent_id,)).fetchone()
        if r is None:
            return None
        return IntentRow(*r[:12], order_type=r[12], expiration_ts=r[13] or 0, builder_bps=r[14] or 0,
                         fee_rate_bps=r[15] or 0, audience=r[16] or "user", max_slippage_bps=r[17] or 0,
                         rule_id=r[18] or "")

    def set_intent_state(self, intent_id: str, state: str, *, at: int | None = None, risk_code: str = "",
                         venue_order_id: str = "", client_order_hash: str = "") -> None:
        self.execute("UPDATE order_intents SET state=?, risk_code=COALESCE(NULLIF(?,''),risk_code),"
                     "venue_order_id=COALESCE(NULLIF(?,''),venue_order_id),"
                     "client_order_hash=COALESCE(NULLIF(?,''),client_order_hash),updated_ms=? "
                     "WHERE id=?",
                     (state, risk_code, venue_order_id, client_order_hash, at or now_ms(), intent_id))

    def requeue_expired_claims(self, *, at: int | None = None) -> int:
        """Leases that expired go back to `queued` — EXCEPT `uncertain`, which must never be requeued
        automatically: a requeued uncertain intent is a re-POST, and a re-POST is the duplicate order."""
        t = at or now_ms()
        cur = self.execute("UPDATE order_intents SET state='queued', updated_ms=? WHERE state='submitting' "
                           "AND updated_ms < ?", (t - CLAIM_LEASE_MS, t))
        return cur.rowcount or 0

    def intents_in_state(self, state: str, *, older_than_ms: int, at: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id,user_id,token_id,side,price_micro,size_micro,COALESCE(client_order_hash,''),"
            "COALESCE(venue_order_id,''),updated_ms FROM order_intents WHERE state=? AND updated_ms < ? "
            "ORDER BY updated_ms", (state, at - older_than_ms)).fetchall()
        keys = ("id", "user_id", "token_id", "side", "price_micro", "size_micro", "client_order_hash",
                "venue_order_id", "updated_ms")
        return [dict(zip(keys, r)) for r in rows]

    def enqueue_intent(self, *, user_id: str, market_id: str, token_id: str, side: str, price_micro: int,
                       size_micro: int, idempotency_key: str, order_type: str = "GTC",
                       audience: str = "automation", rule_id: str = "", config_id: str = "",
                       builder_bps: int = 0, fee_rate_bps: int = 0, max_slippage_bps: int = 0,
                       expiration_ts: int = 0, at: int | None = None) -> dict:
        """Queue an order for execution, from copy or from automation.

        There is deliberately no path from `copy` or `automation` to the venue that skips this function: both
        produce *intents*, and the executor's pre-flight is the only consumer of intents. That is the whole
        answer to "does automation go through the same risk gate?" — it has no other door. The idempotency
        key is derived from the *cause* (which fill, which rule, which bucket), so a replay of the trigger
        cannot create a second order (rule 6).
        """
        t = at or now_ms()
        notional = notional_floor(size_micro, price_micro)
        iid = "i-" + hashlib.sha256(("%s|%s" % (user_id, idempotency_key)).encode()).hexdigest()[:20]
        cur = self.execute("INSERT INTO order_intents (id,user_id,market_id,token_id,side,price_micro,"
                           "size_micro,notional_micro,state,idempotency_key,created_ms,updated_ms) "
                           "VALUES (?,?,?,?,?,?,?,?,'queued',?,?,?) ON CONFLICT(user_id,idempotency_key) "
                           "DO NOTHING",
                           (iid, user_id, market_id, token_id, side, price_micro, size_micro, notional,
                            idempotency_key, t, t))
        if (cur.rowcount or 0) == 0:
            existing = self.conn.execute("SELECT id, state FROM order_intents WHERE user_id=? AND "
                                         "idempotency_key=?", (user_id, idempotency_key)).fetchone()
            return {"created": False, "intent_id": existing[0], "state": existing[1],
                    "reason": "the same cause has already been queued; replay is a no-op"}
        self.execute("INSERT INTO order_directives (intent_id,order_type,expiration_ts,builder_bps,"
                     "fee_rate_bps,audience,rule_id,config_id,max_slippage_bps,all_in_limit_micro,"
                     "created_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (iid, order_type, expiration_ts, builder_bps, fee_rate_bps, audience, rule_id, config_id,
                      max_slippage_bps, notional * 2, t))
        return {"created": True, "intent_id": iid, "state": "queued", "notional_micro": notional}

    # ------------------------------------------------------------------- lifecycle trail (D7 writes)
    LIFECYCLE_WORKING = ("draft", "preflight", "signing", "submitted", "unknown")

    def lifecycle(self, *, intent_id: str, order_id: str, user_id: str, state: str, reason: str,
                  source: str, at: int | None = None) -> None:
        if state not in ("draft", "preflight", "signing", "submitted", "live", "partial", "filled",
                         "cancelled", "rejected", "expired", "unknown", "reconciled"):
            raise ValueError("BAD_LIFECYCLE_STATE: %s" % state)
        if source not in ("venue_ws", "venue_rest", "reconciler", "user", "system"):
            raise ValueError("BAD_LIFECYCLE_SOURCE: %s" % source)
        # "show as working" is written WITH the state so the read path never has to guess. Guessing is how
        # an order we cannot see the answer for renders as failed, which is how a user places a second one.
        self.execute("INSERT INTO order_lifecycle (order_id,intent_id,user_id,state,reason,source,"
                     "show_as_working,at_ms) VALUES (?,?,?,?,?,?,?,?)",
                     (order_id or "", intent_id, user_id, state, reason[:400], source,
                      bool(state in self.LIFECYCLE_WORKING), at or now_ms()))

    def user_visible_state(self, intent_id: str, *, at: int | None = None) -> dict:
        """What Activity shows for this intent, derived from the trail. `working=True` means the UI must not
        show "failed" or a retry button — the only user-facing consequence of the UNCERTAIN design."""
        r = self.conn.execute("SELECT state,reason,show_as_working,at_ms,source FROM order_lifecycle "
                              "WHERE intent_id=? ORDER BY at_ms DESC, id DESC LIMIT 1", (intent_id,)).fetchone()
        if r is None:
            return {"state": "unknown", "reason": "no lifecycle record", "working": True, "at_ms": 0,
                    "source": "system"}
        return {"state": r[0], "reason": r[1], "working": bool(r[2]), "at_ms": r[3], "source": r[4]}

    # ------------------------------------------------------------------- submit-side writes (D2/D8)
    def record_attempt(self, *, client_order_hash: str, intent: IntentRow, signature_type: int,
                       payload_digest: str, at: int) -> None:
        """Written AFTER signing and BEFORE the POST. That ordering is the entire post-crash story: if this
        row exists, we may have sent the order; if it does not, we definitely did not.
        """
        self.execute("INSERT INTO order_attempts (client_order_hash,intent_id,user_id,token_id,side,"
                     "price_micro,size_micro,order_type,expiration_ts,signature_type,builder_code,"
                     "payload_digest,signed_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(client_order_hash) DO UPDATE SET signed_ms=excluded.signed_ms",
                     (client_order_hash, intent.id, intent.user_id, intent.token_id, intent.side,
                      intent.price_micro, intent.size_micro, intent.order_type, intent.expiration_ts,
                      signature_type, "0x" + "0" * 64, payload_digest, at))

    def mark_submitted(self, *, intent: IntentRow, order_id: str, client_order_hash: str, at: int,
                       acknowledged: bool = True, builder_bps: int = 0) -> None:
        outer = self._begin()
        try:
            self.conn.execute(
                "INSERT INTO orders (id,intent_id,user_id,token_id,market_id,side,price_micro,size_micro,"
                "size_matched_micro,state,acknowledged,builder_code,metadata,expiration_ts,placed_ms,"
                "updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET state=excluded.state, acknowledged=excluded.acknowledged,"
                "updated_ms=excluded.updated_ms",
                (order_id, intent.id, intent.user_id, intent.token_id, intent.market_id, intent.side,
                 intent.price_micro, intent.size_micro, 0, "live",
                 bool(acknowledged), "0x" + "0" * 64, "{}", intent.expiration_ts, at, at))
            self.conn.execute("UPDATE order_intents SET state='submitted', venue_order_id=?, "
                              "client_order_hash=?, updated_ms=? WHERE id=?",
                              (order_id, client_order_hash, at, intent.id))
            self.conn.execute("UPDATE order_attempts SET submitted_ms=?, ack_ms=? WHERE client_order_hash=?",
                              (at, at if acknowledged else None, client_order_hash))
            self.conn.execute("INSERT INTO builder_attribution (intent_id,user_id,builder_code,"
                              "fee_bps_expected,fee_micro_expected,market_id,token_id,placed_ms) "
                              "VALUES (?,?,?,?,?,?,?,?)",
                              (intent.id, intent.user_id, "0x" + "0" * 64, builder_bps,
                               notional_floor(intent.size_micro, intent.price_micro) * builder_bps // 10_000,
                               intent.market_id, intent.token_id, at))
            self._commit(outer)
        except Exception:
            self._rollback(outer)
            raise

    def record_fee_estimate(self, *, order_id: str, intent: IntentRow, platform_micro: int,
                            builder_micro: int, fee_rate_bps: int, builder_bps: int, at: int) -> None:
        self.execute("INSERT INTO venue_fees (order_id,intent_id,fee_rate_bps,builder_bps,"
                     "est_platform_micro,est_builder_micro,at_ms) VALUES (?,?,?,?,?,?,?) "
                     "ON CONFLICT(order_id) DO UPDATE SET est_platform_micro=excluded.est_platform_micro,"
                     "est_builder_micro=excluded.est_builder_micro",
                     (order_id, intent.id, fee_rate_bps, builder_bps, platform_micro, builder_micro, at))

    def record_fee_actual(self, *, order_id: str, platform_micro: int, builder_micro: int, at: int) -> dict:
        """D2's "record the post-fill delta and expose an estimate-accuracy metric". The delta is stored,
        not just logged, so the metric is a query and a trend rather than an anecdote."""
        r = self.conn.execute("SELECT est_platform_micro + est_builder_micro FROM venue_fees WHERE order_id=?",
                              (order_id,)).fetchone()
        est = int(r[0]) if r else 0
        actual = platform_micro + builder_micro
        delta = actual - est
        self.execute("UPDATE venue_fees SET actual_platform_micro=?, actual_builder_micro=?, delta_micro=? "
                     "WHERE order_id=?", (platform_micro, builder_micro, delta, order_id))
        return {"estimate_micro": est, "actual_micro": actual, "delta_micro": delta,
                "accuracy_bps": (delta * 10_000 // est) if est else (0 if actual == 0 else 10_000)}

    def fee_accuracy_summary(self, *, at: int, window_ms: int = 86_400_000) -> dict:
        rows = self.conn.execute("SELECT est_platform_micro + est_builder_micro, "
                                 "COALESCE(actual_platform_micro,0) + COALESCE(actual_builder_micro,0), "
                                 "COALESCE(delta_micro,0) FROM venue_fees WHERE at_ms > ?",
                                 (at - window_ms,)).fetchall()
        booked = [r for r in rows if r[1]]
        return {"orders": len(rows), "with_actuals": len(booked),
                "mean_abs_delta_micro": (sum(abs(r[2]) for r in booked) // len(booked)) if booked else 0,
                "under_estimates": sum(1 for r in booked if r[2] > 0),
                "over_estimates": sum(1 for r in booked if r[2] < 0),
                "note": "under_estimates is the number to watch: it means a user's all-in cost exceeded the "
                        "quote they were shown"}

    # ---------------------------------------------------------------------------- fills + ledger
    def fills_booked_micro(self, order_id: str) -> int:
        r = self.conn.execute("SELECT COALESCE(SUM(size_micro),0) FROM fills WHERE order_id=?",
                              (order_id,)).fetchone()
        return int(r[0] or 0)

    def book_fill(self, *, order_id: str, intent_id: str, user_id: str, token_id: str, market_id: str,
                  side: str, price_micro: int, size_micro: int, fee_micro: int, trade_id: str,
                  exchange_ts: int, maker: bool, source: str, at: int | None = None) -> dict:
        """The single entry point for "money actually moved". Returns booked=False when the venue fact was
        already known, which is the normal case for the third observer of the same trade, not an error."""
        t = at or now_ms()
        if side not in ("BUY", "SELL"):
            raise ValueError("BAD_SIDE")
        if size_micro <= 0 or price_micro <= 0:
            raise ValueError("BAD_FILL_SHAPE")
        if source not in ("ws", "rest_poll", "reconcile", "on_chain"):
            raise ValueError("BAD_FILL_SOURCE: %s" % source)
        notional = notional_floor(size_micro, price_micro)
        # Deterministic id for the cash row, built from the VENUE's identity for the trade, so replaying
        # the same trade twice cannot produce two credits even if the fill row was somehow inserted twice.
        cash_ref = "%s|%s|%s" % (trade_id or ("%s:%s:%s" % (order_id, exchange_ts, size_micro)),
                                 price_micro, size_micro)
        outer = self._begin()
        try:
            cur = self.conn.execute(
                "INSERT INTO fills (order_id,trade_id,taker_order_id,side,price_micro,size_micro,"
                "notional_micro,fee_micro,maker,exchange_ts,ingest_ms,source,raw_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (order_id, trade_id or None, trade_id or None, side, price_micro, size_micro, notional,
                 fee_micro, bool(maker), exchange_ts, t, source,
                 json.dumps({"dedupe": cash_ref}, separators=(",", ":"))))
            if (cur.rowcount or 0) == 0:
                self._rollback(outer)
                return {"booked": False, "reason": "duplicate venue trade", "trade_id": trade_id}
            fill_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            # BUY: cash out = notional + fee.  SELL: cash in = notional - fee. Fees never go negative, and
            # a SELL whose fee exceeds proceeds is a venue bug we refuse to absorb silently.
            if side == "BUY":
                amount = -(notional + fee_micro)
                kind = "buy"
            else:
                amount = notional - fee_micro
                kind = "sell_fill"
                if amount <= 0:
                    self._rollback(outer)
                    raise RuntimeError("FEE_EXCEEDS_PROCEEDS: fill %s priced %s with fee %s"
                                       % (cash_ref, fmt_usdc(notional), fmt_usdc(fee_micro)))
            if amount:
                # The reason text is user-facing (Activity), so it states the fill, not the internal key.
                reason = "%s %s sh at %s" % (side, fmt_usdc(size_micro), fmt_usdc(price_micro))
                self.conn.execute("INSERT INTO cash_ledger (user_id,kind,amount_micro,ref_table,ref_id,"
                                  "created_ms,reason) VALUES (?,?,?,?,?,?,?)",
                                  (user_id, kind, amount, "fills", cash_ref, t, reason))
            if side == "BUY":
                self.conn.execute("INSERT INTO position_lots (user_id,token_id,market_id,shares_open_micro,"
                                  "basis_micro,opened_ms,source) VALUES (?,?,?,?,?,?,?)",
                                  (user_id, token_id, market_id, size_micro, notional + fee_micro, t, "fill"))
            else:
                self._reduce_lots(user_id=user_id, token_id=token_id, shares_micro=size_micro, at=t)
            # Evaluate the new matched total FIRST. Reading the row again inside the UPDATE's argument list
            # returns the pre-update value, and an order whose fill count had just reached its size stayed
            # `partial` forever — a book that cannot close itself. (Found by the ledger tests, not by eye.)
            matched = self.fills_booked_micro(order_id)
            self.conn.execute("UPDATE orders SET size_matched_micro=?, state=?, updated_ms=? WHERE id=?",
                              (matched, self._order_state_after_fill(order_id, matched=matched), t, order_id))
            self.conn.execute("UPDATE balances SET usdc_available_micro=usdc_available_micro+?, "
                              "version=version+1, reconcile_ms=? WHERE user_id=?", (amount, t, user_id))
            self._commit(outer)
        except RuntimeError:
            raise
        except Exception as e:
            self._rollback(outer)
            raise RuntimeError("book_fill failed for %s: %s" % (cash_ref, e)) from e
        return {"booked": True, "fill_id": fill_id, "notional_micro": notional, "fee_micro": fee_micro,
                "cash_amount_micro": amount, "trade_id": trade_id}

    def _order_state_after_fill(self, order_id: str, *, matched: int) -> str:
        r = self.conn.execute("SELECT size_micro, state FROM orders WHERE id=?", (order_id,)).fetchone()
        if r is None:
            return "live"
        size, state = int(r[0]), r[1]
        if state in ("cancelled", "expired", "rejected"):
            return state                     # a terminal state is never reopened by a late fill row
        return "filled" if matched >= size else "partial"

    def _reduce_lots(self, *, user_id: str, token_id: str, shares_micro: int, at: int) -> None:
        """FIFO across lots, in integers, and a shortfall is an error rather than a clamp: selling more than
        we hold means the venue and our books disagree about a position, and silently zeroing the lot is how
        that disagreement becomes invisible."""
        left = shares_micro
        rows = self.conn.execute("SELECT id, shares_open_micro, basis_micro FROM position_lots WHERE "
                                 "user_id=? AND token_id=? AND shares_open_micro>0 ORDER BY opened_ms, id",
                                 (user_id, token_id)).fetchall()
        for lot_id, open_shares, basis in rows:
            if left <= 0:
                break
            take = min(left, int(open_shares))
            released = (int(basis) * take) // int(open_shares)
            self.conn.execute("UPDATE position_lots SET shares_open_micro=shares_open_micro-?, "
                              "basis_micro=basis_micro-? WHERE id=?", (take, released, lot_id))
            left -= take
        if left > 0:
            # Position was sold that we do not have. Recorded as a snapshot delta rather than fixed: the
            # reconciler's `ambiguous_settlement` case is what a human then reads, and the position stays at
            # its last consistent value instead of being forced to something we invented.
            # `shares_micro` is the OBSERVED holding (what the venue says: nothing), `our_computed_micro` is
            # what our books would say if we allowed them to go negative. The generated delta_micro is then
            # the shortfall, and the snapshot table is how the discrepancy becomes someone's queue.
            self.conn.execute("INSERT INTO position_snapshots (user_id,token_id,shares_micro,"
                              "our_computed_micro,source,observed_ms) VALUES (?,?,?,?,?,?)",
                              (user_id, token_id, 0, -(left), "clob", at))

    def positions_short(self) -> list[dict]:
        rows = self.conn.execute("SELECT user_id,token_id,shares_micro,our_computed_micro,delta_micro,"
                                 "observed_ms,source FROM position_snapshots WHERE delta_micro <> 0 "
                                 "ORDER BY observed_ms DESC LIMIT 50").fetchall()
        keys = ("user_id", "token_id", "observed_micro", "computed_micro", "delta_micro", "observed_ms",
                "source")
        return [dict(zip(keys, r)) for r in rows]

    # ----------------------------------------------------------------------------- order state maps
    def set_order_state(self, order_id: str, venue_status: str, *, at: int, source: str = "reconciler",
                        size_matched_micro: int | None = None, intent_id: str = "", user_id: str = "") -> dict:
        """One map for venue status -> our state (`venue_status_to_state`), and an unmapped status is a
        loud ValueError. Translating it to `live` is how a new venue status keeps an order open forever."""
        state = venue_status_to_state(venue_status)
        if size_matched_micro is None:
            size_matched_micro = self.fills_booked_micro(order_id)
        self.execute("UPDATE orders SET state=?, size_matched_micro=?, updated_ms=? WHERE id=?",
                     (state, size_matched_micro, at, order_id))
        if intent_id:
            self.lifecycle(intent_id=intent_id, order_id=order_id, user_id=user_id, state=state,
                           reason="venue says %r" % venue_status, source=source, at=at)
            if state in ("filled", "cancelled", "expired", "rejected"):
                self.execute("UPDATE order_intents SET state=?, updated_ms=? WHERE id=? AND "
                             "state IN ('submitting','submitted','uncertain')",
                             ("cancelled" if state in ("cancelled", "expired") else "submitted", at, intent_id))
        return {"state": state, "size_matched_micro": size_matched_micro}

    def order_row(self, order_id: str) -> dict | None:
        r = self.conn.execute("SELECT id,intent_id,user_id,token_id,market_id,side,price_micro,size_micro,"
                              "size_matched_micro,state,acknowledged,expiration_ts,placed_ms,updated_ms "
                              "FROM orders WHERE id=?", (order_id,)).fetchone()
        if r is None:
            return None
        keys = ("id", "intent_id", "user_id", "token_id", "market_id", "side", "price_micro", "size_micro",
                "size_matched_micro", "state", "acknowledged", "expiration_ts", "placed_ms", "updated_ms")
        return dict(zip(keys, r))

    def local_order_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT id FROM orders")}

    def stamp_hash(self, intent_id: str, client_order_hash: str, *, at: int) -> None:
        """Put the deterministic client order hash on the intent BEFORE the order goes on the wire.

        The hash is derived from the payload, not returned by the venue, so it can be written early — and it
        has to be, because it is the only column that lets a process we have never met answer "did this order
        exist?" after we are gone. A stamp that waits for the venue's answer is a stamp a crash can skip, and
        the crash is the only case that needs it.
        """
        self.execute("UPDATE order_intents SET client_order_hash=?, updated_ms=? WHERE id=?",
                     (client_order_hash, at, intent_id))

    def prior_attempt(self, client_order_hash: str) -> dict | None:
        """Has THIS payload already been sent? `order_attempts` is written before the POST and never after, so
        a row means some process — usually one that no longer exists — put these bytes on the wire.

        This is the question a requeue after a crash has to answer. Answering it with "no row, no problem"
        would be right for a fresh intent and catastrophic for an old one, which is why the caller asks it by
        hash rather than by intent state: the hash is the only thing both sides agree on.
        """
        if not client_order_hash:
            return None
        r = self.conn.execute("SELECT intent_id, submitted_ms, ack_ms, signed_ms FROM order_attempts "
                              "WHERE client_order_hash=? ORDER BY signed_ms DESC LIMIT 1",
                              (client_order_hash,)).fetchone()
        return None if r is None else {"intent_id": r[0], "submitted_ms": r[1] or 0, "ack_ms": r[2] or 0,
                                       "signed_ms": r[3] or 0}

    def local_hashes(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT client_order_hash FROM order_attempts "
                                                "WHERE client_order_hash <> ''")}

    def open_orders(self) -> list[dict]:
        rows = self.conn.execute("SELECT id,intent_id,user_id,token_id,market_id,state,expiration_ts,"
                                 "updated_ms FROM orders WHERE state IN ('live','partial') ORDER BY placed_ms")
        keys = ("id", "intent_id", "user_id", "token_id", "market_id", "state", "expiration_ts", "updated_ms")
        return [dict(zip(keys, r)) for r in rows]

    def market_is_closing(self, condition_id: str, *, at: int) -> bool:
        """A market is "closing" for reconciliation purposes when it will not take orders any more, which is
        `accepting_orders=0` OR an explicit end. `closed` does not exist on `markets` (P05 lesson), so the
        winner column is the only resolution fact we have."""
        r = self.conn.execute("SELECT accepting_orders FROM markets WHERE id=?", (condition_id,)).fetchone()
        return bool(r and not r[0])

    # --------------------------------------------------------------------------- risk counters (D4)
    def orders_this_minute(self, user_id: str, *, at: int) -> int:
        r = self.conn.execute("SELECT COUNT(*) FROM order_intents WHERE user_id=? AND created_ms>?",
                              (user_id, at - 60_000)).fetchone()
        return int(r[0] or 0)

    def orders_today(self, user_id: str, *, at: int) -> int:
        r = self.conn.execute("SELECT COUNT(*) FROM order_intents WHERE user_id=? AND created_ms>?",
                              (user_id, at - 86_400_000)).fetchone()
        return int(r[0] or 0)

    def open_order_count(self, user_id: str) -> int:
        r = self.conn.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND state IN ('live','partial')",
                              (user_id,)).fetchone()
        return int(r[0] or 0)

    def spent_24h_micro(self, user_id: str, *, at: int) -> int:
        r = self.conn.execute("SELECT COALESCE(SUM(notional_micro),0) FROM order_intents WHERE user_id=? "
                              "AND state IN ('submitted','submitting') AND created_ms>?",
                              (user_id, at - 86_400_000)).fetchone()
        return int(r[0] or 0)

    def trading_cash_micro(self, user_id: str, *, at: int, window_ms: int = 86_400_000) -> dict:
        rows = self.conn.execute("SELECT COALESCE(SUM(amount_micro),0), "
                                 "COALESCE(SUM(CASE WHEN kind='fee' THEN -amount_micro ELSE 0 END),0), "
                                 "COUNT(*) FROM cash_ledger WHERE user_id=? AND created_ms>? AND kind IN "
                                 "('buy','sell_fill','merge_receipt','resolution_payout','fee','refund')",
                                 (user_id, at - window_ms)).fetchone()
        open_basis = self.conn.execute("SELECT COALESCE(SUM(basis_micro),0) FROM position_lots WHERE "
                                       "user_id=? AND shares_open_micro>0", (user_id,)).fetchone()[0]
        return {"net_micro": int(rows[0] or 0), "fees_paid_micro": int(rows[1] or 0),
                "movements": int(rows[2] or 0), "open_basis_micro": int(open_basis or 0)}

    def realized_pnl_today_micro(self, user_id: str, *, at: int) -> int:
        """Realised PnL for the day, in the one definition this phase needs it for: the daily-loss halt.

            realized = net_trading_cash + fees_paid + open_cost_basis

        i.e. everything the user actually got back, plus the fees they paid, plus what is still sitting in
        positions. Unrealised marks are EXCLUDED on purpose: a binary position has no mark until resolution,
        and a halt that fires on "you are down on paper" would stop a user from managing a position, which
        is the opposite of the protection. The cost of that choice is that a user holding a doomed position
        is not halted until it settles; that is stated in docs/P06 D4.3 rather than hidden, and it is the
        reason the halt also fires on *notional spent* via the 24h cap, which is not mark-dependent.
        """
        t = self.trading_cash_micro(user_id, at=at)
        return int(t["net_micro"] + t["fees_paid_micro"] + t["open_basis_micro"])

    def loss_halt(self, user_id: str) -> dict | None:
        r = self.conn.execute("SELECT threshold_micro, realized_micro, tripped_ms, acknowledged_ms "
                              "FROM loss_halts WHERE user_id=?", (user_id,)).fetchone()
        if r is None:
            return None
        return {"threshold_micro": int(r[0]), "realized_micro": int(r[1]), "tripped_ms": int(r[2]),
                "acknowledged_ms": int(r[3]) if r[3] else None}

    def trip_loss_halt(self, *, user_id: str, threshold_micro: int, realized_micro: int, at: int) -> None:
        self.execute("INSERT INTO loss_halts (user_id,threshold_micro,realized_micro,tripped_ms) "
                     "VALUES (?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET realized_micro=excluded.realized_micro,"
                     "tripped_ms=excluded.tripped_ms, acknowledged_ms=NULL",
                     (user_id, threshold_micro, realized_micro, at))

    def acknowledge_loss_halt(self, *, user_id: str, actor: str, at: int) -> None:
        if not actor.strip():
            raise ValueError("an acknowledgement with no actor is not an acknowledgement")
        cur = self.execute("UPDATE loss_halts SET acknowledged_ms=?, acknowledged_by=? WHERE user_id=? "
                           "AND acknowledged_ms IS NULL", (at, actor.strip()[:64], user_id))
        return cur.rowcount or 0

    def blocklisted(self, market_id: str, *, at: int) -> dict | None:
        r = self.conn.execute("SELECT reason, source, expires_ms FROM risk_blocklists WHERE market_id=?",
                              (market_id,)).fetchone()
        if r is None:
            return None
        if r[2] and int(r[2]) < at:
            return None                                   # an expiry that passed is not a block; it is history
        return {"reason": r[0], "source": r[1], "expires_ms": int(r[2] or 0)}

    def add_blocklist(self, *, market_id: str, reason: str, source: str, actor: str, at: int,
                      expires_ms: int = 0) -> None:
        if len(reason.strip()) < 4:
            raise ValueError("a blocklist entry needs a reason a reader can act on")
        self.execute("INSERT INTO risk_blocklists (market_id,reason,source,added_by,added_ms,expires_ms) "
                     "VALUES (?,?,?,?,?,?) ON CONFLICT(market_id) DO UPDATE SET reason=excluded.reason,"
                     "source=excluded.source,added_ms=excluded.added_ms,expires_ms=excluded.expires_ms",
                     (market_id, reason.strip()[:300], source, actor, at, expires_ms or None))

    def bump_counter(self, key: str, *, at: int, bucket_ms: int = 60_000, limit: int | None = None) -> dict:
        """Rate counters with the bucket in the key, so "12/minute" is a row that ages out instead of a
        sliding window that needs a data structure we would have to test separately."""
        b = at // bucket_ms
        r = self.conn.execute("SELECT count FROM rate_counters WHERE key=? AND bucket_ms=?",
                              (key, b)).fetchone()
        n = (int(r[0]) if r else 0) + 1
        if r is None:
            self.execute("INSERT INTO rate_counters (key,bucket_ms,count,updated_ms) VALUES (?,?,?,?)",
                         (key, b, n, at))
        else:
            self.execute("UPDATE rate_counters SET count=?, updated_ms=? WHERE key=? AND bucket_ms=?",
                         (n, at, key, b))
        return {"count": n, "limit": limit, "allowed": limit is None or n <= limit}

    def counter(self, key: str, *, at: int, bucket_ms: int = 60_000) -> int:
        r = self.conn.execute("SELECT count FROM rate_counters WHERE key=? AND bucket_ms=?",
                              (key, at // bucket_ms)).fetchone()
        return int(r[0] or 0)

    # --------------------------------------------------------------------------- wallets (D1 reads)
    def wallet(self, user_id: str) -> dict | None:
        r = self.conn.execute("SELECT user_id,provider,custody,address,proxy_address,signature_type,"
                              "policy_hash,policy_gap,state,created_ms,updated_ms FROM wallets WHERE user_id=?",
                              (user_id,)).fetchone()
        if r is None:
            return None
        keys = ("user_id", "provider", "custody", "address", "proxy_address", "signature_type", "policy_hash",
                "policy_gap", "state", "created_ms", "updated_ms")
        return dict(zip(keys, r))

    def set_wallet_state(self, user_id: str, state: str, *, at: int, event: str = "",
                         detail: dict | None = None, policy_hash: str = "",
                         policy_gap: str | None = None) -> None:
        """State, the policy hash it is in force under, and the audit event, in one transaction.

        The three are updated together because a wallet whose state moved without a `wallet_events` row is
        the thing D1 says must be impossible: "obvious and logged" means the log is part of the write, not a
        follow-up call that can be forgotten (or fail) on its own.
        """
        outer = self._begin()
        try:
            row = self.conn.execute("SELECT state,policy_gap FROM wallets WHERE user_id=?",
                                    (user_id,)).fetchone()
            gap = policy_gap if policy_gap is not None else ((row or (None, None))[1])
            # The state machine is enforced HERE, at the only writer, and not merely offered by
            # `lifecycle.assert_transition`. A machine that callers may skip is a diagram: `closing -> trading`
            # is the hop that hands trading keys to a wallet in the middle of being emptied, and D1's whole
            # claim is that no code path can produce it.
            if row is None:
                if state not in ("none", "provisioned"):
                    raise ValueError("WALLET_STATE_UNPROVISIONED: a wallet with no row can only be "
                                     "provisioned, not moved to %r" % state)
            elif row[0] != state:
                wl.assert_transition(row[0], state)
            self.conn.execute("UPDATE wallets SET state=?, updated_ms=?, policy_hash=COALESCE(NULLIF(?,''),"
                              "policy_hash), policy_gap=? WHERE user_id=?",
                              (state, at, policy_hash, gap, user_id))
            if event:
                self.conn.execute("INSERT INTO wallet_events (user_id,event,detail_json,actor,at_ms) "
                                  "VALUES (?,?,?,?,?)",
                                  (user_id, event, json.dumps(detail or {}, separators=(",", ":")),
                                   "system", at))
            self._commit(outer)
        except Exception:
            self._rollback(outer)
            raise

    def balance_available_micro(self, user_id: str) -> int:
        r = self.conn.execute("SELECT usdc_available_micro FROM balances WHERE user_id=?", (user_id,)).fetchone()
        return int(r[0] or 0) if r else 0

    def allowance_micro(self, user_id: str, *, token: str = "pUSD", spender: str = "") -> dict:
        rows = self.conn.execute("SELECT spender, amount_micro, checked_ms FROM allowances WHERE user_id=? "
                                 "AND token=?", (user_id, token)).fetchall()
        if not rows:
            return {"granted_micro": 0, "spender": "", "checked_ms": 0, "rows": []}
        best = max(rows, key=lambda r: int(r[1]))
        return {"granted_micro": int(best[1]), "spender": best[0], "checked_ms": int(best[2]),
                "rows": [{"spender": r[0], "amount_micro": int(r[1]), "checked_ms": int(r[2])} for r in rows]}

    def set_allowance(self, *, user_id: str, token: str, spender: str, amount_micro: int, at: int) -> None:
        self.execute("INSERT INTO allowances (user_id,token,spender,amount_micro,checked_ms) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(user_id,token,spender) DO UPDATE SET amount_micro=excluded.amount_micro,"
                     "checked_ms=excluded.checked_ms", (user_id, token, spender, amount_micro, at))

    def market_quote(self, market_id: str, *, at: int, depth: int = 25) -> dict:
        """The last book we hold for a market, from `book_levels` — the only book source that exists, and
        the reason a price-sanity check is allowed to exist at all: it compares against stored truth rather
        than against a value the caller produced.

        Returns `age_ms = 10**12` when there is no book. A caller that treats a missing book as "0 ms old"
        has just approved an order with no reference price, which is the exact hole `STALE_QUOTE` closes.

        The body is `automation.facts.quote` since P10 D8, because the copy engine, the executor and the API's
        rule preview must all read one book — and `last_price_micro` was added there, since `price_cross`
        accepts `uses: "last"` and nothing was filling it.
        """
        return _facts.quote(self.conn, market_id, at=at, depth=depth)

    # ------------------------------------------------------------------- reconciler durable state (D3)
    def cursor_load(self) -> dict:
        r = self.conn.execute("SELECT watermark_ms,in_flight,last_run_ms,last_error,updated_ms "
                              "FROM reconcile_cursors WHERE name='main'").fetchone()
        if r is None:
            self.conn.execute("INSERT INTO reconcile_cursors (name,watermark_ms,in_flight,last_run_ms,"
                              "updated_ms) VALUES ('main',0,0,0,?)", (now_ms(),))
            return {"watermark_ms": 0, "in_flight": 0, "last_run_ms": 0, "last_error": "", "updated_ms": 0}
        return {"watermark_ms": int(r[0]), "in_flight": int(r[1]), "last_run_ms": int(r[2]),
                "last_error": r[3] or "", "updated_ms": int(r[4])}

    def cursor_save(self, *, watermark_ms: int, in_flight: int, at: int, last_error: str = "") -> None:
        self.execute("UPDATE reconcile_cursors SET watermark_ms=?,in_flight=?,last_run_ms=?,last_error=?,"
                     "updated_ms=? WHERE name='main'", (watermark_ms, in_flight, at, last_error[:300], at))

    def open_case(self, *, case: str, intent_id: str = "", order_id: str = "", since_ms: int, note: str,
                  dedupe_key: str) -> bool:
        """True when this is a NEW unreconciled item. The key is what makes an age measurable: an upsert
        would reset `since_ms` on every re-sighting, and an alarm that resets itself every pass is an alarm
        that never fires."""
        cur = self.execute("INSERT INTO reconcile_open (dedupe_key,case_name,intent_id,order_id,since_ms,note)"
                           " VALUES (?,?,?,?,?,?) ON CONFLICT(dedupe_key) DO NOTHING",
                           (dedupe_key, case, intent_id, order_id, since_ms, note[:300]))
        return (cur.rowcount or 0) > 0

    def bump_case(self, dedupe_key: str, *, at: int, note: str = "") -> None:
        self.execute("UPDATE reconcile_open SET attempts=attempts+1, note=COALESCE(NULLIF(?,''),note) "
                     "WHERE dedupe_key=?", (note[:300], dedupe_key))

    def note_case(self, dedupe_key: str, *, at: int, note: str) -> None:
        """Update the note WITHOUT an attempt: for a sighting that proves nothing (a lookup that failed),
        the queue must say what happened without aging the item toward an automatic conclusion."""
        self.execute("UPDATE reconcile_open SET note=? WHERE dedupe_key=?", (note[:300], dedupe_key))

    def escalate_case(self, dedupe_key: str, *, at: int) -> None:
        self.execute("UPDATE reconcile_open SET escalated_ms=? WHERE dedupe_key=? AND escalated_ms IS NULL",
                     (at, dedupe_key))

    def case_attempts(self, dedupe_key: str) -> dict:
        r = self.conn.execute("SELECT attempts, escalated_ms, since_ms, note FROM reconcile_open WHERE "
                              "dedupe_key=?", (dedupe_key,)).fetchone()
        return {"attempts": int(r[0] or 0), "escalated_ms": int(r[1] or 0), "since_ms": int(r[2] or 0),
                "note": r[3] or ""} if r else {"attempts": 0, "escalated_ms": 0, "since_ms": 0, "note": ""}

    def close_case(self, dedupe_key: str) -> int:
        cur = self.execute("DELETE FROM reconcile_open WHERE dedupe_key=?", (dedupe_key,))
        return cur.rowcount or 0

    def unreconciled(self) -> list[dict]:
        rows = self.conn.execute("SELECT dedupe_key,case_name,intent_id,order_id,since_ms,note,attempts,"
                                 "escalated_ms FROM reconcile_open ORDER BY since_ms")
        keys = ("dedupe_key", "case_name", "intent_id", "order_id", "since_ms", "note", "attempts",
                "escalated_ms")
        return [dict(zip(keys, r)) for r in rows]

    def action_taken(self, dedupe_key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM reconcile_actions WHERE dedupe_key=?",
                                 (dedupe_key,)).fetchone() is not None

    def record_action(self, *, case: str, intent_id: str, order_id: str, action: str, dedupe_key: str,
                      at: int) -> bool:
        cur = self.execute("INSERT INTO reconcile_actions (case_name,intent_id,order_id,action,dedupe_key,at_ms)"
                           " VALUES (?,?,?,?,?,?) ON CONFLICT(dedupe_key) DO NOTHING",
                           (case, intent_id, order_id, action[:200], dedupe_key, at))
        return (cur.rowcount or 0) > 0

    # ------------------------------------------------------------------------- notifications (D7)
    def notify(self, *, intent_id: str, user_id: str, event: str, at: int, detail: dict | None = None) -> None:
        """Order-event notifications. Not pushed from the executor: queued, in the same table the P05 fanout
        plans over, so an order event competes for delivery with a signal alert on the SAME priority rules
        instead of inventing a second, subtly different queue. Priority 0: a fill is the most time-sensitive
        message we have."""
        if event not in ORDER_EVENTS:
            raise ValueError("BAD_ORDER_EVENT: %s" % event)
        self.execute("INSERT INTO order_notifications (intent_id,user_id,event,channel,status,at_ms,"
                     "priority,detail_json) VALUES (?,?,?,?,?,?,?,?)",
                     (intent_id, user_id, event, "in_app", "queued", at, 0,
                      json.dumps(detail or {}, separators=(",", ":"))))

    def notifications_queued(self) -> list[dict]:
        rows = self.conn.execute("SELECT intent_id,user_id,event,at_ms FROM order_notifications WHERE "
                                 "status='queued' ORDER BY priority, at_ms LIMIT 50")
        keys = ("intent_id", "user_id", "event", "at_ms")
        return [dict(zip(keys, r)) for r in rows]

    def notification_mark(self, intent_id: str, event: str, status: str, at: int) -> None:
        self.execute("UPDATE order_notifications SET status=?, delivered_ms=? WHERE intent_id=? AND event=?",
                     (status, at, intent_id, event))

    # ------------------------------------------------------------------------ automation run history
    def record_run(self, *, rule_id: str, user_id: str, mode: str, outcome: str, reason: str, deny_code: str,
                   intent_id: str, at: int, detail: dict | None = None) -> None:
        self.execute("INSERT INTO automation_runs (rule_id,user_id,mode,outcome,reason,deny_code,intent_id,"
                     "detail_json,at_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                     (rule_id, user_id, mode, outcome, reason[:300], deny_code, intent_id,
                      json.dumps(detail or {}, separators=(",", ":")), at))

    def dry_run_history(self, rule_id: str, *, limit: int = 20) -> list[dict]:
        """The dry-run passes this rule has actually had, newest first. `mark_dry_run_done` reads this rather
        than trusting a caller's word, which is the difference between a safety step and a checkbox."""
        rows = self.conn.execute("SELECT mode,outcome,reason,at_ms FROM automation_runs WHERE rule_id=? AND "
                                 "mode='dry_run' ORDER BY at_ms DESC LIMIT ?", (rule_id, limit))
        keys = ("mode", "outcome", "reason", "at_ms")
        return [dict(zip(keys, r)) for r in rows]

    def run_history(self, rule_id: str, *, limit: int = 50) -> list[dict]:
        rows = self.conn.execute("SELECT mode,outcome,reason,deny_code,at_ms FROM automation_runs WHERE "
                                 "rule_id=? ORDER BY at_ms DESC LIMIT ?", (rule_id, limit))
        keys = ("mode", "outcome", "reason", "deny_code", "at_ms")
        return [dict(zip(keys, r)) for r in rows]

    # --------------------------------------------------------------------------- attribution (D8)
    def save_terms(self, *, attribution_id: int, builder_code: str, fee_bps: int, notional_micro: int,
                   excluded_reason: str, code_fingerprint: str, at: int) -> None:
        """Freeze the terms an order ran under. Called by the executor next to the attribution row, never by
        the reconciliation job: the job that measures must not also decide what was owed."""
        self.execute("INSERT INTO builder_attribution_terms (attribution_id,builder_code,fee_bps,"
                     "notional_micro,excluded_reason,code_fingerprint,recorded_ms) VALUES (?,?,?,?,?,?,?) "
                     "ON CONFLICT(attribution_id) DO UPDATE SET builder_code=excluded.builder_code,"
                     "fee_bps=excluded.fee_bps,notional_micro=excluded.notional_micro,"
                     "excluded_reason=excluded.excluded_reason,code_fingerprint=excluded.code_fingerprint,"
                     "recorded_ms=excluded.recorded_ms",
                     (attribution_id, builder_code, fee_bps, notional_micro, excluded_reason,
                      code_fingerprint, at))

    def attribution_rows(self, *, at: int, window_ms: int = 86_400_000, only_unmeasured: bool = False) -> list[dict]:
        """Expectation, terms and the chain's own measurement, in one read — so the rollup is one query and
        the delta is computed by the module that owns the tolerance, not by whoever wrote the SQL.

        `measured` distinguishes "the venue paid nothing" (a measure row of 0) from "nobody has looked"
        (no measure row). Collapsing those two is how a reconciliation job reports a clean day.
        """
        sql = ("SELECT a.id,a.intent_id,a.user_id,a.fee_bps_expected,a.fee_micro_expected,"
               "COALESCE(a.fee_micro_observed,0),a.market_id,a.token_id,a.placed_ms,a.order_id,"
               "t.notional_micro,t.excluded_reason,t.code_fingerprint,t.fee_bps,"
               "m.chain_measured_micro,m.delta_micro,m.source,m.reconciled_ms "
               "FROM builder_attribution a LEFT JOIN builder_attribution_terms t ON t.attribution_id=a.id "
               "LEFT JOIN builder_attribution_measures m ON m.attribution_id=a.id WHERE a.placed_ms>?")
        if only_unmeasured:
            sql += " AND m.attribution_id IS NULL"
        sql += " ORDER BY a.placed_ms"
        rows = self.conn.execute(sql, (at - window_ms,)).fetchall()
        keys = ("attribution_id", "intent_id", "user_id", "fee_bps", "expected_micro", "observed_micro",
                "market_id", "token_id", "placed_ms", "order_id", "notional_micro", "excluded_reason",
                "code_fingerprint", "terms_fee_bps", "chain_measured_micro", "delta_micro", "measure_source",
                "reconciled_ms")
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            d["measured"] = d["reconciled_ms"] is not None
            out.append(d)
        return out

    def save_measure(self, *, attribution_id: int, order_id: str, chain_micro: int, notional_micro: int,
                     expected_micro: int, source: str, at: int) -> dict:
        delta = expected_micro - chain_micro
        self.execute("INSERT INTO builder_attribution_measures (attribution_id,order_id,chain_measured_micro,"
                     "notional_micro,source,delta_micro,reconciled_ms) VALUES (?,?,?,?,?,?,?) "
                     "ON CONFLICT(attribution_id) DO UPDATE SET chain_measured_micro=excluded.chain_measured_micro,"
                     "delta_micro=excluded.delta_micro,reconciled_ms=excluded.reconciled_ms",
                     (attribution_id, order_id, chain_micro, notional_micro, source, delta, at))
        return {"delta_micro": delta, "status": "matched" if delta == 0 else "investigating"}

    def save_daily(self, *, day: str, at: int, orders: int, fills: int, volume_micro: int, expected_micro: int,
                   chain_micro: int, platform_fee_micro: int, status: str) -> None:
        self.execute("INSERT INTO builder_revenue_daily (day,orders,fills,volume_micro,expected_micro,"
                     "chain_micro,platform_fee_micro,delta_micro,status,updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(day) DO UPDATE SET orders=excluded.orders,fills=excluded.fills,"
                     "volume_micro=excluded.volume_micro,expected_micro=excluded.expected_micro,"
                     "chain_micro=excluded.chain_micro,platform_fee_micro=excluded.platform_fee_micro,"
                     "delta_micro=excluded.delta_micro,status=excluded.status,updated_ms=excluded.updated_ms",
                     (day, orders, fills, volume_micro, expected_micro, chain_micro, platform_fee_micro,
                      expected_micro - chain_micro, status, at))

    def daily_rows(self, *, limit: int = 30) -> list[dict]:
        rows = self.conn.execute("SELECT day,orders,fills,volume_micro,expected_micro,chain_micro,"
                                 "platform_fee_micro,delta_micro,status FROM builder_revenue_daily "
                                 "ORDER BY day DESC LIMIT ?", (limit,))
        keys = ("day", "orders", "fills", "volume_micro", "expected_micro", "chain_micro",
                "platform_fee_micro", "delta_micro", "status")
        return [dict(zip(keys, r)) for r in rows]

    def ingest_chain_event(self, *, source: str, kind: str, tx_hash: str, log_index: int, order_id: str,
                           fee_micro: int, matched_micro: int, price_micro: int, builder: str, at: int,
                           block_number: int = 0, maker: str = "", taker: str = "", token_id: str = "") -> bool:
        cur = self.execute("INSERT INTO chain_events (source,kind,block_number,log_index,tx_hash,order_id,"
                           "maker,taker,token_id,matched_micro,price_micro,fee_micro,builder,seen_ms) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tx_hash,log_index) DO NOTHING",
                           (source, kind, block_number, log_index, tx_hash, order_id, maker, taker, token_id,
                            matched_micro, price_micro, fee_micro, builder, at))
        return (cur.rowcount or 0) > 0

    # ---------------------------------------------------------------------------- copy / sources
    def save_source_stats(self, s: dict, *, at: int) -> None:
        self.execute("INSERT INTO copy_source_stats (source_user_id,window_days,closed_trades,win_rate_bp,"
                     "realized_pnl_micro,fees_micro,net_after_fees_micro,max_drawdown_micro,"
                     "longest_losing_streak,avg_latency_ms,updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(source_user_id,window_days) DO UPDATE SET "
                     "closed_trades=excluded.closed_trades,win_rate_bp=excluded.win_rate_bp,"
                     "realized_pnl_micro=excluded.realized_pnl_micro,fees_micro=excluded.fees_micro,"
                     "net_after_fees_micro=excluded.net_after_fees_micro,"
                     "max_drawdown_micro=excluded.max_drawdown_micro,"
                     "longest_losing_streak=excluded.longest_losing_streak,"
                     "avg_latency_ms=excluded.avg_latency_ms,updated_ms=excluded.updated_ms",
                     (s["source_user_id"], s["window_days"], s["closed_trades"], s["win_rate_bp"],
                      s["realized_pnl_micro"], s["fees_micro"], s["net_after_fees_micro"],
                      s["max_drawdown_micro"], s["longest_losing_streak"], s["avg_latency_ms"], at))

    def save_economics(self, e: dict, *, at: int) -> None:
        self.execute("INSERT INTO copy_economics (source_user_id,copier_count,copier_volume_micro,fees_micro,"
                     "builder_fees_micro,copier_net_micro,source_payout_micro,updated_ms) VALUES (?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(source_user_id) DO UPDATE SET copier_count=excluded.copier_count,"
                     "copier_volume_micro=excluded.copier_volume_micro,fees_micro=excluded.fees_micro,"
                     "builder_fees_micro=excluded.builder_fees_micro,copier_net_micro=excluded.copier_net_micro,"
                     "source_payout_micro=excluded.source_payout_micro,updated_ms=excluded.updated_ms",
                     (e["source_user_id"], e["copier_count"], e["copier_volume_micro"], e["fees_micro"],
                      e["builder_fees_micro"], e["copier_net_micro"], e["source_payout_micro"], at))

    def copy_config(self, config_id: str) -> dict | None:
        r = self.conn.execute("SELECT c.id,c.user_id,c.source_user,c.mode,c.ratio_bps,c.max_order_micro,"
                              "c.max_daily_micro,c.blocked_markets,c.enabled,p.max_entry_deviation_bps,"
                              "p.min_interval_ms,p.take_profit_bp,p.stop_loss_bp,p.min_seconds_to_resolution,"
                              "p.max_price_micro,p.copy_up_to_side,p.hop_depth,p.paused_reason,p.updated_ms "
                              "FROM copy_configs c LEFT JOIN copy_config_policy p ON p.config_id=c.id "
                              "WHERE c.id=?", (config_id,)).fetchone()
        if r is None:
            return None
        keys = ("id", "user_id", "source_user", "mode", "ratio_bps", "max_order_micro", "max_daily_micro",
                "blocked_markets", "enabled", "max_entry_deviation_bps", "min_interval_ms", "take_profit_bp",
                "stop_loss_bp", "min_seconds_to_resolution", "max_price_micro", "copy_up_to_side", "hop_depth",
                "paused_reason", "updated_ms")
        d = dict(zip(keys, r))
        try:
            d["blocked_markets"] = json.loads(d["blocked_markets"] or "[]")
        except (TypeError, json.JSONDecodeError):
            d["blocked_markets"] = []
        return d

    def copy_configs_for_source(self, source_user: str) -> list[dict]:
        rows = self.conn.execute("SELECT id FROM copy_configs WHERE source_user=? AND enabled=1",
                                 (source_user,)).fetchall()
        return [self.copy_config(r[0]) for r in rows if self.copy_config(r[0])]

    def all_copy_configs(self) -> list[dict]:
        rows = self.conn.execute("SELECT id FROM copy_configs").fetchall()
        return [c for c in (self.copy_config(r[0]) for r in rows) if c]

    def save_copy_config_policy(self, *, config_id: str, at: int, **kw) -> None:
        cols = dict(max_entry_deviation_bps=150, min_interval_ms=0, take_profit_bp=None, stop_loss_bp=None,
                    min_seconds_to_resolution=900, max_price_micro=None, copy_up_to_side=False, hop_depth=1,
                    paused_reason=None)
        cols.update({k: v for k, v in kw.items() if k in cols})
        vals = [cols[k] for k in ("max_entry_deviation_bps", "min_interval_ms", "take_profit_bp",
                                  "stop_loss_bp", "min_seconds_to_resolution", "max_price_micro",
                                  "copy_up_to_side", "hop_depth", "paused_reason")]
        for i, v in enumerate(vals):
            if isinstance(v, bool):
                vals[i] = int(v)
        cur = self.conn.execute("SELECT 1 FROM copy_config_policy WHERE config_id=?", (config_id,)).fetchone()
        if cur is None:
            self.conn.execute("INSERT INTO copy_config_policy (config_id,max_entry_deviation_bps,"
                              "min_interval_ms,take_profit_bp,stop_loss_bp,min_seconds_to_resolution,"
                              "max_price_micro,copy_up_to_side,hop_depth,paused_reason,updated_ms) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (config_id, *vals, at))
        else:
            self.conn.execute("UPDATE copy_config_policy SET max_entry_deviation_bps=?,min_interval_ms=?,"
                              "take_profit_bp=?,stop_loss_bp=?,min_seconds_to_resolution=?,max_price_micro=?,"
                              "copy_up_to_side=?,hop_depth=?,paused_reason=?,updated_ms=? WHERE config_id=?",
                              (*vals, at, config_id))

    def record_copy_event(self, *, copier_id: str, source_user_id: str, action: str, reason: str, at: int,
                          deviation_bps: int = 0, source_intent_id: str = "", intent_id: str = "") -> None:
        self.execute("INSERT INTO copy_events (copier_id,source_user_id,source_intent_id,intent_id,action,"
                     "reason,deviation_bps,at_ms) VALUES (?,?,?,?,?,?,?,?)",
                     (copier_id, source_user_id, source_intent_id, intent_id, action, reason[:300],
                      deviation_bps, at))

    def copy_events(self, copier_id: str, *, limit: int = 50) -> list[dict]:
        rows = self.conn.execute("SELECT action,reason,deviation_bps,at_ms,intent_id FROM copy_events WHERE "
                                 "copier_id=? ORDER BY at_ms DESC LIMIT ?", (copier_id, limit))
        keys = ("action", "reason", "deviation_bps", "at_ms", "intent_id")
        return [dict(zip(keys, r)) for r in rows]

    # ------------------------------------------------------------------------- automation rules
    def automation_rule(self, rule_id: str) -> dict | None:
        r = self.conn.execute("SELECT a.id,a.user_id,a.kind,a.trigger_json,a.max_loss_micro,a.enabled,"
                              "p.trigger_json,p.actions_json,p.dry_run_completed_ms,p.max_per_day,"
                              "p.min_interval_ms,p.human_priority_ms,p.last_fire_ms FROM automation_rules a "
                              "LEFT JOIN automation_rule_policy p ON p.rule_id=a.id WHERE a.id=?",
                              (rule_id,)).fetchone()
        if r is None:
            return None
        keys = ("id", "user_id", "kind", "trigger_json", "max_loss_micro", "enabled", "policy_trigger_json",
                "actions_json", "dry_run_completed_ms", "max_per_day", "min_interval_ms", "human_priority_ms",
                "last_fire_ms")
        d = dict(zip(keys, r))
        for k in ("trigger_json", "policy_trigger_json", "actions_json"):
            try:
                d[k] = json.loads(d[k] or "{}" if k != "actions_json" else (d[k] or "[]"))
            except (TypeError, json.JSONDecodeError):
                d[k] = None if k == "trigger_json" else ([] if k == "actions_json" else {})
        d["trigger"] = d.pop("policy_trigger_json") or d.pop("trigger_json") or {}
        d.pop("trigger_json", None)
        return d

    def automation_rules_enabled(self) -> list[dict]:
        rows = self.conn.execute("SELECT id FROM automation_rules WHERE enabled=1").fetchall()
        return [r for r in (self.automation_rule(x[0]) for x in rows) if r]

    def automation_rules_all(self) -> list[dict]:
        """Every saved rule, enabled or not. `tick(mode='dry_run')` uses this: a rule cannot be enabled before
        it has been observed, and it cannot be observed if the pass only looks at enabled rules — making the
        dry-run gate depend on `enabled` would be a circular safety check that no rule can satisfy."""
        rows = self.conn.execute("SELECT id FROM automation_rules ORDER BY id").fetchall()
        return [r for r in (self.automation_rule(x[0]) for x in rows) if r]

    def save_rule_trigger(self, *, rule_id: str, trigger, actions, at: int, max_per_day: int = 24,
                          min_interval_ms: int = 60_000, human_priority_ms: int = 120_000) -> None:
        cur = self.conn.execute("SELECT 1 FROM automation_rule_policy WHERE rule_id=?", (rule_id,)).fetchone()
        tj, aj = json.dumps(trigger, separators=(",", ":")), json.dumps(list(actions), separators=(",", ":"))
        if cur is None:
            self.conn.execute("INSERT INTO automation_rule_policy (rule_id,trigger_json,actions_json,"
                              "max_per_day,min_interval_ms,human_priority_ms,updated_ms) VALUES (?,?,?,?,?,?,?)",
                              (rule_id, tj, aj, max_per_day, min_interval_ms, human_priority_ms, at))
        else:
            self.conn.execute("UPDATE automation_rule_policy SET trigger_json=?,actions_json=?,max_per_day=?,"
                              "min_interval_ms=?,human_priority_ms=?,updated_ms=? WHERE rule_id=?",
                              (tj, aj, max_per_day, min_interval_ms, human_priority_ms, at, rule_id))
        self.conn.execute("UPDATE automation_rules SET trigger_json=? WHERE id=?", (tj, rule_id))

    def upsert_automation_rule(self, *, rule_id: str, user_id: str, kind: str, enabled: bool, at: int,
                               trigger: dict | None = None, actions: list | None = None,
                               max_loss_micro: int = 0, market_ids: list[tuple[str, str]] | None = None,
                               max_per_day: int = 24, min_interval_ms: int = 60_000,
                               human_priority_ms: int = 120_000) -> dict:
        """Save or replace a rule. Validation is the engine's job; this is the write, and the write is one
        transaction: a rule whose policy row saved but whose target rows half-wrote is a rule that fires on
        the wrong market, which is worse than not saving."""
        # `is not None` into a variable and then compared that variable to None is a bug that makes the
        # insert branch unreachable: the row is never created, the UPDATE matches nothing, and the next
        # write fails on a foreign key several statements later with no hint of the cause.
        existing = self.conn.execute("SELECT 1 FROM automation_rules WHERE id=?", (rule_id,)).fetchone()
        if existing is None:
            self.conn.execute("INSERT INTO automation_rules (id,user_id,kind,trigger_json,max_loss_micro,"
                              "enabled,last_run_ms) VALUES (?,?,?,?,?,?,?)",
                              (rule_id, user_id, kind, json.dumps(trigger or {}, separators=(",", ":")),
                               max_loss_micro, 1 if enabled else 0, None))
        else:
            self.conn.execute("UPDATE automation_rules SET kind=?,max_loss_micro=?,enabled=? WHERE id=?",
                              (kind, max_loss_micro, 1 if enabled else 0, rule_id))
        self.save_rule_trigger(rule_id=rule_id, trigger=trigger or {}, actions=actions or [], at=at,
                              max_per_day=max_per_day, min_interval_ms=min_interval_ms,
                              human_priority_ms=human_priority_ms)
        if market_ids is not None:
            self.conn.execute("DELETE FROM automation_rule_targets WHERE rule_id=?", (rule_id,))
            for market_id, token_id in market_ids:
                self.conn.execute("INSERT INTO automation_rule_targets (rule_id,market_id,token_id,"
                                  "created_ms) VALUES (?,?,?,?)", (rule_id, market_id, token_id, at))
        return {"rule_id": rule_id, "created": existing is None, "targets": len(market_ids or [])}

    def rule_state(self, rule_id: str) -> dict:
        r = self.conn.execute("SELECT paused_reason,armed_market_id,armed_token_id,take_profit_bp,"
                              "stop_loss_bp,failure_count,last_error,updated_ms FROM automation_rule_state "
                              "WHERE rule_id=?", (rule_id,)).fetchone()
        keys = ("paused_reason", "armed_market_id", "armed_token_id", "take_profit_bp", "stop_loss_bp",
                "failure_count", "last_error", "updated_ms")
        # A rule with no state row is a rule that has never failed, not a rule whose failure count is "".
        # Returning "" here type-errored three statements later, in the middle of the pass that was meant to
        # be resilient. Every absent integer must be 0 and every absent string must be "".
        ints = ("take_profit_bp", "stop_loss_bp", "failure_count")
        return dict(zip(keys, r)) if r else {k: (0 if k in ints else "") for k in keys}

    def set_rule_state(self, *, rule_id: str, at: int, **kw) -> None:
        allowed = ("paused_reason", "armed_market_id", "armed_token_id", "take_profit_bp", "stop_loss_bp",
                   "failure_count", "last_error")
        bad = set(kw) - set(allowed)
        if bad:
            raise ValueError("unknown automation_rule_state column(s): %s" % ", ".join(sorted(bad)))
        row = self.conn.execute("SELECT 1 FROM automation_rule_state WHERE rule_id=?", (rule_id,)).fetchone()
        cols = {k: kw[k] for k in allowed if k in kw}
        if row is None:
            names = ", ".join(["rule_id", *cols, "updated_ms"])
            marks = ", ".join("?" * (len(cols) + 2))
            self.conn.execute("INSERT INTO automation_rule_state (%s) VALUES (%s)" % (names, marks),
                              [rule_id, *cols.values(), at])
        else:
            sets = ", ".join("%s=?" % k for k in cols)
            self.conn.execute("UPDATE automation_rule_state SET %s,updated_ms=? WHERE rule_id=?"
                              % (sets or "updated_ms=updated_ms"), [*cols.values(), at, rule_id])

    def rule_targets(self, rule_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT market_id,token_id FROM automation_rule_targets WHERE rule_id=? "
                                 "ORDER BY market_id", (rule_id,)).fetchall()
        return [{"market_id": r[0], "token_id": r[1]} for r in rows]

    def automation_run_stats(self, rule_id: str, *, at: int) -> dict:
        """Placements and notional today, from the run log only. Recomputed rather than cached because a
        counter that drifts from the log it summarises is the bug that makes a cap unenforceable."""
        day_start = (at // 86_400_000) * 86_400_000
        rows = self.conn.execute("SELECT outcome,intent_id FROM automation_runs WHERE rule_id=? AND at_ms>=?",
                                 (rule_id, day_start)).fetchall()
        placed = [r[1] for r in rows if r[0] == "placed" and r[1]]
        notional = 0
        if placed:
            marks = ",".join("?" * len(placed))
            notional = sum(int(x[0] or 0) for x in self.conn.execute(
                "SELECT notional_micro FROM order_intents WHERE id IN (%s)" % marks, placed))
        return {"runs": len(rows), "placed": len(placed), "notional_micro": notional}

    def last_human_order_ms(self, user_id: str, *, at: int, window_ms: int = 900_000) -> int:
        """The most recent HUMAN order in the window, for D6.4's "a person outranks a cron job" rule. Reads
        `order_directives.audience`, which is where the intent records who asked for it; a rule cannot fake
        its way to 'user' because it writes through `enqueue_intent` with audience set by the caller."""
        r = self.conn.execute("SELECT MAX(i.created_ms) FROM order_intents i JOIN order_directives d "
                              "ON d.intent_id=i.id WHERE i.user_id=? AND d.audience='user' AND i.created_ms>=?",
                              (user_id, at - window_ms)).fetchone()
        return int(r[0] or 0)

    def set_rule_enabled(self, rule_id: str, *, enabled: bool) -> None:
        self.conn.execute("UPDATE automation_rules SET enabled=? WHERE id=?", (1 if enabled else 0, rule_id))

    def mark_dry_run(self, rule_id: str, at: int) -> None:
        self.conn.execute("UPDATE automation_rule_policy SET dry_run_completed_ms=? WHERE rule_id=?",
                          (at, rule_id))

    def set_rule_last_fire(self, rule_id: str, at: int) -> None:
        self.conn.execute("UPDATE automation_rule_policy SET last_fire_ms=? WHERE rule_id=?", (at, rule_id))


# `alert` is the D6 automation action that never touches the venue. It shares the queue because a rule's
# message and a fill's message compete for the same user's attention, and two queues means two priority
# rules, which means one of them is advisory.
ORDER_EVENTS = ("queued", "submitted", "live", "partial_fill", "filled", "cancelled", "rejected",
                "expired", "unknown", "reconciled", "position_closed", "alert")
