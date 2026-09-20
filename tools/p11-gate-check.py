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
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".tmp"
#: The web tree. D6 is the first phase whose deliverable lives here — three SSR pages and their card routes —
#: so the gate needs a root for them; the rule the other checks follow still holds, and every path below is named
#: rather than globbed, because a check that says "some page exists" is a check that passes on the wrong page.
WEB = ROOT / "web"
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
             "/v1/leaderboard/snapshots", "/v1/leaderboard/runs", "/v1/leaderboard/recompute",
             # D3. The standing and the comparison are public reads; the follow list is the account's own and the
             # follow itself is the one write a user makes about other people.
             "/v1/leaderboard/rank", "/v1/leaderboard/compare", "/v1/leaderboard/follows",
             # D4. The self-rank and the account's own publication state: three USER operations, because there is
             # no version of "where do I stand" that a stranger may read.
             "/v1/leaderboard/me", "/v1/leaderboard/identity")
#: The whole point of a leaderboard is that a stranger can read it, so six of the seven are public — and the
#: seventh is a user mutation, which the gate also walks (a cadence nobody can exercise is untested).
PUBLIC_OPS = tuple("GET %s" % p for p in P11_PATHS
                   if p not in ("/v1/leaderboard/recompute", "/v1/leaderboard/follows",
                                "/v1/leaderboard/me", "/v1/leaderboard/identity"))
USER_OPS = ("POST /v1/leaderboard/recompute", "GET /v1/leaderboard/follows", "POST /v1/leaderboard/follows",
            "GET /v1/leaderboard/me", "GET /v1/leaderboard/identity", "POST /v1/leaderboard/identity")

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


#: D6's three SSR pages and their card routes, as the web tree lays them out.
#:
#: The segment names are `[who]` and `[market]` rather than `[handle]`/`[slug]` because each URL space already had
#: a dynamic segment (the dossier's pseudonym, the app's market id) and Next forbids two dynamic names at one
#: level. The PUBLIC path is the one the API publishes (`/trader/<handle>`, `/market/<slug>`,
#: `/leaderboard/<board>`), and that is what this list is about: the file names are an implementation detail, the
#: URLs are a promise.
PUBLIC_PAGES = (
    "app/trader/[who]/page.tsx",
    "app/trader/[who]/opengraph-image.tsx",
    "app/market/[market]/page.tsx",
    "app/market/[market]/opengraph-image.tsx",
    "app/leaderboard/[board]/page.tsx",
    "app/leaderboard/[board]/opengraph-image.tsx",
    "app/leaderboard/[board]/w/[window]/page.tsx",
    "app/leaderboard/[board]/w/[window]/opengraph-image.tsx",
    "app/leaderboard/[board]/c/[category]/page.tsx",
    "app/leaderboard/[board]/c/[category]/opengraph-image.tsx",
)

#: The views and the pure layer behind them.
PUBLIC_VIEWS = ("src/public/TraderView.tsx", "src/public/MarketView.tsx", "src/public/BoardView.tsx",
                "src/public/ShareCard.tsx", "src/public/Chrome.tsx", "src/public/JsonLd.tsx",
                "src/public/card.ts", "src/public/rows.ts", "src/public/og.tsx", "src/public/boardRoute.ts",
                "src/public/wire.ts")


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

    def list_handle(self, uid: str, handle: str, *, rank: int = 0) -> tuple[int, dict]:
        """Publish `handle` on a wallet the board actually RANKS, as `uid`.

        The subtlety this helper exists for: the identity write is made as the account that claimed the wallet, and
        a probe posting as `u-demo` while the wallet belongs to somebody else gets a 422 about a missing wallet
        rather than a page. (The gate found that the first time it ran: the check said "the handle could not be
        listed" and the real reason was the wrong principal.)
        """
        app = self.app()
        rows = self.board(board="risk_adjusted").get("rows") or []
        for row in rows[rank:]:
            wallet = app._wallet_for_anon(row["anon"]) or ""
            if not wallet:
                continue
            # A wallet another account already claimed cannot be re-claimed (`user_identities` is UNIQUE on
            # (kind, value)), so the helper walks down the board until it finds one nobody in this run has taken.
            owner = self.rows("SELECT user_id FROM user_identities WHERE kind='wallet' AND value=?", (wallet,))
            if owner and str(owner[0][0]) != str(uid):
                continue
            self.link(uid, wallet)
            return self.post_as(uid, "/v1/leaderboard/identity", {"state": "listed", "handle": handle})
        return 0, {}

    def post_key(self, url: str, body: dict) -> tuple[int, dict]:
        """A write with a well-formed key: every mutating route here requires one, and a 422 about the key would
        be a check measuring the key's shape instead of the thing it is about."""
        key = "g11-%s-%04d" % (url.strip("/").replace("/", "-"), getattr(self, "_post_n", 0) + 1)
        return self.post(url, body, key=key)

    def get_user(self, url: str, **params) -> tuple[int, dict]:
        r = self.client().get(url, params=params, headers={"X-User-Id": "u-demo"})
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    def as_user(self, uid: str, url: str, **params) -> tuple[int, dict]:
        """A read as SOMEBODY ELSE. A referral check needs more than one account, and the two sides must not be
        the same session: the whole point of the pair is that one account referred another."""
        r = self.client().get(url, params=params, headers={"X-User-Id": str(uid)})
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    def post_as(self, uid: str, url: str, body: dict, *, key: str = "", headers: dict | None = None):
        n = getattr(self, "_post_n", 0) + 1
        self._post_n = n
        hdrs = {"X-User-Id": str(uid),
                "Idempotency-Key": key or "g11-%s-%04d" % (url.strip("/").replace("/", "-"), n)}
        hdrs.update(headers or {})
        r = self.client().post(url, json=body, headers=hdrs)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    def admin(self) -> str:
        """Set an operator token that clears `authz.check_service_token`'s 32-character floor and return it."""
        token = "p11-gate-" + "0" * 40
        os.environ["PGM_ADMIN_TOKEN"] = token
        return token

    def account(self, uid: str) -> str:
        """A fresh account with no wallet, so nothing it does can perturb the boards."""
        self.exec("INSERT OR IGNORE INTO users (id, created_ms, tier) VALUES (?,?, 'free')", (str(uid), 1))
        return str(uid)

    def attr_row(self, referee: str, order: str, *, observed: int | None, expected: int | None = None,
                 at: int | None = None) -> None:
        """One attributable order, as the ingest would have written it: the venue's OWN fee number, or NULL when
        the fee has not been paid yet — which is the state the accrual run must walk past."""
        self.exec("INSERT INTO builder_attribution (order_id, intent_id, user_id, builder_code, fee_bps_expected,"
                  " fee_micro_expected, fee_micro_observed, market_id, token_id, placed_ms)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (str(order), "0xintent-%s" % order, str(referee), "polygm-referral", 25,
                   int(expected if expected is not None else (observed or 0)), observed, "0xgate-m", "0xgate-t",
                   int(at if at is not None else self.app()._now_ms() - 60_000)))

    def today(self) -> str:
        import datetime as _dt
        return _dt.datetime.fromtimestamp(self.app()._now_ms() / 1000, _dt.timezone.utc).strftime("%Y-%m-%d")

    def rows(self, sql: str, args: tuple = ()) -> list:
        return self.app()._db.execute(sql, args).fetchall()

    def exec(self, sql: str, args: tuple = ()) -> None:
        """A write, for the gate's fixture only: linking a wallet to the probe's account the way P07 records one,
        and the second account D4's collision check needs. The API itself is probed through its routes."""
        self.app()._db.execute(sql, args)
        self.app()._db.commit()

    def link(self, uid: str, wallet: str) -> str:
        """Claim `wallet` for `uid`, and return the pseudonym — the identity every board read is keyed by."""
        app = self.app()
        self.exec("INSERT OR IGNORE INTO users (id, created_ms, tier) VALUES (?,?, 'free')", (str(uid), 1))
        self.exec("DELETE FROM user_identities WHERE kind='wallet' AND user_id=?", (str(uid),))
        self.exec("INSERT INTO user_identities (kind, value, user_id, state, claimed_ms, verified_ms,"
                  " proof_kind, revoked_ms) VALUES ('wallet',?,?,'verified',?,?, 'gate', NULL)",
                  (str(wallet), str(uid), 1, 1))
        return app._anon(str(wallet))

    def wallet_of_unranked(self) -> str:
        """One refused wallet's ADDRESS: the fixture's own, since `seed_leaderboard` is what put it there."""
        app = self.app()
        unranked = self.board(board="risk_adjusted").get("unranked") or []
        for u in unranked:
            w = app._wallet_for_anon(u["anon"])
            if w:
                return w
        return ""

    def board(self, board: str = "risk_adjusted", **params) -> dict:
        code, out = self.get("/v1/leaderboard", board=board, limit=200, **params)
        return out if code == 200 else {}

    def public_payloads(self) -> list[tuple[str, dict]]:
        """What an ANONYMOUS caller can read, as `(label, body)`.

        This is the set the privacy scanner gets: a handle that has not been published must appear here nowhere, and
        the account's own view of its own setting is not a public payload. The distinction is the whole D4 rule -
        "kept, not published" - so it is a named method rather than a subset built inside a check.
        """
        return [(label, body) for label, body in self.payloads()
                if label not in ("/v1/leaderboard/me", "/v1/leaderboard/identity")]

    def payloads(self) -> list[tuple[str, dict]]:
        """Every leaderboard payload the probe can read, `(label, body)` — the input to the scanner checks."""
        out = [("/v1/leaderboard/boards", self.get("/v1/leaderboard/boards")[1]),
               ("/v1/leaderboard/methodology", self.get("/v1/leaderboard/methodology")[1]),
               ("/v1/leaderboard/runs", self.get("/v1/leaderboard/runs")[1])]
        for board in BOARDS:
            extra = {"category": "Politics"} if board == "category" else {}
            out.append(("board:%s" % board, self.board(board, **extra)))
        # D4: the account's own panel is a payload like any other for the scanners. It is USER-scoped, and the one
        # thing it must never do is carry an address or a handle that has not been published.
        out.append(("/v1/leaderboard/me", self.get_user("/v1/leaderboard/me")[1]))
        out.append(("/v1/leaderboard/identity", self.get_user("/v1/leaderboard/identity")[1]))
        return out


# --------------------------------------------------------------------------------------------------- scanners
ADDRESS_RX = re.compile(r"0x[0-9a-fA-F]{6,}")


def card_findings(payload: dict) -> list[str]:
    """The card and the structured data, checked against the page they were served with.

    Two rules, both of them about the artifact that travels alone. **The card must not be able to disagree with the
    page**: every claim in the graph has to be a string the payload also carries, and the vocabulary rule that
    decides what counts as a claim (`ProfilePage`, `@type` values, URLs) is NOT re-implemented here — it is imported
    from `polygm_core.public_pages.structured`, the layer that builds the graph and whose `unbacked()` is unit-tested
    in `tests/test_public_pages.py`. A second opinion about what counts as a claim is a second answer to "is this
    page lying", and this repo has already paid for that lesson once.

    And **the card must carry its qualifiers**: a card that says it is provisional without saying so, or prints a
    profit with no drawdown, is the failure this whole deliverable exists to prevent — so the scanner reads the
    card's own `provisional` flag rather than inferring the duty from `ranked`.
    """
    out: list[str] = []
    sys.path.insert(0, str(ROOT / "packages"))
    from polygm_core.public_pages import structured as pp_structured

    page = {k: v for k, v in payload.items() if k != "structuredData"}
    # `unbacked(graphs, payload)` wants an ITERABLE of nodes: a bare dict iterates its keys, and the first run of
    # this scanner obliged by reporting `$ = '@type'` — a finding about the scanner rather than about the page.
    graph = payload.get("structuredData") or []
    out += ["the graph claims %s, which is not a field on the page" % claim[:64]
            for claim in pp_structured.unbacked(graph, page)]
    card = payload.get("card") or {}
    foot = " | ".join(str(f).lower() for f in (card.get("footnote") or []))
    if card.get("provisional") and "provisional" not in foot:
        out.append("a provisional card carries no provisional line: %s" % foot)
    if card.get("ranked") and "drawdown" not in foot:
        out.append("a ranked card carries no drawdown")
    if not (payload.get("notes") or []):
        out.append("the page carries no notes, so nothing on it qualifies the row")
    return out
WALLET_RX = re.compile(r"\b0x[0-9a-fA-F]{40}\b")


def public_payload_findings(payloads: list[tuple[str, dict]]) -> list[str]:
    """Every string in every public payload, checked for an address at any depth.

    `json.dumps` rather than a hand-walk, because the failure this catches is a field a component added three
    levels down: an `evidence` object, a sorted `links` map, a `structuredData` node. The rule is the pseudonym
    scheme's, and it has to hold at every depth or it is decoration.
    """
    findings: list[str] = []
    for label, body in payloads:
        blob = json.dumps(body, default=str)
        hits = WALLET_RX.findall(blob) or ADDRESS_RX.findall(blob)
        if hits:
            findings.append("%s carries an address: %s" % (label, hits[:2]))
    return findings


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


