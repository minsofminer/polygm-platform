"""Running the bot: the kill switch, the broadcast tool, and what to do when the account is the problem.

D8 is the phase's least glamorous deliverable and the one that decides whether a bad hour is recoverable. Three
things live here, and each one exists because the alternative is finding out the hard way:

* **A kill switch for the bot that is separate from P06's trading kill switch.** They stop different things, and
  conflating them is how an operator who wants to stop *messages* ends up stopping *trades*: the trading switch
  halts orders and leaves the outbox alone (a user must still be told what happened to their money); the bot switch
  pauses delivery and leaves trading alone. Both are recorded, both are reversible, and neither can be engaged from
  a place a user can reach.
* **Broadcast tooling with a dry run and a staged rollout.** A channel message goes to thousands of phones and
  cannot be edited out of them, so the tool renders the exact bytes first (`dry_run`), and a real send carries the
  audience it went to. The rollout is *staged by kind and category* rather than by percentage: "the first ten large
  fills" is a rollout a human can reason about, and a random 10% of subscribers is not.
* **The recovery path for a restricted or banned bot.** Telegram can restrict a bot, delete it, or ban the channel,
  and the product's answer is that Discord mirrors the channel and the web is the primary hedge — so a ban costs
  distribution, not access. The `recovery_steps()` function is that plan as data, printed by the runbook and
  asserted by the gate, because a plan that only exists in a wiki is a plan nobody has read at 2am.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Why an operator stopped the bot. A reason is required and its shape is enforced: an unexplained kill switch is
#: indistinguishable from an outage, and the first question in the incident channel is always "was that us".
MIN_REASON = 8
MAX_REASON = 400

KILL_SCOPES = ("all", "channel", "personal")     # stop everything, the megaphone only, or one-to-one only


@dataclass
class KillState:
    engaged: bool = False
    scope: str = "all"
    reason: str = ""
    by: str = ""
    at_ms: int = 0

    def as_row(self) -> dict:
        return {"engaged": 1 if self.engaged else 0, "scope": self.scope, "reason": self.reason[:MAX_REASON],
                "changed_by": self.by, "at_ms": int(self.at_ms)}


def kill_findings(engaged: bool, *, scope: str, reason: str, by: str = "") -> list:
    """A kill switch's own rules: what it stops, and what a restart must not silently undo.

    The last clause is the one that mattered in P06 and matters here for the same reason: a kill switch that is
    cleared by a deploy is a kill switch that is off exactly when someone needed it on. `reset_on_restart` is
    therefore explicit, defaults to False, and the row survives every boot.
    """
    out = []
    if scope not in KILL_SCOPES:
        out.append("%r is not a scope this switch understands" % scope)
    if engaged and len(str(reason or "").strip()) < MIN_REASON:
        out.append("engaging the kill switch needs a reason of at least %d characters" % MIN_REASON)
    if len(str(reason or "")) > MAX_REASON:
        out.append("the reason is longer than the %d characters the audit row keeps" % MAX_REASON)
    if engaged and not str(by or "").strip():
        out.append("an engaged switch needs a name on it")
    return out


def blocks(kill: KillState, kind: str) -> bool:
    """Does the switch stop this message? Scope-aware, because "stop the channel" and "stop everything" are
    different decisions an operator makes on different days."""
    if not kill.engaged:
        return False
    if kill.scope == "all":
        return True
    if kill.scope == "channel":
        return kind == "channel"
    if kill.scope == "personal":
        return kind == "personal"
    return True


# --------------------------------------------------------------------------------------- broadcast tooling
DEFAULT_STAGE = {"kinds": ("large_fill",), "categories": (), "limit": 10}


def staged(targets: list, *, stage: dict | None = None) -> tuple:
    """(chosen, held) for a broadcast run. The default stage is deliberately tiny and deliberately boring.

    `limit` is the number of *messages*, not the number of subscribers: a staged rollout is "we will send ten of
    these and read the replies", and the subscribers are counted separately. `held` is returned rather than dropped,
    because an operator needs to see what the stage filtered out — a stage that silently matches nothing looks
    exactly like a pipeline that is broken.
    """
    s = dict(DEFAULT_STAGE, **(stage or {}))
    kinds = tuple(s.get("kinds") or ())
    cats = tuple(str(c).lower() for c in (s.get("categories") or ()))
    limit = max(0, int(s.get("limit") or 0))
    chosen, held = [], []
    for t in targets or []:
        kind_ok = (not kinds) or str(t.get("kind") or "") in kinds
        cat_ok = (not cats) or str(t.get("category") or "").lower() in cats
        (chosen if (kind_ok and cat_ok) else held).append(t)
    return chosen[:limit], chosen[limit:] + held


def dry_run(plan_render) -> dict:
    """Render exactly what would go out, without sending it.

    The interface takes the *composition function* rather than the targets, so the bytes are the real ones: a dry
    run that renders a summary of a message is a dry run that has never caught a broken message.
    """
    out = []
    for item in plan_render:
        plan = item["plan"]
        text = plan.beats[-1].text if plan.beats else ""
        keys = (plan.beats[-1].keyboard or {}).get("inline_keyboard", []) if plan.beats else []
        out.append({"slug": item.get("slug", ""), "chars": len(text), "html_escaped": "&" not in text or "&amp;" in text,
                    "buttons": [b.get("text", "") for row in keys for b in row],
                    "has_deep_link": any(str(b.get("url", "")).startswith("https://t.me/") for row in keys for b in row),
                    "text": text})
    return {"count": len(out), "items": out,
            "note": "nothing was sent; this is the exact text that would go out, plus what a sender would check"}


def recovery_steps() -> tuple:
    """What we do when Telegram restricts the bot, bans the channel, or someone impersonates support.

    Ordered by what costs the least to do first, and every step names the hedge that makes the loss survivable. The
    kit asks for this to be *written down*; it is data so the gate can assert that each of the three failure modes
    has a path, a mirror and an owner.
    """
    return (
        {"failure": "bot_restricted", "detect": "sendMessage answers 403 with 'bot was blocked' or the API starts "
                                                "refusing new chats; the drain's failure count jumps",
         "first_move": "pause broadcasts (kill scope=channel) so we do not lose reach we still have",
         "next": "appeal via @BotSupport with the update ids and timestamps of the last sends",
         "hedge": "every channel post carries the Mini App deep link, so the audience we already reached can come "
                  "back without the channel",
         "owner": "ops-oncall", "mirror": "discord"},
        {"failure": "channel_banned", "detect": "channel_post starts failing with 'chat not found' or the channel "
                                                "disappears from getChat",
         "first_move": "flip the broadcast target to the Discord mirror and the web's /alerts page",
         "next": "export the last 90 days of broadcasts (telegram_broadcasts) as the record of what we said",
         "hedge": "the web is the primary hedge: alert rules live in the product, and Telegram delivery is a "
                  "channel of it rather than the place alerts exist",
         "owner": "ops-oncall", "mirror": "discord+web"},
        {"failure": "impersonation", "detect": "/verify <handle> on a report, or a user forwards a DM that claims "
                                             "to be support",
         "first_move": "publish the account in /support's block list and pin the 'we never DM first' line",
         "next": "report the account to Telegram with the screenshots the user sent",
         "hedge": "/verify exists so a user can check before they trust, and every money-adjacent card repeats "
                  "that staff never ask for a seed phrase or a deposit",
         "owner": "support-lead", "mirror": "web"},
    )


# --------------------------------------------------------------------------------------- username strategy
def username_decision() -> dict:
    """One bot or several? The kit asks for the argument, so here it is as data with the answer.

    **One bot, with the Mini App: `@polygm_bot`.** The reasons, in the order they decided it:

    * A bot's username is its identity to users and to Telegram's own search; per-function bots (`@polygm_alerts_bot`,
      `@polygm_trade_bot`) fragment that identity across three review processes, three support surfaces and three
      ban risks, and a ban of the trade bot leaves the alert bot pointing at a product nobody can use.
    * The Mini App can hold several entry points (`/trade`, `/markets`, `/wallet` short names) inside one bot, which
      is where the "per function" split actually belongs — it is a *screen* distinction, not a bot distinction.
    * Rate limits are per bot, and one bot with a queue we control is easier to keep inside them than three bots each
      answering their own bursts. The channel is the exception in shape, not in identity: it is a chat, served by the
      same bot.
    * The one case for a second bot is the public channel's **anti-spam relay** (a separate bot that only forwards
      our channel posts into groups). That is a deliberate stretch goal, not the first move, and this function says
      so rather than leaving it implied.
    """
    return {"bot": "@polygm_bot", "mini_app_short_names": ("trade", "markets", "wallet"),
            "second_bot": {"when": "only if we start relaying channel posts into third-party groups",
                           "why": "a relay can be muted, rate-limited or banned without touching the primary bot"},
            "rejected": {"per_function_bots": "three identities, three ban risks, three support surfaces; the "
                                              "function split belongs in the Mini App's screens",
                         "personal_bot_per_user": "impossible (rate limits and review) and unnecessary "
                                                  "(a private chat with one bot is already private)"}}


def ops_findings(state: dict) -> list:
    """The operator page's own checks, so a missing piece is a finding rather than a surprise."""
    out = []
    kill = state.get("kill") or KillState()
    out.extend(kill_findings(kill.engaged, scope=kill.scope, reason=kill.reason, by=kill.by))
    for row in recovery_steps():
        for key in ("failure", "detect", "first_move", "hedge", "owner", "mirror"):
            if not str(row.get(key) or "").strip():
                out.append("the recovery row for %s has no %s" % (row.get("failure"), key))
    depth = int(state.get("queue_depth") or 0)
    if depth > 500:
        out.append("the outbox holds %d messages: a queue that deep means delivery is behind, not busy" % depth)
    if state.get("bot_configured") is False and state.get("env") == "production":
        out.append("this pod has no bot token and claims to be production")
    return out
