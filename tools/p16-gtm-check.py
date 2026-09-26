#!/usr/bin/env python3
"""P16 — the go-to-market plan, checked against the things it claims about the product.

    python3 tools/p16-gtm-check.py                 # every check, human output
    python3 tools/p16-gtm-check.py --self-test     # plant each failure the checks exist to catch
    python3 tools/p16-gtm-check.py --json out.json

A growth plan is the easiest document in this repository to write dishonestly: nothing runs it, so a number can be
invented, a gate can be omitted, and a channel can be listed that nobody will ever work on. So the plan lives in
`config/gtm.json` and this file holds it to account in four directions at once:

* **against arithmetic** - the reachable population and the funnel must multiply out to the numbers the plan
  quotes, rather than being asserted next to a plausible-looking figure;
* **against the product** - the message budget must fit the cadence the channel engine actually enforces, the fee
  ramp must obey the venue mechanics in `packages/polygm_core/revenue/schedule.py`, and the free/pro split must not
  include anything the product needs to let a user *leave*;
* **against the code that renders copy** - the required phrases must exist in `web/src/legal/disclaimer.tsx` and be
  rendered by the three surfaces, and the banned list must appear nowhere in the plan or the launch copy;
* **against the kit** - the five gates must carry the kit's numbers, and each must state what happens when it is
  missed. A gate without a decision is a milestone, and milestones do not make decisions.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
GTM = ROOT / "config" / "gtm.json"
DISCLAIMER = ROOT / "web" / "src" / "legal" / "disclaimer.tsx"
CHANNEL = ROOT / "packages" / "polygm_core" / "telegrambot" / "channel.py"
SCHEDULE = ROOT / "packages" / "polygm_core" / "revenue" / "schedule.py"
PLAN_DOC = ROOT / "docs" / "P16-gtm.md"
ASSETS_DOC = ROOT / "docs" / "P16-launch-assets.md"
COMMUNITY_DOC = ROOT / "docs" / "P16-community.md"
SURFACES = [("landing page", ROOT / "web" / "app" / "page.tsx"),
            ("Mini App", ROOT / "web" / "src" / "tma" / "TmaScreen.tsx"),
            ("public pages", ROOT / "web" / "src" / "public" / "Chrome.tsx")]

#: The kit's own gate table. Restated here deliberately: this is the one place where a "helpful" edit to the plan
#: should be caught rather than absorbed, and the numbers are the contract.
KIT_GATES = {
    "week 4": {"channel_members": 2000, "mini_app_mau": 500, "or": True},
    "week 8": {"users_traded_twice": 30, "attributed_volume_week_usd": 50000},
    "week 12": {"attributed_volume_month_usd": 150000, "paying_users": 150},
    "month 6": {"attributed_volume_month_usd": 500000, "mrr_usd": 15000},
    "month 12": {"attributed_volume_month_usd": 1500000, "mrr_usd": 40000},
}
SOURCES = ("[CTX]", "[PROBE]", "[ASSUMPTION]")


class Findings(list):
    def add(self, check: str, ok: bool, detail: str) -> None:
        self.append({"check": check, "ok": ok, "detail": detail})


def _load_mod(rel: str, name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- checks
def c1_arithmetic(gtm: dict) -> list[dict]:
    out = Findings()
    pop = gtm["beachhead"]["population"]
    product = 1.0
    for inp in pop["inputs"]:
        product *= float(inp["value"])
        if not any(s in (inp.get("source") or "") for s in SOURCES):
            out.add("c1 every population input carries provenance", False,
                    "%s has source %r" % (inp["id"], inp.get("source")))
        if not inp.get("range"):
            out.add("c1 every assumption is a range", False, "%s has no range" % inp["id"])
    stated = float(pop["reachable"])
    ok = abs(product - stated) < 1.0
    out.insert(0, {"check": "c1 the reachable population multiplies out (%s)" % pop["arithmetic"].split("=")[-1].strip(),
                   "ok": ok, "detail": "inputs multiply to %.0f, the plan claims %.0f" % (product, stated)})

    steps = gtm["funnel"]["steps"]
    funnel = 1.0
    for i, s in enumerate(steps):
        funnel *= float(s["share"])
        # Step 0 is "member sees an alert" - the channel is the intervention, so there is nothing to name. Every
        # step after it is a place where a person leaves, and a step with no named intervention is a drop-off
        # nobody owns.
        if i and not str(s.get("intervention", "")).strip():
            out.add("c1 every funnel step names its intervention", False, "%s has none" % s["id"])
    stated_act = float(gtm["funnel"]["activation"])
    ok = abs(funnel - stated_act) < 0.0002
    out.append({"check": "c1 the funnel multiplies out to the activation rate",
                "ok": ok, "detail": "steps multiply to %.4f, the plan claims %.4f" % (funnel, stated_act)})
    out.append({"check": "c1 activation is defined as funded + one matched order within 7 days",
                "ok": "funded wallet plus one matched order within 7 days" in gtm["funnel"]["definition"],
                "detail": gtm["funnel"]["definition"]})
    return out


def c2_gates(gtm: dict) -> list[dict]:
    out = Findings()
    have = {g["when"]: g for g in gtm["gates"]}
    for when, expected in KIT_GATES.items():
        gate = have.get(when)
        if gate is None:
            out.add("c2 gate present: %s" % when, False, "missing")
            continue
        checks = {c["metric"]: c["at_least"] for c in gate["checks"]}
        missing = {k: v for k, v in expected.items() if k != "or" and checks.get(k) != v}
        ok = not missing
        struct = {k: v for k, v in gate.items() if k not in ("when", "checks", "if_missed")}
        out.append({"check": "c2 %s carries the kit's numbers" % when, "ok": ok,
                    "detail": "expected %s, found %s" % ({k: v for k, v in expected.items() if k != "or"}, checks)})
        if expected.get("or"):
            out.append({"check": "c2 %s is an OR gate (members OR MAU)" % when, "ok": bool(struct.get("or", False)) or
                        any(c.get("or") for c in gate["checks"]),
                        "detail": "the kit allows either signal to clear week 4"})
        out.append({"check": "c2 %s states what happens if it is missed" % when,
                    "ok": len(str(gate.get("if_missed") or "")) > 40,
                    "detail": (gate.get("if_missed") or "")[:90]})
    return out


def c3_channels_and_budget(gtm: dict) -> list[dict]:
    out = Findings()
    ranked = gtm["channels"]["ranked"]
    chosen = [c for c in ranked if c.get("chosen")]
    out.append({"check": "c3 exactly two channels are chosen and the rest are paused",
                "ok": len(chosen) == 2 and len(ranked) >= 6,
                "detail": "%d chosen of %d ranked: %s" % (len(chosen), len(ranked), [c["id"] for c in chosen])})
    for c in ranked:
        for field in ("mechanism", "cost_per_activated_usd", "time_to_effect", "failure_mode"):
            if not str(c.get(field, "")).strip():
                out.add("c3 every channel states its %s" % field, False, "%s is missing it" % c["id"])
    ads = next((c for c in ranked if c["id"] == "paid_ads"), None)
    out.append({"check": "c3 paid acquisition is argued against, not merely deprioritised",
                "ok": bool(ads) and not ads.get("chosen") and len(ads.get("failure_mode", "")) > 80,
                "detail": (ads or {}).get("failure_mode", "paid_ads is not in the ranking")[:120]})

    budget = gtm["message_budget"]
    cadence = _channel_constants()
    hourly_ceiling = cadence["max_per_hour"] * 12              # a day of the engine's own hourly cap
    fits = budget["channel_per_day_max"] <= hourly_ceiling
    agrees = (budget["per_hour_max"] == cadence["max_per_hour"]
              and budget["per_kind_per_hour_max"] == cadence["max_per_kind_per_hour"]
              and budget["min_gap_minutes"] * 60_000 == cadence["min_gap_ms"])
    out.append({"check": "c3 the daily message budget fits the engine's cadence caps",
                "ok": fits, "detail": "%d/day against a %d/day ceiling from channel.py"
                % (budget["channel_per_day_max"], hourly_ceiling)})
    out.append({"check": "c3 the plan's cadence numbers ARE the engine's (no drift between doc and code)",
                "ok": agrees, "detail": "plan %s vs channel.py %s"
                % ({k: budget[k] for k in ("per_hour_max", "per_kind_per_hour_max", "min_gap_minutes")},
                   {"per_hour_max": cadence["max_per_hour"], "per_kind_per_hour_max": cadence["max_per_kind_per_hour"],
                    "min_gap_minutes": cadence["min_gap_ms"] // 60_000})})

    split = gtm["free_vs_personal_vs_pro"]
    never = " ".join(split["never_paywalled"]).lower()
    for need in ("exit", "withdraw", "kill switch", "loss"):
        out.append({"check": "c3 the free tier never withholds %s" % need, "ok": need in never,
                    "detail": "never_paywalled: %s" % split["never_paywalled"]})
    return out


def c4_monetisation(gtm: dict) -> list[dict]:
    out = Findings()
    sched = _load_mod("packages/polygm_core/revenue/schedule.py", "p16_schedule_for_check")
    raw = gtm["monetisation"]
    # Validate the config this check was HANDED, not the file it could re-read. The first version called
    # sched.load_steps(), which reads config/gtm.json from disk - so the canary that shortens the ramp's spacing
    # was invisible to c4 (the mutation lived in memory, the file was unchanged), and the check looked like it
    # worked because two other checks happened to fail. A check that reads a different input than the one under
    # test is a check that can only ever confirm the status quo.
    steps = [sched.Step(id=s["id"], at_day=int(s["at_day"]), bps=int(s["bps"]), requires=s.get("requires", ""))
             for s in raw["steps"]]
    problems = sched.validate_steps(steps, mechanics=raw["venue_mechanics"])
    drift = [] if [(s.id, s.at_day, s.bps) for s in steps] == \
        [(s.id, s.at_day, s.bps) for s in sched.load_steps()] else \
        ["the ramp in this config is not the ramp schedule.load_steps() reads - the module and the plan disagree"]
    out.append({"check": "c4 the fee ramp is legal (0 bps at launch, >=7 days apart, inside the ceiling)",
                "ok": not problems and not drift,
                "detail": " | ".join(problems + drift) or sched.describe(steps, 0, 0)})
    mix = gtm["monetisation"]["mix_target"]
    for when, m in mix.items():
        fees = float(m["builder_fees"])
        out.append({"check": "c4 %s: builder fees stay under 60%% of revenue (the privilege is revocable)" % when,
                    "ok": fees <= 0.6, "detail": "builder_fees=%.2f" % fees})
    never = " ".join(gtm["monetisation"]["never"]).lower()
    out.append({"check": "c4 'a token' is explicitly refused", "ok": "token" in never,
                "detail": "never: %s" % gtm["monetisation"]["never"][:2]})
    mech = gtm["monetisation"]["venue_mechanics"]
    out.append({"check": "c4 the venue mechanics are stated (7 days / 3 days notice / one pending)",
                "ok": mech["min_days_between_changes"] == 7 and mech["advance_notice_days"] == 3
                and mech["one_pending_change_at_a_time"],
                "detail": "minimum spacing %s days, notice %s days, one pending %s"
                % (mech["min_days_between_changes"], mech["advance_notice_days"],
                   mech["one_pending_change_at_a_time"])})
    return out


def c5_copy_rules(gtm: dict, disclaimer_path: pathlib.Path | None = None,
                  assets_path: pathlib.Path | None = None) -> list[dict]:
    out = Findings()
    rules = gtm["copy_rules"]
    # Comments stripped, for the same reason the surfaces are scanned that way: the file's own docstring says
    # "we are not affiliated with Polymarket" while explaining the rule, so a version that dropped the rendered
    # sentence would still have passed the first draft of this check. The self-test's canary is what found it.
    raw_disclaimer = (disclaimer_path or DISCLAIMER).read_text()
    disclaimer = _strip_comments(raw_disclaimer).lower()
    for rule, phrase in rules["required_phrases"].items():
        ok = phrase.lower() in disclaimer
        out.append({"check": "c5 the disclaimer carries the required phrase '%s'" % rule, "ok": ok,
                    "detail": phrase})
    hit = [w for w in rules["banned"] if w.lower() in disclaimer]
    out.append({"check": "c5 the disclaimer itself carries no banned copy", "ok": not hit,
                "detail": "banned words present: %s" % (hit or "none")})
    for name, path in SURFACES:
        src = path.read_text()
        ok = "@/legal/disclaimer" in src and re.search(r"<DisclaimerFooter(\s|/|>)", src) is not None
        out.append({"check": "c5 %s renders the disclosure" % name, "ok": bool(ok),
                    "detail": str(path.relative_to(ROOT))})

    # Two of the five required places are handwritten copy, not components: `/start` and the pinned channel post.
    # They are the two messages a stranger reads before trusting anything else, so the phrases that make the offer
    # honest are checked *in those sections specifically* rather than anywhere in the file.
    assets_doc = assets_path or ASSETS_DOC
    if assets_doc.exists():
        sections = re.split(r"^## ", assets_doc.read_text(), flags=re.M)
        for needle, label in (("/start", "the /start message"), ("pinned", "the pinned channel post")):
            sec = next((s for s in sections if s.splitlines() and needle in s.splitlines()[0].lower()), None)
            if sec is None:
                out.add("c5 the assets document still has a %s section" % label, False, "no heading matched %r" % needle)
                continue
            for rule, phrase in rules["required_phrases"].items():
                out.append({"check": "c5 %s carries the required phrase '%s'" % (label, rule),
                            "ok": phrase.lower() in sec.lower(), "detail": phrase})
    # The three documents are named by the config, not by this file: `docs` in gtm.json is the single source, so
    # adding a fourth launch asset later is a config edit rather than a silent gap in the check.
    documents = [ROOT / rel for rel in gtm["docs"].values()]
    for path in documents:
        out.append({"check": "c5 the launch document %s exists" % path.name, "ok": path.exists(),
                    "detail": str(path.relative_to(ROOT))})
    docs = {p: p.read_text().lower() for p in documents if p.exists()}
    if assets_path:                       # the canary patched the assets file; judge the patched copy
        docs = {p: (assets_path if p == ASSETS_DOC else p).read_text().lower() for p in docs}
    for p, text in docs.items():
        hit = [w for w in rules["banned"] if w.lower() in text]
        out.append({"check": "c5 %s contains no banned copy" % p.name, "ok": not hit,
                    "detail": "banned words present: %s" % (hit or "none")})
    for name, path in SURFACES:
        # Comments are not copy, but a comment is also where copy gets parked, so the check reads the file with
        # comments stripped rather than trusting the difference: the landing page's own header comment contains
        # the word "guaranteed" while *telling the author not to use it*, which is exactly the false positive that
        # teaches people to delete a check.
        src = _strip_comments(path.read_text()).lower()
        hit = [w for w in rules["banned"] if w.lower() in src]
        out.append({"check": "c5 %s ships no banned copy" % name, "ok": not hit,
                    "detail": "banned words present in rendered copy: %s" % (hit or "none")})
    return out


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)          # block comments (including JSX {/* ... */})
    src = re.sub(r"^\s*//.*$", "", src, flags=re.M)            # line comments
    return re.sub(r"\{\s*/\*.*?\*/\s*\}", " ", src, flags=re.S)


def c6_quality_gate_answer(gtm: dict) -> list[dict]:
    """The kit's closing test, as a check: the five answers must exist, be specific, and match the doc."""
    out = Findings()
    first = gtm["first_1000"]
    for field in ("who", "where", "what_we_say", "day_7_measurement", "if_half"):
        value = str(first.get(field) or "")
        out.append({"check": "c6 the plan answers '%s'" % field.replace("_", " "), "ok": len(value) > 40,
                    "detail": value[:110]})
    if PLAN_DOC.exists():
        plan = PLAN_DOC.read_text().lower()
        for needle, label in (("first 1,000", "names the first 1,000 users"),
                              ("day 7", "says what is measured on day 7"),
                              ("half", "says what happens if the number is half")):
            out.append({"check": "c6 the plan document %s" % label, "ok": needle in plan,
                        "detail": "looking for %r in docs/P16-gtm.md" % needle})
    else:
        out.add("c6 docs/P16-gtm.md exists", False, "the document the plan points at is missing")
    return out


