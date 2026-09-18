"""D5 · market metadata is untrusted input, because anyone can create a market.

Titles, descriptions, outcome labels and image URLs arrive from an open creation flow and are rendered in *our*
UI, next to a button that moves money. The controls here are not decoration:

* **No markup reaches the renderer, and the residue check runs after unescaping.** A single `html.unescape`
  pass over `&lt;script&gt;` produces the tag you just filtered; a loop with no cap is a CPU incident. Three
  passes, then anything still shaped like a tag is stripped and flagged, and the flag becomes a visible badge —
  silently deleting text from a market title is how you get a support ticket about a market that "says
  something else".
* **Bidirectional and zero-width characters are removed, not normalised.** `\u202e` (RLO) can render
  `polymarket.com\u202egnp.tcatnoc` as `contactnpm.polymarket.com`. This is the single cheapest
  "fake resolution source" in the book and it costs one `str.translate`.
* **Confusables are folded for *detection*, never for display.** Rewriting `е`→`e` in a title would corrupt
  legitimate Ukrainian and Greek text; showing the reader what they see while a matcher sees the ASCII form is
  how you flag `рolymarket.com` without mangling a market about Athens.
* **Images are proxied.** A hotlinked attacker-controlled URL hands them the reader's IP, user agent, referrer
  and a precise count of who looked at which market. There is no version of this where the answer is "trust the
  domain".
"""
from __future__ import annotations

import hashlib
import hmac
import html
import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit

TITLE_MAX = 140
DESC_MAX = 2000
OUTCOME_MAX = 80
MAX_OUTCOMES = 32
URL_MAX = 2048
ALLOWED_SCHEMES = ("https",)
UNSAFE_SCHEMES = ("javascript", "data", "vbscript", "file", "blob", "about")

# C0/C1 controls (except tab/newline), the format characters, the bidi isolates and overrides, and zero-width
# joiners. `unicodedata.category` catches most of them; the explicit set catches the ones that are technically
# "format" but legitimate in other scripts (soft hyphen) *only* where we strip anyway.
_STRIP_CATEGORIES = ("Cc", "Cf", "Co", "Cn")
_ALWAYS_STRIP = "\u200b\u200c\u200d\u2060\ufeff\u00ad\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"

# Cyrillic/Greek/other Latin lookalikes, enough to catch a domain, not a full Unicode confusables table (which
# would be the wrong size for a hot path and would flag legitimate text). Extend deliberately, never lazily.
CONFUSABLES = {
    "\u0430": "a", "\u0441": "c", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0443": "y", "\u0445": "x",
    "\u0455": "s", "\u0456": "i", "\u0458": "j", "\u04bb": "h", "\u0501": "d", "\u043d": "h", "\u043a": "k",
    "\u0442": "t", "\u0447": "4", "\u03bf": "o", "\u03b1": "a", "\u03b5": "e", "\u03b7": "n", "\u03b9": "i",
    "\u03ba": "k", "\u03bd": "v", "\u03c1": "p", "\u03c5": "y", "\u03c9": "w", "\u0412": "b", "\u041c": "m",
    "\u041d": "h", "\u0410": "a", "\u0415": "e", "\u041e": "o", "\u0421": "c", "\u0420": "p", "\u0423": "y",
    "\u0425": "x", "\u0422": "t",
}
_BRAND_DOMAINS = ("polymarket.com", "openout.io", "openout.app", "telegram.org", "t.me")
# Domains our own UI is allowed to imply "this is how it resolves". Anything else is a link the reader should
# see as a link, with a badge, and never as a verified source. [UNVERIFIED — confirm the real resolution-source
# set with Polymarket's docs before launch; today this list is our own allowlist, not theirs.]
TRUSTED_RESOLUTION_HOSTS = ("oracle.polymarket.com", "uma.project", "polymarket.com")

