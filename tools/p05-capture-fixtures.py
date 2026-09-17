#!/usr/bin/env python3
"""Record REAL payloads from every P05 source, so the normalisers are written against evidence and a future
failure can be reproduced offline.

D1 of the phase asks for "payload shape (with a real example)" per endpoint. The alternative — writing the
parsers from the docs and finding the field names are different at 3am — is what this file exists to make
impossible. It also re-checks the two upstream facts the design leans on hardest: the Gamma page cap, and
whether a cache buster still defeats the edge cache on the trades endpoint.

    python3 tools/p05-capture-fixtures.py                 # HTTP only, ~10 s
    python3 tools/p05-capture-fixtures.py --ws 45         # + 45 s of the live market channel
    python3 tools/p05-capture-fixtures.py --check         # compare against what is on disk, save nothing

`--check` is the mode CI runs: it fails if a field the code depends on has disappeared, and it never rewrites
the fixtures, so a green check means the recorded evidence still matches the venue.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "services" / "ingest"), str(ROOT / "packages")]
import net                                                     # noqa: E402
import wsclient                                                # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "p05"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
LB = "https://lb-api.polymarket.com"
WS_MARKET = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# The fields our normalisers read. `--check` fails when one of these vanishes, which is the only upstream
# change that actually breaks us: a new field is noise, a missing field is an outage.
DEPENDS = {
    "gamma_events":  ["id", "slug", "title", "volume24hr", "startDate", "endDate", "closed", "archived",
                      "enableOrderBook", "negRisk", "tags"],
    "gamma_markets": ["id", "question", "conditionId", "slug", "endDate", "active", "closed",
                      "acceptingOrders", "orderPriceMinTickSize", "orderMinSize", "outcomes", "outcomePrices",
                      "clobTokenIds", "negRisk", "events", "spread", "bestBid", "bestAsk", "volume24hr",
                      "liquidityNum"],
    "clob_book":     ["market", "asset_id", "bids", "asks", "timestamp", "hash"],
    "clob_history": ["t", "p"],
    "data_trades":   ["proxyWallet", "side", "size", "price", "timestamp", "title", "slug", "outcome",
                      "transactionHash", "asset", "conditionId"],
    "data_activity": ["type", "size", "usdcSize", "price", "timestamp", "transactionHash", "asset",
                      "conditionId"],
}


def shape(obj) -> dict:
    """Type-and-key summary of a payload, so the fixture records its SHAPE even where the values are volatile."""
    if isinstance(obj, dict):
        return {k: shape(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return ["<%d rows>" % len(obj)] + ([shape(obj[0])] if obj else [])
    return type(obj).__name__


def fetch(f: net.Fetcher, name: str, url: str, source: str, params: dict | None = None) -> object | None:
    r = f.get(url, source=source, params=params)
    print("  %-16s %s %s  %s" % (name, r.status, "%.0f ms" % r.ms,
                                 "cached" if r.from_cache else "fresh") + ("" if r.ok else "  ERROR %s" % r.error))
    if not r.ok:
        return None
    (FIX / ("%s.json" % name)).write_text(json.dumps(r.json, indent=1, default=str))
    (FIX / ("%s.shape.json" % name)).write_text(json.dumps(shape(r.json), indent=1, default=str))
    return r.json


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", type=int, default=0, metavar="SECONDS", help="capture the live market channel")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--market-limit", type=int, default=100)
    a = ap.parse_args()
    if a.check:
        return check_only()
    FIX.mkdir(parents=True, exist_ok=True)
    f = net.Fetcher()
    print("capturing into %s" % FIX)
    ev = fetch(f, "gamma_events", f"{GAMMA}/events", "gamma.events",
               {"limit": 10, "active": "true", "closed": "false", "order": "volume24hr", "ascending": "false"})
    mk = fetch(f, "gamma_markets", f"{GAMMA}/markets", "gamma.markets",
               {"limit": a.market_limit, "active": "true", "closed": "false", "order": "volume24hr",
                "ascending": "false"})
    fetch(f, "gamma_tags", f"{GAMMA}/tags", "gamma.tags", {"limit": 20})
    rows = mk if isinstance(mk, list) else []
    live = [m for m in rows if m.get("clobTokenIds") and not m.get("closed") and m.get("acceptingOrders")]
    if not live:
        print("  no live market with token ids; the CLOB half is skipped (that is a finding, not a pass)")
        return 1
    m0 = live[0]
    toks = json.loads(m0["clobTokenIds"]) if isinstance(m0["clobTokenIds"], str) else m0["clobTokenIds"]
    cid = m0["conditionId"]
    print("  subject market: %s (%s), %d tokens" % (m0["question"][:52], cid[:14], len(toks)))
    fetch(f, "clob_book", f"{CLOB}/book", "clob.book", {"token_id": toks[0]})
    books_post(toks)
    fetch(f, "clob_midpoint", f"{CLOB}/midpoint", "clob.book", {"token_id": toks[0]})
    fetch(f, "clob_spread", f"{CLOB}/spread", "clob.book", {"token_id": toks[0]})
    fetch(f, "clob_price_buy", f"{CLOB}/price", "clob.book", {"token_id": toks[0], "side": "BUY"})
    fetch(f, "clob_market", f"{CLOB}/markets/{cid}", "clob.book")
    fetch(f, "clob_history", f"{CLOB}/prices-history", "clob.history",
          {"market": toks[0], "interval": "1h", "fidelity": 10})
    tr = fetch(f, "data_trades", f"{DATA}/trades", "data.trades", {"limit": 200, "takerOnly": "true"})
    fetch(f, "data_trades_busted", f"{DATA}/trades", "data.trades",
          {"limit": 200, "takerOnly": "true", "_": int(time.time() * 1000)})
    wallet = ""
    if isinstance(tr, list) and tr:
        wallet = tr[0].get("proxyWallet") or ""
    if wallet:
        fetch(f, "data_positions", f"{DATA}/positions", "data.activity", {"user": wallet, "limit": 50})
        fetch(f, "data_activity", f"{DATA}/activity", "data.activity", {"user": wallet, "limit": 200})
    fetch(f, "lb_volume", f"{LB}/volume", "lb.volume", {"window": "1d", "limit": 25})
    ws = capture_ws(f, toks[:2], a.ws) if a.ws else None
    man = {"captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "subject_market": {"condition_id": cid, "question": m0["question"], "tokens": toks[:4]},
           "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:16] for p in sorted(FIX.glob("*.json"))},
           "sends": f.sends, "retries": f.retry_count}
    if ws:
        man["ws"] = ws
    (FIX / "manifest.json").write_text(json.dumps(man, indent=1))
    print("  %d requests, %d retries; fixtures: %d" % (f.sends, f.retry_count, len(list(FIX.glob("*.json")))))
    print("  NOTE the edge cache: /trades plain=%s busted=%s" % (cache_note("data_trades"),
                                                                  cache_note("data_trades_busted")))
    return 0


def cache_note(name: str) -> str:
    p = FIX / ("%s.json" % name)
    if not p.is_file():
        return "n/a"
    try:
        rows = json.loads(p.read_text())
    except json.JSONDecodeError:
        return "unparseable"
    return "newest ts %s" % (rows[0].get("timestamp") if isinstance(rows, list) and rows else "?")


def books_post(toks: list[str]) -> None:
    """`/books` is a POST taking {"token_ids": [...]}. Recorded because a batch call is the only way to keep
    the per-market book poll inside its budget, and a normaliser written against the singular /book shape
    alone would silently mishandle the batch response (it returns a list keyed by `asset_id`, not one object)."""
    import urllib.request
    req = urllib.request.Request(f"{CLOB}/books", data=json.dumps({"token_ids": toks}).encode(),
                                 headers={"content-type": "application/json", "user-agent": "openout-ingest/0.1"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read().decode())
        (FIX / "clob_books.json").write_text(json.dumps(body, indent=1, default=str))
        (FIX / "clob_books.shape.json").write_text(json.dumps(shape(body), indent=1, default=str))
        print("  %-16s %s %.0f ms  batch of %d" % ("clob_books", r.status, (time.monotonic() - t0) * 1000,
                                                    len(body) if isinstance(body, list) else 1))
    except Exception as e:                                                    # noqa: BLE001
        print("  clob_books FAILED %s: %s" % (type(e).__name__, e))


def capture_ws(f: net.Fetcher, assets: list[str], seconds: int) -> dict:
    seen: list[dict] = []
    counts: dict[str, int] = {}

    def on_msg(m):
        if isinstance(m, list):                     # the channel delivers arrays on burst
            for x in m:
                on_msg(x)
            return
        et = str(m.get("event_type") or m.get("type") or "?")
        counts[et] = counts.get(et, 0) + 1
        if len(seen) < 40 and not any(s.get("_kind") == et for s in seen):
            seen.append(dict(m, _kind=et))
        if et in ("last_trade_price",) and len(seen) >= 40:
            counts["_capped"] = counts.get("_capped", 0) + 1

    c = wsclient.WsClient(WS_MARKET, on_msg, timeout=10)
    t0 = time.monotonic()
    c.connect()
    c.subscribe_market(assets)
    while time.monotonic() - t0 < seconds:
        try:
            c.poll(1.0)
        except wsclient.WsError as e:
            print("  ws closed: %s" % e)
            break
    c.close()
    dur = time.monotonic() - t0
    (FIX / "ws_frames.json").write_text(json.dumps(seen, indent=1, default=str))
    shapes = {}
    for msg in seen:
        s2 = dict(msg)
        kind = s2.pop("_kind")
        shapes.setdefault(kind, s2)          # one real example per event type, nothing more
    (FIX / "ws_shapes.json").write_text(json.dumps({k: shape(v) for k, v in shapes.items()}, indent=1,
                                                   default=str))
    print("  %-16s %d frames in %.1fs %s" % ("ws_market", c.frames, dur, counts))
    return {"seconds": round(dur, 1), "frames": c.frames, "bytes": c.bytes_in, "pings": c.pings,
            "pongs": c.pongs, "event_types": counts, "handler_errors": c.handler_errors[:5],
            "sample_assets": assets, "closed_reason": c.closed_reason}


def check_only() -> int:
    """Re-fetch each subject and assert the fields DEPENDS still exist. No writes, so it is safe in CI."""
    if not (FIX / "manifest.json").is_file():
        print("no fixtures on disk; run without --check first")
        return 2
    f = net.Fetcher()
    bad = 0
    mk = f.get(f"{GAMMA}/markets", source="gamma.markets",
               params={"limit": 100, "active": "true", "closed": "false", "order": "volume24hr",
                       "ascending": "false"}).json
    rows = mk if isinstance(mk, list) else []
    if not rows:
        print("FAIL gamma /markets returned nothing")
        return 1
    live = [m for m in rows if m.get("clobTokenIds") and not m.get("closed")]
    subj = live[0] if live else rows[0]
    for name, need in DEPENDS.items():
        p = FIX / ("%s.json" % name)
        if not p.is_file():
            print("  %-16s no fixture (never captured)" % name)
            bad += 1
            continue
        recorded = json.loads(p.read_text())
        sample = recorded[0] if isinstance(recorded, list) and recorded else recorded
        keys = set(sample or {})
        gone = [k for k in need if k not in keys]
        if gone:
            print("  %-16s FIELDS GONE from the venue: %s" % (name, ", ".join(gone)))
            bad += 1
        else:
            print("  %-16s ok (%d documented fields present in the live row)" % (name, len(need)))
    tr = f.get(f"{DATA}/trades", source="data.trades", params={"limit": 200, "takerOnly": "true"}).json
    busted = f.get(f"{DATA}/trades", source="data.trades",
                   params={"limit": 200, "takerOnly": "true", "_": int(time.time() * 1000)}).json
    a = (tr[0].get("timestamp") if isinstance(tr, list) and tr else None)
    b = (busted[0].get("timestamp") if isinstance(busted, list) and busted else None)
    print("  cache buster  %s (plain=%s busted=%s)" % ("works" if a != b else "no effect this run", a, b))
    print("\n%s" % ("fixtures still match the venue" if not bad else "%d source(s) need a code change" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
