"""Shared fixtures for the stdlib-unittest suite.

Why unittest and not pytest: P04's acceptance is "make test runs". A suite that needs a third-party runner is
a suite that silently does not run in the environment where someone is debugging the money path. pytest is
still supported (it is in requirements-dev.txt) and discovers these files unchanged.
"""
from __future__ import annotations
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "packages"), str(ROOT / "services" / "api"), str(ROOT / "services" / "executor-mock"),
          str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

# An isolated directory PER PROCESS, never the repo's var/. The pid is in the name on purpose: an earlier
# version keyed only on the test name, so a second `make test` in the same workspace reopened the FIRST
# run's database file. Every symptom was a 409 IDEM_IN_PROGRESS on a brand-new idempotency key - the suite
# reporting a bug in its own fixture as a bug in the product. Tests must not inherit state from a previous
# process, and a test that mutates the dev database makes "works locally" meaningless for the next person.
_TMP = Path(tempfile.mkdtemp(prefix="polygm-tests-%d-" % os.getpid()))


def tmp_db_path(name: str) -> str:
    return str(_TMP / (re.sub(r"[^a-zA-Z0-9_.-]", "-", name) + ".db"))


def apply_schema(db_path: str) -> None:
    """Migrate a database through the SAME code the developer runs (`tools/run-sql.py --sqlite`).

    The API no longer migrates at import, so the suite must do what `make migrate` does. Calling the real
    migrator rather than executescript-ing the files again is the point: if the ledger logic in run-sql.py
    breaks, the API tests go red with it, instead of the suite quietly holding a schema the app cannot boot
    against.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("pgm_run_sql", ROOT / "tools" / "run-sql.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = mod.run_sqlite(ROOT / "db" / "migrations-sqlite", db_path)
    if rc != 0:
        raise RuntimeError("tools/run-sql.py --sqlite failed with %d" % rc)


def import_app(name: str):
    """Import a fresh copy of the API module bound to its own SQLite file."""
    os.environ["PGM_DB_PATH"] = tmp_db_path(name)
    apply_schema(os.environ["PGM_DB_PATH"])
    for mod in ("app", "seed"):
        sys.modules.pop(mod, None)
    try:
        import app as app_mod                                # noqa: F401 - imported by name
    except ModuleNotFoundError as e:                          # a missing third-party dep is not a product bug
        raise ModuleNotFoundError(
            "%s — the API tests need the HTTP layer installed: `pip install -r requirements.txt` "
            "(or run `make doctor`, which prints exactly which tools are absent in this environment)" % e
        ) from e
    import seed
    seed.seed_sqlite(os.environ["PGM_DB_PATH"])
    app_mod.STORE._loaded_ms = 0                             # force a flag read so boot defaults clear
    return app_mod


def refresh_flags(app_mod) -> None:
    app_mod.STORE._loaded_ms = 0
    app_mod.STORE.current()


class CoreTestCase(unittest.TestCase):
    def mk(self, *, now_ms: int = 1_700_000_000_000) -> int:
        return now_ms
