"""The motion spec — and the honest statement of what a chat interface is allowed to animate.

The user asked for a bot that *feels* alive: commands that answer with cards, buttons that do things, animation and
good UI. So this module starts from what the platform actually gives us in a chat, because promising motion that
Telegram does not have is how a bot ends up spamming a chat to look busy:

**What Telegram gives us**

* `sendChatAction` — the "typing…" line. Expires after five seconds, so a slow handler must refresh it; it is the
  only animation a chat has while nothing is on screen yet.
* **Progressive edit** — sending one message and *editing* it. An edit through `editMessageText` is a real
  transition on the client: the text changes in place with the bubble staying put. This is the two-beat ("reading
  the book…" → the card) and it is the only animation primitive that carries information.
* **`editMessageReplyMarkup`** — swapping the buttons under a message in place. After a tap, the button that was
  pressed becomes disabled and its neighbours change: that is the confirmation animation, and it must be an *edit*
  rather than a second message, or the chat fills with dead cards.
* `answerCallbackQuery` — ends the client-side spinner on the pressed button. It has a deadline (a few seconds),
  after which Telegram re-enables the button and the tap looks ignored. So it is answered *first*, always.
* Animated emoji — a `tgs` sticker or an emoji with `is_premium` rendering. We use exactly one, for the fill
  notification, and the rule is that it must never be the only thing that says what happened.

**What it does not have, and what we therefore refuse to fake**

* There is no transition on message text. Editing twice in 200 ms flickers; editing three times reads as a bug.
  So the budget is: **at most two edits per user action**, and the second one is the final answer.
* There is no scroll animation, no skeleton shimmer, no spring. A "loading shimmer" in a chat is a message we send
  and then delete, which is a worse experience than the typing line it replaces — and Telegram may not delete it.
* **Nothing animates a number.** P03's rule, and it is doubly true here: a price that changes under a user's thumb
  between the card and the confirmation is a price they did not agree to. A number changes only when the *card*
  changes, and the card changes only after a fresh read.

**Durations and easings** are P03's tokens, not new numbers, so the Mini App and the chat move at the same speed:
`press 100 ms`, `micro 80 ms`, `small 125 ms`, `medium 150 ms`, `large 200 ms`, `flash 200 ms` (and the one 300 ms
drawer easing, which is gesture-driven and exists only inside the Mini App). The chat has three of them; the rest
belong to the Mini App, where the motion is real CSS.
"""
from __future__ import annotations

#: The P03 motion tokens, copied here as values rather than imported: the Python package cannot read a CSS file,
#: and a constant that silently drifts from the design system is worse than one that is visibly duplicated and
#: checked — `tests/test_telegrambot.py` asserts these equal `web/styles/tokens.css`.
DUR_PRESS_MS = 100
DUR_MICRO_MS = 80
DUR_SMALL_MS = 125
DUR_MEDIUM_MS = 150
DUR_LARGE_MS = 200
DUR_DRAWER_MS = 300
FLASH_OUT_MS = 200
EASE_OUT = "cubic-bezier(0.23, 1, 0.32, 1)"
EASE_IN_OUT = "cubic-bezier(0.77, 0, 0.175, 1)"
EASE_DRAWER = "cubic-bezier(0.32, 0.72, 0, 1)"

#: How long Telegram's "typing…" lasts, and when to refresh it. Sending the action for a reply that lands in
#: 150 ms produces a flash of "typing" that reads as a glitch, so instant paths send nothing at all.
CHAT_ACTION_TTL_MS = 5_000
CHAT_ACTION_REFRESH_MS = 4_000
CHAT_ACTION_FLOOR_MS = 400

#: The edit budget. Two beats is the skeleton and the answer; a third edit is a design failure, not a polish item.
MAX_EDITS_PER_ACTION = 2

MOTIONS: dict = {
    "chat_action": {
        "primitive": "sendChatAction",
        "duration_ms": CHAT_ACTION_TTL_MS,
        "refresh_ms": CHAT_ACTION_REFRESH_MS,
        "applies_to": "every handler slower than the floor",
        "what_it_is": "the typing line, the only motion a chat has before anything is on screen",
        "must_not": "be sent for a reply faster than %d ms (a flicker), or be left running after the answer lands"
                    % CHAT_ACTION_FLOOR_MS,
    },
    "two_beat": {
        "primitive": "sendMessage then editMessageText",
        "duration_ms": DUR_SMALL_MS,
        "applies_to": "any answer that needs a read we cannot promise in one round trip",
        "what_it_is": "a skeleton line replaced in place by the card: one bubble, no chat spam",
        "must_not": "exceed %d beats, edit twice for the same information, or land more than %.1f s after the tap"
                    % (MAX_EDITS_PER_ACTION, 2.5),
    },
    "keyboard_swap": {
        "primitive": "editMessageReplyMarkup",
        "duration_ms": DUR_PRESS_MS,
        "applies_to": "every inline button after it is pressed",
        "what_it_is": "the pressed button stops being pressable while the action runs, in place",
        "must_not": "leave a dead card's buttons live: a second tap on Confirm must hit a disabled button, not send "
                    "a second order",
    },
    "callback_ack": {
        "primitive": "answerCallbackQuery",
        "duration_ms": DUR_MICRO_MS,
        "applies_to": "every callback, before any work",
        "what_it_is": "the only thing that stops the client's spinner, and it has a deadline",
        "must_not": "be skipped because the handler is busy — the spin times out and the tap looks ignored",
    },
    "haptic_fill": {
        "primitive": "HapticFeedback.impactOccurred('light')",
        "duration_ms": 0,
        "applies_to": "a confirmed fill, inside the Mini App only",
        "what_it_is": "the one place a phone is allowed to buzz",
        "must_not": "fire on menu opens, scrolls, price flashes or anything else: the kit's sentence is 'haptics on "
                    "fill — and nowhere else', and a phone that buzzes for everything is a phone the user mutes",
    },
    "haptic_reject": {
        "primitive": "HapticFeedback.notificationOccurred('error')",
        "duration_ms": 0,
        "applies_to": "a refused action in the Mini App (a rejected order, a refused confirm)",
        "what_it_is": "the second and last buzz: something the user asked for did not happen",
        "must_not": "be used for a *validation* hint (a disabled button is not an error), which is what makes it "
                    "informative",
    },
    "flash_change": {
        "primitive": "CSS background animation inside the Mini App",
        "duration_ms": FLASH_OUT_MS,
        "applies_to": "a number that arrived from a fresh read",
        "what_it_is": "the P03 primitive: the cell flashes, the digits do not move",
        "must_not": "animate the value, translate the row, or run when the number did not actually change",
    },
}

