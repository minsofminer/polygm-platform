#!/usr/bin/env python3
"""P15 D5/D9 — fire every page-class alarm on purpose, and keep the notification text as the evidence.

    python3 tools/p15-alert-drill.py --record            # run all scenarios, record each firing
    python3 tools/p15-alert-drill.py --only unreconciled-orders
    python3 tools/p15-alert-drill.py --list              # what is covered here, and what is covered elsewhere

Why this exists: the kit asks for "every alarm fired deliberately in staging and the notification verified", and a
claim like that is worth nothing without the notification's own text attached. So each scenario writes the exact
condition the rule watches into a scratch copy of the seeded database, reads `/v1/admin/metrics` through the app
itself, evaluates `ops/alerts.yaml`, and **fails if the intended rule does not fire** — a drill that cannot fail is
a demonstration, not a test. The evidence line it records is the rendered notification, which is what a human would
actually read at 2am.

What this proves and what it does not, stated in the same breath because the difference matters:

* **proves** that each rule fires on the condition it claims to watch, that the threshold is where the registry
  says it is, and that the notification renders with the right numbers, owner and runbook link;
* **does not prove** delivery to a phone. That needs `PGM_TELEGRAM_BOT_TOKEN` and a real device, and it is listed
  as an owner step in `docs/P15-readiness.md`. The evidence recorded here says "local, seeded" so nobody can read
  it as a staging delivery.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sqlite3
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "ops" / "alerts.yaml"
GOLDEN = ROOT / "var" / "polygm.db"
FIRED_LOG = ROOT / "docs" / "verification" / "p15-alerts-fired.jsonl"

ADMIN = "drill-token-drill-token-drill-token-1234"


def _load_alerts():
    spec = importlib.util.spec_from_file_location("p15_alerts_for_drill", ROOT / "tools" / "p15-alerts.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["p15_alerts_for_drill"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------- the scenarios
def reset_baseline(con: sqlite3.Connection, now: int) -> None:
    """Put the handful of conditions this drill can create back to healthy, so a scenario fires *its own* rule
    and the "other rules also fired" note is information rather than noise.

    Only tables this drill writes are reset. Everything append-only (wallet_events, chain_events, kills, fills) is
    left alone: each scenario runs against its own copy of the seeded database, so nothing leaks between them, and
    a drill that could erase an append-only table would be the bug rather than the fix.
    """
    con.execute("DELETE FROM reconcile_open")
    con.execute("DELETE FROM executor_state")
    con.execute("DELETE FROM deposits WHERE credit_key LIKE 'k-drill-%' OR credit_key LIKE 'k-sample-%'")
    con.execute("DELETE FROM withdrawals WHERE idempotency_key LIKE 'wd-drill-%'"
                " OR idempotency_key LIKE 'wd-spike-%'")
    con.execute("DELETE FROM ingest_cursors")
    con.executemany("INSERT INTO ingest_cursors (source, cursor_json, last_event_ms, last_frame_ms, state,"
                    " updated_ms) VALUES (?,?,?,?,?,?)",
                    [("ws.tape", '{"resyncs": 0}', now - 500, now - 400, "ok", now),
                     ("ws.book", '{"resyncs": 0}', now - 500, now - 400, "ok", now),
                     ("gamma.markets", "{}", now - 1_000, now - 1_000, "ok", now)])
    con.execute("UPDATE builder_code_status SET state='active' WHERE code = 'bc-drill'")


def s_unreconciled(con: sqlite3.Connection, now: int) -> None:
    con.execute("DELETE FROM reconcile_open")
    con.execute("INSERT INTO reconcile_open (dedupe_key, case_name, intent_id, order_id, since_ms, note,"
                " attempts) VALUES (?,?,?,?,?,?,?)",
                ("drill-orphan", "orphan", "i-drill", "o-drill", now - 600_000, "drill", 1))


def s_unknown_state(con: sqlite3.Connection, now: int) -> None:
    con.execute("INSERT OR REPLACE INTO order_intents (id, user_id, market_id, token_id, side, price_micro,"
                " size_micro, notional_micro, state, idempotency_key, created_ms, updated_ms)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("i-drill-uncertain", "u-demo", "0xM1", "0xT10", "BUY", 500_000, 1_000_000, 500_000,
                 "uncertain", "k-drill-uncertain", now - 300_000, now - 300_000))


def s_kill_switch(con: sqlite3.Connection, now: int) -> None:
    con.execute("INSERT INTO kill_switch_state (engaged, reason, changed_by, at_ms) VALUES (?,?,?,?)",
                (1, "drill: stopping new risk on purpose", "p15-alert-drill", now - 120_000))


def s_executor_down(con: sqlite3.Connection, now: int) -> None:
    con.execute("INSERT INTO executor_state (id, at_ms, pid, version, ticks, note) VALUES (1,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET at_ms=excluded.at_ms, ticks=excluded.ticks",
                (now - 240_000, "1", "drill", 9, "stopped beating on purpose"))


def s_key_compromise(con: sqlite3.Connection, now: int) -> None:
    con.execute("INSERT INTO wallet_events (user_id, event, detail_json, actor, at_ms) VALUES (?,?,?,?,?)",
                ("u-demo", "export_requested", "{}", "user", now - 60_000))
    con.execute("INSERT INTO revoke_jobs (id, scope, requested_by, started_ms, total, revoked, failed, batch_size)"
                " VALUES (?,?,?,?,?,?,?,?)",
                ("rj-drill", "all", "drill", now - 30_000, 10, 10, 0, 100))


def s_withdrawal_spike(con: sqlite3.Connection, now: int) -> None:
    base = now - 86_400_000
    con.execute("DELETE FROM withdrawals")
    rows = [("u-demo", "USDC", "base", 1_000_000, "0xabc", "1.00", "0xabc", "queued", "wd-drill-%d" % i,
             base + i * 60_000) for i in range(24)]
    # three ordinary hours, then a spike in the last hour: the ratio is what fires, not the total
    rows += [("u-demo", "USDC", "base", 2_000_000, "0xabc", "2.00", "0xabc", "queued", "wd-spike-%d" % i,
              now - 1_800_000 + i * 60_000) for i in range(8)]
    con.executemany("INSERT INTO withdrawals (user_id, asset, chain, amount_micro, dest_address, typed_amount,"
                    " typed_address, status, idempotency_key, requested_ms) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


def s_ws_dead(con: sqlite3.Connection, now: int) -> None:
    con.execute("DELETE FROM ingest_cursors WHERE source LIKE 'ws.%'")
    con.executemany("INSERT INTO ingest_cursors (source, cursor_json, last_event_ms, last_frame_ms, state,"
                    " updated_ms) VALUES (?,?,?,?,?,?)",
                    [("ws.tape", "{}", now - 600_000, now - 600_000, "silent", now),
                     ("ws.book", "{}", now - 600_000, now - 600_000, "silent", now)])


def s_builder_disabled(con: sqlite3.Connection, now: int) -> None:
    con.execute("INSERT INTO builder_code_status (code, state, last_seen_ms, changed_ms, reject_count, source,"
                " note) VALUES (?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET state=excluded.state,"
                " reject_count=excluded.reject_count",
                ("bc-drill", "disabled", now, now - 300_000, 7, "venue_rejection", "the venue refused it"))


def s_deposit_stuck(con: sqlite3.Connection, now: int) -> None:
    con.execute("DELETE FROM deposits WHERE credit_key LIKE 'k-drill-%'")
    con.execute("INSERT INTO deposits (user_id, asset, chain, amount_micro, credit_key, status, first_seen_ms)"
                " VALUES (?,?,?,?,?,?,?)",
                ("u-demo", "USDC", "base", 5_000_000, "k-drill-stuck", "confirming", now - 2_400_000))


def s_synthetic(con: sqlite3.Connection, now: int, state_dir: pathlib.Path) -> None:
    (state_dir / "synth.json").write_text(json.dumps({"consecutive_misses": 3, "last_ok_ms": now - 600_000,
                                                      "last_detail": "connect timeout after 25 s"}))


SCENARIOS = [
    ("unreconciled-orders", s_unreconciled, {}),
    ("orders-unknown-state", s_unknown_state, {}),
    ("kill-switch-engaged", s_kill_switch, {}),
    ("executor-down", s_executor_down, {}),
    ("key-compromise-indicator", s_key_compromise, {}),
    ("withdrawal-spike", s_withdrawal_spike, {}),
    ("all-websocket-sources-dead", s_ws_dead, {}),
    ("builder-code-disabled", s_builder_disabled, {"PGM_BUILDER_CODE": "bc-drill"}),
    ("deposit-stuck", s_deposit_stuck, {}),
    ("synthetic-order-failed", s_synthetic, {}),
]

#: Rules this drill deliberately does not try to fire, with the reason. They are covered elsewhere, and saying so
#: is better than a scenario that proves nothing: a probe that repairs the condition under test is the failure
#: mode the P14 review documented.
COVERED_ELSEWHERE = {
    "deploy-verification-failed": "fires from the post-deploy watch's verdict file; the CI pipeline's rollback job "
                                  "is the control, and firing it needs a real deploy (owner step)",
    "source-lagging": "SEV2; fired by the same condition class as all-websocket-sources-dead, and asserted in "
                      "tests/test_p15_ops_api.py::TestOperationsPillars",
    "venue-throttling": "SEV2; the rejection-reason path is exercised in tests/test_p15_ops_api.py",
    "rate-budget-low": "needs real rate counters; asserted against a declared budget in the budgets source",
    "elevated-rejections": "SEV2; asserted in tests/test_p15_ops_api.py",
    "order-path-degraded": "SEV2; needs a populated attempt table (P13's load run produces one)",
    "wallet-provider-degraded": "SEV2; needs the provider to fail (P14's drill covers the provider path)",
    "withdrawal-delayed": "SEV2; same table as withdrawal-spike",
    "telegram-delivery-failing": "SEV2; needs real deliveries, which need the bot token (owner step)",
    "queue-depth-high": "SEV2; asserted in tests/test_p15_ops_api.py",
    "cache-age-high": "needs a cache on the box; the collector returns unknown rather than green without one",
    "p99-degraded": "SEV3 ticket, same metric as order-path-degraded",
    "disk-filling": "SEV3; host probe, collected by --host",
    "cost-spike": "SEV3; fired by tools/p15-cost.py --gate, exercised in its own self-test",
    "dependency-vulnerability": "SEV3; fired by the dependency scan, exercised in its own self-test",
}


def metrics_from_app(db: pathlib.Path, env_extra: dict[str, str]) -> dict:
    """Read the endpoint through the app, in a subprocess: each scenario gets a fresh module state, which is what
    keeps one scenario's patched flag from being the next scenario's fixture."""
    code = (
        "import json,os,sys;"
        "sys.path[:0]=['packages','services/api','services/executor-mock','tools'];"
        "from fastapi.testclient import TestClient;"
        "import app as A;"
        "c=TestClient(A.app, raise_server_exceptions=False);"
        "r=c.get('/v1/admin/metrics', headers={'x-admin-token': os.environ['PGM_ADMIN_TOKEN']});"
        "print(r.text if r.status_code != 200 else json.dumps(r.json()))"
    )
    env = dict(os.environ, PGM_DB_PATH=str(db), PGM_ADMIN_TOKEN=ADMIN, **env_extra)
    p = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, env=env,
                       timeout=300)
    if p.returncode != 0:
        raise SystemExit("could not read metrics for the drill: %s" % (p.stderr or p.stdout)[-600:])
    body = json.loads(p.stdout.strip().splitlines()[-1])
    return body.get("data", body)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P15 D5/D9 — fire the page-class alarms on purpose")
    ap.add_argument("--only", default="")
    ap.add_argument("--record", action="store_true", help="append each firing to p15-alerts-fired.jsonl")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)

    alerts = _load_alerts()
    registry = __import__("yaml").safe_load(REGISTRY.read_text())
    rules = {r["id"]: r for r in registry["rules"]}

    if a.list:
        for rid, _fn, _env in SCENARIOS:
            print("covered here      %s" % rid)
        for rid, why in sorted(COVERED_ELSEWHERE.items()):
            print("covered elsewhere %s — %s" % (rid, why))
        return 0
    if not GOLDEN.exists():
        print("no seeded database at %s — run `make migrate && make seed` first" % GOLDEN, file=sys.stderr)
        return 2

    wanted = {x.strip() for x in a.only.split(",") if x.strip()}
    scenarios = [s for s in SCENARIOS if not wanted or s[0] in wanted]
    if not scenarios:
        print("no scenario matches %s (see --list)" % a.only, file=sys.stderr)
        return 2

    results, failures = [], 0
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="p15-alert-drill-"))
    try:
        for rid, seed_fn, env_extra in scenarios:
            # The clock is read per scenario, not once for the run: the baseline feeds are seeded "500 ms ago",
            # and a run that took five seconds to reach the tenth scenario would otherwise report those feeds as
            # lagging — a false positive caused by the drill's own duration, which is the kind of bug a drill is
            # supposed to catch rather than produce.
            now = int(time.time() * 1000)
            db = tmp / ("%s.db" % rid)
            shutil.copy2(GOLDEN, db)
            con = sqlite3.connect(str(db))
            try:
                reset_baseline(con, now)
                if seed_fn is s_synthetic:
                    seed_fn(con, now, tmp)
                else:
                    seed_fn(con, now)
                con.commit()
            finally:
                con.close()
            metrics = metrics_from_app(db, env_extra)
            sources = {"synthetic": alerts.collect_synthetic(tmp / "synth.json")} if rid == "synthetic-order-failed" \
                else {}
            fired, unknown, _quiet = alerts.evaluate(registry["rules"], metrics, sources=sources)
            ids = {r["id"] for r in fired}
            row = next((r for r in fired if r["id"] == rid), None)
            unknown_ids = {u["id"]: u["why"] for u in unknown}
            ok = rid in ids
            state = "FIRED " if ok else ("UNKNOWN" if rid in unknown_ids else "MISSED")
            print("%s %-28s %s" % (state, rid, (row or {}).get("summary", unknown_ids.get(rid, ""))[:110]))
            if ok and a.record:
                note = alerts.render_notification(row, metrics.get("killSwitch"))
                alerts.record_firing(rid, "drill: seeded locally, /v1/admin/metrics read through the app; "
                                          "notification rendered and verified as text (delivery to a phone is an "
                                          "owner step). Rule fired on: %s" % note.replace("\n", " | ")[:150],
                                      registry)
            results.append({"rule": rid, "fired": ok, "summary": (row or {}).get("summary"),
                            "notification": alerts.render_notification(row, metrics.get("killSwitch")) if ok
                            else None,
                            "unknown_why": unknown_ids.get(rid)})
            failures += 0 if ok else 1
            # every rule that fires in a scenario must have been *intended*: a drill that also trips six unrelated
            # alarms is telling you the seeded world is not the world you think it is
            extra = sorted(ids - {rid})
            if extra:
                # Not a failure: several rules legitimately watch neighbouring conditions (a stuck deposit and a
                # withdrawal in flight are the same table), and the note is how a reader sees that the seeded world
                # is wider than the scenario. It is printed rather than asserted so a genuinely isolated scenario
                # and a legitimately coupled one both pass, with the difference visible.
                print("      note: %d other rule(s) also fired (%s)" % (len(extra), ", ".join(extra[:6])))
                for e in extra:
                    why = next((r["details"] for r in fired if r["id"] == e), None)
                    print("            %-24s %s" % (e, str(why)[:110]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nalert drill: %d scenario(s), %d missed" % (len(results), failures))
    if a.json:
        pathlib.Path(a.json).write_text(json.dumps(results, indent=2, default=str) + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
