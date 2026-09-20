"""The OG card: what a link preview is allowed to say, and why it says the same thing as the page.

A preview card is the most-read surface this product will ever ship and the least-reviewed one: it is rendered
by somebody else's unfurler, in somebody else's layout, on a phone, next to a stranger's opinion. So the card
model is deliberately narrow and entirely derived:

* **Every value comes from the public payload**, formatted by the same helper the page uses. The card and the
  page disagreeing about a number is not a rendering bug — it is two claims about the same wallet, and the one
  that travels further is the one nobody can correct.
* **The disclaimers travel with the numbers.** If the page says the win rate is behind a sample gate, if the
  wallet is provisional, if there is a drawdown — the card carries those as its own lines. A share card that
  says "$412k realised" while the page says "provisional, 7 days, no history yet" is the single most damaging
  thing this deliverable could ship, and it is one forgotten line of code away at all times.
* **The card has no free text.** Title, subtitle and lines are all built here, from the payload, by functions
  that take no prose argument. There is nowhere to type a slogan, which is what stops one appearing.

`card_key` is what makes the cache correct: it is a hash of the card's own content, so the rendered image for a
given set of numbers is the same bytes for everyone, and it changes exactly when the numbers do.
"""
from __future__ import annotations

import hashlib
import json
import re

ADDRESS_RX = re.compile(r"^0x[0-9a-fA-F]{40}$")

#: The brand treatment's text half. Colours and geometry live in the web layer (the design system owns them);
#: what belongs here is the words and the structure, because those are the parts a test can hold still.
BRAND = "Openout"
FOOTER = "openout.app — prediction markets, ranked on the trade and not the story"

#: A card shows at most this many stat lines. Three is what a phone-sized preview can render legibly; a fourth
#: is the beginning of a table, and a table in an OG image is a table nobody reads.
MAX_LINES = 3


def _stat(label: str, value: str) -> dict:
    return {"label": str(label), "value": str(value)}


def trader_card(*, handle: str, headline: str, stats: list, notes: list, ranked: bool,
                provisional: bool, drawdown: str = "", url: str = "") -> dict:
    """A trader's share card.

    `notes` is the page's own list of qualifiers (the sample gate, the provisional label, the disputed-market
    exclusions). The card renders the first two as its footnote — not "up to two": two, because the third line
    of a footnote is off the bottom of the image and a disclaimer nobody sees is not a disclaimer.
    """
    lines = [_stat(lbl, val) for lbl, val in stats][:MAX_LINES]
    # The footnote has a priority order, and it is the product's, not the caller's: a provisional wallet and a
    # drawdown are the two facts this phase refuses to drop, so they are placed first and the caller's notes
    # fill whatever room is left. (The first version appended the notes first and then inserted the drawdown by
    # deleting the line it collided with — which quietly deleted "provisional" instead, i.e. the exact failure
    # this docstring claims to prevent. It is written as a priority list now because the bug was a priority.)
    foot: list[str] = []
    if provisional:
        foot.append("provisional: fewer than 7 days of history")
    if drawdown:
        foot.append("drawdown %s" % drawdown)
    # The forced lines are FACTS, and a note that restates one of them is dropped rather than appended: the
    # page's own notes carry "worst drawdown 400" beside the card's "drawdown 400", and a card whose only two
    # footnote lines say the same thing in two words has spent a line it needed. (The first version compared
    # first words, so "worst drawdown" did not match "drawdown" and the duplicate shipped.)
    claimed = [w for f in foot for w in str(f).lower().replace(":", " ").split() if len(w) > 3]
    for n in notes:
        if len(foot) >= 2:
            break
        text = str(n or "").strip()
        low = text.lower()
        if not text or any(w in low for w in claimed):
            continue
        foot.append(text)
    foot = foot[:2]
    return {"kind": "trader", "title": "@" + str(handle), "subtitle": str(headline), "lines": lines,
            "footnote": foot, "brand": BRAND, "footer": FOOTER, "url": str(url), "ranked": bool(ranked),
            # Self-describing on purpose: `ranked` says a rank is printed, `provisional` says the record is
            # younger than the rankable window. Without the flag, every consumer (the gate, the web card, an
            # unfurler's alt text) has to infer "should this card say provisional" from something else, and an
            # inference made in three places is three chances to disagree about a disclaimer.
            "provisional": bool(provisional)}


def market_card(*, question: str, odds: list, meta: list, url: str = "") -> dict:
    """A market's share card: the question, the odds, and the freshness stamp.

    The odds are the payload's formatted strings, so a card cannot round differently from the page. A market
    whose odds are stale gets the staleness in `meta`, because a quoted price with no window is a price
    somebody will treat as current.
    """
    return {"kind": "market", "title": str(question), "subtitle": "",
            "lines": [_stat(lbl, val) for lbl, val in odds][:MAX_LINES],
            "footnote": [str(m) for m in meta][:2], "brand": BRAND, "footer": FOOTER, "url": str(url)}


def leaderboard_card(*, board: str, window: str, rows: list, notes: list, url: str = "") -> dict:
    """A board's share card: the top three, in order, with the formula's name and the row count."""
    lines = [_stat("#%d %s" % (i, str(label)), str(value)) for i, (label, value) in enumerate(rows[:MAX_LINES], 1)]
    return {"kind": "leaderboard", "title": str(board), "subtitle": str(window), "lines": lines,
            "footnote": [str(n) for n in notes][:2], "brand": BRAND, "footer": FOOTER, "url": str(url)}


def card_findings(card: dict) -> list:
    """What must never be on a card.

    Checked here rather than trusted to the callers because the card is assembled from three different payloads
    and the one that leaks will be the one nobody re-read: an address, an email, or an account-shaped id.
    """
    out = []
    text = json.dumps(card, default=str)
    for token in re.findall(r"\S+", text):
        t = token.strip('",:[]{}')
        if ADDRESS_RX.match(t):
            out.append("the card carries a wallet address")
        if t.startswith("u-") and len(t) > 6:
            out.append("the card carries an account id")
    if not card.get("title"):
        out.append("a card with no title renders as an empty image")
    return out


def card_key(card: dict) -> str:
    """A content hash: the same numbers render the same bytes, and the key changes exactly when they change."""
    body = json.dumps({k: v for k, v in card.items() if k not in ("url",)}, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]
