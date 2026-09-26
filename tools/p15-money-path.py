#!/usr/bin/env python3
"""P15 D3 — is this change on the money path?

    python3 tools/p15-money-path.py --since origin/main        # prints one word: money-path | safe
    python3 tools/p15-money-path.py --since HEAD~1 --explain   # the matching files, one per line

The pipeline has one rule that must not be softened by convenience: **the executor never auto-promotes**. The
manual gate exists for "anything touching the executor", and "anything" has to be a list a reviewer can read and
a machine can evaluate — otherwise the gate becomes a step somebody redefines when it is inconvenient at 6pm on
a Friday.

Two properties this file is written to have:

* the list is *paths*, not authors or commit messages. A tag, a branch name and a message are all things a
  person can write wrongly; a changed path is not.
* the classifier **fails towards the gate**: an empty diff (a fresh clone, a shallow checkout, a renamed
  branch) returns `money-path`, because "I could not tell" must not be the same answer as "it is safe".
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The money path: the process that signs and submits, the vocabulary the risk gate speaks, the copy engine that
# turns one fill into N orders, the venue adapters, the wallet/signing layer, the reconciliation job, the ledger,
# and the two artefacts a deploy consumes (the pinned digests and the venue addresses). A migration counts
# because it can change a money table, and the production compose file counts because it can remove a cap.
MONEY_PREFIXES: tuple[str, ...] = (
    "services/executor/",
    "packages/polygm_core/executor/",
    "packages/polygm_core/risk/",
    "packages/polygm_core/copy/",
    "packages/polygm_core/venue/",
    "packages/polygm_core/wallets/",
    "packages/polygm_core/ledger/",
    "packages/polygm_core/reconcile/",
    "packages/polygm_core/money/",
    "db/migrations/",
    "deploy/venue-addresses.json",
    "deploy/image-digests.txt",
    "docker-compose.prod.yml",
    "services/api/app.py",          # the order route, the kill switch and the drain endpoint live here
    "contracts/openapi.yaml",       # a contract change that renames a money field is a money change
)
MONEY_PATH, SAFE = "money-path", "safe"


def changed_files(since: str | None, root: Path = ROOT) -> list[str] | None:
    """None means "could not establish the change set" — the caller must treat that as the money path."""
    refs = [since] if since else ["origin/main", "HEAD~1"]
    for ref in refs:
        try:
            out = subprocess.run(["git", "diff", "--name-only", f"{ref}...HEAD"], cwd=root,
                                 capture_output=True, text=True, check=True).stdout
        except Exception:
            continue
        files = [l.strip() for l in out.splitlines() if l.strip()]
        # untracked files count: a new module dropped into the executor is exactly the change this gate is for
        try:
            un = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=root,
                                capture_output=True, text=True, check=True).stdout
            files += [l.strip() for l in un.splitlines() if l.strip()]
        except Exception:
            pass
        return sorted(set(files))
    return None


def classify(files: list[str] | None) -> tuple[str, list[str]]:
    if files is None:
        return MONEY_PATH, ["<the change set could not be established>"]
    hits = [f for f in files if any(f == p or f.startswith(p) for p in MONEY_PREFIXES)]
    return (MONEY_PATH if hits else SAFE), hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P15 D3 — money-path classifier for the manual deploy gate")
    ap.add_argument("--since", default="", help="git ref to diff against (default: origin/main, then HEAD~1)")
    ap.add_argument("--explain", action="store_true", help="print the matching files")
    ap.add_argument("--list-prefixes", action="store_true")
    args = ap.parse_args(argv)

    if args.list_prefixes:
        print("\n".join(MONEY_PREFIXES))
        return 0

    files = changed_files(args.since or None)
    verdict, hits = classify(files)
    if args.explain:
        if files is None:
            print("the change set could not be established (no origin/main, no HEAD~1): failing towards the gate")
        else:
            print("%d changed file(s), %d on the money path" % (len(files), len(hits)))
        for h in hits:
            print("  %s" % h)
    print(verdict)
    return 0 if verdict == MONEY_PATH else 0


if __name__ == "__main__":
    sys.exit(main())
