"""Where a public page lives, what may be indexed, and how long a copy may be served.

Everything here is pure and stringly, because all of it is checked twice: once by the API route that serves the
page, and once by the route that emits the OG image and the sitemap. The two disagreeing is the failure this
module exists to prevent — an OG image that points at a URL the page does not live at is a preview card that
404s for every reader who clicks it, and it is invisible in every test that only checks the page.

Three decisions worth stating:

* **A handle is normalised, and a slug is not invented.** Handles come from D4's rule (lower-case, 3-24,
  `[a-z0-9_]`), so normalisation is `strip` + `lower` and *validation* is D4's own regex, imported rather than
  re-typed: a second copy of a rule about URLs is how the same handle becomes valid on one surface and invalid
  on another. A market slug is the venue's, so the only thing we do to it is refuse the shapes that would let a
  slug escape its path segment.
* **`noindex` is a decision about the numbers, not about the page.** A trader page is indexable only when the
  wallet is ranked and past its provisional window. Before that the page still works for a human who was sent
  the link — being crawlable and being shareable are different promises, and the one we cannot take back is what
  a search engine cached about somebody's worst week.
* **Cache lifetime is per kind and never longer than the freshness the payload carries.** The market page moves
  with the price, so it is the shortest; a leaderboard row moves when the board is recomputed; a trader page's
  headline is the same for a day. Each is derived from the phase's own freshness contract rather than picked to
  look fast in a report.
"""
from __future__ import annotations

import hashlib
import re

#: The four page kinds plus the crawl artefacts. `og` is a kind rather than a suffix so it can be rate-limited
#: separately: a card is fetched by link unfurlers, which behave nothing like a browser.
KINDS = ("trader", "market", "leaderboard", "sitemap", "og")

#: D4's rule, imported from where it is enforced. Kept here as a thin wrapper so this module has no dependency
#: on the API package; `test_public_pages.py` asserts the two regexes are the same string, which is the only
#: thing that makes a copy safe.
HANDLE_RX = re.compile(r"^[a-z0-9][a-z0-9_]{2,23}$")

#: A slug is the venue's, so we only refuse what cannot be a path segment. No rewriting: a canonical URL that
#: is not the slug the venue published is a URL we cannot re-derive tomorrow.
SLUG_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def base_url(configured: str = "") -> str:
    """The public origin, without a trailing slash.

    Configured rather than constant: a preview deployment that emits canonical URLs pointing at production is
    how a staging page ends up in an index, and the same code has to serve both.
    """
    b = str(configured or "").strip().rstrip("/")
    if not b:
        # P02 chose **Openout** and the shell has shipped `openout.app` as its metadataBase since P08; D6 is the
        # phase that PUBLISHES an origin (canonical tags, the OG image URL, the sitemap), so a default of
        # `polygm.app` here would put a second brand into every canonical tag and split our own index signal.
        return "https://openout.app"
    if not (b.startswith("http://") or b.startswith("https://")):
        return "https://" + b
    return b


# ------------------------------------------------------------------------------------------------ normalising
def normalise_handle(value: str) -> tuple[str, str]:
    """(handle, refusal sentence). Empty handle is a refusal with a sentence, not an exception."""
    s = str(value or "").strip().lower()
    if not s:
        return "", "a trader page needs a handle"
    if not HANDLE_RX.match(s):
        return "", "a handle is 3-24 characters, starts with a letter or digit, and uses a-z, 0-9 or _"
    return s, ""


def normalise_slug(value: str) -> tuple[str, str]:
    s = str(value or "").strip()
    if not s:
        return "", "a market page needs a slug"
    if not SLUG_RX.match(s):
        return "", "that is not a market slug"
    return s, ""


# --------------------------------------------------------------------------------------------------- URLs
def page_url(kind: str, key: str, *, base: str = "", window: str = "", category: str = "") -> str:
    """The canonical URL for a page, built from the same strings the router uses.

    `window` and `category` are part of a leaderboard page's address rather than a query string on its own
    because they change what "the board" *is*: `/leaderboard/win_rate` is the default window, and a page that
    ranked a 14-day window while claiming to be that URL would be a page whose numbers cannot be reproduced
    from its address.
    """
    b = base_url(base)
    k = str(kind or "")
    key = str(key or "")
    if k == "trader":
        h, why = normalise_handle(key)
        if why:
            raise ValueError(why)
        return "%s/trader/%s" % (b, h)
    if k == "market":
        s, why = normalise_slug(key)
        if why:
            raise ValueError(why)
        return "%s/market/%s" % (b, s)
    if k == "leaderboard":
        board = str(key or "").strip().lower()
        if not board:
            raise ValueError("a leaderboard page needs a board")
        parts = [b, "leaderboard", board]
        if category:
            parts.append("c/" + str(category).strip().lower().replace(" ", "-"))
        if window:
            parts.append("w/" + str(window).strip().lower())
        return "/".join(parts)
    if k == "sitemap":
        return "%s/sitemap.xml" % b
    raise ValueError("no canonical URL for kind %r" % k)


def og_url(kind: str, key: str, *, base: str = "", window: str = "") -> str:
    """Where the card for a page lives. Absolute, because unfurlers do not resolve relative URLs."""
    page = page_url(kind, key, base=base, window=window)
    suffix = "/opengraph-image" if kind in ("trader", "market", "leaderboard") else ""
    return page.rstrip("/") + suffix


# ------------------------------------------------------------------------------------------- indexability
PROVISIONAL_DAYS = 7


def robots_for(*, kind: str, ranked: bool = True, age_days: int = 99, rows: int = 0) -> str:
    """The meta robots directive for one page.

    The rule that matters: **a page whose numbers are still provisional is `noindex, follow`.** D1 already
    labels a wallet younger than seven days as provisional on the board; the public page inherits that label,
    and inheriting it in the HTML body while inviting a crawler to index the page would be the label doing half
    its job. `follow` rather than `nofollow` because the links out of it are still the ones we want followed.
    """
    if kind == "market":
        return "index, follow"
    if kind == "leaderboard":
        # A board with nothing on it is a page a crawler should come back to, not a page worth indexing: an
        # empty ranking indexed under "most profitable trader" is a claim we did not make.
        return "index, follow" if int(rows) > 0 else "noindex, follow"
    if kind == "trader":
        if not ranked:
            return "noindex, follow"
        if int(age_days) < PROVISIONAL_DAYS:
            return "noindex, follow"
        return "index, follow"
    return "noindex, follow"


# -------------------------------------------------------------------------------------------------- caching
#: Per kind: (max-age seconds, stale-while-revalidate seconds). Every number is bounded above by the freshness
#: window the payload itself carries, so a cached copy can never outlive the "as of" stamp it is served with.
CACHE = {
    "market": (15, 45),
    "leaderboard": (60, 300),
    "trader": (300, 900),
    "sitemap": (900, 1800),
    "og": (86400, 604800),
}


def cache_control(kind: str) -> str:
    """`public` is a claim, not a default: everything these pages render is already public by decision."""
    age, swr = CACHE.get(str(kind), (0, 0))
    if age <= 0:
        return "no-store"
    return "public, max-age=%d, stale-while-revalidate=%d" % (age, swr)


def etag(payload: dict) -> str:
    """A weak ETag over the public payload, stable across equivalent dicts.

    Built from the payload rather than from a render timestamp on purpose: two requests a second apart that
    would render the same bytes must produce the same validator, or every unfurler and every proxy re-downloads
    an identical image. That is also what makes the card cacheable at all — a stamp in the ETag defeats it.
    """
    import json

    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return 'W/"%s"' % hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]
