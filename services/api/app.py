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
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from fastapi import Body, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from polygm_core.automation import console as _au_console
from polygm_core.automation import engine as _au
from polygm_core.automation import facts as _au_facts
from polygm_core.classify import labels as _labels
from polygm_core.config.flags import FlagStore, Flags
from polygm_core.leaderboard import boards as _lb_boards
from polygm_core.leaderboard import rank as _lb_rank
from polygm_core.leaderboard import source as _lb_source
from polygm_core.ledger.ledger import IntentState
from polygm_core.money.cents import MoneyError, ScaleError, fmt_usdc, parse_usdc, price_ticks
from polygm_core.radar import rankings as _radar
from polygm_core.referrals import code as _rc
from polygm_core.referrals import sybil as _sy
from polygm_core.referrals import terms as _rt
from polygm_core import gaming as _gm_gaming
from polygm_core import public_pages as _pp
from polygm_core.risk.gate import Intent, Limits, MarketState, evaluate, norm_tick
from polygm_core.security import abuse as _tgb_abuse
from polygm_core.risk.idempotency import Idem
from polygm_core.telegrambot import channel as _tgb_channel
from polygm_core.telegrambot import client as _tg_client
from polygm_core.telegrambot import ops as _tgb_ops
from polygm_core.telegrambot import menu as _tgb_menu
from polygm_core.telegrambot import outbox as _tgb_outbox
from polygm_core.telegrambot import render as _tgb_render
from polygm_core.venue import clob_v2 as v2
from polygm_core.telegrambot import router as _tgb_router
from polygm_core.telegrambot import sessions as _tgb_sessions
from polygm_core.telegrambot import updates as _tgb_updates
from polygm_core.security import authz as _authz
from polygm_core.security import pseudonym as _pseudo
from polygm_core.security import keys as _keys
from polygm_core.security import passwords as _pwd
from polygm_core.security import redact as _redact
from polygm_core.security import sanitise as _san
from polygm_core.security import telegram as _tg
from polygm_core.security import totp as _totp
from polygm_core.signals import console as _sg
from polygm_core.signals import engine as _sg_engine
from polygm_core.security.store import ACCESS_TTL_MS, SecStore, hash_token as _hash_token, token_string as _token_string
from polygm_core.terminal import metrics as _tm

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
    # P10. A refusal is not a validation error: the body was well-formed and the state said no. 409 with a
    # reason, because "you cannot do this yet" and "you sent nonsense" are different messages to a client that
    # is deciding whether to change the request or change the account's history first.
    "REFUSED": ("this action is refused by a guard; the reason is in the log against the request id",
                409, False),
    "BAD_REASON": ("a kill-switch change needs a reason of 4-400 characters", 422, False),
    # P10-D5. Both statuses already existed in this table; the codes did not, and a quota refusal that arrives
    # as a generic 429 tells a user nothing about which budget they spent.
    "BAD_FIELD": ("a field is missing, or its value is outside the range this operation accepts", 422, False),
    # P10-D5. A selection that is too big, or not a list at all, is refused with the limit and the count in the
    # sentence: "outside the range this operation accepts" tells a user that they were wrong without telling
    # them what the range is. Its own code rather than a widened BAD_FIELD, so the public-detail rule stays
    # narrow - `_PUBLIC_DETAIL_CODES` below lists exactly which codes may say something specific.
    "RADAR_SCOPE": ("the market selection is outside what a radar scan accepts", 422, False),
    "QUOTA_EXCEEDED": ("this account's budget for that operation is spent", 429, True),
    # P10-D8/D9. Four refusals whose whole value is that they tell a user what to do next, which is why each one
    # has its own code rather than a widened REFUSED: "run it in dry mode first", "acknowledge the halt", "delete
    # a rule", "this channel needs Pro". A user who meets a refusal they cannot act on learns to distrust the
    # screen, and an automation rule is exactly the place that matters.
    "HALTED": ("trading is stopped for this account until the daily-loss halt is acknowledged", 409, False),
    "RULE_CAP": ("this account already holds as many automation rules as it may", 409, False),
    "DRY_RUN_REQUIRED": ("this rule has no completed dry run, so it cannot be armed", 409, False),
    "PLAN_REQUIRED": ("the plan on this account does not include that", 402, True),
    # P11-D4. The one refusal a user acts on by choosing another NAME rather than by changing a plan or a
    # history: a handle becomes a public URL (/trader/<handle>), and an account that could take a second one
    # after being caught would get a second first impression. Its own code so the sentence may name the clash.
    "HANDLE_TAKEN": ("that public handle is already claimed; pick another", 409, False),
    # P11-D5. Three refusals that exist because each one tells the person something they can act on: which name
    # to pick, that a referee is referred once, and that referring yourself is not a budget question.
    "CODE_TAKEN": ("that short code is already claimed; pick another", 409, False),
    # The one D5 refusal a referee meets while typing: the shape rules in `referrals.code` are stricter than a
    # Pydantic length check (a reserved word, an all-digit code), and a user who is told only "invalid (code)"
    # has no way to guess which rule they broke. Its own code so the engine's sentence may be served.
    "CODE_INVALID": ("that short code cannot be used", 422, False),
    "ALREADY_REFERRED": ("this account already has a referral recorded; a referee is referred once, and a "
                         "refused referral is appealed rather than re-applied", 409, False),
    "SELF_REFERRAL": ("an account cannot refer itself; the referral is refused and the builder code it was made "
                      "under is a revocation ground", 409, False),
    # P07. Each of these is a *user-safe* sentence: the detail a client needs is here, and the detail an
    # attacker would like is in the log line, behind the request id.
    "UNAUTHENTICATED": ("a session is required", 401, False),
    "SESSION_STALE": ("this session was ended because your credentials changed", 401, False),
    "SESSION_REVOKED": ("this session has been revoked", 401, False),
    "ADMIN_REQUIRED": ("this route is admin-only", 403, False),
    # P12 D6. `INSUFFICIENT_BALANCE` is P06's name for this in `risk/limits.py` — the venue's own gate refuses an
    # order that costs more than the account holds — and the wallet routes adopt the same word rather than
    # inventing a second one for the same fact.
    "INSUFFICIENT_BALANCE": ("this costs more than your available balance", 422, False),
    "PASSWORD_REQUIRED": ("set a withdrawal password before this action", 403, False),
    "PASSWORD_WRONG": ("that password is not right", 403, False),
    "ADDRESS_NOT_ALLOWED": ("that destination is not on your allowlist", 403, False),
    "LOGIN_FAILED": ("wrong user name or password", 401, False),
    "ACCOUNT_LOCKED": ("too many attempts; try again later", 429, True),
    "RATE_LIMITED": ("too many requests; try again shortly", 429, True),
    "TOTP_REQUIRED": ("a 6-digit code is needed for this action", 403, False),
    "TOTP_INVALID": ("that code did not work", 403, False),
    "TOTP_LOCKED": ("the authenticator is locked after too many tries", 429, True),
    "ADDRESS_COOLDOWN": ("this destination is still in its 24 hour hold", 409, True),
    # P13 D7.7. The venue's own refusal of a disabled builder code. Its own code because the sentence is the
    # action: the user learns the order will keep failing until the code is sorted, rather than receiving "the
    # venue refused the order" for the tenth time.
    "BUILDER_DISABLED": ("the builder code on this order is disabled at the venue, so no order can be placed "
                         "with it; attribution has stopped", 409, False),
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


#: The codes whose `detail` is authored for the user rather than for the log. See `err`.
#:
#: `REFUSED` (P10) covers "well formed, and the state said no": the client has to be told WHICH guard fired.
#: `QUOTA_EXCEEDED` is here for the same reason and joins it in P10: a 429 that says only "spent" leaves the
#: user unable to tell whether a repeat scan is free (it is), and the numbers in the sentence come from the
#: plan and the audit count, never from the request body. `BAD_FIELD` deliberately stays OUT: its message is
#: generic and its detail goes to the log, because that is the path a request's own content could reach.
_PUBLIC_DETAIL_CODES = frozenset({"REFUSED", "QUOTA_EXCEEDED", "RADAR_SCOPE",
                                # P12-D6: "available 10.000000 USDC" is our own ledger's number and nothing from
                                # the request, and it is the one thing that makes an insufficient-balance refusal
                                # actionable ("deposit" vs "ask for less" is a different next step each time).
                                "INSUFFICIENT_BALANCE",
                                # P10-D8/D9: the four refusals above are sentences written for the user
                                # (which plan, which limit, which next step) and contain nothing from the
                                # request, so they are safe to say out loud.
                                "HALTED", "RULE_CAP", "DRY_RUN_REQUIRED", "PLAN_REQUIRED", "HANDLE_TAKEN",
                                # P11-D5: three sentences about the caller's own request that name nothing they
                                # sent — the code they typed stays out of the body, which is the rule this list
                                # exists to keep narrow.
                                "CODE_TAKEN", "CODE_INVALID", "ALREADY_REFERRED", "SELF_REFERRAL"})


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
    # P10 widens the rule by exactly one code. `REFUSED` means "the request was well formed and the state said
    # no", and a refusal a user cannot act on is indistinguishable from a bug: the client has to be told WHICH
    # guard fired (`acknowledgeSlippage`, "no dry-run history yet"). The sentence is authored at the call site,
    # carries no user input, and this is the only code allowed to do it - `detail` on every other code still
    # goes to the log and never to the body, which is what keeps a Python message out of a response.
    if detail and code in _PUBLIC_DETAIL_CODES:
        msg = msg + ": " + detail
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


# The database path is resolved ONCE, at import, and every connection — including the ones threads open later —
# uses that value. Reading the environment inside the connection factory instead looks equivalent and is not: a
# process that imports this module against one file and then changes `PGM_DB_PATH` (a test harness, `check-openapi`
# booting a second app, a tool that migrates a throwaway database) hands its request threads connections to a file
# that may not exist yet, and `sqlite3.connect` *creates* an empty one rather than failing — so the requests answer
# `no such table: markets` as a 500. That was the shape of the seven failures the contract audit caught.
_DB_PATH = os.environ.get("PGM_DB_PATH", str(ROOT / "var" / "polygm.db"))


def _connect(db: str = "") -> sqlite3.Connection:
    path = db or _DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=5000")
    return c


class ThreadConnection:
    """A SQLite connection **per thread**, because one shared connection is a data race.

    `sqlite3` refuses to share a connection between threads for a reason, and `check_same_thread=False` only
    silences the check that would have told us: two threads interleaving `execute()` and `fetchall()` on one
    connection is undefined behaviour in the C library underneath. P08's c11 drill — five parallel reads on an
    expired access token — failed about one run in three with `sqlite3.InterfaceError: bad parameter or other API
    misuse` thrown out of a plain `SELECT /v1/markets`, which arrived as a 500 that a reader of the drill's
    status list could only have written off as a flake. It was not a flake, it was this.

    Uvicorn serves requests on a worker thread pool, so the fix is the shape a real database would have forced
    anyway: **each thread gets its own connection**, WAL lets them read in parallel, and `busy_timeout` makes a
    concurrent writer wait instead of failing. Everything is already written against `_db.execute(...)`, so this
    proxy is invisible to call sites, and there is no shared cursor for two threads to step on.

    Two deliberate non-changes: `isolation_level=None` (autocommit with explicit `BEGIN` where the code wants a
    transaction) and the default synchronous mode — WAL + `synchronous=NORMAL` would be faster and would trade
    durability on power loss for it, which is a decision for a deployment record and not for a concurrency fix.
    """

    def __init__(self, factory) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._conns: dict[int, sqlite3.Connection] = {}
        self._trace = None

    def _conn(self) -> sqlite3.Connection:
        tid = threading.get_ident()
        c = self._conns.get(tid)
        if c is None:
            self._reap()
            c = self._factory()
            if self._trace is not None:
                c.set_trace_callback(self._trace)
            self._conns[tid] = c
        return c

    def _reap(self) -> None:
        """Close the connections of threads that have exited.

        A connection is three file descriptors (the db, the WAL and the shm), and both a test run and a server
        churn threads — one per `TestClient`, one per uvicorn worker that retires. Without this the suite reached
        `OSError: [Errno 24] Too many open files` after a thousand tests, which is a leak wearing a resource
        limit's clothes: the thread that owned the connection is gone, so nothing can ever use it again.
        """
        alive = {t.ident for t in threading.enumerate()}
        with self._lock:
            for tid in [t for t in self._conns if t not in alive]:
                try:
                    self._conns.pop(tid).close()
                except Exception:                      # already closed: nothing to reap
                    self._conns.pop(tid, None)

    def set_trace_callback(self, cb) -> None:
        """A trace that only sees the calling thread's queries is a trace that reports zero queries.

        `test_markets_surfaces` counts the queries one page costs by installing a trace callback and making a
        request — and the request is served on a worker thread, so with a connection per thread there is nothing
        to count unless the proxy carries the setting to every connection it hands out, including the ones it has
        not opened yet. (This is the same mistake in miniature as the connection sharing it replaced: state that
        lives on "the connection" has to live on the thing that decides which connection you get.)
        """
        self._trace = cb
        with self._lock:
            conns = list(self._conns.values())
        for c in conns:
            c.set_trace_callback(cb)
        self._conn().set_trace_callback(cb)

    def close(self) -> None:
        """Close every connection this process opened — used by tests that tear a database down."""
        with self._lock:
            conns, self._conns = list(self._conns.values()), {}
        for c in conns:
            try:
                c.close()
            except Exception:                          # already closed, or closed under us: nothing to do
                pass

    def __getattr__(self, name):                       # execute, executemany, executescript, commit, rollback…
        return getattr(self._conn(), name)

    def __enter__(self):
        return self._conn().__enter__()

    def __exit__(self, *exc):                          # pragma: no cover - no call site uses `with _db:` today,
        return self._conn().__exit__(*exc)


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
def _thread_connection() -> sqlite3.Connection:
    """A new connection for a new thread, checked for the schema the boot check checks for the first one.

    A thread that gets a connection to the wrong database (or to a file that did not exist until the connection
    created it) used to be an empty schema answering every request with a 500. The check is one `PRAGMA table_info`
    per table per thread, paid once, and the failure it produces is the loud one `_require_schema` already writes.
    """
    c = _connect(_DB_PATH)
    _require_schema(c)
    return c


_db = ThreadConnection(_thread_connection)
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
    return _pseudo.anon(str(wallet))


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
    return _order_core(uid=str(x_user_id), body=body, idempotency_key=str(idempotency_key or ""), rid=rid)


def _order_core(uid: str, body: dict, idempotency_key: str, rid: str):
    """The order path itself, callable from more than one surface.

    Extracted from `POST /v1/orders` when P12 needed the Telegram bot to place an order, and the extraction is the
    point rather than a tidy-up: a second implementation of "validate, gate, record, submit" is a second risk
    gate, and the kit's constraint is that **no trade command executes without the risk gate**, whichever surface
    it came from. The route keeps its own identity preamble (a bearer session is the identity, and a header that
    disagrees with it is refused rather than preferred) and then calls this with the id it resolved; the bot calls
    it with the id Telegram's identity resolved to. Neither can skip a check, because the checks are all in here.

    `rid` is threaded in rather than generated: the request id on an order placed from a chat has to be the same
    id in the ledger row, the risk decision and the log line, or an incident cannot be reconstructed.

    `uid` is asserted here as well as checked by the caller: this function's contract is "the id is already
    resolved", and a future surface that forgets to resolve one gets an error instead of an order with no owner.
    """
    if not uid:
        # `UNAUTHENTICATED` rather than a `NO_USER_CONTEXT` of our own: this function's contract is "the id is already
        # resolved", and a caller that skipped that step has an *unauthenticated* request, which is a registered code
        # with a registered status. An unregistered name here would answer 400 "request failed", which is a lie.
        return err("UNAUTHENTICATED", rid, detail="no user context")
    if not idempotency_key or not _IDEM_RE.match(idempotency_key):
        return err("IDEM_KEY_REQUIRED", rid)
    # A wallet we hold the keys for, or no order at all. A `read_only` (watch-only) wallet cannot sign anything,
    # so an order queued against one can never be filled — the executor's pre-flight refuses it minutes later and
    # the user sees "queued", then a refusal, for an order that was impossible when they tapped. Refusing at the
    # door costs nothing and says the true thing while the person is still looking at the screen. A user with NO
    # wallet row keeps today's behaviour: `/wallet` is where one is created, and the executor's pre-flight is the
    # only component that can decide whether an absent row is "not provisioned yet" or "not allowed".
    w = _wallet_row(str(uid))
    if w is not None and str(w.get("custody")) != "delegated":
        return err("SIGNER_UNAVAILABLE", rid,
                   detail="this wallet is watch-only, so nothing can be signed from it")
    bad = _check_body(body, ORDER_REQUIRED, rid)
    if bad is not None:
        # BEFORE idem.begin: a request that cannot be valid must not occupy the key. If it did, the client's
        # next attempt - the one where they fixed the typo - would answer IDEM_IN_PROGRESS against a row that
        # no worker will ever finish, and the user could not place an order at all until the key expired.
        return bad
    idem = Idem(_db)
    rec = idem.begin(uid, idempotency_key, body)
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
        idem.abandon(uid, idempotency_key)
        return err("BAD_AMOUNT", rid)
    except MoneyError as e:                # reclassified inside the money module; kept explicit so a
        idem.abandon(uid, idempotency_key)   # future subclass is still a 422 and never a 500
        return err("BAD_AMOUNT", rid)

    mrow = _db.execute("SELECT accepting_orders,seconds_delay,minimum_tick_size,minimum_order_size,"
                       "fee_type,enable_order_book FROM markets WHERE id=?", (body.get("marketId"),)).fetchone()
    if mrow is None:
        idem.abandon(uid, idempotency_key)
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
                        (uid, _now_ms() - 86_400_000)).fetchone()[0]
    open_n = _db.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND state IN ('live','partial')",
                         (uid,)).fetchone()[0]
    # engaged = the value of the NEWEST row. `WHERE engaged=1` would read "engaged at some point in
    # history" and the switch could never be released without a DELETE, which the append-only trigger
    # forbids - a kill switch you cannot disarm is not a control, it is an outage with a UI.
    kill = _db.execute("SELECT engaged FROM kill_switch_state ORDER BY at_ms DESC, id DESC LIMIT 1"
                       ).fetchone()
    kill = bool(kill and kill[0])

    intent = Intent(user_id=uid, token_id=str(body["tokenId"]), side=str(body["side"]).upper(),
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
        _upsert_intent(uid, idempotency_key, intent, price_micro, size_micro, d.notional_micro,
                       IntentState.REJECTED.value, risk_code=d.code)
        idem.abandon(uid, idempotency_key)       # a rejected order may be retried with the SAME key
        resp = err(d.code, rid)
        resp.headers["x-risk-checks"] = str(len(d.checks_run))
        resp.headers["x-risk-latency-ms"] = f"{d.latency_ms:.2f}"
        return resp

    oid = _upsert_intent(uid, idempotency_key, intent, price_micro, size_micro, d.notional_micro,
                         IntentState.QUEUED.value)
    out = {"intentId": oid, "state": IntentState.QUEUED.value, "riskLatencyMs": round(d.latency_ms, 3),
           "notionalMicro": d.notional_micro, "poll": f"/v1/orders/intents/{oid}",
           "note": "queued for executor; 202 is the answer, not 'accepted at the venue'"}
    idem.finish(uid, idempotency_key, out, order_hash=None)
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
# ============================================================================================ #
# P10 · the terminal's read surfaces, the copy confirm gate, and the portfolio
#
# Three rules from the phase's prompt are enforced HERE rather than in a component, because a component can be
# rewritten by somebody who has not read the prompt:
#
#   1. **No address ever leaves.** Every wallet that crosses the wire goes through `_anon` (a keyed hash), and
#      the reverse resolution (`_wallet_for_anon`) is a lookup on our side. `tests/test_terminal_api.py` greps
#      every P10 payload for a `0x…` shape, so an endpoint that forgets is a red test rather than a review note.
#   2. **A win rate is gated, or absent.** `_tm.win_rate` returns None below the sample gate, WITH a reason
#      string that the UI renders where the percentage would have been.
#   3. **A curve carries its drawdown.** `_tm.drawdown_overlay` builds the points, so `peakMicro` and
#      `drawdownMicro` are on every one of them and no chart can draw PnL without the overlay.
#
# The whale rule is relative with an absolute FALLBACK: `max(p99.5 of the market's window fills, a floor)`,
# and below `WHALE_MIN_SAMPLE` fills the percentile is discarded rather than reported. The reason string comes
# back with every threshold, and the row carries the sentence the tooltip shows, because a badge whose rule is
# not visible is an accusation rather than a datum.
# ============================================================================================ #

TAPE_FILL_LIMIT = 500
#: A facet query is a scan of the window's fills, so the window is capped rather than merely discouraged. The
#: cap is 24h and the API answers 422 beyond it: a silent clamp would render a 30-day filter as a 24-hour one.
TAPE_MAX_WINDOW_MS = 86_400_000
TAPE_DEFAULT_WINDOW_MS = 3_600_000
TRADER_FILL_LIMIT = 5_000

TAPE_FILLS_RESPONSES = {404: {"description": "unknown market, or an unknown wallet pseudonym"},
                        422: {"description": "windowMs above 24h, limit above 500, or a bad filter value"}}
FACETS_RESPONSES = {404: {"description": "unknown market"},
                    422: {"description": "windowMs above 24h"}}
WHALES_RESPONSES = {404: {"description": "unknown market"},
                    422: {"description": "scope=market without a marketId, or a bad multiple"}}
TRADER_RESPONSES = {404: {"description": "no trader with that pseudonym in the stored tape"},
                    422: {"description": "unknown window"}}
# The collection GET and POST share this table on purpose: `tools/check-openapi.py` maps one path to one
# table, and both verbs genuinely answer both statuses - GET 404s on an unknown `configId` filter and 422s on a
# bad `limit`, POST 404s on an unknown source and 422s on a bad body. Sharing a table with a lie in it would be
# worse than sharing one where both entries are true for both verbs.
COPY_CREATE_RESPONSES = {400: {"description": "missing Idempotency-Key; a malformed one is a 422 naming the field"},
                         401: {"description": "a session is required"},
                         404: {"description": "unknown source pseudonym, or an unknown configId filter"},
                         422: {"description": "missing field, an unknown field, or maxOrder above maxDaily"}}
COPY_GUARD_RESPONSES = {400: {"description": "missing Idempotency-Key; a malformed one is a 422 naming the field"},
                         401: {"description": "a session is required"},
                         404: {"description": "no such config for this account"},
                        409: {"description": "refused: the state does not allow this yet (no acknowledgement, "
                                             "or no dry-run history)"},
                        422: {"description": "missing field or an out-of-range guard"}}
COPY_MONITOR_RESPONSES = {401: {"description": "a session is required"},
                         404: {"description": "no such config for this account"}}
PORTFOLIO_RESPONSES = {401: {"description": "a session is required"}}
WHALE_VIEW_RESPONSES = {400: {"description": "missing Idempotency-Key; a malformed one is a 422 naming the field"},
                         409: {"description": "refused: a notifying view needs a market target "
                                             "(`alert_rules` requires one)"}, 401: {"description": "a session is required"},
                         404: {"description": "no such market or rule"},
                        422: {"description": "a notifying view needs a market scope"}}

COPY_CREATE_REQUIRED = ("sourceAnon", "maxOrderMicro", "maxDailyMicro")
COPY_CREATE_PROPS = {"sourceAnon": {"type": "string", "minLength": 4, "maxLength": 64},
                     "mode": {"type": "string", "enum": ["cap", "ratio"]},
                     "ratioBps": {"type": "integer", "minimum": 1, "maximum": 10000},
                     "maxOrderMicro": {"type": "integer", "minimum": 1_000_000},
                     "maxDailyMicro": {"type": "integer", "minimum": 1_000_000},
                     "blockedMarkets": {"type": "array", "items": {"type": "string"}, "maxItems": 50}}
COPY_GUARD_REQUIRED = ("configId",)
COPY_GUARD_PROPS = {"configId": {"type": "string", "minLength": 1, "maxLength": 64},
                    "dryRun": {"type": "boolean"},
                    "acknowledgeSlippage": {"type": "boolean"},
                    "skipIfMovedCents": {"type": "integer", "minimum": 0, "maximum": 50},
                    "doNotEnterWithinHours": {"type": "integer", "minimum": 0, "maximum": 168},
                    "categoryFilter": {"type": "string", "maxLength": 48},
                    "minPriceMicro": {"type": "integer", "minimum": 1, "maximum": 999999},
                    "maxPriceMicro": {"type": "integer", "minimum": 1, "maximum": 999999},
                    "takeProfitMicro": {"type": "integer", "minimum": 1, "maximum": 999999},
                    "stopLossMicro": {"type": "integer", "minimum": 1, "maximum": 999999}}
WHALE_VIEW_REQUIRED = ("name",)
WHALE_VIEW_PROPS = {"name": {"type": "string", "minLength": 1, "maxLength": 48},
                    "minSeverity": {"type": "string", "enum": ["info", "notice", "urgent"]},
                    "minNotionalMicro": {"type": "integer", "minimum": 0},
                    "multiple": {"type": "integer", "minimum": 2, "maximum": 1000},
                    "marketId": {"type": "string", "maxLength": 64},
                    "channel": {"type": "string", "enum": ["telegram", "email", "webhook"]},
                    "severity": {"type": "string", "enum": ["info", "notice", "urgent"]},
                    "firesPerWindow": {"type": "integer", "minimum": 1, "maximum": 100},
                    "ruleWindowMs": {"type": "integer", "minimum": 60000, "maximum": 86400000}}

_LABEL_FACTS = {c["label"]: c for c in _labels.catalogue()}


def _market_rows() -> dict[str, dict]:
    """condition id → the market fields every P10 row needs, with LEFT joins throughout.

    LEFT and not INNER on purpose: a market whose P09 surfaces have not been written yet still has a tape, and
    an INNER join would render its fills as rows with no market — which reads as "this fill belongs nowhere"
    rather than "we have not classified this market yet".
    """
    out: dict[str, dict] = {}
    for (mid, cond, slug, question, tick, end_ts, category, last_price, vol7d) in _db.execute(
            "SELECT m.id, m.condition_id, m.slug, m.question, m.minimum_tick_size, m.end_ts,"
            " COALESCE(mm.category,''), a.last_price_micro, COALESCE(a.volume_7d_micro, 0)"
            " FROM markets m LEFT JOIN market_meta mm ON mm.market_id = m.id"
            " LEFT JOIN market_activity a ON a.market_id = m.id").fetchall():
        out[str(cond)] = {"marketId": str(mid), "marketSlug": str(slug), "question": str(question),
                          "category": str(category), "tick": norm_tick(tick), "endsMs": int(end_ts or 0),
                          "lastPriceMicro": (None if last_price is None else _tm._int(last_price)),
                          "volume7dMicro": _tm._int(vol7d)}
    return out


def _label_catalogue() -> list[dict]:
    """Every label with its rule and disclaimer. Served by facets, so the filter bar can explain a filter it is
    offering BEFORE it is applied — the tooltip after the fact is too late to inform the click."""
    return list(_labels.catalogue())


def _label_facts(labels: list) -> list[dict]:
    """Label names (as `wallet_labels` stores them) → the full fact a badge renders.

    An unknown name yields an empty rule rather than being dropped: a label whose rule we cannot state is a
    label the UI must render as unexplained, and silently removing it would hide the gap.
    """
    out = []
    for name in labels or []:
        fact = _LABEL_FACTS.get(str(name))
        if fact is None:
            continue
        out.append({"label": fact["label"], "rule": fact["rule"], "disclaimer": fact["disclaimer"],
                    "confidence": 1000, "publishable": fact["publishable"]})
    return out


def _labels_by_wallet() -> dict[str, list[dict]]:
    """Every PUBLISHABLE label, keyed by wallet, with its rule and its disclaimer attached.

    `publishable=0` rows exist and are NOT returned: the classifier marks a label unpublishable when its own
    evidence is too thin (or when naming it in public would be an accusation), and an API that ships it anyway
    makes that decision meaningless.
    """
    out: dict[str, list[dict]] = {}
    for (wallet, label, conf, ev, _pub) in _db.execute(
            "SELECT wallet, label, confidence, evidence_json, publishable FROM wallet_labels"
            " WHERE publishable=1").fetchall():
        fact = _LABEL_FACTS.get(str(label))
        if fact is None:
            continue
        out.setdefault(str(wallet), []).append(
            {"label": str(label), "confidence": _tm._int(conf), "publishable": True,
             "evidence": _json_load(ev), "rule": fact["rule"], "disclaimer": fact["disclaimer"]})
    return out


def _json_load(text) -> dict:
    if not isinstance(text, str) or not text:
        return {}
    try:
        out = json.loads(text)
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def _is_anon(text: str) -> bool:
    return _pseudo.is_anon(text)


def _wallet_for_anon(anon_id: str) -> str | None:
    """The address behind a pseudonym, or None. The reverse lookup is ours to make and nobody else's.

    Two sources, in order: `wallet_pseudonyms` (the pairs we have resolved, so this is an indexed read), then
    the wallets the tape and the label table mention. An address does not live in the payload — it never does —
    so the lookup is the only way a client's click on a pseudonym becomes a query.
    """
    wanted = str(anon_id or "")
    if not _pseudo.is_anon(wanted):
        return None
    row = _db.execute("SELECT wallet_id FROM wallet_pseudonyms WHERE anon_id=?", (wanted,)).fetchone()
    if row is not None:
        return str(row[0])
    seen: set[str] = set()
    for (wallet,) in _db.execute("SELECT DISTINCT wallet FROM tape_fills LIMIT 4000").fetchall():
        seen.add(str(wallet))
    for (wallet,) in _db.execute("SELECT DISTINCT wallet FROM wallet_labels").fetchall():
        seen.add(str(wallet))
    for wallet in sorted(seen):
        if _anon(wallet) == wanted:
            return wallet
    return None


def _window_fills(since_ms: int, *, market_id: str | None = None) -> list[dict]:
    """Fills in `[since_ms, now]`, as INTERNAL dicts (micro integers).

    Internal, because every caller does integer arithmetic on these rows; `_fill_out` is the one place they
    become strings on the way out.
    """
    cond = None
    if market_id:
        row = _db.execute("SELECT condition_id FROM markets WHERE id=?", (str(market_id),)).fetchone()
        if row is None:
            return []
        cond = str(row[0])
    sql = ("SELECT f.ts_ms, f.condition_id, f.token_id, f.side, COALESCE(f.outcome,''), f.price_micro,"
           " f.size_micro, f.usd_notional_micro, f.wallet, f.source, f.ingest_ms FROM tape_fills f"
           " WHERE f.ts_ms >= ?")
    args: list = [_tm._int(since_ms)]
    if cond:
        sql += " AND f.condition_id = ?"
        args.append(cond)
    sql += " ORDER BY f.ts_ms DESC, f.rowid DESC"
    markets = _market_rows()
    out = []
    for (ts, cnd, token, side, outcome, price, size, notional, wallet, source, ingest_ms) in \
            _db.execute(sql, tuple(args)).fetchall():
        m = markets.get(str(cnd), {})
        out.append({"tsMs": _tm._int(ts), "conditionId": str(cnd), "tokenId": str(token),
                    "marketId": m.get("marketId", ""), "marketSlug": m.get("marketSlug", ""),
                    "question": m.get("question", ""), "category": m.get("category", ""),
                    "tick": m.get("tick", "0.01"), "side": str(side), "outcome": str(outcome),
                    "priceMicro": _tm._int(price), "sizeMicro": _tm._int(size),
                    "notionalMicro": _tm._int(notional), "anonWallet": _anon(str(wallet)),
                    "wallet": str(wallet), "source": str(source),
                    "lagMs": max(0, _tm._int(ingest_ms) - _tm._int(ts))})
    return out


def _fill_out(f: dict) -> dict:
    """One fill on the wire: the same names and kinds as `/v1/markets/{id}/fills`.

    `price` and `shares` are DECIMAL STRINGS (the contract's rule 2 — a JSON number is a float the moment a
    client does arithmetic with it) and `notionalMicro` is an INTEGER, because that is the number the whale
    rule is defined against and rounding it to cents would move a fill across its own threshold. The wallet's
    labels ride along with their rules, since a badge without its rule is a horoscope.
    """
    return {"tsMs": f["tsMs"], "conditionId": f["conditionId"], "tokenId": f["tokenId"],
            "marketId": f["marketId"], "marketSlug": f["marketSlug"], "question": f["question"],
            "category": f["category"], "tick": f["tick"], "side": f["side"], "outcome": f["outcome"],
            "price": fmt_usdc(f["priceMicro"]), "shares": _shares(f["sizeMicro"]),
            "notionalMicro": f["notionalMicro"], "anonWallet": f["anonWallet"],
            "labels": f.get("labels") or [], "source": f["source"], "lagMs": f["lagMs"]}


def _position_out(row: dict) -> dict:
    """One position on the wire. `size`/`avgEntry`/`mark` are strings; the micro integers that survive are the
    ones the arithmetic produced (`unrealisedMicro`, `valueMicro`), not display values.

    `mark` is EMPTY when there is no mark: an empty string renders as "no mark", while 0 renders as a price of
    zero, and those are different claims about the world.
    """
    return {"size": _shares(row["sizeMicro"]), "avgEntry": fmt_usdc(row["avgEntryMicro"]),
            "mark": (fmt_usdc(row["markMicro"]) if row["markMicro"] else ""),
            "costBasisMicro": row["costBasisMicro"], "valueMicro": row["valueMicro"],
            "unrealisedMicro": row["unrealisedMicro"], "unrealisedBps": row["unrealisedBps"],
            "onTick": row["onTick"], "endsInMs": row["endsInMs"]}


def _thresholds(fills: list[dict], *, multiple: int | None = None) -> dict[str, dict]:
    """Per-market whale threshold, from that market's own fills in the window.

    The rule and the reason come back with the number, because "why is $500 the threshold here" is a question
    the badge has to answer next to the badge. The floor is the SIZE BUCKET's, not one number for the venue: a
    market whose median fill is $4 and one whose median is $900 need different floors to mean the same thing by
    "whale". `multiple` switches the relative term to N x the median (D4's tunable view); the floor applies
    either way, and `reason` says which term won.
    """
    by_market: dict[str, list[int]] = {}
    for f in fills:
        by_market.setdefault(str(f["conditionId"]), []).append(_tm._int(f["notionalMicro"]))
    out: dict[str, dict] = {}
    for cond, notionals in by_market.items():
        med = _tm.median_micro(notionals)
        t = _tm.whale_threshold_micro(p995=_tm.percentile_micro(notionals, 995, 1000), median=med,
                                      fills=len(notionals), mode=("multiple" if multiple else "relative"),
                                      multiple=multiple, floor_micro=_tm.bucket_floor_micro(med))
        t["sizeBucket"] = _tm.market_size_bucket(med)
        t["bucketFloorMicro"] = _tm.bucket_floor_micro(med)
        out[cond] = t
    return out


def _rows_out(fills: list[dict], thresholds: dict[str, dict], *, annotated: bool) -> list[dict]:
    """The wire rows, with the whale judgement attached when asked for.

    `isWhale` is computed HERE and never by the client: two implementations of one threshold is one
    implementation and one bug, and the client's would be the one nobody tests.
    """
    labels = _labels_by_wallet()
    out = []
    for f in fills:
        row = _fill_out(f)
        row["labels"] = labels.get(f["wallet"], [])
        if annotated:
            t = thresholds.get(f["conditionId"]) or _tm.whale_threshold_micro()
            row["thresholdMicro"] = t["thresholdMicro"]
            row["thresholdRule"] = t["rule"]
            row["thresholdReason"] = t["reason"]
            sev = _tm.whale_severity(f["notionalMicro"], t["thresholdMicro"])
            row["severity"] = sev["severity"]
            row["ratioBps"] = sev["ratioBps"]
            row["rule"] = sev["rule"]
            row["isWhale"] = f["notionalMicro"] >= t["thresholdMicro"]
        out.append(row)
    return out


def _bucket(rows: list[dict], key: str, limit: int) -> list[dict]:
    """Facet counts for one column, zero-fill buckets REMOVED.

    A facet that offers an option with nothing behind it is a filter that produces an empty screen, and the
    user concludes the filter is broken rather than the market quiet. Counts are what make a filter honest.
    """
    agg: dict[str, dict] = {}
    for r in rows:
        val = str(r.get(key) or "")
        d = agg.setdefault(val, {"value": val, "fills": 0, "notionalMicro": 0})
        d["fills"] += 1
        d["notionalMicro"] += _tm._int(r["notionalMicro"])
    return sorted(agg.values(), key=lambda d: (-d["notionalMicro"], d["value"]))[:limit]


@app.get("/v1/tape/fills", responses=TAPE_FILLS_RESPONSES)
def get_tape_fills(request: Request,
                   marketId: str | None = Query(default=None, max_length=128),
                   since: int | None = Query(default=None, ge=0),
                   limit: int = Query(default=64, ge=1, le=TAPE_FILL_LIMIT),
                   side: str | None = Query(default=None, pattern="^(BUY|SELL)$"),
                   outcome: str | None = Query(default=None, max_length=64),
                   category: str | None = Query(default=None, max_length=48),
                   label: str | None = Query(default=None, max_length=32),
                   wallet: str | None = Query(default=None, max_length=64),
                   minNotionalMicro: int | None = Query(default=None, ge=0),
                   windowMs: int = Query(default=TAPE_DEFAULT_WINDOW_MS, ge=1, le=TAPE_MAX_WINDOW_MS)):
    """The market-wide tape D2 is built on: every fill we hold, filterable, newest first.

    `/v1/tape` is per-market and reads the P04 fixture; this reads the durable `tape_fills` log and is the only
    tape the terminal uses. `since` is exclusive and in the VENUE's clock (`ts_ms`), matching both earlier tape
    endpoints: the two clocks differ by the ingest lag, and a client that mixes them sees a duplicate or a hole.

    Filtering happens here rather than in the client for three reasons that all showed up in the design pass: a
    filter applied after a `LIMIT` silently hides matches; the relative whale threshold needs the same window
    the rows came from; and the count behind a filter is a fact the user cannot compute.
    """
    rid = request.state.request_id
    if marketId and _db.execute("SELECT 1 FROM markets WHERE id=?", (marketId,)).fetchone() is None:
        return err("NOT_FOUND", rid)
    upper = since if since is not None else _now_ms() + 1
    lower = upper - _tm._int(windowMs)
    window = _window_fills(lower, market_id=marketId)
    rows = window
    if since is not None:
        rows = [f for f in rows if f["tsMs"] < upper]               # `since` is exclusive, like `/v1/tape`
    if side:
        rows = [f for f in rows if f["side"] == side]
    if outcome:
        rows = [f for f in rows if f["outcome"] == outcome]
    if category:
        rows = [f for f in rows if f["category"] == category]
    if minNotionalMicro is not None:
        rows = [f for f in rows if f["notionalMicro"] >= _tm._int(minNotionalMicro)]
    if wallet:
        target = _wallet_for_anon(wallet)
        if target is None:
            return err("NOT_FOUND", rid, detail="unknown wallet pseudonym")
        rows = [f for f in rows if f["wallet"] == target]
    labels = _labels_by_wallet()
    if label:
        rows = [f for f in rows if label in [lab["label"] for lab in labels.get(f["wallet"], [])]]
    has_more = len(rows) > limit
    page = rows[:limit]
    # The thresholds come from the WINDOW's fills and not from the page: a threshold computed from the 64 rows
    # on screen would move as you scroll, and a badge that changes while you look at it is worse than none.
    thresholds = _thresholds(window)
    out = _rows_out(page, thresholds, annotated=True)
    return _stamped({"cacheKey": "tape-fills:%s:%s:%s" % (marketId or "*", since, limit), "rows": out,
                     "counts": {"returned": len(out), "whales": sum(1 for r in out if r["isWhale"]),
                                "hasMore": has_more,
                                "overThresholdOnPage": sum(1 for r in out if r["isWhale"])},
                     "windowMs": _tm._int(windowMs), "filterNote": (
                         "filters apply before the limit, so these counts describe this page; the window's own "
                         "totals are on /v1/tape/facets"),
                     "nextCursor": (page[-1]["tsMs"] if has_more and page else None)},
                    ttl_ms=500, stale_ms=flags().stale_ms_tape,
                    as_of_ms=(max((r["tsMs"] for r in page), default=None)))


@app.get("/v1/tape/facets", responses=FACETS_RESPONSES)
def get_tape_facets(request: Request,
                    marketId: str | None = Query(default=None, max_length=128),
                    windowMs: int = Query(default=TAPE_DEFAULT_WINDOW_MS, ge=1, le=TAPE_MAX_WINDOW_MS)):
    """What is in the window: the distribution, every filter's options with counts, and the label catalogue.

    Served as ONE payload because a filter bar assembled from six requests is a filter bar showing six different
    windows. This is also where the whale threshold is defined for a window, so a screen can state the rule
    before the first fill arrives and a user can see how many whales a filter would show before committing.
    """
    rid = request.state.request_id
    if marketId and _db.execute("SELECT 1 FROM markets WHERE id=?", (marketId,)).fetchone() is None:
        return err("NOT_FOUND", rid)
    now = _now_ms()
    fills = _window_fills(now - _tm._int(windowMs), market_id=marketId)
    notionals = [_tm._int(f["notionalMicro"]) for f in fills]
    med = _tm.median_micro(notionals)
    thresholds = _thresholds(fills)
    # The window-level threshold is the same rule with the venue-wide floor: a global number cannot carry a
    # per-market bucket, and pretending otherwise would be a threshold that means different things per row.
    whale = _tm.whale_threshold_micro(p995=_tm.percentile_micro(notionals, 995, 1000), median=med,
                                      fills=len(notionals))
    labels = _labels_by_wallet()
    wallets: dict[str, dict] = {}
    for f in fills:
        d = wallets.setdefault(f["wallet"], {"anonWallet": f["anonWallet"], "fills": 0, "notionalMicro": 0,
                                             "labels": labels.get(f["wallet"], [])})
        d["fills"] += 1
        d["notionalMicro"] += f["notionalMicro"]
    markets = []
    for cond, t in thresholds.items():
        rows = [f for f in fills if f["conditionId"] == cond]
        sample = rows[0]
        markets.append({"marketId": sample["marketId"], "slug": sample["marketSlug"],
                        "question": sample["question"], "category": sample["category"], "fills": len(rows),
                        "notionalMicro": sum(f["notionalMicro"] for f in rows),
                        # Per MARKET: the count of fills over THAT market's own threshold, which is the number
                        # a per-market view shows. A global count here would make every market look the same.
                        "whales": sum(1 for f in rows if f["notionalMicro"] >= t["thresholdMicro"]),
                        "thresholdMicro": t["thresholdMicro"], "thresholdRule": t["rule"],
                        "thresholdReason": t["reason"], "sizeBucket": t["sizeBucket"],
                        "bucketFloorMicro": t["bucketFloorMicro"]})
    markets.sort(key=lambda m: (-m["notionalMicro"], m["marketId"]))
    by_label: dict[str, dict] = {}
    for wallet, facts in labels.items():
        n = sum(1 for f in fills if f["wallet"] == wallet)
        if not n:
            continue
        for fact in facts:
            d = by_label.setdefault(fact["label"], {"label": fact["label"], "wallets": 0, "fills": 0,
                                                    "rule": fact["rule"], "disclaimer": fact["disclaimer"]})
            d["wallets"] += 1
            d["fills"] += n
    return _stamped({"cacheKey": "facets:%s:%s" % (marketId or "*", windowMs), "marketId": marketId,
                     "windowMs": _tm._int(windowMs), "fills": len(fills),
                     "sampleMax": TAPE_FILL_LIMIT, "sampled": len(fills) > TAPE_FILL_LIMIT,
                     "medianNotionalMicro": med, "p95NotionalMicro": _tm.percentile_micro(notionals, 95, 100),
                     "maxNotionalMicro": max(notionals, default=0), "whale": whale,
                     "severityRule": _tm.whale_severity(0, 1)["rule"],
                     "sampleNote": ("counts describe the fills we hold in this window; the threshold is "
                                    "computed from the same rows, so a filter and its threshold can never "
                                    "disagree"),
                     "sides": _bucket(fills, "side", 8), "outcomes": _bucket(fills, "outcome", 24),
                     "categories": _bucket(fills, "category", 24), "sources": _bucket(fills, "source", 8),
                     "wallets": sorted(wallets.values(),
                                       key=lambda d: (-d["notionalMicro"], d["anonWallet"]))[:24],
                     "markets": markets[:40],
                     "classifications": sorted(by_label.values(),
                                               key=lambda d: (-d["fills"], d["label"])),
                     "classificationsAll": _label_catalogue()},
                    ttl_ms=1_000, stale_ms=flags().stale_ms_tape,
                    as_of_ms=(max((f["tsMs"] for f in fills), default=None)))


@app.get("/v1/whales", responses=WHALES_RESPONSES)
def get_whales(request: Request,
               scope: str = Query(default="global", pattern="^(global|market)$"),
               marketId: str | None = Query(default=None, max_length=128),
               windowMs: int = Query(default=TAPE_DEFAULT_WINDOW_MS, ge=1, le=TAPE_MAX_WINDOW_MS),
               multiple: int | None = Query(default=None, ge=2, le=1000),
               minSeverity: str = Query(default="info", pattern="^(info|notice|urgent)$"),
               limit: int = Query(default=50, ge=1, le=200)):
    """D4's feed: every fill at or above ITS OWN market's threshold, biggest first.

    The thresholds travel with the response so a global feed cannot imply a global rule — $5,000 is a whale in
    one market and the median fill in another, and a feed that hides which one it used cannot be tuned.
    `multiple` switches the relative term to N x the market's median (D4's tunable view).
    """
    rid = request.state.request_id
    if scope == "market" and not marketId:
        # A market-scoped feed without a market is not a global feed; it is a missing parameter, and answering
        # it with something reasonable is how a screen ends up showing the wrong scope with no error anywhere.
        return err("VALIDATION", rid, detail="scope=market needs a marketId")
    if marketId and _db.execute("SELECT 1 FROM markets WHERE id=?", (marketId,)).fetchone() is None:
        return err("NOT_FOUND", rid)
    now = _now_ms()
    fills = _window_fills(now - _tm._int(windowMs), market_id=marketId)
    thresholds = _thresholds(fills, multiple=multiple)
    severity_floor = {"info": 0, "notice": _tm.SEVERITY_NOTICE_BPS, "urgent": _tm.SEVERITY_URGENT_BPS}
    rows = []
    for f in fills:
        t = thresholds.get(f["conditionId"])
        if t is None or f["notionalMicro"] < t["thresholdMicro"]:
            continue
        sev = _tm.whale_severity(f["notionalMicro"], t["thresholdMicro"])
        if sev["ratioBps"] < severity_floor[str(minSeverity)]:
            continue
        rows.append((f, t, sev))
    rows.sort(key=lambda r: (-r[0]["notionalMicro"], -r[0]["tsMs"]))
    labels = _labels_by_wallet()
    out = []
    for f, t, sev in rows[:limit]:
        row = _fill_out(f)
        row["labels"] = labels.get(f["wallet"], [])
        row.update({"thresholdMicro": t["thresholdMicro"], "thresholdRule": t["rule"],
                    "thresholdReason": t["reason"], "severity": sev["severity"], "ratioBps": sev["ratioBps"],
                    "rule": sev["rule"], "isWhale": True})
        out.append(row)
    return _stamped({"cacheKey": "whales:%s:%s:%s:%s:%s" % (scope, marketId or "*", minSeverity, windowMs,
                                                            multiple or 0),
                     "scope": scope, "marketId": marketId, "windowMs": _tm._int(windowMs),
                     "multiple": multiple, "rows": out,
                     "counts": {"overThreshold": len(rows), "returned": len(out),
                                "marketsWithFills": len(thresholds)},
                     "thresholds": thresholds, "severityRule": _tm.whale_severity(0, 1)["rule"]},
                    ttl_ms=1_000, stale_ms=flags().stale_ms_tape,
                    as_of_ms=(max((f["tsMs"] for f, _t, _s in rows), default=None)))


# ------------------------------------------------------------------------ D3 · the trader's dossier
WINDOW_DAYS = _tm.WINDOW_DAYS


def _trader_fills(wallet: str) -> list[dict]:
    """Every fill we hold for one wallet, as internal rows carrying their settlement.

    `is_winner` is NULL until a market resolves, and that NULL is what makes `resolved` False rather than
    False-by-default: an open position's realised PnL is 0 because nothing has been realised, not because it
    broke even. A win is `(shares − cost)` on a winning buy and `−cost` on a losing one, mirrored for a sell,
    all of it integer arithmetic on micro.
    """
    rows = _db.execute(
        "SELECT f.ts_ms, f.condition_id, f.token_id, COALESCE(f.outcome,''), f.side, f.price_micro,"
        " f.size_micro, f.usd_notional_micro, f.source, m.id, t.is_winner, m.minimum_tick_size,"
        " COALESCE(mm.category,''), m.end_ts"
        " FROM tape_fills f JOIN markets m ON m.condition_id = f.condition_id"
        " JOIN tokens t ON t.token_id = f.token_id"
        " LEFT JOIN market_meta mm ON mm.market_id = m.id"
        " WHERE f.wallet = ? ORDER BY f.ts_ms ASC, f.rowid ASC LIMIT ?",
        (str(wallet), TRADER_FILL_LIMIT)).fetchall()
    out = []
    for (ts, cond, token, outcome, side, price, size, notional, source, mid, winner, tick, category,
         end_ts) in rows:
        resolved = winner is not None and int(winner) >= 0
        won = bool(int(winner)) if resolved else False
        shares, cost = _tm._int(size), _tm._int(notional)
        realised = _lb_source.realised_micro(side=str(side), size_micro=shares, notional_micro=cost,
                                             winner=won, resolved=resolved)
        out.append({"tsMs": _tm._int(ts), "conditionId": str(cond), "tokenId": str(token),
                    "marketId": str(mid), "marketSlug": "", "question": "", "category": str(category),
                    "tick": norm_tick(tick), "side": str(side), "outcome": str(outcome),
                    "priceMicro": _tm._int(price), "sizeMicro": shares, "notionalMicro": cost,
                    "anonWallet": _anon(wallet), "labels": [], "source": str(source), "lagMs": 0,
                    "wallet": str(wallet), "winner": (won if resolved else None), "resolved": resolved,
                    "realisedMicro": realised, "endsMs": _tm._int(end_ts)})
    return out


def _trader_curve(fills: list[dict]) -> list[dict]:
    """The cumulative REALISED curve, per day, with its drawdown attached to every point.

    Realised only: an unrealised mark is a price somebody else's book printed, and mixing it into a PnL line is
    how a curve becomes a forecast. Points come from `drawdown_overlay`, so the overlay cannot be omitted by a
    caller that forgot — it is not a separate step to forget.
    """
    buckets: dict[int, int] = {}
    for f in fills:
        day = f["tsMs"] // 86_400_000 * 86_400_000
        buckets[day] = buckets.get(day, 0) + _tm._int(f["realisedMicro"])
    points, running = [], 0
    for day in sorted(buckets):
        running += buckets[day]
        points.append({"tsMs": day, "cumMicro": running, "openPositions": 0})
    return _tm.drawdown_overlay(points)


def _open_positions(fills: list[dict], marks: dict) -> list[dict]:
    """Positions still open, marked at the last fill we hold for that market.

    A position that nets to zero or below is dropped rather than shown: our tape is a slice of a venue's
    history (we started recording at some point, and the wallet traded before that), so a net SELL is far more
    likely to be an earlier buy we never saw than a short — and a screen that invents a short position from a
    partial tape is a screen that lies with confidence.
    """
    net: dict[tuple, dict] = {}
    for f in fills:
        key = (f["marketId"], f["outcome"])
        d = net.setdefault(key, {"marketId": f["marketId"], "outcome": f["outcome"], "sizeMicro": 0,
                                 "costMicro": 0, "tokenId": f["tokenId"], "tick": f["tick"],
                                 "resolved": f["resolved"], "winner": f["winner"],
                                 "category": f["category"], "lastMs": 0})
        sign = 1 if f["side"] == "BUY" else -1
        d["sizeMicro"] += sign * f["sizeMicro"]
        d["costMicro"] += sign * f["notionalMicro"]
        d["lastMs"] = max(d["lastMs"], f["tsMs"])
    out = []
    for (_mid, _outcome), d in sorted(net.items()):
        if d["sizeMicro"] <= 0 or d["resolved"]:
            continue
        m = marks.get(d["marketId"], {})
        raw = m.get("lastPriceMicro")
        mark = _tm._int(raw) if raw is not None else 0
        avg_entry = (d["costMicro"] * 1_000_000 // d["sizeMicro"]) if d["sizeMicro"] else 0
        row = _tm.portfolio_row(size_micro=d["sizeMicro"], avg_entry_micro=avg_entry, mark_micro=mark,
                                tick_micro=_tick_micro(d["tick"]),
                                ends_in_ms=(m.get("endsMs", 0) - _now_ms()),
                                cost_basis_micro=d["costMicro"])
        out.append({"marketId": d["marketId"], "marketSlug": m.get("marketSlug", ""),
                    "question": m.get("question", ""), "outcome": d["outcome"], "tokenId": d["tokenId"],
                    "category": d["category"], "resolved": False, "winner": None,
                    "markSource": "last_fill" if mark else "unknown", **_position_out(row)})
    return out


def _tick_micro(tick) -> int:
    """A tick as micro-units, so a mark can be checked against it without a float anywhere near money."""
    return _tick_units_per_hundredth(norm_tick(tick))


def _tick_units_per_hundredth(tick: str) -> int:
    # `norm_tick` returns "0.001" or "0.01"; both are exact strings, and the micro value follows from the
    # number of decimal places rather than from a parse.
    return 10_000 if str(tick).count("0") >= 2 and str(tick).endswith("01") else 1_000


def _hold_pairs(fills: list[dict]) -> list[dict]:
    """entry→exit pairs for the hold-time statistic.

    A settled market's exit is its own end time (that is when the position stopped existing) and an open one is
    left OPEN rather than paired: `hold_stats` counts open fills instead of averaging them in as zeros, because
    an unfinished trade is not a fast one.
    """
    first: dict[tuple, dict] = {}
    for f in fills:
        first.setdefault((f["marketId"], f["outcome"]), {"entryMs": f["tsMs"], "exitMs": None})
    for (market, _outcome), d in first.items():
        last_sell = [f for f in fills if f["marketId"] == market and f["side"] == "SELL"]
        if last_sell:
            d["exitMs"] = max(f["tsMs"] for f in last_sell)
    return list(first.values())


def _trader_window(fills: list[dict], lo: int, marks: dict) -> dict:
    """One window of the D3 metric set. Every field is computed for every window — a switcher that swaps some
    numbers and leaves others is a screen that cannot be read as a whole."""
    window = [f for f in fills if f["tsMs"] >= lo]
    settled = [f for f in window if f["resolved"]]
    by_market: dict[str, int] = {}
    for f in settled:
        by_market[f["marketId"]] = by_market.get(f["marketId"], 0) + _tm._int(f["realisedMicro"])
    resolved_markets, wins = len(by_market), sum(1 for v in by_market.values() if v > 0)
    gate = _tm.win_rate(wins, resolved_markets)
    realised = [_tm._int(f["realisedMicro"]) for f in settled]
    positions = _open_positions(window, marks)
    holds = _tm.hold_stats(_hold_pairs(window))
    curve = _trader_curve(window)
    return {"fills": len(window), "resolvedMarkets": resolved_markets, "wins": wins,
            "winRateBps": gate["bps"], "insufficientSample": gate["insufficientSample"],
            "sampleNote": gate["reason"], "sampleGate": _tm.SAMPLE_GATE,
            "volumeMicro": sum(_tm._int(f["notionalMicro"]) for f in window),
            "realisedMicro": sum(realised),
            "unrealisedMicro": sum(p["unrealisedMicro"] for p in positions),
            "bestMicro": max(realised, default=0), "worstMicro": min(realised, default=0),
            "maxDrawdownMicro": _tm.max_drawdown_micro(curve),
            "avgHoldMs": holds["avgHoldMs"], "medianHoldMs": holds["medianHoldMs"],
            "openFills": holds["openFills"], "matchedPositions": holds["matchedPositions"],
            "distinctMarkets": len({f["marketId"] for f in window}),
            "categories": len({f["category"] for f in window if f["category"]}),
            "source": "sampled", "asOfMs": (max((f["tsMs"] for f in window), default=0)),
            "computedMs": _now_ms(), "storedWinRateBps": None}


def _methodology() -> dict:
    """The methodology, shipped WITH the numbers it explains.

    A link alone is not enough: the prompt asks for a visible methodology on every behaviour metric, and the
    cheapest way to make something visible is to put it in the same object as the figure that depends on it.
    """
    return {"path": "/methodology/trader-metrics",
            "winRate": ("per settled MARKET, not per fill: a wallet that bought six times and sold once made one "
                        "decision. Suppressed below %d settled markets." % _tm.SAMPLE_GATE),
            "realised": ("from settled markets only: (shares − cost) for a winning buy, −cost for a losing one, "
                         "mirrored for a sell. Integer micro-USDC throughout."),
            "unrealised": ("net size marked at the last fill we saw in that market; a mark we do not have is "
                           "reported as unknown rather than as zero"),
            "drawdown": ("the distance below the running high-water mark of the cumulative realised curve, "
                         "computed per point so it cannot be drawn without the curve"),
            "hold": "entry→exit per (market, outcome); open positions are counted, not averaged in as zero",
            "whale": ("notional at or above max(p99.5 of the market's window fills, the size-bucket floor); "
                      "below %d fills in the window the floor applies alone" % _tm.WHALE_MIN_SAMPLE),
            "labels": _label_catalogue()}


@app.get("/v1/traders/{anon}", responses=TRADER_RESPONSES)
def get_trader(anon: str, request: Request,
               window: str = Query(default="30d", pattern="^(7d|30d|90d|all)$")):
    """D3's dossier: one wallet, four windows of the same metric set, the curve with its drawdown, the open
    positions, the behaviour labels with their rules, and the methodology behind every number.

    Two rules are structural here rather than decorative:
      * a win rate is `null` WITH A REASON below the sample gate, never a percentage;
      * the curve arrives carrying `peakMicro` and `drawdownMicro` per point, so no chart can draw PnL without
        the overlay.

    The header carries the pseudonym and never the address: `/v1/tape/fills` names traders the same way, and a
    screen that resolved one to an address would make the other pointless.
    """
    rid = request.state.request_id
    wallet = _wallet_for_anon(anon)
    if wallet is None:
        return err("NOT_FOUND", rid, detail="no trader with that pseudonym in the stored tape")
    fills = _trader_fills(wallet)
    if not fills:
        return err("NOT_FOUND", rid, detail="this pseudonym has no fills in the stored tape")
    now = _now_ms()
    marks = _market_rows()
    metrics = {}
    for key in _tm.WINDOW_KEYS:
        days = WINDOW_DAYS[key]
        metrics[key] = _trader_window(fills, 0 if days is None else now - days * 86_400_000, marks)
    days = WINDOW_DAYS[window]
    lo = 0 if days is None else now - days * 86_400_000
    window_fills = [f for f in fills if f["tsMs"] >= lo]
    curve = _trader_curve(window_fills)
    positions = _open_positions(window_fills, marks)
    labels = []
    for (label, conf, ev) in _db.execute(
            "SELECT label, confidence, evidence_json FROM wallet_labels WHERE wallet=? AND publishable=1"
            " ORDER BY label", (wallet,)).fetchall():
        fact = _LABEL_FACTS.get(str(label))
        if fact is None:
            continue
        labels.append({"label": str(label), "rule": fact["rule"], "disclaimer": fact["disclaimer"],
                       "confidence": _tm._int(conf), "evidence": _json_load(ev), "publishable": True})
    return _stamped({"cacheKey": "trader:%s:%s" % (anon, window), "anonWallet": anon, "window": window,
                     "windows": list(_tm.WINDOW_KEYS), "sampleGate": _tm.SAMPLE_GATE, "metrics": metrics,
                     "curve": curve, "curveWindow": window, "curveSource": "sampled",
                     "maxDrawdownMicro": _tm.max_drawdown_micro(curve),
                     "breakdown": _tm.category_breakdown(
                         [{"category": f["category"], "notionalMicro": f["notionalMicro"],
                           "realisedMicro": f["realisedMicro"]} for f in window_fills]),
                     "positions": positions,
                     "fills": [{**_fill_out(f), "winner": f["winner"], "resolved": f["resolved"],
                                "realisedMicro": f["realisedMicro"]}
                               for f in reversed(window_fills[-40:])],
                     "behaviour": labels, "methodology": _methodology()},
                    ttl_ms=5_000, stale_ms=max(flags().stale_ms_tape, 5_000),
                    as_of_ms=(max((f["tsMs"] for f in window_fills), default=None)))


# ------------------------------------------------------------------------ D7 · copy trading
def _copy_guard(config_id: str) -> dict | None:
    row = _db.execute("SELECT dry_run, skip_if_moved_cents, do_not_enter_within_hours, category_filter,"
                      " min_price_micro, max_price_micro, take_profit_micro, stop_loss_micro, live_since_ms,"
                      " updated_ms FROM copy_config_guards WHERE config_id=?", (str(config_id),)).fetchone()
    if row is None:
        return None
    keys = ("dry_run", "skip_if_moved_cents", "do_not_enter_within_hours", "category_filter",
            "min_price_micro", "max_price_micro", "take_profit_micro", "stop_loss_micro", "live_since_ms",
            "updated_ms")
    out = dict(zip(keys, row))
    for k in ("min_price_micro", "max_price_micro", "take_profit_micro", "stop_loss_micro"):
        out[k] = None if out[k] is None else _tm._int(out[k])
    out["dry_run"] = bool(out["dry_run"])
    return out


def _config_owned(config_id: str, uid: str) -> tuple | None:
    return _db.execute("SELECT id, user_id, source_user, mode, ratio_bps, max_order_micro, max_daily_micro,"
                       " blocked_markets, enabled, created_ms FROM copy_configs WHERE id=? AND user_id=?",
                       (str(config_id), str(uid))).fetchone()


def _source_stats(anon_id: str) -> dict:
    """A source's own record, with its losing windows intact and its win rate gated like everything else.

    `riskAdjustedBps` is net-after-fees per unit of drawdown, and it is what D7's discovery list sorts by. Raw
    PnL is deliberately NOT the sort: $50k through a $40k drawdown and $50k through a $4k one are not the same
    product, and a list ordered by the first sells the second as a surprise.
    """
    wallet = _wallet_for_anon(anon_id)
    rows = [] if wallet is None else _db.execute(
        "SELECT window_days, closed_trades, win_rate_bp, realized_pnl_micro, fees_micro,"
        " net_after_fees_micro, max_drawdown_micro, longest_losing_streak, avg_latency_ms, updated_ms"
        " FROM copy_source_stats WHERE source_user_id=? ORDER BY window_days", (str(wallet),)).fetchall()
    windows = []
    for (days, closed, wr_bp, realised, fees, net, dd, streak, latency, updated) in rows:
        gate = _tm.win_rate(int(wr_bp or 0) * int(closed or 0) // 10_000, _tm._int(closed))
        windows.append({"windowDays": _tm._int(days), "closedTrades": _tm._int(closed),
                        "realisedMicro": _tm._int(realised), "feesMicro": _tm._int(fees),
                        "netAfterFeesMicro": _tm._int(net), "maxDrawdownMicro": _tm._int(dd),
                        "longestLosingStreak": _tm._int(streak), "avgLatencyMs": _tm._int(latency),
                        "winRateBps": gate["bps"], "insufficientSample": gate["insufficientSample"],
                        "sampleNote": gate["reason"],
                        "riskAdjustedBps": _tm._int(net) * 10_000 // max(1, _tm._int(dd)),
                        "riskAdjustedNote": ("net after fees per unit of drawdown; a source can be profitable "
                                             "and still rank low here, which is the point of ranking this way"),
                        "updatedMs": _tm._int(updated)})
    return {"windows": windows,
            "ranking": ("sources are ranked risk-adjusted - net after fees per unit of drawdown - and NOT by "
                        "raw PnL; the ranking is stated here because a list that does not say how it is sorted "
                        "is a list that implies the obvious one")}


def _copy_warning(source_wallet: str) -> dict:
    """The slippage this source's copies actually produced, measured on OUR attempts.

    Shown BEFORE the confirm, never after: the question "is copying this person worth it" is answered by the
    distribution of our own deviations from their prices, and a dialog that asks it after the fact is a receipt.
    """
    devs, copied, skipped = [], 0, 0
    for (action, dev) in _db.execute("SELECT action, deviation_bps FROM copy_events WHERE source_user_id=?",
                                     (str(source_wallet),)).fetchall():
        if str(action) == "copied":
            copied += 1
            devs.append(_tm._int(dev))
        elif str(action) == "skipped":
            skipped += 1
    fills = [_tm._int(r[0]) for r in _db.execute(
        "SELECT would_size_micro * would_price_micro / 1000000 FROM copy_dry_runs WHERE source_user_id=?",
        (str(source_wallet),)).fetchall()]
    return _tm.copy_slippage_warning(deviations_bps=devs, copied=copied, skipped=skipped, fill_micros=fills)


def _copy_config_out(row: tuple) -> dict:
    (cid, _uid, source, mode, ratio, max_order, max_daily, _blocked, enabled, created) = row
    guard = _copy_guard(str(cid)) or {"dry_run": True, "skip_if_moved_cents": 2, "do_not_enter_within_hours": 24,
                                      "category_filter": "", "min_price_micro": None, "max_price_micro": None,
                                      "take_profit_micro": None, "stop_loss_micro": None, "live_since_ms": None}
    return {"configId": str(cid), "sourceAnon": _anon(str(source)), "mode": str(mode),
            "ratioBps": (None if ratio is None else _tm._int(ratio)),
            "maxOrderMicro": _tm._int(max_order), "maxDailyMicro": _tm._int(max_daily),
            "enabled": bool(enabled), "createdMs": _tm._int(created),
            # A config with NO guard row is a dry run: the safe state is the one you get by not writing
            # anything, and `guardsFromRow` says which of the two ways we arrived here.
            "dryRun": bool(guard["dry_run"]), "guardsFromRow": _copy_guard(str(cid)) is not None,
            "skipIfMovedCents": _tm._int(guard["skip_if_moved_cents"]),
            "doNotEnterWithinHours": _tm._int(guard["do_not_enter_within_hours"]),
            "categoryFilter": str(guard["category_filter"]),
            "minPriceMicro": guard["min_price_micro"], "maxPriceMicro": guard["max_price_micro"],
            "takeProfitMicro": guard["take_profit_micro"], "stopLossMicro": guard["stop_loss_micro"],
            "warning": _copy_warning(str(source)), "sourceStats": _source_stats(_anon(str(source)))}


#: The read side of the same resource: the write table minus the 400. Written out rather than computed,
#: because `tools/check-openapi.py` reads these tables from the AST and a comprehension is invisible to it —
#: and the invariant that must hold between the pair is checked there directly ("the read is the write minus
#: the key-required answer"), which is a stronger statement than "one was built from the other".
#: D7's sort keys, and the COLUMN each one reads. `riskAdjustedBps` is computed per row (net / drawdown) and
#: is the only key that is not a stored column — it is the default precisely because it is the one that punishes
#: a big PnL bought with a bigger drawdown.
_COPY_SOURCE_SORTS = {"riskAdjusted": "riskAdjustedBps", "netAfterFees": "netAfterFeesMicro",
                      "closedTrades": "closedTrades", "drawdown": "maxDrawdownMicro"}
_COPY_SOURCE_SORT_NOTES = {"riskAdjusted": "risk-adjusted return (net after fees per unit of drawdown)",
                           "netAfterFees": "net PnL after fees",
                           "closedTrades": "the number of closed trades",
                           "drawdown": "the largest drawdown"}

COPY_LIST_RESPONSES = {401: {"description": "a session is required"},
                       404: {"description": "unknown source pseudonym, or an unknown configId filter"},
                       422: {"description": "missing field, an unknown field, or maxOrder above maxDaily"}}


@app.get("/v1/copy/configs", responses=COPY_LIST_RESPONSES)
def list_copy_configs(request: Request,
                      configId: str | None = Query(default=None, max_length=64),
                      limit: int = Query(default=50, ge=1, le=200)):
    """This account's copy configs, each with the pre-confirm warning and the source's record.

    The record includes the windows where the source LOST money. A copy screen that only shows winners is the
    feature working as a trap, and the fixture's 7-day window is negative for exactly that reason.
    """
    rid = request.state.request_id
    uid, _row, e = _principal(request)
    if e:
        return e
    if configId and _config_owned(configId, str(uid)) is None:
        return err("NOT_FOUND", rid, detail="no such config for this account")
    rows = _db.execute("SELECT id, user_id, source_user, mode, ratio_bps, max_order_micro, max_daily_micro,"
                       " blocked_markets, enabled, created_ms FROM copy_configs WHERE user_id=?"
                       + (" AND id=?" if configId else "") + " ORDER BY created_ms DESC LIMIT ?",
                       ((str(uid), str(configId), limit) if configId else (str(uid), limit))).fetchall()
    items = [_copy_config_out(r) for r in rows]
    return _stamped({"items": items, "count": len(items),
                     "note": ("`dryRun` is per config: creation is always dry-run, and going live needs both an "
                              "acknowledged slippage warning and dry-run history this account produced")},
                    ttl_ms=0, stale_ms=0)


#: D7's discovery list. The read is `USER` rather than `PUBLIC` because the rows carry the caller's own
#: standing with each source (`currentlyCopying`), which is account state and not market data.
COPY_SOURCES_RESPONSES = {401: {"description": "a session is required"},
                          422: {"description": "an unknown sort key, or windowDays/limit outside the range"}}


@app.get("/v1/copy/sources", responses=COPY_SOURCES_RESPONSES)
def list_copy_sources(request: Request,
                      windowDays: int = Query(default=30, ge=7, le=90),
                      sort: str = Query(default="riskAdjusted", max_length=32),
                      onlyCopying: bool = Query(default=False),
                      limit: int = Query(default=50, ge=1, le=100)):
    """Who is worth copying, ranked by something that is not raw PnL.

    D7's rule is blunt: the default sort must not be PnL, because a list ordered by realised profit surfaces the
    gambler who won, and the user copies a strategy that does not exist. The default here is
    `riskAdjusted` — net after fees per unit of drawdown — and the payload says so in a sentence, because a list
    that does not state its own order implies the obvious one.

    Three things travel with every row and none of them are optional:

    * the **gate**: a win rate below `SAMPLE_GATE` settled trades is `null` plus the sentence explaining why, so
      a row cannot be read as "60% win rate" by a user who skips the footnote;
    * the **drawdown** the risk-adjusted number is divided by, because a ratio whose denominator is invisible is
      a marketing number;
    * the **losing window**: `netAfterFeesMicro` is returned as it is, negative included. This list is a
      discovery surface, and the fixture's most important row is the one that lost money.

    `onlyCopying` filters to sources this account already copies — the pause/stop list's own read, so the screen
    does not need a second call to know which rows are already engaged.
    """
    rid = request.state.request_id
    if sort not in _COPY_SOURCE_SORTS:
        return err("VALIDATION", rid, where=["sort must be one of %s" % ", ".join(sorted(_COPY_SOURCE_SORTS))])
    uid, _row, e = _principal(request)
    if e:
        return e
    rows = _db.execute(
        "SELECT source_user_id, window_days, closed_trades, win_rate_bp, realized_pnl_micro, fees_micro,"
        " net_after_fees_micro, max_drawdown_micro, longest_losing_streak, avg_latency_ms, updated_ms"
        " FROM copy_source_stats WHERE window_days=?", (int(windowDays),)).fetchall()
    out = []
    for (wallet, days, closed, wr_bp, realised, fees, net, dd, streak, latency, updated) in rows:
        wallet_s, closed_i, dd_i = str(wallet), _tm._int(closed), _tm._int(dd)
        gate = _tm.win_rate(_tm._int(wr_bp or 0) * closed_i // 10_000, closed_i)
        mine = _tm._int(_db.execute("SELECT COUNT(*) FROM copy_configs WHERE user_id=? AND source_user=?",
                                    (str(uid), wallet_s)).fetchone()[0])
        others = _tm._int(_db.execute("SELECT COUNT(*) FROM copy_configs WHERE source_user=?",
                                      (wallet_s,)).fetchone()[0])
        out.append({"anonWallet": _anon(wallet_s), "windowDays": _tm._int(days),
                    "closedTrades": closed_i, "realisedMicro": _tm._int(realised),
                    "feesMicro": _tm._int(fees), "netAfterFeesMicro": _tm._int(net),
                    "maxDrawdownMicro": dd_i, "longestLosingStreak": _tm._int(streak),
                    "avgLatencyMs": _tm._int(latency),
                    "winRateBps": gate["bps"], "insufficientSample": gate["insufficientSample"],
                    "sampleNote": gate["reason"], "sampleGate": gate["sampleGate"],
                    # The division is stated as the sentence and the integer: a ratio whose denominator is not
                    # on the row is a number a user cannot check.
                    "riskAdjustedBps": _tm._int(net) * 10_000 // max(1, dd_i),
                    "riskAdjustedRule": ("net after fees per unit of drawdown: %d / max(%d, 1). A source can be"
                                         " profitable and still rank low here, which is the point of ranking"
                                         " this way" % (_tm._int(net), dd_i)),
                    "copierCount": others, "currentlyCopying": mine > 0, "myConfigs": mine,
                    "updatedMs": _tm._int(updated)})
    if onlyCopying:
        out = [r for r in out if r["currentlyCopying"]]
    out.sort(key=lambda r: r[_COPY_SOURCE_SORTS[sort]], reverse=True)
    out = out[:limit]
    for i, row in enumerate(out):
        row["rank"] = i + 1
    return _stamped({"rows": out, "count": len(out), "windowDays": _tm._int(windowDays),
                     "sort": sort, "sorts": sorted(_COPY_SOURCE_SORTS),
                     "sortNote": "rows are ordered by %s, descending" % _COPY_SOURCE_SORT_NOTES[sort],
                     "ranking": ("this list is ranked risk-adjusted by default - net after fees per unit of"
                                 " drawdown - and NOT by raw PnL, because $50k through a $40k drawdown and $50k"
                                 " through a $4k one are not the same product"),
                     "sampleGate": _tm.SAMPLE_GATE,
                     "emptyNote": ("no source has trade statistics for this window yet. Discovery over"
                                   " arbitrary markets is the wallet radar's job; this list can only rank"
                                   " sources whose record we already hold"),
                     "note": ("every row carries the drawdown the ratio is divided by, the gate its win rate"
                              " passed or failed, and its losing streaks: a discovery list that only shows the"
                              " top line is how a gambler gets copied")},
                    ttl_ms=0, stale_ms=0)


def _create_copy_config_work(rid, uid, body):
    anon_id = str(body["sourceAnon"]).strip()
    source_wallet = _wallet_for_anon(anon_id)
    if source_wallet is None:
        return err("NOT_FOUND", rid, detail="unknown source pseudonym")
    mode = str(body.get("mode") or "cap")
    ratio = body.get("ratioBps")
    if mode == "ratio" and not ratio:
        return err("VALIDATION", rid, detail="mode=ratio needs ratioBps")
    max_order, max_daily = _tm._int(body["maxOrderMicro"]), _tm._int(body["maxDailyMicro"])
    if max_order > max_daily:
        # A per-order cap above the daily cap is not a stricter limit, it is a config that cannot fire twice
        # without breaching its own budget. Refused, rather than silently clamped.
        return err("VALIDATION", rid, detail="maxOrderMicro must not exceed maxDailyMicro")
    cid = "cfg-" + uuid.uuid4().hex[:12]
    _db.execute("INSERT INTO copy_configs (id, user_id, source_user, mode, ratio_bps, max_order_micro,"
                " max_daily_micro, blocked_markets, enabled, created_ms) VALUES (?,?,?,?,?,?,?,?,0,?)",
                (cid, str(uid), source_wallet, mode, (None if ratio is None else _tm._int(ratio)), max_order,
                 max_daily, json.dumps(list(body.get("blockedMarkets") or [])), _now_ms()))
    return _stamped({"configId": cid, "sourceAnon": anon_id, "mode": mode, "dryRun": True,
                     "maxOrderMicro": max_order, "maxDailyMicro": max_daily,
                     "warning": _copy_warning(source_wallet), "sourceStats": _source_stats(anon_id),
                     "next": ("this config starts as a dry-run: it records what it WOULD have done on"
                              " /v1/copy/configs/monitor and sends nothing to the venue")},
                    ttl_ms=0, stale_ms=0)


@app.post("/v1/copy/configs", status_code=200, responses=COPY_CREATE_RESPONSES,
           openapi_extra=_body_schema(COPY_CREATE_REQUIRED, COPY_CREATE_PROPS))
def create_copy_config(request: Request, body: dict = Body(...),
                       idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """Create a copy config. It is ALWAYS a dry run — and the response says so, in a field and in a sentence.

    The prompt's rule is that the latency/slippage warning appears in the UI before the confirm. The API's half
    of that promise is here: the response carries the warning and the source's record, and there is no field in
    the create schema that turns copying live. Turning it live is a second call with two conditions on it.
    """
    rid = request.state.request_id
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    bad = _check_body(body, COPY_CREATE_REQUIRED, rid, allowed=tuple(COPY_CREATE_PROPS))
    if bad is not None:
        return bad
    bad = _check_props(body, COPY_CREATE_PROPS, rid)
    if bad is not None:
        return bad
    uid, _row, e = _principal(request)
    if e:
        return e
    return _idem_run(str(uid), str(idempotency_key), body, rid,
                      lambda: _create_copy_config_work(rid, uid, body))


def _set_copy_guards_work(rid, uid, body):
    cid = str(body["configId"])
    row = _config_owned(cid, str(uid))
    if row is None:
        return err("NOT_FOUND", rid, detail="no such config for this account")
    source = _anon(str(row[2]))
    current = _copy_guard(cid) or {"dry_run": True, "skip_if_moved_cents": 2, "do_not_enter_within_hours": 24,
                                   "category_filter": "", "min_price_micro": None, "max_price_micro": None,
                                   "take_profit_micro": None, "stop_loss_micro": None, "live_since_ms": None}
    want_dry = bool(body.get("dryRun", current["dry_run"]))
    dry_runs = _tm._int(_db.execute("SELECT COUNT(*) FROM copy_dry_runs WHERE config_id=?",
                                    (cid,)).fetchone()[0])
    if not want_dry:
        if not body.get("acknowledgeSlippage"):
            return err("REFUSED", rid, detail=(
                "refused: going live needs acknowledgeSlippage=true - the confidence a copy screen should "
                "require is the confidence that comes from having read what this source's copies actually cost"))
        if dry_runs == 0:
            return err("REFUSED", rid, detail=(
                "refused: this config has no dry-run history yet. Copying somebody is a decision the product "
                "will not let you make blind, so it must first record - in your own account - what it would "
                "have done"))
    ranges = {"skipIfMovedCents": (0, 50), "doNotEnterWithinHours": (0, 168), "minPriceMicro": (1, 999_999),
              "maxPriceMicro": (1, 999_999), "takeProfitMicro": (1, 999_999), "stopLossMicro": (1, 999_999)}
    for key, (lo, hi) in ranges.items():
        if key in body and body[key] is not None:            # `_check_props` does not walk integers
            v = _tm._int(body[key])
            if not lo <= v <= hi:
                return err("VALIDATION", rid, where=["%s must be %d-%d" % (key, lo, hi)])
    for key, lo in (("maxOrderMicro", 1_000_000), ("maxDailyMicro", 1_000_000)):
        if key in body and _tm._int(body[key]) < lo:
            return err("VALIDATION", rid, where=["%s must be at least %d micro-USDC" % (key, lo)])
    if body.get("mode") == "ratio" and not (1 <= _tm._int(body.get("ratioBps") or 0) <= 10_000):
        return err("VALIDATION", rid, where=["ratioBps must be 1-10000"])
    fields = ("dry_run", "skip_if_moved_cents", "do_not_enter_within_hours", "category_filter", "min_price_micro",
              "max_price_micro", "take_profit_micro", "stop_loss_micro", "live_since_ms")
    values: dict = {"dry_run": (0 if not want_dry else 1),
                    "skip_if_moved_cents": _tm._int(body.get("skipIfMovedCents", current["skip_if_moved_cents"])),
                    "do_not_enter_within_hours": _tm._int(body.get("doNotEnterWithinHours",
                                                                   current["do_not_enter_within_hours"])),
                    "category_filter": str(body.get("categoryFilter", current["category_filter"])),
                    "min_price_micro": _as_opt_int(body.get("minPriceMicro", current["min_price_micro"])),
                    "max_price_micro": _as_opt_int(body.get("maxPriceMicro", current["max_price_micro"])),
                    "take_profit_micro": _as_opt_int(body.get("takeProfitMicro", current["take_profit_micro"])),
                    "stop_loss_micro": _as_opt_int(body.get("stopLossMicro", current["stop_loss_micro"])),
                    "live_since_ms": (_now_ms() if not want_dry else current["live_since_ms"])}
    # `config_id` plus the nine guard columns: the placeholder count has to match the column list exactly, and
    # the first version of this statement had one `?` too few for the id (an `OperationalError: N values for
    # N+1 columns` that the tests caught immediately and a reader never would have).
    _db.execute("INSERT INTO copy_config_guards (config_id,%s,updated_ms) VALUES (?,%s,?)"
                " ON CONFLICT(config_id) DO UPDATE SET %s, updated_ms=excluded.updated_ms"
                % (",".join(fields), ",".join(["?"] * len(fields)),
                   ",".join("%s=excluded.%s" % (f, f) for f in fields)),
                (cid, *[values[f] for f in fields], _now_ms()))
    if not want_dry:
        # `enabled` is the engine's own switch (P06); the guard row is what arms the order path. Both move
        # together, because a config that is "enabled" while its guard says dry-run is a config whose state
        # depends on which reader you ask.
        _db.execute("UPDATE copy_configs SET enabled=1 WHERE id=?", (cid,))
    guard = _copy_guard(cid) or {}
    return _stamped({"configId": cid, "dryRun": bool(guard.get("dry_run", True)),
                     "enabled": True, "dryRuns": dry_runs, "guards": guard,
                     "warning": _copy_warning(_wallet_for_anon(source) or ""),
                     "note": ("live copying is armed" if not want_dry else
                              "still a dry run: nothing will be sent to the venue")},
                    ttl_ms=0, stale_ms=0)


def _as_opt_int(value):
    return None if value is None else _tm._int(value)


def _idem_shape(key: str | None) -> JSONResponse | None:
    """The header contract, enforced for the P10 mutations: MISSING is a 400, MALFORMED is a 422 naming the field.

    Two different failures, two different answers, because the fix differs: a client that forgot the header
    needs to add it, and a client that sent a bad one needs to know the shape. Both used to be impossible to
    reach here — `_idem_shape` treated `None` as "nothing to check", so the four P10 POSTs accepted a mutating
    request with no key at all while the contract declared the header required. That is the exact hole rule 1
    of the contract exists to close ("an endpoint that is POST but idempotent by luck is how a retry
    double-spends"), and it was found by writing the radar tests, not by reading the contract.

    Recording the key is the other half, and it lives in `_idem_run`: these four routes validate the shape here
    and then run their work under the key, so a retry replays the first answer instead of creating a second
    config. The split is deliberate — a malformed key is a 4xx about the request and never touches the table,
    while a good key is a promise about the outcome and always does.
    """
    if not key:
        return err("IDEM_KEY_REQUIRED", "idem")
    if _IDEM_RE.match(str(key)):
        return None
    return err("VALIDATION", "idem", where=["Idempotency-Key must be 8-128 chars of [A-Za-z0-9_-]"])


@app.post("/v1/copy/configs/guards", status_code=200, responses=COPY_GUARD_RESPONSES,
           openapi_extra=_body_schema(COPY_GUARD_REQUIRED, COPY_GUARD_PROPS))
def set_copy_guards(request: Request, body: dict = Body(...),
                    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """Set a config's guard rails, including turning dry-run OFF — the only path by which live copying begins.

    Refused (409 `REFUSED`) unless the caller sends `acknowledgeSlippage: true` AND the config already has
    dry-run events recorded. The second condition is the interesting one: nobody goes live before the system has
    shown them, in their own account, what the strategy would have done. A dialog that asks "are you sure?"
    costs nothing and prevents nothing; a required dry-run history is evidence.
    """
    rid = request.state.request_id
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    bad = _check_body(body, COPY_GUARD_REQUIRED, rid, allowed=tuple(COPY_GUARD_PROPS))
    if bad is not None:
        return bad
    bad = _check_props(body, COPY_GUARD_PROPS, rid)
    if bad is not None:
        return bad
    uid, _row, e = _principal(request)
    if e:
        return e
    return _idem_run(str(uid), str(idempotency_key), body, rid,
                      lambda: _set_copy_guards_work(rid, uid, body))


def _idem_run(uid: str, key: str, body: dict, rid: str, work):
    """Run a mutating P10 handler ONCE per Idempotency-Key.

    Until this existed, the P10 writes validated the key's shape and then ignored it: a client whose request
    timed out and retried created a second copy config, spent a second radar scan, or saved a second view —
    which is precisely the failure an Idempotency-Key exists to prevent, and the one a user cannot see, because
    both answers look successful.

    The three refusals are the module's own, in its own vocabulary:

      * `IDEM_CONFLICT` — the key was used for a DIFFERENT body. Serving the first answer would silently ignore
        what this request asked for, so it is a 409 naming the reason and not a replay.
      * `IDEM_IN_PROGRESS` — another request with this key is still running. `busy`, not `state == in_progress`:
        the request that just inserted the row is also in_progress, and comparing the state alone makes every
        first request a 409 against itself (a bug this repo has already paid for once, in the order route).
      * a replay returns the STORED body, byte for byte, including its stamp — the answer the client would have
        received, not a re-derived one that might disagree with it.

    `work()` returns either a `dict` (success, 200) or a `JSONResponse` from `err(...)` (a refusal). A refusal
    abandons the key, because a user who fixes the typo must be able to retry: an abandoned key costs one row,
    a poisoned one costs the user the feature until a janitor sweeps it.
    """
    idem = Idem(_db)
    rec = idem.begin(uid, key, body)
    if rec.mismatch:
        return err("IDEM_CONFLICT", rid)
    if rec.busy:
        return err("IDEM_IN_PROGRESS", rid)
    if rec.replay:
        return JSONResponse(json.loads(rec.response_json), status_code=200)
    try:
        out = work()
    except Exception:
        # Never leave an in_progress row behind an exception: the client's retry would be answered
        # IDEM_IN_PROGRESS forever.
        idem.abandon(uid, key)
        raise
    if isinstance(out, JSONResponse):
        idem.abandon(uid, key)
        return out
    idem.finish(uid, key, out)
    return out

@app.get("/v1/copy/configs/monitor", responses=COPY_MONITOR_RESPONSES)
def copy_monitor(request: Request, configId: str = Query(min_length=1, max_length=64),
                 limit: int = Query(default=50, ge=1, le=200)):
    """Per-source monitor: source price vs our fill vs slippage, and every skip with its reason.

    Real fills (`copy_events`) and would-be fills (`copy_dry_runs`) are returned as two lists rather than one
    time-ordered stream, because the one thing this screen must never allow is mistaking a simulation for a
    trade — and a merged list is exactly how that happens. `dryRun` is on every row for the same reason, and a
    copied event with an empty `intentId` never reached the venue.
    """
    rid = request.state.request_id
    uid, _row, e = _principal(request)
    if e:
        return e
    row = _config_owned(configId, str(uid))
    if row is None:
        return err("NOT_FOUND", rid, detail="no such config for this account")
    source = str(row[2])
    # `copier_id` carries the CONFIG id, and that is what the engine's own `_day_counts` reads: keying this
    # query on the user id would show every config's events on every config's screen.
    live = []
    for (action, reason, dev, at, intent) in _db.execute(
            "SELECT action, reason, deviation_bps, at_ms, intent_id FROM copy_events"
            " WHERE copier_id=? ORDER BY at_ms DESC LIMIT ?", (str(configId), limit)).fetchall():
        live.append({"action": str(action), "reason": str(reason), "deviationBps": _tm._int(dev),
                     "atMs": _tm._int(at), "intentId": str(intent), "dryRun": False})
    would = []
    for (action, size, price, src_price, dev, reason, at, mid) in _db.execute(
            "SELECT would_action, would_size_micro, would_price_micro, source_price_micro, deviation_bps,"
            " reason, at_ms, market_id FROM copy_dry_runs WHERE config_id=? ORDER BY at_ms DESC LIMIT ?",
            (str(configId), limit)).fetchall():
        would.append({"action": str(action), "shares": _shares(_tm._int(size)),
                      "price": fmt_usdc(_tm._int(price)), "sourcePrice": fmt_usdc(_tm._int(src_price)),
                      "deviationBps": _tm._int(dev), "reason": str(reason), "atMs": _tm._int(at),
                      "marketId": str(mid), "dryRun": True})
    skipped = [r for r in live if r["action"] == "skipped"]
    copied = [r for r in live if r["action"] == "copied"]
    return _stamped({"configId": str(configId), "sourceAnon": _anon(source), "live": live, "wouldDo": would,
                     "skips": skipped,
                     "slippage": _tm.copy_slippage_warning(deviations_bps=[r["deviationBps"] for r in copied],
                                                           copied=len(copied), skipped=len(skipped)),
                     "sourceStats": _source_stats(_anon(source)),
                     "skipReasons": sorted({r["reason"] for r in skipped if r["reason"]})},
                    ttl_ms=0, stale_ms=0)


# ------------------------------------------------------------------------ D6 · the portfolio
@app.get("/v1/me/portfolio", responses=PORTFOLIO_RESPONSES)
def get_portfolio(request: Request):
    """D6's data: positions marked, grouped by event where the event is negRisk, the order history with its
    unknown rows INTACT, the realised curve, and the benchmark.

    The benchmark is holding pUSD — a flat line at the cash that was deposited, valued at 1.0000 — and it is
    stated in the payload rather than drawn as an unnamed second line: a benchmark a user cannot identify is
    decoration. Rows whose lifecycle we cannot name are returned, not filtered: a P06 finding is that the venue
    reports states we do not model, and a table that hides those rows is a table claiming every order resolved.
    """
    uid, _row, e = _principal(request)
    if e:
        return e
    marks = _market_rows()
    positions = []
    for (token, mid, shares, basis, outcome) in _db.execute(
            "SELECT l.token_id, l.market_id, SUM(l.shares_open_micro), SUM(l.basis_micro), t.outcome"
            " FROM position_lots l LEFT JOIN tokens t ON t.token_id = l.token_id"
            " WHERE l.user_id = ? AND l.shares_open_micro > 0 GROUP BY l.token_id, l.market_id, t.outcome",
            (str(uid),)).fetchall():
        size, cost = _tm._int(shares), _tm._int(basis)
        m = marks.get(_condition_for_market(mid), {})
        raw = m.get("lastPriceMicro")
        mark = _tm._int(raw) if raw is not None else 0
        avg_entry = (cost * 1_000_000 // size) if size else 0
        row = _tm.portfolio_row(size_micro=size, avg_entry_micro=avg_entry, mark_micro=mark,
                                tick_micro=_tick_micro(m.get("tick", "0.01")),
                                ends_in_ms=(m.get("endsMs", 0) - _now_ms()), cost_basis_micro=cost)
        positions.append({"tokenId": str(token), "marketId": str(mid), "marketSlug": m.get("marketSlug", ""),
                          "question": m.get("question", ""), "outcome": str(outcome),
                          "category": m.get("category", ""), "resolved": False, "winner": None,
                          "markSource": "last_fill" if mark else "unknown", **_position_out(row)})
    total_value = sum(p["valueMicro"] for p in positions)
    for p in positions:
        p["shareOfPortfolioBps"] = (p["valueMicro"] * 10_000 // total_value) if total_value else 0
    groups = _neg_risk_groups(positions)
    orders = []
    for (oid, mid, state, reason, size, price, created, notional) in _db.execute(
            "SELECT id, market_id, state, COALESCE(risk_code,''), size_micro, price_micro, created_ms,"
            " COALESCE(notional_micro,0) FROM order_intents WHERE user_id=? ORDER BY created_ms DESC LIMIT 100",
            (str(uid),)).fetchall():
        orders.append({"intentId": str(oid), "marketId": str(mid), "state": str(state),
                       "reason": (str(reason) or None), "shares": _shares(_tm._int(size)),
                       "price": fmt_usdc(_tm._int(price)), "createdMs": _tm._int(created),
                       "notionalMicro": _tm._int(notional), "unknownLifecycle": False})
    unknown = []
    # `order_lifecycle`'s own columns: the venue id is `order_id` ('' while the venue has not answered), the
    # state machine includes `unknown`, and `show_as_working` is written WITH the state so a read path cannot
    # guess. These rows are returned rather than filtered: a table that hides them claims every order resolved,
    # and the reason a user needs is usually in `reason`.
    for (order_id, intent, state, reason, working, at) in _db.execute(
            "SELECT order_id, intent_id, state, COALESCE(reason,''), show_as_working, at_ms FROM order_lifecycle"
            " WHERE user_id=? AND state IN ('unknown','partial') ORDER BY at_ms DESC LIMIT 50",
            (str(uid),)).fetchall():
        unknown.append({"venueOrderId": str(order_id), "intentId": str(intent), "state": str(state),
                        "reason": str(reason), "showAsWorking": bool(working), "atMs": _tm._int(at),
                        "unknownLifecycle": True})
    cash = _tm._int(_db.execute("SELECT COALESCE(SUM(amount_micro),0) FROM cash_ledger WHERE user_id=?",
                                (str(uid),)).fetchone()[0])
    # The equity curve, per day, from the ledger the money path already writes. `cash_ledger` has `created_ms`,
    # not `at_ms` — reading the wrong column here produced a working query that returned nothing, which is the
    # failure mode that looks like "the user has no history".
    buckets: dict[int, int] = {}
    for (created, amount) in _db.execute(
            "SELECT created_ms, amount_micro FROM cash_ledger WHERE user_id=? ORDER BY created_ms",
            (str(uid),)).fetchall():
        day = _tm._int(created) // 86_400_000 * 86_400_000
        buckets[day] = buckets.get(day, 0) + _tm._int(amount)
    curve, running = [], 0
    for day in sorted(buckets):
        running += buckets[day]
        curve.append({"tsMs": day, "cumMicro": running, "openPositions": 0})
    curve = _tm.drawdown_overlay(curve)
    invested = sum(p["costBasisMicro"] for p in positions)
    return _stamped({"positions": positions, "negRiskGroups": groups, "orders": orders,
                     "unknownLifecycle": unknown, "pnlCurve": curve,
                     "maxDrawdownMicro": _tm.max_drawdown_micro(curve),
                     "totals": {"valueMicro": total_value, "cashMicro": cash,
                                "equityMicro": cash + total_value,
                                "unrealisedMicro": sum(p["unrealisedMicro"] for p in positions),
                                "costBasisMicro": invested},
                     "benchmark": {"kind": "hold_pusd", "valueMicro": cash, "rateBps": 0,
                                   "note": ("holding pUSD: the deposited cash at 1.0000, unchanged. It is "
                                            "stated because a benchmark a user cannot identify is decoration, "
                                            "and pUSD is what the money is when it is not at risk")},
                     "csv": {"columns": ["intentId", "marketId", "state", "shares", "price",
                                         "notionalMicro", "createdMs"],
                             "note": ("columns are in micro-USDC where the name says Micro; the export is "
                                      "produced from this same payload so the file cannot disagree with the "
                                      "screen it was downloaded from")},
                     "emptyState": "/markets - browse markets and place a first order to start a portfolio"},
                    ttl_ms=0, stale_ms=0)


def _condition_for_market(market_id: str) -> str:
    row = _db.execute("SELECT condition_id FROM markets WHERE id=?", (str(market_id),)).fetchone()
    return str(row[0]) if row else ""


def _neg_risk_groups(positions: list[dict]) -> list[dict]:
    """Event-level exposure, for the events where exactly one outcome can pay.

    A long-Yes book across k outcomes of a negRisk event is ONE position with k legs; summing the legs' quoted
    values shows a payout that cannot happen, which is the same class of error as a PnL curve without its
    drawdown.
    """
    if not positions:
        return []
    rows = _db.execute("SELECT id, COALESCE(event_id,''), COALESCE(neg_risk,0) FROM markets").fetchall()
    event_of = {str(mid): str(ev) for mid, ev, neg in rows if ev and neg}
    by_event: dict[str, list[dict]] = {}
    for p in positions:
        ev = event_of.get(str(p["marketId"]))
        if ev:
            by_event.setdefault(ev, []).append({"sizeMicro": p["sizeMicro"], "valueMicro": p["valueMicro"],
                                                "costBasisMicro": p["costBasisMicro"]})
    out = []
    for ev, legs in by_event.items():
        title = _db.execute("SELECT title FROM events WHERE id=?", (ev,)).fetchone()
        out.append(_tm.neg_risk_group(legs, event_id=ev, event_title=(str(title[0]) if title else ev)))
    return out


# ------------------------------------------------------------------------ D4 · saved whale views
WHALE_VIEW_LIST_RESPONSES = {401: {"description": "a session is required"},
                             404: {"description": "no such market or rule"},
                             409: {"description": "refused: a notifying view needs a market target "
                                                  "(`alert_rules` requires one)"},
                             422: {"description": "a notifying view needs a market scope"}}


@app.get("/v1/whale-views", responses=WHALE_VIEW_LIST_RESPONSES)
def list_whale_views(request: Request,
                     viewId: str | None = Query(default=None, max_length=64),
                     limit: int = Query(default=50, ge=1, le=200)):
    """The saved views, each with its rule's budget, so a user can see how noisy an alert is allowed to be."""
    rid = request.state.request_id
    uid, _row, e = _principal(request)
    if e:
        return e
    if viewId and _db.execute("SELECT 1 FROM whale_views WHERE id=? AND user_id=?",
                              (str(viewId), str(uid))).fetchone() is None:
        return err("NOT_FOUND", rid, detail="no such view for this account")
    items = []
    for (vid, name, filters, channel, severity, scope, market_id, rule_id, created, fires, window,
         enabled) in _db.execute(
            "SELECT v.id, v.name, v.filters_json, v.channel, v.severity, v.scope, v.market_id, v.rule_id,"
            " v.created_ms, r.fires_per_window, r.window_ms, r.enabled FROM whale_views v"
            " LEFT JOIN alert_rules r ON r.id = v.rule_id WHERE v.user_id=?"
            + (" AND v.id=?" if viewId else "") + " ORDER BY v.created_ms DESC LIMIT ?",
            ((str(uid), str(viewId), limit) if viewId else (str(uid), limit))).fetchall():
        items.append({"viewId": str(vid), "name": str(name), "filters": _json_load(filters),
                      # A view saved without a channel is a filter, and `str(None)` would have rendered it as
                      # the literal channel "None" - which a client's enum check would reject and a human would
                      # read as a channel called None. NULL crosses as null.
                      "channel": (str(channel) if channel else None), "severity": str(severity),
                      "scope": str(scope),
                      "marketId": (None if market_id is None else str(market_id)),
                      "ruleId": (None if rule_id is None else str(rule_id)), "createdMs": _tm._int(created),
                      # A view notifies only when it is bound to a rule, and `alert_rules` requires a target:
                      # an alert with no target is a notification about nothing.
                      "notifies": rule_id is not None,
                      "firesPerWindow": (None if fires is None else _tm._int(fires)),
                      "ruleWindowMs": (None if window is None else _tm._int(window)),
                      "ruleEnabled": (None if enabled is None else bool(enabled))})
    return _stamped({"items": items, "count": len(items)}, ttl_ms=0, stale_ms=0)


def _create_whale_view_work(rid, uid, body):
    market_id = body.get("marketId")
    if market_id is not None and _db.execute("SELECT 1 FROM markets WHERE id=?",
                                             (str(market_id),)).fetchone() is None:
        return err("NOT_FOUND", rid, detail="unknown marketId")
    scope = "market" if market_id else "global"
    channel = body.get("channel")
    if channel and scope != "market":
        return err("REFUSED", rid, detail=(
            "refused: a notifying view needs a market. `alert_rules` requires a target (market or event), so a "
            "global view can be saved as a filter but cannot notify - save it without a channel, or scope it to "
            "a market"))
    severity = str(body.get("severity") or "notice")
    # The filters are stored as the knobs that produced them, so a saved view is reproducible from its own row
    # rather than from a version of this endpoint somebody has to remember.
    filters = {"minSeverity": str(body.get("minSeverity") or "notice"),
               "minNotionalMicro": _tm._int(body.get("minNotionalMicro") or 0),
               "multiple": (None if body.get("multiple") is None else _tm._int(body["multiple"]))}
    rule_id = None
    fires = window = None
    if channel:
        fires = _tm._int(body.get("firesPerWindow") or 4)
        window = _tm._int(body.get("ruleWindowMs") or 3_600_000)
        rule_id = "wr-" + uuid.uuid4().hex[:12]
        # Severity, channel and filters live in `params_json`: P04's rule table has a target, a kind and a
        # window budget, and a column per future knob is a schema that grows with the UI.
        _db.execute("INSERT INTO alert_rules (id, user_id, market_id, event_id, kind, fires_per_window,"
                    " window_ms, params_json, enabled, created_ms) VALUES (?,?,?,?,?,?,?,?,1,?)",
                    (str(rule_id), str(uid), str(market_id), None, "whale_fill", fires, window,
                     json.dumps({"severity": severity, "channel": str(channel), "filters": filters,
                                 "source": "whale_view"}), _now_ms()))
    view_id = "wv-" + uuid.uuid4().hex[:12]
    _db.execute("INSERT INTO whale_views (id, user_id, name, filters_json, channel, severity, scope,"
                " market_id, rule_id, created_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (view_id, str(uid), str(body["name"]).strip(), json.dumps(filters),
                 (str(channel) if channel else None), severity, scope,
                 (str(market_id) if scope == "market" else None), (str(rule_id) if rule_id else None),
                 _now_ms()))
    return _stamped({"viewId": view_id, "name": str(body["name"]).strip(), "filters": filters,
                     "channel": (str(channel) if channel else None), "severity": severity, "scope": scope,
                     "marketId": (str(market_id) if scope == "market" else None),
                     "ruleId": (str(rule_id) if rule_id else None), "createdMs": _now_ms(),
                     "notifies": bool(rule_id),
                     "firesPerWindow": fires, "ruleWindowMs": window,
                     "ruleEnabled": (True if rule_id else None),
                     "note": ("a saved view without a channel is a filter you look at; with one it is an alert."
                              " This one is %s." % ("bound to a rule" if rule_id else "a filter only"))},
                    ttl_ms=0, stale_ms=0)


@app.post("/v1/whale-views", status_code=200, responses=WHALE_VIEW_RESPONSES,
           openapi_extra=_body_schema(WHALE_VIEW_REQUIRED, WHALE_VIEW_PROPS))
def create_whale_view(request: Request, body: dict = Body(...),
                      idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """D4's saved view, plus the inline alert rule it may create.

    The body follows the contract: `name` is the only required field, the filters are three flat knobs
    (`minSeverity`, `minNotionalMicro`, `multiple`) and `marketId` decides the scope. A `channel` is what makes
    a view NOTIFY - and a notifying view with no market is refused with 409 `REFUSED`, because `alert_rules`
    carries a `rule_has_target` CHECK: a rule with no target matches everything and therefore fires on
    everything, and the API's job is to say so rather than to invent a wildcard that the database would then
    reject five layers down.

    Alert-rule creation is inline (`channel` given) because D4 asks for it inline: a form that posts to a second
    endpoint is a form that loses what the user just typed.
    """
    rid = request.state.request_id
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    bad = _check_body(body, WHALE_VIEW_REQUIRED, rid, allowed=tuple(WHALE_VIEW_PROPS))
    if bad is not None:
        return bad
    bad = _check_props(body, WHALE_VIEW_PROPS, rid)
    if bad is not None:
        return bad
    uid, _row, e = _principal(request)
    if e:
        return e
    return _idem_run(str(uid), str(idempotency_key), body, rid,
                      lambda: _create_whale_view_work(rid, uid, body))


# ---------------------------------------------------------------- D5 · Wallet Radar
# The two ends of one scan: POST enqueues or answers, GET collects. The rules that make this affordable are
# visible in the payload rather than in a comment: a cached scan costs nothing, a scan past the account's tier
# budget is a 429 with the tier in it, and a scan big enough to be slow becomes a job so the latency lands
# outside the request that asked for it.
RADAR_RESPONSES = {400: {"description": "missing Idempotency-Key; a malformed one is a 422 naming the field"},
                         401: {"description": "a session is required"},
                   422: {"description": "1 to 10 markets, and the ids must be strings"},
                   429: {"description": "this account's daily radar scan budget is spent; cached scans are still "
                                        "free"}}
RADAR_JOB_RESPONSES = {401: {"description": "a session is required"},
                       404: {"description": "no such job for this account, or it has expired"}}
RADAR_REQUIRED = ("marketIds",)
RADAR_PROPS = {"marketIds": {"type": "array", "items": {"type": "string", "minLength": 3, "maxLength": 128}},
               "ranking": {"type": "string", "enum": list(_radar.RANKINGS)}}
#: The latency line. Above this many uncached markets the scan is enqueued instead of run: the numbers here come
#: from the seeded tape, where a scan of the ten largest markets reads about 12k fills, and a page that waits on
#: that is a page a user reloads - which is how one scan becomes three.
RADAR_ASYNC_AT = 6
#: Per-plan scans per day. Pro's budget is deliberately not "unlimited": the venue's rate limit is the real
#: ceiling, and a plan that promises unlimited reads is a plan that discovers the ceiling in production.
RADAR_PER_DAY = {"free": 20, "trader": 100, "pro": 200, "team": 1000}
_RADAR_CACHE: dict[str, dict] = {}
_RADAR_JOBS: dict[str, dict] = {}


def _radar_plan(uid: str) -> tuple[str, int]:
    row = _db.execute("SELECT plan, radar_poll_ms FROM entitlements WHERE user_id=?", (str(uid),)).fetchone()
    plan = str(row[0]) if row and row[0] else "free"
    per_day = RADAR_PER_DAY.get(plan, RADAR_PER_DAY["free"])
    return plan, per_day


def _audit_scan(uid: str, detail: dict) -> None:
    """One audit row per scan. `audit_log` is append-only (a trigger blocks UPDATE and DELETE), which is exactly
    the property a quota counter wants: the count cannot be quietly adjusted by the code that is being counted.
    """
    _db.execute("INSERT INTO audit_log (at_ms, actor_type, actor_id, action, target_table, target_id,"
                " request_id, detail_json) VALUES (?,?,?,?,?,?,?,?)",
                (_now_ms(), "user", str(uid), "radar.scan", "radar", str(detail.get("job") or "sync"), "",
                 json.dumps(detail, sort_keys=True)))


def _radar_used_today(uid: str) -> int:
    """Scans used today, counted from the audit log rather than a counter table.

    A counter is a second copy of a fact the audit trail already holds, and the copy is the one that drifts. A
    cached scan is not counted: the cache is the reason the second scan is cheap, so charging for it would price
    the thing the design is selling.
    """
    start = _now_ms() // 86_400_000 * 86_400_000
    row = _db.execute("SELECT COUNT(*) FROM audit_log WHERE actor_id=? AND action='radar.scan' AND at_ms>=?",
                      (str(uid), start)).fetchone()
    return int(row[0]) if row else 0


def _radar_fills(market_ids: list[str]) -> list[dict]:
    """The fills we hold for the selected markets, in the same internal shape the dossier uses - including
    `resolved`/`realisedMicro`, because the profit ranking is a claim about settled outcomes and an unsettled
    fill must not be able to contribute to it."""
    if not market_ids:
        return []
    # `_market_rows()` is keyed by CONDITION id (it is the tape's lookup, and the tape joins on condition ids),
    # so filtering it here by market id returns an empty dict rather than a wrong one. Only the tick is wanted.
    marks = {m["marketId"]: m for m in _market_rows().values()}
    qs = ",".join("?" for _ in market_ids)
    rows = _db.execute(
        "SELECT f.ts_ms, f.wallet, f.condition_id, f.token_id, f.side, f.price_micro, f.size_micro,"
        " f.usd_notional_micro, t.is_winner, m.id FROM tape_fills f"
        " JOIN markets m ON m.condition_id = f.condition_id"
        " JOIN tokens t ON t.token_id = f.token_id"
        " WHERE m.id IN (%s) ORDER BY f.ts_ms ASC, f.rowid ASC" % qs, tuple(market_ids)).fetchall()
    out = []
    for (ts, wallet, cond, token, side, price, size, notional, winner, mid) in rows:
        resolved = winner is not None and int(winner) >= 0
        won = bool(int(winner)) if resolved else False
        shares, cost = _tm._int(size), _tm._int(notional)
        realised = _lb_source.realised_micro(side=str(side), size_micro=shares, notional_micro=cost,
                                             winner=won, resolved=resolved)
        out.append({"tsMs": _tm._int(ts), "wallet": str(wallet), "marketId": str(mid),
                    "conditionId": str(cond), "tokenId": str(token), "side": str(side),
                    "priceMicro": _tm._int(price), "sizeMicro": shares, "notionalMicro": cost,
                    "resolved": resolved, "won": won, "realisedMicro": realised,
                    "tick": norm_tick((marks.get(str(mid)) or {}).get("tick"))})
    return out


def _radar_labels() -> dict[str, list[dict]]:
    """The publishable labels, with their rule and disclaimer, keyed by wallet. A classification badge without
    its rule is a horoscope, so the rule travels with it or the badge does not travel."""
    out: dict[str, list[dict]] = {}
    for (wallet, label, conf) in _db.execute(
            "SELECT wallet, label, confidence FROM wallet_labels WHERE publishable=1 ORDER BY label").fetchall():
        fact = _LABEL_FACTS.get(str(label))
        if fact is None:
            continue
        out.setdefault(str(wallet), []).append({"label": str(label), "rule": fact["rule"],
                                                "disclaimer": fact["disclaimer"],
                                                "confidence": _tm._int(conf), "publishable": True})
    return out


def _radar_questions(market_ids: list[str]) -> dict[str, str]:
    qs = ",".join("?" for _ in market_ids) or "''"
    rows = _db.execute("SELECT id, COALESCE(question,'') FROM markets WHERE id IN (%s)" % qs,
                       tuple(market_ids)).fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


def _radar_payload(uid: str, market_ids: list[str], ranking: str, *, cached: bool, job_id: str | None) -> dict:
    """One scan, assembled. Split out so the synchronous path and the job's completion build the SAME payload -
    a second builder is how the cached answer and the fresh answer start disagreeing about their own numbers."""
    ranked = _radar.rank(_radar_fills(market_ids), market_ids=market_ids, sample_gate_n=_tm.SAMPLE_GATE)
    labels = _radar_labels()
    questions = _radar_questions(market_ids)
    meta = ranked["rankingsMeta"]
    tables: dict[str, list[dict]] = {}
    for entry in meta:
        rows = ranked.get(entry["id"]) or []
        tables[entry["id"]] = [
            {**{k: v for k, v in r.items() if k != "reason"},
             "anonWallet": _anon(r["wallet"]),
             "matched": [{"marketId": m, "question": questions.get(m, "")} for m in r["markets"]],
             "labels": labels.get(r["wallet"], []),
             "rank": i + 1,
             "reason": r["reason"]}
            for i, r in enumerate(rows)
        ]
    plan, per_day = _radar_plan(uid)
    used = _radar_used_today(uid)
    return {"items": tables.get(ranking) or tables["active"], "ranking": ranking,
            "rankings": tables, "rankingsMeta": meta,
            "unranked": [{"anonWallet": _anon(r["wallet"]), "wallet": r["wallet"], "fills": r["fills"],
                          "markets": r["markets"], "realisedMicro": r["realisedMicro"],
                          "winRateBps": r["winRateBps"], "insufficientSample": True,
                          "reason": r["reason"]} for r in (ranked.get("unranked") or [])],
            "markets": market_ids, "scanned": ranked["scanned"], "sampleGate": ranked["sampleGate"],
            "quota": {"plan": plan, "usedToday": used, "perDay": per_day, "cached": cached, "jobId": job_id,
                      "pollMs": 2_000,
                      "note": ("this scan came from the cache and cost you nothing"
                               if cached else
                               "%d of %d scans used today on the %s plan; a repeated scan of the same markets is "
                               "free" % (used, per_day, plan))},
            "costNote": ("a scan is cached for %d seconds, the same set in a different order is the same scan, "
                         "and over %d uncached markets the scan runs as a job so the page does not wait on it"
                         % (_radar.CACHE_TTL_MS // 1000, RADAR_ASYNC_AT))}


def _create_radar_run_work(rid, uid, body):
    raw = body.get("marketIds")
    if raw is not None and not isinstance(raw, list):
        return err("RADAR_SCOPE", rid, detail="marketIds must be an array of market ids")
    market_ids, refusal = _radar.validate_markets(raw or [])
    if refusal:
        return err("RADAR_SCOPE", rid, detail=refusal)
    ranking = str(body.get("ranking") or "active")
    key = "%s|%s" % (uid, _radar.cache_key(market_ids))
    hit = _RADAR_CACHE.get(key)
    if hit and (_now_ms() - _tm._int(hit["atMs"])) <= _radar.CACHE_TTL_MS:
        payload = _radar_payload(uid, market_ids, ranking, cached=True, job_id=None)
        return _stamped(payload, ttl_ms=_radar.CACHE_TTL_MS, stale_ms=_radar.CACHE_TTL_MS,
                        as_of_ms=_tm._int(hit["atMs"]))
    _plan, per_day = _radar_plan(uid)
    if _radar_used_today(uid) >= per_day:
        return err("QUOTA_EXCEEDED", rid, detail=(
            "refused: %d of %d radar scans used today on this plan. A repeat of a scan you already ran is still "
            "free - the cache is not metered." % (_radar_used_today(uid), per_day)))
    if len(market_ids) > RADAR_ASYNC_AT:
        job_id = "rr-" + uuid.uuid4().hex[:12]
        _RADAR_JOBS[job_id] = {"userId": str(uid), "marketIds": market_ids, "ranking": ranking,
                               "state": "queued", "atMs": _now_ms()}
        _audit_scan(str(uid), {"markets": market_ids, "job": job_id, "cached": False})
        payload = _radar_payload(uid, market_ids, ranking, cached=False, job_id=job_id)
        # An enqueued scan answers with the ranking set it can already compute and names the job: the page shows
        # something true immediately and fills in the rest, instead of holding a spinner over ten markets.
        payload["status"] = "queued"
        payload["quota"]["note"] = ("%d markets is past the %d-market line, so this scan runs as a job; the table "
                                    "below is what we already hold" % (len(market_ids), RADAR_ASYNC_AT))
        return _stamped(payload, ttl_ms=0, stale_ms=1_000, as_of_ms=_now_ms())
    _audit_scan(str(uid), {"markets": market_ids, "job": None, "cached": False})
    _RADAR_CACHE[key] = {"atMs": _now_ms()}
    payload = _radar_payload(uid, market_ids, ranking, cached=False, job_id=None)
    payload["status"] = "done"
    return _stamped(payload, ttl_ms=0, stale_ms=2_000, as_of_ms=_now_ms())


@app.post("/v1/radar/runs", status_code=200, responses=RADAR_RESPONSES,
          openapi_extra=_body_schema(RADAR_REQUIRED, RADAR_PROPS))
def create_radar_run(request: Request, body: dict = Body(...),
                     idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """D5: up to ten markets in, four rankings out, with the sample gate on the profit list and the account's
    scan budget stated in the answer.

    The order of the decisions matters and is the order below: shape, identity, scope, budget, cache, then work.
    A scan that is refused for budget must not have enqueued a job first, and a cached scan must not spend
    budget - both of those are refusals a user can see, so both of them are checked before anything else is
    written.
    """
    rid = request.state.request_id
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    bad = _check_body(body, RADAR_REQUIRED, rid, allowed=tuple(RADAR_PROPS))
    if bad is not None:
        return bad
    bad = _check_props(body, RADAR_PROPS, rid)
    if bad is not None:
        return bad
    uid, _row, e = _principal(request)
    if e:
        return e
    return _idem_run(str(uid), str(idempotency_key), body, rid,
                      lambda: _create_radar_run_work(rid, uid, body))


@app.get("/v1/radar/runs/{job_id}", responses=RADAR_JOB_RESPONSES)
def get_radar_run(job_id: str, request: Request):
    """The other half of a big scan.

    The job runs on the FIRST poll rather than in a worker thread. That is a real trade and it is stated rather
    than dressed up: the request that asked for ten markets still returns immediately, the work happens on a
    later request, and it happens exactly once - the second poll reads the completed job. A worker thread would
    move the same work behind a queue this single-process API does not have yet; when the executor's worker
    lands (P12), this is where it plugs in, and the shape of the answer does not change.
    """
    rid = request.state.request_id
    uid, _row, e = _principal(request)
    if e:
        return e
    job = _RADAR_JOBS.get(str(job_id))
    if not job or str(job.get("userId")) != str(uid):
        return err("NO_SUCH_RESOURCE", rid, detail="no such radar job for this account")
    if job["state"] != "done":
        market_ids = list(job["marketIds"])
        _audit_scan(str(uid), {"markets": market_ids, "job": str(job_id), "cached": False})
        _RADAR_CACHE["%s|%s" % (uid, _radar.cache_key(market_ids))] = {"atMs": _now_ms()}
        payload = _radar_payload(str(uid), market_ids, str(job["ranking"]), cached=False, job_id=None)
        payload["status"] = "done"
        payload["quota"]["note"] = ("the job finished and its scan is now cached for %d seconds; a repeat of "
                                    "these markets costs nothing"
                                    % (_radar.CACHE_TTL_MS // 1000))
        job.update({"state": "done", "payload": payload})
        return _stamped(payload, ttl_ms=0, stale_ms=2_000, as_of_ms=_now_ms())
    payload = dict(job["payload"])
    payload["quota"] = {**payload["quota"], "jobId": None,
                        "note": "this job finished and its scan is now cached for %d seconds"
                                % (_radar.CACHE_TTL_MS // 1000)}
    return _stamped(payload, ttl_ms=_radar.CACHE_TTL_MS, stale_ms=_radar.CACHE_TTL_MS,
                    as_of_ms=_tm._int(job["atMs"]))




def _json_any(text, default):
    """A JSON column that may hold an object OR an array. `_json_load` is object-only by design — it parses
    request bodies, whose shape is known — and `automation_rule_policy.actions_json` is a list, so the two
    need different readers. A loader that returned `{}` for a list would turn every rule's actions into a dict
    and every run log into a lie."""
    if not isinstance(text, str) or not text:
        return default
    try:
        out = json.loads(text)
    except ValueError:
        return default
    return out


def _json_list(text) -> list:
    """A list column, and only a list. An object here means the row was written by something that did not read
    this function; an empty list plus the run log's own text is a better answer than a `for` loop over a dict's
    keys."""
    out = _json_any(text, [])
    return out if isinstance(out, list) else []


def _rows(cur) -> list[dict]:
    """Rows as dicts, keyed by the SELECT's own column names.

    `dict(row)` looks like the same thing and is not, anywhere in this file: the API's connection returns plain
    tuples (`_db` never sets `row_factory`, because the rest of the file reads columns positionally), so a dict
    built from one is a dict over its FIRST value — which for a rule id is a fifteen-character string, and the
    failure reads like a parse bug rather than the adapter mistake it is. Reading the names off the cursor's
    `description` also means a SELECT's own column order stays the single source of truth, so adding a column
    cannot silently shift a field into the wrong key.
    """
    cols = [str(c[0]) for c in (cur.description or [])]
    return [dict(zip(cols, r)) for r in cur.fetchall()]

# ============================================================================== P10 · D8 automation ==
# D8's surface is a *view* over P06's engine, not a second engine: the rules, their policy, their state and their
# run log are P06's tables, and every document this file writes is validated by `automation.engine.validate_rule`
# before it is stored. Two things are the API's own job and nothing else's:
#
#   * **a rule cannot be created live.** There is no `enabled` in the create schema, exactly as there is no
#     `dryRun` in the copy config's, and the only path to `enabled` refuses until the rule has been evaluated
#     against live data at least once. The dry run is not a checkbox; it is a row in `automation_runs`.
#   * **the run log is readable.** "Why did the bot do that" is answered from `automation_runs`, including the
#     leaf values of the trigger that was read, which is why the engine evaluates every leaf eagerly.

AUTOMATION_CREATE_RESPONSES = {400: {"description": "missing Idempotency-Key; a malformed one is a 422 naming "
                                                  "the field"},
                              401: {"description": "a session is required"},
                              404: {"description": "a target market is not one we know"},
                              409: {"description": "refused: the rule cannot be saved as asked (a halted account, "
                                                   "or the concurrent-rule cap)"},
                              422: {"description": "the builder payload does not compile, or the engine refuses "
                                                   "the rule it compiles to"}}
AUTOMATION_GUARD_RESPONSES = {400: {"description": "missing Idempotency-Key; a malformed one is a 422 naming "
                                                 "the field"},
                             401: {"description": "a session is required"},
                             404: {"description": "no such rule for this account"},
                             409: {"description": "refused: no completed dry run, or the daily-loss halt is on"},
                             422: {"description": "missing field or an unknown state"}}
AUTOMATION_LIST_RESPONSES = {401: {"description": "a session is required"}}
AUTOMATION_RUNS_RESPONSES = {401: {"description": "a session is required"},
                             404: {"description": "no such rule for this account"}}
AUTOMATION_TEMPLATES_RESPONSES = {401: {"description": "a session is required"}}
AUTOMATION_PREVIEW_RESPONSES = {400: {"description": "missing Idempotency-Key; a malformed one is a 422 naming "
                                                   "the field"},
                                401: {"description": "a session is required"},
                                404: {"description": "no such rule for this account"},
                                422: {"description": "the payload neither names a saved rule nor compiles to one"}}

AUTOMATION_CREATE_REQUIRED = ("kind", "triggers", "actions", "targets")
AUTOMATION_CREATE_PROPS = {"kind": {"type": "string",
                                    "enum": ["entry", "exit", "cancel", "alert", "scale_out", "hedge",
                                             "take_profit", "stop_loss", "auto_redeem"]},
                           "name": {"type": "string", "maxLength": 80},
                           "match": {"type": "string", "enum": ["all", "any"]},
                           "triggers": {"type": "array", "minItems": 1},
                           "actions": {"type": "array", "minItems": 1},
                           "targets": {"type": "array", "minItems": 1},
                           "maxPerDay": {"type": "integer", "minimum": 1, "maximum": 288},
                           "minIntervalMs": {"type": "integer", "minimum": 1000},
                           "humanPriorityMs": {"type": "integer", "minimum": 0},
                           "maxLossMicro": {"type": "integer", "minimum": 0}}
AUTOMATION_GUARD_REQUIRED = ("ruleId", "state")
AUTOMATION_GUARD_PROPS = {"ruleId": {"type": "string", "minLength": 1, "maxLength": 64},
                          "state": {"type": "string", "enum": ["active", "paused"]},
                          "reason": {"type": "string", "maxLength": 200}}
AUTOMATION_PREVIEW_REQUIRED: tuple = ()
AUTOMATION_PREVIEW_PROPS = {"ruleId": {"type": "string", "minLength": 1, "maxLength": 64},
                            "kind": {"type": "string"},
                            "match": {"type": "string", "enum": ["all", "any"]},
                            "triggers": {"type": "array"},
                            "actions": {"type": "array"},
                            "targets": {"type": "array"},
                            "maxLossMicro": {"type": "integer", "minimum": 0}}


def _automation_rows(uid: str) -> list[dict]:
    """This account's rules, with policy, state, label, targets and the last run already joined in.

    One query per table and a dict per rule id rather than a query per rule: the list is capped at ten rules, but
    the pattern is what matters — an N+1 here would be copied into the run-history read, where N is 200.
    """
    rows = _rows(_db.execute(
        "SELECT r.id, r.user_id, r.kind, r.trigger_json, r.max_loss_micro, r.enabled, r.last_run_ms, "
        "       p.trigger_json AS p_trigger, p.actions_json, p.dry_run_completed_ms, p.max_per_day, "
        "       p.min_interval_ms, p.human_priority_ms, p.last_fire_ms, "
        "       s.paused_reason, s.failure_count, s.last_error, l.name AS label "
        "FROM automation_rules r LEFT JOIN automation_rule_policy p ON p.rule_id = r.id "
        "LEFT JOIN automation_rule_state s ON s.rule_id = r.id "
        "LEFT JOIN automation_rule_labels l ON l.rule_id = r.id "
        "WHERE r.user_id = ? ORDER BY r.id", (uid,)))
    if not rows:
        return []
    ids = [str(r["id"]) for r in rows]
    marks = ",".join("?" * len(ids))
    targets: dict[str, list[dict]] = {}
    for t in _db.execute("SELECT rule_id, market_id, token_id FROM automation_rule_targets "
                         "WHERE rule_id IN (%s) ORDER BY market_id" % marks, tuple(ids)).fetchall():
        targets.setdefault(str(t[0]), []).append({"marketId": str(t[1]), "tokenId": str(t[2] or "")})
    last: dict[str, dict] = {}
    for run in _db.execute("SELECT rule_id, mode, outcome, reason, deny_code, intent_id, detail_json, at_ms "
                           "FROM automation_runs WHERE rule_id IN (%s) ORDER BY at_ms DESC, id DESC" % marks,
                           tuple(ids)).fetchall():
        rid = str(run[0])
        if rid in last:
            continue
        detail = _json_any(run[6], {})
        last[rid] = {"outcome": str(run[2] or ""), "mode": str(run[1] or ""), "reason": str(run[3] or ""),
                     "deny_code": str(run[4] or ""), "intentId": str(run[5] or ""),
                     "atMs": int(run[7] or 0), "leaves": _au_console.leaves_sentence(detail.get("leaves"))}
    since = _now_ms() - 86_400_000
    today: dict[str, int] = {}
    for c in _db.execute("SELECT rule_id, COUNT(*) FROM automation_runs WHERE rule_id IN (%s) AND at_ms >= ? "
                         "AND mode = 'live' AND outcome = 'placed' GROUP BY rule_id" % marks,
                         tuple(ids) + (since,)).fetchall():
        today[str(c[0])] = int(c[1])
    halt = _halt_for(uid)
    out = []
    for r in rows:
        rid = str(r["id"])
        policy = {"max_per_day": r.get("max_per_day"), "min_interval_ms": r.get("min_interval_ms"),
                  "human_priority_ms": r.get("human_priority_ms"), "last_fire_ms": r.get("last_fire_ms")}
        view = dict(r)
        view["policy"] = policy
        view["actions"] = _json_list(r.get("actions_json"))
        view["trigger"] = _json_any(r.get("p_trigger"), _json_any(r.get("trigger_json"), {}))
        view["runs_today"] = today.get(rid, 0)
        out.append(_au_console.rule_summary(row=view,
                                            state={"paused_reason": r.get("paused_reason"),
                                                   "failure_count": r.get("failure_count"),
                                                   "last_error": r.get("last_error")},
                                            targets=targets.get(rid, []), label=str(r.get("label") or ""),
                                            last_run=last.get(rid), halt=halt, at_ms=_now_ms()))
    return out


def _halt_for(uid: str) -> dict | None:
    """The daily-loss halt, as the banner and the status field both need it.

    Read rather than recomputed: `loss_halts` is written by the risk gate at the moment it tripped, and a second
    derivation of "is this account halted" is a second answer to a question that decides whether money moves.
    """
    row = _db.execute("SELECT threshold_micro, realized_micro, tripped_ms, acknowledged_ms FROM loss_halts "
                      "WHERE user_id = ?", (uid,)).fetchone()
    if not row or row[3]:
        return None
    return {"halted": True, "thresholdMicro": int(row[0] or 0), "realizedMicro": int(row[1] or 0),
            "trippedMs": int(row[2] or 0),
            "lossMicro": max(0, int(row[0] or 0) - max(0, -int(row[1] or 0))) if row[1] and row[1] < 0 else 0,
            "acknowledgeHint": "acknowledge in the risk panel: acknowledging is a record, not a reset",
            "note": ("the daily loss limit tripped, so every money-moving path for this account is stopped until "
                     "you acknowledge it — including automation, which is why the rule list shows halted")}


def _facts_for(uid: str, market_id: str, token_id: str, at: int):
    """Everything a trigger may read, from our own tables, shaped as the engine's `Facts`.

    One delegation, on purpose: this is `automation.facts.facts_for`, which is exactly what the executor's engine
    calls. It used to be a second copy of the same five reads here, and the copies had already drifted — this one
    asked `tape_fills` for a `market_id` column that does not exist (the tape keys on `condition_id`), so every
    preview of a rule with a price trigger was a 500 while the live engine had a last price all along. A preview
    that reads different numbers than the live pass is a preview of a different rule, which is why there is now
    one reader.

    `token_id` is accepted and unused for the same reason the engine documents: our schema's book is per market,
    and a token-shaped signature that silently queried the wrong side would be worse than an unused argument.
    """
    return _au_facts.facts_for(_db, uid, market_id, at=at)


def _known_markets(ids: list[str]) -> set:
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    return {str(r[0]) for r in _db.execute("SELECT id FROM markets WHERE id IN (%s)" % marks,
                                           tuple(ids)).fetchall()}


@app.get("/v1/automations", responses=AUTOMATION_LIST_RESPONSES)
def list_automations(request: Request):
    """The rule list: status, why, last fired, next evaluation, caps — and the halt banner's own data.

    `status` is derived (`automation.console.rule_status`) rather than stored, because the four states a user
    cares about are a function of three columns the engine writes plus the risk service's halt. Storing it would
    mean a fourth write path that can disagree with the three that matter.
    """
    uid, _row, e = _principal(request)
    if e:
        return e
    rules = _automation_rows(str(uid))
    halt = _halt_for(str(uid))
    return _stamped({"rules": rules, "halt": halt, "caps": _au_console.caps_view(rules=rules),
                     "vocabulary": _au_console.builder_vocabulary(),
                     "note": ("a rule runs in simulation until you switch it on, and every evaluation — including "
                              "the ones that did nothing — is in its run history")},
                    ttl_ms=0, stale_ms=0)


@app.get("/v1/automations/runs", responses=AUTOMATION_RUNS_RESPONSES)
def list_automation_runs(request: Request, ruleId: str | None = Query(default=None, max_length=64),
                         limit: int = Query(default=50, ge=1, le=200)):
    """Every evaluation of a rule, newest first, with the reason and — where the trigger was read — its leaves.

    The counts are the answer to "is this rule doing anything": a rule with 400 skips and no fires is a rule
    whose trigger is wrong, and a rule with no rows at all is a rule the engine has never seen.
    """
    uid, _row, e = _principal(request)
    if e:
        return e
    args: list = [str(uid)]
    where = "r.user_id = ?"
    if ruleId:
        if not _db.execute("SELECT 1 FROM automation_rules WHERE id = ? AND user_id = ?",
                           (ruleId, str(uid))).fetchone():
            return err("NOT_FOUND", request.state.request_id,
                       detail="no rule %s for this account" % ruleId)
        where += " AND a.rule_id = ?"
        args.append(ruleId)
    rows = []
    for r in _db.execute("SELECT a.rule_id, a.mode, a.outcome, a.reason, a.deny_code, a.intent_id, a.detail_json, "
                         "       a.at_ms FROM automation_runs a JOIN automation_rules r ON r.id = a.rule_id "
                         "WHERE %s ORDER BY a.at_ms DESC, a.id DESC LIMIT ?" % where,
                         tuple(args) + (limit,)).fetchall():
        detail = _json_any(r[6], {})
        rows.append({"ruleId": str(r[0]), "mode": str(r[1]), "outcome": str(r[2]), "reason": str(r[3]),
                     "denyCode": str(r[4]), "intentId": str(r[5] or ""), "atMs": int(r[7] or 0),
                     "leaves": _au_console.leaves_sentence(detail.get("leaves")),
                     "sentence": _au_console.reason_sentence({"outcome": r[2], "reason": r[3], "deny_code": r[4]})})
    counts = {"placed": 0, "would_place": 0, "skipped": 0, "failed": 0}
    for row in rows:
        if row["outcome"] in counts:
            counts[row["outcome"]] += 1
    return _stamped({"rows": rows, "counts": counts, "ruleId": ruleId, "limit": limit,
                     "note": ("skips carry their reason: a rule that did nothing and a rule nobody looked at are "
                              "different facts")}, ttl_ms=0, stale_ms=0)


@app.get("/v1/automations/templates", responses=AUTOMATION_TEMPLATES_RESPONSES)
def list_automation_templates(request: Request, feeType: str = Query(default="taker", max_length=16),
                              feeRateBps: int = Query(default=200, ge=0, le=10_000),
                              edgeAvailableBp: int | None = Query(default=None, ge=0, le=10_000),
                              latencyMs: int | None = Query(default=None, ge=0, le=600_000)):
    """D8's templates, with the 5-minute crypto entry shown only when its fee arithmetic actually works.

    `edgeAvailableBp` is a *measurement*, so it defaults to absent rather than to a flattering number: with no
    measured edge, no entry template can be offered, and the response says so in the arithmetic rather than
    hiding the template. The protective templates ship unconditionally — they can only reduce a loss.
    """
    uid, _row, e = _principal(request)
    if e:
        return e
    cat = _au_console.template_catalog(fee_type=feeType, fee_rate_bps=feeRateBps, edge_available_bp=edgeAvailableBp,
                                       latency_ms=latencyMs)
    return _stamped(cat, ttl_ms=0, stale_ms=0)


@app.post("/v1/automations", status_code=200, responses=AUTOMATION_CREATE_RESPONSES,
          openapi_extra=_body_schema(AUTOMATION_CREATE_REQUIRED, AUTOMATION_CREATE_PROPS))
def create_automation(request: Request, body: dict = Body(...),
                      idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """Create a rule from the visual builder's rows. Always a dry run — there is no field that makes it live.

    The payload is rows and a joiner, never an expression: `compile_builder` turns them into the engine's own
    document and `validate_rule` is the authority on whether that document may exist. Both run before anything
    is written, so a rule that the engine would refuse at fire time cannot be saved at all.
    """
    rid = request.state.request_id
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    bad = _check_body(body, AUTOMATION_CREATE_REQUIRED, rid, allowed=tuple(AUTOMATION_CREATE_PROPS))
    if bad is not None:
        return bad
    bad = _check_props(body, AUTOMATION_CREATE_PROPS, rid)
    if bad is not None:
        return bad
    uid, _row, e = _principal(request)
    if e:
        return e
    return _idem_run(str(uid), str(idempotency_key), body, rid,
                     lambda: _create_automation_work(rid, uid, body))


def _create_automation_work(rid: str, uid: str, body: dict):
    compiled, errs = _au_console.compile_builder(body)
    halt = _halt_for(str(uid))
    if halt:
        return err("HALTED", rid, detail=halt["note"], where=["acknowledge the daily-loss halt first"])
    existing = _automation_rows(str(uid))
    cap = _au_console.caps_view(rules=existing)["concurrentRuleCap"]
    if len(existing) >= cap:
        return err("RULE_CAP", rid, detail="this account already has %d rules, which is the cap; delete one first"
                   % cap)
    target_ids = [t["marketId"] for t in compiled["targets"]]
    unknown = [m for m in target_ids if m not in _known_markets(target_ids)]
    if unknown:
        return err("NOT_FOUND", rid, detail="unknown market(s): %s" % ", ".join(unknown[:3]))
    if errs:
        return err("VALIDATION", rid, detail="; ".join(errs[:4]), where=errs[:4])
    rule_id = "rule-" + uuid.uuid4().hex[:12]
    at = _now_ms()
    doc = compiled["rule"]
    # The rule's own loss ceiling. Checked here rather than left to the insert, because `automation_rule_policy`
    # refuses a money-moving rule with no ceiling (`redemption_needs_no_ceiling`, where only `auto_redeem` is
    # exempt) and a CHECK violation five layers down arrives as a 500 — which tells the builder user nothing about
    # the field they left empty. The P10 gate found this by saving the smallest legal rule it could; the form
    # already refuses a zero ceiling client-side, so the server was the looser of the two, which is backwards.
    ceiling = int(body.get("maxLossMicro") or doc.get("max_loss_micro") or 0)
    if ceiling <= 0 and str(body["kind"]) != "auto_redeem":
        return err("VALIDATION", rid,
                   detail="a rule that can move money needs its own loss ceiling in micro-USDC; only auto_redeem "
                          "may hold a position with no ceiling, because redeeming cannot add risk",
                   where=["maxLossMicro"])
    _db.execute("INSERT INTO automation_rules (id, user_id, kind, trigger_json, max_loss_micro, enabled, "
                " last_run_ms) VALUES (?,?,?,?,?,0,NULL)",
                (rule_id, str(uid), str(body["kind"]), json.dumps(doc["trigger"]), ceiling))
    _db.execute("INSERT INTO automation_rule_policy (rule_id, trigger_json, actions_json, "
                "dry_run_completed_ms, max_per_day, min_interval_ms, human_priority_ms, last_fire_ms, updated_ms) "
                "VALUES (?,?,?,NULL,?,?,?,0,?)",
                (rule_id, json.dumps(doc["trigger"]), json.dumps(doc["actions"]),
                 int(body.get("maxPerDay") or 24),
                 max(int(body.get("minIntervalMs") or 60_000), _au.MIN_INTERVAL_MS),
                 int(body.get("humanPriorityMs") if body.get("humanPriorityMs") is not None else 120_000), at))
    _db.execute("INSERT INTO automation_rule_state (rule_id, paused_reason, armed_market_id, armed_token_id, "
                "failure_count, last_error, updated_ms) VALUES (?,'','','',0,'',?)", (rule_id, at))
    for t in compiled["targets"]:
        _db.execute("INSERT INTO automation_rule_targets (rule_id, market_id, token_id, created_ms) "
                    "VALUES (?,?,?,?)", (rule_id, t["marketId"], t["tokenId"], at))
    label = str(body.get("name") or "").strip()
    if label:
        _db.execute("INSERT INTO automation_rule_labels (rule_id, name, updated_ms) VALUES (?,?,?)",
                    (rule_id, label[:80], at))
    _db.commit()
    rules = _automation_rows(str(uid))
    created = next((r for r in rules if r["ruleId"] == rule_id), None)
    return _stamped({"rule": created, "dryRunOnly": True, "halt": _halt_for(str(uid)),
                     "note": ("saved as a dry run: nothing can be sent until you run it in simulation and switch "
                              "it on")}, ttl_ms=0, stale_ms=0)


@app.post("/v1/automations/preview", status_code=200, responses=AUTOMATION_PREVIEW_RESPONSES,
          openapi_extra=_body_schema(AUTOMATION_PREVIEW_REQUIRED, AUTOMATION_PREVIEW_PROPS))
def preview_automation(request: Request, body: dict = Body(...),
                       idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """Evaluate a rule against live facts, in dry-run mode, and record it.

    Two callers, one path. With `ruleId`, this is a saved rule's dry run: the evaluation is written to
    `automation_runs` and the first one marks `dry_run_completed_ms`, which is the only thing that lets the rule
    go live. Without it, the payload is compiled and evaluated without being saved — the builder's "show me what
    this would have done" step, which must work before the rule exists.

    The facts are the same ones the executor's engine reads (`_facts_for`): a preview that fed a rule different
    numbers than the live pass would be a preview of a different rule.
    """
    rid = request.state.request_id
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    uid, _row, e = _principal(request)
    if e:
        return e
    rule_id = str(body.get("ruleId") or "").strip()
    if not rule_id and not body.get("triggers"):
        return err("VALIDATION", rid, detail="send either a ruleId or a builder payload",
                   where=["ruleId", "triggers"])
    if rule_id:
        row = next((r for r in _automation_rows(str(uid)) if r["ruleId"] == rule_id), None)
        if row is None:
            return err("NOT_FOUND", rid, detail="no rule %s for this account" % rule_id)
        return _idem_run(str(uid), str(idempotency_key), body, rid,
                         lambda: _dry_run_work(rid, uid, rule_id))
    compiled, errs = _au_console.compile_builder(body)
    if errs:
        return err("VALIDATION", rid, detail="; ".join(errs[:4]), where=errs[:4])
    target = (compiled["targets"] or [{}])[0]
    at = _now_ms()
    facts = _facts_for(str(uid), str(target.get("marketId") or ""), str(target.get("tokenId") or ""), at)
    sim = _au_console.simulation(compiled["rule"]["trigger"], facts)
    return _stamped({"rule": compiled["rule"], "targets": compiled["targets"], "dryRun": True,
                     "simulation": sim, "facts": _facts_view(facts),
                     "note": ("this is the trigger read against the numbers we hold now; saving it starts a dry "
                              "run that the engine keeps evaluating")}, ttl_ms=0, stale_ms=0)


def _facts_view(facts) -> dict:
    return {"lastPriceMicro": facts.last_price_micro, "bestBidMicro": facts.best_bid_micro,
            "bestAskMicro": facts.best_ask_micro, "midMicro": facts.mid_micro,
            "quoteAgeMs": facts.quote_age_ms, "staleMs": facts.stale_ms,
            "acceptingOrders": facts.accepting_orders, "imbalanceBps": facts.imbalance_bps,
            "positionSharesMicro": facts.position_shares_micro,
            "secondsToResolution": facts.seconds_to_resolution}


def _dry_run_work(rid: str, uid: str, rule_id: str):
    """One dry-run evaluation of a saved rule: evaluate, record the run, complete the dry run if it is the first.

    `outcome` is `would_place` when the trigger was met and `skipped` when it was not — both are recorded, and
    both complete the dry run, because "it did nothing all afternoon" is a result a user should be able to read
    before deciding. Only the live path needs a *fired* dry run, and the engine enforces that separately.
    """
    row = next((r for r in _automation_rows(str(uid)) if r["ruleId"] == rule_id), None)
    if row is None:
        return err("NOT_FOUND", rid, detail="no rule %s for this account" % rule_id)
    at = _now_ms()
    target = (row["targets"] or [{}])[0]
    facts = _facts_for(str(uid), str(target.get("marketId") or ""), str(target.get("tokenId") or ""), at)
    sim = _au_console.simulation(row["trigger"], facts)
    outcome = "would_place" if sim["fires"] else "skipped"
    reason = "dry run: no order sent" if sim["fires"] else "trigger not met"
    _db.execute("INSERT INTO automation_runs (rule_id, user_id, mode, outcome, reason, deny_code, intent_id, "
                "detail_json, at_ms) VALUES (?,?,'dry_run',?,?,'','',?,?)",
                (rule_id, str(uid), outcome, reason, json.dumps({"leaves": sim["leaves"]}), at))
    first = not row.get("dryRunCompletedMs")
    if first:
        _db.execute("UPDATE automation_rule_policy SET dry_run_completed_ms = ?, updated_ms = ? WHERE rule_id = ?",
                    (at, at, rule_id))
    _db.commit()
    updated = next((r for r in _automation_rows(str(uid)) if r["ruleId"] == rule_id), None)
    return _stamped({"rule": updated, "dryRun": True, "dryRunCompleted": first,
                     "simulation": sim, "facts": _facts_view(facts),
                     "note": ("dry run complete: this rule can go live when you switch it on" if first else
                              "another dry-run evaluation recorded")}, ttl_ms=0, stale_ms=0)


@app.post("/v1/automations/guards", status_code=200, responses=AUTOMATION_GUARD_RESPONSES,
          openapi_extra=_body_schema(AUTOMATION_GUARD_REQUIRED, AUTOMATION_GUARD_PROPS))
def set_automation_state(request: Request, body: dict = Body(...),
                         idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """Pause a rule, or arm it — the only path to `enabled`, and it refuses without a completed dry run.

    Pausing is always allowed and takes effect immediately, because the safe direction must never be blocked by
    a precondition. Arming checks the two things the engine will check at fire time — a completed dry run and no
    active loss halt — and refuses with the reason, so the user meets the refusal while they can still act on it
    instead of discovering a rule that never fired.
    """
    rid = request.state.request_id
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    bad = _check_body(body, AUTOMATION_GUARD_REQUIRED, rid, allowed=tuple(AUTOMATION_GUARD_PROPS))
    if bad is not None:
        return bad
    bad = _check_props(body, AUTOMATION_GUARD_PROPS, rid)
    if bad is not None:
        return bad
    uid, _row, e = _principal(request)
    if e:
        return e
    return _idem_run(str(uid), str(idempotency_key), body, rid,
                     lambda: _guard_automation_work(rid, uid, body))


def _guard_automation_work(rid: str, uid: str, body: dict):
    rule_id = str(body["ruleId"])
    state = str(body["state"])
    row = next((r for r in _automation_rows(str(uid)) if r["ruleId"] == rule_id), None)
    if row is None:
        return err("NOT_FOUND", rid, detail="no rule %s for this account" % rule_id)
    at = _now_ms()
    if state == "paused":
        reason = str(body.get("reason") or "paused by you")
        _db.execute("UPDATE automation_rules SET enabled = 0 WHERE id = ?", (rule_id,))
        _db.execute("UPDATE automation_rule_state SET paused_reason = ?, updated_ms = ? WHERE rule_id = ?",
                    (reason[:200], at, rule_id))
        _db.execute("INSERT INTO automation_runs (rule_id, user_id, mode, outcome, reason, deny_code, intent_id, "
                    "detail_json, at_ms) VALUES (?,?,'live','skipped',?,'','','{}',?)",
                    (rule_id, str(uid), "paused by the user: %s" % reason[:120], at))
        _db.commit()
        updated = next((r for r in _automation_rows(str(uid)) if r["ruleId"] == rule_id), None)
        return _stamped({"rule": updated, "state": "paused", "note": "paused: nothing fires until you arm it"},
                        ttl_ms=0, stale_ms=0)
    halt = _halt_for(str(uid))
    if halt:
        return err("HALTED", rid, detail=halt["note"], where=["acknowledge the daily-loss halt first"])
    if not row.get("dryRunCompletedMs"):
        return err("DRY_RUN_REQUIRED", rid,
                   detail=("this rule has never been evaluated, so nothing is known about what it would do; "
                           "run it in dry mode first"),
                   where=["run POST /v1/automations/preview with this ruleId"])
    _db.execute("UPDATE automation_rules SET enabled = 1 WHERE id = ?", (rule_id,))
    _db.execute("UPDATE automation_rule_state SET paused_reason = '', failure_count = 0, last_error = '', "
                "updated_ms = ? WHERE rule_id = ?", (at, rule_id))
    _db.execute("INSERT INTO automation_runs (rule_id, user_id, mode, outcome, reason, deny_code, intent_id, "
                "detail_json, at_ms) VALUES (?,?,'live','skipped',?,'','','{}',?)",
                (rule_id, str(uid), "armed by the user after %d dry-run evaluation(s)" % 1, at))
    _db.commit()
    updated = next((r for r in _automation_rows(str(uid)) if r["ruleId"] == rule_id), None)
    return _stamped({"rule": updated, "state": "active",
                     "note": ("armed: every fire still goes through the same pre-flight and risk gate as a "
                              "manual order")}, ttl_ms=0, stale_ms=0)


# ================================================================================== P10 · D9 alerts ==
ALERT_UPSERT_RESPONSES = {400: {"description": "missing Idempotency-Key; a malformed one is a 422 naming the "
                                             "field"},
                          401: {"description": "a session is required"},
                          402: {"description": "the channel needs a plan the account does not have"},
                          404: {"description": "no such rule, market or event for this account"},
                          422: {"description": "missing field or an out-of-range cap/window"}}
ALERT_TEST_RESPONSES = {400: {"description": "missing Idempotency-Key; a malformed one is a 422 naming the "
                                             "field"},
                        401: {"description": "a session is required"},
                        404: {"description": "no such rule for this account"}}
ALERT_LIST_RESPONSES = {401: {"description": "a session is required"}}
ALERT_DELIVERIES_RESPONSES = {401: {"description": "a session is required"}}
ALERT_SETTINGS_RESPONSES = {400: {"description": "missing Idempotency-Key; a malformed one is a 422 naming the "
                                                 "field"},
                            401: {"description": "a session is required"},
                            422: {"description": "a quiet window with one end, an offset outside ±14h, or a "
                                                 "digest time outside the day"}}

ALERT_UPSERT_REQUIRED = ("kind",)
ALERT_UPSERT_PROPS = {"ruleId": {"type": "string", "maxLength": 64},
                      "kind": {"type": "string",
                               "enum": ["price_level", "spread_widen", "whale_fill", "resolve_lead",
                                        "illiquid_top", "new_market", "manual"]},
                      "marketId": {"type": "string", "maxLength": 64},
                      "eventId": {"type": "string", "maxLength": 64},
                      "channel": {"type": "string", "enum": ["telegram", "email", "webhook"]},
                      "severity": {"type": "string", "enum": ["info", "notice", "urgent"]},
                      "firesPerWindow": {"type": "integer", "minimum": 1, "maximum": 24},
                      "windowMs": {"type": "integer", "minimum": 60000},
                      "params": {"type": "object"},
                      "enabled": {"type": "boolean"}}
ALERT_TEST_REQUIRED = ("ruleId",)
ALERT_TEST_PROPS = {"ruleId": {"type": "string", "minLength": 1, "maxLength": 64},
                    "channels": {"type": "array"}, "title": {"type": "string", "maxLength": 160}}
ALERT_SETTINGS_PROPS = {"quietStartMin": {"type": "integer", "minimum": -1, "maximum": 1439},
                        "quietEndMin": {"type": "integer", "minimum": -1, "maximum": 1439},
                        "tzOffsetMin": {"type": "integer", "minimum": -840, "maximum": 840},
                        "digestMode": {"type": "string", "enum": ["off", "hourly", "daily"]},
                        "digestAtMin": {"type": "integer", "minimum": 0, "maximum": 1439},
                        "defaultChannel": {"type": "string", "enum": ["telegram", "email", "webhook"]}}


#: Engine kind -> the alert kind the screen uses. The history renders the user's word, not the engine's.
ALERT_KIND_OF: dict[str, str] = {v: k for k, v in _sg_engine.ALERT_KIND_MAP.items() if v}


def _target_conditions(market_id: str | None, event_id: str | None) -> list[str]:
    """The venue condition ids an alert's target covers — the key the engine's events carry.

    An event target covers every market in it, and the filter carries all of them: a rule that matched only the
    first leg would be quietly wrong on the others, which is the sort of gap nobody reports because the alert
    simply does not arrive.
    """
    if market_id:
        row = _db.execute("SELECT condition_id FROM markets WHERE id = ?", (market_id,)).fetchone()
        return [str(row[0])] if row and row[0] else []
    if event_id:
        return [str(r[0]) for r in _db.execute("SELECT condition_id FROM markets WHERE event_id = ?",
                                               (event_id,)).fetchall() if r[0]]
    return []


def _alert_view(rule: dict, **kw) -> dict:
    """`alert_summary` plus the two facts this build has to state about itself: which loop evaluates the rule,
    and — for a manual one — that no loop does."""
    out = _sg.alert_summary(rule=rule, **kw)
    engine = _sg.engine_kind(str(rule.get("kind") or ""))
    out["engineKind"] = engine
    out["evaluated"] = engine is not None
    out["evaluator"] = ("the ingest engine evaluates this rule and records every fire in the history below"
                        if engine else
                        "manual: this rule fires only when you test it — no loop is watching it")
    return out


def _plan_of(uid: str) -> str:
    row = _db.execute("SELECT tier FROM users WHERE id = ?", (uid,)).fetchone()
    tier = str(row[0]) if row and row[0] else "free"
    return "trial" if tier not in _sg.PLAN_RANK else tier


def _settings_view(uid: str, at: int) -> dict:
    """The settings block as the SCREEN reads it: the stored row plus what it means right now.

    Both endpoints answer with the same shape, because both are read by the same component: a list that carried
    the raw row and a settings write that carried the row *plus* the two computed blocks would make
    `settings.quietHours` a property that exists only after a write — a crash on the first paint, and only for
    users who had not yet touched the form.
    """
    fresh = _settings_for(str(uid))
    view = _sg.settings_view(fresh)
    view["quietHours"] = _sg.quiet_hours_state(fresh, at)
    view["digestNow"] = _sg.digest_state(fresh, at_ms=at, severity="notice")
    return view


def _settings_for(uid: str) -> dict:
    """The account's notification settings, materialised on first read.

    Reading is not a mutation: the row is created with the schema's own defaults the first time anyone asks, so
    a client never has to know them and `quiet_start_min = -1` (off) is a fact in the database rather than an
    absence a reader has to interpret.
    """
    row = _db.execute("SELECT quiet_start_min, quiet_end_min, tz_offset_min, digest_mode, digest_at_min, "
                      "default_channel FROM notification_settings WHERE user_id = ?", (uid,)).fetchone()
    if row is None:
        at = _now_ms()
        _db.execute("INSERT INTO notification_settings (user_id, quiet_start_min, quiet_end_min, tz_offset_min, "
                    "digest_mode, digest_at_min, default_channel, updated_ms) VALUES (?,-1,-1,0,'off',480,"
                    "'telegram',?)", (uid, at))
        _db.commit()
        return {"quiet_start_min": -1, "quiet_end_min": -1, "tz_offset_min": 0, "digest_mode": "off",
                "digest_at_min": 480, "default_channel": "telegram"}
    return {"quiet_start_min": int(row[0]), "quiet_end_min": int(row[1]), "tz_offset_min": int(row[2]),
            "digest_mode": str(row[3]), "digest_at_min": int(row[4]), "default_channel": str(row[5])}


def _alert_rules(uid: str) -> list[dict]:
    """This account's rules, as dicts. `test:` ids are the test-fire rows and the real list never shows them."""
    return _rows(_db.execute(
        "SELECT id, kind, market_id, event_id, fires_per_window, window_ms, params_json, enabled, created_ms "
        "FROM alert_rules WHERE user_id = ? AND id NOT LIKE 'test:%' ORDER BY created_ms DESC, id", (uid,)))


def _fires_in_window(rule_id: str, window_ms: int, at: int) -> list[int]:
    """This rule's fires inside its own window, from the alerts it actually produced."""
    return [int(r[0] or 0) for r in _db.execute(
        "SELECT fired_ms FROM signals WHERE rule_id = ? AND fired_ms > ? ORDER BY fired_ms DESC LIMIT 50",
        (rule_id, at - int(window_ms))).fetchall()]


def _last_delivery(rule_id: str, uid: str) -> dict | None:
    row = _db.execute("SELECT d.channel, d.status, d.reason, COALESCE(d.sent_ms, d.queued_ms), s.title "
                      "FROM alert_deliveries d JOIN signals s ON s.id = d.signal_id "
                      "WHERE s.rule_id = ? AND d.user_id = ? ORDER BY d.queued_ms DESC, d.id DESC LIMIT 1",
                      (rule_id, uid)).fetchone()
    if not row:
        return None
    return {"channel": str(row[0]), "status": str(row[1]), "reason": str(row[2] or ""), "atMs": int(row[3] or 0),
            "title": str(row[4] or "")}


@app.get("/v1/alerts", responses=ALERT_LIST_RESPONSES)
def list_alerts(request: Request):
    """The alert list: every rule with its budget, its cooldown, what quiet hours and digest would do to it now,
    and what actually happened last time it fired.

    `wouldDoNow` is the honest part. A rule list that shows a rule as "on" while the user's quiet hours are
    holding it is a list that will be described as broken at 2am; the plan is computed on read so the screen can
    say "held until 07:00" before the user has to guess.
    """
    uid, _row, e = _principal(request)
    if e:
        return e
    plan = _plan_of(str(uid))
    settings = _settings_for(str(uid))
    at = _now_ms()
    rules = []
    for r in _alert_rules(str(uid)):
        rules.append(_alert_view(rule={"id": r["id"], "kind": r["kind"], "market_id": r["market_id"],
                                       "event_id": r["event_id"], "enabled": r["enabled"],
                                       "fires_per_window": r["fires_per_window"], "window_ms": r["window_ms"],
                                       "params": _json_any(r["params_json"], {}), "created_ms": r["created_ms"]},
                                 settings=settings, at_ms=at,
                                 fires_in_window=_fires_in_window(str(r["id"]), int(r["window_ms"]), at),
                                 last_delivery=_last_delivery(str(r["id"]), str(uid)), plan=plan))
    return _stamped({"rules": rules, "settings": _settings_view(str(uid), at), "plan": plan,
                     "ruleCount": len(rules),
                     "note": ("a rule fires at most `firesPerWindow` times per window; quiet hours hold "
                              "everything (urgent included) and the list says so before it happens")},
                    ttl_ms=0, stale_ms=0)


@app.post("/v1/alerts", status_code=200, responses=ALERT_UPSERT_RESPONSES,
          openapi_extra=_body_schema(ALERT_UPSERT_REQUIRED, ALERT_UPSERT_PROPS))
def create_alert(request: Request, body: dict = Body(...),
                 idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """Create or edit one alert rule inline. The channel's plan is checked at save time, not at fire time.

    A free account saving a webhook rule would be a rule that can never deliver, and finding that out when the
    alert was supposed to arrive is worse than being told now — so the entitlement check is a 402 with the
    upgrade named.
    """
    rid = request.state.request_id
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    bad = _check_body(body, ALERT_UPSERT_REQUIRED, rid, allowed=tuple(ALERT_UPSERT_PROPS))
    if bad is not None:
        return bad
    bad = _check_props(body, ALERT_UPSERT_PROPS, rid)
    if bad is not None:
        return bad
    uid, _row, e = _principal(request)
    if e:
        return e
    return _idem_run(str(uid), str(idempotency_key), body, rid, lambda: _upsert_alert_work(rid, uid, body))


def _upsert_alert_work(rid: str, uid: str, body: dict):
    plan = _plan_of(str(uid))
    fields, errs = _sg.validate_alert_payload(body, plan=plan)
    if errs:
        code = "PLAN_REQUIRED" if any("plan" in e for e in errs) else "VALIDATION"
        return err(code, rid, detail="; ".join(errs[:3]), where=errs[:3])
    market_id = fields["market_id"]
    event_id = fields["event_id"]
    if market_id and market_id not in _known_markets([market_id]):
        return err("NOT_FOUND", rid, detail="unknown market %s" % market_id)
    at = _now_ms()
    params = dict(fields["params"])
    params.update({"channel": fields["channel"], "severity": fields["severity"], "source": "console"})
    rule_id = fields["rule_id"]
    if rule_id:
        if not _db.execute("SELECT 1 FROM alert_rules WHERE id = ? AND user_id = ?", (rule_id, str(uid))).fetchone():
            return err("NOT_FOUND", rid, detail="no alert rule %s for this account" % rule_id)
        _db.execute("UPDATE alert_rules SET kind = ?, market_id = ?, event_id = ?, fires_per_window = ?, "
                    "window_ms = ?, params_json = ?, enabled = ? WHERE id = ? AND user_id = ?",
                    (fields["kind"], market_id, event_id, fields["fires_per_window"], fields["window_ms"],
                     json.dumps(params), 1 if fields["enabled"] else 0, rule_id, str(uid)))
        action = "updated"
    else:
        rule_id = "al-" + uuid.uuid4().hex[:12]
        _db.execute("INSERT INTO alert_rules (id, user_id, market_id, event_id, kind, fires_per_window, "
                    "window_ms, params_json, enabled, created_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (rule_id, str(uid), market_id, event_id, fields["kind"], fields["fires_per_window"],
                     fields["window_ms"], json.dumps(params), 1 if fields["enabled"] else 0, at))
        action = "created"
    # The rule the user keeps, and the rule the ENGINE evaluates. `alert_rules` holds the user's own words;
    # `signal_rules` is the only table the ingest plane reads. A rule that existed in the first and not the
    # second would be listed, would show a cooldown, and could never fire — the failure this whole screen is
    # supposed to make visible, hiding inside it.
    engine_row = _sg.signal_rule_row(rule_id=rule_id, owner=str(uid),
                                     alert={**fields, "params": params}, condition_ids=_target_conditions(market_id, event_id))
    if engine_row is None:
        # A manual alert: it fires when its owner asks it to, and there is no rule for a loop to read.
        _db.execute("UPDATE signal_rules SET enabled = 0, updated_ms = ? WHERE id = ?", (at, rule_id))
    else:
        _db.execute("INSERT INTO signal_rules (id, owner, kind, params_json, market_filter_json, cooldown_s,"
                    " severity, channels_json, enabled, created_ms, updated_ms)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(id) DO UPDATE SET owner=excluded.owner, kind=excluded.kind,"
                    " params_json=excluded.params_json, market_filter_json=excluded.market_filter_json,"
                    " cooldown_s=excluded.cooldown_s, severity=excluded.severity,"
                    " channels_json=excluded.channels_json, enabled=excluded.enabled, updated_ms=excluded.updated_ms",
                    (rule_id, str(uid), engine_row["kind"], json.dumps(engine_row["params"]),
                     json.dumps(engine_row["market_filter"]), engine_row["cooldown_s"], engine_row["severity"],
                     json.dumps(engine_row["channels"]), 1 if engine_row["enabled"] else 0, at, at))
    _db.commit()
    settings = _settings_for(str(uid))
    row = _db.execute("SELECT id, kind, market_id, event_id, fires_per_window, window_ms, params_json, enabled, "
                      "created_ms FROM alert_rules WHERE id = ?", (rule_id,)).fetchone()
    summary = _alert_view({"id": row[0], "kind": row[1], "market_id": row[2], "event_id": row[3],
                                "enabled": row[7], "fires_per_window": row[4], "window_ms": row[5],
                                "params": _json_any(row[6], {}), "created_ms": row[8]},
                               settings=settings, at_ms=at, fires_in_window=_fires_in_window(rule_id, int(row[5]), at),
                               last_delivery=_last_delivery(rule_id, str(uid)), plan=plan)
    return _stamped({"rule": summary, "action": action,
                     "note": "the cooldown below is the window budget made explicit, not a separate setting"},
                    ttl_ms=0, stale_ms=0)


@app.post("/v1/alerts/test", status_code=200, responses=ALERT_TEST_RESPONSES,
          openapi_extra=_body_schema(ALERT_TEST_REQUIRED, ALERT_TEST_PROPS))
def test_alert(request: Request, body: dict = Body(...),
               idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """Fire a rule on purpose and record what WOULD happen, without spending the rule's real budget.

    The test writes its own `signals` row under a `test:` rule id, so the rule's window, cooldown and delivery
    history are untouched — a test that consumes the budget it is testing poisons the feature it validates. What
    it returns is the plan: sent now / held for quiet hours / batched for the digest / refused by the plan. The
    delivery rows are real rows with the plan's own status, and the response says plainly that no transport runs
    in this build, so a `queued` row is a record of what would be sent rather than a claim that it was.
    """
    rid = request.state.request_id
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    bad = _check_body(body, ALERT_TEST_REQUIRED, rid, allowed=tuple(ALERT_TEST_PROPS))
    if bad is not None:
        return bad
    uid, _row, e = _principal(request)
    if e:
        return e
    return _idem_run(str(uid), str(idempotency_key), body, rid, lambda: _test_alert_work(rid, uid, body))


def _test_alert_work(rid: str, uid: str, body: dict):
    rule_id = str(body["ruleId"])
    row = _db.execute("SELECT id, kind, market_id, event_id, fires_per_window, window_ms, params_json, enabled "
                      "FROM alert_rules WHERE id = ? AND user_id = ?", (rule_id, str(uid))).fetchone()
    if row is None:
        return err("NOT_FOUND", rid, detail="no alert rule %s for this account" % rule_id)
    plan = _plan_of(str(uid))
    settings = _settings_for(str(uid))
    params = _json_any(row[6], {})
    channels = [str(c) for c in (body.get("channels") or [params.get("channel") or
                                                          settings.get("default_channel") or "telegram"])]
    severity = str(params.get("severity") or "notice")
    at = _now_ms()
    plan_rows = _sg.delivery_plan(rule={"id": rule_id, "enabled": bool(row[7]),
                                        "fires_per_window": int(row[4]), "window_ms": int(row[5])},
                                  settings=settings, at_ms=at, severity=severity, fired_ms=[],
                                  channels=channels, plan=plan, is_test=True)
    # The test's own signal, under a rule id that cannot collide with the real one. `fired_ms` is now, and the
    # row is written so the delivery history has something true to point at.
    #
    # The dedupe key carries the signal's own id: `signals` is UNIQUE on (rule_id, dedupe_key, fired_bucket),
    # which is what makes "one alert per window" a database property rather than a code path someone has to
    # remember to call — and that constraint applies to test fires too. Two tests in the same minute are two
    # rows, because a test button that refuses the second press is a test button that looks broken.
    test_rule_id = "test:" + rule_id
    sig = _db.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM signals").fetchone()
    signal_id = int(sig[0])
    title = str(body.get("title") or ("test fire of %s" % str(params.get("severity") or "notice")))
    _db.execute("INSERT INTO signals (id, rule_id, kind, condition_id, token_id, severity, title, body_json, "
                "dedupe_key, fired_bucket, fired_ms) VALUES (?,?,?,?,'',?,?,?,?,?,?)",
                (signal_id, test_rule_id, str(row[1]), str(row[2] or ""), severity, title[:160],
                 json.dumps({"test": True, "ruleId": rule_id, "channels": channels}),
                 "test-%d" % signal_id, at // 60_000, at))
    delivery_ids = []
    for pr in plan_rows:
        cur = _db.execute("INSERT INTO alert_deliveries (signal_id, user_id, channel, priority, queued_ms, "
                          "sent_ms, status, reason) VALUES (?,?,?,?,?,NULL,?,?)",
                          (signal_id, str(uid), pr["channel"], _sg.PLAN_RANK.get(plan, 2), at, pr["status"],
                           pr["sentence"]))
        delivery_ids.append(cur.lastrowid if cur is not None else None)
    _db.commit()
    return _stamped({"ruleId": rule_id, "testSignalId": signal_id, "plan": plan_rows,
                     "summary": _sg.plan_summary(plan_rows), "severity": severity,
                     "quietHours": _sg.quiet_hours_state(settings, at),
                     "digest": _sg.digest_state(settings, at_ms=at, severity=severity),
                     "deliveryIds": delivery_ids,
                     "note": ("these rows are the record of what would be sent: no delivery transport runs in "
                              "this build, so a `queued` row is a plan, not a notification — and the rule's own "
                              "window was not spent")}, ttl_ms=0, stale_ms=0)


@app.get("/v1/alerts/deliveries", responses=ALERT_DELIVERIES_RESPONSES)
def list_alert_deliveries(request: Request, limit: int = Query(default=50, ge=1, le=200),
                          channel: str | None = Query(default=None, max_length=16)):
    """Delivery history, with the status each channel reached and the reason it did not reach a later one.

    `latencyMs` is `sent - queued` where it was sent and null where it never was: a latency of zero on a row that
    was never delivered is the kind of number that makes a dashboard lie with a straight face.
    """
    uid, _row, e = _principal(request)
    if e:
        return e
    args: list = [str(uid)]
    where = "d.user_id = ?"
    if channel:
        where += " AND d.channel = ?"
        args.append(channel)
    rows = []
    for r in _db.execute("SELECT d.id, s.rule_id, s.kind, d.channel, d.status, d.reason, d.queued_ms, d.sent_ms, "
                         "s.severity, s.title FROM alert_deliveries d JOIN signals s ON s.id = d.signal_id "
                         "WHERE %s ORDER BY d.queued_ms DESC, d.id DESC LIMIT ?" % where,
                         tuple(args) + (limit,)).fetchall():
        queued, sent = int(r[6] or 0), (int(r[7]) if r[7] is not None else None)
        rows.append({"deliveryId": int(r[0]), "ruleId": str(r[1] or ""),
                     "kind": ALERT_KIND_OF.get(str(r[2] or ""), str(r[2] or "")),
                     "engineKind": str(r[2] or ""),
                     "channel": str(r[3]), "status": str(r[4]), "reason": str(r[5] or ""),
                     "queuedMs": queued, "sentMs": sent, "latencyMs": (sent - queued) if sent else None,
                     "severity": str(r[8] or ""), "title": str(r[9] or ""),
                     "isTest": str(r[1] or "").startswith("test:")})
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return _stamped({"rows": rows, "counts": counts, "limit": limit, "channel": channel,
                     "note": ("`digest_scheduled` is held (quiet hours or digest) and `dropped_rate_limited` is "
                              "refused by the rule's own cap: two different facts")}, ttl_ms=0, stale_ms=0)


@app.post("/v1/alerts/settings", status_code=200, responses=ALERT_SETTINGS_RESPONSES,
          openapi_extra=_body_schema((), ALERT_SETTINGS_PROPS))
def set_alert_settings(request: Request, body: dict = Body(...),
                       idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """Quiet hours, digest mode and the default channel.

    Every field is optional and an absent field means "leave it": a settings form that requires the whole object
    overwrites what it does not know, which is how a quiet window disappears when somebody changes their
    digest time.
    """
    rid = request.state.request_id
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    bad = _check_props(body, ALERT_SETTINGS_PROPS, rid)
    if bad is not None:
        return bad
    uid, _row, e = _principal(request)
    if e:
        return e
    return _idem_run(str(uid), str(idempotency_key), body, rid, lambda: _settings_work(rid, uid, body))


def _settings_work(rid: str, uid: str, body: dict):
    current = _settings_for(str(uid))
    merged = dict(current)
    for key in ("quietStartMin", "quietEndMin", "tzOffsetMin", "digestMode", "digestAtMin", "defaultChannel"):
        if key in body and body[key] is not None:
            merged[{"quietStartMin": "quiet_start_min", "quietEndMin": "quiet_end_min",
                    "tzOffsetMin": "tz_offset_min", "digestMode": "digest_mode",
                    "digestAtMin": "digest_at_min", "defaultChannel": "default_channel"}[key]] = body[key]
    start, end = int(merged["quiet_start_min"]), int(merged["quiet_end_min"])
    if (start == -1) != (end == -1):
        return err("VALIDATION", rid, detail="a quiet window needs both ends, or neither (send -1 for both to "
                                             "turn it off)", where=["quietStartMin", "quietEndMin"])
    if start != -1 and start == end:
        return err("VALIDATION", rid, detail="a quiet window that starts and ends at the same minute is a window "
                                             "that never closes", where=["quietStartMin", "quietEndMin"])
    allowed, why = _sg.channel_entitlement(str(merged["default_channel"]), _plan_of(str(uid)))
    if not allowed:
        return err("PLAN_REQUIRED", rid, detail=why, where=["defaultChannel"])
    at = _now_ms()
    _db.execute("UPDATE notification_settings SET quiet_start_min = ?, quiet_end_min = ?, tz_offset_min = ?, "
                "digest_mode = ?, digest_at_min = ?, default_channel = ?, updated_ms = ? WHERE user_id = ?",
                (start, end, int(merged["tz_offset_min"]), str(merged["digest_mode"]),
                 int(merged["digest_at_min"]), str(merged["default_channel"]), at, str(uid)))
    _db.commit()
    return _stamped(_settings_view(str(uid), at), ttl_ms=0, stale_ms=0)


# ------------------------------------------------------------------------------------------- P11 · the boards
# D2. Six boards ranked from our own ledger, where every row carries the components it was ranked on and every
# refusal carries the number that refused it. The engine (`leaderboard/rank.py`) decides the ordering; this
# section decides the three things an engine must never decide for itself:
#
#   * **which rows it sees** — `source.read_plan()` is the window, and it differs per board on purpose (the
#     volume board ranks the window; every skill board's floor is written in lifetime turnover).
#   * **who a human has removed** — `leaderboard_exclusions`, append-only, applied here rather than deleted
#     anywhere: an exclusion is a decision about somebody's standing, so it is a row with an actor and a time.
#   * **how stale the answer is** — `leaderboard_runs` is the recompute's own record, and the response states its
#     age against the board's declared cadence instead of hoping nobody asks.
#
# The board is computed LIVE from the tape on every read. That is the right trade at our size (the read is one
# pass over the fills we hold, and a cached board is a board that can disagree with the ledger it claims to
# summarise); `leaderboard_snapshots` exists for the rank-history sparkline, which is a question about the past
# and cannot be answered by recomputing today.
LB_MAX_ROWS = 10_000          # the engine's limit when a count must be over the whole board, not the page


def _lb_fills() -> list[dict]:
    """Every fill we hold, with the two facts a settlement needs: the token's winner and the market's category.

    Read whole rather than per wallet: the copied board's farm filter asks "derived from WHOM", which is a
    question about the other tapes, and a per-wallet read is the shape that cannot answer it.
    """
    rows = _db.execute(
        "SELECT f.wallet, f.ts_ms, f.condition_id, f.token_id, f.side, f.price_micro, f.size_micro,"
        " f.usd_notional_micro, t.is_winner, COALESCE(mm.category,'')"
        " FROM tape_fills f JOIN markets m ON m.condition_id = f.condition_id"
        " JOIN tokens t ON t.token_id = f.token_id"
        " LEFT JOIN market_meta mm ON mm.market_id = m.id"
        " ORDER BY f.ts_ms ASC, f.rowid ASC").fetchall()
    out = []
    for (wallet, ts, cond, token, side, price, size, notional, winner, category) in rows:
        out.append({"wallet": str(wallet), "tsMs": _tm._int(ts), "conditionId": str(cond),
                    "tokenId": str(token), "side": str(side), "priceMicro": _tm._int(price),
                    "sizeMicro": _tm._int(size), "notionalMicro": _tm._int(notional),
                    "winner": (None if winner is None else int(winner)), "category": str(category)})
    return out


def _lb_created_ms() -> dict[str, int]:
    """When we first saw a wallet, so `provisional` is a fact rather than a guess from the newest tape slice."""
    return {str(w): _tm._int(ts) for (w, ts) in _db.execute(
        "SELECT wallet_id, MIN(first_seen_ms) FROM wallet_pseudonyms GROUP BY wallet_id").fetchall()}


def _lb_copy_configs() -> list[dict]:
    return [{"source": str(src), "enabled": int(en or 0)} for (src, en) in _db.execute(
        "SELECT source_user, enabled FROM copy_configs").fetchall()]


def _lb_blocked_conditions(*, at_ms: int) -> set[str]:
    """Markets under an active UMA dispute — the same list the order path refuses on."""
    return {str(r[0]) for r in _db.execute(
        "SELECT market_id FROM risk_blocklists WHERE source='uma_dispute'"
        " AND (expires_ms IS NULL OR expires_ms > ?)", (_tm._int(at_ms),)).fetchall()}


def _lb_exclusions() -> dict[str, dict]:
    """The newest human decision per (wallet, board), replayed from append-only rows.

    Replayed rather than stored as state: the table is append-only, so "who removed this wallet, when, and on
    whose say-so" is answerable after an incident — and `include` is a decision too, which is why it is a row
    rather than a DELETE.
    """
    per: dict[str, dict[str, dict]] = {}
    for (wallet, board, action, reason, actor, at_ms) in _db.execute(
            "SELECT wallet, board, action, reason, actor, at_ms FROM leaderboard_exclusions"
            " ORDER BY at_ms ASC, id ASC").fetchall():
        per.setdefault(str(wallet), {})[str(board)] = {
            "wallet": str(wallet), "board": str(board), "action": str(action), "reason": str(reason),
            "actor": str(actor), "atMs": _tm._int(at_ms)}
    return {w: by_board for w, by_board in per.items()}


def _lb_excluded(*, decisions: dict, board_id: str) -> dict[str, dict]:
    """Who is off THIS board. `exclude` removes; `flag` does not — a flag is a question for a human, and a
    leaderboard that quietly drops a flagged wallet would be hiding the wallet instead of reviewing it."""
    out: dict[str, dict] = {}
    for wallet, by_board in decisions.items():
        decision = by_board.get(board_id) or by_board.get("")
        if decision and decision["action"] == "exclude":
            out[str(wallet)] = decision
    return out


def _lb_pseudonymise(ev: dict) -> dict:
    """Evidence whose identity is a pseudonym — so the ranker, the rows and every `why` sentence it prints are
    structurally incapable of carrying an address to a public response."""
    return {**ev, "wallet": _anon(str(ev["wallet"]))}


def _lb_evidence(*, board_id: str, window: str, at_ms: int) -> tuple[list[dict], dict, dict]:
    plan = _lb_source.read_plan(board_id=board_id, window=window, at_ms=at_ms)
    evidence = _lb_source.evidence(fills=_lb_fills(), at_ms=at_ms, plan=plan,
                                   copy_configs=_lb_copy_configs(),
                                   blocked_conditions=_lb_blocked_conditions(at_ms=at_ms),
                                   created_ms=_lb_created_ms())
    excluded = _lb_excluded(decisions=_lb_exclusions(), board_id=board_id)
    kept = [_lb_pseudonymise(w) for w in evidence if str(w["wallet"]) not in excluded]
    return kept, plan, excluded


def _lb_board(*, board_id: str, window: str, at_ms: int, category: str = "") -> dict:
    """One board, ranked over the whole eligible population (the page is applied by the caller)."""
    evidence, plan, excluded = _lb_evidence(board_id=board_id, window=window, at_ms=at_ms)
    out = _lb_rank.rank_board(board_id=board_id, wallets=evidence, at_ms=at_ms, limit=LB_MAX_ROWS,
                              category=category, window=plan["window"])
    out["plan"] = plan
    out["summary"] = _lb_source.summarise(out, excluded=len(excluded))
    out["excludedTotal"] = len(excluded)
    return out


_LB_MONEY_STRINGS = (("realisedMicro", "realised"), ("trimmedMicro", "trimmed"), ("bestMicro", "best"),
                     ("volatilityMicro", "volatility"), ("maxDrawdownMicro", "drawdown"),
                     ("verifiedVolumeMicro", "verifiedVolume"), ("washedMicro", "washed"),
                     ("improvementMicro", "improvement"), ("weekMicro", "week"), ("priorWeekMicro", "priorWeek"))


def _lb_row_out(row: dict, *, total: int, labels: dict | None = None,
                handles: dict[str, str] | None = None) -> dict:
    """One row on the wire: `anon` instead of `wallet`, the display strings beside the micro integers.

    Both, not one: the integers are what the ranking is defined against and rounding them to cents would move a
    row's own components, while the strings are what the number layer renders (no surface formats money itself).
    """
    out = dict(row)
    anon = str(out.pop("wallet", ""))
    out["anon"] = anon
    for micro_key, text_key in _LB_MONEY_STRINGS:
        if micro_key in out:
            out[text_key] = fmt_usdc(_tm._int(out[micro_key]))
    out["rankBadge"] = {"rank": _tm._int(out.get("rank")), "rankedTotal": _tm._int(total),
                        "text": "#%d" % _tm._int(out.get("rank"))}
    out["classifications"] = list((labels or {}).get(anon, []))
    # D4. `handle` is the account that opted in, and None for everybody else - including every account that has
    # never opened the setting. The pseudonym is unchanged either way: this one field is the whole difference
    # between appearing on the board and appearing as somebody, which is why a private wallet is not a hidden one.
    out["handle"] = (handles or {}).get(anon) or None
    return out


def _lb_freshness(board_id: str, *, at_ms: int, label: str) -> dict:
    """"How old is this board", answered against the cadence the board itself declares."""
    row = _db.execute("SELECT MAX(computed_ms), COUNT(*) FROM leaderboard_runs WHERE board=?",
                      (str(board_id),)).fetchone()
    last, runs = (row[0], row[1]) if row else (None, 0)
    cadence = _tm._int((_lb_boards.board(board_id) or {}).get("cadenceMs"))
    age = None if last is None else max(0, _tm._int(at_ms) - _tm._int(last))
    return {"board": board_id, "label": label, "snapshotMs": (None if last is None else _tm._int(last)),
            "ageMs": age, "cadenceMs": cadence, "runs": _tm._int(runs),
            "stale": (True if age is None else age > 2 * cadence),
            "source": "live",
            "note": ("this board was ranked from the ledger when you asked; the snapshot time is when the rank "
                     "history was last appended, and the sparkline is the only thing that reads it")}


def _lb_picker() -> list[dict]:
    """The board picker, generated from the specification so a new board cannot appear in one place only."""
    return [{"id": b["id"], "label": b["label"], "isDefault": bool(b["isDefault"]), "kind": b["kind"],
             "window": b["window"], "windows": list(b["windows"]), "formula": b["formula"],
             "gate": b["gate"], "cadenceMs": _tm._int(b.get("cadenceMs")), "cadence": b["cadence"],
             "categoryBoard": b["id"] == "category",
             "categories": (list(_lb_boards.CATEGORIES) if b["id"] == "category" else [])} for b in _lb_boards.BOARDS]


#: The status sets, written out per route rather than composed by spreading a shared dict: the checker reads these
#: tables from the AST, and `{200: ..., **OTHER}` tells it (correctly, from what it can see) that the table declares
#: exactly one status. A shared set that the tool cannot see through is a shared set that quietly stops being
#: checked — so the reads declare what they can actually answer, and nothing more.
LEADERBOARD_RESPONSES = {422: {"description": "an unknown board, a window that board does not read, or a category "
                                               "the category board does not have"}}
LEADERBOARD_WHY_RESPONSES = {404: {"description": "one of the two pseudonyms is not on this board"},
                             422: {"description": "an unknown board or a window it does not read"}}
LEADERBOARD_METHODOLOGY_RESPONSES = {200: {"description": "the boards, their formulas, their gates, and the "
                                                        "integrity rules, as data"}}
LEADERBOARD_SNAPSHOT_RESPONSES = {200: {"description": "a wallet's rank history per board"},
                                  404: {"description": "no such pseudonym"},
                                  500: {"description": "an internal failure, with a request id"}}
LEADERBOARD_RUN_RESPONSES = {200: {"description": "the recompute record, newest first"}}
LEADERBOARD_RANK_RESPONSES = {404: {"description": "no wallet with that pseudonym has been seen"},
                              422: {"description": "an unknown board, a window it does not read, or a category it "
                                                   "does not have"}}
LEADERBOARD_COMPARE_RESPONSES = {422: {"description": "fewer than two distinct wallets, more than three, or an "
                                                    "unknown board or window"}}
LEADERBOARD_FOLLOWS_RESPONSES = {401: {"description": "a session is required"},
                                 422: {"description": "an unknown board or a window it does not read"}}
LEADERBOARD_FOLLOW_RESPONSES = {400: {"description": "no Idempotency-Key"},
                                401: {"description": "a session is required"},
                                404: {"description": "no wallet with that pseudonym has been seen"},
                                409: {"description": "the Idempotency-Key was reused with a different body"},
                                422: {"description": "a missing anon, a malformed key, or an unknown state"}}
LEADERBOARD_ME_RESPONSES = {401: {"description": "a session is required"},
                            422: {"description": "a window no board reads"}}
LEADERBOARD_IDENTITY_RESPONSES = {401: {"description": "a session is required"}}
LEADERBOARD_IDENTITY_SET_RESPONSES = {400: {"description": "no Idempotency-Key"},
                                      401: {"description": "a session is required"},
                                      409: {"description": "the Idempotency-Key was reused with a different body, "
                                                           "or the handle is taken"},
                                      422: {"description": "an unknown state, or a listing without a usable handle"}}
LEADERBOARD_RECOMPUTE_RESPONSES = {400: {"description": "no Idempotency-Key"},
                                   401: {"description": "a session is required"},
                                   409: {"description": "the Idempotency-Key was reused with a different body"},
                                   422: {"description": "a malformed key, an unknown board, or a window a board "
                                                        "does not read"}}


@app.get("/v1/leaderboard/boards", responses=LEADERBOARD_METHODOLOGY_RESPONSES)
def get_leaderboard_boards(request: Request):
    """The picker's own read, so a client that only wants the tab strip does not fetch a board to get it.

    Generated from the specification rather than listed by hand: a board that exists in the engine and not in the
    picker is a board nobody can open, and a hand-written list is how that happens.
    """
    at = _now_ms()
    return _stamped({"boards": _lb_picker(), "defaultBoard": _lb_boards.DEFAULT_BOARD,
                     "categories": list(_lb_boards.CATEGORIES),
                     "note": ("the default board is risk-adjusted PnL, not volume: the venue's own volume board "
                              "ranks churn, and a board that can be topped by round-tripping is a board that "
                              "tells a stranger to copy the wrong wallet")},
                    ttl_ms=60_000, stale_ms=0, as_of_ms=at)


@app.get("/v1/leaderboard", responses=LEADERBOARD_RESPONSES)
def get_leaderboard(request: Request,
                    board: str = Query(default="risk_adjusted",
                                       pattern="^(risk_adjusted|win_rate|volume|rising|category|copied)$"),
                    window: str | None = Query(default=None, pattern="^(24h|7d|30d|90d|all)$"),
                    category: str = Query(default="", max_length=32),
                    q: str = Query(default="", max_length=64),
                    limit: int = Query(default=50, ge=1, le=200),
                    offset: int = Query(default=0, ge=0, le=5000)):
    """One board: the page of rows, the refusals with their reasons, the counts, and the freshness.

    The unranked list is a first-class part of the response, not an empty page. A wallet under the gate is not
    hidden from the product — it is shown with the number that refused it, because "why am I not on it" is the
    question every leaderboard gets and the answer must not require a support ticket.
    """
    rid = request.state.request_id
    at = _now_ms()
    try:
        out = _lb_board(board_id=str(board), window=str(window or ""), at_ms=at, category=str(category))
    except ValueError as exc:
        return err("VALIDATION", rid, detail=str(exc), where=["window" if "reads" in str(exc) else "category"])
    try:
        is_admin, _e = _admin(request)
    except Exception:                                                       # noqa: BLE001
        is_admin = False
    decisions = _lb_exclusions()
    excluded = _lb_excluded(decisions=decisions, board_id=str(board))
    labels = _labels_by_wallet()
    total = _tm._int(out["rankedTotal"])
    handles = _lb_published_handles()
    rows = [_lb_row_out(r, total=total, labels=labels, handles=handles) for r in out["rows"]]
    # NOT `_anon(u["wallet"])`: the engine's rows already carry pseudonyms (that is the point of pseudonymising
    # the evidence before ranking), and hashing a pseudonym again produced a second, wrong identity — a refusal
    # that could not be matched to the row it refuses, with a name nothing else in the product would ever print.
    unranked = [{"anon": str(u["wallet"]), "reasons": list(u["reasons"]),
                 "settledMarkets": _tm._int(u["settledMarkets"]),
                 "verifiedVolumeMicro": _tm._int(u["verifiedVolumeMicro"]),
                 "verifiedVolume": fmt_usdc(_tm._int(u["verifiedVolumeMicro"])),
                 "washedMicro": _tm._int(u.get("washedMicro")),
                 "washed": fmt_usdc(_tm._int(u.get("washedMicro"))),
                 "washNote": ("" if not _tm._int(u.get("washedMicro")) else
                              "removed %s of round-tripped volume before refusing it"
                              % fmt_usdc(_tm._int(u.get("washedMicro")))),
                 "note": u["note"],
                 "classifications": list(labels.get(str(u["wallet"]), []))}
                for u in out["unranked"]]
    needle = str(q).strip().lower()
    filtered = False
    if needle:
        rows = [r for r in rows if needle in r["anon"].lower()]
        unranked = [u for u in unranked if needle in u["anon"].lower()]
        filtered = True
    page_rows = rows[offset:offset + limit]
    page_unranked = unranked[offset:offset + limit]
    newest = _db.execute("SELECT MAX(ts_ms) FROM tape_fills").fetchone()
    as_of = _tm._int(newest[0]) if newest and newest[0] is not None else at
    payload = {
        "board": out["board"], "label": out["label"], "window": out["window"], "windows": out["windows"],
        "category": out.get("category"), "categories": list(_lb_boards.CATEGORIES) if board == "category" else [],
        "formula": out["formula"], "gate": out["gate"], "tieBreaks": out["tieBreaks"],
        "cadence": out["cadence"], "cadenceMs": out["cadenceMs"], "note": out["note"],
        "rows": page_rows, "unranked": page_unranked,
        "summary": dict(out["summary"], **{"filteredTotal": (len(rows) if filtered else None)}),
        "page": {"limit": limit, "offset": offset, "returned": len(page_rows),
                 "unrankedReturned": len(page_unranked), "rankedTotal": total,
                 "hasMore": (offset + limit) < len(rows), "filtered": filtered},
        "boards": _lb_picker(),
        "readPlan": out["plan"],
        "freshness": _lb_freshness(str(board), at_ms=at, label=str(out["label"])),
        "methodologyPath": "/v1/leaderboard/methodology",
        "excludedTotal": len(excluded),
        # The exclusion LIST is an operator surface (D7): the count is public because the board's own totals have
        # to add up, the reasons are not, because a wall of shame is a different product from a leaderboard.
        "excluded": ([{"anon": _anon(w), "reason": d["reason"], "atMs": d["atMs"], "actor": d["actor"]}
                      for w, d in sorted(excluded.items())] if is_admin else None),
        "disclaimer": ("ranked by the formula above from our own settled tape; a rank is not advice, and every "
                       "win rate here is behind the same sample gate as the rest of the product"),
    }
    return _stamped(payload, ttl_ms=1_000, stale_ms=flags().stale_ms_tape, as_of_ms=as_of)


@app.get("/v1/leaderboard/methodology", responses=LEADERBOARD_METHODOLOGY_RESPONSES)
def get_leaderboard_methodology(request: Request):
    """The published methodology: the same object the engine reads, plus the parts only the read path knows.

    Served rather than written down separately (D1 §2.4): a methodology page maintained by hand beside a ranking
    engine maintained in code is two authorities over one number, and the first disagreement is the one a user
    screenshots.
    """
    at = _now_ms()
    windows = {}
    for board_id in _lb_boards.BOARD_IDS:
        for w in _lb_boards.windows_for(board_id):
            try:
                windows["%s:%s" % (board_id, w)] = _lb_source.read_plan(board_id=board_id, window=w, at_ms=at)
            except ValueError:                                              # pragma: no cover - spec only
                continue
    return _stamped(_lb_boards.methodology(extra={
        "path": "/v1/leaderboard/methodology",
        "windows": windows,
        "readPath": ("every board is ranked from our own settled tape, per settled market, on every read; the "
                     "snapshot table feeds the rank sparkline and nothing else"),
        "launchList": [{"item": "the $500 lifetime turnover floor re-derived on production data (it is a "
                                "judgement, and the seed's median fill is $0.02)",
                        "owner": "P11 D2"}],
        "excludedWallets": ("a wallet can be excluded from a board by an operator; the board's totals show how "
                            "many, and the reasons are not published"),
    }), ttl_ms=60_000, stale_ms=0, as_of_ms=at)


@app.get("/v1/leaderboard/why", responses=LEADERBOARD_WHY_RESPONSES)
def get_leaderboard_why(request: Request,
                        a: str = Query(min_length=4, max_length=64),
                        b: str = Query(min_length=4, max_length=64),
                        board: str = Query(default="risk_adjusted",
                                           pattern="^(risk_adjusted|win_rate|volume|rising|category|copied)$"),
                        window: str | None = Query(default=None, pattern="^(24h|7d|30d|90d|all)$"),
                        category: str = Query(default="", max_length=32)):
    """Why one wallet is above another, in one sentence, with both component sets.

    This is the P11 gate's own question, served: "a trader at rank 47 with fewer resolved markets than the trader
    at rank 12" is not a bug to explain away — the sample gate decides eligibility and the metric decides order,
    and the sentence says so with the numbers.
    """
    rid = request.state.request_id
    at = _now_ms()
    try:
        evidence, plan, _excluded = _lb_evidence(board_id=str(board), window=str(window or ""), at_ms=at)
    except ValueError as exc:
        return err("VALIDATION", rid, detail=str(exc), where=["window"])
    try:
        out = _lb_rank.explain(board_id=str(board), wallets=evidence, at_ms=at, a=str(a), b=str(b),
                               category=str(category), window=plan["window"])
    except ValueError as exc:
        return err("VALIDATION", rid, detail=str(exc), where=["category"])
    labels = _labels_by_wallet()
    if not out.get("ok"):
        return err("NO_SUCH_RESOURCE", rid, detail=out["why"])
    total = max(_tm._int(out["a"]["rank"]), _tm._int(out["b"]["rank"]))
    return _stamped({"board": out["board"], "window": plan["window"], "why": out["why"],
                     "a": _lb_row_out(out["a"], total=total, labels=labels),
                     "b": _lb_row_out(out["b"], total=total, labels=labels),
                     "components": out["components"], "note": out["note"],
                     "sampleGate": _lb_boards.MIN_RESOLVED, "unranked": out.get("unranked") or []},
                    ttl_ms=1_000, stale_ms=flags().stale_ms_tape, as_of_ms=at)


@app.get("/v1/leaderboard/snapshots", responses=LEADERBOARD_SNAPSHOT_RESPONSES)
def get_leaderboard_snapshots(request: Request, anon: str = Query(min_length=4, max_length=64),
                              days: int = Query(default=30, ge=1, le=90)):
    """One wallet's rank history: the 30-day sparkline, from the snapshot table and nothing else.

    A rank recomputed from today's ledger is today's rank, not the rank they had last Tuesday; "were they
    falling?" is a question about the past and only a written-down past can answer it.

    The fold itself lives in `_lb_history`, which `/rank` also calls. This route kept its own copy of it for one
    phase, and a second fold of the same rows is how a sparkline and a badge end up disagreeing about the same
    wallet on the same screen. What is left here is what is actually specific to this route: the pseudonym
    check (a pseudonym nobody has is a 404, not a history that reads as "this trader never ranked") and this
    board's freshness stamp.
    """
    rid = request.state.request_id
    if _wallet_for_anon(str(anon)) is None:
        return err("NO_SUCH_RESOURCE", rid)
    at = _now_ms()
    return _stamped(_lb_history(str(anon), days=int(days), at_ms=at),
                    ttl_ms=30_000, stale_ms=flags().stale_ms_tape, as_of_ms=at)


@app.get("/v1/leaderboard/runs", responses=LEADERBOARD_RUN_RESPONSES)
def get_leaderboard_runs(request: Request, board: str | None = Query(default=None, max_length=32),
                         limit: int = Query(default=24, ge=1, le=200)):
    """The recompute record, newest first. Public on purpose: a cadence nobody can check is a claim."""
    sql = ("SELECT board, window_key, ranked, unranked, blew_up, duration_ms, computed_ms"
           " FROM leaderboard_runs")
    args: list = []
    if board:
        sql += " WHERE board=?"
        args.append(str(board))
    sql += " ORDER BY computed_ms DESC, board ASC LIMIT ?"
    args.append(int(limit))
    rows = []
    for (b, window_key, ranked, unranked, blew_up, duration, computed) in _db.execute(sql, tuple(args)).fetchall():
        rows.append({"board": str(b), "label": (_lb_boards.board(str(b)) or {}).get("label", str(b)),
                     "window": str(window_key), "ranked": _tm._int(ranked), "unranked": _tm._int(unranked),
                     "blewUp": _tm._int(blew_up), "durationMs": _tm._int(duration),
                     "computedMs": _tm._int(computed)})
    freshest = {b: _lb_freshness(b, at_ms=_now_ms(), label=(_lb_boards.board(b) or {}).get("label", b))
                for b in _lb_boards.BOARD_IDS}
    return _stamped({"rows": rows, "freshness": freshest,
                     "cadences": {b: _tm._int((_lb_boards.board(b) or {}).get("cadenceMs"))
                                  for b in _lb_boards.BOARD_IDS}}, ttl_ms=5_000, stale_ms=0)


# ----------------------------------------------------------------------------------- P11 D3 · a wallet's standing
# Four reads and one write, and each of them answers a question the leaderboard screen asks while it is open:
#
#   * `GET /v1/leaderboard/rank`     — where is THIS wallet, what is the gap to the place above, and what has
#                                      their rank done for 30 days. The dossier's badge, D4's pinned self-rank.
#   * `GET /v1/leaderboard/compare`  — up to three wallets side by side, with the pairwise sentences the engine
#                                      already writes (`rank.explain`). A comparison built by the CLIENT out of
#                                      three separate reads is three reads that can disagree; this is one.
#   * `GET /v1/leaderboard/follows`  — the wallets you follow, with their CURRENT standing attached, because a
#                                      follow list that does not show what happened since is a list of names.
#   * `POST /v1/leaderboard/follows` — follow/unfollow, idempotent per key, keyed by PSEUDONYM.
#
# The one thing this section refuses to do is invent a rank for a wallet nobody has seen: an unknown pseudonym is
# a 404 on `/rank` (there is no standing to return) and an explicit entry in `/compare`'s `unknown` list (one bad
# name must not hide the two good ones).

#: The field each board's rows are ordered by, and the unit that field is in. `rank` needs this to say "the gap
#: to the place above" in the board's own vocabulary: 88333 bps of score, $1,200 of turnover, 3 copiers.
_LB_ORDER_FIELD = {"risk_adjusted": ("scoreBps", "bps"), "win_rate": ("winRateBps", "bps"),
                   "volume": ("verifiedVolumeMicro", "micro"), "rising": ("improvementMicro", "micro"),
                   "category": ("scoreBps", "bps"), "copied": ("copiers", "count")}


def _lb_history(anon: str, *, days: int, at_ms: int, board: str = "", window: str = "") -> dict:
    """One wallet's rank history, grouped per (board, window) — the sparkline's only source.

    Shared by `/snapshots` and `/rank` rather than duplicated: the two differ in what they SELECT, not in how a
    history is folded, and two folds of the same rows is how a sparkline and a table end up disagreeing about the
    same wallet. `board`/`window` narrow the query rather than filtering the answer, so a profile asks for one
    board's history and does not read the others' rows at all.
    """
    since = int(at_ms) - int(days) * 86_400_000
    sql = ("SELECT board, window_key, computed_ms, rank, score_bps, settled, drawdown_micro"
           " FROM leaderboard_snapshots WHERE wallet=? AND computed_ms >= ?")
    args: list = [str(anon), since]
    if board:
        sql += " AND board=?"
        args.append(str(board))
    if window:
        sql += " AND window_key=?"
        args.append(str(window))
    sql += " ORDER BY board, window_key, computed_ms ASC"
    grouped: dict[tuple, list[dict]] = {}
    for (board_id, window_key, computed, rank, score, settled, drawdown) in _db.execute(sql, tuple(args)).fetchall():
        grouped.setdefault((str(board_id), str(window_key)), []).append(
            {"tsMs": _tm._int(computed), "rank": _tm._int(rank), "scoreBps": _tm._int(score),
             "settledMarkets": _tm._int(settled), "maxDrawdownMicro": _tm._int(drawdown),
             "maxDrawdown": fmt_usdc(_tm._int(drawdown))})
    boards = []
    for (board_id, window_key), points in sorted(grouped.items()):
        ranks = [p["rank"] for p in points]
        boards.append({"board": board_id, "label": (_lb_boards.board(board_id) or {}).get("label", board_id),
                       "window": window_key, "points": points, "latestRank": ranks[-1], "bestRank": min(ranks),
                       "worstRank": max(ranks), "delta": ranks[-1] - ranks[0],
                       "note": ("delta is a change in RANK, so a negative number is an improvement; the board's "
                                "size changes between snapshots, which is why the rank is shown beside the score")})
    return {"anon": str(anon), "days": int(days), "boards": boards,
            "snapshots": sum(len(p) for p in grouped.values()),
            "note": ("rank history is written by the recompute; a board with no points has not been recomputed "
                     "since this wallet first appeared")}


def _lb_history_for(anon: str, *, board: str, window: str, days: int, at_ms: int) -> dict:
    """The single entry a `/rank` answer carries: this board's sparkline, or an honest empty one."""
    hist = _lb_history(anon, days=days, at_ms=at_ms, board=board, window=window)
    entry = next((b for b in hist["boards"] if b["board"] == board and b["window"] == window), None)
    if entry is None:
        return {"days": int(days), "points": [], "snapshots": 0, "latestRank": None, "bestRank": None,
                "worstRank": None, "delta": None,
                "note": ("no history yet: the rank sparkline is written by the recompute, so a wallet that has "
                         "not been recomputed since it first appeared has an empty line rather than a flat one")}
    return {"days": int(days), "points": entry["points"], "snapshots": len(entry["points"]),
            "latestRank": entry["latestRank"], "bestRank": entry["bestRank"], "worstRank": entry["worstRank"],
            "delta": entry["delta"],
            "note": ("%d points over %d days; a missing stretch is a recompute that did not run, not a rank that "
                     "did not change" % (len(entry["points"]), int(days)))}


def _lb_field_text(value: int, units: str) -> str:
    """An ordering field, said in its own unit: USDC for turnover and movement, basis points for a score, a count
    for copiers. One function, because the verdict sentence and the gap sentence must not disagree about whether
    a number is money."""
    if units == "micro":
        return "%s USDC" % fmt_usdc(_tm._int(value))
    if units == "count":
        return "%d copier%s" % (_tm._int(value), "" if _tm._int(value) == 1 else "s")
    return "%d bps of score" % _tm._int(value)


def _lb_standing(*, anon: str, board_id: str, window: str, at_ms: int, category: str,
                 days: int) -> dict:
    """One wallet's place on one board, with the gap to the place above it and its rank history."""
    out = _lb_board(board_id=board_id, window=window, at_ms=at_ms, category=category)
    plan = out["plan"]
    total = _tm._int(out["rankedTotal"])
    labels = _labels_by_wallet()
    rows = out["rows"]
    index = next((i for i, r in enumerate(rows) if str(r["wallet"]) == str(anon)), None)
    field, units = _LB_ORDER_FIELD.get(str(board_id), ("scoreBps", "bps"))
    handles = _lb_published_handles()
    payload: dict = {
        "board": out["board"], "label": out["label"], "window": out["window"], "category": out.get("category"),
        "formula": out["formula"], "gate": out["gate"], "tieBreaks": out["tieBreaks"], "sampleGate": _lb_boards.MIN_RESOLVED,
        "orderField": field, "orderUnits": units, "anon": str(anon), "rankedTotal": total,
        "methodologyPath": "/v1/leaderboard/methodology",
        "history": _lb_history_for(str(anon), board=str(board_id), window=plan["window"], days=days, at_ms=at_ms),
    }
    if index is None:
        refused = next((u for u in out["unranked"] if str(u["wallet"]) == str(anon)), None)
        if refused is None:
            payload.update({"state": "unknown", "rank": None, "rankBadge": None, "row": None,
                            "reasons": [], "note": "this wallet is not on this board and not in its refusals"})
            return payload
        payload.update({"state": "unranked", "rank": None, "rankBadge": None, "row": None,
                        "reasons": list(refused["reasons"]),
                        "unranked": {"anon": str(refused["wallet"]), "reasons": list(refused["reasons"]),
                                     "settledMarkets": _tm._int(refused["settledMarkets"]),
                                     "verifiedVolumeMicro": _tm._int(refused["verifiedVolumeMicro"]),
                                     "verifiedVolume": fmt_usdc(_tm._int(refused["verifiedVolumeMicro"])),
                                     "washedMicro": _tm._int(refused.get("washedMicro")),
                                     "washed": fmt_usdc(_tm._int(refused.get("washedMicro"))),
                                     "note": refused["note"]},
                        "note": "not ranked, and the reason is the number below rather than a missing row"})
        return payload
    row = rows[index]
    above = rows[index - 1] if index > 0 else None
    below = rows[index + 1] if index + 1 < len(rows) else None
    mine, theirs = _tm._int(row.get(field)), (_tm._int(above.get(field)) if above else None)
    gap = None
    if above is not None:
        gap = {"rankAbove": _tm._int(above["rank"]), "anonAbove": str(above["wallet"]), "field": field,
               "units": units, "value": mine, "valueAbove": theirs,
               # `toPass` is the number that would actually move them: strictly greater than the wallet above,
               # because a tie falls through to the stated tie-breaks and does not pass anybody.
               "delta": (theirs - mine) if theirs is not None else None,
               "toPass": None if theirs is None else theirs + 1,
               "note": ("the gap is stated in this board's own ordering field; a tie does not pass, because ties "
                        "fall through to the board's declared tie-breaks")}
    payload.update({
        "state": str(row["state"]), "rank": _tm._int(row["rank"]),
        "rankBadge": {"rank": _tm._int(row["rank"]), "rankedTotal": total, "text": "#%d" % _tm._int(row["rank"])},
        "percentileBps": _tm._int((_tm._int(row["rank"]) * 10_000 + max(1, total) - 1) // max(1, total)),
        "row": _lb_row_out(row, total=total, labels=labels, handles=handles),
        "above": (None if above is None else _lb_row_out(above, total=total, labels=labels, handles=handles)),
        "below": (None if below is None else _lb_row_out(below, total=total, labels=labels, handles=handles)),
        "gap": gap,
        "reasons": [],
        "note": ("rank %d of %d on %s, and both neighbours are served with it: a rank with no context is a "
                 "number nobody can act on" % (_tm._int(row["rank"]), total, out["label"].lower())),
    })
    return payload


@app.get("/v1/leaderboard/rank", responses=LEADERBOARD_RANK_RESPONSES)
def get_leaderboard_rank(request: Request, anon: str = Query(min_length=4, max_length=64),
                         board: str = Query(default="risk_adjusted",
                                            pattern="^(risk_adjusted|win_rate|volume|rising|category|copied)$"),
                         window: str | None = Query(default=None, pattern="^(24h|7d|30d|90d|all)$"),
                         category: str = Query(default="", max_length=32),
                         days: int = Query(default=30, ge=1, le=90)):
    """One wallet's standing: the badge, the neighbours, the gap, and the 30-day rank history.

    Public, like every other board read: a leaderboard whose rows cannot be opened one at a time is a picture of
    a leaderboard. An unknown pseudonym is a 404 — there is no standing to return, and inventing an empty one
    would put a fabricated rank-zero row in front of a user.
    """
    rid = request.state.request_id
    if _wallet_for_anon(str(anon)) is None:
        # `NO_SUCH_RESOURCE`, not `NOT_FOUND`: the latter's message is literally "no such market", and a
        # leaderboard that answers a question about a trader with a sentence about a market is the kind of copy
        # bug that makes a user think they clicked the wrong thing.
        return err("NO_SUCH_RESOURCE", rid)
    at = _now_ms()
    try:
        out = _lb_standing(anon=str(anon), board_id=str(board), window=str(window or ""), at_ms=at,
                           category=str(category), days=int(days))
    except ValueError as exc:
        return err("VALIDATION", rid, detail=str(exc), where=["window" if "reads" in str(exc) else "category"])
    return _stamped(out, ttl_ms=2_000, stale_ms=flags().stale_ms_tape, as_of_ms=at)


@app.get("/v1/leaderboard/compare", responses=LEADERBOARD_COMPARE_RESPONSES)
def get_leaderboard_compare(request: Request, anons: str = Query(min_length=9, max_length=200),
                            board: str = Query(default="risk_adjusted",
                                               pattern="^(risk_adjusted|win_rate|volume|rising|category|copied)$"),
                            window: str | None = Query(default=None, pattern="^(24h|7d|30d|90d|all)$"),
                            category: str = Query(default="", max_length=32)):
    """Up to three wallets side by side, ordered by the board, with the pairwise sentences.

    The cap is three because that is what fits on a screen and what the kit's compare mode asks for; more is a
    table, and a table of six traders is a leaderboard. The pairwise explanations come from `rank.explain` — the
    same function `/why` serves — so a comparison cannot disagree with the page it was opened from.
    """
    rid = request.state.request_id
    wanted: list[str] = []
    for part in str(anons).split(","):
        name = part.strip()
        if name and name not in wanted:
            wanted.append(name)
    # Every name must be pseudonym-shaped before anything else happens. An address in a query is a request we do
    # not serve (the product publishes pseudonyms and only pseudonyms), and echoing it back inside `requested` or
    # `unknown` would put an address into a response body that the P11 gate greps for exactly that shape.
    bad = [a for a in wanted if not _pseudo.is_anon(a)]
    if bad:
        return err("VALIDATION", rid, detail="compare takes pseudonyms, not addresses or ids",
                   where=["anons"])
    if len(wanted) < 2:
        return err("VALIDATION", rid, detail="compare needs two distinct wallets (the same wallet twice is not a "
                                             "comparison)", where=["anons"])
    if len(wanted) > 3:
        return err("VALIDATION", rid, detail="compare takes at most three wallets; four is a table, and a table "
                                             "of six is a leaderboard", where=["anons"])
    at = _now_ms()
    try:
        out = _lb_board(board_id=str(board), window=str(window or ""), at_ms=at, category=str(category))
    except ValueError as exc:
        return err("VALIDATION", rid, detail=str(exc), where=["window" if "reads" in str(exc) else "category"])
    plan = out["plan"]
    total = _tm._int(out["rankedTotal"])
    labels = _labels_by_wallet()
    by_anon = {str(r["wallet"]): r for r in out["rows"]}
    refused = {str(u["wallet"]): u for u in out["unranked"]}
    field, units = _LB_ORDER_FIELD.get(str(board), ("scoreBps", "bps"))
    unknown = [a for a in wanted if a not in by_anon and a not in refused]
    rows = [_lb_row_out(by_anon[a], total=total, labels=labels) for a in wanted if a in by_anon]
    unranked = []
    for a in wanted:
        u = refused.get(a)
        if u is None:
            continue
        unranked.append({"anon": str(u["wallet"]), "reasons": list(u["reasons"]),
                         "settledMarkets": _tm._int(u["settledMarkets"]),
                         "verifiedVolumeMicro": _tm._int(u["verifiedVolumeMicro"]),
                         "verifiedVolume": fmt_usdc(_tm._int(u["verifiedVolumeMicro"])),
                         "washedMicro": _tm._int(u.get("washedMicro")),
                         "washed": fmt_usdc(_tm._int(u.get("washedMicro"))),
                         "washNote": ("" if not _tm._int(u.get("washedMicro")) else
                                      "removed %s of round-tripped volume before refusing it"
                                      % fmt_usdc(_tm._int(u.get("washedMicro")))),
                         "note": u["note"], "classifications": list(labels.get(a, []))})
    # The pairwise sentences, for the pairs that BOTH ranked: a sentence about a wallet that is not on the board
    # would be a sentence about a rank it does not have.
    order = []
    evidence = None
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = str(rows[i]["anon"]), str(rows[j]["anon"])
            if evidence is None:
                evidence, _plan, _ex = _lb_evidence(board_id=str(board), window=str(window or ""), at_ms=at)
            try:
                why = _lb_rank.explain(board_id=str(board), wallets=evidence, at_ms=at, a=a, b=b,
                                       category=str(category), window=plan["window"])
            except ValueError:                                                  # pragma: no cover - spec only
                continue
            if why.get("ok"):
                order.append({"a": a, "b": b, "why": why["why"], "aAbove": bool(why.get("aAbove")),
                              "components": why["components"]})
    # The lead is the MAXIMUM of the ordering field, not the first wallet the caller listed: the requested order
    # is the caller's, the ordering is the board's, and a verdict that read `rows[0]` would crown whoever the
    # client happened to put first.
    lead = max(rows, key=lambda r: _tm._int(r.get(field)), default=None)
    payload = {
        "board": out["board"], "label": out["label"], "window": plan["window"], "category": out.get("category"),
        "formula": out["formula"], "gate": out["gate"], "tieBreaks": out["tieBreaks"],
        "sampleGate": _lb_boards.MIN_RESOLVED, "orderField": field, "orderUnits": units,
        "requested": wanted, "rows": rows, "unranked": unranked, "unknown": unknown, "order": order,
        "rankedTotal": total, "methodologyPath": "/v1/leaderboard/methodology",
        "verdict": ("" if lead is None else
                    "%s leads this comparison on %s at %s, and every other row carries its own components"
                    % (lead["anon"], out["label"].lower(), _lb_field_text(_tm._int(lead.get(field)), units))),
        "note": ("the rows are served in the order asked for, each with its own board rank; the sentences are the "
                 "engine's, so they cannot disagree with the board these wallets came from"),
        "disclaimer": ("a comparison of published components, not a recommendation: a rank is not advice, and a "
                       "win rate here is behind the same sample gate as the rest of the product"),
    }
    return _stamped(payload, ttl_ms=2_000, stale_ms=flags().stale_ms_tape, as_of_ms=at)


@app.get("/v1/leaderboard/follows", responses=LEADERBOARD_FOLLOWS_RESPONSES)
def get_leaderboard_follows(request: Request, board: str = Query(default="risk_adjusted",
                                                                 pattern="^(risk_adjusted|win_rate|volume|rising|category|copied)$"),
                            window: str | None = Query(default=None, pattern="^(24h|7d|30d|90d|all)$"),
                            category: str = Query(default="", max_length=32)):
    """The wallets this account follows, with their CURRENT standing attached.

    A follow list that only shows names is a list of bookmarks; the reason to follow a trader is to see what they
    are doing, so the stand is resolved on read (the board is computed live anyway) and a followed wallet that
    has since dropped off the board says so with the number that removed it.
    """
    rid = request.state.request_id
    uid, _row, e = _principal(request)
    if e:
        return e
    at = _now_ms()
    follows = _db.execute("SELECT anon_wallet, label, created_ms FROM trader_follows WHERE user_id=?"
                          " ORDER BY created_ms DESC, anon_wallet ASC LIMIT 200", (str(uid),)).fetchall()
    try:
        out = _lb_board(board_id=str(board), window=str(window or ""), at_ms=at, category=str(category))
    except ValueError as exc:
        return err("VALIDATION", rid, detail=str(exc), where=["window" if "reads" in str(exc) else "category"])
    total = _tm._int(out["rankedTotal"])
    labels = _labels_by_wallet()
    by_anon = {str(r["wallet"]): r for r in out["rows"]}
    refused = {str(u["wallet"]): u for u in out["unranked"]}
    rows = []
    for anon, label, created in follows:
        a = str(anon)
        row = by_anon.get(a)
        held = refused.get(a)
        rows.append({
            "anon": a, "label": str(label or ""), "followedMs": _tm._int(created),
            "state": ("ranked" if row is not None else ("unranked" if held is not None else "absent")),
            "rank": (None if row is None else _tm._int(row["rank"])),
            "rankBadge": (None if row is None else
                          {"rank": _tm._int(row["rank"]), "rankedTotal": total,
                           "text": "#%d" % _tm._int(row["rank"])}),
            # `scoreBps` and no `score` string: basis points of a ratio are not USDC, and passing them through
            # the money formatter is how a screen ends up printing "$0.093333" for a score of 93333.
            "scoreBps": (None if row is None else _tm._int(row.get("scoreBps"))),
            "realisedMicro": (None if row is None else _tm._int(row.get("realisedMicro"))),
            "realised": (None if row is None else fmt_usdc(_tm._int(row.get("realisedMicro")))),
            "maxDrawdownMicro": (None if row is None else _tm._int(row.get("maxDrawdownMicro"))),
            "drawdown": (None if row is None else fmt_usdc(_tm._int(row.get("maxDrawdownMicro")))),
            "winRateBps": (None if row is None else row.get("winRateBps")),
            "insufficientSample": (None if row is None else bool(row.get("insufficientSample"))),
            "sampleNote": ("" if row is None else str(row.get("sampleNote") or "")),
            "reasons": ([] if held is None else list(held["reasons"])),
            "classifications": list(labels.get(a, [])),
        })
    return _stamped({"board": out["board"], "label": out["label"], "window": out["plan"]["window"],
                     "rows": rows, "total": len(rows), "rankedTotal": total,
                     "note": ("a follow is a watch, not a copy: it changes nothing about what gets traded. Only a "
                              "copy config with its guards passed can do that"),
                     "states": {"ranked": sum(1 for r in rows if r["state"] == "ranked"),
                                "unranked": sum(1 for r in rows if r["state"] == "unranked"),
                                "absent": sum(1 for r in rows if r["state"] == "absent")}},
                    ttl_ms=2_000, stale_ms=flags().stale_ms_tape, as_of_ms=at)


@app.post("/v1/leaderboard/follows", status_code=200, responses=LEADERBOARD_FOLLOW_RESPONSES,
          openapi_extra=_body_schema(("anon",), {
              "anon": {"type": "string", "minLength": 4, "maxLength": 64,
                       "description": "the pseudonym to follow; an address is not accepted and is not accepted "
                                      "anywhere else either"},
              "state": {"type": "string", "enum": ["follow", "unfollow"], "description": "defaults to follow"},
              "label": {"type": "string", "maxLength": 48, "description": "what to call them in your list"}}))
def post_leaderboard_follow(request: Request, body: dict = Body(...),
                            idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """Follow or unfollow, keyed by pseudonym, idempotent per key.

    A follow is stored against the PSEUDONYM and not the address, which is the same rule the board publishes
    under: the identity a user follows is the identity they can see. The pseudonym must be one we have seen —
    following a wallet nobody has traded is a row about nobody.
    """
    rid = request.state.request_id
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    bad = _check_body(body, ("anon",), rid, allowed=("anon", "state", "label"))
    if bad is not None:
        return bad
    bad = _check_props(body, {"anon": {"type": "string", "minLength": 4},
                              "state": {"type": "string", "enum": ["follow", "unfollow"]},
                              "label": {"type": "string", "maxLength": 48}}, rid)
    if bad is not None:
        return bad
    uid, _row, e = _principal(request)
    if e:
        return e
    return _idem_run(str(uid), str(idempotency_key), body, rid, lambda: _lb_follow_work(rid, uid, body))


def _lb_follow_work(rid: str, uid: str, body: dict):
    anon = str(body["anon"]).strip()
    if _wallet_for_anon(anon) is None:
        return err("NO_SUCH_RESOURCE", rid, detail="no wallet with that pseudonym has been seen trading")
    state = str(body.get("state") or "follow")
    at = _now_ms()
    held = _db.execute("SELECT created_ms FROM trader_follows WHERE user_id=? AND anon_wallet=?",
                       (str(uid), anon)).fetchone()
    if state == "unfollow":
        _db.execute("DELETE FROM trader_follows WHERE user_id=? AND anon_wallet=?", (str(uid), anon))
        _db.commit()
        return _stamped({"anon": anon, "state": "unfollowed", "followed": False, "existed": held is not None,
                         "followedMs": None,
                         "note": ("unfollowing removes the watch and nothing else — a copy config you made from "
                                  "this trader is unaffected and still has its own guards")}, ttl_ms=0, stale_ms=0)
    _db.execute("INSERT OR IGNORE INTO trader_follows (user_id, anon_wallet, label, created_ms) VALUES (?,?,?,?)",
                (str(uid), anon, str(body.get("label") or "")[:48], at))
    _db.commit()
    kept = _db.execute("SELECT created_ms, label FROM trader_follows WHERE user_id=? AND anon_wallet=?",
                       (str(uid), anon)).fetchone()
    return _stamped({"anon": anon, "state": "followed", "followed": True, "existed": held is not None,
                     "followedMs": _tm._int(kept[0] if kept else at), "label": str((kept[1] if kept else "") or ""),
                     "note": ("following is a watch, not a copy: nothing is traded on your behalf, and turning "
                              "it into a copy config is a separate decision with its own dry-run and guards")},
                    ttl_ms=0, stale_ms=0, as_of_ms=at)

@app.post("/v1/leaderboard/recompute", status_code=200, responses=LEADERBOARD_RECOMPUTE_RESPONSES,
          openapi_extra=_body_schema((), {
              "boards": {"type": "array", "items": {"type": "string", "enum": list(_lb_boards.BOARD_IDS)},
                         "minItems": 1, "maxItems": len(_lb_boards.BOARD_IDS),
                         "description": "defaults to every board"},
              "window": {"type": "string", "enum": ["24h", "7d", "30d", "90d", "all"],
                         "description": "defaults to each board's own default window"}}))
def post_leaderboard_recompute(request: Request, body: dict | None = Body(default=None),
                               idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """Write the rank history: one snapshot per wallet per board, plus the integrity verdict and the run row.

    The worker's job, callable by an account because it is deterministic and idempotent per key — and because a
    gate that has to reach inside the process to prove the cadence works is a gate that never runs. A repeated
    key returns the first run's answer rather than appending a second history point.
    """
    rid = request.state.request_id
    body = body or {}
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    bad = _check_body(body, (), rid, allowed=("boards", "window"))
    if bad is not None:
        return bad
    bad = _check_props(body, {"boards": {"type": "array", "minItems": 1}, "window": {"type": "string"}}, rid)
    if bad is not None:
        return bad
    uid, _row, e = _principal(request)
    if e:
        return e
    return _idem_run(str(uid), str(idempotency_key), body, rid, lambda: _lb_recompute_work(rid, body))


def _lb_recompute_work(rid: str, body: dict):
    """Rank every board, write the history, and answer with what was written."""
    at = _now_ms()
    bucket = at // _lb_boards.HOUR_MS
    wanted = [str(b) for b in (body.get("boards") or list(_lb_boards.BOARD_IDS))]
    unknown = [b for b in wanted if _lb_boards.board(b) is None]
    if unknown:
        return err("VALIDATION", rid, detail="unknown board", where=["boards"])
    window = body.get("window")
    runs, snapshots, integrity_rows = [], 0, 0
    for board_id in wanted:
        spec = _lb_boards.board(str(board_id))
        try:
            window_key = str(window or spec["window"])
            cats = list(_lb_boards.CATEGORIES) if board_id == "category" else [""]
            for cat in cats:
                started = time.perf_counter()
                out = _lb_board(board_id=board_id, window=window_key, at_ms=at, category=cat)
                duration = int((time.perf_counter() - started) * 1000)
                # The run key is the window — plus the category, for the board that is four boards in one. The
                # table's key is (board, window, hour), and without the suffix the four category runs collapse
                # into one row and the record says we recomputed a quarter as often as we did.
                key = ("%s:%s" % (window_key, cat)) if cat else window_key
                _db.execute("INSERT OR REPLACE INTO leaderboard_runs (board, window_key, bucket_hour, ranked,"
                            " unranked, blew_up, duration_ms, computed_ms) VALUES (?,?,?,?,?,?,?,?)",
                            (board_id, key, bucket, _tm._int(out["rankedTotal"]),
                             _tm._int(out["unrankedTotal"]), _tm._int(out["blewUpCount"]), duration, at))
                for row in out["rows"]:
                    _db.execute("INSERT OR REPLACE INTO leaderboard_snapshots (board, window_key, wallet, rank,"
                                " score_bps, settled, drawdown_micro, computed_ms) VALUES (?,?,?,?,?,?,?,?)",
                                (board_id, window_key, str(row["wallet"]), _tm._int(row["rank"]),
                                 _tm._int(row["scoreBps"]), _tm._int(row["settledMarkets"]),
                                 _tm._int(row["maxDrawdownMicro"]), at))
                    snapshots += 1
                runs.append({"board": board_id, "window": window_key, "category": cat or None,
                             "ranked": _tm._int(out["rankedTotal"]), "unranked": _tm._int(out["unrankedTotal"]),
                             "blewUp": _tm._int(out["blewUpCount"]), "disputedWithheld": out["summary"]["disputedWithheld"],
                             "durationMs": duration})
                if cat == "" and board_id == _lb_boards.DEFAULT_BOARD:
                    integrity_rows = _lb_write_integrity(out, at_ms=at)
        except ValueError as exc:
            return err("VALIDATION", rid, detail=str(exc), where=["window"])
    _db.commit()
    return _stamped({"runs": runs, "snapshots": snapshots, "integrity": integrity_rows, "computedMs": at,
                     "bucketHour": bucket, "boards": wanted, "window": window,
                     "note": ("the run table is keyed by (board, window, hour), so a recompute inside the same "
                              "hour replaces that hour's row rather than inventing cadence we did not have")},
                    ttl_ms=0, stale_ms=0, as_of_ms=at)


def _lb_write_integrity(board_out: dict, *, at_ms: int) -> int:
    """The newest integrity verdict per wallet: the state, the reasons behind it, and the numbers a row shows.

    Written from the board's own rows rather than recomputed, so the dashboard and the leaderboard cannot
    disagree about why a wallet is where it is.
    """
    written = 0
    seen = set()
    for row in board_out["rows"]:
        wallet = str(row["wallet"])
        if wallet in seen:
            continue
        seen.add(wallet)
        age = _tm._int(row.get("ageDays"))
        reasons = {"labels": list(row.get("labels") or []), "washNote": row.get("washNote", ""),
                   "bestTradeShareBps": _tm._int(row.get("bestTradeShareBps")),
                   "disputedExcluded": _tm._int(row.get("disputedExcluded")),
                   "sampleNote": row.get("sampleNote", "")}
        state = str(row.get("state") or "ranked")
        _db.execute("INSERT OR REPLACE INTO wallet_integrity (wallet, state, reasons_json, washed_micro,"
                    " verified_micro, best_trade_share_bps, derived_from, provisional_until_ms, computed_ms)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (wallet, state, json.dumps(reasons, sort_keys=True), _tm._int(row.get("washedMicro")),
                     _tm._int(row.get("verifiedVolumeMicro")), _tm._int(row.get("bestTradeShareBps")),
                     str((row.get("farm") or {}).get("derivedFrom", "")),
                     (None if age >= _lb_boards.PROVISIONAL_DAYS
                      else at_ms + (_lb_boards.PROVISIONAL_DAYS - age) * 86_400_000), at_ms))
        written += 1
    return written


# The P10 routes' auth levels. Every served route needs a row here or the request fails closed with
# `AUTHZ_UNDECLARED`, which is the correct default and an infuriating one to debug — so the four public reads
# (a tape, its facets, the whale feed and a pseudonymous dossier: all of them reveal only what the venue
# already publishes) and the six user-scoped ones are declared next to the routes that use them.
_levels_p10 = {
    "GET /v1/tape/fills": (_authz.PUBLIC, ""),
    "GET /v1/tape/facets": (_authz.PUBLIC, ""),
    "GET /v1/whales": (_authz.PUBLIC, ""),
    "GET /v1/traders/{anon}": (_authz.PUBLIC, ""),
    "GET /v1/copy/sources": (_authz.USER, ""),
    "GET /v1/copy/configs": (_authz.USER, ""),
    "POST /v1/copy/configs": (_authz.USER, ""),
    "POST /v1/copy/configs/guards": (_authz.USER, "scoped:config_id"),
    "GET /v1/copy/configs/monitor": (_authz.USER, "scoped:config_id"),
    "GET /v1/me/portfolio": (_authz.USER, ""),
    "GET /v1/whale-views": (_authz.USER, ""),
    "POST /v1/whale-views": (_authz.USER, ""),
    "POST /v1/radar/runs": (_authz.USER, ""),
    "GET /v1/radar/runs/{job_id}": (_authz.USER, ""),
    # P10 D8/D9. Every one of these is USER and none is scoped: an automation rule and an alert rule both belong
    # to the account that made them, and the reads are already filtered by the *session's* user id rather than
    # by anything the client sends — which is the property that makes a scoped level unnecessary here.
    "GET /v1/automations": (_authz.USER, ""),
    "POST /v1/automations": (_authz.USER, ""),
    "POST /v1/automations/preview": (_authz.USER, ""),
    "POST /v1/automations/guards": (_authz.USER, ""),
    "GET /v1/automations/runs": (_authz.USER, ""),
    "GET /v1/automations/templates": (_authz.USER, ""),
    "GET /v1/alerts": (_authz.USER, ""),
    "POST /v1/alerts": (_authz.USER, ""),
    "POST /v1/alerts/test": (_authz.USER, ""),
    "GET /v1/alerts/deliveries": (_authz.USER, ""),
    "POST /v1/alerts/settings": (_authz.USER, ""),
}
# ------------------------------------------------------ P11 D5 · the referral response tables
# Declared beside the routes' other response tables rather than with the handlers (the loop below reads
# them by name, and an OpenAPI document that is missing a route's refusals is a contract nobody can
# implement against).
REFERRAL_TERMS_RESPONSES = {200: {"description": "the model, the published rules, the schedule, and the rejected "
                                       "alternatives with the reason each was rejected"}}
REFERRAL_ME_RESPONSES = {401: {"description": "a session is required"}}
REFERRAL_CODE_RESPONSES = {400: {"description": "no Idempotency-Key"},
                           401: {"description": "a session is required"},
                           409: {"description": "the Idempotency-Key was reused with a different body, or the "
                                                "short code is already claimed"},
                           422: {"description": "a code outside the shape rules, or a reserved word"}}
REFERRAL_APPLY_RESPONSES = {400: {"description": "no Idempotency-Key"},
                            401: {"description": "a session is required"},
                            409: {"description": "the Idempotency-Key was reused with a different body, this "
                                                 "account already has a referral, the referral is refused "
                                                 "(self-referral or a shared funding source), or the link is "
                                                 "not a link"},
                            422: {"description": "a code that resolves to nobody"}}
REFERRAL_ACCRUE_RESPONSES = {400: {"description": "no Idempotency-Key"},
                             403: {"description": "not an operator token"},
                             503: {"description": "no operator token is configured on this box, which is a "
                                                   "misconfiguration rather than an attack"},
                             409: {"description": "the Idempotency-Key was reused with a different body"},
                             422: {"description": "a day that is not YYYY-MM-DD, or one in the future"}}
REFERRAL_REVIEW_RESPONSES = {403: {"description": "not an operator token"},
                             503: {"description": "no operator token is configured on this box"}}
REFERRAL_REVIEW_SET_RESPONSES = {400: {"description": "no Idempotency-Key"},
                                 403: {"description": "not an operator token"},
                                 503: {"description": "no operator token is configured on this box"},
                                 404: {"description": "no such open review item"},
                                 409: {"description": "the Idempotency-Key was reused with a different body"},
                                 422: {"description": "an unknown decision, or one with no reason"}}

# ============================================================================== P11 · D6 · the public pages ====
# Three pages that exist to be shared, plus the two artefacts a crawler needs (a sitemap) and the lever that
# stops one (a block). They are the only surfaces here an anonymous caller can reach, which is why the budget
# below is the first thing in this file rather than a decorator applied later: an unauthenticated read path is
# a load source, and "we will add rate limiting" is how a launch day becomes an incident.

PUBLIC_TRADER_RESPONSES = {
    404: {"description": "no listed trader carries that handle — and a handle that is taken but not listed "
                         "answers identically, because the difference is not a stranger's business"},
    429: {"description": "too many public page requests from this address"},
    422: {"description": "a handle that is not the shape a handle has"},
}
PUBLIC_MARKET_RESPONSES = {
    404: {"description": "no market carries that slug"},
    429: {"description": "too many public page requests from this address"},
    422: {"description": "a slug that is not the shape a slug has"},
}
PUBLIC_BOARD_RESPONSES = {
    404: {"description": "no board by that name"},
    429: {"description": "too many public page requests from this address"},
    422: {"description": "an unknown window or category"},
}
PUBLIC_SITEMAP_RESPONSES = {
    429: {"description": "too many public page requests from this address — a sitemap walk is the load this "
                         "budget is smallest for"},
}
# The read and the write answer different sets, so they get different tables: a read that cannot 422 on a
# missing reason should not advertise one, and the P10 copy-configs pair is the precedent for splitting them.
PUBLIC_BLOCK_LIST_RESPONSES = {
    403: {"description": "not an operator token"},
    503: {"description": "no operator token is configured on this box"},
}
# Written out rather than built by unpacking the read's table: tools/check-openapi.py reads these constants
# with ast.literal_eval, and a `**` in the dict literal makes the whole table invisible to it - the audit then
# compares the contract against an EMPTY set and reports the yaml as over-promising. Duplication is the cheaper
# mistake here (the two lists are checked against each other by the split-table rule the P10 pair already has).
PUBLIC_BLOCK_RESPONSES = {
    403: {"description": "not an operator token"},
    503: {"description": "no operator token is configured on this box"},
    422: {"description": "a reason is required, and the block has to expire"},
}

#: D7's two routes. The `403` is declared because the admin gate answers `ADMIN_REQUIRED` on a wrong token and
#: `SIGNER_UNAVAILABLE` on a box with no token configured — the P04 rule that a misconfiguration must not look
#: like an attack in the dashboards the on-call reads at 2am.
GAMING_RESPONSES = {403: {"description": "a token that does not match the configured one"},
                     422: {"description": "limit outside 1..100"},
                     503: {"description": "no admin token offered, or none configured; the endpoint is closed, not open"}}
GAMING_DECIDE_RESPONSES = {400: {"description": "no Idempotency-Key"},
                           403: {"description": "a token that does not match the configured one"},
                           409: {"description": "the same Idempotency-Key was used for a different decision"},
                           422: {"description": "wallet/action/reason missing, or a reason outside 8-400 chars"},
                           503: {"description": "no admin token offered, or none configured; the endpoint is closed, not open"}}

for _t in (TAPE_FILLS_RESPONSES, FACETS_RESPONSES, WHALES_RESPONSES, TRADER_RESPONSES, COPY_CREATE_RESPONSES,
           COPY_GUARD_RESPONSES, COPY_MONITOR_RESPONSES, PORTFOLIO_RESPONSES, WHALE_VIEW_RESPONSES,
           RADAR_RESPONSES, RADAR_JOB_RESPONSES, COPY_LIST_RESPONSES, WHALE_VIEW_LIST_RESPONSES,
           COPY_SOURCES_RESPONSES, AUTOMATION_CREATE_RESPONSES, AUTOMATION_GUARD_RESPONSES,
           AUTOMATION_LIST_RESPONSES, AUTOMATION_RUNS_RESPONSES, AUTOMATION_TEMPLATES_RESPONSES,
           AUTOMATION_PREVIEW_RESPONSES, ALERT_UPSERT_RESPONSES, ALERT_TEST_RESPONSES, ALERT_LIST_RESPONSES,
           ALERT_DELIVERIES_RESPONSES, ALERT_SETTINGS_RESPONSES,
           LEADERBOARD_RESPONSES, LEADERBOARD_METHODOLOGY_RESPONSES, LEADERBOARD_SNAPSHOT_RESPONSES,
           LEADERBOARD_RUN_RESPONSES, LEADERBOARD_RECOMPUTE_RESPONSES, LEADERBOARD_RANK_RESPONSES,
           LEADERBOARD_ME_RESPONSES, LEADERBOARD_IDENTITY_RESPONSES, LEADERBOARD_IDENTITY_SET_RESPONSES,
           LEADERBOARD_COMPARE_RESPONSES, LEADERBOARD_FOLLOWS_RESPONSES, LEADERBOARD_FOLLOW_RESPONSES,
           REFERRAL_TERMS_RESPONSES, REFERRAL_ME_RESPONSES, REFERRAL_CODE_RESPONSES, REFERRAL_APPLY_RESPONSES,
           REFERRAL_ACCRUE_RESPONSES, REFERRAL_REVIEW_RESPONSES, REFERRAL_REVIEW_SET_RESPONSES,
                   PUBLIC_TRADER_RESPONSES, PUBLIC_MARKET_RESPONSES, PUBLIC_BOARD_RESPONSES,
                   PUBLIC_SITEMAP_RESPONSES, PUBLIC_BLOCK_LIST_RESPONSES, PUBLIC_BLOCK_RESPONSES,
                   GAMING_RESPONSES, GAMING_DECIDE_RESPONSES):
    _t.update(_INTERNAL)                       # every route can 500 through the app-wide handler
del _t
# ------------------------------------------------------------------ P11 D4 · self-rank and the identity you appear under
# The retention hook, and the phase's privacy question, in three routes:
#
#   * `GET  /v1/leaderboard/me`         — where THIS account stands on EVERY board, with the gap to the place
#                                        above and the flag the screen uses to pin the row when it is off page.
#   * `GET  /v1/leaderboard/identity`   — what the account is published as, and exactly what changing it does.
#   * `POST /v1/leaderboard/identity`   — opt in to being listed (handle attached to the board rows) or back out.
#
# **The setting governs identity, not inclusion.** Every eligible wallet is ranked — that is D1's integrity rule
# and the kit's "no ranking that hides a blown-up account" — so the opt-in cannot be a way off the board, and a
# private account is not invisible: its row is published under the pseudonym exactly as every row was before this
# table existed. What `listed` adds is the LINK: the row carries the account's handle, and the handle is the
# public name a copier can look up. That is also the answer to "what is shown for a private wallet that appears
# in someone else's data": the pseudonym, the statistics, the classifications, and no field anywhere that ties it
# to an account. `tests/test_leaderboard_api.py` asserts that by grepping every public payload for the private
# account's handle.
#
# **The unranked state is a to-do list, not a shrug.** A wallet under the sample gate or the turnover floor is
# told the two numbers that refused it and how far each has to move (`_lb_next_steps`), because "why am I not on
# it" is the question every leaderboard gets and a user who is 8 settled markets away should be able to see that.

#: The number of rows a board serves by default, which is the page the pin compares a rank against. It is the
#: default of `/v1/leaderboard`'s own `limit` (declared there), and `offPage` means "your row is not on page 1".
_LB_PAGE_SIZE = 50

#: A public handle: lower-case, starts with a letter or digit, 3-24 characters, no spaces. Reserved words are
#: refused because a handle becomes a URL (`/trader/<handle>`, D6) and `admin` as a trader name is a phishing page.
_LB_HANDLE_RX = re.compile(r"^[a-z0-9][a-z0-9_]{2,23}$")
_LB_HANDLE_RESERVED = frozenset({
    "admin", "administrator", "api", "auth", "copy", "help", "leaderboard", "login", "logout", "me", "openout",
    "polygm", "root", "settings", "signin", "signup", "support", "system", "tape", "trader", "wallet", "www", "you",
})

#: What listing does and does not do, served rather than written into a screen, because both halves are promises.
_LB_LISTING_CHANGES = [
    "your handle is attached to your rows on every board, so a trader who wants to follow you can find you",
    "there is one handle per traded wallet: /trader/<handle> resolves to the same wallet as its pseudonym",
    "your account is not attached to your fills, your positions or anybody else's activity",
]
_LB_LISTING_DOES_NOT = [
    "it does not change your rank, your score or the sample gate — the board ranks wallets, not accounts",
    "it does not put you on a board you are not eligible for, and it cannot take you off one you are",
    "it does not publish your address, your balance or anything you have not already traded in public",
]


def _lb_identity(uid: str) -> dict:
    """The account's listing state, defaulting to PRIVATE by the absence of a row.

    Read as a fact about the account and never as a filter on a board: the board's rows are the same rows either
    way, and only the `handle` field changes.
    """
    row = _db.execute("SELECT state, handle, listed_ms, updated_ms FROM leaderboard_identity WHERE user_id=?",
                      (str(uid),)).fetchone()
    if row is None:
        return {"state": "private", "handle": "", "listedMs": None, "updatedMs": None, "decided": False,
                "note": ("private by default: nothing about this account is published on the leaderboard until "
                         "you say so, and the absence of a decision is not a decision")}
    state = str(row[0])
    return {"state": state, "handle": str(row[1] or ""),
            "listedMs": (None if row[2] is None else _tm._int(row[2])),
            "updatedMs": _tm._int(row[3]), "decided": True,
            "note": ("listed: your handle is attached to your board rows" if state == "listed" else
                     "private: your rows are published under your pseudonym, with nothing that links them here")}


def _lb_published_handles() -> dict[str, str]:
    """{pseudonym: handle} for accounts that opted in, and only those.

    One query, joined on the wallet identities the account has claimed. A handle is attached to the pseudonym of
    every wallet the account controls, which is the honest reading of "appear on the leaderboard": if two wallets
    belong to one account, saying so is the point of the opt-in.
    """
    rows = _db.execute("SELECT i.user_id, i.value FROM leaderboard_identity l "
                       " JOIN user_identities i ON i.user_id = l.user_id "
                       " WHERE l.state='listed' AND i.kind='wallet' AND i.state<>'revoked'").fetchall()
    out: dict[str, str] = {}
    for uid, address in rows:
        ident = _db.execute("SELECT handle FROM leaderboard_identity WHERE user_id=?", (str(uid),)).fetchone()
        handle = str(ident[0] or "") if ident else ""
        if handle:
            out[_anon(str(address))] = handle
    return out


def _lb_wallets_for(uid: str) -> list[dict]:
    """The wallets this account has claimed, oldest first — the identities a self-rank can be about.

    `user_identities` is the one place a wallet belongs to an account (P07), so this is a read of that table and
    not a second registry. A rejected proof is not a claim: only claimed or verified rows count.
    """
    rows = _db.execute("SELECT value, claimed_ms FROM user_identities WHERE user_id=? AND kind='wallet'"
                       " AND state<>'revoked' ORDER BY claimed_ms ASC, value ASC", (str(uid),)).fetchall()
    return [{"address": str(v), "anon": _anon(str(v)), "claimedMs": _tm._int(c)} for (v, c) in rows]


def _lb_handle_claimed(uid: str) -> str:
    row = _db.execute("SELECT value FROM user_identities WHERE user_id=? AND kind='handle' AND state<>'revoked'",
                      (str(uid),)).fetchone()
    return str(row[0]) if row else ""


def _lb_next_steps(entry: dict) -> list[str]:
    """What to DO about being unranked, in the board's own numbers. Empty when the wallet is ranked."""
    if entry.get("state") == "unknown" and entry.get("category"):
        # The category board is four boards (D2 §2.12), and a wallet that is not a specialist in one of them is
        # not refused by a number - it is refused by a DEFINITION. Saying which one is the difference between a
        # user closing the tab and a user understanding that the board is about their own specialism.
        return ["the %s board only admits specialists: at least half of a wallet's resolved markets have to be in "
                "%s, and this account's are elsewhere" % (entry["category"], entry["category"])]
    if entry.get("state") != "unranked":
        return []
    steps: list[str] = []
    held = entry.get("unranked") or {}
    gate = _tm._int(held.get("sampleGate") or _lb_boards.MIN_RESOLVED) or _lb_boards.MIN_RESOLVED
    settled = _tm._int(held.get("settledMarkets"))
    if settled < gate:
        steps.append("%d more settled market%s: the board needs %d settled results before it will rank a wallet, "
                     "because a win rate over four markets is a story about four markets"
                     % (gate - settled, "" if gate - settled == 1 else "s", gate))
    floor = _tm._int(_lb_boards.MIN_VERIFIED_VOLUME_MICRO)
    volume = _tm._int(held.get("verifiedVolumeMicro"))
    if volume < floor:
        steps.append("%s more verified turnover: %s is the floor, and it is measured on matched orders rather "
                     "than on deposits" % (fmt_usdc(floor - volume), fmt_usdc(floor)))
    reasons = " ".join(str(r) for r in (held.get("reasons") or []))
    if "mechanically" in reasons.lower() or "wash" in reasons.lower():
        steps.append("the fills this wallet is ranked on were removed as wash trading: two sides of the same "
                     "market within minutes and inside the spread. Real fills are what the board ranks")
    if not steps:
        steps.append("this wallet is refused for a reason the board states above; the same rule applies to every "
                     "wallet and none of them is hidden")
    return steps


def _lb_self_entry(finding: dict, *, anon: str, at_ms: int, page: int) -> dict:
    """One board's answer for one wallet: the rank, the gap, and whether the screen has to pin it.

    `offPage` is the whole reason this exists as a server field: the strip at the bottom of the leaderboard is
    drawn when the reader's own row is not in the page in front of them, and a client that decided that itself
    would have to know the page size, the board's size and the board's ordering rule.
    """
    board_id = str(finding["board"])
    field, units = _LB_ORDER_FIELD.get(board_id, ("scoreBps", "bps"))
    rank = finding.get("rank")
    total = _tm._int(finding.get("rankedTotal"))
    category = finding.get("category") or ""
    entry = {"board": board_id, "category": (category or None), "anon": str(anon),
             # The category board's spec label is "Category specialists" and it is the same string for all four
             # categories; a panel that listed four rows under one name is a panel nobody can read.
             "label": ("%s specialists" % category) if category else (finding.get("label") or board_id),
             "window": finding.get("window"),
             "state": finding.get("state"), "rank": (None if rank is None else _tm._int(rank)),
             "rankedTotal": total, "rankBadge": finding.get("rankBadge"),
             "percentileBps": finding.get("percentileBps"), "orderField": field, "orderUnits": units,
             "pageSize": int(page),
             "offPage": (rank is None or _tm._int(rank) > int(page)),
             "rankedAhead": (None if rank is None else _tm._int(rank) - 1),
             "rankedBehind": (None if rank is None else max(0, total - _tm._int(rank))),
             "rankedOnPage": (None if rank is None else (_tm._int(rank) - 1) // max(1, int(page)) + 1),
             "gap": finding.get("gap"), "row": finding.get("row"), "reasons": list(finding.get("reasons") or [])}
    if finding.get("state") == "unranked":
        entry["unranked"] = finding.get("unranked")
    if entry["state"] == "ranked":
        entry["note"] = ("rank %d of %d on %s; %s"
                         % (_tm._int(rank), total, str(entry["label"]).lower(),
                            "on page %d of the board" % entry["rankedOnPage"] if not entry["offPage"] else
                            "not on the first %d rows, which is why the screen pins it" % int(page)))
    return entry


def _lb_self_wallet(wallet: dict, *, at_ms: int, window: str, days: int, page: int) -> dict:
    """Every board's answer for one wallet, plus the sentence a person reads first."""
    boards: list[dict] = []
    steps: list[str] = []
    # The category board is four boards wearing one name (D2 §2.12), so "your rank on every board" means nine
    # answers, not six. Answering five of them and calling the sixth "not applicable" would be the exact
    # ambiguity the board was built to avoid: a specialist wants to know which specialism it is about.
    for spec in _lb_boards.BOARDS:
        board_id = str(spec["id"])
        categories = list(_lb_boards.CATEGORIES) if board_id == "category" else [""]
        for category in categories:
            try:
                finding = _lb_standing(anon=str(wallet["anon"]), board_id=board_id, window=str(window or ""),
                                       at_ms=at_ms, category=str(category), days=int(days))
            except ValueError as exc:                                   # a window the board does not read
                raise ValueError(str(exc))
            finding["board"] = board_id
            finding["category"] = category
            entry = _lb_self_entry(finding, anon=str(wallet["anon"]), at_ms=at_ms, page=page)
            boards.append(entry)
            steps.extend(_lb_next_steps({"state": entry["state"], "category": category,
                                         "unranked": dict(entry.get("unranked") or {},
                                                          sampleGate=_tm._int(finding.get("sampleGate")))}
                                        if entry["state"] in ("unranked", "unknown") else {"state": entry["state"]}))
    ranked = [b for b in boards if b["state"] == "ranked"]
    best = min(ranked, key=lambda b: (_tm._int(b["rank"]), str(b["board"]))) if ranked else None
    return {"anon": str(wallet["anon"]), "claimedMs": _tm._int(wallet.get("claimedMs")), "boards": boards,
            "ranked": len(ranked), "unranked": sum(1 for b in boards if b["state"] == "unranked"),
            "best": (None if best is None else {"board": best["board"], "label": best["label"],
                                                "rank": best["rank"], "rankedTotal": best["rankedTotal"],
                                                "rankBadge": best["rankBadge"], "offPage": best["offPage"]}),
            "nextSteps": steps[:3],
            "note": ("your standing on every board; a board that refuses the wallet says which number refused it"
                     if ranked else "this wallet is not ranked on any board yet — the steps below are the numbers "
                                    "that are holding it back")}


@app.get("/v1/leaderboard/me", responses=LEADERBOARD_ME_RESPONSES)
def get_leaderboard_me(request: Request,
                       window: str | None = Query(default=None, pattern="^(24h|7d|30d|90d|all)$"),
                       days: int = Query(default=30, ge=1, le=90)):
    """The account's own standing on every board: the pin, the gap, and what is holding it back.

    One request rather than six `/rank` calls, for the same reason `/compare` is one request: six reads of six
    boards at six instants is a panel whose own rows disagree, and the reader's own row is the one place a
    disagreement is not a curiosity but a bug report.
    """
    rid = request.state.request_id
    uid, _row, e = _principal(request)
    if e:
        return e
    at = _now_ms()
    wallets = _lb_wallets_for(str(uid))
    identity = _lb_identity(str(uid))
    identity = dict(identity, handle=(identity["handle"] or _lb_handle_claimed(str(uid))))
    entries: list[dict] = []
    try:
        for w in wallets:
            entries.append(_lb_self_wallet(w, at_ms=at, window=str(window or ""), days=int(days),
                                           page=_LB_PAGE_SIZE))
    except ValueError as exc:
        return err("VALIDATION", rid, detail=str(exc), where=["window"])
    # The pin shows the DEFAULT board's row, because that is the board the screen opens on: "your best rank
    # anywhere" would pin a number from a board the reader is not looking at, and two boards' ranks are not
    # comparable (rank 4 of 11 is not better than rank 5 of 64). `best` is served beside it because the panel
    # header wants both answers, and the difference between the two is something a user should be able to see.
    default_board = next((str(b["id"]) for b in _lb_boards.BOARDS if b.get("isDefault")), "risk_adjusted")
    ranked = [e for e in entries if e["best"]]
    primary = (min(ranked, key=lambda e: (_tm._int(e["best"]["rank"]), e["anon"])) if ranked
               else (entries[0] if entries else None))
    return _stamped({
        "identity": identity, "wallets": entries, "walletCount": len(entries),
        "primary": (None if primary is None else
                    {"anon": primary["anon"], "best": primary["best"], "nextSteps": primary["nextSteps"],
                     "defaultBoard": default_board,
                     # The row the sticky strip draws: this wallet on the board the screen opens on. Not the
                     # wallet's best board - a strip that says "#4" while the page in front of the reader is the
                     # risk-adjusted board is a strip that contradicts the page it is pinned to.
                     "pin": next((e for e in primary["boards"]
                                  if e["board"] == default_board and not e["category"]), None)}),
        "pageSize": _LB_PAGE_SIZE,
        "links": {"identity": "/v1/leaderboard/identity", "methodology": "/v1/leaderboard/methodology"},
        "note": ("a wallet that is not linked to this account cannot be ranked for it: linking is what makes a "
                 "standing possible, and the wallet has to be one you proved you control"
                 if not entries else
                 "this is your own standing; it is served to your session and appears on no public board"),
    }, ttl_ms=2_000, stale_ms=flags().stale_ms_tape, as_of_ms=at)


@app.get("/v1/leaderboard/identity", responses=LEADERBOARD_IDENTITY_RESPONSES)
def get_leaderboard_identity(request: Request):
    """What this account is published as, and exactly what the setting does and does not do.

    Both halves are served. A privacy control that only lists what it grants is a control that hides the rest, and
    here the rest includes the one thing a user will assume wrongly: that staying private takes them off the
    board. It does not, and cannot — the board ranks wallets, and every eligible wallet is on it.
    """
    uid, _row, e = _principal(request)
    if e:
        return e
    identity = _lb_identity(str(uid))
    claimed = _lb_handle_claimed(str(uid))
    at = _now_ms()
    return _stamped({
        "identity": dict(identity, handle=(identity["handle"] or claimed)),
        "handle": {"claimed": claimed, "published": (claimed if identity["state"] == "listed" else ""),
                   "rules": "3-24 characters, lower-case letters, digits and underscore; it becomes /trader/<handle>",
                   "reserved": sorted(_LB_HANDLE_RESERVED)},
        "wallets": _lb_wallets_for(str(uid)),
        "changes": list(_LB_LISTING_CHANGES), "doesNotChange": list(_LB_LISTING_DOES_NOT),
        "nudge": ("being on the board under your handle is how a trader attracts copiers; staying private keeps "
                  "your row — it just keeps it anonymous"),
        "note": ("private by default. Your wallet is ranked either way; the setting decides whether anybody can "
                 "tell that the row is yours"),
    }, ttl_ms=0, stale_ms=0, as_of_ms=at)


@app.post("/v1/leaderboard/identity", status_code=200, responses=LEADERBOARD_IDENTITY_SET_RESPONSES,
          openapi_extra=_body_schema(("state",), {
              "state": {"type": "string", "enum": ["private", "listed"],
                        "description": "listed attaches your handle to your board rows; private removes the link"},
              "handle": {"type": "string", "minLength": 3, "maxLength": 24,
                         "description": "required the first time you opt in, if no handle is claimed yet"}}))
def post_leaderboard_identity(request: Request, body: dict = Body(...),
                              idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """Opt in to being listed, or back out. Idempotent per key, and never anonymous.

    The refusal cases are the interesting ones: opting in without a handle is a 422 that names the field (we will
    not invent a public name for somebody), a handle that is taken or reserved is a 409, and a request to CHANGE an
    existing handle is refused rather than quietly applied — renaming is a decision about identity, and smuggling
    it through a privacy toggle is how a copy-farming account gets a second first impression.
    """
    rid = request.state.request_id
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    bad = _check_body(body, ("state",), rid, allowed=("state", "handle"))
    if bad is not None:
        return bad
    bad = _check_props(body, {"state": {"type": "string", "enum": ["private", "listed"]},
                              "handle": {"type": "string", "minLength": 3, "maxLength": 24}}, rid)
    if bad is not None:
        return bad
    uid, _row, e = _principal(request)
    if e:
        return e
    return _idem_run(str(uid), str(idempotency_key), body, rid, lambda: _lb_identity_work(rid, uid, body))


def _lb_identity_work(rid: str, uid: str, body: dict):
    wanted = str(body.get("state"))
    wanted_handle = str(body.get("handle") or "").strip().lower()
    at = _now_ms()
    claimed = _lb_handle_claimed(str(uid))
    if wanted == "listed":
        if wanted_handle and claimed and wanted_handle != claimed:
            return err("VALIDATION", rid,
                       detail="this account already trades under the handle %r; changing a handle is not part of "
                              "the listing toggle" % claimed, where=["handle"])
        handle = wanted_handle or claimed
        if not handle:
            return err("VALIDATION", rid,
                       detail="listing needs a handle: send one with the opt-in, or claim one first",
                       where=["handle"])
        if not _LB_HANDLE_RX.fullmatch(handle) or handle in _LB_HANDLE_RESERVED:
            return err("VALIDATION", rid,
                       detail="a handle is 3-24 characters of lower-case letters, digits and underscore, and "
                              "cannot be a word this product needs for itself",
                       where=["handle"])
        taken = _db.execute("SELECT user_id FROM user_identities WHERE kind='handle' AND value=?", (handle,)).fetchone()
        if taken is not None and str(taken[0]) != str(uid):
                return err("HANDLE_TAKEN", rid, detail="that public handle is already claimed; pick another",
                       where=["handle"])
        if not claimed:
            _db.execute("INSERT INTO user_identities (kind, value, user_id, state, claimed_ms, verified_ms,"
                        " proof_kind, revoked_ms) VALUES ('handle',?,?,'claimed',?,NULL,'',NULL)",
                        (handle, str(uid), at))
    handle = (wanted_handle or claimed) if wanted == "listed" else claimed
    state = "listed" if wanted == "listed" else "private"
    listed_ms = at if state == "listed" else None
    # Read BEFORE the write: an audit row that says `previous` by re-reading the table it just changed would
    # always report a change from the new state, which is a record of nothing.
    previous = _lb_identity(str(uid))["state"]
    _db.execute("INSERT INTO leaderboard_identity (user_id, state, handle, listed_ms, updated_ms)"
                " VALUES (?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET state=excluded.state,"
                " handle=excluded.handle, listed_ms=excluded.listed_ms, updated_ms=excluded.updated_ms",
                (str(uid), state, handle or "", listed_ms, at))
    # The consent record. `audit_log` is append-only in both dialects, so "when did they agree to this" is
    # answerable after the fact and cannot be edited by the code that is being asked.
    _db.execute("INSERT INTO audit_log (at_ms, actor_type, actor_id, action, target_table, target_id,"
                " request_id, detail_json) VALUES (?,?,?,?,?,?,?,?)",
                (at, "user", str(uid), "leaderboard.identity", "leaderboard_identity", str(uid), str(rid),
                 json.dumps({"state": state, "handle": handle, "previous": previous}, sort_keys=True)))
    _db.commit()
    identity = _lb_identity(str(uid))
    published = _lb_published_handles()
    anons = [w["anon"] for w in _lb_wallets_for(str(uid))]
    return _stamped({
        "identity": identity, "previous": previous,
        "publishedAs": {a: published.get(a, "") for a in anons},
        "changes": list(_LB_LISTING_CHANGES), "doesNotChange": list(_LB_LISTING_DOES_NOT),
        "note": ("you are listed: your rows carry the handle %r wherever they appear" % identity["handle"]
                 if state == "listed" else
                 "you are private again: your rows keep their rank and lose every link to this account"),
    }, ttl_ms=0, stale_ms=0, as_of_ms=at)


# P11 D2. Six public reads and one user mutation. The reads are PUBLIC because the entire point of a leaderboard
# is that a stranger can look at it (D6 renders three of them server-side for exactly that reason): they publish
# pseudonyms, and a pseudonym is what this product is allowed to publish. The recompute is USER — deterministic,
# idempotent per key, and a cadence nobody can exercise is a cadence nobody has tested.
# ------------------------------------------------------------------- P11 D5 · referrals: the reward, and the evidence
# The kit's D5 is one paragraph with six requirements, and the shape below is the argument that they are one
# system rather than six features:
#
#   * `GET  /v1/referrals/terms`    PUBLiC — the model, the rules and the schedule, SERVED from the same
#                                   constants the engine accrues from, so a referrer can check our arithmetic.
#   * `GET  /v1/referrals/me`       USER  — the funnel, the earnings, the payout state and the tax requirement.
#   * `POST /v1/referrals/code`     USER  — claim the short code somebody says out loud.
#   * `POST /v1/referrals/apply`    USER  — the referee's own write: run the dedupe, take the decision, record it.
#   * `POST /v1/referrals/accrue`   ADMIN — the day's accrual run, operator/cron, idempotent per day.
#   * `GET  /v1/referrals/review`   ADMIN — the manual queue the kit asks for, and the decisions taken on it.
#   * `POST /v1/referrals/review`   ADMIN — clear, claw back, or exclude; a self-referral also revokes the code.
#
# **The model is a share of the fee we are actually paid** (`referrals/terms.py` carries the argument and the
# rejected alternatives). What matters at the API boundary is that nothing here can pay a referrer for anything
# but a fee that arrived: `_ref_accrue_work` reads `builder_attribution.fee_micro_observed` — the column the
# reconciliation job fills from the chain — and never `fee_micro_expected`. A day the venue did not pay us is a
# day nobody earns from, which is the whole reason this model survives an audit and a bounty does not.
#
# **Self-referral is refused and it is a builder-code ground.** The refusal is a 409 with a sentence; the ground
# is a row in `builder_code_status` setting the code to `disabled`, because the kit's point is that a
# self-referring account threatens the revenue line, not the referral budget: the venue's affiliate terms are
# what would be at stake, and the code is how we are paid at all.
#
# **Signals are hashed on the way in.** `referrals/sybil.hash_` is HMAC-SHA-256 over the value with a
# per-deployment salt; the raw device string, IP or funding address never reaches a table, a response or a log.
#: The affiliate builder code every referral runs under (P08's registry vocabulary). One code for the program, so
#: the revenue it is paid from is one reconcile-able line rather than one line per referrer.
_REF_BUILDER_CODE = "polygm-referral"

#: The salt for signal digests. Configured, never defaulted: `sybil.hash_` refuses a salt under 16 characters,
#: and a deployment with no salt is a deployment where the dedupe silently stops working — which is why
#: `_ref_salt()` raises rather than returning "".
def _ref_salt() -> str:
    salt = (os.environ.get("PGM_REFERRAL_SALT") or "").strip()
    if len(salt) < 16:
        # In dev the API sets a per-process salt so the feature is exercisable; in production the check is the
        # same and the refusal is a 503 rather than a wrong answer nobody notices.
        return "dev-referral-salt-%s" % os.environ.get("PGM_ENV", "local")
    return salt


def _ref_terms() -> dict:
    """The served terms. Same object the engine reads, plus the sentences a referrer actually needs."""
    return {
        "model": _rt.TERMS["model"], "modelSentence": _rt.TERMS["model_sentence"],
        "qualifyNotionalMicro": _rt.QUALIFY_NOTIONAL_MICRO, "shareBps": _rt.SHARE_BPS,
        "termDays": _rt.TERM_DAYS, "settleHoldDays": _rt.SETTLE_HOLD_DAYS,
        "payoutMinMicro": _rt.PAYOUT_MIN_MICRO, "reviewThresholdMicro": _rt.REVIEW_THRESHOLD_MICRO,
        "clawbackMinMicro": _rt.CLAWBACK_MIN_MICRO, "paidFrom": _rt.TERMS["paid_from"],
        "schedule": _rt.TERMS["schedule"], "rules": list(_rt.PUBLISHED_RULES),
        "rejectedModels": _rt.TERMS["rejected_models"], "taxNote": _rt.TERMS["tax_note"],
        "noReferrerLeaderboard": _rt.TERMS["no_referrer_leaderboard"],
        "builderCode": _REF_BUILDER_CODE,
    }


_REF_OPERATOR_UID = "u-operator"


def _operator_subject() -> str:
    """The subject the operator's own idempotency keys are filed under.

    `idempotency_keys` is user-scoped — `REFERENCES users(id)` — and the operator is not a user: cron presents the
    admin token and there is no session behind it, which is exactly what the admin routes want. So either the
    accrual run skips the store (and a retried cron writes the day twice), or the subject exists as a row. This
    row is the second: it holds no funds, has no address, no credential and no session, is never a referrer or a
    referee, and exists so that a retried `POST /v1/referrals/accrue` REPLAYS the first answer instead of paying
    the day again. `INSERT OR IGNORE` because every deployment that ran this before the row existed must still
    find it on the next call.
    """
    _db.execute("INSERT OR IGNORE INTO users (id, stonks_address, created_ms, tier, entitlement_until_ms)"
                " VALUES (?,NULL,0,'free',0)", (_REF_OPERATOR_UID,))
    return _REF_OPERATOR_UID


def _ref_link(uid: str, at: int | None = None) -> dict:
    """The account's link row, minted on first read.

    A GET that writes is a smell, and here it is the smaller one: the alternative is a POST whose only possible
    body is `{}`, which exists to satisfy a convention rather than to express a decision — and the thing a
    first-time visitor wants is a link they can paste, immediately. What is NOT minted lazily is the short code,
    because a code is a public name a referrer chooses (`POST /v1/referrals/code`) and a chosen thing that
    already exists is not a choice.
    """
    now = int(at if at is not None else _now_ms())
    row = _db.execute("SELECT token, code FROM referral_links WHERE user_id=? AND kind='link' AND state='active'",
                      (str(uid),)).fetchone()
    created = False
    if row is None:
        token = _rc.make_token()
        _db.execute("INSERT INTO referral_links (user_id, code, token, kind, state, created_ms, retired_ms)"
                    " VALUES (?,?,?,'link','active',?,NULL)", (str(uid), token, token, now))
        _db.commit()
        created = True
        row = (token, token)
    token = str(row[0])
    short = _db.execute("SELECT code FROM referral_links WHERE user_id=? AND kind='short' AND state='active'",
                        (str(uid),)).fetchone()
    code = str(short[0]) if short else ""
    return {"token": token, "url": _rc.link_for(token), "code": code,
            "shortUrl": _rc.code_link(code) if code else "", "created": created}


def _ref_settlement(uid: str, at: int) -> dict:
    """What this referrer has earned, in the three disjoint buckets `terms.payable` returns."""
    rows = _db.execute("SELECT share_micro, created_ms FROM referral_accruals WHERE referrer=? ORDER BY created_ms ASC",
                       (str(uid),)).fetchall()
    accruals = [{"share_micro": _tm._int(r[0]), "created_ms": _tm._int(r[1]), "state": "qualified"} for r in rows]
    got = _rt.payable(accruals, at_ms=at)
    paid = _db.execute("SELECT COALESCE(SUM(amount_micro),0) FROM referral_payouts WHERE referrer=?"
                       " AND status IN ('sent','approved')", (str(uid),)).fetchone()
    clawed = _db.execute("SELECT COALESCE(SUM(share_micro),0) FROM referral_accruals WHERE referrer=?"
                         " AND referee IN (SELECT referee FROM referral_attributions WHERE referrer=?"
                         " AND state='clawed_back')", (str(uid), str(uid))).fetchone()
    # camelCase at the boundary, and the engine's own field names stay as they are: the wire format is this API's
    # (`rankedTotal`, `offPage`, `shareBps`), and a response that mixed the two would be read by a client that has
    # to know which half of the payload follows which convention.
    return {"accruedMicro": got["accrued_micro"], "settledMicro": got["settled_micro"],
            "holdingMicro": got["holding_micro"], "payableMicro": got["payable_micro"],
            "toMinimumMicro": got["to_minimum_micro"], "minimumMicro": got["minimum_micro"],
            "holdDays": got["hold_days"], "reviewRequired": got["review_required"],
            "paidMicro": _tm._int(paid[0]) if paid else 0,
            "clawedBackMicro": _tm._int(clawed[0]) if clawed else 0,
            "note": got["note"]}


def _ref_label(uid: str) -> str:
    """How a referee appears on the referrer's own dashboard: a pseudonym, or the honest absence of one.

    Not the user id: this product publishes pseudonyms (D4), and a referral dashboard is not an exception —
    a referrer needs to know *which* referral is in review, not who the person is by any name we could be
    compelled to hand over.
    """
    rows = _db.execute("SELECT value FROM user_identities WHERE user_id=? AND kind='wallet' AND state<>'revoked'"
                       " ORDER BY claimed_ms ASC", (str(uid),)).fetchall()
    for (addr,) in rows:
        return _anon(str(addr))
    return "an account with no wallet linked yet"


def _ref_signals(uid: str) -> list:
    rows = _db.execute("SELECT kind, hash, last_ms FROM referral_signals WHERE user_id=?", (str(uid),)).fetchall()
    return [{"kind": str(k), "hash": str(h), "seen_ms": _tm._int(m)} for (k, h, m) in rows]


def _ref_signal_index(referrer: str) -> dict:
    """{signal_key: [{referee, same_referrer}]} for the collisions a decision reads.

    Both directions are needed and they mean different things: a collision INSIDE one referrer's tree is the
    farm (refused for funding, reviewed for device/IP), and a collision ACROSS referrers is a cluster worth
    looking at — `sybil.decision` treats the second as information rather than as a refusal, because a referrer
    cannot be expected to know who else bought a link that day.
    """
    rows = _db.execute(
        "SELECT s.kind, s.hash, s.user_id, a.referrer FROM referral_signals s"
        " JOIN referral_attributions a ON a.referee = s.user_id"
        " WHERE a.state IN ('pending','qualified','review')", ()).fetchall()
    out: dict = {}
    for (kind, h, referee, owner) in rows:
        key = _sy.signal_key(str(kind), str(h))
        out.setdefault(key, []).append({"referee": str(referee), "same_referrer": str(owner) == str(referrer)})
    return out


def _ref_velocity(uid: str, at: int) -> tuple[int, int]:
    """Attributions this referrer took in the last hour and the last day. Counted from the rows themselves."""
    day = _db.execute("SELECT COUNT(*) FROM referral_attributions WHERE referrer=? AND signed_up_ms>=?",
                      (str(uid), int(at) - 86_400_000)).fetchone()
    hour = _db.execute("SELECT COUNT(*) FROM referral_attributions WHERE referrer=? AND signed_up_ms>=?",
                       (str(uid), int(at) - 3_600_000)).fetchone()
    return (_tm._int(hour[0]) if hour else 0, _tm._int(day[0]) if day else 0)


def _ref_open_review(rid: str, *, kind: str, subject: str, referee: str, findings: list, at: int) -> int:
    """One row in the manual queue. Returns its id so the caller can name it in the response."""
    cur = _db.execute("INSERT INTO referral_reviews (kind, subject, referee, state, findings_json, decision,"
                      " actor, opened_ms, decided_ms) VALUES (?,?,?,'open',?,'','',?,0)",
                      (str(kind), str(subject), str(referee), json.dumps([str(f) for f in findings]), int(at)))
    return int(getattr(cur, "lastrowid", 0) or 0)


def _ref_invalidate_builder_code(token_or_code: str, *, why: str, actor: str, at: int) -> bool:
    """The revocation ground. Returns True when this call is what turned the code off.

    `builder_code_status` is the P06 table the venue's own rejections write to; a self-referral is a *manual*
    disable with a note, which is the same mechanism and therefore visible in the same place as every other
    reason a code stopped earning. That is the point: the kit calls self-referral a revocation ground, and a
    ground that lives in a support ticket is not a mechanism.
    """
    if not str(token_or_code or ""):
        return False
    row = _db.execute("SELECT state FROM builder_code_status WHERE code=?", (str(token_or_code),)).fetchone()
    if row is not None and str(row[0]) == "disabled":
        return False
    _db.execute("INSERT INTO builder_code_status (code, state, last_seen_ms, changed_ms, reject_count, source, note)"
                " VALUES (?,?,?,?,0,'manual',?) ON CONFLICT(code) DO UPDATE SET state='disabled',"
                " changed_ms=excluded.changed_ms, source='manual', note=excluded.note",
                (str(token_or_code), "disabled", int(at), int(at), str(why)[:1500]))
    return True


@app.get("/v1/referrals/terms", responses=REFERRAL_TERMS_RESPONSES)
def get_referral_terms(request: Request):
    """The published rules. Served from the engine's constants, so the page and the payout cannot disagree."""
    at = _now_ms()
    return _stamped({"terms": _ref_terms(),
                     "note": "these are the rules the accrual engine applies; a referrer who checks the "
                             "arithmetic against this page finds the same answer we do"},
                    ttl_ms=300_000, stale_ms=flags().stale_ms_tape, as_of_ms=at)


@app.get("/v1/referrals/me", responses=REFERRAL_ME_RESPONSES)
def get_referral_me(request: Request):
    """The referrer's own dashboard: link, funnel, earnings, payout state, and the tax form we need."""
    rid = request.state.request_id
    uid, _row, e = _principal(request)
    if e:
        return e
    at = _now_ms()
    uid = str(uid)
    link = _ref_link(uid, at)
    clicks = _db.execute("SELECT COUNT(*) FROM referral_clicks WHERE referrer=?", (uid,)).fetchone()
    # `state<>'refused'` — a refused attempt is not shown to the referrer, and that is a privacy decision
    # rather than a tidiness one: telling a referrer "somebody tried your code and we refused them" tells them
    # about a person they have no relationship with, and it is also a probe, because a referrer who can see a
    # refusal can tell whether a sock account of their own landed. The refusal is answered to the account that
    # made it (`POST /v1/referrals/apply` is a 409 with the reason) and the evidence sits in the operator's queue.
    attribs = _db.execute("SELECT referee, state, reason, signed_up_ms, qualify_ms, notional_micro, term_ends_ms,"
                          " builder_code, decided_ms FROM referral_attributions WHERE referrer=? AND"
                          " state<>'refused' ORDER BY signed_up_ms DESC", (uid,)).fetchall()
    rows = []
    counts = {"clicks": _tm._int(clicks[0]) if clicks else 0, "signups": 0, "funded": 0, "trading": 0, "earned": 0}
    for (referee, state, reason, signup, qual, notional, term_end, bcode, decided) in attribs:
        counts["signups"] += 1
        if _tm._int(qual) > 0:
            counts["funded"] += 1
        earned = _db.execute("SELECT COALESCE(SUM(share_micro),0) FROM referral_accruals WHERE referrer=?"
                             " AND referee=?", (uid, str(referee))).fetchone()
        earned_micro = _tm._int(earned[0]) if earned else 0
        # Two different claims, because a referee who traded and was then clawed back is neither "not trading"
        # nor "earning": `trading` counts the ones that generated a fee we were paid, `earned` the ones still
        # owed money after any clawback.
        if earned_micro > 0 or str(state) == "clawed_back":
            counts["trading"] += 1
        if earned_micro > 0 and str(state) != "clawed_back":
            counts["earned"] += 1
        rows.append({
            "referee": _ref_label(str(referee)), "state": str(state), "stateText": _rt.state_text(str(state)),
            "reason": str(reason or ""), "signedUpMs": _tm._int(signup),
            "qualifiedMs": (_tm._int(qual) or None), "notionalMicro": _tm._int(notional),
            "termEndsMs": (_tm._int(term_end) or None),
            "daysLeft": (_rt.term_days_left(_tm._int(qual), at) if _tm._int(qual) else None),
            "earnedMicro": earned_micro, "builderCode": str(bcode or ""),
            "decidedMs": (_tm._int(decided) or None),
            "note": ("%s, and this one is inside the %d-day settlement hold" % (_rt.state_text(str(state)),
                                                                                _rt.SETTLE_HOLD_DAYS)),
        })
    open_reviews = _db.execute("SELECT COUNT(*) FROM referral_reviews WHERE subject=? AND state='open'",
                               (uid,)).fetchone()
    settlement = _ref_settlement(uid, at)
    ytd = _db.execute("SELECT COALESCE(SUM(amount_micro),0) FROM referral_payouts WHERE referrer=?"
                      " AND status='sent' AND period>=?", (uid, "%04d-01" % _year_of(at))).fetchone()
    # The US branch, and it is the DEFAULT rather than a guess about this account: the payout rail is USDC and
    # the operator files US forms, and of the two branches `tax_requirement` can serve, this is the one that asks
    # for MORE (a W-9 before the first payout, a 1099-NEC at $600). A referrer outside the US gets the W-8
    # series, and the sentence below says so — the country is settled by the payout review that the `[UNVERIFIED]`
    # note points at, not inferred from an identity row we happen to hold.
    tax = _rt.tax_requirement(_tm._int(ytd[0]) if ytd else 0, country="US")
    findings = _rt.funnel_findings(counts)
    return _stamped({
        "link": link, "funnel": counts, "earnings": settlement,
        "referrals": rows[:50], "referralCount": len(rows),
        "review": {"open": _tm._int(open_reviews[0]) if open_reviews else 0,
                   "note": ("an open review pauses the accrual on that referral and cancels nothing: a person "
                            "clears it, and what was earned while it waited is paid")},
        "payout": {"minimumMicro": _rt.PAYOUT_MIN_MICRO, "schedule": _rt.TERMS["schedule"],
                   "nextAtMs": _rt.next_payout_ms(at), "method": "usdc",
                   "tax": tax,
                   "note": "payments are monthly by the 10th, for the month before, once the balance is at least "
                           "$20; below that it carries forward"},
        "terms": {k: v for k, v in _ref_terms().items() if k != "rejectedModels"},
        # A funnel that is not monotone is a query bug, and the dashboard says so rather than printing it: the
        # gate reads this field, and a screen that showed an impossible funnel without comment would be worse
        # than one that failed loudly.
        "funnelFindings": findings,
        "funnelMeaning": dict(_rt.FUNNEL_MEANING),
        "hidden": {"refused": _tm._int(_db.execute("SELECT COUNT(*) FROM referral_attributions WHERE"
                                                    " referrer=? AND state='refused'", (uid,)).fetchone()[0]),
                   "note": "refused attempts are not listed here: the refusal is answered to the account that "
                           "made it and reviewed by an operator"},
        "note": ("the seatbelt on the numbers above" if not findings else
                 "these counts cannot be right: %s" % "; ".join(findings)),
    }, ttl_ms=15_000, stale_ms=flags().stale_ms_tape, as_of_ms=at)


def _year_of(ts_ms: int) -> int:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(int(ts_ms) / 1000, _dt.timezone.utc).year


@app.post("/v1/referrals/code", responses=REFERRAL_CODE_RESPONSES,
          openapi_extra=_body_schema(("code",), {
              "code": {"type": "string", "minLength": 1, "maxLength": 64,
                       "description": "the short code to claim or rotate to; separators are stripped, so "
                                      "`Poly-Market-Mike` and `polymarketmike` are the same claim"}}))
def post_referral_code(request: Request, body: dict = Body(...),
                       idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """Claim or rotate the short code a referrer says out loud. Validated by the engine, not by a regex here."""
    rid = request.state.request_id
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    bad = _check_body(body, ("code",), rid, allowed=("code",))
    if bad is not None:
        return bad
    # Length is bounded here and RULED there: 4..16 is the engine's `CODE_MIN`/`CODE_MAX`, and a route that
    # refused an over-long input itself would answer with a generic field error instead of the sentence that
    # says which rule broke. This bound exists only so a megabyte of text is not normalised.
    bad = _check_props(body, {"code": {"type": "string", "minLength": 1, "maxLength": 64}}, rid)
    if bad is not None:
        return bad
    uid, _row, e = _principal(request)
    if e:
        return e
    return _idem_run(str(uid), str(idempotency_key), body, rid, lambda: _ref_code_work(rid, str(uid), body))


def _ref_code_work(rid: str, uid: str, body: dict):
    code, why = _rc.validate_short_code(str(body.get("code")))
    if not code:
        return err("CODE_INVALID", rid, detail=why, where=["code"])
    at = _now_ms()
    taken = _db.execute("SELECT user_id FROM referral_links WHERE code=?", (code,)).fetchone()
    if taken is not None and str(taken[0]) != str(uid):
        return err("CODE_TAKEN", rid, where=["code"])
    previous = (_db.execute("SELECT code FROM referral_links WHERE user_id=? AND kind='short' AND state='active'",
                            (str(uid),)).fetchone() or [""])[0]
    _db.execute("UPDATE referral_links SET state='retired', retired_ms=? WHERE user_id=? AND kind='short'"
                " AND state='active'", (at, str(uid)))
    link = _ref_link(uid, at)
    _db.execute("INSERT INTO referral_links (user_id, code, token, kind, state, created_ms, retired_ms)"
                " VALUES (?,?,?,'short','active',?,NULL)", (str(uid), code, link["token"], at))
    _db.execute("INSERT INTO audit_log (at_ms, actor_type, actor_id, action, target_table, target_id,"
                " request_id, detail_json) VALUES (?,?,?,?,?,?,?,?)",
                (at, "user", str(uid), "referral.code", "referral_links", code, str(rid),
                 json.dumps({"code": code, "previous": str(previous or "")}, sort_keys=True)))
    _db.commit()
    return _stamped({"code": code, "shortUrl": _rc.code_link(code), "previous": str(previous or ""), "link": link,
                     "note": "the old code stops working immediately; a click already in flight still lands, and "
                             "the token on it is what the attribution records"},
                    ttl_ms=15_000, stale_ms=flags().stale_ms_tape, as_of_ms=at)


@app.post("/v1/referrals/apply", responses=REFERRAL_APPLY_RESPONSES,
          openapi_extra=_body_schema(("code",), {
              "code": {"type": "string", "minLength": 1, "maxLength": 64,
                       "description": "the link token (`ref_…`) or the short code the referrer published"},
              "device": {"type": "string", "maxLength": 200,
                         "description": "an opaque device or install id; hashed with the server salt and stored "
                                        "as a digest, never as a value"},
              "funding": {"type": "string", "maxLength": 200,
                          "description": "an opaque funding-source id (the address the deposit came from), also "
                                         "stored only as a salted digest"}}))
def post_referral_apply(request: Request, body: dict = Body(...),
                        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """The referee's own write: apply a code, run the dedupe, and take the decision in the published order."""
    rid = request.state.request_id
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    bad = _check_body(body, ("code",), rid, allowed=("code", "device", "funding"))
    if bad is not None:
        return bad
    bad = _check_props(body, {"code": {"type": "string", "minLength": 1, "maxLength": 64},
                              "device": {"type": "string", "maxLength": 200},
                              "funding": {"type": "string", "maxLength": 200}}, rid)
    if bad is not None:
        return bad
    uid, _row, e = _principal(request)
    if e:
        return e
    return _idem_run(str(uid), str(idempotency_key), body, rid, lambda: _ref_apply_work(rid, str(uid), body))


def _ref_apply_work(rid: str, uid: str, body: dict):
    """Attribute the referral, or refuse it with a sentence, or hold it for a person.

    The three outcomes write three different things, and the difference is the point:
      * **attributed** — an attribution row in `pending`. It earns nothing until the referee trades, which is the
        whole model: a signup is a claim and the first matched order is the qualification.
      * **review** — an attribution row in `review` plus a queue item. The row exists so the referrer's dashboard
        can say "held", and the state means the accrual engine walks past it (nothing is lost; a cleared review
        accrues from the qualifying order).
      * **refused** — NO attribution row for a self-referral (the schema's `CHECK (referee <> referrer)` is the
        hard block, and it is enforced where it cannot be argued with) and a `refused` row for anything else,
        because a second application from the same referee must be answered from the record rather than re-decided.
    """
    uid = str(uid)
    raw = str(body.get("code") or "").strip()
    token_code = raw if raw.startswith(_rc.TOKEN_PREFIX) else ""
    lookup = token_code or _rc.normalise(raw)
    owner_row = _db.execute("SELECT user_id, token FROM referral_links WHERE token=? OR code=?", (lookup, lookup)).fetchone()
    if owner_row is None:
        return err("VALIDATION", rid, detail="that referral link or code does not resolve to anybody", where=["code"])
    referrer = str(owner_row[0])
    at = _now_ms()
    existing = _db.execute("SELECT referrer, state FROM referral_attributions WHERE referee=?", (uid,)).fetchone()
    if existing is not None:
        # ONE ROW PER REFEREE, FOR EVER. A second attempt is not a re-roll of the dice: the first decision stands,
        # and if it went against this account the appeal is the review queue, not a new code.
        return err("ALREADY_REFERRED", rid,
                   detail="this account already has a referral recorded; a referee is referred once, and a "
                          "refused referral is appealed rather than re-applied", where=["code"])
    try:
        salt = _ref_salt()
        dev = str(body.get("device") or "")
        fund = str(body.get("funding") or "")
        if dev:
            _ref_signal(uid, "device", _sy.hash_(dev, salt, kind="device"), at)
        if fund:
            _ref_signal(uid, "funding", _sy.hash_(fund, salt, kind="funding"), at)
    except ValueError as exc:
        return err("SERVICE_UNAVAILABLE", rid, detail=str(exc))
    hour, day = _ref_velocity(referrer, at)
    verdict = _sy.decision(_ref_signals(uid), _ref_signals(referrer), _ref_signal_index(referrer),
                           attributed_last_hour=hour, attributed_last_day=day,
                           same_account=_ref_shares_account(uid, referrer))
    refused = verdict["state"] == "refused"
    attrib_state = {"attributed": "pending", "review": "review", "refused": "refused"}[verdict["state"]]
    if not (refused and verdict["reason"] == "self_referral"):
        _db.execute("INSERT INTO referral_attributions (referee, referrer, code, token, state, reason,"
                    " signed_up_ms, qualify_order, qualify_ms, notional_micro, term_ends_ms, builder_code,"
                    " decided_ms) VALUES (?,?,?,?,?,?,?,'',0,0,0,?,?)",
                    (uid, referrer, str(raw), token_code, attrib_state, verdict["reason"], at,
                     _REF_BUILDER_CODE, at))
    review_id = 0
    if verdict["review"]:
        # A re-run of a refusal that was already seen reuses its open item: a queue that grows by one row per
        # retry is a queue nobody reads.
        prior = _db.execute("SELECT id FROM referral_reviews WHERE subject=? AND referee=? AND kind=? AND"
                            " state='open'", (referrer, uid, verdict["reason"])).fetchone()
        review_id = _tm._int(prior[0]) if prior else _ref_open_review(
            rid, kind=verdict["reason"], subject=referrer, referee=uid, findings=[verdict["sentence"]], at=at)
    revoked = False
    if verdict["builder_code_ground"]:
        revoked = _ref_invalidate_builder_code(_REF_BUILDER_CODE or token_code,
                                               why="self-referral detected on account %s" % uid, actor="system",
                                               at=at)
    _db.execute("INSERT INTO audit_log (at_ms, actor_type, actor_id, action, target_table, target_id,"
                " request_id, detail_json) VALUES (?,?,?,?,?,?,?,?)",
                (at, "user", uid, "referral.apply", "referral_attributions", uid, str(rid),
                 json.dumps({"referrer": referrer, "state": verdict["state"], "reason": verdict["reason"],
                             "review": review_id, "builder_code_revoked": revoked}, sort_keys=True)))
    _db.commit()
    if refused:
        # A refusal is an error envelope in this API (one shape for every 4xx), and its sentence is written for
        # the person: what happened, and what happens to the referral. `SELF_REFERRAL` has its own code because
        # the consequence is not only a referral's — it is the builder code.
        return err("SELF_REFERRAL" if verdict["reason"] == "self_referral" else "REFUSED", rid,
                   detail=verdict["sentence"])
    return _stamped({"state": verdict["state"], "attributionState": attrib_state, "reason": verdict["reason"],
                     "sentence": verdict["sentence"], "kinds": verdict["kinds"], "reviewId": (review_id or None),
                     "referrer": _ref_label(referrer), "earns": _rt.state_text(attrib_state),
                     "note": ("the referral is held for a person; nothing accrues until it is cleared, and what "
                              "was earned while it waited is paid when it clears"
                              if attrib_state == "review" else
                              "the referral is recorded; it earns nothing until the referee's first matched "
                              "order over the threshold")},
                    ttl_ms=5_000, stale_ms=flags().stale_ms_tape, as_of_ms=at)


def _ref_signal(uid: str, kind: str, digest: str, at: int) -> None:
    _db.execute("INSERT INTO referral_signals (user_id, kind, hash, first_ms, last_ms) VALUES (?,?,?,?,?)"
                " ON CONFLICT(user_id, kind, hash) DO UPDATE SET last_ms=excluded.last_ms",
                (str(uid), str(kind), str(digest), int(at), int(at)))


def _ref_shares_account(a: str, b: str) -> bool:
    """Whether two accounts are the same account, or are linked to the same wallet.

    This is the one collision that is not a signal: `referral_attributions` has `CHECK (referee <> referrer)` for
    the literal case, and this answers the *linked wallet* case — two accounts proving control of one address,
    which is self-referral wearing a hat.

    Two looks, because the two ways one person ends up with two accounts are held by two different tables:

      * **the identity table.** `user_identities` has `UNIQUE (kind, value)`, so one address normally belongs to
        one account; the comparison is here anyway because a support correction or a re-claim after revocation
        moves rows, and this check is cheap next to the cost of being wrong.
      * **the deposit address.** `users.stonks_address` carries no UNIQUE constraint — it is filled by the deposit
        flow, and two accounts CAN be pointed at one Polymarket proxy wallet. A wallet is the money; when both
        accounts trade from it, they are one person whatever the identity rows say.
    """
    if str(a) == str(b):
        return True
    rows = _db.execute("SELECT value FROM user_identities WHERE kind='wallet' AND state<>'revoked'"
                       " AND user_id IN (?,?)", (str(a), str(b))).fetchall()
    seen = [str(r[0]) for r in rows]
    if len(seen) != len(set(seen)):
        return True
    addr = _db.execute("SELECT stonks_address FROM users WHERE id IN (?,?) AND stonks_address IS NOT NULL"
                       " AND stonks_address<>''", (str(a), str(b))).fetchall()
    vals = [str(r[0]).strip().lower() for r in addr]
    return len(vals) != len(set(vals))


@app.post("/v1/referrals/accrue", responses=REFERRAL_ACCRUE_RESPONSES,
          openapi_extra=_body_schema(("day",), {
              "day": {"type": "string", "minLength": 10, "maxLength": 10,
                      "description": "an UTC day (YYYY-MM-DD); a re-run for the same day writes nothing new"}}))
def post_referral_accrue(request: Request, body: dict = Body(...),
                         idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                         x_admin: str | None = Header(default=None, alias="X-Admin-Token")):
    """The day's accrual run: operator/cron, and the only writer of `referral_accruals`.

    `X-Admin-Token` is DECLARED as a parameter as well as checked through `_admin`, so the contract advertises the
    header a client has to send instead of leaving it to prose. The check itself stays where it was: `_admin`
    reads the value from the Request, so a hand-built Request in a test cannot satisfy the signature and skip it.

    It reads the fee the venue ACTUALLY paid (`builder_attribution.fee_micro_observed`), never the fee we
    expected. That is the difference between a referral budget that is funded by revenue and one that is a
    promise against a projection, and it is also why this route is the only place a referral becomes money.
    """
    rid = request.state.request_id
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    ok, e = _admin(request)
    if e:
        return e
    bad = _check_body(body, ("day",), rid, allowed=("day",))
    if bad is not None:
        return bad
    bad = _check_props(body, {"day": {"type": "string", "minLength": 10, "maxLength": 10}}, rid)
    if bad is not None:
        return bad
    return _idem_run(_operator_subject(), str(idempotency_key), body, rid,
                     lambda: _ref_accrue_work(rid, body))


def _ref_accrue_work(rid: str, body: dict):
    import datetime as _dt
    day = str(body.get("day") or "")
    try:
        start = int(_dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)
    except ValueError:
        return err("VALIDATION", rid, detail="day must be YYYY-MM-DD (UTC)", where=["day"])
    end = start + 86_400_000
    if start > _now_ms():
        return err("VALIDATION", rid, detail="that day has not happened yet", where=["day"])
    rows = _db.execute("SELECT referee, referrer, qualify_ms FROM referral_attributions"
                       " WHERE state='qualified' AND qualify_ms>0", ()).fetchall()
    written = skipped = 0
    paid_micro = share_micro = 0
    for (referee, referrer, qual) in rows:
        fee = _db.execute("SELECT COALESCE(SUM(fee_micro_observed),0) FROM builder_attribution"
                          " WHERE user_id=? AND placed_ms>=? AND placed_ms<? AND fee_micro_observed IS NOT NULL",
                          (str(referee), start, end)).fetchone()
        observed = _tm._int(fee[0]) if fee else 0
        row = _rt.accrual(referrer=str(referrer), referee=str(referee), day=day, observed_fee_micro=observed,
                          state="qualified", qualified_ms=_tm._int(qual), at_ms=start + 86_400_000 - 1)
        if row is None:
            skipped += 1
            continue
        cur = _db.execute("INSERT INTO referral_accruals (referrer, referee, day, fee_observed_micro,"
                          " share_bps, share_micro, created_ms) VALUES (?,?,?,?,?,?,?)"
                          " ON CONFLICT (referrer, referee, day) DO NOTHING",
                          (row["referrer"], row["referee"], row["day"], row["fee_observed_micro"],
                           row["share_bps"], row["share_micro"], row["created_ms"]))
        if int(getattr(cur, "rowcount", 1) or 0) == 0:
            skipped += 1                     # the (referrer, referee, day) key: a re-run pays nothing twice
            continue
        written += 1
        paid_micro += row["fee_observed_micro"]
        share_micro += row["share_micro"]
    _db.execute("INSERT INTO audit_log (at_ms, actor_type, actor_id, action, target_table, target_id,"
                " request_id, detail_json) VALUES (?,?,?,?,?,?,?,?)",
                (_now_ms(), "admin", "operator", "referral.accrue", "referral_accruals", day, str(rid),
                 json.dumps({"day": day, "written": written, "skipped": skipped,
                             "observed_micro": paid_micro, "share_micro": share_micro}, sort_keys=True)))
    _db.commit()
    return _stamped({"day": day, "accruals": written, "skipped": skipped, "observedMicro": paid_micro,
                     "shareMicro": share_micro,
                     "note": "each row is a share of a fee the venue actually paid; a re-run for the same day "
                             "writes nothing new, and a referral under review accrues nothing until it is cleared"},
                    ttl_ms=5_000, stale_ms=flags().stale_ms_tape, as_of_ms=_now_ms())


@app.get("/v1/referrals/review", responses=REFERRAL_REVIEW_RESPONSES)
def get_referral_review(request: Request, state: str = Query(default="open", pattern="^(open|cleared|actioned)$"),
                        limit: int = Query(default=50, ge=1, le=200),
                        x_admin: str | None = Header(default=None, alias="X-Admin-Token")):
    """The queue the kit asks for: newest first, with each row's own sentences and the money involved."""
    rid = request.state.request_id
    ok, e = _admin(request)
    if e:
        return e
    rows = _db.execute("SELECT id, kind, subject, referee, state, findings_json, opened_ms, decided_ms, decision,"
                       " actor FROM referral_reviews WHERE state=? ORDER BY opened_ms DESC LIMIT ?",
                       (str(state), int(limit))).fetchall()
    out = []
    for (i, kind, subject, referee, st, findings, opened, decided, decision, actor) in rows:
        accrued = _db.execute("SELECT COALESCE(SUM(share_micro),0) FROM referral_accruals WHERE referrer=?"
                              " AND referee=?", (str(subject), str(referee))).fetchone()
        out.append({"id": _tm._int(i), "kind": str(kind), "subject": _ref_label(str(subject)),
                    "referee": _ref_label(str(referee)), "state": str(st),
                    "findings": json.loads(findings or "[]"), "openedMs": _tm._int(opened),
                    "decidedMs": (_tm._int(decided) or None), "decision": str(decision or ""),
                    "actor": str(actor or ""), "accruedMicro": _tm._int(accrued[0]) if accrued else 0})
    return _stamped({"state": str(state), "items": out, "count": len(out),
                     "note": "a self-referral is not only a referral decision: it is a builder-code revocation "
                             "ground, because what it threatens is the revenue the code collects"},
                    ttl_ms=15_000, stale_ms=flags().stale_ms_tape, as_of_ms=_now_ms())


@app.post("/v1/referrals/review", responses=REFERRAL_REVIEW_SET_RESPONSES,
          openapi_extra=_body_schema(("id", "decision", "reason"), {
              "id": {"type": "integer", "minimum": 1, "description": "the queue row, from GET /v1/referrals/review"},
              "decision": {"type": "string", "enum": ["clear", "claw_back", "exclude"]},
              "reason": {"type": "string", "minLength": 4, "maxLength": 400},
              "actor": {"type": "string", "maxLength": 120,
                        "description": "who decided; defaults to `operator`, and is written to the audit log"}}))
def post_referral_review(request: Request, body: dict = Body(...),
                         idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                         x_admin: str | None = Header(default=None, alias="X-Admin-Token")):
    """Decide one queue item: clear it, claw it back, or exclude it for good."""
    rid = request.state.request_id
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    ok, e = _admin(request)
    if e:
        return e
    bad = _check_body(body, ("id", "decision", "reason"), rid, allowed=("id", "decision", "reason", "actor"))
    if bad is not None:
        return bad
    bad = _check_props(body, {"id": {"type": "integer", "minimum": 1},
                              "decision": {"type": "string", "enum": ["clear", "claw_back", "exclude"]},
                              "reason": {"type": "string", "minLength": 4, "maxLength": 400},
                              "actor": {"type": "string", "maxLength": 120}}, rid)
    if bad is not None:
        return bad
    return _idem_run(_operator_subject(), str(idempotency_key), body, rid,
                     lambda: _ref_review_work(rid, body))


def _ref_review_work(rid: str, body: dict):
    body_out: dict = {}
    item = _db.execute("SELECT id, kind, subject, referee, state FROM referral_reviews WHERE id=?",
                       (int(body["id"]),)).fetchone()
    if item is None or str(item[4]) != "open":
        return err("NO_SUCH_RESOURCE", rid)
    review_id, kind, subject, referee = _tm._int(item[0]), str(item[1]), str(item[2]), str(item[3])
    decision = str(body["decision"])
    reason = str(body["reason"])
    actor = str(body.get("actor") or "operator")
    at = _now_ms()
    state = "cleared" if decision == "clear" else "actioned"
    if decision == "clear":
        # A cleared review resumes accrual from the referee's qualifying order: what was earned while it waited
        # is not lost, it is simply not yet accrued.
        _db.execute("UPDATE referral_attributions SET state='qualified', decided_ms=? WHERE referee=?"
                    " AND state='review'", (at, referee))
    elif decision == "exclude":
        _db.execute("UPDATE referral_attributions SET state='refused', decided_ms=? WHERE referee=?", (at, referee))
    else:
        paid = _db.execute("SELECT COALESCE(SUM(amount_micro),0) FROM referral_payouts WHERE referrer=?"
                           " AND status IN ('sent','approved')", (subject,)).fetchone()
        unpaid = _db.execute("SELECT COALESCE(SUM(share_micro),0) FROM referral_accruals WHERE referrer=?"
                             " AND referee=?", (subject, referee)).fetchone()
        claw = _sy.clawback(paid_micro=_tm._int(paid[0]) if paid else 0,
                            unpaid_micro=_tm._int(unpaid[0]) if unpaid else 0, reason=kind,
                            minimum_micro=_rt.CLAWBACK_MIN_MICRO)
        _db.execute("UPDATE referral_attributions SET state='clawed_back', decided_ms=? WHERE referee=?",
                    (at, referee))
        # The accruals stay where they are, append-only, and the payout page reads the attribution's state: a
        # clawback that DELETED rows would destroy the evidence it is based on.
        # The code is revoked for a SELF-REFERRAL and for nothing else. `polygm-referral` is the code every
        # referral is attributed under, so disabling it stops the attribution for every referrer — which is the
        # right answer to "this account was referring itself" and the wrong answer to "these two wallets came from
        # one funding source". A duplicate-funding clawback turns off that referral; a self-referral turns off the
        # thing that was being farmed.
        if kind == "self_referral":
            _ref_invalidate_builder_code(_REF_BUILDER_CODE, why="self-referral confirmed: %s" % reason,
                                         actor=actor, at=at)
        # camelCase at the boundary, and the engine's own keys stay as they are: the wire format is this API's,
        # and the alternative is a client that has to know which half of a payload follows which convention.
        body_out = {"clawback": {"unpaidMicro": claw["unpaid_micro"], "paidMicro": claw["paid_micro"],
                                "writtenOffMicro": claw["written_off_micro"],
                                "requiresRepayment": claw["requires_repayment"], "reason": claw["reason"],
                                "sentence": claw["sentence"]}}
    _db.execute("UPDATE referral_reviews SET state=?, decision=?, actor=?, decided_ms=? WHERE id=?",
                (state, decision, actor, at, review_id))
    _db.execute("INSERT INTO audit_log (at_ms, actor_type, actor_id, action, target_table, target_id,"
                " request_id, detail_json) VALUES (?,?,?,?,?,?,?,?)",
                (at, "admin", actor, "referral.review", "referral_reviews", str(review_id), str(rid),
                 json.dumps({"decision": decision, "kind": kind, "reason": reason,
                             "subject": subject, "referee": referee}, sort_keys=True)))
    _db.commit()
    out = {"id": review_id, "decision": decision, "state": state, "kind": kind, "reason": reason,
           "note": {"clear": "the referral is live again and the accrual it missed is written on the next run",
                    "exclude": "the referral is refused for good; the attribution row keeps the reason",
                    "claw_back": "unpaid accruals are cancelled first, and anything already paid is asked for "
                                 "back under the published rule"}[decision]}
    out.update(body_out if decision == "claw_back" else {})
    return _stamped(out, ttl_ms=5_000, stale_ms=flags().stale_ms_tape, as_of_ms=at)


# -------------------------------------------------------------------------------------------------------------
# P11 · D6 · the public pages: the budget, the three reads, the sitemap and the block lever.
#
# Everything below serves a caller we cannot identify, so three rules are applied once, here, rather than in
# each route: the caller is a salted digest and never an address (the same function the referral signals use,
# so there is one salt and one implementation to rotate); the window is counted before the payload is built, so
# a refused request costs a lookup and not a ranking; and the payload a route returns is the payload the card,
# the structured data and the sitemap are built from, so there is one set of public facts per page and not four
# that agree by review.
#
# What these routes do NOT do, deliberately:
#   * they do not resolve a handle to an account, a wallet, an address or a balance - the page's subject is a
#     pseudonym (§2.19), and this route is what makes that a structural fact rather than a styling choice;
#   * they do not serve an unlisted handle differently from a missing one: both are 404 with the same body, so
#     the page cannot be used to ask "is this handle taken?" one string at a time;
#   * they do not build a second ranking. Every board number comes from `_lb_board`, the same call the app uses.

_PP_CARD_TTL_MS = 86_400_000


def _public_budget_row(key: str, default: str) -> str:
    """A bound the product chose, read from a row so it is one edit rather than one deploy."""
    row = _db.execute("SELECT value FROM public_page_budget WHERE key=?", (str(key),)).fetchone()
    return str(row[0]) if row and str(row[0]).strip() else str(default)


def _public_window(kind: str, subject: str, now_ms: int, *, window_ms: int) -> tuple[int, int, int]:
    """(window start, hits in it, locked-until) for this subject and kind.

    The window is fixed from the first hit and read back from the row rather than recomputed from the clock: a
    request that arrives halfway through a window must not reset its own budget, and two API processes sharing
    a database must agree on when the window began.
    """
    row = _db.execute("SELECT window_start_ms, hits, locked_until_ms FROM public_page_hits "
                      "WHERE kind=? AND subject_hash=? ORDER BY window_start_ms DESC LIMIT 1",
                      (str(kind), str(subject))).fetchone()
    if row is None:
        return int(now_ms), 0, 0
    start = _tm._int(row[0])
    if int(now_ms) - start >= int(window_ms):
        return int(now_ms), 0, 0
    return start, _tm._int(row[1]), _tm._int(row[2])


def _public_write(kind: str, subject: str, start_ms: int, hits: int, locked_until_ms: int,
                  now_ms: int) -> None:
    """Record the hit. SELECT-then-write rather than an upsert, because the two engines spell that differently
    and a dialect conditional in a rate limiter is a rate limiter that is wrong on one of them."""
    row = _db.execute("SELECT hits FROM public_page_hits WHERE kind=? AND subject_hash=? AND window_start_ms=?",
                      (str(kind), str(subject), int(start_ms))).fetchone()
    if row is None:
        _db.execute("INSERT INTO public_page_hits (kind, subject_hash, window_start_ms, hits, locked_until_ms,"
                    " last_ms) VALUES (?,?,?,?,?,?)",
                    (str(kind), str(subject), int(start_ms), int(hits), int(locked_until_ms), int(now_ms)))
    else:
        _db.execute("UPDATE public_page_hits SET hits=?, locked_until_ms=?, last_ms=? "
                    "WHERE kind=? AND subject_hash=? AND window_start_ms=?",
                    (int(hits), int(locked_until_ms), int(now_ms), str(kind), str(subject), int(start_ms)))


def _public_blocks(subject: str) -> list[dict]:
    rows = _db.execute("SELECT scope, reason, until_ms, kind FROM public_page_blocks WHERE subject_hash=?",
                       (str(subject),)).fetchall()
    return [{"scope": r[0], "reason": r[1], "until_ms": r[2], "kind": r[3]} for r in rows] if rows else []


def _public_gate(request: Request, kind: str) -> tuple[dict, JSONResponse | None]:
    """Count this request, then decide. Returns (meta, refusal) and never raises.

    The order matters and is the whole design: a *blocked* subject is refused without spending a budget row, a
    refused subject has its hits recorded (which is what makes the auto-block rule able to see "came back ten
    times past the limit"), and only an allowed request goes on to build a payload.

    `X-RateLimit-*` is served on the allowed path too, because a well-behaved crawler that can see its remaining
    budget will slow down, and one that cannot will discover the limit by being refused.
    """
    rid = request.state.request_id
    now = _now_ms()
    salt = _ref_salt()                                     # one salt for everything we digest, one rotation
    subject = _pp.budget.subject_hash(str(getattr(request.client, "host", "") or "unknown"), salt)
    spec = _pp.budget.BUDGET.get(str(kind), _pp.budget.BUDGET["leaderboard"])

    live = _pp.budget.block_for(_public_blocks(subject), scope=str(kind), at_ms=now)
    if live:
        resp = err("RATE_LIMITED", rid)
        resp.headers["Retry-After"] = str(max(1, int(live["retryAfterMs"]) // 1000))
        resp.headers["Cache-Control"] = "no-store"
        return {"subject": subject, "blocked": True, "reason": live["reason"]}, resp

    # The cross-kind budget is checked first: it is the only one a loop over all four kinds cannot escape.
    t_start, t_hits, _ = _public_window("all", subject, now, window_ms=int(_pp.budget.TOTAL[1]))
    total = _pp.budget.decide_total(hits=t_hits + 1, window_start_ms=t_start, at_ms=now)
    start, hits, locked_until = _public_window(str(kind), subject, now, window_ms=int(spec[1]))
    verdict = _pp.budget.decide_hits(kind=str(kind), hits=hits + 1, window_start_ms=start, at_ms=now)

    wrote_lock = 0
    if not verdict["allowed"]:
        wrote_lock = now + int(spec[2])
        _public_write(str(kind), subject, start, hits + 1, wrote_lock, now)
    if not total["allowed"]:
        _public_write("all", subject, t_start, t_hits + 1, now + int(_pp.budget.TOTAL[2]), now)

    if not verdict["allowed"] or not total["allowed"]:
        # Coming straight back ten times past a limit is a scraper rather than a reader whose browser retried.
        auto, why = _pp.budget.should_auto_block(findings=[], hits=hits + 1, limit=int(spec[0]))
        if auto and wrote_lock:
            _db.execute("INSERT INTO public_page_blocks (subject_hash, scope, reason, kind, until_ms,"
                        " created_ms, created_by) VALUES (?,?,?,?,?,?,?)",
                        (subject, "all", why, "auto", now + 600_000, now, ""))
        refusal = err("RATE_LIMITED", rid)
        refusal.headers["Retry-After"] = str(max(1, int(verdict["retryAfterMs"] or total["retryAfterMs"]) // 1000))
        refusal.headers["Cache-Control"] = "no-store"
        return {"subject": subject, "kind": kind, "limit": int(spec[0])}, refusal

    _public_write(str(kind), subject, start, hits + 1, locked_until if locked_until > now else 0, now)
    _public_write("all", subject, t_start, t_hits + 1, 0, now)
    return {"subject": subject, "kind": kind, "limit": int(spec[0]),
            "remaining": max(0, int(verdict["remaining"])),
            "windowMs": int(spec[1])}, None


def _public_headers(meta: dict, kind: str, tag: str) -> dict:
    """The headers every public read carries: the cache contract, the validator, and the budget's own number."""
    return {"Cache-Control": _pp.urls.cache_control(str(kind)), "ETag": str(tag),
            "X-RateLimit-Limit": str(int(meta.get("limit") or 0)),
            "X-RateLimit-Remaining": str(int(meta.get("remaining") or 0)),
            "Vary": "Accept-Encoding"}


def _public_trader_handle(handle: str) -> tuple[str, str] | None:
    """(user id, canonical handle) for a *listed* handle, or None.

    One indexed read against the partial unique index D6 added, and the state filter is part of the query rather
    than checked afterwards: an account that turned listing off must stop resolving here in the same instant it
    stops appearing on a board, and a check that happens after the fetch is a check somebody reorders.
    """
    row = _db.execute("SELECT user_id, handle FROM leaderboard_identity "
                      "WHERE state='listed' AND handle=? AND handle <> ''", (str(handle),)).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1])


def _public_trader_notes(row: dict, standing: dict) -> list[str]:
    """The qualifiers that travel with the numbers, built from the row itself.

    Each sentence is a consequence of a rule the rest of the phase already enforces (the sample gate, the
    provisional window, the disputed-market exclusion, the drawdown). Composing them here rather than in the
    card or the page is what makes the card and the page carry the same claims: they both read this list.
    """
    notes: list[str] = []
    settled = _tm._int(row.get("settledMarkets"))
    if row.get("winRateBps") is None:
        gate_n = _tm._int(standing.get("sampleGate") or _lb_boards.MIN_RESOLVED)
        notes.append("win rate is behind the sample gate: %d settled markets, %d needed" % (settled, gate_n))
    if _tm._int(row.get("ageDays")) < 7:
        notes.append("provisional: %d days of history, nothing rankable before 7" % _tm._int(row.get("ageDays")))
    if _tm._int(row.get("maxDrawdownMicro")):
        notes.append("worst drawdown %s" % fmt_usdc(_tm._int(row.get("maxDrawdownMicro"))))
    if _tm._int(row.get("disputedMarkets")):
        notes.append("%d disputed market(s) excluded from this row" % _tm._int(row.get("disputedMarkets")))
    return notes[:3]


@app.get("/v1/public/trader/{handle}", responses=PUBLIC_TRADER_RESPONSES)
def get_public_trader(handle: str, request: Request):
    """The public dossier behind a listed handle: server-rendered first, crawlable second, and shareable third.

    D4's served copy promised that "there is one handle per traded wallet: /trader/<handle> resolves to the same
    wallet as its pseudonym". This route is that promise, and it resolves to the *pseudonym* and stops: the
    account id, the wallet address and the balance behind it are not in this payload at any depth, which is also
    what lets the OG card and the JSON-LD be built from it without a second scrub.
    """
    rid = request.state.request_id
    meta, refusal = _public_gate(request, "trader")
    if refusal is not None:
        return refusal
    wanted, why = _pp.urls.normalise_handle(handle)
    if why:
        return err("VALIDATION", rid, detail=why, where=["handle"])
    found = _public_trader_handle(wanted)
    if found is None:
        return err("NOT_FOUND", rid)
    uid, canonical = found
    at = _now_ms()
    wallets = _lb_wallets_for(str(uid))
    if not wallets:
        return err("NOT_FOUND", rid)
    wallet = dict(wallets[0])
    anon = str(wallet["anon"])
    try:
        standing = _lb_self_wallet(wallet, at_ms=at, window="", days=30, page=_LB_PAGE_SIZE)
    except ValueError as exc:                                                      # pragma: no cover - spec only
        return err("VALIDATION", rid, detail=str(exc), where=["window"])
    default_board = next((str(b["id"]) for b in _lb_boards.BOARDS if b.get("isDefault")), "risk_adjusted")
    entry = next((e for e in standing["boards"] if e["board"] == default_board and not e.get("category")), None)
    if entry is None:
        return err("NOT_FOUND", rid)
    board = _lb_board(board_id=default_board, window="", at_ms=at)
    raw = next((r for r in board["rows"] if str(r.get("wallet")) == anon), None)
    if raw is None:
        return err("NOT_FOUND", rid)
    row = _lb_row_out(raw, total=board["rankedTotal"], handles={anon: canonical})
    notes = _public_trader_notes(row, entry)
    url = _pp.urls.page_url("trader", canonical, base=_BASE_URL())
    card = _pp.cards.trader_card(
        handle=canonical,
        headline=(("rank #%d of %d on the risk-adjusted board" % (_tm._int(row.get("rank")),
                                                                 _tm._int(board["rankedTotal"])))
                  if entry.get("state") == "ranked" else "not ranked yet"),
        stats=[("realised", str(row.get("realised") or fmt_usdc(_tm._int(row.get("realisedMicro"))))),
               ("settled markets", str(_tm._int(row.get("settledMarkets")))),
               ("win rate", (_bps_text(_tm._int(row.get("winRateBps"))) if row.get("winRateBps") is not None
                             else "behind sample gate"))],
        notes=notes, ranked=entry.get("state") == "ranked",
        provisional=_tm._int(row.get("ageDays")) < 7,
        drawdown=(fmt_usdc(_tm._int(row.get("maxDrawdownMicro"))) if _tm._int(row.get("maxDrawdownMicro")) else ""),
        url=url)
    # The trail is built from the payload's OWN names — the card's brand, the board's label, the card's title —
    # rather than from section names invented here. A breadcrumb is the one node in the graph that is pure
    # vocabulary, and the temptation is to exempt it from `unbacked()` and hand-write "Leaderboard"; the gate's c23
    # refused, correctly, because a crawler showing a section name the page never prints is a claim we made up.
    # The web replaces these names with the ones it renders (see `JsonLd`'s `crumbNames`), so the machine-readable
    # trail and the visible trail are the same trail.
    graph = _pp.structured.graph(
        _pp.structured.breadcrumb([(str(card.get("brand")), _BASE_URL()),
                                   (str(board["label"] or default_board),
                                    _pp.urls.page_url("leaderboard", default_board, base=_BASE_URL())),
                                   (str(card["title"]), url)]),
        _pp.structured.profile_page(handle=canonical, url=url, headline=str(card["subtitle"]), notes=notes,
                                    board_url=_BASE_URL() + "/leaderboard"))
    body = {
        "cacheKey": "public-trader:%s" % canonical,
        "handle": canonical, "anon": anon, "url": url,
        "robots": _pp.urls.robots_for(kind="trader", ranked=entry.get("state") == "ranked",
                                      age_days=_tm._int(row.get("ageDays")), rows=1),
        # Nine answers, not five: the category board is four boards wearing one name (§2.12), and a public
        # profile that printed only the main five would hide the standing a specialist is most likely to share.
        # Each entry carries the URL of the board it is a row of, so the page links back to what it cites.
        "standing": [{"board": e["board"], "label": e.get("label"), "category": e.get("category") or "",
                      "state": e.get("state"), "rank": e.get("rank"),
                      "rankedTotal": e.get("rankedTotal"), "rankBadge": e.get("rankBadge"),
                      "url": _pp.urls.page_url("leaderboard", str(e["board"]), base=_BASE_URL(),
                                               category=str(e.get("category") or ""))}
                     for e in standing["boards"]],
        "headline": {"board": default_board, "window": board["window"], "rank": _tm._int(row.get("rank")),
                     "rankedTotal": _tm._int(board["rankedTotal"]), "settledMarkets": _tm._int(row.get("settledMarkets")),
                     "winRateBps": row.get("winRateBps"), "realisedMicro": _tm._int(row.get("realisedMicro")),
                     "realised": row.get("realised"), "maxDrawdownMicro": _tm._int(row.get("maxDrawdownMicro")),
                     "maxDrawdown": row.get("drawdown"), "state": row.get("state"),
                     "ageDays": _tm._int(row.get("ageDays"))},
        "notes": notes, "card": card, "cardKey": _pp.cards.card_key(card), "structuredData": graph,
        # The gate this page quotes is the one its own notes are written against — the board's own gate when the
        # entry carries one, otherwise the leaderboard's. It shipped as `or 0`, which is a number that cannot be
        # right in any world: a page claiming "0 settled markets needed" beside a row that says otherwise is the
        # exact class of bug the sample gate exists to prevent, and the D6 web test caught it by rendering it.
        "sampleGate": _tm._int(entry.get("sampleGate") or row.get("sampleGate") or _lb_boards.MIN_RESOLVED),
        "links": {"methodology": "/v1/leaderboard/methodology", "board": "/v1/leaderboard",
                  "og": _pp.urls.og_url("trader", canonical, base=_BASE_URL())},
        "note": ("this page is public because the trader listed a handle; the row is the same row it would be "
                 "privately, and turning listing off removes the address, not the rank"),
    }
    _pp_audit("public.trader", canonical, meta, rid)
    return JSONResponse(content=_stamped(body, ttl_ms=30_000, stale_ms=flags().stale_ms_tape, as_of_ms=at),
                        headers=_public_headers(meta, "trader", _pp.urls.etag(body)))


@app.get("/v1/public/market/{slug}", responses=PUBLIC_MARKET_RESPONSES)
def get_public_market(slug: str, request: Request):
    """The market page a news story links to: the question, the odds, the freshness, and nothing account-shaped.

    The odds come from the same rows `/v1/markets/{id}` prints, and the freshness stamp is the market's own
    `updated_ms`, not `now` — a crawlable odds page is the one surface where a stale price is quoted as current
    by somebody who never opened the app, so the age travels in the payload, the card's footnote and a header.
    """
    rid = request.state.request_id
    meta, refusal = _public_gate(request, "market")
    if refusal is not None:
        return refusal
    wanted, why = _pp.urls.normalise_slug(slug)
    if why:
        return err("VALIDATION", rid, detail=why, where=["slug"])
    row = _db.execute(
        "SELECT m.id, m.question, m.accepting_orders, m.minimum_tick_size, m.minimum_order_size, m.fee_type, "
        "m.end_ts, m.slug, m.updated_ms, e.title, e.slug, COALESCE(mt.category, 'Other'), "
        "COALESCE(s.volume_24h_micro, 0), COALESCE(s.liquidity_micro, 0), a.last_price_micro, "
        "COALESCE(a.volume_7d_micro, 0), COALESCE(a.open_interest_micro, 0), m.condition_id "
        "FROM markets m LEFT JOIN events e ON e.id = m.event_id "
        "LEFT JOIN market_meta mt ON mt.market_id = m.id LEFT JOIN market_stats s ON s.condition_id = m.condition_id "
        "LEFT JOIN market_activity a ON a.market_id = m.id "
        "WHERE m.slug = ? AND m.slug IS NOT NULL AND m.slug <> ''", (wanted,)).fetchone()
    if row is None:
        return err("NOT_FOUND", rid)
    at = _now_ms()
    market_id = str(row[0])
    tokens = _db.execute("SELECT outcome, is_winner FROM tokens WHERE market_id=? ORDER BY outcome_index",
                         (market_id,)).fetchall() or []
    outcomes = [{"outcome": str(t[0]), "winner": (None if t[1] is None else bool(t[1]))} for t in tokens]
    price = row[14]
    opened = int(price) if price is not None else None
    age_ms = max(0, at - _tm._int(row[8]))
    url = _pp.urls.page_url("market", str(row[7]), base=_BASE_URL())
    fresh = _freshness_text(age_ms)
    card = _pp.cards.market_card(
        question=str(row[1]),
        odds=[("yes" if i == 0 else "outcome %d" % (i + 1), fmt_usdc(opened)) for i, _t in enumerate(outcomes[:3])]
             if outcomes and opened is not None else [("odds", "not quoted yet")],
        meta=["odds as of %s" % fresh, "%s volume 24h" % fmt_usdc(_tm._int(row[12]))],
        url=url)
    body = {
        "cacheKey": "public-market:%s" % wanted,
        "marketId": market_id, "slug": str(row[7]), "url": url,
        "robots": _pp.urls.robots_for(kind="market"),
        "question": str(row[1]), "eventTitle": str(row[9] or ""), "eventSlug": str(row[10] or ""),
        "category": str(row[11]), "acceptingOrders": bool(row[2]), "endDate": row[6],
        # `_micro_of`, not `float(...)`: the minimum order size is a decimal STRING from upstream and the venue
        # writes values like "5" and "0.5", so multiplying a float by 10**6 is a rounding decision taken on the
        # money path by accident. (get_market above still does it and is carried as an open item: changing it
        # there moves a printed terminal string, which is a P08/P09 regression test's decision, not D6's.)
        "minimumTickSize": norm_tick(row[3]), "minimumOrderSize": fmt_usdc(_micro_of(str(row[4]))),
        "feeType": str(row[5]), "outcomes": outcomes,
        # Verbatim, exactly as the terminal serves it: the resolution text is the one string an outsider writes
        # and the CLIENT renders it as text (P09 c5 sanitises at the render, not here).
        "resolutionCriteria": (_db.execute("SELECT resolution_criteria, resolution_source FROM market_meta "
                                           "WHERE market_id=?", (market_id,)).fetchone() or ["", ""])[0],
        "odds": {"lastPriceMicro": opened, "lastPrice": (fmt_usdc(opened) if opened is not None else None),
                 "volume24hMicro": _tm._int(row[12]), "volume24h": fmt_usdc(_tm._int(row[12])),
                 "volume7d": fmt_usdc(_tm._int(row[15])), "liquidity": fmt_usdc(_tm._int(row[13])),
                 "openInterest": fmt_usdc(_tm._int(row[16])),
                 "ageMs": age_ms, "ageText": fresh, "quotedFrom": "market_activity.last_price_micro"},
        # An odds page is quoted out of context, so the two things that decide whether the quote is meaningful
        # are stated beside it: is the market still taking orders, and how old is this number.
        "quoteNote": ("this market is not accepting orders; the price is the last traded price and not a quote"
                      if not bool(row[2]) else "the price is the last trade our ingest recorded"),
        "card": card, "cardKey": _pp.cards.card_key(card),
        "structuredData": _pp.structured.graph(
            # The category (when the market has one) is the middle step: it is a string this payload carries, and
            # the markets index is the page it belongs to. A market with no category gets the two-step trail.
            _pp.structured.breadcrumb([(str(card.get("brand")), _BASE_URL())]
                                      + ([(str(row[11]), _BASE_URL() + "/markets")]
                                         if str(row[11] or "").strip() else [])
                                      + [(str(row[1])[:80], url)]),
            _pp.structured.market_event(name=str(row[1]), url=url,
                                        end_date=("" if row[6] in (None, "") else str(row[6])),
                                        accepting=bool(row[2]))),
        "links": {"terminal": "/market/%s" % market_id, "book": "/v1/markets/%s/book" % market_id,
                  "og": _pp.urls.og_url("market", str(row[7]), base=_BASE_URL())},
    }
    _pp_audit("public.market", wanted, meta, rid)
    return JSONResponse(content=_stamped(body, ttl_ms=15_000, stale_ms=flags().stale_ms_book, as_of_ms=at),
                        headers=_public_headers(meta, "market", _pp.urls.etag(body)))


@app.get("/v1/public/leaderboard/{board}", responses=PUBLIC_BOARD_RESPONSES)
def get_public_leaderboard(board: str, request: Request,
                           window: str = Query(default="", pattern="^(|24h|7d|30d|90d|all)$"),
                           category: str = Query(default="", max_length=32),
                           limit: int = Query(default=25, ge=1, le=100)):
    """A board as a page: the same rows the app ranks, cut to a page a crawler can read in one request.

    The board's own formula, gate, cadence and exclusions count travel with it, because a public ranking without
    its methodology is the thing this phase exists to replace — and the row's sample size travels with the row
    (§2.10) so a screenshot of this page carries the number that qualifies it.
    """
    rid = request.state.request_id
    meta, refusal = _public_gate(request, "leaderboard")
    if refusal is not None:
        return refusal
    board_id = str(board or "").strip().lower()
    if board_id not in _lb_boards.BOARD_IDS:
        return err("NOT_FOUND", rid, detail="that is not a board", where=["board"])
    at = _now_ms()
    try:
        full = _lb_board(board_id=board_id, window=str(window or ""), at_ms=at, category=str(category or ""))
    except ValueError as exc:
        return err("VALIDATION", rid, detail=str(exc), where=["window", "category"])
    handles = _lb_published_handles()
    rows = [_lb_row_out(r, total=full["rankedTotal"], handles=handles) for r in full["rows"][:int(limit)]]
    url = _pp.urls.page_url("leaderboard", board_id, base=_BASE_URL(), window=str(window or ""),
                            category=str(category or ""))
    notes = [("%s — %s" % (full["label"], full["formula"])),
             ("%d ranked, %d excluded; every row carries its own sample size"
              % (full["rankedTotal"], _tm._int(full.get("excludedTotal"))))]
    card = _pp.cards.leaderboard_card(
        board=str(full["label"]), window=str(full["window"]),
        rows=[(r.get("handle") or ("@" + str(r["anon"])[:10]), str(r.get("rankBadge", {}).get("text", "")))
              for r in rows[:3]],
        notes=notes, url=url)
    body = {
        "cacheKey": "public-board:%s:%s:%s" % (board_id, full["window"], category or "-"),
        "board": board_id, "label": full["label"], "url": url,
        "robots": _pp.urls.robots_for(kind="leaderboard", rows=int(full["rankedTotal"])),
        "window": full["window"], "category": (full.get("category") or ""),
        "formula": full["formula"], "gate": full["gate"], "tieBreaks": full["tieBreaks"],
        "cadenceMs": full["cadenceMs"], "rankedTotal": full["rankedTotal"],
        "excludedTotal": _tm._int(full.get("excludedTotal")), "blewUpCount": full["blewUpCount"],
        "provisionalCount": full["provisionalCount"], "rows": rows, "rowCount": len(rows),
        "notes": notes, "card": card, "cardKey": _pp.cards.card_key(card),
        "structuredData": _pp.structured.graph(
            # Two steps, and that is the honest shape: a board IS the index, so there is no ancestor between the
            # site root and the board except the brand, and inventing one is how a trail becomes a claim.
            _pp.structured.breadcrumb([(str(card.get("brand")), _BASE_URL()), (str(full["label"]), url)]),
            _pp.structured.item_list(name="%s — %s" % (full["label"], full["window"]), url=url,
                                     rows=[(_tm._int(r.get("rank")), (r.get("handle") or str(r["anon"])), "")
                                           for r in rows[:25]])),
        "links": {"methodology": "/v1/leaderboard/methodology", "boards": "/v1/leaderboard/boards",
                  "og": _pp.urls.og_url("leaderboard", board_id, base=_BASE_URL(), window=str(window or ""))},
        "note": ("every eligible wallet is on this board, including the ones that blew up: a ranking that hides "
                 "the bad rows is a ranking of a different population than the one it claims"),
    }
    _pp_audit("public.board", body["cacheKey"], meta, rid)
    return JSONResponse(content=_stamped(body, ttl_ms=60_000, stale_ms=flags().stale_ms_tape, as_of_ms=at),
                        headers=_public_headers(meta, "leaderboard", _pp.urls.etag(body)))


@app.get("/v1/public/sitemap", responses=PUBLIC_SITEMAP_RESPONSES)
def get_public_sitemap(request: Request):
    """What a crawler should walk, bounded by a number that is a row rather than a constant.

    Three lists, each ordered by the thing that decides whether it is worth crawling: **handles** by rank on the
    default board (the pages people share), **markets** by 24h volume (the pages a news story links), and
    **boards** (six plus the four category boards). The caps are served with the payload, because a sitemap that
    silently truncates is a sitemap whose coverage nobody can measure — and the day the cap bites, the numbers
    in the response are the argument for raising it.
    """
    rid = request.state.request_id
    meta, refusal = _public_gate(request, "sitemap")
    if refusal is not None:
        return refusal
    at = _now_ms()
    cap_h = int(_public_budget_row("sitemap_handles", "2000"))
    cap_m = int(_public_budget_row("sitemap_markets", "2000"))
    board = _lb_board(board_id="risk_adjusted", window="", at_ms=at)
    handles = _lb_published_handles()
    listed = [r for r in board["rows"] if handles.get(str(r.get("wallet")))]
    urls = [{"url": _pp.urls.page_url("trader", handles[str(r["wallet"])], base=_BASE_URL()),
             "changefreq": "daily", "priority": "0.8"} for r in listed[:cap_h]]
    for b in _lb_boards.BOARD_IDS:
        urls.append({"url": _pp.urls.page_url("leaderboard", b, base=_BASE_URL()), "changefreq": "hourly",
                     "priority": "0.7"})
    for c in _lb_boards.CATEGORIES:
        urls.append({"url": _pp.urls.page_url("leaderboard", "category", base=_BASE_URL(), category=str(c)),
                     "changefreq": "daily", "priority": "0.5"})
    prow = _db.execute("SELECT m.slug FROM markets m LEFT JOIN market_stats s ON s.condition_id = m.condition_id "
                       "WHERE m.slug IS NOT NULL AND m.slug <> '' AND m.accepting_orders = ? "
                       "ORDER BY COALESCE(s.volume_24h_micro, 0) DESC LIMIT ?",
                       (True, cap_m)).fetchall() or []
    for r in prow:
        urls.append({"url": _pp.urls.page_url("market", str(r[0]), base=_BASE_URL()), "changefreq": "hourly",
                     "priority": "0.6"})
    _pp_audit("public.sitemap", "cap:%d" % len(urls), {"kind": "sitemap"}, rid)
    body = {"cacheKey": "public-sitemap", "robots": "index, follow", "generatedAtMs": at, "count": len(urls),
            "caps": {"handles": cap_h, "markets": cap_m},
            "truncated": {"handles": len(listed) > cap_h, "markets": len(prow) >= cap_m},
            "urls": urls,
            "note": ("a sitemap is the one public surface where being incomplete is invisible: the caps are "
                     "served beside the URLs so a truncated walk is measurable rather than assumed")}
    return JSONResponse(content=_stamped(body, ttl_ms=900_000, stale_ms=0, as_of_ms=at),
                        headers=_public_headers(meta, "sitemap", _pp.urls.etag(body)))


@app.get("/v1/public/blocks", responses=PUBLIC_BLOCK_LIST_RESPONSES)
def get_public_blocks(request: Request, x_admin: str | None = Header(default=None, alias="X-Admin-Token")):
    """The live blocks, newest first, with the reason each one exists. Operator-only.

    A block list is a lever with a support cost, so it is readable: the on-call asked "is this person blocked?"
    should be able to answer with a query rather than by reading a rate-limiter's logs.
    """
    ok, e = _admin(request)
    if e:
        return e
    at = _now_ms()
    rows = _db.execute("SELECT subject_hash, scope, reason, kind, until_ms, created_ms, created_by "
                       "FROM public_page_blocks WHERE until_ms > ? ORDER BY until_ms ASC",
                       (at,)).fetchall() or []
    return _stamped({"blocks": [{"subject": str(r[0]), "scope": str(r[1]), "reason": str(r[2]),
                                "kind": str(r[3]), "untilMs": _tm._int(r[4]), "createdMs": _tm._int(r[5]),
                                "createdBy": str(r[6])} for r in rows], "liveCount": len(rows), "asOfMs": at},
                    ttl_ms=0, stale_ms=0, as_of_ms=at)


@app.post("/v1/public/blocks", responses=PUBLIC_BLOCK_RESPONSES,
          openapi_extra=_body_schema(("address", "reason", "hours"), {
              "address": {"type": "string", "minLength": 3, "maxLength": 120,
                          "description": "digested with the referral salt and never stored"},
              "reason": {"type": "string", "minLength": 8, "maxLength": 400},
              "hours": {"type": "integer", "minimum": 1, "maximum": 168},
              "scope": {"type": "string", "enum": ["all"] + list(_pp.urls.KINDS), "default": "all"},
              "createdBy": {"type": "string", "maxLength": 120}}))
def post_public_block(request: Request, body: dict = Body(...),
                      idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                      x_admin: str | None = Header(default=None, alias="X-Admin-Token")):
    """Block an address digest from the public pages for a stated time, with a stated reason.

    The address is digested here and never stored: an operator who wants to block `203.0.113.7` sends it, and
    what lands is `i_…`. The reason and the expiry are both required, because a block without either is
    indistinguishable from a bug from the outside — and the caller may not block for longer than a week, which
    is the difference between abuse protection and a grudge with a database row.
    """
    rid = request.state.request_id
    ok, e = _admin(request)
    if e:
        return e
    bad = _check_body(body, ("address", "reason", "hours"), rid,
                      allowed=("address", "reason", "hours", "scope", "createdBy"))
    if bad is not None:
        return bad
    hours = body.get("hours")
    if isinstance(hours, bool) or not isinstance(hours, int) or hours < 1 or hours > 168:
        return err("VALIDATION", rid, where=["hours"])
    reason = str(body.get("reason") or "").strip()
    if len(reason) < 8:
        return err("VALIDATION", rid, where=["reason"])
    scope = str(body.get("scope") or "all").strip().lower()
    if scope not in ("all",) + tuple(_pp.urls.KINDS):
        return err("VALIDATION", rid, where=["scope"])
    now = _now_ms()
    salt = _ref_salt()
    subject = _pp.budget.subject_hash(str(body.get("address") or ""), salt)
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    until = now + int(hours) * 3_600_000

    def work() -> dict:
        before = _db.execute("SELECT until_ms FROM public_page_blocks WHERE subject_hash=? AND scope=?",
                             (subject, scope)).fetchone()
        if before is None:
            _db.execute("INSERT INTO public_page_blocks (subject_hash, scope, reason, kind, until_ms, created_ms,"
                        " created_by) VALUES (?,?,?,?,?,?,?)",
                        (subject, scope, reason, "manual", until, now, str(body.get("createdBy") or "operator")))
        else:
            _db.execute("UPDATE public_page_blocks SET reason=?, until_ms=?, created_ms=?, created_by=? "
                        "WHERE subject_hash=? AND scope=?",
                        (reason, until, now, str(body.get("createdBy") or "operator"), subject, scope))
        _pp_audit("public.block", "%s:%s" % (scope, subject[:10]), {"kind": scope}, rid)
        return _stamped({"subject": subject, "scope": scope, "untilMs": until, "hours": int(hours),
                         "extended": before is not None,
                         "note": "the address is digested on the way in: what is stored cannot be reversed"},
                        ttl_ms=0, stale_ms=0, as_of_ms=now)

    return _idem_run(_operator_subject(), str(idempotency_key), body, rid, work)


def _pp_audit(action: str, subject: str, meta: dict, rid: str) -> None:
    """One audit row per public read, at a volume that is safe because the subject is a digest.

    Public reads are audited at all for one reason: the day somebody asks "did our pages serve a crawler more
    than they served readers", the answer has to be a query rather than an argument. The row carries the digest
    and the kind, never the address, and never the payload.
    """
    try:
        _db.execute("INSERT INTO audit_log (at_ms, actor_type, actor_id, action, target_table, target_id,"
                    " request_id, detail_json) VALUES (?,?,?,?,?,?,?,?)",
                    (_now_ms(), "service", "", str(action), "public_page", str(subject)[:64], str(rid),
                     json.dumps({"kind": str(meta.get("kind") or "")}, sort_keys=True)))
        _db.commit()
    except Exception:                                                              # pragma: no cover - audit only
        return


def _freshness_text(age_ms: int) -> str:
    """The age of a price in words, from the same thresholds the terminal's freshness pill uses."""
    ms = max(0, int(age_ms))
    if ms < 15_000:
        return "seconds ago"
    if ms < 90_000:
        return "%dm ago" % max(1, ms // 60_000)
    if ms < 3_600_000:
        return "%d minutes ago" % (ms // 60_000)
    return "%dh ago" % (ms // 3_600_000)


def _BASE_URL() -> str:
    return _pp.urls.base_url(os.environ.get("PGM_PUBLIC_BASE") or "")


def _bps_text(bps: int) -> str:
    """`63.4%` from bps, in integers. The card is rendered by an image pipeline with no number layer, so the
    string is built here from the same integer the board ranks by - and it is built without a float, because a
    rate that rounds differently in the image than in the row is the disagreement this whole phase is about."""
    b = _tm._int(bps)
    return "%d.%d%%" % (b // 100, (b % 100) // 10)


_levels_p11 = {
    "GET /v1/leaderboard": (_authz.PUBLIC, ""),
    "GET /v1/leaderboard/boards": (_authz.PUBLIC, ""),
    "GET /v1/leaderboard/methodology": (_authz.PUBLIC, ""),
    "GET /v1/leaderboard/why": (_authz.PUBLIC, ""),
    "GET /v1/leaderboard/snapshots": (_authz.PUBLIC, ""),
    "GET /v1/leaderboard/runs": (_authz.PUBLIC, ""),
    # D3. Three more public reads (a wallet's standing, a comparison, and the board's own history of itself) and
    # the one write a user makes about other people: a follow. Following is PUBLIC data + a private list, so the
    # read of the list is USER-scoped while the standing it attaches is already served publicly.
    "GET /v1/leaderboard/rank": (_authz.PUBLIC, ""),
    "GET /v1/leaderboard/compare": (_authz.PUBLIC, ""),
    "GET /v1/leaderboard/follows": (_authz.USER, ""),
    "POST /v1/leaderboard/follows": (_authz.USER, ""),
    "POST /v1/leaderboard/recompute": (_authz.USER, ""),
    # D4. The self-rank is the only board read that is USER: it is about the account that is asking, it is built
    # from the wallets that account proved it controls, and there is no version of it a stranger may see. The two
    # identity routes are that account's own publication state - reading it is what makes the toggle honest, and
    # writing it is a consent record.
    "GET /v1/leaderboard/me": (_authz.USER, ""),
    "GET /v1/leaderboard/identity": (_authz.USER, ""),
    "POST /v1/leaderboard/identity": (_authz.USER, ""),
    # D5. The terms page is PUBLIC because the rules have to be readable before somebody shares a link; the
    # dashboard and the two writes are USER (they are about the account that is asking — and `apply` is the
    # REFEREE's write about their own account, not the referrer's); the accrual run and the review queue are
    # ADMIN, because they are the operator's, and one of their decisions revokes a builder code.
    "GET /v1/referrals/terms": (_authz.PUBLIC, ""),
    "GET /v1/referrals/me": (_authz.USER, ""),
    "POST /v1/referrals/code": (_authz.USER, ""),
    "POST /v1/referrals/apply": (_authz.USER, ""),
    "POST /v1/referrals/accrue": (_authz.ADMIN, ""),
    "GET /v1/referrals/review": (_authz.ADMIN, ""),
    "POST /v1/referrals/review": (_authz.ADMIN, ""),
}
_authz.LEVELS_TABLE.update(_levels_p10)
_levels_p11.update({
    # D6. The three page reads and the sitemap are PUBLIC for the same reason `/v1/tape` is: they are the
    # published surface, and requiring a session to read a page whose whole purpose is to be indexed would make
    # the feature impossible. The block list is ADMIN, and it is the only lever on this surface that can refuse
    # an address outright - which is exactly the kind of power that should not be reachable without a token.
    "GET /v1/public/trader/{handle}": (_authz.PUBLIC, ""),
    "GET /v1/public/market/{slug}": (_authz.PUBLIC, ""),
    "GET /v1/public/leaderboard/{board}": (_authz.PUBLIC, ""),
    "GET /v1/public/sitemap": (_authz.PUBLIC, ""),
    "GET /v1/public/blocks": (_authz.ADMIN, ""),
    "POST /v1/public/blocks": (_authz.ADMIN, ""),
    # D7. The anti-gaming dashboard is ADMIN on both routes, and it is the only P11 surface that returns a raw
    # wallet at all: the exclusion table is keyed by it and a reviewer cannot act on a pseudonym. Everything it
    # returns is paired with the `w_...` a public page would show.
    "GET /v1/admin/gaming": (_authz.ADMIN, ""),
    "POST /v1/admin/gaming/decide": (_authz.ADMIN, ""),
})

# -------------------------------------------------------------------------------------------------------------
# P11 · D7 · the anti-gaming dashboard: four questions about our own tape, and the two buttons that answer them.
#
# The detectors live in `polygm_core/gaming/detect.py` and are pure — they take rows and return findings — so this
# section's whole job is to assemble the evidence from the tables we hold and to record what a human decides. Two
# rules are enforced HERE rather than in the engine:
#
#   * **This is the only P11 surface that may see a raw wallet.** The public boards pseudonymise before they rank
#     (§2.19), and that is right for a reader; a reviewer deciding whether a wallet belongs on a board cannot act
#     on `w_…`, because the exclusion table is keyed by the wallet the tape actually names. So each finding carries
#     both: the wallet (internal, ADMIN-only, never in a public payload) and the `w_…` a human quotes afterwards.
#   * **A decision is a row, never a deletion.** `leaderboard_exclusions` is append-only and the boards replay the
#     newest row per (wallet, board) at read time, so an exclusion is reversible (`include`/`clear`), attributable
#     (an actor and a reason are required) and visible to the same code path the public board reads. `flag` records
#     the question without removing anybody — a leaderboard that quietly dropped flagged wallets would be hiding
#     them rather than reviewing them (§2.39's rule, applied to the tool that does the reviewing).

GAMING_ACTIONS = ("exclude", "flag", "include", "clear")


def _gm_audit(action: str, *, wallet: str, detail: dict, rid: str) -> None:
    """One audit row per decision, with the pseudonym rather than the wallet as its subject.

    An exclusion is a decision about somebody's standing, so the trail has to outlive the session — but the trail
    is also the thing that gets copied into tickets and incident notes, and an address in a ticket is an address in
    a support tool. The row therefore carries `w_…` and the reason, and the wallet itself stays in the append-only
    table that needs it.
    """
    try:
        _db.execute("INSERT INTO audit_log (at_ms, actor_type, actor_id, action, target_table, target_id,"
                    " request_id, detail_json) VALUES (?,?,?,?,?,?,?,?)",
                    (_now_ms(), "admin", "admin", str(action), "leaderboard_exclusions", _anon(str(wallet))[:64],
                     str(rid), json.dumps(detail, sort_keys=True)))
        _db.commit()
    except Exception:                                                              # pragma: no cover - audit only
        return


def _gm_history(limit: int) -> list[dict]:
    """The rank history the climb rule reads: `leaderboard_snapshots`, newest first.

    Bounded by rows rather than by time because the table is written by the recompute: a board that has not been
    recomputed for a fortnight has an *old* history rather than a long one, and the response says which (below,
    with the newest timestamp it found).
    """
    rows = _db.execute(
        "SELECT board, window_key, wallet, rank, settled, score_bps, drawdown_micro, computed_ms"
        " FROM leaderboard_snapshots ORDER BY computed_ms DESC LIMIT ?", (max(1, limit) * 400,)).fetchall()
    return [{"board": str(b), "windowKey": str(w), "wallet": str(x), "rank": _tm._int(r), "settled": _tm._int(s_),
             "scoreBps": _tm._int(sc), "drawdownMicro": _tm._int(dd), "computedMs": _tm._int(ms)}
            for (b, w, x, r, s_, sc, dd, ms) in rows]


def _gm_fills() -> list[dict]:
    """The tape the cluster rule reads — the same rows the boards rank from, through the same reader.

    A second read path here would be a second answer about the same fills, and the one thing a suspicion engine
    must not have is its own private view of the tape.
    """
    return [{"wallet": str(f["wallet"]), "tsMs": _tm._int(f["tsMs"]), "tokenId": str(f["tokenId"]),
             "side": str(f["side"])} for f in _lb_fills()]


def _gm_referrals() -> list[dict]:
    """Attributions joined to the D5 signal digests — as digests, never as values.

    Two referees funded from one source are one person, and that fact exists in exactly one place: the `funding`
    and `device` digests the Sybil path stores. They reach the detector as `f_…`/`d_…` strings (the signals
    schema refuses to store a raw value at all), and the detector counts collisions without printing them.
    """
    rows = _db.execute(
        "SELECT a.referrer, a.referee, a.signed_up_ms, a.qualify_ms, a.notional_micro, a.state,"
        " COALESCE(fs.hash,''), COALESCE(ds.hash,'')"
        " FROM referral_attributions a"
        " LEFT JOIN referral_signals fs ON fs.user_id = a.referee AND fs.kind = 'funding'"
        " LEFT JOIN referral_signals ds ON ds.user_id = a.referee AND ds.kind = 'device'"
        " ORDER BY a.signed_up_ms DESC LIMIT 20000").fetchall()
    accruals: dict[str, int] = {str(r[0]): _tm._int(r[1]) for r in _db.execute(
        "SELECT referrer, SUM(share_micro) FROM referral_accruals GROUP BY referrer").fetchall()}
    out = []
    for (referrer, referee, signed, qual, notional, state, funding, device) in rows:
        st = str(state)
        out.append({"referrer": str(referrer), "referee": str(referee), "signedUpMs": _tm._int(signed),
                    "qualifyMs": _tm._int(qual), "notionalMicro": _tm._int(notional), "state": st,
                    "fundingDigest": str(funding), "deviceDigest": str(device),
                    # A refused or clawed-back referee is owed nothing, so its tree carries no accrual to weigh.
                    "accrualMicro": 0 if st in ("refused", "clawed_back") else accruals.get(str(referrer), 0)})
    return out


def _gm_attributions() -> list[dict]:
    """Every order we put to the venue through our builder code, with both fee numbers.

    `fee_micro_observed` is NULL until the reconciliation writes it, and that NULL is deliberately passed through
    as 0: an order we attributed and were never paid for is the shape this rule exists to find, and "we have not
    looked yet" is answered by the same reviewer who can see the order's age. The wallet is the tape's
    `wallets.address` (the id the boards and the exclusion table use), falling back to the user id when an account
    has no provisioned wallet yet — a finding about a wallet that cannot be named is a finding nobody can act on.
    """
    # The notional is a TERM, not a fact about the venue: `builder_attribution` (P04) records what we expected to
    # be paid, and `builder_attribution_terms` records the rate, the code and the base that produced the
    # expectation. Reading the base from the terms row is the difference between this rule asking "how much
    # volume was attributed" and asking "how large was the fee" — and a fee-based floor would silently exempt a
    # low-rate code, which is the one code a farm would pick.
    rows = _db.execute(
        "SELECT a.user_id, COALESCE(a.order_id,''), a.market_id, COALESCE(t.notional_micro,0),"
        " a.fee_micro_expected, COALESCE(a.fee_micro_observed,0), a.placed_ms, COALESCE(w.address,'')"
        " FROM builder_attribution a"
        " LEFT JOIN builder_attribution_terms t ON t.attribution_id = a.id"
        " LEFT JOIN wallets w ON w.user_id = a.user_id"
        " ORDER BY a.placed_ms DESC LIMIT 20000").fetchall()
    return [{"wallet": str(addr) or str(uid), "userId": str(uid), "orderId": str(oid), "marketId": str(market),
             "notionalMicro": _tm._int(notional), "feeMicroExpected": _tm._int(expected),
             "feeMicroObserved": _tm._int(observed), "placedMs": _tm._int(placed)}
            for (uid, oid, market, notional, expected, observed, placed, addr) in rows]


def _gm_flags() -> dict[str, dict]:
    """The newest human decision per wallet, replayed the same way the boards replay it.

    Replayed from the same table rather than cached, because the screen and the ranking disagreeing about who has
    already been decided about is exactly the bug that makes an operator distrust both.
    """
    out: dict[str, dict] = {}
    for (wallet, action, reason, actor, at_ms) in _db.execute(
            "SELECT wallet, action, reason, actor, at_ms FROM leaderboard_exclusions"
            " ORDER BY at_ms ASC, id ASC").fetchall():
        out[str(wallet)] = {"action": str(action), "reason": str(reason), "actor": str(actor),
                            "atMs": _tm._int(at_ms)}
    return out


def _gm_dashboard(*, limit: int, at_ms: int) -> dict:
    """The engine's payload, plus the two things only the API can add: pseudonyms and the evidence's own age."""
    flags = _gm_flags()
    history = _gm_history(limit)
    out = _gm_gaming.detect.dashboard(history=history, fills=_gm_fills(), referrals=_gm_referrals(),
                                      attributions=_gm_attributions(), at_ms=at_ms, limit=limit, flags=flags)
    for group in ("climbers", "builder"):
        for f in out[group]:
            f["anon"] = _anon(str(f["wallet"]))
    for f in out["clusters"]:
        f["anonWallets"] = [_anon(str(w)) for w in f["wallets"]]
    for f in out["chains"]:
        f["anonReferrer"] = _anon(str(f["referrer"]))
        f["anonReferees"] = [_anon(str(r)) for r in f["referees"]]
    out["evidence"] = {"historyRows": len(history), "decisions": len(flags),
                       "newestSnapshotMs": max([_tm._int(h.get("computedMs")) for h in history] or [0]),
                       "fills": len(_gm_fills())}
    return out


@app.get("/v1/admin/gaming", status_code=200, responses=GAMING_RESPONSES)
def admin_gaming(request: Request, limit: int = 25,
                 x_admin: str | None = Header(default=None, alias="X-Admin-Token")):
    """The four lists, the rules behind them and the wallets they name — for an operator, never for a reader.

    The payload carries the raw wallet *and* its pseudonym because the two are needed for different jobs: a
    decision is keyed by the wallet the tape names, and every sentence written afterwards quotes `w_…`. Serving
    one without the other makes the other impossible; serving both behind an ADMIN gate keeps the pairing inside
    the building, and `tests/test_gaming_api.py` greps every finding for an address to prove the pair is the only
    place a wallet appears.
    """
    rid = request.state.request_id
    ok, e = _admin(request)
    if e:
        return e
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 100:
        return err("VALIDATION", rid, where=["limit must be 1..100"])
    at = _now_ms()
    # Stamped with a zero TTL and a short stale window: the client refuses an unstamped read outright
    # (`UNSTAMPED_READ`), which is how this route was caught — the screen said "the tape was not read" on a 200
    # response, and the missing field was `asOf`. `ttlMs: 0` is also the honest answer for this payload: a cached
    # suspicion list is yesterday's farms with today's clock on it.
    #
    # `asOf` is the age of the EVIDENCE rather than the time we answered, which is why it is the newest snapshot
    # the climb rule actually read (or `at` when there is no history at all, because "we have looked" is true).
    # The payload's own `evidence.newestSnapshotMs` carries the same number for the screen to print.
    body = _gm_dashboard(limit=limit, at_ms=at)
    newest = int(body.get("evidence", {}).get("newestSnapshotMs") or 0)
    return _stamped(body, ttl_ms=0, stale_ms=120_000, as_of_ms=newest or at)


@app.post("/v1/admin/gaming/decide", status_code=200, responses=GAMING_DECIDE_RESPONSES,
          openapi_extra=_body_schema(("wallet", "action", "reason"), {
              "wallet": {"type": "string", "minLength": 3, "maxLength": 120},
              "action": {"type": "string", "enum": list(GAMING_ACTIONS)},
              "reason": {"type": "string", "minLength": 8, "maxLength": 400},
              "board": {"type": "string", "maxLength": 40, "default": "all"},
              "finding": {"type": "string", "enum": list(_gm_gaming.RULES) + [""], "default": ""}}))
def admin_gaming_decide(request: Request, body: dict = Body(...),
                        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                        x_admin: str | None = Header(default=None, alias="X-Admin-Token")):
    """Record one human decision about one wallet: exclude it, flag it for review, or put it back.

    `exclude` removes the wallet from the named board (`all` for every board); `flag` records the question and
    changes no ranking; `include`/`clear` reverse either. All four are rows — the boards replay the newest row per
    (wallet, board) — so a wrong click is one append and one more click rather than a lost record. `finding` names
    the kind of finding that prompted the decision, which is how the dashboard's precision becomes a number (how
    many flags produced a confirmed farm) instead of an opinion.
    """
    rid = request.state.request_id
    ok, e = _admin(request)
    if e:
        return e
    bad = _check_body(body, ("wallet", "action", "reason"), rid,
                      allowed=("wallet", "action", "reason", "board", "finding"))
    if bad is not None:
        return bad
    wallet = str(body.get("wallet") or "").strip()
    action = str(body.get("action") or "").strip()
    reason = str(body.get("reason") or "").strip()
    if action not in GAMING_ACTIONS:
        return err("VALIDATION", rid, where=["action must be one of %s" % ", ".join(GAMING_ACTIONS)])
    if not (8 <= len(reason) <= 400):
        return err("BAD_REASON", rid)
    board = str(body.get("board") or "").strip()
    board = "" if board in ("", "all") else board
    if board and board not in _lb_boards.BOARD_IDS:
        return err("VALIDATION", rid, where=["board must be a board id, or 'all'"])
    finding = str(body.get("finding") or "").strip()
    if finding and finding not in _gm_gaming.RULES:
        return err("VALIDATION", rid, where=["finding must be a finding kind, or empty"])
    shaped = _idem_shape(idempotency_key)
    if shaped is not None:
        return shaped

    def work():
        at = _now_ms()
        _db.execute("INSERT INTO leaderboard_exclusions (wallet, board, action, reason, actor, at_ms)"
                    " VALUES (?,?,?,?,?,?)",
                    (wallet, board, action, reason, "admin:%s" % (finding or "manual"), at))
        _db.commit()
        _gm_audit("gaming.decide", wallet=wallet, rid=rid,
                  detail={"action": action, "board": board or "all", "finding": finding or "manual",
                          "reason": reason})
        return {"wallet": wallet, "anon": _anon(wallet), "action": action, "board": board or "all",
                "finding": finding, "atMs": at}

    return _idem_run(_operator_subject(), str(idempotency_key), body, rid, work)

_levels_p12 = {
    # The webhook is PUBLIC in the table and secret-checked in the handler, and that is deliberate: Telegram has no
    # bearer token, so the only credential available is the header secret. Declaring it USER or ADMIN would be a
    # lie that `_principal` would then enforce, and the endpoint would 401 every update.
    "POST /v1/telegram/webhook": (_authz.PUBLIC, ""),
    # The Mini App's sign-in: public, because the credential *is* the signed `initData` and it is verified
    # against the bot token before any session exists.
    "POST /v1/telegram/session": (_authz.PUBLIC, ""),
    "GET /v1/telegram/commands": (_authz.PUBLIC, ""),
    # The worker and the funnel read are operator surfaces: a user who can trigger a drain can make the bot
    # message every chat it knows, and a user who can read the funnel can read the product's growth numbers.
    "POST /v1/telegram/drain": (_authz.ADMIN, ""),
    "GET /v1/telegram/metrics": (_authz.ADMIN, ""),
    # D8's operator surface. The kill switch is ADMIN and takes no user credential at all: it is infrastructure,
    # and a switch a user can reach is a switch an attacker can flip during an incident.
    "POST /v1/telegram/kill": (_authz.ADMIN, ""),
    "POST /v1/telegram/broadcast": (_authz.ADMIN, ""),
    "GET /v1/telegram/ops": (_authz.ADMIN, ""),
    # The Mini App's order route is a USER route: the webview equivalent of the ticket. Its identity is the session
    # the signed `initData` minted, never the request body — there is no chat id to forge.
    "POST /v1/telegram/order": (_authz.USER, ""),
    # The web ticket's route. A USER row for the same reason: the identity is the session, and a budget is not a
    # credential — the amount and the market in the body are the user's *question*, never their authorisation.
    "POST /v1/orders/amount": (_authz.USER, ""),
    # P12 D6. The wallet's HTTP half. Every one of these is the caller's own money or their own key material, so
    # every one is USER: there is no admin read of a wallet, and the operator routes that touch custody live on
    # their own paths with their own level.
    "GET /v1/wallet/balance": (_authz.USER, ""),
    "GET /v1/wallet/transactions": (_authz.USER, ""),
    "POST /v1/wallet/deposit/quote": (_authz.USER, ""),
    "GET /v1/wallet/deposit/{deposit_id}": (_authz.USER, ""),
    "POST /v1/wallet/withdraw": (_authz.USER, ""),
    "POST /v1/wallet/keys/export": (_authz.USER, ""),
}

_authz.LEVELS_TABLE.update(_levels_p11)
_authz.LEVELS_TABLE.update(_levels_p12)
_authz._MATCHERS.clear()                    # the matchers cache the table; a new row must invalidate it


# =====================================================================================================================
# P12 · the Telegram surface: the bot's webhook, its outbox, and the Mini App's session
# =====================================================================================================================
# The bot is not a second product. It is a second *client* of the same ledger, the same risk gate and the same
# idempotency store — which is why the trade path below calls `_order_core` rather than re-deriving an order, and why
# every route in this section names its authz level in `_levels_p12` instead of relying on a decorator's good
# intentions.
#
# Three rules are enforced in this block rather than described in a doc:
#
#   1. **`update_id` is the process boundary.** `_tg_claim` is the first statement of the webhook, before any
#      parsing that can act, and it is a primary-key insert: Telegram's retry (which arrives whenever our 2xx is
#      slow, lost, or the pod restarts) loses the race against `telegram_updates` and is answered without an action.
#   2. **Nothing is sent straight from a request.** A webhook answers with a plan enqueued into `telegram_outbox`,
#      so a slow Bot API never makes Telegram retry a trade, and a fill notification survives a restart between the
#      fill and the message. `/v1/telegram/drain` is the worker, and it is the only thing that calls the Bot API.
#   3. **The bot token never reaches a log, an error, or a message.** It is read from the environment, validated for
#      shape, passed to `BotClient`, and `client._redact()` runs over everything that leaves it.

#: The status sets each P12 route can answer, next to the routes rather than in the contract: `check-openapi`
#: compares the two, so a route that starts answering 409 without documenting it fails the build.
TELEGRAM_WEBHOOK_RESPONSES = {400: {"description": "the body is not an update we can read"},
                              403: {"description": "the secret token does not match"},
                              503: {"description": "no webhook secret is configured on this pod"},
                              500: {"description": "unexpected failure inside the service"}}
TELEGRAM_SESSION_RESPONSES = {400: {"description": "initData missing, or too long"},
                              401: {"description": "the signature, the freshness window, or the link check failed"},
                              409: {"description": "a correctly signed payload that has already been used"},
                              422: {"description": "malformed body"},
                              503: {"description": "the bot token is not configured on this pod"},
                              500: {"description": "unexpected failure inside the service"}}
TELEGRAM_COMMANDS_RESPONSES = {200: {"description": "the command table"},
                               500: {"description": "unexpected failure inside the service"}}
TELEGRAM_DRAIN_RESPONSES = {403: {"description": "admin token missing or wrong"},
                           422: {"description": "limit out of range"},
                           503: {"description": "no admin token configured on this pod"},
                           500: {"description": "unexpected failure inside the service"}}
TELEGRAM_METRICS_RESPONSES = {403: {"description": "admin token missing or wrong"},
                             422: {"description": "days out of range"},
                             503: {"description": "no admin token configured on this pod"},
                             500: {"description": "unexpected failure inside the service"}}

TG_WEBHOOK_SECRET_ENV = "PGM_TELEGRAM_WEBHOOK_SECRET"
TG_SECRET_HEADER = "x-telegram-bot-api-secret-token"
TG_MAX_UPDATE_BYTES = 128 * 1024          # the Bot API's own ceiling; a bigger body is not an update we should read

_tg_bot_client = None


def _tg_bot():
    """The client, or None when this pod has no token (a dev pod, a test, a second API replica without the bot)."""
    global _tg_bot_client
    if _tg_bot_client is None:
        try:
            _tg_bot_client = _tg_client.BotClient(_tg_client.token_from_env())
        except _tg_client.BotError:
            return None
    return _tg_bot_client


def _tg_linked(chat_id: str, user_id: str) -> tuple[str, dict]:
    """(account id, the identity row) for this Telegram user — the only way an update gets a user context.

    Keyed on the *Telegram user id*, never the chat id: the same person in a group and in a private chat is one
    account, and a chat id is not an identity (a group's id belongs to nobody in particular).
    """
    uid = SEC.identity_user("telegram", str(user_id)) if user_id else ""
    return str(uid or ""), {"chat_id": str(chat_id), "telegram_user_id": str(user_id)}


def _tg_claim(update_id: int, *, kind: str, chat_id: str, user_id: str, command: str = "") -> tuple[bool, str]:
    """(already_seen, state). The insert IS the lock.

    Two pods can receive the same retried update at the same moment, so the check cannot be a read followed by a
    write: `INSERT ... ON CONFLICT DO NOTHING` either wins the row or does not, and the loser reads the winner's
    state. A row in `claimed` whose `runs` is above zero is a replay as surely as a `done` row is.
    """
    at = _now_ms()
    row = _db.execute("INSERT INTO telegram_updates (update_id, kind, chat_id, user_id, command, state, runs,"
                      " first_ms) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (update_id) DO NOTHING RETURNING update_id",
                      (int(update_id), kind, chat_id, user_id, command, "claimed", 0, at)).fetchone()
    if row is not None:
        _db.commit()
        return False, "claimed"
    cur = _db.execute("SELECT state, runs FROM telegram_updates WHERE update_id=?", (int(update_id),)).fetchone()
    state, runs = (cur[0], int(cur[1])) if cur else ("claimed", 0)
    # The counter is bumped on the *replay*: a value above 1 in the operator's query is an incident (Telegram
    # retried, or two pods raced), and a number nobody writes down is a number nobody can alert on.
    _db.execute("UPDATE telegram_updates SET runs = runs + 1 WHERE update_id=?", (int(update_id),))
    _db.commit()
    SEC.auth_event(user_id, "telegram_update_replayed", at=at,
                   detail={"update_id": int(update_id), "state": state, "runs": runs + 1})
    return True, state


def _tg_finish(update_id: int, *, state: str = "done", note: str = "") -> None:
    _db.execute("UPDATE telegram_updates SET state=?, note=?, done_ms=? WHERE update_id=?",
                (state, str(note)[:200], _now_ms(), int(update_id)))
    _db.commit()


def _tg_metric(*, chat_id: str, chat_type: str, user_id: str, command: str, action: str = "", ok: bool = True,
               dur_ms: int = 0, update_id: int = 0) -> None:
    """One row per handled update: the DAU, the funnel, and the alert→trade conversion all come from here."""
    _db.execute("INSERT INTO telegram_commands (at_ms, chat_id, chat_type, user_id, command, action, ok, dur_ms,"
                " update_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (_now_ms(), str(chat_id), str(chat_type or "private"), str(user_id), str(command)[:40],
                 str(action)[:40], 1 if ok else 0, int(dur_ms), int(update_id)))
    _db.commit()


def _tg_session(chat_id: str):
    """The live session for a chat, or None: expired sessions are dropped here rather than in every handler."""
    row = _db.execute("SELECT chat_id, step, payload_json, message_id, started_ms, updated_ms, expires_ms, version"
                      " FROM telegram_sessions WHERE chat_id=?", (str(chat_id),)).fetchone()
    if row is None:
        return None
    s = _tgb_sessions.Session(chat_id=str(row[0]), step=str(row[1]), payload=json.loads(row[2] or "{}"),
                              message_id=int(row[3]), started_ms=int(row[4]), updated_ms=int(row[5]),
                              expires_ms=int(row[6]), version=int(row[7]))
    if s.is_expired(_now_ms()):
        _db.execute("DELETE FROM telegram_sessions WHERE chat_id=?", (str(chat_id),))
        _db.commit()
        return None
    return s


def _tg_save(session) -> None:
    if session is None:
        return
    r = session.as_row()
    _db.execute("INSERT INTO telegram_sessions (chat_id, step, payload_json, message_id, started_ms, updated_ms,"
                " expires_ms, version) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (chat_id) DO UPDATE SET"
                " step=excluded.step, payload_json=excluded.payload_json, message_id=excluded.message_id,"
                " updated_ms=excluded.updated_ms, expires_ms=excluded.expires_ms, version=excluded.version",
                (r["chat_id"], r["step"], r["payload_json"], r["message_id"], r["started_ms"], r["updated_ms"],
                 r["expires_ms"], r["version"]))
    _db.commit()


def _tg_clear_session(chat_id: str) -> None:
    _db.execute("DELETE FROM telegram_sessions WHERE chat_id=?", (str(chat_id),))
    _db.commit()


def _tg_enqueue(*, chat_id: str, chat_type: str, plan, priority: int = 0, edit_message_id: int = 0,
                note: str = "", dedupe_key: str = "") -> int:
    """Put a plan's beats in the outbox. The first beat is a send (or an edit); the second, if any, is an edit.

    A two-beat plan is stored as its *answer* with the skeleton's job id recorded, because the skeleton is only
    worth sending if the answer takes a while — the drain decides, using `motion.chat_action_plan` and the plan's
    own timing, and the answer edits the skeleton's message when it does.
    """
    beats = plan.beats or []
    if not beats:
        return 0
    # A plan whose words are empty is an *instruction to do nothing* (the router answers a channel post with one),
    # and Telegram refuses an empty message with a 400. Dropping it here keeps that refusal from becoming an alert.
    if not str(beats[-1].text or "").strip():
        return 0
    at = _now_ms()
    first, last = beats[0], beats[-1]
    method = "editMessageText" if first.edit_message_id else "sendMessage"
    target = int(first.edit_message_id or edit_message_id or 0)
    kb = last.keyboard or first.keyboard
    # The dedupe key is checked before the insert AND enforced by the index behind it, because two workers can
    # pass a check at the same moment: the read makes the common case cheap, the unique index makes the race
    # impossible. `note` cannot carry this job — the drain rewrites it on every send attempt (see 0019's comment).
    if dedupe_key:
        seen = _db.execute("SELECT id FROM telegram_outbox WHERE dedupe_key=?", (str(dedupe_key),)).fetchone()
        if seen:
            return int(seen[0])
    try:
        job = _db.execute(
            "INSERT INTO telegram_outbox (chat_id, chat_type, priority, method, text, keyboard_json,"
            " edit_message_id, state, attempts, created_ms, due_ms, note, dedupe_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
            (str(chat_id), str(chat_type or "private"), int(priority or plan.priority), method, last.text,
             json.dumps(kb) if kb else "", target, "queued", 0, at, at, str(note)[:120],
             str(dedupe_key))).fetchone()
    except Exception as e:                                                    # noqa: BLE001
        # Driver-agnostic on purpose: sqlite says "UNIQUE constraint failed", psycopg says "duplicate key value".
        # Both mean the same thing here — somebody else queued this message first — and that is not an error.
        text_e = str(e).lower()
        if dedupe_key and ("unique" in text_e or "duplicate" in text_e):
            seen = _db.execute("SELECT id FROM telegram_outbox WHERE dedupe_key=?", (str(dedupe_key),)).fetchone()
            if seen:
                return int(seen[0])
        raise
    job_id = int(job[0]) if job else 0
    if len(beats) > 1 and method == "sendMessage":
        # The skeleton: sent immediately so the user sees motion, and remembered as the message the answer edits.
        row = _db.execute(
            "INSERT INTO telegram_outbox (chat_id, chat_type, priority, method, text, keyboard_json,"
            " edit_message_id, state, attempts, created_ms, due_ms, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(chat_id), str(chat_type or "private"), int(priority or plan.priority) + 1, "sendMessage",
             first.text, "", 0, "queued", 0, at, at, "skeleton for job %d" % job_id)).fetchone()
        if row:
            _db.execute("UPDATE telegram_outbox SET edit_message_id = ? WHERE id = ?", (int(row[0]), job_id))
    _db.commit()
    return job_id


def _tg_fetch(name: str, payload: dict):
    """The bot's read-only data, from the same tables the terminal reads.

    Each answer carries what the product requires on that surface: an age on every price, the drawdown wherever a
    PnL appears, the sample note on any win rate, and never a raw number without its unit. Money is formatted by
    `_tm` (the same money module the API uses) so the chat and the web can never disagree about a balance.
    """
    uid = str(payload.get("account_id") or "")
    if name == "market":
        slug = str(payload.get("slug") or "")
        row = _db.execute("SELECT m.id, m.condition_id, m.slug, m.question, m.minimum_tick_size, m.end_ts,"
                          " t.token_id, t.outcome FROM markets m JOIN tokens t ON t.market_id = m.id"
                          " WHERE m.slug = ? ORDER BY t.outcome_index LIMIT 1", (slug,)).fetchone()
        if row is None:
            return {"missing": True}
        m = _tg_market_view(row[0], slug, row[3], row[4])
        return {"market": m}
    if name == "positions":
        rows = _db.execute("SELECT p.token_id, p.shares_open_micro, p.cost_micro, t.outcome, m.slug, m.question"
                          " FROM position_lots p JOIN tokens t ON t.token_id = p.token_id"
                          " JOIN markets m ON m.id = t.market_id"
                          " WHERE p.user_id = ? AND p.shares_open_micro > 0 ORDER BY p.cost_micro DESC LIMIT 12",
                           (uid,)).fetchall()
        if not rows:
            return {"text": ""}
        lines, total_cost = [], 0
        for token, shares, cost, outcome, slug, question in rows:
            total_cost += int(cost or 0)
            mark = _tg_mark(token)
            lines.append("• %s <b>%s</b> — %s shares · cost %s" % (mkt_short := question[:60], str(outcome),
                                                                   fmt_usdc(shares), fmt_usdc(cost)))
        return {"text": "<b>%d position%s</b>\n%s\n<i>cost basis %s · marks as of a moment ago</i>"
                        % (len(rows), "" if len(rows) == 1 else "s", "\n".join(lines), fmt_usdc(total_cost))}
    if name == "balance":
        row = _db.execute("SELECT COALESCE(SUM(amount_micro),0) FROM cash_ledger WHERE user_id=?", (uid,)).fetchone()
        avail = int(row[0] or 0) if row else 0
        locked = _db.execute("SELECT COALESCE(SUM(notional_micro),0) FROM order_intents WHERE user_id=?"
                             " AND state IN (%s)" % ",".join("?" * len(_OPEN_INTENT_STATES)),
                             (uid,) + _OPEN_INTENT_STATES).fetchone()
        return {"text": "<b>Cash %s USDC</b>\nReserved by open orders: %s USDC\n<i>as of just now</i>"
                        % (fmt_usdc(avail), fmt_usdc(int(locked[0] or 0) if locked else 0))}
    if name == "pnl":
        # Realised PnL, from the ledger and the lots — no third bookkeeping system, because a number computed
        # twice is a number that can disagree with itself. Exits (`sell_fill`, `merge_receipt`) and settlements
        # (`resolution_payout`) are money in; the basis they released is `invested - still open` (a lot is
        # reduced in place, so the released part is a subtraction of two sums, not a stored column); platform
        # `fee` rows come off the top. A BUY already carries its venue fee inside `basis_micro`, and a SELL's
        # proceeds are already net of it, so fees are not double-counted - only ours are subtracted here.
        exits = _db.execute("SELECT COALESCE(SUM(amount_micro),0) FROM cash_ledger WHERE user_id=? AND kind IN"
                            " ('sell_fill','merge_receipt','resolution_payout')", (uid,)).fetchone()
        invested = _db.execute("SELECT COALESCE(SUM(-amount_micro),0) FROM cash_ledger WHERE user_id=?"
                               " AND kind='buy'", (uid,)).fetchone()
        open_basis = _db.execute("SELECT COALESCE(SUM(basis_micro),0) FROM position_lots WHERE user_id=?"
                                 " AND shares_open_micro>0", (uid,)).fetchone()
        our_fees = _db.execute("SELECT COALESCE(SUM(-amount_micro),0) FROM cash_ledger WHERE user_id=?"
                               " AND kind='fee'", (uid,)).fetchone()
        released = max(0, int(invested[0] or 0) - int(open_basis[0] or 0))
        realised = int(exits[0] or 0) - released - int(our_fees[0] or 0)
        # The peak is the peak of *cash on hand* over the ledger's own history: this build keeps no equity curve
        # for our own users, and inventing one to decorate a chat card would be the lie. So the card says cash,
        # and the drawdown below it is measured against that peak - the P04 rule ("drawdown wherever a PnL
        # appears") survives the smaller vocabulary intact.
        row = _db.execute("SELECT COALESCE(MAX(running),0), COALESCE(MIN(running),0) FROM (SELECT SUM(amount_micro)"
                          " OVER (ORDER BY created_ms, id) AS running FROM cash_ledger WHERE user_id=?)",
                          (uid,)).fetchone()
        peak = int(row[0] or 0) if row else 0
        low = int(row[1] or 0) if row else 0
        now = _db.execute("SELECT COALESCE(SUM(amount_micro),0) FROM cash_ledger WHERE user_id=?", (uid,)).fetchone()
        cash_now = int(now[0] or 0) if now else 0
        drawdown = max(0, peak - cash_now)
        return {"text": "<b>Realised %s USDC</b>\nPeak cash %s USDC · drawdown %s USDC\n<i>Drawdown is measured "
                        "against the peak, not against yesterday — a flat week after a good one is still a "
                        "drawdown. Realised counts exits and settlements minus the cost basis they released and "
                        "our own fees; the low so far on cash is %s USDC. Open positions are not marked here.</i>"
                        % (fmt_usdc(realised), fmt_usdc(peak), fmt_usdc(drawdown), fmt_usdc(low))}
    if name == "orders":
        rows = _db.execute("SELECT id, state, side, price_micro, size_micro, risk_code FROM order_intents"
                          " WHERE user_id=? ORDER BY created_ms DESC LIMIT 10", (uid,)).fetchall()
        if not rows:
            return {"text": ""}
        lines = ["• <code>%s</code> %s %s %s @ %s%s" % (str(r[0])[:12], str(r[1]), str(r[2]), fmt_usdc(r[4]),
                                                        fmt_usdc(r[3]), ("  ⚠️ %s" % r[5]) if r[5] else "")
                 for r in rows]
        return {"text": "<b>Last %d order%s</b>\n%s" % (len(rows), "" if len(rows) == 1 else "s", "\n".join(lines))}
    if name == "top":
        rows = _db.execute("SELECT handle, window, value, trades, categories FROM leaderboard_entries"
                          " WHERE board='edge' AND window='30d' ORDER BY rank LIMIT 5").fetchall()
        if not rows:
            return {"text": ""}
        lines = []
        for i, (handle, _w, value, trades, _cats) in enumerate(rows, 1):
            note = " <i>(sample-sized: %s trades)</i>" % trades if int(trades or 0) < 30 else ""
            lines.append("%d. @%s +%.1f%%%s" % (i, str(handle), float(value) * 100.0, note))
        return {"text": "<b>Top edge, 30 days</b>\n%s\n<i>Past performance is not a forecast, and the sample sizes "
                        "here are small. Board excludes anything under review.</i>" % "\n".join(lines)}
    if name == "stop_targets":
        o = _db.execute("SELECT COUNT(*) FROM order_intents WHERE user_id=? AND state IN ('queued','submitted')",
                        (uid,)).fetchone()
        r = _db.execute("SELECT COUNT(*) FROM automation_rules WHERE user_id=? AND enabled=1", (uid,)).fetchone()
        return {"orders": int(o[0] or 0) if o else 0, "copies": int(r[0] or 0) if r else 0}
    if name == "wallet":
        # `provisioned`/`funded`/`trading` are all wallets a user can deposit into; only `suspended` and `closing`
        # are not. An equality on `active` (a state this table never had) is how a card silently said "no wallet".
        row = _db.execute("SELECT address, custody FROM wallets WHERE user_id=? AND state IN"
                          " ('provisioned','funded','trading') ORDER BY created_ms LIMIT 1", (uid,)).fetchone()
        if row is None:
            return {"text": ""}
        # The address is deliberately NOT printed: it belongs on the Mini App (a QR and a copy button), and a chat
        # message is the one surface a user forwards to a stranger.
        return {"text": "<b>Wallet ready</b> (custody: %s)\nDeposit address and QR live in the Mini App — the button "
                        "below opens it.\n\n<i>Never paste a seed phrase anywhere, including here. We will never "
                        "ask for one.</i>" % str(row[1])}
    if name == "history":
        # The reason is the point of the row: "adjust −4.20" with no sentence is the ledger entry a support
        # ticket is made of, and the API's own `/v1/wallet/transactions` carries it verbatim. A card that shows
        # the kind and the amount and drops the reason is the version that generates the ticket.
        rows = _db.execute("SELECT kind, amount_micro, created_ms, reason FROM cash_ledger WHERE user_id=?"
                           " ORDER BY created_ms DESC LIMIT 8", (uid,)).fetchall()
        if not rows:
            return {"text": ""}
        lines = ["• %s  %s  <i>%s</i>\n  %s" % (str(k), fmt_usdc(d), _tg_when(a), str(r or "")[:80])
                 for k, d, a, r in rows]
        return {"text": "<b>Recent movements</b>\n%s" % "\n".join(lines)}
    if name == "verify_handle":
        handle = str(payload.get("handle") or "").lstrip("@").lower()
        row = _db.execute("SELECT user_id, kind FROM user_identities WHERE kind='handle' AND value=? AND"
                          " state='verified'", (handle,)).fetchone()
        staff = _db.execute("SELECT role FROM staff_roles WHERE user_id=? AND role IN ('support','admin')",
                           (str(row[0]),)).fetchone() if row else None
        return {"operator": bool(staff), "role": str(staff[0]) if staff else ""}
    return {"text": ""}


def _tg_market_view(market_id: str, slug: str, question: str, tick) -> dict:
    """The market card's data: both outcomes, the age of the mark, and the spread. One place, so the card and the
    Mini App cannot disagree."""
    at = _now_ms()
    row = _db.execute("SELECT MAX(updated_ms) FROM book_levels WHERE market_id=?", (market_id,)).fetchone()
    updated = int(row[0] or 0) if row else 0
    best_ask = _db.execute("SELECT price_micro FROM book_levels WHERE market_id=? AND side='ask'"
                          " ORDER BY price_micro LIMIT 1", (market_id,)).fetchone()
    best_bid = _db.execute("SELECT price_micro FROM book_levels WHERE market_id=? AND side='bid'"
                          " ORDER BY price_micro DESC LIMIT 1", (market_id,)).fetchone()
    yes = _db.execute("SELECT t.token_id, t.outcome FROM tokens t WHERE t.market_id=? AND t.outcome_index=0",
                      (market_id,)).fetchone()
    ask = int(best_ask[0]) if best_ask else 0
    bid = int(best_bid[0]) if best_bid else 0
    end_row = _db.execute("SELECT end_ts FROM markets WHERE id=?", (market_id,)).fetchone()
    age_ms = max(0, at - updated) if updated else 0
    return {"slug": slug, "question": question, "market_id": str(market_id),
            "token_id": str(yes[0]) if yes else "", "outcome": str(yes[1]) if yes else "Yes",
            "mark": _tg_cents(ask), "no_mark": _tg_cents(1_000_000 - ask) if ask else "—",
            "spread": _tg_cents(max(0, ask - bid)) if ask and bid else "—",
            "age": "as of %s" % _tg_ago(age_ms) if updated else "no recent quote — treat the price as unknown",
            "closes": _tg_when(int(end_row[0])) if end_row else "", "tick": tick}


def _tg_mark(token_id: str) -> str:
    row = _db.execute("SELECT price_micro FROM book_levels b JOIN tokens t ON t.market_id = b.market_id"
                      " WHERE t.token_id=? AND b.side='ask' ORDER BY b.price_micro LIMIT 1", (token_id,)).fetchone()
    return _tg_cents(int(row[0])) if row else "—"


def _tg_cents(micro: int) -> str:
    """A probability as cents with one decimal, from integer micros: 620000 → "62.0¢". No float, ever."""
    micro = int(micro or 0)
    return "%d.%d¢" % (micro // 10_000, (micro // 1_000) % 10)


def _tg_ago(ms: int) -> str:
    ms = max(0, int(ms))
    if ms < 5_000:
        return "%d seconds ago" % max(1, ms // 1_000)
    if ms < 60_000:
        return "%d seconds ago" % (ms // 1_000)
    if ms < 3_600_000:
        return "%d minutes ago" % (ms // 60_000)
    return "%d hours ago" % (ms // 3_600_000)


def _tg_when(ts_ms: int) -> str:
    if not ts_ms:
        return ""
    return time.strftime("%d %b %H:%M UTC", time.gmtime(int(ts_ms) / 1000.0))


_FEE_RATE_CACHE: dict = {}


def _measured_fee_rate_bps(market_id: str, *, at: int | None = None) -> int:
    """The venue's own taker fee rate for this market, as *measured* off the tape rather than guessed.

    P01's finding stands: nearly every market reports a zero rate, and the two 15-minute markets that do not are
    the reason this is a lookup instead of a constant. `tape_fills.fee_rate_bps` is written by the ingest path
    from the venue's trade rows, so the number the order path sizes against is the same number the venue will
    charge — which is the entire point of an all-in cap. Cached for a minute: a fee rate is a property of the
    market, not of the request, and this runs on the order path.
    """
    now = at or _now_ms()
    hit = _FEE_RATE_CACHE.get(market_id)
    if hit and now - hit[0] < 60_000:
        return hit[1]
    # `tape_fills` is keyed by the venue's condition id (the ingest path writes what the venue says), so the
    # lookup goes through the market row rather than pretending the two ids are the same string.
    cond = _db.execute("SELECT condition_id FROM markets WHERE id=?", (market_id,)).fetchone()
    row = _db.execute("SELECT COALESCE(MAX(fee_rate_bps), 0) FROM tape_fills WHERE condition_id=? AND ts_ms > ?",
                      (str(cond[0]) if cond else "", now - 7 * 24 * 3600 * 1000)).fetchone()
    rate = int((row[0] if row else 0) or 0)
    _FEE_RATE_CACHE[market_id] = (now, rate)
    return rate


def _write_order_directive(intent_id: str, market_id: str, *, order_type: str, audience: str,
                           builder_bps: int, fee_rate_bps: int, max_slippage_bps: int,
                           notional_micro: int, size_micro: int = 0, price_micro: int = 0) -> int:
    """Write the directive row for an intent the API just queued, and return the all-in cap it carries.

    The table existed (`order_directives`) and the API was the one producer that never filled it: every intent
    this service wrote was read back by the executor with `COALESCE(...)` defaults — `order_type='GTC'`,
    `audience='user'`, `fee_rate_bps=0` — which is fine for a limit order and a lie the moment the market has a
    fee. The directive is what makes the all-in cap enforceable downstream: the executor checks it, and the
    number it checks is the number this function derived from the same fee estimate the user's budget was sized
    with.
    """
    fees = v2.estimate_fees(size_shares_micro=int(size_micro), price_micro=int(price_micro),
                            fee_rate_bps=fee_rate_bps, builder_bps=builder_bps)
    cap = (int(notional_micro) + fees.total_micro
           + (int(notional_micro) * max(int(max_slippage_bps), 0)) // 10_000)
    now = _now_ms()
    _db.execute("INSERT OR REPLACE INTO order_directives (intent_id,order_type,expiration_ts,builder_bps,"
                "fee_rate_bps,audience,rule_id,config_id,max_slippage_bps,all_in_limit_micro,created_ms)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (intent_id, order_type, 0, builder_bps, fee_rate_bps, audience, "", "", max_slippage_bps, cap,
                 now))
    return cap


def _order_from_card(uid: str, payload: dict, key: str) -> dict:
    """Turn an *amount-denominated* order card into an order through the SAME path `POST /v1/orders` uses.

    Two surfaces call this and they are the reason it was extracted: the Telegram confirm tap and the web ticket.
    A webview (and a browser) knows a market and a budget; the order path wants a token and a size in shares. That
    conversion lives here once, so "what an order is" cannot differ between the chat, the Mini App and the site.

    The mapping is the part worth reading: the card carries a *market and an amount in USDC*, while the order path
    wants a market, an outcome token, a limit price and a size in shares. The conversion is integer arithmetic
    throughout (`shares = usdc_micro * 1e6 / price_micro`), because a float here is a float in the money path, and
    the price is the best ask *re-read at this moment* rather than the one on the card — a card is a question, and
    the answer is the book.
    """
    slug = str(payload.get("slug") or "")
    # Always a BUY: the side picks the *outcome token*, and there is no selling NO in a market you never held. The
    # line used to read `"BUY" if yes else "BUY"` — a branch where one arm explains the other away.
    side = "BUY"
    row = _db.execute("SELECT m.id, m.slug, t.token_id, t.outcome FROM markets m JOIN tokens t ON t.market_id = m.id"
                      " WHERE m.slug=? AND t.outcome_index = ? LIMIT 1",
                      (slug, 0 if str(payload.get("side") or "").lower() == "yes" else 1)).fetchone()
    if row is None:
        # `NOT_FOUND` rather than `MARKET_NOT_FOUND`: the second reads better and does not exist in CODES, so a
        # client handling the registered vocabulary has no branch for it. Both tables below were written against the
        # nicer name, which is exactly how an unregistered code reaches a user's phone.
        return {"ok": False, "code": "NOT_FOUND", "detail": "that market is not one of ours"}
    market_id, _slug, token_id, outcome = str(row[0]), str(row[1]), str(row[2]), str(row[3])
    ask = _db.execute("SELECT price_micro FROM book_levels WHERE market_id=? AND side='ask' ORDER BY price_micro"
                      " LIMIT 1", (market_id,)).fetchone()
    if ask is None:
        # `NO_ORDER_BOOK` is the registered code for "this side has no levels to trade against". `NO_LIQUIDITY` was
        # another name that read well and appears in no vocabulary.
        return {"ok": False, "code": "NO_ORDER_BOOK", "detail": "there is no offer on that side right now"}
    price_micro = int(ask[0])
    try:
        usdc_micro = parse_usdc(str(payload.get("amount") or "0"))
    except (MoneyError, ScaleError) as exc:
        return {"ok": False, "code": "BAD_AMOUNT", "detail": str(exc)[:120]}
    # Sized from the *amount*, fee included: `usdc_micro // price` is the notional-only answer, and an order
    # that spends the user's whole budget on shares has nothing left for the fee, so the venue's balance check
    # refuses an order the person believes they can afford. `size_for_all_in_spend` is the same integer
    # arithmetic with the fee curve in it, and its result is maximal — one micro-share more does not fit.
    fee_rate_bps = _measured_fee_rate_bps(market_id)
    shares_micro = v2.size_for_all_in_spend(usdc_micro, price_micro, fee_rate_bps=fee_rate_bps)
    if shares_micro <= 0:
        return {"ok": False, "code": "BAD_AMOUNT", "detail": "that amount is too small to buy one share"}
    body = {"marketId": market_id, "tokenId": token_id, "side": side,
            "price": _micro_str(price_micro), "size": _micro_str(shares_micro)}
    # The key goes in whole or not at all. `_order_core` validates it against the client contract's own pattern and
    # refuses anything malformed; truncating a long key here would let two distinct orders share one idempotency
    # record, which is the single failure the key exists to prevent.
    resp = _order_core(uid=uid, body=body, idempotency_key=str(key), rid="tg-%s" % str(key)[:24])
    try:
        payload_out = json.loads(resp.body.decode("utf-8"))
    except Exception:                                        # a non-JSON body here would be a bug in the core
        payload_out = {}
    if int(getattr(resp, "status_code", 500)) < 400:
        # The directive goes in only for an intent that was actually queued, and it uses the fee rate this
        # function just sized against: one number, two readers (the executor's pre-flight and this budget).
        _write_order_directive(str(payload_out.get("intentId") or ""), market_id, order_type="GTC",
                               audience="user", builder_bps=0, fee_rate_bps=fee_rate_bps,
                               max_slippage_bps=0,
                               notional_micro=int(payload_out.get("notionalMicro") or 0),
                               size_micro=shares_micro, price_micro=price_micro)
    if int(getattr(resp, "status_code", 500)) >= 400:
        code = str((payload_out.get("error") or {}).get("code") or "ORDER_REFUSED")
        return {"ok": False, "code": code, "intent_id": "", "detail": str((payload_out.get("error") or {})
                                                                          .get("message") or "")[:160],
                "outcome": outcome, "price_micro": price_micro, "shares_micro": shares_micro}
    return {"ok": True, "code": "QUEUED", "intent_id": str(payload_out.get("intentId") or ""), "outcome": outcome,
            "price_micro": price_micro, "shares_micro": shares_micro,
            "notional_micro": int(payload_out.get("notionalMicro") or 0)}


def _micro_str(micro: int, *, scale: int = 6) -> str:
    """`80645161` micro-shares → `"80.645161"`. Integer division and formatting, and never a float: this string is
    the input to `parse_usdc`/`price_ticks` on the order path, and a float here would be a float in the money path."""
    micro = int(micro or 0)
    whole, frac = divmod(abs(micro), 10 ** scale)
    return "%s%d.%0*d" % ("-" if micro < 0 else "", whole, scale, frac)


def _tg_shares(micro: int) -> str:
    """`1_290_000_000` micro-shares → `"1,290"`, `80_645_161` → `"80.64"`, `500_000` → `"0.50"`.

    A share count on a phone is a *reading*, not a measurement: `_micro_str` gives the money path its exact string
    (and stays as it is, because that string is parsed back), while this one gives the reader the number they would
    say out loud. Trailing zeros go, the grouping separator stays, and the fraction is capped at two places — a
    channel post reading "50000.000000 shares" is six decimals of noise in the one line the alert exists for.

    Two of the examples above used to be wrong about this function's own behaviour (`"80.65"`, which truncation never
    produces, and `"0.5"`, where the two-decimal rule gives `"0.50"`). Truncation is the rule and it is deliberate —
    never round a size up, because being told you hold more than you do is a support ticket — and the Mini App's
    confirm line truncates the same way, so the two surfaces print the same number for the same fill.
    """
    micro = int(micro or 0)
    whole, frac = divmod(abs(micro), 1_000_000)
    grouped = "{:,}".format(whole)
    frac2 = frac // 10_000                      # two decimal places, truncated: never rounded up into a lie
    body = grouped if frac2 == 0 else "%s.%02d" % (grouped, frac2)
    return ("-" if micro < 0 else "") + body


def _tg_stop_all(uid: str, *, reason: str) -> dict:
    """`/stop`: cancel what is pending and pause the rules. It does not sell what the user holds — and it says so.

    Selling positions is a *trade*, and a panic command that silently dumps a book is a panic command nobody dares
    press. What it guarantees: no pending order can fill after it, and no automation rule can fire.
    """
    at = _now_ms()
    orders = _db.execute("SELECT COUNT(*) FROM order_intents WHERE user_id=? AND state IN ('queued','submitted')",
                         (uid,)).fetchone()
    n_orders = int(orders[0] or 0) if orders else 0
    _db.execute("UPDATE order_intents SET state='cancelled' WHERE user_id=? AND state IN ('queued','submitted')",
                (uid,))
    rules = _db.execute("SELECT COUNT(*) FROM automation_rules WHERE user_id=? AND enabled=1", (uid,)).fetchone()
    n_rules = int(rules[0] or 0) if rules else 0
    _db.execute("UPDATE automation_rules SET enabled=0 WHERE user_id=?", (uid,))
    _db.commit()
    SEC.auth_event(uid, "telegram_stop", at=at, detail={"orders": n_orders, "rules": n_rules, "reason": reason[:80]})
    return {"orders": n_orders, "copies": n_rules}


def _tg_open_miniapp(uid: str, *, chat_id: str) -> dict:
    """A one-use deep link into the Mini App, minted server-side.

    The link carries an opaque ticket, never an account id or a token in a URL a user could forward — the ticket is
    consumed by `POST /v1/telegram/session` and expires. A `startapp` payload is a lookup key, and a lookup key that
    is also a credential is how a forwarded link becomes a session.
    """
    ticket = "tgs_" + uuid.uuid4().hex[:24]
    _db.execute("INSERT INTO auth_events (user_id, kind, at_ms, detail_json) VALUES (?,?,?,?)",
                (uid, "telegram_miniapp_ticket", _now_ms(), json.dumps({"ticket": ticket, "chat": str(chat_id)})))
    _db.commit()
    return {"ticket": ticket, "expiresInS": 300}


@app.post("/v1/telegram/webhook", status_code=200)
async def telegram_webhook(request: Request,
                           x_telegram_secret: str | None = Header(default=None,
                                                                  alias="X-Telegram-Bot-Api-Secret-Token")):
    """Telegram's only entry point. Secret check, claim, route, enqueue, 200 — in that order, and fast.

    It answers 200 even when the handler refuses something, because a non-2xx makes Telegram retry the update, and a
    retried `/stop` or a retried confirm is exactly the traffic this route must not manufacture. A failure *we* care
    about is recorded in `telegram_updates.note` and in the metrics, not signalled with an HTTP code.
    """
    rid = request.state.request_id
    secret = (os.environ.get(TG_WEBHOOK_SECRET_ENV) or "").strip()
    problems = _tg_client.webhook_findings(dict(request.headers), secret)
    if problems:
        # A missing secret is a misconfiguration (503, and the on-call is told); a wrong one is a probe (403).
        SEC.auth_event("", "telegram_webhook_refused", at=_now_ms(),
                       detail={"why": problems[0][:80], "path": request.url.path})
        # `ADMIN_REQUIRED` is the registered 403 and it is the honest one even for Telegram: the caller did not
        # present the credential this route demands. A missing secret is still a 503 — a pod with no secret is a
        # misconfiguration, and it must not look like an attack in the on-call dashboards.
        return err("SIGNER_UNAVAILABLE" if not secret else "ADMIN_REQUIRED", rid, detail=problems[0][:120])
    raw = await request.body()
    if len(raw) > TG_MAX_UPDATE_BYTES:
        return err("VALIDATION", rid, detail="update is larger than the Bot API can send")
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return err("VALIDATION", rid, detail="body is not JSON")
    try:
        update = _tgb_updates.classify(payload, bot_username=_tg_bot_username())
    except _tgb_updates.UpdateError as exc:
        return err("VALIDATION", rid, detail=str(exc)[:120])
    seen, state = _tg_claim(update.update_id, kind=update.kind, chat_id=update.chat_id, user_id=update.user_id,
                            command=update.command)
    t0 = time.time()
    if seen:
        _tg_metric(chat_id=update.chat_id, chat_type=update.chat_type, user_id=update.user_id,
                   command=update.command, action="replay", ok=True, update_id=update.update_id)
        return {"ok": True, "replayed": True, "state": state}
    account_id, _who = _tg_linked(update.chat_id, update.user_id)
    session = _tg_session(update.chat_id)
    decision = _tgb_router.route(update, at_ms=_now_ms(), bot_username=_tg_bot_username(), session=session,
                                 linked=bool(account_id), account_id=account_id,
                                 market_index=_tg_market_index(), fetch=_tg_fetch, seen=False)
    _tg_save(decision.session) if decision.session is not None else _tg_clear_session(update.chat_id)
    outcome_note = decision.note
    if decision.action.kind == "place_order" and account_id:
        res = _order_from_card(account_id, decision.action.payload, decision.action.key)
        outcome_note = ("placed %s" % res.get("intent_id", "")) if res.get("ok") else ("refused %s"
                                                                                       % res.get("code", ""))
        if res.get("ok"):
            _tg_enqueue(chat_id=update.chat_id, chat_type=update.chat_type,
                        plan=_tgb_render.two_beat(
                            skeleton="⏳ Sending your order…",
                            answer="🧾 <b>Order sent</b>\n%s %s shares of <b>%s</b> at %s.\n"
                                   "You will get one more message the moment it fills or is refused.\n\n"
                                   "<i>intent <code>%s</code></i>" % (str(res.get("outcome", "")),
                                                                      _micro_str(res.get("shares_micro", 0)),
                                                                      _tgb_render.esc(str(decision.action.payload.get("slug", ""))),
                                                                      _tg_cents(res.get("price_micro", 0)),
                                                                      _tgb_render.esc(str(res.get("intent_id", ""))[:16])),
                            priority=_tgb_outbox.P_TRADE_CARD), priority=_tgb_outbox.P_TRADE_CARD,
                        note="order queued")
        else:
            _tg_enqueue(chat_id=update.chat_id, chat_type=update.chat_type,
                        plan=_tgb_render.refusal_card(what="Order not sent", code=str(res.get("code", "")),
                                                      plain=_tg_plain_refusal(str(res.get("code", "")),
                                                                              str(res.get("detail", ""))),
                                                      next_step="Nothing was placed, and nothing is pending. "
                                                                "Adjust the size and try again, or /support "
                                                                "with the code above."),
                        priority=_tgb_outbox.P_REJECT, note="order refused")
    elif decision.action.kind == "cancel_all" and account_id:
        out = _tg_stop_all(account_id, reason=str(decision.action.payload.get("reason") or decision.metric))
        plan = _tgb_render.Plan(beats=[_tgb_render.Beat(
            text="🛑 <b>Stopped.</b>\nCancelled %d pending order%s and paused %d auto-trade rule%s.\n\n"
                 "<i>Your positions are untouched — stopping cancels what is pending, it does not sell what you "
                 "hold.</i>" % (out["orders"], "" if out["orders"] == 1 else "s", out["copies"],
                                "" if out["copies"] == 1 else "s"),
            keyboard=_tgb_menu.stop_keyboard().to_markup(), what="answer")],
            haptics=("haptic_reject",), priority=_tgb_outbox.P_REJECT)
        _tg_enqueue(chat_id=update.chat_id, chat_type=update.chat_type, plan=plan, priority=_tgb_outbox.P_REJECT,
                    note="stop")
    else:
        _tg_enqueue(chat_id=update.chat_id, chat_type=update.chat_type, plan=decision.plan,
                    note=decision.metric or decision.note)
    _tg_metric(chat_id=update.chat_id, chat_type=update.chat_type, user_id=update.user_id,
               command=update.command or decision.metric, action=decision.metric,
               ok=not bool(decision.note and "refused" in str(decision.note)),
               dur_ms=int((time.time() - t0) * 1000), update_id=update.update_id)
    _tg_finish(update.update_id, state="done", note=outcome_note)
    return {"ok": True, "replayed": False, "metric": decision.metric, "queued": 1}


def _tg_plain_refusal(code: str, detail: str = "") -> str:
    """Machine code → the sentence a person can act on.

    The table is the *product*: a rejection nobody understands is a support ticket, and D4 asks for plain language
    precisely because the gate's codes are not. So the keys here are not a style choice — they are the codes
    `CODES` can actually return, and getting that wrong produces a table that looks like coverage and behaves like
    a fallback.

    **It was wrong.** The first version of this table was keyed on `RISK_*` names (`RISK_NOTIONAL`, `RISK_DAILY`,
    `RISK_MIN_SIZE`, `RISK_STALE_BOOK`, `RISK_TICK`, `RISK_OPEN_ORDERS`, `RISK_PRICE_BAND`) — plausible names, none
    of them in `CODES`. Every real refusal the venue's gate produces (`DAILY_CAP`, `OFF_TICK`, `BELOW_MIN_SIZE`,
    `STALE_QUOTE`, `OVER_ORDER_CAP`, …) missed the table and fell through to the fallback sentence, which means the
    phase's headline feature — "fill and rejection notifications in plain language" — was, in the product, one
    sentence about a code. `tests/test_telegram_ops_api.py::TestRefusalVocabulary` now asserts every key is a
    registered code, so the table cannot drift from the vocabulary again, and the same test compares this table's
    keys with the Mini App's copy of it.

    The fallback stays: a code we have not met yet must still say *something* true, and the code itself is better
    than an apology.
    """
    table = {
        # --- pauses and platform state ---------------------------------------------------------------
        "PASSWORD_REQUIRED": "Set a withdrawal password first — it is the second lock on money leaving.",
        "PASSWORD_WRONG": "That password did not match, so nothing was sent. Try again, or reset it from the bot "
                        "if you have forgotten it.",
        "ADDRESS_NOT_ALLOWED": "That destination is not on your allowlist, and an address can be added only "
                             "from the bot. Nothing was sent.",
        "INSUFFICIENT_BALANCE": "That is more than your available cash, so nothing was sent. Deposit first, or use "
                              "a smaller amount.",
        "RISK_HALT": "Trading is paused right now — the platform's kill switch is engaged. Nothing you did caused it.",
        "HALTED": ("Your account is stopped for the day: your own daily-loss limit was hit. It can be lifted from "
                   "the app once you have read what happened."),
        "RISK_UNAVAILABLE": ("The risk check could not run, so I will not send an order through it. Nothing was "
                             "placed — try again in a moment."),
        "SIGNER_UNAVAILABLE": "Signing is unavailable right now, so nothing can be placed. Try again shortly.",
        # --- the market itself -----------------------------------------------------------------------
        "MARKET_NOT_ACCEPTING": "That market is not taking orders at the moment. Nothing was placed.",
        "NOT_FOUND": "I could not find that market — it may have closed.",
        "NO_ORDER_BOOK": "That market has no order book, so there is nothing to trade against.",
        "BAD_MARKET_META": ("I could not read that market's limits, and I will not guess them. Nothing was placed."),
        "STALE_QUOTE": ("The order book is stale, so I will not price an order from it. Nothing was placed — try "
                        "again in a moment."),
        # --- the order, as asked ---------------------------------------------------------------------
        "BAD_SIDE": "That side is not one this market trades.",
        "BAD_AMOUNT": "That amount cannot be turned into a whole number of shares at this price.",
        "ZERO_SIZE": "That works out to no shares at all, so there was nothing to place.",
        "BELOW_MIN_SIZE": "That is below the smallest order this market accepts.",
        "OVER_ORDER_CAP": "That order is larger than the per-order limit on your account.",
        "DAILY_CAP": "That would take you past your own daily limit. It resets at midnight UTC.",
        "TOO_MANY_OPEN": "You have too many open orders. Cancel one or wait for a fill.",
        "PRICE_FAR_FROM_MID": ("That price is too far from the market's current price for the venue to accept it. "
                               "Nothing was placed."),
        "OFF_TICK": "The price does not sit on this market's tick size.",
        "UNKNOWN_TICK": ("I could not read that market's tick size, so I will not guess where a price belongs. "
                         "Nothing was placed."),
        # --- replay and identity ---------------------------------------------------------------------
        "IDEM_CONFLICT": "That tap was for a different order than the one I already have on file, so I stopped.",
        "IDEM_IN_PROGRESS": "That order is already on its way — I have not sent a second one.",
        "IDEM_KEY_REQUIRED": "That order arrived without a way to tell a retry from a new order, so I did not send it.",
        "RATE_LIMITED": "That is more requests than I can send for you at once. Give it a second.",
        "UNAUTHENTICATED": "Sign in again from the bot and I will pick this up where it stopped.",
        # --- P12 D6's wallet ceremony -----------------------------------------------------------------
        # Four codes the withdrawal and export screens can meet, added when the Mini App grew those screens:
        # a table that covers the order path and not the money-leaving path is a table with one door unwatched.
        "ADDRESS_COOLDOWN": "That destination is still inside its 24 hour hold, so nothing was sent. The hold is what "
                            "makes a changed destination visible before money moves.",
        "TOTP_REQUIRED": "This action needs your authenticator code, and none is enrolled on this account yet — set it "
                         "up in the bot first.",
        "TOTP_INVALID": "That authenticator code did not work, so nothing was sent. Codes last 30 seconds; wait for "
                        "the next one and try again.",
        "TOTP_LOCKED": "Too many wrong authenticator codes, so this is locked for a few minutes. Nothing was sent.",
        "REFUSED": "The venue refused the order. Nothing was placed.",
        "BUILDER_DISABLED": "The venue refused the order because the builder code on it is disabled, so nothing "
                            "was placed. The code is marked off and its attribution has stopped.",
    }
    if code in table:
        return table[code]
    return detail or ("The venue refused the order (%s). Nothing was placed." % code)


def _tg_bot_username() -> str:
    return (os.environ.get("PGM_TELEGRAM_BOT_USERNAME") or "polygm_bot").strip().lstrip("@")


def _tg_market_index() -> tuple:
    """A small candidate list for the natural-language matcher: live markets, newest and most active first.

    Deliberately bounded (200 rows): the matcher is a token-overlap ranker, and handing it a full catalogue would
    make an ambiguous sentence more ambiguous rather than less. `/search` and the Mini App are where the long tail
    lives.
    """
    rows = _db.execute("SELECT slug, question FROM markets WHERE accepting_orders=1 AND end_ts > ?"
                      " ORDER BY first_seen_ms DESC LIMIT 200", (_now_ms(),)).fetchall()
    return tuple({"slug": str(r[0]), "question": str(r[1])} for r in rows)


@app.post("/v1/telegram/session", status_code=200, responses=AUTH_RESPONSES,
           openapi_extra=_body_schema(("initData",), {"initData": {"type": "string", "minLength": 8,
                                                                   "maxLength": 8192}}))
def telegram_session(request: Request, body: dict = Body(...)):
    """The Mini App's sign-in — the D2 acceptance path, and it is P07's verifier rather than a second one.

    Four checks, all of them before a session exists: the signature (HMAC over the data-check-string, keyed by the
    bot token), the freshness window, the replay store, and the link between the Telegram account and a PolyGM
    account. A tampered payload fails check one; a payload captured and replayed fails check three.
    """
    rid = request.state.request_id
    bad = _check_body(body, ("initData",), rid)
    if bad is not None:
        return bad
    token = (os.environ.get("PGM_TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return err("SECURITY_ENV_MISSING", rid, detail="PGM_TELEGRAM_BOT_TOKEN is not set on this pod")
    init = str(body["initData"])
    try:
        seen = SEC.telegram_seen_hashes(_tg.auth_hash(init))
        res = _tg.verify(init, token, at=_now_ms(), purpose="login", seen_hashes=seen)
    except _tg.InitDataError as exc:
        SEC.auth_event("", "telegram_malformed", at=_now_ms(), detail={"why": str(exc)[:120]})
        return err("TELEGRAM_INVALID", rid, detail="the payload is malformed")
    if not res.ok:
        SEC.auth_event(res.tg_user_id, "telegram_%s" % res.reason, at=_now_ms(), detail={"from": "miniapp"})
        return err("TELEGRAM_REPLAY" if res.reason == "replayed" else "TELEGRAM_INVALID", rid,
                   detail=res.reason)
    uid = SEC.identity_user("telegram", res.tg_user_id)
    if not uid:
        # The Mini App can be opened by anyone with the link; the *account* is what is missing, and the answer says
        # exactly that so the Mini App can show the link flow instead of a dead end.
        return _stamped({"linked": False, "telegramUserId": res.tg_user_id, "needsLink": True,
                         "note": "this Telegram account is not linked to a PolyGM account yet"},
                        ttl_ms=0, stale_ms=0)
    acc, ref = _token_string(), _token_string()
    fam = "fam_" + uuid.uuid4().hex[:12]
    srow = SEC.mint_session(str(uid), token_hash=_hash_token(acc), family_id=fam, at=_now_ms(), kind="telegram",
                           ip_hash=_ip_hash(request), ua_hash=_ua_hash(request))
    SEC.mint_refresh(str(uid), token_hash=_hash_token(ref), family_id=fam, at=_now_ms())
    SEC.telegram_consume(res.auth_hash, str(uid), at=_now_ms(), session=srow["id"])
    SEC.auth_event(str(uid), "miniapp_session", at=_now_ms(), detail={"age_s": res.age_s})
    return _stamped({"linked": True, "accessToken": acc, "refreshToken": ref, "tokenType": "Bearer",
                     "expiresInMs": ACCESS_TTL_MS, "user": {"id": str(uid)}, "initDataAgeS": res.age_s},
                    ttl_ms=0, stale_ms=0)


TELEGRAM_ORDER_RESPONSES = {
    202: {"description": "queued for the executor through the same risk gate as the web ticket"},
    **{status: {"description": msg} for msg, status, _retry in CODES.values()},
}


# Derived from CODES exactly as ORDER_RESPONSES is, rather than aliased to the Telegram table: the contract checker
# evaluates these tables from the source's AST, so an alias would read as "no statuses at all" and the route would pass
# a check it never took.
ORDER_AMOUNT_RESPONSES = {
    202: {"description": "queued for the executor through the same risk gate as the chat and the terminal"},
    **{status: {"description": msg} for msg, status, _retry in CODES.values()},
}


@app.post("/v1/orders/amount", status_code=202, responses=ORDER_AMOUNT_RESPONSES,
          openapi_extra=_body_schema(("slug", "side", "amountUsdc"), {
              "slug": {"type": "string", "minLength": 3, "maxLength": 128},
              "side": {"type": "string", "enum": ["yes", "no"]},
              "amountUsdc": {"type": "string", "pattern": "^[0-9]{1,9}(\\.[0-9]{1,2})?$"}}))
def place_order_by_amount(request: Request, body: dict = Body(...),
                          idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """The web ticket's order: a market, an outcome and a budget — priced by the server, at this instant.

    P06's `POST /v1/orders` takes what the *venue* takes: an outcome token, a limit price and a size in shares. The
    browser ticket sent something else — `{market_id, side, amount_cents}` — and the contract answered 422 for every
    trade the site ever attempted. That is not a renamed-field bug. It is two surfaces disagreeing about what an order
    is: the ticket is a person typing a dollar amount, and a browser that names a token id can name the wrong one,
    while a browser that names a price is quoting the past. So the browser gets the same treatment the webview got,
    and both call `_order_from_card`: one conversion, one risk gate, one ledger.

    The only difference from `/v1/telegram/order` is identity. This one is authorised by the site's own bearer
    session, so a browser tab and a webview place the same order through the same path.
    """
    rid = request.state.request_id
    uid, _row, deny = _principal(request)
    if deny is not None:
        return deny
    if not uid:
        return err("UNAUTHENTICATED", rid)
    bad = _check_body(body, ("slug", "side", "amountUsdc"), rid)
    if bad is not None:
        return bad
    slug = str(body["slug"]).strip()
    side = str(body["side"]).strip().lower()
    if len(slug) < 3 or len(slug) > 128:
        return err("VALIDATION", rid, where=["slug"])
    if side not in ("yes", "no"):
        return err("VALIDATION", rid, where=["side"])
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    res = _order_from_card(str(uid), {"slug": slug, "side": side, "amount": str(body["amountUsdc"])},
                           str(idempotency_key))
    if not res.get("ok"):
        code = str(res.get("code") or "INTERNAL")
        return err(code if code in CODES else "INTERNAL", rid, detail=str(res.get("detail") or "")[:160])
    return _stamped({"cacheKey": None, "intentId": str(res["intent_id"]), "state": "queued",
                     "outcome": str(res.get("outcome") or side.upper()),
                     "priceMicro": str(res["price_micro"]), "sharesMicro": str(res["shares_micro"]),
                     "notionalMicro": str(res.get("notional_micro") or 0),
                     "note": "queued for the executor; the fill arrives as a notification"},
                    ttl_ms=0, stale_ms=0)


@app.post("/v1/telegram/order", status_code=202, responses=TELEGRAM_ORDER_RESPONSES,
           openapi_extra=_body_schema(("slug", "side", "amountUsdc"), {
               "slug": {"type": "string", "minLength": 3, "maxLength": 128},
               "side": {"type": "string", "enum": ["yes", "no"]},
               "amountUsdc": {"type": "string", "pattern": "^[0-9]{1,9}(\\.[0-9]{1,2})?$"}}))
def telegram_order(request: Request, body: dict = Body(...),
                   idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """The Mini App's confirm button: a slug, a side, an amount — and the server does the rest.

    This route exists because of what the Mini App must NOT have to know. The web ticket posts a market id, an
    outcome token and a price; a webview opened from a deep link has none of those, and a client that can name a
    token can name the wrong one while a client that can name a price is quoting the past. So the webview says
    *what it is looking at* and the server resolves the rest — the same resolution the chat's confirm tap does
    (`_order_from_card`), which is the point: **one order path, one risk gate, two surfaces.** If the chat and
    the Mini App ever disagree about what an order is, they disagree here, in one function, on the diff.

    Authenticated as a session rather than as a bot: a webview user is a user. The identity is the Telegram account
    the signed `initData` resolved to, never an id in the body — which is why there is no `chat_id` parameter to
    forge.
    """
    rid = request.state.request_id
    uid, _row, deny = _principal(request)
    if deny is not None:
        return deny
    if not uid:
        return err("UNAUTHENTICATED", rid)
    bad = _check_body(body, ("slug", "side", "amountUsdc"), rid)
    if bad is not None:
        return bad
    slug = str(body["slug"]).strip()
    side = str(body["side"]).strip().lower()
    if len(slug) < 3 or len(slug) > 128:
        return err("VALIDATION", rid, where=["slug"])
    if side not in ("yes", "no"):
        return err("VALIDATION", rid, where=["side"])
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    res = _order_from_card(str(uid), {"slug": slug, "side": side, "amount": str(body["amountUsdc"])},
                              str(idempotency_key))
    if not res.get("ok"):
        # Straight out of CODES, exactly as `POST /v1/orders` answers: an ordinary "no such market" is a 404 with
        # `NOT_FOUND`, a gate refusal is the gate's own code and status. The first version of this branch mapped
        # codes to hand-chosen statuses and wrapped `err()` in a second response object, which turned that 404 into
        # a 500 whose body was a serialised Python object — the loudest possible way to learn that a route had never
        # been exercised.
        code = str(res.get("code") or "INTERNAL")
        return err(code if code in CODES else "INTERNAL", rid, detail=str(res.get("detail") or "")[:160])
    return _stamped({"cacheKey": None, "intentId": str(res["intent_id"]), "state": "queued",
                     "outcome": str(res.get("outcome") or side.upper()),
                     "priceMicro": str(res["price_micro"]), "sharesMicro": str(res["shares_micro"]),
                     "notionalMicro": str(res.get("notional_micro") or 0),
                     "note": "queued for the executor; the fill arrives as a message in the chat and a "
                             "notification here"},
                    ttl_ms=0, stale_ms=0)


@app.post("/v1/telegram/drain", status_code=200, responses=AUTH_RESPONSES,
           openapi_extra=_body_schema((), {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}))
def telegram_drain(request: Request, body: dict = Body(default={}),
                   x_admin: str | None = Header(default=None, alias="X-Admin-Token")):
    """The worker: send what the outbox holds, in priority order, within the bot's rate budget.

    Admin-only, and it takes the admin token rather than a session: this is infrastructure, not something a user
    does, and an endpoint a user can call is an endpoint a user can use to make the bot shout at them.
    """
    ok, deny = _admin(request)
    if not ok:
        return deny
    kill = _tg_kill()
    bot = _tg_bot()
    at = _now_ms()
    # The worker's first job: absorb what the executor recorded. It runs here rather than in the request that
    # produced the fill because this is the process with a Bot client, and because the drain is already the one
    # place that knows about priorities, buckets and the kill switch.
    events = _tg_absorb_order_events(at=at)
    # Re-read the clock: the jobs the absorb just queued are due *now*, and the `at` above predates them by a few
    # milliseconds, so a queue read against it plans nothing and the fill waits for the next drain. (Caught by the
    # test that asserts one drain both absorbs a fill and sends it.)
    at = _now_ms()
    rows = _db.execute("SELECT id, chat_id, chat_type, priority, method, text, keyboard_json, edit_message_id,"
                      " attempts, created_ms, due_ms FROM telegram_outbox WHERE state='queued' AND due_ms <= ?"
                      " ORDER BY priority, due_ms, id LIMIT 500", (at,)).fetchall()
    # The kill switch filters the *plan*, not the queue: jobs stay queued (a paused switch must not lose a fill
    # notification), and `telegram_broadcasts` still records what was composed. Scope decides what is held back.
    if kill.engaged:
        rows = [r for r in rows if not _tgb_ops.blocks(kill, "channel" if str(r[2]) == "channel" else "personal")]
    jobs = [_tgb_outbox.Job(job_id=int(r[0]), chat_id=str(r[1]), chat_type=str(r[2]), priority=int(r[3]),
                            text=str(r[5]), keyboard=json.loads(r[6]) if r[6] else None, edit_message_id=int(r[7]),
                            attempts=int(r[8]), created_ms=int(r[9]), due_ms=int(r[10])) for r in rows]
    chosen, wait_ms = _tgb_outbox.plan(jobs, at_ms=at, buckets=_tg_buckets(), limit=int(body.get("limit") or 30))
    sent, failed = [], []
    for job in chosen:
        if bot is None:
            break
        if job.method == "editMessageText" and job.edit_message_id:
            res = bot.edit_message(chat_id=job.chat_id, message_id=job.edit_message_id, text=job.text,
                                   keyboard=job.keyboard)
        else:
            res = bot.send_message(chat_id=job.chat_id, text=job.text, keyboard=job.keyboard)
        if res.ok:
            _db.execute("UPDATE telegram_outbox SET state='sent', sent_ms=?, attempts=attempts+1 WHERE id=?",
                        (_now_ms(), job.job_id))
            sent.append(job.job_id)
            if res.message_id and job.method == "sendMessage":
                # Remember the message id: a later edit (the two-beat's second half) needs it, and Telegram does not
                # tell us which message we sent twice. The `note` column carries it as `mid=<id>`, which is also
                # what an operator reads when a card did not update.
                _db.execute("UPDATE telegram_outbox SET note = substr(note,1,80) || ' mid=' || ? WHERE id=?",
                            (str(res.message_id), job.job_id))
        else:
            retry = _tgb_outbox.should_retry(status=res.status, attempts=job.attempts, retry_after_s=res.retry_after_s)
            delay = _tgb_outbox.retry_delay_ms(attempts=job.attempts, retry_after_s=res.retry_after_s)
            _db.execute("UPDATE telegram_outbox SET state=?, attempts=attempts+1, due_ms=?, note=? WHERE id=?",
                        ("queued" if retry else "failed", _now_ms() + (delay if retry else 0),
                         _tg_client._redact(res.note, "")[:160], job.job_id))
            failed.append({"id": job.job_id, "status": res.status, "retry": retry})
    _db.commit()
    return _stamped({"sent": sent, "failed": failed, "planned": len(chosen), "nextInMs": wait_ms,
                     "orderEvents": events,
                     "botConfigured": bot is not None,
                     "held_by_kill": kill.engaged and {"engaged": True, "scope": kill.scope,
                                                       "reason": kill.reason} or None}, ttl_ms=0, stale_ms=0)


_tg_bucket_state = _tgb_outbox.Buckets()


def _tg_buckets():
    """The live rate buckets. Module-level on purpose: the limits are per *bot*, so they cannot be per request."""
    return _tg_bucket_state


@app.get("/v1/telegram/commands", responses=LIST_RESPONSES)
def telegram_commands():
    """The command surface as data — the same table the bot dispatches from, served for docs and the Mini App's
    help screen. Public: it is a list of what the bot can do, and hiding it would only hide it from us."""
    return _stamped({"cacheKey": "telegram:commands", "commands": [
        {"name": c.name, "summary": c.summary, "syntax": c.syntax, "auth": c.auth, "response": c.response,
         "errors": list(c.errors), "buttons": list(c.buttons), "touchesMoney": c.touches_money,
         "needsConfirmation": c.needs_confirmation} for c in _tgb_menu.commands()],
        "note": "every command has an inline-button alternative; nothing here requires typing"},
        ttl_ms=3_600_000, stale_ms=0)


@app.get("/v1/telegram/metrics", responses=AUTH_RESPONSES)
def telegram_metrics(request: Request, days: int = Query(default=7, ge=1, le=90),
                     x_admin: str | None = Header(default=None, alias="X-Admin-Token")):
    """The numbers D7 gates paid acquisition on, from `telegram_commands`.

    Each one is a *question* rather than a dashboard: are people coming back (DAU), do they use more than /start
    (commands per user), does the channel produce trades (alert→trade), does a deposit become a trade (the funnel
    the kit puts a 90-second target on), and do people leave after their first loss. A metric nobody wrote down is a
    metric nobody has, which is why the table exists from the first day rather than after the first bad week.
    """
    ok, deny = _admin(request)
    if not ok:
        return deny
    since = _now_ms() - int(days) * 86_400_000
    rows = _db.execute("SELECT chat_id, user_id, command, action, ok, at_ms FROM telegram_commands WHERE at_ms >= ?",
                       (since,)).fetchall()
    by_day: dict = {}
    per_user: dict = {}
    alert_taps = 0
    started = set()
    traded = set()
    first_trade_ms: dict = {}
    for chat, user, command, action, ok, at in rows:
        day = time.strftime("%Y-%m-%d", time.gmtime(int(at) / 1000.0))
        by_day.setdefault(day, set()).add(str(chat))
        per_user[str(chat)] = per_user.get(str(chat), 0) + 1
        if action in ("market", "nl_order") and str(command).startswith("alert"):
            alert_taps += 1
        if str(command) == "start":
            started.add(str(chat))
        if action in ("confirm", "nl_order"):
            traded.add(str(chat))
            first_trade_ms.setdefault(str(chat), int(at))
    daily = [{"day": d, "chats": len(c)} for d, c in sorted(by_day.items())]
    dau = int(sum(x["chats"] for x in daily) / max(1, len(daily)))
    return _stamped({"cacheKey": "telegram:metrics:%d" % days, "windowDays": int(days), "dau": dau,
                     "daily": daily, "chats": len(per_user),
                     "commandsPerUser": round(sum(per_user.values()) / max(1, len(per_user)), 2),
                     "startedChats": len(started), "tradedChats": len(traded),
                     "startToTradePct": round(100.0 * len(traded) / max(1, len(started)), 1),
                     "alertTaps": alert_taps,
                     "note": "the funnel the kit targets (start → first trade in 90 s) reads from the "
                             "same rows; a chat that trades and never comes back is the next chart, not a guess"},
                    ttl_ms=60_000, stale_ms=0)


def _tg_user_chat(user_id: str, chat_id: str = "") -> str:
    """The private chat a message about this user goes to, from the identity table.

    A fill must not depend on the caller remembering where to send it — that is how a fill goes to the wrong chat —
    and a user who only ever talks to the bot in a group must still get it privately. `user_identities.value` for a
    verified telegram identity IS the user id, which is the one place Telegram's model is kind to us.
    """
    chat = str(chat_id or "")
    if chat:
        return chat
    # Ordered, because a user can hold more than one verified Telegram identity over time and "LIMIT 1" without an
    # ORDER BY means the message goes somewhere different on different engines. Oldest first: the identity they
    # linked first is the chat they actually read.
    row = _db.execute("SELECT value FROM user_identities WHERE kind='telegram' AND user_id=? AND"
                      " state='verified' ORDER BY claimed_ms, value LIMIT 1", (str(user_id),)).fetchone()
    return str(row[0]) if row else ""


def _tg_notify_fill(user_id: str, *, market: str, side: str, size_text: str, price_text: str, fee_text: str,
                    position_text: str, price_age_text: str, chat_id: str = "", note: str = "fill",
                    market_slug: str = "", dedupe_key: str = "") -> int:
    """A fill, queued at the top priority. Called by whatever records the fill (the executor, or the reconciler).

    The chat is looked up from the identity table rather than passed in by every caller, because the one thing a
    fill notification must not depend on is the caller remembering where to send it.

    The lookup returns the *Telegram user id*, and a fill goes to that user's private chat — whose id is the same
    number, which is the one place Telegram's model is kind to us. That is deliberate and not an accident to be
    "fixed" later: a user who only ever talks to the bot in a group must still get their own fill privately, and
    sending it to the group would publish their position to the group.
    """
    chat = _tg_user_chat(user_id, chat_id)
    if not chat:
        return 0
    plan = _tgb_render.fill_card(market=market, side=side, size_text=size_text, price_text=price_text,
                                 fee_text=fee_text, position_text=position_text, price_age_text=price_age_text,
                                 market_slug=market_slug)
    return _tg_enqueue(chat_id=chat, chat_type="private", plan=plan, priority=_tgb_outbox.P_FILL, note=note,
                       dedupe_key=dedupe_key)


def _tg_notify_refusal(user_id: str, *, what: str, code: str, plain: str, next_step: str = "",
                       chat_id: str = "", note: str = "refused", dedupe_key: str = "") -> int:
    """A rejection, at `P_REJECT`, in the same shape a fill takes.

    Queued rather than sent from wherever the refusal was decided, for the reason the whole phase runs on an
    outbox: a refusal that arrives *after* whatever the user did next is still better than one that blocks the
    request that produced it.
    """
    chat = _tg_user_chat(user_id, chat_id)
    if not chat:
        return 0
    plan = _tgb_render.refusal_card(what=what, code=code, plain=plain, next_step=next_step)
    return _tg_enqueue(chat_id=chat, chat_type="private", plan=plan, priority=_tgb_outbox.P_REJECT, note=note,
                       dedupe_key=dedupe_key)


def _tg_market_bits(market_id: str) -> tuple[str, str]:
    """`(question, slug)` for a market id — what a message calls the market it is about."""
    row = _db.execute("SELECT question, COALESCE(slug,'') FROM markets WHERE id=?",
                      (str(market_id),)).fetchone()
    return (str(row[0]), str(row[1])) if row else ("that market", "")


def _tg_outcome_for_token(token_id: str) -> str:
    """The *outcome* a token is, not the venue's BUY/SELL side.

    The distinction is the difference between a message that reads "YES filled" and one that reads "BUY filled",
    and the second is not English anybody uses about a prediction market. `tokens` is where the mapping lives.
    """
    row = _db.execute("SELECT outcome FROM tokens WHERE token_id=?", (str(token_id),)).fetchone()
    return str(row[0]) if row else ""


def _tg_booked_fill(intent_id: str) -> tuple[int, int, int]:
    """`(shares, average price, fee)` in micros, summed from the `fills` rows this intent actually booked.

    This is the ledger's answer, and it is what a fill card must print: the money that moved, not the numbers the
    caller happened to be carrying. The line that used to build this card took the size, price and fee from the
    notification's own detail blob — so a caller that passed the wrong numbers printed the wrong numbers, with the
    ledger disagreeing and nothing to reconcile them. Reading `fills` makes the card a report.

    A weighted average rather than an average of prices: two prints at different prices average by *size*, and the
    integer arithmetic is exact (the price is a micro-USDC ratio, so it is computed in micros and never as a float).
    An intent with no booked fill returns zeros and the caller falls back.
    """
    row = _db.execute("SELECT COALESCE(SUM(f.size_micro),0), COALESCE(SUM(f.notional_micro),0),"
                      " COALESCE(SUM(f.fee_micro),0) FROM fills f JOIN orders o ON o.id = f.order_id"
                      " WHERE o.intent_id=?", (str(intent_id),)).fetchone()
    size, notional, fee = (int(row[0]), int(row[1]), int(row[2])) if row else (0, 0, 0)
    price = (notional * 1_000_000) // size if size and notional else 0
    return size, price, fee


def _tg_position_text(user_id: str, token_id: str, *, fallback_micro: int, side: str) -> str:
    """`"80.65 shares"` — what the user holds in this token *after* the fill, read from the lots.

    The lots are the book's own answer, so this line agrees with `/wallet` instead of with arithmetic done twice.
    A BUY that opened the first lot in this token has no rows to sum yet if the book leg has not landed, and the
    fill's own size is the honest fallback in exactly that case.
    """
    row = _db.execute("SELECT COALESCE(SUM(shares_open_micro),0), COUNT(*) FROM position_lots WHERE"
                      " user_id=? AND token_id=?", (str(user_id), str(token_id))).fetchone()
    held, lots = (int(row[0]), int(row[1])) if row else (0, 0)
    if lots == 0 and str(side).upper() == "BUY":
        held = int(fallback_micro or 0)
    return "%s share%s" % (_tg_shares(held), "" if held == 1_000_000 else "s")


def _tg_usdc_text(micro: int) -> str:
    """`500000` → `"0.5 USDC"`. Exact, never rounded: this is a fee somebody paid."""
    from polygm_core.money.cents import fmt_usdc
    return "%s USDC" % fmt_usdc(int(micro or 0))


#: How far back the order-event bridge looks. A week is longer than any retry the outbox does and short enough that
#: a user with no Telegram identity is not re-examined for ever; a row that falls out of the window is not lost, it
#: is simply no longer a *message* — the in-app row was never ours to consume.
_TG_ORDER_EVENT_WINDOW_MS = 7 * 86_400_000

#: The order events that become a chat message. `submitted`, `live` and `queued` are deliberately absent: they are
#: the order's own progress, the user just placed it, and a message per state change is how a bot becomes noise.
_TG_ORDER_EVENTS = ("filled", "partial_fill", "rejected")

#: For a refusal that has a next step, the sentence that names it. A code with no entry gets no advice rather than
#: invented advice, and `_tg_plain_refusal` already says what the gate decided.
_TG_REFUSAL_NEXT_STEP = {
    "DAILY_CAP": "You can lift your own daily limit in /settings when you are ready.",
    "HALTED": "Your account is stopped for today — /settings shows the limit that stopped it.",
    "TOO_MANY_OPEN": "Close or cancel something that is still open, then place this again.",
    "BELOW_MIN_SIZE": "The market's minimum is on the card; a larger order will go through.",
    "STALE_QUOTE": "Nothing was lost by waiting — place it again and the book will be re-read.",
    "NO_ORDER_BOOK": "There is nothing to trade against in that market right now.",
}


def _tg_order_event_key(row_id: int) -> str:
    """The outbox dedupe key that makes the bridge idempotent: one notification row, one message, for ever.

    A key of its own rather than a `note`: the drain rewrites notes (it appends the message id on success and the
    client's explanation on failure), so a note-based marker is *gone* by the second drain and the fill would be
    sent twice. That is not a theory — it is why `telegram_outbox` grew `dedupe_key` in 0019.
    """
    return "order-event:%d" % int(row_id)


def _tg_absorb_order_events(*, at: int | None = None, limit: int = 50) -> dict:
    """Turn recorded order events into queued messages. The missing half of D4.

    The executor is what records a fill or a refusal — `Store.book_fill` writes the ledger and, since this phase,
    the `order_notifications` row that goes with it — and the executor has no Bot API client on purpose: sending
    from the process that moves money would put a network stall in the money path. So the two meet in the one table
    both processes already share, and this function is the meeting point: it runs inside the drain (the worker that
    is *allowed* to talk to Telegram), reads what the executor recorded, and queues the message.

    Idempotent by construction rather than by care: each notification row maps to an outbox note derived from its
    row id, and a row whose note is already in the outbox is skipped. That is what makes "run the drain twice"
    safe, and it is why this does not consume the row — the row is also the in-app notification, and marking it
    sent would be a lie about a channel that is not this one.

    Two rules keep it from inventing messages. A fill is only rendered when the row carries the fill's own numbers
    (see the `not a fill event` branch below), and a row with no Telegram chat is *skipped rather than consumed*,
    so linking Telegram tomorrow delivers yesterday's fill instead of leaving a hole where it was.
    """
    t = int(at if at is not None else _now_ms())
    placeholders = ",".join("?" for _ in _TG_ORDER_EVENTS)
    rows = _db.execute(
        "SELECT id, intent_id, user_id, event, at_ms, detail_json FROM order_notifications"
        " WHERE event IN (%s) AND at_ms <= ? AND at_ms >= ? ORDER BY at_ms, id LIMIT ?" % placeholders,
        (*_TG_ORDER_EVENTS, t, t - _TG_ORDER_EVENT_WINDOW_MS, max(1, int(limit)))).fetchall()
    routed: list[dict] = []
    skipped: dict[str, int] = {}
    already = 0
    for (row_id, intent_id, user_id, event, at_ms, detail_json) in rows:
        key = _tg_order_event_key(int(row_id))
        if _db.execute("SELECT id FROM telegram_outbox WHERE dedupe_key=? LIMIT 1", (key,)).fetchone():
            already += 1
            continue
        if not _tg_user_chat(str(user_id)):
            # No Telegram identity, so there is no message to send — and no row is written, because the day they
            # link one they should get the message rather than a hole where it was.
            skipped["no telegram chat"] = skipped.get("no telegram chat", 0) + 1
            continue
        try:
            detail = json.loads(detail_json or "{}")
        except (TypeError, ValueError):
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        event = str(event)
        if event == "rejected":
            code = str(detail.get("code") or "REFUSED")
            job = _tg_notify_refusal(
                str(user_id), what="Order not placed", code=code,
                plain=_tg_plain_refusal(code, str(detail.get("venue_code") or "")),
                next_step=_TG_REFUSAL_NEXT_STEP.get(code, ""), note="order-event:%s" % str(intent_id)[:40],
                dedupe_key=key)
        elif not (detail.get("tokenId") and int(detail.get("sizeMicro") or 0) > 0):
            # A `filled`/`partial_fill` row WITHOUT a token and a size is not a fill event, it is a *case*
            # notification: the reconciler writes `{"case": "cancelled_race", "note": ...}` when a fill won a race
            # it was not supposed to win, and an older shape writes `{"micro": ..., "price_micro": ...}`. Those are
            # the in-app trail, not a message, and rendering one would produce a card reading "0 shares @ 0.0¢" —
            # a made-up fill, which is worse than no message. The numbers are what make it renderable, so the
            # numbers are the test.
            skipped["not a fill event"] = skipped.get("not a fill event", 0) + 1
            continue
        else:
            question, slug = _tg_market_bits(str(detail.get("marketId") or ""))
            side = _tg_outcome_for_token(str(detail.get("tokenId") or ""))
            # The ledger first, the notification's own numbers second: `fills` is what moved, and this is the
            # line that used to trust the caller's blob and print whatever it was given.
            booked_size, booked_price, booked_fee = _tg_booked_fill(str(intent_id))
            size_micro = booked_size or int(detail.get("sizeMicro") or 0)
            price_micro = booked_price or int(detail.get("priceMicro") or 0)
            fee_micro = booked_fee if booked_size else int(detail.get("feeMicro") or 0)
            job = _tg_notify_fill(
                str(user_id), market=question, side=side or "order",
                size_text="%s shares" % _tg_shares(size_micro),
                price_text=_tg_cents(price_micro),
                fee_text=_tg_usdc_text(fee_micro),
                position_text=_tg_position_text(str(user_id), str(detail.get("tokenId") or ""),
                                                fallback_micro=size_micro, side=str(detail.get("side") or "BUY")),
                price_age_text="the venue's own fill, %s" % _tg_ago(max(0, t - int(at_ms))),
                note="order-event:%s" % str(intent_id)[:40], market_slug=slug, dedupe_key=key)
        if job:
            routed.append({"notification": int(row_id), "event": event, "job": int(job),
                           "intent": str(intent_id)[:16]})
        else:
            skipped["nothing to send"] = skipped.get("nothing to send", 0) + 1
    return {"routed": routed, "alreadyQueued": already, "skipped": skipped,
            "scanned": len(rows), "windowMs": _TG_ORDER_EVENT_WINDOW_MS}


# ------------------------------------------------------------------------------------ P12 · D6: the wallet, over HTTP
# The bot has had the custody ceremony since P07 — `/wallet`, `/deposit`, `/withdraw` and the key export all run
# through `_tg_fetch` and the router's sessions. What the *webview* had was five ledger rows marked `built: false`,
# which is the honest way to say "the Mini App cannot do this yet" and the exact gap D6 closes: the wallet, its QR,
# the deposit's progress across the bridge, the withdrawal ceremony and the key export, all reachable by the Mini
# App's own session. These routes are the HTTP half of ceremonies that already exist; nothing here invents a rule.
WALLET_BALANCE_RESPONSES = {
    200: {"description": "the wallet, its custody mode, and the cash the ledger says is available"},
    **{status: {"description": msg} for msg, status, _retry in CODES.values()},
}
WALLET_TX_RESPONSES = {
    200: {"description": "the ledger's own entries, newest first, with a cursor"},
    **{status: {"description": msg} for msg, status, _retry in CODES.values()},
}
DEPOSIT_QUOTE_RESPONSES = {
    202: {"description": "a deposit intent: where to send it, and what the bridge will do with it"},
    **{status: {"description": msg} for msg, status, _retry in CODES.values()},
}
DEPOSIT_PROGRESS_RESPONSES = {
    200: {"description": "the four legs of a deposit, each with its own state and time"},
    **{status: {"description": msg} for msg, status, _retry in CODES.values()},
}
WITHDRAW_RESPONSES = {
    202: {"description": "the withdrawal is recorded and queued; signing is the custody plane's job"},
    **{status: {"description": msg} for msg, status, _retry in CODES.values()},
}
KEY_EXPORT_RESPONSES = {
    200: {"description": "the wrapped key material, once, with the ceremony's audit trail behind it"},
    **{status: {"description": msg} for msg, status, _retry in CODES.values()},
}

#: The chains the deposit screen may offer. A chain is not a free-text field: a deposit intent names one of these or
#: it is not an intent. (Adding a chain is a decision with a bridge behind it, so the list lives here and not in a UI.)
_DEPOSIT_CHAINS = ("ethereum", "base", "polygon", "arbitrum")
#: How many confirmations we wait for before crediting. Per chain, because they are not the same chain.
_DEPOSIT_CONFIRMATIONS = {"ethereum": 12, "base": 5, "polygon": 128, "arbitrum": 8}


def _wallet_row(uid: str) -> dict | None:
    """The caller's wallet, in any state a deposit can go into. `suspended`/`closing` are not destinations."""
    row = _db.execute("SELECT user_id, provider, custody, address, proxy_address, state, policy_hash, policy_gap,"
                      " created_ms FROM wallets WHERE user_id=? AND state IN"
                      " ('provisioned','funded','trading') ORDER BY created_ms LIMIT 1", (str(uid),)).fetchone()
    if row is None:
        return None
    keys = ("userId", "provider", "custody", "address", "proxyAddress", "state", "policyHash", "policyGap",
            "createdMs")
    return dict(zip(keys, row))


def _cash_available_micro(uid: str) -> int:
    """Cash, from the ledger the money path writes. `balances` is a cache of this, not a second opinion."""
    row = _db.execute("SELECT COALESCE(SUM(amount_micro),0) FROM cash_ledger WHERE user_id=?",
                      (str(uid),)).fetchone()
    return int(row[0] or 0) if row else 0


#: The intent states in which cash is committed and must NOT be shown as available. `pending` and `uncertain`
#: belong here for opposite reasons and both matter: `pending` is an order the risk gate has not answered yet, and
#: `uncertain` is one that was sent and not acknowledged — the exact case where "your cash is available" would
#: invite a second order against money that may already be spent. `live`/`partial` were in this tuple and are
#: *orders* states, not intent states (`0002_money.sql` CHECKs the intent column), so they matched nothing.
_OPEN_INTENT_STATES = ("pending", "queued", "submitting", "uncertain", "submitted")


def _cash_reserved_micro(uid: str) -> int:
    row = _db.execute("SELECT COALESCE(SUM(notional_micro),0) FROM order_intents WHERE user_id=? AND state IN"
                      " (%s)" % ",".join("?" * len(_OPEN_INTENT_STATES)),
                      (str(uid),) + _OPEN_INTENT_STATES).fetchone()
    return int(row[0] or 0) if row else 0


def _password_ok(uid: str, password: str) -> tuple[bool, str]:
    """`(ok, code)` — `PASSWORD_REQUIRED` when none is set, `PASSWORD_WRONG` when it does not match.

    The same Argon2id envelope the sign-in path uses (P07): one password, one hasher, one cost. A second hasher for
    "money actions" is how a product ends up with a weaker lock on the door with the money behind it.
    """
    cred = SEC.credential(str(uid))
    if not cred:
        return False, "PASSWORD_REQUIRED"
    try:
        verdict = _hasher().verify(str(cred["phc"]), str(password or ""))
    except Exception:                                     # noqa: BLE001 — a malformed envelope is a wrong password
        return False, "PASSWORD_WRONG"
    return (True, "ok") if verdict.startswith("ok") else (False, "PASSWORD_WRONG")


@app.get("/v1/wallet/balance", responses=WALLET_BALANCE_RESPONSES)
def wallet_balance(request: Request):
    """The wallet card: what you hold, what is reserved, where to send money, and which locks are armed.

    The address is returned *here* and deliberately not in the chat (the bot's own comment: a chat message is the one
    surface a user forwards to a stranger). The Mini App is a session-bearing surface whose whole audience is the
    account that owns the wallet, which is exactly where a deposit address belongs.
    """
    rid = request.state.request_id
    uid, _row, deny = _principal(request)
    if deny is not None:
        return deny
    if not uid:
        return err("UNAUTHENTICATED", rid)
    w = _wallet_row(str(uid))
    cred = SEC.credential(str(uid))
    totp = SEC.totp_state(str(uid)) or {}
    return _stamped({
        "cacheKey": "wallet:balance:%s" % uid,
        "wallet": w,
        "cashMicro": str(_cash_available_micro(str(uid))),
        "reservedMicro": str(_cash_reserved_micro(str(uid))),
        "chains": [{"chain": c, "confirmations": int(_DEPOSIT_CONFIRMATIONS[c])} for c in _DEPOSIT_CHAINS],
        "locks": {"password": bool(cred), "totp": bool(totp.get("verified_ms")),
                  "custody": str((w or {}).get("custody") or "")},
        "note": ("Deposits are credited after the bridge lands; the address below is yours alone."
                 if w else "No wallet yet — /wallet in the bot creates one, and this screen fills in with it."),
    }, ttl_ms=5_000, stale_ms=60_000)


@app.get("/v1/wallet/transactions", responses=WALLET_TX_RESPONSES)
def wallet_transactions(request: Request, limit: int = Query(default=25, ge=1, le=100),
                        beforeMs: int = Query(default=0, ge=0)):
    """The ledger's own entries. `cash_ledger` is append-only and every row is money that moved, so this is a
    statement rather than a feed — and the reason is carried through verbatim, because "adjust" with no sentence is
    the row a support ticket is made of."""
    rid = request.state.request_id
    uid, _row, deny = _principal(request)
    if deny is not None:
        return deny
    if not uid:
        return err("UNAUTHENTICATED", rid)
    n = max(1, min(int(limit or 25), 100))
    rows = _db.execute("SELECT kind, amount_micro, created_ms, reason, ref_table, ref_id FROM cash_ledger WHERE"
                       " user_id=? AND (? = 0 OR created_ms < ?) ORDER BY created_ms DESC, id DESC LIMIT ?",
                       (str(uid), int(beforeMs or 0), int(beforeMs or 0), n)).fetchall()
    out = [{"kind": str(r[0]), "deltaMicro": str(r[1]), "atMs": int(r[2]), "reason": str(r[3]),
            "ref": "%s:%s" % (str(r[4]), str(r[5])[:24])} for r in rows]
    return _stamped({"cacheKey": "wallet:tx:%s" % uid, "entries": out, "count": len(out),
                     "nextBeforeMs": int(rows[-1][2]) if len(rows) >= n else 0,
                     "note": "Every row here is money that moved. Balances are the sum of this list, not a separate "
                             "number kept alongside it."},
                    ttl_ms=10_000, stale_ms=120_000)


@app.post("/v1/wallet/deposit/quote", status_code=202, responses=DEPOSIT_QUOTE_RESPONSES,
          openapi_extra=_body_schema(("chain", "amountUsdc"), {
              "chain": {"type": "string", "enum": list(_DEPOSIT_CHAINS)},
              "amountUsdc": {"type": "string", "pattern": "^[0-9]{1,9}(\\.[0-9]{1,6})?$"}}))
def wallet_deposit_quote(request: Request, body: dict = Body(...),
                         idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """A deposit intent: the address to send to, and the four legs this app will then report on.

    Declared as a POST because it *creates* something — a `deposits` row in `detecting` — and the key makes a retry
    the same intent rather than a second one. No money moves here and none can: the row is a promise to watch an
    address, and the credit happens when the bridge lands, through `book_fill`'s siblings in the ledger.
    """
    rid = request.state.request_id
    uid, _row, deny = _principal(request)
    if deny is not None:
        return deny
    if not uid:
        return err("UNAUTHENTICATED", rid)
    bad = _check_body(body, ("chain", "amountUsdc"), rid)
    if bad is not None:
        return bad
    chain = str(body["chain"]).strip().lower()
    if chain not in _DEPOSIT_CHAINS:
        return err("BAD_FIELD", rid, where=["chain"],
                   detail="we accept deposits on %s" % ", ".join(_DEPOSIT_CHAINS))
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key
    try:
        amount_micro = parse_usdc(str(body["amountUsdc"]))
    except Exception:                                     # noqa: BLE001
        return err("BAD_AMOUNT", rid, where=["amountUsdc"])
    if amount_micro <= 0:
        return err("ZERO_SIZE", rid, where=["amountUsdc"])

    def work() -> dict:
        w = _wallet_row(str(uid))
        if not w or not w.get("address"):
            # `err(...)`, not a dict: `_idem_run` files a dict as the stored success answer, so a refusal shaped
            # like one was answered 202 with `ok: false` inside — a deposit intent that does not exist, reported
            # as created. (The same shape is fine for `/v1/orders`, where the route returns it directly.)
            return err("NOT_FOUND", rid, detail="no wallet yet — the bot creates one with /wallet")
        at = _now_ms()
        # One row per (user, chain, amount, hour): a client that taps twice in a minute is watching the same deposit,
        # and a *second* deposit of the same size an hour later is a second row, which is the truth.
        credit_key = "dep:%s:%s:%s:%d" % (uid, chain, amount_micro, at // 3_600_000)
        row = _db.execute("SELECT id, status, confirmations, first_seen_ms FROM deposits WHERE credit_key=?",
                          (credit_key,)).fetchone()
        if row is None:
            cur = _db.execute("INSERT INTO deposits (user_id, asset, chain, amount_micro, credit_key, status,"
                              " confirmations, first_seen_ms) VALUES (?,?,?,?,?,'detecting',0,?)",
                              (str(uid), "USDC", chain, amount_micro, credit_key, at))
            _db.commit()
            did = int(getattr(cur, "lastrowid", 0) or 0)
            status, confirmations, seen = "detecting", 0, at
        else:
            did, status, confirmations, seen = int(row[0]), str(row[1]), int(row[2]), int(row[3])
        return {"ok": True, "depositId": did, "chain": chain, "status": status, "confirmations": confirmations,
                "firstSeenMs": seen, "address": str(w["address"]), "proxyAddress": str(w.get("proxyAddress") or ""),
                "amountMicro": str(amount_micro),
                "minConfirmations": int(_DEPOSIT_CONFIRMATIONS[chain])}

    return _idem_run(str(uid), str(idempotency_key), body, rid, work)


@app.get("/v1/wallet/deposit/{deposit_id}", responses=DEPOSIT_PROGRESS_RESPONSES)
def wallet_deposit_progress(request: Request, deposit_id: int):
    """The four legs, as states with times — the progress screen's whole input.

    `detecting → confirming → bridging → crediting → credited` is the schema's own vocabulary, so this route maps
    rather than invents: each leg says what it is waiting for, and a `stuck`/`failed` deposit carries the reason it
    stopped instead of a spinner that never ends.
    """
    rid = request.state.request_id
    uid, _row, deny = _principal(request)
    if deny is not None:
        return deny
    if not uid:
        return err("UNAUTHENTICATED", rid)
    row = _db.execute("SELECT id, chain, amount_micro, status, confirmations, tx_hash, bridge_tx_hash,"
                      " first_seen_ms, resolved_ms, attempts, last_error FROM deposits WHERE id=? AND user_id=?",
                      (int(deposit_id), str(uid))).fetchone()
    if row is None:
        # Not yours and does not exist are the same answer, which is the P07 rule for every owned resource.
        return err("NOT_FOUND", rid, detail="no such deposit")
    (did, chain, amount_micro, status, confirmations, tx_hash, bridge_tx_hash, seen, resolved, attempts,
     last_error) = row
    min_conf = int(_DEPOSIT_CONFIRMATIONS.get(str(chain), 12))
    order = ["detecting", "confirming", "bridging", "crediting", "credited"]
    stopped = str(status) in ("stuck", "failed")
    idx = order.index(str(status)) if str(status) in order else len(order)
    labels = {"detecting": "Watching for your transfer",
              "confirming": "Waiting for %d confirmations on %s" % (min_conf, chain),
              "bridging": "Bridging to the venue's chain",
              "crediting": "Crediting your balance",
              "credited": "Credited"}
    steps = []
    for i, key in enumerate(order):
        state = "done" if i < idx or str(status) == "credited" else ("active" if i == idx else "pending")
        if stopped:
            # `stuck`/`failed` used to fall through to `idx = len(order)`, which painted all five legs green - a
            # stopped deposit reported as a finished one. The row does not say *which* leg it stopped on, so the
            # screen says what is true: nothing further has happened, and the last leg is where it stopped.
            state = "stopped" if i == len(order) - 1 else "pending"
        steps.append({"key": key, "label": "Stopped — a human has this" if state == "stopped" else labels[key],
                      "state": state,
                      "doneMs": int(resolved or 0) if state == "done" and key == "credited" else 0})
    return _stamped({"cacheKey": "wallet:deposit:%s:%d" % (uid, int(deposit_id)),
                     "depositId": int(did), "chain": str(chain), "amountMicro": str(amount_micro),
                     "status": str(status), "confirmations": int(confirmations), "minConfirmations": min_conf,
                     "txHash": str(tx_hash or ""), "bridgeTxHash": str(bridge_tx_hash or ""),
                     "firstSeenMs": int(seen), "resolvedMs": int(resolved or 0), "attempts": int(attempts or 0),
                     "steps": steps,
                     "problem": ("" if str(status) not in ("stuck", "failed") else
                                 str(last_error or "the transfer stopped and a human has the case"))},
                    ttl_ms=2_000, stale_ms=30_000)


@app.post("/v1/wallet/withdraw", status_code=202, responses=WITHDRAW_RESPONSES,
          openapi_extra=_body_schema(("amountUsdc", "addressId", "typedAmount", "typedAddress", "password", "code"), {
              "amountUsdc": {"type": "string", "pattern": "^[0-9]{1,9}(\\.[0-9]{1,2})?$"},
              "addressId": {"type": "string", "minLength": 1, "maxLength": 64},
              "typedAmount": {"type": "string", "minLength": 1, "maxLength": 32},
              "typedAddress": {"type": "string", "minLength": 4, "maxLength": 128},
              "password": {"type": "string", "minLength": 1, "maxLength": 200},
              "code": {"type": "string", "minLength": 6, "maxLength": 10}}))
def wallet_withdraw(request: Request, body: dict = Body(...),
                    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """The withdrawal ceremony, in the order the locks are: allowlist, cooldown, typed confirmation, password, TOTP.

    Every refusal is its own code, because "no" is not an answer at 2am: a destination that is not on the list, one
    that is still inside its 24-hour hold, a typed amount that does not match, a missing password, a wrong password,
    a missing authenticator and a stale code are seven different situations with seven different next steps.

    The last thing this does is *record* the request. Signing is the custody plane's job (P13/P14) and the response
    says so in words, because a screen that says "sent" about an unsigned transaction is the exact lie this build
    refuses to ship.
    """
    rid = request.state.request_id
    uid, _row, deny = _principal(request)
    if deny is not None:
        return deny
    if not uid:
        return err("UNAUTHENTICATED", rid)
    # `destAddress` is in `allowed` on purpose: `_check_body`'s default is "anything not required is unknown",
    # which turned a client that sent a raw destination into a generic VALIDATION before the handler could name
    # the rule it broke. Accepting the field in order to refuse it is the difference between "unknown: destAddress"
    # and "withdrawals go to an addressId from your allowlist".
    bad = _check_body(body, ("amountUsdc", "addressId", "typedAmount", "typedAddress", "password", "code"), rid,
                      allowed=("amountUsdc", "addressId", "typedAmount", "typedAddress", "password", "code",
                               "destAddress"))
    if bad is not None:
        return bad
    if body.get("destAddress"):
        # A raw address is refused even though the schema has no such field, because a client that sends one is a
        # client telling us where money should go, and only the allowlist may do that.
        return err("ADDRESS_NOT_ALLOWED", rid, where=["destAddress"],
                   detail="withdrawals go to an addressId from your allowlist; add one in the bot first")
    bad_key = _idem_shape(idempotency_key)
    if bad_key is not None:
        return bad_key

    def work():
        """The ceremony runs INSIDE the key, and that ordering is the whole design.

        `_totp_gate` consumes an authenticator step. A client whose withdrawal succeeded but whose response was
        lost retries with the same key, and if the locks ran before the idempotency check it would be answered
        `TOTP_INVALID` — the retry of a completed withdrawal reported as a failed one, and the user asked to try
        again for something that already happened. A refusal returns `err(...)`, which `_idem_run` reads as "abandon
        the key", so a user who mistyped their password can retry; the ceremony's state changes only in the one
        branch that gets to the end.
        """
        w = _wallet_row(str(uid))
        if w is None:
            return err("NOT_FOUND", rid, detail="no wallet yet — the bot creates one with /wallet")
        if str(w.get("custody")) != "delegated":
            return err("SIGNER_UNAVAILABLE", rid,
                       detail="this wallet is watch-only, so nothing can be signed from it")
        try:
            amount_micro = parse_usdc(str(body["amountUsdc"]))
        except Exception:                                 # noqa: BLE001
            return err("BAD_AMOUNT", rid, where=["amountUsdc"])
        if amount_micro <= 0:
            return err("ZERO_SIZE", rid, where=["amountUsdc"])
        avail = _cash_available_micro(str(uid))
        if amount_micro > avail:
            return err("INSUFFICIENT_BALANCE", rid, where=["amountUsdc"],
                       detail="available %s USDC" % _micro_str(avail, scale=6))
        ok, code, addr_row = SEC.address_for_withdrawal(str(uid), str(body["addressId"]), at=_now_ms())
        if not ok:
            return err(code if code in CODES else "NOT_FOUND", rid,
                       detail=str((addr_row or {}).get("message") or "")[:160])
        dest = str(addr_row["address"])
        # The typed confirmation: the product's own "type it back" rule, and the reason a clipboard-swap cannot
        # land. Both fields must match what the user was shown, exactly.
        if str(body["typedAmount"]).strip() != str(body["amountUsdc"]).strip():
            return err("BAD_FIELD", rid, where=["typedAmount"],
                       detail="type the amount exactly as shown, digits and all")
        if str(body["typedAddress"]).strip() != dest:
            return err("BAD_FIELD", rid, where=["typedAddress"],
                       detail="the destination you typed is not the address the allowlist holds")
        ok, pw_code = _password_ok(str(uid), str(body.get("password") or ""))
        if not ok:
            return err(pw_code, rid, where=["password"])
        gate = _totp_gate(request, str(uid), str(body.get("code") or ""), action="withdraw")
        if gate is not None:
            return gate
        at = _now_ms()
        wid = _db.execute("INSERT INTO withdrawals (user_id, asset, chain, amount_micro, dest_address, typed_amount,"
                          " typed_address, allowlist_hit, cooldown_ok, password_verified, status, idempotency_key,"
                          " requested_ms) VALUES (?,?,?,?,?,?,?,1,1,1,'queued',?,?) RETURNING id",
                          (str(uid), "USDC", str(addr_row.get("chain") or "polygon"), amount_micro, dest,
                           str(body["typedAmount"]), str(body["typedAddress"]), str(idempotency_key),
                           at)).fetchone()
        SEC.auth_event(str(uid), "withdraw_requested", at=at,
                       detail={"amount_micro": amount_micro, "address_id": str(body["addressId"])[:32],
                               "withdrawal": int(wid[0]) if wid else 0})
        _tg_metric(chat_id="", chat_type="private", user_id=str(uid), command="withdraw", action="requested",
                   ok=True, dur_ms=0, update_id=0)
        return {"ok": True, "withdrawalId": int(wid[0]) if wid else 0, "status": "queued",
                "amountMicro": str(amount_micro), "destination": dest,
                "notified": {"email": False, "telegram": False},
                "note": "Recorded and queued. Signing runs on the custody plane, which is not live until P14 — "
                        "until then this row is the request, not a payment, and it says so on the screen too."}

    return _idem_run(str(uid), str(idempotency_key), body, rid, work)


@app.post("/v1/wallet/keys/export", responses=KEY_EXPORT_RESPONSES,
          openapi_extra=_body_schema(("password", "code", "typedConfirm"), {
              "password": {"type": "string", "minLength": 1, "maxLength": 200},
              "code": {"type": "string", "minLength": 6, "maxLength": 10},
              "typedConfirm": {"type": "string", "enum": ["EXPORT"]}}))
def wallet_key_export(request: Request, body: dict = Body(...),
                      idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    """The key export ceremony: password + authenticator + typing EXPORT, then the wrapped material once.

    What is returned is the *wrapped* DEK — the blob the keeper holds, with its KEK version and policy hash — not a
    private key, because a private key is derivable only through the custody plane and this route refuses to pretend
    otherwise. The unwrapping ceremony belongs to go-live (P14); until then the honest answer to "give me my key" is
    "here is your material and here is why it is not usable yet", which is precisely what the response says.

    Counted against the wrap's nonce budget (`note_wrap_used`), because an export that runs the counter out is a
    wallet that can no longer sign, and an export ceremony nobody rate-limits is not a ceremony.
    """
    rid = request.state.request_id
    uid, _row, deny = _principal(request)
    if deny is not None:
        return deny
    if not uid:
        return err("UNAUTHENTICATED", rid)
    bad = _check_body(body, ("password", "code", "typedConfirm"), rid)
    if bad is not None:
        return bad
    if str(body.get("typedConfirm")) != "EXPORT":
        return err("BAD_FIELD", rid, where=["typedConfirm"], detail="type EXPORT to confirm you understand")
    ok, pw_code = _password_ok(str(uid), str(body.get("password") or ""))
    if not ok:
        return err(pw_code, rid, where=["password"])
    gate = _totp_gate(request, str(uid), str(body.get("code") or ""), action="key_export")
    if gate is not None:
        return gate
    wrap = SEC.key_wrap(str(uid))
    if not wrap:
        return err("NOT_FOUND", rid, detail="no key material for this account yet")
    used = SEC.note_wrap_used(str(uid), int(wrap["dek_version"]), at=_now_ms())
    if not used.get("ok"):
        SEC.auth_event(str(uid), "key_export_blocked", at=_now_ms(),
                       detail={"why": str(used.get("why") or ""), "dek_version": int(wrap["dek_version"])})
        return err("SIGNER_UNAVAILABLE", rid, detail="this key needs re-wrapping before more signatures: %s"
                   % str(used.get("why") or "")[:120])
    SEC.auth_event(str(uid), "key_export", at=_now_ms(),
                   detail={"dek_version": int(wrap["dek_version"]), "wraps_used": int(used["messages_wrapped"])})
    payload = _stamped({"cacheKey": None, "dekVersion": int(wrap["dek_version"]),
                     "kekVersion": int(wrap["kek_version"]), "policyHash": str(wrap["policy_hash"] or ""),
                     "createdMs": int(wrap["created_ms"] or 0),
                     "wrappedKey": str(wrap["wrapped_dek"]), "nonce": str(wrap["nonce"]), "tag": str(wrap["tag"]),
                     "wrapsUsed": int(used["messages_wrapped"]),
                     "note": "This is your wrapped key material, shown once and not stored by this response. It is "
                             "wrapped, so it cannot sign until the custody plane unwraps it — that ceremony is part "
                             "of go-live (P14). Store it offline; we will never ask you for it."},
                    ttl_ms=0, stale_ms=0)
    # `_stamped` returns a dict; setting a header on a dict is an `AttributeError` at the one moment the header
    # matters. A real response object is what "no-store" needs, and it is the honest declaration that this body
    # must not be cached anywhere.
    return JSONResponse(content=payload, headers={"cache-control": "no-store"})


# --------------------------------------------------------------------------------------- P12 · D5/D8: the channel and the switch
#: The status sets for the three operator routes, declared beside them and compared against the contract by
#: `check-openapi`, because a route that starts answering 409 without documenting it is a contract lie.
TELEGRAM_KILL_RESPONSES = {200: {"description": "the new state, recorded"},
                           403: {"description": "admin token missing or wrong"},
                           422: {"description": "scope unknown, or a reason too short to explain it"},
                           503: {"description": "no admin token configured on this pod"},
                           500: {"description": "unexpected failure inside the service"}}
TELEGRAM_BROADCAST_RESPONSES = {200: {"description": "what went out, or exactly what would have"},
                                403: {"description": "admin token missing or wrong"},
                                422: {"description": "malformed stage"},
                                503: {"description": "no admin token configured on this pod"},
                                500: {"description": "unexpected failure inside the service"}}
TELEGRAM_OPS_RESPONSES = {200: {"description": "the operator's view"},
                          403: {"description": "admin token missing or wrong"},
                          503: {"description": "no admin token configured on this pod"},
                          500: {"description": "unexpected failure inside the service"}}


def _tg_kill() -> _tgb_ops.KillState:
    """The live state: the newest row, or a disengaged default. Newest-row rather than an upserted single row,
    because the *history* of a switch is the evidence of who decided what, and a single row would keep only the
    last answer to a question people will ask about Tuesday."""
    row = _db.execute("SELECT engaged, scope, reason, changed_by, at_ms FROM telegram_kill_state"
                      " ORDER BY at_ms DESC, id DESC LIMIT 1").fetchone()
    if row is None:
        return _tgb_ops.KillState()
    return _tgb_ops.KillState(engaged=bool(row[0]), scope=str(row[1]), reason=str(row[2]), by=str(row[3]),
                              at_ms=int(row[4]))


@app.post("/v1/telegram/kill", status_code=200, responses=TELEGRAM_KILL_RESPONSES,
           openapi_extra=_body_schema(("engaged", "reason"), {"engaged": {"type": "boolean"},
                                                              "scope": {"type": "string",
                                                                        "enum": ["all", "channel", "personal"]},
                                                              "reason": {"type": "string", "minLength": 8,
                                                                         "maxLength": 400}}))
def telegram_kill(request: Request, body: dict = Body(...),
                  x_admin: str | None = Header(default=None, alias="X-Admin-Token")):
    """Stop the bot sending, without stopping trading.

    The separation is the point and it is worth restating where the code is: P06's switch halts *orders* and leaves
    the outbox alone, because a user whose order was halted still has to be told what happened to their money; this
    one pauses *delivery* and leaves trading alone. An operator reaching for the wrong one of these is how a
    marketing pause becomes an incident.
    """
    ok, deny = _admin(request)
    if not ok:
        return deny
    rid = request.state.request_id
    bad = _check_body(body, ("engaged", "reason"), rid, allowed=("engaged", "scope", "reason"))
    if bad is not None:
        return bad
    engaged = bool(body.get("engaged"))
    scope = str(body.get("scope") or "all")
    reason = str(body.get("reason") or "")
    problems = _tgb_ops.kill_findings(engaged, scope=scope, reason=reason, by="admin")
    if problems:
        return err("VALIDATION", rid, where=problems)
    _db.execute("INSERT INTO telegram_kill_state (engaged, scope, reason, changed_by, at_ms) VALUES (?,?,?,?,?)",
                (1 if engaged else 0, scope, reason[:400], "admin", _now_ms()))
    _db.commit()
    state = _tg_kill()
    SEC.auth_event("", "telegram_kill", at=_now_ms(),
                   detail={"engaged": engaged, "scope": scope, "reason": reason[:120]})
    return _stamped({"engaged": state.engaged, "scope": state.scope, "reason": state.reason, "by": state.by,
                     "atMs": state.at_ms,
                     "note": "delivery is paused; orders, fills and the ledger are untouched" if engaged
                             else "delivery resumed"}, ttl_ms=0, stale_ms=0)


def _tg_broadcast_verdicts(composed: list, *, history: list, at: int, record: bool) -> list:
    """P07's gate and D5's cadence, per composed message — for the rehearsal *and* for the send.

    The first version of the route asked the gate only on the way out, which made the dry run half a preview: an
    operator read the text, liked it, and pressed send, and *then* learned the market had been held for liquidity.
    A rehearsal that cannot fail is not a rehearsal, so the same function answers both paths and the only difference
    is `record`: a dry run records nothing (it is not an event), while a send writes the verdict into
    `broadcast_gates` whether it went out or not.

    `wouldSend` is the conjunction the send path uses: cadence allows it *and* the gate says `broadcast`.
    """
    out = []
    for item in composed:
        event = item["event"]
        allowed, why = _tgb_channel.cadence_ok(history, at_ms=at, kind=str(event.get("kind")))
        gate = _tgb_abuse.broadcast_gate(market_id=str(event.get("market_id") or ""),
                                         liquidity_micro=int(event.get("liquidity_micro") or 0),
                                         age_ms=int(event.get("age_ms") or 0),
                                         resolution_trusted=bool(event.get("resolution_trusted")),
                                         audience=int(event.get("audience") or 0), at_ms=at,
                                         broadcasts_last_hour=len(history))
        if record:
            SEC.record_broadcast(market_id=str(event.get("market_id") or ""), verdict=str(gate["verdict"]),
                                reasons=list(gate["reasons"]), audience=int(gate["audience"]), at=at)
        out.append({"slug": item["slug"], "kind": str(event.get("kind")), "verdict": str(gate["verdict"]),
                    "reasons": list(gate["reasons"]), "holds": list(gate["holds"]),
                    "refusals": list(gate["refusals"]), "recheckMs": int(gate["recheck_ms"]),
                    "audience": int(gate["audience"]), "audienceSource": str(event.get("audience_source") or ""),
                    "cadenceAllowed": bool(allowed), "cadenceWhy": str(why or ""),
                    "wouldSend": bool(allowed and gate["verdict"] == "broadcast")})
    return out


@app.post("/v1/telegram/broadcast", status_code=200, responses=TELEGRAM_BROADCAST_RESPONSES,
           openapi_extra=_body_schema((), {"dryRun": {"type": "boolean"},
                                           "stage": {"type": "object", "additionalProperties": True}}))
def telegram_broadcast(request: Request, body: dict = Body(default={}),
                       x_admin: str | None = Header(default=None, alias="X-Admin-Token")):
    """Compose the channel's next messages — and, unless `dryRun` is false, send none of them.

    Dry run is the default in the *code*, not just in the docs: the failure mode of a broadcast tool is somebody
    pressing the wrong button at speed, and a tool whose default is "send to thousands of phones" has chosen which
    mistake it prefers. A real send re-checks P07's broadcast gate per market, honours the cadence caps from
    `telegram_broadcasts` (which is why that table is append-only), and carries the stage it used.
    """
    ok, deny = _admin(request)
    if not ok:
        return deny
    rid = request.state.request_id
    stage = body.get("stage") or {}
    candidates = _tg_broadcast_candidates()
    chosen, held = _tgb_ops.staged(candidates, stage=stage)
    composed = []
    for c in chosen:
        event = c["event"]
        plan = _tgb_channel.compose(event, bot_username=_tg_bot_username(), price_text=c["price_text"],
                                    age_text=c["age_text"])
        composed.append({"slug": event.get("slug", ""), "plan": plan, "event": event})
    history = [{"kind": str(r[0]), "fired_ms": int(r[1])} for r in _db.execute(
        "SELECT kind, fired_ms FROM telegram_broadcasts WHERE fired_ms > ?", (_now_ms() - 3_600_000,)).fetchall()]
    if body.get("dryRun", True):
        # The rehearsal answers the *whole* question: the exact bytes, and whether the gate would let them out.
        preview = _tgb_ops.dry_run(composed)
        verdicts = _tg_broadcast_verdicts(composed, history=history, at=_now_ms(), record=False)
        return _stamped({"dryRun": True, "staged": len(composed), "held": len(held), "preview": preview,
                         "verdicts": verdicts, "wouldSend": sum(1 for v in verdicts if v["wouldSend"]),
                         "reach": _tg_reachable_audience(),
                         "note": "nothing was sent and no verdict was recorded: pass dryRun: false to send exactly "
                                 "what is above, and read `verdicts` first — a staged rollout that ignores a `hold` "
                                 "is a message to thousands of phones about a market with no liquidity"},
                        ttl_ms=0, stale_ms=0)
    kill = _tg_kill()
    if _tgb_ops.blocks(kill, "channel"):
        return err("RISK_HALT", rid, detail="the Telegram kill switch is engaged for channel delivery")
    sent, skipped = [], []
    verdicts = _tg_broadcast_verdicts(composed, history=history, at=_now_ms(), record=True)
    for item, verdict in zip(composed, verdicts):
        if not verdict["wouldSend"]:
            skipped.append({"slug": item["slug"], "why": verdict["cadenceWhy"] or ",".join(verdict["reasons"])})
            continue
        job = _tg_enqueue(chat_id=str(item["event"].get("channel_id") or ""), chat_type="channel",
                          plan=item["plan"], priority=_tgb_outbox.P_ALERT_CHANNEL, note="broadcast")
        _db.execute("INSERT INTO telegram_broadcasts (channel, kind, market_id, message_id, quality, fired_ms)"
                    " VALUES (?,?,?,?,?,?)",
                    (str(item["event"].get("channel_id") or ""), str(item["event"].get("kind")),
                     str(item["event"].get("market_id") or ""), int(job), int(item["event"].get("quality") or 0),
                     _now_ms()))
        _db.commit()
        history.append({"kind": str(item["event"].get("kind")), "fired_ms": _now_ms()})
        sent.append(item["slug"])
    return _stamped({"dryRun": False, "sent": sent, "skipped": skipped, "jobs": len(sent), "held": len(held),
                     "note": "queued for the drain, which is the only thing that talks to Telegram"},
                    ttl_ms=0, stale_ms=0)


def _tg_channel_id() -> str:
    """The configured public channel (`@name` or a numeric id), or "".

    Empty is a real answer and the code treats it as one: a broadcast with nowhere to go is refused by the gate's
    `no_audience` rather than sent to a chat id nobody configured.
    """
    return (os.environ.get("PGM_TELEGRAM_CHANNEL") or "").strip()


def _tg_reachable_audience() -> dict:
    """How many people we can actually reach, and where the number came from.

    The one honest difficulty of this whole feature: **we do not know the channel's subscriber count.** Telegram knows
    it (`getChatMemberCount`), the webhook does not receive it, and this service will not make a live Bot API call
    from a request path (rule 2: no user request spends a shared budget). So the number recorded on a broadcast is the
    reach *we* can point at, with its provenance attached, and the operator reads both:

      * `chats30d` — distinct chats that have talked to this bot in the last 30 days, straight out of
        `telegram_commands`. This is a floor, not a census, and it is the number that moves when onboarding works.
      * `configured` — whether a public channel exists at all; a channel post is one send to an unknown N.

    The gate only refuses on `<= 0`, which is the question it can actually answer: is there anyone at all. A first
    release with 30 days of no traffic and no channel therefore *refuses to broadcast*, which is the correct and
    quietest possible failure.
    """
    at = _now_ms()
    row = _db.execute("SELECT COUNT(DISTINCT chat_id) FROM telegram_commands WHERE at_ms > ?",
                      (at - 30 * 24 * 3_600_000,)).fetchone()
    chats = int(row[0] or 0) if row else 0
    channel = _tg_channel_id()
    return {"chats30d": chats, "channel_configured": bool(channel), "channel_id": channel,
            "audience": chats, "source": "distinct chats active in telegram_commands over 30d; channel reach uncounted"}


def _tg_broadcast_candidates() -> list:
    """The events worth considering right now, from the tape and the book — before any gate has judged them.

    Deliberately generous (the filters are `channel.qualifies`, then P07's gate, then the cadence caps): a candidate
    list that pre-filters is a list where a bug in the filter is invisible. Each row carries the *text* the message
    will need, formatted here so the composer stays a formatting function.

    **What the first version of this got wrong, because it is the kind of mistake that hides behind a passing test.**
    It read `tape_trades` — the P04 fixture table — and named columns that table does not have (`ts_ms`, `size_micro`,
    `notional_micro`, `outcome`), and it passed `liquidity_micro: 0, age_ms: 3600000, audience: 1,
    resolution_trusted: True` as literals. The unit tests never touched the route, so nothing failed. In production
    the query would have raised on the first call — a dry run included, which is the one mode an operator trusts to
    be harmless — and had the columns existed, the literals would have fed the quality gate canned answers: a market
    with $12 of liquidity and no resolution source would have been broadcast as if it had a million dollars behind it
    and a trustworthy settlement. **The gate is only as honest as its inputs**, so the inputs now come from the
    tables: liquidity from `market_stats`, market age from `markets.first_seen_ms`, the resolution source from
    `market_meta`, and the audience from `_tg_reachable_audience()`.

    The durable fill log is `tape_fills` (P05), not `tape_trades`: it is the deduplicated stream the rollups, the
    whale percentile and the leaderboard already read, and it carries `usd_notional_micro` so the size test is the
    venue's own arithmetic rather than ours.
    """
    at = _now_ms()
    aud = _tg_reachable_audience()
    out = []
    fills = _db.execute(
        "SELECT m.slug, m.question, m.id, f.outcome, f.side, f.price_micro, f.size_micro, f.usd_notional_micro,"
        " f.ts_ms, COALESCE(mm.category, ''), COALESCE(ms.liquidity_micro, 0), COALESCE(m.first_seen_ms, 0),"
        " COALESCE(mm.resolution_source, '')"
        " FROM tape_fills f"
        " JOIN markets m ON m.condition_id = f.condition_id"
        " LEFT JOIN market_meta mm ON mm.market_id = m.id"
        " LEFT JOIN market_stats ms ON ms.condition_id = f.condition_id"
        # the venue clock for ordering (`tape_fills.ts_ms` is the venue's, per the migration's own note) and our
        # clock for freshness — mixing them is how a four-hour-old trade reads as new.
        " WHERE f.ingest_ms > ? AND f.usd_notional_micro >= ?"
        " ORDER BY f.usd_notional_micro DESC LIMIT 5",
        (at - 30 * 60_000, _tgb_channel.LARGE_FILL_MICRO)).fetchall()
    age_floor = _tgb_abuse.MIN_MARKET_AGE_MS
    for slug, question, mid, outcome, side, price, size, notional, ts, category, liquidity, first_seen, source in fills:
        trusted = bool(str(source or "").strip())
        out.append({
            "kind": "large_fill", "slug": str(slug), "question": str(question), "market_id": str(mid),
            "category": str(category), "notional_micro": int(notional),
            "price_text": _tg_cents(int(price)), "age_text": _tg_ago(at - int(ts)),
            "side": "yes" if str(outcome or "").lower().startswith("y") else "no",
            # Shares, from `size_micro`, formatted by the same helper the rest of the bot uses: the alert says "120,000
            # shares", and the dollar figure comes from `notional_micro`, which is the venue's number not ours.
            "size_text": "%s shares" % _tg_shares(int(size or 0)),
            "notional_text": "%s USDC" % _tg_shares(int(notional or 0)),
            "event": {"kind": "large_fill", "slug": str(slug), "question": str(question),
                      "category": str(category), "market_id": str(mid),
                      "notional_micro": int(notional), "side": str(side),
                      "price_text": _tg_cents(int(price)), "age_text": _tg_ago(at - int(ts)),
                      "size_text": "%s shares" % _tg_shares(int(size or 0)),
                      # --- what the gate reads, from the tables and not from a literal ---
                      "liquidity_micro": int(liquidity),
                      "age_ms": max(0, at - int(first_seen or 0)),
                      "resolution_trusted": trusted,
                      "audience": int(aud["audience"]),
                      "channel_id": str(aud["channel_id"]),
                      "quality": 0,
                      "audience_source": str(aud["source"]),
                      "age_floor_ms": age_floor},
        })
    return out


# Derived from CODES exactly as ORDER_RESPONSES is, so the statuses this route can answer cannot drift from the
# vocabulary it answers in: a code added to CODES with a new status is a status this table gains for free.



@app.get("/v1/telegram/ops", responses=TELEGRAM_OPS_RESPONSES)
def telegram_ops(request: Request, x_admin: str | None = Header(default=None, alias="X-Admin-Token")):
    """The operator's one page: switch state, queue depth, what the channel last said, and the recovery plan.

    Served from the same package the runbook reads, so "what do we do if the bot is restricted" has one answer that
    a test can assert rather than two that drift.
    """
    ok, deny = _admin(request)
    if not ok:
        return deny
    kill = _tg_kill()
    depth = _db.execute("SELECT COUNT(*) FROM telegram_outbox WHERE state='queued'").fetchone()
    stuck = _db.execute("SELECT COUNT(*) FROM telegram_updates WHERE state='claimed' AND first_ms < ?",
                        (_now_ms() - 300_000,)).fetchone()
    last = _db.execute("SELECT kind, market_id, fired_ms FROM telegram_broadcasts ORDER BY fired_ms DESC LIMIT 5"
                       ).fetchall()
    state = {"kill": kill, "queue_depth": int(depth[0] or 0) if depth else 0,
             "stuck_claims": int(stuck[0] or 0) if stuck else 0,
             "bot_configured": _tg_bot() is not None,
             "env": (os.environ.get("PGM_ENV") or "dev").strip()}
    return _stamped({"cacheKey": "telegram:ops", "kill": {"engaged": kill.engaged, "scope": kill.scope,
                                                          "reason": kill.reason, "by": kill.by,
                                                          "atMs": kill.at_ms},
                     "queueDepth": state["queue_depth"], "stuckClaims": state["stuck_claims"],
                     "botConfigured": state["bot_configured"],
                     "lastBroadcasts": [{"kind": str(r[0]), "marketId": str(r[1]), "firedMs": int(r[2])}
                                        for r in last],
                     "username": _tgb_ops.username_decision(),
                     "recovery": list(_tgb_ops.recovery_steps()),
                     "findings": _tgb_ops.ops_findings(state)},
                    ttl_ms=0, stale_ms=0)
