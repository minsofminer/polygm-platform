"""
PolyGM prototype — "gmgn.ai for Polymarket"

Read-only backend. Talks to three public Polymarket APIs (no auth required):
  - Gamma  https://gamma-api.polymarket.com   market/event metadata
  - CLOB   https://clob.polymarket.com        order books, prices
  - Data   https://data-api.polymarket.com    trades, positions, leaderboard

Design notes for whoever builds the real thing:
  * A background thread refreshes a shared cache every REFRESH_SECS so the
    frontend never hammers Polymarket directly (their Cloudflare limits are
    per-IP, so one server-side cache serves N users).
  * Trading would be a SEPARATE service holding user wallets + CLOB V2 creds.
    It is deliberately NOT in this file.
"""

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
LB = "https://lb-api.polymarket.com"
REFRESH_SECS = int(os.environ.get("REFRESH_SECS", "20"))
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")

_cache = {"data": {}, "ts": 0.0, "errors": []}
_lock = threading.Lock()
_session = urllib.request.build_opener()
_session.addheaders = [("accept", "application/json"), ("user-agent", "polygm-prototype/0.1")]


def get(url, params=None, timeout=25):
    if params:
        url = url + "?" + urllib.parse.urlencode(params, doseq=True)
    with _session.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def slim_event(e):
    """Reduce a Gamma event to what the UI needs."""
    mkts = []
    for m in e.get("markets", []) or []:
        tids = []
        try:
            tids = json.loads(m.get("clobTokenIds") or "[]")
        except json.JSONDecodeError:
            pass
        try:
            outs = json.loads(m.get("outcomes") or "[]")
        except json.JSONDecodeError:
            outs = []
        try:
            prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
        except (json.JSONDecodeError, ValueError):
            prices = []
        mkts.append(
            {
                "question": m.get("question", ""),
                "conditionId": m.get("conditionId"),
                "slug": m.get("slug"),
                "outcomes": outs,
                "prices": prices,
                "tokenIds": tids,
                "volume24hr": _num(m.get("volume24hr")),
                "liquidity": _num(m.get("liquidity")),
                "endDate": m.get("endDate"),
                "closed": m.get("closed", False),
            }
        )
    return {
        "id": e.get("id"),
        "title": e.get("title", ""),
        "slug": e.get("slug"),
        "image": e.get("icon") or e.get("image") or "",
        "volume24hr": _num(e.get("volume24hr")),
        "volume1wk": _num(e.get("volume1wk")),
        "volume1mo": _num(e.get("volume1mo")),
        "liquidity": _num(e.get("liquidity")),
        "openInterest": _num(e.get("openInterest")),
        "endDate": e.get("endDate"),
        "negRisk": e.get("negRisk", False),
        "nMarkets": len(mkts),
        "markets": mkts[:14],
    }


def build_book(token_id):
    b = get(f"{CLOB}/book", params={"token_id": token_id})
    bids = sorted(b.get("bids") or [], key=lambda x: float(x["price"]), reverse=True)
    asks = sorted(b.get("asks") or [], key=lambda x: float(x["price"]))
    bb = float(bids[0]["price"]) if bids else None
    ba = float(asks[0]["price"]) if asks else None
    return {
        "bestBid": bb,
        "bestAsk": ba,
        "spread": round(ba - bb, 4) if (bb is not None and ba is not None) else None,
        "bidDepth": round(sum(float(x["size"]) * float(x["price"]) for x in bids), 2),
        "askDepth": round(sum(float(x["size"]) * float(x["price"]) for x in asks), 2),
        "bidLevels": len(bids),
        "askLevels": len(asks),
        "tick": b.get("tick_size"),
    }


