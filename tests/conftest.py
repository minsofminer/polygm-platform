"""Shared fixtures for the stdlib-unittest suite.

Why unittest and not pytest: P04's acceptance is "make test runs". A suite that needs a third-party runner is
a suite that silently does not run in the environment where someone is debugging the money path. pytest is
still supported (it is in requirements-dev.txt) and discovers these files unchanged.
"""
from __future__ import annotations
import os
import re
import shutil
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
#
# P13 added the other half of that idea: the directory is REMOVED at the end of the run, and the runner
# clears directories left behind by dead processes. A full run writes a migrated database per API module
# (a few hundred megabytes in total), and `/tmp` on this box is a 1 GB tmpfs: without the cleanup the fifth
# `make test` of a session starts failing in ways that look like product failures - pytest exits 120 with an
# empty log, which is ENOSPC from `tempfile`, not a red test. `PGM_TEST_TMPDIR` moves the whole thing (CI sets
# it to a scratch volume).
# What a full run needs, and what is merely worth saying out loud. Measured, not guessed: ~190 migrated
# databases, 1-2 MB each, peak during `executescript` while SQLite holds a journal alongside the file.
TEMP_NEED_MB = 400
TEMP_WARN_MB = 800


def _need_mb() -> int:
    """The refusal threshold, overridable so the refusal can be rehearsed.

    `PGM_TEST_TMP_MIN_MB=999999 make test` raises the same error a full disk raises, on a box with room,
    which is the only way to check that the message a person would actually see is the one intended.
    """
    return int(os.environ.get("PGM_TEST_TMP_MIN_MB") or TEMP_NEED_MB)


def temp_space(base: Path) -> tuple[int, str]:
    """(free MB, "ok" | "warn" | "full") for the filesystem the suite's databases will be written to.

    This exists because the failure it describes is unreadable. On 2026-09-25 a gate run reported
    `suite: 1272 tests, exit 1, tail 'FAILED (errors=33)'` — 1272 of the suite's ~1460 tests, and a tally with no
    cause in it. Every one of the 33 was `sqlite3.OperationalError: database or disk is full` from
    `apply_schema`: `/tmp` on this box is a 1 GB tmpfs, a previous suite run had been killed before its
    cleanup ran, and 336 MB of dead databases were still sitting there. The suite looked like it had a
    product bug; it was out of disk, and nothing said so. Refuse now, with the number in the message.
    """
    free_mb = shutil.disk_usage(str(base)).free // (1024 * 1024)
    if free_mb < _need_mb():
        return free_mb, "full"
    return free_mb, "warn" if free_mb < TEMP_WARN_MB else "ok"


def _tmp_root() -> Path:
    base = Path(os.environ.get("PGM_TEST_TMPDIR") or tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    for stale in base.glob("polygm-tests-*"):
        pid = stale.name.split("-")[2] if len(stale.name.split("-")) > 2 else ""
        if not pid.isdigit() or not Path("/proc/%s" % pid).exists():
            shutil.rmtree(stale, ignore_errors=True)
    free_mb, verdict = temp_space(base)
    if verdict == "full":
        raise RuntimeError(
            "the test suite cannot run: %d MB free on %s, and a full run writes ~360 MB of migrated "
            "databases. This is not a red test - it is ENOSPC, which unittest reports as dozens of "
            "`database or disk is full` errors. Free space, or send the databases somewhere with room: "
            "`PGM_TEST_TMPDIR=/scratch make test`." % (free_mb, base))
    if verdict == "warn":
        print("conftest: %d MB free on %s - a full run peaks around 360 MB; consider PGM_TEST_TMPDIR"
              % (free_mb, base), file=sys.stderr)
    return base


_TMP = Path(tempfile.mkdtemp(prefix="polygm-tests-%d-" % os.getpid(), dir=str(_tmp_root())))


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


def pytest_sessionfinish(session, exitstatus):                      # pragma: no cover - runner plumbing
    """Delete this run's database directory. The pid in the name makes it ours to remove."""
    shutil.rmtree(_TMP, ignore_errors=True)