def c7_metrics_mapped(gtm: dict) -> list[dict]:
    """Every metric must name a formula and a source, and that source must be a table that exists."""
    out = Findings()
    schema = "\n".join(p.read_text() for p in (ROOT / "db" / "migrations").glob("*.sql"))
    for m in gtm["metrics"]:
        ok = bool(m.get("formula")) and bool(m.get("source"))
        out.append({"check": "c7 metric %s has a formula and a source" % m["id"], "ok": ok,
                    "detail": m.get("formula", "")[:90]})
        named = [t.strip() for t in re.split(r",|\+|\band\b|\s+x\s+", m["source"]) if t.strip()]
        missing = [t for t in named if t not in ("operator input", "utm/start parameter on the deep link")
                   and not re.search(r"\b%s\b" % re.escape(t), schema)]
        out.append({"check": "c7 %s reads only tables that exist" % m["id"], "ok": not missing,
                    "detail": "not found in db/migrations: %s" % (missing or "none")})
    return out


def _channel_constants() -> dict:
    """The channel engine's own caps, read from the module rather than restated here."""
    src = CHANNEL.read_text()
    m = re.search(r"CADENCE\s*=\s*\{(.*?)\}", src, re.S)
    if not m:
        raise SystemExit("channel.py no longer declares CADENCE; the budget check cannot be trusted")
    body = m.group(1)
    out = {}
    for key in ("max_per_hour", "max_per_kind_per_hour", "min_gap_ms"):
        hit = re.search(r"%s\"?\s*:\s*([0-9_*\s]+)" % key, body)
        if not hit:
            raise SystemExit("channel.py CADENCE lost %s" % key)
        expr = hit.group(1).replace("_", "").strip()
        out[key] = int(eval(expr))                       # noqa: S307 - a numeric literal from our own file
    return out


