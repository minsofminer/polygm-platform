"""The command surface, as data — every command with its syntax, auth, response, error cases and its buttons.

The kit asks for a command set, and the reason to write it as a *table* rather than as a pile of handlers is that
the table is checkable: the gate walks it and fails on a command with no inline alternative, no documented refusal,
or a button whose `callback_data` payload Telegram will reject. A handler written inline is a command whose
documentation is whatever the code happens to do.

**Telegram users tap; they do not type.** So every command in this table carries a `buttons` entry: the same
capability reachable by pressing, in at most two taps from `/start`. The keyboards are laid out in this module
(rows of `Row`s of `Button`s) because the layout is a design decision that has to be reviewed as one — and because
the callback payload is the part that breaks silently.

Three hard constraints, each of which has bitten a real bot:

* **`callback_data` is limited to 64 bytes.** Longer and the Bot API refuses the *message*, so the menu that
  looked fine in a test fails in production. `callback()` here refuses to build one that long and says which part
  overflowed; `MAX_CALLBACK_BYTES` is asserted by the gate.
* **Nothing private goes in `callback_data`.** Telegram's servers (and anyone with the bot's DB) see it, and a
  button payload is echoed back to us in a later update. So it carries an *opaque action id*, a short slug and an
  integer amount — never an address, a session token or an API key. The gate greps the whole table for `0x…`.
* **A payload is a lookup key, never code.** `parse_callback()` splits on `:` and validates each part against its
  own shape; there is no `eval`, no JSON, and no "just put the market id in and we will sort it out".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Telegram refuses a message whose inline button data exceeds this. Bytes, not characters — and the regexes below
#: keep every part ASCII, so the two coincide by construction, which is the only way this stays true.
MAX_CALLBACK_BYTES = 64
CALLBACK_RE = re.compile(r"^[a-z][a-z0-9_]*:[a-zA-Z0-9_.:@\-]{0,40}:[a-zA-Z0-9_.:\-]{0,40}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")
HANDLE_RE = re.compile(r"^[a-z0-9_]{3,24}$")
AMOUNT_RE = re.compile(r"^\d{1,9}(?:\.\d{1,2})?$")

#: Who may run what. `public` is answered for a stranger (the menu, the marketing cards); `linked` means the
#: Telegram account has been bound to an account (P07's `telegram` identity); `owner` means the wallet is theirs;
#: `admin` is the operator bot. Nothing that moves money is ever `public`.
AUTH = ("public", "linked", "owner", "admin")


@dataclass(frozen=True)
class Button:
    """One inline button. `text` is what the user reads; `action` is what the callback carries."""

    text: str
    action: str = ""
    part: str = ""
    value: str = ""
    url: str = ""
    style: str = "default"          # default | primary | danger  (the client ignores unknown styles)
    confirm: bool = False

    def callback_data(self) -> str:
        """`action:part:value`, or the URL button's own (Telegram allows one or the other, never both)."""
        if self.url:
            return ""
        data = "%s:%s:%s" % (self.action, self.part, self.value)
        if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
            raise ValueError("callback_data is %d bytes, over the %d-byte limit: %s"
                             % (len(data.encode("utf-8")), MAX_CALLBACK_BYTES, data[:80]))
        return data


@dataclass
class Row:
    buttons: list = field(default_factory=list)

    def add(self, *buttons: Button) -> "Row":
        self.buttons.extend(buttons)
        return self


@dataclass
class Keyboard:
    """An inline keyboard: rows of buttons, in the order they appear."""

    rows: list = field(default_factory=list)

    def add(self, *rows: Row) -> "Keyboard":
        self.rows.extend(rows)
        return self

    def to_markup(self) -> dict:
        return {"inline_keyboard": [[b.callback_data() and
                                     {"text": b.text, "callback_data": b.callback_data()}
                                     or {"text": b.text, "url": b.url} for b in row.buttons]
                                    for row in self.rows if row.buttons]}

    def actions(self) -> list:
        return [b.action for row in self.rows for b in row.buttons if b.action]


@dataclass(frozen=True)
class Command:
    """One row of the surface: what it is, who may run it, what comes back, and how to tap it instead."""

    name: str
    summary: str
    syntax: str
    auth: str
    response: str
    errors: tuple = ()
    buttons: tuple = ()            # the rows of the inline keyboard this command answers with
    aliases: tuple = ()
    nl_hint: str = ""              # how the natural-language fallback reaches it, when it can
    touches_money: bool = False
    needs_confirmation: bool = False


