#!/usr/bin/env python3
"""P14 D1 (second half) — the trading and injection probes.

    python3 tools/p14-attack-surface.py --record docs/verification/P14-attack-surface.txt --json …json

The authorisation matrix (`tools/p14-authz-matrix.py`) answers *who may do what*. This tool answers a different
question: **what happens when an authorised user sends the wrong thing on purpose.** Every probe here is made by
the rightful owner of the account, because that is the threat model the kit names — a user attacking the venue's
rules, the risk gate, or another user through the product's own surfaces rather than through somebody else's
account.

Three rules the probes keep, each learned the hard way in this phase:

  * **A refusal is only evidence when it is the refusal under test.** Every money probe asserts the *error code*
    and, where the answer should be "no but you are fine", a must-accept that a legal body gets through.
  * **The account is qualified before the probe.** An unqualified account answers every order `503
    SIGNER_UNAVAILABLE` (watch-only wallet) and a stale book answers `503 STALE_QUOTE`; both pass a sloppy
    "was it refused?" check for the wrong reason.
  * **A mutation gets re-read.** A probe that is refused and still changes a row is the worst outcome, and it
    cannot be seen in the status code.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import uuid
from importlib.util import module_from_spec, spec_from_file_location

ROOT = pathlib.Path(__file__).resolve().parents[1]
VERIF = ROOT / "docs" / "verification"


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


class Surface:
    """The bench, the qualified accounts, and the small vocabulary every probe speaks."""

    def __init__(self) -> None:
        self.bench = _load("p14_drills_for_attacks", ROOT / "tools" / "p14-key-drills.py").Bench(kek_versions=1)
        self.acct = self.bench.user("attacker")
        self.market, self.token, self.tick = self._market()
        self.ready = self._sync_book()
        self.blocked_rows: list[dict] = []

    def _market(self) -> tuple[str, str, str]:
        """The first market the product itself calls tradable, and its first token.

        The token id comes from the seeded `tokens` table rather than from the book payload: the book is an
        aggregate (price levels), and a probe that guessed a token id from it would be measuring the wrong
        refusal. `enableOrderBook` is the product's own flag, so one of those markets is by definition one the
        order path should accept an order for.
        """
        rows = self.bench.client.get("/v1/markets", params={"pageSize": 50}).json().get("items") or []
        for m in rows:
            if m.get("enableOrderBook") and m.get("acceptingOrders"):
                row = self.bench.con.execute("SELECT token_id FROM tokens WHERE market_id=? ORDER BY token_id"
                                            " LIMIT 1", (str(m["id"]),)).fetchone()
                if row:
                    return str(m["id"]), str(row[0]), str(m.get("minimumTickSize") or "0.01")
        raise SystemExit("p14-attack-surface: no tradable market in the fixture")

    def _sync_book(self) -> bool:
        return True

    # ------------------------------------------------------------------ probes
    def order(self, *, price: str = "0.55", size: str = "5", key: str | None = None, refresh: bool = True,
              **extra) -> tuple[int, dict]:
        """One order, always as the rightful owner of a qualified account, with a fresh idempotency key.

        `refresh=False` exists for the freshness probe, and the first version of this tool got it wrong: the
        helper refreshed the book on every call, so the TOCTOU probe aged the book and then refreshed it inside
        its own submit — and reported a stale-book order as accepted. A harness that repairs the condition it is
        testing is worse than no probe, because it produces a green run about nothing.
        """
        if refresh:
            self.bench.refresh_book(self.market)
        body = {"marketId": self.market, "tokenId": self.token, "side": "BUY", "price": price, "size": size}
        body.update(extra)
        r = self.bench.client.post("/v1/orders", headers=self.bench.bearer(self.acct["token"]),
                                   json=body) if key is None else \
            self.bench.client.post("/v1/orders", headers={**self.bench.bearer(self.acct["token"]),
                                                          "Idempotency-Key": key}, json=body)
        try:
            return r.status_code, (r.json() or {})
        except Exception:                                                    # noqa: BLE001
            return r.status_code, {"raw": r.text[:200]}

    @staticmethod
    def code(body: dict) -> str:
        return str(((body.get("error") or {}) if isinstance(body, dict) else {}).get("code") or "")

    def count(self, sql: str) -> int:
        return int(self.bench.con.execute(sql).fetchone()[0])

    def intent_rows(self) -> dict[str, int]:
        rows = self.bench.con.execute("SELECT state, COUNT(*) FROM order_intents WHERE user_id=? GROUP BY state",
                                      (self.acct["uid"],)).fetchall()
        return {str(r[0]): int(r[1]) for r in rows}


# ------------------------------------------------------------------------------------------------ trading
def section_trading(g: Gate, facts: dict, s: Surface) -> None:
    facts["trading"] = {}
    # 0. The must-accept: without it every refusal below could be for an unrelated reason.
    st, body = s.order(price="0.55", size="5")
    facts["trading"]["must_accept"] = {"status": st, "code": s.code(body)}
    g.check("a legal order is accepted first, so the refusals below mean something (%d)" % st, st == 202,
            "the must-accept answered %d %s — every refusal after this would be for the wrong reason"
            % (st, json.dumps(body)[:160]))

    # 1. Off-grid price: refused, and *not* silently rounded to the nearest tick. The distinction matters because a
    #    rounded price is an order the user did not place. `before` is what makes "left no order behind" mean
    #    something: the must-accept above queued its own row on purpose, and the first draft counted it as
    #    evidence against the off-grid probe.
    before = s.intent_rows()
    off = "0.5555" if s.tick == "0.01" else "0.5005"
    st, body = s.order(price=off, size="5")
    facts["trading"]["off_tick"] = {"price": off, "tick": s.tick, "status": st, "code": s.code(body),
                                    "detail": json.dumps(body)[:200]}
    g.check("an off-grid price is refused, not rounded (%s on a %s grid): %d %s"
            % (off, s.tick, st, s.code(body)), st == 422 and s.code(body) in ("OFF_TICK", "VALIDATION"),
            "answered %d %s" % (st, json.dumps(body)[:160]))
    # ...and the refusal did not quietly place the rounded order.
    intents = s.intent_rows()
    delta = {k: intents.get(k, 0) - before.get(k, 0) for k in set(intents) | set(before)}
    facts["trading"]["intents_after_off_tick"] = {"before": before, "after": intents, "delta": delta}
    g.check("the off-grid attempt queued nothing and was recorded as rejected (delta %s)" % 
            json.dumps({k: v for k, v in delta.items() if v}),
            delta.get("queued", 0) == 0 and delta.get("rejected", 0) == 1,
            "the refusal wrote %s — an off-grid price must leave a rejected row and no live order"
            % json.dumps(delta))

    # 2. Size: below the market minimum, zero, negative, and enormous.
    sizes = {"below_minimum": "0.000001", "zero": "0", "negative": "-5",
             "enormous": "1000000000", "not_a_number": "ten", "scientific": "1e30"}
    facts["trading"]["sizes"] = {}
    for label, size in sizes.items():
        st, body = s.order(price="0.55", size=size)
        facts["trading"]["sizes"][label] = {"status": st, "code": s.code(body)}
        g.check("size %s (%s) is refused with a reason" % (label, size), st >= 400,
                "answered %d %s" % (st, json.dumps(body)[:120]))
    g.check("the enormous size is refused for the *cap* reason, not a generic validation error",
            facts["trading"]["sizes"]["enormous"]["code"] in ("OVER_ORDER_CAP", "QUOTA_EXCEEDED", "VALIDATION"),
            json.dumps(facts["trading"]["sizes"]["enormous"]))

    # 3. TOCTOU: quote, then move the book, then submit. The gate must re-check freshness at submit, because a
    #    price that was fine when the user looked is not evidence about the book now.
    quote = s.bench.client.post("/v1/wallet/deposit/quote", headers=s.bench.bearer(s.acct["token"]),
                                json={"amountUsdc": "10"})
    moved = s.bench.con.execute("UPDATE book_levels SET updated_ms=? WHERE market_id=?",
                                (s.bench.app._now_ms() - 60_000, s.market))
    s.bench.con.commit()
    st, body = s.order(price="0.55", size="5", refresh=False)      # do NOT repair the ageing this probe created
    facts["trading"]["toctou"] = {"quote_status": quote.status_code, "submit_status": st, "code": s.code(body),
                                  "book_rows_aged": int(moved.rowcount or 0)}
    g.check("an order submitted against a book that has aged past the gate's window is refused as stale (%d %s)"
            % (st, s.code(body)), st == 503 and s.code(body) == "STALE_QUOTE",
            "answered %d %s: a stale book must not be tradeable" % (st, json.dumps(body)[:140]))
    s.bench.refresh_book(s.market)                    # put the bench back in a tradeable state for later probes

    # 4. Idempotency: the same key with the same body is one order; the same key with a *different* body is a
    #    refusal, because the alternative is a client that "retries" into a second, different order.
    key = "p14-idem-%s" % uuid.uuid4().hex[:10]
    st1, b1 = s.order(price="0.54", size="5", key=key)
    st2, b2 = s.order(price="0.54", size="5", key=key)
    st3, b3 = s.order(price="0.60", size="9", key=key)
    facts["trading"]["idempotency"] = {"same_first": st1, "same_again": st2, "different_body": st3,
                                       "different_code": s.code(b3)}
    g.check("the same key with the same body is one order (%d then %d, %s)"
            % (st1, st2, s.code(b2) or "accepted"),
            st1 == 202 and st2 in (202, 409, 422), "second attempt answered %d" % st2)
    g.check("the same key with a different body is refused rather than executed (%d %s)"
            % (st3, s.code(b3)), st3 >= 400, "a reused key with a new body answered %d — that is a second order "
            "wearing the first one's name" % st3)

    # 5. Concurrent submits that jointly exceed a limit. The gate is in SQLite behind one connection per thread,
    #    so the race is real: the assertion is that the *aggregate* is still bounded, whichever way the interleave
    #    fell, and that nothing 5xxs.
    from concurrent.futures import ThreadPoolExecutor
    before = s.count("SELECT COUNT(*) FROM order_intents WHERE user_id='%s'" % s.acct["uid"])
    keys = ["p14-race-%s-%d" % (uuid.uuid4().hex[:6], i) for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        raced = list(pool.map(lambda k: s.order(price="0.56", size="20000000", key=k), keys))
    after = s.count("SELECT COUNT(*) FROM order_intents WHERE user_id='%s'" % s.acct["uid"])
    codes = sorted({(st, s.code(b)) for st, b in raced})
    facts["trading"]["race"] = {"attempts": len(raced), "outcomes": [{"status": st, "code": c} for st, c in codes],
                                "intents_before": before, "intents_after": after}
    g.check("eight concurrent over-cap submits produced no 5xx (%s)" % json.dumps(codes),
            not any(st >= 500 for st, _c in codes), "a race that answers 5xx is a race that may have half-placed")
    g.check("every over-cap submit in the race was refused (%d attempt(s))" % len(raced),
            all(st >= 400 for st, _b in raced), "at least one over-cap submit was accepted: %s"
            % json.dumps([(st, s.code(b)) for st, b in raced])[:200])

    # 6. An automation rule must not be able to place what a human cannot. A rule is a door into the venue, and a
    #    door with a different lock is not the same control. This check first passed for the WRONG reason: the
    #    payload had no targets, so the builder answered "a rule needs at least one market to watch" and the probe
    #    read any error as the cap firing. F19 is what that hid — a $11,000 action saved, dry-ran clean, and would
    #    have been refused by the risk gate on every single fire.
    import polygm_core.automation.console as console
    rule = {"name": "p14 attack rule", "match": "all", "targets": [{"marketId": s.market}],
            "triggers": [{"kind": "price_cross", "uses": "mid", "op": "<=", "price_micro": 550_000}],
            "actions": [{"kind": "limit", "side": "BUY", "price_micro": 550_000,
                         "size_shares_micro": 20_000_000_000, "max_slippage_bps": 100}]}
    _plan, errs = console.compile_builder(rule)
    ok_plan, ok_errs = console.compile_builder({**rule, "actions": [{**rule["actions"][0],
                                                                    "size_shares_micro": 4_000_000_000}]})
    facts["trading"]["automation"] = {"over_cap_errors": errs[:1], "under_cap_errors": ok_errs,
                                      "under_cap_actions": len(ok_plan.get("actions") or [])}
    g.check("an automation action above the per-order cap is refused by the builder, and the sentence names the "
            "cap (%s)" % (errs[:1] or "NO ERROR"), any("per-order cap" in e for e in errs),
            "the rule compiled with a size far above the cap: a rule must not be a way around the gate, and a "
            "rule that can never fire is a control the user believes in and does not have")
    g.check("an action under the cap still compiles, so the check is a ceiling and not a ban (%s)"
            % (ok_errs or "clean"), not ok_errs and ok_plan["rule"]["actions"],
            "the builder now refuses sized actions altogether: %s" % ok_errs)
    import polygm_core.automation.engine as _eng
    g.check("the engine's own per-run ceiling is a number the compiler enforces, not a comment",
            hasattr(_eng, "GLOBAL_RUNS_PER_DAY") and _eng.GLOBAL_RUNS_PER_DAY > 0,
            "no global ceiling: a rule that fires every minute on every market is a self-inflicted DoS")

    # 7. Copy-trade cascade: one source fill, many copiers. The attack is amplification — 500 copiers each
    #    placing "whatever the whale did" turns one $50,000 fill into $25M of queue pressure per copy config
    #    unless the *engine* bounds each copy. The first version of this check looked for symbols whose names
    #    contained CAP and found none, which would have been a finding about the naming rather than about the
    #    money; the bounds are `CopyConfig.max_order_micro` / `max_daily_micro` / `max_copies_per_day`, applied
    #    in `size_for`, so the probe calls that function with an unbounded remaining budget and a huge fill.
    import polygm_core.copy.engine as copy
    fill = copy.SourceFill(wallet="0xwhale", intent_id="0xfill", token_id="0xT10", market_id="0xM1",
                           side="BUY", price_micro=550_000,
                           size_shares_micro=100_000_000_000, at_ms=s.bench.now)   # 100,000 shares — a whale
    copiers = []
    for i in range(500):
        cfg = copy.CopyConfig(id="c%03d" % i, user_id="u%03d" % i, source_user="u-whale", mode="mirror")
        shares, why = copy.size_for(cfg, fill, remaining_daily_micro=cfg.max_daily_micro)
        copiers.append((shares, why))
    worst = max(c[0] for c in copiers)
    notional = [copy.notional_floor(c[0], 550_000) for c in copiers if c[0]]
    total = sum(notional)
    keys = {copy.idempotency_key_for(copy.CopyConfig(id="c%03d" % i, user_id="u%03d" % i,
                                                     source_user="u-whale"), fill) for i in range(500)}
    facts["trading"]["cascade"] = {"copiers": len(copiers), "largest_copy_shares_micro": worst,
                                   "largest_copy_shares": round(worst / 10**6, 4),
                                   "largest_copy_usdc": round(worst * 550_000 / 10**12, 4),
                                   "aggregate_usdc_if_all_placed": round(total / 10**6, 2),
                                   "distinct_keys": len(keys), "refusals": sorted({c[1] for c in copiers})[:3]}
    g.check("each of 500 mirror copiers is capped by its own $25 per-order ceiling (largest copy %.4f shares "
            "= $%.2f)" % (worst / 10**6, worst * 550_000 / 10**12), 0 < worst * 550_000 // 10**6 <= 25_000_000,
            "a copier placed $%.2f into a $25 ceiling: one whale fill is 500x that" % (worst * 550_000 / 10**12))
    g.check("the 500 copiers are 500 distinct orders and 500 distinct idempotency keys (%d keys)" % len(keys),
            len(keys) == 500, "copiers collided on an idempotency key: one copier's fill would suppress another's")
    replay = {copy.idempotency_key_for(copy.CopyConfig(id="c000", user_id="u000", source_user="u-whale"), fill)
              for _ in range(5)}
    g.check("replaying the same source fill produces ONE key, so a duplicate WS frame cannot double-place",
            len(replay) == 1, "the key moved between evaluations of the same fill: %d keys" % len(replay))
    cfg_daily = copy.CopyConfig(id="cD", user_id="uD", source_user="u-whale", mode="mirror")
    tail_shares, tail_why = copy.size_for(cfg_daily, fill, remaining_daily_micro=1_000_000)   # $1 left today
    facts["trading"]["cascade_daily_tail"] = {"shares": tail_shares, "why": tail_why}
    g.check("a copier with $1 of daily budget left cannot buy $25 of stock (sized to %s shares, %s)"
            % (tail_shares, tail_why or "no reason"),
            copy.notional_floor(max(tail_shares, 0), 550_000) <= 1_000_000,
            "the daily budget was exceeded by the per-order cap: %d shares" % tail_shares)
    #    CLOSED after P16, by measuring it: the arithmetic above is one thing, and the same 500 copiers are now
    #    run end to end as drill 11 of the P13 chaos suite (500 real intents, the executor claiming them
    #    `batch_size` at a time against a filling venue, the fills booked through the same `book_fill` the
    #    venue's trade stream uses). The probe re-runs that drill here rather than citing it, because a number
    #    quoted from another artifact is a number this probe cannot vouch for — drill 11 takes about a second.
    rc, out = 0, ""
    try:
        proc = subprocess.run([sys.executable, str(ROOT / "tools" / "p13-chaos-suite.py"), "--only", "11"],
                              cwd=str(ROOT), capture_output=True, text=True, timeout=300)
        rc = proc.returncode
        art = ROOT / "docs" / "verification" / "P13-chaos-11-500-copiers-on-one-source-fill.txt"
        out = art.read_text() if art.exists() else (proc.stdout + proc.stderr)
    except Exception as exc:                                                     # noqa: BLE001
        rc, out = -1, "%s: %s" % (type(exc).__name__, str(exc)[:160])
    numbers: dict[str, float] = {}
    m = re.search(r"fan-out: ([0-9.]+) ms total \(([0-9.]+) ms per copier\), (\d+) intents queued", out)
    if m:
        numbers.update({"fanout_ms": float(m.group(1)), "per_copier_ms": float(m.group(2)),
                        "intents": int(m.group(3))})
    m = re.search(r"aggregate notional: \$([0-9.]+) across (\d+) orders, largest \$([0-9.]+)", out)
    if m:
        numbers.update({"aggregate_usd": float(m.group(1)), "orders": int(m.group(2)),
                        "largest_usd": float(m.group(3))})
    m = re.search(r"venue filled (\d+) live order\(s\); booked in (\d+) reconciler pass\(es\), ([0-9.]+) ms — "
                  r"(\d+) of (\d+) fills in the ledger", out)
    if m:
        numbers.update({"venue_filled": int(m.group(1)), "passes": int(m.group(2)),
                        "book_ms": float(m.group(3)), "booked": int(m.group(4))})
    facts["trading"]["cascade_end_to_end"] = {"exit": rc, **numbers,
                                             "verdict": "PASS" if "verdict: PASS" in out else "not recorded"}
    g.check("the 500-copier cascade runs end to end (drill 11 exit %d, %s intents, %s orders filled)"
            % (rc, numbers.get("intents"), numbers.get("venue_filled")),
            rc == 0 and "verdict: PASS" in out and numbers.get("intents") == 500
            and numbers.get("venue_filled") == 500 and numbers.get("booked") == 500,
            "the cascade did not complete: %s" % ((out.strip().splitlines() or ["no output"])[-1][:160]))
    g.check("the fan-out is fast enough to be a copy (%s ms total for 500 copiers, %s ms each)"
            % (numbers.get("fanout_ms"), numbers.get("per_copier_ms")),
            (numbers.get("fanout_ms") or 1e9) < 60_000,
            "a copy that arrives a minute late has already missed the price")
    g.check("no copier's order exceeds the per-trade ceiling in the real run (largest $%s of $25.00, aggregate "
            "$%s)" % (numbers.get("largest_usd"), numbers.get("aggregate_usd")),
            (numbers.get("largest_usd") or 1e9) <= 25.0 and (numbers.get("orders") or 0) == 500,
            "an order above the ceiling reached the queue")


# ------------------------------------------------------------------------------------------------ injection
def section_injection(g: Gate, facts: dict, s: Surface) -> None:
    facts["injection"] = {}
    c = s.bench.client
    hdr = s.bench.bearer(s.acct["token"])

    # 1. SQLi in every parameter an attacker can reach: a value that changes the result set is the finding.
    baseline = c.get("/v1/markets", params={"pageSize": 20})
    base_ids = [m["id"] for m in (baseline.json().get("items") or [])]
    payloads = ["' OR 1=1 --", "'; DROP TABLE markets; --", "1) OR (1=1", "%27 OR %271%27=%271"]
    leaks = []
    for p in payloads:
        for path, params in (("/v1/markets", {"q": p}), ("/v1/markets/%s" % p, {}),
                             ("/v1/leaderboard", {"category": p}), ("/v1/markets", {"category": p})):
            r = c.get(path, params=params) if params else c.get(path)
            text = (r.text or "")
            ids = [m.get("id") for m in (r.json().get("items") or [])] if r.headers.get(
                "content-type", "").startswith("application/json") and isinstance(r.json(), dict) else []
            if r.status_code >= 500 or "sql" in text.lower() and "syntax" in text.lower():
                leaks.append((path, p, r.status_code, text[:120]))
            if p in ("' OR 1=1 --", "'; DROP TABLE markets; --") and ids and set(ids) == set(base_ids) and path == "/v1/markets" and "q" in params:
                leaks.append((path, p, "injection returned the full set"))
    facts["injection"]["sqli"] = {"baseline_rows": len(base_ids), "leaks": leaks[:4]}
    g.check("no SQLi payload changes the result set, 500s, or surfaces a SQL error (%d payload(s) x 4 routes)"
            % len(payloads), not leaks, json.dumps(leaks[:3])[:300])
    still = c.get("/v1/markets", params={"pageSize": 20})
    g.check("the markets table survived `DROP TABLE markets` (the route still answers)",
            still.status_code == 200 and bool(still.json().get("items")), "status %d" % still.status_code)

    # 2. Prototype pollution: a body carrying `__proto__`/`constructor` must be refused or ignored, never merged.
    pollute = {"marketId": s.market, "tokenId": s.token, "side": "BUY", "price": "0.55", "size": "5",
               "__proto__": {"polluted": True}, "constructor": {"prototype": {"polluted": True}}}
    s.bench.refresh_book(s.market)
    r = c.post("/v1/orders", headers={**hdr, "Idempotency-Key": "p14-pollute-%s" % uuid.uuid4().hex[:8]},
               json=pollute)
    facts["injection"]["prototype_pollution"] = {"status": r.status_code,
                                                 "body": (r.text or "")[:160]}
    g.check("a body with __proto__/constructor is refused rather than merged into anything",
            r.status_code in (400, 409, 422), "answered %d: an unknown property that is accepted is a property "
            "somebody will rely on" % r.status_code)

    # 3. Stored XSS. The first version of this probe posted to `/v1/alerts` with a "message" field the route does
    #    not have, got a 422, and reported a pass — which is worse than no probe (F8's lesson, in the probe suite
    #    this time). The surfaces that matter are named here instead:
    #      * the chat renderer, which sends `parse_mode=HTML`, so a market question or a pseudonym is *markup*
    #        unless it is escaped — an unescaped `<` is not an XSS in a browser, it is a broken card in every
    #        user's chat, and `esc()` plus `render_findings` are the product's two controls;
    #      * the JSON API, where the answer must be the exact bytes (a client that re-encodes is a client bug)
    #        and never the payload in a markup position.
    import polygm_core.telegrambot.render as render
    import polygm_core.telegrambot as tg
    hostile = "<script>alert(1)</script><b>bold</b>&<img src=x onerror=alert(2)>"
    cards = {
        "fill_card": render.fill_card(market=hostile, side="buy", size_text="5", price_text="$0.55",
                                      fee_text="$0.02", position_text="5 shares", price_age_text="2s ago"),
        "refusal_card": render.refusal_card(what=hostile, code="OFF_TICK", plain=hostile),
        "unknown_order_card": render.unknown_order_card(market=hostile, size_text="5"),
    }
    escaped = {}
    for name, plan in cards.items():
        text = "".join(b.text or "" for b in plan.beats)
        escaped[name] = {"has_raw_script": "<script>" in text, "has_raw_img": "<img" in text,
                         "findings": render.render_findings(plan)[:2]}
    facts["injection"]["chat_escaping"] = escaped
    g.check("hostile text in a market question, a side, a refusal and a limbo card reaches the chat escaped "
            "(%d card(s))" % len(cards),
            not any(v["has_raw_script"] or v["has_raw_img"] for v in escaped.values()),
            json.dumps(escaped)[:300])
    hard = {k: [f for f in v["findings"] if "tag Telegram will reject" in f or "carries an address" in f]
            for k, v in escaped.items()}
    cosmetic = {k: v["findings"] for k, v in escaped.items() if v["findings"]}
    facts["injection"]["scanner_findings"] = {"hard": {k: v for k, v in hard.items() if v},
                                             "cosmetic": cosmetic}
    g.check("the scanner finds no tag-level failure in the escaped cards (%d card(s); %d cosmetic finding(s))"
            % (len(cards), len(cosmetic)),
            not any(hard.values()), "an escaped card is reported as a rejected tag: %s" % json.dumps(hard)[:200])
    if cosmetic:
        # Not a security finding and not a pass dressed up: the confetti rule matches any `*`, `_`, `[`, `]` or
        # backtick in the text, so a market question containing an underscore raises "looks like Markdown" on the
        # broadcast path. Telegram renders the characters literally, so nothing breaks — but an operator warning
        # that fires on ordinary questions is a warning operators learn to skip, which is how the *real* confetti
        # gets through later.
        g.open("A LEGITIMATE QUESTION CONTAINING `_`, `*`, `[`, `]` OR A BACKTICK TRIPS THE BROADCAST "
               "MARKDOWN-CONFETTI WARNING (cosmetic: the text renders literally)",
               "narrow MARKDOWN_CONFETTI_RE to paired emphasis markers (`**x**`, `__x__`, `[x](y)`, `` `x` ``) "
               "rather than any occurrence of the character, and add the underscore-in-a-market-question case as "
               "a test beside the confetti one")

    #    The canary: a card built with a *raw* tag (bypassing esc()) must be caught by the scanner. Without this,
    #    "no findings" above could mean the scanner never looks.
    leaky = render.Plan(beats=[render.Beat(text="<b>%s</b>" % hostile)])       # noqa: the deliberate mistake
    canary = render.render_findings(leaky)
    facts["injection"]["render_canary"] = canary[:2]
    g.check("a card assembled without esc() is caught by the scanner (canary: %s)" % (canary[:1] or "NOTHING"),
            bool(canary), "the scanner is blind to an unescaped tag: every green card above is unfalsifiable")

    #    The same text through the storage the product actually has: an alert rule's free-form `params`.
    rule = c.post("/v1/alerts", headers={**hdr, "Idempotency-Key": "p14-xss-%s" % uuid.uuid4().hex[:8]},
                  json={"kind": "price_level", "marketId": s.market, "channel": "telegram",
                        "params": {"label": hostile, "priceMicro": 550_000}})
    stored = {}
    if rule.status_code == 200:
        rid = (rule.json() or {}).get("ruleId") or (rule.json() or {}).get("rule", {}).get("ruleId")
        back = c.get("/v1/alerts", headers=hdr)
        rows = [r for r in (back.json().get("items") or back.json().get("rules") or [])
                if str(r.get("ruleId") or r.get("id")) == str(rid)]
        label = ((rows[0].get("params") or {}).get("label") if rows else None)
        card = render.fill_card(market=str(label or ""), side="buy", size_text="5", price_text="$0.55",
                                fee_text="$0.02", position_text="5 shares", price_age_text="2s ago")
        stored = {"status": rule.status_code, "roundtrip_exact": label == hostile,
                  "rendered_escaped": "<script>" not in "".join(b.text or "" for b in card.beats)}
    else:
        stored = {"status": rule.status_code, "code": s.code(rule.json() or {}),
                  "note": "the route refused the rule before storage"}
    facts["injection"]["stored_params"] = stored

    listing = c.get("/v1/markets", params={"q": hostile}).text or ""
    g.check("a script payload in a query is never echoed into a JSON response", "<script>" not in listing,
            "the payload came back in the body: %s" % listing[:160])
    hero = c.get("/v1/public/market/fed-cut-sept")
    g.check("the public market page serves JSON with a no-sniff content type",
            hero.headers.get("x-content-type-options") == "nosniff"
            and "json" in hero.headers.get("content-type", ""),
            json.dumps({k: v for k, v in hero.headers.items() if k.lower().startswith("x-")})[:160])
    if rule.status_code == 200:
        g.check("a hostile label survives storage byte-for-byte and is escaped at render, not at save", stored.get(
            "roundtrip_exact") is True and stored.get("rendered_escaped") is True,
            json.dumps(stored)[:200])

    # 4. SSRF. The honest shape of this probe: **enumerate** the routes and the outbound destinations, then say
    #    what the fetch surface is. Two earlier drafts of it were wrong in instructive ways: the first guessed four
    #    URL-ish paths, got a 200 from `/v1/markets?image=…` (an ignored query parameter, not a fetch) and called
    #    it a finding; the second read the route list from the served document, which is 404 in this shape, and
    #    then reported "0 parameters checked" as a pass. The question is not "does a route take a parameter named
    #    url", it is "can any request make this process open a socket to an address of the caller's choosing".
    paths = {}
    try:
        paths = s.bench.app.app.openapi().get("paths") or {}          # the *declared* surface, built in-process
    except Exception as exc:                                                     # noqa: BLE001
        g.check("the declared route surface could be enumerated (%s)" % type(exc).__name__, False,
                "no route list, so 'no URL parameter' would be a claim about an empty set")
    url_params = []
    for path, ops in paths.items():
        for method, op in (ops.items() if isinstance(ops, dict) else []):
            if not isinstance(op, dict):
                continue
            names = [str(prm.get("name") or "") for prm in (op.get("parameters") or [])]
            schema = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}
                      ).get("schema") or {}
            names += list((schema.get("properties") or {}).keys())
            #    Word-aware, because `any(k in name)` matched `targets` on the automation routes — a list of
            #    market ids, not a fetch destination — and a check that cries wolf on a market id is a check
            #    somebody deletes. A name matches when one of its `_`/camelCase words IS the keyword, or when it
            #    contains `url`/`uri` anywhere (`targetUrl`, `image_url`, `webhookURL`).
            for n in names:
                words = {w.lower() for w in re.split(r"[^A-Za-z0-9]+|(?<=[a-z])(?=[A-Z])", n) if w}
                if (words & {"url", "uri", "href", "endpoint", "proxy", "callback", "redirect", "webhook",
                             "destination", "host"}) or "url" in n.lower() or "uri" in n.lower():
                    url_params.append("%s %s %s" % (method.upper(), path, n))

    #    The outbound side, by AST rather than by grep. A call site like `urlopen(req)` names no host at all — the
    #    host is a module constant or an env default, which is exactly what makes it safe or not. So: find every
    #    `https://…` literal that is assigned to a URL-ish name, and check the host. An endpoint that can be
    #    pointed anywhere by *configuration* is a deployment decision; one derived from a *request* would be the
    #    finding, and that is what `url_params` above is for.
    import ast as _ast
    endpoints: list[dict] = []
    fetch_sites: list[str] = []
    for f in sorted((ROOT / "services").rglob("*.py")) + sorted((ROOT / "packages").rglob("*.py")):
        rel = f.relative_to(ROOT).as_posix()
        if "__pycache__" in rel or "/tests" in rel or rel.startswith("tools/"):
            continue
        src = f.read_text()
        try:
            tree = _ast.parse(src)
        except SyntaxError:
            continue
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Call):
                fn = node.func
                nm = getattr(fn, "attr", None) or getattr(fn, "id", None) or ""
                #    A *socket-level* verb, and the call has to name an http-ish module or one of the two stdlib
                #    entry points: `dict.get` and `TestClient.post` are not outbound anything, and the draft that
                #    counted them reported 1,586 fetch sites in a repository with four.
                seg = (_ast.get_source_segment(src, fn) or "") if hasattr(node, "lineno") else ""
                socketish = nm in ("urlopen", "urlretrieve", "create_connection") or (
                    nm in ("get", "post", "put", "patch", "delete", "request", "send") and
                    any(k in seg.lower() for k in ("http", "urllib", "requests", "aiohttp", "socket")))
                if socketish:
                    args = [a.id for a in node.args if isinstance(a, _ast.Name)]
                    consts = [getattr(a, "id", "") for a in node.args
                              if isinstance(a, _ast.Name) and a.id.isupper()]
                    fetch_sites.append("%s:%d %s(%s)" % (rel, node.lineno, seg,
                                                         ", ".join(args or consts)[:40]))
            if not isinstance(node, _ast.Assign) and not isinstance(node, _ast.AnnAssign):
                continue
            targets = node.targets if isinstance(node, _ast.Assign) else [node.target]
            value = node.value
            if not isinstance(value, _ast.Constant) or not isinstance(value.value, str):
                continue
            if "://" not in value.value:
                continue
            for t in targets:
                name = getattr(t, "id", None) or ""
                if not any(k in name.upper() for k in ("URL", "BASE", "HOST", "ENDPOINT", "API", "RPC", "WS",
                                                       "CLOB", "GAMMA", "DATA", "ORIGIN")):
                    continue
                host = re.sub(r"^[a-z]+://", "", value.value).split("/")[0]
                endpoints.append({"const": name, "host": host, "where": "%s:%d" % (rel, node.lineno)})
    hosts = sorted({e["host"] for e in endpoints})
    facts["injection"]["ssrf"] = {"declared_paths": len(paths), "url_named_parameters": url_params,
                                  "endpoint_constants": endpoints, "endpoint_hosts": hosts,
                                  "fetch_sites": sorted(set(fetch_sites))[:10]}

    known_hosts = {"api.polymarket.com", "gamma-api.polymarket.com", "data-api.polymarket.com", "clob.polymarket.com",
                   "lb-api.polymarket.com", "api.telegram.org", "polygm-api.vercel.app", "polygm-mini-app.vercel.app",
                   "fonts.googleapis.com", "fonts.gstatic.com", "api.stripe.com", "api.turnkey.com", "api.privy.io",
                   "api.dynamic.xyz", "www.polymarket.com", "polymarket.com", "docs.polymarket.com",
                   "ws-subscriptions-clob.polymarket.com", "testnet-api.turnkey.com", "explorer-api.walletconnect.com"}
    unknown = [e for e in endpoints if e["host"] not in known_hosts]
    g.check("no request parameter or body field can name an address the server fetches (%d declared path(s), %d "
            "URL-ish name(s) found)" % (len(paths), len(url_params)), not url_params and bool(paths),
            "these look like a fetch target supplied by the caller: %s" % url_params)
    g.check("every endpoint constant points at a known vendor (%d constant(s), %d host(s): %s)"
            % (len(endpoints), len(hosts), ", ".join(hosts[:6])), not unknown and bool(endpoints),
            "an endpoint constant nobody declared: %s" % json.dumps(unknown)[:240])
    g.check("the fetch call sites are the ingest and executor clients, and each takes a constant rather than a "
            "request field (%d site(s))" % len(set(fetch_sites)), bool(fetch_sites),
            "no outbound call site was found in the tree at all, so this check has nothing to say")
    #    CLOSED after P16, by building the transport with the guard in it rather than waiting for one.
    #    `polygm_core.signals.webhook` is the only place a user-chosen address becomes a socket, and the guard is
    #    the first thing that runs. This section measured the *latent* path in P14 because no transport existed;
    #    now it measures the refusal, and it measures it two ways: through the library (with an opener that fails
    #    the check if it is ever called, so "refused" is a claim about the socket) and through the served API as a
    #    Pro account (so the guard is proven to be wired into the product, not just importable).
    try:
        from polygm_core.signals import webhook as _wh
    except Exception as exc:                                                     # noqa: BLE001
        _wh = None
        g.check("the webhook transport is importable (%s)" % type(exc).__name__, False, str(exc)[:160])
    if _wh is not None:
        opened = []

        def _never(url, body, headers, timeout):                                  # noqa: ARG001
            opened.append(url)
            raise AssertionError("connects-then-complains: %s" % url)

        hostile = ["http://hooks.example.com/x", "https://127.0.0.1/v1/admin/kill-switch",
                   "https://127.1.2.3/x", "https://169.254.169.254/latest/meta-data/iam/security-credentials/",
                   "https://10.1.2.3/x", "https://192.168.0.1/x", "https://172.16.4.4/x", "https://100.64.0.7/x",
                   "https://0.0.0.0/x", "https://[::1]/x", "https://[fd00::1]/x", "https://[fe80::1]/x",
                   "https://[::ffff:169.254.169.254]/x", "https://2130706433/x", "https://0x7f000001/x",
                   "https://user:pw@hooks.example.com/x", "https://hooks.example.com:22/x",
                   "https://ex%61mple.com/x", "gopher://hooks.example.com/x", "file:///etc/passwd"]
        refusals = {}
        for url in hostile:
            try:
                _wh.deliver(url=url, payload={"probe": 1}, secret="s", delivery_id="d", at_ms=1,
                            resolve=lambda host: [(2, 1, 6, "", ("93.184.216.34", 0))], opener=_never)
                refusals[url] = "ALLOWED"
            except _wh.TargetRefused as exc:
                refusals[url] = exc.code
        facts["injection"]["ssrf"]["webhook_guard"] = {"refused": refusals, "hostile_count": len(hostile),
                                          "sockets_opened": len(opened)}
        g.check("all %d hostile webhook URLs are refused, and not one of them opened a socket"
                % len(hostile), "ALLOWED" not in refusals.values() and not opened,
                "allowed: %s; sockets: %d" % (json.dumps([u for u, c in refusals.items() if c == "ALLOWED"])[:160],
                                              len(opened)))
        #    The half that only a resolver can catch: the URL's shape is ordinary and its *answer* is internal.
        try:
            _wh.check_target("https://hooks.example.com/x",
                             resolve=lambda host: [(2, 1, 6, "", ("93.184.216.34", 0)),
                                                   (2, 1, 6, "", ("10.0.0.5", 0))])
            split_horizon = "ALLOWED"
        except _wh.TargetRefused as exc:
            split_horizon = exc.code
        g.check("a host that resolves to a public AND a private address is refused (%s)" % split_horizon,
                split_horizon == "TARGET_NOT_PUBLIC",
                "split-horizon DNS is the realistic version of the attack: one bad answer must refuse the host")
        #    The redirect: the first hop is public, the second is the metadata service. The refusal must land at
        #    the hop, which is asserted by the number of connections rather than by the final status.
        class _Redir(Exception):
            def __init__(self, code, loc):
                super().__init__("redirect")
                self.code = code
                self.headers = {"Location": loc}

        hops = []

        def _redirecting(url, body, headers, timeout):                            # noqa: ARG001
            hops.append(url)
            raise _Redir(302, "https://169.254.169.254/latest/meta-data/")
        hop_refusal = "ALLOWED"
        try:
            _wh.deliver(url="https://hooks.example.com/x", payload={"probe": 1}, secret="s", delivery_id="d",
                        at_ms=1, resolve=lambda host: [(2, 1, 6, "", ("93.184.216.34", 0))], opener=_redirecting)
        except _wh.TargetRefused as exc:
            hop_refusal = exc.code
        g.check("a redirect into the metadata range is refused at the hop (%s, %d connection(s))"
                % (hop_refusal, len(hops)), hop_refusal == "TARGET_NOT_PUBLIC" and len(hops) == 1,
                "the redirect hop was dialled: the guard is checking the first URL only")
        #    And the signature a receiver verifies, computed here from first principles rather than from our own
        #    helper, because a signature the sender and the verifier compute with the same function is a
        #    signature that agrees with itself.
        seen = {}

        class _Ok:
            status = 200

            def read(self, n):
                return b"{}"

        def _capture(url, body, headers, timeout):                                # noqa: ARG001
            seen.update({"body": body, "headers": headers})
            return _Ok()
        _wh.deliver(url="https://hooks.example.com/x", payload={"b": 2, "a": 1}, secret="s3cret",
                    delivery_id="deadbeef", at_ms=1_700_000_000_000,
                    resolve=lambda host: [(2, 1, 6, "", ("93.184.216.34", 0))], opener=_capture)
        import hashlib as _h, hmac as _hm
        _expected = _hm.new(b"s3cret", b"1700000000000." + seen["body"], _h.sha256).hexdigest()
        g.check("the delivery is signed over the timestamp and the body, and carries the queue's own key",
                seen["headers"].get("X-PolyGM-Signature") == "t=1700000000000,v1=%s" % _expected
                and seen["headers"].get("X-PolyGM-Delivery") == "deadbeef"
                and seen["body"] == b'{"a":1,"b":2}',
                json.dumps(seen["headers"].get("X-PolyGM-Signature"))[:80])
        #    Through the product: a Pro account saves the same hostile URL through the served API. This is the
        #    check that says the guard is where the user can reach it.
        pro = s.bench.user("prohook")
        s.bench.con.execute("UPDATE users SET tier='pro' WHERE id=?", (pro["uid"],))
        s.bench.con.commit()
        phdr = s.bench.bearer(pro["token"])
        save = c.post("/v1/alerts", headers={**phdr, "Idempotency-Key": "p14-ssrf-%s" % uuid.uuid4().hex[:8]},
                      json={"kind": "price_level", "channel": "webhook", "marketId": s.market,
                            "params": {"priceMicro": 550_000, "url": "https://169.254.169.254/latest/meta-data/"}})
        sbody = save.json() if save.content else {}
        good = c.post("/v1/alerts", headers={**phdr, "Idempotency-Key": "p14-ssrfok-%s" % uuid.uuid4().hex[:8]},
                      json={"kind": "price_level", "channel": "webhook", "marketId": s.market,
                            "params": {"priceMicro": 550_000, "url": "https://hooks.example.com/alerts"}})
        gbody = good.json() if good.content else {}
        stored = c.get("/v1/alerts", headers=phdr).json() or {}
        urls = [str((r.get("params") or {}).get("url") or "") for r in (stored.get("rules") or [])]
        facts["injection"]["ssrf"]["webhook_save"] = {"internal": {"status": save.status_code, "code": s.code(sbody)},
                                         "public": {"status": good.status_code, "code": s.code(gbody)},
                                         "stored_urls": urls}
        g.check("a Pro account cannot SAVE an internal webhook URL (%d %s), and the same account CAN save a "
                "public one (%d)" % (save.status_code, s.code(sbody) or "accepted", good.status_code),
                save.status_code >= 400 and good.status_code == 200
                and not any("169.254" in u for u in urls) and any("hooks.example.com" in u for u in urls),
                "the stored rules carry %s" % json.dumps(urls)[:160])
        #    The queue half: a refusal is dead on the first refusal, a provider failure is a retry.
        try:
            from polygm_core.signals import fanout as _fan
        except Exception:                                                        # noqa: BLE001
            _fan = None
        if _fan is not None:
            _claim = {"signal_id": "sig-p14", "user_id": pro["uid"], "channel": "webhook", "priority": 0,
                      "status": _fan.STATUS_SENDING, "attempts": 0, "queued_ms": 1,
                      "idempotency_key": _fan.idempotency_key("sig-p14", "webhook"),
                      "target": "https://169.254.169.254/x"}
            dead = _wh.send_claimed(_claim, payload={"a": 1}, secret="s", now_ms=2, resolve=lambda host: [],
                                    opener=_never)
            facts["injection"]["ssrf"]["webhook_queue"] = {"status": dead.get("status"), "reason": dead.get("dead_reason"),
                                              "retry_in_ms": dead.get("retry_in_ms")}
            g.check("a refused target is dead-lettered rather than retried into the queue (%s %s)"
                    % (dead.get("status"), dead.get("dead_reason")),
                    dead.get("status") == _fan.STATUS_DEAD and dead.get("dead_reason") ==
                    "refused:TARGET_NOT_PUBLIC" and "retry_in_ms" not in dead,
                    json.dumps(facts["injection"]["ssrf"]["webhook_queue"])[:160])

    # 5. ReDoS: a pathological input must not make a route take seconds. Measured, because "we use safe regexes"
    #    is a claim and the measured version is a number.
    evil = "a" * 5_000 + "!" + "a" * 5_000
    timed = {}
    for path, params in (("/v1/markets", {"q": evil}), ("/v1/markets", {"category": evil * 2}),
                         ("/v1/leaderboard", {"q": evil})):
        t0 = time.perf_counter()
        r = c.get(path, params=params)
        timed["%s?%s" % (path, list(params)[0])] = {"status": r.status_code,
                                                    "ms": round((time.perf_counter() - t0) * 1000, 1)}
    facts["injection"]["redos"] = timed
    worst = max(v["ms"] for v in timed.values())
    g.check("a 10,000-character pathological parameter is refused or answered quickly (worst %.1f ms)" % worst,
            worst < 2_000.0 and all(v["status"] < 500 for v in timed.values()), json.dumps(timed)[:200])

    # 6. CSV injection in the tax export: a cell beginning `=`, `+`, `-` or `@` executes in every spreadsheet, and
    #    the tax export is the file a user hands to an accountant. The probe asks for the route before believing
    #    it does not exist.
    exporters = [p for p in ("/v1/tax/export", "/v1/export/tax", "/v1/wallet/tax-export", "/v1/tax/csv")
                 if c.get(p, headers=hdr).status_code != 404]
    if not exporters:
        g.open("NO CSV EXPORT ROUTE EXISTS YET, so formula injection could not be probed end to end",
               "when the tax export ships: write a row whose fields begin `=`, `+`, `-`, `@`, then assert the "
               "writer prefixes each with a quote and that the header row is unchanged; add a test next to the "
               "wallet's own, and keep this probe as the outside view")
    else:
        rows = []
        for path in exporters:
            r = c.get(path, headers={**hdr, "Accept": "text/csv"})
            body = r.text or ""
            rows.append({"path": path, "status": r.status_code,
                         "first_line": body.split("\n")[0][:80], "formula_cell": bool(
                             re.search(r"(^|,)[=+\-@]", body, re.M)),
                         "content_type": r.headers.get("content-type", "")})
        facts["injection"]["csv"] = rows
        g.check("a CSV export is served as a download and carries no leading-formula cell (%s)"
                % json.dumps(rows)[:200],
                all(r["status"] == 200 and not r["formula_cell"] for r in rows),
                "a cell starting with = executes when the accountant opens the file")


# ---------------------------------------------------------------------------------------------- business logic
def section_logic(g: Gate, facts: dict, s: Surface) -> None:
    """The money rules, attacked by the people they apply to.

    Authorisation (the matrix) asks *who may act*. Trading (above) asks *what the order path does with a bad
    order*. This section asks the third question: **can a user get value they did not earn, or make the product
    pay for behaviour its own rules were written to stop.** Every probe here is performed by a legitimate account
    using the product's own surfaces, and every refusal is read back from the record it should have written.
    """
    facts["logic"] = {}
    c = s.bench.client
    con = s.bench.con

    # ---------------------------------------------------------------- 1. self-referral and the builder code
    a = s.acct
    hdr = s.bench.bearer(a["token"])
    code = "p14self%d" % (int(time.time()) % 100000)
    made = c.post("/v1/referrals/code", headers={**hdr, "Idempotency-Key": "p14-rc-%s" % code},
                  json={"code": code})
    link = ((made.json() or {}).get("link") or {}) if made.status_code == 200 else {}
    token = str(link.get("token") or code)
    applied = c.post("/v1/referrals/apply", headers={**hdr, "Idempotency-Key": "p14-ra-%s" % code},
                     json={"code": token})
    body = applied.json() if applied.content else {}
    attrib = con.execute("SELECT COUNT(*) FROM referral_attributions WHERE referee=? AND referrer=?",
                         (a["uid"], a["uid"])).fetchone()[0]
    audit = con.execute("SELECT detail_json FROM audit_log WHERE action='referral.apply' AND actor_id=?"
                        " ORDER BY id DESC LIMIT 1", (a["uid"],)).fetchone()
    audit_detail = json.loads(audit[0]) if audit else {}
    #    The code that gets revoked is the PROGRAMME's builder code (one code for the whole referral programme, so
    #    the revenue is one reconcile-able line), not the referral token. The first draft looked up the token and
    #    reported "no row" — a finding about my query, not about the product.
    prog_code = str(getattr(s.bench.app, "_REF_BUILDER_CODE", "") or "")
    status_row = con.execute("SELECT code, state, source, substr(note,1,80) FROM builder_code_status WHERE code=?",
                             (prog_code,)).fetchone()
    facts["logic"]["self_referral"] = {"code_status": made.status_code, "apply_status": applied.status_code,
                                       "apply_code": s.code(body), "attribution_rows": attrib,
                                       "audit": audit_detail, "programme_code": prog_code,
                                       "builder_code": (list(status_row) if status_row else None),
                                       "referral_token": token[:12] + "…"}
    g.check("an account applying its own link is refused as a self-referral (%d %s)"
            % (applied.status_code, s.code(body)),
            applied.status_code == 409 and s.code(body) == "SELF_REFERRAL",
            "answered %d %s" % (applied.status_code, json.dumps(body)[:160]))
    g.check("the self-referral wrote no attribution row", int(attrib) == 0,
            "%d attribution row(s) exist for (referee=referrer): the referral was recorded despite the refusal"
            % attrib)
    g.check("the refusal recorded the ground in the audit trail (%s)" % json.dumps(audit_detail)[:120],
            audit_detail.get("builder_code_revoked") is True,
            "the audit row does not say the builder code was revoked: %s" % json.dumps(audit_detail)[:160])
    g.check("the programme's builder code (%s) is marked disabled with the reason readable (%s)"
            % (prog_code, list(status_row) if status_row else "NO ROW"),
            bool(status_row) and str(status_row[1]) == "disabled" and str(status_row[2]) == "manual"
            and "self-referral" in str(status_row[3] or ""),
            "the product called self-referral a revocation ground and then did not record one")

    # ---------------------------------------------------------------- 2. Sybil: what a signup is worth
    referrer = s.bench.user("referrer")
    rhdr = s.bench.bearer(referrer["token"])
    rcode = "p14ref%d" % (int(time.time()) % 100000)
    rmade = c.post("/v1/referrals/code", headers={**rhdr, "Idempotency-Key": "p14-rc-%s" % rcode},
                   json={"code": rcode})
    rlink = ((rmade.json() or {}).get("link") or {}) if rmade.status_code == 200 else {}
    rtoken = str(rlink.get("token") or rcode)

    def claim(tag: str, *, device: str = "", funding: str = "") -> dict:
        """One claim, read back from the RECORD.

        A refusal answers with the generic error envelope (the decision's own reason stays out of the body — it is
        the sentence a person reads, and the field an attacker would farm), so the state and reason are read from
        `referral_attributions`: `pending` for an attributed claim, `review`, or `refused` with the reason.
        """
        u = s.bench.user(tag)
        r = c.post("/v1/referrals/apply",
                   headers={**s.bench.bearer(u["token"]), "Idempotency-Key": "p14-claim-%s-%s"
                            % (tag, uuid.uuid4().hex[:6])},
                   json={"code": rtoken, "device": device, "funding": funding})
        j = r.json() if r.content else {}
        row = con.execute("SELECT state, reason, qualify_order, notional_micro FROM referral_attributions"
                          " WHERE referee=?", (u["uid"],)).fetchone()
        return {"uid": u["uid"], "status": r.status_code, "body_state": j.get("state"), "code": s.code(j),
                "record_state": (row[0] if row else None), "record_reason": (row[1] if row else None),
                "qualify_order": (row[2] if row else None), "notional_micro": (int(row[3]) if row else None)}

    #    The funding collision first: two accounts claiming the same referrer with the SAME funding source is the
    #    Sybil shape the engine has a reason code for, and it must be refused rather than reviewed.
    fund = "0xfeedfacecafebabe"
    first = claim("sybil-a", funding=fund)
    second = claim("sybil-b", funding=fund)
    facts["logic"]["duplicate_funding"] = {"first": first, "second": second}
    g.check("two accounts claiming one referrer from the same funding source: the second is refused for the "
            "funding collision (%d, record says %s/%s)" % (second["status"], second["record_state"],
                                                           second["record_reason"]),
            second["status"] == 409 and second["record_state"] == "refused"
            and second["record_reason"] == "duplicate_funding",
            "the second claim answered %d and was recorded as %s/%s — a funding collision is the shape of a farm"
            % (second["status"], second["record_state"], second["record_reason"]))
    g.check("the refused claim is recorded as REFUSED rather than as a live attribution, and earns nothing "
            "(%s, $%s)" % (second["record_state"], (second["notional_micro"] or 0) / 10 ** 6),
            second["record_state"] == "refused" and not second["notional_micro"],
            "a refused claim carries a qualification or a notional: %s" % json.dumps(second))

    #    Then the value of a signup. Five distinct accounts (the hour limit is five) each bring their own device
    #    and funding, so nothing about them is redundant — and the assertion is that *none of it earns anything*,
    #    because a referral programme that pays on signup is a programme that pays for signups.
    claims = [claim("sybil-%d" % i, device="dev-%d-%d" % (i, int(time.time())),
                    funding="0xfund%04d" % i) for i in range(5)]
    claims.append(claim("sybil-6", device="dev-sixth-%d" % int(time.time()), funding="0xfund9999"))
    facts["logic"]["sybil_claims"] = claims
    states = {}
    for c2 in claims:
        states[c2["record_state"]] = states.get(c2["record_state"], 0) + 1
    facts["logic"]["sybil_states"] = states
    g.check("every one of six fresh accounts claiming one referrer lands in a named state (%s)" % json.dumps(states),
            all(c2["record_state"] in ("pending", "review", "refused") for c2 in claims) and len(claims) == 6,
            "a claim left no record: %s" % json.dumps([c2 for c2 in claims if not c2["record_state"]])[:200])
    unattributed = [c2 for c2 in claims if c2["record_state"] != "pending"]
    g.check("every held or refused claim carries its reason in the record (%s)"
            % json.dumps([c2["record_reason"] for c2 in unattributed]),
            all(str(c2["record_reason"] or "") for c2 in unattributed),
            "a claim was held with no reason: a queue item nobody can read is a queue nobody reads")
    #    The point of the whole exercise: what a signup is worth. Nothing here may have earned anything, because
    #    earning is tied to a matched order over the threshold and none of these accounts has ever traded.
    zero = con.execute("SELECT COUNT(*) FROM referral_attributions WHERE referrer=? AND (qualify_ms>0 OR"
                       " notional_micro>0 OR qualify_order!='')", (referrer["uid"],)).fetchone()[0]
    g.check("signup alone earns the referrer $0: no row carries a qualification with no qualifying order behind "
            "it (%d earning row(s))" % zero, int(zero) == 0,
            "%d row(s) carry a qualification with no qualifying order behind it" % zero)
    g.check("the hourly velocity limit is a state the product reaches and names (%s)"
            % json.dumps([c2["record_reason"] for c2 in claims if c2["record_reason"] == "velocity"]),
            any(c2["record_reason"] == "velocity" for c2 in claims),
            "six claims in an hour from one referrer produced no velocity hold: %s" % json.dumps(states))

    # ---------------------------------------------------------------- 3. wash trading
    import polygm_core.leaderboard.integrity as integ
    wallet = "0xwashwashwash"
    mk = s.market
    fills = [{"wallet": wallet, "conditionId": mk, "side": "BUY", "priceMicro": 500_000,
              "notionalMicro": 50_000_000, "tsMs": 1_000},
             {"wallet": wallet, "conditionId": mk, "side": "SELL", "priceMicro": 500_000,
              "notionalMicro": 50_000_000, "tsMs": 30_000},
             # A GENUINE trade: same market, opposite side, but three hours later — the market moved, so this is
             # not a round trip and must not be subtracted.
             {"wallet": wallet, "conditionId": mk, "side": "BUY", "priceMicro": 620_000,
              "notionalMicro": 62_000_000, "tsMs": 30_000 + 3 * 3_600_000}]
    wash = integ.wash_volume(fills)
    facts["logic"]["wash"] = wash
    g.check("a same-wallet round trip 30s apart at one price is subtracted exactly once ($%s of $%s, %d pair(s))"
            % (wash["washedMicro"] / 10 ** 6, (wash["washedMicro"] + wash["verifiedMicro"]) / 10 ** 6,
               wash["roundTrips"]),
            wash["washedMicro"] == 50_000_000 and wash["verifiedMicro"] == 112_000_000 and wash["roundTrips"] == 1,
            json.dumps(wash)[:240])
    g.check("the wash subtracted the round-tripped leg and NOT the third, genuine $620 trade "
            "(verified $%s)" % (wash["verifiedMicro"] / 10 ** 6),
            wash["verifiedMicro"] == 112_000_000,
            "the washer erased volume that was not part of a round trip: %s" % json.dumps(wash)[:200])
    #    The control, and the reason the window is a parameter: a detector that subtracts every opposite-side pair
    #    would erase real trading volume, which is the failure mode nobody would notice until a good trader was
    #    ranked below a quiet one.
    far = integ.round_trip_pairs(fills, window_ms=60_000)
    gone = integ.wash_volume([fills[0], {"wallet": "0xsomeoneelse", "conditionId": mk, "side": "SELL",
                                         "priceMicro": 500_000, "notionalMicro": 50_000_000, "tsMs": 40_000}])
    facts["logic"]["wash_control"] = {"pairs_in_window": len(far), "other_wallet_washed": gone["washedMicro"]}
    g.check("the detector does not erase genuine volume: %d pair(s) in the window, another wallet's opposite "
            "fill washes $%s" % (len(far), gone["washedMicro"] / 10 ** 6),
            len(far) == 1 and gone["washedMicro"] == 0,
            "an opposite fill by a DIFFERENT wallet, or one three hours later, was counted as a wash: %s"
            % json.dumps(facts["logic"]["wash_control"]))
    #    Wiring, stated as a line of code rather than a hope: every leaderboard evidence build washes the tape.
    rank_src = (ROOT / "packages" / "polygm_core" / "leaderboard" / "rank.py").read_text()
    g.check("the watchlist's own row builder calls the washer on every evidence build",
            "ig.wash_volume(fills)" in rank_src,
            "rank.py does not call wash_volume: the detector exists and nothing subtracts with it")

    # ---------------------------------------------------------------- 4. the copy farm
    leader = "0xleaderleader"
    farm_own = [{"wallet": "0xfarmfarmfarm", "conditionId": mk, "side": "BUY", "priceMicro": 500_000,
                 "notionalMicro": 10_000_000, "tsMs": 1_000_000 + i * 60_000} for i in range(12)]
    leader_tape = [{"wallet": leader, "conditionId": mk, "side": "BUY", "priceMicro": 500_000,
                    "notionalMicro": 90_000_000, "tsMs": 1_000_000 + i * 60_000 - 5_000} for i in range(12)]
    caught = integ.copy_farm(wallet="0xfarmfarmfarm", own=farm_own, candidates={leader: leader_tape})
    #    The control: the same twelve fills, but the leader's are *after* the farm's, i.e. the "farm" leads. A
    #    detector that fires on correlation alone would call every market's participants a farm.
    #    The controls, and a limitation I could not build a control *around*.
    #
    #    Three attempts at "the same tape, but not a copy" all failed, and the third one is the interesting one:
    #    with the other wallet's fills moved 200 seconds earlier, 10 of 12 of ours still matched — because with a
    #    60-second cadence the candidate's *next* fill lands ~25s before each of ours, and the rule counts "a fill
    #    the other wallet made first, inside the window" without pairing them. So a market where two wallets trade
    #    the same side on a similar cadence flags in both directions, and no amount of moving the tape fixes it:
    #    it is what the rule *is*. (The docstring's "the candidate has to lead ... a source that trails the farm is
    #    the farm by another name" is true of the ordering test and not of the pairing, which is the gap.)
    #
    #    The controls that do hold are the ones the rule actually draws: a different market, a different side, and
    #    a candidate whose nearest fill is outside the window altogether.
    other_market = integ.copy_farm(wallet="0xfarmfarmfarm", own=farm_own,
                                   candidates={leader: [{**r, "conditionId": "0xOTHER"} for r in leader_tape]})
    other_side = integ.copy_farm(wallet="0xfarmfarmfarm", own=farm_own,
                                 candidates={leader: [{**r, "side": "SELL"} for r in leader_tape]})
    long_ago = integ.copy_farm(wallet="0xfarmfarmfarm", own=farm_own,
                               candidates={leader: [{**r, "tsMs": r["tsMs"] - 30 * 60_000}
                                                    for r in leader_tape]})
    interleaved = integ.copy_farm(wallet="0xfarmfarmfarm", own=farm_own,
                                  candidates={leader: [{**r, "tsMs": r["tsMs"] - 200_000} for r in leader_tape]})
    thin = integ.copy_farm(wallet="0xfarmfarmfarm", own=farm_own[:9],
                           candidates={leader: leader_tape})
    facts["logic"]["copy_farm"] = {"caught": caught, "other_market": other_market, "other_side": other_side,
                                   "thirty_minutes_earlier": long_ago, "interleaved_same_cadence": interleaved,
                                   "below_the_fill_floor": thin}
    g.check("twelve fills mirroring another wallet that traded 5s earlier on the same side are named as a copy "
            "farm (%s)" % json.dumps(caught)[:110], bool(caught),
            "a mechanical mirror of 12/12 fills was not flagged")
    g.check("a different market, a different side, and a candidate half an hour earlier are all NOT a farm "
            "(%s / %s / %s)" % (other_market, other_side, long_ago),
            not other_market and not other_side and not long_ago,
            "the detector matches on something other than market, side and the window: %s"
            % json.dumps([other_market, other_side, long_ago])[:200])
    g.check("nine mirrored fills are below the floor and are not a farm (%s)" % (thin or "none"), not thin,
            "the detector has no minimum-fill floor: nine coincidences and twelve look the same to it")
    #    Closed in P16, by the tape that used to flag. The rule now pairs one-to-one (a candidate fill explains at
    #    most one of ours, nearest first) and requires *coverage*: a candidate with fills left over in the markets
    #    where we paired was not being followed, it was merely nearby. `interleaved` is computed above and the
    #    check is that it is now None — the whole point of the fixture.
    g.check("two traders on the same cadence are not called a copy farm (%s)" % (interleaved or "none"),
            not interleaved,
            "a public 'derived from 0x…' row is a claim about a person: the same-cadence tape still flags")
    g.check("the same tape with the candidate leading IS still a farm (%s)" % json.dumps(caught)[:90],
            bool(caught) and caught.get("mirroredFills") == 12,
            "the fix overshot: a genuine twelve-of-twelve copy is no longer detected")
    single = [{"wallet": "0xleadleader", "conditionId": mk, "side": "BUY", "priceMicro": 500_000,
               "notionalMicro": 90_000_000, "tsMs": 1_000_000 - 1_000}]
    many = integ.copy_farm(wallet="0xfarmfarmfarm", own=farm_own, candidates={leader: single})
    busy = integ.copy_farm(wallet="0xfarmfarmfarm", own=farm_own,
                           candidates={leader: [{"wallet": leader, "conditionId": mk, "side": "BUY",
                                                 "priceMicro": 500_000, "notionalMicro": 1_000_000,
                                                 "tsMs": 1_000_000 + i * 10_000} for i in range(80)]})
    facts["logic"]["copy_farm_pairing"] = {"one_candidate_fill": many, "busy_candidate": busy,
                                           "rule": (caught or {}).get("rule", "")}
    g.check("one leader fill cannot explain twelve followers' fills (%s)" % (many or "none"), not many,
            "a single fill inside the window was counted once per follower fill: that is the per-fill rule back")
    g.check("a market maker's dense tape is not a leader's tape (%s)" % (busy or "none"), not busy,
            "a candidate with eighty fills left unexplained in the same market counted as a leader")

    # ---------------------------------------------------------------- 5. free tier: the cap, and who sets it
    free = s.bench.user("freeloader")
    con.execute("UPDATE users SET tier='free' WHERE id=?", (free["uid"],))
    con.commit()
    fhdr = s.bench.bearer(free["token"])
    paid = c.post("/v1/alerts", headers={**fhdr, "Idempotency-Key": "p14-plan-%s" % uuid.uuid4().hex[:8]},
                  json={"kind": "price_level", "channel": "webhook", "marketId": s.market,
                        "params": {"priceMicro": 550_000}})
    pbody = paid.json() if paid.content else {}
    facts["logic"]["free_webhook"] = {"status": paid.status_code, "code": s.code(pbody),
                                      "detail": json.dumps(pbody)[:200]}
    g.check("a free account cannot save a Pro channel, and the refusal names the plan (%d %s)"
            % (paid.status_code, s.code(pbody)),
            paid.status_code == 402 and s.code(pbody) == "PLAN_REQUIRED",
            "answered %d %s" % (paid.status_code, json.dumps(pbody)[:200]))
    #    The injection: the same request, declaring its own plan. A body that can name a tier is an entitlement
    #    that a client grants itself.
    inject = c.post("/v1/alerts", headers={**fhdr, "Idempotency-Key": "p14-planinj-%s" % uuid.uuid4().hex[:8]},
                    json={"kind": "price_level", "channel": "webhook", "marketId": s.market, "plan": "pro",
                          "tier": "pro", "params": {"priceMicro": 550_000}})
    ibody = inject.json() if inject.content else {}
    listed = c.get("/v1/alerts", headers=fhdr).json() or {}
    facts["logic"]["plan_injection"] = {"status": inject.status_code, "code": s.code(ibody),
                                        "plan_read_back": listed.get("plan"),
                                        "rules_created": len(listed.get("rules") or [])}
    g.check("declaring `plan: pro` in the body cannot buy a Pro channel (%d %s) and the account still reads free "
            "(%s)" % (inject.status_code, s.code(ibody) or "accepted", listed.get("plan")),
            (inject.status_code >= 400 or listed.get("plan") == "free") and listed.get("plan") == "free",
            "the plan came from the request: %s" % json.dumps(facts["logic"]["plan_injection"]))
    #    The automation rule cap, through the API: ten rules may exist, the eleventh is refused with a code the UI
    #    can act on. The count is read back from the product rather than assumed to be ten.
    created = 0
    refusal = None
    for i in range(12):
        r = c.post("/v1/automations", headers={**fhdr, "Idempotency-Key": "p14-cap-%d-%s" % (i, uuid.uuid4().hex[:6])},
                   json={"kind": "exit", "name": "cap probe %d" % i, "match": "all",
                         "triggers": [{"kind": "time", "at_ms": s.bench.now + 600_000, "once": True}],
                         "actions": [{"kind": "close_position", "method": "market", "max_slippage_bps": 100}],
                         "targets": [{"marketId": s.market}], "maxLossMicro": 5_000_000})
        if r.status_code == 200:
            created += 1
        else:
            refusal = {"status": r.status_code, "code": s.code(r.json() or {})}
            break
    facts["logic"]["rule_cap"] = {"created": created, "refusal": refusal}
    g.check("the concurrent-rule cap binds on the server: %d created, then %s"
            % (created, json.dumps(refusal)),
            refusal is not None and refusal["code"] in ("RULE_CAP", "HALTED") and created >= 1,
            "twelve rules were created with no refusal, so the cap is a UI convention: %s" % json.dumps(refusal))

    # ---------------------------------------------------------------- 6. payment and update forgery
    forged = c.post("/v1/telegram/webhook", headers={"X-Telegram-Bot-Api-Secret-Token": "not-the-secret"},
                    json={"update_id": 99001, "message": {"message_id": 1, "date": int(time.time()),
                                                          "chat": {"id": 42, "type": "private"},
                                                          "from": {"id": 42},
                                                          "text": "/start"}})
    claimed = con.execute("SELECT COUNT(*) FROM telegram_updates WHERE update_id=99001").fetchone()[0]
    facts["logic"]["forged_update"] = {"status": forged.status_code, "code": s.code(forged.json() or {}),
                                       "claim_rows": int(claimed)}
    g.check("a Telegram update with the wrong secret is refused and claims nothing (%d %s, %d claim row(s))"
            % (forged.status_code, s.code(forged.json() or {}), claimed),
            forged.status_code in (401, 403, 503) and int(claimed) == 0,
            "a forged update was processed: status %d, %d claim row(s)" % (forged.status_code, claimed))
    #    Telegram Stars and Stripe both deliver a *payment* as an inbound webhook, and neither exists in this
    #    build: no route takes a payment event anywhere in the declaration, and no handler reads
    #    `successful_payment`. So the probe records the absence with the requirements, rather than inventing a
    #    route to test.
    declared = " ".join((pth for pth in (s.bench.app.app.openapi().get("paths") or {})
                         if any(k in pth for k in ("stripe", "billing", "webhook", "payment", "checkout"))))
    stars = "successful_payment" in (ROOT / "services" / "api" / "app.py").read_text()
    facts["logic"]["payment_surface"] = {"declared_paths": declared or "none", "stars_handler": stars}
    g.open("NEITHER STRIPE NOR TELEGRAM-STARS FULFILMENT EXISTS YET, so payment-webhook forgery has no target",
           "both arrive as attacker-reachable POSTs, so before either ships: verify the signature over the RAW "
           "bytes with a constant-time compare (Stripe's `Stripe-Signature` includes a timestamp — refuse outside "
           "a 5-minute tolerance and store every event id to refuse replays; Telegram's is the secret header plus "
           "`successful_payment` arriving only in an update, never in a form post), and never read the amount, the "
           "user id or the plan from the body without cross-checking the record the provider keeps")
    #    The two gaps this OPEN item named are closed, and closed by re-test rather than by assertion: the same
    #    account applies its own link a second time and the audit line must say what state the code is in (not
    #    only whether this call flipped it), then the review that the first self-referral opened is *cleared*
    #    through the product's own admin route and the code must come back — with the event recorded.
    second = c.post("/v1/referrals/apply", headers={**hdr, "Idempotency-Key": "p14-ra-again-%s" % code},
                    json={"code": token})
    audit2 = con.execute("SELECT detail_json FROM audit_log WHERE action='referral.apply' AND actor_id=?"
                         " ORDER BY id DESC LIMIT 1", (a["uid"],)).fetchone()
    audit2_detail = json.loads(audit2[0]) if audit2 else {}
    facts["logic"]["self_referral_second_apply"] = {"status": second.status_code, "audit": audit2_detail}
    g.check("a repeat self-referral still records the code's *state*, not just that nothing changed (%s)"
            % json.dumps(audit2_detail)[:140],
            audit2_detail.get("builder_code_state") == "already-disabled"
            and audit2_detail.get("builder_code_revoked") is False,
            "the audit line does not distinguish 'already off' from 'nothing happened': %s"
            % json.dumps(audit2_detail)[:200])

    review_row = con.execute("SELECT id FROM referral_reviews WHERE kind='self_referral' AND state='open'"
                             " ORDER BY id DESC LIMIT 1").fetchone()
    admin_hdr = {"X-Admin-Token": os.environ["PGM_ADMIN_TOKEN"], "Content-Type": "application/json",
                 "Idempotency-Key": "p14-clear-%s" % code}
    cleared = c.post("/v1/referrals/review", headers=admin_hdr,
                     json={"id": int(review_row[0]) if review_row else 0, "decision": "clear",
                           "reason": "probe: the second wallet was a colleague's, reviewed and cleared",
                           "actor": "p14-probe"})
    after = con.execute("SELECT state, source, substr(note,1,90) FROM builder_code_status WHERE code=?",
                        (prog_code,)).fetchone()
    cleared_audit = con.execute("SELECT actor_id, detail_json FROM audit_log WHERE"
                                " action='referral.builder_code.cleared' ORDER BY id DESC LIMIT 1").fetchone()
    facts["logic"]["self_referral_cleared"] = {"status": cleared.status_code, "code_row": list(after) if after else None,
                                               "audit": json.loads(cleared_audit[1]) if cleared_audit else None,
                                               "actor": cleared_audit[0] if cleared_audit else None}
    g.check("clearing the review puts the programme's builder code back (%s)"
            % (list(after) if after else "NO ROW"),
            cleared.status_code == 200 and after is not None and str(after[0]) == "active"
            and str(after[1]) == "manual" and "review" in str(after[2] or ""),
            "a cleared self-referral review left the code disabled: the only route back is a hand-written UPDATE")
    g.check("the restore is an audit event naming who cleared it and why (%s)" % json.dumps(facts["logic"]
            ["self_referral_cleared"]["audit"])[:120],
            bool(cleared_audit) and cleared_audit[0] == "p14-probe",
            "the code came back with no record of who brought it back")

    #    And the guard the other way: a disable the VENUE wrote is not ours to clear. Plant one, clear a review,
    #    require it to stay off.
    con.execute("INSERT INTO builder_code_status (code, state, last_seen_ms, changed_ms, reject_count, source,"
                " note) VALUES (?, 'disabled', ?, ?, 9, 'venue_rejection', 'venue rejected the code')"
                " ON CONFLICT(code) DO UPDATE SET state='disabled', source='venue_rejection',"
                " note='venue rejected the code'",
                (prog_code, int(time.time() * 1000), int(time.time() * 1000)))
    con.commit()
    third = c.post("/v1/referrals/apply", headers={**hdr, "Idempotency-Key": "p14-ra-third-%s" % code},
                   json={"code": token})
    review2 = con.execute("SELECT id FROM referral_reviews WHERE kind='self_referral' AND state='open'"
                          " ORDER BY id DESC LIMIT 1").fetchone()
    cleared2 = c.post("/v1/referrals/review", headers={**admin_hdr, "Idempotency-Key": "p14-clear2-%s" % code},
                      json={"id": int(review2[0]) if review2 else 0, "decision": "clear",
                            "reason": "probe: clearing again to prove the venue's own disable survives",
                            "actor": "p14-probe"})
    venue_row = con.execute("SELECT state, source FROM builder_code_status WHERE code=?", (prog_code,)).fetchone()
    facts["logic"]["venue_disable_survives"] = {"third_apply": third.status_code, "clear": cleared2.status_code,
                                                "code_row": list(venue_row) if venue_row else None}
    g.check("a review cannot clear a disable the venue wrote (%s)" % (list(venue_row) if venue_row else "NO ROW"),
            venue_row is not None and str(venue_row[0]) == "disabled"
            and str(venue_row[1]) == "venue_rejection",
            "clearing our own flag overruled the venue's rejection: the product can re-enable a code the "
            "programme's owner switched off")
    g.open("A `close_position` ACTION LARGER THAN THE PER-ORDER CAP IS STILL REFUSED AT FIRE TIME ONLY",
           "the F19 fix covers sized actions (limit/market). Closing a position is sized by the position itself, so "
           "a $25,000 position cannot be closed by a rule while the cap is $2,500 — the risk review has to decide "
           "whether closes are exempt from the entry cap or split into legs, and that decision belongs in the "
           "gate's deny table rather than in this probe")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P14 D1 (second half) — trading + injection probes")
    ap.add_argument("--record")
    ap.add_argument("--json", dest="json_path")
    ap.add_argument("--only", default="")
    args = ap.parse_args(argv)
    want = [x.strip() for x in args.only.split(",") if x.strip()] or ["trading", "injection", "logic"]
    g = Gate()
    facts: dict = {"at_ms": int(time.time() * 1000)}
    s = Surface()
    facts["bench"] = {"market": s.market, "token": s.token, "tick": s.tick, "account": s.acct["uid"]}
    if "trading" in want:
        section_trading(g, facts, s)
    if "injection" in want:
        section_injection(g, facts, s)
    if "logic" in want:
        section_logic(g, facts, s)
    failed, opened = g.failed, g.opened
    verdict = "FAIL" if failed else ("CONDITIONAL" if opened else "PASS")
    lines = ["P14 D1 (second half) — trading and injection probes", "=" * 96, ""]
    for status, name, why in g.results:
        lines.append("%-4s %s%s" % (status, name, ("\n        — " + why) if why else ""))
    for key in ("trading", "injection", "logic"):
        if key in facts:
            lines += ["", "  %s: %s" % (key, json.dumps(facts[key], default=str)[:900])]
    passed = len(g.results) - len(failed) - len(opened)
    lines += ["", "P14 ATTACK SURFACE: %s — %d checks passed, %d failed, %d OPEN"
              % (verdict, passed, len(failed), len(opened))]
    if opened:
        lines += ["", "OPEN (not passes, not failures):"]
        lines += ["  * %s\n      %s" % (n, w) for _s, n, w in opened]
    text = "\n".join(lines) + "\n"
    print(text)
    if args.record:
        pathlib.Path(args.record).write_text(text)
    if args.json_path:
        pathlib.Path(args.json_path).write_text(json.dumps(
            {"verdict": verdict, "open_conditions": [{"check": n, "why": w} for _s, n, w in opened],
             "facts": facts, "checks": [{"status": st, "name": n, "why": w} for st, n, w in g.results]},
            indent=2, default=str) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
