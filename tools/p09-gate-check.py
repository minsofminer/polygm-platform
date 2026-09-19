#!/usr/bin/env python3
"""P09 Quality Gate — the markets surfaces. Same rule as every other phase's gate in this repo: a check either
EXECUTES (a subprocess, a parser over the real tree, a real test run) or it does not go in the list.

The phase's own acceptance line is `npm run test && npm run build`. That line is deliberately *not* the gate.
The build proves the TypeScript agrees with itself; the suite proves the pure functions do what their names say.
Neither can see the seven things this phase is actually promising: that every price on screen went through the
number layer with a freshness attached, that no ladder number is a float, that attacker-written resolution text
reaches the DOM as text, that the doc's constants are the code's constants, that a book with one side is
rendered as a state rather than a failure, and that the measured payload describes *this* tree.

Three properties every check is held to, from the standing rules:

  1. **no control without an owner and a test** — c2 parses `docs/P09-frontend-markets.md` and resolves each
     `[owner: … · test: …]` marker against a real file, a real symbol, or a real check id in this script;
  2. **a control that cannot fail is a costume** — `--self-test` plants each violation the scans look for and
     fails if a scan walks past it;
  3. **a recorded number is a stale number** — c7 refuses a bundle measurement older than the newest P09 source
     file, because a payload number that describes a build nobody has is a number nobody should read.

Two scanners are *reused* from `tools/p08-gate-check.py` rather than reimplemented (money path, freshness on
prices), because two scanners for one rule is how the two of them end up disagreeing. c3 and c4 therefore run
the P08 gate's own functions over the P09 files: a change to the rule changes both gates at once.

`--fast` drops c7, which runs the whole web suite twice over (its own run plus the money-path test run in c3) and
reads the build artefact.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DOC = ROOT / "docs" / "P09-frontend-markets.md"
CONTRACT = ROOT / "contracts" / "openapi.yaml"
TOKENS = ROOT / "brand" / "tokens.css"
BUNDLE = ROOT / "docs" / "verification" / "P08-bundle.txt"
PY = sys.executable
TMP = ROOT / ".tmp"

# The files this phase owns. c3/c4/c5 scan exactly these, plus the two libs they depend on; a check that re-scans
# the whole tree would just be the P08 gate with a different name, and `make p08` already runs it.
P09_FILES = ("src/lib/depth.ts", "src/lib/ladders.ts", "src/lib/upstream.ts", "src/lib/anchor.ts",
             "src/screens/OrderBook.tsx", "src/screens/PriceChart.tsx", "src/screens/EventTable.tsx",
             "src/screens/MarketCard.tsx", "src/screens/MarketRail.tsx", "src/screens/MarketsClient.tsx",
             "src/screens/MarketView.tsx", "src/screens/EventView.tsx",
             "app/markets/page.tsx", "app/market/[market_id]/page.tsx", "app/event/[event_id]/page.tsx")


def sh(argv, cwd: Path = ROOT, timeout: int = 900) -> tuple[int, str]:
    e = dict(os.environ)
    e["TMPDIR"] = str(TMP)
    e["CI"] = "1"
    TMP.mkdir(exist_ok=True)
    try:
        p = subprocess.run(argv, cwd=str(cwd), env=e, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "(timed out after %ds)" % timeout
    except FileNotFoundError as f:
        return 127, "cannot run %s: %s" % (argv[0], f)
    return p.returncode, p.stdout + p.stderr


def read(path: Path) -> str:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return ""


def p08():
    """The P08 gate as a module. Its scanners are the authority for two of the rules this phase also has to
    keep (money path, freshness), so P09 calls them instead of writing a second opinion."""
    spec = importlib.util.spec_from_file_location("pgm_p08_gate", ROOT / "tools" / "p08-gate-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------------------------- c1  surfaces wired
def surface_findings(root: Path = WEB) -> list:
    """Every P09 surface exists, every route imports it, and the route ledger + contract agree about the three
    endpoints this phase added. A screen that exists but is imported by nothing is a file, not a surface."""
    f = []
    for rel in P09_FILES:
        if not (root / rel).exists():
            f.append("%s does not exist" % rel)
    wiring = {
        "app/markets/page.tsx": "MarketsClient",
        "app/market/[market_id]/page.tsx": "MarketView",
        "app/event/[event_id]/page.tsx": "EventView",
    }
    for page, screen in wiring.items():
        body = read(root / page)
        if body and ("@/screens/%s" % screen) not in body:
            f.append("%s does not import @/screens/%s — the route and the surface are not wired" % (page, screen))
    composed = {
        "src/screens/MarketView.tsx": ("OrderBook", "PriceChart", "MarketRail"),
        "src/screens/EventView.tsx": ("EventTable", "OrderBook"),
        "src/screens/MarketsClient.tsx": ("MarketCard",),
    }
    for rel, parts in composed.items():
        body = read(root / rel)
        for part in parts:
            if body and ("./%s" % part) not in body and ("%s" % part) not in body:
                f.append("%s does not compose %s" % (rel, part))
    return f


def ledger_entry(routes: str, key: str) -> str:
    r"""The text of one ledger entry, brace-balanced.

    A regex `\{([^}]*)\}` was the first version and it truncated at the `}` inside a path template
    (`/v1/markets/{market_id}/history`), so an entry that WAS built and owned read as neither. The path
    parameter is the reason this needs a real reader."""
    i = routes.find("\n  %s: {" % key)
    if i < 0:
        i = routes.find("%s: {" % key)
    if i < 0:
        return ""
    j = routes.index("{", i)
    depth, k = 0, j
    while k < len(routes):
        if routes[k] == "{":
            depth += 1
        elif routes[k] == "}":
            depth -= 1
            if depth == 0:
                return routes[j:k + 1]
        k += 1
    return ""


def ledger_findings(web: Path = WEB, contract: Path = CONTRACT) -> list:
    """The three routes P09 added are in the ledger as built, owned by P09, and documented in the contract. The
    ledger is what the shell reads to decide whether to offer a surface, so a missing row is a hidden feature and
    a wrong row is a lie (§2.7's state table is what renders when the row says `built: false`)."""
    f = []
    routes = read(web / "src/api/routes.ts")
    spec = read(contract)
    for key, path in (("history", "/v1/markets/{market_id}/history"),
                      ("holders", "/v1/markets/{market_id}/holders"),
                      ("event", "/v1/events/{event_id}")):
        body = ledger_entry(routes, key)
        if not body:
            f.append("the route ledger has no `%s` entry" % key)
            continue
        if "built: true" not in body:
            f.append("`%s` is not built:true — the surface is unreachable by the shell's own rule" % key)
        if '"P09"' not in body:
            f.append("`%s` has no P09 owner" % key)
        if ('"%s"' % path) not in spec and ("%s:" % path) not in spec:
            f.append("%s is not in contracts/openapi.yaml" % path)
    return f


def c1_every_surface_the_doc_names_exists_and_is_wired(ctx) -> tuple:
    """The screens, their routes, and the ledger/contract for the three endpoints they added."""
    f = surface_findings() + ledger_findings()
    return ("c1", not f, "15 files, 3 routes wired, 3 ledger rows owned by P09%s"
            % ("" if not f else " | " + " | ".join(f[:6])))


# ------------------------------------------------------------------------------- c2  owners and tests
def doc_markers(text: str) -> list:
    """`[owner: X · test: Y]` markers with their line numbers — the same shape `tools/p08-gate-check.py` parses,
    so a control written for one phase's doc is readable by the other's review."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in re.finditer(r"\[owner:\s*([^\s·\]]+)\s*·\s*test:\s*([^\]]+?)\]", line):
            out.append({"line": n, "owner": m.group(1).strip(), "test": m.group(2).strip()})
    return out


OWNERS = {"frontend-owner", "design-owner", "backend-owner", "product-owner", "security-owner", "ops-ani"}


def resolve_marker(part: str, problems: list, line: int, root: Path = ROOT) -> None:
    """One `path[::symbol]` fragment. A symbol that does not exist is the exact costume this rule exists to
    catch, so a missing symbol is a finding — not a warning."""
    part = part.strip()
    if part.startswith("docs-only"):
        if not re.search(r"launch item \d+", part):
            problems.append("line %d: a control tested by a document must name a launch item" % line)
        return
    path_part, _, symbol = part.partition("::")
    path_part, symbol = path_part.strip(), symbol.strip()
    if re.fullmatch(r"c\d+[a-z0-9_]*", path_part):
        path_part = "tools/p09-gate-check.py::" + re.match(r"c\d+", path_part).group(0)
        symbol = ""
    f = root / path_part
    if not f.exists() and (root / "web" / path_part).exists():
        f = root / "web" / path_part
    if not f.exists():
        problems.append("line %d: test target %s is not a file in this repo" % (line, path_part))
        return
    if not symbol:
        return
    body = read(f)
    if f.name == Path(__file__).name:
        if not re.search(r"\bdef %s_" % re.escape(symbol), body):
            problems.append("line %d: this gate has no check %s_ in it" % (line, symbol))
    elif f.suffix == ".py":
        if not re.search(r"\bdef %s" % re.escape(symbol), body):
            problems.append("line %d: %s has no function %s" % (line, path_part, symbol))
    elif symbol not in body:
        problems.append("line %d: %s does not contain %r, so the test named in the marker does not exist"
                        % (line, path_part, symbol))


def launch_items(doc: str) -> dict:
    """The numbered list under §4, as {number: text}. The pairing rule reads these and not the whole document:
    an item is a promise with an owner, a stray mention is not."""
    section = re.split(r"^## 4\.", doc, flags=re.M)
    body = section[1] if len(section) > 1 else ""
    body = re.split(r"^## 5\.", body, flags=re.M)[0]
    out = {}
    for m in re.finditer(r"^(\d+)\.\s+(.*?)(?=^\d+\.\s|\Z)", body, flags=re.M | re.S):
        out[int(m.group(1))] = m.group(2).strip()
    return out


def marker_findings(doc: str, root: Path = ROOT) -> list:
    problems = []
    markers = doc_markers(doc)
    if not markers:
        return ["docs/P09-frontend-markets.md has no [owner: … · test: …] markers at all"]
    for mk in markers:
        if "…" in mk["owner"] or "…" in mk["test"]:
            continue            # the line showing the marker format is not itself a control claim
        if mk["owner"] not in OWNERS:
            problems.append("line %d: owner %r is not one the launch review can assign" % (mk["line"], mk["owner"]))
        for part in (s.strip() for s in mk["test"].split("+")):
            resolve_marker(part, problems, mk["line"], root)
    docs_only = sum(1 for mk in markers if mk["test"].startswith("docs-only"))
    if docs_only > 2:
        problems.append("%d of %d controls are tested by a document; that is a costume, not a review"
                        % (docs_only, len(markers)))
    # Every [UNVERIFIED] slug must be answered by a numbered launch item. The check is against the ITEMS, not
    # against the document: a slug is of course "in the document" — it is in the marker being checked — so the
    # first version of this line passed for any slug at all, which is the failure mode the standing rule names.
    items = launch_items(doc)
    for m in re.finditer(r"\[UNVERIFIED — confirm before launch:\s*([a-z0-9-]+)\]", doc):
        slug = m.group(1)
        if not any(slug in text for text in items.values()):
            problems.append("`[UNVERIFIED — …: %s]` has no numbered launch item" % slug)
    # each control section carries at least one marker, or the section is prose
    for section in re.split(r"^### ", doc, flags=re.M)[1:]:
        title = section.splitlines()[0].strip()
        if not title.startswith("2.") or title.startswith("2.8"):
            continue
        if "[owner:" not in section:
            problems.append("section %s declares controls with no owner/test marker" % title)
    # §2.7 is the state table; every screen row must be a screen this phase ships
    table = re.search(r"^### 2\.7(.*?)^### ", doc, flags=re.M | re.S)
    if not table:
        problems.append("the document has no §2.7 state table")
    else:
        for screen in ("/markets", "/market/[id]", "/event/[id]", "ladder", "chart", "ticket"):
            if screen not in table.group(1):
                problems.append("the state table in §2.7 does not cover `%s`" % screen)
    return problems


def c2_every_control_has_an_owner_and_a_real_test(ctx) -> tuple:
    """The standing rule, machine-resolved over this phase's own document: an assignable owner and a test that
    exists — a file, a symbol inside it, or a check id in this script."""
    f = marker_findings(read(DOC))
    slugs = len(re.findall(r"\[UNVERIFIED — confirm before launch:", read(DOC)))
    return ("c2", not f, "%d markers, %d launch items, %d [UNVERIFIED] slugs paired%s"
            % (len(doc_markers(read(DOC))), len(launch_items(read(DOC))), slugs,
               "" if not f else " | " + " | ".join(f[:6])))


# ------------------------------------------------------------------------------------ c3  integer ladder
def ladder_findings(root: Path = WEB) -> list:
    """The P09 files parse money nowhere, and the three conversion helpers refuse what they cannot do.

    The refusals are the point. `microOf("1e-3")`, `priceUnitsOf("0.425", "0.01")` and `shareUnitsOf("1e-3")` must
    all throw: a helper that quietly rounds is how a 1000x error renders as a plausible price."""
    f = []
    gate = p08()
    for finding in gate.money_findings(root):
        rel = finding.split(":")[0]
        if rel in P09_FILES or rel.startswith("src/lib/"):
            f.append(finding)
    for rel in ("src/lib/depth.ts", "src/lib/anchor.ts"):
        # Comments are stripped first: these files' own doc blocks quote `parseFloat(row.price) * ...` to explain
        # why it is banned, and a scanner that reads prose as code reports its own doc as the product's bug.
        body = gate.strip_comments(read(root / rel))
        for m in re.finditer(r"\b(?:parseFloat|parseInt)\s*\(", body):
            f.append("%s: %r — a float parse in the ladder's own arithmetic" % (rel, m.group(0)))
    return f


def helper_contract_findings(root: Path = ROOT) -> list:
    """Run the helpers' refusals as a real subprocess. A test asserts them too; this asserts that the test suite
    is not the only thing standing between a rounding bug and a rendered price."""
    script = (
        "import { describe, expect, it } from \"vitest\";\n"
        "import { microOf, priceUnitsOf, shareUnitsOf } from \"@/lib/depth\";\n"
        "describe(\"p09 gate probe: the unit helpers refuse what they cannot do\", () => {\n"
        "  it(\"throws on an off-scale or off-tick input instead of rounding\", () => {\n"
        "    expect(() => microOf(\"1e-3\")).toThrow();\n"
        "    expect(() => priceUnitsOf(\"0.425\", \"0.01\")).toThrow();\n"
        "    expect(() => shareUnitsOf(\"1e-3\")).toThrow();\n"
        "    expect(priceUnitsOf(\"0.001\", \"0.001\")).toBe(1);\n"
        "    expect(shareUnitsOf(\"232978723.404255\")).toBe(232978723);\n"
        "  });\n"
        "});\n"
    )
    probe = root / "web" / "src" / "lib" / "__p09-gate-probe.test.ts"
    probe.write_text(script)
    try:
        rc, out = sh(["npx", "vitest", "run", "src/lib/__p09-gate-probe.test.ts"], cwd=root / "web", timeout=600)
    finally:
        probe.unlink(missing_ok=True)
    clean = re.sub(r"\x1b\[[0-9;]*m", "", out)
    if rc == 0 and re.search(r"Tests\s+1 passed", clean):
        return []
    return ["the helper-contract probe did not pass (%d): %s"
            % (rc, clean.strip().splitlines()[-1][:150] if clean.strip() else "no output")]


def c3_the_ladder_arithmetic_is_integer_only(ctx) -> tuple:
    """No float in the ladder's own path, and the three unit conversions refuse what they cannot do."""
    f = ladder_findings() + helper_contract_findings()
    rc, out = sh(["npx", "vitest", "run", "src/lib"], cwd=WEB, timeout=900)
    m = re.search(r"Tests\s+(\d+) passed", re.sub(r"\x1b\[[0-9;]*m", "", out))
    if rc != 0 or not m:
        f.append("`vitest run src/lib` is not green: " + (out.strip().splitlines()[-1][:140] if out.strip() else "no output"))
    elif int(m.group(1)) < 40:
        f.append("only %s lib tests ran; the ladder's rules need more than that to be covered" % m.group(1))
    return ("c3", not f, "integer arithmetic only in %d files; refusals fire; %s lib tests green%s"
            % (len(P09_FILES), m.group(1) if m else "?", "" if not f else " | " + " | ".join(f[:5])))


# ------------------------------------------------------------------------------ c4  freshness on prices
def c4_no_price_without_its_freshness(ctx) -> tuple:
    """The P08 gate's own freshness scan, over this phase's screens. A `kind="price"` with no freshness is a
    number that cannot say whether it is old, and a ladder is the surface where that matters most."""
    gate = p08()
    f = []
    for finding in gate.price_findings(WEB):
        rel = finding.split(":")[0]
        if rel in P09_FILES:
            f.append(finding)
    ladder = read(WEB / "src/screens/OrderBook.tsx")
    for need, why in (("pgm-book-stale", "the ladder has no stale overlay: a dimmed number is not a state a "
                                        "reader can decode"),
                      ("freshness !== \"live\"", "the stale overlay must be driven by the freshness value, not "
                                                 "by a flag"),
                      ("staleMs={staleMs}", "the ladder's price cells do not carry the staleness window")):
        if need not in ladder:
            f.append("src/screens/OrderBook.tsx: %s (%s)" % (why, need))
    return ("c4", not f, "every price cell in %d phase files carries freshness and a window; the ladder's stale "
            "overlay exists%s" % (len(P09_FILES), "" if not f else " | " + " | ".join(f[:5])))


# ------------------------------------------------------------------------------- c5  attacker-written text
def upstream_findings(root: Path = WEB) -> list:
    """Resolution text is the one field on the page an outsider writes. It must reach the DOM as sanitised text:
    no `dangerouslySetInnerHTML` in a screen, the criteria through `sanitiseUpstreamText`, the source through
    `safeSourceUrl`, and the sanitiser idempotent (which is what makes a double pass safe)."""
    f = []
    for rel in P09_FILES:
        body = p08().strip_comments(read(root / rel))
        if rel.endswith(".tsx") and "dangerouslySetInnerHTML" in body:
            f.append("%s renders HTML from a string — resolution text is text, not markup" % rel)
    rail = read(root / "src/screens/MarketRail.tsx")
    for need in ("sanitiseUpstreamText", "safeSourceUrl"):
        if need not in rail:
            f.append("src/screens/MarketRail.tsx does not call %s" % need)
    if "resolutionCriteria" in rail and "sanitiseUpstreamText(market.resolutionCriteria" not in rail:
        f.append("the rail renders resolutionCriteria without sanitising it")
    if "{criteria}" not in rail:
        f.append("the rail does not render the sanitised criteria as a text node")
    # Idempotence is asserted where assertions live: the test. A doc block claiming it is prose.
    test = read(root / "src/lib/upstream.test.ts")
    if "idempot" not in test.lower() or "sanitiseUpstreamText(" not in test:
        f.append("web/src/lib/upstream.test.ts does not assert idempotence — strip/decode/strip is only safe "
                 "because a second pass is a no-op, and that is a test, not a comment")
    return f


def c5_upstream_text_is_sanitised_and_documented_as_such(ctx) -> tuple:
    """Sanitised plain text for the criteria, an http(s)-only link for the source, and the tests that prove it."""
    f = upstream_findings()
    rc, out = sh(["npx", "vitest", "run", "src/lib/upstream.test.ts"], cwd=WEB, timeout=600)
    clean = re.sub(r"\x1b\[[0-9;]*m", "", out)
    m = re.search(r"Tests\s+(\d+) passed", clean)
    if rc != 0 or not m:
        f.append("the upstream tests are not green: " + (clean.strip().splitlines()[-1][:140] if clean.strip() else "no output"))
    return ("c5", not f, "no dangerouslySetInnerHTML in %d files; criteria sanitised and rendered as text; %s "
            "upstream tests green%s" % (len(P09_FILES), m.group(1) if m else "?", "" if not f else " | " + " | ".join(f[:5])))


# ---------------------------------------------------------------------------- c6  the doc's own numbers
CONSTANTS = (
    # (file, pattern, what the document must also say)
    ("src/screens/MarketCard.tsx", r"SUMMARY_AT\s*=\s*(\d+)", "SUMMARY_AT = {v}"),
    ("src/screens/EventTable.tsx", r"MAX_ROWS\s*=\s*(\d+)", "MAX_ROWS = {v}"),
    ("src/screens/PriceChart.tsx", r"SHORT_LIFE_MS\s*=\s*([0-9_]+)", "SHORT_LIFE_MS = {v}"),
    ("src/screens/MarketView.tsx", r"POLL_MS\s*=\s*([0-9_]+)", "POLL_MS = {v}"),
)


def constant_findings(doc: str, root: Path = WEB) -> list:
    """A number in the document that is also a number in the code has two owners the moment they disagree. The
    doc names each of these constants *by name and value*, so a change to one is a change the document has to
    make too — otherwise §2 is describing a build nobody is running."""
    problems = []
    for rel, rx, claim in CONSTANTS:
        body = read(root / rel)
        m = re.search(rx, body)
        if not m:
            problems.append("%s no longer defines %s" % (rel, rx))
            continue
        value = m.group(1)
        variants = {value, value.replace("_", "")}
        if not any(v in doc for v in variants):
            problems.append("the document never states %s (code says %r)" % (claim.format(v=value), value))
    return problems


def c6_the_documents_constants_are_the_codes_constants(ctx) -> tuple:
    """The constants this phase's §2 claims — the summarisation threshold, the row cap, the short-life window,
    the poll — are read out of the code and looked for in the document, and the two layout tokens the ladder and
    shell depend on are declared where the design system can find them."""
    doc = read(DOC)
    f = constant_findings(doc)
    tokens = read(TOKENS)
    for name, need in (("--pgm-book-max-block", "the ladder is bounded only if this token exists"),
                       ("--pgm-rail-left", "the shell's left rail had no declaration until this phase"),
                       ("--pgm-rail-right", "the shell's right rail had no declaration until this phase")):
        if name not in tokens:
            f.append("brand/tokens.css does not declare %s — %s" % (name, need))
        elif name not in doc:
            f.append("the document does not mention %s, so a reader cannot tell where the width came from" % name)
    steps = read(WEB / "src/screens/OrderBook.tsx")
    for step, why in (("10_000", "1c"), ("50_000", "5c")):
        if step not in steps:
            f.append("the ladder's %s bucket (%s micro) is missing from the aggregate control" % (why, step))
    return ("c6", not f, "%d constants match the document, 3 layout tokens declared and named%s"
            % (len(CONSTANTS), "" if not f else " | " + " | ".join(f[:5])))


# ------------------------------------------------------------------- c7  the tree is the thing measured
def newest_source_ms(root: Path = WEB) -> int:
    """The newest mtime among the files this phase owns — the reference the bundle measurement is compared to."""
    newest = 0.0
    for rel in P09_FILES:
        p = root / rel
        if p.exists():
            newest = max(newest, p.stat().st_mtime)
    lib = root / "src" / "lib"
    if lib.exists():
        for p in lib.glob("*.ts"):
            newest = max(newest, p.stat().st_mtime)
    return int(newest * 1000)


def c7_the_suite_is_green_and_the_measurement_describes_this_tree(ctx) -> tuple:
    """The phase's acceptance line, run whole: the full web suite, the type checker, and the P08 bundle artefact
    that the payload claim rests on. A measurement older than the newest source file describes a build that no
    longer exists, which is how `make p08`'s c8 got its own rule in the first place."""
    f = []
    rc_tsc, out_tsc = sh(["npx", "tsc", "--noEmit"], cwd=WEB, timeout=900)
    if rc_tsc != 0:
        f.append("tsc: " + (out_tsc.strip().splitlines()[0][:140] if out_tsc.strip() else "rc=%d" % rc_tsc))
    rc, out = sh(["npm", "run", "--silent", "test"], cwd=WEB, timeout=1200)
    clean = re.sub(r"\x1b\[[0-9;]*m", "", out)
    m = re.search(r"Tests\s+(\d+) passed", clean)
    files = re.search(r"Test Files\s+(\d+) passed", clean)
    if rc != 0 or not m:
        f.append("npm run test is not green: " + (clean.strip().splitlines()[-1][:140] if clean.strip() else "no output"))
    elif int(m.group(1)) < 140:
        f.append("only %s tests ran; P08 ended at 145 and a shrinking suite is a shrinking claim" % m.group(1))
    if files and int(files.group(1)) < 20:
        f.append("only %s test files: the P09 lib and screen tests are not being collected" % files.group(1))
    artefact = read(BUNDLE)
    if not artefact:
        f.append("docs/verification/P08-bundle.txt is missing — run `make p08`")
    else:
        age_note = "the measurement is older than the newest source file" if BUNDLE.stat().st_mtime * 1000 < newest_source_ms() else ""
        if age_note:
            f.append(age_note)
        if "status: pass" not in artefact:
            f.append("the bundle artefact does not say `status: pass`")
    return ("c7", not f, "%s tests in %s files, tsc clean, bundle artefact current%s"
            % (m.group(1) if m else "no", files.group(1) if files else "no", "" if not f else " | " + " | ".join(f[:4])))


CHECKS = [c1_every_surface_the_doc_names_exists_and_is_wired,
          c2_every_control_has_an_owner_and_a_real_test,
          c3_the_ladder_arithmetic_is_integer_only,
          c4_no_price_without_its_freshness,
          c5_upstream_text_is_sanitised_and_documented_as_such,
          c6_the_documents_constants_are_the_codes_constants,
          c7_the_suite_is_green_and_the_measurement_describes_this_tree]


# ------------------------------------------------------------------------------------------- --self-test
def fixture(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def self_test() -> int:
    """Plant each violation the scans claim to catch and fail if a scan walks past it."""
    TMP.mkdir(exist_ok=True)
    cases, passes = [], 0

    def canary(fn):
        cases.append(fn)
        return fn

    @canary
    def c1_surfaces():
        d = TMP / "p09-self-c1"
        root = d / "web"
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        fixture(root, "app/markets/page.tsx", "export default () => null;\n")
        got = surface_findings(root)
        got += ledger_findings(root, TMP / "p09-self-c1" / "openapi.yaml")
        return any("does not import" in g for g in got) and any("does not exist" in g for g in got), got

    @canary
    def c2_markers():
        doc = ("### 2.1 D1\n- a control [owner: nobody · test: web/src/lib/nope.ts::gone]\n"
               "[UNVERIFIED — confirm before launch: never-mentioned]\n"
               "## 4. Launch checklist\n1. **`something-else` — unrelated.**\n## 5. Measured\n")
        got = marker_findings(doc)
        return (any("nobody" in g for g in got) and any("not a file" in g for g in got)
                and any("numbered launch item" in g for g in got)), got

    @canary
    def c3_ladder():
        d = TMP / "p09-self-c3"
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        fixture(d, "src/lib/depth.ts", "export const bad = parseFloat('0.1');\n")
        fixture(d, "src/screens/OrderBook.tsx", "export const x = v.toFixed(3);\n")
        got = ladder_findings(d)
        return len(got) >= 2, got

    @canary
    def c4_freshness():
        d = TMP / "p09-self-c4"
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        fixture(d, "src/screens/OrderBook.test.tsx", "// fixture files are skipped by the P08 scan\n")
        fixture(d, "src/screens/OrderBook.tsx", "export const Book = () => <NumberView kind=\"price\" value={1} />;\n")
        gate = p08()
        got = [g for g in gate.price_findings(d) if g.split(":")[0] in P09_FILES]
        got += ["no stale overlay"] if "pgm-book-stale" not in read(d / "src/screens/OrderBook.tsx") else []
        return len(got) >= 2, got

    @canary
    def c5_upstream():
        d = TMP / "p09-self-c5"
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        fixture(d, "src/screens/MarketRail.tsx", "export const Rail = () => <p dangerouslySetInnerHTML={{__html: market.resolutionCriteria}} />;\n")
        fixture(d, "src/lib/upstream.ts", "export const sanitiseUpstreamText = (s: string) => s;\n")
        got = upstream_findings(d)
        return len(got) >= 3, got

    @canary
    def c6_constants():
        d = TMP / "p09-self-c6"
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        for rel, body in (("src/screens/MarketCard.tsx", "const SUMMARY_AT = 99;\n"),
                          ("src/screens/EventTable.tsx", "const MAX_ROWS = 7;\n"),
                          ("src/screens/PriceChart.tsx", "const SHORT_LIFE_MS = 1;\n"),
                          ("src/screens/MarketView.tsx", "const POLL_MS = 5;\n")):
            fixture(d, rel, body)
        got = constant_findings("a document that states none of them", d)
        return len(got) == 4, got

    @canary
    def c7_staleness():
        """c7's bundle rule is the P08 gate's rule; prove the comparison is direction-sensitive by construction:
        a source file touched now must be newer than an artefact written an hour ago."""
        d = TMP / "p09-self-c7"
        import shutil, os as _os
        shutil.rmtree(d, ignore_errors=True)
        fixture(d, "src/lib/depth.ts", "export const x = 1;\n")
        art = d / "P08-bundle.txt"
        art.write_text("status: pass\n")
        old = time.time() - 3600
        _os.utime(art, (old, old))
        stale = art.stat().st_mtime * 1000 < newest_source_ms(d)
        fixture(d, "src/lib/ladders.ts", "export const y = 2;\n")
        fresh = art.stat().st_mtime * 1000 < newest_source_ms(d)
        return stale and fresh, ["stale=%s fresh=%s" % (stale, fresh)]

    for fn in cases:
        try:
            ok, got = fn()
        except Exception as e:                     # a canary that raises is a canary that did not fire
            ok, got = False, ["raised %s: %s" % (type(e).__name__, e)]
        print("  %s  %s%s" % ("fired" if ok else "MISS ", fn.__name__, "" if ok else "  <- " + str(got)[:160]))
        passes += 1 if ok else 0
    print("p09 gate self-test: %d/%d canaries fired" % (passes, len(cases)))
    return 0 if passes == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--fast", action="store_true", help="skip c7 (the full suite + the bundle artefact)")
    ap.add_argument("--only", default="", help="run only the checks whose name contains this")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--record", default="", help="write the run to this file as well as stdout")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.list:
        for fn in CHECKS:
            print("  %-64s %s" % (fn.__name__, (fn.__doc__ or "").strip().split("\n")[0][:76]))
        print("%d checks; c7 runs the whole web suite and reads the recorded build measurement" % len(CHECKS))
        return 0
    checks = [c for c in CHECKS if not a.only or a.only in c.__name__]
    if a.fast:
        checks = [c for c in checks if "c7" not in c.__name__]
    ctx = {}
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
    summary = "\nP09 gate: %d/%d checks passed in %d ms%s" % (
        passed, len(results), sum(m for *_x, m in results), " (--fast: c7 not run)" if a.fast else "")
    if passed != len(results):
        summary += "\nthe gate is a floor: a FAIL here means P09 is not done, whatever the document says"
    print(summary)
    if a.json:
        print(json.dumps({"phase": "P09", "passed": passed, "total": len(results),
                          "checks": [{"label": l, "ok": ok, "detail": d, "ms": m} for l, ok, d, m in results]},
                         indent=2))
    if a.record:
        out = ROOT / a.record
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("# %s\n\n%s\n" % ("P09 gate — recorded by tools/p09-gate-check.py", chr(10).join(lines).replace("  PASS", "PASS  ").replace("  FAIL", "FAIL  ")))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