# ----------------------------------------------------------------------------------------- the command table
def commands() -> tuple:
    """Every command, in the order they appear in the main menu. The order is the design.

    `/start` and `/wallet` first, because the first two questions a new user has are "what is this" and "where is
    the money"; `/market` third, because it is the single highest-value command (paste any Polymarket link, get a
    card with BUY/SELL); `/stop` deliberately in the *main* menu rather than at the bottom of `/settings`, since a
    panic command that needs two taps is not a panic command.
    """
    return (
        Command(
            name="start", summary="what this is, in two lines, plus the button menu",
            syntax="/start [payload]  ·  t.me/<bot>?start=<slug|handle|code>", auth="public",
            response="two lines of what this is + the risk disclosure + the menu keyboard; a `startapp` payload "
                     "is treated as a lookup key (market slug, trader handle or referral code) and never as code",
            errors=("a payload that matches nothing falls back to the menu with one line saying so",
                    "a payload over 200 characters is truncated at the boundary, not rejected"),
            buttons=("menu:market,wallet,positions", "menu:copy,alerts,follow", "menu:top,settings,help"),
            nl_hint="'hi', 'what is this', anything unrecognised",
        ),
        Command(
            name="wallet", summary="balance, deposit address, withdraw, key export",
            syntax="/wallet", auth="owner",
            response="balance card with the deposit address, its QR, and four buttons: deposit, withdraw, export "
                     "key, history",
            errors=("no wallet yet: offers creation first, because a card about a wallet that does not exist is "
                    "a dead end",
                    "a custody mode that cannot sign (`read_only`): says so plainly and points at the web app"),
            buttons=("wallet:deposit,wallet:withdraw", "wallet:export,wallet:history"),
            nl_hint="'balance', 'my address', 'how much do I have'",
        ),
        Command(
            name="deposit", summary="the deposit address, as text and as a QR",
            syntax="/deposit [chain]", auth="owner",
            response="address + QR + the chains we accept + 'we credit after N confirmations'; a bridge leg shows "
                     "progress edits on this same message",
            errors=("an unsupported chain: names the supported ones", "a bridge that is stuck: the recovery path"),
            buttons=("deposit:copy,deposit:qr", "deposit:bridge,wallet:hide"),
        ),
        Command(
            name="withdraw", summary="send funds out, with the password and the allowlist",
            syntax="/withdraw <amount> <address|alias>", auth="owner",
            response="a confirmation card with amount, address, network fee, and what arrives; Confirm/Cancel",
            errors=("over the balance: the largest sendable amount, not just 'insufficient'",
                    "address not on the allowlist: the cooldown and how to add it",
                    "no withdrawal password: the setup step, first"),
            buttons=("withdraw:confirm,withdraw:cancel", "withdraw:max,withdraw:allowlist"),
            touches_money=True, needs_confirmation=True,
            nl_hint="'send 100 to my wallet', 'cash out 250'",
        ),
        Command(
            name="balance", summary="the same balance half of /wallet, without the address",
            syntax="/balance", auth="owner",
            response="one line of total, one line per open position's value, and the freshness of the price used",
            errors=("a stale mark: the age is printed next to it rather than hidden",),
            buttons=("menu:wallet,menu:positions", "wallet:deposit,wallet:history"),
        ),
        Command(
            name="positions", summary="open positions with PnL, size and the worst drawdown",
            syntax="/positions [market]", auth="owner",
            response="a row per position: market, side, size, entry, mark (with its age), realised and unrealised, "
                     "and the drawdown",
            errors=("no positions: says so and offers the market card instead of an empty table",),
            buttons=("positions:close:<id>,positions:detail:<id>", "menu:pnl,menu:orders"),
            touches_money=False,
        ),
        Command(
            name="pnl", summary="realised, unrealised, fees paid, and the drawdown",
            syntax="/pnl [7d|30d|all]", auth="owner",
            response="a four-number card: realised, unrealised, fees, drawdown — each with its window stated",
            errors=("a window with no trades: zero, not an error, with the last trade's date for context",),
            buttons=("pnl:7d,pnl:30d,pnl:all", "menu:positions,menu:export"),
        ),
        Command(
            name="orders", summary="open and recent orders, with cancel buttons",
            syntax="/orders [open|filled|cancelled]", auth="owner",
            response="open orders first, each with a Cancel button; then the last few fills",
            errors=("an order in the `unknown` state: shown at the top with the sentence that says we are "
                    "reconciling it, never omitted",),
            buttons=("orders:cancel:<id>,orders:cancelall", "orders:open,orders:filled"),
            # touches_money AND needs_confirmation: the card itself is a read, but the Cancel button under every
            # row is a money action, and `menu_findings()` is right to demand the flag for it — a cancel that
            # happens on one tap from a list is a cancel somebody taps by accident while scrolling.
            touches_money=True, needs_confirmation=True,
        ),
        Command(
            name="market", summary="paste a Polymarket link (or search) and get a tradeable card",
            syntax="/market <slug | url | question>", auth="public",
            response="the market card: question, both outcomes with odds and their age, volume, resolution date, "
                     "and BUY YES / BUY NO buttons; a tap opens the confirm card",
            errors=("two markets match: the disambiguation card, both listed, tap to choose",
                    "no match: the closest three by tokens, then /search",),
            buttons=("market:yes:<slug>,market:no:<slug>", "market:chart:<slug>,market:watch:<slug>"),
            nl_hint="a pasted URL, a slug, or 'the fed cut market'",
        ),
        Command(
            name="search", summary="find markets by text",
            syntax="/search <query>", auth="public",
            response="up to five results as buttons, each with its odds and volume; tapping opens the market card",
            errors=("no results: the query tokens that were dropped, so the user can see why",),
            buttons=("search:open:<slug>", "search:more:<query>"),
        ),
        Command(
            name="price", summary="just the price, in one line",
            syntax="/price <slug|url>", auth="public",
            response="one line: outcome, odds, the age of the quote, and a link to the full card",
            errors=("no match: the same disambiguation as /market",),
            buttons=("market:yes:<slug>,market:no:<slug>",),
        ),
        Command(
            name="copy", summary="copy-trading sources: list, add, configure, pause, stop",
            syntax="/copy [list|add <handle>|config <id>|pause <id>|resume <id>|stop <id>]", auth="owner",
            response="a card per source with its state, its drawdown, its gate and the buttons that act on it; "
                     "every live change needs the same confirmation as the web app",
            errors=("adding a source that has not traded for 30 days: refused with the reason",
                    "no sources: the discovery list, ranked risk-adjusted, with the sample gate shown"),
            buttons=("copy:add:<handle>,copy:pause:<id>", "copy:config:<id>,copy:stop:<id>"),
            touches_money=True, needs_confirmation=True,
        ),
        Command(
            name="alerts", summary="personal alert rules: list, add, pause, quiet hours",
            syntax="/alerts [list|add <rule>|pause <id>|quiet <from> <to>]", auth="owner",
            response="a card per rule with its condition, its budget and its state; a new rule shows the dry run "
                     "of what it would have fired on in the last week",
            errors=("a rule that would fire more than the budget allows: named before it is saved",),
            buttons=("alerts:add,alerts:pause:<id>", "alerts:quiet,alerts:test"),
        ),
        Command(
            name="follow", summary="watch a wallet's tape without copying it",
            syntax="/follow <handle|address>  ·  /unfollow <handle>", auth="owner",
            response="confirmation with the wallet's pseudonym, its 30-day record and its sample gate; the follow "
                     "list is one card with Unfollow buttons",
            errors=("an address that has never traded: refused, because a follow on silence is noise",),
            buttons=("follow:add:<handle>,follow:list", "follow:unfollow:<handle>"),
        ),
        Command(
            name="top", summary="the leaderboard snapshot",
            syntax="/top [board] [n]", auth="public",
            response="the top n of the requested board with the formula, the sample gate and the drawdown line; "
                     "each row is a button that opens that trader's shared page",
            errors=("an unknown board: the list of boards",),
            buttons=("top:board:risk_adjusted,top:board:volume", "top:board:rising,top:board:win_rate"),
        ),
        Command(
            name="settings", summary="defaults: slippage, confirm threshold, notifications, one-click mode",
            syntax="/settings [key value]", auth="owner",
            response="the current values with their buttons; every change is confirmed and echoed back",
            errors=("one-click mode without the warning acknowledged: refused (the acknowledgement is a row, not "
                    "a checkbox)",
                    "a confirm threshold below the fee floor: refused with the arithmetic",),
            buttons=("settings:slippage,settings:confirm", "settings:notifications,settings:oneclick"),
            touches_money=False,
        ),
        Command(
            name="stop", summary="the panic command: cancel everything and halt automation now",
            syntax="/stop", auth="owner",
            response="no confirmation, no menu: cancels every open order, halts every automation, replies with what "
                     "was cancelled; the reply carries the Resume button and nothing else",
            errors=("partial failure: the reply lists which order could not be cancelled and why, and says the "
                    "halt still holds",),
            buttons=("stop:resume", "stop:report"),
            touches_money=True,
        ),
        Command(
            name="help", summary="the command list with examples, and the safety rules",
            syntax="/help [command]", auth="public",
            response="the grouped command list; with an argument, that command's syntax, auth and errors",
            errors=(),
            buttons=("help:commands,help:safety", "help:support,help:verify"),
        ),
        Command(
            name="support", summary="how to reach a human — and how to tell a fake one",
            syntax="/support", auth="public",
            response="one line: nobody from this bot will ever DM first, ask for a seed phrase or ask for a "
                     "deposit; then the only two legitimate places to ask for help",
            errors=(),
            buttons=("support:verify,help:safety", "support:report"),
        ),
        Command(
            name="verify", summary="is an account claiming to be our support real?",
            syntax="/verify <@username>", auth="public",
            response="a yes/no card naming the account, the check that was performed and the timestamp; a no says "
                     "what to do (report and block)",
            errors=("an account we have never issued a support handle for: a plain no, with the impersonation "
                    "warning",),
            buttons=("support:report,help:safety",),
        ),
    )


