#!/usr/bin/env python3
"""P08 Quality Gate — the frontend shell. Same rule as every other phase's gate in this repo: a check either
EXECUTES (a subprocess, a parser over the real tree, a request against two booted servers) or it does not go
in the list. Nothing here asks a human to confirm that a stylesheet was written; it reads the stylesheet.

The phase's own acceptance line is `npm run build && npm run test`. That line is deliberately *not* the gate:
the build proves the TypeScript agrees with itself, and 15 of the phase's promises are things a compiler
cannot see — that a price is never rendered without its staleness, that no bundle can carry a secret, that
the money path is one path, that every unbuilt capability says so on screen. Those are the checks below.

Three properties every check is held to, from the standing rules:

  1. **no control without an owner and a test** — c3 parses `docs/P08-frontend-shell.md` and resolves each
     `[owner: … · test: …]` marker against a real file, a real test name, or a real check id in this script;
  2. **a control that cannot fail is a costume** — `--self-test` plants each violation the scans look for and
     fails if a scan walks past it. A check whose canary does not fire is removed, not celebrated;
  3. **a recorded number is a stale number** — c8 refuses a performance artefact older than the newest source
     file, because the whole point of measuring the payload is that it describes *this* build.

`--fast` drops the one check that boots two servers (c11); everything else, including the security scans,
runs. The build itself is not run by the gate — `make p08` builds first, and a gate that rebuilds the world
every time nobody runs the gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import string
import sys
import threading
import time
import urllib.error
import urllib.request
from http.client import HTTPConnection
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DOC = ROOT / "docs" / "P08-frontend-shell.md"
BUNDLE = ROOT / "docs" / "verification" / "P08-bundle.txt"
CONTRACT = ROOT / "contracts" / "openapi.yaml"
TOKENS = ROOT / "brand" / "tokens.css"
WEB_TOKENS = WEB / "styles" / "tokens.css"
PY = sys.executable
TMP = ROOT / ".tmp"

# --------------------------------------------------------------------------------- the shared parsers
# Everything below takes a tree argument rather than reading module globals, so --self-test can point the same
# function at a planted fixture. A check and its canary must run the same code, or the canary proves nothing.


def sh(argv, cwd: Path = ROOT, timeout: int = 900, env: dict | None = None) -> tuple[int, str]:
    e = dict(os.environ)
    e["TMPDIR"] = str(TMP)
    e["CI"] = "1"
    e.update(env or {})
    TMP.mkdir(exist_ok=True)
    try:
        p = subprocess.run(argv, cwd=str(cwd), env=e, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "(timed out after %ds)" % timeout
    except FileNotFoundError as f:
        return 127, "cannot run %s: %s" % (argv[0], f)
    return p.returncode, p.stdout + p.stderr


def strip_comments(text: str) -> str:
    """Comments out, strings kept.

    A regex stripper is what this file had first, and it deleted half of `next.config.mjs` by reading the `//`
    inside `https://telegram.org` as a line comment — which made a correct CSP look like a missing one. Same
    failure class as the CSS comment in `app/globals.css` that closed itself: prose is not code, and a scanner
    that confuses them reports its own bug as the product's."""
    out, i, q = [], 0, None
    n = len(text)
    while i < n:
        c = text[i]
        if q:
            out.append(c)
            if c == "\\":
                if i + 1 < n:
                    out.append(text[i + 1])
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if c in "\"'`":
            q = c
            out.append(c)
            i += 1
            continue
        if text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            stop = n if end == -1 else end + 2
            out.append("\n" * text.count("\n", i, stop))   # line numbers survive, so a report is readable
            i = stop
            continue
        out.append(c)
        i += 1
    return "".join(out)


def read(path: Path) -> str:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return ""


def tree_files(web: Path, exts=(".ts", ".tsx", ".css")):
    """Every source file the shell ships, minus what a generator owns. `web` is a tree root, so a fixture
    directory can be scanned exactly like the real one."""
    for d in ("src", "app", "styles"):
        base = web / d
        if not base.exists():
            continue
        for f in sorted(base.rglob("*")):
            if not f.is_file() or f.suffix not in exts:
                continue
            rel = f.relative_to(web).as_posix()
            if rel.endswith(".gen.ts"):
                continue
            yield rel, read(f)


def brace_at(text: str, start: int) -> int:
    """Index just past the `}` matching the `{` at `start`. String-literal *and comment* aware.

    Two versions of this parser died on `web/src/api/routes.ts` and each death is the same lesson: a scanner
    that reads prose as code reports a structure that is not there.

      1. a `}` inside a route's `note` string ended the match and silently dropped the rest of the ledger, so
         quote tracking went in;
      2. a comment containing an apostrophe — "the radar's two routes are `built: false`" — opened a string
         that never closed, so the whole file looked like an unterminated literal and the gate raised
         `unbalanced braces` before it ran a single check. That one arrived from P10's own comment, which is
         how a P08 parser found a bug two phases later: the file both gates read keeps growing.

    Comments are skipped rather than tracked, because nothing inside one can be a brace that matters.
    """
    depth, i, quote = 0, start, None
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        two = text[i:i + 2]
        if two == "//":
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        if two == "/*":
            end = text.find("*/", i + 2)
            i = len(text) if end == -1 else end + 2
            continue
        if c in "\"'`":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced braces")


def parse_ledger(web: Path) -> dict:
    """`src/api/routes.ts` -> {key: {method, path, built, whileMissing, owner}}."""
    text = read(web / "src/api/routes.ts")
    m = re.search(r"export const ROUTES\s*=\s*\{", text)
    if not m:
        raise ValueError("no `export const ROUTES = {` in src/api/routes.ts")
    body = text[m.end(): brace_at(text, m.end() - 1) - 1]
    out = {}
    for em in re.finditer(r"^  (\w+):\s*\{", body, re.M):
        blk = body[em.end() - 1: brace_at(body, em.end() - 1)]
        f = {k: v.strip().strip('"') for k, v in re.findall(r"(\w+):\s*(\"[^\"]*\"|true|false)", blk)}
        if "path" not in f:
            continue
        out[em.group(1)] = {"method": f.get("method", ""), "path": f["path"], "built": f.get("built") == "true",
                            "whileMissing": f.get("whileMissing", ""), "owner": f.get("owner", "")}
    return out


def parse_notes(web: Path) -> dict:
    """`src/api/route-notes.ts` -> {key: note}. The prose half of the ledger, kept out of the client module.

    `src/api/routes.ts` is imported by `src/api/client.ts`, so everything in it is fetched by every signed-in
    document. The notes explain capabilities that do not exist yet and nothing renders them, so they cost a
    phone 2.9 KB to read nothing and they live in their own module, imported by this gate and by a test. This
    function is what makes "in their own module" checkable, and `ledger_findings` uses it to keep the notes
    attached to routes that still exist.
    """
    path = web / "src/api/route-notes.ts"
    if not path.exists():
        return {}
    text = read(path)
    m = re.search(r"export const ROUTE_NOTES[^=]*=\s*\{", text)
    if not m:
        raise ValueError("no `export const ROUTE_NOTES` in src/api/route-notes.ts")
    body = text[m.end(): brace_at(text, m.end() - 1) - 1]
    return {k: v for k, v in re.findall(r"^\s+(\w+):\s*\"([^\"]*)\"", body, re.M)}


def parse_contract(path: Path = CONTRACT) -> set:
    """Every operation the API contract actually serves, as {(METHOD, path)} — parsed with a loader that
    refuses duplicate keys, because YAML's default behaviour is to keep the last one and call it valid."""
    import yaml

    class Strict(yaml.SafeLoader):
        pass

    def no_dupes(loader, node, deep=False):
        seen = set()
        for key, _ in node.value:
            k = loader.construct_object(key, deep=deep)
            if k in seen:
                raise ValueError("duplicate key %r in the contract" % k)
            seen.add(k)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, no_dupes)
    spec = yaml.load(read(path), Loader=Strict)
    return {(mm.upper(), p) for p, item in (spec.get("paths") or {}).items() if isinstance(item, dict)
            for mm in item if mm in ("get", "post", "put", "patch", "delete")}


def launch_items(doc_text: str) -> dict:
    """§4's numbered list -> {n: text}. The list is the ledger of gaps; a reference that resolves to nothing
    is the doc promising a launch review of an item that does not exist."""
    sec = doc_text.split("## 4.", 1)
    if len(sec) < 2:
        return {}
    body = sec[1].split("\n## ", 1)[0]
    out = {}
    for m in re.finditer(r"^(\d+)\.\s+(.*)$", body, re.M):
        item, rest = int(m.group(1)), m.group(2)
        # items wrap over lines: take everything up to the next number, so a key named on a continuation line
        # is still inside the item that owns it
        start = m.end()
        nxt = re.search(r"^\d+\.\s", body[start:], re.M)
        tail = body[start: start + (nxt.start() if nxt else 600)]
        out[item] = " ".join([rest] + [l.strip() for l in tail.splitlines()]).strip()
    return out


def doc_markers(doc_text: str) -> list:
    """`[owner: X · test: Y]` markers, in document order, with their line numbers."""
    out = []
    for n, line in enumerate(doc_text.splitlines(), 1):
        for m in re.finditer(r"\[owner:\s*([^\s·\]]+)\s*·\s*test:\s*([^\]]+?)\]", line):
            out.append({"line": n, "owner": m.group(1).strip(), "test": m.group(2).strip()})
    return out


# ---------------------------------------------------------------------------------------- c1  the ledger
def ledger_findings(routes: dict, ops: set, items: dict, notes: dict | None = None) -> list:
    """`notes` is optional so the canary can exercise the ledger rules without a fixture notes file; when it is
    passed, an unbuilt route must carry one and a note must not outlive its route."""
    f = []
    for key, d in routes.items():
        op = (d["method"].upper(), d["path"])
        if d["built"] and op not in ops:
            f.append("`%s` claims built:true but %s %s is not in contracts/openapi.yaml" % (key, d["method"], d["path"]))
        if not d["built"] and op in ops:
            f.append("`%s` claims built:false but the contract serves %s — flip the ledger, the UI is lying today" % (key, d["path"]))
        if not d["built"]:
            m = re.fullmatch(r"P08-L(\d+)", d["owner"])
            if not m:
                f.append("`%s` is unbuilt and its owner %r is not a P08-L item, so nothing owns closing it" % (key, d["owner"]))
            elif int(m.group(1)) not in items:
                f.append("`%s` names launch item %s and §4 has no such item" % (key, d["owner"]))
            elif key not in items[int(m.group(1))]:
                f.append("launch item %s never mentions route `%s`, so the checklist cannot be used to close it"
                         % (m.group(1), key))
    if notes is not None:
        # Coverage is the launch list's job (§4 explains every unbuilt route, and the check above makes sure each
        # one is named there). What this file must not do is drift: a note whose route has been renamed or
        # deleted is prose about a capability nobody has any more, and it is the first thing to rot in a ledger.
        for key in notes:
            if key not in routes:
                f.append("`src/api/route-notes.ts` explains `%s`, which is not a route in the ledger" % key)
    return f