_TAGGED = re.compile(r"<[^>]*>")
_ENTITY = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]{1,8};)")
_DOMAINISH = re.compile(r"[A-Za-z0-9\u0400-\u04ff\u0370-\u03ff][A-Za-z0-9.\-\u0400-\u04ff\u0370-\u03ff]*"
                        r"\.[A-Za-z]{2,}(?:/[^\s<]*)?")
_PHISHING = (
    (re.compile(r"claim\s+(?:your|the)\s+(?:airdrop|reward|refund|prize)", re.I), "claim_airdrop"),
    (re.compile(r"connect\s+(?:your\s+)?wallet", re.I), "connect_wallet"),
    (re.compile(r"(?:send|transfer|deposit)\s+(?:funds|eth|usdc|pusd)\s+to", re.I), "send_funds_to"),
    (re.compile(r"(?:support|admin|moderator)\s*(?:@|t\.me/|telegram)", re.I), "fake_support"),
    (re.compile(r"urgent(?:ly)?\s+(?:verify|confirm|action|required)", re.I), "urgency"),
    (re.compile(r"(?:duplicate|verify|secure)\s+your\s+account", re.I), "account_verification"),
    (re.compile(r"(?:100x|guaranteed\s+(?:return|profit)|risk[- ]free\s+profit|no\s+loss)", re.I), "yield_promise"),
    (re.compile(r"(?:http://|https://)?[a-z0-9-]+\.(?:tk|ml|ga|cf|gq)\b", re.I), "free_tld_link"),
)