def command(name: str) -> Command | None:
    """One command by name or alias — the router's lookup, and the `/help <command>` path."""
    key = str(name or "").lstrip("/").lower()
    for c in commands():
        if key == c.name or key in c.aliases:
            return c
    return None


def main_menu() -> Keyboard:
    """The keyboard `/start` and `/help` answer with: three rows, six buttons, every high-value path one tap away.

    The layout is the design decision. Row 1 is the daily loop (market → positions → wallet); row 2 is the
    *earning* loop (copy → alerts → follow); row 3 is everything else, including `/stop` — visible, not buried, and
    wearing the danger style so it reads as what it is.
    """
    return Keyboard().add(
        Row().add(Button("📈 Market", action="menu", part="market"), Button("📊 Positions", action="menu", part="positions"),
                  Button("💰 Wallet", action="menu", part="wallet")),
        Row().add(Button("🧬 Copy", action="menu", part="copy"), Button("🔔 Alerts", action="menu", part="alerts"),
                  Button("👀 Follow", action="menu", part="follow")),
        Row().add(Button("🏆 Top", action="menu", part="top"), Button("⚙️ Settings", action="menu", part="settings"),
                  Button("🛑 Stop everything", action="stop", part="now", style="danger", confirm=False)),
    )


def order_card_keyboard(*, action_id: str, slug: str) -> Keyboard:
    """BUY YES / BUY NO / size chips, then the confirm pair — the two-tap path from a link to an order.

    The size chips carry the amount in `callback_data` because it is a number the user chose from a fixed set; the
    *price* deliberately does not travel in the payload, since a price typed into a button is a price from the past
    and the confirm card must re-read it.
    """
    return Keyboard().add(
        Row().add(Button("BUY YES", action="market", part="yes", value=slug, style="primary"),
                  Button("BUY NO", action="market", part="no", value=slug, style="primary")),
        Row().add(Button("$25", action="size", part="25", value=slug), Button("$50", action="size", part="50", value=slug),
                  Button("$100", action="size", part="100", value=slug), Button("custom", action="size", part="c", value=slug)),
        Row().add(Button("chart", action="market", part="chart", value=slug),
                  Button("watch", action="market", part="watch", value=slug)),
    )


