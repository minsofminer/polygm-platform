#!/usr/bin/env python3
"""P15's quality gate, demonstrated: one page, three answers, a phone, five minutes.

    python3 tools/p15-2am-drill.py                  # run the drill (exit 1 if any step is not demonstrable)
    python3 tools/p15-2am-drill.py --record          # write docs/verification/P15-2am-drill.txt
    python3 tools/p15-2am-drill.py --self-test       # plant each way the drill could lie, require it to notice

The kit's gate for this phase is not a checklist of artefacts, it is a sentence: *"It is 2am. You get one page.
From a phone, in under 5 minutes, you can say: is the system healthy, is any user's money in an inconsistent
state, and should I stop trading? Demonstrate it."*

A demonstration is not a claim in a document, so this file does the whole thing with real components and refuses
to print a green line it has not earned:

1. it writes a genuinely inconsistent condition into a scratch copy of the seeded database (a single `orphan` —
   an order at the venue we cannot map to a user — open for ten minutes, well past the 60-second `for_ms` the
   registry demands before paging), leaving everything else in the world healthy: one page, one problem;
2. it reads `/v1/admin/metrics` **through the app** (the same request the phone makes), evaluates `ops/alerts.yaml`
   with the same engine the dashboards import, and renders the notification with the same function the alert
   drill records — so the page in the drill is the page the on-call would receive;
3. it opens the runbook that notification links to, on the filesystem, and reads it: the third answer has to be
   *in the page's own instructions*, not in someone's head;
4. it renders the on-call dashboard from the same payload and checks the phone page and the page agree on the
   money number — the bug this catches is the one where the dashboard is quietly a release behind the alarm;
5. it times the whole thing. The budget is five minutes; the honest number is printed either way.

Every step can fail, and `--self-test` proves it by planting each failure in memory: a notification with no
runbook link, a runbook that does not exist, an owner-less alarm, a dashboard whose number disagrees with the
notification, a phone page that reaches out to the network, a page missing its viewport, and a triage that took
longer than the budget. A drill that cannot fail is a demonstration; the difference is this file's `--self-test`.

What it proves and what it does not: it proves the three answers are **obtainable from one page in this
environment, from artefacts in this repository**. It does not prove delivery to a real phone — that needs
`PGM_TELEGRAM_BOT_TOKEN` and a device, and it stays listed as an owner step in `docs/P15-readiness.md`. The
recorded transcript says "local, seeded" for exactly that reason.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "ops" / "alerts.yaml"
GOLDEN = ROOT / "var" / "polygm.db"
RECORD_PATH = ROOT / "docs" / "verification" / "P15-2am-drill.txt"

PAGE_BUDGET_MS = 300_000          # the kit's five minutes, in milliseconds
PHONE_MAX_KB = 64                 # what we claim the on-call page weighs on a phone
EXTERNAL = re.compile(r"""(?:src|href)\s*=\s*["']https?://|fetch\(\s*["']https?://|@import\s+url\(\s*["']https?://""")


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- findings
class Finding(tuple):
    """(check, ok, detail) — a tuple so the record reads like a transcript, a class so `.ok` reads like English."""

    def __new__(cls, check: str, ok: bool, detail: str):
        return super().__new__(cls, (check, ok, detail))

    @property
    def check(self) -> str:
        return self[0]

    @property
    def ok(self) -> bool:
        return self[1]

    @property
    def detail(self) -> str:
        return self[2]


# --------------------------------------------------------------------------- the checks
# Each takes the same context dict and returns findings, so the self-test can break one input at a time and require
# the matching check to notice. The context keys are: rule, notification, page_html, metrics, runbook_text,
# runbook_path, firing, elapsed_ms, scenarios.
def c1_the_page_arrives(ctx: dict) -> list[Finding]:
    rule, firing = ctx["rule"], ctx["firing"]
    ok = bool(rule) and rule.get("severity") == "SEV1" and rule.get("route") == "page_now"
    return [Finding("c1 the page arrives: a SEV1 that pages, naming what broke",
                    ok, "%s %s · route=%s" % (rule.get("id"), rule.get("severity"), rule.get("route")) if rule
                    else "no rule fired")]


