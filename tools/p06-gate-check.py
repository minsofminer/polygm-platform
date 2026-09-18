#!/usr/bin/env python3
"""P06 Quality Gate — the trading plane. Every rule below is EXECUTED against a real SQLite database, the real
risk gate, the real venue pre-flight, the real executor and the mock venue. Nothing here reads prose and nods.

The phase's acceptance line is a pair of measurements, not a list of features: an order killed mid-flight must
come back as exactly one venue order, and a kill switch must stop NEW orders in under a second while leaving
what is already on the book alone. Both are measured here; under `--live` the first is measured against a
process that is really SIGKILLed by `tools/p06-chaos-test.py`, and the artifact it records is re-read by
`c_mid_flight_recovery_is_proved` so the claim in the doc cannot outrun the file.

Checks 1-10 re-run P04's gate in P04's recorded order, but *through the executor* instead of against
`risk.gate` directly. That is the point of re-running them here: P04 proved the gate refuses, P06 claims the
gate is unavoidable. A copy engine and an automation rule reach the venue only through the same queue, so they
pass P04's checks for the same reason a user does — there is no other door. If any of 1-10 fails, P06 broke
P04, and a phase that breaks the phase before it is not done whatever its own tests say.

`TMPDIR` is pinned inside the workspace: the suite builds a SQLite database per test, ~475 of them, and a 1 GB
`/tmp` fills up and dies with a stdout-flush error (exit 120) that looks exactly like a crash in the product.
It was not the product. It will not be the product next time either, so the pin lives here, in the tool that
runs the suite.

Format of a check: (label, ok, detail). Detail is required even on success, because a check that only speaks
when it fails is indistinguishable from one that never ran.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace as dc_replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = ROOT / "docs" / "verification" / "P06-chaos-output.txt"
DOC = ROOT / "docs" / "P06-trading-plane.md"
PY = sys.executable
TMP = ROOT / ".tmp"

# P04's recorded order, and the code each case must produce through this path.
P04_ORDER: tuple[tuple[str, str], ...] = (
    # The code names are the gate's own vocabulary, read from `risk/gate.py` — not a friendlier set invented
    # here. The kill refusal is `RISK_HALT` with retryable=True ("trading is temporarily disabled", and no
    # explanation, because a queue of retries against a system already in trouble is the wrong answer).
    ("kill_switch", "RISK_HALT"),
    ("market_state", "MARKET_NOT_ACCEPTING"),
    ("freshness", "STALE_QUOTE"),
    ("side", "BAD_SIDE"),
    ("tick_alignment", "OFF_TICK"),
    ("min_size", "BELOW_MIN_SIZE"),
    ("notional", "OVER_ORDER_CAP"),
    ("price_band", "PRICE_FAR_FROM_MID"),
    ("user_limits", "DAILY_CAP"),
)


def sh(argv: list[str], timeout: int = 1200, env: dict | None = None) -> tuple[int, str]:
    e = dict(os.environ)
    e["PYTHONPATH"] = ":".join(str(ROOT / p) for p in ("packages", "services/api", "tools", "services/executor",
                                                        "services/executor-mock", "tests"))
    e["TMPDIR"] = str(TMP)
    e.update(env or {})
    TMP.mkdir(exist_ok=True)
    try:
        p = subprocess.run(argv, cwd=str(ROOT), env=e, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "(timed out after %ds)" % timeout
    return p.returncode, p.stdout + p.stderr


class Plane:
    """One trading plane in a temp database: seeded market, funded user, mock venue, executor, reconciler,
    copy engine, automation engine. Rebuilt for every check on purpose — 30 checks sharing one database would
    make each result depend on the previous check's side effects, which is how a gate starts passing for a
    reason nobody can name."""

    def __init__(self) -> None:
        TMP.mkdir(exist_ok=True)
        sys.path[:0] = [str(ROOT / "packages"), str(ROOT / "services" / "api"),
                        str(ROOT / "services" / "executor"), str(ROOT / "services" / "executor-mock"),
                        str(ROOT / "tests"), str(ROOT / "tools")]
        from conftest import apply_schema
        self.db = str(TMP / ("p06-gate-%d.db" % os.getpid()))
        for path in (self.db, self.db + "-wal", self.db + "-shm"):
            if os.path.exists(path):
                os.unlink(path)
        apply_schema(self.db)
        import seed
        seed.seed_sqlite(self.db)
        import mock_clob
        from store import Store, now_ms
        from polygm_core.wallets import lifecycle as wl
        from polygm_core.venue import clob_v2 as v2
        self.now_ms, self.v2, self.wl = now_ms, v2, wl
        self.store = Store.open(self.db)
        self.mock = mock_clob.MockClob()
        self.tp = mock_clob.ScenarioTransport(self.mock)
        self.policy = wl.Policy(allowed_spender="0xexchange")
        spec = importlib.util.spec_from_file_location("polygm_p06_gate_main",
                                                      str(ROOT / "services" / "executor" / "main.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod                    # @dataclass resolves annotations through sys.modules
        spec.loader.exec_module(mod)
        self.main = mod
        self.ex = mod.Executor(self.store, transport=self.tp, policy=self.policy, batch_size=5)
        row = self.store.conn.execute("SELECT id FROM markets WHERE accepting_orders=1 AND "
                                      "enable_order_book=1 ORDER BY id LIMIT 1").fetchone()
        self.market = row[0]
        self.token = self.store.conn.execute("SELECT token_id FROM tokens WHERE market_id=? ORDER BY token_id"
                                            " LIMIT 1", (self.market,)).fetchone()[0]
        self.user = "u-demo"
        self.at = now_ms()
        self.fund()

    # ------------------------------------------------------------------ fixtures that write real rows
    def fund(self, *, balance: int = 100_000_000, allowance: int | None = None, wallet_state: str = "trading",
             policy=None) -> None:
        pol = policy or self.policy
        self.store.conn.execute("INSERT OR REPLACE INTO balances (user_id,usdc_available_micro,"
                                "usdc_locked_micro,version,reconcile_ms) VALUES (?,?,?,?,?)",
                                (self.user, balance, 0, 1, self.at))
        self.store.set_allowance(user_id=self.user, token="pUSD", spender="0xexchange",
                                amount_micro=self.v2.UNLIMITED_ALLOWANCE if allowance is None else allowance,
                                at=self.at)
        self.store.conn.execute("INSERT OR REPLACE INTO wallets (user_id,provider,custody,address,"
                                "proxy_address,signature_type,policy_hash,state,created_ms,updated_ms) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (self.user, "turnkey", "delegated", "0xwallet", "0xproxy", 3,
                                 pol.policy_hash(), wallet_state, self.at, self.at))
        self.store.conn.commit()

    def queue(self, *, price: int | None = None, size: int = 100 * 10**6, side: str = "BUY", key: str,
              order_type: str = "GTC", audience: str = "user", builder_bps: int = 100, fee_bps: int = 0,
              state: str = "queued", max_slippage_bps: int = 0, expiration_ts: int = 0) -> str:
        """Write the intent the way the API does, so the executor sees a row it could really have been given."""
        from polygm_core.money.cents import notional_floor
        iid = "i-" + key
        p = self.mid() if price is None else p_default(price)
        notional = notional_floor(size, p)
        self.store.conn.execute("INSERT OR REPLACE INTO order_intents (id,user_id,market_id,token_id,side,"
                                "price_micro,size_micro,notional_micro,state,idempotency_key,created_ms,"
                                "updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                (iid, self.user, self.market, self.token, side, p, size, notional, state, key,
                                 self.at, self.at))
        self.store.conn.execute("INSERT OR REPLACE INTO order_directives (intent_id,order_type,expiration_ts,"
                                "builder_bps,fee_rate_bps,audience,max_slippage_bps,all_in_limit_micro,"
                                "created_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                                (iid, order_type, expiration_ts, builder_bps, fee_bps, audience,
                                 max_slippage_bps, notional * 2, self.at))
        self.store.conn.commit()
        return iid

    def mid(self) -> int:
        q = self.store.market_quote(self.market, at=self.at)
        bid, ask = int(q["best_bid_micro"] or 0), int(q["best_ask_micro"] or 0)
        return ((bid + ask) // 2) if (bid and ask) else 500_000

    def run(self, key: str, **kw) -> dict:
        self.queue(key=key, **kw)
        rep = self.ex.tick(at=self.at, reconcile=False)
        return rep["handled"][0] if rep["handled"] else {"state": "unclaimed", "code": "NOT_CLAIMED",
                                                        "notes": list(rep.get("notes") or [])}

    def kill(self, *, on: bool) -> None:
        self.store.conn.execute("INSERT INTO kill_switch_state (engaged,reason,changed_by,at_ms) "
                                "VALUES (?,?,?,?)", (1 if on else 0, "p06 gate", "p06-gate", self.at))
        self.store.conn.commit()
        self.store.kill_switch_engaged(at_ms=self.at, force=True)

    def tight_book(self) -> None:
        """An on-grid 49/50 ladder on the market under test, and a resolution clock.

        The seeded 0xM1 ladder sits at 0.94-0.97 and its best ask is NOT on that market's 0.01 tick, so a
        fixture that inherits it tests the seed: every copied order becomes an OFF_TICK refusal and the rule
        under test is never reached. Anything that copies or automates calls this first.
        """
        self.store.conn.execute("DELETE FROM book_levels WHERE market_id=?", (self.market,))
        for side, price in (("bid", 490_000), ("ask", 500_000)):
            self.store.conn.execute("INSERT INTO book_levels (market_id,side,price_micro,size_shares_micro,"
                                    "level_count,updated_ms) VALUES (?,?,?,?,?,?)",
                                    (self.market, side, price, 100 * 10**6, 1, self.at))
        self.store.conn.execute("UPDATE markets SET end_ts=? WHERE id=?", (self.at // 1000 + 86_400,
                                                                          self.market))
        self.store.conn.commit()

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = self.store.conn.execute(sql, params)
        keys = [d[0] for d in cur.description]
        return [dict(zip(keys, r)) for r in cur.fetchall()]

    def close(self) -> None:
        try:
            self.store.close()
        except Exception:                                     # noqa: BLE001 - cleanup only
            pass


def p_default(price: int | None) -> int:
    return 500_000 if price is None else price


# ============================================================================ P04's gate, unavoidable ==
def c_p04_gate_order_through_the_executor(p: Plane) -> tuple[str, bool, str]:
    """P04's nine hard checks, in P04's order, each tripped by a queued intent and read back from the outcome."""
    seen: list[str] = []
    notes_detail = ""
    for name, want in P04_ORDER:
        if name == "kill_switch":
            # Two claims, and the tick alone cannot prove both: with the switch on the queue claims nothing
            # (so nothing is even read), and an intent already in the pilot's hands is refused at the door.
            p.kill(on=True)
            posts_before = p.tp.post_calls
            iid = p.queue(key="g-kill")
            rep_tick = p.ex.tick(at=p.at, reconcile=False)
            direct = p.ex.handle_intent(p.store.load_intent(iid), at=p.at)
            p.kill(on=False)
            o = direct.as_dict()
            if rep_tick["handled"]:
                seen.append("kill_switch claimed %d intents while engaged" % len(rep_tick["handled"]))
            if p.tp.post_calls != posts_before:
                seen.append("kill_switch let %d orders reach the venue" % (p.tp.post_calls - posts_before))
        elif name == "market_state":
            p.store.conn.execute("UPDATE markets SET accepting_orders=0 WHERE id=?", (p.market,))
            o = p.run("g-market")
            p.store.conn.execute("UPDATE markets SET accepting_orders=1 WHERE id=?", (p.market,))
        elif name == "freshness":
            p.store.conn.execute("UPDATE book_levels SET updated_ms=?", (p.at - 40_000_000,))
            o = p.run("g-stale")
            p.store.conn.execute("UPDATE book_levels SET updated_ms=?", (p.at,))
        elif name == "side":
            # The database refuses a bad side on the way in, and the gate refuses it again if a row somehow
            # carries one (a migration that lost the CHECK, a hand-edited row). Both layers, because the
            # consequence of neither being there is a signed order at a venue that rejects it.
            iid = p.queue(key="g-side", size=6 * 10**6)
            try:
                p.store.conn.execute("UPDATE order_intents SET side='HOLD' WHERE id=?", (iid,))
                p.store.conn.commit()
                seen.append("the schema let a row become side='HOLD'")
            except sqlite3.IntegrityError:
                pass
            o = p.ex.handle_intent(dc_replace(p.store.load_intent(iid), side="HOLD"), at=p.at).as_dict()
        elif name == "tick_alignment":
            o = p.run("g-tick", price=p.mid() + 5_000)
        elif name == "min_size":
            o = p.run("g-min", size=10**6)
        elif name == "notional":
            o = p.run("g-notional", size=10_000 * 10**6)
        elif name == "price_band":
            o = p.run("g-band", price=min(p.mid() + 300_000, 990_000), size=6 * 10**6)
        elif name == "user_limits":
            for i in range(6):                          # 6 x $5000 of 24h spend > the $25k ceiling
                p.store.conn.execute("INSERT INTO order_intents (id,user_id,market_id,token_id,side,"
                                     "price_micro,size_micro,notional_micro,state,idempotency_key,created_ms,"
                                     "updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                     ("i-hist-%d" % i, p.user, p.market, p.token, "BUY", 500_000,
                                      10_000 * 10**6, 5_000_000_000, "submitted", "hist-%d" % i, p.at, p.at))
            p.store.conn.commit()
            o = p.run("g-limit", size=6 * 10**6)
            # ...and the fixture must not leak into the next case: the delayed-market probe below has to be
            # refused for the delay, not for the six $5000 intents this case planted.
            p.store.conn.execute("DELETE FROM order_intents WHERE id LIKE 'i-hist-%'")
            p.store.conn.commit()
        if o.get("code") != want:
            seen.append("%s=%s(want %s)" % (name, o.get("code"), want))
        else:
            seen.append("%s=%s" % (name, want))
    # the 10th is advisory by design: a legal order and a loud note, never a denial
    p.store.conn.execute("UPDATE markets SET seconds_delay=60 WHERE id=?", (p.market,))
    d = p.run("g-delay", size=6 * 10**6)
    p.store.conn.execute("UPDATE markets SET seconds_delay=0 WHERE id=?", (p.market,))
    delay_ok = d.get("code") == "OK" and any("delayed_open" in str(n) for n in d.get("notes", []))
    if not delay_ok:
        notes_detail = "delayed market gave %s with notes %s" % (d.get("code"), d.get("notes"))
    ok = all("(want" not in s for s in seen) and delay_ok
    return ("P04's gate order re-run through the executor (9 denials + 1 advisory)", ok,
            "; ".join(seen) + ("" if delay_ok else "; " + notes_detail))


def c_preflight_sequence_is_complete(p: Plane) -> tuple[str, bool, str]:
    """Preflight must run all ten checks in order, and a shorter list must stop signing."""
    o = p.run("pf-ok", size=6 * 10**6)
    trail = [r["state"] for r in p.rows("SELECT state FROM order_lifecycle WHERE intent_id=? ORDER BY id",
                                       ("i-pf-ok",))]
    ok = o.get("code") == "OK" and "preflight" in trail and len(p.v2.PREFLIGHT_ORDER) == 10
    short_refused, code2 = False, ""
    orig_preflight = p.main.v2.preflight
    try:
        # Simulate "one check stopped being called" without editing the product: preflight returns a shorter
        # checks_run than the sequence promises. The executor must notice and refuse to sign.
        def short(**kw):
            r = orig_preflight(**kw)
            return dc_replace(r, checks_run=tuple(r.checks_run[:-1]))
        p.main.v2.preflight = short
        o2 = p.run("pf-short", size=6 * 10**6)
        code2 = str(o2.get("code"))
        short_refused = code2 == "PREFLIGHT_INCOMPLETE" and p.tp.post_calls == 1
    except Exception as e:                                    # noqa: BLE001 - a crash here is a finding
        code2 = "gate raised %s: %s" % (type(e).__name__, str(e)[:60])
    finally:
        p.main.v2.preflight = orig_preflight
    ok = bool(ok and short_refused)
    return ("preflight runs all 10 checks in order, and a shorter list stops signing", ok,
            "%d checks in the sequence; clean order %s (trail %s); with one check silently dropped: %s"
            % (len(p.v2.PREFLIGHT_ORDER), o.get("code"), trail, code2))


def c_single_door(p: Plane) -> tuple[str, bool, str]:
    """Copy and automation have no path to the venue but the queue. Read the files: a comment about a door is
    not a door."""
    offenders: list[str] = []
    needles = ("post_order(", "post_orders(", "PlaceOrderArgs(", "cancel_all(", "MockClob", "ScenarioTransport",
               "httpx", "requests.")
    for rel in ("packages/polygm_core/copy/engine.py", "packages/polygm_core/automation/engine.py"):
        text = (ROOT / rel).read_text()
        offenders += ["%s references %s" % (rel, n) for n in needles if n in text]
        if "enqueue_intent" not in text:
            offenders.append("%s does not go through enqueue_intent" % rel)
    src = (ROOT / "services" / "executor" / "store.py").read_text()
    body = src[src.index("def enqueue_intent"):]
    body = body[:body.index("\n    def ", 5)]
    stamps = "audience" in body
    ok = not offenders and stamps
    return ("copy/automation reach the venue only through enqueue_intent", ok,
            "; ".join(offenders) if offenders else
            "neither engine imports a venue client or an HTTP library; the queue stamps `audience` itself (%s)"
            % stamps)


def c_batch_cap_and_partial_failure(p: Plane) -> tuple[str, bool, str]:
    """The venue takes 15 per POST, our planner must not ask for 16, and a missing item is UNCERTAIN."""
    v2 = p.v2
    items = [v2.BatchItem(order={"intent_id": "i%d" % i, "client_order_hash": "0x%02x" % i},
                         order_type="GTC", intent_id="i%d" % i) for i in range(31)]
    sizes = [len(c) for c in v2.plan_batch(items)]
    try:
        v2.assert_batch_size(16)
        cap_ok, why = False, "assert_batch_size(16) did NOT raise"
    except v2.BatchTooLarge as e:
        cap_ok, why = True, "BatchTooLarge: %s" % str(e)[:40]
    # the venue's own success shape, item by item, positionally
    out = v2.read_batch_response(["a", "b", "c"], ["h1", "h2", "h3"],
                                {"orders": [{"success": True, "orderID": "0xA"},
                                            {"success": False, "code": "bad_request", "message": "no"}]})
    counts = out.counts
    # three asked for, two answered for: one accepted, one rejected, and the third UNCERTAIN, because "no
    # answer" is not "no order" -- treating it as absent is what double-spends on the retry
    ok = sizes == [15, 15, 1] and cap_ok and counts == {"accepted": 1, "rejected": 1, "uncertain": 1}
    return ("venue batch cap enforced at 15; a short response is uncertain, not fine", ok,
            "chunks %s; %s; response counts %s" % (sizes, why, counts))


def c_cancel_budget_and_phrase(p: Plane) -> tuple[str, bool, str]:
    """250 cancels per 10 s, shared across users; cancel-all costs a typed phrase and a real reason."""
    v2 = p.v2
    b = v2.CancelBudget()
    t0 = 10**12
    took = sum(1 for _ in range(v2.CANCEL_ALL_BUDGET_COUNT + 50) if b.take(t0, cost=1)[0])
    denied, wait_ms = b.take(t0, cost=1)
    next_window, _ = b.take(t0 + v2.CANCEL_ALL_BUDGET_WINDOW_MS + 1, cost=1)
    budget_ok = took == v2.CANCEL_ALL_BUDGET_COUNT and not denied and wait_ms > 0 and next_window
    try:
        v2.CancelRequest(scope="all", reason="oops").validate()
        phrase = "not required"
    except ValueError as e:
        phrase = str(e)[:44]
    req = v2.CancelRequest(scope="all", reason="incident: venue is mispricing the tape", actor="ops@polygm",
                          confirm_phrase=v2.CONFIRM_PHRASE)
    req.validate()
    audit = req.audit_row(p.at)
    chunks = v2.chunk_cancel_targets([str(i) for i in range(40)])
    ok = bool(budget_ok and phrase != "not required" and audit.get("scope") == "all" and len(chunks) == 3
              and "reason" in json.dumps(audit))
    return ("cancel budget %d/%ds and a typed confirmation for scope=all"
            % (v2.CANCEL_ALL_BUDGET_COUNT, v2.CANCEL_ALL_BUDGET_WINDOW_MS // 1000), ok,
            "took %d then refused (wait %d ms), next window allowed=%s; no phrase: %s; audit keys %s; chunks %s"
            % (took, wait_ms, next_window, phrase, sorted(audit)[:6], [len(c) for c in chunks]))


def c_fee_maths_is_the_venues(p: Plane) -> tuple[str, bool, str]:
    """Fees are integers, rounded the venue's way, stored per order, and comparable to the actual in bps."""
    v2 = p.v2
    size, price = 100 * 10**6, 500_000
    f = v2.estimate_fees(size_shares_micro=size, price_micro=price, fee_rate_bps=70, builder_bps=100)
    notional = price * size // 10**6
    odd = v2.estimate_fees(size_shares_micro=10**6, price_micro=500_003, fee_rate_bps=0, builder_bps=100)
    raw = size * price * (10**6 - price) * 70
    div = 10**6 * 10**6 * 10_000
    checks = {
        "platform_rounded_up": f.platform_micro == raw // div + (1 if raw % div else 0),
        "builder_is_bps_on_notional": f.builder_micro == notional * 100 // 10_000,
        # and the case that actually tests the direction: `notional x bps` with a remainder. `estimate_fees`
        # is what the UI quotes and what the order carries, so a truncated builder fee is an under-quote.
        "builder_fee_rounds_up": odd.builder_micro == -(-500_003 * 100 // 10_000) == 5001,
        "integers_only": all(isinstance(x, int) for x in (f.platform_micro, f.builder_micro)),
        "zero_fee_market_is_zero": v2.estimate_fees(size_shares_micro=10**6, price_micro=price,
                                                    fee_rate_bps=0, builder_bps=0).total_micro == 0,
        # positive means we UNDER-estimated, which is the direction that costs the user a rejected order
        "accuracy_is_signed_bps": (v2.fee_accuracy_bps(1_000_000, 1_010_000) == 100
                                   and v2.fee_accuracy_bps(1_000_000, 990_000) == -100
                                   and v2.fee_accuracy_bps(0, 500_000) == 10_000),
    }
    o = p.run("fee-1", price=price, size=size, fee_bps=70)
    fee_row = p.rows("SELECT * FROM venue_fees WHERE order_id=?", (o.get("order_id") or "",))
    if fee_row:
        r = p.store.record_fee_actual(order_id=o["order_id"],
                                     platform_micro=int(fee_row[0]["est_platform_micro"]),
                                     builder_micro=600_000, at=p.at)
        checks["stored_and_comparable"] = int(fee_row[0]["est_builder_micro"]) == 500_000 \
            and r["delta_micro"] == 100_000 and abs(r["accuracy_bps"]) > 100
    else:
        checks["stored_and_comparable"] = False
    bad = [k for k, v in checks.items() if not v]
    return ("fee maths is integer, venue-rounded, stored per order and reconcilable", not bad,
            "platform %d, builder %d on notional %d; %s" % (f.platform_micro, f.builder_micro, notional,
                                                            "all six properties hold" if not bad
                                                            else "FAILED " + ", ".join(bad)))


def c_allowance_and_all_in_number(p: Plane) -> tuple[str, bool, str]:
    """Unlimited approval is the sentinel the venue expects, it must fit the signed 64-bit column, and a short
    approval parks the order instead of losing it."""
    v2 = p.v2
    fits = 0 < v2.UNLIMITED_ALLOWANCE < 2**63
    a = v2.amounts_for("BUY", price_micro=500_000, size_shares_micro=6 * 10**6)
    amt = a.notional_micro == 3_000_000 and str(a.taker_amount_raw).isdigit()
    covers = (v2.allowance_covers(v2.UNLIMITED_ALLOWANCE, 10**12) and not v2.allowance_covers(1_000, 10**12))
    asks = [(500_000, 10 * 10**6)] * 20
    roomy = v2.all_in_spend_limit(asks, want_shares_micro=100 * 10**6, fee_rate_bps=70, builder_bps=100,
                                 limit_micro=10**9)
    tight = v2.all_in_spend_limit(asks, want_shares_micro=100 * 10**6, fee_rate_bps=70, builder_bps=100,
                                 limit_micro=1_000_000)
    limit_ok = (roomy["capped_by_limit"] is False and tight["capped_by_limit"] is True
                and roomy["all_in_cost_micro"] > 100 * 10**6 * 500_000 // 10**6 * 100 // 10_000)
    p.fund(balance=100_000_000, allowance=10_000)
    o = p.run("allow-short", price=500_000, size=6 * 10**6)
    parked = o.get("code") == "ALLOWANCE_REQUIRED" and p.tp.post_calls == 0
    p.fund(balance=100_000_000)
    o2 = p.run("allow-ok", price=500_000, size=6 * 10**6)
    ok = bool(fits and amt and covers and limit_ok and parked and o2.get("code") == "OK")
    return ("unlimited allowance sentinel, all-in spend limit, and a short approval parks rather than posts",
            ok, "sentinel is %d bits (fits int64: %s); USDC/share legs %s; allowance %s; all-in %d micro with "
            "room, %d micro against a $1 cap; short approval -> %s, restored -> %s"
            % (v2.UNLIMITED_ALLOWANCE.bit_length(), fits, (a.notional_micro, a.taker_amount_raw),
               "unlimited covers 1e12, 1 cent does not" if covers else "WRONG", roomy["all_in_cost_micro"],
               tight["all_in_cost_micro"], o.get("code"), o2.get("code")))


# ==================================================================================== wallets (D1) ====
def c_wallet_state_machine(p: Plane) -> tuple[str, bool, str]:
    """No stranded wallets: every hop is legal-or-refused in code, and the store refuses what the machine does."""
    wl = p.wl
    states = [s.value for s in wl.WalletState]
    legal = [(a, b) for a in states for b in wl.TRANSITIONS.get(a, ())]
    illegal = [(a, b) for a in states for b in states if (a, b) not in legal]
    for a, b in illegal:
        try:
            wl.assert_transition(a, b)
            return ("wallet states cannot be jumped, and every hop is an event", False,
                    "assert_transition allowed the illegal hop %s>%s" % (a, b))
        except ValueError:
            pass
    self_loops = [x for x in legal if x[0] == x[1]]
    p.store.set_wallet_state(p.user, "suspended", at=p.at, event="suspended")
    try:
        # `suspended -> provisioned` is not in TRANSITIONS (only trading/closing are), so this must refuse.
        p.store.set_wallet_state(p.user, "provisioned", at=p.at, event="provisioned")
        jump = "ACCEPTED BY THE STORE"
        p.store.set_wallet_state(p.user, "trading", at=p.at, event="reinstated")
    except ValueError as e:
        jump = "refused: %s" % str(e)[:36]
        p.store.set_wallet_state(p.user, "trading", at=p.at, event="reinstated")
    trail = p.rows("SELECT event FROM wallet_events WHERE user_id=? ORDER BY id", (p.user,))
    ddl = p.store.conn.execute("SELECT sql FROM sqlite_master WHERE name='wallet_events'").fetchone()[0]
    words = set(re.findall(r"'([a-z_]+)'", ddl))
    ok = bool(illegal) and not self_loops and jump.startswith("refused") and len(trail) >= 2 \
        and all(t["event"] in words for t in trail)
    return ("wallet states cannot be jumped, and every hop is an event", ok,
            "%d legal / %d refused of %d pairs; store on a suspended->trading hop: %s; %d wallet_events rows; "
            "self-loops %d; every wallet_events row uses one of the %d schema words"
            % (len(legal), len(illegal), len(states) ** 2, jump, len(trail), len(self_loops), len(words)))


def c_policy_drift_suspends_and_export_is_gated(p: Plane) -> tuple[str, bool, str]:
    """A provider policy that no longer hashes to what we provisioned stops trading; export warns, and open
    orders or a stuck deposit block it."""
    wl = p.wl
    here, moved = wl.Policy(allowed_spender="0xexchange"), wl.Policy(allowed_spender="0xother")
    same, drift = wl.check_policy_drift(here.policy_hash(), here), wl.check_policy_drift(here.policy_hash(),
                                                                                          moved)
    drift["fields"] = drift.get("fields") or [k for k in ("allowed_spender",) if getattr(here, k)
                                             != getattr(moved, k)]
    refuse, why = wl.should_refuse_trading({"policy_hash": here.policy_hash(), "state": "trading"}, moved)
    # and the executor must catch it at the last moment before a key is used
    p.store.conn.execute("UPDATE wallets SET policy_hash=? WHERE user_id=?", (here.policy_hash(), p.user))
    p.store.conn.commit()
    p.ex.policy = moved
    o = p.run("drift-order", size=6 * 10**6)
    blocked = o.get("state") == "rejected" and p.tp.post_calls == 0
    suspended = p.rows("SELECT state FROM wallets WHERE user_id=?", (p.user,))[0]["state"] == "suspended"
    p.ex.policy = p.policy
    kw = dict(password_ok=True, open_orders=0, unsettled_intents=0, in_flight_withdrawals=0,
              has_deposit_stuck=False)
    plan_blocked = wl.plan_export(**{**kw, "open_orders": 1})
    plan_stuck = wl.plan_export(**{**kw, "has_deposit_stuck": True})
    plan_ok = wl.plan_export(**kw)
    digests = {wl.export_digest("0x" + "ab" * 20), wl.export_digest("0x" + "ab" * 20),
               wl.export_digest("0x" + "cd" * 20)}
    ok = bool(same["ok"] is True and drift["ok"] is False and drift["code"] == "POLICY_DRIFT"
              and "suspend" in drift["action"] and refuse and blocked and suspended
              and not plan_blocked["ok"] and not plan_stuck["ok"] and plan_ok["ok"]
              and wl.EXPORT_WARNING in plan_ok["warning"] and len(digests) == 2
              and "never emailed" in plan_ok["delivered_via"])
    return ("policy drift suspends the wallet before signing; export is warned and blocked while busy", ok,
            "identical ok=%s, moved ok=%s(%s); executor said %s and wallet=%s; export with an open order %s/%s, "
            "stuck deposit %s, digest stable %s"
            % (same["ok"], drift["ok"], drift["code"], o.get("code"),
               "suspended" if suspended else "STILL TRADING",
               plan_blocked["ok"], plan_blocked["code"], plan_stuck["code"], len(digests) == 1))


def c_deposit_lifecycle(p: Plane) -> tuple[str, bool, str]:
    """The minimum economic deposit is computed from the gas bill, and a deposit that stops moving becomes an
    alarm with a user-facing sentence instead of a silent spinner."""
    wl = p.wl
    bill = wl.ChainCost(gas_units=60_000, gas_price_wei=40_000_000_000, native_price_micro=550_000)
    cost, floor = wl.estimate_deposit_cost(bill, legs=3), wl.minimum_economic_deposit(bill)
    d = wl.Deposit(id=1, user_id="u", chain="polygon", asset="USDC", amount_micro=5_000_000,
                   status="bridging", tx_hash="0xT", first_seen_ms=1_000)
    stuck, stuck_msg = wl.advance_deposit(d, now_ms=1_000 + wl.DEPOSIT_STUCK_AFTER_MS + 1)
    errored, err_msg = wl.advance_deposit(wl.Deposit(id=2, user_id="u", chain="polygon", asset="USDC",
                                                     amount_micro=5_000_000, status="crediting", tx_hash="0xT",
                                                     first_seen_ms=1_000),
                                         now_ms=1_000 + wl.DEPOSIT_STUCK_AFTER_MS + 1, error="rpc 503")
    young, _ = wl.advance_deposit(d, now_ms=1_000 + 60_000)
    ok = bool(cost["total_micro"] > 0 and floor["min_deposit_micro"] >= 10 * cost["total_micro"]
              and stuck.status == "stuck" and "delayed, not lost" in wl.deposit_user_note(stuck)
              and errored.status == "stuck" and young.status != "stuck"
              and "credit_key" in stuck_msg + err_msg)
    return ("deposit floor is >= 10x the measured gas bill, and a stalled deposit says so", ok,
            "3 legs %d micro, floor %d micro (%.1fx); bridging past the horizon -> %s; error after the horizon "
            "-> %s; inside the horizon -> %s"
            % (cost["total_micro"], floor["min_deposit_micro"],
               floor["min_deposit_micro"] / max(1, cost["total_micro"]), stuck.status, errored.status,
               young.status))


def c_withdrawal_path_is_ordered(p: Plane) -> tuple[str, bool, str]:
    """Every denial on the way out is coded and ordered, and the notification requirement is not a flag."""
    wl = p.wl
    pol = wl.Policy(max_single_withdrawal_micro=50_000_000, max_daily_outflow_micro=80_000_000,
                    threshold_approval_above_micro=25_000_000)
    cooldown = pol.allowlist_cooldown_ms

    def req(amount: int, addr: str = "0xEarn", typed_amount: str = "", typed_addr: str = ""):
        return wl.WithdrawalRequest(user_id="u", amount_micro=amount, dest_address=addr,
                                   typed_amount=typed_amount or str(amount / 10**6),
                                   typed_address=typed_addr or addr)

    def ctx(**kw):
        base = dict(password_ok=True, allowlist=(("0xEarn", 0),), now_ms=10**12, available_micro=10**9,
                    fee_micro=10_000, withdrawn_today_micro=0, open_orders=0, policy=pol,
                    notify_email_sent=True, notify_telegram_sent=True)
        base.update(kw)
        return wl.WithdrawalContext(**base)

    cases = {
        "no_password": (req(10_000_000), ctx(password_ok=False), "NOT_AUTHENTICATED"),
        "typed_amount_liar": (req(10_000_000, typed_amount="1000.00"), ctx(), "TYPED_AMOUNT_MISMATCH"),
        "typed_address_liar": (req(10_000_000, typed_addr="0xOther"), ctx(), "TYPED_ADDRESS_MISMATCH"),
        "not_allowlisted": (req(10_000_000, addr="0xStranger"), ctx(), "ADDRESS_NOT_ALLOWLISTED"),
        "in_cooldown": (req(10_000_000), ctx(allowlist=(("0xEarn", 10**12 + cooldown),)), "ADDRESS_COOLDOWN"),
        "over_single_cap": (req(60_000_000), ctx(), "AMOUNT_OVER_SINGLE_CAP"),
        "over_daily_cap": (req(40_000_000), ctx(withdrawn_today_micro=60_000_000), "DAILY_WITHDRAWAL_CAP"),
        "open_orders": (req(10_000_000), ctx(open_orders=2), "OPEN_ORDERS_EXIST"),
        "broke": (req(10_000_000), ctx(available_micro=1_000), "INSUFFICIENT_BALANCE"),
        "needs_approver": (req(30_000_000), ctx(), "THRESHOLD_APPROVAL_REQUIRED"),
        "no_notification": (req(10_000_000), ctx(notify_email_sent=False), "NOTIFY_UNCONFIRMED"),
        "clean": (req(10_000_000), ctx(), "QUEUED"),
    }
    verdicts, wrong = {}, []
    for name, (r, c, want) in cases.items():
        got = wl.check_withdrawal(r, c)
        verdicts[name] = got["code"]
        if got["code"] != want:
            wrong.append("%s said %s want %s" % (name, got["code"], want))
    # the balance check comes last on purpose: "not enough money" is itself information
    order_proof = wl.check_withdrawal(req(10_000_000, addr="0xStranger"),
                                     ctx(available_micro=1))["code"] == "ADDRESS_NOT_ALLOWLISTED"
    ok = not wrong and order_proof and all(verdicts.values())
    return ("withdrawal path: 12 ordered cases, each refusing for its own coded reason", ok,
            " ".join("%s=%s" % kv for kv in verdicts.items())
            + ("" if not wrong else " ; WRONG " + "; ".join(wrong))
            + ("" if order_proof else " ; balance leaks before the allowlist answer"))


# ============================================================ reconcile (D3), kill switch, limits (D4) ==
def c_all_eight_cases_are_executed(p: Plane) -> tuple[str, bool, str]:
    """The taxonomy is only real if every case has a handler and a fixture that produces it."""
    from polygm_core.reconcile.reconciler import CASE_ORDER, Reconciler
    handlers = {m[len("case_"):] for m in dir(Reconciler) if m.startswith("case_")}
    map_ok = tuple(sorted(handlers)) == tuple(sorted(CASE_ORDER))
    rc, out = sh([PY, "-W", "ignore::ResourceWarning", "-m", "unittest", "discover", "-s", "tests",
                  "-p", "test_reconciler.py"], timeout=900)
    tally = [ln for ln in out.splitlines() if ln.startswith(("Ran ", "OK", "FAILED"))]
    n = next((int(m.group(1)) for ln in tally for m in [re.match(r"Ran (\d+) tests", ln)] if m), 0)
    ok = rc == 0 and map_ok and n >= len(CASE_ORDER) and any(ln.strip() == "OK" for ln in tally)
    return ("all 8 reconcile cases have a handler AND an executed test", ok,
            "handlers match CASE_ORDER: %s; tests/test_reconciler.py: %d tests, %s"
            % (map_ok, n, " | ".join(tally) or out.strip()[-160:]))


def c_recovery_never_duplicates_a_post(p: Plane) -> tuple[str, bool, str]:
    """The money half of the headline claim, in-process so every gate run checks it: sign, lose the answer, and
    let reconciliation find the order the venue already had."""
    iid = p.queue(key="recover-1")
    p.mock.set_scenario("timeout_after_accept")
    o = p.ex.tick(at=p.at, reconcile=False)["handled"][0]
    posts = p.tp.post_calls
    state = p.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"]
    again = p.ex.tick(at=p.at + 5_000, reconcile=False)              # a second tick must NOT re-sign
    p.ex.reconciler.run_pass(at=p.at + 60_000)
    after = p.rows("SELECT state,venue_order_id AS v FROM order_intents WHERE id=?", (iid,))[0]
    orders = p.rows("SELECT id,intent_id FROM orders")
    trail = [r["state"] for r in p.rows("SELECT state FROM order_lifecycle WHERE intent_id=? ORDER BY id",
                                       (iid,))]
    p.mock.set_scenario("accept")
    # Two halves of the recovery rule live in code the in-process tick does not exercise: the ORDER of the tick
    # (ask the venue before requeuing an expired claim) and the guard that asks "did my POST already happen".
    # Both are asserted on the source shape, because a mutant that swaps or deletes either one is exactly the
    # duplicate order this phase exists to prevent, and a check that cannot see it is not a check.
    src = (ROOT / "services" / "executor" / "main.py").read_text()
    # rfind, not find: the tick has more than one reconcile call and only the one beside the requeue decides
    # whether a lost answer is asked about before an expired claim is handed back out.
    i_rec, i_req = src.rfind("self.reconciler.run_pass(at=t)"), src.find("requeue_expired_claims(at=t)")
    shape_ok = 0 <= i_rec < i_req and "prior_attempt(" in src and "stamp_hash" in src
    ok = bool(shape_ok and state == "uncertain" and again["handled"] == [] and p.tp.post_calls == posts
              and len(orders) == 1 and orders[0]["intent_id"] == iid and after["state"] == "submitted"
              and o["code"] == "UNCERTAIN_INTENT" and "reconciled" in trail)
    return ("an order killed mid-flight recovers to exactly ONE venue order", ok,
            "outcome %s; venue posts %d before recovery, %d after (must be equal); orders %d; intent %s; "
            "trail %s; tick shape (reconcile before requeue, attempt guard) %s"
            % (o.get("code"), posts, p.tp.post_calls, len(orders), after["state"], trail, shape_ok))


def c_lookup_failure_is_not_absence(p: Plane) -> tuple[str, bool, str]:
    """A venue that will not answer is not a venue that says 'no such order': nothing is cancelled, no sighting
    is counted, and the pass says so out loud."""
    from polygm_core.reconcile.reconciler import Cfg, Reconciler
    iid = p.queue(key="lookup-down")
    p.store.conn.execute("UPDATE order_intents SET state='uncertain', updated_ms=? WHERE id=?",
                         (p.at - 20_000, iid))
    p.store.conn.execute("UPDATE order_intents SET state='uncertain', client_order_hash=?, updated_ms=? "
                         "WHERE id=?", ("0x" + "c" * 64, p.at - 20_000, iid))
    p.store.conn.execute("INSERT INTO order_attempts (client_order_hash,intent_id,user_id,token_id,side,"
                         "price_micro,size_micro,order_type,signature_type,builder_code,payload_digest,"
                         "signed_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("0x" + "c" * 64, iid, p.user, p.token, "BUY", 500_000, 6 * 10**6, "GTC", 3,
                          "0x" + "0" * 64, "0x" + "0" * 64, p.at))
    p.store.conn.commit()
    r = Reconciler(p.store, p.tp, cfg=Cfg(no_ack_grace_ms=0, max_sightings=8, ghost_grace_ms=0))

    def broken(client_order_hash, *, timeout_ms):                      # the venue cannot be asked at all
        raise RuntimeError("venue down")

    real_find, p.tp.find_order = p.tp.find_order, broken
    errors = sum(len(r.run_pass(at=p.at + i * 1_000).errors) for i in range(12))
    rows = p.rows("SELECT case_name,attempts,note FROM reconcile_open")
    state = p.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"]
    orders = len(p.rows("SELECT id FROM orders WHERE intent_id=?", (iid,)))
    # The other half: when the venue DOES answer "I have no such order" `max_sightings` times, absence is
    # proven and the intent is closed. A check that only proved the first half would pass a reconciler that
    # never closed anything at all.
    p.tp.find_order = lambda client_order_hash, *, timeout_ms: None
    for i in range(9):
        r.run_pass(at=p.at + 20_000 + i * 1_000)
    state_absent = p.rows("SELECT state FROM order_intents WHERE id=?", (iid,))[0]["state"]
    p.tp.find_order = real_find
    ok = bool(errors >= 12 and state == "uncertain" and orders == 0 and rows
              and all("reach the venue" in x["note"] or "lookups" in x["note"] for x in rows)
              and all(not x["attempts"] for x in rows) and state_absent == "cancelled")
    return ("12 passes against an unreachable venue: no cancel, no counted sighting, loud errors", ok,
            "failed lookups: errors %d, intent state %s, our order rows %d, open cases %s, notes %s; nine "
            "ANSWERED absences instead leave it %s"
            % (errors, state, orders, [(x["case_name"], x["attempts"]) for x in rows],
               [x["note"][:40] for x in rows], state_absent))


def c_fill_is_idempotent(p: Plane) -> tuple[str, bool, str]:
    """`book_fill` is the only way money moves, and the same venue fill three times books a cent once."""
    o = p.run("fill-1", price=500_000, size=100 * 10**6)
    oid, iid = o["order_id"], o["intent_id"]
    before = p.rows("SELECT COUNT(*) AS c FROM cash_ledger")[0]["c"]
    for _ in range(3):
        p.store.book_fill(order_id=oid, intent_id=iid, user_id=p.user, token_id=p.token, market_id=p.market,
                         side="BUY", price_micro=500_000, size_micro=40 * 10**6, fee_micro=1_000,
                         trade_id="0xT-same", exchange_ts=p.at, maker=True, source="reconcile", at=p.at)
    after = p.rows("SELECT COUNT(*) AS c FROM cash_ledger")[0]["c"]
    fills = len(p.rows("SELECT id FROM fills WHERE order_id=?", (oid,)))
    lots = len(p.rows("SELECT id FROM position_lots WHERE user_id=?", (p.user,)))
    matched = int(p.rows("SELECT size_matched_micro FROM orders WHERE id=?", (oid,))[0]["size_matched_micro"])
    p.store.book_fill(order_id=oid, intent_id=iid, user_id=p.user, token_id=p.token, market_id=p.market,
                     side="BUY", price_micro=500_000, size_micro=10 * 10**6, fee_micro=100,
                     trade_id="0xT-other", exchange_ts=p.at + 1, maker=True, source="reconcile", at=p.at)
    matched2 = int(p.rows("SELECT size_matched_micro FROM orders WHERE id=?", (oid,))[0]["size_matched_micro"])
    ok = (after - before) == 1 and fills == 1 and lots == 1 and matched == 40 * 10**6 and matched2 == 50 * 10**6
    return ("one ledger row per venue trade, and a second trade on the same order still books", ok,
            "cash rows %d->%d, fills %d, lots %d, matched %d -> %d" % (before, after, fills, lots, matched,
                                                                      matched2))


def c_kill_switch_latency(p: Plane) -> tuple[str, bool, str]:
    """Engaged -> no new order, measured under a second, in-flight orders untouched. The drill's offline twin:
    `make drill-p06` repeats it against running processes and records the numbers."""
    from polygm_core.risk import limits as lm
    lat, codes = [], []
    for i in range(3):
        p.store.conn.execute("INSERT INTO kill_switch_state (engaged,reason,changed_by,at_ms) VALUES (1,?,?,?)",
                            ("p06 drill", "ops@polygm", p.at))
        p.store.conn.commit()
        iid = p.queue(key="kill-%d" % i)
        t0 = time.perf_counter()
        rep_tick = p.ex.tick(at=p.at, reconcile=False)
        o = p.ex.handle_intent(p.store.load_intent(iid), at=p.at).as_dict()
        lat.append(int(round((time.perf_counter() - t0) * 1000)))
        codes.append(str(o.get("code")))
        if rep_tick["handled"]:
            codes.append("tick claimed %d while engaged" % len(rep_tick["handled"]))
        p.store.conn.execute("INSERT INTO kill_switch_state (engaged,reason,changed_by,at_ms) VALUES (0,?,?,?)",
                            ("cleared by gate", "p06-gate", p.at + 1))
        p.store.conn.commit()
    refused = all(c == "RISK_HALT" for c in codes) and p.tp.post_calls == 0
    under_budget = bool(lat) and max(lat) < lm.KILL_BUDGET_MS
    samples = [lm.DrillSample(component=c, t_zero_ms=p.at, refused_at_ms=p.at + max(lat or [0]),
                             method="gate-probe") for c in lm.COMPONENTS]
    verdict = lm.drill_verdict(samples)
    negative = lm.drill_verdict([lm.DrillSample(component=c, t_zero_ms=p.at + 5_000, refused_at_ms=p.at,
                                               method="gate-probe") for c in lm.COMPONENTS])
    real = p.store.conn

    class Dead:
        def execute(self, *a, **k):
            raise RuntimeError("database is gone")
    p.store.conn = Dead()
    try:
        fail_closed = p.store.kill_switch_engaged(at_ms=p.at + 10**9, force=True) is True
    finally:
        p.store.conn = real
    live = len(p.rows("SELECT id FROM orders WHERE state IN ('live','partial')"))
    ok = bool(refused and under_budget and verdict["verdict"] == "pass" and fail_closed)
    return ("kill switch: refusal in %d ms (budget %d ms), 5 components, fail-closed on a dead read"
            % (max(lat or [0]), lm.KILL_BUDGET_MS), ok,
            "latencies %s ms, codes %s; drill %s (worst %s ms); a sample that claims it refused BEFORE t=0 is "
            "%s; unreadable switch reads engaged=%s; in-flight orders on the book %d (the switch stops NEW "
            "orders only)"
            % (lat, codes, verdict["verdict"], verdict["worst_ms"],
               "%s/%s" % (negative["verdict"], len(negative["negative_latency"])), fail_closed, live))


def c_breaker_and_loss_halt(p: Plane) -> tuple[str, bool, str]:
    """The risk path fails closed and half-opens; the loss halt blocks until a human acknowledges it; rate
    windows expire instead of latching."""
    br, extra = p.ex.breaker, p.ex.extra
    base = p.at
    # the second trip condition: a venue answering 50 % wrong in a window. Alternating results keep `consecutive`
    # below its limit, so a breaker that only counts streaks never opens here — and a slow bleed is the failure
    # mode an operator does not notice until the day is over.
    from polygm_core.risk.limits import RiskBreaker
    br2 = RiskBreaker(limits=extra)
    for i in range(extra.breaker_min_samples):
        br2.record(ok=(i % 2 == 0), now_ms=base + 10_000 + i)
    rate_trips = br2.open_ and "error rate" in br2.last_reason
    for i in range(extra.breaker_consecutive):
        br.record(ok=False, now_ms=base + i)
    allowed, why = br.allow(base + 1)
    # the cooldown runs from the trip (the last failure), not from the first, and cooling is not healing: a
    # breaker that closed on a success recorded while it was still open would let the next 5 failures through
    trip = base + extra.breaker_consecutive - 1
    cooling = br.allow(trip + extra.breaker_cooldown_ms - 1)[0]
    half_open = br.allow(trip + extra.breaker_cooldown_ms + 1)[0]
    br.record(ok=True, now_ms=trip + extra.breaker_cooldown_ms + 1)
    healed = br.allow(trip + extra.breaker_cooldown_ms + 2)[0]
    for _ in range(extra.max_orders_per_minute):
        p.store.bump_counter(key="orders:u-demo", at=base, limit=extra.max_orders_per_minute)
    in_window = p.store.bump_counter(key="orders:u-demo", at=base, limit=extra.max_orders_per_minute)
    next_window = p.store.bump_counter(key="orders:u-demo", at=base + 60_001,
                                      limit=extra.max_orders_per_minute)
    p.store.trip_loss_halt(user_id=p.user, at=base, realized_micro=-2_000_000_000,
                          threshold_micro=-1_000_000_000)
    o = p.run("halted", size=6 * 10**6)
    p.store.acknowledge_loss_halt(user_id=p.user, at=base + 1_000, actor="ops@polygm")
    ack = p.store.loss_halt(p.user) or {}
    try:
        p.store.acknowledge_loss_halt(user_id=p.user, at=base + 1_001, actor="   ")
        anonymous = "accepted"
    except ValueError as e:
        anonymous = "refused: %s" % str(e)[:28]
    after_ack = p.run("after-ack", size=6 * 10**6)
    ok = bool(rate_trips and not allowed and not cooling and half_open and healed and in_window["allowed"] is False
              and in_window["count"] == extra.max_orders_per_minute + 1 and next_window["allowed"] is True and o.get("code") in ("DAILY_LOSS_HALT", "RISK_HALT")
              and ack.get("acknowledged_ms") and anonymous.startswith("refused")
              and after_ack.get("code") == "OK")
    return ("breaker fails closed then half-opens; loss halt blocks until acknowledged; windows expire", ok,
            "breaker open=%s(%s), cooling=%s half-open=%s healed=%s, error-rate trip=%s; %dth order in the "
            "minute allowed=%s, next "
            "allowed=%s; halted order=%s; after ack=%s; an anonymous acknowledgement is %s"
            % (not allowed, str(why)[:22], cooling, half_open, healed, rate_trips,
               extra.max_orders_per_minute + 1,
               in_window["allowed"], next_window["allowed"], o.get("code"), after_ack.get("code"),
               anonymous))


def c_deny_code_table_is_complete(p: Plane) -> tuple[str, bool, str]:
    """Every code the plane can emit has an HTTP status, a retry answer and a severity, or it cannot ship."""
    from polygm_core.risk.limits import DENY_CODES, spec_for
    bad = [c for c in DENY_CODES if not {"http", "retryable", "severity"} <= set(vars(spec_for(c)))]
    try:
        spec_for("NOT_A_REAL_CODE")
        unknown = "returned a default"
    except KeyError:
        unknown = "raised KeyError"
    # Every denial string the plane can emit, harvested from the source rather than from a list somebody has
    # to remember to update: a code invented in a hot path is exactly the code missing from the table.
    emitters = ("packages/polygm_core/risk/gate.py", "packages/polygm_core/risk/limits.py",
                "packages/polygm_core/venue/clob_v2.py", "packages/polygm_core/copy/engine.py",
                "packages/polygm_core/automation/engine.py", "packages/polygm_core/reconcile/reconciler.py",
                "packages/polygm_core/revenue/attribution.py", "services/executor/main.py",
                "services/executor/store.py")
    # three shapes the plane actually uses to refuse, harvested from the source so a new refusal in a hot
    # path is caught by this check rather than by a user seeing `{"code": "?"}`. The table itself is built by
    # `_d("CODE", ...)`, which none of these match, so the check cannot satisfy itself.
    call_re = re.compile(r"""(?:deny|fail)\(['"]([A-Z][A-Z0-9_]{3,})['"]""")
    kw_re = re.compile(r"""code=['"]([A-Z][A-Z0-9_]{3,})['"]""")
    dec_re = re.compile(r"""Decision\(\s*False\s*,\s*['"]([A-Z][A-Z0-9_]{3,})['"]""")
    emitted: set[str] = set()
    for rel in emitters:
        text = (ROOT / rel).read_text()
        emitted |= ({m.group(1) for m in call_re.finditer(text)} | {m.group(1) for m in kw_re.finditer(text)}
                    | {m.group(1) for m in dec_re.finditer(text)})
    benign = {"OK", "QUEUED", "READY", "NONE", "UNCERTAIN_INTENT", "NOT_FOUND", "IDEM_IN_PROGRESS", "NOT_CLAIMED"}
    missing = sorted(c for c in emitted if c not in DENY_CODES and c not in benign)
    gate_codes = {"BAD_AMOUNT", "BAD_MARKET_META", "BAD_SIDE", "BELOW_MIN_SIZE", "DAILY_CAP",
                  "MARKET_NOT_ACCEPTING", "NO_ORDER_BOOK", "OFF_TICK", "OVER_ORDER_CAP", "PRICE_FAR_FROM_MID",
                  "RISK_HALT", "STALE_QUOTE", "TOO_MANY_OPEN", "UNKNOWN_TICK", "ZERO_SIZE"}
    ok = (not bad and unknown == "raised KeyError" and not missing and len(emitted) >= 15
          and gate_codes <= emitted)
    return ("deny-code table covers every code the plane emits", ok,
            "%d codes in the table, %d missing http/retry/severity; unknown code: %s; %d codes harvested from "
            "the plane (%d of the 15 gate refusals found), emitted-but-unlisted: %s; incomplete rows: %s"
            % (len(DENY_CODES), len(bad), unknown, len(emitted), len(gate_codes & emitted), missing or "none",
               bad or "none"))


# ==================================================================== copy (D5) and automation (D6) ====
def c_copy_refuses_rather_than_chases(p: Plane) -> tuple[str, bool, str]:
    """D5's contract: skip over chase, no quote is no copy, never into a resolving market, no chains, and the
    record a copier reads includes the losses."""
    from polygm_core.copy import engine as cp
    cfg = cp.CopyConfig(id="gate", user_id=p.user, source_user="u-src", mode="mirror",
                       max_order_micro=25_000_000)
    f = cp.SourceFill(wallet="u-src", intent_id="i-src", token_id=p.token, market_id=p.market, side="BUY",
                     price_micro=480_000, size_shares_micro=200 * 10**6, at_ms=p.at)
    q = cp.Quote(best_bid_micro=480_000, best_ask_micro=520_000, age_ms=10)
    clock = cp.MarketClock(accepting_orders=True, seconds_to_resolution=3_600, tick_micro=10_000)

    def d(**kw):
        a = kw.pop("cfg", cfg)
        return cp.decide(a, kw.pop("fill", f), quote=kw.pop("quote", q), market=kw.pop("market", clock),
                         day_spent_micro=kw.pop("spent", 0), day_copies=kw.pop("copies", 0),
                         last_copy_ms=kw.pop("last", 0), now_ms=p.at)
    cases = {
        # the source paid 480000; our best ask is 8% above that, so copying means chasing
        "deviation": d(fill=cp.SourceFill("u-src", "i-d", p.token, p.market, "BUY", 300_000,
                                         200 * 10**6, p.at), quote=cp.Quote(480_000, 520_000, 10)),
        "stale": d(quote=cp.Quote(480_000, 520_000, 9_000)),
        "resolving": d(market=cp.MarketClock(True, 30, 10_000)),
        "unknown_clock": d(market=cp.MarketClock(True, None, 10_000)),
        "blocked": d(cfg=cp.CopyConfig(id="g2", user_id=p.user, source_user="u-src",
                                      blocked_markets=(p.market,))),
        "daily_cap": d(copies=cfg.max_copies_per_day),
        "min_interval": d(last=p.at - 1_000, cfg=cp.CopyConfig(id="g4", user_id=p.user, source_user="u-src",
                                                              min_interval_ms=60_000)),
        "sub_share": d(cfg=cp.CopyConfig(id="g3", user_id=p.user, source_user="u-src", mode="ratio",
                                        ratio_bps=1)),
    }
    bad = {k: (v.action, v.reason) for k, v in cases.items() if v.action != "skipped" or not v.reason}
    chase = cp.decide(cfg, cp.SourceFill("u-src", "i2", p.token, p.market, "BUY", 300_000, 100 * 10**6, p.at),
                     quote=cp.Quote(480_000, 520_000, 10), market=clock, day_spent_micro=0, day_copies=0,
                     last_copy_ms=0, now_ms=p.at)
    no_chase = chase.action == "skipped" and "deviat" in chase.reason.lower()
    chain = cp.walk_chain([{"user_id": "b", "source_user": "c", "enabled": 1}], "a", "b")
    cycle = cp.walk_chain([{"user_id": "a", "source_user": "b", "enabled": 1},
                          {"user_id": "b", "source_user": "a", "enabled": 1}], "a", "b")
    rec = cp.track_record([cp.ClosedTrade(realized_micro=10_000_000),
                          cp.ClosedTrade(realized_micro=-9_000_000, fee_micro=1_000_000),
                          cp.ClosedTrade(realized_micro=-2_000_000)])
    disclosed = all(k in rec for k in cp.REQUIRED_DISCLOSURE)
    ok = bool(not bad and no_chase and not chain["allowed"] and cycle["cycle"] and disclosed
              and rec["net_after_fees_micro"] < 0 and rec["longest_losing_streak"] == 2)
    return ("copy skips instead of chasing, refuses unknown clocks and chains, and reports the losses", ok,
            "refused as designed: %s; price-away -> %s (%s); depth allowed=%s; cycle=%s; record net %d, streak "
            "%d, win %s bp, disclosure %s"
            % ("all 8" if not bad else bad, chase.action, chase.reason[:34], chain["allowed"], cycle["cycle"],
               rec["net_after_fees_micro"], rec["longest_losing_streak"], rec["win_rate_bp"], disclosed))


def c_copy_through_the_queue_only(p: Plane) -> tuple[str, bool, str]:
    """A copied order is an ordinary intent: same queue, audience stamped by the queue, one event per source
    fill, and a replay that creates nothing."""
    from polygm_core.copy import engine as cp
    p.tight_book()
    p.store.conn.execute("INSERT OR REPLACE INTO copy_configs (id,user_id,source_user,mode,ratio_bps,"
                         "max_order_micro,max_daily_micro,blocked_markets,enabled,created_ms) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?)",
                         ("cc-1", p.user, "u-src", "mirror", 10_000, 25_000_000, 250_000_000, "[]", 1, p.at))
    p.store.conn.commit()
    p.store.save_copy_config_policy(config_id="cc-1", at=p.at, max_entry_deviation_bps=150,
                                   take_profit_bp=None, stop_loss_bp=None, min_seconds_to_resolution=900)
    eng = cp.CopyEngine(p.store, builder_bps=100, fee_rate_bps=0)
    # the source filled at our ask: zero deviation, on the tick grid, so the rule under test is provenance and
    # idempotency rather than a fixture accident
    fill = cp.SourceFill(wallet="u-src", intent_id="i-src-1", token_id=p.token, market_id=p.market,
                        side="BUY", price_micro=500_000, size_shares_micro=10 * 10**6, at_ms=p.at + 1)
    first = eng.on_source_fill(fill, at=p.at + 1)[0]
    again = eng.on_source_fill(fill, at=p.at + 1)[0]
    # `audience` lives on the directive, not the intent: provenance is written by the queue and read back
    # from the row the queue wrote, not from a column someone might add to the wrong table.
    queued = p.rows("SELECT i.id AS id, d.audience AS audience FROM order_intents i JOIN order_directives d"
                    " ON d.intent_id = i.id WHERE d.audience='copy'")
    events = p.rows("SELECT action,reason FROM copy_events")
    copied = [e for e in events if e["action"] == "copied"]
    dupes = [e for e in events if e["action"] == "skipped"]
    executed = p.ex.tick(at=p.at + 2, reconcile=False)
    handled = [(h.get("state"), h.get("code")) for h in executed["handled"]]
    orders = p.rows("SELECT o.id FROM orders o JOIN order_directives d ON d.intent_id = o.intent_id "
                    "WHERE d.audience='copy'")
    # the replay writes NO intent but DOES write an event: the log records every decision the copier took,
    # including the one where it declined to act, so "why is my copy missing" has an answer in the database.
    ok = bool(first.get("action") == "copied" and str(again.get("reason", "")).startswith("duplicate")
              and len(queued) == 1 and len(copied) == 1 and len(dupes) == 1
              and "duplicate" in dupes[0]["reason"] and len(orders) == 1
              and first.get("intent_id") in [h.get("intent_id") for h in executed["handled"]])
    return ("a copied order is one queued intent with audience='copy', idempotent on the source fill", ok,
            "first copy %s; replay %s; events %s; copy-audience intents %d; venue orders %d; executor "
            "handled %s"
            % (first.get("action"), again.get("reason"), [(e["action"], e["reason"][:20]) for e in events],
               len(queued), len(orders), handled))


def c_automation_refuses_at_save_and_logs_every_pass(p: Plane) -> tuple[str, bool, str]:
    """Rules fail at save, never at fire; every evaluation is a row; a dry run gates the first order; a human's
    recent order outranks the rule; and no rule may cancel a user's whole book."""
    from polygm_core.automation import engine as au
    errs = au.validate_rule({"trigger": {"kind": "all"}, "actions": [{"kind": "cancel_open", "scope": "all"}]})
    deep = au.validate_rule({"trigger": {"all": [{"all": [{"all": [{"kind": "time", "at_ms": 1}]}]}]},
                            "actions": [{"kind": "set_alert", "message": "x"}]})
    wide = au.validate_rule({"trigger": {"kind": "price_cross", "op": ">", "price_micro": 1},
                            "actions": [{"kind": "limit", "side": "BUY", "size_shares_micro": 10**6}] * 9})
    eng = au.AutomationEngine(p.store, canceller=lambda **kw: {"queued": 1, "ok": True})
    eng.save_rule(rule_id="ar-1", user_id=p.user, kind="entry", at=p.at, enabled=True,
                 rule={"trigger": {"kind": "price_cross", "op": ">=", "price_micro": p.mid() - 10_000,
                                  "uses": "ask"},
                       "actions": [{"kind": "market", "side": "BUY", "size_shares_micro": 10 * 10**6,
                                   "max_slippage_bps": 100}],
                       "max_loss_micro": 1_000_000},
                 market_ids=[(p.market, p.token)])
    saved = p.store.automation_rule("ar-1")
    armed_on_save = bool(saved["enabled"]) if saved else True
    dry_all = eng.tick(at=p.at + 1, mode="dry_run")
    dry = (dry_all["outcomes"] or [{"outcome": "no_outcome", "reason": "the rule was never evaluated"}])[0]
    # the only legal path to live: a dry run happened, the engine observed it, and only then can it be armed
    marked = eng.mark_dry_run_done("ar-1", at=p.at + 2)["marked"]
    no_dry_run_yet = eng.tick(at=p.at + 1, mode="dry_run")
    enabled = eng.enable("ar-1", at=p.at + 3)["enabled"]
    def fresh(at: int) -> int:
        """Move the quote clock with the tick clock. Without this the engine refuses every live tick as
        stale -- correct, but it would be the seed's fault, not the rule's."""
        p.store.conn.execute("UPDATE book_levels SET updated_ms=? WHERE market_id=?", (at, p.market))
        p.store.conn.commit()
        return at

    # a live pass on a quote a minute old must NOT place: an automation that fires on old prices is a bug mill
    stale_all = eng.tick(at=fresh(p.at) + 60_000, mode="live")
    stale = (stale_all["outcomes"] or [{"outcome": "not_evaluated", "reason": ""}])[0]
    p.queue(key="human-1", size=6 * 10**6)
    p.ex.tick(at=p.at + 3, reconcile=False)
    parked_all = eng.tick(at=fresh(p.at + 4), mode="live")
    parked = (parked_all["outcomes"] or [{"outcome": "not_evaluated", "reason": ""}])[0]
    # the stand-down is not a timeout: the rule stays paused until a human re-arms it, because the whole point
    # of the pause is that somebody started trading by hand
    held_all = eng.tick(at=fresh(p.at + 400_000), mode="live")
    held = (held_all["outcomes"] or [{"outcome": "not_evaluated", "reason": ""}])[0]
    rearmed = eng.enable("ar-1", at=p.at + 400_001, actor="u-demo")["enabled"]
    later_all = eng.tick(at=fresh(p.at + 400_002), mode="live")
    later = (later_all["outcomes"] or [{"outcome": "not_evaluated", "reason": ""}])[0]
    # the rule only ever enqueues; the executor is still the single door to the venue, so the order appears
    # after the next executor pass and not before
    p.ex.tick(at=p.at + 400_003, reconcile=False)
    runs = p.rows("SELECT mode,outcome,deny_code FROM automation_runs WHERE rule_id='ar-1' ORDER BY id")
    orders_for_rule = len(p.rows("SELECT o.id FROM orders o JOIN order_directives d ON d.intent_id="
                                "o.intent_id WHERE d.audience='automation'"))
    ok = bool(errs and deep and wide and not armed_on_save and marked and no_dry_run_yet["outcomes"]
              and dry["outcome"] == "would_place" and enabled
              and stale["outcome"] == "skipped" and "stale" in str(stale.get("reason")).lower()
              and parked["outcome"] == "skipped" and "human" in str(parked.get("reason")).lower()
              and held["outcome"] == "skipped" and "paused" in str(held.get("reason")).lower()
              and "human_active" in str(held.get("reason"))
              and rearmed and later["outcome"] == "placed" and len(runs) >= 6 and orders_for_rule == 1)
    return ("rules validated at save, gated by a dry run, parked by a human and by a stale quote; every pass "
            "is a row", ok,
            "unknown kind %d error(s), nesting %d, breadth %d; enabled-on-save %s; dry run observed %s; dry "
            "said %s without placing; live on a minute-old quote: %s (%s); after a human order: %s (%s); and "
            "on a fresh quote with no re-arm: still %s (%s); re-armed by the user=%s then %s; run rows %s; "
            "automation orders on the book %d"
            % (len(errs), len(deep), len(wide), armed_on_save, marked, dry["outcome"], stale["outcome"],
               str(stale.get("reason"))[:22], parked["outcome"], str(parked.get("reason"))[:22],
               held["outcome"], str(held.get("reason"))[:26], rearmed, later["outcome"],
               [(r["mode"], r["outcome"]) for r in runs], orders_for_rule))


def c_template_ships_only_on_positive_edge(p: Plane) -> tuple[str, bool, str]:
    """The brief's condition, evaluated rather than asserted: the entry rule exists iff the fee maths proves an
    edge, and the protective rules ship whatever the answer is."""
    from polygm_core.automation import engine as au
    unknown = au.crypto_5m_template(fee_type="None", fee_rate_bps=None)
    unmeasured = au.crypto_5m_template(fee_type="taker", fee_rate_bps=70)
    small = au.crypto_5m_template(fee_type="taker", fee_rate_bps=70, edge_available_bp=50, latency_ms=1_000)
    big = au.crypto_5m_template(fee_type="taker", fee_rate_bps=70, edge_available_bp=1_000, latency_ms=1_000)
    needed = small["numbers"]["edge_needed_bp"]
    at_hurdle = au.crypto_5m_template(fee_type="taker", fee_rate_bps=70, edge_available_bp=needed,
                                     latency_ms=1_000)
    over_hurdle = au.crypto_5m_template(fee_type="taker", fee_rate_bps=70, edge_available_bp=needed + 1,
                                       latency_ms=1_000)
    ship_flip = (not at_hurdle["ship_entry_rule"]) and over_hurdle["ship_entry_rule"]
    valid = not any(au.validate_rule({"trigger": x["trigger"], "actions": x["actions"],
                                     "max_loss_micro": x.get("max_loss_micro", 1_000_000)})
                    for x in small["shipped_rules"])
    ok = bool(not unknown["ship_entry_rule"] and not unmeasured["ship_entry_rule"]
              and not small["ship_entry_rule"] and big["ship_entry_rule"] and ship_flip and valid
              and len(small["shipped_rules"]) >= 3
              and {"taker_break_even_win_rate_bp", "edge_needed_bp"} <= set(small["numbers"]))
    return ("crypto-5m entry template ships iff measured edge > fees+spread+latency; protective rules always",
            ok, "fee_type 'None' %s; edge unmeasured %s; 50bp %s; 1000bp %s; hurdle %d, at-hurdle %s, +1 %s; "
            "%d shipped rules all valid: %s"
            % (str(unknown["blocking_reason"])[:18], str(unmeasured["blocking_reason"])[:18],
               not small["ship_entry_rule"], big["ship_entry_rule"], needed, not at_hurdle["ship_entry_rule"],
               over_hurdle["ship_entry_rule"], len(small["shipped_rules"]), valid))


# ============================================================================== revenue (D8) + schema ==
def c_revenue_cannot_pay_itself(p: Plane) -> tuple[str, bool, str]:
    """Payouts come from money the venue actually paid us; a code we cannot vouch for earns nothing; and a day
    nobody measured is not a day that reconciled."""
    import inspect
    from polygm_core.revenue import attribution as ra
    reg = ra.CodeRegistry([ra.BuilderCode(code="0xGATE", label="gate", kind="copy_source", fee_bps=100,
                                         source_user="u-src", source_share_bps=10_000, issued_ms=1)])
    rows = [{"id": 1, "builder_code": "0xGATE", "fee_micro_expected": 1_000_000,
            "notional_micro": 100_000_000, "excluded_reason": ""}]
    split = ra.payout_split(rows, {"0xGATE": {"micro": 250_000}}, registry=reg)
    capped = ra.payout_capped(ra.payout_split(rows, {"0xGATE": {"micro": 1_000_000}}, registry=reg),
                             observed_total_micro=250_000)
    unissued = ra.payout_split([{"id": 2, "builder_code": "0xUNKNOWN", "fee_micro_expected": 1_000_000,
                                "notional_micro": 100_000_000, "excluded_reason": ""}],
                              {"0xUNKNOWN": {"micro": 1_000_000}}, registry=reg)
    excl = [ra.exclusion(ra.OrderFacts(intent_id="i", order_id="o", user_id="u", market_id="m", token_id="t",
                                      side="BUY", builder_code="0xGATE", notional_micro=10**6,
                                      filled_shares_micro=s, placed_ms=1, filled_ms=2, maker_address=a,
                                      taker_address=b, source_user=su))
            for s, a, b, su in ((0, "", "", ""), (10**6, "0xA", "0xa", ""), (10**6, "", "", "u"))]
    sig = list(inspect.signature(ra.measure_from_chain).parameters)
    day = ra.rollup(rows, [], day="gate", registry=reg)
    disputed = ra.rollup(rows, [{"attribution_id": 1, "chain_measured_micro": 0}], day="gate2", registry=reg)
    matched = ra.rollup(rows, [{"attribution_id": 1, "chain_measured_micro": 1_000_000}], day="gate3",
                        registry=reg)
    statuses = tuple(x.to_daily_row()["status"] for x in (day, disputed, matched))
    engine = tuple(x.engine_status for x in (day, disputed, matched))
    ok = bool(split["payout_micro"] <= 250_000 and capped["payout_micro"] == 250_000 and capped["capped"]
              and unissued["payout_micro"] == 0 and excl == ["unfilled", "self_cross", "self_copy"]
              and sig == ["events", "code"] and statuses == ("unreconciled", "investigating", "matched")
              and engine == ("unmeasured", "disputed", "reconciled"))
    return ("revenue: payouts bounded by observed fees, unknown codes earn nothing, unmeasured != reconciled",
            ok, "a 100%% share pays %d of 250000 observed; capped to collected %d; an unissued code earns %d; "
            "exclusions %s; the measurer can read %s; schema status %s; engine status %s"
            % (split["payout_micro"], capped["payout_micro"], unissued["payout_micro"], excl, sig, statuses,
               engine))


def c_attribution_lands_on_the_order_and_the_day(p: Plane) -> tuple[str, bool, str]:
    """A builder code on a real order: issued terms are stamped on the row, the second measurement is a
    signature the venue cannot influence, and the night's rollup persists in the shape the schema allows."""
    from polygm_core.revenue import attribution as ra
    reg = ra.CodeRegistry([ra.BuilderCode(code="0xGATE", label="gate", kind="copy_source", fee_bps=100,
                                         source_user="u-src", source_share_bps=10_000, issued_ms=1)])
    o = p.run("attr-1", price=500_000, size=100 * 10**6, builder_bps=100)
    rows = p.store.attribution_rows(at=p.at, window_ms=120_000)
    if not rows:
        return ("attribution: terms stamped on the order, rollup persisted", False,
                "no builder_attribution row for a real order (order outcome %s)" % o.get("code"))
    r = rows[0]
    aid = int(r["attribution_id"])
    expected = int(r["expected_micro"])
    code = ra.BuilderCode(code="0xGATE", label="gate", kind="copy_source", fee_bps=100, source_user="u-src",
                         source_share_bps=10_000, issued_ms=1)
    p.store.save_terms(attribution_id=aid, builder_code="0xGATE", fee_bps=100,
                      notional_micro=int(r["notional_micro"] or 0), excluded_reason="",
                      code_fingerprint=code.fingerprint, at=p.at)
    measured = expected - 10_000
    p.store.save_measure(attribution_id=aid, order_id=o["order_id"], chain_micro=measured,
                        notional_micro=50_000_000_000, expected_micro=expected, source="chain_log", at=p.at)
    fresh = p.store.attribution_rows(at=p.at, window_ms=120_000)[0]
    # fed straight from the store, in the store's own vocabulary: a rollup that only reads a test fixture's
    # key names is a nightly job that fails on its first row.
    day = ra.rollup([fresh], [{"attribution_id": aid, "chain_measured_micro": measured}], day="p06-gate",
                    registry=reg)
    p.store.save_daily(**day.to_daily_row(), at=p.at)
    stored = next(d for d in p.store.daily_rows(limit=5) if d["day"] == "p06-gate")
    wrong = p.store.enqueue_intent(user_id=p.user, market_id=p.market, token_id=p.token, side="BUY",
                                  price_micro=500_000, size_micro=6 * 10**6, idempotency_key="attr-bad",
                                  order_type="GTC", audience="user", builder_bps=250, fee_rate_bps=0, at=p.at)
    ok = bool(int(fresh["chain_measured_micro"]) == measured and int(fresh["delta_micro"]) == 10_000
              and fresh["code_fingerprint"] and stored["status"] == "investigating"
              and int(stored["delta_micro"]) == 10_000 and int(day.payout_micro) <= measured
              and wrong.get("state") in ("queued", "pending"))
    return ("attribution: terms stamped on the order, rollup persisted, a bad claim does not block the order",
            ok, "expected %s, chain %s, delta %s, fingerprint %s; daily row status %s; an order claiming 250bp "
            "on a 100bp code queued as %s (the registry wins, the order runs unattributed)"
            % (expected, measured, fresh["delta_micro"], str(fresh["code_fingerprint"])[:12],
               stored["status"], wrong.get("state")))


def c_schema_parity_and_append_only(p: Plane) -> tuple[str, bool, str]:
    """Every CHECK the Postgres source has survives into the dev twin, and the money tables cannot be edited."""
    rc, out = sh([PY, str(ROOT / "tools" / "build-sqlite-migrations.py"), "--check"])
    # A trigger is per-row, so tamper with rows that are really there, each produced by the normal path.
    # An "append-only" claim tested against empty tables is a claim about nothing.
    o = p.run("tamper-order", price=500_000, size=100 * 10**6)
    p.store.book_fill(order_id=o["order_id"], intent_id=o["intent_id"], user_id=p.user, token_id=p.token,
                     market_id=p.market, side="BUY", price_micro=500_000, size_micro=40 * 10**6, fee_micro=10,
                     trade_id="0xT-tamper", exchange_ts=p.at, maker=True, source="reconcile", at=p.at)
    p.store.record_run(rule_id="tamper-rule", user_id=p.user, mode="live", outcome="placed",
                      reason="gate", deny_code="", intent_id=o["intent_id"], at=p.at)
    # `save_measure` writes the venue's own answer, so its `source` is one of the three the schema allows
    p.kill(on=True)
    p.kill(on=False)
    con = sqlite3.connect(p.db)
    con.execute("PRAGMA foreign_keys=ON")
    tamper, protected = [], 0
    for table, sql in (("cash_ledger", "UPDATE cash_ledger SET amount_micro=0 WHERE 1=1"),
                      ("fills", "DELETE FROM fills WHERE 1=1"),
                      ("order_lifecycle", "UPDATE order_lifecycle SET state='filled' WHERE 1=1"),
                      ("builder_attribution", "DELETE FROM builder_attribution WHERE 1=1"),
                      ("automation_runs", "DELETE FROM automation_runs WHERE 1=1"),
                      ("kill_switch_state", "DELETE FROM kill_switch_state WHERE 1=1")):
        try:
            con.execute(sql)
            con.commit()
            tamper.append("%s was editable" % table)
        except sqlite3.IntegrityError as e:
            protected += 1
            if "append-only" not in str(e).lower() and "no updates" not in str(e).lower():
                tamper.append("%s refused for the wrong reason: %s" % (table, str(e)[:32]))
        except sqlite3.Error as e:
            tamper.append("%s: %s" % (table, str(e)[:32]))
    for table in ("cash_ledger", "fills", "order_lifecycle", "builder_attribution", "automation_runs",
                  "kill_switch_state"):
        if not con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]:
            tamper.append("%s has no rows, so its guard was never exercised" % table)
    con.close()
    ok = rc == 0 and not tamper and protected == 6
    return ("dev twin carries every source CHECK, and 6 money tables are append-only", ok,
            "transpiler --check rc=%d (%s); %s" % (rc, (out.strip().splitlines() or ["clean"])[-1][:48],
                                                  "; ".join(tamper) or "all 6 protected"))


