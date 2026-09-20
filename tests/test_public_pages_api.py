"""P11 · D6's public routes: a page somebody will share, and the budget that keeps it a page.

What this file pins, and why each one is a rule rather than a convenience:

  1. **A page that is not published does not exist.** A handle that has been listed and a handle nobody has
     ever claimed answer with the same body — otherwise the endpoint is a way to ask "is this name taken?"
     one string at a time, which is the beginning of a phishing kit.
  2. **No address, anywhere, at any depth.** Every payload is grepped for a `0x…` shape and for an account id:
     a public page addressed by a pseudonym is the only reason this product can publish a trader at all.
  3. **The qualifiers travel.** The sample gate, the provisional window and the drawdown are in the payload,
     in the card's footnote and in the JSON-LD — because the card is what gets screenshotted.
  4. **The budget refuses, and the block is the second lever.** Eleven sitemap walks in a minute is a 429 with
     a `Retry-After`; an operator can refuse an address outright, and what gets stored is a digest.
"""
from __future__ import annotations

import json
import re
import unittest

from conftest import import_app, refresh_flags  # noqa: F401

ADDRESS = re.compile(r"0x[0-9a-fA-F]{6,}")
HANDLE = "public_whale"
PRIVATE_HANDLE = "public_private"
SLUG = "fed-cut-sept"


class PublicPagesBase(unittest.TestCase):
    app_name = "api-public-pages"
    app = None
    client = None

    @classmethod
    def setUpClass(cls):
        cls.app = import_app(cls.app_name)
        import seed_leaderboard
        cls.seeded = seed_leaderboard.seed_sqlite()
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.app.app, raise_server_exceptions=False)

    def setUp(self):
        refresh_flags(self.app)
        self.app._db.execute("DELETE FROM leaderboard_identity")
        self.app._db.execute("DELETE FROM user_identities WHERE kind='wallet'")
        self.app._db.execute("DELETE FROM public_page_hits")
        self.app._db.execute("DELETE FROM public_page_blocks")
        self.app._db.commit()

    # ------------------------------------------------------------------ fixtures
    def link(self, uid: str, wallet: str) -> str:
        self.app._db.execute("INSERT OR IGNORE INTO users (id, created_ms, tier) VALUES (?,?, 'free')", (uid, 1))
        self.app._db.execute("INSERT OR IGNORE INTO user_identities (kind, value, user_id, state, claimed_ms,"
                             " verified_ms, proof_kind, revoked_ms)"
                             " VALUES ('wallet',?,?,'verified',?,?, 'fixture', NULL)", (wallet, uid, 1, 1))
        self.app._db.commit()
        return self.app._anon(wallet)

    def wallet_at_rank(self, rank: int = 20, board: str = "risk_adjusted") -> str:
        """The ADDRESS of the wallet at `rank`. Not "the first seeded wallet": most of the population is below
        the gate, and a public page for an unranked wallet is a 404 by design — an assertion built on one would
        fail for a reason that has nothing to do with D6."""
        board_body = self.app._lb_board(board_id=board, window="", at_ms=self.app._now_ms())
        row = next((r for r in board_body["rows"] if r["rank"] == rank), None)
        self.assertIsNotNone(row, "the fixture has a rank %d on %s" % (rank, board))
        address = self.app._wallet_for_anon(str(row["wallet"]))
        self.assertIsNotNone(address, "the pseudonym must resolve back for the fixture to link it")
        return str(address)

    def list_handle(self, handle: str, uid: str = "u-public"):
        wallet = self.wallet_at_rank()
        self.link(uid, wallet)
        # The key is unique per TEST, not per handle: the API memoises by key, so a second test reusing the
        # first one's key would get the first one's stored 200 back without touching the database that setUp
        # just reset - a green assertion about a row that no longer exists.
        key = "d6-list-%s-%s" % (handle, self._testMethodName)
        r = self.client.post("/v1/leaderboard/identity", json={"state": "listed", "handle": handle},
                             headers={"X-User-Id": uid, "Idempotency-Key": key})
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(r.json()["identity"]["handle"], handle)
        return wallet

    def tree(self, obj) -> list:
        """Every string in a payload, at any depth."""
        out: list = []

        def walk(v):
            if isinstance(v, str):
                out.append(v)
            elif isinstance(v, dict):
                for k, sub in v.items():
                    out.append(str(k))
                    walk(sub)
            elif isinstance(v, (list, tuple)):
                for sub in v:
                    walk(sub)

        walk(obj)
        return out

    def assert_no_address(self, payload, where: str):
        hits = [s for s in self.tree(payload) if ADDRESS.search(s)]
        self.assertEqual(hits, [], "%s carries an address: %s" % (where, hits[:2]))


