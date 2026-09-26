"""The webhook transport: the only place in this product where a *user* chooses an address we open a socket to.

Everything else that talks to a network talks to a constant (`packages/polygm_core/venues`, the Telegram client,
the ingest clients), which is why P14's SSRF section could only record this channel as *latent*: a stored URL with
no transport is not a vulnerability, it is a promise to build one carefully. This is that transport, and the guard
is not a feature of it — it is the first thing that runs.

**The threat, stated as the thing being prevented.** A Pro user saves a rule whose webhook URL is
`https://169.254.169.254/latest/meta-data/iam/security-credentials/`. If we fetch it, *our* process — the one with
the database password, the KMS handle and the venue keys — reads the cloud metadata service and hands the answer
to the user's alert history. The same trick reaches `127.0.0.1` (our own admin routes, which are bound to loopback
and therefore have no request-level authorisation at all), `10.0.0.0/8` (the database), and a private-range
receiver that then redirects to either. None of those are reachable by the user directly; all of them are reachable
*through* us. This is why the refusal has to be a decision about the address, made before the connection, rather
than a timeout after it.

**Where each half of the check lives, and why they differ.**

  * **At save time** (`signals.console.validate_alert_payload`): the URL's *shape* — https only, no userinfo, a
    real host, and a literal address that is not internal. This is deliberately DNS-free: a user saving a rule must
    not fail because our resolver is having a bad minute, and a hostname's current resolution is not a property of
    the rule anyway. What save time guarantees is that nothing *already* internal can be stored.
  * **At send time** (`deliver` below): everything above, **plus** resolution — every address the host resolves to
    must be public, and no resolution at all is a refusal (fail closed: an unresolvable host is not a safe host).
    A hostname that resolved publicly when the rule was saved and privately when it fired is exactly the attack,
    so the check is repeated on every send and on every redirect hop.

**Redirects are followed, but each hop is a new decision.** Refusing redirects outright is simpler and some
products do it; it is also wrong for the ordinary case, because a provider moving `http://` to `https://` or
`example.com` to `www.example.com` would silently break every rule. So instead: at most `max_redirects` hops, the
full guard re-run on each `Location`, and a downgrade to http refused by the scheme rule. A hop to
`127.0.0.1` is refused *at the hop*, which the tests assert by counting connections rather than by trusting the
final status.

**What a delivery carries.** `X-PolyGM-Signature: t=<unix ms>,v1=<hex>` — HMAC-SHA256 over `"<t>.<body>"`, the
same construction as the payment provider's, because a receiver's verification code should be something a
developer has already written once. `X-PolyGM-Delivery` is `fanout.idempotency_key(signal, channel)`, so a
redelivery after a visibility timeout is recognisable as the same alert and the receiver can drop it — that is
the at-least-once contract the fan-out module documents, honoured at the last hop. `X-PolyGM-Timestamp` repeats
`t` in plain form for receivers that want it without parsing a header they do not understand.

**What it does not do.** No retries — `fanout.plan` scheduled this attempt and `fanout.fail` will schedule the
next one with backoff; a transport that retries is a transport that double-books the queue's own budget. No
body echo: the response body is a hostile input as far as we are concerned, so we read a bounded prefix, hash it
for the delivery record and never render it. No floats anywhere.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

SCHEME_ALLOWED = ("https",)
#: https on 443, or any unprivileged port. A self-hosted receiver on 8443 is a legitimate Pro customer; asking
#: for 22, 25, 2375 or 6379 as the *destination* is not a webhook, it is a port scan, and those live below 1024.
#: The dial is one constant so an operator can widen it deliberately rather than by accident.
PORT_ALLOWED = (443,)
MIN_UNPRIVILEGED_PORT = 1024
MAX_URL_LEN = 2_048
#: 100.64.0.0/10 — read explicitly because `is_private` is False for it on this interpreter while `is_global` is
#: also False, and the message a refusal carries should name the range rather than the absence of a property.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
CONNECT_TIMEOUT_S = 5.0
MAX_RESPONSE_BYTES = 8_192
MAX_REDIRECTS = 3
REDIRECT_CODES = (301, 302, 303, 307, 308)
USER_AGENT = "PolyGM-Webhook/1 (+https://polygm.app/docs/webhooks)"


class TargetRefused(ValueError):
    """The URL is not an address we are willing to open a socket to. Carries a code, because the code is what a
    test asserts on and what the API returns to the user — 'invalid URL' would tell a Pro customer nothing about
    which half of their URL we objected to."""

    def __init__(self, code: str, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def _ip_blocked(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> str:
    """Why this address is not ours to connect to, or "" if it is fine. Order matters only for the message."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        # `::ffff:169.254.169.254` is the metadata service wearing a v6 costume; classify it as what it maps to.
        return _ip_blocked(ip.ipv4_mapped)
    if ip.is_loopback:
        return "loopback: our own admin routes are bound here and carry no request-level auth"
    if ip.is_link_local:
        return "link-local: 169.254.0.0/16 is the cloud metadata service (IMDS), and fe80::/10 is the same idea"
    if ip.is_private:
        return "private: RFC1918/ULA ranges are where the database and the internal services live"
    if ip.is_unspecified:
        return "unspecified: 0.0.0.0 is every interface at once, not a destination"
    if ip.is_multicast:
        return "multicast"
    if ip in _CGNAT:
        # Named rather than left to `is_global`: a filter whose message is "not global" tells an operator nothing
        # about *why* a customer's URL was refused, and CGNAT is the range most likely to be a real address in
        # somebody's internal network.
        return "carrier-grade NAT: not routable from the public internet"
    if ip.is_reserved:
        return "reserved by IANA"
    if not ip.is_global:
        # The catch-all, and deliberately the last word: anything Python does not consider globally routable —
        # benchmarking ranges, documentation ranges, protocol assignments — is not a webhook destination either.
        # The alternative (enumerating the ranges we happen to know about) is a filter that ages badly.
        return "not a globally routable address"
    return ""