def c1_the_route_ledger_and_the_contract_and_the_launch_list_agree(ctx) -> tuple:
    """Both directions: nothing in the ledger may claim a route the contract does not serve, nothing the
    contract serves may still be marked unbuilt, and every unbuilt route is owned by a numbered launch item
    that names it."""
    routes, ops = ctx["routes"], ctx["ops"]
    findings = ledger_findings(routes, ops, ctx["items"], ctx.get("notes"))
    dangling = []
    for rel, text in tree_files(WEB):
        for m in re.finditer(r"P08-L(\d+)", text):
            if int(m.group(1)) not in ctx["items"]:
                dangling.append("%s mentions %s" % (rel, m.group(0)))
    found = sorted(set(dangling))
    if found:
        findings.append("dangling launch-item reference(s): " + "; ".join(found[:4]))
    unbuilt = [k for k, d in routes.items() if not d["built"]]
    return ("c1", not findings,
             "%d routes in the ledger (%d built, %d refused-by-design), %d contract operations, %d launch items; %s"
             % (len(routes), len(routes) - len(unbuilt), len(unbuilt), len(ops), len(ctx["items"]),
                "all three agree" if not findings else "%d disagreement(s)" % len(findings))
             + (" | " + " | ".join(findings[:6]) if findings else ""))


# ------------------------------------------------------------------------------------------ c2  typegen
def c2_the_api_types_are_generated_and_nothing_is_hand_typed(ctx) -> tuple:
    """`src/api/schema.gen.ts` is current, the contract parses under a strict loader *and* under
    openapi-typescript, and no module hand-types an API body."""
    rc_api, out_api = sh(["npm", "run", "--silent", "check:api"], cwd=WEB, timeout=420)
    rc_spec, out_spec = sh([PY, str(ROOT / "tools/check-openapi.py")], timeout=300)
    gen = read(WEB / "src/api/schema.gen.ts")
    problems = []
    if rc_api != 0:
        problems.append("`npm run check:api` failed: " + out_api.strip().splitlines()[-1][:150] if out_api.strip() else "check:api rc=%d" % rc_api)
    if rc_spec != 0:
        problems.append("tools/check-openapi.py rc=%d: %s" % (rc_spec, out_spec.strip().splitlines()[-1][:150]))
    if not gen:
        problems.append("src/api/schema.gen.ts is missing — run `npm run gen:api`")
    typed = []
    for rel, text in tree_files(WEB, exts=(".ts", ".tsx")):
        for m in re.finditer(r"\btype\s+\w*(?:Response|ResponseBody|ApiBody|Payload)\b[^=\n]*=\s*\{", text):
            typed.append("%s: %s" % (rel, m.group(0)[:40]))
    if typed:
        problems.append("hand-typed API body shape(s): " + "; ".join(typed[:4]))
    m_spec = re.search(r"(\d+) passed, (\d+) failed", out_spec)
    if not m_spec:
        problems.append("tools/check-openapi.py printed no `N passed, M failed` summary, so its verdict is unreadable")
    elif int(m_spec.group(2)) or int(m_spec.group(1)) < 150:
        problems.append("the contract check reported %s passed / %s failed" % (m_spec.group(1), m_spec.group(2)))
    if gen and "auto-generated by openapi-typescript" not in gen[:400]:
        problems.append("schema.gen.ts lost the generator banner, so a hand edit would be indistinguishable")
    return ("c2", not problems,
             "check:api rc=%d, contract %s, schema.gen.ts %d lines, no hand-typed bodies%s"
             % (rc_api, ("%s passed / %s failed" % (m_spec.group(1), m_spec.group(2))) if m_spec else "unreadable",
                gen.count("\n") + 1,
                "" if not problems else " | " + " | ".join(problems[:5])))


# ---------------------------------------------------------------------------------------- c3  the doc
def resolve_marker(part: str, web: Path, line: int, problems: list) -> None:
    """One `path[::symbol][ prose]` fragment of a marker's test field.

    The prose after a symbol is the doc explaining the test (`::c7 (with a planted var that MUST fail)`), so
    the identifier is taken up to the first non-identifier character and the rest ignored. What is *not*
    ignored is a symbol that does not exist: a marker pointing at a test nobody wrote is the exact costume the
    standing rule is about, and the doc had two of them when this gate was first run."""
    if part.startswith("docs-only"):
        return
    if re.fullmatch(r"c\d+[a-z0-9_]*", part):        # `+ c13` means "this gate's check 13"
        part = "tools/p08-gate-check.py::" + re.match(r"c\d+", part).group(0)
    path_part, _, symbol = part.partition("::")
    path_part = re.split(r"\s", path_part.strip(), maxsplit=1)[0]
    symbol = symbol.strip()
    if " " in symbol:
        # A symbol with spaces is a test *title*, so most of it has to be there verbatim; the parenthetical
        # after `::c7 (with a planted var that MUST fail)` is the doc talking, so it is dropped.
        symbol = re.split(r"\s+\(", symbol, maxsplit=1)[0].rstrip(" .,;")
    else:
        m = re.match(r"[A-Za-z0-9_.-]+", symbol)
        symbol = m.group(0) if m else ""
    if not path_part:
        problems.append("line %d: the marker names no test file at all (%r)" % (line, part))
        return
    f = ROOT / path_part
    if not f.exists() and (web / path_part).exists():
        f = web / path_part
    if not f.exists():
        problems.append("line %d: test target %s is not a file in this repo" % (line, path_part))
        return
    if not symbol:
        return
    body = read(f)
    if f.name == Path(__file__).name:
        if not re.search(r"\b%s_" % re.escape(symbol), body):
            problems.append("line %d: %s has no check %s_ in it" % (line, path_part, symbol))
    elif symbol not in body:
        problems.append("line %d: %s does not contain %r, so the test named in the marker is not the test that exists"
                        % (line, path_part, symbol))


def marker_findings(doc_text: str, items: dict, web: Path) -> list:
    problems = []
    markers = doc_markers(doc_text)
    if not markers:
        return ["the document has no [owner: … · test: …] markers at all"]
    owner_line = items.get(19, "")
    owners = set(re.findall(r"`([a-z][a-z-]+)`", owner_line)) or {"frontend-owner", "design-owner", "security-owner", "backend-owner", "product-owner", "ops-ani"}
    for mk in markers:
        if "…" in mk["owner"] or "…" in mk["test"]:
            continue            # the line that shows the marker format is not itself a control claim
        if mk["owner"] not in owners:
            problems.append("line %d: owner %r is not one the launch review can assign (item 19 lists %s)"
                            % (mk["line"], mk["owner"], ", ".join(sorted(owners))))
        test = mk["test"]
        if test.startswith("docs-only"):
            m = re.search(r"launch item (\d+)", test)
            if not m or int(m.group(1)) not in items:
                problems.append("line %d: a control whose test is a document must name a launch item that exists" % mk["line"])
            continue
        for part in (s.strip() for s in test.split("+")):   # "a unit test + a gate check" is one honest answer
            resolve_marker(part, web, mk["line"], problems)
    docs_only = sum(1 for mk in markers if mk["test"].startswith("docs-only"))
    if docs_only > 2:
        problems.append("%d of %d controls are tested by a document; that is a costume, not a review"
                        % (docs_only, len(markers)))
    # every [UNVERIFIED] slug must be answered by a numbered launch item
    for m in re.finditer(r"\[UNVERIFIED — confirm before launch:\s*([a-z0-9-]+)\]", doc_text):
        slug = m.group(1)
        if not any(slug in text for text in items.values()):
            problems.append("`[UNVERIFIED — …: %s]` has no numbered launch item" % slug)
    # each control section must carry at least one marker, or the section is prose
    sections = re.split(r"^### ", doc_text, flags=re.M)[1:]
    for s in sections:
        title = s.splitlines()[0].strip()
        if not title.startswith("2."):
            continue
        if "[owner:" not in s and "2.8" not in title:
            problems.append("section %s declares controls with no owner/test marker" % title)
    return problems


def c3_every_control_in_the_doc_resolves_to_an_owner_and_a_real_test(ctx) -> tuple:
    """The standing rule, machine-resolved: an owner the launch review can assign, and a test that exists —
    a file, a named test inside it, or a check id in this script. Plus the pairing this phase added: every
    `[UNVERIFIED]` is answered by a numbered item, and every section with controls has a marker."""
    doc = ctx["doc"]
    problems = marker_findings(doc, ctx["items"], WEB)
    markers = doc_markers(doc)
    return ("c3", not problems,
             "%d markers, %d numbered launch items, %d [UNVERIFIED] slugs paired%s"
             % (len(markers), len(ctx["items"]),
                len(re.findall(r"\[UNVERIFIED — confirm before launch:", doc)),
                "" if not problems else " | " + " | ".join(problems[:6])))


# ------------------------------------------------------------------------------------ c4  the money path
def money_findings(web: Path) -> list:
    out = []
    for rel, text in tree_files(web, exts=(".ts", ".tsx")):
        stripped = strip_comments(text)
        in_money = rel.startswith("src/money/")
        for m in re.finditer(r"parseFloat\(|\.toFixed\(|Intl\.NumberFormat", stripped):
            if not in_money:
                out.append("%s: `%s` — money arithmetic outside src/money/ is the float coming back in" % (rel, m.group(0).rstrip("(")))
        if in_money:
            for m in re.finditer(r"\|\s*0\b|<<|>>>|>>", stripped):
                out.append("%s: bitwise `%s` in the money path reintroduces an int32 ceiling" % (rel, m.group(0).strip()))
        # a `Number(...)` wrapped around a value handed to the number component is an unchecked coercion
        for m in re.finditer(r"value=\{([^}]*)\}", stripped):
            if re.search(r"(^|[^A-Za-z])Number\(", m.group(1)):
                out.append("%s: value={%s} — coerce at the parse site where the contract is checked, not here" % (rel, m.group(1).strip()[:60]))
    return out


def c4_money_is_one_path_and_the_build_knows_it(ctx) -> tuple:
    """`src/money/` is the only place that parses or formats money, the number layer is the only renderer, and
    no bitwise coercion hides an int32 ceiling in the arithmetic that was written to avoid floats."""
    out = money_findings(WEB)
    callers = sorted({rel for rel, text in tree_files(WEB, exts=(".ts", ".tsx"))
                      if "formatNumber(" in text and not rel.startswith(("src/money/", "src/num/"))})
    if callers:
        out.append("formatNumber() called outside src/money and src/num (a second renderer): " + ", ".join(callers[:4]))
    return ("c4", not out,
            "parseFloat/toFixed/Intl.NumberFormat confined to src/money/, no bitwise in the money path, "
            "one numeric renderer%s" % ("" if not out else " | " + " | ".join(out[:6])))


