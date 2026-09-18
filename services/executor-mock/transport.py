#!/usr/bin/env python3
"""HttpTransport — talks to the mock over a real socket (or, later, to the real V2 client).

The point of this file is that `executor.submit`'s contract is "*may* raise TimeoutError", and the only way
to know our handling survives that is to raise it across an actual socket rather than in-process. A
Transport implemented with a method call cannot distinguish "the venue was slow" from "my test was slow".
"""
from __future__ import annotations
import json
import urllib.error
import urllib.request


class HttpTransport:
    def __init__(self, base_url: str, *, client_order_hash: str | None = None,
                 user_agent: str = "polygm-executor/0.4") -> None:
        self.base = base_url.rstrip("/")
        self.client_order_hash = client_order_hash
        self.user_agent = user_agent
        self.post_calls = 0
        self.lookup_calls = 0
        self.last_status: int | None = None

    def _call(self, method: str, path: str, body: dict | None, timeout_ms: int):
        req = urllib.request.Request(self.base + path,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"content-type": "application/json",
                                              "user-agent": self.user_agent},
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout_ms / 1000) as r:
                self.last_status = r.status
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            # 504/503 from the mock mean "we do not know"; everything else is an answer.
            raw = e.read()
            self.last_status = e.code
            payload = json.loads(raw) if raw else {}
            if e.code in (503, 504):
                if e.code == 504:
                    raise TimeoutError(payload.get("message", "gateway timeout")) from e
                raise ConnectionError(payload.get("message", "unreachable")) from e
            return payload if payload else {"success": False, "code": f"http_{e.code}",
                                            "message": "mock http error"}
        except TimeoutError as e:
            raise TimeoutError(f"read timeout after {timeout_ms}ms") from e
        except json.JSONDecodeError as e:
            raise ConnectionError(f"non-JSON response from venue: {e}") from e

    # ------------------------------------------------------------------ hops
    def post_order(self, signed: dict, *, timeout_ms: int) -> dict:
        # The payload already carries client_order_hash (the executor puts it there). This transport does
        # not invent one - doing so is what let a "recovery" test pass against a hash nobody indexed.
        self.post_calls += 1
        return self._call("POST", "/v1/orders", dict(signed), timeout_ms)

    def find_order(self, client_order_hash: str, *, timeout_ms: int) -> dict | None:
        self.lookup_calls += 1
        try:
            r = self._call("GET", "/v1/order-by-hash/" + client_order_hash, None, timeout_ms)
        except FileNotFoundError:
            return None
        except (TimeoutError, ConnectionError):
            raise
        return r if r and "orderID" in r else None

    def cancel_all(self, *, timeout_ms: int) -> dict:
        return self._call("POST", "/v1/cancel-all", {}, timeout_ms)

    # ------------------------------------------------------- P06: batch, cancels, reconciliation reads
    def post_batch(self, items: list[dict], *, timeout_ms: int) -> dict:
        """`POST /v1/orders` with {orders:[{order,orderType}]} — at most 15 items (P06 D2).

        The cap is enforced HERE as well as in `clob_v2.plan_batch`, deliberately: the batch planner and the
        transport are written by different hands at different times, and the second check is the one that
        fires when the first is wrong. A 400 from the venue is a bug report; a silent truncation is a user
        whose order never existed.
        """
        if len(items) > 15:
            raise ValueError("BATCH_TOO_LARGE: the venue accepts at most 15 orders per request, got %d"
                             % len(items))
        self.post_calls += 1
        return self._call("POST", "/v1/orders", {"orders": items}, timeout_ms)

    def cancel(self, order_id: str, *, timeout_ms: int = 2000) -> dict:
        return self._call("POST", "/v1/cancel", {"orderID": order_id}, timeout_ms)

    def cancel_batch(self, order_ids: list[str], *, timeout_ms: int = 2000) -> dict:
        if len(order_ids) > 15:
            raise ValueError("BATCH_TOO_LARGE")
        return self._call("POST", "/v1/cancel", {"orderIDs": list(order_ids)}, timeout_ms)

    def list_orders(self, *, timeout_ms: int = 2000) -> dict:
        return self._call("GET", "/v1/orders", None, timeout_ms)

    def trades_for(self, order_id: str, *, timeout_ms: int = 2000) -> list[dict]:
        r = self._call("GET", "/v1/trades?orderID=" + order_id, None, timeout_ms)
        return list(r.get("trades") or []) if isinstance(r, dict) else []

    def recent_trades(self, *, limit: int = 50, timeout_ms: int = 2000) -> list[dict]:
        r = self._call("GET", "/v1/trades?limit=%d" % limit, None, timeout_ms)
        return list(r.get("trades") or []) if isinstance(r, dict) else []

    # ------------------------------------------------------------------ test helpers
    def set_scenario(self, name: str, timeout_ms: int = 2000, **kw) -> dict:
        return self._call("POST", "/v1/scenario", dict(kw, scenario=name), timeout_ms)

    def snapshot(self, timeout_ms: int = 2000) -> dict:
        return self._call("GET", "/v1/orders", None, timeout_ms)

    def fill(self, order_id: str, *, price: float, size: float, timeout_ms: int = 2000) -> dict:
        return self._call("POST", "/v1/fill", {"orderID": order_id, "price": price, "size": size},
                          timeout_ms)
