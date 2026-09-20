"""P11 · D6: pages that exist to be shared, and the rules that keep them honest.

The kit asks for three server-rendered public pages (`/trader/<handle>`, `/market/<slug>`,
`/leaderboard/<board>`), dynamic OG images, structured data "where it genuinely applies", and rate limiting.

What makes this a deliverable rather than a template job is that **publishing a page changes the status of
facts that were internal**:

* A **handle** stops being decoration on a board row and becomes an address. `/trader/<handle>` must resolve to
  the same wallet its pseudonym does — D4 promised exactly that in its served listing copy — which means the
  handle namespace needs to be unique in the *database*, not merely checked by the route that writes it.
* A **market slug** stops being a column nothing read and becomes a canonical URL, with the same consequence.
* An **anonymous reader** stops being a non-event and becomes a load source with no session to throttle. Every
  other surface in this system is behind a credential; these three are the part a crawler can reach.

Three rules are load-bearing across all four modules here, and each one is a way this feature fails in
production rather than in review:

1. **The public payload is the whole truth on both sides of the boundary.** The page, the OG image, the JSON-LD
   and the sitemap are all built from *one* dict — the same one the API serves — so the image cannot show a
   number the page hides, and the structured data cannot carry a field the page does not render. `cards.py` and
   `structured.py` both refuse a payload that contains an address, and both are asserted against the same
   fixture, because "the page is sanitised" is not a property any single module can hold on its own.
2. **Digits we publish today are numbers somebody will quote tomorrow.** A page whose headline is derived from
   nine settled markets is a page a journalist can screenshot; so the sample gate, the provisional label and
   the drawdown travel with the numbers into the card and into the structured data, not only into the HTML body.
   A share card that quietly drops "provisional" is the single most damaging thing this deliverable could ship.
3. **The budget is per subject and generous, the block is per scope and bounded.** A carrier NAT exit is
   thousands of readers; a limit tight enough to stop a scraper behind one is a denial of service for everyone
   behind it. So the limit protects *the kind* (nobody gets to walk the whole sitemap every minute) and the
   block is what protects the pages from a specific scraper that stays politely under it.

The three public page kinds are also the first surfaces in the product where **we do not know who is asking**,
so the only identity we keep is a salted digest of an address, kept for one window and never joined to
anything — the same rule the referral signals already follow, and implemented by the same function.
"""
from __future__ import annotations

from . import budget, cards, structured, urls

__all__ = ["budget", "cards", "structured", "urls"]