# --------------------------------------------------------------------------------- c5  the token ceiling
LITERAL_PATTERNS = [
    (re.compile(r"#[0-9a-fA-F]{3,8}\b"), "a colour literal"),
    # `0` and `100%` are not design decisions, so a length has to carry a unit to be a literal worth banning.
    (re.compile(r"\b(\d+(?:\.\d+)?)px\b"), "a px literal"),
    (re.compile(r"\b\d+(?:\.\d+)?ms\b"), "a duration in ms"),
    (re.compile(r"z-index:\s*-?\d"), "a z-index literal"),
    (re.compile(r"border-radius:\s*\d"), "a radius literal"),
    (re.compile(r"font-size:\s*\d"), "a font-size literal"),
    (re.compile(r"(?:margin|padding|width|height|gap|inset-inline-start|inset-block-start)\s*:\s*\d+(?:\.\d+)?(?:rem|em|pt)"),
     "a numeric box property"),
]


def token_findings(web: Path, tokens_text: str) -> tuple:
    found, unknown = [], []
    declared = set(re.findall(r"(--pgm-[a-zA-Z0-9-]+)\s*:", tokens_text))
    used = set()
    bp = set(re.findall(r"--pgm-[a-zA-Z0-9-]+:\s*(\d+px)", tokens_text))
    for rel, text in tree_files(web, exts=(".ts", ".tsx", ".css")):
        if rel.startswith("styles/") or ".test." in rel:
            # styles/ is the generated mirror of brand/tokens.css, which *defines* lengths by construction and
            # is compared to its source by c6; and a test's own prose ("does not fire twice inside 120ms") is
            # not shipped CSS. Neither exemption is open-ended: a new file in either category still lands here.
            continue
        body = strip_comments(text)
        raw = text.splitlines()          # the media-query exemption below is *documented* by a comment, and a
        for n, line in enumerate(body.splitlines(), 1):   # comment is exactly what stripping removes
            if "--pgm-" in line and "var(" not in line and "setProperty" not in line:
                continue                                    # declarations and comment remnants, not uses
            for rx, what in LITERAL_PATTERNS:
                for m in re.finditer(rx, line):
                    if "@media" in line and m.groups() and m.group(1) + "px" in bp:
                        continue                      # the ladder's own number, written where var() cannot go
                    if "@media" in line and m.groups() and m.group(1).isdigit():
                        # `max-width: 767px` is one of the ladder's rungs minus a pixel, and only a nearby
                        # comment naming that rung makes it verifiable rather than plausible.
                        vals = {int(v[:-2]) for v in bp}
                        around = "\n".join(raw[max(0, n - 5):n + 1])
                        if "--pgm-bp-" in around and (int(m.group(1)) + 1) in vals:
                            continue
                    if "@media" in line and (m.group(1) + "px" in bp or not m.groups()):
                        # A media-query condition cannot read a custom property, so a breakpoint written there
                        # is allowed only when it is the ladder's own number, repeated, not a new one.
                        if m.group(1) + "px" in bp:
                            continue
                    found.append("%s:%d %s `%s`" % (rel, n, what, m.group(0)[:34]))
        for m in re.finditer(r"var\((--pgm-[a-zA-Z0-9-]+)\)", body):
            used.add(m.group(1))
    for name in sorted(used):
        if name not in declared:
            unknown.append(name)
    return found, unknown


def c5_the_design_tokens_are_the_only_source_of_dimensions(ctx) -> tuple:
    """The half of web/DESIGN.md §1 that P03's colour scan left open: no hex, px, ms, z-index, radius or
    font-size literal anywhere in the shipped tree, every `var(--pgm-*)` resolves in `brand/tokens.css`, and
    the Tailwind preset is the generated one."""
    rc_preset, out_preset = sh([PY if False else "node", str(ROOT / "tools/build-tailwind-preset.mjs"), "--check"], timeout=120)
    found, unknown = token_findings(WEB, read(TOKENS))
    problems = list(found[:20])
    if unknown:
        problems.append("%d var() name(s) with no declaration in brand/tokens.css: %s" % (len(unknown), ", ".join(unknown[:6])))
    if rc_preset != 0:
        problems.append("tailwind.preset.cjs is stale: " + (out_preset.strip().splitlines()[-1][:120] if out_preset.strip() else "rc=%d" % rc_preset))
    return ("c5", not problems,
            "%d literal(s) and %d unknown var() name(s) across the tree%s"
            % (len(found), len(unknown), "" if not problems else " | " + " | ".join(problems[:8])))


# -------------------------------------------------------------------------------------- c6  generated CSS
def c6_the_theme_layer_is_generated_current_and_parseable(ctx) -> tuple:
    """Three generators own the token files; `--check` is what makes them controls rather than habits. A real
    CSS parser also walks both token files, because every P03 check read them as text and shipped seven
    unclosed `@media` blocks. And the theme/density/reduced-motion mechanism has to be *in* the stylesheet."""
    checks = [
        (["node", str(ROOT / "tools/build-tokens.mjs"), "--check"], "brand/tokens.css"),
        (["node", str(ROOT / "tools/build-web-tokens.mjs"), "--check"], "web/styles/tokens.css"),
        (["node", str(ROOT / "tools/check-css-blocks.mjs"), str(TOKENS), str(WEB_TOKENS)], "block structure"),
        (["node", str(ROOT / "tools/check-css-blocks.mjs"), "--self-test"], "canaries for the above"),
    ]
    bad = []
    for argv, what in checks:
        rc, out = sh(argv, timeout=180)
        if rc != 0:
            tail = out.strip().splitlines()[-2:] or ["no output"]
            bad.append("%s: %s" % (what, " / ".join(t[-110:] for t in tail)))
    css = read(WEB / "src/globals.css") + read(WEB / "app/globals.css")
    for need in ("100dvh", "env(safe-area-inset", "prefers-reduced-motion", "data-density", "data-theme"):
        if need not in css:
            bad.append("the stylesheet no longer contains `%s`, so the mechanism that claim depends on is gone" % need)
    layout = read(WEB / "app/layout.tsx")
    if "theme-colors.json" not in layout:
        bad.append("app/layout.tsx reads its theme colours from somewhere other than the generated JSON mirror")
    return ("c6", not bad,
             "4 generated/structural checks over %d lines of token CSS and the shell stylesheet%s"
             % (read(TOKENS).count("\n"), "" if not bad else " | " + " | ".join(bad[:5])))


# --------------------------------------------------------------------------------------- c7  secret shape
def c7_no_secret_can_reach_the_bundle_and_the_frame_is_csp_per_surface(ctx) -> tuple:
    """assert-env before the build, the CI grep after it (over `.next`, the only place a value can arrive from
    a third route), and the CSP: `telegram.org` in `script-src` only on the subtree Telegram frames."""
    rc_env, out_env = sh(["node", "scripts/assert-env.mjs"], cwd=WEB, timeout=180)
    rc_scan, out_scan, scanned = (1, ".next/build output is absent — run `npm run build` (the grep of the built output is the control; there is no substitute for it)", 0)
    if (WEB / ".next").exists():
        # The scanner takes log lines through stdin or `--file`; `--sources` would re-scan *tracked* sources,
        # which is a different claim. The built output is what a value can arrive from by a third route, so the
        # files the build produced are what gets handed to it.
        rc_scan, out_scan = sh([PY, str(ROOT / "tools/ci-log-scan.py"), "--built", str(WEB / ".next")], timeout=900)
        scanned = int(re.search(r"(\d+) file\(s\)", out_scan).group(1)) if "file(s)" in out_scan else -1
    problems = []
    if rc_env != 0:
        problems.append("assert-env: " + (out_env.strip().splitlines()[-1][:160] if out_env.strip() else "rc=%d" % rc_env))
    if rc_scan != 0:
        problems.append("built-output scan: " + (out_scan.strip().splitlines()[-1][:200] if out_scan.strip() else "rc=%d" % rc_scan))
    cfg = strip_comments(read(WEB / "next.config.mjs"))  # a comment naming X-Frame-Options is not a header

    m = re.search(r"\"script-src 'self'\" \+ \(tma \? \" (https://telegram\.org)\" : \"\"\)", cfg)
    if not m:
        problems.append("the CSP builder no longer gates telegram.org behind the tma flag — grep the file, "
                        "a script-src that opens telegram.org everywhere lets any page call HapticFeedback and read initDataUnsafe")
    elsewhere = [rel for rel, text in tree_files(WEB, exts=(".tsx", ".html")) if "telegram.org/js" in text and not rel.startswith("app/tma/")]
    if elsewhere:
        problems.append("the Telegram bridge script is injected outside app/tma/: " + ", ".join(elsewhere))
    if re.search(r"X-Frame-Options", cfg):
        problems.append("next.config.mjs sets X-Frame-Options, which P07 recorded as the wrong tool for a "
                        "per-origin frame policy")
    # ...and the *values*, not just the shapes: whatever this environment holds in a server-only variable must
    # not be findable in the built output. Grep, not a re-read of every file — the tree is 400 files of chunks.
    secrets = sorted({v for k, v in os.environ.items() if k.startswith("PGM_") and len(v) >= 12
                      and not k.endswith(("_URL", "_ORIGIN", "_ALLOWED_ORIGINS"))})
    if (WEB / ".next").exists() and secrets:
        rc_v, out_v = sh(["grep", "-rlF"] + sum([["-e", v] for v in secrets], []) + [str(WEB / ".next")], timeout=180)
        if rc_v == 0:
            problems.append("a server-side PGM_* value is present in the built output: %s" % out_v.strip()[:200])
        elif rc_v != 1:
            problems.append("the value grep did not run (rc=%d) — a scanner that cannot read is not a control" % rc_v)
    # a NEXT_PUBLIC_* value is allowed to be in a bundle only if it is a URL, a path, or a flag
    odd = {k: v for k, v in os.environ.items() if k.startswith("NEXT_PUBLIC_") and v
           and not re.fullmatch(r"https?://\S+|/[A-Za-z0-9._/~-]*|true|false|\d+", v)}
    if odd:
        problems.append("NEXT_PUBLIC_* value(s) that are neither a URL/path nor a flag would be shipped to "
                        "every browser: " + ", ".join(sorted(odd))[:160])
    return ("c7", not problems,
            "assert-env rc=%d; built-output grep rc=%d over %d built file(s); telegram.org in script-src only on /tma%s"
            % (rc_env, rc_scan, scanned,
               "" if not problems else " | " + " | ".join(problems[:4])))


# ------------------------------------------------------------------------------------------ c8  measured

def bundle_source_hash(web: Path) -> str:
    """The files the first-load numbers describe: everything under `src/` and `app/`, plus what decides the build."""
    files = [p for d in ("src", "app") for p in (web / d).rglob("*") if p.is_file()]
    files += [p for p in (web / "package.json", web / "next.config.mjs", web / "scripts" / "measure-first-load.mjs")
              if p.is_file()]   # a canary fixture tree holds only what the canary needs; a hash that insists on
                                 # files a fixture does not have turns every canary into a crash, and a crashed
                                 # canary is indistinguishable from a scan that cannot fail
    h = hashlib.sha256()
    for p in sorted(files, key=lambda q: q.relative_to(web).as_posix()):
        h.update(p.relative_to(web).as_posix().encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:16]


