#!/usr/bin/env python3
"""P10 Quality Gate — the terminal.

Same rule as every other phase's gate in this repo: a check either EXECUTES (a real HTTP probe against the
seeded app, a parser over the real tree, a subprocess) or it does not go in the list.

The phase's acceptance line is:

    a new user can find a whale fill in the tape -> open the trader's profile -> see the win rate is real
    (sample >= gate) -> see their drawdown -> set up a copy config in dry-run -> understand the slippage risk,
    *without reading documentation*.

That sentence is not decoration here: c9 walks exactly that path over the API and fails if any step is missing,
because a chain verified by reading six endpoints separately is a chain nobody has ever walked.

Four properties every check is held to, from the standing rules:

  1. **no address ever leaves** — c2 scans every P10 payload for a `0x…` shape. The pseudonym is the API's
     answer; an address in a response is the venue's answer, and one of those is a leak;
  2. **a win rate without its sample gate is a lie, and a PnL curve without its drawdown is a forecast** — c3
     checks both directions, on live payloads;
  3. **a control that cannot fail is a costume** — `--self-test` plants each violation the scanners look for and
     fails if a scan walks past it;
  4. **the rule travels with the number** — c4 requires the whale threshold's own sentence (`whale = max(…)`)
     on every row that carries a badge, because a badge with no visible rule is an accusation.

`--fast` skips c1 (which runs `tools/check-openapi.py`, itself a live-app comparison) and c7 (the P08 money-path
scanner over the terminal files); everything else is a few hundred milliseconds of HTTP.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".tmp"
for _p in (str(ROOT / "packages"), str(ROOT / "services" / "api"), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
DOC = ROOT / "docs" / "P10-frontend-terminal.md"
PERF = ROOT / "docs" / "verification" / "P10-perf.txt"
#: What the tape's frame budget is measured against. 16.6 ms is one frame at 60fps; the tape is allowed a
#: fraction of it per second of load, and the artefact records what it actually spent.
FRAME_MS = 1000 / 60
PERF_SOURCES = ("web/src/terminal/tape.ts", "web/scripts/measure-tape-budget.mjs",
                "web/src/terminal/perf.test.ts")
CONTRACT = ROOT / "contracts" / "openapi.yaml"
METRICS = ROOT / "packages" / "polygm_core" / "terminal" / "metrics.py"
PY = sys.executable

#: The paths this phase serves. c1 requires each of them in the contract AND in `tools/check-openapi.py`'s table
#: (`TABLE_FOR_PATH`), because a path missing from that table is a path whose status sets are never compared.
P10_PATHS = ("/v1/tape/fills", "/v1/tape/facets", "/v1/whales", "/v1/traders/{anon}", "/v1/copy/configs",
             "/v1/copy/configs/guards", "/v1/copy/configs/monitor", "/v1/copy/sources", "/v1/me/portfolio",
             "/v1/whale-views", "/v1/automations", "/v1/automations/runs", "/v1/automations/templates",
             "/v1/alerts", "/v1/alerts/deliveries")

#: The four reads the phase's own doc calls public, and the seven that are the caller's own state.
PUBLIC_OPS = ("GET /v1/tape/fills", "GET /v1/tape/facets", "GET /v1/whales", "GET /v1/traders/{anon}")
USER_OPS = ("GET /v1/copy/configs", "POST /v1/copy/configs", "POST /v1/copy/configs/guards",
            "GET /v1/copy/sources",
            "GET /v1/copy/configs/monitor", "GET /v1/me/portfolio", "GET /v1/whale-views",
            "POST /v1/whale-views",
            "GET /v1/automations", "POST /v1/automations", "POST /v1/automations/preview",
            "POST /v1/automations/guards", "GET /v1/automations/runs", "GET /v1/automations/templates",
            "GET /v1/alerts", "POST /v1/alerts", "POST /v1/alerts/test", "GET /v1/alerts/deliveries",
            "POST /v1/alerts/settings")

#: Anything that looks like an Ethereum address. Deliberately loose: 6+ hex chars after `0x` catches a
#: truncated address too, and a false positive here is a sentence to rewrite, not a leak.
ADDRESS_RX = re.compile(r"\b0x[0-9a-fA-F]{6,}\b")


def sh(argv, cwd: Path = ROOT, timeout: int = 900) -> tuple[int, str]:
    e = dict(os.environ)
    e["TMPDIR"] = str(TMP)
    e["CI"] = "1"
    TMP.mkdir(exist_ok=True)
    try:
        p = subprocess.run(argv, cwd=str(cwd), env=e, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "(timed out after %ds)" % timeout
    except FileNotFoundError as f:
        return 127, "cannot run %s: %s" % (argv[0], f)
    return p.returncode, p.stdout + p.stderr


def read(path: Path) -> str:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return ""


# ---------------------------------------------------------------------------------------------- the live probe
class Probe:
    """The seeded API, on a throwaway database, driven through the real HTTP layer.

    One app per gate run, lazily built: the probe is the only thing in this file that needs `fastapi` installed,
    and a missing dependency must fail the checks that need it rather than the ones that do not.
    """

    def __init__(self) -> None:
        self._app = None
        self._client = None
        self._user = None

    def app(self):
        if self._app is None:
            sys.path.insert(0, str(ROOT / "tests"))
            import conftest                                                # noqa: PLC0415
            self._app = conftest.import_app("p10-gate")
        return self._app

    def client(self):
        if self._client is None:
            from fastapi.testclient import TestClient                     # noqa: PLC0415
            self._client = TestClient(self.app().app, raise_server_exceptions=False)
        return self._client

    def user_headers(self) -> dict:
        """The fixture account's identity, via the same dev-header path the API's own tests use.

        Six of the nine surfaces are USER-scoped; a gate that probed them anonymously would be testing the 401
        path nine times and calling it coverage of the terminal.
        """
        if self._user is None:
            self._user = {"X-User-Id": "u-demo"}
        return self._user

    def get(self, url: str, **params) -> tuple[int, dict]:
        r = self.client().get(url, params=params, headers=self.user_headers())
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    def post(self, url: str, body: dict, **kw) -> tuple[int, dict]:
        """POST the way the contract says a client must: with an `Idempotency-Key`.

        Every P10 mutation documents the header as required and answers 400 without it (the rule this gate
        itself helped enforce: the routes accepted a header-less POST until the radar's tests found the hole).
        A probe that omitted the key would be measuring the 400 path and calling the copy config "not a dry
        run", which is exactly what happened the first time this ran after the fix.
        """
        self._post_n = getattr(self, "_post_n", 0) + 1
        headers = {**self.user_headers(), "Idempotency-Key": "g10-%s-%04d" % (url.strip("/").replace("/", "-"), self._post_n)}
        r = self.client().post(url, json=body, headers=headers, **kw)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    def rows(self, sql: str, args: tuple = ()) -> list:
        return self.app()._db.execute(sql, args).fetchall()


# --------------------------------------------------------------------------------------------------- scanners
def address_findings(payload, where: str = "payload") -> list:
    """Every `0x…`-shaped string anywhere in a payload, as `where.path[:60]` sentences.

    Written as a pure function over a decoded payload so `--self-test` can plant an address in a dict and watch
    this catch it — a scanner that only runs against a live API is a scanner nobody can prove is running.
    """
    f = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, "%s.%s" % (path, k))
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, "%s[%d]" % (path, i))
        elif isinstance(node, str) and ADDRESS_RX.search(node):
            f.append("%s %s carries an address-shaped string: %s" % (where, path, node[:60]))

    walk(payload, "$")
    return f


def wallet_field_findings(payload) -> list:
    """Every field that NAMES a wallet must carry a pseudonym. A wallet name that is not `w_…` is either an
    address (c2's other half) or an id we invented and can no longer join to a trader."""
    f = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k.lower() in ("anonwallet", "sourceanon", "anon", "wallet") and isinstance(v, str) and v:
                    if not v.startswith("w_"):
                        f.append("%s is not a pseudonym: %s" % (path + "." + k, v[:40]))
                walk(v, "%s.%s" % (path, k))
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, "%s[%d]" % (path, i))

    walk(payload, "$")
    return f


def drawdown_findings(dossier: dict) -> list:
    """A curve point without its drawdown, a drawdown that is not peak−cum, or a negative one.

    The arithmetic is recomputed here rather than trusted: the point of the overlay is that it is the same
    number the chart draws, and a gate that reads `drawdownMicro` without checking it is reading a claim.
    """
    f = []
    points = dossier.get("curve") or []
    if not points:
        f.append("the dossier has no curve at all: a PnL surface with no curve cannot carry a drawdown")
    peak = 0
    for i, p in enumerate(points):
        cum = p.get("cumMicro")
        if not isinstance(cum, int) or isinstance(cum, bool):
            f.append("point %d: cumMicro is %r, not an integer" % (i, cum))
            continue
        peak = max(peak, cum)
        for key in ("peakMicro", "drawdownMicro"):
            if not isinstance(p.get(key), int) or isinstance(p.get(key), bool):
                f.append("point %d: %s is %r" % (i, key, p.get(key)))
        if p.get("peakMicro") != peak:
            f.append("point %d: peakMicro %r is not the running high-water mark %d" % (i, p.get("peakMicro"), peak))
        if p.get("drawdownMicro") != peak - cum:
            f.append("point %d: drawdownMicro %r != peak − cum %d" % (i, p.get("drawdownMicro"), peak - cum))
        if isinstance(p.get("drawdownMicro"), int) and p["drawdownMicro"] < 0:
            f.append("point %d: drawdownMicro is negative (%d)" % (i, p["drawdownMicro"]))
    if not isinstance(dossier.get("maxDrawdownMicro"), int):
        f.append("maxDrawdownMicro is missing: the summary figure and the curve must come from one computation")
    return f


def win_rate_findings(metrics: dict, where: str = "metrics") -> list:
    """Every window: a gated win rate, or an explicit refusal. Never a percentage below the gate."""
    f = []
    gate = metrics.get("sampleGate")
    for window, m in (metrics.get("metrics") or {}).items():
        if not isinstance(m, dict):
            f.append("%s.%s is not an object" % (where, window))
            continue
        bps, insufficient, note = m.get("winRateBps"), m.get("insufficientSample"), m.get("sampleNote")
        if m.get("resolvedMarkets", 0) < (gate or 20):
            if bps is not None:
                f.append("%s.%s: a win rate (%r) below the sample gate of %s" % (where, window, bps, gate))
            if not insufficient or not note:
                f.append("%s.%s: below the gate with no insufficientSample/sampleNote" % (where, window))
        elif bps is not None and insufficient:
            f.append("%s.%s: a win rate AND insufficientSample" % (where, window))
        for key in ("realisedMicro", "unrealisedMicro", "volumeMicro", "maxDrawdownMicro"):
            if not isinstance(m.get(key), int) or isinstance(m.get(key), bool):
                f.append("%s.%s.%s is %r, not an integer micro amount" % (where, window, key, m.get(key)))
    return f


def threshold_findings(rows: list, floor_micro: int, where: str = "rows") -> list:
    """`isWhale` agrees with the threshold it shipped, the threshold is at or above the floor, and the RULE
    travels with it."""
    f = []
    for i, r in enumerate(rows):
        t = r.get("thresholdMicro")
        if not isinstance(t, int) or isinstance(t, bool):
            f.append("%s[%d]: thresholdMicro is %r, not an integer" % (where, i, t))
            t = None
        elif t < floor_micro:
            # The row is still checked for the two things a bad threshold does not excuse: the floor is one
            # finding, and `isWhale` disagreeing with the threshold it shipped is a different one.
            f.append("%s[%d]: thresholdMicro %r is below the absolute floor %d" % (where, i, t, floor_micro))
        if t is not None and r.get("isWhale") is not (r.get("notionalMicro", 0) >= t):
            f.append("%s[%d]: isWhale disagrees with notionalMicro %r vs threshold %d"
                     % (where, i, r.get("notionalMicro"), t))
        rule = str(r.get("thresholdRule") or "")
        if "whale = max(" not in rule or "absolute floor" not in rule:
            f.append("%s[%d]: the badge has no rule sentence (tooltip): %r" % (where, i, rule[:50]))
        if not str(r.get("rule") or "").strip():
            f.append("%s[%d]: the severity bands are not stated on the row" % (where, i))
    return f


def freshness_findings(payload: dict, where: str = "payload") -> list:
    """Every read carries `asOf`, `serverAsOf` and `staleAfter`, and the data is not from the future.

    `asOf` is the age of the DATA (a fill's timestamp) and `serverAsOf` is when we answered: the two are
    different clocks, and a payload where the data is newer than the answer is a payload from a machine whose
    clocks disagree.
    """
    f = []
    for key in ("asOf", "serverAsOf", "staleAfter"):
        if not isinstance(payload.get(key), int):
            f.append("%s: %s is %r" % (where, key, payload.get(key)))
    if all(isinstance(payload.get(k), int) for k in ("asOf", "serverAsOf", "staleAfter")):
        if payload["asOf"] > payload["serverAsOf"] + 1_000:
            f.append("%s: asOf %d is after serverAsOf %d by more than a second"
                     % (where, payload["asOf"], payload["serverAsOf"]))
        ttl = (payload.get("cache") or {}).get("ttlMs", 0)
        if payload["staleAfter"] < payload["asOf"]:
            f.append("%s: staleAfter %d is BEFORE asOf %d" % (where, payload["staleAfter"], payload["asOf"]))
        elif ttl and payload["staleAfter"] <= payload["asOf"]:
            # a cacheable read must be usable for its ttl; a no-store read (ttl 0) is stale the instant it is
            # answered, and saying so is the point of the stamp rather than a missing one
            f.append("%s: ttlMs %d but staleAfter %d is not after asOf %d"
                     % (where, ttl, payload["staleAfter"], payload["asOf"]))
    if not payload.get("cache") or "ttlMs" not in payload["cache"]:
        f.append("%s: no cache block (ttlMs) - the client cannot tell a cached read from a live one" % where)
    return f


def float_findings(text: str, where: str) -> list:
    """Float arithmetic in a money or metric path: a literal with a decimal point in an expression, `float(`,
    `round(`, or a `/ 1e`/`* 1e` scale. Comments are stripped first - this repo has spent a phase learning that
    a scanner without a comment stripper reports the sentence that documents the rule."""
    body = re.sub(r'"""(?:.|\n)*?"""', "", text)      # docstrings: prose that documents the rule
    body = re.sub(r'"(?:[^"\n]|\\.)*"', '""', body)    # string literals: sentences the UI renders, not maths
    body = re.sub(r"'(?:[^'\n]|\\.)*'", "''", body)
    body = re.sub(r"(?m)#.*$", "", body)              # trailing comments: they explain rules, they are not code
    f = []
    for pat, why in ((r"\bfloat\s*\(", "a float() conversion"),
                     (r"\bround\s*\(", "round(): rounding a micro integer is a lost unit of money"),
                     (r"[*/]\s*1e\d", "an exponent scale factor"),
                     (r"\b\d+\.\d+(?!\d)", "a decimal literal")):
        m = pat and re.search(pat, body)
        if m:
            f.append("%s: %s at %r" % (where, why, body[max(0, m.start() - 30):m.start() + 30].replace("\n", " ")))
    return f


# ---------------------------------------------------------------------------------------------------- checks
def c1_contract(p: Probe) -> tuple[str, bool, str]:
    """The contract, the router and the authz table agree about P10's fifteen surfaces (D1-D9)."""
    rc, out = sh([PY, "tools/check-openapi.py"], timeout=600)
    tail = [l for l in out.splitlines() if l.startswith("check-openapi:")]
    ok_openapi = rc == 0 and any(" 0 failed" in l for l in tail)
    yaml_text = read(CONTRACT)
    checker = read(ROOT / "tools" / "check-openapi.py")
    missing_contract = [path for path in P10_PATHS if ("  %s:" % path) not in yaml_text]
    # A path is in the table when it appears as its own key, either bare (`"/v1/x": ...` — one response set for
    # every verb) or inside a verb-keyed tuple (`("POST", "/v1/x"): ...`, which exists because a GET and a POST on
    # the same path answer different status sets). Both forms compare the served status sets; only a path absent
    # from both is unguarded. The D9 paths are verb-keyed, which is why this accepts either spelling.
    missing_table = [path for path in P10_PATHS
                     if not any(('"%s"%s' % (path, tail)) in checker for tail in (":", ")", ","))]
    p.app()                     # app.py is what applies the P10 rows to the registry; import it first
    from polygm_core.security import authz                                        # noqa: PLC0415
    public = [op for op in PUBLIC_OPS if authz.LEVELS_TABLE.get(op, ("",))[0] == authz.PUBLIC]
    user = [op for op in USER_OPS if authz.LEVELS_TABLE.get(op, ("",))[0] == authz.USER]
    ok = (ok_openapi and not missing_contract and not missing_table
          and len(public) == len(PUBLIC_OPS) and len(user) == len(USER_OPS))
    return ("the contract, the router and the authz table agree on P10's fifteen surfaces", ok,
            "%s; paths in the contract %d/%d, in TABLE_FOR_PATH %d/%d; public %d/%d, user %d/%d"
            % (tail[-1] if tail else out.strip()[-90:], len(P10_PATHS) - len(missing_contract), len(P10_PATHS),
               len(P10_PATHS) - len(missing_table), len(P10_PATHS), len(public), len(PUBLIC_OPS), len(user),
               len(USER_OPS)))


def c2_no_addresses(p: Probe) -> tuple[str, bool, str]:
    """Nine surfaces, one rule: a pseudonym or nothing.

    Every wallet-graph read is fetched, plus a fresh config creation (the response that names a source), and all
    of them are scanned for a `0x…` shape. The fixture's wallets are real hex addresses in the database, so a
    single `SELECT wallet` rendered into a payload fails here.
    """
    code, facets = p.get("/v1/tape/facets", windowMs=86_400_000)
    code2, fills = p.get("/v1/tape/fills", limit=200, windowMs=86_400_000)
    code3, whales = p.get("/v1/whales", windowMs=86_400_000)
    anon = ""
    for row in (fills.get("rows") or []):
        anon = row.get("anonWallet") or ""
        if anon:
            break
    code4, dossier = p.get("/v1/traders/%s" % anon)
    code5, configs = p.get("/v1/copy/configs")
    code6, monitor = p.get("/v1/copy/configs/monitor", configId="cfg-seed01")
    code7, portfolio = p.get("/v1/me/portfolio")
    code8, views = p.get("/v1/whale-views")
    code9, created = p.post("/v1/copy/configs", {"sourceAnon": anon, "maxOrderMicro": 100_000_000,
                                                 "maxDailyMicro": 400_000_000})
    codes = [code, code2, code3, code4, code5, code6, code7, code8]
    payloads = {"facets": facets, "fills": fills, "whales": whales, "trader": dossier, "configs": configs,
                "monitor": monitor, "portfolio": portfolio, "views": views, "created": created}
    findings = []
    for name, payload in payloads.items():
        findings += address_findings(payload, name)
        findings += wallet_field_findings(payload)
    ok = (not findings and all(c == 200 for c in codes) and created.get("dryRun") is True
          and (fills.get("rows") or [{}])[0].get("anonWallet", "").startswith("w_"))
    return ("no P10 payload carries an address, and every wallet field is a pseudonym", ok,
            "%d payloads scanned (%s); %d findings%s"
            % (len(payloads), "all 200" if all(c == 200 for c in codes) else "statuses %s" % codes,
               len(findings), ("; " + "; ".join(findings[:3])) if findings else ""))


def c3_gate_and_drawdown(p: Probe) -> tuple[str, bool, str]:
    """The win rate is gated in all four windows and every curve point is a drawdown point.

    The fixture is built so that the 7-day window is UNDER the gate and the 30-day window is over it: a gate that
    is never crossed is a component nobody has seen work, so the check requires one of each.
    """
    _code, facets = p.get("/v1/tape/facets", windowMs=86_400_000)
    anon = ((facets.get("wallets") or [{}])[0] or {}).get("anonWallet", "")
    _code2, dossier = p.get("/v1/traders/%s" % anon, window="30d")
    findings = win_rate_findings(dossier) + drawdown_findings(dossier)
    windows = dossier.get("metrics") or {}
    under = [w for w, m in windows.items() if m.get("winRateBps") is None and m.get("insufficientSample")]
    over = [w for w, m in windows.items() if m.get("winRateBps") is not None]
    labels = dossier.get("behaviour") or []
    for lab in labels:
        for key in ("rule", "disclaimer"):
            if not str(lab.get(key) or "").strip():
                findings.append("label %s has no %s" % (lab.get("label"), key))
    if any(lab.get("label") == "insider_suspect" for lab in labels):
        findings.append("insider_suspect is published: the classifier marks it unpublishable")
    method = dossier.get("methodology") or {}
    for key in ("winRate", "realised", "unrealised", "drawdown", "hold"):
        if not str(method.get(key) or "").strip():
            findings.append("methodology.%s is missing: a metric with no stated method is a number" % key)
    ok = (not findings and bool(under) and bool(over) and len(dossier.get("curve") or []) >= 2
          and str(method.get("path", "")).startswith("/methodology"))
    return ("every win rate is gated, every curve point carries its drawdown, and the method is stated", ok,
            "windows: %d under the gate (%s), %d over it (%s); curve points %d; max drawdown %s; %d findings%s"
            % (len(under), ",".join(sorted(under)) or "-", len(over), ",".join(sorted(over)) or "-",
               len(dossier.get("curve") or []), dossier.get("maxDrawdownMicro"), len(findings),
               ("; " + "; ".join(findings[:3])) if findings else ""))


def c4_whale_rule(p: Probe) -> tuple[str, bool, str]:
    """The threshold is relative with an absolute fallback, the reason is stated, and severity is a ratio."""
    from polygm_core.terminal import metrics as tm                                   # noqa: PLC0415
    floor = tm.bucket_floor_micro(tm.median_micro([])) if False else tm.WHALE_FLOOR_MICRO
    _c1, facets = p.get("/v1/tape/facets", windowMs=86_400_000)
    _c2, tape = p.get("/v1/tape/fills", limit=200, windowMs=86_400_000)
    _c3, whales = p.get("/v1/whales", windowMs=86_400_000)
    _c4, multi = p.get("/v1/whales", windowMs=86_400_000, multiple=5)
    threshold = (facets.get("whale") or {}).get("thresholdMicro")
    findings = threshold_findings(tape.get("rows") or [], floor, "tape")
    findings += threshold_findings((whales.get("rows") or []), floor, "whales")
    if not isinstance(threshold, int) or threshold < floor:
        findings.append("facets.whale.thresholdMicro %r is below the floor %d" % (threshold, floor))
    rule = str((facets.get("whale") or {}).get("rule") or "")
    if "whale = max(" not in rule or "absolute floor" not in rule:
        findings.append("facets.whale.rule does not state the rule: %r" % rule[:60])
    if "absolute_fallback" not in json.dumps(facets.get("markets") or []) and not any(
            m.get("thresholdReason") == "absolute_fallback" for m in (facets.get("markets") or [])):
        findings.append("no market in the fixture states the absolute_fallback reason: the thin-window path is "
                        "never exercised")
    for row in (whales.get("rows") or []):
        sev = row.get("severity")
        ratio = row.get("ratioBps")
        if not isinstance(ratio, int) or ratio != row.get("notionalMicro", 0) * 10_000 // max(1, row["thresholdMicro"]):
            findings.append("severity ratioBps %r is not notional // threshold" % ratio)
        want = ("urgent" if ratio >= tm.SEVERITY_URGENT_BPS else
                "notice" if ratio >= tm.SEVERITY_NOTICE_BPS else "info")
        if sev != want:
            findings.append("severity %r should be %r at %d bps" % (sev, want, ratio))
    multi_rules = [v.get("rule") for v in (multi.get("thresholds") or {}).values() if v.get("reason") == "relative"]
    if multi_rules and not any("5×" in str(r) for r in multi_rules):
        findings.append("multiple=5 does not state the 5× it used: %r" % multi_rules[:1])
    absolute_only = p.get("/v1/tape/fills", windowMs=86_400_000, minNotionalMicro=floor)[1]
    if any(r["notionalMicro"] < floor for r in (absolute_only.get("rows") or [])):
        findings.append("minNotionalMicro does not filter: a row below the floor came back")
    severities = {r.get("severity") for r in (whales.get("rows") or [])}
    ok = (not findings and {"urgent", "notice"} <= severities if severities else True) and not findings
    return ("the whale threshold is relative-with-a-floor, stated on every row, and severity is a ratio", ok,
            "floor %d, window threshold %s (reason %s); %d whale rows, severities %s; %d findings%s"
            % (floor, threshold, (facets.get("whale") or {}).get("reason"), len(whales.get("rows") or []),
               ",".join(sorted(severities)) or "-", len(findings),
               ("; " + "; ".join(findings[:3])) if findings else ""))


def c5_copy_safety(p: Probe) -> tuple[str, bool, str]:
    """A config cannot be created live, going live needs BOTH an acknowledgement and dry-run history, and the
    create response already carries the slippage warning the confirm dialog must show."""
    _c0, facets = p.get("/v1/tape/facets", windowMs=86_400_000)
    anon = ((facets.get("wallets") or [{}])[0] or {}).get("anonWallet", "")
    findings = []
    code, created = p.post("/v1/copy/configs", {"sourceAnon": anon, "maxOrderMicro": 100_000_000,
                                                "maxDailyMicro": 400_000_000})
    cid = created.get("configId")
    if code != 200 or not cid:
        findings.append("creating a config failed: %d %s" % (code, json.dumps(created)[:120]))
    if created.get("dryRun") is not True:
        findings.append("a new config is not a dry run: dryRun=%r" % created.get("dryRun"))
    warn = (created.get("warning") or {})
    if not str(warn.get("warning") or "").strip() or "slippage" not in str(warn.get("warning", "")).lower():
        findings.append("the create response carries no slippage sentence")
    if warn.get("default") != "skip_instead_of_chase":
        findings.append("the default policy is not stated: %r" % warn.get("default"))
    stats = (created.get("sourceStats") or {})
    if "risk-adjusted" not in str(stats.get("ranking", "")):
        findings.append("sourceStats does not say how sources are ranked")
    if stats.get("windows") and not any(w.get("netAfterFeesMicro", 0) < 0 for w in stats["windows"]):
        findings.append("no losing window in the source's record: the honest half of the record is missing")
    live_field = p.post("/v1/copy/configs", {"sourceAnon": anon, "maxOrderMicro": 100_000_000,
                                             "maxDailyMicro": 400_000_000, "dryRun": False})
    if live_field[0] != 422:
        findings.append("a `dryRun` field on create is not refused (status %d)" % live_field[0])
    no_ack = p.post("/v1/copy/configs/guards", {"configId": cid, "dryRun": False})
    if no_ack[0] != 409 or "acknowledgeSlippage" not in json.dumps(no_ack[1]):
        findings.append("going live without the acknowledgement answered %d %s"
                        % (no_ack[0], json.dumps(no_ack[1])[:80]))
    ack = p.post("/v1/copy/configs/guards", {"configId": cid, "dryRun": False, "acknowledgeSlippage": True})
    if ack[0] != 409 or "dry-run history" not in json.dumps(ack[1]):
        findings.append("going live with no dry-run history answered %d %s"
                        % (ack[0], json.dumps(ack[1])[:80]))
    con = p.app()._db
    con.execute("INSERT INTO copy_dry_runs (config_id,copier_id,source_user_id,source_intent_id,market_id,"
                "would_action,would_size_micro,would_price_micro,source_price_micro,deviation_bps,reason,at_ms)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, "u-demo", "0x" + "bb" * 20, "src-gate", "0xM9", "enter", 10_000_000, 42_000, 41_000, 24,
                 "would copy", p.app()._now_ms()))
    con.commit()
    went_live = p.post("/v1/copy/configs/guards", {"configId": cid, "dryRun": False, "acknowledgeSlippage": True})
    if went_live[0] != 200 or went_live[1].get("dryRun") is not False or not went_live[1].get("enabled"):
        findings.append("with history and the acknowledgement it did not go live: %d %s"
                        % (went_live[0], json.dumps(went_live[1])[:90]))
    _cm, monitor = p.get("/v1/copy/configs/monitor", configId="cfg-seed01")
    if not (monitor.get("wouldDo") and monitor.get("live")):
        findings.append("the monitor does not separate simulations from fills")
    for row in (monitor.get("live") or []):
        if row.get("dryRun") is not False:
            findings.append("a live row is not labelled dryRun=false")
    for row in (monitor.get("wouldDo") or []):
        if row.get("dryRun") is not True:
            findings.append("a simulated row is not labelled dryRun=true")
    if not (monitor.get("skips") and "moved_past_limit" in (monitor.get("skipReasons") or [])):
        findings.append("skips carry no reasons: watch-only without a why is a mystery")
    ok = not findings
    return ("a copy config is a dry run by construction, and going live needs an acknowledgement AND history", ok,
            "created %s, live-field %d, no-ack %d, ack-only %d, with-history %d; monitor live %d / wouldDo %d, "
            "skip reasons %s; %d findings%s"
            % (cid, live_field[0], no_ack[0], ack[0], went_live[0], len(monitor.get("live") or []),
               len(monitor.get("wouldDo") or []), ",".join(sorted(monitor.get("skipReasons") or [])) or "-",
               len(findings), ("; " + "; ".join(findings[:3])) if findings else ""))


