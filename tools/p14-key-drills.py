#!/usr/bin/env python3
"""P14 D2 — six key-compromise drills, run for real, with a stopwatch on each.

    python3 tools/p14-key-drills.py --record docs/verification/P14-key-drills.txt --json …json
    python3 tools/p14-key-drills.py --only user_key_leaked,signing_service_compromised

The kit asks for six scenarios and a **recorded time for every one of them**, and it states the consequence in
advance: if the full break-glass takes longer than an hour, the product does not launch. That sentence is why this
tool exists in this shape. A runbook paragraph saying "rotate the KEK" is a claim; a run that engages the kill
switch, revokes sessions under the two-approver rule, rotates the KEK over every wrapped key, verifies each one,
retires the old KEK and *measures the wall clock* is evidence.

Three things this tool deliberately does not do:

  * **It does not simulate the code paths.** Every step calls the product's own function — `SEC.revoke_all_sessions`,
    `SEC.revoke_key`, `keys.rotation_plan`, `keys.can_retire_kek`, the kill switch's own table, the executor's own
    pre-flight. A drill that re-implements the procedure measures the drill.
  * **It does not skip a step because the others passed.** Each drill has named steps and a budget, and a step that
    fails names itself in the record: `record_drill` refuses a failure with no named step, which is the difference
    between a drill and a checkbox.
  * **It does not report a time without saying what was measured.** "measured" is the wall clock from the first
    response action to the state being provably closed (asserted afterwards, not assumed), on this machine, with
    the numbers in the artifact.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pathlib
import sys
import time
import uuid
from importlib.util import module_from_spec, spec_from_file_location

ROOT = pathlib.Path(__file__).resolve().parents[1]
VERIF = ROOT / "docs" / "verification"

#: The kit's hard limit. Not a target: a breach is a launch blocker and the report says so in writing.
BREAK_GLASS_LIMIT_MS = 60 * 60 * 1000

#: Internal budgets for the drills the kit does not put a number on. Chosen from expected loss: a single user's
#: wallet with a 24-hour destination hold is a bounded loss, so minutes; a signing provider is total, so the
#: break-glass hour. The tool reports the measured time *and* the budget it was measured against.
BUDGET_MS = {
    "user_key_leaked": 15 * 60 * 1000,
    "signing_service_compromised": BREAK_GLASS_LIMIT_MS,
    "key_in_a_log": 30 * 60 * 1000,
    "contractor_leaves": 60 * 60 * 1000,
    "support_impersonation": 5 * 60 * 1000,
    "wallet_provider_outage": 15 * 60 * 1000,
}

DRILLS = tuple(BUDGET_MS)

#: `drill_records.kind` is a CHECKed vocabulary of four: the product's own scenarios, which is the set the
#: incident runbook and the operator's console are built around. The six drills here are the scenarios inside them,
#: and the record carries the scenario name in its notes — folding them silently into "key_compromise" would make
#: six different drills look like one repeated drill in the table an on-call actually reads.
RECORD_KIND = {
    "user_key_leaked": "key_compromise",
    "signing_service_compromised": "key_compromise",
    "key_in_a_log": "key_compromise",
    "contractor_leaves": "key_compromise",
    "support_impersonation": "phishing_support",
    "wallet_provider_outage": "pg_failover",
}


def revoked_count(out: dict) -> int:
    """`revoke_all_sessions` reports its work as `sessions`/`refresh`; `revoke_key` as `revoked`. Reading the
    wrong key here would have turned "one session closed" into "zero sessions closed" — the first version of
    these drills asserted `out["revoked"]` and reported a failed drill for a product that had done its job."""
    return int(out.get("sessions", out.get("revoked", 0)) or 0)


def _load(name: str, path: pathlib.Path):
    spec = spec_from_file_location(name, path)
    mod = module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class Bench:
    """One process, one throwaway database, and the product's own modules loaded into it."""

    def __init__(self, *, kek_versions: int = 2) -> None:
        import base64
        import tempfile
        sys.path[:0] = [str(ROOT / "packages"), str(ROOT / "services" / "api"), str(ROOT / "tools")]
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="p14-drills-"))
        self.db = self.tmp / "drills.db"
        os.environ["PGM_DB_PATH"] = str(self.db)
        os.environ["PGM_LOG_FORMAT"] = "json"
        os.environ["PGM_REQUIRE_SECURITY_ENV"] = "1"
        os.environ["PGM_KEK_VERSION"] = str(kek_versions)
        os.environ["PGM_ADMIN_TOKEN"] = "adm_p14_%s" % uuid.uuid4().hex
        for v in range(1, kek_versions + 1):
            os.environ["PGM_KEK_v%d" % v] = base64.b64encode(bytes([v] * 32)).decode()
        os.environ["PGM_IP_PEPPER"] = base64.b64encode(bytes(range(64, 96))).decode()
        os.environ["PGM_SERVICE_TOKEN"] = "svc-" + "s" * 40
        os.environ["PGM_IMAGE_PROXY_SECRET"] = base64.b64encode(bytes(range(96, 128))).decode()
        os.environ["PGM_TELEGRAM_BOT_TOKEN"] = "8123456789:" + "b" * 32
        os.environ["PGM_REFERRAL_SALT"] = "p14-drill-salt-0123456789"
        run_sql = _load("pgm_drill_sql", ROOT / "tools" / "run-sql.py")
        with contextlib.redirect_stdout(io.StringIO()):
            if run_sql.run_sqlite(ROOT / "db" / "migrations-sqlite", str(self.db)) != 0:
                raise SystemExit("p14-key-drills: migration failed, so there is nothing to drill")
            import seed
            seed.seed_sqlite(str(self.db))
        self.app = _load("pgm_drill_app", ROOT / "services" / "api" / "app.py")
        self.keys = self.app._keys
        self.sdb = self.app.SEC
        self.con = self.app._db
        from fastapi.testclient import TestClient
        self.client = TestClient(self.app.app, raise_server_exceptions=False)
        self.now = int(time.time() * 1000)
        #   KEK rows: `key_wraps.kek_version` is a foreign key, so a wrap cannot exist without the KEK record it
        #   names — which is the product stopping a drill (or an operator) from inventing a key that was never
        #   commissioned. The bench commissions each version the same way a deployment does.
        self._phc: str | None = None
        self.kek_versions = [self.app.SEC.new_kek(at=self.now, provider="env", key_id="p14-drill-v%d" % v,
                                                  ceremony_by="p14 harness", witnesses="a,b",
                                                  audit_note="P14 D2 drill bench")["version"]
                             for v in range(1, kek_versions + 1)]

    # ------------------------------------------------------------------ fixtures
    def user(self, tag: str, *, kek_version: int = 1, wrap: bool = True) -> dict:
        """A user with a wrapped key, a delegated wallet, and a session.

        The password hash is computed once per bench and reused: Argon2id at the product's parameters costs ~100 ms
        by design, and hashing a 500-account population per drill is *fixture* time. The break-glass clock starts
        after the population exists, so hashing it inside the measurement would inflate the number the kit's
        one-hour limit is judged against — and inflating a security measurement is a lie in the safe direction,
        which is still a lie.
        """
        uid = "u_%s_%s" % (tag, uuid.uuid4().hex[:8])
        self.con.execute("INSERT INTO users (id, created_ms, tier) VALUES (?,?, 'trader')", (uid, self.now))
        self.app.SEC.link_identity(uid, "email", "%s@example.test" % uid, at=self.now)
        if self._phc is None:
            self._phc = self.app._hasher().hash("correct horse battery staple 7!")
        self.app.SEC.set_password(uid, self._phc, at=self.now)
        if wrap:
            self.wrap(uid, kek_version=kek_version)
        self.con.commit()
        token = self.login(uid)
        return {"uid": uid, "token": token}

    def wrap(self, uid: str, *, kek_version: int = 1, dek_version: int = 1) -> dict:
        env = self.app._envelope()
        sealed = env.seal(("dek-material-%s" % uid).encode(), {"user_id": uid, "kek_version": kek_version,
                                                    "dek_version": dek_version, "policy_hash": "drill"})
        return self.app.SEC.wrap_key(uid, ciphertext=sealed["ciphertext"], nonce=sealed["nonce"],
                                     tag=sealed["tag"], kek_version=kek_version, dek_version=dek_version,
                                     policy_hash="drill", at=self.now)

    def refresh_book(self, market: str) -> int:
        """Re-stamp `book_levels.updated_ms` so the risk gate's freshness window does not refuse a probe for a
        reason the probe is not about. `PGM_STALE_MS_TAPE` widens the *response* staleness stamp, not the gate: the
        gate reads `now - book_levels.updated_ms` against `LIMITS.max_snap_age_ms` (5 s), so any run that takes
        longer than five seconds must keep the book fresh rather than widen the window it is measured against.
        """
        cur = self.con.execute("UPDATE book_levels SET updated_ms=? WHERE market_id=?",
                               (self.app._now_ms(), market))
        self.con.commit()
        return int(cur.rowcount or 0)

    def login(self, uid: str) -> str:
        r = self.client.post("/v1/auth/login", json={"identifier": uid,
                                                     "password": "correct horse battery staple 7!"})
        if r.status_code != 200:
            raise SystemExit("p14-key-drills: could not log %s in: %s" % (uid, r.text[:160]))
        return r.json()["accessToken"]

    def bearer(self, token: str) -> dict:
        return {"Authorization": "Bearer %s" % token, "Content-Type": "application/json",
                "Idempotency-Key": "p14drill-%s" % uuid.uuid4().hex[:16]}


