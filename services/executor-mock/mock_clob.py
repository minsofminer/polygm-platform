#!/usr/bin/env python3
"""Executor mock — a stand-in CLOB V2 venue for local dev and CI (P04).

Stdlib only. It exists so the D5 sequence (risk gate → sign → POST → maybe die → reconcile → WSS) is
*executed* in tests rather than drawn on a slide, and so it can execute the nastiest branch in the
system: a POST that times out after the venue already accepted the order.

It is deliberately NOT a fake that always says yes:
  * it validates V2 order shape and rejects V1 (`version != "v2"`) the way the real client does;
  * it enforces tick size and minimum_order_size, so a client that rounds the wrong way gets a venue error
    instead of a silent fill;
  * it rejects a price/size float that cannot round-trip to the integer we sent — the only way to test the
    `to_float_for_sdk` assertion end-to-end instead of trusting it;
  * it has scenarios: `timeout_after_accept`, `reject`, `partial_fill`, `rate_limit`, `unreachable`,
    `builder_disabled` (P13 D7.7);
    P06 added `ghost_order` (a cancel that says success and changes nothing) and `cancel_races_fill` (a
    cancel that loses to a fill), because D3's reconciliation cases are only testable if the venue can be
    *wrong* in those specific ways;
  * it takes a V2 batch (`POST /v1/orders` with {orders: [...]}), enforces the 15-item cap, and answers
    per item, so partial failure is a response shape we handle rather than a story we tell.

Every scenario models a venue that is behaving *plausibly badly*, never randomly: a random mock produces
tests that pass for the wrong reason.

`ScenarioTransport` is the in-process Transport used by unit tests; `serve()` exposes the same state over
HTTP so `make dev` and the compose stack can drive it, and so the HTTP path itself has one test.
"""
from __future__ import annotations
import argparse, json, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

TICKS = {"0.001": 1000, "0.01": 10000}          # tick size -> price_micro quantum
MIN_ORDER_SHARES = 5 * 10**6                      # minimum_order_size 5 (measured, P01)


def _now_ms() -> int:
    return int(time.time() * 1000)