def c6_views_and_alerts(p: Probe) -> tuple[str, bool, str]:
    """A view that notifies needs a target, and the rule it creates carries a fire budget.

    `alert_rules` has a `rule_has_target` CHECK (P04). A tracker screen that offers a channel on a global view
    is offering something the database will refuse five layers down, so the API refuses it with the reason.
    """
    _c, facets = p.get("/v1/tape/facets", windowMs=86_400_000)
    market = ((facets.get("markets") or [{}])[0] or {}).get("marketId", "")
    findings = []
    code, scoped = p.post("/v1/whale-views", {"name": "gate scoped", "marketId": market, "channel": "telegram",
                                              "minSeverity": "notice", "firesPerWindow": 2,
                                              "ruleWindowMs": 3_600_000})
    if code != 200 or not scoped.get("ruleId") or scoped.get("notifies") is not True:
        findings.append("a notifying market-scoped view did not create its rule: %d %s"
                        % (code, json.dumps(scoped)[:100]))
    budget = p.rows("SELECT fires_per_window, window_ms FROM alert_rules WHERE id=?", (scoped.get("ruleId"),))
    if not budget or budget[0][0] != 2:
        findings.append("the rule row has no fire budget: %s" % (budget,))
    global_notify = p.post("/v1/whale-views", {"name": "gate global", "channel": "email"})
    if global_notify[0] != 409 or "market" not in json.dumps(global_notify[1]):
        findings.append("a notifying global view answered %d %s"
                        % (global_notify[0], json.dumps(global_notify[1])[:90]))
    plain = p.post("/v1/whale-views", {"name": "gate filter", "minNotionalMicro": 500_000_000})
    if plain[0] != 200 or plain[1].get("notifies") is not False:
        findings.append("a global FILTER-only view should save without notifying: %d" % plain[0])
    unknown = p.post("/v1/whale-views", {"name": "gate unknown", "marketId": "0xNOPE"})
    if unknown[0] != 404:
        findings.append("an unknown marketId answered %d, not 404" % unknown[0])
    _l, listed = p.get("/v1/whale-views")
    for item in (listed.get("items") or []):
        if item.get("notifies") and not item.get("ruleId"):
            findings.append("a view claims to notify with no rule")
        if item.get("scope") == "market" and not item.get("marketId"):
            findings.append("a market-scoped view with no market")
    ok = not findings
    return ("a notifying view carries a market target and its rule ships with a fire budget", ok,
            "scoped rule %s (fires %s), global-notify %d, filter-only %d, unknown market %d, %d views listed; "
            "%d findings%s"
            % (scoped.get("ruleId"), budget[0][0] if budget else "-", global_notify[0], plain[0], unknown[0],
               len(listed.get("items") or []), len(findings),
               ("; " + "; ".join(findings[:3])) if findings else ""))


