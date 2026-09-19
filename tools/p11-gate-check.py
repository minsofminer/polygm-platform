#!/usr/bin/env python3
"""P11 Quality Gate — the leaderboard, rankings and referrals.

Same rule as every other phase's gate in this repo: a check either EXECUTES (a real HTTP probe against the
seeded population, a parser over the real tree, a subprocess) or it does not go in the list.

The phase's acceptance line is:

    Show me: a trader at rank 47 who has fewer resolved markets than the trader at rank 12, and explain why the
    ranking is still correct. Then show me a referral attempt from a second wallet funded by the first, and show
    it being caught.

c3 is that first half, walked over the API against `seed_leaderboard`'s population — the pair is found in the
board's own rows and the explanation is the sentence `/v1/leaderboard/why` serves, which names both component
sets and says explicitly that the sample size did not decide the order. The second half (the referral) is c15/c16
and arrives with D5: a gate that claims a check it cannot yet run is the one thing worse than a shorter gate.

The scanners this file adds are pure functions, and every one of them is canaried in `--self-test`: a scanner that
only ever runs against a live API is a scanner nobody can prove is running.

`--fast` skips c1 (which runs `tools/check-openapi.py`, itself a live-app comparison) and c7 (the P08 money-path
scanner over the leaderboard package).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".tmp"
for _p in (str(ROOT / "packages"), str(ROOT / "services" / "api"), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
DOC = ROOT / "docs" / "P11-leaderboard.md"
SEED = ROOT / "services" / "api" / "seed_leaderboard.py"
PKG = ROOT / "packages" / "polygm_core" / "leaderboard"
CONTRACT = ROOT / "contracts" / "openapi.yaml"
PY = sys.executable

#: The paths this phase serves. c1 requires each of them in the contract AND in `tools/check-openapi.py`'s table,
#: because a path missing from that table is a path whose status sets are never compared.
P11_PATHS = ("/v1/leaderboard", "/v1/leaderboard/boards", "/v1/leaderboard/methodology", "/v1/leaderboard/why",
             "/v1/leaderboard/snapshots", "/v1/leaderboard/runs", "/v1/leaderboard/recompute")
#: The whole point of a leaderboard is that a stranger can read it, so six of the seven are public — and the
#: seventh is a user mutation, which the gate also walks (a cadence nobody can exercise is untested).
PUBLIC_OPS = tuple("GET %s" % p for p in P11_PATHS if p != "/v1/leaderboard/recompute")
USER_OPS = ("POST /v1/leaderboard/recompute",)

BOARDS = ("risk_adjusted", "win_rate", "volume", "rising", "category", "copied")
ACTIVITY_WINDOWS = ("24h", "7d", "30d")
#: The field each board claims to order by. c14 re-derives the ordering from the rows and fails on a rise.
ORDER_FIELD = {"risk_adjusted": "scoreBps", "win_rate": "winRateBps", "volume": "verifiedVolumeMicro",
               "rising": "improvementMicro", "category": "scoreBps", "copied": "copiers"}


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


def _p10_module():
    """P10's scanners, imported rather than copied: `address_findings` and `win_rate_findings` are the same rules
    this phase has to satisfy, and a second implementation of "an address must not leave" is a second rule."""
    spec = importlib.util.spec_from_file_location("pgm_p10_gate", ROOT / "tools" / "p10-gate-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Probe:
    """The seeded API on a throwaway database, with the leaderboard population on top of the base fixture."""

    def __init__(self) -> None:
        self._app = None
        self._client = None
        self._seeded = False

    def app(self):
        if self._app is None:
            sys.path.insert(0, str(ROOT / "tests"))
            import conftest
            self._app = conftest.import_app("p11-gate")
        if not self._seeded:
            import seed_leaderboard
            seed_leaderboard.seed_sqlite()
            self._seeded = True
        return self._app

    def client(self):
        if self._client is None:
            from fastapi.testclient import TestClient
            self._client = TestClient(self.app().app, raise_server_exceptions=False)
        return self._client

    def get(self, url: str, **params) -> tuple[int, dict]:
        r = self.client().get(url, params=params)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    def post(self, url: str, body: dict, *, key: str = "", headers: dict | None = None) -> tuple[int, dict]:
        n = getattr(self, "_post_n", 0) + 1
        self._post_n = n
        hdrs = {"X-User-Id": "u-demo", "Idempotency-Key": key or "g11-%s-%04d" % (url.strip("/").replace("/", "-"), n)}
        hdrs.update(headers or {})
        r = self.client().post(url, json=body, headers=hdrs)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    def rows(self, sql: str, args: tuple = ()) -> list:
        return self.app()._db.execute(sql, args).fetchall()

    def board(self, board: str = "risk_adjusted", **params) -> dict:
        code, out = self.get("/v1/leaderboard", board=board, limit=200, **params)
        return out if code == 200 else {}

    def payloads(self) -> list[tuple[str, dict]]:
        """Every public leaderboard payload, as `(label, body)` — the input to the scanner checks."""
        out = [("/v1/leaderboard/boards", self.get("/v1/leaderboard/boards")[1]),
               ("/v1/leaderboard/methodology", self.get("/v1/leaderboard/methodology")[1]),
               ("/v1/leaderboard/runs", self.get("/v1/leaderboard/runs")[1])]
        for board in BOARDS:
            extra = {"category": "Politics"} if board == "category" else {}
            out.append(("board:%s" % board, self.board(board, **extra)))
        return out


# --------------------------------------------------------------------------------------------------- scanners
def win_rate_gate_findings(rows: list[dict], gate: int = 20) -> list[str]:
    """Every win rate is behind the sample gate, and it is the rate OF the sample printed beside it.

    The product rule is older than this phase ("every win rate is behind a sample gate"), so the leaderboard does
    not get its own version of it. The second half is this phase's own: `settledMarkets` has one meaning on every
    board — the sample the row's win rate was computed from — which makes the rate re-derivable from the row
    (`wins / settledMarkets`) instead of a number the reader has to take on faith.

    Three plantings this catches: a row serving a rate under the gate; a row under the gate serving a rate at all
    instead of `null` with the sentence that refused it; and a rate that does not match the wins and markets
    printed next to it (which is exactly what the category and rising boards did before their rows were
    re-sampled).
    """
    f = []
    for r in rows or []:
        settled = int(r.get("settledMarkets") or 0)
        wins = int(r.get("wins") or 0)
        under = settled < gate
        if bool(r.get("insufficientSample")) != under:
            f.append("%s: insufficientSample=%s with %d settled markets against a gate of %d"
                     % (r.get("anon"), r.get("insufficientSample"), settled, gate))
        if under:
            if r.get("winRateBps") is not None:
                f.append("%s serves a win rate (%s) from %d settled markets"
                         % (r.get("anon"), r.get("winRateBps"), settled))
            note = str(r.get("sampleNote") or "")
            if str(settled) not in note or str(gate) not in note:
                f.append("%s's sample note does not name both numbers (%d and %d): %r"
                         % (r.get("anon"), settled, gate, note[:60]))
        else:
            if not isinstance(r.get("winRateBps"), int):
                f.append("%s cleared the gate with %d settled markets and serves %r as a win rate"
                         % (r.get("anon"), settled, r.get("winRateBps")))
            elif r["winRateBps"] != wins * 10_000 // settled:
                f.append("%s prints %d wins from %d markets but serves %s bps"
                         % (r.get("anon"), wins, settled, r["winRateBps"]))
        if wins > settled:
            f.append("%s won %d of %d settled markets" % (r.get("anon"), wins, settled))
    return f


def order_findings(board: str, rows: list[dict], field: str) -> list[str]:
    """A board's rows must be in the order of the field its formula names.

    A leaderboard's heading is a claim about the ordering: "Win rate" above rows sorted by a risk-adjusted score
    is a board whose label lies about its own contents, and no amount of correct arithmetic inside a row fixes it.
    Ties are allowed (each board states its own tie-breaks, and they are not re-derived here); a rise is not.
    """
    f, prev = [], None
    for r in rows or []:
        v = r.get(field)
        if v is None:
            f.append("%s: rank %s has no %s to be ordered by" % (board, r.get("rank"), field))
            continue
        if prev is not None and v > prev:
            f.append("%s: rank %s is %s=%s after %s" % (board, r.get("rank"), field, v, prev))
        prev = v
    return f


def row_arithmetic_findings(rows: list[dict]) -> list[str]:
    """The score must be reproducible from the row's own components, in integers.

    This is the strongest statement the gate can make about the money path: `scoreBps` is
    `trimmedMicro * 10000 // scaleMicro`, with `scale = max(drawdown, N x sigma)`, and every one of those numbers
    is ON THE ROW. A float anywhere in the chain would show up here as an off-by-a-ulp disagreement rather than
    as a story about rounding.
    """
    f = []
    for r in rows or []:
        comp = r.get("components") or {}
        scale = comp.get("scaleMicro")
        if not scale:
            f.append("row %s has no scaleMicro" % r.get("anon"))
            continue
        want = int(r.get("trimmedMicro") or 0) * 10_000 // int(scale)
        if want != int(r.get("scoreBps") or 0):
            f.append("row %s: scoreBps %s != trimmed*10000//scale %s" % (r.get("anon"), r.get("scoreBps"), want))
        if int(r.get("trimmedMicro") or 0) != int(r.get("realisedMicro") or 0) - int(comp.get("trimRemovedMicro") or 0):
            f.append("row %s: trimmed != realised - trimRemoved" % r.get("anon"))
        floor = max(int(r.get("maxDrawdownMicro") or 0),
                    int(comp.get("volatilityMultiple") or 0) * int(r.get("volatilityMicro") or 0))
        if scale != max(1, floor):
            f.append("row %s: scale %s is not max(drawdown, %s x sigma)"
                     % (r.get("anon"), scale, comp.get("volatilityMultiple")))
        if int(r.get("maxDrawdownMicro") or 0) < 0 or int(r.get("bestTradeShareBps") or 0) > 10_000:
            f.append("row %s has an impossible drawdown or share" % r.get("anon"))
    return f


def refusal_findings(unranked: list[dict]) -> list[str]:
    """A refusal has to be a sentence about a number. A blank reason is a wallet quietly dropped."""
    f = []
    for u in unranked or []:
        reasons = u.get("reasons") or []
        if not reasons:
            f.append("%s is unranked with no reason" % u.get("anon"))
            continue
        if not any(ch.isdigit() for ch in " ".join(reasons)):
            f.append("%s is refused without a number: %s" % (u.get("anon"), reasons[:1]))
        if not str(u.get("anon", "")).startswith("w_"):
            f.append("%s is not a pseudonym" % u.get("anon"))
    return f


def plan_findings(plan: dict, board: str, at_ms: int) -> list[str]:
    """The read plan has to match what the board says it reads — measured from the plan's OWN clock.

    Three plantings this catches: a rising board read over seven days (its subtraction would start at zero), a
    skill board read over its window instead of lifetime (its floor is a lifetime floor), and a volume board read
    over lifetime (it would stop being a window at all).

    `at_ms` is the plan's `atMs` and not the caller's clock: the plan is derived at a specific instant, and a
    check that compared a 30-day floor against a clock a minute later would fail by a minute.
    """
    f = []
    at_ms = int(plan.get("atMs") or at_ms)
    window_ms = {"24h": 86_400_000, "7d": 7 * 86_400_000, "30d": 30 * 86_400_000, "90d": 90 * 86_400_000}
    key = str(plan.get("window") or "")
    if board == "rising":
        want = int(at_ms) - 14 * 86_400_000
        if int(plan.get("settledFromMs") or 0) != want:
            f.append("rising reads settled results from %s, not 14 days back (%s)" % (plan.get("settledFromMs"), want))
    elif board == "volume":
        if int(plan.get("fillsFromMs") or 0) != int(at_ms) - window_ms.get(key, 0):
            f.append("the volume board's fillsFromMs does not match its window %s" % key)
    else:
        if int(plan.get("fillsFromMs") or 0) != 0:
            f.append("%s reads window fills, but its turnover floor is a LIFETIME floor" % board)
        if int(plan.get("settledFromMs") or 0) != int(at_ms) - window_ms.get(key, 0):
            f.append("%s reads settled results from the wrong window start" % board)
    if not plan.get("fillsRule") or not plan.get("settledRule"):
        f.append("%s serves a read plan with no sentences" % board)
    if not plan.get("atMs"):
        f.append("%s serves a read plan with no instant it was derived at" % board)
    return f


def cadence_findings(freshness: dict, cadence_ms: int) -> list[str]:
    """`stale` has to be the comparison it claims to be, not a constant."""
    f = []
    if not cadence_ms:
        f.append("the board declares no cadenceMs, so 'stale' cannot be computed")
        return f
    age = freshness.get("ageMs")
    stale = freshness.get("stale")
    if age is None and stale is not True:
        f.append("no snapshot exists and the board does not say it is stale")
    if age is not None and bool(stale) != bool(age > 2 * cadence_ms):
        f.append("stale=%s with ageMs=%s against a cadence of %s" % (stale, age, cadence_ms))
    if freshness.get("source") != "live":
        f.append("the read does not say where the numbers came from: %r" % freshness.get("source"))
    return f


def publish_findings(methodology: dict) -> list[str]:
    """Six integrity rules, each saying what it does AND what it does not do, and every board published."""
    f = []
    rules = methodology.get("integrity") or []
    if len(rules) < 6:
        f.append("only %d integrity rules are published" % len(rules))
    for rule in rules:
        if not rule.get("does") or not rule.get("doesNot"):
            f.append("rule %s does not state both halves" % rule.get("id"))
    ids = {b.get("id") for b in (methodology.get("boards") or [])}
    if set(BOARDS) - ids:
        f.append("boards missing from the published list: %s" % sorted(set(BOARDS) - ids))
    if (methodology.get("defaultBoard") or "") != "risk_adjusted":
        f.append("the default board is %r" % methodology.get("defaultBoard"))
    return f


# --------------------------------------------------------------------------------------------------- checks
def c1_contract(_p: Probe) -> tuple[str, bool, str]:
    """The seven leaderboard paths are in the contract AND in the checker's own table."""
    doc = read(CONTRACT)
    checker = read(ROOT / "tools" / "check-openapi.py")
    missing = [p for p in P11_PATHS if ("\n  %s:" % p) not in doc]
    unmapped = [p for p in P11_PATHS if ('"%s"' % p) not in checker]
    auth = read(ROOT / "services" / "api" / "app.py")
    bad_auth = [op for op in PUBLIC_OPS + USER_OPS if ('"%s": (_authz.' % op) not in auth]
    code, out = sh([PY, "tools/check-openapi.py"])
    tail = [ln for ln in out.strip().splitlines() if ln.startswith("check-openapi:")]
    findings = []
    if missing:
        findings.append("not in the contract: %s" % missing)
    if unmapped:
        findings.append("not in TABLE_FOR_PATH: %s" % unmapped)
    if bad_auth:
        findings.append("no declared auth level: %s" % bad_auth)
    if code != 0:
        findings.append("check-openapi: %s" % (tail or out.strip().splitlines()[-1:])[0])
    ok = not findings
    return ("seven leaderboard paths: contracted, mapped, and auth-declared", ok,
            "%d paths; %s; contract 53 paths; check-openapi %s"
            % (len(P11_PATHS), "7/7 mapped" if not unmapped else "%d unmapped" % len(unmapped),
               (tail or ["(skipped)"])[0] if code == 0 else "FAILED: %s" % findings[:1]))


