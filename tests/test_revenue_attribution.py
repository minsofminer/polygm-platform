"""P06 D8: builder-code revenue — issuance, exclusions, the second measurement, and the payout cap.

The theme of every test here is that a revenue number is only as good as the refusal behind it. A fee we
expect, a fee the chain says we got, and a payout we promised a source are three different facts, and the
phase's job was to keep them in three places that cannot contaminate each other.
"""
from __future__ import annotations

import unittest

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "packages"),):
    if p not in sys.path:
        sys.path.insert(0, p)

from polygm_core.money.cents import notional_floor                                # noqa: E402
from polygm_core.revenue import attribution as ra                                  # noqa: E402
from polygm_core.venue import clob_v2 as v2                                        # noqa: E402


def code(**kw) -> ra.BuilderCode:
    base = dict(code="0xCOPY", label="copy program", kind="copy_source", fee_bps=100, source_user="u-src",
                source_share_bps=2_500, issued_ms=1_000)
    base.update(kw)
    return ra.BuilderCode(**base)


def facts(**kw) -> ra.OrderFacts:
    base = dict(intent_id="i-1", order_id="0xorder", user_id="u-demo", market_id="0xM1", token_id="t-1",
                side="BUY", builder_code="0xCOPY", notional_micro=100_000_000,
                filled_shares_micro=100 * 10**6, placed_ms=2_000, source_user="u-src")
    base.update(kw)
    return ra.OrderFacts(**base)


class TestIssuance(unittest.TestCase):
    def test_a_code_is_dated_and_unique(self):
        with self.assertRaises(ValueError):
            ra.CodeRegistry([code(issued_ms=0)])          # an undated code cannot be placed in time
        reg = ra.CodeRegistry([code()])
        with self.assertRaises(ValueError):
            reg.issue(code())                             # and never re-issued under the same name
    def test_a_rate_cannot_be_edited_after_issue(self):
        reg = ra.CodeRegistry([code()])
        with self.assertRaises(ValueError):
            reg.issue(code(fee_bps=150))
        self.assertEqual(reg.get("0xCOPY").fee_bps, 100, "the only way to change a rate is to revoke and "
                                                         "re-issue, so old orders keep the rate they ran under")

    def test_a_code_cannot_claim_more_than_the_ceiling(self):
        for bad in (201, 10_000, -1):
            with self.assertRaises(ValueError, msg=bad):
                code(fee_bps=bad)
        self.assertEqual(code(fee_bps=200, max_fee_bps=200).fee_bps, 200)

    def test_a_share_needs_a_source_and_an_internal_code_has_neither(self):
        with self.assertRaises(ValueError):
            code(source_user="", source_share_bps=500)
        with self.assertRaises(ValueError):
            code(kind="internal", source_user="u-src")
        with self.assertRaises(ValueError):
            code(kind="referral")

    def test_an_unknown_code_is_refused_loudly_in_the_ledger_and_quietly_at_the_venue(self):
        reg = ra.CodeRegistry([])
        r = reg.validate("0xNOPE", at=10)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "BUILDER_CODE_UNKNOWN")
        self.assertIn("unattributed", r["message"])

    def test_revocation_binds_at_the_revocation_time_not_before(self):
        reg = ra.CodeRegistry([code(revoked_ms=5_000)])
        self.assertTrue(reg.validate("0xCOPY", at=4_999)["ok"], "orders under the old terms still attribute")
        self.assertEqual(reg.validate("0xCOPY", at=5_000)["code"], "BUILDER_CODE_REVOKED")
        # at=4_999 the revocation has not happened yet, so a re-run of an old day still sees the code as
        # attributable; at=5_000 it does not. Both sides of the timestamp matter for a backfill.
        self.assertEqual([c.code for c in reg.attributable_codes(at=4_999)], ["0xCOPY"])
        self.assertEqual([c.code for c in reg.attributable_codes(at=5_000)], [])
    def test_a_client_that_states_a_different_rate_loses_the_argument(self):
        reg = ra.CodeRegistry([code()])
        r = reg.validate("0xCOPY", at=10, fee_bps_claimed=0)
        self.assertTrue(r["ok"], "the order still runs; only the attribution is affected")
        self.assertEqual(r["code"], "BUILDER_RATE_MISMATCH")
        self.assertEqual(r["used_bps"], 100, "the registry's rate is used, not the request's")

    def test_the_fingerprint_moves_with_the_economics_and_not_with_the_copy(self):
        self.assertEqual(code().fingerprint, code().fingerprint)
        self.assertNotEqual(code().fingerprint, code(fee_bps=90).fingerprint)
        self.assertNotEqual(code().fingerprint, code(source_share_bps=2_600).fingerprint)
        self.assertEqual(code(label="a").fingerprint, code(label="b").fingerprint,
                         "the label is decoration; a fingerprint that moved on a rename would make every "
                         "historic order look re-priced")


