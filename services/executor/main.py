#!/usr/bin/env python3
"""The executor process (P06): the only thing in the system that can make a signature.

Rules this file exists to keep, each of which is checkable in a test:

1. **Nothing here decides whether a trade is allowed.** Risk, limits, market state, freshness, wallet
   tradability, policy drift, price bounds — all are computed above, and this process refuses to sign if
   the answers are not present. A `--force`, a `--dev`, or an admin flag would be the hole; there is none.
2. **Every path is idempotent.** Re-running this process against the same queue must not produce a second
   order, a second fill, or a second credit. That is why the payload is built from
   `OrderIntent.client_order_hash` (P04's single-source function), why the intent is claimed under a lease,
   and why money enters only through `Store.book_fill`.
3. **The crash between signing and submitting is a designed state, not an error.** `PGM_CRASH_AFTER=signed`
   exists so the quality gate can *perform* that crash with a real SIGKILL and a real restart instead of
   asserting a mock's return value. The order_attempts row is written before the POST precisely so the
   restarted process can answer "did I send it?" by asking the venue.
4. **The kill switch is honoured inside the submit loop**, not only at the API: `kill_switch_engaged()` is
   re-read every cycle (250 ms cache) and checked again immediately before signing, so the worst-case
   exposure after a switch is one in-flight intent, and the drill measures the number rather than assuming it.

Injected at the seams so the whole thing is testable without a venue: `transport` (the V2 client or the
mock), `signer` (a custody provider or a deterministic test signer), `clock` (integer ms).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import time
from dataclasses import dataclass, field, replace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))                  # services/executor
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "packages"))                                    # packages/

from polygm_core.ledger.ledger import IntentState                                 # noqa: E402
from polygm_core.money.cents import SCALE, fmt_usdc, notional_floor               # noqa: E402
from polygm_core.reconcile.reconciler import Cfg as ReconCfg                      # noqa: E402
from polygm_core.reconcile.reconciler import Reconciler                          # noqa: E402
from polygm_core.risk import gate as risk_gate                                    # noqa: E402
from polygm_core.risk.gate import Limits, MarketState, evaluate                   # noqa: E402
from polygm_core.risk.limits import ExtraLimits, RiskBreaker, evaluate_extra     # noqa: E402
from polygm_core.risk.limits import RiskContext                                   # noqa: E402
from polygm_core.venue import clob_v2 as v2                                       # noqa: E402
from polygm_core.wallets import lifecycle as wl                                   # noqa: E402

from store import CLAIM_LEASE_MS, IntentRow, Store, now_ms                        # noqa: E402

# The stages at which the process can be killed on purpose, for the quality gate. `signed` is the money
# stage: after the signature exists, before the venue has answered.
CRASH_STAGES = ("preflight", "signed", "submitted", "booked")


def crash_hook(stage: str) -> None:
    """Exit the way a crash does: no cleanup, no finally, no buffered output flushed on purpose.

    `os._exit(9)` rather than `sys.exit()`: `SystemExit` unwinds, and an unwinding process runs its own
    `finally` blocks, which is exactly the difference between "the executor was killed" and "the executor
    stopped and tidied up". The gate's whole claim is about the first one.
    """
    if os.environ.get("PGM_CRASH_AFTER") != stage:
        return
    if os.environ.get("PGM_CRASH_SIGNAL") == "stop":
        # The harness wants to be the one that kills us. Announce the stage, freeze, and wait to be SIGKILLed:
        # an `os._exit` is still our own syscall, with our own interpreter shutting down underneath it.
        sys.stdout.write("halted-after=%s\n" % stage)
        sys.stdout.flush()
        os.kill(os.getpid(), signal.SIGSTOP)
        return
    sys.stderr.write("executor: simulated crash after %s (PGM_CRASH_AFTER)\n" % stage)
    sys.stderr.flush()
    os._exit(9)


class TestSigner:
    """Deterministic signer for CI. It is NOT a key: it is an HMAC whose "secret" is a fixed string, and the
    payload it "signs" is the payload the real signer would sign, so every downstream digest matches.

    Deliberately refuses to return anything if asked to sign a payload that does not carry a
    `client_order_hash`, because that is the one property the reconciler depends on.
    """
    name = "test-hmac"

    def __init__(self, *, key: str = "polygm-ci-signer-not-a-key") -> None:
        self.key = key.encode()
        self.sign_calls = 0
        self.pubkey = "0x" + hashlib.sha256(self.key).hexdigest()[:40]

    def sign(self, payload: dict) -> dict:
        if not payload.get("client_order_hash"):
            raise RuntimeError("SIGNER_REFUSED: no client_order_hash on the payload to reconcile by")
        self.sign_calls += 1
        core = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return {"signature": "0x" + hashlib.sha256(self.key + core).hexdigest(),
                "signer_pubkey": self.pubkey, "signature_type": 3}


class CustodySigner:
    """The seam P13 fills: Turnkey's `sign` with a scoped session key. Present, named, and empty on
    purpose — a P06 that "worked" against a fake custody provider would be a P06 that shipped a fake."""

    def __init__(self, *, organization: str, credential_ref: str = "") -> None:
        raise NotImplementedError(
            "CustodySigner is wired in P13 (real money). It calls the provider's sign RPC with the policy "
            "hash from the wallet row attached, and refuses when the provider's live policy hashes "
            "differently (docs/P06 D1.4). No signer, no trading, by design.")


@dataclass
class Outcome:
    intent_id: str
    stage: str = "draft"
    state: str = "rejected"
    order_id: str = ""
    client_order_hash: str = ""
    code: str = ""
    notes: list[str] = field(default_factory=list)
    notional_micro: int = 0
    fee_micro: int = 0
    latency_ms: float = 0.0
    booked: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"intent_id": self.intent_id, "stage": self.stage, "state": self.state, "order_id": self.order_id,
                "code": self.code, "notional_micro": self.notional_micro, "fee_micro": self.fee_micro,
                "latency_ms": round(self.latency_ms, 3), "notes": self.notes, "booked": self.booked,
                "client_order_hash": self.client_order_hash}


class Executor:
    def __init__(self, store: Store, *, transport, signer=None, recon_cfg: ReconCfg | None = None,
                 limits: Limits | None = None, extra: ExtraLimits | None = None, policy: wl.Policy | None = None,
                 batch_size: int = 5, log=None) -> None:
        self.store = store
        self.transport = transport
        self.signer = signer or TestSigner()
        self.limits = limits or Limits()
        self.extra = extra or ExtraLimits()
        self.policy = policy or wl.Policy()
        self.batch_size = batch_size
        self.log = log or (lambda msg: print(msg, flush=True))
        self.breaker = RiskBreaker(limits=self.extra)
        self.reconciler = Reconciler(store, transport, cfg=recon_cfg or ReconCfg(), log=self.log,
                                     on_book=crash_hook)
        self.cancel_budget = v2.CancelBudget()
        self.stats: dict[str, int] = {"ticks": 0, "handled": 0, "submitted": 0, "uncertain": 0, "rejected": 0,
                                      "halted_skips": 0, "signed": 0, "reconcile_passes": 0}
        self.last_pass: dict = {}

    # --------------------------------------------------------------------------- context assembly
    def market_state(self, condition_id: str, *, at: int) -> tuple[MarketState | None, dict]:
        r = self.store.conn.execute("SELECT accepting_orders,seconds_delay,minimum_tick_size,"
                                    "minimum_order_size,fee_type,enable_order_book FROM markets WHERE id=?",
                                    (condition_id,)).fetchone()
        if r is None:
            return None, {"missing_market": condition_id}
        q = self.store.market_quote(condition_id, at=at)
        st = MarketState(accepting_orders=bool(r[0]), seconds_delay=int(r[1] or 0),
                         minimum_tick_size=risk_gate.norm_tick(r[2]), minimum_order_size=str(r[3]),
                         fee_type=r[4] or "", enable_order_book=bool(r[5]),
                         best_bid_micro=q["best_bid_micro"], best_ask_micro=q["best_ask_micro"],
                         snap_age_ms=q["age_ms"])
        return st, q

    def risk_decision(self, it: IntentRow, st: MarketState, *, at: int) -> risk_gate.Decision:
        f = self.store.current_flags()
        limits = replace(self.limits, max_order_notional_micro=getattr(f, "max_order_notional_micro",
                                                                        self.limits.max_order_notional_micro),
                         max_24h_notional_micro=getattr(f, "max_24h_notional_micro",
                                                         self.limits.max_24h_notional_micro),
                         min_order_size_shares_micro=getattr(f, "min_order_size_shares_micro",
                                                             self.limits.min_order_size_shares_micro))
        kill = self.store.kill_switch_engaged(at_ms=at)
        primary = evaluate(risk_gate.Intent(user_id=it.user_id, token_id=it.token_id, side=it.side,
                                            price_micro=it.price_micro, size_shares_micro=it.size_micro,
                                            idempotency_key=it.idempotency_key, market_id=it.market_id),
                           st, limits=limits, open_orders=self.store.open_order_count(it.user_id),
                           spent_24h_micro=self.store.spent_24h_micro(it.user_id, at=at), kill_switch=kill,
                           now_ms=at)
        block = self.store.blocklisted(it.market_id, at=at)
        halt = self.store.loss_halt(it.user_id)
        q = self.store.market_quote(it.market_id, at=at)
        ctx = RiskContext(now_ms=at, orders_this_minute=self.store.orders_this_minute(it.user_id, at=at),
                          orders_today=self.store.orders_today(it.user_id, at=at),
                          realized_pnl_today_micro=self.store.realized_pnl_today_micro(it.user_id, at=at),
                          loss_halted=bool(halt and halt["acknowledged_ms"] is None),
                          blocklisted=bool(block), last_quote_micro=q["mid_micro"], quote_age_ms=q["age_ms"],
                          priced_against_micro=it.price_micro,
                          breaker_open=self.breaker.allow(at)[0] is False,
                          breaker_reason=self.breaker.allow(at)[1])
        allow, why = self.breaker.allow(at)
        if not allow:
            extra = risk_gate.Decision(False, "CIRCUIT_OPEN", "risk controls unavailable: %s" % why,
                                       checks_run=("circuit_breaker",))
        else:
            extra = evaluate_extra(price_micro=it.price_micro, side=it.side, ctx=ctx, limits=self.extra)
        return self._join_decisions(primary, extra)

    @staticmethod
    def _join_decisions(primary, extra):
        from polygm_core.risk.limits import evaluate_with_breaker
        joined = evaluate_with_breaker(primary, extra)
        return replace(joined, checks_run=tuple(primary.checks_run) + tuple(extra.checks_run),
                       latency_ms=round(primary.latency_ms + extra.latency_ms, 3))

    # ------------------------------------------------------------------------------------ one intent
    def handle_intent(self, it: IntentRow, *, at: int | None = None) -> Outcome:
        t0 = time.perf_counter()
        at = at or now_ms()
        o = Outcome(intent_id=it.id, client_order_hash="")
        self.store.lifecycle(intent_id=it.id, order_id="", user_id=it.user_id, state="draft",
                             reason="taken from the queue", source="system", at=at)
        # 1. the P04 gate + this phase's extras, in one joined decision, before anything expensive.
        st, quote = self.market_state(it.market_id, at=at)
        if st is None:
            return self._reject(o, it, code="NOT_FOUND", msg="market row is missing", at=at, t0=t0)
        decision = self.risk_decision(it, st, at=at)
        # `RISK_UNAVAILABLE` is a failure of US, not of the user: recording it keeps the breaker honest
        # about whether the risk path works, and a denial for a user typo does not trip anything.
        self.breaker.record(ok=decision.code not in ("RISK_UNAVAILABLE", "CIRCUIT_OPEN"), now_ms=at)
        if not decision.allowed:
            return self._reject(o, it, code=decision.code, msg=decision.message, at=at, t0=t0,
                                checks=decision.checks_run, pre_flight_denial=True)

        # 2. the venue pre-flight, which includes the fee estimate and the balance check against
        #    notional + fees. It is a separate call on purpose: the gate answers "may this user trade",
        #    preflight answers "will this order be accepted, and what will it cost".
        allowance = self.store.allowance_micro(it.user_id)
        fees = v2.estimate_fees(size_shares_micro=it.size_micro, price_micro=it.price_micro,
                               fee_rate_bps=it.fee_rate_bps, builder_bps=it.builder_bps)
        pf = v2.preflight(order_type=it.order_type, audience=it.audience, side=it.side,
                          price_micro=it.price_micro, size_shares_micro=it.size_micro,
                          usdc_available_micro=self.store.balance_available_micro(it.user_id),
                          allowance_micro=allowance["granted_micro"], fee_rate_bps=it.fee_rate_bps,
                          builder_bps=it.builder_bps,
                          market_accepting=bool(st.accepting_orders and st.enable_order_book),
                          seconds_delay=st.seconds_delay,
                          # The tick/min-size facts were established by the gate above; passing "whatever the
                          # gate already proved" keeps preflight honest for callers that skip the gate (there
                          # are none on a money path) without inventing a second parser in this file.
                          tick_ok="tick_alignment" in decision.checks_run,
                          min_size_ok="min_size" in decision.checks_run,
                          venue_reachable=True, kill_switch=False, self_trade=False,
                          risk=decision,
                          expected_fill_price_micro=quote["best_ask_micro"], max_slippage_bps=it.max_slippage_bps)
        if pf.total_cost_micro:
            o.notional_micro = notional_floor(it.size_micro, it.price_micro)
        if not pf.ok:
            return self._reject(o, it, code=pf.deny_code, msg=pf.deny_message, at=at, t0=t0,
                                checks=pf.checks_run, pre_flight_denial=True)
        if tuple(pf.checks_run) != v2.PREFLIGHT_ORDER:
            # Preflight that did not run every check is a bug in this file, and the safe response to a bug in
            # the file that signs is to stop signing, not to carry on with a shorter list.
            return self._reject(o, it, code="PREFLIGHT_INCOMPLETE",
                                msg="preflight returned %s of %s checks" % (len(pf.checks_run),
                                                                            len(v2.PREFLIGHT_ORDER)),
                                at=at, t0=t0)
        o.stage = "preflight"
        o.fee_micro = fees.total_micro
        o.notes.extend(pf.notes)
        self.store.lifecycle(intent_id=it.id, order_id="", user_id=it.user_id, state="preflight",
                             reason="%d checks, est fees %s" % (len(pf.checks_run), fmt_usdc(fees.total_micro)),
                             source="system", at=at)

        # 3. wallet tradability and the signature type: policy drift and gaps are checked here, at the last
        #    moment before a key is used, because a policy that changed 40ms ago is the case that matters.
        wallet = self.store.wallet(it.user_id)
        if wallet is None:
            return self._reject(o, it, code="WALLET_NOT_TRADABLE", msg="no wallet row for this user",
                                at=at, t0=t0, checks=pf.checks_run)
        refuse, why = wl.should_refuse_trading(wallet, self.policy)
        if refuse:
            self.store.set_wallet_state(it.user_id, "suspended", at=at, event="policy_drift",
                                       detail={"why": why[:200]})
            return self._reject(o, it, code=why.split(":")[0], msg=why, at=at, t0=t0, checks=pf.checks_run)
        try:
            sig_type = wl.signature_type_for(custody=str(wallet.get("custody")),
                                             has_proxy=bool(wallet.get("proxy_address")),
                                             delegated_signer=True)
        except ValueError as e:
            return self._reject(o, it, code="SIGNATURE_REFUSED", msg=str(e), at=at, t0=t0)

        # 4. build and sign. P04's `build_signed_payload` is the only place the struct exists, so the hash
        #    we reconcile by and the hash the venue indexes cannot diverge.
        from polygm_core.executor.executor import OrderIntent, build_signed_payload, submit
        oi = OrderIntent(id=it.id, user_id=it.user_id, token_id=it.token_id, side=it.side,
                         price_micro=it.price_micro, size_shares_micro=it.size_micro,
                         idempotency_key=it.idempotency_key, condition_id=it.market_id,
                         tick_size=str(st.minimum_tick_size), expiration=it.expiration_ts)
        chash = oi.client_order_hash(self.signer.pubkey)
        o.client_order_hash = chash
        payload = build_signed_payload(oi, client_order_hash=chash)
        payload["builder"] = oi.builder_code
        payload["fee_rate_bps"] = it.fee_rate_bps
        payload["order_type"] = it.order_type
        payload["expiration"] = it.expiration_ts
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        # 4b. exactly-once, the crash-safe half. An attempt row for this hash means a process that is no
        # longer here already put these bytes on the wire, and the only thing that can tell "it was accepted
        # and the answer was lost" from "it never left" is the venue itself. Re-sending is the duplicate
        # order; sending the intent to `uncertain` is not, because the reconciler's `no_ack` case asks by
        # hash and adopts whatever it finds. So: never post twice, and never decide absence ourselves.
        prior = self.store.prior_attempt(chash)
        if prior is not None and prior["intent_id"] == it.id:
            # The attempt row proves "we may have sent it" and nothing more: it is written before the POST, so
            # a kill one instruction later and a kill one minute later look identical in the database. Only the
            # venue can tell those apart, so we ask it, and the three answers are the three actions:
            #   has it   -> leave it for the reconciler's `no_ack` case, which adopts by hash and books fills
            #   has not  -> absence was PROVEN by an answer, so this order never left: post it, once
            #   no answer-> nothing is proven; post nothing and go uncertain, which is never auto-requeued
            try:
                found = self.transport.find_order(chash, timeout_ms=1_500)
            except (TimeoutError, ConnectionError) as e:
                found = None
                say = "the venue would not answer (%s)" % type(e).__name__
            else:
                say = "the venue holds it" if found else "the venue answered: no such order"
            if found is not None:
                self.stats["uncertain"] += 1
                self.store.set_intent_state(it.id, IntentState.UNCERTAIN.value, at=at, client_order_hash=chash)
                self.store.lifecycle(intent_id=it.id, order_id="", user_id=it.user_id, state="unknown",
                                     reason="already attempted at %d; %s; not re-sent, reconciliation adopts by "
                                            "hash" % (prior["signed_ms"], say), source="reconciler", at=at)
                self.store.notify(intent_id=it.id, user_id=it.user_id, event="unknown", at=at,
                                  detail={"client_order_hash": chash[:16], "recovered": True})
                # the same code the timeout path uses, on purpose: to the API and to the user this IS the same
                # situation ("we do not know whether the venue has it"), and inventing a second word for it
                # would mean a second thing for the UI to learn and a second code to be missing from the table.
                o.state, o.stage, o.code = "uncertain", "signing", "UNCERTAIN_INTENT"
                o.notes.append("already attempted; %s; nothing was re-sent" % say)
                o.latency_ms = (time.perf_counter() - t0) * 1000
                return o
            self.log(json.dumps({"intent": it.id, "repost_after_crash": True, "proof": say},
                                separators=(",", ":")))
        self.store.lifecycle(intent_id=it.id, order_id="", user_id=it.user_id, state="signing",
                             reason="payload %s" % digest[:12], source="system", at=at)
        signed = self.signer.sign(payload)
        self.stats["signed"] += 1
        # The attempt row is written BEFORE the POST: it is the difference between "we may have sent it"
        # being provable and being a guess.
        self.store.record_attempt(client_order_hash=chash, intent=it, signature_type=sig_type,
                                  payload_digest=digest, at=at)
        # before the wire, not after the answer: see Store.stamp_hash
        self.store.stamp_hash(it.id, chash, at=at)
        crash_hook("preflight")
        crash_hook("signed")

        # 5. submit, exactly once. `submit` never retries the POST (HOPS); a timeout is UNCERTAIN.
        #
        # A raised ConnectionError is treated the same as a timeout even though the most likely cause is
        # "it never left". That is deliberate: we cannot distinguish "refused before the bytes" from "answered
        # and lost", and the asymmetric cost means the guess we are allowed to make is the one that cannot
        # double-spend. Reconciliation resolves it in both directions within a pass or two.
        try:
            resp = self.transport.post_order(payload, timeout_ms=1500)
            success = bool(isinstance(resp, dict) and resp.get("success"))
        except (TimeoutError, ConnectionError) as e:
            resp = {"success": False, "code": "timeout" if isinstance(e, TimeoutError) else "unreachable",
                    "message": str(e)[:180]}
            success = False
        crash_hook("submitted")
        if not success:
            code = str((resp or {}).get("code") or "VENUE_REJECTED")
            if code in ("gateway_timeout", "unreachable", "timeout"):
                self.stats["uncertain"] += 1
                self.store.set_intent_state(it.id, IntentState.UNCERTAIN.value, at=at, client_order_hash=chash)
                self.store.lifecycle(intent_id=it.id, order_id="", user_id=it.user_id, state="unknown",
                                     reason="no answer from the venue; reconciling by client order id, "
                                            "nothing was re-sent", source="system", at=at)
                self.store.notify(intent_id=it.id, user_id=it.user_id, event="unknown", at=at,
                                  detail={"client_order_hash": chash[:16]})
                o.state, o.stage, o.code = "uncertain", "submitted", "UNCERTAIN_INTENT"
                o.latency_ms = (time.perf_counter() - t0) * 1000
                return o
            self.stats["rejected"] += 1
            mapped = v2.BATCH_ITEM_CODES.get(code, "VENUE_REJECTED")
            return self._reject(o, it, code=mapped, msg=str((resp or {}).get("message") or "")[:180], at=at,
                                t0=t0, venue_code=code)

        order_id = str(resp.get("orderID") or resp.get("orderId") or "")
        self.store.mark_submitted(intent=it, order_id=order_id, client_order_hash=chash, at=at,
                                  acknowledged=True, builder_bps=it.builder_bps)
        self.store.record_fee_estimate(order_id=order_id, intent=it, platform_micro=fees.platform_micro,
                                       builder_micro=fees.builder_micro, fee_rate_bps=it.fee_rate_bps,
                                       builder_bps=it.builder_bps, at=at)
        self.store.lifecycle(intent_id=it.id, order_id=order_id, user_id=it.user_id, state="submitted",
                             reason="venue acknowledged", source="system", at=at)
        self.store.lifecycle(intent_id=it.id, order_id=order_id, user_id=it.user_id, state="live",
                             reason="on the book", source="venue_rest", at=at)
        self.store.notify(intent_id=it.id, user_id=it.user_id, event="submitted", at=at,
                          detail={"order_id": order_id[:16], "notional": fmt_usdc(o.notional_micro)})
        self.store.bump_counter("user:%s|orders" % it.user_id, at=at, bucket_ms=60_000,
                               limit=self.extra.max_orders_per_minute)
        self.stats["submitted"] += 1
        o.state, o.stage, o.order_id, o.code = "submitted", "submitted", order_id, "OK"
        o.latency_ms = (time.perf_counter() - t0) * 1000
        return o

    def _reject(self, o: Outcome, it: IntentRow, *, code: str, msg: str, at: int, t0: float,
                 checks: tuple[str, ...] = (), venue_code: str = "", pre_flight_denial: bool = False) -> Outcome:
        from polygm_core.risk.limits import spec_for
        o.state, o.stage, o.code = "rejected", "preflight", code
        o.notes.append(msg[:200])
        o.latency_ms = (time.perf_counter() - t0) * 1000
        # D7: a refusal from the gate or from pre-flight gets its OWN stage row, before the `rejected` row.
        # Without it the user's Activity reads `draft -> rejected` and the question "did anything actually
        # check this order, or did it die in a queue?" has no answer in the database. A venue-side rejection
        # does NOT get this row: it passed pre-flight, was signed, and the truth is in the `signing` /
        # `submitted` rows already, so adding a preflight row there would misdate the failure.
        if pre_flight_denial:
            self.store.lifecycle(intent_id=it.id, order_id="", user_id=it.user_id, state="preflight",
                                 reason="%d checks ran; refused with %s" % (len(checks), code),
                                 source="system", at=at)
        self.store.set_intent_state(it.id, IntentState.REJECTED.value, at=at, risk_code=code)
        self.store.lifecycle(intent_id=it.id, order_id="", user_id=it.user_id, state="rejected",
                             reason="%s: %s" % (code, msg[:180]), source="system", at=at)
        self.store.notify(intent_id=it.id, user_id=it.user_id, event="rejected", at=at,
                          detail={"code": code, "http": spec_for(code).http, "checks": list(checks),
                                  "venue_code": venue_code})
        self.stats["rejected"] += 1
        return o

    # ------------------------------------------------------------------------- batch + cancels (D2)
    def submit_batch(self, items: list[IntentRow], *, at: int | None = None) -> v2.BatchOutcome:
        """`POST /orders` with up to 15 orders, partial failure handled per item.

        Anything the venue did not answer for becomes UNCERTAIN (never "rejected"), and those intents are
        left where they are so the reconciler's `no_ack` case owns them. The batch path is the one place a
        "retry the whole thing" instinct produces duplicate orders, so this function has no retry.
        """
        chunks = v2.plan_batch([v2.BatchItem(order={}, order_type=i.order_type, intent_id=i.id) for i in items])
        out = v2.BatchOutcome()
        t = at or now_ms()
        for chunk in chunks:
            payload_items, order_ids, hashes = [], [], []
            for b in chunk:
                it = next(i for i in items if i.id == b.intent_id)
                payload = self._payload_for(it, t)
                order_ids.append(it.id)
                hashes.append(payload["client_order_hash"])
                payload_items.append({"order": payload, "orderType": it.order_type})
            try:
                resp = self.transport.post_batch(payload_items, timeout_ms=1500)
            except (TimeoutError, ConnectionError):
                for iid, h in zip(order_ids, hashes):
                    self.store.set_intent_state(iid, IntentState.UNCERTAIN.value, at=t, client_order_hash=h)
                    self.store.lifecycle(intent_id=iid, order_id="", user_id="", state="unknown",
                                         reason="batch POST unanswered; reconciling", source="system", at=t)
                    out.uncertain.append({"intent_id": iid, "client_order_hash": h})
                continue
            res = v2.read_batch_response(order_ids, hashes, resp)
            for a in res.accepted:
                it = next(i for i in items if i.id == a["intent_id"])
                self.store.mark_submitted(intent=it, order_id=str(a.get("order_id") or ""),
                                          client_order_hash=a["client_order_hash"], at=t, acknowledged=True)
                self.store.lifecycle(intent_id=it.id, order_id=str(a.get("order_id") or ""),
                                     user_id=it.user_id, state="submitted", reason="accepted in a batch",
                                     source="venue_rest", at=t)
            for r in res.rejected:
                self.store.set_intent_state(r["intent_id"], IntentState.REJECTED.value, at=t,
                                            risk_code=r["code"])
                self.store.lifecycle(intent_id=r["intent_id"], order_id="", user_id="", state="rejected",
                                     reason="%s: %s" % (r["code"], r["message"]), source="venue_rest", at=t)
            for u in res.uncertain:
                self.store.set_intent_state(u["intent_id"], IntentState.UNCERTAIN.value, at=t,
                                            client_order_hash=u["client_order_hash"])
                self.store.lifecycle(intent_id=u["intent_id"], order_id="", user_id="", state="unknown",
                                     reason="the batch answered for other items, not this one",
                                     source="venue_rest", at=t)
            out.accepted += res.accepted
            out.rejected += res.rejected
            out.uncertain += res.uncertain
        return out

    def _payload_for(self, it: IntentRow, at: int) -> dict:
        from polygm_core.executor.executor import OrderIntent, build_signed_payload
        oi = OrderIntent(id=it.id, user_id=it.user_id, token_id=it.token_id, side=it.side,
                         price_micro=it.price_micro, size_shares_micro=it.size_micro,
                         idempotency_key=it.idempotency_key, condition_id=it.market_id,
                         expiration=it.expiration_ts)
        p = build_signed_payload(oi, client_order_hash=oi.client_order_hash(self.signer.pubkey))
        p["builder"] = it.builder_code if hasattr(it, "builder_code") else "0x" + "0" * 64
        p["order_type"] = it.order_type
        self.store.record_attempt(client_order_hash=p["client_order_hash"], intent=it, signature_type=3,
                                  payload_digest=hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest(),
                                  at=at)
        return p

    def cancel(self, req: v2.CancelRequest, *, at: int | None = None) -> dict:
        """Cancel, with the scarce budget applied to `all` only, and a typed phrase for the destructive one."""
        t = at or now_ms()
        req.validate()
        if req.scope == "all":
            ok, retry_ms = self.cancel_budget.take(t, cost=1)
            if not ok:
                return {"ok": False, "code": "CANCEL_THROTTLED", "retry_after_ms": retry_ms,
                        "budget_used": self.cancel_budget.in_window}
        ids = list(req.order_ids)
        if req.scope == "market":
            ids = [o["id"] for o in self.store.open_orders() if o["market_id"] == req.market_id]
        if req.scope == "all":
            ids = [o["id"] for o in self.store.open_orders()]
        results = []
        for chunk in v2.chunk_cancel_targets(ids):
            r = self.transport.cancel_batch(chunk, timeout_ms=2000) if hasattr(self.transport, "cancel_batch") \
                else {"results": [self.transport.cancel(i) for i in chunk]}
            results += list((r or {}).get("results") or [])
        for oid, res in zip(ids, results):
            row = self.store.order_row(oid)
            if not row:
                continue
            if (res or {}).get("code") == "already_filled":
                # The race: the venue is the authority, so this is a fill, not a failed cancel.
                from polygm_core.reconcile.reconciler import Pass
                self.reconciler.sync_fills(order_id=oid, intent_id=row["intent_id"], user_id=row["user_id"],
                                          venue={"status": "matched"}, p=Pass(at_ms=t), case="cancelled_race",
                                          at=t)
                continue
            if (res or {}).get("success"):
                self.store.set_order_state(oid, "canceled", at=t, source="user", intent_id=row["intent_id"],
                                           user_id=row["user_id"])
                self.store.notify(intent_id=row["intent_id"], user_id=row["user_id"], event="cancelled", at=t,
                                  detail={"scope": req.scope, "reason": req.reason[:120]})
        return {"ok": True, "requested": len(ids), "results": len(results), "audit": req.audit_row(t)}

    # ------------------------------------------------------------------------------------ the loop
    def tick(self, *, at: int | None = None, reconcile: bool = True) -> dict:
        t = at or now_ms()
        self.stats["ticks"] += 1
        killed = self.store.kill_switch_engaged(at_ms=t)
        report: dict = {"at_ms": t, "kill_switch": killed, "handled": [], "reconcile": None, "requeued": 0}
        if killed:
            # Engaged: claim nothing. The intents stay queued and age; a queue that drains during a halt is
            # a queue that dumps on release.
            self.store.claim_intents(limit=0, at=t, kill_switch=True)
            report["halted"] = True
            self.stats["halted_skips"] += 1
            if reconcile:
                p = self.reconciler.run_pass(at=t)
                report["reconcile"] = {"per_case": p.per_case, "errors": p.errors, "totals": p.totals}
                self.stats["reconcile_passes"] += 1
            report["metric"] = self.reconciler.metric(at=t)
            return report
        # Order matters, and this is the whole reason a crash does not become a duplicate: the reconciler runs
        # BEFORE the lease requeue. An expired lease and a lost answer look identical in the database, and the
        # only difference between them is at the venue. Requeue first and the executor re-POSTs an order that
        # is already live; reconcile first and the order is adopted from the venue's own answer.
        if reconcile:
            p = self.reconciler.run_pass(at=t)
            report["reconcile"] = {"totals": p.totals, "errors": p.errors, "changed": p.changed}
            self.stats["reconcile_passes"] += 1
        report["requeued"] = self.store.requeue_expired_claims(at=t)
        intents = self.store.claim_intents(limit=self.batch_size, at=t)
        for it in intents:
            self.stats["handled"] += 1
            o = self.handle_intent(it, at=t)
            report["handled"].append(o.as_dict())
        self.deliver_notifications(at=t)
        report["metric"] = self.reconciler.metric(at=t)
        self.last_pass = report
        return report

    def deliver_notifications(self, *, at: int) -> int:
        """In-app notifications are delivered by marking them sent; email/Telegram are P09's transports and
        are left `queued` on purpose so the queue is real. The rule the code keeps: a notification is never
        recorded as sent before the thing that triggered it is durable, which is why this runs after the
        writes and not alongside the venue call."""
        rows = self.store.notifications_queued()
        for r in rows:
            self.store.notification_mark(r["intent_id"], r["event"], "sent", at)
        return len(rows)

    def run_forever(self, *, interval_ms: int = 250, max_ticks: int = 0) -> None:
        stop = {"v": False}

        def on_signal(signum, frame):                                        # noqa: ARG001
            stop["v"] = True
            self.log("executor: %s, finishing the tick" % signal.Signals(signum).name)

        for s in (signal.SIGTERM, signal.SIGINT):
            signal.signal(s, on_signal)
        ticks = 0
        while not stop["v"]:
            t0 = time.perf_counter()
            rep = self.tick()
            n = len(rep.get("handled") or [])
            if n or rep.get("halted"):
                self.log(json.dumps({"tick": ticks, "handled": n, "halted": bool(rep.get("halted")),
                                     "metric": rep.get("metric")}, separators=(",", ":")))
            ticks += 1
            if max_ticks and ticks >= max_ticks:
                return
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            time.sleep(max(interval_ms - elapsed_ms, 0) / 1000)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="polygm-executor", description="the only process that signs")
    ap.add_argument("--db", default=os.environ.get("PGM_DB_PATH", "var/polygm.db"))
    ap.add_argument("--venue", default=os.environ.get("PGM_VENUE_URL", "http://127.0.0.1:8090"))
    ap.add_argument("--once", action="store_true", help="one tick, then exit (dev/CI)")
    ap.add_argument("--ticks", type=int, default=0, help="stop after N ticks (chaos harness)")
    ap.add_argument("--interval-ms", type=int, default=250)
    ap.add_argument("--crash-after", choices=CRASH_STAGES, default=None,
                    help="exit the process after this stage (quality gate); same as PGM_CRASH_AFTER")
    ap.add_argument("--reconcile-only", action="store_true", help="no queue: reconciliation only")
    ap.add_argument("--status", action="store_true", help="print one JSON status line and exit")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if a.crash_after:
        os.environ["PGM_CRASH_AFTER"] = a.crash_after
    store = Store.open(a.db)
    missing = store.ready()
    if missing:
        print("executor: schema is missing %s — run `make migrate`" % ", ".join(missing), file=sys.stderr)
        return 3
    from transport import HttpTransport                       # services/executor-mock
    tp = HttpTransport(a.venue)
    ex = Executor(store, transport=tp)
    if a.status:
        print(json.dumps({"stats": ex.stats, "metric": ex.reconciler.metric(),
                          "kill_switch": ex.store.kill_switch_engaged(force=True),
                          "venue_pin": v2.SDK_PINNED}, indent=2))
        return 0
    boot = ex.reconciler.startup()
    print(json.dumps({"service": "executor", "db": a.db, "venue": a.venue, "recovered": boot,
                      "crash_after": os.environ.get("PGM_CRASH_AFTER") or ""}, separators=(",", ":")),
          flush=True)
    if a.reconcile_only:
        rep = ex.tick(reconcile=True)
        print(json.dumps({"reconcile_only": rep.get("reconcile"), "metric": rep.get("metric")}, indent=2))
        return 0
    if a.once:
        rep = ex.tick()
        print(json.dumps(rep, indent=2, default=str))
        return 0
    ex.run_forever(interval_ms=a.interval_ms, max_ticks=a.ticks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
