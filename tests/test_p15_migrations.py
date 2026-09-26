"""P15 D3 — the migration rules, canaried.

A lint that has never been seen to fail is a lint nobody knows works. These tests take the checker's rules out of
the file reader (`tools/p15-migration-check.py::judge_sql`) and feed them synthetic migrations: each destructive or
non-additive shape must be caught, and each correct shape must pass. The repo's real migrations are checked by the
gate itself (`make migration-check`), not here.
"""
from __future__ import annotations

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("p15_migration_check", ROOT / "tools" / "p15-migration-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load()
APPEND_ONLY = {"fills", "tape_trades", "audit_log"}


class FakeDir:
    """A directory that contains exactly the migration file names given. Two of these replace `db/migrations`
    and `db/migrations-sqlite` so the twin and contract-name rules can be tested without writing files."""

    def __init__(self, names=()):
        self.names = set(names)

    def __truediv__(self, other):
        return FakeFile(other, self.names)


class FakeFile:
    def __init__(self, name, names):
        self.name = name
        self._names = names

    def exists(self):
        return self.name in self._names


def judge(sql: str, *, name="0099_new.sql", pg_names=("0099_new.sql",), sqlite_names=("0099_new.sql",),
          age_days=30, strict=True):
    return MOD.judge_sql(
        name, sql, append_only=APPEND_ONLY, strict_contract=strict,
        pg=FakeDir(pg_names), sqlite=FakeDir(sqlite_names),
        # a fake `git log --diff-filter=A`: epoch seconds, or None when the age cannot be established
        commit_time_of=(lambda _p: None) if age_days is None else (lambda _p: _now_minus(age_days)))


def _now_minus(days):
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).timestamp() - days * 86400


def failures(sql: str, **kw) -> list[str]:
    return judge(sql, **kw).failures


# --------------------------------------------------------------------------- additive rules

def test_a_plain_new_table_passes():
    assert failures("CREATE TABLE IF NOT EXISTS widgets (id TEXT PRIMARY KEY, note TEXT);") == []


def test_a_nullable_column_passes():
    assert failures("ALTER TABLE users ADD COLUMN IF NOT EXISTS nickname TEXT;") == []


def test_a_not_null_column_with_a_default_passes():
    assert failures("ALTER TABLE users ADD COLUMN IF NOT EXISTS region TEXT NOT NULL DEFAULT 'eu';") == []


def test_a_not_null_column_without_a_default_is_caught():
    out = failures("ALTER TABLE users ADD COLUMN IF NOT EXISTS region TEXT NOT NULL;")
    assert out and "nullable or defaulted" in out[0]


def test_a_rename_is_caught():
    out = failures("ALTER TABLE users RENAME COLUMN handle TO username;")
    assert out and "contract" in out[0]


# --------------------------------------------------------------------------- destructive rules

def test_an_unmarked_drop_is_caught():
    out = failures("DROP TABLE IF EXISTS referral_events;")
    assert out and "contract" in out[0]


def test_a_marked_drop_that_names_a_real_earlier_migration_passes():
    sql = "-- CONTRACT: 0021_referral_events\nDROP TABLE IF EXISTS referral_events;"
    assert failures(sql, pg_names={"0099_new.sql", "0021_referral_events.sql"}) == []


def test_a_marked_drop_naming_a_later_or_missing_migration_is_caught():
    sql = "-- CONTRACT: 0100_future\nDROP TABLE IF EXISTS referral_events;"
    out = failures(sql, pg_names={"0099_new.sql", "0100_future.sql"})
    assert out and "names an earlier migration" in out[0]


def test_a_contract_that_is_only_hours_old_is_caught():
    sql = "-- CONTRACT: 0021_referral_events\nDROP TABLE IF EXISTS referral_events;"
    out = failures(sql, pg_names={"0099_new.sql", "0021_referral_events.sql"}, age_days=0.4)
    assert out and "release old" in out[0]


def test_an_unverifiable_contract_age_is_noted_not_failed():
    sql = "-- CONTRACT: 0021_referral_events\nDROP TABLE IF EXISTS referral_events;"
    rep = judge(sql, pg_names={"0099_new.sql", "0021_referral_events.sql"}, age_days=None)
    assert rep.failures == [] and any("not verifiable" in n for n in rep.notes)


# --------------------------------------------------------------------------- index rules

def test_a_plain_index_on_a_big_table_is_caught():
    out = failures("CREATE INDEX fills_ts_ix ON fills (at_ms);")
    assert out and "CONCURRENTLY" in out[0]


def test_a_concurrent_index_without_the_marker_is_caught():
    out = failures("CREATE INDEX CONCURRENTLY fills_ts_ix ON fills (at_ms);")
    assert out and "NO-TRANSACTION" in out[0]


def test_a_concurrent_index_with_the_marker_passes():
    sql = "-- NO-TRANSACTION: CREATE INDEX CONCURRENTLY cannot run inside a transaction\n" \
          "CREATE INDEX CONCURRENTLY fills_ts_ix ON fills (at_ms);"
    assert failures(sql) == []


def test_a_plain_index_on_a_small_table_is_fine():
    assert failures("CREATE INDEX widgets_note_ix ON widgets (note);") == []


# --------------------------------------------------------------------------- append-only rules

def test_deleting_from_an_append_only_table_is_caught():
    out = failures("DELETE FROM fills WHERE at_ms < 0;")
    assert out and "append-only" in out[0]


def test_updating_an_append_only_table_is_caught():
    out = failures("UPDATE audit_log SET actor = 'x' WHERE id = 1;")
    assert out and "append-only" in out[0]


def test_writing_an_ordinary_table_is_fine():
    assert failures("UPDATE users SET region = 'eu' WHERE region IS NULL;") == []


# --------------------------------------------------------------------------- the twin

def test_a_missing_sqlite_twin_is_caught():
    out = failures("CREATE TABLE IF NOT EXISTS widgets (id TEXT PRIMARY KEY);", sqlite_names=set())
    assert out and "SQLite twin" in out[0]


def test_a_comment_mentioning_drop_table_does_not_fail_the_file():
    sql = "-- we deliberately do NOT DROP TABLE fills here; the DROP COLUMN shape is what we avoid\n" \
          "CREATE TABLE IF NOT EXISTS widgets (id TEXT PRIMARY KEY);"
    assert failures(sql) == []


def test_an_alter_in_a_comment_does_not_fail_the_file():
    sql = "-- ALTER TABLE users RENAME COLUMN handle TO username was the shape we rejected\n" \
          "ALTER TABLE users ADD COLUMN IF NOT EXISTS nickname TEXT;"
    assert failures(sql) == []