def standing_findings(standing: dict, rows: dict) -> list[str]:
    """The standing has to be the row it came from, with the gap re-derivable from the two neighbours.

    Four plantings this catches: a badge that disagrees with the rank it labels; a gap computed against a wallet
    that is not the one directly above; a `toPass` that equals the neighbour's value (a tie does not pass — the
    board's tie-breaks decide); and a percentile that is not `rank / board` rounded up, which is the difference
    between "the top 74%" and "the bottom 26%" for the same row.
    """
    f = []
    anon = standing.get("anon")
    if standing.get("state") == "unranked":
        if standing.get("rank") is not None or standing.get("rankBadge") is not None:
            f.append("%s is unranked but carries a placing" % anon)
        if not standing.get("reasons"):
            f.append("%s is unranked with no reason" % anon)
        return f
    rank = standing.get("rank")
    total = standing.get("rankedTotal") or 0
    if (standing.get("rankBadge") or {}).get("rank") != rank or (standing.get("rankBadge") or {}).get("text") != "#%d" % rank:
        f.append("%s's badge disagrees with rank %s" % (anon, rank))
    if (standing.get("rankBadge") or {}).get("rankedTotal") != total:
        f.append("%s's badge states a board size of %s, the board is %s"
                 % (anon, (standing.get("rankBadge") or {}).get("rankedTotal"), total))
    if rank and rank > 1:
        above = rows.get(rank - 1)
        if above is None:
            f.append("rank %s has no neighbour above it in the board's own rows" % rank)
        elif (standing.get("above") or {}).get("anon") != above["anon"]:
            f.append("%s's `above` is not the wallet ranked %s" % (anon, rank - 1))
        else:
            gap = standing.get("gap") or {}
            field = gap.get("field")
            if field != standing.get("orderField"):
                f.append("the gap is measured in %s while the board orders by %s"
                         % (field, standing.get("orderField")))
            elif gap.get("value") != rows[rank][field] or gap.get("valueAbove") != above[field]:
                f.append("the gap's two values are not the two rows' %s" % field)
            elif gap.get("delta") != above[field] - rows[rank][field]:
                f.append("the gap's delta is not above - mine")
            elif gap.get("toPass") != above[field] + 1:
                f.append("toPass is %s: a tie does not pass, so it has to be %s"
                         % (gap.get("toPass"), above[field] + 1))
        if (standing.get("below") or {}).get("anon") != (rows.get(rank + 1) or {}).get("anon"):
            f.append("%s's `below` is not the wallet ranked %s" % (anon, rank + 1))
    elif (standing.get("gap") is not None or standing.get("above") is not None):
        f.append("the top of the board has something above it")
    if total and standing.get("percentileBps") != (rank * 10_000 + total - 1) // total:
        f.append("%s's percentile %s is not ceil(rank/board) for %s of %s"
                 % (anon, standing.get("percentileBps"), rank, total))
    return f


