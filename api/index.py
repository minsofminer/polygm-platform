"""The API as a Vercel Python function — the entry for the `polygm-api` project.

Why this file exists, and what it is not:

The API is written to run as a long-lived process (`uvicorn app:app`, a Dockerfile, a warm connection pool). Vercel
runs functions: a cold start per instance, a read-only filesystem apart from `/tmp`, and no process to keep warm. So
this module does the three things a long-lived host would have done for us, and does them **once per cold start**:

  1. **Puts the repo on `sys.path`** — the app imports `polygm_core` from `packages/` and `seed` from `services/api/`,
     which is how the Dockerfile's `PYTHONPATH` reads too. Vercel's runtime does not read that Dockerfile.
  2. **Migrates and seeds a database in `/tmp`** — the `db/migrations-sqlite/` ledger is the same one `make migrate`
     and the test suite run, not a second schema invented for the demo. Seeding is idempotent (`INSERT OR IGNORE`)
     and guarded by a marker file, because every cold start would otherwise re-run 19 migrations and a fixture load
     before answering the first request.
  3. **Exports `app`** — the ASGI application, unchanged. Every route, every auth level, every refusal code is the
     same object the local `uvicorn` serves. The point of deploying this is to prove the *product* works end to end,
     not to build a demo-shaped subset of it.

**What this deployment is honest about.** Two consequences of the filesystem follow from `/tmp` being per-instance:
state written in one request may not be visible to the next one if Vercel routes it to a different instance, and an
instance that recycles loses it. Wallets, orders and sessions therefore live in a demo database: the markets and their
fixtures are stable, the *records* are disposable. That is acceptable for what this deployment is — a live backend so
the Mini App can be opened and used, before P13/P14 where real funds are gated — and it is written down here rather
than discovered by someone wondering why their demo account forgot them. The real deployment keeps Postgres, which is
exactly the thing the SQLite twin has always been a dev stand-in for.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

# The deployment places the whole repo at /var/task, so the two import roots are relative to this file.
ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "packages", ROOT / "services" / "api", ROOT / "tools"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

# The database lives in the one directory a Vercel function may write to. `PGM_DB_PATH` has to be set BEFORE the app
# module is imported: `app.py` resolves it once at import and every later connection uses that value, deliberately, so
# a process cannot end up with requests reading a different file than the one it migrated.
_DB = Path("/tmp/polygm-demo.db")
os.environ.setdefault("PGM_DB_PATH", str(_DB))


def _boot(path: Path) -> None:
    """Migrate and seed `path`, once per cold start, and never in the request path of a warm instance.

    The marker is a file rather than a module-level boolean because the interesting failure is not "we ran this
    twice in one process" — that cannot happen — but "a second instance started against a `/tmp` that already holds a
    migrated database", where re-running the ledger would be wasted work on every cold start.
    """
    marker = path.with_suffix(".ready")
    if marker.exists() and path.exists():
        return
    import importlib.util

    spec = importlib.util.spec_from_file_location("pgm_run_sql", ROOT / "tools" / "run-sql.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    rc = mod.run_sqlite(ROOT / "db" / "migrations-sqlite", str(path))
    if rc != 0:
        raise RuntimeError("migrations failed with %d" % rc)
    import seed

    seed.seed_sqlite(str(path))
    # The security plane's own tables are separate from the product seed, and the Mini App's session route needs them
    # (a session row, an identity row). Same reasoning as the migrations: use the repo's code, not a copy.
    if (ROOT / "tools" / "seed-security.py").exists():
        security = importlib.util.spec_from_file_location("pgm_seed_security", ROOT / "tools" / "seed-security.py")
        sec_mod = importlib.util.module_from_spec(security)
        assert security.loader is not None
        security.loader.exec_module(sec_mod)
        for name in ("seed", "main", "run"):
            fn = getattr(sec_mod, name, None)
            if callable(fn):
                try:
                    fn()
                except TypeError:                     # a CLI that takes argv; the demo has none to give it
                    pass
                break
    marker.write_text("ok", encoding="utf-8")


_boot(_DB)

# Imported after the boot so the module the app resolves `_DB_PATH` from sees the database that exists.
from app import app  # noqa: E402
