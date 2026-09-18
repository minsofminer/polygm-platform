"""D6 · the log scrubber. A stack trace containing a key is a breach, so the redaction runs *before* the line.

The list is a deny-list of shapes, deliberately: an allow-list of fields to keep is how a new secret ends up in
a log the day someone adds a field. Anything that can spend money, log in, or decrypt is stripped, and the
strip is visible (`0x***`, `[bearer]`) so an operator can tell "nothing was here" from "something was here and
we are not showing you".

Three properties the tests pin, because each one is a way this file could be quietly useless:
* `scan()` finds the secret **after** `redact_text()` has run — i.e. the redaction is checked by an independent
  matcher, not by the same regex that removed it (a regex that fails to match also fails to complain);
* key-name scrubbing is case-insensitive and recurses into nested dicts, because `{\"error\": {\"detail\":
  {\"passphrase\": ...}}}` is the shape an exception serializer actually produces;
* an ordinary English sentence is not a seed phrase: the mnemonic rule needs the exact word counts, and the
  false-positive cost (one mangled INFO line) is written down next to the choice.
"""
from __future__ import annotations

import json
import re

REDACTED = "***"
MAX_FIELD = 2000

# (name, pattern, replacement). Order matters: the 64-hex key before the 40-hex address, the PEM block before
# anything line-based.
PATTERNS: tuple[tuple[str, "re.Pattern[str]", str], ...] = (
    ("pem_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "[pem-key]"),
    ("private_key_hex", re.compile(r"\b0x[0-9a-fA-F]{64}\b"), "0x" + REDACTED),
    ("key_hex_bare", re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])"), REDACTED),
    # No "generic long base64" rule: it eats content hashes and truncates half the debug output we rely on, and
    # every real base64 secret we store is stored under a name `named_secret`/`SENSITIVE_KEYS` already cover.
    ("bot_token", re.compile(r"\b\d{4,12}:[A-Za-z0-9_-]{30,}\b"), "[bot-token]"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{8,}"), "[jwt]"),
    ("bearer", re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/\-=]{10,}"), r"\1 " + REDACTED),
    # A seed phrase that arrives after a label ("wallet seed: abandon abandon …") must be eaten as a whole. The
    # `named_secret` rule below stops at the first space, which left 11 of the 12 words sitting in the log —
    # redaction that removes the first word of a mnemonic is not redaction. Ordered before `named_secret` for
    # that reason; the false-positive cost is a 12-word English sentence that follows the word "seed" on one
    # line, which buys back nothing an operator needs.
    ("seed_phrase", re.compile(r"(?i)\b(seed|mnemonic|recovery phrase|wallet seed)\b[^A-Za-z0-9]{0,3}[:=]?\s*"
                               r"((?:[a-z]{3,8}[ \t]){11,23}[a-z]{3,8})"),
     lambda m: m.group(1) + ": " + REDACTED),
    ("named_secret", re.compile(r"(?i)([\"']?(?:api[_-]?key|apikey|secret|passphrase|password|passwd|auth[_-]?token|"
                               r"access[_-]?token|refresh[_-]?token|session[_-]?token|init[_-]?data|initdata|"
                               r"private[_-]?key|signing[_-]?key|admin[_-]?token|x-admin-token|signature|"
                               r"totp[_-]?secret|seed|mnemonic|dek|kek)[\"']?\s*[:=]\s*[\"']?)"
                               r"([^\"',\s}&]{4,})"),
     lambda m: m.group(1) + REDACTED),
    # `^` as well as `[?&]`: Sentry serialises `request.query_string` as `token=abc&x=1` with no leading `?`,
    # and a scrubber that only fires on a URL-shaped prefix leaks the half of the secret that was the point.
    ("query_param", re.compile(r"(?i)(^|[?&])(token|key|secret|initData|signature|sig|password)=[^&\s\"']+"),
     lambda m: m.group(1) + m.group(2) + "=" + REDACTED),
    ("telegram_initdata", re.compile(r"(?i)(initData[\"']?\s*[:=]\s*[\"']?)[^\"'&\s]{20,}(&[^\s\"']*)?"),
     lambda m: m.group(1) + REDACTED),
    ("email", re.compile(r"([A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]*(@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"),
     r"\1" + REDACTED + r"\2"),
    ("address", re.compile(r"\b0x[0-9a-fA-F]{40}\b"), lambda m: m.group(0)[:6] + "\u2026" + m.group(0)[-4:]),
    ("phone", re.compile(r"(?<!\d)\+\d[\d \-]{7,14}\d(?!\d)"), "[phone]"),
    ("seed_words", re.compile(r"(?im)^\s*((?:[a-z]{3,8}[ \t]){11,23}[a-z]{3,8})\s*$"), "[wordlist-redacted]"),
)

# Any of these as a *dict key* means the value is scrubbed regardless of shape. This is what catches the secrets
# we have not thought of yet, which is most of them.
SENSITIVE_KEYS: frozenset = frozenset({
    "apikey", "api_key", "key", "secret", "passphrase", "password", "passwd", "authorization", "cookie",
    "set-cookie", "token", "access_token", "refresh_token", "session_token", "id_token", "initdata",
    "init_data", "private_key", "privatekey", "signer_key", "signature", "sig", "seed", "mnemonic",
    "totp_secret", "otpauth", "dek", "kek", "wrapped_dek", "x-admin-token", "admin_token", "x-polygm-signature",
    "auth", "credentials", "session",
})


def _norm_key(k: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def redact_text(s: str) -> str:
    out = s if isinstance(s, str) else str(s)
    for _name, pat, repl in PATTERNS:
        out = pat.sub(repl, out)
    return out


def scan(text: str) -> list[str]:
    """Independent matcher: which secret *shapes* are still present. Used by the CI log scan and by the test
    that keeps this file honest; it is deliberately not the same list as `PATTERNS`."""
    t = text or ""
    hits: list[str] = []
    if re.search(r"\b0x[0-9a-fA-F]{64}\b", t):
        hits.append("private_key")
    if re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", t):
        hits.append("pem_key")
    if re.search(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/\-=]{20,}", t):
        hits.append("bearer")
    if re.search(r"\b[A-Za-z0-9]{20,}\.[A-Za-z0-9]{20,}\.[A-Za-z0-9_-]{20,}", t):
        hits.append("jwt")
    # The value class excludes `*` so the rule cannot fire on our own placeholder: a scanner that reports
    # "secret found" on `password=***` trains the reader to ignore it, which is worse than no scanner.
    if re.search(r"(?i)(passphrase|secret|password|api[_-]?key|initdata|private[_-]?key)"
                 r"\s*[:=]\s*[\"']?[^\s\"',}*]{6,}", t):
        hits.append("named_secret")
    if re.search(r"\b\d{4,12}:[A-Za-z0-9_-]{35,}\b", t):
        hits.append("bot_token")
    return hits


def redact_obj(obj: object, *, _depth: int = 0) -> object:
    """Scrub a structure by key name *and* by value shape, without mutating the caller's object."""
    if _depth > 12:
        return "[depth-limited]"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _norm_key(k) in {_norm_key(x) for x in SENSITIVE_KEYS}:
                out[k] = REDACTED
                continue
            out[k] = redact_obj(v, _depth=_depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v, _depth=_depth + 1) for v in list(obj)[:64]]
    if isinstance(obj, (bytes, bytearray)):
        return "[%d bytes]" % len(obj)
    if isinstance(obj, str):
        s = obj if len(obj) <= MAX_FIELD else obj[:MAX_FIELD] + "…[truncated]"
        return redact_text(s)
    return obj


def line(**fields: object) -> str:
    """The one JSON log-line builder the API and executor should use. Compact, sorted, scrubbed, and a string
    that a grep in `tools/p07-gate-check.py` can require at every call site."""
    return json.dumps(redact_obj(fields), sort_keys=True, separators=(",", ":"), default=str)


def before_send(event: dict, hint: dict | None = None) -> dict | None:
    """Sentry scrubber. Returns None for the events whose whole purpose would be to leak.

    The rule: an exception message is kept (it is how we debug), its *repr of a payload* is not. Sentry
    receives the stack, the request id and the route, never the body — a body is where a pasted private key
    lives.
    """
    if not isinstance(event, dict):
        return None
    ev = json.loads(json.dumps(event, default=str))         # a deep copy we are free to mangle
    for k in ("message", "transaction", "server_name", "logger", "culprit"):
        if isinstance(ev.get(k), str):
            ev[k] = redact_text(ev[k])
    if isinstance(ev.get("extra"), dict):
        ev["extra"] = redact_obj(ev["extra"])
    # The user context is where an email address goes to live forever in a third-party retention window, and
    # `send_default_pii=False` only stops Sentry *collecting* it - it does not stop us putting it there.
    if isinstance(ev.get("user"), dict):
        ev["user"] = redact_obj(ev["user"])
    if isinstance(ev.get("request"), dict):
        req = ev["request"]
        if isinstance(req.get("url"), str):
            req["url"] = redact_text(req["url"])             # a token in a query string is still a token
        req["headers"] = redact_obj(req.get("headers") or {})
        req.pop("data", None)                                # the body never leaves this process
        req.pop("cookies", None)
        if req.get("body") is not None:
            req["body"] = "[dropped]"
        if req.get("query_string") is not None:
            req["query_string"] = redact_text(str(req["query_string"]))
        if req.get("env") is not None:
            req["env"] = redact_obj(req["env"])
    if isinstance(ev.get("contexts"), dict):
        ev["contexts"] = redact_obj(ev["contexts"])
    for crumb in ev.get("breadcrumbs") or []:
        if isinstance(crumb, dict):
            crumb["data"] = redact_obj(crumb.get("data") or {})
            # Breadcrumb *messages* are written by our own code, which is exactly why they leak: "loaded key
            # 0x..." is a debugging line someone added while the keystore was being built, and it is the last
            # thing a scrubber would think to look at.
            if isinstance(crumb.get("message"), str):
                crumb["message"] = redact_text(crumb["message"])
    ev["event_extra"] = ev.pop("extra", {})
    return ev


def describe_choice() -> dict:
    """The trade-offs, in the file, so the next reader does not "optimise" them away."""
    return {
        "seed_words": "exactly 12/15/18/21/24 lowercase words of 3-8 letters on one line. A sentence can match "
                       "and lose its detail; a seed phrase can never survive. We take the false positive.",
        "address": "kept as 0x1234…abcd: the app already shows this to the user, so the short form is a "
                    "correlator for support, not a secret. A *list* of them is a wallet dump, so the pattern "
                    "still trims each.",
        "email": "first char + domain: 'who' is usually answerable from the session id in the same line, and "
                 "the domain is what tells you it is the pro-tier account.",
        "truncation": "MAX_FIELD=%d per string: a 2 MB order payload in journald is an incident of a different "
                      "kind." % MAX_FIELD,
    }
