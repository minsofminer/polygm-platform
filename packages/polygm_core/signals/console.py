"""D9's API-side logic: what an alert would do right now, and what held it back.

The transport is the ingest worker's job (`fanout.py` plans claims; a worker owns the network). Everything a
*user* reads before an alert exists — quiet hours, digest mode, the per-rule cooldown, which channel a plan is
allowed to use, and the honest status of a test fire — is decided here, as pure functions of a rule, the user's
settings and a clock.

Four decisions worth stating, because each could have gone the other way:

1. **The cooldown is the window budget, made explicit.** P04's `alert_rules` has `fires_per_window` and
   `window_ms` and no separate cooldown column, and that is not a gap: three fires an hour *is* one every twenty
   minutes at best. `cooldown_state` derives it and shows the arithmetic ("3 per hour = one every 20 minutes"),
   so the number a user reads is checkable rather than a setting we invented.
2. **Quiet hours hold everything, urgent included — and the plan says so.** A digest is a batching decision we
   make; quiet hours is an instruction the user gave us about their own sleep. An "urgent" alert quietly
   breaching it would mean the setting is a suggestion, so the urgent severity is allowed to breach the *digest*
   only, and a held urgent alert is labelled as held.
3. **A test fire reports the plan and changes nothing about the rule.** It is recorded with its own signal so
   the rule's real window, cooldown and delivery history are untouched — a test that consumes the budget it is
   testing is a test that poisons the feature it validates (the same mistake P10's radar quota test made once).
4. **`digest_scheduled` and `dropped_rate_limited` are the API's own words for "held" and "refused",** because
   they are the statuses `alert_deliveries` already records. A held alert and a refused one are different facts
   and a user can act on the difference.
"""
from __future__ import annotations

CHANNELS = ("telegram", "email", "webhook")

#: Which plan each channel needs. Telegram is the default and free; email is for anyone paying; webhook is Pro,
#: which is the brief's own split ("Telegram (default), email, webhook (Pro)").
CHANNEL_PLAN = {"telegram": "free", "email": "trader", "webhook": "pro"}
PLAN_RANK = {"free": 0, "trial": 1, "trader": 2, "pro": 3, "team": 4}

DAY_MS = 86_400_000
MINUTE_MS = 60_000

#: The severities an alert can carry, and what each one buys. `urgent` is the only one that skips a digest.
SEVERITIES = ("info", "notice", "urgent")


def channel_entitlement(channel: str, plan: str) -> tuple[bool, str]:
    """`(allowed, note)`. The note is written for the case where it is NOT allowed, because that is the case
    with a decision in it: "webhook needs Pro" tells a user what to do; a 403 that just says "forbidden" does
    not."""
    need = CHANNEL_PLAN.get(channel)
    if need is None:
        return False, "%r is not a channel we deliver to" % channel
    if PLAN_RANK.get(plan, 0) >= PLAN_RANK[need]:
        return True, ""
    return False, "%s needs the %s plan" % (channel, need)


