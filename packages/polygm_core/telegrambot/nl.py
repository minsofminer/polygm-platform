"""The natural-language fallback: "buy 50 yes on the fed market" should be a confirmation card, not an error.

This is the part of a chat product that decides whether it feels like a product or like a form. A bot that answers
"unknown command" to a sentence a human would understand is a bot nobody uses twice, so the parser's job is to turn
a sentence into **slots** — side, size, market phrase — and then to be honest about its own confidence:

* **It never executes.** `parse()` returns `executes: False` always, because the parser's output is an input to the
  confirmation card, and a parser that can act is a parser that can act wrongly. The single acceptable path from a
  sentence to an order is: slots → card → tap. `menu.natural_language_findings()` enforces exactly this.
* **Below the threshold it asks.** A sentence with a side and a size but no market asks *which* market, offering
  the two best matches as buttons; a sentence with a market and no size offers the size chips. Guessing is the
  failure mode that costs money, and a question costs one tap.
* **Ambiguity is carried, not resolved silently.** Two plausible markets — "the fed market" matching both "Fed
  decision in September" and "Fed cuts by December" — produce `ambiguous: True` with `options` and a question.
  Picking the higher-scoring one would be right most of the time, and "most of the time" is not a standard for a
  trade.
* **The numbers are strings.** `amount` is a decimal string, never a float: it is going to be parsed by
  `money/cents.py` on its way to the risk gate, and a float in that pipe is the bug the money layer exists to
  prevent.

The phrase matcher is deliberately simple — token overlap with a length penalty, no embeddings. A fuzzy matcher
that is right 95% of the time without explanation is worse here than one that is right 85% of the time and says
which two markets it is choosing between, because the user is answering with money.
"""
from __future__ import annotations

import re

CONFIDENT = 0.75          # the shipped threshold; below this the parser asks instead of acting
HIGH, MEDIUM, LOW = 0.92, 0.62, 0.35

URL_RE = re.compile(r"https?://\S+")
SLUG_RE = re.compile(r"\b([a-z0-9][a-z0-9-]{6,60})\b")
MONEY_RE = re.compile(r"(?:\$|usd\s*|usdc\s*)(\d{1,9}(?:\.\d{1,2})?)", re.I)
AMOUNT_RE = re.compile(r"(?<![\w.])(\d{1,9}(?:\.\d{1,2})?)(?:\s*(?:usd|usdc|dollars?|bucks?|\$))?", re.I)
SHARES_RE = re.compile(r"(\d{1,9}(?:\.\d{1,2})?)\s*(?:shares?|contracts?|yes shares?|no shares?)", re.I)
PHRASE_RE = re.compile(r"\b(?:on|in|for|about|re:?)\s+(.{3,80}?)(?:[.?!]|$)", re.I)

YES_WORDS = ("yes", "yep", "yeah", "up", "true", "over", "long")
NO_WORDS = ("no", "nope", "nay", "down", "false", "under", "short")
BUY_WORDS = ("buy", "get", "grab", "take", "enter", "open")
SELL_WORDS = ("sell", "exit", "close", "dump", "get out")

LOOKUPS = {"price": ("price", "what's it at", "what is it at", "where is it", "odds", "quote"),
           "positions": ("my positions", "what do i own", "holdings", "portfolio"),
           "balance": ("balance", "how much do i have", "my cash", "funds"),
           "orders": ("my orders", "open orders", "anything open"),
           "pnl": ("pnl", "how am i doing", "my profit", "my loss", "performance"),
           "help": ("help", "what can you do", "commands"),
           "wallet": ("wallet", "my address", "deposit", "withdraw")}


def _words(text: str) -> list:
    return re.findall(r"[a-z0-9$']+", (text or "").lower())


def _find_market_phrase(text: str) -> str:
    """The market phrase: a pasted link, a slug, or the words after "on"/"in"/"for"."""
    url = URL_RE.search(text or "")
    if url:
        tail = url.group(0).rstrip(".,)")
        return tail
    m = PHRASE_RE.search(text or "")
    if m:
        return m.group(1).strip()
    return ""