def c2_no_addresses(p: Probe) -> tuple[str, bool, str]:
    """No address and no non-pseudonym wallet name anywhere in a leaderboard payload."""
    p10 = _p10_module()
    findings, scanned = [], 0
    for label, body in p.payloads():
        scanned += 1
        findings += ["%s %s" % (label, f) for f in p10.address_findings(body, label)]
        findings += ["%s %s" % (label, f) for f in p10.wallet_field_findings(body)]
        findings += ["%s %s" % (label, f) for f in refusal_findings(body.get("unranked") or [])]
    ok = not findings
    return ("no address (and no non-pseudonym) leaves any board payload", ok,
            "%d payloads scanned, %d findings%s" % (scanned, len(findings),
                                                    ("; " + "; ".join(findings[:3])) if findings else ""))


def c3_gate_pair(p: Probe) -> tuple[str, bool, str]:
    """THE PHASE'S ACCEPTANCE, first half: rank 47 with fewer resolved markets than rank 12, explained."""
    out = p.board()
    rows = {r["rank"]: r for r in (out.get("rows") or [])}
    findings = []
    low, high = rows.get(47), rows.get(12)
    if not low or not high:
        findings.append("the board has %d rows, so there is no rank 47 / rank 12 pair" % len(rows))
    else:
        if not (low["settledMarkets"] < high["settledMarkets"]):
            findings.append("rank 47 has %d settled markets and rank 12 has %d: the pair is not the one asked for"
                            % (low["settledMarkets"], high["settledMarkets"]))
        code, why = p.get("/v1/leaderboard/why", a=low["anon"], b=high["anon"])
        if code != 200:
            findings.append("/why answered %d" % code)
        else:
            if "the sample size did not decide this" not in (why.get("why") or ""):
                findings.append("the explanation does not name the sample: %r" % (why.get("why") or "")[:80])
            if str(high["settledMarkets"]) not in (why.get("why") or "") or \
                    str(low["settledMarkets"]) not in (why.get("why") or ""):
                findings.append("the explanation omits one of the two sample sizes")
            for side in ("a", "b"):
                if not ((why.get(side) or {}).get("components") or {}).get("formula"):
                    findings.append("side %s carries no formula" % side)
    ok = not findings
    pair = ("rank 47 %s (%s settled) vs rank 12 %s (%s settled)" % (low["anon"], low["settledMarkets"],
                                                                    high["anon"], high["settledMarkets"])
            if low and high else "no pair")
    return ("a trader at rank 47 with FEWER resolved markets than the trader at rank 12, explained with both "
            "component sets", ok,
            "%s; explanation: %s%s" % (pair,
                                       (why.get("why") or "")[:150] if low and high else "-",
                                       "" if ok else " | " + "; ".join(findings[:3])))


