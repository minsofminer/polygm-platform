#!/usr/bin/env python3
"""Report which tools exist here, and — the useful part — what their absence MEANS for the checks.

This exists because "make dev" failing with `docker: command not found` is a different fact from "the P04
stack is broken", and a builder who cannot tell them apart will "fix" the wrong thing. Every line is a
measured probe, not a guess.
"""
import importlib.util
import shutil
import sqlite3
import subprocess
import sys

MISSING_OK = {
    "docker": "compose targets are skipped; `make test` runs the same service code on the portable "
              "SQLite engine. [UNVERIFIED] here: container wiring.",
    "docker-compose": "same as docker.",
    "psql": "db/migrations/*.sql are still validated statically by tools/p04-gate-check.py; `make migrate` "
            "against Postgres must run in CI or on a machine that has it.",
    "sqlite3": "the sqlite3 CLI is absent but the stdlib module works — that is what the tests use.",
    "mypy": "`make typecheck` prints [skip] instead of pretending to pass.",
    "node": "web/ build steps are skipped; brand/tokens tooling is python-only.",
}


def main() -> int:
    rows = []
    for t in ("docker", "docker-compose", "psql", "sqlite3", "python3", "node", "npm", "mypy", "pytest"):
        p = shutil.which(t)
        rows.append((t, p or "MISSING"))
    print("tooling (measured, not assumed):")
    for t, v in rows:
        print("  %-14s %s" % (t, v))
        if v == "MISSING" and t in MISSING_OK:
            print("  %-14s   -> %s" % ("", MISSING_OK[t]))
    print("python          %s" % sys.version.split()[0])
    print("sqlite stdlib   %s" % sqlite3.sqlite_version)
    for mod in ("fastapi", "uvicorn", "pydantic", "yaml", "httpx", "asyncpg", "redis"):
        print("  %-14s %s" % (mod, "present" if importlib.util.find_spec(mod) else "MISSING"))
    r = subprocess.run([sys.executable, "-c", "import app"], capture_output=True, text=True,
                       env={"PYTHONPATH": "packages:services/api", "PATH": "/usr/bin:/bin"},
                       cwd=".")
    print("import app      %s" % ("ok" if r.returncode == 0 else "FAILED: " + r.stderr.strip()[-200:]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
