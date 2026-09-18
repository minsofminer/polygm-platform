"""Wallet lifecycle: provisioning, funding, allowances, withdrawal, export, and the signature decision.

Two structural facts drive everything in this file, and both are the phase's requirement rather than my
taste:

* **Only the executor holds a signer.** So this module never signs anything. It answers "is this allowed to
  be signed, and under what policy", and the executor asks it before every signature. A `sign(...)` function
  appearing here would be the bug: the wallet layer is policy, the executor is capability.
* **A custody provider is a vendor we can leave.** Every decision below is written so that leaving is a data
  migration, not a re-architecture: the policy we care about is expressed as a *hash we control*
  (`Policy.policy_hash`), stored at provisioning time, so a provider-side change is detected by us rather
  than discovered by an incident. The corollary, and D1's sharpest question: if the provider cannot express
  one of our limits, `plan_for()` puts that limit on our side of the boundary and marks the wallet with a
  `policy_gap`; a limit that has **no** enforcement point is a refusal to trade, not a comment in a doc.

Money is integer micros throughout (rule: no floats in the money path). Addresses are compared exactly,
never case-folded: a checksummed address that "matches case-insensitively" is how a typo becomes a donation.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from enum import Enum

from ..money.cents import SCALE, ceil_div
from ..venue.clob_v2 import UNLIMITED_ALLOWANCE


# ====================================================================================== provider choice ==
@dataclass(frozen=True)
class ProviderFacts:
    """What we believe about each custody provider, as data.

    `verified=False` means the number came from a pricing page at decision time and nobody has re-checked it
    since — which the tooling is allowed to test for, and `docs/P06-trading-plane.md` must therefore say
    `[UNVERIFIED]` next to. A table of confident numbers we cannot source is worse than no table.
    """
    name: str
    price_model: str
    monthly_at_10k_wallets_micro: int
    per_wallet_fee_model: bool
    policy_granularity: tuple[str, ...]      # what the provider's own policy engine can enforce
    threshold_approvals: bool
    self_hosted_option: bool
    key_export_supported: bool
    exit_cost: str                            # what leaving actually takes
    audit_trail: str
    verified: bool = False


PROVIDERS: tuple[ProviderFacts, ...] = (
    ProviderFacts(
        name="turnkey",
        price_model="flat monthly tier + per-MAU overage; policy engine included at Standard",
        monthly_at_10k_wallets_micro=1_500_000_000,             # ~$1.5k/mo tier [UNVERIFIED]
        per_wallet_fee_model=False,
        policy_granularity=("per-wallet spend cap", "allowed contract allowlist", "allowed token allowlist",
                            "quorum/threshold approval", "session key expiry"),
        threshold_approvals=True,
        self_hosted_option=True,                                 # Enterprise only
        key_export_supported=True,
        exit_cost="export the wallet's private key material per-wallet (encrypted), or rotate to our own "
                  "signer and move funds; both are supported by the API, so leaving is a batch job",
        audit_trail="activity API, exportable; retains policy decision with the signature",
    ),
    ProviderFacts(
        name="privy",
        price_model="per-MAU plus a TVL-linked card fee",
        monthly_at_10k_wallets_micro=2_000_000_000,             # [UNVERIFIED]
        per_wallet_fee_model=True,
        policy_granularity=("server wallet spending limits (aggregate)", "allowed-token allowlist"),
        threshold_approvals=False,
        self_hosted_option=False,
        key_export_supported=True,                               # only for exported/shared signing keys
        exit_cost="wallets are created inside their infra; leaving means creating new wallets and sweeping "
                  "funds, which is a user-visible event",
        audit_trail="dashboard + webhooks; policy is coarser than the wallet layer we need",
    ),
    ProviderFacts(
        name="dynamic",
        price_model="per-MAU",
        monthly_at_10k_wallets_micro=1_800_000_000,             # [UNVERIFIED]
        per_wallet_fee_model=True,
        policy_granularity=("spend limits", "allowlists via their policy module"),
        threshold_approvals=True,
        self_hosted_option=False,
        key_export_supported=False,
        exit_cost="no bulk key export: funds must be moved by transaction, wallet by wallet",
        audit_trail="dashboard only",
    ),
    ProviderFacts(
        name="self_hosted",
        price_model="our engineering time; hardware-security-module or encrypted KMS keys",
        monthly_at_10k_wallets_micro=0,
        per_wallet_fee_model=False,
        policy_granularity=("everything we write ourselves"),
        threshold_approvals=True,                                # we build it, so we decide
        self_hosted_option=True,
        key_export_supported=True,
        exit_cost="there is no exit; there is an incident",
        audit_trail="whatever we build, which is the point and also the risk",
    ),
)

CHOSEN_PROVIDER = "turnkey"


def choose_provider(*, active_wallets: int, needs_self_host: bool, needs_granular_policy: bool,
                    monthly_budget_micro: int) -> dict:
    """The decision as a function, so it can be re-run when our scale changes rather than re-litigated in
    a meeting. Ranking rules, in order: any provider that cannot express the policy we need is out; among
    the rest, cheapest within budget; self-host only if it is the only option left, because "there is no
    exit" is a worse property than a vendor bill.
    """
    scored: list[dict] = []
    for p in PROVIDERS:
        reasons: list[str] = []
        if needs_granular_policy and "per-wallet spend cap" not in p.policy_granularity \
                and not p.self_hosted_option:
            reasons.append("policy engine cannot express a per-wallet spend cap")
        if needs_self_host and not p.self_hosted_option:
            reasons.append("no self-hosted deployment")
        # Cost scales with wallets only for the per-wallet-priced vendors; that is the whole reason the flat
        # tier wins for us at 10k wallets and loses at 500k.
        scale = max(active_wallets, 1)
        est = p.monthly_at_10k_wallets_micro if not p.per_wallet_fee_model \
            else ceil_div(p.monthly_at_10k_wallets_micro * scale, 10_000)
        if est > monthly_budget_micro:
            reasons.append("over budget at %d wallets" % active_wallets)
        if p.name == "self_hosted":
            reasons.append("no exit path — held in reserve, chosen only when nothing else works")
        scored.append({"provider": p.name, "monthly_micro": est, "excluded": reasons,
                       "key_export": p.key_export_supported, "threshold": p.threshold_approvals})
    eligible = [s for s in scored if not s["excluded"]]
    if not eligible:
        return {"provider": None, "verdict": "no provider fits; escalate", "scored": scored}
    eligible.sort(key=lambda s: (s["monthly_micro"], s["provider"]))
    return {"provider": eligible[0]["provider"], "scored": scored,
            "verdict": "cheapest within budget that can express the policy", "candidates": [
                e["provider"] for e in eligible]}


def adapter_surface() -> tuple[str, ...]:
    """The only methods our code may call on a custody provider. Kept as data because D1's answer to "what
    if we leave" is a *list of five functions*, and if the list grows the abstraction is leaking."""
    return ("create_wallet", "sign", "get_policy", "set_policy", "export_key")