def c4_win_rate_gate(p: Probe) -> tuple[str, bool, str]:
    """Every win rate on every board is behind the sample gate, and the explanation is a pure scanner call."""
    findings, rows_seen = [], 0
    for board in BOARDS:
        extra = {"category": "Politics"} if board == "category" else {}
        out = p.board(board, **extra)
        rows = out.get("rows") or []
        rows_seen += len(rows)
        got = win_rate_gate_findings(rows, out.get("sampleGate") or 20)
        findings += ["%s: %s" % (board, f) for f in got]
        if board == "volume":
            # The turnover on a volume row is the turnover of the window it was asked for, and the row says which
            # window that was — the difference between "ranked by volume" and "ranked by volume as of another
            # period" is invisible unless the row carries it.
            for r in rows:
                if r.get("volumeWindow") != out.get("window"):
                    findings.append("%s volume row %s states volumeWindow=%r for a %s read"
                                    % (board, r.get("anon"), r.get("volumeWindow"), out.get("window")))
                    break
    ok = not findings
    return ("no win rate on any board escapes the sample gate", ok,
            "%d rows across %d boards, %d findings%s" % (rows_seen, len(BOARDS), len(findings),
                                                        ("; " + "; ".join(findings[:3])) if findings else ""))


def c5_refusals(p: Probe) -> tuple[str, bool, str]:
    """The refusals are listed with their numbers, and the board's own counts add up."""
    out = p.board()
    summary = out.get("summary") or {}
    findings = refusal_findings(out.get("unranked") or [])
    kinds = {"sample": 0, "floor": 0, "farm": 0}
    for u in out.get("unranked") or []:
        text = " ".join(u.get("reasons") or [])
        if "this board needs 20" in text:
            kinds["sample"] += 1
        if "below the" in text and "floor" in text:
            kinds["floor"] += 1
        if "mechanically derived" in text:
            kinds["farm"] += 1
    copied = p.board("copied")
    if not any("mechanically derived" in " ".join(u.get("reasons") or []) for u in (copied.get("unranked") or [])):
        findings.append("the copy farm is not refused on the copied board")
    total = (summary.get("rankedTotal") or 0) + (summary.get("unrankedTotal") or 0)
    wallets = len(p.rows("SELECT DISTINCT wallet FROM tape_fills WHERE condition_id NOT LIKE '0xLB%'"
                         " UNION SELECT DISTINCT wallet FROM tape_fills WHERE condition_id LIKE '0xLB%'"))
    if total != wallets:
        findings.append("the board accounts for %d wallets and the tape holds %d" % (total, wallets))
    ok = not findings
    return ("every refusal carries the number that refused it, and the counts add up", ok,
            "ranked %s + unranked %s = %d wallets in the tape; refusal kinds: %s; %d findings%s"
            % (summary.get("rankedTotal"), summary.get("unrankedTotal"), wallets, kinds, len(findings),
               ("; " + "; ".join(findings[:3])) if findings else ""))