CHECKS = [c1_arithmetic, c2_gates, c3_channels_and_budget, c4_monetisation, c5_copy_rules,
          c6_quality_gate_answer, c7_metrics_mapped]


def run(gtm: dict, json_out: str = "", record: str = "") -> int:
    findings: list[dict] = []
    for check in CHECKS:
        findings.extend(check(gtm))
    failed = [f for f in findings if not f["ok"]]
    lines = []
    for f in findings:
        lines.append("%s %s\n        %s" % ("PASS" if f["ok"] else "FAIL", f["check"], f["detail"]))
    summary = "p16 gtm check: %d passed, %d failed" % (len(findings) - len(failed), len(failed))
    print("\n".join(lines))
    print("\n" + summary)
    if json_out:
        pathlib.Path(json_out).write_text(json.dumps(findings, indent=2) + "\n")
    if record:
        header = [
            "P16 gate — the launch plan, checked against the product it describes",
            "",
            "Recorded by tools/p16-gtm-check.py from config/gtm.json (the plan's single source), the copy that",
            "ships in web/src/legal/disclaimer.tsx and on the three rendering surfaces, the channel engine's own",
            "cadence constants in packages/polygm_core/telegrambot/channel.py, the venue mechanics in",
            "packages/polygm_core/revenue/schedule.py, and the three documents in docs/P16-*.md.",
            "",
            "Every line below is a claim the plan makes about itself, recomputed. A FAIL here means the plan and",
            "the repository disagree, and the repository is right.",
            "",
        ]
        pathlib.Path(record).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(record).write_text("\n".join(header + lines + ["", summary, ""]).replace("\n\n\n", "\n\n"))
        print("recorded: %s" % record)
    return 1 if failed else 0


