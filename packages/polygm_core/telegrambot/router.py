"""Routing: one update in, one decision out.

`route()` is a pure function over (update, session, linked state) that returns a `Decision` — the words, the buttons,
the action the service layer must take, and the session to persist. Everything that touches the outside world is
*not* here: no database, no Bot API, no clock except the `now_ms` it is handed. That is what makes the properties
this phase is judged on testable without a bot token:

    * a duplicate `update_id` returns `replay=True` and no action;
    * `/stop` cancels without asking, from any state, in one hop;
    * a tap on a stale card (the session moved on) is answered with a line rather than acting on the old context;
    * an order can only be reached by a tap on a confirm button whose session is live, unexpired, and owned by the
      same chat.

The command table lives in `menu.py`; the flows live here. Three flows are worth reading closely because they are
where chat UIs usually break:

* **`/market`** — paste a link or a slug, get a card with BUY YES / BUY NO and size chips. The card is the product.
* **Confirm** — the tap carries an `action_id` minted when the card was drawn, and the order that comes out of it is
  keyed by that id, so Telegram's retry of the tap is the *same* order rather than a second one.
* **`/stop`** — cancels, then reports what it cancelled, with a Resume button. No "are you sure": a panic command
  that asks a question is a panic command that fails.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import menu, nl, render
from .sessions import Session, advance, start, step_findings
from .updates import Update, classify, split_command

STALE_TAP = ("That card is out of date — the flow moved on. Here is the fresh one.", "stale")

#: Actions the service layer must carry out. `none` is the common case and it is explicit rather than implied: a
#: decision with no action is a decision that only sends a message, which is what a read-only command is.
ACTIONS = ("none", "place_order", "cancel_all", "cancel_order", "export_key", "open_miniapp", "link_account")


@dataclass
class Action:
    kind: str = "none"
    payload: dict = field(default_factory=dict)
    #: The idempotency key the action must be carried out under. Non-empty for every money action, and equal to the
    #: `action_id` in the button that produced it — so a double tap and a Telegram retry collapse into one.
    key: str = ""
    needs_confirmation: bool = True


@dataclass
class Decision:
    plan: render.Plan
    action: Action = field(default_factory=Action)
    session: Session | None = None
    replay: bool = False
    note: str = ""
    #: What the metrics table records: the command or callback action, and whether it succeeded.
    metric: str = ""
    haptic: str = ""


def _linked_or_link(*, chat_id: str, at_ms: int, what: str) -> Decision:
    """The one refusal that has to be friendly: money needs an account, and the way to get one is one tap.

    Returning "not authorised" here would be true and useless. The button is the whole message, and the session is
    untouched, so linking and coming back does not lose what they were doing.
    """
    text = ("To %s I need your PolyGM account attached to this chat — one tap, and Telegram sends me a signed "
            "confirmation, not your password.\n\n<i>Your PolyGM password is never asked for in Telegram. Nobody "
            "from support will ever DM you first, ask for a seed phrase, or ask you to send a deposit.</i>" % what)
    kb = menu.Keyboard().add(menu.Row().add(menu.Button("🔗 Link account", action="menu", part="link"),
                                            menu.Button("What is PolyGM?", action="menu", part="about")))
    return Decision(plan=render.Plan(beats=[render.Beat(text=text, keyboard=kb.to_markup())]),
                    action=Action(kind="link_account"), metric="link_prompt")


def _card_plan(text: str, keyboard=None) -> render.Plan:
    return render.Plan(beats=[render.Beat(text=text, keyboard=keyboard.to_markup() if keyboard else None)])


def route(update: Update, *, at_ms: int, bot_username: str = "", session: Session | None = None,
          linked: bool = False, account_id: str = "", market_index: tuple = (), fetch=None,
          seen: bool = False) -> Decision:
    """Decide what this update does. `seen=True` means the update_id was already handled: replay, do nothing.

    The replay branch is first and it is *not* a shortcut for the tests: Telegram retries any update it did not get
    a 2xx for, and the update most likely to arrive twice is the one where the user pressed Confirm. Answering a
    replay with the same words (and no action) is what keeps a retried tap from being a second order.
    """
    if seen:
        return Decision(plan=_card_plan("That one was already handled — I have not done it twice."),
                        replay=True, metric="replay", note="duplicate update_id %d" % update.update_id)

    kind = update.kind
    if kind == "callback":
        return _on_callback(update, at_ms=at_ms, session=session, linked=linked, fetch=fetch)
    if kind == "command":
        return _on_command(update, at_ms=at_ms, bot_username=bot_username, session=session, linked=linked,
                           account_id=account_id, market_index=market_index, fetch=fetch)
    if kind == "message":
        return _on_message(update, at_ms=at_ms, session=session, linked=linked, market_index=market_index,
                           fetch=fetch)
    if kind == "channel_post":
        return Decision(plan=_card_plan(""), note="channel posts are not handled here", metric="ignore")
    return Decision(plan=_card_plan("I did not know what to do with that."), metric="unknown")


# --------------------------------------------------------------------------------------- commands
def _on_command(u: Update, *, at_ms: int, bot_username: str, session, linked: bool, account_id: str,
                market_index: tuple, fetch) -> Decision:
    name, args = u.command, u.args
    if name in ("start", "help"):
        return _on_start(u, at_ms=at_ms, session=session, linked=linked, args=args, help_=name == "help")
    if name == "stop":
        # The panic command: no confirmation, no session, from anywhere.
        rows = _cancel_targets(fetch, account_id) if (fetch and account_id) else {}
        n_orders, n_copies = int(rows.get("orders", 0)), int(rows.get("copies", 0))
        kb = menu.stop_keyboard()
        text = ("🛑 <b>Stopped.</b>\nCancelled %d open order%s and paused %d auto-trade rule%s. Your money is where "
                "it was — stopping cancels what is pending, it does not sell what you hold."
                % (n_orders, "" if n_orders == 1 else "s", n_copies, "" if n_copies == 1 else "s"))
        if not linked:
            return _linked_or_link(chat_id=u.chat_id, at_ms=at_ms, what="stop everything")
        return Decision(plan=render.Plan(beats=[render.Beat(text=text, keyboard=kb.to_markup())],
                                         haptics=("haptic_reject",), priority=20),
                        action=Action(kind="cancel_all", payload={"reason": "user /stop", "chat_id": u.chat_id,
                                                                  "update_id": u.update_id},
                                      key="stop:%d" % u.update_id, needs_confirmation=False),
                        session=None, metric="stop")
    if name == "market":
        if not args:
            return Decision(plan=_card_plan("Give me a market: <code>/market will-the-fed-cut</code> — or paste any "
                                            "Polymarket link and I will do this automatically."),
                            metric="market", note="no argument")
        # Public on purpose, and the menu's auth column says so: the card is a price and two buttons, and the
        # moment a stranger has to link an account before *seeing* a market is the moment the acquisition engine
        # stops working. The link requirement appears where the money does — on the size tap and the confirm.
        return _market_card(args, at_ms=at_ms, chat_id=u.chat_id, session=session, market_index=market_index,
                            fetch=fetch, why="command")
    if name == "price":
        if not args:
            return Decision(plan=_card_plan("Which market? <code>/price &lt;slug or link&gt;</code>"), metric="price")
        return _price(args, market_index=market_index, fetch=fetch)
    if name == "search":
        if not args:
            return Decision(plan=_card_plan("What are you looking for? "
                                            "<code>/search fed rate cut</code>"), metric="search")
        return _search(args, market_index=market_index)
    if name == "verify":
        handle = args.strip().lstrip("@").lower()
        if not handle:
            return Decision(plan=_card_plan("Who should I verify? <code>/verify alice</code> — I will check whether "
                                            "that Telegram account belongs to a PolyGM operator."), metric="verify")
        return _verify(handle, fetch=fetch)
    if name == "positions":
        return _positions(linked=linked, at_ms=at_ms, chat_id=u.chat_id, fetch=fetch, account_id=account_id)
    if name == "balance":
        return _balance(linked=linked, at_ms=at_ms, chat_id=u.chat_id, fetch=fetch, account_id=account_id)
    if name == "pnl":
        return _pnl(linked=linked, at_ms=at_ms, chat_id=u.chat_id, fetch=fetch, account_id=account_id)
    if name == "orders":
        return _orders(linked=linked, at_ms=at_ms, chat_id=u.chat_id, fetch=fetch, account_id=account_id)
    if name in ("wallet", "deposit"):
        return _wallet(linked=linked, at_ms=at_ms, chat_id=u.chat_id, fetch=fetch, account_id=account_id,
                       which=name)
    if name == "withdraw":
        if not linked:
            return _linked_or_link(chat_id=u.chat_id, at_ms=at_ms, what="start a withdrawal")
        if session and session.live and session.step == "withdraw_amount":
            # A second `/withdraw` while already in the flow: continue it rather than start a second one, because
            # two parallel flows in one chat is how a user confirms the wrong address.
            return Decision(plan=_card_plan("You are already withdrawing — send the amount, or /cancel to stop."),
                            session=session, metric="withdraw")
        return _withdraw_start(chat_id=u.chat_id, at_ms=at_ms, fetch=fetch, account_id=account_id, args=args)
    if name in ("alerts", "follow", "copy", "settings", "top", "support"):
        return _lookup(name, args=args, fetch=fetch, account_id=account_id, linked=linked, at_ms=at_ms, u=u)
    if name == "cancel":
        return Decision(plan=_card_plan("Cancelled. Nothing was sent."), session=None, metric="cancel")
    # An unknown command that the table does not list: say so, with the menu, rather than silence.
    return Decision(plan=_card_plan("I do not know <code>/%s</code>. Here is everything I do:" % render.esc(name),
                                    menu.main_menu()), metric="unknown_command")


def _on_start(u: Update, *, at_ms: int, session, linked: bool, args: str, help_: bool = False) -> Decision:
    """`/start` is the product's front door, and D7 measures it: the clock to a first trade starts here."""
    payload = (args or u.start_param or "").strip()
    about = menu.command("start")
    text = "\n".join([
        "📈 <b>PolyGM</b> — prediction markets, in this chat.",
        "Paste any Polymarket link and you get a live price and a BUY YES / BUY NO card. Or tap something below.",
        "",
        "<i>Prediction markets can lose money. Prices are someone else's opinion until they settle. "
        "Nothing here is advice.</i>",
        "<i>PolyGM staff never message you first, never ask for a seed phrase, and never ask you to send a deposit. "
        "Check anyone who does with /verify.</i>",
    ])
    if help_:
        text += "\n\n<b>Everything I do:</b> " + ", ".join("/%s" % c.name for c in menu.commands())
    note = ""
    if payload:
        # A deep link is a lookup key, never code: the payload decides *which* card opens, nothing else.
        if menu.SLUG_RE.match(payload) and "-" in payload:
            note = "start payload looks like a market slug: %s" % payload
            d = _market_card(payload, at_ms=at_ms, chat_id=u.chat_id, session=session, market_index=(),
                             fetch=None, why="deep link")
            d.plan.beats[0] = render.Beat(text=text + "\n\n" + d.plan.beats[0].text,
                                          keyboard=d.plan.beats[0].keyboard)
            d.note = note
            return d
        if menu.HANDLE_RE.match(payload.lstrip("@")):
            note = "start payload looks like a trader handle"
            text += "\n\nShowing <b>@%s</b> — /follow to track them." % render.esc(payload.lstrip("@"))
        else:
            note = "start payload matched nothing; fell back to the menu"
    plan = _card_plan(text, menu.main_menu())
    plan.priority = menu.command("start").touches_money and 30 or 50
    return Decision(plan=plan, session=session, metric="start", note=note)