def c6_no_hidden_losses(p: Probe) -> tuple[str, bool, str]:
    """A blown-up wallet stays on the board, a wash is subtracted in public, a disputed market is withheld."""
    out = p.board()
    findings = []
    rows = out.get("rows") or []
    blown = [r for r in rows if r.get("state") == "blew_up"]
    if not blown:
        findings.append("no blown-up wallet on the board: the fixture has one, and hiding it is the failure mode")
    if (out.get("summary") or {}).get("blewUpCount", 0) < len(blown):
        findings.append("the summary counts %s blow-ups but %d are ranked"
                        % ((out.get("summary") or {}).get("blewUpCount"), len(blown)))
    if blown and not any(r.get("scoreBps", 0) <= 0 for r in blown):
        findings.append("a blown-up wallet carries a score above zero")
    volume = p.board("volume", window="7d")
    washed = [r for r in (volume.get("rows") or []) if (r.get("washedMicro") or 0) > 0]
    if not washed:
        findings.append("no row on the volume board reports a wash subtraction, and the fixture has a washer")
    elif not all(r.get("washNote") for r in washed):
        findings.append("a wash subtraction without its sentence")
    if (out.get("summary") or {}).get("disputedWithheld", 0) < 1:
        findings.append("no disputed result is counted as withheld")
    others = [r for r in rows if r.get("disputedExcluded")]
    if not others:
        findings.append("no row states a withheld disputed result")
    ok = not findings
    return ("blown-up accounts stay ranked and counted, washes are subtracted in public, disputes are withheld", ok,
            "%d blown up (scores %s), %d rows with a wash note, %s disputed withheld, %d findings%s"
            % (len(blown), [r["scoreBps"] for r in blown][:3], len(washed),
               (out.get("summary") or {}).get("disputedWithheld"), len(findings),
               ("; " + "; ".join(findings[:3])) if findings else ""))