class TestPublicTrader(PublicPagesBase):
    def test_a_handle_that_is_not_published_does_not_exist(self):
        wanted = self.list_handle(HANDLE)
        self.assertIsNotNone(wanted)
        # published: it resolves
        ok = self.client.get("/v1/public/trader/" + HANDLE)
        self.assertEqual(ok.status_code, 200, ok.text[:300])
        # withdrawn: the same body as a name nobody ever claimed
        r = self.client.post("/v1/leaderboard/identity", json={"state": "private"},
                             headers={"X-User-Id": "u-public",
                                      "Idempotency-Key": "d6-unlist-" + self._testMethodName})
        self.assertEqual(r.status_code, 200, r.text[:200])
        gone = self.client.get("/v1/public/trader/" + HANDLE)
        never = self.client.get("/v1/public/trader/never_was_a_handle")
        self.assertEqual(gone.status_code, 404)
        self.assertEqual(never.status_code, 404)
        self.assertEqual(gone.json()["error"]["code"], never.json()["error"]["code"])
        self.assertEqual(gone.json()["error"]["message"], never.json()["error"]["message"])

    def test_the_page_carries_a_rank_the_qualifiers_and_no_address(self):
        self.list_handle(HANDLE)
        r = self.client.get("/v1/public/trader/" + HANDLE)
        self.assertEqual(r.status_code, 200, r.text[:400])
        body = r.json()
        self.assertEqual(body["handle"], HANDLE)
        self.assertEqual(body["url"].endswith("/trader/" + HANDLE), True)
        self.assertEqual(len(body["standing"]), 9, "six boards plus four category boards (\u00a72.12)")
        boards = [e["board"] for e in body["standing"]]
        self.assertEqual(boards.count("category"), 4, "the category board is four answers, not one")
        self.assertEqual(len({e["category"] for e in body["standing"] if e["board"] == "category"}), 4)
        default = next(e for e in body["standing"] if e["board"] == "risk_adjusted")
        self.assertGreater(default["rank"], 0)
        self.assertGreater(default["rankedTotal"], 0)
        self.assertEqual(default["rankBadge"]["rank"], default["rank"])
        self.assertTrue(body["notes"], "a public row without its qualifiers is a claim, not a measurement")
        self.assertTrue(body["card"]["footnote"], "the card must carry what qualifies the number")
        self.assertEqual(body["cardKey"], self.app._pp.cards.card_key(body["card"]))
        self.assert_no_address(body, "the trader page")
        # The indexability rule in one line: a provisional wallet's page works for the human who was sent the
        # link and is not offered to a crawler. The fixture's wallets are all inside their first week.
        age = int(default.get("ageDays") or 0)
        if default["state"] != "ranked" or age < self.app._pp.urls.PROVISIONAL_DAYS:
            self.assertTrue(body["robots"].startswith("noindex"), body["robots"])
        else:
            self.assertTrue(body["robots"].startswith("index"), body["robots"])
        # the validator and the cache contract are the two things a crawler reads first
        self.assertIn("ETag", r.headers)
        self.assertTrue(r.headers["Cache-Control"].startswith("public, max-age="))
        self.assertIn("X-RateLimit-Remaining", r.headers)
        self.assertEqual(body["structuredData"][0]["@type"], "BreadcrumbList")
        self.assertIn("ProfilePage", json.dumps(body["structuredData"]))

    def test_a_malformed_handle_is_a_422_naming_the_field(self):
        r = self.client.get("/v1/public/trader/ab")
        self.assertEqual(r.status_code, 422)
        self.assertIn("handle", r.json()["error"]["message"])


class TestPublicMarket(PublicPagesBase):
    def test_the_odds_page_states_how_old_the_price_is(self):
        r = self.client.get("/v1/public/market/" + SLUG)
        self.assertEqual(r.status_code, 200, r.text[:400])
        body = r.json()
        self.assertEqual(body["slug"], SLUG)
        self.assertIn("ageMs", body["odds"])
        self.assertGreaterEqual(body["odds"]["ageMs"], 0)
        self.assertTrue(body["odds"]["ageText"])
        self.assertTrue(body["quoteNote"])
        self.assert_no_address(body, "the market page")
        self.assertEqual(body["structuredData"][0]["@type"], "BreadcrumbList")

    def test_a_missing_or_malformed_slug(self):
        self.assertEqual(self.client.get("/v1/public/market/no-such-market-here").status_code, 404)
        self.assertEqual(self.client.get("/v1/public/market/" + "x" * 200).status_code, 422)

    def test_a_resolution_quote_is_served_as_data_and_not_sanitised_away(self):
        body = self.client.get("/v1/public/market/" + SLUG).json()
        self.assertIn("resolutionCriteria", body)


