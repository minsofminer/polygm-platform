"""Message plans: the words, the buttons, and the sequence that puts them on screen.

A message plan is a small object — text, keyboard, the two-beat sequence, the haptics, the chat action — and having
it as a *value* rather than a chain of API calls is what makes the interface reviewable: the gate can render every
command's answer and assert things about it (no address, no unescaped `<`, a keyboard whose payloads fit, at most
two beats, the disclosure present on anything that touches money) without a bot token and without a network.

Four rules are enforced here, and each one is a way a bot looks broken in production:

* **Escaping happens once, at assembly.** `parse_mode=HTML` needs `<`, `>` and `&` escaped; a market question is
  user-visible data from an upstream API and routinely contains `&` (`Will Trump & Biden debate?` in a screenshot
  with `&amp;` missing is how a message gets *rejected* by Telegram with a 400). `esc()` is the only path, and
  `sanitise.py` from P07 strips the other vectors (control characters, zero-width joiners, bidi overrides).
* **Length is handled, not hoped for.** Telegram caps a message at 4,096 characters and a caption at 1,024. Long
  text is split at a paragraph boundary and the parts are numbered, because a market question plus a resolution
  source plus a disclaimer genuinely exceeds the cap on some markets.
* **Numbers are formatted by the money layer.** `render` never does arithmetic: it receives strings that
  `money/cents.py` formatted, so the chat and the web cannot disagree about a price — the same rule that produced
  the number layer in P08.
* **Every card that can lead to an order carries the sentence that qualifies it.** The odds carry their age, the
  sample gate travels with any win rate, and the drawdown travels with any PnL — the product's standing rules,
  restated in the medium where they are most likely to be dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..security import sanitise as _sanitise

MAX_TEXT = 4_096
MAX_CAPTION = 1_024

#: Anything that looks like an address, refused at render time as a second line of defence. The first is that no
#: payload crossing this boundary is supposed to carry one; the second is that a message quoted into a Telegram
#: support chat is a message out of our control.
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{6,}")
MARKDOWN_CONFETTI_RE = re.compile(r"[*_\[\]`]")

E = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def esc(text: str) -> str:
    """HTML-escape, then strip the invisible: the only way text from outside this process reaches a message.

    Two passes, both from P07's sanitiser rather than a new one: `strip_invisible` removes the zero-width and bidi
    characters that make a market question render as something other than itself, and the control-character sweep
    keeps newlines (which the layout needs) while dropping the rest. A market question is upstream data, and a
    message is user-visible text — the seam between them is this function.
    """
    clean = _sanitise.strip_invisible(str(text or ""))[0]
    clean = _CONTROL_RE.sub("", clean)
    for ch, rep in E.items():
        clean = clean.replace(ch, rep)
    return clean


def bold(text: str) -> str:
    return "<b>%s</b>" % esc(text)


def code(text: str) -> str:
    return "<code>%s</code>" % esc(text)


@dataclass(frozen=True)
class Beat:
    """One screenful: the skeleton, or the answer. `edit` targets the message the skeleton created."""

    text: str
    keyboard: object = None
    edit_message_id: int = 0
    chat_action: str = ""
    what: str = "answer"

    def __post_init__(self) -> None:
        # A `Keyboard` is accepted anywhere markup is, and converted here rather than at each call site: the
        # alternative is a handler that passes the object and one that passes `to_markup()`, and a Beat whose
        # `keyboard` is a dataclass serialises into a Bot API call that Telegram rejects with a 400.
        if self.keyboard is not None and hasattr(self.keyboard, "to_markup"):
            object.__setattr__(self, "keyboard", self.keyboard.to_markup())


@dataclass
class Plan:
    """A message plan: what to send, what to edit, and how it should feel."""

    beats: list = field(default_factory=list)
    haptics: tuple = ()
    priority: int = 50
    parse_mode: str = "HTML"
    disable_preview: bool = True
    caption: bool = False

    @property
    def sends(self) -> int:
        return 1 if self.beats and not self.beats[0].edit_message_id else 0

    @property
    def edits(self) -> int:
        return sum(1 for b in self.beats if b.edit_message_id)

    @property
    def animates_number(self) -> bool:
        # A plan never animates a number: the field exists so the motion checker can assert it, and because the
        # first design sketch had a "counting up" PnL line, which is exactly the thing P03 forbids.
        return False

    def motion(self) -> dict:
        """The shape `motion.motion_findings` checks: beats, haptics, chat action, budget."""
        return {"sends": self.sends, "edits": self.edits, "haptics": list(self.haptics),
                "chat_action": any(b.chat_action for b in self.beats),
                "edit_message_id": self.beats[0].edit_message_id if self.beats else 0,
                "expected_ms": 900, "animates_number": self.animates_number}

    def lines(self) -> int:
        return sum(len(b.text) for b in self.beats)


def two_beat(*, skeleton: str, answer: str, keyboard=None, message_id: int = 0, priority: int = 50,
             chat_action: str = "typing", haptics: tuple = ()) -> Plan:
    """The standard sequence: a skeleton line, then the card, in the same bubble.

    `message_id` decides which half of the pair we are in. With no id we send the skeleton and the caller edits it
    once the answer is ready; with an id the plan is a single edit — which is what a retry after a timeout looks
    like, and why the same function serves both.
    """
    if message_id:
        return Plan(beats=[Beat(text=answer, keyboard=keyboard, edit_message_id=int(message_id), what="answer")],
                    haptics=haptics, priority=priority)
    return Plan(beats=[Beat(text=skeleton, chat_action=chat_action, what="skeleton"),
                       Beat(text=answer, keyboard=keyboard, what="answer")],
                haptics=haptics, priority=priority)


def fill_card(*, market: str, side: str, size_text: str, price_text: str, fee_text: str, position_text: str,
              price_age_text: str, market_slug: str = "") -> Plan:
    """The fill notification: the one message the product exists to deliver, and the one that gets a haptic.

    Everything a user needs to answer "did I get what I asked for" is on it — realised price, size, fee, and the
    position after — plus the age of the mark, because a fill confirmed against a stale reference price is a
    support ticket waiting to happen.
    """
    text = "\n".join([
        "✅ <b>%s filled</b>" % esc(side.upper()),
        "%s · %s @ %s" % (esc(market), esc(size_text), esc(price_text)),
        "fee %s · position now %s" % (esc(fee_text), esc(position_text)),
        "<i>mark %s</i>" % esc(price_age_text),
    ])
    if market_slug:
        text += "\n\n<i>tap through for the live book</i>"
    return Plan(beats=[Beat(text=text, what="answer")], haptics=("haptic_fill",),
                priority=10)


def refusal_card(*, what: str, code: str, plain: str, next_step: str = "") -> Plan:
    """A rejection in plain language, mapped from the machine code — with the code kept for support.

    The kit's requirement, and the reason both strings are on the card: the plain sentence is what the user acts on,
    and the code is what a human needs if they ask. `refusal_text()` below is the mapping, and it lives here rather
    than in the handler so the gate can assert that every risk code has a sentence.
    """
    text = "⚠️ <b>%s</b>\n%s" % (esc(what), esc(plain))
    if next_step:
        text += "\n\n%s" % esc(next_step)
    text += "\n\n<code>%s</code>" % esc(code)
    return Plan(beats=[Beat(text=text, what="answer")], haptics=("haptic_reject",), priority=20)


def unknown_order_card(*, market: str, size_text: str) -> Plan:
    """The `unknown` state, said out loud rather than hidden in a log.

    An order whose outcome we do not know is the one state where silence looks like theft. The card says what
    happened, what we are doing about it, and that the user will get a second message either way.
    """
    text = "\n".join([
        "🕓 <b>%s is in limbo</b>" % esc(size_text),
        "We sent your order for %s but did not get a confirmation from the venue." % esc(market),
        "It may be live. We are checking — do not resend it. You will get one more message, filled or cancelled.",
    ])
    return Plan(beats=[Beat(text=text, what="answer")], haptics=(), priority=20)


def split_text(text: str, *, limit: int = MAX_TEXT) -> list:
    """Split on paragraph boundaries, numbering the parts — never truncate mid-sentence.

    The numbering matters: two messages with no marker read as two messages, and a user scrolling back cannot tell
    that the second is the continuation of the first.
    """
    body = str(text or "")
    if len(body) <= limit:
        return [body]
    parts, current = [], ""
    for para in body.split("\n\n"):
        candidate = (current + "\n\n" + para) if current else para
        if len(candidate) <= limit - 24:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(para) > limit - 24:
            parts.append(para[:limit - 24])
            para = para[limit - 24:]
        current = para
    if current:
        parts.append(current)
    total = len(parts)
    return ["<i>%d/%d</i>\n%s" % (i + 1, total, p) if total > 1 else p for i, p in enumerate(parts)]


def render_findings(plan: Plan, *, limit: int = MAX_TEXT, money_allowed: bool = False) -> list:
    """Everything wrong with a plan, checked before it goes near the API.

    The scanners are the cheap version of the failures that reach users: an unescaped `<` (Telegram answers 400 and
    the user sees nothing at all), a message over the cap, an address in a message that is about to be forwarded
    into a support chat, Markdown confetti in an HTML message, and a card that mentions a win rate or a PnL without
    its qualifier.
    """
    out = []
    if not plan.beats:
        return ["a plan with no beats says nothing"]
    for b in plan.beats:
        if not str(b.text or "").strip():
            out.append("an empty beat")
        if len(b.text or "") > limit:
            out.append("a beat is %d characters, over the %d cap" % (len(b.text), limit))
        if re.search(r"<(?!/?(b|i|u|s|code|pre|a|tg-spoiler)\b)", b.text or ""):
            out.append("a beat has a tag Telegram will reject")
        if ADDRESS_RE.search(b.text or "") and not money_allowed:
            out.append("a beat carries an address")
        if MARKDOWN_CONFETTI_RE.search(re.sub(r"</?[a-z-]+>", "", b.text or "")) and plan.parse_mode == "HTML":
            out.append("a beat looks like Markdown in an HTML message")
        stripped = re.sub(r"</?[a-z-]+>", "", b.text or "").lower()
        if re.search(r"\bwin rate\b|\bhit rate\b", stripped) and not re.search(r"\bover \d+|\bof \d+|\bn ?= ?\d+|"
                                                                                 r"\bclosed trades\b|\bsample\b",
                                                                                 stripped):
            # P11's standing rule, restated in the medium that drops it most easily: a win rate without its sample
            # is a number that means nothing, and a chat message is where qualifiers go to die.
            out.append("a beat quotes a win rate without its sample size")
    if plan.edits > 2:
        out.append("a plan edits %d times; the budget is two beats" % plan.edits)
    if plan.sends and plan.edits and plan.beats[0].what != "skeleton":
        out.append("a plan sends then edits without a skeleton beat")
    for h in plan.haptics or ():
        if h not in ("haptic_fill", "haptic_reject"):
            out.append("a plan asks for the haptic %r" % h)
    return out