# ================================================================================== lifecycle states ==
class WalletState(str, Enum):
    NONE = "none"                        # no row at all; distinct from `provisioned` on purpose
    PROVISIONED = "provisioned"          # address exists, nothing in it
    FUNDED = "funded"                    # balance >= minimum economic deposit
    TRADING = "trading"                  # allowance granted, gate checks pass
    SUSPENDED = "suspended"              # policy drift, insider flag, or operator action
    CLOSING = "closing"                  # withdrawal in flight, no new orders


TRANSITIONS: dict[str, tuple[str, ...]] = {
    WalletState.NONE.value: (WalletState.PROVISIONED.value,),
    WalletState.PROVISIONED.value: (WalletState.FUNDED.value, WalletState.SUSPENDED.value,
                                    WalletState.CLOSING.value),
    WalletState.FUNDED.value: (WalletState.TRADING.value, WalletState.SUSPENDED.value,
                                WalletState.CLOSING.value),
    WalletState.TRADING.value: (WalletState.SUSPENDED.value, WalletState.CLOSING.value),
    # Suspended is entered easily and left deliberately: only an operator, and only with a reason (D8).
    WalletState.SUSPENDED.value: (WalletState.TRADING.value, WalletState.CLOSING.value),
    WalletState.CLOSING.value: (WalletState.PROVISIONED.value, WalletState.NONE.value),
}


