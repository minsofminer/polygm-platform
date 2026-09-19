"""P10's arithmetic, pinned. Stdlib only, no database, no clock.

Each test here corresponds to a sentence the phase's prompt wrote as a rule:
  * "relative whale threshold with an absolute fallback" → `whale_threshold_micro`
  * "every win rate has a sample-size gate" → `win_rate`
  * "drawdown appears wherever PnL appears" → `drawdown_overlay`
  * "no float arithmetic in the money path" → the file's own imports (checked by the gate, not here)
"""
from __future__ import annotations

import unittest

from polygm_core.terminal import metrics as m


class TestMedianAndPercentile(unittest.TestCase):
    def test_median_odd_and_even_round_down(self):
        self.assertEqual(m.median_micro([5, 1, 3]), 3)
        # (2 + 3) / 2 = 2.5 → 2: a threshold that rounds up occasionally misses the fill it was built to catch.
        self.assertEqual(m.median_micro([3, 2]), 2)
        self.assertEqual(m.median_micro([]), 0)

    def test_median_refuses_a_float(self):
        with self.assertRaises(TypeError):
            m.median_micro([1.5, 2])

    def test_percentile_is_nearest_rank_and_not_interpolated(self):
        vals = [i * 10 for i in range(1, 101)]                  # 10..1000
        self.assertEqual(m.percentile_micro(vals, 95, 100), 950)
        self.assertEqual(m.percentile_micro(vals, 50, 100), 500)
        # p99.5 of 40 values with a rank of 40 is the largest value: no invention between two real fills.
        self.assertEqual(m.percentile_micro([100] * 40, 995, 1000), 100)
        self.assertEqual(m.percentile_micro([], 95, 100), 0)


class TestWhaleRule(unittest.TestCase):
    def test_floor_wins_when_the_percentile_is_lower(self):
        # The measured distribution from P05 D5: median $5, p95 $133. A busy market's p99.5 can still sit under
        # the $500 floor, and when it does the floor is the answer.
        t = m.whale_threshold_micro(p995=128 * m.MICRO, median=5 * m.MICRO, fills=640)
        self.assertEqual(t["thresholdMicro"], 500 * m.MICRO)
        self.assertEqual(t["reason"], "absolute_floor")
        self.assertIn("$500.00", t["rule"])

    def test_relative_wins_when_the_percentile_is_higher(self):
        t = m.whale_threshold_micro(p995=2_400 * m.MICRO, median=40 * m.MICRO, fills=900)
        self.assertEqual(t["thresholdMicro"], 2_400 * m.MICRO)
        self.assertEqual(t["reason"], "relative")

    def test_below_the_sample_the_absolute_floor_is_the_fallback(self):
        t = m.whale_threshold_micro(p995=9_000 * m.MICRO, median=6 * m.MICRO, fills=12)
        self.assertEqual(t["thresholdMicro"], 500 * m.MICRO)
        self.assertEqual(t["reason"], "absolute_fallback")
        self.assertFalse(t["sampleOk"])
        self.assertIn("12 fills", t["rule"])

    def test_multiple_mode_is_the_same_rule_with_a_knob(self):
        t = m.whale_threshold_micro(median=5 * m.MICRO, fills=200, mode="multiple", multiple=100)
        self.assertEqual(t["thresholdMicro"], 500 * m.MICRO)
        self.assertEqual(t["reason"], "absolute_floor")
        t2 = m.whale_threshold_micro(median=20 * m.MICRO, fills=200, mode="multiple", multiple=100)
        self.assertEqual(t2["thresholdMicro"], 2_000 * m.MICRO)
        self.assertEqual(t2["reason"], "relative")

    def test_severity_is_a_ratio_to_the_threshold(self):
        thr = 500 * m.MICRO
        self.assertEqual(m.whale_severity(400 * m.MICRO, thr)["severity"], "info")
        # At exactly the threshold the ratio is 1× and the STATED rule says urgent at 4×, notice at 1.5× — so a
        # fill sitting on the threshold is `info`: it is a whale, and it is not a big one. The rule is the spec,
        # which is why the boundaries are pinned here rather than left to whichever component renders the badge.
        self.assertEqual(m.whale_severity(500 * m.MICRO, thr)["severity"], "info")
        self.assertEqual(m.whale_severity(750 * m.MICRO, thr)["severity"], "notice")
        self.assertEqual(m.whale_severity(1_999 * m.MICRO, thr)["severity"], "notice")
        self.assertEqual(m.whale_severity(2_000 * m.MICRO, thr)["severity"], "urgent")
        self.assertEqual(m.whale_severity(2_000 * m.MICRO, 0)["severity"], "info")

    def test_market_size_buckets_have_stated_floors(self):
        self.assertEqual(m.market_size_bucket(5_000 * m.MICRO), "small")
        self.assertEqual(m.market_size_bucket(50_000 * m.MICRO), "mid")
        self.assertEqual(m.market_size_bucket(5_000_000 * m.MICRO), "large")
        self.assertEqual(m.bucket_floor_micro(5_000 * m.MICRO), m.BUCKET_FLOOR_MICRO["small"])
        self.assertEqual(m.bucket_floor_micro(5_000_000 * m.MICRO), m.BUCKET_FLOOR_MICRO["large"])