def _market_card(query: str, *, at_ms: int, chat_id: str, session, market_index: tuple, fetch, why: str) -> Decision:
    """`/market` — the highest-value command in the phase, and the one the whole acquisition story points at."""
    resolved = _resolve(query, market_index=market_index, fetch=fetch)
    if resolved.get("missing"):
        return Decision(plan=_card_plan("I could not find “%s”. Paste the full link, or try /search."
                                        % render.esc(query[:60])), metric="market", note="unresolved")
    if resolved.get("ambiguous"):
        kb = menu.Keyboard().add(*[menu.Row().add(menu.Button(m["question"][:28], action="market", part="open",
                                                              value=m["slug"])) for m in resolved["options"][:4]])
        return Decision(plan=_card_plan("Which one?\n" + "\n".join(
            "• %s" % render.esc(m["question"]) for m in resolved["options"][:4]), kb), metric="market",
            note="ambiguity offered")
    m = resolved["market"]
    card = render.Plan(beats=[
        render.Beat(text="⏳ Looking up %s…" % render.esc(m["question"][:40]), chat_action="typing", what="skeleton"),
        render.Beat(text=_market_text(m), keyboard=menu.order_card_keyboard(action_id="", slug=m["slug"]).to_markup(),
                    what="answer")])
    sess = start(chat_id=chat_id, step="choose_side", at_ms=at_ms, payload={"slug": m["slug"]})
    return Decision(plan=card, session=sess, metric="market", note=why)