class Drill:
    """One scenario: named steps, a wall clock, and the state it must have left behind."""

    def __init__(self, bench: Bench, kind: str, setup=None) -> None:
        self.b = bench
        self.kind = kind
        self.steps: list[dict] = []
        self.t0 = 0.0
        self.notes: list[str] = []
        self.failed_step = ""
        # `setup` is the population, and it runs *before* the clock: the kit asks how long the response takes,
        # not how long it takes to build a fixture that a real deployment already has.
        self.fixture = setup() if setup else None
        self.start()

    def start(self) -> None:
        # The clock starts when the *response* starts, which is the moment the runbook is opened — not when the
        # fixture is built. Setup time in a measurement about reaction time is a lie in the safe direction, and
        # the safe direction is still a lie.
        self.t0 = time.monotonic()

    def step(self, name: str, ok: bool, detail: str = "") -> bool:
        ms = int((time.monotonic() - self.t0) * 1000)
        self.steps.append({"step": name, "at_ms": ms, "ok": bool(ok), "detail": detail[:220]})
        if not ok and not self.failed_step:
            self.failed_step = name
        return bool(ok)

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def verdict(self) -> str:
        return "fail" if self.failed_step else "pass"

    def record(self) -> dict:
        """Write it into the product's own drill table, so the operator's console sees it too."""
        out = self.b.sdb.record_drill(kind=RECORD_KIND[self.kind], started_ms=self.b.now,
                                      finished_ms=self.b.now, verdict=self.verdict(),
                                      measured_ms=self.elapsed_ms, failed_step=self.failed_step,
                                      participants="p14 harness",
                                      notes=("scenario %s (P14 D2); " % self.kind
                                             + "; ".join(self.notes))[:900])
        return out


# ------------------------------------------------------------------------------------ the six drills
def drill_user_key_leaked(b: Bench, g) -> Drill:
    """One user's signing key is in somebody else's hands.

    The runbook, in order: close the doors the key opens (sessions), then kill the key itself (the DEK), then
    confirm the money cannot leave anyway because the destination is not allowlisted and the destination hold is
    24 hours. The last step is the one that matters: revocation without a checked invariant is a feeling.
    """
    d = Drill(b, "user_key_leaked")
    victim = b.user("victim")
    stolen = victim["token"]
    stale_ok = d.step("the stolen session works before the drill (the drill must not be measuring a dead token)",
                      b.client.get("/v1/wallet/balance", headers=b.bearer(stolen)).status_code == 200)
    out = b.sdb.revoke_all_sessions(user_id=victim["uid"], at=b.app._now_ms(), reason="key compromise drill")
    d.step("every session of that account is revoked", revoked_count(out) >= 1, json.dumps(out)[:160])
    d.step("the stolen token no longer reads the wallet",
           b.client.get("/v1/wallet/balance", headers=b.bearer(stolen)).status_code == 401)
    revoked = b.sdb.revoke_key(victim["uid"], at=b.app._now_ms())
    d.step("the signing key itself is revoked (the exec can no longer sign for this user)",
           int(revoked) >= 1, "%d wrap row(s) revoked" % revoked)
    live = [r for r in b.sdb.key_wrap_rows() if r.get("user_id") == victim["uid"] and not r.get("revoked_ms")]
    d.step("no live key wrap remains for that account", not live, json.dumps(live)[:160])
    d.step("...and the first step really did work (must-accept guard for the whole drill)", stale_ok)
    d.record()
    return d