class TestExpectedFee(unittest.TestCase):
    def test_the_formula_is_the_venues_not_a_restatement(self):
        # estimate_fees is P04's implementation of the venue's builder-fee rule (bps on notional, rounded up).
        # If this test ever disagrees with that function, the disagreement is the bug, in one of the two.
        for notional, bps in ((100_000_000, 100), (1, 100), (999, 7), (50_000_000, 200), (12_345, 33)):
            self.assertEqual(ra.expected_builder_fee_micro(notional, bps),
                            v2.estimate_fees(size_shares_micro=notional, price_micro=10**6, fee_rate_bps=0,
                                             builder_bps=bps).builder_micro,
                            (notional, bps))

    def test_rounding_is_up_so_we_never_under_claim_our_own_entitlement(self):
        self.assertEqual(ra.expected_builder_fee_micro(100_000_001, 100), 1_000_001)
        self.assertEqual(ra.expected_builder_fee_micro(1, 1), 1)

    def test_zero_notional_is_zero_and_negative_is_a_bug(self):
        self.assertEqual(ra.expected_builder_fee_micro(0, 100), 0)
        with self.assertRaises(ValueError):
            ra.expected_builder_fee_micro(-1, 100)


class TestExclusions(unittest.TestCase):
    def test_an_unfilled_order_earns_nothing_but_still_gets_a_row(self):
        row = ra.attribution_row(facts(filled_shares_micro=0), ra.CodeRegistry([code()]).get("0xCOPY"), at=1)
        self.assertEqual(row["excluded_reason"], "unfilled")
        self.assertEqual(row["fee_micro_expected"], 0)
        self.assertEqual(row["builder_code"], "0xCOPY", "the row proves coverage: we sent the code, the venue "
                                                        "just had nothing to charge")

    def test_maker_equals_taker_is_not_trading(self):
        for a, b in (("0xAa", "0xaA"), ("0xA", "0xB")):
            row = ra.attribution_row(facts(maker_address=a, taker_address=b), None, at=1)
            self.assertEqual(ra.exclusion(facts(maker_address=a, taker_address=b)),
                             "self_cross" if a.lower() == b.lower() else "")

    def test_copying_yourself_is_excluded_whatever_the_size(self):
        self.assertEqual(ra.exclusion(facts(user_id="u-src", source_user="u-src")), "self_copy")
        self.assertEqual(ra.exclusion(facts(user_id="u-demo", counterparty_source="u-demo")), "cycle")
        self.assertEqual(ra.exclusion(facts(user_id="u-demo", source_user="u-src")), "")

    def test_an_order_with_no_valid_code_is_attributable_to_nobody(self):
        reg = ra.CodeRegistry([])
        row = ra.attribution_row(facts(), reg.get("0xCOPY"), at=1)
        self.assertEqual(row["builder_code"], "")
        self.assertEqual(row["fee_micro_expected"], 0)
        self.assertEqual(row["code_fingerprint"], "")

    def test_the_sized_notional_and_the_fee_use_the_same_integer_path(self):
        size, price = 12_345_678, 432_100
        notional = notional_floor(size, price)
        row = ra.attribution_row(facts(notional_micro=notional, filled_shares_micro=size), code(), at=1)
        self.assertEqual(row["fee_micro_expected"], ra.expected_builder_fee_micro(notional, 100))
        self.assertGreater(row["fee_micro_expected"], 0)