def _market_text(m: dict) -> str:
    """The card's words. Price, age, spread and the two sentences the product requires on every price surface."""
    bid, ask = m.get("bid", "—"), m.get("ask", "—")
    mark = m.get("mark", "—")
    age = m.get("age", "as of just now")
    lines = ["<b>%s</b>" % render.esc(m.get("question", "")),
             "YES %s · NO %s · spread %s" % (render.esc(mark), render.esc(m.get("no_mark", "—")),
                                             render.esc(m.get("spread", "—"))),
             "<i>%s</i>" % render.esc(age)]
    if m.get("closes"):
        lines.append("<i>closes %s</i>" % render.esc(str(m["closes"])))
    if m.get("sample_note"):
        lines.append("<i>%s</i>" % render.esc(str(m["sample_note"])))
    lines.append("")
    lines.append("<i>Prices move; the price on your confirm card is the one that rules. "
                 "Prediction markets can lose money — never trade more than you can afford to lose.</i>")
    return "\n".join(lines)


def _price(query: str, *, market_index: tuple, fetch) -> Decision:
    r = _resolve(query, market_index=market_index, fetch=fetch)
    if r.get("missing") or r.get("ambiguous"):
        return Decision(plan=_card_plan("I could not pin that down — try /search %s" % render.esc(query[:60])),
                        metric="price")
    m = r["market"]
    kb = menu.Keyboard().add(menu.Row().add(menu.Button("Open trade card", action="market", part="open",
                                                        value=m["slug"])))
    return Decision(plan=_card_plan("<b>%s</b>\nYES %s · NO %s\n<i>%s</i>\n\n<i>Ends %s. Odds are a market, not a "
                                    "forecast.</i>" % (render.esc(m["question"]), render.esc(m.get("mark", "—")),
                                                       render.esc(m.get("no_mark", "—")), render.esc(m.get("age", "")),
                                                       render.esc(str(m.get("closes", "—")))),
                                    kb), metric="price")


def _search(query: str, *, market_index: tuple) -> Decision:
    hits = nl.resolve_market(query, market_index)[:5] if market_index else []
    if not hits:
        return Decision(plan=_card_plan("Nothing matched “%s”. Try fewer words, or /top for what is moving."
                                        % render.esc(query[:60])), metric="search")
    kb = menu.Keyboard().add(*[menu.Row().add(menu.Button(h["question"][:30], action="market", part="open",
                                                          value=h["slug"])) for h in hits])
    text = "<b>%d match%s for “%s”</b>" % (len(hits), "" if len(hits) == 1 else "es", render.esc(query[:40]))
    return Decision(plan=_card_plan(text, kb), metric="search")


def _verify(handle: str, *, fetch) -> Decision:
    """The support-impersonation defence, and the reason D6 insists it exists: a user who can check, checks."""
    who = (fetch("verify_handle", {"handle": handle}) if fetch else None) or {}
    if who.get("operator"):
        text = ("✅ <b>@%s is a PolyGM operator</b> (%s).\nThey will still never ask for your seed phrase, your "
                "password, or a deposit — nobody legitimate ever does. If they do, /report it." % (render.esc(handle),
                                                                                                   render.esc(str(who.get('role', 'support')))))
    else:
        text = ("⚠️ <b>@%s is not a PolyGM account.</b>\nAnyone messaging you first, offering help, or asking for a "
                "seed phrase or a deposit is impersonating us. Block them and /report it. We never DM first."
                % render.esc(handle))
    kb = menu.Keyboard().add(menu.Row().add(menu.Button("🚩 Report this account", action="menu", part="report"),
                                            menu.Button("What we never ask", action="menu", part="about")))
    return Decision(plan=_card_plan(text, kb), metric="verify")


# --------------------------------------------------------------------------------------- read-only flows
def _ask_fetch(fetch, name: str, payload: dict, *, at_ms: int, chat_id: str, account_id: str, skeleton: str,
               empty: str, keyboard=None, metric: str) -> Decision:
    """A read-only answer in two beats: a skeleton line, then the content, in the same bubble.

    The skeleton is not decoration: a `/positions` that takes 900 ms to answer is fine, and a `/positions` that
    shows nothing for 900 ms looks broken. The second beat *edits* the first, so the chat does not fill up with
    "loading…" lines — the phase's motion rule, and the reason `Plan` has beats rather than messages.
    """
    if fetch is None:
        return Decision(plan=render.two_beat(skeleton=skeleton, answer=empty, keyboard=keyboard, priority=50,
                                             haptics=()), metric=metric)
    data = fetch(name, dict(payload, account_id=account_id, at_ms=at_ms)) or {}
    if not data.get("text"):
        return Decision(plan=_card_plan(empty, keyboard), metric=metric)
    return Decision(plan=render.two_beat(skeleton=skeleton, answer=data["text"],
                                         keyboard=data.get("keyboard", keyboard), priority=50), metric=metric)