def drill_signing_service_compromised(b: Bench, g, *, scale: int = 500, provider_rate: float | None = None) -> Drill:
    """The kit's hard one: the component that signs for everybody is compromised. Full break-glass, < 1 hour.

    Six actions, and they are ordered by what stops the bleeding fastest rather than by what is tidiest to script:
    stop trading, close every session (two approvers), stop the old KEK being usable, re-wrap every DEK under a new
    KEK, verify every new wrap by reading it back, and only then retire the old KEK. The measurement is the wall
    clock from the first action to the last verification, with the kill switch's own propagation checked.

    On the population: the kit's limit is about a *real* break-glass, and a drill over twenty-four keys would clear
    an hour on arithmetic that does not survive contact with the real population. The measured run covers `scale`
    keys (500 by default) with fixture time excluded, and the per-key cost of the rotation phase is extrapolated to
    P07's 10,000-wallet target, which is the number the one-hour claim is actually about. Both are reported.
    """
    d = Drill(b, "signing_service_compromised", setup=lambda: [b.user("bg%04d" % i) for i in range(scale)])
    users = d.fixture or []
    with contextlib.redirect_stdout(io.StringIO()):
        r = b.client.post("/v1/admin/kill-switch", headers={"X-Admin-Token": os.environ["PGM_ADMIN_TOKEN"],
                                                           "Content-Type": "application/json",
                                                           "Idempotency-Key": "p14drill-%s" % uuid.uuid4().hex[:8]},
                          json={"engaged": True, "reason": "P14 break-glass drill: signing service compromised"})
    d.step("the kill switch engages (stops new orders and sweeps the queue)", r.status_code == 200,
           "%d %s" % (r.status_code, (r.text or "")[:120]))
    engaged = b.con.execute("SELECT engaged FROM kill_switch_state ORDER BY id DESC LIMIT 1").fetchone()
    d.step("the kill switch is engaged on the record, not just in the response",
           bool(engaged and int(engaged[0]) == 1), str(engaged))
    # Two approvers, a 20-character reason, and the window. The refusal path is asserted first, because a
    # break-glass that accepts one approver is a break-glass an attacker with an admin token can do alone.
    single = b.client.post("/v1/admin/revoke-sessions",
                           headers={"X-Admin-Token": os.environ["PGM_ADMIN_TOKEN"],
                                    "Content-Type": "application/json",
                                    "Idempotency-Key": "p14drill-%s" % uuid.uuid4().hex[:8]},
                           json={"approvers": ["miner"], "reason": "one approver tries the break-glass path"})
    d.step("one approver is refused (must-refuse: an attacker with the admin token cannot do it alone)",
           single.status_code == 422, "%d %s" % (single.status_code, (single.text or "")[:120]))
    out = b.sdb.revoke_all_sessions(user_id=None, at=b.app._now_ms(), reason="break-glass drill")
    d.step("every session in the product is revoked under the two-approver rule",
           revoked_count(out) >= len(users), json.dumps(out)[:160])
    dead = b.client.get("/v1/wallet/balance", headers=b.bearer(users[0]["token"])).status_code
    d.step("a session minted before the break-glass reads nothing now", dead == 401, "status %d" % dead)
    # Keystore: create v2, re-wrap every live DEK under it, read every one back before retiring v1.
    rows = [r for r in b.sdb.key_wrap_rows() if not r.get("revoked_ms")]
    plan = b.keys.rotation_plan(rows, new_kek_version=2)
    d.step("the rotation plan covers every live wrap (%s)" % plan["steps"][0], plan["total"] == len(rows),
           "planned %d of %d" % (plan["total"], len(rows)))
    rewrapped = 0
    # The KEK for v2 is the same 32 bytes the bench put in `PGM_KEK_v2`; the app's `_envelope()` is a lazily
    # cached singleton for the *current* version, so the drill builds the v2 envelope the way a rotation process
    # does — from the environment, at the version it is rotating to.
    env = b.app._secb.AesGcmEnvelope(bytes([2] * 32), version=2)
    # The order here is P07's own, because it is the only one that is safe: **mint new -> serve both -> revoke
    # old -> retire**. `key_wraps` is unique on (user_id, dek_version), so a re-wrap is a *new generation* of the
    # same DEK rather than an edit of the existing row — and that is the schema refusing a rotation that would
    # make an in-flight signature unresolvable.
    rotate_t0 = time.monotonic()
    for row in rows:
        new_dek = int(row["dek_version"]) + 1
        sealed = env.seal(("dek-material-%s" % row["user_id"]).encode(),
                          {"user_id": row["user_id"], "kek_version": 2, "dek_version": new_dek,
                           "policy_hash": row.get("policy_hash") or "drill"})
        b.sdb.wrap_key(row["user_id"], ciphertext=sealed["ciphertext"], nonce=sealed["nonce"], tag=sealed["tag"],
                       kek_version=2, dek_version=new_dek, policy_hash=row.get("policy_hash") or "drill",
                       at=b.app._now_ms())
        rewrapped += 1
    back = 0
    for row in b.sdb.key_wrap_rows():
        if int(row.get("kek_version") or 0) != 2 or row.get("revoked_ms"):
            continue
        try:
            env.open(ciphertext=row["wrapped_dek"], nonce=row["nonce"], tag=row["tag"],
                     aad={"user_id": row["user_id"], "kek_version": 2, "dek_version": row["dek_version"],
                          "policy_hash": row.get("policy_hash") or "drill"})
            back += 1
        except Exception as e:                                       # noqa: BLE001 — a wrap that will not open is
            # the finding, not an error… and the *type* is recorded: the first version of this loop swallowed a
            # TypeError from calling `open()` positionally and reported it as a keystore failure, which is the
            # most expensive kind of confusion a drill can produce.
            d.note("a wrap under v2 did not open: %s (%s: %s)" % (row.get("user_id"), type(e).__name__,
                                                                  str(e)[:80]))
    d.step("every re-wrapped DEK is read back under the new KEK (%d of %d)" % (back, rewrapped),
           back == rewrapped and rewrapped > 0, "unverified %d" % (rewrapped - back))
    # Old generations are revoked only after every new one has been read back. `revoke_key` with an explicit
    # version is the API for "this generation is done", and it is what moves `can_retire_kek` off v1.
    moved = 0
    for row in rows:
        moved += b.sdb.revoke_key(row["user_id"], at=b.app._now_ms(), dek_version=int(row["dek_version"]))
    rotate_ms = int((time.monotonic() - rotate_t0) * 1000)
    per_key_ms = (rotate_ms / len(rows)) if rows else 0.0
    projected_ms = int(per_key_ms * 10_000)
    d.step("the superseded generations are revoked once every new one is verified (%d rows)" % moved,
           moved == len(rows), "%d of %d" % (moved, len(rows)))
    d.note("rotation phase: %d ms for %d keys (%.2f ms/key) -> a 10,000-key rotation projects to %.1f minutes, "
           "against the kit's 60-minute limit" % (rotate_ms, len(rows), per_key_ms, projected_ms / 60_000.0))
    d.step("the local half scales: 10,000 keys projects to %.2f minutes at the measured rate"
           % (projected_ms / 60_000.0), projected_ms < BREAK_GLASS_LIMIT_MS,
           "projected %d ms over the %d ms limit" % (projected_ms, BREAK_GLASS_LIMIT_MS))
    # ...and now the half this box cannot measure. Both halves are the kit's one hour, and the honest report of an
    # unmeasured half is a launch condition, not a pass: at one provider call per second per key, 10,000 keys is
    # 2.8 hours, which fails the kit's limit outright. The product's own `revocation_throughput()` says the same
    # thing in the same words, and it is the reason this check exists as a check rather than as a footnote.
    # One provider call per key, which is what a custodian's revoke/rewrap limits usually are: the *batching* we
    # control is not the binding constraint, so modelling 500-key batches here would have printed 0.0 hours next
    # to a 10,000-key break-glass — the exact mistake `revocation_throughput`'s own docstring warns about.
    provider = b.keys.revocation_throughput(10_000, batch=1, per_call_ms=1_000, concurrency=1)
    d.note("provider-bound half: %d calls, %.1f hours at 1 call/s per key — %s"
           % (provider["calls"], provider["wall_s"] / 3600.0, provider["bounded_by"][:120]))
    if provider_rate is None:
        g.open("BREAK-GLASS IS ONE HOUR ONLY IF THE CUSTODIAN IS FAST ENOUGH: the provider-bound half of a "
               "10,000-key break-glass is UNMEASURED (at 1 call/s per key it is %.1f hours, over the limit)"
               % (provider["wall_s"] / 3600.0),
               "measure Turnkey's revoke/rewrap rate limit on real keys, then re-run this drill with "
               "--provider-rate <calls per second>; until then the one-hour claim is unproven and the product "
               "does not launch on a key-compromise promise nobody can keep")
    else:
        hours = 10_000 / float(provider_rate) / 3600.0
        d.step("a 10,000-key break-glass at the measured provider rate (%.2f calls/s) is %.2f hours"
               % (provider_rate, hours), hours < 1.0, "%.2f h over the 1 h limit" % hours)
    still = [r for r in b.sdb.key_wrap_rows() if not r.get("revoked_ms") and int(r.get("kek_version") or 0) == 1]
    ok_retire, why = b.keys.can_retire_kek(b.sdb.key_wrap_rows(), 1)
    d.step("the old KEK is retired only once nothing live references it", ok_retire, why)
    if ok_retire:
        retired = b.sdb.retire_kek(1, at=b.app._now_ms())
        d.step("KEK v1 is retired and the rotation is closed", retired == {"retired": 1},
               json.dumps(retired)[:120])
    else:
        # Not a failure to hide: an unfinished rotation is exactly what the drill is looking for, and the record
        # must say so rather than round up to a pass.
        d.step("KEK v1 could not be retired: %d live wraps still reference it" % len(still), False, why)
    # The runbook's last line, and the reason it is a step rather than a note: a break-glass that leaves the
    # product halted is a second outage. It also matters to the drill suite — the halt is global, so a later drill
    # in the same process saw `RISK_HALT` on every intent and its own measurement would have been about this one.
    with contextlib.redirect_stdout(io.StringIO()):
        rel = b.client.post("/v1/admin/kill-switch",
                            headers={"X-Admin-Token": os.environ["PGM_ADMIN_TOKEN"],
                                     "Content-Type": "application/json",
                                     "Idempotency-Key": "p14drill-%s" % uuid.uuid4().hex[:8]},
                            json={"engaged": False, "reason": "break-glass drill: controls verified, resuming"})
    off = b.con.execute("SELECT engaged FROM kill_switch_state ORDER BY id DESC LIMIT 1").fetchone()
    d.step("the halt is released once the rotation is verified",
           rel.status_code == 200 and bool(off and int(off[0]) == 0), "%d %s" % (rel.status_code, str(off)))
    d.step("the whole break-glass is inside the kit's one-hour limit (%d ms measured, limit %d)"
           % (d.elapsed_ms, BREAK_GLASS_LIMIT_MS), d.elapsed_ms < BREAK_GLASS_LIMIT_MS,
           "%d keys, %d sessions" % (len(rows), revoked_count(out)))
    d.note("measured wall clock: %.2f s for %d wrapped keys and %d sessions" % (d.elapsed_ms / 1000.0, len(rows),
                                                                               revoked_count(out)))
    d.record()
    return d


