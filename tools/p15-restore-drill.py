#!/usr/bin/env python3
"""P15 D7 — the restore drill: a backup that has never been restored is a hope, not a control.

    python3 tools/p15-restore-drill.py --transcript docs/verification/P15-restore-drill.txt --json /tmp/drill.json
    python3 tools/p15-restore-drill.py --source var/polygm.db --out /tmp/drill --no-record

What it does, in the order a real restore happens:

1. **Snapshot.** Copy the database aside (the artefact that would be shipped to object storage), record its size,
   its SHA-256, every table's row count, and the *money checksums* — row count plus the integer micro-sum of each
   money column. Those sums are what a restore must reproduce exactly: a restore that "worked" but dropped a fill
   is a restore that lost somebody's money.
2. **Restore.** A brand-new empty database, the schema rebuilt by the product's own migration runner
   (`tools/run-sql.py --sqlite --dir db/migrations-sqlite`) and then the rows copied table by table. Nothing is
   created by hand here: if the migration runner cannot rebuild the schema, the drill fails, which is the point.
3. **Verify.** Row counts and money checksums match, foreign keys check clean, and the append-only triggers still
   *fire*. The last one matters most, because a restore that drops its guards turns a tamper-evident ledger into an
   editable table.
4. **Record.** The result goes into `backup_restore_tests` (the P07 table, written through the product's own
   store) with the date, the measured times, the verified row count and the verdict. D7 asks for a tested restore
   with a recorded date and result; this is where that record comes from.

The honest scope note, printed in every run: this exercises the *procedure and its verification* against a
database this environment can build. The Postgres path used in production (`pg_dump | age | rclone`, restored from
R2) needs the hosts and the provider credentials, and is marked `[UNVERIFIED]` in `docs/P15-dr.md` until an owner
runs it. A drill that quietly substituted sqlite for Postgres and called it done would be worse than no drill.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "db" / "migrations-sqlite"
#: Never counted and never copied: sqlite's own bookkeeping, and the migration runner's record of what it has
#: applied. The last one matters: a restore *rebuilds* it by running the migrations (that is the point of the
#: exercise), so copying the snapshot's copy over the fresh one would both collide with it and hide the answer to
#: "can the migrations rebuild this schema from empty".
SKIP = {"sqlite_sequence", "sqlite_stat1", "sqlite_stat4", "schema_migrations"}
# One entry per money table: (table, the integer micro column whose sum must survive a restore).
MONEY = (("cash_ledger", "amount_micro"), ("orders", "notional_micro"), ("order_intents", "notional_micro"),
         ("fills", "notional_micro"), ("balances", "available_micro"), ("withdrawals", "amount_micro"),
         ("deposits", "amount_micro"))


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tables(con: sqlite3.Connection) -> list[str]:
    return sorted(r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        if r[0] not in SKIP)


def fingerprint(db: pathlib.Path) -> dict:
    con = sqlite3.connect(str(db))
    try:
        out = {"tables": {}, "money": {}}
        for t in tables(con):
            out["tables"][t] = int(con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0])
        for t, col in MONEY:
            if t not in out["tables"]:
                continue
            row = con.execute('SELECT COUNT(*), COALESCE(SUM("%s"), 0) FROM "%s"' % (col, t)).fetchone()
            out["money"]["%s.%s" % (t, col)] = [int(row[0]), int(row[1] or 0)]
        out["triggers"] = sorted(r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'").fetchall())
        return out
    finally:
        con.close()


def self_check_guards(con: sqlite3.Connection, before: dict) -> tuple[bool, str]:
    """Prove the append-only triggers survived the restore.

    An `UPDATE` against a row that already exists is the strongest form of this check, but a freshly restored
    database may be empty, and "nothing to check" is a much weaker claim than the drill should be making — the
    trigger is a property of the schema, not of the data. So when there is no row to attack, the probe inserts one
    into a table with no foreign keys and attacks that: same rule, same proof, no dependency on what the snapshot
    happened to contain.
    """
    for table in ("cash_ledger", "fills", "order_lifecycle", "kill_switch_state", "chain_events"):
        if table not in before["tables"]:
            continue
        try:
            if before["tables"].get(table, 0) == 0:
                if table != "kill_switch_state":
                    continue
                con.execute("INSERT INTO kill_switch_state (engaged, reason, changed_by, at_ms)"
                            " VALUES (0, 'p15 restore drill: guard probe', 'restore-drill', 1)")
                con.commit()
                rowid = con.execute("SELECT MAX(id) FROM kill_switch_state").fetchone()[0]
            else:
                rowid = con.execute('SELECT MIN(rowid) FROM "%s"' % table).fetchone()[0]
            con.execute('UPDATE "%s" SET rowid = rowid WHERE rowid = ?' % table, (rowid,))
            con.rollback()
            return False, "an UPDATE on %s succeeded after the restore — the trigger did not survive" % table
        except sqlite3.Error:
            con.rollback()
            return True, "trigger still refuses writes to %s" % table
    return True, "no append-only table exists in this schema (nothing to check)"


def restore(source: pathlib.Path, scratch: pathlib.Path, migrations: pathlib.Path) -> dict:
    """Snapshot, rebuild the schema, copy the rows back, verify. Returns the evidence dict."""
    scratch.mkdir(parents=True, exist_ok=True)
    backup, restored = scratch / "backup.db", scratch / "restored.db"
    for f in (backup, restored):
        if f.exists():
            f.unlink()
    timings: list[tuple[str, float]] = []

    def timed(name: str, fn):
        t0 = time.perf_counter()
        value = fn()
        timings.append((name, (time.perf_counter() - t0) * 1000))
        return value

    def snap() -> dict:
        shutil.copy2(source, backup)
        return {"path": str(backup), "bytes": backup.stat().st_size, "sha256": sha256(backup),
                "fingerprint": fingerprint(backup)}

    snap_info = timed("snapshot", snap)

    def rebuild() -> dict:
        env = dict(os.environ, PGM_DB_PATH=str(restored))
        p = subprocess.run([sys.executable, str(ROOT / "tools" / "run-sql.py"), "--sqlite", "--dir",
                            str(migrations.relative_to(ROOT))], cwd=str(ROOT), capture_output=True, text=True,
                           env=env, timeout=900)
        if p.returncode != 0:
            raise SystemExit("the migration runner could not rebuild the schema: %s" % (p.stderr or p.stdout)[-400:])
        con = sqlite3.connect(str(restored))
        try:
            con.execute("PRAGMA foreign_keys=OFF")
            con.execute("ATTACH DATABASE ? AS bak", (str(backup),))
            bk = {r[0] for r in con.execute("SELECT name FROM bak.sqlite_master WHERE type='table'").fetchall()}
            have = {r[0] for r in con.execute("SELECT name FROM main.sqlite_master WHERE type='table'").fetchall()}
            copied = []
            for t in sorted(bk & have):
                if t in SKIP:
                    continue
                cols = [r[1] for r in con.execute('PRAGMA main.table_info("%s")' % t).fetchall()]
                bcols = [r[1] for r in con.execute('PRAGMA bak.table_info("%s")' % t).fetchall()]
                use = [c for c in cols if c in bcols]
                if not use:
                    continue
                # The copy runs with foreign keys deferred and they are re-enabled before verification, because a
                # table whose parent is still empty would otherwise fail on insert order rather than on content.
                con.execute('INSERT INTO main."%s" (%s) SELECT %s FROM bak."%s"'
                            % (t, ", ".join('"%s"' % c for c in use), ", ".join('"%s"' % c for c in use), t))
                copied.append(t)
            missing = sorted(bk - have - SKIP)
            con.commit()
            con.execute("DETACH DATABASE bak")
            return {"tables_copied": len(copied), "tables_missing_from_schema": missing,
                    "rebuilt_not_copied": sorted(SKIP & bk),
                    "runner": "tools/run-sql.py --sqlite --dir db/migrations-sqlite"}
        finally:
            con.close()

    rebuild_info = timed("restore", rebuild)

    def verify() -> dict:
        con = sqlite3.connect(str(restored))
        try:
            before, after = fingerprint(backup), fingerprint(restored)
            con.execute("PRAGMA foreign_keys=ON")
            fk = [tuple(r) for r in con.execute("PRAGMA foreign_key_check").fetchall()]
            guard_ok, guard_msg = self_check_guards(con, before)
            return {"tables_match": before["tables"] == after["tables"],
                    "counts_before": sum(before["tables"].values()), "counts_after": sum(after["tables"].values()),
                    "money_before": before["money"], "money_after": after["money"],
                    "money_match": before["money"] == after["money"],
                    "foreign_key_violations": len(fk), "append_only_guard": guard_msg, "guard_ok": guard_ok,
                    "triggers_before": len(before["triggers"]), "triggers_after": len(after["triggers"])}
        finally:
            con.close()

    verify_info = timed("verify", verify)
    ok = (verify_info["tables_match"] and verify_info["money_match"] and verify_info["guard_ok"]
          and verify_info["foreign_key_violations"] == 0)
    return {"ok": ok, "source": str(source), "snapshot": snap_info, "restore": rebuild_info,
            "verify": verify_info, "timings": [{"phase": n, "ms": round(m, 1)} for n, m in timings],
            "total_ms": round(sum(m for _n, m in timings), 1)}


def record(result: dict, *, tested_by: str, note: str) -> dict | None:
    """Write the row the D7 requirement is judged on: `backup_restore_tests`, through the product's own store."""
    sys.path.insert(0, str(ROOT / "packages"))
    try:
        from polygm_core.security.store import SecStore
    except Exception as exc:                                     # pragma: no cover - import shape
        print("could not import the security store (%s) — the drill ran, its record did not" % exc, file=sys.stderr)
        return None
    db = pathlib.Path(os.environ.get("PGM_DB_PATH") or ROOT / "var" / "polygm.db")
    if not db.exists():
        return None
    now = int(time.time() * 1000)
    store = SecStore.open(str(db))
    started = now - int(result["total_ms"])
    store.record_backup_test(kind="sqlite_file", encrypted=False, taken_ms=started, restore_started_ms=started,
                             restore_done_ms=now, verified_rows=int(result["verify"]["counts_after"]),
                             money_checks_ok=bool(result["verify"]["money_match"]), tested_by=tested_by,
                             note=note[:400])
    return {"kind": "sqlite_file", "verified_rows": int(result["verify"]["counts_after"]),
            "money_checks_ok": bool(result["verify"]["money_match"]), "db": str(db)}


