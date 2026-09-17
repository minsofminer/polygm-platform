"""`api` — the public HTTP surface. Stateless, horizontally scalable, and it has NO key: rule 1 of P04.

What "no secrets" means concretely here: this process's environment may contain a DB credential and
nothing else. It cannot sign, so it cannot be the thing that loses money; it can only enqueue an intent.
That is also why `POST /v1/orders` returns 202 and a state, never a venue answer — the api process
literally does not know yet, and pretending otherwise is how you end up telling a user their order is
live when the executor crashed.

Deps are intentionally minimal and the DB layer is raw SQL: no ORM decides to emit an UPDATE against a
ledger table, and the append-only rule (P04 rule 4) is enforced in the schema, not in Python politeness.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from fastapi import Body, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from polygm_core.config.flags import FlagStore, Flags
from polygm_core.ledger.ledger import IntentState
from polygm_core.money.cents import MoneyError, ScaleError, fmt_usdc, parse_usdc, price_ticks
from polygm_core.risk.gate import Intent, Limits, MarketState, evaluate, norm_tick
from polygm_core.risk.idempotency import Idem

ROOT = Path(__file__).resolve().parents[2]

# --------------------------------------------------------------------------- #
# error envelope — one shape everywhere (D4). `message` is user-safe by construction:
# internal detail goes to logs with the request id, never into the body.
CODES = {
    "RISK_HALT": ("trading is temporarily disabled", 503, True),
    "MARKET_NOT_ACCEPTING": ("this market is not accepting orders", 409, False),
    "NO_ORDER_BOOK": ("this market has no order book", 409, False),
    "STALE_QUOTE": ("prices are stale; try again", 503, True),
    "BAD_SIDE": ("side must be BUY or SELL", 422, False),
    "UNKNOWN_TICK": ("market tick size unrecognised", 503, True),
    "OFF_TICK": ("price is not on a valid tick", 422, False),
    "BAD_MARKET_META": ("could not read market limits", 503, True),
    "BELOW_MIN_SIZE": ("order is below the minimum size", 422, False),
    "ZERO_SIZE": ("size must be greater than zero", 422, False),
    "BAD_AMOUNT": ("amount is outside the supported scale", 422, False),
    "OVER_ORDER_CAP": ("order is above the per-order limit", 403, False),
    "PRICE_FAR_FROM_MID": ("price is far from the market", 422, False),
    "TOO_MANY_OPEN": ("too many open orders", 429, True),
    "DAILY_CAP": ("24h limit reached", 403, False),
    "IDEM_CONFLICT": ("idempotency key reused with a different body", 409, False),
    "IDEM_IN_PROGRESS": ("the first attempt is still running", 409, True),
    "RISK_UNAVAILABLE": ("risk check unavailable", 503, True),
    "SIGNER_UNAVAILABLE": ("signing is unavailable; trading disabled", 503, True),
    "NOT_FOUND": ("no such market", 404, False),
    "BAD_REASON": ("a kill-switch change needs a reason of 4-400 characters", 422, False),
    # The three below exist so that NOTHING in this API answers outside the envelope. Without them a
    # malformed body gets FastAPI's default 422 `{"detail":[...]}` and a crash gets `{"detail":"Internal
    # Server Error"}` - both leak-free, but a client written against the contract breaks on both, and a
    # 500 with no requestId is an unsearchable log line.
    "IDEM_KEY_REQUIRED": ("mutating endpoints require an Idempotency-Key of 8-128 chars "
                          "[A-Za-z0-9_-]", 400, False),
    "VALIDATION": ("request body or parameters are not valid for this endpoint", 422, False),
    # INTERNAL is retryable ON PURPOSE, but only with the same key: the write may have landed, and
    # "retry the idempotent request" is the honest advice. A new key would double-spend.
    "INTERNAL": ("request failed inside the service; retry with the same Idempotency-Key", 500, True),
}


# --------------------------------------------------------------------------- #
# Response tables, declared on the routes so the SERVED /openapi.json is complete rather than merely the
# hand-written contract. They live next to CODES because the two must be edited together: a status in CODES
# that no route declares is a client that cannot generate its error model, and tests/test_api.py asserts both
# directions of that.
#
# 500 is in `_INTERNAL` and merged into every table because the crash handler is app-wide, while each
# endpoint's own table lists only the statuses that endpoint can DECIDE on. FastAPI puts a 500 in the served
# document only when it is declared, so "implied by the handler" is not enough.
_INTERNAL = {500: {"description": "unexpected failure inside the service; the body is the envelope with a "
                                  "request id, never a Python message"}}

# The statuses POST /v1/orders can answer, derived from CODES so the two cannot drift: a code added to CODES
# without a status here is caught by test_every_documented_code_is_reachable_through_the_http_layer, and a
# status documented but never produced is caught by tools/check-openapi.py's "unused code" rule.
ORDER_RESPONSES = {
    202: {"description": "queued for the executor (NOT accepted at the venue)"},
    **{status: {"description": msg} for msg, status, _retry in CODES.values()},
}
READYZ_RESPONSES = {503: {"description": "not ready; the body lists why. Orchestrators read the status, "
                                         "humans read the list"}}
LIST_RESPONSES = {422: {"description": "limit above 100, unknown sortBy, or a malformed cursor"}}
TAPE_RESPONSES = {404: {"description": "no such market"}, 422: {"description": "limit above 500, or a "
                                                                               "non-numeric since"}}
# /healthz gets an explicit EMPTY table for one reason: tools/check-openapi.py audits "every documented
# operation has a table, and the two agree", and liveness is the operation where "it 500s" must be impossible
# - it has to keep answering 200 while the database is on fire. An empty dict is that statement, written down.
HEALTH_RESPONSES: dict[int, dict] = {}
KILL_RESPONSES = {422: {"description": "reason missing or outside 4-400 characters"},
                  503: {"description": "no admin token configured; the endpoint is closed, not open"}}
MARKET_RESPONSES = {404: {"description": "no such market"}}
BOOK_RESPONSES = {404: {"description": "no such market, or a market with no order book"},
                  422: {"description": "depth outside 1-400"}}
INTENT_RESPONSES = {404: {"description": "no such intent, or it belongs to another user (never 403: an "
                                         "id-probing endpoint must not confirm existence)"}}
# 500 for every operation except /healthz, which has nothing to fail on and must keep answering 200 while the
# DB is on fire. Applied in a loop so a tenth endpoint cannot forget it, and `del` so the loop variable does
# not survive into the module namespace where a reader would take it for state.
for _t in (READYZ_RESPONSES, LIST_RESPONSES, TAPE_RESPONSES, KILL_RESPONSES, MARKET_RESPONSES,
           BOOK_RESPONSES, INTENT_RESPONSES):
    _t.update(_INTERNAL)
del _t


def err(code: str, request_id: str, *, detail: str | None = None, where: list[str] | None = None
        ) -> JSONResponse:
    """One envelope shape, always. `detail` is accepted for internal callers but deliberately NOT put in the
    body: it is the free-text half where a Python message would leak, and `log` gets it instead.

    `where` is the exception, and the reason it is allowed is that it carries field NAMES and nothing else:
    "missing: price" tells a client what to fix without echoing what they sent (a token id, an address, a
    pasted key). Values never cross this line.
    """
    msg, status, retry = CODES.get(code, ("request failed", 400, False))
    if where:
        msg = msg + " (" + ", ".join(where) + ")"
    return JSONResponse({"error": {"code": code, "message": msg, "retryable": retry,
                                   "requestId": request_id}}, status_code=status,
                        headers={"Retry-After": "2"} if retry else {})


def _now_ms() -> int:
    return int(time.time() * 1000)


def _connect() -> sqlite3.Connection:
    db = os.environ.get("PGM_DB_PATH", str(ROOT / "var" / "polygm.db"))
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA foreign_keys=ON")
    return c


# The tables this process reads or writes. Not "nice to have": if `orders` is missing, the failure would
# surface as a 500 on the first order, in production, three days after a partial migration.
REQUIRED_TABLES = ("users", "markets", "tokens", "book_levels", "tape_trades", "order_intents", "orders",
                   "idempotency_keys", "cash_ledger", "kill_switch_state", "feature_flags")


def _require_schema(c: sqlite3.Connection) -> None:
    """The web process CHECKS for the schema and refuses to start without it. It does not create anything.

    There used to be an `_migrate()` here that executed db/migrations-sqlite/*.sql on every import. That is
    the classic "the app migrates itself" shortcut, and it fails twice: on a second boot it crashed with
    `table events already exists` (which is every `docker compose restart`, and was found by
    tools/envelope-demo.py rather than by a test — the tests always start from a fresh file), and on a
    partially migrated database it would apply half a schema and answer traffic on it.

    Schema changes belong to one job (`make migrate`, or the compose `migrate` service) because that job can
    be ordered, logged, and rolled back. A process that mutates schema on boot makes all three impossible.
    """
    have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [t for t in REQUIRED_TABLES if t not in have]
    if missing:
        raise RuntimeError(
            "database %s has no schema (missing: %s). Run `make migrate` — or, for Postgres, the compose "
            "`migrate` service — before starting the API. The API deliberately does not migrate at import."
            % (c.execute("PRAGMA database_list").fetchone()[2], ", ".join(missing)))


app = FastAPI(title="Openout API", version="1.0.0",
              description="Read endpoints are public or user-scoped. Every mutating endpoint requires an "
                          "Idempotency-Key header (P04 rule 5) and is versioned under /v1/.")
_db = _connect()
_require_schema(_db)
FLAGS = Flags.from_env()
LIMITS = Limits()           # the boot fallback; the per-request copy in place_order overlays the flag store
FLAGS_UNUSED = None         # noqa: F841 - kept so `import app` still exposes the boot snapshot for tooling
STORE = FlagStore(_db)          # boot defaults until the first successful read; readyz reports that
try:
    STORE.current()             # ...and the FIRST read happens HERE, at import, not on the first request.
except Exception:               # noqa: BLE001 - a DB that is down at boot must not stop the process: it
    pass                        # leaves boot_defaults_used True, which is the observable, honest state.
                                # Without this line a fresh pod answers readyz=503 forever and the
                                # orchestrator restart-loops it (found by tools/envelope-demo.py, which is
                                # exactly the kind of bug a unit test with a forced refresh cannot see).

# --- envelope for the two paths that never reach a route handler -------------------------------------------
# They must live AFTER `app` exists; a decorator on a name defined 50 lines below is a NameError at
# import time, which is the whole suite failing to start. Ordered here, not at the bottom, so the
# reader meets the error contract before any endpoint that can violate it.
@app.exception_handler(RequestValidationError)
def _on_validation(request: Request, exc: RequestValidationError):
    """FastAPI's default 422 body is `{"detail":[{"loc":..,"msg":..,"input":..}]}`.

    `input` is the client's own value, echoed back: it can be a token, an address, or anything else the user
    pasted, and echoing it into a response body is precisely the leak the "no secrets in errors" rule is
    about. So the message names WHICH field is wrong (loc is a path of keys, never a value) and stops there.
    """
    where = ", ".join(".".join(str(x) for x in e.get("loc", ()) if x not in ("body",)) for e in exc.errors())
    rid = getattr(request.state, "request_id", "-")
    msg, status, retry = CODES["VALIDATION"]
    return JSONResponse({"error": {"code": "VALIDATION",
                                  "message": msg + (" (%s)" % where if where else ""),
                                  "retryable": retry, "requestId": rid}}, status_code=status)


@app.exception_handler(Exception)
def _on_crash(request: Request, exc: Exception):
    """Last line of defence. The body is the envelope with a code; the Python message goes to the log, keyed
    by the request id, where a developer can read it. `type(exc).__name__` is not in the body either: an
    `sqlite3.OperationalError` naming a column is a schema diagram for anyone probing the API."""
    rid = getattr(request.state, "request_id", "-")
    print(json.dumps({"ts": _now_ms(), "level": "error", "ev": "unhandled", "rid": rid,
                      "type": type(exc).__name__}, sort_keys=True), flush=True)
    msg, status, retry = CODES["INTERNAL"]
    return JSONResponse({"error": {"code": "INTERNAL", "message": msg, "retryable": retry,
                                  "requestId": rid}}, status_code=status,
                        headers={"Retry-After": "2"})



def flags() -> Flags:
    """Every request reads the *store*, never the module-level FLAGS: a pod that cached the boot object at
    import time would never see an emergency flag change, which is the one thing flags are for."""
    return STORE.current()


@app.middleware("http")
async def request_id(request: Request, call_next):
    """A request id on every response and every log line: the 3am requirement. Nothing else in this file
    is optional in that sense — an error without an id is an error nobody can find."""
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    request.state.request_id = rid
    t0 = time.perf_counter()
    resp = await call_next(request)
    resp.headers["x-request-id"] = rid
    resp.headers["server-timing"] = f"app;dur={((time.perf_counter() - t0) * 1000):.1f}"
    line = {"ts": _now_ms(), "level": "info", "ev": "http", "rid": rid, "path": request.url.path,
            "method": request.method, "status": resp.status_code,
            "dur_ms": round((time.perf_counter() - t0) * 1000, 2)}
    print(json.dumps(line, sort_keys=True), flush=True)      # structured, stdout, no bodies
    return resp


@app.get("/healthz", responses=HEALTH_RESPONSES)
def healthz():
    """Liveness: can this process answer at all. Deliberately does not touch Postgres/Redis — a pod that
    fails readiness must not also be declared dead, or the fleet restarts during an upstream outage."""
    return {"ok": True}


@app.get("/readyz", responses=READYZ_RESPONSES)
def readyz():
    """Readiness: should it receive traffic. Checks the DB and whether the flag store fell back to boot
    defaults, because a pod running on defaults during an incident is worse than a pod that is not up."""
    problems = []
    # Refresh, so a pod that booted during a DB blip recovers on the NEXT probe instead of needing a restart.
    # `current()` is TTL-guarded and cheap, and the failure path keeps the last snapshot, so probing readiness
    # in an outage cannot make readiness worse.
    STORE.current()
    try:
        _db.execute("SELECT 1").fetchone()
    except Exception:
        problems.append("db_unreachable")
    if STORE.boot_defaults_used:
        problems.append("flags_from_defaults")
    return JSONResponse({"ready": not problems, "problems": problems}, status_code=200 if not problems else 503)


# --------------------------------------------------------------------------- #
# the SERVED document, corrected once at generation instead of per route
ERROR_SCHEMA = {
    "type": "object",
    "required": ["error"],
    "additionalProperties": False,
    "properties": {"error": {
        "type": "object",
        "required": ["code", "message", "retryable", "requestId"],
        "additionalProperties": False,
        "properties": {
            "code": {"type": "string"},
            "message": {"type": "string"},
            "retryable": {"type": "boolean"},
            "requestId": {"type": "string"},
        }}},
}


# The order endpoint's body schema is declared via openapi_extra (a dict body cannot be derived), and it
# references Price by $ref — which the SERVED document did not define, so a client generator died on a
# dangling pointer while every human-readable check stayed green. Injected here rather than inlined there so
# the pattern stays `{"$ref": ...}` in both documents.
PRICE_SCHEMA = {
    "type": "string",
    "pattern": "^0?\\.\\d{1,6}$|^0$",
    "description": "price in (0,1) USDC as a decimal string; a JSON number is refused with BAD_AMOUNT "
                    "because the venue client types these as float",
}


def _rewrite_openapi(spec: dict) -> dict:
    """Make the derived document tell the truth about 422s, then remove the schemas that no longer answer.

    FastAPI attaches its own `HTTPValidationError` body (`{"detail":[{"loc","msg","input"}]}`) to every
    operation with a validated parameter, and `input` is the client's own value echoed back. Our handler
    never sends that body — but the SERVED spec claimed it did, which is the same class of lie as the 200
    that started this file, just pointing at a schema instead of a status. So: every documented 422 gets the
    Error envelope, and the two schemas that nothing returns any more are dropped rather than left dangling.
    """
    for item in (spec.get("paths") or {}).values():
        for op in (item or {}).values():
            if not isinstance(op, dict):
                continue
            for status, resp in ((op.get("responses") or {}).items() if isinstance(op.get("responses"), dict)
                                 else ()):
                if str(status) != "422":
                    continue
                desc = (resp or {}).get("description") or "request rejected before any money logic ran"
                op["responses"][status] = {
                    "description": desc,
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}
    comp = spec.setdefault("components", {})
    schemas = comp.setdefault("schemas", {})
    schemas["Error"] = ERROR_SCHEMA
    schemas["Price"] = PRICE_SCHEMA
    blob = json.dumps(spec)
    for dead in ("HTTPValidationError", "ValidationError"):
        # drop only if nothing references it any more; a blind del is how you break a schema a client needs
        refs = blob.count("#/components/schemas/%s" % dead)
        if refs == 0:
            schemas.pop(dead, None)
    return spec


_original_openapi = app.openapi


def _openapi() -> dict:
    if app.openapi_schema:
        return app.openapi_schema
    app.openapi_schema = _rewrite_openapi(_original_openapi())
    return app.openapi_schema


app.openapi = _openapi      # type: ignore[method-assign]


# --------------------------------------------------------------------------- #
# reads — every one carries asOf + staleAfter (P04 D4 rule: a price without a timestamp is a rumour)
def _stamped(payload: dict, *, ttl_ms: int, stale_ms: int) -> dict:
    now = _now_ms()
    return {**payload, "asOf": now, "staleAfter": now + stale_ms,
            "cache": {"ttlMs": ttl_ms, "key": payload.get("cacheKey"),
                      "public": ttl_ms > 0, "immutable": False}}


# --------------------------------------------------------------------------- #
# the two list reads (markets, tape). Both are cursor-paginated for the same reason: Gamma caps a page at
# 100 and ignores `limit=5000` (measured in P01), so an offset/limit client silently under-reads. The app
# enforces the same cap the venue does, and says so in the payload, rather than being "polite" about a


@app.get("/v1/markets", responses=LIST_RESPONSES)
def list_markets(request: Request,
                 cursor: str | None = Query(default=None, max_length=128),
                 limit: int = Query(default=50, ge=1, le=100),
                 live: bool = Query(default=True),
                 sort_by: str = Query(default="endsSoon", alias="sortBy",
                                     pattern="^(endsSoon|newMarket|spread)$"),
                 q: str | None = Query(default=None, max_length=128)):
    """One page of markets, ordered by the one thing the terminal needs first: what is about to resolve.

    `volume24h` is deliberately NOT a sort key yet: there is no volume column, because volume is a P05
    rollup of the tape. Sorting by a number nobody has computed does not fail loudly - it orders by NULLs
    and reads like a broken sort.

    The cursor encodes `<sort key>|<id>`, i.e. the value the rows were ordered BY. Paging by id while
    ordering by end_ts - the version of this function that existed ten minutes ago - repeats a market on
    page 2 and drops another, and no single-page test can see it: only walking the whole set does.
    """
    rid = request.state.request_id
    key_sql, direction = _MARKET_SORT[sort_by]
    where, args = [], []
    if live:
        where.append("accepting_orders = 1")
    if q:
        # instr(), not LIKE: a LIKE pattern lets a user's own % and _ change what the query means, and there
        # is nothing to gain from letting them. Case is folded on both sides, once.
        where.append("instr(lower(question) || ' ' || lower(coalesce(slug, '')), ?) > 0")
        args.append(q.lower())
    if cursor is not None:
        try:
            raw_key, last_id = cursor.rsplit("|", 1)
            key_val = int(raw_key)
        except ValueError:
            return err("VALIDATION", rid)       # a malformed cursor is the client's bug, said plainly; not a
        if not re.fullmatch(r"[^|]{1,128}", last_id):      # 500 from an unguarded int(), and not a silent
            return err("VALIDATION", rid)                  # restart from the beginning, which duplicates rows
        # Explicit OR-chain rather than `(key, id) > (?, ?)`: row-value comparison applies ONE direction to
        # both columns, and this sort is key DESC with id ASC for newMarket. The wrong form silently drops
        # every row that ties on the key.
        op = ">" if direction == "ASC" else "<"
        where.append("(%s %s ? OR (%s = ? AND id > ?))" % (key_sql, op, key_sql))
        args += [key_val, key_val, last_id]
    # Two levels on purpose. `spread_micro` is a derived alias, and `COALESCE(spread_micro, ...) AS sort_key`
    # in the SAME select list is a "misuse of aliased column" error in SQLite (and in Postgres) - the sort
    # that only worked in the two tests that did not use it, which is why the contract-vs-app liveness check
    # asks the live app for every documented sort key rather than trusting that one of them works.
    spread = ("(SELECT MIN(price_micro) FROM book_levels b WHERE b.market_id = m.id AND b.side = 'ask') - "
              "(SELECT MAX(price_micro) FROM book_levels b WHERE b.market_id = m.id AND b.side = 'bid')")
    inner = ("SELECT m.id, m.question, m.accepting_orders, m.seconds_delay, m.minimum_tick_size, "
              "m.minimum_order_size, m.fee_type, m.enable_order_book, m.end_ts, m.first_seen_ms, "
              + spread + " AS spread_micro FROM markets m")
    sql = "SELECT *, " + key_sql + " AS sort_key FROM (" + inner + ") x"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY sort_key %s, id ASC LIMIT ?" % direction
    rows = _db.execute(sql, (*args, limit + 1)).fetchall()   # one extra row: is there a next page?
    has_more = len(rows) > limit
    rows = rows[:limit]
    # Tuples on purpose: sqlite3.Row would let a later edit reorder the SELECT while `r["question"]` kept
    # working, quietly. Positional reads weld the column list to the mapping below.
    items = [{"id": r[0], "question": r[1], "acceptingOrders": bool(r[2]), "secondsDelay": r[3],
              "minimumTickSize": norm_tick(r[4]),           # the SAME normaliser the gate uses, so the tick
              "minimumOrderSize": fmt_usdc(int(round(r[5] * 10 ** 6))),   # the UI paints and the gate applies
              "feeType": r[6], "enableOrderBook": bool(r[7]), "endTs": r[8],   # cannot become two answers
              "spreadMicro": r[9]} for r in rows]
    # sort_key is LAST by construction (`SELECT *, <expr> AS sort_key`), so the cursor does not depend on how
    # many columns the inner select grows to - that is how this line broke once already, silently, when
    # first_seen_ms was added for the newMarket sort: the cursor started encoding a spread and every page
    # after the first repeated the first one.
    nxt = "%d|%s" % (rows[-1][-1], rows[-1][0]) if (has_more and rows) else None
    return _stamped({"cacheKey": "markets:%s:%s" % (sort_by, cursor), "items": items, "nextCursor": nxt,
                     "pageSizeHardCap": 100, "sortBy": sort_by, "sortKeys": sorted(_MARKET_SORT)},
                    ttl_ms=1000, stale_ms=flags().stale_ms_metadata)


@app.get("/v1/tape", responses=TAPE_RESPONSES)
def get_tape(request: Request,
             market_id: str = Query(alias="marketId", min_length=1, max_length=128),
             since: int | None = Query(default=None, ge=0),
             limit: int = Query(default=64, ge=1, le=500)):
    """Recent fills, newest first, for first paint and gap fill. The live tape is WebSocket; this endpoint
    cannot be the tape because `/trades` at the venue is Cloudflare-cached (measured in P01: 14.7-33.3
    fills/sec arrive only over the socket).

    `since` is exclusive and is the VENUE's clock (exchange_ts), not our ingest clock: a client that mixes the
    two sees either a duplicate or a hole, and a hole in a tape reads exactly like a suppressed trade.
    `raw_json` is never returned: it is the venue's payload, it is large, and it carries other users'
    addresses.
    """
    rid = request.state.request_id
    if _db.execute("SELECT 1 FROM markets WHERE id=?", (market_id,)).fetchone() is None:
        return err("NOT_FOUND", rid)
    sql = ("SELECT price_micro, size_shares_micro, side, taker_is_maker, exchange_ts, ingest_ms "
           "FROM tape_trades WHERE market_id = ?")
    args: list = [market_id]
    if since is not None:
        sql += " AND exchange_ts < ?"                        # exclusive: `>=` would re-serve the last row seen
        args.append(since)
    sql += " ORDER BY exchange_ts DESC, id DESC LIMIT ?"
    rows = _db.execute(sql, (*args, limit + 1)).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    out = [{"price": fmt_usdc(r[0]), "size": fmt_usdc(r[1]), "side": r[2], "maker": bool(r[3]),
            "exchangeTs": r[4], "ingestMs": r[5]} for r in rows]
    return _stamped({"cacheKey": f"tape:{market_id}:{since}", "rows": out,
                     "nextCursor": (rows[-1][4] if has_more and rows else None)},
                    ttl_ms=500, stale_ms=flags().stale_ms_tape)


# sort_by -> (SQL expression, direction). The expression is ALSO selected as a trailing column, because a
# cursor has to encode exactly the value the rows were ordered by: deriving it from a column index is how
# `newMarket` (whose key is not in the original select list) nearly shipped a cursor that skipped markets.
# The sentinel is 2**63-1, which no real millisecond timestamp reaches, and it is what puts undated markets
# at the END of an ascending sort.
_SENTINEL = 9223372036854775807
_MARKET_SORT = {
    "endsSoon": ("COALESCE(end_ts, %d)" % _SENTINEL, "ASC"),
    "newMarket": ("first_seen_ms", "DESC"),
    "spread": ("COALESCE(spread_micro, %d)" % _SENTINEL, "ASC"),   # legal HERE: this is the outer query
}


@app.get("/v1/markets/{market_id}", responses=MARKET_RESPONSES)
def get_market(market_id: str, request: Request):
    row = _db.execute("SELECT id,question,accepting_orders,seconds_delay,minimum_tick_size,"
                      "minimum_order_size,fee_type,enable_order_book,end_ts FROM markets WHERE id=?",
                      (market_id,)).fetchone()
    if row is None:
        return err("NOT_FOUND", request.state.request_id)
    # the same normalisation the gate uses, so the number the UI renders and the number the gate compares
    # cannot disagree (Postgres "0.0100" vs SQLite 0.01 vs the gate's "0.01")
    row = list(row)
    row[4] = norm_tick(row[4])
    keys = ["id", "question", "accepting_orders", "seconds_delay", "minimum_tick_size",
            "minimum_order_size", "fee_type", "enable_order_book", "end_ts"]
    m = dict(zip(keys, row))
    return _stamped({"cacheKey": f"market:{market_id}",
                     "market": {"id": m["id"], "question": m["question"],
                                "acceptingOrders": bool(m["accepting_orders"]),
                                "secondsDelay": m["seconds_delay"],
                                "minimumTickSize": m["minimum_tick_size"],
                                "minimumOrderSize": m["minimum_order_size"],
                                "feeType": m["fee_type"],
                                "enableOrderBook": bool(m["enable_order_book"]),
                                "endDate": m["end_ts"]}},
                    ttl_ms=flags().cache_ttl_market_ms, stale_ms=flags().stale_ms_metadata)


@app.get("/v1/markets/{market_id}/book", responses=BOOK_RESPONSES)
def get_book(market_id: str, request: Request, depth: int = Query(default=24, ge=1, le=400)):
    """Books come from `ingest`'s materialised snapshot, never proxied live: one user must not be able to
    spend the shared rate budget (rule 2)."""
    # one query per side, each with its own LIMIT: a single ORDER BY side,price DESC with LIMIT 2k is the
    # classic way to return 2k bids and no asks, which then renders as a one-sided book (P01 measured 0/22
    # one-sided markets in reality, so the bug would have hidden in the data layer).
    per = []
    for side, ord_ in (("bid", "DESC"), ("ask", "ASC")):
        per.append((side, _db.execute(
            "SELECT side,price_micro,size_shares_micro,level_count,updated_ms FROM book_levels"
            " WHERE market_id=? AND side=? ORDER BY price_micro " + ord_ + " LIMIT ?",
            (market_id, side, depth)).fetchall()))
    rows = [r for _, rs in per for r in rs]
    if not rows:
        return err("NOT_FOUND", request.state.request_id)
    out = {"BUY": [], "SELL": []}
    for side, p, s, levels, upd in rows:
        out["SELL" if side == "ask" else "BUY"].append({"price": f"{p / 1e6:.6f}".rstrip("0"),
                                                          "shares": f"{s / 1e6:.6f}".rstrip("0"),
                                                          "levels": levels})
    age = _now_ms() - max(r[4] for r in rows)
    bid = max((r[1] for r in rows if r[0] == "bid"), default=None)
    ask = min((r[1] for r in rows if r[0] == "ask"), default=None)
    meta = _db.execute("SELECT minimum_tick_size FROM markets WHERE id=?", (market_id,)).fetchone()
    spread_ticks = None
    if bid is not None and ask is not None and meta and meta[0]:
        spread_ticks = round((ask - bid) / (float(meta[0]) * 1e6), 3)     # metadata maths, not money
    return _stamped({"cacheKey": f"book:{market_id}:{depth}", "market": market_id,
                     "bids": out["BUY"], "asks": out["SELL"],   # best-first on both sides, per the design system
                     "spreadTicks": spread_ticks, "bestBid": f"{bid / 1e6:.6f}".rstrip("0") if bid else None,
                     "bestAsk": f"{ask / 1e6:.6f}".rstrip("0") if ask else None, "ageMs": age},
                    ttl_ms=flags().cache_ttl_books_ms, stale_ms=flags().stale_ms_book)


# --------------------------------------------------------------------------- #
# the order path — the only write endpoint in P04 (everything else is promoted later, deliberately)
_IDEM_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def _upsert_intent(user_id: str, key: str, intent: "Intent", price_micro: int, size_micro: int,
                   notional_micro: int, state: str, *, risk_code: str | None = None) -> str:
    """Write (or revise) the intent row for this idempotency key, and return its id.

    An INSERT would fail UNIQUE(user_id, idempotency_key) the moment a user fixes a rejected order and
    retries with the same key - which is the behaviour the idempotency layer *promises* (see Idem.abandon) -
    and the retry came back as a 500. Revising the same row is also the honest model: one key, one intent,
    possibly more than one decision, and the ledger keeps the id so the rejected attempt stays countable.
    """
    existing = _db.execute("SELECT id FROM order_intents WHERE user_id=? AND idempotency_key=?",
                           (user_id, key)).fetchone()
    now = _now_ms()
    if existing:
        _db.execute("UPDATE order_intents SET side=?, price_micro=?, size_micro=?, notional_micro=?, state=?"
                    ", risk_code=?, updated_ms=? WHERE id=?",
                    (intent.side, price_micro, size_micro, notional_micro, state, risk_code, now,
                     existing[0]))
        return existing[0]
    oid = uuid.uuid4().hex
    _db.execute("INSERT INTO order_intents (id,user_id,market_id,token_id,side,price_micro,size_micro,"
                "notional_micro,state,risk_code,idempotency_key,created_ms,updated_ms) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, user_id, intent.market_id, intent.token_id, intent.side, price_micro, size_micro,
                 notional_micro, state, risk_code, key, now, now))
    return oid


# The two POST bodies, as one source of truth. The handler validates a plain dict rather than a pydantic
# model ON PURPOSE: a model typed `price: str` turns a JSON number into a generic 422, while the dict path
# answers BAD_AMOUNT, which is the money-specific advice. The cost of that choice is that FastAPI cannot
# derive a body schema, so the same tuples below are handed to `openapi_extra` and the SERVED document ends
# up saying the same thing the contract does. Two consumers, one list.
ORDER_REQUIRED = ("marketId", "tokenId", "side", "price", "size")
ORDER_PROPS = {
    "marketId": {"type": "string", "minLength": 1, "maxLength": 128},
    "tokenId": {"type": "string", "minLength": 1, "maxLength": 128},
    "side": {"type": "string", "enum": ["BUY", "SELL"]},
    "price": {"$ref": "#/components/schemas/Price"},
    "size": {"type": "string", "pattern": "^[0-9]+(\\.[0-9]{1,6})?$", "maxLength": 24},
}
KILL_REQUIRED = ("engaged", "reason")
KILL_PROPS = {"engaged": {"type": "boolean"},
              "reason": {"type": "string", "minLength": 4, "maxLength": 400}}


def _body_schema(required: tuple[str, ...], props: dict) -> dict:
    return {"requestBody": {"required": True, "content": {"application/json": {
        "schema": {"type": "object", "additionalProperties": False, "required": list(required),
                   "properties": props}}}}}


def _check_body(body: object, required: tuple[str, ...], rid: str) -> JSONResponse | None:
    """Shape before semantics: a missing field must be a 422 naming the field, not a 500 from a KeyError
    (which is what `body["tokenId"]` produced for exactly this request, in the first end-to-end test)."""
    if not isinstance(body, dict):
        return err("VALIDATION", rid, detail="body must be a JSON object")
    missing = ["missing: %s" % k for k in required if k not in body]
    unknown = ["unknown: %s" % k for k in body if k not in required]
    if missing or unknown:
        return err("VALIDATION", rid, where=missing + unknown)
    return None


@app.post("/v1/orders", status_code=202, responses=ORDER_RESPONSES,
           openapi_extra=_body_schema(ORDER_REQUIRED, ORDER_PROPS))
def place_order(request: Request, body: dict = Body(...),
                idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                x_user_id: str | None = Header(default=None, alias="X-User-Id")):
    rid = request.state.request_id
    if not x_user_id:
        return err("SIGNER_UNAVAILABLE", rid, detail="no user context")     # in prod: 401 from auth middleware
    if not idempotency_key or not _IDEM_RE.match(idempotency_key):
        return err("IDEM_KEY_REQUIRED", rid)
    bad = _check_body(body, ORDER_REQUIRED, rid)
    if bad is not None:
        # BEFORE idem.begin: a request that cannot be valid must not occupy the key. If it did, the client's
        # next attempt - the one where they fixed the typo - would answer IDEM_IN_PROGRESS against a row that
        # no worker will ever finish, and the user could not place an order at all until the key expired.
        return bad
    idem = Idem(_db)
    rec = idem.begin(x_user_id, idempotency_key, body)
    # everything below runs inside _guarded(): an unexpected exception must release the key, or the client's
    # retry is answered with IDEM_IN_PROGRESS forever and the user cannot place an order at all.
    if rec.mismatch:
        return err("IDEM_CONFLICT", rid)
    if rec.busy:                       # NOT rec.state == "in_progress": the request that just INSERTED the
        return err("IDEM_IN_PROGRESS", rid)    # row is also in_progress, and comparing the state alone
                                        # makes every first order a 409 against itself (caught by test_api)
    if rec.replay:
        return JSONResponse(json.loads(rec.response_json), status_code=202)

    try:
        # strings only, into the integer parser. Accepting a JSON number here would mean a float is
        # the input to a money path, and the whole point of parse_usdc is that it refuses excess scale.
        if not isinstance(body.get("price"), str) or not isinstance(body.get("size"), str):
            raise ScaleError("price and size must be decimal strings")
        price_micro = price_ticks(body["price"])
        size_micro = parse_usdc(body["size"])
    except (ScaleError, KeyError, ValueError, TypeError):
        idem.abandon(x_user_id, idempotency_key)
        return err("BAD_AMOUNT", rid)
    except MoneyError as e:                # reclassified inside the money module; kept explicit so a
        idem.abandon(x_user_id, idempotency_key)   # future subclass is still a 422 and never a 500
        return err("BAD_AMOUNT", rid)

    mrow = _db.execute("SELECT accepting_orders,seconds_delay,minimum_tick_size,minimum_order_size,"
                       "fee_type,enable_order_book FROM markets WHERE id=?", (body.get("marketId"),)).fetchone()
    if mrow is None:
        idem.abandon(x_user_id, idempotency_key)
        return err("NOT_FOUND", rid)
    b = _db.execute("SELECT side,price_micro,updated_ms FROM book_levels WHERE market_id=? "
                    "ORDER BY side,price_micro DESC LIMIT 2", (body.get("marketId"),)).fetchall()
    bid = next((r[1] for r in b if r[0] == "bid"), None)
    ask = next((r[1] for r in b if r[0] == "ask"), None)
    age = max([_now_ms() - r[2] for r in b], default=10 ** 12)
    st = MarketState(accepting_orders=bool(mrow[0]), seconds_delay=mrow[1],
                     minimum_tick_size=norm_tick(mrow[2]),      # engine-dependent type; see norm_tick
                     minimum_order_size=str(mrow[3]), fee_type=mrow[4], enable_order_book=bool(mrow[5]),
                     best_bid_micro=bid, best_ask_micro=ask, snap_age_ms=age)
    spent = _db.execute("SELECT COALESCE(SUM(notional_micro),0) FROM order_intents WHERE user_id=? "
                        "AND state IN ('submitted','submitting') AND created_ms>?",
                        (x_user_id, _now_ms() - 86_400_000)).fetchone()[0]
    open_n = _db.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND state IN ('live','partial')",
                         (x_user_id,)).fetchone()[0]
    # engaged = the value of the NEWEST row. `WHERE engaged=1` would read "engaged at some point in
    # history" and the switch could never be released without a DELETE, which the append-only trigger
    # forbids - a kill switch you cannot disarm is not a control, it is an outage with a UI.
    kill = _db.execute("SELECT engaged FROM kill_switch_state ORDER BY at_ms DESC, id DESC LIMIT 1"
                       ).fetchone()
    kill = bool(kill and kill[0])

    intent = Intent(user_id=x_user_id, token_id=str(body["tokenId"]), side=str(body["side"]).upper(),
                    price_micro=price_micro, size_shares_micro=size_micro, idempotency_key=idempotency_key,
                    market_id=str(body.get("marketId") or ""))
    f = flags()
    # Limits come from the flag store, not from the module constant: Flags says "the DB copy is authoritative
    # at runtime" and this line was the contradiction. An incident response is `UPDATE feature_flags SET
    # max_order_notional_micro...`; a limit that is only read at import time makes that operation a no-op
    # until the pods restart, which is the opposite of the reason it is a flag.
    limits = Limits(max_order_notional_micro=f.max_order_notional_micro,
                     max_24h_notional_micro=f.max_24h_notional_micro,
                     min_order_size_shares_micro=f.min_order_size_shares_micro,
                     max_tick_sizes=LIMITS.max_tick_sizes, max_snap_age_ms=LIMITS.max_snap_age_ms)
    d = evaluate(intent, st, limits=limits, open_orders=open_n, spent_24h_micro=int(spent),
                 kill_switch=bool(kill))
    if not d.allowed:
        _upsert_intent(x_user_id, idempotency_key, intent, price_micro, size_micro, d.notional_micro,
                       IntentState.REJECTED.value, risk_code=d.code)
        idem.abandon(x_user_id, idempotency_key)       # a rejected order may be retried with the SAME key
        resp = err(d.code, rid)
        resp.headers["x-risk-checks"] = str(len(d.checks_run))
        resp.headers["x-risk-latency-ms"] = f"{d.latency_ms:.2f}"
        return resp

    oid = _upsert_intent(x_user_id, idempotency_key, intent, price_micro, size_micro, d.notional_micro,
                         IntentState.QUEUED.value)
    out = {"intentId": oid, "state": IntentState.QUEUED.value, "riskLatencyMs": round(d.latency_ms, 3),
           "notionalMicro": d.notional_micro, "poll": f"/v1/orders/intents/{oid}",
           "note": "queued for executor; 202 is the answer, not 'accepted at the venue'"}
    idem.finish(x_user_id, idempotency_key, out, order_hash=None)
    # status_code is stated HERE as well as on the decorator: a returned JSONResponse carries its own
    # status, and an explicit 200 in the body of a route documented as 202 is a contract lie that only an
    # end-to-end assertion catches.
    resp = JSONResponse(_stamped({"cacheKey": None, **out}, ttl_ms=0, stale_ms=0), status_code=202)
    # on BOTH paths: a gate that only reports its latency when it says "no" cannot tell you it is getting
    # slow, and "how long does the risk check take" is the question an incident asks first.
    resp.headers["x-risk-latency-ms"] = f"{d.latency_ms:.2f}"
    resp.headers["x-risk-checks"] = str(len(d.checks_run))
    return resp


@app.get("/v1/orders/intents/{intent_id}", responses=INTENT_RESPONSES)
def get_intent(intent_id: str, request: Request, x_user_id: str | None = Header(default=None, alias="X-User-Id")):
    row = _db.execute("SELECT state,risk_code,venue_order_id,updated_ms FROM order_intents WHERE id=? AND user_id=?",
                      (intent_id, x_user_id)).fetchone()
    if row is None:
        return err("NOT_FOUND", request.state.request_id)
    return _stamped({"cacheKey": f"intent:{intent_id}", "intentId": intent_id, "state": row[0],
                     "riskCode": row[1], "venueOrderId": row[2]}, ttl_ms=0, stale_ms=flags().stale_ms_book)


@app.post("/v1/admin/kill-switch", responses=KILL_RESPONSES,
           openapi_extra=_body_schema(KILL_REQUIRED, KILL_PROPS))
def kill_switch(request: Request, body: dict = Body(...), x_admin: str | None = Header(default=None,
                                                                                       alias="X-Admin-Token")):
    """Even admin tooling goes through the risk service (rule 3): admin can *engage* the switch, never
    bypass it, and there is no endpoint that submits an order while it is engaged."""
    rid = request.state.request_id
    if not x_admin:
        return err("SIGNER_UNAVAILABLE", rid)
    bad = _check_body(body, KILL_REQUIRED, rid)
    if bad is not None:
        return bad
    engaged = 1 if body.get("engaged") else 0
    reason = str(body.get("reason") or "").strip()
    # The DB CHECK (4..400) is the rule; translating its violation into a 422 rather than a 500 is the
    # API's job. A 500 here would tell the on-call "the endpoint is broken" when the truth is "say why".
    if not (4 <= len(reason) <= 400):
        return err("BAD_REASON", rid)
    _db.execute("INSERT INTO kill_switch_state (engaged, reason, changed_by, at_ms) VALUES (?,?,?,?)",
                (engaged, reason, "admin", _now_ms()))
    if engaged:
        _db.execute("UPDATE order_intents SET state='rejected', risk_code='RISK_HALT' "
                    "WHERE state IN ('pending','queued')")
    return {"engaged": bool(engaged), "atMs": _now_ms()}
