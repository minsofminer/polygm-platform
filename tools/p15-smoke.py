#!/usr/bin/env python3
"""P15 D4 — the post-deploy watch: read the four pillars and refuse to call a deploy good while money is odd.

    python3 tools/p15-smoke.py --api https://api.staging.example --token "$PGM_ADMIN_TOKEN" --watch 300
    python3 tools/p15-smoke.py --api https://canary.example --token ... --watch 900 --canary

`deploy.sh` proves the containers run. This proves the *product* works: for `--watch` seconds it polls
`/v1/admin/metrics` and fails the deploy on the conditions that mean "roll back", not on the conditions that mean
"the market is quiet".

The rules, and why each is a rule rather than a number somebody eyeballs:

* **`unreconciled.count > 0` at the end of the window, or any case older than the page threshold** — the one
  number the kit calls the most important in the system. A deploy that leaves an order unresolved is not a
  deploy that went fine.
* **`ordersUnknownState.count` rising during the window** — an order that entered `submitting`/`uncertain` and
  stayed is the P6 D3 ambiguity happening live.
* **a silent feed** — "all WebSockets dead" is a SEV1 for a reason: the UI keeps rendering prices from the last
  frame, so green dashboards are exactly what it looks like.
* **the kill switch engaged during the window** — something pulled it, and no deploy continues past that.
* **`--canary` (real money) additionally requires the two independent revenue measurements to agree**: a canary
  that trades but whose builder attribution drifts from the chain measure is a canary that found the bug it
  exists to find.

It deliberately does NOT fail on: rejections (a rate is not a fault), quiet volume, or a market with no fills.
Those are what a dashboard is for; a deploy gate that trips on them gets bypassed within a month.
"""
from __future__ import annotations

import argparse
import pathlib
import json
import sys
import time
import urllib.error
import urllib.request

OK, FAILED, UNREACHABLE = 0, 1, 2


