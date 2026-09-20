"""Structured data, emitted only where the schema is true and only from fields the page already renders.

Two rules, both of them consequences of this being a *public* surface rather than an internal one:

1. **Never a field the page does not show.** Every string emitted here is asserted to be present in the public
   payload the page renders from (`public_strings` / `unbacked`). The failure mode this prevents is specific: a
   JSON-LD block is machine-readable, is quoted by aggregators, and is the easiest place in a page to add a
   number nobody reviewed — it does not appear on screen, so no screenshot, no visual diff and no reader ever
   sees the mistake. If a value is not on the page, it is not in the graph.
2. **Never a claim the product refuses to make.** The trader graph carries the sample-gate note and the
   drawdown alongside the headline stat, the same way the card and the page do. `Person` with an
   `interactionStatistic` of "realised PnL" and no sample size is exactly the schema.org shape that gets
   screenshotted into a tweet — and §2.10 already decided that a win rate without its sample is a number this
   product does not print.

What is genuinely applicable, and what is not:

* `BreadcrumbList` — on all three. True, cheap, and the only structured data a hand-built page needs.
* `ProfilePage` + `Person` — on `/trader/<handle>`. A page about one trader, whose identifier is a pseudonym.
  The `Person` is the *pseudonym* (that is the identity this product publishes, §2.19) and it carries no
  `address`, no `email`, and an `identifier` that is the handle, not an account.
* `ItemList` — on `/leaderboard/<board>`. The board is an ordered list, and `ItemList` says exactly that.
* `Dataset` — **not used.** It would be the flattering choice for a board (it reads as citable), and it would be
  false: a leaderboard is not a dataset we publish with a licence and a distribution.
* `Event` on `/market/<slug>` — used only when the market has a resolution date, because that is the only fact
  that makes the mapping true. An undated market gets the breadcrumb and nothing else, rather than an `Event`
  with an invented `startDate`. `eventStatus` is emitted only in the direction we can prove: accepting orders
  is `EventScheduled`; not accepting orders is *omitted*, because "not accepting orders" is not "cancelled".
"""
from __future__ import annotations

import re
from typing import Iterable

ADDRESS_RX = re.compile(r"^0x[0-9a-fA-F]{40}$")
EMAIL_RX = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
SCHEMA = "https://schema.org"

#: Names of things rather than claims about anybody. The brand and the schema's own type names (`Person`,
#: `ItemList`, …) are vocabulary, and requiring them to appear in the payload would mean adding our own product
#: name to a trader's data so a checker would let a breadcrumb through.
VOCAB = ("Openout", "BreadcrumbList", "ListItem", "ProfilePage", "Person", "ItemList", "Event", "WebSite")


def public_strings(payload: dict) -> set:
    """Every string inside the public payload, recursively. The backing set the rule below is checked against."""
    out: set = set()

    def walk(v) -> None:
        if isinstance(v, str):
            out.add(v)
        elif isinstance(v, dict):
            for k, sub in v.items():
                out.add(str(k))
                walk(sub)
        elif isinstance(v, (list, tuple)):
            for sub in v:
                walk(sub)

    walk(payload)
    return out


def unbacked(graph: Iterable[dict], payload: dict) -> list:
    """Strings the graph states that the payload does not contain — the check that keeps rule 1 honest.

    Scoped to strings rather than to the whole shape on purpose: a graph is *allowed* to re-state a field under
    a different key (`headline` for `label`), and forbidding that would make the check so annoying that somebody
    would delete it. What it forbids is a value with no source at all.
    """
    have = public_strings(payload)
    missing = []

    def walk(v, path: str, key: str = "") -> None:
        if isinstance(v, str):
            if (key.startswith("@") or key in ("eventStatus",) or v in VOCAB
                    or v.startswith("http://") or v.startswith("https://")):
                # Vocabulary, type names and URLs are navigation; the rule is about CLAIMS (a number or a
                # sentence about a trader), and requiring a URL to appear verbatim in the payload would make
                # every breadcrumb unbacked.
                return                                    # type names and status enums are vocabulary
            if v not in have:
                missing.append("%s = %r" % (path, v[:60]))
        elif isinstance(v, dict):
            for k, sub in v.items():
                walk(sub, path + "." + str(k), str(k))
        elif isinstance(v, (list, tuple)):
            for i, sub in enumerate(v):
                walk(sub, "%s[%d]" % (path, i), key)

    for g in graph:
        walk(g, "$")
    return missing


def leak_findings(graph: Iterable[dict]) -> list:
    """An address or an email anywhere in the graph. Both are always a bug: neither is ever public (§2.19)."""
    out = []

    def walk(v, path: str) -> None:
        if isinstance(v, str):
            if ADDRESS_RX.match(v.strip()):
                out.append("%s carries a wallet address" % path)
            elif EMAIL_RX.search(v):
                out.append("%s carries an email address" % path)
        elif isinstance(v, dict):
            for k, sub in v.items():
                walk(sub, path + "." + str(k))
        elif isinstance(v, (list, tuple)):
            for i, sub in enumerate(v):
                walk(sub, "%s[%d]" % (path, i))

    for g in graph:
        walk(g, "$")
    return out


# ------------------------------------------------------------------------------------------------- builders
def breadcrumb(trail: list, *, base: str = "") -> dict:
    """`trail` is [(name, url)] from the site root down. The last item is the page itself."""
    items = []
    for i, (name, url) in enumerate(trail, start=1):
        items.append({"@type": "ListItem", "position": i, "name": str(name), "item": str(url)})
    return {"@context": SCHEMA, "@type": "BreadcrumbList", "itemListElement": items}


def profile_page(*, handle: str, url: str, headline: str, notes: list, board_url: str = "") -> dict:
    """A trader's public page. `headline` and `notes` are the page's own strings, verbatim.

    `identifier` is the handle and `name` is the handle: the pseudonym is the subject of this page, and there is
    no other identity to give — which is the point of the page existing at all.
    """
    person = {"@type": "Person", "name": str(handle), "identifier": str(handle), "url": str(url)}
    page = {"@context": SCHEMA, "@type": "ProfilePage", "mainEntity": person, "url": str(url),
            "description": str(headline), "text": [str(n) for n in notes]}
    if board_url:
        page["isPartOf"] = {"@type": "WebSite", "url": str(board_url)}
    return page


def item_list(*, name: str, url: str, rows: list) -> dict:
    """A board. `rows` is [(position, label, url_or_empty)]; an anonymous row has no URL to point at."""
    items = []
    for pos, label, href in rows:
        item = {"@type": "ListItem", "position": int(pos), "name": str(label)}
        if href:
            item["url"] = str(href)
        items.append(item)
    return {"@context": SCHEMA, "@type": "ItemList", "name": str(name), "url": str(url),
            "numberOfItems": len(items), "itemListElement": items}


def market_event(*, name: str, url: str, end_date: str = "", accepting: bool = False) -> dict:
    """A market, as an `Event`, only when it has a resolution date.

    Returns `{}` when there is no date, and the caller emits the breadcrumb alone: an `Event` needs a date to be
    an event, and inventing one is the kind of lie that gets quoted back at us with our own name on it.
    """
    if not str(end_date or "").strip():
        return {}
    out = {"@context": SCHEMA, "@type": "Event", "name": str(name), "url": str(url),
           "endDate": str(end_date)}
    if accepting:
        out["eventStatus"] = SCHEMA + "/EventScheduled"
    return out


def graph(*parts) -> list:
    """The page's JSON-LD blocks, with the empty ones dropped (a market with no resolution date)."""
    return [p for p in parts if p]
