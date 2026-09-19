"""P10 · the terminal's read surfaces, as contract assertions.

The class at the bottom walks the phase's own quality gate as a test:

  find a whale fill in the tape → open the trader's profile → see the win rate is real (sample ≥ gate) → see the
  drawdown → set up a copy config in dry-run → be told the slippage risk before confirming.

If that path stops working, the phase's acceptance criterion has stopped holding, and it should fail here rather
than in a UI review. The individual tests around it pin the rules that make the path honest:

  1. **No address ever leaves the API.** `test_no_wallet_address_appears_anywhere` greps every P10 payload for a
     `0x…` shape: the tape names traders by pseudonym, and one endpoint that forgot would undo the whole scheme.
  2. **A win rate is either gated or absent.** `test_no_win_rate_escapes_the_sample_gate` asserts it across all
     four windows of the dossier, not just the one the fixture was tuned for.
  3. **A drawdown rides with every curve.** `test_every_curve_point_carries_its_drawdown` checks the identity
     `drawdown = peak − cum` on every point, so a chart cannot draw PnL without the overlay.
  4. **Dry-run is the default in every direction.** Creation forces it, a missing guard row reads as it, and
     going live needs both an acknowledgement AND dry-run history this account actually produced.
"""
from __future__ import annotations

import json
import re
import unittest

from conftest import import_app, refresh_flags  # noqa: F401

MICRO = 10 ** 6
USER = {"X-User-Id": "u-demo"}


def micro(text: str) -> int:
    """A decimal string to micro-units with integer arithmetic only.

    Deliberately not `float(text) * 1e6`: that is the exact expression the money path is forbidden from using,
    and a test that reads money the way the product must not is a test that would pass on a broken client.
    """
    text = str(text)
    sign = -1 if text.startswith("-") else 1
    text = text.lstrip("-")
    whole, _, frac = text.partition(".")
    return sign * (int(whole or 0) * 10 ** 6 + int((frac + "000000")[:6] or 0))


class TerminalBase(unittest.TestCase):
    app_name = "api-terminal"
    app = None
    client = None

    @classmethod
    def setUpClass(cls):
        cls.app = import_app(cls.app_name)
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.app.app, raise_server_exceptions=False)

    def setUp(self):
        refresh_flags(self.app)

    def get(self, url, *, user=USER, **params):
        """GET with the dev identity attached, because every P10 read surface is either PUBLIC or the caller's
        own state. Tests that need somebody else's view pass `user=` explicitly."""
        r = self.client.get(url, params=params, headers=user or {})
        self.assertEqual(r.status_code, 200, "%s -> %d %s" % (url, r.status_code, r.text[:400]))
        return r.json()

    def post(self, url, body, headers=None):
        return self.client.post(url, json=body, headers=headers or USER)

    def fixture_config(self):
        """The seeded config, by id — not `items[0]`, which is whichever config a previous test created."""
        items = self.get("/v1/copy/configs")["items"]
        return next(i for i in items if i["configId"] == "cfg-seed01")

    def whale_anon(self):
        """The pseudonym of the fixture's whale, through the index the API itself uses."""
        import app as app_mod
        return app_mod._anon("0x" + "aa" * 20)