def fetch(api: str, token: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(api.rstrip("/") + "/v1/admin/metrics",
                                 headers={"X-Admin-Token": token, "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def judge(m: dict, *, canary: bool) -> list[str]:
    """Returns the list of reasons this snapshot is not good. Empty means good — and every reason is a sentence
    an operator can act on, because the exit code is read at 3am by somebody who is not the author."""
    bad: list[str] = []
    money = m.get("money") or {}
    un = money.get("unreconciled") or {}
    if int(un.get("count") or 0) > 0:
        bad.append("unreconciled orders: %s (oldest %sms) %s"
                   % (un.get("count"), un.get("oldestAgeMs"), json.dumps(un.get("byCase") or {})))
    if int((money.get("ordersUnknownState") or {}).get("count") or 0) > 0:
        bad.append("orders in an unknown state: %s" % json.dumps(money.get("ordersUnknownState")))
    silent = (m.get("freshness") or {}).get("silent") or []
    if silent:
        bad.append("feeds that stopped delivering frames: %s" % ", ".join(map(str, silent)))
    if (m.get("killSwitch") or {}).get("engaged"):
        bad.append("the kill switch is engaged (%s)" % (m.get("killSwitch") or {}).get("reason", ""))
    if canary:
        bf = money.get("builderFees") or {}
        if int(bf.get("daysUnmatched") or 0) > 0:
            bad.append("builder attribution: %s day(s) where the ledger and the chain disagree"
                       % bf.get("daysUnmatched"))
        if int(bf.get("deltaMicro") or 0) != 0:
            bad.append("builder attribution delta is %s micro, not zero" % bf.get("deltaMicro"))
        drift = money.get("positionDrift") or {}
        if int(drift.get("mismatchedOrders") or 0) > 0:
            bad.append("position drift: %s order(s) where our fills disagree with the venue's log"
                       % drift.get("mismatchedOrders"))
    return bad


def line(m: dict) -> str:
    money = m.get("money") or {}
    biz = m.get("business") or {}
    return ("unreconciled=%s unknown=%s silent=%s kill=%s fees24hOk=%s vol24h=%s traders=%s"
            % ((money.get("unreconciled") or {}).get("count"),
               (money.get("ordersUnknownState") or {}).get("count"),
               len((m.get("freshness") or {}).get("silent") or []),
               bool((m.get("killSwitch") or {}).get("engaged")),
               int((money.get("feeEstimateDelta") or {}).get("measured") or 0),
               biz.get("volume24hMicro"), biz.get("traders24h")))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P15 D4 — post-deploy watch over /v1/admin/metrics")
    # Not `required=True`: `--check-state` is the alarm path and it must work with no API in reach — that is the
    # whole point of reading a verdict that was written when the API was.
    ap.add_argument("--api", default="")
    ap.add_argument("--token", default="")
    ap.add_argument("--watch", type=float, default=0.0, help="seconds to keep watching (0 = a single read)")
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--canary", action="store_true", help="also require the two revenue measurements to agree")
    ap.add_argument("--allow-empty", action="store_true",
                    help="pass when the snapshot is empty (a fresh staging box with no traffic yet)")
    ap.add_argument("--json", action="store_true")
    # P15 D5: the state file is how the post-deploy watch becomes an *alarm* rather than a CI step nobody reads
    # at 2am. `--state` writes the verdict; `--check-state` reads it back, and the three exit codes are the three
    # answers that matter: OK, FAILED, and CANNOT-TELL (no watch has ever run, or the state is stale).
    ap.add_argument("--state", default="", help="write the verdict here after the watch")
    ap.add_argument("--check-state", default="", help="read a verdict written earlier and exit on it")
    ap.add_argument("--max-age-s", type=float, default=3600.0,
                    help="with --check-state: how old a verdict may be before it means nothing")
    args = ap.parse_args(argv)

    if args.check_state:
        return check_state(pathlib.Path(args.check_state), max_age_s=args.max_age_s)
    if not args.api:
        ap.error("--api is required unless --check-state is used")

    deadline = time.time() + args.watch
    polls = 0
    worst: list[str] = []
    first_bad_at = None
    while True:
        polls += 1
        try:
            m = fetch(args.api, args.token)
        except urllib.error.HTTPError as exc:
            print("metrics refused (%s %s): a deploy gate must not read a refusal as health"
                  % (exc.code, exc.reason))
            return UNREACHABLE
        except Exception as exc:  # noqa: BLE001
            print("metrics unreachable (%s: %s)" % (type(exc).__name__, exc))
            return UNREACHABLE
        bad = judge(m, canary=args.canary)
        if bad and first_bad_at is None:
            first_bad_at = time.strftime("%H:%M:%S")
        if bad and len(bad) > len(worst):
            worst = bad
        print("[%s] %s%s" % (time.strftime("%H:%M:%S"), line(m), "  BAD: " + "; ".join(bad) if bad else ""))
        if args.json:
            print(json.dumps(m, sort_keys=True))
        if time.time() >= deadline:
            break
        time.sleep(min(args.interval, max(0.0, deadline - time.time())))

    if args.state:
        write_state(pathlib.Path(args.state), ok=not worst, polls=polls, api=args.api, failures=worst)
    if worst:
        print("\nFAILED after %d poll(s); first bad reading at %s" % (polls, first_bad_at))
        for w in worst:
            print("  - %s" % w)
        print("next: the runbook for the first line above. Do not promote; see docs/runbooks/.")
        return FAILED
    print("OK: %d poll(s), money consistent, no silent feeds, switch where it should be" % polls)
    return OK


def write_state(path: pathlib.Path, *, ok: bool, polls: int, api: str, failures: list[str]) -> dict:
    row = {"ok": bool(ok), "polls": int(polls), "api": api, "at_ms": int(time.time() * 1000),
           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "failures": list(failures)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")
    return row


def check_state(path: pathlib.Path, *, max_age_s: float) -> int:
    """Turn a recorded verdict into an alarm. Nothing written yet is CANNOT-TELL, not "fine": a box where the
    watch has never run is a box whose deploys have never been verified."""
    if not path.exists():
        print("no smoke verdict at %s — no post-deploy watch has been recorded here (cannot tell)" % path)
        return UNREACHABLE
    try:
        row = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print("the smoke verdict at %s is unreadable (%s) — cannot tell" % (path, exc))
        return UNREACHABLE
    age_s = time.time() - (int(row.get("at_ms") or 0) / 1000.0)
    if age_s > max_age_s:
        print("the last smoke verdict is %.0f s old (limit %.0f s) — a stale verdict is not a verification"
              % (age_s, max_age_s))
        return UNREACHABLE
    if not row.get("ok"):
        print("the last post-deploy watch FAILED at %s (%s): %s"
              % (row.get("at"), row.get("api"), "; ".join(row.get("failures") or [])[:300]))
        return FAILED
    print("the last post-deploy watch passed at %s (%d poll(s), %s)" % (row.get("at"), row.get("polls", 0),
                                                                       row.get("api")))
    return OK


if __name__ == "__main__":
    raise SystemExit(main())
