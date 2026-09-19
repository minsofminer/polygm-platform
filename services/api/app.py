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

import hashlib
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
from polygm_core.security import authz as _authz
from polygm_core.security import keys as _keys
from polygm_core.security import passwords as _pwd
from polygm_core.security import redact as _redact
from polygm_core.security import sanitise as _san
from polygm_core.security import telegram as _tg
from polygm_core.security import totp as _totp
from polygm_core.security.store import ACCESS_TTL_MS, SecStore, hash_token as _hash_token, token_string as _token_string

# The only place the non-stdlib security primitives are imported. A missing dependency is a refusal on the
# paths that need it (503 SECURITY_ENV_MISSING), and with `PGM_REQUIRE_SECURITY_ENV=1` it is a refusal to boot.
try:
    import security_backends as _secb                                # noqa: F401  (sibling module)
    _SECB_OK, _SECB_ERR = True, ""
except Exception as _exc:                                            # noqa: BLE001
    _secb, _SECB_OK, _SECB_ERR = None, False, type(_exc).__name__

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
    # P07. Each of these is a *user-safe* sentence: the detail a client needs is here, and the detail an
    # attacker would like is in the log line, behind the request id.
    "UNAUTHENTICATED": ("a session is required", 401, False),
    "SESSION_STALE": ("this session was ended because your credentials changed", 401, False),
    "SESSION_REVOKED": ("this session has been revoked", 401, False),
    "ADMIN_REQUIRED": ("this route is admin-only", 403, False),
    "LOGIN_FAILED": ("wrong user name or password", 401, False),
    "ACCOUNT_LOCKED": ("too many attempts; try again later", 429, True),
    "TOTP_REQUIRED": ("a 6-digit code is needed for this action", 403, False),
    "TOTP_INVALID": ("that code did not work", 403, False),
    "TOTP_LOCKED": ("the authenticator is locked after too many tries", 429, True),
    "ADDRESS_COOLDOWN": ("this destination is still in its 24 hour hold", 409, True),
    "ADDRESS_LIMIT": ("too many saved destinations", 422, False),
    "REMOVE_DURING_COOLDOWN": ("a destination that has not finished its hold cannot be deleted", 409, False),
    "NO_SUCH_RESOURCE": ("no such resource", 404, False),
    "REFRESH_UNKNOWN": ("that session cannot be refreshed", 401, False),
    "REFRESH_REUSED": ("this session was ended for safety", 401, False),
    "TELEGRAM_REPLAY": ("that sign-in had already been used", 409, False),
    "TELEGRAM_INVALID": ("could not verify the Telegram sign-in", 401, False),
    "SECURITY_ENV_MISSING": ("this action needs a security backend that is not configured", 503, True),
    "KEYSTORE_TAMPER": ("the key store refused to release a key", 503, False),
    "BREAK_GLASS_DENIED": ("break-glass needs two named approvers and a 20-character reason", 422, False),
    "SESSION_MISMATCH": ("the session and the request disagree about who is asking", 403, False),
    "AUTHZ_UNDECLARED": ("this route has no auth decision on record", 500, False),
    "REFRESH_EXPIRED": ("that session expired", 401, False),
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
# The durable-fill read added by P05. `422` is declared because FastAPI emits it on a bad `limit` regardless of
# whether the handler checks, and the OpenAPI audit treats an undocumented status as a lie by omission.
FILLS_RESPONSES = {404: {"description": "no such market"},
                   422: {"description": "limit outside 1..1000, or a non-numeric `since`"}}
BOOK_RESPONSES = {404: {"description": "no such market, or a market with no order book"},
                  422: {"description": "depth outside 1-400"}}
# P09 · markets. Three read surfaces the discovery screen, the event page and the info rail need, declared as
# literals for the same reason as the tables below: `tools/check-openapi.py` reads them out of the AST.
HISTORY_RESPONSES = {
    404: {"description": "no such market"},
    422: {"description": "interval outside the server-built set, or limit outside 2-1000"},
}
EVENT_RESPONSES = {
    404: {"description": "no such event"},
    422: {"description": "limit outside 1-400"},
}
HOLDERS_RESPONSES = {
    404: {"description": "no such market"},
    422: {"description": "limit outside 1-100"},
}
INTENT_RESPONSES = {404: {"description": "no such intent, or it belongs to another user (never 403: an "
                                         "id-probing endpoint must not confirm existence)"}}
AUTH_RESPONSES = {
    401: {"description": "no session, or a session that has been revoked or made stale"},
    403: {"description": "the session is fine and the action still needs the authenticator"},
    422: {"description": "the body is not the shape this route takes"},
    429: {"description": "too many attempts against this credential"},
    503: {"description": "the security backend is not configured on this pod"},
}
BREAK_GLASS_RESPONSES = {
    403: {"description": "the token is not the admin token, or the two approvers did not check out"},
    422: {"description": "no reason, or fewer than two approvers"},
    503: {"description": "this pod has no admin token configured, which is a misconfiguration, not an attack"},
}
# A literal, not a `dict(AUTH_RESPONSES, **{...})`: `tools/check-openapi.py` reads these tables out of the
# module's AST to compare them with the contract, and a call expression reads back as "no statuses at all" —
# which shows up as a contract that documents errors the app allegedly cannot return.
ADDRESS_RESPONSES = {
    401: {"description": "no session, or a session that has been revoked or made stale"},
    403: {"description": "the session is fine and the action still needs the authenticator"},
    409: {"description": "the destination is inside its hold, or still in use"},
    422: {"description": "the body is not the shape this route takes"},
    429: {"description": "too many attempts against this credential"},
    503: {"description": "the security backend is not configured on this pod"},
}
# Every one of these tables is a *literal with literal values*, because `tools/check-openapi.py` reads them out
# of the AST: `{401: AUTH_RESPONSES[401]}` parses as "no status", and the contract then looks like it documents
# errors the API allegedly cannot return. Duplication here is the price of the checker being a parser.
SESSIONS_RESPONSES = {
    200: {"description": "the caller's own sessions, with no token material in any field"},
    401: {"description": "no bearer session, or the session ended"},
    422: {"description": "the body is not the shape this route takes"},
}


# 500 for every operation except /healthz, which has nothing to fail on and must keep answering 200 while the
# DB is on fire. Applied in a loop so a tenth endpoint cannot forget it, and `del` so the loop variable does
# not survive into the module namespace where a reader would take it for state.
for _t in (READYZ_RESPONSES, LIST_RESPONSES, TAPE_RESPONSES, KILL_RESPONSES, MARKET_RESPONSES,
           BOOK_RESPONSES, INTENT_RESPONSES, AUTH_RESPONSES, SESSIONS_RESPONSES, ADDRESS_RESPONSES,
           BREAK_GLASS_RESPONSES):
    _t.update(_INTERNAL)
del _t


def err(code: str, request_id: str, *, detail: str | None = None, where: list[str] | None = None,
        retry_after_s: int | None = None) -> JSONResponse:
    """One envelope shape, always. `detail` is accepted for internal callers but deliberately NOT put in the
    body: it is the free-text half where a Python message would leak, and `log` gets it instead.

    `where` is the exception, and the reason it is allowed is that it carries field NAMES and nothing else:
    "missing: price" tells a client what to fix without echoing what they sent (a token id, an address, a
    pasted key). Values never cross this line.
    """
    msg, status, retry = CODES.get(code, ("request failed", 400, False))
    if where:
        msg = msg + " (" + ", ".join(where) + ")"
    # A constant `Retry-After: 2` on a 15-minute lockout is an instruction to hammer us: the header has to say
    # what the server actually means. Clamped, because a client that trusts us should not be told to wait a year.
    headers = {}
    if retry:
        headers["Retry-After"] = str(max(1, min(3600, int(retry_after_s if retry_after_s else 2))))
    return JSONResponse({"error": {"code": code, "message": msg, "retryable": retry,
                                   "requestId": request_id}}, status_code=status, headers=headers)


def _ip_hash(request: Request) -> str:
    """Keyed, truncated, and never reversible. The pepper is a secret, so the same IP hashes differently in
    dev than in prod and a stolen table cannot be joined to a stolen log."""
    xff = request.headers.get("x-forwarded-for") or ""
    ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else "")
    return _authz.ip_hash(ip, os.environ.get("PGM_IP_PEPPER", ""))


def _ua_hash(request: Request) -> str:
    ua = (request.headers.get("user-agent") or "").strip()[:256]
    return _authz.ip_hash(ua, os.environ.get("PGM_IP_PEPPER", "")) if ua else ""


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
    print(_redact.line(ts=_now_ms(), level="error", ev="unhandled", rid=rid, type=type(exc).__name__,
                       msg=_redact.redact_text(str(exc))[:400]), flush=True)
    msg, status, retry = CODES["INTERNAL"]
    return JSONResponse({"error": {"code": "INTERNAL", "message": msg, "retryable": retry,
                                  "requestId": rid}}, status_code=status,
                        headers={"Retry-After": "2"})



def flags() -> Flags:
    """Every request reads the *store*, never the module-level FLAGS: a pod that cached the boot object at
    import time would never see an emergency flag change, which is the one thing flags are for."""
    return STORE.current()


# D7 · transport and framing. `frame-ancestors` is the one that needs a sentence: the Mini App is *framed by
# Telegram*, so `X-Frame-Options: DENY` would break the product and `SAMEORIGIN` would too. CSP's
# `frame-ancestors` is the only directive that can say "these origins, and nobody else", so that is what we set
# and XFO is deliberately absent — a reviewer looking for it should find this comment, not a silent omission.
# `base-uri` and `form-action` are pinned to self because a single injected `<base>` rewrites every relative
# URL on the page, which is how a market title becomes a credential sink.
SECURITY_HEADERS = {
    "content-security-policy": ("default-src 'self'; script-src 'self' https://telegram.org "
                               "https://web.telegram.org; style-src 'self' 'unsafe-inline'; img-src 'self' data: "
                               "https:; font-src 'self'; connect-src 'self' https://clob.polymarket.com "
                               "wss://ws-subscriptions-clob.polymarket.com; frame-ancestors 'none'; "
                               "base-uri 'self'; form-action 'self'; object-src 'none'; upgrade-insecure-requests"),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "permissions-policy": "geolocation=(), camera=(), microphone=(), payment=(), usb=()",
    "strict-transport-security": "max-age=31536000; includeSubDomains; preload",
}
# The Mini App is the only thing that may frame us; the API itself never frames anything, so `frame-ancestors
# 'none'` above is right for /v1/* and the *web* app (P08) swaps in the Telegram allowlist at its own edge.
MINI_APP_FRAME_ANCESTORS = "frame-ancestors https://web.telegram.org https://*.telegram.org"


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
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    # `cache-control` is deliberately not in SECURITY_HEADERS: a public market list is exactly what a CDN should
    # hold, and P05's `cache.ttlMs` is the contract for that. What must never be stored is anything that says
    # who you are or that something went wrong, so those two cases get the header here, next to the CSP that
    # makes the same decision for framing.
    if request.url.path.startswith(("/v1/auth", "/v1/wallet", "/v1/admin")) or resp.status_code >= 400:
        resp.headers["cache-control"] = "no-store"
    if (request.headers.get("x-openout-frame") or "") == "miniapp":
        resp.headers["content-security-policy"] = SECURITY_HEADERS["content-security-policy"].replace(
            "frame-ancestors 'none'", MINI_APP_FRAME_ANCESTORS)
    line = {"ts": _now_ms(), "level": "info", "ev": "http", "rid": rid, "path": request.url.path,
            "method": request.method, "status": resp.status_code,
            "dur_ms": round((time.perf_counter() - t0) * 1000, 2)}
    # `_redact.line`, not `json.dumps`: a path or query that carries a token (`?token=`, `#initData=...`) is a
    # secret in an access log otherwise, and the access log is the one log everyone keeps forever.
    print(_redact.line(**line), flush=True)
    return resp


@app.get("/healthz", responses=HEALTH_RESPONSES)
def healthz():
    """Liveness: can this process answer at all. Deliberately does not touch Postgres/Redis — a pod that
    fails readiness must not also be declared dead, or the fleet restarts during an upstream outage."""
    return {"ok": True}


@app.on_event("startup")
def _sync_route_levels():
    """Mirror the authorisation table into the database at boot, so the deployment artifact and the code can be
    diffed by the gate instead of argued about. A route added without a level shows up as an undeclared row
    within one restart, not at the next review."""
    try:
        SEC.sync_route_levels(_authz.LEVELS_TABLE, at=_now_ms())
    except Exception:                                          # noqa: BLE001 - never block a boot on a mirror
        pass