def c7_integers_only(p: Probe) -> tuple[str, bool, str]:
    """The terminal's money path is integer arithmetic, and the wire's money is strings.

    Two halves, both executed: a scan of `terminal/metrics.py` (the module every one of these numbers comes
    through) plus the P08 gate's own money-path scanner over the P10 files, and a look at a live payload to
    confirm that the *strings* on the wire are decimal strings rather than JSON numbers.
    """
    findings = float_findings(read(METRICS), "metrics.py")
    gate_p08 = None
    try:
        spec = importlib.util.spec_from_file_location("pgm_p08_gate", ROOT / "tools" / "p08-gate-check.py")
        gate_p08 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate_p08)
    except Exception as e:                                                       # pragma: no cover
        findings.append("the P08 money scanner could not be loaded: %s" % e)
    if gate_p08 is not None and hasattr(gate_p08, "money_findings"):
        findings += [f for f in gate_p08.money_findings(ROOT / "web") if "terminal" in f]
    _c, tape = p.get("/v1/tape/fills", limit=50, windowMs=86_400_000)
    _c2, dossier = p.get("/v1/traders/%s" % (((tape.get("rows") or [{}])[0].get("anonWallet")) or ""))
    for i, row in enumerate((tape.get("rows") or [])[:50]):
        if not isinstance(row.get("price"), str) or not isinstance(row.get("shares"), str):
            findings.append("row %d: price/shares are not decimal strings (%r/%r)"
                            % (i, row.get("price"), row.get("shares")))
        if not isinstance(row.get("notionalMicro"), int) or isinstance(row.get("notionalMicro"), bool):
            findings.append("row %d: notionalMicro is %r" % (i, row.get("notionalMicro")))
    for pos in (dossier.get("positions") or []):
        if not isinstance(pos.get("size"), str) or not isinstance(pos.get("mark"), str):
            findings.append("position size/mark are not strings: %r/%r" % (pos.get("size"), pos.get("mark")))
    ok = not findings
    return ("the money path is integers, the wire's money is decimal strings", ok,
            "metrics.py %d lines scanned; %d tape rows and %d positions checked; %d findings%s"
            % (len(read(METRICS).splitlines()), len(tape.get("rows") or []), len(dossier.get("positions") or []),
               len(findings), ("; " + "; ".join(findings[:3])) if findings else ""))


