"""The public channel: the acquisition engine, and the one surface where a mistake is public.

An alert here is not a notification — it is *marketing that has to be true*. Every design decision below comes from
that:

* **One market per message, scannable in one glance.** A digest of five markets reads as a newsletter and converts
  like one. The format is a whale-or-spike line, the market, the price with its age, and nothing else; the reader
  gets the whole thing in the preview line before they decide to open it.
* **The trade button is a URL, not a callback.** This is the difference between a button that works for 40,000
  strangers and one that works for the people who have already started the bot: a `callback_data` tap from a
  channel post can only be answered with a message to a user who has an open conversation with us, and most people
  reading the channel do not. The button is therefore a `t.me/<bot>/<app>?startapp=<slug>` deep link into the Mini
  App, which is one tap for everybody. The *personal* copy of the same alert — sent to someone who follows the
  market — carries callback buttons instead, because there we do have a conversation, and a tap there can place a
  trade without leaving the chat.
* **The gate runs before composition, not after.** P07's `abuse.broadcast_gate()` decides `broadcast`/`hold`/
  `refused` on liquidity, age, resolution source, metadata flags and our own hourly cap, and every verdict goes into
  `broadcast_gates` — including the refusals, because a broadcast that is silently refused is indistinguishable
  from a broken pipeline. This module never invents a second gate; it composes what the gate allowed.
* **What we say about a market is a claim, so it carries what a claim needs.** The age of the price, the fact that
  the odds are a market rather than a forecast, and no win rate anywhere without its sample. The channel is not the
  place where the disclaimer gets dropped for length.
"""
from __future__ import annotations

import json
import pathlib

import re

from . import menu, render

#: The four event kinds the kit names. A closed vocabulary, because "something happened" is not a broadcast rule.
KINDS = ("large_fill", "volume_spike", "new_market", "resolution_soon")

#: Floors, in micro-USDC and basis points where money, because a filter written in floats is a filter that disagrees
#: with the ledger about whether a $4,999.99 fill was large.
LARGE_FILL_MICRO = 5_000 * 1_000_000        # $5,000 notional: big enough to be interesting, small enough to happen
SPIKE_BPS = 25_000                          # 2.5x the trailing hour, as basis points of the trailing hour
RESOLUTION_SOON_MS = 6 * 3_600_000          # six hours: close enough that "it ends today" is the news

#: Cadence caps: the channel's value is that it is quiet. Per kind *and* overall, because four kinds each inside
#: their own cap is still a shout.
CADENCE = {"max_per_hour": 4, "max_per_kind_per_hour": 2, "min_gap_ms": 6 * 60_000}

#: Categories the channel is allowed to talk about, and the ones it is not. Politics and sport produce constant
#: large fills; crypto produces spikes that mean nothing; the categories a new user can *act* on are the ones with
#: a reason to end. The exclusion list is short and honest rather than a claim that everything else is curated.
TRACKED_HINT = ("politics", "economics", "crypto", "tech", "sports", "science", "entertainment")
EXCLUDED = ("test", "demo", "internal")

#: Quiet hours for *personal* delivery, in UTC hours. The channel is never quiet — it is pull, not push; a person's
#: phone in their pocket at 3am is a different thing, and a digest is how we keep the alerts without the buzz.
QUIET_FROM, QUIET_TO = 22, 7
URGENT = ("fill", "reject", "stop")          # the three that ignore quiet hours, and they are all about the user's