def _install_sentry_scrubbing() -> str:
    """If a DSN is configured, no event leaves this pod without passing through the same redactor the logs use.

    Deliberately *not* an import-time side effect: the SDK must not be needed to import the app (tests and
    `--help` would then depend on a network service), and a scrubber that fails to install has to be visible in
    the log line it writes. The rule behind it is D6's: the error reporter is the one destination an engineer
    forgets is a third party.
    """
    dsn = (os.environ.get("PGM_SENTRY_DSN") or "").strip()
    if not dsn:
        return "disabled (no PGM_SENTRY_DSN)"
    try:
        import sentry_sdk                                            # noqa: WPS433 - optional by design
    except Exception as exc:                                         # noqa: BLE001
        msg = "PGM_SENTRY_DSN is set but sentry_sdk is not importable: %s" % type(exc).__name__
        if _flag("PGM_REQUIRE_SECURITY_ENV"):
            raise _secb.BootError(msg) from exc
        print(_redact.line(level="warn", ev="sentry", msg=msg), flush=True)
        return "missing-sdk"
    sentry_sdk.init(dsn=dsn, before_send=_redact.before_send, send_default_pii=False, max_breadcrumbs=20,
                    request_bodies="never")
    return "installed"


_SENTRY = ""


@app.on_event("startup")
def _boot_security_plane():
    """Boot-time security posture: the Sentry scrubber, and the refusal to serve money paths with no KEK.

    `PGM_REQUIRE_SECURITY_ENV=1` turns "the pod came up in a degraded state" from a dashboard surprise into a
    failed start. Dev without the flag keeps working, because a repo that cannot boot without five secrets is a
    repo where nobody runs the auth tests.
    """
    global _SENTRY
    _SENTRY = _install_sentry_scrubbing()
    if _flag("PGM_REQUIRE_SECURITY_ENV"):
        missing, notes = _secb.boot_check()["missing"], _secb.boot_check()["notes"]
        if missing:
            raise _secb.BootError("this pod is configured to require the security plane and cannot start: %s"
                                  % ", ".join(missing[:6]))
        if notes:
            print(_redact.line(level="warn", ev="security-boot", notes="; ".join(notes)[:300]), flush=True)


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
    if _SENTRY == "missing-sdk":
        # The event reporter is down, which is not a reason to stop serving - but it is a reason for the probe
        # to say so, because an incident with no error stream is how a second one gets missed.
        problems.append("sentry_scrubber_unavailable")
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
def _anon(wallet: str) -> str:
    """A stable pseudonym for a counterparty address.

    Stable so a client can group by trader across requests, keyed so it cannot be reversed to an address by
    someone who does not have our secret, and truncated because it appears in a URL-cacheable list. This is the
    only way a fill log may show a counterparty at all: the venue's payload carries the address, and an API
    that echoes it turns a public tape into an address book of our users' counterparties.
    """
    return "w_" + hashlib.sha256(("polygm-anon:" + str(wallet)).encode()).hexdigest()[:10]


def _shares(micro: int) -> str:
    r"""Shares, not dollars: the venue quotes size in shares and `fmt_usdc` would imply a $1 cap.

    The trailing `.` strip is not cosmetic. `f"{5_000_000 / 1e6:.6f}".rstrip("0")` is `"5."`, which fails the
    contract's own `^[0-9]+(\.[0-9]{1,6})?$` pattern — a bug that sat in the book route's inline formatting too,
    invisible until a seed used a whole number of shares, and `fmt_usdc` in the money module already had the
    correct two-step strip. Numbers that leave this file must be strings a schema accepts, not "close enough".
    """
    return f"{int(micro) // 10 ** 6}.{int(micro) % 10 ** 6:06d}".rstrip("0").rstrip(".") or "0"


SEC = SecStore(_db)


_HASHER = None


def _hasher():
    global _HASHER
    if _HASHER is None:
        _HASHER = _secb.Argon2id()
    return _HASHER


def _envelope():
    """Lazily built so that importing this module never needs a KEK; every *use* of it does, and fails loudly."""
    global _ENVELOPE
    if _ENVELOPE is None:
        kek, v = _secb.kek_from_env()
        _ENVELOPE = _secb.AesGcmEnvelope(kek, version=v)
    return _ENVELOPE


_ENVELOPE = None


def _trust_header() -> bool:
    """`X-User-Id` is a development identity, not an authentication path.

    Default on for dev/CI (every harness in this repo, and the P06 drill, present that header), default off
    when `PGM_REQUIRE_SECURITY_ENV=1` — which is what a production compose file sets. `tools/p07-gate-check.py`
    asserts both halves, because a control that only exists as an env var nobody reads is a comment.
    """
    if _flag("PGM_REQUIRE_SECURITY_ENV"):
        return False
    return os.environ.get("PGM_TRUST_USER_HEADER", "1") == "1"


def _flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip() in ("1", "true", "yes", "on")


def _bearer(request: Request) -> str:
    h = request.headers.get("authorization") or ""
    return h[7:].strip() if h[:7].lower() == "bearer " else ""


def _principal(request: Request) -> tuple[str | None, dict | None, JSONResponse | None]:
    """(user_id, session row, error response). One function, so one place can be wrong.

    It does not raise: a route that forgets to check is the bug this returns 401 for, and every route in the
    table below calls it because `authz.require` is applied to its declared level, not to its good intentions.
    """
    rid = request.state.request_id
    tok = _bearer(request)
    # The operation must be the route *template*, not the URL. The table keys rows by `/v1/orders/intents/{intent_id}`
    # because that is the thing a reviewer can check; matching a concrete `/v1/orders/intents/0xabc…` against it
    # would either miss (and a miss here is a 500 on every authenticated call to any parameterised route) or
    # require the table to hold a regex per identifier shape, which is how authorisation tables rot.
    # `scope["route"]` is what the router settled on, so it is right even for a mounted or redirected path; the
    # two later fallbacks exist because a hand-built Request in a test has no route at all.
    _route = request.scope.get("route")
    _path = (getattr(_route, "path_format", None) or getattr(_route, "path", None)
             or request.scope.get("route_path") or request.url.path)
    op = "%s %s" % (request.method, _path)
    if tok:
        row = SEC.resolve_session(_hash_token(tok), at=_now_ms())
        if not row:
            return None, None, err("UNAUTHENTICATED", rid)
        d = _authz.require(op, user_id=str(row["user_id"]), session_cred_gen=row.get("cred_gen"),
                          credential_gen=(SEC.credential(str(row["user_id"])) or {}).get("gen"))
        if not d.allowed:
            return None, None, err(d.code if d.code in CODES else "UNAUTHENTICATED", rid, detail=d.reason or None)
        request.state.authz_level = _authz.lookup(op)[1] if _authz.lookup(op) else "undeclared"
        SEC.touch_session(row["id"], _now_ms())
        request.state.session_id = row["id"]
        request.state.owner_uid = str(row["user_id"])
        request.state.uid = str(row["user_id"])
        return str(row["user_id"]), row, None
    declared = _authz.lookup(op)
    if declared is None:
        # Loud, not permissive. A route that nobody classified must not be served on the strength of a forgotten
        # table row: 500 with a code of its own is what a dashboard shows and what `make gate-p07` refuses.
        return None, None, err("AUTHZ_UNDECLARED", rid, detail=op)
    if declared[1] == _authz.PUBLIC:
        return None, None, None
    if _trust_header():
        uid = (request.headers.get("x-user-id") or "").strip()
        if not uid:
            return None, None, err("UNAUTHENTICATED", rid,
                                   detail="no bearer token, and X-User-Id is empty")
        request.state.dev_identity = True
        request.state.owner_uid = uid
        request.state.uid = uid
        return uid, None, None
    return None, None, err("UNAUTHENTICATED", rid)


def _admin(request: Request) -> tuple[bool, JSONResponse | None]:
    rid = request.state.request_id
    tok = request.headers.get("x-admin-token") or ""
    if not tok:
        # Deliberately 503 rather than 401 (the P04 rule, still true): a box with no admin token configured is
        # a misconfiguration, and it must not look like an attack in the dashboards the on-call reads.
        return False, err("SIGNER_UNAVAILABLE", rid)
    expected = (os.environ.get("PGM_ADMIN_TOKEN") or "").strip()
    if not expected or not _authz.check_service_token(tok, expected):
        return False, err("ADMIN_REQUIRED", rid)
    return True, None


