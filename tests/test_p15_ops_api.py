"""P15 D3 — the two operational surfaces a deploy calls, pinned as contract.

`deploy/deploy.sh` will not replace the executor until `GET /v1/admin/drain-status` says it is safe, and the
instant-off path is `POST /v1/admin/flags`. Both are scripts talking to an API at 2am, so the interesting
assertions are the *refusals* and the exact boundary between "durable queue" and "in flight with the venue":

* `pending`/`queued` do **not** block a replace — the queue lives in Postgres and the next executor drains it;
* `submitting`/`uncertain` **do** — that is the ambiguous-order hazard P6 D3 exists for;
* the draining flag blocks regardless of counts, because it is the operator saying "not now";
* a flag write needs a reason, names a real flag, and lands in `flag_audit`.
"""
from __future__ import annotations

import os
import unittest
import uuid

from conftest import import_app  # noqa: F401

ADMIN = "adm_" + "k" * 44
H = {"X-Admin-Token": ADMIN}


class OpsBase(unittest.TestCase):
    app_name = "p15-ops"
    app = None
    client = None

    @classmethod
    def setUpClass(cls):
        os.environ["PGM_ADMIN_TOKEN"] = ADMIN
        cls.app = import_app(cls.app_name)
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.app.app, raise_server_exceptions=False)

    def setUp(self):
        self.con = self.app._db
        # The module-level app is imported once for the class, so state from one test is the next test's
        # fixture unless it is cleared here. `flag_audit` is append-only by schema and is deliberately NOT
        # cleared: the audit assertion below finds its own row by reason string instead.
        # Intents can only be removed while nothing references them: `orders` and the append-only
        # `order_lifecycle` do, and neither may be deleted (the schema refuses, correctly). So the delete is
        # guarded rather than blanket — the alternative is a test file that goes red the moment another test
        # records an order.
        self.con.execute("DELETE FROM order_directives WHERE intent_id IN (SELECT id FROM order_intents)")
        self.con.execute("DELETE FROM order_intents WHERE NOT EXISTS"
                         " (SELECT 1 FROM orders o WHERE o.intent_id = order_intents.id)"
                         " AND NOT EXISTS (SELECT 1 FROM order_lifecycle l WHERE l.intent_id = order_intents.id)"
                         " AND NOT EXISTS (SELECT 1 FROM order_attempts a WHERE a.intent_id = order_intents.id)")
        self.con.execute("DELETE FROM feature_flags WHERE name IN ('executor_draining',"
                         " 'max_order_notional_micro')")
        self.con.commit()
        self.app.STORE.refresh()

    def intent(self, state: str, intent_id: str = "i-1"):
        self.con.execute(
            "INSERT INTO order_intents (id, user_id, market_id, token_id, side, price_micro, size_micro,"
            " notional_micro, state, idempotency_key, created_ms, updated_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            # the seeded demo fixtures the other API tests use, so the foreign keys hold
            (intent_id, "u-demo", "0xM1", "0xT10", "BUY", 500_000, 10_000_000, 5_000_000, state,
             "k-" + intent_id, 1, 1))
        self.con.commit()

    def status(self):
        r = self.client.get("/v1/admin/drain-status", headers=H)
        self.assertEqual(200, r.status_code, r.text)
        return r.json()