def confirm_keyboard(*, action_id: str, side: str) -> Keyboard:
    """Confirm / Cancel for a trade, with the confirm button carrying the action id the server minted.

    The id is what makes the tap idempotent from the button's side too: a double tap sends the same `callback_data`
    twice, and the server answers the second one with the first one's result rather than a second order.
    """
    return Keyboard().add(
        Row().add(Button("✅ Confirm", action="confirm", part=side, value=action_id, style="primary", confirm=True),
                  Button("✖️ Cancel", action="cancel", part=side, value=action_id, style="danger")),
    )


def stop_keyboard() -> Keyboard:
    """The panic reply's keyboard: Resume, and the report. Nothing else, so nothing is a mis-tap away."""
    return Keyboard().add(Row().add(Button("▶️ Resume", action="stop", part="resume"), Button("📄 What was cancelled",
                                                                                              action="stop", part="report")))


def parse_callback(data: str) -> tuple:
    """`'market:yes:fed-cut-sept'` → `('market', 'yes', 'fed-cut-sept')`, or `('', '', '')` if it is not ours.

    Shape-validated rather than trusted: an unknown action or a malformed part is an empty tuple, and the router
    answers the tap with the menu. A payload is data — it is never interpolated into a query, a path or a template.
    """
    text = str(data or "")
    if len(text.encode("utf-8")) > MAX_CALLBACK_BYTES or not CALLBACK_RE.match(text):
        return "", "", ""
    action, part, value = text.split(":", 2)
    if action not in CALLBACK_ACTIONS:
        return "", "", ""
    return action, part, value