def _positions(*, linked: bool, at_ms: int, chat_id: str, fetch, account_id: str) -> Decision:
    if not linked:
        return _linked_or_link(chat_id=chat_id, at_ms=at_ms, what="show your positions")
    kb = menu.Keyboard().add(menu.Row().add(menu.Button("PnL", action="menu", part="pnl"),
                                            menu.Button("Orders", action="menu", part="orders"),
                                            menu.Button("Refresh", action="menu", part="positions")))
    return _ask_fetch(fetch, "positions", {}, at_ms=at_ms, chat_id=chat_id, account_id=account_id,
                      skeleton="⏳ Counting your positions…",
                      empty="You have no open positions yet. Find a market with /search and I will show you a card.",
                      keyboard=kb, metric="positions")


def _balance(*, linked: bool, at_ms: int, chat_id: str, fetch, account_id: str) -> Decision:
    if not linked:
        return _linked_or_link(chat_id=chat_id, at_ms=at_ms, what="show your balance")
    kb = menu.Keyboard().add(menu.Row().add(menu.Button("Deposit", action="menu", part="deposit"),
                                            menu.Button("Withdraw", action="menu", part="withdraw"),
                                            menu.Button("History", action="menu", part="history")))
    return _ask_fetch(fetch, "balance", {}, at_ms=at_ms, chat_id=chat_id, account_id=account_id,
                      skeleton="⏳ Reading your ledger…", empty="No wallet yet — /wallet will create one.",
                      keyboard=kb, metric="balance")


def _pnl(*, linked: bool, at_ms: int, chat_id: str, fetch, account_id: str) -> Decision:
    if not linked:
        return _linked_or_link(chat_id=chat_id, at_ms=at_ms, what="calculate your PnL")
    return _ask_fetch(fetch, "pnl", {}, at_ms=at_ms, chat_id=chat_id, account_id=account_id,
                      skeleton="⏳ Working out your PnL…",
                      empty="No closed trades yet, so there is no PnL to show. That is not a good thing or a bad "
                            "one — it is a blank.", metric="pnl")


def _orders(*, linked: bool, at_ms: int, chat_id: str, fetch, account_id: str) -> Decision:
    if not linked:
        return _linked_or_link(chat_id=chat_id, at_ms=at_ms, what="list your orders")
    return _ask_fetch(fetch, "orders", {}, at_ms=at_ms, chat_id=chat_id, account_id=account_id,
                      skeleton="⏳ Checking the book…",
                      empty="Nothing open. Orders you place show up here until they fill or you cancel them.",
                      metric="orders")


def _wallet(*, linked: bool, at_ms: int, chat_id: str, fetch, account_id: str, which: str) -> Decision:
    if not linked:
        return _linked_or_link(chat_id=chat_id, at_ms=at_ms, what="open your wallet")
    kb = menu.Keyboard().add(menu.Row().add(menu.Button("Deposit", action="menu", part="deposit"),
                                            menu.Button("Withdraw", action="menu", part="withdraw")),
                             menu.Row().add(menu.Button("Export key", action="menu", part="export"),
                                            menu.Button("History", action="menu", part="history")))
    d = _ask_fetch(fetch, "wallet" if which == "wallet" else "deposit", {}, at_ms=at_ms, chat_id=chat_id,
                   account_id=account_id, skeleton="⏳ Reading your wallet…",
                   empty="No wallet yet. Tap Create and I will make one now — it takes a second.", keyboard=kb,
                   metric=which)
    return d


def _withdraw_start(*, chat_id: str, at_ms: int, fetch, account_id: str, args: str) -> Decision:
    """Withdrawal: the password step is not optional, and the flow lives in one bubble."""
    body = (args or "").strip()
    sess = start(chat_id=chat_id, step="withdraw_amount", at_ms=at_ms, payload={})
    text = ("<b>Withdraw</b>\nSend the amount in USDC (for example <code>250</code>), or /cancel.\n\n"
            "<i>Withdrawals need your password, and only go to an address on your allowlist. A new address waits "
            "out a 24-hour security delay before it can be used.</i>")
    if body and menu.AMOUNT_RE.match(body.split()[0]):
        sess = advance(sess, step="withdraw_address", at_ms=at_ms, payload={"amount": body.split()[0]})
        text = ("<b>Withdraw %s USDC</b>\nNow the destination — an address from your allowlist, or a saved name."
                % render.esc(body.split()[0]))
    return Decision(plan=_card_plan(text), session=sess, metric="withdraw")


# --------------------------------------------------------------------------------------- the generic lookups
def _lookup(name: str, *, args: str, fetch, account_id: str, linked: bool, at_ms: int, u: Update) -> Decision:
    """alerts / follow / copy / settings / top / support — each a card from the service, with buttons.

    These are grouped because they share a shape: a read that returns words plus a keyboard, with no state to carry
    between messages. `top` is public (it is the acquisition surface); the rest need an account, except `support`,
    which must always answer, because a user with a problem is exactly the user who cannot be told to link first.
    """
    if name == "support":
        text = ("<b>Support</b>\nTell me what happened and I will open a ticket with the order id.\n\n"
                "<b>Nobody from PolyGM will ever:</b>\n"
                "• message you first,\n• ask for your seed phrase or private key,\n• ask you to send a deposit to "
                "\"unlock\" a withdrawal,\n• ask for your password or a 2FA code.\n\n"
                "Anyone who does is impersonating us — use /verify &lt;username&gt; to check, and /report to flag "
                "them.")
        kb = menu.Keyboard().add(menu.Row().add(menu.Button("Deposit not showing", action="menu", part="ticket_dep"),
                                                menu.Button("Order stuck", action="menu", part="ticket_order")),
                                 menu.Row().add(menu.Button("Withdrawal blocked", action="menu", part="ticket_wd"),
                                                menu.Button("Report a message", action="menu", part="report")))
        return Decision(plan=_card_plan(text, kb), metric="support")
    if name == "top":
        return _ask_fetch(fetch, "top", {"args": args}, at_ms=at_ms, chat_id=u.chat_id, account_id=account_id,
                          skeleton="⏳ Ranking…",
                          empty="The board is empty right now. Trades appear here as they fill.",
                          metric="top")
    if name in ("alerts", "follow", "copy", "settings") and not linked:
        return _linked_or_link(chat_id=u.chat_id, at_ms=at_ms, what="open %s" % name)
    kb = menu.Keyboard().add(menu.Row().add(menu.Button("🔔 Alerts", action="menu", part="alerts"),
                                            menu.Button("⭐ Watched", action="menu", part="follow"),
                                            menu.Button("⚙️ Settings", action="menu", part="settings")))
    return _ask_fetch(fetch, name, {"args": args}, at_ms=at_ms, chat_id=u.chat_id, account_id=account_id,
                      skeleton="⏳ One moment…", empty="Nothing set up yet — tap a button below to start.",
                      keyboard=kb, metric=name)


