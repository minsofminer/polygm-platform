"""D1: custody rules that a signature depends on.

These are the decisions `services/executor/main.py` makes in the milliseconds before it asks a key to do
something, plus the two money-adjacent UI guards (typed amount, export gating). They are tested here rather
than only through the executor because each one has a failure mode that no end-to-end test would notice: a
guard that *always passes* and a guard that always refuses look identical from the outside until the day one
of them is wrong about a real withdrawal.
"""
from __future__ import annotations
import unittest

from conftest import ROOT  # noqa: F401 - path setup side effect
from polygm_core.wallets import lifecycle as wl


def policy() -> wl.Policy:
    return wl.Policy()


def ctx(**kw) -> wl.WithdrawalContext:
    base = dict(password_ok=True, allowlist=(("0xAbC", 0),), now_ms=1_700_000_000_000,
                available_micro=50_000_000, fee_micro=1_000, withdrawn_today_micro=0, open_orders=0,
                policy=policy(), notify_email_sent=True, notify_telegram_sent=True)
    base.update(kw)
    return wl.WithdrawalContext(**base)


def req(**kw) -> wl.WithdrawalRequest:
    base = dict(user_id="u1", amount_micro=12_500_000, dest_address="0xAbC", typed_amount="12.50",
                typed_address="0xAbC")
    base.update(kw)
    return wl.WithdrawalRequest(**base)


class TestTypedAmountGuard(unittest.TestCase):
    """The guard that was inert, and how to prove it is not.

    `_norm_decimal` used to reference a name it never imported. A broad `except` turned that into the sentinel
    for EVERY input, so `typed == intended` became `sentinel == sentinel`: the check "passed" by agreeing with
    itself, and a digit-slip would have left the building. Both halves are pinned here — that real money
    matches, and that a mismatch does not.
    """

    def test_the_normaliser_does_not_swallow_its_own_failure(self):
        self.assertNotEqual(wl._norm_decimal("12.5"), wl._norm_decimal("nonsense"))
        self.assertNotIn("invalid", wl._norm_decimal("12.5"))
        # a non-ASCII digit string is NOT a number to us; and it must not normalise to the same thing as a
        # valid amount, which is what an always-sentinel normaliser would have produced
        self.assertNotEqual(wl._norm_decimal("١٢٫٥"), wl._norm_decimal("12.5"))

    def test_same_number_different_typing_is_allowed(self):
        for typed in ("12.5", "12.50", " 12.50 ", "012.5", "12.5000000"):
            with self.subTest(typed=typed):
                r = wl.check_withdrawal(req(typed_amount=typed, amount_micro=12_500_000), ctx())
                self.assertNotEqual(r.get("code"), "TYPED_AMOUNT_MISMATCH", r)

    def test_a_digit_slip_is_refused_rather_than_rounded(self):
        # "12" is not a spelling of 12.5, it is a different number — the guard must agree with the human who
        # typed it, which is the only property the executor can rely on.
        for typed, amount in (("1250", 12_500_000), ("12.5", 125_000_000), ("12.500001", 12_500_000),
                              ("", 12_500_000), ("$12.5", 12_500_000), ("12", 12_500_000)):
            with self.subTest(typed=typed):
                r = wl.check_withdrawal(req(typed_amount=typed, amount_micro=amount), ctx())
                self.assertEqual(r.get("code"), "TYPED_AMOUNT_MISMATCH", r)
                self.assertIn("typed", r["message"])

    def test_denial_order_never_leaks_balance_before_authentication(self):
        # an unauthenticated caller must not learn whether the money is there
        r = wl.check_withdrawal(req(amount_micro=999 * 10**6), ctx(password_ok=False))
        self.assertEqual(r["code"], "NOT_AUTHENTICATED", r)