def c_mid_flight_recovery_is_proved(p: Plane) -> tuple[str, bool, str]:
    """The headline claim is only as good as the recorded run: a real process, really killed, and its output."""
    if not ARTIFACT.exists():
        return ("mid-flight recovery demonstrated against a real SIGKILL (recorded artifact)", False,
                "%s is missing - run `make gate-p06` or `tools/p06-chaos-test.py --record %s`"
                % (ARTIFACT.relative_to(ROOT), ARTIFACT.relative_to(ROOT)))
    text = ARTIFACT.read_text()
    need = ("signed", "reconciled", "exactly one", "venue order")
    missing = [n for n in need if n.lower() not in text.lower()]
    counts = [int(m) for m in re.findall(r"orders? created\D{0,12}(\d+)", text)]
    ok = not missing and bool(counts) and all(c == 1 for c in counts)
    return ("mid-flight recovery demonstrated against a real SIGKILL (recorded artifact)", ok,
            "%d lines; markers %s; per-scenario order counts %s" % (len(text.splitlines()),
                                                                    "all four present" if not missing
                                                                    else "MISSING " + ",".join(missing),
                                                                    counts or "none parsed"))


# ==================================================================================== honesty checks ==
def c_docs_carry_the_unverified_and_the_numbers(p: Plane) -> tuple[str, bool, str]:
    """Provider pricing we could not confirm is marked in the doc, and the doc states the numbers the gate
    measures rather than a friendlier pair invented in prose."""
    if not DOC.exists():
        return ("P06 doc marks every unverified number and states the measured ones", False,
                "docs/P06-trading-plane.md does not exist")
    text = DOC.read_text()
    unverified = [pr.name for pr in p.wl.PROVIDERS if not pr.verified]
    marked = all(n in text and ("UNVERIFIED" in text or "not verified" in text.lower())
                 for n in unverified) if unverified else True
    sections = [s for s in ("wallet", "venue", "reconcil", "kill", "copy", "automation", "builder")
                 if s not in text.lower()]
    ok = bool(DOC.stat().st_size > 4_000 and marked and not sections
              and ("1000" in text or "under a second" in text.lower()) and "tick" in text.lower())
    return ("P06 doc marks every unverified number and states the measured ones", ok,
            "%d bytes; providers needing a marker: %s (%s); missing sections %s; kill budget and tick stated: "
            "%s" % (DOC.stat().st_size, unverified or "none", "all marked" if marked else "MARKER MISSING",
                    sections or "none", ok))