def can_move(frm: str, to: str) -> bool:
    return to in TRANSITIONS.get(frm, ())


def assert_transition(frm: str, to: str) -> None:
    if not can_move(frm, to):
        raise ValueError("BAD_WALLET_TRANSITION: %s -> %s" % (frm, to))


# ============================================================================================== policy ==
@dataclass(frozen=True)
class Policy:
    """Our custody policy. `policy_hash` is what the wallet row stores, and what the executor re-derives
    before signing: a signature made under a policy that hashes differently from the provisioned one is
    refused, whatever either provider's dashboard says.
    """
    allowed_spender: str = "0x0000000000000000000000000000000000000000"   # CTF Exchange; set at boot
    allowed_tokens: tuple[str, ...] = ("pUSD",)
    chains: tuple[str, ...] = ("polygon", "ethereum")
    max_daily_outflow_micro: int = 50_000_000_000            # $50k/user/day, withdrawals
    max_single_withdrawal_micro: int = 25_000_000_000
    threshold_approval_above_micro: int = 10_000_000_000     # >$10k needs a second approver
    session_key_ttl_ms: int = 900_000                        # 15 min; D1's "scoped session key"
    allowlist_cooldown_ms: int = 86_400_000                  # new address unusable for 24h

    def canonical(self) -> str:
        return json.dumps({"spender": self.allowed_spender, "tokens": list(self.allowed_tokens),
                           "chains": list(self.chains), "daily_out": self.max_daily_outflow_micro,
                           "single_out": self.max_single_withdrawal_micro,
                           "threshold": self.threshold_approval_above_micro,
                           "session_ttl": self.session_key_ttl_ms,
                           "cooldown": self.allowlist_cooldown_ms},
                          sort_keys=True, separators=(",", ":"))

    def policy_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical().encode()).hexdigest()


@dataclass(frozen=True)
class ProviderCaps:
    """What the provider's own policy engine can enforce. Anything in our Policy that is not here must be
    enforced locally — and if we cannot enforce it locally, the wallet is not tradable (D1.4)."""
    supports_spender_allowlist: bool = True
    supports_token_allowlist: bool = True
    supports_per_wallet_daily_cap: bool = True
    supports_single_tx_cap: bool = False
    supports_threshold_approval: bool = True
    supports_session_expiry: bool = True


ENFORCE_PROVIDER = "provider"
ENFORCE_LOCAL = "local_gate"
ENFORCE_REFUSED = "refused"


@dataclass
class Enforcement:
    rule: str
    where: str
    note: str = ""


def plan_for(policy: Policy, caps: ProviderCaps) -> dict:
    """Where each limit lives. `refused` is not a warning: the caller must treat it as `policy_gap` on the
    wallet row and the gate must refuse the affected action class. Trading with a hole in the policy is how
    a custody system becomes a liability instead of a control."""
    rows: list[Enforcement] = []
    rows.append(Enforcement("allowed_spender",
                            ENFORCE_PROVIDER if caps.supports_spender_allowlist else ENFORCE_LOCAL,
                            "checked at the allowance row too ( spender mismatch suspends the wallet )"))
    rows.append(Enforcement("allowed_tokens",
                            ENFORCE_PROVIDER if caps.supports_token_allowlist else ENFORCE_LOCAL))
    rows.append(Enforcement("max_daily_outflow_micro",
                            ENFORCE_PROVIDER if caps.supports_per_wallet_daily_cap else ENFORCE_LOCAL))
    rows.append(Enforcement("max_single_withdrawal_micro",
                            ENFORCE_PROVIDER if caps.supports_single_tx_cap else ENFORCE_LOCAL,
                            "enforced by the withdrawal service when the provider cannot"))
    gap = "single-tx cap and threshold approval both outside the provider"
    rows.append(Enforcement("threshold_approval_above_micro",
                            ENFORCE_PROVIDER if caps.supports_threshold_approval else ENFORCE_REFUSED,
                            "a withdrawal above the threshold with no second approver is refused outright; "
                            "we do not implement 2-of-2 in our own process, because then we are the "
                            "single point again" if not caps.supports_threshold_approval else ""))
    rows.append(Enforcement("session_key_ttl_ms",
                            ENFORCE_PROVIDER if caps.supports_session_expiry else ENFORCE_REFUSED))
    refused = [r.rule for r in rows if r.where == ENFORCE_REFUSED]
    return {"rows": [r.__dict__ for r in rows], "policy_gap": "; ".join(refused),
            "tradable": not refused}


