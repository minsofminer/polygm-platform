"""P11 · D7's two routes: what the operator sees, and what one click does.

This file pins four things, each of which is a rule rather than a convenience:

  1. **The pairing is the point.** Every finding carries the wallet *and* the `w_…` a public page would show,
     and the payload carries no address-shaped string anywhere else — the P10 rule ("no address ever leaves")
     still applies above the line, and this is the only P11 surface where a raw wallet appears at all.
  2. **A click is a row, and the boards obey it.** `exclude` takes the wallet off the named board and the public
     leaderboard read proves it (count down by one, the pseudonym gone); `include` puts it back; `flag` changes no
     ranking but answers the question. Nothing is deleted, so nothing is unrecoverable.
  3. **A second click is not a second decision.** The same `Idempotency-Key` replays the first answer and writes
     no second row, because the boards replay the *newest* row and a duplicate would read as a later decision.
  4. **The gate's own acceptance case, end to end.** A referral tree where a second wallet was funded by the first
     is caught here — through the API, from rows in the tables, not from a hand-built fixture — and the catching
     is visible on this screen before it is visible anywhere else.
"""
from __future__ import annotations

import json
import os
import re
import unittest

from conftest import import_app, refresh_flags  # noqa: F401

ADDRESS = re.compile(r"0x[0-9a-fA-F]{6,}")
ADMIN = "adm_" + "k" * 44
DAY = 86_400_000


class GamingBase(unittest.TestCase):
    app_name = "api-gaming"
    app = None
    client = None

    @classmethod
    def setUpClass(cls):
        os.environ["PGM_ADMIN_TOKEN"] = ADMIN
        cls.app = import_app(cls.app_name)
        import seed_leaderboard
        cls.seeded = seed_leaderboard.seed_sqlite()
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.app.app, raise_server_exceptions=False)

    def setUp(self):
        refresh_flags(self.app)
        os.environ["PGM_ADMIN_TOKEN"] = ADMIN
        # `leaderboard_exclusions` is append-only — the trigger refuses a DELETE, which is the product working —
        # so a test cannot clean up after itself here. Every test therefore owns its own seeded wallet, and every
        # assertion is about that wallet rather than about a count of rows, which is also what makes the tests
        # read as "this decision did this", the way an operator reads the screen.
        self.app._db.execute("DELETE FROM leaderboard_snapshots")
        self.app._db.commit()

    # ------------------------------------------------------------------ plumbing
    _keys = 0

    def h(self, *, key: str = "", admin: str = ADMIN) -> dict:
        """Admin header plus an Idempotency-Key.

        A key is minted by default because the route REQUIRES one (the P10 rule: a mutating route that is
        idempotent by luck is how a retry double-spends), so a test that omits it is testing the 400 rather than
        the decision it meant to make. The keys are unique per call, which is what a real client sends; the one
        test that wants a repeat passes the same key twice on purpose.
        """
        out = {"X-Admin-Token": admin}
        if not key:
            type(self)._keys += 1
            key = "gaming-key-%d" % type(self)._keys
        out["Idempotency-Key"] = key
        return out

    def decide(self, wallet: str, action: str, reason: str = "operator: test decision", **kw):
        key = str(kw.pop("idem", ""))
        body = {"wallet": wallet, "action": action, "reason": reason}
        body.update(kw)
        return self.client.post("/v1/admin/gaming/decide", json=body, headers=self.h(key=key))

    def snapshot(self, wallet, *, rank, settled, ms, board="risk_adjusted", score=100):
        """`ms` is RELATIVE to now, because the climb rule reads a seven-day window against the request's clock:
        a fixture stamped in 1970 is a fixture the detector correctly ignores, which is how the first version of
        these tests produced an empty list and looked like a broken rule rather than a stale fixture."""
        self.app._db.execute(
            "INSERT INTO leaderboard_snapshots (board, window_key, wallet, rank, score_bps, settled,"
            " drawdown_micro, computed_ms) VALUES (?,?,?,?,?,?,?,?)",
            (board, "30d", wallet, rank, score, settled, 0, ms))
        self.app._db.commit()

    def now_ms(self) -> int:
        return int(self.app._now_ms())

    def planted_climb(self, wallet: str) -> str:
        """A thin record climbing past a population of ordinary traders — the shape the climb rule exists for.

        Both snapshots sit INSIDE the rule's seven-day window, which the first version of this fixture did not do:
        it stamped the earlier one eight days back, so every wallet had exactly one sample in the window and the
        rule correctly reported nothing. The window is the rule; a fixture outside it is a fixture about the past.
        """
        end = self.now_ms() - 1_000
        start = end - 7 * DAY + 60_000
        for i in range(12):
            self.snapshot("0xLBORD%02d" % i, rank=300 + i, settled=60, ms=start)
            self.snapshot("0xLBORD%02d" % i, rank=298 + i, settled=61, ms=end)
        self.snapshot(wallet, rank=900, settled=9, ms=start)
        self.snapshot(wallet, rank=30, settled=9, ms=end)
        return wallet

    def board_anons(self, board: str = "risk_adjusted", limit: int = 200) -> list:
        r = self.client.get("/v1/leaderboard", params={"board": board, "limit": limit})
        self.assertEqual(r.status_code, 200, r.text[:200])
        return [row["anon"] for row in r.json()["rows"]]