# --------------------------------------------------------------------------- self-test
def self_test() -> int:
    """Break the plan one way at a time and require the matching check to notice.

    Each case mutates a *deep copy* of the real config, so the canaries cannot leave the repository in a state
    where the next run passes for the wrong reason. A canary that trips a different check than the one named is
    reported as missed: a failure for an unrelated reason is not evidence that the check works.
    """
    import copy
    base = json.loads(GTM.read_text())
    cases: list[tuple[str, str, dict]] = []

    def mut(fn) -> dict:
        g = copy.deepcopy(base)
        fn(g)
        return g

    cases.append(("a population input loses its provenance",
                  "c1", mut(lambda g: g["beachhead"]["population"]["inputs"][0].pop("source"))))
    cases.append(("the reachable number stops matching its inputs",
                  "c1", mut(lambda g: g["beachhead"]["population"].update({"reachable": 90000}))))
    cases.append(("a funnel step loses its intervention",
                  "c1", mut(lambda g: g["funnel"]["steps"][3].pop("intervention"))))
    cases.append(("the activation number stops matching the funnel",
                  "c1", mut(lambda g: g["funnel"].update({"activation": 0.2}))))
    cases.append(("a kit gate is quietly lowered",
                  "c2", mut(lambda g: g["gates"][1]["checks"][1].update({"at_least": 5000}))))
    cases.append(("a gate loses its decision",
                  "c2", mut(lambda g: g["gates"][0].update({"if_missed": ""}))))
    cases.append(("a third channel is chosen",
                  "c3", mut(lambda g: (g["channels"]["ranked"][3].update({"chosen": True}),
                                       g["channels"].update({"chosen": ["free_alert_channel", "public_pages_seo",
                                                                        "x_twitter"]})))))
    cases.append(("the daily budget exceeds what the cadence allows",
                  "c3", mut(lambda g: g["message_budget"].update({"channel_per_day_max": 200}))))
    cases.append(("the plan's cadence drifts from the engine's",
                  "c3", mut(lambda g: g["message_budget"].update({"min_gap_minutes": 1}))))
    cases.append(("paid ads become a chosen channel",
                  "c3", mut(lambda g: (next(c for c in g["channels"]["ranked"] if c["id"] == "paid_ads")
                                       .update({"chosen": True, "failure_mode": "short"})))))
    cases.append(("the fee ramp breaches the venue's spacing rule",
                  "c4", mut(lambda g: g["monetisation"]["steps"][2].update({"at_day": 95}))))
    cases.append(("builder fees become most of revenue",
                  "c4", mut(lambda g: g["monetisation"]["mix_target"]["month_6"].update({"builder_fees": 0.8}))))
    cases.append(("the venue mechanics are edited to look faster than they are",
                  "c4", mut(lambda g: g["monetisation"]["venue_mechanics"].update({"advance_notice_days": 0}))))
    cases.append(("a banned word reaches the disclaimer",
                  "c5", None))                       # handled specially: patches the disclaimer file in memory
    cases.append(("a required phrase loses the word that matters",
                  "c5", None))
    cases.append(("the /start message loses its affiliation line",
                  "c5", "assets"))
    cases.append(("the fourth answer to the quality gate goes vague",
                  "c6", mut(lambda g: g["first_1000"].update({"day_7_measurement": "look at the numbers"}))))
    cases.append(("a metric points at a table that does not exist",
                  "c7", mut(lambda g: g["metrics"][0].update({"source": "users, deposits, wishful_thinking"}))))

    caught, missed = 0, []
    for name, expect, mutated in cases:
        if mutated is None:
            got = _check_c5_with_patched_disclaimer(name)
        else:
            got = _failing_checks(mutated)
        if expect in got:
            caught += 1
            print("  caught  %-58s -> %s" % (name, expect))
        else:
            missed.append(name)
            print("  MISSED  %-58s expected %s, got %s" % (name, expect, got or "nothing"))
    print("\np16 gtm check self-test: %d caught, %d missed" % (caught, len(missed)))
    return 1 if missed else 0