def _totp_gate(request: Request, user_id: str, code: str, *, action: str) -> JSONResponse | None:
    """The mandatory-second-factor path: withdrawals, address changes, key export, break-glass, revocation.

    Every failure mode returns a *different* code, because support will be asked "why" at 2am and "no" is not
    an answer. `reused` in particular must never be softened into "try again": it means somebody else has
    already used that exact code.
    """
    rid = request.state.request_id
    if not _totp.is_mandatory(action):
        return None
    st = SEC.totp_state(user_id)
    if not st:
        return err("TOTP_REQUIRED", rid, detail="enroll an authenticator before %s" % action.replace("_", " "))
    if not st.get("verified_ms"):
        # The route's own promise: `verified_ms` stays NULL until a code has been entered, so an attacker who
        # enrols their own authenticator through a hijacked session authorises nothing with it. Recorded,
        # because "somebody attached a factor and immediately tried to spend it" is the incident, not the bug.
        SEC.auth_event(user_id, "totp_unverified_use", at=_now_ms(), detail={"action": action})
        return err("TOTP_REQUIRED", rid, detail="finish authenticator enrolment before %s"
                   % action.replace("_", " "))
    res = _totp.verify(_ENVELOPE_TOTP_SECRET(st), str(code or ""), at=_now_ms(),
                       last_step=int(st.get("last_step") or -1), attempts=int(st.get("failed_count") or 0),
                       locked_until_ms=int(st.get("locked_until_ms") or 0))
    if not res.ok:
        SEC.totp_reject(user_id, at=_now_ms())
        SEC.auth_event(user_id, "totp_%s" % res.reason, at=_now_ms())
        code_out = "TOTP_LOCKED" if res.reason == "locked" else "TOTP_INVALID"
        return err(code_out, rid, detail=res.reason if res.reason != "bad_code" else None,
                   retry_after_s=max(1, int(res.retry_after_ms or 0) // 1000) if res.reason == "locked" else None)
    SEC.totp_accept(user_id, step=res.step, at=_now_ms())
    return None


def _ENVELOPE_TOTP_SECRET(state: dict) -> str:
    """Unwrap the TOTP secret for one comparison. No cache: a cached copy is a plaintext second factor sitting
    in a process that also renders error pages."""
    if _ENVELOPE is None:
        raise _secb.BootError("no keystore")
    return _ENVELOPE.open(ciphertext=state["secret_wrapped"], nonce=state["nonce"], tag=str(state["tag"]),
                          aad={"user_id": state["user_id"], "kek_version": int(state["kek_version"]),
                               "dek_version": 0, "policy_hash": "totp"}).decode()


def _stamped(payload: dict, *, ttl_ms: int, stale_ms: int, as_of_ms: int | None = None) -> dict:
    """Stamp a read with the clock the client needs to decide "is this live?".

    `asOf` is the AGE OF THE DATA, not the time we answered, and `staleAfter` is derived from the same number.
    Both used to be `now`, which made a response built from a ten-minute-old book say "fresh for two more
    seconds" while `ageMs` on the same object said 600000 — two fields describing the same fact, disagreeing,
    and the one a UI reads naively is the reassuring one. That is precisely the failure P05's gate exists to
    catch ("the UI showed a stale indicator the whole time"), so the arithmetic now comes from the source row.
    """
    now = _now_ms()
    as_of = int(as_of_ms) if as_of_ms else now
    return {**payload, "asOf": as_of, "staleAfter": as_of + stale_ms, "serverAsOf": now,
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
                                      pattern="^(endsSoon|newMarket|spread|volume24h|liquidity|"
                                              "openInterest|move24h)$"),
                 q: str | None = Query(default=None, max_length=128),
                 category: str | None = Query(default=None, max_length=32),
                 min_volume: int | None = Query(default=None, alias="minVolume24h", ge=0),
                 min_liquidity: int | None = Query(default=None, alias="minLiquidity", ge=0),
                 min_open_interest: int | None = Query(default=None, alias="minOpenInterest", ge=0),
                 ends_within_hours: int | None = Query(default=None, alias="endsWithinHours", ge=1,
                                                       le=87_600),
                 new_within_hours: int | None = Query(default=None, alias="newWithinHours", ge=1,
                                                      le=87_600),
                 neg_risk_only: bool = Query(default=False, alias="negRiskOnly"),
                 include_long_tail: bool = Query(default=False, alias="includeLongTail")):
    """One page of markets, ordered by the one thing the terminal needs first: what is about to resolve.

    P09 turns this into the discovery read, and the three decisions that matter are all about what is NOT
    shown:

      * **The dead tail is off by default.** P01 measured a median event at $19,910/day; the markets below
        `DEAD_TAIL_MICRO` a day are a majority of the count and none of the trades. Hiding them silently
        would break the header count, so the response reports how many were left out (`longTail.hiddenCount`)
        with the threshold, and `includeLongTail=true` is one click.
      * **A 128-market event arrives summarised.** A card that lists 128 rows is unreadable, and one that
        lists the first 3 is wrong. Each row that belongs to an event carries `event.topOutcomes` (the three
        by 24h volume), `event.marketCount` and `event.hiddenCount`, computed with ONE extra query for the
        whole page - a per-row query here would be 50 queries for one screen.
      * **Facets are counted with the filters applied, except the one being faceted.** A category tab that
        zeroes the other tabs is a tab nobody can navigate back from.

    `volume24h` is a sort key now because ingest writes it (`market_stats`, Gamma's own number). In P04 it
    was deliberately absent.

    The cursor encodes `<sort key>|<id>`, i.e. the value the rows were ordered BY. Paging by id while
    ordering by end_ts repeats a market on page 2 and drops another, and no single-page test can see it:
    only walking the whole set does.
    """
    rid = request.state.request_id
    key_sql, direction = _MARKET_SORT[sort_by]
    if category is not None:
        # Canonicalised against the vocabulary rather than passed through: `category=politics` is a different
        # string from `Politics`, and an unknown value is a client bug worth naming, not an empty page that
        # looks like "no markets match".
        match = [c for c in CATEGORY_VOCAB if c.lower() == category.strip().lower()]
        if not match:
            return err("VALIDATION", rid)
        category = match[0]
    # Every predicate is tagged with WHY it exists, because two of the response's own fields are built by
    # REMOVING one: the facets are the counts with the category filter lifted, and hiddenCount is what the
    # tail filter left out. Parallel `where`/`args` lists made that a slicing exercise ("drop the last three
    # args if there is a cursor"), which is the kind of arithmetic that is right until someone inserts a
    # filter in the middle and then silently counts the wrong rows.
    filters: list[tuple[str, str, list]] = []
    if live:
        filters.append(("live", "accepting_orders = 1", []))
    if q:
        # instr(), not LIKE: a LIKE pattern lets a user's own % and _ change what the query means, and there
        # is nothing to gain from letting them. Case is folded on both sides, once. Three columns, not the
        # prompt's four: we do not store tags yet, and searching a column that does not exist would be a
        # sentence in a docstring rather than a working typeahead - the doc says which three.
        filters.append(("q", "instr(lower(question || ' ' || coalesce(slug, '') || ' ' || "
                             "coalesce(category, '')), ?) > 0", [q.lower()]))
    if category is not None:
        filters.append(("category", "category = ?", [category]))
    if min_volume is not None:
        filters.append(("minVolume", "volume_24h_micro >= ?", [min_volume]))
    if min_liquidity is not None:
        filters.append(("minLiquidity", "liquidity_micro >= ?", [min_liquidity]))
    if min_open_interest is not None:
        filters.append(("minOI", "open_interest_micro >= ?", [min_open_interest]))
    if ends_within_hours is not None:
        # `<=` on the deadline and `>=` on now: a market that ended an hour ago is not "ending within 24
        # hours", and including it puts a dead market at the top of the default sort.
        now_ms = _now_ms()
        filters.append(("endsWithin", "end_ts IS NOT NULL AND end_ts <= ? AND end_ts >= ?",
                        [now_ms + ends_within_hours * 3_600_000, now_ms]))
    if new_within_hours is not None:
        filters.append(("newWithin", "first_seen_ms >= ?", [_now_ms() - new_within_hours * 3_600_000]))
    if neg_risk_only:
        filters.append(("negRisk", "neg_risk = 1", []))
    if not include_long_tail:
        filters.append(("tail", "volume_24h_micro >= %d" % DEAD_TAIL_MICRO, []))
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
        filters.append(("cursor", "(%s %s ? OR (%s = ? AND id > ?))" % (key_sql, op, key_sql),
                        [key_val, key_val, last_id]))
    # Two levels on purpose. `spread_micro` is a derived alias, and `COALESCE(spread_micro, ...) AS sort_key`
    # in the SAME select list is a "misuse of aliased column" error in SQLite (and in Postgres) - the sort
    # that only worked in the two tests that did not use it, which is why the contract-vs-app liveness check
    # asks the live app for every documented sort key rather than trusting that one of them works.
    #
    # The joins are LEFT and COALESCEd: a market with no venue statistics yet (ingest has not seen it) must
    # still be listed. An INNER join here is how a discovery screen goes blank during a backfill.
    inner = _discovery_inner(with_spread=True)
    # Inside the subquery every column has a name of its own, so the predicates can be written against names
    # rather than against a column ORDER. The previous revision of this function relied on positional reads
    # and grew a comment numbering its columns; a predicate that says `volume_24h_micro` cannot be broken by
    # inserting a column.
    sql = "SELECT *, " + key_sql + " AS sort_key FROM (" + inner + ") x"
    if filters:
        sql += " WHERE " + " AND ".join(clause for _tag, clause, _a in filters)
    sql += " ORDER BY sort_key %s, id ASC LIMIT ?" % direction
    rows = _db.execute(sql, (*[a for _t, _c, args_ in filters for a in args_], limit + 1)).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    events = _event_summaries([r[EVENT_ID] for r in rows if r[EVENT_ID]])
    items = []
    for r in rows:
        change_micro = r[CHANGE_24H]
        items.append({
            "id": r[ID], "question": r[QUESTION], "acceptingOrders": bool(r[ACCEPTING]),
            "secondsDelay": r[DELAY],
            "minimumTickSize": norm_tick(r[TICK]),       # the SAME normaliser the gate uses, so the tick
            "minimumOrderSize": fmt_usdc(int(round(r[MIN_SIZE] * 10 ** 6))),   # the UI paints and the gate
            "feeType": r[FEE], "enableOrderBook": bool(r[BOOK]), "endTs": r[END_TS],   # cannot disagree
            "spreadMicro": r[SPREAD], "eventId": r[EVENT_ID], "outcomeCount": r[OUTCOME_COUNT] or 0,
            "negRisk": bool(r[NEG_RISK]), "category": r[CATEGORY], "volume24h": fmt_usdc(r[VOLUME_24H]),
            "liquidity": fmt_usdc(r[LIQUIDITY]), "openInterest": fmt_usdc(r[OPEN_INTEREST]),
            "lastPrice": fmt_usdc(r[LAST_PRICE]) if r[LAST_PRICE] is not None else None,
            "price24hAgo": fmt_usdc(r[PRICE_24H_AGO]) if r[PRICE_24H_AGO] is not None else None,
            # signed, and NULL when our tape does not reach back 24h: a "0.00" change is a claim, and we do
            # not have the data to make it
            "change24h": fmt_usdc(change_micro) if change_micro is not None else None,
            "event": events.get(r[EVENT_ID]),
        })
    # sort_key is LAST by construction (`SELECT *, <expr> AS sort_key`), so the cursor does not depend on how
    # many columns the inner select grows to - that is how this line broke once already, silently, when
    # first_seen_ms was added for the newMarket sort: the cursor started encoding a spread and every page
    # after the first repeated the first one.
    nxt = "%d|%s" % (rows[-1][-1], rows[-1][ID]) if (has_more and rows) else None
    return _stamped({"cacheKey": "markets:%s:%s" % (sort_by, cursor), "items": items, "nextCursor": nxt,
                     "pageSizeHardCap": 100, "sortBy": sort_by, "sortKeys": sorted(_MARKET_SORT),
                     "facets": _facets(filters),
                     "longTail": {"includeLongTail": include_long_tail,
                                  "thresholdMicro": DEAD_TAIL_MICRO,
                                  "hiddenCount": _hidden_tail_count(filters)},
                     "categories": list(CATEGORY_VOCAB)},
                    ttl_ms=1000, stale_ms=flags().stale_ms_metadata)


# The inner select's column order, named once. `sqlite3.Row` would be the other way to do this; the P04 note
# beside the row mapping explains why the reads are positional, and THIS is what keeps them honest across an
# edit that inserts a column: the names are the contract, the indices are derived, and
# tests/test_markets_surfaces.py asserts the names are still in the order the SQL selects them.
_DISCOVERY_COLUMNS = (
    "id", "question", "accepting_orders", "seconds_delay", "minimum_tick_size", "minimum_order_size",
    "fee_type", "enable_order_book", "end_ts", "first_seen_ms", "event_id", "outcome_count", "neg_risk",
    "spread_micro", "change_24h_micro", "volume_24h_micro", "liquidity_micro", "open_interest_micro",
    "price_24h_ago_micro", "last_price_micro", "slug", "category")
(ID, QUESTION, ACCEPTING, DELAY, TICK, MIN_SIZE, FEE, BOOK, END_TS, FIRST_SEEN, EVENT_ID, OUTCOME_COUNT,
 NEG_RISK, SPREAD, CHANGE_24H, VOLUME_24H, LIQUIDITY, OPEN_INTEREST, PRICE_24H_AGO, LAST_PRICE, SLUG,
 CATEGORY) = range(len(_DISCOVERY_COLUMNS))


def _discovery_inner(*, with_spread: bool) -> str:
    """The discovery FROM-clause, built once so the page, the facets and the tail count cannot disagree about
    what a market's category, volume or open interest IS. Three hand-copied joins is three chances for one of
    them to forget a COALESCE and make an entire category vanish from its own tab.

    Every filterable column is selected under its OWN name (not `SELECT *`), because the predicate lists are
    written against names: a where-clause that says `volume_24h_micro` keeps working when a column is
    inserted, and the earlier positional version of this function did not.

    `with_spread` is false for the counting queries: the spread is a correlated subquery per row and nothing
    filters or groups by it. Paying for it three times to answer "how many markets per category" is how a
    facets query ends up slower than the page it annotates.
    """
    spread = ("" if not with_spread else
              "(SELECT MIN(price_micro) FROM book_levels b WHERE b.market_id = m.id AND b.side = 'ask') - "
              "(SELECT MAX(price_micro) FROM book_levels b WHERE b.market_id = m.id AND b.side = 'bid') "
              "AS spread_micro, ")
    change = ("CASE WHEN a.price_24h_ago_micro IS NULL OR a.last_price_micro IS NULL THEN NULL "
              "ELSE a.last_price_micro - a.price_24h_ago_micro END AS change_24h_micro, ")
    return ("SELECT m.id AS id, m.question AS question, m.accepting_orders AS accepting_orders, "
            "m.seconds_delay AS seconds_delay, m.minimum_tick_size AS minimum_tick_size, "
            "m.minimum_order_size AS minimum_order_size, m.fee_type AS fee_type, "
            "m.enable_order_book AS enable_order_book, m.end_ts AS end_ts, "
            "m.first_seen_ms AS first_seen_ms, m.event_id AS event_id, "
            # NOT `m.outcome_count`. That column is Postgres-only: it is GENERATED ALWAYS AS
            # jsonb_array_length(outcomes_json) STORED, and the portable subset the suite runs on DROPS
            # generated columns (recorded in db/migrations-sqlite/DROPPED.json). Reading it made the
            # discovery screen work in production and 500 in the test engine - the exact "tests pass,
            # production differs" shape the transpiler's drop record exists to expose, caught here only
            # because the tests run on the subset. The count comes from `tokens`, which both engines have and
            # which is what the generated column was deriving from in the first place.
            "(SELECT COUNT(*) FROM tokens tk WHERE tk.market_id = m.id) AS outcome_count, "
            "m.neg_risk AS neg_risk, " + spread + change +
            "COALESCE(s.volume_24h_micro, 0) AS volume_24h_micro, "
            "COALESCE(s.liquidity_micro, 0) AS liquidity_micro, "
            "COALESCE(a.open_interest_micro, 0) AS open_interest_micro, "
            "a.price_24h_ago_micro AS price_24h_ago_micro, a.last_price_micro AS last_price_micro, "
            "m.slug AS slug, COALESCE(mt.category, 'Other') AS category "
            "FROM markets m "
            "LEFT JOIN market_stats s ON s.condition_id = m.condition_id "
            "LEFT JOIN market_activity a ON a.market_id = m.id "
            "LEFT JOIN market_meta mt ON mt.market_id = m.id")


def _clauses(filters: list[tuple[str, str, list]], drop: set[str]) -> tuple[str, list]:
    kept = [(clause, args_) for tag, clause, args_ in filters if tag not in drop]
    sql = (" WHERE " + " AND ".join(c for c, _a in kept)) if kept else ""
    return sql, [a for _c, args_ in kept for a in args_]


def _facets(filters: list[tuple[str, str, list]]) -> dict:
    """Market counts per category, with every filter applied EXCEPT the category one, and without the cursor
    (which is about position in a list, not membership in it). A category tab that zeroes the other tabs is a
    tab nobody can navigate back from."""
    where, args = _clauses(filters, drop={"category", "cursor"})
    sql = ("SELECT category, COUNT(*) FROM (" + _discovery_inner(with_spread=False) + ") x"
           + where + " GROUP BY category")
    return {row[0]: row[1] for row in _db.execute(sql, args).fetchall()}


def _hidden_tail_count(filters: list[tuple[str, str, list]]) -> int:
    """How many markets the dead-tail filter is hiding, with every OTHER filter still applied. Zero when the
    caller asked for the long tail - the number means "left out", and nothing was."""
    if not any(tag == "tail" for tag, _c, _a in filters):
        return 0
    # Count the COMPLEMENT, with every other filter still applied. The first version dropped the tail clause
    # and counted what was left - i.e. it reported the size of the VISIBLE set as the number of hidden
    # markets, and the two differ by exactly everything the user can see. It looked plausible on a fixture
    # where nothing was hidden at all (0 hidden, 0 visible), which is why the seed now contains a tail.
    where, args = _clauses(filters, drop={"tail", "cursor"})
    tail = "volume_24h_micro < %d" % DEAD_TAIL_MICRO
    sql = ("SELECT COUNT(*) FROM (" + _discovery_inner(with_spread=False) + ") x"
           + (where + " AND " if where else " WHERE ") + tail)
    return int(_db.execute(sql, args).fetchone()[0])


def _event_summaries(event_ids: list[str]) -> dict:
    """Top-3-by-volume outcomes for every event on the page, in ONE query per fact.

    Window function, not a loop: ROW_NUMBER() OVER (PARTITION BY ...) is supported by every engine this runs
    on (SQLite >= 3.25, Postgres) and it is the difference between one round trip and 50 for a page of 50
    events. A per-row query would also have been invisible in the fixture, where the biggest event has six
    markets - the shape this exists for has 128.
    """
    if not event_ids:
        return {}
    marks = ",".join("?" * len(event_ids))
    meta_rows = _db.execute(
        "SELECT m.event_id, e.title, COUNT(*) AS market_count, "
        "COALESCE(SUM(s.volume_24h_micro), 0) AS total_volume "
        "FROM markets m LEFT JOIN events e ON e.id = m.event_id "
        "LEFT JOIN market_stats s ON s.condition_id = m.condition_id "
        "WHERE m.event_id IN (%s) GROUP BY m.event_id, e.title" % marks, event_ids).fetchall()
    # volume comes back in the SAME row as the ranking: an earlier draft then queried the volume per top
    # outcome, three extra round trips per event for a number it had already selected.
    top_rows = _db.execute(
        "SELECT event_id, market_id, question, price_micro, volume_micro FROM ("
        "  SELECT m.event_id AS event_id, m.id AS market_id, m.question AS question, "
        "         (COALESCE((SELECT MAX(price_micro) FROM book_levels b WHERE b.market_id = m.id AND "
        "          b.side = 'bid'), 0) + COALESCE((SELECT MIN(price_micro) FROM book_levels b WHERE "
        "          b.market_id = m.id AND b.side = 'ask'), 0)) / 2 AS price_micro, "
        "         COALESCE(s.volume_24h_micro, 0) AS volume_micro, "
        "         ROW_NUMBER() OVER (PARTITION BY m.event_id ORDER BY COALESCE(s.volume_24h_micro, 0) DESC, "
        "         m.id ASC) AS rn "
        "  FROM markets m LEFT JOIN market_stats s ON s.condition_id = m.condition_id "
        "  WHERE m.event_id IN (%s)) ranked WHERE rn <= 3" % marks, event_ids).fetchall()
    tops: dict[str, list] = {}
    for event_id, market_id, question, price_micro, volume_micro in top_rows:
        tops.setdefault(event_id, []).append({
            "marketId": market_id, "question": question,
            # a zero price is "no book", not "$0.00 a share": the client renders a dash
            "price": fmt_usdc(price_micro) if price_micro else None,
            "volume24h": fmt_usdc(int(volume_micro))})
    out = {}
    for event_id, title, count, total in meta_rows:
        out[event_id] = {"id": event_id, "title": title, "marketCount": int(count),
                         "totalVolume24h": fmt_usdc(int(total)),
                         "topOutcomes": tops.get(event_id, []),
                         "hiddenCount": max(0, int(count) - len(tops.get(event_id, [])))}
    return out


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
    # the newest fill's venue timestamp is the age of this answer; an empty tape has no data age, and `now` is
    # then honest (there is nothing older being misrepresented)
    return _stamped({"cacheKey": f"tape:{market_id}:{since}", "rows": out,
                     "nextCursor": (rows[-1][4] if has_more and rows else None)},
                    ttl_ms=500, stale_ms=flags().stale_ms_tape,
                    as_of_ms=(max((r[4] for r in rows), default=None)))


@app.get("/v1/markets/{market_id}/fills", responses=FILLS_RESPONSES)
def get_market_fills(market_id: str, request: Request,
                     since: int | None = Query(default=None, ge=0),
                     limit: int = Query(default=100, ge=1, le=1000)):
    """The fills P05's ingest actually stored, which is a different table from `/v1/tape`'s `tape_trades`.

    Both exist on purpose, and the distinction is the phase: `tape_trades` is the P04 fixture the risk gate and
    the tape widget were built against, while `tape_fills` is the durable, deduplicated log the venue's tape
    lands in — the one the rollups, the whale percentile and the leaderboard read. Serving only the first would
    leave the ingest writing a table nobody reads, which is how a pipeline rots.

    `since` is exclusive and in the VENUE's clock (`ts_ms`), matching `/v1/tape` for the same reason: mixing the
    two clocks produces either a duplicate or a hole, and a hole in a fill log reads as a suppressed trade.

    No wallet address is returned. `anonWallet` is a stable pseudonym so a client can group fills by trader,
    and the labels that hang off that wallet come along because a labelled fill without its label is the half of
    the feature that took a whole phase to earn.
    """
    rid = request.state.request_id
    mrow = _db.execute("SELECT condition_id FROM markets WHERE id=?", (market_id,)).fetchone()
    if mrow is None:
        return err("NOT_FOUND", rid)
    cid = str(mrow[0])
    sql = ("SELECT price_micro, size_micro, usd_notional_micro, side, outcome, wallet, ts_ms, ingest_ms, "
           "source FROM tape_fills WHERE condition_id=?")
    args: list = [cid]
    if since is not None:
        sql += " AND ts_ms < ?"
        args.append(since)
    sql += " ORDER BY ts_ms DESC, rowid DESC LIMIT ?"
    rows = _db.execute(sql, (*args, limit + 1)).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    labelled = dict(_db.execute("SELECT wallet, label FROM wallet_labels WHERE publishable=1").fetchall()
                   ) if rows else {}
    out = []
    for price, size, notional, side, outcome, wallet, ts, ingest_ms, source in rows:
        out.append({"price": fmt_usdc(price), "shares": _shares(size), "notional": fmt_usdc(notional),
                    "side": side, "outcome": outcome, "venueTs": ts, "ingestMs": ingest_ms, "source": source,
                    "anonWallet": _anon(wallet), "labels": [labelled[wallet]] if wallet in labelled else []})
    return _stamped({"cacheKey": f"fills:{market_id}:{since}", "market": market_id, "conditionId": cid,
                     "rows": out, "nextCursor": (rows[-1][6] if has_more and rows else None)},
                    ttl_ms=1000, stale_ms=flags().stale_ms_tape,
                    as_of_ms=(max((r[6] for r in rows), default=None)))


# sort_by -> (SQL expression, direction). The expression is ALSO selected as a trailing column, because a
# cursor has to encode exactly the value the rows were ordered by: deriving it from a column index is how
# `newMarket` (whose key is not in the original select list) nearly shipped a cursor that skipped markets.
# The sentinel is 2**63-1, which no real millisecond timestamp reaches, and it is what puts undated markets
# at the END of an ascending sort.
_SENTINEL = 9223372036854775807
# P09 adds four keys, and each one is a column somebody maintains. That is the rule this table follows since
# `volume24h` was deliberately withheld in P04 ("sorting by a number nobody has computed orders by NULLs and
# reads like a broken sort"): a sort key may only name a column that ingest writes. volume24h/liquidity come
# from `market_stats` (Gamma's own numbers), openInterest from `market_activity`, and move24h is the
# difference between the newest fill we hold and the one we held 24h ago - NULL, not 0, when our tape does
# not reach back that far, because a move against a price of zero renders as +100%.
_MARKET_SORT = {
    "endsSoon": ("COALESCE(end_ts, %d)" % _SENTINEL, "ASC"),
    "newMarket": ("first_seen_ms", "DESC"),
    "spread": ("COALESCE(spread_micro, %d)" % _SENTINEL, "ASC"),   # legal HERE: this is the outer query
    "volume24h": ("volume_24h_micro", "DESC"),
    "liquidity": ("liquidity_micro", "DESC"),
    "openInterest": ("open_interest_micro", "DESC"),
    "move24h": ("COALESCE(change_24h_micro, %d)" % -_SENTINEL, "DESC"),
}

# The P09 filter vocabulary. `Other` is in the list because an unclassified market has to render somewhere and
# a tab that hides rows is a tab that makes the count in the header a lie.
CATEGORY_VOCAB = ("Politics", "Sports", "Crypto", "Finance", "Economics", "Tech", "Culture", "Weather",
                  "Geopolitics", "Other")
# The dead tail. P01 measured a median event at $19,910/day, so "$1,000 of 24h volume" is far below the median
# and still well above the noise: the markets it hides are the ones with no realistic fill. The default view is
# what a user can actually trade; the long tail is one click away and its size is reported, never implied.
DEAD_TAIL_MICRO = 1_000_000_000
# Aggregate-by ladder steps, in micro-USDC. `None` is the raw tick. Rounding is DIRECTIONAL and conservative:
# a bid aggregates DOWN and an ask UP, so an aggregated ladder never shows a price you could not have got.
AGGREGATES = {"raw": None, "1c": 10_000, "5c": 50_000}
# Which candle widths the SERVER builds. 6h and 1d are derivable by bucketing 1h candles on the client for
# free; asking the server for them would be a second, weaker implementation of the same maths (docs/P09
# D5). The list is served WITH the candles so a client feature-detects instead of guessing from a changelog.
HISTORY_INTERVALS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}
DERIVED_INTERVALS = ("6h", "1d")


