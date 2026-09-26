#!/usr/bin/env python3
"""Capture one `/v1/admin/metrics` read into the sample the alarm and dashboard checks evaluate against.

    python3 tools/p15-capture-metrics.py --seed --out docs/verification/p15-metrics-sample.json
    python3 tools/p15-capture-metrics.py --api https://api.polygm.trade --token "$PGM_ADMIN_TOKEN"

Why a captured payload rather than a hand-written fixture: the registry's `--check` fails when a rule names a
field the payload does not have, and that check is only worth anything if the payload is the real shape. A
hand-written fixture would pass forever while the endpoint drifted underneath it.

`--seed` writes a small, complete world first — two feeds (one healthy, one quiet), a heartbeat, a stuck deposit,
one withdrawal in flight and a builder-code row — because an empty database produces an empty payload, and an
empty payload is exactly the case where a missing-field check silently passes.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "verification" / "p15-metrics-sample.json"


def seed(db: pathlib.Path) -> dict:
    now = int(time.time() * 1000)
    con = sqlite3.connect(str(db))
    try:
        con.execute("DELETE FROM ingest_cursors WHERE source LIKE 'ws.%' OR source LIKE 'gamma.%'"
                    " OR source LIKE 'data.%'")
        con.execute("DELETE FROM executor_state")
        con.execute("DELETE FROM deposits WHERE credit_key LIKE 'k-sample-%'")
        con.executemany(
            "INSERT INTO ingest_cursors (source, cursor_json, last_event_ms, last_frame_ms, state, updated_ms)"
            " VALUES (?,?,?,?,?,?)",
            [("ws.tape", '{"resyncs": 1, "fetched": 512}', now - 900, now - 400, "ok", now),
             ("ws.book", '{"resyncs": 0}', now - 1_200, now - 900, "ok", now),
             ("data.trades", '{"from_ms": 0, "fetched": 42}', now - 30_000, now - 5_000, "lagging", now),
             ("gamma.markets", '{}', now - 60_000, now - 60_000, "lagging", now),
             # a feed that has never delivered a frame: the state the payload must never render as fresh
             ("clob.book", '{}', 0, 0, "down", now)])
        con.execute("INSERT INTO executor_state (id, at_ms, pid, version, ticks, note) VALUES (1,?,?,?,?,?)",
                    (now - 2_000, str(os.getpid()), "sample", 128, ""))
        con.execute("INSERT INTO deposits (user_id, asset, chain, amount_micro, credit_key, status,"
                    " first_seen_ms) VALUES (?,?,?,?,?,?,?)",
                    ("u-demo", "USDC", "base", 5_000_000, "k-sample-1", "confirming", now - 1_800_000))
        con.execute("INSERT INTO builder_code_status (code, state, last_seen_ms, changed_ms, reject_count,"
                    " source, note) VALUES (?,?,?,?,?,?,?)",
                    ("polygm-sample", "active", now, now - 86_400_000, 0, "api", ""))
        con.commit()
    finally:
        con.close()
    return {"seeded": str(db)}


def capture_from_app(db: pathlib.Path, token: str) -> dict:
    """Read the endpoint through the app itself: the same code path a phone hits, with no server to start."""
    sys.path[:0] = [str(ROOT / "packages"), str(ROOT / "services" / "api"),
                    str(ROOT / "services" / "executor-mock"), str(ROOT / "tools")]
    os.environ["PGM_DB_PATH"] = str(db)
    os.environ["PGM_ADMIN_TOKEN"] = token
    from fastapi.testclient import TestClient                        # noqa: PLC0415
    import app as A                                                  # noqa: PLC0415
    client = TestClient(A.app, raise_server_exceptions=True)
    r = client.get("/v1/admin/metrics", headers={"x-admin-token": token})
    if r.status_code != 200:
        raise SystemExit("metrics read failed: %s %s" % (r.status_code, r.text[:300]))
    return r.json()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="capture the metrics payload the alarm checks evaluate against")
    ap.add_argument("--db", default=os.environ.get("PGM_DB_PATH") or str(ROOT / "var" / "polygm.db"))
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--token", default=os.environ.get("PGM_ADMIN_TOKEN") or "sample-token-sample-token-sample-token")
    ap.add_argument("--seed", action="store_true", help="write a small complete world before capturing")
    ap.add_argument("--api", default="")
    a = ap.parse_args(argv)

    if a.api:
        import urllib.request                                            # noqa: PLC0415
        req = urllib.request.Request(a.api.rstrip("/") + "/v1/admin/metrics",
                                    headers={"x-admin-token": a.token, "accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:            # noqa: S310 - the operator's own URL
            payload = json.loads(resp.read().decode())
    else:
        db = pathlib.Path(a.db)
        if not db.exists():
            print("no database at %s — run `make migrate` first" % db, file=sys.stderr)
            return 2
        if a.seed:
            print(json.dumps(seed(db)))
        payload = capture_from_app(db, a.token)

    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    body = payload.get("data", payload)
    print("captured %s" % out)
    print("  blocks: %s" % ", ".join(sorted(str(k) for k in body if not str(k).startswith("asOf"))))
    feeds = (body.get("freshness") or {}).get("feeds") or []
    print("  feeds: %d (%d silent, %d lagging)" % (len(feeds), len((body.get("freshness") or {}).get("silent") or []),
                                                   len((body.get("freshness") or {}).get("lagging") or [])))
    print("  executor: %s" % json.dumps(body.get("executor") or {})[:120])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
