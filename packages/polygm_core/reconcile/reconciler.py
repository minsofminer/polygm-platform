"""D3: the reconciler. One process, one durable cursor, eight cases, and a metric that ages.

Why one owner. The eight cases below are not eight features; they are the eight ways "what the venue has"
and "what we booked" can differ, and each of them overlaps the others in exactly the place a bug lives. If
a fill sweeper and an order sweeper run independently, the fill sweeper books a trade for an order the
order sweeper is about to declare nonexistent, and both of them are right on their own terms. So:

* one pass, one after the other, in `CASE_ORDER` — an order chosen so that existence is settled before
  amounts (`no_ack` and `submitted_unacked` first: until we know whether an order is real, booking its
  fills is meaningless), and terminal transitions before orphan hunting (so a cancel we just issued is not
  mistaken for an orphan at the venue);
* one durable cursor (`reconcile_cursors`, name `'main'`, CHECK'd so a second one cannot exist) carrying a
  watermark and an in-flight claim count, so a crash mid-pass resumes from the claim, not from hope;
* one idempotency mechanism: `reconcile_actions.dedupe_key UNIQUE` for everything that touched the venue,
  and the ledger's own UNIQUE keys for everything that touched money. Re-running the pass is the recovery
  procedure, so it must be a no-op by construction rather than by review.

What "reconciled" means here, precisely: not "the numbers matched once", but "this item is out of
`reconcile_open`". An item that cannot be resolved is *escalated* (attempts + `escalated_ms`) and keeps
aging, because a reconciler that drops what it cannot fix turns its own blind spots into green dashboards.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from dataclasses import dataclass, field

from polygm_core.ledger.ledger import venue_status_to_state
from polygm_core.money.cents import notional_floor

# The eight cases, in pass order. The metric `unreconciled_orders` counts rows in `reconcile_open`, whose
# case_name is CHECK'd against exactly this list, so a case cannot be added in code without being added in
# the schema (which is the point: an ownerless case is how the next incident starts).
CASE_ORDER: tuple[str, ...] = (
    "no_ack",                    # we never got a venue answer for a POST we signed
    "submitted_unacked",        # venue id known, but `acknowledged=0` in our row
    "cancelled_race",           # our cancel and a fill crossed in the middle
    "lagging_fill",             # the venue reports more matched than we booked
    "closing_market",           # market stopped accepting orders, we still have live orders in it
    "ghost_order",              # we say terminal, the venue says live
    "orphan",                   # the venue has an order with our builder code and we have no row for it
    "ambiguous_settlement",     # a venue trade we cannot yet tie to one of our orders
)
ALARM_AFTER_MS = 60_000
CURSOR_NAME = "main"


@dataclass(frozen=True)
class Cfg:
    no_ack_grace_ms: int = 10_000            # how long an unanswered POST is allowed to be unanswered
    unacked_grace_ms: int = 20_000
    cancel_race_grace_ms: int = 5_000
    fill_grace_ms: int = 3_000               # a fill 3s late is normal; 3s+grace with no fill is not
    closing_grace_ms: int = 15_000
    ghost_grace_ms: int = 30_000
    orphan_grace_ms: int = 120_000           # generous: our own write can legitimately trail the venue's
    ambiguous_grace_ms: int = 60_000
    max_sightings: int = 8                   # escalate after this many *successful* sightings
    look_limit: int = 200
    # The reconciler owns its own hop budgets, which are not the executor's: a lookup during recovery is
    # allowed to be slower than a POST on the live path, and `HOPS` says so (P04).
    lookup_timeout_ms: int = 2_000
    list_timeout_ms: int = 2_000
    cancel_timeout_ms: int = 2_000
    trades_timeout_ms: int = 2_000
    alarm_after_ms: int = ALARM_AFTER_MS
    builder_prefix: str = "0x"               # how we recognise "an order placed through us" at the venue


def case_key(case: str, *parts: str) -> str:
    """A dedupe key that depends only on the venue facts being reconciled, never on when we looked. A key
    that included a timestamp would make every pass a new action, which is the definition of not idempotent."""
    core = "|".join((case, *(p or "" for p in parts)))
    return "%s:%s" % (case, hashlib.sha256(core.encode()).hexdigest()[:24])


@dataclass
class Pass:
    """The pass report. Returned rather than only logged: `tools/p06-gate-check.py` reads the shape, the API
    can surface `unreconciled` on a dashboard, and a report that only exists in stdout cannot be asserted."""
    at_ms: int = 0
    per_case: dict[str, dict] = field(default_factory=dict)
    actions: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    recovered_claims: int = 0
    watermark_ms: int = 0

    def add(self, case: str, **kw) -> None:
        d = self.per_case.setdefault(case, {"found": 0, "acted": 0, "opened": 0, "closed": 0, "bumped": 0,
                                            "escalated": 0})
        for k, v in kw.items():
            d[k] = d.get(k, 0) + v

    @property
    def totals(self) -> dict:
        return {k: sum(c.get(k, 0) for c in self.per_case.values())
                for k in ("found", "acted", "opened", "closed", "bumped", "escalated")}

    @property
    def changed(self) -> bool:
        t = self.totals
        return any(t[k] for k in ("acted", "opened", "closed", "escalated"))


class Reconciler:
    """`store` is `services/executor/store.Store`; `venue` is anything with `list_orders()`,
    `find_order(hash)`, `trades_for(order_id)`, `cancel(order_id)`. Both are injected because the whole
    point of this module is that its behaviour is a function of *what the venue says*, and a test that
    cannot lie about the venue cannot test it."""

    def __init__(self, store, venue, *, cfg: Cfg | None = None, log=None, on_book=None) -> None:
        self.store = store
        self.venue = venue
        self.cfg = cfg or Cfg()
        self.log = log or (lambda *a: None)
        # `on_book` exists for the chaos gate: a callable that gets invoked with the stage name the moment a
        # fill is durable, so the harness can freeze the process on the money line. Default None; the product
        # never passes one, and nothing else reads it.
        self.on_book = on_book
        self.passes = 0
        self.venue_cache: dict = {}
        self.venue_cache_at = 0

    # ------------------------------------------------------------------ durable cursor + crash recovery
    def startup(self, *, at: int | None = None) -> dict:
        """Recover the claim. A cursor with in_flight > 0 says the previous pass died mid-work, and the
        actions it had already taken are durable, so what recovery means is: re-own those items and re-run;
        every step below is a no-op if the previous pass finished them."""
        t = at or self._now()
        cur = self.store.cursor_load()
        out = {"watermark_ms": cur["watermark_ms"], "claimed_at_death": cur["in_flight"],
               "last_run_ms": cur["last_run_ms"], "last_error": cur["last_error"]}
        open_rows = self.store.unreconciled()
        out["open_items"] = len(open_rows)
        self.store.cursor_save(watermark_ms=cur["watermark_ms"], in_flight=len(open_rows), at=t,
                               last_error=cur["last_error"])
        self.recovered = cur["in_flight"]
        return out

    def _now(self) -> int:
        import time
        return int(time.time() * 1000)

    # --------------------------------------------------------------------- venue call helpers
    # Every venue call in this module goes through one of these, for one reason: a transport that takes a
    # timeout is a transport that WILL be called with a timeout, and a reconciler that inherits the submit
    # path's 1500ms budget is a reconciler that gives up on a busy venue before a patient one has started.
    def _find(self, client_order_hash: str):
        return self.venue.find_order(client_order_hash, timeout_ms=self.cfg.lookup_timeout_ms)

    def _trades(self, order_id: str):
        return self.venue.trades_for(order_id, timeout_ms=self.cfg.trades_timeout_ms)

    def _cancel(self, order_id: str) -> dict:
        return self.venue.cancel(order_id, timeout_ms=self.cfg.cancel_timeout_ms) or {}

    def _recent_trades(self, *, limit: int):
        if hasattr(self.venue, "recent_trades"):
            return self.venue.recent_trades(limit=limit, timeout_ms=self.cfg.list_timeout_ms) or []
        return (self.venue.snapshot(timeout_ms=self.cfg.list_timeout_ms) or {}).get("trades", [])

    # ------------------------------------------------------------------ venue reads, once per pass
    def _venue_orders(self, *, at: int) -> dict:
        """One venue read per pass, shared by every case. The alternative — each case calling the venue —
        makes the eight cases disagree about what the venue said, and a disagreement inside one pass is a
        reconciliation bug that cannot be reproduced from logs."""
        if at - self.venue_cache_at > 2_000 or not self.venue_cache:
            snap = self.venue.list_orders(timeout_ms=self.cfg.list_timeout_ms) or {}
            self.venue_cache = snap.get("orders", snap) if isinstance(snap, dict) else {}
            self.venue_cache_at = at
        return self.venue_cache

    # -------------------------------------------------------------------------------------------- pass
    def run_pass(self, *, at: int | None = None) -> Pass:
        t = at or self._now()
        p = Pass(at_ms=t)
        p.recovered_claims = getattr(self, "recovered", 0)
        self.recovered = 0
        self.passes += 1
        cur = self.store.cursor_load()
        # in_flight is written BEFORE the work, so a crash leaves a non-zero claim; after the work it is
        # the count of still-open items, which is what a dashboard wants.
        self.store.cursor_save(watermark_ms=cur["watermark_ms"], in_flight=1, at=t, last_error="")
        for case in CASE_ORDER:
            try:
                getattr(self, "case_" + case)(p, at=t)
            except Exception as e:                                  # noqa: BLE001 - a case must not kill the pass
                # A case that raises is recorded as an error AND left open (whatever it had opened stays),
                # because "the reconciler crashed on orphan hunting" must not read as "there are no orphans".
                p.errors.append("%s: %s" % (case, str(e)[:180]))
                self.log("reconcile case %s raised: %s" % (case, e))
        rows = self.store.unreconciled()
        self.store.cursor_save(watermark_ms=max([t] + [r["since_ms"] for r in rows]), in_flight=len(rows),
                               at=t, last_error="; ".join(p.errors)[:300])
        p.watermark_ms = t
        self._age(p, rows=rows, at=t)
        return p

    def _age(self, p: Pass, *, rows: list[dict], at: int) -> None:
        for r in rows:
            if at - r["since_ms"] > self.cfg.alarm_after_ms:
                if not r["escalated_ms"]:
                    self.store.escalate_case(r["dedupe_key"], at=at)
                    p.add(r["case_name"], escalated=1)

    # ------------------------------------------------------------------------- metric + alarm (D3)
    def metric(self, *, at: int | None = None) -> dict:
        t = at or self._now()
        rows = self.store.unreconciled()
        oldest = min((r["since_ms"] for r in rows), default=0)
        age = (t - oldest) if oldest else 0
        return {"unreconciled_orders": len(rows), "oldest_age_ms": age,
                "alarm": bool(rows and age > self.cfg.alarm_after_ms),
                "alarm_after_ms": self.cfg.alarm_after_ms,
                "by_case": {c: sum(1 for r in rows if r["case_name"] == c) for c in CASE_ORDER},
                "escalated": sum(1 for r in rows if r["escalated_ms"]),
                "passes": self.passes}

    # ------------------------------------------------------------------------------------ the cases
    def case_no_ack(self, p: Pass, *, at: int) -> None:
        """Question 1: *we signed, POSTed, and never got an answer.*

        Ask the venue, by the client order hash we derived deterministically before sending (P04's
        contribution, which is what makes this answerable at all). Three outcomes, and the third is the one
        that keeps money safe:
          found            -> adopt the venue id, mark submitted, book any fills it already has;
          not found        -> it is still not safe to say "no order": absence is only provable after
                              `max_sightings` across multiple passes, then the intent is cancelled — never
                              re-POSTed, because a re-POST is the duplicate;
          lookup failed    -> leave it open, keep aging, alarm at 60s. The human is the fallback, not a guess.
        """
        cfg = self.cfg
        rows = self.store.intents_in_state("uncertain", older_than_ms=cfg.no_ack_grace_ms, at=at)
        rows += self.store.intents_in_state("submitting", older_than_ms=cfg.no_ack_grace_ms, at=at)
        for it in rows[:cfg.look_limit]:
            p.add("no_ack", found=1)
            key = case_key("no_ack", it["id"], it["client_order_hash"])
            if not it["client_order_hash"]:
                # Nothing to look up by: this can only be an intent written by a version that did not stamp
                # the hash. That is a hard integrity failure, so it is escalated, not skipped.
                if self.store.open_case(case="no_ack", intent_id=it["id"], since_ms=at,
                                        note="no client_order_hash on the intent; cannot prove what was sent",
                                        dedupe_key=key):
                    p.add("no_ack", opened=1)
                continue
            found, lookup_ok = None, True
            try:
                found = self._find(it["client_order_hash"])
            except Exception as e:                                  # noqa: BLE001
                lookup_ok = False
                p.errors.append("no_ack lookup %s: %s" % (it["id"], str(e)[:120]))
            if found:
                if self.store.record_action(case="no_ack", intent_id=it["id"], order_id=str(found.get("orderID") or ""),
                                            action="adopt:%s" % found.get("status"), dedupe_key=key, at=at):
                    p.add("no_ack", acted=1)
                self._adopt(it, found, at=at, note="reconciled: the order did exist")
                self.store.close_case(key)
                p.add("no_ack", closed=1)
                continue
            attempts = self.store.case_attempts(key)
            # A failed lookup is NOT a sighting. Absence is only evidence when the question was actually
            # answered, and "we could not reach the venue 8 times, therefore the order does not exist" is the
            # inference that cancels a live order while the venue keeps matching it. Failed lookups update
            # the note (so the queue says why) without advancing the counter.
            if not lookup_ok:
                if not self.store.open_case(case="no_ack", intent_id=it["id"], order_id="", since_ms=at,
                                            note="cannot reach the venue to ask about %s"
                                                 % it["client_order_hash"][:16], dedupe_key=key):
                    self.store.note_case(key, at=at, note="lookups failing; absence not established")
                p.add("no_ack", bumped=0)
                continue
            if not self.store.open_case(case="no_ack", intent_id=it["id"], order_id="", since_ms=at,
                                        note="venue answered: no order for %s" % it["client_order_hash"][:16],
                                        dedupe_key=key):
                self.store.bump_case(key, at=at, note="still absent on sighting %d" % (attempts["attempts"] + 1))
                p.add("no_ack", bumped=1)
            else:
                p.add("no_ack", opened=1)
            if attempts["attempts"] + 1 >= cfg.max_sightings:
                if self.store.record_action(case="no_ack", intent_id=it["id"], order_id="",
                                            action="declare_absent_after_%d_sightings" % (attempts["attempts"] + 1),
                                            dedupe_key=key + ":absent", at=at):
                    p.add("no_ack", acted=1)
                # NOT "rejected": `cancelled` says "this never reached the book", which is the claim we can
                # now make. A rejection would imply the venue refused it, and the user would ask why there is
                # no venue record of a refusal.
                self.store.set_intent_state(it["id"], "cancelled", at=at, risk_code="",
                                            client_order_hash=it["client_order_hash"])
                self.store.lifecycle(intent_id=it["id"], order_id="", user_id=it["user_id"], state="cancelled",
                                     reason="no order exists at the venue after %d checks; nothing was sent "
                                            "twice" % (attempts["attempts"] + 1), source="reconciler", at=at)
                self.store.lifecycle(intent_id=it["id"], order_id="", user_id=it["user_id"],
                                     state="reconciled", reason="intent closed by reconciliation",
                                     source="reconciler", at=at)
                try:
                    self.store.notify(intent_id=it["id"], user_id=it["user_id"], event="reconciled", at=at,
                                      detail={"case": "no_ack", "outcome": "absent"})
                except ValueError:
                    pass
                self.store.close_case(key)
                p.add("no_ack", closed=1)

    def case_submitted_unacked(self, p: Pass, *, at: int) -> None:
        """Question 2: *the venue acknowledged and then said nothing more.*

        Our row exists with `acknowledged=0`. The venue is the authority, so the fix is the same lookup, and
        the state we hold is only replaced by a status the venue actually reports — mapped through
        `venue_status_to_state`, which raises on anything new.
        """
        stale = self.store.conn.execute("SELECT id,intent_id,user_id FROM orders WHERE acknowledged=0 AND "
                                        "placed_ms < ? ORDER BY placed_ms LIMIT ?",
                                        (at - self.cfg.unacked_grace_ms, self.cfg.look_limit)).fetchall()
        for oid, intent_id, user_id in stale:
            p.add("submitted_unacked", found=1)
            key = case_key("submitted_unacked", oid)
            allv = self._venue_orders(at=at)
            v = allv.get(oid) or {}
            status = v.get("status")
            if not status:
                # The venue does not have an order we hold an id for. Do not delete our row: open the case
                # (this is `ghost_order`'s mirror) and let a human see the two views side by side.
                if self.store.open_case(case="submitted_unacked", intent_id=intent_id, order_id=oid, since_ms=at,
                                        note="venue has no record of order id we hold", dedupe_key=key):
                    p.add("submitted_unacked", opened=1)
                else:
                    self.store.bump_case(key, at=at, note="still unknown")
                    p.add("submitted_unacked", bumped=1)
                continue
            if self.store.record_action(case="submitted_unacked", intent_id=intent_id, order_id=oid,
                                        action="ack:%s" % status, dedupe_key=key, at=at):
                p.add("submitted_unacked", acted=1)
            self.store.execute("UPDATE orders SET acknowledged=1 WHERE id=?", (oid,))
            self.store.set_order_state(oid, status, at=at, source="reconciler", intent_id=intent_id,
                                       user_id=user_id)
            self.sync_fills(order_id=oid, intent_id=intent_id, user_id=user_id, venue=v, p=p, case="submitted_unacked",
                             at=at)
            self.store.close_case(key)
            p.add("submitted_unacked", closed=1)

    def case_cancelled_race(self, p: Pass, *, at: int) -> None:
        """Question 3: *a cancel and a fill crossed.*

        The venue's answer is authoritative and it is `already_filled`, not an error. So the order becomes
        filled/partial with real fills booked, and the user is told "filled", never "cancelled" — because the
        money moved and a UI that says "cancelled" about a filled order is the user-visible inconsistency this
        whole phase is written to make impossible.
        """
        rows = self.store.conn.execute("SELECT o.id,o.intent_id,o.user_id,o.token_id,o.market_id,o.side,"
                                       "o.price_micro,o.size_micro FROM orders o WHERE o.state IN "
                                       "('live','partial') AND o.updated_ms < ? ORDER BY o.updated_ms LIMIT ?",
                                       (at - self.cfg.cancel_race_grace_ms, self.cfg.look_limit)).fetchall()
        for oid, intent_id, user_id, token_id, market_id, side, price_micro, size_micro in rows:
            v = self._venue_orders(at=at).get(oid) or {}
            if v.get("status") not in ("matched", "canceled", "expired", "unmatched"):
                continue
            booked = self.store.fills_booked_micro(oid)
            want = int(v.get("size_matched") or 0)
            if v.get("status") == "matched" or want > booked:
                p.add("cancelled_race", found=1)
                self.sync_fills(order_id=oid, intent_id=intent_id, user_id=user_id, venue=v, p=p,
                                 case="cancelled_race", at=at)
                try:
                    self.store.notify(intent_id=intent_id, user_id=user_id, event="filled", at=at,
                                      detail={"case": "cancelled_race", "note": "the fill won the race"})
                except ValueError:
                    pass

    def case_lagging_fill(self, p: Pass, *, at: int) -> None:
        """Question 4: *the venue says more was matched than we booked.*

        Booking is done through the ledger's own idempotent path, so this case can run on every pass against
        the same order forever without changing a cent. A reconciler that "fixes" the difference by writing
        the delta directly is how a double-sighting becomes a double-credit.
        """
        rows = self.store.conn.execute("SELECT id,intent_id,user_id,token_id,market_id,side,state FROM orders "
                                        "WHERE state IN ('live','partial','filled') ORDER BY updated_ms LIMIT ?",
                                        (self.cfg.look_limit,)).fetchall()
        for oid, intent_id, user_id, token_id, market_id, side, state in rows:
            v = self._venue_orders(at=at).get(oid)
            if not v:
                continue
            want, booked = int(v.get("size_matched") or 0), self.store.fills_booked_micro(oid)
            if want <= booked:
                continue
            p.add("lagging_fill", found=1)
            key = case_key("lagging_fill", oid, str(want))
            if at - (self.store.order_row(oid) or {}).get("placed_ms", at) < self.cfg.fill_grace_ms:
                continue                                    # too young to call it lagging
            self.sync_fills(order_id=oid, intent_id=intent_id, user_id=user_id, venue=v, p=p,
                             case="lagging_fill", at=at)
            after = self.store.fills_booked_micro(oid)
            if after >= want:
                if self.store.record_action(case="lagging_fill", intent_id=intent_id, order_id=oid,
                                            action="booked_to_%d" % after, dedupe_key=key, at=at):
                    p.add("lagging_fill", acted=1)
                self.store.close_case(key)
                p.add("lagging_fill", closed=1)
            else:
                if self.store.open_case(case="lagging_fill", intent_id=intent_id, order_id=oid, since_ms=at,
                                        note="venue %d > booked %d with no venue trades" % (want, after),
                                        dedupe_key=key):
                    p.add("lagging_fill", opened=1)
                else:
                    self.store.bump_case(key, at=at, note="still %d short" % (want - after))
                    p.add("lagging_fill", bumped=1)

    def case_closing_market(self, p: Pass, *, at: int) -> None:
        """Question 5: *the market closed while we had an order in it.*

        Cancel first. If the venue says the order already filled, that is `cancelled_race`, and the position
        closes at resolution rather than being force-sold: we do not have a market to sell into any more,
        and pretending otherwise is how a system ends up "hedging" a position that cannot be traded.
        """
        rows = self.store.open_orders()
        seen_market: dict[str, bool] = {}
        for o in rows:
            m = o["market_id"]
            if m not in seen_market:
                seen_market[m] = self.store.market_is_closing(m, at=at)
            if not seen_market[m]:
                continue
            if at - int(o["updated_ms"] or 0) < self.cfg.closing_grace_ms:
                continue                                   # let the live path handle a fresh order first
            p.add("closing_market", found=1)
            key = case_key("closing_market", o["id"])
            if self.store.action_taken(key):
                self.store.close_case(key)
                continue
            resp = self._cancel(o["id"])
            code = str(resp.get("code") or "")
            if code == "already_filled":
                self.sync_fills(order_id=o["id"], intent_id=o["intent_id"], user_id=o["user_id"],
                                 venue=self._venue_orders(at=at).get(o["id"]) or {}, p=p,
                                 case="closing_market", at=at)
                self.store.record_action(case="closing_market", intent_id=o["intent_id"], order_id=o["id"],
                                         action="already_filled;position_held_to_resolution", dedupe_key=key,
                                         at=at)
                p.add("closing_market", acted=1)
                continue
            if resp.get("success"):
                self.store.set_order_state(o["id"], "canceled", at=at, source="reconciler",
                                           intent_id=o["intent_id"], user_id=o["user_id"])
                self.store.record_action(case="closing_market", intent_id=o["intent_id"], order_id=o["id"],
                                         action="canceled_before_close", dedupe_key=key, at=at)
                self.store.close_case(key)
                p.add("closing_market", acted=1, closed=1)
                continue
            if self.store.open_case(case="closing_market", intent_id=o["intent_id"], order_id=o["id"],
                                     since_ms=at, note="cancel refused: %s" % code, dedupe_key=key):
                p.add("closing_market", opened=1)
            else:
                self.store.bump_case(key, at=at, note="cancel still refused: %s" % code)
                p.add("closing_market", bumped=1)

    def case_ghost_order(self, p: Pass, *, at: int) -> None:
        """Question 6: *we cancelled it and the venue did not.* (The mock's `ghost_order` scenario.)

        Our row says terminal; the venue says live. We trust the venue about the venue: re-cancel, and until
        the venue agrees the order is NOT reconciled — it keeps aging and it keeps alarming, and the user
        keeps seeing the honest state, which is "we believe this is cancelled; the venue still lists it".
        Telling the user "cancelled, fully" while the venue can still fill it is the specific lie that
        produces an unexplained fill tomorrow.
        """
        rows = self.store.conn.execute("SELECT id,intent_id,user_id,state,updated_ms FROM orders WHERE state "
                                        "IN ('cancelled','expired','rejected') AND updated_ms < ? ORDER BY "
                                        "updated_ms LIMIT ?", (at - self.cfg.ghost_grace_ms, self.cfg.look_limit))
        for oid, intent_id, user_id, state, updated in rows.fetchall():
            v = self._venue_orders(at=at).get(oid) or {}
            if v.get("status") not in ("live", "partial", "delayed"):
                continue
            p.add("ghost_order", found=1)
            key = case_key("ghost_order", oid)
            if not self.store.open_case(case="ghost_order", intent_id=intent_id, order_id=oid, since_ms=at,
                                        note="local=%s venue=%s" % (state, v.get("status")), dedupe_key=key):
                self.store.bump_case(key, at=at, note="re-cancel pending, sighting %d"
                                     % (self.store.case_attempts(key)["attempts"] + 1))
                p.add("ghost_order", bumped=1)
            else:
                p.add("ghost_order", opened=1)
            if self.store.record_action(case="ghost_order", intent_id=intent_id, order_id=oid,
                                        action="recancel", dedupe_key=key + ":cancel", at=at):
                resp = self._cancel(oid)
                p.add("ghost_order", acted=1)
                after = self._find(str(v.get("client_order_hash") or "")) if v.get("client_order_hash") \
                    else None
                if after and after.get("status") in ("canceled", "cancelled", "expired", "unmatched"):
                    self.store.set_order_state(oid, after["status"], at=at, source="reconciler",
                                               intent_id=intent_id, user_id=user_id)
                    self.store.close_case(key)
                    p.add("ghost_order", closed=1)
                elif not resp.get("success"):
                    self.store.bump_case(key, at=at, note="cancel refused: %s" % resp.get("code"))
            att = self.store.case_attempts(key)
            if att["attempts"] >= self.cfg.max_sightings:
                self.store.escalate_case(key, at=at)
                p.add("ghost_order", escalated=1)

    def case_orphan(self, p: Pass, *, at: int) -> None:
        """Question 7: *the venue has an order with our builder code that we have no row for.*

        Two candidate explanations, and they lead to opposite actions: either our write lost (the crash case,
        where the order IS ours and the right move is to adopt it from `order_attempts`), or it is not ours
        at all (a leaked builder code, or a bug that let a client self-submit). Adoption is therefore keyed
        on the ONLY evidence we have — a signed `order_attempts` row, whose payload digest matches the venue's
        order — and absent that we cancel and open a case. Never "adopt it into the user's balance": a
        position we cannot attribute is money appearing out of nowhere, which is the failure this whole
        pipeline is built to make impossible.
        """
        local = self.store.local_order_ids()
        attempts = {r[0]: r for r in self.store.conn.execute(
            "SELECT a.client_order_hash,a.intent_id,a.user_id,a.token_id,a.side,a.price_micro,a.size_micro,"
            "a.signed_ms FROM order_attempts a LEFT JOIN orders o ON o.intent_id = a.intent_id "
            "WHERE a.submitted_ms IS NULL AND o.id IS NULL")}
        for oid, v in self._venue_orders(at=at).items():
            if oid in local or not isinstance(v, dict):
                continue
            if v.get("status") not in ("live", "partial", "delayed", "matched"):
                continue
            # Age gate. `placed_ms` is present on the mock and on `GET /data/orders`; when a venue gives us
            # no timestamp we still consider it (a zero age would mean "ignore forever", which is an easy way
            # for a real orphan to be permanent) but the cancel is held back behind `max_sightings`.
            placed = int(v.get("placed_ms") or 0)
            if placed and at - placed < self.cfg.orphan_grace_ms:
                continue
            builder = str(v.get("builder") or "")
            if not builder.startswith(self.cfg.builder_prefix):
                continue
            p.add("orphan", found=1)
            key = case_key("orphan", oid, str(v.get("client_order_hash") or ""))
            h = str(v.get("client_order_hash") or "")
            if h in attempts and self.store.load_intent(attempts[h][1]) is not None:
                intent_id, user_id = attempts[h][1], attempts[h][2]
                if self.store.record_action(case="orphan", intent_id=intent_id, order_id=oid,
                                            action="adopt_from_attempt", dedupe_key=key, at=at):
                    self.store.mark_submitted(intent=self.store.load_intent(intent_id), order_id=oid,
                                              client_order_hash=h, at=at, acknowledged=True)
                    self.store.lifecycle(intent_id=intent_id, order_id=oid, user_id=user_id, state="submitted",
                                         reason="adopted by reconciliation from the signed attempt",
                                         source="reconciler", at=at)
                    p.add("orphan", acted=1)
                self.sync_fills(order_id=oid, intent_id=intent_id, user_id=user_id, venue=v, p=p,
                                 case="orphan", at=at)
                self.store.close_case(key)
                p.add("orphan", closed=1)
                continue
            why = ("venue order matches a signed attempt whose intent row is gone; not adopted"
                   if h in attempts else
                   "venue order with our builder code, no local row, no signed attempt")
            if self.store.open_case(case="orphan", order_id=oid, since_ms=at, note=why, dedupe_key=key):
                p.add("orphan", opened=1)
            else:
                self.store.bump_case(key, at=at, note="still unowned on sighting %d"
                                     % (self.store.case_attempts(key)["attempts"] + 1))
                p.add("orphan", bumped=1)
            if self.store.case_attempts(key)["attempts"] >= self.cfg.max_sightings:
                if self.store.record_action(case="orphan", intent_id="", order_id=oid, action="cancel_unowned",
                                            dedupe_key=key + ":cancel", at=at):
                    self._cancel(oid)
                    p.add("orphan", acted=1)
                self.store.escalate_case(key, at=at)
                p.add("orphan", escalated=1)

    def case_ambiguous_settlement(self, p: Pass, *, at: int) -> None:
        """Question 8: *a venue trade we cannot tie to one of our orders.*

        The action is: do not book. The tempting alternative — book it against "the closest order" — is the
        one that puts a wrong number in a user's balance with a plausible story attached. The fill stays in
        `chain_events` (the ingest side keeps the raw facts), the position stays at its last consistent
        value, the case opens and ages, and the alarm is the escalation path. When ownership becomes provable
        (the order row appears via `orphan`, or a human resolves it) the booking happens through
        `book_fill` on the next pass.
        """
        known = self.store.local_order_ids()
        try:
            trades = (self.venue.recent_trades(limit=self.cfg.look_limit) if hasattr(self.venue, "recent_trades")
                      else (self.venue.snapshot() or {}).get("trades", []))
        except Exception:                                     # noqa: BLE001 - the venue may be mid-outage
            trades = []
        for tr in trades or []:
            oid = str((tr or {}).get("orderID") or "")
            if not oid or oid in known:
                continue
            ts = int((tr or {}).get("timestamp") or 0) * 1000
            if ts and at - ts < self.cfg.ambiguous_grace_ms:
                continue                                       # our write may still be in flight; give it room
            p.add("ambiguous_settlement", found=1)
            key = case_key("ambiguous_settlement", oid, str(tr.get("tradeID") or ""))
            if self.store.open_case(case="ambiguous_settlement", order_id=oid, since_ms=at,
                                    note="trade %s for unknown order %s; NOT booked"
                                         % (str(tr.get("tradeID") or "?")[:16], oid[:16]), dedupe_key=key):
                p.add("ambiguous_settlement", opened=1)
            else:
                self.store.bump_case(key, at=at, note="still unattributed")
                p.add("ambiguous_settlement", bumped=1)
            if self.store.case_attempts(key)["attempts"] >= self.cfg.max_sightings:
                self.store.escalate_case(key, at=at)
                p.add("ambiguous_settlement", escalated=1)

    # --------------------------------------------------------------------------------- shared helpers
    def _adopt(self, it: dict, found: dict, *, at: int, note: str) -> None:
        """Take ownership of a venue order we can prove is ours, and close the intent's uncertainty.

        The venue status is translated BEFORE anything is written. Adopting first and mapping later leaves an
        intent marked `submitted` against an order we could not interpret — a user-visible live order with no
        meaning behind it — and that is the exact inconsistency D7 forbids.
        """
        intent = self.store.load_intent(it["id"])
        if intent is None:
            # No intent row: there is nothing to adopt INTO. This is not "no order exists" (the venue says
            # otherwise) and it is not ours to resolve, so the case stays open for a human.
            self.store.open_case(case="no_ack", intent_id=it["id"], order_id=str(found.get("orderID") or ""),
                                 since_ms=at, note="venue has the order but the intent row is gone",
                                 dedupe_key=case_key("no_ack", it["id"], "missing-intent"))
            return
        status = str(found.get("status") or "live")
        try:
            venue_status_to_state(status)
        except ValueError:
            key = case_key("no_ack", it["id"], "unmapped:" + status)
            self.store.open_case(case="no_ack", intent_id=it["id"], order_id=str(found.get("orderID") or ""),
                                 since_ms=at,
                                 note="venue status %r is not in our map; order left unreconciled" % status,
                                 dedupe_key=key)
            raise ValueError("unmapped venue status %r on intent %s; nothing was adopted"
                             % (status, it["id"])) from None
        self.store.mark_submitted(intent=intent, order_id=str(found.get("orderID")),
                                  client_order_hash=it["client_order_hash"], at=at,
                                  acknowledged=bool(found.get("acknowledged", True)))
        self.store.lifecycle(intent_id=it["id"], order_id=str(found.get("orderID")), user_id=it["user_id"],
                             state="submitted", reason=note, source="reconciler", at=at)
        self.store.set_order_state(str(found.get("orderID")), status, at=at, source="reconciler",
                                   intent_id=it["id"], user_id=it["user_id"])
        self.sync_fills(order_id=str(found.get("orderID")), intent_id=it["id"], user_id=it["user_id"],
                        venue=found, p=Pass(), case="adopt", at=at)
        # D7's terminal edge: `unknown -> reconciled`. Without this row the trail stops at "submitted" and
        # reads as though the live path had produced it, which is the difference between an audit trail and a
        # reconstruction. The intent is closed; the order carries on wherever the venue says it is.
        self.store.lifecycle(intent_id=it["id"], order_id=str(found.get("orderID")), user_id=it["user_id"],
                             state="reconciled", reason="reconciled: the venue had it, we adopted it",
                             source="reconciler", at=at)
        try:
            self.store.notify(intent_id=it["id"], user_id=it["user_id"], event="reconciled", at=at,
                              detail={"case": "no_ack", "outcome": "found"})
        except ValueError:
            pass

    def sync_fills(self, *, order_id: str, intent_id: str, user_id: str, venue: dict, p: Pass, case: str,
                    at: int) -> dict:
        """Book every venue trade for this order that we do not already hold, through `book_fill`.

        Prices and sizes arrive as venue floats. Each must round-trip to an exact number of micro units or
        the fill is REFUSED and the difference becomes an `ambiguous_settlement` case: on the money path the
        strictness is the point (P04's rule), which is deliberately different from the tape, where the same
        strict parser threw away 49 of 200 real rows and cost a phase the lesson (P05 §15).
        """
        booked = {"count": 0, "size_micro": 0, "refused": 0}
        try:
            trades = self._trades(order_id)
        except Exception:                                     # noqa: BLE001
            p.errors.append("%s trades_for %s failed" % (case, order_id))
            return booked
        row = self.store.order_row(order_id) or {}
        for tr in trades or []:
            try:
                price_micro = _exact_micro(tr.get("price"))
                size_micro = _exact_micro(tr.get("size"))
            except ValueError:
                booked["refused"] += 1
                key = case_key(case + ":shape", order_id, str(tr.get("tradeID") or ""))
                self.store.open_case(case="ambiguous_settlement", intent_id=intent_id, order_id=order_id,
                                     since_ms=at, note="venue price/size not on the micro grid; not booked",
                                     dedupe_key=key)
                continue
            if price_micro is None or size_micro is None or size_micro <= 0:
                booked["refused"] += 1
                continue
            r = self.store.book_fill(order_id=order_id, intent_id=intent_id, user_id=user_id,
                                     token_id=row.get("token_id", ""), market_id=row.get("market_id", ""),
                                     side=str(tr.get("side") or row.get("side") or "BUY"),
                                     price_micro=price_micro, size_micro=size_micro,
                                     fee_micro=int(tr.get("fee_micro") or 0),
                                     trade_id=str(tr.get("tradeID") or ""),
                                     exchange_ts=int(tr.get("timestamp") or 0),
                                     maker=bool(tr.get("maker", True)), source="reconcile", at=at)
            if r.get("booked"):
                if self.on_book is not None:
                    self.on_book("booked")
                booked["count"] += 1
                booked["size_micro"] += size_micro
                p.add(case, acted=1)
                if intent_id:
                    self.store.lifecycle(intent_id=intent_id, order_id=order_id, user_id=user_id,
                                         state="partial", reason="reconciler booked venue trade %s"
                                        % str(tr.get("tradeID") or "?")[:16], source="reconciler", at=at)
                    try:
                        self.store.notify(intent_id=intent_id, user_id=user_id, event="partial_fill", at=at,
                                          detail={"micro": size_micro, "price_micro": price_micro})
                    except ValueError:
                        pass
        return booked


def _exact_micro(value) -> int | None:
    """A venue price/size as an exact number of micro units, or None, or a raise on garbage.

    The rule is the one P04 established for the client side, applied to the venue's side: the value must
    ROUND-TRIP, `round(v * 1e6) / 1e6 == v`. A tolerance was my first attempt (`abs(scaled - round(scaled)) >
    0.5`) and it is inert — the distance from a float to its own rounding is never more than 0.5 by
    definition, so that check could not fail, and a test that expected it to refuse 0.4999999991234 passed
    the number straight into a ledger. Round-tripping refuses exactly the numbers that are not the integer
    we would store, and nothing else.

    This is stricter than the tape (P05's `fill_micro`), on purpose: a statistic may be off by a rounding,
    a settlement must not be.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("BAD_VENUE_NUMBER")
    if isinstance(value, int):
        # A whole number of dollars (0, 1, 2...) is unambiguous; anything at or above 1e6 could equally be
        # a micros value, and guessing the scale of a settlement number is not a judgement this phase gets
        # to make. Refuse and say why: the caller must pass the venue's own float.
        if value >= 10**6:
            raise ValueError("AMBIGUOUS_SCALE: %r could be dollars or micro units; the venue sends float "
                             "dollars, so pass that" % value)
        return value * 10**6
    if not isinstance(value, (float, Decimal)):
        raise ValueError("BAD_VENUE_NUMBER")
    import math
    f = float(value)
    if not math.isfinite(f) or f < 0:
        raise ValueError("BAD_VENUE_NUMBER")
    n = int(round(f * 10**6))
    if n / 10**6 != f:
        raise ValueError("VENUE_VALUE_OFF_GRID: %r is not an exact number of micro units" % value)
    return n