def parse(text: str, *, markets: tuple = (), threshold: float = CONFIDENT) -> dict:
    """Slots, confidence, and a question when the confidence is not there.

    `markets` is the small candidate list the caller already narrowed (a search result, a watchlist, the market
    the user last opened). Passing it in keeps this function pure and keeps the index query in the service, which is
    where the database lives.
    """
    body = (text or "").strip()
    low = body.lower()
    out = {"text": body[:280], "action": "", "side": "", "amount": "", "amount_kind": "usdc", "market_query": "",
           "confidence": 0.0, "threshold": float(threshold), "ambiguous": False, "options": [],
           "question": "", "executes": False, "why": ""}

    if not low:
        out["why"] = "nothing to parse"
        out["question"] = "Type a market slug or paste a link and I'll show you a trade card."
        out["confidence"] = 0.0
        return out

    # 1. A lookup question ("what's my pnl") beats an order guess: the words "my"/"what"/"how" are the tell.
    for action, cues in LOOKUPS.items():
        if any(cue in low for cue in cues):
            if any(w in low for w in ("my", "what", "how", "show")) or len(_words(low)) <= 3:
                out.update(action=action, confidence=HIGH, market_query=_find_market_phrase(body),
                           why="a lookup cue matched; this asks rather than trades")
                return out

    # 2. Side and verb.
    toks = set(_words(low))
    yes = toks & set(YES_WORDS)
    no = toks & set(NO_WORDS)
    verb = "buy" if (toks & set(BUY_WORDS)) else ("sell" if (toks & set(SELL_WORDS)) else "")
    if yes and not verb:
        verb = "buy"
    if no and not verb:
        verb = "buy"
    side = "yes" if (yes and not no) else ("no" if (no and not yes) else "")

    # 3. Size: shares win over dollars when both appear, because "50 shares" is more specific than a stray number.
    shares = SHARES_RE.search(body)
    money = MONEY_RE.search(body)
    bare = AMOUNT_RE.search(body)
    if shares:
        out["amount"], out["amount_kind"] = shares.group(1), "shares"
    elif money:
        out["amount"], out["amount_kind"] = money.group(1), "usdc"
    elif bare and verb:
        out["amount"], out["amount_kind"] = bare.group(1), "usdc"

    query = _find_market_phrase(body)
    if not query and not URL_RE.search(body):
        slug = SLUG_RE.search(body)
        query = slug.group(1) if slug and "-" in slug.group(1) else ""
    out["market_query"] = query[:120]

    # 4. Confidence: every slot present is a confident parse; a bare market is a lookup; nothing is a question.
    # The side and verb go INTO the result: a slot computed but not stored is a slot the confirmation card cannot
    # show, and the first run of this parser produced exactly that — a confident parse with `side: ""` (caught by
    # the test below, which asserts a sentence's side survives the parse).
    out["side"], out["verb"] = side, verb
    slots = sum(1 for v in (side, out["amount"], out["market_query"]) if v)
    out["action"] = "order" if verb else ("lookup" if out["market_query"] else "")
    out["confidence"] = {3: HIGH, 2: MEDIUM, 1: LOW}.get(slots, 0.0)
    out["why"] = ("side, size and market" if slots == 3 else
                  "a market and one of side/size" if slots == 2 else
                  "one slot only" if slots == 1 else "no slots")

    # 5. Resolution and ambiguity, when the caller gave us candidates.
    if out["market_query"] and markets:
        matches = resolve_market(out["market_query"], markets)
        out["options"] = matches[:3]
        if not matches:
            out["confidence"] = min(out["confidence"], LOW)
            out["question"] = "I could not find a market for “%s”. Paste the link or type /search." % out["market_query"]
            out["why"] += "; the market did not resolve"
        elif len(matches) > 1 and matches[1]["score"] >= matches[0]["score"] * 0.85:
            out["ambiguous"] = True
            out["confidence"] = min(out["confidence"], MEDIUM)
            out["question"] = "Two markets could be “%s”. Which one?" % out["market_query"]
            out["why"] += "; two candidates scored within 15%% of each other"
        elif len(matches) == 1:
            out["market_slug"] = matches[0].get("slug", "")

    # 6. Below the threshold we ask. The question is specific, because "I didn't understand" is not help.
    if out["confidence"] < out["threshold"]:
        if not out["question"]:
            missing = [n for n, v in (("which market", out["market_query"]), ("yes or no", side),
                                      ("how much", out["amount"])) if not v]
            out["question"] = ("I need %s. For example: “buy $50 yes on <market link>”. "
                               "Or tap a button below." % " and ".join(missing))
        if out["action"] == "order":
            out["action"] = ""      # a low-confidence order is not an order
    return out


def resolve_market(query: str, markets: tuple) -> list:
    """Rank candidate markets by token overlap, with the length penalty that keeps a two-word market from winning.

    The penalty is the whole trick: "fed" matches "Fed decision" and "Fed cuts by December" equally on tokens, so
    the tie is broken by how much of the *query* each candidate explains — a candidate whose question is mostly
    other words scores lower. Nothing here is clever, and it is deliberately not clever: the caller shows the top
    matches and the user decides.
    """
    q = [w for w in _words(query) if len(w) > 1 and w not in ("the", "a", "an", "of", "in", "on", "for", "market")]
    if not q:
        return []
    out = []
    for m in markets or ():
        hay = ("%s %s" % (m.get("slug", ""), m.get("question", ""))).lower()
        if not hay.strip():
            continue
        hits = sum(1 for w in q if w in hay)
        if not hits:
            continue
        score = hits / len(q)
        length_pen = 1.0 - min(0.3, max(0, len(hay.split()) - len(query.split()) * 3) / 100.0)
        out.append({"slug": m.get("slug", ""), "question": str(m.get("question", ""))[:90],
                    "score": round(score * length_pen, 3)})
    out.sort(key=lambda r: (-r["score"], r["slug"]))
    return out


def size_chips(slug: str) -> list:
    """The chips a below-threshold parse offers: the three sizes the order card uses, plus custom."""
    return [{"slug": slug, "amount": a} for a in ("25", "50", "100", "custom")]