def bundle_findings(text: str, web: Path, artefact: Path) -> list:
    out = []
    if not text.strip():
        return ["docs/verification/P08-bundle.txt is missing — `npm run measure` writes it"]
    # A hash of the sources, not their timestamps. The mtime rule that used to live here ("the record must be
    # newer than the newest source file") passed for five phases while three routes were over budget: a checkout
    # touches every file, so *newer* stopped meaning *true*, and the artefact was never re-measured because it
    # looked current. Two P15 checks were failing on that rule for the opposite reason at the same time. The
    # record now states which sources it measured; this recomputes and compares.
    want = bundle_source_hash(web)
    said = re.search(r"sources-sha256:\s*([0-9a-f]{16})", text)
    if not said:
        out.append("the artefact carries no `sources-sha256` line, so nothing in it says which sources it measured")
    elif said.group(1) != want:
        out.append("the artefact describes different sources (it says %s, the tree hashes to %s) — re-run `npm run build && npm run measure`"
                   % (said.group(1), want))
    if "status: pass" not in text:
        out.append("the artefact does not say `status: pass` (it says: %s)"
                   % (text.strip().splitlines()[-1][:80] if text.strip() else "empty"))
    budget = re.search(r"budget for the initial route:\s*(\d+)\s*KB", text)
    limit = int(budget.group(1)) if budget else 200
    rows = re.findall(r"^\s+(\S+)\s+http \d+ .*?JS ([\d.]+) KB.*?money layer (\w+)", text, re.M)
    if len(rows) < 2:
        out.append("only %d route(s) measured; a one-route measurement cannot show route-level splitting" % len(rows))
    present = [r for r in rows if r[2] == "present"]
    absent = [r for r in rows if r[2] == "absent"]
    if not present or not absent:
        out.append("the money layer is %s everywhere, which means the measurement cannot distinguish the two "
                   "cases the claim is about (present: %d, absent: %d)" % ("present" if present else "absent",
                                                                            len(present), len(absent)))
    for route, kb, _layer in rows:
        if float(kb) > limit:
            out.append("%s is %s KB against a %d KB budget" % (route, kb, limit))
    if "route-level splitting: proven" not in text:
        out.append("the artefact does not carry the splitting verdict line")
    # What the budget is a budget *of*, asserted rather than assumed. This check was passing on a record written
    # in the P08 era while the tree had grown past 200 KB on three routes: the number was stale, the artefact's
    # mtime was newer than every source file, and nothing in it had to say what it measured. So the line has to
    # name its scope (`first-party JS`), and the one script that is deliberately outside it — Telegram's platform
    # bridge, which the Mini App cannot work without and we do not build — has to be named, with its size, and
    # with the reason it is excluded. A new third-party script cannot be moved across that line quietly.
    if "first-party JS" not in text:
        out.append("the budget line does not say what it budgets: it must read `200 KB of first-party JS`")
    if "platform bridge" in text:
        if "excluded from the budget" not in text or "telegram.org/js/telegram-web-app.js" not in text:
            out.append("a platform bridge is measured but the artefact does not name what was excluded and why")
        if not re.search(r"platform bridge ([\d.]+) KB", text):
            out.append("the platform bridge is mentioned without a measured size")
    return out


def c8_the_payload_budget_is_measured_not_asserted(ctx) -> tuple:
    """The prompt's 200 KB, LCP and 60 fps lines: two of the three need a browser, which this environment does
     not have, so they are launch items with the [UNVERIFIED] marker. What *is* measurable here is measured:
    the gzipped bytes a running server actually sends per route, re-read if it is stale."""
    problems = bundle_findings(read(BUNDLE), WEB, BUNDLE)
    kb = re.findall(r"JS ([\d.]+) KB", read(BUNDLE))
    return ("c8", not problems,
            "worst route %s KB of a 200 KB budget; %d routes measured; the Lighthouse/frame-trace claims are "
            "launch items 3 and 4, not this artefact%s"
            % (max(kb) if kb else "?", len(kb), "" if not problems else " | " + " | ".join(problems[:4])))


# ---------------------------------------------------------------------------------------------- c9  i18n
CHROME = ("src/ui/", "src/shell/", "src/num/", "src/screens/", "src/live/", "src/telegram/", "src/auth/",
          "app/error.tsx", "app/not-found.tsx")
TEXT_NODE = re.compile(r'''>\s*([A-Za-z][A-Za-z ,.!?"\u2019-]{1,}?)\s*<''')
COPY_ATTR = re.compile(r'\b(help|why|label|title|extra|placeholder)="([^"]{3,})"')


LABELISH = re.compile(r'''\b(?:label|help|why|title|extra|placeholder|aria-label)\s*[:=]\s*(?:"([^"]+)"|`([^`]+)`)''')


def chrome_literal_findings(web: Path) -> list:
    """Copy that is not in the dictionary, by three routes: prose between tags, prose in a copy-bearing
    attribute, and a `label:`/`help:` field bound to a string literal.

    A `(file, line, value)` is reported once. `help="Some copy"` is both a copy attribute and a label-like
    field, and two messages for one defect is how a check gets skimmed instead of fixed."""
    out = []
    seen = set()
    for rel, text in tree_files(web, exts=(".tsx", ".ts")):
        if not any(rel.startswith(p) or rel == p for p in CHROME):
            continue
        body = strip_comments(text)
        for n, line in enumerate(body.splitlines(), 1):
            # JSX only: in a `.ts` file `>` closes a generic, so `Array<Promise>` is a type, not a tag, and a
            # rule that reads type arguments as untranslated copy gets silenced by being ignored.
            for m in (TEXT_NODE.finditer(line) if rel.endswith(".tsx") else []):
                key = (rel, n, m.group(1).strip())
                if key in seen:
                    continue
                words = re.findall(r"[A-Za-z']{2,}", m.group(1))
                # Two words of prose between tags is copy. So is one capitalised word (`>Continue<`). A single
                # lowercase identifier between angle brackets is a generic type argument, not markup.
                if len(words) >= 2 or (len(words) == 1 and m.group(1)[0].isupper()):
                    seen.add(key)
                    out.append("%s:%d untranslated chrome copy `%s`" % (rel, n, m.group(1)[:44]))
            for m in COPY_ATTR.finditer(line):
                if len(re.findall(r"[A-Za-z']{2,}", m.group(2))) < 3:
                    continue
                key = (rel, n, m.group(2).strip())
                if key in seen:
                    continue
                seen.add(key)
                out.append("%s:%d %s= copy outside the dictionary `%s`" % (rel, n, m.group(1), m.group(2)[:40]))
            for m in LABELISH.finditer(line):
                val = m.group(1) or m.group(2) or ""
                if "${" in val:
                    val = re.sub(r"\$\{[^}]*\}", "x", val)     # interpolated: the prose around it is still copy
                key = (rel, n, val.strip())
                if key in seen:
                    continue
                words = re.findall(r"[A-Za-z']{2,}", val)
                if len(words) >= 2 or (len(words) == 1 and words[0][0].isupper()):
                    seen.add(key)
                    out.append("%s:%d a %s bound to a literal instead of the dictionary: `%s`"
                               % (rel, n, m.group(0).split(":")[0].split("=")[0].strip(), val[:40]))
    return out


def c9_the_dictionary_is_the_only_source_of_ui_copy(ctx) -> tuple:
    """`i18n-check` runs as `pretest` and in CI; this re-runs it and *its* canaries, because the first version
    of the matcher could not see `t("key", {vars})` at all — 8 keys looked unused and a missing one would have
    looked nothing. Dynamic keys are a failure for the same reason: unchecked means unenforced."""
    rc, out = sh(["node", "scripts/i18n-check.mjs"], cwd=WEB, timeout=180)
    rc_self, out_self = sh(["node", "scripts/i18n-check.mjs", "--self-test"], cwd=WEB, timeout=180)
    problems = []
    if rc != 0:
        problems.append(out.strip().splitlines()[-1][:180] if out.strip() else "rc=%d" % rc)
    if rc_self != 0:
        problems.append("the i18n checker cannot fail: " + (out_self.strip()[:150] or "rc=%d" % rc_self))
    chrome = chrome_literal_findings(WEB)
    if chrome:
        problems.append("%d component(s) with copy outside the dictionary: %s" % (len(chrome), "; ".join(chrome[:3])))
    return ("c9", not problems,
            "%s | canary: %s%s" % ((out.strip().splitlines()[-1] if out.strip() else "i18n-check rc=%d" % rc),
                                   (out_self.strip().splitlines()[-1] if out_self.strip() else "rc=%d" % rc_self),
                                   "" if not problems else " | " + " | ".join(problems[:4])))


# ------------------------------------------------------------------------------- c10  price surfaces etc.
def jsx_element_blocks(text: str, tag: str):
    """`<Tag ... />` blocks, including multi-line ones, yielded with the line they start on."""
    for m in re.finditer(r"<%s\b" % re.escape(tag), text):
        end = text.find("/>", m.start())
        close = text.find(">", m.start())
        if end == -1 or (0 <= close < end and text[close - 1] not in "/"):
            end = close
        if end == -1:
            continue
        blk = text[m.start(): end + 2]
        yield text[:m.start()].count("\n") + 1, blk


def price_findings(web: Path) -> list:
    out = []
    for rel, text in tree_files(web, exts=(".tsx",)):
        for line, blk in jsx_element_blocks(text, "NumberView"):
            if rel.endswith(".test.tsx"):
                continue
            if 'kind="price"' not in blk:
                continue
            if "freshness=" not in blk or "staleMs=" not in blk:
                out.append("%s:%d a `kind=\"price\"` with no freshness/staleMs, i.e. a price that cannot say it is old" % (rel, line))
    return out


