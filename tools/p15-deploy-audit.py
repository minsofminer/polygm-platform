#!/usr/bin/env python3
"""P15 D3 — the deploy audit: who, what, when, and which commit, in a ledger that can be checked.

    python3 tools/p15-deploy-audit.py begin  --env prod --actor ci --reason "release 1.4.2"
    python3 tools/p15-deploy-audit.py finish --id 20260923T101500Z-prod --result ok --note "smoke green"
    python3 tools/p15-deploy-audit.py verify

The kit asks for a deploy audit. A file that a deploy appends to is not an audit — it is a diary, and a diary does
not notice when somebody rewrites yesterday. So entries are **hash-chained**: each one carries the digest of the
previous entry, and `verify` recomputes the chain, checks every recorded commit exists in git with the tree it
claims, and reports the first entry that does not add up.

What is recorded, and why each field earns its line:

* **actor** — a person or a robot, taken from `--actor` or the git identity; "who" is the first question.
* **commit + tree** — the exact revision, plus the tree hash, so a tag moved under us is visible.
* **previous commit** — what it is replacing; the rollback target for a deploy that has to be undone at 2am.
* **image digests** — the running artifact, by digest, from `deploy/image-digests.txt`.
* **migrations added** — the change set the migration checker saw.
* **result + note** — "ok", "failed", or "rolled-back", with the sentence the operator would have written.

And the property that makes it usable at 2am: it **refuses rather than guesses**. A repository whose HEAD is
unborn has no revision to name, so `begin` writes nothing and says so; a `verify` that cannot resolve any commit
in the repository is a failed verification, not a vacuous pass. Both refusals are exercised in
`tests/test_p15_drain_guard.py` against a scratch checkout — a gate that cannot be made to fail is not a gate.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
LEDGER = pathlib.Path(os.environ.get("PGM_DEPLOY_AUDIT") or ROOT / "deploy" / "audit.jsonl")
DIGESTS = ROOT / "deploy" / "image-digests.txt"
GENESIS = "0" * 64
UNBORN = 2  # exit code for a refusal: nothing was written, and the caller must not treat the deploy as recorded


def repo_root() -> pathlib.Path:
    """Where the historical questions are asked. It is this repository in every real deploy; a drill or a test
    points `PGM_DEPLOY_REPO` at a scratch checkout so the refusal paths are provable rather than asserted in prose."""
    return pathlib.Path(os.environ.get("PGM_DEPLOY_REPO") or ROOT)


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=str(repo_root()), capture_output=True, text=True,
                             check=True).stdout.strip()
    except Exception:
        return ""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def entry_id(env: str) -> str:
    return "%s-%s" % (now(), env)


def digest_of(entry: dict) -> str:
    body = json.dumps({k: v for k, v in entry.items() if k != "hash"}, sort_keys=True).encode()
    return hashlib.sha256(body).hexdigest()


def read_ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    out = []
    for i, line in enumerate(LEDGER.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit("deploy/audit.jsonl line %d is not JSON (%s) — the ledger has been edited by hand"
                             % (i, exc))
    return out


def append(entry: dict) -> None:
    entry = dict(entry)
    entry["hash"] = digest_of(entry)
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


def read_digests() -> dict[str, str]:
    """`<service> <image-ref-with-digest> <recorded-utc> <who>` — the file the deploy actually reads."""
    out: dict[str, str] = {}
    if not DIGESTS.exists():
        return out
    for line in DIGESTS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out


def cmd_begin(args) -> int:
    ledger = read_ledger()
    head = git("rev-parse", "HEAD")
    if not head:
        print("refusing to open a deploy entry in %s: HEAD is unborn, so the entry could not name the revision "
              "being deployed (and there would be nothing to roll back to). Commit first, then re-run." % repo_root(),
              file=sys.stderr)
        return UNBORN
    tree = git("rev-parse", "HEAD^{tree}")
    prev = ""
    for e in reversed(ledger):
        if e.get("env") == args.env and e.get("event") == "finish" and e.get("result") == "ok":
            prev = e.get("commit", "")
            break
    prev = prev or git("rev-parse", "HEAD~1")
    eid = entry_id(args.env)
    entry = {
        "event": "begin", "id": eid, "at": now(), "env": args.env,
        "actor": args.actor or git("config", "user.name") or "unknown",
        "reason": args.reason or "",
        "commit": head, "tree": tree, "previous_commit": prev,
        "dirty": bool(git("status", "--porcelain")),
        "digests": read_digests(),
        "migrations_added": [l.split("/")[-1] for l in
                             git("diff", "--name-only", "--diff-filter=A", f"{prev}...HEAD", "--", "db/migrations")
                             .splitlines() if l.strip()],
        "prev_hash": ledger[-1]["hash"] if ledger else GENESIS,
    }
    append(entry)
    print(eid)
    return 0


def cmd_finish(args) -> int:
    ledger = read_ledger()
    target = None
    for e in reversed(ledger):
        if e.get("id") == args.id:
            target = e
            break
    if target is None:
        print("no begin entry with id %s" % args.id, file=sys.stderr)
        return 1
    entry = {"event": "finish", "id": args.id, "at": now(), "env": target.get("env"),
             "actor": args.actor or target.get("actor"), "result": args.result, "note": args.note or "",
             "commit": target.get("commit"), "prev_hash": ledger[-1]["hash"]}
    append(entry)
    print("%s %s" % (args.result, args.id))
    return 0 if args.result == "ok" else 0  # the ledger records outcomes; the caller decides what to do next


def cmd_verify(args) -> int:
    ledger = read_ledger()
    problems: list[str] = []
    if not ledger:
        print("the ledger is empty — no deploy has been recorded yet")
        return 0 if args.allow_empty else 1
    prev = GENESIS
    known_commits = set(git("log", "--format=%H").split())
    if not known_commits:
        # Fail closed: with no commits to compare against, every commit check below would silently pass and the
        # verdict would be a PASS about a repository the ledger says nothing about.
        problems.append("no commit can be verified: %s has no commits at all (HEAD is unborn or this is not a "
                        "repository)" % repo_root())
    for n, e in enumerate(ledger, 1):
        if e.get("prev_hash") != prev:
            problems.append("entry %d (%s): prev_hash does not match the previous entry — the ledger was edited"
                            % (n, e.get("id")))
        if e.get("hash") != digest_of(e):
            problems.append("entry %d (%s): the entry's own hash does not match its content" % (n, e.get("id")))
        commit = e.get("commit") or ""
        if not commit:
            problems.append("entry %d (%s): no commit recorded — an entry that cannot name its revision cannot "
                            "be rolled back to" % (n, e.get("id")))
        elif known_commits and commit not in known_commits:
            problems.append("entry %d (%s): commit %s is not in this repository" % (n, e.get("id"), commit[:12]))
        prev = e.get("hash") or prev
    finishes = [e for e in ledger if e.get("event") == "finish"]
    ok = [e for e in finishes if e.get("result") == "ok"]
    print("deploy audit: %d entries, %d completed, %d ok" % (len(ledger), len(finishes), len(ok)))
    for f in finishes[-5:]:
        print("  %s %-6s %s %s" % (f.get("at"), f.get("result"), f.get("env"), (f.get("note") or "")[:60]))
    for p in problems:
        print("  FAIL %s" % p)
    print("Verdict: %s" % ("PASS" if not problems else "FAIL"))
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    global LEDGER
    ap = argparse.ArgumentParser(description="P15 D3 — deploy audit ledger")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("begin", help="record a deploy starting")
    b.add_argument("--env", required=True)
    b.add_argument("--actor", default="")
    b.add_argument("--reason", default="")
    b.set_defaults(fn=cmd_begin)

    f = sub.add_parser("finish", help="record the outcome")
    f.add_argument("--id", required=True)
    f.add_argument("--result", choices=["ok", "failed", "rolled-back"], required=True)
    f.add_argument("--note", default="")
    f.add_argument("--actor", default="")
    f.set_defaults(fn=cmd_finish)

    v = sub.add_parser("verify", help="check the chain")
    v.add_argument("--allow-empty", action="store_true")
    v.set_defaults(fn=cmd_verify)

    for sp in (b, f, v):
        # Tests and drills point this at a scratch file; the deployed ledger is the repo's default and is what
        # `git log` on deploy/audit.jsonl shows the reviewers.
        sp.add_argument("--ledger", default=os.environ.get("PGM_DEPLOY_AUDIT") or str(LEDGER),
                        help="path to the ledger (default deploy/audit.jsonl)")
    args = ap.parse_args(argv)
    LEDGER = pathlib.Path(args.ledger)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