def follow_findings(follow: dict, listing: dict, anon: str) -> list[str]:
    """A follow is a watch: it says so, it is keyed by a pseudonym, and it never claims to trade."""
    f = []
    if follow.get("state") not in ("followed", "unfollowed"):
        f.append("the follow answered state=%r" % follow.get("state"))
    if follow.get("anon") != anon:
        f.append("the follow was acknowledged for %r" % follow.get("anon"))
    if "not a copy" not in str(follow.get("note") or ""):
        f.append("the acknowledgement does not say what a follow is not")
    if "not a copy" not in str(listing.get("note") or ""):
        f.append("the list does not say what a follow is not")
    for row in listing.get("rows") or []:
        if not str(row.get("anon") or "").startswith("w_"):
            f.append("a followed row is not a pseudonym: %r" % row.get("anon"))
        if row.get("state") not in ("ranked", "unranked", "absent"):
            f.append("a followed row is in state %r" % row.get("state"))
        if row.get("state") == "unranked" and not row.get("reasons"):
            f.append("a followed wallet left the board with no reason")
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
            % (len(P11_PATHS), "all mapped" if not unmapped else "%d unmapped" % len(unmapped),
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


def c15_wallet_standing(p: Probe) -> tuple[str, bool, str]:
    """A wallet's standing is the board's own row, with a gap that can be re-derived from its neighbours.

    D3's profile integration and D4's pinned self-rank both render this, so the two things it must get right are
    the things a reader can check: the badge is the rank, the neighbours are the neighbours, and the number to
    the place above is stated in the field the board is ordered by.
    """
    board = p.board(board="risk_adjusted")
    rows = {r["rank"]: r for r in (board.get("rows") or [])}
    findings, checked = [], 0
    for rank in (1, 12, 47):
        row = rows.get(rank)
        if row is None:
            continue
        code, standing = p.get("/v1/leaderboard/rank", anon=row["anon"], days=30)
        checked += 1
        if code != 200:
            findings.append("rank %d's standing answered %d" % (rank, code))
            continue
        findings += ["rank %d: %s" % (rank, f) for f in standing_findings(standing, rows)]
        if rank == 47 and standing.get("gap"):
            gap = standing["gap"]
            if not (gap["toPass"] > gap["valueAbove"] > gap["value"] - 1):
                findings.append("rank 47's gap is not a gap: %s" % gap)
    # An unranked wallet is the other half of the same surface: no placing, and the refusal with its number.
    unranked = board.get("unranked") or []
    if unranked:
        code, standing = p.get("/v1/leaderboard/rank", anon=unranked[0]["anon"])
        checked += 1
        if code != 200:
            findings.append("an unranked wallet's standing answered %d" % code)
        else:
            findings += standing_findings(standing, rows)
    if p.get("/v1/leaderboard/rank", anon="w_0000000000")[0] != 404:
        findings.append("an unknown pseudonym did not 404")
    ok = not findings
    detail = "%d stand%s checked" % (checked, "ing" if checked == 1 else "ings")
    if checked and rows.get(47):
        g47 = p.get("/v1/leaderboard/rank", anon=rows[47]["anon"])[1].get("gap") or {}
        detail += "; rank 47's gap is %s %s of %s, toPass %s" % (g47.get("delta"), g47.get("units"),
                                                                 g47.get("field"), g47.get("toPass"))
    return ("a wallet's standing is the board's own row, with a re-derivable gap to the place above", ok,
            detail + ("; %d findings%s" % (len(findings), "; " + "; ".join(findings[:3])) if findings else ""))


def c16_comparison_and_follows(p: Probe) -> tuple[str, bool, str]:
    """Compare up to three, follow without copying, and neither one ever carries an address."""
    p10 = _p10_module()
    board = p.board(board="risk_adjusted")
    rows = board.get("rows") or []
    findings, notes = [], []
    if len(rows) < 3:
        return ("compare and follow behave as a watch and a table", False, "not enough rows to compare")
    a, b, c = rows[46]["anon"], rows[11]["anon"], rows[0]["anon"]
    code, cmp_ = p.get("/v1/leaderboard/compare", anons="%s,%s,%s" % (a, b, c))
    if code != 200:
        findings.append("compare answered %d" % code)
    else:
        asked = [a, b, c]
        if [r["anon"] for r in cmp_.get("rows") or []] != asked:
            findings.append("compare did not return the wallets in the order asked")
        if len(cmp_.get("order") or []) != 3:
            findings.append("three wallets is three pairwise sentences, got %d" % len(cmp_.get("order") or []))
        for pair in cmp_.get("order") or []:
            if "the sample size did not decide this" not in (pair.get("why") or ""):
                findings.append("a pairwise sentence does not name the sample")
                break
        if len(cmp_.get("order") or []) == 3:
            notes.append("3 pairwise sentences, all naming the sample")
    for bad, why in ((a, "one wallet"), ("%s,%s" % (a, a), "the same wallet twice"),
                     ("%s,%s,%s,%s" % (a, b, c, rows[1]["anon"]), "four wallets")):
        if p.get("/v1/leaderboard/compare", anons=bad)[0] != 422:
            findings.append("%s was accepted by compare" % why)
    # Follow, then read the list back with the standing attached, then unfollow.
    before = p.get_user("/v1/leaderboard/follows")[1].get("total")
    code, follow = p.post_key("/v1/leaderboard/follows", {"anon": b, "label": "gate"})
    if code != 200:
        findings.append("following answered %d" % code)
    else:
        listing = p.get_user("/v1/leaderboard/follows")[1]
        findings += follow_findings(follow, listing, b)
        followed = [r for r in listing.get("rows") or [] if r["anon"] == b]
        if not followed:
            findings.append("the follow is not in the list")
        else:
            row = followed[0]
            if row.get("rank") != 12 and row.get("state") == "ranked":
                findings.append("the followed wallet's standing says rank %s, the board says 12" % row.get("rank"))
            if not row.get("drawdown"):
                findings.append("the followed row carries a PnL without its drawdown")
            notes.append("followed %s (%s, rank %s)" % (b, row.get("state"), row.get("rank")))
        findings += ["follow payload %s" % f for f in p10.address_findings(listing, "follows")]
        code2, back = p.post_key("/v1/leaderboard/follows", {"anon": b, "state": "unfollow"})
        if code2 != 200 or back.get("state") != "unfollowed":
            findings.append("unfollowing answered %d %s" % (code2, json.dumps(back)[:60]))
        after = p.get_user("/v1/leaderboard/follows")[1].get("total")
        if after != before:
            findings.append("the follow list went %s -> %s across follow+unfollow" % (before, after))
    # An address is not a pseudonym anywhere on this surface.
    for where, got in (("a follow", p.post_key("/v1/leaderboard/follows", {"anon": "0x" + "ab" * 20})),
                       ("a comparison", p.get("/v1/leaderboard/compare", anons="0x%s,0x%s" % ("ab" * 20, "cd" * 20)))):
        text = json.dumps(got[1])
        if p10.ADDRESS_RX.search(text):
            findings.append("%s echoed an address" % where)
    ok = not findings
    return ("a comparison is one read of one board, and a follow is a watch that never trades", ok,
            "%s%s" % ("; ".join(notes) if notes else "no notes",
                      "; %d findings%s" % (len(findings), "; " + "; ".join(findings[:3])) if findings else ""))


# ------------------------------------------------------------------ D4 scanners: the self-rank and the published identity
def self_rank_findings(me: dict, boards: tuple, categories: tuple, page: int = 50) -> list[str]:
    """The self-rank panel, re-derived: every board, the pin on the default board, and an honest off-page flag.

    Four plantings this catches — a panel that answers five of the six boards and calls the sixth "not applicable";
    an `offPage` that disagrees with the rank and the page size (the field the sticky strip is drawn from, so a
    wrong one pins the wrong row); a badge that is not the rank; and a ranked row with no gap to the place above,
    which is the number the whole panel exists to show.
    """
    f: list[str] = []
    wallets = me.get("wallets") or []
    if not wallets:
        return ["no wallet in the self-rank: an unlinked account must still be a described state, not an empty one"]
    for entry in wallets:
        rows = entry.get("boards") or []
        ids = sorted({b.get("board") for b in rows})
        if ids != sorted(boards):
            f.append("boards covered are %s, not %s" % (ids, sorted(boards)))
        cats = sorted({b.get("category") for b in rows if b.get("category")})
        if cats != sorted(categories):
            f.append("the category board is four boards; got %s" % (cats or "none"))
        for b in rows:
            if int(b.get("pageSize") or 0) != page:
                f.append("%s: pageSize is %s, not %d" % (b.get("board"), b.get("pageSize"), page))
            if b.get("state") == "ranked":
                rank = int(b["rank"])
                if (b.get("rankBadge") or {}).get("text") != "#%d" % rank:
                    f.append("%s: badge %r is not the rank %d" % (b.get("board"), (b.get("rankBadge") or {}).get("text"), rank))
                if bool(b.get("offPage")) != (rank > page):
                    f.append("%s: offPage=%s at rank %d of a %d-row page" % (b.get("board"), b.get("offPage"), rank, page))
                if b.get("gap") is None and rank > 1:
                    f.append("%s: rank %d has no gap to the place above" % (b.get("board"), rank))
            elif b.get("offPage") is not True:
                f.append("%s: a wallet with no rank claims to be on the page" % b.get("board"))
    pin = ((me.get("primary") or {}).get("pin")) if me.get("primary") else None
    if me.get("walletCount") and me.get("primary"):
        if not pin:
            f.append("the pin is missing: the strip has nothing to draw")
        else:
            if pin.get("board") != me["primary"].get("defaultBoard"):
                f.append("the pin is %s, not the default board %s" % (pin.get("board"), me["primary"].get("defaultBoard")))
            if pin.get("category"):
                f.append("the pin is a category board: the strip would show a specialism the reader is not on")
    return f


def privacy_findings(payloads: list[tuple], handle: str, *, listed: bool) -> list[str]:
    """A private account's handle appears nowhere; a listed account's handle is on its rows and nowhere else.

    The interesting half is what stays: the wallet is on the board either way. A setting that removed a row would
    be a way out of a ranking, which is the one thing D1's integrity rules do not allow, so this scanner checks
    both directions — the handle is gone when it should be, and the ROW is not.
    """
    f: list[str] = []
    published = [label for label, body in payloads if handle and handle in json.dumps(body)]
    if listed and not published:
        f.append("a listed handle (%r) is in no public payload" % handle)
    if not listed and published:
        f.append("a private handle (%r) is in %s" % (handle, ", ".join(published[:3])))
    return f


def c17_self_rank_every_board(p: Probe) -> tuple[str, bool, str]:
    """D4: the account's own standing on every board, with the pin the sticky strip draws.

    The probe links a real seeded wallet to the fixture account the way P07 does — a row in `user_identities` — and
    then walks the panel: nine answers, the default board pinned, the gap in the board's own field, and an
    unranked wallet told which number refused it and what to do about it.
    """
    app = p.app()
    rows = p.board(board="risk_adjusted").get("rows") or []
    if len(rows) < 12:
        return ("the self-rank answers every board, with the pin on the default one", False, "no board to rank on")
    wallet = app._wallet_for_anon(rows[11]["anon"])
    p.link("u-demo", wallet)
    findings: list[str] = []
    code, me = p.get_user("/v1/leaderboard/me")
    if code != 200:
        return ("the self-rank answers every board, with the pin on the default one", False,
                "/me answered %d" % code)
    findings += self_rank_findings(me, BOARDS, ("Politics", "Sports", "Crypto", "Finance"))
    if me.get("walletCount") != 1:
        findings.append("walletCount is %s for one linked wallet" % me.get("walletCount"))
    if me.get("identity", {}).get("state") != "private":
        findings.append("a fresh account is not private by default: %s" % me.get("identity"))
    # the pin's rank has to be the rank the BOARD gives that wallet, not a second opinion
    pin = (me.get("primary") or {}).get("pin") or {}
    board_rank = next((r["rank"] for r in rows if r["anon"] == rows[11]["anon"]), None)
    if pin.get("rank") != board_rank:
        findings.append("the pin says rank %s and the board says %s" % (pin.get("rank"), board_rank))
    if pin.get("gap") is not None and pin["gap"]["toPass"] <= pin["gap"]["valueAbove"]:
        findings.append("toPass is not strictly above the wallet ahead: %s" % pin["gap"])
    # and an unranked wallet gets a to-do list rather than a shrug
    thin = p.wallet_of_unranked()
    if thin:
        p.link("u-demo", thin)
        code2, me2 = p.get_user("/v1/leaderboard/me")
        steps = (me2.get("wallets") or [{}])[0].get("nextSteps") or []
        if code2 != 200 or not steps:
            findings.append("an unranked wallet got no next steps")
        elif not any("settled market" in s or "turnover" in s for s in steps):
            findings.append("the next steps do not name a number: %s" % steps[:1])
    if p.get("/v1/leaderboard/me")[0] != 401:
        findings.append("/me answered an anonymous caller")
    ok = not findings
    detail = "9 board answers; pin rank %s of %s, offPage %s" % (pin.get("rank"), pin.get("rankedTotal"), pin.get("offPage"))
    return ("the self-rank answers every board, with the pin on the default one", ok,
            detail + ("; %d findings%s" % (len(findings), "; " + "; ".join(findings[:3])) if findings else ""))


def c18_identity_never_leaks(p: Probe) -> tuple[str, bool, str]:
    """D4: private by default, listed on request, and never a link where there is no opt-in.

    The phase's privacy line, walked: a fresh account publishes no handle anywhere, opting in attaches exactly one
    handle to its rows, a second account cannot take that handle, and going back to private removes the link
    WITHOUT removing the row — the board ranks wallets, and no setting may change that.
    """
    app = p.app()
    rows = p.board(board="risk_adjusted").get("rows") or []
    if not rows:
        return ("private by default, listed on request, and the row survives either way", False, "no board")
    wallet = app._wallet_for_anon(rows[10]["anon"])
    anon = rows[10]["anon"]
    p.link("u-demo", wallet)
    p.exec("INSERT OR IGNORE INTO users (id, created_ms, tier) VALUES ('u-gate-2', 1, 'free')")
    findings: list[str] = []

    # 1. nothing is published before the opt-in
    before = p.public_payloads() + [("/v1/leaderboard/rank", p.get("/v1/leaderboard/rank", anon=anon)[1])]
    findings += privacy_findings(before, "gate_handle", listed=False)
    row_before = next((r for r in rows if r["anon"] == anon), None)
    if row_before is None or row_before.get("handle") is not None:
        findings.append("a private wallet's row is missing or already carries a handle")

    # 2. the opt-in attaches the handle to that wallet's rows, and only those
    code, listed = p.post_key("/v1/leaderboard/identity", {"state": "listed", "handle": "Gate_Handle"})
    if code != 200:
        findings.append("the opt-in answered %d" % code)
    else:
        if listed.get("identity", {}).get("handle") != "gate_handle":
            findings.append("the handle was not normalised: %s" % listed.get("identity"))
        findings += privacy_findings(p.public_payloads(), "gate_handle", listed=True)
        carrying = [r["anon"] for r in (p.board(board="risk_adjusted").get("rows") or []) if r.get("handle")]
        if carrying != [anon]:
            findings.append("the handle is on %s, not just on the account's own row" % (carrying or "nothing"))
        if (p.get("/v1/leaderboard/rank", anon=anon)[1].get("row") or {}).get("handle") != "gate_handle":
            findings.append("the standing row did not gain the handle")

    # 3. a second account cannot take it, and its own state does not change
    taken = p.post("/v1/leaderboard/identity", {"state": "listed", "handle": "gate_handle"},
                   headers={"X-User-Id": "u-gate-2"})
    if taken[0] != 409 or taken[1].get("error", {}).get("code") != "HANDLE_TAKEN":
        findings.append("a second account claiming the handle got %s %s" % (taken[0], taken[1].get("error")))
    second = p.client().get("/v1/leaderboard/identity", headers={"X-User-Id": "u-gate-2"}).json()
    if second.get("identity", {}).get("state") != "private":
        findings.append("the failed claim left the second account listed")

    # 4. going private removes the link and keeps the ranking
    back = p.post_key("/v1/leaderboard/identity", {"state": "private"})
    if back[0] != 200 or back[1].get("identity", {}).get("state") != "private":
        findings.append("going private answered %s" % (back[0],))
    after = p.board(board="risk_adjusted").get("rows") or []
    row_after = next((r for r in after if r["anon"] == anon), None)
    if row_after is None:
        findings.append("the wallet left the board when it went private")
    elif row_after.get("handle") is not None or row_after.get("rank") != row_before.get("rank"):
        findings.append("going private changed the row: %s" % {k: row_after.get(k) for k in ("handle", "rank")})
    findings += privacy_findings(p.public_payloads(), "gate_handle", listed=False)
    # The account's own two payloads still carry the handle, and that is the rule rather than a leak: opting out
    # removes the LINK and keeps the name, so opting back in republishes the same identity. Asserted here so a
    # future "delete the handle on opt-out" change has to argue with the gate.
    own = [p.get_user("/v1/leaderboard/identity")[1], p.get_user("/v1/leaderboard/me")[1]]
    if not any("gate_handle" in json.dumps(body) for body in own):
        findings.append("going private forgot the handle entirely: the owner can no longer see what is retained")

    # 5. the consent is on the record, in the append-only log
    audit = p.rows("SELECT detail_json FROM audit_log WHERE action='leaderboard.identity' ORDER BY at_ms DESC")
    if len(audit) < 2:
        findings.append("the consent is not in the audit log (%d rows)" % len(audit))
    ok = not findings
    return ("private by default, listed on request, and the row survives either way", ok,
            "handle published on 1 of %d rows, then removed; %d findings%s"
            % (len(rows), len(findings), "; " + "; ".join(findings[:3]) if findings else ""))



# ------------------------------------------------------------------------------------- D5 · the referral rules
def referral_model_findings(accruals: list[dict], bps: int, qualifies: dict) -> list[str]:
    """The reward, re-derived from the rows: a share of a fee we were PAID, on a referee who actually traded.

    Four plantings this catches, and each one is a real way a referral programme pays for nothing: a share computed
    off the EXPECTED fee (money we have not been paid), a row with no observed fee at all (a signup or a deposit
    earning), an accrual for a referee with no qualifying order, and a share that is not `bps` of the fee — which
    is the difference between the published rate and the rate somebody typed.
    """
    f = []
    for row in accruals:
        referee = str(row.get("referee") or "")
        fee = row.get("feeObservedMicro")
        share = row.get("shareMicro")
        if not isinstance(fee, int) or fee <= 0:
            f.append("an accrual with fee %r: nothing was paid to us on it" % (fee,))
            continue
        want = fee * bps // 10_000
        if share != want:
            f.append("share %r is not %d bps of the observed fee %d (want %d)" % (share, bps, fee, want))
        if share is not None and fee is not None and share > fee:
            f.append("the share exceeds the fee: %r > %r" % (share, fee))
        if not qualifies.get(referee):
            f.append("an accrual for %s, who never placed a qualifying order" % referee)
    return f


def referral_collision_findings(attributions: list[dict], queue: list[dict], signals: dict) -> list[str]:
    """The three collisions, re-derived from the record: dedupe by signal, a queue item for every hold, no row for
    a self-referral.

    `signals` maps a referee to the set of digests we stored for them. A referee who shares a DIGEST with their own
    referrer is a self-referral wearing a hat; a second referee sharing one with an already-attributed referee of
    the same referrer is the kit's acceptance pair. Either one may exist only in `review`/`refused`, and each needs
    the queue item that tells a person to look at it — otherwise the money stops with nobody to ask.
    """
    f = []
    held = {str(q.get("referee")) for q in queue if str(q.get("state")) == "open"}
    by_referrer: dict[str, list[str]] = {}
    held_states: dict[str, str] = {}
    for a in attributions:
        by_referrer.setdefault(str(a.get("referrer")), []).append(str(a.get("referee")))
        held_states[str(a.get("referee"))] = str(a.get("state"))
    for a in attributions:
        referee, referrer, state = str(a.get("referee")), str(a.get("referrer")), str(a.get("state"))
        if referee == referrer:
            f.append("an attribution row points %s at themselves" % referee)
        shared = signals.get(referee, set()) & signals.get(referrer, set())
        if shared and state not in ("review", "refused"):
            f.append("%s shares a signal with the referrer and is %r, not held" % (referee, state))
        for other in by_referrer.get(referrer, []):
            if other == referee:
                continue
            if not (signals.get(referee, set()) & signals.get(other, set())):
                continue
            # BOTH halves of the pair have to be un-held before this is a hole: the first referee to arrive on a
            # shared device was clean when they arrived, and it is the second one the dedupe holds. A rule that
            # flagged the first as well would be a scanner demanding that the API retroactively punish the
            # account it already trusted.
            other_state = held_states.get(other, "pending")
            if state not in ("review", "refused") and other_state not in ("review", "refused"):
                f.append("%s and %s share a signal and neither is held" % (referee, other))
        if state in ("review", "refused") and referee not in held:
            f.append("a %s referral (%s) is not on the review queue" % (state, referee))
    return f


def referral_dashboard_findings(me: dict) -> list[str]:
    """The dashboard's arithmetic, re-derived: a monotone money chain, buckets that add up, no invented number.

    The kit asks for clicks -> signups -> funded -> trading -> earned -> pending -> paid, and the trap is the first
    step: clicks are a LEADING count and no later step is bounded by them (a code read aloud produces signups with
    no clicks), so the scanner checks the four money steps for monotonicity and checks clicks only for a floor of
    zero. Everything else is an identity that must hold against the rows below it.
    """
    f = []
    funnel = me.get("funnel") or {}
    counts = [int(funnel.get(k) or 0) for k in ("signups", "funded", "trading", "earned")]
    for i in range(1, len(counts)):
        if counts[i] > counts[i - 1]:
            f.append("the funnel is not monotone: %s" % counts)
            break
    if int(funnel.get("clicks") or 0) < 0:
        f.append("a negative click count")
    if me.get("funnelFindings"):
        f.append("the dashboard reported its own funnel as impossible: %s" % me["funnelFindings"][:1])
    earned = int((me.get("earnings") or {}).get("accruedMicro") or 0)
    rows = me.get("referrals") or []
    if sum(int(r.get("earnedMicro") or 0) for r in rows) > earned and not (me.get("hidden") or {}).get("refused"):
        f.append("the referees' rows exceed the earnings and nothing is hidden")
    buckets = me.get("earnings") or {}
    if int(buckets.get("settledMicro") or 0) + int(buckets.get("holdingMicro") or 0) != int(buckets.get("accruedMicro") or 0):
        f.append("settled + holding does not equal accrued: %s" % buckets)
    if int(buckets.get("paidMicro") or 0) > int(buckets.get("accruedMicro") or 0) + int(buckets.get("clawedBackMicro") or 0):
        f.append("paid exceeds everything ever accrued")
    if int(funnel.get("earned") or 0) != len([r for r in rows if int(r.get("earnedMicro") or 0) > 0]):
        f.append("the `earned` step is not the number of referees still owed money")
    return f


def referral_terms_findings(terms: dict, payout: dict) -> list[str]:
    """The payout reality and the published rules: the kit's "state it or you have hidden it".

    A referral programme is a money promise, so the parts a referrer must be able to check BEFORE they post a link
    are the ones this scanner refuses to let go missing: what earns (and what was rejected), when it is paid, how
    little is too little, what happens on a clawback, which tax form is required, and the position on ranking
    referrers — served rather than left to be asked.
    """
    f = []
    rules = " ".join(terms.get("rules") or []).lower()
    for needle in ("claw", "no second level", "funded from the same source", "revoking the builder code"):
        if needle not in rules:
            f.append("the published rules do not mention %r" % needle)
    if not str(terms.get("noReferrerLeaderboard") or "").strip():
        f.append("the position on a referrer leaderboard is not served")
    if not (terms.get("rejectedModels") or []):
        f.append("the rejected reward models are not published with their reasons")
    for key in ("schedule", "modelSentence", "paidFrom"):
        if not str(terms.get(key) or "").strip():
            f.append("the terms do not state %s" % key)
    if int(terms.get("payoutMinMicro") or 0) <= 0:
        f.append("the payout minimum is not stated")
    tax = payout.get("tax") or {}
    if not tax.get("form") or not tax.get("reportForm"):
        f.append("the payout page does not name the tax form it needs and files")
    if "UNVERIFIED" not in str(terms.get("taxNote") or ""):
        f.append("the tax note does not carry its own uncertainty marker")
    if int(payout.get("minimumMicro") or 0) != int(terms.get("payoutMinMicro") or 0):
        f.append("the payout page's minimum is not the engine's")
    return f


def c19_reward_needs_a_trade(p: Probe) -> tuple[str, bool, str]:
    """D5: the reward is a share of a fee we were PAID, on a referee who actually traded.

    The kit's rule is one sentence — never signup or deposit size — and this walks it end to end: a clean referral
    starts `pending` with zero earnings, an observed fee on a referee who has not qualified accrues NOTHING, the
    qualifying order unlocks a share of the fee the venue actually paid (not the fee we expected), and the accrual
    the run writes is re-derived from the row.
    """
    ref, sub = "u-ref-c19", "u-sub-c19"
    p.account(ref)
    p.account(sub)
    findings: list[str] = []
    code, me = p.as_user(ref, "/v1/referrals/me")
    if code != 200:
        return ("a referral earns a share of the fee we were paid, and nothing for a signup", False,
                "/v1/referrals/me answered %d" % code)
    token = (me.get("link") or {}).get("token") or ""
    code, applied = p.post_as(sub, "/v1/referrals/apply", {"code": token, "device": "c19-device",
                                                           "funding": "c19-funding"})
    if code != 200 or applied.get("attributionState") != "pending":
        return ("a referral earns a share of the fee we were paid, and nothing for a signup", False,
                "a clean apply answered %d / %s" % (code, applied.get("attributionState")))
    if (p.as_user(ref, "/v1/referrals/me")[1].get("funnel") or {}).get("funded"):
        findings.append("a pending referral is counted as funded")
    # An order whose fee we have not been paid yet, on a referee who has not qualified: nothing accrues.
    p.attr_row(sub, "0xc19-unpaid", observed=None, expected=9_000_000)
    p.attr_row(sub, "0xc19-paid", observed=8_000_000, expected=40_000_000)
    p.admin()
    code, ran = p.post_as(ref, "/v1/referrals/accrue", {"day": p.today()},
                          headers={"X-Admin-Token": os.environ["PGM_ADMIN_TOKEN"]})
    if code != 200:
        findings.append("the accrual run answered %d" % code)
    elif ran.get("accruals"):
        findings.append("an unqualified referee accrued %s rows" % ran.get("accruals"))
    # The qualifying order: the DB carries the state the ingest would have written.
    # The qualifying order lands AFTER the signup — 0016 CHECKs `qualify_ms >= signed_up_ms`, and an order that
    # predates the account that placed it is a fixture bug rather than a scenario (the probe reads `_now_ms()`
    # a millisecond later than the apply, so the order is stamped a second AHEAD of the signup).
    p.exec("UPDATE referral_attributions SET state='qualified', qualify_order=?, qualify_ms=?, notional_micro=?,"
           " term_ends_ms=? WHERE referee=?",
           ("0xc19-qualify", p.app()._now_ms() + 1_000, 40_000_000,
            p.app()._now_ms() + 364 * 86_400_000, sub))
    # A SECOND run for the same day, after the qualifying order landed — the day's first run was before it, and a
    # later run must still pay it: the (referrer, referee, day) key is what stops a re-run paying twice, not the
    # calendar.
    code, ran = p.post_as(ref, "/v1/referrals/accrue", {"day": p.today()},
                          headers={"X-Admin-Token": os.environ["PGM_ADMIN_TOKEN"]})
    rows = [{"referee": r[0], "feeObservedMicro": int(r[1]), "shareMicro": int(r[2])}
            for r in p.rows("SELECT referee, fee_observed_micro, share_micro FROM referral_accruals WHERE referrer=?",
                            (ref,))]
    qualifies = {sub: True}
    findings += referral_model_findings(rows, 2500, qualifies)
    if not rows:
        findings.append("the qualifying order accrued nothing")
    elif rows[0]["feeObservedMicro"] != 8_000_000:
        # The expected fee was 40 on that order. Reading it would pay out against a projection, which is the one
        # failure mode that turns a funded programme into an unfunded promise.
        findings.append("the accrual used the expected fee, not the observed one: %s" % rows[0])
    dash = p.as_user(ref, "/v1/referrals/me")[1]
    if int((dash.get("earnings") or {}).get("accruedMicro") or 0) <= 0:
        findings.append("the dashboard shows nothing accrued after a paid order")
    ok = not findings
    return ("a referral earns a share of the fee we were paid, and nothing for a signup", ok,
            "%d accrual row(s), %.2f of fee observed; %d findings%s"
            % (len(rows), (rows[0]["feeObservedMicro"] / 1e6) if rows else 0.0, len(findings),
               ("; " + "; ".join(findings[:3])) if findings else ""))


def c20_second_wallet_is_caught(p: Probe) -> tuple[str, bool, str]:
    """D5: the phase's acceptance sentence — a referral from a second wallet funded by the first, caught.

    Two of the three collisions here are the kit's own examples (one funding source, a shared device) and the third
    is the one that costs us the revenue line: an account applying its own code. That one must ALSO leave the
    revocation ground in `builder_code_status`, because "protected the revenue line" is not a sentence in a doc.
    """
    ref, sub, sub2, sub3 = "u-ref-c20", "u-sub-c20", "u-sub2-c20", "u-sub3-c20"
    for uid in (ref, sub, sub2, sub3):
        p.account(uid)
    findings: list[str] = []
    token = (p.as_user(ref, "/v1/referrals/me")[1].get("link") or {}).get("token") or ""
    p.post_as(sub, "/v1/referrals/apply", {"code": token, "funding": "c20-shared-funding"})
    code, second = p.post_as(sub2, "/v1/referrals/apply", {"code": token, "funding": "c20-shared-funding"})
    if code != 409 or (second.get("error") or {}).get("code") != "REFUSED":
        findings.append("the second wallet from one funding source answered %d/%s"
                        % (code, (second.get("error") or {}).get("code")))
    code, held = p.post_as(sub3, "/v1/referrals/apply", {"code": token, "device": "c20-device"})
    p.post_as(sub, "/v1/referrals/apply", {"code": token, "device": "c20-device"})
    dash = p.as_user(ref, "/v1/referrals/me")[1]
    if (dash.get("hidden") or {}).get("refused", 0) < 1:
        findings.append("the refusal is not counted on the dashboard")
    # Self-referral: refused, no row, and the builder code the referral was made under goes down.
    p.admin()
    code, self_ref = p.post_as(ref, "/v1/referrals/apply", {"code": p.as_user(ref, "/v1/referrals/me")[1]
                                                            .get("link", {}).get("token", "")})
    if code != 409 or (self_ref.get("error") or {}).get("code") != "SELF_REFERRAL":
        findings.append("a self-referral answered %d/%s" % (code, (self_ref.get("error") or {}).get("code")))
    p.exec("UPDATE builder_code_status SET state='active', source='api', note='' WHERE code='polygm-referral'")
    p.post_as(ref, "/v1/referrals/apply", {"code": p.as_user(ref, "/v1/referrals/me")[1].get("link", {})
                                          .get("token", "")})
    status = p.rows("SELECT state, note FROM builder_code_status WHERE code='polygm-referral'")
    if not status or str(status[0][0]) != "disabled":
        findings.append("the self-referral left the builder code %r" % (status[0][0] if status else None))
    elif "self-referral" not in str(status[0][1]).lower():
        findings.append("the revocation does not name its ground: %s" % status[0][1])
    queue = [{"referee": r[0], "state": r[1], "kind": r[2]}
             for r in p.rows("SELECT referee, state, kind FROM referral_reviews WHERE state='open'")]
    attributions = [{"referee": r[0], "referrer": r[1], "state": r[2]}
                    for r in p.rows("SELECT referee, referrer, state FROM referral_attributions")]
    signals: dict[str, set] = {}
    for uid, digest in p.rows("SELECT user_id, hash FROM referral_signals"):
        signals.setdefault(str(uid), set()).add(str(digest))
    findings += referral_collision_findings(attributions, queue, signals)
    if any(a["referee"] == ref for a in attributions):
        findings.append("the self-referral wrote an attribution row")
    ok = not findings
    return ("a second wallet funded by the first is refused, a shared device is held, and a self-referral is a "
            "revocation ground", ok,
            "%d attribution(s), %d open review item(s); %d findings%s"
            % (len(attributions), len(queue), len(findings), ("; " + "; ".join(findings[:3])) if findings else ""))

def c21_dashboard_arithmetic(p: Probe) -> tuple[str, bool, str]:
    """D5: the dashboard's own numbers, re-derived from its own rows.

    The kit's dashboard is clicks -> signups -> funded -> trading -> earned -> pending -> paid, and the two ways it
    can lie are a chain that is not monotone and a total that is not the sum of its parts. This walks a referrer
    with two live referees and one clawback candidate and checks every identity on the payload, including the one
    that is easy to get wrong: `clicks` is not a ceiling on signups.
    """
    ref, sub, sub2 = "u-ref-c21", "u-sub-c21", "u-sub2-c21"
    for uid in (ref, sub, sub2):
        p.account(uid)
    findings: list[str] = []
    token = (p.as_user(ref, "/v1/referrals/me")[1].get("link") or {}).get("token") or ""
    for uid, dev in ((sub, "c21-a"), (sub2, "c21-b")):
        code, got = p.post_as(uid, "/v1/referrals/apply", {"code": token, "device": dev, "funding": "fund-%s" % dev})
        if code != 200:
            findings.append("a clean apply answered %d" % code)
    for uid in (sub, sub2):
        p.exec("UPDATE referral_attributions SET state='qualified', qualify_order=?, qualify_ms=?, notional_micro=?,"
               " term_ends_ms=? WHERE referee=?", ("0xq-%s" % uid, p.app()._now_ms() + 1_000, 60_000_000,
                                                   p.app()._now_ms() + 364 * 86_400_000, uid))
        p.attr_row(uid, "0xc21-%s" % uid, observed=4_000_000, at=p.app()._now_ms() - 60_000)
    p.admin()
    code, ran = p.post_as(ref, "/v1/referrals/accrue", {"day": p.today()},
                          headers={"X-Admin-Token": os.environ["PGM_ADMIN_TOKEN"]})
    me = p.as_user(ref, "/v1/referrals/me")[1]
    findings += referral_dashboard_findings(me)
    funnel = me.get("funnel") or {}
    if funnel.get("signups") != 2:
        findings.append("signups is %s for two attributions" % funnel.get("signups"))
    if int(funnel.get("earned") or 0) != 2:
        findings.append("earned is %s for two referees with accruals" % funnel.get("earned"))
    if len(me.get("referrals") or []) != 2:
        findings.append("%d referral rows for two referees" % len(me.get("referrals") or []))
    open_items = p.rows("SELECT COUNT(*) FROM referral_reviews WHERE subject=? AND state='open'", (ref,))[0][0]
    if int((me.get("review") or {}).get("open") or 0) != int(open_items):
        findings.append("the review count is not the queue: %s vs %s" % (me.get("review") or {}).get("open"),
                        open_items)
    # The leading count is not a ceiling: a code read out loud produces signups with no clicks at all, so the
    # scanner must not demand clicks >= signups — it demands the four money steps monotone and clicks non-negative.
    paid_csv = (me.get("earnings") or {})
    if int(paid_csv.get("accruedMicro") or 0) != sum(int(r.get("earnedMicro") or 0)
                                                     for r in me.get("referrals") or []):
        findings.append("accrued is not the sum of the referees' rows")
    ok = not findings
    return ("the dashboard's funnel is monotone, its totals are its rows, and its review count is the queue", ok,
            "signups %s, funded %s, trading %s, earned %s, accrued %s micro; %d findings%s"
            % (funnel.get("signups"), funnel.get("funded"), funnel.get("trading"), funnel.get("earned"),
               (me.get("earnings") or {}).get("accruedMicro"), len(findings),
               ("; " + "; ".join(findings[:3])) if findings else ""))


def c22_payout_reality_and_clawback(p: Probe) -> tuple[str, bool, str]:
    """D5: what a referrer can check before they post a link, and what a clawback actually does.

    Four things are asserted rather than described: the terms page carries the model, the rejected alternatives,
    the payout schedule, the minimum, the tax form and the position on referrer leaderboards; money inside the
    settlement hold is not payable; a future day is refused by the accrual run; and a clawback cancels the UNPAID
    accruals first while leaving the rows where they are — the table is append-only, so the evidence of what was
    paid and what was taken back has to survive the decision that took it.
    """
    ref, sub = "u-ref-c22", "u-sub-c22"
    for uid in (ref, sub):
        p.account(uid)
    findings: list[str] = []
    code, payload = p.get("/v1/referrals/terms")
    if code != 200:
        return ("the payout reality is published, and a clawback cancels what was never paid", False,
                "/v1/referrals/terms answered %d" % code)
    terms = payload.get("terms") or {}
    token = (p.as_user(ref, "/v1/referrals/me")[1].get("link") or {}).get("token") or ""
    p.post_as(sub, "/v1/referrals/apply", {"code": token, "device": "c22-device", "funding": "c22-funding"})
    p.exec("UPDATE referral_attributions SET state='qualified', qualify_order=?, qualify_ms=?, notional_micro=?,"
           " term_ends_ms=? WHERE referee=?", ("0xc22", p.app()._now_ms() + 1_000, 40_000_000,
                                               p.app()._now_ms() + 364 * 86_400_000, sub))
    p.attr_row(sub, "0xc22-order", observed=40_000_000, at=p.app()._now_ms() - 60_000)
    p.admin()
    admin = {"X-Admin-Token": os.environ["PGM_ADMIN_TOKEN"]}
    p.post_as(ref, "/v1/referrals/accrue", {"day": p.today()}, headers=admin)
    me = p.as_user(ref, "/v1/referrals/me")[1]
    findings += referral_terms_findings(terms, me.get("payout") or {})
    earnings = me.get("earnings") or {}
    if int(earnings.get("holdingMicro") or 0) <= 0:
        findings.append("a fresh accrual is not inside the settlement hold: %s" % earnings)
    if int(earnings.get("payableMicro") or 0) != 0:
        findings.append("money inside the hold is payable: %s" % earnings.get("payableMicro"))
    if int(earnings.get("toMinimumMicro") or 0) != int(terms.get("payoutMinMicro") or 0):
        findings.append("the gap to the minimum ignores the hold")
    code, future = p.post_as(ref, "/v1/referrals/accrue", {"day": "2099-01-01"}, headers=admin)
    if code != 422:
        findings.append("a future day answered %d" % code)
    # A clawback: unpaid accruals first, nothing asked back when nothing was paid, and the rows survive.
    p.exec("INSERT INTO referral_reviews (kind, subject, referee, state, findings_json, decision, actor, opened_ms,"
           " decided_ms) VALUES ('duplicate_funding',?,?,'open','[]','','',?,0)", (ref, sub, p.app()._now_ms()))
    item = p.rows("SELECT id FROM referral_reviews WHERE subject=? AND state='open' ORDER BY id DESC LIMIT 1", (ref,))
    before = p.rows("SELECT COUNT(*), COALESCE(SUM(share_micro),0) FROM referral_accruals WHERE referrer=?", (ref,))
    code, decided = p.post_as(ref, "/v1/referrals/review",
                              {"id": int(item[0][0]), "decision": "claw_back", "reason": "gate: one funding source"},
                              headers=admin)
    if code != 200:
        findings.append("the clawback answered %d" % code)
    else:
        claw = decided.get("clawback") or {}
        if int(claw.get("unpaidMicro") or 0) != int(before[0][1]):
            findings.append("the clawback took %s of %s unpaid" % (claw.get("unpaidMicro"), before[0][1]))
        if claw.get("requiresRepayment"):
            findings.append("it asked for money back when nothing had been paid")
    after = p.rows("SELECT COUNT(*), COALESCE(SUM(share_micro),0) FROM referral_accruals WHERE referrer=?", (ref,))
    if after != before:
        findings.append("the clawback DELETED accrual rows: %s -> %s" % (before, after))
    dash = p.as_user(ref, "/v1/referrals/me")[1]
    if int((dash.get("earnings") or {}).get("clawedBackMicro") or 0) != int(before[0][1]):
        findings.append("the dashboard does not show what was clawed back")
    if int((dash.get("funnel") or {}).get("earned") or 0) != 0:
        findings.append("a clawed-back referee still counts as earning")
    # The position on a referrer leaderboard is served, and there is no such board to read.
    src = read(ROOT / "services" / "api" / "app.py") or ""
    if "/v1/referrals/leaderboard" in src:
        findings.append("a public referrer leaderboard exists")
    ok = not findings
    return ("the payout reality is published, and a clawback cancels what was never paid", ok,
            "minimum %s micro, hold %s days, %d rule(s); %d findings%s"
            % (terms.get("payoutMinMicro"), terms.get("settleHoldDays"), len(terms.get("rules") or []),
               len(findings), ("; " + "; ".join(findings[:3])) if findings else ""))

def c23_public_pages_exist_and_are_server_rendered(p: Probe) -> tuple[str, bool, str]:
    """D6: the three pages exist, are wired to a view, are rendered on the server, and carry a card.

    The kit's D6 is one sentence — every trader who shares their page is doing our marketing — and it fails in
    three quiet ways that a screenshot would not show: a page that exists but imports nothing (an unreachable
    file), a page that is a client component (an unfurler runs no JavaScript, so the card unfurls empty), and a
    page with no `opengraph-image` (the link is a bare URL in every timeline it is posted in).

    The last assertion is the one with a history: a public page must NOT import the app shell's providers, because
    that is 100+ KB of client code on the critical path of a page whose only reader is a stranger.
    """
    findings: list[str] = []
    for rel in PUBLIC_PAGES + PUBLIC_VIEWS:
        if not (WEB / rel).exists():
            findings.append("%s does not exist" % rel)
    wiring = {
        "app/trader/[who]/page.tsx": "TraderView",
        "app/market/[market]/page.tsx": "MarketView",   # the module is @/public/MarketView; the export is MarketPublicView
        "app/leaderboard/[board]/page.tsx": "BoardView",
        "app/leaderboard/[board]/w/[window]/page.tsx": "BoardView",
        "app/leaderboard/[board]/c/[category]/page.tsx": "BoardView",
    }
    for page, view in wiring.items():
        body = read(WEB / page)
        module = view.split("#")[0].split(" ")[0]
        if body and ("@/public/%s" % module) not in body:
            findings.append("%s does not import @/public/%s — the route and the surface are not wired" % (page, view))
        if body and "serverRead" not in body and "loadBoard" not in body:
            findings.append("%s reads nothing on the server" % page)
    for rel in ("src/public/TraderView.tsx", "src/public/MarketView.tsx", "src/public/BoardView.tsx",
                "src/public/ShareCard.tsx", "src/public/JsonLd.tsx"):
        body = read(WEB / rel)
        if '"use client"' in body:
            findings.append("%s is a client component: an unfurler runs no JavaScript, so the page unfurls empty" % rel)
    # The graph's breadcrumb has to be the trail the page RENDERS: the API names it in its own words (it has no
    # idea what copy the page will use), so each view states its trail once and hands that one value to both the
    # chrome and the JSON-LD. A view that passes only the graph is a page whose markup says "Leaderboard" while
    # its reader sees "the boards" — which is what c23 found before this was fixed.
    for rel in ("src/public/TraderView.tsx", "src/public/MarketView.tsx", "src/public/BoardView.tsx"):
        body = read(WEB / rel)
        if "trail: Crumb[]" not in body or "trail={trail}" not in body or "crumbs={trail}" not in body:
            findings.append("%s renders a trail and states a different one to the graph" % rel)
    for rel in PUBLIC_PAGES:
        if rel.endswith("opengraph-image.tsx") and "revalidate" not in read(WEB / rel):
            findings.append("%s does not set `revalidate`: a card that regenerates every request is a card whose "
                            "numbers move under a reader comparing it with the page" % rel)
    # the price surface on the market page goes through the number layer with a freshness, like every other one
    market = read(WEB / "src/public/MarketView.tsx")
    if 'kind="price"' not in market or "freshness=" not in market:
        findings.append("the public odds are rendered without the number layer's freshness")

    # ...and then the pages are actually served, because a file check passes on a route nothing can reach.
    code, listed = p.list_handle("u-gate-d6", "gate_public")
    if code != 200:
        findings.append("the handle could not be listed (%d): %s" % (code, listed.get("error")))
    served: list[str] = []
    for path in ("/v1/public/trader/gate_public", "/v1/public/market/fed-cut-sept",
                 "/v1/public/leaderboard/risk_adjusted", "/v1/public/sitemap"):
        code, body = p.get(path)
        if code != 200:
            findings.append("%s answered %d" % (path, code))
            continue
        served.append(path)
        if not body.get("robots"):
            findings.append("%s serves no robots answer, so nothing decides its indexability" % path)
        if not body.get("cacheKey"):
            findings.append("%s serves no cache key" % path)
    trader = p.get("/v1/public/trader/gate_public")[1]
    if trader.get("url") and not str(trader["url"]).startswith("https://"):
        findings.append("the trader page's canonical URL is not absolute: %s" % trader.get("url"))
    if trader.get("cardKey") and not trader.get("card"):
        findings.append("the trader page serves a cardKey with no card")
    if trader.get("structuredData"):
        findings += card_findings(trader)   # the graph and the card against the page they were served with
    return ("%d page/view files present, %d wired, %d public routes served, no client component, every card cached"
            % (len(PUBLIC_PAGES) + len(PUBLIC_VIEWS), len(wiring), len(served)), not findings,
            "; ".join(findings[:6]))


def c24_the_budget_refuses_and_then_blocks(p: Probe) -> tuple[str, bool, str]:
    """D6: a sitemap walk is refused past the budget, and coming back past the refusal is a block.

    The public pages are the only surfaces in the product with no account behind them, so their abuse protection
    is the whole protection. The check walks the real limiter: it clears its own rows, walks the sitemap until the
    API says 429, keeps going, and then asserts that (a) a block exists, (b) it is a DIGEST and not an address,
    (c) it applies to every public page and not only the one that was scraped, and (d) a fresh caller is still
    served — a budget that denies the world is an outage with a nicer name.
    """
    findings: list[str] = []
    p.exec("DELETE FROM public_page_hits")
    p.exec("DELETE FROM public_page_blocks")
    limit = int(p.app()._pp.budget.BUDGET["sitemap"][0])
    codes = [p.get("/v1/public/sitemap")[0] for _ in range(limit + 12)]
    if 200 not in codes:
        findings.append("no sitemap walk was served at all: %s" % codes[:5])
    if 429 not in codes:
        findings.append("%d walks never hit the budget" % len(codes))
    else:
        first = codes.index(429)
        if any(c == 200 for c in codes[first:]):
            findings.append("a walk was served after the budget was spent (position %d)" % first)
    blocks = p.rows("SELECT subject_hash, scope, reason, kind, until_ms FROM public_page_blocks")
    if not blocks:
        findings.append("a caller who kept going past the lock was never blocked")
    else:
        subject, scope, reason, kind, _until = blocks[0]
        if not str(subject).startswith("i_") or "127.0.0.1" in str(subject):
            findings.append("the block stores something that is not a digest: %r" % str(subject)[:40])
        if scope != "all":
            findings.append("the auto-block is scoped to %r, so it protects one page" % scope)
        if kind != "auto" or len(str(reason)) < 8:
            findings.append("the auto-block has no reason a support agent could read: %r" % reason)
        if p.get("/v1/public/market/fed-cut-sept")[0] != 429:
            findings.append("the block does not reach the other public pages")
    # and a reader who was never over the limit is unaffected by somebody else's block
    p.exec("DELETE FROM public_page_blocks")
    p.exec("DELETE FROM public_page_hits")
    if p.get("/v1/public/market/fed-cut-sept")[0] != 200:
        findings.append("clearing the block did not restore service")
    return ("a %d-walk budget refuses with 429 and blocks the caller, by digest, across every public page" % limit,
            not findings, "; ".join(findings[:5]))


def c25_no_address_and_the_card_carries_its_qualifiers(p: Probe) -> tuple[str, bool, str]:
    """D6: every public payload is greppable for an address, and a shared card keeps its qualifiers.

    Two rules with one shape: what a stranger receives. The scanner walks every public read the probe can reach —
    including a trader page, which is the only one that names a person — and then checks the card itself: a ranked
    trader card carries the provisional label and the drawdown, a market card carries the price's age, and the
    payload's own `notes` are in the card's footnote rather than only on the page. The card is the artifact that
    travels without the page around it, so this is the check that decides whether the share is honest.
    """
    findings: list[str] = []
    app = p.app()
    rows = p.board(board="risk_adjusted").get("rows") or []
    if not rows:
        return ("no public payload carries an address, and every card keeps its qualifiers", True, "no board")
    handle = "gate_public"
    code, listed = p.list_handle("u-gate-d6", handle)
    if code != 200:
        findings.append("the handle could not be listed (%d): %s" % (code, listed.get("error")))
    payloads: list[tuple[str, dict]] = [
        ("trader", p.get("/v1/public/trader/" + handle)[1]),
        ("market", p.get("/v1/public/market/fed-cut-sept")[1]),
        ("board", p.get("/v1/public/leaderboard/risk_adjusted")[1]),
        ("sitemap", p.get("/v1/public/sitemap")[1]),
    ]
    findings += public_payload_findings(payloads)
    card = (payloads[0][1].get("card") or {})
    foot = [str(f).lower() for f in (card.get("footnote") or [])]
    joined = " | ".join(foot)
    if card.get("provisional") and "provisional" not in joined:
        findings.append("a provisional trader card lost its provisional line: %s" % foot)
    if card.get("ranked") and "drawdown" not in joined:
        findings.append("a ranked trader card lost its drawdown")
    if not (payloads[0][1].get("notes") or []):
        findings.append("the trader page carries no notes, so nothing on it qualifies the row")
    mcard = (payloads[1][1].get("card") or {})
    mfoot = " | ".join(str(f).lower() for f in (mcard.get("footnote") or []))
    if not re.search(r"ago|as of|second|minute|hour", mfoot):
        findings.append("the market card does not say how old its odds are: %s" % mfoot)
    if not (payloads[2][1].get("formula") and payloads[2][1].get("gate")):
        findings.append("the board page does not carry the formula and the gate it ranked by")
    return ("4 public payloads clean of addresses, %s" % ("cards qualified" if not findings else "cards checked"),
            not findings, "; ".join(findings[:6]))


def c26_the_pages_are_published_once(p: Probe) -> tuple[str, bool, str]:
    """D6: the contract, the ledger, the API's own index and the pages all agree about one URL grammar.

    A public page that exists at a URL different from the one the API publishes as canonical is a page with two
    addresses and half the signal. This check reads the four places the grammar is written — `contracts/
    openapi.yaml`, the web route ledger, `public_pages/urls.py`, and the live sitemap — and fails if any of them
    disagrees. It also asserts the three rules that make the grammar a decision rather than a convention: the
    sitemap lists all three kinds, a `noindex` page is absent from the trader list only when it is unlisted, and
    the OG URL is the page URL plus one suffix (an unfurler's cache key is the URL, so a second convention is a
    second card).
    """
    findings: list[str] = []
    contract = read(ROOT / "contracts" / "openapi.yaml")
    ledger = read(WEB / "src" / "api" / "routes.ts")
    urls = read(ROOT / "packages" / "polygm_core" / "public_pages" / "urls.py")
    for path in ("/v1/public/trader/{handle}", "/v1/public/market/{slug}", "/v1/public/leaderboard/{board}",
                 "/v1/public/sitemap", "/v1/public/blocks"):
        if path not in contract:
            findings.append("%s is not in the contract" % path)
        if path not in ledger:
            findings.append("%s is not in the web route ledger" % path)
    for kind in ("trader", "market", "leaderboard"):
        if ('"%s"' % kind) not in urls:
            findings.append("urls.py has no %s kind" % kind)
    code, listed_body = p.list_handle("u-gate-d26", "gate_grammar")
    if code != 200:
        findings.append("the handle could not be listed (%d): %s" % (code, listed_body.get("error")))
    sitemap = p.get("/v1/public/sitemap")[1]
    listed = [u.get("url", "") for u in (sitemap.get("urls") or [])]
    for kind in ("trader", "market", "leaderboard"):
        if not any(("/%s/" % kind) in u for u in listed):
            findings.append("the sitemap lists no %s URL" % kind)
    canonical = p.app()._pp.urls.page_url("trader", "gate_grammar", base=p.app()._BASE_URL())
    if canonical not in listed:
        findings.append("the sitemap's trader URL is not the canonical one: %s not in %s"
                        % (canonical, [u for u in listed if "/trader/" in u][:2]))
    if sitemap.get("count") != len(listed) or "caps" not in sitemap:
        findings.append("the sitemap's count and its URLs disagree, or it serves no caps")
    if sitemap.get("robots") and not str(sitemap["robots"]).startswith("index"):
        findings.append("the sitemap itself asks not to be indexed: %r" % sitemap.get("robots"))
    # the OG URL is derived, never re-invented
    og = p.app()._pp.urls.og_url("trader", "gate_public")
    if not og.endswith("/trader/gate_public/opengraph-image"):
        findings.append("the OG URL is not the page URL plus a suffix: %s" % og)
    return ("5 public routes in the contract and the ledger, %d sitemap URLs, one URL grammar" % len(listed),
            not findings, "; ".join(findings[:6]))


def _p08_module():
    """`tools/p08-gate-check.py`, imported for its `Servers` context — the real uvicorn + `next start` pair.

    D6's claim is a page a stranger opens, and nothing short of two real servers answers it: an in-process
    TestClient renders no HTML, and `next build` type-checks a page without ever fetching a payload. Reusing P08's
    bootstrapper rather than writing a second one is the point — the plane that serves the terminal is the plane
    that must serve these pages, and a second boot would be a second set of env defaults to keep in step.
    """
    if "_p08" not in globals():
        import importlib.util
        spec = importlib.util.spec_from_file_location("p08gate_for_p11", ROOT / "tools" / "p08-gate-check.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["p08gate_for_p11"] = mod
        spec.loader.exec_module(mod)
        globals()["_p08"] = mod
    return globals()["_p08"]


def _call(port: int, method: str, path: str, *, body: dict | None = None, headers: dict | None = None):
    """One HTTP call to a local port, returning (status, body, content-type). No cookie jar: the public pages are
    the surfaces that must work with no cookies at all, so a jar would be testing a session nobody has."""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    payload = None if body is None else json.dumps(body)
    hdrs = {"accept": "text/html,application/json;q=0.9", **(headers or {})}
    if payload is not None:
        hdrs["content-type"] = "application/json"
    try:
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", "replace")
        return resp.status, text, resp.getheader("content-type") or ""
    finally:
        conn.close()


def public_page_findings(app_mod, web_port: int, api_port: int) -> list:
    """What a stranger's browser and an unfurler get, checked on the rendered HTML rather than on the payload.

    Five rules, each one a way a page can be *served* and still fail the kit's sentence ("every trader who shares
    their page is doing our marketing"):

    * **200 with a real document**, for a request with no cookies, no `X-User-Id` and no session of any kind.
    * **The numbers are on the page**, not only in a payload: the board's rows and the market's question appear in
      the HTML, because a page that renders client-side is a page a crawler sees empty.
    * **The canonical URL and the JSON-LD are in the head**, and the JSON-LD parses — an unfurler that cannot read
      it shows the URL as its title.
    * **No address anywhere in the HTML**, which is the one leak the whole pseudonym design is for (the payload
      scanners already refuse it; this is the rendered document, where a stray `console.log`-shaped prop would land).
    * **The OG route answers with an image**, because a card that 500s unfurls as a broken image on every timeline
      it is posted in — and it is the artifact that outlives the link.
    """
    out: list[str] = []
    # The row to publish is read from the SERVED board, not from a call into the app module: an in-process read
    # would answer even if the route were broken, and the page under test is rendered from the route.
    status, body, _ = _call(api_port, "GET", "/v1/leaderboard?board=risk_adjusted&limit=5",
                            headers={"accept": "application/json"})
    try:
        rows = (json.loads(body).get("rows") or []) if status == 200 else []
    except ValueError:
        rows = []
    if not rows:
        return ["the seeded population has no rows to publish a handle on (%d)" % status]
    wallet = app_mod._wallet_for_anon(rows[0]["anon"]) or ""
    user = "u-gate-d28"
    app_mod._db.execute("INSERT OR IGNORE INTO users (id, created_ms, tier) VALUES (?,?,'free')", (user, 1))
    app_mod._db.execute("DELETE FROM user_identities WHERE kind='wallet' AND user_id=?", (user,))
    app_mod._db.execute("INSERT INTO user_identities (kind, value, user_id, state, claimed_ms, verified_ms,"
                        " proof_kind, revoked_ms) VALUES ('wallet',?,?,'verified',?,?,'gate',NULL)",
                        (wallet, user, 1, 1))
    status, text, _ = _call(api_port, "POST", "/v1/leaderboard/identity",
                            body={"state": "listed", "handle": "gate_served"},
                            headers={"X-User-Id": user, "Idempotency-Key": "g11-d28-listed"})
    if status != 200:
        return ["the handle could not be listed through the served API (%d): %s" % (status, text[:160])]
    pages = ("/leaderboard/risk_adjusted", "/market/fed-cut-sept", "/trader/gate_served")
    for path in pages:
        status, html, ctype = _call(web_port, "GET", path)
        if status != 200 or "text/html" not in ctype:
            out.append("%s answered %s %s" % (path, status, ctype))
            continue
        if not html.lstrip().lower().startswith("<!doctype html"):
            out.append("%s is not a document" % path)
        if 'rel="canonical"' not in html:
            out.append("%s carries no canonical link" % path)
        if "application/ld+json" not in html:
            out.append("%s carries no structured data" % path)
        if path.startswith("/leaderboard") and "w_2cfb79dff4" not in html and "#1" not in html:
            out.append("%s does not contain a single ranked row: a board page that renders no rows is a page a "
                       "crawler indexes as empty" % path)
        for m in ADDRESS_RX.finditer(html):
            out.append("%s serves an address: %s" % (path, m.group(0)[:12]))
    for path in ("/trader/gate_served/opengraph-image", "/leaderboard/risk_adjusted/opengraph-image",
                 "/market/fed-cut-sept/opengraph-image"):
        status, body, ctype = _call(web_port, "GET", path)
        if status != 200 or "image" not in ctype:
            out.append("the card route %s answered %s %s" % (path, status, ctype))
    return out


def c28_the_pages_render_for_a_stranger(p: Probe) -> tuple[str, bool, str]:
    """D6: the three pages and their cards, fetched from the running pair by a client with no session at all."""
    p08 = _p08_module()
    import importlib
    try:
        with p08.Servers() as srv:
            sys.path.insert(0, str(ROOT / "services/api"))
            import seed_leaderboard
            seed_leaderboard.seed_sqlite()
            app_mod = importlib.import_module("app")
            findings = public_page_findings(app_mod, srv.web_port, srv.api_port)
    except Exception as exc:
        return ("3 public pages and their cards, fetched by a client with no session", False,
                "the plane did not boot: %s: %s" % (type(exc).__name__, str(exc)[:180]))
    return ("3 pages rendered and 3 cards served by the running pair for a session-less client",
            not findings, "; ".join(findings[:6]))


def declared_append_only() -> list[str]:
    """The append-only set, read from the list the SQLite triggers are generated from.

    `tools/build-sqlite-migrations.py`'s `APPEND_ONLY` is the one place the set is *declared*; the Postgres triggers
    are scattered across migrations (0005 for the tables that existed then, each later migration for its own) and
    the grants are in 0005, 0016 and 0018. Scanning `CREATE TRIGGER` statements instead would be worse than reading
    the declaration: `referral_events` still has a trigger in 0012's text although 0016 dropped the table, and a
    scan cannot tell that from a live one — which is the same reason the declaration exists. Parsed with `ast`
    rather than imported, so a gate run never executes a migration tool to answer a question about a list.
    """
    import ast
    src = (ROOT / "tools" / "build-sqlite-migrations.py").read_text()
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "APPEND_ONLY":
            return list(ast.literal_eval(node.value))
    raise RuntimeError("APPEND_ONLY is not declared in tools/build-sqlite-migrations.py")


def append_only_findings(migrations: dict, declared: list[str]) -> list:
    """Declared append-only tables missing either half of the promise.

    Both halves matter and they fail differently: without the trigger our own code can rewrite evidence, and without
    the REVOKE the application role can. The D6 sweep found **seventeen** tables with a trigger and no grant — every
    append-only table born after 0005, whose `DO $$` block could not name a table that did not exist yet — and the
    mirror image: `leaderboard_exclusions`, declared append-only in the portable list since D1, with no Postgres
    trigger at all. `db/migrations/0018_append_only_grants.sql` closes both, and this is the check that would have
    caught either one.
    """
    trig, revoked = set(), set()
    for text in migrations.values():
        for m in re.finditer(r"CREATE TRIGGER\s+append_only_\w+\s+[^;]*?ON\s+(\w+)", text, re.I | re.S):
            trig.add(m.group(1))
        for m in re.finditer(r"REVOKE[^;]*?UPDATE[^;]*?ON\s+(\w+)", text, re.I | re.S):
            revoked.add(m.group(1))
        for m in re.finditer(r"FOREACH t IN ARRAY ARRAY\[(.*?)\]", text, re.S):
            # `[a-z0-9_]+`, not `[a-z_]+`: `flag_audit_p06` has a digit, and a scanner that silently drops it
            # reports a table as unguarded while the migration three files away grants for it.
            revoked |= set(re.findall(r"'([a-z0-9_]+)'", m.group(1)))
    out = []
    for t in declared:
        if t not in trig:
            out.append("%s is declared append-only and no migration creates its trigger" % t)
        if t not in revoked:
            out.append("%s is append-only and no REVOKE names it, so the app role can rewrite it" % t)
    return out


def c27_append_only_has_both_halves(p: Probe) -> tuple[str, bool, str]:
    """The trigger AND the grant, for every declared append-only table — the invariant D6 found broken twice."""
    migrations = {f.name: f.read_text() for f in sorted((ROOT / "db" / "migrations").glob("*.sql"))}
    declared = declared_append_only()
    findings = append_only_findings(migrations, declared)
    return ("%d declared append-only table(s) have a trigger and a REVOKE" % len(declared),
            not findings, "; ".join(findings[:6]))


# --------------------------------------------------------------------------------- D7 · the anti-gaming dashboard
GAMING_KINDS = ("fast_climb", "correlated_cluster", "synthetic_chain", "builder_anomaly")
GAMING_ACTIONS = ("exclude", "flag", "include", "clear")


def gaming_rule_findings(rules: dict) -> list:
    """Every finding kind served with BOTH of its readings, at a length that can explain something.

    The product rule is "every classification label carries a visible rule and a disclaimer", and this is the one
    screen where violating it costs somebody their standing: a finding that arrives with only the damning reading
    is a finding that gets acted on before it is read. So the two texts are *data the API serves as a pair* — a
    consumer cannot render one without having the other in hand — and this is the check that says so.
    """
    out = []
    for kind in GAMING_KINDS:
        entry = (rules or {}).get(kind)
        if not isinstance(entry, dict):
            out.append("%s has no rule entry at all" % kind)
            continue
        if set(entry) != {"rule", "innocent"}:
            out.append("%s serves %s, expected exactly rule+innocent" % (kind, sorted(entry)))
            continue
        if len(str(entry["rule"])) < 200:
            out.append("%s's rule is too short to state what was measured" % kind)
        if len(str(entry["innocent"])) < 100:
            out.append("%s has no workable reading of the innocent case" % kind)
        if str(entry["rule"]) == str(entry["innocent"]):
            out.append("%s's two readings are the same text" % kind)
    extra = sorted(set(rules or {}) - set(GAMING_KINDS))
    if extra:
        out.append("the dashboard serves rules for unknown kinds: %s" % extra)
    return out


def decision_journal_findings(rows: list) -> list:
    """The invariants of the append-only decision journal, checked on rows a live run just produced.

    Three ways an operator's click stops being a record:

    * **a repeated idempotency key** — a retried click that wrote twice reads, to every consumer that replays the
      NEWEST row, as a later decision than the first one;
    * **a reason too short to answer an appeal** (or no actor at all) — "suspicious" is not a record, and an
      anonymous removal is worse than one that names its author;
    * **an action outside the vocabulary** — the boards only act on `exclude` and `include` (with `flag` as a
      question), so a typo'd action is a click that silently does nothing.
    """
    out = []
    seen = {}
    for row in rows or []:
        key = str(row.get("idempotencyKey") or "")
        if key:
            seen[key] = seen.get(key, 0) + 1
        if str(row.get("action") or "") not in GAMING_ACTIONS:
            out.append("a decision with action %r is outside the vocabulary" % row.get("action"))
        if len(str(row.get("reason") or "").strip()) < 8:
            out.append("a decision was recorded without a usable reason")
        if not str(row.get("actor") or "").strip():
            out.append("a decision was recorded with no actor")
        if str(row.get("wallet") or "") and not str(row.get("anon") or ""):
            out.append("a decision row names a wallet and no pseudonym to quote afterwards")
    for key in sorted(seen):
        if seen[key] > 1:
            out.append("idempotency key %s was written %d times" % (key, seen[key]))
    return out


def gaming_detector_findings() -> list:
    """The four detectors, fired on a planted farm apiece and left quiet on an honest population.

    A detector that only fires on the specimen it was written for is a stopped clock; a detector that fires on an
    honest market maker is one whose output an operator learns to skip. Both directions are checked here, on the
    real package, because the API tests prove the wiring and this proves the arithmetic.
    """
    sys.path.insert(0, str(ROOT / "packages"))
    from polygm_core.gaming import detect as d, RULES, INNOCENT
    out = gaming_rule_findings({k: {"rule": RULES[k], "innocent": INNOCENT[k]} for k in RULES})
    day = d.DAY_MS
    now = 1_700_000_000_000

    def snap(wallet, rank, settled, ms):
        return {"wallet": wallet, "board": "risk_adjusted", "windowKey": "30d", "rank": rank, "settled": settled,
                "scoreBps": 100, "drawdownMicro": 0, "computedMs": ms}

    history = []
    for i in range(12):
        history += [snap("w_ord%02d" % i, 300 + i, 60, now - 7 * day + 60_000),
                    snap("w_ord%02d" % i, 298 + i, 61, now)]
    history += [snap("w_thin", 900, 9, now - 7 * day + 60_000), snap("w_thin", 30, 9, now)]
    climbs = d.climb_findings(history=history, at_ms=now)
    if [f["wallet"] for f in climbs] != ["w_thin"]:
        out.append("the climb rule did not fire on a thin record climbing past its population")

    def fill(wallet, ts, token="tok1", side="buy"):
        return {"wallet": wallet, "tsMs": ts, "tokenId": token, "side": side, "conditionId": "c",
                "priceMicro": 1, "sizeMicro": 1, "notionalMicro": 1, "winner": None, "category": "", "marketId": "m"}

    mirrored = ([fill("w_a", now + i * 60_000) for i in range(10)]
                + [fill("w_b", now + i * 60_000 + 5_000) for i in range(10)])
    clusters = d.cluster_findings(fills=mirrored, at_ms=now + 700_000)
    if not clusters or clusters[0]["wallets"] != ["w_a", "w_b"]:
        out.append("the cluster rule missed two mirrored wallets")
    for c in clusters:
        if int(c.get("worstOverlapBps") or 0) > 10_000:
            out.append("an overlap of %d bps is not a ratio" % c["worstOverlapBps"])
    thin = ([fill("w_x", now + i * 60_000) for i in range(3)]
            + [fill("w_y", now + i * 60_000 + 1_000) for i in range(3)])
    if d.cluster_findings(fills=thin, at_ms=now + 300_000):
        out.append("the cluster rule fired on a tape too thin to be evidence")

    refs = [{"referrer": "u-ref", "referee": "u-r%d" % i, "signedUpMs": 1_000, "qualifyMs": 2_000 + i,
             "notionalMicro": 25_000_000, "fundingDigest": ("f_same" if i < 2 else "f_other"), "deviceDigest": "",
             "accrualMicro": 0} for i in range(1, 4)]
    chains = d.chain_findings(referrals=refs, at_ms=10_000)
    if not chains or chains[0]["sharedFunding"] != 1:
        out.append("the chain rule missed a second wallet funded by the first")
    if "f_same" in repr(chains):
        out.append("a funding digest was printed into a finding")

    attrs = [{"wallet": "w_script", "userId": "u1", "orderId": "o%d" % i, "marketId": "m%d" % i,
              "notionalMicro": 10_000_000, "feeMicroExpected": 1, "feeMicroObserved": 0,
              "placedMs": now + i * 30_000} for i in range(6)]
    if not d.builder_findings(attributions=attrs, at_ms=now + 300_000):
        out.append("the builder rule missed a burst of attributed volume")
    honest = [{"wallet": "w_real", "userId": "u2", "orderId": "o", "marketId": "m",
               "notionalMicro": 10_000_000, "feeMicroExpected": 1, "feeMicroObserved": 1,
               "placedMs": now + i * 86_400_000} for i in range(6)]
    if d.builder_findings(attributions=honest, at_ms=now + 10 * 86_400_000):
        out.append("the builder rule fired on an ordinary trader using our code")

    clean = d.dashboard(history=[snap("w_a", 10, 90, now - day), snap("w_a", 10, 91, now)], fills=[],
                        referrals=[], attributions=[], at_ms=now)
    if any(clean[k] for k in ("climbers", "clusters", "chains", "builder")):
        out.append("a clean tape produced findings: %s" % clean["counts"])
    return out


def gaming_api_findings(app_mod, client, token: str) -> list:
    """What the served dashboard says, checked on the payload a real request returns."""
    out = []
    r = client.get("/v1/admin/gaming", headers={"X-Admin-Token": token})
    if r.status_code != 200:
        return ["the dashboard answered %d for an operator token" % r.status_code]
    try:
        body = r.json()
    except ValueError:
        return ["the dashboard answered 200 with a body that is not JSON"]
    out += gaming_rule_findings(body.get("rules") or {})
    for flag in ("asOf", "staleAfter", "cache"):
        if flag not in body:
            out.append("the payload is missing %s, so the client refuses it as an unstamped read" % flag)
    findings = [f for key in ("climbers", "clusters", "chains", "builder") for f in body.get(key) or []]
    for f in findings:
        if not f.get("evidence"):
            out.append("a %s finding arrived with no evidence" % f.get("kind"))
        if str(f.get("suggested") or "") not in GAMING_ACTIONS:
            out.append("a finding suggests %r" % f.get("suggested"))
        pair = (body.get("rules") or {}).get(str(f.get("kind")))
        if not pair or f.get("rule") != pair.get("rule"):
            out.append("a finding's rule is not the one the dashboard serves for its kind")
        if not any(f.get(k) for k in ("wallet", "wallets", "referrer")):
            out.append("a finding names no subject at all")
        anon = f.get("anon") or f.get("anonReferrer") or (f.get("anonWallets") or [""])[0]
        if not str(anon).startswith("w_"):
            out.append("a finding carries no pseudonym to quote afterwards")
    blob = json.dumps(body)
    allowed = set()
    for f in findings:
        allowed |= {str(w) for w in (f.get("wallets") or [])}
        allowed |= {str(f.get("wallet") or ""), str(f.get("referrer") or "")}
    for m in re.finditer(r"0x[0-9a-fA-F]{6,}", blob):
        if m.group(0) not in allowed:
            out.append("an address-shaped string nobody acted on is in the payload: %s" % m.group(0))
    if client.get("/v1/admin/gaming", headers={"X-Admin-Token": "not-the-token"}).status_code != 403:
        out.append("a wrong operator token was not refused")
    if client.get("/v1/admin/gaming").status_code != 503:
        out.append("a missing operator token did not answer SIGNER_UNAVAILABLE")
    return out


def c29_the_rules_travel_with_their_other_reading(p: Probe) -> tuple:
    """D7: four detectors, each serving its rule AND the innocent reading, wired through to the served dashboard."""
    findings = gaming_detector_findings()
    os.environ["PGM_ADMIN_TOKEN"] = "adm_" + "g" * 44
    findings += gaming_api_findings(p.app(), p.client(), "adm_" + "g" * 44)
    return ("4 detectors fire on a planted farm and stay quiet on an honest tape, each serving its rule and the "
            "innocent reading through the API", not findings, "; ".join(findings[:6]))


def c30_one_click_is_one_row_and_the_boards_obey(p: Probe) -> tuple:
    """D7: exclude/flag/include through the served route, the replay rule, and the public board obeying."""
    import seed_leaderboard
    out = []
    token = "adm_" + "g" * 44
    os.environ["PGM_ADMIN_TOKEN"] = token
    client = p.client()
    wallet = seed_leaderboard.wallet_for(3)
    anon = p.app()._anon(wallet)

    def click(action, reason, key):
        body = {"wallet": wallet, "action": action, "reason": reason}
        return client.post("/v1/admin/gaming/decide", json=body,
                           headers={"X-Admin-Token": token, "Idempotency-Key": key})

    def board_has() -> bool:
        status, body = p.get("/v1/leaderboard", board="risk_adjusted", limit=200)
        return status == 200 and any(row.get("anon") == anon for row in body.get("rows") or [])

    if not board_has():
        return ("one click is one append-only row and the public board obeys it", False,
                "the seeded board does not contain the wallet the gate is about to exclude")
    if click("flag", "the gate: climbing on a thin record", "g11-d7-flag").status_code != 200:
        out.append("a flag was refused")
    if not board_has():
        out.append("a flag removed a wallet from a board; a flag is a question, not a removal")
    clicked = click("exclude", "the gate: confirmed as a wash pair", "g11-d7-exclude")
    if clicked.status_code != 200:
        out.append("an exclude was refused (%d)" % clicked.status_code)
    if board_has():
        out.append("the public board still lists a wallet the operator excluded")
    replay = click("exclude", "the gate: confirmed as a wash pair", "g11-d7-exclude")
    if replay.status_code != 200 or replay.json().get("atMs") != clicked.json().get("atMs"):
        out.append("a retried click did not replay the stored answer")
    if click("include", "the gate: put back after review", "g11-d7-include").status_code != 200:
        out.append("an include was refused")
    if not board_has():
        out.append("the board did not list the wallet again after an include")

    # The reason is deliberately valid here: this call is about the MISSING KEY, and a short reason would answer
    # 422 first (`_check_body` -> semantics -> key shape, the order every mutating route since P10 uses), which is
    # how this check first failed - naming a rule it was not testing.
    no_key = client.post("/v1/admin/gaming/decide",
                         json={"wallet": wallet, "action": "exclude", "reason": "the gate: no key was sent at all"},
                         headers={"X-Admin-Token": token})
    if no_key.status_code != 400:
        out.append("a decision without an Idempotency-Key answered %d, not 400" % no_key.status_code)
    short = client.post("/v1/admin/gaming/decide", json={"wallet": wallet, "action": "exclude", "reason": "short"},
                        headers={"X-Admin-Token": token, "Idempotency-Key": "g11-d7-short"})
    if short.status_code != 422:
        out.append("a decision with an unusable reason answered %d, not 422" % short.status_code)
    bad_action = client.post("/v1/admin/gaming/decide",
                             json={"wallet": wallet, "action": "banish", "reason": "the gate: not an action"},
                             headers={"X-Admin-Token": token, "Idempotency-Key": "g11-d7-action"})
    if bad_action.status_code != 422:
        out.append("a decision outside the action vocabulary answered %d, not 422" % bad_action.status_code)

    landed = p.app()._db.execute("SELECT wallet, action, reason, actor FROM leaderboard_exclusions"
                                 " WHERE wallet=?", (wallet,)).fetchall()
    if len(landed) != 3:
        out.append("%d decision row(s) landed for 4 clicks; a replay must not append" % len(landed))
    audit = p.app()._db.execute("SELECT target_id FROM audit_log WHERE action='gaming.decide'").fetchall()
    if not audit:
        out.append("no audit row was written for any decision")
    for a in audit:
        if "0x" in str(a[0]):
            out.append("an audit row carries the wallet rather than the pseudonym")
    rows = [{"action": str(r[1]), "reason": str(r[2]), "actor": str(r[3]), "wallet": str(r[0]), "anon": anon,
             "idempotencyKey": "g11-d7-%s" % str(r[1])} for r in landed]
    out += decision_journal_findings(rows)
    return ("one click is one append-only row: flag asks, exclude removes from the public board, include restores, "
            "and a retried click replays", not out, "; ".join(out[:6]))

CHECKS = (c1_contract, c2_no_addresses, c3_gate_pair, c4_win_rate_gate, c5_refusals, c6_no_hidden_losses,
          c7_integers_only, c8_freshness, c9_read_plans, c10_history, c11_exclusions, c12_population,
          c13_published, c14_board_orders, c15_wallet_standing, c16_comparison_and_follows,
          c17_self_rank_every_board, c18_identity_never_leaks,
          # D5. The two halves of the kit's second acceptance sentence, then the two rules that decide whether the
          # programme is solvent and survivable.
          c19_reward_needs_a_trade, c20_second_wallet_is_caught, c21_dashboard_arithmetic,
          c22_payout_reality_and_clawback,
          # D6. The pages, the budget that keeps them serving, the payloads a stranger receives, and the one URL
          # grammar three artifacts have to agree about.
          c23_public_pages_exist_and_are_server_rendered, c24_the_budget_refuses_and_then_blocks,
          c25_no_address_and_the_card_carries_its_qualifiers, c26_the_pages_are_published_once,
          # ...and the schema invariant D6 found broken while reading 0005 for the D6 migration: an append-only
          # table needs a trigger AND a grant, and four tables had only the first.
          c27_append_only_has_both_halves,
          # D6's own sentence, on the rendered document: 200 for a client with no cookies, the rows in the HTML,
          # the canonical link and the graph in the head, no address anywhere, and a card that is an image.
          c28_the_pages_render_for_a_stranger,
          # D7. The dashboard's two claims: every finding arrives with its rule AND the innocent reading of the
          # same shape, and one click is one append-only row the public board obeys.
          c29_the_rules_travel_with_their_other_reading, c30_one_click_is_one_row_and_the_boards_obey)


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
    def standing():
        rows = {1: {"anon": "w_best", "scoreBps": 900},
                2: {"anon": "w_second", "scoreBps": 800},
                3: {"anon": "w_third", "scoreBps": 700}}
        good = {"anon": "w_second", "state": "ranked", "rank": 2, "rankedTotal": 3, "orderField": "scoreBps",
                "rankBadge": {"rank": 2, "rankedTotal": 3, "text": "#2"}, "percentileBps": 6667,
                "above": {"anon": "w_best"}, "below": {"anon": "w_third"},
                "gap": {"rankAbove": 1, "anonAbove": "w_best", "field": "scoreBps", "units": "bps",
                        "value": 800, "valueAbove": 900, "delta": 100, "toPass": 901}}
        counting_order = dict(good, rankedTotal=2, rankBadge={"rank": 2, "rankedTotal": 2, "text": "#2"},
                              percentileBps=10_000)
        tie = dict(good, gap=dict(good["gap"], toPass=900))                    # a tie would not pass
        wrong_neighbour = dict(good, above={"anon": "w_third"})
        no_placing = {"anon": "w_thin", "state": "unranked", "rank": 47, "rankBadge": {"rank": 47},
                      "reasons": ["9 settled markets; this board needs 20"], "rankedTotal": 3}
        good_unranked = {"anon": "w_thin", "state": "unranked", "rank": None, "rankBadge": None,
                         "reasons": ["9 settled markets; this board needs 20"], "rankedTotal": 3}
        for case in (good, counting_order, good_unranked):
            assert not standing_findings(case, rows), case
        got = [len(standing_findings(x, rows)) for x in (tie, wrong_neighbour, no_placing)]
        return got == [1, 1, 1], got

    @canary
    def follows():
        good = {"anon": "w_2", "state": "followed", "followed": True, "existed": False, "note": "a watch, not a copy"}
        listing = {"note": "a follow is a watch, not a copy",
                   "rows": [{"anon": "w_2", "state": "ranked"}, {"anon": "w_3", "state": "unranked",
                                                                "reasons": ["9 settled markets"]}]}
        silent = dict(good, note="saved")
        address = {"note": "a watch, not a copy",
                   "rows": [{"anon": "0x" + "ab" * 20, "state": "ranked"}]}
        reasonless = {"note": "a watch, not a copy", "rows": [{"anon": "w_3", "state": "unranked", "reasons": []}]}
        return (not follow_findings(good, listing, "w_2") and len(follow_findings(silent, listing, "w_2")) == 1
                and len(follow_findings(good, address, "w_2")) == 1
                and len(follow_findings(good, reasonless, "w_2")) == 1), follow_findings(silent, listing, "w_2")

    @canary
    def self_rank():
        """Four plantings: a panel missing the category board's four answers, an `offPage` that disagrees with the
        page size, a badge that is not the rank, and a pin on a category board."""
        good = {"wallets": [{"anon": "w_1", "boards": [
            {"board": "risk_adjusted", "category": None, "state": "ranked", "rank": 12, "rankedTotal": 64,
             "rankBadge": {"text": "#12"}, "pageSize": 50, "offPage": False,
             "gap": {"toPass": 5, "valueAbove": 4, "value": 1}},
            {"board": "category", "category": "Politics", "state": "ranked", "rank": 3, "rankedTotal": 11,
             "rankBadge": {"text": "#3"}, "pageSize": 50, "offPage": False, "gap": {"toPass": 9, "valueAbove": 8, "value": 1}},
            {"board": "category", "category": "Sports", "state": "unranked", "rank": None, "rankBadge": None,
             "pageSize": 50, "offPage": True},
            {"board": "category", "category": "Crypto", "state": "unranked", "rank": None, "rankBadge": None,
             "pageSize": 50, "offPage": True},
            {"board": "category", "category": "Finance", "state": "unranked", "rank": None, "rankBadge": None,
             "pageSize": 50, "offPage": True}]}],
            "walletCount": 1,
            "primary": {"defaultBoard": "risk_adjusted", "pin": {"board": "risk_adjusted", "category": None,
                                                                 "state": "ranked", "rank": 12, "pageSize": 50,
                                                                 "offPage": False, "rankBadge": {"text": "#12"},
                                                                 "gap": {"toPass": 5, "valueAbove": 4, "value": 1}}}}
        boards = ("risk_adjusted", "category")
        cats = ("Politics", "Sports", "Crypto", "Finance")
        missing_board = json.loads(json.dumps(good))
        missing_board["wallets"][0]["boards"] = [b for b in missing_board["wallets"][0]["boards"]
                                                if b["category"] != "Finance"]
        wrong_page = json.loads(json.dumps(good))
        wrong_page["wallets"][0]["boards"][0]["rank"] = 70
        wrong_page["primary"]["pin"]["rank"] = 70
        bad_badge = json.loads(json.dumps(good))
        bad_badge["wallets"][0]["boards"][0]["rankBadge"]["text"] = "#1"
        cat_pin = json.loads(json.dumps(good))
        cat_pin["primary"]["pin"]["board"] = "category"
        for case in (good,):
            assert not self_rank_findings(case, boards, cats), self_rank_findings(case, boards, cats)
        got = [len(self_rank_findings(x, boards, cats))
               for x in (missing_board, wrong_page, bad_badge, cat_pin)]
        return all(n >= 1 for n in got), got

    @canary
    def identity_privacy():
        """The privacy scanner fires in both directions: a private handle that is published, and a listed handle
        that is not."""
        payloads = [("board:risk_adjusted", {"rows": [{"anon": "w_1", "handle": "gate_handle"}]})]
        quiet = [("board:risk_adjusted", {"rows": [{"anon": "w_1", "handle": None}]})]
        return (not privacy_findings(quiet, "gate_handle", listed=False)
                and len(privacy_findings(payloads, "gate_handle", listed=False)) == 1
                and not privacy_findings(payloads, "gate_handle", listed=True)
                and len(privacy_findings(quiet, "gate_handle", listed=True)) == 1), "both directions"

    @canary
    def referral_model():
        """A share off the EXPECTED fee, a share that is not the published rate, an accrual for a referee who never
        traded, and an accrual with no fee behind it: the four ways a referral programme pays for nothing."""
        rows = [{"referee": "u_a", "feeObservedMicro": 8_000_000, "shareMicro": 2_000_000}]
        expected_not_observed = [{"referee": "u_a", "feeObservedMicro": 40_000_000, "shareMicro": 2_000_000}]
        wrong_rate = [{"referee": "u_a", "feeObservedMicro": 8_000_000, "shareMicro": 4_000_000}]
        unqualified = [{"referee": "u_b", "feeObservedMicro": 8_000_000, "shareMicro": 2_000_000}]
        no_fee = [{"referee": "u_a", "feeObservedMicro": 0, "shareMicro": 0}]
        qualifies = {"u_a": True}
        assert not referral_model_findings(rows, 2500, qualifies), "the good row was flagged"
        got = [len(referral_model_findings(x, 2500, qualifies))
               for x in (expected_not_observed, wrong_rate, unqualified, no_fee)]
        return all(n >= 1 for n in got), got

    @canary
    def referral_collisions():
        """A shared funding digest that was NOT held, a self-referral with a row, and a refusal with no queue
        item: each one is a hole in the same wall."""
        attributions = [{"referee": "u_a", "referrer": "u_ref", "state": "pending"},
                        {"referee": "u_b", "referrer": "u_ref", "state": "review"}]
        # The FIRST referee on a shared device was clean when they arrived; the SECOND is the one held. The
        # referrer's own digest is a third value, so nothing here is a self-referral wearing a hat.
        signals = {"u_ref": {"d_referrer"}, "u_a": {"d_1"}, "u_b": {"d_1"}}
        queue = [{"referee": "u_b", "state": "open", "kind": "duplicate_funding"}]
        assert not referral_collision_findings(attributions, queue, signals), "the held pair was flagged"
        silent = [dict(attributions[0], state="pending"), dict(attributions[1], state="pending")]
        self_row = [{"referee": "u_ref", "referrer": "u_ref", "state": "pending"}]
        orphans = [dict(attributions[0]), dict(attributions[1])]
        unreported = [dict(attributions[0]), dict(attributions[1], state="pending")]
        got = [len(referral_collision_findings(silent, queue, signals)),
               len(referral_collision_findings(self_row, queue, signals)),
               len(referral_collision_findings(orphans, queue, {"u_ref": {"d_1"}, "u_a": {"d_1"}})),
               len(referral_collision_findings(unreported, [], signals))]
        return all(n >= 1 for n in got), got

    @canary
    def referral_dashboard():
        """A funnel that goes up, a bucket sum that does not add up, a paid figure above everything ever accrued,
        and an `earned` step that is not the number of referees still owed money."""
        good = {"funnel": {"clicks": 0, "signups": 2, "funded": 2, "trading": 2, "earned": 2},
                "earnings": {"accruedMicro": 4_000_000, "settledMicro": 0, "holdingMicro": 4_000_000,
                             "payableMicro": 0, "paidMicro": 0, "clawedBackMicro": 0},
                "referrals": [{"referee": "u_a", "earnedMicro": 2_000_000}, {"referee": "u_b", "earnedMicro": 2_000_000}],
                "hidden": {"refused": 0}, "funnelFindings": []}
        rising = json.loads(json.dumps(good))
        rising["funnel"]["funded"] = 3
        leaking = json.loads(json.dumps(good))
        leaking["earnings"]["accruedMicro"] = 5_000_000
        overpaid = json.loads(json.dumps(good))
        overpaid["earnings"]["paidMicro"] = 9_000_000
        miscounted = json.loads(json.dumps(good))
        miscounted["funnel"]["earned"] = 1
        assert not referral_dashboard_findings(good), referral_dashboard_findings(good)
        got = [len(referral_dashboard_findings(x)) for x in (rising, leaking, overpaid, miscounted)]
        return all(n >= 1 for n in got), got

    @canary
    def referral_terms():
        """A rules list with the clawback missing, a tax form that is not named, an uncertainty marker that was
        dropped, and a payout minimum that disagrees with the engine's."""
        good = {"rules": ["a clawback cancels unpaid accruals first", "there is no second level",
                          "a referral funded from the same source is refused",
                          "revoking the builder code is the ground for a self-referral"],
                "noReferrerLeaderboard": "no public referrer leaderboard: it would be a spam contest",
                "rejectedModels": ["flat bounty on a first funded trade: it pays for a signup"],
                "schedule": "monthly, by the 10th", "modelSentence": "a share of the builder fee we are paid",
                "paidFrom": "the fee the venue actually paid us", "payoutMinMicro": 20_000_000,
                "taxNote": "[UNVERIFIED] the forms are a jurisdiction review"}
        payout = {"minimumMicro": 20_000_000,
                  "tax": {"form": "W-9", "reportForm": "1099-NEC", "note": "before the first payout"}}
        no_claw = dict(good, rules=[r for r in good["rules"] if "claw" not in r])
        no_form = {"minimumMicro": 20_000_000, "tax": {"form": "", "reportForm": ""}}
        no_marker = dict(good, taxNote="the forms are a jurisdiction review")
        wrong_min = {"minimumMicro": 5_000_000, "tax": payout["tax"]}
        assert not referral_terms_findings(good, payout), referral_terms_findings(good, payout)
        got = [len(referral_terms_findings(no_claw, payout)), len(referral_terms_findings(good, no_form)),
               len(referral_terms_findings(no_marker, payout)), len(referral_terms_findings(good, wrong_min))]
        return all(n >= 1 for n in got), got

    @canary
    def gaming_rule_halves():
        """The rules the dashboard serves: complete, half-served, empty, and moulded from the real pair."""
        # Long enough to pass the check's own length floors: a canary whose "good" fixture is too short to be
        # accepted is a canary that reports the fixture rather than the scanner (which is what it did first).
        window = ("A wallet is listed when it gains at least 25 places inside seven days and the gain is either "
                  "three times the median climb on that board for the same window or at or above the 99th "
                  "percentile of every climb on it, so the board's own churn sets the bar rather than a constant")
        innocent = ("A lucky streak, a genuine edge that only just arrived, or a wallet that was simply unknown to "
                    "us and is being discovered by the tape: rank is a comparison, so somebody climbs every week")
        real = {k: {"rule": window + " " + k, "innocent": innocent + " " + k} for k in GAMING_KINDS}
        complete = {**real, "fast_climb": {"rule": window, "innocent": innocent}}
        no_innocent = {k: dict(v) for k, v in complete.items()}
        no_innocent["climbers_typo"] = no_innocent.pop("fast_climb")
        same = {k: dict(v) for k, v in complete.items()}
        same["fast_climb"] = {"rule": window, "innocent": window}
        short = {k: dict(v) for k, v in complete.items()}
        short["fast_climb"] = {"rule": "climbs fast", "innocent": innocent}
        for case in (complete,):
            assert not gaming_rule_findings(case), case
        got = [len(gaming_rule_findings(no_innocent)), len(gaming_rule_findings(same)),
               len(gaming_rule_findings(short)), len(gaming_rule_findings({}))]
        return got == [2, 1, 1, 4], got

    @canary
    def decision_journal():
        """A journal that is a record, one that repeats a key, one with no reason, one with an unknown action."""
        good = {"action": "exclude", "reason": "confirmed as a wash pair", "actor": "admin:fast_climb",
                "wallet": "0xabc", "anon": "w_1", "idempotencyKey": "k-1"}
        clean = [good, {**good, "action": "include", "reason": "put back after review", "idempotencyKey": "k-2"}]
        replay = [good, {**good, "idempotencyKey": "k-1"}]
        silent = [good, {**good, "reason": "hmm", "idempotencyKey": "k-3"}]
        bogus = [good, {**good, "action": "banish", "idempotencyKey": "k-4"}]
        for case in (clean,):
            assert not decision_journal_findings(case), case
        got = [len(decision_journal_findings(replay)), len(decision_journal_findings(silent)),
               len(decision_journal_findings(bogus))]
        return got == [1, 1, 1], got

    @canary
    def append_only_halves():
        """D6's schema scanner, in both directions: nothing fires on a guarded table, and each half fires alone.

        The two halves fail independently, and the grant is the one that shipped broken seventeen times. The array
        form is planted too: the grant lives in a `FOREACH ... ARRAY[...]` block in 0005 and 0018, so a scanner that
        only understood `REVOKE ... ON t` would call every table in those two files unguarded.
        """
        both = {"0018.sql": "CREATE TRIGGER append_only_thing\n    BEFORE UPDATE OR DELETE ON thing\n"
                            "    FOR EACH ROW EXECUTE FUNCTION f();\n"
                            "REVOKE UPDATE, DELETE ON thing FROM PUBLIC;"}
        no_grant = {"0018.sql": "CREATE TRIGGER append_only_thing BEFORE UPDATE OR DELETE ON thing"
                                " FOR EACH ROW EXECUTE FUNCTION f();"}
        no_trigger = {"0018.sql": "REVOKE UPDATE, DELETE ON thing FROM PUBLIC;"}
        array_form = {"0005.sql": "FOREACH t IN ARRAY ARRAY['thing','other']\n    LOOP",
                      "0018.sql": "CREATE TRIGGER append_only_thing BEFORE UPDATE OR DELETE ON thing"
                                  " FOR EACH ROW EXECUTE FUNCTION f();"}
        got = [len(append_only_findings(both, ["thing"])), len(append_only_findings(no_grant, ["thing"])),
               len(append_only_findings(no_trigger, ["thing"])), len(append_only_findings(array_form, ["thing"]))]
        return got == [0, 1, 1, 0], got

    @canary
    def public_payloads_and_cards():
        """D6's two scanners, on planted violations: an address three levels down, a graph claim the page never
        makes, and a provisional card that lost the sentence that qualifies it.

        The address is planted in a NESTED object on purpose — the failure this scanner exists for is a field a
        component added inside `evidence` or a sorted `links` map, and a scanner that only read the top level would
        pass on exactly the payload it is meant to catch.
        """
        clean = {"card": {"kind": "trader", "title": "@deep_book", "ranked": True, "provisional": True,
                          "footnote": ["provisional: fewer than 7 days of history", "drawdown 400"]},
                 "notes": ["provisional: 3 days of history"],
                 "structuredData": [{"@type": "ProfilePage", "name": "@deep_book", "url": "https://x.example/t"}]}
        nested = {"payload": {"deep": {"evidence": {"wallet": "0x" + "ab" * 20}}}}
        unbacked = {"card": {"ranked": True, "provisional": False, "footnote": ["drawdown 400"]},
                    "notes": ["worst drawdown 400"],
                    "structuredData": [{"@type": "ProfilePage", "description": "$9,999,999 profit"}]}
        unqualified = {"card": {"ranked": True, "provisional": True, "footnote": ["drawdown 400"]},
                       "notes": ["provisional: 3 days of history"]}
        no_notes = dict(clean, notes=[])
        got = [len(public_payload_findings([("planted", nested)])), len(card_findings(unbacked)),
               len(card_findings(unqualified)), len(card_findings(no_notes))]
        assert public_payload_findings([("clean", clean)]) == []
        return got == [1, 1, 1, 1], got

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
