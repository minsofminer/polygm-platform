"""The store for everything P07 owns: sessions and rotation, TOTP, the address allowlist, the key envelope,
revoke jobs, and the readiness tables the gate reads.

Same shape as the executor's store — a connection in, autocommit with explicit `BEGIN IMMEDIATE` around the
write-and-read-back pairs, `at` always injected so a test can be at 03:00 on a Tuesday without waiting for it.
Nothing here prints: the only log lines a security store writes are the caller's, through
`redact.line(...)`, which is what keeps a token out of journald by construction rather than by care.
"""
from __future__ import annotations

import hashlib
import json
import secrets as _secrets
import sqlite3
import time
from pathlib import Path

from . import authz, keys as _keys, passwords as _pw, totp as _totp

ACCESS_TTL_MS = 15 * 60 * 1000           # D3: short access tokens; the refresh token is the long-lived one
REFRESH_TTL_MS = 30 * 24 * 3600 * 1000
TOKEN_BYTES = 32
# The proof kinds a link may cite, enforced here (see the note in `db/migrations/0009_security.sql` on why the
# CHECK lives in Python for this one column). A proof that is not in this list is not a proof.
PROOF_KINDS = ("", "signed", "code", "initdata", "ticket")

WANT_TABLES = ("auth_sessions", "refresh_secrets", "auth_events", "totp_enrollments", "withdrawal_addresses",
               "kek_versions", "key_wraps", "revoke_jobs", "telegram_nonces", "password_credentials",
               "route_auth_levels", "backup_restore_tests", "drill_records", "secret_inventory")


def now_ms() -> int:
    return int(time.time() * 1000)


def token_string() -> str:
    """What the client gets: 256 bits of url-safe randomness, once, never stored."""
    return _secrets.token_urlsafe(TOKEN_BYTES)


def normalise_identity(kind: str, value: object) -> str:
    """One normalisation, shared by the claim and the lookup. Two spellings of one email is how an account
    gets taken over by somebody who types it with different capitalisation."""
    s = str(value or "").strip()
    if kind == "email":
        s = s.lower()
        if "@" not in s or s.count("@") != 1 or len(s) > 200:
            return ""
    elif kind == "wallet":
        s = s.lower()
    elif kind == "telegram":
        s = s if s.isdigit() else ""
    else:
        s = s.lower()[:64]
    return s


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