#: The motion whitelist, as data, so a review can disagree with a list rather than with a scatter of calls.
HAPTICS: tuple = ("haptic_fill", "haptic_reject")

#: What must never be animated, with the reason, so a future contributor can argue with it explicitly.
NEVER = (
    "a price: it changes only when the card is re-read, or the user confirms a number they never saw",
    "a PnL figure: P03 forbids moving a number, and a moving total is a total nobody can check",
    "a button's position: Telegram's inline keyboard is a grid we do not lay out; a 'shifting' keyboard is an "
    "illusion built from sending two keyboards, and it makes the second tap hit the wrong control",
)


def chat_action_plan(*, started_ms: int, now_ms: int, answered: bool) -> dict:
    """Whether to refresh the typing line, and when to start it.

    Returns `{"send": bool, "after_ms": int}`. The floor is what keeps instant replies from flickering, and the
    refresh keeps a slow handler (a market search, a risk gate call) alive past Telegram's five-second expiry.
    """
    elapsed = max(0, int(now_ms) - int(started_ms))
    if answered:
        return {"send": False, "after_ms": 0, "note": "the answer is landing; the typing line must follow it out"}
    if elapsed < CHAT_ACTION_FLOOR_MS:
        return {"send": False, "after_ms": CHAT_ACTION_FLOOR_MS - elapsed,
                "note": "too early to be honest: a reply this fast should not flash 'typing' at all"}
    since = elapsed % CHAT_ACTION_REFRESH_MS
    if since < 250 or elapsed < CHAT_ACTION_REFRESH_MS:
        return {"send": True, "after_ms": 0, "note": "first or refreshed typing line"}
    return {"send": False, "after_ms": CHAT_ACTION_REFRESH_MS - since, "note": "wait for the next refresh window"}


def motion_findings(plan: dict) -> list:
    """A message plan's motion, checked against the budget: at most two beats, no animated numbers, no stray buzz.

    This is what the gate canaries: the shapes it refuses are the ones that make a chat look broken — three edits,
    an edit that only changes a price, a haptic on a menu, a typing line sent for an instant reply.
    """
    out = []
    beats = int((plan or {}).get("edits", 0) or 0) + (1 if (plan or {}).get("sends") else 0)
    if beats > MAX_EDITS_PER_ACTION:
        out.append("a plan with %d beats exceeds the %d-beat budget" % (beats, MAX_EDITS_PER_ACTION))
    for h in (plan or {}).get("haptics") or []:
        if h not in HAPTICS:
            out.append("a plan asks for the haptic %r, which is not on the whitelist" % h)
    if (plan or {}).get("animates_number"):
        out.append("a plan animates a number, which P03 and P12 both refuse")
    if (plan or {}).get("chat_action") and int((plan or {}).get("expected_ms", 0) or 0) < CHAT_ACTION_FLOOR_MS:
        out.append("a typing line is scheduled for a reply faster than the floor")
    if int((plan or {}).get("edits", 0) or 0) and not (plan or {}).get("edit_message_id"):
        out.append("a plan edits a message without naming which one")
    if (plan or {}).get("sends", 0) and (plan or {}).get("edits", 0) and (plan or {}).get("new_message_per_beat"):
        out.append("a plan sends a new message per beat, which is chat spam rather than motion")
    return out


def mini_app_css() -> str:
    """The motion the Mini App owns, as CSS built from the same tokens.

    Kept as a string here so the *contract* between the chat and the Mini App is one file: the durations the bot
    quotes in a message ("checking the book…") and the ones the webview animates are read from the same constants.
    `web/src/globals.css` carries the real rules; this is the subset the Mini App must not disagree with, and
    `tests/test_telegrambot.py` asserts the durations in the stylesheet equal these.
    """
    return (
        ":root{--pgm-dur-press:%dms;--pgm-dur-micro:%dms;--pgm-dur-small:%dms;--pgm-dur-medium:%dms;"
        "--pgm-dur-large:%dms;--pgm-dur-drawer:%dms;--pgm-flash-out:%dms;--pgm-ease-out:%s;"
        "--pgm-ease-in-out:%s;--pgm-ease-drawer:%s}"
        % (DUR_PRESS_MS, DUR_MICRO_MS, DUR_SMALL_MS, DUR_MEDIUM_MS, DUR_LARGE_MS, DUR_DRAWER_MS, FLASH_OUT_MS,
           EASE_OUT, EASE_IN_OUT, EASE_DRAWER)
    )
