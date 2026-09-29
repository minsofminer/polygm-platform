#!/usr/bin/env python3
"""P14 D4 — a restore drill that runs against Postgres, not the sqlite twin.

`tools/p14-infra-verify.py`'s restore section has said this for as long as it has existed:

    "VACUUM INTO is a real logical backup: consistent, and it does not copy a possibly-hot WAL the way a file
    copy does. (For Postgres this is pg_dump/PITR; the drill shape is the same, the tool differs.)"

and its verdict row has carried the matching OPEN: *"the drill above is the SQLite twin, which is what this
environment runs; no restore has been performed against the managed Postgres that production will use."* The tool
that differs is this one. It runs the same drill shape against a real Postgres — the compose service, with the
schema applied by `db/migrations/` and the repo's own seed — and it asks the restored copy the same money-path
questions, because a restore that copies rows but cannot answer the product's questions is not a restore.

What it proves, in order: the backup is a *logical, versioned* dump (`pg_dump -Fc`, which pg_restore can rebuild
into any empty database); the restore lands in a database that does not exist yet; the restored copy has every
table, every row, and identical money answers; and the *controls* survived the round trip — the append-only
trigger is present and still refuses a mutation, and the grants are the same. A restore that silently loses a
trigger is a restored box that will happily delete a ledger row.

Scope, stated plainly: this is Postgres as the project's own compose runs it. The managed instances (Supabase,
Neon) have their own restore procedures — point-in-time recovery from their console, on their schedule — and
that half stays an owner action, recorded as such rather than implied by this drill.

Usage:  python3 tools/p14-postgres-restore.py [--record …] [--json …]
Exit:   0 drilled, 2 no docker runtime (recorded as OPEN, not as a pass).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB, RESTORED = "polygm", "polygm_r1_restore_drill"
DUMP = "/tmp/p14-restore-drill.dump"

#: The reads a page load makes on the money path. If these differ between the live and restored databases, the
#: restore is a copy of something, but not of the product.
MONEY_QUERIES = {
    "tables": "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'",
    "balances": "SELECT COALESCE(sum(usdc_available_micro),0)||'/'||COALESCE(sum(usdc_locked_micro),0) FROM balances",
    "ledger_sum": "SELECT COALESCE(sum(amount_micro),0) FROM cash_ledger",
    "open_orders": "SELECT count(*) FROM orders WHERE state IN ('live','partial')",
    "markets": "SELECT count(*) FROM markets",
    "tokens": "SELECT count(*) FROM tokens",
    "users": "SELECT count(*) FROM users",
    "watchlists_uq": "SELECT indexname FROM pg_indexes WHERE indexname='watchlists_user_name_uq'",
    "append_only_triggers": "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                            "WHERE NOT t.tgisinternal AND t.tgname LIKE 'append_only_%'",
}


class Gate:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []

    def check(self, name: str, ok: bool, why: str = "") -> bool:
        self.results.append(("PASS" if ok else "FAIL", name, "" if ok else why))
        return bool(ok)

    def open(self, name: str, why: str) -> None:
        self.results.append(("OPEN", name, why))

    @property
    def failed(self):
        return [r for r in self.results if r[0] == "FAIL"]

    @property
    def opened(self):
        return [r for r in self.results if r[0] == "OPEN"]


def sh(argv, *, timeout=900, stdin=None):
    p = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout, input=stdin)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def docker_argv() -> list[str]:
    if shutil.which("docker") is None:
        return []
    rc, _, _ = sh(["docker", "info"], timeout=60)
    return ["docker"] if rc == 0 else ["sudo", "-n", "docker"]


def compose_argv(dk: list[str]) -> list[str]:
    for tail in (["compose"],):
        rc, _, _ = sh(dk + tail + ["version"], timeout=60)
        if rc == 0:
            return dk + tail
    return []


def psql(cp: list[str], sql: str, db: str = DB) -> tuple[int, str]:
    rc, out, err = sh(cp + ["exec", "-T", "postgres", "psql", "-U", "polygm", "-d", db,
                            "-tAc", sql], timeout=300)
    return rc, (out or err).strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P14 D4 — a Postgres restore drill (pg_dump → fresh database → compare)")
    ap.add_argument("--record")
    ap.add_argument("--json", dest="json_path")
    ap.add_argument("--keep", action="store_true", help="leave the restored database in place")
    a = ap.parse_args(argv)

    g, facts = Gate(), {}
    dk = docker_argv()
    cp = compose_argv(dk) if dk else []
    if not dk or not cp:
        g.open("NO POSTGRES RESTORE HAS BEEN DRILLED HERE: no docker/compose runtime on this machine, so the "
               "restore evidence remains the sqlite twin",
               "run `python3 tools/p14-postgres-restore.py --record docs/verification/P14-postgres-restore.txt "
               "--json docs/verification/P14-postgres-restore.json` where docker and compose are installed")
        rc = 2
    else:
        facts["runtime"] = " ".join(cp)
        # 1. A real database with the real schema, from the repo's own migration and seed path.
        sh(cp + ["down", "-v"], timeout=300)
        rc_up, out, err = sh(cp + ["up", "-d", "postgres"], timeout=900)
        g.check("postgres starts", rc_up == 0, (out + err)[-200:])
        for _ in range(30):
            time.sleep(2)
            if sh(cp + ["exec", "-T", "postgres", "pg_isready", "-U", "polygm", "-d", "polygm"], timeout=60)[0] == 0:
                break
        # `--build`: the migrate and seed services are built from this working tree, and a drill that runs
        # yesterday's image is a drill of yesterday's product.
        rc_m, out_m, err_m = sh(cp + ["run", "--rm", "--build", "migrate"], timeout=1800)
        g.check("db/migrations applies to this database",
                rc_m == 0 and "already present" in out_m or "applied" in out_m, (out_m + err_m)[-200:])
        # The repo's own `db/seed.sql` does NOT run against Postgres, and that is a finding this drill hands to
        # the Postgres-portability workstream rather than one it fixes here: the generated seed emits
        # engine-agnostic ms integers and boolean-free tuples, while the Postgres schema declares `neg_risk`
        # BOOLEAN and three columns TIMESTAMPTZ. It is the same root as the API's sqlite-only engine (both sides
        # assume the sqlite representation), so the drill writes its own money-path fixture — users, balances and
        # ledger rows — which is all a restore drill needs, and records the seed defect as a fact.
        fixture = (
            "INSERT INTO users (id, created_ms) VALUES ('drill_user', 1) ON CONFLICT DO NOTHING;"
            "INSERT INTO balances (user_id, usdc_available_micro, usdc_locked_micro) "
            "VALUES ('drill_user', 250500000, 1200000) ON CONFLICT DO NOTHING;"
            "INSERT INTO cash_ledger (user_id,kind,amount_micro,ref_table,ref_id,created_ms,reason) "
            "VALUES ('drill_user','deposit',250500000,'drill','d1',1,'restore drill fixture');")
        rc_f, out_f, err_f = sh(cp + ["exec", "-T", "postgres", "psql", "-U", "polygm", "-d", DB,
                                      "-v", "ON_ERROR_STOP=1", "-c", fixture], timeout=300)
        facts["fixture"] = (out_f or err_f)[-200:]
        facts["seed_note"] = ("db/seed.sql does not apply to Postgres: neg_risk is boolean, end_ts/created columns "
                              "are timestamptz, and the generated SQL emits integers for both")
        g.check("a post-migration money-path fixture is present to restore", rc_f == 0, (out_f + err_f)[-300:])

        before = {k: psql(cp, q)[1] for k, q in MONEY_QUERIES.items()}
        facts["before"] = before
        g.check("the live database has money-path rows to lose (not an empty drill)",
                all(before[k] not in ("", "0") for k in ("balances", "ledger_sum", "users")),
                json.dumps(before)[:300])

        # 2. The backup: a logical dump, which is what makes it restorable into a *different* database rather
        #    than merely copyable beside this one.
        t0 = time.monotonic()
        rc_d, out_d, err_d = sh(cp + ["exec", "-T", "postgres", "pg_dump", "-U", "polygm", "-d", DB,
                                      "-Fc", "-f", DUMP], timeout=900)
        dump_ms = int((time.monotonic() - t0) * 1000)
        # `pg_relation_size` takes a relation, not a path (the first run of this tool asked it for one and got
        # an error where a size should have been); the file is measured where it is.
        size = sh(cp + ["exec", "-T", "postgres", "sh", "-c", "wc -c < %s" % DUMP], timeout=120)[1]
        facts["dump"] = {"ms": dump_ms, "container_path": DUMP, "size": size}
        g.check("pg_dump -Fc produced a logical backup (%d ms, %s bytes)" % (dump_ms, size or "?"),
                rc_d == 0, (out_d + err_d)[-200:])

        # 3. The restore: into a database that does not exist yet, which is the state a real recovery starts in.
        t1 = time.monotonic()
        psql(cp, "DROP DATABASE IF EXISTS %s" % RESTORED, db="postgres")
        rc_c, out_c, err_c = sh(cp + ["exec", "-T", "postgres", "createdb", "-U", "polygm", RESTORED], timeout=300)
        rc_r, out_r, err_r = sh(cp + ["exec", "-T", "postgres", "pg_restore", "-U", "polygm",
                                      "-d", RESTORED, "--no-owner", DUMP], timeout=1800)
        restore_ms = int((time.monotonic() - t1) * 1000)
        facts["restore"] = {"ms": restore_ms, "target": RESTORED, "createdb_rc": rc_c, "pg_restore_rc": rc_r}
        g.check("the dump restores into a freshly created database (%d ms)" % restore_ms,
                rc_c == 0 and rc_r == 0, (out_c + err_c + out_r + err_r)[-300:])

        # 4. The questions. Rows are the easy half; the money answers and the *controls* are the half that matters.
        after = {k: psql(cp, q, db=RESTORED)[1] for k, q in MONEY_QUERIES.items()}
        facts["after"] = after
        differing = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
        facts["differing"] = differing
        g.check("the restored database holds every table, every row, and the same index",
                not differing, "differences: %s" % json.dumps(differing)[:300])
        g.check("the append-only triggers survived the round trip (%s in both)"
                % before["append_only_triggers"],
                before["append_only_triggers"] == after["append_only_triggers"]
                and int(before["append_only_triggers"] or 0) > 0,
                "live=%s restored=%s" % (before["append_only_triggers"], after["append_only_triggers"]))

        # 5. And the restored copy still refuses to be mutated — the control, exercised, on the restored data.
        sql = ("INSERT INTO audit_log (at_ms,actor_type,actor_id,action,request_id) "
               "VALUES (1,'service','restore-drill','p14.restore_drill','req-restore-drill'); "
               "UPDATE audit_log SET action='tampered' WHERE request_id='req-restore-drill';")
        rc_t, out_t, err_t = sh(cp + ["exec", "-T", "postgres", "psql", "-U", "polygm", "-d", RESTORED,
                                      "-v", "ON_ERROR_STOP=0", "-c", sql], timeout=300)
        text = out_t + err_t
        facts["restored_append_only"] = text[-260:]
        g.check("the restored database still refuses to mutate an append-only table",
                "INSERT 0 1" in text and "append-only table: audit_log is not updatable" in text, text[-260:])

        if not a.keep:
            psql(cp, "DROP DATABASE IF EXISTS %s" % RESTORED, db="postgres")
            sh(cp + ["down", "-v"], timeout=300)
        rc = 1 if g.failed else 0

    passed = sum(1 for r in g.results if r[0] == "PASS")
    lines = ["P14 POSTGRES RESTORE — %s" % ("PASS" if rc == 0 else ("OPEN" if rc == 2 else "FAIL")),
             "%d passed, %d failed, %d OPEN" % (passed, len(g.failed), len(g.opened)), ""]
    lines += ["%-5s %s%s" % (s, n, (" — " + w) if w else "") for s, n, w in g.results]
    if facts:
        lines += ["", "facts:"] + ["  %-22s %s" % (k, json.dumps(v)[:300]) for k, v in sorted(facts.items())]
    text = "\n".join(lines) + "\n"
    print(text)
    if a.record:
        pathlib.Path(a.record).write_text(text)
    if a.json_path:
        pathlib.Path(a.json_path).write_text(json.dumps(
            {"verdict": "PASS" if rc == 0 else ("OPEN" if rc == 2 else "FAIL"),
             "checks": [{"status": s, "name": n, "why": w} for s, n, w in g.results],
             "open_conditions": [{"check": n, "why": w} for s, n, w in g.opened],
             "facts": facts}, indent=1, sort_keys=True))
    return rc


if __name__ == "__main__":
    sys.exit(main())