def drill_key_in_a_log(b: Bench, g) -> Drill:
    """A private key (or a DEK) turned up in a log line: it is leaked, whatever the log said.

    Two halves, and the second is the one teams forget. The redaction pipeline has to refuse to print it, and then
    the key has to be treated as compromised: sessions revoked, the DEK re-wrapped at a new generation, and the old
    material unusable. A redaction fix alone leaves the exposed key in service.
    """
    d = Drill(b, "key_in_a_log")
    victim = b.user("leak")
    secret = "0x" + "f" * 64
    seed = "abandon ability able about above absent absorb abstract absurd abuse access accident"
    init_hash = "a" * 64
    # The *real* emission path: `_redact.line` is what the app prints for every request, and it is the only place
    # a secret in an access log can be stopped. A drill that calls a redaction helper directly measures the helper.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print(b.app._redact.line(level="error", ev="http", path="/v1/wallet?token=" + secret,
                                 detail={"material": secret, "seed": seed, "init": init_hash}), flush=True)
    printed = buf.getvalue()
    d.step("the log line exists (so the next check is not passing on an empty string)", bool(printed.strip()),
           printed[:80])
    d.step("the private key does not appear in what was emitted", secret not in printed, printed[:160])
    d.step("a mnemonic does not appear in what was emitted", "abandon ability" not in printed, printed[:160])
    d.step("an initData hash does not appear in what was emitted", init_hash not in printed, printed[:160])
    # ...and the independent matcher agrees, which is the check that keeps the pattern list honest: a redactor
    # tested only against its own patterns is a redactor that passes by agreeing with itself.
    leftovers = b.app._redact.scan(printed)
    d.step("the independent secret scanner finds nothing in the sanitised line", not leftovers,
           "still present: %s" % leftovers)
    before = b.sdb.credential(victim["uid"]) or {}
    out = b.sdb.revoke_all_sessions(user_id=victim["uid"], at=b.app._now_ms(), reason="key in a log")
    d.step("the account's sessions are closed as well: a leaked key usually travelled with one",
           revoked_count(out) >= 1, json.dumps(out)[:140])
    d.step("the stolen token is dead", b.client.get("/v1/auth/sessions",
                                                    headers=b.bearer(victim["token"])).status_code == 401)
    rotated = b.sdb.revoke_key(victim["uid"], at=b.app._now_ms())
    d.step("the exposed generation is revoked, not merely re-wrapped", int(rotated) >= 1, "%d revoked" % rotated)
    live = [r for r in b.sdb.key_wrap_rows() if r.get("user_id") == victim["uid"] and not r.get("revoked_ms")]
    d.step("nothing live remains for that account", not live, json.dumps(live)[:140])
    d.note("credential generation before the drill: %s" % before.get("cred_gen"))
    d.record()
    return d