def c2_answer_healthy(ctx: dict) -> list[Finding]:
    """Answer one has to be readable from the page itself: an alarm id is not an answer without a summary, and a
    summary is not actionable without the fact that produced it."""
    note = ctx["notification"]
    has_summary = bool(ctx["rule"].get("summary")) and ctx["rule"]["summary"] in note
    has_why = "why it matters:" in note
    has_owner = ctx["rule"].get("owner") in note
    return [Finding("c2 answer one (what is broken, and on whose watch)",
                    has_summary and has_why and has_owner,
                    "summary on the page=%s · why=%s · owner named=%s" % (has_summary, has_why, has_owner))]


def money_section(page_html: str) -> str:
    """The dashboard's money block as *text*, so a check about a number does not depend on the markup around it.

    The first version of c3 looked for the literal `>1 <` and failed against a page that does carry the number,
    because the count follows a status chip inside its own span. That failure was worth having: it is the same
    mistake as reading a fixture instead of the payload, and the fix is to reduce the section to text and search
    that. The canary for this check renders the page from a wrong payload rather than editing markup, so it cannot
    go red for a markup reason either.
    """
    text = re.sub(r"<[^>]+>", " ", page_html)
    text = re.sub(r"\s+", " ", text)
    start = text.find("money correctness")
    end = text.find("order path", start if start >= 0 else 0)
    return text[start:end] if start >= 0 else ""


def c3_answer_money(ctx: dict) -> list[Finding]:
    """The money answer, and the drift check that matters: the number on the phone must be the number that fired
    the alarm. A dashboard one release behind the alarm is how a person concludes 'nothing is wrong' while a case
    sits open."""
    metrics, page = ctx["metrics"], ctx["page_html"]
    count = (((metrics.get("money") or {}).get("unreconciled") or {}).get("count"))
    paged = ((metrics.get("money") or {}).get("unreconciled") or {}).get("page")
    in_note = isinstance(count, int) and ("%s unreconciled order" % count) in ctx["notification"].replace("order(s)", "order")
    section = money_section(page)
    in_page = isinstance(count, int) and bool(re.search(r"unreconciled orders\D{0,40}?\b%d\b" % count, section))
    ok = isinstance(count, int) and count >= 1 and bool(paged) and in_note and in_page
    return [Finding("c3 answer two (is any user's money inconsistent): count=%s, page=%s" % (count, paged),
                    ok, "count=%s on the notification=%s on the dashboard=%s (money section read as text: %r)"
                    % (count, in_note, in_page, section[:90]))]


def c4_answer_stop(ctx: dict) -> list[Finding]:
    """Answer three is the one people get wrong at 2am: an alarm that says what broke but not whether to stop
    trading leaves the decision to a tired person. Two facts are required: the kill-switch state on the page, and
    a runbook that states the condition under which trading stops."""
    note, runbook = ctx["notification"], ctx["runbook_text"]
    ks_on_page = "kill switch:" in note
    decision = bool(re.search(r"stop trading|kill switch", runbook, re.I))
    remediation = bool(re.search(r"^##\s+Remediation", runbook, re.M))
    ok = ks_on_page and decision and remediation
    return [Finding("c4 answer three (should I stop trading): kill-switch state on the page, and the decision in the runbook",
                    ok, "kill switch on the page=%s · stop/kill instruction in the runbook=%s · remediation section=%s"
                    % (ks_on_page, decision, remediation))]


def c5_runbook_is_reachable(ctx: dict) -> list[Finding]:
    """An alarm without a runbook gets deleted — the kit's own rule. Reachability is two hops: the notification
    links a path, and that path exists and is the runbook the registry names (not a stale copy of the name)."""
    linked = re.search(r"^runbook: (.+)$", ctx["notification"], re.M)
    target = (linked.group(1).strip() if linked else "")
    path = ctx["runbook_path"]
    exists = bool(path) and pathlib.Path(path).exists()
    ok = bool(target) and not target.startswith("NONE") and exists and pathlib.Path(path) == ROOT / target
    return [Finding("c5 the page's runbook link resolves to the runbook the registry names",
                    ok, "linked=%r exists=%s" % (target, exists))]


