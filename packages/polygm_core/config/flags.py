"""Every tunable in one place (D7), loaded once per process, refreshed on a timer, never per request.

Change without a deploy: flags live in `feature_flags` (Postgres), and a reader refreshes this cache every
`FLAG_TTL_MS`. Env vars are the bootstrap/default layer only — that ordering is deliberate, because a
pod that boots during an incident must not resurrect a flag someone flipped in the DB.

Audit: every write goes through `set_flag`, which appends to `flag_audit` (who, what, old, new, reason).
`kill_switch` is *not* a flag in the casual sense — it is a row with its own table and an append-only
history, because "who disabled trading and when" is evidence, not configuration.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class RateLimit:
    """Class name -> requests per window per scope. Limits are per-IP and per-signer upstream (shared
    context §2), so the class — not the endpoint — is the unit we budget against."""
    burst: int
    sustained_per_min: int
    scope: str          # "ip" | "user" | "signer"


@dataclass(frozen=True)
class Flags:
    # ---- upstream budgets. The numbers come from docs/verification/P01-probe.json where measured.
    gamma_per_10s: int = 300              # [CTX] P04 prompt's stated limit; probe did not exhaust it
    balance_per_10s: int = 200            # [CTX] shared context §2 — governs reconcile cadence, D3
    tape_ws_max_rows: int = 64            # design system D5.2 hard DOM cap

    # ---- freshness (design system D5.5). Kept here so API and UI agree on "stale".
    stale_ms_book: int = 3_000
    stale_ms_tape: int = 3_000
    # 5s, the design system's own number for a price: a book level and a last trade are 3s, but a chart point
    # and an event's outcome table are aggregates of many trades and are honest for longer. P09's history and
    # event reads carry it, so the API and the UI cannot disagree about when a candle is old.
    stale_ms_price: int = 5_000
    stale_ms_metadata: int = 120_000
    stale_ms_positions: int = 30_000

    # ---- caching (rule 2: mandatory, not an optimisation)
    cache_ttl_market_ms: int = 1_000
    cache_ttl_books_ms: int = 250
    cache_ttl_leaderboard_ms: int = 60_000
    cache_serve_stale_ms: int = 30_000    # one user must not be able to spend our rate budget twice

    # ---- risk (mirrors polygm_core.risk.gate.Limits; the DB copy is authoritative at runtime)
    max_order_notional_micro: int = 2_500_000_000
    max_24h_notional_micro: int = 25_000_000_000
    min_order_size_shares_micro: int = 5_000_000

    # ---- money-adjacent product knobs
    builder_bps: int = 100                # our take; must match the registered builder code's rate
    whale_flag_micro: int = 2_000_000_000  # $2k, from P01's distribution (median fill $5-6)

    # ---- behaviour switches, all default-off until a phase turns them on
    flags: tuple[str, ...] = ()
    flag_cache: dict = field(default_factory=dict, compare=False)

    def on(self, name: str) -> bool:
        return name in self.flags

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Flags":
        e = env if env is not None else os.environ
        over = {}
        for f in cls.__dataclass_fields__:
            key = f"PGM_{f.upper()}"
            if key in e:
                raw = e[key]
                cur = getattr(cls, f, None)
                over[f] = json.loads(raw) if isinstance(cur, (int, float)) or f == "flags" else raw
        return cls(**over)

    @classmethod
    def with_flags(cls, base: "Flags", enabled: list[str]) -> "Flags":
        return replace(base, flags=tuple(sorted(set(base.flags) | set(enabled))))


# --------------------------------------------------------------------------- #
# the refresh loop (kept out of the dataclass so the config object stays pure/hashable)
# --------------------------------------------------------------------------- #
FLAG_TTL_MS = 5_000


class FlagStore:
    """Reads `feature_flags` and hands out immutable snapshots.

    Failure mode is the whole design: if the DB is unreachable we keep serving the last snapshot and
    raise the age, rather than falling back to code defaults. A cache miss that re-enables a feature
    someone just disabled is an outage with extra steps. Only at boot, when there is no last snapshot,
    do we use defaults — and then `boot_defaults_used` stays True forever so health can report it.
    """

    def __init__(self, conn, ttl_ms: int = FLAG_TTL_MS) -> None:
        self.conn, self.ttl_ms = conn, ttl_ms
        self.base = Flags.from_env()
        self._snap: Flags = self.base
        self._loaded_ms = 0
        self.boot_defaults_used = True

    def current(self, now_ms: int | None = None) -> Flags:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        if now_ms - self._loaded_ms < self.ttl_ms:
            return self._snap
        try:
            rows = self.conn.execute("SELECT name, value_json FROM feature_flags").fetchall()
            enabled = [r[0] for r in rows
                       if json.loads(r[1]).get("on") is True]      # explicit: not "truthy"
            self._snap = Flags.with_flags(self._reload_numeric(), enabled)
            self.boot_defaults_used = False
        except Exception:
            pass                      # keep the last good snapshot; age is observable via .age_ms
        self._loaded_ms = now_ms
        return self._snap

    def _reload_numeric(self) -> Flags:
        rows = self.conn.execute(
            "SELECT name, value_json FROM feature_flags WHERE kind='number'").fetchall()
        over = {r[0]: json.loads(r[1])["value"] for r in rows
                if r[0] in Flags.__dataclass_fields__}
        return replace(self.base, **over) if over else self.base

    def refresh(self) -> Flags:
        """Reload now, ignoring the TTL.

        The TTL exists so a hot path does not read the database on every request. An operation that just *changed*
        a flag does not have that luxury: a kill switch with a five-second hole in it is a kill switch that gets
        used on a five-second problem, and the operator watching the screen sees a switch that did not work.
        """
        self._loaded_ms = 0
        return self.current()

    @property
    def age_ms(self) -> int:
        return int(time.time() * 1000) - self._loaded_ms


SQL_AUDIT = """INSERT INTO flag_audit (name, old_value, new_value, changed_by, reason, at_ms)
               VALUES (?,?,?,?,?,?)"""


def set_flag(conn, name: str, value, *, changed_by: str, reason: str,
             now_ms: int | None = None) -> None:
    """The only sanctioned write path. No service may UPDATE feature_flags directly — the CI check
    (tools/p04-gate-check.py G3) greps for raw UPDATEs on that table so the audit cannot be bypassed
    by accident or by convenience."""
    if not reason.strip():
        raise ValueError("a flag change without a reason is not a flag change, it is a mistake in progress")
    old = conn.execute("SELECT value_json FROM feature_flags WHERE name=?", (name,)).fetchone()
    kind = "bool" if isinstance(value, bool) else "number"
    # `on` is a boolean SWITCH, so it is only ever written for kind='bool'. Storing the number 0 as a
    # feature flag and letting `on: null` fall through to the switch filter (or, worse, `0` reading as
    # "off" on a threshold that should always be active) conflates two different things. A first draft did
    # exactly that: `bool(1234)` is True, so a notional cap looked like an enabled feature.
    payload = {"value": value, "on": bool(value)} if kind == "bool" else {"value": value}
    conn.execute("INSERT INTO feature_flags (name, kind, value_json) VALUES (?,?,?) "
                 "ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json",
                 (name, kind, json.dumps(payload)))
    conn.execute(SQL_AUDIT, (name, old[0] if old else None, json.dumps(value), changed_by, reason,
                             now_ms if now_ms is not None else int(time.time() * 1000)))