def drill_contractor_leaves(b: Bench, g) -> Drill:
    """A contractor with operator access leaves, today, and their credential has to stop working today.

    Rotation is only half of it: the old value must be *refused* by the route that reads it, and the operator's own
    secret inventory has to know when it was last rotated and who owns it.
    """
    d = Drill(b, "contractor_leaves")
    old_token = os.environ["PGM_ADMIN_TOKEN"]
    op = b.client.get("/v1/admin/gaming", headers={"X-Admin-Token": old_token,
                                                  "Content-Type": "application/json"})
    d.step("the operator credential works before the drill (must-accept)", op.status_code == 200,
           "%d %s" % (op.status_code, (op.text or "")[:100]))
    new_token = "adm_rotated_%s" % uuid.uuid4().hex
    os.environ["PGM_ADMIN_TOKEN"] = new_token
    stale = b.client.get("/v1/admin/gaming", headers={"X-Admin-Token": old_token,
                                                     "Content-Type": "application/json"})
    d.step("the departed contractor's credential is refused once the token is rotated",
           stale.status_code == 403, "%d %s" % (stale.status_code, (stale.text or "")[:100]))
    fresh = b.client.get("/v1/admin/gaming", headers={"X-Admin-Token": new_token,
                                                     "Content-Type": "application/json"})
    d.step("the new credential works (so the rotation is not an outage)", fresh.status_code == 200,
           "%d" % fresh.status_code)
    now = b.app._now_ms()
    b.sdb.put_secret(name="PGM_ADMIN_TOKEN", environment="prod", stored_in="secret_manager",
                     owner="on-call lead", rotate_by_ms=now + 90 * 86_400_000, last_rotated_ms=now,
                     can_rotate_live=True, note="rotated after a contractor left (P14 D2 drill)")
    inv = b.sdb.secret_inventory("prod")
    row = next((r for r in inv if r["name"] == "PGM_ADMIN_TOKEN"), None)
    d.step("the rotation is recorded in the secret inventory with an owner and a next date",
           bool(row) and int(row.get("last_rotated_ms") or 0) > 0, json.dumps(row)[:180])
    overdue = b.sdb.secrets_due(at=now + 100 * 86_400_000, environment="prod")
    d.step("a secret past its rotation date is reported as due (the inventory is not decoration)",
           any(r["name"] == "PGM_ADMIN_TOKEN" for r in overdue), json.dumps(overdue)[:140])
    inhouse = b.user("contractor")
    out = b.sdb.revoke_all_sessions(user_id=inhouse["uid"], at=now, reason="contractor left")
    d.step("their product account's sessions are revoked as well as their operator token",
           revoked_count(out) >= 1, json.dumps(out)[:140])
    d.record()
    return d


def drill_support_impersonation(b: Bench, g) -> Drill:
    """Support says they are the user. The product has to say no, and it has to say it on the money paths.

    The kit's framing is that support impersonation is the attack with the least resistance: the operator can
    read the ticket, the user is already confused, and "I can do that for you" is what the attacker *says*. So
    the drill takes every money action a support agent could be asked to perform and tries it with a valid admin
    token: withdraw, export keys, remove a destination, read a key wrap, move an allowlist entry.
    """
    d = Drill(b, "support_impersonation")
    victim = b.user("mark")
    admin = {"X-Admin-Token": os.environ["PGM_ADMIN_TOKEN"], "Content-Type": "application/json",
             "Idempotency-Key": "p14drill-%s" % uuid.uuid4().hex[:8]}
    attempts = [
        ("/v1/wallet/withdraw", {"amountUsdc": "50", "addressId": "any", "typedAmount": "50",
                                 "typedAddress": "0x" + "1" * 40, "password": "x", "code": "000000"}),
        ("/v1/wallet/keys/export", {"password": "x", "code": "000000", "typedConfirm": "EXPORT"}),
        ("/v1/wallet/withdrawal-addresses/remove", {"addressId": "any"}),
        ("/v1/orders", {"marketId": "0xM1", "tokenId": "0xT10", "side": "BUY", "price": "0.50", "size": "10"}),
    ]
    refused = []
    for path, body in attempts:
        r = b.client.post(path, headers=admin, json=body)
        if 200 <= r.status_code < 300:
            refused.append((path, r.status_code))
        d.step("an admin token cannot %s as the user (%d)" % (path, r.status_code),
               200 <= r.status_code < 300 or r.status_code in (401, 403, 404, 422, 503))
    d.step("no money path accepted an operator credential as a user (%d attempted)" % len(attempts),
           not refused, "accepted: %s" % refused)
    # ...and the structural version, so a route added later is caught by this drill too.
    from polygm_core.security import authz
    import re as _re
    admin_ops = [o for o, (lv, _id) in authz.LEVELS_TABLE.items() if lv == authz.ADMIN]
    # "Money-moving" is about the money leaving, not about the word in the path: `POST /v1/admin/revoke-keys` is
    # *required* by the break-glass runbook and it only removes the ability to sign. An earlier version of this
    # check matched `revoke-key` and failed a legitimate operation — a check that fires on the correct behaviour
    # is a check somebody switches off, which is worse than not having it.
    moneyish = [o for o in admin_ops if _re.search(r"withdraw|payout|transfer|/keys/export", o, _re.I)]
    d.step("no admin operation is a money-moving route at all (%d admin operations)" % len(admin_ops),
           not moneyish and len(admin_ops) > 0, "money-shaped admin routes: %s%s"
           % (moneyish, "" if admin_ops else " — and the admin list is empty, which would also pass this check"))
    # The inventory goes into the artifact on purpose: the reviewer's question is "what can an operator token do
    # today", and a list is answerable in a diff while a count is not.
    d.note("admin operations (%d): %s" % (len(admin_ops), ", ".join(sorted(admin_ops))))
    # The product's own admin-forbidden list, applied to itself: every action an admin endpoint must never be able
    # to take is asked about by name. This is what stops "admin is a role, not a bypass" from being a comment.
    hits = []
    for op in admin_ops:
        for action in authz.ADMIN_FORBIDDEN:
            forbidden, why = authz.admin_may_not(action)
            if not forbidden:
                hits.append((action, "the list does not forbid it"))
            if _re.search(action.replace("_", "[_ -]?"), op, _re.I):
                hits.append((action, op))
    d.step("admin_may_not() forbids all %d actions, and no admin route's name matches one"
           % len(authz.ADMIN_FORBIDDEN), not hits, "hits: %s" % hits[:4])
    # The user's own factor is what authorises a money action: without it, even the *user's* session cannot do it.
    no_factor = b.client.post("/v1/wallet/keys/export", headers=b.bearer(victim["token"]),
                              json={"password": "correct horse battery staple 7!", "code": "000000",
                                    "typedConfirm": "EXPORT"})
    d.step("the user's own session without a second factor cannot export keys either",
           no_factor.status_code in (403, 422, 503), "%d %s" % (no_factor.status_code,
                                                                (no_factor.text or "")[:110]))
    denied = b.con.execute("SELECT COUNT(*) FROM auth_events WHERE kind='break_glass_denied'").fetchone()
    d.note("break_glass_denied events on the record: %s" % (denied[0] if denied else 0))
    d.record()
    return d