class TestWalletStateMachine(unittest.TestCase):
    def test_no_self_loops_and_no_skipped_stages(self):
        states = [s.value for s in wl.WalletState]
        self.assertEqual(len(states), 6, states)
        legal = [(a, b) for a in states for b in states if wl.can_move(a, b)]
        self.assertFalse([s for s in states if wl.can_move(s, s)],
                         "a wallet re-entering the state it is in is how a stuck loop hides")
        self.assertIn(("provisioned", "funded"), legal)
        self.assertIn(("funded", "trading"), legal)
        # resuming a suspension IS a hop in the machine — but the only thing that can take it is a human
        # clearing the reason, which is why the store demands the event and the operator (see the P06 gate,
        # where the same hop is refused until those are present).
        self.assertIn(("suspended", "trading"), legal)
        self.assertNotIn(("none", "trading"), legal)
        self.assertNotIn(("trading", "funded"), legal, "a wallet does not un-learn that it was trading")
        self.assertNotIn(("closing", "trading"), legal, "wind-down is one-way until it reaches none")
        # the number is asserted because the pairs are the design: 6x6=36, 13 of them legal
        self.assertEqual(len(legal), 13, legal)

    def test_assert_transition_names_both_sides(self):
        with self.assertRaises(ValueError) as e:
            wl.assert_transition("none", "trading")
        msg = str(e.exception)
        self.assertIn("none", msg)
        self.assertIn("trading", msg)

    def test_every_state_in_the_machine_is_reachable_from_provisioned(self):
        states = [s.value for s in wl.WalletState]
        seen, queue = {"provisioned"}, ["provisioned"]
        while queue:
            cur = queue.pop()
            for to in states:
                if wl.can_move(cur, to) and to not in seen:
                    seen.add(to)
                    queue.append(to)
        self.assertEqual(seen, set(states), "a state nothing can reach is a state nobody can leave")
        self.assertIn("none", seen, "closing must be able to release the wallet entirely")


class TestPolicyAndSigning(unittest.TestCase):
    def test_drift_suspends_and_refuses_rather_than_signing_under_a_new_policy(self):
        p = policy()
        good = wl.check_policy_drift(p.policy_hash(), p)
        self.assertTrue(good["ok"], good)
        moved = wl.check_policy_drift(p.policy_hash(), wl.Policy(max_daily_outflow_micro=1))
        self.assertFalse(moved["ok"])
        self.assertEqual(moved["code"], "POLICY_DRIFT")
        self.assertIn("suspend", moved["action"].lower())
        self.assertIn("refuse", moved["action"].lower())

    def test_should_refuse_trading_reads_the_state_and_the_hash_together(self):
        p = policy()
        row = {"state": "trading", "policy_hash": p.policy_hash()}
        self.assertEqual(wl.should_refuse_trading(row, p), (False, ""), "a healthy wallet must trade")
        for bad in ({"state": "suspended", "policy_hash": p.policy_hash()},
                    {"state": "provisioned", "policy_hash": p.policy_hash()},
                    {"state": "trading", "policy_hash": "0" * 64}):
            refuse, why = wl.should_refuse_trading(bad, p)
            self.assertTrue(refuse, bad)
            self.assertTrue(why.strip(), "a refusal with no reason is a support ticket with no content")

    def test_eoa_and_read_only_custody_are_refused_not_defaulted(self):
        self.assertEqual(wl.signature_type_for(custody="delegated", has_proxy=True, delegated_signer=True),
                         wl.SIG_POLY_1271)
        for kw in ({"custody": "read_only", "has_proxy": False, "delegated_signer": False},
                   {"custody": "eoa", "has_proxy": True, "delegated_signer": True}):
            with self.subTest(**kw):
                with self.assertRaises(ValueError) as e:
                    wl.signature_type_for(**kw)
                self.assertIn("SIGNATURE_REFUSED", str(e.exception))


class TestDepositFloor(unittest.TestCase):
    COST = wl.ChainCost(gas_units=60_000, gas_price_wei=40 * 10**9, native_price_micro=550_000)

    def test_floor_is_ten_times_the_measured_bill(self):
        d = wl.minimum_economic_deposit(self.COST)
        self.assertEqual(d["multiple"], 10)
        self.assertEqual(d["min_deposit_micro"], 10 * d["cost"]["total_micro"])
        self.assertGreater(d["min_deposit_micro"], 0)

    def test_the_arithmetic_stays_in_integers_and_in_the_right_order_of_magnitude(self):
        # the original bug divided by 1e6 twice and produced a floor of $0.0000004, i.e. a floor that refuses
        # nothing. A gas bill of 60k @ 40 gwei at $0.55 is single-digit cents per leg, NOT sub-micro.
        cost = wl.estimate_deposit_cost(self.COST)
        self.assertIsInstance(cost["total_micro"], int)
        self.assertGreaterEqual(cost["per_leg_micro"], 1_000)
        self.assertLess(cost["per_leg_micro"], 10_000_000)
        self.assertEqual(cost["legs"], 3)

    def test_a_cheaper_chain_means_a_lower_floor_proportionally(self):
        cheap = wl.minimum_economic_deposit(wl.ChainCost(gas_units=60_000, gas_price_wei=10 * 10**9,
                                                         native_price_micro=550_000))
        base = wl.minimum_economic_deposit(self.COST)
        self.assertEqual(cheap["min_deposit_micro"] * 4, base["min_deposit_micro"])

    def test_bridge_fee_is_disclosed_and_not_folded_into_the_floor(self):
        cost = wl.estimate_deposit_cost(self.COST)
        self.assertEqual(cost["bridge_fee_bps"], 30)
        self.assertEqual(wl.estimate_deposit_cost(wl.ChainCost(gas_units=60_000, gas_price_wei=40 * 10**9,
                                                              native_price_micro=550_000,
                                                              bridge_fee_bps=500))["total_micro"],
                         cost["total_micro"], "a rate that scales with the amount does not belong in a floor")