class TestDrainStatus(OpsBase):
    def test_an_empty_queue_is_safe_to_replace(self):
        body = self.status()
        self.assertTrue(body["safeToReplace"])
        self.assertEqual(0, body["blockingIntents"])
        self.assertFalse(body["draining"])

    def test_the_durable_queue_does_not_block_a_replace(self):
        """The design decision, pinned: `queued` is a row, not a conversation."""
        self.intent("queued")
        body = self.status()
        self.assertEqual(1, body["openIntents"])
        self.assertEqual(0, body["blockingIntents"])
        self.assertTrue(body["safeToReplace"])

    def test_pending_does_not_block_either(self):
        self.intent("pending")
        self.assertTrue(self.status()["safeToReplace"])

    def test_submitting_blocks_the_replace(self):
        self.intent("submitting")
        body = self.status()
        self.assertEqual(1, body["blockingIntents"])
        self.assertFalse(body["safeToReplace"])

    def test_uncertain_blocks_the_replace(self):
        """The ambiguous order: sent to the venue, no answer back. Replacing the process here orphans it."""
        self.intent("uncertain")
        body = self.status()
        self.assertEqual(["submitting", "uncertain"], body["blockingStates"])
        self.assertFalse(body["safeToReplace"])

    def test_the_draining_flag_blocks_even_an_empty_queue(self):
        r = self.client.post("/v1/admin/flags", headers=H,
                             json={"name": "executor_draining", "value": True, "reason": "deploy in progress"})
        self.assertEqual(200, r.status_code, r.text)
        body = self.status()
        self.assertTrue(body["draining"])
        self.assertFalse(body["safeToReplace"])

    def test_the_admin_token_is_required(self):
        self.assertEqual(403, self.client.get("/v1/admin/drain-status", headers={"X-Admin-Token": "wrong"}).status_code)

    def test_the_two_refusals_are_different_codes_because_they_are_different_problems(self):
        """A request with no token is 503 (a misconfiguration must not look like an attack in the dashboards the
        on-call reads); a wrong token is 403; and a box with no token configured is closed, not open."""
        r = self.client.get("/v1/admin/drain-status")
        self.assertEqual(503, r.status_code, r.text)
        self.assertEqual("SIGNER_UNAVAILABLE", r.json()["error"]["code"])
        saved = os.environ.pop("PGM_ADMIN_TOKEN")
        try:
            self.assertEqual(403, self.client.get("/v1/admin/drain-status", headers=H).status_code)
        finally:
            os.environ["PGM_ADMIN_TOKEN"] = saved


class TestInstantOff(OpsBase):
    def test_a_flag_write_needs_a_reason(self):
        r = self.client.post("/v1/admin/flags", headers=H,
                             json={"name": "executor_draining", "value": True, "reason": "ab"})
        self.assertEqual(422, r.status_code)
        self.assertEqual("BAD_REASON", r.json()["error"]["code"])

    def test_an_unknown_flag_is_refused_rather_than_created(self):
        """A typo that silently creates a switch nobody reads looks like a fix and is not one."""
        r = self.client.post("/v1/admin/flags", headers=H,
                             json={"name": "executor_drainign", "value": True, "reason": "typo, honestly"})
        self.assertEqual(404, r.status_code, r.text)
        self.assertEqual(0, self.con.execute(
            "SELECT COUNT(*) FROM feature_flags WHERE name='executor_drainign'").fetchone()[0])

    def test_the_switch_takes_effect_on_the_next_request_not_the_next_ttl(self):
        """The store's TTL is 5 s. A kill switch that takes five seconds is a kill switch with a five-second hole."""
        before = self.status()["draining"]
        self.assertFalse(before)
        self.client.post("/v1/admin/flags", headers=H,
                         json={"name": "executor_draining", "value": True, "reason": "synthetic drill"})
        self.assertTrue(self.status()["draining"])
        self.client.post("/v1/admin/flags", headers=H,
                         json={"name": "executor_draining", "value": False, "reason": "drill over"})
        self.assertFalse(self.status()["draining"])

    def test_every_flag_write_leaves_an_audit_row(self):
        self.client.post("/v1/admin/flags", headers=H,
                         json={"name": "executor_draining", "value": True, "reason": "because the drill says so"})
        row = self.con.execute("SELECT name, new_value, changed_by, reason FROM flag_audit"
                               " WHERE reason='because the drill says so' ORDER BY at_ms DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row, "no audit row for the flag change")
        self.assertEqual("executor_draining", row[0])
        self.assertEqual("admin", row[2])

    def test_a_numeric_flag_can_be_tightened_between_deploys(self):
        """The other half of instant-off: a money limit lowered without shipping code."""
        r = self.client.post("/v1/admin/flags", headers=H,
                             json={"name": "max_order_notional_micro", "value": 250_000_000,
                                   "reason": "incident: cap orders while we look"})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(250_000_000, self.app.flags().max_order_notional_micro)

    def test_the_list_shows_what_is_on(self):
        """The operator reading a phone needs to see the switch they are about to flip, and its stored value."""
        self.client.post("/v1/admin/flags", headers=H,
                         json={"name": "executor_draining", "value": True, "reason": "listing drill"})
        r = self.client.get("/v1/admin/flags", headers=H)
        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertIn("toggles", body)
        stored = {f["name"]: f["stored"] for f in body["flags"]}
        self.assertEqual({"value": True, "on": True}, stored.get("executor_draining"), stored)
        self.assertIn("executor_draining", body["toggles"])


if __name__ == "__main__":
    unittest.main()

def day_ago(n: int) -> str:
    import datetime as dt
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=n)).strftime("%Y-%m-%d")