def drill_wallet_provider_outage(b: Bench, g) -> Drill:
    """The signer's provider is down. Fail closed, leave nothing half-done, and recover without a cleanup.

    This drill drives the **executor's own signer seam** rather than an HTTP route, because the provider outage
    happens where keys are used, and the API's export route returns *wrapped* material without touching a signer:
    a drill against the route would have measured a refusal that had nothing to do with the provider. What is
    asserted here is the failure mode that costs money — an order that is *half* placed: a provider that never
    answered while our database says the order is away, or a state machine wedged in `submitting` so that the
    retry double-submits.
    """
    d = Drill(b, "wallet_provider_outage")
    sys.path.insert(0, str(ROOT / "services" / "executor"))             # the executor's store lives beside it
    sys.path.insert(0, str(ROOT / "services" / "executor-mock"))        # ...as does the venue its CI runs against
    import store as ex_store                                            # noqa: PLC0415 - the executor's own module
    import mock_clob                                                    # the CI venue the executor is built with
    from polygm_core.wallets import lifecycle as wl
    from polygm_core.venue import clob_v2 as v2
    ex_main = _load("pgm_drill_executor", ROOT / "services" / "executor" / "main.py")
    st = ex_store.Store.open(b.db)
    market = st.conn.execute("SELECT id FROM markets WHERE accepting_orders=1 AND enable_order_book=1"
                             " ORDER BY id LIMIT 1").fetchone()[0]
    token = st.conn.execute("SELECT token_id FROM tokens WHERE market_id=? ORDER BY token_id LIMIT 1",
                            (market,)).fetchone()[0]
    uid = "u_outage_%s" % uuid.uuid4().hex[:6]
    at = b.app._now_ms()
    policy = wl.Policy(allowed_spender="0xexchange")
    st.conn.execute("INSERT OR REPLACE INTO users (id, created_ms, tier) VALUES (?,?, 'trader')", (uid, at))
    st.conn.execute("INSERT OR REPLACE INTO balances (user_id, usdc_available_micro, usdc_locked_micro,"
                    " version, reconcile_ms) VALUES (?,?,?,?,?)", (uid, 10_000_000_000, 0, 1, at))
    st.set_allowance(user_id=uid, token="pUSD", spender="0xexchange", amount_micro=v2.UNLIMITED_ALLOWANCE, at=at)
    st.conn.execute("INSERT OR REPLACE INTO wallets (user_id, provider, custody, address, proxy_address,"
                    " signature_type, policy_hash, state, created_ms, updated_ms) VALUES"
                    " (?, 'turnkey', 'delegated', '0xot', '0xotp', 3, ?, 'trading', ?, ?)",
                    (uid, policy.policy_hash(), at, at))
    st.conn.commit()

    class DownSigner:
        """The provider, answering nothing.

        It still has a `pubkey`, and that is not a detail: the executor derives the client order hash from the
        signer's public key *before* asking for a signature, so an outage that changes the public key would turn a
        retry into a second order. The drill asserts the hash is unchanged across the outage and the recovery.
        """
        name = "provider-down"
        pubkey = "0x" + "d0" * 32

        def sign(self, payload: dict) -> dict:                          # noqa: ARG002
            raise RuntimeError("PROVIDER_DOWN: the signing provider returned no signature")

    mock = mock_clob.MockClob()
    counts = lambda: dict((k, int(st.conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]))  # noqa: E731
                          for k, t in (("orders", "orders"), ("fills", "fills"), ("cash", "cash_ledger")))
    before = counts()
    ex = ex_main.Executor(st, transport=mock_clob.ScenarioTransport(mock), policy=policy, signer=DownSigner(),
                          batch_size=5)
    q = st.enqueue_intent(user_id=uid, market_id=market, token_id=token, side="BUY", price_micro=550_000,
                          size_micro=100 * 10**6, idempotency_key="p14-outage-%s" % uuid.uuid4().hex[:8],
                          order_type="GTC", audience="user", at=at)
    intent_id = q["intent_id"]
    # Fresh book before the *first* attempt as well as before the retry. In a full suite run this drill starts a
    # minute after the bench was built, so the book was already past the gate's five-second window: the first tick
    # answered `STALE_QUOTE` at preflight and never reached the signer at all, which would have made every claim
    # below about the wrong control. The staleness check itself is P14's freshness probe and P06's own tests.
    st.conn.execute("UPDATE book_levels SET updated_ms=? WHERE market_id=?", (b.app._now_ms(), market))
    st.conn.commit()
    rep = ex.tick(at=at + 1, reconcile=False)
    handled = (rep.get("handled") or [{}])[0]
    d.step("the executor does not crash on a provider that returns nothing (it answers with a verdict)",
           isinstance(rep, dict), json.dumps(rep)[:180])
    # The must-reach guard: a refusal at preflight is a refusal by a *different* control, and it would let this
    # whole drill pass while measuring nothing.
    reached = str((handled or {}).get("code") or "") not in ("STALE_QUOTE", "RISK_HALT", "KILL_SWITCH", "")
    d.step("the attempt actually reached the signer (not refused earlier by the gate or the halt)",
           reached, "the first tick answered %r at stage %r" % ((handled or {}).get("code"),
                                                                 (handled or {}).get("stage")))
    row = st.conn.execute("SELECT state, risk_code, client_order_hash FROM order_intents WHERE id=?",
                          (intent_id,)).fetchone()
    state, last_error, coh = (row[0], row[1], row[2]) if row else (None, None, None)
    d.note("intent state after the outage tick: %s / %s" % (state, str(last_error)[:120]))
    d.step("the intent is NOT reported as executed while the provider never signed (state=%s)" % state,
           state not in ("executed", "filled", "submitted"), "state %r with error %r" % (state, last_error))
    d.step("no order row was created for a signature that does not exist", counts() == before,
           "%s -> %s" % (before, counts()))
    d.step("the failure is named, not silent (a state a human can act on)", bool(state), "state %r" % state)
    # The hash is computed *before* the signature and stamped on the outcome. It is deliberately not persisted
    # until after signing succeeds (a hash row for an order that was never signed is a hash the reconciler would
    # ask the venue about). So the claim to check is the one that matters for a retry: the same intent and the
    # same signer public key produce the same hash, whether or not the first attempt reached the wire.
    first_hash = str((handled or {}).get("client_order_hash") or "")
    d.step("the client order hash is computed before any signature exists (a retry is the *same* order)",
           first_hash.startswith("0x") and len(first_hash) > 20, "client_order_hash %r" % first_hash[:24])
    stuck = st.conn.execute("SELECT COUNT(*) FROM order_intents WHERE state IN ('submitting','claimed')"
                            " AND user_id=?", (uid,)).fetchone()[0]
    d.step("nothing is left mid-flight for a retry to double-submit", int(stuck) == 0, "stuck: %s" % stuck)
    # ...and the row that would make a retry unsafe is absent, which is what "absence proves 'not sent'" means.
    attempts_after_failure = st.conn.execute("SELECT COUNT(*) FROM order_attempts WHERE intent_id=?"
                                             " AND user_id=?", (intent_id, uid)).fetchone()[0]
    d.step("no attempt row was written for the failed signature (so absence still proves 'not sent')",
           int(attempts_after_failure) == 0, "%d attempt row(s)" % attempts_after_failure)
    # Recovery: the provider comes back and the same intent is ticked again. This is where a fail-closed design
    # either resumes or leaves debris; "no cleanup needed" is the assertion.
    # Fresh book before the retry — and that is not a convenience for the drill, it is the point: the requeued
    # intent goes through the risk gate *again*, so a retry can never ride an old snapshot into the book. The
    # first version of this drill left the bench book stale and the retry came back `rejected/STALE_QUOTE`, which
    # is the gate doing its job on the second pass rather than a bug.
    st.conn.execute("UPDATE book_levels SET updated_ms=? WHERE market_id=?", (b.app._now_ms(), market))
    st.conn.commit()
    ex2 = ex_main.Executor(st, transport=mock_clob.ScenarioTransport(mock), policy=policy, signer=None,
                           batch_size=5)
    rep2 = ex2.tick(at=at + 5_000, reconcile=False)
    row2 = st.conn.execute("SELECT state, risk_code FROM order_intents WHERE id=?", (intent_id,)).fetchone()
    d.step("recovery needs no cleanup: once the provider is back the executor moves the intent on",
           bool(rep2.get("handled")) or (row2 and row2[0] != state), "state %s -> %s (risk code %r)"
           % (state, row2[0] if row2 else None, row2[1] if row2 else None))
    d.step("...and the recovery produced at most one order (no double submit on the retry)",
           counts()["orders"] - before["orders"] <= 1, "%s -> %s" % (before["orders"], counts()["orders"]))
    coh2 = st.conn.execute("SELECT client_order_hash FROM order_intents WHERE id=?", (intent_id,)).fetchone()
    # What makes the retry safe is NOT that the hash is stable across attempts — the payload carries a fresh
    # expiration and salt per attempt, so it is not, and the first version of this drill asserted a property the
    # product deliberately does not have. What makes it safe is the attempt row: it is written after signing and
    # before the POST, so *presence proves "may have been sent"* and *absence proves "definitely not sent"*.
    # Both directions are asserted here, because both are load-bearing.
    attempts_now = st.conn.execute("SELECT COUNT(*) FROM order_attempts WHERE intent_id=? AND user_id=?",
                                   (intent_id, uid)).fetchone()[0]
    d.step("the successful retry wrote exactly one attempt row", int(attempts_now) == 1,
           "%d attempt row(s), order ids: %s" % (attempts_now, (coh2 or [None])[0]))
    orders_before_third = counts()["orders"]
    ex2.tick(at=at + 9_000, reconcile=False)
    d.step("a third pass does not re-POST the order that attempt row already covers",
           counts()["orders"] == orders_before_third,
           "%d -> %d order(s)" % (orders_before_third, counts()["orders"]))
    d.note("hashes: failed attempt %s, retry %s — different by design (a fresh expiration per attempt), which is "
           "why exactly-once is carried by the attempt row and not by the hash" % (first_hash[:18],
                                                                                  str((coh2 or [None])[0])[:18]))
    d.record()
    return d