def transcript(result: dict, recorded: dict | None, path: pathlib.Path) -> None:
    import shutil as _sh
    width = min(120, _sh.get_terminal_size((100, 24)).columns)
    lines = ["P15 D7 — restore drill, %s" % dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
             "=" * width,
             "source: %s (%d bytes, sha256 %s)" % (result["source"], result["snapshot"]["bytes"],
                                                   result["snapshot"]["sha256"][:16]),
             "", "phase timings:"]
    lines += ["  %-10s %8.1f ms" % (t["phase"], t["ms"]) for t in result["timings"]]
    lines += ["  %-10s %8.1f ms" % ("total", result["total_ms"]), "",
              "restore: %(tables_copied)d table(s) copied via %(runner)s" % result["restore"]]
    if result["restore"]["tables_missing_from_schema"]:
        lines.append("  MISSING FROM SCHEMA: %s" % ", ".join(result["restore"]["tables_missing_from_schema"]))
    v = result["verify"]
    lines += ["", "verify:",
              "  rows      %d before / %d after   %s" % (v["counts_before"], v["counts_after"],
                                                         "match" if v["tables_match"] else "MISMATCH"),
              "  money     %d column(s) compared   %s" % (len(v["money_before"]),
                                                          "match" if v["money_match"] else "MISMATCH"),
              "  foreign keys: %d violation(s)" % v["foreign_key_violations"],
              "  append-only guards: %s" % v["append_only_guard"],
              "  triggers  %d before / %d after" % (v["triggers_before"], v["triggers_after"]), ""]
    for key, (rows, micro) in sorted(v["money_before"].items()):
        same = v["money_after"].get(key) == [rows, micro]
        lines.append("  %-30s %8d row(s) %20d micro  %s" % (key, rows, micro, "ok" if same else "CHANGED"))
    lines += ["", "record: %s" % (json.dumps(recorded) if recorded else "not written (no store, or --no-record)"),
              "VERDICT: %s" % ("RESTORED AND VERIFIED" if result["ok"] else "FAILED — see the mismatches above"),
              "",
              "Scope, stated plainly: this drill exercises the procedure and its verification against a database",
              "this environment can build. The production transport (pg_dump -> age -> R2 -> restore) needs the",
              "hosts and provider credentials and is marked [UNVERIFIED] in docs/P15-dr.md until an owner runs it."]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P15 D7 — the restore drill")
    ap.add_argument("--source", default=str(ROOT / "var" / "polygm.db"))
    ap.add_argument("--out", default=str(pathlib.Path(os.environ.get("TMPDIR") or "/tmp") / "p15-restore-drill"))
    ap.add_argument("--migrations", default=str(MIGRATIONS))
    ap.add_argument("--json", default="")
    ap.add_argument("--transcript", default="")
    ap.add_argument("--record", dest="record", action="store_true", default=True)
    ap.add_argument("--no-record", dest="record", action="store_false")
    ap.add_argument("--tested-by", default=os.environ.get("PGM_TESTED_BY") or "p15-restore-drill")
    a = ap.parse_args(argv)

    source = pathlib.Path(a.source)
    if not source.exists() or source.stat().st_size < 4096:
        print("no database at %s — run `make migrate && make seed` first (a restore drill needs something to "
              "restore)" % source, file=sys.stderr)
        return 2
    if not pathlib.Path(a.migrations).exists():
        print("no migrations at %s" % a.migrations, file=sys.stderr)
        return 2

    result = restore(source, pathlib.Path(a.out), pathlib.Path(a.migrations))
    recorded = record(result, tested_by=a.tested_by,
                      note="restore drill: %d rows, money checksums %s, total %.0f ms"
                           % (result["verify"]["counts_after"],
                              "matched" if result["verify"]["money_match"] else "MISMATCHED",
                              result["total_ms"])) if a.record else None
    v = result["verify"]
    print("restore drill: %d row(s) across %d table(s), money %s, fk violations %d, guard %s, %.0f ms"
          % (v["counts_after"], len(v["money_before"]),
             "matched" if v["money_match"] else "MISMATCHED", v["foreign_key_violations"], v["append_only_guard"],
             result["total_ms"]))
    for key, (rows, micro) in sorted(v["money_before"].items()):
        print("  %-30s %8d row(s) %20d micro  %s"
              % (key, rows, micro, "ok" if v["money_after"].get(key) == [rows, micro] else "CHANGED"))
    if recorded:
        print("recorded in backup_restore_tests: %s" % json.dumps(recorded))
    if a.json:
        pathlib.Path(a.json).write_text(json.dumps(result, indent=2, default=str) + "\n")
    if a.transcript:
        transcript(result, recorded, pathlib.Path(a.transcript))
        print("transcript: %s" % a.transcript)
    print("VERDICT: %s" % ("RESTORED AND VERIFIED" if result["ok"] else "FAILED"))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
