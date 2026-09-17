#!/usr/bin/env python3
"""Prove the P05 gate can fail, by breaking the product on a copy and requiring the gate to say so.

A gate that prints "13/13" could be printing it because nothing can fail. Each mutant below is a bug this phase
ACTUALLY had, or the exact inverse of a rule P05 depends on, applied to a scratch copy of the tree; then
`tools/p05-gate-check.py` runs there. A mutant the gate does not catch is reported, not forgiven — it means the
rule is prose.

    python3 tools/p05-mutation-test.py                    # all mutants
    python3 tools/p05-mutation-test.py --only artifact-vacuous-alerts,fanout-age-last
    python3 tools/p05-mutation-test.py --keep             # leave the copies for inspection

The `needs_full_gate` flag marks mutants whose catch lives in a gate check rather than in a unit test (the
artifact's non-vacuity rules, the make-target honesty rule, the bounded-range scan). Those run the whole gate;
the rest run the one test file that should notice, which is what keeps this under a minute per mutant.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", "__pycache__", "var", ".pytest_cache", "dist", "build", ".venv",
             ".parcel-cache", ".next", "coverage"}

# (name, file, find, replace, needs_full_gate, test_file_hint)
MUTANTS: list[tuple[str, str, str, str, bool, str]] = [
    # ── the tape: bounded range, refusals, dedupe ─────────────────────────────────────────────────────────
    ("unbounded-tape-page", "services/ingest/main.py",
     'params={"limit": TAPE_PAGE, "takerOnly": "true", "start": since_s, "end": until_s,',
     'params={"limit": TAPE_PAGE, "takerOnly": "true", "_": str(int(self._now() * 1000)),', False,
     "the venue's unbounded /trades is a view minutes stale; a cache-buster is what we believed before measuring"),
        ("shape-refusal-crashes-poll", "services/ingest/main.py",
     '                except (N.ShapeError, ArithmeticError, ValueError, TypeError) as e:\n                    # Defence in depth, and the reason is a bug this phase actually shipped:',
     '                except (N.ShapeError, ArithmeticError, ValueError, TypeError) as e:\n                    raise   # mutant: the guard is present but absorbs nothing',
     False, "one unreadable field must cost one row, not the whole tape"),
    ("notional-float", "services/ingest/normalise.py",
     '        "usd_notional_micro": price_micro * size_micro // 10 ** 6,',
     '        "usd_notional_micro": price_micro * size_micro / 10 ** 6,', False,
     "no float may be stored where a number can be owed"),
    ("strict-fill-price", "services/ingest/normalise.py",
     '    price_micro = fill_micro(price, field="price")',
     '    price_micro = to_micro(price, field="price")', False,
     "the strict parser drops 49 of the 200 recorded fills — this is the bug, restored"),
    ("dedupe-ignores-tx", "services/ingest/tape.py",
     '    return (f.get("tx_hash") or "", f.get("token_id") or "", f.get("side") or "",',
     '    return ("", f.get("token_id") or "", f.get("side") or "",', False,
     "two distinct fills in one market must not merge into one row"),
    ("db-unique-dedupe-gone", "db/migrations-sqlite/0006_ingest.sql",
     "UNIQUE (dedupe_key)", "dedupe_key_uniq_removed_by_mutant", False,
     "replay safety is a constraint, and the constraint is what the gate checks"),

    # ── freshness ownership ───────────────────────────────────────────────────────────────────────────────
    ("poller-advances-socket", "services/ingest/main.py",
     '''                    self.tape.refusals += 1
                    self.tape.last_refusal = repr(e)[:160]''',
     '''                    self.tape.refusals += 1
                    self.tape.last_refusal = repr(e)[:160]
                    self.fresh.note_event("ws.tape", int(self._now() * 1000))''', True,
     "the chaos harness shipped this exact conflation; a poller must not make a dead socket look alive"),
    ("frame-not-proof", "services/ingest/main.py",
     '''        if kind in ("book", "price_change"):
            self.fresh.sources["ws.book"].transport_alive = True
        elif kind == "last_trade_price":
            self.fresh.sources["ws.tape"].transport_alive = True''',
     '''        if kind in ("book", "price_change"):
            pass
        elif kind == "last_trade_price":
            pass''', False,
     "a connected socket that delivers nothing is not live, and a frame is the only proof"),

    # ── the resolution model ──────────────────────────────────────────────────────────────────────────────
    ("resolved-by-invented-column", "services/ingest/main.py",
     'RESOLVED_SQL = "EXISTS (SELECT 1 FROM tokens w WHERE w.market_id = m.id AND w.is_winner IS NOT NULL)"',
     'RESOLVED_SQL = "m.closed = 1"', False,
     "`markets` has no `closed`; this is the 19-test failure from this session, kept as a mutant"),
    ("null-winner-becomes-zero", "services/ingest/main.py",
     '                          (tok, m["id"], (m["outcomes"][idx] if idx < len(m["outcomes"]) else ""), idx, winner))',
     '                          (tok, m["id"], (m["outcomes"][idx] if idx < len(m["outcomes"]) else ""), idx, 0))',
     False, "NULL must not read as FALSE: 0 makes every open market a loss for smart_money"),
    ("label-confidence-float", "services/ingest/main.py",
     '            r["confidence_int"] = int(round(conf * 1000)) if isinstance(conf, float) else int(conf)',
     '            r["confidence_int"] = conf', False, "the column is integer per-mille; a float is a lie about scale"),

    # ── the book ──────────────────────────────────────────────────────────────────────────────────────────
    ("book-merge-leaves-ghosts", "services/ingest/main.py",
     '        self.conn.execute("DELETE FROM book_levels WHERE market_id=?", (market_id,))',
     '        self.conn.execute("DELETE FROM book_levels WHERE 0", (market_id,))', False,
     "an upsert-only ladder keeps a resting order the venue removed, and a user sizes against it"),

    # ── the universe: metadata versions, refusal counting ─────────────────────────────────────────────────
    ("refusals-uncounted", "services/ingest/main.py",
     "                dropped += 1",
     "                pass", False, "a refusal that is invisible is a bug we cannot see arriving"),
    # Two drafts of this mutant were no-ops, and the second one survived the harness, which is the correct
    # outcome for a no-op: a mutant that cannot change behaviour is not a bug. What is mutated here is the
    # metadata-versioning feature's memory — no market ever has a previous observation, so every real venue
    # change (an endDate pushed by a week, a market reopened for orders) is written as nothing.
    ("meta-diff-amnesia", "services/ingest/main.py",
     "                if d:\n                    out[str(cid)] = d",
     "                if False:\n                    out[str(cid)] = d",
     False, "without the stored snapshot the diff has no left-hand side, and history silently stops"),

    # ── signals: cooldown, dedupe, per-window uniqueness ──────────────────────────────────────────────────
    ("cooldown-key-order", "packages/polygm_core/signals/engine.py",
     '                key = "%s|%s" % (a.dedupe_key, rule.id)',
     '                key = "%s|%s" % (rule.id, a.dedupe_key)', False,
     "the other order re-fires every alert once after each deploy, which is what this table exists to stop"),
    ("signals-window-unique-gone", "db/migrations-sqlite/0006_ingest.sql",
     "UNIQUE (rule_id, dedupe_key, fired_bucket)", "", True,
     "one alert per cooldown window is a DB property; the gate checks for the constraint, not the code"),

    # ── delivery fan-out ──────────────────────────────────────────────────────────────────────────────────
    ("fanout-strict-priority", "packages/polygm_core/signals/fanout.py",
     "    live.sort(key=lambda t: (t[0], t[1], int(t[3].get(\"queued_ms\") or 0), str(t[3].get(\"signal_id\", \"\"))))",
     "    live.sort(key=lambda t: (t[1], t[0], int(t[3].get(\"queued_ms\") or 0), str(t[3].get(\"signal_id\", \"\"))))",
     False, "priority as the first key starves the free tier indefinitely"),
    ("fanout-ignores-backoff", "packages/polygm_core/signals/fanout.py",
     "        if due > now_ms:", "        if False:", False,
     "a retry that ignores its own schedule hammers the channel that just said no"),
    ("fanout-lost-lease-free", "packages/polygm_core/signals/fanout.py",
     '                row["attempts"] = attempts + 1        # a lost lease consumed an attempt; pretending otherwise',
     '                row["attempts"] = attempts            # mutant: a wedged worker becomes an infinite queue',
     False, "a reclaim that costs nothing is a queue that never drains"),
    ("fanout-status-int-cast", "packages/polygm_core/signals/fanout.py",
     '    if str(row.get("status") or STATUS_QUEUED) == STATUS_RETRY and attempts > 0:',
     '    if int(row.get("status") or STATUS_QUEUED) == STATUS_RETRY and attempts > 0:', False,
     "the bug I actually wrote here: casting a status string to int"),

    # ── schema-tooling ────────────────────────────────────────────────────────────────────────────────────
    ("alter-drop-silent", "tools/build-sqlite-migrations.py",
     '            if "ADD COLUMN" in up:',
     '            if False and "ADD COLUMN" in up:   # mutant: record the drop and move on',
     True, "a dev schema missing a column production has is how an ALTER ships untested"),

    # ── the gate's own eyes: does it read the artifact or nod at it? ───────────────────────────────────────
    ("artifact-vacuous-alerts", "docs/verification/P05-chaos-output.txt",
     "B. no duplicate alerts (", "B. no duplicate alerts (0 alerts, 0 rules fired) (", True,
     "check B 'passing' with zero alerts is the false pass this phase already shipped once"),
    ("artifact-short-outage", "docs/verification/P05-chaos-output.txt",
     "ALL 7 CHECKS PASSED", "ALL 7 CHECKS PASSED (outage 20s", True,
     "the gate names five minutes; a 20 s run must not satisfy it"),
    ("artifact-inconclusive-tape", "docs/verification/P05-chaos-output.txt",
     "no missed large fills", "INCONCLUSIVE: reference could not reach the window end; no missed large fills",
     True, "a comparison over a reference that never reached the window is not evidence"),
    ("docs-overclaim", "docs/P05-data-ingestion.md",
     "## Measured", "## Measured\n\nALL 7 CHECKS PASSED on the live run.\n", True,
     "if the artifact does not say it, the doc may not either"),
    ("make-eats-exit-code", "Makefile",
     "\t@$(PY) tools/datasource-probe.py --check-cache; rc=$$?; \\",
     "\t@$(PY) tools/datasource-probe.py --check-cache || echo ok; rc=0; \\", True,
     "a swallowed exit code is how a wrong conclusion survived a whole phase"),
]


def make_copy(dst: Path) -> None:
    def ignore(dirpath: str, names: list[str]) -> set[str]:
        return {n for n in names if n in SKIP_DIRS}
    shutil.copytree(ROOT, dst, ignore=ignore)


def run_gate(dst: Path, full: bool) -> tuple[int, str]:
    # `full` means "the whole offline gate", never "--live": the live gate re-runs the 300-second outage, and
    # multiplying that by every gate-level mutant would make this harness a 40-minute benchmark instead of a
    # proof. No mutant targets the live run itself — the artifact checks read what it left behind.
    argv = [sys.executable, "tools/p05-gate-check.py", "--fast"]
    env = dict(os.environ, PWD=str(dst))
    p = subprocess.run(argv, cwd=str(dst), env=env, capture_output=True, text=True, timeout=1800)
    return p.returncode, (p.stdout + p.stderr)


def run_tests(dst: Path, patterns: list[str]) -> tuple[int, str]:
    """One test file per quick mutant, because a full-suite pass/fail cannot tell you WHICH rule noticed."""
    rc_all, out_all = 0, ""
    for pat in patterns:
        p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", pat, "-q"],
                           cwd=str(dst), capture_output=True, text=True, timeout=600)
        rc_all |= p.returncode
        out_all += p.stdout + p.stderr
    return rc_all, out_all


def apply_mutation(dst: Path, rel: str, old: str, new: str) -> str | None:
    f = dst / rel
    if not f.is_file():
        return "file not found: %s" % rel
    txt = f.read_text()
    if old not in txt:
        return "anchor not present in %s: %r" % (rel, old[:70])
    if txt.count(old) > 1 and rel.endswith(".py"):
        return "anchor is ambiguous in %s (%d matches) — the mutant would be non-deterministic" % (rel,
                                                                                                    txt.count(old))
    f.write_text(txt.replace(old, new, 1))
    return None


TEST_HINT = {
    "services/ingest/main.py": ["test_ingest_main.py", "test_ingest.py"],
    "services/ingest/normalise.py": ["test_ingest.py", "test_ingest_main.py"],
    "services/ingest/tape.py": ["test_ingest.py"],
    "services/ingest/books.py": ["test_ingest.py"],
    "services/ingest/freshness.py": ["test_ingest.py"],
    "services/ingest/universe.py": ["test_ingest.py"],
    "packages/polygm_core/signals/engine.py": ["test_signals.py", "test_ingest_main.py"],
    "packages/polygm_core/signals/fanout.py": ["test_fanout.py"],
    "packages/polygm_core/classify/labels.py": ["test_ingest.py", "test_ingest_main.py"],
    "db/migrations-sqlite/0006_ingest.sql": ["test_ingest_main.py", "test_migrations.py"],
    "tools/build-sqlite-migrations.py": [],          # gate-only
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        for name, rel, _o, _n, full, why in MUTANTS:
            print("  %-28s %-42s %s%s" % (name, rel, "[gate] " if full else "[test] ", why[:70]))
        print("%d mutants" % len(MUTANTS))
        return 0
    wanted = {x.strip() for x in a.only.split(",") if x.strip()}

    # Anchors are verified against the working tree BEFORE any copy: a mutant whose anchor is missing is not a
    # caught bug, and a silently-dead mutant is the failure I refuse to ship twice.
    bad = []
    for name, rel, old, new, _full, _why in MUTANTS:
        f = ROOT / rel
        if not f.is_file():
            bad.append("%s: file missing %s" % (name, rel))
        elif old not in f.read_text():
            bad.append("%s: anchor not present in %s" % (name, rel))
    if bad:
        print("MUTANT ANCHORS DO NOT MATCH THE TREE:")
        for b in bad:
            print("  " + b)
        print("\nThis is not a product bug and not a pass. Fix the anchors; a silent skip here would shrink the "
              "proof set without saying so.")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="p05-mutation-"))
    base = tmp / "baseline"
    print("copying the tree to %s and checking the CLEAN gate first" % base)
    make_copy(base)
    rc, out = run_gate(base, full=False)
    if rc != 0:
        print("BASELINE GATE DOES NOT PASS — nothing here can be trusted:")
        print(out[-3000:])
        return 1
    summary = [l for l in out.splitlines() if l.startswith("P05 gate:")]
    print("  baseline: %s" % (summary[0] if summary else out.splitlines()[0]))

    caught, missed, broken = [], [], []
    for name, rel, old, new, full, why in MUTANTS:
        if wanted and name not in wanted:
            continue
        dst = tmp / ("m-" + name)
        shutil.rmtree(dst, ignore_errors=True)
        make_copy(dst)
        err = apply_mutation(dst, rel, old, new)
        if err:
            broken.append((name, err))
            print("  %-28s MUTANT-ERROR  %s" % (name, err))
            continue
        if full:
            rc, out = run_gate(dst, full=True)
            fails = [l.strip() for l in out.splitlines() if l.strip().startswith("FAIL")]
            where = fails[0][:110] if fails else ""
        else:
            rc, out = run_tests(dst, TEST_HINT.get(rel, ["test_ingest.py"]))
            fails = [l.strip() for l in out.splitlines()
                     if l.strip().startswith(("FAIL:", "ERROR:"))]
            where = (fails[0][:110] if fails else re_summary(out))
        if rc != 0:
            caught.append(name)
            print("  %-28s caught by: %s" % (name, where))
        else:
            missed.append((name, why))
            print("  %-28s *** NOT CAUGHT *** (%s)" % (name, rel))
        if not a.keep:
            shutil.rmtree(dst, ignore_errors=True)

    print("\nmutation report: %d caught, %d NOT caught, %d mutant-construct errors (%d mutants)"
          % (len(caught), len(missed), len(broken), len(caught) + len(missed) + len(broken)))
    for name, why in missed:
        print("  UNCAUGHT %s: the gate cannot see this class of breakage (%s)" % (name, why))
    for name, why in broken:
        print("  BROKEN   %s: %s" % (name, why))
    if not a.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print("copies left in", tmp)
    print("\nA missed mutant is not a pass for the product — it is a rule the gate only states in prose.")
    return 1 if (missed or broken) else 0


def re_summary(out: str) -> str:
    m = [l for l in out.splitlines() if re.match(r"^(FAILED|OK)", l)]
    return m[-1][:110] if m else "(no summary)"



if __name__ == "__main__":
    sys.exit(main())