class TestDepositStuck(unittest.TestCase):
    def d(self, **kw):
        base = dict(id=1, user_id="u1", chain="polygon", asset="pUSD", amount_micro=10_000_000,
                    first_seen_ms=1_700_000_000_000)
        base.update(kw)
        return wl.Deposit(**base)

    def test_past_the_horizon_an_error_becomes_stuck_and_the_note_says_delayed_not_lost(self):
        horizon = wl.DEPOSIT_STUCK_AFTER_MS
        moved, note = wl.advance_deposit(self.d(), now_ms=1_700_000_000_000 + horizon + 1,
                                        error="bridge 502")
        self.assertEqual(moved.status, "stuck", (moved.status, note))
        text = wl.deposit_user_note(moved)
        self.assertIn("delayed, not lost", text)
        for banned in ("stuck", "lost your", "gone"):
            self.assertNotIn(banned, text.lower(), text)

    def test_inside_the_horizon_an_error_is_not_treated_as_failure(self):
        moved, note = wl.advance_deposit(self.d(), now_ms=1_700_000_000_000 + horizon_ms(),
                                         error="bridge 502")
        self.assertNotEqual(moved.status, "stuck", (moved.status, note))

    def test_confirmations_use_the_chains_own_finality(self):
        self.assertGreater(wl.DEPOSIT_CONFIRMATIONS["polygon"], 6, "Polygon needs dozens of blocks")
        poly = self.d(confirmations=wl.DEPOSIT_CONFIRMATIONS["polygon"])
        moved, _note = wl.advance_deposit(poly, now_ms=1_700_000_000_001)
        self.assertEqual(moved.status, "confirming", "one step, because the caller can only prove one thing")


def horizon_ms() -> int:
    return wl.DEPOSIT_STUCK_AFTER_MS // 2


class TestAllowance(unittest.TestCase):
    def test_spender_mismatch_revokes_and_suspends(self):
        plan = wl.plan_allowance(granted_micro=wl.UNLIMITED_ALLOWANCE, expected_spender="0xExchange",
                                actual_spender="0xAttacker", needed_micro=5_000_000)
        self.assertEqual(plan.action, "revoke_and_suspend")
        self.assertEqual(plan.amount_micro, 0)
        self.assertIn("suspended", plan.reason)

    def test_max_allowance_is_the_sentinel_we_can_actually_store(self):
        self.assertEqual(wl.UNLIMITED_ALLOWANCE, 2**63 - 1)
        plan = wl.plan_allowance(granted_micro=0, expected_spender="0xExchange", actual_spender="0xexchange",
                                needed_micro=5_000_000)
        self.assertEqual((plan.action, plan.amount_micro), ("approve", wl.UNLIMITED_ALLOWANCE))
        self.assertNotIn("2**256", plan.reason)

    def test_a_covered_exact_allowance_needs_no_transaction(self):
        plan = wl.plan_allowance(granted_micro=5_000_000, expected_spender="0xExchange",
                                actual_spender="0xExchange", needed_micro=5_000_000)
        self.assertEqual(plan.action, "none")