class TestWinRateGate(unittest.TestCase):
    def test_below_the_gate_is_a_refusal_with_a_reason(self):
        r = m.win_rate(3, 4)
        self.assertIsNone(r["bps"])
        self.assertTrue(r["insufficientSample"])
        self.assertIn("insufficient sample", r["reason"])
        self.assertIn("needs 20", r["reason"])

    def test_at_the_gate_it_is_a_rate(self):
        r = m.win_rate(15, 24)
        self.assertEqual(r["bps"], 6250)
        self.assertFalse(r["insufficientSample"])
        self.assertEqual(m.bps_str(r["bps"]), "62.5%")

    def test_exactly_at_the_gate_is_enough(self):
        self.assertEqual(m.win_rate(10, m.SAMPLE_GATE)["bps"], 5000)

    def test_a_zero_denominator_never_returns_a_number(self):
        self.assertIsNone(m.win_rate(0, 0)["bps"])


class TestDrawdownOverlay(unittest.TestCase):
    def test_drawdown_is_the_distance_from_the_high_water_mark(self):
        pts = m.drawdown_overlay([{"tsMs": 1, "cumMicro": 100}, {"tsMs": 2, "cumMicro": 250},
                                  {"tsMs": 3, "cumMicro": 200}, {"tsMs": 4, "cumMicro": 400}])
        self.assertEqual([p["peakMicro"] for p in pts], [100, 250, 250, 400])
        self.assertEqual([p["drawdownMicro"] for p in pts], [0, 0, 50, 0])
        self.assertEqual(m.max_drawdown_micro(pts), 50)

    def test_a_monotone_loss_streak_keeps_one_peak(self):
        pts = m.drawdown_overlay([{"tsMs": i, "cumMicro": -i * 10} for i in range(1, 6)])
        self.assertEqual([p["peakMicro"] for p in pts], [0] * 5)
        self.assertEqual(m.max_drawdown_micro(pts), 50)
        self.assertEqual(m.max_drawdown_micro([]), 0)

    def test_a_negative_only_curve_still_has_a_drawdown(self):
        self.assertEqual(m.drawdown_overlay([{"tsMs": 1, "cumMicro": -5}])[0]["drawdownMicro"], 5)


class TestHoldStats(unittest.TestCase):
    def test_open_fills_are_counted_not_averaged_in(self):
        h = m.hold_stats([{"entryMs": 0, "exitMs": 60_000}, {"entryMs": 0, "exitMs": 120_000},
                          {"entryMs": 5, "exitMs": None}])
        self.assertEqual(h["avgHoldMs"], 90_000)
        self.assertEqual(h["medianHoldMs"], 90_000)
        self.assertEqual(h["openFills"], 1)
        self.assertEqual(h["matchedPositions"], 2)

    def test_a_negative_span_is_clamped_rather_than_kept(self):
        self.assertEqual(m.hold_stats([{"entryMs": 100, "exitMs": 50}])["avgHoldMs"], 0)

    def test_no_fills_is_zero_not_an_error(self):
        self.assertEqual(m.hold_stats([]), {"avgHoldMs": 0, "medianHoldMs": 0, "matchedPositions": 0,
                                           "openFills": 0})


