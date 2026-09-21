"""P14 D3 · the SAST findings that were triaged rather than fixed, each with its reason.

Found and fixed in D3 instead: `assert` used as control flow on the money path
(`packages/polygm_core/risk/limits.py`, `packages/polygm_core/copy/engine.py`) — those are exceptions now, so
`python -O` cannot remove them. The retest is
`tests/test_security_plane.py::TestOptimisedBuildIsNotADifferentProduct`.

What is left is the list below. It is not a burial ground: the scan fails on a finding that is *not* in this
file, and fails again on an entry whose finding has gone. A triage entry therefore has to be deleted the moment
its code stops producing the shape, which is what keeps the file from turning into a list of things somebody
once worried about.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: One reason per (rule, file) — the reasons are facts about the *mechanism*, and they were arrived at by reading
#: every distinct construction site rather than by counting them: 61 S608 findings in `services/` and `packages/`
#: are 7 files and about a dozen helpers, most of the sites being callers of the same two functions.
REASONS: dict[tuple[str, str], dict[str, str]] = {
    ("S608", "services/api/seed.py"): {
        "why": "fixture SQL in the seed script: every statement is a literal, and the `%s` interpolation is a run "
               "of `?` placeholders sized from a literal tuple (`markers`), never a value",
        "by": "P14 D3 review"},
    ("S608", "services/api/seed_leaderboard.py"): {
        "why": "same pattern as seed.py: literal SQL, `%s` used only for `?` placeholder runs",
        "by": "P14 D3 review"},
    ("S608", "services/api/app.py"): {
        "why": "query text is assembled from module-level constant fragments (`_discovery_inner`, `_MARKET_SORT`, "
               "`RESOLVED_SQL`, column-name tuples) and every value is a bind parameter; where `%s` appears it is "
               "a run of `?` placeholders (`qs`, `marks`) sized by len(). The identifiers that do reach the SQL "
               "come from whitelists (`sort_by` is validated against `_MARKET_SORT` before use, `direction` is a "
               "literal from that table). Runtime evidence: the D1 matrix's IDOR fuzz sends 39 hostile ids and "
               "every answer is 404 or 422 with no 5xx, and D3's DAST probe sends `sortBy=../../etc/passwd` with "
               "an HTML payload and gets neither a 500 nor the payload echoed",
        "by": "P14 D3 review"},
    ("S608", "services/ingest/main.py"): {
        "why": "fixed statement text with `?` placeholders; the one interpolation is a table name from a literal "
               "tuple of the three tables the ingester owns",
        "by": "P14 D3 review"},
    ("S608", "services/executor/store.py"): {
        "why": "`%s` is a run of `?` placeholders for an IN-list, and the column list is a literal; the executor "
               "binds every value",
        "by": "P14 D3 review"},
    ("S608", "packages/polygm_core/security/store.py"): {
        "why": "the interpolation chooses between two literal WHERE clauses (include_revoked); no value is "
               "interpolated and both parameters are bound",
        "by": "P14 D3 review"},
    ("S608", "packages/polygm_core/automation/facts.py"): {
        "why": "`marks` is `\",\".join(\"?\" * len(WALLET_LABELS))` — a placeholder run sized from a module "
               "constant, with the labels themselves bound",
        "by": "P14 D3 review"},
    ("S110", "packages/polygm_core/config/flags.py"): {
        "why": "the flag poller keeps the last good snapshot when a reload fails, and the age of that snapshot is "
               "observable (`age_ms`) and reported by readyz — the alternative (raising) turns a database blip "
               "into a 500 on every feature-gated route",
        "by": "P14 D3 review"},
    ("S110", "services/api/app.py"): {
        "why": "three sites, all deliberate and commented in place: closing an already-closed connection, reading "
               "flags at boot (a DB down at boot must not stop the process — readyz reports boot_defaults_used), "
               "and syncing the route-level mirror at boot (a mirror must never block a boot). None of the three "
               "can hide a money-path failure, and each has an observable state instead of a log line",
        "by": "P14 D3 review"},
    ("S110", "services/ingest/main.py"): {
        "why": "closing the websocket during shutdown: the process is going away and a close() that raises has "
               "nowhere useful to report to",
        "by": "P14 D3 review"},
    ("S101", "services/executor-mock/mock_clob.py"): {
        "why": "the CI venue double asserts the executor stamped a client order hash before posting. It is test "
               "scaffolding (never imported by a service), and the property is asserted for real against the "
               "executor in tests/test_executor_service.py, where the hash is read back out of the database",
        "by": "P14 D3 review"},
    ("S104", "services/executor-mock/mock_clob.py"): {
        "why": "the mock venue's HTTP server binds all interfaces so a container in the compose network can reach "
               "it. It is the CI double for Polymarket, never a deployed service: compose publishes no port for "
               "it, and the real venue is reached over the public internet by the executor",
        "by": "P14 D3 review"},
    ("S310", "services/executor-mock/transport.py"): {
        "why": "the mock transport fetches from its own local double at a URL built from configuration, not from a "
               "request; there is no user-controlled URL on this path. The SSRF-relevant surface is the API's "
               "image proxy, which is not this code and is probed in D1's injection section",
        "by": "P14 D3 review"},
    ("S105", "packages/polygm_core/referrals/code.py"): {
        "why": "`TOKEN_PREFIX = \"ref_\"` is a public prefix, not a credential — it is the part of a referral code "
               "that users are meant to read (and that the account page shows). The secret part is 22 characters "
               "of `secrets.token_urlsafe`",
        "by": "P14 D3 review"},
    ("S105", "services/api/app.py"): {
        "why": "`TG_WEBHOOK_SECRET_ENV` and `TG_SECRET_HEADER` are the *names* of the environment variable and the "
               "header the Telegram webhook secret lives in. Reading them as secrets is the false positive the "
               "word 'secret' in an identifier always produces",
        "by": "P14 D3 review"},
    ("S105", "services/api/seed.py"): {
        "why": "`NOW_TOKEN` is the fixed clock value the seed script writes into fixtures so every environment's "
               "demo data is identical; it is a timestamp, not a token",
        "by": "P14 D3 review"},
    ("S107", "services/executor/store.py"): {
        "why": "`token: str = \"pUSD\"` is the *symbol* of the settlement token the allowance table is keyed by, "
               "with the default being the only token the ledger currently knows. Verified by reading the call "
               "sites: every one passes a symbol, and no credential is defaulted anywhere in this module",
        "by": "P14 D3 review"},
    ("S310", "services/ingest/net.py"): {
        "why": "this is the ingest client's one HTTP call, and the URL is not user-controlled: it is built from "
               "the configured Gamma/CLOB base URLs in `polygm_core/config`, with the path segment taken from a "
               "literal endpoint table. The rule fires because the scheme is not restricted to https at the call "
               "site; the *guard* is upstream (the config refuses a non-https base URL for a production engine), "
               "and this module is the only place in the product that fetches a remote document without one",
        "by": "P14 D3 review"},
    ("S311", "services/ingest/net.py"): {
        "why": "`random.uniform` jitters the ingest retry backoff — a scheduling decision, not a security one. "
               "The verification that matters is that nothing security-shaped uses this module: tokens, nonces, "
               "session ids and the KEK generator all come from `secrets` (checked by the same review, and by "
               "P07's own gate)",
        "by": "P14 D3 review"},
}


def build() -> dict:
    code = subprocess.run(["ruff", "check", "--select", "S", "--no-cache", "--output-format", "concise",
                           "services", "packages"], cwd=ROOT, capture_output=True, text=True)
    entries, missing = [], []
    # A regex, not `split(":")`: concise output separates the location with colons and the *message* usually
    # contains one too ("assigned to: ..."), so splitting silently misses every finding whose text has a colon —
    # which is how the first version of this generator reported four rules as untriaged while printing reasons
    # for them two lines below.
    pat = re.compile(r"^([^:]+):(\d+):(\d+): ([A-Z]\d{3}) (.*)$")
    for line in code.stdout.splitlines():
        m = pat.match(line.strip())
        if not m:
            continue
        f, _ln, _col, rule, message = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5).strip()
        reason = REASONS.get((rule, f))
        if not reason:
            missing.append("%s %s" % (rule, f))
            continue
        entries.append({"key": "%s|%s|%s" % (rule, f, message[:160]), "code": rule, "file": f,
                        "message": message[:160], "why": reason["why"], "by": reason["by"]})
    if missing:
        raise SystemExit("\n".join(sorted(set(missing))))
    return {"note": __doc__.strip().splitlines()[0], "generated_by": "tools/build-sast-triage.py",
            "rule": "an entry is deleted when its finding stops being reported; the scan fails on both a new "
                    "finding and a stale entry",
            "entries": entries}


if __name__ == "__main__":
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "tools/sast-triage.json")
    out.write_text(json.dumps(build(), indent=2) + "\n")
    print("%s: %d entries" % (out, len(json.loads(out.read_text())["entries"])))
