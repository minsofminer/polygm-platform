"""P11 · D6's pure layer: the URLs, the indexability rules, the anonymous budget, the graph and the card.

The tests that matter here are the ones that stop a *public* surface from quietly becoming a leaky one:

  1. **The handle rule is the same rule D4 enforces.** Not "similar" — the same string, asserted against the
     regex the API compiles, because a handle that is valid on the settings screen and invalid in a URL (or the
     reverse) is a page that exists for a name that cannot be typed.
  2. **The graph cannot say anything the payload does not.** `unbacked()` is the check, and it is tested with a
     deliberately added field, because the failure mode is a number that appears in machine-readable data and on
     no screen.
  3. **A card cannot carry an address or an account id**, and it *does* carry the provisional label and the
     drawdown — a share card that drops either is the most damaging thing this deliverable could ship.
  4. **The budget refuses on the window's own arithmetic**, and crossing it from several kinds at once is what
     the cross-kind budget exists for.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT / "services" / "api"))

from polygm_core import public_pages as pp            # noqa: E402


class TestUrls(unittest.TestCase):
    def test_the_handle_rule_is_d4s_rule(self):
        src = (ROOT / "services" / "api" / "app.py").read_text()
        m = re.search(r'_LB_HANDLE_RX = re\.compile\(r"([^"]+)"\)', src)
        self.assertIsNotNone(m, "the API's handle regex moved; this test is the thing that keeps them equal")
        self.assertEqual(pp.urls.HANDLE_RX.pattern, m.group(1))

    def test_normalising(self):
        self.assertEqual(pp.urls.normalise_handle("  Surat_Whale ")[0], "surat_whale")
        for bad in ("", "ab", "1" * 30, "has space", "dash-here", "UPPER!"):
            self.assertEqual(pp.urls.normalise_handle(bad)[0], "", bad)
            self.assertTrue(pp.urls.normalise_handle(bad)[1])
        self.assertEqual(pp.urls.normalise_slug("will-btc-close-above-100k")[0], "will-btc-close-above-100k")
        for bad in ("", "../etc/passwd", "a/b", "x" * 200):
            self.assertEqual(pp.urls.normalise_slug(bad)[0], "", bad)

    def test_canonical_urls(self):
        base = "openout.app/"                       # a configured origin missing its scheme is repaired, not trusted
        self.assertEqual(pp.urls.page_url("trader", "Surat_Whale", base=base),
                         "https://openout.app/trader/surat_whale")
        self.assertEqual(pp.urls.page_url("market", "btc-100k", base=base),
                         "https://openout.app/market/btc-100k")
        self.assertEqual(pp.urls.page_url("leaderboard", "win_rate", base=base, window="7d"),
                         "https://openout.app/leaderboard/win_rate/w/7d")
        self.assertEqual(pp.urls.page_url("leaderboard", "category", base=base, category="Politics"),
                         "https://openout.app/leaderboard/category/c/politics")
        self.assertEqual(pp.urls.og_url("trader", "surat_whale", base=base),
                         "https://openout.app/trader/surat_whale/opengraph-image")
        with self.assertRaises(ValueError):
            pp.urls.page_url("trader", "no", base=base)
        with self.assertRaises(ValueError):
            pp.urls.page_url("nonsense", "x", base=base)

    def test_the_default_origin_is_the_brand_p02_chose(self):
        """A canonical tag is the one place a second brand name would be invisible and permanent: it is published
        to a crawler rather than shown to a reader. P02 chose Openout, the shell has shipped `openout.app` since
        P08, and this is the assertion that keeps the API's default from drifting back to the codename."""
        self.assertEqual(pp.urls.base_url(""), "https://openout.app")
        self.assertEqual(pp.cards.BRAND, "Openout")
        self.assertNotIn("polygm", pp.cards.FOOTER.lower())
        self.assertIn("openout.app", pp.cards.FOOTER)

    def test_only_a_ranked_settled_trader_is_indexable(self):
        self.assertEqual(pp.urls.robots_for(kind="market"), "index, follow")
        self.assertEqual(pp.urls.robots_for(kind="leaderboard", rows=40), "index, follow")
        self.assertEqual(pp.urls.robots_for(kind="leaderboard", rows=0), "noindex, follow")
        self.assertEqual(pp.urls.robots_for(kind="trader", ranked=True, age_days=30), "index, follow")
        self.assertEqual(pp.urls.robots_for(kind="trader", ranked=True, age_days=3), "noindex, follow")
        self.assertEqual(pp.urls.robots_for(kind="trader", ranked=False, age_days=300), "noindex, follow")

    def test_cache_lifetimes_are_bounded_by_the_payloads_freshness(self):
        for kind, (age, swr) in pp.urls.CACHE.items():
            self.assertGreater(age, 0, kind)
            self.assertGreater(swr, 0, kind)
        # the market page moves with the price, so it is the shortest of the three pages
        self.assertLess(pp.urls.CACHE["market"][0], pp.urls.CACHE["leaderboard"][0])
        self.assertLess(pp.urls.CACHE["leaderboard"][0], pp.urls.CACHE["trader"][0])
        self.assertEqual(pp.urls.cache_control("nonexistent"), "no-store")

    def test_etag_is_content_addressed(self):
        a = pp.urls.etag({"rows": [1, 2], "board": "volume"})
        b = pp.urls.etag({"board": "volume", "rows": [1, 2]})
        self.assertEqual(a, b, "key order must not change the validator")
        self.assertNotEqual(a, pp.urls.etag({"board": "volume", "rows": [1, 3]}))
        self.assertTrue(a.startswith('W/"'))