class _Text(HTMLParser):
    """Collect text, drop the *contents* of script/style, and remember that a tag was there at all."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.tags: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        self.tags.append(tag.lower())
        if tag.lower() in ("script", "style", "noscript", "iframe", "object", "embed"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("script", "style", "noscript", "iframe", "object", "embed") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


@dataclass(frozen=True)
class Cleaned:
    text: str
    flags: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.flags


@dataclass(frozen=True)
class SanitisedMarket:
    market_id: str
    title: Cleaned
    description: Cleaned
    outcomes: tuple[Cleaned, ...]
    image_url: str
    resolution: dict
    flags: tuple[str, ...] = ()
    badges: tuple[str, ...] = ()
    rejected_reason: str = ""

    def as_dict(self) -> dict:
        return {"market_id": self.market_id, "title": self.title.text, "title_flags": list(self.title.flags),
                "description": self.description.text, "description_flags": list(self.description.flags),
                "outcomes": [o.text for o in self.outcomes], "image_url": self.image_url,
                "resolution": self.resolution, "flags": list(self.flags), "badges": list(self.badges),
                "rejected_reason": self.rejected_reason}


def strip_invisible(text: str) -> tuple[str, bool]:
    """Remove control/format characters. Returns (text, removed_something)."""
    out = []
    removed = False
    for ch in text:
        if ch in _ALWAYS_STRIP or (unicodedata.category(ch) in _STRIP_CATEGORIES and ch not in "\t\n"):
            removed = True
            continue
        out.append(ch)
    return "".join(out), removed


def fold_confusables(text: str) -> str:
    """ASCII view of `text` for *matching*. Never for display, never stored."""
    return "".join(CONFUSABLES.get(c, c) for c in text)


def mixed_script(text: str) -> bool:
    """True when Latin letters and a confusable-capable script share a run — the shape of a lookalike domain."""
    saw_latin = saw_other = False
    for c in text:
        if ("A" <= c <= "z") and c.isalpha():
            saw_latin = True
        elif ("\u0400" <= c <= "\u04ff") or ("\u0370" <= c <= "\u03ff"):
            saw_other = True
        if saw_latin and saw_other:
            return True
    return False


def clean_text(raw: str, *, limit: int, field_name: str = "text") -> Cleaned:
    """The single entry point every externally-authored string goes through."""
    flags: list[str] = []
    s = "" if raw is None else str(raw)
    if len(s) > limit * 4:
        flags.append("oversized_input")
        s = s[: limit * 4]
    s, removed = strip_invisible(s)
    if removed:
        flags.append("invisible_characters")
    # Unescape, then look for markup again: `&lt;script&gt;` is the first thing a filter that runs once gets.
    for _ in range(3):
        nxt = html.unescape(s)
        if nxt == s:
            break
        s = nxt
        flags.append("nested_markup")
    if _TAGGED.search(s) or "<" in s or ">" in s:
        flags.append("markup")
    parser = _Text()
    try:
        parser.feed(s)
        parser.close()
    except Exception:                                       # noqa: BLE001 - malformed markup is the input
        parser.parts = [_TAGGED.sub(" ", s)]                 # we were sent, and we still have to render text.
    s = "".join(parser.parts)
    if parser.tags:
        flags.append("had_tags:%d" % len(set(parser.tags)))
    s = re.sub(r"[ \t]+", " ", s).replace("\n\n\n", "\n\n").strip()
    for pattern, name in _PHISHING:
        if pattern.search(s):
            flags.append(name)
    hosts = {h.lower() for h in _DOMAINISH.findall(s)}
    for h in hosts:
        folded = fold_confusables(h)
        if h not in _BRAND_DOMAINS and folded in _BRAND_DOMAINS:
            flags.append("impersonates:%s" % folded)
        if mixed_script(h):
            flags.append("mixed_script_domain")
    if len(s) > limit:
        flags.append("truncated_to_%d" % limit)
        s = s[:limit].rstrip() + "…"
    return Cleaned(s, tuple(dict.fromkeys(flags)))


def safe_url(u: str, *, what: str = "link") -> tuple[bool, str, str]:
    """(ok, reason, host). https only, no credentials in the URL, no scheme inside the path."""
    s = (u or "").strip()
    if not s:
        return False, "empty", ""
    if len(s) > URL_MAX:
        return False, "too_long", ""
    low = s.lower()
    for sch in UNSAFE_SCHEMES:
        if low.startswith(sch + ":") or ("%s:" % sch) in low[:40]:
            return False, "scheme:%s" % sch, ""
    parts = urlsplit(s if "//" in s or ":" in s else "https://" + s)
    if parts.scheme not in ALLOWED_SCHEMES:
        return False, "scheme:%s" % (parts.scheme or "none"), ""
    if parts.username or parts.password:
        return False, "credentials_in_url", ""
    host = (parts.hostname or "").lower()
    if not host or "." not in host:
        return False, "no_host", ""
    return True, "ok", host


def proxy_image_url(u: str, *, base: str, secret: str) -> dict:
    """Our proxy path for an external image. The origin lives in a signed table the proxy owns; nothing here
    embeds the attacker's URL in a way the browser would fetch directly, and nothing here trusts it not to."""
    ok, reason, host = safe_url(u, what="image")
    if not ok:
        return {"ok": False, "reason": reason, "url": "", "host": host}
    canon = "%s%s" % (host, parts_path(u))
    if not secret:
        # Refuse to mint an *unsigned* proxy path: an open proxy that fetches any URL is an SSRF with our
        # source IP, which is worse than the hotlink it replaced.
        return {"ok": False, "reason": "no_proxy_secret", "url": "", "host": host}
    d = hmac.new(secret.encode(), canon.encode(), hashlib.sha256).hexdigest()
    return {"ok": True, "reason": "proxied", "host": host,
            "url": "%s/i/%s/%s" % (base.rstrip("/"), d[:2], d)}


def parts_path(u: str) -> str:
    p = urlsplit(u if "//" in u else "https://" + u)
    return p.path or "/"