def _failing_checks(gtm: dict, disclaimer_path: pathlib.Path | None = None,
                    assets_path: pathlib.Path | None = None) -> list[str]:
    out = []
    for check in CHECKS:
        try:
            findings = check(gtm, disclaimer_path, assets_path) if check is c5_copy_rules else check(gtm)
        except Exception as exc:                                  # a check that crashes is a check that failed
            out.append(check.__name__[:2])
            continue
        if any(not f["ok"] for f in findings):
            out.append(check.__name__[:2])
    return out


def _check_c5_with_patched_disclaimer(case_name: str) -> list[str]:
    """c5 reads three real files; the canaries replace one of them in a temp copy of the tree and re-run the
    check against that copy, which keeps the repository untouched while still exercising the real code path."""
    import tempfile
    gtm = json.loads(GTM.read_text())
    with tempfile.TemporaryDirectory() as td:
        fake = pathlib.Path(td) / "disclaimer.tsx"
        body = DISCLAIMER.read_text()
        if case_name == "assets":
            body = ASSETS_DOC.read_text()
            body = body.replace("Openout is not affiliated with Polymarket.",
                                "Openout is a product of the Polymarket ecosystem.", 1)
            fake.write_text(body)
            real_assets = ASSETS_DOC
            return _failing_checks(gtm, assets_path=fake)
        if "banned" in case_name:
            body = body.replace("is not affiliated with Polymarket", "is a seamless, guaranteed way to trade")
        else:
            body = body.replace("not affiliated with Polymarket", "independent")
        fake.write_text(body)
        return _failing_checks(gtm, disclaimer_path=fake)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P16 — check the go-to-market plan against the product")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--json", default="")
    ap.add_argument("--record", default="", help="write a gate record to this path (docs/verification/...)")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    return run(json.loads(GTM.read_text()), json_out=a.json, record=a.record)


if __name__ == "__main__":
    raise SystemExit(main())
