"""Append-only, integer-only ledger for the three-stage money path:
`order_intents` (requested) → `orders` (submitted to the venue) → `fills` (matched).

Two properties the whole design rests on:

1. **Corrections are new rows.** An `orders` row is never UPDATEd to a different status without a
   matching `ledger_event`, and no money-shaped column is ever rewritten in place. If the executor
   mis-reads a fill and later learns the truth, we append a reversal. This is what makes a 3am
   post-mortem possible: the history contains the wrong belief *and* the correction, in order.
2. **Every row is reconcilable to a user-visible balance.** `cash_ledger` carries every movement, so
   `sum(cash_ledger.amount) + open cost basis == money the user can account for`. That equation is the
   reconciliation invariant, asserted in tests and by the nightly worker (D3).

Statuses below are the union of what the CLOB returns and what we need to show; the DB enforces the
transitions with a CHECK + trigger so a bug cannot write an impossible history.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..money.cents import SCALE, notional_floor


class IntentState(str, Enum):
    PENDING = "pending"            # accepted by api, not yet risk-decided
    REJECTED = "rejected"          # risk gate said no (no venue call was made)
    QUEUED = "queued"              # risk passed, waiting for executor
    SUBMITTING = "submitting"      # executor has taken it; MAY HAVE BEEN SENT — see UNCERTAIN
    UNCERTAIN = "uncertain"        # died between signing and receiving a response: the money-losing state
    SUBMITTED = "submitted"        # venue acknowledged; an `orders` row now exists
    CANCELLED = "cancelled"        # never reached the book, or was cancelled pre-match


class OrderState(str, Enum):
    LIVE = "live"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"          # venue-level rejection (off-tick, disabled builder code, …)


# Venue status -> our state. A single map, used by the executor and the read API, because the alternative
# (each caller translating) is how "matched" shows as unknown in one place and filled in another.
# Values outside this map are a LOUD failure, not a default: a new venue status means the venue changed its
# contract and silently mapping it to "live" would keep an order open forever.
VENUE_ORDER_STATUS: dict[str, str] = {
    "live": OrderState.LIVE.value,
    "delayed": OrderState.LIVE.value,          # delayed = accepted, opens on a timer; still an open order
    "matched": OrderState.FILLED.value,
    "filled": OrderState.FILLED.value,
    "canceled": OrderState.CANCELLED.value,    # the venue spells it with one l
    "cancelled": OrderState.CANCELLED.value,
    "expired": OrderState.EXPIRED.value,
    "unmatched": OrderState.CANCELLED.value,   # venue term for "never matched, now dead"
}
RECONCILE_ACCEPTED_STATES = ("live", "matched")   # what reconcile() treats as "the order exists"


def venue_status_to_state(raw: str) -> str:
    try:
        return VENUE_ORDER_STATUS[raw]
    except KeyError:
        raise ValueError(f"unmapped venue order status {raw!r}") from None


# The states in which a user MUST be allowed to see the order, and MUST NOT be double-charged.
UNSETTLED = {IntentState.SUBMITTING, IntentState.UNCERTAIN, IntentState.SUBMITTED}


@dataclass(frozen=True)
class CashEntry:
    """Money in or out, one row per movement, never edited."""
    id: int
    user_id: str
    kind: str                  # buy | sell_fill | merge_receipt | resolution_payout | fee | refund | adjust
    amount_micro: int          # signed: + credits, - debits
    ref_table: str
    ref_id: str
    as_of_ms: int


@dataclass
class Position:
    """One token held by one user. Cost basis is an integer, and it is the only thing that makes PnL
    computable after a merge, so it is maintained here rather than derived from trade history at read."""
    user_id: str
    token_id: str
    market_id: str
    shares_micro: int = 0
    cost_basis_micro: int = 0        # what those shares cost, in aggregate, not per share

    @property
    def avg_entry_micro(self) -> int | None:
        """Micro-USDC per share, floor. Display rounds; the ledger does not."""
        if self.shares_micro <= 0:
            return None
        return (self.cost_basis_micro * 10 ** SCALE) // self.shares_micro

    def buy(self, shares_micro: int, notional_micro: int) -> None:
        if shares_micro <= 0 or notional_micro < 0:
            raise ValueError("a buy adds shares and costs money")
        self.shares_micro += shares_micro
        self.cost_basis_micro += notional_micro

    def sell(self, shares_micro: int) -> int:
        """Remove shares and return the cost basis released. Pro-rata on integers:
        released = basis * sold // held. An average-cost book does not get to choose which lots were
        sold (that is a tax election, not a trading system), and doing it in integer arithmetic means
        the released amount is exactly reproducible from the stored columns - no float, no rounding mode
        to argue about, and floor here means we never release more basis than the position has.
        """
        if shares_micro <= 0 or shares_micro > self.shares_micro:
            raise ValueError("cannot sell more than held")
        released = (self.cost_basis_micro * shares_micro) // self.shares_micro
        self.shares_micro -= shares_micro
        self.cost_basis_micro -= released
        return released


# --------------------------------------------------------------------------- #
# negRisk multi-outcome realised PnL — the explicit D3 question
# --------------------------------------------------------------------------- #
def negrisk_event_pnl(open_positions: list[Position], settlements: list[dict]) -> dict:
    """Event-level, not market-level. A user can hold YES on three mutually exclusive outcomes and merge
    them, so any market-level computation double-counts.

    Invariants this implements (each is asserted in tests):
      I1  at most ONE YES in a negRisk event can pay out; NO pays on every other outcome
      I2  a merge of N YES legs is worth exactly 1 USDC *guaranteed*, because the N YES either pay
          1 in total or 0 in total and the NO set always pays the complement
      I3  therefore a merge CLOSES N YES legs (realised), and OPENS N NO legs at zero cash — the NO
          cost basis must be ASSIGNED, not paid, or the books cannot balance
      I4  realised + unrealised + cash == cost of everything bought  (the reconciliation equation)

    `settlements` is what the chain said happened: {"kind": "buy"|"sell"|"merge"|"resolve", ...}.
    """
    basis_in = sum(p.cost_basis_micro for p in open_positions)
    cash = 0
    realised = 0
    for s in settlements:
        if s["kind"] == "buy":
            cash -= s["notional_micro"]
        elif s["kind"] == "sell":
            cash += s["proceeds_micro"]
            realised += s["proceeds_micro"] - s["basis_released_micro"]
        elif s["kind"] == "merge":
            # The $1 arrives as cash; the YES legs leave at their pro-rata basis. The residual
            # (1 - sum of released basis, which can be negative) is pushed onto the NO legs created by
            # the merge, because they were created for free and must carry the difference for I4 to hold.
            cash += s["usdc_micro"]
            released = s["yes_basis_released_micro"]
            realised += s["usdc_micro"] - released
            s["no_basis_assigned_micro"] = released - s["usdc_micro"]   # residual, sign included
        elif s["kind"] == "resolve":
            cash += s["payout_micro"]
            realised += s["payout_micro"] - s["basis_released_micro"]
        else:  # pragma: no cover - guarded by the DB CHECK constraint too
            raise ValueError(f"unknown settlement kind {s['kind']!r}")
    # I4 cannot be evaluated from these inputs alone: it also needs the cost basis of positions still
    # OPEN at settlement time (i.e. `basis_in` after the merges have reassigned residual basis onto the
    # new NO legs). The caller in `reconcile()` supplies that, and the invariant is asserted there —
    # returning a boolean from here would be a check that is structurally incapable of failing.
    return {"realised_micro": realised, "cash_micro": cash,
            "open_cost_basis_micro": basis_in, "settled_count": len(settlements)}


def merge_split(n_legs: int, usdc_micro: int, bases: list[int]) -> dict:
    """How a merge's $1 is split across N YES legs and what basis the new NO legs carry.

    Floor on the YES side (we never credit more than arrived), remainder to the LAST leg so the split is
    exact and deterministic, and the NO side takes the complement. Deterministic matters: a non-
    deterministic split makes two runs of the same history disagree, which looks like money appearing.
    """
    if n_legs != len(bases) or n_legs < 1:
        raise ValueError("one basis per merged leg")
    per = usdc_micro // n_legs
    alloc = [per] * n_legs
    alloc[-1] = usdc_micro - per * (n_legs - 1)
    return {
        "yes_credit_micro": alloc,
        "yes_realised_micro": [alloc[i] - bases[i] for i in range(n_legs)],
        "no_basis_micro": [bases[i] - alloc[i] for i in range(n_legs)],
        "sum_credit": sum(alloc),
    }