# --------------------------------------------------------------------------------------- callbacks
def _on_callback(u: Update, *, at_ms: int, session, linked: bool, fetch) -> Decision:
    """A tap. The `action_id` is the idempotency key, and a stale tap is answered rather than obeyed."""
    action, part, value = menu.parse_callback(u.callback_data or "")
    if not action:
        return Decision(plan=_card_plan("That button is from an older version of me — try /start."),
                        metric="bad_callback")
    if action == "menu":
        return _menu_action(part, value=value, u=u, at_ms=at_ms, linked=linked, session=session, fetch=fetch)
    if action == "market":
        if part in ("yes", "no"):
            if not linked:
                return _linked_or_link(chat_id=u.chat_id, at_ms=at_ms, what="place an order")
            sid = session if (session and session.live and session.payload.get("slug") == value) else None
            if sid is None:
                # A tap on a card whose session expired: restart the flow from the market, keeping the side.
                d = _market_card(value, at_ms=at_ms, chat_id=u.chat_id, session=None, market_index=(), fetch=fetch,
                                 why="expired card")
                d.note = STALE_TAP[1]
                d.plan.beats[-1] = render.Beat(
                    text=STALE_TAP[0] + "\n\n" + d.plan.beats[-1].text,
                    keyboard=d.plan.beats[-1].keyboard, what="answer")
                return d
            ns = advance(sid, step="choose_size", at_ms=at_ms, payload={"slug": value, "side": part})
            return Decision(plan=_size_prompt(ns), session=ns, metric="side")
        if part == "open":
            return _market_card(value, at_ms=at_ms, chat_id=u.chat_id, session=session, market_index=(), fetch=fetch,
                                why="button")
        return Decision(plan=_card_plan("That market link is out of date — /search for it."), metric="market")
    if action == "size":
        if not linked:
            return _linked_or_link(chat_id=u.chat_id, at_ms=at_ms, what="place an order")
        if session is None or not session.live or session.step not in ("choose_side", "choose_size", "custom_size"):
            return Decision(plan=_card_plan(STALE_TAP[0]), metric="stale", note="size tap with no live flow")
        if session.payload.get("slug") not in ("", None) and session.payload.get("slug") != value:
            # The chip belongs to a card for a *different* market than the flow is about. Without this check the
            # order would be placed on the session's market using a chip tapped on someone else's — a real bug the
            # first run of `TestFlows.test_a_stale_card_is_refused_rather_than_obeyed` found, because two cards
            # open in one chat is the normal case rather than the exotic one.
            return Decision(plan=_card_plan(STALE_TAP[0]), metric="stale",
                            note="size tap for %s while the flow is about %s" % (value,
                                                                                 session.payload.get("slug")))
        if part == "c" or value == "custom":
            ns = advance(session, step="custom_size", at_ms=at_ms, payload={"slug": session.payload.get("slug", ""),
                                                                            "side": session.payload.get("side", "")})
            return Decision(plan=_card_plan("How much? Send an amount in USDC, like <code>75</code>."), session=ns,
                            metric="size_custom")
        ns = advance(session, step="confirm_order", at_ms=at_ms,
                     payload={"slug": session.payload.get("slug", ""), "side": session.payload.get("side", ""),
                              "amount": part, "action_id": _action_id(u, session)})
        return Decision(plan=_confirm_prompt(ns, u=u), session=ns, metric="size")
    if action == "confirm":
        if not linked:
            return _linked_or_link(chat_id=u.chat_id, at_ms=at_ms, what="confirm that order")
        if session is None or not session.live or session.step != "confirm_order":
            return Decision(plan=_card_plan(STALE_TAP[0]), metric="stale", note="confirm with no live flow")
        problems = step_findings(session.step, session.payload)
        if problems:
            return Decision(plan=_card_plan("I lost the details of that order — start again with /market."),
                            session=None, metric="stale", note="; ".join(problems))
        p = session.payload
        return Decision(plan=render.Plan(beats=[render.Beat(text="⏳ Sending %s %s…"
                                                           % (render.esc(p.get("amount", "")),
                                                              render.esc(p.get("side", "").upper())),
                                                           chat_action="typing", what="skeleton")],
                                         priority=30),
                        action=Action(kind="place_order",
                                      payload={"slug": p.get("slug", ""), "side": p.get("side", ""),
                                               "amount": p.get("amount", ""), "chat_id": u.chat_id,
                                               "message_id": u.message_id},
                                      key=str(p.get("action_id") or ""), needs_confirmation=True),
                        session=None, metric="confirm")
    if action == "cancel":
        if session is not None and value and str(session.payload.get("action_id")) != str(value):
            return Decision(plan=_card_plan(STALE_TAP[0]), metric="stale", note="cancel for a different card")
        return Decision(plan=_card_plan("✖️ Cancelled — nothing was sent, no order exists.", menu.main_menu()),
                        session=None, metric="cancel")
    if action == "stop":
        if part == "resume":
            return Decision(plan=_card_plan("Resumed. Auto-trade is on again — /stop stops it again."),
                            action=Action(kind="cancel_all", payload={"resume": True}, key="stop:resume:%d"
                                          % u.update_id), metric="stop_resume")
        return Decision(plan=_card_plan("Here is what /stop cancelled:"),
                        action=Action(kind="cancel_all", payload={"report": True}, key="stop:report:%d"
                                      % u.update_id), metric="stop_report")
    if action == "positions":
        return _positions(linked=linked, at_ms=at_ms, chat_id=u.chat_id, fetch=fetch, account_id="")
    if action == "orders":
        return _orders(linked=linked, at_ms=at_ms, chat_id=u.chat_id, fetch=fetch, account_id="")
    if action == "wallet":
        return _wallet(linked=linked, at_ms=at_ms, chat_id=u.chat_id, fetch=fetch, account_id="", which="wallet")
    return Decision(plan=_card_plan("I do not know that button yet."), metric="callback")