class TestSecondMeasurement(unittest.TestCase):
    def test_the_measurer_cannot_see_our_expectation(self):
        import inspect
        params = list(inspect.signature(ra.measure_from_chain).parameters)
        self.assertEqual(params, ["events", "code"],
                         "a parameter that lets the reconciliation job read fee_micro_expected turns the "
                         "delta into a tautology")

    def test_only_events_naming_our_code_count(self):
        m = ra.measure_from_chain([{"builder_code": "0xCOPY", "builder_fee_micro": 900_000},
                                  {"builder_code": "0xOTHER", "builder_fee_micro": 5_000_000},
                                  {"builder_code": "0xCOPY", "builder_fee_micro": 100_000}], code="0xCOPY")
        self.assertEqual(m["measured_micro"], 1_000_000)
        self.assertEqual(m["events_counted"], 2)
        self.assertTrue(m["complete"])

    def test_a_missing_value_is_a_gap_not_a_zero(self):
        m = ra.measure_from_chain([{"builder_code": "0xCOPY", "builder_fee_micro": None}], code="0xCOPY")
        self.assertEqual(m["measured_micro"], 0)
        self.assertFalse(m["complete"])
        self.assertEqual(m["events_incomplete"], 1)

    def test_a_fractional_settlement_figure_is_refused(self):
        with self.assertRaises(ValueError):
            ra.measure_from_chain([{"builder_code": "0xCOPY", "builder_fee_micro": 100.5}], code="0xCOPY")
        with self.assertRaises(ValueError):
            ra.measure_from_chain([{"builder_code": "0xCOPY", "builder_fee_micro": -1}], code="0xCOPY")

    def test_the_delta_is_compared_in_both_units_because_a_cent_is_not_one_size(self):
        small = ra.compare({"fee_micro_expected": 4_000}, {"measured_micro": 3_990, "events_incomplete": 0,
                                                           "events_counted": 1})
        self.assertTrue(small["reconciled"], "a $0.04 expectation off by a tenth of a cent is rounding")
        big = ra.compare({"fee_micro_expected": 400_000_000}, {"measured_micro": 399_990_000,
                                                               "events_incomplete": 0, "events_counted": 1})
        self.assertTrue(big["reconciled"], "a cent on $400 is noise")
        real = ra.compare({"fee_micro_expected": 4_000_000}, {"measured_micro": 0, "events_incomplete": 0,
                                                              "events_counted": 0})
        self.assertFalse(real["reconciled"])
        self.assertEqual(real["dispute"], "we_expected_more")
        over = ra.compare({"fee_micro_expected": 1_000}, {"measured_micro": 9_000, "events_incomplete": 0,
                                                          "events_counted": 1})
        self.assertEqual(over["dispute"], "venue_paid_more", "we do not quietly keep a windfall; it is a "
                                                             "reconciliation problem with someone else's "
                                                             "order in it")

    def test_an_unmeasured_day_is_not_a_zero_delta(self):
        r = ra.rollup([{"id": 1, "builder_code": "0xCOPY", "fee_micro_expected": 1_000_000,
                       "notional_micro": 100_000_000, "excluded_reason": ""}], [], day="2026-09-18")
        self.assertEqual(r.delta_micro, 1_000_000)
        self.assertFalse(r.reconciled)
        d = r.as_dict()
        self.assertEqual(d["unmeasured_orders"], 1)
        self.assertEqual(d["observed_micro"], 0)


