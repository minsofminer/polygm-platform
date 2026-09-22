#!/usr/bin/env python3
"""P15 D1 — the environment matrix, and the differences that must be written down.

    python3 tools/p15-env-diff.py                 # the tables, and the checks
    python3 tools/p15-env-diff.py --write docs/P15-environments.md

The kit's warning is specific: *"a staging environment that differs from prod in ways nobody has written down is how
you get a production incident on deploy day."* So this tool does three things:

1. **Declares** every knob's value in every environment (`config/environments.json` — the file the deploy actually
   reads, so the document cannot drift from what we deploy).
2. **Refuses to pass** when a knob differs between two environments and the difference has no `why`. "It's staging"
   is not a reason; "the mock executor, because staging must never hold a real order" is.
3. **Asserts the invariants** that make the environments different *on purpose* rather than by accident — prod is
   bearer-auth with the security-plane env required and the venue transport live; staging trades against the mock
   venue; local may use the dev identity header and binds to loopback only.

The invariant list is the part with teeth. A matrix is documentation; a matrix whose prod row is checked for
`PGM_REQUIRE_SECURITY_ENV=1` before anything is deployed is a control. (P14's F9 was exactly this class of mistake:
the deployed API was in the development identity shape because an environment variable was absent, and nothing
anywhere said it must be present.)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
MATRIX = ROOT / "config" / "environments.json"

#: (env, knob, expected, why) — checked on every run. These are the rows where a wrong value is a security or
#: money-path defect rather than a preference.
INVARIANTS = (
    ("prod", "PGM_REQUIRE_SECURITY_ENV", "1",
     "the deployed box runs the production identity shape: no dev header, and a missing KEK is a boot failure (F9)"),
    ("prod", "PGM_AUTH_MODE", "bearer", "the only way in is a session token"),
    ("prod", "POLYGM_TRANSPORT", "live", "prod is the venue"),
    ("prod", "POLYGM_ENGINE", "postgres", "SQLite is dev/CI only and must never hold real money"),
    ("prod", "PGM_LOG_FORMAT", "json", "the log scanner and the redactor read JSON lines"),
    ("staging", "POLYGM_TRANSPORT", "mock",
     "staging must be able to exercise the whole order path without a real order ever leaving the building"),
    ("staging", "PGM_REQUIRE_SECURITY_ENV", "1",
     "staging is prod-shaped for auth: a staging-only identity convenience is how F9 happens twice"),
    ("canary", "POLYGM_TRANSPORT", "live", "the canary exists to place real orders, with real money"),
    ("canary", "PGM_REQUIRE_SECURITY_ENV", "1", "real money, production shape"),
    ("local", "PGM_AUTH_MODE", "dev-header", "the dev header is the laptop's convenience, and only the laptop's"),
    ("local", "PGM_REQUIRE_SECURITY_ENV", "", "unset locally: the developer's KEK comes from .env or the fixtures"),
    ("preview", "POLYGM_TRANSPORT", "mock", "a per-PR environment must never reach the venue"),
    ("preview", "POLYGM_ENGINE", "postgres", "preview runs the real schema against a branch database, not SQLite"),
)

#: Knobs whose value must be EMPTY in the environments that talk to the real venue: a rate-limit override left on
#: in production is how a budget is spent twice.
MOCK_ONLY = ("PGM_MOCK_CLOB_URL",)


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passes = 0

    def check(self, ok: bool, what: str, why: str = "") -> bool:
        if ok:
            self.passes += 1
        else:
            self.failures.append("%s%s" % (what, (" — " + why) if why else ""))
        return bool(ok)


def load() -> dict:
    data = json.loads(MATRIX.read_text())
    if not isinstance(data.get("environments"), dict):
        raise SystemExit("config/environments.json needs an `environments` object")
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P15 D1 — environment matrix and differences")
    ap.add_argument("--write", help="write the markdown document here")
    args = ap.parse_args(argv)
    data = load()
    # `_purpose`/`_data`/`_cost` are the kit's own columns for the table, not knobs: they are metadata about the
    # environment, and treating them as configuration would make every environment differ from every other by
    # description alone.
    envs = {name: {k: v for k, v in row.items() if not k.startswith("_")}
            for name, row in data["environments"].items()}
    meta = {name: {k: v for k, v in row.items() if k.startswith("_")}
            for name, row in data["environments"].items()}
    knobs = data.get("knobs") or {}
    order = list(envs)
    rep = Report()

    # 1. Structural sanity: the same knob set in every environment, and every knob documented.
    keysets = {name: set(envs[name]) for name in order}
    common = set.intersection(*keysets.values()) if keysets else set()
    rep.check(all(ks == common for ks in keysets.values()),
              "every environment declares the same %d knob(s)" % len(common),
              "these differ: %s" % {n: sorted(ks - common) for n, ks in keysets.items() if ks != common})
    undocumented = sorted(k for k in common if k not in knobs)
    rep.check(not undocumented, "every knob has a one-line meaning in `knobs`",
              "missing: %s" % ", ".join(undocumented))

    # 2. Every difference carries a reason.
    diffs: list[dict] = []
    for i, a in enumerate(order):
        for b in order[i + 1:]:
            for knob in sorted(common):
                if envs[a][knob] == envs[b][knob]:
                    continue
                why = (knobs[knob].get("diffReason") or {}).get("%s|%s" % (a, b)) or \
                      (knobs[knob].get("diffReason") or {}).get("%s|%s" % (b, a)) or \
                      knobs[knob].get("whyDiffers", "")
                diffs.append({"a": a, "b": b, "knob": knob, "a_value": envs[a][knob], "b_value": envs[b][knob],
                              "why": why})
    unexplained = [d for d in diffs if not str(d["why"]).strip()]
    rep.check(not unexplained, "all %d cross-environment difference(s) have a written reason" % len(diffs),
              "unexplained: %s" % ", ".join("%s/%s %s" % (d["a"], d["b"], d["knob"]) for d in unexplained[:6]))

    # 3. The invariants.
    for env, knob, want, why in INVARIANTS:
        got = envs.get(env, {}).get(knob)
        rep.check(got == want, "%s: %s is %r" % (env, knob, want),
                  "it is %r — %s" % (got, why))

    # 4. Mock-only knobs are empty wherever the venue is real.
    for env in order:
        if envs[env].get("POLYGM_TRANSPORT") != "mock":
            for knob in MOCK_ONLY:
                rep.check(not envs[env].get(knob),
                          "%s: %s is empty (the venue is real here)" % (env, knob),
                          "a mock endpoint pointed at from a live environment is a misroute waiting to happen")

    # ------------------------------------------------------------------ the document
    lines = ["# P15 D1 — environments",
             "",
             "Generated by `tools/p15-env-diff.py` from `config/environments.json` (the file the deploy reads), so",
             "this page cannot describe a configuration nobody deploys. Every difference between two environments",
             "carries a reason: the tool refuses to pass while any difference is unexplained.",
             ""]
    lines += ["| Environment | Purpose | Data | Cost |", "|---|---|---|---|"]
    for env in order:
        row = meta[env]
        lines.append("| `%s` | %s | %s | %s |" % (env, row.get("_purpose", ""), row.get("_data", ""),
                                                 row.get("_cost", "")))
    lines += ["", "## What each difference is for", ""]
    for d in diffs:
        lines.append("* **%s vs %s — `%s`**: `%s` vs `%s`  \n  %s"
                     % (d["a"], d["b"], d["knob"], d["a_value"] or "(empty)", d["b_value"] or "(empty)", d["why"]))
    lines += ["", "## The invariants, checked on every run", "", "| Environment | Knob | Must be | Why |", "|---|---|---|---|"]
    for env, knob, want, why in INVARIANTS:
        lines.append("| `%s` | `%s` | `%s` | %s |" % (env, knob, want or "(empty)", why))

    doc = "\n".join(lines) + "\n"
    if args.write:
        pathlib.Path(args.write).write_text(doc)
        print("wrote %s" % args.write)
    else:
        print(doc)

    print("P15 environments: %d checks passed, %d failed" % (rep.passes, len(rep.failures)))
    for f in rep.failures:
        print("  FAIL %s" % f)
    return 1 if rep.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