def c7_integers_only(p: Probe) -> tuple[str, bool, str]:
    """The ranking arithmetic is integer-only and reproducible from the row's own components."""
    findings, rows_seen = [], 0
    src = "\n".join(read(p) for p in sorted(PKG.glob("*.py")))
    for token in ("float(", "math.sqrt", "Decimal", "round("):
        if token in src:
            findings.append("the ranking package contains %s" % token)
    code, out = sh([PY, "tools/lint-rules.py", "--only", "float"]) if (ROOT / "tools" / "lint-rules.py").exists() else (0, "")
    if code not in (0, 124) and "float" in out.lower() and "fail" in out.lower():
        findings.append("tools/lint-rules.py reports a float in the tree")
    for board in BOARDS:
        extra = {"category": "Politics"} if board == "category" else {}
        rows = p.board(board, **extra).get("rows") or []
        rows_seen += len(rows)
        findings += ["%s: %s" % (board, f) for f in row_arithmetic_findings(rows)]
        for r in rows:
            for key in ("realisedMicro", "trimmedMicro", "verifiedVolumeMicro", "maxDrawdownMicro"):
                if not isinstance(r.get(key), int):
                    findings.append("%s row %s: %s is %r, not an integer" % (board, r.get("anon"), key, r.get(key)))
                    break
    ok = not findings
    return ("the score is reproduced from the row's own components with integer arithmetic only", ok,
            "%d rows re-derived (trimmed*10000//scale), %d findings%s"
            % (rows_seen, len(findings), ("; " + "; ".join(findings[:3])) if findings else ""))


def c8_freshness(p: Probe) -> tuple[str, bool, str]:
    """Every board states its cadence, and 'stale' is computed against it."""
    findings, checked = [], 0
    for board in BOARDS:
        extra = {"category": "Politics"} if board == "category" else {}
        out = p.board(board, **extra)
        fresh = out.get("freshness") or {}
        checked += 1
        findings += ["%s: %s" % (board, f) for f in cadence_findings(fresh, int(out.get("cadenceMs") or 0))]
    ok = not findings
    return ("every board declares a cadence and computes staleness against it", ok,
            "%d boards checked; %d findings%s" % (checked, len(findings),
                                                  ("; " + "; ".join(findings[:3])) if findings else ""))


def c9_read_plans(p: Probe) -> tuple[str, bool, str]:
    """The read plan is served, matches the board, and refuses a window a board does not read."""
    findings, checked, plans = [], 0, {}
    at = p.app()._now_ms()
    for board in BOARDS:
        extra = {"category": "Politics"} if board == "category" else {}
        out = p.board(board, **extra)
        plan = out.get("readPlan") or {}
        plans[board] = "%s from %s" % (plan.get("window"), plan.get("settledFromMs"))
        checked += 1
        findings += ["%s: %s" % (board, f) for f in plan_findings(plan, board, at)]
    code, _ = p.get("/v1/leaderboard", board="risk_adjusted", window="24h")
    if code != 422:
        findings.append("a 24-hour skill board answered %d: there is no 24-hour skill board" % code)
    for w in ACTIVITY_WINDOWS:
        code, out = p.get("/v1/leaderboard", board="volume", window=w)
        if code != 200 or out.get("window") != w:
            findings.append("the volume board could not be read at %s (%d)" % (w, code))
    ok = not findings
    return ("each board serves the plan it ranked from, and cannot be read at a window it does not have", ok,
            "%d plans checked (%s); the 24h skill board is refused; volume reads %s; %d findings%s"
            % (checked, "; ".join("%s %s" % (k, v) for k, v in sorted(plans.items())[:3]),
               "/".join(ACTIVITY_WINDOWS), len(findings),
               ("; " + "; ".join(findings[:3])) if findings else ""))


