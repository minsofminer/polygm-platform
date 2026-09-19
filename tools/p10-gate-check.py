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
CONTRACT = ROOT / "contracts" / "openapi.yaml"
METRICS = ROOT / "packages" / "polygm_core" / "terminal" / "metrics.py"
PY = sys.executable

#: The paths this phase serves. c1 requires each of them in the contract AND in `tools/check-openapi.py`'s table
#: (`TABLE_FOR_PATH`), because a path missing from that table is a path whose status sets are never compared.
P10_PATHS = ("/v1/tape/fills", "/v1/tape/facets", "/v1/whales", "/v1/traders/{anon}", "/v1/copy/configs",
             "/v1/copy/configs/guards", "/v1/copy/configs/monitor", "/v1/me/portfolio", "/v1/whale-views")

#: The four reads the phase's own doc calls public, and the seven that are the caller's own state.
PUBLIC_OPS = ("GET /v1/tape/fills", "GET /v1/tape/facets", "GET /v1/whales", "GET /v1/traders/{anon}")
USER_OPS = ("GET /v1/copy/configs", "POST /v1/copy/configs", "POST /v1/copy/configs/guards",
            "GET /v1/copy/configs/monitor", "GET /v1/me/portfolio", "GET /v1/whale-views",
            "POST /v1/whale-views")

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
        r = self.client().post(url, json=body, headers=self.user_headers(), **kw)
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
    """The contract, the served routes and the authz table agree about the nine P10 operations."""
    rc, out = sh([PY, "tools/check-openapi.py"], timeout=600)
    tail = [l for l in out.splitlines() if l.startswith("check-openapi:")]
    ok_openapi = rc == 0 and any(" 0 failed" in l for l in tail)
    yaml_text = read(CONTRACT)
    checker = read(ROOT / "tools" / "check-openapi.py")
    missing_contract = [path for path in P10_PATHS if ("  %s:" % path) not in yaml_text]
    missing_table = [path for path in P10_PATHS if ('"%s":' % path) not in checker]
    p.app()                     # app.py is what applies the P10 rows to the registry; import it first
    from polygm_core.security import authz                                        # noqa: PLC0415
    public = [op for op in PUBLIC_OPS if authz.LEVELS_TABLE.get(op, ("",))[0] == authz.PUBLIC]
    user = [op for op in USER_OPS if authz.LEVELS_TABLE.get(op, ("",))[0] == authz.USER]
    ok = (ok_openapi and not missing_contract and not missing_table
          and len(public) == len(PUBLIC_OPS) and len(user) == len(USER_OPS))
    return ("the contract, the router and the authz table agree on P10's nine operations", ok,
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


CHECKS = (c1_contract, c2_no_addresses, c3_gate_and_drawdown, c4_whale_rule, c5_copy_safety, c6_views_and_alerts,
          c7_integers_only, c8_freshness, c9_acceptance_path)


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
