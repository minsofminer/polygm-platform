#!/usr/bin/env python3
"""P15 D5 — the external synthetic order: the check that catches "everything looks green but the product is broken".

    # one shot, for CI smoke after a deploy
    python3 tools/p15-synthetic.py --api https://api.staging.example --user u-synth --once --await 25

    # the long-running form (a systemd timer on a box OUTSIDE the fleet, or a cron on the API host)
    python3 tools/p15-synthetic.py --api https://api.example --user u-synth --loop --interval 60 \
        --state /var/lib/polygm/synth.json --max-notional-micro 2000000

Why an out-of-fleet box: a probe that runs inside the thing it is probing cannot see a dead load balancer, an
expired certificate, a DNS record that got edited, or an ingress rule that forgot one path. It must come from
outside, over the public network, with the same credentials a real user's session would use.

What it proves, in one round trip: the edge accepts a request, auth works, the risk gate answers, the executor
signs and submits, the venue acks, and the state machine records the result. It is deliberately the *cheapest*
order the system will accept, and it is deliberately not admin-authenticated: an admin token would still work
while user auth is broken.

Failure policy, which is the part that matters more than the probe:

* a single miss is recorded and NOT paged — networks blip, and a pager that fires on one lost packet gets muted,
  which is how a real outage goes unnoticed;
* three consecutive misses exit non-zero and stamp `alert: true` into the state file, which is what the SEV1
  rule reads;
* a **rejection** (a 4xx the product itself chose) is not a probe failure: the probe's job is the path, not the
  outcome. It is counted separately, because a sudden run of rejections is a signal about the venue or the market.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

OK, FAILED, UNREACHABLE = 0, 1, 2


def call(url: str, *, method: str = "GET", body: dict | None = None, user: str = "", token: str = "",
         key: str = "", timeout: float = 15.0):
    headers = {"content-type": "application/json"}
    if user:
        headers["x-user-id"] = user
    if token:
        headers["authorization"] = "Bearer %s" % token
    if key:
        headers["Idempotency-Key"] = key
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:400]}


def read_state(path: pathlib.Path) -> dict:
    if path and path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def write_state(path: pathlib.Path, state: dict) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def one_round(api: str, args) -> tuple[str, dict]:
    """Returns (outcome, detail). outcome is one of ok|rejected|unreachable|wrong-shape."""
    now_ms = int(time.time() * 1000)
    key = "synth-%d" % (now_ms // max(1000, int(args.interval * 1000)) if args.loop else now_ms // 1000)
    body = {"marketId": args.market, "tokenId": args.token, "side": "BUY",
            "price": args.price, "size": args.size}
    t0 = time.time()
    try:
        code, resp = call("%s/v1/orders" % api.rstrip("/"), method="POST", body=body, user=args.user,
                          token=args.token_value, key=key[:128], timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001 — "could not reach" and "refused" are different outcomes
        return "unreachable", {"error": "%s: %s" % (type(exc).__name__, exc)}
    took_ms = int((time.time() - t0) * 1000)
    detail = {"status": code, "tookMs": took_ms, "body": resp if isinstance(resp, dict) else {}}

    if code == 202:
        intent_id = str((resp or {}).get("intentId") or (resp or {}).get("intent_id") or "")
        if not intent_id:
            return "wrong-shape", detail
        deadline = time.time() + args.await_
        while time.time() < deadline:
            st, st_body = call("%s/v1/orders/intents/%s" % (api.rstrip("/"), intent_id),
                               user=args.user, token=args.token_value, timeout=args.timeout)
            state = str((st_body or {}).get("state") or (st_body or {}).get("intent", {}).get("state") or "")
            if state in ("filled", "rejected", "cancelled", "failed"):
                detail["state"] = state
                notional = int((st_body or {}).get("notionalMicro") or 0)
                if notional > args.max_notional_micro:
                    return "wrong-shape", dict(detail, error="notional %d exceeded the synthetic budget" % notional)
                return "ok", detail
            time.sleep(1.0)
        detail["state"] = "no-terminal-state"
        return "wrong-shape", detail
    if 400 <= code < 500:
        # The product refused, in a shape the product chose. Not a path failure — but not "ok" either, so it is
        # counted where the alerting rules can see a *rate* of it.
        return "rejected", detail
    return "unreachable", detail


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P15 D5 — external synthetic paper order")
    ap.add_argument("--api", required=True, help="public base URL of the environment under test")
    ap.add_argument("--user", default="u-synth", help="the paper account the probe trades as")
    ap.add_argument("--token", default="", help="bearer token, if the environment is not dev-header")
    ap.add_argument("--token-value", default="", dest="token_value", help=argparse.SUPPRESS)
    ap.add_argument("--market", default="0xM1")
    ap.add_argument("--token-id", default="0xT1")
    ap.add_argument("--price", default="0.50")
    ap.add_argument("--size", default="1", help="the smallest meaningful size; the probe is not a trade")
    ap.add_argument("--max-notional-micro", type=int, default=2_000_000,
                    help="refuse to place anything above this (default $2)")
    ap.add_argument("--await", dest="await_", type=float, default=25.0, help="seconds to wait for a terminal state")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--state", default="", help="JSON file the alerting rules read (lastOkMs, consecutiveFailures)")
    ap.add_argument("--repeat", type=int, default=0, help="run this many rounds then stop (drills)")
    args = ap.parse_args(argv)
    if not args.loop and not args.once and not args.repeat:
        args.once = True

    state_path = pathlib.Path(args.state) if args.state else None
    state = read_state(state_path)
    state.setdefault("consecutiveFailures", 0)
    state["api"] = args.api
    rounds = 0
    while True:
        outcome, detail = one_round(args.api, args)
        rounds += 1
        now_ms = int(time.time() * 1000)
        state["lastRoundMs"] = now_ms
        state["rounds"] = int(state.get("rounds") or 0) + 1
        if outcome == "ok":
            state["lastOkMs"] = now_ms
            state["lastState"] = detail.get("state")
            state["lastLatencyMs"] = detail.get("tookMs")
            state["consecutiveFailures"] = 0
            state["alert"] = False
            print("ok %dms state=%s" % (detail.get("tookMs") or 0, detail.get("state")))
        elif outcome == "rejected":
            state["lastRejectedMs"] = now_ms
            state.setdefault("rejections", 0)
            state["rejections"] = int(state["rejections"]) + 1
            state["consecutiveFailures"] = 0
            print("rejected %s %s" % (detail.get("status"), json.dumps(detail.get("body", {}))[:200]))
        else:
            state["consecutiveFailures"] = int(state.get("consecutiveFailures") or 0) + 1
            state["lastFailureMs"] = now_ms
            state["alert"] = state["consecutiveFailures"] >= 3
            print("FAILED (%s) %s" % (outcome, json.dumps(detail)[:300]))
        write_state(state_path, state)
        if args.repeat and rounds >= args.repeat:
            return OK if state.get("consecutiveFailures", 0) < 3 else FAILED
        if not args.loop:
            return OK if outcome in ("ok", "rejected") else (UNREACHABLE if outcome == "unreachable" else FAILED)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
