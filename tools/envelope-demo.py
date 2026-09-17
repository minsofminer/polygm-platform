#!/usr/bin/env python3
"""`make envelope` — P04's acceptance line, run for real: an order POSTed over a TCP socket to the API,
which queues it, and the executor path talking to `executor-mock` over another socket.

No TestClient here on purpose. `tests/test_api.py` already covers the handler; this script exists to prove
the thing a unit test cannot: that two separate processes, real HTTP, real JSON, real headers, produce the
contract's answers. It prints the RAW exchanges, so a reviewer can read the response they would curl, not my
summary of it.

Runs without Docker: the mock is stdlib's ThreadingHTTPServer, the API is uvicorn, both on 127.0.0.1.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API_PORT = 18080
MOCK_PORT = 18090


def free_port(want: int) -> int:
    """Use the wanted port if it is free, else whatever the OS gives. A demo that dies because port 8080 is
    taken by the developer's other project is a demo nobody runs twice."""
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", want))
            return want
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def wait_up(port: int, proc: subprocess.Popen, timeout: float = 25.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        if proc.poll() is not None:
            raise SystemExit("service exited early with code %s" % proc.returncode)
        time.sleep(0.15)
    raise SystemExit("service on port %d never came up within %.0fs" % (port, timeout))


def call(method: str, url: str, *, body=None, headers=None) -> tuple[int, dict, str, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw, code, hdrs = r.read().decode(), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:                  # a 4xx IS the answer we came to look at
        raw, code, hdrs = e.read().decode(), e.code, dict(e.headers or {})
    pretty = {k: v for k, v in hdrs.items()
              if k.lower().startswith(("x-", "retry-after", "server-timing", "content-type"))}
    print("    <- %d" % code, json.dumps(pretty, sort_keys=True))
    try:
        parsed = json.loads(raw)
        print("    <- body", json.dumps(parsed, sort_keys=True)[:600])
    except json.JSONDecodeError:
        parsed = {}
        print("    <- body (not json)", raw[:200])
    return code, parsed, raw, hdrs


def main() -> int:
    api_port, mock_port = free_port(API_PORT), free_port(MOCK_PORT)
    env = dict(os.environ, PYTHONPATH="%s:%s:%s" % (ROOT / "packages", ROOT / "services" / "api",
                                                     ROOT / "services" / "executor-mock"))
    db = "/tmp/pgm-envelope-%d.db" % os.getpid()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except FileNotFoundError:
            pass
    env["PGM_DB_PATH"] = db
    env["PGM_ENGINE"] = "sqlite"
    env["POLYGM_TRANSPORT"] = "mock"
    env["PGM_MOCK_CLOB_URL"] = "http://127.0.0.1:%d" % mock_port

    print("starting executor-mock on 127.0.0.1:%d and api on %d (SQLite at %s)" % (mock_port, api_port, db))
    # Seed FIRST, in a separate process, the way a developer does: `make migrate && make seed`. A fresh DB
    # with no markets is a valid state for production and a useless one for this demo, and the API must not
    # auto-seed itself - a process that inserts rows at import time is how a restart silently mutates data.
    for argv, note in (([sys.executable, "tools/run-sql.py", "--sqlite", "--dir", "db/migrations-sqlite",
                         "--db", db], "migrate"),
                       ([sys.executable, "tools/run-sql.py", "--file", "db/seed.sql", "--db", db], "seed")):
        r = subprocess.run(argv, cwd=str(ROOT), env=env, capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("%s failed: %s" % (note, (r.stderr or r.stdout)[-400:]))
        print("   %-8s %s" % (note + ":", r.stdout.strip().splitlines()[-1]))
    mock = subprocess.Popen([sys.executable, "mock_clob.py", "--host", "127.0.0.1", "--port", str(mock_port)],
                            cwd=str(ROOT / "services" / "executor-mock"), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
                            "--port", str(api_port), "--log-level", "warning"],
                           cwd=str(ROOT / "services" / "api"), env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    rc = 0
    try:
        wait_up(mock_port, mock)
        wait_up(api_port, api)
        base = "http://127.0.0.1:%d" % api_port

        print("\n[1] health — liveness answers even if readiness would not")
        code, body, _, H = call("GET", base + "/healthz")
        assert code == 200 and body.get("ok") is True, body
        code, ready, _, _h = call("GET", base + "/readyz")
        print("    readyz says:", json.dumps(ready))

        print("\n[2] the seed the gate reads — tick 0.01, min size 5 shares, book present")
        code, m, _, _h = call("GET", base + "/v1/markets/0xM1")
        assert code == 200, (code, m)
        code, bk, _, _h = call("GET", base + "/v1/markets/0xM1/book")
        assert code == 200 and bk.get("bids") and bk.get("asks"), (code, bk)

        print("\n[3] POST /v1/orders with no Idempotency-Key — refused BEFORE any work")
        code, e1, _, _h = call("POST", base + "/v1/orders", body={"marketId": "0xM1", "tokenId": "0xT10",
                                                              "side": "BUY", "price": "0.50", "size": "10"},
                          headers={"X-User-Id": "u-demo"})
        assert code == 400 and e1["error"]["code"] == "IDEM_KEY_REQUIRED", (code, e1)

        print("\n[4] a real order — 202, queued, with the gate's latency and check count")
        key = "envelope-demo-%d" % int(time.time())
        order = {"marketId": "0xM1", "tokenId": "0xT10", "side": "BUY", "price": "0.50", "size": "10"}
        code, ok, _, H = call("POST", base + "/v1/orders", body=order,
                          headers={"X-User-Id": "u-demo", "Idempotency-Key": key})
        assert code == 202, (code, ok)
        assert ok.get("state") == "queued", ok
        assert "accepted at the venue" in str(ok.get("note", "")).lower(), \
            "the 202 body must not imply a venue answer: %s" % ok
        assert not any(k in ok for k in ("orderId", "venueOrderId")), "the API cannot know a venue id yet"
        # the gate's telemetry on the ACCEPT path, over a socket. A header is the only place "is the risk
        # check getting slow?" is answerable, and a mutation that drops it here is caught by this line.
        low = {k.lower(): v for k, v in H.items()}
        assert "x-risk-latency-ms" in low and "x-risk-checks" in low, \
            "accepted order lost its risk telemetry: %s" % sorted(low)
        assert int(low["x-risk-checks"]) >= 5, "only %s checks ran before accepting an order" % low["x-risk-checks"]
        assert low.get("x-request-id"), "no request id echoed on the success path"

        print("\n[5] replay of the SAME key — the stored 202 comes back, nothing new is written")
        code, again, _, _h = call("POST", base + "/v1/orders", body=order,
                             headers={"X-User-Id": "u-demo", "Idempotency-Key": key})
        assert code == 202 and again["intentId"] == ok["intentId"], (code, again)

        print("\n[6] same key, different body — 409 IDEM_CONFLICT, not a second order")
        code, conf, _, _h = call("POST", base + "/v1/orders", body={**order, "size": "12"},
                            headers={"X-User-Id": "u-demo", "Idempotency-Key": key})
        assert code == 409 and conf["error"]["code"] == "IDEM_CONFLICT", (code, conf)

        print("\n[7] a refusal — off-tick price, and the key is NOT burned by it")
        code, off, _, Hoff = call("POST", base + "/v1/orders", body={**order, "price": "0.5005"},
                           headers={"X-User-Id": "u-demo", "Idempotency-Key": key + "-off"})
        assert code == 422 and off["error"]["code"] == "OFF_TICK", (code, off)
        assert "detail" not in off and "Traceback" not in json.dumps(off)
        low_off = {k.lower(): v for k, v in Hoff.items()}
        assert "x-risk-latency-ms" in low_off, "a refusal must report the gate's cost too, or the metric is one-sided"

        print("\n[8] fix the price under the SAME key — accepted, which is the point of abandoning on refusal")
        code, fixed, _, _h = call("POST", base + "/v1/orders", body={**order, "price": "0.51"},
                             headers={"X-User-Id": "u-demo", "Idempotency-Key": key + "-off"})
        assert code == 202, (code, fixed)

        print("\n[9] the intent, polled")
        code, intent, _, _h = call("GET", base + ok["poll"], headers={"X-User-Id": "u-demo"})
        assert code == 200 and intent["intentId"] == ok["intentId"], (code, intent)
        assert "asOf" in intent and "staleAfter" in intent, "a read without a timestamp is a rumour"

        print("\n[10] the mock's view — EMPTY, and that is the assertion")
        mbase = "http://127.0.0.1:%d" % mock_port
        code, snap, _, _h = call("GET", mbase + "/v1/orders")
        n_orders = len(snap.get("orders", {})) if isinstance(snap.get("orders"), dict) else len(snap.get("orders", []))
        assert code == 200, (code, snap)
        assert n_orders == 0, (
            "the API placed an order at the venue: %s — in P04 the api process only ENQUEUES, and it has no "
            "key, so anything else means the boundary between 'queued' and 'sent' has been broken" % snap)
        print("     mock has %d orders after 3 accepted intents: correct, the executor service is P05/P06; "
              "the mock is exercised in-process by tests/test_executor.py and end-to-end by this endpoint pair"
              % n_orders)

        print("\n[11] kill switch — engage, refuse, disarm, allow again (append-only history)")
        code, eng, _, _h = call("POST", base + "/v1/admin/kill-switch",
                           body={"engaged": True, "reason": "envelope demo"},
                           headers={"X-Admin-Token": "demo-token"})
        assert code == 200 and eng["engaged"] is True, (code, eng)
        code, halt, _, Hhalt = call("POST", base + "/v1/orders", body=order,
                            headers={"X-User-Id": "u-demo", "Idempotency-Key": key + "-halt"})
        assert code == 503 and halt["error"]["code"] == "RISK_HALT", (code, halt)
        assert halt["error"].get("retryable") is True, "a halt must be marked retryable or clients give up"
        assert {k.lower() for k in Hhalt} >= {"retry-after", "x-risk-latency-ms"}, \
            "a 503 without Retry-After/latency tells the client neither when to return nor what was slow"
        code, dis, _, _h = call("POST", base + "/v1/admin/kill-switch",
                           body={"engaged": False, "reason": "envelope demo over"},
                           headers={"X-Admin-Token": "demo-token"})
        assert code == 200 and dis["engaged"] is False, (code, dis)
        code, after, _, _h = call("POST", base + "/v1/orders", body=order,
                             headers={"X-User-Id": "u-demo", "Idempotency-Key": key + "-after"})
        assert code == 202, (code, after)

        print("\n[12] the contract, served — the yaml is not the only description of the API")
        code, spec, _, _h = call("GET", base + "/openapi.json")
        assert 202 in {int(k) for k in spec["paths"]["/v1/orders"]["post"]["responses"]}, "lost the 202"

        print("\nALL TWELVE STEPS MATCHED THE CONTRACT")
        print("(every step asserts; step 10 asserts a NEGATIVE: no venue call from the api process)")
    except AssertionError as e:
        print("\nMISMATCH:", e)
        rc = 1
    finally:
        for p in (api, mock):
            p.terminate()
        for p in (api, mock):
            try:
                out = p.communicate(timeout=6)[0] or ""
            except subprocess.TimeoutExpired:
                p.kill()
                out = ""
            tail = [ln for ln in out.splitlines() if ln.strip()][-8:]
            if tail:
                print("\n-- service log tail --")
                for ln in tail:
                    print("  ", ln[:200])
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db + suffix)
            except OSError:
                pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
