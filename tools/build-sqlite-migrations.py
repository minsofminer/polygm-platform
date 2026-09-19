#!/usr/bin/env python3
"""Transpile the Postgres migrations into the SQLite subset the dev/CI harness runs on.

Why generated rather than hand-maintained: a second hand-written copy drifts, and the drift shows up as
"tests pass, production would fail". Every transformation here is a *removal or a type widening*; when a
construct cannot be carried over faithfully the transpiler drops it and RECORDS the drop in
db/migrations-sqlite/DROPPED.json, so the CI gate can see exactly which Postgres-only invariants the test
suite is NOT exercising instead of assuming it was. That record is the difference between a portable
subset and a silent lie.

Three lessons are encoded in the implementation, each from a bug that cost time in this build:
  1. Comments are stripped. SQLite's tokenizer ends a statement at a `;` *inside* a `--` comment, so the
     Postgres files' prose cannot be carried through verbatim.
  2. Comma repair is anchored on a sentinel left exactly where a line was dropped. A general `,\n)` regex
     silently deleted legal commas before `PRIMARY KEY` clauses and produced SQL that failed at runtime.
  3. Table-level CHECK clauses are moved to the end of the column list: SQLite reads a mid-list CHECK as a
     column definition and errors on the NEXT line, while Postgres accepts it. See reorder_checks().
"""
from __future__ import annotations
import argparse, json, re, sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC, DST = ROOT / "db" / "migrations", ROOT / "db" / "migrations-sqlite"
SENTINEL = "@@DROPPED@@"


# P07 joins the list with the tables that are *only ever inserted*: the auth trail, the abuse findings, the
# broadcast gate's decisions, and the two readiness records. Tables that converge in place — `revoke_jobs`
# (progress), `key_wraps` (rotation), `kek_versions` (retirement), `route_auth_levels` (a mirror of the code),
# `withdrawal_addresses` (removal), `incident_findings` (open -> mitigated) — are deliberately NOT here: an
# append-only promise on a state machine is a bug report waiting to happen, and the honest alternative is a
# history table, which P06 already has for the states that need one.
APPEND_ONLY = ["cash_ledger", "fills", "tape_trades", "builder_attribution", "position_snapshots",
               "auth_events", "wash_findings", "broadcast_gates", "backup_restore_tests", "drill_records",
               "audit_log", "flag_audit", "alert_fires", "kill_switch_state", "referral_events",
               # P06: the trading plane's evidence tables. Each one exists so a decision made at 3am can be
               # read back; a table you can UPDATE is a table you cannot trust after an incident.
               "wallet_events", "order_lifecycle", "reconcile_actions", "copy_events", "automation_runs",
               "chain_events", "flag_audit_p06", "kill_switch_drills",
               # P10: the copy engine's would-be actions and the history of a trader's score. Both are
               # evidence read back after the fact — "why did it skip that fill" and "why did this wallet
               # stop being smart money" — so neither may be rewritten.
               "copy_dry_runs", "trader_metric_snapshots", "wallet_pseudonyms",
               # P11: an exclusion is a decision about somebody's standing, and the question after an incident is
               # "who removed this wallet, when, and why" — which an UPDATE cannot answer.
               "leaderboard_exclusions"]
API_TABLES = {"markets", "events", "tokens", "book_levels", "tape_trades", "users", "idempotency_keys",
              "kill_switch_state", "order_intents", "orders", "fills", "cash_ledger", "position_lots",
              "position_snapshots", "builder_attribution", "feature_flags", "flag_audit", "audit_log",
              "entitlements", "alert_rules", "alert_fires", "copy_configs", "automation_rules",
              "watchlists", "watchlist_items", "referrals", "referral_events", "subscriptions", "balances"}