#: Every action a button may carry. A table rather than a free string, so the router's dispatch and the menu's
#: buttons cannot drift apart — and so an unknown action from an old message (a card from last week) is answered
#: with the menu instead of an error.
CALLBACK_ACTIONS = ("menu", "market", "size", "confirm", "cancel", "stop", "positions", "orders", "wallet",
                    "deposit", "withdraw", "copy", "alerts", "follow", "top", "settings", "help", "support",
                    "search", "pnl", "export", "verifystate")


def menu_findings(table: tuple | None = None) -> list:
    """Every way the command table can be wrong — the gate's scanner, and the canary's target.

    The five failures it looks for are the ones that reach a user: a command with no way to tap it, a command whose
    refusals are undocumented (so the handler improvises an apology), a money command that can be triggered without
    a confirmation, a callback payload the Bot API will reject, and a payload carrying something private (an
    address, a token) into Telegram's servers.
    """
    out = []
    names = set()
    for c in table if table is not None else commands():
        if c.name in names:
            out.append("the command %s is declared twice" % c.name)
        names.add(c.name)
        for field_name in ("summary", "syntax", "response"):
            if not str(getattr(c, field_name, "")).strip():
                out.append("/%s has no %s" % (c.name, field_name))
        if c.auth not in AUTH:
            out.append("/%s declares the auth level %r" % (c.name, c.auth))
        if not c.buttons:
            out.append("/%s has no inline alternative, so it is unreachable for a user who taps" % c.name)
        if c.touches_money and not c.needs_confirmation and c.name != "stop":
            out.append("/%s touches money without a confirmation" % c.name)
        if c.touches_money and not c.errors:
            out.append("/%s touches money with no documented refusal" % c.name)
    for c in commands():
        for row in c.buttons or ():
            for spec in str(row).split(","):
                spec = spec.strip()
                if not spec:
                    continue
                action, _, value = spec.partition(":")
                if action not in CALLBACK_ACTIONS:
                    out.append("/%s offers the unknown action %r" % (c.name, action))
                if "0x" in spec.lower():
                    out.append("/%s puts an address in a callback payload: %s" % (c.name, spec))
    for kb in (main_menu(), order_card_keyboard(action_id="a1", slug="fed-cut-sept"),
               confirm_keyboard(action_id="a1", side="yes"), stop_keyboard()):
        for row in kb.rows:
            for b in row.buttons:
                data = b.callback_data() if not b.url else ""
                if data and len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
                    out.append("a button carries %d bytes of callback data" % len(data.encode("utf-8")))
                if data and not CALLBACK_RE.match(data):
                    out.append("a button carries malformed callback data: %s" % data)
                if data and "0x" in data.lower():
                    out.append("a button carries an address: %s" % data)
    return out


def natural_language_findings(parsed: dict) -> list:
    """The fallback path's own rules, checked on a parser result.

    A user who types "buy 50 yes on the fed market" must get a confirmation card rather than an error, and the
    parser's honesty is the whole feature: a low-confidence parse must ask, and a parse with two plausible markets
    must show both. So the checks are that a confidence is stated, that anything below the threshold is a question
    rather than an action, and that an ambiguous parse carries its alternatives.
    """
    out = []
    conf = parsed.get("confidence")
    if not isinstance(conf, (int, float)):
        return ["a parse without a confidence cannot be acted on or safely refused"]
    if conf < 0 or conf > 1:
        out.append("a confidence of %r is not a probability" % conf)
    if conf < parsed.get("threshold", 0.75) and not parsed.get("question"):
        out.append("a below-threshold parse must ask a question, not act")
    if parsed.get("ambiguous") and not parsed.get("options"):
        out.append("an ambiguous parse must carry its alternatives")
    if parsed.get("action") and conf < parsed.get("threshold", 0.75) and parsed.get("executes"):
        out.append("a below-threshold parse executed something")
    return out