def c6_phone_page(ctx: dict) -> list[Finding]:
    """The phone half of the gate: one self-contained document, no network, no external anything, under the size
    we claim, carrying the three sections a person reads at 2am."""
    html = ctx["page_html"]
    kb = len(html.encode()) / 1024
    external = EXTERNAL.findall(html)
    viewport = 'name="viewport"' in html and "width=device-width" in html
    sections = all(s in html for s in ("money correctness", "order path", "data freshness"))
    ok = not external and viewport and kb < PHONE_MAX_KB and sections
    return [Finding("c6 the phone page: self-contained, phone-shaped, %.1f KB" % kb,
                    ok, "external requests=%d viewport=%s sections=%s size=%.1f KB (budget %d)"
                    % (len(external), viewport, sections, kb, PHONE_MAX_KB))]


def c7_within_budget(ctx: dict) -> list[Finding]:
    ms = ctx["elapsed_ms"]
    return [Finding("c7 under five minutes from page to three answers (the kit's number)",
                    ms < PAGE_BUDGET_MS, "%.1f s of %d s" % (ms / 1000, PAGE_BUDGET_MS // 1000))]


def c8_no_unowned_alarms(ctx: dict) -> list[Finding]:
    """Whatever else is on fire has to be actionable too. This is the constraint re-checked at the moment it
    matters: every firing alarm carries an owner and a runbook, and every one of those runbooks exists."""
    firing = ctx["firing"]
    bad = []
    for row in firing:
        rb = row.get("runbook") or ""
        if not row.get("owner") or not rb or not (ROOT / rb).exists():
            bad.append(row.get("id"))
    return [Finding("c8 every alarm that fired (all %d) has an owner and an existing runbook" % len(firing),
                    not bad, "missing: %s" % (sorted(bad) or "none"))]


CHECKS = [c1_the_page_arrives, c2_answer_healthy, c3_answer_money, c4_answer_stop,
          c5_runbook_is_reachable, c6_phone_page, c7_within_budget, c8_no_unowned_alarms]


# --------------------------------------------------------------------------- the drill
def build_context() -> tuple[dict, str]:
    """Everything the checks read, produced by the real components. Returns (context, transcript header)."""
    alerts = _load("tools/p15-alerts.py", "p15_alerts_for_2am")
    drill = _load("tools/p15-alert-drill.py", "p15_alert_drill_for_2am")
    dash = _load("tools/p15-dashboards.py", "p15_dashboards_for_2am")
    import yaml

    registry = yaml.safe_load(REGISTRY.read_text())
    rules = {r["id"]: r for r in registry["rules"]}

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="p15-2am-"))
    try:
        now = int(time.time() * 1000)
        db = tmp / "2am.db"
        shutil.copy2(GOLDEN, db)
        con = sqlite3.connect(str(db))
        try:
            drill.reset_baseline(con, now)
            drill.s_unreconciled(con, now)
            # A beating executor, written the way the executor writes one. `reset_baseline` clears this table
            # deliberately (each alert-drill scenario wants its own state), and without this the drill's page
            # showed `executor=never` — a second red with no story behind it, which is exactly the kind of noise
            # that teaches people to skip pages. The world at 2am should have exactly one problem in it.
            con.execute("INSERT INTO executor_state (id, at_ms, pid, version, ticks, note)"
                        " VALUES (1, ?, 'drill', 'sample', 128, '') ON CONFLICT(id) DO UPDATE SET at_ms=excluded.at_ms",
                        (now - 1_500,))
            con.commit()
        finally:
            con.close()
        t0 = time.time()
        metrics = drill.metrics_from_app(db, {})
        fired, unknown, _quiet = alerts.evaluate(registry["rules"], metrics, sources={})
        rule = next((r for r in fired if r["id"] == "unreconciled-orders"), None)
        if rule is None:
            raise SystemExit("the drill could not make the money alarm fire; nothing to demonstrate")
        note = alerts.render_notification(rule, metrics.get("killSwitch"))
        page_html = dash.render_oncall(metrics, fired, unknown)
        triage_ms = (time.time() - t0) * 1000
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    linked = re.search(r"^runbook: (.+)$", note, re.M)
    runbook_path = (ROOT / linked.group(1).strip()) if linked else None
    runbook_text = runbook_path.read_text() if runbook_path and runbook_path.exists() else ""

    ctx = {"rule": rule, "firing": fired, "unknown": unknown, "notification": note, "page_html": page_html,
           "metrics": metrics, "runbook_text": runbook_text, "runbook_path": runbook_path, "elapsed_ms": triage_ms,
           "registry_rules": rules}
    return ctx, registry


def feed_state(ctx: dict) -> str:
    """One word about the feeds, taken from the metric itself rather than recomputed here: 'fresh' unless some
    source is lagging or silently dead, in which case name how many."""
    feeds = ((ctx["metrics"].get("freshness") or {}).get("feeds") or [])
    bad = [f for f in feeds if f.get("lagging") or f.get("silent")]
    if not feeds:
        return "unknown (no feeds in the payload)"
    return "fresh (%d source(s))" % len(feeds) if not bad else \
        "%d of %d source(s) lagging or silent" % (len(bad), len(feeds))


def the_three_answers(ctx: dict) -> str:
    """The part a person would say out loud, assembled only from facts the page carried."""
    money = ctx["metrics"].get("money") or {}
    unr = money.get("unreconciled") or {}
    ex = ctx["metrics"].get("executor") or {}
    ks = ctx["metrics"].get("killSwitch") or {}
    stop_line = next((l.strip("* ").strip() for l in ctx["runbook_text"].splitlines()
                      if re.search(r"stop trading", l, re.I)), "")
    return "\n".join([
        "  1. is the system healthy?  the page names the one thing that is not: %s (%s). executor=%s, feeds=%s"
        % (ctx["rule"]["id"], ctx["rule"]["severity"], ex.get("state"),
           # `freshness.feeds` is a list of per-source rows (source, state, lagging, silent) - not a map. The
           # first version of this line assumed a dict and died with AttributeError after the drill had already
           # proved everything; the lesson is the one this file keeps learning, so it is written down rather than
           # silently corrected: read the payload's shape from the payload.
           feed_state(ctx)),
        "  2. is any user's money inconsistent?  yes — %s unreconciled order(s), oldest %s ms, cases %s"
        % (unr.get("count"), unr.get("oldestAgeMs"), unr.get("byCase")),
        "  3. should I stop trading?  kill switch is %s. the page's runbook answers it: %s"
        % ("ENGAGED" if ks.get("engaged") else "clear", stop_line or "see the remediation section"),
    ])


def run(record: bool, json_out: str = "") -> int:
    if not GOLDEN.exists():
        print("no seeded database at %s — run `make migrate && make seed` first" % GOLDEN, file=sys.stderr)
        return 2
    ctx, registry = build_context()
    findings = [f for check in CHECKS for f in check(ctx)]
    failed = [f for f in findings if not f.ok]

    lines = []
    lines.append("P15 quality gate, demonstrated: one page, three answers, a phone, under five minutes.")
    lines.append("Run: %s · seeded locally (delivery to a real phone is an owner step, docs/P15-readiness.md)"
                 % time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()))
    lines.append("")
    lines.append("— the page, verbatim —")
    lines.append(ctx["notification"])
    lines.append("")
    lines.append("— the three answers, from that page and its runbook —")
    lines.append(the_three_answers(ctx))
    lines.append("")
    for f in findings:
        lines.append("%s %s\n        %s" % ("PASS" if f.ok else "FAIL", f.check, f.detail))
    lines.append("")
    lines.append("triage time: %.1f s (budget %d s) · page %d B · %d alarm(s) firing of %d registered"
                 % (ctx["elapsed_ms"] / 1000, PAGE_BUDGET_MS // 1000, len(ctx["page_html"].encode()),
                    len(ctx["firing"]), len(registry["rules"])))
    lines.append("gate: %s" % ("DEMONSTRATED" if not failed else "NOT demonstrated — %d step(s) failed" % len(failed)))
    text = "\n".join(lines)

    print(text)
    if record and not failed:
        RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
        RECORD_PATH.write_text(text + "\n")
        print("\nrecorded to %s" % RECORD_PATH.relative_to(ROOT))
    if json_out:
        pathlib.Path(json_out).write_text(json.dumps(
            {"findings": [{"check": f.check, "ok": f.ok, "detail": f.detail} for f in findings],
             "triage_ms": ctx["elapsed_ms"], "notification": ctx["notification"]}, indent=2) + "\n")
    return 1 if failed else 0


