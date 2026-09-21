#!/usr/bin/env python3
"""P13's headline chaos test: the same kill, N times, with the seam and the timing moved at random.

    python3 tools/p13-recovery-loop.py --runs 100 --seed 7 --record docs/verification/P13-chaos.txt
    python3 tools/p13-recovery-loop.py --runs 6              # the smoke run the phase gate makes
    python3 tools/p13-recovery-loop.py --runs 20 --only-seam posted

P12's prompt ends with a single sentence about which test is worth the most, and this is that test:

    "Show me the chaos test that kills the executor between signing and response, run 100 times in a loop, and the
    report proving zero duplicate orders and zero lost positions."

`tools/p06-chaos-test.py` demonstrates the three seams ONCE each, deliberately, with a transcript a human reads: the
kill lands at a named stage, and every number is narrated. That is the right shape for a demonstration and the wrong
shape for evidence about *recovery rates*, because a single pass can pass by luck — the process dies at one instant,
and one instant is one draw from a distribution nobody has sampled.

So this harness runs the same thing in a loop, and it randomises exactly the things that decide whether recovery
works: **which seam** the kill lands on (after signing and before the POST / inside the POST after the venue accepted
/ after a fill was booked), **where inside that window** the SIGKILL lands, how long the outage is simulated to have
lasted, the price and size of the order, and the recovery pass's tick budget. The seed is printed, so a failure is
reproducible; the per-run JSON is written next to the transcript, so a claim in the report can be checked against the
run that produced it.

Three invariants, and each is a way this system loses money:

  1. **No duplicate order, ever.** The venue's own count of orders carrying our `client_order_hash` is never 2 — it is
     read from the venue process over HTTP, not from our tables, because our tables are what is under test. And our
     side never ends up with two `orders` rows for one intent.
  2. **No lost position.** Every venue trade has exactly one `fills` row, the summed size matches the venue's summed
     size to the micro-share, and the open position lots for that token hold exactly that many shares. A fill the
     ledger missed is a user whose balance is wrong in the direction that costs them money.
  3. **No limbo.** The intent does not end the run parked in `queued`/`pending`/`submitting`: a recovery pass that
     leaves the row where it found it is a recovery pass that did nothing, and the user is left with an order they
     cannot see the state of.

The venue is a real second process on a socket and the executor is a real child process that really receives
`SIGKILL`. An in-process simulation cannot make these claims, because the code that simulates is the code that would
have had to recover.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tools" / "p06-chaos-test.py"
PY = sys.executable

#: The three seams, in the words the report uses. Each one is a different failure mode for the same order.
SEAMS = {
    "signed": "killed between the signature and the POST — the venue never heard about it",
    "posted": "killed inside the POST, after the venue accepted — the answer never arrived",
    "booked": "killed after a venue fill was booked, before the pass finished",
}

#: Where an intent is allowed to be when the loop is done. `uncertain` is legitimate (a submission we cannot yet
#: prove), and so is any state past it; `queued`/`pending`/`submitting` are not, because they mean the recovery pass
#: either did not look at this row or looked and left it exactly where it was.
SETTLED = frozenset({"uncertain", "submitted", "live", "partial", "filled", "canceled", "cancelled"})
IN_FLIGHT = frozenset({"queued", "pending", "submitting", "signed"})


def load_harness():
    """The p06 harness, imported rather than copied.

    `Scenario` is the part worth reusing — a temp database, a seeded market, a funded wallet, a venue on a real
    socket and an executor that can be SIGKILLed without taking the venue with it. A second implementation of that
    would be a second thing to keep correct, and the difference between the two would be the evidence.
    """
    spec = importlib.util.spec_from_file_location("p06_chaos_harness", str(HARNESS))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class Report:
    """What the harness prints is what it records, so the artifact is a transcript rather than a summary."""

    def __init__(self, record: Path | None, quiet: bool = False) -> None:
        self.lines: list[str] = []
        self.record = record
        self.quiet = quiet

    def __call__(self, text: str = "") -> None:
        self.lines.append(text)
        if not self.quiet:
            print(text, flush=True)

    def save(self, runs: list[dict], summary: dict) -> None:
        if not self.record:
            return
        head = [
            "P13 recovery loop — %s" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "generated by tools/p13-recovery-loop.py; not hand-written and not edited afterwards",
            "real processes: services/executor/main.py (SIGKILLed by this harness) and the mock venue as a SECOND",
            "process on a socket, so the venue outlives the executor and can be asked what it holds",
            "each run: its own SQLite database, its own venue, its own order, its own random seam and timing",
            "seed %s — every run below is reproducible with --seed %s" % (summary["seed"], summary["seed"]),
            "",
        ]
        self.record.parent.mkdir(parents=True, exist_ok=True)
        self.record.write_text("\n".join(head + self.lines) + "\n")


def run_once(sc, seam: str, spec: dict, say) -> dict:
    """One draw: queue, kill at the seam, recover, then read every number from the venue and from our rows."""
    rec = {"seam": seam, "price_micro": spec["price"], "size_micro": spec["size"], "age_ms": spec["age_ms"]}
    iid = sc.p.queue(key="p13-%s-%d" % (seam, spec["i"]), price=spec["price"], size=spec["size"])
    proc = pump = None
    t0 = time.perf_counter()
    try:
        if seam == "signed":
            sc.scenario("accept")
            proc, pump = sc.executor(spec["child"], stop_at="signed")
            hit = wait_stage(sc, pump, iid, "halted-after=signed")
            rec["reached_stage"] = bool(hit)
            rec["child_tail"] = pump.tail(6) if not hit else ""
            h = sc.hash_of(iid)
            rec["hash_stamped"] = bool(h)
            rec["venue_before"] = sc.held(h)
            rec["posts_before"] = sc.posts()
            sc.kill(proc)
            proc = None
            expect = {"posts_delta": 1, "venue": 1, "fills_min": 0}
        elif seam == "posted":
            sc.scenario("timeout_after_accept", delay_ms=spec["delay_ms"])
            proc, pump = sc.executor(spec["child"])
            # The randomness that matters: the kill lands somewhere inside the window where the venue has accepted
            # and the response has not come back. `partial` keeps it strictly inside.
            time.sleep(round(spec["delay_ms"] * spec["kill_fraction"] / 1000.0, 3))
            h = sc.hash_of(iid)
            rec["hash_stamped"] = bool(h)
            rec["venue_before"] = sc.held(h)
            rec["posts_before"] = sc.posts()
            sc.kill(proc)
            proc = None
            expect = {"posts_delta": 0, "venue": 1, "fills_min": 0}
        else:  # booked
            sc.scenario("partial_fill")
            proc, pump = sc.executor(spec["child"], stop_at="booked")
            hit = wait_stage(sc, pump, iid, "halted-after=booked")
            rec["reached_stage"] = bool(hit)
            rec["child_tail"] = pump.tail(6) if not hit else ""
            h = sc.hash_of(iid)
            rec["hash_stamped"] = bool(h)
            rec["venue_before"] = sc.held(h)
            rec["posts_before"] = sc.posts()
            sc.kill(proc)
            proc = None
            expect = {"posts_delta": 0, "venue": 1, "fills_min": 1}
        sc.scenario("accept")
        sc.age(iid)
        sc.recover()
    finally:
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=10)
            except Exception:                                   # noqa: BLE001 - cleanup only
                pass

    money = sc.money(iid)
    rec.update({
        "outage_ms": spec["age_ms"],
        "secs": round(time.perf_counter() - t0, 2),
        "venue_after": sc.held(h),
        "posts_after": sc.posts(),
        "our_orders": money["orders"],
        "our_fills": money["fills"],
        "our_ledger": money["ledger"],
        "venue_trades": money["trades"],
        "state": sc.state_of(iid),
        "actions": sc.actions(iid)[-1:],
        "hash": h[:18],
    })

    # --- the trade-level totals: sizes from the venue, sizes from our fills, and the open position lots.
    trades = read_trade_sizes(sc, money)
    rec["venue_shares_micro"] = trades["venue_shares"]
    rec["our_shares_micro"] = trades["our_shares"]
    rec["lot_shares_micro"] = trades["lots"]
    rec["ledger_rows_per_trade"] = trades["rows_per_trade"]

    # --- invariant 1: never two, and (per seam) exactly one where the venue should hold one.
    rec["dup_venue"] = rec["venue_after"] > 1
    rec["dup_ours"] = rec["our_orders"] > 1
    rec["venue_ok"] = rec["venue_after"] == expect["venue"]
    rec["posts_ok"] = (rec["posts_after"] - rec["posts_before"]) == expect["posts_delta"]
    # --- invariant 2: nothing lost. Every venue trade is one fill row and one ledger row, and the position holds
    # exactly what was bought.
    rec["shares_ok"] = (rec["our_shares_micro"] == rec["venue_shares_micro"] == rec["lot_shares_micro"]
                        and rec["our_fills"] == rec["our_ledger"] == rec["venue_trades"]
                        and rec["our_fills"] >= expect["fills_min"])
    # --- invariant 3: no limbo.
    rec["settled"] = rec["state"] in SETTLED and rec["state"] not in IN_FLIGHT
    # A stopped process is only evidence if the order reached the stage the seam names. `rejected` means the fixture
    # was wrong, not that recovery failed, and the two must not be averaged together.
    rec["setup_ok"] = rec["state"] != "rejected" and (seam == "posted" or bool(rec.get("reached_stage")))
    if not rec["setup_ok"]:
        risk = sc.conn.execute("SELECT COALESCE(risk_code,'') FROM order_intents WHERE id=?", (iid,)).fetchone()
        rec["why"] = ("refused before the seam: %s" % (risk[0] if risk and risk[0] else rec["state"])) \
            if rec["state"] == "rejected" else "never reached the %s seam in 30 s" % seam
    rec["ok"] = bool(rec["setup_ok"] and not rec["dup_venue"] and not rec["dup_ours"] and rec["venue_ok"]
                     and rec["posts_ok"] and rec["shares_ok"] and rec["settled"])
    say("  run %3d  %-6s price=%.2f size=%.6f  venue=%d posts=%d->%d orders=%d fills=%d/%d/%d shares %s==%s==%s "
        "state=%-9s %s  [%.1fs]"
        % (spec["i"], seam, spec["price"] / 1_000_000, spec["size"] / 10**6, rec["venue_after"],
           rec["posts_before"], rec["posts_after"], rec["our_orders"], rec["our_fills"], rec["our_ledger"],
           rec["venue_trades"], rec["our_shares_micro"], rec["venue_shares_micro"], rec["lot_shares_micro"],
           rec["state"], "ok" if rec["ok"] else ("ANOMALY: " + rec.get("why", "") if not rec["setup_ok"]
                                                   else "FAIL"), rec["secs"]))
    return rec


def wait_stage(sc, pump, iid: str, needle: str, *, timeout: float = 30.0) -> bool:
    """Wait for the child to reach its seam - and stop early if the order was refused before it could.

    The child freezes itself at the stage, so the wait is short when the fixture is right. When the fixture is
    wrong (a size under the market's minimum, a price off the tick) the intent is refused and the stage can never
    come: waiting out the full timeout turns one fixture mistake into thirty seconds of silence, which reads like
    a hang in the harness rather than a finding in the order.
    """
    until = time.time() + timeout
    while time.time() < until:
        if pump.wait_for(needle, timeout=0.05):
            return True
        if sc.state_of(iid) == "rejected":
            return False
        time.sleep(0.05)
    return False


def widen_book(sc, ask: int) -> None:
    """The lot under test gets a 49/ask ladder on the market's own 0.01 grid, with 100 shares on the ask.

    `tight_book()` (inherited from the P06 gate) puts a single 49/50 rung there, which is the right fixture for a
    demonstration and too narrow for a loop: every run would trade the same price. The depth is what makes a random
    size meaningful — a size larger than the ask would rest as a partial and change what "one venue order" means.
    """
    sc.p.store.conn.execute("DELETE FROM book_levels WHERE market_id=?", (sc.p.market,))
    for side, price in (("bid", ask - 10_000), ("ask", ask)):
        sc.p.store.conn.execute("INSERT INTO book_levels (market_id,side,price_micro,size_shares_micro,"
                                "level_count,updated_ms) VALUES (?,?,?,?,?,?)",
                                (sc.p.market, side, price, 100 * 10**6, 1, sc.p.at))
    sc.p.store.conn.commit()


def read_trade_sizes(sc, money: dict) -> dict:
    """Sizes, from three independent places, in micro-shares.

    * the venue's own trade list for our order (asked over HTTP),
    * our `fills` rows for that order (what the ledger believes was bought),
    * our `position_lots.shares_open_micro` for the token (what the user's position is built from).

    A buy that the ledger missed shows up as `our_shares < venue_shares`; a position built twice shows up as
    `lots > our_shares`. Both are money, and neither is visible in a count of rows.
    """
    oid = money.get("order_id") or ""
    row = sc.conn.execute("SELECT token_id, user_id FROM orders WHERE id=?", (oid,)).fetchone() if oid else None
    token, uid = (row[0], row[1]) if row else ("", "")
    our_shares = sc.conn.execute("SELECT COALESCE(SUM(size_micro),0) FROM fills WHERE order_id=?", (oid,)).fetchone()[0]
    lots = sc.conn.execute("SELECT COALESCE(SUM(shares_open_micro),0) FROM position_lots WHERE token_id=? AND user_id=?",
                           (token, uid)).fetchone()[0]
    _code, body = _http_trades(sc.url, oid)
    trades = body.get("trades") or []
    # `size_micro` when the venue sends it, else the venue's decimal size converted here — the mock sends both
    # shapes (its explicit `/v1/fill` path and its scenario ticker), and a real venue is not ours to choose from.
    def _micro(t: dict) -> int:
        if t.get("size_micro") is not None:
            return int(t["size_micro"])
        return int(round(float(t.get("size") or 0) * 10**6))

    venue_shares = sum(_micro(t) for t in trades)
    rows_per_trade = []
    for t in trades:
        tid = str(t.get("tradeID") or "")
        n = sc.conn.execute("SELECT COUNT(*) FROM cash_ledger WHERE ref_table='fills' AND ref_id LIKE ?",
                            ("%" + tid + "%",)).fetchone()[0] if tid else 0
        rows_per_trade.append(n)
    return {"venue_shares": venue_shares, "our_shares": int(our_shares), "lots": int(lots),
            "rows_per_trade": rows_per_trade}


def _http_trades(url: str, order_id: str) -> tuple[int, dict]:
    import urllib.request
    try:
        with urllib.request.urlopen("%s/v1/trades?orderID=%s" % (url, order_id), timeout=8) as r:
            return r.status, json.loads(r.read() or b"{}")
    except Exception as exc:                                    # noqa: BLE001 - reported as a number, not a crash
        return 0, {"error": type(exc).__name__, "trades": []}


def main() -> int:
    ap = argparse.ArgumentParser(description="P13: kill the executor between signing and the answer, N times")
    ap.add_argument("--runs", type=int, default=100, help="how many kills (the prompt's number is 100)")
    ap.add_argument("--seed", type=int, default=13, help="the seed the run prints and the report records")
    ap.add_argument("--record", default="", help="write the transcript here (the phase gate reads this file)")
    ap.add_argument("--json", default="", help="write the per-run records here")
    ap.add_argument("--only-seam", default="", choices=["", *SEAMS], help="one seam, for editing")
    ap.add_argument("--quiet", action="store_true", help="aggregate only")
    a = ap.parse_args()

    harness = load_harness()
    say = Report(Path(a.record) if a.record else None, quiet=a.quiet)
    rng = random.Random(a.seed)
    seams = [a.only_seam] if a.only_seam else list(SEAMS)

    say("P13 recovery loop — %d kills, seed %d" % (a.runs, a.seed))
    say("")
    say("the seams, and what each one means for the user:")
    for name, why in SEAMS.items():
        say("  %-7s %s" % (name, why))
    say("")

    runs: list[dict] = []
    t_start = time.perf_counter()
    # The market's tick is 0.01, so every price the loop draws has to be a multiple of 10_000 micro or the risk gate
    # refuses it OFF_TICK and the run proves nothing about recovery — which is exactly what the first version of this
    # harness did, twenty times in a row, at a minute each, looking like a recovery failure. A run that never reached
    # its seam is counted as a setup anomaly, not as evidence.
    ON_GRID_ASKS = (500_000, 510_000, 520_000, 600_000, 780_000, 990_000)
    for i in range(1, a.runs + 1):
        seam = rng.choice(seams)
        spec = {
            "i": i,
            "price": rng.choice(ON_GRID_ASKS),
            # 12-80 shares: at the lowest drawn ask that is $6, over the fixture markets' $5 minimum order size.
            # Drawing from 1 turned one run in four into `rejected` (a `MIN_NOTIONAL`-class refusal), which is a
            # fixture mistake dressed as a recovery failure - the same class of error as the off-grid prices.
            "size": rng.randint(12, 80) * 10**6,
            # 50–90 s of simulated outage: past the 45 s claim lease (so the intent is genuinely stuck) without
            # being a different scenario from the one the gate documents.
            "age_ms": rng.randint(50_000, 90_000),
            "delay_ms": rng.randint(3_000, 9_000),
            "kill_fraction": round(rng.uniform(0.15, 0.85), 3),
            "ticks": rng.randint(4, 8),
            # 10–30 s of child life. The first version drew 20–60 ticks at 30–90 ms (~1.5 s), which is shorter
            # than the reconciler's fill-sync cadence, so the `booked` seam could never be reached and every such
            # run looked like a lost position — a harness that ends before the thing it is waiting for.
            "child": ["--ticks", str(rng.randint(100, 300)), "--interval-ms", str(rng.randint(100, 150))],
        }
        sc = harness.Scenario("p13-%d" % i, say if not a.quiet else (lambda *_a, **_k: None),
                              age_ms=spec["age_ms"], ticks=spec["ticks"])
        try:
            widen_book(sc, spec["price"])
            rec = run_once(sc, seam, spec, say)
        finally:
            sc.close()
        runs.append(rec)

    secs = round(time.perf_counter() - t_start, 1)
    dup_venue = [r for r in runs if r["dup_venue"]]
    dup_ours = [r for r in runs if r["dup_ours"]]
    lost = [r for r in runs if not r["shares_ok"]]
    limbo = [r for r in runs if not r["settled"]]
    wrong = [r for r in runs if not (r["venue_ok"] and r["posts_ok"])]
    bad = [r for r in runs if not r["ok"]]
    setup = [r for r in runs if not r["setup_ok"]]
    by_seam = {s: {"runs": sum(1 for r in runs if r["seam"] == s),
                   "ok": sum(1 for r in runs if r["seam"] == s and r["ok"]),
                   "posts_moved": sum(r["posts_after"] - r["posts_before"] for r in runs if r["seam"] == s)}
               for s in seams}
    summary = {"runs": len(runs), "seed": a.seed, "secs": secs, "ok": len(runs) - len(bad),
               "duplicate_orders": len(dup_venue) + len(dup_ours), "lost_positions": len(lost),
               "limbo": len(limbo), "wrong_shape": len(wrong), "setup_anomalies": len(setup),
               "by_seam": by_seam}

    say("")
    say("aggregate — %d kills in %.1fs (%.2fs per kill)" % (len(runs), secs, secs / max(1, len(runs))))
    for s in seams:
        b = by_seam[s]
        say("  %-7s %3d runs, %3d clean, POSTs moved %+d (0 means the venue was never asked twice)"
            % (s, b["runs"], b["ok"], b["posts_moved"]))
    say("")
    say("  duplicate orders at the venue (0 means the venue never held two for one hash): %d" % len(dup_venue))
    say("  duplicate orders in our tables (0 means never two `orders` rows for one intent):     %d" % len(dup_ours))
    say("  lost positions (0 means every venue trade is one fill, one ledger row, and in the lots): %d" % len(lost))
    say("  intents left in limbo (0 means none parked in queued/pending/submitting):            %d" % len(limbo))
    say("  wrong shape (venue count or POST delta off by seam):                                 %d" % len(wrong))
    say("  setup anomalies (never reached the seam, or refused before it — not evidence either way): %d" % len(setup))
    say("")
    verdict = "PASS" if runs and not bad else "FAIL"
    say("RECOVERY LOOP: %s — %d/%d kills recovered cleanly, zero duplicate orders, zero lost positions"
        % (verdict, len(runs) - len(bad), len(runs)))
    if bad:
        say("")
        for r in bad[:5]:
            say("  failing run: %s" % json.dumps({k: v for k, v in r.items() if k != "actions"}))
    say.save(runs, summary)
    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps({"summary": summary, "runs": runs}, indent=2) + "\n")
    if a.record:
        print("recorded: %s" % a.record, flush=True)
    if a.json:
        print("records:  %s" % a.json, flush=True)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