class TestPublicBoard(PublicPagesBase):
    def test_the_board_page_carries_the_formula_and_every_rows_sample(self):
        r = self.client.get("/v1/public/leaderboard/risk_adjusted", params={"limit": 10})
        self.assertEqual(r.status_code, 200, r.text[:300])
        body = r.json()
        self.assertEqual(body["board"], "risk_adjusted")
        self.assertTrue(body["formula"])
        self.assertTrue(body["notes"])
        self.assertTrue(body["rows"], "the seeded population ranks")
        for row in body["rows"]:
            self.assertIn("settledMarkets", row)
            self.assertIn("anon", row)
        self.assert_no_address(body, "the board page")
        types = [g["@type"] for g in body["structuredData"]]
        self.assertIn("BreadcrumbList", types)
        self.assertIn("ItemList", types)
        self.assertEqual(self.client.get("/v1/public/leaderboard/not_a_board").status_code, 404)
        self.assertEqual(self.client.get("/v1/public/leaderboard/win_rate", params={"window": "3d"}).status_code,
                         422)


class TestBudgetAndBlocks(PublicPagesBase):
    def test_the_sitemap_walk_is_the_load_the_budget_is_smallest_for(self):
        body = self.client.get("/v1/public/sitemap").json()
        self.assertTrue(body["urls"])
        self.assertEqual(body["count"], len(body["urls"]))
        self.assertIn("caps", body)
        self.assert_no_address(body, "the sitemap")
        self.assertTrue(any("/leaderboard/" in u["url"] for u in body["urls"]))
        limit = self.app._pp.budget.BUDGET["sitemap"][0]
        for _ in range(limit):
            r = self.client.get("/v1/public/sitemap")
        self.assertEqual(r.status_code, 429, "the %dth walk must be refused" % (limit + 1))
        self.assertIn("Retry-After", r.headers)
        self.assertGreater(int(r.headers["Retry-After"]), 0)
        hit = self.app._db.execute("SELECT hits, locked_until_ms FROM public_page_hits "
                                   "WHERE kind='sitemap'").fetchone()
        self.assertIsNotNone(hit)
        self.assertGreater(int(hit[0]), limit)
        self.assertGreater(int(hit[1]), 0, "a refusal past the limit is a lock, not just a 429")

    def test_coming_back_past_the_limit_is_what_earns_a_block(self):
        limit = self.app._pp.budget.BUDGET["sitemap"][0]
        for _ in range(limit + 12):
            self.client.get("/v1/public/sitemap")
        row = self.app._db.execute("SELECT scope, reason, kind, until_ms FROM public_page_blocks").fetchone()
        self.assertIsNotNone(row, "a caller who kept going after a lock is a scraper and should be blocked")
        self.assertEqual(row[0], "all")
        self.assertEqual(row[2], "auto")
        # and the block applies to every public page, not only the one that was scraped
        self.assertEqual(self.client.get("/v1/public/market/" + SLUG).status_code, 429)

    def test_an_operator_can_block_an_address_and_what_is_stored_is_a_digest(self):
        self.app._db.execute("DELETE FROM public_page_blocks")
        self.app._db.commit()
        import os
        os.environ["PGM_ADMIN_TOKEN"] = "t" * 40
        r = self.client.post("/v1/public/blocks",
                             json={"address": "203.0.113.7", "reason": "recursive scrape of the sitemap",
                                   "hours": 2, "scope": "all"},
                             headers={"X-Admin-Token": "t" * 40, "Idempotency-Key": "d6-block-1"})
        self.assertEqual(r.status_code, 200, r.text[:300])
        body = r.json()
        self.assertTrue(body["subject"].startswith("i_"))
        self.assertNotIn("203.0.113.7", json.dumps(body))
        listed = self.client.get("/v1/public/blocks", headers={"X-Admin-Token": "t" * 40})
        self.assertEqual(listed.status_code, 200, listed.text[:200])
        self.assertGreaterEqual(listed.json()["liveCount"], 1)
        self.assertNotIn("203.0.113.7", listed.text)

    def test_the_block_routes_need_an_operator_token(self):
        import os
        os.environ["PGM_ADMIN_TOKEN"] = "t" * 40
        self.assertEqual(self.client.get("/v1/public/blocks").status_code, 503)
        self.assertEqual(self.client.get("/v1/public/blocks",
                                         headers={"X-Admin-Token": "wrong" * 10}).status_code, 403)
        # A token sent to a box with none configured is a 403, not a 503: the 503 is for the case where the
        # CALLER sent nothing and the honest answer is "this box is not configured" rather than "you are wrong".
        os.environ.pop("PGM_ADMIN_TOKEN", None)
        self.assertEqual(self.client.get("/v1/public/blocks", headers={"X-Admin-Token": "t" * 40}).status_code, 403)

    def test_public_reads_are_audited_without_recording_the_caller(self):
        self.client.get("/v1/public/sitemap")
        rows = self.app._db.execute("SELECT action, actor_type, target_id FROM audit_log "
                                    "WHERE action LIKE 'public.%'").fetchall()
        self.assertTrue(rows, "public reads are audited so 'who did we serve' is a query")
        for r in rows:
            self.assertEqual(r[1], "service")
            self.assertFalse(ADDRESS.search(str(r[2] or "")))


if __name__ == "__main__":                                   # pragma: no cover
    unittest.main()