def _menu_action(part: str, *, value: str, u: Update, at_ms: int, linked: bool, session, fetch) -> Decision:
    """The main menu's buttons, mapped onto the same commands — one keyboard, one implementation."""
    if part in ("market", "wallet", "positions", "orders", "pnl", "balance", "alerts", "follow", "copy", "top",
                "settings", "support", "help", "deposit"):
        cmd = Update(update_id=u.update_id, kind="command", chat_id=u.chat_id, chat_type=u.chat_type,
                     user_id=u.user_id, command="wallet" if part == "deposit" else part, args=value,
                     message_id=u.message_id)
        return _on_command(cmd, at_ms=at_ms, bot_username="", session=session, linked=linked, account_id="",
                           market_index=(), fetch=fetch)
    if part == "export":
        return _export_start(u, at_ms=at_ms)
    if part == "export_ack":
        if session is None or not session.live or session.step != "export_disclaimer":
            return Decision(plan=_card_plan("That warning timed out and I did not act on it. /start if you still "
                                            "want to export."), metric="stale", note="export ack with no live flow")
        ns = advance(session, step="export_confirm", at_ms=at_ms, payload={"acknowledged": True})
        text = ("<b>Last step</b>\nPolyGM will show your private key once, in this chat, for two minutes — then it "
                "is gone from the message and from our side.\n\nAnybody who has it has your money, and we cannot "
                "undo that. Tap only if you are alone and the screen is yours.")
        kb = menu.Keyboard().add(menu.Row().add(menu.Button("Show my key", action="menu", part="export_now",
                                                           style="danger", confirm=True),
                                               menu.Button("Cancel", action="cancel", part="x", value="export")))
        return Decision(plan=_card_plan(text, kb), session=ns, metric="export_ack")
    if part == "export_now":
        if session is None or not session.live or session.step != "export_confirm":
            return Decision(plan=_card_plan("That timed out — nothing was shown."), metric="stale")
        return Decision(plan=render.Plan(beats=[render.Beat(text="⏳ Preparing your key…", chat_action="typing",
                                                            what="skeleton")]),
                        action=Action(kind="export_key", payload={"chat_id": u.chat_id},
                                      key="export:%d:%d" % (u.update_id, session.version), needs_confirmation=True),
                        session=None, metric="export_now")
    if part == "withdraw":
        return _withdraw_start(chat_id=u.chat_id, at_ms=at_ms, fetch=fetch, account_id="", args="")
    if part == "about":
        about = ("<b>PolyGM</b> trades Polymarket prediction markets from this chat.\n\n"
                 "<b>What staff never do:</b> message you first, ask for a seed phrase or private key, ask for your "
                 "password or a 2FA code, or ask you to send crypto to unlock anything. Support only ever replies "
                 "to you, and /verify &lt;username&gt; checks any account that claims to be us.\n\n"
                 "<i>Markets can lose money; the disclaimer travels with every price we show.</i>")
        return Decision(plan=_card_plan(about, menu.main_menu()), metric="about")
    if part in ("report", "ticket_dep", "ticket_order", "ticket_wd"):
        kinds = {"report": "impersonation report", "ticket_dep": "deposit not showing",
                 "ticket_order": "order stuck", "ticket_wd": "withdrawal blocked"}
        text = ("<b>%s</b>\nReply with one line describing it and, if you have one, the order id. A human reads "
                "these; the bot does not close tickets.\n\n<i>Support never asks for a seed phrase. If someone "
                "does, that message is the report.</i>" % kinds.get(part, "ticket").capitalize())
        return Decision(plan=_card_plan(text), metric="ticket_" + part)
    if part == "link":
        return _linked_or_link(chat_id=u.chat_id, at_ms=at_ms, what="do that")
    if part == "history":
        return _ask_fetch(fetch, "history", {}, at_ms=at_ms, chat_id=u.chat_id, account_id="",
                          skeleton="⏳ Reading your history…", empty="No activity yet.", metric="history")
    return Decision(plan=_card_plan("Unknown menu button."), metric="menu_unknown")


def _export_start(u: Update, *, at_ms: int) -> Decision:
    """Key export: the flow with the longest warning and the shortest TTL, in that order."""
    text = ("⚠️ <b>Exporting your key hands over your money.</b>\n\n"
            "Anyone with this key can move everything in the wallet, on every chain, without your password and "
            "without asking us. PolyGM support will never ask for it — a request for it is theft, whoever makes it.\n\n"
            "If you are exporting to use the wallet elsewhere, tap Continue. If somebody told you to, do not.")
    ns = start(chat_id=u.chat_id, step="export_disclaimer", at_ms=at_ms, payload={})
    kb = menu.Keyboard().add(menu.Row().add(menu.Button("I understand", action="menu", part="export_ack"),
                                            menu.Button("Cancel", action="cancel", part="x", value="export")))
    return Decision(plan=_card_plan(text, kb), session=ns, metric="export")


