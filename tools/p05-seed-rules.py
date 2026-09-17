#!/usr/bin/env python3
"""Seed `signal_rules` for one owner, through the same validator the daemon uses when it loads them.

Why this exists as a tool and not as a migration: an alert rule is user data, and nothing that ships a product
should invent rows in someone's alert table on upgrade. But the phase gate needs the *loaded* path exercised
against a real DB, and P09's UI needs a "restore defaults" action — both are the same list of rules, so it lives
here once.

Two rules this file follows on purpose:

* Every rule is passed through `polygm_core.signals.engine.build_rule` BEFORE the insert. A seed that writes a
  row the loader will reject produces a silent no-alert account, which is the exact failure this phase keeps
  finding: the row exists, the UI shows it, nothing fires. `--dry-run` runs the same validation and writes
  nothing, so the check is available without a DB.
* It writes `signal_rules` only. `alert_rules` (0004) is the product's rule row and P09 owns the pair-writing;
  a seed that wrote one and not the other would leave the UI and the daemon disagreeing about what a user
  subscribed to, which is worse than either being empty.

    python3 tools/p05-seed-rules.py --dry-run
    PGM_DB_PATH=var/polygm.db python3 tools/p05-seed-rules.py --owner demo
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages"))

from polygm_core.signals import engine as E                                        # noqa: E402

# The default set, and why each is here. `params_json` is EMPTY on purpose: `Rule.params` falls back to
# `DEFAULTS`, so a seeded rule tracks a threshold change in one place. A rule that copies the numbers into its
# own row is a rule that stays at last year's threshold forever, and "my alerts got noisy" has no other answer.
SEED: list[tuple[str, str, dict, tuple[str, ...]]] = [
    ("large_fill", "someone moved real size here", {}, ("push", "telegram")),
    ("volume_spike", "the tape is louder than this market's own history", {}, ("push",)),
    ("rapid_move", "the mid jumped further than the book can absorb", {}, ("push", "telegram")),
    ("imbalance_flip", "one side of the ladder just disappeared", {}, ("push",)),
    ("negrisk_divergence", "the legs of a neg-risk event stopped summing to 1", {}, ("telegram",)),
    ("resolution_imminent", "the clock, not the tape", {}, ("push",)),
]
SKIP_BY_DEFAULT = ("new_market", "watched_wallet")     # need tags/a wallet list; not derivable from a CLI


def build(owner: str, market: str | None) -> list[dict]:
    out = []
    for kind, why, params, channels in SEED:
        rid = "seed-%s-%s" % (kind, owner)
        rule = E.build_rule(rid, kind, params, market_filter=({"condition_id": market} if market else {}),
                            cooldown_s=E.DEFAULTS[kind]["cooldown_s"], channels=list(channels))
        out.append({"id": rid, "owner": owner, "kind": kind, "why": why, "rule": rule})
        # note: `enabled` is a literal in the INSERT, not a bound value, so it is absent from the tuple below
    return out


def rows_for(seeded: list[dict], now_ms: int) -> list[tuple]:
    return [(r["id"], r["owner"], r["kind"], json.dumps({}), json.dumps(r["rule"].market_filter),
             r["rule"].cooldown_s, r["rule"].severity_level, json.dumps(r["rule"].channels), now_ms, now_ms)
            for r in seeded]      # 10 values for the statement's 10 `?`, in that order


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.executescript("PRAGMA foreign_keys=ON;")
    return con


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=os.environ.get("PGM_DB_PATH") or str(ROOT / "var" / "polygm.db"))
    ap.add_argument("--owner", default="demo")
    ap.add_argument("--market", default=None, help="condition id to scope every rule to")
    ap.add_argument("--replace", action="store_true", help="delete this owner's other seed rows first")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    now_ms = int(time.time() * 1000)
    try:
        seeded = build(a.owner, a.market)
    except E.RuleError as e:
        print("REFUSED: a default rule failed its own validator: %s" % e)
        return 1

    if a.dry_run:
        # Validate the way the loader does, without a DB: build_rule is the same function, so "the dry run is
        # clean" means "the daemon will accept these", not "the strings look plausible".
        payload = [{"id": r["id"], "kind": r["kind"], "cooldown_s": r["rule"].cooldown_s,
                    "severity": r["rule"].severity_level, "channels": r["rule"].channels} for r in seeded]
        print(json.dumps(payload, indent=2) if a.json else
              "\n".join("  %-22s %-20s cd=%-5s %s" % (r["id"], r["kind"], r["rule"].cooldown_s, r["why"])
                        for r in seeded))
        print("dry run: %d rules validated, nothing written" % len(seeded))
        return 0

    con = connect(a.db)
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "signal_rules" not in have:
        print("REFUSED: %s has no `signal_rules` — run `make migrate` first (I will not create the schema from a"
              " seed script)." % a.db)
        return 2
    if a.replace:
        con.execute("DELETE FROM signal_rules WHERE id LIKE 'seed-%' AND owner=?", (a.owner,))
    con.executemany("INSERT INTO signal_rules (id,owner,kind,params_json,market_filter_json,cooldown_s,severity,"
                    "channels_json,enabled,created_ms,updated_ms) VALUES (?,?,?,?,?,?,?,?,1,?,?)"
                    " ON CONFLICT (id) DO UPDATE SET kind=excluded.kind,"
                    " market_filter_json=excluded.market_filter_json, cooldown_s=excluded.cooldown_s,"
                    " severity=excluded.severity, channels_json=excluded.channels_json, enabled=1,"
                    " updated_ms=excluded.updated_ms", rows_for(seeded, now_ms))
    con.commit()
    loaded, errors, _cd, _ow = None, None, None, None
    # Read them back through the loader: the only proof that matters is that a daemon starting now sees the same
    # rules and rejects none of them.
    rows = con.execute("SELECT id,owner,kind,params_json,market_filter_json,cooldown_s,severity,channels_json,"
                       "enabled FROM signal_rules WHERE owner=?", (a.owner,)).fetchall()
    rejected = []
    for rid, owner, kind, pj, mj, cd, sev, cj, enabled in rows:
        if not enabled:
            rejected.append("%s: written disabled, so the daemon will not load it" % rid)
            continue
        try:
            # `enabled` is deliberately NOT a build_rule argument: the loader filters on the column and builds
            # the rule from what is left, so passing it here would either be ignored or raise TypeError, and a
            # seed script that raises TypeError looks like a database problem.
            E.build_rule(rid, kind, json.loads(pj or "{}"), market_filter=json.loads(mj or "{}"),
                         cooldown_s=cd, severity=sev, channels=json.loads(cj or "[]"))
        except E.RuleError as e:
            rejected.append("%s: %s" % (rid, e))
    print(("wrote %d rules for %s; loader accepted %d" % (len(seeded), a.owner, len(rows) - len(rejected)))
          if not rejected else "LOADER REJECTED: " + "; ".join(rejected))
    return 1 if rejected else 0


if __name__ == "__main__":
    sys.exit(main())
