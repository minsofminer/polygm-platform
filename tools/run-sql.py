#!/usr/bin/env python3
"""Apply .sql migrations, idempotently, with a ledger — `make migrate`.

Two engines, one contract:
    tools/run-sql.py --dir db/migrations        --url $PGM_DB_URL          # Postgres (asyncpg/psycopg)
    tools/run-sql.py --dir db/migrations-sqlite --sqlite                   # dev/CI portable subset

Why a ledger table instead of "run every file every time": `CREATE INDEX` and `ALTER ... ADD CONSTRAINT` are
not re-runnable, and a deploy that must be manually edited to be idempotent is a deploy that fails on the
second run. Applied filenames are recorded, and a file that changed after being applied is a HARD error
(hashes below) — silent re-application of an edited migration is how a team ends up with three different
schemas on three different boxes.
"""
from __future__ import annotations
import argparse, hashlib, os, re, sqlite3, sys
from pathlib import Path

LEDGER = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name       TEXT PRIMARY KEY,
    sha256     TEXT NOT NULL,
    applied_ms BIGINT NOT NULL
)
"""
# Statement splitting. sqlite3.executescript handles `;` itself, but Postgres drivers do not, and the
# migrations use `$$ ... $$` bodies: a naive split on ';' corrupts a trigger definition in a way that only
# shows up as a syntax error mid-deploy.
def split_statements(sql: str) -> list[str]:
    """Split on `;` while respecting $$ dollar quotes, ' strings, and -- comments.

    A naive split is not a cosmetic problem here: the 0005 migrations contain trigger bodies whose RAISE
    message carries a semicolon, and splitting inside `$$ ... $$` yields two syntactically invalid
    statements that fail mid-deploy, after the earlier files have already been applied.
    """
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    in_dollar = in_sq = False
    while i < n:
        ch = sql[i]
        if not in_dollar and not in_sq and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            continue
        if not in_sq and sql.startswith("$$", i):
            buf.append("$$")
            in_dollar = not in_dollar
            i += 2
            continue
        if not in_dollar and ch == "'":
            in_sq = not in_sq
            buf.append(ch)
            i += 1
            continue
        if ch == ";" and not in_dollar and not in_sq:
            if "".join(buf).strip():
                out.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if "".join(buf).strip():
        out.append("".join(buf).strip())
    return out


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


def applied_names(con) -> dict[str, str]:
    con.execute(LEDGER) if hasattr(con, "execute") else None
    rows = con.execute("SELECT name, sha256 FROM schema_migrations").fetchall()
    return {r[0]: r[1] for r in rows}


def run_sqlite(d: Path, db: str) -> int:
    con = sqlite3.connect(db)
    con.executescript(LEDGER)
    done = {r[0]: r[1] for r in con.execute("SELECT name, sha256 FROM schema_migrations")}
    n = 0
    for f in sorted(d.glob("*.sql")):
        text = f.read_text()
        h = hashlib.sha256(text.encode()).hexdigest()[:16]
        if f.name in done:
            if done[f.name] != h:
                print("MIGRATION DRIFT: %s changed after being applied (%s != %s). Restore it or add a new "
                      "file; never edit an applied migration." % (f.name, done[f.name], h))
                return 2
            continue
        con.executescript(text)
        con.execute("INSERT INTO schema_migrations (name, sha256, applied_ms) VALUES (?,?,?)",
                    (f.name, h, _now_ms()))
        con.commit()
        print("applied", f.name)
        n += 1
    print("sqlite: %d applied, %d already present" % (n, len(done)))
    return 0


def run_postgres(d: Path, url: str) -> int:
    try:
        import asyncpg
    except ImportError:
        print("asyncpg not installed; Postgres migrations must run via `make migrate` in the image that has "
              "it (or CI).")
        return 3
    import asyncio

    async def go() -> int:
        con = await asyncpg.connect(url)
        try:
            await con.execute(LEDGER.replace("BIGINT", "BIGINT"))
            done = {r["name"]: r["sha256"] for r in await con.fetch(
                "SELECT name, sha256 FROM schema_migrations")}
            n = 0
            for f in sorted(d.glob("*.sql")):
                text = f.read_text()
                h = hashlib.sha256(text.encode()).hexdigest()[:16]
                if f.name in done:
                    if done[f.name] != h:
                        print("MIGRATION DRIFT: %s" % f.name)
                        return 2
                    continue
                for stmt in split_statements(text):
                    await con.execute(stmt)
                await con.execute("INSERT INTO schema_migrations (name, sha256, applied_ms) VALUES ($1,$2,$3)",
                                  f.name, h, _now_ms())
                print("applied", f.name)
                n += 1
            print("postgres: %d applied, %d already present" % (n, len(done)))
            return 0
        finally:
            await con.close()

    return asyncio.run(go())


NOW_EXPR = {
    "sqlite": "(CAST(strftime('%s','now') AS INTEGER) * 1000)",
    "postgres": "(FLOOR(EXTRACT(EPOCH FROM now()) * 1000))::bigint",
}


def expand_now(sql: str, engine: str) -> str:
    """Replace the seed's `{{NOW_MS}}` with the engine's own now-in-milliseconds.

    The DB clock, not Python's: `make seed` runs in a container whose idea of now can differ from the
    database's by seconds, and a book stamped against the wrong clock reads as stale (the freshness gate is
    3 s). One clock, everywhere, is the only version of this that does not need a comment later.
    """
    expr = NOW_EXPR[engine]
    if "{{NOW_MS}}" not in sql:
        return sql
    return sql.replace("{{NOW_MS}}", expr)


def run_file_sqlite(path: Path, db: str) -> int:
    con = sqlite3.connect(db)
    con.executescript(expand_now(path.read_text(), "sqlite"))
    con.commit()
    con.close()
    print("applied", path.name, "to", db)
    return 0


def run_file_postgres(path: Path, url: str) -> int:
    import asyncio

    try:
        import asyncpg
    except ImportError:
        print("asyncpg not installed; apply it with psql instead: psql $PGM_DB_URL -f", path)
        return 3

    async def go() -> int:
        con = await asyncpg.connect(url)
        try:
            for stmt in split_statements(expand_now(path.read_text(), "postgres")):
                await con.execute(stmt)
        finally:
            await con.close()
        print("applied", path.name, "to Postgres")
        return 0

    return asyncio.run(go())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="apply ONE .sql file (e.g. the generated db/seed.sql); no ledger, so it "
                                   "must be re-runnable on its own — which is why seed.sql uses ON CONFLICT "
                                   "DO NOTHING rather than relying on a migrations table")
    ap.add_argument("--dir", required=False)
    ap.add_argument("--url", default=os.environ.get("PGM_DB_URL", ""))
    ap.add_argument("--sqlite", action="store_true")
    ap.add_argument("--db", default=os.environ.get("PGM_DB_PATH", "var/polygm.db"))
    ap.add_argument("--check", action="store_true", help="report pending files, change nothing")
    a = ap.parse_args()
    if a.file:
        f = Path(a.file)
        if not f.is_absolute():
            f = Path(__file__).resolve().parent.parent / a.file
        if not f.is_file():
            print("no such file:", f)
            return 2
        if a.sqlite or not a.url:
            Path(a.db).parent.mkdir(parents=True, exist_ok=True)
            return run_file_sqlite(f, a.db)
        return run_file_postgres(f, a.url)
    if not a.dir:
        print("need --dir or --file")
        return 2
    d = Path(a.dir)
    if not d.is_absolute():
        d = Path(__file__).resolve().parent.parent / a.dir
    if not d.is_dir():
        print("no such migration dir:", d)
        return 2
    if a.check:
        print("%d migration files in %s" % (len(list(d.glob("*.sql"))), d.name))
        return 0
    if a.sqlite or not a.url:
        Path(a.db).parent.mkdir(parents=True, exist_ok=True)
        return run_sqlite(d, a.db)
    return run_postgres(d, a.url)


if __name__ == "__main__":
    sys.exit(main())