def _size_prompt(session: Session) -> render.Plan:
    kb = menu.order_card_keyboard(action_id="", slug=session.payload.get("slug", ""))
    side = str(session.payload.get("side", "")).upper()
    return render.Plan(beats=[
        render.Beat(text="⏳ Pricing %s…" % render.esc(session.payload.get("slug", "")), chat_action="typing",
                    what="skeleton"),
        render.Beat(text="<b>%s</b> — %s. How much?\n\n<i>The size buttons carry only the amount; the price comes "
                        "from the book when you confirm.</i>" % (render.esc(session.payload.get("slug", "")),
                                                                 render.esc(side)),
                    keyboard=kb.to_markup(), what="answer")])


def _confirm_prompt(session: Session, *, u: Update) -> render.Plan:
    """The confirmation card: market, side, size, estimated fee, max loss — and the two buttons.

    Max loss is printed even when it equals the size, and it is the *worst case* the size can lose: a user who sees
    "max loss $50" understands a prediction market, and one who sees only "size $50" does not.
    """
    p = session.payload
    amount = str(p.get("amount", ""))
    fee = _fee_estimate(amount)
    text = "\n".join([
        "<b>Confirm order</b>",
        "Market: %s" % render.esc(str(p.get("slug", ""))),
        "Side: <b>%s</b>" % render.esc(str(p.get("side", "")).upper()),
        "Size: %s USDC" % render.esc(amount),
        "Estimated fee: %s" % render.esc(fee),
        "Max loss: %s USDC (the whole position — this is a prediction market, not a hedge)" % render.esc(amount),
        "",
        "<i>The price at the moment you confirm is the price you get, or the order does not go. This card is good "
        "for 15 minutes.</i>",
    ])
    kb = menu.confirm_keyboard(action_id=str(p.get("action_id", "")), side=str(p.get("side", "")))
    return render.Plan(beats=[render.Beat(text=text, keyboard=kb.to_markup(), what="answer")], priority=30)