def refresh():
    """One full cache rebuild."""
    errs = []
    data = {}

    # 1. Hot events by 24h volume
    try:
        raw = get(GAMMA + "/events", {
            "limit": 60, "active": "true", "closed": "false",
            "order": "volume24hr", "ascending": "false",
        })
        data["events"] = [slim_event(e) for e in raw]
    except Exception as ex:  # noqa: BLE001
        errs.append(f"events: {ex}")
        data["events"] = []

    # 2. Live trade feed (the "tape")
    try:
        trades = get(DATA + "/trades", {"limit": 300})
        data["tape"] = [
            {
                "wallet": t.get("proxyWallet"),
                "name": t.get("pseudonym") or t.get("name") or "?",
                "side": t.get("side"),
                "outcome": t.get("outcome"),
                "price": _num(t.get("price")),
                "size": _num(t.get("size")),
                "usd": round(_num(t.get("size")) * _num(t.get("price")), 2),
                "title": t.get("title", ""),
                "slug": t.get("slug", ""),
                "ts": t.get("timestamp"),
            }
            for t in trades
        ]
        data["tapeStats"] = {
            "count": len(trades),
            "uniqueWallets": len({t.get("proxyWallet") for t in trades}),
            "spanSecs": (max(t.get("timestamp", 0) for t in trades) - min(t.get("timestamp", 0) for t in trades)) if trades else 0,
            "whales": sum(1 for t in trades if _num(t.get("size")) * _num(t.get("price")) >= 1000),
        }
    except Exception as ex:  # noqa: BLE001
        errs.append(f"tape: {ex}")
        data["tape"] = []
        data["tapeStats"] = {}

    # 3. Top traders (all-time volume leaderboard)
    try:
        lb = get(LB + "/volume", {"window": "all", "limit": 25})
        data["leaderboard"] = [
            {
                "wallet": r.get("proxyWallet"),
                "name": r.get("pseudonym") or r.get("name") or "?",
                "volume": round(_num(r.get("amount")), 0),
            }
            for r in lb
        ]
    except Exception as ex:  # noqa: BLE001
        errs.append(f"leaderboard: {ex}")
        data["leaderboard"] = []

    # 4. Order books for the top outcomes of the 4 hottest events
    books = {}
    want = []
    for e in data["events"][:4]:
        for m in e["markets"]:
            for i, tid in enumerate(m["tokenIds"][:2]):
                want.append((tid, m, i))
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(build_book, t): (t, m, i) for t, m, i in want[:24]}
        for f, (tid, m, i) in futs.items():
            try:
                books[tid] = f.result()
            except Exception as ex:  # noqa: BLE001
                errs.append(f"book {tid[:10]}: {ex}")
    data["books"] = books

    with _lock:
        _cache["data"] = data
        _cache["ts"] = time.time()
        _cache["errors"] = errs[-8:]


def loop():
    while True:
        t0 = time.time()
        try:
            refresh()
        except Exception as ex:  # noqa: BLE001
            with _lock:
                _cache["errors"] = [f"refresh: {ex}"]
        time.sleep(max(5.0, REFRESH_SECS - (time.time() - t0)))


STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/" or path.startswith("/index"):
            path = "/index.html"

        if path.startswith("/api/"):
            with _lock:
                payload = dict(_cache["data"])
                meta = {"ts": _cache["ts"], "ageSecs": round(time.time() - _cache["ts"], 1), "errors": _cache["errors"]}
            if path == "/api/state":
                payload["_meta"] = meta
                return self._send(200, json.dumps(payload))
            key = path.rsplit("/", 1)[-1]
            return self._send(200, json.dumps({"items": payload.get(key, []), "_meta": meta}))

        fpath = os.path.join(STATIC, path.lstrip("/"))
        if os.path.isfile(fpath):
            ext = os.path.splitext(fpath)[1]
            with open(fpath, "rb") as f:
                return self._send(200, f.read(), MIME.get(ext, "application/octet-stream"))
        return self._send(404, json.dumps({"error": "not found"}))


if __name__ == "__main__":
    print("warming cache from live Polymarket APIs...")
    refresh()
    threading.Thread(target=loop, daemon=True).start()
    print(f"PolyGM prototype on http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
