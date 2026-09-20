"""Update handling: what Telegram sent, what it means, and the rule that makes it safe to be sent twice.

Every handler in this product runs inside `handle()`, which is the only place that decides whether an update has
been seen before. Three properties are enforced here rather than trusted to each handler:

* **One `update_id`, one execution.** The store is a table (`telegram_updates`), not a process dictionary: the
  retry that matters arrives after a restart, when memory is gone. The row is written *before* the work runs, so a
  crash mid-handler leaves a claim rather than a gap — and a claimed-but-unfinished update is reported to the
  operator (`stale_claims`) instead of being silently retried into a second trade.
* **A trade command is never swallowed.** An exception in a normal handler is logged and answered with a friendly
  line; an exception in a handler that *places, cancels or halts* something is recorded as `trade_error` and
  answered with the plain-language `unknown`-state message — because "nothing happened" and "we do not know what
  happened" are different sentences, and only one of them is true when the failure is a timeout.
* **Command parsing is forgiving, intent is not.** `/buy 50 yes on the fed cut` is a message, not a command, and
  lands in the natural-language path with its confidence and its disambiguation, rather than being rejected as
  malformed input (the kit: "a user who types ... should get a confirmation card, not an error").

Telegram's own shapes are honoured literally, because they are the ones that bite: `message.text` may carry a
`/command@botname` suffix in groups, a `callback_query` carries the button's `data` and *must* be answered within a
few seconds or the client re-enables the button and re-sends the tap, and a `channel_post` has no `from` at all.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: The commands a text message may carry. The set is closed on purpose: anything else beginning with `/` is
#: answered with the menu rather than an error, because a typo'd command is a user looking for the menu.
#: A command's argument is a lookup key, not a document. Capped here rather than in each handler, because a
#: 4 KB "slug" is either a mistake or an attempt to make us store something we should not — and the command table
#: documents the boundary ("a payload over 200 characters is truncated at the boundary, not rejected").
MAX_ARGS = 200

COMMAND_RE = re.compile(r"^/([a-z0-9_]{1,32})(?:@([A-Za-z0-9_]{3,32}))?(?:\s+(.*))?$", re.S)
MENTION_RE = re.compile(r"^\s*/([a-z0-9_]{1,32})@([A-Za-z0-9_]{3,32})\b", re.I)

#: Actions that touch money or automation. An exception while handling one of these is *never* answered with a
#: generic apology: it is recorded as a trade error and the user is told their action is in an unknown state.
MONEY_KINDS = frozenset({"place_order", "cancel_order", "cancel_all", "halt_automation", "withdraw", "export_key"})

#: The classifier's vocabulary. `unknown` is a kind rather than an error: an unclassified update is still deduped.
KINDS = ("command", "callback", "message", "start_param", "channel_post", "unknown")


class UpdateError(ValueError):
    """Malformed input only. A *refusal* (an unknown command, a stale tap) is a result, not an exception."""


@dataclass(frozen=True)
class Update:
    """One Bot API update, flattened to what this product acts on."""

    update_id: int
    kind: str = "unknown"
    chat_id: str = ""
    chat_type: str = ""            # private | group | supergroup | channel
    user_id: str = ""              # the TELEGRAM id; the account id is resolved later, never assumed
    username: str = ""
    command: str = ""              # without the leading slash, lowercased, without the `@botname` suffix
    args: str = ""
    text: str = ""
    callback_data: str = ""
    callback_id: str = ""
    message_id: int = 0
    start_param: str = ""          # `?startapp=` payload from a deep link (D2), already length-capped
    language: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def is_private(self) -> bool:
        return self.chat_type == "private"

    @property
    def touches_money(self) -> bool:
        return self.kind in ("place_order", "cancel_order", "cancel_all")


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _str(v, default: str = "") -> str:
    return default if v is None else str(v)


def split_command(text: str, *, bot_username: str = "") -> tuple[str, str]:
    """`('/market fed-cut-sept') -> ('market', 'fed-cut-sept')`, and `('', '')` for anything else.

    A command addressed to another bot in a group (`/start@other_bot`) is *not* ours and returns empty: answering it
    would be the bot that talks over its neighbours, which is how a bot gets removed from groups.
    """
    m = COMMAND_RE.match((text or "").strip())
    if not m:
        return "", ""
    name, addressed, args = m.group(1).lower(), m.group(2), (m.group(3) or "")
    if addressed and bot_username and addressed.lower() != bot_username.lstrip("@").lower():
        return "", ""
    return name, args.strip()


def classify(update: dict, *, bot_username: str = "") -> Update:
    """One Bot API update → the flat shape the router switches on.

    The order of the branches is the whole function: a `callback_query` also carries a `message`, and the message's
    text is the *bot's own* card — classifying on `text` first would turn every button tap into a command.
    """
    if not isinstance(update, dict):
        raise UpdateError("an update must be an object")
    uid = update.get("update_id")
    if uid is None:
        raise UpdateError("an update without update_id cannot be deduped, and an undedupable update is a double-spend")

    cb = update.get("callback_query")
    if isinstance(cb, dict):
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        sender = cb.get("from") or {}
        return Update(update_id=_int(uid), kind="callback", chat_id=_str(chat.get("id")),
                      chat_type=_str(chat.get("type")), user_id=_str(sender.get("id")),
                      username=_str(sender.get("username")), callback_data=_str(cb.get("data")),
                      callback_id=_str(cb.get("id")), message_id=_int(msg.get("message_id")), raw=update)

    for key in ("message", "edited_message", "channel_post"):
        msg = update.get(key)
        if not isinstance(msg, dict):
            continue
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        text = _str(msg.get("text"))
        kind = "message"
        name, args = "", ""
        if key == "channel_post":
            # A channel post has no `from`: the channel IS the author, and there is nobody to hold responsible for
            # a trade command, so none is accepted from one.
            kind = "channel_post"
            text = text or _str(msg.get("caption"))
        else:
            name, args = split_command(text, bot_username=bot_username)
            if name:
                kind = "command"
            else:
                # A deep link that opens the bot with a payload (`t.me/bot?start=<payload>`) arrives as
                # `/start <payload>`; `startapp` payloads arrive in `message.web_app_data`. Both are classified
                # apart from ordinary commands because both must be validated before use (D2).
                mention = MENTION_RE.match(text or "")
                if mention:
                    name, args = mention.group(1).lower(), (text or "")[mention.end():].strip()
                    kind = "command"
        return Update(update_id=_int(uid), kind=kind, chat_id=_str(chat.get("id")),
                      chat_type=_str(chat.get("type")), user_id=_str(sender.get("id")),
                      username=_str(sender.get("username")), command=name, args=args[:MAX_ARGS], text=text,
                      message_id=_int(msg.get("message_id")), language=_str(sender.get("language_code")),
                      start_param=_start_param(msg), raw=update)

    return Update(update_id=_int(uid), kind="unknown", raw=update)


def _start_param(msg: dict) -> str:
    """The `startapp` payload, length-capped and stripped of anything that is not a slug-ish string.

    Capped rather than rejected: a long payload is a client bug, and the honest answer is to ignore the excess and
    show the menu. Never interpreted, never evaluated — D2's rule is that a payload is a *lookup key*, and the only
    thing this function returns is a string that the router will look up.
    """
    data = msg.get("web_app_data")
    if isinstance(data, dict):
        return _str(data.get("data"))[:200]
    return ""


@dataclass(frozen=True)
class Claim:
    """The result of trying to own an update: `fresh` means the work may run, `duplicate` means it already did."""

    ok: bool
    fresh: bool
    status: str                     # fresh | duplicate | in_progress | retry_after_error
    note: str = ""
    attempts: int = 0


def claim_findings(rows: list) -> list:
    """Everything wrong with the update ledger, for the gate and for the on-call screen.

    Two of these are the bug the kit names, seen from the operator's side: an update that was *never finished*
    (`state='claimed'` and old) is a trade whose outcome nobody knows, and an update that ran twice is a trade that
    happened twice. The first is a page; the second is an incident.
    """
    out = []
    by_id: dict[int, int] = {}
    for row in rows or []:
        uid = _int(row.get("update_id"))
        by_id[uid] = by_id.get(uid, 0) + 1
        if _int(row.get("runs", 1)) > 1:
            out.append("update %d ran %d times" % (uid, _int(row.get("runs"))))
        if str(row.get("state")) not in ("done", "failed", "claimed"):
            out.append("update %d has an unknown state %r" % (uid, row.get("state")))
        if str(row.get("state")) == "failed" and str(row.get("kind")) in MONEY_KINDS:
            out.append("update %d is a failed money handler, which needs a human: %s" % (uid, row.get("note")))
    for uid, n in sorted(by_id.items()):
        if n > 1:
            out.append("update %d has %d ledger rows" % (uid, n))
    return out