class TestBudget(unittest.TestCase):
    def test_under_the_limit_is_allowed_and_over_it_is_refused(self):
        limit = pp.budget.BUDGET["sitemap"][0]
        ok = pp.budget.decide_hits(kind="sitemap", hits=limit - 1, window_start_ms=1_000, at_ms=5_000)
        self.assertTrue(ok["allowed"])
        self.assertEqual(ok["remaining"], 1)
        no = pp.budget.decide_hits(kind="sitemap", hits=limit, window_start_ms=1_000, at_ms=5_000)
        self.assertFalse(no["allowed"])
        self.assertGreater(no["retryAfterMs"], 0)
        self.assertTrue(no["sentence"])

    def test_the_window_is_fixed_from_the_first_hit(self):
        limit, window_ms, _lock = pp.budget.BUDGET["sitemap"]
        start = 1_000
        ref = pp.budget.decide_hits(kind="sitemap", hits=limit, window_start_ms=start, at_ms=start + window_ms - 1)
        self.assertFalse(ref["allowed"], "inside the window the budget is spent")
        again = pp.budget.decide_hits(kind="sitemap", hits=limit, window_start_ms=start, at_ms=start + window_ms)
        self.assertTrue(again["allowed"], "past the window the counters are gone, not decayed")

    def test_the_cross_kind_budget_is_what_stops_a_loop_over_all_four(self):
        self.assertLess(pp.budget.TOTAL[0], sum(v[0] for v in pp.budget.BUDGET.values()))
        t = pp.budget.decide_total(hits=pp.budget.TOTAL[0], window_start_ms=1, at_ms=2)
        self.assertFalse(t["allowed"])

    def test_blocks_are_scoped_and_expire(self):
        blocks = [{"scope": "market", "reason": "scrape", "until_ms": 5_000},
                  {"scope": "all", "reason": "worse", "until_ms": 9_000}]
        live = pp.budget.block_for(blocks, scope="market", at_ms=1_000)
        self.assertTrue(live["blocked"])
        self.assertEqual(live["scope"], "all", "a block against everything outranks a per-kind one")
        self.assertEqual(pp.budget.block_for(blocks, scope="trader", at_ms=1_000)["scope"], "all")
        self.assertEqual(pp.budget.block_for(blocks, scope="market", at_ms=10_000), {}, "blocks expire")

    def test_auto_block_needs_a_caller_who_came_back(self):
        self.assertFalse(pp.budget.should_auto_block(findings=[], hits=5, limit=10)[0])
        self.assertTrue(pp.budget.should_auto_block(findings=[], hits=20, limit=10)[0])

    def test_the_subject_is_a_digest_and_the_salt_is_required(self):
        d = pp.budget.subject_hash("203.0.113.7", "0123456789abcdef")
        self.assertTrue(d.startswith("i_"))
        self.assertNotIn("203", d)
        with self.assertRaises(ValueError):
            pp.budget.subject_hash("203.0.113.7", "short")


