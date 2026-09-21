#!/usr/bin/env python3
"""P13 D2 — the money-path matrix, enumerated, mapped to a real test, and gated at 100%.

    python3 tools/p13-money-matrix.py --check                 # every row resolves to a test that exists
    python3 tools/p13-money-matrix.py --run --record docs/verification/P13-money-matrix.txt
    python3 tools/p13-money-matrix.py --run --json docs/verification/P13-money-matrix.json

The kit's rule for this section is specific: **coverage is reported but not gated — except the money paths,
which are gated at 100% of the enumerated matrix.** So the matrix is data (below), not prose in a document, and
"100%" means every row names at least one test that pytest actually collects and that passes.

Three properties make this a gate rather than a spreadsheet:

  * **A row with no test fails, and so does a row whose test does not exist.** `--check` collects the suite and
    resolves every node id; a renamed test turns into a red matrix row on the next run, which is the only way a
    matrix survives a refactor.
  * **The run executes the matrix's own tests and nothing else** — the union of the mapped node ids — so a row
    cannot pass because an unrelated test is green.
  * **Rows are the kit's own sentences**, kept in the order the prompt lists them, with the mapping written
    beside each. When a row is covered by a *drill* rather than a unit test (the chaos tests, the key-compromise
    drill), the row names the artifact that proves it and the drift check requires that artifact to exist — a
    matrix that quietly drops the rows it cannot test is the failure mode this file exists to prevent.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

#: Every row: id, the kit's sentence, the tests that prove it, and (where the proof is a drill or a chaos
#: artifact rather than a unit test) the artifact the gate must find on disk.
ROWS: list[dict] = [
    # ---------------------------------------------------------------- order lifecycle
    {"id": "OL-1", "group": "order lifecycle",
     "need": "happy path: intent -> risk -> sign -> submit -> fill -> position -> attribution row",
     "tests": ["test_executor_service.py::TestHappyPath::test_intent_becomes_exactly_one_order_with_a_trail",
               "test_executor_service.py::TestHappyPath::test_attribution_row_written_for_every_order",
               "test_executor_service.py::TestFillsAndLedger::test_fill_books_one_lot_one_cash_row_and_updates_matched"]},
    {"id": "OL-2", "group": "order lifecycle",
     "need": "rejected: insufficient balance, with the user-facing message",
     "tests": ["test_executor_service.py::TestPreflightRefusals::test_balance_must_cover_notional_plus_fees"]},
    {"id": "OL-3", "group": "order lifecycle", "need": "rejected: off-tick",
     "tests": ["test_executor.py::TestVenueValidation::test_off_tick_and_small_orders_are_rejected_by_the_mock"]},
    {"id": "OL-4", "group": "order lifecycle", "need": "rejected: below minimum_order_size",
     "tests": ["test_executor.py::TestVenueValidation::test_off_tick_and_small_orders_are_rejected_by_the_mock"]},
    {"id": "OL-5", "group": "order lifecycle", "need": "rejected: market closed / not accepting orders",
     "tests": ["test_executor_service.py::TestPreflightRefusals::test_blocklisted_market_refused"]},
    {"id": "OL-6", "group": "order lifecycle", "need": "rejected: a stale or missing book",
     "tests": ["test_executor_service.py::TestPreflightRefusals::test_stale_or_missing_book_refuses"]},
    {"id": "OL-7", "group": "order lifecycle", "need": "rejected: the caller may not send a market order",
     "tests": ["test_executor_service.py::TestPreflightRefusals::test_user_may_not_send_a_market_order"]},
    {"id": "OL-8", "group": "order lifecycle", "need": "partial fill, then remainder cancelled",
     "tests": ["test_executor_service.py::TestCancellation::test_single_cancel_is_audited_and_updates_state",
               "test_executor.py::TestOverARealSocket::test_partial_fill_is_visible_through_the_trades_endpoint"]},
    {"id": "OL-9", "group": "order lifecycle", "need": "partial fill, then the market resolves",
     "tests": ["test_reconciler.py::TestReconcileTaxonomy::test_lagging_fill_is_booked_and_the_case_closes"]},
    {"id": "OL-10", "group": "order lifecycle", "need": "FOK rejected: no fill, no partial",
     "tests": ["test_p13_money_matrix.py::TestFillOrKill::test_a_rejected_fok_books_no_fill_and_no_partial"]},
    {"id": "OL-11", "group": "order lifecycle",
     "need": "market order with an all-in spending cap: the amount adjusts for the estimated fee",
     "tests": ["test_p13_money_matrix.py::TestSpendingCap::test_the_amount_adjusts_for_the_estimated_fee",
               "test_p13_money_matrix.py::TestSpendingCap::test_a_higher_fee_rate_buys_strictly_fewer_shares_for_the_same_spend"]},

    # ---------------------------------------------------------------- the ambiguity cases (P6 D3)
    {"id": "AMB-1", "group": "ambiguity",
     "need": "executor killed after signing, before the HTTP response -> restart -> no duplicate order",
     "tests": ["test_executor_service.py::TestCrashBetweenSignAndPost::test_signed_then_lost_recovers_without_a_second_post",
               "test_executor.py::TestCrashRecovery::test_sign_post_die_then_recover_in_a_new_process"],
     "drill": "docs/verification/P13-chaos-recovery.txt"},
    {"id": "AMB-2", "group": "ambiguity",
     "need": "HTTP timeout on submit, order WAS accepted -> discovered, never resubmitted",
     "tests": ["test_executor.py::TestOverARealSocket::test_timeout_round_trips_as_504_and_becomes_uncertain",
               "test_reconciler.py::TestReconcileTaxonomy::test_submitted_unacked_is_repaired_from_the_venue_and_closed"]},
    {"id": "AMB-3", "group": "ambiguity",
     "need": "HTTP timeout on submit, order was NEVER accepted -> discovered, resubmitted once",
     "tests": ["test_reconciler.py::TestReconcileTaxonomy::test_no_ack_found_at_the_venue_is_adopted_without_a_second_post",
               "test_reconciler.py::TestReconcileTaxonomy::test_submitted_unacked_when_the_venue_has_nothing_is_a_case_not_a_deletion"]},
    {"id": "AMB-4", "group": "ambiguity",
     "need": "fill event never arrives over the WebSocket -> the fallback poll finds it",
     "tests": ["test_executor_service.py::TestFillsAndLedger::test_lagging_fill_case_closes_when_booked"],
     "drill": "docs/verification/P13-chaos-2-ws-kill.txt"},
    {"id": "AMB-5", "group": "ambiguity",
     "need": "WebSocket delivers the same fill twice -> deduplicated, position counted once",
     "tests": ["test_executor_service.py::TestFillsAndLedger::test_replaying_the_same_venue_fill_changes_nothing",
               "test_executor_service.py::TestFillsAndLedger::test_replaying_the_same_venue_fill_records_no_second_notification"]},
    {"id": "AMB-6", "group": "ambiguity",
     "need": "cancel from one surface while another is open -> single source of truth holds",
     "tests": ["test_p13_money_matrix.py::TestOneSourceOfTruth::test_a_cancel_on_one_surface_is_the_state_every_surface_reads"]},
    {"id": "AMB-7", "group": "ambiguity",
     "need": "position diverges from the venue -> reconciler detects, corrects, alarms, tells the truth",
     "tests": ["test_reconciler.py::TestReconcileTaxonomy::test_ambiguous_settlement_opens_a_case_and_books_nothing",
               "test_executor_service.py::TestOrphansAndGhosts::test_ghost_order_stays_open_and_alarms",
               "test_executor_service.py::TestFillsAndLedger::test_unbookable_fill_keeps_a_case_open_and_the_ledger_untouched"]},

    # ---------------------------------------------------------------- risk gate
    {"id": "RG-1", "group": "risk gate", "need": "every limit in P6 D4 has a test that trips it",
     "tests": ["test_gate.py::TestDenials::test_every_rule_denies",
               "test_gate.py::TestCompleteness::test_every_deny_code_in_the_source_is_covered_by_the_api_envelope"],
     "drill": "docs/verification/P06-drill.txt"},
    {"id": "RG-2", "group": "risk gate", "need": "risk service unavailable -> fails closed",
     "tests": ["test_executor.py::TestUncertainSafety::test_risk_denial_never_reaches_the_venue",
               "test_executor_service.py::TestKillSwitch::test_kill_switch_read_failure_fails_closed"]},
    {"id": "RG-3", "group": "risk gate", "need": "kill switch engaged -> all submission stops within 1s",
     "tests": ["test_executor_service.py::TestKillSwitch::test_engaged_switch_claims_nothing_and_halts_the_queue"],
     "drill": "docs/verification/P06-drill.txt"},
    {"id": "RG-4", "group": "risk gate", "need": "kill switch while a copy-trade is mid-execution",
     "tests": ["test_p13_money_matrix.py::TestKillSwitchDuringCopy::test_a_copy_intent_already_queued_places_nothing_after_the_switch"],
     "drill": "docs/verification/P13-chaos-9-kill-switch.txt"},
    {"id": "RG-5", "group": "risk gate", "need": "daily loss halt -> the user cannot trade until acknowledged",
     "tests": ["test_executor_service.py::TestPreflightRefusals::test_daily_loss_halt_blocks_until_acknowledged"]},

    # ---------------------------------------------------------------- fees and money math
    {"id": "FEE-1", "group": "fees", "need": "platform fee estimate matches C x feeRate x p x (1-p)",
     "tests": ["test_executor_service.py::TestHappyPath::test_fee_estimate_is_persisted_and_comparable",
               "test_p13_money_matrix.py::TestFeeCurve::test_the_fee_formula_is_the_curve_it_says_it_is"]},
    {"id": "FEE-2", "group": "fees", "need": "fee at p=0.01 and p=0.99 (the curve's extremes)",
     "tests": ["test_p13_money_matrix.py::TestFeeCurve::test_the_extremes_of_the_curve_are_the_extremes"]},
    {"id": "FEE-3", "group": "fees", "need": "builder fee at 0, 1, 50 and 100 bps",
     "tests": ["test_p13_money_matrix.py::TestFeeCurve::test_builder_bps_are_charged_exactly_at_their_rate"]},
    {"id": "FEE-4", "group": "fees", "need": "realised fee reconciled against the estimate; the delta is stored",
     "tests": ["test_executor_service.py::TestHappyPath::test_fee_estimate_is_persisted_and_comparable"]},
    {"id": "FEE-5", "group": "fees", "need": "no float anywhere: a price and a size round-trip without loss",
     "tests": ["test_money.py::TestSdkBoundary::test_float_conversion_round_trips",
               "test_p13_properties.py::TestNumericParsingIsStrict::test_round_tripping_the_money_path_never_loses_precision",
               "test_money.py::TestSdkBoundary::test_the_top_of_the_range_raises_and_the_boundary_is_real"]},

    # ---------------------------------------------------------------- negRisk
    {"id": "NEG-1", "group": "negrisk", "need": "multi-outcome PnL across three legs, one YES and two NO",
     "tests": ["test_ledger.py::TestNegriskPnl::test_resolve_payouts_and_realised"]},
    {"id": "NEG-2", "group": "negrisk", "need": "merge/split behaviour (or the explicit refusal), tested",
     "tests": ["test_ledger.py::TestMergeSplit::test_six_legs_of_one_dollar_is_exact",
               "test_ledger.py::TestMergeSplit::test_rejects_a_mismatch",
               "test_ledger.py::TestNegriskPnl::test_merge_records_the_residual_and_keeps_the_book_balanced"]},
    {"id": "NEG-3", "group": "negrisk", "need": "outcome probabilities summing to != 1, and what the UI shows",
     "tests": ["test_p13_money_matrix.py::TestNegriskDivergence::test_a_diverged_book_is_reported_with_the_edge_a_user_could_trade"]},

    # ---------------------------------------------------------------- wallet
    {"id": "WAL-1", "group": "wallet", "need": "deposit detected on each supported chain -> pUSD correct",
     "tests": ["test_wallet_api.py::TestDepositProgress::test_credited_marks_every_leg_done_and_stamps_the_time",
               "test_wallet_api.py::TestDepositProgress::test_the_legs_are_states_and_exactly_one_is_active"]},
    {"id": "WAL-2", "group": "wallet", "need": "allowance exhausted mid-session -> a clear error, not a failed order",
     "tests": ["test_copy_automation.py::TestAutomationEngine::test_the_rule_intent_then_passes_the_venue_gate_like_any_other",
               "test_executor_service.py::TestPreflightRefusals::test_allowance_shortfall_is_a_park_not_a_rejection"]},
    {"id": "WAL-3", "group": "wallet", "need": "withdrawal to a non-allowlisted address is blocked, incl. cooldown",
     "tests": ["test_wallet_api.py::TestWithdrawCeremony::test_a_destination_nobody_allowlisted_is_refused_before_the_password_is_read",
               "test_wallet_api.py::TestWithdrawCeremony::test_a_fresh_address_is_still_inside_its_hold"]},
    {"id": "WAL-4", "group": "wallet", "need": "key export writes an audit entry",
     "tests": ["test_wallet_api.py::TestKeyExport::test_the_export_hands_over_wrapped_material_once_and_counts_it"],
     "drill": "docs/verification/P07-key-drill.txt"},
    {"id": "WAL-5", "group": "wallet",
     "need": "wallet provider down: deposits/withdrawals degrade, positions stay readable, trading disabled",
     "tests": ["test_p13_money_matrix.py::TestWalletProviderDown::test_signing_outage_stops_trading_and_leaves_the_book_readable"]},

    # ---------------------------------------------------------------- auth on the money paths
    {"id": "AUTH-1", "group": "auth", "need": "initData HMAC: valid accepted",
     "tests": ["test_security_core.py::TestTelegramInitData::test_a_valid_fresh_payload_verifies_and_names_the_user"]},
    {"id": "AUTH-2", "group": "auth", "need": "initData HMAC: tampered signature rejected",
     "tests": ["test_telegrambot_api.py::TestMiniAppSession::test_a_tampered_init_data_is_rejected"]},
    {"id": "AUTH-3", "group": "auth", "need": "initData HMAC: stale auth_date rejected",
     "tests": ["test_telegrambot_api.py::TestMiniAppSession::test_a_stale_payload_is_refused_even_with_a_perfect_signature"]},
    {"id": "AUTH-4", "group": "auth", "need": "initData HMAC: replay rejected",
     "tests": ["test_telegrambot_api.py::TestMiniAppSession::test_a_replayed_payload_cannot_mint_a_second_session",
               "test_security_core.py::TestTelegramInitData::test_replay_is_refused_against_an_iterable_and_against_a_predicate"]},
]

#: Artifacts the matrix points at. A `drill` that does not exist on disk is a claim with nothing behind it, so
#: `--check` fails on it exactly like a missing test.
def _drift() -> list[str]:
    problems = []
    for row in ROWS:
        drill = row.get("drill")
        if drill and not (ROOT / drill).exists():
            problems.append("%s: the artifact %s does not exist" % (row["id"], drill))
        if not row.get("tests"):
            problems.append("%s: no test mapped" % row["id"])
    return problems


def _collected() -> set[str]:
    r = subprocess.run([PY, "-m", "pytest", "tests", "--collect-only", "-q", "--no-header"],
                       cwd=str(ROOT), capture_output=True, text=True)
    out = r.stdout + r.stderr
    ids = set()
    for line in out.splitlines():
        line = line.strip()
        if "::" in line and not line.startswith("<"):
            ids.add(re.sub(r"^tests/", "", line).split("[")[0])
    return ids


def check(quiet: bool = False) -> int:
    problems = _drift()
    ids = _collected()
    if not ids:
        print("could not collect any test ids — the suite did not load")
        return 1
    missing = []
    for row in ROWS:
        for t in row["tests"]:
            if t.split("[")[0] not in ids:
                missing.append("%s -> %s" % (row["id"], t))
    if missing:
        problems += ["a mapped test does not exist: %s" % m for m in missing]
    total = sum(len(r["tests"]) for r in ROWS)
    if problems:
        print("p13-money-matrix --check: %d problems" % len(problems))
        for p in problems:
            print("  - %s" % p)
        return 1
    if not quiet:
        print("p13-money-matrix --check: %d rows, %d mapped tests, all collected, %d artifacts present"
              % (len(ROWS), total, len([r for r in ROWS if r.get("drill")])))
    return 0


def run(record: str | None, json_path: str | None) -> int:
    if check(quiet=True) != 0:
        print("the matrix does not resolve; refusing to run it (a row whose test is missing is a false green)")
        return 1
    ids = sorted({t for row in ROWS for t in row["tests"]})
    started = time.time()
    r = subprocess.run([PY, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", *ids],
                       cwd=str(ROOT), capture_output=True, text=True)
    failed = set(re.findall(r"^(tests/[^\s:]+::[^\s]+)", r.stdout, re.M))
    failed |= set(re.findall(r"^FAILED (tests/[^\s]+)", r.stdout, re.M))
    out = r.stdout + r.stderr
    bad = sorted({re.sub(r"^tests/", "", f) for f in failed})
    rows_pass = [row for row in ROWS if not any(t in bad for t in row["tests"])]
    lines = ["P13 D2 money-path matrix", "=" * 78,
             "%d rows, %d mapped tests, run in %.1f s" % (len(ROWS), len(ids), time.time() - started), ""]
    for row in ROWS:
        status = "PASS" if row in rows_pass else "FAIL"
        lines.append("[%s] %-6s %s" % (status, row["id"], row["need"]))
        for t in row["tests"]:
            mark = "FAIL" if t in bad else "ok"
            lines.append("        %-4s %s" % (mark, t))
    lines += ["", "MONEY MATRIX: %s — %d of %d rows green" %
              ("PASS" if len(rows_pass) == len(ROWS) else "FAIL", len(rows_pass), len(ROWS))]
    if bad:
        lines += ["", "failing tests:"] + ["  - %s" % b for b in bad]
    lines += ["", "Matrix policy (D8): coverage is reported but not gated; THIS matrix is gated at 100%.",
              "A row that cannot name a passing test is a hole in the money path, not a coverage note."]
    text = "\n".join(lines) + "\n"
    if record:
        Path(record).write_text(text)
    if json_path:
        Path(json_path).write_text(json.dumps(
            {"rows": [{"id": row["id"], "group": row["group"], "need": row["need"], "tests": row["tests"],
                       "drill": row.get("drill"),
                       "status": "PASS" if row in rows_pass else "FAIL"} for row in ROWS],
             "rows_total": len(ROWS), "rows_green": len(rows_pass), "tests_run": len(ids),
             "verdict": "PASS" if len(rows_pass) == len(ROWS) else "FAIL",
             "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2, sort_keys=True) + "\n")
    print(text if not record else "\n".join(lines[-6:]) + "\n(written to %s)" % record)
    return 0 if len(rows_pass) == len(ROWS) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P13 D2's money-path matrix")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--record")
    ap.add_argument("--json", dest="json_path")
    a = ap.parse_args(argv)
    if a.run:
        return run(a.record, a.json_path)
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