def c8_freshness(p: Probe) -> tuple[str, bool, str]:
    """Every P10 read carries the three clock fields, and the public reads carry a cache key."""
    reads = [("/v1/tape/fills", {"windowMs": 86_400_000}), ("/v1/tape/facets", {"windowMs": 86_400_000}),
             ("/v1/whales", {"windowMs": 86_400_000}), ("/v1/copy/configs", {}),
             ("/v1/copy/configs/monitor", {"configId": "cfg-seed01"}), ("/v1/me/portfolio", {}),
             ("/v1/whale-views", {})]
    _c, facets = p.get("/v1/tape/facets", windowMs=86_400_000)
    anon = ((facets.get("wallets") or [{}])[0] or {}).get("anonWallet", "")
    reads.append(("/v1/traders/%s" % anon, {}))
    findings = []
    for url, params in reads:
        code, body = p.get(url, **params)
        if code != 200:
            findings.append("%s answered %d" % (url, code))
            continue
        findings += freshness_findings(body, url)
    ok = not findings
    return ("every P10 read is stamped with the data's clock, not the answer's", ok,
            "%d reads; %d findings%s" % (len(reads), len(findings),
                                         ("; " + "; ".join(findings[:3])) if findings else ""))


def c9_acceptance_path(p: Probe) -> tuple[str, bool, str]:
    """The phase's own acceptance line, walked end to end in one script.

    find a whale -> open the trader -> the win rate is real (or refused with its reason) -> the drawdown is there
    -> create a copy config in dry-run -> the slippage risk is stated BEFORE the confirm. Each step's output is
    the next step's input, exactly as a new user would click it, so a surface that is "documented" but
    unreachable from the previous screen fails here.
    """
    from polygm_core.terminal import metrics as tm                                   # noqa: PLC0415
    steps = []
    _c, facets = p.get("/v1/tape/facets", windowMs=86_400_000)
    threshold = (facets.get("whale") or {}).get("thresholdMicro")
    steps.append("facets state the whale rule (threshold %s: %s)"
                 % (threshold, str((facets.get("whale") or {}).get("rule"))[:60]))
    _c2, filtered = p.get("/v1/tape/fills", windowMs=86_400_000, minNotionalMicro=threshold, limit=20)
    rows = filtered.get("rows") or []
    if not rows:
        return ("a new user can walk whale -> dossier -> dry-run copy -> slippage", False,
                "the tape filter at the stated threshold returned nothing")
    whale = rows[0]
    steps.append("the tape filter at that threshold found %d fill(s); the first is %s at %s"
                 % (len(rows), whale.get("anonWallet"), tm._micro_str(whale.get("notionalMicro", 0))))
    if not whale.get("isWhale"):
        return (steps[-1], False, "a row returned BY the threshold filter is not marked isWhale")
    _c3, dossier = p.get("/v1/traders/%s" % whale["anonWallet"])
    w = (dossier.get("metrics") or {}).get(dossier.get("window"))
    if not isinstance(w, dict):
        return (steps[-1], False, "the dossier carries no metrics for its own default window")
    if w.get("winRateBps") is None:
        if not w.get("insufficientSample") or not str(w.get("sampleNote")).strip():
            return (steps[-1], False, "the win rate is absent with no reason given")
        steps.append("the win rate is REFUSED with its reason: %s" % str(w.get("sampleNote"))[:80])
    else:
        steps.append("the win rate is %s over %d settled markets (gate %s)"
                     % (tm.bps_str(w["winRateBps"]), w.get("resolvedMarkets"), w.get("sampleGate")))
    if not dossier.get("curve"):
        return (steps[-1], False, "the dossier has no curve")
    worst = min((pt["drawdownMicro"] for pt in dossier["curve"]), default=0)
    steps.append("the curve carries its drawdown (worst %s)" % tm._micro_str(worst))
    _c4, created = p.post("/v1/copy/configs", {"sourceAnon": whale["anonWallet"], "mode": "cap",
                                               "maxOrderMicro": 100_000_000, "maxDailyMicro": 400_000_000})
    if created.get("dryRun") is not True:
        return (steps[-1], False, "the copy config created from the dossier is not a dry run")
    steps.append("the copy config %s exists in dry-run" % created.get("configId"))
    warning = str((created.get("warning") or {}).get("warning") or "")
    if "slippage" not in warning.lower() or "skip" not in warning.lower():
        return (steps[-1], False, "the config came back without the slippage warning a confirm dialog needs")
    steps.append("the slippage risk is stated before the confirm: %s" % warning[:90])
    return ("a new user can walk whale -> dossier -> dry-run copy -> slippage, on the API alone", True,
            " | ".join(steps))



def radar_findings(payload: dict) -> list[str]:
    """Wallet Radar (D5): what the payload must say about its own cost and its own gating.

    Four things, and each is a rule the screen depends on: the budget is stated (plan, used, per-day, and
    whether this scan was cached), the four rankings are named with their questions, the profit list contains
    nobody under the sample gate, and every wallet the gate excluded is present with the sentence that says why.
    A payload missing the last one is a screen that shows a shorter list with no explanation.
    """
    out: list[str] = []
    quota = payload.get("quota") or {}
    for key in ("plan", "usedToday", "perDay", "cached", "jobId", "note"):
        if key not in quota:
            out.append("quota.%s is missing" % key)
    if isinstance(quota.get("perDay"), int) and isinstance(quota.get("usedToday"), int) \
            and quota["usedToday"] > quota["perDay"]:
        out.append("usedToday %d is past perDay %d with no refusal" % (quota["usedToday"], quota["perDay"]))
    meta = payload.get("rankingsMeta") or []
    ids = sorted(str(m.get("id")) for m in meta)
    if ids != ["active", "earliest", "overlap", "profit"]:
        out.append("rankingsMeta is %s, not the four rankings" % ids)
    for m in meta:
        if not str(m.get("question") or "").strip():
            out.append("ranking %s has no question" % m.get("id"))
    for row in ((payload.get("rankings") or {}).get("profit") or []):
        if row.get("winRateBps") is None and not row.get("insufficientSample"):
            out.append("profit row %s has neither a win rate nor a refusal" % row.get("anonWallet"))
    for row in payload.get("unranked") or []:
        if not row.get("insufficientSample") or not str(row.get("reason") or "").strip():
            out.append("an unranked wallet (%s) is listed without the gate's sentence" % row.get("anonWallet"))
    if not str(payload.get("costNote") or "").strip():
        out.append("costNote is missing: the cache window and the async line are not stated")
    return out