def c10_no_price_without_its_freshness_and_no_paywall_in_a_trade(ctx) -> tuple:
    """Four rules with one thing in common — a decision taken somewhere the user is not looking: a price is
    never shown without its staleness; the billing module is never imported by the ticket or the feed, so no
    paywall can appear mid-trade; the tape's live region is off with one sentence in a status region; and
    haptics fire only where a confirmation happened."""
    problems = price_findings(WEB)
    billing = re.compile(r"""from "@/screens/Billing|@/api/entitlement|useEntitlement|entitlement""")
    for rel, text in tree_files(WEB, exts=(".ts", ".tsx")):
        if rel in ("src/screens/TradeTicket.tsx",) or rel.startswith("src/live/"):
            for m in re.finditer(billing, text):
                if "t(\"" not in text[max(0, m.start() - 12):m.start()]:
                    problems.append("%s imports/uses billing (`%s`) — a plan check in the trade path is a paywall mid-trade"
                                    % (rel, m.group(0)))
    tape = read(WEB / "src/screens/Tape.tsx")
    if 'aria-live="off"' not in tape:
        problems.append("the tape list lost `aria-live=\"off\"`: a polite region at 20 updates/sec is unusable")
    if tape.count('role="status"') != 1:
        problems.append("the tape has %d status regions; the rule is one sentence per 5 seconds, not a stream" % tape.count('role="status"'))
    if "@tanstack/react-virtual" in tape or "react-window" in tape:
        problems.append("the tape imports a virtualiser while P08's own row cap holds — declared, not paid for")
    haptic_bad = []
    for rel, text in tree_files(WEB, exts=(".ts", ".tsx")):
        if ".test." in rel:
            continue                                   # a test that calls the function is not a call site
        for m in re.finditer(r"(?<!function )hapticConfirm\(", text):
            line = text[:m.start()].count("\n")
            src = text.splitlines()[line] if line < len(text.splitlines()) else ""
            if "usesMainButton(" not in src:
                haptic_bad.append("%s:%d hapticConfirm() without a usesMainButton() guard" % (rel, line + 1))
    if haptic_bad:
        problems.extend(haptic_bad)
    computed = sorted({rel for rel, text in tree_files(WEB, exts=(".ts", ".tsx"))
                       if ".test." not in rel and "canTradeGivenFreshness(" in text
                       and not rel.startswith(("src/api/envelope.ts", "src/live/useLive.ts"))})
    if computed:
        problems.append("canTrade computed outside useLive (two answers to one question): " + ", ".join(computed))
    return ("c10", not problems,
             "%d price surface(s) guarded, tape announcements capped, haptics on confirmations only%s"
             % (sum(1 for rel, text in tree_files(WEB, exts=(".tsx",))
                    for _l, b in jsx_element_blocks(text, "NumberView") if 'kind="price"' in b and "freshness=" in b),
                "" if not problems else " | " + " | ".join(problems[:5])))


# ---------------------------------------------------------------------------- c11  the two servers, live
GATE_EMAIL, GATE_PASSWORD = "gate@example.test", "correct horse battery staple 7!"


def http_call(port: int, method: str, path: str, body=None, headers=None, timeout: int = 30):
    """One request, one response, connection closed. `http.client` connections are not context managers, and
    `with HTTPConnection(...)` inside a poll loop turns that into a timeout that reads like a product failure."""
    conn = HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        return r.status, r.read().decode("utf-8", "replace"), r.getheaders()
    finally:
        conn.close()


def free_ports(count: int) -> list:
    """Bind 0 `count` times and read the numbers back. A fixed port is how this check spent 90 seconds timing
    out on a machine that already had something listening, and then reported the *product* as broken.

    Every socket stays open until all the ports are chosen: released one at a time, the kernel is free to hand
    back the same number twice — which put uvicorn on the web port and left `next start` binding nothing while
    the gate waited on a server that had already answered."""
    import socket
    socks, ports = [], []
    try:
        for _ in range(count):
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            ports.append(s.getsockname()[1])
            socks.append(s)
        if len(set(ports)) != count:
            raise RuntimeError("the kernel handed out the same port twice: %s" % ports)
        return ports
    finally:
        for s in socks:
            s.close()


class Servers:
    """The real plane: FastAPI under uvicorn, Next in production mode in front of it, one migrated+seeded
    SQLite file shared between them. An in-process TestClient could not prove any of what this checks — the
    cookie flags, the CSP header, the proxy hop and the refresh race are all properties of a served response.

    Everything that can go wrong during boot is a *boot* failure with the server's own log attached, never a
    silent timeout: `__enter__` tears down what it started if any step raises."""

    def __enter__(self):
        sys.path[:0] = [str(ROOT / x) for x in ("packages", "services/api", "tools", "tests")]
        import base64
        import contextlib
        import io
        import conftest
        os.environ["PGM_KEK_VERSION"] = "1"
        os.environ["PGM_KEK_v1"] = base64.b64encode(bytes(range(32, 64))).decode()
        os.environ["PGM_IP_PEPPER"] = "pepper-for-the-p08-gate"
        os.environ["PGM_SERVICE_TOKEN"] = "svc-" + "s" * 40
        os.environ["PGM_IMAGE_PROXY_SECRET"] = "img-" + "i" * 40
        os.environ["PGM_TELEGRAM_BOT_TOKEN"] = "7123456789:" + "Aa4" + "x" * 40
        os.environ["PGM_ADMIN_TOKEN"] = "adm_" + "k" * 44
        os.environ["PGM_TRUST_USER_HEADER"] = "1"
        os.environ.pop("PGM_REQUIRE_SECURITY_ENV", None)
        TMP.mkdir(exist_ok=True)
        # two logs, because "the plane did not boot" is only actionable if it says which half failed
        self.log_path = TMP / ("p08-plane-api-%d.log" % os.getpid())
        self.web_log_path = TMP / ("p08-plane-web-%d.log" % os.getpid())
        self.log = open(self.log_path, "w")
        self.web_log = open(self.web_log_path, "w")
        self.api = self.web = None
        self.api_port, self.web_port = free_ports(2)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                app = conftest.import_app("p08-web-plane")
            self.db = os.environ["PGM_DB_PATH"]
            now = int(time.time() * 1000)
            app._db.execute("INSERT INTO users (id, created_ms) VALUES (?,?)", ("u_p08gate", now))
            app.SEC.link_identity("u_p08gate", "email", GATE_EMAIL, at=now)
            app.SEC.set_password("u_p08gate", app._hasher().hash(GATE_PASSWORD), at=now)
            env = dict(os.environ)
            env["PYTHONPATH"] = ":".join(str(ROOT / x) for x in ("packages", "services/api", "services/executor",
                                                                  "services/executor-mock", "tools", "tests"))
            self.api = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--port", str(self.api_port),
                                         "--host", "127.0.0.1", "--log-level", "info"],
                                        cwd=str(ROOT / "services/api"), env=env,
                                        stdout=self.log, stderr=subprocess.STDOUT)
            self.wait(self.api_port, "/healthz")
            web_env = dict(os.environ)
            web_env.update({"PGM_API_ORIGIN": "http://127.0.0.1:%d" % self.api_port,
                            "PGM_ALLOWED_ORIGINS": "http://127.0.0.1:%d" % self.web_port,
                            "NEXT_PUBLIC_API_BASE_URL": "/api", "NODE_ENV": "production"})
            # `node node_modules/next/dist/bin/next`, not `npx`: the same choice measure-first-load.mjs makes,
            # for the same reason — npx resolves through a cache and can answer 127 instead of 1
            self.web = subprocess.Popen(["node", str(WEB / "node_modules" / "next" / "dist" / "bin" / "next"),
                                         "start", "-p", str(self.web_port), "-H", "127.0.0.1"],
                                        cwd=str(WEB), env=web_env,
                                        stdout=self.web_log, stderr=subprocess.STDOUT)
            self.wait(self.web_port, "/", needle="<!doctype html")
        except Exception:
            self.stop()
            raise
        return self

    def wait(self, port: int, path: str, needle=None, timeout: int = 75) -> None:
        """Poll until the port answers. `needle` says *which* server must answer: the web half has to return
        HTML, so a port collision is reported as a collision instead of presenting as a hang."""
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            if not self.alive():
                raise RuntimeError("a server exited during boot; logs: %s" % self.tail())
            try:
                status, body, _h = http_call(port, "GET", path, timeout=3)
                if needle and needle not in body.lower():
                    raise RuntimeError("port %d answered %d without %r — the other server is on this port"
                                       % (port, r.status, needle))
                if status < 500 or path != "/healthz":
                    return
                last = "health %d" % status
            except RuntimeError:
                raise
            except Exception as e:
                last = "%s: %s" % (type(e).__name__, str(e)[:80])
            time.sleep(0.5)
        raise RuntimeError("nothing usable on port %d (%s); log: %s" % (port, last, self.tail()))

    def alive(self) -> bool:
        return all(p is None or p.poll() is None for p in (self.api, self.web))

    def tail(self) -> str:
        bits = []
        for label, path in (("api", self.log_path), ("web", self.web_log_path)):
            try:
                bits.append("%s: %s" % (label, " / ".join(l.strip() for l in read(path).splitlines()[-4:])[:260]))
            except Exception:
                bits.append("%s: (no log)" % label)
        return " | ".join(bits)

    def __exit__(self, *_exc):
        self.stop()

    def stop(self) -> None:
        for pr in (self.web, self.api):
            if pr and pr.poll() is None:
                pr.terminate()
        for pr in (self.web, self.api):
            if pr:
                try:
                    pr.wait(timeout=20)
                except Exception:
                    pr.kill()
        for handle in (getattr(self, "log", None), getattr(self, "web_log", None)):
            try:
                if handle:
                    handle.close()
            except Exception:
                pass


def req(srv: "Servers", method: str, path: str, body=None, cookies: dict | None = None, headers: dict | None = None):
    h = {"host": "127.0.0.1:%d" % srv.web_port, "accept": "application/json"}
    if cookies:
        h["cookie"] = "; ".join("%s=%s" % kv for kv in cookies.items())
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["content-type"] = "application/json"
    h.update(headers or {})
    status, text, raw_headers = http_call(srv.web_port, method, path, body=data, headers=h)
    if not srv.alive():
        raise RuntimeError("a server died mid-test; log: %s" % srv.tail())
    sets = [v for k, v in raw_headers if k.lower() == "set-cookie"]
    jar = {}
    for s in sets:
        m = re.match(r"([^=]+)=([^;]*)", s)
        if m:
            jar[m.group(1)] = (m.group(2), s)
    return status, text, jar


