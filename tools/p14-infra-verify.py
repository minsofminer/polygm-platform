#!/usr/bin/env python3
"""P14 D4 — infra verification: egress, runtime hardening, the database, a tested restore, IAM/MFA, headers.

    python3 tools/p14-infra-verify.py --record docs/verification/P14-infra-verify.txt --json …json

The kit's list, and what each item can honestly mean from this machine:

  egress          "the executor's subnet egress allowlist, proved by trying". The executor is not deployed and
                  there is no subnet to try from, so this section proves the half that exists — every outbound
                  destination in the product is a *configured* one, and no route fetches a caller-supplied URL —
                  and records the deployed-subnet drill as OPEN. A pass here means "no SSRF surface and one
                  allowlist to write", not "the firewall was tested".
  runtime         no shell, non-root, read-only: read from the Dockerfiles and compose, and the image-level proof
                  (exec into a container) is OPEN because there is no docker here.
  database        "the DB is not public", asked of the provider's own API rather than of the documentation: every
                  database on every account this deployment uses, with its network restrictions.
  restore         **a restore that actually runs**: back up the migrated database, restore it elsewhere, and check
                  the restored copy answers the same money-path questions. The managed-Postgres PITR drill is OPEN.
  iam             MFA on the accounts that can deploy this product. GitHub's API states it; the others are OPEN
                  with the check written down, because an MFA status nobody verified is not an MFA control.
  headers         live: the deployed API and Mini App, cache-busted — CSP, HSTS, frame-ancestors, no wildcard CORS,
                  the interactive surface, and the spoofed-identity probe that found the worst defect of this phase.

Every network call is made from this tool rather than typed into a doc, so "we checked" is re-runnable, and the
identity probe is deliberately a permanent check: it is the one that caught a live authorisation hole.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from importlib.util import module_from_spec, spec_from_file_location

ROOT = pathlib.Path(__file__).resolve().parents[1]
VERIF = ROOT / "docs" / "verification"
SECRETS = pathlib.Path.home() / ".secrets" / "tokens.env"
API_LIVE = "https://polygm-api.vercel.app"
TMA_LIVE = "https://polygm-mini-app.vercel.app"


def _load(name: str, path: pathlib.Path):
    spec = spec_from_file_location(name, path)
    mod = module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class Gate:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []

    def check(self, name: str, ok: bool, why: str = "") -> bool:
        self.results.append(("PASS" if ok else "FAIL", name, "" if ok else why))
        return bool(ok)

    def open(self, name: str, why: str) -> None:
        self.results.append(("OPEN", name, why))

    @property
    def failed(self):
        return [r for r in self.results if r[0] == "FAIL"]

    @property
    def opened(self):
        return [r for r in self.results if r[0] == "OPEN"]


def env() -> dict[str, str]:
    out = dict(os.environ)
    if SECRETS.exists():
        for line in SECRETS.read_text().splitlines():
            m = re.match(r"^\s*(?:export\s+)?([A-Z0-9_]+)=(.*)$", line)
            if m:
                out[m.group(1)] = m.group(2).strip().strip("\"'")
    return out


def http(url: str, *, headers: dict | None = None, method: str = "GET", timeout: int = 20,
         body: bytes | None = None) -> tuple[int, dict, str]:
    """(status, headers, body). Never raises: a network failure is a fact this tool reports."""
    req = urllib.request.Request(url, headers=headers or {}, method=method, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:                  # noqa: S310 - fixed https URLs
            return r.status, {k.lower(): v for k, v in dict(r.headers).items()}, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in dict(e.headers).items()}, e.read().decode("utf-8", "replace")
    except Exception as e:                                                       # noqa: BLE001
        return 0, {}, "network error: %s: %s" % (type(e).__name__, e)


# ---------------------------------------------------------------------------------------------- 1. egress
#: Every place the product makes an outbound HTTP request, and where its URL comes from. Written out because "we
#: have no SSRF surface" is a claim, and the only way to keep it true is to be able to read the list.
OUTBOUND = (
    ("packages/polygm_core/config/*.py", "the venue base URLs (Gamma, CLOB, websocket) come from configuration"),
    ("services/ingest/net.py", "the ingest client fetches the configured endpoints only"),
    ("services/executor-mock/transport.py", "the CI transport talks to PGM_MOCK_CLOB_URL"),
    ("packages/polygm_core/security/telegram.py", "nothing: this module verifies, it does not fetch"),
)


def section_egress(g: Gate, facts: dict) -> None:
    hits = []
    for pattern in ("urllib.request.urlopen", "urlopen(", "httpx.get(", "httpx.post(", "requests.get(",
                    "requests.post(", "aiohttp"):
        p = subprocess.run(["grep", "-rn", "--include=*.py", pattern, "services", "packages"],
                           cwd=str(ROOT), capture_output=True, text=True)
        for line in p.stdout.splitlines():
            if "tools/" in line:
                continue
            hits.append(line.strip())
    facts["egress"] = {"call_sites": hits[:20], "count": len(hits)}
    # A caller-supplied URL is what SSRF needs. The API must not have one: no route takes a url parameter.
    bad = [h for h in hits if re.search(r"(url|uri)\s*=\s*(body|params|request|query)", h, re.I)]
    g.check("no outbound call site builds its URL from a request value (%d call site(s) total)" % len(hits),
            not bad, "call sites taking a caller-supplied URL: %s" % bad[:3])
    # The vendor endpoints for the *ingest* side live in `services/ingest/main.py` (module constants, which is
    # where the P04 review put them), and the API's egress is the venue client's, configured through the same
    # constants. The first version of this check only read `packages/polygm_core/config/`, found no URLs at all,
    # and failed — a check that fails on a correct tree is a check somebody turns off, so it reads both places.
    text = "\n".join(f.read_text() for f in (ROOT / "packages" / "polygm_core" / "config").glob("*.py"))
    text += "\n" + (ROOT / "services" / "ingest" / "main.py").read_text()
    urls = sorted(set(re.findall(r'"?((?:https|wss)://[a-z0-9.-]+)', text)))
    plain_http = sorted(set(re.findall(r'"http://(?!(?:127\.0\.0\.1|localhost))[a-z0-9.-]+', text)))
    facts["egress"]["config_urls"] = urls[:10]
    facts["egress"]["plain_http"] = plain_http
    g.check("every configured vendor endpoint is https/wss (%s)%s"
            % (", ".join(u.split("//")[1] for u in urls[:4]),
               " and any plain-http one is loopback" if not plain_http else ""),
            len(urls) > 0 and not plain_http,
            "non-https vendor endpoints: %s" % plain_http)
    g.open("DEPLOYED-SUBNET EGRESS IS UNPROVEN: the executor is not deployed and there is no subnet to try from, "
           "so the firewall allowlist has not been tested by trying",
           "when the executor lands: from inside its subnet, curl a non-allowlisted host and a non-allowlisted "
           "port and record both refusals (the kit's 'proved by trying'), plus an allowed vendor call")


# -------------------------------------------------------------------------------------------- 2. runtime
def section_runtime(g: Gate, facts: dict) -> None:
    facts["runtime"] = {}
    for f in sorted(ROOT.glob("**/Dockerfile*"), key=str):
        if "node_modules" in str(f):
            continue
        text = f.read_text()
        name = str(f.relative_to(ROOT))
        checks = {
            "non_root_user": bool(re.search(r"^USER\s+(?!root)\S+", text, re.M)),
            "no_login_shell": "-s /usr/sbin/nologin" in text,
            "no_added_packages": "apt-get" not in text and "apk add" not in text,
            "no_credential_copy": not re.search(r"COPY\s+.*(\.env|id_rsa|\.pem|credential)", text, re.I),
        }
        facts["runtime"][name] = checks
        g.check("%s: the app user has no login shell" % name, checks["no_login_shell"],
                "the user keeps `/bin/sh` from the base image's useradd default")
        g.check("%s: no additional OS packages are installed" % name, checks["no_added_packages"],
                "an apt/apk install adds attack surface the base image did not ship")
    compose = (ROOT / "docker-compose.yml").read_text()
    api_block = compose[compose.index("  api:"):] if "  api:" in compose else compose
    facts["runtime"]["compose_api"] = {
        "read_only": "read_only: true" in api_block,
        "cap_drop_all": bool(re.search(r"cap_drop:\s*\n?\s*-\s*ALL|cap_drop:\s*\[ALL\]", api_block)),
        "no_new_privileges": "no-new-privileges:true" in api_block.replace(" ", ""),
        "user": bool(re.search(r"user:\s*[\"']?\d+", api_block)),
    }
    c = facts["runtime"]["compose_api"]
    g.check("the api container is read-only, drops all capabilities and cannot gain privileges",
            c["read_only"] and c["cap_drop_all"] and c["no_new_privileges"], json.dumps(c))
    code, _out = subprocess.run(["which", "docker"], capture_output=True, text=True).returncode, None
    if code != 0:
        g.open("NO IMAGE-LEVEL PROOF OF THE RUNTIME HARDENING: no docker here, so the Dockerfiles and compose are "
               "read as text; nobody has yet run `id` inside the built image or tried to write to `/`",
               "run the kit's own probes on the built image: `docker run … id` (expect uid 10001, not 0), "
               "`touch /srv/app/x` (expect permission denied), `nc -l` (absent), `getent passwd polygm` (expect "
               "nologin), then record the output")


# ------------------------------------------------------------------------------------------- 3. database
def section_database(g: Gate, facts: dict) -> None:
    """Ask the provider, not the doc. Every database reachable with these credentials, and its restrictions."""
    e = env()
    facts["database"] = {"providers": {}}
    tok = e.get("SUPABASE_ACCESS_TOKEN")
    if not tok:
        g.open("DATABASE EXPOSURE UNCHECKED: no Supabase token in ~/.secrets, so nothing was asked",
               "re-run with SUPABASE_ACCESS_TOKEN set")
    else:
        status, _h, body = http("https://api.supabase.com/v1/projects",
                                headers={"Authorization": "Bearer %s" % tok})
        projects = json.loads(body) if status == 200 else []
        facts["database"]["providers"]["supabase"] = [{"id": p.get("id"), "name": p.get("name"),
                                                       "region": p.get("region")} for p in projects]
        # The failure has three causes and they are not the same thing, so the message names which one it is: a
        # credential that is missing (handled above), a credential the provider rejects (401 — rotate it; P16 found
        # this token dead after it had worked during P14, and a check that only printed "status 401" left the
        # operator to guess whose problem it was), or an account that genuinely has no projects.
        hint = ("the token in ~/.secrets/tokens.env is rejected by the provider: rotate it and re-run. The check "
                "cannot see the database this product will use until it does, so the D4 verdict stays FAIL"
                if status in (401, 403) else body[:160])
        g.check("the Supabase account was queried and lists %d project(s)" % len(projects), status == 200
                and bool(projects), "status %d: %s" % (status, hint))
        for p in projects:
            ref = p.get("id")
            st2, _h2, b2 = http("https://api.supabase.com/v1/projects/%s/network-restrictions" % ref,
                                headers={"Authorization": "Bearer %s" % tok})
            cfg = (json.loads(b2).get("config") or {}) if st2 == 200 else {}
            v4 = cfg.get("dbAllowedCidrs") or []
            v6 = cfg.get("dbAllowedCidrsV6") or []
            open_wide = any(c in ("0.0.0.0/0", "::/0") for c in list(v4) + list(v6))
            facts["database"]["providers"].setdefault("supabase_cidrs", {})[ref] = {"v4": v4, "v6": v6,
                                                                                   "status": st2}
            g.check("database %s (%s) is not open to the internet (v4=%s v6=%s)"
                    % (p.get("name"), ref, v4, v6), not open_wide,
                    "0.0.0.0/0 means every host on the internet can reach the Postgres port; the password is then "
                    "the only control and the log fills with credential-stuffing attempts")
    # Nothing else on this account is a production database for *this* product, and that is worth stating.
    names = [p.get("name") for p in facts["database"]["providers"].get("supabase", [])]
    if names and not any("polygm" in str(n).lower() or "openout" in str(n).lower() for n in names):
        g.open("NO PRODUCTION DATABASE FOR THIS PRODUCT EXISTS YET: the deployed API runs the SQLite demo engine "
               "in /tmp (`api/index.py`), so there is nothing to be public or private",
               "when the real Postgres lands: create it with network restrictions set to the app's egress IPs "
               "(never 0.0.0.0/0), enable PITR, and re-run this section — the check above then applies to it")


# -------------------------------------------------------------------------------------------- 4. restore
def section_restore(g: Gate, facts: dict) -> None:
    """A restore that actually runs: back up, restore elsewhere, and ask the restored copy the same questions."""
    drills = _load("p14_drills_for_infra", ROOT / "tools" / "p14-key-drills.py")
    with contextlib.redirect_stdout(io.StringIO()):
        bench = drills.Bench(kek_versions=1)
    live_db = pathlib.Path(os.environ["PGM_DB_PATH"])
    backup = live_db.with_name("restore-drill-backup.db")
    restored = live_db.with_name("restore-drill-restored.db")
    for p in (backup, restored):
        if p.exists():
            p.unlink()

    def counts(path: pathlib.Path) -> dict:
        con = sqlite3.connect(str(path))
        try:
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'"
                                               " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            return {t: int(con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]) for t in tables}
        finally:
            con.close()

    t0 = time.monotonic()
    con = sqlite3.connect(str(live_db))
    try:
        # `VACUUM INTO` is a real logical backup: consistent, and it does not copy a possibly-hot WAL the way a
        # file copy does. (For Postgres this is pg_dump/PITR; the drill shape is the same, the tool differs.)
        con.execute("VACUUM INTO ?", (str(backup),))
    finally:
        con.close()
    backup_ms = int((time.monotonic() - t0) * 1000)
    g.check("a consistent backup of the migrated database was taken (%d ms, %d bytes)"
            % (backup_ms, backup.stat().st_size if backup.exists() else 0),
            backup.exists() and backup.stat().st_size > 0, "no backup file")

    t1 = time.monotonic()
    shutil.copy2(backup, restored)
    before, after = counts(live_db), counts(restored)
    restore_ms = int((time.monotonic() - t1) * 1000)
    facts["restore"] = {"backup_ms": backup_ms, "restore_ms": restore_ms, "tables": len(after),
                        "rows": sum(after.values()), "mismatches": {k: (before.get(k), after.get(k))
                                                                   for k in set(before) | set(after)
                                                                   if before.get(k) != after.get(k)}}
    g.check("the restored copy holds every table and every row (%d tables, %d rows, %d ms)"
            % (len(after), sum(after.values()), restore_ms), before == after,
            "row-count mismatches: %s" % json.dumps(facts["restore"]["mismatches"])[:200])

    # A restore that copies rows but does not answer the *product's* questions is not a restore. These are the
    # money-path reads the API makes on every page load, run against the restored file.
    def money_answers(path: pathlib.Path) -> dict:
        con = sqlite3.connect(str(path))
        try:
            q = lambda sql: [tuple(r) for r in con.execute(sql)]                        # noqa: E731
            return {
                "balances": q("SELECT user_id, usdc_available_micro, usdc_locked_micro FROM balances"
                             " ORDER BY user_id"),
                "ledger_sum": q("SELECT COALESCE(SUM(amount_micro),0) FROM cash_ledger"),
                "open_orders": q("SELECT COUNT(*) FROM orders WHERE state IN ('live','partial')"),
                "intents": q("SELECT state, COUNT(*) FROM order_intents GROUP BY state ORDER BY state"),
                "key_wraps": q("SELECT user_id, dek_version, kek_version FROM key_wraps ORDER BY user_id"),
            }
        finally:
            con.close()

    same = money_answers(live_db) == money_answers(restored)
    facts["restore"]["money_answers_identical"] = same
    g.check("the restored copy answers the money-path reads identically (balances, ledger sum, open orders, "
            "intents, key wraps)", same, "a money-path query differs between the live and restored databases")

    # ...and the API's own schema guard accepts it, which is what a real failover does first.
    con = sqlite3.connect(str(restored))
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    required = ("users", "markets", "tokens", "book_levels", "tape_trades", "order_intents", "orders",
                "idempotency_keys", "cash_ledger", "kill_switch_state", "feature_flags")
    missing = [t for t in required if t not in tables]
    g.check("the restored database satisfies the API's boot schema guard", not missing, "missing: %s" % missing)
    for p in (backup, restored):
        with contextlib.suppress(OSError):
            p.unlink()
    g.open("MANAGED-POSTGRES RESTORE UNPROVEN: the drill above is the SQLite twin, which is what this environment "
           "runs; no restore has been performed against the managed Postgres that production will use",
           "on the managed instance: take a PITR snapshot, restore to a scratch branch at a known timestamp, run "
           "the same money-path queries, and record the wall-clock time and the recovery point")


# ------------------------------------------------------------------------------------------------ 5. IAM
def section_iam(g: Gate, facts: dict) -> None:
    e = env()
    facts["iam"] = {}
    gh = e.get("GH_TOKEN") or e.get("GITHUB_TOKEN")
    if gh:
        status, _h, body = http("https://api.github.com/user", headers={"Authorization": "token %s" % gh,
                                                                       "User-Agent": "polygm-p14"})
        d = json.loads(body) if status == 200 else {}
        scopes = ""
        st2, h2, _b = http("https://api.github.com/user", headers={"Authorization": "token %s" % gh,
                                                                  "User-Agent": "polygm-p14"})
        scopes = h2.get("x-oauth-scopes", "")
        facts["iam"]["github"] = {"login": d.get("login"), "two_factor_authentication":
                                  d.get("two_factor_authentication"), "token_scopes": scopes}
        g.check("the GitHub account that owns this repository has MFA enabled (%s)"
                % (d.get("login") or "unknown"), bool(d.get("two_factor_authentication")),
                "two_factor_authentication is false: the account that can rewrite every commit, and whose token "
                "holds admin:org and delete_repo, is protected by a password alone")
        risky = [s for s in scopes.split(",") if s.strip() in ("admin:org", "delete_repo", "admin:public_key")]
        facts["iam"]["github"]["over_scoped"] = risky
        # Not a failure by itself — it is the standing state the owner chose — but it has to be *written down*,
        # because this token is pasted into CI and a leaked CI secret with delete_repo is a repo-deletion event.
        g.check("the token scopes are recorded, and the dangerous ones are named (%s)"
                % (", ".join(risky) or "none of admin:org/delete_repo"), True,
                "")
    else:
        g.open("GITHUB MFA UNCHECKED: no token available", "set GH_TOKEN and re-run")
    for name, why in (("VERCEL_TOKEN", "Vercel account (deploys the API and the Mini App)"),
                      ("SUPABASE_ACCESS_TOKEN", "Supabase account (will hold the production database)"),
                      ("RAILWAY_TOKEN", "Railway account (compose/host)")):
        if not e.get(name):
            continue
        g.open("%s: MFA could not be read from %s's API" % (name, why),
               "the provider exposes no MFA field on the endpoints this token can reach; check it in the "
               "dashboard and record the answer in docs/P14-security-testing.md (an MFA status nobody verified "
               "is not an MFA control)")


# --------------------------------------------------------------------------------------------- 6. headers
def section_headers(g: Gate, facts: dict) -> None:
    cb = str(int(time.time()))
    facts["headers"] = {}
    status, h, body = http("%s/healthz?cb=%s" % (API_LIVE, cb))
    facts["headers"]["api"] = {"status": status, "headers": {k: h.get(k) for k in
                                                            ("content-security-policy", "strict-transport-security",
                                                             "x-content-type-options", "referrer-policy",
                                                             "access-control-allow-origin", "server")}}
    if status == 0:
        g.open("THE DEPLOYED API DID NOT ANSWER: %s" % body[:120],
               "live header and identity checks could not run; re-run when the deployment is reachable")
        return
    api = facts["headers"]["api"]["headers"]
    g.check("the deployed API sends a CSP with frame-ancestors 'none'",
            "frame-ancestors 'none'" in (api["content-security-policy"] or ""), json.dumps(api)[:200])
    g.check("the deployed API sends HSTS with preload",
            "preload" in (api["strict-transport-security"] or ""), str(api["strict-transport-security"]))
    g.check("no wildcard CORS on the API (%s)" % (api["access-control-allow-origin"] or "absent"),
            api["access-control-allow-origin"] in (None, ""), "a wildcard origin would let any site read it")
    # The interactive surface, live: the P14 D1 finding, as a permanent check.
    docs = {}
    for path in ("/docs", "/redoc", "/openapi.json"):
        st, _h, _b = http("%s%s?cb=%s" % (API_LIVE, path, cb))
        docs[path] = st
    facts["headers"]["docs"] = docs
    g.check("the deployed API serves no interactive surface (%s)" % docs,
            all(v == 404 for v in docs.values()), json.dumps(docs))
    # The dependency-identity probe. This is the check that caught the live hole; it stays.
    spoof = {}
    for path in ("/v1/referrals/me", "/v1/auth/sessions", "/v1/wallet/balance"):
        st, _h, b = http("%s%s?cb=%s" % (API_LIVE, path, cb), headers={"X-User-Id": "u-demo"})
        spoof[path] = {"status": st, "code": (json.loads(b).get("error") or {}).get("code")
                       if b.startswith("{") and "error" in b else None}
    facts["headers"]["spoofed_identity"] = spoof
    g.check("the deployed API refuses a spoofed X-User-Id header on every user route (%s)"
            % json.dumps({k: v["status"] for k, v in spoof.items()}),
            all(v["status"] == 401 for v in spoof.values()),
            "a 200 here means anyone on the internet can read any account by setting one header")
    st, _h, b = http("%s/v1/orders?cb=%s" % (API_LIVE, cb), method="POST",
                     headers={"Content-Type": "application/json", "Idempotency-Key": "p14-infra-%s" % cb},
                     body=json.dumps({"marketId": "0xM1", "tokenId": "0xT10", "side": "BUY", "price": "0.5",
                                      "size": "5"}).encode())
    facts["headers"]["anonymous_order"] = {"status": st, "body": b[:140]}
    g.check("an anonymous writer is refused with an identity answer, not a retryable signing error (%s)" % st,
            st == 401, "expected 401 UNAUTHENTICATED; got %d %s" % (st, b[:120]))
    st2, h2, _b2 = http("%s/?cb=%s" % (TMA_LIVE, cb))
    tma = {k: h2.get(k) for k in ("content-security-policy", "strict-transport-security",
                                  "access-control-allow-origin")}
    facts["headers"]["mini_app"] = {"status": st2, "headers": tma}
    g.check("the Mini App sends a CSP that frames only Telegram, and no wildcard CORS",
            st2 == 200 and "frame-ancestors" in (tma["content-security-policy"] or "")
            and tma["access-control-allow-origin"] in (None, ""), json.dumps(tma)[:220])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P14 D4 — infra verification")
    ap.add_argument("--record")
    ap.add_argument("--json", dest="json_path")
    ap.add_argument("--only", default="", help="comma-separated: egress,runtime,database,restore,iam,headers")
    args = ap.parse_args(argv)
    want = [s.strip() for s in args.only.split(",") if s.strip()] or \
        ["egress", "runtime", "database", "restore", "iam", "headers"]
    g = Gate()
    facts = {"at_ms": int(time.time() * 1000), "api_live": API_LIVE, "mini_app_live": TMA_LIVE}
    sections = {"egress": section_egress, "runtime": section_runtime, "database": section_database,
                "restore": section_restore, "iam": section_iam, "headers": section_headers}
    for name in want:
        sections[name](g, facts)
    failed, opened = g.failed, g.opened
    verdict = "FAIL" if failed else ("CONDITIONAL" if opened else "PASS")
    lines = ["P14 D4 — infra verification: egress, runtime, database, restore, IAM, headers", "=" * 96, ""]
    for status, name, why in g.results:
        lines.append("%-4s %s%s" % (status, name, ("\n        — " + why) if why else ""))
    passed = len(g.results) - len(failed) - len(opened)
    lines += ["", "P14 INFRA: %s — %d checks passed, %d failed, %d OPEN" % (verdict, passed, len(failed),
                                                                            len(opened))]
    if opened:
        lines += ["", "OPEN (not passes, not failures — measurements this machine cannot take):"]
        lines += ["  * %s\n      %s" % (n, w) for _s, n, w in opened]
    text = "\n".join(lines) + "\n"
    print(text)
    if args.record:
        pathlib.Path(args.record).write_text(text)
    if args.json_path:
        pathlib.Path(args.json_path).write_text(json.dumps(
            {"verdict": verdict, "open_conditions": [{"check": n, "why": w} for _s, n, w in opened],
             "facts": facts, "checks": [{"status": s, "name": n, "why": w} for s, n, w in g.results]},
            indent=2, default=str) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