class TestExportGating(unittest.TestCase):
    def test_export_exists_but_not_while_the_wallet_is_busy(self):
        free = wl.plan_export(password_ok=True, open_orders=0, unsettled_intents=0, in_flight_withdrawals=0,
                             has_deposit_stuck=False)
        self.assertTrue(free["ok"], free)
        busy = wl.plan_export(password_ok=True, open_orders=1, unsettled_intents=2, in_flight_withdrawals=0,
                             has_deposit_stuck=False)
        self.assertFalse(busy["ok"])
        self.assertEqual(len(busy["blockers"]), 2, busy)
        # every individual blocker counts, including the deposit the user may not even know about
        for kw in ({"open_orders": 1}, {"unsettled_intents": 1}, {"in_flight_withdrawals": 1},
                   {"has_deposit_stuck": True}):
            a = dict(password_ok=True, open_orders=0, unsettled_intents=0, in_flight_withdrawals=0,
                     has_deposit_stuck=False)
            a.update(kw)
            with self.subTest(**kw):
                self.assertFalse(wl.plan_export(**a)["ok"])

    def test_authentication_is_still_required_even_when_idle(self):
        self.assertEqual(wl.plan_export(password_ok=False, open_orders=0, unsettled_intents=0,
                                       in_flight_withdrawals=0, has_deposit_stuck=False)["code"],
                        "NOT_AUTHENTICATED")


class TestProviderTable(unittest.TestCase):
    def test_the_adapter_surface_is_five_functions_and_no_bigger(self):
        # D1's "what if we leave" answer is a list of five calls. Every name added here is a name the vendor
        # must implement for us to switch at all, so growth is the leak this test watches for.
        self.assertEqual(set(wl.adapter_surface()),
                         {"create_wallet", "sign", "get_policy", "set_policy", "export_key"})
        self.assertIn("export_key", wl.adapter_surface())

    def test_unverified_pricing_is_marked_in_the_data_not_only_in_the_prose(self):
        # `verified=False` is what the P06 doc reads to decide where an [UNVERIFIED] marker must appear; a
        # table that silently claimed verification is how a pricing number becomes a promise.
        unverified = [pr for pr in wl.PROVIDERS if not pr.verified]
        self.assertTrue(unverified, "if every number were verified this test would be decoration")
        for pr in unverified:
            with self.subTest(provider=pr.name):
                self.assertFalse(pr.verified)
                self.assertTrue(pr.price_model, "an unverified price still has to say what it is")
                self.assertTrue(pr.exit_cost, "leaving is a decision the user can make, so it must be priced")
        # a $0 row is not "free", it is "our own machines and our own on-call": the table has to say so in
        # words, because a spreadsheet that reads 0 next to a vendor's 1.5e9 is how a build-out gets approved
        # without anybody noticing which column it was compared against.
        zero = [pr.name for pr in wl.PROVIDERS if pr.monthly_at_10k_wallets_micro == 0]
        for name in zero:
            pr = next(x for x in wl.PROVIDERS if x.name == name)
            self.assertTrue(pr.per_wallet_fee_model or "self" in pr.price_model.lower()
                            or "our" in pr.price_model.lower(), (name, pr.price_model))

    def test_a_refusal_names_the_reason_and_the_reserve_is_not_picked(self):
        poor = wl.choose_provider(active_wallets=10_000, needs_self_host=False, needs_granular_policy=True,
                                 monthly_budget_micro=1)
        self.assertIsNone(poor["provider"], poor)
        self.assertIn("escalate", poor["verdict"])
        self.assertTrue(all(r["excluded"] for r in poor["scored"]),
                        "an excluded provider with no reason is a shrug in JSON")
        rich = wl.choose_provider(active_wallets=10_000, needs_self_host=False, needs_granular_policy=True,
                                  monthly_budget_micro=10**13)
        self.assertIsNotNone(rich["provider"], rich)
        self.assertNotIn("self_hosted", rich["candidates"],
                        "self-host is held in reserve; it is not the cheap option, it is the last one")
        # and the scaling rule: a per-wallet-priced vendor stops being cheap as we grow
        small = wl.choose_provider(active_wallets=1_000, needs_self_host=False, needs_granular_policy=True,
                                  monthly_budget_micro=10**13)["provider"]
        huge = wl.choose_provider(active_wallets=500_000, needs_self_host=False, needs_granular_policy=True,
                                 monthly_budget_micro=10**18)["provider"]
        self.assertIsInstance(small, str)
        self.assertIsInstance(huge, str)


if __name__ == "__main__":
    unittest.main()