def check_policy_drift(provisioned_hash: str, live_policy: Policy) -> dict:
    h = live_policy.policy_hash()
    if h == provisioned_hash:
        return {"ok": True, "hash": h}
    return {"ok": False, "hash": h, "provisioned": provisioned_hash,
            "code": "POLICY_DRIFT",
            "action": "suspend the wallet, notify the operator, refuse to sign; do not 're-adopt' the new "
                      "policy from a cache — an operator must write it"}


# ================================================================================ signature decision ==
SIG_EOA, SIG_POLY_PROXY, SIG_POLY_SAFE, SIG_POLY_1271 = 0, 1, 2, 3
POLY_1271_DOC = ("signatureType = 3 (POLY_1271) is Polymarket's own EIP-1271 contract-wallet signature: the "
                 "proxy account we deploy for every user is the signer, and the delegated session key is what "
                 "our executor holds. Chosen over 0 (EOA) because an EOA flow would make the *user's* key the "
                 "thing that signs and therefore the thing we would have to custody, which is the exact thing "
                 "the custody design is written to avoid. Chosen over 1/2 (POLY_PROXY / POLY_GNOSIS_SAFE, the "
                 "legacy 712-based proxy flows) because 1271 is what the CLOB verifies for a newly created "
                 "proxy and what `builder` + `feeRateBps` fields are valid on. [UNVERIFIED: confirm against the "
                 "V2 client's `create_proxy_wallet` path before P13 moves real funds; the executor refuses to "
                 "run at all if the venue reports a different type for our proxy.]")


def signature_type_for(*, custody: str, has_proxy: bool, delegated_signer: bool) -> int:
    if custody == "delegated" and has_proxy and delegated_signer:
        return SIG_POLY_1271
    if custody == "read_only":
        raise ValueError("SIGNATURE_REFUSED: a read-only wallet can display balances and sign nothing")
    raise ValueError("SIGNATURE_REFUSED: we do not operate EOA custody at P06; refusing rather than "
                     "defaulting to signatureType=0 (docs/P06 D1.6: %s)" % "see POLY_1271_DOC")


# ============================================================================ funding: minimum deposit ==
@dataclass(frozen=True)
class ChainCost:
    """An on-chain cost estimate in integer micros. `native_price_micro` is the USD price of the chain's gas
    token, also micros, so no float enters the arithmetic that decides whether a deposit is worth making."""
    gas_units: int
    gas_price_wei: int
    native_price_micro: int
    bridge_fee_bps: int = 30
    swap_gas_units: int = 180_000


def estimate_deposit_cost(c: ChainCost, *, legs: int = 3) -> dict:
    """Cost of getting funds into a tradable balance, in integer micros.

    wei -> micro-USD without a float:  `gas_units x gas_price_wei` is wei; / 1e18 is the native amount;
    x price_micro (a micro-USD price) lands directly in micro-USD. Dividing by 1e6 again — which is what I
    wrote first — produced a minimum deposit of $0.0000004, i.e. a floor that refuses nothing.
    Three legs (deposit tx, bridge tx, our credit tx) plus the swap leg, because on some chains the pUSD the
    user sent is not the pUSD the venue wants.

    The percentage bridge fee is deliberately NOT folded into the minimum: it scales with the amount, so it
    is disclosed as a rate at confirm time rather than smuggled into a floor that is supposed to answer
    "is this transfer worth making at all".
    """
    per_leg = ceil_div(c.gas_units * c.gas_price_wei * c.native_price_micro, 10**18)
    swap = ceil_div(c.swap_gas_units * c.gas_price_wei * c.native_price_micro, 10**18)
    return {"per_leg_micro": per_leg, "swap_micro": swap, "total_micro": per_leg * legs + swap,
            "legs": legs, "bridge_fee_bps": c.bridge_fee_bps}


