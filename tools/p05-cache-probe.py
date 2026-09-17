#!/usr/bin/env python3
"""Is REST /trades still serving fresh data? A one-shot measurement, kept because it decided a design rule.

`make probe-fresh` reported hard drift at 12:44Z on 2026-09-17: P01's `cache-buster-works` flipped from true to
false (`cache_bust_newer_of_3` = 0/3), which agrees with what the fixture capture had just seen (a 25 s wait
changed nothing). Either the venue has started caching harder or its behaviour is intermittent, and "which"
matters to P05 more than to P01: if a REST poll keeps returning the SAME stale page, then the catch-up path I
just wrote can silently never see the fills the socket missed — the tape loses fills and nothing looks broken.

So measure it the way the product uses it: watch a live market over the socket while polling REST three ways —
same URL, URL + a varying param, and a URL with a range that makes the query unique — and compare each to the
wall clock and to the newest fill actually seen on the wire.

    timeout 300 python3 tools/p05-cache-probe.py

Writes nothing. Exits 0 always: this reports, it does not gate.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request

GAMMA = "https://gamma-api.polymarket.com"
# The PUBLIC tape lives on data-api. clob.polymarket.com/trades is the authenticated per-user trade endpoint and
# answers 401 "Unauthorized/Invalid api key" to an anonymous request — I wrote this file against that host
# first and got a clean-looking "every variant failed" run, which is the second time today that fetching the
# wrong endpoint nearly produced a finding.
DATA = "https://data-api.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def fetch(url: str) -> tuple[int | None, dict, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), ""
    except Exception as e:  # noqa: BLE001 - a measurement script has to survive a bad request
        return None, {}, repr(e)[:120]


def newest_trade_ts(url: str) -> tuple[int | None, str, int]:
    """(newest trade second, cf-cache-status, rows). None if the request or parse failed."""
    status, headers, body = fetch(url)
    if status != 200:
        return None, "HTTP %s" % status, 0
    try:
        rows = json.loads(body)
    except json.JSONDecodeError:
        return None, "unparseable", 0
    if not isinstance(rows, list):
        return None, "not a list", 0
    out = []
    for r in rows:
        try:
            out.append(int(str(r.get("timestamp", ""))))
        except (TypeError, ValueError):
            pass
    return (max(out) if out else 0), str(headers.get("cf-cache-status")), len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, default=75, help="seconds to keep the socket open")
    args = ap.parse_args()

    st, _, body = fetch(GAMMA + "/markets?limit=100&active=true&closed=false&order=volume24hr&ascending=false")
    if st != 200:
        print("gamma unreachable (%s); nothing measured" % st)
        return 0
    token = slug = cond = None
    for m in json.loads(body):
        try:
            toks = json.loads(m.get("clobTokenIds") or "[]")
        except json.JSONDecodeError:
            continue
        if toks:
            token, slug, cond = toks[0], m.get("slug"), m.get("conditionId")
            break
    if not token:
        print("no market with a token id; nothing measured")
        return 0
    print("watching %s (conditionId %s)" % (slug, cond))

    live: list[int] = []
    stop = threading.Event()

    def socket_reader() -> None:
        try:
            from websocket import create_connection
        except ImportError:
            print("no websocket-client; comparing REST against the wall clock only")
            return
        try:
            ws = create_connection(WS_URL, timeout=20)
        except Exception as e:  # noqa: BLE001
            print("ws connect failed: %r" % (e,))
            return
        ws.send(json.dumps({"assets_ids": [token], "type": "market"}))
        ws.settimeout(2.0)
        t0 = time.time()
        while not stop.is_set() and time.time() - t0 < args.watch:
            try:
                frame = ws.recv()
            except Exception:  # noqa: BLE001 - timeouts are the normal case
                continue
            if isinstance(frame, bytes):
                frame = frame.decode("utf-8", "replace")
            for line in str(frame).split("\n"):
                line = line.strip()
                if line.startswith("{") and '"event_type":"trade"' in line:
                    try:
                        live.append(int(str(json.loads(line).get("timestamp", ""))[:13]) // 1000)
                    except (json.JSONDecodeError, ValueError):
                        pass
        ws.close()

    th = threading.Thread(target=socket_reader, daemon=True)
    th.start()
    time.sleep(4)

    # Mirrored off how the phase actually reads the tape (tools/p05-chaos-test.py rest_thread): a GLOBAL feed of
    # recent fills, filtered locally. `market=` was tried too, because whether per-market catch-up is even
    # possible changes what `universe.py` can promise — and the first version of this file asked for
    # `market=<token id>`, got two empty pages back, and was about to call that a cache verdict.
    base = DATA + "/trades?limit=100&takerOnly=true"
    bycond = "%s/trades?limit=100&takerOnly=true&market=%s" % (DATA, cond)
    bytoken = "%s/trades?limit=100&takerOnly=true&market=%s" % (DATA, token)
    before = newest_trade_ts(base)
    print("  before            newest=%s  cf=%s  rows=%d" % before)
    time.sleep(30)
    plain = newest_trade_ts(base)
    busted = newest_trade_ts(base + "&_=" + str(int(time.time())))
    ranged = newest_trade_ts(base + "&start=%d&end=%d" % (int(time.time()) - 3600, int(time.time()) + 60))
    filt_cond = newest_trade_ts(bycond)
    filt_token = newest_trade_ts(bytoken)
    stop.set()
    time.sleep(0.5)

    now = int(time.time())
    newest_live = max(live) if live else None
    print("\n  after 30 s (wall clock %d):" % now)
    print("  per-market filter: by conditionId rows=%s newest=%s | by token id rows=%s newest=%s"
          % (filt_cond[2], filt_cond[0], filt_token[2], filt_token[0]))
    for label, (ts, cf, rows) in (("same url   ", plain), ("busted url ", busted), ("own range  ", ranged)):
        if rows == 0:
            print("  %s EMPTY PAGE — no evidence here, see the guard below" % label)
        if ts is None:
            print("  %s request failed: %s" % (label, cf))
            continue
        behind = now - ts if ts else None
        vs_wire = (newest_live - ts) if (ts and newest_live is not None) else None
        print("  %s newest=%s  %ss behind now  %s vs the wire  cf=%s rows=%d"
              % (label, ts, behind, ("%+ds" % -vs_wire if vs_wire is not None else "n/a"), cf, rows))
    print("  trades seen live on the socket: %d (newest %s)" % (len(live), newest_live))
    rest_ts = [r[0] for r in (plain, busted, ranged)]
    # Every conclusion needs a denominator. Two empty pages "differ" trivially and that is not a finding; the
    # gate in this phase learned this the hard way twice already.
    empty = max((r[2] if isinstance(r[2], int) else 0) for r in (plain, busted, ranged)) == 0
    if empty or not live:
        print("\n  CONCLUSION: refused — %s, so there is nothing to compare. Re-run when the venue is"
              " trading rather than reporting a verdict from zeros."
              % ("REST returned no rows" if empty else "the socket saw no fills"))
        return 0
    all_failed = all(t is None for t in rest_ts)
    if all_failed:
        print("\n  CONCLUSION: every REST variant failed, so this run measured nothing — retry, and do not"
              "\n  record a verdict from a run with no numbers in it.")
    elif plain == busted == ranged and newest_live is not None and plain[0] and max(live) > plain[0]:
        print("\n  CONCLUSION: all three REST variants returned the same page while the socket had newer fills —"
              "\n  the catch-up path is reading a cache, not the tape. P05 must not treat a REST poll as"
              "\n  authoritative without a second, differently-keyed request.")
    elif plain != busted or (newest_live is not None and plain[0] and plain[0] >= newest_live):
        print("\n  CONCLUSION: at least one variant moved, so the cache is breakable and the stale read was"
              "\n  intermittent — but the design still cannot DEPEND on it.")
    else:
        print("\n  CONCLUSION: REST was already current at both samples, so nothing is stale this run;"
              "\n  the earlier 0/3 was a window when the venue cached harder.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