TYPE_MAP = [
    (r"\bBIGSERIAL\b", "INTEGER"),        # INTEGER PRIMARY KEY is the rowid alias => autoincrement
    (r"\bBIGINT\b", "INTEGER"),
    (r"\bSMALLINT\b", "INTEGER"),
    (r"\bINT\b", "INTEGER"),
    (r"\bBOOLEAN\b", "INTEGER"),
    (r"\bTIMESTAMPTZ\b", "INTEGER"),
    (r"\bJSONB\b", "TEXT"),
    (r"\bNUMERIC\(\d+\s*,\s*\d+\)", "REAL"),
]
PG_ONLY_STATEMENTS = ("CREATE EXTENSION", "CREATE TRIGGER", "CREATE OR REPLACE FUNCTION", "DO $$",
                      "REVOKE", "GRANT", "ALTER TABLE", "COMMENT ON")
TABLE_LEVEL_OK_MIDLIST = ("PRIMARY KEY", "UNIQUE (", "FOREIGN KEY")


def comment_line(line: str) -> bool:
    """True when a line is comment-only, including a block comment's *continuation* lines, which carry no
    `--` of their own after the first line ("  this is *metadata*, not money")."""
    return line.strip().startswith("--")


def strip_inline_comments(text: str, keep_blank: bool = True) -> str:
    """Truncate every line at its own `--`, quote-aware, with the state carried ACROSS lines.

    Per-line quote counting is wrong: `DEFAULT '[]',` has one apostrophe pair open and closed in the same
    line, but a line like `CHECK (kind = 'auto_redeem' OR ...)` makes a naive count flip, and the next
    line's `--` then looks like it is inside a string. So the state is threaded through the file.
    """
    out, in_str = [], False
    for line in text.splitlines():
        cut, buf, i = None, "", 0
        while i < len(line):
            ch = line[i]
            if ch == "'":
                in_str = not in_str
            elif not in_str and line.startswith("--", i):
                cut = i
                break
            buf += ch
            i += 1
        line = (buf if cut is not None else line).rstrip()
        if line.strip() or keep_blank:
            out.append(line)
    return "\n".join(out)


def _split_top_level(inner: str) -> tuple[list[str], str]:
    """Split a column list on top-level commas. Returns (parts, trailing_text_after_last_comma)."""
    parts, cur, d = [], "", 0
    for ch in inner:
        cur += ch
        if ch == "(":
            d += 1
        elif ch == ")":
            d -= 1
        elif ch == "," and d == 0:
            parts.append(cur)
            cur = ""
    return parts, cur


def _clean(part: str) -> str:
    return "\n".join(l for l in part.splitlines() if l.strip()).strip().rstrip(",").strip()


