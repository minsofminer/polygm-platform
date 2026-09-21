#!/usr/bin/env python3
"""P13 D3 — record the shapes our contracts promise, so a rename upstream fails our build.

    python3 tools/build-p13-contract-fixtures.py            # (re)record
    python3 tools/build-p13-contract-fixtures.py --check    # fail if the fixtures are stale

The kit's sentence is "a field rename upstream must fail the build". Two renames matter, and they are different
failures:

  * **The venue renames a field** in a payload we parse. `tests/fixtures/p05/` holds the recorded raw payloads
    and their shapes, and `tests/test_ingest.py` replays them — but replay proves the *input* still parses, not
    that our normalised output still carries what the product reads. So this tool records the *normalised*
    shape: the keys and value types `book_from_clob`, `fill_from_rest`, `market_from_gamma` and friends produce
    today. A venue rename that our parser tolerates silently (a field that stops appearing) changes this shape
    and turns the contract test red, which is the failure the kit is asking for.
  * **We rename a field in a response.** The API's read routes have a response shape the web and the bot read;
    that shape is recorded per route the same way. A rename is then a test failure rather than a broken screen
    somebody notices on a phone.

A shape is `{key: type}` recursively, with lists recorded as `["<n>", <shape of the first element>]` — enough to
catch a rename, a type change or a vanished key, and deliberately not so much that a new market in the seed data
turns the suite red.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "packages"), str(ROOT / "services" / "ingest"), str(ROOT / "services" / "api"),
                str(ROOT / "tools"), str(ROOT / "tests")]

FX_P05 = ROOT / "tests" / "fixtures" / "p05"
OUT = ROOT / "tests" / "fixtures" / "p13" / "contracts.json"

NOW_MS = 1_800_000_000_000                     # a fixed clock: `age_ms` must not enter a recorded shape


def shape(value, *, depth: int = 0):
    """A structural summary: types, keys, and the shape of the first element of a list."""
    if depth > 6:
        return "<deep>"
    if isinstance(value, dict):
        return {k: shape(v, depth=depth + 1) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return ["<%d>" % len(value)] + ([shape(value[0], depth=depth + 1)] if value else [])
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if value is None:
        return "null"
    return "str"


def normalised_shapes() -> dict:
    import normalise as N
    out: dict = {}

    # Each normaliser is called defensively: a fixture whose payload this parser is not for (the CLOB market
    # body is not a Gamma row) must not stop the recorder from pinning the ones that are. What is recorded is
    # whatever the parser produced, error string included, so a parser that starts failing shows up as a diff.
    def safe(key, fn):
        try:
            out[key] = shape(fn())
        except Exception as e:                                     # noqa: BLE001 — recorded, not swallowed
            out[key] = "ERROR: %s: %s" % (type(e).__name__, str(e)[:80])

    def load(name):
        p = FX_P05 / name
        return json.loads(p.read_text()) if p.exists() else None

    book = load("clob_book.json")
    if book:
        safe("clob_book", lambda: N.book_from_clob(book, NOW_MS))
    book_shape = load("clob_book.shape.json")
    if book_shape:
        out["clob_book_raw"] = shape(book_shape)
    hist = load("clob_history.json")
    if hist:
        safe("clob_history", lambda: N.history_points(hist))
    gm = load("gamma_markets.json")
    if isinstance(gm, list) and gm:
        safe("gamma_markets", lambda: N.market_from_gamma(gm[0]))
    elif isinstance(gm, dict):
        safe("gamma_markets", lambda: N.market_from_gamma(gm))
    trades = load("data_trades.json")
    if isinstance(trades, list) and trades:
        safe("data_trade_row", lambda: N.fill_from_rest(trades[0]))
    elif isinstance(trades, dict) and trades.get("data"):
        safe("data_trade_row", lambda: N.fill_from_rest(trades["data"][0]))
    frames = load("ws_frames.json")
    if isinstance(frames, dict):
        out["ws_frame_keys"] = shape({k: "<frame>" for k in sorted(frames)})
    return out


ROUTES = ["/healthz", "/readyz", "/v1/markets?limit=5", "/v1/markets/0xM1",
          "/v1/markets/0xM1/book?depth=5", "/v1/markets/0xM1/fills?limit=5",
          "/v1/leaderboard?limit=5", "/v1/referrals/terms", "/v1/alerts", "/v1/markets/zzz"]


def response_shapes() -> dict:
    import contextlib
    from conftest import import_app
    app = import_app("p13-contracts")
    from fastapi.testclient import TestClient
    out: dict = {}
    with contextlib.ExitStack() as stack:
        client = stack.enter_context(TestClient(app.app, raise_server_exceptions=False))
        for r in ROUTES:
            res = client.get(r)
            try:
                body = res.json()
            except Exception:                                  # noqa: BLE001 — a non-JSON body is the shape
                body = "<non-json>"
            out[r] = {"status": res.status_code, "shape": shape(body),
                      "content_type": str(res.headers.get("content-type", "")).split(";")[0]}
    return out


def venue_payload_shape() -> dict:
    from polygm_core.executor.executor import OrderIntent, build_signed_payload
    i = OrderIntent(id="contract", user_id="u-contract", token_id="token-contract", side="BUY",
                    price_micro=500_000, size_shares_micro=10_000_000, idempotency_key="k-contract",
                    condition_id="0xcondition", tick_size="0.01", builder_code="0x" + "b" * 8)
    p = build_signed_payload(i, client_order_hash="0x" + "c" * 64)
    return {k: shape(v) for k, v in sorted(p.items())}


def build() -> dict:
    return {
        "generated_by": "tools/build-p13-contract-fixtures.py",
        "why": "shapes our contracts promise: normalised venue payloads, API response bodies, the order struct",
        "normalised": normalised_shapes(),
        "responses": response_shapes(),
        "venue_payload": venue_payload_shape(),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="record the P13 D3 contract shapes")
    ap.add_argument("--check", action="store_true", help="fail if the recorded shapes no longer match")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    current = build()
    target = Path(a.out)
    if a.check:
        if not target.exists():
            print("MISSING %s — run the tool without --check" % target)
            return 1
        recorded = json.loads(target.read_text())
        stale = []
        for section in ("normalised", "responses", "venue_payload"):
            if recorded.get(section) != current.get(section):
                for key in sorted(set(recorded.get(section, {})) | set(current.get(section, {}))):
                    if recorded.get(section, {}).get(key) != current.get(section, {}).get(key):
                        stale.append("%s.%s" % (section, key))
        if stale:
            print("STALE contract shapes: %s" % ", ".join(stale))
            print("If the change is intended, re-record: python3 tools/build-p13-contract-fixtures.py")
            return 1
        print("contract shapes current: %d normalised, %d responses, %d venue fields"
              % (len(current["normalised"]), len(current["responses"]), len(current["venue_payload"])))
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n")
    print("wrote %s: %d normalised shapes, %d responses, %d venue fields"
          % (target, len(current["normalised"]), len(current["responses"]), len(current["venue_payload"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
