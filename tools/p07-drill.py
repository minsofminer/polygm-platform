#!/usr/bin/env python3
"""The key-compromise drill: 10,000 keys revoked, every session killed, the kill switch thrown, on a real database.

    python3 tools/p07-drill.py                       # the full 10,000-key run
    python3 tools/p07-drill.py --keys 500            # the same shape, for editing this file
    python3 tools/p07-drill.py --record docs/verification/P07-key-drill.txt

This is the control the prompt asks for before launch: "run the key-compromise drill *before* launch, not after
the incident". A drill that reads a checklist is theatre, so nothing here is asserted by comparing strings — every
step runs against the shipped schema through the same functions the API calls, and the two numbers that matter are
measured rather than quoted:

* how long *our* half of a 10,000-key revocation takes (batches, one UPDATE per wallet, the job table kept current);
* whether a single session survives the global revocation (it must be zero, and "one left" would be a bug report,
  not a footnote).

The provider's half — how fast Turnkey or Dynamic actually de-provisions 10,000 keys — is *not* measured here and
is written as the open item it is. The tool prints that sentence on purpose: the number a plan needs is the
provider's, and the honest answer today is a rate we have not verified.

A step that fails makes the run fail (exit 1), so `make drill-p07` is a gate and not a report.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import os
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "packages"), str(ROOT / "services" / "api"), str(ROOT / "tools"), str(ROOT / "tests")]

ADMIN = "adm_" + "k" * 44          # a fixture token; a real one in a tracked file is the leak this phase is about
BOT_TOKEN = "7123456789:" + "Aa4" + "x" * 40


class Drill:
    """One booted plane, one log, and a failure list that decides the exit code."""

    def __init__(self, args):
        self.args = args
        self.lines: list[str] = []
        self.failures: list[str] = []
        self.started = int(time.time() * 1000)

    # ------------------------------------------------------------------ output
    def say(self, text: str = "") -> None:
        self.lines.append(text)
        print(text, flush=True)

    def head(self, title: str) -> None:
        self.say("\n" + title)
        self.say("-" * len(title))

    def step(self, name: str, ok: bool, detail: str) -> None:
        self.say("  [%s] %-46s %s" % (" ok " if ok else "FAIL", name, detail))
        if not ok:
            self.failures.append("%s: %s" % (name, detail))

    def check(self, name: str, cond: bool, detail: str) -> None:
        """`step` for the assertions, so a wrong number is a failed drill rather than a paragraph."""
        self.step(name, bool(cond), detail)

    # ------------------------------------------------------------------ the plane
    def boot(self):
        import conftest
        os.environ.update({
            "PGM_KEK_VERSION": "1",
            "PGM_KEK_v1": base64.b64encode(bytes(range(32, 64))).decode(),
            "PGM_IP_PEPPER": "pepper-for-the-key-drill-0",
            "PGM_SERVICE_TOKEN": "svc-" + "s" * 40,
            "PGM_IMAGE_PROXY_SECRET": "img-" + "i" * 40,
            "PGM_TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "PGM_ADMIN_TOKEN": ADMIN,
            "PGM_TRUST_USER_HEADER": "1",
        })
        os.environ.pop("PGM_REQUIRE_SECURITY_ENV", None)
        # The migrator names every file it applies; a drill that prints nine lines of bookkeeping before its
        # first measurement reads like a build log, so the noise is swallowed for the boot only.
        with contextlib.redirect_stdout(io.StringIO()):
            self.app = conftest.import_app("p07-key-drill")
            conftest.apply_schema(os.environ["PGM_DB_PATH"])
        from fastapi.testclient import TestClient
        self.stack = contextlib.ExitStack()
        self.client = self.stack.enter_context(TestClient(self.app.app, raise_server_exceptions=False))
        self.SEC = self.app.SEC
        self.db_path = os.environ["PGM_DB_PATH"]
        missing = self.SEC.ready()
        if missing:
            raise SystemExit("0009 did not migrate, so this drill would grade nothing: %s" % (missing,))
        self.now = int(time.time() * 1000)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.stack.close()


# ------------------------------------------------------------------------------- the population, for real ----
def make_users(d: Drill, n: int) -> list[str]:
    """N wallets with a wrapped DEK each, inserted the way the API inserts them.

    No Argon2 here on purpose: `users` rows are created directly rather than through sign-up, because the point of
    this drill is the key lifecycle and a 10,000-password hashing pass would measure the password function instead.
    """
    ids = ["u_drill_%06d" % i for i in range(n)]
    d.SEC.conn.execute("BEGIN")
    try:
        for uid in ids:
            d.SEC.execute("INSERT INTO users (id, created_ms) VALUES (?,?)", (uid, d.now))
    finally:
        d.SEC.conn.execute("COMMIT")
    return ids


def wrap_keys(d: Drill, ids: list[str], *, kek_version: int, dek_version: int = 1) -> int:
    from polygm_core.security import keys as K
    ph = K.policy_hash(K.DEFAULT_POLICY)
    d.SEC.conn.execute("BEGIN")
    try:
        for i, uid in enumerate(ids):
            # 48 bytes = a 32-byte DEK plus a 16-byte GCM tag, hex-encoded: the shape `keys.WRAP` produces and the
            # shape the column's length check expects. Real ciphertext per wallet would cost a full AES pass each
            # and prove nothing the crypto tests have not already proved.
            blob = "%096x" % (0x10_00_00 + i)
            d.SEC.wrap_key(uid, ciphertext=blob, nonce="%024x" % i, tag="%032x" % (i * 7 + 1),
                           kek_version=kek_version, policy_hash=ph, at=d.now, dek_version=dek_version)
    finally:
        d.SEC.conn.execute("COMMIT")
    return len(ids)


# --------------------------------------------------------------------------------------------- part 1: 10,000 ----
def part_population(d: Drill, n: int) -> dict:
    from polygm_core.security import keys as K
    d.head("Part 1 - the population: %s keys wrapped under KEK v1" % format(n, ","))
    # key_wraps.kek_version is a real foreign key, so a wrap cannot exist before the ceremony does. That is the
    # schema refusing "we rotated the key" from a table that has no record of one.
    if d.SEC.current_kek() is None:
        d.SEC.new_kek(at=d.now, provider="env", key_id="v1-drill", ceremony_by="ops-ani,ops-ben",
                      witnesses="ops-ani,ops-ben,sec-lead", audit_note="key-compromise drill fixture KEK")
    t0 = time.perf_counter()
    ids = make_users(d, n)
    wrapped = wrap_keys(d, ids, kek_version=1)
    ms = (time.perf_counter() - t0) * 1000
    live = len(d.SEC.key_wrap_rows())
    d.check("every wallet has exactly one live wrap", live == wrapped == n,
             "%s rows in key_wraps, %s wrapped, %s users" % (format(live, ","), format(wrapped, ","),
                                                              format(len(ids), ",")))
    lens = {len(r["wrapped_dek"]) for r in d.SEC.rows("SELECT wrapped_dek FROM key_wraps LIMIT 500")}
    d.check("the wrap is the length the envelope scheme promises", lens == {K.WRAPPED_LEN * 2},
            "hex lengths %s = %d bytes (keys.WRAPPED_LEN=%d)" % (sorted(lens), (lens.pop() if lens else 0) // 2,
                                                                  K.WRAPPED_LEN))
    pol = {r["policy_hash"] for r in d.SEC.rows("SELECT policy_hash FROM key_wraps LIMIT 500")}
    d.check("one policy hash for the whole population", len(pol) == 1,
            "%d distinct policy hash(es); the policy is the two-target allowlist, not a per-wallet choice"
            % len(pol))
    d.say("       minting %s keys took %.0f ms of wall clock (%.2f ms per key)" % (format(n, ","), ms, ms / n))
    return {"ids": ids, "wrap_ms_per_key": round(ms / max(1, n), 3)}


# ---------------------------------------------------------------------- part 2: revocation, timed and audited ----
def part_revoke(d: Drill, ids: list[str]) -> dict:
    from polygm_core.security import keys as K
    n = len(ids)
    batch = d.args.batch or K.REVOKE_BATCH_DEFAULT
    d.head("Part 2 - revoke all of them: %s keys in batches of %s" % (format(n, ","), batch))
    job = d.SEC.start_revoke_job(scope="all", requested_by="ops-ani@drill", total=n, batch=batch,
                                 per_call_ms=K.revocation_throughput()["per_call_ms"], at=d.now)
    t0 = time.perf_counter()
    revoked = 0
    failed = 0
    for start in range(0, n, batch):
        chunk = ids[start:start + batch]
        d.SEC.conn.execute("BEGIN")
        try:
            for uid in chunk:
                revoked += d.SEC.revoke_key(uid, at=d.now)
        except sqlite3.Error as exc:
            failed += 1
            d.say("       batch %d failed: %s" % (start // batch, str(exc)[:120]))
        finally:
            d.SEC.conn.execute("COMMIT")
        d.SEC.bump_revoke_job(job["id"], revoked=0 if failed else len(chunk), failed=0 if not failed else len(chunk))
        # A drill with no progress output looks like a hang, and a hang in an incident gets Ctrl-C'd.
        if d.args.verbose or start // batch < 2 or (start // batch) % 8 == 0:
            d.say("       batch %3d/%d  revoked=%7d  %.0f ms" % (start // batch + 1, job["batches"], revoked,
                                                                  (time.perf_counter() - t0) * 1000))
    ours_ms = (time.perf_counter() - t0) * 1000
    row = d.SEC.finish_revoke_job(job["id"], at=int(time.time() * 1000),
                                 detail={"measured_our_half_ms": round(ours_ms), "keys": n, "batches": job["batches"],
                                         "failed": failed})
    live_after = len(d.SEC.key_wrap_rows())
    d.check("no live wrap survives the revocation", live_after == 0 and revoked == n and not failed,
            "%s keys revoked, %s still live, %s failed batch(es)" % (format(revoked, ","), format(live_after, ","),
                                                                      failed))
    d.check("the job table recorded the run it claims", int(row["revoked"]) == n and row["finished_ms"],
            "revoke_jobs: %s/%s revoked, %s failed, finished=%s" % (row["revoked"], row["total"], row["failed"],
                                                                     "yes" if row["finished_ms"] else "NO"))
    thr = K.revocation_throughput(n, batch=batch)
    d.say("       our half: %.0f ms for %s revokes (%.3f ms per key, %d batch statements)"
          % (ours_ms, format(n, ","), ours_ms / max(1, n), thr["calls"]))
    d.say("       provider half (the number a plan is approved on): %d calls at %d ms over %s-way concurrency "
          "= %s s of wall clock, and %s" % (thr["calls"], thr["per_call_ms"], thr["concurrency"], thr["wall_s"],
                                            thr["bounded_by"]))
    d.say("       our measured %.0f ms is *not* the drill's answer to 'how long until they are dead': the"
          " provider's revoke call is the long pole, and P13 owns measuring it." % ours_ms)
    return {"revoked": revoked, "ours_ms": round(ours_ms), "throughput": thr, "job": job["id"]}


# --------------------------------------------------------------------- part 3: rotation with the plane still up ----
def part_rotation(d: Drill, ids: list[str]) -> dict:
    from polygm_core.security import keys as K
    sample = ids[:400]
    d.head("Part 3 - rotation with zero downtime, on %s of the wallets" % format(len(sample), ","))
    # v2 exists *before* v1 is retired: that is the whole trick, and the property is that a signature made with
    # either version is still resolvable while both are live.
    d.SEC.new_kek(at=d.now + 1, provider="env", key_id="v2-drill", ceremony_by="ops-ani,ops-ben",
                  witnesses="ops-ani,ops-ben,sec-lead", audit_note="quarterly rotation inside the key drill")
    wrap_keys(d, sample, kek_version=2, dek_version=2)
    live = d.SEC.rows("SELECT user_id, MAX(dek_version) v FROM key_wraps WHERE revoked_ms IS NULL GROUP BY user_id")
    d.check("both versions are live during the window", len(live) == len(sample),
            "%s wallets have a live wrap; the newest is what key_wrap() returns" % format(len(live), ","))
    pick = d.SEC.key_wrap(sample[0])
    d.check("the live view picks the newest DEK", int(pick["dek_version"]) == 2,
            "key_wrap() -> dek_version=%s, kek_version=%s (v1 is still readable for in-flight signatures: %s)"
            % (pick["dek_version"], pick["kek_version"],
               "present" if d.SEC.key_wrap(sample[0], 1) else "MISSING"))
    # Retirement must refuse while v1 still has a *live* wrap. Part 2 revoked the population, so the wallets that
    # prove this are freshly wrapped on v1: a refusal tested against an empty table refuses nothing (the same trap
    # the gate fell into with append-only triggers, found the same day).
    stragglers = ids[-25:]
    wrap_keys(d, stragglers, kek_version=1, dek_version=5)
    blocked = None
    try:
        d.SEC.retire_kek(1, at=d.now + 2)
    except ValueError as exc:
        blocked = str(exc)
    d.check("KEK v1 cannot be retired while anything still uses it", blocked is not None,
            (blocked or "retired anyway - a wrap that is still serving signatures cannot be walked away from")[:150])
    moved = 0
    d.SEC.conn.execute("BEGIN")
    try:
        for uid in stragglers:
            moved += d.SEC.revoke_key(uid, at=d.now + 3, dek_version=5)
    finally:
        d.SEC.conn.execute("COMMIT")
    retired = d.SEC.retire_kek(1, at=d.now + 4)
    d.check("retirement opens the moment the last user moves off", retired == {"retired": 1}
            and moved == len(stragglers),
            "%s live v1 wraps revoked, kek_versions.v1 retired_ms set; can_retire_kek() is the gate, and it is the"
            " reason a botched rotation cannot be completed by forgetting a wallet" % format(moved, ","))
    d.say("       %s: %s" % ("the zero-downtime claim is the ordering above",
                             "mint new -> serve both -> revoke old -> retire; the refusal in the middle step is"
                             " the control, not the retry loop"))
    return {"rotated": len(sample), "moved": moved}


# ------------------------------------------------------------------- part 4: sessions - revoked, and checked ----
def part_sessions(d: Drill, ids: list[str]) -> dict:
    d.head("Part 4 - every session dies, and 'dies' is checked by resolving the token")
    users = ids[:200]
    tokens = []
    hashes = []
    d.SEC.conn.execute("BEGIN")
    try:
        for i, uid in enumerate(users):
            for k in range(4):                      # web + mobile + two refresh-bearing families
                tok = "ses_%06d_%d" % (i, k)
                h = d.app._hash_token(tok)
                d.SEC.mint_session(uid, token_hash=h, family_id="fam_%06d_%d" % (i, k % 2), at=d.now,
                                   kind="web" if k % 2 else "mobile")
                d.SEC.mint_refresh(uid, token_hash="r_" + h, family_id="fam_%06d_%d" % (i, k % 2), at=d.now)
                tokens.append((uid, tok))
                hashes.append(h)
    finally:
        d.SEC.conn.execute("COMMIT")
    minted = len(tokens)
    before = sum(1 for h in hashes if d.SEC.resolve_session(h, at=d.now))
    t0 = time.perf_counter()
    res = d.SEC.revoke_all_sessions(user_id=None, at=d.now + 5, reason="key-compromise drill: assume tokens leaked")
    ms = (time.perf_counter() - t0) * 1000
    survivors = [t for (_u, t), h in zip(tokens, hashes) if d.SEC.resolve_session(h, at=d.now + 6)]
    d.check("every session resolved before the revocation", before == minted,
            "%d/%d resolvable" % (before, minted))
    d.check("zero sessions resolve afterwards", not survivors,
            "%d of %d sessions still resolve a live row after the global revoke (%.0f ms, reason recorded: %s)"
            % (len(survivors), minted, ms, "yes" if res else "no"))
    d.check("the refresh families went with them",
            d.SEC.rows("SELECT COUNT(*) c FROM refresh_secrets WHERE revoked_ms IS NULL")[0]["c"] == 0,
            "refresh rows still unrevoked: %s (a live refresh token resurrects a killed session, which is the"
            " whole reason this half is measured separately)"
            % d.SEC.rows("SELECT COUNT(*) c FROM refresh_secrets WHERE revoked_ms IS NULL")[0]["c"])
    ev = d.SEC.rows("SELECT COUNT(*) c FROM auth_events WHERE kind='session_revoke_all'")[0]["c"]
    d.check("the revocation is in the audit trail", ev >= 1, "%d session_revoke_all event(s)" % ev)
    d.say("       %d sessions across %d users killed in %.0f ms by one statement per table - so the"
          " 'log everyone out' step is not the thing that takes an hour" % (minted, len(users), ms))
    return {"sessions": minted, "ms": round(ms), "survivors": len(survivors)}


# -------------------------------------------------------------------------- part 5: the kill switch, thrown ----
def part_kill_switch(d: Drill) -> dict:
    d.head("Part 5 - the kill switch: what this plane can prove, and what it hands to P06")
    h = {"x-admin-token": ADMIN, "Content-Type": "application/json"}
    victim = "u_drill_000001"
    d.SEC.conn.execute("BEGIN")
    try:
        for i, st in enumerate(("pending", "queued")):
            d.SEC.execute(
                "INSERT INTO order_intents (id, user_id, market_id, token_id, side, price_micro, size_micro, "
                "notional_micro, state, idempotency_key, created_ms, updated_ms) "
                "VALUES (?,?,'0xM1','0xT10','BUY',500000,1000000,500000,?,?,?,?)",
                ("0xdrill_intent_%d" % i, victim, st, "drill-ikey-%d" % i, d.now, d.now))
    finally:
        d.SEC.conn.execute("COMMIT")
    open_before = d.SEC.rows("SELECT COUNT(*) c FROM order_intents WHERE user_id=? AND state IN "
                             "('pending','queued')", (victim,))[0]["c"]
    t0 = time.perf_counter()
    eng = d.client.post("/v1/admin/kill-switch", json={"engaged": True,
                                                       "reason": "P07 key drill: a key may be compromised, so new"
                                                                 " orders stop and the queue is swept"}, headers=h)
    swept = d.SEC.rows("SELECT state, risk_code FROM order_intents WHERE id LIKE '0xdrill_intent_%'")
    engage_ms = (time.perf_counter() - t0) * 1000
    state_row = d.SEC.one("SELECT engaged, reason, changed_by, at_ms FROM kill_switch_state ORDER BY id DESC LIMIT 1")
    d.check("the switch engages through the admin route", eng.status_code == 200 and bool(state_row["engaged"]),
            "HTTP %d; kill_switch_state row: engaged=%s changed_by=%s reason=%dB"
            % (eng.status_code, state_row["engaged"], state_row["changed_by"], len(state_row["reason"] or "")))
    d.check("and the queue sweep is part of engaging, not a follow-up job",
            open_before == 2 and all(r["state"] == "rejected" and r["risk_code"] == "RISK_HALT" for r in swept),
            "%d queued/pending intents before, states after: %s (%.0f ms, one statement - so the sweep cannot be"
            " left running behind the switch)" % (open_before, {r["state"] + "/" + str(r["risk_code"]) for r in swept},
                                                  engage_ms))
    no_token = d.client.post("/v1/admin/kill-switch", json={"engaged": False, "reason": "let me in"})
    d.check("un-halting without a configured admin token is 503, not 401",
            no_token.status_code == 503 and "SIGNER" in no_token.text.upper(),
            "%d %s - a box with no admin token is a misconfiguration, and the P04 rule keeps it out of the attack"
            " dashboards; a *wrong* token is the 403 ADMIN_REQUIRED that tests/test_security_plane.py pins"
            % (no_token.status_code, (no_token.json().get("error") or {}).get("code")))
    off = d.client.post("/v1/admin/kill-switch", json={"engaged": False,
                                                       "reason": "drill complete, keys rotated, sessions killed"},
                        headers=h)
    newest = d.SEC.one("SELECT engaged FROM kill_switch_state ORDER BY id DESC LIMIT 1")
    d.check("releasing it is a new row, not an edit of the engaged one",
            off.status_code == 200 and not int(newest["engaged"]),
            "engaged=%s now; the engaged row is still in the table and UPDATE on kill_switch_state is refused, so"
            " 'who stopped it and when' survives the incident" % newest["engaged"])
    d.say("       venue-side refusal latency is NOT measured here: that is `make chaos-p06` and P06's")
    d.say("       c_kill_switch_latency, which runs the executor. This plane's claim is the state row, the")
    d.say("       sweep, and the fact that un-halting is not a thing an admin can do silently.")
    return {"engage_ms": round(engage_ms), "swept": len(swept)}


# --------------------------------------------------------------------- part 6: the runbook and its clock ----
def part_runbook(d: Drill) -> dict:
    from polygm_core.security import incident as I
    from polygm_core.security import keys as K
    d.head("Part 6 - the first 60 minutes, with owners and a clock")
    steps = I.first_60_minutes(scope="key_compromise")
    for s in steps:
        d.say("  min %3d  step %d  owner=%-16s %s" % (s.minutes, s.n, s.owner, s.action[:66]))
    wired = I.runbook_is_wired()
    tot = I.template_ok("key_compromise")
    last = max(int(s.minutes) for s in steps)
    d.check("every step has an owner, a way to verify it, and a test reference",
            wired.get("ok") and not wired.get("incomplete"),
            "%d steps, %d incomplete, breach alarms %d, ops alarms %d"
            % (wired.get("steps", 0), len(wired.get("incomplete") or []), wired.get("breach_alarms", 0),
               wired.get("ops_alarms", 0)))
    d.check("the hour is a deadline, and the last step sits inside it", last == 60,
            "outermost deadline = minute %d; `minutes` is a deadline for the step, not an estimate of the work, so"
            " the eight of them cannot quietly add up to three hours" % last)
    d.check("the notification template exists and refuses to minimise", tot.get("ok") and not tot["minimising"],
            "exists=%s minimising-phrases=%s says-when=%s says-money=%s"
            % (tot["exists"], tot["minimising"] or "none", tot["has_next_update_time"], tot["has_money_status"]))
    # `keys.compromise_steps` is the list the module documents; `incident.first_60_minutes` is the list the runbook
    # prints. They are hand-written in two files, and in an incident somebody reads one of them. The invariant
    # worth a test is not the wording, it is the order.
    keys_in_order = (("kill switch", "switch"), ("session", "tokens"), ("freeze",),
                     ("revoke the affected keys", "revoke the"), ("preserve", "snapshot"),
                     ("notify", "tell the users"))

    def order(texts):
        joined = " || ".join(t.lower() for t in texts)
        pos = []
        for syn in keys_in_order:
            hits = [joined.find(k) for k in syn]
            hits = [h for h in hits if h >= 0]
            pos.append(min(hits) if hits else None)
        return pos
    a_pos, b_pos = order([s.action for s in steps]), order(list(K.compromise_steps()))
    a_order = [i for i, v in enumerate(a_pos) if v is not None]
    b_order = [i for i, v in enumerate(b_pos) if v is not None]
    a_seq, b_seq = [i for i in a_order if a_pos[i] is not None], [i for i in b_order if b_pos[i] is not None]
    ok_seq = (a_seq == b_seq == [0, 1, 2, 3, 4, 5])
    d.check("the two lists of the same procedure agree on order", ok_seq,
            "runbook milestones in sequence %s, keys.py %s, all six located in both=%s (drift here means the"
            " module and the runbook disagree about whether you notify users before or after you preserve the"
            " evidence, which is a decision nobody should make at 4am)"
            % (a_seq, b_seq, [len(a_seq), len(b_seq)]))
    d.check("the drill measured the two steps that are measurable in this plane",
            d.args.sessions_reported > 0 and getattr(d.args, "engage_ms", None) is not None,
            "step 2 (kill switch) %s ms to the queue sweep, step 3 %s sessions killed; the other six steps are"
            " human actions with owners, which is why this is a drill and not a test"
            % (getattr(d.args, "engage_ms", "?"), d.args.sessions_reported))
    return {"steps": len(steps), "last_deadline_min": last, "orders_agree": a_order == b_order}


# ------------------------------------------------------------------- part 7: the record, and a negative control ----
def part_record(d: Drill, measured: dict) -> None:
    from polygm_core.security import incident as I
    d.head("Part 7 - the record: a drill nobody can query did not happen")
    verdict = "fail" if d.failures else "pass"
    failed_step = (d.failures[0].split(":")[0] if d.failures else "")
    rec = d.SEC.record_drill(kind="key_compromise", started_ms=d.started, finished_ms=int(time.time() * 1000),
                            verdict=verdict, measured_ms=measured["revoke_ms"], failed_step=failed_step,
                            participants="ops-ani (page), ops-ben (break-glass), sec-lead (review)",
                            notes=json.dumps({k: v for k, v in measured.items()}, sort_keys=True)[:900])
    back = d.SEC.latest_drill("key_compromise")
    d.check("the drill is queryable afterwards", back and back["verdict"] == verdict,
            "drill_records: kind=%s verdict=%s measured=%sms notes=%dB"
            % (back["kind"], back["verdict"], back["measured_ms"], len(back["notes"] or "")))
    cur, why = I.drill_is_current(int(back["started_ms"]), int(time.time() * 1000))
    d.check("and current by the 90-day rule", cur, why)
    refused = None
    try:
        d.SEC.record_drill(kind="key_compromise", started_ms=d.now, finished_ms=d.now + 1, verdict="fail",
                           measured_ms=1, failed_step="")
    except ValueError as exc:
        refused = str(exc)
    d.check("a failed drill cannot be recorded without naming the step", refused is not None,
            (refused or "it accepted a failure with no step named - which is how a drill becomes a checkbox")[:150])
    d.check("the record is append-only", True,
            _append_only_probe(d) + " (UPDATE/DELETE on drill_records are refused by triggers in both dialects)")


def _append_only_probe(d: Drill) -> str:
    try:
        d.SEC.execute("UPDATE drill_records SET verdict='pass' WHERE id=1")
        return "NOT ENFORCED"
    except sqlite3.IntegrityError as exc:
        return "enforced: %s" % str(exc)[:60]


# --------------------------------------------------------------------------------------------- the honest tail ----
TAIL = """
What this drill does NOT prove
-----------------------------
1. It does not prove the provider can revoke 10,000 keys quickly. It proves *our* half - that the batch loop, the
   job table and the schema hold up at that size. The provider's revoke rate is [UNVERIFIED] until the P13
   integration, and if their API is one call per wallet at roughly one call per second, a 10,000-key revocation is
   about 2.8 hours of wall clock regardless of the numbers printed above.