class TestTheDashboardIsClosedToEveryoneElse(GamingBase):
    def test_a_missing_or_wrong_token_is_refused_and_a_missing_configuration_is_not_an_attack(self):
        """Three refusals, and the codes are the whole point of having three.

        `_admin` separates *no token offered* (503 `SIGNER_UNAVAILABLE` — the caller may be a browser that was
        never going to have one) from *a token that does not match* or *a box with nothing configured to match it
        against* (403 `ADMIN_REQUIRED`). Both are closed; the difference is what the on-call reads at 2am, where
        a misconfiguration that renders as an intrusion costs an hour of the wrong investigation.
        """
        r = self.client.get("/v1/admin/gaming", headers={"X-Admin-Token": "wrong"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "ADMIN_REQUIRED"))
        r = self.client.get("/v1/admin/gaming")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (503, "SIGNER_UNAVAILABLE"))
        os.environ.pop("PGM_ADMIN_TOKEN", None)
        try:
            r = self.client.get("/v1/admin/gaming", headers={"X-Admin-Token": ADMIN})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "ADMIN_REQUIRED"))
        finally:
            os.environ["PGM_ADMIN_TOKEN"] = ADMIN

    def test_the_limit_is_bounded_and_the_route_declares_itself_admin(self):
        self.assertEqual(self.client.get("/v1/admin/gaming", params={"limit": 0},
                                         headers=self.h()).status_code, 422)
        self.assertEqual(self.client.get("/v1/admin/gaming", params={"limit": 500},
                                         headers=self.h()).status_code, 422)
        self.assertEqual(self.app._authz.level_for("GET /v1/admin/gaming"), self.app._authz.ADMIN)
        self.assertEqual(self.app._authz.level_for("POST /v1/admin/gaming/decide"), self.app._authz.ADMIN)