def reorder_checks(sql: str, moved: list[str]) -> str:
    """Move table-level CHECK clauses after the column definitions in every CREATE TABLE.

    Semantics-preserving (constraints are a conjunction); recorded in `moved` so the normalisation is
    auditable. Only applied to the derived subset - the Postgres source keeps the author's ordering.
    """
    pieces, i = [], 0
    for m in re.finditer(r"CREATE TABLE \w+ \(", sql):
        pieces.append(sql[i:m.end()])        # everything up to and INCLUDING the open paren
        depth, j = 0, m.end() - 1
        while j < len(sql):
            if sql[j] == "(":
                depth += 1
            elif sql[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        raw_inner = sql[m.end():j]
        # A dropped clause is replaced by a sentinel LINE, and a line is not a column. Two things then go
        # wrong if the sentinel is handled naively: it glues itself to the next column and drags that column
        # away with it (markets silently lost first_seen_ms), or - when it sits INSIDE a multi-line clause -
        # it is mistaken for "the last column was dropped", the separator comma is removed, and the table is
        # truncated at a `,)`. So: (a) mark each sentinel with what follows it, (b) drop the line, (c) only
        # touch a comma when a real clause boundary follows.
        raw_lines = raw_inner.splitlines()
        closes_block = []
        for idx, l in enumerate(raw_lines):
            if l.strip() != SENTINEL:
                continue
            nxt = next((raw_lines[k].strip() for k in range(idx + 1, len(raw_lines))
                        if raw_lines[k].strip()), ")")
            closes_block.append(nxt.startswith(")"))
        inner = "\n".join(l for l in raw_lines if l.strip() != SENTINEL)
        parts, tail = _split_top_level(inner)
        parts = parts + ([tail] if tail.strip() else [])
        # a sentinel that ended the column list leaves `col TYPE,` with a comma that must go; one that ended
        # mid-list must leave its predecessor's comma ALONE.
        if closes_block and closes_block[-1]:
            for k in range(len(parts) - 1, -1, -1):
                if parts[k].strip():
                    parts[k] = parts[k].rstrip()[: -1] if parts[k].rstrip().endswith(",") else parts[k]
                    break
        checks, cols = [], []
        for pi, part in enumerate(parts):
            body = _clean(part)
            # a dropped column can leave a comment's continuation lines glued to the next column's clause;
            # they are comment-only text and must never be mistaken for a column definition.
            body = "\n".join(l for l in body.splitlines() if not comment_line(l)).strip()
            if not body:
                continue
            if SENTINEL in body:
                # A dropped column/constraint. No comma repair is needed here because this function
                # rebuilds the column list from comma-free parts - which is exactly why the repair lives
                # here rather than in a regex over the finished text (that version corrupted valid SQL).
                continue
            if re.match(r"^(CONSTRAINT \w+\s+)?CHECK\s*\(", body, re.I):
                checks.append(body)
            else:
                cols.append(body)
        if checks:
            moved.append(re.search(r"CREATE TABLE (\w+)", m.group(0)).group(1))
        # rebuild whenever something was dropped OR checks were moved; a leaked sentinel is worse than a
        # cosmetic re-indent, so the rebuild is the default path when the two never coincide.
        if checks or len(cols) + len(checks) != len([p for p in parts if _clean(p)]):
            rebuilt = ",\n".join("    " + c for c in cols + checks)
            pieces.append("\n" + rebuilt + "\n")
        else:
            pieces.append(inner)
        pieces.append(sql[j:j + 1])       # just the closing ')'
        i = j + 1
    pieces.append(sql[i:])
    return "".join(pieces)


def transpile(text: str, dropped: list[str]) -> str:
    out: list[str] = []
    skip_until_semi = False
    in_dollar = False
    # P07 found the hole: the PG-only test below is a *prefix* test, and this loop reads lines, not
    # statements. A column whose name begins with a PG-only keyword - `revoked_ms`, `revoked_reason`,
    # `revoked`, `grant_id`, `comment_count` - was classified as `REVOKE`/`COMMENT ON`/`GRANT`, its line
    # dropped, and `skip_until_semi` then ate the rest of the table up to the next `;`. Six columns across
    # 0009 vanished while the file still "executed", which is precisely the silently-smaller subset this
    # tool exists to prevent. Tracking paren depth means the test only fires where a statement can start.
    depth = 0
    for raw in strip_inline_comments(text).splitlines():
        st = raw.strip()
        at_statement_start = depth == 0
        if in_dollar:
            if "$$" in st:
                in_dollar = False
                skip_until_semi = not st.endswith(";")
            continue
        if skip_until_semi:
            if st.endswith(";"):
                skip_until_semi = False
            continue
        if not st:
            continue
        up = st.upper()
        depth += st.count("(") - st.count(")")
        if depth < 0:
            depth = 0

        if at_statement_start and up.startswith(PG_ONLY_STATEMENTS):
            # A column added by ALTER is not in the same class as a trigger or a deferrable constraint: dropping
            # it leaves dev/CI running a schema that is MISSING a column production has, and the code that reads
            # it fails in whichever test gets there first. "Portable subset" must never mean "silently smaller".
            if "ADD COLUMN" in up:
                raise SystemExit("0007 lesson: `ALTER TABLE ... ADD COLUMN` cannot go to the sqlite subset —\n"
                                 "  sqlite drops it and the two engines then disagree about the schema. Put the\n"
                                 "  columns in a table of this phase's own (see 0007_ingest_market_stats.sql) or\n"
                                 "  add them to the base migration if no database has ever been deployed with it.")
            dropped.append("PG-only statement: " + st[:78])
            in_dollar = "$$" in st
            skip_until_semi = (";" not in st) and not in_dollar
            continue
        if at_statement_start and up.startswith(("CREATE UNIQUE INDEX", "CREATE INDEX")):
            if " WHERE " in up or "DATE_TRUNC" in up or "LOWER(" in up:
                dropped.append("partial/expression index: " + st[:70])
                skip_until_semi = ";" not in st
                continue
            out.append(raw.replace(" DESC", ""))
            continue

        keep = raw
        if re.search(r"GENERATED ALWAYS AS", keep, re.I) and re.search(r"jsonb|date_trunc", keep, re.I):
            dropped.append("generated column over a PG-only function - the service computes it on sqlite")
            out.append(SENTINEL)
            continue
        if re.search(r"jsonb_typeof|detail_json::text", keep, re.I):
            dropped.append("CHECK using a PG-only expression (asserted in the API/CI instead): " + st[:60])
            # A COLUMN LINE that carries such a CHECK (`detail_json JSONB NOT NULL DEFAULT '{}'
            # CHECK (jsonb_typeof(...))`) must survive with its CHECK removed - emitting a bare sentinel
            # here deleted the whole column, and the portable subset silently lost detail_json while still
            # "executing". Column-preserving is the only correct behaviour for a column line.
            col = re.match(r"^\s*(\w+\s+\w+)", keep)
            if col and not re.match(r"^\s*CONSTRAINT\b", keep, re.I):
                keep = re.sub(r"\s*CONSTRAINT \w+\s+CHECK \s*\(.*$", "", keep, flags=re.I).rstrip()
                keep = re.sub(r"\s+CHECK \s*\(.*$", "", keep, flags=re.I).rstrip()
                keep = keep.rstrip(",").rstrip()
                out.append(keep + ("," if st.rstrip().endswith(",") else ""))
                continue
            out.append(SENTINEL)
            continue
        keep = re.sub(r"\s+unique \(", " UNIQUE (", keep, flags=re.I)
        mu = re.match(r"\s*(?:CONSTRAINT \w+\s+)?UNIQUE\s*\((.*)\)\s*,?\s*$", keep, re.I)
        if mu and re.search(r"[A-Za-z_]\w*\s*\(", mu.group(1)):
            cells = [c.strip() for c in mu.group(1).split(",")]
            exprs = [c for c in cells if re.search(r"[A-Za-z_]\w*\s*\(", c)]
            plain = [c for c in cells if not re.search(r"[A-Za-z_]\w*\s*\(", c)]
            if not plain:
                dropped.append("UNIQUE over expressions only (%s) - dropped; the service enforces it"
                               % ", ".join(exprs))
                out.append(SENTINEL)
                continue
            dropped.append("expression UNIQUE(%s) -> plain UNIQUE(%s): sqlite forbids expressions in "
                           "UNIQUE/PRIMARY KEY" % (", ".join(exprs), ", ".join(plain)))
            keep = "    UNIQUE (%s)%s" % (", ".join(plain), "," if keep.rstrip().endswith(",") else "")

        keep = re.sub(r"\s+ON DELETE (CASCADE|SET NULL|RESTRICT)", "", keep, flags=re.I)
        if re.search(r"DEFAULT\s+\(floor\(extract\(epoch", keep, re.I):
            keep = re.sub(r"\(floor\(extract\(epoch from now\(\)\) \* 1000\)::BIGINT\)",
                          "(CAST(strftime('%s','now') AS INTEGER) * 1000)", keep, flags=re.I)
        for pat, rep in TYPE_MAP:
            keep = re.sub(pat, rep, keep, flags=re.I)
        keep = keep.replace("::BIGINT", "").replace("::TEXT", "").replace("::JSONB", "")
        out.append(keep)

    out = [l for l in out if l.strip()]        # drop blanks BEFORE indexing: the repair below works on
                                                # adjacency, and a blank between a column and the sentinel
                                                # made it skip its own edits (seen as a leaked @@DROPPED@@)
    # sentinel-anchored comma repair (see module docstring lesson 2)
    for i, l in enumerate(out):
        if l.strip() != SENTINEL:
            continue
        prev = next((j for j in range(i - 1, -1, -1) if out[j].strip()), None)
        nxt = next((j for j in range(i + 1, len(out)) if out[j].strip()), None)
        if prev is None or nxt is None:
            continue
        nxt_s = out[nxt].strip()
        if nxt_s.startswith(")"):
            out[prev] = out[prev].rstrip().rstrip(",")
        elif not out[prev].rstrip().endswith(","):
            out[prev] = out[prev].rstrip() + ","
        if re.match(r"(CHECK|CONSTRAINT)\b", nxt_s) and not nxt_s.rstrip().endswith(","):
            out[nxt] = out[nxt].rstrip() + ","
    body = "\n".join(l for l in out if l.strip())
    return body + "\n"


def build() -> tuple[dict[str, str], dict[str, dict]]:
    rendered, manifest = {}, {}
    for src in sorted(SRC.glob("*.sql")):
        dropped: list[str] = []
        body = transpile(src.read_text(), dropped)
        moved: list[str] = []
        body = reorder_checks(body, moved)
        header = ("-- GENERATED by tools/build-sqlite-migrations.py from db/migrations/%s -- do not edit.\n"
                  "-- Dev/CI portable subset; Postgres remains the product database.\n\n" % src.name)
        # comments are removed by comment_line() *inside* reorder_checks, not by a second global strip: a
        # second strip re-computes its apostrophe parity over lines that are already clean, which flipped
        # parity at `DEFAULT '[]',` and ate the next three columns (the first run of test_migrations caught
        # `first_seen_ms` vanishing from markets). Parity state must not be rebuilt over already-processed
        # text.
        leftovers = sorted(set(__import__("re").findall(
            r"\bJSONB\b|\bBIGSERIAL\b|\bNUMERIC\(\d+,\s*\d+\)|\bTIMESTAMPTZ\b|\bSMALLINT\b",
            __import__("re").sub(r"^--.*$", "", header + body, flags=__import__("re").M))))
        if leftovers:
            raise SystemExit("%s: PG-only type names survived into the portable subset: %s. The transpiler "
                             "widened them in the lines it kept and did not widen them in text it re-spliced "
                             "after a clause move; SQLite tolerates unknown type names, so this is silent "
                             "until a CHECK or an index disagrees between engines."
                             % (src.name, ", ".join(leftovers)))
        lost = check_parity(src.name, header + body)
        if lost:
            # P06 found the case: a column-level CHECK written across several lines (`event TEXT NOT NULL
            # CHECK (event IN\n  (...))`) was dropped by the line-based clause mover in silence, so the
            # portable subset quietly enforced nothing while the Postgres source enforced everything. Any
            # dropped constraint is now a build failure, because a subset that is smaller than its source is
            # only honest if the subset SAYS so — and this one could not, since a CHECK clause is not a
            # droppable construct the way a trigger is.
            raise SystemExit("%s: CHECK clauses present in the Postgres source are missing from the generated "
                             "file for: %s. Write the constraint as a TABLE-level CHECK on its own line "
                             "(a column-level CHECK whose body spans lines is not portable through this "
                             "parser)." % (src.name, ", ".join(lost)))
        rendered[src.name] = header + body
        manifest[src.name] = {"dropped": dropped, "check_clauses_reordered": moved}
    return rendered, manifest


def _table_bodies(sql: str) -> dict[str, str]:
    """`CREATE TABLE x (...)` -> the text inside the parens, by bracket depth (a comment-free scan)."""
    out: dict[str, str] = {}
    for m in re.finditer(r"CREATE TABLE (\w+) \(", sql):
        depth, j = 0, m.end() - 1
        while j < len(sql):
            if sql[j] == "(":
                depth += 1
            elif sql[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out[m.group(1)] = sql[m.end():j]
    return out


def check_parity(name: str, rendered_sql: str) -> list[str]:
    """Tables whose CHECK count fell during transpilation. Compares the Postgres source of the same file."""
    src = (SRC / name).read_text()
    src_sql = "\n".join(l for l in src.splitlines() if not comment_line(l))
    src_sql = strip_inline_comments(src_sql)
    a = {t: v.count("CHECK (") + v.count("CHECK(") for t, v in _table_bodies(src_sql).items()}
    b = {t: v.count("CHECK (") + v.count("CHECK(") for t, v in _table_bodies(rendered_sql).items()}
    return sorted(t for t in a if b.get(t, 0) < a[t])


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    a = ap.parse_args()
    DST.mkdir(parents=True, exist_ok=True)
    rendered, manifest = build()

    if a.write:
        for name, txt in rendered.items():
            (DST / name).write_text(txt)

    con = sqlite3.connect(":memory:")
    try:
        for name in sorted(rendered):
            con.executescript(rendered[name])
    except Exception as e:                                   # noqa: BLE001 - reported, not swallowed
        print("generated SQL does not execute: %s: %s" % (type(e).__name__, e))
        return 1
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()

    emit = []
    for t in APPEND_ONLY:
        if t in have:
            emit.append("CREATE TRIGGER append_only_%s_update BEFORE UPDATE ON %s BEGIN "
                        "SELECT RAISE(ABORT,'append-only table: %s is not updatable'); END;" % (t, t, t))
            emit.append("CREATE TRIGGER append_only_%s_delete BEFORE DELETE ON %s BEGIN "
                        "SELECT RAISE(ABORT,'append-only table: %s is not deletable'); END;" % (t, t, t))
    append_txt = ("-- GENERATED by tools/build-sqlite-migrations.py -- do not edit.\n"
                  "-- append-only enforcement for the portable subset, mirroring 0005_triggers.sql.\n\n"
                  + "\n\n".join(emit) + "\n")
    if a.write:
        (DST / "_append_only.sql").write_text(append_txt)
        (DST / "DROPPED.json").write_text(json.dumps(
            {"append_only": APPEND_ONLY, "tables": sorted(have), "files": manifest}, indent=2) + "\n")

    stale = [n for n, t in rendered.items() if not (DST / n).exists() or (DST / n).read_text() != t]
    if not (DST / "_append_only.sql").exists() or (DST / "_append_only.sql").read_text() != append_txt:
        stale.append("_append_only.sql")
    if stale and not a.write:
        print("STALE generated files (run tools/build-sqlite-migrations.py --write): "
              + ", ".join(sorted(stale)))
        return 1

    con = sqlite3.connect(":memory:")
    con.executescript("PRAGMA foreign_keys = ON;\n"
                      + "\n".join((DST / n).read_text() for n in sorted(rendered)) + "\n" + append_txt)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    triggers = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    con.close()

    missing = sorted(API_TABLES - tables)
    need_trig = {f"append_only_{t}_{k}" for t in APPEND_ONLY for k in ("update", "delete")}
    missing_trig = sorted(need_trig - triggers)
    reordered = sorted({t for v in manifest.values() for t in v["check_clauses_reordered"]})
    print("portable subset executes: %d tables, %d triggers (%d files)"
          % (len(tables), len(triggers), len(rendered) + 1))
    print("tables the API needs:", "all present" if not missing else "MISSING " + ",".join(missing))
    print("append-only triggers:", "all present" if not missing_trig else "MISSING " + ",".join(missing_trig))
    print("CHECK clauses moved to end of column list (sqlite parser quirk): %s"
          % (", ".join(reordered) if reordered else "none"))
    print("recorded PG-only drops: %d (db/migrations-sqlite/DROPPED.json)"
          % sum(len(v["dropped"]) for v in manifest.values()))
    if missing or missing_trig:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