class TestBreakdownAndPortfolio(unittest.TestCase):
    def test_breakdown_sums_and_shares(self):
        rows = [{"category": "Politics", "notionalMicro": 100, "realisedMicro": 50},
                {"category": "Politics", "notionalMicro": 100, "realisedMicro": -10},
                {"category": "Crypto", "notionalMicro": 10, "realisedMicro": 40}]
        out = m.category_breakdown(rows)
        self.assertEqual(out[0]["category"], "Crypto")
        self.assertEqual(out[0]["realisedMicro"], 40)
        self.assertEqual(out[1]["category"], "Politics")
        self.assertEqual(out[1]["realisedMicro"], 40)
        self.assertEqual(sum(d["shareBps"] for d in out), 10_000)

    def test_breakdown_of_nothing_is_empty_not_a_zero_row(self):
        self.assertEqual(m.category_breakdown([]), [])

    def test_uncategorised_rows_are_named_rather_than_dropped(self):
        out = m.category_breakdown([{"category": "", "notionalMicro": 1, "realisedMicro": 1}])
        self.assertEqual(out[0]["category"], "Uncategorised")

    def test_portfolio_row_marks_a_position(self):
        r = m.portfolio_row(size_micro=1_000 * m.MICRO, avg_entry_micro=400_000, mark_micro=600_000,
                            tick_micro=1_000, ends_in_ms=3_600_000, cost_basis_micro=0)
        self.assertEqual(r["costBasisMicro"], 400 * m.MICRO)
        self.assertEqual(r["valueMicro"], 600 * m.MICRO)
        self.assertEqual(r["unrealisedMicro"], 200 * m.MICRO)
        self.assertEqual(r["unrealisedBps"], 5000)              # +50.00%
        self.assertTrue(r["onTick"])

    def test_off_tick_marks_say_so(self):
        r = m.portfolio_row(size_micro=m.MICRO, avg_entry_micro=500_000, mark_micro=500_500, tick_micro=1_000,
                            ends_in_ms=0, cost_basis_micro=500_000)
        self.assertFalse(r["onTick"])

    def test_negrisk_max_payout_is_the_best_leg_not_the_sum(self):
        legs = [{"sizeMicro": 100 * m.MICRO, "valueMicro": 40 * m.MICRO, "costBasisMicro": 50 * m.MICRO},
                {"sizeMicro": 200 * m.MICRO, "valueMicro": 30 * m.MICRO, "costBasisMicro": 50 * m.MICRO}]
        g = m.neg_risk_group(legs, event_id="0xEV9", event_title="Nominee")
        self.assertEqual(g["legs"], 2)
        self.assertEqual(g["valueMicro"], 70 * m.MICRO)
        self.assertEqual(g["unrealisedMicro"], -30 * m.MICRO)
        self.assertEqual(g["maxPayoutMicro"], 200 * m.MICRO)          # the best single leg
        self.assertIn("not the sum", g["note"])


class TestSlippageWarning(unittest.TestCase):
    def test_the_warning_quotes_what_was_measured(self):
        w = m.copy_slippage_warning(deviations_bps=[5, 40, 90, 12], copied=4, skipped=3)
        self.assertEqual(w["medianSlippageBps"], m.median_micro([5, 40, 90, 12]))
        self.assertEqual(w["worstSlippageBps"], 90)
        self.assertEqual(w["default"], "skip_instead_of_chase")
        self.assertIn("skipped 3", w["warning"])
        self.assertIn("skip", w["warning"].lower())

    def test_no_history_is_not_a_zero_risk_claim(self):
        w = m.copy_slippage_warning(deviations_bps=[], copied=0, skipped=0)
        self.assertEqual(w["samples"], 0)
        self.assertIn("copied this source 0 times", w["warning"])

    def test_it_reports_adverse_slippage_without_softening_it(self):
        w = m.copy_slippage_warning(deviations_bps=[120, 300], copied=2, skipped=0)
        self.assertEqual(w["p90SlippageBps"], 300)
        self.assertIn("300", w["warning"])


class TestFormatting(unittest.TestCase):
    def test_micro_str_is_integer_arithmetic(self):
        self.assertEqual(m._micro_str(500 * m.MICRO), "$500.00")
        self.assertEqual(m._micro_str(1), "$0.00")
        self.assertEqual(m._micro_str(-1_234_567), "-$1.23")

    def test_bps_str_has_one_decimal(self):
        self.assertEqual(m.bps_str(6250), "62.5%")
        self.assertEqual(m.bps_str(0), "0.0%")
        self.assertEqual(m.bps_str(-250), "-2.5%")


if __name__ == "__main__":
    unittest.main()