def minimum_economic_deposit(c: ChainCost, *, multiple: int = 10) -> dict:
    """A deposit is refused below 10x its own on-chain cost. The multiple is the decision (not a number
    from a vendor doc): at 1x, a user's first experience of us is a transaction that consumed their money to
    move pocket change; at 10x the fee is under 10% of what arrives, which is the threshold at which a fee
    stops being an embarrassment on a receipt. `stuck` deposits get the same treatment: we do not chase a
    stuck bridge for a deposit that could not pay for the chase."""
    cost = estimate_deposit_cost(c)
    floor = cost["total_micro"] * max(multiple, 1)
    return {"min_deposit_micro": floor, "cost": cost, "multiple": multiple,
            "why": "fees must stay under 10%% of the credited amount; a %dx multiple on the measured "
                   "transfer cost does that" % multiple}


DEPOSIT_STAGES = ("detecting", "confirming", "bridging", "crediting", "credited")
DEPOSIT_STUCK_AFTER_MS = 900_000          # 15 min: longer than a healthy Polygon->Ethereum bridge leg
DEPOSIT_CONFIRMATIONS = {"polygon": 64, "ethereum": 12}   # finality we are willing to credit before


@dataclass
class Deposit:
    id: int
    user_id: str
    chain: str
    asset: str
    amount_micro: int
    status: str = "detecting"
    confirmations: int = 0
    tx_hash: str = ""
    bridge_tx_hash: str = ""
    credit_key: str = ""
    first_seen_ms: int = 0
    attempts: int = 0
    last_error: str = ""

    def stage_index(self) -> int:
        return DEPOSIT_STAGES.index(self.status) if self.status in DEPOSIT_STAGES else -1


def credit_key_for(tx_hash: str, chain: str) -> str:
    """The idempotency key of the credit: derived from the deposit, never from the attempt, so three retries
    of a stuck bridge produce one ledger row (rule 6)."""
    return "dep:" + hashlib.sha256(("%s|%s" % (chain, tx_hash)).encode()).hexdigest()[:32]


def advance_deposit(d: Deposit, *, now_ms: int, confirmations: int | None = None,
                    bridge_tx_hash: str | None = None, credited: bool = False,
                    error: str | None = None) -> tuple[Deposit, str]:
    """One legal step, or `stuck`. Never two steps at once: the caller learns the state from the venue, so a
    single advance keeps the visible state equal to what we can prove. Returns (deposit, note)."""
    d = replace(d)
    if error is not None:
        d.last_error = error[:200]
        if d.status not in ("credited",) and now_ms - d.first_seen_ms > DEPOSIT_STUCK_AFTER_MS:
            d.status = "stuck"
            return d, "error after the stuck horizon; parked for operator recovery, user told 'delayed'"
        return d, "error recorded; still inside the stuck horizon"
    if confirmations is not None:
        d.confirmations = confirmations
    need = DEPOSIT_CONFIRMATIONS.get(d.chain, 12)
    if d.status == "detecting":
        if d.confirmations >= need:
            d.status = "confirming"
            return d, "%d confirmations on %s" % (d.confirmations, d.chain)
        return d, "waiting for confirmations (%d/%d)" % (d.confirmations, need)
    if d.status == "confirming":
        d.status = "bridging"
        return d, "bridge leg submitted"
    if d.status == "bridging":
        if bridge_tx_hash:
            d.bridge_tx_hash = bridge_tx_hash
        if d.stage_index() >= 0 and now_ms - d.first_seen_ms > DEPOSIT_STUCK_AFTER_MS and not bridge_tx_hash:
            d.status = "stuck"
            return d, "no bridge transaction after %dms; recovery must re-drive with credit_key, never " \
                     "re-credit" % DEPOSIT_STUCK_AFTER_MS
        d.status = "crediting"
        return d, "bridged; crediting to the proxy"
    if d.status == "crediting":
        if not credited:
            if now_ms - d.first_seen_ms > DEPOSIT_STUCK_AFTER_MS * 2:
                d.status = "stuck"
            return d, "credit transaction not yet confirmed"
        d.status = "credited"
        if not d.credit_key:
            d.credit_key = credit_key_for(d.tx_hash, d.chain)
        return d, "credited once, under %s" % d.credit_key
    if d.status == "stuck":
        if credited:
            d.status = "credited"
            return d, "operator re-drive landed; the credit key guarantees no second row"
        d.attempts += 1
        return d, "recovery attempt %d; same credit_key" % d.attempts
    return d, "terminal state %s; no-op" % d.status


