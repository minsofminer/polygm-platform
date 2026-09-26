#!/usr/bin/env python3
"""P15 D3 — the drain guard: the one question a deploy must get right about the executor.

    python3 tools/p15-drain-guard.py --api https://api.<domain> --token "$PGM_ADMIN_TOKEN" \\
        --set-draining --timeout 300

Before the executor process may be replaced, this asks the running system a question it cannot answer from a
file: *is there an order in flight that we would orphan by killing the process that is talking to the venue?*

Three exit codes, and the middle one is the whole point:

  0   safe — draining is on (if asked for) and no intent is in `submitting`/`uncertain`. Replace.
  3   **timed out waiting for safety — do NOT replace.** A deploy that is a minute late is a deploy; an
      orphaned order is an incident, and the order of those two harms is not a matter of taste.
  2   could not establish safety (unreachable, unparseable, refused). Fail-closed: the same as 3, but the
      message says which. "The check did not run" must never be read as "the check passed".

`pending`/`queued` intents are reported and do not block: they are durable rows the next executor drains from
Postgres. `submitting`/`uncertain` block, because those are the states where this process may be mid-conversation
with the venue — the P6 D3 ambiguity, as two strings.

The guard is a separate program from the deploy script so that it can be *drilled*: the release harness runs it
against a fake status endpoint that reports an unsafe state, then a safe one, then nothing at all, and asserts
the three exit codes. A guard nobody has seen refuse is a guard that will one night say yes to everything.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

SAFE, UNSAFE_TIMEOUT, CANNOT_TELL = 0, 3, 2


def _req(url: str, token: str, *, method: str = "GET", body: dict | None = None, timeout: float = 10.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"X-Admin-Token": token, "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def set_draining(api: str, token: str, on: bool, reason: str) -> None:
    _req(api.rstrip("/") + "/v1/admin/flags", token, method="POST",
         body={"name": "executor_draining", "value": bool(on), "reason": reason})


def status(api: str, token: str) -> dict:
    return _req(api.rstrip("/") + "/v1/admin/drain-status", token)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P15 D3 — drain guard")
    ap.add_argument("--api", required=True, help="base URL of the API the deploy is talking to")
    ap.add_argument("--token", default="", help="admin token (X-Admin-Token)")
    ap.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for safety before giving up")
    ap.add_argument("--interval", type=float, default=5.0, help="poll interval")
    ap.add_argument("--set-draining", action="store_true", help="turn the draining flag on first")
    ap.add_argument("--clear-draining", action="store_true", help="turn the draining flag off when done")
    ap.add_argument("--reason", default="deploy: executor replace", help="the reason recorded with the flag")
    args = ap.parse_args(argv)

    if args.set_draining:
        try:
            set_draining(args.api, args.token, True, args.reason)
        except Exception as exc:  # noqa: BLE001 — reported, and the exit code is the contract
            print("cannot set the draining flag (%s: %s) — refusing to proceed" % (type(exc).__name__, exc))
            return CANNOT_TELL

    deadline = time.time() + args.timeout
    last = None
    while True:
        try:
            st = status(args.api, args.token)
        except urllib.error.HTTPError as exc:
            print("drain-status refused (%s %s) — cannot establish safety; NOT replacing" % (exc.code, exc.reason))
            return CANNOT_TELL
        except Exception as exc:  # noqa: BLE001
            print("drain-status unreachable (%s: %s) — cannot establish safety; NOT replacing"
                  % (type(exc).__name__, exc))
            return CANNOT_TELL
        last = st
        blocking = int(st.get("blockingIntents") or 0)
        open_n = int(st.get("openIntents") or 0)
        if blocking == 0:
            print("safe: %d open intent(s) queued (durable, not blocking), 0 in %s"
                  % (open_n, ", ".join(st.get("blockingStates") or ())))
            if args.clear_draining:
                try:
                    set_draining(args.api, args.token, False, args.reason + " (complete)")
                except Exception as exc:  # noqa: BLE001
                    print("warning: could not clear the draining flag (%s)" % exc)
            return SAFE
        print("waiting: %d intent(s) in %s — an ambiguous order must not be orphaned by a deploy"
              % (blocking, ", ".join(st.get("blockingStates") or ())))
        if time.time() >= deadline:
            print("TIMED OUT after %.0fs with %d blocking intent(s) — NOT replacing the executor."
                  % (args.timeout, blocking))
            print("next step: P6 D3 reconciliation, then re-run the deploy. The process that is mid-conversation"
                  " with the venue is the last thing to restart.")
            print(json.dumps(last, sort_keys=True))
            return UNSAFE_TIMEOUT
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