class SecStore:
    def __init__(self, conn: sqlite3.Connection, *, path: str = ":memory:") -> None:
        self.conn = conn
        self.path = path
        self.statement_count = 0

    @classmethod
    def open(cls, path: str) -> "SecStore":
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            if not Path(path).exists():
                raise SystemExit("no database at %s — run `make migrate` first" % path)
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return cls(conn, path=path)

    def close(self) -> None:
        self.conn.close()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        self.statement_count += 1
        return self.conn.execute(sql, params)

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = self.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        got = self.rows(sql, params)
        return got[0] if got else None

    def ready(self) -> list[str]:
        have = {r[0] for r in self.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return [t for t in WANT_TABLES if t not in have]

    # ------------------------------------------------------------------------ D3: credentials & sessions
    def set_password(self, user_id: str, phc: str, *, at: int) -> dict:
        """Store a hash only, and refuse one whose parameters are below the schema floor.

        The floor is checked here and in the DDL, not in the hashing call, because the *hash* can arrive from
        another process (a migration, a support import) and the table must not accept a fast hash regardless of
        who is writing.
        """
        p = _pw.parse_phc(phc)
        if p is None:
            raise ValueError("not a parseable Argon2 PHC string; refusing to store a credential we cannot read")
        bad, why = _pw.below_floor(phc)
        if bad:
            raise ValueError("credential refused: %s" % why)
        if not p:                                                # unreachable; keeps the type checker honest
            raise ValueError("credential refused: no parameters")
        cur = self.one("SELECT user_id, gen FROM password_credentials WHERE user_id=?", (user_id,))
        gen = int((cur or {}).get("gen") or 1) + (1 if cur else 0)
        self.execute(
            "INSERT INTO password_credentials (user_id, phc, kdf, memory_kib, time_cost, parallelism, set_ms,"
            " updated_ms, gen) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET phc=excluded.phc,"
            " kdf=excluded.kdf, memory_kib=excluded.memory_kib, time_cost=excluded.time_cost,"
            " parallelism=excluded.parallelism, updated_ms=excluded.updated_ms, gen=excluded.gen",
            (user_id, phc, p.algo, p.memory_kib, p.time_cost, p.parallelism, at, at, gen))
        # A password change invalidates every other session *immediately*: the generation counter is how that
        # happens without a scan, so a stolen token dies on the reset rather than at its own expiry.
        self.auth_event(user_id, "password_set", at=at, detail={"gen": gen})
        return {"gen": gen, "kdf": p.algo, "memory_kib": p.memory_kib, "needs_update": False}

    def link_identity(self, user_id: str, kind: str, value: str, *, at: int, proof_kind: str = "",
                      verified: bool = False) -> dict:
        """Claim an identity for a user. `verified` is only reachable through a proof, and the caller that
        sets it is the code that checked the proof — nothing else writes it."""
        kind = (kind or "").strip().lower()
        if kind not in ("email", "telegram", "wallet", "handle"):
            raise ValueError("IDENTITY_KIND: %r is not one of email/telegram/wallet/handle" % kind)
        v = normalise_identity(kind, value)
        if not v:
            raise ValueError("IDENTITY_EMPTY: a %s identity cannot be blank" % kind)
        if proof_kind not in PROOF_KINDS:
            raise ValueError("PROOF_KIND: %r is not one of %s" % (proof_kind, list(PROOF_KINDS)))
        held = self.one("SELECT user_id, state FROM user_identities WHERE kind=? AND value=? "
                        "AND state <> 'revoked'", (kind, v))
        if held and str(held["user_id"]) != str(user_id):
            # The message is the same whether the other account is verified or merely claimed: "this address is
            # already taken, try the password reset" is the leak-free answer, and a reset is the safe path.
            self.auth_event(user_id, "identity_claim_refused", at=at, detail={"kind": kind, "value": v[:60]})
            raise ValueError("IDENTITY_TAKEN: that %s already belongs to another account" % kind)
        self.execute(
            "INSERT INTO user_identities (kind, value, user_id, state, claimed_ms, verified_ms, proof_kind) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(kind, value) DO UPDATE SET user_id=excluded.user_id,"
            " state=excluded.state, claimed_ms=excluded.claimed_ms, verified_ms=excluded.verified_ms,"
            " proof_kind=excluded.proof_kind, revoked_ms=NULL",
            (kind, v, user_id, "verified" if verified else "claimed", at, at if verified else None, proof_kind))
        self.auth_event(user_id, "identity_%s" % ("verified" if verified else "linked"), at=at,
                        detail={"kind": kind, "value": v[:60], "proof": proof_kind})
        return {"kind": kind, "value": v, "state": "verified" if verified else "claimed"}

    def identity_user(self, kind: str, value: str) -> str | None:
        """Whose account is this? Only a live row answers, and a revoked one answers nothing."""
        v = normalise_identity((kind or "").strip().lower(), value)
        if not v:
            return None
        row = self.one("SELECT user_id FROM user_identities WHERE kind=? AND value=? AND state <> 'revoked'",
                       ((kind or "").strip().lower(), v))
        return str(row["user_id"]) if row else None

    def verify_identity(self, kind: str, value: str, *, at: int, proof_kind: str) -> dict:
        v = normalise_identity((kind or "").strip().lower(), value)
        cur = self.execute("UPDATE user_identities SET state='verified', verified_ms=?, proof_kind=? WHERE "
                           "kind=? AND value=? AND state <> 'revoked'", (at, proof_kind,
                                                                        (kind or "").strip().lower(), v))
        return {"verified": cur.rowcount or 0}

    def identities(self, user_id: str) -> list[dict]:
        return self.rows("SELECT kind, value, state, claimed_ms, verified_ms, proof_kind FROM user_identities "
                         "WHERE user_id=? AND state <> 'revoked' ORDER BY kind", (user_id,))

    def credential(self, user_id: str) -> dict | None:
        return self.one("SELECT * FROM password_credentials WHERE user_id=?", (user_id,))

    def mint_session(self, user_id: str, *, token_hash: str, family_id: str, at: int,
                     kind: str = "web", ttl_ms: int = ACCESS_TTL_MS, device_label: str = "",
                     ip_hash: str = "", ua_hash: str = "", cred_gen: int = 1) -> dict:
        sid = "ses_" + _secrets.token_hex(8)
        self.execute(
            "INSERT INTO auth_sessions (id, user_id, kind, device_label, token_hash, family_id, issued_ms,"
            " last_seen_ms, expires_ms, cred_gen, ip_hash, ua_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, user_id, kind, device_label[:120], token_hash, family_id, at, at, at + int(ttl_ms),
             int(cred_gen or 1), ip_hash[:64], ua_hash[:64]))
        self.auth_event(user_id, "session_mint", at=at, session_id=sid, ip_hash=ip_hash,
                        detail={"kind": kind, "ttl_ms": int(ttl_ms)})
        return {"id": sid, "user_id": user_id, "family_id": family_id, "expires_ms": at + int(ttl_ms)}

    def resolve_session(self, token_hash: str, *, at: int) -> dict | None:
        """The only path from a bearer token to an identity. Returns None for anything that is not currently a
        live, unrevoked session belonging to a credential generation that still matches."""
        if not token_hash:
            return None
        row = self.one(
            "SELECT s.*, c.gen AS cred_gen_now FROM auth_sessions s "
            "LEFT JOIN password_credentials c ON c.user_id = s.user_id WHERE s.token_hash=?", (token_hash,))
        if not row:
            return None
        if row.get("revoked_ms"):
            return None
        if int(row["expires_ms"]) <= int(at):
            return None
        # A user with no password row (Telegram-only account) has gen NULL: that is not a mismatch, and it is
        # also not a licence to skip the check for users who *do* have one.
        if row.get("cred_gen_now") is not None and int(row["cred_gen"]) != int(row["cred_gen_now"]):
            return None
        return row

    def touch_session(self, session_id: str, at: int) -> None:
        self.execute("UPDATE auth_sessions SET last_seen_ms=? WHERE id=?", (at, session_id))

    def revoke_session(self, session_id: str, *, at: int, reason: str, user_id: str = "") -> dict:
        cur = self.execute("UPDATE auth_sessions SET revoked_ms=?, revoked_reason=? WHERE id=? AND revoked_ms IS NULL",
                           (at, reason[:200], session_id))
        n = cur.rowcount or 0
        if n:
            self.auth_event(user_id or "", "session_revoked", at=at, session_id=session_id,
                            detail={"reason": reason[:200], "count": n})
        return {"revoked": n}

    def revoke_all_sessions(self, *, user_id: str | None, at: int, reason: str) -> dict:
        """One statement, so "log me out everywhere" cannot half-happen.

        The reason is required (>= 4 chars): a revocation with no reason is the row an investigator cannot
        interpret, and it is also the row an operator writes when they are improvising, which is when the
        record matters most.
        """
        if not reason or len(reason.strip()) < 4:
            raise ValueError("a revocation needs a reason of at least 4 characters")
        if user_id:
            cur = self.execute("UPDATE auth_sessions SET revoked_ms=?, revoked_reason=? WHERE user_id=? AND "
                               "revoked_ms IS NULL", (at, reason[:200], user_id))
            cur2 = self.execute("UPDATE refresh_secrets SET revoked_ms=? WHERE user_id=? AND revoked_ms IS NULL",
                                (at, user_id))
        else:
            cur = self.execute("UPDATE auth_sessions SET revoked_ms=?, revoked_reason=? WHERE revoked_ms IS NULL",
                               (at, reason[:200]))
            cur2 = self.execute("UPDATE refresh_secrets SET revoked_ms=?", (at,))
        n = cur.rowcount or 0
        self.auth_event(user_id or "", "session_revoke_all", at=at,
                        detail={"reason": reason[:200], "sessions": n, "refresh": cur2.rowcount or 0})
        return {"sessions": n, "refresh": cur2.rowcount or 0}

    def sessions_for(self, user_id: str, *, at: int | None = None, include_revoked: bool = False) -> list[dict]:
        sql = ("SELECT id, kind, device_label, issued_ms, last_seen_ms, expires_ms, revoked_ms, revoked_reason,"
               " ip_hash, ua_hash FROM auth_sessions WHERE user_id=?")
        if not include_revoked:
            sql += " AND revoked_ms IS NULL"
        out = self.rows(sql + " ORDER BY issued_ms DESC", (user_id,))
        now = at if at is not None else now_ms()
        for r in out:
            r["expired"] = int(r["expires_ms"]) <= int(now)
            r["active"] = not r["revoked_ms"] and not r["expired"]
        return out

    # ----------------------------------------------------------------------------- D3: refresh rotation
    def mint_refresh(self, user_id: str, *, token_hash: str, family_id: str, at: int) -> dict:
        self.execute("INSERT INTO refresh_secrets (token_hash, family_id, user_id, issued_ms) VALUES (?,?,?,?)",
                     (token_hash, family_id, user_id, at))
        return {"family_id": family_id, "expires_ms": at + REFRESH_TTL_MS}

    def rotate_refresh(self, old_hash: str, *, new_hash: str, at: int) -> dict:
        """Rotation with reuse detection. The *detection* is the whole design: a refresh token is single-use,
        so a second presentation of a rotated token means somebody else has a copy. The response is to kill the
        family — the attacker's token and the victim's new one — and to write the event the alarm reads.
        """
        row = self.one("SELECT * FROM refresh_secrets WHERE token_hash=?", (old_hash,))
        if not row:
            return {"ok": False, "code": "REFRESH_UNKNOWN"}
        if int(row["issued_ms"]) + REFRESH_TTL_MS <= int(at):
            return {"ok": False, "code": "REFRESH_EXPIRED"}
        if row["used_ms"] or row["revoked_ms"]:
            fam = str(row["family_id"])
            self.execute("UPDATE refresh_secrets SET revoked_ms=? WHERE family_id=? AND revoked_ms IS NULL",
                         (at, fam))
            self.execute("UPDATE auth_sessions SET revoked_ms=?, revoked_reason=? WHERE family_id=? AND "
                         "revoked_ms IS NULL", (at, "refresh token reuse", fam))
            self.auth_event(str(row["user_id"]), "refresh_reuse", at=at,
                            detail={"family_id": fam, "action": "family revoked, sessions killed"})
            return {"ok": False, "code": "REFRESH_REUSED", "family_id": fam}
        self.execute("UPDATE refresh_secrets SET used_ms=? WHERE token_hash=?", (at, old_hash))
        self.mint_refresh(str(row["user_id"]), token_hash=new_hash, family_id=str(row["family_id"]), at=at)
        return {"ok": True, "user_id": str(row["user_id"]), "family_id": str(row["family_id"])}

    def auth_events(self, user_id: str, *, since_ms: int, limit: int = 50) -> list[dict]:
        return self.rows("SELECT kind, at_ms, session_id, ip_hash, detail_json FROM auth_events WHERE user_id=?"
                         " AND at_ms>=? ORDER BY at_ms DESC LIMIT ?", (user_id, since_ms, int(limit)))

    def auth_event(self, user_id: str, kind: str, *, at: int, session_id: str = "", ip_hash: str = "",
                   detail: dict | None = None) -> None:
        self.execute("INSERT INTO auth_events (user_id, kind, at_ms, session_id, ip_hash, detail_json) "
                     "VALUES (?,?,?,?,?,?)",
                     (user_id or "", kind[:60], at, session_id, (ip_hash or "")[:64],
                      json.dumps(detail or {}, sort_keys=True, separators=(",", ":"))[:4000]))

    # ------------------------------------------------------------------------- D3: TOTP (stored wrapped)
    def totp_enroll(self, user_id: str, *, secret_wrapped: str, nonce: str, at: int, tag: str = "",
                    kek_version: int = 1) -> dict:
        self.execute("INSERT INTO totp_enrollments (user_id, secret_wrapped, nonce, tag, kek_version, alg,"
                     " digits, period_s, enrolled_ms, last_step) VALUES (?,?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(user_id) DO UPDATE SET secret_wrapped=excluded.secret_wrapped,"
                     " nonce=excluded.nonce, tag=excluded.tag, kek_version=excluded.kek_version,"
                     " enrolled_ms=excluded.enrolled_ms, verified_ms=NULL, last_step=-1, failed_count=0,"
                     " locked_until_ms=0",
                     (user_id, secret_wrapped, nonce, tag, int(kek_version), "AES-256-GCM", _totp.DIGITS,
                      _totp.PERIOD_S, at, -1))
        self.auth_event(user_id, "totp_enrolled", at=at)
        return {"digits": _totp.DIGITS, "period_s": _totp.PERIOD_S}

    def totp_state(self, user_id: str) -> dict | None:
        return self.one("SELECT * FROM totp_enrollments WHERE user_id=?", (user_id,))

    def totp_enrolled(self, user_id: str) -> bool:
        return self.one("SELECT 1 AS x FROM totp_enrollments WHERE user_id=?", (user_id,)) is not None

    def totp_accept(self, user_id: str, *, step: int, at: int) -> None:
        self.execute("UPDATE totp_enrollments SET verified_ms=?, last_step=?, failed_count=0, "
                     "locked_until_ms=0 WHERE user_id=?", (at, int(step), user_id))

    def totp_reject(self, user_id: str, *, at: int, lock_ms: int = _totp.LOCK_MS) -> dict:
        row = self.totp_state(user_id) or {}
        n = int(row.get("failed_count") or 0) + 1
        st = _totp.attempt_state(n)
        locked = int(at) + lock_ms if st["locked"] else 0
        self.execute("UPDATE totp_enrollments SET failed_count=?, locked_until_ms=? WHERE user_id=?",
                     (n, locked, user_id))
        if st["locked"]:
            self.auth_event(user_id, "totp_locked", at=at, detail={"failed_count": n})
        return {"failed_count": n, "locked_until_ms": locked, "tries_left": st["tries_left"]}

    # ------------------------------------------------------------------- D3: the withdrawal address list
    def add_address(self, user_id: str, address: str, *, at: int, label: str = "", via: str = "user") -> dict:
        addr = (address or "").strip()
        if not addr or len(addr) > 128:
            raise ValueError("an address of %d characters is not an address" % len(addr))
        n = self.one("SELECT COUNT(*) AS c FROM withdrawal_addresses WHERE user_id=? AND removed_ms IS NULL",
                     (user_id,)) or {"c": 0}
        if int(n["c"]) >= authz.MAX_ADDRESSES_PER_USER:
            raise ValueError("ADDRESS_LIMIT: %d addresses is the cap; remove one you still control first"
                             % authz.MAX_ADDRESSES_PER_USER)
        # The cooldown starts when the destination is *confirmed*, never when it was typed, and nothing can
        # skip it: `skip_cooldown` has a CHECK that only allows FALSE, so the column exists to make the
        # temptation visible in the schema.
        aid = "waddr_" + _secrets.token_hex(8)
        self.execute("INSERT INTO withdrawal_addresses (id, user_id, address, label, added_ms, usable_ms,"
                     " confirmed_ms, added_via, skip_cooldown) VALUES (?,?,?,?,?,?,?,?,?)",
                     (aid, user_id, addr, label[:80], at, at + authz.COOLDOWN_MS, at, via, 0))
        shared = self.rows("SELECT user_id FROM withdrawal_addresses WHERE address=? AND removed_ms IS NULL",
                           (addr,))
        others = sorted({str(r["user_id"]) for r in shared} - {user_id})
        self.auth_event(user_id, "address_added", at=at,
                        detail={"usable_in_ms": authz.COOLDOWN_MS, "shared_with": others[:5]})
        return {"id": aid, "usable_ms": at + authz.COOLDOWN_MS, "shared_with": others,
                "note": "usable after the 24 h cooldown; the cooldown exists so a takeover is slow enough to see"}

    def confirm_address(self, address_id: str, user_id: str, *, at: int) -> dict:
        cur = self.execute("UPDATE withdrawal_addresses SET confirmed_ms=?, usable_ms=? WHERE id=? AND user_id=? "
                           "AND removed_ms IS NULL", (at, at + authz.COOLDOWN_MS, address_id, user_id))
        return {"updated": cur.rowcount or 0}

    def addresses(self, user_id: str, *, at: int) -> list[dict]:
        out = self.rows("SELECT id, address, label, added_ms, usable_ms, confirmed_ms, added_via FROM "
                        "withdrawal_addresses WHERE user_id=? AND removed_ms IS NULL ORDER BY added_ms DESC",
                        (user_id,))
        for r in out:
            r["usable"] = authz.is_address_allowlisted(r.get("usable_ms"), at)
            r["cooldown_remaining_ms"] = max(0, int(r.get("usable_ms") or 0) - int(at))
            shown = str(r.get("address") or "")
            r["display"] = shown[:6] + "\u2026" + shown[-4:] if len(shown) > 12 else shown
        return out

    def address_for_withdrawal(self, user_id: str, address_id: str, *, at: int) -> tuple[bool, str, dict]:
        row = self.one("SELECT * FROM withdrawal_addresses WHERE id=? AND user_id=? AND removed_ms IS NULL",
                       (address_id, user_id))
        if not row:
            # 404-shape on purpose (see authz.assert_owns): "not yours" and "does not exist" are the same body.
            return False, "NOT_FOUND", {}
        if not authz.is_address_allowlisted(row.get("usable_ms"), at):
            remain = int(row["usable_ms"]) - int(at)
            return False, "ADDRESS_COOLDOWN", {"remaining_ms": max(0, remain),
                                              "message": "this destination was added %ds ago and can be used "
                                                         "after the 24 hour hold" % (remain // 1000)}
        return True, "ok", row

    def remove_address(self, user_id: str, address_id: str, *, at: int) -> dict:
        row = self.one("SELECT * FROM withdrawal_addresses WHERE id=? AND user_id=? AND removed_ms IS NULL",
                       (address_id, user_id))
        if not row:
            return {"removed": 0, "code": "NOT_FOUND"}
        # The hard block: you cannot delete a destination while it is still in its cooldown. An attacker who
        # added their own address uses the removal to erase the trail before the first payout clears.
        if int(row.get("usable_ms") or 0) > int(at):
            self.auth_event(user_id, "address_remove_refused", at=at,
                            detail={"remaining_ms": int(row["usable_ms"]) - int(at)})
            return {"removed": 0, "code": "REMOVE_DURING_COOLDOWN",
                    "message": "a destination that has not finished its hold cannot be deleted; that is the "
                               "rule that makes a takeover visible"}
        self.execute("UPDATE withdrawal_addresses SET removed_ms=? WHERE id=?", (at, address_id))
        self.auth_event(user_id, "address_removed", at=at, detail={"address_id": address_id})
        return {"removed": 1}

    # --------------------------------------------------------------------------- D2: KEK/DEK bookkeeping
    def new_kek(self, *, at: int, provider: str = "env", key_id: str = "", ceremony_by: str = "",
                witnesses: str = "", audit_note: str = "") -> dict:
        row = self.one("SELECT MAX(version) AS v FROM kek_versions")
        v = int((row or {}).get("v") or 0) + 1
        self.execute("INSERT INTO kek_versions (version, created_ms, provider, key_id, ceremony_by, witnesses,"
                     " audit_note) VALUES (?,?,?,?,?,?,?)",
                     (v, at, provider, key_id[:80], ceremony_by[:80], witnesses[:200], audit_note[:400]))
        return {"version": v, "witnesses": [w for w in witnesses.split(",") if w.strip()]}

    def current_kek(self) -> dict | None:
        return self.one("SELECT * FROM kek_versions WHERE retired_ms IS NULL ORDER BY version DESC LIMIT 1")

    def wrap_key(self, user_id: str, *, ciphertext: str, nonce: str, tag: str, kek_version: int,
                 policy_hash: str, at: int, dek_version: int | None = None) -> dict:
        if dek_version is None:
            last = self.one("SELECT MAX(dek_version) AS v FROM key_wraps WHERE user_id=?", (user_id,))
            dek_version = int((last or {}).get("v") or 0) + 1
        self.execute("INSERT INTO key_wraps (user_id, dek_version, wrapped_dek, nonce, tag, kek_version,"
                     " policy_hash, created_ms, messages_wrapped) VALUES (?,?,?,?,?,?,?,?,?)",
                     (user_id, int(dek_version), ciphertext, nonce, tag, int(kek_version), policy_hash, at, 0))
        return {"user_id": user_id, "dek_version": int(dek_version), "kek_version": int(kek_version)}

    def key_wrap(self, user_id: str, dek_version: int | None = None) -> dict | None:
        if dek_version is None:
            return self.one("SELECT * FROM key_wraps WHERE user_id=? AND revoked_ms IS NULL "
                            "ORDER BY dek_version DESC LIMIT 1", (user_id,))
        return self.one("SELECT * FROM key_wraps WHERE user_id=? AND dek_version=?", (user_id, int(dek_version)))

    def key_wrap_rows(self, *, include_revoked: bool = False) -> list[dict]:
        sql = "SELECT * FROM key_wraps" + ("" if include_revoked else " WHERE revoked_ms IS NULL")
        return self.rows(sql + " ORDER BY user_id, dek_version")

    def note_wrap_used(self, user_id: str, dek_version: int, *, at: int) -> dict:
        """Count the wrap, and refuse to continue past the nonce budget.

        The executor calls this once per signature. If it returns `ok=False`, the caller stops signing for that
        wallet and a re-wrap is scheduled: a DEK that has run out of nonces is not "a bit weaker".
        """
        row = self.key_wrap(user_id, dek_version) or {}
        used = int(row.get("messages_wrapped") or 0) + 1
        ok, why = _keys.nonce_budget_ok(used)
        self.execute("UPDATE key_wraps SET messages_wrapped=? WHERE user_id=? AND dek_version=?",
                     (used, user_id, int(dek_version)))
        return {"ok": ok, "messages_wrapped": used, "why": why, "rotate": not ok}

    def revoke_key(self, user_id: str, *, at: int, dek_version: int | None = None) -> int:
        if dek_version is None:
            cur = self.execute("UPDATE key_wraps SET revoked_ms=? WHERE user_id=? AND revoked_ms IS NULL",
                               (at, user_id))
        else:
            cur = self.execute("UPDATE key_wraps SET revoked_ms=? WHERE user_id=? AND dek_version=?",
                               (at, user_id, int(dek_version)))
        return cur.rowcount or 0

    def retire_kek(self, version: int, *, at: int) -> dict:
        ok, why = _keys.can_retire_kek(self.key_wrap_rows(), version)
        if not ok:
            raise ValueError("KEK v%d still in use: %s" % (version, why))
        self.execute("UPDATE kek_versions SET retired_ms=? WHERE version=?", (at, int(version)))
        return {"retired": int(version)}

    # ------------------------------------------------------------------------ D2: the revocation job
    def start_revoke_job(self, *, scope: str, requested_by: str, total: int, batch: int, per_call_ms: int,
                         at: int, job_id: str = "") -> dict:
        jid = job_id or ("rev_" + _secrets.token_hex(6))
        self.execute("INSERT INTO revoke_jobs (id, scope, requested_by, started_ms, total, batch_size,"
                     " per_call_ms) VALUES (?,?,?,?,?,?,?)",
                     (jid, scope, requested_by, at, int(total), int(batch), int(per_call_ms)))
        return {"id": jid, "total": int(total), "batches": (int(total) + int(batch) - 1) // int(batch)}

    def bump_revoke_job(self, job_id: str, *, revoked: int, failed: int) -> None:
        self.execute("UPDATE revoke_jobs SET revoked=revoked+?, failed=failed+? WHERE id=?",
                     (int(revoked), int(failed), job_id))

    def finish_revoke_job(self, job_id: str, *, at: int, detail: dict | None = None) -> dict:
        self.execute("UPDATE revoke_jobs SET finished_ms=?, detail_json=? WHERE id=?",
                     (at, json.dumps(detail or {}, sort_keys=True, separators=(",", ":"))[:4000], job_id))
        return self.revoke_job(job_id) or {}

    def revoke_job(self, job_id: str) -> dict | None:
        return self.one("SELECT * FROM revoke_jobs WHERE id=?", (job_id,))

    # ------------------------------------------------------------------------- D3: Telegram replay store
    def telegram_state(self, auth_hash: str) -> dict | None:
        return self.one("SELECT * FROM telegram_nonces WHERE auth_hash=?", (auth_hash,))

    def telegram_seen_hashes(self, auth_hash: str) -> set[str]:
        """The `seen_hashes` callable for `telegram.verify`: a replayed login is refused inside the same
        request, and the durable row is what refuses it after a restart."""
        row = self.telegram_state(auth_hash)
        return {auth_hash} if row and row.get("used_ms") else set()

    def telegram_consume(self, auth_hash: str, user_id: str, *, at: int, session: str = "") -> dict:
        """`INSERT … ON CONFLICT DO NOTHING`, then read back: whoever inserted it is the one that gets to use
        it. Two logins from one captured payload therefore cannot both succeed, even across processes."""
        self.execute("INSERT INTO telegram_nonces (auth_hash, user_id, seen_ms) VALUES (?,?,?) "
                     "ON CONFLICT(auth_hash) DO NOTHING", (auth_hash, user_id, at))
        row = self.telegram_state(auth_hash) or {}
        if row.get("used_ms"):
            self.auth_event(user_id, "telegram_replay_refused", at=at,
                            detail={"first_seen_ms": row.get("seen_ms"), "first_user": row.get("user_id")})
            return {"ok": False, "code": "TELEGRAM_REPLAY", "first_seen_ms": int(row.get("seen_ms") or 0)}
        self.execute("UPDATE telegram_nonces SET used_ms=?, bound_session=? WHERE auth_hash=? AND used_ms IS NULL",
                     (at, session, auth_hash))
        return {"ok": True, "code": "ok"}

    # --------------------------------------------------------------------- D4: route levels, as data
    def sync_route_levels(self, mapping: dict[str, tuple[str, str]], *, at: int) -> dict:
        have = {r["operation"]: r for r in self.rows("SELECT operation, level, object_check FROM route_auth_levels")}
        added = changed = removed = 0
        for op, (lv, oc) in mapping.items():
            cur = have.get(op)
            if cur is None:
                self.execute("INSERT INTO route_auth_levels (operation, level, object_check) VALUES (?,?,?)",
                             (op, lv, oc))
                added += 1
            elif cur["level"] != lv or str(cur["object_check"] or "") != oc:
                self.execute("UPDATE route_auth_levels SET level=?, object_check=? WHERE operation=?",
                             (lv, oc, op))
                changed += 1
        for op in sorted(set(have) - set(mapping)):
            self.execute("DELETE FROM route_auth_levels WHERE operation=?", (op,))
            removed += 1
        return {"declared": len(mapping), "added": added, "changed": changed, "removed": removed, "at_ms": at}

    def route_levels(self) -> dict[str, str]:
        return {r["operation"]: r["level"] for r in self.rows("SELECT operation, level FROM route_auth_levels")}

    # --------------------------------------------------------------- D6/D9: readiness rows the gate reads
    def record_backup_test(self, *, kind: str, encrypted: bool, taken_ms: int, restore_started_ms: int,
                           restore_done_ms: int | None, verified_rows: int, money_checks_ok: bool,
                           tested_by: str, note: str = "") -> dict:
        self.execute("INSERT INTO backup_restore_tests (kind, encrypted, taken_ms, restore_started_ms,"
                     " restore_done_ms, verified_rows, money_checks_ok, tested_by, note) VALUES (?,?,?,?,?,?,?,?,?)",
                     (kind, 1 if encrypted else 0, taken_ms, restore_started_ms, restore_done_ms,
                      int(verified_rows), 1 if money_checks_ok else 0, tested_by[:80], note[:400]))
        return {"kind": kind}

    def latest_backup_test(self, kind: str) -> dict | None:
        return self.one("SELECT * FROM backup_restore_tests WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,))

    def record_drill(self, *, kind: str, started_ms: int, finished_ms: int | None, verdict: str,
                     measured_ms: int, failed_step: str = "", participants: str = "",
                     notes: str = "") -> dict:
        if verdict == "fail" and not failed_step.strip():
            # The rule that stops a drill from being a checkbox: a failure with no named step is a failure that
            # will be "fixed" by rerunning it until it says pass.
            raise ValueError("a failed drill must name the step that failed")
        self.execute("INSERT INTO drill_records (kind, started_ms, finished_ms, verdict, measured_ms,"
                     " failed_step, participants, notes) VALUES (?,?,?,?,?,?,?,?)",
                     (kind, started_ms, finished_ms, verdict, int(measured_ms), failed_step[:120],
                      participants[:200], notes[:1000]))
        return {"kind": kind, "verdict": verdict, "measured_ms": int(measured_ms)}

    def latest_drill(self, kind: str) -> dict | None:
        return self.one("SELECT * FROM drill_records WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,))

    def put_secret(self, *, name: str, environment: str, stored_in: str, owner: str, rotate_by_ms: int,
                   last_rotated_ms: int = 0, can_rotate_live: bool = True, note: str = "") -> dict:
        if not str(owner).strip():
            raise ValueError("SECRET_OWNER_REQUIRED: a secret with no named human owner is an unrotated secret")
        self.execute("INSERT INTO secret_inventory (name, environment, stored_in, owner, rotate_by_ms,"
                     " last_rotated_ms, can_rotate_live, note) VALUES (?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(name) DO UPDATE SET environment=excluded.environment, stored_in=excluded.stored_in,"
                     " owner=excluded.owner, rotate_by_ms=excluded.rotate_by_ms,"
                     " last_rotated_ms=excluded.last_rotated_ms, can_rotate_live=excluded.can_rotate_live,"
                     " note=excluded.note",
                     (name, environment, stored_in, owner, int(rotate_by_ms), int(last_rotated_ms),
                      1 if can_rotate_live else 0, note[:400]))
        return {"name": name}

    def secrets_due(self, *, at: int, environment: str = "prod") -> list[dict]:
        return self.rows("SELECT * FROM secret_inventory WHERE environment=? AND rotate_by_ms < ? "
                         "ORDER BY rotate_by_ms", (environment, int(at)))

    def secret_inventory(self, environment: str = "prod") -> list[dict]:
        return self.rows("SELECT * FROM secret_inventory WHERE environment=? ORDER BY name", (environment,))

    # ---------------------------------------------------------------------------- D8: findings and gates
    def record_wash(self, *, user_id: str, builder_code: str, verdict, window_start_ms: int, window_end_ms: int,
                    volume_micro: int, decided_by: str = "rule") -> dict:
        self.execute("INSERT INTO wash_findings (user_id, builder_code, score_bps, factors_json, window_start_ms,"
                     " window_end_ms, volume_micro, action, at_ms, decided_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (user_id, builder_code, int(verdict.score_bps),
                      json.dumps(list(verdict.factors), separators=(",", ":")), window_start_ms, window_end_ms,
                      int(volume_micro), verdict.action, now_ms(), decided_by[:40]))
        return {"action": verdict.action, "score_bps": int(verdict.score_bps)}

    def wash_findings(self, user_id: str, *, limit: int = 20) -> list[dict]:
        return self.rows("SELECT * FROM wash_findings WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id,
                                                                                                   int(limit)))

    def set_builder_code(self, code: str, *, state: str, at: int, reject_count: int = 0, source: str = "manual",
                         note: str = "") -> dict:
        self.execute("INSERT INTO builder_code_status (code, state, last_seen_ms, changed_ms, reject_count,"
                     " source, note) VALUES (?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET state=excluded.state,"
                     " last_seen_ms=excluded.last_seen_ms, changed_ms=excluded.changed_ms,"
                     " reject_count=excluded.reject_count, source=excluded.source, note=excluded.note",
                     (code, state, at, at, int(reject_count), source, note[:400]))
        return {"code": code, "state": state}

    def builder_code(self, code: str) -> dict | None:
        return self.one("SELECT * FROM builder_code_status WHERE code=?", (code,))

    def record_broadcast(self, *, market_id: str, verdict: str, reasons: list[str], audience: int,
                         at: int) -> dict:
        self.execute("INSERT INTO broadcast_gates (market_id, verdict, reasons_json, audience, at_ms) "
                     "VALUES (?,?,?,?,?)",
                     (market_id, verdict, json.dumps(reasons, separators=(",", ":"))[:2000], int(audience), at))
        return {"market_id": market_id, "verdict": verdict}

    def broadcast_rows(self, market_id: str = "", *, limit: int = 25) -> list[dict]:
        if market_id:
            return self.rows("SELECT * FROM broadcast_gates WHERE market_id=? ORDER BY id DESC LIMIT ?",
                             (market_id, int(limit)))
        return self.rows("SELECT * FROM broadcast_gates ORDER BY id DESC LIMIT ?", (int(limit),))

    def record_finding(self, *, component: str, threat: str, actor: str, control: str, owner: str,
                       test_ref: str, severity: str, likelihood: int, impact_micro: int, rank: int,
                       status: str = "open", accepted_reason: str = "", note: str = "") -> dict:
        self.execute("INSERT INTO incident_findings (component, threat, actor, control, owner, test_ref, "
                     "severity, likelihood, impact_micro, rank, status, accepted_reason, note) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(rank) DO UPDATE SET "
                     "component=excluded.component, threat=excluded.threat, actor=excluded.actor,"
                     " control=excluded.control, owner=excluded.owner, test_ref=excluded.test_ref,"
                     " severity=excluded.severity, likelihood=excluded.likelihood,"
                     " impact_micro=excluded.impact_micro, status=excluded.status,"
                     " accepted_reason=excluded.accepted_reason, note=excluded.note",
                     (component, threat, actor, control, owner, test_ref, severity, int(likelihood),
                      int(impact_micro), int(rank), status, accepted_reason[:400], note[:400]))
        return {"rank": int(rank), "owner": owner, "test_ref": test_ref}

    def findings(self) -> list[dict]:
        return self.rows("SELECT * FROM incident_findings ORDER BY rank")