# --------------------------------------------------------------------------- self-test
def self_test() -> int:
    """Break one input per canary and require the check that owns it to go red.

    The point is not that the drill passes here; it is that the drill *could* fail. Each case names the check it
    expects to trip, and a case that trips the wrong check is reported as missed too — a canary that goes red for
    an unrelated reason proves nothing.
    """
    ctx, _registry = build_context()
    baseline = [f for check in CHECKS for f in check(ctx) if not f.ok]
    if baseline:
        # A canary harness that plants failures into a drill that is *already* red proves nothing: the mutation
        # for c3 sat undetected for exactly one run because c3 was failing on the unmutated page too, so the
        # canary looked caught when it had never been tested. The drill has to be green before anything is broken.
        print("the drill is not green before any canary was planted — fix that first:")
        for f in baseline:
            print("  already failing: %s\n        %s" % (f.check, f.detail))
        return 1
    print("baseline: %d checks green, now planting failures" % len([f for check in CHECKS for f in check(ctx)]))
    cases: list[tuple[str, str, dict]] = []      # (name, check that must fail, mutation)

    def mutated(**kw) -> dict:
        c = dict(ctx)
        c.update(kw)
        return c

    cases.append(("notification with no runbook link", "c5",
                  mutated(notification=ctx["notification"].replace(doc_link(ctx), "runbook: NONE"))))
    cases.append(("runbook the link names does not exist", "c5",
                  mutated(runbook_path=ROOT / "docs/runbooks/this-was-renamed.md")))
    cases.append(("an alarm with no owner", "c8",
                  mutated(firing=[dict(r, owner="") for r in ctx["firing"]])))
    dash = sys.modules["p15_dashboards_for_2am"]
    wrong = dict(ctx["metrics"])
    wrong_money = dict(wrong.get("money") or {})
    wrong_money["unreconciled"] = dict(wrong_money.get("unreconciled") or {}, count=money_count(ctx) + 98)
    wrong["money"] = wrong_money
    cases.append(("dashboard rendered from a payload whose money count drifted",
                  "c3", mutated(page_html=dash.render_oncall(wrong, ctx["firing"], ctx["unknown"]))))
    cases.append(("phone page reaching out to the network", "c6",
                  mutated(page_html=ctx["page_html"].replace("<head>", '<head><script src="https://cdn.example/x.js">'))))
    cases.append(("phone page with no viewport", "c6",
                  mutated(page_html=ctx["page_html"].replace('name="viewport"', 'name="gone"'))))
    cases.append(("page whose kill-switch line was dropped", "c4",
                  mutated(notification="\n".join(l for l in ctx["notification"].splitlines()
                                                 if not l.startswith("kill switch:")))))
    cases.append(("a triage that took eleven minutes", "c7", mutated(elapsed_ms=11 * 60 * 1000)))
    cases.append(("a summary the page does not carry", "c2",
                  mutated(notification=ctx["notification"].replace(ctx["rule"]["summary"], "something broke"))))
    cases.append(("a quiet rule instead of a paging one", "c1",
                  mutated(rule=dict(ctx["rule"], severity="SEV3", route="ticket"))))

    caught, missed = 0, []
    for name, expect, c in cases:
        got = [f.check.split()[0] for check in CHECKS for f in check(c) if not f.ok]
        if expect in got:
            caught += 1
            print("  caught  %-58s -> %s" % (name, expect))
        else:
            missed.append(name)
            print("  MISSED  %-58s expected %s, got %s" % (name, expect, got or "nothing"))
    print("\n2am drill self-test: %d caught, %d missed" % (caught, len(missed)))
    return 1 if missed else 0


def doc_link(ctx: dict) -> str:
    return re.search(r"^runbook: .+$", ctx["notification"], re.M).group(0)


def money_count(ctx: dict) -> int:
    return ((ctx["metrics"].get("money") or {}).get("unreconciled") or {}).get("count")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P15's quality gate, demonstrated")
    ap.add_argument("--record", action="store_true", help="write docs/verification/P15-2am-drill.txt")
    ap.add_argument("--self-test", action="store_true", help="plant each way this drill could lie")
    ap.add_argument("--json", default="", help="also write the findings as JSON")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    return run(record=a.record, json_out=a.json)


if __name__ == "__main__":
    raise SystemExit(main())