class TestWhatTheOperatorSees(GamingBase):
    def test_a_fast_climb_on_a_thin_record_arrives_with_its_rule_its_reading_and_its_pseudonym(self):
        wallet = self.planted_climb("0xLBFARM0000000000000000000000000000000001")
        body = self.client.get("/v1/admin/gaming", headers=self.h()).json()
        found = [f for f in body["climbers"] if f["wallet"] == wallet]
        self.assertEqual(len(found), 1)
        f = found[0]
        self.assertEqual(f["severity"], 3)
        self.assertEqual(f["suggested"], "flag")
        self.assertEqual(f["anon"], self.app._anon(wallet))
        self.assertEqual(f["rule"], body["rules"]["fast_climb"]["rule"])
        self.assertTrue(body["rules"]["fast_climb"]["innocent"].startswith("A lucky streak"))
        self.assertEqual(body["evidence"]["historyRows"], 26)

    def test_the_evidence_lists_a_population_not_a_single_sample(self):
        """One wallet is its own median: the rule must stay quiet rather than invent a bar out of nothing."""
        start = self.now_ms() - 8 * DAY
        self.snapshot("0xLBONLY0000000000000000000000000000000001", rank=900, settled=9, ms=start)
        self.snapshot("0xLBONLY0000000000000000000000000000000001", rank=30, settled=9, ms=start + 7 * DAY)
        body = self.client.get("/v1/admin/gaming", headers=self.h()).json()
        self.assertEqual(body["climbers"], [])

    def test_no_address_reaches_the_payload_even_here(self):
        """The wallet is in the finding — that is the point of an admin surface — but nothing else is.

        The test greps the whole document for a `0x…` that is not one of the wallets the findings name, which is
        how a future field (an order id, a funding address, a market id) gets caught before it ships.
        """
        wallet = self.planted_climb("0xLBFARM0000000000000000000000000000000002")
        body = self.client.get("/v1/admin/gaming", headers=self.h()).json()
        allowed = {f["wallet"] for f in body["climbers"]} | {f["wallet"] for f in body["builder"]}
        for f in body["clusters"]:
            allowed |= set(f["wallets"])
        for f in body["chains"]:
            allowed |= set(f["referees"]) | {f["referrer"]}
        blob = json.dumps(body)
        leftovers = [m.group(0) for m in ADDRESS.finditer(blob) if m.group(0) not in allowed
                     and not any(m.group(0) in a for a in allowed)]
        self.assertEqual(leftovers, [], "an address-shaped string that no finding is about")
        self.assertIn(wallet, blob)

    def test_a_referral_tree_where_a_second_wallet_was_funded_by_the_first_is_caught(self):
        """The kit's acceptance case, through the API: the referrer, the second wallet, and the collision."""
        def user(uid: str) -> None:
            self.app._db.execute("INSERT OR IGNORE INTO users (id, created_ms, tier) VALUES (?,?, 'free')",
                                 (uid, 1))

        for uid in ("u-ref", "u-r1", "u-r2", "u-r3"):
            user(uid)
        for referee, funding in (("u-r1", "f_sharedfund"), ("u-r2", "f_sharedfund"), ("u-r3", "f_other")):
            self.app._db.execute(
                "INSERT INTO referral_attributions (referee, referrer, code, token, state, reason,"
                " signed_up_ms, qualify_order, qualify_ms, notional_micro, term_ends_ms, builder_code, decided_ms)"
                " VALUES (?,?,'','','qualified','',?,?,?,?,?,'',?)",
                (referee, "u-ref", 1_000, "o-" + referee, 1_000 + 3_600_000, 25_000_000,
                 1_000 + 365 * DAY, 2_000))
            self.app._db.execute("INSERT OR REPLACE INTO referral_signals (user_id, kind, hash, first_ms, last_ms)"
                                 " VALUES (?, 'funding', ?, ?, ?)", (referee, funding, 1_000, 2_000))
        self.app._db.commit()

        body = self.client.get("/v1/admin/gaming", headers=self.h()).json()
        self.assertEqual(len(body["chains"]), 1)
        chain = body["chains"][0]
        self.assertEqual(chain["referrer"], "u-ref")
        self.assertEqual(sorted(chain["referees"]), ["u-r1", "u-r2", "u-r3"])
        self.assertEqual(chain["sharedFunding"], 1, "two referees on one funding digest")
        self.assertEqual(chain["severity"], 3)
        # The digest is a one-way value and it is counted, never printed: a ticket that quotes `f_…` is a ticket
        # that has copied a link between two people into a support tool.
        self.assertNotIn("f_sharedfund", json.dumps(body))
        self.assertNotIn("f_other", json.dumps(body))


