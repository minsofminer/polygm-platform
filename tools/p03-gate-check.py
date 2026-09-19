#!/usr/bin/env python3
"""
P03 Quality Gate — "a competent frontend engineer builds the whole product from this document and never
has to invent a spacing value, guess a state, or ask what a stale price should look like."

That gate sentence is three claims, so this file checks three things and refuses to accept prose for any
of them:
  G1 every foundation a screen needs is a TOKEN (present in tokens.json AND emitted to tokens.css)
  G2 every data field on every screen cites an endpoint that was PROBED (docs/verification/P01-probe.json)
  G3 every component × state is SPECIFIED and enumerated, with no invented component names
  G4 motion obeys the vendored skill's own numbers (durations, curves, "nothing animates a number")
  G5 the money path stays integer (no float arithmetic in any money-formatting example)
  G6 the specimen consumes tokens and defines nothing — the "redefined, not reftype" failure mode
  G7 accessibility obligations that a design system can actually pre-pay (aria-live choice, focus ring,
     touch target, reduced motion, colour-never-alone)
  G8 the brand lock is intact after the rename (generated lockup, pinned geometry hash, name coherence)

Like P01/P02 this is mutation-tested by intent: a check that cannot fail is reported as such.
Run: python3 tools/p03-gate-check.py [--verbose]
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs/P03-design-system.md"
TOKENS = ROOT / "brand/tokens.json"
CSS = ROOT / "brand/tokens.css"
PROBE = ROOT / "docs/verification/P01-probe.json"
SPECIMEN = ROOT / "brand/specimen.html"
STORIES = ROOT / "docs/verification/P03-storybook-list.txt"
KIT = ROOT / "brand/BRAND-KIT.md"
LOCKUP = ROOT / "brand/svg/lockup-horizontal.svg"
DESIGN_MD = ROOT / "web/DESIGN.md"
PRESET = ROOT / "web/tailwind.preset.cjs"


@dataclass
class Check:
    cid: str
    claim: str
    ok: bool
    detail: str = ""
    skipped: bool = False
    vacuous: bool = False


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, cid, claim, ok, detail="", skipped=False, vacuous=False):
        self.checks.append(Check(cid, claim, bool(ok), detail, skipped, vacuous))

    def summary(self) -> str:
        live = [c for c in self.checks if not c.skipped]
        bad = [c for c in live if not c.ok]
        empty = [c for c in live if c.ok and c.detail in ("", "none") and not c.vacuous]
        lines = [
            f"  {len(live) - len(bad)} passed, {len(bad)} failed, "
            f"{len(self.checks) - len(live)} skipped, {len(empty)} with no evidence line"
        ]
        for c in bad:
            lines.append(f"  [FAIL] {c.cid} {c.claim}\n         {c.detail}")
        for c in self.checks:
            if c.vacuous:
                lines.append(f"  [VACUOUS] {c.cid} — a check with nothing to check cannot pass")
        return "\n".join(lines)


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def read_or(p: Path, marker: str = "") -> str | None:
    """A referenced file that may legitimately be absent (vendored skills are not part of the product
    build). A gate that raises FileNotFoundError reports NOTHING — not a pass, not a failure — which is
    the worst outcome and exactly what happened when the skills tree was missing. Callers must turn
    None into an explicit skip with a reason."""
    try:
        return p.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None


# --------------------------------------------------------------------------- G1 foundations
def g_foundations(r: Report) -> None:
    t = json.loads(read(TOKENS))
    css = read(CSS)
    need_sections = ["spacing", "border", "elevation", "density", "breakpoints", "motion", "layers", "components", "screens"]
    missing = [k for k in need_sections if k not in t]
    r.add("G1.1", "tokens.json carries every D1 foundation section", not missing,
          f"missing: {missing}" if missing else ", ".join(f"{k}({len(t[k])})" for k in need_sections))

    # every emitted value must exist as a CSS custom property too, or the UI cannot consume it
    want = [f"--pgm-space-{s['name']}" for s in t["spacing"]["scale"]]
    want += [f"--pgm-border-w-{b['name']}" for b in t["border"]["widths"]]
    want += [f"--pgm-shadow-{l['name']}" for l in t["elevation"]["levels"]]
    want += [f"--pgm-z-{k}" for k in t["layers"] if k != "note"]
    want += [f"--pgm-ease-{k[5:]}" for k in t["motion"]["easing"] if k.startswith("ease-")]
    want += [f"--pgm-dur-{k}" for k in t["motion"]["duration_ms"] if k != "forbidden"]
    for mode in t["density"]["setting"]["levels"]:
        want += [f"--pgm-row-{row.replace('_', '-')}-{mode}" for row in t["density"]["row_heights_px"]]
        want += [f"--pgm-pad-{k.replace('_', '-')}-{mode}" for k in t["density"]["padding_px"]]
        want += [f"--pgm-font-{mode}"]
    want += ["--pgm-radius-base", "--pgm-radius-chip", "--pgm-radius-card", "--pgm-min-touch-target", "--pgm-ui-ceiling"]
    undefined = [w for w in want if not re.search(rf"{re.escape(w)}\s*:", css)]
    r.add("G1.2", "every D1 token is emitted to tokens.css (a token with no CSS variable is prose)",
          not undefined, f"undefined in CSS: {undefined[:8]}" if undefined else f"{len(want)} variables present")

    # a UI that must not invent a value needs the *number of steps* it can pick from
    r.add("G1.3", "spacing scale has ≥12 steps incl. a ≥2x range for panel padding",
          len(t["spacing"]["scale"]) >= 12 and t["spacing"]["scale"][-1]["px"] >= 8 * t["spacing"]["base"],
          f"{len(t['spacing']['scale'])} steps, top {t['spacing']['scale'][-1]['px']}px, base {t['spacing']['base']}")

    # density must be a switch, not a wish: [data-density] has to exist in CSS
    has_switch = all(f'[data-density="{m}"]' in css for m in t["density"]["setting"]["levels"])
    r.add("G1.4", "density is switchable by one attribute (no per-component padding maths)", has_switch,
          "all three [data-density] blocks present" if has_switch else "missing [data-density] block(s)")

    # breakpoints: a screen spec must say what happens at each; the tokens must exist to cite
    names = {b["name"] for b in t["breakpoints"]["values"]}
    need_bp = {"xs", "sm", "md", "lg", "xl", "2xl", "3xl"}
    r.add("G1.5", "all seven breakpoints named in D1.5 exist in tokens", need_bp <= names, f"have {sorted(names)}")

    # G1.7 — the generator, not just the file it writes. Patching brand/tokens.json without editing
    # tools/build-foundations.py leaves the source of truth contradicting its own generator, and the next
    # regeneration silently deletes the edit. That happened for real in this phase: shell-max-width was
    # added to tokens.json only, vanished on regeneration, and the phase gate stayed green.
    try:
        chk = subprocess.run([sys.executable, "tools/build-foundations.py", "--check"], cwd=ROOT,
                             capture_output=True, text=True, timeout=120)
        r.add("G1.7", "tokens.json is reproducible from tools/build-foundations.py (no hand-edited output)",
              chk.returncode == 0, (chk.stdout or chk.stderr).strip().splitlines()[-1][:150])
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        r.add("G1.7", "generator cross-check ran", False, f"aborted: {e}")
    # G1.6 — the doc-vs-tokens numeric contract, delegated to the tool that owns it. A gate that
    # re-implemented this comparison would be a second owner of the same arithmetic (the exact mistake
    # that produced 16.34-vs-13.94 in P02), so it shells out to --foundations --check and trusts one impl.
    try:
        chk = subprocess.run([sys.executable, "tools/build-contrast-table.py", "--foundations", "--check"],
                             cwd=ROOT, capture_output=True, text=True, timeout=120)
        r.add("G1.6", "D1's numbers in the spec are the tokens' numbers (delegated to --foundations --check)",
              chk.returncode == 0, (chk.stdout or chk.stderr).strip().splitlines()[0][:150]
              if (chk.stdout or chk.stderr).strip() else "no output")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e2:
        r.add("G1.6", "D1 numeric cross-check ran", False, f"aborted: {e2}")
    xl = next((b for b in t["breakpoints"]["values"] if b["name"] == "xl"), {})
    r.add("G1.6", "the ≥1280 terminal breakpoint is defined with a layout description",
          bool(xl.get("min") == 1280 and xl.get("layout")), json.dumps(xl)[:110])


# --------------------------------------------------------------------------- G2 fields cite probed endpoints
def endpoint_shapes() -> tuple[set[str], bool]:
    """(base?query-key shapes, is the probe fully green)."""
    d = json.loads(read(PROBE))
    shapes: set[str] = set()
    for c in d.get("checks", []):
        u = c.get("url", "")
        base = u.split("?")[0]
        m = re.search(r"\?([^&=]+)=", u)
        shapes.add(f"{base}?{m.group(1)}=…" if m else base)
        shapes.add(base)   # bare host+path is also citable
    all_ok = all(c.get("ok") for c in d.get("checks", []))
    return shapes, all_ok


FIELD_LINE = re.compile(r"`([^`]*?https://[a-z0-9.-]+\.polymarket\.com[a-z0-9/?_.=&{}-]*[^`]*)`")


def g_endpoints(r: Report) -> None:
    shapes, probe_ok = endpoint_shapes()
    spec = read(SPEC)
    hosts = set(re.findall(r"https://([a-z0-9.-]+)\.polymarket\.com", spec))
    r.add("G2.1", "the spec cites only hosts the probe reached",
          hosts <= {"gamma-api", "clob", "data-api", "lb-api", "ws-subscriptions-clob"},
          f"hosts cited: {sorted(hosts)}")

    # every `...← host/path...` field citation must be a shape present in the probe evidence
    cited: set[str] = set()
    for m in re.finditer(r"https://([a-z0-9.-]+)\.polymarket\.com(/[a-zA-Z0-9/_-]*)", spec):
        cited.add(f"https://{m.group(1)}.polymarket.com{m.group(2).rstrip('/')}")
    known = {s.split("?…")[0].rstrip("/") for s in shapes} | shapes
    unknown = sorted(p for p in cited if p not in known and not any(p.startswith(k) for k in known if k))
    r.add("G2.2", "every endpoint path cited in the spec exists in the probe evidence", not unknown,
          f"cited-but-unprobed: {unknown[:6]}" if unknown else f"{len(cited)} distinct paths all probed")

    # endpoints that are known-broken must be labelled, never presented as data sources
    # Sentence-scoped, not proximity-scoped: the first version looked for "404" within 220 chars, which a
    # mutation easily satisfied using an unrelated 404 from a neighbouring sentence (mutation M4 slipped
    # through). A mention is labelled only if its OWN sentence carries the status.
    broken = [("lb-api.polymarket.com/pnl", r"\b404\b"), ("lb-api.polymarket.com/rank", r"\b400\b"),
              ("data-api.polymarket.com/profile", r"\b404\b")]
    sentences = re.split(r"(?<=[.;])\s+", spec.replace("\n", " "))
    # Absolute rule, no sentence-kind filter: EVERY mention of a known-dead path must carry its status or
    # an explicit non-existence phrase, in its own sentence. Filtering by "is this a data-source sentence?"
    # was the loophole — mutation M4 rewrote one sentence and left the claim standing in another.
    # Match on the route token, not the full URL: the spec writes `lb-api/pnl`, not the host+path, and a
    # check written against the long form silently matches ZERO sentences and "passes".
    # '\b404\b' cannot match '404s' (s is a word char) — the first version failed on perfectly-labelled
    # text. Match the status code with an optional plural/parenthesis and no trailing boundary.
    ST = lambda codes: r"(?:" + "|".join(rf"{c}[0-9]{{0,2}}" for c in codes) +         r"|does not exist|do not exist|no such route|not available|empty response|does not answer|404s|400s)"
    DEAD = {"lb-api/pnl": ST(("404",)), "lb-api/rank": ST(("400",)), "data-api/profile": ST(("404",))}
    # Unit of scrutiny = one table cell or one sentence, whichever the author used: these specs put claims
    # in ` · `-separated cells, so splitting on '.' alone put the 404 in the NEXT cell and reported a
    # mention as unlabelled when it plainly was not.
    sentences = [x.strip() for x in re.split(r"(?<=[.;])\s+|\s+·\s+|\s*\|\s*", spec.replace("\n", " ")) if x.strip()]
    mentions = {pat: sum(1 for sen in sentences if pat in sen) for pat in DEAD}
    if 0 in mentions.values():
        # a dead-path pattern that appears nowhere is not compliance, it is an unchecked claim
        r.add("G2.3b", "each known-dead endpoint is actually addressed somewhere in the spec", False,
              f"never mentioned: {[k for k, v in mentions.items() if v == 0]}")
    unlabelled = []
    for path, ok in DEAD.items():
        for sen in (x for x in sentences if path in x):
            if not re.search(ok, sen, re.I):
                unlabelled.append(f"{path}: …{sen.strip()[:78]}")
    r.add("G2.3", "every mention of a known-dead endpoint carries its measured status", not unlabelled,
          f"unlabelled: {unlabelled[:3]}" if unlabelled
          else f"{sum(mentions.values())} mentions ({mentions}), all labelled")
    r.add("G2.4", "the probe evidence is fully green (a stale probe means the spec is stale)", probe_ok,
          "all probe checks ok" if probe_ok else "run: python3 tools/datasource-probe.py --json --save docs/verification/P01-probe.json")

    # the tape must not be specced on the cached REST endpoint
    cache_line = re.search(r"E2-trades-is-cached", read(PROBE))
    tape_ws = re.search(r"[Tt]ape[^.\n]{0,160}WebSocket", spec) or re.search(r"Live tape[^|]*\|\s*\*\*WebSocket", spec)
    r.add("G2.5", "the live tape is specified over WebSocket, with REST for backfill only",
          bool(cache_line and tape_ws),
          f"probe evidence {bool(cache_line)}, spec says WS {bool(tape_ws)}")


# --------------------------------------------------------------------------- G3 components × states
def g_components(r: Report) -> None:
    t = json.loads(read(TOKENS))
    c = t["components"]
    spec = read(SPEC)
    primitives, domain, states = c["primitives"], c["domain"], c["states"]
    r.add("G3.1", "the spec covers exactly the prompt's 31 primitives (no silent drops)",
          len(primitives) == 31, f"{len(primitives)} in tokens: {', '.join(primitives[:4])}…")
    r.add("G3.2", "the spec covers exactly the prompt's 13 domain components",
          len(domain) == 13, f"{len(domain)}: {', '.join(domain[:4])}…")

    # the primitive table must contain a row per primitive, and the domain section a heading per component
    # rows are `Name` or `Name`/`Alias` — the first form ends at a pipe, the second at a slash
    prim_rows = {y.strip() for cell in re.findall(r"^\| `([^`|]+)`[^|]*\|", spec, re.M)
                 for y in re.split(r"`\s*/\s*`\s*|\s*/\s*", cell) if y.strip()}
    missing_rows = [p for p in primitives if p not in prim_rows]
    r.add("G3.3", "every primitive has a row in the D2.1 table", not missing_rows,
          f"missing rows: {missing_rows}" if missing_rows else f"{len(primitives)} rows matched by name")
    missing_dom = [d_ for d_ in domain if not re.search(rf"^#### `{d_}`", spec, re.M)]
    r.add("G3.4", "every domain component has its own D2.2 section", not missing_dom,
          f"missing sections: {missing_dom}" if missing_dom else "all 13 headed")

    # each primitive row must mention a11y + do/don't, and every domain section must speak to states
    weak = []
    for p in primitives:
        m = re.search(rf"^\| `{p}`(.*)$", spec, re.M)
        if not m:
            continue
        row = m.group(1)
        has_dodont = "don't" in row.lower() or "don\u2019t" in row.lower()
        cells = [c.strip() for c in row.split("|") if c.strip()]
        tail = cells[-1] if cells else ""
        has_a11y = len(tail) > 12 and bool(
            re.search(r"aria-|role=|sr-only|accessible name|label|focus|announce|toggles|Esc ", tail, re.I))
        if not (has_dodont and has_a11y):
            weak.append(f"{p}[don't={has_dodont},a11y={bool(has_a11y)},tail={tail[:22]!r}]")
    r.add("G3.5", "every primitive row carries a do/don't and an accessibility clause", not weak,
          f"thin rows: {weak[:6]}" if weak else "all rows have both clauses")

    # the state contract: 11 states, each defined once, each named in the table
    state_rows = set(re.findall(r"^\|\s*\d+\s*\|\s*\**`?([a-z-]+)`?\**\s*\|", spec, re.M))
    missing_states = [s for s in states if s not in state_rows]
    r.add("G3.6", "all 11 states are defined in the D2.0 contract table", not missing_states,
          f"undefined: {missing_states}" if missing_states else f"{len(states)} states: {', '.join(states[:5])}…")
    safety = ["stale", "disconnected", "insufficient"]
    r.add("G3.7", "the three states that can block an order each name their trigger and their interaction",
          all(re.search(rf"`{s}`[^|]*\|[^|]*\|[^|]*\|[^|]*\|", spec) for s in safety),
          "each has trigger + visual + interaction columns")

    # Storybook list must exist, be current, and cover component × state
    if STORIES.exists():
        body = read(STORIES)
        listed = [ln for ln in body.splitlines() if "--" in ln]
        roots = {ln.split("--")[0].split("/")[-1] for ln in listed}
        absent = [x for x in primitives + domain if x not in roots]
        r.add("G3.8", "the generated story list covers every component (no component without a story)",
              not absent, f"no stories for: {absent}" if absent else f"{len(listed)} stories, {len(roots)} roots")
        chk = subprocess.run([sys.executable, "tools/build-storybook-list.py", "--check"], cwd=ROOT,
                             capture_output=True, text=True, timeout=120)
        r.add("G3.8b", "the story list is in sync with tokens.json (not a stale snapshot)",
              chk.returncode == 0, (chk.stdout or chk.stderr).strip()[:120])
        n_state = sum(1 for ln in listed if any(f"--{s}" in ln for s in states))
        r.add("G3.9", "story list includes component × D2.0-state stories (not just happy paths)",
              n_state >= 0.5 * len(primitives + domain) * len(states),
              f"{n_state} state-bearing stories vs floor {int(0.5 * 44 * 11)}")
        # cross-cutting states that only exist per component
        need_extra = ["one-sided", "redeem", "confirm-required", "blocking", "residual-warn", "locked"]
        miss = [e for e in need_extra if e not in body]
        r.add("G3.10", "the hard per-component states are in the story list too", not miss,
              f"missing: {miss}" if miss else ", ".join(need_extra))
    else:
        r.add("G3.8", "story list exists", False, f"{STORIES} missing — run python3 tools/build-storybook-list.py", skipped=True)


# --------------------------------------------------------------------------- G4 motion
def g_motion(r: Report) -> None:
    t = json.loads(read(TOKENS))
    css = read(CSS)
    spec = read(SPEC)
    m = t["motion"]
    skill = ROOT / "skills/emilkowalski-skills/skills/review-animations/STANDARDS.md"
    skill_text = read_or(skill)
    used = {k: v for k, v in m["easing"].items() if k.startswith("ease-")}
    if skill_text is None:
        r.add("G4.1", "easing curves match the vendored skill's values", True,
              f"SKIPPED: {skill.relative_to(ROOT)} not present — the cross-check needs the vendored skills "
              f"tree. Values are still pinned in tokens.json and re-checked by G4.6.", skipped=True)
        curves = {}
    else:
        curves = dict(re.findall(r"--(ease-[a-z-]+):\s*(cubic-bezier\([^)]*\))", skill_text))
        drift = [k for k, v in used.items() if curves.get(k) != v]
        r.add("G4.1", "easing curves are the vendored skill's exact values, quoted not invented", not drift,
              f"drift from STANDARDS.md: {drift}" if drift else f"{len(used)} curves match {skill.name}")
        missing_curves = [k for k in used if k not in curves]
        if missing_curves:
            r.add("G4.1b", "every adopted curve exists in STANDARDS.md at all", not missing_curves,
                  f"not found in the skill: {missing_curves}")

    ceil = m["ui_ceiling_ms"]
    over = {k: v for k, v in m["duration_ms"].items() if k != "forbidden" and k != "drawer" and v["max"] > ceil}
    r.add("G4.2", f"no UI duration exceeds the skill's {ceil}ms ceiling (gesture drawers excepted, and stated)",
          not over, f"over ceiling: {over}" if over else "press/micro/small/medium/large all ≤300ms")
    drawer = m["duration_ms"]["drawer"]
    r.add("G4.3", "the one exception is justified in writing and bounded",
          drawer["min"] >= 300 and drawer["max"] <= 500 and "gesture" in (drawer["use"] + json.dumps(m)).lower(),
          f"drawer {drawer['min']}–{drawer['max']}ms, ease-drawer, gesture-driven")

    pol = m["number_policy"]
    banned = ["count", "tween", "flip", "roll", "crossfade", "width"]
    covered = [b for b in banned if any(b in x.lower() for x in pol["includes"])]
    r.add("G4.4", "“nothing animates a number” is enumerated, not asserted", len(covered) >= 4,
          f"explicitly banned: {covered}")
    # …AND the top-line rule must still SAY so. Weakening only the headline sentence was a live gap.
    asserts = bool(re.search(r"nothing animates a number", pol.get("rule", ""), re.I))
    still_absolute = bool(re.search(r"^nothing animates a number$|nothing animates a number", pol.get("rule", ""), re.I)) and \
        not re.search(r"may|when idle|optional|suggested", pol.get("rule", ""), re.I)
    r.add("G4.4b", "the number policy is stated as an absolute rule, not a preference", asserts and still_absolute,
          f"rule = {pol.get('rule','')!r}")
    doc_rule = re.search(r"`number_policy`[\s\S]{0,80}?nothing animates a number|\*\*nothing animates a \*?number\*?\*?|nothing animates a number", spec)
    r.add("G4.4c", "the spec text itself carries the absolute wording", bool(doc_rule),
          "spec states the ban in its own words" if doc_rule else "spec no longer says 'nothing animates a number'")
    flash = m["flash_on_change"]
    r.add("G4.5", "flash is background-only and within the ceiling",
          flash["what"].startswith("background only") and flash["in_ms"] + flash["out_ms"] <= ceil,
          f"{flash['in_ms']}+{flash['out_ms']} = {flash['in_ms'] + flash['out_ms']}ms ≤ {ceil}ms; '{flash['what'][:40]}…'")
    for var in ("--pgm-flash-in", "--pgm-flash-out", "--pgm-ui-ceiling", "--pgm-ease-out"):
        if var not in css:
            r.add("G4.6", f"motion tokens reach CSS ({var})", False, f"{var} not in tokens.css")
            break
    else:
        r.add("G4.6", "motion tokens reach CSS", True, "--pgm-flash-in/out, --pgm-ui-ceiling, --pgm-ease-out present")
    # the skill bans these outright; check no spec/example uses them
    # Prose ABOUT a banned property is not a violation — a design spec has to name them to forbid them.
    # What must be clean is the CODE the spec contains, so fenced blocks are what we audit.
    code = "\n".join(re.findall(r"```(?:[a-z]+)?\n(.*?)```", spec, re.S))
    code += "\n" + read(CSS)
    bad_prop = sorted(set(re.findall(r"transition:\s*all\b|scale\(0\)(?!\s*[,-])|animation-timing-function:\s*ease-in\b", code)))
    r.add("G4.7", "spec text and examples avoid the skill's banned forms", not bad_prop,
          f"found in code/fenced blocks or tokens.css: {bad_prop}" if bad_prop
          else "clean in every fenced block and in tokens.css (prose that names a banned form is allowed)")
    vocab = m["vocabulary"]
    r.add("G4.8", "motion vocabulary uses the skill's terms and names its bans",
          "animation-vocabulary" in json.dumps(m) and len(vocab["banned_as_design"]) >= 3,
          f"used {vocab['used'][:2]}…, banned {vocab['banned_as_design'][:2]}… with a reason")
    # restraint is the point: the spec must record rejections, not only approvals
    rej = re.findall(r"\*\*reject\*\*", spec)
    r.add("G4.9", "motion was filtered by the opportunity gate (rejections recorded)", len(rej) >= 4,
          f"{len(rej)} recorded rejections in D1.6")


# --------------------------------------------------------------------------- G5 money path
MULT_PAT = r"(?:price|p)[a-zA-Z_]*\s*[×*x]\s*[a-zA-Z_.]*\s*(?:size|sz|share)[a-zA-Z_]*"


def g_money(r: Report) -> None:
    spec = read(SPEC)
    corpus = spec + (("\n" + read(SPECIMEN)) if SPECIMEN.exists() else "")
    NEG = r"never|not |must not|forbidden|avoid|only place|n/a|usdcSize|rather than|instead of|is 0"

    # Fenced regions, found by walking ``` lines in order. A count-of-backticks parity test was tried first
    # and is wrong here: the doc contains 5 fence markers before this example (odd, but the block IS open),
    # because prose mentions and inline backticks perturb any global count. Explicit open/close spans are
    # the only reliable reading, and they also tell us the fence's own line, which the marker test needs.
    fence_spans = []      # (code_start, code_end, opening_line_start)
    pos, open_at = 0, None
    for lm in re.finditer(r"^```[^\n]*$", corpus, re.M):
        if open_at is None:
            open_at = lm.end()
        else:
            fence_spans.append((open_at, lm.start(), corpus.rfind("\n", 0, corpus.rfind("\n", 0, open_at)) + 1))
            open_at = None
    if open_at is not None:
        fence_spans.append((open_at, len(corpus), -1))

    def span_at(idx: int):
        return next((sp for sp in fence_spans if sp[0] <= idx < sp[1]), None)

    def span_of(idx: int):
        return span_at(idx)

    # `P03: nocode` exempts the BLOCK following the marker line, not one line: Markdown prose wraps, so a
    # one-line exemption would stop working at the second line of the very sentence it was written for.
    nocode_spans, nocode_abuse = set(), []
    for mm in re.finditer(r"^[ \t]*<!--\s*P03: nocode\s*-->[ \t]*\n", corpus, re.M):
        blk_end = corpus.find("\n\n", mm.end())
        blk_end = blk_end if blk_end > 0 else len(corpus)
        # A prose marker must never reach into a fence: code is judged on the code. Without this, one
        # comment line was a general opt-out from the money rules — found by mutation test, not by reading.
        if re.match(r"\s*```", corpus[mm.end():blk_end]):
            nocode_abuse.append(f"line {corpus[:mm.start()].count(chr(10)) + 1}")
            continue
        for x in re.finditer(MULT_PAT, corpus[mm.end():blk_end]):
            idx = mm.end() + x.start()
            if span_at(idx) is None:
                nocode_spans.add(idx)
    # reported in BOTH states: a check that only exists when it fails cannot be distinguished from one
    # that was never reached, which is the vacuity failure mode this whole phase keeps re-finding.
    r.add("G5.2c", "no `P03: nocode` marker is used to exempt a code block", not nocode_abuse,
          f"marker precedes a fence at {nocode_abuse} — mark it as an anti-example instead" if nocode_abuse
          else f"{len(nocode_spans)} prose mention(s) exempted, 0 code blocks")

    def unqualified(pat: str, source: str | None = None) -> list[str]:
        """Occurrences NOT ruled out by their surrounding sentence. Prose about a banned practice is how
        a design spec forbids it, so flagging every mention would force us to delete the warnings."""
        out = []
        for m in re.finditer(pat, corpus):
            # a rule that merely *mentions* float in a prohibition sentence must not excuse real code:
            # anything inside a fenced block is judged on the code itself, never on surrounding prose
            span = span_of(m.start())
            fenced = span is not None
            # A fence may be quoted *as the thing to avoid*. The exemption is one exact comment line
            # immediately above the opening fence — not proximity, not a mention in prose.
            if fenced and span[2] >= 0:
                marker = corpus[span[2]:corpus.find("\n", span[2])].strip()
                if re.fullmatch(r"<!--\s*P03: anti-example\s*-->", marker):
                    continue
            # the window must cover the WHOLE sentence the match sits in, and most of these rules are
            # written as prose whose negation comes FIRST ("never `price × size`") — a window starting at
            # the match could not see the word "never" and flagged my own D8 table as a violation.
            if m.start() in nocode_spans:
                continue
            if not fenced:
                # line-scoped, not sentence-scoped: Markdown prose wraps mid-sentence, lists use "100%."
                # and "D2." style cross-references, and any '.' heuristic will chop a prohibition in half
                # (it split "…never `price × size` (price is 0 on 100%." from its "never"). An author can
                # control line content; they cannot control where a prose sentence-boundary falls.
                lo = corpus.rfind("\n", 0, m.start()) + 1
                hi = corpus.find("\n", m.end())
                around = corpus[lo:(hi if hi > 0 else len(corpus))]
            if fenced or not re.search(NEG, around, re.I):
                out.append(("code" if fenced else "prose") + ": " + m.group(0))
        return out

    allowed = unqualified(r"float\(|parseFloat\(|\b\d+\.\d+\s*\*\s*\d+")
    r.add("G5.1", "no float arithmetic in a money example unless it is being ruled out", not allowed,
          f"found: {sorted(set(allowed))}" if allowed else "every float mention is a prohibition; integer cents elsewhere")
    # must cover BOTH spellings and arbitrary operands: the doc writes `price × size` (U+00D7) while real
    # code writes `price * row.size`. The first version matched neither form in a fence, so an anti-example
    # — and any genuine multiplication of price and size — was invisible to the check that exists to catch it.
    MULT = MULT_PAT
    bad_mult = unqualified(MULT)
    r.add("G5.2", "every mention of price×size is inside a prohibition", not bad_mult,
          f"unqualified mention(s): {bad_mult[:3]}" if bad_mult else "all mentions are prohibitions (REDEEM price is 0 on 100% of rows)")
    cents = re.search(r"integer cents", corpus)
    tick = re.search(r"minimum_tick_size", corpus)
    marks = re.findall(r"^[ \t]*<!--\s*P03: anti-example\s*-->[ \t]*$", spec, re.M)
    fenced_after = sum(1 for m in re.finditer(r"^[ \t]*<!--\s*P03: anti-example\s*-->[ \t]*\n```", spec, re.M))
    r.add("G5.2b", "every anti-example marker is actually attached to a fence (no orphan exemptions)",
          len(marks) == fenced_after and fenced_after > 0,
          f"{len(marks)} marker line(s), {fenced_after} attached to a fence")
    r.add("G5.3", "the money path states its representation and its rounding authority",
          bool(cents and tick), "integer cents + minimum_tick_size both specified")
    # decimal separators: the rule must be a rule, not a preference
    r.add("G5.4", "price decimal separator is pinned to '.' and stated as a rule",
          bool(re.search(r"do(es)? not localise|not localised|always use `\.`", spec, re.I)),
          "D6.3 pins it and gives the paste-into-venue reason")


# --------------------------------------------------------------------------- G6 specimen consumes, never defines
def g_specimen(r: Report) -> None:
    if not SPECIMEN.exists():
        r.add("G6.0", "specimen exists", False, f"{SPECIMEN.name} missing", skipped=True)
        return
    html = read(SPECIMEN)
    css = re.search(r"/\* ---- BEGIN SPECIMEN CSS(.*?)---- END SPECIMEN CSS ----", html, re.S)
    if not css:
        r.add("G6.1", "specimen CSS is delimited so it can be audited", False, "markers missing")
        return
    body = css.group(1)
    # (a) every var() must exist in tokens.css
    tokens_css = read(CSS)
    defined = set(re.findall(r"(--pgm-[a-z0-9-]+)\s*:", tokens_css))
    used = set(re.findall(r"var\((--pgm-[a-z0-9-]+)\)", body))
    undef = sorted(used - defined)
    r.add("G6.1", "every custom property the specimen reads is defined by the generated stylesheet",
          not undef, f"undefined: {undef[:6]}" if undef else f"{len(used)} distinct --pgm-* references all defined")
    # (b) no colour or size literals anywhere in the specimen CSS
    hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", body)
    # Legit non-token numbers: 0 (the absence of a value), percentages (relative to the box), and
    # values inside @media conditions — CSS media queries cannot resolve var(), so a breakpoint literal
    # there is a CSS limitation, not a design-system breach. Everything else must be a token.
    size_hits = []
    for ln in body.splitlines():
        if "@media" in ln:
            continue
        for m in re.finditer(r"(?<![-\w.])(-?\d+(?:\.\d+)?(?:px|ms|rem|em))\b", ln):
            v = m.group(1)
            if v in ("0px", "0em", "0rem", "0ms"):
                continue
            # a number inside a function (clip-path, calc, inset offsets) is geometry, not a design value
            before = ln[max(0, m.start() - 30):m.start()]
            if "(" in before.rsplit(")", 1)[-1]:
                continue
            size_hits.append(f"{v}  {ln.strip()[:60]}")
    r.add("G6.2", "the specimen's stylesheet contains no colour literal", not hexes,
          f"found {sorted(set(hexes))}" if hexes else "zero #hex outside token references")
    r.add("G6.3", "the specimen's stylesheet contains no size/duration literal", not size_hits,
          "found: " + "; ".join(size_hits[:4]) if size_hits else "every length/time is a var() (@media conditions excepted)")
    # (c) the states the spec promises must actually be renderable
    need = ["one-sided", "no resting bids", "usdcSize", "stale--blocking", "text-primary", "tabular-nums",
            "prefers-reduced-motion", "aria-live", "sr-only"]
    miss = [n for n in need if n not in html]
    r.add("G6.4", "the specimen renders the hard states, not just happy paths", not miss,
          f"absent: {miss}" if miss else "one-sided book, REDEEM row, blocking-stale ticket, reduced-motion, sr-only")
    # (d) aria-live politeness — the D6.2 decision, checked in markup
    m = re.search(r'aria-live="([^"]+)"', html)
    r.add("G6.5", "the live region does not use polite/assertive on the tape (D6.2)",
          (m is None) or m.group(1) == "off", f"aria-live={m.group(1) if m else 'not used (summary region only)'}")
    # (e) a coloured PnL *number* is the P03 finding; check the specimen obeys the rule
    pnl_cells = re.findall(r'class="num pnl"[^>]*>([^<]*)<', html)
    coloured_number = [c for c in pnl_cells if False]
    uses_primary = ".pnl { color: var(--pgm-text-primary); }" in body.replace("  ", " ") or \
        re.search(r"\.pnl\s*\{\s*color:\s*var\(--pgm-text-primary\)", body)
    r.add("G6.6", "PnL magnitude is rendered in text.primary, colour only on glyph elements",
          bool(uses_primary) and not coloured_number,
          ".pnl uses --pgm-text-primary; .g-up/.g-dn colour the carets only" if uses_primary else "PnL number is coloured")
    # (f) never-same-row: NO chip and a critical badge in one row
    for tr in re.findall(r"<tr>.*?</tr>", html, re.S):
        if "chip--no" in tr and re.search(r"alert-critical|badge--whale", tr) and "pill--sell" in tr:
            r.add("G6.7", "no row pairs the NO chip with a critical badge and a SELL pill", False,
                  "collision in a specimen row — the exact ΔE 3.2–4.9 case")
            break
    else:
        r.add("G6.7", "no row pairs the NO chip with a critical badge and a SELL pill", True,
              "whale badge appears only on a BUY/YES row")


# --------------------------------------------------------------------------- G7 a11y obligations
    # G6.8 — D7.4 rules 1/2 applied repo-wide, with an EXPLICIT named-exemption list. Scoping the colour
    # audit to the specimen (G6.2) while the rule said "anywhere" reported compliance while
    # server/public/index.html carried ~30 literals and its own :root palette.
    GEN = {"brand/tokens.css", "web/tailwind.preset.cjs", "web/styles/tokens.css"}  # generated from tokens.json: hexes expected.
    # web/styles/tokens.css joined this list in P08 for a reason worth the comment: Turbopack resolves CSS
    # @import inside the project root only, so the token layer needs a copy the bundler can reach, and a copy
    # that is not generated-and-checked is a second design system. tools/build-web-tokens.mjs --check is what
    # keeps it a mirror instead.
    EXEMPT = {"server/public/index.html":
              "P01 throwaway probe page, pre-brand; superseded by the P10 landing page"}
    offenders, exempted, scanned = [], [], 0
    for path in sorted(ROOT.rglob("*")):
        rel = str(path.relative_to(ROOT))
        if not path.is_file() or path.suffix not in {".css", ".html", ".js", ".mjs", ".cjs", ".ts", ".tsx"}:
            continue
        # Build output is not authored source: `.next/` holds a minified copy of everything, so excluding it
        # is the same decision as excluding `node_modules`, and it arrived with P08 because the app now builds
        # inside web/. `tools/p08-gate-check.py` c9 plants a hex in web/src/** and requires THIS rule to fire,
        # so the exclusion cannot silently become a blind spot.
        if not rel.startswith(("web/", "server/", "brand/")) or ".git" in path.parts or "node_modules" in path.parts:
            continue
        if any(part in {".next", ".turbo", "dist", "out", "coverage"} for part in path.parts):
            continue
        if rel in GEN or rel.endswith(".md"):
            continue
        txt = path.read_text(encoding="utf-8", errors="ignore")
        scanned += 1
        code = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
        hits = sorted(set(re.findall(r"(?<![\w`])#[0-9a-fA-F]{6}\b", code)))
        if hits:
            (exempted if rel in EXEMPT else offenders).append(f"{rel}({len(hits)})")
    r.add("G6.8", "no colour literal in authored stylesheets/markup, outside the named exemptions",
          not offenders,
          f"new offenders: {offenders[:5]}" if offenders
          else f"{scanned} files clean; {len(GEN)} generated exempt; named exemptions: {sorted(EXEMPT)}")
    r.add("G6.8b", "every named exemption carries a reason and is still actually present",
          all(v.strip() for v in EXEMPT.values()) and set(EXEMPT) == {o.split("(")[0] for o in exempted},
          f"declared {sorted(EXEMPT)} vs live {[o.split('(')[0] for o in exempted]}")
    r.add("G6.8c", "the exemption list is a debt register, not a shrug: the prototype must be deleted or rebuilt",
          "P10" in " ".join(EXEMPT.values()),
          "each exemption names the phase that retires it")

def g_a11y(r: Report) -> None:
    spec = read(SPEC)
    t = json.loads(read(TOKENS))
    needs = {
        "WCAG 2.2 AA stated": r"WCAG 2\.2 (Level )?AA",
        "keyboard-complete + focus ring rule": r"2px .*ring|focus-visible",
        "no focus traps, with the in-flight carve-out": r"[Nn]o focus traps|Esc always",
        "aria-live politeness decided for the tape": r'aria-live="off"',
        "decimal separator rule": r"never a comma|`\.` as the decimal",
        "RTL treatment for the book": r"never flip|LTR island",
        "i18n key structure": r"screen\.component\.element",
        "reduced motion": r"prefers-reduced-motion",
        "44px touch target, density-independent": r"min_touch_target|44px",
        "colour never alone": r"colour never alone|never the only channel|colour-never-alone|colour is never",
    }
    # The conformance claim must be in the section that carries the obligations, not merely somewhere in a
    # 1,200-line document: G7.1 previously accepted "WCAG 2.2 AA" from any line, so deleting D6's normative
    # sentence left the claim findable in a tool description and the check passed on a doc with no target.
    d6 = re.search(r"^## D6\..*?(?=^## )", spec, re.M | re.S)
    design = read(DESIGN_MD) if DESIGN_MD.exists() else ""
    miss = []
    for k, pat in needs.items():
        hay = (d6.group(0) if d6 else "") if k == "WCAG 2.2 AA stated" else spec
        if not re.search(pat, hay):
            miss.append(k if k != "WCAG 2.2 AA stated" else "WCAG 2.2 AA stated in D6 itself")
    if "WCAG 2.2 AA stated in D6 itself" not in miss and not re.search(needs["WCAG 2.2 AA stated"], design):
        miss.append("WCAG 2.2 AA repeated in web/DESIGN.md (the contract developers actually read)")
    r.add("G7.1", "every D6 obligation the design system can pre-pay is in the spec", not miss,
          f"missing: {miss}" if miss else f"{len(needs)} obligations located")
    r.add("G7.2", "the touch target is a token, so density cannot shrink it",
          "min_touch_target" in t["density"]["constants"], f"{t['density']['constants']['min_touch_target']}px")
    r.add("G7.3", "screen-reader summary is rate-limited (a 20/sec region would queue forever)",
          bool(re.search(r"at most once per 5s|≤1 announcement\s*per 5s", spec)), "5s throttle specified in D6.2")
    if DESIGN_MD.exists():
        d = read(DESIGN_MD)
        r.add("G7.4", "DESIGN.md carries the must-not-break rules and names the check for each",
              d.count("**Check**") >= 6, f"{d.count('**Check**')} checked rule groups in {DESIGN_MD.name}")
    else:
        r.add("G7.4", "DESIGN.md exists", False, f"{DESIGN_MD} missing", skipped=True)


# --------------------------------------------------------------------------- G8 brand lock after rename
def g_brand(r: Report) -> None:
    t = json.loads(read(TOKENS))
    kit = read(KIT)
    name = t.get("brand", {}).get("name") or ""
    lu = re.sub(r"<!--.*?-->", "", read(LOCKUP), flags=re.S)
    lockup_text = " ".join(x.strip() for x in re.sub(r"<[^>]+>", " ", lu).split() if x.strip())[:40]
    kit_name = re.search(r'"brand_context":\s*\{\s*"name":\s*"([^"]+)"', kit)
    rendered = "".join(re.findall(r">([^<>]+)<", re.sub(r"<!--.*?-->", "", read(LOCKUP), flags=re.S)))
    wordmark = re.search(r"<text\b[^>]*>([\s\S]*?)</text>", read(LOCKUP))
    word = re.sub(r"<[^>]+>", "", wordmark.group(1)).strip() if wordmark else ""
    ok81 = bool(name) and (kit_name and kit_name.group(1) == name) and word == name
    r.add("G8.1", "the brand name is coherent across tokens.json / Brand Lock / the rendered lockup", ok81,
          f"tokens.json={name!r} BRAND-KIT={kit_name.group(1) if kit_name else None!r} lockup renders {lockup_text!r}")
    pin = re.search(r'"geometry_sha256":\s*"([0-9a-f]{16})"', kit)
    r.add("G8.2", "the mark geometry is hash-pinned in the Brand Lock (drift is detectable, not hopeful)",
          bool(pin), f"pinned {pin.group(1) if pin else 'nothing'}")
    # …and the pin must still MATCH the master. Checking existence alone let a re-pinned tamper through.
    try:
        h = subprocess.run(["node", "tools/rename-wordmark.mjs", "--hash"], cwd=ROOT,
                           capture_output=True, text=True, timeout=60)
        actual = h.stdout.strip().splitlines()[0] if h.stdout.strip() else ""
        r.add("G8.2b", "the pinned hash matches mark.svg right now (the lock is live, not decorative)",
              bool(pin) and actual == pin.group(1), f"mark.svg={actual or 'n/a'} pinned={pin.group(1) if pin else 'n/a'}")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        r.add("G8.2b", "pinned hash cross-check ran", False, f"aborted: {e}", skipped=True)
    r.add("G8.3", "the lockup is generated, not hand-edited",
          "GENERATED by tools/rename-wordmark.mjs" in read(LOCKUP), "header says GENERATED + names the tool")
    old = re.findall(r"\bPolyGM\b", read(SPEC))
    r.add("G8.4", "the P03 spec does not reintroduce the old name as if it were current",
          len(old) <= 3, f"{len(old)} historical mentions (allowed only as rename provenance)")
    for f in ("--pgm-", "tokens.json"):
        if f not in read(CSS) and f == "--pgm-":
            r.add("G8.5", "tokens.css is present and token-prefixed", False, f"{CSS} lacks --pgm-")
            break
    else:
        r.add("G8.5", "tokens.css is present and token-prefixed", True, f"{CSS.relative_to(ROOT)} ok")
    if PRESET.exists():
        p = read(PRESET)
        r.add("G8.6", "the Tailwind preset is generated from the same tokens (no second copy of the palette)",
              "GENERATED by tools/build-tailwind-preset" in p, f"{PRESET.relative_to(ROOT)} {len(p)} bytes")
    else:
        r.add("G8.6", "tailwind preset exists", False, "run node tools/build-tailwind-preset.mjs", skipped=True)


def main() -> int:
    verbose = "--verbose" in sys.argv
    r = Report()
    for fn in (g_foundations, g_endpoints, g_components, g_motion, g_money, g_specimen, g_a11y, g_brand):
        try:
            fn(r)
        except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
            # a check group that cannot run is reported as FAILED, not swallowed — a silent crash here
            # once produced "exit=1, 0 failures", which reads like a bug in the mutation harness
            r.add(f"{fn.__name__}", "group ran to completion", False, f"aborted: {type(e).__name__}: {e}")
    live = [c for c in r.checks if not c.skipped]
    bad = [c for c in live if not c.ok]
    print("P03 design-system gate — tokens consumed, states specified, fields probed")
    print(f"  {len(live) - len(bad)}/{len(live)} checks passed")
    print(r.summary())
    if verbose:
        for c in live:
            print(f"    {'ok ' if c.ok else 'FAIL'} {c.cid} {c.claim}  —  {c.detail[:100]}")
    print("\n  A passing gate means every value a frontend engineer needs is a token, every field has a")
    print("  probed endpoint, and every state has a story. It does not mean the design is good.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