def _host_issues(host: str) -> list[str]:
    """The refusals that need no DNS. Kept separate because save time runs *only* these."""
    out: list[str] = []
    if not host:
        out.append("no host")
        return out
    if any(ch in host for ch in " \t\r\n\x00"):
        out.append("whitespace or NUL in the host")
    if "%" in host:
        # `https://ex%61mple.com/` is not a host we should be decoding: percent-decoding a hostname is how a
        # filter that checks the raw string and a client that checks the decoded one disagree about the address.
        out.append("percent-escape in the host")
    if host.startswith("."):
        out.append("leading dot in the host")
    if host.endswith(".."):
        out.append("more than one trailing dot in the host")
    bare = host.strip(".")
    if bare.isdigit():
        # `https://2130706433/` — a decimal integer is a legal way to write 127.0.0.1, and a filter that only
        # looks for dotted quads walks straight past it.
        out.append("a bare integer host is an alternate notation for an address")
    elif bare.lower().startswith("0x") and all(c in "0123456789abcdefx" for c in bare.lower()):
        out.append("a hex-integer host is an alternate notation for an address")
    return out


def _classify(host: str, resolve) -> dict:
    """The address half of the guard. `resolve=None` means syntax and literal addresses only."""
    bare = host.strip(".")
    literal = None
    try:
        literal = ipaddress.ip_address(bare)
    except ValueError:
        literal = None
    if literal is not None:
        why = _ip_blocked(literal)
        if why:
            raise TargetRefused("TARGET_NOT_PUBLIC", "that webhook address is not public", detail=why)
        return {"kind": "literal", "addresses": [str(literal)]}
    if resolve is None:
        return {"kind": "hostname", "addresses": []}
    try:
        infos = resolve(bare)
    except Exception as exc:                                                     # noqa: BLE001
        raise TargetRefused("TARGET_UNRESOLVABLE", "that webhook host could not be resolved",
                            detail="%s: %s" % (type(exc).__name__, str(exc)[:120])) from exc
    addrs = sorted({str(a[4][0]) for a in infos if a and len(a) > 4 and a[4]})
    if not addrs:
        # Fail closed. An unresolvable host is not a safe host, it is a host we cannot make a claim about.
        raise TargetRefused("TARGET_UNRESOLVABLE", "that webhook host resolved to no address",
                            detail="no A/AAAA record")
    for raw in addrs:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            raise TargetRefused("TARGET_UNRESOLVABLE", "that webhook host resolved to something that is not an "
                                "address", detail=raw[:64]) from None
        why = _ip_blocked(ip)
        if why:
            raise TargetRefused("TARGET_NOT_PUBLIC", "that webhook host resolves to a private address",
                                detail="%s -> %s (%s)" % (bare, ip, why))
    return {"kind": "hostname", "addresses": addrs}