def c10_radar_cost(p: Probe) -> tuple[str, bool, str]:
    """D5's cost control, as four behaviours rather than as a promise.

    A scan is answered; the SAME scan in a different order is answered from the cache without spending budget; a
    selection past the limit is refused with the limit in the sentence; a big scan becomes a job that runs once;
    and a scan with no `Idempotency-Key` is refused before any of it, because a POST that is idempotent by luck
    is how a retry doubles a purchase.
    """
    rows = p.rows("SELECT m.id, COUNT(*) n FROM tape_fills f JOIN markets m ON m.condition_id=f.condition_id"
                  " GROUP BY m.id HAVING n >= 4 ORDER BY n DESC LIMIT 12")
    markets = [str(r[0]) for r in rows]
    if len(markets) < 12:
        return ("a scan of many markets is refused, cached, and enqueued", False,
                "the seed holds only %d markets with fills" % len(markets))
    steps = []
    code, first = p.post("/v1/radar/runs", {"marketIds": markets[:2]})
    if code != 200:
        return ("a scan of many markets is refused, cached, and enqueued", False,
                "a two-market scan answered %d: %s" % (code, str(first)[:160]))
    findings = radar_findings(first)
    if findings:
        return ("a scan of many markets is refused, cached, and enqueued", False, "; ".join(findings[:3]))
    steps.append("two markets -> 4 rankings; quota %d/%d on the %s plan"
                 % (first["quota"]["usedToday"], first["quota"]["perDay"], first["quota"]["plan"]))
    code, second = p.post("/v1/radar/runs", {"marketIds": list(reversed(markets[:2]))})
    if code != 200 or second.get("quota", {}).get("cached") is not True:
        return (steps[-1], False, "the same scan in a different order was not served from the cache (%d)" % code)
    if second["quota"]["usedToday"] != first["quota"]["usedToday"]:
        return (steps[-1], False, "a cached scan spent budget: %d -> %d"
                % (first["quota"]["usedToday"], second["quota"]["usedToday"]))
    steps.append("the same two markets, reversed, came from the cache and spent nothing")
    code, refused = p.post("/v1/radar/runs", {"marketIds": markets})
    if code != 422 or "up to 10" not in str(refused.get("error", {}).get("message")):
        return (steps[-1], False, "twelve markets answered %d without the limit in the sentence" % code)
    steps.append("twelve markets: 422 with the limit stated (%s)" % refused["error"]["message"][:60])
    code, big = p.post("/v1/radar/runs", {"marketIds": markets[:8]})
    job_id = (big.get("quota") or {}).get("jobId")
    if code != 200 or not job_id:
        return (steps[-1], False, "an eight-market scan was not enqueued as a job (%d, jobId %s)" % (code, job_id))
    code2, done = p.get("/v1/radar/runs/%s" % job_id)
    if code2 != 200 or done.get("status") != "done" or (done.get("quota") or {}).get("jobId") is not None:
        return (steps[-1], False, "the job did not finish on its first poll (%d)" % code2)
    steps.append("eight markets ran as job %s and finished with 4 rankings" % job_id)
    # Deliberately NOT `p.post`: that helper adds the header (as a real client must), and this step is the one
    # place that has to send nothing to prove the 400 body is real.
    code3 = p.client().post("/v1/radar/runs", json={"marketIds": markets[:2]},
                            headers=p.user_headers()).status_code
    if code3 != 400:
        return (steps[-1], False, "a mutation with no Idempotency-Key answered %d, not 400" % code3)
    steps.append("a scan with no Idempotency-Key is 400 before anything runs")
    return ("a scan is cached, budgeted, refused past the limit, enqueued when large, and keyed", True,
            " | ".join(steps))


def perf_findings(text: str, age_note: str = "") -> list:
    """Everything the tape-budget artefact has to say before a 60fps claim can rest on it.

    The requirement is "60fps under live load or the component is not done", and the trap in it is that the
    JavaScript half of a frame is measurable in this environment and the pixel half is not (no browser: the
    Playwright download fails its host-requirements check, which is why P08's numbers are byte counts too). So
    the artefact must state the load it ran, the number it produced, and — in the same file — that paint,
    layout and compositing are `[UNVERIFIED]`. A measurement that quietly upgrades itself to a rendering claim
    is the failure this check exists for, and it is why the caveat is parsed rather than trusted.
    """
    f = []
    if not text.strip():
        return ["docs/verification/P10-perf.txt is missing — `npm run measure:tape` writes it"]
    if "200 fills/second" not in text:
        f.append("the artefact does not state the load it ran (200 fills/second)")
    if "[UNVERIFIED]" not in text or "No browser exists in this environment" not in text:
        f.append("the artefact claims pixels it did not measure: the paint/layout caveat is missing")
    m = re.search(r"mean work per second of load\s+([0-9.]+) ms", text)
    if not m:
        f.append("the mean work per second is not in the artefact, so nothing here is a measurement")
    elif float(m.group(1)) > FRAME_MS:
        f.append("the tape spends %.3f ms per second of load, past one frame (%.1f ms)" % (float(m.group(1)), FRAME_MS))
    m2 = re.search(r"worst single batch release\s+([0-9.]+) ms", text)
    if m2 and float(m2.group(2 - 1)) > FRAME_MS:
        f.append("a single batch release takes %.3f ms, past one frame" % float(m2.group(1)))
    if "status: pass" not in text:
        f.append("the artefact does not say `status: pass`")
    if age_note:
        f.append(age_note)
    return f



def perf_source_hash() -> str:
    """The tape files the frame budget describes — the same three the measure script names, in the same order."""
    import hashlib
    h = hashlib.sha256()
    for rel in sorted(PERF_SOURCES):
        path = ROOT / rel
        # The measure script hashes paths relative to `web/` (it runs from there); the list here is repo-relative.
        # Hashing the two spellings of the same file gives the two sides different digests, which is a bug the
        # comparison reports as drift — so the strip is part of the convention, not a convenience.
        h.update(rel.removeprefix("web/").encode())
        h.update(b"\0")
        h.update(path.read_bytes() if path.exists() else b"")
        h.update(b"\0")
    return h.hexdigest()[:16]


def c11_tape_frame_budget(p: Probe) -> tuple[str, bool, str]:
    """The 60fps line is measured, on the tape's real functions, at 200 fills/s — and the pixels are not claimed."""
    text = PERF.read_text() if PERF.exists() else ""
    # Content, not timestamps. The rule here used to compare mtimes, which a `git checkout` defeats in both
    # directions: it makes an accurate record look stale (every file is touched — this check failed on a clean
    # reset with sources it had never measured differently) and a stale one look current (which is how the P08
    # payload record stayed green for five phases while three routes were over budget). The artefact now states
    # the hash of the files it measured; this recomputes it.
    want = perf_source_hash()
    said = re.search(r"sources-sha256:\s*([0-9a-f]{16})", text)
    note = ""
    if not said:
        note = "the artefact carries no `sources-sha256` line, so nothing in it says which sources it measured"
    elif said.group(1) != want:
        note = ("the measurement describes different sources (it says %s, the tree hashes to %s) — re-run "
                "`npm run measure:tape`" % (said.group(1), want))
    findings = perf_findings(text, note)
    mean = re.search(r"mean work per second of load\s+([0-9.]+) ms", text)
    detail = ("%s ms per second of load at 200 fills/s, worst release %s ms, budget %.1f ms; %d findings%s"
              % (mean.group(1) if mean else "no", (re.search(r"worst single batch release\s+([0-9.]+)", text).group(1)
                 if re.search(r"worst single batch release\s+([0-9.]+)", text) else "no"),
                 FRAME_MS, len(findings), ("; " + "; ".join(findings[:3])) if findings else ""))
    return ("the tape's own work at 200 fills/s fits in a frame, and the artefact says what it did not measure",
            not findings, detail)


# ---------------------------------------------------------------------------------- D8/D9 scanners (canaried)
def automation_findings(rules: list, catalog: dict | None = None, halt: dict | None = None) -> list:
    """A rule list and a template catalog that a person can act on.

    Four plantings this looks for: a rule whose badge carries no sentence, a rule that is `active` without a
    completed dry run (the one state the whole feature exists to prevent), a withheld template with no reason
    (the user cannot tell "broken" from "the maths said no"), and a halt banner with nothing to acknowledge.
    """
    out = []
    for rule in rules or []:
        rid = rule.get("ruleId") or "?"
        if not rule.get("statusWhy"):
            out.append("rule %s: no sentence beside the status badge" % rid)
        if rule.get("status") not in ("active", "paused", "dry_run", "halted"):
            out.append("rule %s: status %r is not one of the four" % (rid, rule.get("status")))
        if rule.get("status") == "active" and not rule.get("dryRunCompletedMs"):
            out.append("rule %s: armed with no completed dry run" % rid)
        if halt and halt.get("halted") and rule.get("status") != "halted":
            out.append("rule %s: shows %r while the account is halted" % (rid, rule.get("status")))
    if catalog is not None:
        for tpl in catalog.get("templates") or []:
            tid = tpl.get("templateId") or "?"
            if not tpl.get("available") and not (tpl.get("blockingReason") and tpl.get("why")):
                out.append("template %s: withheld with no reason stated" % tid)
            if tpl.get("available") and tpl.get("kind") == "entry" and not tpl.get("feeArithmetic"):
                out.append("template %s: an entry rule offered with no fee arithmetic" % tid)
    if halt and halt.get("halted") and not (halt.get("note") and halt.get("acknowledgeHint")):
        out.append("a halt banner with no note and no way to acknowledge it")
    return out


def alert_findings(payload: dict) -> list:
    """The alerts list as the screen reads it: every rule states its own fire budget and what would happen now."""
    out = []
    settings = payload.get("settings") or {}
    for key in ("quietHours", "digestNow"):
        if not isinstance(settings.get(key), dict) or not (settings.get(key) or {}).get("note"):
            out.append("settings: %s is missing from the list read" % key)
    for row in settings.get("channels") or []:
        if not row.get("plan"):
            out.append("channel %r states no plan" % row.get("channel"))
    for rule in payload.get("rules") or []:
        rid = rule.get("ruleId") or "?"
        if not rule.get("cooldownRule"):
            out.append("rule %s: no fire-budget sentence" % rid)
        now = rule.get("wouldDoNow") or {}
        if not now.get("sentence"):
            out.append("rule %s: would-do-now with no sentence" % rid)
        if rule.get("channelAllowed") is False and not rule.get("channelNote"):
            out.append("rule %s: a channel the plan refuses, with no reason" % rid)
    return out


def delivery_findings(rows: list) -> list:
    """A history where held, rate-limited and failed are different facts, and a row that was never sent has no
    latency — a `latencyMs` on a `queued` row is a claim about a send that did not happen."""
    out = []
    for row in rows or []:
        did = row.get("deliveryId")
        if not row.get("status"):
            out.append("delivery %s: no status" % did)
        if not row.get("reason"):
            out.append("delivery %s: no reason" % did)
        if not row.get("channel"):
            out.append("delivery %s: no channel" % did)
        if row.get("status") in ("queued", "held", "digest_scheduled") and row.get("latencyMs") is not None:
            out.append("delivery %s: %s row claims a latency" % (did, row.get("status")))
        if row.get("sentMs") and not row.get("latencyMs") and row.get("latencyMs") != 0:
            out.append("delivery %s: sent with no latency recorded" % did)
    return out


