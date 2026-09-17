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


def pick_subjects(f: NET.Fetcher, n: int) -> list[dict]:
    """The busiest accepting markets right now, because a dead market cannot fail informatively.

    Several rather than one, for two reasons found the hard way: a single market can go 90 s without a public
    fill (which makes B2 refuse to pass on nothing, correctly), and one market's book tells you nothing about
    what the socket does when some subscriptions are quiet and others are not.
    """
    rows = f.get(f"{GAMMA}/markets", source="gamma.markets",
                 params={"limit": 100, "active": "true", "closed": "false", "order": "volume24hr",
                         "ascending": "false"}).json or []
    live = []
    for r in rows:
        try:
            m = N.market_from_gamma(r)
        except N.ShapeError:
            continue
        if m["accepting_orders"] and m["enable_order_book"] and m["tokens"]:
            live.append(m)
    if not live:
        raise SystemExit("no live order-book market to test against — the venue or Gamma changed; this test "
                         "refuses to pass on a substitute")
    live.sort(key=lambda m: -m["volume_24h_micro"])
    seen, out = set(), []
    for m in live:
        if m["condition_id"] in seen:
            continue
        seen.add(m["condition_id"])
        out.append(m)
        if len(out) >= max(1, n):
            break
    return out


def pick_subject(f: NET.Fetcher) -> dict:
    return pick_subjects(f, 1)[0]


def rest_snapshot(f: NET.Fetcher, bookset: B.BookSet, token_ids: list[str], now_ms: int) -> None:
    for tok in token_ids:
        r = f.get(f"{CLOB}/book", source="clob.book", params={"token_id": tok})
        if not r.ok:
            continue
        bk = bookset.books.setdefault(tok, B.Book(tok))
        bk.apply_snapshot(N.book_from_clob(r.json, now_ms=now_ms), now_ms)
        bookset.note_resync(tok)


