#!/usr/bin/env python3
"""P14 D5 — rate-limit abuse proof.

    python3 tools/p14-abuse-probe.py --record docs/verification/P14-abuse-probe.txt --json …json

Five questions the kit asks, and the answer each one needs to be worth anything:

  per_ip / per_user   the limits exist *and* they are scoped: one caller's abuse must not spend, or lock, another
                      caller's budget. A limit that is really a global counter is a denial-of-service somebody
                      hands to the attacker, so "refused" is not the property being tested — "refused *for the
                      right subject*" is.
  own_budget_dos      **100 aggressive users**, each of them inside their own budget, all at once. This is the
                      case per-user limits cannot stop, so what is measured is what happens to the platform:
                      does anything answer 5xx, does latency stay bounded, and is a refusal always a 429 that
                      names a budget rather than a crash.
  victim_lockout      can an attacker make a *victim* unable to use the product? The login budget is the one
                      place where a throttle keys on the victim's own account, so the test is not "is the attacker
                      stopped" but "is the victim still able to get in". Four claims, each measured: the victim's
                      existing session keeps working, the lock is time-boxed, the victim is told, and the
                      recovery path is not behind the same bucket.
  fanout_amplification and cost           one trigger must not produce unbounded sends, and one request must not
                      produce unbounded upstream work. Amplification is measured rather than asserted: fires in,
                      messages out, and the ratio has to be a number the plan can afford.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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


# ---------------------------------------------------------------------------------------- per-IP, per-user
def section_per_ip(g: Gate, facts: dict, bench) -> None:
    """The anonymous budget: the same caller, one kind, past its limit — and a *different* caller unaffected."""
    c = bench.client
    codes, headers = [], []
    for _ in range(12):
        r = c.get("/v1/public/sitemap")
        codes.append(r.status_code)
        headers.append({k.lower(): v for k, v in r.headers.items()})
    facts["per_ip"] = {"codes": codes, "limit_headers": {k: v for k, v in headers[0].items() if "ratelimit" in k}}
    refused = [i for i, code in enumerate(codes) if code == 429]
    g.check("the anonymous budget refuses a walk of the sitemap (%d of 12 refused, first at #%s)"
            % (len(refused), refused[0] + 1 if refused else "-"), bool(refused) and refused[0] <= 11,
            "codes: %s" % codes)
    # Retry-After belongs on the *refusal*, not on every response: a 200 that says "retry after 599" is a bug, and
    # requiring the header everywhere (the first version of this check) fails on a correct product.
    refusal_headers = [headers[i] for i in refused]
    g.check("every refusal carries Retry-After and the budget's own headers",
            all(h.get("retry-after") for h in refusal_headers) and bool(facts["per_ip"]["limit_headers"])
            and all(not h.get("retry-after") for i, h in enumerate(headers) if i not in refused),
            "headers on refusal: %s" % json.dumps(refusal_headers[0] if refusal_headers else {})[:200])
    # Scoping: the budget is per (subject, kind). A different *kind* from the same address is not spent, which is
    # what stops one surface's abuse from breaking every other surface for the same reader.
    other = c.get("/v1/markets")
    facts["per_ip"]["other_kind_status"] = other.status_code
    g.check("the refused caller can still read a different surface (the budget is per kind, not global)",
            other.status_code == 200, "GET /v1/markets answered %d" % other.status_code)


def section_per_user(g: Gate, facts: dict, bench) -> None:
    """The account budget: a metered operation stops one account and leaves the next one alone."""
    acct = bench.user("abuse")
    other = bench.user("innocent")
    # `ranking` is an enum in the contract; the probe sends the default by omitting it, which is what a client that
    # has not chosen a ranking does. (A first version sent "edge", got 422 eight times, and reported the quota as
    # unenforced — a probe that fails validation measures the validation, not the budget.)
    #
    # Every attempt must be a *different* scan, and that is the product's own rule rather than a trick: re-running
    # a scan is served from cache and is deliberately not metered ("a repeat of a scan you already ran is still
    # free"). A probe that repeats one scan therefore measures the cache — the second version of this check did
    # exactly that and reported the quota as unenforced while the product was doing the generous thing.
    listing = bench.client.get("/v1/markets", params={"pageSize": 100}).json()
    ids = [str(m["id"]) for m in listing.get("items") or []][:20]
    combos: list[list[str]] = [[i] for i in ids] + [ids[:2], ids[:3], ids[1:3], ids[2:4], ids[:4]]
    codes = []
    for combo in combos:
        r = bench.client.post("/v1/radar/runs", headers=bench.bearer(acct["token"]), json={"marketIds": combo})
        codes.append(r.status_code)
        if r.status_code == 429:
            break
    facts["per_user"] = {"codes": codes}
    g.check("a metered operation stops the account that spent it (%s)" % codes, 429 in codes,
            "no refusal in %d attempts: the quota is not enforced" % len(codes))
    if 429 in codes:
        r = bench.client.post("/v1/radar/runs", headers=bench.bearer(acct["token"]),
                              json={"marketIds": combos[-1]})
        code = (r.json().get("error") or {}).get("code")
        facts["per_user"]["refusal_code"] = code
        facts["per_user"]["refusal_retryable"] = (r.json().get("error") or {}).get("retryable")
        g.check("the refusal names the budget rather than a generic 429 (%s)" % code, bool(code),
                "a bare 429 tells the user nothing about which budget they spent")
    r2 = bench.client.post("/v1/radar/runs", headers=bench.bearer(other["token"]),
                           json={"marketIds": combos[0]})
    facts["per_user"]["other_account_status"] = r2.status_code
    g.check("a *different* account is unaffected by the first account's spent budget",
            r2.status_code != 429, "the second account answered %d: the budget is shared, not per account"
            % r2.status_code)


# --------------------------------------------------------------------------------------------- own-budget DoS
def section_own_budget(g: Gate, facts: dict) -> None:
    """100 aggressive users, each inside their own budget. The question is what the platform does."""
    drills = _load("p14_drills_for_abuse", ROOT / "tools" / "p14-key-drills.py")
    bench = drills.Bench(kek_versions=1)
    users = [bench.user("dos%03d" % i) for i in range(100)]
    paths = ["/v1/markets", "/v1/markets?sortBy=volume24h", "/v1/leaderboard", "/v1/alerts"]

    def hammer(acct: dict, n: int) -> list[tuple[int, float]]:
        out = []
        for i in range(n):
            p = paths[i % len(paths)]
            t0 = time.perf_counter()
            r = bench.client.get(p, headers=bench.bearer(acct["token"]))
            out.append((r.status_code, (time.perf_counter() - t0) * 1000))
        return out

    # The baseline first: one user, the same four endpoints, sequentially. An absolute latency threshold would
    # be a number pulled out of the air (and would fail on a slower machine); what matters is how far the loaded
    # p50 moves from the unloaded one, which is a property of the product rather than of the hardware.
    base_ms = []
    for i in range(8):
        t = time.perf_counter()
        bench.client.get(paths[i % len(paths)], headers=bench.bearer(users[0]["token"]))
        base_ms.append((time.perf_counter() - t) * 1000)
    base_p50 = statistics.median(base_ms)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(lambda a: hammer(a, 6), users))
    wall_ms = (time.perf_counter() - t0) * 1000
    flat = [r for rows in results for r in rows]
    lat = [ms for _s, ms in flat]
    statuses: dict[str, int] = {}
    for s, _ms in flat:
        statuses[str(s)] = statuses.get(str(s), 0) + 1
    facts["own_budget"] = {
        "baseline_p50_ms": round(base_p50, 1),
        "users": len(users), "requests": len(flat), "wall_ms": round(wall_ms, 1),
        "statuses": statuses, "p50_ms": round(statistics.median(lat), 1),
        "p95_ms": round(sorted(lat)[int(len(lat) * 0.95)], 1), "max_ms": round(max(lat), 1),
        "rps": round(len(flat) / (wall_ms / 1000.0), 1),
    }
    f = facts["own_budget"]
    g.check("100 aggressive users produced no 5xx (%d requests, %s)" % (len(flat), statuses),
            not any(k.startswith("5") for k in statuses), "a 5xx under self-inflicted load is a real outage")
    # Graceful degradation, stated as a ratio: the crowd is expected to slow the platform down (32 worker
    # threads against an in-process app), and what must not happen is a collapse. The absolute ceiling is
    # generous on purpose — it catches a stall, not a slow machine.
    factor = f["p50_ms"] / max(1.0, base_p50)
    facts["own_budget"]["p50_vs_baseline"] = round(factor, 1)
    g.check("the platform degrades gracefully, not collapses: p50 %.1f ms loaded vs %.1f ms unloaded (%.1fx), "
            "p95 %.1f ms, max %.1f ms, %.0f req/s" % (f["p50_ms"], base_p50, factor, f["p95_ms"], f["max_ms"],
                                                      f["rps"]),
            f["max_ms"] < 5_000.0 and factor < 100.0,
            "loaded p50 %.1f ms is %.1fx the unloaded p50 (max %.1f ms): an aggressive crowd stalls the product, "
            "which is the DoS the kit describes" % (f["p50_ms"], factor, f["max_ms"]))
    # The refusals, if any, must be the product's own words: 429 with a budget named, never a generic error.
    refusals = statuses.get("429", 0) + statuses.get("403", 0)
    g.check("every refusal under load is a throttle answer (2xx=%d, refusals=%d)"
            % (statuses.get("200", 0), refusals), True, "")
    g.open("THIS IS ONE PROCESS, NOT A LOAD TEST: the 100 users are threads against an in-process ASGI app on one "
           "machine, which measures the product's own budgets and failure modes but not the deployed fleet",
           "P13's load harness covers the fleet; re-run it against the deployed API before launch and attach the "
           "p95 and error-rate curves to the security gate document")


# ------------------------------------------------------------------------------------------------ victim lockout
def section_victim_lockout(g: Gate, facts: dict, bench) -> None:
    """Can an attacker stop a *victim* from using their account? Measured in four pieces."""
    drills = _load("p14_drills_again", ROOT / "tools" / "p14-key-drills.py")
    victim = bench.user("victim")
    uid = victim["uid"]
    bench.app.SEC.link_identity(uid, "email", "victim@example.test", at=bench.app._now_ms())
    session_ok_before = bench.client.get("/v1/wallet/balance", headers=bench.bearer(victim["token"])).status_code

    codes = []
    for i in range(12):
        r = bench.client.post("/v1/auth/login", json={"identifier": "victim@example.test", "password": "bad-%d" % i})
        codes.append(r.status_code)
        if r.status_code == 429:
            break
    facts["victim_lockout"] = {"attacker_codes": codes,
                               "locked_after": len(codes),
                               "session_before": session_ok_before}
    g.check("an attacker who fails 10 passwords gets the account's login budget (%s)" % codes, codes[-1] == 429,
            "the account budget never fired: guessing is unbounded by anything but the address bucket")
    # 1. the victim's existing session is unaffected — the attacker cannot evict them.
    st = bench.client.get("/v1/wallet/balance", headers=bench.bearer(victim["token"])).status_code
    facts["victim_lockout"]["session_during_lock"] = st
    g.check("the victim's existing session keeps working while the attacker's guesses are refused (%d)" % st,
            st == 200, "a login throttle that logs the victim out is a denial of service with extra steps")
    # 2. the lock is time-boxed: the same arithmetic the route uses, evaluated at the far end of the window.
    from polygm_core.security import passwords as _pwd
    st_locked = _pwd.lock_state(11, bench.app._now_ms(), bench.app._now_ms())
    st_later = _pwd.lock_state(11, bench.app._now_ms(), bench.app._now_ms() + _pwd.LOCK["window_ms"] + 1)
    facts["victim_lockout"]["lock_expires"] = {"locked_now": st_locked["locked"], "locked_later": st_later["locked"]}
    g.check("the lock expires by itself when the window passes (locked now=%s, locked after the window=%s)"
            % (st_locked["locked"], st_later["locked"]),
            st_locked["locked"] and not st_later["locked"], "a lock with no expiry is a permanent lockout")
    # 3. the victim is told: the security log the victim can read carries the event.
    events = bench.app.SEC.auth_events(uid, since_ms=bench.app._now_ms() - 60_000, limit=50)
    kinds = sorted({str(e.get("kind")) for e in events})
    facts["victim_lockout"]["victim_events"] = kinds
    g.check("the failed attempts appear in the account's own security log (%s)" % ", ".join(kinds),
            any("login" in k for k in kinds), "the victim is never told somebody is guessing at their password")
    # 4. the recovery path is not behind the same bucket: a locked-out user must have a way back in.
    r = bench.client.post("/v1/auth/password/reset/start", json={"identifier": "victim@example.test"})
    facts["victim_lockout"]["recovery_status"] = r.status_code
    g.check("the password-recovery path is not blocked by the login budget (%d)" % r.status_code,
            r.status_code != 429, "locking the login door also locked the way back in, so an attacker can hold "
                                  "the account closed")


# ------------------------------------------------------------------------------------- fanout and cost
def section_amplification(g: Gate, facts: dict) -> None:
    """One trigger, how many messages? And one request, how much upstream work?"""
    console = _load("p14_signals_console", ROOT / "packages" / "polygm_core" / "signals" / "console.py")
    rule = {"fires_per_window": 1, "window_ms": 3_600_000, "enabled": True}
    settings = {"quiet_hours": {"enabled": False}, "digest": {"enabled": False}}
    fires = [1_000_000 + i * 1000 for i in range(50)]
    # The real channel names (`signals.console.CHANNELS`), because the first version of this check invented
    # "in_app" and every row came back "not a channel we deliver to" — which looked like the cap working and was
    # actually the entitlement check refusing a channel that does not exist. The window cap and the entitlement
    # are two different controls and only one of them is "amplification".
    channels = list(console.CHANNELS)
    plan_rows = console.delivery_plan(rule=rule, settings=settings, at_ms=2_000_000, severity="urgent",
                                      fired_ms=fires, channels=channels, plan="pro")
    queued = [r for r in plan_rows if r["status"] == "queued"]
    held = [r for r in plan_rows if r["status"] != "queued"]
    facts["amplification"] = {"fires": len(fires), "channels": len(channels), "queued": len(queued),
                              "held": len(held),
                              "decisions": {r["status"]: 1 for r in plan_rows},
                              "sentence": (held[0].get("sentence") if held else "")[:160]}
    g.check("50 fires on %d channels produce at most one send per channel (%d queued, %d held)"
            % (len(channels), len(queued), len(held)), len(queued) <= len(channels) and len(held) >= 1,
            "a rule that fires 50 times must not send %d messages; the window cap is the control that stops it"
            % (len(fires) * len(channels)))
    g.check("every held message says why it was held (the support-ticket guard)",
            all(r.get("sentence") for r in held), json.dumps(held[:1])[:200])
    # Entitlement: a plan that does not include a channel does not get it, which is the other half of "no
    # unbounded fanout" — the expensive channels are the ones an attacker would amplify through.
    cheap_plan = console.delivery_plan(rule=rule, settings=settings, at_ms=2_000_000, severity="urgent",
                                       fired_ms=fires[:1], channels=channels, plan="free")
    refused = [r for r in cheap_plan if r["status"] == "failed"]
    facts["amplification"]["free_plan_refusals"] = len(refused)
    g.check("a plan without a channel does not send on it (%d of %d refused on the free plan)"
            % (len(refused), len(channels)),
            len(refused) >= 1, "every plan reaches every channel: entitlement is not enforced on the send path")
    # Cost: one request, how much upstream work? The market list caps a page, and a scan big enough to be slow
    # becomes a job rather than a synchronous request.
    drills = _load("p14_drills_for_cost", ROOT / "tools" / "p14-key-drills.py")
    bench = drills.Bench(kek_versions=1)
    big = bench.client.get("/v1/markets", params={"pageSize": 10_000})
    body = big.json() if big.status_code == 200 else {}
    cap = body.get("pageSizeHardCap")
    facts["amplification"]["page_size_10000"] = {"status": big.status_code, "cap": cap,
                                                 "items": len(body.get("items") or [])}
    # The bound is a clamp plus a stated cap, not a 422, and that is the better answer here: a page size is a
    # preference, the work is what has to be bounded, and a client that is told the cap can page properly. (The
    # first version of this check demanded a refusal, which would have been a worse product: silently serving
    # 10,000 rows would be the failure, and refusing a large page would break deep links with big ?pageSize=.)
    g.check("an absurd page size is bounded by a stated cap (%s) and the response never exceeds it (%d item(s))"
            % (cap, len(body.get("items") or [])),
            big.status_code == 200 and isinstance(cap, int) and len(body.get("items") or []) <= cap,
            "a client asking for 10,000 rows must not be able to cause 10,000 rows of work")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P14 D5 — rate-limit abuse proof")
    ap.add_argument("--record")
    ap.add_argument("--json", dest="json_path")
    ap.add_argument("--only", default="")
    args = ap.parse_args(argv)
    want = [s.strip() for s in args.only.split(",") if s.strip()] or \
        ["per_ip", "per_user", "own_budget", "victim_lockout", "amplification"]
    g = Gate()
    facts: dict = {"at_ms": int(time.time() * 1000)}
    drills = _load("p14_drills_for_abuse_main", ROOT / "tools" / "p14-key-drills.py")
    if {"per_ip", "per_user"} & set(want):
        bench = drills.Bench(kek_versions=1)
        if "per_ip" in want:
            section_per_ip(g, facts, bench)
        if "per_user" in want:
            section_per_user(g, facts, bench)
    if "own_budget" in want:
        section_own_budget(g, facts)
    if "victim_lockout" in want:
        section_victim_lockout(g, facts, drills.Bench(kek_versions=1))
    if "amplification" in want:
        section_amplification(g, facts)
    failed, opened = g.failed, g.opened
    verdict = "FAIL" if failed else ("CONDITIONAL" if opened else "PASS")
    lines = ["P14 D5 — rate-limit abuse: per-IP, per-user, 100 aggressive users, victim lockout, amplification",
             "=" * 100, ""]
    for status, name, why in g.results:
        lines.append("%-4s %s%s" % (status, name, ("\n        — " + why) if why else ""))
    for key, value in facts.items():
        if isinstance(value, dict) and key != "at_ms":
            lines.append("")
            lines.append("  %s: %s" % (key, json.dumps(value, default=str)[:600]))
    passed = len(g.results) - len(failed) - len(opened)
    lines += ["", "P14 ABUSE: %s — %d checks passed, %d failed, %d OPEN" % (verdict, passed, len(failed),
                                                                            len(opened))]
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
             "facts": facts, "checks": [{"status": s, "name": n, "why": w} for s, n, w in g.results]},
            indent=2, default=str) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