class TestRollupAndPayout(unittest.TestCase):
    def setUp(self):
        self.reg = ra.CodeRegistry([code()])
        self.rows = [{"id": 1, "builder_code": "0xCOPY", "fee_micro_expected": 1_000_000,
                     "notional_micro": 100_000_000, "excluded_reason": ""},
                    {"id": 2, "builder_code": "0xCOPY", "fee_micro_expected": 2_000_000,
                     "notional_micro": 200_000_000, "excluded_reason": ""},
                    {"id": 3, "builder_code": "0xCOPY", "fee_micro_expected": 0,
                     "notional_micro": 50_000_000, "excluded_reason": "unfilled"}]
        self.meas = [{"attribution_id": 1, "chain_measured_micro": 990_000},
                     {"attribution_id": 2, "chain_measured_micro": 2_000_000}]

    def test_the_day_totals_carry_the_exclusions_instead_of_hiding_them(self):
        r = ra.rollup(self.rows, self.meas, day="2026-09-18", registry=self.reg).as_dict()
        self.assertEqual((r["orders"], r["fills"]), (3, 2))
        self.assertEqual(r["excluded"], {"unfilled": 1})
        self.assertEqual(r["volume_micro"], 350_000_000, "volume is what was attempted; fee base is what matched")
        self.assertEqual(r["expected_micro"], 3_000_000)
        self.assertEqual(r["observed_micro"], 2_990_000)
        self.assertEqual(r["delta_micro"], 10_000)
        self.assertEqual(r["disputes"], ["0xCOPY:we_expected_more(10000)"])
        self.assertFalse(r["reconciled"])
        self.assertEqual(r["excluded_order_share_bps"], 3_333)

    def test_a_measurement_for_an_order_we_never_expected_is_unusable(self):
        # No row has id 9, so a chain event that cannot be attached contributes nothing and cannot create
        # revenue out of nowhere.
        r = ra.rollup(self.rows, self.meas + [{"attribution_id": 9, "chain_measured_micro": 9_000_000}],
                     day="2026-09-18", registry=self.reg)
        self.assertEqual(r.observed_micro, 2_990_000)
        self.assertEqual(r.measured_rows, 2)

    def test_the_payout_is_a_share_of_what_the_venue_paid_not_of_what_we_asked_for(self):
        r = ra.rollup(self.rows, self.meas, day="2026-09-18", registry=self.reg).as_dict()
        self.assertEqual(r["payout_micro"], (2_990_000 * 2_500) // 10_000)
        self.assertEqual(r["platform_micro"], 2_990_000 - r["payout_micro"])
        self.assertLess(r["payout_micro"], 2_990_000)

    def test_the_payout_is_not_multiplied_by_the_number_of_orders_on_a_code(self):
        # This was a real bug: the observed fee is a per-CODE total, and a loop over rows that re-derives the
        # payout from that total pays the source once per order for the same collected money.
        rows = [{"id": i, "builder_code": "0xCOPY", "fee_micro_expected": 1_000_000,
                "notional_micro": 100_000_000, "excluded_reason": ""} for i in (1, 2, 3)]
        meas = [{"attribution_id": 1, "chain_measured_micro": 900_000},
                {"attribution_id": 2, "chain_measured_micro": 800_000},
                {"attribution_id": 3, "chain_measured_micro": 300_000}]
        d = ra.rollup(rows, meas, day="d", registry=self.reg).as_dict()
        self.assertEqual(d["observed_micro"], 2_000_000)
        self.assertEqual(d["payout_micro"], 500_000, "25% of what was collected, once")
        self.assertEqual(d["platform_micro"], 1_500_000)
    def test_a_day_where_everything_matches_is_reconciled(self):
        meas = [{"attribution_id": 1, "chain_measured_micro": 1_000_000},
                {"attribution_id": 2, "chain_measured_micro": 2_000_000}]
        r = ra.rollup(self.rows[:2], meas, day="d", registry=self.reg).as_dict()
        self.assertTrue(r["reconciled"], r)
        self.assertEqual(r["disputes"], [])

    def test_a_payout_larger_than_the_collected_fee_is_scaled_and_says_so(self):
        split = ra.payout_split(self.rows, {"0xCOPY": {"micro": 2_990_000}}, registry=self.reg)
        capped = ra.payout_capped(split, observed_total_micro=100_000)
        self.assertTrue(capped["capped"])
        self.assertEqual(capped["payout_micro"], 100_000)
        self.assertEqual(sum(capped["by_source"].values()), capped["payout_micro"],
                         "the last source absorbs the rounding so the parts always add up to the whole")
        self.assertIn("has not paid", capped["note"])

    def test_no_collected_fee_no_payout(self):
        split = ra.payout_split(self.rows, {}, registry=self.reg)
        self.assertEqual(split["payout_micro"], 0)
        self.assertEqual(split["by_source"], {})
        self.assertEqual(split["expected_micro"], 3_000_000)
        self.assertEqual(split["unpaid_expectation_micro"], 3_000_000,
                         "the shortfall is a reported number, not a smaller revenue line")

    def test_a_code_with_no_source_keeps_the_whole_fee(self):
        reg = ra.CodeRegistry([code(source_user="", source_share_bps=0)])
        split = ra.payout_split([dict(self.rows[0], excluded_reason="")], {"0xCOPY": {"micro": 1_000_000}},
                               registry=reg)
        self.assertEqual(split["by_source"], {})
        self.assertEqual(split["platform_micro"], 1_000_000)

    def test_a_source_cannot_be_paid_more_than_the_fee_even_at_100_percent(self):
        reg = ra.CodeRegistry([code(source_share_bps=10_000)])
        split = ra.payout_split([self.rows[0]], {"0xCOPY": {"micro": 900_000}}, registry=reg)
        self.assertEqual(split["payout_micro"], 900_000)
        self.assertEqual(split["platform_micro"], 0)


def self_rows() -> list[dict]:
    return [{"id": 1, "builder_code": "0xCOPY", "fee_micro_expected": 1_000_000,
            "notional_micro": 100_000_000, "excluded_reason": ""},
            {"id": 2, "builder_code": "0xCOPY", "fee_micro_expected": 2_000_000,
             "notional_micro": 200_000_000, "excluded_reason": ""}]


def self_meas() -> list[dict]:
    return [{"attribution_id": 1, "chain_measured_micro": 990_000},
            {"attribution_id": 2, "chain_measured_micro": 2_000_000}]


class TestStoreWiring(unittest.TestCase):
    """The two tables exist to be read back, and the reads are where a bug hides if every test is pure."""

    def setUp(self) -> None:
        import importlib.util
        sys.path[:0] = [os.path.join(ROOT, "services", "executor"), os.path.join(ROOT, "tests"),
                        os.path.join(ROOT, "services", "api")]
        spec = importlib.util.spec_from_file_location("pgm_store_p06_rev",
                                                     os.path.join(ROOT, "services", "executor", "store.py"))
        self.store_mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.store_mod
        spec.loader.exec_module(self.store_mod)
        from conftest import apply_schema, tmp_db_path
        self.db = tmp_db_path("p06-rev-%s" % self.id())
        apply_schema(self.db)
        import seed
        seed.seed_sqlite(self.db)
        self.store = self.store_mod.Store.open(self.db)
        self.at = self.store_mod.now_ms()

    def tearDown(self) -> None:
        self.store.close()

    def test_a_measure_written_from_the_chain_is_the_second_row_of_the_same_fact(self):
        aid = self.place()
        self.store.save_measure(attribution_id=aid, order_id="0xorder", chain_micro=990_000,
                               notional_micro=100_000_000, expected_micro=1_000_000, source="chain_log",
                               at=self.at)
        r = self.store.attribution_rows(at=self.at + 1)[0]
        self.assertEqual(int(r["expected_micro"]), 1_000_000)
        self.assertEqual(int(r["chain_measured_micro"]), 990_000)
        self.assertEqual(int(r["delta_micro"]), 10_000, "the delta is stored per row, so a day can be summed "
                                                        "without re-deriving it")
        self.assertTrue(r["measured"])
        self.assertEqual(r["code_fingerprint"], code().fingerprint)
        self.assertEqual(int(r["notional_micro"]), 100_000_000)
        # the rollup reads the same rows the store gave it, so the day total is the sum of the row deltas
        day = ra.rollup([dict(r, id=r["attribution_id"], builder_code=r["user_id"] and "0xCOPY",
                             fee_micro_expected=r["expected_micro"], excluded_reason=r["excluded_reason"] or "",
                             chain_measured_micro=r["chain_measured_micro"])],
                       [{"attribution_id": aid, "chain_measured_micro": 990_000}], day="d",
                       registry=ra.CodeRegistry([code()]))
        self.assertEqual(day.as_dict()["delta_micro"], 10_000)

    def place(self, **over) -> int:
        row = ra.attribution_row(facts(**over), ra.CodeRegistry([code()]).get("0xCOPY"), at=self.at)
        cur = self.store.conn.execute("INSERT INTO builder_attribution (intent_id,user_id,builder_code,"
                                      "fee_bps_expected,fee_micro_expected,market_id,token_id,placed_ms,"
                                      "order_id) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",
                                      (row["intent_id"], row["user_id"], row["builder_code"],
                                       row["fee_bps_expected"], row["fee_micro_expected"], row["market_id"],
                                       row["token_id"], self.at, row["order_id"]))
        aid = cur.fetchone()[0]
        self.store.save_terms(attribution_id=aid, builder_code=row["builder_code"],
                             fee_bps=row["fee_bps_expected"], notional_micro=row["notional_micro"]
                             if "notional_micro" in row else 100_000_000,
                             excluded_reason=row["excluded_reason"], code_fingerprint=row["code_fingerprint"],
                             at=self.at)
        return aid

    def test_an_unmeasured_order_is_reported_as_unmeasured_not_as_zero(self):
        aid = self.place()
        r = self.store.attribution_rows(at=self.at + 1)[0]
        self.assertFalse(r["measured"])
        self.assertIsNone(r["chain_measured_micro"])
        self.assertEqual(self.store.attribution_rows(at=self.at + 1, only_unmeasured=True)[0]["attribution_id"],
                         aid)
        self.assertEqual(self.store.attribution_rows(at=self.at + 1, only_unmeasured=True)[0]["excluded_reason"],
                         "")

    def test_the_daily_row_status_is_three_valued(self):
        rows = [{"id": 1, "builder_code": "0xCOPY", "fee_micro_expected": 1_000_000,
                "notional_micro": 100_000_000, "excluded_reason": ""}]
        unmeasured = ra.rollup(rows, [], day="d")
        self.assertEqual(unmeasured.to_daily_row()["status"], "unreconciled")
        self.assertEqual(unmeasured.engine_status, "unmeasured")
        disputed = ra.rollup(rows, [{"attribution_id": 1, "chain_measured_micro": 0}], day="d")
        self.assertEqual(disputed.to_daily_row()["status"], "investigating")
        self.assertEqual(disputed.engine_status, "disputed")
        clean = ra.rollup(rows, [{"attribution_id": 1, "chain_measured_micro": 1_000_000}], day="d")
        self.assertEqual(clean.to_daily_row()["status"], "matched")
        self.assertEqual(clean.engine_status, "reconciled")
        empty = ra.rollup([], [], day="d")
        self.assertEqual(empty.engine_status, "reconciled",
                         "a day with no attributable orders is reconciled, not pending forever")
    def test_the_daily_rollup_is_persisted_and_readable(self):
        day = ra.rollup(self_rows(), self_meas(), day="2026-09-18",
                       registry=ra.CodeRegistry([code()]))
        row = day.to_daily_row()
        self.store.save_daily(at=self.at, **row)
        self.assertEqual(row["status"], "investigating", "the schema's word for a day we disagree about")
        got = self.store.daily_rows(limit=5)[0]
        self.assertEqual(got["day"], "2026-09-18")
        self.assertEqual(int(got["expected_micro"]) - int(got["chain_micro"]), 10_000)
        self.assertEqual(got["status"], "investigating", "the schema's word for a day we disagree about")
        # re-running the job for the same day replaces the row rather than double-counting it
        self.store.save_daily(at=self.at + 1000, **row)
        self.assertEqual(len(self.store.daily_rows(limit=5)), 1)

    def test_an_ingested_chain_event_is_idempotent_on_tx_hash_and_log_index(self):
        args = dict(source="chain_log", kind="builder_fee", tx_hash="0xT", log_index=7, order_id="0xorder",
                   builder="0xCOPY", fee_micro=990_000, matched_micro=100_000_000, price_micro=500_000,
                   at=self.at)
        first = self.store.ingest_chain_event(**args)
        again = self.store.ingest_chain_event(**args)
        n = self.store.conn.execute("SELECT COUNT(*) FROM chain_events WHERE tx_hash='0xT' AND log_index=7"
                                   ).fetchone()[0]
        self.assertEqual(n, 1, (first, again))


if __name__ == "__main__":
    unittest.main(verbosity=2)