def _local_minute(at_ms: int, tz_offset_min: int) -> int:
    """Minute of the user's local day, 0..1439. The offset comes from the client and is never guessed: a quiet
    window evaluated in the wrong timezone is a setting that does not work, silently."""
    return ((at_ms // MINUTE_MS) + int(tz_offset_min)) % 1440


def _clock(minute: int) -> str:
    return "%02d:%02d" % (minute // 60, minute % 60)


def quiet_hours_state(settings: dict, at_ms: int) -> dict:
    """Whether the user's quiet window is open right now, and when it ends.

    A window that wraps midnight (`22:00 -> 07:00`) is the normal case, not the edge case, which is why the
    comparison is written as two ranges rather than as `start <= now < end`.
    """
    start = int(settings.get("quiet_start_min", -1))
    end = int(settings.get("quiet_end_min", -1))
    tz = int(settings.get("tz_offset_min", 0))
    if start < 0 or end < 0 or start == end:
        return {"configured": False, "active": False, "untilMs": None, "note":
                "quiet hours are off: every alert is delivered when it fires"}
    now = _local_minute(at_ms, tz)
    active = (start <= now < end) if start < end else (now >= start or now < end)
    if not active:
        return {"configured": True, "active": False, "untilMs": None,
                "note": "quiet hours %s-%s (UTC%s): outside the window now"
                        % (_clock(start), _clock(end), _offset(tz))}
    # Minutes until the window ends, in local time, then converted back to an instant. `(end - now) % 1440` is
    # the wrap-safe form and is never zero here (a zero-length window was refused above).
    mins = (end - now) % 1440
    return {"configured": True, "active": True, "untilMs": at_ms + mins * MINUTE_MS,
            "note": "quiet hours until %s local (UTC%s): held, not dropped" % (_clock(end), _offset(tz))}


def _offset(tz_offset_min: int) -> str:
    sign = "+" if tz_offset_min >= 0 else "-"
    tz = abs(int(tz_offset_min))
    return "%s%02d:%02d" % (sign, tz // 60, tz % 60)


def digest_state(settings: dict, *, at_ms: int, severity: str) -> dict:
    """Whether this alert waits for a digest, and when the digest goes out.

    Off by default. Hourly defers to the next hour boundary *in the user's own offset* (a :30 offset has :30
    boundaries, which is the whole reason this is computed in local time), daily to `digest_at_min`.
    """
    mode = str(settings.get("digest_mode") or "off")
    tz = int(settings.get("tz_offset_min", 0))
    if mode == "off":
        return {"mode": "off", "deferred": False, "atMs": None, "note": "no digest: alerts go out as they fire"}
    if severity == "urgent":
        return {"mode": mode, "deferred": False, "atMs": None,
                "note": "urgent breaches the %s digest: a digest is our batching, not your instruction" % mode}
    if mode == "hourly":
        now = _local_minute(at_ms, tz)
        mins = 60 - (now % 60)
        return {"mode": mode, "deferred": True, "atMs": at_ms + mins * MINUTE_MS,
                "note": "digest mode: batched into the next hour"}
    at_min = int(settings.get("digest_at_min", 480))
    now = _local_minute(at_ms, tz)
    mins = (at_min - now) % 1440 or 1440
    return {"mode": mode, "deferred": True, "atMs": at_ms + mins * MINUTE_MS,
            "note": "digest mode: batched for the %s local send" % _clock(at_min)}


def cooldown_state(*, fires_per_window: int, window_ms: int, fired_ms: list[int], at_ms: int) -> dict:
    """The rule's own budget, as a user can check it: how many of the window's fires are spent, the effective
    cooldown, and the next moment this rule may fire again."""
    cap = max(1, int(fires_per_window or 1))
    window = max(MINUTE_MS, int(window_ms or 3_600_000))
    recent = sorted(int(t) for t in fired_ms if int(t) > at_ms - window)
    cooldown = window // cap
    next_allowed = None
    if len(recent) >= cap:
        # The window frees one slot when its OLDEST fire falls out of it.
        next_allowed = recent[0] + window
    return {
        "firesPerWindow": cap,
        "windowMs": window,
        "cooldownMs": cooldown,
        "firesInWindow": len(recent),
        "remaining": max(0, cap - len(recent)),
        "nextAllowedMs": next_allowed,
        "rule": "%d per %s = one every %s at most" % (cap, _duration(window), _duration(cooldown)),
        "note": ("%d of %d fires used in the current window" % (len(recent), cap)
                 + ("" if next_allowed is None else "; the next slot frees at %d" % next_allowed)),
    }


def _duration(ms: int) -> str:
    if ms % 3_600_000 == 0:
        return "%dh" % (ms // 3_600_000)
    if ms % MINUTE_MS == 0:
        return "%dm" % (ms // MINUTE_MS)
    return "%ds" % (ms // 1000)


def delivery_plan(*, rule: dict, settings: dict, at_ms: int, severity: str, fired_ms: list[int],
                  channels: list[str], plan: str, is_test: bool = False) -> list[dict]:
    """One decision per channel, each with the sentence that explains it.

    Order of the checks is the order a user would ask them: can this plan use the channel at all, has the rule
    spent its window, is the user asleep, is the user batching. The first "no" wins and says which one it was,
    so a held alert is never a mystery ("it did not arrive" is the support ticket this function exists to
    prevent).
    """
    cool = cooldown_state(fires_per_window=int(rule.get("fires_per_window") or 1),
                          window_ms=int(rule.get("window_ms") or 3_600_000), fired_ms=fired_ms, at_ms=at_ms)
    quiet = quiet_hours_state(settings, at_ms)
    digest = digest_state(settings, at_ms=at_ms, severity=severity)
    out = []
    for channel in channels:
        allowed, reason = channel_entitlement(channel, plan)
        if not allowed:
            out.append({"channel": channel, "decision": "refused", "status": "failed", "reason": reason,
                        "atMs": None, "sentence": "not delivered on %s: %s" % (channel, reason)})
            continue
        if not rule.get("enabled", True):
            out.append({"channel": channel, "decision": "refused", "status": "failed",
                        "reason": "the rule is paused", "atMs": None,
                        "sentence": "not delivered: the rule is paused"})
            continue
        if cool["remaining"] <= 0 and not is_test:
            out.append({"channel": channel, "decision": "rate_limited", "status": "dropped_rate_limited",
                        "reason": "window spent: %s" % cool["rule"], "atMs": cool["nextAllowedMs"],
                        "sentence": "held by the rule's own cap: %s (next slot %s)"
                                    % (cool["note"], cool["nextAllowedMs"])})
            continue
        if quiet["active"]:
            out.append({"channel": channel, "decision": "quiet_hours", "status": "digest_scheduled",
                        "reason": quiet["note"], "atMs": quiet["untilMs"],
                        "sentence": "held for quiet hours; it goes out at %s" % quiet["untilMs"]})
            continue
        if digest["deferred"]:
            out.append({"channel": channel, "decision": "digest", "status": "digest_scheduled",
                        "reason": digest["note"], "atMs": digest["atMs"],
                        "sentence": "batched: %s" % digest["note"]})
            continue
        out.append({"channel": channel, "decision": "send_now", "status": "queued", "reason": "",
                    "atMs": at_ms, "sentence": "queued for %s" % channel})
    return out


def plan_summary(plan_rows: list[dict]) -> dict:
    """The one-line answer for a test fire: what happens to this alert, on each channel, right now."""
    sends = [r for r in plan_rows if r["decision"] == "send_now"]
    held = [r for r in plan_rows if r["status"] == "digest_scheduled"]
    refused = [r for r in plan_rows if r["status"] in ("failed", "dropped_rate_limited")]
    return {
        "sends": len(sends), "held": len(held), "refused": len(refused),
        "sentence": ("would be delivered now on %s" % ", ".join(r["channel"] for r in sends)) if sends else
                    ("held: " + "; ".join(r["sentence"] for r in held)) if held else
                    ("not delivered: " + "; ".join(r["sentence"] for r in refused)) if refused else
                    "no channel to deliver on",
    }


def alert_summary(*, rule: dict, settings: dict, at_ms: int, fires_in_window: list[int],
                  last_delivery: dict | None = None, plan: str = "free") -> dict:
    """One alert rule as the list shows it: the target, the budget, the channel, and the two sentences that make
    the state checkable — the rule of the cooldown, and what quiet hours/digest would do to it right now."""
    params = rule.get("params") if isinstance(rule.get("params"), dict) else {}
    channel = str(params.get("channel") or settings.get("default_channel") or "telegram")
    severity = str(params.get("severity") or rule.get("severity") or "notice")
    allowed, why = channel_entitlement(channel, plan)
    cool = cooldown_state(fires_per_window=int(rule.get("fires_per_window") or 1),
                          window_ms=int(rule.get("window_ms") or 3_600_000), fired_ms=fires_in_window, at_ms=at_ms)
    quiet = quiet_hours_state(settings, at_ms)
    digest = digest_state(settings, at_ms=at_ms, severity=severity)
    would = delivery_plan(rule=rule, settings=settings, at_ms=at_ms, severity=severity,
                          fired_ms=fires_in_window, channels=[channel] if allowed else [], plan=plan)[0] if allowed \
        else {"channel": channel, "decision": "refused", "status": "failed", "reason": why, "atMs": None,
              "sentence": "not delivered on %s: %s" % (channel, why)}
    return {
        "ruleId": str(rule.get("id") or ""),
        "kind": str(rule.get("kind") or ""),
        # The user's own params, as they were saved. The inline editor reopens on these: a form that forgot the
        # price level it was saved with would silently rewrite the rule to its default the next time somebody
        # changed the channel.
        "params": params,
        "target": {"marketId": rule.get("market_id"), "eventId": rule.get("event_id")},
        "enabled": bool(rule.get("enabled")),
        "severity": severity,
        "channel": channel,
        "channelAllowed": allowed,
        "channelNote": why,
        "firesPerWindow": cool["firesPerWindow"],
        "windowMs": cool["windowMs"],
        "cooldownMs": cool["cooldownMs"],
        "cooldownRule": cool["rule"],
        "cooldownNote": cool["note"],
        "firesInWindow": cool["firesInWindow"],
        "remainingInWindow": cool["remaining"],
        "nextAllowedMs": cool["nextAllowedMs"],
        "quietHours": quiet,
        "digest": digest,
        "wouldDoNow": would,
        "lastDelivery": last_delivery,
        "createdMs": int(rule.get("created_ms") or 0),
    }


def validate_alert_payload(payload: dict, *, plan: str) -> tuple[dict, list[str]]:
    """The inline editor's payload -> the rule's fields, plus every problem. Channel entitlement is checked here
    as well as in the plan, so a free user cannot SAVE a webhook rule that could never deliver."""
    errs: list[str] = []
    kind = str(payload.get("kind") or "")
    if kind not in ("price_level", "spread_widen", "whale_fill", "resolve_lead", "illiquid_top", "new_market",
                    "manual"):
        errs.append("kind must be one of price_level, spread_widen, whale_fill, resolve_lead, illiquid_top, "
                    "new_market, manual")
    market_id = str(payload.get("marketId") or "").strip()
    event_id = str(payload.get("eventId") or "").strip()
    if not market_id and not event_id:
        errs.append("an alert needs a market or an event to watch: a rule with no target cannot fire")
    fires = payload.get("firesPerWindow")
    fires = 3 if fires is None else fires
    if not isinstance(fires, int) or isinstance(fires, bool) or not (1 <= fires <= 24):
        errs.append("firesPerWindow must be an integer between 1 and 24")
        fires = 3
    window = payload.get("windowMs")
    window = 3_600_000 if window is None else window
    if not isinstance(window, int) or isinstance(window, bool) or window < 60_000:
        errs.append("windowMs must be an integer of at least 60000 (a cap per minute is not a cap)")
        window = 3_600_000
    severity = str(payload.get("severity") or "notice")
    if severity not in SEVERITIES:
        errs.append("severity must be one of %s" % ", ".join(SEVERITIES))
    channel = str(payload.get("channel") or "telegram")
    allowed, why = channel_entitlement(channel, plan)
    if not allowed:
        errs.append(why)
    params = payload.get("params")
    if params is not None and not isinstance(params, dict):
        errs.append("params must be an object")
    params = params or {}
    # The engine's params for this kind, and the reason each is unusable when it is. Checked at SAVE time: a rule
    # whose condition the engine cannot evaluate is a rule that looks armed and never fires, and the moment to say
    # so is while the user still has the form open.
    engine, param_errs = engine_params(kind, params)
    errs.extend(param_errs)
    return ({"kind": kind, "market_id": market_id or None, "event_id": None if market_id else event_id,
             "fires_per_window": fires, "window_ms": window, "severity": severity, "channel": channel,
             "params": params, "engine_kind": engine_kind(kind), "engine_params": engine,
             "enabled": bool(payload.get("enabled", True)), "rule_id": str(payload.get("ruleId") or "")}, errs)


#: The wire name each alert kind's params arrive under, the engine's own name for the same number, and the
#: defaults. The two vocabularies differ on purpose (the screen says `whale_fill`, the engine says `large_fill`),
#: so the translation is a function with a name rather than a dict built at the call site.
KIND_PARAMS: dict[str, tuple[tuple[str, str], ...]] = {
    "price_level": (("priceMicro", "price_micro"), ("op", "op")),
    "spread_widen": (("spreadBp", "spread_bp"),),
    "whale_fill": (("absUsdMicro", "abs_usd_micro"),),
    "resolve_lead": (("hours", "hours"),),
    "illiquid_top": (("minDepthUsdMicro", "min_depth_usd_micro"),),
    "new_market": (),
    "manual": (),
}

#: What a kind needs when the client does not say, and what it may not exceed. `None` means "required": a price
#: level alert with no level is not a rule, it is a form the user has not finished, and defaulting it to 0.50
#: would fire on a number nobody chose.
PARAM_DEFAULTS = {"priceMicro": None, "op": ">=", "spreadBp": None, "absUsdMicro": 10_000 * 10 ** 6,
                  "hours": 6, "minDepthUsdMicro": 500 * 10 ** 6}
PARAM_LIMITS = {"priceMicro": (1, 999_999), "spreadBp": (1, 10_000), "absUsdMicro": (1, 10 ** 15),
                "hours": (1, 720), "minDepthUsdMicro": (1, 10 ** 15)}
PARAM_LABELS = {"priceMicro": "a price level between 0.000001 and 0.999999",
                "spreadBp": "a spread in basis points (1 = 0.01%)",
                "absUsdMicro": "a notional in micro-USDC", "hours": "a number of hours",
                "minDepthUsdMicro": "a book depth in micro-USDC"}


def engine_kind(kind: str) -> str | None:
    """The engine rule kind an alert kind is evaluated by, or `None` for `manual`.

    `None` is not "unknown": a manual alert fires only when its owner triggers it, and the list says so rather
    than pretending some loop is watching it.
    """
    from .engine import ALERT_KIND_MAP
    return ALERT_KIND_MAP.get(str(kind))


def engine_params(kind: str, params: dict | None) -> tuple[dict, list[str]]:
    """The engine's params for this alert kind, plus every reason they are unusable.

    A param the engine does not get is a rule that never fires, and a rule that never fires is invisible from the
    screen — so a missing or out-of-range one is an error the caller has to handle, never a value we quietly
    invent. The one exception is a param the product has a defensible default for, and those defaults are in
    `PARAM_DEFAULTS` where a reader can check them.
    """
    p = params if isinstance(params, dict) else {}
    errs: list[str] = []
    out: dict = {}
    for wire, engine_name in KIND_PARAMS.get(str(kind), ()):
        if wire == "op":
            op = str(p.get("op") or PARAM_DEFAULTS["op"])
            if op not in (">=", "<="):
                errs.append("op must be '>=' or '<=', got %r" % (op,))
            out[engine_name] = op
            continue
        raw = p.get(wire, PARAM_DEFAULTS.get(wire))
        if raw is None:
            errs.append("%s needs %s" % (kind, PARAM_LABELS.get(wire, wire)))
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or int(raw) != raw:
            errs.append("%s must be a whole number, got %r" % (wire, raw))
            continue
        lo, hi = PARAM_LIMITS.get(wire, (1, 10 ** 15))
        if not lo <= int(raw) <= hi:
            errs.append("%s must be between %d and %d, got %r" % (wire, lo, hi, raw))
            continue
        out[engine_name] = int(raw)
    return out, errs


def signal_rule_row(*, rule_id: str, owner: str, alert: dict, condition_ids: list[str] | None = None) -> dict | None:
    """The `signal_rules` row that makes a user's alert actually fire, or `None` for a manual one.

    The ingest plane evaluates `signal_rules` and nothing else. A user's rule that lives only in `alert_rules` is
    a rule NO loop reads: the screen lists it, the cooldown arithmetic works, and it can never fire. So the upsert
    writes both — the user's own words in `alert_rules`, the engine's input in `signal_rules`, under the SAME id,
    so a fire lands in `signals` under the id the rule list reads its window budget from.

    `market_filter` is keyed by the engine's own event field (`market`, which is the venue's condition id) and may
    hold a list: an alert on an event covers every market in it, and a filter that matched only the first would
    quietly drop the rest.
    """
    engine = engine_kind(alert.get("kind"))
    if engine is None:
        return None
    conds = tuple(str(c) for c in (condition_ids or []) if str(c))
    return {"kind": engine, "owner": owner,
            "params": dict(alert.get("engine_params") or {}),
            # The engine's cooldown IS the window budget, derived the same way the screen's sentence derives it:
            # fires per window, expressed as one fire per (window / fires).
            "cooldown_s": max(60, int(alert.get("window_ms") or 3_600_000) // 1000
                              // max(1, int(alert.get("fires_per_window") or 3))),
            "severity": str(alert.get("severity") or "notice"),
            "channels": [str(alert.get("channel") or "telegram")],
            "enabled": bool(alert.get("enabled", True)),
            "market_filter": ({"market": conds} if conds else {})}


def settings_view(row: dict | None) -> dict:
    """The settings block, with the defaults materialised so a client never has to know them."""
    r = row or {}
    tz = int(r.get("tz_offset_min") or 0)
    return {
        "quietStartMin": int(r.get("quiet_start_min", -1)),
        "quietEndMin": int(r.get("quiet_end_min", -1)),
        "tzOffsetMin": tz,
        "digestMode": str(r.get("digest_mode") or "off"),
        "digestAtMin": int(r.get("digest_at_min") or 480),
        "defaultChannel": str(r.get("default_channel") or "telegram"),
        "channels": [{"channel": c, "plan": CHANNEL_PLAN[c], "isDefault": c == str(r.get("default_channel")
                                                                                  or "telegram")}
                     for c in CHANNELS],
        "note": ("quiet hours hold everything, urgent included — a digest is our batching, quiet hours are your "
                 "instruction; turn them off if you want to be woken"),
    }


__all__ = ["CHANNELS", "CHANNEL_PLAN", "PLAN_RANK", "SEVERITIES", "channel_entitlement", "quiet_hours_state",
           "digest_state", "cooldown_state", "delivery_plan", "plan_summary", "alert_summary",
           "validate_alert_payload", "settings_view"]