def check_target(url: str, *, resolve=None) -> dict:
    """The guard. Returns the parsed target, or raises `TargetRefused` with a code a caller can render.

    `resolve` is injected rather than called directly so that (a) the transport's own decision is testable
    without a network, and (b) the tests can prove that no socket was opened for a refusal, which is the
    difference between "we checked and refused" and "we connected and then complained".
    """
    raw = (url or "").strip()
    if not raw:
        raise TargetRefused("TARGET_MISSING", "a webhook rule needs a URL to deliver to")
    if len(raw) > MAX_URL_LEN:
        raise TargetRefused("TARGET_TOO_LONG", "that URL is longer than %d characters" % MAX_URL_LEN)
    if any(ch in raw for ch in "\r\n\t\x00"):
        raise TargetRefused("TARGET_MALFORMED", "that URL contains a control character",
                            detail="header injection is how a URL becomes two requests")
    # `urlsplit` rather than a regex: one parser, not two, so there is no gap between what we validate and what
    # the client uses — the classic SSRF bypass is exactly a disagreement between those two.
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme.lower() not in SCHEME_ALLOWED:
        raise TargetRefused("TARGET_SCHEME", "a webhook must be https",
                            detail="scheme was %r; file:, gopher:, dict: and http: are not delivery mechanisms"
                                   % (parts.scheme or ""))
    if parts.username or parts.password:
        raise TargetRefused("TARGET_USERINFO", "a webhook URL must not carry credentials",
                            detail="userinfo is ignored by some clients and sent by others, and it hides the "
                                   "real host from a human reading the rule")
    host = parts.hostname or ""
    issues = _host_issues(host)
    if issues:
        raise TargetRefused("TARGET_MALFORMED", "that webhook URL is not a valid address", detail="; ".join(issues))
    port = parts.port or 443
    if port not in PORT_ALLOWED and port < MIN_UNPRIVILEGED_PORT:
        raise TargetRefused("TARGET_PORT", "a webhook may use 443 or an unprivileged port",
                            detail="port %d is in the privileged range" % port)
    if not 1 <= port <= 65_535:
        raise TargetRefused("TARGET_PORT", "that port number is not a port", detail=str(port))
    checked = _classify(host, resolve)
    return {"url": urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "/",
                                           parts.query, "")),
            "host": host, "port": port, **checked}


def signature(secret: str, body: bytes, *, at_ms: int) -> str:
    """`t=<ms>,v1=<hex>`: HMAC-SHA256 over `"<ms>.<body>"`, so a replayed body cannot be re-timestamped by an
    attacker who has the body but not the secret."""
    mac = hmac.new(secret.encode(), ("%d." % at_ms).encode() + body, hashlib.sha256).hexdigest()
    return "t=%d,v1=%s" % (at_ms, mac)