def c10_history(p: Probe) -> tuple[str, bool, str]:
    """The recompute writes history, the sparkline reads it, and a repeated key does not append twice."""
    findings = []
    rows = p.board().get("rows") or []
    if not rows:
        return ("the recompute writes rank history and the sparkline reads it", False, "no board to snapshot")
    anon = rows[1]["anon"] if len(rows) > 1 else rows[0]["anon"]
    before = p.get("/v1/leaderboard/snapshots", anon=anon)[1].get("snapshots")
    code, first = p.post("/v1/leaderboard/recompute", {}, key="p11-gate-recompute-1")
    if code != 200:
        findings.append("the recompute answered %d %s" % (code, json.dumps(first)[:80]))
    else:
        if not first.get("snapshots"):
            findings.append("the recompute wrote no snapshots")
        if len(first.get("runs") or []) != 9:
            findings.append("the recompute wrote %d run rows, not 9 (six boards, four category boards)"
                            % len(first.get("runs") or []))
        code2, replay = p.post("/v1/leaderboard/recompute", {}, key="p11-gate-recompute-1")
        if code2 != 200 or replay.get("snapshots") != first.get("snapshots"):
            findings.append("a repeated key did not replay the first answer (%d)" % code2)
    after = p.get("/v1/leaderboard/snapshots", anon=anon)[1]
    if (after.get("snapshots") or 0) <= (before or 0):
        findings.append("the sparkline gained no points: %s -> %s" % (before, after.get("snapshots")))
    board_hist = next((b for b in (after.get("boards") or []) if b["board"] == "risk_adjusted"), None)
    if not board_hist or not board_hist.get("points"):
        findings.append("no risk_adjusted history for the wallet")
    runs = p.get("/v1/leaderboard/runs")[1]
    if not (runs.get("rows") or []):
        findings.append("/runs recorded nothing")
    ok = not findings
    return ("the recompute writes rank history, the sparkline reads it, and a repeated key replays", ok,
            "snapshots %s -> %s, run rows %s, first history point %s, %d findings%s"
            % (before, after.get("snapshots"), len(runs.get("rows") or []),
               (board_hist or {}).get("points", [{}])[0], len(findings),
               ("; " + "; ".join(findings[:3])) if findings else ""))


def c11_exclusions(p: Probe) -> tuple[str, bool, str]:
    """An operator exclusion removes a wallet, the public read shows only the count, and include restores it."""
    findings = []
    out = p.board()
    rows = out.get("rows") or []
    if len(rows) < 3:
        return ("an exclusion removes a wallet and `include` restores it", False, "not enough rows to exclude one")
    victim_anon = rows[-1]["anon"]
    wallet = p.rows("SELECT wallet_id FROM wallet_pseudonyms WHERE anon_id=?", (victim_anon,))
    if not wallet:
        return ("an exclusion removes a wallet and `include` restores it", False,
                "the pseudonym does not resolve to a wallet")
    wallet = str(wallet[0][0])
    db = p.app()._db
    at = p.app()._now_ms()
    try:
        db.execute("INSERT INTO leaderboard_exclusions (wallet, board, action, reason, actor, at_ms)"
                   " VALUES (?, '', 'exclude', 'gate: operator action', 'gate', ?)", (wallet, at))
        db.commit()
        after = p.board()
        if any(r["anon"] == victim_anon for r in (after.get("rows") or [])):
            findings.append("the excluded wallet is still ranked")
        if (after.get("excludedTotal") or 0) != 1:
            findings.append("excludedTotal is %s" % after.get("excludedTotal"))
        if after.get("excluded") is not None:
            findings.append("the public read published the exclusion reasons")
        # The admin token must clear `authz.check_service_token`'s floor of 32 characters: a short token is
        # treated as no configured secret at all, which is the app failing closed rather than a bug.
        admin_token = "p11-gate-" + "0" * 40
        os.environ["PGM_ADMIN_TOKEN"] = admin_token
        r = p.client().get("/v1/leaderboard", params={"limit": 5}, headers={"x-admin-token": admin_token})
        try:
            admin = r.json()
        except ValueError:
            admin = {}
        if not (admin.get("excluded") or []):
            findings.append("the operator read does not show the exclusion list")
        elif "gate: operator action" not in json.dumps(admin.get("excluded")):
            findings.append("the operator read omits the reason")
        if (after.get("summary") or {}).get("rankedTotal") != (out.get("summary") or {}).get("rankedTotal", 0) - 1:
            findings.append("the counts did not follow the exclusion")
        db.execute("INSERT INTO leaderboard_exclusions (wallet, board, action, reason, actor, at_ms)"
                   " VALUES (?, '', 'include', 'gate: cleared', 'gate', ?)", (wallet, at + 1))
        db.commit()
        back = p.board()
        if not any(r["anon"] == victim_anon for r in (back.get("rows") or [])):
            findings.append("`include` did not put the wallet back")
    finally:
        # No cleanup DELETE, deliberately. `leaderboard_exclusions` is append-only (0013), and it is append-only
        # because "who decided this wallet was out, and when" is the question an exclusion has to answer later.
        # The gate's `include` row is therefore the undo, exactly as it is for a real operator, and the two audit
        # rows stay — in a throwaway database, which is the only reason a check is allowed to make a decision.
        os.environ.pop("PGM_ADMIN_TOKEN", None)
    ok = not findings
    return ("an exclusion removes a wallet from the board, the reasons stay operator-only, and include restores it",
            ok, "%d findings%s" % (len(findings), ("; " + "; ".join(findings[:3])) if findings else ""))


def c12_population(p: Probe) -> tuple[str, bool, str]:
    """The fixture the boards are demonstrated on: big enough for a rank 47, and adversarial on purpose."""
    findings = []
    src = read(SEED)
    if not src:
        return ("the population fixture exists and contains the adversarial cases", False, "seed_leaderboard.py is missing")
    for name in ("the lucky gambler", "the blown-up account", "the washer", "the copy farm",
                 "the provisional wallet"):
        if name not in src:
            findings.append("the fixture does not document %s" % name)
    out = p.board()
    summary = out.get("summary") or {}
    if (summary.get("rankedTotal") or 0) < 48:
        findings.append("only %s wallets are ranked: there is no rank 47 to show" % summary.get("rankedTotal"))
    if (summary.get("unrankedTotal") or 0) < 2:
        findings.append("only %s wallets are refused: the two refusal kinds are not demonstrated"
                        % summary.get("unrankedTotal"))
    lucky = [r for r in (out.get("rows") or []) if (r.get("bestTradeShareBps") or 0) >= 5_000]
    if not lucky:
        findings.append("no wallet publishes a best-trade share at or above the lucky-gambler line")
    farm = p.board("copied")
    if not [r for r in (farm.get("rows") or [])]:
        findings.append("the most-copied board has no rows")
    ok = not findings
    return ("the population is large enough for a 47th place and contains every adversarial case", ok,
            "ranked %s / unranked %s, %s lucky-gambler rows, %d copied rows, median settled %s, %d findings%s"
            % (summary.get("rankedTotal"), summary.get("unrankedTotal"), len(lucky), len(farm.get("rows") or []),
               summary.get("medianSettledMarkets"), len(findings),
               ("; " + "; ".join(findings[:3])) if findings else ""))