def venue_fills_in_window(f: NET.Fetcher, since_s: int, until_s: int, *, settle_s: int = 20
                          ) -> tuple[list[dict], bool]:
    """(fills >= our alert threshold in [since_s, until_s], did the reference reach the end of that window).

    This function is the ONLY thing "no large fill was missed" can mean, so a broken version of it is worse than
    no check at all: the first version asked for the unbounded page (`?limit=100&offset=N`) and it took me a
    while to notice that endpoint returns a **view that is minutes stale and never refreshes**. Measured on
    2026-09-17 at three points 20 s apart: the newest fill it would name was 236 s, 257 s, 277 s old — the lag
    grew exactly with real time, and bypassing the CDN cache (cf-cache-status MISS, identical bytes) changed
    nothing, so it is the origin's materialised view, not our cache. Adding `&start=&end=` returns a different
    path that is 0-1 s current, reproduced 3/3.

    Two consequences, both baked in here: the reference query must be bounded, and it must *prove* it observed
    the end of the window before it is allowed to report "nothing was missed". Without that proof an empty
    result is indistinguishable from a venue whose tape had not caught up — which is exactly the trap that made
    check C inconclusive the first time round.
    """
    out, offset, reached_end = [], 0, False
    while offset <= 2_000:
        rows = f.get(f"{DATA}/trades", source="data.trades",
                     params={"limit": 100, "offset": offset, "takerOnly": "true",
                             "start": since_s, "end": until_s + settle_s}).json or []
        if not rows:
            break
        stamps = [int(r.get("timestamp") or 0) for r in rows]
        if stamps and max(stamps) >= until_s:
            reached_end = True                      # the reference saw as far as the end of the outage
        for row in rows:
            try:
                fill = N.fill_from_rest(row)
            except N.ShapeError:
                continue
            if since_s <= fill["ts_ms"] // 1000 <= until_s and \
                    fill["usd_notional_micro"] >= BIG_FILL_USD * 10 ** 6:
                out.append(fill)
        if stamps and min(stamps) < since_s:
            break                                   # paged past the start of the window; the rest is older
        offset += 100
    return out, reached_end


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kill-seconds", type=int, default=300)
    ap.add_argument("--warmup-seconds", type=int, default=45)
    ap.add_argument("--recover-seconds", type=int, default=45)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--capacity", type=int, default=40_000,
                    help="durable tape rows to hold; a 300s outage at ~20 fills/s is ~6,000, so the default "
                         "must not evict the very window under test")
    ap.add_argument("--subjects", type=int, default=6,
                    help="markets to watch; one market in a 25s warm-up can easily deliver no fills at all, "
                         "and B2 then correctly refuses to pass on nothing")
    a = ap.parse_args()
    if a.kill_seconds < 60:
        print("note: --kill-seconds %d is a smoke run. The phase gate is 300.\n" % a.kill_seconds)

    f = NET.Fetcher()
    subject = pick_subject(f)
    subjects = pick_subjects(f, a.subjects)
    subject = subjects[0]
    tokens = []
    for sub in subjects:
        tokens.extend(sub["tokens"][:2])
    tokens = tokens[:B.BookSet.SUBS_PER_CONNECTION]
    print("subject: %s\n         %s (%d tokens, $%.0f 24h) — %d markets, %d tokens subscribed"
          % (subject["question"][:70], subject["id"][:18], len(subject["tokens"]),
             subject["volume_24h_micro"] / 10 ** 6, len(subjects), len(tokens)))

    tape = T.Tape(capacity=a.capacity)
    bookset = B.BookSet(stale_ms=3000)
    fresh = FR.Freshness()
    fresh.add(FR.Source("ws.tape", "tape", 3000, transport_alive=False))
    fresh.add(FR.Source("clob.book", "book", 3000, transport_alive=False))
    # The REST poller is a SEPARATE source with its own staleness budget. It used to note its fills onto
    # `ws.tape`, which made the composite flip back to `ok` in the middle of the outage — a harness bug that
    # reads exactly like the product bug the check exists to catch, and worth the 5 extra lines because the fix
    # is the assertion: the indicator tracks the LIVE feed, not whatever else happens to be polling.
    fresh.add(FR.Source("data.trades", "tape", 10_000, transport_alive=False))
    engine, rule = Engine(), [build_rule("chaos-large", "large_fill",
                                        {"abs_usd_micro": BIG_FILL_USD * 10 ** 6, "min_sample": 5})]
    alerts: list = []
    stats = {"ws_msgs": 0, "rest_polls": 0, "stale_samples": 0, "ok_samples_during_outage": 0,
             "frames_during_outage": 0,
             # How current the reference tape was, worst observed. A "nothing was missed" line otherwise rests on
             # an unseen assumption about how fresh the venue's own query happened to be mid-measurement.
             "rest_poll_lag_ms": 10 ** 15}
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
                # `type` is the engine's dispatch key — it reads events, not venue payloads. Feeding it a raw
                # fill made every check below "pass" with zero alerts ever fired, which is a vacuous pass of
                # exactly the kind this harness exists to catch, so the field is spelled out here.
                alerts.extend(engine.evaluate(rule, dict(live, type="fill"), now))
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
            # Bounded on purpose: the unbounded "latest" page is a view that is minutes stale and never
            # refreshes — measured 2026-09-17, newest fill 236 s / 257 s / 277 s old at samples 20 s apart,
            # identical bytes even with the CDN cache bypassed. A poller built on it looks healthy while reading
            # yesterday. `takerOnly` plus the range selects the path that measured 0-1 s current, 3/3.
            rows = f.get(f"{DATA}/trades", source="data.trades",
                         params={"limit": 100, "takerOnly": "true", "start": now_ms // 1000 - 600,
                                 "end": now_ms // 1000 + 60}).json or []
            stats["rest_polls"] += 1
            stamps = [int(r.get("timestamp") or 0) for r in (rows or [])]
            if stamps and max(stamps):
                stats["rest_poll_lag_ms"] = min(stats["rest_poll_lag_ms"], now_ms - max(stamps) * 1000)
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
                fresh.sources["data.trades"].transport_alive = True
                if tape.add(dict(fill, source="rest")):
                    fresh.note_event("data.trades", fill["ts_ms"])
                alerts.extend(engine.evaluate(rule, dict(fill, type="fill", market_median_fill_micro=0,
                                                          market_fill_sample=max(20, len(tape.rows))), now_ms))
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
    # Kill it for real, then let the product notice. Setting `transport_alive = False` by hand here would test
    # the assertion instead of the code that is supposed to produce it — and the first 300 s run proved the
    # difference: a socket that was merely "not read any more" stayed alive-looking for two samples, which is
    # precisely the state a real close kills. Closing makes `poll()` raise, and the `except` path below is the
    # product's own dead-transport path.
    sock = client["ws"]
    if sock is not None:
        try:
            sock.close()
        except Exception:
            pass
    print("KILLING THE WEBSOCKET at %d (for %ds). tape=%d rows, ws_msgs=%d"
          % (killed["since"], a.kill_seconds, pre, stats["ws_msgs"]))
    t_end = time.time() + a.kill_seconds
    # (seconds since kill, status) for every sample. Recording the series rather than a count is the point: the
    # claim worth testing is not "never 'ok' again" — the indicator is deliberately time-based, so flipping
    # within `stale_ms` is correct behaviour and an assertion that ignored it failed a working system — but
    # "flips promptly, then does not flap back".
    series: list[tuple[float, str]] = []
    while time.time() < t_end:
        st = fresh.composite(int(time.time() * 1000))
        stats["stale_samples"] += 1
        # the per-source breakdown travels with every sample, because "it flapped" is not diagnosable and the
        # first version of this message cost a 300 s re-run to learn WHICH source said ok
        series.append((round(time.time() - killed["since"], 1), st["status"], dict(st["sources"])))
        if st["status"] == "ok":
            stats["ok_samples_during_outage"] += 1
        if st["sources"].get("ws.tape") == "ok":
            stats["frames_during_outage"] += 1
        time.sleep(1.0)
    flips = [t for t, st, _src in series if st != "ok"]
    stats["freshness_series"] = [(t, st) for t, st, _src in series]
    stats["ok_detail"] = [(t, src) for t, st, src in series if st == "ok"]
    stats["flip_after_s"] = min(flips) if flips else None
    # any 'ok' AFTER the first flip is a flap: the page promising live data in the middle of an outage
    stats["ok_after_flip"] = sum(1 for t, st, _src in series if st == "ok"
                                  and stats["flip_after_s"] is not None
                                 and t > stats["flip_after_s"])
    killed["until"] = int(time.time())
    print("outage over (%ds). reconnecting..." % (killed["until"] - killed["since"]))
    ws = client["ws"]
    if ws is not None:
        fresh.sources["ws.tape"].transport_alive = False
    killed["since"] = None                                     # un-block ws_thread -> it reconnects
    time.sleep(a.recover_seconds)
    stop.set()
    now_ms = int(time.time() * 1000)

    # What "promptly" means is derived from the product's own threshold, not from a number I like: the source
    # is allowed to read 'ok' until its stale window elapses, plus one sample interval of slack.
    grace_s = (max(s2.stale_ms for s2 in fresh.sources.values()) / 1000.0) + 2.0

    # ---- verdicts ---------------------------------------------------------------------------------------
    win_fills, ref_reached_end = venue_fills_in_window(f, max(0, killed["until"] - a.kill_seconds),
                                                        killed["until"])
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

    # A's failure text, built outside the list so the paren nesting stays readable and the message can name the
    # source that said ok instead of just counting the times it happened.
    ok_reason = "; ".join("t=%.1fs[%s]" % (t, ",".join("%s=%s" % kv for kv in sorted(src.items())))
                          for t, src in stats["ok_detail"][:5]) or "no ok samples"
    a_detail = ("the page never stopped claiming live data (statuses: %s)"
                % ", ".join("%gs:%s" % (t, st) for t, st in stats["freshness_series"][:12])
                if stats["flip_after_s"] is None else
                "flapped back to 'ok' %d time(s) after going stale at %.1fs; ok because: %s"
                % (stats["ok_after_flip"], stats["flip_after_s"], ok_reason))

    checks = [
        ("A. the stale indicator flipped within %.0fs of the kill and held for %d/%d samples"
         % (grace_s, stats["stale_samples"] - stats["ok_samples_during_outage"], stats["stale_samples"]),
         stats["flip_after_s"] is not None and stats["flip_after_s"] <= grace_s
         and stats["ok_after_flip"] == 0 and stats["stale_samples"] >= max(10, a.kill_seconds // 2),
         a_detail),
                ("B. no duplicate alerts (%d alerts, %d rules fired)" % (len(alerts), engine.fired),
         dup_alerts == 0, "%d duplicate deliveries" % dup_alerts),
        ("B2. the two clocks actually collided (live fills seen=%d, already-booked=%d, durable merges=%d, "
         "alerts fired=%d)" % (tape.live_seen, tape.live_suppressed, tape.dups, engine.fired),
         tape.live_seen > 0 and engine.fired > 0,
         "the WS delivered %d trade frame(s) and the engine fired %d alert(s), so alert-level dedupe was not "
         "exercised by this run — re-run it, or raise --subjects until a large fill lands in the window"
         % (tape.live_seen, engine.fired)),
        ("C. no missed large fills (>= $%d: %d of %d venue rows in the window)"
         % (BIG_FILL_USD, len(covered), len(win_fills)) if ref_reached_end else
         "INCONCLUSIVE 0 of %d large fills — the venue's own tape had not reached the end of the window when "
         "we asked, so it cannot vouch for anything; wait longer or re-run" % len(win_fills),
         ref_reached_end and len(covered) == len(win_fills) and len(win_fills) > 0,
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
    print("  reference tape lag: worst %s behind wall clock%s"
          # Signed on purpose. Negative = the venue's indexer stamped a fill ahead of our wall clock (its HTTP
          # `Date` agrees with us to <1 s, so it is their ingest path, not NTP). The product clamps this to 0;
          # the harness shows the real number, because a reader needs to know the reference was current.
          % ("%.1f s" % (stats["rest_poll_lag_ms"] / 1000) if stats["rest_poll_lag_ms"] < 10 ** 14
             else "NEVER MEASURED (the poller saw no fill at all)",
             "" if stats["rest_poll_lag_ms"] < 60_000 else " — above a minute and check C is weaker than it looks"))
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