class TestMetrics(OpsBase):
    """`/v1/admin/metrics` — the numbers a page at 2am is made of.

    Every test here asserts a DELTA: a row is written, the endpoint is read, and the number moves. A test that
    reads a zero out of an empty database passes for the wrong reason, and the P14 review has the scar to prove
    it (a probe that repaired the condition it was testing).
    """
    app_name = "p15-metrics"

    def setUp(self):
        super().setUp()
        self.now = self.app._now_ms()
        # Cleanup is limited to tables that are NOT append-only. The schema's triggers refuse a DELETE on
        # `fills`, `chain_events`, `order_lifecycle` and `kill_switch_state` once they hold a row — which is the
        # product keeping its own promise — so the tests for those measure a DELTA against a baseline instead of
        # assuming an empty table. (An earlier version of this class tried to clean them and went red in the
        # full-file run while passing in isolation: the worst kind of test failure to debug.)
        for table in ("reconcile_open", "venue_fees", "builder_revenue_daily", "ingest_cursors",
                      "order_attempts", "withdrawals"):
            self.con.execute("DELETE FROM %s" % table)
        self.con.commit()
        self.uid = uuid.uuid4().hex[:10]

    def metrics(self, headers=None) -> dict:
        r = self.client.get("/v1/admin/metrics", headers=H if headers is None else headers)
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    # -- auth ------------------------------------------------------------------------------------------------
    def test_metrics_is_admin_only_and_closed_rather_than_open(self):
        self.assertEqual(503, self.client.get("/v1/admin/metrics").status_code)
        self.assertEqual(403, self.client.get("/v1/admin/metrics",
                                              headers={"X-Admin-Token": "nope"}).status_code)
        self.assertEqual(200, self.client.get("/v1/admin/metrics", headers=H).status_code)

    def test_every_block_names_the_query_that_produced_it(self):
        m = self.metrics()
        for block, key in (("money", "unreconciled"), ("money", "positionDrift"), ("money", "builderFees")):
            self.assertIn("source", m[block][key], "%s.%s must say what was counted" % (block, key))
        self.assertIn("asOf", m)
        self.assertIn("staleAfter", m)

    # -- the single most important number --------------------------------------------------------------------
    def test_an_unreconciled_case_older_than_a_minute_is_visible_and_paging(self):
        before = self.metrics()["money"]["unreconciled"]
        self.assertEqual(0, before["count"])
        self.assertFalse(before["page"])
        self.con.execute("INSERT INTO reconcile_open (dedupe_key, case_name, intent_id, order_id, since_ms, note)"
                         " VALUES (?,?,?,?,?,?)",
                         ("drill:1", "no_ack", "i-1", "0xO1", self.now - 90_000, "the venue never acked"))
        self.con.commit()
        after = self.metrics()["money"]["unreconciled"]
        self.assertEqual(1, after["count"])
        self.assertGreaterEqual(after["oldestAgeMs"], 90_000)
        self.assertTrue(after["page"], "a case open for 90s must be paging; the threshold is 60s")
        self.assertEqual({"no_ack": 1}, after["byCase"])
        self.assertIn("reconcile_open", after["source"])

    def test_a_case_that_resolves_stops_paging(self):
        self.con.execute("INSERT INTO reconcile_open (dedupe_key, case_name, intent_id, order_id, since_ms, note)"
                         " VALUES (?,?,?,?,?,?)",
                         ("drill:2", "orphan", "i-2", "0xO2", self.now - 120_000, ""))
        self.con.commit()
        self.assertTrue(self.metrics()["money"]["unreconciled"]["page"])
        self.con.execute("DELETE FROM reconcile_open WHERE dedupe_key = ?", ("drill:2",))
        self.con.commit()
        self.assertFalse(self.metrics()["money"]["unreconciled"]["page"])

    def test_only_an_in_flight_state_counts_as_orders_unknown_state(self):
        before = self.metrics()["money"]["ordersUnknownState"]["count"]
        self.intent("submitting", "i-sub-" + self.uid)
        self.intent("queued", "i-que-" + self.uid)
        m = self.metrics()["money"]["ordersUnknownState"]
        self.assertEqual(before + 1, m["count"], "a queued intent is durable work, not an order in flight")
        self.assertEqual(["submitting", "uncertain"], m["states"])

    # -- drift and fees ---------------------------------------------------------------------------------------
    def test_position_drift_is_our_fills_against_the_venues_own_log(self):
        """`fills` and `chain_events` are append-only, so the assertion is a delta — and the delta has to move
        in BOTH directions, or a metric that is always zero would pass."""
        base = self.metrics()["money"]["positionDrift"]["mismatchedOrders"]

        matched = "0xO-" + self.uid + "-a"
        self._chain_fill(matched, matched_micro=5_000_000)
        self._fill(matched, size_micro=5_000_000)
        self.assertEqual(base, self.metrics()["money"]["positionDrift"]["mismatchedOrders"],
                         "a chain event with our matching fill is not drift")

        missing = "0xO-" + self.uid + "-b"
        self._chain_fill(missing, matched_micro=5_000_000)
        drift = self.metrics()["money"]["positionDrift"]
        self.assertEqual(base + 1, drift["mismatchedOrders"], "a venue fill we never recorded IS drift")
        self.assertGreaterEqual(drift["worstMicro"], 5_000_000)

        short = "0xO-" + self.uid + "-c"
        self._chain_fill(short, matched_micro=5_000_000)
        self._fill(short, size_micro=4_999_999)
        self.assertEqual(base + 2, self.metrics()["money"]["positionDrift"]["mismatchedOrders"])
        self.assertIn("OrderFilled", drift["source"])

    def test_fee_estimate_delta_is_measured_not_guessed(self):
        before = self.metrics()["money"]["feeEstimateDelta"]
        self.con.execute("INSERT INTO venue_fees (order_id, intent_id, est_platform_micro, est_builder_micro,"
                         " actual_platform_micro, delta_micro, at_ms) VALUES (?,?,?,?,?,?,?)",
                         ("0xO-fund-" + self.uid, "i-1", 100, 20, 90, 10, self.now))
        self.con.commit()
        after = self.metrics()["money"]["feeEstimateDelta"]
        self.assertEqual(before["measured"] + 1, after["measured"])
        self.assertEqual(before["sumAbsMicro"] + 10, after["sumAbsMicro"])
        self.assertGreaterEqual(after["worstMicro"], 10)

    def test_builder_fees_are_two_independent_measurements(self):
        self.con.execute("INSERT INTO builder_revenue_daily (day, orders, volume_micro, expected_micro,"
                         " chain_micro, delta_micro, status, updated_ms) VALUES (?,?,?,?,?,?,?,?)",
                         (day_ago(1), 5, 10_000_000, 2_000, 1_975, 25, "unreconciled", self.now))
        self.con.execute("INSERT INTO builder_revenue_daily (day, orders, volume_micro, expected_micro,"
                         " chain_micro, delta_micro, status, updated_ms) VALUES (?,?,?,?,?,?,?,?)",
                         (day_ago(2), 3, 6_000_000, 1_200, 1_200, 0, "matched", self.now))
        self.con.commit()
        bf = self.metrics()["money"]["builderFees"]
        self.assertEqual(2, bf["days"])
        self.assertEqual(8, bf["orders"])
        self.assertEqual(3_200, bf["expectedMicro"])
        self.assertEqual(3_175, bf["chainMeasuredMicro"])
        self.assertEqual(25, bf["deltaMicro"])
        self.assertEqual(1, bf["daysUnmatched"])
        self.assertTrue(bf["independent"], "the chain measurement must not read our expectation")

    # -- freshness --------------------------------------------------------------------------------------------
    def test_a_socket_that_stopped_delivering_frames_is_silent_not_lagging(self):
        self.con.execute("INSERT INTO ingest_cursors (source, last_event_ms, last_frame_ms, state, updated_ms)"
                         " VALUES (?,?,?,?,?)", ("ws.tape", self.now - 400_000, self.now - 300_000, "ok", self.now))
        self.con.commit()
        f = self.metrics()["freshness"]
        self.assertIn("ws.tape", f["silent"])
        self.assertTrue(f["feeds"][0]["silent"])
        self.con.execute("UPDATE ingest_cursors SET last_frame_ms = ? WHERE source = 'ws.tape'", (self.now,))
        self.con.commit()
        f = self.metrics()["freshness"]
        self.assertNotIn("ws.tape", f["silent"])
        self.assertFalse(f["feeds"][0]["silent"])

    # -- business ---------------------------------------------------------------------------------------------
    def test_a_withdrawal_spike_is_visible_as_a_ratio(self):
        self.assertEqual(0.0, self.metrics()["business"]["withdrawalSpikeRatio"])
        for i in range(4):
            self._withdrawal("w-%d" % i, 1_000_000)
        ratio = self.metrics()["business"]["withdrawalSpikeRatio"]
        self.assertGreater(ratio, 3.0, "four withdrawals in the last hour against a flat day must stand out")

    # -- kill switch ------------------------------------------------------------------------------------------
    def test_the_kill_switch_is_on_every_dashboard_with_its_age(self):
        self.con.execute("INSERT INTO kill_switch_state (engaged, reason, changed_by, at_ms) VALUES (?,?,?,?)",
                         (1, "drill", "tester", self.now - 5_000))  # append-only: never cleaned, always newest
        self.con.commit()
        ks = self.metrics()["killSwitch"]
        self.assertTrue(ks["engaged"])
        self.assertGreaterEqual(ks["engagedForMs"], 5_000)
        self.con.execute("INSERT INTO kill_switch_state (engaged, reason, changed_by, at_ms) VALUES (?,?,?,?)",
                         (0, "drill over", "tester", self.now))
        self.con.commit()
        self.assertFalse(self.metrics()["killSwitch"]["engaged"])

    # -- the order path ---------------------------------------------------------------------------------------
    def test_the_hops_carry_percentiles_from_the_attempt_rows(self):
        iid = "i-hop-" + self.uid
        self.intent("submitted", iid)
        self.con.execute("INSERT INTO order_lifecycle (order_id, intent_id, user_id, state, reason, source,"
                         " show_as_working, at_ms) VALUES (?,?,?,?,?,?,?,?)",
                         ("0xO1", iid, "u-demo", "draft", "", "system", 1, self.now - 500))
        self.con.execute("INSERT INTO order_attempts (client_order_hash, intent_id, user_id, token_id, side,"
                         " price_micro, size_micro, order_type, signature_type, builder_code, payload_digest,"
                         " signed_ms, submitted_ms, ack_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("h-" + self.uid, iid, "u-demo", "0xT10", "BUY", 500_000, 10_000_000, "GTC", 3, "",
                          "digest", self.now - 400, self.now - 300, self.now - 200))
        self.con.commit()
        hops = self.metrics()["orderPath"]["hops"]
        self.assertEqual(1, hops["draftToSigned"]["n"], "order_attempts is cleared in setUp, so n is this row")
        self.assertEqual(100, hops["draftToSigned"]["p50"], hops)
        self.assertEqual(100, hops["signedToSubmitted"]["p50"], hops)
        self.assertEqual(100, hops["submittedToAck"]["p50"], hops)
        self.assertEqual(300, hops["endToEnd"]["p50"], hops)

    def test_rejections_are_counted_by_reason_code(self):
        before = self.metrics()["orderPath"]
        rows = (("i-r1-" + self.uid, "OVER_ORDER_CAP"), ("i-r2-" + self.uid, "OVER_ORDER_CAP"),
                ("i-r3-" + self.uid, "STALE_QUOTE"))
        for iid, code in rows:
            self.intent("rejected", iid)
            # the helper writes a fixed updated_ms of 1; "the last hour" is the window under test, so age the row
            # to now — a probe that leaves the fixture's clock alone tests nothing
            self.con.execute("UPDATE order_intents SET risk_code = ?, updated_ms = ? WHERE id = ?",
                             (code, self.now, iid))
        self.con.commit()
        after = self.metrics()["orderPath"]
        self.assertEqual(before["rejectionsLastHour"] + 3, after["rejectionsLastHour"])
        by = after["rejectionsByReason"]
        was = before["rejectionsByReason"]
        self.assertEqual((was.get("OVER_ORDER_CAP", 0) or 0) + 2, by.get("OVER_ORDER_CAP"))
        self.assertEqual((was.get("STALE_QUOTE", 0) or 0) + 1, by.get("STALE_QUOTE"))

    # -- helpers ----------------------------------------------------------------------------------------------
    def _chain_fill(self, order_id, matched_micro):
        self.con.execute("INSERT INTO chain_events (source, kind, tx_hash, log_index, order_id, matched_micro,"
                         " seen_ms) VALUES (?,?,?,?,?,?,?)",
                         ("chain_log", "OrderFilled", "0xtx" + order_id, 1, order_id, matched_micro, self.now))
        self.con.commit()

    def _fill(self, order_id, size_micro):
        # `orders` needs an existing intent (FK) and the state column is `state`, not `status`; the intent here
        # is a throwaway row that exists only so the fill can hang off it.
        self.con.execute("INSERT INTO order_intents (id, user_id, market_id, token_id, side, price_micro,"
                         " size_micro, notional_micro, state, idempotency_key, created_ms, updated_ms)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("i-" + order_id, "u-demo", "0xM1", "0xT10", "BUY", 500_000, size_micro,
                          size_micro // 2, "submitted", "k-" + order_id, self.now, self.now))
        self.con.execute("INSERT INTO orders (id, intent_id, user_id, market_id, token_id, side, price_micro,"
                         " size_micro, size_matched_micro, state, acknowledged, builder_code, placed_ms,"
                         " updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (order_id, "i-" + order_id, "u-demo", "0xM1", "0xT10", "BUY", 500_000, size_micro,
                          size_micro, "filled", 1, "", self.now, self.now))
        self.con.execute("INSERT INTO fills (order_id, side, price_micro, size_micro, notional_micro, fee_micro,"
                         " maker, exchange_ts, ingest_ms, source, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (order_id, "BUY", 500_000, size_micro, size_micro // 2, 0, 0, self.now, self.now,
                          "reconcile", "{}"))
        self.con.commit()

    def _withdrawal(self, wid, amount_micro):
        self.con.execute("INSERT INTO withdrawals (user_id, asset, chain, amount_micro, dest_address,"
                         " typed_amount, typed_address, status, idempotency_key, requested_ms)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?)",
                         ("u-demo", "USDC", "base", amount_micro, "0xabc", "1.00", "0xabc", "queued",
                          "wd-" + wid, self.now))
        self.con.commit()

