"""The 2am drill's checks, tested without a seeded database.

`tools/p15-2am-drill.py --self-test` proves the drill can fail, but it needs `var/polygm.db` (it fires a real
alarm through the real app to get there). These tests cover the same logic hermetically: each check is a function
of a context dict, so the fixture here is built by hand and every assertion is about the check's reasoning rather
than about the machine it ran on.

Two of them exist because of bugs this drill actually had, and are the reason to keep this file honest:

* `test_money_section_reads_text_not_markup` — c3 first searched the raw HTML for `>1 <` and failed against a page
  that *does* carry the number, because the count follows a status chip inside its own span. A check that depends
  on the markup around a number is a check that reports its author's assumptions, not the product's state.
* `test_a_canary_against_a_red_baseline_is_not_a_pass` — the first version of the self-test planted its mutations
  into a context where c3 was already failing, so the c3 canary looked caught while it had never been exercised.
  The tool now refuses to plant anything until the baseline is green; this test pins that order.

The committed record is asserted too, because the drill's whole point is that the quality gate leaves evidence:
if someone deletes `docs/verification/P15-2am-drill.txt` or edits its verdict, this goes red.
"""
from __future__ import annotations
import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "verification" / "P15-2am-drill.txt"


def _load():
    spec = importlib.util.spec_from_file_location("p15_2am_for_tests", ROOT / "tools" / "p15-2am-drill.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["p15_2am_for_tests"] = mod
    spec.loader.exec_module(mod)
    return mod


DRILL = _load()

RULE = {"id": "unreconciled-orders", "severity": "SEV1", "route": "page_now", "owner": "money-path",
        "summary": "1 unreconciled order(s), oldest 10 min, cases {\"orphan\": 1}",
        "runbook": "docs/runbooks/unreconciled-orders.md",
        "why": "the single most important metric in the system: it is the only one whose meaning is \"a user's "
               "money may be inconsistent\""}


def notification() -> str:
    return "\n".join([
        "[SEV1] unreconciled-orders",
        RULE["summary"],
        "kill switch: clear",
        "owner: money-path",
        "why it matters: %s" % RULE["why"],
        "runbook: %s" % RULE["runbook"],
    ])


def metrics(count: int = 1) -> dict:
    return {
        "money": {"unreconciled": {"count": count, "page": count > 0, "oldestAgeMs": 600_652,
                                   "byCase": {"orphan": count}}},
        "executor": {"state": "live", "lastBeatAgeMs": 1500},
        "freshness": {"feeds": [{"source": "ws.tape", "lagging": False, "silent": False},
                                {"source": "gamma.markets", "lagging": False, "silent": False}]},
        "killSwitch": {"engaged": False, "reason": "", "engagedForMs": 0},
    }


def page_html(count: int = 1) -> str:
    """Shaped like the real on-call page where it matters: the money count sits inside its own span, after a chip,
    which is exactly the markup the first version of c3 could not read."""
    return ("<!doctype html><html><head><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "</head><body>"
            "<h2>1 · money correctness</h2><div class=\"panel\"><div class=\"row\">"
            "<span class=\"k\">unreconciled orders</span><span class=\"v\">"
            "<span class=\"chip bad\">bad</span> %d</span></div>"
            "<div class=\"row\"><span class=\"k\">unknown-state intents</span><span class=\"v\">0</span></div></div>"
            "<h2>2 · order path</h2><div class=\"panel\">executor live</div>"
            "<h2>3 · data freshness</h2><div class=\"panel\">feeds fresh</div>"
            "</body></html>" % count)


def context(**overrides) -> dict:
    ctx = {"rule": dict(RULE), "firing": [dict(RULE)], "unknown": [], "notification": notification(),
           "page_html": page_html(), "metrics": metrics(),
           "runbook_path": ROOT / RULE["runbook"],
           "runbook_text": (ROOT / RULE["runbook"]).read_text(), "elapsed_ms": 900.0}
    ctx.update(overrides)
    return ctx


def failed(ctx: dict) -> list[str]:
    return [f.check.split()[0] for check in DRILL.CHECKS for f in check(ctx) if not f.ok]


class TestTheDrillIsGreen(unittest.TestCase):
    def test_a_healthy_world_passes_every_check(self):
        self.assertEqual(failed(context()), [], "the fixture describes the 2am world the drill claims to handle")

    def test_the_number_of_checks_is_what_the_record_says(self):
        self.assertEqual(len(DRILL.CHECKS), 8)
        self.assertEqual(DRILL.PAGE_BUDGET_MS, 300_000, "the kit's budget is five minutes, not four or six")


class TestEachCheckCanFail(unittest.TestCase):
    """One targeted breakage per check. The mutation is always in the *input*, never in the check's source."""

    def assert_only(self, expect: str, **overrides):
        got = failed(context(**overrides))
        self.assertIn(expect, got, "expected %s to fail for %s" % (expect, overrides.keys()))

    def test_c1_a_ticket_where_a_page_was_promised(self):
        self.assert_only("c1", rule=dict(RULE, severity="SEV3", route="ticket"))

    def test_c2_a_page_that_does_not_carry_its_own_summary(self):
        self.assert_only("c2", notification=notification().replace(RULE["summary"], "something broke"))

    def test_c3_a_dashboard_that_disagrees_with_the_page(self):
        self.assert_only("c3", page_html=page_html(count=99))

    def test_c4_a_page_that_never_answers_should_i_stop(self):
        self.assert_only("c4", notification="\n".join(l for l in notification().splitlines()
                                                      if not l.startswith("kill switch:")))

    def test_c5_a_runbook_that_is_not_there(self):
        self.assert_only("c5", runbook_path=ROOT / "docs/runbooks/renamed-this-morning.md")

    def test_c6_a_phone_page_that_reaches_out(self):
        self.assert_only("c6", page_html=page_html().replace(
            "<head>", '<head><script src="https://cdn.example/tracker.js">'))

    def test_c6_a_phone_page_without_a_viewport(self):
        self.assert_only("c6", page_html=page_html().replace('name="viewport"', 'name="gone"'))

    def test_c7_a_triage_that_missed_the_budget(self):
        self.assert_only("c7", elapsed_ms=DRILL.PAGE_BUDGET_MS + 1)

    def test_c8_an_alarm_with_no_owner(self):
        self.assert_only("c8", firing=[dict(RULE, owner="")])

    def test_c8_an_alarms_runbook_having_been_deleted(self):
        self.assert_only("c8", firing=[dict(RULE, runbook="docs/runbooks/deleted.md")])


class TestTheBugsThatShapedTheseChecks(unittest.TestCase):
    def test_money_section_reads_text_not_markup(self):
        """The count nested inside a chip's sibling span must still be found: that markup is the real one."""
        section = DRILL.money_section(page_html())
        self.assertIn("money correctness", section)
        self.assertNotIn("order path", section, "the section must stop at the next heading")
        self.assertIn("unreconciled orders", section)
        self.assertNotIn(">1 <", page_html(), "the fixture must not contain the literal the old check searched for")
        self.assertEqual(failed(context()), [])

    def test_money_section_absent_is_a_failure_not_a_crash(self):
        self.assertIn("c3", failed(context(page_html="<html><body>nothing here</body></html>")))

    def test_a_runbook_must_actually_answer_the_third_question(self):
        prose = "# Unreconciled orders\n\n## Symptoms\n\nsome symptoms\n"
        got = failed(context(runbook_text=prose))
        self.assertIn("c4", got, "a runbook without a remediation section is not an answer")

    def test_a_canary_against_a_red_baseline_is_not_a_pass(self):
        """`--self-test` must refuse to plant into a red baseline; the guard is in the tool, so assert its
        behaviour by reading the source of the function that has to contain it. (Running it needs a seeded
        database; this is the part that does not.)"""
        import inspect
        src = inspect.getsource(DRILL.self_test)
        self.assertIn("the drill is not green before any canary was planted", src)
        self.assertLess(src.index("baseline = [f for check in CHECKS"), src.index("cases.append"),
                        "the baseline must be checked before the first canary is added")


class TestTheRecordedDemonstration(unittest.TestCase):
    def test_the_record_is_committed(self):
        self.assertTrue(RECORD.exists(), "P15's quality gate is demonstrated by this file; it must exist")

    def test_the_record_states_the_verdict_and_the_three_answers(self):
        text = RECORD.read_text()
        self.assertIn("gate: DEMONSTRATED", text)
        for n, question in enumerate(("is the system healthy?", "is any user's money inconsistent?",
                                      "should I stop trading?"), start=1):
            self.assertIn("%d. %s" % (n, question), text)

    def test_the_record_carries_the_page_and_its_runbook(self):
        text = RECORD.read_text()
        self.assertIn("[SEV1] unreconciled-orders", text, "the page itself, verbatim")
        self.assertIn("runbook: docs/runbooks/unreconciled-orders.md", text)
        self.assertIn("triage time:", text)

    def test_the_record_says_what_it_does_not_prove(self):
        self.assertIn("delivery to a real phone is an owner step", RECORD.read_text())


if __name__ == "__main__":
    unittest.main()