def c12_builder_safety(p: Probe) -> tuple[str, bool, str]:
    """A rule cannot be saved armed, cannot be armed without an observed dry run, and every evaluation is a row.

    The three refusals are the whole D8 safety story, so the gate walks them rather than reading about them:
    `dryRunOnly` on the create, `DRY_RUN_REQUIRED` on the first arm attempt, then the preview that earns the arm.
    """
    from polygm_core.automation import engine as au_engine                            # noqa: PLC0415
    findings = []
    if p.client().get("/v1/automations").status_code != 401:
        findings.append("the rule list answered an anonymous caller")
    _c, listed = p.get("/v1/automations")
    vocab = listed.get("vocabulary") or {}
    kinds = sorted(str(x.get("kind")) for x in (vocab.get("triggers") or []))
    if kinds != sorted(au_engine.TRIGGER_KINDS):
        findings.append("the builder's triggers are not the engine's: %s" % kinds)
    actions = sorted(str(x.get("kind")) for x in (vocab.get("actions") or []))
    if actions != sorted(au_engine.ACTION_KINDS):
        findings.append("the builder's actions are not the engine's: %s" % actions)
    if (vocab.get("joiners") or {}) != {"all": "AND", "any": "OR"}:
        findings.append("the joiner vocabulary is not AND/OR: %r" % (vocab.get("joiners"),))
    limits = vocab.get("limits") or {}
    if limits.get("maxLeaves") != au_engine.MAX_LEAVES or limits.get("minIntervalMs") != au_engine.MIN_INTERVAL_MS:
        findings.append("the stated limits are not the engine's: %r" % (limits,))
    cancel = next((x for x in (vocab.get("actions") or []) if x.get("kind") == "cancel_open"), None)
    scope = next((f for f in (cancel or {}).get("fields") or [] if f.get("name") == "scope"), {})
    if "all" in (scope.get("options") or []):
        findings.append("the builder offers a scope the engine refuses (cancel everything)")

    market = str((p.rows("SELECT id FROM markets ORDER BY id LIMIT 1") or [("",)])[0][0])
    body = {"kind": "exit", "name": "gate exit-before-resolution", "match": "all",
            "triggers": [{"kind": "time", "at_ms": p.app()._now_ms() + 600_000, "once": True}],
            "actions": [{"kind": "close_position", "method": "market", "max_slippage_bps": 100}],
            "targets": [{"marketId": market}], "maxPerDay": 12, "minIntervalMs": 60_000,
            "maxLossMicro": 5_000_000}
    code, created = p.post("/v1/automations", body)
    rule = created.get("rule") or {}
    rid = rule.get("ruleId") or ""
    if code != 200 or not rid:
        return ("a rule cannot be saved armed, and cannot be armed without an observed dry run", False,
                "the create answered %d: %s" % (code, json.dumps(created)[:200]))
    if not created.get("dryRunOnly") or rule.get("status") != "dry_run" or rule.get("enabled") is not False:
        findings.append("a saved rule is not a dry run: status=%r enabled=%r" % (rule.get("status"), rule.get("enabled")))
    if rule.get("dryRunCompletedMs"):
        findings.append("a brand-new rule claims a completed dry run")
    if not rule.get("statusWhy"):
        findings.append("the new rule's status carries no sentence")

    arm = p.post("/v1/automations/guards", {"ruleId": rid, "state": "active"})
    if arm[0] != 409 or (arm[1].get("error") or {}).get("code") != "DRY_RUN_REQUIRED":
        findings.append("arming without a dry run answered %d %s" % (arm[0], json.dumps(arm[1])[:90]))

    prev = p.post("/v1/automations/preview", {"ruleId": rid})
    if prev[0] != 200 or prev[1].get("dryRun") is not True:
        findings.append("the dry run answered %d: %s" % (prev[0], json.dumps(prev[1])[:120]))

    arm2 = p.post("/v1/automations/guards", {"ruleId": rid, "state": "active"})
    armed_rule = arm2[1].get("rule") or {}
    if arm2[0] != 200 or armed_rule.get("status") != "active" or armed_rule.get("enabled") is not True:
        findings.append("arming after the dry run answered %d %s" % (arm2[0], json.dumps(arm2[1])[:120]))
    pause = p.post("/v1/automations/guards", {"ruleId": rid, "state": "paused"})
    if pause[0] != 200 or (pause[1].get("state") != "paused"):
        findings.append("pausing answered %d %s" % (pause[0], json.dumps(pause[1])[:90]))

    if p.client().post("/v1/automations", json=body, headers=p.user_headers()).status_code != 400:
        findings.append("a write with no Idempotency-Key was accepted")

    _rc, runs = p.get("/v1/automations/runs", ruleId=rid)
    rows = runs.get("rows") or []
    if not rows:
        findings.append("the dry run was not recorded in the history")
    for row in rows:
        if not row.get("outcome") or not row.get("sentence"):
            findings.append("run %s: outcome %r with sentence %r" % (row.get("atMs"), row.get("outcome"),
                                                                    row.get("sentence")))
    if sorted((runs.get("counts") or {}).keys()) != ["failed", "placed", "skipped", "would_place"]:
        findings.append("the run counts do not separate placed/would-place/skipped/failed: %r" % (runs.get("counts"),))
    modes = sorted({r.get("mode") for r in rows})
    ok = not findings
    return ("a rule cannot be saved armed, and cannot be armed without an observed dry run", ok,
            "vocabulary %d triggers/%d actions matches the engine, create dryRunOnly, arm-before-dry-run %d, "
            "arm-after %s, pause %s, %d run rows (%s), counts %s; %d findings%s"
            % (len(kinds), len(actions), arm[0], armed_rule.get("status"), pause[1].get("state"), len(rows),
               "/".join(str(m) for m in modes), json.dumps(runs.get("counts") or {}), len(findings),
               ("; " + "; ".join(findings[:3])) if findings else ""))


def c13_template_fees(p: Probe) -> tuple[str, bool, str]:
    """The 5-minute crypto template is offered only when its own fee arithmetic works — shown in both states.

    One catalog with no measured edge and one with an edge large enough to clear the break-even. The first must
    withhold the entry half *and say why*; the second must offer it. A gate that only ever saw the withheld state
    could not tell "correctly withheld" from "hard-coded off".
    """
    _c, bare = p.get("/v1/automations/templates")
    _c2, edged = p.get("/v1/automations/templates", edgeAvailableBp=900)
    findings = automation_findings([], catalog=bare) + automation_findings([], catalog=edged)
    five = next((t for t in (bare.get("templates") or []) if t.get("templateId") == "entry-momentum-5m"), None)
    five_e = next((t for t in (edged.get("templates") or []) if t.get("templateId") == "entry-momentum-5m"), None)
    if five is None or five_e is None:
        return ("the crypto 5-minute template is offered only when its fee arithmetic works", False,
                "the catalog does not list the 5-minute template")
    if five.get("available") is not False or bare.get("ships") is not False:
        findings.append("with no measured edge the entry template was offered anyway")
    if five.get("blockingReason") != "no_measured_edge":
        findings.append("the withheld template's blocking reason is %r" % five.get("blockingReason"))
    if five_e.get("available") is not True or edged.get("ships") is not True:
        findings.append("an edge above break-even did not release the entry template")
    arith = five.get("feeArithmetic") or {}
    if not (isinstance(arith.get("breakEvenWinRateBp"), int) and isinstance(arith.get("impliedProbBp"), int)):
        findings.append("the fee arithmetic has no break-even vs implied comparison: %r" % (arith,))
    elif arith["breakEvenWinRateBp"] <= arith["impliedProbBp"]:
        findings.append("fees do not make the break-even harder than the implied probability")
    if arith.get("feeType") != "taker" or not arith.get("legs"):
        findings.append("the arithmetic does not state the fee type and the leg count: %r" % (arith,))
    protective = [t for t in (bare.get("templates") or []) if t.get("templateId") != "entry-momentum-5m"]
    if len(protective) < 3 or any(not t.get("available") for t in protective):
        findings.append("the protective templates are not all offered: %r"
                        % [(t.get("templateId"), t.get("available")) for t in protective])
    if any(not t.get("why") for t in protective):
        findings.append("a protective template is offered with no reason")
    ok = not findings
    return ("the crypto 5-minute template is offered only when its own fee arithmetic works", ok,
            "no edge: %s (%s); edge %dbp: %s; break-even %s vs implied %s, %s fees, %d legs; %d protective "
            "templates offered; %d findings%s"
            % (five.get("verdict"), five.get("blockingReason"), 900, five_e.get("verdict"),
               arith.get("breakEvenWinRateBp"), arith.get("impliedProbBp"), arith.get("feeType"),
               arith.get("legs"), len(protective), len(findings),
               ("; " + "; ".join(findings[:3])) if findings else ""))