def resolution_source(text: str) -> dict:
    """Trusted or not, and why. The UI rule that follows from this: an untrusted source is rendered as a link
    with a badge, never as a fact, and it can never be broadcast to the alert channel."""
    s = (text or "").strip()
    if not s:
        return {"trusted": False, "reason": "no_source", "host": "", "display": ""}
    ok, reason, host = safe_url(s, what="resolution")
    if not ok:
        # Plain text ("UMA optimistic oracle, resolved 2026-09-12") is legitimate; a *link* is what we judge.
        if "<" in s or "http" in s.lower():
            return {"trusted": False, "reason": reason, "host": "", "display": clean_text(s, limit=DESC_MAX).text}
        return {"trusted": False, "reason": "not_a_url", "host": "", "display": clean_text(s, limit=DESC_MAX).text}
    trusted = host in TRUSTED_RESOLUTION_HOSTS or any(host.endswith("." + t) for t in TRUSTED_RESOLUTION_HOSTS)
    folded = fold_confusables(host)
    if not trusted and folded != host and any(folded.endswith("." + t) for t in TRUSTED_RESOLUTION_HOSTS):
        return {"trusted": False, "reason": "lookalike_of_trusted", "host": host, "display": s}
    return {"trusted": trusted, "reason": "allowlisted" if trusted else "unknown_host", "host": host,
            "display": s}


def market_metadata(payload: dict, *, image_base: str = "", image_secret: str = "") -> SanitisedMarket:
    """Everything about a market, from the schema-in-our-heads version to the version the UI may render."""
    p = payload or {}
    title = clean_text(str(p.get("question") or p.get("title") or ""), limit=TITLE_MAX, field_name="title")
    desc = clean_text(str(p.get("description") or ""), limit=DESC_MAX, field_name="description")
    raw_outcomes = p.get("outcomes") or []
    if isinstance(raw_outcomes, str):
        try:
            import json as _json
            raw_outcomes = _json.loads(raw_outcomes)
        except Exception:                                   # noqa: BLE001 - a string stays a string
            raw_outcomes = [raw_outcomes]
    outcomes = tuple(clean_text(str(o), limit=OUTCOME_MAX, field_name="outcome")
                     for o in list(raw_outcomes)[:MAX_OUTCOMES])
    flags: list[str] = list(title.flags) + ["desc:%s" % f for f in desc.flags]
    if len(raw_outcomes or []) > MAX_OUTCOMES:
        flags.append("too_many_outcomes")
    img = proxy_image_url(str(p.get("image") or p.get("image_url") or ""), base=image_base, secret=image_secret)
    if not img["ok"]:
        flags.append("image:%s" % img["reason"])
    res = resolution_source(str(p.get("resolutionSource") or p.get("resolution_source") or ""))
    if not res["trusted"]:
        flags.append("resolution:%s" % res["reason"])
    badges: list[str] = []
    if any(f in flags for f in ("markup", "nested_markup", "invisible_characters")):
        badges.append("formatting stripped")
    if not res["trusted"]:
        badges.append("unverified resolution source")
    if not img["ok"]:
        badges.append("no image")
    # A refusal is different from a warning: `flags` means "render it, with a badge", and this means "there is
    # nothing here to render". An empty market is how a feed bug becomes a screen full of blank cards that a
    # user reads as "Polymarket is down", so the caller gets one boolean instead of a list to interpret.
    rejected = ""
    if not title.text.strip():
        rejected = "no_title"
    elif len([o for o in outcomes if o.text.strip()]) < 2:
        rejected = "no_outcomes"
    return SanitisedMarket(market_id=str(p.get("id") or p.get("market_id") or ""), title=title, description=desc,
                           outcomes=outcomes, image_url=img["url"], resolution=res,
                           rejected_reason=rejected,
                           flags=tuple(dict.fromkeys(flags)), badges=tuple(badges))


def alert_line(text: str) -> Cleaned:
    """Broadcast copy: the same rules, a tighter cap, and no link that is not https. A message pushed to
    40,000 people is the highest-leverage phishing surface we own.

    The one extra rule versus a market title is that the output cannot contain a newline. Our alert pipeline
    renders this into a Telegram message, a log line and an email subject; in two of those three a `\n` ends
    the field, which is exactly where a market author would put a second, different sentence.
    """
    c = clean_text(text, limit=320, field_name="alert")
    flat = re.sub(r"\s+", " ", c.text).strip()
    flags = tuple(dict.fromkeys(list(c.flags) + (["newline_in_alert"] if flat != c.text else [])))
    return Cleaned(flat, flags)