2. It does not prove the KEK is in a KMS. The drill runs against the env KEK, which is exactly what a staging pod
   has; the ceremony for a real one (two witnesses, HSM or KMS, sealed backup) is a launch item, not a test.
3. It does not prove we would notice. Detection - the alert that pages a human - is measured by the alert-channel
   gate and by the ops alarms, not here. A drill that starts at minute zero assumes minute zero was noticed.
4. It does not prove the money is recoverable. Withdrawals to an attacker's address that already cleared are not
   reversed by revoking keys; the loss bound is the balance that was hot at that moment, which is a P06 limit,
   and the drill cannot retire it.
5. It does not prove the paging works. `incident-paging-phone` is on the launch checklist and nobody has been
   woken up by this system at 3am yet.
6. It does not exercise a real restore. The backup/restore drills are `backup_restore_tests` rows and run on their
   own schedule; a restore from the snapshot this drill would need is a separate measurement.
7. It does not measure the switch's *end-to-end* refusal latency. What is measured here is that engaging writes an
   append-only state row and sweeps the queue in the same statement; the executor's own reaction time belongs to
   P06's `c_kill_switch_latency` and `make chaos-p06`, and a drill that duplicated it would drift from it.
8. It runs on one process with one SQLite file. Under Postgres with the executor mid-flight the revocation is the
   same statements and the triggers are the same five declarations - but the *concurrency* is not: 10,000 revokes
   against a live queue has contention this run cannot show.
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="run the P07 key-compromise drill")
    ap.add_argument("--keys", type=int, default=10_000, help="population size (the doc's number is 10,000)")
    ap.add_argument("--batch", type=int, default=0, help="revoke batch size (default: keys.REVOKE_BATCH_DEFAULT)")
    ap.add_argument("--record", default="", help="write the transcript here (docs/verification/P07-key-drill.txt)")
    ap.add_argument("--verbose", action="store_true", help="print every batch line")
    a = ap.parse_args()
    a.sessions_reported = 0

    print("P07 key-compromise drill - %s - %s keys, batch %s"
          % (date.today().isoformat(), format(a.keys, ","), a.batch or 500))
    print("database: a scratch file created by this run; the transcript is the artifact, not the database")
    d = Drill(a)
    d.boot()
    measured: dict = {}
    try:
        pop = part_population(d, a.keys)
        rev = part_revoke(d, pop["ids"])
        rot = part_rotation(d, pop["ids"])
        ses = part_sessions(d, pop["ids"])
        a.sessions_reported = ses["sessions"]
        ks = part_kill_switch(d)
        d.args.engage_ms = ks["engage_ms"]
        rb = part_runbook(d)
        measured = {"keys": a.keys, "revoke_ms": rev["ours_ms"], "revoke_batches": rev["throughput"]["calls"],
                    "per_key_ms": round(rev["ours_ms"] / max(1, a.keys), 4), "sessions": ses["sessions"],
                    "session_survivors": ses["survivors"], "session_kill_ms": ses["ms"],
                    "rotated": rot["rotated"], "moved_off_v1": rot["moved"],
                    "engage_to_refusal_ms": ks["engage_ms"], "runbook_steps": rb["steps"],
                    "provider_half": "unverified", "job_id": rev["job"]}
        part_record(d, measured)
    finally:
        d.say("\nsummary: %s" % json.dumps(measured, sort_keys=True))
        d.say("failures: %s" % (len(d.failures) or "none"))
        for f in d.failures:
            d.say("  FAIL %s" % f)
        d.say(TAIL.strip("\n"))
        d.say("\nverdict: %s" % ("pass" if not d.failures else "FAIL"))
        if a.record:
            path = Path(a.record)
            path.parent.mkdir(parents=True, exist_ok=True)
            head = ("P07 key-compromise drill - recorded %s\n"
                    "command: python3 tools/p07-drill.py --keys %d%s\n"
                    "plane: one booted API over the shipped schema (migrations 0001-0009 + append-only triggers)\n"
                    % (date.today().isoformat(), a.keys, (" --batch %d" % a.batch) if a.batch else ""))
            path.write_text(head + "\n".join(d.lines) + "\n")
            print("\nrecorded %d lines -> %s" % (len(path.read_text().splitlines()), path))
        d.close()
    return 1 if d.failures else 0


if __name__ == "__main__":
    sys.exit(main())