def integration_findings(srv) -> list:
    origin = {"origin": "http://127.0.0.1:%d" % srv.web_port, "sec-fetch-site": "same-origin"}
    problems = []
    st, body, _ = req(srv, "POST", "/api/session/login", {"identifier": GATE_EMAIL, "password": "wrong"}, headers=origin)
    if st == 200:
        problems.append("a wrong password was accepted")
    st, body, jar = req(srv, "POST", "/api/session/login", {"identifier": GATE_EMAIL, "password": GATE_PASSWORD},
                        headers=origin)
    if st != 200:
        return ["login through the proxy returned %d: %s" % (st, body[:160])]
    if "accessToken" in body or "refreshToken" in body:
        problems.append("the login response body carries a token — the page must never hold one: %s" % body[:120])
    at, rt = jar.get("pgm_at"), jar.get("pgm_rt")
    if not at or "HttpOnly" not in at[1]:
        problems.append("pgm_at was not set HttpOnly (set-cookie: %s)" % (at[1][:130] if at else "absent"))
    if not rt or "HttpOnly" not in rt[1]:
        problems.append("pgm_rt was not set HttpOnly")
    if at and "SameSite=None" in at[1] and "Secure" not in at[1]:
        problems.append("a SameSite=None cookie without Secure — the webview needs both, and one without the other is a bug")
    cookies = {k: v[0] for k, v in jar.items() if k in ("pgm_at", "pgm_rt")}
    if "pgm_at" not in cookies:
        return problems + ["no pgm_at cookie to test with"]
    st, body, _ = req(srv, "GET", "/api/v1/markets", cookies=cookies, headers=origin)
    if st != 200 or "asOf" not in body:
        problems.append("an authenticated read through the proxy did not return a stamped body (%d): %s" % (st, body[:140]))
    st, body, _ = req(srv, "POST", "/api/session/login", {"identifier": GATE_EMAIL, "password": "x"},
                      headers={"origin": "https://evil.example", "sec-fetch-site": "cross-site"})
    if st != 403 or "CSRF_ORIGIN" not in body:
        problems.append("a cross-site POST was not refused with CSRF_ORIGIN (%d): %s" % (st, body[:140]))
    # single-flight under the worst condition the design has: the access token is expired for every request,
    # the refresh token is single-use, and reuse revokes the family. Five parallel requests must still be one
    # rotation, so all five succeed and the session survives afterwards.
    expired = dict(cookies)
    expired["pgm_at"] = "1:" + cookies["pgm_at"].split(":", 1)[-1]
    import threading
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(
        req(srv, "GET", "/api/v1/markets", cookies=expired, headers=origin))) for _ in range(5)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(40)
    # The statuses alone are not actionable: a 500 from the API and a 500 from the proxy look identical in a
    # list of five numbers, and this drill failed twice on a loaded machine with exactly that list. Capture the
    # body and the API's log tail for the failing ones — a drill that cannot say WHY it failed is a drill whose
    # next failure costs another afternoon.
    if len(results) != 5 or any(r[0] != 200 for r in results):
        detail = "; ".join("%d %s" % (r[0], (r[1] or "")[:160]) for r in results if r[0] != 200)
        tail = ""
        try:
            tail = open(srv.log_path).read()[-600:].replace("\n", " ")[-400:]
        except Exception:
            pass
        problems.append("5 concurrent reads on an expired access token gave %s — that is the refresh "
                        "single-flight failing, and upstream it ends every session the user has | %s | api: %s"
                        % ([r[0] for r in results], detail, tail))
    st, body, _ = req(srv, "GET", "/profile")
    if st not in (302, 307, 308) and "Terminal" in body:
        problems.append("an anonymous /profile response carried the protected frame (%d) — the logged-out flash" % st)
    st, body, jar3 = req(srv, "POST", "/api/session/logout", cookies=cookies, headers=origin)
    cleared = any("Max-Age=0" in v[1] or v[0] == "" for k, v in jar3.items() if k in ("pgm_at", "pgm_rt"))
    if st != 200 or not cleared:
        problems.append("logout did not clear the cookies (%d, %s)" % (st, "cleared" if cleared else "still set"))
    return problems


def c11_the_proved_by_a_served_response(ctx) -> tuple:
    """The claims that only a running pair can answer: the proxy sets httpOnly cookies and hands the browser no
    token; a cross-site POST is refused; five concurrent expired-access reads cause one rotation, not five;
    a signed-out request never renders the protected frame; logout clears the jar."""
    if not (WEB / ".next" / "BUILD_ID").exists():
        return ("c11", False, "web/.next is not built — `npm run build` first; this check refuses to pass by skipping")
    try:
        with Servers() as srv:
            problems = integration_findings(srv)
    except Exception as e:
        return ("c11", False, "the plane did not boot: %s: %s" % (type(e).__name__, str(e)[:180]))
    return ("c11", not problems,
            "login/CSRF/refresh-race/logout all exercised against next start + uvicorn%s"
            % ("" if not problems else " | " + " | ".join(problems[:5])))


# ------------------------------------------------------------------------------- c12  unbuilt is visible
def refusal_findings(web: Path, routes: dict) -> list:
    texts = {rel: text for rel, text in tree_files(web, exts=(".ts", ".tsx"))}
    allt = "\n".join(texts.values())
    notices = set(re.findall(r'<RefusalNotice\s+route="(\w+)"', allt))
    gated = {k for k in re.findall(r"ROUTES\.(\w+)\.built", allt)}
    out = []
    unreferenced = []
    for key, d in routes.items():
        if d["built"]:
            continue
        touched = key in notices or key in gated or re.search(r'key:\s*"%s"' % key, allt)
        if not touched:
            unreferenced.append(key)
            continue
        if d["whileMissing"] == "hides" and key not in gated:
            out.append("`%s` hides while missing but nothing gates on ROUTES.%s.built" % (key, key))
        if d["whileMissing"] == "refuses" and key not in notices:
            out.append("`%s` is called or wired in the tree with no RefusalNotice — that is a screen made to look built" % key)
    for rel, text in texts.items():
        if rel.endswith("index.ts") and "/src/" in "/" + rel:
            out.append("%s: a barrel file (import cycles arrive at 2am in a file nobody owns)" % rel)
        if re.search(r"^export \* from", text, re.M):
            out.append("%s: `export * from` — the barrel's worse cousin" % rel)
    palette = read(web / "src/shell/CommandPalette.tsx")
    if "ROUTES.globalSearch.built" not in palette:
        out.append("the palette no longer gates search on ROUTES.globalSearch.built (a local filter presented as search)")
    client = read(web / "src/api/client.ts")
    if not re.search(r"if \(!decl\.built\) return", client):
        out.append("src/api/client.ts no longer refuses an unbuilt route before the network — every unbuilt screen would 404 instead")
    # A route with no UI is not a lie on screen, but it is an entry in the ledger that nothing will notice
    # going stale, so it is reported through the launch list: §4 must name it, or the gap is unowned.
    for key in sorted(unreferenced):
        owner = routes[key]["owner"]
        m = re.fullmatch(r"P08-L(\d+)", owner)
        if not m:
            out.append("`%s` is unbuilt, has no UI and has no launch item (%r)" % (key, owner))
        else:
            out.append("%s" % ("`%s` is unbuilt with no UI at all: launch item %s owns it, so nothing will notice when the API lands it" % (key, m.group(1))) if False else "ok")
    out = [o for o in out if o != "ok"]
    return out


def c12_an_unbuilt_capability_is_visible_on_screen(ctx) -> tuple:
    """The ledger's purpose: a missing route is a refusal the user can read, never a 404 dressed as an empty
    state, never a spinner, never a mocked success. No barrels, either — the same rule against a second
    indirection layer."""
    problems = refusal_findings(WEB, ctx["routes"])
    n = sum(1 for d in ctx["routes"].values() if not d["built"])
    return ("c12", not problems,
            "%d unbuilt routes, every one of them refused or gated in the tree; no src barrels%s"
            % (n, "" if not problems else " | " + " | ".join(problems[:5])))


# ------------------------------------------------------------------------------------- c13 rails & cap
def c13_the_frame_itsself_holds_its_budgets(ctx) -> tuple:
    """Rails persist per device as viewport fractions with sane bounds, the tape cap is the design system's
    DOM budget rather than a suggestion, and the mobile tab set is the five that were argued for. Also where
    the one deviation from the prompt is checked: two rails are resizable, and §3 must say so."""
    problems = []
    tape = read(WEB / "src/screens/Tape.tsx")
    m = re.search(r"export const TAPE_ROW_CAP\s*=\s*(\d+)", tape)
    if not m:
        problems.append("no TAPE_ROW_CAP in the tape — the row budget moved somewhere nobody reads")
    elif int(m.group(1)) > 64:
        problems.append("TAPE_ROW_CAP is %s; web/DESIGN.md §4 caps the tape at 64 rows" % m.group(1))
    elif "Math.min(rows, TAPE_ROW_CAP)" not in tape:
        problems.append("the cap is declared but not applied to the caller's `rows`")
    rails = read(WEB / "src/shell/rails.ts")
    mn = re.search(r"RAIL_MIN\s*=\s*([\d.]+)", rails)
    mx = re.search(r"RAIL_MAX\s*=\s*([\d.]+)", rails)
    if not (mn and mx):
        problems.append("the rail bounds are gone")
    else:
        lo, hi = float(mn.group(1)), float(mx.group(1))
        if not (0 < lo < hi <= 0.5):
            problems.append("rail fractions %s..%s must be a positive fraction with both rails under half the viewport" % (lo, hi))
    if 'KEY = "pgm.rails.v1"' not in rails:
        problems.append("the rail storage key lost its version suffix, so an old shape is loaded as a new one")
    if "clampFraction(typeof" not in rails and "!Number.isFinite" not in rails:
        problems.append("loadRails no longer refuses a non-finite stored value")
    hooks = read(WEB / "src/shell/useRails.ts")
    sides = len(re.findall(r'"left" \| "right"', hooks))
    doc = ctx["doc"]
    if sides and "rail" not in doc.split("## 3.", 1)[1].split("## 4.", 1)[0].lower():
        problems.append("the frame has two resizable rails while the prompt asked for three — §3 must record that, or the gap is invisible")
    src = read(WEB / "src/shell/shortcuts.ts")
    mm = re.search(r"export const MOBILE_TABS\s*=\s*\[(.*?)\n\](?: as const)?;", src, re.S)
    block = mm.group(1) if mm else ""
    tabs = re.findall(r'href:\s*"(/[^"]*)"', block)
    n = block.count("href:")
    if n != 5 or len(set(tabs)) < 5:
        problems.append("MOBILE_TABS is %d entries over %d hrefs; the argued set is five" % (n, len(set(tabs))))
    return ("c13", not problems,
            "cap %s, rails %s..%s of the viewport per device, %d tabs%s"
            % (m.group(1) if m else "?", mn.group(1) if mn else "?", mx.group(1) if mx else "?", n,
               "" if not problems else " | " + " | ".join(problems[:4])))


# --------------------------------------------------------- c16 the key a client sends is the key we validate
def idem_key_findings(repo: Path) -> list:
    """One rule stated in two files and *produced* in a third, and nothing that makes them agree.

    The `Idempotency-Key` shape is declared in `contracts/openapi.yaml`, enforced by `_IDEM_RE` in the API, and
    manufactured by `newIdempotencyKey` in `web/src/api/client.ts`. The client joined its scope and its random half
    with a colon for the whole of P08–P10, so every mutation that did not pass a key of its own was refused with a
    422 about a header the client had just generated itself. No screen showed it: the screens that were exercised
    pass their own key, and a missing header is caught by the API, not by the UI. This check reads the rule from
    the two files that state it and the producer from the third, and refuses a producer that cannot satisfy it.
    """
    problems = []
    want = "^[A-Za-z0-9_-]{8,128}$"
    contract = read(repo / "contracts" / "openapi.yaml")
    if ("pattern: '%s'" % want) not in contract:
        problems.append("the contract no longer declares the Idempotency-Key pattern %s" % want)
    api = read(repo / "services" / "api" / "app.py")
    m = re.search(r"_IDEM_RE\s*=\s*re\.compile\(r\"([^\"]+)\"\)", api)
    if not m:
        problems.append("_IDEM_RE is gone from services/api/app.py: the key is validated somewhere else now")
    elif m.group(1) != want:
        problems.append("the API validates %s while the contract declares %s" % (m.group(1), want))

    allowed = set(string.ascii_letters + string.digits + "_-")
    # Comments out first: the first run of this check failed on the function's own docstring, which quotes the
    # shape it has to satisfy (a backticked rule contains a colon and braces). A scan that reads prose as code is
    # a scan that reports the documentation as the bug.
    client = strip_comments(read(repo / "web" / "src" / "api" / "client.ts"))
    body = client.split("export function newIdempotencyKey", 1)[-1].split("\nexport ", 1)[0]
    if not body.strip():
        problems.append("newIdempotencyKey is gone: something else generates the keys now, and it is not checked")
    else:
        for lit in re.findall(r"`([^`]*)`", body):
            # What the template injects literally, with the `${…}` substitutions taken out: the literal is the
            # separator and any fixed decoration, and it is the only part this function can be wrong about.
            literal = re.sub(r"\$\{[^}]*\}", "", lit)
            injected = sorted({c for c in literal if c not in allowed})
            if injected:
                problems.append("a key template injects %r, which the key rule does not allow" % "".join(injected))
        if not re.search(r"slice\(0,\s*\d+\)|substring\(", body):
            problems.append("the client no longer clamps the scope, so a long scope can exceed the 128-char limit")
    test = read(repo / "web" / "src" / "api" / "client.test.ts")
    if want not in test:
        problems.append("web/src/api/client.test.ts does not assert the shape the server enforces")
    return problems


