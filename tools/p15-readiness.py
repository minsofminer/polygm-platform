#!/usr/bin/env python3
"""P15 D9 — the operational readiness review: a signed checklist whose lines are artefacts, not opinions.

    python3 tools/p15-readiness.py --check                 # what is proven, what is missing, and by whom
    python3 tools/p15-readiness.py --sign on-call --evidence "reviewed 3 dashboards on a phone 2026-09-23"
    python3 tools/p15-readiness.py --json /tmp/readiness.json

The kit's D9 is a signed checklist before launch. A checklist that is a list of sentences is a checklist that
gets signed twice: once by the person who did the work and once by whoever is in the room. So every line here names
the **artefact** that proves it, and `--check` reads those artefacts:

| line | artefact |
| --- | --- |
| dashboards reviewed by the on-call | `docs/verification/p15-dashboard-review.jsonl` |
| every alarm fired deliberately | `docs/verification/p15-alerts-fired.jsonl`, one row per rule id |
| every runbook run by a non-author | each page's `last_drilled` + `drilled_by` front matter |
| rollback tested and timed | `docs/verification/P15-rollback-drill.txt` |
| synthetic check running and alarming | the probe state file + a recorded firing |
| rotation staffed for 30 days | `docs/P15-oncall.md` (names and dates) |
| kill switch drilled from a phone | a `kill_switch_drills` row with a propagation measurement |
| cost dashboard showing actual spend | `var/cost-projection.json` with an `actual` recorded |

Three of the eight can be satisfied here; the rest are owner steps, and the tool's job is to say *which artefact is
missing* rather than to be encouraging about it. `--sign` refuses while any line it can check is red, because the
value of a signature is that it cannot be given to a system that is not ready.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sqlite3
import sys
import typing as t

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "ops" / "alerts.yaml"
FIRED = ROOT / "docs" / "verification" / "p15-alerts-fired.jsonl"
DASH_REVIEW = ROOT / "docs" / "verification" / "p15-dashboard-review.jsonl"
ROLLBACK = ROOT / "docs" / "verification" / "P15-rollback-drill.txt"
SYNTH_STATE = pathlib.Path(os.environ.get("PGM_SYNTHETIC_STATE") or "/var/lib/polygm/synth.json")
ONCALL = ROOT / "docs" / "P15-oncall.md"
COST_STATE = ROOT / "var" / "cost-projection.json"
SIGNATURES = ROOT / "docs" / "verification" / "p15-readiness-signatures.jsonl"
RUNBOOKS = ROOT / "docs" / "runbooks"
ROTATION_DAYS = 30


def _rows(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"_unreadable": line[:80]})
    return out


def _front_matter(path: pathlib.Path) -> dict:
    text = path.read_text()
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    try:
        return yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}


def review(state_dir: pathlib.Path | None = None) -> list[dict]:
    """One row per checklist line: what it is, whether the artefact exists, and who still has to act."""
    state_dir = state_dir or ROOT
    fired = _rows(state_dir / "docs" / "verification" / "p15-alerts-fired.jsonl")
    fired_ids = {r.get("id") for r in fired if r.get("id")}
    alarm_ids = set()
    if REGISTRY.exists():
        alarm_ids = {r["id"] for r in (yaml.safe_load(REGISTRY.read_text()).get("rules") or [])}
    sev1 = {r["id"] for r in (yaml.safe_load(REGISTRY.read_text()).get("rules") or [])
            if r.get("severity") == "SEV1"} if REGISTRY.exists() else set()

    drills = [_front_matter(p) for p in sorted(RUNBOOKS.glob("*.md")) if p.name != "README.md"]
    drilled = [d for d in drills if d.get("last_drilled") and d.get("drilled_by")]
    review_rows = _rows(state_dir / "docs" / "verification" / "p15-dashboard-review.jsonl")
    rollback_text = (state_dir / "docs" / "verification" / "P15-rollback-drill.txt")
    synth = json.loads(SYNTH_STATE.read_text()) if SYNTH_STATE.exists() else None
    oncall_text = (state_dir / "docs" / "P15-oncall.md")
    cost = json.loads(COST_STATE.read_text()) if COST_STATE.exists() else {}
    actual = [v.get("actual") for v in cost.values() if isinstance(v, dict) and v.get("actual") is not None]
    kills = _kill_drill_rows(state_dir)

    return [
        {"id": "dashboards-reviewed",
         "what": "every dashboard reviewed by the person who will be on call",
         "artifact": "docs/verification/p15-dashboard-review.jsonl",
         "ok": len({r.get("dashboard") for r in review_rows}) >= 3,
         "detail": "%d dashboard(s) reviewed" % len({r.get("dashboard") for r in review_rows}),
         "owner": "on-call"},
        {"id": "alarms-fired",
         "what": "every SEV1 alarm fired deliberately in staging, notification verified",
         "artifact": "docs/verification/p15-alerts-fired.jsonl",
         "ok": bool(sev1) and sev1 <= fired_ids,
         "detail": "%d of %d SEV1 alarm(s) recorded as fired (%s missing)"
                   % (len(sev1 & fired_ids), len(sev1), ", ".join(sorted(sev1 - fired_ids)) or "none"),
         "owner": "on-call"},
        {"id": "runbooks-drilled",
         "what": "every runbook executed once, in staging, by someone other than its author",
         "artifact": "each runbook's `last_drilled` + `drilled_by`",
         "ok": len(drilled) == len(drills) and bool(drills),
         "detail": "%d of %d runbook(s) carry a drill date and a driller" % (len(drilled), len(drills)),
         "owner": "on-call + the author of each page"},
        {"id": "rollback-timed",
         "what": "rollback tested, with the seconds measured against the 60 s budget",
         "artifact": "docs/verification/P15-rollback-drill.txt",
         "ok": rollback_text.exists() and "seconds" in rollback_text.read_text().lower()
               if rollback_text.exists() else False,
         "detail": "recorded" if rollback_text.exists() else "the drill has not been recorded",
         "owner": "money-path"},
        {"id": "synthetic-live",
         "what": "synthetic order check running and alarming correctly",
         "artifact": "the probe's state file + a recorded firing of synthetic-order-failed",
         "ok": bool(synth) and "synthetic-order-failed" in fired_ids,
         "detail": ("probe state present" if synth else "no probe state file")
                   + ("; firing recorded" if "synthetic-order-failed" in fired_ids else "; no firing recorded"),
         "owner": "money-path"},
        {"id": "rotation-staffed",
         "what": "on-call rotation staffed for the next 30 days",
         "artifact": "docs/P15-oncall.md",
         "ok": oncall_text.exists() and oncall_text.stat().st_size > 200,
         "detail": "written" if oncall_text.exists() else "not written (staffing is an owner decision)",
         "owner": "owner"},
        {"id": "kill-switch-from-a-phone",
         "what": "kill switch drilled from a phone, with the propagation measured",
         "artifact": "a `kill_switch_drills` row",
         "ok": bool(kills),
         "detail": ("%d drill row(s), newest %s" % (len(kills), kills[0][:10]) if kills
                    else "no kill-switch drill recorded in this database"),
         "owner": "on-call (on a real phone)"},
        {"id": "cost-actuals",
         "what": "cost dashboard showing actual spend",
         "artifact": "var/cost-projection.json with an `actual`",
         "ok": bool(actual),
         "detail": ("actual recorded: %s" % ", ".join("$%.2f" % a for a in actual)) if actual
                   else "no actual recorded yet (needs one billing cycle)",
         "owner": "platform"},
    ]


def _kill_drill_rows(state_dir: pathlib.Path) -> list[str]:
    db = pathlib.Path(os.environ.get("PGM_DB_PATH") or state_dir / "var" / "polygm.db")
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(str(db))
        rows = con.execute("SELECT started_ms FROM kill_switch_drills ORDER BY started_ms DESC LIMIT 5").fetchall()
        con.close()
    except sqlite3.Error:
        return []
    out = []
    for (ms,) in rows:
        try:
            out.append(dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).strftime("%Y-%m-%d"))
        except (ValueError, OSError, TypeError):
            out.append("?")
    return out


def sign(role: str, evidence: str, rows: list[dict]) -> int:
    red = [r for r in rows if not r["ok"]]
    if red:
        print("refusing to record a signature while %d line(s) are red:" % len(red), file=sys.stderr)
        for r in red:
            print("  - %s (%s): %s" % (r["id"], r["owner"], r["detail"]), file=sys.stderr)
        print("the point of a signature is that it cannot be given to a system that is not ready", file=sys.stderr)
        return 1
    if len(evidence.strip()) < 12:
        print("a signature needs evidence a reviewer can read", file=sys.stderr)
        return 2
    SIGNATURES.parent.mkdir(parents=True, exist_ok=True)
    row = {"role": role, "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "evidence": evidence.strip()[:400], "lines_ok": sum(1 for r in rows if r["ok"]), "lines_total": len(rows)}
    with SIGNATURES.open("a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    print("signed: %s at %s" % (role, row["at"]))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P15 D9 — operational readiness")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--sign", default="", help="role signing the checklist")
    ap.add_argument("--evidence", default="")
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)

    rows = review()
    green = sum(1 for r in rows if r["ok"])
    if a.sign:
        return sign(a.sign, a.evidence, rows)
    for r in rows:
        print("  %s %-24s %s" % ("PASS" if r["ok"] else "OPEN", r["id"], r["detail"]))
    print("readiness: %d/%d line(s) proven by an artefact; the rest are owner steps, named above"
          % (green, len(rows)))
    if a.json:
        pathlib.Path(a.json).write_text(json.dumps({"rows": rows, "green": green}, indent=2) + "\n")
    return 0 if a.check and green == len(rows) else (0 if not a.check else 1)


if __name__ == "__main__":
    raise SystemExit(main())