@app.get("/v1/markets/{market_id}", responses=MARKET_RESPONSES)
def get_market(market_id: str, request: Request):
    """The market's own row plus everything the info rail renders.

    One round trip, not five: the rail shows the category, the resolution text, liquidity, three volume
    windows, open interest, the last price, the 24h change and the holder count. A client assembling that from
    five endpoints would show five different `asOf` stamps for one page, and the freshness indicator is
    supposed to answer "how old is what I am looking at", singular.
    """
    row = _db.execute(
        "SELECT m.id, m.question, m.accepting_orders, m.seconds_delay, m.minimum_tick_size, "
        "m.minimum_order_size, m.fee_type, m.enable_order_book, m.end_ts, m.event_id, "
        "(SELECT COUNT(*) FROM tokens tk WHERE tk.market_id = m.id), "      # see _discovery_inner: the
        "m.neg_risk, m.slug, e.title AS event_title, e.slug AS event_slug, "      # generated column is PG-only

        "COALESCE(mt.category, 'Other'), mt.resolution_source, mt.resolution_criteria, "
        "COALESCE(s.volume_24h_micro, 0), COALESCE(s.liquidity_micro, 0), "
        "COALESCE(a.open_interest_micro, 0), COALESCE(a.volume_7d_micro, 0), "
        "COALESCE(a.volume_30d_micro, 0), a.price_24h_ago_micro, a.last_price_micro "
        "FROM markets m LEFT JOIN events e ON e.id = m.event_id "
        "LEFT JOIN market_meta mt ON mt.market_id = m.id "
        "LEFT JOIN market_stats s ON s.condition_id = m.condition_id "
        "LEFT JOIN market_activity a ON a.market_id = m.id WHERE m.id = ?", (market_id,)).fetchone()
    if row is None:
        return err("NOT_FOUND", request.state.request_id)
    # The count of wallets WE have seen trade, not the venue's holder list: the venue does not publish one for
    # every market, and a number that silently means two different things is worse than a number that says
    # which one it is. The rail labels it "seen trading" for exactly this reason.
    holders = _db.execute("SELECT COUNT(DISTINCT wallet) FROM tape_fills WHERE condition_id = "
                          "(SELECT condition_id FROM markets WHERE id = ?)", (market_id,)).fetchone()
    # SELECT order: ... 21 volume_7d, 22 volume_30d, 23 price_24h_ago, 24 last_price
    change = None if (row[23] is None or row[24] is None) else int(row[24]) - int(row[23])
    market = {
        "id": row[0], "question": row[1], "acceptingOrders": bool(row[2]), "secondsDelay": row[3],
        # the same normalisation the gate uses, so the number the UI renders and the number the gate compares
        # cannot disagree (Postgres "0.0100" vs SQLite 0.01 vs the gate's "0.01")
        "minimumTickSize": norm_tick(row[4]),
        # formatted exactly as the list endpoint formats it: SQLite hands back a REAL for a NUMERIC column and
        # Postgres a Decimal, so returning the raw value is a `5.0` in one engine and a `5` in the other - and
        # the contract says string. The two endpoints disagreeing is how a client ends up validating against
        # one number and displaying another.
        "minimumOrderSize": fmt_usdc(int(round(float(row[5]) * 10 ** 6))), "feeType": row[6],
        "enableOrderBook": bool(row[7]), "endDate": row[8], "eventId": row[9],
        "outcomeCount": row[10] or 0, "negRisk": bool(row[11]), "slug": row[12],
        "eventTitle": row[13], "eventSlug": row[14], "category": row[15],
        # Verbatim, both of them. `resolutionCriteria` is written by whoever created the market and is the one
        # string on the page an outsider controls, so it is returned as data and the CLIENT is what renders it
        # as text. Sanitising here instead would hide the fixture that proves the client does not trust it.
        "resolutionSource": row[16], "resolutionCriteria": row[17],
        "volume24h": fmt_usdc(int(row[18])), "liquidity": fmt_usdc(int(row[19])),
        "openInterest": fmt_usdc(int(row[20])), "volume7d": fmt_usdc(int(row[21])),
        "volume30d": fmt_usdc(int(row[22])),
        "price24hAgo": fmt_usdc(int(row[23])) if row[23] is not None else None,
        "lastPrice": fmt_usdc(int(row[24])) if row[24] is not None else None,
        "change24h": fmt_usdc(change) if change is not None else None,
        "holderCount": int(holders[0]) if holders else 0,
    }
    return _stamped({"cacheKey": f"market:{market_id}", "market": market},
                    ttl_ms=flags().cache_ttl_market_ms, stale_ms=flags().stale_ms_metadata)