def c16_the_client_and_the_contract_agree_on_the_idempotency_key(ctx) -> tuple:
    problems = idem_key_findings(ROOT)
    return ("c16", not problems,
            "one shape (`^[A-Za-z0-9_-]{8,128}$`) in the contract, in `_IDEM_RE` and in the client's producer%s"
            % ("" if not problems else " | " + " | ".join(problems[:3])))


# --------------------------------------------------------------------------------------- c14 boundaries
def boundary_findings(web: Path) -> list:
    problems = []
    if not (web / "app/error.tsx").exists():
        problems.append("app/error.tsx is gone: a route with no boundary is a blank page")
    wb = read(web / "src/ui/WidgetBoundary.tsx")
    if "getDerivedStateFromError" not in wb or "componentDidCatch" not in wb:
        problems.append("WidgetBoundary is not an error boundary (no getDerivedStateFromError/componentDidCatch)")
    if "componentStack" not in wb:
        problems.append("the boundary logs the component stack; without it a widget failure is a message with no name")
    texts = {rel: read(web / rel) for rel in sorted(p.relative_to(web).as_posix() for p in (web / "app").rglob("*.tsx"))}
    for rel, text in texts.items():
        if not rel.endswith("page.tsx"):
            continue
        widgets = re.findall(r"<([A-Z]\w+)", text)
        screens = [w for w in widgets if ("@/screens/%s" % w) in text or ("@/shell/%s" % w) in text]
        if len(screens) < 2:
            continue                       # one widget on a page is covered by the route boundary
        wrapped = text.count("<WidgetBoundary")
        selfwrap = sum(1 for w in screens
                       if "<WidgetBoundary" in read(web / ("src/screens/%s.tsx" % w)) if (web / ("src/screens/%s.tsx" % w)).exists())
        if wrapped + selfwrap < len(screens):
            problems.append("%s composes %d widgets inside %d boundaries — one crash must not blank the frame"
                            % (rel, len(screens), wrapped + selfwrap))
        for m in re.finditer(r"<WidgetBoundary((?:[^>]*)?)>", text):
            if "label=" not in m.group(1):
                problems.append("%s: a boundary with no label cannot say which widget died" % rel)
    return problems


def c14_every_route_has_a_boundary_and_every_widget_has_one_of_its_own(ctx) -> tuple:
    """Per-route and per-widget are different granularities and the prompt asks for both; a boundary that does
    not name its widget is a blank rectangle with a stack trace nobody reads."""
    problems = boundary_findings(WEB)
    return ("c14", not problems,
            "route boundary + %d widget boundaries over %d page(s)%s"
            % (sum(read(p).count("<WidgetBoundary") for p in sorted((WEB / "app").rglob("*.tsx"))),
               len(list((WEB / "app").rglob("page.tsx"))),
               "" if not problems else " | " + " | ".join(problems[:4])))


# ----------------------------------------------------------------------------------------- c15 the suite
def c15_the_test_suite_is_green_and_big_enough_to_mean_it(ctx) -> tuple:
    """The phase's gate line. Run whole: `pretest` fires the dictionary check, vitest runs the 15 files, and a
    suite that quietly collected two tests would pass everything above it."""
    rc, out = sh(["npm", "run", "--silent", "test"], cwd=WEB, timeout=900)
    out = re.sub(r"\x1b\[[0-9;]*m", "", out)          # vitest paints its summary; the gate reads text
    m = re.search(r"Tests\s+(\d+) passed(?:.*?(\d+) skipped)?", out, re.S)
    problems = []
    if not m:
        # Name the failing file. Without this the gate said "1 failed | 32 passed" and the *identity* of the
        # failure — the only part that can be acted on — was in the truncated output. A gate that reports a
        # count and withholds the name sends the reader to run the suite by hand, which is what happened.
        fails = [re.sub(r"\s+", " ", ln).strip() for ln in out.splitlines()
                 if re.search(r"^\s*(FAIL|❯|×)\s", ln) or " failed)" in ln or re.match(r"^\s*FAIL ", ln)]
        fnames = []
        for ln in out.splitlines():
            fm = re.search(r"(src/[\w./-]+\.test\.tsx?)", ln)
            if fm and ("failed" in ln.lower() or "✗" in ln or "×" in ln):
                fnames.append(fm.group(1))
        named = sorted(set(fnames))[:3] or fails[:3]
        problems.append("no `Tests N passed` line: "
                        + ("; ".join(named) if named else (out.strip().splitlines()[-1][:160] if out.strip() else "no output")))
    else:
        if int(m.group(1)) < 80:
            problems.append("only %s tests ran — the phase's rules need more than that to be covered" % m.group(1))
        if m.group(2):
            problems.append("%s skipped test(s): a skipped test is an unwritten one" % m.group(2))
    if rc != 0:
        problems.append("npm run test exited %d" % rc)
    return ("c15", not problems,
            "%s%s" % ((re.search(r"Test Files.*", out).group(0).strip() if re.search(r"Test Files.*", out) else "no summary"),
                      "" if not problems else " | " + " | ".join(problems[:3])))