class TestOperationsPillars(OpsBase):
    """The four D4 blocks the kit names explicitly, added in P15 D5 so the alarm registry has a fact to read.

    Each of these exists because an alarm needed it, and each assertion is a **delta**: the row goes in, the
    payload changes, the row comes out. A block that renders a plausible zero is worse than a gap, so the tests
    also pin the two states that must never look healthy — an executor that has *never* beaten, and a feed that
    has never been seen.
    """

    app_name = "p15-pillars"

    def setUp(self):
        super().setUp()
        self.now = self.app._now_ms()
        for sql in ("DELETE FROM executor_state",
                    "DELETE FROM builder_code_status WHERE code = 'bc_test'",
                    "DELETE FROM deposits WHERE credit_key LIKE 'k-p15-%'",
                    "DELETE FROM withdrawals WHERE idempotency_key LIKE 'wd-p15-%'"):
            self.con.execute(sql)
        self.con.execute("DELETE FROM ingest_cursors WHERE source LIKE 'ws.p15%'")
        self.con.commit()

    def metrics(self) -> dict:
        r = self.client.get("/v1/admin/metrics", headers=H)
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def test_an_executor_that_never_beat_is_not_reported_as_live(self):
        """`state: never` and `lastBeatAgeMs: null` — the distinction that stops a fresh box reading as healthy."""
        ex = self.metrics()["executor"]
        self.assertEqual("never", ex["state"])
        self.assertIsNone(ex["lastBeatAgeMs"])
        self.assertEqual(0, ex["ticks"])

    def test_a_beat_older_than_the_page_threshold_reads_as_down_and_a_fresh_one_does_not(self):
        """The threshold is the alarm's `for_ms`, and it is read from the payload so the two cannot drift."""
        self.con.execute("INSERT INTO executor_state (id, at_ms, pid, version, ticks, note) VALUES (1,?,?,?,?,?)",
                         (self.now - 120_000, "4242", "test-sha", 7, ""))
        self.con.commit()
        ex = self.metrics()["executor"]
        self.assertEqual("down", ex["state"])
        self.assertGreater(ex["lastBeatAgeMs"], ex["pageAfterMs"])
        self.con.execute("UPDATE executor_state SET at_ms=?, ticks=8 WHERE id=1", (self.now,))
        self.con.commit()
        ex = self.metrics()["executor"]
        self.assertEqual("live", ex["state"])
        self.assertLess(ex["lastBeatAgeMs"], ex["pageAfterMs"])
        self.assertEqual("4242", ex["pid"])
        self.assertEqual(8, ex["ticks"])

    def test_the_drain_flag_appears_on_the_executor_block(self):
        """The kit asks for the kill switch on every dashboard; the drain flag belongs next to the beat for the
        same reason — a deploy that is draining and a deploy that is not look identical otherwise."""
        self.client.post("/v1/admin/flags", headers=H,
                         json={"name": "executor_draining", "value": True, "reason": "drain for the drill"})
        self.assertTrue(self.metrics()["executor"]["draining"])
        self.client.post("/v1/admin/flags", headers=H,
                         json={"name": "executor_draining", "value": False, "reason": "drain drill finished"})

    def test_a_suspension_and_an_export_request_are_compromise_indicators(self):
        """Three named rows, never a score: an export request, a suspension, and a revocation job opened."""
        before = self.metrics()["security"]
        self.con.execute("INSERT INTO wallet_events (user_id, event, detail_json, actor, at_ms)"
                         " VALUES (?,?,?,?,?)", ("u-demo", "export_requested", "{}", "user", self.now))
        self.con.commit()
        after = self.metrics()["security"]
        self.assertEqual(before["compromiseIndicators24h"] + 1, after["compromiseIndicators24h"])
        self.assertEqual(before["exportRequests24h"] + 1, after["exportRequests24h"])
        # and the event is named, so the notification can say which kind of indicator fired
        self.assertEqual(1, after["events"].get("export_requested"))

    def test_an_unconfigured_builder_code_says_unconfigured_rather_than_unknown(self):
        """Two different problems: "we never set it" is a configuration error, "the venue told us nothing" is an
        incident. Collapsing them would page somebody about a typo, or hide the venue's answer behind one."""
        bc = self.metrics()["builderCode"]
        self.assertIn(bc["state"], ("unconfigured", "unknown", "active", "disabled", "throttled"))
        self.assertEqual(bc["state"] == "unconfigured", bc["code"] is None)

    def test_a_builder_code_the_venue_disabled_is_visible_with_its_rejection_count(self):
        self.con.execute("INSERT INTO builder_code_status (code, state, last_seen_ms, changed_ms, reject_count,"
                         " source, note) VALUES (?,?,?,?,?,?,?)",
                         ("bc_test", "disabled", self.now, self.now, 4, "venue_rejection", "the venue said so"))
        self.con.commit()
        import importlib
        os.environ["PGM_BUILDER_CODE"] = "bc_test"
        try:
            bc = self.metrics()["builderCode"]
            self.assertEqual("bc_test", bc["code"])
            self.assertEqual("disabled", bc["state"])
            self.assertEqual(4, bc["rejectCount"])
            self.assertEqual({"disabled": 1}, bc["states"])
        finally:
            os.environ.pop("PGM_BUILDER_CODE", None)

    def test_an_uncredited_deposit_moves_the_stuck_counter_and_pages_only_after_the_threshold(self):
        """Two deposits: one just seen (not a page) and one older than the threshold (a page)."""
        self.con.execute("INSERT INTO deposits (user_id, asset, chain, amount_micro, credit_key, status,"
                         " first_seen_ms) VALUES (?,?,?,?,?,?,?)",
                         ("u-demo", "USDC", "base", 5_000_000, "k-p15-fresh", "detecting", self.now))
        self.con.commit()
        b = self.metrics()["business"]["stuckDeposits"]
        self.assertEqual(1, b["count"])
        self.assertFalse(b["page"], "a deposit seen a minute ago is not a page yet")
        self.con.execute("INSERT INTO deposits (user_id, asset, chain, amount_micro, credit_key, status,"
                         " first_seen_ms) VALUES (?,?,?,?,?,?,?)",
                         ("u-demo", "USDC", "base", 5_000_000, "k-p15-old", "bridging",
                          self.now - 2_000_000))
        self.con.commit()
        b = self.metrics()["business"]["stuckDeposits"]
        self.assertEqual(2, b["count"])
        self.assertTrue(b["page"])
        self.assertGreaterEqual(b["oldestAgeMs"], 2_000_000)

    def test_a_withdrawal_in_flight_is_counted_until_it_completes(self):
        self.con.execute("INSERT INTO withdrawals (user_id, asset, chain, amount_micro, dest_address,"
                         " typed_amount, typed_address, status, idempotency_key, requested_ms)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?)",
                         # `queued` rather than `submitted`: the schema's own CHECK requires both notification
                         # flags before a withdrawal may be marked submitted, and this test is about the
                         # in-flight counter, not about the notification contract.
                         ("u-demo", "USDC", "base", 2_000_000, "0xabc", "2.00", "0xabc", "queued",
                          "wd-p15-1", self.now - 1_000_000))
        self.con.commit()
        w = self.metrics()["business"]["withdrawalsInFlight"]
        self.assertGreaterEqual(w["count"], 1)
        self.assertGreaterEqual(w["oldestAgeMs"], 1_000_000)

    def test_a_feed_carries_its_transport_its_own_threshold_and_its_resync_count(self):
        """The per-source fields the kit asks for: WS state, resync count, and a threshold that belongs to the
        source rather than to the dashboard. `resyncs: null` means "not reported", never "zero"."""
        self.con.execute("INSERT INTO ingest_cursors (source, cursor_json, last_event_ms, last_frame_ms, state,"
                         " updated_ms) VALUES (?,?,?,?,?,?)",
                         ("ws.p15tape", '{"resyncs": 3}', self.now - 500, self.now - 400, "ok", self.now))
        self.con.commit()
        feeds = {f["source"]: f for f in self.metrics()["freshness"]["feeds"]}
        feed = feeds["ws.p15tape"]
        self.assertEqual("ws", feed["transport"])
        self.assertEqual(3, feed["resyncs"])
        self.assertFalse(feed["silent"])
        self.assertFalse(feed["lagging"])
        self.assertEqual(3_000, feed["thresholdMs"], "a tape feed is measured against the tape threshold")
        self.assertEqual([], self.metrics()["freshness"]["lagging"])

    def test_a_lagging_feed_is_named_without_being_called_silent(self):
        """The distinction the freshness module exists for: a quiet market (lagging) is not a dead pipe (silent).
        Only the second one pages."""
        self.con.execute("INSERT INTO ingest_cursors (source, cursor_json, last_event_ms, last_frame_ms, state,"
                         " updated_ms) VALUES (?,?,?,?,?,?)",
                         ("ws.p15quiet", "{}", self.now - 30_000, self.now - 100, "lagging", self.now))
        self.con.commit()
        fresh = self.metrics()["freshness"]
        feed = {f["source"]: f for f in fresh["feeds"]}["ws.p15quiet"]
        self.assertTrue(feed["lagging"])
        self.assertFalse(feed["silent"])
        self.assertIn("ws.p15quiet", fresh["lagging"])
        self.assertNotIn("ws.p15quiet", fresh["silent"])
        self.assertIsNone(feed["resyncs"], "a feed that never reported resyncs says null, not zero")
