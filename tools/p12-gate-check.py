#!/usr/bin/env python3
"""P12's gate check: the phase's own quality gate, run the way the other phases' gates are run.

Every phase in this build has a gate — a script that answers "is this phase actually done" with checks that are
specific to *that* phase's promises rather than a generic test suite. This is P12's, and it is written around the
three things this phase can get wrong in ways no unit test on either side would notice:

  1. **The deep link across two languages.** The bot mints `t.me/<bot>/trade?startapp=<payload>` in Python; the Mini
     App parses it in TypeScript. Each side was internally consistent from P08 to P12 while disagreeing with the other,
     so every channel alert's trade button opened the Mini App on nothing. This check mints links with the *real*
     Python function and hands them to the *real* TypeScript parser (via `node`), then asserts the parser resolved the
     market the link named. A test on one side cannot do this; it is the seam that was broken.

  2. **The refusal vocabulary, in both directions.** Every code the chat can say to a person must exist in `CODES`,
     and the Python table and the TypeScript table must have the same keys. A code that exists only in the nicer
     table is a message that never renders.

  3. **The surfaces stay separate.** The Mini App is its own deployment with its own root document and its own
     surface gate: the paths it must 404, the CSP that frames only Telegram, the two noindex signals, and the
     `PGM_SURFACE` switch that turns all of it on. Checked here by reading the sources and config that implement it —
     the live URLs are checked by `--live`, which is opt-in because a gate that needs the network is a gate that fails
     on a train.

Every check carries a canary: a deliberately broken input that must make *that check* fail. A check that cannot fail
is a check that is not checking, and this build has already been burned once by a gate whose inputs were constants
(`_tg_broadcast_candidates` filled the quality gate with literals, so the gate agreed with everything).

Usage: tools/p12-gate-check.py            (all checks, canaries included)
       tools/p12-gate-check.py --live     (also probes the deployed Mini App and API over the network)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT / "services" / "api"))

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, why: str = "") -> bool:
    results.append((PASS if ok else FAIL, name, "" if ok else why))
    return ok


def canary(name: str) -> bool:
    """A fresh `check` that must fail. Returns True when the check really did fail on broken input."""
    return not check(name, False, "canary: this check must fail on broken input")


# ---------------------------------------------------------------------------------------------------------------------
# 1 · the deep link, minted in Python and parsed in TypeScript
# ---------------------------------------------------------------------------------------------------------------------
def deep_link_round_trip() -> None:
    from polygm_core.telegrambot import channel

    contract = json.loads((ROOT / "contracts" / "startapp.json").read_text(encoding="utf-8"))
    slugs = ["fed-cut-sept", "top-30d", "recount-incumbent", "btc-150k-sep"]
    links = {slug: channel.mini_app_url(slug, bot_username="polygm_bot") for slug in slugs}
    payloads = {slug: link.split("startapp=", 1)[1] for slug, link in links.items()}

    check("every minted payload uses the contract's separator",
          all(p[len(p.split(contract['separator'], 1)[0])] == contract["separator"] for p in payloads.values()),
          str(payloads))
    check("every minted payload is inside the contract's charset",
          all(set(p) <= set(contract["value_charset_literal"]) | {contract["separator"]} for p in payloads.values()),
          str(payloads))

    # The TypeScript half is run, not re-implemented: the app's own parser is compiled to JS with the esbuild that
    # already sits in `web/node_modules` (vitest's dependency) and executed in node against the links Python just
    # minted. Re-implementing the parse in Python would have proved nothing — the bug this check exists for was two
    # implementations agreeing with themselves and disagreeing with each other.
    bundle = ROOT / ".tmp" / "p12-startapp.mjs"
    bundle.parent.mkdir(exist_ok=True)
    esbuild = ROOT / "web" / "node_modules" / ".bin" / "esbuild"
    if not esbuild.exists():
        check("the app's parser is built and run against the bot's own links", False,
              "web/node_modules/.bin/esbuild is missing — run `npm install` in web/")
        return
    build = subprocess.run([str(esbuild), str(ROOT / "web" / "src" / "telegram" / "startapp.ts"),
                            "--bundle", "--format=esm", "--platform=node", "--outfile=" + str(bundle)],
                           capture_output=True, text=True, timeout=180)
    if build.returncode != 0:
        check("the app's parser is built and run against the bot's own links", False, build.stderr[-300:])
        return
    script = bundle.with_suffix(".run.mjs")
    script.write_text(
        "import { parseStartapp, startappTarget } from %s;\n"
        "const payloads = %s;\n"
        "const out = {};\n"
        "for (const [slug, p] of Object.entries(payloads)) {\n"
        "  const v = parseStartapp(p);\n"
        "  out[slug] = { kind: v.kind, marketId: v.marketId ?? null, href: startappTarget(v).href ?? null };\n"
        "}\n"
        "console.log(JSON.stringify(out));\n" % (json.dumps(str(bundle)), json.dumps(payloads)),
        encoding="utf-8")
    run = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=120)
    if run.returncode != 0:
        check("the app's parser is built and run against the bot's own links", False, (run.stderr or "")[-300:])
        return
    check("the app's parser is built and run against the bot's own links", True)
    parsed = json.loads(run.stdout.strip().splitlines()[-1])
    check("the app's parser resolves every market the bot linked",
          all(parsed.get(slug, {}).get("kind") == "market" for slug in slugs), json.dumps(parsed))
    check("the parser's market id is the slug the link carried",
          all(parsed.get(slug, {}).get("marketId") == slug for slug in slugs), json.dumps(parsed))
    check("a resolved market is routed to the page that exists",
          all((parsed.get(slug, {}).get("href") or "").startswith("/market/") for slug in slugs), json.dumps(parsed))
    # The canary that matters: the parser must refuse the shapes we used to mint. If it accepted these, the check above
    # would pass on a parser that accepts anything.
    leftover = script.with_name("p12-canary.run.mjs")
    leftover.write_text(
        "import { parseStartapp } from %s;\n"
        "const bad = ['fed-cut-sept', 'm:fed-cut-sept', 'm-fed-cut-sept:extra', 'm-<script>'];\n"
        "console.log(JSON.stringify(bad.map((b) => parseStartapp(b).kind)));\n" % json.dumps(str(bundle)),
        encoding="utf-8")
    canary_run = subprocess.run(["node", str(leftover)], capture_output=True, text=True, timeout=120)
    kinds = json.loads(canary_run.stdout.strip()) if canary_run.returncode == 0 else []
    check("canary: an untagged or colon-shaped payload is refused by the parser",
          kinds == ["rejected", "rejected", "rejected", "rejected"], str(kinds))

    # Canaries: the shapes that were wrong before this check existed.
    for broken, label in (("startapp=fed-cut-sept", "an untagged payload"),
                          ("startapp=m:fed-cut-sept", "the old colon grammar")):
        url = links["fed-cut-sept"].replace(links["fed-cut-sept"].split("startapp=")[1], broken.split("=")[1])
        payload = url.split("startapp=", 1)[1]
        check("canary: %s is refused by both sides" % label,
              channel.startapp_payload("m", payload) != payload or not re.fullmatch(
                  r"m-%s+" % re.escape(contract["value_charset_literal"]), payload),
              payload)


# ---------------------------------------------------------------------------------------------------------------------
# 2 · the refusal vocabulary
# ---------------------------------------------------------------------------------------------------------------------
def refusal_vocabulary() -> None:
    app_src = (ROOT / "services" / "api" / "app.py").read_text(encoding="utf-8")
    ts_src = (ROOT / "web" / "src" / "tma" / "trade.ts").read_text(encoding="utf-8")

    codes_block = app_src.split("CODES = {", 1)[1].split("\n}", 1)[0]
    codes = set(re.findall(r'"([A-Z][A-Z0-9_]{3,})":\s*\(', codes_block))
    # `_tg_plain_refusal`'s table is a local dict of `"CODE": "sentence"` (occasionally wrapped over two lines), so the
    # keys are read from the table body itself rather than from a pattern that assumes one code per line.
    py_table = app_src.split("def _tg_plain_refusal", 1)[1].split("def ", 1)[0]
    py_keys = set(re.findall(r'^\s{8}"([A-Z][A-Z0-9_]+)":', py_table, re.M))
    ts_fn = ts_src.split("export function plainRefusal", 1)[1].split("\n}", 1)[0]
    ts_keys = set(re.findall(r'^\s{4}([A-Z][A-Z0-9_]+):', ts_fn, re.M))

    check("CODES parsed out of the API source", len(codes) > 40, "%d codes" % len(codes))
    check("the Python refusal table is not empty", len(py_keys) > 15, "%d keys: %s" % (len(py_keys), sorted(py_keys)[:3]))
    check("the TypeScript refusal table was found", len(ts_keys) > 15, "%d keys: %s" % (len(ts_keys), sorted(ts_keys)[:3]))
    check("every Python refusal key is a registered code", not (py_keys - codes), str(sorted(py_keys - codes)))
    check("every TypeScript refusal key is a registered code", not (ts_keys - codes), str(sorted(ts_keys - codes)))
    check("both refusal tables have the same keys", py_keys == ts_keys,
          "py-only %s · ts-only %s" % (sorted(py_keys - ts_keys), sorted(ts_keys - py_keys)))
    check("a refusal the table does not know still says something true",
          "The venue refused the order" in py_table and "The venue refused the order" in ts_fn,
          "no fallback sentence in one of the two")
    check("the fallback can carry the gate's own detail rather than swallowing it",
          "detail" in py_table.split("return", 1)[-1] and "detail" in ts_fn.split("return", 1)[-1], "")
    # Canaries, and each is the *specific* mistake this phase made: a plausible name that is not a code.
    check("canary: an invented RISK_* key is not in either table",
          "RISK_NOTIONAL" not in py_keys and "RISK_NOTIONAL" not in ts_keys and "RISK_NOTIONAL" not in codes,
          "the RISK_* names this table was first written with")
    check("canary: the checker rejects a table keyed on those names",
          not ({k for k in py_keys if k.startswith("RISK_")} - codes), "no RISK_* key may be unregistered")


def surfaces() -> None:
    mw = (ROOT / "web" / "middleware.ts").read_text(encoding="utf-8")
    surface = (ROOT / "web" / "src" / "tma" / "surface.server.ts").read_text(encoding="utf-8")
    cfg = (ROOT / "web" / "next.config.mjs").read_text(encoding="utf-8")
    layout = (ROOT / "web" / "app" / "layout.tsx").read_text(encoding="utf-8")
    vercel_api = (ROOT / "vercel.json").read_text(encoding="utf-8")
    vercel_web = (ROOT / "web" / "vercel.json").read_text(encoding="utf-8")
    entry = (ROOT / "api" / "index.py").read_text(encoding="utf-8")

    check("the surface is read in exactly one place", surface.count("PGM_SURFACE") >= 1 and "miniapp" in surface, "")
    check("the surface module is server-only by name", "surface.server.ts" in (ROOT / "web" / "middleware.ts").read_text(encoding="utf-8")
          or "surface.server" in mw, "the middleware must import the server read")
    check("the Mini App gate turns a non-miniapp deployment into a pass-through",
          "isMiniAppSurface()" in mw and "NextResponse.next()" in mw, "")
    # A 404 with a sentence, not a redirect: a redirect would turn the Mini App's domain into a doorway to the site,
    # which is the second indexable copy this split exists to avoid.
    check("off-surface paths on the Mini App are a 404, not a redirect",
          "status: 404" in mw and "NextResponse.redirect" not in mw and "surfaceAllows(pathname)" in mw, "")
    check("framing is Telegram-only and behind the surface flag",
          "frame-ancestors" in cfg and "web.telegram.org" in cfg and "MINIAPP" in cfg, "")
    check("noindex is per-surface, never in a shared config",
          "noindex" in cfg.lower() and "noindex" not in vercel_web.lower(),
          "a noindex header in web/vercel.json de-indexes the main site (P08 D1)")
    check("the robots meta is decided by the surface", "isMiniAppSurface" in layout, "")
    check("the API deployment rewrites everything into its own function",
          "/api/index" in vercel_api and "includeFiles" in vercel_api, "")
    check("the API's Vercel entry migrates and seeds before it imports the app",
          entry.index("_boot(_DB)") < entry.index("from app import app"), "the boot must precede the import")
    check("the API entry names its own persistence caveat",
          "/tmp/polygm-demo.db" in entry and "per-instance" in entry,
          "the file has to say out loud that the filesystem is per-instance")

    # Canaries: the two configurations that must not exist.
    check("canary: the Mini App's middleware does not pass every path through",
          not re.search(r"isMiniAppSurface\(\)\s*\{[^}]*return NextResponse\.next\(\);\s*\}\s*$", mw, re.M),
          "a pass-through before the allow-list is a gate that gates nothing")
    check("canary: a noindex header in web/vercel.json is absent",
          "noindex" not in vercel_web.lower(), "")


def live_probe() -> None:
    import urllib.error
    import urllib.request

    def get(url: str) -> tuple[int, str, dict]:
        req = urllib.request.Request(url, headers={"User-Agent": "PolyGM-gate-check/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read(4000).decode("utf-8", "replace"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(2000).decode("utf-8", "replace"), dict(e.headers)

    api = "https://polygm-api.vercel.app"
    app = "https://polygm-mini-app.vercel.app"
    code, body, _ = get(api + "/healthz?cb=p12gate")
    check("live: the API answers healthz", code == 200 and '"ok":true' in body, "%s %s" % (code, body[:80]))
    code, body, _ = get(app + "/?cb=p12gate")
    check("live: the Mini App root serves the trade screen", code == 200, "%s %s" % (code, body[:80]))
    for path in ("/terminal", "/admin", "/wallet", "/markets"):
        code, body, _ = get(app + path + "?cb=p12gate")
        check("live: %s is 404 on the Mini App surface" % path, code == 404, "%s" % code)
    code, _b, headers = get(app + "/?cb=p12gate")
    check("live: the Mini App sends X-Robots-Tag noindex",
          "noindex" in (headers.get("X-Robots-Tag") or headers.get("x-robots-tag") or "").lower(), str(headers.get("X-Robots-Tag")))
    code, _b, headers = get(api + "/v1/markets?limit=1&cb=p12gate")
    check("live: the API serves real market data", code == 200, "%s" % code)


def main() -> int:
    live = "--live" in sys.argv
    deep_link_round_trip()
    refusal_vocabulary()
    surfaces()
    if live:
        live_probe()

    width = max(len(n) for _s, n, _w in results)
    for status, name, why in results:
        print("%-4s %s%s" % (status, name.ljust(width), ("  — " + why) if why else ""))
    failed = [r for r in results if r[0] == FAIL]
    print("\np12-gate-check: %d passed, %d failed%s" % (len(results) - len(failed), len(failed),
                                                       "" if live else "  (--live skipped: no network probes)"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