def _open(url: str, body: bytes, headers: dict, timeout: float):
    """The one place a socket is opened. `urllib` rather than a client library because the request is a POST
    with headers and nothing else, and a dependency is a thing that has to be patched."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=timeout)                          # noqa: S310 - guarded above


def deliver(*, url: str, payload: dict, secret: str, delivery_id: str, at_ms: int | None = None,
            resolve=None, opener=None, max_redirects: int = MAX_REDIRECTS,
            timeout_s: float = CONNECT_TIMEOUT_S) -> dict:
    """One delivery attempt. Returns a record; raises `TargetRefused` for anything the guard refuses.

    The caller (the ingest worker) turns a refusal into a *dead* delivery without retry — a URL that resolves
    into a private range is not going to start being public on the fourth attempt — and a transport error into
    `fanout.fail`, which schedules the backoff. Those are different outcomes on purpose.
    """
    at_ms = int(time.time() * 1000) if at_ms is None else int(at_ms)
    resolve = resolve or (lambda host: socket.getaddrinfo(host, None))
    opener = opener or _open
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    hops: list[dict] = []
    target = check_target(url, resolve=resolve)
    hops.append({"url": target["url"], "host": target["host"], "kind": target["kind"]})
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT,
               "X-PolyGM-Signature": signature(secret, body, at_ms=at_ms),
               "X-PolyGM-Timestamp": str(at_ms), "X-PolyGM-Delivery": delivery_id}
    for hop_index in range(max_redirects + 1):
        try:
            resp = opener(target["url"], body, headers, timeout_s)
        except Exception as exc:                                                 # noqa: BLE001
            # The branch reads the two attributes it needs (`code`, `headers`) rather than requiring one
            # library's exception type, for a reason that is about correctness rather than convenience: an
            # HTTP status is a *reply*, not a transport error, and a client that answered through a different
            # exception class must not be mistaken for one. It is also what lets the tests present a redirect
            # without a live server, which is the only way the redirect refusal can be asserted at the hop.
            code = getattr(exc, "code", None)
            if code in REDIRECT_CODES:
                location = (getattr(exc, "headers", None) or {}).get("Location") or ""
                if not location:
                    raise TargetRefused("TARGET_REDIRECT", "the webhook answered a redirect with no Location",
                                        detail="status %d" % code) from exc
                if hop_index >= max_redirects:
                    raise TargetRefused("TARGET_REDIRECT", "too many redirects",
                                        detail="%d hop(s)" % (hop_index + 1)) from exc
                # A relative Location is resolved against the hop we just made, then re-guarded in full: the
                # point of this branch is that the *next* address is checked, not the first one.
                target = check_target(urllib.parse.urljoin(target["url"], location), resolve=resolve)
                hops.append({"url": target["url"], "host": target["host"], "kind": target["kind"],
                             "redirectedFrom": hops[-1]["url"]})
                continue
            if code is not None:
                return {"ok": False, "status": int(code), "hops": hops, "bytes": 0,
                        "bodySha256": None, "refused": None}
            return {"ok": False, "status": None, "hops": hops, "bytes": 0, "bodySha256": None,
                    "error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}
        status = int(getattr(resp, "status", 0) or 0)
        if status in REDIRECT_CODES:
            # A client that *returns* the 3xx instead of raising it (any library configured with redirects off)
            # must take the same path as one that raises, or the hop guard is only as good as the client's
            # exception style. Returning it silently here would mean a `Location` nobody guarded — the response
            # is not an answer, it is the next address, and it gets the same treatment as the raised shape.
            location = (getattr(resp, "headers", None) or {}).get("Location") or ""
            if not location:
                raise TargetRefused("TARGET_REDIRECT", "the webhook answered a redirect with no Location",
                                    detail="status %d" % status)
            if hop_index >= max_redirects:
                raise TargetRefused("TARGET_REDIRECT", "too many redirects", detail="%d hop(s)" % (hop_index + 1))
            target = check_target(urllib.parse.urljoin(target["url"], location), resolve=resolve)
            hops.append({"url": target["url"], "host": target["host"], "kind": target["kind"],
                         "redirectedFrom": hops[-1]["url"]})
            continue
        raw = resp.read(MAX_RESPONSE_BYTES)
        return {"ok": 200 <= status < 300, "status": status, "hops": hops, "bytes": len(raw),
                "bodySha256": hashlib.sha256(raw).hexdigest()[:32], "refused": None}
    raise TargetRefused("TARGET_REDIRECT", "too many redirects", detail="%d hop(s)" % len(hops))


def send_claimed(claim: dict, *, payload: dict, secret: str, resolve=None, opener=None,
                 now_ms: int, max_attempts: int = 5) -> dict:
    """A claimed `alert_deliveries` row in, the row to store out. The queue half of the transport.

    The one decision worth stating: **a refused target is dead, not retry.** `fanout.fail` exists to schedule a
    backoff for failures that might not repeat — a provider timing out, a 503, a dropped connection. A URL that
    resolves into `10.0.0.0/8` is not going to stop being private on the fourth attempt, so retrying it burns four
    more tries of a *deliberately blocked* address and, worse, keeps a hostile rule alive in the queue. The row
    lands in `dead` with `dead_reason` naming the refusal code, which is also what a support thread about "my
    webhook never fired" is answered from.
    """
    from . import fanout

    try:
        result = deliver(url=str(claim.get("target") or ""), payload=payload, secret=secret,
                         delivery_id=str(claim.get("idempotency_key") or ""), at_ms=now_ms,
                         resolve=resolve, opener=opener)
    except TargetRefused as exc:
        dead = dict(claim, status=fanout.STATUS_DEAD, attempts=int(claim.get("attempts") or 0) + 1,
                    dead_reason="refused:%s" % exc.code, refused_code=exc.code,
                    refused_detail=(exc.detail or exc.message)[:160], sent_ms=None)
        return dead
    if result["ok"]:
        return fanout.ack(claim, now_ms=now_ms)
    return fanout.fail(claim, now_ms=now_ms, max_attempts=max_attempts,
                       error="http %s" % result["status"] if result["status"] else str(result.get("error", "")))