class TestStructuredAndCards(unittest.TestCase):
    PAYLOAD = {"question": "Will BTC close above $100k?", "lastPrice": "$0.42", "volume24h": "$1,204",
               "category": "Crypto", "slug": "btc-100k"}

    def test_a_graph_string_with_no_backing_field_is_found(self):
        good = pp.structured.graph(pp.structured.breadcrumb([(pp.cards.BRAND, "https://openout.app"),
                                                            (self.PAYLOAD["question"], "https://x")]))
        self.assertEqual(pp.structured.unbacked(good, self.PAYLOAD), [])
        bad = pp.structured.graph({"@context": pp.structured.SCHEMA, "@type": "Event",
                                   "name": self.PAYLOAD["question"], "description": "$9,999,999 profit"})
        found = pp.structured.unbacked(bad, self.PAYLOAD)
        self.assertEqual(len(found), 1)
        self.assertIn("9,999,999", found[0])

    def test_addresses_and_emails_are_always_a_bug(self):
        g = [{"@type": "Person", "name": "0x" + "a" * 40}]
        self.assertTrue(pp.structured.leak_findings(g))
        g2 = [{"@type": "Person", "name": "reach me at trader@example.com"}]
        self.assertTrue(pp.structured.leak_findings(g2))
        self.assertEqual(pp.structured.leak_findings([{"@type": "Person", "name": "surat_whale"}]), [])

    def test_an_undated_market_is_not_an_event(self):
        self.assertEqual(pp.structured.market_event(name="x", url="u"), {})
        ev = pp.structured.market_event(name="x", url="u", end_date="2026-11-03T00:00:00Z", accepting=True)
        self.assertEqual(ev["@type"], "Event")
        self.assertTrue(ev["eventStatus"].endswith("EventScheduled"))
        closed = pp.structured.market_event(name="x", url="u", end_date="2026-11-03T00:00:00Z", accepting=False)
        self.assertNotIn("eventStatus", closed, "not accepting orders is not the same claim as cancelled")

    def test_the_card_carries_the_labels_the_page_carries(self):
        card = pp.cards.trader_card(handle="surat_whale", headline="rank #4 of 91",
                                    stats=[("realised", "$412k"), ("settled markets", "48"), ("win rate", "63.4%")],
                                    notes=["win rate is behind the sample gate"], ranked=True, provisional=True,
                                    drawdown="$12,400", url="https://polygm.app/trader/surat_whale")
        joined = " ".join(card["footnote"]).lower()
        self.assertIn("provisional", joined)
        self.assertIn("drawdown", joined)
        self.assertEqual(len(card["lines"]), 3)
        self.assertEqual(pp.cards.card_findings(card), [])

    def test_a_card_key_is_the_content_and_not_the_clock(self):
        kw = dict(handle="h", headline="rank #1 of 2", stats=[("a", "1")], notes=[], ranked=True,
                  provisional=False)
        one = pp.cards.trader_card(**kw, url="https://polygm.app/trader/h")
        two = pp.cards.trader_card(**{**kw, "headline": "rank #2 of 2"}, url="https://polygm.app/trader/h")
        self.assertEqual(pp.cards.card_key(one),
                         pp.cards.card_key(pp.cards.trader_card(**kw, url="https://elsewhere.example/trader/h")),
                         "the URL is not part of the card's content")
        self.assertNotEqual(pp.cards.card_key(one), pp.cards.card_key(two))

    def test_a_card_with_an_address_is_a_finding(self):
        bad = pp.cards.leaderboard_card(board="volume", window="7d", rows=[("0x" + "b" * 40, "$1m")],
                                        notes=[], url="u")
        self.assertTrue(any("address" in f for f in pp.cards.card_findings(bad)))


if __name__ == "__main__":                                   # pragma: no cover
    unittest.main()