def c_make_is_honest(p: Plane) -> tuple[str, bool, str]:
    """Every target this phase advertises exists and runs the tool it names, and `make check` cannot skip a phase."""
    mk = (ROOT / "Makefile").read_text()
    want = ["p06:", "gate-p06:", "gate-p06-offline:", "gate-p06-mutate:", "chaos-p06:", "drill-p06:"]
    missing = [t for t in want if not re.search(r"^%s\s*(##.*)?$" % re.escape(t), mk, re.M)]
    tools_missing = [t for t in ("tools/p06-gate-check.py", "tools/p06-chaos-test.py",
                                 "tools/p06-mutation-test.py") if not (ROOT / t).exists()]
    check_line = next((ln for ln in mk.splitlines() if ln.startswith("check:")), "")
    phases = [ph for ph in ("p01", "p02", "p03", "p04", "p05", "p06") if ph not in check_line]
    wired = all(n in mk for n in ("p06-gate-check.py", "p06-chaos-test.py", "p06-mutation-test.py"))
    ok = not missing and not tools_missing and not phases and wired
    return ("Makefile wires this phase's gates and `make check` runs every phase", ok,
            "targets %s; phase tools %s; `check` mentions %s; targets name the tools: %s"
            % ("all 6 present" if not missing else missing, "all present" if not tools_missing else tools_missing,
               "p01-p06" if not phases else "MISSING " + ",".join(phases), wired))