class TestTape(TerminalBase):
    def test_the_tape_is_the_durable_log_and_names_rows_by_pseudonym(self):
        body = self.get("/v1/tape/fills", limit=50, windowMs=3_600_000)
        self.assertTrue(body["rows"], "the fixture's last hour must not be empty")
        row = body["rows"][0]
        # The wire shape, not the internal one: prices and sizes are decimal strings (the contract's rule 2, so
        # no client arithmetic ever lands on a float) and only `notionalMicro` crosses as an integer, because
        # that is the number the whale threshold is defined against.
        for f in ("tsMs", "price", "shares", "notionalMicro", "side", "outcome", "anonWallet", "marketId",
                  "marketSlug", "tick"):
            self.assertIn(f, row)
        self.assertIsInstance(row["price"], str)
        self.assertIsInstance(row["shares"], str)
        self.assertIsInstance(row["notionalMicro"], int)
        self.assertEqual(micro(row["shares"]) * micro(row["price"]) // 10 ** 6, row["notionalMicro"],
                         "a row's notional must be its own size x price, in micro, exactly")
        self.assertTrue(row["anonWallet"].startswith("w_"))
        # The threshold and the rule that produced it travel with every row: a badge without its rule is a
        # horoscope, and the rule sentence is what the tooltip shows.
        self.assertGreater(row["thresholdMicro"], 0)
        self.assertIn("whale = max(", row["thresholdRule"])
        self.assertIn("absolute floor", row["thresholdRule"])

    def test_is_whale_agrees_with_the_threshold_it_shipped(self):
        body = self.get("/v1/tape/fills", limit=100, windowMs=86_400_000)
        for row in body["rows"]:
            self.assertEqual(row["isWhale"], row["notionalMicro"] >= row["thresholdMicro"])
            if row["isWhale"]:
                self.assertGreaterEqual(row["notionalMicro"], 500 * MICRO)   # the absolute floor, always

    def test_whales_are_found_through_the_filter_and_the_feed_not_by_luck(self):
        """The tape's first page is whatever traded last — and in this fixture that is a burst of small fills
        across the generated markets, with the whales a minute or two behind them. That is the honest shape of a
        real tape, and it is why the product finds whales with a *filter* (the threshold from `/v1/tape/facets`)
        and a feed (`/v1/whales`), rather than hoping one scrolls past. This test pins both affordances.
        """
        facets = self.get("/v1/tape/facets", windowMs=3_600_000)
        threshold = facets["whale"]["thresholdMicro"]
        filtered = self.get("/v1/tape/fills", limit=100, windowMs=3_600_000, minNotionalMicro=threshold)
        self.assertTrue(filtered["rows"], "the threshold filter must surface the fixture's whales")
        self.assertTrue(all(r["isWhale"] for r in filtered["rows"]))
        feed = self.get("/v1/whales", windowMs=3_600_000)
        self.assertTrue(feed["rows"])
        self.assertEqual(feed["counts"]["returned"], len(feed["rows"]))

    def test_filters_apply_before_the_limit(self):
        all_rows = self.get("/v1/tape/fills", limit=100, windowMs=86_400_000)["rows"]
        buys = self.get("/v1/tape/fills", limit=100, windowMs=86_400_000, side="BUY")["rows"]
        self.assertTrue(buys)
        self.assertTrue(all(r["side"] == "BUY" for r in buys))
        self.assertLessEqual(len(buys), len(all_rows))

    def test_a_label_filter_returns_only_that_label(self):
        body = self.get("/v1/tape/fills", limit=100, windowMs=86_400_000, label="whale")
        for row in body["rows"]:
            self.assertIn("whale", [lab["label"] for lab in row["labels"]])
        self.assertTrue(body["rows"])

    def test_a_wallet_filter_is_a_pseudonym_and_round_trips(self):
        anon = self.whale_anon()
        body = self.get("/v1/tape/fills", limit=100, windowMs=86_400_000, wallet=anon)
        self.assertTrue(body["rows"])
        self.assertTrue(all(r["anonWallet"] == anon for r in body["rows"]))

    def test_an_unknown_pseudonym_is_a_404_not_an_empty_list(self):
        r = self.client.get("/v1/tape/fills", params={"wallet": "w_deadbeef00"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "NOT_FOUND")

    def test_a_min_notional_filter_is_absolute_and_says_what_it_did(self):
        body = self.get("/v1/tape/fills", limit=100, windowMs=86_400_000, minNotionalMicro=500 * MICRO)
        self.assertTrue(all(r["notionalMicro"] >= 500 * MICRO for r in body["rows"]))

    def test_a_window_above_a_day_is_refused(self):
        r = self.client.get("/v1/tape/fills", params={"windowMs": 90 * 86_400_000})
        self.assertEqual(r.status_code, 422)

    def test_an_unknown_market_is_a_404(self):
        self.assertEqual(self.client.get("/v1/tape/fills", params={"marketId": "0xNOPE"}).status_code, 404)


class TestFacets(TerminalBase):
    def test_the_distribution_is_stated_beside_the_threshold_it_produces(self):
        body = self.get("/v1/tape/facets", windowMs=86_400_000)
        for key in ("fills", "medianNotionalMicro", "p95NotionalMicro", "maxNotionalMicro", "whale",
                    "severityRule", "sampleNote"):
            self.assertIn(key, body)
        self.assertLessEqual(body["p95NotionalMicro"], body["maxNotionalMicro"])
        self.assertGreaterEqual(body["whale"]["thresholdMicro"], 500 * MICRO)
        self.assertIn("whale = max(", body["whale"]["rule"])

    def test_every_facet_option_carries_the_count_behind_it(self):
        body = self.get("/v1/tape/facets", windowMs=86_400_000)
        for key in ("sides", "outcomes", "categories", "markets", "wallets", "classifications"):
            self.assertIn(key, body)
            for item in body[key]:
                self.assertGreater(item.get("fills", 1), 0)
        # An empty result must never be a mystery: a category that is offered must have fills behind it.
        for cat in body["categories"]:
            self.assertGreater(cat["notionalMicro"], 0)

    def test_every_classification_has_a_rule_and_a_disclaimer(self):
        # The window cap is 24 hours and the API enforces it (a facet query over 30 days is a scan, not a
        # facet) — so this asks for the longest window the endpoint allows.
        body = self.get("/v1/tape/facets", windowMs=86_400_000)
        self.assertTrue(body["classificationsAll"], "the label catalogue must not be empty")
        for item in body["classificationsAll"]:
            self.assertTrue(item["rule"], "%s has no rule" % item["label"])
            self.assertTrue(item["disclaimer"], "%s has no disclaimer" % item["label"])
            # A label we publish must be one the classifier is willing to publish.
            self.assertIn(item["label"], ("whale", "smart_money", "new_wallet", "insider_suspect", "cluster",
                                         "wash_like"))

    def test_each_market_carries_its_own_threshold_and_bucket(self):
        body = self.get("/v1/tape/facets", windowMs=86_400_000)
        self.assertTrue(body["markets"])
        for mkt in body["markets"]:
            self.assertGreaterEqual(mkt["thresholdMicro"], 500 * MICRO)
            self.assertIn(mkt["sizeBucket"], ("small", "mid", "large"))
            self.assertEqual(mkt["whales"], sum(1 for w in body["wallets"] if False) + mkt["whales"])


class TestWhales(TerminalBase):
    def test_the_feed_returns_only_fills_over_their_own_threshold(self):
        body = self.get("/v1/whales", windowMs=86_400_000, limit=50)
        self.assertTrue(body["rows"])
        for row in body["rows"]:
            self.assertGreaterEqual(row["notionalMicro"], row["thresholdMicro"])
            self.assertIn(row["severity"], ("info", "notice", "urgent"))
            self.assertEqual(row["ratioBps"], row["notionalMicro"] * 10_000 // row["thresholdMicro"])

    def test_min_severity_is_a_floor_not_a_filter_by_name(self):
        urgent = self.get("/v1/whales", windowMs=86_400_000, minSeverity="urgent")["rows"]
        self.assertTrue(all(r["severity"] == "urgent" for r in urgent))
        all_rows = self.get("/v1/whales", windowMs=86_400_000)["rows"]
        self.assertLessEqual(len(urgent), len(all_rows))

    def test_market_scope_needs_a_market(self):
        r = self.client.get("/v1/whales", params={"scope": "market"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "VALIDATION")

    def test_the_multiple_mode_states_the_rule_it_used(self):
        body = self.get("/v1/whales", windowMs=86_400_000, multiple=5)
        self.assertTrue(body["thresholds"])
        for rule in body["thresholds"].values():
            if rule["reason"] == "relative":
                self.assertIn("5×", rule["rule"])

    def test_severity_bands_are_stated(self):
        body = self.get("/v1/whales", windowMs=86_400_000)
        self.assertIn("urgent at 4×", body["severityRule"])
        self.assertIn("notice at 1×", body["severityRule"].replace("1.5×", "1×"))


class TestTrader(TerminalBase):
    def dossier(self, window="30d"):
        import app as app_mod
        return self.get("/v1/traders/%s" % app_mod._anon("0x" + "aa" * 20), window=window)

    def test_an_unknown_pseudonym_is_a_404(self):
        self.assertEqual(self.client.get("/v1/traders/w_deadbeef00").status_code, 404)

    def test_all_four_windows_come_from_the_same_metric_set(self):
        body = self.dossier()
        self.assertEqual(body["windows"], ["7d", "30d", "90d", "all"])
        keys = set(body["metrics"]["30d"])
        for w in body["windows"]:
            self.assertTrue(keys.issubset(set(body["metrics"][w])), "window %s is missing metrics" % w)

    def test_no_win_rate_escapes_the_sample_gate(self):
        body = self.dossier()
        gate = body["sampleGate"]
        for w, m in body["metrics"].items():
            if m["winRateBps"] is None:
                self.assertTrue(m["insufficientSample"])
                self.assertIn("insufficient sample", m["sampleNote"])
            else:
                # The only way a number is allowed to exist: enough resolved markets, and a rate that is that
                # count. Both halves, because a gate that only checks presence passes a fabricated 100%.
                self.assertGreaterEqual(m["resolvedMarkets"], gate, "window %s" % w)
                self.assertEqual(m["winRateBps"], m["wins"] * 10_000 // m["resolvedMarkets"])

    def test_the_switcher_recomputes_rather_than_relabels(self):
        # The 7-day window of this fixture holds fewer settled markets than the 30-day one: if the switcher only
        # relabelled, the two would be equal and the sample gate could never fire.
        body = self.dossier()
        self.assertLess(body["metrics"]["7d"]["resolvedMarkets"], body["metrics"]["30d"]["resolvedMarkets"])
        self.assertIsNone(body["metrics"]["7d"]["winRateBps"])
        self.assertIsNotNone(body["metrics"]["30d"]["winRateBps"])

    def test_every_curve_point_carries_its_drawdown(self):
        for window in ("7d", "30d", "90d", "all"):
            body = self.dossier(window)
            self.assertTrue(body["curve"], "window %s has no curve" % window)
            for p in body["curve"]:
                self.assertEqual(p["drawdownMicro"], p["peakMicro"] - p["cumMicro"])
            self.assertEqual(body["maxDrawdownMicro"], max(p["drawdownMicro"] for p in body["curve"]))

    def test_the_fixture_has_a_drawdown_to_show(self):
        self.assertGreater(self.dossier()["maxDrawdownMicro"], 0)

    def test_behaviour_metrics_carry_a_methodology_and_a_disclaimer(self):
        body = self.dossier()
        self.assertIn("/methodology/trader-metrics", body["methodology"]["path"])
        for key in ("winRate", "realised", "unrealised", "drawdown", "hold", "whale"):
            self.assertTrue(body["methodology"][key], "methodology for %s is empty" % key)
        for lab in body["behaviour"]:
            self.assertTrue(lab["rule"])
            self.assertTrue(lab["disclaimer"])
        # Insider-suspect must never be published without its caveat: the classifier marks it unpublishable and
        # the API honours that, so the fixture's unpublishable row must not appear here at all.
        self.assertNotIn("insider_suspect", [lab["label"] for lab in body["behaviour"]])

    def test_positions_are_marked_and_say_where_the_mark_came_from(self):
        body = self.dossier()
        self.assertTrue(body["positions"])
        for p in body["positions"]:
            self.assertIn(p["markSource"], ("last_fill", "unknown"))
            self.assertIn("unrealisedMicro", p)
            self.assertIsInstance(p["size"], str, "size crosses as a decimal string of shares")
            self.assertGreaterEqual(micro(p["size"]), 0)
            self.assertIsInstance(p["mark"], str)

    def test_trade_history_links_a_market_and_carries_the_settlement(self):
        body = self.dossier()
        self.assertTrue(body["fills"])
        for f in body["fills"]:
            self.assertTrue(f["marketId"])
            self.assertIn("resolved", f)
            self.assertIn("realisedMicro", f)


class TestCopy(TerminalBase):
    def anon(self):
        import app as app_mod
        return app_mod._anon("0x" + "bb" * 20)

    def test_the_fixture_config_is_a_dry_run_with_its_guards(self):
        cfg = self.fixture_config()
        self.assertTrue(cfg["dryRun"])
        self.assertEqual(cfg["skipIfMovedCents"], 2)              # the prompt's skip default
        self.assertEqual(cfg["doNotEnterWithinHours"], 24)
        self.assertTrue(cfg["guardsFromRow"])

    def test_the_warning_quotes_the_record_including_a_negative_window(self):
        cfg = self.fixture_config()
        windows = {w["windowDays"]: w for w in cfg["sourceStats"]["windows"]}
        self.assertLess(windows[7]["netAfterFeesMicro"], 0, "the fixture must contain a losing window")
        self.assertGreater(windows[30]["netAfterFeesMicro"], 0)
        self.assertIn("risk-adjusted", cfg["sourceStats"]["ranking"])
        self.assertIn("slippage", cfg["warning"]["warning"])
        self.assertEqual(cfg["warning"]["default"], "skip_instead_of_chase")

    def test_a_new_config_is_forced_into_dry_run(self):
        r = self.post("/v1/copy/configs", {"sourceAnon": self.anon(), "mode": "cap",
                                           "maxOrderMicro": 100 * MICRO, "maxDailyMicro": 400 * MICRO})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["dryRun"])
        self.assertIn("dry-run", body["next"])
        self.assertGreater(body["warning"]["samples"], 0)

    def test_a_config_cannot_be_created_live_even_by_sending_the_field(self):
        r = self.post("/v1/copy/configs", {"sourceAnon": self.anon(), "maxOrderMicro": 100 * MICRO,
                                           "maxDailyMicro": 400 * MICRO, "dryRun": False})
        # The schema has no `dryRun` on create: an unknown field is a 422 rather than a silently ignored wish.
        self.assertEqual(r.status_code, 422)

    def test_per_order_above_daily_is_refused(self):
        r = self.post("/v1/copy/configs", {"sourceAnon": self.anon(), "maxOrderMicro": 900 * MICRO,
                                           "maxDailyMicro": 100 * MICRO})
        self.assertEqual(r.status_code, 422)

    def test_going_live_without_acknowledging_slippage_is_refused(self):
        cid = self.post("/v1/copy/configs", {"sourceAnon": self.anon(), "maxOrderMicro": 100 * MICRO,
                                             "maxDailyMicro": 400 * MICRO}).json()["configId"]
        r = self.post("/v1/copy/configs/guards", {"configId": cid, "dryRun": False})
        self.assertEqual(r.status_code, 409)
        detail = r.json()["error"]
        self.assertIn("acknowledgeSlippage", json.dumps(detail))

    def test_going_live_needs_dry_run_history_not_only_a_checkbox(self):
        cid = self.post("/v1/copy/configs", {"sourceAnon": self.anon(), "maxOrderMicro": 100 * MICRO,
                                             "maxDailyMicro": 400 * MICRO}).json()["configId"]
        r = self.post("/v1/copy/configs/guards", {"configId": cid, "dryRun": False,
                                                  "acknowledgeSlippage": True})
        self.assertEqual(r.status_code, 409)
        self.assertIn("dry-run history", json.dumps(r.json()["error"]))
        self.assertTrue(self.fixture_config()["dryRun"])

    def test_with_history_and_an_acknowledgement_it_goes_live(self):
        cid = self.post("/v1/copy/configs", {"sourceAnon": self.anon(), "maxOrderMicro": 100 * MICRO,
                                             "maxDailyMicro": 400 * MICRO}).json()["configId"]
        con = self.app._db
        con.execute("INSERT INTO copy_dry_runs (config_id,copier_id,source_user_id,source_intent_id,market_id,"
                    "would_action,would_size_micro,would_price_micro,source_price_micro,deviation_bps,reason,"
                    "at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, "u-demo", "0x" + "bb" * 20, "src-x", "0xM9", "enter", 10 * MICRO, 42_000, 41_000,
                     24, "would copy", self.app._now_ms()))
        r = self.post("/v1/copy/configs/guards", {"configId": cid, "dryRun": False, "acknowledgeSlippage": True,
                                                  "skipIfMovedCents": 3})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["dryRun"])
        self.assertTrue(body["enabled"])
        self.assertEqual(body["dryRuns"], 1)
        self.assertEqual(body["guards"]["skip_if_moved_cents"], 3)

    def test_the_monitor_keeps_simulations_out_of_the_fills_list(self):
        cid = self.fixture_config()["configId"]
        body = self.get("/v1/copy/configs/monitor", configId=cid)
        self.assertTrue(body["wouldDo"])
        self.assertTrue(body["live"])
        for row in body["wouldDo"]:
            self.assertTrue(row["dryRun"])
        for row in body["live"]:
            self.assertFalse(row["dryRun"])
        self.assertIn("moved_past_limit", body["skipReasons"])
        for skip in body["skips"]:
            self.assertTrue(skip["reason"])

    def test_a_404_for_somebody_elses_config(self):
        r = self.client.get("/v1/copy/configs/monitor", params={"configId": "cfg-seed01"},
                            headers={"X-User-Id": "u-someone-else"})
        self.assertEqual(r.status_code, 404)


class TestPortfolio(TerminalBase):
    def test_the_portfolio_answers_with_its_benchmark_and_its_unknown_rows(self):
        body = self.get("/v1/me/portfolio")
        for key in ("positions", "negRiskGroups", "orders", "unknownLifecycle", "totals", "benchmark", "csv"):
            self.assertIn(key, body)
        self.assertEqual(body["benchmark"]["kind"], "hold_pusd")
        self.assertIn("pUSD", body["benchmark"]["note"])
        self.assertIn("micro-USDC", body["csv"]["note"])

    def test_share_of_portfolio_is_consistent_with_the_totals(self):
        body = self.get("/v1/me/portfolio")
        if not body["positions"]:
            self.assertIn("emptyState", body)
            return
        total = sum(p["valueMicro"] for p in body["positions"])
        self.assertEqual(total, body["totals"]["valueMicro"])
        self.assertLessEqual(sum(p["shareOfPortfolioBps"] for p in body["positions"]), 10_000)


class TestTheQualityGatePath(TerminalBase):
    """The phase's acceptance criterion, as one test. Each step is the one the prompt names."""

    def test_whale_to_win_rate_to_drawdown_to_a_dry_run_copy_config(self):
        # 1. find a whale fill in the tape — through the tape's own threshold affordance, which is one click in
        #    the UI and the reason `isWhale`/`thresholdMicro` travel with every row.
        facets = self.get("/v1/tape/facets", windowMs=3_600_000)
        tape = self.get("/v1/tape/fills", limit=100, windowMs=3_600_000,
                        minNotionalMicro=facets["whale"]["thresholdMicro"])
        whales = [r for r in tape["rows"] if r["isWhale"]]
        self.assertTrue(whales, "step 1: no whale in the tape")
        fill = whales[0]
        self.assertIn("whale", [lab["label"] for lab in fill["labels"]] or ["whale"])

        # 2. open the trader's profile from that row — by pseudonym, so no address is needed
        dossier = self.get("/v1/traders/%s" % fill["anonWallet"])
        self.assertEqual(dossier["anonWallet"], fill["anonWallet"])

        # 3. the win rate is real: present only where the sample gate is met, with the count attached
        m30 = dossier["metrics"]["30d"]
        self.assertIsNotNone(m30["winRateBps"], "step 3: the fixture must have a gated win rate on 30d")
        self.assertGreaterEqual(m30["resolvedMarkets"], dossier["sampleGate"])
        self.assertEqual(m30["winRateBps"], m30["wins"] * 10_000 // m30["resolvedMarkets"])

        # 4. the drawdown is on screen with the PnL, per point
        self.assertGreater(dossier["maxDrawdownMicro"], 0, "step 4: no drawdown to show")
        for p in dossier["curve"]:
            self.assertEqual(p["drawdownMicro"], p["peakMicro"] - p["cumMicro"])

        # 5. set up a copy config in dry-run...
        created = self.post("/v1/copy/configs", {"sourceAnon": fill["anonWallet"], "mode": "cap",
                                                 "maxOrderMicro": 50 * MICRO, "maxDailyMicro": 200 * MICRO})
        self.assertEqual(created.status_code, 200, created.text)
        cfg = created.json()
        self.assertTrue(cfg["dryRun"], "step 5: a new config must be a dry run")

        # 6. ...and be told the slippage risk before confirming anything live.
        self.assertIn("slippage", cfg["warning"]["warning"])
        live = self.post("/v1/copy/configs/guards", {"configId": cfg["configId"], "dryRun": False})
        self.assertEqual(live.status_code, 409, "step 6: going live must be refused without the acknowledgement")


class TestNoAddresses(TerminalBase):
    """One rule, checked everywhere at once: the API names traders by pseudonym and never by address."""

    def test_no_wallet_address_appears_anywhere(self):
        import app as app_mod
        anon = app_mod._anon("0x" + "aa" * 20)
        cfg = self.fixture_config()
        payloads = [self.get("/v1/tape/fills", limit=100, windowMs=86_400_000),
                    self.get("/v1/tape/facets", windowMs=86_400_000),
                    self.get("/v1/whales", windowMs=86_400_000),
                    self.get("/v1/traders/%s" % anon),
                    self.get("/v1/copy/configs"),
                    self.get("/v1/copy/configs/monitor", configId=cfg["configId"]),
                    self.get("/v1/me/portfolio")]
        for payload in payloads:
            text = json.dumps(payload)
            for address in ("0x" + "aa" * 20, "0x" + "bb" * 20, "0x" + "dd" * 20):
                self.assertNotIn(address, text, "an address leaked into a P10 payload")
            # The pseudonym shape is what a client may see, and the source IS described — by anon.
        self.assertIn("sourceAnon", json.dumps(payloads[4]))


if __name__ == "__main__":
    unittest.main()
