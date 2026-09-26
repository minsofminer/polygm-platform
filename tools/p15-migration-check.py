#!/usr/bin/env python3
"""P15 D3 — migrations may only ever *add*, until a later release removes.

    python3 tools/p15-migration-check.py                     # new migrations since origin/main
    python3 tools/p15-migration-check.py --since v1.2.0      # everything a release would ship
    python3 tools/p15-migration-check.py --all               # the historical inventory (informational)

The rule, in the kit's words: **expand-contract only**. Between the moment a migration runs and the moment the
last old container is gone, both versions of the code talk to the same schema. Anything that removes or renames
breaks the old code; anything that adds a `NOT NULL` column without a default breaks the old *writer*. So:

* **Additive by default.** A new column is nullable, or has a default. `NOT NULL` without one is a deploy that
  succeeds and then fails at 2am when an old pod writes a row.
* **Renames never.** Not once. Add the new column, backfill, switch the code, drop the old one a release later.
* **Destructive statements need a contract marker.** `DROP TABLE`/`DROP COLUMN`/`ALTER … TYPE` are allowed only in
  a migration that says which earlier migration added the thing (`-- CONTRACT: 0021_add_x`), and that migration
  must be **at least seven days older** — one release, not one afternoon.
* **Indexes on the big tables are CONCURRENTLY.** A plain `CREATE INDEX` takes a write lock; on `fills` or the
  tape that is an outage with a nice SQL error. Those migrations must also declare `-- NO-TRANSACTION:` since
  CONCURRENTLY cannot run inside one.
* **Append-only tables stay append-only.** `UPDATE`/`DELETE` against any table on the repo's own append-only list
  (read from `db/migrations-sqlite/DROPPED.json`, which the transpiler already maintains) is a finding, not a
  migration.
* **The SQLite twin exists and still transpiles.** `tools/build-sqlite-migrations.py --check` is the repo's own
  gate, and a production migration with no test-side twin is the "tests pass, production differs" shape.

None of this replaces review. It catches the mistakes that are mechanical — which is most of the ones that page
somebody.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PG = ROOT / "db" / "migrations"
SQLITE = ROOT / "db" / "migrations-sqlite"
DROPPED = SQLITE / "DROPPED.json"

#: Tables where a non-concurrent index is an outage rather than a slow migration. Kept short and specific: a list
#: that names everything is a list nobody reads.
BIG_TABLES = ("fills", "tape_trades", "order_lifecycle", "reconcile_actions", "position_snapshots", "chain_events")

CONTRACT_MARKER = re.compile(r"^--\s*CONTRACT:\s*(\S+)", re.M)
NO_TXN_MARKER = re.compile(r"^--\s*NO-TRANSACTION:", re.M)
ADD_COLUMN = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(\w+)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)([^;]*);",
    re.I | re.S)
DESTRUCTIVE = re.compile(
    r"\b(DROP\s+TABLE|DROP\s+COLUMN|ALTER\s+COLUMN\s+\w+\s+(?:SET\s+DATA\s+)?TYPE|RENAME\s+(?:TO|COLUMN))\b", re.I)
CREATE_INDEX = re.compile(r"\bCREATE\s+(UNIQUE\s+)?INDEX\s+(CONCURRENTLY\s+)?(\w+)\s+ON\s+(\w+)", re.I)
WRITE_ROW = re.compile(r"\b(DELETE\s+FROM|UPDATE)\s+([a-z_][a-z0-9_]*)", re.I)


class Report:
    def __init__(self, soft: bool = False) -> None:
        self.failures: list[str] = []
        self.passes = 0
        self.notes: list[str] = []
        self.soft = soft  # inventory mode: failures become notes, because history is not a change set

    def check(self, ok: bool, what: str, why: str = "") -> bool:
        if ok:
            self.passes += 1
        elif self.soft:
            self.notes.append(what + ((" — " + why) if why else ""))
        else:
            self.failures.append("%s%s" % (what, (" — " + why) if why else ""))
        return bool(ok)


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return ""


def commit_time(path: pathlib.Path) -> int | None:
    """When this file first appeared, unix seconds. Used for the seven-day rule; None if git cannot say (a shallow
    clone, or a file that is still uncommitted — in which case the call is made on the file's existence)."""
    out = git("log", "--diff-filter=A", "--format=%ct", "--", str(path.relative_to(ROOT)))
    return int(out.splitlines()[-1]) if out else None


def added_since(ref: str) -> list[pathlib.Path]:
    out = git("diff", "--name-only", "--diff-filter=A", f"{ref}...HEAD", "--", "db/migrations")
    files = [ROOT / line for line in out.splitlines() if line.strip().endswith(".sql")]
    # Uncommitted work counts too: a migration sitting in the working tree is exactly the one about to ship.
    out2 = git("status", "--porcelain", "--", "db/migrations")
    for line in out2.splitlines():
        if line.startswith("??") or line.startswith("A "):
            path = ROOT / line[3:].strip().strip('"')
            if path.suffix == ".sql" and path not in files:
                files.append(path)
    return sorted(files)


def append_only_tables() -> set[str]:
    if not DROPPED.exists():
        return set()
    return set(json.loads(DROPPED.read_text()).get("append_only") or [])


def judge_sql(name: str, sql: str, *, append_only: set[str], strict_contract: bool,
              pg: pathlib.Path = PG, sqlite: pathlib.Path = SQLITE,
              commit_time_of=None, soft: bool = False) -> Report:
    """The rules, applied to one migration's text. Pure enough to unit-test with synthetic SQL, which is how the
    rules get canaried — a lint nobody has seen fail is a lint nobody knows works."""
    rep = Report(soft=soft)
    body = re.sub(r"--[^\n]*", "", sql)  # comments out of the way before any statement is judged

    # ---- additive columns
    for table, column, rest in ADD_COLUMN.findall(body):
        has_default = re.search(r"\bDEFAULT\b", rest, re.I) is not None
        not_null = re.search(r"\bNOT\s+NULL\b", rest, re.I) is not None
        rep.check(not (not_null and not has_default),
                  "%s: %s.%s is nullable or defaulted" % (name, table, column),
                  "NOT NULL without DEFAULT breaks every writer that is still on the old code")

    # ---- destructiveness
    dest = [m.group(0).upper() for m in DESTRUCTIVE.finditer(body)]
    if dest:
        m = CONTRACT_MARKER.search(sql)
        if not m:
            rep.check(False, "%s: destructive statement is marked as a contract" % name,
                      "found %s with no `-- CONTRACT: <migration>` marker; expand-contract means the removal "
                      "rides one release behind the code that stopped using it" % sorted(set(dest)))
        else:
            target = pg / (m.group(1) if m.group(1).endswith(".sql") else m.group(1) + ".sql")
            rep.check(target.exists() and target.name < name,
                      "%s: contract names an earlier migration (%s)" % (name, m.group(1)),
                      "the marker must name the migration that added the thing you are removing")
            # `commit_time_of` answers "when was this file added, in epoch seconds"; the rule is in days.
            t0 = commit_time_of(target) if (commit_time_of and target.exists()) else None
            age_days = (dt.datetime.now(dt.timezone.utc).timestamp() - t0) / 86400 if t0 else None
            if strict_contract and age_days is not None:
                rep.check(age_days >= 7, "%s: the contract is a release old (%.1f days)" % (name, age_days),
                          "seven days is the floor: one release, not one afternoon")
            elif age_days is None:
                rep.notes.append("%s: contract age not verifiable (shallow clone or uncommitted) — recorded, "
                                 "not failed" % name)

    # ---- index creation on the big tables
    for _unique, concurrently, index_name, table in CREATE_INDEX.findall(body):
        if table in BIG_TABLES:
            rep.check(bool(concurrently), "%s: index %s on %s must be created CONCURRENTLY" % (name, index_name, table),
                      "a plain CREATE INDEX write-locks a table that already has rows")
            if concurrently:
                rep.check(bool(NO_TXN_MARKER.search(sql)),
                          "%s: declares -- NO-TRANSACTION: for the CONCURRENTLY index" % name,
                          "CREATE INDEX CONCURRENTLY cannot run inside a transaction; the runner needs to know")

    # ---- append-only tables
    for verb, table in WRITE_ROW.findall(body):
        rep.check(table not in append_only,
                  "%s: %s on %s does not touch an append-only table" % (name, verb.upper(), table),
                  "append-only is a promise the product makes in its own docs; a migration is not an exception")

    # ---- the twin
    rep.check((sqlite / name).exists(), "%s: the SQLite twin exists" % name,
              "a migration with no test-side twin is the 'tests pass, production differs' shape")
    return rep


def check_file(path: pathlib.Path, rep: Report, append_only: set[str], *, strict_contract: bool) -> None:
    """Judge one real file and merge the result into the run's report."""
    name = str(path.relative_to(ROOT))
    sub = judge_sql(name.split("/")[-1], path.read_text(), append_only=append_only,
                    strict_contract=strict_contract, commit_time_of=commit_time, soft=rep.soft)
    rep.passes += sub.passes
    rep.failures += sub.failures
    rep.notes += sub.notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P15 D3 — expand-contract migration check")
    ap.add_argument("--since", default=None,
                    help="git ref to compare against (default: origin/main if it exists, else the previous commit)")
    ap.add_argument("--all", action="store_true", help="check the historical inventory instead of a change set")
    ap.add_argument("--out", help="write this report here as well")
    args = ap.parse_args(argv)

    rep = Report(soft=args.all)
    append_only = append_only_tables()
    if args.all:
        files, strict, label = sorted(PG.glob("*.sql")), False, "every migration on disk (informational)"
    else:
        ref = args.since or ("origin/main" if git("rev-parse", "--verify", "origin/main") else "HEAD~1")
        files = added_since(ref)
        strict, label = True, "added since %s" % ref

    lines = ["P15 D3 — expand-contract migration check", "=" * 72, label, ""]
    if not files:
        lines.append("No new migrations. Nothing to remove, nothing to fear.")
    for path in files:
        if path.exists():
            check_file(path, rep, append_only, strict_contract=strict)

    # ---- the repo's own transpile gate, run rather than trusted
    transpile = subprocess.run([sys.executable, str(ROOT / "tools" / "build-sqlite-migrations.py"), "--check"],
                               cwd=ROOT, capture_output=True, text=True)
    rep.check(transpile.returncode == 0, "the SQLite twin still transpiles from the Postgres set",
              (transpile.stdout + transpile.stderr).strip().splitlines()[-1] if transpile.returncode else "")

    lines += ["Files checked: %d" % len(files), "Checks passed: %d, failed: %d" % (rep.passes, len(rep.failures))]
    for n in rep.notes:
        lines.append("  note %s" % n)
    for f in rep.failures:
        lines.append("  FAIL %s" % f)
    lines.append("")
    lines.append("Verdict: %s" % ("PASS" if not rep.failures else "FAIL"))
    text = "\n".join(lines) + "\n"
    print(text)
    if args.out:
        pathlib.Path(args.out).write_text(text)
    return 1 if rep.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