@app.get("/v1/markets/{market_id}/book", responses=BOOK_RESPONSES)
def get_book(market_id: str, request: Request, depth: int = Query(default=24, ge=1, le=400),
             aggregate: str = Query(default="raw", pattern="^(raw|1c|5c)$")):
    """Books come from `ingest`'s materialised snapshot, never proxied live: one user must not be able to
    spend the shared rate budget (rule 2).

    P09 adds two things the ladder cannot be built without:

      * **`aggregate`.** A market with a 0.001 tick has 1000 price levels per dollar; rendering them raw is
        both unreadable and expensive (the prompt's own observation: 94 levels out of a live book). Buckets
        are 1c and 5c. Rounding is DIRECTIONAL - a bid rounds DOWN and an ask rounds UP - so an aggregated
        ladder never displays a price the user could not have got. Aggregating in the client would need float
        bucketing of a decimal string, which is where money bugs come from.
      * **`oneSided`.** A book with asks and no bids is not a broken feed, it is a market where everyone
        holding the other side has left: P01 measured 94 ask levels at 0.001 totalling $21.9M against zero
        bids. The verdict is computed here, once, so the ladder, the depth chart and the ticket cannot
        disagree about whether the market is one-sided - and `why` is a machine code, not a sentence, because
        the client owns its own wording.
    """
    # one query per side, each with its own LIMIT: a single ORDER BY side,price DESC with LIMIT 2k is the
    # classic way to return 2k bids and no asks, which then renders as a one-sided book (P01 measured 0/22
    # one-sided markets in reality, so the bug would have hidden in the data layer).
    # The LIMIT is applied BEFORE aggregation, on the raw ladder, and that is deliberate: aggregating first
    # would need every level in memory to bucket it, which is the cost this endpoint exists to avoid. The
    # consequence - a 24-level page can aggregate into fewer than 24 buckets - is documented on `buckets`.
    per = []
    for side, ord_ in (("bid", "DESC"), ("ask", "ASC")):
        per.append((side, _db.execute(
            "SELECT side,price_micro,size_shares_micro,level_count,updated_ms FROM book_levels"
            " WHERE market_id=? AND side=? ORDER BY price_micro " + ord_ + " LIMIT ?",
            (market_id, side, depth)).fetchall()))
    rows = [r for _, rs in per for r in rs]
    if not rows:
        return err("NOT_FOUND", request.state.request_id)
    meta = _db.execute("SELECT minimum_tick_size FROM markets WHERE id=?", (market_id,)).fetchone()
    tick_micro = int(round(float(meta[0]) * 10 ** 6)) if meta and meta[0] else 10_000

    def bucket(levels: list[tuple], step: int | None, up: bool) -> list[dict]:
        """Fold the raw ladder into price buckets, keeping the WORST price in each bucket for the side.

        Worst, not best, because the bucket's number is a promise to the reader: "you can trade at least this
        well inside this band". A bid bucket that reports the highest price in the band promises fills that
        the lower levels inside it will not deliver.
        """
        if step is None:
            return [{"price": fmt_usdc(p), "shares": _shares(s), "levels": lv} for _sd, p, s, lv, _u in levels]
        out: dict[int, dict] = {}
        for _sd, p, s, lv, _u in levels:
            key = ((p + step - 1) // step) * step if up else (p // step) * step
            slot = out.setdefault(key, {"priceMicro": key, "sharesMicro": 0, "levels": 0})
            slot["sharesMicro"] += int(s)
            slot["levels"] += int(lv)
        ordered = sorted(out.values(), key=lambda d: d["priceMicro"], reverse=not up)
        return [{"price": fmt_usdc(d["priceMicro"]), "shares": _shares(d["sharesMicro"]),
                 "levels": d["levels"]} for d in ordered]

    step = AGGREGATES[aggregate]
    bids, asks = bucket(per[0][1], step, up=False), bucket(per[1][1], step, up=True)
    age = _now_ms() - max(r[4] for r in rows)
    bid_top = max((r[1] for r in rows if r[0] == "bid"), default=None)
    ask_top = min((r[1] for r in rows if r[0] == "ask"), default=None)
    spread_ticks = None
    if bid_top is not None and ask_top is not None and tick_micro:
        spread_ticks = round((ask_top - bid_top) / tick_micro, 3)     # metadata maths, not money
    # The rows the ladder and the chart both need, computed once. `cumShares` is cumulative FROM THE TOP, so
    # the depth bar's width is a fraction of the biggest cumulative figure on either side.
    for side_rows in (bids, asks):
        running = 0
        for level in side_rows:
            running += _micro_of(level["shares"])
            level["cumShares"] = _shares(running)
    cum_max = max([_micro_of(l["cumShares"]) for l in bids + asks] or [0])
    # `shares` in these rows is a decimal string; the client sums in integer micro units, never as a float,
    # which is why the cumulative is served rather than left to be derived from floats in a chart loop.
    one_sided = None
    if bid_top is None and ask_top is not None:
        one_sided = {"side": "asks-only", "why": "NO_BIDS", "levels": len(asks),
                     "notionalUsdc": fmt_usdc(_notional_micro([(l["price"], l["shares"]) for l in asks])),
                     "bestAsk": fmt_usdc(ask_top)}
    elif ask_top is None and bid_top is not None:
        one_sided = {"side": "bids-only", "why": "NO_ASKS", "levels": len(bids),
                     "notionalUsdc": fmt_usdc(_notional_micro([(l["price"], l["shares"]) for l in bids])),
                     "bestBid": fmt_usdc(bid_top)}
    return _stamped({"cacheKey": f"book:{market_id}:{depth}:{aggregate}", "market": market_id,
                     "aggregate": aggregate, "aggregateStep": fmt_usdc(step) if step else None,
                     "tickSize": norm_tick(meta[0]) if meta and meta[0] else None,
                     # best-first on both sides, per the design system
                     "bids": bids, "asks": asks,
                     "spreadTicks": spread_ticks,
                     "midPrice": fmt_usdc((bid_top + ask_top) // 2) if (bid_top and ask_top) else None,
                     "bestBid": fmt_usdc(bid_top) if bid_top is not None else None,
                     "bestAsk": fmt_usdc(ask_top) if ask_top is not None else None,
                     "oneSided": one_sided, "maxCumShares": _shares(cum_max),
                     "ageMs": age},
                    ttl_ms=flags().cache_ttl_books_ms, stale_ms=flags().stale_ms_book,
                    as_of_ms=max(r[4] for r in rows))   # the ladder's own `updated_ms`, i.e. `ageMs`'s source


def _notional_micro(levels: list[tuple[str, str]]) -> int:
    """Sum of price x shares in micro-units, from the DECIMAL STRINGS the payload carries.

    Integer arithmetic end to end: parse the two strings into micro-integers, multiply, divide by 10^6 once.
    The first version of this multiplied floats (`float(price) * float(shares)`) and then rounded - which is
    the exact money-path float the repo's rules forbid, in the one place it is least visible, a footer number
    that nobody diffs.
    """
    total = 0
    for price, shares in levels:
        p = _micro_of(price)
        s = _micro_of(shares)
        total += p * s // 10 ** 6
    return total


def _micro_of(decimal: str) -> int:
    """`"0.001"` -> 1000. Whole and fractional parts are parsed separately; `int(float(x) * 10**6)` rounds
    wrong on values like 0.1+0.2 and raises on nothing, so it fails silently."""
    whole, _, frac = str(decimal).partition(".")
    frac = (frac + "000000")[:6]
    sign = -1 if whole.startswith("-") else 1
    return sign * (abs(int(whole)) * 10 ** 6 + int(frac or "0"))


# --------------------------------------------------------------------------- #
# P09 · the market surfaces: history, the event page, and holders


@app.get("/v1/markets/{market_id}/history", responses=HISTORY_RESPONSES)
def get_market_history(market_id: str, request: Request,
                       interval: str = Query(default="1m", pattern="^(1m|5m|15m|1h)$"),
                       limit: int = Query(default=200, ge=2, le=1000)):
    """Candles built from OUR fills, at the widths the server owns.

    Deliberate limits, stated rather than implied:
      * **These are not venue candles.** The venue's `/prices-history` was measured in P01 and it is a
        different series (it samples mid-price on its own schedule, and it is Cloudflare-cached). Ours are
        trade-derived: a bucket with no fills is a GAP, not a flat candle, and `trades: 0` says so. A chart
        that draws a line through a gap is a chart that invents prices.
      * **1m..1h are built here; 6h and 1d are derived by the client** by bucketing these 1h candles. The
        split is served in `derivedIntervals` so a client feature-detects instead of trusting a changelog.
      * **The window is bounded by `limit` buckets, not by an unbounded scan.** Asking for 1000 hourly candles
        means reading a month of fills; the request says how far back it goes (`fromTs`) so the chart can
        render "the tape does not reach that far" instead of a flat line at the left edge.
    """
    rid = request.state.request_id
    if _db.execute("SELECT 1 FROM markets WHERE id=?", (market_id,)).fetchone() is None:
        return err("NOT_FOUND", rid)
    bucket_ms = HISTORY_INTERVALS[interval]
    now = _now_ms()
    from_ts = now - bucket_ms * limit
    rows = _db.execute(
        "SELECT price_micro, size_micro, usd_notional_micro, ts_ms FROM tape_fills WHERE condition_id = "
        "(SELECT condition_id FROM markets WHERE id = ?) AND ts_ms >= ? ORDER BY ts_ms ASC LIMIT 20000",
        (market_id, from_ts)).fetchall()
    # Buckets are built in integer micro units throughout; the bucket key is floored division of the venue
    # clock, which is the ONLY clock a chart may bucket by (mixing it with ingest_ms spreads one trade over
    # two candles during a lag spike).
    buckets: dict[int, dict] = {}
    for price, size, notional, ts in rows:
        key = int(ts) // bucket_ms * bucket_ms
        b = buckets.get(key)
        if b is None:
            buckets[key] = {"t": key, "o": int(price), "h": int(price), "l": int(price), "c": int(price),
                            "sizeMicro": int(size), "notionalMicro": int(notional), "trades": 1}
        else:
            b["h"] = max(b["h"], int(price)); b["l"] = min(b["l"], int(price)); b["c"] = int(price)
            b["sizeMicro"] += int(size); b["notionalMicro"] += int(notional); b["trades"] += 1
    candles = [{"t": b["t"], "o": fmt_usdc(b["o"]), "h": fmt_usdc(b["h"]), "l": fmt_usdc(b["l"]),
                "c": fmt_usdc(b["c"]), "shares": _shares(b["sizeMicro"]), "trades": b["trades"],
                # The volume histogram is sized by the NOTIONAL the fill actually carried, not by shares: a
                # histogram of share counts makes a 1000-share trade at 0.001 look like a whale.
                "notional": fmt_usdc(b["notionalMicro"])}
               for b in sorted(buckets.values(), key=lambda d: d["t"])]
    return _stamped({"cacheKey": f"history:{market_id}:{interval}:{limit}", "market": market_id,
                     "interval": interval, "bucketMs": bucket_ms, "fromTs": from_ts, "candles": candles,
                     "serverIntervals": sorted(HISTORY_INTERVALS), "derivedIntervals": list(DERIVED_INTERVALS),
                     "source": "fills", "sampled": len(rows) < 20000},
                    ttl_ms=flags().cache_ttl_books_ms, stale_ms=flags().stale_ms_price,
                    as_of_ms=(rows[-1][2] if rows else None))


@app.get("/v1/events/{event_id}", responses=EVENT_RESPONSES)
def get_event(event_id: str, request: Request, limit: int = Query(default=400, ge=1, le=400)):
    """One negRisk event and every market under it - the 128-row table, plus the invariant it must satisfy.

    The invariant, and what "opportunity" means: for a negRisk event the YES prices partition one dollar, so
    the sum of the mids must be near 1. Near, not equal: each price is only known to a tick, so the sum of N
    quotes is only known to N ticks and `tolerance` is exactly that. The *tradable* statement is stronger and
    is computed from executable levels rather than mids - buying every outcome at the best ask must cost more
    than a dollar, and selling every outcome at the best bid must return less - because that is the pair of
    orders a user could actually send. `opportunity` is null when neither side is exploitable, which is the
    honest answer for the vast majority of events; a screen that shouts on every 1-tick deviation teaches its
    reader to ignore it.
    """
    rid = request.state.request_id
    ev = _db.execute("SELECT id, slug, title, neg_risk, category, end_ts FROM events WHERE id=?",
                     (event_id,)).fetchone()
    if ev is None:
        return err("NOT_FOUND", rid)
    rows = _db.execute(
        "SELECT m.id, m.question, m.accepting_orders, m.minimum_tick_size, m.minimum_order_size, "
        "COALESCE(s.volume_24h_micro, 0), COALESCE(s.liquidity_micro, 0), COALESCE(a.open_interest_micro, 0), "
        "a.price_24h_ago_micro, a.last_price_micro, "
        "(SELECT MAX(price_micro) FROM book_levels b WHERE b.market_id = m.id AND b.side = 'bid'), "
        "(SELECT MIN(price_micro) FROM book_levels b WHERE b.market_id = m.id AND b.side = 'ask') "
        "FROM markets m LEFT JOIN market_stats s ON s.condition_id = m.condition_id "
        "LEFT JOIN market_activity a ON a.market_id = m.id WHERE m.event_id = ? "
        "ORDER BY COALESCE(s.volume_24h_micro, 0) DESC, m.id ASC LIMIT ?", (event_id, limit)).fetchall()
    outcomes, sum_mid, sum_bid, sum_ask, tolerance_micro, tick_sum = [], 0, 0, 0, 0, 0
    for (mid_, question, accepting, tick, min_size, vol, liq, oi, p24, last, best_bid,
         best_ask) in rows:
        tick_micro = _micro_of(norm_tick(tick))
        # Mid is the reference for the SUM. A market with only one side has no mid, and pretending its last
        # trade is its price would put a stale number into an invariant that is supposed to be live.
        if best_bid is not None and best_ask is not None:
            mid_p = (int(best_bid) + int(best_ask)) // 2
        elif last is not None:
            mid_p = int(last)
        else:
            mid_p = None
        change = None if (p24 is None or last is None) else int(last) - int(p24)
        tolerance_micro += tick_micro
        tick_sum += 1
        outcomes.append({
            "marketId": mid_, "question": question, "acceptingOrders": bool(accepting),
            "minimumTickSize": norm_tick(tick), "minimumOrderSize": str(min_size),
            "price": fmt_usdc(mid_p) if mid_p is not None else None,
            "bestBid": fmt_usdc(int(best_bid)) if best_bid is not None else None,
            "bestAsk": fmt_usdc(int(best_ask)) if best_ask is not None else None,
            "volume24h": fmt_usdc(int(vol)), "liquidity": fmt_usdc(int(liq)),
            "openInterest": fmt_usdc(int(oi)),
            "change24h": fmt_usdc(change) if change is not None else None,
            # a market with one side (or none) cannot be summed into a partition, and the client must be able
            # to say WHICH rows were left out rather than quietly summing anyway
            "summable": mid_p is not None,
        })
        if mid_p is not None:
            sum_mid += mid_p
        if best_bid is not None:
            sum_bid += int(best_bid)
        if best_ask is not None:
            sum_ask += int(best_ask)
    summable = sum(1 for o in outcomes if o["summable"])
    deviation_micro = sum_mid - 10 ** 6
    # Executable edges. Buying one of every outcome costs sum(best ask); that is profitable when it is under a
    # dollar. Selling one of every outcome (which requires holding a short on each) returns sum(best bid);
    # profitable above a dollar. Both are stated in micro dollars so the client renders them like any money.
    buy_edge = 10 ** 6 - sum_ask if all(o["bestAsk"] for o in outcomes) and outcomes else None
    sell_edge = sum_bid - 10 ** 6 if all(o["bestBid"] for o in outcomes) and outcomes else None
    opportunity = None
    if buy_edge is not None and buy_edge > max(tolerance_micro // tick_sum if tick_sum else 0, 0):
        opportunity = {"kind": "buy-all", "edge": fmt_usdc(buy_edge)}
    elif sell_edge is not None and sell_edge > max(tolerance_micro // tick_sum if tick_sum else 0, 0):
        opportunity = {"kind": "sell-all", "edge": fmt_usdc(sell_edge)}
    return _stamped({
        "cacheKey": f"event:{event_id}",
        "event": {"id": ev[0], "slug": ev[1], "title": ev[2], "negRisk": bool(ev[3]), "category": ev[4],
                  "endTs": ev[5], "marketCount": len(rows)},
        "outcomes": outcomes,
        "probabilitySum": fmt_usdc(sum_mid) if summable else None,
        "deviation": fmt_usdc(deviation_micro) if summable else None,
        "tolerance": fmt_usdc(tolerance_micro),          # N outcomes x 1 tick each
        "withinTolerance": abs(deviation_micro) <= tolerance_micro if summable else None,
        "summableCount": summable,
        "buyAllCost": fmt_usdc(sum_ask) if all(o["bestAsk"] for o in outcomes) and outcomes else None,
        "sellAllProceeds": fmt_usdc(sum_bid) if all(o["bestBid"] for o in outcomes) and outcomes else None,
        "opportunity": opportunity,
    }, ttl_ms=flags().cache_ttl_books_ms, stale_ms=flags().stale_ms_price)


@app.get("/v1/markets/{market_id}/holders", responses=HOLDERS_RESPONSES)
def get_market_holders(market_id: str, request: Request, limit: int = Query(default=20, ge=1, le=100)):
    """The wallets we have seen trade this market, by notional, with their published labels.

    Provenance is stated because it is not the venue's holder list: it is OUR fills, so a whale that bought
    before our ingest started is invisible, and a wallet that only ever placed unfilled orders never appears.
    `provenance` says "tape" and the rail renders that word next to the heading. The address itself is never
    returned - the same rule as `/v1/tape`: a public list of the wallets our users trade against is an address
    book, and `insider_suspect` is not a publishable label, so the join filters on `publishable` exactly as
    the tape does.
    """
    rid = request.state.request_id
    mrow = _db.execute("SELECT condition_id FROM markets WHERE id=?", (market_id,)).fetchone()
    if mrow is None:
        return err("NOT_FOUND", rid)
    rows = _db.execute(
        "SELECT wallet, SUM(usd_notional_micro) AS notional, COUNT(*) AS fills, MIN(ts_ms), MAX(ts_ms) "
        "FROM tape_fills WHERE condition_id = ? GROUP BY wallet ORDER BY notional DESC, wallet ASC LIMIT ?",
        (str(mrow[0]), limit)).fetchall()
    total = _db.execute("SELECT COALESCE(SUM(usd_notional_micro), 0) FROM tape_fills WHERE condition_id = ?",
                        (str(mrow[0]),)).fetchone()
    labels = dict(_db.execute("SELECT wallet, label FROM wallet_labels WHERE publishable=1").fetchall())
    out = []
    for wallet, notional, fills, first_ts, last_ts in rows:
        out.append({"anonWallet": _anon(wallet), "notional": fmt_usdc(int(notional)),
                    "fills": int(fills), "firstSeenMs": int(first_ts), "lastSeenMs": int(last_ts),
                    # share of the tape's notional, in basis points, integer maths all the way
                    "shareBp": int(int(notional) * 10_000 // int(total[0])) if total and total[0] else 0,
                    "labels": [labels[wallet]] if wallet in labels else []})
    return _stamped({"cacheKey": f"holders:{market_id}:{limit}", "market": market_id, "holders": out,
                     "provenance": "tape", "holderCount": len(out)},
                    ttl_ms=flags().cache_ttl_market_ms, stale_ms=flags().stale_ms_metadata)


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


def _check_body(body: object, required: tuple[str, ...], rid: str,
                allowed: tuple[str, ...] | None = None) -> JSONResponse | None:
    """Shape before semantics: a missing field must be a 422 naming the field, not a 500 from a KeyError
    (which is what `body["tokenId"]` produced for exactly this request, in the first end-to-end test).

    `allowed` is the set of keys the endpoint accepts at all; it defaults to `required`, which is right for the
    P04-P06 routes where every field is mandatory. A route with an optional field MUST pass the wider set, or
    its own optional field is a 422 — which is how P07's `code` and `scope` arrived looking like an attack.
    """
    if not isinstance(body, dict):
        return err("VALIDATION", rid, detail="body must be a JSON object")
    ok = tuple(required) if allowed is None else tuple(allowed)
    missing = ["missing: %s" % k for k in required if k not in body]
    unknown = ["unknown: %s" % k for k in body if k not in ok]
    if missing or unknown:
        return err("VALIDATION", rid, where=missing + unknown)
    return None


def _check_props(body: dict, props: dict, rid: str) -> JSONResponse | None:
    """The second half of the contract: `openapi_extra` promises minLength, a pattern and an enum, and a
    request that breaks them must not reach the crypto.

    This is not decoration. An empty password that reaches Argon2 costs 90 ms of CPU and burns one slot of the
    lockout budget, so a client bug ("we send an empty string while the field is still loading") becomes a
    denial of service the attacker does not have to pay for. Length and pattern are checked here, on the same
    table the OpenAPI document is generated from, so the two cannot drift.
    """
    bad = []
    for k, spec in props.items():
        if k not in body:
            continue
        v = body[k]
        if spec.get("type") == "string" and not isinstance(v, str):
            bad.append("%s must be a string" % k)
            continue
        if isinstance(v, str):
            if "minLength" in spec and len(v) < int(spec["minLength"]):
                bad.append("%s is too short" % k)
            if "maxLength" in spec and len(v) > int(spec["maxLength"]):
                bad.append("%s is too long" % k)
            if "pattern" in spec and not re.match(spec["pattern"], v):
                bad.append("%s has the wrong shape" % k)
        if "enum" in spec and v not in spec["enum"]:
            bad.append("%s must be one of %s" % (k, "/".join(map(str, spec["enum"]))))
        if spec.get("type") == "array" and isinstance(v, list):
            if "minItems" in spec and len(v) < int(spec["minItems"]):
                bad.append("%s needs at least %d entries" % (k, int(spec["minItems"])))
    if bad:
        return err("VALIDATION", rid, where=bad)
    return None


# --------------------------------------------------------------------------- P07: authn, sessions, 2FA
LOGIN_REQUIRED = ("identifier", "password")
LOGIN_PROPS = {"identifier": {"type": "string", "minLength": 3, "maxLength": 200},
               "password": {"type": "string", "minLength": 1, "maxLength": 1024}}
REFRESH_REQUIRED = ("refreshToken",)
REFRESH_PROPS = {"refreshToken": {"type": "string", "minLength": 20, "maxLength": 200}}
TELEGRAM_REQUIRED = ("initData",)
TELEGRAM_PROPS = {"initData": {"type": "string", "minLength": 20, "maxLength": 8000}}
TOTP_ENROLL_REQUIRED: tuple[str, ...] = ()
TOTP_ENROLL_PROPS: dict = {}
TOTP_VERIFY_REQUIRED = ("code",)
TOTP_VERIFY_PROPS = {"code": {"type": "string", "pattern": "^[0-9]{6}$"}}
ADDR_ADD_REQUIRED = ("address",)
ADDR_ADD_PROPS = {"address": {"type": "string", "minLength": 8, "maxLength": 128},
                  "label": {"type": "string", "maxLength": 80},
                  # Optional in the schema, mandatory in behaviour: `_totp_gate` refuses without it for
                  # `address_add`. The schema is permissive so the refusal is the *security* answer (403 with a
                  # reason), not a 422 about field shape.
                  "code": {"type": "string", "pattern": "^[0-9]{6}$"}}
ADDR_RM_REQUIRED = ("addressId",)
ADDR_RM_PROPS = {"addressId": {"type": "string", "minLength": 4, "maxLength": 64},
                 # Same shape as `address_add`: optional in the schema, mandatory in behaviour, so the refusal a
                 # caller gets is 403 with a reason rather than 422 about field shape. Removal is the step an
                 # attacker takes to erase the address they just added, so it is an address *change* in the sense
                 # the second factor exists for.
                 "code": {"type": "string", "pattern": "^[0-9]{6}$"}}
REVOKE_REQUIRED = ("reason", "approvers")
REVOKE_PROPS = {"reason": {"type": "string", "minLength": 4, "maxLength": 400},
                "approvers": {"type": "array", "minItems": 2, "items": {"type": "string", "minLength": 2}},
                "scope": {"type": "string", "enum": ["user", "all"]},
                "userId": {"type": "string", "maxLength": 64}}


@app.post("/v1/auth/login", status_code=200, responses=AUTH_RESPONSES,
           openapi_extra=_body_schema(LOGIN_REQUIRED, LOGIN_PROPS))
def auth_login(request: Request, body: dict = Body(...)):
    """Email-or-identifier + password. Returns a short access token and a single-use refresh token.

    The response carries the refresh token exactly once, and the row that backs it is a hash. A login that
    succeeds with parameters below the current floor re-hashes before it answers, so the upgrade happens while
    we hold the plaintext and never again.
    """
    rid = request.state.request_id
    bad = _check_body(body, LOGIN_REQUIRED, rid, allowed=tuple(LOGIN_PROPS))
    if bad is not None:
        return bad
    bad = _check_props(body, LOGIN_PROPS, rid)
    if bad is not None:
        return bad
    ident = str(body["identifier"]).strip().lower()
    pw = str(body["password"])
    # The account-side budget, from the event log we already write: 10 failures in 15 minutes stops the door,
    # and the window is fixed from the first failure so the answer is explainable to the person locked out.
    # Two budgets, both read out of the event log we already write, and *neither* of them global: a single
    # counter over all failures is a denial of service somebody hands to the attacker, who then locks every
    # account in the product by spraying ten bad passwords. The account bucket keys on the identifier (or on
    # the id we resolved), the address bucket is looser and only stops *password guessing*.
    win_from = _now_ms() - _pwd.LOCK["window_ms"]
    iph = _ip_hash(request)
    acct = SEC.one("SELECT COUNT(*) AS n, MIN(at_ms) AS first_ms FROM auth_events WHERE kind=? AND user_id=?"
                   " AND at_ms>=?", ("login_bad_password", ident, win_from)) or {}
    from_ip = SEC.one("SELECT COUNT(*) AS n, MIN(at_ms) AS first_ms FROM auth_events WHERE kind=? AND ip_hash=?"
                      " AND at_ms>=?", ("login_bad_password", iph, win_from)) or {}
    st = _pwd.lock_state(int(acct.get("n") or 0), int(acct.get("first_ms") or _now_ms()), _now_ms())
    st_ip = _pwd.lock_state(int(from_ip.get("n") or 0), int(from_ip.get("first_ms") or _now_ms()), _now_ms(),
                            limit=_pwd.LOCK["ip_max_failed"])
    if st["locked"] or st_ip["locked"]:
        which = "account" if st["locked"] else "address"
        wait = max(st["retry_after_ms"], st_ip["retry_after_ms"])
        SEC.auth_event(ident, "login_locked", at=_now_ms(), ip_hash=iph,
                       detail={"retry_after_ms": wait, "bucket": which})
        return err("ACCOUNT_LOCKED", rid, detail="retry in %ds" % (wait // 1000),
                   retry_after_s=max(1, wait // 1000))
    who = SEC.identity_user("email", ident) or SEC.identity_user("handle", ident) or (
        ident if SEC.one("SELECT id FROM users WHERE id=?", (ident,)) else None)
    row = {"user_id": who} if who else None
    cred = SEC.credential(str((row or {}).get("user_id") or ""))
    t0 = time.perf_counter()
    # A missing user and a wrong password run the *same* code path, and the same body: the difference between
    # "no such account" and "wrong password" is a user-enumeration oracle on a product where the identifier is
    # an email address.
    if not row or not cred:
        # Keyed on the identifier when no account matched: that is what makes "10 tries for THIS email"
        # countable, and the identifier never reaches a log line (see `redact.py`).
        SEC.auth_event(ident, "login_bad_password", at=_now_ms(), ip_hash=_ip_hash(request))
        return err("LOGIN_FAILED", rid)
    verdict = _hasher().verify(str(cred["phc"]), pw)
    if verdict.startswith("bad"):
        SEC.auth_event(str(row["user_id"]), "login_bad_password", at=_now_ms(), ip_hash=_ip_hash(request))
        return err("LOGIN_FAILED", rid)
    fam = "fam_" + uuid.uuid4().hex[:12]
    acc, ref = _token_string(), _token_string()
    SEC.mint_session(str(row["user_id"]), token_hash=_hash_token(acc), family_id=fam, at=_now_ms(),
                     ip_hash=_ip_hash(request), ua_hash=_ua_hash(request), cred_gen=int(cred["gen"]))
    SEC.mint_refresh(str(row["user_id"]), token_hash=_hash_token(ref), family_id=fam, at=_now_ms())
    if verdict == "ok_needs_rehash":
        SEC.set_password(str(row["user_id"]), _hasher().hash(pw), at=_now_ms())
    SEC.auth_event(str(row["user_id"]), "login_ok", at=_now_ms(), ip_hash=_ip_hash(request),
                   detail={"ms": round((time.perf_counter() - t0) * 1000, 1), "rehashed": verdict == "ok_needs_rehash"})
    return _stamped({"accessToken": acc, "refreshToken": ref, "tokenType": "Bearer",
                     "expiresInMs": ACCESS_TTL_MS, "user": {"id": str(row["user_id"])}},
                    ttl_ms=0, stale_ms=0)


@app.post("/v1/auth/refresh", status_code=200, responses=AUTH_RESPONSES,
           openapi_extra=_body_schema(REFRESH_REQUIRED, REFRESH_PROPS))
def auth_refresh(request: Request, body: dict = Body(...)):
    """Rotation, with the reuse alarm. A `REFRESH_REUSED` is a security event: it means a token that was
    already spent came back, which means somebody else has it."""
    rid = request.state.request_id
    bad = _check_body(body, REFRESH_REQUIRED, rid, allowed=tuple(REFRESH_PROPS))
    if bad is not None:
        return bad
    bad = _check_props(body, REFRESH_PROPS, rid)
    if bad is not None:
        return bad
    old = _hash_token(str(body["refreshToken"]))
    new = _token_string()
    res = SEC.rotate_refresh(old, new_hash=_hash_token(new), at=_now_ms())
    if not res.get("ok"):
        SEC.auth_event("", "refresh_%s" % str(res.get("code", "")).lower(), at=_now_ms())
        return err(str(res.get("code") or "REFRESH_UNKNOWN"), rid)
    acc = _token_string()
    SEC.mint_session(str(res["user_id"]), token_hash=_hash_token(acc), family_id=str(res["family_id"]),
                     at=_now_ms(), ip_hash=_ip_hash(request), ua_hash=_ua_hash(request),
                     cred_gen=int((SEC.credential(str(res["user_id"])) or {}).get("gen") or 1))
    return _stamped({"accessToken": acc, "refreshToken": new, "tokenType": "Bearer", "expiresInMs": ACCESS_TTL_MS,
                     "user": {"id": str(res["user_id"])}}, ttl_ms=0, stale_ms=0)


@app.post("/v1/auth/logout", status_code=200, responses=SESSIONS_RESPONSES)
def auth_logout(request: Request):
    uid, row, e = _principal(request)
    if e:
        return e
    if not row:
        return err("UNAUTHENTICATED", request.state.request_id, detail="the dev identity has no session to end")
    SEC.revoke_session(str(row["id"]), at=_now_ms(), reason="logged out", user_id=uid or "")
    return {"ended": True}


@app.get("/v1/auth/sessions", responses=SESSIONS_RESPONSES)
def auth_sessions(request: Request):
    """The per-device list with a revocation button behind it. No token material, ever — the rows are hashes
    and labels, and that is what makes this endpoint safe to render in a page."""
    uid, _row, e = _principal(request)
    if e:
        return e
    return _stamped({"items": SEC.sessions_for(str(uid), at=_now_ms()), "ttlMs": ACCESS_TTL_MS},
                    ttl_ms=5_000, stale_ms=60_000)


@app.post("/v1/auth/sessions/revoke", status_code=200, responses=SESSIONS_RESPONSES,
           openapi_extra=_body_schema(("sessionId",), {"sessionId": {"type": "string", "maxLength": 64},
                                                       "everywhere": {"type": "boolean"}}))
def auth_sessions_revoke(request: Request, body: dict = Body(...)):
    # `_principal` first, and for a reason worth stating: this route takes a session id and kills it. Without an
    # authenticated owner the (id, user_id) lookup below is a lookup against an empty string, which turns every
    # revocation into a 404 - safe by accident, and an accident is not a control.
    uid, _srow, e = _principal(request)
    if e:
        return e
    bad = _check_body({k: v for k, v in (body or {}).items() if k in ("sessionId", "everywhere")},
                      ("sessionId",), request.state.request_id, allowed=("sessionId", "everywhere"))
    if bad is not None:
        return bad
    sid = str(body.get("sessionId") or "")
    if str(body.get("everywhere") or "").lower() in ("1", "true", "yes"):
        # The current session dies with the rest, which is the point of the button; the caller has to sign in
        # again, and every device gets an explicit `session_revoked` event so support can say what happened.
        out = SEC.revoke_all_sessions(user_id=str(uid), at=_now_ms(),
                                      reason="user asked to be logged out everywhere")
        return _stamped(out, ttl_ms=0, stale_ms=0)
    # Object-level authorisation: a session id belongs to a user, and revoking somebody else's session is an
    # account lockout primitive. The row is looked up by (id, user) so a wrong id is a miss, not a 403.
    row = SEC.one("SELECT id, user_id FROM auth_sessions WHERE id=? AND user_id=?", (sid, uid_owner(request)))
    if not row:
        return err("NO_SUCH_RESOURCE", request.state.request_id)
    return _stamped(SEC.revoke_session(sid, at=_now_ms(), reason="revoked from the session list",
                                       user_id=uid_owner(request)), ttl_ms=0, stale_ms=0)


def uid_owner(request: Request) -> str:
    return str(getattr(request.state, "owner_uid", "") or getattr(request.state, "uid", "") or "")


@app.post("/v1/auth/telegram", status_code=200, responses=AUTH_RESPONSES,
           openapi_extra=_body_schema(TELEGRAM_REQUIRED, TELEGRAM_PROPS))
def auth_telegram(request: Request, body: dict = Body(...)):
    """Telegram Mini App sign-in: signature, freshness, then the replay store. In that order, all three.

    The replay table is what makes "valid forever" false. A payload copied off a phishing page still carries a
    correct signature — with the freshness window it is at most `LOGIN_MAX_AGE_S` useful, and with the nonce it
    is useful exactly once.
    """
    rid = request.state.request_id
    bad = _check_body(body, TELEGRAM_REQUIRED, rid, allowed=tuple(TELEGRAM_PROPS))
    if bad is not None:
        return bad
    bad = _check_props(body, TELEGRAM_PROPS, rid)
    if bad is not None:
        return bad
    token = (os.environ.get("PGM_TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return err("SECURITY_ENV_MISSING", rid, detail="PGM_TELEGRAM_BOT_TOKEN is not set on this pod")
    try:
        seen = SEC.telegram_seen_hashes(_tg.auth_hash(str(body["initData"])))
        res = _tg.verify(str(body["initData"]), token, at=_now_ms(), purpose="login", seen_hashes=seen)
    except _tg.InitDataError as exc:
        SEC.auth_event("", "telegram_malformed", at=_now_ms(), detail={"why": str(exc)[:120]})
        return err("TELEGRAM_INVALID", rid)
    if not res.ok:
        SEC.auth_event(res.tg_user_id, "telegram_%s" % res.reason, at=_now_ms(), ip_hash=_ip_hash(request),
                       detail={"age_s": res.age_s, "hash": res.auth_hash[:12]})
        return err("TELEGRAM_REPLAY" if res.reason == "replayed" else "TELEGRAM_INVALID", rid,
                   detail=res.reason)
    uid = SEC.identity_user("telegram", res.tg_user_id)
    if not uid:
        # Refused without saying whether the id exists, is unverified, or belongs to someone else. The
        # `link` flow (P09) is where a proof is checked; this route only ever consumes one.
        return err("TELEGRAM_INVALID", rid, detail="this Telegram account is not linked to an Openout user")
    acc, ref = _token_string(), _token_string()
    fam = "fam_" + uuid.uuid4().hex[:12]
    srow = SEC.mint_session(uid, token_hash=_hash_token(acc), family_id=fam, at=_now_ms(), kind="telegram",
                            ip_hash=_ip_hash(request), ua_hash=_ua_hash(request))
    SEC.mint_refresh(uid, token_hash=_hash_token(ref), family_id=fam, at=_now_ms())
    SEC.telegram_consume(res.auth_hash, uid, at=_now_ms(), session=srow["id"])
    SEC.auth_event(uid, "login_telegram", at=_now_ms(), ip_hash=_ip_hash(request),
                   detail={"age_s": res.age_s, "session": srow["id"]})
    return _stamped({"accessToken": acc, "refreshToken": ref, "tokenType": "Bearer",
                     "expiresInMs": ACCESS_TTL_MS, "user": {"id": uid}, "telegramAgeS": res.age_s},
                    ttl_ms=0, stale_ms=0)


@app.post("/v1/auth/totp/enroll", status_code=200, responses=AUTH_RESPONSES,
           openapi_extra=_body_schema(TOTP_ENROLL_REQUIRED, TOTP_ENROLL_PROPS))
def auth_totp_enroll(request: Request, body: dict = Body(default={})):
    """Generate a secret, wrap it, show the QR once, and keep nothing readable.

    Enrolment is deliberately *not* verified-and-armed in one step: `verified_ms` stays NULL until the user
    enters a code, so a support agent or a session hijacker cannot silently attach their own authenticator and
    call it done.
    """
    uid, _row, e = _principal(request)
    if e:
        return e
    if not _SECB_OK:
        return err("SECURITY_ENV_MISSING", request.state.request_id, detail=_SECB_ERR)
    if SEC.totp_enrolled(str(uid)) and not str(body.get("force") or ""):
        # Re-enrolment replaces a factor, which is exactly what an attacker with a session wants. It is allowed
        # — the user must be able to recover a lost phone — but only through an explicit call, and the event is
        # written to the account's own audit trail so the *first* thing they see on login is the replacement.
        SEC.auth_event(str(uid), "totp_reenrolled", at=_now_ms(),
                       detail={"note": "the previous authenticator stops working immediately"})
    try:
        secret = _totp.new_secret(os.urandom(_totp.SECRET_BYTES))
        env = _envelope()
        w = env.seal(secret.encode(), {"user_id": str(uid), "kek_version": env.version, "dek_version": 0,
                                       "policy_hash": "totp"})
    except _secb.BootError as exc:
        return err("SECURITY_ENV_MISSING", request.state.request_id, detail=str(exc)[:120])
    SEC.totp_enroll(str(uid), secret_wrapped=w["ciphertext"], nonce=w["nonce"], at=_now_ms(),
                    tag=w["tag"], kek_version=env.version)
    SEC.auth_event(str(uid), "totp_enrolled", at=_now_ms())
    return _stamped({"secret": secret, "uri": _totp.provisioning_uri(label=str(uid), secret_b32=secret),
                     "digits": _totp.DIGITS, "periodS": _totp.PERIOD_S, "verified": False,
                     "next": "enter a code from the app to finish; an unverified authenticator authorises nothing"},
                    ttl_ms=0, stale_ms=0)


@app.post("/v1/auth/totp/verify", status_code=200, responses=AUTH_RESPONSES,
           openapi_extra=_body_schema(TOTP_VERIFY_REQUIRED, TOTP_VERIFY_PROPS))
def auth_totp_verify(request: Request, body: dict = Body(...)):
    bad = _check_body(body, TOTP_VERIFY_REQUIRED, request.state.request_id, allowed=tuple(TOTP_VERIFY_PROPS))
    if bad is not None:
        return bad
    bad = _check_props(body, TOTP_VERIFY_PROPS, request.state.request_id)
    if bad is not None:
        return bad
    uid, _row, e = _principal(request)
    if e:
        return e
    st = SEC.totp_state(str(uid))
    if not st:
        return err("TOTP_REQUIRED", request.state.request_id, detail="no authenticator on this account")
    res = _totp.verify(_ENVELOPE_TOTP_SECRET(st), str(body["code"]), at=_now_ms(),
                       last_step=int(st.get("last_step") or -1), attempts=int(st.get("failed_count") or 0),
                       locked_until_ms=int(st.get("locked_until_ms") or 0))
    if not res.ok:
        SEC.totp_reject(str(uid), at=_now_ms())
        SEC.auth_event(str(uid), "totp_%s" % res.reason, at=_now_ms())
        return err("TOTP_LOCKED" if res.reason == "locked" else "TOTP_INVALID", request.state.request_id,
                   detail=res.reason if res.reason in ("reused", "expired", "locked") else None,
                   retry_after_s=max(1, int(res.retry_after_ms or 0) // 1000) if res.reason == "locked" else None)
    SEC.totp_accept(str(uid), step=res.step, at=_now_ms())
    return _stamped({"ok": True, "verified_ms": _now_ms()}, ttl_ms=0, stale_ms=0)


@app.get("/v1/wallet/withdrawal-addresses", responses=SESSIONS_RESPONSES)
def wallet_addresses(request: Request):
    uid, _row, e = _principal(request)
    if e:
        return e
    # The list is the screen an attacker sees over your shoulder and the one a compromised session reads first,
    # so it carries `0x1234…abcd` and a label, never the whole destination: a UI that has the full string in the
    # DOM is a UI that offers "copy", and clipboard substitution is on the threat list (D1). The complete address
    # is in the *add* response, where the user just typed it and can compare it once.
    items = [{k: v for k, v in row.items() if k != "address"} for row in SEC.addresses(str(uid), at=_now_ms())]
    return _stamped({"items": items, "cooldownMs": _authz.COOLDOWN_MS,
                     "reveal": "the full destination is shown when you add it; a list is not a reveal"},
                    ttl_ms=0, stale_ms=0)


# A `POST …/add` rather than a POST on the collection path: FastAPI gives one path one schema, and
# `tools/check-openapi.py` compares *path* to a single `responses=` table. Listing the same statuses twice, for
# two verbs that genuinely differ, would have meant a contract that lies about one of them.
@app.post("/v1/wallet/withdrawal-addresses/add", status_code=200, responses=ADDRESS_RESPONSES,
           openapi_extra=_body_schema(ADDR_ADD_REQUIRED, ADDR_ADD_PROPS))
def wallet_address_add(request: Request, body: dict = Body(...)):
    """Add a destination. It cannot be used for 24 hours, and the hold starts when it is confirmed.

    The 60-second confirmation step exists so the "are you sure?" page and the cooldown share one moment in
    time; an address that was typed and never confirmed is not a destination, it is a note.
    """
    bad = _check_body(body, ADDR_ADD_REQUIRED, request.state.request_id, allowed=tuple(ADDR_ADD_PROPS))
    if bad is not None:
        return bad
    bad = _check_props(body, ADDR_ADD_PROPS, request.state.request_id)
    if bad is not None:
        return bad
    uid, _row, e = _principal(request)
    if e:
        return e
    code = _totp_gate(request, str(uid), str(body.get("code") or ""), action="address_add")
    if code is not None:
        return code
    try:
        out = SEC.add_address(str(uid), str(body["address"]), at=_now_ms(), label=str(body.get("label") or ""))
    except ValueError as exc:
        return err("ADDRESS_LIMIT" if "ADDRESS_LIMIT" in str(exc) else "VALIDATION", request.state.request_id,
                   detail=str(exc)[:160])
    if out["shared_with"]:
        SEC.auth_event(str(uid), "address_shared", at=_now_ms(), detail={"others": out["shared_with"][:5]})
    return _stamped(out, ttl_ms=0, stale_ms=0)


@app.post("/v1/wallet/withdrawal-addresses/remove", status_code=200, responses=ADDRESS_RESPONSES,
           openapi_extra=_body_schema(ADDR_RM_REQUIRED, ADDR_RM_PROPS))
def wallet_address_remove(request: Request, body: dict = Body(...)):
    bad = _check_body(body, ADDR_RM_REQUIRED, request.state.request_id, allowed=tuple(ADDR_RM_PROPS))
    if bad is not None:
        return bad
    bad = _check_props(body, ADDR_RM_PROPS, request.state.request_id)
    if bad is not None:
        return bad
    uid, _row, e = _principal(request)
    if e:
        return e
    code = _totp_gate(request, str(uid), str(body.get("code") or ""), action="address_remove")
    if code is not None:
        return code
    res = SEC.remove_address(str(uid), str(body["addressId"]), at=_now_ms())
    if res.get("code") == "REMOVE_DURING_COOLDOWN":
        return err("REMOVE_DURING_COOLDOWN", request.state.request_id, detail=res["message"])
    if res.get("code") == "NOT_FOUND":
        return err("NO_SUCH_RESOURCE", request.state.request_id)
    return _stamped(res, ttl_ms=0, stale_ms=0)


@app.post("/v1/admin/revoke-sessions", status_code=200, responses=BREAK_GLASS_RESPONSES,
           openapi_extra=_body_schema(REVOKE_REQUIRED, REVOKE_PROPS))
def admin_revoke_sessions(request: Request, body: dict = Body(...)):
    """Break-glass. Two named approvers, a real reason, and every session in scope dies in one statement.

    The second approver is not bureaucracy: the single most dangerous account in this product is the one that
    can log everybody out, because that is what an attacker does to stop you seeing what they did.
    """
    rid = request.state.request_id
    ok, e = _admin(request)
    if e:
        return e
    bad = _check_body(body, REVOKE_REQUIRED, rid, allowed=tuple(REVOKE_PROPS))
    if bad is not None:
        return bad
    bad = _check_props(body, REVOKE_PROPS, rid)
    if bad is not None:
        return bad
    g = _keys.break_glass_ok(list(body.get("approvers") or []), reason=str(body.get("reason") or ""),
                             now_ms=_now_ms())
    if not g["ok"]:
        # The *refusal* is the event worth keeping. A successful break-glass is visible in every downstream
        # effect; an attempt that was denied by the two-approver rule is invisible unless we write it, and a
        # denied attempt by someone holding a valid admin token is the most informative row in this table.
        SEC.auth_event("", "break_glass_denied", at=_now_ms(),
                       detail={**g["denied"], "approvers": body.get("approvers"), "rid": rid})
        return err("BREAK_GLASS_DENIED", rid, detail="; ".join(g["denied"].values())[:200])
    scope = str(body.get("scope") or "all")
    target = str(body.get("userId") or "") if scope == "user" else None
    if scope == "user" and not target:
        return err("VALIDATION", rid, detail="scope=user needs userId")
    out = SEC.revoke_all_sessions(user_id=target or None, at=_now_ms(), reason=str(body["reason"])[:200])
    SEC.auth_event(target or "", "admin_revoke_all", at=_now_ms(),
                   detail={"approvers": body.get("approvers"), "scope": scope, **out})
    return _stamped({"revoked": out, "scope": scope, "atMs": _now_ms()}, ttl_ms=0, stale_ms=0)


@app.post("/v1/orders", status_code=202, responses=ORDER_RESPONSES,
           openapi_extra=_body_schema(ORDER_REQUIRED, ORDER_PROPS))
def place_order(request: Request, body: dict = Body(...),
                idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                x_user_id: str | None = Header(default=None, alias="X-User-Id")):
    rid = request.state.request_id
    if _bearer(request):
        # P07: a bearer session is the identity, and a header that disagrees with it is refused rather than
        # preferred. Accepting "whichever is present" is how a permissive dev path survives into production.
        uid2, _srow, e2 = _principal(request)
        if e2 is not None:
            return e2
        if x_user_id and str(x_user_id) != str(uid2):
            SEC.auth_event(str(uid2), "identity_mismatch", at=_now_ms(), detail={"header": str(x_user_id)[:40]})
            return err("SESSION_MISMATCH", rid)
        x_user_id = uid2
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
    if _bearer(request):
        uid2, _srow, e2 = _principal(request)
        if e2 is not None:
            return e2
        x_user_id = uid2
    # Object-level authorisation, in the WHERE clause rather than in a comparison after the read: a row that is
    # never fetched cannot leak through an exception, a debug page, or a future refactor that forgets the check.
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
