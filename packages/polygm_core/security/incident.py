"""D9 · incident response, as data the gate can hold us to.

A runbook in a wiki is a hope. This module holds the severity definitions, the ordered first-60-minutes steps
(each with an owner and the *test or rehearsal* that proves the step works), the notification templates written
before they are needed, and the two alarm lists that people mix up: the alarms that mean "we are broken" and
the alarms that mean "we are being breached". The drill's own record lives in `drill_records`, and the gate
refuses a phase that claims readiness with no drill in the last 90 days.

Two rules that are easy to violate under pressure and therefore belong in code:

* **Preserve before you clean up.** The instinct is to restore the backup and get the product back. The
  forensic window closes the moment you do, and a post-mortem written without the logs is a guess.
* **Users are told at the 60-minute mark, not at the end of the investigation.** A template that minimises
  ("no evidence of unauthorised access at this time") is the sentence that ends up in the screenshot.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Step:
    n: int
    action: str
    owner: str
    minutes: int
    verify: str
    test_ref: str

    def as_dict(self) -> dict:
        return {"n": self.n, "action": self.action, "owner": self.owner, "minutes": self.minutes,
                "verify": self.verify, "test_ref": self.test_ref}


@dataclass(frozen=True)
class Severity:
    id: str
    means: str
    examples: tuple[str, ...]
    page_within_s: int
    ack_within_ms: int
    who: tuple[str, ...]
    customer_notice: str

    def as_dict(self) -> dict:
        return {"id": self.id, "means": self.means, "examples": list(self.examples),
                "page_within_s": self.page_within_s, "ack_within_ms": self.ack_within_ms, "who": list(self.who),
                "customer_notice": self.customer_notice}


SEVERITIES: tuple[Severity, ...] = (
    Severity("S1", "user money can move without a user asking, or keys may be exposed",
             ("a signature appears for a wallet whose owner did not trade",
              "a withdrawal address appears that the user never added",
              "the keystore answers for a key we did not authorise",
              "any auth_events burst of session mints across many users"),
             30, 5 * 60 * 1000, ("founder-on-call", "security-owner", "executor owner"),
             "told within the hour, in plain language, with the money status and the next update time"),
    Severity("S2", "data may have left, or integrity of what we display is in question",
             ("a read of auth_events/positions by an unexpected principal",
              "a market-metadata injection that reached a broadcast",
              "a dependency with a fix available that we ignored for a release"),
             300, 30 * 60 * 1000, ("founder-on-call", "the owning engineer"),
             "told when we know what was exposed and what is not"),
    Severity("S3", "the product is degraded but the money is safe",
             ("ingest down so prices are stale", "the venue rate-limits us", "an executor node loop-crashing"),
             1800, 2 * 3600 * 1000, ("the owning engineer",),
             "a status page line, not a push"),
    Severity("S4", "cosmetic or self-inflicted noise", ("a dashboard that lies", "a flaky test"),
             0, 0, ("whoever noticed",), "nothing"),
)

# "We are being breached" — these page the security owner even at 04:00 and even if everything is green.
BREACH_ALARMS: tuple[str, ...] = (
    "policy_drift",                 # P06: a signature asked for under a policy that is not the one we stored
    "keystore_unwrap_burst",        # more unwraps than orders: someone is reading keys, not signing
    "session_mint_burst",           # many mints, few logins
    "refresh_reuse",                # a rotated token presented again: a thief has the old one
    "totp_lockout_burst",           # many accounts locked at once: a spray, not clumsy users
    "withdrawal_address_churn",     # add-then-use-then-remove inside the cooldown window
    "export_after_hours",           # a key export from an account that has never exported
    "unattributed_order_surge",     # orders arriving with no session and no rule: a service token leaked
    "telegram_replay_refused",      # a valid initData presented twice
    "broadcast_gate_refused_spike", # someone is fishing our megaphone
    "admin_path_touched",           # any break-glass open/close, always, even when legitimate
)

# "We are broken" — page during the day, and never let these reach the security owner's phone at 04:00 unless
# they persist, because an alarm that cries wolf gets silenced and the one that matters gets missed with it.
OPS_ALARMS: tuple[str, ...] = (
    "ingest_lagging", "ingest_down", "venue_rate_limited", "executor_tick_slow", "reconcile_backlog",
    "breaker_open", "kill_switch_engaged", "queue_backlog", "cache_stale_serving",
)


def first_60_minutes(*, scope: str = "key_compromise") -> list[Step]:
    """The order that limits loss. `minutes` is a deadline for the step, not an estimate."""
    common = [
        Step(1, "Declare the severity out loud in the incident channel and page the owners in "
                "`SEVERITIES[severity].who`. The page *is* the clock.", "whoever is on call", 2,
             "the channel has 2 named humans and a severity string", "test:tools/p07-drill.py step 1"),
        Step(2, "Engage the kill switch with a reason. New orders stop; the queue is swept to "
                "rejected/RISK_HALT.", "founder-on-call", 5,
             "POST /v1/admin/kill-switch returned engaged=true, and no intent moved after the grace window",
             "gate:c_kill_switch_latency (P06)"),
        Step(3, "Revoke every session and every refresh token. This is the attacker's foothold and it is "
                "ours to take in one statement.", "security owner", 10,
             "an old token returns 401; auth_events shows one revoke_all row",
             "test:tests/test_security_auth.py::test_revoke_everywhere_kills_old_tokens"),
        Step(4, "Freeze withdrawals, key export and address changes at the *policy* level, not by asking.",
             "security owner", 12, "a withdrawal attempt is refused with a reason that names the freeze",
             "gate:c_export_and_withdrawal_gated"),
        Step(5, "Revoke the affected keys at the provider; if the blast radius is unknown, revoke all of them "
                "and re-provision after. Measure it, do not hope it.", "executor owner", 25,
             "the revoke_jobs row has finished_ms and failed == 0", "test:tools/p07-drill.py measures the run"),
        Step(6, "Preserve: DB snapshot, the auth_events window, the executor's log files, the venue's order "
                "list for the affected wallets. Write each item's sha256 into the incident record.",
             "security owner", 35, "the incident row lists four hashes", "gate:c_forensics_are_named"),
        Step(7, "Tell the users. The template, with the money status and the time of the next update. Do not "
                "wait for the investigation.", "founder", 45,
             "the message exists in the incident record and was sent to every affected owner",
             "gate:c_notify_template_does_not_minimise"),
        Step(8, "Freeze the incident record: what we know, what we do not, who changed what during the "
                "window. The post-mortem starts here, not in two weeks.", "security owner", 60,
             "the record is append-only and has an owner for every open question", "gate:c_incident_model_is_a_query"),
    ]
    if scope == "channel_poison":
        return [Step(1, "Stop the broadcast fan-out (one flag, not a deploy).", "alerts owner", 2,
                     "no new messages leave", "gate:c_broadcast_gate_refused_is_loud")] + common[1:]
    return common


def severity_for(*, money_moving: bool, keys_exposed: bool, data_left: bool, degraded: bool,
                 cosmetic: bool = False) -> str:
    """The classification rule, so two people at 4am reach the same page target."""
    if money_moving or keys_exposed:
        return "S1"
    if data_left:
        return "S2"
    if degraded:
        return "S3"
    if cosmetic:
        return "S4"
    return "S3"


TEMPLATES: dict[str, str] = {
    "key_compromise": (
        "Security incident — what we know and what we are doing\n\n"
        "At about {time} ({tz}), we found that the keys used to sign trades on our platform may have been "
        "accessible to someone who should not have had them. We stopped all trading and all withdrawals at "
        "{stop_time}, and we are revoking and re-issuing the affected keys.\n\n"
        "What this means for your money: your deposits are not reachable by that party without going through "
        "your own account, and we have blocked every withdrawal while we check. If any movement we cannot "
        "attribute to you is found, we will say so here, in the same words, and we will cover it.\n\n"
        "What we are not saying: we do not yet know the full scope. We will update at {next_update}, whether "
        "or not there is good news."
    ),
    "data_exposure": (
        "We found a read of data we did not intend to expose\n\n"
        "Between {from_time} and {to_time}, {what} could be read by {who}. We have closed it. We are not "
        "minimising this: if we can rule out that your information was included, we will tell you how we "
        "ruled it out.\n\nNo key changed and no money moved as a result of this read: your wallet, your "
        "withdrawal address list and your trading limits are exactly as you left them.\n\nWe will update you "
        "by {next_update}, whether or not there is anything new to say."
    ),
    "channel_poison": (
        "A message we sent was misleading\n\n"
        "At {time} our alert channel broadcast a market that had been set up to look like something it was "
        "not. Nothing was moved and no funds were at risk from us, but if you acted on that message, the "
        "position is yours to close and we will cover the fees on any trade you made in the {window} after it. "
        "We have changed the gate that let it through: {fix}.\n\nWe will update you by {next_update}, and the "
        "fee credit needs no form: it lands on the same balance the trade came from."
    ),
}

FORBIDDEN_PHRASES: tuple[str, ...] = (
    "no evidence of", "we believe there is no", "at this time", "isolated incident", "out of an abundance of "
    "caution", "no indication that",
)


def template_ok(kind: str) -> dict:
    """The style rule, checked: the templates must not contain the phrases that exist to make an event smaller.

    A security engineer reading this doc should find it odd that a *word list* is a control. It is here because
    every post-mortem of a crypto product that lost trust lost it in the notification, not in the breach.
    """
    body = TEMPLATES.get(kind) or ""
    low = body.lower()
    hits = [p for p in FORBIDDEN_PHRASES if p in low]
    return {"kind": kind, "exists": bool(body), "minimising": hits,
            "has_next_update_time": "next update" in low or "we will update" in low,
            "has_money_status": "money" in low or "withdrawal" in low or "fees" in low,
            "ok": bool(body) and not hits and ("{next_update}" in body or "{to_time}" in body or "{window}" in body)}


DRILL_MAX_AGE_MS = 90 * 24 * 3600 * 1000           # run the key-compromise drill before launch, and quarterly
BACKUP_MAX_AGE_MS = 7 * 24 * 3600 * 1000            # weekly, restored and *read back*
BACKUP_MAX_AGE_STAGING_MS = 30 * 24 * 3600 * 1000   # the keystore's own backup: monthly restore test


def drill_is_current(last_ms: int | None, at_ms: int, *, max_age_ms: int = DRILL_MAX_AGE_MS) -> tuple[bool, str]:
    if not last_ms:
        return False, "no drill on record: an untested procedure is a paragraph"
    age = int(at_ms) - int(last_ms)
    if age > max_age_ms:
        return False, "last drill was %d days ago (limit %d)" % (age // 86_400_000, max_age_ms // 86_400_000)
    return True, "drilled %d days ago" % (age // 86_400_000)


def backup_is_current(row: dict | None, at_ms: int, *, max_age_ms: int = BACKUP_MAX_AGE_MS) -> tuple[bool, str]:
    """`encrypted` and `money_checks_ok` are columns, not adjectives: an unverified restore is not a backup."""
    if not row:
        return False, "no restore test on record"
    if not int(row.get("encrypted") or 0):
        return False, "the backup is not encrypted, so it is a copy of the incident waiting to happen"
    if not int(row.get("money_checks_ok") or 0):
        return False, "the restore was not read back (verified_rows=%r)" % row.get("verified_rows")
    age = int(at_ms) - int(row.get("restore_done_ms") or row.get("restore_started_ms") or 0)
    if age > max_age_ms:
        return False, "last verified restore was %d days ago (limit %d)" % (age // 86_400_000,
                                                                            max_age_ms // 86_400_000)
    return True, "verified restore %d days ago" % (age // 86_400_000)


def runbook_is_wired() -> dict:
    """Every step has an owner and something that proves it. The gate calls this and fails on an empty string,
    because the failure mode of a runbook is the step that was "obviously" somebody's job.
    """
    bad = [{"n": s.n, "missing": [k for k, v in (("owner", s.owner), ("verify", s.verify),
                                                  ("test", s.test_ref)) if not str(v).strip()]}
           for s in first_60_minutes()]
    return {"steps": len(first_60_minutes()), "incomplete": [b for b in bad if b["missing"]],
            "ok": not any(b["missing"] for b in bad),
            "severities": [s.id for s in SEVERITIES], "breach_alarms": len(BREACH_ALARMS),
            "ops_alarms": len(OPS_ALARMS)}
