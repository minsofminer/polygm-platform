#!/usr/bin/env python3
"""Prove the P06 gate can fail, by breaking each money rule on a copy of the tree and requiring the gate to say so.

A gate that prints "31/31" may be printing it because nothing can fail. Each mutant below is either a bug this
phase actually had or the exact inverse of a rule P06 depends on, applied to a scratch copy; then
`tools/p06-gate-check.py --only <the check that should notice>` runs there. A mutant the gate does not catch is
reported as `SURVIVED` and makes this tool exit non-zero — it means the rule is prose.

    python3 tools/p06-mutation-test.py                       # all mutants (~2-3 min)
    python3 tools/p06-mutation-test.py --only batch-cap-16,failed-lookup-is-absence
    python3 tools/p06-mutation-test.py --keep                # leave the copies to look at

Two design notes, because both are what makes this trustworthy:

* The baseline runs first, in the copy. If the clean copy's target does not pass, every "killed" below would be
  a false positive, so the tool stops rather than reporting a 100 % kill rate for a broken tree.
* Each mutant restores its target file from the pristine tree before applying, so no mutant inherits another
  mutant's damage — a run where two edits combine is a run where a mutant can be killed by the wrong check.

The `target` field names what should notice: `gate:<check>` runs one gate check (fast, and it proves the gate
owns the rule), `test:<file>` runs one unit-test file (for rules the suite guards rather than the gate).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
SCRATCH = Path(tempfile.mkdtemp(prefix="p06-mutation-"))   # the copy lives under here, and so does TMPDIR
SKIP_DIRS = {".git", "node_modules", "__pycache__", "var", ".tmp", ".pytest_cache", "dist", "build", ".venv",
             "docs/verification", "coverage", ".next", ".svelte-kit"}

# (name, file, find, replace, target, why-it-matters)
MUTANTS: list[tuple[str, str, str, str, str, str]] = [
    # ------------------------------------------------------------------ the venue path: batch, fees, caps
    ("batch-cap-16", "packages/polygm_core/venue/clob_v2.py",
     'MAX_BATCH = 15', 'MAX_BATCH = 16', "gate:c_batch_cap_and_partial_failure",
     "a 16th order in a 15-order batch is a silently dropped order, and the venue's own cap is the number"),
    ("short-answer-is-fine", "packages/polygm_core/venue/clob_v2.py",
     '    for idx, (iid, h) in enumerate(zip(order_ids, hashes)):\n        if idx not in seen:',
     '    for idx, (iid, h) in enumerate(zip(order_ids, hashes)):\n        if False:                 # mutant: '
     'no answer means no problem', "gate:c_batch_cap_and_partial_failure",
     "treating a missing item as accepted is how a retry becomes a second order at the venue"),
    ("fee-rounds-down", "packages/polygm_core/venue/clob_v2.py",
     '    builder = muldiv_up(notional, max(builder_bps, 0), 10_000)',
     '    builder = notional * max(builder_bps, 0) // 10_000       # mutant: rounds down', "gate:c_fee_maths_is_the_venues",
     "an estimated fee under the real one spends the user's balance twice: once on the order, once on the gap"),
    ("typed-amount-guard-inert", "packages/polygm_core/wallets/lifecycle.py",
     '    if _norm_decimal(req.typed_amount) != _norm_decimal(_fmt_amount(req.amount_micro)):',
     '    if False:   # mutant: the guard is present and agrees with itself', "test:tests/test_wallets_lifecycle.py",
     "the exact bug this phase shipped with: a broken normaliser made every typed amount match"),
    ("deposit-floor-1e6", "packages/polygm_core/wallets/lifecycle.py",
     '    per_leg = ceil_div(c.gas_units * c.gas_price_wei * c.native_price_micro, 10**18)',
     '    per_leg = ceil_div(c.gas_units * c.gas_price_wei * c.native_price_micro, 10**24)   # mutant: the '
     'original double-divide', "test:tests/test_wallets_lifecycle.py",
     "a floor of $0.0000004 refuses nothing, which is the same as not having a floor"),
    ("unlimited-allowance-too-big", "packages/polygm_core/venue/clob_v2.py",
     'UNLIMITED_ALLOWANCE = 2**63 - 1', 'UNLIMITED_ALLOWANCE = 2**256 - 1', "gate:c_allowance_and_all_in_number",
     "uint256 max does not fit a BIGINT column; the sentinel has to be the largest value we can persist"),

    # ------------------------------------------------------------------- wallets: the machine, not the memo
    ("wallet-self-loops-allowed", "packages/polygm_core/wallets/lifecycle.py",
     'def can_move(frm: str, to: str) -> bool:\n    return to in TRANSITIONS.get(frm, ())',
     'def can_move(frm: str, to: str) -> bool:\n    return to in TRANSITIONS.get(frm, ()) or to == frm  '
     '// mutant: re-entering your own state is fine', "gate:c_wallet_state_machine",
     "a self-loop turns every retry of a stuck transition into a silent no-op that looks like progress"),
    ("store-skips-the-machine", "services/executor/store.py",
     '                wl.assert_transition(row[0], state)',
     '                pass  # mutant: the machine is a suggestion',
     "gate:c_wallet_state_machine",
     "a state machine callers may skip is documentation, and documentation does not refuse a hop"),

    # -------------------------------------------------------------------------- reconcile: absence and proof
    ("failed-lookup-is-absence", "packages/polygm_core/reconcile/reconciler.py",
     '            if not lookup_ok:\n                if not self.store.open_case(case="no_ack"',
     '            if not lookup_ok and False:\n                if not self.store.open_case(case="no_ack"',
     "gate:c_lookup_failure_is_not_absence",
     "eight unreachable passes are not eight confirmed absences; this is the inference that cancels a live order"),
    ("requeue-before-reconcile", "services/executor/main.py",
     '        if reconcile:\n            p = self.reconciler.run_pass(at=t)\n            report["reconcile"] = '
     '{"totals": p.totals, "errors": p.errors, "changed": p.changed}\n            '
     'self.stats["reconcile_passes"] += 1\n        report["requeued"] = '
     'self.store.requeue_expired_claims(at=t)',
     '        report["requeued"] = self.store.requeue_expired_claims(at=t)\n        if reconcile:\n            '
     'p = self.reconciler.run_pass(at=t)\n            report["reconcile"] = {"totals": p.totals, "errors": '
     'p.errors, "changed": p.changed}\n            self.stats["reconcile_passes"] += 1',
     "gate:c_recovery_never_duplicates_a_post",
     "an expired lease and a lost answer look identical in the database; asking the venue first is the only "
     "thing that tells them apart"),
    ("no-attempt-guard", "services/executor/main.py",
     '        prior = self.store.prior_attempt(chash)', '        prior = None   # mutant: never look back',
     "gate:c_recovery_never_duplicates_a_post",
     "the attempt row is the memory that a POST happened; ignoring it is the duplicate order"),
    ("book-fill-not-idempotent", "services/executor/store.py",
     '            if (cur.rowcount or 0) == 0:\n                self._rollback(outer)',
     '            if False:\n                self._rollback(outer)', "gate:c_fill_is_idempotent",
     "one venue trade must be one ledger row; a replayed fill that books twice is money appearing from a retry"),

    # ------------------------------------------------------------------- limits, breaker, switch, deny table
    ("breaker-opens-on-success", "packages/polygm_core/risk/limits.py",
     '        if total >= self.limits.breaker_min_samples and self.failures / total >= self.limits.breaker_error_rate:',
     '        if False:   # mutant: the error-rate trip never fires', "gate:c_breaker_and_loss_halt",
     "a breaker that trips on green lights trains operators to ignore it"),
    ("kill-read-fails-open", "services/executor/store.py",
     '        except Exception:                                   # noqa: BLE001 - fail closed, whatever broke\n'
     '            engaged = True',
     '        except Exception:                                   # noqa: BLE001 - fail closed, whatever broke\n'
     '            engaged = False', "gate:c_kill_switch_latency",
     "a kill switch whose error path is 'carry on trading' is a switch that only works when the database is up"),
    ("loss-halt-acks-itself", "services/executor/store.py",
     '        if not actor.strip():\n            raise ValueError("an acknowledgement with no actor is not an '
     'acknowledgement")',
     '        if False:\n            raise ValueError("unreachable")', "gate:c_breaker_and_loss_halt",
     "an unacknowledged halt that clears itself is a halt that never happened"),
    ("allowance-covers-is-true", "packages/polygm_core/venue/clob_v2.py",
     'def allowance_covers(granted_micro: int, needed_micro: int) -> bool:',
     'def allowance_covers(granted_micro: int, needed_micro: int) -> bool:\n    return True  # mutant',
     "gate:c_allowance_and_all_in_number", "the one check that stops an order the venue will reject for lack of approval"),

    # ---------------------------------------------------------------------------- copy and automation gates
    ("copy-chases-price", "packages/polygm_core/copy/engine.py",
     '    if dev > cfg.max_entry_deviation_bps:', '    if False:   # mutant: the bound is decorative',
     "gate:c_copy_refuses_rather_than_chases",
     "a copy that chases a price the source already paid buys the top of the book on someone else's account"),
    ("copy-ignores-cycle", "packages/polygm_core/copy/engine.py",
     '        if nxt_src in seen:', '        if False:   # mutant: loops are a strategy',
     "gate:c_copy_refuses_rather_than_chases",
     "A copies B copies A is not a strategy, it is a loop that pays fees twice"),
    ("automation-arms-without-dry-run", "packages/polygm_core/automation/engine.py",
     '        if not v["dry_run_completed_ms"]:', '        if False:   # mutant: arm whatever you like',
     "gate:c_automation_refuses_at_save_and_logs_every_pass",
     "a rule the user has never seen evaluated is a position they did not agree to"),
    ("automation-ignores-human", "packages/polygm_core/automation/engine.py",
     '        if human_last_ms and at - human_last_ms < human_priority_ms:', '        if False:',
     "gate:c_automation_refuses_at_save_and_logs_every_pass", "a bot that trades over the hands of the human who just clicked is a bot that gets turned off"),
    ("automation-logs-nothing", "packages/polygm_core/automation/engine.py",
     '    def _record(self, res: RunResult, user_id: str, at: int) -> RunResult:',
     '    def _record(self, res: RunResult, user_id: str, at: int) -> RunResult:\n        if res.outcome != '
     '"placed":\n            return res', "gate:c_automation_refuses_at_save_and_logs_every_pass",
     "the run history IS the audit; skips that are not written are the ones users ask support about"),
    ("template-ships-on-negative-edge", "packages/polygm_core/automation/engine.py",
     '    elif edge_available_bp <= edge_needed_bp:', '    elif False:   # mutant: always ship',
     "gate:c_template_ships_only_on_positive_edge",
     "the brief's condition: ship the entry template only if the fee maths proves the edge, not whenever one "
     "is claimed"),
    ("payout-pays-expected", "packages/polygm_core/revenue/attribution.py",
     '        share = (observed * builder.source_share_bps) // 10_000\n        share = min(share, '
     'observed)                         # a source can never be owed more than was paid',
     '        share = (expected * builder.source_share_bps) // 10_000   # mutant: pays the estimate, and the '
     'cap that made the naive version harmless is removed with it',
     "gate:c_revenue_cannot_pay_itself",
     "our revenue is what the chain says we were paid; paying out the estimate is paying ourselves with money "
     "that may not exist"),

    # --------------------------------------------------------------------- schema, append-only, contract
    ("append-only-protected-tables", "tools/build-sqlite-migrations.py",
     'APPEND_ONLY = ["cash_ledger", "fills", "tape_trades", "builder_attribution", "position_snapshots",',
     'APPEND_ONLY = ["cash_ledger", "fills", "position_snapshots",', "gate:c_schema_parity_and_append_only",
     "a money table without its triggers is a money table that can be edited after the fact"),
    ("preflight-sequence-short", "packages/polygm_core/venue/clob_v2.py",
     'PREFLIGHT_ORDER', 'PREFLIGHT_ORDER_UNUSED_BY_MUTANT', "gate:c_preflight_sequence_is_complete",
     "the sequence being complete is the assertion; renaming it should break the check, not pass quietly"),
    ("deny-code-dropped", "packages/polygm_core/risk/limits.py",
     '    _d("RISK_HALT", 503, True, "hard", "trading is temporarily disabled", "p04"),', '',
     "gate:c_deny_code_table_is_complete", "a refusal with no row in the table cannot be mapped to an HTTP status by the API"),
]


def sh(cmd: list[str], cwd: Path, timeout: int = 1800) -> tuple[int, str]:
    env = dict(os.environ, TMPDIR=str(SCRATCH / "tmp"))   # /tmp fills up in this sandbox and a full tmpdir
    try:                                                  # fails the suite with a confusing rc=120
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, "(timed out)"
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def make_copy(dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for item in sorted(ROOT.iterdir()):
        if item.name in SKIP_DIRS or str(item.relative_to(ROOT)) in SKIP_DIRS:
            continue
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, ignore=shutil.ignore_patterns(*SKIP_DIRS, "*.db", "*.db-wal",
                                                                        "*.log", "node_modules", "__pycache__"))
        else:
            shutil.copy2(item, target)


def run_target(tree: Path, target: str) -> tuple[int, str]:
    kind, name = target.split(":", 1)
    if kind == "gate":
        return sh([PY, str(tree / "tools" / "p06-gate-check.py"), "--only", name], tree)
    return sh([PY, "-W", "ignore::ResourceWarning", "-m", "unittest", "discover", "-s", "tests",
               "-p", Path(name).name], tree, timeout=1500)


def main() -> int:
    ap = argparse.ArgumentParser(description="prove the P06 gate can fail")
    ap.add_argument("--only", default="", help="comma-separated mutant names")
    ap.add_argument("--keep", action="store_true", help="leave the scratch copies on disk")
    a = ap.parse_args()
    wanted = {w.strip() for w in a.only.split(",") if w.strip()}

    # anchors first: a mutant that cannot find its text is a silently skipped mutant, and a skipped mutant
    # shrinks the proof set without saying so
    bad_anchor = []
    for name, rel, old, new, target, _why in MUTANTS:
        path = ROOT / rel
        if not path.exists():
            bad_anchor.append((name, "missing file %s" % rel))
            continue
        if old not in path.read_text():
            bad_anchor.append((name, "anchor not found in %s" % rel))
    if bad_anchor:
        print("MUTANT-CONSTRUCT ERRORS (fix these; a silent skip is not a pass):")
        for name, why in bad_anchor:
            print("  %-34s %s" % (name, why))
        return 1

    tmp = SCRATCH
    (tmp / "tmp").mkdir(exist_ok=True)
    base = tmp / "baseline"
    print("copying the tree to %s and checking the CLEAN targets first" % base)
    make_copy(base)
    targets = sorted({t for _n, _f, _o, _n2, t, _w in MUTANTS})
    for t in targets:
        rc, out = run_target(base, t)
        if rc != 0:
            print("BASELINE DOES NOT PASS for %s — nothing here can be trusted:" % t)
            print(out[-2500:])
            return 1
    print("  baseline clean for %d targets" % len(targets))

    caught, survived = [], []
    for name, rel, old, new, target, why in MUTANTS:
        if wanted and name not in wanted:
            continue
        dst_file = base / rel
        shutil.copy2(ROOT / rel, dst_file)                      # pristine, so mutants never combine
        text = dst_file.read_text()
        dst_file.write_text(text.replace(old, new, 1))
        rc, out = run_target(base, target)
        if rc != 0:
            first = next((ln.strip() for ln in out.splitlines() if ln.strip().startswith("FAIL")), "")
            caught.append(name)
            print("  %-34s killed by %-14s %s" % (name, target, first[:78]))
        else:
            survived.append((name, target, why))
            print("  %-34s SURVIVED %s" % (name, target))
        shutil.copy2(ROOT / rel, dst_file)                        # leave the copy as we found it
    total = len(caught) + len(survived)
    print("\nmutation report: %d/%d mutants killed (%d survived)" % (len(caught), total, len(survived)))
    for name, target, why in survived:
        print("  SURVIVED %s: %s is not enforced by %s — the rule is prose (%s)" % (name, name, target, why))
    if not a.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print("copies left in", tmp)
    print("\nA survived mutant is not a product bug. It is a rule the gate only describes.")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