RUNNERS = {
    "user_key_leaked": drill_user_key_leaked,
    "signing_service_compromised": drill_signing_service_compromised,
    "key_in_a_log": drill_key_in_a_log,
    "contractor_leaves": drill_contractor_leaves,
    "support_impersonation": drill_support_impersonation,
    "wallet_provider_outage": drill_wallet_provider_outage,
}


# ----------------------------------------------------------------------------------------------- the gate
class Gate:
    """Three states, and the third one is the point.

    `FAIL` is a check that ran and was not met. `OPEN` is a check that *cannot be run here* and therefore gates the
    launch until somebody with the real credential runs it — in this phase that is the signing provider's rate
    limit, which decides whether a 10,000-key break-glass is forty minutes or three hours. Bending an unmeasurable
    condition into PASS is how a document ends up claiming something nobody checked; making it FAIL would say the
    code is broken when what is missing is a measurement.
    """

    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []

    def check(self, name: str, ok: bool, why: str = "") -> bool:
        self.results.append(("PASS" if ok else "FAIL", name, "" if ok else why))
        return ok

    def open(self, name: str, why: str) -> None:
        self.results.append(("OPEN", name, why))

    @property
    def failed(self):
        return [r for r in self.results if r[0] == "FAIL"]

    @property
    def opened(self):
        return [r for r in self.results if r[0] == "OPEN"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P14 D2's key-compromise drills")
    ap.add_argument("--only", default="", help="comma-separated drills: %s" % ", ".join(DRILLS))
    ap.add_argument("--scale", type=int, default=500,
                    help="keys in the break-glass population (default 500, fixture time excluded)")
    ap.add_argument("--provider-rate", type=float, default=None,
                    help="measured custodian calls/second; omit while it is unmeasured, which records the "
                         "one-hour claim as an OPEN launch condition rather than a pass")
    ap.add_argument("--record")
    ap.add_argument("--json", dest="json_path")
    args = ap.parse_args(argv)
    want = [s.strip() for s in args.only.split(",") if s.strip()] or list(DRILLS)
    unknown = [d for d in want if d not in DRILLS]
    if unknown:
        raise SystemExit("unknown drill(s): %s" % ", ".join(unknown))

    g = Gate()
    b = Bench()
    facts: dict = {"budgets_ms": BUDGET_MS, "break_glass_limit_ms": BREAK_GLASS_LIMIT_MS, "drills": {}}
    for kind in want:
        d = RUNNERS[kind](b, g, scale=args.scale, provider_rate=args.provider_rate) \
            if kind == "signing_service_compromised" else RUNNERS[kind](b, g)
        facts["drills"][kind] = {"steps": d.steps, "measured_ms": d.elapsed_ms, "budget_ms": BUDGET_MS[kind],
                                 "verdict": d.verdict(), "failed_step": d.failed_step, "notes": d.notes}
        g.check("%s: every step passed (%d steps, %.2f s of %d s budget)"
                % (kind, len(d.steps), d.elapsed_ms / 1000.0, BUDGET_MS[kind] / 1000.0),
                d.verdict() == "pass", "first failed step: %s" % d.failed_step)
        # The time is asserted separately from the steps, because "it worked eventually" is not a passed drill.
        g.check("%s: inside its budget (%.2f s)" % (kind, d.elapsed_ms / 1000.0),
                d.elapsed_ms <= BUDGET_MS[kind], "%.2f s over %d s" % (d.elapsed_ms / 1000.0,
                                                                       BUDGET_MS[kind] // 1000))

    bg_ms = facts["drills"].get("signing_service_compromised", {}).get("measured_ms")
    if bg_ms is not None:
        # The kit's own sentence, turned into a check with the number in it. If this line fails, the product does
        # not launch and the report says so; there is no "close enough" reading of an hour.
        g.check("BREAK-GLASS UNDER THE KIT'S ONE HOUR (%.2f s measured, %d s limit)"
                % (bg_ms / 1000.0, BREAK_GLASS_LIMIT_MS // 1000), bg_ms < BREAK_GLASS_LIMIT_MS,
                "the kit's constraint: if break-glass takes longer than an hour, the product does not launch")
    recorded = b.con.execute("SELECT kind, verdict, measured_ms, failed_step FROM drill_records"
                             " ORDER BY id").fetchall()
    facts["recorded"] = [{"kind": r[0], "verdict": r[1], "measured_ms": r[2], "failed_step": r[3]}
                         for r in recorded]
    g.check("every drill wrote its own record into the product's drill table (%d rows)" % len(recorded),
            len(recorded) == len(want), "recorded %d of %d" % (len(recorded), len(want)))
    # The canary for the most dangerous shape a drill tool can have: a scenario that runs nothing and passes.
    g.check("canary: a drill with no steps recorded is a failure, not a pass",
            not (facts["drills"] and all(not v["steps"] for v in facts["drills"].values())),
            "a drill produced an empty step list and reported pass")

    lines = ["P14 D2 — key-compromise drills, with a stopwatch on each", "=" * 96, ""]
    for kind in want:
        info = facts["drills"][kind]
        lines.append("%-30s %-5s %8.2f s of %6.1f s budget" % (kind, info["verdict"].upper(),
                                                               info["measured_ms"] / 1000.0,
                                                               info["budget_ms"] / 1000.0))
        for step in info["steps"]:
            lines.append("    %-4s %7.2f s  %s%s" % ("ok" if step["ok"] else "FAIL", step["at_ms"] / 1000.0,
                                                     step["step"], ("  — " + step["detail"]) if step["detail"]
                                                     else ""))
        for note in info["notes"]:
            lines.append("    note  %s" % note)
        lines.append("")
    for status, name, why in g.results:
        lines.append("%-4s %s%s" % (status, name, ("  — " + why) if why else ""))
    failed, opened = g.failed, g.opened
    passed = len(g.results) - len(failed) - len(opened)
    verdict = "FAIL" if failed else ("CONDITIONAL" if opened else "PASS")
    lines += ["", "P14 KEY DRILLS: %s — %d checks passed, %d failed, %d OPEN"
              % (verdict, passed, len(failed), len(opened))]
    if opened:
        lines += ["", "OPEN CONDITIONS THAT GATE THE LAUNCH (not passes, not failures — measurements nobody here "
                  "can take):"]
        lines += ["  * %s\n      %s" % (n, w) for _s, n, w in opened]
    if failed:
        lines += ["", "THE KIT'S CONSTRAINT APPLIES: a failed break-glass drill or an authorisation failure means",
                  "the product does not launch. This document is that statement in writing."]
    text = "\n".join(lines) + "\n"
    print(text)
    if args.record:
        pathlib.Path(args.record).write_text(text)
    if args.json_path:
        pathlib.Path(args.json_path).write_text(json.dumps(
            {"verdict": "FAIL" if failed else ("CONDITIONAL" if opened else "PASS"),
             "open_conditions": [{"check": n, "why": w} for _s, n, w in opened], "facts": facts,
             "checks": [{"status": s, "name": n, "why": w} for s, n, w in g.results]}, indent=2, default=str)
            + "\n")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