def c14_alert_plans(p: Probe) -> tuple[str, bool, str]:
    """D9 end to end: the plan is stated before it happens, quiet hours hold even urgent, and a test costs nothing.

    The order is the user's: save a rule -> read what it would do now -> turn quiet hours on -> see the plan change
    -> test fire -> read the history. The last assertion is the one that makes the feature honest: the test fire
    must leave the rule's own window untouched and appear in the history as a test.
    """
    findings = []
    if p.client().get("/v1/alerts").status_code != 401:
        findings.append("the alerts list answered an anonymous caller")
    _c, listed = p.get("/v1/alerts")
    findings += alert_findings(listed)

    market = str((p.rows("SELECT id FROM markets ORDER BY id LIMIT 1") or [("",)])[0][0])
    code, created = p.post("/v1/alerts", {"kind": "whale_fill", "marketId": market, "channel": "telegram",
                                          "severity": "notice", "firesPerWindow": 3, "windowMs": 3_600_000})
    rule = created.get("rule") or {}
    rid = rule.get("ruleId") or ""
    if code != 200 or not rid:
        return ("the plan is stated before it happens, quiet hours hold even urgent, and a test costs nothing",
                False, "the alert save answered %d: %s" % (code, json.dumps(created)[:200]))
    if "3 per 1h" not in str(rule.get("cooldownRule") or ""):
        findings.append("the rule's fire budget is not stated as a rule: %r" % rule.get("cooldownRule"))
    if rule.get("firesInWindow") != 0:
        findings.append("a brand-new rule already has fires in its window")
    if (rule.get("wouldDoNow") or {}).get("decision") != "send_now":
        findings.append("a free-plan telegram rule with quiet hours off would not send now: %r"
                        % (rule.get("wouldDoNow"),))

    webhook = p.post("/v1/alerts", {"kind": "whale_fill", "marketId": market, "channel": "webhook",
                                    "severity": "notice", "firesPerWindow": 1, "windowMs": 3_600_000})
    if webhook[0] != 402 or (webhook[1].get("error") or {}).get("code") != "PLAN_REQUIRED" \
            or "pro" not in json.dumps(webhook[1]):
        findings.append("a webhook rule on the free plan answered %d %s"
                        % (webhook[0], json.dumps(webhook[1])[:90]))

    # Quiet hours around the API's own "now", in its own local clock: a window that does not contain this moment
    # would test nothing, and one that wraps midnight is refused by design.
    local = (p.app()._now_ms() // 60_000) % 1440
    start, end = (0, 60) if local < 60 else (local - 30, local + 30)
    p.post("/v1/alerts/settings", {"quietStartMin": start, "quietEndMin": end, "tzOffsetMin": 0,
                                   "digestMode": "off"})
    _c2, quiet = p.get("/v1/alerts")
    qstate = (quiet.get("settings") or {}).get("quietHours") or {}
    if qstate.get("active") is not True:
        findings.append("quiet hours covering now do not read as active: %r" % (qstate,))
    held_rule = next((r for r in (quiet.get("rules") or []) if r.get("ruleId") == rid), {})
    if (held_rule.get("wouldDoNow") or {}).get("decision") != "quiet_hours":
        findings.append("a rule that would be held says %r" % ((held_rule.get("wouldDoNow") or {}).get("decision"),))

    test = p.post("/v1/alerts/test", {"ruleId": rid})
    plan = (test[1].get("plan") or [{}])[0]
    if test[0] != 200 or plan.get("decision") != "quiet_hours" or not plan.get("sentence"):
        findings.append("the test fire under quiet hours answered %d %r" % (test[0], plan))
    summary = (test[1].get("summary") or {}).get("sentence") or ""
    caveat = str(test[1].get("note") or "")
    if "would" not in (summary + " " + caveat).lower():
        findings.append("the test fire does not speak in the conditional: %r / %r" % (summary, caveat))
    if not test[1].get("deliveryIds"):
        findings.append("the test fire wrote no delivery row")
    if "window was not spent" not in str(test[1].get("note") or ""):
        findings.append("the test fire does not say it left the rule's budget alone")

    # A digest batches a notice but never an urgent alert: the one thing the product refuses to batch.
    p.post("/v1/alerts/settings", {"quietStartMin": -1, "quietEndMin": -1, "digestMode": "hourly", "tzOffsetMin": 0})
    _c3, digest = p.get("/v1/alerts")
    drows = next((r for r in (digest.get("rules") or []) if r.get("ruleId") == rid), {})
    if ((drows.get("digest") or {}).get("deferred") is not True
            or (drows.get("wouldDoNow") or {}).get("decision") != "digest"):
        findings.append("a notice under an hourly digest is not batched: %r" % (drows.get("wouldDoNow"),))
    urgent = p.post("/v1/alerts", {"kind": "whale_fill", "marketId": market, "channel": "telegram",
                                   "severity": "urgent", "firesPerWindow": 1, "windowMs": 3_600_000})
    urow = urgent[1].get("rule") or {}
    if (urow.get("wouldDoNow") or {}).get("decision") != "send_now":
        findings.append("an urgent alert was batched by the digest: %r" % (urow.get("wouldDoNow"),))
    p.post("/v1/alerts/settings", {"quietStartMin": -1, "quietEndMin": -1, "digestMode": "off", "tzOffsetMin": 0})

    _c4, after = p.get("/v1/alerts")
    spent = next((r for r in (after.get("rules") or []) if r.get("ruleId") == rid), {})
    if spent.get("firesInWindow") != 0:
        findings.append("the test fire spent the rule's own window: %d fires" % spent.get("firesInWindow"))

    _c5, hist = p.get("/v1/alerts/deliveries")
    findings += delivery_findings(hist.get("rows") or [])
    tests = [r for r in (hist.get("rows") or []) if r.get("isTest")]
    if not tests:
        findings.append("the test fire is not in the history as a test")

    # The editor's own params reach the engine: a level rule with no level is refused at save time.
    bad = p.post("/v1/alerts", {"kind": "price_level", "marketId": market, "channel": "telegram",
                                "severity": "notice", "firesPerWindow": 1, "windowMs": 3_600_000, "params": {}})
    if bad[0] != 422:
        findings.append("a level rule with no level answered %d, not 422" % bad[0])
    good = p.post("/v1/alerts", {"kind": "price_level", "marketId": market, "channel": "telegram",
                                 "severity": "notice", "firesPerWindow": 1, "windowMs": 3_600_000,
                                 "params": {"priceMicro": 620_000, "op": ">="}})
    if good[0] != 200 or (good[1].get("rule") or {}).get("engineKind") != "price_level":
        findings.append("a level rule with a level answered %d %s" % (good[0], json.dumps(good[1])[:90]))
    ok = not findings
    return ("the plan is stated before it happens, quiet hours hold even urgent, and a test costs nothing", ok,
            "budget %r, webhook on free %d, quiet hours held the plan, digest batched a notice and not the urgent "
            "one, test left the window at %s fires, %d delivery rows (%d test); %d findings%s"
            % (rule.get("cooldownRule"), webhook[0], spent.get("firesInWindow"), len(hist.get("rows") or []),
               len(tests), len(findings), ("; " + "; ".join(findings[:3])) if findings else ""))


def c15_halt_banner(p: Probe) -> tuple[str, bool, str]:
    """When the risk service has stopped the account, the halted state outranks `enabled` on every rule.

    The halt is written the way the risk gate writes it, then every money-moving path is asked: the list, the
    save, and the arm. Acknowledge is a record rather than a reset, so the gate checks the row survives with its
    acknowledgement and that the banner clears on the acknowledge rather than on a delete.
    """
    findings = []
    uid = "u-demo"
    save_code = arm_code = 0
    at = p.app()._now_ms()
    market = str((p.rows("SELECT id FROM markets ORDER BY id LIMIT 1") or [("",)])[0][0])
    p.post("/v1/automations", {"kind": "exit", "name": "gate halt fixture", "match": "all",
                               "triggers": [{"kind": "time", "at_ms": at + 600_000, "once": True}],
                               "actions": [{"kind": "close_position", "method": "market",
                                            "max_slippage_bps": 100}],
                               "targets": [{"marketId": market}], "maxLossMicro": 5_000_000,
                               "maxPerDay": 12, "minIntervalMs": 60_000})
    before = len(p.get("/v1/automations")[1].get("rules") or [])
    p.app()._db.execute("DELETE FROM loss_halts WHERE user_id = ?", (uid,))
    p.app()._db.execute("INSERT INTO loss_halts (user_id, threshold_micro, realized_micro, tripped_ms, "
                        "acknowledged_ms) VALUES (?,?,?,?,NULL)", (uid, 50_000_000, -60_000_000, at))
    p.app()._db.commit()
    try:
        _c, listed = p.get("/v1/automations")
        halt = listed.get("halt") or {}
        findings += automation_findings(listed.get("rules") or [], halt=halt)
        if halt.get("halted") is not True:
            findings.append("the halt is not on the banner: %r" % (halt,))
        for rule in (listed.get("rules") or []):
            if rule.get("status") != "halted":
                findings.append("rule %s reads %r while the account is halted" % (rule.get("ruleId"), rule.get("status")))

        body = {"kind": "exit", "name": "gate halted", "match": "all",
                "triggers": [{"kind": "time", "at_ms": at + 600_000, "once": True}],
                "actions": [{"kind": "close_position", "method": "market", "max_slippage_bps": 100}],
                "targets": [{"marketId": market}], "maxLossMicro": 5_000_000}
        save = p.post("/v1/automations", body)
        save_code = save[0]
        if save[0] != 409 or (save[1].get("error") or {}).get("code") != "HALTED":
            findings.append("saving a rule while halted answered %d %s" % (save[0], json.dumps(save[1])[:90]))
        rules = listed.get("rules") or []
        if not rules:
            findings.append("no rule exists for the arm test")
        else:
            arm = p.post("/v1/automations/guards", {"ruleId": rules[0].get("ruleId"), "state": "active"})
            arm_code = arm[0]
            if arm[0] != 409 or (arm[1].get("error") or {}).get("code") != "HALTED":
                findings.append("arming while halted answered %d %s" % (arm[0], json.dumps(arm[1])[:90]))

        p.app()._db.execute("UPDATE loss_halts SET acknowledged_ms = ? WHERE user_id = ?", (at + 1_000, uid))
        p.app()._db.commit()
        kept = p.rows("SELECT acknowledged_ms FROM loss_halts WHERE user_id = ?", (uid,))
        if not kept or not kept[0][0]:
            findings.append("acknowledging deleted the halt row instead of recording the acknowledgement")
        cleared = p.get("/v1/automations")[1].get("halt")
        if cleared:
            findings.append("the banner survives the acknowledgement: %r" % (cleared,))
    finally:
        p.app()._db.execute("DELETE FROM loss_halts WHERE user_id = ?", (uid,))
        p.app()._db.commit()
    after = len(p.get("/v1/automations")[1].get("rules") or [])
    if after != before:
        findings.append("a refused save still created a rule: %d -> %d" % (before, after))
    ok = not findings
    return ("a halted account cannot save or arm, and every rule says halted until it is acknowledged", ok,
            "%d rules all read halted while stopped, save %d, arm %d, acknowledgement recorded and the banner "
            "cleared, rule count %d -> %d; %d findings%s"
            % (before, save_code, arm_code, before, after, len(findings),
               ("; " + "; ".join(findings[:3])) if findings else ""))
CHECKS = (c1_contract, c2_no_addresses, c3_gate_and_drawdown, c4_whale_rule, c5_copy_safety, c6_views_and_alerts,
          c7_integers_only, c8_freshness, c9_acceptance_path, c10_radar_cost, c11_tape_frame_budget,
          c12_builder_safety, c13_template_fees, c14_alert_plans, c15_halt_banner)


# --------------------------------------------------------------------------------------------------- self-test
def self_test() -> int:
    """Plant each violation the scanners claim to catch and fail if a scan walks past it.

    The probes (c1, c6, c9) are not canaried: they are live walks over the real app, and a canary for "the API
    works" would be the test suite. What is canaried here is every SCANNER — the pure functions this file uses to
    decide whether a payload is acceptable, because those are the parts whose silence would be invisible.
    """
    cases, passes = [], 0

    def canary(fn):
        cases.append(fn)
        return fn

    @canary
    def address_scanner():
        got = address_findings({"rows": [{"anonWallet": "w_abc1234567"}],
                                "trader": {"wallet": "0x" + "ab" * 20}})
        clean = address_findings({"rows": [{"anonWallet": "w_abc1234567", "note": "not an address"}]})
        return len(got) == 1 and not clean, got

    @canary
    def pseudonym_scanner():
        got = wallet_field_findings({"a": {"sourceAnon": "0x" + "cd" * 20}, "b": {"anonWallet": "w_9"}})
        return len(got) == 1, got

    @canary
    def drawdown_scanner():
        badly = {"curve": [{"cumMicro": 100, "peakMicro": 200, "drawdownMicro": 0},
                           {"cumMicro": 300, "peakMicro": 200, "drawdownMicro": -100}],
                 "maxDrawdownMicro": 0}
        good = {"curve": [{"cumMicro": 100, "peakMicro": 100, "drawdownMicro": 0},
                          {"cumMicro": -50, "peakMicro": 100, "drawdownMicro": 150}],
                "maxDrawdownMicro": 150}
        return bool(drawdown_findings(badly)) and not drawdown_findings(good), drawdown_findings(badly)

    @canary
    def win_rate_scanner():
        bad = {"sampleGate": 20, "metrics": {"7d": {"resolvedMarkets": 4, "winRateBps": 7500,
                                                    "insufficientSample": False, "sampleNote": "",
                                                    "realisedMicro": 1, "unrealisedMicro": 0, "volumeMicro": 1,
                                                    "maxDrawdownMicro": 0}}}
        good = {"sampleGate": 20, "metrics": {"7d": {"resolvedMarkets": 4, "winRateBps": None,
                                                     "insufficientSample": True, "sampleNote": "4 of 20",
                                                     "realisedMicro": 1, "unrealisedMicro": 0, "volumeMicro": 1,
                                                     "maxDrawdownMicro": 0}}}
        return len(win_rate_findings(bad)) >= 2 and not win_rate_findings(good), win_rate_findings(bad)

    @canary
    def perf_scanner():
        """Three plantings: a breach, a missing caveat, and a clean one that must stay clean.

        The clean case matters as much as the two failures — a scanner that flags everything would make the gate
        impossible to pass and therefore impossible to trust.
        """
        head = ("P10 tape budget — generated by web/scripts/measure-tape-budget.mjs.\n\n"
                "driven at 200 fills/second for 10 seconds\n")
        caveat = ("What it does NOT measure: paint, layout and compositing. No browser exists in this environment,\n"
                  "so the pixels are [UNVERIFIED].\n")
        clean = (head + caveat + "  mean work per second of load   1.463 ms  (budget 16.7 ms)\n"
                 "  worst single batch release     0.113 ms\n\nstatus: pass\n")
        breached = clean.replace("1.463 ms", "41.000 ms").replace("status: pass", "status: fail")
        no_caveat = clean.replace(caveat, "")
        ok = (not perf_findings(clean) and len(perf_findings(breached)) >= 2 and len(perf_findings(no_caveat)) == 1
              and perf_findings("", "") != [])
        return ok, {"clean": perf_findings(clean), "breached": perf_findings(breached), "no_caveat": perf_findings(no_caveat)}

    @canary
    def threshold_scanner():
        bad = [{"notionalMicro": 900, "thresholdMicro": 100, "isWhale": False, "thresholdRule": "",
                "rule": ""}]
        good = [{"notionalMicro": 900, "thresholdMicro": 500, "isWhale": True, "rule": "severity is a ratio",
                 "thresholdRule": "whale = max(the p99.5 fill, $500.00 absolute floor) = $500.00"}]
        got = threshold_findings(bad, 500)
        return len(got) >= 4 and not threshold_findings(good, 500), got

    @canary
    def freshness_scanner():
        bad = {"asOf": 9_000, "serverAsOf": 1_000, "staleAfter": 1_500}      # data from the future, no cache
        good = {"asOf": 1_000, "serverAsOf": 1_000, "staleAfter": 4_000, "cache": {"ttlMs": 500}}
        return len(freshness_findings(bad)) >= 3 and not freshness_findings(good), freshness_findings(bad)

    @canary
    def float_scanner():
        bad = "x = round(total_micro / 1e6)\ny = 0.5 * price_micro\n"
        good = "# a decimal like 1.5 in prose is fine\nx = total_micro // 1_000_000  # 1.0 exact\n"
        return len(float_findings(bad, "planted")) >= 2 and not float_findings(good, "clean"), \
            float_findings(bad, "planted")

    @canary
    def radar_scanner():
        bad = {"rankingsMeta": [{"id": "active", "question": ""}],
               "rankings": {"profit": [{"anonWallet": "w_x", "winRateBps": None, "insufficientSample": False}]},
               "unranked": [{"anonWallet": "w_y", "insufficientSample": True, "reason": ""}],
               "quota": {"plan": "free"}}
        good = {"rankingsMeta": [{"id": i, "question": "q"} for i in ("active", "profit", "earliest", "overlap")],
                "rankings": {"profit": [{"anonWallet": "w_x", "winRateBps": 6000, "insufficientSample": False}]},
                "unranked": [{"anonWallet": "w_y", "insufficientSample": True, "reason": "2 settled markets"}],
                "quota": {"plan": "free", "usedToday": 1, "perDay": 20, "cached": False, "jobId": None,
                          "note": "1 of 20"},
                "costNote": "cached for 60 seconds"}
        got = radar_findings(bad)
        return len(got) >= 4 and not radar_findings(good), got

    # ------------------------------------------------------------- D8/D9 canaries (added with the closing work)
    @canary
    def automation_scanner():
        """Three plantings: an armed rule with no dry run, a withheld template with no reason, a silent halt."""
        bad = ([{"ruleId": "r1", "status": "active", "statusWhy": "", "dryRunCompletedMs": None}],)
        catalog = {"templates": [{"templateId": "entry-momentum-5m", "kind": "entry", "available": False,
                                  "blockingReason": "", "why": ""},
                                 {"templateId": "protect-exit-before-resolution", "kind": "protect",
                                  "available": True, "feeArithmetic": None}]}
        got = automation_findings(*bad, catalog=catalog,
                                 halt={"halted": True, "note": "", "acknowledgeHint": ""})
        clean = ([{"ruleId": "r1", "status": "halted", "statusWhy": "stopped by the daily-loss halt",
                   "dryRunCompletedMs": None}], {"templates": []},
                 {"halted": True, "note": "the daily loss limit tripped", "acknowledgeHint": "acknowledge in the "
                  "risk panel"})
        return len(got) >= 4 and not automation_findings(*clean), got

    @canary
    def alert_scanner():
        bad = {"settings": {"quietHours": {"note": ""}, "channels": [{"channel": "webhook"}]},
               "rules": [{"ruleId": "a1", "cooldownRule": "", "wouldDoNow": {}, "channelAllowed": False,
                          "channelNote": ""}]}
        good = {"settings": {"quietHours": {"note": "off"}, "digestNow": {"note": "off"},
                             "channels": [{"channel": "webhook", "plan": "pro"}]},
                "rules": [{"ruleId": "a1", "cooldownRule": "3 per 1h = one every 20m at most",
                           "wouldDoNow": {"sentence": "queued for telegram"}, "channelAllowed": False,
                           "channelNote": "webhook needs the pro plan"}]}
        got = alert_findings(bad)
        return len(got) >= 5 and not alert_findings(good), got

    @canary
    def delivery_scanner():
        bad = [{"deliveryId": 1, "status": "queued", "reason": "queued for telegram", "channel": "telegram",
                "latencyMs": 120},
               {"deliveryId": 2, "status": "queued", "reason": "", "channel": ""}]
        good = [{"deliveryId": 1, "status": "queued", "reason": "queued for telegram", "channel": "telegram",
                 "latencyMs": None, "sentMs": None},
                {"deliveryId": 2, "status": "sent", "reason": "delivered", "channel": "telegram",
                 "latencyMs": 240, "sentMs": 1}]
        got = delivery_findings(bad)
        return len(got) >= 3 and not delivery_findings(good), got

    for fn in cases:
        try:
            ok, got = fn()
        except Exception as e:                     # a canary that raises is a canary that did not fire
            ok, got = False, ["raised %s: %s" % (type(e).__name__, e)]
        print("  %s  %s%s" % ("fired" if ok else "MISS ", fn.__name__, "" if ok else "  <- " + str(got)[:160]))
        passes += 1 if ok else 0
    print("p10 gate self-test: %d/%d canaries fired" % (passes, len(cases)))
    return 0 if passes == len(cases) else 1


# ------------------------------------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--fast", action="store_true", help="skip c1 (check-openapi) and c7 (the P08 money scanner)")
    ap.add_argument("--only", default="", help="run only the checks whose name contains this")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--record", default="", help="write the run to this file as well as stdout")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.list:
        for fn in CHECKS:
            print("  %-64s %s" % (fn.__name__, (fn.__doc__ or "").strip().split("\n")[0][:76]))
        print("%d checks; c9 walks the phase's acceptance sentence end to end" % len(CHECKS))
        return 0
    checks = [c for c in CHECKS if not a.only or a.only in c.__name__]
    if a.fast:
        checks = [c for c in checks if c not in (c1_contract, c7_integers_only)]
    probe = Probe()
    lines, results = [], []
    for fn in checks:
        t0 = time.perf_counter()
        try:
            label, ok, detail = fn(probe)
        except Exception as e:
            import traceback
            label, ok = fn.__name__, False
            detail = "raised %s: %s | %s" % (type(e).__name__, str(e)[:150],
                                             traceback.format_exc(limit=3).strip().splitlines()[-1][:110])
        ms = int((time.perf_counter() - t0) * 1000)
        results.append((label, ok, detail, ms))
        line = "  %-4s %5d ms  %s\n        %s" % ("PASS" if ok else "FAIL", ms, label, detail)
        print(line)
        lines.append(line)
    passed = sum(1 for _l, ok, _d, _m in results if ok)
    summary = "\nP10 gate: %d/%d checks passed in %d ms%s" % (
        passed, len(results), sum(m for *_x, m in results), " (--fast)" if a.fast else "")
    if passed != len(results):
        summary += "\nthe gate is a floor: a FAIL here means P10 is not done, whatever the document says"
    print(summary)
    if a.json:
        print(json.dumps({"phase": "P10", "passed": passed, "total": len(results),
                          "checks": [{"label": l, "ok": ok, "detail": d, "ms": m} for l, ok, d, m in results]},
                         indent=2))
    if a.record:
        out = ROOT / a.record
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("# P10 gate — recorded by tools/p10-gate-check.py\n\n%s\n"
                       % chr(10).join(lines).replace("  PASS", "PASS  ").replace("  FAIL", "FAIL  "))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