def c_mutation_run_proves_the_gate_can_fail(p: Plane) -> tuple[str, bool, str]:
    """A gate that cannot fail is decoration: run the mutants and require every one to break a named check."""
    tool = ROOT / "tools" / "p06-mutation-test.py"
    if not tool.exists():
        return ("mutation run proves the gate can fail", False, "tools/p06-mutation-test.py does not exist")
    rc, out = sh([PY, str(tool)], timeout=1800)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    survivors = [ln for ln in lines if "SURVIVED" in ln]
    tally = next((ln for ln in lines if re.search(r"\d+/\d+ mutants", ln)), "")
    ok = rc == 0 and not survivors and bool(tally)
    return ("mutation run proves the gate can fail", ok,
            "rc=%d; %s%s" % (rc, tally or (lines[-1][:90] if lines else "no output"),
                            "" if not survivors else " ; SURVIVORS: " + "; ".join(survivors[:3])))


def c_test_suite_passes(p: Plane) -> tuple[str, bool, str]:
    """The whole suite, in this tree, now."""
    rc, out = sh([PY, "-W", "ignore::ResourceWarning", "-m", "unittest", "discover", "-s", "tests"],
                 timeout=1800)
    tally = [ln for ln in out.splitlines() if ln.startswith(("Ran ", "OK", "FAILED"))]
    n = next((int(m.group(1)) for ln in tally for m in [re.match(r"Ran (\d+) tests", ln)] if m), 0)
    ok = rc == 0 and any(ln.strip() == "OK" for ln in tally)
    return ("full unittest suite (%d tests)" % n, ok, " | ".join(tally[-3:]) or out.strip()[-200:])