class MockClob:
    def __init__(self, *, default_scenario: str = "accept") -> None:
        self.lock = threading.Lock()
        self.orders: dict[str, dict] = {}          # venue orderID -> order
        self.by_hash: dict[str, str] = {}          # client_order_hash -> venue orderID
        self.trades: list[dict] = []
        self.scenario = default_scenario
        self.scenario_args: dict = {}
        self.requests: list[str] = []
        self.cancelled_all = 0
        self.cancelled = 0
        self.batch_requests = 0
        self.cancel_requests = 0

    # ---------------------------------------------------------------- scenarios
    def set_scenario(self, s: str, **kw) -> None:
        with self.lock:
            self.scenario = s
            self.scenario_args = kw

    # ---------------------------------------------------------------- orders
    def post_order(self, signed: dict) -> dict:
        with self.lock:
            self.requests.append(signed.get("version", "?"))
            scen = self.scenario
        if scen == "unreachable":
            raise ConnectionError("mock: venue unreachable")
        if scen == "timeout_after_accept":
            # accepted-and-then-lost: the response never arrives, but the order IS live. This is the case
            # that turns "retry the POST" into a duplicate order.
            self._accept(signed, acknowledged=False)
            # `delay_ms` exists so a chaos harness can land a real SIGKILL inside the request. Without it the
            # window here is a few microseconds and the "mid-flight" kill is a race we win by accident.
            delay = int(self.scenario_args.get("delay_ms") or 0)
            if delay:
                time.sleep(delay / 1000.0)
            raise TimeoutError("mock: read timeout after accept")
        if scen == "rate_limit":
            return {"success": False, "code": "rate_limited",
                    "message": "mock: clob rate limit (read_timeout backoff path)"}
        err = self._validate(signed)
        if err:
            return err
        if scen == "builder_disabled":
            # A disabled builder code is the venue's refusal, not ours: the order is well-formed and the account
            # is funded, and the code the order carries has been switched off. P13 D7.7 needs it to be legible
            # ("orders rejected, users informed, alarm fired, revenue dashboard shows the drop").
            return {"success": False, "code": "builder_code_not_allowed",
                    "message": "mock: builder code %s is disabled" % (str(signed.get("b") or "")[:18])}
        if scen == "reject":
            return {"success": False, "code": "not_enough_balance_or_allowance",
                    "message": "mock: rejection for the specified reason"}
        oid = self._accept(signed, acknowledged=True)
        return {"success": True, "orderID": oid, "status": "live"}

    def post_orders(self, items: list) -> list[dict]:
        """V2 batch: at most 15 orders, answered positionally, each item independent.

        The positional contract is the whole point: our reader maps results back to intents BY INDEX and
        treats a missing answer as UNCERTAIN. A mock that answered as a dict keyed by order id would let
        that rule go untested.
        """
        out: list[dict] = []
        for it in items:
            order = it.get("order") if isinstance(it, dict) else None
            if not isinstance(order, dict):
                out.append({"success": False, "code": "malformed", "message": "mock: item has no order"})
                continue
            try:
                out.append(self.post_order(order))
            except (TimeoutError, ConnectionError) as e:
                # The batch is not atomic. Some items exist, and saying "the batch failed" is the lie that
                # produces duplicate orders, so each item that may exist answers as a per-item timeout.
                out.append({"success": False, "code": "timeout" if isinstance(e, TimeoutError) else "unreachable",
                            "message": "mock: %s (this item may exist)" % e})
        return out

    def cancel(self, order_id: str) -> dict:
        with self.lock:
            scen = self.scenario
            o = self.orders.get(order_id)
        if o is None:
            # "not found" is NOT an error for our purposes: cancelling something that is not there is what
            # the user asked for, so it resolves as success. The venue's own answer is different and the
            # reconciler must not read a 404 as "the order still exists".
            return {"success": True, "code": "not_found", "message": "mock: nothing to cancel",
                    "canceled": False}
        if scen == "cancel_races_fill":
            with self.lock:
                total = round(o["payload"]["size"] * 10**6)
                o["size_matched"] = total
                o["status"] = "matched"
                self.trades.append({"tradeID": "0x" + uuid.uuid4().hex[:16], "orderID": order_id,
                                    "price": o["payload"]["price"], "size": o["payload"]["size"],
                                    "side": o["side"], "maker": True, "timestamp": _now_ms() // 1000,
                                    "status": "matched", "size_micro": total,
                                    "notional_micro": round(o["payload"]["price"] * o["payload"]["size"]
                                                            * 10**6)})
            return {"success": False, "code": "already_filled",
                    "message": "mock: filled before the cancel arrived"}
        if scen == "ghost_order":
            # Says yes, does nothing: the order stays live. This is the case that makes a "cancelled" row in
            # our DB a lie, and it is why `ghost_order` reconciliation exists.
            return {"success": True, "code": "canceled", "message": "mock: ack, not applied",
                    "canceled": True, "ghost": True}
        with self.lock:
            was = o["status"]
            if was in ("live", "partial", "delayed"):
                o["status"] = "canceled"
                self.cancelled += 1
            elif was in ("matched", "canceled"):
                return {"success": True, "code": was, "message": "mock: already terminal", "canceled": False}
            else:
                o["status"] = "canceled"
                self.cancelled += 1
        return {"success": True, "code": "canceled", "message": "mock: canceled", "canceled": True}

    def trades_for(self, order_id: str) -> list[dict]:
        with self.lock:
            return [dict(t) for t in self.trades if t.get("orderID") == order_id]

    def _validate(self, signed: dict) -> dict | None:
        """Venue-side validation. Returns an error dict, or None when the order is acceptable."""
        if signed.get("version") != "v2":
            return {"success": False, "code": "order_version_mismatch",
                    "message": "mock: V1 payload rejected (P04 constraint 1: V1 endpoints are dead)"}
        price, size = signed.get("price"), signed.get("size")
        if not isinstance(price, float) or not isinstance(size, float):
            return {"success": False, "code": "malformed", "message": "mock: price/size must be SDK floats"}
        # Deliberately the SAME rule the client applies (round(v*1e6) must reproduce v exactly), not a
        # tolerance-based one. A tolerance like 1e-6 at 1e6 scale is ~1e-12 relative — smaller than the
        # ULP of the double — so it accepts every float and the check is decoration.
        for field, val, scale in (("price", price, 10**6), ("size", size, 10**6)):
            back = round(val * scale)
            if back / scale != val or not (0 < back < 10**6 if field == "price" else back > 0):
                return {"success": False, "code": "rounding_boundary",
                        "message": f"mock: {field}={val!r} cannot round-trip to an integer micro unit"}
        tick = TICKS.get(str(signed.get("tick_size", "0.001")))
        pm = round(price * 10**6)
        if tick and pm % tick:
            return {"success": False, "code": "invalid_tick_size",
                    "message": f"mock: price {pm} is not a multiple of tick {tick}"}
        if pm <= 0 or pm >= 10**6:
            return {"success": False, "code": "invalid_price", "message": "mock: price outside (0,1)"}
        sm = round(size * 10**6)
        if sm < MIN_ORDER_SHARES:
            return {"success": False, "code": "invalid_size",
                    "message": f"mock: size below minimum_order_size ({MIN_ORDER_SHARES} shares micro)"}
        return None

    def _accept(self, signed: dict, *, acknowledged: bool) -> str:
        oid = "0x" + uuid.uuid4().hex
        h = signed.get("client_order_hash") or _hash_of(signed)
        self.orders[oid] = {"orderID": oid, "client_order_hash": h, "status": "live",
                            "size_matched": 0, "acknowledged": acknowledged,
                            "placed_ms": _now_ms(), "payload": signed,
                            "average_price": signed["price"], "side": signed["side"],
                            "token_id": signed["token_id"]}
        self.by_hash[h] = oid
        return oid

    def find_order(self, client_order_hash: str) -> dict | None:
        with self.lock:
            oid = self.by_hash.get(client_order_hash)
            if oid is None:
                return None
            o = dict(self.orders[oid])
            o["status"] = o["status"]
            return {"orderID": oid, "status": o["status"], "size_matched": o["size_matched"],
                    "acknowledged": o["acknowledged"],
                    "average_price": o.get("average_price"),
                    "size_micro": round(o["payload"]["size"] * 10**6),
                    "builder": o["payload"].get("builder", ""),
                    "maker": o["payload"].get("maker", ""),
                    "token_id": o.get("token_id", ""),
                    "side": o.get("side", ""),
                    "placed_ms": o.get("placed_ms", 0),
                    "size_micro": round(o["payload"]["size"] * 10**6)}

    def cancel_all(self) -> dict:
        with self.lock:
            n = 0
            for o in self.orders.values():
                if o["status"] in ("live", "partial"):
                    o["status"] = "canceled"
                    n += 1
            self.cancelled_all += n
            return {"canceled": n}

    # ---------------------------------------------------------------- fills
    def fill(self, order_id: str, *, price: float, size: float, maker: bool = True) -> dict:
        """Simulate a match. A venue can fill part of an order, and at a better price than asked; tests
        use this to assert our average-price accounting rather than assuming it."""
        with self.lock:
            o = self.orders[order_id]
            sm = round(size * 10**6)
            total = round(o["payload"]["size"] * 10**6)
            o["size_matched"] = min(total, o.get("size_matched", 0) + sm)
            o["average_price"] = price
            o["status"] = "matched" if o["size_matched"] >= total else "live"
            tr = {"tradeID": "0x" + uuid.uuid4().hex[:16], "orderID": order_id,
                  "price": price, "size": size, "side": o["side"], "maker": maker,
                  "timestamp": _now_ms() // 1000, "status": "matched",
                  "size_micro": sm, "notional_micro": round(price * size * 10**6)}
            self.trades.append(tr)
            return tr

    #: every status the mock can report; tests assert the product maps ALL of them
    EMITS = ("live", "matched", "canceled", "expired", "delayed")

    def tick_scenario(self) -> None:
        """Apply the scenario's automatic fill behaviour (called by the HTTP layer and by tests)."""
        with self.lock:
            scen = self.scenario
            for o in self.orders.values():
                if scen == "partial_fill" and o["status"] == "live":
                    o["status"] = "matched"
                    # The 40% is computed in MICRO units, not as a float: `size * 0.4` on 6 shares is
                    # 2.4000000000000004 in binary floating point, and the reconciler's `_exact_micro` round-trips
                    # every venue number on the money path by design, so the trade would be refused as
                    # `ambiguous_settlement` and never booked. That made this mock — whose whole job is to model a
                    # venue behaving *plausibly* — the one venue shape the product is built to refuse: P13's
                    # recovery loop found 40% of its `booked` runs producing no fill at all, with an
                    # `ambiguous_settlement` case sitting open next to them. `size_matched` was already clean here,
                    # so the mock was printing the same fill two ways and only one of them was on the grid.
                    sm = round(o["payload"]["size"] * 10**6 * 0.4)
                    price_micro = round(o["payload"]["price"] * 10**6)
                    self.trades.append({"tradeID": "0x" + uuid.uuid4().hex[:16],
                                        "orderID": o["orderID"], "price": o["payload"]["price"],
                                        "size": sm / 10**6, "size_micro": sm,
                                        "notional_micro": price_micro * sm // 10**6,
                                        "side": o["side"],
                                        "maker": True, "timestamp": _now_ms() // 1000,
                                        "status": "trade"})
                    o["size_matched"] = sm

    def snapshot(self) -> dict:
        with self.lock:
            return {"orders": {k: {"status": v["status"], "size_matched": v.get("size_matched", 0),
                                   "acknowledged": v["acknowledged"],
                                   "builder": v["payload"].get("builder", ""),
                                   "maker": v["payload"].get("maker", ""),
                                   "token_id": v.get("token_id", ""),
                                   "side": v.get("side", ""),
                                   "price": v.get("average_price"),
                                   "size_micro": round(v["payload"]["size"] * 10**6),
                                   "placed_ms": v.get("placed_ms", 0),
                                   "client_order_hash": v.get("client_order_hash", "")}
                               for k, v in self.orders.items()},
                    "cancelled": self.cancelled, "batch_requests": self.batch_requests,
                    "trades": self.trades, "scenario": self.scenario,
                    "requests": list(self.requests), "cancelled_all": self.cancelled_all}