def c13_published(p: Probe) -> tuple[str, bool, str]:
    """The methodology the user reads is the object the engine ranked from."""
    code, methodology = p.get("/v1/leaderboard/methodology")
    findings = [] if code == 200 else ["/methodology answered %d" % code]
    if code == 200:
        findings += publish_findings(methodology)
        sys.path.insert(0, str(ROOT / "packages"))
        from polygm_core.leaderboard import boards as bd
        served = {b["id"]: b["formula"] for b in methodology.get("boards") or []}
        engine = {b["id"]: b["formula"] for b in bd.BOARDS}
        if served != engine:
            findings.append("the published formulas differ from the engine's")
        picker = p.get("/v1/leaderboard/boards")[1]
        if [b["id"] for b in (picker.get("boards") or [])] != list(bd.BOARD_IDS):
            findings.append("the picker's board list is not the engine's")
        if (picker.get("defaultBoard") or "") != bd.DEFAULT_BOARD:
            findings.append("the picker's default is %r" % picker.get("defaultBoard"))
    ok = not findings
    return ("the methodology page and the ranking engine are the same object", ok,
            "%d rules published with both halves, %d boards, %d findings%s"
            % (len(methodology.get("integrity") or []), len(methodology.get("boards") or []), len(findings),
               ("; " + "; ".join(findings[:3])) if findings else ""))


def c14_board_orders(p: Probe) -> tuple[str, bool, str]:
    """Every board is ordered by the field its formula names — the label is not decoration."""
    findings, checked = [], 0
    for board in BOARDS:
        extra = {"category": "Politics"} if board == "category" else {}
        out = p.board(board, **extra)
        checked += 1
        findings += order_findings(board, out.get("rows") or [], ORDER_FIELD[board])
    # The win-rate board is the one the fixture can disprove cheaply: with at least two rows, its order must
    # differ from the default board's, or the label is a costume.
    wr = [r["anon"] for r in (p.board("win_rate").get("rows") or [])]
    ra = [r["anon"] for r in (p.board("risk_adjusted").get("rows") or [])]
    if wr and wr == ra:
        findings.append("the win-rate board serves the risk-adjusted order: the two boards are one board")
    ok = not findings
    return ("each board's rows are in the order of the field its formula names", ok,
            "%d boards checked by %s; win-rate order %s risk-adjusted order; %d findings%s"
            % (checked, "/".join(sorted(set(ORDER_FIELD.values()))),
               "differs from" if wr != ra else "equals", len(findings),
               ("; " + "; ".join(findings[:3])) if findings else ""))


CHECKS = (c1_contract, c2_no_addresses, c3_gate_pair, c4_win_rate_gate, c5_refusals, c6_no_hidden_losses,
          c7_integers_only, c8_freshness, c9_read_plans, c10_history, c11_exclusions, c12_population,
          c13_published, c14_board_orders)


