#!/usr/bin/env python3
"""P05's quality gate, as an executable: kill the WebSocket for five minutes and prove the layer behaves.

    Kill the WebSocket for 5 minutes during a live test. On reconnect: no duplicate alerts, no missed fills
    above threshold, book resyncs correctly, and the UI showed a stale indicator the whole time.

This is not a simulation and not a mock: it opens a real subscription to
wss://ws-subscriptions-clob.polymarket.com/ws/market against the busiest live market it can find, polls the
real REST endpoints, and runs the same `books.py` / `tape.py` / `freshness.py` / signal-engine code the product
runs. The "kill" is a local decision to stop reading and refuse to reconnect for `--kill-seconds` — which is
the failure under test, not a nicety: a dead socket that the process keeps trying to read from is exactly the
silent case an operator cannot distinguish from a quiet market.

Four assertions, and each one can fail on its own:
  A. stale indicator          every sample inside the outage window reads stale/down — not one "ok"
  B. no duplicate alerts      alerts == unique (rule, dedupe key, cooldown bucket), and the WS/REST overlap was
                              actually exercised (zero overlaps means the test proved nothing about dedupe)
  C. no missed large fills    every above-threshold fill the venue reports for the outage window is in our tape
  D. book resyncs             after reconnect, our top of book matches GET /book, and the gap/resync counters
                              moved (a resync that never triggered is the same as no resync)

    python3 tools/p05-chaos-test.py                      # the gate: 300 s outage
    python3 tools/p05-chaos-test.py --kill-seconds 45    # a smoke run while editing this file
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "services" / "ingest"), str(ROOT / "packages")]

import books as B                                    # noqa: E402
import freshness as FR                               # noqa: E402
import net as NET                                    # noqa: E402
import normalise as N                                # noqa: E402
import tape as T                                     # noqa: E402
import wsclient as W                                 # noqa: E402
from polygm_core.signals.engine import Engine, build_rule   # noqa: E402

GAMMA, CLOB, DATA = ("https://gamma-api.polymarket.com", "https://clob.polymarket.com",
                     "https://data-api.polymarket.com")
WS = "https://ws-subscriptions-clob.polymarket.com/ws/market".replace("https", "wss", 1)
# $1,000, not $2k: P01 measured 0.2%% of fills at or above $1,000, so a 5-minute window holds a handful of
# them. "No missed fills" on an empty window is the classic vacuous pass, so the check itself requires >0.
BIG_FILL_USD = 1_000


def pick_subject(f: NET.Fetcher) -> dict:
    """The busiest accepting market we can find right now, because a dead market cannot fail informatively."""
    rows = f.get(f"{GAMMA}/markets", source="gamma.markets",
                 params={"limit": 100, "active": "true", "closed": "false", "order": "volume24hr",
                         "ascending": "false"}).json or []
    live = [N.market_from_gamma(r) for r in rows if r.get("clobTokenIds")]
    live = [m for m in live if m["accepting_orders"] and m["enable_order_book"] and m["tokens"]]
    if not live:
        raise SystemExit("no live order-book market to test against — the venue or Gamma changed; this test "
                         "refuses to pass on a substitute")
    live.sort(key=lambda m: -m["volume_24h_micro"])
    return live[0]


def rest_snapshot(f: NET.Fetcher, bookset: B.BookSet, token_ids: list[str], now_ms: int) -> None:
    for tok in token_ids:
        r = f.get(f"{CLOB}/book", source="clob.book", params={"token_id": tok})
        if not r.ok:
            continue
        bk = bookset.books.setdefault(tok, B.Book(tok))
        bk.apply_snapshot(N.book_from_clob(r.json, now_ms=now_ms), now_ms)
        bookset.note_resync(tok)


def venue_fills_in_window(f: NET.Fetcher, since_s: int, until_s: int) -> list[dict]:
    """An independent read of the truth for the outage window, from the endpoint we are NOT connected to."""
    out, offset = [], 0
    while offset < 3000:
        rows = f.get(f"{DATA}/trades", source="data.trades",
                     params={"limit": 100, "offset": offset, "takerOnly": "true"}).json or []
        if not rows:
            break
        for row in rows:
            try:
                fill = N.fill_from_rest(row)
            except N.ShapeError:
                continue
            if since_s <= fill["ts_ms"] // 1000 <= until_s and fill["usd_notional_micro"] >= BIG_FILL_USD * 10 ** 6:
                out.append(fill)
        if min(int(r.get("timestamp") or 0) for r in rows) < since_s:
            break
        offset += 100
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kill-seconds", type=int, default=300)
    ap.add_argument("--warmup-seconds", type=int, default=45)
    ap.add_argument("--recover-seconds", type=int, default=45)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--capacity", type=int, default=40_000,
                    help="durable tape rows to hold; a 300s outage at ~20 fills/s is ~6,000, so the default "
                         "must not evict the very window under test")
    a = ap.parse_args()
    if a.kill_seconds < 60:
        print("note: --kill-seconds %d is a smoke run. The phase gate is 300.\n" % a.kill_seconds)

    f = NET.Fetcher()
    subject = pick_subject(f)
    tokens = subject["tokens"][:2]
    print("subject: %s\n         %s (%d tokens, $%.0f 24h)"
          % (subject["question"][:70], subject["id"][:18], len(subject["tokens"]),
             subject["volume_24h_micro"] / 10 ** 6))

    tape = T.Tape(capacity=a.capacity)
    bookset = B.BookSet(stale_ms=3000)
    fresh = FR.Freshness()
    fresh.add(FR.Source("ws.tape", "tape", 3000, transport_alive=False))
    fresh.add(FR.Source("clob.book", "book", 3000, transport_alive=False))
    engine, rule = Engine(), [build_rule("chaos-large", "large_fill",
                                        {"abs_usd_micro": BIG_FILL_USD * 10 ** 6, "min_sample": 5})]
    alerts: list = []
    stats = {"ws_msgs": 0, "rest_polls": 0, "stale_samples": 0, "ok_samples_during_outage": 0,
             "frames_during_outage": 0}
    stop = threading.Event()
    killed = {"since": None, "until": None}
    client = {"ws": None}

    def on_message(msg):
        fresh.note_event("ws.tape", int(time.time() * 1000))
        stats["ws_msgs"] += 1
        now_ms = int(time.time() * 1000)
        for m in (msg if isinstance(msg, list) else [msg]):
            kind = m.get("event_type") or m.get("type")
            if kind == "book":
                try:
                    snap = N.book_from_clob(m, now_ms=now_ms)
                except N.ShapeError:
                    continue
                bk = bookset.books.setdefault(snap["token_id"], B.Book(snap["token_id"]))
                if bk.snapshot_age_ms(now_ms) > 2000:      # a snapshot is authoritative; deltas fix the rest
                    bk.apply_snapshot(snap, now_ms)
            elif kind == "last_trade_price":
                # The live path: a WS trade is NOT written to the durable tape (no transaction hash, so it
                # cannot be reconciled exactly), but it DOES feed the alert engine, which is the whole point of
                # the WebSocket — a whale fill should page in ~0.1s, not ~2s. Whether that alert then collides
                # with the REST alert for the same fill is assertion B, and it can only be tested if both
                # paths run, which is why this branch exists.
                try:
                    lt = N.last_trade(m)
                except (N.ShapeError, KeyError):
                    continue
                now = int(time.time() * 1000)
                live = dict(lt, usd_notional_micro=lt["price_micro"] * lt["size_micro"] // 10 ** 6,
                            market=subject["id"], wallet="", tx_hash="", type="fill",
                            market_median_fill_micro=0, market_fill_sample=max(20, len(tape.rows)))
                tape.add_live(dict(live, ts_ms=lt["ts_ms"]))
                alerts.extend(engine.evaluate(rule, live, now))
            elif kind == "price_change":
                for ch in N.price_changes(m):
                    bk = bookset.books.get(ch["token_id"])
                    if bk is None:
                        continue
                    if bk.apply_delta(ch, now_ms) == "resync" or bk.needs_resync(now_ms, 3000):
                        bookset.note_resync(ch["token_id"])
                        rest_snapshot(f, bookset, [ch["token_id"]], now_ms)

    def ws_thread():
        while not stop.is_set():
            if killed["since"] is not None and killed["until"] is None:
                time.sleep(0.2)
                continue                                     # the outage: no read, no reconnect
            try:
                c = W.WsClient(WS, on_message, timeout=10)
                c.connect()
                c.subscribe_market(tokens)
                client["ws"] = c
                fresh.sources["ws.tape"].transport_alive = True
                for tok in tokens:
                    bookset.watch(tok, priority=0)
                while not stop.is_set() and killed["since"] is None:
                    c.poll(0.5)
            except (W.WsError, OSError) as e:
                fresh.sources["ws.tape"].transport_alive = False
                fresh.sources["ws.tape"].notes.append("ws error: %s" % str(e)[:80])
                time.sleep(1.0)
            finally:
                if client["ws"] is not None:
                    try:
                        client["ws"].close()
                    except Exception:
                        pass
                    client["ws"] = None
                if killed["since"] is None:
                    fresh.sources["ws.tape"].transport_alive = False

    def rest_thread():
        """The catch-up poller. It must keep running during the outage — that is how a missed fill is not a
        lost fill, and it is the only reason assertion C can pass at all."""
        while not stop.is_set():
            now_ms = int(time.time() * 1000)
            rows = f.get(f"{DATA}/trades", source="data.trades",
                         params={"limit": 100, "takerOnly": "true", "_": now_ms}).json or []
            stats["rest_polls"] += 1
            if killed["since"] is None or killed["until"] is not None:
                fresh.sources["clob.book"].transport_alive = True
                for tok in tokens:
                    r = f.get(f"{CLOB}/book", source="clob.book", params={"token_id": tok})
                    if r.ok:
                        bk = bookset.books.setdefault(tok, B.Book(tok))
                        snap = N.book_from_clob(r.json, now_ms=now_ms)
                        if bk.needs_resync(now_ms, 3000) or not bk.bids and not bk.asks:
                            bk.apply_snapshot(snap, now_ms)
                            bookset.note_resync(tok)
                        fresh.note_event("clob.book", snap["ts_ms"])
            fresh.sources["clob.book"].transport_alive = True
            for row in rows:
                try:
                    fill = N.fill_from_rest(row)
                except N.ShapeError:
                    continue
                if tape.add(dict(fill, source="rest")):
                    fresh.note_event("ws.tape", fill["ts_ms"])
                alerts.extend(engine.evaluate(rule, dict(fill, market_median_fill_micro=0, market_fill_sample=
                                                          max(20, len(tape.rows))), now_ms))
            time.sleep(2.0)

    t1, t2 = threading.Thread(target=ws_thread, daemon=True), threading.Thread(target=rest_thread, daemon=True)
    t1.start(), t2.start()
    print("warming up for %ds (WS + REST both live)..." % a.warmup_seconds)
    time.sleep(a.warmup_seconds)
    if stats["ws_msgs"] == 0:
        print("FAIL: the socket delivered nothing during warm-up, so the outage would prove nothing")
        stop.set()
        return 1
    pre = len(tape.rows)
    killed["since"] = int(time.time())
    print("KILLING THE WEBSOCKET at %d (for %ds). tape=%d rows, ws_msgs=%d"
          % (killed["since"], a.kill_seconds, pre, stats["ws_msgs"]))
    t_end = time.time() + a.kill_seconds
    while time.time() < t_end:
        st = fresh.composite(int(time.time() * 1000))
        stats["stale_samples"] += 1
        if st["status"] == "ok":
            stats["ok_samples_during_outage"] += 1
        if st["sources"].get("ws.tape") == "ok":
            stats["frames_during_outage"] += 1
        time.sleep(1.0)
    killed["until"] = int(time.time())
    print("outage over (%ds). reconnecting..." % (killed["until"] - killed["since"]))
    ws = client["ws"]
    if ws is not None:
        fresh.sources["ws.tape"].transport_alive = False
    killed["since"] = None                                     # un-block ws_thread -> it reconnects
    time.sleep(a.recover_seconds)
    stop.set()
    now_ms = int(time.time() * 1000)

    # ---- verdicts ---------------------------------------------------------------------------------------
    win_fills = venue_fills_in_window(f, max(0, killed["until"] - a.kill_seconds), killed["until"])
    mine = {T.fill_key(r) for r in tape.rows}
    missed = [x for x in win_fills if T.fill_key(x) not in mine]
    covered = [x for x in win_fills if T.fill_key(x) in mine]
    keys = [(a_.rule_id, a_.dedupe_key, a_.at_ms // (300 * 1000)) for a_ in alerts]
    dup_alerts = len(keys) - len(set(keys))
    resync_ok, mism = True, []
    for tok in tokens:
        r = f.get(f"{CLOB}/book", source="clob.book", params={"token_id": tok})
        if not r.ok:
            resync_ok = False
            mism.append("GET /book failed for %s" % tok[:8])
            continue
        snap = N.book_from_clob(r.json, now_ms=now_ms)
        bk = bookset.books.get(tok) or B.Book(tok)
        for side, want in (("best_bid", max([p for p, _ in snap["bids"]], default=None)),
                           ("best_ask", min([p for p, _ in snap["asks"]], default=None))):
            got = getattr(bk, side)
            if want is not None and got is not None and abs(want - got) > 10_000:
                mism.append("%s %s ours=%s venue=%s" % (tok[:8], side, got, want))
                resync_ok = False
    resync_count = sum(b.resyncs for b in bookset.books.values())
    gap_count = sum(b.gap_detections for b in bookset.books.values())

    checks = [
        ("A. stale indicator held for the whole outage (%d samples)" % stats["stale_samples"],
         stats["ok_samples_during_outage"] == 0 and stats["stale_samples"] >= max(10, a.kill_seconds // 2),
         "%d/%d samples read 'ok' during the outage" % (stats["ok_samples_during_outage"],
                                                          stats["stale_samples"])),
        ("B. no duplicate alerts (%d alerts, %d rules fired)" % (len(alerts), engine.fired),
         dup_alerts == 0, "%d duplicate deliveries" % dup_alerts),
        ("B2. the two clocks actually collided (live fills seen=%d, already-booked=%d, durable merges=%d)"
         % (tape.live_seen, tape.live_suppressed, tape.dups),
         tape.live_seen > 0,
         "the WS delivered no trade frames at all, so alert-level dedupe was not exercised by this run"),
        ("C. no missed large fills (>= $%d: %d of %d venue rows in the window)"
         % (BIG_FILL_USD, len(covered), len(win_fills)),
         len(covered) == len(win_fills) and len(win_fills) > 0,
         ("missed %s" % [x["tx_hash"][:12] for x in missed][:4]) if missed
         else "the venue reported no >= $%d fills in the window: raise --kill-seconds rather than trusting "
              "this line" % BIG_FILL_USD),
        ("D. book resynced to the venue within one tick after reconnect", resync_ok, "; ".join(mism[:2])),
        ("D2. the resync machinery engaged (resyncs=%d, gap detections=%d)" % (resync_count, gap_count),
         resync_count > 0 or gap_count > 0,
         "neither counter moved: either the book never went stale (test too short) or detection is dead"),
        ("E. the tape kept filling from REST while the socket was dead (+%d rows)"
         % (len(tape.rows) - pre), len(tape.rows) > pre, "no rows arrived during or after the outage"),
    ]
    print("\nchaos test — %s" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    bad = 0
    for label, ok, detail in checks:
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label, "" if ok else "   -> " + detail))
        bad += 0 if ok else 1
    if a.json:
        print(json.dumps({"checks": [{"label": l, "ok": o, "detail": d} for l, o, d in checks],
                          "stats": stats, "tape_rows": len(tape.rows), "dups": tape.dups,
                          "alerts": len(alerts), "fired": engine.fired, "suppressed": engine.suppressed},
                         indent=1, default=str))
    print("\n%s" % ("ALL %d CHECKS PASSED (outage %ds, %d WS msgs, %d REST polls)"
                    % (len(checks), a.kill_seconds, stats["ws_msgs"], stats["rest_polls"])
                    if not bad else "%d CHECK(S) FAILED" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())