def _hash_of(signed: dict) -> str:
    core = json.dumps({k: signed.get(k) for k in ("token_id", "price", "size", "side", "maker")},
                      sort_keys=True, separators=(",", ":"))
    import hashlib
    return "0x" + hashlib.sha256(core.encode()).hexdigest()


class ScenarioTransport:
    """In-process Transport for polygm_core.executor.executor.submit / reconcile."""

    def __init__(self, mock: MockClob, *, signer_pubkey: str = "0x" + "ab" * 20) -> None:
        self.mock = mock
        self.signer_pubkey = signer_pubkey
        self.post_calls = 0
        self.lookup_calls = 0
        self.cancel_calls = 0
        self.batch_calls = 0

    def post_order(self, signed: dict, *, timeout_ms: int) -> dict:
        self.post_calls += 1
        signed = dict(signed)
        assert signed.get("client_order_hash"), "executor must stamp the hash it will reconcile by"
        return self.mock.post_order(signed)

    def find_order(self, client_order_hash: str, *, timeout_ms: int) -> dict | None:
        self.lookup_calls += 1
        self.mock.tick_scenario()
        return self.mock.find_order(client_order_hash)

    def cancel_all(self, *, timeout_ms: int) -> dict:
        return self.mock.cancel_all()

    # ---- P06: the same seams the HTTP transport exposes, so a unit test and the chaos harness exercise
    # ---- the identical venue behaviours. A venue adapter that only exists over a socket would leave the
    # ---- batch/partial-failure logic tested by exactly one of the two paths.
    def post_batch(self, items: list[dict], *, timeout_ms: int) -> dict:
        self.post_calls += 1
        self.batch_calls += 1
        if len(items) > 15:
            raise ValueError("BATCH_TOO_LARGE")
        return {"orders": self.mock.post_orders(items), "batch": True}

    def cancel(self, order_id: str, *, timeout_ms: int = 2000) -> dict:
        self.cancel_calls += 1
        return self.mock.cancel(order_id)

    def cancel_batch(self, order_ids: list[str], *, timeout_ms: int = 2000) -> dict:
        return {"results": [self.mock.cancel(i) for i in order_ids]}

    def list_orders(self, *, timeout_ms: int = 2000) -> dict:
        self.mock.tick_scenario()
        return self.mock.snapshot()

    def snapshot(self, *, timeout_ms: int = 2000) -> dict:
        return self.mock.snapshot()

    def trades_for(self, order_id: str, *, timeout_ms: int = 2000) -> list[dict]:
        return self.mock.trades_for(order_id)

    def recent_trades(self, *, limit: int = 50, timeout_ms: int = 2000) -> list[dict]:
        return list(self.mock.snapshot()["trades"][-limit:])