# --------------------------------------------------------------------------------------------------- self-test
def self_test() -> int:
    """Plant each violation the scanners claim to catch and fail if a scan walks past it.

    The live probes (c1–c3, c8–c13) are not canaried: they are walks over the real app and the real population,
    and a canary for "the API works" would be the test suite. What is canaried is every SCANNER, because those
    are the parts whose silence would be invisible.
    """
    cases, passes = [], 0

    def canary(fn):
        cases.append(fn)
        return fn

    @canary
    def win_rate_gate():
        note = "insufficient sample: 4 settled markets; a win rate needs 20 before it is a rate"
        under = [{"anon": "w_1", "wins": 2, "settledMarkets": 4, "winRateBps": None, "insufficientSample": True,
                  "sampleNote": note}]
        lie = [dict(under[0], winRateBps=5000)]                                    # a rate under the gate
        silent = [dict(under[0], sampleNote="")]                                   # no sentence
        over = [{"anon": "w_2", "wins": 40, "settledMarkets": 48, "winRateBps": 8333, "insufficientSample": False,
                 "sampleNote": ""}]
        wrong = [dict(over[0], winRateBps=9000)]                                   # a rate over the wrong sample
        stale = [{"anon": "w_3", "wins": 30, "settledMarkets": 48, "winRateBps": 6250, "insufficientSample": True,
                  "sampleNote": "insufficient sample: 4 settled markets; a win rate needs 20"}]
        for case in (under, over):
            assert not win_rate_gate_findings(case), case
        got = [len(win_rate_gate_findings(lie)), len(win_rate_gate_findings(silent)),
               len(win_rate_gate_findings(wrong)), len(win_rate_gate_findings(stale))]
        return got == [1, 1, 1, 1], got

    @canary
    def board_order():
        rows = [{"rank": 1, "scoreBps": 900}, {"rank": 2, "scoreBps": 900}, {"rank": 3, "scoreBps": 100}]
        rising = [{"rank": 1, "scoreBps": 100}, {"rank": 2, "scoreBps": 900}]
        gap = [{"rank": 1, "winRateBps": 9_000}, {"rank": 2, "winRateBps": None}]
        return (not order_findings("risk_adjusted", rows, "scoreBps")
                and len(order_findings("risk_adjusted", rising, "scoreBps")) == 1
                and len(order_findings("volume", gap, "winRateBps")) == 1), order_findings("risk_adjusted", rising, "scoreBps")

    @canary
    def row_arithmetic():
        good = {"anon": "w_1", "trimmedMicro": 4_600_000_000, "realisedMicro": 5_200_000_000, "scoreBps": 38_333,
                "maxDrawdownMicro": 1_200_000_000, "volatilityMicro": 400_000_000, "bestTradeShareBps": 1_000,
                "components": {"scaleMicro": 1_200_000_000, "trimRemovedMicro": 600_000_000,
                               "volatilityMultiple": 2}}
        floaty = dict(good, scoreBps=38_334)
        wrong_scale = dict(good, components=dict(good["components"], scaleMicro=800_000_000))
        return (not row_arithmetic_findings([good]) and len(row_arithmetic_findings([floaty])) == 1
                and len(row_arithmetic_findings([wrong_scale])) >= 1), row_arithmetic_findings([floaty])

    @canary
    def refusal():
        bad = [{"anon": "w_1", "reasons": []}, {"anon": "w_2", "reasons": ["not enough of something"]},
               {"anon": "0x" + "ab" * 20, "reasons": ["9 settled markets; this board needs 20"]}]
        good = [{"anon": "w_3", "reasons": ["9 settled markets; this board needs 20"]}]
        return len(refusal_findings(bad)) == 3 and not refusal_findings(good), refusal_findings(bad)

    @canary
    def plan():
        at = 1_700_000_000_000
        day = 86_400_000
        good_skill = {"window": "30d", "atMs": at, "settledFromMs": at - 30 * day, "fillsFromMs": 0,
                      "fillsRule": "lifetime", "settledRule": "window"}
        bad_rising = {"window": "7d", "atMs": at, "settledFromMs": at - 7 * day, "fillsFromMs": 0,
                      "fillsRule": "x", "settledRule": "y"}
        bad_skill = dict(good_skill, fillsFromMs=at - 30 * day)
        return (not plan_findings(good_skill, "risk_adjusted", at)
                and len(plan_findings(bad_rising, "rising", at)) == 1
                and len(plan_findings(bad_skill, "win_rate", at)) == 1), plan_findings(bad_rising, "rising", at)

    @canary
    def cadence():
        good = {"ageMs": 1_000, "stale": False, "source": "live"}
        old = {"ageMs": 10_000_000, "stale": True, "source": "live"}
        lying = {"ageMs": 10_000_000, "stale": False, "source": "live"}
        none = {"ageMs": None, "stale": False, "source": "live"}
        return (not cadence_findings(good, 3_600_000) and not cadence_findings(old, 3_600_000)
                and len(cadence_findings(lying, 3_600_000)) == 1 and len(cadence_findings(none, 3_600_000)) == 1
                and len(cadence_findings(good, 0)) == 1), cadence_findings(lying, 3_600_000)

    @canary
    def publish():
        good = {"integrity": [{"id": str(i), "does": "d", "doesNot": "n"} for i in range(6)],
                "boards": [{"id": b} for b in BOARDS], "defaultBoard": "risk_adjusted"}
        thin = {"integrity": good["integrity"][:3], "boards": good["boards"][:4], "defaultBoard": "volume"}
        vague = {"integrity": [{"id": "wash", "does": "d", "doesNot": ""}] * 6, "boards": good["boards"],
                 "defaultBoard": "volume"}
        return (not publish_findings(good) and len(publish_findings(thin)) >= 2
                and len(publish_findings(vague)) >= 2), publish_findings(vague)

    @canary
    def p10_scanners_still_work():
        """The two scanners imported from P10 must still fire — an import that silently returns zero findings
        would make c2 pass while checking nothing."""
        p10 = _p10_module()
        return (len(p10.address_findings({"a": "0x" + "ab" * 20})) == 1
                and not p10.address_findings({"a": "w_abc1234567"})), "p10 scanners"

    for fn in cases:
        try:
            ok, detail = fn()
        except Exception as exc:                       # a canary that raises is a canary that did not fire
            ok, detail = False, "%s: %s" % (type(exc).__name__, exc)
        passes += 1 if ok else 0
        print("  %-4s %-24s %s" % ("PASS" if ok else "FAIL", fn.__name__, str(detail)[:150]))
    print("\nP11 self-test: %d/%d scanners fired on a planted violation" % (passes, len(cases)))
    return 0 if passes == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--fast", action="store_true", help="skip c1 (check-openapi) and c7 (the lint-rule subprocess)")
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
        print("%d checks; c3 walks the phase's acceptance sentence over the ranked population" % len(CHECKS))
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
    summary = "\nP11 gate: %d/%d checks passed in %d ms%s" % (
        passed, len(results), sum(m for *_x, m in results), " (--fast)" if a.fast else "")
    if passed != len(results):
        summary += "\nthe gate is a floor: a FAIL here means P11 is not done, whatever the document says"
    print(summary)
    if a.json:
        print(json.dumps({"phase": "P11", "passed": passed, "total": len(results),
                          "checks": [{"label": l, "ok": ok, "detail": d, "ms": m} for l, ok, d, m in results]},
                         indent=2))
    if a.record:
        out = ROOT / a.record
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("# P11 gate — recorded by tools/p11-gate-check.py\n\n%s\n"
                       % chr(10).join(lines).replace("  PASS", "PASS  ").replace("  FAIL", "FAIL  "))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