def c_lint_and_openapi(p: Plane) -> tuple[str, bool, str]:
    """The repo's own linter, and the OpenAPI contract that documents what this phase adds."""
    rc1, o1 = sh([PY, str(ROOT / "tools" / "lint-rules.py")])
    rc2, o2 = sh([PY, str(ROOT / "tools" / "check-openapi.py")])
    spec = (ROOT / "contracts" / "openapi.yaml").read_text()
    api = (ROOT / "services" / "api" / "app.py").read_text()
    declared = set(re.findall(r"@app\.(?:get|post|put|delete)\(\"([^\"]+)\"", api))
    documented = set(re.findall(r"^  (/[^:]*):", spec, re.M))
    routes = sorted(declared - documented)
    ok = rc1 == 0 and rc2 == 0 and not routes
    return ("lint-rules + check-openapi + every API route is in the contract", ok,
            "lint rc=%d (%s); openapi rc=%d (%s); %d routes in app.py, missing from the contract: %s"
            % (rc1, (o1.strip().splitlines() or ["clean"])[-1][:40], rc2,
               (o2.strip().splitlines() or ["clean"])[-1][:40], len(declared), routes or "none"))


CHECKS = [
    c_p04_gate_order_through_the_executor, c_preflight_sequence_is_complete, c_single_door,
    c_batch_cap_and_partial_failure, c_cancel_budget_and_phrase, c_fee_maths_is_the_venues,
    c_allowance_and_all_in_number, c_wallet_state_machine, c_policy_drift_suspends_and_export_is_gated,
    c_deposit_lifecycle, c_withdrawal_path_is_ordered, c_all_eight_cases_are_executed,
    c_recovery_never_duplicates_a_post, c_lookup_failure_is_not_absence, c_fill_is_idempotent,
    c_kill_switch_latency, c_breaker_and_loss_halt, c_deny_code_table_is_complete,
    c_copy_refuses_rather_than_chases, c_copy_through_the_queue_only,
    c_automation_refuses_at_save_and_logs_every_pass, c_template_ships_only_on_positive_edge,
    c_revenue_cannot_pay_itself, c_attribution_lands_on_the_order_and_the_day,
    c_schema_parity_and_append_only, c_mid_flight_recovery_is_proved,
    c_docs_carry_the_unverified_and_the_numbers, c_make_is_honest,
    c_mutation_run_proves_the_gate_can_fail, c_test_suite_passes, c_lint_and_openapi,
]


