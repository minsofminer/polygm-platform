#!/usr/bin/env python3
"""P05 Quality Gate. The prompt's line is: "Kill the WebSocket for 5 minutes during a live test. On reconnect:
no duplicate alerts, no missed fills above threshold, book resyncs correctly, and the UI showed a stale indicator
the whole time. Show me the test." Every rule below is EXECUTED and reports what it counted.

`--fast` runs everything that needs no network. `--live` adds the two network checks: the 300 s outage itself
(`tools/p05-chaos-test.py`) and the fixture-contract drift check. The offline checks are not a substitute for it:
the gate's headline claim is a live measurement, and `make gate-p05` runs both and records the artifact in
`docs/verification/P05-chaos-output.txt` — "show me the test" is answered by a committed file, not by this tool's
own prose.

Format of a check: (label, ok, detail). Detail is required even on success, because a check that only speaks when
it fails is indistinguishable from one that never ran.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT / "services" / "ingest"))

ARTIFACT = ROOT / "docs" / "verification" / "P05-chaos-output.txt"
PG_MIG = ROOT / "db" / "migrations"
LITE_MIG = ROOT / "db" / "migrations-sqlite"
PY = sys.executable


def sh(argv: list[str], timeout: int = 1200, env: dict | None = None) -> tuple[int, str]:
    e = dict(os.environ)
    e["PYTHONPATH"] = "%s:%s:%s" % (ROOT / "packages", ROOT / "services" / "api", ROOT / "tools")
    e.update(env or {})
    try:
        p = subprocess.run(argv, cwd=str(ROOT), env=e, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "(timed out after %ds)" % timeout
    return p.returncode, p.stdout + p.stderr


def fresh_db(path: str | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or ":memory:")
    con.executescript("PRAGMA foreign_keys=ON;\n"
                      + "\n".join(p.read_text() for p in sorted(LITE_MIG.glob("*.sql"))))
    return con


# --------------------------------------------------------------------------- the offline checks
def c_suite() -> tuple[str, bool, str]:
    rc, out = sh([PY, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-q"], timeout=900)
    m = re.search(r"Ran (\d+) tests", out)
    n = int(m.group(1)) if m else 0
    # Per-module floors, not just a total: a phase that deletes a test file to go green is a real thing that has
    # happened to real projects, and a total alone cannot see it.
    # Floors are "this many tests exist", so deleting a file goes red. They are set to the current counts on
    # purpose, and raised when a phase adds coverage; a floor invented above what exists is a gate that fails
    # for arithmetic rather than for a bug (this caught `test_migrations.py` at 18, where I had guessed 20).
    want = {"test_ingest.py": 47, "test_ingest_main.py": 44, "test_signals.py": 22, "test_fanout.py": 16,
            "test_api.py": 58, "test_migrations.py": 18}
    counts = {}
    for name, floor in want.items():
        rc2, out2 = sh([PY, "-m", "unittest", "discover", "-s", "tests", "-p", name, "-q"], timeout=300)
        mm = re.search(r"Ran (\d+) tests", out2)
        counts[name] = int(mm.group(1)) if mm else 0
    short = ["%s=%d<%d" % (k, v, want[k]) for k, v in counts.items() if v < want[k]]
    ok = rc == 0 and n >= 290 and not short
    return ("suite: %d tests, 0 failures (%s)" % (n, ", ".join("%s:%d" % kv for kv in sorted(counts.items()))),
            ok, "failures: %s | %s" % ("none" if rc == 0 else "see below", "; ".join(short) or "floors met"))


def parse_chaos(text: str) -> dict:
    """Read the verdict lines rather than trusting the summary word."""
    out: dict = {"checks": [], "outage_s": 0, "alerts": 0, "live_seen": 0, "covered": 0, "total": 0,
                 "lag_line": None}
    for line in text.splitlines():
        m = re.match(r"\s*\[(PASS|FAIL)\]\s+(.*)", line)
        if m:
            out["checks"].append((m.group(1) == "PASS", m.group(2).strip()))
        if (m := re.search(r"outage (\d+)s", line)):
            out["outage_s"] = int(m.group(1))
        if (m := re.search(r"B\. no duplicate alerts \((\d+) alerts", line)):
            out["alerts"] = int(m.group(1))
        if (m := re.search(r"live fills seen=(\d+)", line)):
            out["live_seen"] = int(m.group(1))
        if (m := re.search(r"(\d+) of (\d+) venue rows", line)):
            out["covered"], out["total"] = int(m.group(1)), int(m.group(2))
        if "reference tape lag" in line:
            out["lag_line"] = line.strip()
    return out


def c_artifact() -> tuple[str, bool, str]:
    if not ARTIFACT.exists():
        return ("live chaos artifact present", False, "docs/verification/P05-chaos-output.txt is missing")
    text = ARTIFACT.read_text()
    v = parse_chaos(text)
    passed = [ok for ok, _ in v["checks"]]
    why = []
    if len(v["checks"]) != 7:
        why.append("only %d/7 verdict lines" % len(v["checks"]))
    if not all(passed):
        why.append("failing lines: %s" % " | ".join(t for ok, t in v["checks"] if not ok)[:200])
    if v["outage_s"] < 300:
        why.append("outage was %ds, the gate says 5 minutes" % v["outage_s"])
    # The non-vacuity rules. Each of these has been a real false pass in this phase: check B "passed" with zero
    # alerts because the harness fed the engine events without the dispatch key, and C "passed" because the
    # reference query compared nothing it had actually reached.
    if v["alerts"] == 0:
        why.append("B is vacuous: zero alerts fired, so dedupe was never exercised")
    if v["live_seen"] == 0:
        why.append("B2 is vacuous: the socket delivered no trade frames")
    if v["total"] == 0 or v["covered"] != v["total"]:
        why.append("C is not a pass: %d of %d venue fills covered" % (v["covered"], v["total"]))
    if "INCONCLUSIVE" in text:
        why.append("the artifact still says INCONCLUSIVE")
    if v["lag_line"] is None:
        why.append("no reference-lag line: the tape's own freshness during the run is unknown")
    return ("live chaos artifact: %d/7 checks PASS, outage %ds, %d alerts, %d live fills, %d/%d large fills"
            % (sum(passed), v["outage_s"], v["alerts"], v["live_seen"], v["covered"], v["total"]),
            not why, "; ".join(why) or "all seven, and none of them vacuous")


def c_live_chaos() -> tuple[str, bool, str]:
    rc, out = sh([PY, "tools/p05-chaos-test.py", "--subjects", "8", "--capacity", "40000"], timeout=1500)
    v = parse_chaos(out)
    return ("live 300s outage re-run now: %d/7 PASS (alerts=%d, live=%d, fills=%d/%d)"
            % (sum(1 for ok, _ in v["checks"] if ok), v["alerts"], v["live_seen"], v["covered"], v["total"]),
            rc == 0 and len(v["checks"]) == 7 and all(ok for ok, _ in v["checks"]) and v["alerts"] > 0,
            out.strip().splitlines()[-1][:200] if out.strip() else "no output")


def c_fixture_drift() -> tuple[str, bool, str]:
    rc, out = sh([PY, "tools/p05-capture-fixtures.py", "--check"], timeout=400)
    return ("recorded payloads still match the venue", rc == 0,
            (out.strip().splitlines() or ["(no output)"])[-1][:200])


def c_stale_indicator() -> tuple[str, bool, str]:
    """The fourth clause of the gate's sentence, checked at the HTTP boundary.

    "The UI showed a stale indicator" is not a property of the ingest; it is a property of what the API answers.
    So the check seeds `book_levels` twice — 10 minutes old and 10 ms old — and asserts the client's one
    comparison (`now > staleAfter`) flips. A route that stamps `staleAfter = now + budget` passes a "stale works"
    unit test on the freshness module and still lies to the user, which is what this check exists to prevent.
    """
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    con = fresh_db(db.name)
    con.close()
    env = {"PGM_DB_PATH": db.name, "PGM_AUTH_MODE": "dev-header"}
    code = '''
import json, os, sqlite3, sys, time
sys.path.insert(0, os.environ["ROOT"])
from fastapi.testclient import TestClient
import app as A
con = sqlite3.connect(os.environ["PGM_DB_PATH"])
now = int(time.time() * 1000)
row = con.execute("SELECT id, condition_id FROM markets LIMIT 1").fetchone()
mid, cid = str(row[0]), str(row[1])
def stamp(ms):
    con.execute("DELETE FROM book_levels")
    con.execute("INSERT INTO book_levels (market_id,side,price_micro,size_shares_micro,level_count,updated_ms)"
                " VALUES (?,'bid',400000,1000000,1,?)", (mid, ms))
    con.execute("INSERT INTO book_levels (market_id,side,price_micro,size_shares_micro,level_count,updated_ms)"
                " VALUES (?,'ask',600000,1000000,1,?)", (mid, ms))
    con.commit()
out = {}
stamp(now - 600_000)
c = TestClient(A.app, raise_server_exceptions=False)
r = c.get("/v1/markets/%s/book" % mid).json()
out["old"] = {"asOf": r["asOf"], "staleAfter": r["staleAfter"], "ageMs": r["ageMs"], "now": A._now_ms()}
stamp(now - 10)
r2 = c.get("/v1/markets/%s/book" % mid).json()
out["fresh"] = {"asOf": r2["asOf"], "staleAfter": r2["staleAfter"], "ageMs": r2["ageMs"], "now": A._now_ms()}
print(json.dumps(out))
'''
    rc, out = sh([PY, "-c", code], timeout=180, env=dict(env, ROOT=str(ROOT)))
    try:
        data = json.loads(out.strip().splitlines()[-1])
    except Exception:
        return ("stale indicator reaches the API", False, "probe failed: " + out.strip()[-200:])
    old, fresh = data["old"], data["fresh"]
    stale_old = old["staleAfter"] <= old["now"]
    fresh_ok = fresh["staleAfter"] > fresh["now"]
    consistent = abs(old["asOf"] - (old["now"] - old["ageMs"])) < 2000
    # user copy: the UI text is generated from these numbers, and the word must never appear
    user_copy_clean = "websocket" not in out.lower() and "ws" not in json.dumps(old).lower().replace("asOf", "")
    return ("stale indicator at the HTTP boundary: a 10-minute-old ladder answers staleAfter<=now (%s), a fresh "
            "one does not (%s)" % (stale_old, fresh_ok),
            stale_old and fresh_ok and consistent and user_copy_clean,
            "asOf matches the row's clock: %s | no transport vocabulary in the payload: %s" % (consistent,
                                                                                                user_copy_clean))


def c_dedupe_constraint() -> tuple[str, bool, str]:
    import main as M
    import tape as T
    con = fresh_db()
    st = M.Store(con)
    fill = {"tx_hash": "0xabc", "token_id": "t1", "market": "0xc1", "side": "BUY", "outcome": "Yes",
            "outcome_index": 0, "wallet": "0xw", "price_micro": 500_000, "size_micro": 10_000_000,
            "usd_notional_micro": 5_000_000, "ts_ms": 1789700000000, "source": "rest", "fee_rate_bps": 0}
    first = st.insert_fills([fill])
    replay = st.insert_fills([fill])
    n = con.execute("SELECT COUNT(*) FROM tape_fills").fetchone()[0]
    pg = (PG_MIG / "0006_ingest.sql").read_text()
    lite = "".join(p.read_text() for p in sorted(LITE_MIG.glob("*.sql")))
    both = "UNIQUE (dedupe_key)" in pg and "dedupe_key" in lite and "UNIQUE" in lite
    return ("replay safety is a DB property: insert %d, replay %d, rows %d" % (first, replay, n),
            first == 1 and replay == 0 and n == 1 and both and T.dedupe_key(fill) != T.dedupe_key(
                dict(fill, tx_hash="0xdef")),
            "UNIQUE(dedupe_key) in both dialects: %s | a different tx is a different row: %s"
            % (both, T.dedupe_key(fill) != T.dedupe_key(dict(fill, tx_hash="0xdef"))))


def c_bounded_tape() -> tuple[str, bool, str]:
    src = (ROOT / "services" / "ingest" / "main.py").read_text()
    harness = (ROOT / "tools" / "p05-chaos-test.py").read_text()
    code = "\n".join(l for l in (src + "\n" + harness).split("\n") if not l.strip().startswith("#"))
    # "Every tape request is bounded" has to be decided at the CALL SITE, keyed on the source name, not by
    # looking for `limit` anywhere: the universe poll also sends a `limit` and has no time range (it is a
    # metadata read, and bounding it would be nonsense). A gate that flags correct code is a gate people widen.
    tape_reqs, bad = [], []
    for m in re.finditer(r'source="data\.trades"', code):
        window = code[max(0, m.start() - 400):m.end() + 400]
        tape_reqs.append(window)
        if "start" not in window or "end" not in window:
            bad.append(window.strip().splitlines()[-1][:90])
    # …and any request that hits the trades endpoint at all, in either file, must be bounded the same way.
    for m in re.finditer(r"\{DATA\}/trades|data-api.{0,40}/trades", code):
        window = code[max(0, m.start() - 500):m.end() + 500]
        tape_reqs.append(window)
        if "start" not in window or "end" not in window:
            bad.append("unbounded trades request near: " + window.strip().splitlines()[0][:70])
    bust = re.findall(r'"_"\s*:\s*_?now|cache_bust|_nocache|"__bust"', code, re.I)
    # the venue's cache is not the problem; a buster in the tree would mean we learned the wrong lesson
    return ("every tape request is bounded by start/end (%d request sites), and no cache-buster exists (%d)"
            % (len(tape_reqs), len(bust)), not bad and not bust,
            "offenders: %s" % (bad + bust) if (bad or bust) else "bounded only; cache-busting is a dead end")


def c_source_ownership() -> tuple[str, bool, str]:
    src = (ROOT / "services" / "ingest" / "main.py").read_text()

    def body_of(name: str) -> str:
        i = src.index("def " + name)
        j = src.index("\n    def ", i + 5)
        return src[i:j]

    # Only the REST-side functions are under suspicion. `on_message` noting `ws.tape` is the socket reporting
    # its own liveness, which is exactly what it is for; a check that forbade that would "fix" the product into
    # a worse shape, which is the failure mode of a gate written from a rule instead of from the code.
    rest_paths = [body_of(n) for n in ("poll_tape", "fetch_universe", "apply_universe", "resync")]
    ws_notes = sum(b.count('note_event("ws.') for b in rest_paths)
    tape_notes = body_of("poll_tape").count('note_event("data.trades"')
    return ("freshness ownership: the REST poller notes %d source(s) and `ws.*` is noted %d time(s) anywhere"
            % (tape_notes, ws_notes), tape_notes >= 1 and ws_notes == 0,
            "a poller that advances a socket's clock hides an outage — that bug cost a full chaos run"
            if ws_notes else "one freshness source per transport")


def c_sqlite_parity() -> tuple[str, bool, str]:
    rc, out = sh([PY, "tools/build-sqlite-migrations.py", "--check"], timeout=180)
    return ("sqlite subset is current", rc == 0, (out.strip().splitlines() or ["(no output)"])[-1][:160])


def c_alter_guard() -> tuple[str, bool, str]:
    """The generator must REFUSE to silently drop an ADD COLUMN, not record it and continue."""
    sys.path.insert(0, str(ROOT / "tools"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("bsm", ROOT / "tools" / "build-sqlite-migrations.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                                          # type: ignore[union-attr]
    dropped: list[str] = []
    try:
        mod.transpile("ALTER TABLE markets ADD COLUMN closed BOOLEAN NOT NULL DEFAULT FALSE;", dropped)
    except SystemExit as e:
        msg = str(e)
        return ("ADD-COLUMN drop is refused by the generator", "0007" in msg or "ALTER" in msg,
                "refused with a lesson attached: %s" % msg.splitlines()[0][:120])
    except Exception as e:  # any refusal is better than silence, but it must not be a crash we did not mean
        return ("ADD-COLUMN drop is refused by the generator", False, "unexpected %s: %s" % (type(e).__name__, e))
    return ("ADD-COLUMN drop is refused by the generator", False,
            "transpile accepted it and recorded %s — dev/CI would run a schema missing columns production has"
            % (dropped or "nothing"))


def c_resolution_model() -> tuple[str, bool, str]:
    pg = (PG_MIG / "0001_core.sql").read_text()
    at = pg.index("CREATE TABLE markets (")
    nxt = pg.find("CREATE TABLE", at + 10)
    markets_block = pg[at:nxt if nxt > 0 else len(pg)]
    has_closed = re.search(r"^\s*closed\s", markets_block, re.M) is not None
    nullable_winner = re.search(r"is_winner\s+BOOLEAN", pg) is not None
    src = (ROOT / "services" / "ingest" / "main.py").read_text()
    uses_resolved = "RESOLVED_SQL" in src and "m.closed" not in src
    return ("resolution model: no `markets.closed` (%s), one RESOLVED_SQL expression (%s)"
            % (not has_closed, uses_resolved),
            not has_closed and nullable_winner and uses_resolved,
            "`is_winner` exists as a nullable column: %s — NULL is the only legal value for an unresolved "
            "market, because 0 makes every open market a loss" % nullable_winner)


def c_rules_seed() -> tuple[str, bool, str]:
    rc, out = sh([PY, "tools/p05-seed-rules.py", "--dry-run"], timeout=120)
    dry_ok = rc == 0 and "rules validated" in out
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    fresh_db(db.name).close()
    rc2, out2 = sh([PY, "tools/p05-seed-rules.py", "--db", db.name, "--owner", "gate"], timeout=120)
    code = ('import sys,json,sqlite3;sys.path.insert(0,"%s");import main as M;'
            'st=M.Store(sqlite3.connect("%s"));r,e,c,o=st.load_rules();'
            'print(json.dumps({"n":len(r),"errors":len(e),"kinds":sorted(x.kind for x in r),"cd":len(c)}))'
            % (ROOT / "services" / "ingest", db.name))
    rc3, out3 = sh([PY, "-c", code], timeout=120)
    try:
        got = json.loads(out3.strip().splitlines()[-1])
    except Exception:
        got = {"n": 0, "errors": -1, "kinds": [], "cd": 0}
    os.unlink(db.name)
    uniq = "UNIQUE (rule_id, dedupe_key, fired_bucket)" in (PG_MIG / "0006_ingest.sql").read_text()
    ok = dry_ok and rc2 == 0 and got["n"] >= 6 and got["errors"] == 0 and uniq and got["cd"] >= 6
    return ("rules reach the daemon: dry-run %s, seed %d loaded, %d rejected, cooldowns %d, per-window UNIQUE %s"
            % ("ok" if dry_ok else "FAILED", got["n"], got["errors"], got["cd"], uniq), ok,
            "one alert per cooldown window is a constraint, not a code path" if uniq else
            "signals has no UNIQUE(rule_id,dedupe_key,fired_bucket)")


#: The eight kinds P05 shipped. Later phases add kinds; this check keeps the P05 set as a floor and names the rest.
P05_KINDS = ("large_fill", "volume_spike", "rapid_move", "new_market", "imbalance_flip",
             "negrisk_divergence", "watched_wallet", "resolution_imminent")


def c_signal_coverage() -> tuple[str, bool, str]:
    from polygm_core.signals import engine as E
    from polygm_core.classify import labels as L
    kinds = set(E.VALID_KINDS)
    defs = set(E.DEFAULTS)
    cd = {k: E.DEFAULTS[k].get("cooldown_s") for k in defs}
    names = set(getattr(L, "LABELS", []) or [x["name"] if isinstance(x, dict) else x.name for x in
                                              getattr(L, "LABEL_DEFS", [])]) if (
        hasattr(L, "LABELS") or hasattr(L, "LABEL_DEFS")) else set()
    if not names:                                                    # fall back to the documented six
        names = {n for n in ("whale", "smart_money", "new_wallet", "insider_suspect", "cluster", "wash_like")
                 if re.search(r"\b%s\b" % n, (ROOT / "packages" / "polygm_core" / "classify" / "labels.py")
                              .read_text())}
    src = (ROOT / "packages" / "polygm_core" / "classify" / "labels.py").read_text()
    controls = {
        "whale": "min_sample" in src and "percentile" in src.lower(),
        "smart_money": "min_settled" in src or "settled" in src,
        "new_wallet": "max_age" in src or "days" in src,
        "insider_suspect": "publishable=False" in src or "publishable': False" in src,
        "cluster": "cluster_min_members" in src,
        "wash_like": "roundtrip" in src,
    }
    missing = [k for k in E.VALID_KINDS if k not in defs]
    # `new_market` legitimately has no mute window: its dedupe key is per market, and a market is new once.
    no_cd = [k for k, v in cd.items() if v is None or (v == 0 and k != "new_market")]
    unl = [n for n, v in controls.items() if not v]
    # The property this check enforces is "every kind the engine computes has a default, a cooldown and a severity"
    # — not "there are eight of them". P10 D9 deliberately added three kinds (`price_level`, `spread_widen`,
    # `illiquid_top`) so the alerts screen could not sell a rule the engine cannot evaluate, and a hard-coded count
    # made this check wrong from that day on: `make check` had not completed since P12, so nobody saw it go red.
    # The extra kinds are now *named in the detail*, so growth is visible instead of fatal.
    added = sorted(kinds - set(P05_KINDS))
    detail_ok = set(P05_KINDS) <= kinds and not missing and not no_cd and len(names) == 6 and not unl
    return ("signals: %d kinds (%d from P05, %d added later%s) each with a default and a cooldown; "
            "labels: %d of 6 with their false-positive controls"
            % (len(kinds), len(P05_KINDS), len(added), (": " + ", ".join(added)) if added else "",
               6 - len(unl)),
            detail_ok,
            "missing: %s" % [missing, no_cd, sorted(set(P05_KINDS) - kinds), unl] if not detail_ok else
            "every kind has a default, a cooldown and a label control")


def c_clickhouse_answer() -> tuple[str, bool, str]:
    f = ROOT / "db" / "clickhouse" / "tape.sql"
    if not f.exists():
        return ("capacity answer: ClickHouse alternative documented", False, "db/clickhouse/tape.sql missing")
    text = f.read_text()
    body = "\n".join(l.split("--")[0] for l in text.split("\n"))
    stmts = [x for x in body.split(";") if x.strip()]
    tables = [x for x in stmts if "CREATE TABLE" in x]
    views = [x for x in stmts if "CREATE MATERIALIZED VIEW" in x]
    engines = sum(1 for x in tables if "ENGINE =" in x)
    ordered = sum(1 for x in tables if "ORDER BY" in x)
    # A materialised view has no ENGINE/ORDER BY of its own — it writes INTO a table — so it is counted under
    # `TO`, which is the clause that makes it correct. Lumping the two together would demand a schema the engine
    # rejects, and a gate that demands invalid DDL is worse than no gate.
    routed = sum(1 for x in views if " TO polygm." in x)
    measured = re.findall(r"\d+(?:\.\d+)? (?:fills/s|rows/s|fills/day|rows/day|MB|ms|B)\b", text)
    triggers = [m.group(0) for m in re.finditer(r"^\s*(?:--\s*)?([a-d])\)", text, re.M)]
    make = (ROOT / "Makefile").read_text() + (ROOT / "tools" / "run-sql.py").read_text()
    not_auto = "clickhouse" not in make.lower()
    return ("capacity answer: %d statements (%d tables + %d routed views), %d measured figures, %d switch "
            "triggers, never auto-applied: %s" % (len(stmts), len(tables), len(views), len(measured),
                                                 len(triggers), not_auto),
            len(stmts) >= 4 and engines == len(tables) and ordered == len(tables)
            and routed == len(views) and len(views) >= 1 and len(measured) >= 3
            and len(triggers) >= 4 and not_auto and body.count("(") == body.count(")"),
            "the answer is Postgres; the file has to be the priced alternative, so it needs numbers, triggers "
            "and DDL that would actually run" if not (len(measured) >= 3 and len(triggers) >= 4) else
            "decision, costs and the conditions that would reverse it are all present")


def c_docs_consistency() -> tuple[str, bool, str]:
    doc = ROOT / "docs" / "P05-data-ingestion.md"
    if not doc.exists():
        return ("phase doc agrees with the artifact", False, "docs/P05-data-ingestion.md missing")
    text = doc.read_text()
    art = ARTIFACT.read_text() if ARTIFACT.exists() else ""
    # A doc may QUOTE the run; it may not CLAIM the run. The difference is mechanical: inside a fenced block the
    # words are evidence (and must appear in the artifact verbatim), in prose they are a summary somebody can
    # write on a day the run failed. The first version of this check only forbade claiming a pass the artifact
    # did not show, so it could not see a fabricated quote at all — a mutant proved that, and it is recorded here
    # so the next person does not "simplify" it back.
    prose = re.sub(r"```.*?```", "", text, flags=re.S)
    claims_pass = "ALL 7 CHECKS PASSED" in prose or re.search(r"\b7/7\b", prose) is not None
    quoted = re.findall(r"ALL 7 CHECKS PASSED[^\n]*", text)
    verbatim = all(q.strip() in art for q in quoted)
    honest = (not claims_pass) and verbatim
    has_measured = "## Measured" in text
    has_correction = "IN PROGRESS" not in text or "Still open" not in text
    refs = "P05-chaos-output.txt" in text
    stale_claim = "IN PROGRESS" in text
    return ("phase doc: measured section %s, run quoted as evidence not claimed as prose (%s), %d quoted line(s) "
            "verbatim in the artifact (%s), references the artifact (%s)"
            % (has_measured, honest, len(quoted), verbatim, refs),
            honest and has_measured and refs and not stale_claim and verbatim,
            "a doc that summarises a live run in prose will be wrong the first time the run fails; quote it, or "
            "say nothing" if not honest else ("the doc's quoted verdict does not appear in the artifact file"
                                              if not verbatim else "no overclaim, and the evidence is linked"))


def c_make_honesty() -> tuple[str, bool, str]:
    make = (ROOT / "Makefile").read_text()
    swallowed = re.findall(r"^\t.*(check|probe|gate).*\|\|\s*echo", make, re.M)
    has_p05 = re.search(r"^gate-p05:", make, re.M) and re.search(r"^p05:", make, re.M)
    probe = re.search(r"probe-fresh:\n((?:\t[^\n]*\n?)+)", make)
    probe_ok = bool(probe) and "|| echo" not in probe.group(1) and "exit" in probe.group(1)
    return ("make: p05 + gate-p05 targets present (%s), no check piped into a status-swallowing tail (%d), "
            "probe-fresh propagates its code (%s)" % (bool(has_p05), len(swallowed), probe_ok),
            bool(has_p05) and not swallowed and probe_ok,
            "a gate whose exit code is eaten by `|| echo` is how the cache-busting conclusion survived a whole "
            "phase" if not probe_ok else "exit statuses travel")


def c_lint() -> tuple[str, bool, str]:
    rc, out = sh([PY, "tools/lint-rules.py"], timeout=300)
    tail = [l for l in out.splitlines() if "FAIL" in l]
    return ("lint rules (money-no-float, no-secrets, order-via-gate, ...)", rc == 0,
            "; ".join(t[:120] for t in tail) if tail else (out.strip().splitlines() or [""])[-1][:120])


def main() -> int:
    ap = argparse.ArgumentParser(description="P05 quality gate")
    ap.add_argument("--live", action="store_true", help="also run the 300s outage and the fixture drift check")
    ap.add_argument("--fast", action="store_true", help="alias for the default (no network)")
    ap.add_argument("--list", action="store_true", help="print the rules without running them")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    checks = [c_suite, c_lint, c_dedupe_constraint, c_bounded_tape, c_source_ownership, c_sqlite_parity,
              c_alter_guard, c_resolution_model, c_rules_seed, c_signal_coverage, c_clickhouse_answer,
              c_docs_consistency, c_make_honesty]
    if a.live:
        checks = [c_artifact, c_live_chaos, c_fixture_drift] + checks
    else:
        checks = [c_artifact] + checks
    if a.list:
        for fn in checks:
            print("  %-24s %s" % (fn.__name__, (fn.__doc__ or "").strip().split("\n")[0][:88]))
        print("%d checks, %s" % (len(checks), "including the live outage" if a.live else "offline; --live adds the run"))
        return 0
    results = []
    for fn in checks:
        try:
            label, ok, detail = fn()
        except Exception as e:                                    # a crashing check is a FAIL, never a skip
            label, ok, detail = fn.__name__, False, "check raised %s: %s" % (type(e).__name__, e)
        results.append((label, ok, detail))
        print("  %-5s %s\n        %s" % ("PASS" if ok else "FAIL", label, detail))
    passed = sum(1 for _l, ok, _d in results if ok)
    payload = {"phase": "P05", "passed": passed, "total": len(results), "live": a.live,
               "checks": [{"label": l, "ok": ok, "detail": d} for l, ok, d in results]}
    if a.json:
        print(json.dumps(payload, indent=2))
    print("\nP05 gate: %d/%d checks passed" % (passed, len(results)))
    if passed != len(results):
        print("gate is a floor: any FAIL here means the phase is not done, whatever the prose elsewhere says")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