# ------------------------------------------------------------------------------------------- --self-test
def fixture(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def self_test() -> int:
    """Plant each violation the scans claim to catch. A scan whose canary walks past is deleted, not kept: this
    phase's own §2.8 is a list of checks that reported green while the thing under them was broken."""
    TMP.mkdir(exist_ok=True)
    cases, passes = [], 0

    def canary(fn):
        cases.append(fn)
        return fn

    @canary
    def c16_idem(name=TMP / "p08-self-c16"):
        """A client that joins its scope and its random half with a colon: the shipped bug, in a fixture."""
        root = name / "repo"
        shutil.rmtree(name, ignore_errors=True)
        fixture(root, "contracts/openapi.yaml", "        pattern: '^[A-Za-z0-9_-]{8,128}$'\n")
        fixture(root, "services/api/app.py", '_IDEM_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")\n')
        fixture(root, "web/src/api/client.test.ts", 'expect(key).toMatch(/^[A-Za-z0-9_-]{8,128}$/);\n')
        fixture(root, "web/src/api/client.ts",
                'export function newIdempotencyKey(scope: string): string {\n'
                '  const bytes = new Uint8Array(8);\n'
                '  return `${scope}:${rand}`;\n'
                '}\nexport const other = 1;\n')
        bad = idem_key_findings(root_path := root)
        # The same fixture with the clamp and the hyphen separator: the scan must walk past a good client.
        fixture(root, "web/src/api/client.ts",
                'export function newIdempotencyKey(scope: string): string {\n'
                '  const flat = scope.replace(/[^A-Za-z0-9_-]+/g, "-").slice(0, 111);\n'
                '  return `${flat}-${rand}`;\n'
                '}\nexport const other = 1;\n')
        # …and the clean one carries a docstring that quotes the rule, which is exactly how the real client is
        # written: the comment must not be read as a template.
        good = idem_key_findings(root_path)
        fixture(root, "web/src/api/client.ts",
                '/** Keys look like `abc-123`: 8-128 chars of `[A-Za-z0-9_-]`. */\n'
                'export function newIdempotencyKey(scope: string): string {\n'
                '  const flat = scope.replace(/[^A-Za-z0-9_-]+/g, "-").slice(0, 111);\n'
                '  return `${flat}-${rand}`;\n'
                '}\nexport const other = 1;\n')
        documented = idem_key_findings(root_path)
        return (len(bad) == 2 and not good and not documented,
                {"planted": bad, "clean": good, "documented": documented})

    @canary
    def c4_money(name=TMP / "p08-self-c4"):
        shutil.rmtree(name, ignore_errors=True)
        fixture(name, "src/ui/Planted.tsx", 'const x = parseFloat(y);\nexport const z = v.toFixed(2);\n')
        fixture(name, "src/money/cents.ts", 'export const w = (a + b) | 0;\n')
        fixture(name, "src/screens/Planted2.tsx", '<NumberView kind="count" value={Number(row.x)} />\n')
        got = money_findings(name)
        return len(got) >= 3, got

    @canary
    def c5_tokens(name=TMP / "p08-self-c5"):
        shutil.rmtree(name, ignore_errors=True)
        fixture(name, "src/globals.css", ".a { color: #ff0000; padding: 12px; transition: 200ms; z-index: 5; border-radius: 4px; width: var(--pgm-not-a-token); }\n")
        found, unknown = token_findings(name, "--pgm-space-2: 8px;\n")
        return len(found) >= 5 and unknown == ["--pgm-not-a-token"], (found, unknown)

    @canary
    def c3_markers(name=TMP / "p08-self-c3"):
        doc = ("### 2.1 x\n- a control [owner: nobody-owner · test: tools/does-not-exist.py::c9]\n"
               "- another [owner: frontend-owner · test: web/src/live/ws.test.ts::a test that was renamed]\n"
               "- `[UNVERIFIED — confirm before launch: ghost-slug]` needs an item\n"
               "## 4. Launch checklist\n1. **`signup` — serve it.**\n")
        problems = marker_findings(doc, {1: "`signup` — serve it."}, WEB)
        want = ("nobody-owner", "does-not-exist", "a test that was renamed", "ghost-slug")
        return all(any(w in p for p in problems) for w in want), problems

    @canary
    def c1_ledger(name=TMP / "p08-self-c1"):
        routes = {"madeUp": {"method": "GET", "path": "/v1/made-up", "built": True, "whileMissing": "refuses", "owner": "P07"},
                  "signup": {"method": "POST", "path": "/v1/auth/signup", "built": False, "whileMissing": "refuses", "owner": "P08-L9"},
                  "ghost": {"method": "POST", "path": "/v1/ghost", "built": False, "whileMissing": "refuses", "owner": "not-an-item"}}
        ops = {("POST", "/v1/auth/signup")}
        got = " | ".join(ledger_findings(routes, ops, {1: "something else"}))
        want = ("madeUp` claims built:true", "signup` claims built:false", "not a P08-L item", "no such item")
        return all(w in got for w in want), got[:200]

    @canary
    def c1_notes(name=TMP / "p08-self-c1-notes"):
        routes = {"madeUp": {"method": "GET", "path": "/v1/made-up", "built": True, "whileMissing": "refuses", "owner": "P07"},
                  "signup": {"method": "POST", "path": "/v1/auth/signup", "built": False, "whileMissing": "refuses", "owner": "P08-L1"},
                  "ghost": {"method": "POST", "path": "/v1/ghost", "built": False, "whileMissing": "refuses", "owner": "P08-L1"}}
        got = " | ".join(ledger_findings(routes, {}, {1: "`signup` — serve it."}, {"signup": "not built yet",
                                                                                   "retired": "a route that left"}))
        want = ("`retired`", "not a route in the ledger")
        return all(w in got for w in want) and "not built yet" not in got, got[:220]

    @canary
    def c9_chrome_copy(name=TMP / "p08-self-c9"):
        shutil.rmtree(name, ignore_errors=True)
        fixture(name, "src/ui/Planted.tsx",
                'export const A = () => <div className="x"><strong>This copy never went through the dictionary</strong>'
                '<Button>Continue</Button><Field help="A whole sentence nobody translated either" /></div>;\n')
        got = chrome_literal_findings(name)
        return len(got) == 3, got

    @canary
    def c10_price(name=TMP / "p08-self-c10"):
        shutil.rmtree(name, ignore_errors=True)
        fixture(name, "src/screens/Bare.tsx", 'export const A = () => <NumberView\n kind="price"\n value={425}\n tick="0.001" />;\n')
        fixture(name, "src/screens/Oked.tsx", 'export const A = () => <NumberView kind="price" value={1} tick="0.01" freshness={f} staleMs={a} />;\n')
        got = price_findings(name)
        return len(got) == 1 and "Bare" in got[0], got

    @canary
    def c12_refusal(name=TMP / "p08-self-c12"):
        shutil.rmtree(name, ignore_errors=True)
        fixture(name, "src/api/routes.ts", 'export const ROUTES = {\n  fakeThing: { method: "POST", path: "/v1/fake", built: false, whileMissing: "refuses", owner: "P08-L1" },\n  fakeHide: { method: "GET", path: "/v1/fake-hide", built: false, whileMissing: "hides", owner: "P08-L2" },\n} satisfies Record<string, RouteDecl>;\n')
        fixture(name, "src/screens/Uses.tsx",
                'const r = await request({ key: "fakeThing" });\n'
                '// `fakeHide` is wired to run a query but the component never gates on `built`, so an\n'
                '// "hides while missing" route would show an empty panel instead of disappearing.\n'
                'const q = await request({ key: "fakeHide" });\n')
        fixture(name, "src/ui/index.ts", 'export * from "./Button";\n')
        got = refusal_findings(name, parse_ledger(name))
        joined = " ".join(got)
        # three distinct holes planted, three findings: a wired refusal with no notice, a barrel, and an
        # `hides` route that nothing gates on. A fourth kind of finding is fine; a missing one is not.
        want = ("fakeThing", "index.ts", "hides while missing")
        return all(w in joined for w in want), got

    @canary
    def c14_boundaries(name=TMP / "p08-self-c14"):
        shutil.rmtree(name, ignore_errors=True)
        fixture(name, "src/ui/WidgetBoundary.tsx", 'export class WidgetBoundary extends Component {}\n')
        fixture(name, "app/x/page.tsx", 'import { A } from "@/screens/A";\nimport { B } from "@/screens/B";\nexport default () => <div><A /><B /></div>;\n')
        got = boundary_findings(name)
        return len(got) >= 3, got

    @canary
    def c8_bundle(name=TMP / "p08-self-c8"):
        d = TMP / "p08-self-c8"
        shutil.rmtree(d, ignore_errors=True)
        web = d / "web"
        fixture(web, "src/num/Number.tsx", "export const x = 1;\n")
        art = d / "bundle.txt"
        art.write_text("budget for the initial route: 200 KB of first-party JS\n"
                       "sources-sha256: 0000000000000000\n"
                       "  /  http 200 · 10 chunk(s) · JS 300.0 KB · CSS 3 KB · money layer absent\n"
                       "  /markets  http 200 · 11 chunk(s) · JS 10 KB · CSS 3 KB · money layer present\n")
        got = bundle_findings(read(art), web, art)
        staleness = any("describes different sources" in g for g in got) and any("300.0" in g for g in got)
        # ...and the policy half: a measured platform bridge with no named exclusion is a finding, not a footnote.
        art.write_text(
            "budget for the initial route: 200 KB of first-party JS\n"
            "  /  http 200 \u00b7 9 chunk(s) \u00b7 JS 10.0 KB \u00b7 CSS 3 KB \u00b7 money layer absent"
            " \u00b7 platform bridge 18.0 KB (not budgeted)\n"
            "  /markets  http 200 \u00b7 10 chunk(s) \u00b7 JS 10 KB \u00b7 CSS 3 KB \u00b7 money layer present\n"
            "route-level splitting: proven\nstatus: pass\n")
        got2 = bundle_findings(read(art), web, art)
        policy = any("does not name what was excluded" in g for g in got2)
        return staleness and policy, got + got2

    @canary
    def c7_assert_env():
        planted = WEB / "src" / "__p08_canary_secret.ts"
        try:
            planted.write_text('export const leak = process.env.PGM_ADMIN_TOKEN ?? "";\n')
            rc, out = sh(["node", "scripts/assert-env.mjs"], cwd=WEB, timeout=180)
            return rc != 0 and "PGM_ADMIN_TOKEN" in out, out.strip().splitlines()[-1][:160] if out.strip() else "no output"
        finally:
            planted.unlink(missing_ok=True)

    @canary
    def c7_ci_scan():
        planted = WEB / ".next" / "__p08_canary.js"
        if not (WEB / ".next").exists():
            return True, "web/.next absent — the canary for this runs in CI, where the build output exists"
        try:
            # shapes `--built` is defined to catch (a 32-byte hex key, a URI with a password), planted inside
            # the tree so the walk has to find it there rather than being told which file to open
            planted.write_text("var cfg = { key: \"0x" + "ab" * 32 + "\",\n"
                               '  dsn: "postgres://polygm:hunter2@db:5432/polygm" }\n')
            rc, out = sh([PY, str(ROOT / "tools/ci-log-scan.py"), "--built", str(WEB / ".next")], timeout=600)
            return rc != 0, out.strip().splitlines()[-1][:160] if out.strip() else "rc=%d" % rc
        finally:
            planted.unlink(missing_ok=True)

    for fn in cases:
        name = fn.__name__
        try:
            ok, detail = fn()
        except Exception as e:
            import traceback
            ok, detail = False, "raised %s: %s | %s" % (type(e).__name__, str(e)[:140],
                                                         traceback.format_exc(limit=3).strip().splitlines()[-1][:100])
        passes += 1 if ok else 0
        print("  %-4s %-16s %s" % ("FIRED" if ok else "MISSED", name, str(detail)[:190]))
    print("\nP08 self-test: %d/%d canaries fired on planted violations" % (passes, len(cases)))
    return 0 if passes == len(cases) else 1


# -------------------------------------------------------------------------------------------------- main
CHECKS = [c1_the_route_ledger_and_the_contract_and_the_launch_list_agree,
          c2_the_api_types_are_generated_and_nothing_is_hand_typed,
          c3_every_control_in_the_doc_resolves_to_an_owner_and_a_real_test,
          c4_money_is_one_path_and_the_build_knows_it,
          c5_the_design_tokens_are_the_only_source_of_dimensions,
          c6_the_theme_layer_is_generated_current_and_parseable,
          c7_no_secret_can_reach_the_bundle_and_the_frame_is_csp_per_surface,
          c8_the_payload_budget_is_measured_not_asserted,
          c9_the_dictionary_is_the_only_source_of_ui_copy,
          c10_no_price_without_its_freshness_and_no_paywall_in_a_trade,
          c11_the_proved_by_a_served_response,
          c12_an_unbuilt_capability_is_visible_on_screen,
          c13_the_frame_itsself_holds_its_budgets,
          c14_every_route_has_a_boundary_and_every_widget_has_one_of_its_own,
          c15_the_test_suite_is_green_and_big_enough_to_mean_it,
          c16_the_client_and_the_contract_agree_on_the_idempotency_key]


def context() -> dict:
    return {"doc": read(DOC), "items": launch_items(read(DOC)), "routes": parse_ledger(WEB),
            "ops": parse_contract(), "notes": parse_notes(WEB)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--fast", action="store_true", help="skip the check that boots uvicorn + next start")
    ap.add_argument("--only", default="", help="run only the checks whose name contains this")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--record", default="", help="write the run to this file as well as stdout")
    ap.add_argument("--self-test", action="store_true", help="plant every violation and prove the scans fire")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.list:
        for fn in CHECKS:
            print("  %-64s %s" % (fn.__name__, (fn.__doc__ or "").strip().split("\n")[0][:76]))
        print("%d checks; c11 boots two servers, the rest read the tree or run a real subprocess" % len(CHECKS))
        return 0
    checks = [c for c in CHECKS if not a.only or a.only in c.__name__]
    if a.fast:
        checks = [c for c in checks if "c11" not in c.__name__]
    ctx = context()
    lines, results = [], []
    for fn in checks:
        t0 = time.perf_counter()
        try:
            label, ok, detail = fn(ctx)
        except Exception as e:
            import traceback
            label, ok = fn.__name__, False
            detail = "raised %s: %s | %s" % (type(e).__name__, str(e)[:150],
                                             traceback.format_exc(limit=3).strip().splitlines()[-1][:110])
        ms = int((time.perf_counter() - t0) * 1000)
        results.append((label, ok, detail, ms))
        line = "  %-4s %5d ms  %s\n        %s" % ("PASS" if ok else "FAIL", ms, label, detail)
        print(line)
        lines.append(line)
    passed = sum(1 for _l, ok, _d, _m in results if ok)
    summary = "\nP08 gate: %d/%d checks passed in %d ms%s" % (
        passed, len(results), sum(m for *_x, m in results), " (--fast: c11 not run)" if a.fast else "")
    if passed != len(results):
        summary += "\nthe gate is a floor: a FAIL here means P08 is not done, whatever the document says"
    print(summary)
    if a.json:
        print(json.dumps({"phase": "P08", "passed": passed, "total": len(results),
                          "checks": [{"label": l, "ok": ok, "detail": d, "ms": m} for l, ok, d, m in results]},
                         indent=2))
    if a.record:
        out = Path(a.record)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("P08 gate run, %s (--fast=%s)\n\n%s\n%s\n"
                       % (time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()), a.fast, "\n".join(lines), summary.strip()))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