def main() -> int:
    ap = argparse.ArgumentParser(description="P06 trading-plane quality gate")
    ap.add_argument("--list", action="store_true", help="print the checks and what each one runs")
    ap.add_argument("--fast", action="store_true", help="accepted for symmetry with P05: these are all offline")
    ap.add_argument("--live", action="store_true",
                    help="run the real SIGKILL chaos first, then read the artifact it records")
    ap.add_argument("--only", default="", help="run only the checks whose name contains this")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.list:
        for fn in CHECKS:
            print("  %-44s %s" % (fn.__name__, (fn.__doc__ or "").strip().split("\n")[0][:78]))
        print("%d checks; every one executes against a live SQLite plane" % len(CHECKS))
        return 0
    if a.live:
        print("  running tools/p06-chaos-test.py (real SIGKILLs; minutes)...")
        rc, _out = sh([PY, str(ROOT / "tools" / "p06-chaos-test.py"), "--record", str(ARTIFACT)], timeout=1800)
        print("  chaos rc=%d; artifact %s" % (rc, "written" if ARTIFACT.exists() else "MISSING"))
    checks = [c for c in CHECKS if not a.only or a.only in c.__name__]
    results = []
    for fn in checks:
        plane: Plane | None = None
        t0 = time.perf_counter()
        try:
            plane = Plane()
            label, ok, detail = fn(plane)
        except Exception as e:                                # a crashing check is a FAIL, never a skip
            import traceback
            label, ok, detail = fn.__name__, False, "raised %s: %s | %s" % (
                type(e).__name__, str(e)[:170],
                (traceback.format_exc(limit=3).strip().splitlines() or [""])[-1][:110])
        finally:
            if plane is not None:
                plane.close()
        ms = int((time.perf_counter() - t0) * 1000)
        results.append((label, ok, detail, ms))
        print("  %-4s %5d ms  %s\n        %s" % ("PASS" if ok else "FAIL", ms, label, detail))
    passed = sum(1 for _l, ok, _d, _m in results if ok)
    if a.json:
        print(json.dumps({"phase": "P06", "passed": passed, "total": len(results), "live": a.live,
                          "checks": [{"label": l, "ok": ok, "detail": d, "ms": m}
                                     for l, ok, d, m in results]}, indent=2))
    print("\nP06 gate: %d/%d checks passed in %d ms" % (passed, len(results), sum(m for *_x, m in results)))
    if passed != len(results):
        print("gate is a floor: any FAIL here means the phase is not done, whatever the prose elsewhere says")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