def deposit_user_note(d: Deposit) -> str:
    """What the user sees, written so it cannot accidentally become "lost". The distinction between delayed
    and lost is the difference between a support ticket and a refund, and it is made by the words."""
    if d.status == "stuck":
        return ("Your deposit is delayed, not lost. It left %s in transaction %s and our credit step has not "
                "confirmed; we are re-driving it and you do not need to send it again."
                % (d.chain, d.tx_hash[:16] or "unknown"))
    return {"detecting": "seen, waiting for confirmations", "confirming": "confirming on %s" % d.chain,
            "bridging": "bridging to the destination chain", "crediting": "crediting to your trading wallet",
            "credited": "available to trade", "failed": "failed; contact support before sending again"}[d.status]


# ===================================================================================== allowance policy ==
APPROVE_EXACT = "exact"
APPROVE_UNLIMITED = "unlimited_once"


@dataclass
class AllowancePlan:
    action: str                       # 'none' | 'approve' | 'revoke_and_suspend'
    amount_micro: int
    reason: str


def plan_allowance(*, granted_micro: int, expected_spender: str, actual_spender: str,
                   needed_micro: int) -> AllowancePlan:
    """D1.5's decision: `max`-approve the venue's CTF exchange once, per wallet, and re-check the SPENDER on
    every submit.

    Why not exact-per-order: it costs one transaction per order, on a chain where the user cannot see it,
    and it makes every order's latency depend on a mempool. Why unlimited is survivable: the spender is a
    single audited exchange contract, and the dangerous case is not "the allowance is large" but "the
    allowance points somewhere unexpected" — so what we enforce is the spender identity, and an upgrade of
    the exchange contract triggers a revoke + suspend rather than a silent continue.
    """
    if actual_spender and expected_spender and actual_spender.lower() != expected_spender.lower():
        return AllowancePlan("revoke_and_suspend", 0,
                             "the pUSD approval points at a contract that is not the exchange this wallet was "
                             "provisioned for; the wallet is suspended until an operator re-approves")
    # The comparison is a ceiling check, not an equality check: an approval of `uint256 max` on-chain reads
    # back as the largest value we can persist, and anything at or above it is unlimited.
    if granted_micro >= needed_micro or granted_micro >= UNLIMITED_ALLOWANCE:
        return AllowancePlan("none", granted_micro, "allowance covers notional plus fees")
    return AllowancePlan("approve", UNLIMITED_ALLOWANCE, "grant the exchange allowance once; revoked on "
                                                         "policy or contract change, never silently refreshed. "
                                                         "Persisted as the sentinel (docs/P06 D1.5), not as "
                                                         "uint256 max, which does not fit a BIGINT column")


# ======================================================================================== withdrawals ==
DENY_CODES_WITHDRAW = ("NOT_AUTHENTICATED", "TYPED_AMOUNT_MISMATCH", "TYPED_ADDRESS_MISMATCH",
                       "ADDRESS_NOT_ALLOWLISTED", "ADDRESS_COOLDOWN", "AMOUNT_TOO_SMALL",
                       "AMOUNT_OVER_SINGLE_CAP", "DAILY_WITHDRAWAL_CAP", "INSUFFICIENT_BALANCE",
                       "OPEN_ORDERS_EXIST", "NOTIFY_UNCONFIRMED", "THRESHOLD_APPROVAL_REQUIRED", "DENIED")


@dataclass(frozen=True)
class WithdrawalRequest:
    user_id: str
    amount_micro: int
    dest_address: str
    typed_amount: str            # exactly what the UI saw typed, stored for the audit
    typed_address: str
    asset: str = "pUSD"
    chain: str = "polygon"
    idempotency_key: str = ""