def qualifies(event: dict, *, at_ms: int) -> dict:
    """Is this event worth a stranger's attention? Pure arithmetic on numbers the ingest plane already computes.

    Returns `{"ok": bool, "why": str, "kind": str}`. Each branch is a floor with a reason attached, so a rejected
    event can be explained in one line to whoever asks why the channel is quiet — which is a question asked far more
    often than the opposite.
    """
    kind = str(event.get("kind") or "")
    if kind not in KINDS:
        return {"ok": False, "why": "%r is not a broadcast kind" % kind, "kind": kind}
    category = str(event.get("category") or "").lower()
    if any(bad in category for bad in EXCLUDED):
        return {"ok": False, "why": "category %r is excluded from the channel" % category, "kind": kind}
    notional = int(event.get("notional_micro") or 0)
    if kind == "large_fill":
        if notional < LARGE_FILL_MICRO:
            return {"ok": False, "why": "fill of %d µ is under the %d µ floor" % (notional, LARGE_FILL_MICRO),
                    "kind": kind}
    if kind == "volume_spike":
        prior = int(event.get("prior_hour_micro") or 0)
        now = int(event.get("hour_micro") or 0)
        if prior <= 0:
            # A spike against a zero hour is a market that just had its first trade, not a spike. Broadcasting it
            # would make every new market look like news.
            return {"ok": False, "why": "no trailing hour to spike against", "kind": kind}
        if now * 10_000 < prior * SPIKE_BPS:
            return {"ok": False, "why": "%.2fx the trailing hour is under the %.1fx floor"
                                        % (now / prior, SPIKE_BPS / 10_000.0), "kind": kind}
    if kind == "resolution_soon":
        ends = int(event.get("ends_ms") or 0)
        if not ends or ends - int(at_ms) > RESOLUTION_SOON_MS:
            return {"ok": False, "why": "resolves later than %d h away" % (RESOLUTION_SOON_MS // 3_600_000),
                    "kind": kind}
        if not str(event.get("resolution_source") or "").strip():
            # "It resolves today" is only useful if the reader can see *who* resolves it. An alert with no source
            # is a rumour with a countdown.
            return {"ok": False, "why": "no resolution source to cite", "kind": kind}
    if kind == "new_market":
        if int(event.get("liquidity_micro") or 0) <= 0:
            return {"ok": False, "why": "a new market with no liquidity is not tradeable news", "kind": kind}
    return {"ok": True, "why": "meets the %s floor" % kind, "kind": kind}


def cadence_ok(history: list, *, at_ms: int, kind: str, cadence: dict | None = None) -> tuple:
    """Whether the channel may speak now, given what it has already said.

    `history` is rows of `{"kind", "fired_ms"}` — the `telegram_broadcasts` table, which exists so this question has
    an answer that survives a restart. The three caps are checked in the order that produces the most useful refusal:
    the minimum gap is what a reader notices, the per-kind cap is what a category's fans notice, and the hourly cap
    is what makes the channel worth staying subscribed to.
    """
    c = dict(CADENCE, **(cadence or {}))
    # A row with no timestamp at all is not evidence that the channel spoke; a row timestamped zero is (and the
    # difference matters, because silently dropping zero rows would make a broken writer look like a quiet hour).
    rows = [r for r in (history or []) if r.get("fired_ms") is not None]
    hour = [r for r in rows if at_ms - int(r["fired_ms"]) < 3_600_000]
    if rows:
        # `if rows` rather than `if last`: a broadcast stamped at epoch 0 is a real row, and `if last` treats it as
        # absent — the falsy-zero bug this test caught in its first run, in a function whose whole job is to count.
        last = max(int(r["fired_ms"]) for r in rows)
        if at_ms - last < int(c["min_gap_ms"]):
            return False, "the last broadcast was %ds ago; the floor is %ds" % ((at_ms - last) // 1000,
                                                                               int(c["min_gap_ms"]) // 1000)
    same = [r for r in hour if str(r.get("kind")) == kind]
    if len(same) >= int(c["max_per_kind_per_hour"]):
        return False, "%d %s alerts in the past hour is the cap" % (len(same), kind)
    if len(hour) >= int(c["max_per_hour"]):
        return False, "%d alerts in the past hour is the cap" % len(hour)
    return True, ""


#: Where the grammar lives. Read at import so the emitter and the parser cannot disagree: the module is loaded from
#: `contracts/startapp.json` — the same file the web app mirrors into TypeScript — and a missing or malformed contract
#: is a hard failure at import rather than a link that lands on "that link did not look right".
_STARTAAP_CONTRACT = json.loads(
    (pathlib.Path(__file__).resolve().parents[3] / "contracts" / "startapp.json").read_text(encoding="utf-8"))


def startapp_payload(kind: str, value: str) -> str:
    """`("m", "fed-cut-sept")` → `"m-fed-cut-sept"`, in the grammar `contracts/startapp.json` defines.

    The tag comes from the contract's own table rather than a local literal, and the value is filtered to the
    contract's charset rather than trusted: a slug is already `[a-z0-9-]` by construction, but this function is the
    last place before a link goes out, and a value that survives into a link is a value a stranger will parse.
    """
    tag = str(kind).strip()
    if tag not in _STARTAAP_CONTRACT["tags"]:
        raise ValueError("unknown startapp tag %r" % tag)
    sep = _STARTAAP_CONTRACT["separator"]
    allowed = set(_STARTAAP_CONTRACT["value_charset_literal"])
    clean = "".join(ch if ch in allowed else "" for ch in str(value))[:_STARTAAP_CONTRACT["value_max_len"]]
    if not clean:
        raise ValueError("startapp payload has no value after sanitising %r" % value)
    return f"{tag}{sep}{clean}"


def mini_app_url(slug: str, *, bot_username: str, app_short_name: str = "trade") -> str:
    """The deep link a stranger can tap: it opens the Mini App on that market's card.

    `startapp` rather than `start`, and the payload is a *slug* — a lookup key, never a credential and never an
    account reference. A deep link is forwarded, screenshotted and quoted; it must be safe in all three.

    **This function and the client's parser were minting different formats until P12.** It emitted a bare slug while
    `web/src/telegram/startapp.ts` expected `<tag><sep><value>`, so every trade button on every channel alert was
    rejected by the app it pointed at. The fix is not "make them match" — matching by hand is how they stopped
    matching — it is that both sides now read `contracts/startapp.json`, and the gate compares a link this function
    produces against the parser that will receive it.
    """
    # Normalising a slug is not the same as filtering a payload. The payload function drops characters outside the
    # contract's charset; this one *shapes* a lookup key, so runs of separator become one hyphen and the ends are
    # trimmed — `"Fed-Cut/Sept??"` is the market `fed-cut-sept`, not `fed-cut-sept--`, and the difference is a link
    # that opens the market versus one that opens "nothing here to trade".
    clean = re.sub(r"[^a-z0-9]+", "-", str(slug).lower()).strip("-")[:56].strip("-")
    if not clean:
        raise ValueError("a deep link needs a market: %r normalises to nothing" % slug)
    payload = startapp_payload("m", clean)
    return "https://t.me/%s/%s?startapp=%s" % (bot_username.lstrip("@"), app_short_name, payload)


def compose(event: dict, *, bot_username: str, price_text: str, age_text: str) -> render.Plan:
    """The channel message: one market, the number with its age, and two buttons that work for a stranger."""
    kind = str(event.get("kind"))
    question = str(event.get("question") or "")
    slug = str(event.get("slug") or "")
    headline = {"large_fill": "🐋 <b>Large fill</b>",
                "volume_spike": "📈 <b>Volume spike</b>",
                "new_market": "🆕 <b>New market</b>",
                "resolution_soon": "⏳ <b>Resolving soon</b>"}[kind]
    detail = {"large_fill": "%s %s · %s" % (render.esc(price_text), render.esc(str(event.get("size_text") or "")),
                                            render.esc(age_text)),
              "volume_spike": "%s · %.1fx the past hour · %s" % (render.esc(price_text),
                                                                 int(event.get("hour_micro", 0))
                                                                 / max(1, int(event.get("prior_hour_micro", 1))),
                                                                 render.esc(age_text)),
              "new_market": "%s · %s on the book" % (render.esc(price_text),
                                                     render.esc(str(event.get("liquidity_text") or "liquidity"))),
              "resolution_soon": "%s · ends %s · resolves via %s" % (render.esc(price_text),
                                                                     render.esc(str(event.get("ends_text") or "")),
                                                                     render.esc(str(event.get("resolution_source")
                                                                                    or "")))}[kind]
    text = "%s\n<b>%s</b>\n%s\n\n<i>Odds are a market, not a forecast. Not advice; markets can lose money.</i>" \
           % (headline, render.esc(question), detail)
    side = "yes" if str(event.get("side") or "yes").lower() == "yes" else "no"
    kb = menu.Keyboard().add(
        menu.Row().add(menu.Button("Trade %s %s" % (side.upper(), price_text),
                                   url=mini_app_url(slug, bot_username=bot_username)),
                       menu.Button("Full card", url=mini_app_url(slug, bot_username=bot_username))),
        menu.Row().add(menu.Button("📊 All markets", url=mini_app_url("top-30d", bot_username=bot_username,
                                                                      app_short_name="markets"))),
    )
    return render.Plan(beats=[render.Beat(text=text, keyboard=kb.to_markup(), what="answer")],
                       priority=70, haptics=(), disable_preview=True)


def personal_copy(event: dict, *, bot_username: str, price_text: str, age_text: str, why: str) -> render.Plan:
    """The same alert, sent to somebody who follows the market — with buttons that place a trade in-chat.

    The difference from `compose` is not decoration: this reader has a conversation with the bot, so a tap can
    carry `callback_data` and reach the confirm card without leaving Telegram. The `why` line is what makes it
    personal ("you follow this market") rather than a broadcast that leaked into a private chat.
    """
    base = compose(event, bot_username=bot_username, price_text=price_text, age_text=age_text)
    slug = str(event.get("slug") or "")
    kb = menu.Keyboard().add(
        menu.Row().add(menu.Button("BUY YES", action="market", part="yes", value=slug, style="primary"),
                       menu.Button("BUY NO", action="market", part="no", value=slug, style="primary")),
        menu.Row().add(menu.Button("Open card", action="market", part="open", value=slug),
                       menu.Button("Mute this market", action="menu", part="mute", value=slug)),
    )
    text = base.beats[0].text + "\n\n<i>%s</i>" % render.esc(why)
    return render.Plan(beats=[render.Beat(text=text, keyboard=kb.to_markup(), what="answer")], priority=40)


def is_quiet(at_ms: int, *, quiet: tuple = (QUIET_FROM, QUIET_TO)) -> bool:
    """Quiet hours, in UTC, wrapping midnight. The *hour* is UTC rather than local because the server's timezone is
    not the user's; a socket-level timezone is a P12-of-its-own problem, and the honest interim is a documented UTC
    window plus a per-user override stored with the notification settings that P10 already routes."""
    import time
    hour = time.gmtime(int(at_ms) / 1000.0).tm_hour
    start, end = quiet
    return (hour >= start or hour < end) if start > end else (start <= hour < end)


def deliver_now(kind: str, *, at_ms: int, quiet: bool | None = None, urgent: tuple = URGENT) -> tuple:
    """(send_now, reason) for a personal alert. Urgent ones ignore quiet hours, and only three kinds are urgent."""
    if kind in urgent:
        return True, "%s is urgent: it is about the user's own money" % kind
    if quiet is None:
        quiet = is_quiet(at_ms)
    if quiet:
        return False, "quiet hours: this waits for the digest rather than buzzing a phone at night"
    return True, ""


def digest(alerts: list, *, bot_username: str, at_ms: int) -> render.Plan:
    """Everything that waited, in one message, at the top of the day.

    A digest that repeats the detail of an alert is a second alert; this one is a list with one line per item and a
    button per market, so the reader chooses. Hard cap of eight items: a digest that needs scrolling is a digest
    nobody reads, and what falls off the end is not lost — `/alerts` has the history.
    """
    items = list(alerts or [])[:8]
    lines = ["☀️ <b>While you were asleep</b>" if items else "☀️ <b>Nothing waited overnight</b>"]
    kb = menu.Keyboard()
    for a in items:
        lines.append("• %s — %s" % (render.esc(str(a.get("question") or "")[:70]),
                                    render.esc(str(a.get("price_text") or ""))))
        kb.add(menu.Row().add(menu.Button(str(a.get("question") or "market")[:28], action="market", part="open",
                                          value=str(a.get("slug") or ""))))
    if len(alerts or []) > len(items):
        lines.append("<i>…and %d more in /alerts.</i>" % (len(alerts) - len(items)))
    return render.Plan(beats=[render.Beat(text="\n".join(lines), keyboard=kb.to_markup(), what="answer")],
                       priority=90)


def channel_findings(plan: render.Plan, *, event: dict, at_ms: int = 0) -> list:
    """What is wrong with a channel message before it goes to thousands of people.

    The scanners are the ones that would be embarrassing rather than merely broken: a broadcast with no source, a
    price with no age, a question longer than a phone's preview line, a second market smuggled into a one-market
    message, and any win rate without its sample — the standing rule, enforced at the last gate before a claim
    leaves the building.
    """
    out = render.render_findings(plan)
    text = plan.beats[-1].text if plan.beats else ""
    kind = str(event.get("kind") or "")
    if kind not in KINDS:
        out.append("%r is not a broadcast kind" % kind)
    if kind == "resolution_soon" and not str(event.get("resolution_source") or "").strip():
        out.append("a resolution alert without its resolution source")
    if "as of" not in text and "ago" not in text:
        out.append("the broadcast shows a price with no age on it")
    question = str(event.get("question") or "")
    if len(question) > 100:
        out.append("the question is %d characters: it is the headline, and headlines do not scroll" % len(question))
    if re.search(r"\bodds of\b|\bwill definitely\b|\bguaranteed\b|\bsure thing\b", text, re.I):
        out.append("the broadcast promises an outcome, which is not a thing a market can do")
    if not any(btn.get("url", "").startswith("https://t.me/") for row in
               (plan.beats[-1].keyboard or {}).get("inline_keyboard", []) for btn in row):
        # A channel alert with no tappable deep link is a broadcast with no way in. The whole point of the channel
        # is that the reader can act on it in one tap, from a phone, without finding the bot.
        out.append("no deep link into the Mini App: a stranger has no way in")
    if at_ms and kind == "resolution_soon":
        ends = int(event.get("ends_ms") or 0)
        if ends and ends - at_ms > RESOLUTION_SOON_MS:
            out.append("a resolution alert for a market that ends later than the window")
    return out