def _fee_estimate(amount: str) -> str:
    """Fee estimate, integer arithmetic on the decimal string: 1% of the notional, floored to a cent."""
    try:
        whole, _, frac = str(amount).partition(".")
        cents = int(whole) * 100 + int((frac + "00")[:2] or 0)
    except ValueError:
        return "—"
    fee_cents = cents // 100
    return "%d.%02d" % (fee_cents // 100, fee_cents % 100)


def _cancel_targets(fetch, account_id: str) -> dict:
    """How many pending orders and armed rules `/stop` is about to stop, for the sentence that reports it.

    Asked *before* the cancellation so the reply can name the number, and answered by the service because the
    counts live where the rows live. A missing answer is `{}`, and the sentence then says zero of each — which is
    the honest reading of "we could not count, and nothing is stopping you from pressing it anyway".
    """
    if fetch is None or not account_id:
        return {}
    try:
        return fetch("stop_targets", {"account_id": account_id}) or {}
    except TypeError:
        return {}


def _action_id(u: Update, session: Session) -> str:
    """The idempotency key for a trade, minted once and carried by the button.

    It is derived from the chat, the flow's version and the update that produced the card, so a re-drawn card gets a
    *new* id — which is right, because a re-drawn card is a new decision — while a double tap on one card, or
    Telegram's retry of it, resolves to the same id and therefore to the same order.
    """
    slug = str(session.payload.get("slug", ""))[:24]
    side = str(session.payload.get("side", ""))[:4]
    return "act-%d-%s-%s-%d" % (u.update_id, slug, side, session.version)


# --------------------------------------------------------------------------------------- plain messages
def _on_message(u: Update, *, at_ms: int, session, linked: bool, market_index: tuple, fetch) -> Decision:
    """A message that is not a command: a flow step, an amount, a sentence, or a pasted link."""
    text = (u.text or "").strip()
    if not text:
        return Decision(plan=_card_plan(""), metric="empty", note="message with no text")
    if render.ADDRESS_RE.search(text) and not (session and session.live):
        # An address sent with no flow running is either a mistake or a phishing attempt's suggestion; either way
        # the answer is the same, and it never echoes the address back.
        return Decision(plan=_card_plan("I did not expect an address. If someone asked you to send crypto, "
                                        "stop — that is not us. /support opens a ticket."), metric="address_unsolicited")
    if session and session.live:
        d = _continue(u, session=session, at_ms=at_ms, linked=linked)
        if d is not None:
            return d
    if session and session.live and session.is_expired(at_ms):
        return Decision(plan=_card_plan("That flow timed out, so I did not act on it. Start again whenever you "
                                        "like."), session=None, metric="expired")
    # A pasted market link is the highest-intent message a user can send: treat it as `/market`.
    if "polymarket.com" in text.lower() or "/event/" in text.lower():
        return _market_card(text, at_ms=at_ms, chat_id=u.chat_id, session=session, market_index=market_index,
                            fetch=fetch, why="pasted link")
    if text.lower() in ("hi", "hello", "hey", "yo", "gm") or len(text.split()) <= 2 and text.isalpha():
        return _on_start(u, at_ms=at_ms, session=session, linked=linked, args="")
    parsed = nl.parse(text, markets=market_index)
    findings = menu.natural_language_findings(parsed)
    if findings:
        return Decision(plan=_card_plan("I misread that — try a link, or /help."), metric="nl_bad",
                        note="; ".join(findings))
    if parsed.get("action") == "order" and parsed.get("market_slug") and parsed.get("amount") and parsed.get("side"):
        return _nl_order_card(u, parsed=parsed, at_ms=at_ms, session=session)
    if parsed.get("action") == "order" and not linked:
        return _linked_or_link(chat_id=u.chat_id, at_ms=at_ms, what="place an order")
    if parsed.get("action") in ("positions", "balance", "orders", "pnl", "wallet"):
        return _lookup(parsed["action"], args="", fetch=fetch, account_id="", linked=linked, at_ms=at_ms, u=u)
    if parsed.get("question"):
        kb = None
        if parsed.get("options"):
            kb = menu.Keyboard().add(*[menu.Row().add(menu.Button(o["question"][:30], action="market", part="open",
                                                                 value=o["slug"])) for o in parsed["options"][:3]])
        return Decision(plan=_card_plan(parsed["question"], kb), metric="nl_question",
                        note=parsed.get("why", ""))
    return Decision(plan=_card_plan("I did not catch that. Paste a market link, or tap a button:",
                                    menu.main_menu()), metric="nl_fallback")


def _nl_order_card(u: Update, *, parsed: dict, at_ms: int, session) -> Decision:
    """A confident sentence becomes the *same* card the buttons produce — one order path, one confirm."""
    amount = str(parsed.get("amount", ""))
    slug = str(parsed.get("market_slug", ""))
    side = str(parsed.get("side", ""))
    action_id = "nl-%d-%s-%s" % (u.update_id, slug[:20], side[:4])
    ns = start(chat_id=u.chat_id, step="confirm_order", at_ms=at_ms,
               payload={"slug": slug, "side": side, "amount": amount, "action_id": action_id})
    return Decision(plan=_confirm_prompt(ns, u=u), session=ns, metric="nl_order",
                    note="parsed from a sentence at confidence %.2f" % parsed.get("confidence", 0))


def _continue(u: Update, *, session: Session, at_ms: int, linked: bool) -> Decision | None:
    """The live session's next step. Returns None when this message is not for the flow, so `/market` still works."""
    text = (u.text or "").strip()
    if session.is_expired(at_ms):
        return Decision(plan=_card_plan("That timed out — nothing was sent. Start again with /market."),
                        session=None, metric="expired")
    if session.step == "custom_size":
        if not menu.AMOUNT_RE.match(text):
            return Decision(plan=_card_plan("That is not an amount I can use. Send something like <code>75</code> "
                                            "(USDC), or /cancel."), session=session, metric="custom_size_bad")
        ns = advance(session, step="confirm_order", at_ms=at_ms,
                     payload={"slug": session.payload.get("slug", ""), "side": session.payload.get("side", ""),
                              "amount": text, "action_id": _action_id(u, session)})
        return Decision(plan=_confirm_prompt(ns, u=u), session=ns, metric="custom_size")
    if session.step == "withdraw_amount":
        if not menu.AMOUNT_RE.match(text.split()[0] if text.split() else ""):
            return Decision(plan=_card_plan("Send an amount in USDC, like <code>250</code>, or /cancel."),
                            session=session, metric="withdraw_amount_bad")
        ns = advance(session, step="withdraw_address", at_ms=at_ms, payload={"amount": text.split()[0]})
        return Decision(plan=_card_plan("<b>Withdraw %s USDC</b>\nNow the destination address, or a saved name "
                                        "from your allowlist." % render.esc(text.split()[0])), session=ns,
                        metric="withdraw_amount")
    if session.step == "withdraw_address":
        problems = step_findings(session.step, {"address_or_alias": text})
        if problems:
            return Decision(plan=_card_plan("That does not look like an address or a saved name. Type /cancel to "
                                            "stop."), session=session, metric="withdraw_address_bad")
        ns = advance(session, step="withdraw_amount", at_ms=at_ms,
                     payload={"amount": session.payload.get("amount", ""), "address_or_alias": text})
        return Decision(plan=_card_plan("<b>Almost done.</b>\n%s USDC to <code>%s</code>. Send your password to "
                                        "confirm — it is only used for this one request, never stored, and never "
                                        "sent to Telegram."
                                        % (render.esc(str(session.payload.get("amount", ""))),
                                           render.esc(text[:60]))),
                        session=ns, metric="withdraw_address")
    if session.step == "export_disclaimer":
        return Decision(plan=_card_plan("Tap <b>I understand</b> on the warning above, or /cancel."),
                        session=session, metric="export_wait")
    if session.step == "export_confirm":
        ns = advance(session, step="export_disclaimer", at_ms=at_ms, payload={"acknowledged": True})
        return Decision(plan=render.Plan(beats=[render.Beat(text="⏳ Preparing your key…", chat_action="typing",
                                                            what="skeleton")]),
                        action=Action(kind="export_key", payload={"chat_id": u.chat_id},
                                      key="export:%d" % u.update_id, needs_confirmation=True),
                        session=ns, metric="export_confirm")
    return None


# --------------------------------------------------------------------------------------- the index helper
def _resolve(query: str, *, market_index: tuple, fetch) -> dict:
    """Resolve a slug, a link or a phrase to one market, asking the service when the route has no index yet.

    Two sources on purpose: `/market <slug>` from a deep link arrives before any search has run, so the service gets
    a chance to look it up directly (`fetch("market", ...)`), while a phrase is matched against the index the caller
    already narrowed. A missing market is a result, never an exception: the answer to "no such market" is a sentence.
    """
    q = (query or "").strip()
    slug = ""
    if "/" in q and ("polymarket.com" in q or q.startswith("/")):
        tail = [p for p in q.split("/") if p][-1].split("?")[0]
        slug = tail if menu.SLUG_RE.match(tail) else ""
    elif menu.SLUG_RE.match(q.split()[0] if q else ""):
        slug = q.split()[0]
    if slug and fetch is not None:
        m = fetch("market", {"slug": slug})
        if m and m.get("market"):
            return {"market": m["market"]}
        if m and m.get("missing"):
            return {"missing": True}
    if slug and fetch is None:
        return {"market": {"slug": slug, "question": slug.replace("-", " ").capitalize(), "mark": "—", "no_mark": "—",
                           "age": "price unavailable without the service", "spread": "—"}}
    hits = nl.resolve_market(q, market_index) if market_index else []
    if not hits:
        return {"missing": True}
    if len(hits) > 1 and hits[1]["score"] >= hits[0]["score"] * 0.85:
        return {"ambiguous": True, "options": hits[:4]}
    m = next((x for x in market_index if x.get("slug") == hits[0]["slug"]), None) or {}
    return {"market": dict(m, slug=hits[0]["slug"], question=m.get("question", hits[0]["question"]))}