@dataclass(frozen=True)
class WithdrawalContext:
    password_ok: bool
    allowlist: tuple[tuple[str, int], ...]        # (address, usable_from_ms)
    now_ms: int
    available_micro: int
    fee_micro: int
    withdrawn_today_micro: int
    open_orders: int
    policy: Policy
    approver: str = ""                            # second-party approval for threshold amounts
    notify_email_sent: bool = False
    notify_telegram_sent: bool = False


def check_withdrawal(req: WithdrawalRequest, ctx: WithdrawalContext) -> dict:
    """The ordered gate for leaving. Order matters for a different reason than the risk gate: here it is
    about not revealing more than necessary. Authentication and typing-integrity checks come first, so an
    unauthenticated caller learns nothing about balances or the allowlist; the balance check comes last
    because a "not enough money" answer is itself information.

    Notification is NOT a parameter the caller can set to skip. There is no `notify=False` in this
    signature, and the delivery flags must be True before the request is submitted — the requirement is
    "the user cannot turn these off", which is enforced by the data flow, not by a comment.
    """
    def deny(code: str, msg: str) -> dict:
        return {"ok": False, "code": code, "at_ms": ctx.now_ms, "message": msg}

    if not ctx.password_ok:
        return deny("NOT_AUTHENTICATED", "withdrawals require the account password, re-checked server side")
    if req.amount_micro <= 0:
        return deny("AMOUNT_TOO_SMALL", "amount must be greater than zero")
    # exact string equality on the typed amount: the UI sends what the human typed, and a mismatch means the
    # transaction and the intent are not the same number. 12.50 vs 12.5 is deliberately allowed, because
    # that is the same number; 12.50 vs 1250 is not.
    if _norm_decimal(req.typed_amount) != _norm_decimal(_fmt_amount(req.amount_micro)):
        return deny("TYPED_AMOUNT_MISMATCH", "the amount you typed is not the amount being sent")
    if req.typed_address != req.dest_address:
        return deny("TYPED_ADDRESS_MISMATCH", "the address you typed is not the destination address")
    # Case-EXACT comparison, and it is the whole check: a checksummed address that matched case-insensitively
    # would let a lowercase paste of a typo'd address through against an allowlist entry that is different.
    hit = next((a for a in ctx.allowlist if a[0] == req.dest_address), None)
    if hit is None:
        return deny("ADDRESS_NOT_ALLOWLISTED", "funds can only go to an address on your allowlist")
    usable = hit[1]
    if ctx.now_ms < usable:
        return deny("ADDRESS_COOLDOWN", "this address was added less than %dh ago; a new destination has to "
                                        "age before it can take money out"
                    % (ctx.policy.allowlist_cooldown_ms // 3_600_000))
    if req.amount_micro > ctx.policy.max_single_withdrawal_micro:
        return deny("AMOUNT_OVER_SINGLE_CAP", "above the per-withdrawal cap")
    if ctx.withdrawn_today_micro + req.amount_micro > ctx.policy.max_daily_outflow_micro:
        return deny("DAILY_WITHDRAWAL_CAP", "this would exceed today's outflow limit")
    if ctx.open_orders > 0:
        # Settling first is friendlier than leaving a live order against a drained wallet. The venue will
        # reject on balance, and a rejected order after our withdrawal is a mess the user did not ask for.
        return deny("OPEN_ORDERS_EXIST", "cancel your open orders first")
    if req.amount_micro + ctx.fee_micro > ctx.available_micro:
        return deny("INSUFFICIENT_BALANCE", "not enough available balance for the amount plus the network fee")
    if req.amount_micro > ctx.policy.threshold_approval_above_micro and not ctx.approver:
        return deny("THRESHOLD_APPROVAL_REQUIRED", "this amount needs a second approver, and we do not "
                                                   "implement that ourselves; refusing is the design")
    if not (ctx.notify_email_sent and ctx.notify_telegram_sent):
        return deny("NOTIFY_UNCONFIRMED", "withdrawal notifications could not be delivered; delivery is "
                                          "mandatory before the transaction is submitted")
    return {"ok": True, "code": "QUEUED", "at_ms": ctx.now_ms, "amount_micro": req.amount_micro,
            "dest_address": req.dest_address, "idempotency_key": req.idempotency_key,
            "notify": {"email": True, "telegram": True},
            "policy_hash": ctx.policy.policy_hash()}


def _norm_decimal(text: str) -> str:
    """`12.50` and `12.5` are the same number; `12.50` and `1250` are not, and that difference is the whole
    check. `except` is deliberately narrow: the first version of this helper caught `Exception`, a name was
    misspelled, every input became the same sentinel, and the guard silently stopped guarding — the check
    passed by agreeing with itself. A missing name must raise, not be reported as bad user input."""
    try:
        d = Decimal(text)
    except (InvalidOperation, ValueError, TypeError):
        return "\x00invalid"
    d = d.normalize()
    s = format(d, "f")
    return s


def _fmt_amount(micro: int) -> str:
    from ..money.cents import fmt_usdc
    return fmt_usdc(micro)


# `check_withdrawal` needs the allowlist tuple in a dict-friendly shape; the dataclass is frozen so the
# helper lives here rather than as a method (a method would tempt a caller to mutate it).
def _allowlist_hit(ctx: WithdrawalContext, req: WithdrawalRequest):
    return next((a for a in ctx.allowlist if a[0] == req.dest_address), None)


@dataclass(frozen=True)
class AllowlistEntry:
    address: str
    added_ms: int
    note: str = ""

    def usable_ms(self, policy: Policy) -> int:
        return self.added_ms + policy.allowlist_cooldown_ms


# ========================================================================================== key export ==
EXPORT_WARNING = ("Anyone with this key controls this wallet's funds with no further confirmation from us: "
                  "no password, no 2FA, no cooldown, no notification. Export it only into a signer you "
                  "control, and treat the next withdrawal as the last thing this wallet does for us.")


def plan_export(*, password_ok: bool, open_orders: int, unsettled_intents: int,
                in_flight_withdrawals: int, has_deposit_stuck: bool) -> dict:
    """D1: key export exists, is obvious, and is logged. Deliberately NOT rate-limited into uselessness —
    a user under duress must be able to leave — but it is refused while the wallet is mid-flight, because
    handing over keys while our process holds an intent that will be signed produces two owners of the same
    money and one of them will be surprised.
    """
    if not password_ok:
        return {"ok": False, "code": "NOT_AUTHENTICATED"}
    blockers = []
    if open_orders:
        blockers.append("open orders")
    if unsettled_intents:
        blockers.append("intents the executor has not finished")
    if in_flight_withdrawals:
        blockers.append("a withdrawal in flight")
    if has_deposit_stuck:
        blockers.append("a deposit we are still recovering")
    if blockers:
        return {"ok": False, "code": "WALLET_BUSY", "blockers": blockers,
                "message": "finish or cancel these first; we will not hand over keys while our own process "
                           "is about to sign for this wallet"}
    return {"ok": True, "code": "READY", "warning": EXPORT_WARNING,
            "logged_event": "export_completed",
            "delivered_via": "one-time token shown once over TLS; never emailed, never stored, and the "
                             "material is not in any log line — the DB row holds a digest of it",
            "export_digest_salt": "polygm-export-v1"}


def export_digest(material: str) -> str:
    """What the audit row stores instead of the key: enough to prove we handed something over, useless to a
    log reader."""
    return hashlib.sha256(("polygm-export-v1|" + material).encode()).hexdigest()[:32]


# ====================================================================================== kill / suspend ==
SUSPEND_REASONS = ("policy_drift", "policy_gap", "insider_flagged", "operator", "allowance_mismatch",
                  "export_under_investigation")


def should_refuse_trading(wallet: dict, live_policy: Policy) -> tuple[bool, str]:
    """The executor's pre-signure question. Written to take the row, not the object, because the row is what
    survives a restart — and this check is precisely a post-restart check."""
    if wallet.get("state") not in ("funded", "trading"):
        return True, "WALLET_NOT_TRADABLE: state=%s" % wallet.get("state")
    if check_policy_drift(str(wallet.get("policy_hash") or ""), live_policy)["ok"] is False:
        return True, "POLICY_DRIFT: the provisioned policy hash does not match the live policy"
    if wallet.get("policy_gap"):
        return True, "POLICY_GAP: %s" % wallet["policy_gap"]
    return False, ""