# ------------------------------------------------------------------ HTTP layer
class Handler(BaseHTTPRequestHandler):
    mock: MockClob = None                      # injected by serve()

    def log_message(self, *a) -> None:           # keep CI output clean; the app logs instead
        pass

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        u = urlparse(self.path)
        if u.path == "/healthz":
            return self._send(200, {"ok": True, "service": "executor-mock"})
        if u.path == "/v1/orders":
            self.mock.tick_scenario()
            return self._send(200, self.mock.snapshot())
        if u.path.startswith("/v1/order-by-hash/"):
            h = u.path.rsplit("/", 1)[-1]
            found = self.mock.find_order(h)
            return self._send(200 if found else 404, found or {"error": "not_found"})
        if u.path == "/v1/trades":
            self.mock.tick_scenario()
            q = parse_qs(u.query)
            limit = int((q.get("limit") or ["50"])[0])
            oid = (q.get("orderID") or [""])[0]
            if oid:
                return self._send(200, {"trades": self.mock.trades_for(oid)})
            return self._send(200, {"trades": self.mock.trades[-limit:]})
        return self._send(404, {"error": "no_route", "path": u.path})

    def do_POST(self) -> None:
        u = urlparse(self.path)
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if u.path == "/v1/scenario":
            self.mock.set_scenario(body.get("scenario", "accept"), **{k: v for k, v in body.items()
                                                                     if k != "scenario"})
            return self._send(200, {"ok": True, "scenario": self.mock.scenario})
        if u.path == "/v1/orders":
            if isinstance(body, dict) and isinstance(body.get("orders"), list):
                items = body["orders"]
                if len(items) > 15:
                    # The cap is refused, not truncated: a client that silently drops the 16th order has
                    # created a user-visible inconsistency, which is worse than an error.
                    return self._send(400, {"success": False, "code": "batch_too_large",
                                           "message": "mock: at most 15 orders per request, got %d" % len(items)})
                self.mock.batch_requests += 1
                try:
                    return self._send(200, {"orders": self.mock.post_orders(items), "batch": True})
                except (TimeoutError, ConnectionError) as e:
                    return self._send(504, {"success": False, "code": "gateway_timeout",
                                           "message": "mock: %s" % e})
            try:
                return self._send(200, self.mock.post_order(body))
            except TimeoutError as e:
                return self._send(504, {"success": False, "code": "gateway_timeout",
                                        "message": "mock: %s (the order may or may not exist)" % e})
            except ConnectionError as e:
                return self._send(503, {"success": False, "code": "unreachable",
                                        "message": "mock: %s" % e})
        if u.path == "/v1/cancel":
            self.mock.cancel_requests += 1
            ids = body.get("orderIDs") or ([body["orderID"]] if body.get("orderID") else [])
            if len(ids) > 15:
                return self._send(400, {"success": False, "code": "batch_too_large",
                                       "message": "mock: at most 15 cancels per request"})
            return self._send(200, {"results": [self.mock.cancel(i) for i in ids]})
        if u.path == "/v1/cancel-all":
            return self._send(200, self.mock.cancel_all())
        if u.path == "/v1/fill":
            oid = body.get("orderID")
            if oid not in self.mock.orders:
                return self._send(404, {"error": "unknown_order"})
            return self._send(200, self.mock.fill(oid, price=float(body.get("price", 0.5)),
                                                  size=float(body.get("size", 1.0)),
                                                  maker=bool(body.get("maker", True))))
        return self._send(404, {"error": "no_route", "path": u.path})


def serve(host: str = "0.0.0.0", port: int = 8090) -> None:
    Handler.mock = MockClob()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(json.dumps({"service": "executor-mock", "listening": f"{host}:{port}",
                      "note": "stdlib mock; not a real venue; scenarios: accept/reject/partial_fill/"
                              "timeout_after_accept/rate_limit/unreachable"}), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8090)
    a = ap.parse_args()
    serve(a.host, a.port)
