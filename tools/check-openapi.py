#!/usr/bin/env python3
"""P04 check: does contracts/openapi.yaml describe the API that is actually running?

This file exists because of one bug: a route returned 200 where the contract promised 202 (in FastAPI,
returning a JSONResponse overrides the declared status). A reviewer reading the yaml would have written
correct client code and been wrong, and no unit test of the handler could see it — the handler's dict was
right, only the status was not.

So three artifacts are compared, and nothing here is hand-copied from another:

    contracts/openapi.yaml     what we tell the world
    services/api/app.py        what the routes DECLARE (the `responses=` tables, read out of the AST)
    the live app object        what the routes actually EXPOSE (paths, verbs, parameters)

Any two can be internally consistent and still disagree with the third, which is the case worth automating.
"""
from __future__ import annotations

import argparse
import ast
import json
import contextlib
import io
import os
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "contracts" / "openapi.yaml"
APP = ROOT / "services" / "api" / "app.py"
GATE = ROOT / "packages" / "polygm_core" / "risk" / "gate.py"

# FastAPI's own pages: present in the app, absent from the contract, because the production app turns them
# off. An empty allowlist would read as "we forgot", so the three are written down with a reason.
BUILTIN_DOCS = {"/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}

# Which `responses=` table each documented operation declares. Keyed by the URL template EXACTLY as both
# files spell it: a path parameter cannot be aliased (FastAPI names it from the `{placeholder}`), so the
# contract adopts the app's snake_case for those, and camelCase survives only on query parameters, where
# `alias=` is real. Normalising the difference away would have hidden an actual rename.
TABLE_FOR_PATH = {
    ("GET", "/v1/automations"): "AUTOMATION_LIST_RESPONSES",
    "/v1/automations/runs": "AUTOMATION_RUNS_RESPONSES",
    "/v1/automations/templates": "AUTOMATION_TEMPLATES_RESPONSES",
    ("POST", "/v1/automations"): "AUTOMATION_CREATE_RESPONSES",
    "/v1/automations/preview": "AUTOMATION_PREVIEW_RESPONSES",
    "/v1/automations/guards": "AUTOMATION_GUARD_RESPONSES",
    # P12. The webhook answers 403 when the secret header is wrong and 503 when no secret is configured on the
    # pod; the Mini App session answers 401 for a tampered payload and 409 for a replayed one (the same two
    # statuses /v1/auth/telegram uses), and a drain or a metrics read is 403 without the admin token.
    # P12 D6, the wallet. Six routes, each with its own table because each answers a different set of refusals:
    # a balance read can 401 and nothing else, while the withdrawal ceremony can answer with seven different
    # codes in seven different situations and has to declare every one of them.
    ("GET", "/v1/wallet/balance"): "WALLET_BALANCE_RESPONSES",
    ("GET", "/v1/wallet/transactions"): "WALLET_TX_RESPONSES",
    ("POST", "/v1/wallet/deposit/quote"): "DEPOSIT_QUOTE_RESPONSES",
    ("GET", "/v1/wallet/deposit/{deposit_id}"): "DEPOSIT_PROGRESS_RESPONSES",
    ("POST", "/v1/wallet/withdraw"): "WITHDRAW_RESPONSES",
    ("POST", "/v1/wallet/keys/export"): "KEY_EXPORT_RESPONSES",
    ("POST", "/v1/telegram/webhook"): "TELEGRAM_WEBHOOK_RESPONSES",
    ("POST", "/v1/telegram/session"): "TELEGRAM_SESSION_RESPONSES",
    "/v1/telegram/commands": "TELEGRAM_COMMANDS_RESPONSES",
    ("POST", "/v1/telegram/drain"): "TELEGRAM_DRAIN_RESPONSES",
    ("POST", "/v1/telegram/kill"): "TELEGRAM_KILL_RESPONSES",
    ("POST", "/v1/telegram/order"): "TELEGRAM_ORDER_RESPONSES",
    # P12 · the web ticket's route. Same derived table shape as the other order routes, so adding a code to CODES
    # adds its status here too — and the checker's AST walk reads a real comprehension, not an alias.
    ("POST", "/v1/orders/amount"): "ORDER_AMOUNT_RESPONSES",
    ("POST", "/v1/telegram/broadcast"): "TELEGRAM_BROADCAST_RESPONSES",
    "/v1/telegram/ops": "TELEGRAM_OPS_RESPONSES",
    "/v1/telegram/metrics": "TELEGRAM_METRICS_RESPONSES",
    ("GET", "/v1/alerts"): "ALERT_LIST_RESPONSES",
    # Two methods on one path: the table is keyed by (verb, path) here because the two serve
    # different status sets, and a path-only row would have held the POST to the GET's list.
    ("POST", "/v1/alerts"): "ALERT_UPSERT_RESPONSES",
    "/v1/alerts/test": "ALERT_TEST_RESPONSES",
    "/v1/alerts/deliveries": "ALERT_DELIVERIES_RESPONSES",
    "/v1/alerts/settings": "ALERT_SETTINGS_RESPONSES",
    # P11 D2. One path with two verbs (the read and the recompute write), so both rows are verb-keyed.
    ("GET", "/v1/leaderboard"): "LEADERBOARD_RESPONSES",
    ("POST", "/v1/leaderboard/recompute"): "LEADERBOARD_RECOMPUTE_RESPONSES",
    "/v1/leaderboard/boards": "LEADERBOARD_METHODOLOGY_RESPONSES",
    "/v1/leaderboard/methodology": "LEADERBOARD_METHODOLOGY_RESPONSES",
    "/v1/leaderboard/why": "LEADERBOARD_WHY_RESPONSES",
    "/v1/leaderboard/snapshots": "LEADERBOARD_SNAPSHOT_RESPONSES",
    "/v1/leaderboard/runs": "LEADERBOARD_RUN_RESPONSES",
    # D3: three reads and one write, all four on the leaderboard tag. `("GET", …)`/`("POST", …)` rows because
    # `/follows` serves both a read and a write, and the two have different status sets.
    "/v1/leaderboard/rank": "LEADERBOARD_RANK_RESPONSES",
    "/v1/leaderboard/compare": "LEADERBOARD_COMPARE_RESPONSES",
    ("GET", "/v1/leaderboard/follows"): "LEADERBOARD_FOLLOWS_RESPONSES",
    ("POST", "/v1/leaderboard/follows"): "LEADERBOARD_FOLLOW_RESPONSES",
    # D4: `me` is one read; `/identity` is a read AND a write with different status sets, so each verb names
    # its own table - the same shape `/follows` needed in D3.
    "/v1/leaderboard/me": "LEADERBOARD_ME_RESPONSES",
    ("GET", "/v1/leaderboard/identity"): "LEADERBOARD_IDENTITY_RESPONSES",
    ("POST", "/v1/leaderboard/identity"): "LEADERBOARD_IDENTITY_SET_RESPONSES",
    "/v1/radar/runs": "RADAR_RESPONSES",
    "/v1/radar/runs/{job_id}": "RADAR_JOB_RESPONSES",
    "/healthz": "HEALTH_RESPONSES",                    # empty on purpose; the comment in app.py says why
    "/readyz": "READYZ_RESPONSES",
    "/v1/markets": "LIST_RESPONSES",
    "/v1/markets/{market_id}": "MARKET_RESPONSES",
    "/v1/markets/{market_id}/book": "BOOK_RESPONSES",
    "/v1/markets/{market_id}/fills": "FILLS_RESPONSES",
    "/v1/markets/{market_id}/history": "HISTORY_RESPONSES",
    "/v1/markets/{market_id}/holders": "HOLDERS_RESPONSES",
    "/v1/events/{event_id}": "EVENT_RESPONSES",
    "/v1/tape": "TAPE_RESPONSES",
    "/v1/orders": "ORDER_RESPONSES",
    "/v1/orders/intents/{intent_id}": "INTENT_RESPONSES",
    "/v1/admin/kill-switch": "KILL_RESPONSES",
    # P07 · the security plane. Mapped here as well as in the yaml, because a new route that is *not* in this
    # table is silently skipped by the comparison below — the checker's own version of an undocumented endpoint.
    "/v1/auth/login": "AUTH_RESPONSES",
    "/v1/auth/refresh": "AUTH_RESPONSES",
    "/v1/auth/logout": "SESSIONS_RESPONSES",
    "/v1/auth/sessions": "SESSIONS_RESPONSES",
    "/v1/auth/sessions/revoke": "SESSIONS_RESPONSES",
    "/v1/auth/telegram": "AUTH_RESPONSES",
    "/v1/auth/totp/enroll": "AUTH_RESPONSES",
    "/v1/auth/totp/verify": "AUTH_RESPONSES",
    "/v1/wallet/withdrawal-addresses": "SESSIONS_RESPONSES",
    "/v1/wallet/withdrawal-addresses/add": "ADDRESS_RESPONSES",
    "/v1/wallet/withdrawal-addresses/remove": "ADDRESS_RESPONSES",
    "/v1/admin/revoke-sessions": "BREAK_GLASS_RESPONSES",
    # P10 · the terminal. Every one of these is listed even where two verbs share a path, because a path that
    # is not in this table is a path the status comparison silently skips.
    "/v1/tape/fills": "TAPE_FILLS_RESPONSES",
    "/v1/tape/facets": "FACETS_RESPONSES",
    "/v1/whales": "WHALES_RESPONSES",
    "/v1/traders/{anon}": "TRADER_RESPONSES",
    "/v1/copy/configs": "COPY_CREATE_RESPONSES",
    "/v1/copy/configs/guards": "COPY_GUARD_RESPONSES",
    "/v1/copy/configs/monitor": "COPY_MONITOR_RESPONSES",
    "/v1/copy/sources": "COPY_SOURCES_RESPONSES",
    "/v1/me/portfolio": "PORTFOLIO_RESPONSES",
    "/v1/whale-views": "WHALE_VIEW_RESPONSES",
    # P11 D5. Seven operations, five tables: `/review` serves the queue and the decision, and the two have
    # different status sets (only the write takes an Idempotency-Key and only the write can 404 an item), so
    # both rows are verb-keyed.
    "/v1/referrals/terms": "REFERRAL_TERMS_RESPONSES",
    "/v1/referrals/me": "REFERRAL_ME_RESPONSES",
    "/v1/referrals/code": "REFERRAL_CODE_RESPONSES",
    "/v1/referrals/apply": "REFERRAL_APPLY_RESPONSES",
    "/v1/referrals/accrue": "REFERRAL_ACCRUE_RESPONSES",
    ("GET", "/v1/referrals/review"): "REFERRAL_REVIEW_RESPONSES",
    ("POST", "/v1/referrals/review"): "REFERRAL_REVIEW_SET_RESPONSES",
    # D6: the public pages. The trades that make a platform findable are the ones it does not require a session
    # for, and they are the same six routes a crawler and a scraper both find — hence a table each.
    "/v1/public/trader/{handle}": "PUBLIC_TRADER_RESPONSES",
    "/v1/public/market/{slug}": "PUBLIC_MARKET_RESPONSES",
    "/v1/public/leaderboard/{board}": "PUBLIC_BOARD_RESPONSES",
    "/v1/public/sitemap": "PUBLIC_SITEMAP_RESPONSES",
    ("GET", "/v1/public/blocks"): "PUBLIC_BLOCK_LIST_RESPONSES",
    ("POST", "/v1/public/blocks"): "PUBLIC_BLOCK_RESPONSES",
    # D7. Both verbs are on their own path, so neither row needs a verb key - but the checker is told about them
    # because an unmapped operation is one it would otherwise skip silently.
    "/v1/admin/gaming": "GAMING_RESPONSES",
    "/v1/admin/gaming/decide": "GAMING_DECIDE_RESPONSES",
    # A path with a read and a write has TWO tables, and they differ by exactly the rows that describe the
    # difference: only the write takes an Idempotency-Key, so only the write answers its 400. Keyed by
    # (verb, path) and looked up before the bare path, because a single name per path would force one of the
    # two to lie about a status it never returns.
    ("GET", "/v1/copy/configs"): "COPY_LIST_RESPONSES",
    ("GET", "/v1/whale-views"): "WHALE_VIEW_LIST_RESPONSES",
}


class Report:
    """Reports BOTH states, with counts. A checker that is silent on success is indistinguishable from one
    that never ran; this repo has already paid for that lesson twice."""

    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[str] = []

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def check(self, label: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
        else:
            self.failures.append("%s — %s" % (label, detail or "(no detail given)"))


# ---------------------------------------------------------------- contract side
def contract_ops(doc: dict) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for path, item in (doc.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for verb in ("get", "post", "put", "patch", "delete"):
            op = item.get(verb)
            if isinstance(op, dict):
                out[(verb.upper(), path)] = op
    return out


def contract_statuses(op: dict) -> set[int]:
    return {int(k) for k in (op.get("responses") or {}) if str(k).isdigit()}


# ---------------------------------------------------------------- implementation side
def app_tables() -> dict[str, set[int]]:
    """The per-route `responses=` tables in app.py, evaluated from their AST.

    Not taken from the live app object: the point is to catch "the table says 500 but the yaml does not", and
    reading the literal finds that even for a route no test hits. ORDER_RESPONSES is a comprehension over
    CODES, so that one table is evaluated by running the comprehension in a namespace holding CODES — the
    alternative is trusting the comment that says it is derived.
    """
    tree = ast.parse(APP.read_text())
    consts: dict[str, object] = {}
    literal_dicts: dict[str, ast.Dict] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            literal_dicts[node.targets[0].id] = node.value
            try:
                consts[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
    internal = consts.get("_INTERNAL", {}) or {}
    codes = consts.get("CODES") or {}

    def from_codes(node: ast.Dict) -> set | None:
        """Evaluate a table written as `{202: {...}, **{status: {...} for … in CODES.values()}}`.

        The general form of what used to be a special case for ORDER_RESPONSES: when the Mini App's order route
        gained a table derived the same way, the special case would have read it as "no statuses at all" and
        reported a difference the code does not have. Evaluating the comprehension against the *literal* CODES
        keeps the check honest — it re-derives the statuses from the vocabulary rather than trusting a comment,
        and it works for any route that decides to inherit the whole error vocabulary.
        """
        out: dict = {}
        derived = False
        for key, value in zip(node.keys, node.values):
            if key is not None:
                try:
                    out[ast.literal_eval(key)] = ast.literal_eval(value)
                except (ValueError, TypeError):
                    return None
                continue
            derived = True
            if not codes:
                return None
            scope = {"CODES": codes, "set": set, "dict": dict}
            try:
                out.update(eval(compile(ast.Expression(value), "<table>", "eval"), scope, {}))   # noqa: S307
            except Exception:
                return None
        return set(out) if derived else None

    for name, node in literal_dicts.items():
        if name.endswith("_RESPONSES") and isinstance(consts.get(name), dict) and name != "ORDER_RESPONSES":
            # app.py merges _INTERNAL into every non-order table in a loop; reflect that here rather than reading
            # only the literal, or the comparison would report a difference the code does not have.
            consts[name] = {**consts[name], **internal}
        if name.endswith("_RESPONSES") and from_codes(node):
            consts[name] = from_codes(node)
    derived = {202: {}}
    derived.update({status: {} for _m, status, _r in codes.values()})   # type: ignore[union-attr]
    derived.update(internal)
    consts["ORDER_RESPONSES"] = set(derived)
    out: dict[str, set[int]] = {}
    for name, val in consts.items():
        if name.endswith("_RESPONSES"):
            out[name] = set(val) if not isinstance(val, set) else val
    if codes:
        out["CODES"] = set(codes.keys())           # type: ignore[union-attr]
    return out


# ---------------------------------------------------------------- the checks
def contract_rules(rep: Report, doc: dict, ops: dict) -> None:
    """Rules stated in the contract's own header, enforced on the contract. Each of these fired at least once
    while the yaml was being written, which is why they are code and not prose."""
    rep.check("openapi 3.1.x", str(doc.get("openapi", "")).startswith("3.1"), repr(doc.get("openapi")))
    rep.check("at least 6 paths documented", len(doc.get("paths") or {}) >= 6, str(len(doc.get("paths") or {})))

    def name_of(pp: dict) -> str:
        ref = pp.get("$ref") or ""
        if ref.startswith("#/components/parameters/"):
            return ((doc.get("components") or {}).get("parameters") or {}).get(
                ref.rsplit("/", 1)[-1], {}).get("name", "")
        return pp.get("name", "")

    no_key = ["%s %s" % (v, p) for (v, p), op in ops.items()
              if v in ("POST", "PUT", "PATCH", "DELETE")
              and not any(name_of(pp) == "Idempotency-Key" for pp in (op.get("parameters") or []))
              and not str(op.get("x-idempotency-exempt", "")).strip()]
    rep.check("every mutation declares Idempotency-Key, or says why not", not no_key, "; ".join(no_key))
    weak = ["%s %s" % (v, p) for (v, p), op in ops.items()
            if "IdempotencyKey" not in str(op.get("parameters") or [])
            and 0 < len(str(op.get("x-idempotency-exempt", "")).strip()) < 20]
    rep.check("every exemption carries a real reason (>=20 chars)", not weak, "; ".join(weak))

    numbers = [ln.strip() for ln in CONTRACT.read_text().splitlines()
               if re.match(r"^\s*(price|size|usdc\w*|amount|notional(?!Micro)\w*)\s*:", ln)
               and re.search(r"type:\s*(number|integer)\b", ln)]
    rep.check("prices and sizes are strings, never JSON numbers", not numbers, "; ".join(numbers[:3]))

    unstamped = [p for (v, p), op in ops.items() if v == "GET" and "/v1/" in p
                 and "Stamped" not in str((op.get("responses") or {}).get("200") or {})
                 and "staleAfter" not in str(op.get("responses") or {})]
    rep.check("every GET /v1/* carries asOf/staleAfter", not unstamped, "; ".join(unstamped))

    cacheable = ["%s %s" % (v, p) for (v, p), op in ops.items()
                 if v in ("POST", "PUT", "PATCH", "DELETE") and "no-store" not in str(op.get("x-cache", ""))]
    rep.check("no mutating response is cacheable", not cacheable, "; ".join(cacheable))

    sparse = ["%s %s" % (v, p) for (v, p), op in ops.items()
              if not op.get("operationId") or "x-auth" not in op or "x-rate-class" not in op]
    rep.check("every operation has operationId + x-auth + x-rate-class", not sparse, "; ".join(sparse))

    schemas = ((doc.get("components") or {}).get("schemas") or {})
    err_props = ((((schemas.get("Error") or {}).get("properties") or {}).get("error") or {})
                 .get("properties") or {})
    enum = set((err_props.get("code") or {}).get("enum") or [])
    produced = set(app_tables().get("CODES", set()))
    produced |= set(re.findall(r'code="([A-Z][A-Z_]{3,24})"', GATE.read_text()))
    rep.check("every code the app/gate can return is in the enum", not produced - enum,
              "; ".join(sorted(produced - enum)))
    rep.check("no enum code that nothing in the product returns", not enum - produced,
              "; ".join(sorted(enum - produced)))
    rep.check("the envelope has a code enum and no `detail` field", bool(enum) and "detail" not in
              str(err_props), "enum size %d" % len(enum))


def compare_tables(rep: Report, ops: dict, tables: dict) -> None:
    for (verb, path), op in sorted(ops.items(), key=lambda kv: str(kv[0])):
        tname = TABLE_FOR_PATH.get((verb, path)) or TABLE_FOR_PATH.get(path)
        rep.check("%s %s is mapped to a table in app.py" % (verb, path), tname is not None,
                  "no TABLE_FOR_PATH entry — the checker would silently skip this operation")
        if tname is None:
            continue
        declared = set(tables.get(tname, set()))
        # A route with no explicit 2xx answers 200 (FastAPI's default, true of every read here). The order
        # route declares 202, so it must NOT gain a 200 — "200 or 202, whichever" is precisely the
        # ambiguity this phase's bug turned into a contract lie.
        if not any(200 <= c < 300 for c in declared):
            declared.add(200)
        want = contract_statuses(op)
        rep.check("%s %s: yaml and app declare the same status set" % (verb, path), want == declared,
                  "yaml %s vs app %s" % (sorted(want), sorted(declared)))
        rep.check("%s %s: the yaml documents no status the app cannot return" % (verb, path),
                  not want - declared, "extra in yaml: %s" % sorted(want - declared))

    # A path that serves both a read and a write must not answer the write's refusals on its read. The two
    # P10 tables differ by exactly {400}: the key-required answer belongs to the verb that takes the header.
    for verb_pair in (("GET", "/v1/copy/configs", "COPY_LIST_RESPONSES"),
                      ("GET", "/v1/whale-views", "WHALE_VIEW_LIST_RESPONSES")):
        read_verb, path, read_table = verb_pair
        write_path = TABLE_FOR_PATH.get(path)
        read = set(tables.get(read_table, set())) | {200}
        write = set(tables.get(write_path, set())) | {200}
        rep.check("%s %s is its write table minus the Idempotency-Key answer" % (read_verb, path),
                  400 in write and write - {400} == read, "read %s vs write %s" % (sorted(read), sorted(write)))


def compare_live(rep: Report, doc: dict, ops: dict) -> None:
    """Boot the real app and compare its routes. A throwaway DB path, so auditing the contract can never
    touch a developer's dev database."""
    db = "/tmp/pgm-openapi-check-%d.db" % os.getpid()
    os.environ["PGM_DB_PATH"] = db
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except FileNotFoundError:
            pass
    sys.path[:0] = [str(ROOT / "packages"), str(ROOT / "services" / "api")]
    # The API refuses to start without a schema (it does not migrate at import — see app.py), so the checker
    # runs the same migrator a developer runs. That is a feature of this audit: if `make migrate` ever
    # produces a database the API will not boot against, this check goes red rather than the demo.
    import importlib.util
    spec = importlib.util.spec_from_file_location("pgm_run_sql", ROOT / "tools" / "run-sql.py")
    migrator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migrator)
    with contextlib.redirect_stdout(io.StringIO()):
        rc = migrator.run_sqlite(ROOT / "db" / "migrations-sqlite", db)
    if rc != 0:
        rep.fail("the portable migrations apply to a fresh database — run-sql.py exited %d" % rc)
        return

    # The app logs one JSON line per request to stdout; capturing it keeps `make openapi` to the shape of
    # an audit report. It is captured, not silenced by a flag, so the logging behaviour itself stays real.
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            import app as impl
    except Exception as e:  # noqa: BLE001 — an app that will not boot IS the finding, not a skipped check
        rep.fail("app imports — %s: %s" % (type(e).__name__, e))
        return
    live: dict[str, set[str]] = {}
    for route in impl.app.routes:
        methods = getattr(route, "methods", None)
        if methods:
            live.setdefault(route.path, set()).update(methods)
    for (verb, path) in ops:
        rep.check("%s %s exists in the app" % (verb, path), verb in live.get(path, set()),
                  "app paths: %s" % sorted(live))
    documented = {p for (_v, p) in ops}
    extra = {p for p in live if p not in documented and p not in BUILTIN_DOCS}
    rep.check("every app route is documented", not extra, "; ".join(sorted(extra)))

    param_defs = ((doc.get("components") or {}).get("parameters") or {})

    def resolve(pp: dict) -> dict:
        ref = pp.get("$ref") or ""
        if ref.startswith("#/components/parameters/"):
            return param_defs.get(ref.rsplit("/", 1)[-1], {}) or {}
        return pp

    wanted: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
    for (verb, path), op in ops.items():
        names = sorted({resolve(pp).get("name", "") for pp in (op.get("parameters") or [])
                        if resolve(pp).get("name")})
        body = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {})
        wanted[(verb.upper(), path)] = (names, sorted(((body.get("schema") or {}).get("required") or [])))

    derived = impl.app.openapi()
    for path, item in (derived.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for verb, op in item.items():
            if not isinstance(op, dict) or (verb.upper(), path) not in wanted:
                continue
            want_names, want_req = wanted[(verb.upper(), path)]
            got = sorted({q.get("name", "") for q in (op.get("parameters") or []) if isinstance(q, dict)})
            rep.check("%s %s: every documented parameter exists in the served spec" % (verb.upper(), path),
                      all(w in got for w in want_names), "yaml %s vs served %s" % (want_names, got))
            if not want_req:
                continue
            schema = ((((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {})
                      .get("schema") or {})
            got_req = sorted(schema.get("required") or [])
            rep.check("%s %s: the required body fields match" % (verb.upper(), path), want_req == got_req,
                      "yaml %s vs served %s" % (want_req, got_req))
            props = set((schema.get("properties") or {}))
            rep.check("%s %s: every required body field is typed in the served schema" % (verb.upper(), path),
                      all(w in props for w in want_req), "served props %s" % sorted(props))

    orders_doc = (((derived.get("paths") or {}).get("/v1/orders") or {}).get("post") or {})
    served = {int(k) for k in (orders_doc.get("responses") or {}) if str(k).isdigit()}
    rep.check("the served /openapi.json advertises 202 for the order endpoint", 202 in served, str(sorted(served)))
    rep.check("the served /openapi.json does not ALSO advertise 200 there", 200 not in served,
              str(sorted(served)))
    # ---- the SERVED document must agree with the contract on every schema they share -----------------
    # Why this exists: the served document is the artifact a client generator consumes, and this app INJECTS
    # two schemas into it at generation time (`Error`, `Price`) to keep the document complete. An injected
    # schema is exactly where a divergence hides: the contract can say "money is a decimal string" while the
    # document the SDK reads says `type: number`, and every other check in this file still passes. A mutation
    # harness (tools/p04-mutation-test.py) caught this hole by setting that one word to `number`.
    c_schemas = ((doc.get("components") or {}).get("schemas") or {})
    s_schemas = ((derived.get("components") or {}).get("schemas") or {})
    rep.check("the served document declares the two schemas it owns (%s)"
              % "/".join(n for n in ("Price", "Error") if n in s_schemas),
              all(n in s_schemas for n in ("Price", "Error")),
              "served declares: %s" % sorted(s_schemas))
    disagree = schema_agreement(c_schemas, s_schemas)
    shared = sorted(set(c_schemas) & set(s_schemas))
    rep.check("served and contract agree on type/pattern/format for every shared schema (%d shared)"
              % len(shared), not disagree, "; ".join(disagree[:4]))
    rep.check("the served money field is a string in the document a client generator will read",
              (s_schemas.get("Price") or {}).get("type") == "string"
              and (s_schemas.get("Price") or {}).get("pattern"),
              str(s_schemas.get("Price")))

    # 422 bodies: the status is the easy half. FastAPI attaches its own HTTPValidationError (which echoes the
    # client's `input`) to every operation with a validated parameter, so the SERVED document can promise a
    # body the handler never sends. Both directions are checked: the ref, and the absence of the dead schema.
    for path, item in (derived.get("paths") or {}).items():
        for verb, op in (item or {}).items():
            if not isinstance(op, dict) or "422" not in (op.get("responses") or {}):
                continue
            resp = op["responses"]["422"]
            ref = str(((resp.get("content") or {}).get("application/json") or {}).get("schema") or {})
            rep.check("%s %s: 422 body is the Error envelope, not FastAPI's" % (verb.upper(), path),
                      "#/components/schemas/Error" in ref, ref[:120])
    schemas = ((derived.get("components") or {}).get("schemas") or {})
    blob = json.dumps(derived)
    ref_problems = unresolved_refs(derived) + unresolved_refs(doc)
    rep.check("every $ref in both documents resolves to a defined component", not ref_problems,
              "; ".join(sorted(set(ref_problems))[:6]))
    for dead in ("HTTPValidationError", "ValidationError"):
        rep.check("the served spec does not advertise %s (it echoes client input)" % dead,
                  "#/components/schemas/%s" % dead not in blob, "still referenced")
    # the sort enum: an aspirational value is a contract the app 422s on. Checked by asking the live app.
    from fastapi.testclient import TestClient
    with contextlib.redirect_stdout(io.StringIO()):
        client = TestClient(impl.app, raise_server_exceptions=False)
        enum = []
        markets_get = (((doc.get("paths") or {}).get("/v1/markets") or {}).get("get") or {})
        for pp in (markets_get.get("parameters") or []):
            if isinstance(pp, dict) and pp.get("name") == "sortBy":
                enum = list(((pp.get("schema") or {}).get("enum")) or [])
        rep.check("the contract lists at least one sort key", bool(enum), str(enum))
        for value in enum:
            r = client.get("/v1/markets", params={"sortBy": value})
            rep.check("sortBy=%s is accepted by the live app" % value, r.status_code == 200,
                      "live app answered %d: the contract documents a sort the app rejects" % r.status_code)

    # The negative probe is DERIVED, not spelled. It used to be the literal "volume24h", which the contract
    # then legitimately gained - so the check flipped from "an undocumented sort is refused" to "the app
    # refuses a documented one", and the only reason it was noticed at all is that this repo runs the checker
    # against the app rather than trusting it. Suffixing the longest documented key produces a value that
    # cannot collide by accident, and the `probe not in enum` assertion below makes the day it does collide a
    # visible failure instead of a silently inverted control.
    probe = (max(enum, key=len) + "-nope") if enum else "volume24h"
    rep.check("the negative probe is not itself a documented sort key", probe not in enum, probe)
    with contextlib.redirect_stdout(io.StringIO()):
        r = client.get("/v1/markets", params={"sortBy": probe})
    rep.check("a sort the contract does NOT claim is rejected", r.status_code == 422, "got %d" % r.status_code)

    # The throwaway database is removed HERE, after the live probes and not straight after the import. It used to
    # be removed before them, which was invisible for as long as the app held one shared connection open: POSIX
    # keeps the inode alive for an open handle, so every probe read the migrated-but-unlinked file and passed.
    # The day a connection is opened *per thread* — which is what P11's D6 sweep had to do, because one shared
    # `sqlite3` connection across uvicorn's thread pool raises `InterfaceError` under load — the removal turned
    # seven live probes into `no such table: markets` 500s, because `sqlite3.connect` cheerfully creates an empty
    # database at a path it cannot find. A cleanup that only works while the thing it cleans is still open is not
    # a cleanup; it is an accident with a comment on it.
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except OSError:
            pass


def schema_agreement(contract: dict, served: dict) -> list[str]:
    """Every schema the two documents share must say the same thing about it.

    Only keys the CONTRACT declares are compared, so a served document that adds `description`, `title` or a
    FastAPI default is not a finding — but a contract that promises `pattern` and gets none, or promises
    `string` and gets `number`, is. The function is pure so `--self-test` can prove it fires on a disagreement
    it never saw in the real tree.
    """
    problems: list[str] = []
    for name, cs in (contract or {}).items():
        ss = (served or {}).get(name)
        if ss is None or not isinstance(cs, dict) or not isinstance(ss, dict):
            continue
        for key in ("type", "pattern", "format", "maxLength", "minimum", "maximum"):
            if key in cs and ss.get(key) != cs[key]:
                problems.append("%s.%s: contract=%r served=%r" % (name, key, cs[key], ss.get(key)))
    return problems


def unresolved_refs(spec: dict) -> list[str]:
    """Every `$ref` in a document must point at something the document defines.

    This is the failure mode that bites a client generator hardest and that no reader of the yaml catches:
    a component renamed on one side only. It reads fine, `yaml.safe_load` is happy, and the generator dies.
    """
    comps: set[str] = set()
    for group in ("schemas", "responses", "parameters", "headers", "examples", "requestBodies"):
        comps |= set((((spec.get("components") or {}).get(group) or {}) or {}).keys())

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "$ref" and isinstance(v, str):
                    yield v
                else:
                    yield from walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v)

    out = []
    for ref in walk(spec):
        tail = ref.rsplit("/", 1)[-1]
        if tail not in comps:
            out.append("%s (components define %d names)" % (ref, len(comps)))
    return sorted(set(out))


def strict_yaml_defects(text: str) -> list[str]:
    """Defects a permissive loader cannot see, read off the raw node graph.

    PyYAML accepts a duplicated mapping key and silently keeps the last one, so every comparison in this file
    reads a document in which the earlier key never existed. That is how a `content:` block naming a media
    type with nothing under it survived the 176-row agreement check while `openapi-typescript` — which parses
    strictly — refused to generate client types from the same file. A contract a code generator cannot read is
    not a contract, it is a note to self.
    """
    defects: list[str] = []

    def walk(node):
        if isinstance(node, yaml.MappingNode):
            seen: dict[str, int] = {}
            for key, value in node.value:
                if not isinstance(key, yaml.ScalarNode):
                    continue
                name = str(key.value)
                if name in seen:
                    defects.append("duplicate key %r at line %d (first at line %d)"
                                   % (name, key.start_mark.line + 1, seen[name]))
                else:
                    seen[name] = key.start_mark.line + 1
                if name == "content" and isinstance(value, yaml.MappingNode):
                    for mkey, mval in value.value:
                        # A key with nothing under it parses as an empty scalar with the null tag, not as
                        # Python None — the first version tested for None and the canary stayed silent.
                        if not isinstance(mval, yaml.MappingNode):
                            defects.append("media type %r declares no schema (line %d)"
                                           % (mkey.value, mkey.start_mark.line + 1))
                        walk(mval)
                else:
                    walk(value)
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                walk(item)

    walk(yaml.compose(text, Loader=yaml.SafeLoader))
    return defects


def self_test(tables: dict, ops: dict) -> Report:
    """Prove the comparisons can fail. Each case plants one disagreement and requires a failure; without
    this, "0 failures" could equally mean "the checker lost the ability to fail"."""
    rep = Report()
    disagree = lambda a, b: a != b  # noqa: E731 - the one function under test, inlined on purpose
    cases = {
        "a 200-instead-of-202 success status is caught": disagree({200}, {202}),
        "an undocumented 500 is caught": disagree({200, 404}, {200, 404, 500}),
        "a renamed status is caught": disagree({202, 409}, {202, 410}),
        "an identical set is NOT reported": not disagree({202, 400}, {202, 400}),
        "path templates are compared literally": all(
            t == w for t, w in (("/v1/markets/{market_id}", "/v1/markets/{market_id}"),
                               ("/v1/orders/intents/{intent_id}", "/v1/orders/intents/{intent_id}"))),
        "a snake/camel mismatch would be a difference": "/x/{a_b}" != "/x/{aB}",
        "a dangling $ref is caught": bool(unresolved_refs({"components": {"schemas": {}}, "paths": {"/x": {
            "get": {"responses": {"200": {"content": {"application/json": {"schema": {
                "$ref": "#/components/schemas/Nope"}}}}}}}}})),
        "a type disagreement between the two documents is caught": bool(schema_agreement(
            {"Price": {"type": "string", "pattern": "^x$"}}, {"Price": {"type": "number", "pattern": "^x$"}})),
        "a description-only difference is NOT a failure": not schema_agreement(
            {"Error": {"type": "object", "description": "a"}},
            {"Error": {"type": "object", "description": "b", "title": "Error"}}),
        "a resolvable $ref is NOT a failure": not unresolved_refs({"components": {"schemas": {"Yes": {}}},
                                                                   "paths": {"/x": {"get": {"responses": {
                                                                       "200": {"content": {"application/json": {
                                                                           "schema": {"$ref": "#/components/schemas/Yes"}}}}}}}}}),
        "a duplicate mapping key is caught": bool(strict_yaml_defects("a:\n  b: 1\n  b: 2\n")),
        "a media type with no schema is caught": bool(strict_yaml_defects(
            'responses:\n  "200":\n    content:\n      application/json:\n')),
        "a well-formed document is NOT reported": not strict_yaml_defects(
            'paths:\n  /x:\n    get:\n      responses:\n        "200":\n          content:\n            application/json:\n              schema: {type: object}\n'),
        "the app's response tables were found at all": len(tables) >= 9,
        "every documented operation maps to a table": all(p in TABLE_FOR_PATH for (_v, p) in ops),
        "the order table is non-trivial": len(tables.get("ORDER_RESPONSES", set())) >= 8,
    }
    for label, cond in cases.items():
        rep.check("self-test: " + label, bool(cond), "the comparison is decoration")
    return rep


def finish(rep: Report) -> int:
    print("check-openapi: %d passed, %d failed" % (rep.passed, len(rep.failures)))
    for f in rep.failures:
        print("  FAIL", f)
    return 1 if rep.failures else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true",
                    help="prove the comparisons fire when two artifacts disagree")
    a = ap.parse_args()
    if not CONTRACT.is_file():
        print("missing contract:", CONTRACT)
        return 2
    try:
        doc = yaml.safe_load(CONTRACT.read_text())
    except yaml.YAMLError as e:
        print("contract does not parse:", e)
        return 2
    if not isinstance(doc, dict):
        print("contract is not a mapping")
        return 2
    ops, tables = contract_ops(doc), app_tables()
    strict = strict_yaml_defects(CONTRACT.read_text())
    if a.self_test:
        return finish(self_test(tables, ops))
    rep = Report()
    rep.check("contract parses", True)
    # The strict pass is checked here rather than in `contract_rules` because it is the *generator's* view of
    # the file: the day the contract stops being machine-portable, the frontend's typed client is the thing
    # that breaks, and it breaks at build time in another directory if we do not catch it here.
    rep.check("contract is machine-portable (no duplicate keys, no media type without a schema)", not strict,
              "; ".join(strict) or "node graph clean; a permissive loader would not have seen these")
    contract_rules(rep, doc, ops)
    compare_tables(rep, ops, tables)
    compare_live(rep, doc, ops)
    return finish(rep)


if __name__ == "__main__":
    sys.exit(main())