class TestOneClickIsOneRow(GamingBase):
    def wallet(self, n: int = 3) -> str:
        import seed_leaderboard
        return seed_leaderboard.wallet_for(n)

    def test_flag_records_the_question_without_moving_anybody(self):
        wallet = self.wallet(3)
        self.planted_climb(wallet)
        before = self.board_anons()
        r = self.decide(wallet, "flag", "operator: climbed 40 places on 4 resolved markets", finding="fast_climb")
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertEqual(r.json()["action"], "flag")
        self.assertEqual(self.board_anons(), before, "a flag is a question, not a removal")
        row = self.app._db.execute("SELECT action, board, reason, actor FROM leaderboard_exclusions"
                                   " WHERE wallet=?", (wallet,)).fetchone()
        self.assertEqual(row[0], "flag")
        self.assertEqual(row[1], "", "board '' is every board")
        self.assertEqual(row[3], "admin:fast_climb")
        # ...and the screen shows it as already decided, so the same question is not asked twice.
        body = self.client.get("/v1/admin/gaming", headers=self.h()).json()
        self.assertGreaterEqual(body["counts"]["reviewed"], 1)
        flagged = [f for f in body["climbers"] if f["wallet"] == wallet]
        self.assertEqual(len(flagged), 1, "the planted climb is on the screen")
        self.assertEqual(flagged[0]["decided"]["action"], "flag")
        # The audit row names the pseudonym, not the wallet — and the trail is append-only, so the query is
        # scoped to this wallet rather than counting rows a sibling test also wrote.
        audit = self.app._db.execute("SELECT target_id, actor_type, action, detail_json FROM audit_log"
                                     " WHERE action='gaming.decide' AND target_id=?",
                                     (self.app._anon(wallet),)).fetchall()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0][1], "admin")
        self.assertIn("fast_climb", str(audit[0][3]), "the finding kind is the record of why a click happened")

    def test_exclude_takes_the_wallet_off_the_board_and_include_puts_it_back(self):
        wallet = self.wallet(4)
        anon = self.app._anon(wallet)
        self.assertIn(anon, self.board_anons())
        r = self.decide(wallet, "exclude", "operator: confirmed wash pair with w_second")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(anon, self.board_anons(), "the ranking obeys the decision")
        r = self.decide(wallet, "include", "operator: appealed, evidence did not hold")
        self.assertEqual(r.status_code, 200)
        self.assertIn(anon, self.board_anons(), "a decision that cannot be reversed is not a decision")

    def test_a_retried_click_replays_and_writes_no_second_row(self):
        wallet = self.wallet(5)
        first = self.decide(wallet, "exclude", "operator: duplicate click test", idem="gaming-idem-1")
        second = self.decide(wallet, "exclude", "operator: duplicate click test", idem="gaming-idem-1")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["atMs"], second.json()["atMs"], "the stored answer, byte for byte")
        rows = self.app._db.execute("SELECT COUNT(*) FROM leaderboard_exclusions WHERE wallet=?",
                                    (wallet,)).fetchone()[0]
        self.assertEqual(rows, 1, "a second row would read as a later decision")

    def test_a_missing_key_is_a_400_and_the_shape_of_the_request_is_still_a_422(self):
        wallet = self.wallet(6)
        r = self.client.post("/v1/admin/gaming/decide",
                             json={"wallet": wallet, "action": "exclude", "reason": "operator: no key sent"},
                             headers={"X-Admin-Token": ADMIN})
        self.assertEqual(r.status_code, 400)
        r = self.decide(wallet, "banish", "operator: not an action")
        self.assertEqual(r.status_code, 422)
        r = self.decide(wallet, "exclude", "short")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "BAD_REASON")
        r = self.decide(wallet, "exclude", "operator: unknown board", board="not_a_board")
        self.assertEqual(r.status_code, 422)
        r = self.decide(wallet, "exclude", "operator: unknown finding", finding="vibes")
        self.assertEqual(r.status_code, 422)

    def test_a_decision_can_be_scoped_to_one_board(self):
        wallet = self.wallet(7)
        anon = self.app._anon(wallet)
        r = self.decide(wallet, "exclude", "operator: only the volume board", board="volume")
        self.assertEqual(r.status_code, 200, r.text[:200])
        r = self.client.get("/v1/leaderboard", params={"board": "volume", "limit": 200})
        self.assertNotIn(anon, [row["anon"] for row in r.json()["rows"]])
        self.assertIn(anon, self.board_anons("risk_adjusted"), "the other board is untouched")


if __name__ == "__main__":
    unittest.main()
