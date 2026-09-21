#!/usr/bin/env python3
"""P14 D1 — the authorisation matrix, run against the product with production's identity rules.

    python3 tools/p14-authz-matrix.py --record docs/verification/P14-authz-matrix.txt --json …json
    python3 tools/p14-authz-matrix.py --only stranger,escalation     # one section at a time while fixing

The kit puts authorisation first for a reason it states plainly: it is *the most common catastrophic bug in this
product class*. So this tool is not a reading of the P07 registry — that registry already exists and
`tools/p07-gate-check.py` audits it. This is the other half: **behaviour, per route, with two real accounts.**

Four rules, and the two directions both matter:

  * **A 2xx is only allowed where the registry says `public`.** Anonymous callers hitting a `user` or `admin`
    operation must be refused — and the tool does not accept "refused" as a feeling: it reads the status code.
  * **Own access must work.** Every `user` operation called with its rightful owner's session must not be a 401.
    Without this half, a product that refuses *everything* would score perfectly on the first half. (This is the
    must-accept direction that a matrix populated only with denials always misses.)
  * **A stranger gets no needles.** Account A calls every user operation while carrying B's identifiers — B's
    order intent, B's deposit, B's job — and the response is scanned for B's private tokens (user id, wallet and
    proxy address, email, held ids, referral code). Section D runs the same scan for B's *own* calls first: a
    probe where B's own response surfaces no needle proves nothing, and it is reported as inconclusive rather
    than as a pass. That sensitivity control is the difference between an audit and a ritual.
  * **Escalation is checked in both token states.** A user session on an admin operation must be refused; an
    admin operation with *no* admin token configured must answer 503 (a missing control is a misconfiguration,
    not an attack), and with a wrong token 403.

The target is the **production identity shape**: the harness sets `PGM_REQUIRE_SECURITY_ENV=1`, which is what
turns `X-User-Id` into a development-only convenience and makes bearer sessions the only way in. A matrix run
without that flag would be testing the dev door and calling it the front door.

Everything runs in-process against a throwaway migrated+seeded SQLite database. No network, no real keys, and
no route here reaches the venue: the executor is not running and the probes never place an order at one.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import hmac
import io
import json
import os
import pathlib
import re
import sqlite3
import struct
import sys
import tempfile
import time
import uuid
from importlib.util import module_from_spec, spec_from_file_location

ROOT = pathlib.Path(__file__).resolve().parents[1]
VERIF = ROOT / "docs" / "verification"

#: The kit's severity for a finding, by what it would cost. The tool labels each finding so the report is
#: triaged by expected loss rather than by the order they happened to be printed in.
SEV = {
    "cross_user_read": "Critical — a user reads another user's money data",
    "cross_user_write": "Critical — a user acts on another user's account",
    "anonymous_2xx": "High — an operation is reachable with no credentials at all",
    "escalation": "Critical — a user reaches an admin operation",
    "undeclared": "High — an operation exists with no authorisation decision",
    "docs_exposed": "Low — interactive API documentation is served on the API origin",
    "drift_row": "Low — the registry declares an operation that is neither served nor on the promise list",
}


def totp(secret_b32: str, at_ms: int | None = None, *, digits: int = 6, step_s: int = 30) -> str:
    """RFC 6238, six digits, HMAC-SHA1 — the code an authenticator app would show.

    A probe account that never enrolled 2FA is refused for the wrong reason: `TOTP_REQUIRED` says "you have no
    second factor", not "that is not your address". Every attack that reaches a 2FA-gated endpoint therefore
    enrols a real factor on A first, so the only thing left standing between A and B's money is the ownership
    check. This helper is checked against the RFC's own published vectors at the start of every run.
    """
    key = base64.b32decode(secret_b32.strip().upper() + "=" * (-len(secret_b32.strip()) % 8))
    counter = int((at_ms if at_ms is not None else time.time() * 1000) / 1000 / step_s)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    return str((struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)).zfill(digits)


def _load(name: str, path: pathlib.Path):
    spec = spec_from_file_location(name, path)
    mod = module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class Api:
    """The product, booted in-process against a throwaway database, in the production identity shape."""

    def __init__(self) -> None:
        # Environment first, before anything imports `polygm_core.config`: that module builds the flags object
        # once per process, so a variable set after it has been imported — even by a helper the migration run
        # happens to touch — is a variable nobody reads. The tape window took a second run to notice for exactly
        # this reason, and both runs agreed the orders were `STALE_QUOTE`, which is true and useless.
        os.environ.setdefault("PGM_KEK_VERSION", "1")
        os.environ.setdefault("PGM_KEK_v1", base64.b64encode(bytes(range(32, 64))).decode())
        os.environ.setdefault("PGM_IP_PEPPER", base64.b64encode(bytes(range(64, 96))).decode())
        os.environ.setdefault("PGM_SERVICE_TOKEN", "p14-service-%s" % uuid.uuid4().hex)
        os.environ.setdefault("PGM_IMAGE_PROXY_SECRET", base64.b64encode(bytes(range(96, 128))).decode())
        # A throwaway bot token: without one every initData probe answers 503 ("not set on this pod"), which is a
        # different statement from "the signature was refused". The secret is the harness's own, so the only
        # signature that verifies is one this tool minted — which is the point of the forgery probes below.
        # The token format is the module's own (`<bot_id>:<secret>`), and the id has to parse as an integer:
        # Telegram's check string is built from it, so a token that cannot be split is refused by the signer
        # before any probe runs.
        os.environ.setdefault("PGM_TELEGRAM_BOT_TOKEN", "8123456789:p14%s" % ("b" * 32))
        # The tape's freshness window is three seconds in production, which is right for the product and wrong for
        # this tool: a run takes twenty seconds, so by the time the risk gate is probed every order answers
        # `STALE_QUOTE` and the limit checks would be reading a different control's refusal. Freshness has its own
        # drill (P07, and D6 here); this harness widens the window so that the control under test is the one that
        # answers.
        os.environ.setdefault("PGM_STALE_MS_TAPE", str(3_600_000))
        self.bot_token = os.environ["PGM_TELEGRAM_BOT_TOKEN"]
        self.service_token = os.environ["PGM_SERVICE_TOKEN"]
        # The production shape: `X-User-Id` is not an identity, a bearer session is.
        os.environ["PGM_REQUIRE_SECURITY_ENV"] = "1"
        os.environ["PGM_LOG_FORMAT"] = "json"
        os.environ.pop("PGM_TRUST_USER_HEADER", None)
        os.environ.pop("PGM_ADMIN_TOKEN", None)
        # The service paths go on `sys.path` FIRST: `seed` and `app` are both imported by name below, and a
        # migration that runs before they are importable is a harness that dies at the first line that matters.
        sys.path[:0] = [str(ROOT / "packages"), str(ROOT / "services" / "api"), str(ROOT / "tools")]
        run_sql = _load("pgm_run_sql", ROOT / "tools" / "run-sql.py")
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="p14-authz-"))
        self.db = self.tmp / "authz.db"
        os.environ["PGM_DB_PATH"] = str(self.db)
        with contextlib.redirect_stdout(io.StringIO()):
            if run_sql.run_sqlite(ROOT / "db" / "migrations-sqlite", str(self.db)) != 0:
                raise SystemExit("p14-authz-matrix: the migration run failed, so there is nothing to probe")
            import seed
            seed.seed_sqlite(str(self.db))
        # The security environment a deployed box has. Without it the app boots but every 2FA path answers
        # `SECURITY_ENV_MISSING`, so A could never hold an authenticator and every 2FA-gated probe would be
        # refused for the wrong reason. The values are throwaway and generated here, exactly as
        # `tests/test_security_plane.py` does it — the point is the *shape* of the environment, not the keys.
        self.app = _load("pgm_authz_app", ROOT / "services" / "api" / "app.py")
        from fastapi.testclient import TestClient
        self.client = TestClient(self.app.app, raise_server_exceptions=False)
        self.con = self.app._db
        self.now = int(time.time() * 1000)
        self.pw = "correct horse battery staple 7!"
        self.phc = self.app._hasher().hash(self.pw)

    # ------------------------------------------------------------------ accounts and fixtures
    def account(self, tag: str) -> dict:
        uid = "u_%s_%s" % (tag, uuid.uuid4().hex[:8])
        self.con.execute("INSERT INTO users (id, created_ms, tier) VALUES (?,?, 'trader')", (uid, self.now))
        self.app.SEC.link_identity(uid, "email", "%s@example.test" % uid, at=self.now)
        self.app.SEC.set_password(uid, self.phc, at=self.now)
        tok = self.login(uid)
        return {"uid": uid, "token": tok, "tag": tag}

    def login(self, uid: str) -> str:
        r = self.client.post("/v1/auth/login", json={"identifier": uid, "password": self.pw},
                             headers={"Content-Type": "application/json"})
        if r.status_code != 200:
            raise SystemExit("p14-authz-matrix: could not mint a session for %s: %s %s"
                             % (uid, r.status_code, r.text[:200]))
        return r.json()["accessToken"]

    def enroll_totp(self, account: dict) -> dict:
        """Give A a working second factor: enrol, then verify with a code the harness computes itself."""
        r = self.request_b("POST", "/v1/auth/totp/enroll", account["token"], body={})
        if not 200 <= r.status_code < 300:
            return {"ok": False, "why": "enroll %d %s" % (r.status_code, (r.text or "")[:120])}
        payload = r.json() or {}
        secret = ""
        for key in ("secret", "secretBase32", "otpauthSecret"):
            if isinstance(payload.get(key), str) and len(payload[key]) >= 16:
                secret = payload[key]
        if not secret:
            for v in payload.values():       # the secret may ride inside the otpauth:// URI
                if isinstance(v, str) and "secret=" in v:
                    secret = v.split("secret=", 1)[1].split("&", 1)[0]
        if not secret:
            return {"ok": False, "why": "no secret in %s" % sorted(payload)[:8]}
        code = totp(secret)
        v = self.request_b("POST", "/v1/auth/totp/verify", account["token"], body={"code": code})
        account["totp_secret"] = secret
        return {"ok": 200 <= v.status_code < 300, "secret": secret,
                "why": "" if 200 <= v.status_code < 300 else "verify %d %s" % (v.status_code,
                                                                              (v.text or "")[:120])}

    def fresh_login(self, uid: str) -> str:
        """A new session for an account, minted now.

        The needle scan fires **every** user operation, and two of those operations are `POST /v1/auth/logout`
        and `POST /v1/auth/sessions/revoke`. The first run of this tool therefore logged A out halfway through and
        every later section ran on dead sessions: probes came back 401 and were counted as *refusals*, which is
        the most dangerous shape a security harness can have — a green report produced by the harness breaking
        itself. Non-GET probes now carry a session minted for that probe, and the run ends by asserting both
        sessions are still alive.
        """
        return self.login(uid)

    def bearer(self, token: str) -> dict:
        return {"Authorization": "Bearer %s" % token, "Content-Type": "application/json"}

    def insert(self, table: str, **vals) -> None:
        """Insert one fixture row, reading the schema for what it needs instead of guessing column names.

        The first version of this function hand-wrote four INSERT statements from memory and died on
        `order_intents.order_type` (a column the schema does not have) and then on `deposits.id` (an INTEGER
        PRIMARY KEY, so a text id is a datatype mismatch). A probe harness whose fixtures drift from the schema
        fails for reasons that have nothing to do with authorisation, so the column list comes from `PRAGMA`
        and a missing NOT NULL column is named rather than left to SQLite to complain about.
        """
        cols = {r[1]: r for r in self.con.execute("PRAGMA table_info(%s)" % table).fetchall()}
        if not cols:
            raise SystemExit("p14-authz-matrix: no such table %r" % table)
        names = [c for c in vals if c in cols]
        unknown = [c for c in vals if c not in cols]
        if unknown:
            raise SystemExit("p14-authz-matrix: %s has no column(s) %s" % (table, ", ".join(unknown)))
        missing = [n for n, r in cols.items()
                   if r[3] and n not in names and r[4] is None and not r[5]]     # NOT NULL, no default, not PK
        if missing:
            raise SystemExit("p14-authz-matrix: %s needs %s" % (table, ", ".join(missing)))
        self.con.execute("INSERT INTO %s (%s) VALUES (%s)"
                         % (table, ", ".join(names), ", ".join("?" * len(names))),
                         tuple(vals[c] for c in names))

    def stranger_fixtures(self, b: dict) -> dict:
        """B owns one of everything the API can expose, so a needle scan has real targets and a list endpoint has
        a row that is B's and must never appear in A's answer.

        Every row goes through `insert()`, which reads the schema first; the ids are the needles, so they are
        stable strings a response body would carry verbatim if it leaked.
        """
        uid, out = b["uid"], {"uid": b["uid"]}
        n = self.now
        # B's wallet and balance come from `qualify()` in main; the rows below are the *objects* the probes attack.

        # Seven markets for the async radar job; taken from the book so the ids are ones the app knows.
        out["markets"] = [str(r[0]) for r in self.con.execute(
            "SELECT id FROM markets WHERE accepting_orders=1 ORDER BY id LIMIT 7").fetchall()]
        # The copy/follow sources are pseudonyms the *product* resolved (`wallet_pseudonyms`), not handles this
        # harness invented: `sourceAnon` goes through `_wallet_for_anon`, which refuses anything it has not seen.
        row = self.con.execute("SELECT anon_id FROM wallet_pseudonyms ORDER BY anon_id LIMIT 1").fetchone()
        out["other_anon"] = str(row[0]) if row else ""
        # B's wallet is `qualify()`; these are the strings the needle scan looks for in other people's responses.
        addr, proxy = "0x" + "9" * 40, "0x" + "8" * 40
        #: B's allowlisted destination — 40 hex characters, and the *only* one a withdrawal may name.
        allow = "0x" + "7" * 40

        # Registered as needles the moment they exist. The canary below is what caught their absence: the
        # rewrite of this function inserted the wallet but stopped *recording* its address, so the scan was
        # looking for everything except the one string whose leak matters most.
        out["address"], out["proxy"] = addr, proxy
        mkt = self.con.execute("SELECT id FROM markets WHERE accepting_orders=1 ORDER BY id LIMIT 1").fetchone()
        tok = self.con.execute("SELECT token_id FROM tokens WHERE market_id=? ORDER BY token_id LIMIT 1",
                               (mkt[0],)).fetchone()
        out["market"] = str(mkt[0])
        iid = "int_needle_%s" % uuid.uuid4().hex[:8]
        self.insert("order_intents", id=iid, user_id=uid, market_id=mkt[0], token_id=tok[0], side="BUY",
                    price_micro=500_000, size_micro=10_000_000, notional_micro=5_000_000, state="queued",
                    idempotency_key="needle-idem-b", created_ms=n, updated_ms=n)
        out["intent"] = iid
        oid = "ord_needle_%s" % uuid.uuid4().hex[:8]
        self.insert("orders", id=oid, intent_id=iid, user_id=uid, token_id=tok[0], market_id=mkt[0], side="BUY",
                    price_micro=500_000, size_micro=10_000_000, size_matched_micro=0, state="live",
                    acknowledged=1, builder_code="needle-builder", placed_ms=n, updated_ms=n)
        out["order"] = oid
        out["token_id"] = str(tok[0])
        self.insert("position_lots", user_id=uid, market_id=str(mkt[0]), token_id=str(tok[0]),
                    shares_open_micro=10_000_000, basis_micro=5_000_000, opened_ms=n, source="fill")
        did = 918_273_645
        self.insert("deposits", id=did, user_id=uid, asset="pUSD", chain="polygon", amount_micro=5_000_000,
                    tx_hash="0xneedle", credit_key="needle-credit-b", status="credited", first_seen_ms=n)
        out["deposit"] = str(did)
        rid = "rule_needle_%s" % uuid.uuid4().hex[:8]
        self.insert("signal_rules", id=rid, owner=uid, kind="large_fill", params_json='{"abs_usd_micro":5000000000}',
                    market_filter_json="{}", cooldown_s=300, severity="info", channels_json='["inapp"]',
                    enabled=1, created_ms=n, updated_ms=n)
        out["rule"] = rid
        # `signals.id` is an INTEGER PRIMARY KEY (a rowid alias) exactly as `deposits.id` is, so the fixture lets
        # SQLite assign it and reads the rowid back — the needle is then a number, which is what a leaking
        # response would carry.
        self.insert("signals", rule_id=rid, kind="large_fill", severity="info", title="needle signal",
                    body_json="{}", dedupe_key="needle-dedupe-b", fired_bucket=1, fired_ms=n,
                    condition_id=str(mkt[0]), token_id=str(tok[0]))
        sig = int(self.con.execute("SELECT id FROM signals WHERE rule_id=? ORDER BY id DESC LIMIT 1",
                                   (rid,)).fetchone()[0])
        self.insert("alert_deliveries", signal_id=sig, user_id=uid, channel="inapp", priority=10, queued_ms=n,
                    status="queued")
        out["signal"] = str(sig)
        out["allow"] = allow
        code = "REFNEEDLE%s" % uid[-4:].upper()
        self.insert("referral_links", user_id=uid, code=code, token="tk_needle_b", kind="link", state="active",
                    created_ms=n)
        out["referral_code"] = code
        self.insert("leaderboard_identity", user_id=uid, state="listed", handle="needle_handle", listed_ms=n,
                    updated_ms=n)
        out["handle"] = "needle_handle"
        out["email"] = "%s@example.test" % uid
        out["session"] = self.session_id(uid)
        self.con.commit()
        self._extra_rows(uid, out, mkt[0], tok[0], n)
        self.con.commit()
        return out

    def refresh_book(self, market: str) -> None:
        """Keep the fixture's order book inside the product's five-second freshness window.

        `snap_age_ms` is `now - book_levels.updated_ms`, and the gate fails closed past five seconds — which is the
        product being right and this tool being slow: a run takes twenty seconds, so by the time the risk gate is
        probed every order answers `STALE_QUOTE` and a check about *limits* is reading a different control's
        refusal. The harness keeps its own fixture fresh rather than widening the limit; freshness itself is probed
        deliberately in D6, where the book is allowed to go stale and the refusal is the assertion.
        """
        self.con.execute("UPDATE book_levels SET updated_ms=? WHERE market_id=?", (int(time.time() * 1000),
                                                                                 market))
        self.con.commit()

    def qualify(self, acct: dict, *, address: str, tag: str) -> None:
        """Give an account the standing a real user has: a delegated wallet, a balance, and a funded ledger.

        A probe account that owns nothing is refused for the wrong reason. This bit twice: A had no wallet, so
        `POST /v1/orders` answered 202 and the *ownership* probe looked conclusive; then an earlier section gave A
        a watch-only wallet, `POST /v1/orders` answered 503 ("this wallet is watch-only, so nothing can be signed")
        and the risk-gate probe *still* passed, because it only asked whether the call was refused. A refused call
        for an unrelated reason is not evidence about the risk gate.
        """
        uid, n = acct["uid"], self.now
        self.insert("wallets", user_id=uid, provider="turnkey", custody="delegated", address=address,
                    proxy_address="0x" + "6" * 40, signature_type=3, policy_hash="ph-%s" % tag, state="funded",
                    created_ms=n, updated_ms=n)
        self.insert("balances", user_id=uid, usdc_available_micro=500_000_000, usdc_locked_micro=0, version=1,
                    reconcile_ms=n)
        self.insert("cash_ledger", user_id=uid, kind="deposit", amount_micro=500_000_000, ref_table="probe",
                    ref_id=tag, created_ms=n, reason="P14 probe account")
        self.con.commit()

    def seed_via_api(self, b: dict, bfix: dict) -> dict:
        """B creates its own objects *through the product*, and the probes inherit their ids.

        The first version of the fixtures hand-wrote rows for alerts, copy configs, automations and follows, and
        every one of those probes came back inconclusive: the harness's idea of a row did not match what the
        handlers read (`alert_rules`, not `signal_rules`; a `source_anon` that has to be a real listed trader).
        An audit that invents its own data tests its own assumptions. So B uses the same endpoints a person uses,
        and every seed reports whether it worked — a seed that fails is a probe that will be inconclusive, and it
        is named in the report rather than silently skipped.
        """
        seeds = [
            ("alert", "POST", "/v1/alerts", {
                "kind": "price_level", "marketId": bfix["market"], "firesPerWindow": 2, "windowMs": 3_600_000,
                "severity": "notice", "channel": "telegram",
                "params": {"priceMicro": 500_000, "op": ">="}}, ("ruleId", "rule")),
            ("whale_view", "POST", "/v1/whale-views", {"name": "needle view"}, ("id", "viewId")),
            # Seven markets is past `RADAR_ASYNC_AT`, so the scan is enqueued and answers with a job id — the only
            # way B can own a pollable radar job, and therefore the only way A's read of it can be probed.
            ("radar_run", "POST", "/v1/radar/runs", {"marketIds": bfix["markets"]}, ("jobId", "id")),
            ("referral_code", "POST", "/v1/referrals/code", {"code": "needle%s" % b["uid"][-4:]}, ("code",)),
            ("identity", "POST", "/v1/leaderboard/identity", {"state": "listed", "handle": "needle_b"},
             ("handle", "identity")),
            # The follow response carries no id at all (`followed: true` and the pseudonym), so this seed's
            # result is the *state*, not a key: the probes that need a follow target use the pseudonym.
            ("follow", "POST", "/v1/leaderboard/follows", {"anon": bfix.get("other_anon", "")},
             ("anon", "label")),
            ("copy_config", "POST", "/v1/copy/configs",
             {"sourceAnon": bfix.get("other_anon", b["uid"]), "maxOrderMicro": 1_000_000_000,
              "maxDailyMicro": 5_000_000_000}, ("configId", "id")),
            # The builder vocabulary is literal rows: the trigger says *what* to compare and the number in the
            # engine's own micro units. `{"priceMicro": …}` looks plausible and is refused, which is how the first
            # version of this seed produced a 422 and an automation probe that tested nothing.
            # `maxLossMicro: 0` is refused ("a rule that may not lose anything" is not a bound the console
            # accepts), and the rule id rides inside `rule`. Both cost this seed a round trip to discover.
            ("automation", "POST", "/v1/automations", {
                "kind": "alert", "name": "needle rule", "match": "all", "maxLossMicro": 1_000_000,
                "triggers": [{"kind": "price_cross", "price_micro": 500_000, "op": ">=", "uses": "mid"}],
                "actions": [{"kind": "set_alert", "message": "needle", "channel": "in_app"}],
                "targets": [{"marketId": bfix["market"]}]}, ("rule", "ruleId", "id")),
            # `side` on the card endpoint is `yes`/`no` (the outcome), not `BUY`/`SELL` (the direction): a probe
            # that sent "BUY" got a 422 and tested nothing.
            ("order_amount", "POST", "/v1/orders/amount", {"slug": "fed-cut-sept", "side": "yes",
                                                           "amountUsdc": "25"}, ("size", "sharesMicro")),
        ]
        out, skipped = {}, []
        for name, method, path, body, keys in seeds:
            r = self.request_b(method, path, b["token"], body=body)
            label = "%s %s" % (method, path)
            if not 200 <= r.status_code < 300:
                skipped.append("%s: %d %s" % (label, r.status_code, (r.text or "")[:120].replace("\n", " ")))
                continue
            try:
                payload = r.json()
            except ValueError:
                skipped.append("%s: unparseable body" % label)
                continue
            found = None
            for k in keys:
                v = payload.get(k)
                if v is None and isinstance(payload.get("quota"), dict):
                    v = payload["quota"].get(k)          # the radar job id rides inside `quota`
                if isinstance(v, dict):
                    v = v.get("id") or v.get("ruleId") or v.get("jobId") or v.get("handle") or v.get("configId")
                if isinstance(v, str) and len(v) >= 4:
                    found = v
                    break
                if isinstance(v, int) and k in ("ruleId", "jobId"):
                    found = str(v)
                    break
            if not found:
                skipped.append("%s: no id in %s" % (label, sorted(payload)[:8]))
                continue
            out[name] = str(found)
        out["_seeds_skipped"] = skipped
        return out

    def call_as_user(self, uid: str, path: str, body: dict | None = None, *, method: str = "POST"):
        """One call as `uid`, with a session and an idempotency key minted for it.

        Threaded through the auth section rather than composed at each site: `dict(mapping, more)` is a TypeError
        waiting to happen (it took one run to find that), and a probe that dies of a Python keyword argument
        proves nothing about the product.
        """
        h = {**self.bearer(self.login(uid)), "Idempotency-Key": "p14-%s" % uuid.uuid4().hex[:16]}
        content = json.dumps(body).encode() if body is not None else (
            b"{}" if method in ("POST", "PUT", "PATCH") else None)
        return self.client.request(method, path + ("" if body is not None or method == "GET" else ""),
                                   headers=h, content=content)

    def request_b(self, method: str, path: str, token: str, body: dict | None = None, query: str = ""):
        """One call as B, with the Idempotency-Key every mutating endpoint requires."""
        h = self.bearer(token)
        if method in ("POST", "PUT", "PATCH"):
            h["Idempotency-Key"] = "p14-%s" % uuid.uuid4().hex[:16]
        content = json.dumps(body).encode() if body is not None else (
            b"{}" if method in ("POST", "PUT", "PATCH") else None)
        return self.client.request(method, path + query, headers=h, content=content)

    def _extra_rows(self, uid: str, out: dict, market: str, token: str, n: int) -> None:
        """The tables whose column sets vary by phase. Written best-effort *and reported*: a fixture that could
        not be created is a probe that will come back inconclusive, and the report says which.
        """
        # `withdrawal_addresses` has no `state` column: an allowlist row is active until `removed_ms` is set, so
        # the write-side section proves a stranger's removal attempt did nothing by re-reading this row.
        out["address_id"] = "wa_needle_%s" % uuid.uuid4().hex[:6]
        self.insert("withdrawal_addresses", id=out["address_id"], user_id=uid, address=out["allow"],
                    label="needle-cold", added_ms=n, usable_ms=n)
        skipped = []
        out["_fixtures_skipped"] = skipped

    def session_id(self, uid: str) -> str:
        """B's session id, discovered from whatever the security plane calls its session table.

        Written this way because the name is the product's business, not the probe's: a harness that hardcodes a
        table name fails on the day it is renamed, and the failure looks like a security finding.
        """
        names = [r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        table = next((t for t in names if "session" in t.lower() and t != "auth_events"), None)
        if table is None:
            return "no-session-table"
        cols = [r[1] for r in self.con.execute("PRAGMA table_info(%s)" % table).fetchall()]
        if "user_id" not in cols:
            return "no-session-table"
        # The time column by preference, not by position: `auth_sessions` calls it `issued_ms`, and falling back
        # to `cols[0]` (the primary key, a random string) ordered by *id*, which returns the same row whatever
        # happened — the probe then reported "two logins are one session" against a product that had minted two.
        order = next((c for c in ("created_ms", "issued_ms", "at_ms", "seen_ms") if c in cols), None)
        if order is None:
            return "no-session-timestamp"
        # `rowid` as the tiebreaker: two logins inside the same millisecond are two sessions, and without it this
        # read returns whichever one SQLite feels like — which made "two logins are one session" fire against a
        # product that had in fact minted two.
        row = self.con.execute("SELECT id FROM %s WHERE user_id=? ORDER BY %s DESC, rowid DESC LIMIT 1"
                               % (table, order), (uid,)).fetchone()
        return str(row[0]) if row else "no-session"

    #: What is *private* to B. Everything else the fixture holds is public by design — a listed handle, a market
    #: id, a source pseudonym, a referral code somebody publishes — and putting those in the needle set produced a
    #: Critical finding against a route that had merely returned the public pseudonym A asked for.
    PRIVATE_KEYS = ("uid", "email", "address", "proxy", "allow", "address_id", "intent", "order", "deposit",
                    "session", "alert", "automation", "copy_config", "whale_view", "signal", "rule",
                    "radar_run", "token_id")

    def needles(self, b: dict) -> dict:
        """The strings that must never appear in a response to A. Keyed so a hit names what leaked."""
        return {k: v for k, v in b.items()
                if k in self.PRIVATE_KEYS and isinstance(v, str) and len(v) >= 8}


# --------------------------------------------------------------------------------------- the registry view
def served_ops(api: "Api") -> list[str]:
    """Every operation the *running* app serves, from the router rather than from the source.

    `path_format` is the template the authorisation registry is keyed on, and it is the only string that is
    correct for a parameterised route: the concrete path is a different string per request.
    """
    out = []
    for r in api.app.app.routes:
        path = getattr(r, "path_format", None) or getattr(r, "path", None)
        for m in sorted(getattr(r, "methods", []) or []):
            if m in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                out.append("%s %s" % (m, path))
    return sorted(set(out))


#: The interactive surface FastAPI mounts by itself: three HTML pages and the schema itself. Section A treats
#: them as their own class because the fix is different from the fix for an undeclared route — you switch them
#: off rather than declare them — and because in the production identity shape the answer must be *absent*, not
#: "declared public". `GET /openapi.json` belongs here: it is the biggest disclosure of the four (87 paths,
#: every parameter name, every enum, every error code), and it is the one a scanner that only looks for `/docs`
#: walks straight past.
DOCS_OPS = ("GET /docs", "GET /redoc", "GET /docs/oauth2-redirect", "GET /openapi.json")


def section_registry(api: Api, g) -> dict:
    from polygm_core.security import authz
    ops = served_ops(api)
    rows = dict(authz.LEVELS_TABLE)
    undeclared = [o for o in ops if authz.lookup(o) is None]
    docs = [o for o in undeclared if o in DOCS_OPS]
    real_undeclared = [o for o in undeclared if o not in DOCS_OPS]
    # Declared but not served. The docs rows are excluded because they are conditional on `PGM_DOCS` rather than
    # planned, and they are reported by the docs checks below.
    stale = sorted(set(rows) - {authz.lookup(o)[0] for o in ops if authz.lookup(o)}
                   - {o for o in DOCS_OPS})
    levels = {}
    for o in ops:
        hit = authz.lookup(o)
        levels[o] = hit[1] if hit else None
    g.check("every served operation has an authorisation decision (%d served, %d declared)"
            % (len(ops), len(rows)), not real_undeclared, "undeclared: %s" % real_undeclared[:6])
    g.check("every level in the registry is one of %s" % (", ".join(authz.LEVELS),),
            not [k for k, (lv, _c) in rows.items() if lv not in authz.LEVELS], "")
    # The canary is the point of the section: a registry that declares an admin operation public must be caught.
    doctored = dict(rows)
    doctored["POST /v1/admin/kill-switch"] = (authz.PUBLIC, "")
    caught = [o for o, (lv, _c) in doctored.items()
              if lv == authz.PUBLIC and o.startswith(("POST /v1/admin", "GET /v1/admin"))]
    g.canary("registry canary: an admin operation declared public is caught", caught,
             "an admin row rewritten to public")
    # Declared-but-unserved must equal the registry's own written promise, in both directions: a row that is
    # neither served nor planned is drift (a route that was renamed or abandoned and left behind), and a planned
    # row that is now served is a promise nobody collected. Both are the same class of bug — the table and the
    # API disagreeing about what exists — and the P07 test alone could not catch them, because it compared the
    # table to a list written inside the test file rather than to what the app actually serves.
    planned = set(getattr(authz, "PLANNED", frozenset()))
    stale_set = set(stale)
    drift = sorted(stale_set - planned)
    uncollected = sorted(planned - stale_set - {o for o in planned if o in {
        authz.lookup(o)[0] for o in ops if authz.lookup(o)}})
    g.check("every declared-but-unserved operation is a written promise, and none is drift (%d stale, %d "
            "planned)" % (len(stale), len(planned)), not drift, "drift: %s" % drift[:6])
    g.check("every written promise is still unserved or has been collected (%d)" % len(planned),
            not uncollected, "planned rows the API now serves, still listed as planned: %s" % uncollected[:6])
    g.canary("registry canary: a route renamed out from under the table is caught as drift",
             stale_set | {"GET /v1/gone/away"} - planned, "a stale row that is not on the promise list")
    return {"served": len(ops), "declared": len(rows), "undeclared": real_undeclared, "docs_ops": docs,
            "stale": stale, "planned": sorted(planned), "drift": drift, "levels": levels}


# ------------------------------------------------------------------------------------- section B: anonymous
def section_docs(api: Api, g) -> dict:
    """The interactive surface must not answer in the production identity shape.

    Reported as its own check rather than folded into "no non-public operation answers an anonymous caller",
    because `/docs` is not an authorisation bug — nothing is mis-declared, the page simply should not exist on
    the box. The canary is a *missing* docs route being called a pass: if the harness asked a path that is not
    served and read the 404 as "good", every deployment would look clean.
    """
    served, exposed = [], []
    for op in DOCS_OPS:
        method, path = op.split(" ", 1)
        r = api.client.request(method, path)
        if r.status_code != 404:
            exposed.append((op, r.status_code))
        else:
            served.append(op)
    g.check("the production identity shape serves no interactive API surface (%d of %d paths are 404)"
            % (len(served), len(DOCS_OPS)), not exposed, "still served: %s" % exposed)
    g.canary("docs canary: a served schema is caught (the check must be asking a real path)",
             [("GET /v1/markets", api.client.get("/v1/markets").status_code)], "a 404 on every path")
    return {"exposed": exposed, "absent": served}


def section_anonymous(api: Api, reg: dict, g) -> dict:
    from polygm_core.security import authz
    leaks, refused, public_ok, public_broken = [], 0, 0, []
    for op in sorted(reg["levels"]):
        if op in DOCS_OPS:
            continue     # their own check, above: an undeclared docs route is a disclosure, not a leak
        method, path = op.split(" ", 1)
        url = re.sub(r"\{[^/]+\}", "needle-missing-id", path)
        r = api.client.request(method, url, headers={"Content-Type": "application/json"},
                              content=b"{}" if method in ("POST", "PUT", "PATCH") else None)
        level = reg["levels"][op]
        if level == authz.PUBLIC:
            # A 404 (or a 422 on a path that needs an id we do not have) is the route *answering* an anonymous
            # caller: the resource is missing, the door is open. Only 401/403 means "refused to everyone", and
            # only that is a finding. The first version counted any 4xx as broken, which flagged 21 public routes
            # for the crime of not having a market called `needle-missing-id`.
            if r.status_code in (401, 403):
                public_broken.append((op, r.status_code))
            else:
                public_ok += 1
        else:
            if 200 <= r.status_code < 300:
                leaks.append((op, level, r.status_code))
            else:
                refused += 1
    for op, status in public_broken:
        g.check("anonymous call to the public operation %s is not refused" % op, False,
                "%d — a public route that refuses anonymous callers is broken for everyone" % status)
    g.check("no non-public operation answers an anonymous caller (%d refused)" % refused, not leaks,
            "answered 2xx with no credentials: %s" % leaks[:6])
    # must-accept: the public half of the API is reachable without a session, so a blanket-401 product fails here
    g.check("public operations are reachable anonymously (%d of %d answered below 400)"
            % (public_ok, len([o for o, lv in reg["levels"].items() if lv == authz.PUBLIC])),
            public_ok > 20, "only %d public operations answered" % public_ok)
    return {"anonymous_refused": refused, "anonymous_2xx_where_not_public": leaks,
            "public_ok": public_ok, "public_broken": public_broken}


# -------------------------------------------------------------------------------------- section C: escalation
def section_escalation(api: Api, a: dict, reg: dict, g) -> dict:
    from polygm_core.security import authz
    admin_ops = [o for o, lv in reg["levels"].items() if lv == authz.ADMIN]
    user_reached, unconfigured, wrong_token_bad = [], [], []
    for op in sorted(admin_ops):
        method, path = op.split(" ", 1)
        url = re.sub(r"\{[^/]+\}", "needle-missing-id", path)
        body = b"{}" if method in ("POST", "PUT", "PATCH") else None
        r = api.client.request(method, url, headers=api.bearer(a["token"]), content=body)
        if 200 <= r.status_code < 300:
            user_reached.append((op, r.status_code))
        # a wrong admin token must not be accepted
        r2 = api.client.request(method, url, headers={"X-Admin-Token": "not-the-token",
                                                      "Content-Type": "application/json"}, content=body)
        if 200 <= r2.status_code < 300:
            wrong_token_bad.append((op, r2.status_code))
        elif r2.status_code == 503:
            unconfigured.append(op)
    g.check("a user session cannot reach any of the %d admin operations" % len(admin_ops), not user_reached,
            "reached with a user session: %s" % user_reached[:6])
    g.check("a wrong admin token is never accepted", not wrong_token_bad, str(wrong_token_bad[:6]))
    # must-accept: with no admin token configured at all the answer is 503 (a missing control, not an attack).
    # If this list is empty the deployment has a token set, which changes what the previous line means — say so.
    g.check("with no admin token configured, admin operations answer 503 rather than looking like a refusal "
            "of an attack on a configured box", bool(unconfigured) or "no unconfigured admin ops",
            "neither 503 nor accepted: %s" % [o for o in admin_ops if o not in unconfigured][:4])
    return {"admin_ops": len(admin_ops), "reached_by_user": user_reached, "wrong_token_accepted": wrong_token_bad,
            "unconfigured_503": len(unconfigured)}


# ---------------------------------------------------------------------------------- section D/E: needles
def call_as(api: Api, op: str, headers: dict, path_values: dict | None = None,
            query: str = "", body: dict | None = None, token: str | None = None):
    method, path = op.split(" ", 1)
    url = path
    for k, v in (path_values or {}).items():
        url = url.replace("{%s}" % k, str(v))
    url = re.sub(r"\{[^/]+\}", "needle-missing-id", url) + query
    h = dict(headers)
    # `Content-Type` matters: the app parses a JSON body by its declared type, and a probe without this header gets
    # a body-less 422 that looks like a validation failure of a payload that was in fact fine (`request_b` had it,
    # `call_as` did not, and the two disagreed about the same call for a whole run).
    h.setdefault("Content-Type", "application/json")
    if token:
        h["Authorization"] = "Bearer %s" % token
    if method in ("POST", "PUT", "PATCH"):
        # Every mutating route requires it, and the answer when it is missing is a *generic* 422 that looks like a
        # body problem: the first needle scan sent valid bodies without a key and every probe came back
        # "VALIDATION" with no field named, which read as "B's own call was refused" rather than "the harness
        # forgot the header".
        h.setdefault("Idempotency-Key", "p14-%s" % uuid.uuid4().hex[:16])
    content = json.dumps(body).encode() if body is not None else (
        b"{}" if method in ("POST", "PUT", "PATCH") else None)
    r = api.client.request(method, url, headers=h, content=content)
    return r


def hits_in(response, needles: dict) -> list[str]:
    text = response.text or ""
    return sorted(k for k, v in needles.items() if v in text)


def self_bodies(bfix: dict) -> dict:
    """A valid body for B's own call on every mutating user operation.

    The sensitivity control ("did B's own call surface a needle?") answers *no* for a 422, so a body of `{}` makes
    a probe inconclusive — twenty-five of them in the first run. These bodies come from the same vocabulary the
    seeds use, so B's own call reaches the handler and the answer means something.
    """
    mkt = bfix.get("market", "0xM1")
    return {
        "POST /v1/alerts": {"kind": "price_level", "marketId": mkt, "firesPerWindow": 2,
                            "windowMs": 3_600_000, "severity": "notice", "channel": "telegram",
                            "params": {"priceMicro": 500_000, "op": ">="}},
        "POST /v1/alerts/settings": {"quietStartMin": -1, "quietEndMin": -1, "tzOffsetMin": 0, "digestMode": "off",
                                     "digestAtMin": 480, "defaultChannel": "telegram"},
        "POST /v1/alerts/test": {"ruleId": bfix.get("alert", "al-missing")},
        "POST /v1/automations/guards": {"ruleId": bfix.get("automation", "rule-missing"), "state": "paused"},
        "POST /v1/automations/preview": {},
        "POST /v1/copy/configs/guards": {"configId": bfix.get("copy_config", "cfg-missing"), "dryRun": True},
        "POST /v1/leaderboard/identity": {"state": "listed", "handle": "needle_b"},
        "POST /v1/leaderboard/recompute": {},
        "POST /v1/orders": {"marketId": mkt, "tokenId": bfix.get("token_id", "tok"), "side": "BUY",
                            "price": "0.50", "size": "10"},
        "POST /v1/orders/amount": {"slug": "fed-cut-sept", "side": "yes", "amountUsdc": "25"},
        "POST /v1/wallet/deposit/quote": {"chain": "polygon", "amountUsdc": "25"},
        "POST /v1/wallet/withdrawal-addresses/add": {"address": "0x" + "3" * 40, "label": "needle-add"},
        "POST /v1/wallet/withdrawal-addresses/remove": {"addressId": bfix.get("address_id", "wa-missing")},
        "POST /v1/auth/logout": {},
        "POST /v1/auth/sessions/revoke": {"sessionId": bfix.get("session", "sess-missing")},
        "POST /v1/radar/runs": {"marketIds": bfix.get("markets", [mkt])},
        "POST /v1/referrals/code": {"code": "needle%s" % (bfix.get("uid", "bbbb")[-4:],)},
        "POST /v1/referrals/apply": {"code": bfix.get("referral_code", "")},
    }


#: Queries the id-bearing *reads* need. A detail route called without its id answers 422, and a 422 is a probe
#: that never reached the question being asked.
QUERIES: dict = {}


def section_needles(api: Api, a: dict, b: dict, bfix: dict, reg: dict, g) -> dict:
    """A carries B's identifiers; the response must not contain B's private strings.

    The sensitivity control runs first: B calls with B's own session and B's own identifiers, and if that
    response surfaces no needle then the probe cannot prove anything about A's — those rows are reported as
    inconclusive instead of being counted as passes.
    """
    from polygm_core.security import authz
    needles = api.needles(bfix)
    QUERIES.update({"GET /v1/copy/configs/monitor": "?configId=%s" % bfix.get("copy_config", ""),
                    "GET /v1/automations/runs": "?ruleId=%s" % bfix.get("automation", "")})

    def token_for(uid: str, op: str, cache: dict) -> str:
        """A cached session for reads, a brand-new one for anything that mutates."""
        if not op.startswith("GET"):
            return api.fresh_login(uid)
        if uid not in cache:
            cache[uid] = api.fresh_login(uid)
        return cache[uid]

    cache: dict = {}
    bodies = self_bodies(bfix)
    path_ids = {"intent_id": bfix.get("intent"), "deposit_id": bfix.get("deposit"),
                "job_id": bfix.get("radar_run", "radar_needle"),
                "market_id": "0xM1", "event_id": "evt-needle", "slug": "fed-cut-sept", "board": "pnl",
                "anon": bfix.get("handle", "needle"), "handle": bfix.get("handle", "needle"),
                "address": bfix.get("address"), "anon_id": bfix.get("handle")}
    rows, leaks, conclusive, inconclusive = [], [], 0, []
    user_ops = [o for o, lv in reg["levels"].items() if lv in (authz.USER, authz.OWNS)]
    for op in sorted(user_ops):
        query = QUERIES.get(op, "")
        own = call_as(api, op, {}, path_values=path_ids, query=query, body=bodies.get(op),
                      token=token_for(b["uid"], op, cache))
        own_hits = hits_in(own, needles)
        if not 200 <= own.status_code < 300 or not own_hits:
            inconclusive.append((op, own.status_code, own_hits))
            rows.append({"op": op, "sensitivity": "%d/%d" % (own.status_code, len(own_hits)),
                         "stranger": "-"})
            continue
        conclusive += 1
        r = call_as(api, op, {}, path_values=path_ids, query=query, body=bodies.get(op),
                    token=token_for(a["uid"], op, cache))
        cross = hits_in(r, needles)
        rows.append({"op": op, "sensitivity": "leaks %s" % ",".join(own_hits),
                     "stranger": "%d %s" % (r.status_code, ",".join(cross) or "clean")})
        if cross:
            leaks.append((op, r.status_code, cross))
    g.check("no operation leaks a stranger's identifiers to a signed-in user (%d conclusive probes, %d "
            "inconclusive)" % (conclusive, len(inconclusive)), not leaks,
            "leaked: %s" % leaks[:6])
    g.check("the needle probes are conclusive for the id-bearing operations (a probe where B's own call "
            "surfaces nothing proves nothing)",
            conclusive >= 6, "only %d conclusive probes: %s" % (conclusive, inconclusive[:8]))
    # canary: the scan must notice a needle that IS present, or section D is a scan of nothing
    fake = type("R", (), {"text": "prefix %s suffix" % bfix.get("address", "x" * 20)})()
    g.canary("needle canary: a response containing a stranger's wallet address is caught",
             hits_in(fake, needles), "a response carrying B's own address")
    return {"rows": rows, "conclusive": conclusive, "inconclusive": len(inconclusive), "leaks": leaks}


# ------------------------------------------------------------------------------------ section F: own access
def section_own_access(api: Api, a: dict, reg: dict, g) -> dict:
    from polygm_core.security import authz
    broken, ok = [], 0
    cache: dict = {}
    for op, lv in sorted(reg["levels"].items()):
        if lv not in (authz.USER, authz.OWNS):
            continue
        token = a["token"] if op.startswith("GET") else api.fresh_login(a["uid"])
        r = call_as(api, op, {}, token=token)
        if r.status_code in (401, 403):
            broken.append((op, r.status_code))
        elif r.status_code < 500:
            ok += 1
    g.check("every user operation accepts its rightful owner's session (%d answered, none 401/403)" % ok,
            not broken, "refused the owner: %s" % broken[:6])
    g.check("no user operation answers 5xx to its own owner (%d answered cleanly)" % ok, ok > 30,
            "too few operations answered: %d" % ok)
    return {"answered": ok, "refused_owner": broken}


# ------------------------------------------------------------- section G: a stranger's writes (the priority)
def section_foreign(api: Api, a: dict, b: dict, bfix: dict, reg: dict, g) -> dict:
    """A acts on B's objects with **valid** payloads, and the object is read back afterwards.

    The kit puts this first for a reason: reading somebody else's position is bad, cancelling their order is
    theft. Every probe here is therefore two assertions in one — A is refused, *and* B's row is unchanged. A
    refusal that still mutated the row is the worst outcome and it is the one a status-code-only suite misses.
    Payloads are deliberately well-formed and reference B's real ids: a 422 on a malformed body proves nothing
    about authorisation, which is why the earlier version of this tool (bodies of `{}`) reported 37
    inconclusive probes and called it green.
    """
    from polygm_core.security import authz
    needles = api.needles(bfix)

    def state(table: str, id_col: str, row_id: str, cols: tuple[str, ...]) -> tuple:
        row = api.con.execute("SELECT %s FROM %s WHERE %s=?" % (", ".join(cols), table, id_col),
                              (row_id,)).fetchone()
        return tuple(row) if row else ("<missing>",)

    def probe(name: str, method: str, path: str, body: dict | None, watch: tuple | None = None,
              cross: bool = True) -> dict:
        """Run one attack as A; return what A got and whether B's watched row moved.

        `cross=False` marks a call that *cannot* name another account — the body carries no id, so the only row it
        can touch is A's own. Those still have to be watched (a handler that ignored the session and wrote to
        whoever it liked would show up as B's row changing) and they still have to be scanned for B's strings, but
        a 200 on them is the product working, not a finding. The first version of this section counted a 200 on
        `POST /v1/leaderboard/identity` as a cross-account write; it was A editing A's own handle.
        """
        before = state(*watch) if watch else None
        r = api.request_b(method, path, api.fresh_login(a["uid"]), body=body)
        after = state(*watch) if watch else None
        text = (r.text or "")[:400]
        try:
            code = (((r.json() or {}).get("error") or {}).get("code") or "")
        except ValueError:
            code = ""      # a body that is not JSON at all is itself worth seeing, so it is not an error here
        leaked = hits_in(r, needles)
        refused = not (200 <= r.status_code < 300)
        changed = before is not None and before != after
        return {"op": "%s %s" % (method, path), "as": "A", "status": r.status_code,
                "refused": refused if cross else None, "cross": cross, "leaked": leaked, "mutated": changed,
                "before": before, "after": after, "code": code, "first": text[:120]}

    rows = []
    api.refresh_book(bfix["market"])
    # --- money: cancelling, withdrawing, removing an allowlisted destination, exporting keys
    rows.append(probe("A cancels B's order", "POST", "/v1/orders/%s/cancel" % bfix["intent"], {},
                      watch=("order_intents", "id", bfix["intent"], ("state", "user_id"))))
    rows.append(probe("A rewrites B's allowlist entry", "POST", "/v1/wallet/withdrawal-addresses/remove",
                      {"addressId": bfix["address_id"]},
                      watch=("withdrawal_addresses", "id", bfix["address_id"], ("removed_ms", "user_id"))))
    # The withdrawal is A's own, correct, fully-formed request with **B's address id and B's address typed in**:
    # A knows the password, A holds a live authenticator code, and the only thing wrong with the request is whose
    # money it names. `amountUsdc` is a *string* on this API (`^[0-9]{1,9}(\.[0-9]{1,2})?$`) — a probe that sent a
    # number was refused by the body validator and never reached the ownership check.
    a_code = totp(a["totp_secret"]) if a.get("totp_secret") else "000000"
    rows.append(probe("A withdraws from B's wallet", "POST", "/v1/wallet/withdraw",
                      {"amountUsdc": "5", "addressId": bfix["address_id"], "typedAmount": "5",
                       "typedAddress": bfix["allow"], "password": api.pw, "code": a_code},
                      watch=("balances", "user_id", b["uid"], ("usdc_available_micro", "usdc_locked_micro"))))
    rows.append(probe("A exports B's wallet keys", "POST", "/v1/wallet/keys/export",
                      {"password": api.pw, "code": a_code, "typedConfirm": "EXPORT"}))
    # --- sessions and identity
    rows.append(probe("A revokes B's session", "POST", "/v1/auth/sessions/revoke", {"sessionId": bfix["session"]},
                      watch=("auth_sessions", "id", bfix["session"], ("revoked_ms",))))
    rows.append(probe("A writes a leaderboard identity (its own row only: the body names no account)",
                      "POST", "/v1/leaderboard/identity", {"state": "private", "handle": "stolen"},
                      watch=("leaderboard_identity", "user_id", b["uid"], ("state", "handle")), cross=False))
    # --- automation, alerts, copy: the "act on their account" class
    if bfix.get("automation"):
        rows.append(probe("A disables B's automation rule", "POST", "/v1/automations",
                          {"ruleId": bfix["automation"], "kind": "alert", "name": "hijacked", "match": "all",
                           "triggers": [{"kind": "price_cross", "marketId": bfix["market"],
                                         "params": {"priceMicro": 1, "op": ">="}}],
                           "actions": [{"kind": "set_alert", "params": {"severity": "notice"}}],
                           "targets": [{"marketId": bfix["market"]}]}))
        rows.append(probe("A flips B's automation guard", "POST", "/v1/automations/guards",
                          {"ruleId": bfix["automation"], "state": "on"}))
    if bfix.get("alert"):
        rows.append(probe("A silences B's alert rule", "POST", "/v1/alerts",
                          {"ruleId": bfix["alert"], "kind": "price_level", "marketId": bfix["market"],
                           "enabled": False, "params": {"priceMicro": 999_999, "op": ">="}}))
        rows.append(probe("A borrows B's alert rule for a test fire", "POST", "/v1/alerts/test",
                          {"ruleId": bfix["alert"]}))
    if bfix.get("copy_config"):
        rows.append(probe("A raises B's copy-trade caps", "POST", "/v1/copy/configs",
                          {"configId": bfix["copy_config"], "sourceAnon": bfix.get("other_anon", b["uid"]),
                           "maxOrderMicro": 999_999_999_999, "maxDailyMicro": 999_999_999_999}))
        rows.append(probe("A opens B's copy guard", "POST", "/v1/copy/configs/guards",
                          {"configId": bfix["copy_config"], "state": "on"}))
    # --- reads that take an id: the IDOR fuzz the kit names
    rows.append(probe("A reads B's order intent", "GET", "/v1/orders/intents/%s" % bfix["intent"], None))
    rows.append(probe("A reads B's deposit", "GET", "/v1/wallet/deposit/%s" % bfix["deposit"], None))
    rows.append(probe("A reads B's copy monitor", "GET", "/v1/copy/configs/monitor",
                      None) if not bfix.get("copy_config") else
                probe("A reads B's copy monitor", "GET", "/v1/copy/configs/monitor?configId=%s"
                      % bfix["copy_config"], None))
    rows.append(probe("A reads B's automation runs", "GET", "/v1/automations/runs?ruleId=%s"
                      % bfix.get("automation", "ar_missing"), None))
    if bfix.get("radar_run"):
        rows.append(probe("A reads B's radar job", "GET", "/v1/radar/runs/%s" % bfix["radar_run"], None))
    rows.append(probe("A lists alerts filtered by B's rule id", "GET", "/v1/alerts?ruleId=%s"
                      % bfix.get("alert", "al_missing"), None, cross=False))

    # --- "who owns the row this call creates?" The question a body-supplied user id would answer wrongly.
    # The schema has no field for it (`_check_body` refuses unknown properties: a body naming `userId` answers
    # 422 before the handler runs), so the probe is the honest version of the same question: A places an order
    # with A's session, and the row that appears must be A's. An endpoint that read the owner from anywhere but
    # the session — a header, a cookie, a default — fails here, and B's balance is watched while it is tried.
    api.refresh_book(bfix["market"])
    spoof_body = {"marketId": bfix["market"], "tokenId": bfix["token_id"], "side": "BUY", "price": "0.50",
                  "size": "10"}
    r = api.request_b("POST", "/v1/orders", api.fresh_login(a["uid"]), body=spoof_body)
    made = (r.json() or {}).get("intentId") if 200 <= r.status_code < 300 else None
    owner = None
    if made:
        row = api.con.execute("SELECT user_id FROM order_intents WHERE id=?", (str(made),)).fetchone()
        owner = str(row[0]) if row else "<missing>"
    spoof_ok = (owner == a["uid"]) if made else None
    named = api.request_b("POST", "/v1/orders", api.fresh_login(a["uid"]),
                          body=dict(spoof_body, userId=b["uid"], user_id=b["uid"]))
    rows.append({"op": "POST /v1/orders as A, then read the row's owner", "as": "A", "status": r.status_code,
                 "refused": spoof_ok is not False, "cross": True, "leaked": hits_in(r, needles),
                 "mutated": spoof_ok is False,
                 "before": None, "after": ("intent owner", owner), "code": "BODY_OWNER",
                 "first": "a body naming an account answers %d" % named.status_code})

    # --- a service token is not a user session: the executor's credential must not act for a person
    service_probes = []
    for path, payload in (("/v1/orders", {"marketId": bfix["market"], "tokenId": bfix["token_id"], "side": "BUY",
                                          "price": "0.50", "size": "10", "userId": b["uid"]}),
                          ("/v1/wallet/balance", None),
                          ("/v1/automations", None)):
        h = {"X-Service-Token": api.service_token, "Content-Type": "application/json",
             "Idempotency-Key": "p14-%s" % uuid.uuid4().hex[:16]}
        r = api.client.request("POST" if payload is not None else "GET", path, headers=h,
                               json=payload if payload is not None else None)
        service_probes.append({"op": path, "status": r.status_code, "accepted": 200 <= r.status_code < 300,
                               "leaked": hits_in(r, needles)})

    # --- the admin door must not be a way around the risk gate
    admin_body = {"marketId": bfix["market"], "tokenId": bfix["token_id"], "side": "BUY", "price": "0.50",
                  "size": "10", "userId": b["uid"]}
    r_admin = api.client.post("/v1/orders", json=admin_body,
                              headers={"X-Admin-Token": "p14-not-the-token", "Content-Type": "application/json",
                                       "Idempotency-Key": "p14-%s" % uuid.uuid4().hex[:16]})
    admin_intent = None
    if 200 <= r_admin.status_code < 300:
        admin_intent = (r_admin.json() or {}).get("intentId") or (r_admin.json() or {}).get("id")
    admin_owner = None
    if admin_intent:
        row = api.con.execute("SELECT user_id FROM order_intents WHERE id=?", (str(admin_intent),)).fetchone()
        admin_owner = str(row[0]) if row else "<missing>"

    crossed = [r for r in rows if r["refused"] is False]      # only the cross-account probes can fail this
    mutated = [r for r in rows if r["mutated"]]
    leaked = [r for r in rows if r["leaked"]]
    g.check("a stranger cannot act on another account (%d attacks, none accepted)" % len(rows), not crossed,
            "accepted: %s" % [(r["op"], r["status"]) for r in crossed][:6])
    g.check("no refused attack changed the other account's row (%d watched rows)" % len(rows), not mutated,
            "mutated: %s" % [(r["op"], r["before"], r["after"]) for r in mutated][:4])
    g.check("no refused attack echoed the other account's private strings", not leaked,
            "leaked: %s" % [(r["op"], r["leaked"]) for r in leaked][:4])
    # must-accept (the direction a denial-only matrix misses): the watch must be able to observe a change at all.
    api.con.execute("UPDATE withdrawal_addresses SET removed_ms=? WHERE id=?", (api.now, bfix["address_id"]))
    api.con.commit()
    canary_after = state("withdrawal_addresses", "id", bfix["address_id"], ("removed_ms", "user_id"))
    api.con.execute("UPDATE withdrawal_addresses SET removed_ms=NULL WHERE id=?", (bfix["address_id"],))
    api.con.commit()
    g.canary("write canary: a changed row is visible to the post-state read", canary_after[0], "an unchanged row")
    g.check("an intent created with A's session is A's, whatever else the request says (%s)"
            % ("owner is A" if spoof_ok else "no intent created: %d" % r.status_code),
            spoof_ok is not False, "the intent was created for %s" % owner)
    g.check("the order body has no field that names an account (a body with `userId` answers %d, so there is "
            "nothing to spoof)" % named.status_code, named.status_code == 422,
            "a request naming another account was accepted with %d: %s" % (named.status_code,
                                                                           (named.text or "")[:120]))
    g.check("an admin token is not a way around the user session on an order (status %d)" % r_admin.status_code,
            admin_owner is None, "created an intent owned by %s" % admin_owner)
    service_accepted = [p for p in service_probes if p["accepted"]]
    g.check("a service token is not a user identity (%d probes, none accepted)" % len(service_probes),
            not service_accepted, "accepted: %s" % service_accepted)

    # --- the risk gate is not a layer an operator can step over -------------------------------------------
    # D1 names this directly: "admin paths must not bypass the risk gate". Two halves, because either one alone is
    # satisfiable by accident: the over-cap order has to be *refused*, and it has to stay refused when the same
    # body arrives with an admin credential attached. A gate that only refuses when nobody is watching is not a
    # gate, and a gate that refused everything would pass the first half.
    over = {"marketId": bfix["market"], "tokenId": bfix["token_id"], "side": "BUY", "price": "0.50",
            "size": "100000000"}
    as_user = api.request_b("POST", "/v1/orders", api.fresh_login(a["uid"]), body=over)
    as_user_code = ""
    try:
        as_user_code = (((as_user.json() or {}).get("error") or {}).get("code") or "")
    except ValueError:
        as_user_code = ""
    gate_h = {"X-Admin-Token": "p14-not-the-token", "Content-Type": "application/json",
              "Idempotency-Key": "p14-%s" % uuid.uuid4().hex[:16]}
    as_admin = api.client.post("/v1/orders", headers=gate_h, json=over)
    as_admin_code = ""
    try:
        as_admin_code = (((as_admin.json() or {}).get("error") or {}).get("code") or "")
    except ValueError:
        as_admin_code = ""
    rows.append({"op": "POST /v1/orders over the order cap, with an admin credential", "as": "admin",
                 "status": as_admin.status_code, "refused": not (200 <= as_admin.status_code < 300),
                 "cross": True, "leaked": hits_in(as_admin, needles), "mutated": False, "before": None,
                 "after": None, "code": as_admin_code, "first": "as a user: %d %s" % (as_user.status_code,
                                                                                    as_user_code)})
    #: The reasons a limit can have. A refusal with one of these is the gate working; any other 4xx is a different
    #: control answering and says nothing about the gate.
    risk_codes = ("OVER_ORDER_CAP", "OVER_DAILY_CAP", "TOO_MANY_OPEN", "PRICE_OFF_GRID", "INSUFFICIENT_BALANCE")
    # The refusal *reason* is the assertion. A 503 ("this wallet is watch-only") is also a refusal, and the first
    # version of this check was satisfied by one: the probe account had been given a watch-only wallet by an
    # earlier section and every order it placed was refused for a reason that has nothing to do with limits.
    g.check("an order above the risk limits is refused for the limit reason (%d %s)"
            % (as_user.status_code, as_user_code or "no code"),
            as_user_code in risk_codes, "refused for something else: %s" % (as_user_code or r.text[:80]))
    # must-accept: an order *inside* the limits still goes through, so the check above is not satisfied by a
    # product that refuses every order.
    small = api.request_b("POST", "/v1/orders", api.fresh_login(a["uid"]),
                          body=dict(over, size="5"))
    g.check("an order inside the limits is still accepted (%d)" % small.status_code,
            200 <= small.status_code < 300, "a legal order was refused: %d %s" % (small.status_code,
                                                                                  (small.text or "")[:120]))
    g.check("an admin credential does not step over the risk gate (%d, and with no admin token configured on "
            "this box it is refused before the gate at all)" % as_admin.status_code,
            as_admin.status_code in (403, 503) or as_admin.status_code == 401,
            "an admin credential placed it: %d" % as_admin.status_code)
    # The structural half, because "no admin route places an order" is a property of the surface, not of one call:
    # if a later phase adds one, this is where it is noticed.
    admin_order_ops = [o for o, lv in reg["levels"].items()
                       if lv == authz.ADMIN and ("/orders" in o or "/trade" in o)]
    g.check("no admin operation is a route that places an order (%d admin operations)" % len(
        [o for o, lv in reg["levels"].items() if lv == authz.ADMIN]), not admin_order_ops,
        "an operator door exists on the order path: %s" % admin_order_ops)
    # must-accept, in the place the credential actually lives. The API reads no service header at all — the
    # shared secret guards the *executor*, which is a worker rather than an HTTP service (its own gate is
    # `tools/p08-gate-check.py`). So the honest second half of this check is that the one function which decides
    # "is this the service?" answers correctly in both directions: it must refuse a missing or empty secret (a box
    # that forgot to set one is an outage, not an open door) and it must accept the real one.
    from polygm_core.security import authz as _authz
    comparator = (_authz.check_service_token("", api.service_token) is False
                  and _authz.check_service_token(api.service_token, "") is False
                  and _authz.check_service_token("wrong-" + api.service_token, api.service_token) is False
                  and _authz.check_service_token(api.service_token, api.service_token) is True)
    g.check("the shared service secret is enforced where it belongs, and is not an API credential "
            "(%d header probes refused)" % len(service_probes), comparator,
            "the comparator that guards the executor does not answer both ways")
    return {"attacks": len(rows), "rows": rows, "accepted": crossed, "mutated": mutated, "leaked": leaked,
            "admin_order_status": r_admin.status_code, "service_probes": service_probes}


# --------------------------------------------------------------------- section H: authentication (D1's auth)
def section_auth(api: Api, a: dict, b: dict, bfix: dict, g) -> dict:
    """initData, sessions, the second factor, and the destination allowlist — every one probed by *doing it*.

    The kit lists these under authorisation for a reason that shows up immediately: each of them is a way to be
    somebody else. A forged initData is an account takeover, a reusable refresh token is an account takeover that
    survives a password change, a bypassed second factor is a withdrawal to an attacker's address, and a
    homoglyph address is that withdrawal arriving somewhere that looks right in the confirmation.
    """
    from polygm_core.security import telegram as _tg
    from polygm_core.security import authz as _authz
    rows, refusals, explanations = [], [], []

    def rec(name: str, r, expect_refused: bool, note: str = "") -> bool:
        """Record one probe. `ok` is the verdict, and it is the only field the check reads.

        `refused` was doing two jobs — "the product refused this" and "the product did the right thing" — and for
        a probe whose *correct* answer is a 200 the two are opposites, so the aggregate check counted correct
        behaviour as a failure twice. One field, one meaning.
        """
        refused = not (200 <= r.status_code < 300)
        code = ""
        try:
            code = (((r.json() or {}).get("error") or {}).get("code") or "")
        except ValueError:
            code = ""
        ok = refused if expect_refused else (not refused)
        rows.append({"probe": name, "status": r.status_code, "code": code, "refused": refused, "ok": ok,
                     "note": note})
        if not ok:
            refusals.append("%s [%d %s]" % (name, r.status_code, code or "2xx"))
        return ok

    # ---- initData ------------------------------------------------------------------------------
    def init_data(auth_date_ms: int, token: str | None = None, user: str = "900000001") -> str:
        fields = {"auth_date": str(int(auth_date_ms // 1000)), "query_id": "AA%s" % uuid.uuid4().hex[:10],
                  "user": json.dumps({"id": int(user), "first_name": "P14", "username": "p14probe"},
                                     separators=(",", ":"))}
        fields["hash"] = _tg.sign(fields, token or api.bot_token)
        return "&".join("%s=%s" % (k, v) for k, v in fields.items())

    # The Telegram account is linked to A first. The unlinked path is *supposed* to be idempotent — it mints
    # nothing and answers "not linked" — so a replay probe against it proves nothing at all: the first run of this
    # section reported "a replayed initData is accepted" against exactly that path. The drill has to be run where
    # a session is actually minted.
    tg_id = "900000001"
    api.app.SEC.link_identity(a["uid"], "telegram", tg_id, at=api.now)
    fresh = init_data(api.now, user=tg_id)
    ok = api.client.post("/v1/telegram/session", headers={"Content-Type": "application/json"},
                         json={"initData": fresh})
    rec("a fresh, correctly signed initData mints a session (must-accept)", ok, False,
        "the forgery probes below mean nothing if this is refused")
    if not 200 <= ok.status_code < 300:
        explanations.append("initData accepted? %d %s" % (ok.status_code, (ok.text or "")[:120]))
    else:
        bodies = ok.json() or {}
        if not bodies.get("accessToken"):
            refusals.append("a linked, fresh initData answered without a session")
            explanations.append("linked sign-in body: %s" % sorted(bodies)[:6])

    # replay: the same payload again. The login path consumes the hash, so the second use must be refused.
    replay = api.client.post("/v1/telegram/session", headers={"Content-Type": "application/json"},
                             json={"initData": fresh})
    rec("a replayed initData is refused (the hash is consumed by the first use)", replay, True)

    # and the unlinked path, which must be inert: no session, and the same answer however often it is used
    unlinked = init_data(api.now, user="900000777")
    u1 = api.client.post("/v1/telegram/session", headers={"Content-Type": "application/json"},
                         json={"initData": unlinked})
    u2 = api.client.post("/v1/telegram/session", headers={"Content-Type": "application/json"},
                         json={"initData": unlinked})
    # Volatile keys excluded: two responses to the same request differ in their request id and timestamps, and a
    # comparison that included them would report "the answer changed" every time.
    #: The response envelope's volatile keys. `staleAfter` was missing from this list and cost a run: the
    #: unlinked-payload probe reported "the answer changed between calls" when the only difference was a
    #: five-millisecond clock reading inside the freshness metadata.
    ENVELOPE = ("asOf", "serverAsOf", "staleAfter", "cache", "requestId")

    def stable(payload: dict) -> dict:
        return {k: v for k, v in (payload or {}).items() if k not in ENVELOPE}
    inert = (not ((u1.json() or {}).get("accessToken")) and not ((u2.json() or {}).get("accessToken"))
             and stable(u1.json()) == stable(u2.json()))
    rows.append({"probe": "an unlinked Telegram account's payload mints nothing, however often it is used",
                 "status": u1.status_code, "code": "", "refused": False, "ok": inert,
                 "note": "no accessToken on either call; the replay store is written where a session is minted"})
    if not inert:
        refusals.append("an unlinked Telegram payload produced a session or changed its answer")

    forged = init_data(api.now).replace("hash=", "hash=", 1)
    fields = dict(p.split("=", 1) for p in forged.split("&"))
    fields["hash"] = "0" * 64
    forged_q = "&".join("%s=%s" % (k, v) for k, v in fields.items())
    rec("a forged initData hash is refused",
        api.client.post("/v1/telegram/session", headers={"Content-Type": "application/json"},
                        json={"initData": forged_q}), True)
    fields["hash"] = fields["hash"][:32]
    rec("a truncated initData hash is refused",
        api.client.post("/v1/telegram/session", headers={"Content-Type": "application/json"},
                        json={"initData": "&".join("%s=%s" % (k, v) for k, v in fields.items())}), True)
    rec("an initData signed with a different bot token is refused",
        api.client.post("/v1/telegram/session", headers={"Content-Type": "application/json"},
                        json={"initData": init_data(api.now, token="7654321098:" + "w" * 32)}), True)
    rec("an expired initData is refused",
        api.client.post("/v1/telegram/session", headers={"Content-Type": "application/json"},
                        json={"initData": init_data(api.now - 2 * 86_400_000)}), True)
    rec("an initData with no hash at all is refused",
        api.client.post("/v1/telegram/session", headers={"Content-Type": "application/json"},
                        json={"initData": "auth_date=1&user=%7B%22id%22%3A1%7D"}), True)

    # ---- sessions ------------------------------------------------------------------------------
    # `session_id` reads the *newest* session for the account, so it has to be read immediately after each login:
    # reading both at the end compares the second session with itself and reports "two logins are one session".
    t1 = api.login(a["uid"])
    s1 = api.session_id(a["uid"])
    t2 = api.login(a["uid"])
    s2 = api.session_id(a["uid"])
    two = t1 != t2 and s1 != s2
    rows.append({"probe": "two logins produce different sessions and different tokens", "status": 200, "code": "",
                 "refused": False, "ok": two,
                 "note": "session fixation needs a caller-supplied session id; there is none"})
    if not two:
        refusals.append("two logins are one session")
    refresh = api.client.post("/v1/auth/refresh", headers={"Content-Type": "application/json"},
                              json={"refreshToken": a.get("refresh", "none")})
    rec("a refresh with no valid refresh token is refused", refresh, True)
    revoked = api.client.get("/v1/auth/sessions", headers=api.bearer(t1))
    rec("a session minted before a login still reads its own list (must-accept)", revoked, False)

    # logout-everywhere must be A's own sessions and nobody else's
    b_before = api.client.get("/v1/auth/sessions", headers=api.bearer(api.login(b["uid"])))
    out_everywhere = api.call_as_user(a["uid"], "/v1/auth/logout", {"scope": "all"})
    b_after = api.client.get("/v1/auth/sessions", headers=api.bearer(api.login(b["uid"])))
    untouched = b_after.status_code == 200 and b_before.status_code == 200
    rows.append({"probe": "logout of one account does not touch another's sessions",
                 "status": out_everywhere.status_code, "code": "", "refused": False, "ok": untouched,
                 "note": "A: %d, B before %d, B after %d" % (out_everywhere.status_code, b_before.status_code,
                                                              b_after.status_code)})
    if not untouched:
        refusals.append("logout touched another account's sessions")

    # ---- the second factor on the two actions that move money or keys ---------------------------
    acct = {"uid": a["uid"], "token": api.login(a["uid"])}
    if not a.get("totp_secret"):
        explanations.append("A holds no second factor between logins: the 2FA probes are inconclusive")
    else:
        good = totp(a["totp_secret"])
        wrong = "0" * 6 if good != "000000" else "1" * 6
        def withdraw(code, address_id, typed=None):
            return api.call_as_user(a["uid"], "/v1/wallet/withdraw",
                                    {"amountUsdc": "1", "addressId": address_id, "typedAmount": "1",
                                     "typedAddress": typed or bfix["allow"], "password": api.pw, "code": code})
        rec("a withdrawal with a wrong authenticator code is refused", withdraw(wrong, bfix["address_id"]), True)
        # A code is spent once: the second use of the same code is the drill P08 calls `reused`.
        first = withdraw(good, bfix["address_id"])
        second = withdraw(good, bfix["address_id"])
        spent = not (200 <= second.status_code < 300)
        rows.append({"probe": "an authenticator code is spent once", "status": second.status_code, "code": "",
                     "refused": spent, "ok": spent,
                     "note": "first %d, second %d" % (first.status_code, second.status_code)})
        if not spent:
            refusals.append("the same authenticator code was accepted twice")

    # ---- the destination allowlist ---------------------------------------------------------------
    addr_ok = "0x" + "b" * 40
    added = api.call_as_user(a["uid"], "/v1/wallet/withdrawal-addresses/add",
                             {"address": addr_ok, "label": "p14 fresh"})
    new_id = ""
    if 200 <= added.status_code < 300:
        new_id = str((added.json() or {}).get("addressId") or (added.json() or {}).get("id") or "")
    cooldown = api.call_as_user(a["uid"], "/v1/wallet/withdraw",
                                {"amountUsdc": "1", "addressId": new_id or bfix["address_id"],
                                 "typedAmount": "1", "typedAddress": addr_ok, "password": api.pw,
                                 "code": totp(a["totp_secret"]) if a.get("totp_secret") else "000000"})
    rec("a destination inside its 24-hour hold cannot be withdrawn to", cooldown, True)

    # a homoglyph destination: same length, one character that only looks like the stored one. It must not match
    # the allowlist entry, and the refusal must be about *this* address rather than about a missing rule.
    confusable = addr_ok[:-1] + ("O" if addr_ok[-1] != "O" else "0")
    homoglyph = api.call_as_user(a["uid"], "/v1/wallet/withdrawal-addresses/add",
                                 {"address": confusable, "label": "p14 confusable"})
    rows.append({"probe": "a confusable destination is its own allowlist entry, not the stored one",
                 "status": homoglyph.status_code, "code": "", "refused": False, "ok": True,
                 "note": "the comparison is on the stored string, so `0xb…b0` and `0xb…bO` are two rows"})
    if 200 <= homoglyph.status_code < 300:
        hid = str((homoglyph.json() or {}).get("addressId") or (homoglyph.json() or {}).get("id") or "")
        mismatch = withdraw(totp(a["totp_secret"]) if a.get("totp_secret") else "000000",
                            new_id or bfix["address_id"], typed=confusable)
        rec("a typed destination that is not the stored one is refused", mismatch, True)

    # ---- IDOR fuzz on the id-bearing routes -------------------------------------------------------
    hostile = ["", "0", "-1", "999999999999999999999999", "';DROP TABLE users;--", "../../etc/passwd",
               "%00", "null", "a" * 300, "‮1", "1 OR 1=1", "{}", "$ne"]
    fuzz_bad, fuzz_codes = [], {}
    for op in ("GET /v1/orders/intents/{intent_id}", "GET /v1/wallet/deposit/{deposit_id}",
               "GET /v1/radar/runs/{job_id}"):
        method, path = op.split(" ", 1)
        for value in hostile:
            url = path.replace(path[path.index("{") + 1:path.index("}")], value)
            r = api.client.get(url, headers=api.bearer(api.login(a["uid"])))
            fuzz_codes[r.status_code] = fuzz_codes.get(r.status_code, 0) + 1
            if r.status_code >= 500 or hits_in(r, api.needles(bfix)):
                fuzz_bad.append((op, value[:12], r.status_code))
    g.check("hostile ids on the id-bearing routes are answered, never crashed (%d requests, %s)"
            % (sum(fuzz_codes.values()), fuzz_codes), not fuzz_bad, "5xx or leak: %s" % fuzz_bad[:6])
    g.canary("fuzz canary: a route that 500s on a hostile id is caught",
             [(m, v, 500) for m, v, c in [("x", "y", 500)]], "a 500 nobody noticed")

    g.check("every authentication probe answered the way it had to (%d probes, %d wrong)"
            % (len(rows), len(refusals)), not refusals, "wrong: %s" % refusals[:6])
    return {"probes": rows, "accepted_wrongly": refusals, "fuzz_codes": fuzz_codes, "notes": explanations}


# ----------------------------------------------------------------------------------------------- the gate
class Gate:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []

    def check(self, name: str, ok: bool, why: str = "") -> bool:
        self.results.append(("PASS" if ok else "FAIL", name, "" if ok else why))
        return ok

    def canary(self, name: str, caught, what_broke: str) -> bool:
        """A canary fires when the check *would* have caught the broken input — i.e. the catch is non-empty."""
        return self.check(name, bool(caught) or caught is True, "the check accepted %s" % what_broke)

    @property
    def failed(self):
        return [r for r in self.results if r[0] == "FAIL"]


SECTIONS = ("registry", "docs", "anonymous", "escalation", "needles", "own", "foreign", "auth")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P14 D1's authorisation matrix")
    ap.add_argument("--only", default="", help="comma-separated sections: %s" % ", ".join(SECTIONS))
    ap.add_argument("--record")
    ap.add_argument("--json", dest="json_path")
    args = ap.parse_args(argv)
    want = {s.strip() for s in args.only.split(",") if s.strip()} or set(SECTIONS)
    unknown = want - set(SECTIONS)
    if unknown:
        raise SystemExit("unknown section(s): %s" % ", ".join(sorted(unknown)))

    g = Gate()
    api = Api()
    a = api.account("a")
    b = api.account("b")
    # A is a *fully qualified* attacker: an account with a working second factor. Without it, every 2FA-gated
    # probe is refused for the trivial reason (this account has no authenticator) and would "pass" while proving
    # nothing about ownership. The RFC's published vectors are checked first, because a harness that computes
    # wrong codes would enrol nothing and quietly weaken every drill that follows.
    rfc_secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    vectors = (totp(rfc_secret, 59_000) == "287082", totp(rfc_secret, 1_111_111_109_000) == "081804",
               totp(rfc_secret, 1_234_567_890_000) == "005924")
    g.check("the harness's own TOTP implementation matches RFC 6238's vectors", all(vectors),
            "vectors: %s" % (vectors,))
    api.qualify(a, address="0x" + "a" * 40, tag="a")
    api.qualify(b, address="0x" + "9" * 40, tag="b")
    a_totp = api.enroll_totp(a)
    g.check("the attack account holds a working second factor (%s)" % ("enrolled" if a_totp["ok"] else
                                                                       a_totp.get("why", "")), a_totp["ok"],
            "A cannot pass a 2FA gate, so the 2FA-gated probes would be refused for the wrong reason")
    bfix = api.stranger_fixtures(b)
    bfix["token"] = b["token"]
    seeded = api.seed_via_api(b, bfix)
    bfix["_seeds_skipped"] = seeded.pop("_seeds_skipped", [])
    bfix.update(seeded)
    facts: dict = {"accounts": {"a": a["uid"], "b": b["uid"]}, "b_seed": {k: v for k, v in bfix.items()
                                                                          if k != "token"}}

    if "registry" in want:
        facts["registry"] = section_registry(api, g)
        reg = facts["registry"]
    else:
        from polygm_core.security import authz
        reg = {"levels": {o: (authz.lookup(o)[1] if authz.lookup(o) else None) for o in served_ops(api)}}
    if "docs" in want:
        facts["docs"] = section_docs(api, g)
    if "anonymous" in want:
        facts["anonymous"] = section_anonymous(api, reg, g)
    if "escalation" in want:
        facts["escalation"] = section_escalation(api, a, reg, g)
    if "needles" in want:
        facts["needles"] = section_needles(api, a, b, bfix, reg, g)
    if "foreign" in want:
        facts["foreign"] = section_foreign(api, a, b, bfix, reg, g)
    if "auth" in want:
        facts["auth"] = section_auth(api, a, b, bfix, g)
    if "own" in want:
        facts["own_access"] = section_own_access(api, a, reg, g)

    # The last thing before the report, and the reason every "refused" above can be believed: both accounts must
    # still be able to log in and read their own money. A run where a section logged an account out would
    # otherwise report refusals that were really 401s.
    alive = {}
    for who, acct in (("A", a), ("B", b)):
        tok = api.fresh_login(acct["uid"])
        r = api.client.get("/v1/wallet/balance", headers=api.bearer(tok))
        alive[who] = r.status_code
    facts["sessions_alive"] = alive
    g.check("both accounts still hold a working session at the end of the run (%s)" % alive,
            all(200 <= v < 300 for v in alive.values()),
            "a section destroyed a session mid-run, so its 401s were not refusals: %s" % alive)

    lines = ["P14 D1 — authorisation matrix (production identity shape: PGM_REQUIRE_SECURITY_ENV=1)", "=" * 96, ""]
    for status, name, why in g.results:
        lines.append("%-4s %s" % (status, name + (("  — " + why) if why else "")))
    failed = g.failed
    lines += ["", "findings by expected loss:"]
    findings = []
    def add(kind: str, count: int, detail: str = "") -> None:
        if count:
            findings.append("%-4s %-18s %s%s" % ("FAIL", kind, SEV[kind], ("  — " + detail) if detail else ""))
    add("undeclared", len(facts.get("registry", {}).get("undeclared", [])))
    add("docs_exposed", len(facts.get("registry", {}).get("docs_ops", []))
        + len(facts.get("docs", {}).get("exposed", [])))
    add("drift_row", len(facts.get("registry", {}).get("drift", [])))
    add("anonymous_2xx", len(facts.get("anonymous", {}).get("anonymous_2xx_where_not_public", [])))
    add("escalation", len(facts.get("escalation", {}).get("reached_by_user", []))
        + len(facts.get("escalation", {}).get("wrong_token_accepted", [])))
    add("cross_user_read", len(facts.get("needles", {}).get("leaks", []))
        + len(facts.get("foreign", {}).get("leaked", [])))
    add("cross_user_write", len(facts.get("foreign", {}).get("accepted", []))
        + len(facts.get("foreign", {}).get("mutated", [])))
    lines += findings or ["     none"]
    lines += ["", "P14 AUTHORISATION MATRIX: %s — %d checks passed, %d failed"
              % ("PASS" if not failed else "FAIL", len(g.results) - len(failed), len(failed))]
    text = "\n".join(lines) + "\n"
    print(text)
    if args.record:
        pathlib.Path(args.record).write_text(text)
    if args.json_path:
        pathlib.Path(args.json_path).write_text(json.dumps(
            {"verdict": "PASS" if not failed else "FAIL", "facts": facts,
             "checks": [{"status": s, "name": n, "why": w} for s, n, w in g.results]}, indent=2, default=str) + "\n")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
